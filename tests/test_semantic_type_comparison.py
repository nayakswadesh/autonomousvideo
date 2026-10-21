"""The opt-in semantic pilot must reject bad plans before rendering."""

from hashlib import sha256
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tools import render_semantic_type_comparison as pilot


def component(**overrides):
    data = {
        "label": "Moving part",
        "shape": "rectangle",
        "start_box": [100, 400, 200, 450],
        "label_box": [100, 470, 300, 510],
        "cue": "part moves",
        "end_box": [100, 500, 200, 550],
    }
    data.update(overrides)
    return data


def plan_data(baseline_hash):
    return {
        "project_id": "test-project",
        "project_version": 2,
        "baseline_sha256": baseline_hash,
        "shots": [{
            "kind": "spatial_cutaway",
            "shot_id": "shot_03",
            "heading": "Test cutaway",
            "source_ids": ["s1"],
            "accuracy_note": "Illustrative and not to scale.",
            "components": [
                component(),
                component(label="Static core", end_box=None, cue=None),
            ],
        }],
    }


def test_component_rejects_offscreen_and_unsynchronized_motion():
    with pytest.raises(ValidationError, match="mobile diagram frame"):
        pilot.Component.model_validate(component(start_box=[1, 400, 200, 450]))
    with pytest.raises(ValidationError, match="exact narration cue"):
        pilot.Component.model_validate(component(cue=None))


def test_plan_rejects_duplicate_shots_and_invalid_union():
    data = plan_data("a" * 64)
    data["shots"].append(data["shots"][0])
    with pytest.raises(ValidationError, match="duplicate pilot shot IDs"):
        pilot.PilotPlan.model_validate(data)
    data["shots"][1] = {**data["shots"][1], "kind": "invented"}
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        pilot.PilotPlan.model_validate(data)


def test_plan_requires_approved_baseline_source_and_order(tmp_path, monkeypatch):
    baseline = tmp_path / "approved.mp4"
    baseline.write_bytes(b"approved preview")
    correct_hash = sha256(b"approved preview").hexdigest()
    data = plan_data(correct_hash)
    project = SimpleNamespace(
        id="test-project", version=2, state="COMPLETED",
        spec=SimpleNamespace(
            presentation=True,
            shots=[SimpleNamespace(id="shot_03", kind="hero_image", source_ids=["s1"])],
            brief=SimpleNamespace(research=SimpleNamespace(
                sources=[SimpleNamespace(id="s1")]
            )),
            loop=SimpleNamespace(strategy="semantic", match_opening_visual=True),
        ),
    )
    monkeypatch.setattr(pilot, "phrase_time", lambda _project, _shot, _cue: 1.0)
    pilot.validate_plan(project, pilot.PilotPlan.model_validate(data), baseline)

    data["baseline_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="immutable baseline"):
        pilot.validate_plan(project, pilot.PilotPlan.model_validate(data), baseline)
    data["baseline_sha256"] = correct_hash

    data["shots"][0]["source_ids"] = ["s2"]
    with pytest.raises(ValueError, match="source support"):
        pilot.validate_plan(project, pilot.PilotPlan.model_validate(data), baseline)


def test_flow_must_preserve_loop_handoff(tmp_path, monkeypatch):
    baseline = tmp_path / "approved.mp4"
    baseline.write_bytes(b"approved preview")
    data = plan_data(sha256(b"approved preview").hexdigest())
    data["shots"] = [{
        "kind": "process_flow", "shot_id": "shot_06", "heading": "Process",
        "source_ids": ["s1"],
        "steps": [{"label": "First step", "cue": "first"},
                  {"label": "Second step", "cue": "second"}],
    }]
    project = SimpleNamespace(
        id="test-project", version=2, state="COMPLETED",
        spec=SimpleNamespace(
            presentation=True,
            shots=[SimpleNamespace(id="shot_06", kind="outro", source_ids=["s1"])],
            brief=SimpleNamespace(research=SimpleNamespace(
                sources=[SimpleNamespace(id="s1")]
            )),
            loop=SimpleNamespace(strategy="semantic", match_opening_visual=False),
        ),
    )
    monkeypatch.setattr(pilot, "phrase_time", lambda _project, _shot, cue:
                        {"first": 1.0, "second": 2.0}[cue])
    with pytest.raises(ValueError, match="approved loop visual"):
        pilot.validate_plan(project, pilot.PilotPlan.model_validate(data), baseline)
