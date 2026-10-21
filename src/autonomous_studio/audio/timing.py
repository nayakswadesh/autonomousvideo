"""Measured narration planning, independent of speech providers and workflow/UI adapters."""

import hashlib
import json
from math import ceil

from autonomous_studio.domain.models import validate_timeline

MAX_TIMING_REPAIRS = 2
SHORT_TARGET_SECONDS = 60
SHORT_AUTO_MAX_SECONDS = 90
SHORT_REVIEW_MAX_SECONDS = 180


class NarrationTimingError(ValueError):
    pass


def is_timing_failure(project):
    return project.last_error_code == "narration_timing" or (
        "synthesized narration exceeds" in (project.last_error or "")
    )


def voice_signature(project, segment):
    # Timing is intentionally excluded: an unchanged recording can be moved without resynthesis.
    if project.creation_settings.get("voice_signature_schema") == 3:
        # Older schemas remain byte-for-byte compatible with saved recordings.
        provider = project.spec.brief.voice.provider
        models = {
            "provider": provider,
            "tts_model": project.live_models.get(
                "longform_tts_model" if provider == "gemini" else "tts_model"
            ),
        }
    elif project.creation_settings.get("voice_signature_schema") == 2:
        # Text-role changes cannot alter an already synthesized recording.
        models = {
            "tts_model": project.live_models.get("tts_model"),
            "voice": project.live_models.get("voice"),
        }
    else:
        # Legacy signatures included every live model. Preserve their approved
        # hashes after a text-role migration, but still invalidate a real TTS
        # or voice change.
        models = dict(
            project.creation_settings["voice_signature_legacy_models"]
            if "voice_signature_legacy_models" in project.creation_settings
            else project.live_models
        )
        for key in ("tts_model", "voice"):
            if key in project.live_models:
                models[key] = project.live_models[key]
    data = {
        "voice": project.spec.brief.voice.model_dump(mode="json"),
        "models": models,
        "text": segment.spoken_text,
        "delivery": segment.delivery_direction,
        "pronunciation": segment.important_pronunciations,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def measured_plan(project, *, limit_seconds=None):
    """Return a reflowed copy, a complete measurement report, and whether approved timing fails.

    Preserve silent/source-card duration and existing shot durations wherever possible. Grow to
    the selected production mode's approved maximum, using whole frames. Never speed up, crop
    speech or hide pauses. Full Shorts can grow to 90 seconds without changing their script;
    a longer timeline requires a separate runtime approval.
    A None spec means narration needs rewriting, not unbounded timeline expansion.
    """
    spec = project.spec.model_copy(deep=True)
    measurements = {(v.shot_id, v.segment_index): v for v in project.prepared_voices}
    report, required_frames, preferred_frames = [], [], []
    mismatch = False
    fps = spec.brief.fps
    for shot in spec.shots:
        cursor = 0.0
        for index, segment in enumerate(sorted(shot.voice_segments, key=lambda v: v.offset)):
            measured = measurements[(shot.id, index)]
            if measured.signature != voice_signature(project, segment):
                raise ValueError("prepared narration no longer matches its script")
            actual = measured.asset.duration
            available = shot.duration - segment.offset - segment.pause_after
            overlaps = segment.offset < cursor - 1e-5
            mismatch |= actual > available + 0.001 or overlaps
            report.append(
                {
                    "shot_id": shot.id,
                    "segment_index": index,
                    "text": segment.spoken_text,
                    "estimated_seconds": segment.estimated_duration,
                    "actual_seconds": actual,
                    "available_seconds": available,
                    "overrun_seconds": max(0, actual - available),
                    "overlap": overlaps,
                    "artifact_id": measured.asset.artifact.id,
                }
            )
            segment.offset = max(segment.offset, cursor)
            segment.estimated_duration = actual
            cursor = segment.offset + actual + segment.pause_after
        # Spoken timing is authoritative. A decorative cue near the old scene ending must not
        # force a valid script into another paid rewrite; it can be moved to the new ending after
        # the scene durations are solved. Silent scenes retain their authored duration.
        minimum = max(cursor + 0.1, 1.5) if shot.voice_segments else shot.duration
        required_frames.append(ceil(minimum * fps - 1e-6))
        preferred_frames.append(ceil(max(shot.duration, minimum) * fps - 1e-6))
    if limit_seconds is None:
        limit_seconds = (
            15
            if spec.brief.production_mode == "prototype"
            else max(SHORT_AUTO_MAX_SECONDS, spec.brief.duration)
        )
    limit = limit_seconds * fps
    if sum(required_frames) > limit:
        return None, report, mismatch
    frames = preferred_frames[:]
    # Only reclaim surplus from narrated shots; never steal time from source/silent cards.
    excess = max(0, sum(frames) - limit)
    for index in range(len(frames) - 1, -1, -1):
        reclaim = min(excess, frames[index] - required_frames[index])
        frames[index] -= reclaim
        excess -= reclaim
    cursor = 0
    for shot, count in zip(spec.shots, frames, strict=True):
        shot.start, shot.duration = cursor / fps, count / fps
        for cue in shot.sound_cues:
            cue.duration = min(cue.duration, shot.duration)
            cue.offset = min(cue.offset, max(0, shot.duration - cue.duration))
        cursor += count
    spec.brief.duration = cursor / fps
    # A supplied YAML timeline must agree with the revised declarative timeline too.
    if spec.brief.shots:
        spec.brief.shots = [s.model_copy(deep=True) for s in spec.shots]
    validate_timeline(spec.shots, spec.brief.duration, spec.brief.research)
    return spec, report, mismatch
