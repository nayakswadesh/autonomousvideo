"""Compile approved long-form visual cues into a semantic renderer scene.

The model supplies only presentation details. Beat identity, approved wording,
spoken cue order, and measured timing remain owned by earlier workflow stages.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass

from autonomous_studio.rendering.semantic_assets import CODE_SYMBOLS, default_catalog
from autonomous_studio.rendering.semantic_diagrams import SCENE_MODELS
from autonomous_studio.rendering.visual_registry import (
    effective_design_treatment,
    screen_slot_contract,
)


@dataclass(frozen=True)
class CueSlot:
    path: tuple[str | int, ...]
    label_field: str
    anchor_field: str


@dataclass(frozen=True)
class SceneContract:
    family: str
    treatment: str
    cues: tuple[tuple[str, str], ...]
    slots: tuple[CueSlot, ...]
    opening: tuple[str, str] | None
    response_schema: dict

    def assemble(
        self, response: dict, *, beat: dict, narration: str, topic: str, duration: float
    ) -> dict:
        """Inject only approved/project-owned fields into a model's sparse scene."""
        data = deepcopy(response)
        blueprint = beat.get("screen_blueprint")
        forbidden = {
            "id", "topic", "narration", "duration", "provenance", "family", "treatment",
            "opening_at",
        }
        if beat.get("screen_title"):
            forbidden.add("title")
        if self.opening:
            forbidden.update({"opening_label", "opening_anchor"})
        if blueprint is not None:
            forbidden.update({"title", "opening_label", "opening_anchor", "change",
                              "document_label", "chapter_label"})
            if (self.family, self.treatment) in {
                ("concept_contrast", "juxtaposition"),
                ("concept_contrast", "paired_contributions"),
            }:
                forbidden.add("resolution")
            if (self.family, self.treatment) == ("editorial_close", "recap_grid"):
                forbidden.add("bridge")
            if self.family == "evidence" and self.treatment != "prediction":
                forbidden.add("unit")
            if _contains_key(data, "detail"):
                raise ValueError("semantic response supplied unapproved supporting copy")
            if _contains_key(data, "state_visual"):
                raise ValueError("semantic response supplied unapproved graphic state")
            if _contains_key(data, "graphic_visible"):
                raise ValueError("semantic response supplied project-owned graphic visibility")
            if self.family == "quantitative_display" and self.treatment != "time_trend":
                if _contains_key(data, "date"):
                    raise ValueError("semantic response supplied unapproved trend date")
            if self.treatment == "community_close" and _contains_key(data, "role"):
                raise ValueError("semantic response supplied an unapproved audience action")
            if self.treatment == "community_close":
                forbidden.update({
                    "recap", "question", "question_anchor", "question_at",
                    "invitation", "invitation_anchor", "invitation_at",
                    "bridge", "bridge_anchor", "bridge_at", "end_screen_side",
                    "response_options",
                    "response_option_ats",
                })
        if (self.family, self.treatment) == ("evidence", "signal_to_inference"):
            forbidden.update({"expected", "expected_anchor", "expected_at", "expected_value",
                              "expected_asset_concept", "observed_value", "unit"})
        if forbidden.intersection(data):
            raise ValueError("semantic response supplied project-owned scene fields")
        data.update(
            id=beat["id"],
            topic=topic[:65],
            narration=narration,
            duration=duration,
            provenance="illustrative",
            family=self.family,
            treatment=self.treatment,
            opening_at=0.0,
        )
        if beat.get("screen_title"):
            data["title"] = beat["screen_title"]
        if self.opening:
            data["opening_label"], data["opening_anchor"] = self.opening
        elif blueprint is not None:
            data.update(opening_label="", opening_anchor="")
        if blueprint is not None:
            data["title"] = beat["screen_title"]
            data.update(blueprint["special_copy"])
            if self.treatment == "community_close":
                data["end_screen_side"] = blueprint.get("end_screen_side", "right")
                data["response_options"] = blueprint.get("community_options", [])
            if self.family == "editorial_frame":
                data.setdefault("chapter_label", "")
            if (self.opening and screen_slot_contract(self.family, self.treatment)[
                "supports_detail"
            ] and blueprint["supporting_copy"][0]):
                raise ValueError("opening cue has no supporting-copy slot")
        for name in {
            slot.path[0]
            for slot in self.slots
            if slot.path and isinstance(slot.path[-1], int)
        }:
            required = sum(bool(slot.path) and slot.path[0] == name for slot in self.slots)
            if not isinstance(data.get(name), list) or len(data[name]) != required:
                raise ValueError(
                    f"semantic response changed the approved cue count for {name}: "
                    f"expected {required}"
                )
        for cue_index, (slot, (label, anchor)) in enumerate(
            zip(self.slots, self.cues, strict=True), start=bool(self.opening)
        ):
            node = data
            try:
                for part in slot.path:
                    node = node[part]
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError(
                    f"semantic scene is missing a planned cue slot: {slot.path}"
                ) from exc
            if slot.label_field in node or slot.anchor_field in node or "at" in node:
                raise ValueError("semantic response supplied project-owned cue fields")
            if (
                blueprint is None and self.family == "process" and len(label) > 34
                and slot.label_field == "label"
            ):
                # Older approved process cues can exceed the narrow step label.
                # Preserve every word by moving the overflow to the rendered
                # detail line; do not ask the model to paraphrase the cue.
                split = label.rfind(" ", 1, 35)
                if split < 2 or len(label[split + 1 :]) > 42:
                    raise ValueError("approved process cue cannot fit without rewriting")
                if "detail" not in node:
                    raise ValueError("process step lacks a detail line for local cue wrap")
                node["detail"] = label[split + 1 :]
                label = label[:split]
            node[slot.label_field] = label
            node[slot.anchor_field] = anchor
            if blueprint is not None:
                slots = screen_slot_contract(self.family, self.treatment)
                intent = blueprint["graphic_intents"][cue_index]
                if isinstance(node, dict) and "symbol" in node:
                    node["graphic_visible"] = intent["mode"] != "none"
                if slots["supports_detail"]:
                    support = blueprint["supporting_copy"][cue_index]
                    if support and not slot.path:
                        raise ValueError("supporting copy has no renderer text box")
                    if slot.path:
                        node["detail"] = support
                if self.family == "process":
                    node["state_visual"] = blueprint["graphic_intents"][cue_index][
                        "visual_change"
                    ]
                if self.treatment == "community_close":
                    node["role"] = blueprint["community_roles"][cue_index]
        if (self.family, self.treatment) == ("cutaway", "focal_inset"):
            # The feature is a crop of the base illustration, not a second asset.
            # Coordinates are located against the resolved illustration later.
            data["parts"][0].update(
                symbol="box", asset_concept=None, x=0.5, y=0.5
            )
        if (self.family, self.treatment) == ("relationship", "nested_scope"):
            for index, node in enumerate(data["nodes"]):
                if "id" in node or "parent_id" in node:
                    raise ValueError("nested scope links come from the approved role order")
                node["id"] = ("inner", "system", "wider")[index]
                node["parent_id"] = ("system", "wider", None)[index]
        approved_data = beat.get("visual_data")
        if approved_data is not None:
            values = approved_data.get("values", [])
            dates = approved_data.get("dates", [])
            unit = approved_data.get("unit", "")
            if self.family in {"quantitative_display", "scale_ladder"}:
                items = data["points" if self.family == "quantitative_display" else "items"]
                if len(values) != len(items):
                    raise ValueError("approved numeric values disagree with renderer cue count")
                if "unit" in data or any("value" in item for item in items):
                    raise ValueError("semantic response supplied approved numeric data")
                data["unit"] = unit
                for item, value in zip(items, values, strict=True):
                    item["value"] = value
                if self.family == "quantitative_display" and self.treatment == "time_trend":
                    if len(dates) != len(items):
                        raise ValueError("approved trend dates disagree with cue count")
                    if any("date" in item for item in items):
                        raise ValueError("semantic response supplied approved trend dates")
                    for item, date in zip(items, dates, strict=True):
                        item["date"] = date
            elif self.family == "range":
                if any(name in data for name in ("low", "high", "unit")):
                    raise ValueError("semantic response supplied approved range data")
                data.update(low=values[0], high=values[1], unit=unit)
            elif (self.family, self.treatment) == ("evidence", "prediction"):
                if any(name in data for name in ("expected_value", "observed_value", "unit")):
                    raise ValueError("semantic response supplied approved prediction data")
                data.update(expected_value=values[0], observed_value=values[1], unit=unit)
            elif self.family == "chronology":
                if len(dates) != len(data["events"]):
                    raise ValueError("approved timeline dates disagree with cue count")
                if any("date" in item for item in data["events"]):
                    raise ValueError("semantic response supplied approved timeline dates")
                for item, date in zip(data["events"], dates, strict=True):
                    item["date"] = date
        return data


def bind_required_graphic_assets(
    payload: dict, beat: dict, contract: SceneContract
) -> tuple[dict, list[dict]]:
    """Fulfil approved graphic subjects with exact local assets or budgeted cutouts.

    The sparse semantic response controls composition, not whether a required
    graphic is allowed to degrade to a neutral box. Resolve from the approved
    blueprint so equivalent model wording cannot change procurement behavior.
    """
    blueprint = beat.get("screen_blueprint")
    if not blueprint:
        return payload, []
    result = deepcopy(payload)
    catalog = default_catalog()
    decisions = []

    if (contract.family, contract.treatment) == ("cutaway", "guided"):
        base = result.get("base_asset_concept")
        base_choice = catalog.select(base) if base else None
        if not base_choice or base_choice.match != "exact":
            symbol = result.get("symbol", "")
            choice = catalog.select(symbol) if symbol else None
            if choice and choice.match == "exact" and catalog.get(choice.asset_id).status == "ready":
                result["base_asset_concept"] = symbol
                decisions.append({"requested": symbol, "asset_id": choice.asset_id,
                                  "reason": "exact local guided base"})

    for index, slot in enumerate(contract.slots, start=bool(contract.opening)):
        intent = blueprint["graphic_intents"][index]
        if intent["mode"] == "none" or (
            contract.family, contract.treatment
        ) == ("cutaway", "focal_inset"):
            continue
        node = result
        for part in slot.path:
            node = node[part]
        if contract.family == "evidence" and not slot.path:
            concept_key = f"{slot.label_field}_asset_concept"
        elif isinstance(node, dict) and "symbol" in node:
            concept_key = "asset_concept"
        else:
            raise ValueError(f"{beat['id']} graphic intent has no renderer asset slot")
        subject = intent["subject"].strip()
        if not subject:
            raise ValueError(f"{beat['id']} required graphic has no approved subject")
        state_needs_asset = intent["mode"] == "state" and (
            intent.get("visual_change", "none") == "none"
        )
        if subject in CODE_SYMBOLS and not state_needs_asset and "symbol" in node:
            node["symbol"], node[concept_key] = subject, None
            asset_id = f"code/{subject}"
            reason = "exact code graphic"
        else:
            choice = catalog.select(subject)
            if (not state_needs_asset and choice.match == "exact"
                    and catalog.get(choice.asset_id).status == "ready"):
                node[concept_key] = subject
                asset_id = choice.asset_id
                reason = "exact local graphic"
            else:
                description = subject
                if state_needs_asset:
                    description = f"{subject}, {intent['state']}"
                node[concept_key] = f"illustrate:{description}"
                asset_id = "pending/generated"
                reason = "required graphic has no suitable local asset"
            if "symbol" in node:
                node["symbol"] = "box"
        decisions.append({"requested": subject, "asset_id": asset_id, "reason": reason})
    return result, decisions


def _slot(path, label="label", anchor="anchor") -> CueSlot:
    return CueSlot(tuple(path), label, anchor)


def _array(name: str, count: int) -> list[CueSlot]:
    return [_slot((name, index)) for index in range(count)]


def _cue_slots(family: str, treatment: str, count: int) -> tuple[list[CueSlot], bool]:
    """Map approved on-screen cues to actual renderer reveal slots.

    A few treatments have one additional introductory cue. It is explicitly
    represented as opening context, never silently added by the model.
    """
    opening = False
    if (family, treatment) in {
        ("quantitative_display", "metric_reveal"),
        ("geography", "map_locator"),
        ("geography", "region_focus"),
        ("cutaway", "focal_inset"),
    } or family in {"range", "before_after"}:
        required = 2 if family == "before_after" else 1
        if count == required + 1:
            opening = True
            count -= 1
    if (family, treatment) == ("cutaway", "focal_inset") and not opening:
        raise ValueError("focal inset needs a spoken subject and one feature")
    if family in {
        "process", "cutaway", "causal", "journey", "scale_ladder",
        "chronology", "quantitative_display", "relationship", "editorial_frame",
    }:
        name = {
            "process": "steps", "cutaway": "parts", "causal": "stages",
            "journey": "places", "scale_ladder": "items", "chronology": "events",
            "quantitative_display": "points", "relationship": "nodes",
            "editorial_frame": "lines",
        }[family]
        slots = _array(name, count)
    elif family == "evidence":
        slots = (
            [_slot((), "observed", "observed_anchor"),
             _slot((), "inference", "inference_anchor")]
            if treatment == "signal_to_inference"
            else [_slot((), "expected", "expected_anchor"),
                  _slot((), "observed", "observed_anchor"),
                  _slot((), "inference", "inference_anchor")]
        )
    elif family == "range":
        slots = [_slot((), "qualifier", "anchor")]
    elif family == "before_after":
        slots = [_slot(("before",)), _slot(("after",))]
    elif family == "parallel":
        slots = _array("tracks", count - 1) + [_slot((), "outcome", "outcome_anchor")]
    elif family == "synthesis":
        slots = _array("inputs", count - 1) + [_slot((), "takeaway", "takeaway_anchor")]
    elif family == "geography":
        slots = (
            [_slot(("region",))]
            if treatment == "region_focus"
            else _array("places", count)
        )
    elif family == "concept_contrast":
        slots = [_slot(("left",)), _slot(("right",))]
        if treatment == "correction_reveal":
            slots.append(_slot((), "resolution", "resolution_anchor"))
    elif family == "document_focus":
        slots = [
            _slot((), "excerpt", "excerpt_anchor"),
            _slot((), "finding", "finding_anchor"),
        ]
    elif family == "editorial_close":
        if treatment == "community_close":
            slots = _array("actions", count)
        else:
            suffix = 3 if treatment == "loop_bridge" else 2
            slots = _array("recap", count - suffix) + [
                _slot((), "question", "question_anchor"),
                _slot((), "invitation", "invitation_anchor"),
            ]
            if treatment == "loop_bridge":
                slots.append(_slot((), "bridge", "bridge_anchor"))
    elif family == "claim_boundary":
        slots = [_slot(("claim",)), _slot(("limit",))]
    elif family == "scenario_branch":
        slots = [_slot(("condition",)), _slot(("first_outcome",)),
                 _slot(("second_outcome",))]
    else:
        raise ValueError(f"unregistered semantic family: {family}")
    if len(slots) != count:
        raise ValueError(
            f"{family}/{treatment} has {len(slots)} renderer cues but the approved "
            f"visual plan has {count}; replan the beat instead of inventing cues"
        )
    return slots, opening


def _remove_property(node: dict, name: str) -> None:
    node.get("properties", {}).pop(name, None)
    if "required" in node:
        node["required"] = [item for item in node["required"] if item != name]


def _contains_key(value, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(child, key) for child in value.values())
    if isinstance(value, list):
        return any(_contains_key(child, key) for child in value)
    return False


def _strip_nested_cues(node):
    if isinstance(node, list):
        for item in node:
            _strip_nested_cues(item)
    elif isinstance(node, dict):
        if node.get("type") == "object":
            for name in ("label", "anchor", "at"):
                _remove_property(node, name)
        for child in node.values():
            _strip_nested_cues(child)


def build_scene_contract(beat: dict, family: str, treatment: str) -> SceneContract:
    labels = beat.get("overlay_text") or []
    anchors = beat.get("overlay_anchors") or []
    if not labels or len(labels) != len(anchors):
        raise ValueError(f"{beat['id']} needs paired approved overlay labels and anchors")
    slots, uses_opening = _cue_slots(family, treatment, len(labels))
    if uses_opening:
        opening = (labels[0], anchors[0])
        cues = list(zip(labels[1:], anchors[1:], strict=True))
    else:
        opening = None
        cues = list(zip(labels, anchors, strict=True))
    schema = deepcopy(SCENE_MODELS[family].model_json_schema())
    for name in (
        "id", "topic", "narration", "duration", "provenance", "family",
        "treatment", "opening_at",
    ):
        _remove_property(schema, name)
    if (family, treatment) == ("evidence", "signal_to_inference"):
        for name in ("expected", "expected_anchor", "expected_at", "expected_value",
                     "expected_asset_concept", "observed_value", "unit"):
            _remove_property(schema, name)
    if (family, treatment) == ("relationship", "nested_scope"):
        for name in ("id", "parent_id"):
            _remove_property(schema["$defs"]["RelationshipNode"], name)
    if (family, treatment) == ("cutaway", "focal_inset"):
        for name in ("symbol", "asset_concept", "x", "y"):
            _remove_property(schema["$defs"]["Part"], name)
    if beat.get("screen_title"):
        _remove_property(schema, "title")
    if beat.get("screen_blueprint") is not None:
        for name in ("title", "opening_label", "opening_anchor", "change",
                     "document_label", "chapter_label"):
            _remove_property(schema, name)
        _strip_nested_detail(schema.get("$defs", {}))
        _strip_nested_detail(schema.get("properties", {}))
        _strip_nested_property(schema.get("$defs", {}), "state_visual")
        _strip_nested_property(schema.get("properties", {}), "state_visual")
        _strip_nested_property(schema.get("$defs", {}), "graphic_visible")
        _strip_nested_property(schema.get("properties", {}), "graphic_visible")
        if (family, treatment) == ("editorial_close", "community_close"):
            _strip_nested_property(schema.get("$defs", {}), "role")
        if family == "evidence" and treatment != "prediction":
            _remove_property(schema, "unit")
        if family == "concept_contrast" and treatment != "correction_reveal":
            _remove_property(schema, "resolution")
        if (family, treatment) == ("editorial_close", "recap_grid"):
            _remove_property(schema, "bridge")
        if (family, treatment) == ("editorial_close", "community_close"):
            for name in (
                "recap", "question", "question_anchor", "question_at",
                "invitation", "invitation_anchor", "invitation_at",
                "bridge", "bridge_anchor", "bridge_at", "end_screen_side",
                "response_options",
                "response_option_ats",
            ):
                _remove_property(schema, name)
        if family == "quantitative_display" and treatment != "time_trend":
            _remove_property(schema["$defs"]["QuantPoint"], "date")
    if opening:
        _remove_property(schema, "opening_label")
        _remove_property(schema, "opening_anchor")
    # These slots are supplied from approved reveal cues, not model prose.
    for slot in slots:
        if not slot.path:
            _remove_property(schema, slot.label_field)
            _remove_property(schema, slot.anchor_field)
    for name in (
        "expected_at", "reveal_at", "inference_at", "outcome_at",
        "takeaway_at", "resolution_at", "excerpt_at", "finding_at",
        "question_at", "invitation_at", "bridge_at",
    ):
        _remove_property(schema, name)
    _strip_nested_cues(schema.get("$defs", {}))
    _strip_nested_cues(schema.get("properties", {}))
    if beat.get("visual_data") is not None:
        if family in {"quantitative_display", "scale_ladder", "range"} or (
            family, treatment
        ) == ("evidence", "prediction"):
            _remove_property(schema, "unit")
        if family == "range":
            _remove_property(schema, "low")
            _remove_property(schema, "high")
        elif (family, treatment) == ("evidence", "prediction"):
            _remove_property(schema, "expected_value")
            _remove_property(schema, "observed_value")
        elif family in {"quantitative_display", "scale_ladder"}:
            definition = "QuantPoint" if family == "quantitative_display" else "ScaleItem"
            _remove_property(schema["$defs"][definition], "value")
            if treatment == "time_trend":
                _remove_property(schema["$defs"][definition], "date")
        elif family == "chronology":
            _remove_property(schema["$defs"]["ChronologyEvent"], "date")
    # A role list has exactly the approved number of cues, never a model-chosen
    # count. The schema makes this visible before response validation as well.
    arrays = {slot.path[0] for slot in slots if slot.path and isinstance(slot.path[-1], int)}
    for name in arrays:
        count = sum(bool(slot.path) and slot.path[0] == name for slot in slots)
        entry = schema["properties"][name]
        entry["minItems"] = entry["maxItems"] = count
    return SceneContract(family, treatment, tuple(cues), tuple(slots), opening, schema)


def apply_approved_graphic_visibility(beat: dict, scene):
    """Upgrade a saved scene's presentation from its unchanged approved blueprint.

    This is deliberately opt-in for a versioned rebuild: loading an old project
    must not silently change what its already reviewed video shows.
    """
    blueprint = beat.get("screen_blueprint")
    if blueprint is None:
        return scene
    contract = build_scene_contract(
        beat, beat["design_family"], effective_design_treatment(beat)
    )
    data = scene.model_dump(mode="json")
    for index, slot in enumerate(contract.slots, start=bool(contract.opening)):
        node = data
        for part in slot.path:
            node = node[part]
        if not isinstance(node, dict) or "symbol" not in node:
            continue
        intent = blueprint["graphic_intents"][index]
        if intent["mode"] == "none" and (
            node.get("asset_concept") or node.get("symbol") not in {"box", None}
        ):
            raise ValueError(f"{beat['id']} saved scene added an unapproved graphic")
        node["graphic_visible"] = intent["mode"] != "none"
    updated = SCENE_MODELS[scene.family].model_validate(data)
    validate_scene_fidelity(beat, updated)
    return updated


def _strip_nested_detail(node) -> None:
    if isinstance(node, list):
        for child in node:
            _strip_nested_detail(child)
    elif isinstance(node, dict):
        if node.get("type") == "object":
            _remove_property(node, "detail")
        for child in node.values():
            _strip_nested_detail(child)


def _strip_nested_property(node, field: str) -> None:
    if isinstance(node, list):
        for child in node:
            _strip_nested_property(child, field)
    elif isinstance(node, dict):
        if node.get("type") == "object":
            _remove_property(node, field)
        for child in node.values():
            _strip_nested_property(child, field)


def validate_scene_fidelity(beat: dict, scene) -> None:
    """Reject stale or model-expanded saved scenes before they enter production."""
    blueprint = beat.get("screen_blueprint")
    if blueprint is None:
        return  # Already approved legacy projects retain their saved contract.
    family = beat["design_family"]
    treatment = effective_design_treatment(beat)
    contract = build_scene_contract(beat, family, treatment)
    supports_detail = screen_slot_contract(family, treatment)["supports_detail"]
    data = scene.model_dump(mode="json")
    if (scene.family, scene.treatment) != (family, treatment):
        raise ValueError("scene changed the approved family or treatment")
    if family != "process" and _nondefault_field(data, "state_visual", "none"):
        raise ValueError("scene added an unapproved graphic state")
    if data["title"] != beat["screen_title"]:
        raise ValueError("scene changed the approved screen title")
    if (data["opening_label"], data["opening_anchor"]) != (contract.opening or ("", "")):
        raise ValueError("scene added or changed an opening cue")
    for name, expected in blueprint["special_copy"].items():
        if data.get(name) != expected:
            raise ValueError(f"scene changed approved {name}")
    for name in ("change", "document_label", "chapter_label"):
        if name not in blueprint["special_copy"] and data.get(name):
            raise ValueError(f"scene added unapproved {name}")
    if family == "concept_contrast" and treatment != "correction_reveal" and (
        data.get("resolution")
    ):
        raise ValueError("scene added an unapproved resolution")
    if (family, treatment) == ("editorial_close", "community_close"):
        if data.get("recap") or data.get("question") or data.get("invitation"):
            raise ValueError("community close added unapproved recap or invitation")
        if data.get("end_screen_side") != blueprint.get("end_screen_side", "right"):
            raise ValueError("community close changed its approved end-screen safe side")
        if data.get("response_options", []) != blueprint.get("community_options", []):
            raise ValueError("community close changed approved response choices")
    if (family, treatment) == ("editorial_close", "recap_grid") and data.get("bridge"):
        raise ValueError("scene added an unapproved bridge")
    if family == "evidence" and treatment != "prediction" and data.get("unit"):
        raise ValueError("scene added an unapproved unit")
    if family == "quantitative_display" and treatment != "time_trend" and any(
        point.get("date") for point in data.get("points", [])
    ):
        raise ValueError("scene added unapproved trend dates")
    arrays = {slot.path[0] for slot in contract.slots
              if slot.path and isinstance(slot.path[-1], int)}
    for name in arrays:
        expected = sum(
            1 for slot in contract.slots
            if slot.path and isinstance(slot.path[-1], int) and slot.path[0] == name
        )
        if len(data[name]) != expected:
            raise ValueError(f"scene changed approved {name} cue count")
    for cue_index, (slot, (label, anchor)) in enumerate(
        zip(contract.slots, contract.cues, strict=True), start=bool(contract.opening)
    ):
        node = data
        for part in slot.path:
            node = node[part]
        if node[slot.label_field] != label or node[slot.anchor_field] != anchor:
            raise ValueError("scene changed approved reveal copy or spoken anchor")
        if supports_detail and slot.path and "detail" in node:
            if node["detail"] != blueprint["supporting_copy"][cue_index]:
                raise ValueError("scene changed approved supporting copy")
        if family == "process" and slot.path:
            if node["state_visual"] != blueprint["graphic_intents"][cue_index][
                "visual_change"
            ]:
                raise ValueError("scene changed approved graphic state")
        if isinstance(node, dict) and "symbol" in node and node.get("graphic_visible") != (
            blueprint["graphic_intents"][cue_index]["mode"] != "none"
        ):
            raise ValueError("scene changed approved graphic visibility")
        if (family, treatment) == ("editorial_close", "community_close"):
            if node["role"] != blueprint["community_roles"][cue_index]:
                raise ValueError("scene changed approved audience action")
    approved_data = beat.get("visual_data")
    if approved_data is not None:
        values = approved_data.get("values", [])
        dates = approved_data.get("dates", [])
        unit = approved_data.get("unit", "")
        if family in {"quantitative_display", "scale_ladder"}:
            items = data["points" if family == "quantitative_display" else "items"]
            if [item["value"] for item in items] != values or data["unit"] != unit:
                raise ValueError("scene changed approved numeric visual data")
            if family == "quantitative_display" and treatment == "time_trend" and (
                [item["date"] for item in items] != dates
            ):
                raise ValueError("scene changed approved trend dates")
        elif family == "range" and (
            [data["low"], data["high"]] != values or data["unit"] != unit
        ):
            raise ValueError("scene changed approved range data")
        elif (family, treatment) == ("evidence", "prediction") and (
            [data["expected_value"], data["observed_value"]] != values
            or data["unit"] != unit
        ):
            raise ValueError("scene changed approved prediction data")
        elif family == "chronology" and [item["date"] for item in data["events"]] != dates:
            raise ValueError("scene changed approved chronology dates")
    _validate_graphic_intents(beat, data, contract)


def _nondefault_field(node, field: str, default) -> bool:
    if isinstance(node, dict):
        return (field in node and node[field] != default) or any(
            _nondefault_field(child, field, default) for child in node.values()
        )
    if isinstance(node, list):
        return any(_nondefault_field(child, field, default) for child in node)
    return False


def _concept_words(value: str) -> set[str]:
    return {
        word for word in re.findall(r"[a-z0-9]+", value.casefold())
        if word not in {"a", "an", "the", "of", "in", "on", "for", "and", "illustrate"}
    }


def _validate_graphic_intents(beat: dict, scene: dict, contract: SceneContract) -> None:
    """An installed subject icon does not silently stand in for a changed state."""
    blueprint = beat["screen_blueprint"]
    catalog = default_catalog()
    prior_state: tuple[str, str, str, str] | None = None
    for index, slot in enumerate(contract.slots, start=bool(contract.opening)):
        intent = blueprint["graphic_intents"][index]
        if (contract.family, contract.treatment) == ("cutaway", "focal_inset"):
            # The approved feature is located within the one base image. The
            # part has no independently rendered graphic to validate.
            continue
        node = scene
        for part in slot.path:
            node = node[part]
        if not isinstance(node, dict):
            if intent["mode"] != "none":
                raise ValueError(f"{beat['id']} graphic intent has no renderer asset slot")
            continue
        if contract.family == "evidence" and not slot.path:
            concept = node.get(f"{slot.label_field}_asset_concept")
            symbol = "box"
        elif "symbol" in node:
            concept = node.get("asset_concept")
            symbol = node.get("symbol")
        else:
            if intent["mode"] != "none":
                raise ValueError(f"{beat['id']} graphic intent has no renderer asset slot")
            continue
        if intent["mode"] == "none":
            if concept or symbol not in {"box", None}:
                raise ValueError(f"{beat['id']} added an unplanned graphic")
            continue
        if not concept:
            if _concept_words(intent["subject"]) != _concept_words(str(symbol)):
                raise ValueError(f"{beat['id']} lacks the planned graphic subject")
            asset_id = f"code/{symbol}"
            record = catalog.get(asset_id)
        else:
            choice = catalog.select(concept)
            if choice.match != "exact":
                raise ValueError(f"{beat['id']} has no exact asset for {intent['subject']}")
            asset_id = choice.asset_id
            record = catalog.get(asset_id)
        described = " ".join((record.label, *record.concepts, str(concept or "")))
        # A generated asset is already tied to its receipted image request.
        # Word overlap between that request and the approved subject is not a
        # reliable check of what the resulting illustration depicts.
        if record.kind != "generated" and not (
            _concept_words(intent["subject"]) <= _concept_words(described)
        ):
            raise ValueError(f"{beat['id']} asset does not depict the planned subject")
        if intent["mode"] == "state":
            state_visual = node.get("state_visual", "none")
            if state_visual == "none" and (
                record.kind != "generated"
                or not _concept_words(intent["state"]) <= _concept_words(described)
            ):
                raise ValueError(f"{beat['id']} generic icon does not depict planned state")
            signature = (
                intent["subject"].casefold(), asset_id, state_visual,
                intent["state"].casefold(),
            )
            if prior_state is not None and signature[:3] == prior_state[:3] and (
                signature[3] != prior_state[3]
            ):
                raise ValueError(f"{beat['id']} repeats one graphic for distinct states")
            prior_state = signature
