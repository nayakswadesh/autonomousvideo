"""Future long-form creative limits and image directions stay off legacy projects."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from autonomous_studio.config import Settings
from autonomous_studio.domain.longform_policy import (
    CREATIVE_STAGES,
    LongFormCreativePolicy,
    creative_attempts,
)
from autonomous_studio.domain.models import (
    GeneratedMediaCandidate,
    ImageDirection,
    LongFormChapter,
    NarrationBlock,
    State,
    VisualModeAssessment,
)
from autonomous_studio.providers.longform import (
    MockLongFormDirector,
    MockLongFormResearch,
    OpenAILongFormResearch,
)
from autonomous_studio.providers.longform_media import longform_image_payload
from autonomous_studio.providers.longform_visuals import (
    ChapterBeatPartition,
    ChapterSpeechBoundary,
    ChapterVisualSelection,
    DesignChoice,
    GeneratedImageSelection,
    GeneratedRequestDraft,
    OpenAILongFormVisualPlanner,
    SpokenBeatPartition,
    VisualBeatSelection,
    assemble_visual_selection,
    assign_chapter_beat_ids,
    compact_selection_menu,
    selection_treatment_menu,
    validate_beat_partition,
    validate_image_directions,
    validate_partition_completion,
    validate_speech_boundaries,
    validate_spoken_visual_anchors,
    validate_visual_mode_assessments,
)
from autonomous_studio.providers.openai import (
    ProviderValidationError,
    strict_schema,
    validate_strict_schema,
)
from autonomous_studio.rendering import semantic_diagrams
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief
from autonomous_studio.workflow.longform_production import preflight_longform_motion
from autonomous_studio.workflow.longform_visuals import SavedVisualResponseReplay
from autonomous_studio.workflow.motion_planning import _historical_simulation
from autonomous_studio.workflow.semantic_scene_contract import (
    build_scene_contract,
    validate_scene_fidelity,
)
from autonomous_studio.workflow.spoken_units import chapter_units

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "longform-pilot.yaml"


class InvalidClient:
    def __init__(self):
        self.calls = 0
        self.settings = Settings(api_key="test")
        self.longform_creative_policy = LongFormCreativePolicy()

    async def response(self, *_args, **_kwargs):
        self.calls += 1
        return {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "{}"}]}
            ],
        }

    def reject_last(self, _reason):
        pass


def immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("fixture coroutine unexpectedly suspended")


@pytest.mark.parametrize("stage", CREATIVE_STAGES)
def test_zero_creative_repairs_means_one_initial_attempt(stage):
    client = SimpleNamespace(longform_creative_policy=LongFormCreativePolicy())
    assert creative_attempts(client, stage) == 1
    client.longform_creative_policy.repair_limits[stage] = 2
    assert creative_attempts(client, stage) == 3


def test_new_longform_pins_zero_repairs_and_luna_routing_without_changing_shorts(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    longform = studio.create_longform_topic("Why stars shine")
    short = studio.create_topic("Why stars shine", duration=60, production_mode="full")

    assert longform.longform_creative_policy.repair_limits == {
        stage: 0 for stage in CREATIVE_STAGES
    }
    assert studio.repo.load(longform.id).longform_creative_policy == (
        longform.longform_creative_policy
    )
    assert longform.live_models["research_model"] == "gpt-6-luna"
    assert longform.live_models["story_model"] == "gpt-6.1-sol"
    assert longform.live_models["visual_plan_model"] == "gpt-6-luna"
    assert longform.live_models["semantic_layout_model"] == "gpt-6-luna"
    assert short.longform_creative_policy is None
    assert short.live_models["director_model"] == "gpt-6.1-sol"
    assert short.live_models["research_model"] == "gpt-6-luna"

    updated = studio.set_longform_creative_repair_limit(
        longform.id, "visual_chapter", 2, version=longform.version
    )
    assert studio.repo.load(longform.id).longform_creative_policy.repair_limits[
        "visual_chapter"
    ] == 2
    assert updated.longform_creative_policy.repair_limits["script"] == 0
    with pytest.raises(ValueError, match="stale"):
        studio.set_longform_creative_repair_limit(
            longform.id, "script", 1, version=longform.version + 1
        )


def test_invalid_first_draft_cannot_be_retried_unchanged(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_longform_topic("Why stars shine")
    project.last_error = "Long-form visual chapter failed first-draft validation"
    project.last_error_code = "provider_validation"
    studio.repo.save(project, "test_first_draft_failure", {})
    with pytest.raises(ValueError, match="invalid first draft"):
        studio.request_retry(project.id, actor="test")


def test_visual_validator_recovery_replays_saved_response_without_new_request(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_longform_topic("Why stars shine")
    project.state = State.LONGFORM_VISUAL_PLANNING
    project.longform_visual_plan_attempts = 1
    project.last_error = "Long-form visual plan failed first-draft validation: "
    project.last_error_code = "provider_validation"
    response = {
        "status": "completed",
        "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": "{}"}],
        }],
    }
    trace = (
        studio.repo.project_dir(project.id) / "artifacts"
        / "longform-visual-plan-attempt-1-provider-responses.json"
    )
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text(json.dumps([{
        "chapter_id": "chapter-1",
        "requested_model": "gpt-6-luna",
        "response": response,
    }]), encoding="utf-8")
    project.artifacts.append(studio.repo.artifact(project, trace, "provider_usage"))
    studio.repo.save(project, "failed_visual_fixture", {})
    studio.repo.save(project, "longform_visual_plan_rejected", {
        "version": project.version, "attempt": 1,
        "error": "ch2-beat-1 displays labels out of spoken order",
    })

    recovered = studio.request_retry(project.id, actor="test", version=project.version)
    assert recovered.longform_visual_plan_attempts == 0
    assert recovered.last_error is None
    assert recovered.creation_settings["visual_plan_replay_artifact"] == (
        project.artifacts[-1].path
    )
    assert studio.repo.events(project.id)[-1]["kind"] == (
        "visual_plan_saved_response_recovery_requested"
    )
    assert studio.repo.events(project.id)[-1]["payload"]["new_model_requests_allowed"] is False

    client = SimpleNamespace(
        settings=Settings(api_key="test"),
        longform_creative_policy=LongFormCreativePolicy(),
        cache=tmp_path,
    )
    replay = SavedVisualResponseReplay(client, [{
        "chapter_id": "chapter-1", "requested_model": "gpt-6-luna",
        "response": response,
    }])
    prompt = "Plan this chapter.\n" + json.dumps({
        "story": {}, "chapter": {"id": "chapter-1"},
    })
    assert immediate(replay.response("gpt-6-luna", prompt)) == response
    replay.assert_complete()
    with pytest.raises(ValueError, match="exhausted"):
        immediate(replay.response("gpt-6-luna", prompt))


def test_text_slot_recovery_replays_saved_response_without_new_request(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_longform_topic("Why stars shine")
    project.state = State.LONGFORM_VISUAL_PLANNING
    project.longform_visual_plan_attempts = 1
    project.last_error_code = "provider_validation"
    project.last_error = (
        "Long-form visual chapter failed first-draft validation: "
        "designed reveal label exceeds renderer slot; "
        "concept_contrast/juxtaposition supporting_copy 2 is 46 characters"
    )
    response = {
        "status": "completed",
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "{}"},
        ]}],
    }
    trace = (
        studio.repo.project_dir(project.id) / "artifacts"
        / "longform-visual-plan-attempt-1-provider-responses.json"
    )
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text(json.dumps([{
        "chapter_id": "chapter-1", "requested_model": "gpt-6-luna",
        "response": response,
    }]), encoding="utf-8")
    project.artifacts.append(studio.repo.artifact(project, trace, "provider_usage"))
    studio.repo.save(project, "failed_visual_fixture", {})
    studio.repo.save(project, "longform_visual_plan_rejected", {
        "version": project.version, "attempt": 1, "error": project.last_error,
    })

    recovered = studio.request_retry(project.id, actor="test", version=project.version)
    assert recovered.longform_visual_plan_attempts == 0
    assert recovered.last_error is None
    assert recovered.creation_settings["visual_plan_replay_artifact"] == (
        project.artifacts[-1].path
    )
    assert "visual_plan_replay_allow_continuation" not in recovered.creation_settings
    event = studio.repo.events(project.id)[-1]
    assert event["kind"] == "visual_plan_saved_response_recovery_requested"
    assert event["payload"]["new_model_requests_allowed"] is False


def test_visual_validator_recovery_does_not_unlock_unrelated_failure(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_longform_topic("Why stars shine")
    project.state = State.LONGFORM_VISUAL_PLANNING
    project.longform_visual_plan_attempts = 1
    project.last_error = "Long-form visual plan failed first-draft validation: other failure"
    project.last_error_code = "provider_validation"
    studio.repo.save(project, "failed_visual_fixture", {})
    studio.repo.save(project, "longform_visual_plan_rejected", {
        "version": project.version, "attempt": 1, "error": "unrelated invalid treatment",
    })
    with pytest.raises(ValueError, match="invalid first draft"):
        studio.request_retry(project.id, actor="test", version=project.version)


def test_image_role_validator_recovery_reuses_saved_calls_then_continues(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_longform_topic("Why stars shine")
    project.state = State.LONGFORM_VISUAL_PLANNING
    project.longform_visual_plan_attempts = 1
    project.last_error_code = "provider_validation"
    project.last_error = (
        "Long-form visual plan failed first-draft validation: "
        "ch2-beat1 reveals must cover its speech once in order"
    )
    response = {
        "status": "completed",
        "output": [{"type": "message", "content": [{
            "type": "output_text",
            "text": json.dumps({"beats": [{
                "id": "ch2-beat1", "visual_type": "generated_image",
            }]}),
        }]}],
    }
    trace = (
        studio.repo.project_dir(project.id) / "artifacts"
        / "longform-visual-plan-attempt-1-provider-responses.json"
    )
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text(json.dumps([{
        "chapter_id": "chapter-2",
        "requested_model": "gpt-6-luna",
        "response": response,
    }]), encoding="utf-8")
    project.artifacts.append(studio.repo.artifact(project, trace, "provider_usage"))
    studio.repo.save(project, "failed_visual_fixture", {})
    studio.repo.save(project, "longform_visual_plan_rejected", {
        "version": project.version, "attempt": 1,
        "error": project.last_error,
    })

    recovered = studio.request_retry(project.id, actor="test", version=project.version)
    assert recovered.longform_visual_plan_attempts == 0
    assert recovered.creation_settings["visual_plan_replay_artifact"] == (
        project.artifacts[-1].path
    )
    assert recovered.creation_settings["visual_plan_replay_allow_continuation"] is True
    assert studio.repo.events(project.id)[-1]["payload"]["new_model_requests_allowed"] is True

    class FakeLiveClient:
        settings = Settings(api_key="test")
        longform_creative_policy = LongFormCreativePolicy()
        cache = tmp_path

        def __init__(self):
            self.trace = []
            self.rejections = []

        async def response(self, model, prompt, **kwargs):
            self.trace.append({"requested_model": model, "cache_hit": False})
            return {"status": "completed", "output": [{"type": "message", "content": [{
                "type": "output_text", "text": "{}",
            }]}]}

        def reject_last(self, reason):
            self.rejections.append(reason)

    live = FakeLiveClient()
    replay = SavedVisualResponseReplay(live, [{
        "chapter_id": "chapter-2", "requested_model": "gpt-6-luna",
        "response": response,
    }], allow_continuation=True)
    prompt = 'Plan this chapter.\n{"story": {}, "chapter": {"id": "chapter-2"}}'
    assert immediate(replay.response("gpt-6-luna", prompt)) == response
    assert replay.trace[0]["saved_response_replay"] is True
    replay.reject_last("historical")
    assert live.rejections == []
    assert immediate(replay.response("gpt-6-luna", prompt))["status"] == "completed"
    assert replay.trace[1]["cache_hit"] is False
    replay.reject_last("live")
    assert live.rejections == ["live"]
    replay.assert_complete()


def test_removed_relation_check_can_resume_failed_visual_plan_without_new_version(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_longform_topic("Why stars shine")
    project.state = State.LONGFORM_VISUAL_PLANNING
    project.longform_visual_plan_attempts = 1
    project.last_error_code = "provider_validation"
    project.last_error = (
        "Long-form visual plan failed first-draft validation: "
        "same-subject progression changed graphic subject"
    )
    studio.repo.save(project, "failed_visual_fixture", {})

    recovered = studio.request_retry(project.id, actor="test", version=project.version)
    assert recovered.version == project.version
    assert recovered.longform_visual_plan_attempts == 0
    assert recovered.last_error is None
    assert studio.repo.events(project.id)[-1]["kind"] == (
        "visual_plan_validator_recovery_requested"
    )


def test_canonical_beat_ids_can_resume_failed_partition_without_new_version(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_longform_topic("Why stars shine")
    project.state = State.LONGFORM_VISUAL_PLANNING
    project.longform_visual_plan_attempts = 1
    project.last_error_code = "provider_validation"
    project.last_error = (
        "Long-form visual plan failed first-draft validation: "
        "partition reuses an earlier chapter beat ID"
    )
    studio.repo.save(project, "failed_partition_fixture", {})

    recovered = studio.request_retry(project.id, actor="test", version=project.version)
    assert recovered.version == project.version
    assert recovered.longform_visual_plan_attempts == 0
    assert recovered.last_error is None
    assert studio.repo.events(project.id)[-1]["kind"] == (
        "visual_plan_partition_id_recovery_requested"
    )


def test_ignored_copy_can_resume_failed_visual_plan_without_new_version(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_longform_topic("Why stars shine")
    project.state = State.LONGFORM_VISUAL_PLANNING
    project.longform_visual_plan_attempts = 1
    project.last_error_code = "provider_validation"
    project.last_error = (
        "Long-form visual plan failed first-draft validation: "
        "claim_boundary/bounded_statement has no supporting-copy slot; "
        "every supporting_copy entry must be empty"
    )
    studio.repo.save(project, "failed_unused_copy_fixture", {})

    recovered = studio.request_retry(project.id, actor="test", version=project.version)
    assert recovered.version == project.version
    assert recovered.longform_visual_plan_attempts == 0
    assert recovered.last_error is None
    assert studio.repo.events(project.id)[-1]["kind"] == (
        "visual_plan_unused_copy_recovery_requested"
    )


def test_response_choice_alignment_fix_can_resume_semantic_stage(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_longform_topic("Why stars shine")
    project.state = State.LONGFORM_SEMANTIC_PLANNING
    project.last_error_code = "provider_validation"
    project.last_error = (
        "Family-first scene ch2-beat12 failed first-draft validation: "
        "response choice needs one ordered occurrence in approved speech"
    )
    studio.repo.save(project, "failed_semantic_fixture", {})

    recovered = studio.request_retry(project.id, actor="test", version=project.version)
    assert recovered.version == project.version
    assert recovered.last_error is None
    assert studio.repo.events(project.id)[-1]["kind"] == (
        "semantic_response_choice_recovery_requested"
    )


def test_other_semantic_validation_failure_still_requires_review(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_longform_topic("Why stars shine")
    project.state = State.LONGFORM_SEMANTIC_PLANNING
    project.last_error_code = "provider_validation"
    project.last_error = "Family-first scene failed first-draft validation: unrelated issue"
    studio.repo.save(project, "failed_semantic_fixture", {})

    with pytest.raises(ValueError, match="invalid first draft"):
        studio.request_retry(project.id, actor="test", version=project.version)


def test_versioned_limit_edit_preserves_legacy_policy_schema(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_longform_topic("Why stars shine")
    project.longform_creative_policy = LongFormCreativePolicy(
        schema_version=1,
        repair_limits={stage: 0 for stage in CREATIVE_STAGES if stage != "visual_partition"},
    )
    studio.repo.save(project, "test_legacy_policy", {})
    edited = studio.set_longform_creative_repair_limit(
        project.id, "visual_chapter", 1, version=project.version
    )
    assert edited.longform_creative_policy.schema_version == 1
    assert edited.longform_creative_policy.repair_limits["visual_chapter"] == 1
    with pytest.raises(ValueError, match="unavailable"):
        studio.set_longform_creative_repair_limit(
            project.id, "visual_partition", 1, version=project.version
        )


def test_new_longform_research_plan_stops_after_one_invalid_draft(tmp_path):
    client = InvalidClient()
    brief = load_brief(EXAMPLE)

    with pytest.raises(ProviderValidationError, match="first-draft validation"):
        OpenAILongFormResearch(client).plan(brief).send(None)
    assert client.calls == 1


def test_new_speech_partition_uses_luna_once_and_keeps_approved_words():
    brief = load_brief(EXAMPLE)
    plan = immediate(MockLongFormResearch().plan(brief))
    spec = immediate(MockLongFormDirector().develop_treatment(brief, plan, []))
    spec, _ = immediate(MockLongFormDirector().write_script(spec, []))
    chapter = spec.chapters[0]
    unit_ids = [unit["id"] for unit in chapter_units(chapter)]

    class PartitionClient:
        settings = SimpleNamespace(visual_plan_model="gpt-6-luna")
        longform_creative_policy = LongFormCreativePolicy()

        def __init__(self):
            self.models = []
            self.prompts = []

        async def response(self, model, prompt, **_kwargs):
            self.models.append(model)
            self.prompts.append(prompt)
            return {"status": "completed", "output": [{"content": [{
                "type": "output_text", "text": json.dumps({"beats": [{
                    "id": "hook_beat", "spoken_unit_ids": unit_ids,
                    "visual_intent": "Establish the narrated hook with one image moment.",
                }]})
            }]}]}

        def reject_last(self, _reason):
            raise AssertionError("valid partition was rejected")

    client = PartitionClient()
    partition, menus = immediate(
        OpenAILongFormVisualPlanner(client)._partition_chapter(spec, chapter, [], set())
    )
    assert client.models == ["gpt-6-luna"]
    assert '"estimated_seconds"' in client.prompts[0]
    assert '"role_unit_groups"' not in client.prompts[0]
    assert partition.beats[0].spoken_unit_ids == unit_ids
    assert partition.beats[0].id == "ch1-beat1"
    assert menus["ch1-beat1"]["treatments"] == []
    assert menus["ch1-beat1"]["image_allowed"] is True
    assert isinstance(partition, ChapterSpeechBoundary)
    assert "role_unit_groups" not in partition.model_dump()


def test_studio_assigns_distinct_ids_even_when_chapters_repeat_model_ids():
    raw = ChapterSpeechBoundary(beats=[
        {"id": "beat-1", "spoken_unit_ids": ["unit-1"],
         "visual_intent": "Introduce the physical evidence."},
        {"id": "beat-1", "spoken_unit_ids": ["unit-2"],
         "visual_intent": "Explain what the evidence means."},
    ])
    first = assign_chapter_beat_ids(raw, 1)
    second = assign_chapter_beat_ids(raw, 2)
    assert [beat.id for beat in first.beats] == ["ch1-beat1", "ch1-beat2"]
    assert [beat.id for beat in second.beats] == ["ch2-beat1", "ch2-beat2"]
    assert [beat.id for beat in raw.beats] == ["beat-1", "beat-1"]
    assert [beat.spoken_unit_ids for beat in second.beats] == [
        ["unit-1"], ["unit-2"]
    ]


def test_visual_selection_receives_validator_matched_beat_durations():
    brief = load_brief(EXAMPLE)
    plan = immediate(MockLongFormResearch().plan(brief))
    spec = immediate(MockLongFormDirector().develop_treatment(brief, plan, []))
    spec, _ = immediate(MockLongFormDirector().write_script(spec, []))
    chapter = spec.chapters[0]
    unit_ids = [unit["id"] for unit in chapter_units(chapter)]

    class CaptureClient:
        settings = SimpleNamespace(visual_plan_model="gpt-6-luna", director_model="gpt-6-luna")
        longform_creative_policy = LongFormCreativePolicy()

        def __init__(self):
            self.prompts = []

        async def response(self, model, prompt, **_kwargs):
            assert model == "gpt-6-luna"
            self.prompts.append(prompt)
            value = (
                {"beats": [{
                    "id": "hook_beat", "spoken_unit_ids": unit_ids,
                    "visual_intent": "Establish the narrated hook with one image moment.",
                }]}
                if len(self.prompts) == 1 else {}
            )
            return {"status": "completed", "output": [{"content": [{
                "type": "output_text", "text": json.dumps(value)
            }]}]}

        def reject_last(self, _reason):
            pass

    client = CaptureClient()
    with pytest.raises(ProviderValidationError):
        immediate(OpenAILongFormVisualPlanner(client)._plan_by_chapter(
            spec.model_copy(update={"chapters": [chapter]}),
            SimpleNamespace(maximum_generated_requests=30), [],
        ))
    assert len(client.prompts) == 2
    assert '"estimated_beat_timing"' in client.prompts[1]
    assert '"estimated_seconds"' in client.prompts[1]
    assert '"eligible_treatments_by_beat"' in client.prompts[1]
    assert '"design_families"' not in client.prompts[1]
    assert "advisory targets" in client.prompts[1]
    assert '"treatment_contracts"' in client.prompts[1]
    assert '"ch1-beat1"' in client.prompts[1]
    assert "Choose a fitting treatment and group" in client.prompts[1]
    assert "first-class choices" in client.prompts[1]
    assert "even when it has several spoken role groups" in client.prompts[1]
    assert "Do not target an image percentage" in client.prompts[1]
    assert "COMPLETE image shot" in client.prompts[1]
    assert "mode_assessment" in client.prompts[1]


def test_speech_boundary_leaves_reveal_count_to_treatment_selection():
    chapter = {
        "id": "chapter-2", "order": 2,
        "narration_blocks": [{"id": "block-1", "text": (
            "A cloud of gas collapsed. A spinning disk formed. "
            "The young Sun emerged at its center."
        )}],
    }
    units = chapter_units(chapter)
    assert len(units) == 3
    boundary = ChapterSpeechBoundary(beats=[{
        "id": "cloud-beat", "spoken_unit_ids": [unit["id"] for unit in units],
        "visual_intent": "Show the cloud becoming a disk with a central star.",
    }])
    with pytest.raises(ValidationError, match="role_unit_groups"):
        ChapterSpeechBoundary.model_validate({"beats": [{
            **boundary.beats[0].model_dump(),
            "role_unit_groups": [[unit["id"]] for unit in units],
        }]})
    menu = validate_speech_boundaries(chapter, boundary)
    choices = {(item["family"], item["treatment"]): item
               for item in menu["cloud-beat"]["treatments"]}
    assert ("process", "working") in choices
    assert 2 in choices["process", "working"]["allowed_role_counts"]
    # The second call may combine two related sentences into the final reveal.
    beat = SimpleNamespace(
        id="cloud-beat", spoken_unit_ids=boundary.beats[0].spoken_unit_ids,
        role_unit_groups=[[units[0]["id"]], [units[1]["id"], units[2]["id"]]],
        visual_type="comparison", design_family="process", design_treatment="working",
        overlay_text=["Cloud collapses", "Disk and young Sun form"],
    )
    validate_partition_completion(
        chapter, SimpleNamespace(beats=[beat]), boundary, menu,
    )
    beat.role_unit_groups = [[units[0]["id"]], [units[2]["id"]]]
    with pytest.raises(ValueError, match="cover its speech once in order"):
        validate_partition_completion(
            chapter, SimpleNamespace(beats=[beat]), boundary, menu,
        )


def test_image_cues_do_not_require_designed_scene_role_groups():
    chapter = {
        "id": "chapter-2", "order": 2,
        "narration_blocks": [{"id": "block-1", "text": (
            "What powers the Sun's hidden core? One test comes from neutrinos, "
            "tiny particles produced in nuclear reactions."
        )}],
    }
    units = chapter_units(chapter)
    boundary = ChapterSpeechBoundary(beats=[{
        "id": "ch2-beat1", "spoken_unit_ids": [unit["id"] for unit in units],
        "visual_intent": "Show the Sun while introducing its hidden core and neutrinos.",
    }])
    menu = validate_speech_boundaries(chapter, boundary)
    beat = SimpleNamespace(
        id="ch2-beat1", spoken_unit_ids=boundary.beats[0].spoken_unit_ids,
        role_unit_groups=[[], []], visual_type="generated_image",
        design_family=None, design_treatment=None,
        overlay_text=["The Sun's hidden core", "Neutrinos from nuclear reactions"],
    )
    validate_partition_completion(chapter, SimpleNamespace(beats=[beat]), boundary, menu)
    beat.spoken_unit_ids = beat.spoken_unit_ids[:-1]
    with pytest.raises(ValueError, match="changed its speech boundary"):
        validate_partition_completion(chapter, SimpleNamespace(beats=[beat]), boundary, menu)
    beat.spoken_unit_ids = boundary.beats[0].spoken_unit_ids
    beat.visual_type = "comparison"
    beat.design_family, beat.design_treatment = "process", "working"
    with pytest.raises(ValueError, match="cover its speech once in order"):
        validate_partition_completion(chapter, SimpleNamespace(beats=[beat]), boundary, menu)


def test_shared_treatment_contracts_are_not_repeated_per_beat():
    chapter = {
        "id": "chapter-2", "order": 2,
        "narration_blocks": [{"id": "block-1", "text": (
            "The gas cloud collapsed. A disk formed. The star emerged. "
            "Another cloud collapsed. Another disk formed. Another star emerged."
        )}],
    }
    units = chapter_units(chapter)
    boundary = ChapterSpeechBoundary(beats=[{
        "id": f"beat-{index + 1}",
        "spoken_unit_ids": [unit["id"] for unit in units[index:index + 3]],
        "visual_intent": "Show a narrated physical transformation in sequence.",
    } for index in (0, 3)])
    menu = validate_speech_boundaries(chapter, boundary)
    by_beat, contracts = compact_selection_menu(menu)
    assert by_beat["beat-1"]["treatments"]
    assert "process/working" in contracts
    assert "2" in contracts["process/working"]["by_role_count"]
    assert all("depicts" not in item for item in by_beat["beat-1"]["treatments"])
    assert len(json.dumps({"beats": by_beat, "contracts": contracts})) < len(
        json.dumps(selection_treatment_menu(menu))
    )


def test_shared_contract_preserves_count_specific_validator_rules():
    chapter = {
        "id": "chapter-2", "order": 2,
        "narration_blocks": [{"id": "block-1", "text": (
            "The first distance is 10 kilometers. The second is 20 kilometers. "
            "The third reaches 30 kilometers."
        )}],
    }
    units = chapter_units(chapter)
    boundary = ChapterSpeechBoundary(beats=[{
        "id": "scale-beat", "spoken_unit_ids": [unit["id"] for unit in units],
        "visual_intent": "Compare the three spoken distances in their stated units.",
    }])
    by_beat, contracts = compact_selection_menu(
        validate_speech_boundaries(chapter, boundary)
    )
    scale = next(item for item in by_beat["scale-beat"]["treatments"]
                 if item["design_choice"] == "scale_ladder/bars")
    assert 2 in scale["allowed_role_counts"]
    assert 3 in scale["allowed_role_counts"]
    counts = contracts["scale_ladder/bars"]["by_role_count"]
    assert counts["2"]["minimum_spoken_quantities"] == 2
    assert counts["3"]["minimum_spoken_quantities"] == 3


@pytest.mark.parametrize("image_type", ["generated_image", "authentic_image"])
def test_image_label_may_precede_camera_move_anchor_but_not_other_labels(image_type):
    speech = (
        "Evidence arriving today includes solar neutrinos, "
        "tiny particles associated with reactions inside the Sun."
    )
    chapter = LongFormChapter(
        id="chapter-1", order=1, title="Evidence", purpose="Explain the observation",
        question="What arrives?", reveal="Solar neutrinos", target_duration=12,
        spoken_unit_version=2,
        narration_blocks=[NarrationBlock(id="block-1", text=speech, estimated_duration=12)],
    )
    units = [unit["id"] for unit in chapter_units(chapter)]
    beat = SimpleNamespace(
        id="beat-1", chapter_id="chapter-1", narration_block_ids=["block-1"],
        spoken_unit_ids=units, visual_type=image_type,
        narration_anchor="tiny particles associated with reactions inside the Sun",
        overlay_anchors=["includes solar neutrinos"],
        overlay_text=["Solar neutrinos"], screen_title="Solar neutrinos",
        display_layout=None,
    )
    spec = SimpleNamespace(
        chapters=[chapter], narrative=SimpleNamespace(hook=speech),
    )
    draft = SimpleNamespace(beats=[beat])
    validate_spoken_visual_anchors(spec, draft)

    beat.overlay_anchors = [
        "tiny particles associated with reactions inside the Sun",
        "includes solar neutrinos",
    ]
    beat.overlay_text = ["Tiny particles", "Solar neutrinos"]
    with pytest.raises(ValueError, match="labels out of spoken order"):
        validate_spoken_visual_anchors(spec, draft)

    beat.overlay_anchors = ["a phrase not in the approved speech"]
    beat.overlay_text = ["Unsupported label"]
    with pytest.raises(ValueError, match="spoken anchor must occur exactly once"):
        validate_spoken_visual_anchors(spec, draft)


def test_designed_scene_can_enter_on_first_label_before_later_narration_anchor():
    speech = "A visual hook opens. First the observation arrives, then the explanation follows."
    chapter = LongFormChapter(
        id="chapter-1", order=1, title="Evidence", purpose="Explain the observation",
        question="What arrives?", reveal="An explanation", target_duration=12,
        spoken_unit_version=2,
        narration_blocks=[NarrationBlock(id="block-1", text=speech, estimated_duration=12)],
    )
    unit_ids = [unit["id"] for unit in chapter_units(chapter)]
    hook = SimpleNamespace(
        id="hook", chapter_id="chapter-1", narration_block_ids=["block-1"],
        spoken_unit_ids=unit_ids[:1], visual_type="generated_image",
        narration_anchor="A visual hook opens", overlay_anchors=[], overlay_text=[],
        screen_title="Opening", display_layout=None,
    )
    beat = SimpleNamespace(
        id="beat-2", chapter_id="chapter-1", narration_block_ids=["block-1"],
        spoken_unit_ids=unit_ids[1:],
        visual_type="timeline", narration_anchor="the explanation follows",
        overlay_anchors=["the observation arrives"],
        overlay_text=["Observation"], screen_title="What the evidence shows",
        display_layout=None,
    )
    spec = SimpleNamespace(chapters=[chapter], narrative=SimpleNamespace(hook=speech))
    validate_spoken_visual_anchors(spec, SimpleNamespace(beats=[hook, beat]))
    beat.overlay_anchors = ["the explanation follows", "the observation arrives"]
    beat.overlay_text = ["Explanation", "Observation"]
    with pytest.raises(ValueError, match="labels out of spoken order"):
        validate_spoken_visual_anchors(spec, SimpleNamespace(beats=[hook, beat]))


def test_new_policy_clause_units_split_em_dash_without_changing_legacy_ids():
    speech = (
        "Curie made the activity measurable—turning an observed phenomenon "
        "into something that could be compared. The range was 18–20 cars."
    )
    chapter = {
        "id": "chapter_1", "narration_blocks": [{"id": "block_1", "text": speech}],
    }
    legacy = chapter_units(chapter)
    assert len(legacy) == 2
    chapter["spoken_unit_version"] = 2
    revised = chapter_units(chapter)
    assert len(revised) == 3
    assert [unit["id"] for unit in revised] == [
        "block_1_u1", "block_1_u2", "block_1_u3"
    ]
    assert revised[1]["text"].startswith("turning an observed phenomenon")
    assert "18–20" in revised[2]["text"]
    assert revised[-1]["end_word"] == len(speech.replace("—", " ").replace("–", " ").split())


def test_new_policy_marks_structure_for_clause_level_units_before_approval(tmp_path):
    studio = Studio(tmp_path)
    project = studio.create(
        load_brief(EXAMPLE), longform_creative_policy=LongFormCreativePolicy()
    )
    for _ in range(6):
        project = asyncio.run(studio.run(project.id))
        if project.state == State.TREATMENT_APPROVAL:
            break
    assert project.state == State.TREATMENT_APPROVAL
    assert all(chapter.spoken_unit_version == 2 for chapter in project.spec.chapters)
    assert all(
        chapter.spoken_unit_version == 2
        for chapter in studio.repo.load(project.id).spec.chapters
    )
    studio.decide(project.id, "treatment", "approve", actor="test")
    scripted = asyncio.run(studio.run(project.id))
    assert scripted.state == State.SCRIPT_APPROVAL
    assert all(chapter.spoken_unit_version == 2 for chapter in scripted.spec.chapters)


def direction():
    return ImageDirection(
        spoken_idea="the ice glows",
        focal_subject="a faint light in ice",
        visual_moment="a blue pulse appears beside one sensor",
        setting="deep polar ice",
        framing="wide observational view",
        focal_placement="lower right",
        overlay_safe_region="left",
    )


def test_generated_image_direction_is_speech_bound_and_text_free(monkeypatch):
    monkeypatch.setattr(
        "autonomous_studio.providers.longform_visuals.beat_words",
        lambda *_args: "Here the ice glows when a particle passes".split(),
    )
    beat = SimpleNamespace(id="beat1", overlay_text=["Particle passes"])
    asset = GeneratedRequestDraft(
        id="image1",
        beat_ids=["beat1"],
        prompt="A single blue pulse beside a sensor in deep ice",
        direction=direction(),
    )
    chunk = SimpleNamespace(beats=[beat], assets=[asset])
    validate_image_directions(SimpleNamespace(), chunk)
    asset.direction.spoken_idea = "the sea glows"
    with pytest.raises(ValueError, match="not anchored"):
        validate_image_directions(SimpleNamespace(), chunk)
    asset.direction.spoken_idea = "the ice glows"
    asset.prompt = "A sensor with typographic element tiles in the ice"
    with pytest.raises(ValueError, match="text inside generated art"):
        validate_image_directions(SimpleNamespace(), chunk)
    asset.prompt = "A data-informed schematic diagram of the sensor in ice"
    with pytest.raises(ValueError, match="diagram inside generated art"):
        validate_image_directions(SimpleNamespace(), chunk)
    asset.prompt = (
        "An atmospheric, non-schematic moment beside the sensor. "
        "No arrows, pathways, labels, or diagram marks."
    )
    validate_image_directions(SimpleNamespace(), chunk)
    asset.prompt = "A non-schematic scene, but include a chart beside the sensor"
    with pytest.raises(ValueError, match="diagram inside generated art"):
        validate_image_directions(SimpleNamespace(), chunk)


def test_compiled_treatment_menu_excludes_too_few_slots_and_unspoken_document():
    chapter = {
        "id": "chapter_2", "order": 2,
        "narration_blocks": [{
            "id": "block_1",
            "text": "They expected a signal. The instrument showed a weaker result. "
                    "That difference changed the interpretation.",
        }],
    }
    partition = ChapterBeatPartition(beats=[SpokenBeatPartition(
        id="beat_1",
        spoken_unit_ids=["block_1_u1", "block_1_u2", "block_1_u3"],
        role_unit_groups=[["block_1_u1"], ["block_1_u2"], ["block_1_u3"]],
        visual_intent="Show the expectation, observation and resulting inference.",
    )])
    menu = validate_beat_partition(chapter, partition)["beat_1"]
    choices = {(item["family"], item["treatment"]) for item in menu["treatments"]}
    assert ("concept_contrast", "juxtaposition") not in choices
    assert ("document_focus", "extracted_finding") not in choices
    assert ("evidence", "prediction") in choices
    assert menu["image_allowed"]
    invalid = SimpleNamespace(beats=[SimpleNamespace(
        id="beat_1",
        spoken_unit_ids=partition.beats[0].spoken_unit_ids,
        role_unit_groups=partition.beats[0].role_unit_groups,
        visual_type="comparison", design_family="concept_contrast",
        design_treatment="juxtaposition",
    )])
    with pytest.raises(ValueError, match="outside its compiled menu"):
        validate_partition_completion(chapter, invalid, partition, {"beat_1": menu})


def test_partition_rejects_uncovered_speech_but_keeps_multi_role_image_eligible():
    chapter = {
        "id": "chapter_1", "order": 1,
        "narration_blocks": [{"id": "block_1", "text": "First idea. Then another idea."}],
    }
    missing = ChapterBeatPartition(beats=[SpokenBeatPartition(
        id="beat_1", spoken_unit_ids=["block_1_u1"],
        role_unit_groups=[["block_1_u1"]],
        visual_intent="Establish the first concrete visual idea.",
    )])
    with pytest.raises(ValueError, match="cover every spoken unit"):
        validate_beat_partition(chapter, missing)
    partition = ChapterBeatPartition(beats=[SpokenBeatPartition(
        id="beat_1", spoken_unit_ids=["block_1_u1", "block_1_u2"],
        role_unit_groups=[["block_1_u1"], ["block_1_u2"]],
        visual_intent="Show both successive ideas in order.",
    )])
    menu = validate_beat_partition(chapter, partition)["beat_1"]
    assert menu["image_allowed"]
    assert selection_treatment_menu({"beat_1": menu})["beat_1"]["image_allowed"]
    image_beat = SimpleNamespace(
        id="beat_1", spoken_unit_ids=partition.beats[0].spoken_unit_ids,
        role_unit_groups=partition.beats[0].role_unit_groups,
        visual_type="generated_image", overlay_text=["First idea", "Another idea"],
        design_family=None, design_treatment=None,
    )
    validate_partition_completion(
        SimpleNamespace(id="chapter_1"), SimpleNamespace(beats=[image_beat]),
        partition, {"beat_1": menu},
    )
    image_beat.role_unit_groups = [[], []]
    validate_partition_completion(
        SimpleNamespace(id="chapter_1"), SimpleNamespace(beats=[image_beat]),
        partition, {"beat_1": menu},
    )
    image_beat.overlay_text.append("Third label")
    with pytest.raises(ValueError, match="two-cue"):
        validate_partition_completion(
            SimpleNamespace(id="chapter_1"), SimpleNamespace(beats=[image_beat]),
            partition, {"beat_1": menu},
        )


def test_role_function_menu_prefers_claim_limit_over_false_observation_inference():
    chapter = {
        "id": "chapter-2", "order": 2,
        "narration_blocks": [{"id": "block-1", "text": (
            "Measurements associated with multiple steps give scientists evidence "
            "about the energy-producing reactions, rather than simply an observation "
            "that the Sun shines."
        )}],
    }
    units = chapter_units(chapter)
    assert len(units) == 2
    partition = ChapterBeatPartition(beats=[SpokenBeatPartition(
        id="beat-16", spoken_unit_ids=[unit["id"] for unit in units],
        role_unit_groups=[[unit["id"]] for unit in units],
        role_functions=[
            {"function": "claim", "spoken_anchor": "give scientists evidence"},
            {"function": "limit", "spoken_anchor": "rather than simply an observation"},
        ],
        visual_intent="Show what the measurements support and what they do not prove.",
    )])
    menu = validate_beat_partition(chapter, partition)
    choices = {(item["family"], item["treatment"])
               for item in menu["beat-16"]["treatments"]}
    assert ("claim_boundary", "bounded_statement") in choices
    assert ("evidence", "signal_to_inference") not in choices
    sent = selection_treatment_menu(menu)["beat-16"]
    assert sent["spoken_roles"] == [
        {"text": units[0]["text"], "function": "claim",
         "spoken_anchor": "give scientists evidence"},
        {"text": units[1]["text"], "function": "limit",
         "spoken_anchor": "rather than simply an observation"},
    ]


def test_role_function_menu_keeps_genuine_observation_then_inference():
    chapter = {
        "id": "chapter-2", "order": 2,
        "narration_blocks": [{"id": "block-1", "text": (
            "The detector measured a clear pulse, which suggests an unseen source "
            "beneath the ice."
        )}],
    }
    units = chapter_units(chapter)
    assert len(units) == 2
    partition = ChapterBeatPartition(beats=[SpokenBeatPartition(
        id="pulse-beat", spoken_unit_ids=[unit["id"] for unit in units],
        role_unit_groups=[[unit["id"]] for unit in units],
        role_functions=[
            {"function": "observation", "spoken_anchor": "measured a clear pulse"},
            {"function": "inference", "spoken_anchor": "suggests an unseen source"},
        ],
        visual_intent="Connect a recorded signal to its supported interpretation.",
    )])
    menu = validate_beat_partition(chapter, partition)["pulse-beat"]
    choices = {(item["family"], item["treatment"]) for item in menu["treatments"]}
    assert ("evidence", "signal_to_inference") in choices
    assert ("claim_boundary", "bounded_statement") not in choices
    partition.beats[0].role_functions[1].spoken_anchor = "measured a clear pulse"
    with pytest.raises(ValueError, match="role-function anchor is outside"):
        validate_beat_partition(chapter, partition)


@pytest.mark.parametrize("first,second,expected", [
    (
        "The sequence begins with hydrogen nuclei, each a single proton.",
        "Through that sequence, hydrogen is converted into helium and energy is released.",
        True,
    ),
    (
        "Crude oil enters the refinery.",
        "Through refining, the crude oil is converted into transport fuels.",
        True,
    ),
    (
        "A city installed flood-warning systems.",
        "The warnings gave residents more time to evacuate.",
        False,
    ),
    (
        "A city installed flood-warning systems.",
        "That does not transform a dangerous flood into a safe event.",
        False,
    ),
])
def test_material_transformation_can_offer_before_after_without_role_relabeling(
    first, second, expected
):
    chapter = {
        "id": "chapter-2", "order": 2,
        "narration_blocks": [
            {"id": "block-1", "text": first},
            {"id": "block-2", "text": second},
        ],
    }
    units = chapter_units(chapter)
    first_group = [unit["id"] for unit in units if unit["block_id"] == "block-1"]
    second_group = [unit["id"] for unit in units if unit["block_id"] == "block-2"]
    partition = ChapterBeatPartition(beats=[SpokenBeatPartition(
        id="change-beat", spoken_unit_ids=[unit["id"] for unit in units],
        role_unit_groups=[first_group, second_group],
        role_functions=[
            {"function": "system", "spoken_anchor": "sequence begins"}
            if "sequence" in first else
            {"function": "system", "spoken_anchor": "Crude oil enters"}
            if "Crude" in first else
            {"function": "system", "spoken_anchor": "installed flood-warning systems"},
            {"function": "effect", "spoken_anchor": "converted into"}
            if "converted into" in second else
            {"function": "effect", "spoken_anchor": "more time to evacuate"}
            if "more time" in second else
            {"function": "effect", "spoken_anchor": "does not transform"},
        ],
        visual_intent="Explain the spoken system and its result without inventing details.",
    )])
    menu = validate_beat_partition(chapter, partition)["change-beat"]
    options = {
        (item["family"], item["treatment"]): item
        for item in menu["treatments"]
    }
    assert (("before_after", "split") in options) is expected
    assert (("before_after", "wipe") in options) is expected
    if expected:
        assert options["before_after", "split"]["ordered_roles"] == ["before", "after"]
        sent = selection_treatment_menu({"change-beat": menu})["change-beat"]
        split = next(item for item in sent["treatments"]
                     if item["design_choice"] == "before_after/split")
        assert split["role_match_basis"].startswith("Spoken transformation")


def test_partition_role_vocabulary_covers_registered_fixed_treatments():
    from autonomous_studio.providers.longform_visuals import SpokenRoleFunction
    from autonomous_studio.rendering.visual_registry import (
        REGISTRY,
        role_capacity,
        treatment_role_sequence,
    )

    allowed = set(SpokenRoleFunction.model_fields["function"].annotation.__args__)
    for family in REGISTRY.values():
        for treatment in family.treatments:
            minimum, maximum = role_capacity(family.id, treatment.id)
            for count in range(minimum, maximum + 1):
                try:
                    sequence = treatment_role_sequence(family.id, treatment.id, count)
                except ValueError:
                    continue
                assert set(sequence or []) <= allowed
    validate_strict_schema(strict_schema(ChapterBeatPartition.model_json_schema()))


def test_designed_asset_is_derived_not_guessed_as_generated_image():
    beat = VisualBeatSelection(
        id="beat_2", chapter_id="chapter_1", narration_block_ids=["block_1"],
        spoken_unit_ids=["block_1_u1", "block_1_u2"],
        role_unit_groups=[["block_1_u1"], ["block_1_u2"]],
        visual_type="timeline", treatment="progressive_highlight",
        design_choice=DesignChoice.PROCESS__WORKING,
        purpose="Reveal the observed sequence in two steps.",
        narration_anchor="Reveal the observed sequence",
        screen_title="The observed sequence",
        reveal_cues=[
            {"label": "First observation", "spoken_anchor": "first observation"},
            {"label": "Next observation", "spoken_anchor": "next observation"},
        ],
        screen_blueprint={
            "supporting_copy": ["", ""],
            "graphic_intents": [{"mode": "none"}, {"mode": "none"}],
        },
    )
    selection = ChapterVisualSelection(
        style_direction="An understated widescreen documentary palette.",
        rationale="The spoken sequence needs two designed reveals.",
        beats=[beat],
    )
    draft = assemble_visual_selection(selection)
    assert draft.assets[0].kind == "code_rendered"
    assert draft.assets[0].renderer == "timeline"
    assert draft.beats[0].design_family == "process"
    assert draft.beats[0].design_treatment == "working"
    beat.mode_assessment = VisualModeAssessment(
        image_candidate="One still apparatus cannot show both changing measured states.",
        image_fits=False,
        designed_candidate="Process/working shows the two spoken states in order.",
        designed_necessary=True,
        decision_reason="The visible sequence carries a change that a still plus cues cannot show.",
    )
    assert assemble_visual_selection(selection).beats[0].mode_assessment.designed_necessary
    with pytest.raises(ValidationError, match="image tie-break"):
        VisualBeatSelection.model_validate({
            **beat.model_dump(mode="json"),
            "mode_assessment": {
                "image_candidate": "One focal apparatus, a slow push, and two timed labels.",
                "image_fits": True,
                "designed_candidate": "Process/working has two ordered reveal nodes.",
                "designed_necessary": False,
                "decision_reason": "The complete image scene explains both spoken ideas.",
            },
        })
    selection.beats[0].image_request = GeneratedImageSelection(
        prompt="An illustrative apparatus scene with one clear focal subject",
    )
    with pytest.raises(ValueError, match="designed beat cannot request"):
        assemble_visual_selection(selection)
    with pytest.raises(ValidationError, match="design_choice"):
        VisualBeatSelection.model_validate({
            **beat.model_dump(mode="json"),
            "design_choice": "Side-by-side concepts; reveal in speech order",
        })


def test_image_asset_is_bound_by_beat_not_redundant_model_id():
    beat = VisualBeatSelection(
        id="beat_1", chapter_id="chapter_1", narration_block_ids=["block_1"],
        spoken_unit_ids=["block_1_u1"], role_unit_groups=[["block_1_u1"]],
        visual_type="generated_image", treatment="ken_burns",
        purpose="Show the one concrete narrated moment.",
        narration_anchor="one concrete narrated moment",
        image_request=GeneratedImageSelection(
            prompt="A single luminous instrument in a quiet laboratory"
        ),
    )
    selection = ChapterVisualSelection(
        style_direction="A luminous widescreen observational style.",
        rationale="The subject is best understood as one image.",
        beats=[beat],
    )
    draft = assemble_visual_selection(selection)
    assert draft.beats[0].asset_id == "beat_1_image"
    assert draft.assets[0].id == "beat_1_image"
    assert draft.assets[0].beat_ids == ["beat_1"]
    beat.spoken_unit_ids.append("block_1_u2")
    beat.role_unit_groups.append(["block_1_u2"])
    multi_role = assemble_visual_selection(selection)
    assert multi_role.assets[0].kind == "generated_image"
    assert len(multi_role.beats[0].role_unit_groups) == 2
    selection.beats[0].mode_assessment = VisualModeAssessment(**{
        "image_candidate": "One instrument, a slow push, and a timed subject cue.",
        "image_fits": True,
        "designed_candidate": "A code card would only repeat the subject label.",
        "designed_necessary": False,
        "decision_reason": "The complete image scene already carries the spoken idea.",
    })
    # A new live selection must supply the comparison, not a post-hoc rationale.
    validated = ChapterVisualSelection.model_validate(selection.model_dump(mode="json"))
    validate_visual_mode_assessments(
        validated, {"beat_1": {"image_allowed": True}}
    )
    assert assemble_visual_selection(validated).beats[0].mode_assessment.image_fits
    validated.beats[0].mode_assessment = None
    with pytest.raises(ValueError, match="complete image-versus-designed assessment"):
        validate_visual_mode_assessments(
            validated, {"beat_1": {"image_allowed": True}}
        )
    with pytest.raises(ValidationError, match="matching embedded image request"):
        VisualBeatSelection.model_validate(
            beat.model_dump(mode="json", exclude={"image_request"})
        )


def test_selection_reveal_cues_keep_labels_paired_with_spoken_anchors():
    beat = VisualBeatSelection(
        id="beat_1", chapter_id="chapter_1", narration_block_ids=["block_1"],
        spoken_unit_ids=["block_1_u1"], role_unit_groups=[["block_1_u1"]],
        visual_type="generated_image", treatment="ken_burns",
        purpose="Show the one concrete narrated moment.",
        narration_anchor="the narrated moment",
        image_request=GeneratedImageSelection(
            prompt="A single luminous instrument in a quiet laboratory"
        ),
        reveal_cues=[
            {"label": "First idea", "spoken_anchor": "first spoken idea"},
            {"label": "Second idea", "spoken_anchor": "second spoken idea"},
        ],
    )
    selection = ChapterVisualSelection(
        style_direction="A luminous widescreen observational style.",
        rationale="The subject is best understood as one image.",
        beats=[beat],
    )
    draft = assemble_visual_selection(selection)
    assert draft.beats[0].overlay_text == ["First idea", "Second idea"]
    assert draft.beats[0].overlay_anchors == ["first spoken idea", "second spoken idea"]
    schema = ChapterVisualSelection.model_json_schema()
    beat_properties = schema["$defs"]["VisualBeatSelection"]["properties"]
    assert "overlay_text" not in beat_properties
    assert "overlay_anchors" not in beat_properties
    assert "design_family" not in beat_properties
    assert "design_treatment" not in beat_properties
    assert "role_semantics" not in beat_properties
    assert "mode_assessment" in beat_properties
    validate_strict_schema(strict_schema(schema))
    with pytest.raises(ValidationError, match="spoken_anchor"):
        VisualBeatSelection.model_validate({
            **beat.model_dump(mode="json", exclude={"reveal_cues"}),
            "reveal_cues": [{"label": "Unanchored text"}],
        })


def test_visual_selection_uses_fixed_special_copy_slots_in_provider_schema():
    schema = strict_schema(ChapterVisualSelection.model_json_schema())
    validate_strict_schema(schema)
    slots = schema["$defs"]["SpecialCopySelection"]
    assert slots["additionalProperties"] is False
    assert set(slots["required"]) == {"change", "document_label", "chapter_label"}
    assert "propertyNames" not in slots

    from autonomous_studio.providers.longform_visuals import ScreenBlueprintSelection

    blueprint = ScreenBlueprintSelection.model_validate({
        "special_copy": {"change": "Before and after", "chapter_label": None},
    })
    assert blueprint.to_domain().special_copy == {"change": "Before and after"}
    with pytest.raises(ValidationError, match="48 characters"):
        ScreenBlueprintSelection.model_validate({
            "special_copy": {"change": "x" * 49},
        })


def test_visual_selection_menu_puts_all_text_limits_beside_each_allowed_treatment():
    menus = {
        "beat-2": {
            "image_allowed": False,
            "treatments": [
                {"family": "cutaway", "treatment": "focal_inset",
                 "ordered_roles": ["subject", "feature"]},
                {"family": "claim_boundary", "treatment": "bounded_statement",
                 "ordered_roles": ["claim", "limit"]},
                {"family": "synthesis", "treatment": "mosaic",
                 "ordered_roles": ["input", "input", "input", "takeaway"]},
            ],
        },
    }
    options = selection_treatment_menu(menus)["beat-2"]
    assert options["image_allowed"] is False
    cutaway, boundary, synthesis = options["treatments"]
    expected_slots = {
        "design_choice": "cutaway/focal_inset",
        "ordered_roles": ["subject", "feature"],
        "screen_title_max_chars": 66,
        "reveal_label_max_chars": 31,
        "reveal_label_max_chars_by_role": {},
        "supporting_copy_per_reveal": "optional",
        "supporting_copy_max_chars": 42,
        "special_copy_max_chars": {},
        "required_special_copy": [],
        "visual_data_unit_max_chars": 0,
        "visual_data_date_max_chars": 0,
        "requires_measured_text_fit": False,
    }
    assert {key: cutaway[key] for key in expected_slots} == expected_slots
    assert cutaway["depicts"] == "Subject retained beside magnified feature"
    assert cutaway["graphic_cue_indices"] == [1]
    assert cutaway["role_shape"] == "any"
    assert cutaway["allowed_visual_changes"] == []
    assert boundary["design_choice"] == "claim_boundary/bounded_statement"
    assert boundary["reveal_label_max_chars"] == 34
    assert boundary["reveal_label_max_chars_by_position"] == [34, 45]
    assert cutaway["reveal_label_max_chars_by_position"] == []
    assert boundary["supporting_copy_per_reveal"] == "empty"
    assert boundary["supporting_copy_max_chars"] == 0
    contrast_options = selection_treatment_menu({
        "beat-contrast": {"image_allowed": False, "treatments": [
            {"family": "concept_contrast", "treatment": "juxtaposition",
             "ordered_roles": ["first", "second"]},
        ]},
    })["beat-contrast"]["treatments"][0]
    assert contrast_options["supporting_copy_target_chars"] == 45
    assert contrast_options["supporting_copy_max_chars"] == 60
    assert synthesis["design_choice"] == "synthesis/mosaic"
    assert synthesis["ordered_roles"] == ["input", "input", "input", "takeaway"]
    schema = ChapterVisualSelection.model_json_schema()
    assert "maxLength" not in schema["$defs"]["ScreenBlueprintSelection"][
        "properties"
    ]["supporting_copy"]["items"]
    assert schema["$defs"]["VisualBeatSelection"]["properties"][
        "screen_title"
    ]["maxLength"] == 66


def test_visual_selection_menu_exposes_special_data_and_closing_limits():
    menus = {
        "beat-special": {"image_allowed": False, "treatments": [
            {"family": "editorial_frame", "treatment": "chapter_marker",
             "ordered_roles": ["statement"]},
            {"family": "before_after", "treatment": "split",
             "ordered_roles": ["before", "after"]},
            {"family": "document_focus", "treatment": "extracted_finding",
             "ordered_roles": ["excerpt", "finding"]},
            {"family": "quantitative_display", "treatment": "time_trend",
             "ordered_roles": ["point", "point"]},
            {"family": "chronology", "treatment": "event_track",
             "ordered_roles": ["event", "event"]},
            {"family": "process", "treatment": "working",
             "ordered_roles": ["step", "step"]},
        ]},
        "beat-close": {"image_allowed": False, "community_roles": [
            "comment", "subscribe", "like_share"
        ], "treatments": [
            {"family": "editorial_close", "treatment": "community_close",
             "ordered_roles": ["comment", "subscribe", "like_share"]},
        ]},
    }
    menu = selection_treatment_menu(menus)
    by_choice = {
        item["design_choice"]: item
        for item in menu["beat-special"]["treatments"]
    }
    assert by_choice["editorial_frame/chapter_marker"]["special_copy_max_chars"] == {
        "chapter_label": 28
    }
    assert by_choice["editorial_frame/chapter_marker"]["required_special_copy"] == [
        "chapter_label"
    ]
    assert by_choice["before_after/split"]["special_copy_max_chars"] == {"change": 48}
    assert by_choice["document_focus/extracted_finding"]["special_copy_max_chars"] == {
        "document_label": 46
    }
    assert by_choice["quantitative_display/time_trend"]["visual_data_unit_max_chars"] == 20
    assert by_choice["quantitative_display/time_trend"]["visual_data_date_max_chars"] == 20
    assert by_choice["chronology/event_track"]["visual_data_date_max_chars"] == 24
    assert by_choice["process/working"]["requires_measured_text_fit"] is True
    close = menu["beat-close"]["treatments"][0]
    assert close["reveal_label_max_chars_by_role"] == {
        "comment": 100, "subscribe": 48, "like_share": 48
    }


def test_visual_selection_ignores_copy_without_detail_slot_but_keeps_it_in_plan():
    fields = {
        "id": "beat-4", "chapter_id": "chapter-1",
        "narration_block_ids": ["block-1"],
        "spoken_unit_ids": ["unit-1", "unit-2"],
        "role_unit_groups": [["unit-1"], ["unit-2"]],
        "visual_type": "comparison", "treatment": "progressive_highlight",
        "design_choice": "claim_boundary/bounded_statement",
        "purpose": "Bound a supported proposition by its spoken limitation.",
        "screen_title": "What the evidence can tell us",
        "narration_anchor": "the evidence",
        "reveal_cues": [
            {"label": "Supported claim", "spoken_anchor": "supported claim"},
            {"label": "Spoken limit", "spoken_anchor": "spoken limit"},
        ],
        "screen_blueprint": {
            "supporting_copy": ["Unrenderable extra words", ""],
            "graphic_intents": [{"mode": "none"}, {"mode": "none"}],
        },
    }
    fields["screen_blueprint"]["supporting_copy"][0] = "Unused note " * 9
    selection = VisualBeatSelection.model_validate(fields)
    assert selection.screen_blueprint.supporting_copy[0] == "Unused note " * 9
    beat = selection.to_draft().model_dump(mode="json")
    contract = build_scene_contract(beat, "claim_boundary", "bounded_statement")
    scene = contract.assemble(
        {"claim": {"symbol": "box"}, "limit": {"symbol": "box"}},
        beat=beat,
        narration="The evidence supports a supported claim, but there is a spoken limit.",
        topic="Evidence limits", duration=12,
    )
    assert scene["claim"].get("detail", "") == ""
    assert scene["limit"].get("detail", "") == ""
    scene["claim"]["at"] = 1
    scene["limit"]["at"] = 6
    validate_scene_fidelity(
        beat, semantic_diagrams.ClaimBoundaryScene.model_validate(scene)
    )
    fields["screen_blueprint"]["supporting_copy"] = ["", ""]
    fields["design_choice"] = "cutaway/focal_inset"
    fields["screen_blueprint"]["supporting_copy"] = ["x" * 43, ""]
    with pytest.raises(ValidationError, match="maximum is 42"):
        VisualBeatSelection.model_validate(fields)
    fields["screen_blueprint"]["supporting_copy"] = ["", ""]
    fields["reveal_cues"][1]["label"] = "Calcium-aluminium-rich inclusions"
    with pytest.raises(ValidationError, match="designed reveal label exceeds renderer slot"):
        VisualBeatSelection.model_validate(fields)
    fields["reveal_cues"][1]["label"] = "Calcium-rich inclusions"
    assert VisualBeatSelection.model_validate(fields).reveal_cues[1].label == (
        "Calcium-rich inclusions"
    )


def test_overlong_designed_label_uses_one_fitting_alternative_without_new_call():
    fields = {
        "id": "beat-2-7", "chapter_id": "chapter-2",
        "narration_block_ids": ["block-2"],
        "spoken_unit_ids": ["u8", "u9"],
        "role_unit_groups": [["u8"], ["u9"]],
        "visual_type": "comparison", "treatment": "hold",
        "design_choice": "claim_boundary/bounded_statement",
        "purpose": "Show evidence about energy-producing reactions and its spoken limit.",
        "screen_title": "What the measurements show",
        "narration_anchor": "give scientists evidence",
        "reveal_cues": [
            {
                "label": "Evidence about energy-producing reactions",
                "short_label": "Evidence of energy reactions",
                "spoken_anchor": "give scientists evidence about the energy-producing reactions",
            },
            {
                "label": "More than observing sunlight",
                "short_label": "Beyond observed sunlight",
                "spoken_anchor": "rather than simply an observation that the Sun shines",
            },
        ],
        "screen_blueprint": {
            "supporting_copy": ["", ""],
            "graphic_intents": [{"mode": "none"}, {"mode": "none"}],
        },
    }
    beat = VisualBeatSelection.model_validate(fields)
    assert beat.reveal_cues[0].label == "Evidence of energy reactions"
    assert beat.reveal_cues[1].label == "More than observing sunlight"
    assert beat.to_draft().overlay_text == [
        "Evidence of energy reactions", "More than observing sunlight",
    ]
    assert beat.to_draft().overlay_anchors == [
        "give scientists evidence about the energy-producing reactions",
        "rather than simply an observation that the Sun shines",
    ]
    fields["reveal_cues"][0]["short_label"] = "Evidence about energy-producing stars"
    with pytest.raises(ValidationError, match="designed reveal label exceeds renderer slot"):
        VisualBeatSelection.model_validate(fields)
    fields["reveal_cues"][0]["label"] = "No evidence about energy-producing reactions"
    fields["reveal_cues"][0]["short_label"] = "Evidence of energy reactions"
    with pytest.raises(ValidationError, match="designed reveal label exceeds renderer slot"):
        VisualBeatSelection.model_validate(fields)
    fields["reveal_cues"][0]["label"] = "2014 measurements of energy-producing reactions"
    with pytest.raises(ValidationError, match="designed reveal label exceeds renderer slot"):
        VisualBeatSelection.model_validate(fields)
    schema = strict_schema(ChapterVisualSelection.model_json_schema())
    assert "short_label" in schema["$defs"]["RevealCue"]["required"]


def test_visual_selection_derives_fixed_roles_from_chosen_treatment():
    selection = VisualBeatSelection.model_validate({
        "id": "beat-8", "chapter_id": "chapter-1",
        "narration_block_ids": ["block-3"],
        "spoken_unit_ids": ["unit-1", "unit-2", "unit-3", "unit-4"],
        "role_unit_groups": [["unit-1"], ["unit-2"], ["unit-3"], ["unit-4"]],
        "visual_type": "comparison", "treatment": "hold",
        "design_choice": "synthesis/mosaic",
        "purpose": "Combine three spoken inputs into the final takeaway.",
        "screen_title": "How the evidence comes together",
        "narration_anchor": "spoken input one",
        "reveal_cues": [
            {"label": f"Cue {index}", "spoken_anchor": f"spoken input {index}"}
            for index in range(1, 5)
        ],
        "screen_blueprint": {
            "supporting_copy": ["", "", "", ""],
            "graphic_intents": [{"mode": "none"} for _ in range(4)],
        },
    })
    assert selection.to_draft().role_semantics == [
        "input", "input", "input", "takeaway",
    ]
    assert "role_semantics" not in selection.model_dump()
    with pytest.raises(ValidationError, match="role_semantics"):
        VisualBeatSelection.model_validate({
            **selection.model_dump(mode="json"),
            "role_semantics": ["inputs", "inputs", "inputs", "takeaway"],
        })


def test_negative_historical_portrait_instruction_is_not_a_positive_request():
    assert not _historical_simulation(
        "A present-day university exterior, not an invented historical portrait."
    )
    assert _historical_simulation("An invented historical portrait of a scientist")
    assert not _historical_simulation(
        "A contemporary editorial still life of an electrometer beside a mineral "
        "sample, rendered as an illustration rather than an archival photograph."
    )
    assert _historical_simulation(
        "An archival photograph of a scientist rather than an editorial illustration."
    )
    assert _historical_simulation(
        "No embedded text, but create an invented archival photograph of a scientist."
    )
    assert not _historical_simulation(
        "Contemporary editorial illustration of a mineral specimen on a tabletop; "
        "no person, text, labels, or archival-photo styling."
    )
    assert _historical_simulation(
        "No text, create an invented archival photograph of a scientist."
    )


def test_final_image_prompt_includes_film_style_and_focal_composition():
    asset = GeneratedMediaCandidate(
        id="image1", beat_ids=["beat1"], prompt="A light pulse beneath the ice",
        direction=direction(),
    )
    project = SimpleNamespace(
        longform_creative_policy=LongFormCreativePolicy(),
        longform_visual_plan=SimpleNamespace(
            style_direction="Bright cyan editorial science illustration with spacious framing"
        ),
    )
    payload = longform_image_payload(project, asset, Settings(api_key="test"))
    assert "Bright cyan editorial" in payload["prompt"]
    assert "lower right" in payload["prompt"]
    assert "left region visually quiet" in payload["prompt"]
    assert "No embedded text" in payload["prompt"]


def test_longform_stage_usage_is_separated_and_prices_only_known_models(tmp_path):
    card = tmp_path / "rates.json"
    card.write_text(
        json.dumps({"models": {"gpt-6-luna": {
            "input_per_million": 1.0, "output_per_million": 2.0,
        }}}), encoding="utf-8"
    )
    studio = Studio(tmp_path / "projects", settings=Settings(api_key="test", rate_card=str(card)))
    project = studio.create_longform_topic("Why stars shine")
    studio.configure_live(project)
    studio.live_client.trace.append({
        "requested_model": "gpt-6-luna",
        "response": {"usage": {"input_tokens": 1000, "output_tokens": 500}},
        "chapter_id": "chapter_1",
        "cache_hit": False,
    })
    output = studio.repo.attempt_dir(project, "usage-check")
    studio.record_trace(project, output, "longform-research-plan")
    summary = project.current_artifacts("longform_stage_usage")[-1]
    first = json.loads(studio.repo.verify(project, summary).read_text(encoding="utf-8"))
    assert first["requests"][0]["estimated_cost_usd"] == 0.002
    studio.live_client.trace.append({
        "requested_model": "unpriced-model",
        "response": {"usage": {"input_tokens": 10, "output_tokens": 20}},
        "cache_hit": False,
    })
    studio.record_trace(project, output, "longform-research")
    summary = project.current_artifacts("longform_stage_usage")[-1]
    second = json.loads(studio.repo.verify(project, summary).read_text(encoding="utf-8"))
    assert len(second["requests"]) == 1
    assert second["requests"][0]["model"] == "unpriced-model"
    assert second["requests"][0]["estimated_cost_usd"] is None


def test_semantic_renderer_strict_preflight_rejects_unfittable_label():
    canvas = semantic_diagrams._Draw(Image.new("RGB", (960, 540)))
    token = semantic_diagrams._STRICT_TEXT_FIT.set(True)
    try:
        with pytest.raises(ValueError, match="overflows its renderer slot"):
            semantic_diagrams._fit_text(canvas, (0, 0), "W" * 100, 25)
    finally:
        semantic_diagrams._STRICT_TEXT_FIT.reset(token)


def test_semantic_layout_preflight_samples_intermediate_reveal_frames(monkeypatch):
    sampled = []
    monkeypatch.setattr(
        semantic_diagrams,
        "render_frame",
        lambda _scene, now, **_kwargs: sampled.append(now),
    )
    scene = SimpleNamespace(
        duration=6.0,
        model_dump=lambda **_kwargs: {
            "items": [{"at": 2.0, "label": "middle"}, {"at": 4.0}],
        },
    )
    semantic_diagrams.validate_scene_layout(scene)
    assert 0.0 in sampled
    assert 2.02 in sampled
    assert 4.02 in sampled
    assert 5.98 in sampled


def test_new_policy_checks_measured_reveal_gaps_before_visual_asset_acquisition(monkeypatch):
    block = SimpleNamespace(id="block_1")
    chapter = SimpleNamespace(
        id="chapter_1", narration_blocks=[block],
        model_dump=lambda **_kwargs: {"id": "chapter_1", "visual_beats": []},
    )
    project = SimpleNamespace(
        longform_visual_plan=SimpleNamespace(assets=[SimpleNamespace(id="asset_1")]),
        spec=SimpleNamespace(
            chapters=[chapter], brief=SimpleNamespace(width=1920, height=1080, fps=30)
        ),
        longform_word_timings={"block_1": [{"word": "spoken", "start": 0.0}]},
        longform_voices={"block_1": SimpleNamespace(duration=4.0)},
        longform_creative_policy=LongFormCreativePolicy(),
        pipeline="live",
        model_dump=lambda **_kwargs: {"spec": {"chapters": []}},
    )
    from autonomous_studio.workflow import (
        longform_production,
        motion_planning,
        visual_reveal_timing,
    )

    monkeypatch.setattr(
        longform_production, "narration_windows",
        lambda *_args: [(block, project.longform_voices["block_1"], 0.0, 4.0)],
    )
    monkeypatch.setattr(longform_production, "family_first_chapter", lambda *_: True)
    monkeypatch.setattr(motion_planning, "plan_motion_chapter", lambda *_a, **_k: ("base", {}))

    def reject_measured_gap(*_args, **_kwargs):
        raise ValueError("measured inactive middle")

    monkeypatch.setattr(
        visual_reveal_timing, "validate_measured_reveal_tails", reject_measured_gap
    )
    with pytest.raises(ValueError, match="measured inactive middle"):
        preflight_longform_motion(project)
