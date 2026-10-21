"""A bounded, versioned repair for one approved long-form visual beat.

This does not alter the visual planner's beat-count heuristic. It lets an
operator repair a treatment-capacity mismatch in an already approved plan.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import BaseModel, Field

from autonomous_studio.audio.alignment import phrase_start
from autonomous_studio.domain.models import (
    LongFormSpec,
    LongFormVisualPlan,
    State,
    VideoProject,
)
from autonomous_studio.rendering.longform import narration_windows
from autonomous_studio.storage import Repository
from autonomous_studio.storage.repository import write_json
from autonomous_studio.workflow.motion_planning import plan_motion_chapter
from autonomous_studio.workflow.spoken_units import (
    chapter_units,
    unique_phrase_position,
    validate_unit_coverage,
)
from autonomous_studio.workflow.spoken_units import (
    words as spoken_words,
)
from autonomous_studio.workflow.visual_reveal_timing import validate_measured_reveal_tails


class BeatSplit(BaseModel):
    original_id: str
    new_id: str
    split_anchor: str = Field(min_length=3)
    first_overlay_count: int = Field(ge=1)
    first_purpose: str
    first_screen_title: str
    first_design_rationale: str
    first_visual_type: str | None = None
    first_design_family: str | None = None
    first_design_treatment: str | None = None
    first_overlay_text: list[str] | None = None
    first_overlay_anchors: list[str] | None = None
    first_role_unit_groups: list[list[str]] | None = None
    new_visual_type: str
    new_design_family: str
    new_design_treatment: str
    new_purpose: str
    new_screen_title: str
    new_design_rationale: str
    new_narration_anchor: str
    new_overlay_text: list[str] | None = None
    new_overlay_anchors: list[str] | None = None
    new_role_unit_groups: list[list[str]] | None = None
    new_duration_fraction: float = Field(gt=0.1, lt=0.9)


class BeatRetarget(BaseModel):
    beat_id: str
    new_visual_type: str
    new_design_family: str
    new_design_treatment: str
    new_purpose: str
    new_screen_title: str
    new_design_rationale: str
    new_overlay_text: list[str] | None = None
    new_overlay_anchors: list[str] | None = None
    new_role_unit_groups: list[list[str]] | None = None


def partition_spoken_roles(
    text: str,
    words: list[dict],
    labels: list[str],
    anchors: list[str],
    *,
    first_count: int,
    split_anchor: str,
    new_anchor: str,
) -> tuple[float, list[str], list[str], list[str], list[str]]:
    """A role cannot remain in a scene after its measured spoken boundary."""
    split_at = phrase_start(text, split_anchor, words)
    first_labels, second_labels = labels[:first_count], labels[first_count:]
    first_anchors, second_anchors = anchors[:first_count], anchors[first_count:]
    if (
        not first_labels
        or not second_labels
        or len(first_labels) != len(first_anchors)
        or len(second_labels) != len(second_anchors)
    ):
        raise ValueError("overlay roles must partition cleanly across both beats")
    if any(phrase_start(text, anchor, words) >= split_at for anchor in first_anchors):
        raise ValueError("first visual contains a reveal spoken after the split")
    if any(phrase_start(text, anchor, words) < split_at for anchor in second_anchors):
        raise ValueError("second visual contains a reveal spoken before the split")
    if phrase_start(text, new_anchor, words) < split_at:
        raise ValueError("new beat's entrance is spoken before its split")
    return split_at, first_labels, first_anchors, second_labels, second_anchors


def prepare_split(project: VideoProject, proposal: BeatSplit) -> tuple[VideoProject, dict]:
    """Return a validated copy; leave the durable project untouched."""
    if project.state != State.LONGFORM_SEMANTIC_PLANNING or project.pipeline != "live":
        raise ValueError("beat repair requires a live semantic-planning checkpoint")
    if not isinstance(project.spec, LongFormSpec) or project.longform_visual_plan is None:
        raise ValueError("beat repair requires an approved long-form visual plan")
    if project.longform_visual_policy is None:
        raise ValueError("beat repair requires a saved visual policy")
    if any(
        proposal.new_id == beat.id
        for chapter in project.spec.chapters
        for beat in chapter.visual_beats
    ):
        raise ValueError("new beat ID already exists")
    result = project.model_copy(deep=True)
    matches = [
        (chapter, index, beat)
        for chapter in result.spec.chapters
        for index, beat in enumerate(chapter.visual_beats)
        if beat.id == proposal.original_id
    ]
    if len(matches) != 1:
        raise ValueError("original beat must exist exactly once")
    chapter, index, original = matches[0]
    if chapter.id in result.longform_semantic_decisions:
        raise ValueError("cannot replace a chapter with committed semantic decisions")
    if original.visual_type in {"authentic_image", "generated_image"}:
        raise ValueError("this repair only splits an existing code-rendered beat")
    if len(original.narration_block_ids) != 1:
        raise ValueError("split beat must reference one narration block")
    block_id = original.narration_block_ids[0]
    block = next(block for block in chapter.narration_blocks if block.id == block_id)
    words = result.longform_word_timings[block_id]
    unit_lookup = {unit["id"]: unit for unit in chapter_units(chapter)}
    selected_units = [unit_lookup[unit_id] for unit_id in original.spoken_unit_ids]
    split_word = unique_phrase_position(spoken_words(block.text), proposal.split_anchor)
    boundaries = [
        index for index, unit in enumerate(selected_units) if unit["start_word"] == split_word
    ]
    if len(boundaries) != 1 or not 0 < boundaries[0] < len(selected_units):
        raise ValueError("split anchor must begin one of the beat's later spoken units")
    split_index = boundaries[0]
    first_units = original.spoken_unit_ids[:split_index]
    second_units = original.spoken_unit_ids[split_index:]
    first_labels = (
        proposal.first_overlay_text or original.overlay_text[: proposal.first_overlay_count]
    )
    first_anchors = (
        proposal.first_overlay_anchors or original.overlay_anchors[: proposal.first_overlay_count]
    )
    second_labels = (
        proposal.new_overlay_text or original.overlay_text[proposal.first_overlay_count :]
    )
    second_anchors = (
        proposal.new_overlay_anchors or original.overlay_anchors[proposal.first_overlay_count :]
    )
    split_at, first_labels, first_anchors, second_labels, second_anchors = partition_spoken_roles(
        block.text,
        words,
        first_labels + second_labels,
        first_anchors + second_anchors,
        first_count=len(first_labels),
        split_anchor=proposal.split_anchor,
        new_anchor=proposal.new_narration_anchor,
    )
    first_groups = proposal.first_role_unit_groups or [
        [unit for unit in group if unit in first_units]
        for group in original.role_unit_groups
        if any(unit in first_units for unit in group)
    ]
    second_groups = proposal.new_role_unit_groups or [
        [unit for unit in group if unit in second_units]
        for group in original.role_unit_groups
        if any(unit in second_units for unit in group)
    ]
    from autonomous_studio.rendering.visual_registry import REGISTRY, role_capacity

    for beat_id, unit_ids, groups, labels, anchors, family_id, treatment_id in (
        (
            original.id,
            first_units,
            first_groups,
            first_labels,
            first_anchors,
            proposal.first_design_family or original.design_family,
            proposal.first_design_treatment or original.design_treatment,
        ),
        (
            proposal.new_id,
            second_units,
            second_groups,
            second_labels,
            second_anchors,
            proposal.new_design_family,
            proposal.new_design_treatment,
        ),
    ):
        family = REGISTRY.get(family_id or "")
        if family is None or treatment_id not in {item.id for item in family.treatments}:
            raise ValueError(f"{beat_id} needs a registered family and treatment")
        minimum, maximum = role_capacity(family_id, treatment_id)
        if (
            [unit for group in groups for unit in group] != unit_ids
            or any(not group for group in groups)
            or not minimum <= len(groups) <= maximum
            or len(groups) != len(labels)
            or len(labels) != len(anchors)
        ):
            raise ValueError(f"{beat_id} reveal roles do not partition its spoken units")
        for group, anchor in zip(groups, anchors, strict=True):
            group_words = [
                word for unit_id in group for word in spoken_words(unit_lookup[unit_id]["text"])
            ]
            unique_phrase_position(group_words, anchor)
    asset = next(
        (asset for asset in result.longform_visual_plan.assets if asset.id == original.asset_ref),
        None,
    )
    if asset is None or asset.kind != "code_rendered" or original.id not in asset.beat_ids:
        raise ValueError("approved code asset is unavailable for the split")
    new_duration = round(original.duration * proposal.new_duration_fraction, 8)
    first_duration = original.duration - new_duration
    if min(first_duration, new_duration) < 2:
        raise ValueError("both planned beats need at least two seconds")
    first = original.model_copy(
        update={
            "duration": first_duration,
            "spoken_unit_ids": first_units,
            "role_unit_groups": first_groups,
            "role_semantics": [],
            "visual_type": proposal.first_visual_type or original.visual_type,
            "design_family": proposal.first_design_family or original.design_family,
            "design_treatment": proposal.first_design_treatment or original.design_treatment,
            "purpose": proposal.first_purpose,
            "screen_title": proposal.first_screen_title,
            "design_rationale": proposal.first_design_rationale,
            "overlay_text": first_labels,
            "overlay_anchors": first_anchors,
        }
    )
    second = original.model_copy(
        update={
            "id": proposal.new_id,
            "start": original.start + first_duration,
            "duration": new_duration,
            "spoken_unit_ids": second_units,
            "role_unit_groups": second_groups,
            "role_semantics": [],
            "visual_type": proposal.new_visual_type,
            "design_family": proposal.new_design_family,
            "design_treatment": proposal.new_design_treatment,
            "design_rationale": proposal.new_design_rationale,
            "purpose": proposal.new_purpose,
            "screen_title": proposal.new_screen_title,
            "overlay_text": second_labels,
            "overlay_anchors": second_anchors,
            "narration_anchor": proposal.new_narration_anchor,
        }
    )
    chapter.visual_beats[index : index + 1] = [first, second]
    validate_unit_coverage(chapter, chapter.visual_beats)
    asset.beat_ids.append(second.id)
    # Each repair adds precisely one beat. Allow two independently audited
    # treatment-capacity repairs on this already approved plan; the generator's
    # policy remains unchanged and no other chapter is replanned.
    old_count = sum(len(c.visual_beats) for c in project.spec.chapters)
    new_count = sum(len(c.visual_beats) for c in result.spec.chapters)
    if (
        new_count != old_count + 1
        or new_count > result.longform_visual_policy.maximum_visual_beats + 2
    ):
        raise ValueError("split exceeds the single-beat repair allowance")
    result.spec = LongFormSpec.model_validate(result.spec.model_dump(mode="json"))
    result.longform_visual_plan = LongFormVisualPlan.model_validate(
        result.longform_visual_plan.model_dump(mode="json")
    )
    result = VideoProject.model_validate(result.model_dump(mode="json"))
    windows = narration_windows(chapter, result.longform_voices)
    speech_windows = [
        {
            "block_id": item.id,
            "start": start,
            "end": end,
            "words": result.longform_word_timings[item.id],
        }
        for item, _, start, end in windows
    ]
    plan, _ = plan_motion_chapter(
        result.model_dump(mode="json"),
        chapter.id,
        speech_windows,
        width=result.spec.brief.width,
        height=result.spec.brief.height,
        fps=result.spec.brief.fps,
        family_first=True,
    )
    shots = {shot.id: shot for shot in plan.shots}
    if not (
        shots[first.id].end == shots[second.id].start
        and shots[second.id].end > shots[second.id].start
    ):
        raise ValueError("measured motion plan did not produce a contiguous split")
    validate_measured_reveal_tails(
        {
            "visual_beats": [first.model_dump(mode="json"), second.model_dump(mode="json")],
            "narration_blocks": [block.model_dump(mode="json")],
        },
        plan,
        speech_windows,
    )
    return result, {
        "chapter_id": chapter.id,
        "original_id": first.id,
        "new_id": second.id,
        "split_anchor": proposal.split_anchor,
        "spoken_split_seconds": split_at,
        "measured_shots": {
            beat_id: {"start": shots[beat_id].start, "end": shots[beat_id].end}
            for beat_id in (first.id, second.id)
        },
        "beat_count_before": old_count,
        "beat_count_after": new_count,
        "policy_maximum": result.longform_visual_policy.maximum_visual_beats,
        "script_unchanged": True,
        "asset_reused": asset.id,
    }


def prepare_retarget(project: VideoProject, proposal: BeatRetarget) -> tuple[VideoProject, dict]:
    """Replace one impossible treatment, preserving speech, beat count and assets."""
    if project.state != State.LONGFORM_SEMANTIC_PLANNING or project.pipeline != "live":
        raise ValueError("beat repair requires a live semantic-planning checkpoint")
    if not isinstance(project.spec, LongFormSpec) or project.longform_visual_plan is None:
        raise ValueError("beat repair requires an approved long-form visual plan")
    result = project.model_copy(deep=True)
    matches = [
        (chapter, beat)
        for chapter in result.spec.chapters
        for beat in chapter.visual_beats
        if beat.id == proposal.beat_id
    ]
    if len(matches) != 1:
        raise ValueError("retargeted beat must exist exactly once")
    chapter, beat = matches[0]
    if chapter.id in result.longform_semantic_decisions:
        raise ValueError("cannot replace a chapter with committed semantic decisions")
    if beat.visual_type in {"authentic_image", "generated_image"}:
        raise ValueError("cannot retarget an approved image beat")
    if proposal.new_visual_type in {"authentic_image", "generated_image"}:
        raise ValueError("a code-rendered repair cannot become an image beat")
    if len(beat.overlay_text) != len(beat.overlay_anchors):
        raise ValueError("original beat has mismatched spoken roles")
    asset = next(
        (asset for asset in result.longform_visual_plan.assets if asset.id == beat.asset_ref),
        None,
    )
    if asset is None or asset.kind != "code_rendered" or beat.id not in asset.beat_ids:
        raise ValueError("approved code asset is unavailable for retargeting")
    old_family, old_treatment = beat.design_family, beat.design_treatment
    beat.visual_type = proposal.new_visual_type
    beat.design_family = proposal.new_design_family
    beat.design_treatment = proposal.new_design_treatment
    beat.design_rationale = proposal.new_design_rationale
    beat.purpose = proposal.new_purpose
    beat.screen_title = proposal.new_screen_title
    if proposal.new_overlay_text is not None:
        beat.overlay_text = proposal.new_overlay_text
    if proposal.new_overlay_anchors is not None:
        beat.overlay_anchors = proposal.new_overlay_anchors
    if proposal.new_role_unit_groups is not None:
        beat.role_unit_groups = proposal.new_role_unit_groups
    beat.role_semantics = []
    from autonomous_studio.rendering.visual_registry import role_capacity

    minimum, maximum = role_capacity(beat.design_family, beat.design_treatment)
    groups = beat.role_unit_groups
    if (
        not minimum <= len(groups) <= maximum
        or [unit for group in groups for unit in group] != beat.spoken_unit_ids
        or len(groups) != len(beat.overlay_text)
        or len(groups) != len(beat.overlay_anchors)
    ):
        raise ValueError("retargeted reveal roles do not partition the spoken units")
    units = {unit["id"]: unit for unit in chapter_units(chapter)}
    for group, anchor in zip(groups, beat.overlay_anchors, strict=True):
        group_words = [word for unit_id in group for word in spoken_words(units[unit_id]["text"])]
        unique_phrase_position(group_words, anchor)
    result.spec = LongFormSpec.model_validate(result.spec.model_dump(mode="json"))
    result = VideoProject.model_validate(result.model_dump(mode="json"))
    windows = narration_windows(chapter, result.longform_voices)
    speech_windows = [
        {
            "block_id": item.id,
            "start": start,
            "end": end,
            "words": result.longform_word_timings[item.id],
        }
        for item, _, start, end in windows
    ]
    plan, _ = plan_motion_chapter(
        result.model_dump(mode="json"),
        chapter.id,
        speech_windows,
        width=result.spec.brief.width,
        height=result.spec.brief.height,
        fps=result.spec.brief.fps,
        family_first=True,
    )
    shot = next(shot for shot in plan.shots if shot.id == beat.id)
    window = next(
        item for item in speech_windows if item["block_id"] == beat.narration_block_ids[0]
    )
    block = next(item for item in chapter.narration_blocks if item.id == window["block_id"])
    entrances = [
        phrase_start(block.text, anchor, window["words"], offset=window["start"])
        for anchor in beat.overlay_anchors
    ]
    if (
        entrances != sorted(set(entrances))
        or entrances[0] > shot.start + 2.5
        or entrances[-1] > shot.end - 0.28
    ):
        raise ValueError("retargeted reveal roles do not fit the measured shot")
    validate_measured_reveal_tails(
        {
            "visual_beats": [beat.model_dump(mode="json")],
            "narration_blocks": [block.model_dump(mode="json")],
        },
        plan,
        speech_windows,
    )
    return result, {
        "chapter_id": chapter.id,
        "beat_id": beat.id,
        "from": [old_family, old_treatment],
        "to": [beat.design_family, beat.design_treatment],
        "measured_shot": {"start": shot.start, "end": shot.end},
        "measured_entrances": entrances,
        "script_unchanged": True,
        "asset_reused": asset.id,
    }


def main():
    parser = argparse.ArgumentParser(description="Version a single approved long-form beat split")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    repo = Repository(args.root)
    payload = json.loads(args.proposal.read_text(encoding="utf-8"))
    with repo.lock(args.project):
        project = repo.load(args.project)
        if "beat_id" in payload:
            proposal = BeatRetarget.model_validate(payload)
            result, report = prepare_retarget(project, proposal)
        else:
            proposal = BeatSplit.model_validate(payload)
            result, report = prepare_split(project, proposal)
        if args.commit:
            stage = "visual-beat-retarget" if "beat_id" in payload else "visual-beat-split"
            output = repo.attempt_dir(project, stage)
            write_json(output / "proposal.json", proposal.model_dump(mode="json"))
            write_json(output / "report.json", report)
            for name, role in (
                ("proposal.json", "longform_beat_repair_proposal"),
                ("report.json", "longform_beat_repair_report"),
            ):
                result.artifacts.append(repo.artifact(result, output / name, role))
            repo.save(result, stage.replace("-", "_"), report)
        print(json.dumps({**report, "committed": args.commit}, indent=2))


if __name__ == "__main__":
    main()
