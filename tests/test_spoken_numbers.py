"""Numerical grounding accepts speech forms, not invented visual data."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from autonomous_studio.domain.models import LongFormChapter, NarrationBlock
from autonomous_studio.providers.longform_visuals import (
    ChapterVisualPlanDraft,
    CodeRequestDraft,
    VisualBeatDraft,
    validate_family_choices,
)
from autonomous_studio.workflow.spoken_numbers import numeric_literals, spoken_number_values
from autonomous_studio.workflow.spoken_units import chapter_units


@pytest.mark.parametrize(
    ("speech", "expected"),
    [
        ("roughly five to six billion years from now", {5, 6}),
        ("about 5 to 6 billion years", {5, 6}),
        ("twenty-five years", {25}),
        ("one hundred and twenty five samples", {125}),
        ("four point five six seven billion years", {Decimal("4.567")}),
        ("a million years", {1, 1_000_000}),
        ("one million two hundred thousand samples", {1_200_000}),
    ],
)
def test_spoken_number_values(speech, expected):
    assert expected <= spoken_number_values(speech)


def test_spoken_number_values_do_not_invent_precision_or_join_separate_items():
    assert spoken_number_values("several billion years") == set()
    assert Decimal(6) not in spoken_number_values("roughly five billion years")
    assert Decimal(3) not in spoken_number_values("one or two samples")
    assert Decimal(12) not in spoken_number_values("one, two samples")
    assert numeric_literals("5–6 billion years") == {Decimal(5), Decimal(6)}


def _range_plan(speech, values):
    chapter = LongFormChapter(
        id="chapter-1",
        order=1,
        title="Future",
        purpose="Explain a forecast",
        question="When?",
        reveal="A modeled range",
        target_duration=12,
        spoken_unit_version=2,
        narration_blocks=[NarrationBlock(id="block-1", text=speech, estimated_duration=12)],
    )
    unit_ids = [unit["id"] for unit in chapter_units(chapter)]
    draft = ChapterVisualPlanDraft(
        style_direction="A clean science graphic with a bounded axis.",
        rationale="The spoken range calls for a precise numerical treatment.",
        beats=[
            VisualBeatDraft(
                id="beat-1",
                chapter_id=chapter.id,
                narration_block_ids=["block-1"],
                spoken_unit_ids=unit_ids,
                role_unit_groups=[unit_ids],
                visual_type="comparison",
                treatment="hold",
                design_family="range",
                design_treatment="scale",
                purpose="Show the modeled future interval clearly.",
                screen_title="A distant modeled milestone",
                overlay_text=["Future interval"],
                narration_anchor="five to six billion years",
                overlay_anchors=["five to six billion years"],
                visual_data={"values": values, "unit": "billion years"},
                asset_id="range-graphic",
            )
        ],
        assets=[CodeRequestDraft(id="range-graphic", beat_ids=["beat-1"], renderer="comparison")],
    )
    return SimpleNamespace(chapters=[chapter]), draft


def test_range_visual_data_accepts_spoken_word_form_endpoints():
    spec, draft = _range_plan(
        "Stellar models place the change roughly five to six billion years from now.",
        [5, 6],
    )
    validate_family_choices(draft, spec, require_role_contract=True)


def test_range_visual_data_still_rejects_unspoken_endpoint():
    spec, draft = _range_plan(
        "Stellar models place the change roughly five to six billion years from now.",
        [5, 7],
    )
    with pytest.raises(ValueError, match="visual_data contains an unspoken number"):
        validate_family_choices(draft, spec, require_role_contract=True)
