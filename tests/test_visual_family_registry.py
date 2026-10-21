"""Every registered family/treatment is a validated, renderable capability."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from PIL import ImageChops
from pydantic import TypeAdapter, ValidationError

from autonomous_studio.domain.motion import MotionShot
from autonomous_studio.rendering.semantic_assets import detailed_subject_asset
from autonomous_studio.rendering.semantic_diagrams import (
    SCENE_MODELS,
    Scene,
    render_frame,
    scene_asset_report,
    scene_cues,
    validate_scene_layout,
)
from autonomous_studio.rendering.visual_registry import (
    FAMILIES,
    LEGACY_DESIGNED_KINDS,
    LEGACY_MEDIA_KINDS,
    LEGACY_VISUAL_TYPES,
    REGISTRY,
    effective_design_treatment,
    eligible_treatments,
    planner_catalog,
)
from autonomous_studio.workflow.semantic_integration import (
    _set_relative_times,
    align_scene_to_measured_speech,
    normalize_scene_time_placeholders,
)
from autonomous_studio.workflow.semantic_scene_contract import _cue_slots, build_scene_contract

ADAPTER = TypeAdapter(Scene)
ROOT = Path(__file__).resolve().parents[1]


def _sparse_response(value, schema, definitions):
    if "$ref" in schema:
        schema = definitions[schema["$ref"].split("/")[-1]]
    if "anyOf" in schema:
        variant = next(
            (item for item in schema["anyOf"] if item.get("type") != "null"),
            schema["anyOf"][0],
        )
        return _sparse_response(value, variant, definitions)
    if isinstance(value, dict) and schema.get("type") == "object":
        return {
            name: _sparse_response(value[name], child, definitions)
            for name, child in schema.get("properties", {}).items()
            if name in value
        }
    if isinstance(value, list) and schema.get("type") == "array":
        return [_sparse_response(item, schema["items"], definitions) for item in value]
    return value


def test_approved_cues_compile_into_every_registered_family():
    studies = new_scene_examples()
    studies += json.loads(
        (ROOT / "examples/semantic_asset_studies.json").read_text(encoding="utf-8")
    )
    studies += json.loads(
        (ROOT / "examples/semantic_extended_studies.json").read_text(encoding="utf-8")
    )
    assert {scene["family"] for scene in studies} == set(REGISTRY)
    for source in studies:
        if (source["family"], source["treatment"]) == ("geography", "verified_route"):
            continue  # New visual plans cannot self-certify route geometry.
        scene = ADAPTER.validate_python(source)
        cues = scene_cues(scene)
        slots, planned_opening = _cue_slots(scene.family, scene.treatment, len(cues))
        labels = []
        if planned_opening:
            labels.append(scene.opening_label)
        for slot in slots:
            node = source
            for part in slot.path:
                node = node[part]
            labels.append(node[slot.label_field])
        beat = {
            "id": source["id"], "screen_title": source["title"],
            "overlay_text": labels, "overlay_anchors": [anchor for anchor, _ in cues],
        }
        contract = build_scene_contract(beat, scene.family, scene.treatment)
        sparse = _sparse_response(
            source, contract.response_schema, contract.response_schema.get("$defs", {})
        )
        assembled = contract.assemble(
            sparse, beat=beat, narration=scene.narration, topic=scene.topic,
            duration=scene.duration,
        )
        rebuilt = ADAPTER.validate_python(
            normalize_scene_time_placeholders(assembled, scene.duration)
        )
        assert [anchor for anchor, _ in scene_cues(rebuilt)] == [anchor for anchor, _ in cues]
        assert rebuilt.family == scene.family and rebuilt.treatment == scene.treatment


def common(family, treatment, narration, **extra):
    return {
        "id": f"test-{family}-{treatment}",
        "topic": f"Unrelated {family} demonstration",
        "title": f"How {family.replace('_', ' ')} works",
        "narration": narration,
        "duration": 12,
        "provenance": "illustrative",
        "family": family,
        "treatment": treatment,
        **extra,
    }


def step(label, anchor, at, **extra):
    return {
        "label": label,
        "detail": "Narrated detail",
        "anchor": anchor,
        "symbol": "box",
        "at": at,
        **extra,
    }


def new_scene_examples():
    scenes = []
    chronology = (
        "In 1901 the survey began. In 1905 the team measured again. "
        "In 1910 the result was published."
    )
    events = [
        step("Survey began", "1901 the survey began", 0.5, date="1901"),
        step("Measured again", "1905 the team measured again", 3.4, date="1905"),
        step("Result published", "1910 the result was published", 6.5, date="1910"),
    ]
    for treatment in ("event_track", "event_cards"):
        scenes.append(
            common("chronology", treatment, chronology, events=events, source_ref="fixture-history")
        )

    point = {
        "label": "Equatorial station",
        "latitude": 0,
        "longitude": -78.5,
        "anchor": "equatorial station",
        "at": 0.6,
        "source_ref": "fixture-coordinates",
    }
    second = {
        "label": "Northern station",
        "latitude": 45,
        "longitude": 7,
        "anchor": "northern station",
        "at": 5.2,
        "source_ref": "fixture-coordinates",
    }
    scenes.append(
        common(
            "geography",
            "map_locator",
            "We locate the equatorial station before measuring the signal.",
            places=[point],
        )
    )
    scenes.append(
        common(
            "geography",
            "region_focus",
            "We focus on the northern region before comparing the samples.",
            region={
                "label": "Northern region",
                "west": -10,
                "east": 25,
                "south": 35,
                "north": 60,
                "anchor": "northern region",
                "at": 0.6,
                "source_ref": "fixture-bounds",
            },
        )
    )
    scenes.append(
        common(
            "geography",
            "verified_route",
            "The transect starts at the equatorial station and reaches the northern station.",
            provenance="verified",
            places=[point, second],
            route_source="synthetic fixture route geometry",
            route_path=[
                {"latitude": 0, "longitude": -78.5},
                {"latitude": 18, "longitude": -42},
                {"latitude": 45, "longitude": 7},
            ],
        )
    )

    contrast = (
        "Chemical noise was the old claim. Atomic emission is the supported explanation. "
        "The correction changes the model."
    )
    left = step("Chemical noise", "Chemical noise", 0.5)
    right = step("Atomic emission", "Atomic emission", 4.0)
    scenes.append(common("concept_contrast", "juxtaposition", contrast, left=left, right=right))
    scenes.append(
        common("concept_contrast", "paired_contributions", contrast, left=left, right=right)
    )
    scenes.append(
        common(
            "concept_contrast",
            "correction_reveal",
            contrast,
            left=left,
            right=right,
            resolution="Model corrected",
            resolution_anchor="correction changes the model",
            resolution_at=7.2,
        )
    )

    quantities = (
        "In 2001 the sample measured 10 units. In 2002 it measured 20 units. "
        "In 2003 it measured 30 units."
    )
    points = [
        {"label": "Sample A", "value": 10, "anchor": "10 units", "at": 0.5, "date": "2001"},
        {"label": "Sample B", "value": 20, "anchor": "20 units", "at": 4.1, "date": "2002"},
        {"label": "Sample C", "value": 30, "anchor": "30 units", "at": 7.2, "date": "2003"},
    ]
    for treatment, count in (("metric_reveal", 1), ("comparable_bars", 2), ("time_trend", 3)):
        scenes.append(
            common(
                "quantitative_display",
                treatment,
                quantities,
                points=points[:count],
                unit="units",
                source_ref="fixture-measurements",
            )
        )

    document = "The report records water at 10 meters. The finding shows the river rose."
    for treatment in ("guided_highlight", "extracted_finding"):
        scenes.append(
            common(
                "document_focus",
                treatment,
                document,
                document_label="Survey report",
                source_ref="fixture-report",
                excerpt="Water at 10 meters",
                excerpt_anchor="water at 10 meters",
                excerpt_at=0.5,
                finding="The river rose",
                finding_anchor="river rose",
                finding_at=5.4,
            )
        )

    relationship = (
        "The institute oversees Lab A. Lab A works with Lab B. Lab B shares results with Lab C."
    )
    nodes = [
        step("Institute", "institute", 0.5, id="institute", parent_id=None),
        step("Lab A", "Lab A", 3.1, id="lab_a", parent_id="institute"),
        step("Lab B", "Lab B", 5.8, id="lab_b", parent_id="institute"),
        step("Lab C", "Lab C", 8.1, id="lab_c", parent_id="institute"),
    ]
    scenes.append(
        common(
            "relationship",
            "hierarchy_build",
            relationship,
            nodes=nodes,
            source_ref="fixture-organization",
        )
    )
    scenes.append(
        common(
            "relationship",
            "network_build",
            relationship,
            nodes=nodes,
            links=[
                {"from_id": "institute", "to_id": "lab_a"},
                {"from_id": "lab_a", "to_id": "lab_b"},
                {"from_id": "lab_b", "to_id": "lab_c"},
            ],
            source_ref="fixture-organization",
        )
    )
    scenes.append(common(
        "evidence", "signal_to_inference",
        "The detector found neutrinos. That observation supports fusion in the star.",
        observed="Neutrinos detected", observed_anchor="found neutrinos", reveal_at=1.1,
        inference="Evidence for fusion", inference_anchor="supports fusion", inference_at=5.0,
        observed_asset_concept="inspect", inference_asset_concept="energy",
    ))
    scenes.append(common(
        "cutaway", "focal_inset",
        "The Sun has a core. Hydrogen fuses in that core.",
        opening_label="The Sun", opening_anchor="The Sun", opening_at=0.5,
        symbol="box", base_asset_concept="solar cross-section study",
        parts=[{
            "label": "Fusion in the core", "detail": "One selected feature",
            "anchor": "fuses in that core", "symbol": "box", "x": 0.5, "y": 0.5,
            "at": 4.0,
        }],
    ))
    scenes.append(common(
        "relationship", "nested_scope",
        "One branch sits within the library network, and the network serves the city.",
        nodes=[
            step("One branch", "One branch", 0.5, id="branch", parent_id="network"),
            step("Library network", "library network", 3.4, id="network", parent_id="city"),
            step("City service", "the city", 7.0, id="city", parent_id=None),
        ],
    ))
    scenes.append(common(
        "claim_boundary", "bounded_statement",
        "The bridge is open to pedestrians, but not to vehicles.",
        claim=step("Open to pedestrians", "open to pedestrians", 0.5),
        limit=step("Not to vehicles", "not to vehicles", 5.0),
    ))
    scenes.append(common(
        "scenario_branch", "conditional_fork",
        "Depending on inspection, the shipment may be released or held for review.",
        condition=step("Inspection decides", "Depending on inspection", 0.5),
        first_outcome=step("Release possible", "may be released", 4.0),
        second_outcome=step("Review possible", "held for review", 7.0),
    ))

    frame = (
        "Why did the signal change? The measurements reveal a hidden pattern. "
        "Chapter two follows the search."
    )
    for treatment in ("kinetic_question", "statement_reveal", "chapter_marker"):
        scenes.append(
            common(
                "editorial_frame",
                treatment,
                frame,
                lines=[
                    step("Why did the signal change?", "Why did the signal change", 0.5),
                    step("A hidden pattern", "hidden pattern", 4.2),
                ],
                chapter_label="Chapter two" if treatment == "chapter_marker" else "",
            )
        )

    close = (
        "We traced the origin and measured the change. What would you investigate next? "
        "Subscribe for more. That brings us back to the first question."
    )
    recap = [step("Origin", "origin", 0.5), step("Change", "change", 2.2)]
    for treatment in ("recap_grid", "loop_bridge"):
        scenes.append(
            common(
                "editorial_close",
                treatment,
                close,
                recap=recap,
                question="What would you investigate next?",
                question_anchor="What would you investigate next",
                question_at=4.1,
                invitation="Subscribe for more",
                invitation_anchor="Subscribe for more",
                invitation_at=7.0,
                bridge="Back to the first question",
                bridge_anchor="back to the first question" if treatment == "loop_bridge" else "",
                bridge_at=9.1,
            )
        )
    scenes.append(common(
        "editorial_close", "community_close",
        "Tell us in the comments which clue to explain next. Subscribe for more. "
        "If useful, like and share this video.",
        recap=[],
        actions=[
            {"role": "comment", "label": "Which clue next?", "anchor": "Tell us in the comments",
             "at": 0.5},
            {"role": "subscribe", "label": "Subscribe for more", "anchor": "Subscribe for more",
             "at": 4.0},
            {"role": "like_share", "label": "Like and share", "anchor": "like and share",
             "at": 7.0},
        ],
    ))
    return scenes


def test_registry_matches_all_scene_models_and_treatments():
    schema = ADAPTER.json_schema()
    variants = schema["oneOf"]
    assert len(FAMILIES) == len(REGISTRY) == len(variants) == 20
    assert set(SCENE_MODELS) == set(REGISTRY)
    treatments = {
        (family.id, treatment.id) for family in FAMILIES for treatment in family.treatments
    }
    assert len(treatments) == 46
    for variant in variants:
        model = schema["$defs"][variant["$ref"].split("/")[-1]]
        family = model["properties"]["family"]["const"]
        treatment_schema = model["properties"]["treatment"]
        treatments = set(treatment_schema.get("enum", [treatment_schema.get("const")]))
        registered = REGISTRY[family]
        assert treatments == {item.id for item in registered.treatments}
        assert len(treatments) >= 1
        assert registered.renderer
        assert registered.use_when and registered.avoid_when
    assert len(planner_catalog()) == 20
    assert all(item["treatments"] for item in planner_catalog())


def test_true_complementary_roles_cannot_be_crossed_out():
    beat = {
        "design_family": "concept_contrast",
        "design_treatment": "correction_reveal",
        "purpose": "Distinguish the team's complementary contributions",
        "overlay_text": ["Observer: found the signal", "Analyst: measured its strength"],
    }
    assert effective_design_treatment(beat) == "paired_contributions"
    beat["purpose"] = "Distinguish two concepts"
    assert effective_design_treatment(beat) == "juxtaposition"
    beat["overlay_text"][0] = "Myth: the signal was noise"
    assert effective_design_treatment(beat) == "correction_reveal"


def test_new_treatments_are_exposed_only_with_compatible_spoken_roles():
    def options(speech, count, first=""):
        return {
            (item["family"], item["treatment"])
            for item in eligible_treatments(speech, count, first_role_text=first)
        }

    observed = options("An ash layer suggests a past fire", 2, "An ash layer")
    assert ("evidence", "signal_to_inference") in observed
    assert ("evidence", "competing") not in observed
    assert ("cutaway", "focal_inset") in observed
    assert ("relationship", "nested_scope") not in observed
    qualified = options("The bridge is open, but only to pedestrians", 2)
    assert ("claim_boundary", "bounded_statement") in qualified
    branched = options("Depending on inspection, it may pass or fail", 3)
    assert ("scenario_branch", "conditional_fork") in branched
    assert ("scenario_branch", "conditional_fork") not in observed


def test_focal_inset_requires_detailed_subject_not_generic_icon():
    assert detailed_subject_asset("solar cross-section study")
    assert not detailed_subject_asset("sun")
    assert not detailed_subject_asset(None)


@pytest.mark.parametrize("family,treatment,mutate", [
    ("evidence", "signal_to_inference", lambda scene: scene.update(expected="Invented baseline")),
    ("cutaway", "focal_inset", lambda scene: scene.update(base_asset_concept=None)),
    ("cutaway", "focal_inset", lambda scene: scene["parts"].append(
        step("Second feature", "The Sun", 7.0, x=0.6, y=0.6)
    )),
    ("relationship", "nested_scope", lambda scene: scene["nodes"][0].update(parent_id="city")),
    ("claim_boundary", "bounded_statement", lambda scene: scene["limit"].update(at=0.3)),
    ("scenario_branch", "conditional_fork", lambda scene: scene["second_outcome"].update(at=2.0)),
])
def test_new_treatments_reject_incompatible_scene_data(family, treatment, mutate):
    scene = next(
        deepcopy(item) for item in new_scene_examples()
        if (item["family"], item["treatment"]) == (family, treatment)
    )
    mutate(scene)
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(scene)


def test_new_treatments_take_reveal_times_from_measured_speech():
    choices = {
        ("evidence", "signal_to_inference"), ("cutaway", "focal_inset"),
        ("relationship", "nested_scope"), ("claim_boundary", "bounded_statement"),
        ("scenario_branch", "conditional_fork"),
    }
    for data in new_scene_examples():
        if (data["family"], data["treatment"]) not in choices:
            continue
        scene = ADAPTER.validate_python(data)
        words = scene.narration.split()
        window = {
            "start": 0.0,
            "words": [
                {"word": word, "start": index * 0.45, "end": (index + 1) * 0.45}
                for index, word in enumerate(words)
            ],
        }
        aligned, absolute = align_scene_to_measured_speech(
            scene, scene.narration, window, 0.0, 12.0
        )
        assert absolute == pytest.approx([time for _, time in scene_cues(aligned)])
        assert absolute == sorted(absolute)
        validate_scene_layout(aligned)


def test_every_legacy_designed_kind_has_explicit_family_coverage():
    schema = TypeAdapter(MotionShot).json_schema()
    variants = schema["oneOf"]
    kinds = {
        schema["$defs"][variant["$ref"].split("/")[-1]]["properties"]["kind"]["const"]
        for variant in variants
    }
    assert kinds == set(LEGACY_DESIGNED_KINDS) | set(LEGACY_MEDIA_KINDS)
    assert all(set(families) <= set(REGISTRY) for families in LEGACY_DESIGNED_KINDS.values())
    assert all(set(families) <= set(REGISTRY) for families in LEGACY_VISUAL_TYPES.values())


def test_all_new_treatments_validate_render_and_expose_cues():
    scenes = [ADAPTER.validate_python(item) for item in new_scene_examples()]
    assert len(scenes) == 26
    assert {scene.family for scene in scenes} == set(REGISTRY) - {
        "process",
        "range",
        "causal",
        "before_after",
        "parallel",
        "journey",
        "scale_ladder",
        "synthesis",
    }
    for scene in scenes:
        cues = scene_cues(scene)
        assert cues and [at for _, at in cues] == sorted(set(at for _, at in cues))
        assert all(anchor.casefold() in scene.narration.casefold() for anchor, _ in cues)
        start = render_frame(scene, 0, presentation="production")
        later = render_frame(scene, scene.duration * 0.8, presentation="production")
        assert start.size == later.size == (960, 540)
        assert ImageChops.difference(start, later).getbbox(), (scene.family, scene.treatment)
        assert all(asset["status"] == "ready" for asset in scene_asset_report(scene))


def test_all_new_treatments_can_be_retimed_from_measured_speech():
    for data in new_scene_examples():
        scene = ADAPTER.validate_python(data)
        updated = scene.model_dump(mode="json")
        times = [0.6 + index * 1.4 for index, _ in enumerate(scene_cues(scene))]
        _set_relative_times(updated, times)
        revised = ADAPTER.validate_python(updated)
        assert [time for _, time in scene_cues(revised)] == pytest.approx(times)


@pytest.mark.parametrize(
    ("family", "treatment", "mutate"),
    [
        ("geography", "verified_route", lambda scene: scene.pop("route_path")),
        ("geography", "verified_route", lambda scene: scene.update(provenance="illustrative")),
        ("geography", "verified_route", lambda scene: scene.update(route_source="")),
        (
            "geography",
            "verified_route",
            lambda scene: scene["route_path"][0].update(latitude=12),
        ),
        ("geography", "region_focus", lambda scene: scene["region"].update(east=-30)),
        ("quantitative_display", "comparable_bars", lambda scene: scene["points"].pop()),
        (
            "quantitative_display",
            "time_trend",
            lambda scene: scene["points"][1].update(date=""),
        ),
        (
            "relationship",
            "hierarchy_build",
            lambda scene: scene["nodes"][1].update(parent_id="missing"),
        ),
        (
            "relationship",
            "hierarchy_build",
            lambda scene: scene["nodes"][2].update(parent_id="lab_a"),
        ),
        ("relationship", "network_build", lambda scene: scene.update(links=[])),
        ("editorial_frame", "chapter_marker", lambda scene: scene.update(chapter_label="")),
        ("editorial_close", "loop_bridge", lambda scene: scene.update(bridge_anchor="")),
    ],
)
def test_invalid_scene_combinations_fail(family, treatment, mutate):
    scene = next(
        deepcopy(item)
        for item in new_scene_examples()
        if item["family"] == family and item["treatment"] == treatment
    )
    mutate(scene)
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(scene)
