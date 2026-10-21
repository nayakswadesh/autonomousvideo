"""Replay selected long-form audio provider calls without replacing approved artifacts.

The fresh provider cache and outputs live in a versioned attempt directory. Only
blocks whose new word timings align with approved narration are promoted.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from autonomous_studio.domain.models import State, VoiceSegment
from autonomous_studio.providers.openai import OpenAIClient, OpenAIVoice
from autonomous_studio.storage.repository import write_json
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.longform_production import _voice_signature
from autonomous_studio.workflow.spoken_units import _aligned_word_slice, chapter_units


def alignment_errors(chapter, block_id: str, observed: list[dict]) -> list[str]:
    block = next(item for item in chapter.narration_blocks if item.id == block_id)
    errors = []
    for unit in chapter_units(chapter):
        if unit["block_id"] != block_id:
            continue
        try:
            aligned = _aligned_word_slice(
                block.text, observed, unit["start_word"], unit["end_word"]
            )
            if any(word.get("estimated") for word in aligned):
                errors.append(f"{unit['id']}: only an estimated midpoint is available")
        except ValueError as exc:
            errors.append(f"{unit['id']}: {exc}")
    return errors


async def replay(
    root: Path,
    project_id: str,
    version: int,
    asr_block: str | None = None,
    tts_block: str | None = None,
):
    studio = Studio(root)
    repo = studio.repo
    with repo.lock(project_id):
        project = repo.load(project_id)
        if project.version != version or project.state != State.LONGFORM_AUDIO_PREPARING:
            raise ValueError("Project is not at the requested long-form audio checkpoint")
        if project.pending() or not project.last_error:
            raise ValueError("Expected a failed audio stage with no pending approval")
        blocks = {
            block.id: (chapter, block)
            for chapter in project.spec.chapters
            for block in chapter.narration_blocks
        }
        requested = [block_id for block_id in (asr_block, tts_block) if block_id]
        if (
            not requested
            or len(set(requested)) != len(requested)
            or any(block_id not in blocks for block_id in requested)
        ):
            raise ValueError("Specify one or two distinct, existing narration block IDs")
        original_voices = {block_id: project.longform_voices[block_id] for block_id in requested}
        audio_paths = {
            block_id: repo.verify(project, voice.artifact)
            for block_id, voice in original_voices.items()
        }
        starting_revision = project.revision
        expected_hashes = {
            block_id: voice.artifact.sha256 for block_id, voice in original_voices.items()
        }
        attempt = repo.attempt_dir(project, "longform-audio-replay")
        write_json(
            attempt / "request.json",
            {
                "project_id": project_id,
                "version": version,
                "revision": starting_revision,
                "retranscribe": asr_block,
                "resynthesize": tts_block,
                "original_voice_sha256": expected_hashes,
            },
        )

    studio.configure_live(project)
    client = OpenAIClient(
        studio.live_client.settings,
        attempt / "provider-cache",
        studio.live_client.http,
    )
    results = {}
    print(f"Replay attempt: {attempt}", flush=True)

    if asr_block:
        try:
            print(f"Retranscribing existing WAV: {asr_block}", flush=True)
            words = await client.transcribe_words(audio_paths[asr_block])
            errors = alignment_errors(blocks[asr_block][0], asr_block, words)
            alignment_path = attempt / f"alignment-{asr_block}.json"
            write_json(
                alignment_path,
                {"block_id": asr_block, "provider": "openai-whisper-1", "words": words},
            )
            results[asr_block] = {
                "words": words,
                "errors": errors,
                "alignment_path": alignment_path,
            }
            print(f"{asr_block}: {'aligned' if not errors else '; '.join(errors)}", flush=True)
        except Exception as exc:
            results[asr_block] = {"errors": [f"{type(exc).__name__}: {exc}"]}
            print(f"{asr_block}: provider failure: {type(exc).__name__}: {exc}", flush=True)

    if tts_block:
        try:
            print(f"Synthesizing new WAV: {tts_block}", flush=True)
            chapter, block = blocks[tts_block]
            segment = VoiceSegment(
                shot_id=block.id,
                spoken_text=block.text,
                estimated_duration=block.estimated_duration,
                delivery_direction=block.delivery_direction,
            )
            write_json(
                attempt / f"{tts_block}.request.json", {"block_id": block.id, "status": "reserved"}
            )
            voice = await OpenAIVoice(client, repo).synthesize(
                project, segment, project.spec.brief.voice, 0, attempt / f"{tts_block}.wav"
            )
            voice.artifact.input_reference = _voice_signature(project, block)
            words = await client.transcribe_words(repo.verify(project, voice.artifact))
            errors = alignment_errors(chapter, tts_block, words)
            alignment_path = attempt / f"alignment-{tts_block}.json"
            write_json(
                alignment_path,
                {"block_id": tts_block, "provider": "openai-whisper-1", "words": words},
            )
            results[tts_block] = {
                "voice": voice,
                "words": words,
                "errors": errors,
                "alignment_path": alignment_path,
            }
            print(f"{tts_block}: {'aligned' if not errors else '; '.join(errors)}", flush=True)
        except Exception as exc:
            results[tts_block] = {"errors": [f"{type(exc).__name__}: {exc}"]}
            print(f"{tts_block}: provider failure: {type(exc).__name__}: {exc}", flush=True)

    passed = [block_id for block_id, result in results.items() if not result["errors"]]
    write_json(
        attempt / "result.json",
        {
            "project_id": project_id,
            "version": version,
            "passed": passed,
            "errors": {block_id: result["errors"] for block_id, result in results.items()},
        },
    )
    if passed:
        with repo.lock(project_id):
            current = repo.load(project_id)
            if current.revision != starting_revision or current.version != version:
                raise ValueError(
                    "Project changed during provider replay; staged files were not promoted"
                )
            for block_id in passed:
                if current.longform_voices[block_id].artifact.sha256 != expected_hashes[block_id]:
                    raise ValueError(
                        "Original voice changed during replay; staged files were not promoted"
                    )
            for block_id in passed:
                result = results[block_id]
                if "voice" in result:
                    current.longform_voices[block_id] = result["voice"]
                    current.artifacts.append(result["voice"].artifact)
                current.longform_word_timings[block_id] = result["words"]
                current.artifacts.append(
                    repo.artifact(
                        current,
                        result["alignment_path"],
                        "longform_word_alignment",
                        provider="openai-whisper-1",
                    )
                )
            repo.save(
                current,
                "longform_audio_targeted_replay",
                {
                    "version": version,
                    "blocks": passed,
                    "attempt": attempt.relative_to(repo.project_dir(project_id)).as_posix(),
                },
            )
    print(f"Promoted aligned blocks: {', '.join(passed) if passed else 'none'}", flush=True)
    print("Project remains paused; no review gate was approved.", flush=True)
    return len(passed) == len(requested)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id")
    parser.add_argument("--root", type=Path, default=Path("projects/live"))
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--retranscribe", help="Block with correct WAV but bad transcription")
    parser.add_argument("--resynthesize", help="Block needing a new TTS take")
    args = parser.parse_args()
    complete = asyncio.run(
        replay(args.root, args.project_id, args.version, args.retranscribe, args.resynthesize)
    )
    raise SystemExit(0 if complete else 2)


if __name__ == "__main__":
    main()
