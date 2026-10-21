"""Report long reveal gaps without mistaking a timing proxy for a bad scene.

The script-only pass is an estimate. The measured pass runs after TTS, before
buying semantic scenes; all passes keep semantic anchor validation strict.
"""

from __future__ import annotations

import logging

from autonomous_studio.audio.alignment import phrase_start
from autonomous_studio.workflow.spoken_units import (
    beat_words,
    combined_speech,
    unique_phrase_position,
)

MAX_INACTIVE_END_SECONDS = 8.5
ESTIMATE_TOLERANCE_SECONDS = 0.5
LOGGER = logging.getLogger(__name__)


def estimated_beat_windows(chapter, beats, fps: int) -> list[dict]:
    """Use the same word-weighted frame allocation as visual materialization."""
    frames = round(chapter.target_duration * fps)
    weights = [max(3, len(beat_words(chapter, beat))) for beat in beats]
    allocated = 0
    result = []
    for index, (beat, weight) in enumerate(zip(beats, weights, strict=True)):
        duration_frames = (
            frames - allocated
            if index == len(beats) - 1
            else max(1, round(frames * weight / sum(weights)))
        )
        remaining = len(beats) - index - 1
        duration_frames = min(duration_frames, frames - allocated - remaining)
        result.append(
            {
                "beat_id": beat.id,
                "estimated_seconds": duration_frames / fps,
                "spoken_words": len(beat_words(chapter, beat)),
            }
        )
        allocated += duration_frames
    return result


def warn_reveal_gap(beat_id: str, stage: str, position: str, seconds: float, limit: float) -> None:
    LOGGER.warning(
        "%s %s reveal gap %s %.2fs exceeds %.2fs design target; review visual activity",
        beat_id, stage, position, seconds, limit,
    )


def _designed_with_reveals(beat) -> bool:
    value = beat.get if isinstance(beat, dict) else lambda key: getattr(beat, key)
    return bool(value("design_family") and value("spoken_unit_ids") and value("overlay_anchors"))


def validate_estimated_reveal_tails(chapter, beats, fps: int) -> None:
    """Warn about long estimated gaps; still reject invalid spoken anchor order.

    Match materialize_visual_plan's word-weighted frame allocation. This does
    not pretend to know TTS speed; the measured preflight checks it again.
    """
    for beat, window in zip(beats, estimated_beat_windows(chapter, beats, fps), strict=True):
        if not _designed_with_reveals(beat):
            continue
        spoken = beat_words(chapter, beat)
        positions = [unique_phrase_position(spoken, anchor) for anchor in beat.overlay_anchors]
        if positions != sorted(set(positions)):
            raise ValueError(f"{beat.id} planned reveals must follow speech order")
        opening = window["estimated_seconds"] * positions[0] / len(spoken)
        if opening > 2.5 + ESTIMATE_TOLERANCE_SECONDS:
            warn_reveal_gap(beat.id, "estimated", "opening", opening, 2.5)
        middle = [
            window["estimated_seconds"] * (right - left) / len(spoken)
            for left, right in zip(positions, positions[1:])
        ]
        for gap in middle:
            if gap > MAX_INACTIVE_END_SECONDS + ESTIMATE_TOLERANCE_SECONDS:
                warn_reveal_gap(beat.id, "estimated", "middle", gap, MAX_INACTIVE_END_SECONDS)
        last = positions[-1]
        tail = window["estimated_seconds"] * (len(spoken) - last) / len(spoken)
        if tail > MAX_INACTIVE_END_SECONDS + ESTIMATE_TOLERANCE_SECONDS:
            warn_reveal_gap(beat.id, "estimated", "ending", tail, MAX_INACTIVE_END_SECONDS)


def validate_measured_reveal_tails(
    chapter: dict,
    base_plan,
    speech_windows: list[dict],
    *,
    planning_allowance_seconds: float = 0.0,
) -> None:
    """Preflight spoken order and report measured timing advisories."""
    shots = {shot.id: shot for shot in base_plan.shots}
    windows = {window["block_id"]: window for window in speech_windows}
    for beat in chapter["visual_beats"]:
        if not _designed_with_reveals(beat):
            continue
        shot = shots[beat["id"]]
        if shot.kind in {"camera", "detail_reveal", "guided_image"}:
            continue
        speech, window = combined_speech(chapter, beat, windows)
        entrances = [
            phrase_start(speech["text"], anchor, window["words"], offset=window["start"])
            for anchor in beat["overlay_anchors"]
        ]
        if entrances != sorted(set(entrances)):
            raise ValueError(f"{beat['id']} measured reveals must follow speech order")
        limit = MAX_INACTIVE_END_SECONDS + planning_allowance_seconds
        opening = entrances[0] - shot.start if hasattr(shot, "start") else 0.0
        if opening > 2.5 + planning_allowance_seconds:
            warn_reveal_gap(beat["id"], "measured", "opening", opening, 2.5)
        for left, right in zip(entrances, entrances[1:]):
            if right - left > limit:
                warn_reveal_gap(
                    beat["id"], "measured", "middle", right - left, MAX_INACTIVE_END_SECONDS
                )
        last = entrances[-1]
        tail = shot.end - last
        if tail > limit:
            warn_reveal_gap(beat["id"], "measured", "ending", tail, MAX_INACTIVE_END_SECONDS)


def validate_saved_speech_against_visual_plan(project, spec, plan) -> None:
    """Use already prepared TTS when an existing project's visuals are replanned.

    New projects have no audio yet and use only the script estimate at this gate.
    The later semantic-stage preflight always repeats the measured check.
    """
    from autonomous_studio.rendering.longform import narration_windows
    from autonomous_studio.workflow.motion_planning import plan_motion_chapter

    block_ids = {block.id for chapter in spec.chapters for block in chapter.narration_blocks}
    if (
        not block_ids <= project.longform_voices.keys()
        or not block_ids <= project.longform_word_timings.keys()
    ):
        return
    candidate = project.model_copy(deep=True)
    candidate.spec = spec
    candidate.longform_visual_plan = plan
    payload = candidate.model_dump(mode="json")
    # The motion planner needs asset identities, not acquired pixels, to test
    # where each approved speech beat will start and end.
    payload["longform_media"] = {asset.id: {} for asset in plan.assets}
    for chapter in spec.chapters:
        windows = narration_windows(chapter, candidate.longform_voices)
        speech_windows = [
            {
                "block_id": block.id,
                "start": start,
                "end": end,
                "words": candidate.longform_word_timings[block.id],
            }
            for block, _, start, end in windows
        ]
        base, _ = plan_motion_chapter(
            payload,
            chapter.id,
            speech_windows,
            comparison_name=f"visual-review-{chapter.id}",
            width=spec.brief.width,
            height=spec.brief.height,
            fps=spec.brief.fps,
            family_first=True,
        )
        validate_measured_reveal_tails(
            chapter.model_dump(mode="json"),
            base,
            speech_windows,
            planning_allowance_seconds=ESTIMATE_TOLERANCE_SECONDS,
        )
