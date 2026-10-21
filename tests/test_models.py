import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from autonomous_studio.domain.models import (
    AudioAsset,
    Brief,
    Claim,
    ProviderResult,
    ResearchBrief,
    Shot,
    TitleShot,
    VoiceSegment,
    validate_story_scope,
    validate_timeline,
)
from autonomous_studio.providers.mock import MockDirector
from autonomous_studio.workflow.engine import load_brief


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data["research"]["sources"].clear(),
        lambda data: data["research"].update(ranking=None),
        lambda data: data["research"]["subjects"][0].update(rank=1),
        lambda data: data["research"]["claims"][0].update(value=1),
        lambda data: data.update(width=541),
        lambda data: data.update(schema_version=2),
    ],
)
def test_invalid_briefs_fail(brief, change):
    data = deepcopy(brief.model_dump(mode="json"))
    change(data)
    with pytest.raises(ValidationError):
        Brief.model_validate(data)


def test_discriminated_unions_reject_wrong_shapes():
    with pytest.raises(ValidationError):
        TypeAdapter(Claim).validate_python(
            dict(
                kind="qualitative",
                id="x",
                text="test",
                source_ids=["s"],
                confidence=0.5,
                status="supported",
                value=42,
            )
        )
    with pytest.raises(ValidationError):
        TypeAdapter(Shot).validate_python(
            dict(kind="rank_reveal", id="x", title="T", start=0, duration=3)
        )
    with pytest.raises(ValidationError):
        TypeAdapter(AudioAsset).validate_python(dict(kind="music", spoken_text="no"))
    with pytest.raises(ValidationError):
        TypeAdapter(ProviderResult).validate_python(dict(kind="failure", artifacts=[]))


def test_numeric_rankings_support_both_directions(brief):
    descending = brief.research.model_dump(mode="json")
    assert ResearchBrief.model_validate(descending).ranking.direction == "descending"

    ascending = deepcopy(descending)
    ascending["ranking"]["direction"] = "ascending"
    claims = {claim["id"]: claim for claim in ascending["claims"]}
    for subject in ascending["subjects"]:
        claim = claims[subject["claim_id"]]
        claim["value"] = subject["rank"]
        claim["unit"] = "chart position"
        claim["lower"] = claim["upper"] = None
    assert ResearchBrief.model_validate(ascending).ranking.direction == "ascending"

    ascending["ranking"]["direction"] = "descending"
    with pytest.raises(ValidationError, match="rank 1 must have the largest value"):
        ResearchBrief.model_validate(ascending)


def test_nonmonotonic_numeric_ranking_requires_metric_or_tie_break(brief):
    data = brief.research.model_dump(mode="json")
    claims = {claim["id"]: claim for claim in data["claims"]}
    values = [1, 3, 2, 4, 5]
    for subject, value in zip(sorted(data["subjects"], key=lambda item: item["rank"]), values):
        claim = claims[subject["claim_id"]]
        claim["value"] = value
        claim["lower"] = claim["upper"] = None
    with pytest.raises(ValidationError, match="do not establish a monotonic ranking"):
        ResearchBrief.model_validate(data)


def test_timing_and_cross_references(brief):
    spec = asyncio.run(MockDirector().develop(brief, []))
    validate_timeline(spec.shots, brief.duration, brief.research)
    spec.shots[1].start += 0.1
    with pytest.raises(ValueError, match="contiguous"):
        validate_timeline(spec.shots, brief.duration, brief.research)
    spec.shots[1].start -= 0.1
    spec.shots[0].voice_segments = [
        VoiceSegment(shot_id="hook", spoken_text="too long", estimated_duration=20)
    ]
    with pytest.raises(ValueError, match="narration exceeds"):
        validate_timeline(spec.shots, brief.duration, brief.research)


def test_touching_voice_segments_allow_float_noise_but_reject_real_overlap(brief):
    shot = TitleShot(
        id="hook",
        start=0,
        duration=8,
        title="Opening",
        voice_segments=[
            VoiceSegment(
                shot_id="hook",
                spoken_text="First sentence.",
                offset=0.2,
                estimated_duration=6.4,
                pause_after=0.5,
            ),
            VoiceSegment(
                shot_id="hook",
                spoken_text="Second sentence.",
                offset=7.1,
                estimated_duration=0.5,
                pause_after=0,
            ),
        ],
    )
    validate_timeline([shot], 8, brief.research)

    shot.voice_segments[1].offset = 7.09
    with pytest.raises(ValueError, match="voice segments overlap"):
        validate_timeline([shot], 8, brief.research)


def test_dense_narration_requires_rewrite(brief):
    brief.hook_narration = (
        "This sentence has far too many words for a two second spoken introduction."
    )
    with pytest.raises(ValueError, match="rewrite"):
        asyncio.run(MockDirector().develop(brief, []))


def test_full_production_duration_and_complete_countdown_scope():
    full = load_brief(Path("examples/earthquake-full.yaml"))
    spec = asyncio.run(MockDirector().develop(full, []))
    validate_story_scope(spec)
    assert full.production_mode == "full" and full.duration == 55
    assert [s.rank for s in spec.shots if s.kind == "rank_reveal"] == [5, 4, 3, 2, 1]
    assert len(spec.shots) == 8 and spec.shots[-1].kind == "source_card"
    broken = spec.model_copy(deep=True)
    rank_shots = [s for s in broken.shots if s.kind == "rank_reveal"]
    rank_shots[0].rank, rank_shots[1].rank = rank_shots[1].rank, rank_shots[0].rank
    with pytest.raises(ValueError, match="reverse order"):
        validate_story_scope(broken)

    inferred = spec.model_copy(deep=True)
    inferred.brief.expected_rank_count = None
    validate_story_scope(inferred)
    inferred.shots = [shot for shot in inferred.shots if getattr(shot, "rank", None) != 5]
    with pytest.raises(ValueError, match="every rank once"):
        validate_story_scope(inferred)


@pytest.mark.parametrize(
    ("mode", "duration"),
    [("prototype", 16), ("prototype", 9), ("full", 44), ("full", 181)],
)
def test_production_mode_duration_bounds(brief, mode, duration):
    data = brief.model_dump(mode="json")
    data.update(production_mode=mode, duration=duration, shots=[])
    with pytest.raises(ValidationError, match="duration"):
        Brief.model_validate(data)


def test_full_short_may_extend_beyond_sixty_seconds(brief):
    data = brief.model_dump(mode="json")
    data.update(production_mode="full", duration=90, shots=[])
    assert Brief.model_validate(data).duration == 90
