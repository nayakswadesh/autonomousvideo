from copy import deepcopy

import pytest

from autonomous_studio.domain.motion import FactReveal, _contains_spoken_phrase, _plain
from autonomous_studio.workflow.motion_planning import plan_motion_chapter


def test_motion_cue_excerpt_matches_across_paragraph_breaks():
    narration = "A story closes.\n\nWhich finding surprised you?\n\nSubscribe for more."
    excerpt = "story closes. Which finding surprised you?"
    assert _plain(excerpt) in _plain(narration)


def test_spoken_cue_check_accepts_punctuation_variants_but_not_unspoken_words():
    narration = "Curie’s Nobel-winning research and her wartime radiology were distinct."
    assert _contains_spoken_phrase(narration, "curie s nobel winning research")
    assert _contains_spoken_phrase(narration, "Nobel-winning research and her wartime")
    assert not _contains_spoken_phrase(narration, "Curie's Nobel winning teaching")
    assert not _contains_spoken_phrase(narration, "bel winning")


def test_motion_takeaway_accepts_visual_beat_purpose_limit():
    shot = {
        "id": "beat1",
        "kind": "fact_reveal",
        "intent": "establish",
        "start": 0,
        "end": 2,
        "why_this_template": "The beat explains a spoken distinction.",
        "narration_block_ids": ["block1"],
        "line_1": "A spoken distinction",
        "cues": [
            {
                "at_seconds": 0.5,
                "narration_excerpt": "spoken distinction",
                "action": "show_line_1",
            }
        ],
    }
    valid = FactReveal.model_validate({**shot, "viewer_takeaway": "a" * 500})
    assert len(valid.viewer_takeaway) == 500
    with pytest.raises(ValueError, match="at most 500 characters"):
        FactReveal.model_validate({**shot, "viewer_takeaway": "a" * 501})


def _fixture():
    project = {
        "id": "test-project",
        "spec": {
            "chapters": [
                {
                    "id": "ch1",
                    "target_duration": 20.0,
                    "narration_blocks": [
                        {
                            "id": "b1",
                            "text": (
                                "Henri discovered uranium rays. Curie made measurement possible. "
                                "The instrument repeated the result."
                            ),
                        },
                        {
                            "id": "b2",
                            "text": (
                                "Marie and Pierre investigated together. "
                                "A laboratory notebook record survives from 1898."
                            ),
                        },
                    ],
                    "evidence_claim_ids": ["c1"],
                    "evidence_source_ids": ["s1"],
                    "visual_beats": [
                        {
                            "id": "v1",
                            "start": 0,
                            "duration": 4,
                            "narration_block_ids": ["b1"],
                            "visual_type": "generated_image",
                            "purpose": "Introduce Henri's earlier discovery of uranium rays.",
                            "asset_ref": "portrait",
                            "overlay_text": ["Henri", "Uranium rays"],
                            "claim_ids": ["c1"],
                            "source_ids": ["s1"],
                        },
                        {
                            "id": "v2",
                            "start": 4,
                            "duration": 6,
                            "narration_block_ids": ["b1"],
                            "visual_type": "comparison",
                            "purpose": "Contrast discovery with Curie's measurement.",
                            "asset_ref": "contrast",
                            "overlay_text": ["Discovery", "Measurement"],
                            "claim_ids": ["c1"],
                            "source_ids": ["s1"],
                        },
                        {
                            "id": "v3",
                            "start": 10,
                            "duration": 5,
                            "narration_block_ids": ["b2"],
                            "visual_type": "generated_image",
                            "purpose": "Show the surviving laboratory notebook record.",
                            "asset_ref": "notebook",
                            "overlay_text": ["Laboratory record", "1898"],
                            "claim_ids": ["c1"],
                            "source_ids": ["s1"],
                        },
                        {
                            "id": "v4",
                            "start": 15,
                            "duration": 5,
                            "narration_block_ids": ["b2"],
                            "visual_type": "authentic_image",
                            "purpose": "Show Marie and Pierre working together.",
                            "asset_ref": "photo",
                            "overlay_text": ["Marie and Pierre"],
                            "claim_ids": ["c1"],
                            "source_ids": ["s1"],
                        },
                    ],
                }
            ]
        },
        "longform_visual_plan": {
            "assets": [
                {"id": "portrait", "kind": "generated_image", "prompt": "Historical portrait"},
                {"id": "contrast", "kind": "code_rendered"},
                {
                    "id": "notebook",
                    "kind": "generated_image",
                    "prompt": "Historical laboratory notebook",
                },
                {"id": "photo", "kind": "authentic_image"},
            ]
        },
        "longform_media": {
            key: {"path": f"{key}.png"} for key in ("portrait", "contrast", "notebook", "photo")
        },
    }
    windows = [
        {"block_id": "b1", "start": 0.1, "end": 9.9},
        {"block_id": "b2", "start": 10.1, "end": 19.9},
    ]
    return project, windows


def test_auto_planner_reorders_to_narration_and_suppresses_fake_archives():
    project, windows = _fixture()
    plan, report = plan_motion_chapter(project, "ch1", windows)
    assert [shot.id for shot in plan.shots] == ["v1", "v2", "v4", "v3"]
    assert [shot.kind for shot in plan.shots] == [
        "fact_reveal",
        "concept_contrast",
        "camera",
        "fact_reveal",
    ]
    assert plan.shots[1].title == "Discovery vs. Measurement"
    assert plan.shots[-1].end == 20
    assert [decision["reason"] for decision in report["decisions"]].count(
        "suppressed_generated_historical_simulation"
    ) == 2


def test_family_first_preserves_approved_editorial_image_as_camera_shot():
    project, windows = _fixture()
    project["longform_visual_plan"]["assets"][0]["prompt"] = "Contemporary mineral illustration"
    project["longform_visual_plan"]["assets"][2]["prompt"] = (
        "Contemporary editorial illustration of a closed archival notebook inside "
        "a protective cradle; no readable handwriting or reproduced pages."
    )
    plan, _ = plan_motion_chapter(project, "ch1", windows, family_first=True)
    assert next(shot for shot in plan.shots if shot.id == "v3").kind == "camera"


def test_only_first_image_beat_receives_the_opening_and_thumbnail_promise():
    project, windows = _fixture()
    project["longform_visual_plan"]["assets"][0]["prompt"] = (
        "Contemporary mineral illustration"
    )
    project["longform_visual_plan"]["assets"][2]["prompt"] = (
        "Contemporary editorial illustration of a closed archival notebook in a cradle"
    )
    project["spec"]["chapters"][0]["visual_beats"][0].update(
        hook_headline="WHAT DID THIS DISCOVERY REVEAL?",
        thumbnail_headline="A SURPRISING DISCOVERY",
    )
    project["spec"]["chapters"][0]["visual_beats"][2].update(
        hook_headline="NOT THE OPENING",
    )
    plan, _ = plan_motion_chapter(project, "ch1", windows, family_first=True)
    opening = next(shot for shot in plan.shots if shot.id == "v1")
    later = next(shot for shot in plan.shots if shot.id == "v3")
    assert opening.hook_headline == "WHAT DID THIS DISCOVERY REVEAL?"
    assert opening.thumbnail_headline == "A SURPRISING DISCOVERY"
    assert later.hook_headline == ""


def test_family_first_refuses_to_replace_unsafe_image_with_unplanned_code_scene():
    project, windows = _fixture()
    with pytest.raises(ValueError, match="replan the visual rather than substituting"):
        plan_motion_chapter(project, "ch1", windows, family_first=True)


def test_auto_planner_fails_safely_when_approved_inputs_are_missing():
    project, windows = _fixture()
    with pytest.raises(ValueError, match="Measured narration"):
        plan_motion_chapter(project, "ch1", [])
    broken = deepcopy(project)
    del broken["longform_media"]["portrait"]
    with pytest.raises(ValueError, match="Missing acquired asset"):
        plan_motion_chapter(broken, "ch1", windows)
    broken = deepcopy(project)
    broken["spec"]["chapters"][0]["visual_beats"][0]["claim_ids"] = ["missing"]
    with pytest.raises(ValueError, match="Unknown claim"):
        plan_motion_chapter(broken, "ch1", windows)


def test_new_story_motion_uses_claim_links_without_copying_source_ids():
    project, windows = _fixture()
    project["spec"]["story_contract_version"] = 2
    project["spec"]["brief"] = {"research": {
        "claims": [{"id": "c1", "source_ids": ["s1", "s2"]}],
        "sources": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}],
    }}
    # Legacy approved data may still contain copied source tags, but new shots do not.
    project["spec"]["chapters"][0]["visual_beats"][0]["source_ids"] = ["s2"]
    plan, _ = plan_motion_chapter(project, "ch1", windows)
    assert plan.shots[0].claim_ids == ["c1"]
    assert "source_ids" not in plan.shots[0].model_dump()

    unrelated = deepcopy(project)
    unrelated["spec"]["chapters"][0]["visual_beats"][0]["source_ids"] = ["s3"]
    plan_motion_chapter(unrelated, "ch1", windows)

    unknown = deepcopy(project)
    unknown["spec"]["chapters"][0]["visual_beats"][0]["source_ids"] = ["missing"]
    plan_motion_chapter(unknown, "ch1", windows)


def test_weakly_aligned_terminal_diagram_uses_spoken_closing():
    project, windows = _fixture()
    chapter = project["spec"]["chapters"][0]
    chapter["narration_blocks"][1]["text"] += (
        " Measurement raised another question; what does it mean for matter."
    )
    chapter["visual_beats"].append(
        {
            "id": "v5",
            "start": 18,
            "duration": 2,
            "narration_block_ids": ["b2"],
            "visual_type": "comparison",
            "purpose": "Clarify collaborative roles around this discovery.",
            "asset_ref": "roles",
            "overlay_text": ["Measurement", "Instrumentation", "Chemical work"],
            "claim_ids": ["c1"],
            "source_ids": ["s1"],
        }
    )
    project["longform_visual_plan"]["assets"].append({"id": "roles", "kind": "code_rendered"})
    project["longform_media"]["roles"] = {"path": "roles.png"}
    plan, report = plan_motion_chapter(project, "ch1", windows)
    assert plan.shots[-1].id == "v5"
    assert plan.shots[-1].kind == "fact_reveal"
    assert plan.shots[-1].intent == "foreshadow"
    assert "matter" in plan.shots[-1].line_2
    assert report["decisions"][-1]["reason"] == (
        "closing_narration_replaces_weakly_aligned_relation"
    )


def test_auto_planner_never_treats_untyped_years_as_bar_values():
    project, windows = _fixture()
    chapter = project["spec"]["chapters"][0]
    chapter["narration_blocks"][0]["text"] += " The first year was 1898 and the next was 1903."
    chapter["visual_beats"][1]["overlay_text"] = ["1898 discovery", "1903 measurement"]
    plan, _ = plan_motion_chapter(project, "ch1", windows)
    assert next(shot for shot in plan.shots if shot.id == "v2").kind == "concept_contrast"
