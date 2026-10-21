import asyncio

import pytest
from pydantic import TypeAdapter

from autonomous_studio.domain.models import Motion, Shot, VideoProject
from autonomous_studio.providers.mock import MockDirector
from autonomous_studio.rendering.scenes import scene


@pytest.mark.parametrize(
    "motion",
    [
        "fade",
        "push",
        "zoom",
        "spring",
        "reveal",
        "wipe",
        "draw",
        "compress",
        "magnify",
        "parallax",
        "ken_burns",
        "impact",
        "stagger",
        "hold",
    ],
)
def test_motion_primitives_are_deterministic(brief, motion):
    TypeAdapter(Motion).validate_python(motion)
    spec = asyncio.run(MockDirector().develop(brief, []))
    project = VideoProject(id="a" * 12, spec=spec)
    shot = spec.shots[0].model_copy(update={"motion": motion})
    first = scene(project, shot, 0.025)
    assert first.tobytes() == scene(project, shot, 0.025).tobytes()
    assert first.size == (brief.width, brief.height)
    if motion != "hold":
        assert first.tobytes() != scene(project, shot, 0.75).tobytes()


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "hero_image"},
        {"kind": "map", "latitude": 30, "longitude": 105},
        {"kind": "comparison", "labels": ["A", "B"], "values": [10, 30]},
        {"kind": "timeline", "labels": ["1900", "2000", "2020"]},
        {"kind": "data_chart", "values": [2, 3, 1, 5]},
        {"kind": "outro"},
    ],
)
def test_additional_shot_types(brief, fields):
    spec = asyncio.run(MockDirector().develop(brief, []))
    project = VideoProject(id="a" * 12, spec=spec)
    shot = TypeAdapter(Shot).validate_python(
        dict(id="extra", start=0, duration=4, title="Scene test", subtitle="Example", **fields)
    )
    assert scene(project, shot).size == (540, 960)
