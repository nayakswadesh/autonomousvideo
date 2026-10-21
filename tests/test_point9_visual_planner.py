"""New treatments are selectable from approved speech without topic-specific code."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from autonomous_studio.domain.models import LongFormChapter, NarrationBlock
from autonomous_studio.providers.longform_visuals import (
    ChapterVisualPlanDraft,
    DesignChoice,
    validate_family_choices,
)
from autonomous_studio.rendering.visual_registry import treatment_role_sequence
from autonomous_studio.workflow.spoken_units import chapter_units

CASES = [
    (
        "evidence", "signal_to_inference",
        "A detector recorded neutrinos. That observation supports fusion.",
        ["Neutrinos recorded", "Evidence for fusion"],
        ["recorded neutrinos", "supports fusion"],
    ),
    (
        "cutaway", "focal_inset",
        "The Sun has a core. Hydrogen fuses in that core.",
        ["The Sun", "Fusion in the core"],
        ["The Sun", "fuses in that core"],
    ),
    (
        "relationship", "nested_scope",
        "One branch sits within a network. The network serves a city. The city is wider still.",
        ["One branch", "The network", "The city"],
        ["One branch", "The network serves", "The city is wider"],
    ),
    (
        "claim_boundary", "bounded_statement",
        "The bridge is open to pedestrians. Vehicles remain excluded.",
        ["Open to pedestrians", "Vehicles excluded"],
        ["open to pedestrians", "Vehicles remain excluded"],
    ),
    (
        "scenario_branch", "conditional_fork",
        "Inspection determines the shipment's status. "
        "It can be released. It can be held for review.",
        ["Inspection decides", "Release possible", "Review possible"],
        ["Inspection determines", "can be released", "held for review"],
    ),
]


def draft_for(case):
    family, treatment, narration, labels, anchors = case
    chapter = LongFormChapter(
        id="ch1", order=1, title="Example", purpose="Explain one relationship",
        question="What follows?", reveal="The supported relationship",
        target_duration=12, narration_blocks=[
            NarrationBlock(id="block1", text=narration, estimated_duration=12)
        ],
    )
    units = chapter_units(chapter)
    assert len(units) == len(labels)
    unit_ids = [unit["id"] for unit in units]
    draft = ChapterVisualPlanDraft.model_validate({
        "style_direction": "A consistent explanatory documentary design language.",
        "rationale": "The approved spoken roles support this explanatory relationship.",
        "beats": [{
            "id": "beat1", "chapter_id": "ch1", "narration_block_ids": ["block1"],
            "spoken_unit_ids": unit_ids, "role_unit_groups": [[item] for item in unit_ids],
            "role_semantics": treatment_role_sequence(family, treatment, len(labels)),
            "visual_type": "comparison", "treatment": "hold",
            "design_family": family, "design_treatment": treatment,
            "design_rationale": "The spoken relationship fits the selected ordered role pattern.",
            "purpose": "Explain the spoken relationship with aligned visual cues.",
            "screen_title": "One supported relationship",
            "narration_anchor": anchors[0], "overlay_text": labels,
            "overlay_anchors": anchors, "asset_id": "asset1",
        }],
        "assets": [{"kind": "code_rendered", "id": "asset1",
                    "beat_ids": ["beat1"], "renderer": "comparison"}],
    })
    return draft, SimpleNamespace(chapters=[chapter])


@pytest.mark.parametrize("case", CASES, ids=[f"{x[0]}-{x[1]}" for x in CASES])
def test_new_visual_relationships_pass_first_draft_contract(case):
    family, treatment, *_ = case
    assert DesignChoice(f"{family}/{treatment}")
    draft, spec = draft_for(case)
    validate_family_choices(draft, spec, require_role_contract=True)


@pytest.mark.parametrize("case", CASES, ids=[f"{x[0]}-{x[1]}" for x in CASES])
def test_new_visual_relationships_reject_swapped_spoken_roles(case):
    draft, spec = draft_for(case)
    draft.beats[0].role_semantics[0], draft.beats[0].role_semantics[-1] = (
        draft.beats[0].role_semantics[-1], draft.beats[0].role_semantics[0]
    )
    with pytest.raises(ValueError, match="needs spoken roles"):
        validate_family_choices(draft, spec, require_role_contract=True)


def test_signal_to_inference_does_not_require_invented_expectation():
    draft, spec = draft_for(CASES[0])
    validate_family_choices(draft, spec, require_role_contract=True)
    draft.beats[0].design_treatment = "competing"
    draft.beats[0].role_semantics = ["expected", "observed", "inference"]
    with pytest.raises(ValueError):
        validate_family_choices(draft, spec, require_role_contract=True)
