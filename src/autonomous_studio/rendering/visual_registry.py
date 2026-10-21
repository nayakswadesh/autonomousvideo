"""Capability and feasibility contracts for long-form designed scenes.

New visual plans compile beat-specific choices from this registry. Saved legacy
plans retain their existing interpretation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from autonomous_studio.workflow.spoken_numbers import (
    spoken_number_mentions,
    spoken_unit_candidates,
)

_REJECTED_CLAIM = re.compile(
    r"^(?:myth|false claim|incorrect claim|misconception|unsupported claim)\s*[:—-]",
    re.IGNORECASE,
)


def effective_design_treatment(beat: dict) -> str | None:
    """A correction graphic requires its crossed-out side to be a rejected claim."""
    treatment = beat.get("design_treatment")
    if beat.get("design_family") == "concept_contrast" and treatment == "correction_reveal":
        labels = beat.get("overlay_text") or []
        if not labels or not _REJECTED_CLAIM.match(labels[0].strip()):
            purpose = (beat.get("purpose") or "").casefold()
            if any(
                word in purpose for word in ("contribution", "complementary role", "distinct role")
            ):
                return "paired_contributions"
            return "juxtaposition"
    return treatment


@dataclass(frozen=True)
class TreatmentCapability:
    id: str
    composition: str
    motion: str


@dataclass(frozen=True)
class TreatmentPrerequisites:
    """Machine-checkable inputs needed before a treatment can be offered."""

    minimum_quantities: int = 0
    minimum_dates: int = 0
    date_per_role: bool = False
    spoken_unit: bool = False
    role_shape: str = "any"
    graphic_rule: str = "static_or_none"


_DATE_EXPRESSION = re.compile(
    r"\b(?:\d+(?:\.\d+)?\s+(?:(?:thousand|million|billion)\s+)?years?\s+ago|"
    r"\d{1,4}\s*(?:BCE|BC|CE|AD)|(?:1[0-9]{3}|20[0-9]{2})|"
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2}(?:,?\s+\d{3,4})?)\b",
    re.IGNORECASE,
)


def spoken_date_mentions(text: str) -> list[str]:
    matches = list(_DATE_EXPRESSION.finditer(text))
    dates = [match.group() for match in matches]
    covered_ends = {match.end() for match in matches}
    for match in re.finditer(r"\byears?\s+ago\b", text, re.IGNORECASE):
        context = text[max(0, match.start() - 55):match.start()]
        if match.end() not in covered_ends and spoken_number_mentions(context):
            dates.append((context + match.group()).strip())
    return dates


def treatment_prerequisites(
    family: str, treatment: str, role_count: int
) -> TreatmentPrerequisites:
    """One registry-owned rule set for menu compilation and output checking."""
    if family == "process":
        return TreatmentPrerequisites(
            role_shape="state_progression" if treatment == "transformation" else "stages",
            graphic_rule=(
                "same_subject_progression" if treatment == "transformation"
                else "static_or_none"
            ),
        )
    if family == "range":
        return TreatmentPrerequisites(minimum_quantities=2, spoken_unit=True)
    if family == "scale_ladder":
        return TreatmentPrerequisites(minimum_quantities=role_count, spoken_unit=True)
    if family == "quantitative_display":
        return TreatmentPrerequisites(
            minimum_quantities=1 if treatment == "metric_reveal" else role_count,
            minimum_dates=role_count if treatment == "time_trend" else 0,
            spoken_unit=True,
        )
    if (family, treatment) == ("evidence", "prediction"):
        return TreatmentPrerequisites(minimum_quantities=2, spoken_unit=True)
    if family == "chronology":
        return TreatmentPrerequisites(minimum_dates=role_count, date_per_role=True)
    return TreatmentPrerequisites()


def treatment_input_failure(
    family: str,
    treatment: str,
    role_texts: list[str],
    role_functions: list[str] | None = None,
) -> str | None:
    """Reject only prerequisites whose absence is observable in approved speech.

    Unit identity and factual comparability still require a chosen unit and
    values, checked by the later scene validator. Guessing units from adjacent
    nouns here would wrongly remove valid treatments.
    """
    rule = treatment_prerequisites(family, treatment, len(role_texts))
    speech = " ".join(role_texts)
    if len(spoken_number_mentions(speech)) < rule.minimum_quantities:
        return f"needs {rule.minimum_quantities} distinct spoken quantities"
    if rule.spoken_unit and not spoken_unit_candidates(speech):
        return "needs an explicit unit attached to a spoken quantity"
    if len(spoken_date_mentions(speech)) < rule.minimum_dates:
        return f"needs {rule.minimum_dates} spoken date expressions"
    if rule.date_per_role and any(not spoken_date_mentions(text) for text in role_texts):
        return "needs a spoken date in every reveal role"
    if role_functions and rule.role_shape == "state_progression" and (
        len(role_functions) < 2
        or any(role not in {"stage", "before", "after"} for role in role_functions)
    ):
        return "needs successive spoken states of one changing subject"
    if role_functions and rule.role_shape == "stages" and any(
        role not in {"stage", "input", "outcome", "cause", "mediator", "effect"}
        for role in role_functions
    ):
        return "needs successive spoken operating steps"
    return None


def treatment_plan_contract(family: str, treatment: str, role_count: int) -> dict:
    """Compact contract shared by the model menu and selection validator."""
    registered = REGISTRY[family]
    selected = next(item for item in registered.treatments if item.id == treatment)
    rule = treatment_prerequisites(family, treatment, role_count)
    slots = screen_slot_contract(family, treatment)
    return {
        "depicts": selected.composition,
        "avoid_when": registered.avoid_when,
        "required_facts": [
            fact for fact in registered.required_data
            if fact not in {"data source", "geography source"}
        ],
        "minimum_spoken_quantities": rule.minimum_quantities,
        "minimum_spoken_dates": rule.minimum_dates,
        "date_per_role": rule.date_per_role,
        "unit_must_be_spoken": rule.spoken_unit,
        "role_shape": rule.role_shape,
        "graphic_rule": rule.graphic_rule,
        "graphic_visibility_contract": (
            "For each graphic-capable cue, subject/state paints a verified subject "
            "graphic; none is an intentional text-led layout with no icon or "
            "placeholder. Never use none expecting a generic box to appear."
        ),
        "graphic_cue_indices": sorted(graphic_cue_indices(family, treatment, role_count)),
        "allowed_visual_changes": (
            ["fill_up", "fill_down", "expand", "contract", "highlight"]
            if rule.graphic_rule == "same_subject_progression" else []
        ),
        "slots": slots,
    }


@dataclass(frozen=True)
class FamilyCapability:
    id: str
    category: str
    purpose: str
    use_when: str
    avoid_when: str
    required_data: tuple[str, ...]
    asset_roles: tuple[str, ...]
    renderer: str
    treatments: tuple[TreatmentCapability, ...]
    version: int = 1

    def planner_description(self) -> dict:
        return {
            "family": self.id,
            "version": self.version,
            "purpose": self.purpose,
            "use_when": self.use_when,
            "avoid_when": self.avoid_when,
            "required_data": list(self.required_data),
            "asset_roles": list(self.asset_roles),
            "treatments": [
                {
                    "id": item.id,
                    "composition": item.composition,
                    "motion": item.motion,
                    "min_roles": role_capacity(self.id, item.id)[0],
                    "max_roles": role_capacity(self.id, item.id)[1],
                    "ordered_roles": treatment_role_pattern(self.id, item.id),
                    "screen_slots": screen_slot_contract(self.id, item.id),
                }
                for item in self.treatments
            ],
        }


def _t(id: str, composition: str, motion: str) -> TreatmentCapability:
    return TreatmentCapability(id, composition, motion)


def graphic_cue_indices(family: str, treatment: str, count: int) -> set[int]:
    """Cue positions whose current renderer actually paints a requested graphic."""
    if family in {"process", "causal", "journey", "scale_ladder", "evidence"}:
        return set(range(count))
    if family == "before_after":
        return set(range(count - 2, count))
    if family == "concept_contrast":
        return {0, 1}
    if family == "cutaway":
        return {count - 1} if treatment == "focal_inset" else set(range(count))
    if family in {"parallel", "synthesis"}:
        return set(range(count - 1))
    if family == "range":
        return {count - 1}
    if family == "claim_boundary":
        return {0}
    return set()


def screen_slot_contract(family: str, treatment: str) -> dict:
    """Visible copy the planner must own before semantic asset/layout fulfilment."""
    detail_families = {
        "process", "cutaway", "causal", "parallel", "chronology",
        "concept_contrast", "relationship",
    }
    supports_detail = family in detail_families or (
        family == "before_after" and treatment == "split"
    )
    allowed_special = {
        "before_after": ("change",),
        "document_focus": ("document_label",),
        "editorial_frame": ("chapter_label",),
    }.get(family, ())
    required_special = (
        ("chapter_label",) if (family, treatment) == ("editorial_frame", "chapter_marker")
        else allowed_special if family in {"before_after", "document_focus"} else ()
    )
    graphic_slots = {
        "process": "every reveal",
        "causal": "every reveal",
        "journey": "every reveal",
        "scale_ladder": "every reveal",
        "evidence": "every reveal",
        "before_after": "the final two reveals; not an optional opening cue",
        "concept_contrast": "the first two reveals; not a resolution cue",
        "cutaway": (
            "the feature reveal only" if treatment == "focal_inset" else "every reveal"
        ),
        "parallel": "every track reveal; not the final outcome",
        "synthesis": "every input reveal; not the final takeaway",
        "range": "the final qualifier reveal; not an optional opening cue",
        "claim_boundary": "the first claim reveal only",
    }.get(family, "none")
    return {
        "title_max_chars": 66,
        "min_reveals": role_capacity(family, treatment)[0],
        "max_reveals": role_capacity(family, treatment)[1],
        "reveal_labels": "exactly one per spoken renderer cue",
        "label_max_chars": 100 if (family, treatment) == (
            "editorial_close", "community_close"
        ) else {
            "editorial_frame": 66, "editorial_close": 34,
            "evidence": 48, "document_focus": 65,
            "cutaway": 31, "parallel": 34, "synthesis": 34,
            "range": 46, "claim_boundary": 34, "scenario_branch": 34,
        }.get(family, 34),
        # The claim uses the ordinary Step slot; the wider lower banner can
        # carry a longer spoken limit without changing labels in other scenes.
        "label_max_chars_by_position": (
            [34, 45] if (family, treatment) == ("claim_boundary", "bounded_statement")
            else []
        ),
        "supports_detail": supports_detail,
        "detail_target_chars": (
            45 if (family, treatment) == ("concept_contrast", "juxtaposition")
            else 42 if supports_detail else 0
        ),
        "detail_max_chars": (
            60 if (family, treatment) == ("concept_contrast", "juxtaposition")
            else 42 if supports_detail else 0
        ),
        "unit_max_chars": {
            "quantitative_display": 20,
            "scale_ladder": 18,
            "range": 25,
            "evidence": 16,
        }.get(family, 0),
        "date_max_chars": (
            24 if family == "chronology" else
            20 if (family, treatment) == ("quantitative_display", "time_trend") else 0
        ),
        "allowed_special": list(allowed_special),
        "required_special": list(required_special),
        "graphic_intent": "one subject/state intent or none per approved cue",
        "graphic_slots": graphic_slots,
    }


def reveal_label_limit(slots: dict, index: int) -> int:
    """Return the limit for one visible cue, including treatment-specific slots."""
    by_position = slots.get("label_max_chars_by_position", [])
    return by_position[index] if index < len(by_position) else slots["label_max_chars"]


FAMILIES = (
    FamilyCapability(
        "process",
        "explanation",
        "Show operating steps or a transformation.",
        "A narrated sequence has meaningful intermediate stages.",
        "The narration only names an outcome or an unproven mechanism.",
        ("ordered steps",),
        ("step illustration",),
        "_process",
        (
            _t("working", "Connected stages", "Activate successive parts"),
            _t(
                "transformation",
                "One subject changing through stages",
                "Shift emphasis across stages",
            ),
        ),
    ),
    FamilyCapability(
        "evidence",
        "explanation",
        "Contrast observations and the inference they support.",
        "Speech links observation to inference, compares rival explanations, "
        "or tests a prediction.",
        "Two ideas are merely different, or evidence is missing.",
        ("observation", "inference"),
        ("evidence symbol",),
        "_evidence",
        (
            _t("competing", "Two evidence panels", "Reveal the supported inference"),
            _t("prediction", "Prediction beside observation", "Expose their measured gap"),
            _t("signal_to_inference", "Observed clue connected to supported inference",
               "Reveal observation, then evidence-supported conclusion without inventing a trace"),
        ),
    ),
    FamilyCapability(
        "cutaway",
        "explanation",
        "Explain parts of a physical system.",
        "Part placement is supported or one feature can be magnified "
        "from a resolved subject asset.",
        "Spatial relationships would be invented.",
        ("subject", "parts"),
        ("subject illustration", "part icons"),
        "_cutaway",
        (
            _t("guided", "Subject with callouts", "Reveal anchored parts"),
            _t("exploded", "Separated components", "Connect parts in order"),
            _t("focal_inset", "Subject retained beside magnified feature",
               "Focus the one spoken feature after establishing the subject"),
        ),
    ),
    FamilyCapability(
        "range",
        "explanation",
        "Show a bounded estimate with its uncertainty.",
        "Low and high bounds share a unit and source.",
        "The bounds are not comparable or not evidenced.",
        ("low", "high", "unit"),
        ("context symbol",),
        "_range",
        (
            _t("scale", "Bounded axis", "Draw measured span"),
            _t("context", "Range within a context panel", "Reveal qualifier and values"),
        ),
    ),
    FamilyCapability(
        "causal",
        "explanation",
        "Explain a supported cause-to-effect chain.",
        "The evidence supports each link.",
        "The narration only suggests correlation or a possibility.",
        ("ordered causal stages",),
        ("stage symbols",),
        "_causal",
        (
            _t("chain", "Linear stages", "Advance causal arrows"),
            _t("mechanism", "Cause, mediator, effect", "Pulse through mechanism"),
        ),
    ),
    FamilyCapability(
        "before_after",
        "explanation",
        "Compare two states across a real change.",
        "A narrated subject or material has evidenced starting and resulting "
        "states after a change.",
        "The contrast is conceptual rather than temporal.",
        ("before", "after", "change"),
        ("state illustrations",),
        "_before_after",
        (
            _t("split", "Simultaneous states", "Reveal second state"),
            _t("wipe", "Shared frame", "Wipe into later state"),
        ),
    ),
    FamilyCapability(
        "parallel",
        "explanation",
        "Show simultaneous paths and their outcome.",
        "Two or three processes happen alongside one another.",
        "The items are a hierarchy or unrelated list.",
        ("parallel tracks", "outcome"),
        ("track symbols",),
        "_parallel",
        (
            _t("swimlanes", "Horizontal tracks", "Advance each track"),
            _t("converge", "Tracks meeting an outcome", "Draw convergence"),
        ),
    ),
    FamilyCapability(
        "journey",
        "explanation",
        "Show a schematic progression through places.",
        "Place order matters but exact geometry is not claimed.",
        "A geographically accurate map or region is required.",
        ("ordered places",),
        ("place symbols",),
        "_journey",
        (
            _t("locator", "Schematic place locator", "Reveal places"),
            _t("route", "Schematic route", "Trace narrated progression"),
        ),
    ),
    FamilyCapability(
        "scale_ladder",
        "explanation",
        "Compare ordered magnitudes on one scale.",
        "Values share a unit and increase in narrated order.",
        "Values have different denominators or units.",
        ("ordered values", "unit"),
        ("item symbols",),
        "_scale_ladder",
        (
            _t("bars", "Common-axis magnitude bars", "Grow each measured bar"),
            _t("zoom", "Successive magnitude focus", "Move up the scale"),
        ),
    ),
    FamilyCapability(
        "synthesis",
        "explanation",
        "Combine several ideas into a takeaway.",
        "The narration explicitly brings multiple supported threads together.",
        "The ideas have no supported combined conclusion.",
        ("inputs", "takeaway"),
        ("input symbols",),
        "_synthesis",
        (
            _t("mosaic", "Visible idea tiles", "Reveal collective takeaway"),
            _t("convergence", "Inputs around a center", "Draw toward takeaway"),
        ),
    ),
    FamilyCapability(
        "chronology",
        "explanation",
        "Present dated events without implying causation.",
        "Dates and their sequence are supported.",
        "The sequence is a process without meaningful dates.",
        ("dated events",),
        ("event icons",),
        "_chronology",
        (
            _t("event_track", "Persistent dated track", "Advance timeline marker"),
            _t("event_cards", "Dated event cards", "Bring each event forward"),
        ),
    ),
    FamilyCapability(
        "geography",
        "explanation",
        "Locate evidenced points, regions, or routes on a real map.",
        "Coordinates or bounds come from a recorded geographic source.",
        "A schematic journey is sufficient or geometry is unsupported.",
        ("coordinates or region bounds", "geography source"),
        ("Natural Earth land", "location markers"),
        "_geography",
        (
            _t("map_locator", "World-to-point locator", "Focus and mark location"),
            _t("region_focus", "World-to-region view", "Reveal bounded region"),
            _t("verified_route", "Mapped locations", "Trace evidenced route"),
        ),
    ),
    FamilyCapability(
        "concept_contrast",
        "explanation",
        "Distinguish concepts without implying rival evidence or time change.",
        "Narration compares two ideas, pairs complementary contributions, or corrects a claim.",
        "The claims are actually before/after states or rival evidence.",
        ("two concepts", "relationship"),
        ("concept symbols",),
        "_concept_contrast",
        (
            _t("juxtaposition", "Side-by-side concepts", "Reveal distinction"),
            _t(
                "paired_contributions", "Equally valid contributions", "Reveal each in speech order"
            ),
            _t("correction_reveal", "Incorrect claim and supported correction", "Mark correction"),
        ),
    ),
    FamilyCapability(
        "quantitative_display",
        "data",
        "Show sourced quantities with typed units.",
        "Values are spoken, sourced, and comparable for the chosen treatment.",
        "Values have mismatched units or a chart would imply nonexistent data.",
        ("values", "unit", "data source"),
        ("data marks",),
        "_quantitative_display",
        (
            _t("metric_reveal", "One large sourced value", "Reveal value with context"),
            _t("comparable_bars", "Shared-axis values", "Build measured bars"),
            _t("time_trend", "Dated series", "Draw observed trend"),
        ),
    ),
    FamilyCapability(
        "document_focus",
        "source",
        "Focus the audience on a documented finding.",
        "The spoken beat explicitly discusses a document, record, paper, or quoted "
        "excerpt as the visual subject.",
        "A written source merely supports narration but speech is not about that "
        "document; or a fabricated facsimile would imply an archival original.",
        ("document reference", "highlighted finding"),
        ("document image or schematic excerpt",),
        "_document_focus",
        (
            _t("guided_highlight", "Document with selected line", "Reveal highlight"),
            _t("extracted_finding", "Document beside finding", "Move focus to excerpt"),
        ),
    ),
    FamilyCapability(
        "relationship",
        "explanation",
        "Show structural links among people, objects, or institutions.",
        "The exact parent, connection, or nested-scope links are supported.",
        "The items are only a sequence or have unverified links.",
        ("nodes", "typed links"),
        ("node symbols",),
        "_relationship",
        (
            _t("hierarchy_build", "Parent and children", "Reveal branches"),
            _t("network_build", "Connected nodes", "Draw supported links"),
            _t("nested_scope", "Nested part, system, and wider whole",
               "Move emphasis outward in narrated order; geometry is schematic"),
        ),
    ),
    FamilyCapability(
        "claim_boundary", "explanation",
        "Distinguish an established proposition from its spoken limit.",
        "The narration states a supported claim and then qualifies its scope.",
        "The second idea is a rival claim, unrelated fact, or fabricated uncertainty.",
        ("supported claim", "spoken limitation"), ("optional claim symbols",),
        "_claim_boundary",
        (_t("bounded_statement", "Claim held beside its boundary",
            "Reveal the limit only when narration states it"),),
    ),
    FamilyCapability(
        "scenario_branch", "explanation",
        "Show conditional alternatives without encoding probabilities.",
        "The narration states a condition and two genuinely possible outcomes.",
        "The paths are simultaneous, certain, or unsupported alternatives.",
        ("spoken condition", "possible outcomes"), ("optional outcome symbols",),
        "_scenario_branch",
        (_t("conditional_fork", "One condition, two unweighted possibilities",
            "Reveal each possible outcome in spoken order"),),
    ),
    FamilyCapability(
        "editorial_frame",
        "editorial",
        "Frame a hook, claim, or chapter promise.",
        "The spoken line needs a memorable visual entry.",
        "A factual explanation needs a more specific family.",
        ("spoken headline",),
        ("optional background symbol",),
        "_editorial_frame",
        (
            _t("kinetic_question", "Question-led typography", "Build a question"),
            _t("statement_reveal", "Claim-led typography", "Reveal key statement"),
            _t("chapter_marker", "Chapter identity and promise", "Transition into chapter"),
        ),
    ),
    FamilyCapability(
        "editorial_close",
        "editorial",
        "Close with approved audience actions; legacy treatments can include a recap.",
        "All elements follow the approved ending narration.",
        "It is being used as a substitute for an unfinished explanation.",
        ("approved spoken closing roles",),
        ("optional brand marks",),
        "_editorial_close",
        (
            _t("recap_grid", "Recap cards and question", "Build toward invitation"),
            _t("loop_bridge", "Return to opening premise", "Bridge last thought to first"),
            _t(
                "community_close", "One branded audience close",
                "Reveal spoken actions in one frame",
            ),
        ),
    ),
)

REGISTRY = {family.id: family for family in FAMILIES}

# Spoken/on-screen explanatory roles, not an arbitrary number of visual beats.
# The maxima reflect the corresponding semantic scene's practical reveal slots.
ROLE_CAPACITY = {
    "process": (2, 4),
    "evidence": (2, 3),
    "cutaway": (2, 3),
    "range": (2, 3),
    "causal": (3, 4),
    "before_after": (2, 3),
    "parallel": (2, 4),
    "journey": (2, 4),
    "scale_ladder": (2, 3),
    # SynthesisScene requires 3–4 inputs followed by a takeaway.
    "synthesis": (4, 5),
    "chronology": (2, 4),
    "geography": (1, 4),
    "concept_contrast": (2, 2),
    "quantitative_display": (1, 5),
    "document_focus": (2, 2),
    "relationship": (3, 5),
    "claim_boundary": (2, 2),
    "scenario_branch": (3, 3),
    "editorial_frame": (1, 3),
    "editorial_close": (2, 6),
}
TREATMENT_ROLE_CAPACITY = {
    ("evidence", "competing"): (3, 3),
    ("evidence", "prediction"): (3, 3),
    ("evidence", "signal_to_inference"): (2, 2),
    ("cutaway", "focal_inset"): (2, 2),
    ("relationship", "nested_scope"): (3, 3),
    ("range", "scale"): (1, 2),
    ("range", "context"): (1, 2),
    ("parallel", "swimlanes"): (3, 4),
    ("parallel", "converge"): (3, 4),
    ("concept_contrast", "correction_reveal"): (3, 3),
    ("quantitative_display", "metric_reveal"): (1, 2),
    ("quantitative_display", "comparable_bars"): (2, 5),
    ("quantitative_display", "time_trend"): (2, 5),
    ("editorial_frame", "kinetic_question"): (1, 2),
    ("editorial_frame", "chapter_marker"): (1, 2),
    ("editorial_close", "loop_bridge"): (4, 4),
    ("editorial_close", "recap_grid"): (3, 6),
    ("editorial_close", "community_close"): (1, 4),
    ("geography", "map_locator"): (1, 2),
    ("geography", "region_focus"): (1, 2),
}

# Only treatments with non-interchangeable semantic slots need a typed role
# contract. Other families still reveal their labels in the spoken order.
TREATMENT_ROLE_PATTERNS = {
    ("evidence", "competing"): "expected, observed, inference",
    ("evidence", "prediction"): "expected, observed, inference",
    ("evidence", "signal_to_inference"): "observation, inference",
    ("cutaway", "focal_inset"): "subject, feature",
    ("relationship", "nested_scope"): "inner, system, wider",
    ("claim_boundary", "bounded_statement"): "claim, limit",
    ("scenario_branch", "conditional_fork"): "condition, possibility, possibility",
    ("before_after", "split"): "before, after",
    ("before_after", "wipe"): "before, after",
    ("causal", "mechanism"): "cause, mediator, effect",
    ("parallel", "swimlanes"): "track(s), outcome",
    ("parallel", "converge"): "track(s), outcome",
    ("synthesis", "mosaic"): "input(s), takeaway",
    ("synthesis", "convergence"): "input(s), takeaway",
    ("document_focus", "guided_highlight"): "excerpt, finding",
    ("document_focus", "extracted_finding"): "excerpt, finding",
}


_SPOKEN_TRANSFORMATION = re.compile(
    r"\b(?:convert(?:ed|s)?|transform(?:ed|s)?|turn(?:ed|s)?|"
    r"chang(?:ed|es)|becom(?:e|es)|became)\s+(?:\w+\s+){0,2}"
    r"(?:into|to)\b",
    re.IGNORECASE,
)
_NEGATED_TRANSFORMATION = re.compile(
    r"\b(?:not|never|cannot|can't|didn't|doesn't|wasn't|isn't)\b",
    re.IGNORECASE,
)


def transformation_role_match(role_texts: list[str], role_functions: list[str]) -> bool:
    """A narrated material change may have system/effect rather than before/after roles.

    This is only a candidate-menu exception. The selector must still judge whether
    the two states and change are faithfully representable in the chosen scene.
    """
    if len(role_texts) != 2 or role_functions not in (
        ["system", "effect"], ["subject", "effect"], ["input", "outcome"]
    ):
        return False
    for match in _SPOKEN_TRANSFORMATION.finditer(role_texts[1]):
        if not _NEGATED_TRANSFORMATION.search(
            role_texts[1][max(0, match.start() - 24):match.start()]
        ):
            return True
    return False


def treatment_roles_fit(
    family: str, treatment: str, ordered_roles: list[str],
    role_texts: list[str], role_functions: list[str],
) -> bool:
    if ordered_roles == ["spoken-order"] * len(role_functions):
        return True
    if ordered_roles == role_functions:
        return True
    return (
        (family, treatment) in {("before_after", "split"), ("before_after", "wipe")}
        and transformation_role_match(role_texts, role_functions)
    )


def treatment_role_pattern(family: str, treatment: str) -> str:
    return TREATMENT_ROLE_PATTERNS.get((family, treatment), "spoken-order reveals")


def treatment_role_sequence(family: str, treatment: str, count: int) -> list[str] | None:
    """Return required roles for a fixed-order treatment, or None when flexible."""
    if (family, treatment) not in TREATMENT_ROLE_PATTERNS:
        return None
    if family == "evidence" and count == 3:
        return ["expected", "observed", "inference"]
    if (family, treatment) == ("evidence", "signal_to_inference") and count == 2:
        return ["observation", "inference"]
    if (family, treatment) == ("cutaway", "focal_inset") and count == 2:
        return ["subject", "feature"]
    if (family, treatment) == ("relationship", "nested_scope") and count == 3:
        return ["inner", "system", "wider"]
    if family == "claim_boundary" and count == 2:
        return ["claim", "limit"]
    if family == "scenario_branch" and count == 3:
        return ["condition", "possibility", "possibility"]
    if family == "before_after" and count == 2:
        return ["before", "after"]
    if family == "causal" and count == 3:
        return ["cause", "mediator", "effect"]
    if (family == "parallel" and 3 <= count <= 4) or (
        family == "synthesis" and 4 <= count <= 5
    ):
        role = "track" if family == "parallel" else "input"
        return [role] * (count - 1) + ["outcome" if family == "parallel" else "takeaway"]
    if family == "document_focus" and count == 2:
        return ["excerpt", "finding"]
    raise ValueError(f"{family}/{treatment} needs a different number of spoken roles")


def role_capacity(family: str, treatment: str) -> tuple[int, int]:
    if family not in REGISTRY or treatment not in {item.id for item in REGISTRY[family].treatments}:
        raise ValueError(f"Unknown visual treatment: {family}/{treatment}")
    return TREATMENT_ROLE_CAPACITY.get((family, treatment), ROLE_CAPACITY[family])


# Compatibility audit only. New visual plans must name a concrete family.
LEGACY_DESIGNED_KINDS = {
    "title_reveal": ("editorial_frame",),
    "route_reveal": ("journey", "geography"),
    "comparison_build": ("quantitative_display",),
    "process_steps": ("process",),
    "narration_sequence": (
        "chronology",
        "process",
        "evidence",
        "relationship",
        "synthesis",
        "editorial_close",
    ),
    "document_highlight": ("document_focus",),
    "fact_reveal": ("editorial_frame",),
    "concept_contrast": ("concept_contrast",),
    "evidence_gap": ("evidence",),
    "relationship_map": ("relationship",),
    "branded_outro": ("editorial_close",),
    "semantic_diagram": tuple(REGISTRY),
}
LEGACY_MEDIA_KINDS = frozenset({"camera", "detail_reveal", "guided_image"})
LEGACY_VISUAL_TYPES = {
    "map": ("geography", "journey"),
    "timeline": ("chronology",),
    "comparison": ("concept_contrast", "evidence", "range", "parallel", "quantitative_display"),
    "data_chart": ("quantitative_display", "range", "scale_ladder"),
    "document": ("document_focus",),
    "kinetic_title": ("editorial_frame",),
    "chapter_card": ("editorial_frame",),
    "outro": ("editorial_close",),
}

# Only for loading old plans that predate explicit family choices. New live
# visual plans select a treatment from the catalog instead of relying on this.
LEGACY_DEFAULT_SELECTION = {
    "map": ("journey", "locator"),
    "timeline": ("chronology", "event_track"),
    "comparison": ("concept_contrast", "juxtaposition"),
    "data_chart": ("quantitative_display", "metric_reveal"),
    "document": ("document_focus", "guided_highlight"),
    "kinetic_title": ("editorial_frame", "statement_reveal"),
    "chapter_card": ("editorial_frame", "chapter_marker"),
    "outro": ("editorial_close", "recap_grid"),
}


def capability(family: str) -> FamilyCapability:
    return REGISTRY[family]


def planner_catalog() -> list[dict]:
    """Expose every treatment, not one arbitrary example per family."""
    return [family.planner_description() for family in FAMILIES]


_SPOKEN_DOCUMENT = re.compile(
    r"\b(?:document|notebook|manuscript|letter|diary|paper|article|report|"
    r"ledger|archive|excerpt|publication|published|wrote|written)\b",
    re.IGNORECASE,
)
_SPOKEN_EXPECTATION = re.compile(
    r"\b(?:if|would|should|expect\w*|predict\w*|baseline|hypothes\w*|"
    r"model|assum\w*|suppos\w*|only)\b",
    re.IGNORECASE,
)
_SPOKEN_CORRECTION = re.compile(
    r"\b(?:not|never|wrong|false|mistaken|misconception|myth|instead|rather)\b",
    re.IGNORECASE,
)


def eligible_treatments(
    spoken_text: str,
    role_count: int,
    *,
    first_role_text: str | None = None,
    role_texts: list[str] | None = None,
    role_functions: list[str] | None = None,
) -> list[dict]:
    """Executable menu for a proposed spoken beat; image-led remains a separate choice.

    This filters only hard registry/prerequisite mismatches. It deliberately does
    not infer story semantics from keywords or silently choose a treatment.
    """
    options = []
    for family in FAMILIES:
        if family.id == "document_focus" and not _SPOKEN_DOCUMENT.search(spoken_text):
            continue
        for treatment in family.treatments:
            minimum, maximum = role_capacity(family.id, treatment.id)
            if not minimum <= role_count <= maximum:
                continue
            if (family.id, treatment.id) == ("geography", "verified_route"):
                continue  # Requires reviewed geometry unavailable to this planner.
            if (
                family.id == "evidence"
                and treatment.id != "signal_to_inference"
                and first_role_text is not None
            ):
                if not _SPOKEN_EXPECTATION.search(first_role_text):
                    continue
            if (family.id, treatment.id) == ("concept_contrast", "correction_reveal"):
                if not _SPOKEN_CORRECTION.search(spoken_text):
                    continue
            try:
                ordered = treatment_role_sequence(family.id, treatment.id, role_count)
            except ValueError:
                continue
            if role_texts is not None and treatment_input_failure(
                family.id, treatment.id, role_texts, role_functions
            ):
                continue
            options.append(
                {
                    "family": family.id,
                    "treatment": treatment.id,
                    "role_count": role_count,
                    "ordered_roles": ordered or ["spoken-order"] * role_count,
                    "required_data": (
                        ["approved audience-action lines"]
                        if (family.id, treatment.id) == (
                            "editorial_close", "community_close"
                        ) else list(family.required_data)
                    ),
                }
            )
    return options


def eligible_treatments_for_boundary(spoken_text: str, unit_count: int) -> list[dict]:
    """Offer treatments before reveal groups exist, filtering only observable facts.

    Role-specific prerequisites are checked after visual selection has assigned
    consecutive speech to the chosen treatment's actual reveal slots.
    """
    quantities = len(spoken_number_mentions(spoken_text))
    dates = len(spoken_date_mentions(spoken_text))
    has_unit = bool(spoken_unit_candidates(spoken_text))
    options = []
    for family in FAMILIES:
        if family.id == "document_focus" and not _SPOKEN_DOCUMENT.search(spoken_text):
            continue
        for treatment in family.treatments:
            if (family.id, treatment.id) == ("geography", "verified_route"):
                continue
            if (family.id, treatment.id) == ("editorial_close", "community_close"):
                continue
            if family.id == "evidence" and treatment.id != "signal_to_inference":
                if not _SPOKEN_EXPECTATION.search(spoken_text):
                    continue
            if (family.id, treatment.id) == ("concept_contrast", "correction_reveal"):
                if not _SPOKEN_CORRECTION.search(spoken_text):
                    continue
            minimum, maximum = role_capacity(family.id, treatment.id)
            counts = []
            for count in range(minimum, min(maximum, unit_count) + 1):
                try:
                    treatment_role_sequence(family.id, treatment.id, count)
                except ValueError:
                    continue
                rule = treatment_prerequisites(family.id, treatment.id, count)
                if quantities < rule.minimum_quantities or dates < rule.minimum_dates:
                    continue
                if rule.spoken_unit and not has_unit:
                    continue
                counts.append(count)
            if counts:
                options.append({
                    "family": family.id,
                    "treatment": treatment.id,
                    "allowed_role_counts": counts,
                })
    return options
