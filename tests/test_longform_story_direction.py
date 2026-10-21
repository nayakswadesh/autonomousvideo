from copy import deepcopy

import pytest
from PIL import Image
from pydantic import ValidationError

from autonomous_studio.domain.motion import GuidedMark, MotionPlan
from tools.plan_longform_story_direction import (
    DirectionRequest,
    DirectorDraft,
    compile_direction,
)
from tools.render_longform_motion_comparison import _guided_frame


def _fixture():
    project = {
        "id": "p1",
        "spec": {"chapters": [{
            "id": "ch1", "target_duration": 20.0,
            "narration_blocks": [
                {"id": "b1", "text": (
                    "Uranium rays were observed by Henri Becquerel. "
                    "Curie then measured the effect."
                )},
                {"id": "b2", "text": "The investigation continued."},
            ],
            "evidence_claim_ids": ["c1"], "evidence_source_ids": ["s1"],
        }]},
        "longform_media": {"ore": {"path": "ore.png"}},
    }
    windows = [
        {"block_id": "b1", "start": 0.1, "end": 9.9},
        {"block_id": "b2", "start": 10.1, "end": 19.9},
    ]
    baseline = MotionPlan.model_validate({
        "project_id": "p1", "chapter_id": "ch1", "comparison_name": "baseline",
        "width": 640, "height": 360, "fps": 24,
        "shots": [
            {"id": "old1", "kind": "fact_reveal", "start": 0, "end": 10,
             "intent": "establish", "viewer_takeaway": "Initial observation",
             "why_this_template": "Earlier text-only treatment",
             "narration_block_ids": ["b1"], "claim_ids": ["c1"], "source_ids": ["s1"],
             "eyebrow": "BEFORE", "line_1": "Uranium rays", "line_2": "Becquerel",
             "cues": [
                 {"at_seconds": 0.1, "narration_excerpt": "Uranium rays",
                  "action": "show_line_1"},
                 {"at_seconds": 4.0, "narration_excerpt": "Henri Becquerel",
                  "action": "show_line_2"},
             ]},
            {"id": "old2", "kind": "fact_reveal", "start": 10, "end": 20,
             "intent": "establish", "viewer_takeaway": "Story continues",
             "why_this_template": "Existing approved cut remains",
             "narration_block_ids": ["b2"], "claim_ids": ["c1"], "source_ids": ["s1"],
             "eyebrow": "AFTER", "line_1": "Investigation", "line_2": "Continued",
             "cues": [
                 {"at_seconds": 10.1, "narration_excerpt": "The investigation",
                  "action": "show_line_1"},
                 {"at_seconds": 12.0, "narration_excerpt": "investigation continued",
                  "action": "show_line_2"},
             ]},
        ],
    })
    request = DirectionRequest.model_validate({
        "project_id": "p1", "chapter_id": "ch1", "baseline_plan": "baseline.json",
        "comparison_name": "story-led",
        "slots": [{"id": "new1", "start": 0, "end": 10,
                   "narration_block_id": "b1", "allowed_asset_refs": ["ore"],
                   "viewer_question": "What was known before measurement?",
                   "spoken_excerpt": (
                       "Uranium rays were observed by Henri Becquerel. "
                       "Curie then measured the effect."
                   ),
                   "required_visual_anchors": [
                       "Uranium rays", "Henri Becquerel", "measured the effect"
                   ],
                   "required_tags": ["Uranium rays"]}],
        "assets": [{"asset_ref": "ore", "description": "A single mineral specimen",
                    "rights_note": "Editorial illustration only"}],
    })
    draft = DirectorDraft.model_validate({"moments": [{
        "slot_id": "new1", "asset_ref": "ore",
        "viewer_takeaway": "Observation became measurable evidence",
        "why_visual": "Draw on the same object while the story develops",
        "visual_thread": "radiation-to-record",
        "marks": [
            {"kind": "tag", "narration_anchor": "Uranium rays", "label": "Uranium rays",
             "points": [[0.1, 0.12]], "radius": 0.08},
            {"kind": "ring", "narration_anchor": "Henri Becquerel", "label": "",
             "points": [[0.5, 0.45]], "radius": 0.08},
            {"kind": "arrow", "narration_anchor": "measured the effect", "label": "",
             "points": [[0.3, 0.55], [0.7, 0.55]], "radius": 0.08},
        ],
    }]})
    return project, windows, baseline, request, draft


def test_story_direction_anchors_visual_order_and_preserves_unchanged_tail():
    project, windows, baseline, request, draft = _fixture()
    plan = compile_direction(request, draft, baseline, project, windows)
    assert [shot.kind for shot in plan.shots] == ["guided_image", "fact_reveal"]
    assert plan.shots[1].model_dump() == baseline.shots[1].model_dump()
    times = [cue.at_seconds for cue in plan.shots[0].cues]
    assert times == sorted(times)
    assert times[0] < times[1] < times[2]


def test_story_direction_rejects_reversed_or_unspoken_marks():
    project, windows, baseline, request, draft = _fixture()
    reversed_draft = deepcopy(draft)
    reversed_draft.moments[0].marks.reverse()
    with pytest.raises(ValueError, match="spoken order"):
        compile_direction(request, reversed_draft, baseline, project, windows)
    unspoken = deepcopy(draft)
    unspoken.moments[0].marks[0].label = "Invented reading 42"
    no_required_tag = deepcopy(request)
    no_required_tag.slots[0].required_tags = []
    with pytest.raises(ValueError, match="wording not in narration"):
        compile_direction(no_required_tag, unspoken, baseline, project, windows)


def test_guided_mark_geometry_and_frame_render():
    with pytest.raises(ValidationError, match="at least 2 points"):
        GuidedMark.model_validate({
            "kind": "arrow", "narration_anchor": "spoken phrase", "points": [[0.5, 0.5]]
        })
    project, windows, baseline, request, draft = _fixture()
    shot = compile_direction(request, draft, baseline, project, windows).shots[0]
    source = Image.new("RGB", (640, 360), (45, 51, 57))
    before = _guided_frame(source, shot.model_dump(mode="json"), (640, 360), 0.0)
    after = _guided_frame(source, shot.model_dump(mode="json"), (640, 360), 8.0)
    assert before.size == after.size == (640, 360)
    assert before.tobytes() != after.tobytes()
