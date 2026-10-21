"""The live semantic stage is durable and never re-buys valid decisions on resume."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from autonomous_studio.domain.semantic import SemanticDecisionBatch
from autonomous_studio.workflow import longform_production
from autonomous_studio.workflow.semantic_integration import (
    require_family_first_decisions,
    semantic_input_hash,
)


def test_empty_role_contract_preserves_saved_semantic_hash():
    beat = SimpleNamespace(asset_ref="a1")
    block = SimpleNamespace(id="b1")
    payload = {"id": "ch1", "visual_beats": [{"id": "v1"}]}
    chapter = SimpleNamespace(
        narration_blocks=[block], visual_beats=[beat],
        model_dump=lambda **_: {
            **payload,
            "visual_beats": [item.copy() for item in payload["visual_beats"]],
        },
    )
    project = SimpleNamespace(
        longform_voices={"b1": SimpleNamespace(artifact=SimpleNamespace(sha256="a" * 64))},
        longform_word_timings={"b1": []},
        longform_media={"a1": SimpleNamespace(sha256="b" * 64)},
        longform_visual_policy=SimpleNamespace(model_dump=lambda **_: {}),
        live_models={},
    )
    base = SimpleNamespace(model_dump=lambda **_: {"shots": []})
    saved = semantic_input_hash(project, chapter, base)
    payload["visual_beats"][0]["role_semantics"] = []
    assert semantic_input_hash(project, chapter, base) == saved
    payload["visual_beats"][0]["role_semantics"] = ["expected", "observed"]
    assert semantic_input_hash(project, chapter, base) != saved


def test_saved_semantic_chapter_keeps_its_original_model_hash():
    chapter = SimpleNamespace(
        id="ch1",
        narration_blocks=[SimpleNamespace(id="b1")],
        visual_beats=[SimpleNamespace(asset_ref="a1")],
        model_dump=lambda **_: {"id": "ch1", "visual_beats": []},
    )
    project = SimpleNamespace(
        longform_voices={"b1": SimpleNamespace(artifact=SimpleNamespace(sha256="a" * 64))},
        longform_word_timings={"b1": []},
        longform_media={"a1": SimpleNamespace(sha256="b" * 64)},
        longform_visual_policy=SimpleNamespace(model_dump=lambda **_: {}),
        live_models={"director_model": "gpt-5.6-sol"},
        longform_semantic_models={},
    )
    base = SimpleNamespace(model_dump=lambda **_: {"shots": []})
    old_hash = semantic_input_hash(project, chapter, base)
    project.longform_semantic_models["ch1"] = "gpt-5.6-sol"
    project.live_models["semantic_layout_model"] = "gpt-6-luna"
    assert semantic_input_hash(project, chapter, base) == old_hash
    project.longform_semantic_models.pop("ch1")
    assert semantic_input_hash(project, chapter, base) != old_hash


def test_semantic_stage_persists_chapter_and_reuses_it_on_retry(tmp_path, monkeypatch):
    block = SimpleNamespace(id="b1")
    beat = SimpleNamespace(asset_ref="image1")
    chapter = SimpleNamespace(
        id="ch1",
        narration_blocks=[block],
        visual_beats=[beat],
        model_dump=lambda **_: {"id": "ch1", "visual_beats": [], "narration_blocks": []},
    )
    voice = SimpleNamespace(artifact=SimpleNamespace(sha256="a" * 64))
    project = SimpleNamespace(
        pipeline="live",
        spec=SimpleNamespace(
            chapters=[chapter], brief=SimpleNamespace(width=1920, height=1080, fps=24)
        ),
        longform_visual_policy=SimpleNamespace(
            semantic_diagrams=True, model_dump=lambda **_: {"semantic_diagrams": True}
        ),
        longform_voices={"b1": voice},
        longform_word_timings={"b1": [{"word": "hello", "start": 0.0, "end": 0.5}]},
        longform_media={"image1": SimpleNamespace(sha256="b" * 64)},
        longform_semantic_decisions={},
        longform_semantic_hashes={},
        live_models={},
        artifacts=[],
        model_dump=lambda **_: {
            "spec": {"chapters": [{"id": "ch1", "visual_beats": [], "narration_blocks": []}]}
        },
    )
    project.current_artifacts = lambda: project.artifacts
    plan = SimpleNamespace(shots=[], model_dump=lambda **_: {"chapter_id": "ch1", "shots": []})
    monkeypatch.setattr(longform_production, "narration_windows", lambda *_: [(block, voice, 0, 1)])
    monkeypatch.setattr(
        longform_production, "plan_motion_chapter", lambda *_args, **_kw: (plan, {})
    )
    monkeypatch.setattr(
        longform_production,
        "apply_semantic_decisions",
        lambda *_args: (plan, {"applied": 0}),
    )
    from autonomous_studio.workflow import visual_reveal_timing

    preflight_allowances = []
    monkeypatch.setattr(
        visual_reveal_timing,
        "validate_measured_reveal_tails",
        lambda *_args, planning_allowance_seconds: preflight_allowances.append(
            planning_allowance_seconds
        ),
    )

    class Repo:
        def __init__(self):
            self.events = []

        def attempt_dir(self, _project, stage):
            target = Path(tmp_path) / stage
            target.mkdir()
            return target

        def artifact(self, _project, _path, role):
            return SimpleNamespace(role=role)

        def save(self, _project, kind, payload):
            self.events.append((kind, payload))

    class Director:
        calls = 0

        async def propose(self, *_args):
            self.calls += 1
            return SemanticDecisionBatch(chapter_id="ch1", decisions=[])

    repo, director = Repo(), Director()
    studio = SimpleNamespace(repo=repo, longform_semantic_director=director)
    asyncio.run(longform_production.prepare_longform_semantic_decisions(studio, project))
    asyncio.run(longform_production.prepare_longform_semantic_decisions(studio, project))
    assert director.calls == 1
    assert preflight_allowances == [visual_reveal_timing.ESTIMATE_TOLERANCE_SECONDS]
    assert len(repo.events) == 1
    assert project.longform_semantic_hashes["ch1"]
    assert len(project.artifacts) == 3


def test_family_first_cutover_blocks_missing_or_legacy_decisions():
    beat = SimpleNamespace(
        id="v1", visual_type="timeline", design_family="chronology",
        design_treatment="milestones",
    )
    chapter = SimpleNamespace(id="ch1", visual_beats=[beat])
    project = SimpleNamespace(pipeline="live", longform_semantic_decisions={})
    with pytest.raises(ValueError, match="saved v2 semantic decisions"):
        require_family_first_decisions(project, chapter)
    project.longform_semantic_decisions["ch1"] = SemanticDecisionBatch(
        chapter_id="ch1", decisions=[]
    )
    with pytest.raises(ValueError, match="saved v2 semantic decisions"):
        require_family_first_decisions(project, chapter)
    project.longform_semantic_decisions["ch1"].schema_version = 2
    require_family_first_decisions(project, chapter)
    beat.design_treatment = None
    with pytest.raises(ValueError, match="no approved family/treatment"):
        require_family_first_decisions(project, chapter)


def test_chapter_stage_fails_closed_before_renderer_when_decisions_are_missing():
    beat = SimpleNamespace(
        id="v1", visual_type="timeline", design_family="chronology",
        design_treatment="milestones",
    )
    chapter = SimpleNamespace(id="ch1", visual_beats=[beat])
    studio = SimpleNamespace(
        longform_renderer=SimpleNamespace(chapter=lambda *_: pytest.fail("legacy render ran"))
    )
    project = SimpleNamespace(
        pipeline="live", spec=SimpleNamespace(chapters=[chapter]),
        longform_semantic_decisions={},
    )
    with pytest.raises(ValueError, match="saved v2 semantic decisions"):
        longform_production.render_longform_chapters(studio, project)
