from types import SimpleNamespace

import pytest
from PIL import Image

from autonomous_studio.domain.models import (
    ChartShot,
    ComparisonShot,
    MapShot,
    MetricPairShot,
    MetricSetShot,
    MetricShot,
    QualitativeComparisonShot,
    StudyDesignShot,
    TimelineShot,
)
from tools.render_short_motion_comparison import (
    GOLD,
    IVORY,
    Painter,
    camera_frame,
    contrast,
    display_value,
    panel_opacity,
)
from tools.render_short_object_motion_comparison import (
    object_progress,
    validate_pilot_shot,
)
from tools.render_short_object_motion_comparison import (
    render_frame as object_motion_frame,
)


def test_bright_background_gets_stronger_local_text_backing():
    dark = Image.new("RGB", (540, 960), (20, 25, 31))
    light = Image.new("RGB", (540, 960), (220, 220, 220))
    rect = (42, 200, 498, 330)
    dark_alpha, dark_ratio = panel_opacity(dark, rect, IVORY)
    light_alpha, light_ratio = panel_opacity(light, rect, IVORY)
    assert dark_alpha == 0
    assert dark_alpha < light_alpha
    assert dark_ratio >= 4.5
    assert light_ratio >= 4.5
    assert contrast((247, 245, 240), (220, 220, 220)) < 4.5


def test_pilot_keeps_card_units_without_duplicate_label():
    assert display_value("Large galaxies", "≈100,000 large galaxies") == "≈100,000"
    assert display_value("Mass", "≈100 quadrillion solar masses") == (
        "≈100 quadrillion solar masses"
    )


def test_camera_move_and_staged_text_are_deterministic():
    source = Image.new("RGB", (864, 1536), (205, 210, 218))
    config = {
        "focus_start": [0.45, 0.45], "focus_end": [0.55, 0.55],
        "zoom_start": 1.01, "zoom_end": 1.12,
    }
    image = camera_frame(source, config, 0.5)
    assert image.size == (540, 960)
    assert image.tobytes() == camera_frame(source, {**config, "brightness": 2.0}, 0.5).tobytes()
    report = []
    painter = Painter(image, 0.1, report)
    painter.text("REVEAL", (42, 200, 498, 330), 50, color=GOLD, at=0.5)
    assert not report
    painter = Painter(image, 0.7, report)
    painter.text("REVEAL", (42, 200, 498, 330), 50, color=GOLD, at=0.5)
    assert report[0]["contrast_estimate"] >= 4.5


def test_grouped_caption_backing_stays_readable_on_bright_image():
    image = Image.new("RGB", (540, 960), (230, 220, 206))
    report = []
    painter = Painter(image, 1.0, report)
    _, ratio = painter.box((35, 779, 505, 859), minimum=190)
    painter.text("A caption over bright artwork", (42, 787, 498, 851), 22, panel=False)
    assert ratio >= 4.5
    assert report[-1]["contrast_estimate"] >= 4.5


def test_object_motion_stages_real_comparison_values_without_changing_shot():
    shot = ComparisonShot(
        id="comparison", kind="comparison", start=0, duration=8,
        title="Two depths", labels=["Shallow", "Deep"], values=[6.9, 8.3],
        display_values=["6.9 km", "8.3 km"],
    )
    project = SimpleNamespace(spec=SimpleNamespace(closed_captions=False))
    look = SimpleNamespace(eyebrow="DEPTH", headline="Two depths")
    base = Image.new("RGB", (540, 960), (13, 20, 28))
    first = object_motion_frame(project, shot, look, base, 0.0)
    last = object_motion_frame(project, shot, look, base, 6.0)
    assert first.size == last.size == (540, 960)
    assert first.tobytes() != last.tobytes()
    assert [item for item in shot.display_values] == ["6.9 km", "8.3 km"]
    assert object_progress(0, 0, 8) == 0
    assert object_progress(6, 1, 8) == 1


def test_timeline_pilot_rejects_mixed_measurements_as_events():
    shot = TimelineShot(
        id="mixed", kind="timeline", start=0, duration=8, title="Mixed",
        labels=["2014", "3T observations", "2025"],
    )
    with pytest.raises(ValueError, match="not chronology"):
        validate_pilot_shot(shot)
    shot.labels = ["2014 — survey began", "2025 — survey ended"]
    validate_pilot_shot(shot)


def test_comparison_pilot_requires_readable_values():
    shot = ComparisonShot(
        id="unlabeled", start=0, duration=8, title="Two dimensions",
        labels=["Across", "Thick"], values=[100000, 1000],
    )
    with pytest.raises(ValueError, match="visible values"):
        validate_pilot_shot(shot)


@pytest.mark.parametrize("shot", [
    ChartShot(id="chart", start=0, duration=8, title="Chart",
              values=[78, 22], labels=["Excluded", "Not excluded"],
              display_values=["78 percent", "22 percent"]),
    TimelineShot(id="timeline", start=0, duration=8, title="Events",
                 labels=["2014 — began", "2025 — ended"]),
    QualitativeComparisonShot(id="ideas", start=0, duration=8, title="Ideas",
                              alternatives=["Dissolution", "Erosion"]),
    StudyDesignShot(id="study", start=0, duration=8, title="Study",
                    steps=["Survey", "Compare"]),
    MetricShot(id="metric", start=0, duration=8, title="Value", metric="3,000 km"),
    MetricPairShot(id="pair", start=0, duration=8, title="Pair",
                   metrics=["100 km", "200 km"]),
    MetricSetShot(id="set", start=0, duration=8, title="Set",
                  labels=["Count", "Mass"], display_values=["100", "20 kg"]),
    MapShot(id="map", start=0, duration=8, title="Place", latitude=40,
            longitude=10, location_label="Sample place"),
])
def test_object_motion_supports_existing_semantic_shots(shot):
    project = SimpleNamespace(spec=SimpleNamespace(closed_captions=False))
    look = SimpleNamespace(eyebrow="TEST", headline=shot.title)
    base = Image.new("RGB", (540, 960), (13, 20, 28))
    early = object_motion_frame(project, shot, look, base, 0.0)
    late = object_motion_frame(project, shot, look, base, 5.0)
    assert early.size == late.size == (540, 960)
    assert early.tobytes() != late.tobytes()
