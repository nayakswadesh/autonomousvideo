"""Deterministic timing for semantic objects in editorial Short scenes.

The shot's authored motion remains the opt-out: hold means no object entrance.
All objects settle early enough to leave reading time before the shot ends.
Explicit phrase cues use the approved transcript and measured voice duration. They
are proportional phrase estimates, not phoneme/word forced alignment.
"""


def cue_onsets(project, shot) -> list[float]:
    """Estimate phrase starts within measured narration; reject ambiguous missing cues."""
    if not shot.entrance_cues:
        return []
    segments = sorted(shot.voice_segments, key=lambda item: item.offset)
    if not segments:
        raise ValueError(f"{shot.id}: entrance cues require narration")
    starts = []
    previous = -1.0
    for cue in shot.entrance_cues:
        if not cue.strip():
            raise ValueError(f"{shot.id}: empty entrance cue")
        candidates = []
        for index, voice in enumerate(segments):
            transcript = voice.spoken_text.casefold()
            at = transcript.find(cue.casefold())
            if at < 0:
                continue
            prepared = next(
                (
                    item
                    for item in project.prepared_voices
                    if (item.shot_id, item.segment_index) == (shot.id, index)
                ),
                None,
            )
            duration = prepared.asset.duration if prepared else voice.estimated_duration
            # Word position is more stable than raw characters for an estimate;
            # punctuation and long words otherwise skew the entrance.
            before = len(transcript[:at].split())
            total = max(1, len(transcript.split()))
            candidates.append(voice.offset + duration * before / total)
        later = [item for item in candidates if item > previous]
        if not later:
            raise ValueError(f"{shot.id}: entrance cue missing or out of order: {cue}")
        onset = later[0]
        starts.append(onset)
        previous = onset
    return starts


def object_count(shot) -> int:
    return {
        "comparison": lambda: len(shot.values),
        "data_chart": lambda: len(shot.values),
        "timeline": lambda: len(shot.labels),
        "qualitative_comparison": lambda: len(shot.alternatives),
        "study_design": lambda: len(shot.steps),
        "metric_pair": lambda: len(shot.metrics),
        "metric_set": lambda: len(shot.labels),
        "big_metric": lambda: 1,
        "map": lambda: 1,
    }.get(shot.kind, lambda: 0)()


def _ease(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def _window(duration: float, index: int, count: int) -> tuple[float, float]:
    if count < 1:
        raise ValueError("object count must be positive")
    fade = min(0.5, max(0.18, duration * 0.08))
    first = min(0.35, duration * 0.08)
    last = max(first, min(3.5, duration * 0.48) - fade)
    onset = first + (last - first) * index / max(1, count - 1)
    return onset, fade


def reveal(
    local_time: float,
    duration: float,
    index: int,
    count: int,
    *,
    motion: str,
    onset: float | None = None,
) -> float:
    if motion == "hold":
        return 1.0
    default_onset, fade = _window(duration, index, count)
    onset = default_onset if onset is None else onset
    return _ease((local_time - onset) / fade)


def timeline_motion_supported(labels: list[str]) -> bool:
    """Do not imply that mixed dates and measurements form a sequence of events."""
    return len(labels) >= 2 and all("—" in label or ":" in label for label in labels)


def motion_sample_progresses(shot, project=None) -> list[float]:
    """Inspect moving labels during entry as well as after their settled pose."""
    if shot.motion == "hold":
        return []
    if shot.kind == "timeline" and not timeline_motion_supported(shot.labels):
        return []
    count = object_count(shot)
    if not count:
        return []
    onsets = cue_onsets(project, shot) if project is not None else []
    if onsets and len(onsets) != count:
        raise ValueError(f"{shot.id}: entrance cue count must match visible objects")
    # A twenty-item chart does not need forty extra full-HD layout samples;
    # its object boxes are fixed, so inspect the first, middle and final rows.
    indices = range(count) if count <= 4 else sorted({0, count // 2, count - 1})
    points = []
    for index in indices:
        default, fade = _window(shot.duration, index, count)
        onset = onsets[index] if onsets else default
        for local in (onset + fade * 0.5, onset + fade):
            points.append(min(0.999999, local / shot.duration))
    return sorted(set(points))
