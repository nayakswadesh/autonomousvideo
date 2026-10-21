from copy import deepcopy

import pytest
from PIL import Image
from pydantic import ValidationError

from autonomous_studio.domain.motion import MotionPlan, validate_motion_plan
from tools.render_longform_motion_comparison import (
    _camera,
    _comparison_frame,
    _concept_frame,
    _document_frame,
    _evidence_gap_frame,
    _fact_frame,
    _process_frame,
    _relationship_frame,
    _route_frame,
    validate_plan,
)


def _fixture():
    project = {
        "id": "example",
        "spec": {"chapters": [{"id": "ch1", "target_duration": 4.0}]},
        "longform_media": {"image-a": {}},
    }
    plan = {
        "project_id": "example",
        "chapter_id": "ch1",
        "shots": [
            {"start": 0.0, "end": 2.0, "kind": "camera", "asset_ref": "image-a"},
            {"start": 2.0, "end": 4.0, "kind": "route_reveal"},
        ],
    }
    return project, plan


def test_motion_plan_requires_contiguous_timeline_and_known_assets():
    project, plan = _fixture()
    assert validate_plan(plan, project)[1] == 4.0
    gap = deepcopy(plan)
    gap["shots"][1]["start"] = 2.2
    with pytest.raises(ValueError, match="contiguous"):
        validate_plan(gap, project)
    missing = deepcopy(plan)
    missing["shots"][0]["asset_ref"] = "missing"
    with pytest.raises(ValueError, match="Unknown asset"):
        validate_plan(missing, project)


def test_directed_camera_and_route_produce_temporal_change():
    image = Image.new("RGB", (300, 180))
    for x in range(300):
        for y in range(180):
            image.putpixel((x, y), (x % 256, y % 256, 60))
    shot = {
        "focus_start": [0.25, 0.5],
        "focus_end": [0.75, 0.5],
        "zoom_start": 1.05,
        "zoom_end": 1.2,
    }
    assert (
        _camera(image, shot, 0.0, (160, 90)).tobytes()
        != _camera(image, shot, 1.0, (160, 90)).tobytes()
    )
    route = {"origin": "A", "destination": "B", "year": "1891"}
    assert (
        _route_frame(route, 0.0, (960, 540)).tobytes()
        != _route_frame(route, 1.0, (960, 540)).tobytes()
    )


def _story_fixture(kind="comparison_build"):
    project = {
        "id": "example",
        "spec": {
            "chapters": [
                {
                    "id": "ch1",
                    "target_duration": 4.0,
                    "narration_blocks": [
                        {
                            "id": "b1",
                            "text": (
                                "The disk spans 100000 light years, yet it is "
                                "1000 light years thick."
                            ),
                        }
                    ],
                    "evidence_claim_ids": ["c1"],
                    "evidence_source_ids": ["s1"],
                }
            ]
        },
        "longform_media": {"document": {"path": "document.png"}},
    }
    base = {
        "id": "shot1",
        "kind": kind,
        "intent": "compare",
        "start": 0,
        "end": 4,
        "viewer_takeaway": "The disk is much wider than it is thick.",
        "why_this_template": "The two measured dimensions need an explicit visual comparison.",
        "narration_block_ids": ["b1"],
        "claim_ids": ["c1"],
        "source_ids": ["s1"],
        "title": "A very thin disk",
        "left_label": "Across",
        "right_label": "Thick",
        "left_value": 100000,
        "right_value": 1000,
        "unit": "light years",
        "cues": [
            {
                "at_seconds": 0.2,
                "narration_excerpt": "spans 100000 light years",
                "action": "reveal_left",
            },
            {
                "at_seconds": 2.2,
                "narration_excerpt": "1000 light years thick",
                "action": "reveal_right",
            },
        ],
    }
    plan = {
        "schema_version": 1,
        "project_id": "example",
        "chapter_id": "ch1",
        "comparison_name": "test-comparison",
        "width": 960,
        "height": 540,
        "fps": 24,
        "shots": [base],
    }
    windows = [{"block_id": "b1", "start": 0, "end": 4}]
    return project, plan, windows


def _validate_story(project, plan, windows):
    return validate_motion_plan(MotionPlan.model_validate(plan), project, windows)


def test_story_motion_plan_validates_timeline_and_grounding():
    project, plan, windows = _story_fixture()
    assert _validate_story(project, plan, windows)[1] == 4

    with pytest.raises(ValueError, match="Measured narration windows"):
        _validate_story(project, plan, [])

    missing_claim = deepcopy(plan)
    missing_claim["shots"][0]["claim_ids"] = []
    with pytest.raises(ValueError, match="needs claim IDs"):
        _validate_story(project, missing_claim, windows)

    invented_number = deepcopy(plan)
    invented_number["shots"][0]["right_value"] = 10000
    with pytest.raises(ValueError, match="Comparison numbers"):
        _validate_story(project, invented_number, windows)

    misplaced_cue = deepcopy(plan)
    misplaced_cue["shots"][0]["cues"][1]["at_seconds"] = 4.1
    with pytest.raises(ValueError, match="outside"):
        _validate_story(project, misplaced_cue, windows)

    unsupported_action = deepcopy(plan)
    unsupported_action["shots"][0]["cues"][1]["action"] = "draw_route"
    with pytest.raises(ValueError, match="Unsupported or missing"):
        _validate_story(project, unsupported_action, windows)

    invented_excerpt = deepcopy(plan)
    invented_excerpt["shots"][0]["cues"][1]["narration_excerpt"] = "an invented line"
    with pytest.raises(ValueError, match="not in narration"):
        _validate_story(project, invented_excerpt, windows)

    wrong_template = deepcopy(plan)
    wrong_template["shots"][0]["kind"] = "unknown_animation"
    with pytest.raises(ValidationError):
        MotionPlan.model_validate(wrong_template)


def test_story_motion_plan_rejects_missing_asset_and_wrong_speech_window():
    project, plan, windows = _story_fixture()
    plan["shots"][0] = {
        "id": "shot1",
        "kind": "document_highlight",
        "intent": "examine_evidence",
        "start": 0,
        "end": 4,
        "viewer_takeaway": "An original document contains the relevant evidence.",
        "why_this_template": "A highlight points directly to the cited document passage.",
        "narration_block_ids": ["b1"],
        "claim_ids": ["c1"],
        "source_ids": ["s1"],
        "asset_ref": "missing",
        "highlight_box": [0.1, 0.1, 0.7, 0.3],
        "cues": [
            {
                "at_seconds": 1.0,
                "narration_excerpt": "The disk spans",
                "action": "highlight_document",
            }
        ],
    }
    with pytest.raises(ValueError, match="Unknown asset"):
        _validate_story(project, plan, windows)
    plan["shots"][0]["asset_ref"] = "document"
    with pytest.raises(ValueError, match="outside measured narration"):
        _validate_story(project, plan, [{"block_id": "b1", "start": 2, "end": 4}])


def test_additional_templates_reveal_over_time():
    _, plan, _ = _story_fixture()
    shot = MotionPlan.model_validate(plan).model_dump(mode="json")["shots"][0]
    before = _comparison_frame(shot, (960, 540), 0).tobytes()
    after = _comparison_frame(shot, (960, 540), 3).tobytes()
    assert before != after

    process = {
        "title": "A process",
        "steps": ["First", "Second"],
        "cues": [
            {"action": "reveal_step", "step_index": 0, "at_seconds": 0.2, "duration_seconds": 0.3},
            {"action": "reveal_step", "step_index": 1, "at_seconds": 2.2, "duration_seconds": 0.3},
        ],
    }
    assert (
        _process_frame(process, (960, 540), 1).tobytes()
        != _process_frame(process, (960, 540), 3).tobytes()
    )

    document = {
        "highlight_box": [0.1, 0.1, 0.7, 0.3],
        "cues": [{"action": "highlight_document", "at_seconds": 2.2, "duration_seconds": 0.3}],
    }
    source = Image.new("RGB", (800, 600), (235, 230, 220))
    assert (
        _document_frame(source, document, (960, 540), 1).tobytes()
        != _document_frame(source, document, (960, 540), 3).tobytes()
    )


def test_qualitative_templates_animate_without_fabricated_measurements():
    base_cues = [
        {"action": "reveal_left", "at_seconds": 0.2, "duration_seconds": 0.3},
        {"action": "reveal_right", "at_seconds": 2.2, "duration_seconds": 0.3},
    ]
    concept = {
        "title": "Two distinct ideas",
        "left_label": "Observation",
        "right_label": "Measurement",
        "cues": base_cues,
    }
    assert (
        _concept_frame(concept, (960, 540), 1).tobytes()
        != _concept_frame(concept, (960, 540), 3).tobytes()
    )

    gap = {
        "title": "The discrepancy",
        "expected_label": "Expected activity",
        "observed_label": "Measured activity",
        "gap_label": "MISMATCH",
        "cues": [
            {"action": "show_expected", "at_seconds": 0.2},
            {"action": "show_observed", "at_seconds": 1.2},
            {"action": "show_gap", "at_seconds": 2.2},
        ],
    }
    assert (
        _evidence_gap_frame(gap, (960, 540), 1).tobytes()
        != _evidence_gap_frame(gap, (960, 540), 3).tobytes()
    )

    fact = {
        "eyebrow": "Prior discovery",
        "line_1": "Henri Becquerel",
        "line_2": "Uranium rays",
        "cues": [
            {"action": "show_line_1", "at_seconds": 0.2, "duration_seconds": 0.3},
            {"action": "show_line_2", "at_seconds": 2.2, "duration_seconds": 0.3},
        ],
    }
    assert _fact_frame(fact, (960, 540), 1).tobytes() != _fact_frame(fact, (960, 540), 3).tobytes()

    relation = {
        "title": "Collaborative roles",
        "nodes": ["Observation", "Instrumentation", "Chemical work"],
        "cues": [
            {"action": "reveal_step", "step_index": index, "at_seconds": index + 0.2}
            for index in range(3)
        ],
    }
    assert (
        _relationship_frame(relation, (960, 540), 1).tobytes()
        != _relationship_frame(relation, (960, 540), 3).tobytes()
    )
