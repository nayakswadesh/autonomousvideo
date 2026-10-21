from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from PIL import Image, ImageChops
from pydantic import TypeAdapter, ValidationError

from autonomous_studio.rendering.semantic_diagrams import (
    Scene,
    _Draw,
    _wrapped_card_lines,
    render_frame,
    scene_asset_report,
    validate_scene_layout,
)

STUDIES = Path(__file__).resolve().parents[1] / "examples/semantic_extended_studies.json"
ADAPTER = TypeAdapter(list[Scene])


def test_six_additional_families_have_two_distinct_treatments_each():
    scenes = ADAPTER.validate_json(STUDIES.read_text(encoding="utf-8"))
    assert len(scenes) == 12
    counts = Counter(scene.family for scene in scenes)
    assert counts == {
        "causal": 2,
        "before_after": 2,
        "parallel": 2,
        "journey": 2,
        "scale_ladder": 2,
        "synthesis": 2,
    }
    assert all(len({s.treatment for s in scenes if s.family == family}) == 2 for family in counts)
    assert len({scene.topic for scene in scenes}) >= 10


def test_every_scene_renders_motion_and_reports_mixed_asset_provenance():
    scenes = ADAPTER.validate_json(STUDIES.read_text(encoding="utf-8"))
    reports = []
    for scene in scenes:
        start = render_frame(scene, 0)
        later = render_frame(scene, scene.duration * 0.8)
        assert start.size == later.size == (960, 540)
        assert ImageChops.difference(start, later).getbbox(), scene.id
        reports.extend(scene_asset_report(scene))
    assert {asset["kind"] for asset in reports} == {"code", "svg", "generated"}
    assert all(asset["match"] != "generic_fallback" for asset in reports)
    assert any(asset["status"] == "review_only" for asset in reports)


def test_native_1080p_raster_does_not_upscale_preview():
    scenes = ADAPTER.validate_json(STUDIES.read_text(encoding="utf-8"))
    for scene in (scenes[0], scenes[3], scenes[11]):
        image = render_frame(scene, scene.duration * 0.8, size=(1920, 1080))
        assert image.size == (1920, 1080)
        assert image.getbbox() is not None


def test_parallel_converge_wraps_complete_copy_inside_its_cards():
    scenes = ADAPTER.validate_json(STUDIES.read_text(encoding="utf-8"))
    scene = next(
        item for item in scenes
        if item.family == "parallel" and item.treatment == "converge"
    )
    scene.tracks[0].label = "The material was mostly barium."
    scene.tracks[1].label = "Weight did not distinguish barium."
    scene.outcome = "No element was established by that measurement."
    draw = _Draw(Image.new("RGB", (960, 540)))
    for label in (scene.tracks[0].label, scene.tracks[1].label):
        _, lines = _wrapped_card_lines(
            draw, label, 239, size=23, minimum=16, max_lines=3,
        )
        assert len(lines) > 1
        assert " ".join(lines) == label
    validate_scene_layout(scene)
    assert render_frame(scene, scene.duration - 0.1, presentation="production").size == (960, 540)


def test_production_frames_omit_study_metadata_and_preserve_design():
    scene = ADAPTER.validate_json(STUDIES.read_text(encoding="utf-8"))[0]
    study = render_frame(scene, scene.duration * 0.8)
    production = render_frame(scene, scene.duration * 0.8, presentation="production")
    assert ImageChops.difference(study, production).getbbox()
    assert ImageChops.difference(
        study.crop((40, 500, 900, 535)), production.crop((40, 500, 900, 535))
    ).getbbox()


def test_invalid_cue_order_and_unbound_visuals_fail_validation():
    scenes = ADAPTER.validate_json(STUDIES.read_text(encoding="utf-8"))
    causal = scenes[0].model_dump()
    causal["stages"][1]["anchor"] = "a phrase absent from the narration"
    with pytest.raises(ValidationError, match="bind to narration"):
        ADAPTER.validate_python([causal])
    scale = next(s for s in scenes if s.family == "scale_ladder").model_dump()
    scale["items"][1]["value"] = 0.5
    with pytest.raises(ValidationError, match="increase"):
        ADAPTER.validate_python([scale])


def test_schematic_journey_and_route_comparison_cannot_claim_other_geometry():
    scenes = ADAPTER.validate_json(STUDIES.read_text(encoding="utf-8"))
    journey = next(s for s in scenes if s.family == "journey").model_dump()
    journey["geometry"] = "surveyed_map"
    with pytest.raises(ValidationError):
        ADAPTER.validate_python([journey])
    before_after = next(s for s in scenes if s.family == "before_after").model_dump()
    before_after["treatment"] = "wipe"
    with pytest.raises(ValidationError, match="route comparison"):
        ADAPTER.validate_python([before_after])
