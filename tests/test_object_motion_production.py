from pathlib import Path

import pytest
from PIL import Image
from pydantic import TypeAdapter

from autonomous_studio.domain.models import Presentation, Shot
from autonomous_studio.rendering.contrast import TEXT_CONTRAST_TARGET
from autonomous_studio.rendering.editorial import scene
from autonomous_studio.rendering.object_motion import (
    motion_sample_progresses,
    reveal,
    timeline_motion_supported,
)
from autonomous_studio.workflow import Studio


def _project(tmp_path, brief, fields, *, captions=False):
    project = Studio(tmp_path).create(brief)
    shot = TypeAdapter(Shot).validate_python({
        "id": "animated", "start": 0, "duration": 6,
        "title": "A clear idea", "subtitle": "One useful conclusion",
        "motion": "draw", **fields,
    })
    project.spec.shots = [shot]
    project.spec.presentation = Presentation()
    project.spec.closed_captions = captions
    return project, shot


@pytest.mark.parametrize("fields", [
    {"kind": "comparison", "labels": ["A", "B"], "values": [6, 8],
     "display_values": ["6 km", "8 km"]},
    {"kind": "data_chart", "labels": ["A", "B"], "values": [78, 22],
     "display_values": ["78 percent", "22 percent"]},
    {"kind": "timeline", "labels": ["1900 — first", "2000 — second"]},
    {"kind": "qualitative_comparison", "alternatives": ["One idea", "Another idea"]},
    {"kind": "study_design", "steps": ["Observe", "Compare"]},
    {"kind": "big_metric", "metric": "3,000 km"},
    {"kind": "metric_pair", "metrics": ["6 km", "8 km"]},
    {"kind": "metric_set", "labels": ["Distance", "Count"],
     "display_values": ["6 km", "8 objects"]},
    {"kind": "map", "latitude": 10, "longitude": 20, "location_label": "Sample site"},
])
def test_production_object_motion_preserves_contrast_and_layout(tmp_path, brief, fields):
    project, shot = _project(tmp_path, brief, fields)
    assert motion_sample_progresses(shot)
    early = scene(project, shot, 0.01)
    settled = scene(project, shot, 0.5)
    assert early.tobytes() != settled.tobytes()
    for progress in (0.05, 0.5, *motion_sample_progresses(shot)):
        records = []
        scene(project, shot, progress, records=records)
        assert records
        assert all(
            record["contrast_estimate"] >= TEXT_CONTRAST_TARGET
            for record in records if "contrast_estimate" in record
        )
        assert all(0 <= box < 541 for record in records for box in
                   (record["box"][0], record["box"][2]))


def test_object_motion_keeps_bright_artwork_and_conditional_backing(tmp_path, brief):
    project, shot = _project(
        tmp_path, brief,
        {"kind": "qualitative_comparison", "alternatives": ["Dissolution", "Erosion"]},
    )
    bright = Path(tmp_path) / "bright.png"
    Image.new("RGB", (540, 960), (220, 220, 220)).save(bright)
    records = []
    scene(project, shot, 0.5, image_path=bright, records=records)
    assert all(record["contrast_estimate"] >= TEXT_CONTRAST_TARGET for record in records)
    # The production renderer uses local backing/halos, not a forced full-width card.
    assert any(record["backing_alpha"] == 0 for record in records)


def test_hold_opt_out_and_caption_setting_survive_object_motion(tmp_path, brief):
    project, shot = _project(
        tmp_path, brief,
        {"kind": "comparison", "labels": ["A", "B"], "values": [6, 8],
         "display_values": ["6 km", "8 km"],
         "voice_segments": [{"shot_id": "animated", "spoken_text": "Two compared depths.",
                             "offset": 0, "estimated_duration": 5}]},
        captions=True,
    )
    with_caption = []
    scene(project, shot, 0.5, records=with_caption)
    assert any(record["text"] == "Two compared depths." for record in with_caption)
    project.spec.closed_captions = False
    without_caption = []
    scene(project, shot, 0.5, records=without_caption)
    assert not any(record["text"] == "Two compared depths." for record in without_caption)
    shot.motion = "hold"
    assert reveal(0, 6, 0, 2, motion=shot.motion) == 1
    assert motion_sample_progresses(shot) == []


def test_mixed_date_and_measurement_timeline_does_not_animate(tmp_path, brief):
    project, shot = _project(
        tmp_path, brief,
        {"kind": "timeline", "labels": ["2014", "3T observations", "2025"]},
    )
    assert not timeline_motion_supported(shot.labels)
    assert motion_sample_progresses(shot) == []
    records = []
    scene(project, shot, 0.05, records=records)
    assert {"2014", "3T observations", "2025"} <= {
        record["text"] for record in records
    }
