import asyncio
import json
from pathlib import Path

import pytest
from google.auth.exceptions import RefreshError

from autonomous_studio.config import Settings, load_local_env
from autonomous_studio.domain.longform_policy import LongFormCreativePolicy
from autonomous_studio.domain.models import MetricShot, ResearchBrief, State, TitleShot
from autonomous_studio.hitl.adapters import TelegramAdapter, TelegramConfig, review_message
from autonomous_studio.hitl.worker import TelegramWorker
from autonomous_studio.providers.http import RemoteError
from autonomous_studio.providers.mock import MockDirector, tone
from autonomous_studio.providers.openai import (
    IncompleteResponseError,
    NarrativeDraft,
    OpenAIClient,
    OpenAIDirector,
    OpenAIResearch,
    OpenAIVoice,
    ProviderTimeoutError,
    ShotPlanDraft,
    StoryDraft,
    build_narrative_prompt,
    director_context,
    normalize_shot_plan,
    output_text,
    strict_schema,
    validate_narrative_draft,
    validate_shot_type_choices,
)
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief
from autonomous_studio.workflow.research import recover_research


def response(value, urls=()):
    return {
        "status": "completed",
        "output": [
            {"type": "web_search_call", "action": {"sources": [{"url": u} for u in urls]}},
            {"type": "message", "content": [{"type": "output_text", "text": value}]},
        ],
    }


class FakeHTTP:
    def __init__(self, replies=(), audio=b"", polls=()):
        self.replies, self.audio, self.polls = list(replies), audio, list(polls)
        self.calls = []

    async def json(self, url, payload, headers=None, **kwargs):
        self.calls.append((url.rsplit("/", 1)[-1], payload))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def get_json(self, url, headers=None, **kwargs):
        self.calls.append((f"GET:{url.rsplit('/', 1)[-1]}", None))
        reply = self.polls.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def post(self, url, body, headers=None, **kwargs):
        self.calls.append((url.rsplit("/", 1)[-1], body))
        return self.audio


def directing_replies(spec):
    """Create a coherent locked narrative plus a shot plan from deterministic fixture shots."""
    shots = [shot.model_copy(deep=True) for shot in spec.shots]
    if spec.brief.production_mode == "full":
        hook = "The strongest earthquake was not the deadliest."
        loop_bridge = "That is why strength alone cannot tell us which earthquake was deadliest."
        narration = {
            "hook": hook,
            "rank-5": ("At number five, Haiyuan shows why destruction is more than magnitude."),
            "rank-4": ("Then the Indian Ocean disaster carried its force across coastlines."),
            "rank-3": ("Tangshan followed, while dense cities turned shaking into catastrophe."),
            "rank-2": (
                "Haiti ranks second here, but its disputed toll warns us not to confuse "
                "estimates with certainty."
            ),
            "rank-1": ("Finally, Shaanxi stands first under this source-specific measure."),
            "payoff": loop_bridge,
        }
        candidates = [
            hook,
            "Magnitude alone cannot explain an earthquake's toll.",
        ]
    else:
        hook = "Earthquakes can change history in seconds."
        loop_bridge = ""
        narration = {
            shots[0].id: hook,
            next(shot.id for shot in shots if shot.voice_segments and shot.id != shots[0].id): (
                "Haiyuan shows why magnitude cannot predict human loss."
            ),
        }
        candidates = [hook, "One earthquake can redraw a human story."]

    for shot in shots:
        shot.shot_type_reasoning = (
            f"The narrated {shot.id} idea is shown with a {shot.kind} visual "
            "so its specific claim remains legible."
        )
        if shot.id == shots[0].id:
            shot.title = hook
        if shot.id in narration:
            text = narration[shot.id]
            segment = shot.voice_segments[0]
            segment.spoken_text = text
            segment.estimated_duration = max(
                0.35, len(text.split()) * 60 / spec.brief.voice.pace_wpm
            )
    source_duration = sum(shot.duration for shot in shots if shot.kind == "source_card")
    shots = [shot for shot in shots if shot.kind != "source_card"]
    if source_duration:
        shots[-1].duration += source_duration
    if spec.brief.production_mode == "full":
        shots[-1].title = hook

    script = " ".join(
        segment.spoken_text
        for shot in shots
        for segment in sorted(shot.voice_segments, key=lambda item: item.offset)
    )
    narrative = {
        "candidate_hooks": candidates,
        "hook": hook,
        "thesis": "Destruction depends on human exposure, not shaking alone.",
        "payoff": "Every ranking needs its metric and its uncertainty.",
        "audio_direction": "Natural, curious and respectful.",
        "narrative_beats": ["Orient", "Build the contrast", "Resolve the measure"],
        "narration_script": script,
        "loop_bridge": loop_bridge,
    }
    plan = {
        "shots": [shot.model_dump(mode="json") for shot in shots],
        "shot_type_reviews": [
            {
                "shot_id": shot.id,
                "suggested_kind": "keep_current",
                "reason": "The chosen visual already matches this spoken claim.",
            }
            for shot in shots
        ],
    }
    return narrative, plan


def test_env_is_scoped_and_process_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "already-set")
    monkeypatch.setenv("STUDIO_MAX_OUTPUT_TOKENS", "12000")
    monkeypatch.setenv("STUDIO_YOUTUBE_CATEGORY_ID", "28")
    monkeypatch.delenv("STUDIO_VOICE", raising=False)
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=ignored\nSTUDIO_VOICE=coral\nPATH=bad\n")
    load_local_env(path)
    import os

    assert os.environ["OPENAI_API_KEY"] == "already-set"
    assert os.environ["STUDIO_VOICE"] == "coral"
    assert os.environ["PATH"] != "bad"
    assert Settings.environment().max_output_tokens == 12000
    assert Settings.environment().youtube_category_id == "28"


def test_default_output_limit_is_20000_and_can_be_overridden(monkeypatch):
    monkeypatch.delenv("STUDIO_MAX_OUTPUT_TOKENS", raising=False)
    assert Settings().max_output_tokens == 20000
    assert Settings.environment().max_output_tokens == 20000
    monkeypatch.setenv("STUDIO_MAX_OUTPUT_TOKENS", "12000")
    assert Settings.environment().max_output_tokens == 12000


@pytest.mark.parametrize(
    "schema",
    [
        ResearchBrief.model_json_schema(),
        StoryDraft.model_json_schema(),
        NarrativeDraft.model_json_schema(),
        ShotPlanDraft.model_json_schema(),
    ],
)
def test_strict_schema(schema):
    def inspect(value):
        if isinstance(value, dict):
            assert not {"default", "discriminator", "oneOf"} & value.keys()
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value.get("properties", {}))
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(strict_schema(schema))


def test_unsupported_strict_schema_fails_before_openai_request(tmp_path):
    http = FakeHTTP([response("unused")])
    client = OpenAIClient(Settings(api_key="test"), tmp_path, http)
    invalid = {
        "type": "object",
        "properties": {
            "special_copy": {
                "type": "object",
                "propertyNames": {"enum": ["change"]},
                "additionalProperties": {"type": "string"},
            },
        },
    }
    with pytest.raises(ValueError, match="propertyNames"):
        asyncio.run(client.response("gpt-6-luna", "visual selection", schema=invalid))
    assert http.calls == []


def test_shot_type_audit_is_advisory_and_rejects_unimplemented_render_kinds():
    from autonomous_studio.domain.models import ShotTypeReview

    review = ShotTypeReview(
        shot_id="shot_02", suggested_kind="process_flow",
        reason="The causal stages should appear in sequence.",
    )
    assert review.suggested_kind == "process_flow"
    with pytest.raises(ValueError):
        ShotTypeReview(
            shot_id="shot_02", suggested_kind="invented_kind",
            reason="This is not one of the four candidates.",
        )


def test_response_cache_and_refusal(tmp_path):
    http = FakeHTTP([response("hello")])
    client = OpenAIClient(Settings(api_key="test"), tmp_path, http)
    asyncio.run(client.response("model", "prompt"))
    again = OpenAIClient(Settings(), tmp_path, http)
    asyncio.run(again.response("model", "prompt"))
    assert len(http.calls) == 1
    assert again.trace[0]["cache_hit"]
    with pytest.raises(ValueError, match="incomplete"):
        output_text({"status": "incomplete"})
    with pytest.raises(ValueError, match="declined"):
        output_text({"status": "completed", "output": [{"content": [{"type": "refusal"}]}]})


def test_incomplete_response_is_retried_and_receipted(tmp_path):
    incomplete = {
        "id": "resp-incomplete",
        "model": "model",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "usage": {"output_tokens": 10000},
    }
    http = FakeHTTP([incomplete, response("recovered")])
    client = OpenAIClient(Settings(api_key="test", max_output_tokens=12000), tmp_path, http)

    result = asyncio.run(client.response("model", "prompt"))

    assert output_text(result) == "recovered" and len(http.calls) == 2
    receipts = list(tmp_path.glob("*.incomplete-1.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["incomplete_details"]["reason"] == "max_output_tokens"
    assert receipt["configured_max_output_tokens"] == 12000
    assert "output" not in receipt


def test_longform_output_limit_failure_is_receipted_without_creative_retry(tmp_path):
    incomplete = {
        "id": "resp-limit",
        "model": "gpt-6-luna",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "usage": {"output_tokens": 20000},
    }
    http = FakeHTTP([incomplete, response("should not be requested")])
    client = OpenAIClient(Settings(api_key="test"), tmp_path, http)
    client.longform_creative_policy = LongFormCreativePolicy()

    with pytest.raises(IncompleteResponseError, match="max_output_tokens"):
        asyncio.run(client.response("gpt-6-luna", "chapter visual selection"))

    assert len(http.calls) == 1
    assert http.calls[0][1]["max_output_tokens"] == 20000
    receipts = list(tmp_path.glob("*.incomplete-1.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["incomplete_details"]["reason"] == "max_output_tokens"
    assert receipt["usage"]["output_tokens"] == 20000
    assert receipt["configured_max_output_tokens"] == 20000


def test_background_response_is_journaled_and_polled(tmp_path):
    queued = {"id": "resp-background", "model": "model", "status": "queued"}
    in_progress = {"id": "resp-background", "model": "model", "status": "in_progress"}
    completed = {**response("finished"), "id": "resp-background", "model": "model"}
    http = FakeHTTP([queued], polls=[in_progress, completed])
    client = OpenAIClient(Settings(api_key="test"), tmp_path, http)
    client.background_poll_interval = 0

    result = asyncio.run(client.response("model", "prompt", background=True))

    assert output_text(result) == "finished"
    assert http.calls[0][1]["background"] is True and http.calls[0][1]["store"] is True
    journal = json.loads(next(tmp_path.glob("*.background-1.job.json")).read_text())
    assert journal["response_id"] == "resp-background" and journal["status"] == "completed"


def test_background_incomplete_response_is_not_replayed_on_explicit_retry(tmp_path):
    incomplete = {
        "id": "resp-incomplete",
        "model": "model",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
    }
    http = FakeHTTP([incomplete, {**response("fresh treatment"), "id": "resp-fresh"}])
    client = OpenAIClient(Settings(api_key="test"), tmp_path, http)
    client.longform_creative_policy = LongFormCreativePolicy()

    with pytest.raises(IncompleteResponseError, match="max_output_tokens"):
        asyncio.run(client.response("model", "same treatment", background=True))

    result = asyncio.run(client.response("model", "same treatment", background=True))

    assert output_text(result) == "fresh treatment"
    assert len(http.calls) == 2
    journal = json.loads(next(tmp_path.glob("*.background-1.job.json")).read_text())
    assert journal["response_id"] == "resp-fresh"


def test_background_polling_resumes_from_saved_response_id(tmp_path):
    queued = {"id": "resp-resume", "model": "model", "status": "queued"}
    first_http = FakeHTTP([queued], polls=[RemoteError("OpenAI", kind="timeout")])
    first = OpenAIClient(Settings(api_key="test"), tmp_path, first_http)
    first.background_poll_interval = 0
    first.background_poll_errors = 1

    with pytest.raises(ProviderTimeoutError, match="resume polling"):
        asyncio.run(first.response("model", "prompt", background=True))

    completed = {**response("resumed"), "id": "resp-resume", "model": "model"}
    second_http = FakeHTTP(polls=[completed])
    second = OpenAIClient(Settings(api_key="test"), tmp_path, second_http)
    second.background_poll_interval = 0
    result = asyncio.run(second.response("model", "prompt", background=True))

    assert output_text(result) == "resumed"
    assert second_http.calls == [("GET:resp-resume", None)]


def test_background_polling_preserves_definitive_http_error(tmp_path):
    queued = {"id": "resp-missing", "model": "model", "status": "queued"}
    http = FakeHTTP([queued], polls=[RemoteError("OpenAI", status=404)])
    client = OpenAIClient(Settings(api_key="test"), tmp_path, http)
    client.background_poll_interval = 0

    with pytest.raises(RemoteError, match=r"HTTP 404") as captured:
        asyncio.run(client.response("model", "prompt", background=True))

    assert captured.value.status == 404
    assert len(http.calls) == 2


def test_rejected_cached_response_is_not_replayed(tmp_path):
    http = FakeHTTP([response("invalid"), response("valid")])
    client = OpenAIClient(Settings(api_key="test"), tmp_path, http)
    assert output_text(asyncio.run(client.response("model", "same prompt"))) == "invalid"
    client.reject_last("semantic validation failed")

    assert output_text(asyncio.run(client.response("model", "same prompt"))) == "valid"
    assert len(http.calls) == 2


def test_research_rejects_unobserved_citations(tmp_path, brief):
    http = FakeHTTP(
        [
            response("Evidence", ["https://example.org/observed"]),
            response(brief.research.model_dump_json()),
        ]
    )
    provider = OpenAIResearch(OpenAIClient(Settings(api_key="test"), tmp_path, http))
    with pytest.raises(ValueError, match="not observed"):
        asyncio.run(provider.research(brief))


def test_research_repairs_clear_ranking_direction_without_an_extra_call(tmp_path, brief):
    data = brief.research.model_dump(mode="json")
    claims = {claim["id"]: claim for claim in data["claims"]}
    for subject in data["subjects"]:
        claim = claims[subject["claim_id"]]
        claim["value"] = subject["rank"]
        claim["unit"] = "chart position"
        claim["lower"] = claim["upper"] = None
    data["ranking"]["direction"] = "descending"
    urls = [source.url for source in brief.research.sources]
    http = FakeHTTP([response("Evidence", urls), response(json.dumps(data))])
    provider = OpenAIResearch(OpenAIClient(Settings(api_key="test"), tmp_path, http))

    result = asyncio.run(provider.research(brief))

    assert result.ranking.direction == "ascending"
    assert provider.repairs == [
        {
            "kind": "ranking_direction",
            "from": "descending",
            "to": "ascending",
            "reason": "rank-ordered numeric claim values establish the opposite direction",
            "values": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
    ]
    assert len(http.calls) == 2


def test_cached_research_recovery_advances_without_provider_calls(tmp_path, brief):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_topic("Top five chart positions", project_id="123456abcdef")
    data = brief.research.model_dump(mode="json")
    claims = {claim["id"]: claim for claim in data["claims"]}
    for subject in data["subjects"]:
        claim = claims[subject["claim_id"]]
        claim["value"] = subject["rank"]
        claim["unit"] = "chart position"
        claim["lower"] = claim["upper"] = None
    data["ranking"]["direction"] = "descending"
    cache = studio.repo.project_dir(project.id) / "provider-cache"
    cache.mkdir()
    evidence_path = cache / "evidence.json"
    structured_path = cache / "structured.json"
    urls = [source.url for source in brief.research.sources]
    evidence_path.write_text(json.dumps(response("Evidence", urls)), encoding="utf-8")
    structured_path.write_text(json.dumps(response(json.dumps(data))), encoding="utf-8")
    project.last_error = "old direction failure"
    studio.repo.save(project, "stage_failed", {"state": project.state})

    recovered = recover_research(
        studio,
        project.id,
        evidence_path,
        structured_path,
        version=1,
        actor="test",
    )

    assert recovered.state == State.RESEARCHED and recovered.last_error is None
    assert recovered.spec.brief.research.ranking.direction == "ascending"
    assert len(recovered.current_artifacts("provider_usage")) == 2
    assert len(recovered.current_artifacts("research_repair")) == 1
    event = studio.repo.events(project.id)[-1]
    assert event["kind"] == "research_revalidated"
    assert event["payload"]["additional_api_calls"] == 0


def test_live_pipeline_to_story_restarts_without_new_calls(tmp_path, brief):
    spec = asyncio.run(MockDirector().develop(brief, []))
    narrative, plan = directing_replies(spec)
    http = FakeHTTP(
        [
            response("Evidence ledger", [s.url for s in brief.research.sources]),
            response(brief.research.model_dump_json()),
            response(json.dumps(narrative)),
            response(json.dumps(plan)),
        ]
    )
    settings = Settings(api_key="test")
    studio = Studio(tmp_path, settings=settings, live_http=http)
    project = studio.create(brief, pipeline="live")
    result = asyncio.run(studio.run(project.id))
    assert result.state == State.STORY_APPROVAL
    assert len(result.current_artifacts("provider_usage")) == 2
    director_usage = json.loads(
        studio.repo.verify(result, result.current_artifacts("provider_usage")[-1]).read_text()
    )
    assert len(director_usage) == 2
    assert len(http.calls) == 4
    assert result.spec.narration_script
    assert result.spec.shots[0].title == result.spec.hook
    assert len(result.spec.shot_type_reviews) == len(result.spec.shots)
    assert len(result.current_artifacts("shot_type_review")) == 1
    reasons = json.loads(
        studio.repo.verify(result, result.current_artifacts("shot_type_reasoning")[0]).read_text()
    )
    assert len(reasons) == len(result.spec.shots)
    assert all(item["shot_type_reasoning"] for item in reasons)
    story_review = review_message(result, result.pending())
    assert "Chosen shot types (why):" in story_review.text
    assert result.spec.shots[0].shot_type_reasoning in story_review.text
    assert result.spec.shots[0].voice_segments[0].spoken_text == result.spec.hook
    assert not result.spec.audio_assets
    assert asyncio.run(Studio(tmp_path).run(project.id)).pending().id == result.pending().id
    assert len(http.calls) == 4


def test_short_model_roles_are_pinned_and_can_be_adopted_without_media_changes(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_topic("A test documentary subject", duration=55)
    assert project.live_models["research_model"] == "gpt-6-luna"
    assert project.live_models["director_model"] == "gpt-6.1-sol"
    assert project.live_models["shorts_repair_model"] == "gpt-6-luna"
    assert project.live_models["tts_model"] == "gpt-4o-mini-tts"
    image_model = project.auto_assets.image_model
    project.live_models["director_model"] = "gpt-5.6-sol"
    studio.repo.save(project, "legacy_model_fixture", {})
    updated = studio.adopt_shorts_stage_models(project.id)
    assert updated.live_models["director_model"] == "gpt-6.1-sol"
    assert updated.live_models["tts_model"] == project.live_models["tts_model"]
    assert updated.auto_assets.image_model == image_model
    assert studio.repo.events(project.id)[-1]["kind"] == "shorts_stage_models_adopted"


def test_short_writer_gets_evidence_without_preliminary_subject_narration():
    brief = load_brief(Path("examples/earthquake-full.yaml"))
    brief.research.subjects[0].narration = "Research-stage wording must not steer the writer."

    prompt = build_narrative_prompt(brief, [])
    context = director_context(brief, include_subject_narration=False)

    assert "Research-stage wording must not steer the writer." not in prompt
    assert "narration" not in context["research"]["subjects"][0]
    assert context["research"]["subjects"][0]["title"] == brief.research.subjects[0].title
    assert context["research"]["subjects"][0]["rank"] == brief.research.subjects[0].rank
    assert context["research"]["claims"]
    assert context["research"]["ranking"]
    assert director_context(brief)["research"]["subjects"][0]["narration"]


def test_short_writer_requires_rank_names_only_for_countdowns():
    brief = load_brief(Path("examples/earthquake-full.yaml"))
    brief.expected_rank_count = None
    hook = "An earthquake can change a city in seconds."
    ending = "The ground tells the story of why cities must prepare."
    draft = NarrativeDraft(
        candidate_hooks=[hook, "A city can change in seconds after an earthquake."],
        hook=hook,
        thesis="Destruction is more than magnitude.",
        payoff="Preparation matters.",
        audio_direction="Measured documentary tone.",
        narrative_beats=["Shaking begins", "Cities respond", "Preparation matters"],
        narration_script=(
            f"{hook} Damage depends on where people live. "
            "Buildings and preparation shape what happens next. "
            f"{ending}"
        ),
        loop_bridge=ending,
    )
    validate_narrative_draft(brief, draft, [])
    assert "A lone research subject with a rank is not a countdown" in build_narrative_prompt(
        brief, []
    )
    brief.expected_rank_count = 5
    with pytest.raises(ValueError, match="ranked narration must name every subject"):
        validate_narrative_draft(brief, draft, [])


def test_full_director_contract_requires_complete_countdown(tmp_path):
    brief = load_brief(Path("examples/earthquake-full.yaml"))
    spec = asyncio.run(MockDirector().develop(brief, []))
    narrative, plan = directing_replies(spec)
    http = FakeHTTP([response(json.dumps(narrative)), response(json.dumps(plan))])
    director = OpenAIDirector(OpenAIClient(Settings(api_key="test"), tmp_path, http))
    result = asyncio.run(director.develop(brief, []))
    assert [s.rank for s in result.shots if s.kind == "rank_reveal"] == [5, 4, 3, 2, 1]
    assert result.loop.strategy == "semantic"
    assert result.shots[-1].kind == "outro" and result.shots[-1].title == result.hook
    assert not any(s.kind == "source_card" for s in result.shots)
    assert "45–60 second complete" in http.calls[0][1]["input"]
    assert "ONE spoken story" in http.calls[0][1]["input"]
    assert "Select only the research facts needed to tell that story" in http.calls[0][1]["input"]
    assert "A countdown must still cover every promised rank" in http.calls[0][1]["input"]
    assert "without a claim for every sentence" in http.calls[0][1]["input"]
    assert "do not invent empirical details" in http.calls[0][1]["input"]
    assert "payoff answers it rather than echoing the hook" in http.calls[0][1]["input"]
    assert "strongest concrete discovery" in http.calls[0][1]["input"]
    assert "leave a specific how, why or consequence unresolved" in http.calls[0][1]["input"]
    assert "the payoff answers it rather than echoing the hook" in http.calls[0][1]["input"]
    assert "make that achievement the opening promise" not in http.calls[0][1]["input"]
    assert "do not make disproving that label" in http.calls[0][1]["input"]
    assert "end on the concrete discovery" in http.calls[0][1]["input"]
    assert "visual direction, not another writing pass" in http.calls[1][1]["input"]
    shot_prompt = http.calls[1][1]["input"]
    assert "For animated study_design steps and qualitative_comparison alternatives" in shot_prompt
    assert "For other animated rows, entrance_cues remain optional" in shot_prompt
    assert "Cues use estimated word positions" in shot_prompt
    assert http.calls[0][1]["background"] is True
    assert http.calls[0][1]["reasoning"]["effort"] == "medium"
    assert http.calls[1][1]["reasoning"]["effort"] == "low"
    assert http.calls[1][1]["text"]["verbosity"] == "low"
    invalid = json.loads(json.dumps(plan))
    invalid["shots"][1]["rank"], invalid["shots"][2]["rank"] = (
        invalid["shots"][2]["rank"],
        invalid["shots"][1]["rank"],
    )
    repair_http = FakeHTTP(
        [
            response(json.dumps(narrative)),
            response(json.dumps(invalid)),
            response(json.dumps(invalid)),
        ]
    )
    broken = OpenAIDirector(OpenAIClient(Settings(api_key="test"), tmp_path / "bad", repair_http))
    with pytest.raises(ValueError, match="failed validation"):
        asyncio.run(broken.develop(brief, []))
    assert len(repair_http.calls) == 3
    assert repair_http.calls[1][1]["model"] == "gpt-5.6-sol"
    assert repair_http.calls[2][1]["model"] == "gpt-6-luna"


def test_director_reuses_validated_narrative_after_shot_timeout(tmp_path):
    brief = load_brief(Path("examples/earthquake-full.yaml"))
    spec = asyncio.run(MockDirector().develop(brief, []))
    narrative, plan = directing_replies(spec)
    first_http = FakeHTTP(
        [
            response(json.dumps(narrative)),
            RemoteError("OpenAI", kind="timeout"),
        ]
    )
    first = OpenAIDirector(OpenAIClient(Settings(api_key="test"), tmp_path, first_http))

    with pytest.raises(ProviderTimeoutError, match="creation timed out"):
        asyncio.run(first.develop(brief, []))

    assert len(list(tmp_path.glob("*.validated-narrative.json"))) == 1
    second_http = FakeHTTP([response(json.dumps(plan))])
    second = OpenAIDirector(OpenAIClient(Settings(api_key="test"), tmp_path, second_http))
    result = asyncio.run(second.develop(brief, []))

    assert result.narration_script == narrative["narration_script"]
    assert len(second_http.calls) == 1
    assert second.client.trace[0]["checkpoint_hit"] is True


def test_narrative_rejects_fragmented_fact_list():
    brief = load_brief(Path("examples/earthquake-full.yaml"))
    draft = NarrativeDraft(
        candidate_hooks=["Earthquakes changed history.", "Destruction has another measure."],
        hook="Earthquakes changed history.",
        thesis="Human exposure changes the outcome.",
        payoff="Numbers need context.",
        audio_direction="Conversational.",
        narrative_beats=["Open", "Count", "Resolve"],
        narration_script=(
            "Earthquakes changed history. Haiyuan was fifth. Indian Ocean was fourth. "
            "Tangshan was third. Haiti was second. Shaanxi was first."
        ),
        loop_bridge="Numbers alone do not settle the story.",
    )

    with pytest.raises(ValueError, match="clipped or fragment-like"):
        validate_narrative_draft(brief, draft, [])


def test_director_repairs_hook_and_first_frame_divergence(tmp_path, brief):
    spec = asyncio.run(MockDirector().develop(brief, []))
    narrative, plan = directing_replies(spec)
    invalid = json.loads(json.dumps(plan))
    invalid["shots"][0]["title"] = "A competing opening headline"
    http = FakeHTTP(
        [
            response(json.dumps(narrative)),
            response(json.dumps(invalid)),
            response(json.dumps(plan)),
        ]
    )
    director = OpenAIDirector(OpenAIClient(Settings(api_key="test"), tmp_path, http))

    result = asyncio.run(director.develop(brief, []))

    assert result.shots[0].title == result.hook
    assert "first-frame title" in http.calls[2][1]["input"]


def test_shot_plan_normalizes_silence_audio_contradictions(brief):
    spec = asyncio.run(MockDirector().develop(brief, []))
    _, data = directing_replies(spec)
    spoken = next(shot for shot in data["shots"] if shot["voice_segments"])
    spoken["intentional_silence"] = True
    silent = next(shot for shot in data["shots"] if not shot["voice_segments"])
    silent["intentional_silence"] = True
    silent["sound_cues"] = [
        {"shot_id": silent["id"], "cue_type": "transition", "offset": 0, "duration": 0.2}
    ]

    plan = normalize_shot_plan(ShotPlanDraft.model_validate(data))

    assert next(shot for shot in plan.shots if shot.id == spoken["id"]).intentional_silence is False
    assert not next(shot for shot in plan.shots if shot.id == silent["id"]).sound_cues


def test_shot_type_reasoning_is_required_and_nonnumeric_metric_is_rejected():
    metric = MetricShot(
        id="claim", start=0, duration=5, title="Possibility is not proof",
        metric="Not proof",
        shot_type_reasoning=(
            "The narration distinguishes a modeled possibility from historical proof."
        ),
    )
    title = TitleShot(
        id="end", start=5, duration=3, title="An open question",
        shot_type_reasoning="The narrated ending returns to the unresolved question.",
    )
    plan = ShotPlanDraft(shots=[metric, title])
    with pytest.raises(ValueError, match="claim big_metric needs a numerical quantity"):
        validate_shot_type_choices(plan)
    metric.metric = "740 PBq"
    validate_shot_type_choices(plan)
    metric.shot_type_reasoning = ""
    with pytest.raises(ValueError, match="shot_type_reasoning"):
        validate_shot_type_choices(plan)
    assert "shot_type_reasoning" in strict_schema(ShotPlanDraft.model_json_schema())[
        "$defs"
    ]["MetricShot"]["required"]
    assert (
        MetricShot(id="old", start=0, duration=2, title="Old", metric="5 km").shot_type_reasoning
        == ""
    )


def test_shot_kind_normalization_explains_final_choice():
    metric = MetricShot(
        id="pair", start=0, duration=5, title="Two distances",
        metric="6 km • 8 km",
        shot_type_reasoning="The narration compares two distances using a numerical reveal.",
    )
    title = TitleShot(
        id="end", start=5, duration=3, title="Ending",
        shot_type_reasoning="The narration closes with the question viewers should retain.",
    )
    plan = ShotPlanDraft(shots=[metric, title])
    normalize_shot_plan(plan)
    assert plan.shots[0].kind == "metric_pair"
    assert "Normalized from big_metric to metric_pair" in plan.shots[0].shot_type_reasoning
    assert "Planner rationale:" in plan.shots[0].shot_type_reasoning
    validate_shot_type_choices(plan)


def test_director_rejects_conceptual_big_metric_before_story_approval(tmp_path):
    brief = load_brief(Path("examples/earthquake-full.yaml"))
    spec = asyncio.run(MockDirector().develop(brief, []))
    narrative, valid = directing_replies(spec)
    invalid = json.loads(json.dumps(valid))
    payoff = next(shot for shot in invalid["shots"] if shot["id"] == "payoff")
    payoff["kind"] = "big_metric"
    payoff["metric"] = "Not proof"
    payoff["shot_type_reasoning"] = (
        "The narrated conclusion is important enough for a large visual statement."
    )
    http = FakeHTTP(
        [
            response(json.dumps(narrative)),
            response(json.dumps(invalid)),
            response(json.dumps(valid)),
        ]
    )
    director = OpenAIDirector(OpenAIClient(Settings(api_key="test"), tmp_path, http))

    result = asyncio.run(director.develop(brief, []))

    assert next(shot for shot in result.shots if shot.id == "payoff").kind == "outro"
    assert "payoff big_metric needs a numerical quantity" in http.calls[2][1]["input"]
    assert http.calls[2][1]["model"] == "gpt-6-luna"


def test_tts_actual_duration_cache_and_metadata(tmp_path, brief):
    studio = Studio(tmp_path / "projects")
    project = studio.create(brief)
    spec = asyncio.run(MockDirector().develop(brief, []))
    segment = spec.shots[0].voice_segments[0]
    wav = tmp_path / "fixture.wav"
    tone(wav, 1.25, "voice")
    http = FakeHTTP(audio=wav.read_bytes())
    provider = OpenAIVoice(
        OpenAIClient(Settings(api_key="test"), tmp_path / "cache", http), studio.repo
    )
    output = studio.repo.attempt_dir(project, "tts")
    first = asyncio.run(provider.synthesize(project, segment, brief.voice, 0, output / "a.wav"))
    second = asyncio.run(provider.synthesize(project, segment, brief.voice, 0, output / "b.wav"))
    assert first.duration == pytest.approx(1.25)
    assert first.artifact.provider == "openai"
    assert first.artifact.cost is None
    assert first.artifact.sha256 == second.artifact.sha256
    assert len(http.calls) == 1
    traces = list((studio.repo.project_dir(project.id) / "diagnostics" / "tts").glob("*.json"))
    assert len(traces) == 1  # A cache hit does not make or log another network request.
    trace = json.loads(traces[0].read_text(encoding="utf-8"))
    assert trace["result"] == "success"
    assert trace["model"] == "gpt-4o-mini-tts"
    assert trace["client_request_id"].startswith("studio-tts-")
    assert segment.spoken_text not in traces[0].read_text(encoding="utf-8")
    assert "Bearer test" not in traces[0].read_text(encoding="utf-8")


def test_tts_timeout_writes_sanitized_trace_and_keeps_original_error(tmp_path, brief):
    class TimeoutHTTP(FakeHTTP):
        async def post(self, url, body, headers=None, **kwargs):
            raise RemoteError("OpenAI TTS", kind="timeout")

    studio = Studio(tmp_path / "projects")
    project = studio.create(brief)
    segment = asyncio.run(MockDirector().develop(brief, [])).shots[0].voice_segments[0]
    provider = OpenAIVoice(
        OpenAIClient(Settings(api_key="private-key"), tmp_path / "cache", TimeoutHTTP()),
        studio.repo,
    )
    output = studio.repo.attempt_dir(project, "tts") / "voice-shot_1-0.wav"
    with pytest.raises(RemoteError, match="timeout"):
        asyncio.run(provider.synthesize(project, segment, brief.voice, 0, output))
    traces = list((studio.repo.project_dir(project.id) / "diagnostics" / "tts").glob("*.json"))
    assert len(traces) == 1
    trace_text = traces[0].read_text(encoding="utf-8")
    trace = json.loads(trace_text)
    assert trace["result"] == "error"
    assert trace["error_kind"] == "timeout"
    assert trace["segment"] == "voice-shot_1-0"
    assert segment.spoken_text not in trace_text
    assert "private-key" not in trace_text


class FakeTelegram(TelegramAdapter):
    def __init__(self, repo):
        super().__init__(
            TelegramConfig(bot_token="123:" + "a" * 30, chat_id=123, allowed_user_ids=[42]), repo
        )
        self.sent = []
        self.updates = []

    async def call(self, method, payload):
        self.sent.append((method, payload))
        if method == "getUpdates":
            return self.updates
        if method == "getWebhookInfo":
            return {}
        return {"message_id": len(self.sent)}

    async def upload(self, method, path, caption):
        return await self.call(method, {"path": str(path)})


def callback(project, update_id=1, user=42, action="approve"):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user},
            "message": {"chat": {"id": 123}},
            "data": f"{action}:{project.pending().id}",
        },
    }


def test_telegram_auth_replay_and_stale_gate(tmp_path, brief):
    studio = Studio(tmp_path)
    project = asyncio.run(studio.run(studio.create(brief).id))
    adapter = FakeTelegram(studio.repo)
    worker = TelegramWorker(studio, adapter, lambda: Studio(tmp_path))
    asyncio.run(worker.process(callback(project, user=7)))
    assert studio.repo.load(project.id).pending()
    assert not adapter.sent
    update = callback(project, update_id=2)
    asyncio.run(worker.process(update))
    assert studio.repo.load(project.id).state == State.STORYBOARDED
    revision = studio.repo.load(project.id).revision
    asyncio.run(worker.process(update))
    asyncio.run(worker.process(callback(project, update_id=3)))
    assert studio.repo.load(project.id).revision == revision
    assert adapter.sent[-1][1]["show_alert"]


def test_telegram_script_approval_is_acknowledged_immediately(tmp_path, brief):
    studio = Studio(tmp_path)
    project = asyncio.run(studio.run(studio.create(brief).id))
    project.state = State.SCRIPT_APPROVAL
    project.pending().gate = "script"
    studio.repo.save(project, "script_gate_fixture", {})
    adapter = FakeTelegram(studio.repo)
    worker = TelegramWorker(studio, adapter, lambda: Studio(tmp_path))

    asyncio.run(worker.process(callback(project)))

    answers = [payload for method, payload in adapter.sent if method == "answerCallbackQuery"]
    messages = [payload for method, payload in adapter.sent if method == "sendMessage"]
    assert answers[-1]["text"] == "Script approved"
    assert "Preparing visuals, music, audio mix" in messages[-1]["text"]


def test_telegram_private_youtube_upload_is_queued_and_reported(tmp_path, brief):
    studio = Studio(tmp_path)
    project = studio.create(brief)
    project.state = State.COMPLETED
    studio.repo.save(project, "completed_fixture", {})
    adapter = FakeTelegram(studio.repo)

    class FakePublisher:
        def publish(self, project_id):
            assert project_id == project.id

            class Result:
                status = "uploaded"
                video_id = "private-video-1"

            return Result()

    worker = TelegramWorker(studio, adapter, lambda: Studio(tmp_path), youtube=FakePublisher())
    update = {
        "update_id": 10,
        "message": {
            "chat": {"id": 123},
            "from": {"id": 42},
            "text": f"/uploadyoutubeshorts {project.id}",
        },
    }

    async def process_and_wait():
        await worker.process(update)
        await asyncio.gather(*worker.upload_tasks.values())

    asyncio.run(process_and_wait())

    messages = [payload["text"] for method, payload in adapter.sent if method == "sendMessage"]
    assert "Private YouTube upload queued" in messages[0]
    assert "uploaded privately" in messages[-1]


def test_telegram_can_refresh_youtube_token_without_uploading(tmp_path):
    studio = Studio(tmp_path)
    adapter = FakeTelegram(studio.repo)

    class FakePublisher:
        refreshes = 0

        def refresh_authorization(self):
            self.refreshes += 1
            return {"channel_title": "CryoKnowledge", "expires_at_utc": "2030-01-01T00:00:00+00:00"}

        def publish(self, project_id):
            raise AssertionError("token refresh must not upload")

    publisher = FakePublisher()
    worker = TelegramWorker(studio, adapter, lambda: studio, youtube=publisher)
    command = {
        "update_id": 101,
        "message": {"chat": {"id": 123}, "from": {"id": 42}, "text": "/refreshyoutubeauth"},
    }
    asyncio.run(worker.process(command))
    asyncio.run(worker.process(command))
    unauthorized = {
        "update_id": 102,
        "message": {"chat": {"id": 123}, "from": {"id": 7}, "text": "/refreshyoutubeauth"},
    }
    asyncio.run(worker.process(unauthorized))

    messages = [payload["text"] for method, payload in adapter.sent if method == "sendMessage"]
    assert publisher.refreshes == 1
    assert "access token renewed for CryoKnowledge" in messages[-1]
    assert "No video was uploaded" in messages[-1]
    assert len(messages) == 2


def test_telegram_reports_google_refresh_error_after_upload_ack(tmp_path, brief):
    studio = Studio(tmp_path)
    project = studio.create(brief)
    project.state = State.COMPLETED
    studio.repo.save(project, "completed_fixture", {})
    adapter = FakeTelegram(studio.repo)

    class FailingPublisher:
        def publish(self, project_id):
            raise RefreshError("invalid_grant")

    worker = TelegramWorker(studio, adapter, lambda: studio, youtube=FailingPublisher())
    command = {
        "update_id": 103,
        "message": {
            "chat": {"id": 123}, "from": {"id": 42},
            "text": f"/uploadyoutubeshorts {project.id}",
        },
    }

    async def process_and_wait():
        await worker.process(command)
        await asyncio.gather(*worker.upload_tasks.values())

    asyncio.run(process_and_wait())
    messages = [payload["text"] for method, payload in adapter.sent if method == "sendMessage"]
    assert "upload queued" in messages[0]
    assert "could not be refreshed" in messages[-1]
    assert "/refreshyoutubeauth" in messages[-1]
    assert not studio.repo.load(project.id).youtube_uploads


def test_telegram_force_reply_revision_is_idempotent(tmp_path, brief):
    studio = Studio(tmp_path)
    project = asyncio.run(studio.run(studio.create(brief).id))
    adapter = FakeTelegram(studio.repo)
    worker = TelegramWorker(studio, adapter, lambda: Studio(tmp_path))
    asyncio.run(worker.process(callback(project, action="revise")))
    update = {
        "update_id": 2,
        "message": {
            "chat": {"id": 123},
            "from": {"id": 42},
            "reply_to_message": {"message_id": 1},
            "text": "Shorter hook",
        },
    }
    asyncio.run(worker.process(update))
    assert studio.repo.load(project.id).version == 2
    # Simulate a crash after revision commit but before update acknowledgement.
    studio.repo.set_runtime(worker.prefix + ":2", False)
    asyncio.run(worker.process(update))
    assert studio.repo.load(project.id).version == 2
    assert studio.repo.load(project.id).feedback[-1].message == "Shorter hook"


def test_delivery_checkpoint_and_actions_last(tmp_path, brief):
    studio = Studio(tmp_path)
    project = asyncio.run(studio.run(studio.create(brief).id))
    adapter = FakeTelegram(studio.repo)
    message = review_message(project, project.pending())
    asyncio.run(adapter.deliver(message))
    assert "inline_keyboard" in adapter.sent[-1][1]["reply_markup"]
    assert not any(method == "sendDocument" for method, _ in adapter.sent)
    count = len(adapter.sent)
    restarted = FakeTelegram(studio.repo)
    asyncio.run(restarted.deliver(message))
    assert not restarted.sent
    assert len(adapter.sent) == count


def test_telegram_image_budget_persists_for_future_projects(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    adapter = FakeTelegram(studio.repo)
    worker = TelegramWorker(studio, adapter, lambda: studio)

    def command(update_id, text):
        return {
            "update_id": update_id,
            "message": {"chat": {"id": 123}, "from": {"id": 42}, "text": text},
        }

    asyncio.run(worker.process(command(1, "/imagebudget 7")))
    asyncio.run(worker.process(command(2, "/topic A seven-image future project")))
    first = studio.repo.all_projects()[0]
    assert first.auto_assets.max_image_requests == 7

    restarted = TelegramWorker(studio, adapter, lambda: studio)
    asyncio.run(restarted.process(command(3, "/full A second future project")))
    assert studio.repo.all_projects()[1].auto_assets.max_image_requests == 7

    asyncio.run(restarted.process(command(4, "/imagebudget reset")))
    asyncio.run(restarted.process(command(5, "/topic Back to default")))
    assert studio.repo.all_projects()[2].auto_assets.max_image_requests == 2


def test_telegram_closed_caption_preference_is_snapshotted(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test", closed_captions=True))
    adapter = FakeTelegram(studio.repo)
    worker = TelegramWorker(studio, adapter, lambda: studio)

    def command(update_id, text):
        return {
            "update_id": update_id,
            "message": {"chat": {"id": 123}, "from": {"id": 42}, "text": text},
        }

    asyncio.run(worker.process(command(1, "/closedcaption no")))
    asyncio.run(worker.process(command(2, "/topic A project without burned-in captions")))
    first = studio.repo.all_projects()[0]
    assert first.spec.closed_captions is False
    assert first.publication_clearance_enabled is True

    restarted = TelegramWorker(studio, adapter, lambda: studio)
    asyncio.run(restarted.process(command(3, "/closedcaption yes")))
    asyncio.run(restarted.process(command(4, "/full A project with burned-in captions")))
    projects = studio.repo.all_projects()
    assert projects[0].spec.closed_captions is False
    assert projects[1].spec.closed_captions is True


def test_telegram_caption_alias_settings_and_audit(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test", closed_captions=True))
    adapter = FakeTelegram(studio.repo)
    worker = TelegramWorker(studio, adapter, lambda: studio)

    def command(update_id, text):
        return {
            "update_id": update_id,
            "message": {"chat": {"id": 123}, "from": {"id": 42}, "text": text},
        }

    asyncio.run(worker.process(command(1, "/closed-caption no")))
    asyncio.run(worker.process(command(2, "/settings")))
    asyncio.run(worker.process(command(3, "/full A future project")))

    project = studio.repo.all_projects()[0]
    assert project.spec.closed_captions is False
    assert project.creation_settings["closed_captions"] is False
    assert project.creation_settings["closed_captions_source"] == "telegram_saved"
    assert project.creation_settings["youtube_category_id"] == "28"
    assert "model" not in " ".join(project.creation_settings).casefold()
    assert "Captions: no (telegram_saved)" in adapter.sent[-2][1]["text"]
    assert "YouTube category: Science & Technology (28)" in adapter.sent[-2][1]["text"]
    with studio.repo.connect() as db:
        events = db.execute(
            "SELECT key, old_value, new_value, actor, external_id FROM runtime_events"
        ).fetchall()
    assert len(events) == 1
    assert events[0][0] == worker.closed_caption_key
    assert events[0][2] == "false" and events[0][3] == "telegram:42"
    assert events[0][4] == worker.prefix + ":1"


def test_poll_offset_and_topic_replay(tmp_path):
    studio = Studio(tmp_path, settings=Settings())
    adapter = FakeTelegram(studio.repo)
    adapter.updates = [
        {
            "update_id": 51,
            "message": {
                "chat": {"id": 123},
                "from": {"id": 42},
                "text": "/topic The rings of Saturn",
            },
        }
    ]
    worker = TelegramWorker(studio, adapter, lambda: Studio(tmp_path))
    asyncio.run(worker.poll(0))
    assert studio.repo.get_runtime(worker.prefix + ":offset") == 52
    asyncio.run(TelegramWorker(studio, adapter, lambda: Studio(tmp_path)).poll(0))
    assert len(studio.repo.all_projects()) == 1
    assert studio.repo.all_projects()[0].pipeline == "live"


def test_worker_once_runs_one_job_and_delivers_gate(tmp_path, brief):
    studio = Studio(tmp_path)
    project = studio.create(brief, pipeline="live")

    class OfflineStudio(Studio):
        def configure_live(self, project):
            pass

    adapter = FakeTelegram(studio.repo)
    worker = TelegramWorker(studio, adapter, lambda: OfflineStudio(tmp_path))
    asyncio.run(worker.run(once=True))
    assert studio.repo.load(project.id).state == State.STORY_APPROVAL
    assert not worker.tasks
    assert any("reply_markup" in payload for _, payload in adapter.sent)


def test_busy_project_command_does_not_stop_polling(tmp_path, brief):
    studio = Studio(tmp_path)
    project = studio.create(brief)
    adapter = FakeTelegram(studio.repo)
    worker = TelegramWorker(studio, adapter, lambda: Studio(tmp_path))
    with studio.repo.lock(project.id):
        asyncio.run(
            worker.process(
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": 123},
                        "from": {"id": 42},
                        "text": f"/retry {project.id}",
                    },
                }
            )
        )
    assert "busy" in adapter.sent[-1][1]["text"]


def test_telegram_full_command_records_rank_scope_without_api_calls(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test", auto_assets=False))
    adapter = FakeTelegram(studio.repo)
    worker = TelegramWorker(studio, adapter, lambda: studio)
    asyncio.run(
        worker.process(
            {
                "update_id": 8,
                "message": {
                    "chat": {"id": 123},
                    "from": {"id": 42},
                    "text": "/full 5 Five selected events",
                },
            }
        )
    )
    project = studio.repo.all_projects()[0]
    assert project.state == State.CREATED and project.spec.brief.production_mode == "full"
    assert project.spec.brief.duration == 55 and project.spec.brief.expected_rank_count == 5
    assert (project.spec.brief.width, project.spec.brief.height) == (1080, 1920)
    provider_cache = studio.repo.project_dir(project.id) / "provider-cache"
    assert not project.auto_assets and not provider_cache.exists()


@pytest.mark.integration
def test_full_live_contract_with_fake_http_and_real_renderer(tmp_path, brief):
    spec = asyncio.run(MockDirector().develop(brief, []))
    narrative, plan = directing_replies(spec)
    wav = tmp_path / "speech-fixture.wav"
    tone(wav, 0.7, "voice")
    http = FakeHTTP(
        [
            response("Evidence", [s.url for s in brief.research.sources]),
            response(brief.research.model_dump_json()),
            response(json.dumps(narrative)),
            response(json.dumps(plan)),
        ],
        audio=wav.read_bytes(),
    )
    settings = Settings(api_key="test")
    studio = Studio(tmp_path / "projects", settings=settings, live_http=http)
    project = studio.create(brief, pipeline="live")
    asyncio.run(studio.run(project.id))
    studio.decide(project.id, "story", "approve", actor="test-harness")
    asyncio.run(studio.run(project.id))
    studio.decide(project.id, "hero", "approve", actor="test-harness")
    result = asyncio.run(studio.run(project.id))
    assert result.state == State.PROTOTYPE_APPROVAL
    qa = result.current_artifacts("qa")[0]
    metrics = json.loads(studio.repo.verify(result, qa).read_text())
    assert metrics["video"]["has_audio"]
    assert "AI-generated speech" in metrics["speech_review"]
    assert len(result.current_artifacts("prototype")) == 1
