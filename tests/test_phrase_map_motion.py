from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from autonomous_studio.domain.models import MapShot, Presentation, StudyDesignShot, VoiceSegment
from autonomous_studio.rendering.editorial import scene
from autonomous_studio.rendering.map_motion import camera, geography_for
from autonomous_studio.rendering.map_motion import project as geo_project
from autonomous_studio.rendering.object_motion import cue_onsets, motion_sample_progresses, reveal
from autonomous_studio.workflow import Studio


def test_phrase_cues_follow_measured_speech_and_reject_missing(tmp_path, brief):
    project = Studio(tmp_path).create(brief)
    shot = StudyDesignShot(
        id="study", start=0, duration=10, title="Evidence", steps=["Letters", "Newspapers"],
        entrance_cues=["letters", "newspapers"],
        voice_segments=[VoiceSegment(
            shot_id="study", offset=0.5, estimated_duration=7,
            spoken_text="First the letters arrived, then newspapers followed.",
        )],
    )
    project.spec.shots = [shot]
    project.spec.presentation = Presentation()
    estimate = cue_onsets(project, shot)
    assert estimate[0] < estimate[1]
    assert reveal(estimate[1] - 0.1, 10, 1, 2, motion="draw", onset=estimate[1]) == 0
    assert motion_sample_progresses(shot, project)
    project.prepared_voices = [SimpleNamespace(
        shot_id="study", segment_index=0, asset=SimpleNamespace(duration=9)
    )]
    measured = cue_onsets(project, shot)
    assert measured[1] > estimate[1]
    project.spec.closed_captions = False
    before = scene(project, shot, (measured[1] - 0.1) / 10)
    after = scene(project, shot, (measured[1] + 0.6) / 10)
    assert before.tobytes() != after.tobytes()
    shot.entrance_cues = ["letters", "missing phrase"]
    with pytest.raises(ValueError, match="missing or out of order"):
        cue_onsets(project, shot)


@pytest.mark.parametrize("geometry", [
    {"kind": "point", "latitude": 35, "longitude": 140, "label": "Japan"},
    {"kind": "region", "west": -168, "south": 15, "east": -52, "north": 73,
     "label": "North America"},
    {"kind": "region", "west": 170, "south": -30, "east": -170, "north": 0,
     "label": "Pacific island area"},
    {"kind": "route", "label": "Crossing", "points": [
        {"latitude": 30, "longitude": 170, "label": "Start"},
        {"latitude": 35, "longitude": -170, "label": "Finish"},
    ]},
])
def test_point_region_route_maps_render_globally(tmp_path, brief, geometry):
    project = Studio(tmp_path).create(brief)
    shot = MapShot(id="map", start=0, duration=6, title="Where it happened",
                   geography=geometry, motion="zoom")
    project.spec.shots = [shot]
    project.spec.presentation = Presentation()
    project.spec.closed_captions = False
    assert camera(geography_for(shot), 0.1, motion="zoom") != camera(
        geography_for(shot), 0.9, motion="zoom"
    )
    for progress in (0.1, 0.5, 0.9):
        records = []
        image = scene(project, shot, progress, records=records)
        assert image.size == (brief.width, brief.height)
        assert all(item["contrast_estimate"] >= 4.5 for item in records)
    if geometry["kind"] == "route":
        view = camera(geography_for(shot), 1, motion="zoom")
        start = geo_project(30, 170, (0, 0, 456, 240), view)
        finish = geo_project(35, -170, (0, 0, 456, 240), view)
        assert abs(start[0] - finish[0]) < 456


def test_map_schema_rejects_missing_and_invalid_geometry():
    with pytest.raises(ValidationError):
        MapShot(id="bad", start=0, duration=5, title="No location")
    with pytest.raises(ValidationError):
        MapShot(id="bad", start=0, duration=5, title="Bad region", geography={
            "kind": "region", "west": 10, "east": 10, "south": 0, "north": 20,
            "label": "Bad", "route": [],
        })
