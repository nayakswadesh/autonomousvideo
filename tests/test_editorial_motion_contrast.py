from types import SimpleNamespace

from PIL import Image
from pydantic import TypeAdapter

from autonomous_studio.domain.models import Presentation, Shot, ShotLook
from autonomous_studio.rendering.contrast import local_backing
from autonomous_studio.rendering.editorial import _camera_artwork, metric_card_value, scene


def _project(shot):
    style = SimpleNamespace(background="#101923", foreground="#f0efe8", accent="#eab878")
    brief = SimpleNamespace(width=540, height=960, duration=shot.duration, visual_style=style)
    look = ShotLook(shot_id=shot.id, eyebrow="Method", headline="Reconstructing motion")
    spec = SimpleNamespace(brief=brief, shots=[shot], presentation=Presentation(shots=[look]),
                           closed_captions=False)
    return SimpleNamespace(spec=spec, prepared_voices=[])


def _study():
    return TypeAdapter(Shot).validate_python({
        "kind": "study_design", "id": "method", "start": 0, "duration": 9,
        "motion": "draw", "title": "Reconstructing motion",
        "steps": ["galaxy flows were reconstructed", "distances and velocities supplied data"],
    })


def test_local_contrast_only_adds_backing_when_needed():
    dark = Image.new("RGB", (540, 960), (22, 27, 32))
    pale = Image.new("RGB", (540, 960), (225, 225, 220))
    rect = (42, 210, 498, 330)
    assert local_backing(dark, rect, "#f0efe8")[0] == 0
    alpha, ratio, _ = local_backing(pale, rect, "#f0efe8")
    assert alpha > 0 and ratio >= 4.5


def test_study_design_preserves_bullets_order_and_needs_no_dark_artwork_cards(tmp_path):
    path = tmp_path / "dark.png"
    Image.new("RGB", (540, 960), (22, 27, 32)).save(path)
    shot = _study()
    project = _project(shot)
    early = scene(project, shot, 0.2, path)
    late_records = []
    late = scene(project, shot, 0.85, path, records=late_records)
    assert early.getpixel((48, 424)) != early.getpixel((48, 554))
    assert late.getpixel((48, 554)) != early.getpixel((48, 554))
    steps = [item for item in late_records if item["text"] in shot.steps]
    assert [item["text"] for item in steps] == shot.steps
    assert all(item["backing_alpha"] == 0 for item in steps)
    assert all(item["contrast_estimate"] >= 4.5 for item in steps)


def test_bright_artwork_gets_local_title_backing_without_exposure_change(tmp_path):
    path = tmp_path / "bright.png"
    Image.new("RGB", (540, 960), (240, 230, 216)).save(path)
    shot = _study()
    records = []
    scene(_project(shot), shot, 0.85, path, records=records)
    heading = next(item for item in records if item["text"] == "RECONSTRUCTING MOTION")
    assert heading["backing_alpha"] == 0
    assert heading["outline_alpha"] > 0
    assert heading["contrast_estimate"] >= 4.5


def test_camera_motion_changes_crop_but_not_source_image():
    source = Image.linear_gradient("L").convert("RGB").resize((540, 960))
    original = source.tobytes()
    start = _camera_artwork(source, (540, 960), "parallax", 0.0)
    end = _camera_artwork(source, (540, 960), "parallax", 1.0)
    assert start.tobytes() != end.tobytes()
    assert source.tobytes() == original


def test_metric_card_does_not_repeat_its_category():
    assert metric_card_value("large galaxies", "≈100,000 large galaxies") == "≈100,000"
    assert metric_card_value("mass", "≈100 quadrillion solar masses") == (
        "≈100 quadrillion solar masses"
    )
