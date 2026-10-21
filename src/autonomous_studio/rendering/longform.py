"""FFmpeg chapter renderer: one encoded stream per chapter, no Python frame loop."""

from __future__ import annotations

import hashlib
import json
import math
import re
from difflib import SequenceMatcher
from pathlib import Path

from autonomous_studio.audio.alignment import tokens
from autonomous_studio.audio.mixer import align_pcm_duration, pcm_metrics
from autonomous_studio.domain.models import hashable_longform_chapter
from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.rendering.longform_motion import (
    PRODUCTION_MOTION_VERSION,
    render_motion_silent,
)
from autonomous_studio.storage.repository import write_json
from autonomous_studio.workflow.motion_planning import MOTION_PLANNER_VERSION, plan_motion_chapter

RENDERER_VERSION = "lf-v0.5-family-first-1"

_LANDING_GAPS = {
    "ordinary": 0.12,
    "idea_landing": 0.65,
    "chapter_turn": 0.9,
}


def landing_gap_seconds(block):
    """Editorial gap after a complete thought; old blocks retain the ordinary join."""
    return _LANDING_GAPS[getattr(block, "landing_after", "ordinary")]


def narration_required_duration(windows):
    """Include a deliberate final landing when fitting a chapter to its timeline."""
    if not windows:
        return 0.0
    block, _, _, end = windows[-1]
    return end + (
        landing_gap_seconds(block)
        if getattr(block, "landing_after", "ordinary") != "ordinary" else 0.0
    )


def chapter_music_offset(project, chapter, music):
    if not music or not music.duration:
        return 0.0
    elapsed = sum(
        item.target_duration for item in project.spec.chapters if item.order < chapter.order
    )
    return elapsed % music.duration


def chapter_input_hash(project, chapter, music):
    data = {
        "renderer": RENDERER_VERSION,
        "motion_planner": MOTION_PLANNER_VERSION,
        "motion_renderer": PRODUCTION_MOTION_VERSION,
        "format": {
            "width": project.spec.brief.width,
            "height": project.spec.brief.height,
            "fps": project.spec.brief.fps,
        },
        "chapter": hashable_longform_chapter(chapter),
        "visuals": {
            beat.asset_ref: project.longform_media[beat.asset_ref].sha256
            for beat in chapter.visual_beats
        },
        "voices": {
            block.id: project.longform_voices[block.id].artifact.sha256
            for block in chapter.narration_blocks
        },
        "word_timings": {
            block.id: project.longform_word_timings.get(block.id, [])
            for block in chapter.narration_blocks
        },
        "music": music.sha256 if music else None,
        "music_offset": chapter_music_offset(project, chapter, music),
        "mix": project.spec.brief.mix.model_dump(mode="json"),
        "captions": project.spec.closed_captions,
    }
    if chapter.id in project.longform_semantic_decisions:
        from pydantic import TypeAdapter

        from autonomous_studio.rendering.semantic_diagrams import Scene, scene_asset_report

        adapter = TypeAdapter(Scene)
        decisions = project.longform_semantic_decisions[chapter.id]
        data["semantic"] = {
            "decisions": decisions.model_dump(mode="json"),
            "input_hash": project.longform_semantic_hashes.get(chapter.id),
            "assets": {
                decision.beat_id: scene_asset_report(adapter.validate_json(decision.scene_json))
                for decision in decisions.decisions
                if decision.mode == "diagram"
            },
        }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def narration_windows(chapter, voices, *, strict=True):
    """Align measured speech to the first visual referring to each block, never overlapping it."""

    def schedule(align_visuals):
        windows = []
        cursor = 0.12
        for block in chapter.narration_blocks:
            first_visual = min(
                (
                    beat.start
                    for beat in chapter.visual_beats
                    if block.id in beat.narration_block_ids
                ),
                default=cursor,
            )
            voice = voices[block.id]
            start = max(cursor, first_visual) if align_visuals else cursor
            end = start + voice.duration
            windows.append((block, voice, start, end))
            cursor = end + landing_gap_seconds(block)
        return windows

    windows = schedule(True)
    if windows and narration_required_duration(windows) > chapter.target_duration - 0.1:
        # The approved visuals are not speech timing constraints. Let speech start
        # over the preceding still when that saves a near-miss without truncation.
        windows = schedule(False)
    if strict and windows and narration_required_duration(windows) > chapter.target_duration - 0.1:
        raise ValueError(
            f"Measured narration does not fit chapter {chapter.order}: "
            f"{narration_required_duration(windows):.1f}s within "
            f"{chapter.target_duration:.1f}s. "
            "Revise the script or approve an expanded timeline; speech was not cut or sped up."
        )
    return windows


def _srt_time(seconds):
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    whole, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{whole:02},{millis:03}"


def _caption_chunks(text):
    """Small, clause-aware groups; em dashes can form a natural speech break."""
    words = text.replace("—", "— ").split()
    chunks, current = [], []
    for word in words:
        current.append(word)
        if word.endswith("—"):
            if chunks and len(current) < 3 and len(chunks[-1]) + len(current) <= 10:
                chunks[-1].extend(current)
            else:
                chunks.append(current)
            current = []
        elif len(current) >= 8 or (len(current) >= 4 and re.search(r"[.!?;:]$", word)):
            chunks.append(current)
            current = []
    if current:
        if chunks and len(current) < 3 and len(chunks[-1]) + len(current) <= 10:
            chunks[-1].extend(current)
        else:
            chunks.append(current)
    return chunks


def _timed_caption_block(text, timestamps, start, end):
    chunks = _caption_chunks(text)
    if not timestamps:
        raise ValueError("Word timings are required for long-form captions")
    words = [word for chunk in chunks for word in chunk]
    scripted, script_owner = [], []
    observed, observed_owner = [], []
    for index, word in enumerate(words):
        for token in tokens(word):
            scripted.append(token)
            script_owner.append(index)
    for index, item in enumerate(timestamps):
        for token in tokens(str(item["word"])):
            observed.append(token)
            observed_owner.append(index)
    matcher = SequenceMatcher(None, scripted, observed, autojunk=False)
    if matcher.ratio() < 0.72:
        raise ValueError("Caption word alignment differs too much from approved narration")
    aligned = {}
    for left, right, count in matcher.get_matching_blocks():
        for offset in range(count):
            aligned[script_owner[left + offset]] = observed_owner[right + offset]
    if not aligned:
        raise ValueError("Caption word alignment has no matching speech")
    result, cursor = [], start
    word_cursor = 0
    for chunk in chunks:
        indices = [aligned[i] for i in range(word_cursor, word_cursor + len(chunk)) if i in aligned]
        if not indices:
            raise ValueError("A caption phrase could not be aligned to speech")
        first, last = min(indices), max(indices)
        cue_start = max(cursor, start + float(timestamps[first]["start"]))
        cue_end = min(end, start + float(timestamps[last]["end"]) + 0.12)
        if cue_end <= cue_start:
            cue_end = min(end, cue_start + 0.08)
        if cue_end <= cue_start:
            raise ValueError("Caption cue has no duration")
        result.append((cue_start, cue_end, " ".join(chunk).replace("— ", "—")))
        cursor = cue_start + 0.01
        word_cursor += len(chunk)
    # End an earlier cue at the next phrase onset rather than leave the old
    # caption visible while the next phrase is already being spoken.
    return [
        (a, min(b, result[index + 1][0]) if index + 1 < len(result) else b, caption)
        for index, (a, b, caption) in enumerate(result)
    ]


def write_captions(windows, path, word_timings):
    cues = []
    for block, _, start, end in windows:
        cues.extend(_timed_caption_block(block.text, word_timings.get(block.id, []), start, end))
    path.write_text(
        "\n\n".join(
            f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}"
            for index, (start, end, text) in enumerate(cues, 1)
        )
        + "\n",
        encoding="utf-8",
    )
    return len(cues)


def _subtitle_filter(path):
    escaped = path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")
    return (
        f"subtitles='{escaped}':force_style='FontName=Arial,FontSize=24,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00101923,Outline=2,"
        "Alignment=2,MarginV=55'"
    )


def _duck_expression(windows, gain_db, attack, release):
    lowered = 10 ** (gain_db / 20)
    expression = "1"
    for _, _, start, end in windows:
        a = max(0.001, attack / 1000)
        r = max(0.001, release / 1000)
        window = (
            f"if(lt(t,{start}),1,if(lt(t,{start + a}),"
            f"1-(1-{lowered})*(t-{start})/{a},if(lt(t,{end}),{lowered},"
            f"if(lt(t,{end + r}),{lowered}+(1-{lowered})*(t-{end})/{r},1))))"
        )
        expression = f"min({expression},{window})"
    return expression


def render_chapter(
    repo, project, chapter, music, output: Path, *, ffmpeg=None, motion_plan_override=None
):
    ffmpeg = ffmpeg or FFmpeg()
    output.mkdir(parents=True, exist_ok=True)
    brief = project.spec.brief
    windows = narration_windows(chapter, project.longform_voices)
    fps = brief.fps
    silent = output / "visual.mp4"
    captions = output / "captions.srt"
    cue_count = 0
    if project.spec.closed_captions:
        cue_count = write_captions(windows, captions, project.longform_word_timings)
    motion_plan = None
    from autonomous_studio.workflow.semantic_integration import (
        family_first_chapter,
        require_family_first_decisions,
    )

    require_family_first_decisions(project, chapter)
    if project.pipeline == "live" and family_first_chapter(chapter) and not getattr(
        project, "longform_visual_plan", None
    ):
        raise ValueError("family-first chapter needs an approved visual plan")
    if getattr(project, "longform_visual_plan", None) is not None:
        speech_windows = [
            {
                "block_id": block.id,
                "start": start,
                "end": end,
                "words": project.longform_word_timings.get(block.id, []),
            }
            for block, _, start, end in windows
        ]
        if motion_plan_override is None:
            motion_plan, report = plan_motion_chapter(
                project.model_dump(mode="json"),
                chapter.id,
                speech_windows,
                comparison_name=f"production-{chapter.id}",
                width=brief.width,
                height=brief.height,
                fps=fps,
                family_first=project.pipeline == "live" and family_first_chapter(chapter),
            )
            if chapter.id in project.longform_semantic_decisions:
                from autonomous_studio.workflow.semantic_integration import (
                    apply_semantic_decisions,
                    semantic_input_hash,
                )

                batch = project.longform_semantic_decisions[chapter.id]
                if project.longform_semantic_hashes.get(chapter.id) != semantic_input_hash(
                    project, chapter, motion_plan, schema_version=batch.schema_version
                ):
                    raise ValueError("semantic decisions no longer match approved chapter inputs")
                motion_plan, decision_report = apply_semantic_decisions(
                    project.model_dump(mode="json"),
                    chapter.id,
                    motion_plan,
                    speech_windows,
                    project.longform_semantic_decisions[chapter.id],
                )
                write_json(output / "semantic-decision-report.json", decision_report)
                report = {**report, "semantic_applied": decision_report["applied"]}
        else:
            from autonomous_studio.domain.motion import validate_motion_plan

            motion_plan = motion_plan_override
            validate_motion_plan(motion_plan, project.model_dump(mode="json"), speech_windows)
            if project.pipeline == "live" and family_first_chapter(chapter) and any(
                shot.kind not in {"semantic_diagram", "camera", "detail_reveal", "guided_image"}
                for shot in motion_plan.shots
            ):
                raise ValueError("family-first render override retained a legacy designed shot")
            report = {
                "planner": "semantic-opt-in",
                "chapter_id": chapter.id,
                "source": "approved narration and measured word timing",
            }
        write_json(output / "motion-plan.json", motion_plan.model_dump(mode="json"))
        write_json(output / "motion-planner-report.json", report)
        render_motion_silent(
            repo,
            project,
            motion_plan,
            silent,
            ffmpeg=ffmpeg,
            captions_path=captions if project.spec.closed_captions else None,
        )
    else:
        visuals, filters = [], []
        for index, beat in enumerate(chapter.visual_beats):
            picture = repo.verify(project, project.longform_media[beat.asset_ref])
            frames = round(beat.duration * fps)
            visuals += [
                "-loop",
                "1",
                "-framerate",
                str(fps),
                "-t",
                str(beat.duration),
                "-i",
                picture,
            ]
            treatment = getattr(beat, "treatment", "hold")
            if treatment in {"ken_burns", "detail_crop", "parallax"}:
                motion = (
                    f"zoompan=z='min(zoom+0.0005,1.08)':"
                    f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:"
                    f"s=1920x1080:fps={fps}"
                )
            else:
                motion = "scale=1920:1080"
            filters.append(
                f"[{index}:v]fps={fps},{motion},trim=end_frame={frames},setpts=PTS-STARTPTS[v{index}]"
            )
        filters.append(
            "".join(f"[v{i}]" for i in range(len(chapter.visual_beats)))
            + f"concat=n={len(chapter.visual_beats)}:v=1:a=0[sequence]"
        )
        filters.append(
            f"[sequence]{_subtitle_filter(captions)}[finished]"
            if project.spec.closed_captions
            else "[sequence]null[finished]"
        )
        ffmpeg.run(
            [
                *visuals,
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[finished]",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "21",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(fps),
                "-movflags",
                "+faststart",
                silent,
            ],
            timeout=900,
        )

    # Mix each complete chapter; duck music under the measured narration windows.
    voice_inputs = []
    afilters = []
    for index, (_, voice, start, _) in enumerate(windows):
        path = repo.verify(project, voice.artifact)
        if pcm_metrics(path)["clipped_samples"]:
            raise ValueError("narration input clips")
        voice_inputs += ["-i", path]
        afilters.append(
            f"[{index}:a]aresample=48000,adelay={round(start * 1000)}:all=1,"
            f"volume={brief.mix.voice_gain_db}dB,"
            f"apad=whole_dur={chapter.target_duration},atrim=duration={chapter.target_duration}"
            f"[voice{index}]"
        )
    afilters.append(
        "".join(f"[voice{i}]" for i in range(len(windows)))
        + f"amix=inputs={len(windows)}:normalize=0:duration=longest[spoken]"
    )
    policy = brief.mix
    music_input = []
    if music:
        music_input = [
            "-stream_loop",
            "-1",
            "-ss",
            str(chapter_music_offset(project, chapter, music)),
            "-i",
            repo.verify(project, music),
        ]
        music_index = len(windows)
        envelope = (
            _duck_expression(
                windows, policy.duck_gain_db, policy.duck_attack_ms, policy.duck_release_ms
            )
            if policy.music_ducking
            else "1"
        )
        first = not project.spec.chapters or chapter.order == project.spec.chapters[0].order
        last = not project.spec.chapters or chapter.order == project.spec.chapters[-1].order
        fades = (f",afade=t=in:st=0:d={policy.fade_in_ms / 1000}" if first else "") + (
            f",afade=t=out:st={max(0, chapter.target_duration - policy.fade_out_ms / 1000)}:"
            f"d={policy.fade_out_ms / 1000}"
            if last
            else ""
        )
        afilters.append(
            f"[{music_index}:a]aresample=48000,atrim=duration={chapter.target_duration},"
            f"volume={policy.music_gain_db}dB,aeval='val(0)*({envelope})'"
            f"{fades}[bed]"
        )
        afilters.append("[spoken][bed]amix=inputs=2:normalize=0:duration=longest[raw]")
    else:
        afilters.append("[spoken]anull[raw]")
    afilters.append(
        f"[raw]loudnorm=I={policy.target_loudness}:TP={policy.true_peak_db}:LRA=11,"
        f"aresample=48000,apad=whole_dur={chapter.target_duration},"
        f"atrim=duration={chapter.target_duration},asetpts=PTS-STARTPTS[master]"
    )
    mixed = output / "mix.wav"
    ffmpeg.run(
        [
            *voice_inputs,
            *music_input,
            "-filter_complex",
            ";".join(afilters),
            "-map",
            "[master]",
            "-t",
            str(chapter.target_duration),
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            mixed,
        ],
        timeout=900,
    )
    audio_alignment = align_pcm_duration(mixed, chapter.target_duration)
    metrics = pcm_metrics(mixed)
    if metrics["clipped_samples"]:
        raise ValueError("chapter audio clips")
    if abs(metrics["duration"] - chapter.target_duration) > 0.04:
        raise ValueError("chapter audio duration differs from its video")
    movie = output / "chapter.mp4"
    ffmpeg.mux(silent, mixed, movie)
    info = ffmpeg.inspect(movie, timeout=900)
    if (info["width"], info["height"]) != (brief.width, brief.height) or not info["has_audio"]:
        raise ValueError("chapter MP4 failed video/audio validation")
    if not math.isclose(info["duration"], chapter.target_duration, abs_tol=0.15):
        raise ValueError("chapter MP4 duration is invalid")
    qa = {
        "chapter_id": chapter.id,
        "video": info,
        "audio": metrics,
        "audio_alignment_seconds": audio_alignment,
        "captions_enabled": project.spec.closed_captions,
        "music_offset": chapter_music_offset(project, chapter, music),
        "caption_cues": cue_count,
        "motion_planner": MOTION_PLANNER_VERSION if motion_plan else None,
        "motion_shots": len(motion_plan.shots) if motion_plan else 0,
        "speech_windows": [
            {"block_id": b.id, "start": start, "end": end} for b, _, start, end in windows
        ],
    }
    write_json(output / "qa.json", qa)
    silent.unlink()
    mixed.unlink()
    return movie, qa


def assemble_review(repo, project, output: Path, *, ffmpeg=None):
    ffmpeg = ffmpeg or FFmpeg()
    output.mkdir(parents=True, exist_ok=True)
    playlist = output / "chapters.txt"
    lines = []
    for chapter in project.spec.chapters:
        path = repo.verify(project, project.longform_chapters[chapter.id])
        lines.append("file '" + path.as_posix().replace("'", "'\\''") + "'")
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    movie = output / "review.mp4"
    ffmpeg.run(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            playlist,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            movie,
        ],
        timeout=900,
    )
    info = ffmpeg.inspect(movie, timeout=900)
    brief = project.spec.brief
    if (
        not info["decodable"]
        or not info["has_audio"]
        or (info["width"], info["height"]) != (brief.width, brief.height)
        or abs(info["duration"] - brief.duration) > 0.3
    ):
        raise ValueError("long-form review MP4 failed duration, audio or dimensions validation")
    write_json(output / "qa.json", {"video": info, "chapters": len(lines), "passed": True})
    return movie, info


class LongFormRenderer:
    def __init__(self, repo, ffmpeg=None):
        self.repo = repo
        self.ffmpeg = ffmpeg or FFmpeg()

    def chapter(self, project, chapter, music, output):
        return render_chapter(self.repo, project, chapter, music, output, ffmpeg=self.ffmpeg)

    def review(self, project, output):
        return assemble_review(self.repo, project, output, ffmpeg=self.ffmpeg)
