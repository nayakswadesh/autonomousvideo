from pathlib import Path

import pytest
from PIL import Image

from autonomous_studio.domain.motion import MotionPlan
from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.rendering.longform_motion import frame_for_shot, render_motion_silent
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "longform-pilot.yaml"


def _plan(project_id):
    return MotionPlan.model_validate({
        "project_id": project_id, "chapter_id": "ch1", "comparison_name": "production-test",
        "width": 640, "height": 360, "fps": 24,
        "shots": [
            {"id": "photo", "kind": "camera", "intent": "establish", "start": 0,
             "end": 1, "viewer_takeaway": "Show an observed specimen",
             "why_this_template": "An actual image is better than an invented graph",
             "narration_block_ids": ["b1"], "claim_ids": ["c1"], "source_ids": ["s1"],
             "asset_ref": "sample", "zoom_start": 1.0, "zoom_end": 1.05,
             "cues": [{"at_seconds": 0.1, "narration_excerpt": "the specimen",
                       "action": "start_move"}]},
            {"id": "point", "kind": "fact_reveal", "intent": "examine_evidence",
             "start": 1, "end": 2, "viewer_takeaway": "The measured difference matters",
             "why_this_template": "The synthetic archive image is intentionally not used",
             "narration_block_ids": ["b1"], "claim_ids": ["c1"], "source_ids": ["s1"],
             "eyebrow": "THE RESULT", "line_1": "Measured", "line_2": "Difference",
             "cues": [
                 {"at_seconds": 1.1, "narration_excerpt": "measured",
                  "action": "show_line_1", "duration_seconds": 0.3},
                 {"at_seconds": 1.55, "narration_excerpt": "difference",
                  "action": "show_line_2", "duration_seconds": 0.3},
             ]},
        ],
    })


@pytest.mark.integration
def test_production_motion_renders_captions_on_and_off_and_blocks_guided_pilot(tmp_path):
    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE))
    root = studio.repo.project_dir(project.id)
    picture = root / "specimen.png"
    Image.new("RGB", (640, 360), "#456278").save(picture)
    project.longform_media["sample"] = studio.repo.artifact(project, picture, "longform_visual")
    plan = _plan(project.id)
    ffmpeg = FFmpeg()
    captions = root / "captions.srt"
    captions.write_text(
        "1\n00:00:00,100 --> 00:00:00,900\nThe specimen\n\n"
        "2\n00:00:01,100 --> 00:00:01,900\nMeasured difference\n",
        encoding="utf-8",
    )
    for enabled in (False, True):
        output = root / f"motion-{enabled}.mp4"
        render_motion_silent(studio.repo, project, plan, output, ffmpeg=ffmpeg,
                             captions_path=captions if enabled else None)
        info = ffmpeg.inspect(output)
        assert info["decodable"] and (info["width"], info["height"]) == (640, 360)
        assert abs(info["duration"] - 2) < 0.1
    frame = frame_for_shot(plan.shots[1].model_dump(mode="json"), 1.9, (640, 360), {})
    assert frame.size == (640, 360)
    rejected = plan.model_copy(deep=True)
    rejected.comparison_assets = {"sample": {"path": "unused.png", "sha256": "0" * 64,
                                           "provenance": "comparison only"}}
    with pytest.raises(ValueError, match="Comparison-only assets"):
        render_motion_silent(studio.repo, project, rejected, root / "rejected.mp4")


def test_motion_rendered_persists_a_fresh_review_without_rewriting_narration(tmp_path):
    from test_longform_production import FixtureRenderer

    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE))
    renderer = FixtureRenderer()
    studio.longform_renderer = renderer
    import asyncio

    project = asyncio.run(studio.run(project.id))
    studio.decide(project.id, "treatment", "approve", actor="test")
    project = asyncio.run(studio.run(project.id))
    studio.decide(project.id, "script", "approve", actor="test")
    project = asyncio.run(studio.run(project.id))
    studio.decide(project.id, "visual_plan", "approve", actor="test")
    project = asyncio.run(studio.run(project.id))
    prior = project.pending()
    voice_hashes = {key: value.artifact.sha256 for key, value in project.longform_voices.items()}
    media_hashes = {key: value.sha256 for key, value in project.longform_media.items()}
    rebuilt = asyncio.run(studio.rebuild_longform(project.id, actor="test"))
    assert rebuilt.pending().id != prior.id
    assert next(a for a in rebuilt.approvals if a.id == prior.id).decision == "superseded"
    assert len(renderer.rendered) == 2 * len(rebuilt.spec.chapters)
    assert {
        key: value.artifact.sha256 for key, value in rebuilt.longform_voices.items()
    } == voice_hashes
    assert {key: value.sha256 for key, value in rebuilt.longform_media.items()} == media_hashes


@pytest.mark.integration
def test_family_first_semantic_scene_encodes_at_production_resolution(tmp_path):
    from test_semantic_integration import _fixture

    from autonomous_studio.workflow.semantic_integration import apply_semantic_decisions

    source, base, windows, batch = _fixture()
    source["spec"]["chapters"][0]["visual_beats"][0].update(
        design_family="process", design_treatment="working"
    )
    batch.schema_version = 2
    plan, report = apply_semantic_decisions(source, "ch1", base, windows, batch)
    assert report["applied"] == 1
    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE))
    output = studio.repo.project_dir(project.id) / "family-first-proof.mp4"
    render_motion_silent(studio.repo, project, plan, output)
    info = FFmpeg().inspect(output)
    assert info["decodable"] and (info["width"], info["height"]) == (1920, 1080)
    assert abs(info["duration"] - 7) < 0.1
    assert not info["has_audio"]  # Audio is mixed by render_chapter, not the silent adapter.


@pytest.mark.parametrize("layout, count", [("carousel", 8), ("hierarchy", 4)])
def test_long_form_semantic_layouts_render_spoken_progression(layout, count):
    entries = [f"Narrated point {index + 1}" for index in range(count)]
    shot = {
        "id": "sequence", "kind": "narration_sequence", "intent": "connect",
        "start": 0, "end": 12, "viewer_takeaway": "Explain linked narrated ideas",
        "why_this_template": "The spoken order determines the visual sequence",
        "narration_block_ids": ["b1"], "claim_ids": ["c1"], "source_ids": ["s1"],
        "heading": "A developing explanation", "layout": layout, "entries": entries,
        "cues": [
            {"at_seconds": 0.1 + index * 1.2, "narration_excerpt": entries[index],
             "action": "reveal_step", "step_index": index}
            for index in range(count)
        ],
    }
    plan = MotionPlan.model_validate({
        "project_id": "semantic-test", "chapter_id": "ch1", "comparison_name": "layout-test",
        "width": 640, "height": 360, "fps": 24, "shots": [shot],
    })
    planned = plan.shots[0].model_dump(mode="json")
    initial = frame_for_shot(planned, 0.5, (640, 360), {})
    final = frame_for_shot(planned, 11, (640, 360), {})
    assert initial.tobytes() != final.tobytes()
    assert final.getpixel((320, 350)) == (19, 29, 39)
