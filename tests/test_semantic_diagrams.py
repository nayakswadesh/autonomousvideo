from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import ImageChops
from pydantic import TypeAdapter, ValidationError

from autonomous_studio.rendering.semantic_diagrams import Scene, render_frame

FIXTURES = Path(__file__).resolve().parents[1] / "examples/semantic_diagrams.json"
ADAPTER = TypeAdapter(Scene)


@pytest.mark.parametrize("value,unit", [(4.567, "billion years"), (240, "years")])
def test_metric_rule_stays_below_fitted_value(value, unit):
    scene = ADAPTER.validate_python({
        "id": "metric-proof", "topic": "A sample", "title": "Estimated age",
        "narration": f"Measurements suggest about {value:g} {unit}.",
        "duration": 6.0, "family": "quantitative_display", "treatment": "metric_reveal",
        "points": [{"label": "One estimate", "value": value,
                    "anchor": f"{value:g} {unit}", "at": 1.0}],
        "unit": unit,
    })
    frame = render_frame(scene, 3.0, presentation="production")
    pixels = frame.load()
    gold_rows = [y for y in range(frame.height) for x in range(frame.width)
                 if pixels[x, y] == (237, 193, 122)]
    cyan_rows = [y for y in range(frame.height) for x in range(frame.width)
                 if pixels[x, y] == (46, 184, 199)]
    assert gold_rows and cyan_rows
    assert min(cyan_rows) > max(gold_rows) + 10


def examples():
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def test_unrelated_scripts_use_all_four_families_and_both_treatments():
    scenes = TypeAdapter(list[Scene]).validate_python(examples())
    assert len(scenes) == 8
    assert len({scene.topic for scene in scenes}) == len(scenes)
    assert {scene.family for scene in scenes} == {"process", "evidence", "cutaway", "range"}
    assert all(scene.provenance == "illustrative" for scene in scenes)
    assert all(render_frame(scene, scene.duration * 0.8).size == (960, 540) for scene in scenes)
    assert all(
        ImageChops.difference(
            render_frame(scene, 0), render_frame(scene, scene.duration * 0.8)
        ).getbbox()
        for scene in scenes
    )


def test_renderer_does_not_require_topic_specific_names():
    data = examples()[0]
    data["topic"] = "Unrelated fictional world"
    data["title"] = "A different team prepares"
    data["narration"] = (
        "A different team gathers, prepares, divides into groups, and travels onward."
    )
    data["steps"][0]["label"] = "Assemble"
    for step, anchor in zip(data["steps"], ("gathers", "prepares", "divides", "travels onward")):
        step["anchor"] = anchor
    scene = ADAPTER.validate_python(data)
    assert render_frame(scene, 5).size == (960, 540)


@pytest.mark.parametrize(
    "index,change",
    [
        (0, lambda d: d["steps"][0].update(symbol="ore_sample")),
        (0, lambda d: d["steps"][1].update(at=0.3)),
        (0, lambda d: d["steps"][1].update(anchor="a phrase not spoken")),
        (0, lambda d: d["steps"][1].update(anchor="sent each team")),
        (3, lambda d: d.update(observed_value=None)),
        (4, lambda d: d.update(depiction="verified_physical")),
        (5, lambda d: d["parts"][1].update(symbol="unverified_object")),
        (6, lambda d: d.update(low=21, high=12)),
    ],
)
def test_invalid_visual_plans_fail_closed(index, change):
    data = examples()[index]
    change(data)
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(data)
