"""Evidence-gated semantic diagrams over approved long-form motion plans."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from pydantic import TypeAdapter

from autonomous_studio.audio.alignment import phrase_start, tokens
from autonomous_studio.domain.models import hashable_longform_chapter
from autonomous_studio.domain.motion import MotionPlan, validate_motion_plan
from autonomous_studio.domain.semantic import SemanticDecisionBatch
from autonomous_studio.rendering.semantic_assets import detailed_subject_asset
from autonomous_studio.rendering.semantic_diagrams import (
    Scene,
    scene_asset_report,
    scene_cues,
    validate_scene_layout,
)
from autonomous_studio.rendering.visual_registry import effective_design_treatment
from autonomous_studio.workflow.semantic_scene_contract import validate_scene_fidelity
from autonomous_studio.workflow.spoken_numbers import numeric_literals, spoken_number_values

SCENE_ADAPTER = TypeAdapter(Scene)
IMAGE_TYPES = {"authentic_image", "generated_image"}
MEDIA_SHOT_KINDS = {"camera", "detail_reveal", "guided_image"}
SEMANTIC_PLANNER_VERSION = "semantic-production-v2-measured-reveals"


def family_first_chapter(chapter) -> bool:
    """A saved v1 chapter stays compatible; a v2 chapter must be complete."""
    beats = chapter.visual_beats
    return any(getattr(beat, "design_family", None) for beat in beats)


def require_family_first_decisions(project, chapter) -> None:
    """Never let a production render show the neutral timing scaffold."""
    if project.pipeline != "live" or not family_first_chapter(chapter):
        return
    for beat in chapter.visual_beats:
        if beat.visual_type in IMAGE_TYPES:
            continue
        if not beat.design_family or not beat.design_treatment:
            raise ValueError(f"family-first beat {beat.id} has no approved family/treatment")
    batch = project.longform_semantic_decisions.get(chapter.id)
    if batch is None or batch.schema_version != 2:
        raise ValueError(f"family-first chapter {chapter.id} needs saved v2 semantic decisions")


def semantic_input_hash(project, chapter, base_plan: MotionPlan, *, schema_version: int = 2) -> str:
    """Invalidate a paid decision only when its approved visual/speech input changes."""
    chapter_payload = hashable_longform_chapter(chapter)
    # A newly added optional planning field must not invalidate an already
    # approved, paid scene when its value is the old implicit default. A
    # populated role contract remains part of the hash and triggers replanning.
    for beat in chapter_payload.get("visual_beats", []):
        if not beat.get("role_semantics"):
            beat.pop("role_semantics", None)
        if beat.get("visual_data") is None:
            beat.pop("visual_data", None)
        if beat.get("screen_blueprint") is None:
            beat.pop("screen_blueprint", None)
    if schema_version == 1:
        for beat in chapter_payload.get("visual_beats", []):
            beat.pop("design_family", None)
            beat.pop("design_treatment", None)
    payload = {
        "planner": SEMANTIC_PLANNER_VERSION if schema_version == 2 else "semantic-production-v1",
        "chapter": chapter_payload,
        "base_plan": base_plan.model_dump(mode="json"),
        "voices": {
            block.id: project.longform_voices[block.id].artifact.sha256
            for block in chapter.narration_blocks
        },
        "word_timings": {
            block.id: project.longform_word_timings.get(block.id, [])
            for block in chapter.narration_blocks
        },
        "visuals": {
            beat.asset_ref: project.longform_media[beat.asset_ref].sha256
            for beat in chapter.visual_beats
        },
        "policy": project.longform_visual_policy.model_dump(mode="json"),
        "model": getattr(project, "longform_semantic_models", {}).get(
            chapter_payload.get("id", getattr(chapter, "id", "")),
            project.live_models.get(
                "semantic_layout_model", project.live_models.get("director_model", "mock-director")
            ),
        ),
    }
    if schema_version == 2:
        from autonomous_studio.rendering.visual_registry import FAMILIES

        payload["visual_registry"] = [(family.id, family.version) for family in FAMILIES]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _displayed_text(scene: Scene) -> list[str]:
    data = scene.model_dump(mode="json")
    keys = {
        "title",
        "label",
        "detail",
        "change",
        "qualifier",
        "inference",
        "takeaway",
        "expected",
        "observed",
        "outcome",
        "opening_label",
    }

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in keys and isinstance(child, str):
                    yield child
                else:
                    yield from visit(child)
        elif isinstance(value, list):
            for child in value:
                yield from visit(child)

    return list(visit(data))


def _set_relative_times(data: dict, times: list[float]) -> None:
    if data.get("opening_anchor"):
        data["opening_at"] = times[0]
        times = times[1:]
    family = data["family"]
    if family in {
        "process",
        "causal",
        "cutaway",
        "journey",
        "scale_ladder",
        "chronology",
        "quantitative_display",
        "relationship",
        "editorial_frame",
    }:
        key = {
            "process": "steps",
            "causal": "stages",
            "cutaway": "parts",
            "journey": "places",
            "scale_ladder": "items",
            "chronology": "events",
            "quantitative_display": "points",
            "relationship": "nodes",
            "editorial_frame": "lines",
        }[family]
        for item, at in zip(data[key], times, strict=True):
            item["at"] = at
    elif family == "evidence":
        if data["treatment"] == "signal_to_inference":
            data["reveal_at"], data["inference_at"] = times
        else:
            data["expected_at"], data["reveal_at"], data["inference_at"] = times
    elif family == "range":
        data["reveal_at"] = times[0]
    elif family == "before_after":
        data["before"]["at"], data["after"]["at"] = times
    elif family in {"parallel", "synthesis"}:
        key, final = ("tracks", "outcome_at") if family == "parallel" else ("inputs", "takeaway_at")
        for item, at in zip(data[key], times[:-1], strict=True):
            item["at"] = at
        data[final] = times[-1]
    elif family == "geography":
        key = "region" if data["treatment"] == "region_focus" else "places"
        if key == "region":
            data[key]["at"] = times[0]
        else:
            for item, at in zip(data[key], times, strict=True):
                item["at"] = at
    elif family == "concept_contrast":
        data["left"]["at"], data["right"]["at"] = times[:2]
        if data["treatment"] == "correction_reveal":
            data["resolution_at"] = times[2]
    elif family == "document_focus":
        data["excerpt_at"], data["finding_at"] = times
    elif family == "claim_boundary":
        data["claim"]["at"], data["limit"]["at"] = times
    elif family == "scenario_branch":
        data["condition"]["at"], data["first_outcome"]["at"], data["second_outcome"]["at"] = times
    elif family == "editorial_close":
        if data["treatment"] == "community_close":
            for item, at in zip(data["actions"], times, strict=True):
                item["at"] = at
            return
        count = len(data["recap"])
        for item, at in zip(data["recap"], times[:count], strict=True):
            item["at"] = at
        data["question_at"], data["invitation_at"] = times[count : count + 2]
        if data["treatment"] == "loop_bridge":
            data["bridge_at"] = times[-1]


def normalize_scene_time_placeholders(data: dict, shot_duration: float) -> dict:
    """Replace model-supplied clock guesses before structural scene validation.

    The scene schema needs ascending in-shot numbers, but measured speech later
    supplies the actual reveal times. A model may return chapter-absolute times
    or zeros; neither should invalidate otherwise sound visual roles.
    """
    data = dict(data)
    data["duration"] = round(shot_duration, 5)
    family = data.get("family")
    sequence_keys = {
        "process": "steps",
        "causal": "stages",
        "cutaway": "parts",
        "journey": "places",
        "scale_ladder": "items",
        "chronology": "events",
        "quantitative_display": "points",
        "relationship": "nodes",
        "editorial_frame": "lines",
    }
    try:
        if family in sequence_keys:
            count = len(data[sequence_keys[family]])
        elif family in {"evidence", "editorial_close"}:
            count = (
                (2 if data["treatment"] == "signal_to_inference" else 3)
                if family == "evidence"
                else len(data["actions"]) if data["treatment"] == "community_close"
                else len(data["recap"]) + 2 + (data["treatment"] == "loop_bridge")
            )
        elif family in {"before_after", "document_focus"}:
            count = 2
        elif family in {"parallel", "synthesis"}:
            count = len(data["tracks" if family == "parallel" else "inputs"]) + 1
        elif family == "geography":
            count = 1 if data["treatment"] == "region_focus" else len(data["places"])
        elif family == "concept_contrast":
            count = 2 + (data["treatment"] == "correction_reveal")
        elif family == "range":
            count = 1
        elif family == "claim_boundary":
            count = 2
        elif family == "scenario_branch":
            count = 3
        else:
            raise ValueError(f"unregistered scene family: {family}")
        count += bool(data.get("opening_anchor"))
        if count <= 0 or shot_duration <= 2:
            raise ValueError("scene needs positive duration and spoken reveal roles")
        times = [round(shot_duration * index / (count + 1), 5) for index in range(1, count + 1)]
        _set_relative_times(data, times)
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError(f"scene timing roles are incomplete: {exc}") from exc
    return data


def align_scene_to_measured_speech(
    scene: Scene, narration: str, window: dict, shot_start: float, shot_end: float
) -> tuple[Scene, list[float]]:
    """The model chooses anchors; recorded speech is the only timing authority."""
    words = window.get("words") or []
    if not words:
        raise ValueError("measured word timing is unavailable")
    cues = scene_cues(scene)
    if (
        scene.opening_anchor
        and len(cues) > 1
        and (
            tokens(cues[0][0]) == tokens(cues[1][0])
            or tokens(cues[1][0])[: len(tokens(cues[0][0]))] == tokens(cues[0][0])
            or tokens(cues[0][0])[: len(tokens(cues[1][0]))] == tokens(cues[1][0])
        )
        and phrase_start(narration, cues[1][0], words, offset=window["start"]) <= shot_start + 2.5
    ):
        # The first family role already supplies the opening visual. A second
        # cue beginning at the same spoken words would duplicate its entrance,
        # even if one anchor is a longer version of the other.
        data = scene.model_dump(mode="json")
        data.update(opening_anchor="", opening_label="", opening_at=0)
        scene = SCENE_ADAPTER.validate_python(data)
    anchors = [anchor for anchor, _ in scene_cues(scene)]
    absolute = [
        phrase_start(narration, anchor, words, offset=window["start"]) for anchor in anchors
    ]
    script_words = tokens(narration)
    spoken_positions = [
        next(
            (
                index
                for index in range(len(script_words) - len(target) + 1)
                if script_words[index : index + len(target)] == target
            ),
            None,
        )
        for anchor in anchors
        for target in [tokens(anchor)]
    ]
    if (
        None in spoken_positions
        or spoken_positions != sorted(set(spoken_positions))
        or absolute != sorted(absolute)
    ):
        details = ", ".join(
            f"{anchor!r}@word {position}"
            for anchor, position in zip(anchors, spoken_positions, strict=True)
        )
        raise ValueError(
            "spoken semantic anchors are not unique and ordered: "
            f"{details}. A repeated word may resolve to its earlier mention; "
            "choose longer, unambiguous phrases from this shot's spoken slice"
        )
    if absolute[0] < shot_start - 0.15 or absolute[-1] > shot_end - 0.28:
        raise ValueError("spoken roles fall outside this visual beat")
    absolute = [max(shot_start + 0.04, value) for value in absolute]
    # Word-level ASR sometimes assigns the same onset to adjacent words. The
    # shot-start clamp can also collapse two nearby onsets. Preserve verified
    # script order with a minimal separation rather than regenerating anchors.
    for index in range(1, len(absolute)):
        if absolute[index] <= absolute[index - 1]:
            absolute[index] = round(absolute[index - 1] + 0.04, 5)
    if absolute[-1] > shot_end - 0.28:
        raise ValueError("cue entrances collide at shot boundary")
    data = scene.model_dump(mode="json")
    data["duration"] = round(shot_end - shot_start, 5)
    _set_relative_times(data, [round(at - shot_start, 5) for at in absolute])
    if scene.family == "editorial_close" and scene.treatment == "community_close" and scene.response_options:
        script_words = tokens(narration)
        option_times = []
        comment = next(item for item in data["actions"] if item["role"] == "comment")
        comment_at = comment["at"]
        comment_phrase = tokens(comment["anchor"])
        comment_positions = [
            index for index in range(len(script_words) - len(comment_phrase) + 1)
            if script_words[index:index + len(comment_phrase)] == comment_phrase
        ]
        if len(comment_positions) != 1:
            raise ValueError("comment invitation needs one occurrence in approved speech")
        comment_position = comment_positions[0]
        previous_position = -1
        for choice in scene.response_options:
            phrase = tokens(choice)
            positions = [
                index for index in range(len(script_words) - len(phrase) + 1)
                if index < comment_position
                and script_words[index:index + len(phrase)] == phrase
            ]
            if len(positions) != 1 or positions[0] <= previous_position:
                raise ValueError("response choice needs one ordered occurrence in approved speech")
            previous_position = positions[0]
            at = max(0.04, round(
                phrase_start(narration, choice, words, offset=window["start"]) - shot_start,
                5,
            ))
            if option_times and at <= option_times[-1]:
                at = round(option_times[-1] + 0.04, 5)
            if at >= comment_at:
                raise ValueError("response choice is not spoken before the comment invitation")
            option_times.append(at)
        data["response_option_ats"] = option_times
    from autonomous_studio.workflow.visual_reveal_timing import warn_reveal_gap

    # Response choices are visible entrances too. Counting only CTA actions
    # would incorrectly report an inactive opening on choice-led closes.
    entrances = sorted(absolute + [shot_start + at for at in data.get("response_option_ats", [])])
    if entrances[0] > shot_start + 2.5:
        warn_reveal_gap(scene.id, "measured", "opening", entrances[0] - shot_start, 2.5)
    for left, right in zip(entrances, entrances[1:]):
        if right - left > 8.5:
            warn_reveal_gap(scene.id, "measured", "middle", right - left, 8.5)
    if shot_end - entrances[-1] > 8.5:
        warn_reveal_gap(scene.id, "measured", "ending", shot_end - entrances[-1], 8.5)
    return SCENE_ADAPTER.validate_python(data), absolute


def validate_decision_inputs(
    project: dict,
    chapter_id: str,
    batch: SemanticDecisionBatch,
    base_plan: MotionPlan | None = None,
    *,
    allow_partial: bool = False,
):
    chapter = next((c for c in project["spec"]["chapters"] if c["id"] == chapter_id), None)
    if chapter is None or batch.chapter_id != chapter_id:
        raise ValueError("semantic decision chapter does not match project")
    beats = {beat["id"]: beat for beat in chapter["visual_beats"]}
    blocks = {block["id"]: block["text"] for block in chapter["narration_blocks"]}
    if len({d.beat_id for d in batch.decisions}) != len(batch.decisions):
        raise ValueError("duplicate semantic decision for a beat")
    designed = set()
    if batch.schema_version == 2:
        if base_plan is None:
            raise ValueError("family-first coverage requires the measured base motion plan")
        designed = {shot.id for shot in base_plan.shots if shot.kind not in MEDIA_SHOT_KINDS}
        submitted = {decision.beat_id for decision in batch.decisions}
        if ((not allow_partial and submitted != designed)
            or (allow_partial and not submitted <= designed) or any(
            decision.mode != "diagram" for decision in batch.decisions
        )):
            raise ValueError("family-first plan must design every code-rendered beat")
    for decision in batch.decisions:
        beat = beats.get(decision.beat_id)
        if beat is None:
            raise ValueError(f"semantic decision references unknown beat {decision.beat_id}")
        if batch.schema_version == 2 and beat["visual_type"] in IMAGE_TYPES:
            raise ValueError(
                f"{beat['id']} is an approved image beat; replan it before choosing a design family"
            )
        if decision.mode == "keep":
            continue
        base_kind = (
            next((shot.kind for shot in base_plan.shots if shot.id == decision.beat_id), None)
            if base_plan is not None
            else None
        )
        if beat["visual_type"] in IMAGE_TYPES and base_kind in MEDIA_SHOT_KINDS | {None}:
            raise ValueError(f"semantic pilot cannot replace image-led beat {beat['id']}")
        if len(beat["narration_block_ids"]) != 1 and not beat.get("spoken_unit_ids"):
            raise ValueError("semantic pilot needs a known approved spoken span")
        scene = SCENE_ADAPTER.validate_json(decision.scene_json)
        if batch.schema_version == 2:
            validate_scene_fidelity(beat, scene)
        if batch.schema_version == 2 and beat.get("design_family"):
            expected = effective_design_treatment(beat)
            if (scene.family, scene.treatment) != (beat["design_family"], expected):
                raise ValueError(f"{beat['id']} changed its approved family or treatment")
        elif batch.schema_version == 2:
            raise ValueError(f"{beat['id']} has no approved family or treatment")
        if beat.get("spoken_unit_ids"):
            from autonomous_studio.workflow.spoken_units import spoken_text

            narration = spoken_text(chapter, beat)
        else:
            narration = blocks[beat["narration_block_ids"][0]]
        if scene.narration != narration:
            raise ValueError(f"semantic scene changed approved narration for {beat['id']}")
        if scene.provenance != "illustrative":
            raise ValueError("semantic director cannot self-certify verified provenance")
        if not beat["claim_ids"]:
            raise ValueError("semantic diagram needs claim links to the approved speech")
        asset_report = scene_asset_report(scene)
        if (scene.family, scene.treatment) == ("cutaway", "focal_inset") and not (
            detailed_subject_asset(scene.base_asset_concept)
        ):
            raise ValueError(
                f"focal inset needs a detailed subject illustration, not an icon: {beat['id']}"
            )
        if any(
            item["status"] != "ready" or item["match"] == "generic_fallback"
            for item in asset_report
        ):
            raise ValueError(f"semantic diagram has an unreviewed or missing asset: {beat['id']}")
        spoken_numbers = spoken_number_values(narration)
        for label in _displayed_text(scene):
            if not numeric_literals(label) <= spoken_numbers:
                raise ValueError(f"semantic diagram displays an unspoken number: {beat['id']}")
        if scene.family == "scale_ladder":
            for item in scene.items:
                if Decimal(str(item.value)) not in spoken_numbers:
                    raise ValueError("scale ladder values must be spoken as numbers")
        if scene.family == "quantitative_display":
            for item in scene.points:
                if Decimal(str(item.value)) not in spoken_numbers:
                    raise ValueError("quantitative values must be spoken as numbers")
        if scene.family == "journey" and scene.geometry != "schematic":
            raise ValueError("unverified journeys must be schematic")
        creative_policy = project.get("longform_creative_policy") or {}
        if creative_policy.get("schema_version", 0) >= 2:
            try:
                validate_scene_layout(scene)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"{beat['id']} semantic scene failed executable layout preflight: {exc}"
                ) from exc
    return chapter


def apply_semantic_decisions(
    project: dict,
    chapter_id: str,
    base_plan: MotionPlan,
    speech_windows: list[dict],
    batch: SemanticDecisionBatch,
    *,
    allow_partial: bool = False,
) -> tuple[MotionPlan, dict]:
    """Convert validated proposals into measured cues; abstain on mistimed shots."""
    validate_decision_inputs(
        project, chapter_id, batch, base_plan, allow_partial=allow_partial
    )
    windows = {window["block_id"]: window for window in speech_windows}
    chapter = next(c for c in project["spec"]["chapters"] if c["id"] == chapter_id)
    beats = {beat["id"]: beat for beat in chapter["visual_beats"]}
    blocks = {block["id"]: block["text"] for block in chapter["narration_blocks"]}
    decisions = {decision.beat_id: decision for decision in batch.decisions}
    shots = []
    report = []
    for original in base_plan.shots:
        shot = original.model_dump(mode="json")
        decision = decisions.get(shot["id"])
        if decision is None or decision.mode == "keep":
            shots.append(shot)
            asset = project.get("longform_media", {}).get(shot.get("asset_ref"))
            report.append(
                {
                    "beat_id": shot["id"],
                    "result": "kept",
                    "reason": decision.chosen_because if decision else "no semantic proposal",
                    "claim_ids": shot["claim_ids"],
                    "media": (
                        {
                            "asset_ref": shot["asset_ref"],
                            "sha256": asset.get("sha256"),
                            "provider": asset.get("provider"),
                            "provenance": asset.get("provenance"),
                        }
                        if asset and shot.get("asset_ref")
                        else None
                    ),
                }
            )
            continue
        scene = SCENE_ADAPTER.validate_json(decision.scene_json)
        beat = beats[shot["id"]]
        if beat.get("spoken_unit_ids"):
            from autonomous_studio.workflow.spoken_units import combined_speech

            spoken_block, window = combined_speech(chapter, beat, windows)
            narration = spoken_block["text"]
        else:
            block_id = beat["narration_block_ids"][0]
            window = windows[block_id]
            narration = blocks[block_id]
        try:
            scene, absolute = align_scene_to_measured_speech(
                scene, narration, window, shot["start"], shot["end"]
            )
            anchors = [anchor for anchor, _ in scene_cues(scene)]
            base_fields = {
                name: shot[name]
                for name in (
                    "id",
                    "start",
                    "end",
                    "intent",
                    "viewer_takeaway",
                    "narration_block_ids",
                    "claim_ids",
                )
            }
            replacement = {
                **base_fields,
                "kind": "semantic_diagram",
                "why_this_template": decision.chosen_because,
                "scene": scene.model_dump(mode="json"),
                "asset_status": "ready",
                "cues": [
                    {
                        "at_seconds": round(at, 5),
                        "narration_excerpt": anchor,
                        "action": "reveal_step",
                        "step_index": index,
                        "duration_seconds": min(0.35, max(0.08, shot["end"] - at - 0.04)),
                    }
                    for index, (anchor, at) in enumerate(zip(anchors, absolute, strict=True))
                ],
            }
            shots.append(replacement)
            report.append(
                {
                    "beat_id": shot["id"],
                    "result": "applied",
                    "family": scene.family,
                    "treatment": scene.treatment,
                    "reason": decision.chosen_because,
                    "rejected_alternative": decision.rejected_alternative,
                    "assets": scene_asset_report(scene),
                    "cue_alignment": [
                        {"phrase": anchor, "at_seconds": round(at, 5)}
                        for anchor, at in zip(anchors, absolute, strict=True)
                    ],
                    "claim_ids": shot["claim_ids"],
                }
            )
        except ValueError as exc:
            if batch.schema_version == 2:
                raise ValueError(
                    f"family-first scene {shot['id']} failed alignment: {exc}"
                ) from exc
            shots.append(shot)
            report.append(
                {"beat_id": shot["id"], "result": "kept_after_validation", "reason": str(exc)}
            )
    plan = MotionPlan.model_validate({**base_plan.model_dump(mode="json"), "shots": shots})
    if batch.schema_version == 2 and not allow_partial:
        if any(
            shot.kind != "semantic_diagram" and shot.kind not in MEDIA_SHOT_KINDS
            for shot in plan.shots
        ):
            raise ValueError("family-first plan retained a legacy designed shot")
    validate_motion_plan(plan, project, speech_windows)
    return plan, {
        "chapter_id": chapter_id,
        "decisions": report,
        "applied": sum(item["result"] == "applied" for item in report),
    }
