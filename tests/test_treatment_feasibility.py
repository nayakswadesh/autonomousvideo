"""A selected treatment must be feasible from the approved spoken beat."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from autonomous_studio.providers.longform_visuals import (
    ChapterBeatPartition,
    ChapterVisualSelection,
    SpokenBeatPartition,
    VisualBeatSelection,
    selection_treatment_menu,
    validate_beat_partition,
    validate_partition_completion,
)
from autonomous_studio.rendering.visual_registry import treatment_plan_contract
from autonomous_studio.workflow.spoken_numbers import spoken_number_mentions
from autonomous_studio.workflow.spoken_units import chapter_units


def _partition(chapter, functions):
    units = chapter_units(chapter)
    assert len(units) == len(functions)
    return ChapterBeatPartition(beats=[SpokenBeatPartition(
        id="beat-1",
        spoken_unit_ids=[unit["id"] for unit in units],
        role_unit_groups=[[unit["id"]] for unit in units],
        role_functions=[
            {"function": function, "spoken_anchor": unit["text"].split(",")[0]}
            for function, unit in zip(functions, units, strict=True)
        ],
        visual_intent="Explain the narrated idea with its correct visual relationship.",
    )])


def _choices(menu):
    return {(item["family"], item["treatment"]) for item in menu["treatments"]}


def test_subject_and_feature_do_not_become_fake_transformation():
    chapter = {
        "id": "chapter-2", "order": 2,
        "narration_blocks": [{"id": "block-1", "text": (
            "The core is the star's central region. Its hydrogen supply is dwindling."
        )}],
    }
    partition = _partition(chapter, ["subject", "feature"])
    menu = validate_beat_partition(chapter, partition)["beat-1"]
    assert ("cutaway", "focal_inset") in _choices(menu)
    assert ("process", "transformation") not in _choices(menu)
    assert ("process", "working") not in _choices(menu)
    assert ("quantitative_display", "metric_reveal") not in _choices(menu)

    # The post-selection check uses the same prerequisite, even if a stale
    # or forged menu accidentally offered the treatment.
    stale = {**menu, "treatments": [
        {"family": "process", "treatment": "transformation"}
    ]}
    selected = SimpleNamespace(beats=[SimpleNamespace(
        id="beat-1", spoken_unit_ids=partition.beats[0].spoken_unit_ids,
        role_unit_groups=partition.beats[0].role_unit_groups,
        visual_type="comparison", design_family="process",
        design_treatment="transformation", screen_blueprint=None,
    )])
    with pytest.raises(ValueError, match="successive spoken states"):
        validate_partition_completion(chapter, selected, partition, {"beat-1": stale})


def test_spoken_measurements_filter_data_choices_without_double_counting_scale():
    assert spoken_number_mentions("five billion years") == ["five billion"]
    assert spoken_number_mentions("five to six billion years") == ["five", "six billion"]
    assert spoken_number_mentions("one hundred and twenty five samples") == [
        "one hundred and twenty five"
    ]
    assert spoken_number_mentions("one million two hundred thousand samples") == [
        "one million two hundred thousand"
    ]
    assert spoken_number_mentions("four point five six seven billion years") == [
        "four point five six seven billion"
    ]
    one = {
        "id": "chapter-2", "order": 2,
        "narration_blocks": [{"id": "block-1", "text": "The age is five billion years."}],
    }
    menu = validate_beat_partition(one, _partition(one, ["unclassified"]))["beat-1"]
    assert ("quantitative_display", "metric_reveal") in _choices(menu)
    assert ("range", "scale") not in _choices(menu)
    assert ("quantitative_display", "comparable_bars") not in _choices(menu)
    sent = selection_treatment_menu({"beat-1": menu})["beat-1"]
    metric = next(
        option for option in sent["treatments"]
        if option["design_choice"] == "quantitative_display/metric_reveal"
    )
    assert sent["observed_inputs"]["spoken_quantities"] == ["five billion"]
    assert sent["observed_inputs"]["candidate_units"] == ["years"]
    assert metric["minimum_spoken_quantities"] == 1
    assert metric["unit_must_be_spoken"] is True
    unitless = {
        "id": "chapter-2", "order": 2,
        "narration_blocks": [{"id": "block-1", "text": "The value rose to five."}],
    }
    menu = validate_beat_partition(
        unitless, _partition(unitless, ["unclassified"])
    )["beat-1"]
    assert ("quantitative_display", "metric_reveal") not in _choices(menu)


def test_explicit_operating_stages_can_still_choose_process():
    chapter = {
        "id": "chapter-2", "order": 2,
        "narration_blocks": [{"id": "block-1", "text": (
            "The sample begins frozen. Then the same sample melts."
        )}],
    }
    menu = validate_beat_partition(chapter, _partition(chapter, ["stage", "stage"]))[
        "beat-1"
    ]
    assert ("process", "transformation") in _choices(menu)
    assert ("process", "working") in _choices(menu)


def test_chronology_needs_spoken_date_in_each_role():
    undated = {
        "id": "chapter-2", "order": 2,
        "narration_blocks": [{"id": "block-1", "text": (
            "The observatory opened. The telescope was installed."
        )}],
    }
    menu = validate_beat_partition(
        undated, _partition(undated, ["unclassified", "unclassified"])
    )["beat-1"]
    assert not any(family == "chronology" for family, _ in _choices(menu))
    dated = {
        "id": "chapter-2", "order": 2,
        "narration_blocks": [{"id": "block-1", "text": (
            "The observatory opened in 1891. The telescope arrived in 1893."
        )}],
    }
    menu = validate_beat_partition(
        dated, _partition(dated, ["unclassified", "unclassified"])
    )["beat-1"]
    assert ("chronology", "event_track") in _choices(menu)


def test_selection_menu_and_validator_share_transformation_graphic_rule():
    contract = treatment_plan_contract("process", "transformation", 2)
    menu = selection_treatment_menu({"beat-1": {
        "image_allowed": False,
        "treatments": [{"family": "process", "treatment": "transformation",
                        "ordered_roles": ["stage", "stage"]}],
    }})["beat-1"]["treatments"][0]
    assert menu["graphic_rule"] == contract["graphic_rule"] == "same_subject_progression"
    assert menu["graphic_cue_indices"] == [0, 1]
    assert "fill_down" in menu["allowed_visual_changes"]

    fields = {
        "id": "beat-1", "chapter_id": "chapter-2", "narration_block_ids": ["block-1"],
        "spoken_unit_ids": ["u1", "u2"], "role_unit_groups": [["u1"], ["u2"]],
        "visual_type": "comparison", "treatment": "hold",
        "design_choice": "process/transformation",
        "purpose": "Show one object's successive physical states.",
        "screen_title": "The changing state",
        "narration_anchor": "first physical state",
        "reveal_cues": [
            {"label": "First state", "spoken_anchor": "first physical state"},
            {"label": "Second state", "spoken_anchor": "second physical state"},
        ],
        "screen_blueprint": {
            "supporting_copy": ["", ""],
            "graphic_intents": [
                {"mode": "subject", "subject": "battery"},
                {"mode": "state", "subject": "battery",
                 "state": "second physical state", "visual_change": "fill_down"},
            ],
        },
    }
    assert VisualBeatSelection.model_validate(fields).design_choice.value == (
        "process/transformation"
    )
    fields["screen_blueprint"]["graphic_intents"][0] = {"mode": "none"}
    with pytest.raises(ValidationError, match="baseline subject"):
        VisualBeatSelection.model_validate(fields)


def test_second_call_can_report_no_honest_visual_fit_without_inventing_scene():
    blocked = ChapterVisualSelection(
        style_direction="A restrained, legible documentary treatment.",
        rationale="The fixed roles need distinct visual explanations.",
        beats=[], blocked_beat_id="beat-7",
        blocker_reason="The two roles need different scene boundaries.",
    )
    assert blocked.blocked_beat_id == "beat-7"
    with pytest.raises(ValidationError, match="beats or one explicit planning blocker"):
        ChapterVisualSelection(
            style_direction="A restrained, legible documentary treatment.",
            rationale="The fixed roles need distinct visual explanations.",
            beats=[],
        )
