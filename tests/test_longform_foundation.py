import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from autonomous_studio.cli import app
from autonomous_studio.config import Settings
from autonomous_studio.domain.formats import get_format_profile
from autonomous_studio.domain.models import (
    LongFormBrief,
    LongFormChapter,
    LongFormSpec,
    NarrationBlock,
    State,
    VideoProject,
    VisualBeat,
)
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "longform-pilot.yaml"


def longform_brief() -> LongFormBrief:
    brief = load_brief(EXAMPLE)
    assert isinstance(brief, LongFormBrief)
    return brief


def test_longform_profile_and_brief_constraints():
    profile = get_format_profile("longform_pilot")
    assert profile.chaptered
    assert profile.supported_canvases == ((1920, 1080),)

    brief = longform_brief()
    assert brief.duration == 210
    assert brief.duration_policy.expansion_maximum_seconds == 600
    assert (brief.width, brief.height, brief.aspect_ratio) == (1920, 1080, "16:9")

    invalid = brief.model_dump(mode="json")
    invalid["duration_policy"]["maximum_seconds"] = 1201
    invalid["duration_policy"]["expansion_maximum_seconds"] = 1201
    with pytest.raises(ValidationError, match="longform_pilot duration"):
        LongFormBrief.model_validate(invalid)

    invalid = brief.model_dump(mode="json")
    invalid["width"] = 1280
    with pytest.raises(ValidationError):
        LongFormBrief.model_validate(invalid)


def test_new_longform_topic_starts_at_210_with_600_second_ceiling(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_longform_topic("How a telescope works")
    policy = project.spec.brief.duration_policy
    assert policy.target_seconds == 210
    assert policy.maximum_seconds == 240
    assert policy.expansion_maximum_seconds == 600
    assert project.spec.brief.engagement_ending.like_prompt
    assert project.spec.brief.engagement_ending.share_prompt


def test_existing_longform_ceiling_upgrade_resumes_only_matching_failure(tmp_path):
    studio = Studio(tmp_path)
    brief = longform_brief()
    brief.duration_policy.expansion_maximum_seconds = 300
    project = studio.create(brief)
    project.state = State.LONGFORM_SCRIPTING
    project.last_error_code = "provider_validation"
    project.last_error = (
        "Long-form script failed validation after bounded internal repairs: "
        "Estimated speech is 340.0s and requires approximately 357.9s, "
        "beyond the configured 300.0s production ceiling."
    )
    studio.repo.save(project, "ceiling_failure_fixture", {})

    updated = studio.adopt_longform_runtime_ceiling(project.id, actor="test")
    assert updated.spec.brief.duration_policy.target_seconds == 210
    assert updated.spec.brief.duration_policy.expansion_maximum_seconds == 600
    assert updated.state == State.LONGFORM_SCRIPTING
    assert updated.last_error is None
    assert updated.last_error_code is None
    assert studio.repo.events(project.id)[-1]["payload"]["resumed_ceiling_failure"] is True

    restarted = Studio(tmp_path).repo.load(project.id)
    assert restarted.spec.brief.duration_policy.expansion_maximum_seconds == 600
    assert restarted.last_error is None


def test_longform_chapters_and_visual_beats_validate_evidence_and_timeline():
    brief = longform_brief()
    chapters = []
    for order in range(1, 4):
        chapter_id = f"chapter_{order}"
        block_id = f"narration_{order}"
        chapters.append(
            LongFormChapter(
                id=chapter_id,
                order=order,
                title=f"Chapter {order}",
                purpose="Advance one part of the explanation.",
                question="What does this evidence tell us?",
                reveal="The observation narrows the plausible explanations.",
                transition="That creates the next question.",
                target_duration=70,
                evidence_claim_ids=["c1"],
                evidence_source_ids=["nasa_moon_formation"],
                narration_blocks=[
                    NarrationBlock(
                        id=block_id,
                        text="A coherent paragraph of evidence-led narration.",
                        estimated_duration=12,
                        claim_ids=["c1"],
                    )
                ],
                visual_beats=[
                    VisualBeat(
                        id=f"visual_{order}",
                        chapter_id=chapter_id,
                        start=0,
                        duration=70,
                        narration_block_ids=[block_id],
                        visual_type="data_chart",
                        treatment="progressive_highlight",
                        claim_ids=["c1"],
                        source_ids=["nasa_moon_formation"],
                    )
                ],
            )
        )
    spec = LongFormSpec(brief=brief, chapters=chapters)
    assert sum(chapter.target_duration for chapter in spec.chapters) == brief.duration

    broken = spec.model_dump(mode="json")
    broken["chapters"][0]["visual_beats"][0]["claim_ids"] = ["missing"]
    with pytest.raises(ValidationError, match="missing research evidence"):
        LongFormSpec.model_validate(broken)

    broken = spec.model_dump(mode="json")
    broken["chapters"][2]["target_duration"] = 60
    broken["chapters"][2]["visual_beats"][0]["duration"] = 60
    with pytest.raises(ValidationError, match="fill the long-form target"):
        LongFormSpec.model_validate(broken)


def test_longform_project_persists_foundation_and_stops_at_treatment_gate(tmp_path):
    studio = Studio(tmp_path)
    created = studio.create(longform_brief())
    assert created.spec.format == "youtube_longform"
    assert created.spec.closed_captions is False
    assert created.creation_settings["format_profile"] == "longform_pilot"
    assert created.state == State.CREATED

    ready = asyncio.run(studio.run(created.id))
    assert ready.state == State.TREATMENT_APPROVAL
    assert ready.pending().gate == "treatment"
    assert ready.longform_research_plan is not None
    revision = ready.revision

    restarted = Studio(tmp_path)
    loaded = restarted.repo.load(created.id)
    assert isinstance(loaded.spec, LongFormSpec)
    assert loaded.spec.brief.duration == 210
    assert asyncio.run(restarted.run(created.id)).revision == revision
    states = [event["payload"].get("to") for event in restarted.repo.events(created.id)]
    assert states.count("LONGFORM_FOUNDATION_READY") == 1
    assert states.count("TREATMENT_APPROVAL") == 1


def test_cli_creates_and_reports_longform_profile(tmp_path):
    runner = CliRunner()
    prefix = ["--root", str(tmp_path)]
    created = runner.invoke(app, [*prefix, "create", str(EXAMPLE)])
    assert created.exit_code == 0, created.output
    payload = json.loads(created.stdout)
    assert payload["format"] == "youtube_longform"
    assert payload["profile"] == "longform_pilot"
    assert payload["duration"] == 210
    assert payload["closed_captions"] is False

    resumed = runner.invoke(app, [*prefix, "run", payload["id"]])
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.stdout)["state"] == "TREATMENT_APPROVAL"


def test_cli_longform_topic_command_creates_without_provider_calls(tmp_path):
    result = CliRunner().invoke(
        app,
        ["--root", str(tmp_path), "longform", "Why oceans shape climate"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["format"] == "youtube_longform"
    assert payload["state"] == "CREATED"
    assert payload["duration"] == 210
    assert payload["closed_captions"] is False
    assert not (tmp_path / payload["id"] / "provider-cache").exists()


def test_projects_written_before_spec_discriminator_still_load(brief):
    project = VideoProject(id="0123456789ab", spec={"brief": brief.model_dump(mode="json")})
    stored = deepcopy(project.model_dump(mode="json"))
    stored["spec"].pop("format")
    migrated = VideoProject.model_validate(stored)
    assert migrated.spec.format == "vertical_short"
