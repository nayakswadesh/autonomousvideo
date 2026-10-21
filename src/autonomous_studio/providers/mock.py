from __future__ import annotations

import sys
import wave
from array import array
from math import exp, pi, sin
from pathlib import Path

from autonomous_studio.domain.models import (
    AmbienceAsset,
    Brief,
    HeroShot,
    MetricShot,
    MusicAsset,
    MusicSpec,
    NumericClaim,
    RankShot,
    SfxAsset,
    SoundCue,
    SourceShot,
    TitleShot,
    VideoProject,
    VideoSpec,
    VoiceAsset,
    VoiceSegment,
    VoiceSpec,
    validate_story_scope,
    validate_timeline,
)
from autonomous_studio.storage import Repository


class MockResearch:
    async def research(self, brief: Brief):
        return brief.research.model_copy(deep=True)


def voice_segment(shot_id: str, text: str, voice: VoiceSpec) -> VoiceSegment:
    return VoiceSegment(
        shot_id=shot_id,
        spoken_text=text,
        estimated_duration=max(0.35, len(text.split()) * 60 / voice.pace_wpm),
        delivery_direction=voice.style,
        important_pronunciations=voice.pronunciation_notes,
    )


class MockDirector:
    """Deterministic fixture director. Free-text feedback is recorded, not interpreted by an LLM."""

    async def develop(self, brief: Brief, feedback: list[str]) -> VideoSpec:
        if brief.shots:
            shots = [s.model_copy(deep=True) for s in brief.shots]
        elif brief.production_mode == "full" and brief.research.ranking:
            subjects = sorted(
                (s for s in brief.research.subjects if s.rank is not None),
                key=lambda s: s.rank,
                reverse=True,
            )
            if not subjects:
                raise ValueError("full ranked fixture requires ranked research subjects")
            frame = brief.fps
            total = round(brief.duration * frame)
            hook_frames = round(5 * frame)
            source_frames = round(6 * frame)
            outro_frames = round(4 * frame)
            available = total - hook_frames - source_frames - outro_frames
            each, remainder = divmod(available, len(subjects))
            counts = [hook_frames] + [each + (i < remainder) for i in range(len(subjects))]
            counts += [outro_frames, source_frames]
            starts, cursor = [], 0
            for count in counts:
                starts.append(cursor / frame)
                cursor += count
            shots = [
                TitleShot(
                    id="hook",
                    start=starts[0],
                    duration=counts[0] / frame,
                    title=brief.hook,
                    subtitle=brief.thesis,
                    voice_segments=[voice_segment("hook", brief.hook_narration, brief.voice)],
                )
            ]
            for index, subject in enumerate(subjects, 1):
                claim = next(c for c in brief.research.claims if c.id == subject.claim_id)
                metric = f"{claim.value:,.0f}" if isinstance(claim, NumericClaim) else "CONTEXT"
                shot_id = f"rank-{subject.rank}"
                narration = subject.narration
                if isinstance(claim, NumericClaim):
                    narration += f" About {claim.value:,.0f} {claim.unit}."
                shots.append(
                    RankShot(
                        id=shot_id,
                        start=starts[index],
                        duration=counts[index] / frame,
                        title=subject.title,
                        subtitle=subject.subtitle,
                        rank=subject.rank,
                        metric=metric,
                        motion="push",
                        claim_ids=[claim.id],
                        source_ids=claim.source_ids,
                        voice_segments=[voice_segment(shot_id, narration, brief.voice)],
                        sound_cues=[SoundCue(shot_id=shot_id)],
                        music_state="rise" if subject.rank == 1 else "bed",
                    )
                )
            outro_index = len(subjects) + 1
            shots.extend(
                [
                    TitleShot(
                        id="payoff",
                        start=starts[outro_index],
                        duration=counts[outro_index] / frame,
                        title=brief.payoff,
                        subtitle="Every estimate needs context.",
                        voice_segments=[voice_segment("payoff", brief.payoff, brief.voice)],
                        music_state="reduce",
                    ),
                    SourceShot(
                        id="sources",
                        start=starts[outro_index + 1],
                        duration=counts[outro_index + 1] / frame,
                        title="SOURCES AND CAVEATS",
                        subtitle="Review the evidence before publication.",
                        source_ids=list(
                            dict.fromkeys(
                                source
                                for subject in subjects
                                for source in next(
                                    c for c in brief.research.claims if c.id == subject.claim_id
                                ).source_ids
                            )
                        ),
                        intentional_silence=True,
                        music_state="silence",
                        motion="fade",
                    ),
                ]
            )
        else:
            frame = brief.fps
            total = round(brief.duration * frame)
            # One countdown excerpt: hook, #5, its metric, source context.
            boundaries = [0, round(total * 0.24), round(total * 0.55), round(total * 0.79), total]
            times = [
                (boundaries[i] / frame, (boundaries[i + 1] - boundaries[i]) / frame)
                for i in range(4)
            ]
            subject = max(brief.research.subjects, key=lambda s: s.rank or 0)
            claim = next(c for c in brief.research.claims if c.id == subject.claim_id)
            metric = f"{claim.value:,.0f}" if isinstance(claim, NumericClaim) else "CONTEXT"
            details = dict(
                id="subject",
                start=times[1][0],
                duration=times[1][1],
                title=subject.title,
                subtitle=subject.subtitle,
                motion="push",
                claim_ids=[claim.id],
                source_ids=claim.source_ids,
                voice_segments=[voice_segment("subject", subject.narration, brief.voice)],
                sound_cues=[SoundCue(shot_id="subject")],
            )
            middle = (
                RankShot(**details, rank=subject.rank, metric=metric)
                if subject.rank
                else HeroShot(**details)
            )
            shots = [
                TitleShot(
                    id="hook",
                    start=0,
                    duration=times[0][1],
                    title=brief.hook,
                    subtitle="A HUMAN-IMPACT COUNTDOWN" if subject.rank else brief.thesis,
                    voice_segments=[voice_segment("hook", brief.hook_narration, brief.voice)],
                ),
                middle,
                MetricShot(
                    id="metric",
                    start=times[2][0],
                    duration=times[2][1],
                    title=claim.unit.upper() if isinstance(claim, NumericClaim) else "CONTEXT",
                    subtitle="Reported estimate / see research notes",
                    metric=metric,
                    claim_ids=[claim.id],
                    source_ids=claim.source_ids,
                    motion="magnify",
                    music_state="reduce",
                )
                if isinstance(claim, NumericClaim)
                else TitleShot(
                    id="context",
                    start=times[2][0],
                    duration=times[2][1],
                    title=claim.text,
                    subtitle="A sourced perspective",
                    source_ids=claim.source_ids,
                    claim_ids=[claim.id],
                    music_state="reduce",
                ),
                SourceShot(
                    id="sources",
                    start=times[3][0],
                    duration=times[3][1],
                    title="BEHIND THE NUMBERS",
                    subtitle="Read the evidence and context.",
                    source_ids=claim.source_ids,
                    music_state="reduce",
                    motion="fade",
                ),
            ]
        validate_timeline(shots, brief.duration, brief.research)
        spec = VideoSpec(
            brief=brief,
            hook=brief.hook,
            thesis=brief.thesis,
            payoff=brief.payoff,
            narrative_beats=[s.title for s in shots],
            shots=shots,
        )
        validate_story_scope(spec)
        return spec


def tone(path: Path, duration: float, kind: str, intensity=1.0, tempo=72):
    """Original deterministic PCM fixtures; voice is intentionally a timing tone, not speech."""
    rate = 48000
    samples = array("h")
    for i in range(round(duration * rate)):
        t = i / rate
        envelope = min(1.0, t / 0.03, max(0, (duration - t) / 0.05))
        if kind == "voice":
            syllable = t % 0.32
            envelope *= 1 if syllable < 0.22 else 0
            value = 0.28 * (sin(2 * pi * 180 * t) + 0.25 * sin(2 * pi * 360 * t))
        elif kind == "music":
            value = 0.20 * (sin(2 * pi * 110 * t) + 0.3 * sin(2 * pi * 165 * t))
            envelope *= 0.7 + 0.3 * sin(pi * t * tempo / 60) ** 2
        elif kind == "sfx":
            value = 0.3 * sin(2 * pi * (90 * t - 25 * t * t)) * exp(-9 * t)
        else:
            value = 0.12 * (sin(2 * pi * 55 * t) + 0.3 * sin(2 * pi * 57 * t))
        samples.append(round(32767 * value * envelope * intensity))
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(samples.tobytes())


class MockAudio:
    def __init__(self, repository: Repository):
        self.repo = repository

    async def synthesize(self, project, segment: VoiceSegment, spec: VoiceSpec, start, output):
        tone(output, segment.estimated_duration, "voice")
        artifact = self.repo.artifact(
            project,
            output,
            "voice",
            provider="mock-tts",
            model="timing-tone-v1",
            duration=segment.estimated_duration,
        )
        return VoiceAsset(
            artifact=artifact,
            start=start,
            duration=segment.estimated_duration,
            spoken_text=segment.spoken_text,
            characters=len(segment.spoken_text),
            voice_id=spec.voice_id,
        )

    async def create_or_select(self, project, spec: MusicSpec, output):
        tone(output, project.spec.brief.duration, "music", tempo=spec.tempo)
        artifact = self.repo.artifact(
            project, output, "music", provider="mock-music", duration=project.spec.brief.duration
        )
        return MusicAsset(
            artifact=artifact, start=0, duration=project.spec.brief.duration, mood=spec.mood
        )

    async def create(self, project, cue: SoundCue, start, output):
        tone(output, cue.duration, "sfx", cue.intensity)
        artifact = self.repo.artifact(
            project, output, "sfx", provider="mock-sfx", duration=cue.duration
        )
        return SfxAsset(
            artifact=artifact, start=start, duration=cue.duration, cue_type=cue.cue_type
        )

    async def ambience(self, project: VideoProject, output: Path):
        duration = project.spec.brief.duration
        tone(output, duration, "ambience")
        artifact = self.repo.artifact(
            project, output, "ambience", provider="mock-ambience", duration=duration
        )
        return AmbienceAsset(artifact=artifact, start=0, duration=duration)
