import pytest

from autonomous_studio.audio.alignment import estimated_words
from autonomous_studio.domain.models import LongFormChapter
from autonomous_studio.domain.motion import _plain
from autonomous_studio.providers.longform_visuals import (
    ChapterVisualPlanDraft,
    validate_family_choices,
)
from autonomous_studio.workflow.motion_planning import _excerpt, plan_motion_chapter
from autonomous_studio.workflow.spoken_units import (
    _aligned_word_slice,
    chapter_units,
    combined_speech,
    unique_phrase_position,
    validate_unit_coverage,
)


def test_missing_transcribed_unit_uses_neighbor_midpoint_only():
    script = "Before the pause but it dates early solids not the exact birthday."
    observed = [
        {"word": "Before", "start": 0.0, "end": 0.4},
        {"word": "the", "start": 0.4, "end": 0.6},
        {"word": "pause", "start": 0.6, "end": 3.0},
        {"word": "not", "start": 3.0, "end": 3.3},
        {"word": "the", "start": 3.3, "end": 3.5},
        {"word": "exact", "start": 3.5, "end": 3.8},
        {"word": "birthday", "start": 3.8, "end": 4.2},
    ]
    inferred = _aligned_word_slice(script, observed, 3, 8)
    assert inferred[0]["word"] == "but it dates early solids"
    assert inferred[0]["start"] == pytest.approx(1.8)
    assert inferred[0]["end"] == pytest.approx(1.9)
    assert inferred[0]["estimated"] is True
    assert _aligned_word_slice(script, observed, 8, 9) == observed[3:4]


def test_missing_unit_without_following_word_is_not_estimated():
    script = "Before the pause not the exact birthday but it dates early solids."
    observed = [
        {"word": word, "start": i * 0.2, "end": (i + 1) * 0.2}
        for i, word in enumerate("Before the pause not the exact birthday".split())
    ]
    with pytest.raises(ValueError, match="could not be aligned"):
        _aligned_word_slice(script, observed, 7, 12)


def _chapter():
    return LongFormChapter.model_validate(
        {
            "id": "ch1",
            "order": 1,
            "title": "A discovery",
            "purpose": "Explain a discovery",
            "question": "What happened?",
            "reveal": "A measurement changed the answer.",
            "target_duration": 12,
            "narration_blocks": [
                {
                    "id": "b1",
                    "text": "The first observation was puzzling. Then the measurement began.",
                    "estimated_duration": 6,
                },
                {
                    "id": "b2",
                    "text": (
                        "The measurement revealed a hidden pattern. "
                        "That pattern changed the explanation."
                    ),
                    "estimated_duration": 6,
                },
            ],
        }
    )


def test_spoken_units_are_owned_once_and_repeated_phrase_needs_context():
    chapter = _chapter()
    units = chapter_units(chapter)
    assert len(units) == 4
    beats = [
        type(
            "Beat",
            (),
            {
                "id": "v1",
                "spoken_unit_ids": [u["id"] for u in units[:2]],
                "narration_block_ids": ["b1"],
            },
        )(),
        type(
            "Beat",
            (),
            {
                "id": "v2",
                "spoken_unit_ids": [u["id"] for u in units[2:]],
                "narration_block_ids": ["b2"],
            },
        )(),
    ]
    validate_unit_coverage(chapter, beats)
    beats[1].spoken_unit_ids.insert(0, units[1]["id"])
    with pytest.raises(ValueError, match="exactly once"):
        validate_unit_coverage(chapter, beats)
    with pytest.raises(ValueError, match="exactly once"):
        unique_phrase_position("measurement then measurement".split(), "measurement")


def test_motion_scene_can_continue_across_tts_blocks():
    chapter = _chapter().model_dump(mode="json")
    units = chapter_units(chapter)
    chapter["evidence_claim_ids"] = ["c1"]
    chapter["evidence_source_ids"] = ["s1"]
    chapter["visual_beats"] = [
        {
            "id": "v1",
            "start": 0,
            "duration": 8,
            "spoken_unit_ids": [u["id"] for u in units[:3]],
            "narration_block_ids": ["b1", "b2"],
            "visual_type": "timeline",
            "design_family": "editorial_frame",
            "design_treatment": "statement_reveal",
            "purpose": "Follow observation into measurement and its result.",
            "asset_ref": "a1",
            "narration_anchor": "The first observation",
            "overlay_text": ["The measurement began", "A hidden pattern"],
            "overlay_anchors": ["the measurement began", "revealed a hidden pattern"],
            "claim_ids": ["c1"],
            "source_ids": ["s1"],
        },
        {
            "id": "v2",
            "start": 8,
            "duration": 4,
            "spoken_unit_ids": [units[3]["id"]],
            "narration_block_ids": ["b2"],
            "visual_type": "timeline",
            "design_family": "editorial_frame",
            "design_treatment": "statement_reveal",
            "purpose": "Explain the changed conclusion.",
            "asset_ref": "a2",
            "narration_anchor": "That pattern changed",
            "overlay_text": ["A changed explanation"],
            "overlay_anchors": ["changed the explanation"],
            "claim_ids": ["c1"],
            "source_ids": ["s1"],
        },
    ]
    project = {
        "id": "test",
        "spec": {"chapters": [chapter]},
        "longform_visual_plan": {
            "assets": [{"id": "a1", "kind": "code_rendered"}, {"id": "a2", "kind": "code_rendered"}]
        },
        "longform_media": {"a1": {}, "a2": {}},
    }
    windows = [
        {
            "block_id": block["id"],
            "start": i * 6.0,
            "end": (i + 1) * 6.0,
            "words": estimated_words(block["text"], 6.0),
        }
        for i, block in enumerate(chapter["narration_blocks"])
    ]
    plan, _ = plan_motion_chapter(project, "ch1", windows, family_first=True)
    assert len(plan.shots) == 2
    assert plan.shots[0].narration_block_ids == ["b1", "b2"]
    assert plan.shots[0].end == plan.shots[1].start


@pytest.mark.parametrize("second_kind", ["generated_image", "timeline"])
def test_nonopening_scene_enters_on_first_reveal_without_shifting_camera_move(second_kind):
    speech = (
        "An image opens the story. Evidence arriving today includes solar neutrinos, "
        "tiny particles associated with reactions inside the Sun."
    )
    chapter = LongFormChapter.model_validate(
        {
            "id": "ch1", "order": 1, "title": "Evidence", "purpose": "Explain the observation",
            "question": "What arrives?", "reveal": "Solar neutrinos",
            "target_duration": 14,
            "narration_blocks": [{"id": "b1", "text": speech, "estimated_duration": 14}],
        }
    ).model_dump(mode="json")
    units = chapter_units(chapter)
    chapter["evidence_claim_ids"] = ["c1"]
    chapter["evidence_source_ids"] = ["s1"]
    chapter["visual_beats"] = [
        {
            "id": "opening", "start": 0, "duration": 5,
            "spoken_unit_ids": [units[0]["id"]], "narration_block_ids": ["b1"],
            "visual_type": "generated_image", "purpose": "Open with the image",
            "asset_ref": "a1", "narration_anchor": "An image opens the story",
            "overlay_text": [], "overlay_anchors": [], "claim_ids": ["c1"],
            "source_ids": ["s1"],
        },
        {
            "id": "evidence", "start": 5, "duration": 9,
            "spoken_unit_ids": [unit["id"] for unit in units[1:]],
            "narration_block_ids": ["b1"],
            "visual_type": second_kind, "purpose": "Show the incoming evidence",
            "asset_ref": "a2",
            "narration_anchor": "tiny particles associated with reactions inside the Sun",
            "overlay_text": ["Solar neutrinos"],
            "overlay_anchors": ["includes solar neutrinos"],
            "claim_ids": ["c1"], "source_ids": ["s1"],
        },
    ]
    project = {
        "id": "test", "spec": {"chapters": [chapter]},
        "longform_visual_plan": {"assets": [
            {"id": "a1", "kind": "generated_image"},
            {
                "id": "a2",
                "kind": second_kind if second_kind == "generated_image" else "code_rendered",
            },
        ]},
        "longform_media": {"a1": {}, "a2": {}},
    }
    window = [{
        "block_id": "b1", "start": 0.0, "end": 14.0,
        "words": estimated_words(speech, 14.0),
    }]
    plan, report = plan_motion_chapter(project, "ch1", window, family_first=True)
    opening, evidence = plan.shots
    assert opening.end == evidence.start
    assert evidence.start == pytest.approx(
        report["decisions"][1]["estimated_spoken_anchor"], abs=0.001
    )
    if second_kind == "generated_image":
        cues = {cue.action: cue.at_seconds for cue in evidence.cues}
        assert evidence.kind == "camera"
        assert evidence.start < cues["show_label"] < cues["start_move"] < evidence.end
        compressed = [{
            "block_id": "b1", "start": 0.0, "end": 1.5,
            "words": estimated_words(speech, 1.5),
        }]
        with pytest.raises(ValueError, match="first spoken reveal would precede its scene"):
            plan_motion_chapter(project, "ch1", compressed, family_first=True)
    else:
        assert evidence.cues[0].at_seconds >= evidence.start


def test_measured_speech_view_uses_the_second_occurrence_when_assigned():
    chapter = _chapter().model_dump(mode="json")
    units = chapter_units(chapter)
    beat = {"spoken_unit_ids": [units[2]["id"]], "narration_block_ids": ["b2"]}
    block = chapter["narration_blocks"][1]
    windows = {
        "b2": {
            "block_id": "b2",
            "start": 6.0,
            "end": 12.0,
            "words": estimated_words(block["text"], 6.0),
        }
    }
    spoken, window = combined_speech(chapter, beat, windows)
    assert spoken["text"] == "The measurement revealed a hidden pattern."
    assert window["start"] == 6.0
    assert all(word["start"] < 6 for word in window["words"])


def test_fallback_cue_excerpt_preserves_hyphenated_original_across_sentence_boundary():
    narration = (
        "Those measurements exposed the puzzle. Some uranium-bearing minerals "
        "were anomalously active, suggesting that uranium alone could not explain the readings."
    )
    chapter = {"narration_blocks": [{"id": "b1", "text": narration}]}
    units = chapter_units(chapter)
    beat = {"spoken_unit_ids": [unit["id"] for unit in units],
            "narration_block_ids": ["b1"]}
    block, _ = combined_speech(
        chapter, beat,
        {"b1": {"start": 0.0, "end": 8.0, "words": estimated_words(narration, 8.0)}},
    )
    excerpt = _excerpt(block["text"], 0.3)
    assert "uranium-bearing" in excerpt
    assert _plain(excerpt) in _plain(narration)


def test_two_role_treatment_rejects_three_spoken_visual_roles():
    units = chapter_units(_chapter())[:3]
    payload = {
        "style_direction": "A consistent explanatory visual design for this chapter.",
        "rationale": "The proposed scene would show three distinct spoken roles.",
        "beats": [
            {
                "id": "v1",
                "chapter_id": "ch1",
                "narration_block_ids": ["b1", "b2"],
                "spoken_unit_ids": [item["id"] for item in units],
                "role_unit_groups": [[item["id"]] for item in units],
                "visual_type": "comparison",
                "treatment": "progressive_highlight",
                "design_family": "concept_contrast",
                "design_treatment": "juxtaposition",
                "purpose": "Explain three separate ideas in a single comparison.",
                "asset_id": "a1",
            }
        ],
        "assets": [
            {"kind": "code_rendered", "id": "a1", "beat_ids": ["v1"], "renderer": "comparison"}
        ],
    }
    with pytest.raises(ValueError, match="supports 2"):
        validate_family_choices(ChapterVisualPlanDraft.model_validate(payload))
