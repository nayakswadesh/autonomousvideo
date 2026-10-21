"""Durable long-form audio, visual and chapter production checkpoints."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path

from autonomous_studio.audio.alignment import estimated_words, phrase_start
from autonomous_studio.domain.formats import get_format_profile
from autonomous_studio.domain.models import (
    LongFormRuntimeProposal,
    LongFormSpec,
    VoiceSegment,
    now,
)
from autonomous_studio.providers.gemini_tts import gemini_cache_path, gemini_speech_payload
from autonomous_studio.providers.mock import tone
from autonomous_studio.providers.music_catalog import select_music
from autonomous_studio.providers.openai import speech_payload
from autonomous_studio.rendering.longform import (
    chapter_input_hash,
    narration_required_duration,
    narration_windows,
)
from autonomous_studio.storage.repository import digest, write_json
from autonomous_studio.workflow.motion_planning import plan_motion_chapter
from autonomous_studio.workflow.semantic_integration import (
    apply_semantic_decisions,
    family_first_chapter,
    require_family_first_decisions,
    semantic_input_hash,
    validate_decision_inputs,
)


def _checkpoint_json(studio, project, output, stem, value, role):
    """Retries must never overwrite a previously hashed production artifact."""
    signature = hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]
    path = output / f"{stem}-{signature}.json"
    prior = next(
        (
            artifact
            for artifact in project.artifacts
            if artifact.role == role
            and artifact.path == path.relative_to(studio.repo.project_dir(project.id)).as_posix()
        ),
        None,
    )
    if prior is not None:
        studio.repo.verify(project, prior)
    else:
        write_json(path, value)
        project.artifacts.append(studio.repo.artifact(project, path, role))
    return path


def _voice_signature(project, block):
    provider = project.spec.brief.voice.provider
    return hashlib.sha256(
        json.dumps(
            {
                "text": block.text,
                "direction": block.delivery_direction,
                "voice": project.spec.brief.voice.model_dump(mode="json"),
                "model": (
                    project.live_models.get("longform_tts_model", "gemini-3.8-flash-tts")
                    if provider == "gemini"
                    else project.live_models.get("tts_model", "mock-tts")
                ),
                "fresh_media_epoch": project.creation_settings.get("fresh_media_epoch"),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _allocate_frames(total: int, minimums: list[int], weights: list[float]) -> list[int]:
    spare = total - sum(minimums)
    if spare < 0:
        raise ValueError("narration requires more frames than the available timeline")
    weights = [max(0.0, weight) for weight in weights]
    if not any(weights):
        weights = [1.0] * len(minimums)
    exact = [spare * weight / sum(weights) for weight in weights]
    portions = [math.floor(value) for value in exact]
    for index in sorted(range(len(exact)), key=lambda i: exact[i] - portions[i], reverse=True)[
        : spare - sum(portions)
    ]:
        portions[index] += 1
    return [minimum + portion for minimum, portion in zip(minimums, portions, strict=True)]


class RuntimeExtensionRequired(ValueError):
    def __init__(self, proposal: LongFormRuntimeProposal):
        self.proposal = proposal
        super().__init__(
            f"Measured narration needs at least {proposal.measured_minimum_seconds:.1f}s, "
            f"above the saved {proposal.prior_ceiling_seconds:.1f}s planning ceiling. "
            f"Keep this script at about {proposal.proposed_seconds:.0f}s or trim it."
        )


def retime_longform_narration(project):
    """Fit measured speech by reallocating still time; never cut or accelerate narration."""
    spec = project.spec.model_copy(deep=True)
    fps = spec.brief.fps
    old_frames = [round(chapter.target_duration * fps) for chapter in spec.chapters]
    minimums = []
    spoken_ends = []
    for chapter in spec.chapters:
        windows = narration_windows(chapter, project.longform_voices, strict=False)
        end = narration_required_duration(windows)
        spoken_ends.append(end)
        minimums.append(math.ceil((end + 0.16) * fps))
    previous = sum(old_frames)
    ceiling = math.floor(spec.brief.duration_policy.expansion_maximum_seconds * fps)
    needed = sum(minimums)
    if needed > ceiling:
        minimum_seconds = needed / fps
        # Leave real room for transitions and visual comprehension; this is a
        # measured production proposal, not a claim about ideal YouTube length.
        buffer = max(12, minimum_seconds * 0.04)
        proposed = math.ceil((minimum_seconds + buffer) / 5) * 5
        get_format_profile("longform_pilot").validate_duration(proposed)
        raise RuntimeExtensionRequired(
            LongFormRuntimeProposal(
                version=project.version,
                original_seconds=previous / fps,
                prior_ceiling_seconds=ceiling / fps,
                measured_speech_seconds=sum(
                    voice.duration for voice in project.longform_voices.values()
                ),
                measured_minimum_seconds=minimum_seconds,
                proposed_seconds=proposed,
                chapter_minimums={
                    chapter.id: minimum / fps
                    for chapter, minimum in zip(spec.chapters, minimums, strict=True)
                },
            )
        )
    approved = project.longform_runtime_approved_seconds or 0
    linked = bool(project.creation_settings.get("narration_linked_timeline"))
    if linked:
        # A planned chapter length is not permission for a long music-only tail.
        # Keep a short visual breath, while preserving every synthesized word.
        chapter_frames = [
            max(minimum, min(old, math.ceil((end + 3.0) * fps)))
            for old, minimum, end in zip(old_frames, minimums, spoken_ends, strict=True)
        ]
        required_total = math.ceil(spec.brief.duration_policy.minimum_seconds * fps)
        if sum(chapter_frames) < required_total:
            chapter_frames = _allocate_frames(
                required_total,
                chapter_frames,
                [max(1, old - new) for old, new in zip(old_frames, chapter_frames, strict=True)],
            )
        total = sum(chapter_frames)
    else:
        chapter_frames = []
        total = min(ceiling, max(previous, needed + round(0.4 * fps), round(approved * fps)))
    if (
        not linked and approved <= previous / fps or linked and chapter_frames == old_frames
    ) and all(minimum <= old for minimum, old in zip(minimums, old_frames, strict=True)):
        return {
            "changed": False,
            "original_seconds": previous / fps,
            "measured_minimum_seconds": needed / fps,
        }
    if total < needed:
        total = needed
    if not linked:
        chapter_frames = _allocate_frames(
            total,
            minimums,
            [max(0, old - minimum) for old, minimum in zip(old_frames, minimums, strict=True)],
        )
    changes = []
    for chapter, old, new in zip(spec.chapters, old_frames, chapter_frames, strict=True):
        weights = [round(beat.duration * fps) for beat in chapter.visual_beats]
        beat_frames = _allocate_frames(new, [1] * len(weights), weights)
        cursor = 0
        for beat, count in zip(chapter.visual_beats, beat_frames, strict=True):
            beat.start = cursor / fps
            beat.duration = count / fps
            cursor += count
        chapter.target_duration = new / fps
        changes.append(
            {"chapter_id": chapter.id, "from_seconds": old / fps, "to_seconds": new / fps}
        )
    spec.brief.duration_policy.target_seconds = total / fps
    spec.brief.duration_policy.maximum_seconds = max(
        spec.brief.duration_policy.maximum_seconds, total / fps
    )
    project.spec = LongFormSpec.model_validate(spec.model_dump(mode="json"))
    return {
        "changed": True,
        "original_seconds": previous / fps,
        "new_seconds": total / fps,
        "measured_minimum_seconds": needed / fps,
        "chapters": changes,
        "speech_cut_or_accelerated": False,
    }


async def prepare_longform_audio(studio, project):
    if not project.longform_visual_plan:
        raise ValueError("visual plan is required before narration synthesis")
    output = studio.repo.project_dir(project.id) / "audio" / f"v{project.version:03d}"
    output.mkdir(parents=True, exist_ok=True)
    if project.longform_music:
        studio.repo.verify(project, project.longform_music)
    elif project.pipeline == "mock":
        path = output / "music-bed.wav"
        tone(path, 30, "music", tempo=project.spec.brief.music.tempo)
        artifact = studio.repo.artifact(
            project, path, "longform_music", provider="mock-music", duration=30
        )
        project.longform_music = artifact
        project.artifacts.append(artifact)
        studio.repo.save(
            project, "longform_music_selected", {"track": "mock", "version": project.version}
        )
    else:
        from autonomous_studio.config import Settings

        settings = studio.settings or Settings.environment()
        catalog = Path(settings.music_catalog).resolve()
        mood = project.spec.brief.music.mood.casefold()
        tags = [
            tag for tag in ("reflective", "tense", "uplifting", "neutral", "ambient") if tag in mood
        ] or ["reflective"]
        track, source, evidence, scores = select_music(
            catalog,
            tags,
            spec=project.spec.brief.music,
            required_duration=max(chapter.target_duration for chapter in project.spec.chapters),
        )
        path = output / f"music-{track.id}{source.suffix}"
        shutil.copyfile(source, path)
        if digest(path) != track.sha256:
            raise ValueError("music changed during project copy")
        artifact = studio.repo.artifact(
            project,
            path,
            "longform_music",
            provider="music-catalog",
            duration=track.duration_seconds,
        )
        artifact.provenance = track.provenance.model_copy(deep=True)
        license_copy = output / "music-license.md"
        shutil.copyfile(evidence, license_copy)
        project.artifacts.append(studio.repo.artifact(project, license_copy, "license_evidence"))
        selection = output / "music-selection.json"
        write_json(selection, {"track": track.model_dump(mode="json"), "scores": scores})
        project.artifacts.append(
            studio.repo.artifact(project, selection, "longform_music_selection")
        )
        project.longform_music = artifact
        project.artifacts.append(artifact)
        studio.repo.save(
            project, "longform_music_selected", {"track": track.id, "version": project.version}
        )
    for chapter in project.spec.chapters:
        for block in chapter.narration_blocks:
            signature = _voice_signature(project, block)
            prior = project.longform_voices.get(block.id)
            if prior:
                if prior.artifact.input_reference != signature:
                    raise ValueError("prepared narration differs from the approved script")
                studio.repo.verify(project, prior.artifact)
                continue
            segment = VoiceSegment(
                shot_id=block.id,
                spoken_text=block.text,
                estimated_duration=block.estimated_duration,
                delivery_direction=block.delivery_direction,
            )
            path = output / f"{block.id}.wav"
            journal = output / f"{block.id}.job.json"
            if project.pipeline == "live":
                if project.spec.brief.voice.provider == "gemini":
                    payload = gemini_speech_payload(
                        project.live_models.get("longform_tts_model", "gemini-3.8-flash-tts"),
                        segment,
                        project.spec.brief.voice,
                    )
                    cache = gemini_cache_path(
                        studio.live_client.cache, payload,
                        project.creation_settings.get("fresh_media_epoch"),
                    )
                else:
                    payload = speech_payload(
                        studio.live_client.settings.tts_model,
                        segment,
                        project.spec.brief.voice,
                        consistent_delivery=True,
                    )
                    cache_key = hashlib.sha256(
                        json.dumps(
                            {
                                "payload": payload,
                                "fresh_media_epoch": project.creation_settings.get("fresh_media_epoch"),
                            },
                            sort_keys=True,
                        ).encode()
                    ).hexdigest()
                    cache = studio.live_client.cache / f"tts-{cache_key}.wav"
                if journal.exists() and not cache.is_file():
                    raise ValueError(
                        f"Narration request for {block.id} has no receipt and may have incurred "
                        "usage; inspect the cache before replaying it"
                    )
                if not cache.is_file():
                    write_json(journal, {"signature": signature, "reserved_at": now()})
                    studio.repo.save(
                        project,
                        "longform_voice_request_reserved",
                        {
                            "block_id": block.id,
                            "signature": signature,
                            "version": project.version,
                        },
                    )
            voice = await studio.voice.synthesize(
                project, segment, project.spec.brief.voice, 0, path
            )
            voice.artifact.input_reference = signature
            project.longform_voices[block.id] = voice
            project.artifacts.append(voice.artifact)
            studio.repo.save(
                project,
                "longform_voice_prepared",
                {
                    "block_id": block.id,
                    "version": project.version,
                    "duration": voice.duration,
                },
            )
    for chapter in project.spec.chapters:
        for block in chapter.narration_blocks:
            if block.id in project.longform_word_timings:
                continue
            voice = project.longform_voices[block.id]
            voice_path = studio.repo.verify(project, voice.artifact)
            if project.pipeline == "live" and studio.live_client is not None:
                words = await studio.live_client.transcribe_words(voice_path)
                provider = "openai-whisper-1"
            else:
                words = estimated_words(block.text, voice.duration)
                provider = "fixture-estimate"
            path = output / f"alignment-{block.id}.json"
            write_json(path, {"block_id": block.id, "provider": provider, "words": words})
            project.longform_word_timings[block.id] = words
            project.artifacts.append(
                studio.repo.artifact(project, path, "longform_word_alignment", provider=provider)
            )
            studio.repo.save(
                project,
                "longform_word_alignment_prepared",
                {"block_id": block.id, "version": project.version, "provider": provider},
            )
    repair = retime_longform_narration(project)
    _checkpoint_json(
        studio,
        project,
        output,
        "measured-timeline-repair",
        repair,
        "longform_timing_repair",
    )
    if repair["changed"]:
        project.production_changes.append(
            f"Measured narration retimed chapter stills from {repair['original_seconds']:.1f}s "
            f"to {repair['new_seconds']:.1f}s; no speech cut or accelerated."
        )
    studio.repo.save(project, "longform_timeline_repaired", repair)
    for chapter in project.spec.chapters:
        narration_windows(chapter, project.longform_voices)
        blocks = {block.id: block for block in chapter.narration_blocks}
        for beat in chapter.visual_beats:
            if not beat.narration_anchor:
                continue  # Historical and mock plans predate phrase-linked planning.
            if beat.spoken_unit_ids:
                from autonomous_studio.workflow.spoken_units import (
                    beat_words,
                    unique_phrase_position,
                )

                spoken = beat_words(chapter, beat)
                positions = [
                    unique_phrase_position(spoken, phrase)
                    for phrase in [beat.narration_anchor, *beat.overlay_anchors]
                ]
                if positions[1:] != sorted(positions[1:]):
                    raise ValueError(
                        f"Visual labels for {beat.id} disagree with approved speech order"
                    )
                continue  # Measured entrance validation is performed by the motion preflight.
            block = blocks[beat.narration_block_ids[0]]
            words = project.longform_word_timings[block.id]
            starts = [
                phrase_start(block.text, phrase, words)
                for phrase in [beat.narration_anchor, *beat.overlay_anchors]
            ]
            if starts[1:] != sorted(starts[1:]):
                raise ValueError(f"Visual labels for {beat.id} disagree with measured speech order")
    # Mock fixtures are deliberately coarse and may use a replacement renderer.
    # Live media production must pass the exact planner before paid images begin.
    if project.pipeline == "live":
        preflight_longform_motion(project)
    report_payload = {
        "version": project.version,
        "chapters": [
            {
                "chapter_id": chapter.id,
                "target_duration": chapter.target_duration,
                "windows": [
                    {
                        "block_id": block.id,
                        "start": start,
                        "end": end,
                        "measured": voice.duration,
                    }
                    for block, voice, start, end in narration_windows(
                        chapter, project.longform_voices
                    )
                ],
            }
            for chapter in project.spec.chapters
        ],
    }
    _checkpoint_json(
        studio,
        project,
        output,
        "measured-narration",
        report_payload,
        "longform_narration_validation",
    )
    studio.repo.save(project, "longform_narration_validated", {"version": project.version})


def preflight_longform_motion(project):
    """Validate measured visual cues before any paid long-form image request."""
    from autonomous_studio.workflow.motion_planning import plan_motion_chapter

    preview_project = project.model_dump(mode="json")
    preview_project["longform_media"] = {
        asset.id: {} for asset in project.longform_visual_plan.assets
    }
    for chapter in project.spec.chapters:
        windows = [
            {
                "block_id": block.id,
                "start": start,
                "end": end,
                "words": project.longform_word_timings[block.id],
            }
            for block, _, start, end in narration_windows(chapter, project.longform_voices)
        ]
        base, _ = plan_motion_chapter(
            preview_project,
            chapter.id,
            windows,
            width=project.spec.brief.width,
            height=project.spec.brief.height,
            fps=project.spec.brief.fps,
            family_first=project.pipeline == "live" and family_first_chapter(chapter),
        )
        if (
            project.longform_creative_policy is not None
            and project.longform_creative_policy.schema_version >= 2
        ):
            from autonomous_studio.workflow.visual_reveal_timing import (
                ESTIMATE_TOLERANCE_SECONDS,
                validate_measured_reveal_tails,
            )

            validate_measured_reveal_tails(
                chapter.model_dump(mode="json"),
                base,
                windows,
                planning_allowance_seconds=ESTIMATE_TOLERANCE_SECONDS,
            )


async def prepare_longform_visual_assets(studio, project):
    from autonomous_studio.providers.longform_media import acquire_longform_media

    await acquire_longform_media(studio, project)


async def prepare_longform_semantic_decisions(studio, project, *, max_new_chapters=None):
    """Persist each chapter's decisions before rendering; retries never re-buy them."""
    if not hasattr(project, "longform_semantic_models"):
        project.longform_semantic_models = {}
    if project.pipeline != "live":
        project.longform_semantic_decisions = {}
        project.longform_semantic_hashes = {}
        project.longform_semantic_models = {}
        return
    new_chapters = 0
    for chapter in project.spec.chapters:
        windows = narration_windows(chapter, project.longform_voices)
        speech_windows = [
            {
                "block_id": block.id,
                "start": start,
                "end": end,
                "words": project.longform_word_timings.get(block.id, []),
            }
            for block, _, start, end in windows
        ]
        base, _ = plan_motion_chapter(
            project.model_dump(mode="json"),
            chapter.id,
            speech_windows,
            comparison_name=f"production-{chapter.id}",
            width=project.spec.brief.width,
            height=project.spec.brief.height,
            fps=project.spec.brief.fps,
            family_first=project.pipeline == "live" and family_first_chapter(chapter),
        )
        batch = project.longform_semantic_decisions.get(chapter.id)
        if batch is not None and chapter.id not in project.longform_semantic_models:
            # Chapters saved before stage-specific routing used director_model.
            project.longform_semantic_models[chapter.id] = project.live_models.get(
                "director_model", "mock-director"
            )
        key = semantic_input_hash(
            project, chapter, base, schema_version=batch.schema_version if batch else 2
        )
        reused = batch is not None and project.longform_semantic_hashes.get(chapter.id) == key
        if not reused:
            if project.creation_settings.get("semantic_replay_only_version") == project.version:
                raise ValueError(
                    f"{chapter.id} saved semantic inputs changed; this rebuild must not "
                    "silently issue new model or illustration requests"
                )
            from autonomous_studio.workflow.visual_reveal_timing import (
                ESTIMATE_TOLERANCE_SECONDS,
                validate_measured_reveal_tails,
            )

            validate_measured_reveal_tails(
                chapter.model_dump(mode="json"),
                base,
                speech_windows,
                planning_allowance_seconds=ESTIMATE_TOLERANCE_SECONDS,
            )
            from autonomous_studio.providers.semantic_director import OpenAIFamilyDirector

            if isinstance(studio.longform_semantic_director, OpenAIFamilyDirector):
                from autonomous_studio.providers.semantic_illustrations import SemanticIllustrations

                batch = await studio.longform_semantic_director.propose(
                    project.model_dump(mode="json"),
                    chapter.id,
                    base,
                    speech_windows,
                    asset_resolver=SemanticIllustrations(studio, project),
                )
            else:
                batch = await studio.longform_semantic_director.propose(
                    project.model_dump(mode="json"), chapter.id, base, speech_windows
                )
            project.longform_semantic_models[chapter.id] = project.live_models.get(
                "semantic_layout_model", project.live_models.get("director_model", "mock-director")
            )
            key = semantic_input_hash(project, chapter, base, schema_version=batch.schema_version)
        if family_first_chapter(chapter) and batch.schema_version != 2:
            raise ValueError(f"family-first chapter {chapter.id} needs v2 semantic decisions")
        validate_decision_inputs(project.model_dump(mode="json"), chapter.id, batch, base)
        plan, report = apply_semantic_decisions(
            project.model_dump(mode="json"), chapter.id, base, speech_windows, batch
        )
        project.longform_semantic_decisions[chapter.id] = batch
        project.longform_semantic_hashes[chapter.id] = key
        if not reused or not any(
            artifact.role == f"longform_semantic_decisions_{chapter.id}"
            for artifact in project.current_artifacts()
        ):
            output = studio.repo.attempt_dir(project, f"semantic-{chapter.id}")
            write_json(output / "decisions.json", batch.model_dump(mode="json"))
            write_json(output / "decision-report.json", report)
            write_json(output / "motion-plan.json", plan.model_dump(mode="json"))
            for name, role in (
                ("decisions.json", f"longform_semantic_decisions_{chapter.id}"),
                ("decision-report.json", f"longform_semantic_report_{chapter.id}"),
                ("motion-plan.json", f"longform_semantic_plan_{chapter.id}"),
            ):
                project.artifacts.append(studio.repo.artifact(project, output / name, role))
            studio.repo.save(
                project,
                "longform_semantic_planned",
                {"chapter_id": chapter.id, "input_hash": key, "applied": report["applied"]},
            )
            new_chapters += 1
            if max_new_chapters is not None and new_chapters >= max_new_chapters:
                return


def render_longform_chapters(studio, project):
    renderer = studio.longform_renderer
    for chapter in project.spec.chapters:
        require_family_first_decisions(project, chapter)
        key = chapter_input_hash(project, chapter, project.longform_music)
        previous = project.longform_chapters.get(chapter.id)
        if previous and project.longform_chapter_hashes.get(chapter.id) == key:
            studio.repo.verify(project, previous)
            continue
        output = studio.repo.attempt_dir(project, f"chapter-{chapter.order}")
        movie, qa = renderer.chapter(project, chapter, project.longform_music, output)
        artifact = studio.repo.artifact(
            project,
            movie,
            "longform_chapter",
            provider="local",
            model="lf-v0.5-auto-motion",
            duration=chapter.target_duration,
            width=project.spec.brief.width,
            height=project.spec.brief.height,
        )
        artifact.input_reference = key
        project.longform_chapters[chapter.id] = artifact
        project.longform_chapter_hashes[chapter.id] = key
        project.artifacts.append(artifact)
        qa_path = output / "qa.json"
        project.artifacts.append(studio.repo.artifact(project, qa_path, "longform_chapter_qa"))
        if (output / "motion-plan.json").is_file():
            project.artifacts.append(
                studio.repo.artifact(project, output / "motion-plan.json", "longform_motion_plan")
            )
            project.artifacts.append(
                studio.repo.artifact(
                    project, output / "motion-planner-report.json", "longform_motion_report"
                )
            )
        if (output / "semantic-decision-report.json").is_file():
            project.artifacts.append(
                studio.repo.artifact(
                    project,
                    output / "semantic-decision-report.json",
                    "longform_semantic_render_report",
                )
            )
        studio.repo.save(
            project,
            "longform_chapter_rendered",
            {
                "chapter_id": chapter.id,
                "version": project.version,
                "input_hash": key,
                "duration": qa["video"]["duration"],
            },
        )


async def prepare_longform_review(studio, project):
    output = studio.repo.attempt_dir(project, "longform-review")
    movie, info = studio.longform_renderer.review(project, output)
    video = studio.repo.artifact(
        project,
        movie,
        "prototype",
        provider="local",
        model="lf-v0.5-auto-motion-review",
        duration=info["duration"],
        width=info["width"],
        height=info["height"],
    )
    project.artifacts.append(video)
    project.artifacts.append(
        studio.repo.artifact(project, output / "qa.json", "longform_review_qa")
    )
    from autonomous_studio.domain.motion import MotionPlan
    from autonomous_studio.rendering.longform_motion import render_motion_thumbnail

    for artifact in project.current_artifacts("longform_motion_plan"):
        plan = MotionPlan.model_validate_json(
            studio.repo.verify(project, artifact).read_text(encoding="utf-8")
        )
        if plan.chapter_id != project.spec.chapters[0].id:
            continue
        candidate = output / "thumbnail.png"
        if render_motion_thumbnail(studio.repo, project, plan, candidate):
            root = studio.repo.project_dir(project.id) / (
                f"thumbnail-v{project.version:03d}-{digest(candidate)[:8]}.png"
            )
            if not root.exists():
                shutil.copyfile(candidate, root)
            project.artifacts.append(
                studio.repo.artifact(
                    project, root, "longform_thumbnail", provider="local",
                    model="opening-promise-v1", width=1280, height=720,
                )
            )
        break
    if movie.stat().st_size > 49_000_000:
        root = studio.repo.project_dir(project.id) / (
            f"review-video-v{project.version:03d}-{digest(movie)[:8]}.mp4"
        )
        temporary = root.with_suffix(".tmp")
        shutil.copyfile(movie, temporary)
        temporary.replace(root)
        exported = studio.repo.artifact(
            project,
            root,
            "longform_review_root",
            provider="local",
            model="lf-v0.5-auto-motion-review",
            duration=info["duration"],
            width=info["width"],
            height=info["height"],
        )
        project.artifacts.append(exported)
    studio.repo.save(
        project,
        "longform_review_validated",
        {
            "version": project.version,
            "duration": info["duration"],
            "bytes": movie.stat().st_size,
            "chapters": len(project.spec.chapters),
            "delivery": "project_root" if movie.stat().st_size > 49_000_000 else "telegram",
        },
    )
