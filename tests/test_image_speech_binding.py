"""Image speech context belongs to the approved script, not model-generated copy."""

import json
from pathlib import Path

import pytest

from autonomous_studio.domain.models import LongFormChapter, NarrationBlock, State
from autonomous_studio.providers.longform_visuals import (
    ChapterVisualSelection,
    assemble_visual_selection,
    bind_approved_image_speech,
    chapter_visual_response_schema,
    validate_image_directions,
)
from autonomous_studio.providers.openai import strict_schema, validate_strict_schema
from autonomous_studio.storage.repository import write_json
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief
from autonomous_studio.workflow.longform_visuals import saved_visual_responses


@pytest.mark.parametrize("partitioned", [True, False])
def test_model_schema_requests_image_composition_without_copied_speech(partitioned):
    schema = chapter_visual_response_schema(partitioned=partitioned)
    direction = schema["$defs"]["ImageDirection"]
    assert "spoken_idea" not in direction["properties"]
    assert "spoken_idea" not in direction["required"]
    assert "visual_moment" in direction["required"]
    if partitioned:
        validate_strict_schema(strict_schema(schema))


@pytest.mark.parametrize("kind", ["authentic_image", "generated_image"])
@pytest.mark.parametrize("historical_excerpt", [None, "wrong copied speech עת"])
def test_image_context_is_exact_approved_speech_with_no_180_character_cap(
    kind, historical_excerpt
):
    text = (
        "Observing stars at different evolutionary stages lets astronomers compare their "
        "models with more than our Sun alone. But those stars are comparisons, "
        "not recordings of our Sun’s future."
    )
    assert len(text) > 180
    chapter = LongFormChapter(
        id="chapter-1", order=1, title="Comparing stars", purpose="Explain the comparison",
        question="How can other stars help?", reveal="They test a model", target_duration=20,
        narration_blocks=[NarrationBlock(id="block-1", text=text, estimated_duration=20)],
    )
    direction = {
        "focal_subject": "A field of distant stars",
        "visual_moment": "Stars of different apparent brightness in a telescope view",
        "setting": "Deep space", "framing": "A single wide star field",
        "focal_placement": "Right third", "overlay_safe_region": "left",
    }
    if historical_excerpt is not None:
        direction["spoken_idea"] = historical_excerpt
    request = (
        {"kind": kind, "search_query": "telescope star field",
         "fallback_prompt": "A single star field viewed through a telescope",
         "fallback_direction": direction}
        if kind == "authentic_image" else
        {"kind": kind, "prompt": "A single star field viewed through a telescope",
         "direction": direction}
    )
    beat = {
        "id": "ch1-beat1", "chapter_id": chapter.id,
        "narration_block_ids": ["block-1"],
        "spoken_unit_ids": ["block-1_u1", "block-1_u2", "block-1_u3"],
        "role_unit_groups": [[]], "visual_type": kind, "treatment": "ken_burns",
        "purpose": "Show other stars as comparisons with our Sun",
        "narration_anchor": "Observing stars", "image_request": request,
    }
    payload = {
        "style_direction": "An observational widescreen astronomy documentary.",
        "rationale": "The star field introduces the comparison with other stars.",
        "beats": [beat],
    }
    original = json.dumps(payload, ensure_ascii=False)
    bound = bind_approved_image_speech(chapter, payload)
    selection = ChapterVisualSelection.model_validate(bound)
    draft = assemble_visual_selection(selection)
    asset = draft.assets[0]
    actual = asset.fallback_direction if kind == "authentic_image" else asset.direction
    assert actual.spoken_idea == text
    assert actual.visual_moment == direction["visual_moment"]
    assert json.dumps(payload, ensure_ascii=False) == original
    validate_image_directions(chapter, draft)
    beat["spoken_unit_ids"] = ["unknown-unit"]
    with pytest.raises(ValueError, match="unknown speech"):
        bind_approved_image_speech(chapter, payload)


def test_legacy_shared_image_gets_context_from_only_its_assigned_beats():
    chapter = {
        "narration_blocks": [{"id": "block", "text": "Water warms. It evaporates. Clouds form."}]
    }
    payload = {
        "beats": [
            {"id": "first", "spoken_unit_ids": ["block_u1"]},
            {"id": "second", "spoken_unit_ids": ["block_u2"]},
            {"id": "third", "spoken_unit_ids": ["block_u3"]},
        ],
        "assets": [{
            "kind": "generated_image", "beat_ids": ["first", "second"],
            "direction": {"spoken_idea": "Water becomes ice."},
        }],
    }
    bound = bind_approved_image_speech(chapter, payload)
    assert bound["assets"][0]["direction"]["spoken_idea"] == "Water warms. It evaporates."


def test_corrected_image_context_replays_paid_chapter_and_resets_only_visual_attempt(tmp_path):
    studio = Studio(tmp_path)
    example = Path(__file__).resolve().parents[1] / "examples" / "longform-pilot.yaml"
    project = studio.create(load_brief(example))
    project.spec.chapters = [
        LongFormChapter(
            id=f"chapter-{index}", order=index, title=f"Chapter {index}",
            purpose="Explain one part of the topic", question="What happens?",
            reveal="An explanation", target_duration=project.spec.brief.duration / 2,
        )
        for index in (1, 2)
    ]
    project.state = State.LONGFORM_VISUAL_PLANNING
    project.longform_visual_plan_attempts = 1
    project.last_error_code = "provider_validation"
    project.last_error = "ch1-beat7_image image direction is not anchored in ch1-beat7 speech"
    record = {
        "requested_model": "gpt-6-luna", "chapter_id": "chapter-1",
        "response": {"status": "completed", "output": [{
            "type": "message", "content": [{"type": "output_text", "text": "{}"}],
        }]},
    }
    path = studio.repo.project_dir(project.id) / "longform-visual-plan-attempt-1.json"
    write_json(path, [record])
    artifact = studio.repo.artifact(project, path, "provider_usage")
    project.artifacts.append(artifact)
    studio.repo.save(project, "longform_visual_plan_rejected", {
        "version": project.version, "error": project.last_error,
    })
    with pytest.raises(ValueError, match="incomplete"):
        saved_visual_responses(studio, project)
    saved_visual_responses(studio, project, allow_partial=True)
    resumed = studio.request_retry(project.id)
    assert resumed.longform_visual_plan_attempts == 0
    assert resumed.last_error is None
    assert resumed.version == project.version
    assert resumed.creation_settings["visual_plan_replay_artifact"] == artifact.path
    assert resumed.creation_settings["visual_plan_replay_allow_continuation"] is True
    assert resumed.approvals == project.approvals
