"""Future long-form closes use approved speech; older recap scenes remain valid."""

import asyncio
from pathlib import Path

import pytest

from autonomous_studio.audio.alignment import estimated_words
from autonomous_studio.domain.models import EngagementEnding, ScreenBlueprint
from autonomous_studio.providers.longform import (
    MockLongFormDirector,
    MockLongFormResearch,
    continuity_report,
    script_editorial_contract,
    validate_comment_invitation,
)
from autonomous_studio.providers.longform_visuals import (
    ChapterBeatPartition,
    VisualBeatSelection,
    closing_action_groups,
    validate_beat_partition,
)
from autonomous_studio.rendering.semantic_diagrams import (
    EditorialCloseScene,
    render_frame,
    validate_scene_layout,
)
from autonomous_studio.workflow.engine import load_brief
from autonomous_studio.workflow.semantic_integration import align_scene_to_measured_speech
from autonomous_studio.workflow.semantic_scene_contract import (
    build_scene_contract,
    validate_scene_fidelity,
)
from autonomous_studio.workflow.spoken_units import chapter_units

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/longform-pilot.yaml"


def _scripted():
    brief = load_brief(EXAMPLE)
    plan = asyncio.run(MockLongFormResearch().plan(brief))
    director = MockLongFormDirector()
    treatment = asyncio.run(director.develop_treatment(
        brief, plan, [], story_contract_version=2,
    ))
    scripted, report = asyncio.run(director.write_script(treatment, []))
    assert report.passed
    return scripted


def _beat(roles=("comment", "subscribe", "like_share")):
    labels = {
        "comment": "Which clue should we explain next?",
        "like": "Like this video",
        "share": "Share this video",
        "subscribe": "Subscribe for more stories",
        "like_share": "Like and share if useful",
    }
    anchors = {
        "comment": "Tell us in the comments",
        "like": "Like this video",
        "share": "Share this video",
        "subscribe": "Subscribe for more",
        "like_share": "like and share",
    }
    return {
        "id": "community-close", "design_family": "editorial_close",
        "design_treatment": "community_close", "screen_title": "Keep exploring",
        "overlay_text": [labels[role] for role in roles],
        "overlay_anchors": [anchors[role] for role in roles],
        "screen_blueprint": {
            "supporting_copy": ["" for _ in roles],
            "graphic_intents": [
                {"mode": "none", "subject": "", "state": "",
                 "relation": "not_applicable", "visual_change": "none"}
                for _ in roles
            ],
            "community_roles": list(roles), "special_copy": {},
        },
    }


def _scene(roles=("comment", "subscribe", "like_share")):
    beat = _beat(roles)
    text = {
        "comment": "Tell us in the comments which clue to explain next.",
        "subscribe": "Subscribe for more stories.",
        "like_share": "If useful, like and share this video.",
    }
    narration = " ".join(text[role] for role in roles)
    contract = build_scene_contract(beat, "editorial_close", "community_close")
    schema = contract.response_schema
    action_schema = schema["$defs"]["CommunityAction"]["properties"]
    assert not {"role", "label", "anchor", "at"} & action_schema.keys()
    assert "recap" not in schema["properties"]
    payload = contract.assemble(
        {"actions": [{} for _ in roles]}, beat=beat, narration=narration,
        topic="Unrelated science story", duration=12,
    )
    for index, action in enumerate(payload["actions"]):
        action["at"] = 0.5 + index * 3
    return beat, EditorialCloseScene.model_validate(payload)


@pytest.mark.parametrize("line", [
    "Tell us in the comments: which clue should we explain next?",
    "Which clue would you explore next? Leave your answer below.",
    "Let us know in the comments which experiment you would try.",
])
def test_complete_audience_line_is_actionable(line):
    validate_comment_invitation(line)


def test_vague_question_and_destination_without_question_fail():
    with pytest.raises(ValueError, match="comments destination"):
        validate_comment_invitation("Which clue should we explain next?")
    with pytest.raises(ValueError, match="viewer-answer prompt"):
        validate_comment_invitation("See more in the comments section.")
    validate_comment_invitation("")  # No audience question is a valid editorial choice.


def test_final_lines_form_one_partitioned_beat_not_three_cards():
    spec = _scripted()
    chapter = spec.chapters[-1]
    groups = closing_action_groups(chapter, spec.engagement_ending)
    assert [group["role"] for group in groups] == ["comment", "subscribe", "like_share"]
    units = chapter_units(chapter)
    prefix = [unit["id"] for unit in units[:groups[0]["start"]]]
    partition = ChapterBeatPartition.model_validate({"beats": [
        {"id": "answer", "spoken_unit_ids": prefix,
         "role_unit_groups": [prefix], "visual_intent": "Show the actual story payoff."},
        {"id": "audience", "spoken_unit_ids": [
            unit_id for group in groups for unit_id in group["spoken_unit_ids"]
         ], "role_unit_groups": [group["spoken_unit_ids"] for group in groups],
         "visual_intent": "Invite one answer and channel actions."},
    ]})
    menu = validate_beat_partition(chapter, partition, groups)
    assert [(item["family"], item["treatment"]) for item in menu["audience"]["treatments"]] == [
        ("editorial_close", "community_close")
    ]
    assert menu["audience"]["image_allowed"] is False
    split = partition.model_copy(deep=True)
    split.beats[-1].role_unit_groups = [[
        unit_id for group in groups for unit_id in group["spoken_unit_ids"]
    ]]
    with pytest.raises(ValueError, match="one role per approved line"):
        validate_beat_partition(chapter, split, groups)


def test_visual_selection_cannot_add_or_reorder_audience_roles():
    beat = _beat()
    selection = {
        "id": beat["id"], "chapter_id": "chapter_3",
        "narration_block_ids": ["closing_block"],
        "spoken_unit_ids": ["u1", "u2", "u3"],
        "role_unit_groups": [["u1"], ["u2"], ["u3"]],
        "visual_type": "outro", "treatment": "hold",
        "design_choice": "editorial_close/community_close",
        "purpose": "Invite one answer and a compact channel close.",
        "screen_title": beat["screen_title"],
        "narration_anchor": beat["overlay_anchors"][0],
        "reveal_cues": [
            {"label": label, "spoken_anchor": anchor}
            for label, anchor in zip(beat["overlay_text"], beat["overlay_anchors"], strict=True)
        ],
        "screen_blueprint": beat["screen_blueprint"],
    }
    draft = VisualBeatSelection.model_validate(selection).to_draft()
    roles = draft.screen_blueprint.community_roles
    assert roles == [
        "comment", "subscribe", "like_share"
    ]
    selection["screen_blueprint"]["community_roles"] = ["subscribe", "comment", "like_share"]
    with pytest.raises(ValueError, match="community question must lead"):
        VisualBeatSelection.model_validate(selection)


def test_duplicate_comment_appeal_fails_new_contract_but_not_old_saved_contract():
    spec = _scripted()
    spec.chapters[-1].narration_blocks[-1].text += " " + spec.engagement_ending.comment_question
    assert any(
        issue.code == "duplicate_comment_invitation"
        for issue in continuity_report(spec).issues
    )
    old = spec.model_copy(deep=True)
    old.closing_contract_version = 1
    assert not any(
        issue.code == "duplicate_comment_invitation"
        for issue in continuity_report(old).issues
    )


def test_disabled_ending_does_not_acquire_a_generic_comment_request():
    brief = load_brief(EXAMPLE)
    brief.engagement_ending.enabled = False
    plan = asyncio.run(MockLongFormResearch().plan(brief))
    director = MockLongFormDirector()
    treatment = asyncio.run(director.develop_treatment(
        brief, plan, [], story_contract_version=2,
    ))
    scripted, report = asyncio.run(director.write_script(treatment, []))
    assert report.passed
    assert closing_action_groups(scripted.chapters[-1], scripted.engagement_ending) == []
    assert scripted.engagement_ending.comment_question not in (
        scripted.chapters[-1].narration_blocks[-1].text
    )


@pytest.mark.parametrize("roles", [
    ("comment", "subscribe", "like_share"),
    ("comment", "subscribe"),
    ("subscribe",),
])
def test_community_scene_has_no_fake_recap_and_keeps_end_screen_region_clear(roles):
    beat, scene = _scene(roles)
    validate_scene_fidelity(beat, scene)
    validate_scene_layout(scene)
    frame = render_frame(scene, 11.9, presentation="production")
    assert frame.size == (960, 540)
    assert frame.getpixel((790, 250)) == frame.getpixel((790, 300))
    changed = scene.model_copy(deep=True)
    changed.actions[-1].label = "Unapproved appeal"
    with pytest.raises(ValueError, match="reveal copy"):
        validate_scene_fidelity(beat, changed)


def test_unapproved_extra_action_and_recap_are_rejected():
    beat, scene = _scene()
    contract = build_scene_contract(beat, "editorial_close", "community_close")
    with pytest.raises(ValueError, match="cue count"):
        contract.assemble(
            {"actions": [{}, {}, {}, {}]}, beat=beat, narration=scene.narration,
            topic=scene.topic, duration=scene.duration,
        )
    with pytest.raises(ValueError, match="project-owned scene fields"):
        contract.assemble(
            {"actions": [{}, {}, {}], "recap": []}, beat=beat,
            narration=scene.narration, topic=scene.topic, duration=scene.duration,
        )


def test_left_side_safe_zone_and_missing_logo_do_not_shift_copy(monkeypatch):
    from autonomous_studio.rendering import semantic_diagrams

    beat = _beat()
    beat["screen_blueprint"]["end_screen_side"] = "left"
    contract = build_scene_contract(beat, "editorial_close", "community_close")
    narration = (
        "Tell us in the comments which clue to explain next. "
        "Subscribe for more stories. If useful, like and share this video."
    )
    payload = contract.assemble(
        {"actions": [{}, {}, {}]}, beat=beat, narration=narration,
        topic="Another science story", duration=12,
    )
    for index, action in enumerate(payload["actions"]):
        action["at"] = 0.5 + 3 * index
    scene = EditorialCloseScene.model_validate(payload)
    monkeypatch.setattr(semantic_diagrams, "CHANNEL_LOGO", Path("missing-logo.png"))
    validate_scene_fidelity(beat, scene)
    validate_scene_layout(scene)
    frame = render_frame(scene, 11.9, presentation="production")
    assert frame.getpixel((170, 250)) == frame.getpixel((170, 300))


@pytest.mark.parametrize("options", [
    ("meteorite dating", "solar neutrinos"),
    ("an ancient clue", "a modern test", "a future prediction"),
])
def test_choice_close_preserves_approved_options_and_separate_action_callouts(options):
    beat = _beat()
    beat["screen_title"] = "Which clue surprised you?"
    beat["screen_blueprint"]["community_options"] = list(options)
    narration = (
        "Which clue surprised you: " + ", ".join(options[:-1]) +
        " or " + options[-1] + "? Tell us in the comments which clue to explain next. "
        "Subscribe for more stories. If useful, like and share this video."
    )
    contract = build_scene_contract(beat, "editorial_close", "community_close")
    assert "response_options" not in contract.response_schema["properties"]
    with pytest.raises(ValueError, match="project-owned scene fields"):
        contract.assemble(
            {"actions": [{}, {}, {}], "response_options": ["invented", "choices"]},
            beat=beat, narration=narration, topic="Unrelated story", duration=13,
        )
    payload = contract.assemble(
        {"actions": [{}, {}, {}]}, beat=beat, narration=narration,
        topic="Unrelated story", duration=13,
    )
    for index, action in enumerate(payload["actions"]):
        action["at"] = (4, 7, 10)[index]
    scene = EditorialCloseScene.model_validate(payload)
    validate_scene_fidelity(beat, scene)
    validate_scene_layout(scene)
    early = render_frame(scene, 2.0, presentation="production")
    middle = render_frame(scene, 8.0, presentation="production")
    final = render_frame(scene, 11.0, presentation="production")
    assert early.size == middle.size == final.size == (960, 540)
    # Like and Share occupy distinct left-hand positions and Subscribe the right.
    assert early.getpixel((80, 464)) != final.getpixel((80, 464))
    assert middle.getpixel((80, 464)) != final.getpixel((80, 464))
    assert final.getpixel((270, 464)) != early.getpixel((270, 464))
    assert final.getpixel((455, 464)) == middle.getpixel((455, 464))
    assert final.getpixel((800, 270)) == early.getpixel((800, 270))
    changed = scene.model_copy(deep=True)
    changed.response_options[0] = "unapproved choice"
    with pytest.raises(ValueError, match="approved response choices"):
        validate_scene_fidelity(beat, changed)


def test_choice_close_rejects_unspoken_or_single_choice():
    beat = _beat()
    beat["screen_blueprint"]["community_options"] = ["one answer"]
    with pytest.raises(ValueError, match="two or three"):
        ScreenBlueprint.model_validate(beat["screen_blueprint"])
    beat["screen_blueprint"]["community_options"] = ["spoken answer", "invented answer"]
    contract = build_scene_contract(beat, "editorial_close", "community_close")
    payload = contract.assemble(
        {"actions": [{}, {}, {}]}, beat=beat,
        narration="Spoken answer. Tell us in the comments. Subscribe for more. Like and share.",
        topic="Another story", duration=12,
    )
    for index, action in enumerate(payload["actions"]):
        action["at"] = 1 + index * 3
    with pytest.raises(ValueError, match="response choice must be spoken"):
        EditorialCloseScene.model_validate(payload)


def test_new_ending_contract_and_spoken_groups_are_comment_like_share_subscribe():
    ending = EngagementEnding(
        comment_question="Which clue surprised you? Tell us in the comments.",
        like_prompt="If this sparked your curiosity, like this video.",
        share_prompt="Share it with someone curious.",
        subscribe_reason="Subscribe for more evidence-led science stories.",
    )
    spec = _scripted().model_copy(deep=True)
    spec.closing_contract_version = 3
    spec.engagement_ending = ending
    assert script_editorial_contract(spec)["exact_ending_lines"] == [
        ending.comment_question, ending.like_prompt,
        ending.share_prompt, ending.subscribe_reason,
    ]
    chapter = {"spoken_unit_version": 2, "narration_blocks": [{
        "id": "closing", "text": (
            "The evidence changes the answer. "
            + " ".join(script_editorial_contract(spec)["exact_ending_lines"])
        ),
    }]}
    groups = closing_action_groups(chapter, ending)
    assert [group["role"] for group in groups] == [
        "comment", "like", "share", "subscribe",
    ]
    wrong = {"spoken_unit_version": 2, "narration_blocks": [{
        "id": "closing", "text": (
            "The evidence changes the answer. " + " ".join([
                ending.comment_question, ending.subscribe_reason,
                ending.like_prompt, ending.share_prompt,
            ])
        ),
    }]}
    with pytest.raises(ValueError, match="consecutive final passage"):
        closing_action_groups(wrong, ending)


def test_new_live_treatment_and_mock_script_use_four_action_contract():
    brief = load_brief(EXAMPLE)
    brief.engagement_ending.like_prompt = "If the story helped, like this video."
    brief.engagement_ending.share_prompt = "Share it with someone curious."
    plan = asyncio.run(MockLongFormResearch().plan(brief))
    director = MockLongFormDirector()
    treatment = asyncio.run(director.develop_treatment(
        brief, plan, [], story_contract_version=2,
    ))
    assert treatment.closing_contract_version == 3
    scripted, report = asyncio.run(director.write_script(treatment, []))
    assert report.passed
    assert [group["role"] for group in closing_action_groups(
        scripted.chapters[-1], scripted.engagement_ending,
    )] == ["comment", "like", "share", "subscribe"]


def test_new_ending_response_choices_reveal_on_measured_speech():
    roles = ("comment", "like", "share", "subscribe")
    beat = _beat(roles)
    beat["screen_title"] = "Which clue surprised you?"
    beat["screen_blueprint"]["community_options"] = [
        "meteorite dating", "solar neutrinos",
    ]
    narration = (
        "Which clue surprised you more: meteorite dating or solar neutrinos? "
        "Tell us in the comments. Like this video. Share this video. "
        "Subscribe for more stories."
    )
    contract = build_scene_contract(beat, "editorial_close", "community_close")
    assert "response_option_ats" not in contract.response_schema["properties"]
    payload = contract.assemble(
        {"actions": [{} for _ in roles]}, beat=beat, narration=narration,
        topic="The evidence story", duration=14,
    )
    for index, action in enumerate(payload["actions"]):
        action["at"] = (5, 7, 9, 11)[index]
    scene = EditorialCloseScene.model_validate(payload)
    aligned, action_times = align_scene_to_measured_speech(
        scene, narration, {"start": 0.0, "words": estimated_words(narration, 14)},
        0.0, 14.0,
    )
    first, second = aligned.response_option_ats
    assert 0 < first < second < aligned.actions[0].at
    assert action_times == sorted(action_times)
    before = render_frame(aligned, first - 0.1, presentation="production")
    between = render_frame(aligned, (first + second) / 2, presentation="production")
    after = render_frame(aligned, second + 0.1, presentation="production")
    assert before.getpixel((120, 205)) != between.getpixel((120, 205))
    assert between.getpixel((120, 264)) != after.getpixel((120, 264))
    assert render_frame(aligned, 13.0, presentation="production").size == (960, 540)


def test_response_choice_repeated_in_later_like_prompt_uses_question_occurrence():
    roles = ("comment", "like", "share", "subscribe")
    beat = _beat(roles)
    beat["screen_blueprint"]["community_options"] = ["Measurement", "chemistry"]
    beat["overlay_anchors"][2] = "Share it with a science-curious friend"
    narration = (
        "Measurement or chemistry—which surprised you more? "
        "Tell us in the comments. Like this video for measurement-led science. "
        "Share it with a science-curious friend. Subscribe for more evidence-led stories."
    )
    contract = build_scene_contract(beat, "editorial_close", "community_close")
    payload = contract.assemble(
        {"actions": [{} for _ in roles]}, beat=beat, narration=narration,
        topic="Marie Curie", duration=18,
    )
    for index, action in enumerate(payload["actions"]):
        action["at"] = (6, 8, 11, 14)[index]
    scene = EditorialCloseScene.model_validate(payload)
    aligned, _ = align_scene_to_measured_speech(
        scene, narration, {"start": 0.0, "words": estimated_words(narration, 18)},
        0.0, 18.0,
    )
    assert aligned.response_option_ats[0] < aligned.response_option_ats[1]
    assert aligned.response_option_ats[1] < aligned.actions[0].at
    assert aligned.response_option_ats[0] < aligned.actions[1].at
