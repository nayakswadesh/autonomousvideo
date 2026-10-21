"""The approved screen blueprint owns copy, cue count, and graphic states."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from autonomous_studio.domain.models import GraphicIntent, LongFormChapter, NarrationBlock
from autonomous_studio.providers.longform_visuals import (
    ChapterVisualSelection,
    VisualBeatSelection,
    assemble_visual_selection,
    validate_family_choices,
)
from autonomous_studio.providers.semantic_illustrations import SemanticIllustrations
from autonomous_studio.rendering.semantic_diagrams import (
    ClaimBoundaryScene,
    ConceptContrastScene,
    CutawayScene,
    EvidenceScene,
    ProcessScene,
    SynthesisScene,
    _Draw,
    _process_copy,
    juxtaposition_detail_lines,
    render_frame,
    scene_asset_report,
    validate_process_slots,
    validate_scene_layout,
)
from autonomous_studio.rendering.visual_registry import FAMILIES, role_capacity
from autonomous_studio.workflow import semantic_scene_contract
from autonomous_studio.workflow.semantic_scene_contract import (
    apply_approved_graphic_visibility,
    bind_required_graphic_assets,
    build_scene_contract,
    validate_scene_fidelity,
)


def test_required_guided_graphics_use_local_base_and_generate_missing_subjects():
    beat = {
        "id": "solar-core-beat",
        "design_family": "cutaway",
        "design_treatment": "guided",
        "screen_title": "Inside a star",
        "overlay_text": ["Core", "Fuel supply"],
        "overlay_anchors": ["central core", "hydrogen supply"],
        "screen_blueprint": {
            "supporting_copy": ["", ""],
            "graphic_intents": [
                {"mode": "subject", "subject": "Sun’s core", "state": "",
                 "visual_change": "none"},
                {"mode": "subject", "subject": "Hydrogen supply in the core",
                 "state": "", "visual_change": "none"},
            ],
            "special_copy": {},
        },
    }
    contract = build_scene_contract(beat, "cutaway", "guided")
    payload = contract.assemble(
        {
            "symbol": "sun", "base_asset_concept": "solar cross-section study",
            "depiction": "schematic",
            "parts": [
                {"symbol": "sun-core", "asset_concept": "Sun’s central core",
                 "x": 0.55, "y": 0.5},
                {"symbol": "hydrogen-supply",
                 "asset_concept": "Hydrogen supply in the Sun’s core",
                 "x": 0.78, "y": 0.55},
            ],
        },
        beat=beat,
        narration="The central core has a dwindling hydrogen supply.",
        topic="A star", duration=9,
    )
    bound, decisions = bind_required_graphic_assets(payload, beat, contract)
    # The approved solar cross-section is already in the local catalog; only
    # the two required callout subjects lack suitable installed assets.
    assert bound["base_asset_concept"] == "solar cross-section study"
    assert [part["asset_concept"] for part in bound["parts"]] == [
        "illustrate:Sun’s core", "illustrate:Hydrogen supply in the core"
    ]
    assert len([item for item in decisions if item["asset_id"] == "pending/generated"]) == 2


def test_required_graphic_prefers_exact_local_asset_over_generation():
    beat = _beat()
    beat["screen_blueprint"]["graphic_intents"][0] = {
        "mode": "subject", "subject": "sun", "state": "", "visual_change": "none"
    }
    contract = build_scene_contract(beat, "process", "working")
    payload = contract.assemble(
        {"steps": [{"symbol": "box", "asset_concept": "vague star"},
                   {"symbol": "box", "asset_concept": None}]},
        beat=beat, narration="a battery begins charging. its charge level rises.",
        topic="A star", duration=9,
    )
    bound, decisions = bind_required_graphic_assets(payload, beat, contract)
    assert bound["steps"][0]["asset_concept"] == "sun"
    assert decisions[0]["reason"] == "exact local graphic"


def _beat(count=2, *, states=False):
    words = ["a battery begins charging", "its charge level rises",
             "the indicator reaches full", "the charger stops"]
    labels = ["Begins charging", "Charge rises", "Full charge", "Charger stops"][:count]
    intents = [
        {
            "mode": "state" if states else "none",
            "subject": "battery" if states else "",
            "state": label if states else "",
            "relation": "same_subject" if states else "not_applicable",
            "visual_change": (
                ("highlight", "fill_up", "expand", "contract")[index]
                if states else "none"
            ),
        }
        for index, label in enumerate(labels)
    ]
    return {
        "id": "battery-beat",
        "design_family": "process",
        "design_treatment": "transformation" if states else "working",
        "screen_title": "How a battery charges",
        "overlay_text": labels,
        "overlay_anchors": words[:count],
        "screen_blueprint": {
            "supporting_copy": ["" for _ in labels],
            "graphic_intents": intents,
            "special_copy": {},
        },
    }


def _scene(beat):
    contract = build_scene_contract(
        beat, beat["design_family"], beat["design_treatment"]
    )
    response = {
        "steps": [
            {"symbol": "box", "asset_concept": "battery" if (
                intent["mode"] == "state"
            ) else None}
            for intent in beat["screen_blueprint"]["graphic_intents"]
        ]
    }
    narration = ". ".join(beat["overlay_anchors"]) + "."
    payload = contract.assemble(
        response, beat=beat, narration=narration, topic="Battery charge", duration=16
    )
    for index, step in enumerate(payload["steps"]):
        step["at"] = 1 + index * 3
    return ProcessScene.model_validate(payload)


def test_sparse_scene_injects_only_planned_copy_and_graphic_states():
    beat = _beat(states=True)
    contract = build_scene_contract(beat, "process", "transformation")
    step_schema = contract.response_schema["$defs"]["Step"]["properties"]
    assert "label" not in step_schema
    assert "detail" not in step_schema
    assert "state_visual" not in step_schema
    assert "graphic_visible" not in step_schema
    scene = _scene(beat)
    assert [step.label for step in scene.steps] == beat["overlay_text"]
    assert [step.state_visual for step in scene.steps] == ["highlight", "fill_up"]
    validate_scene_fidelity(beat, scene)
    validate_scene_layout(scene)


def test_text_led_process_has_no_reported_or_painted_subject_placeholder():
    beat = _beat()
    contract = build_scene_contract(beat, "process", "working")
    with pytest.raises(ValueError, match="project-owned graphic visibility"):
        contract.assemble(
            {"steps": [{"symbol": "box", "graphic_visible": True}, {"symbol": "box"}]},
            beat=beat, narration="a battery begins charging. its charge level rises.",
            topic="Battery charge", duration=16,
        )
    scene = _scene(beat)
    assert [step.graphic_visible for step in scene.steps] == [False, False]
    validate_scene_fidelity(beat, scene)
    assert scene_asset_report(scene) == []
    assert render_frame(scene, 7, presentation="production").size == (960, 540)
    unauthorized = scene.model_copy(deep=True)
    unauthorized.steps[0].graphic_visible = True
    with pytest.raises(ValueError, match="graphic visibility"):
        validate_scene_fidelity(beat, unauthorized)
    upgraded = apply_approved_graphic_visibility(beat, unauthorized)
    assert [step.graphic_visible for step in upgraded.steps] == [False, False]
    assert scene_asset_report(upgraded) == []


def test_text_led_contrast_does_not_report_box_assets():
    contrast = {
        "id": "contrast-beat", "design_family": "concept_contrast",
        "design_treatment": "juxtaposition", "screen_title": "Two interpretations",
        "overlay_text": ["First interpretation", "Second interpretation"],
        "overlay_anchors": ["first interpretation", "second interpretation"],
        "screen_blueprint": {
            "supporting_copy": ["", ""],
            "graphic_intents": [{"mode": "none"}, {"mode": "none"}],
            "special_copy": {},
        },
    }
    payload = build_scene_contract(contrast, "concept_contrast", "juxtaposition").assemble(
        {"left": {"symbol": "box"}, "right": {"symbol": "box"}},
        beat=contrast,
        narration="The first interpretation differs from the second interpretation.",
        topic="Interpretations", duration=10,
    )
    payload["left"]["at"], payload["right"]["at"] = 1, 5
    scene = ConceptContrastScene.model_validate(payload)
    validate_scene_fidelity(contrast, scene)
    assert scene_asset_report(scene) == []
    assert render_frame(scene, 8, presentation="production").size == (960, 540)


def test_juxtaposition_warns_above_target_and_wraps_at_19px():
    detail = "Researchers followed the evidence toward them."
    assert len(detail) == 46
    assert len(juxtaposition_detail_lines(detail)) == 2
    fields = {
        "id": "contrast-wrap", "chapter_id": "chapter-2",
        "narration_block_ids": ["block-1"],
        "spoken_unit_ids": ["unit-1", "unit-2"],
        "role_unit_groups": [["unit-1"], ["unit-2"]],
        "visual_type": "comparison", "treatment": "hold",
        "design_choice": "concept_contrast/juxtaposition",
        "purpose": "Contrast what the instrument named with what its reading helped track.",
        "screen_title": "Following the evidence",
        "narration_anchor": "instrument had not named",
        "reveal_cues": [
            {"label": "The instrument named no elements.",
             "spoken_anchor": "instrument had not named"},
            {"label": "It helped follow the evidence.",
             "spoken_anchor": "followed the evidence"},
        ],
        "screen_blueprint": {
            "supporting_copy": ["", detail],
            "graphic_intents": [{"mode": "none"}, {"mode": "none"}],
        },
    }
    selection = ChapterVisualSelection.model_validate({
        "style_direction": "A calm, two-card editorial contrast with no graphics.",
        "rationale": "The contrast separates an instrument's observation from the later inference.",
        "beats": [fields],
    })
    with pytest.warns(UserWarning, match=r"46 characters \(target 45\)"):
        draft = assemble_visual_selection(selection)
    beat = draft.beats[0].model_dump(mode="json")
    narration = (
        "The instrument had not named the elements, but researchers "
        "followed the evidence toward them."
    )
    payload = build_scene_contract(beat, "concept_contrast", "juxtaposition").assemble(
        {"left": {"symbol": "box"}, "right": {"symbol": "box"}},
        beat=beat, narration=narration, topic="Marie Curie", duration=10,
    )
    payload["left"]["at"], payload["right"]["at"] = 1, 5
    scene = ConceptContrastScene.model_validate(payload)
    validate_scene_fidelity(beat, scene)
    validate_scene_layout(scene)
    assert render_frame(scene, 8, size=(1920, 1080), presentation="production").size == (
        1920, 1080
    )

    excessive = deepcopy(fields)
    excessive["screen_blueprint"]["supporting_copy"][1] = "x" * 61
    with pytest.raises(ValidationError, match="maximum is 60"):
        VisualBeatSelection.model_validate(excessive)
    unwrappable = deepcopy(fields)
    unwrappable["screen_blueprint"]["supporting_copy"][1] = "W" * 40
    with pytest.raises(ValidationError, match="unwrappable word"):
        VisualBeatSelection.model_validate(unwrappable)
    paired = deepcopy(fields)
    paired["design_choice"] = "concept_contrast/paired_contributions"
    with pytest.raises(ValidationError, match="maximum is 42"):
        VisualBeatSelection.model_validate(paired)


def test_text_led_mosaic_does_not_report_box_assets():
    mosaic = {
        "id": "mosaic-beat", "design_family": "synthesis",
        "design_treatment": "mosaic", "screen_title": "Three clues",
        "overlay_text": ["First clue", "Second clue", "Third clue", "A conclusion"],
        "overlay_anchors": ["first clue", "second clue", "third clue", "a conclusion"],
        "screen_blueprint": {
            "supporting_copy": ["", "", "", ""],
            "graphic_intents": [{"mode": "none"} for _ in range(4)],
            "special_copy": {},
        },
    }
    payload = build_scene_contract(mosaic, "synthesis", "mosaic").assemble(
        {"inputs": [{"symbol": "box"} for _ in range(3)]},
        beat=mosaic,
        narration="The first clue, second clue, and third clue lead to a conclusion.",
        topic="Three clues", duration=12,
    )
    for index, item in enumerate(payload["inputs"]):
        item["at"] = 1 + index * 3
    payload["takeaway_at"] = 10
    synthesis = SynthesisScene.model_validate(payload)
    validate_scene_fidelity(mosaic, synthesis)
    assert scene_asset_report(synthesis) == []
    assert render_frame(synthesis, 11, presentation="production").size == (960, 540)


def test_text_led_bounded_claim_hides_only_claim_icon():
    beat = {
        "id": "boundary-beat", "design_family": "claim_boundary",
        "design_treatment": "bounded_statement", "screen_title": "A bounded finding",
        "overlay_text": ["What is supported", "What remains unknown"],
        "overlay_anchors": ["what is supported", "what remains unknown"],
        "screen_blueprint": {
            "supporting_copy": ["", ""],
            "graphic_intents": [{"mode": "none"}, {"mode": "none"}],
            "special_copy": {},
        },
    }
    payload = build_scene_contract(beat, "claim_boundary", "bounded_statement").assemble(
        {"claim": {"symbol": "box"}, "limit": {"symbol": "box"}},
        beat=beat,
        narration="We can say what is supported, but not what remains unknown.",
        topic="A finding", duration=10,
    )
    payload["claim"]["at"], payload["limit"]["at"] = 1, 5
    scene = ClaimBoundaryScene.model_validate(payload)
    validate_scene_fidelity(beat, scene)
    assert scene_asset_report(scene) == []
    assert render_frame(scene, 8, presentation="production").size == (960, 540)


def test_bounded_statement_only_widens_its_lower_limit_label():
    limit_label = "Mechanism and structure remained unexplained."
    assert len(limit_label) == 45
    selection = {
        "id": "boundary-wide-limit", "chapter_id": "chapter-2",
        "narration_block_ids": ["block-1"],
        "spoken_unit_ids": ["unit-1", "unit-2"],
        "role_unit_groups": [["unit-1"], ["unit-2"]],
        "visual_type": "comparison", "treatment": "hold",
        "design_choice": "claim_boundary/bounded_statement",
        "purpose": "Bound the atomic-property finding by the unresolved mechanism.",
        "screen_title": "What Curie's measurements showed",
        "narration_anchor": "radioactivity belonged to atoms",
        "reveal_cues": [
            {"label": "Radioactivity belonged to atoms.",
             "spoken_anchor": "radioactivity belonged to atoms"},
            {"label": limit_label,
             "spoken_anchor": "mechanism and structure remained unexplained"},
        ],
        "screen_blueprint": {
            "supporting_copy": ["", ""],
            "graphic_intents": [{"mode": "none"}, {"mode": "none"}],
        },
    }
    beat = VisualBeatSelection.model_validate(selection).to_draft().model_dump(mode="json")
    contract = build_scene_contract(beat, "claim_boundary", "bounded_statement")
    narration = (
        "Her measurements showed radioactivity belonged to atoms, but the "
        "mechanism and structure remained unexplained."
    )
    scene_data = contract.assemble(
        {"claim": {"symbol": "box"}, "limit": {"symbol": "box"}},
        beat=beat, narration=narration, topic="Marie Curie", duration=10,
    )
    scene_data["claim"]["at"] = 1
    scene_data["limit"]["at"] = 5
    scene = ClaimBoundaryScene.model_validate(scene_data)
    validate_scene_fidelity(beat, scene)
    validate_scene_layout(scene)
    assert render_frame(scene, 8, presentation="production").size == (960, 540)

    too_long_claim = deepcopy(selection)
    too_long_claim["reveal_cues"][0]["label"] = limit_label
    with pytest.raises(ValidationError, match="designed reveal label exceeds renderer slot"):
        VisualBeatSelection.model_validate(too_long_claim)
    too_long_limit = deepcopy(selection)
    too_long_limit["reveal_cues"][1]["label"] = limit_label + "!"
    with pytest.raises(ValidationError, match="designed reveal label exceeds renderer slot"):
        VisualBeatSelection.model_validate(too_long_limit)
    scene_data["claim"]["label"] = limit_label
    with pytest.raises(ValidationError):
        ClaimBoundaryScene.model_validate(scene_data)
    scene_data["claim"]["label"] = "Radioactivity belonged to atoms."
    scene_data["limit"]["label"] = limit_label + "!"
    with pytest.raises(ValidationError):
        ClaimBoundaryScene.model_validate(scene_data)


def test_signal_to_inference_does_not_paint_unrequested_box_graphics():
    scene = EvidenceScene.model_validate({
        "id": "oscillation-evidence", "topic": "A star",
        "narration": "We measure vibrations, and those patterns constrain its structure.",
        "title": "What the vibration reveals", "duration": 10,
        "family": "evidence", "treatment": "signal_to_inference",
        "observed": "Measured vibrations", "observed_anchor": "measure vibrations",
        "inference": "Patterns constrain structure",
        "inference_anchor": "patterns constrain its structure",
        "reveal_at": 1, "inference_at": 6,
    })
    assert scene_asset_report(scene) == []
    image = render_frame(scene, 8, presentation="production")
    assert image.size == (960, 540)
    # The middle of each card is background, not a cyan/gold placeholder.
    assert image.getpixel((239, 282)) != (46, 184, 199)
    assert image.getpixel((721, 282)) != (237, 193, 122)


def test_fidelity_still_checks_copy_when_treatment_displays_detail():
    beat = _beat()
    beat["screen_blueprint"]["supporting_copy"][0] = "Battery gains energy"
    scene = _scene(beat)
    assert scene.steps[0].detail == "Battery gains energy"
    validate_scene_fidelity(beat, scene)
    changed = scene.model_copy(deep=True)
    changed.steps[0].detail = "Different explanation"
    with pytest.raises(ValueError, match="scene changed approved supporting copy"):
        validate_scene_fidelity(beat, changed)


@pytest.mark.parametrize("kind, should_pass", [("generated", True), ("svg", False)])
def test_graphic_subject_paraphrase_is_allowed_only_for_generated_art(
    monkeypatch, kind, should_pass
):
    approved_subject = "Molecular cloud collapsing as material gathers into a disk"
    asset_concept = (
        "molecular cloud collapsing as gas and dust gather into a rotating disk "
        "around the forming Sun"
    )
    catalog = SimpleNamespace(
        select=lambda _concept: SimpleNamespace(match="exact", asset_id="test-asset"),
        get=lambda _asset_id: SimpleNamespace(
            kind=kind, label=asset_concept, concepts=[asset_concept]
        ),
    )
    monkeypatch.setattr(semantic_scene_contract, "default_catalog", lambda: catalog)
    beat = {
        "id": "formation-beat", "design_family": "claim_boundary",
        "design_treatment": "bounded_statement",
        "screen_title": "A supported reconstruction, not footage",
        "overlay_text": ["Cloud collapses; disk forms", "Not a recording"],
        "overlay_anchors": ["collapsing", "not a recording"],
        "screen_blueprint": {
            "supporting_copy": ["", ""], "special_copy": {},
            "graphic_intents": [
                {"mode": "subject", "subject": approved_subject, "state": ""},
                {"mode": "none", "subject": "", "state": ""},
            ],
        },
    }
    contract = build_scene_contract(beat, "claim_boundary", "bounded_statement")
    payload = contract.assemble(
        {"claim": {"symbol": "box", "asset_concept": asset_concept},
         "limit": {"symbol": "box"}},
        beat=beat,
        narration="The cloud is collapsing, but this is not a recording of its birth.",
        topic="The Sun", duration=12,
    )
    payload["claim"]["at"] = 1
    payload["limit"]["at"] = 6
    scene = ClaimBoundaryScene.model_validate(payload)
    if should_pass:
        validate_scene_fidelity(beat, scene)
    else:
        with pytest.raises(ValueError, match="asset does not depict the planned subject"):
            validate_scene_fidelity(beat, scene)


def test_synthesis_counts_inputs_without_treating_takeaway_as_an_array_slot():
    beat = {
        "id": "synthesis-beat", "design_family": "synthesis",
        "design_treatment": "mosaic", "screen_title": "Evidence and reconstruction",
        "overlay_text": [
            "Neither eyewitness nor guess", "Dated material anchors history",
            "Models reconstruct the process", "No precise birthday",
        ],
        "overlay_anchors": [
            "neither eyewitness nor guess", "dated material", "models reconstruct",
            "no precise birthday",
        ],
        "screen_blueprint": {
            "supporting_copy": ["", "", "", ""], "special_copy": {},
            "graphic_intents": [
                {"mode": "none", "subject": "", "state": ""} for _ in range(4)
            ],
        },
    }
    contract = build_scene_contract(beat, "synthesis", "mosaic")
    payload = contract.assemble(
        {"inputs": [{"symbol": "box"} for _ in range(3)]},
        beat=beat,
        narration=(
            "This is neither eyewitness nor guess. Dated material anchors history; "
            "models reconstruct the process, but there is no precise birthday."
        ),
        topic="The Sun", duration=16,
    )
    for index, step in enumerate(payload["inputs"]):
        step["at"] = 1 + index * 3
    payload["takeaway_at"] = 10
    scene = SynthesisScene.model_validate(payload)
    validate_scene_fidelity(beat, scene)

    extra = scene.model_copy(deep=True)
    extra.inputs.append(extra.inputs[-1].model_copy(update={"at": 8}))
    with pytest.raises(ValueError, match="scene changed approved inputs cue count"):
        validate_scene_fidelity(beat, extra)


def test_cutaway_spoken_anchor_ignores_lost_em_dash_but_keeps_word_order():
    scene_data = {
        "id": "sun-core", "topic": "The Sun",
        "narration": (
            "The core is the Sun's central region, and the dwindling supply "
            "of hydrogen there not simply the passage of time matters."
        ),
        "title": "What changes in the Sun's core", "duration": 12,
        "family": "cutaway", "treatment": "guided", "symbol": "box",
        "parts": [
            {"label": "Solar core", "anchor": "the Sun's central region",
             "symbol": "box", "x": 0.4, "y": 0.5, "at": 1},
            {"label": "Hydrogen supply", "anchor": (
                "dwindling supply of hydrogen there—not simply the passage of time"
            ), "symbol": "box", "x": 0.6, "y": 0.5, "at": 7},
        ],
    }
    assert CutawayScene.model_validate(scene_data).parts[1].at == 7
    reversed_words = deepcopy(scene_data)
    reversed_words["parts"][1]["anchor"] = "passage of time not simply"
    with pytest.raises(ValidationError, match="every cutaway part must bind"):
        CutawayScene.model_validate(reversed_words)


def test_repeated_spoken_phrase_binds_to_successive_occurrences():
    scene_data = {
        "id": "repeat-test", "topic": "Repeated phrase",
        "narration": "We test the Sun; then later we test the Sun again.",
        "title": "Two observations", "duration": 12,
        "family": "process", "treatment": "working",
        "steps": [
            {"label": "First observation", "anchor": "test the Sun",
             "symbol": "box", "at": 1},
            {"label": "Second observation", "anchor": "test the Sun",
             "symbol": "box", "at": 7},
        ],
    }
    assert len(ProcessScene.model_validate(scene_data).steps) == 2
    one_occurrence = deepcopy(scene_data)
    one_occurrence["narration"] = "We test the Sun once, then describe the result."
    with pytest.raises(ValidationError, match="process cues must follow narration order"):
        ProcessScene.model_validate(one_occurrence)


def test_semantic_response_cannot_add_a_step_or_unapproved_detail():
    beat = _beat()
    contract = build_scene_contract(beat, "process", "working")
    base = {"steps": [{"symbol": "box"}, {"symbol": "box"}]}
    with pytest.raises(ValueError, match="supporting copy"):
        contract.assemble(
            {"steps": [{"symbol": "box", "detail": "New fact"}, {"symbol": "box"}]},
            beat=beat, narration="a battery begins charging. its charge level rises.",
            topic="Battery charge", duration=16,
        )
    with pytest.raises(ValueError, match="cue count"):
        contract.assemble(
            {"steps": [*base["steps"], {"symbol": "box"}]},
            beat=beat, narration="a battery begins charging. its charge level rises.",
            topic="Battery charge", duration=16,
        )


def test_sun_two_reveal_plan_rejects_the_saved_three_step_failure():
    beat = {
        "id": "sun-trial", "design_family": "process",
        "design_treatment": "transformation",
        "screen_title": "From stable to changing",
        "overlay_text": ["Stable phase", "Predicted departure"],
        "overlay_anchors": ["present stable phase", "departure from that phase"],
        "screen_blueprint": {
            "supporting_copy": ["", ""],
            "graphic_intents": [
                {"mode": "none", "subject": "", "state": "", "relation": "not_applicable",
                 "visual_change": "none"}
                for _ in range(2)
            ],
            "special_copy": {},
        },
    }
    contract = build_scene_contract(beat, "process", "transformation")
    narration = (
        "The Sun's present stable phase will not last forever. "
        "Models place its departure from that phase in the future."
    )
    with pytest.raises(ValueError, match="cue count"):
        contract.assemble(
            {"steps": [{"symbol": "box"} for _ in range(3)]},
            beat=beat, narration=narration, topic="The Sun", duration=18,
        )
    with pytest.raises(ValueError, match="supporting copy"):
        contract.assemble(
            {"steps": [{"symbol": "box", "detail": "Show the Sun"},
                       {"symbol": "box"}]},
            beat=beat, narration=narration, topic="The Sun", duration=18,
        )


def test_saved_scene_with_added_reveal_or_repeated_state_fails():
    beat = _beat(states=True)
    scene = _scene(beat)
    stale = scene.model_copy(deep=True)
    stale.steps[1].label = "Unapproved wording"
    with pytest.raises(ValueError, match="reveal copy"):
        validate_scene_fidelity(beat, stale)
    repeated = scene.model_copy(deep=True)
    repeated.steps[1].state_visual = repeated.steps[0].state_visual
    with pytest.raises(ValueError, match="graphic state"):
        validate_scene_fidelity(beat, repeated)
    extra = scene.model_copy(deep=True)
    extra.steps.append(extra.steps[-1].model_copy(update={"at": 13}))
    with pytest.raises(ValueError, match="cue count"):
        validate_scene_fidelity(beat, extra)


@pytest.mark.parametrize("count", [2, 3, 4])
def test_process_node_boxes_fit_or_fail_precisely(count):
    beat = _beat(count)
    scene = _scene(beat)
    validate_scene_layout(scene)
    broken = scene.model_copy(deep=True)
    broken.steps[-1].detail = "Supercalifragilisticexpialidocious long overflowing explanation"
    with pytest.raises(ValueError, match="process step .* detail"):
        validate_scene_layout(broken)


def test_unavailable_font_glyph_fails_before_render():
    scene = _scene(_beat())
    scene.steps[0].detail = "Hangul \ud55c"
    with pytest.raises(ValueError, match="font cannot display"):
        validate_scene_layout(scene)


def test_process_copy_rejects_caption_and_frame_intrusion():
    draw = _Draw(Image.new("RGB", (960, 540), "white"))
    kwargs = {"text": "A growing tree", "width": 200, "role": "step 1 detail",
              "bold": False, "color": "black", "size": 18, "minimum": 15}
    with pytest.raises(ValueError, match="caption-safe area"):
        _process_copy(draw, x=125, y=450, **kwargs)
    with pytest.raises(ValueError, match="frame-safe text area"):
        _process_copy(draw, x=10, y=400, **kwargs)


def test_process_copy_tries_smaller_font_before_rejecting_safe_boundary():
    image = Image.new("RGB", (960, 540), "white")
    draw = _Draw(image)
    kwargs = {
        "text": "Hydrogen nuclei are single protons",
        "x": 125, "y": 400, "width": 200, "role": "step 1 detail",
        "bold": False, "color": "black", "size": 18,
    }
    before = image.tobytes()
    with pytest.raises(ValueError, match="caption-safe area"):
        _process_copy(draw, minimum=18, **kwargs)
    assert image.tobytes() == before  # No first line from a rejected size is painted.
    _process_copy(draw, minimum=17, **kwargs)
    assert image.tobytes() != before
    validate_process_slots(
        "The proton-proton chain",
        ["Hydrogen nuclei; single protons", "Hydrogen becomes helium"],
        ["Hydrogen nuclei are single protons", "Helium forms; energy is released"],
    )


def test_generic_icon_cannot_stand_for_distinct_battery_states():
    beat = _beat()
    beat["screen_blueprint"]["graphic_intents"] = [
        {"mode": "state", "subject": "battery", "state": state,
         "relation": "same_subject", "visual_change": "none"}
        for state in ("starting to charge", "charge rising")
    ]
    contract = build_scene_contract(beat, "process", "working")
    payload = contract.assemble(
        {"steps": [{"symbol": "box", "asset_concept": "battery"} for _ in range(2)]},
        beat=beat, narration="a battery begins charging. its charge level rises.",
        topic="Battery charge", duration=16,
    )
    for index, step in enumerate(payload["steps"]):
        step["at"] = 1 + index * 3
    with pytest.raises(ValueError, match="generic icon does not depict planned state"):
        validate_scene_fidelity(beat, ProcessScene.model_validate(payload))


def test_growing_tree_uses_one_subject_with_distinct_approved_changes():
    beat = _beat(states=True)
    beat["id"] = "tree-growth"
    beat["screen_title"] = "A growing plant"
    beat["overlay_text"] = ["Tree begins growing", "Canopy expands"]
    beat["overlay_anchors"] = ["tree begins growing", "canopy expands"]
    beat["screen_blueprint"]["graphic_intents"] = [
        {"mode": "state", "subject": "tree", "state": label,
         "relation": "same_subject", "visual_change": change}
        for label, change in zip(beat["overlay_text"], ["highlight", "expand"], strict=True)
    ]
    beat["design_treatment"] = "transformation"
    response = {"steps": [{"symbol": "box", "asset_concept": "tree"} for _ in range(2)]}
    payload = build_scene_contract(beat, "process", "transformation").assemble(
        response, beat=beat,
        narration="A tree begins growing. Its canopy expands.",
        topic="Plant growth", duration=12,
    )
    payload["steps"][0]["at"] = 0.5
    payload["steps"][1]["at"] = 6.0
    scene = ProcessScene.model_validate(payload)
    validate_scene_fidelity(beat, scene)
    validate_scene_layout(scene)
    stale = scene.model_copy(deep=True)
    stale.steps[1].state_visual = "highlight"
    with pytest.raises(ValueError, match="graphic state"):
        validate_scene_fidelity(beat, stale)


@pytest.mark.parametrize(
    "subjects,relation",
    [(["tree", "tree"], "same_subject"),
     (["tree", "sun"], "distinct_subject")],
)
def test_subject_icons_may_repeat_only_when_the_plan_does_not_claim_state_change(
    subjects, relation,
):
    beat = _beat()
    beat["screen_blueprint"]["graphic_intents"] = [
        {"mode": "subject", "subject": subject, "state": "",
         "relation": relation, "visual_change": "none"}
        for subject in subjects
    ]
    response = {"steps": [
        {"symbol": "box", "asset_concept": subject} for subject in subjects
    ]}
    payload = build_scene_contract(beat, "process", "working").assemble(
        response, beat=beat,
        narration="a battery begins charging. its charge level rises.",
        topic="Unrelated icon plan", duration=16,
    )
    for index, step in enumerate(payload["steps"]):
        step["at"] = 1 + 3 * index
    validate_scene_fidelity(beat, ProcessScene.model_validate(payload))


@pytest.mark.parametrize("direction", [
    "illustrate:tree with label GROWTH",
    "illustrate:battery showing the number 100",
    "illustrate:sun with written caption",
])
def test_semantic_illustration_directions_cannot_embed_screen_copy(direction):
    resolver = SemanticIllustrations(
        SimpleNamespace(), SimpleNamespace(pipeline="live")
    )
    with pytest.raises(ValueError, match="embedded text"):
        asyncio.run(resolver.resolve(direction))


def test_visual_planner_rejects_unrenderable_blueprint_before_approval():
    beat = _beat()
    selection = {
        "id": beat["id"], "chapter_id": "chapter-1",
        "narration_block_ids": ["block-1"],
        "spoken_unit_ids": ["unit-1", "unit-2"],
        "role_unit_groups": [["unit-1"], ["unit-2"]],
        "visual_type": "timeline", "treatment": "progressive_highlight",
        "design_choice": "process/working", "purpose": "Explain a charging sequence.",
        "screen_title": beat["screen_title"],
        "narration_anchor": beat["overlay_anchors"][0],
        "reveal_cues": [
            {"label": label, "spoken_anchor": anchor}
            for label, anchor in zip(beat["overlay_text"], beat["overlay_anchors"], strict=True)
        ],
        "screen_blueprint": beat["screen_blueprint"],
    }
    assert VisualBeatSelection.model_validate(selection).to_draft().overlay_text == (
        beat["overlay_text"]
    )
    missing = deepcopy(selection)
    missing["screen_blueprint"]["supporting_copy"].pop()
    with pytest.raises(ValueError, match="every approved reveal"):
        VisualBeatSelection.model_validate(missing)
    invisible = deepcopy(selection)
    invisible["design_choice"] = "editorial_frame/statement_reveal"
    invisible["screen_blueprint"]["graphic_intents"][0] = {
        "mode": "subject", "subject": "battery",
    }
    with pytest.raises(ValueError, match="no visible renderer slot"):
        VisualBeatSelection.model_validate(invisible)
    wide = deepcopy(selection)
    wide["reveal_cues"][0]["label"] = "W" * 34
    with pytest.raises(ValueError, match="process step 1 label cannot fit"):
        VisualBeatSelection.model_validate(wide)
    glyph = deepcopy(selection)
    glyph["screen_blueprint"]["supporting_copy"][0] = "\ud55c"
    with pytest.raises(ValueError, match="font cannot display"):
        VisualBeatSelection.model_validate(glyph)


def test_subject_graphic_ignores_unused_state_before_semantic_planning():
    raw = {
        "id": "meteorite-beat", "chapter_id": "chapter-1",
        "narration_block_ids": ["block-1"],
        "spoken_unit_ids": ["u2", "u3"],
        "role_unit_groups": [["u2"], ["u3"]],
        "visual_type": "comparison", "treatment": "progressive_highlight",
        "design_choice": "cutaway/guided",
        "purpose": "Identify the meteorite material and its named inclusions.",
        "screen_title": "A clue preserved in meteorites",
        "narration_anchor": "One clue survives in primitive meteorites",
        "reveal_cues": [
            {"label": "Meteorite inclusions", "spoken_anchor": "primitive meteorites"},
            {"label": "Pockets rich in two elements", "spoken_anchor": "pockets of material"},
        ],
        "screen_blueprint": {
            "supporting_copy": ["Mineral inclusions", ""],
            "graphic_intents": [
                {"mode": "subject", "subject": "Primitive meteorite"},
                {
                    "mode": "subject",
                    "subject": "Calcium-aluminium-rich inclusion pockets",
                    "state": "Pockets rich in calcium and aluminium",
                    # Legacy plans may assert sameness while describing a more
                    # specific feature; the old equality check rejected this.
                    "relation": "same_subject",
                },
            ],
        },
    }
    selection = VisualBeatSelection.model_validate_json(json.dumps(raw))
    assert raw["screen_blueprint"]["graphic_intents"][1]["state"] == (
        "Pockets rich in calcium and aluminium"
    )
    assert selection.screen_blueprint.graphic_intents[1].state == ""
    assert selection.to_draft().screen_blueprint.graphic_intents[1].state == ""

    state = GraphicIntent(mode="state", subject="Sun", state="hydrogen dwindles",
                          relation="same_subject", visual_change="fill_down")
    assert state.state == "hydrogen dwindles"
    with pytest.raises(ValueError, match="needs the spoken state"):
        GraphicIntent(mode="state", subject="Sun", relation="same_subject")
    with pytest.raises(ValueError, match="subject-only icon cannot request"):
        GraphicIntent(mode="subject", subject="Sun", visual_change="fill_down")


def test_new_graphic_contract_omits_relation_but_reads_old_plans():
    schema = GraphicIntent.model_json_schema()
    assert "relation" not in schema["properties"]
    old = GraphicIntent.model_validate({
        "mode": "state", "subject": "balloon", "state": "expanding",
        "visual_change": "expand", "relation": "same_subject",
    })
    assert "relation" not in old.model_dump()
    assert old.subject == "balloon"


def test_transformation_accepts_near_identical_subject_descriptions():
    selection = {
        "id": "balloon-growth", "chapter_id": "chapter-1",
        "narration_block_ids": ["block-1"],
        "spoken_unit_ids": ["unit-1", "unit-2"],
        "role_unit_groups": [["unit-1"], ["unit-2"]],
        "visual_type": "timeline", "treatment": "progressive_highlight",
        "design_choice": "process/transformation",
        "purpose": "Show a balloon expanding as air enters it.",
        "screen_title": "The balloon expands",
        "narration_anchor": "The balloon begins to inflate",
        "reveal_cues": [
            {"label": "Balloon", "spoken_anchor": "balloon begins to inflate"},
            {"label": "Expanding", "spoken_anchor": "balloon expands"},
        ],
        "screen_blueprint": {
            "supporting_copy": ["", ""],
            "graphic_intents": [
                {"mode": "subject", "subject": "balloon"},
                {"mode": "state", "subject": "inflating balloon",
                 "state": "balloon expands", "visual_change": "expand"},
            ],
        },
    }
    planned = VisualBeatSelection.model_validate(selection)
    assert [item.subject for item in planned.screen_blueprint.graphic_intents] == [
        "balloon", "inflating balloon"
    ]
    selection["screen_blueprint"]["graphic_intents"][1]["visual_change"] = "none"
    with pytest.raises(ValueError, match="changing graphic states"):
        VisualBeatSelection.model_validate(selection)


@pytest.mark.parametrize(
    "subject,speech,first_anchor,second_anchor,state",
    [
        (
            "Sun", "Core hydrogen is depleted. The Sun will expand through giant stages.",
            "Core hydrogen is depleted", "expand through giant stages",
            "expands through giant stages",
        ),
        (
            "balloon", "The balloon contains air. It grows wider as more air enters.",
            "balloon contains air", "grows wider as more air enters",
            "inflating balloon with a larger outline",
        ),
    ],
)
def test_graphic_state_description_can_paraphrase_speech(
    subject, speech, first_anchor, second_anchor, state
):
    chapter = LongFormChapter(
        id="chapter-1", order=1, title="A changing subject",
        purpose="Explain the narrated physical change", question="What changes?",
        reveal="The subject expands", target_duration=12,
        narration_blocks=[NarrationBlock(id="block-1", text=speech, estimated_duration=12)],
    )
    selection = ChapterVisualSelection.model_validate({
        "style_direction": "A clear documentary diagram showing a changing subject.",
        "rationale": "The two spoken states are shown through a physical expansion.",
        "beats": [{
            "id": "ch1-beat1", "chapter_id": chapter.id,
            "narration_block_ids": ["block-1"],
            "spoken_unit_ids": ["block-1_u1", "block-1_u2"],
            "role_unit_groups": [["block-1_u1"], ["block-1_u2"]],
            "visual_type": "comparison", "treatment": "progressive_highlight",
            "design_choice": "process/transformation",
            "purpose": "Show the subject changing from its initial condition.",
            "screen_title": "A physical change", "narration_anchor": first_anchor,
            "reveal_cues": [
                {"label": "Initial condition", "spoken_anchor": first_anchor},
                {"label": "Expansion", "spoken_anchor": second_anchor},
            ],
            "screen_blueprint": {
                "supporting_copy": ["", ""],
                "graphic_intents": [
                    {"mode": "subject", "subject": subject},
                    {"mode": "state", "subject": subject,
                     "state": state, "visual_change": "expand"},
                ],
            },
        }],
    })
    draft = assemble_visual_selection(selection)
    validate_family_choices(draft, SimpleNamespace(chapters=[chapter]), require_role_contract=True)
    assert draft.beats[0].screen_blueprint.graphic_intents[1].state == state
    assert draft.beats[0].overlay_anchors == [first_anchor, second_anchor]


@pytest.mark.parametrize(
    "family,treatment",
    [(family.id, treatment.id) for family in FAMILIES for treatment in family.treatments],
)
def test_every_registered_treatment_excludes_model_authored_screen_copy(family, treatment):
    count = role_capacity(family, treatment)[0]
    beat = {
        "id": "schema-proof", "design_family": family,
        "design_treatment": treatment, "screen_title": "Approved title",
        "overlay_text": [f"Cue {index}" for index in range(count)],
        "overlay_anchors": [f"spoken {index}" for index in range(count)],
        "screen_blueprint": {
            "supporting_copy": ["" for _ in range(count)],
            "graphic_intents": [{"mode": "none", "subject": "", "state": "",
                                "relation": "not_applicable", "visual_change": "none"}
                               for _ in range(count)],
            "special_copy": {},
        },
    }
    if family in {"quantitative_display", "scale_ladder", "range", "chronology"} or (
        family, treatment
    ) == ("evidence", "prediction"):
        beat["visual_data"] = {
            "values": [1.0, 2.0] if family == "range" or (
                family, treatment
            ) == ("evidence", "prediction") else [float(i + 1) for i in range(count)],
            "unit": "years", "dates": [str(2000 + i) for i in range(count)],
        }
    schema = build_scene_contract(beat, family, treatment).response_schema
    banned = {
        "title", "opening_label", "detail", "label", "anchor", "change",
        "document_label", "chapter_label", "expected", "observed", "inference",
        "qualifier", "outcome", "takeaway", "excerpt", "finding", "question",
        "invitation", "bridge", "resolution", "date", "value", "unit",
    }

    def visit(node):
        if isinstance(node, dict):
            for name, child in node.get("properties", {}).items():
                assert name not in banned, f"{family}/{treatment} exposes {name}"
                visit(child)
            for child in node.get("$defs", {}).values():
                visit(child)
            if "items" in node:
                visit(node["items"])
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(schema)
