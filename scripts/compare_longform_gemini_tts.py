"""Compare approved Chapter 1 words in the original voice and Gemini Aoede.

This is deliberately read-only with respect to the project's persisted workflow.
Only files under comparisons/gemini-aoede-ch1 are written.
"""

import argparse
import asyncio
import hashlib
import json
import wave
from pathlib import Path

from autonomous_studio.config import Settings
from autonomous_studio.domain.models import VoiceSegment
from autonomous_studio.providers.gemini_tts import GeminiVoice
from autonomous_studio.providers.http import HTTP
from autonomous_studio.storage import Repository
from autonomous_studio.workflow import Studio  # Import engine before longform rendering.


def assemble(path: Path, chapter, voices, paths):
    _ = Studio  # Keep the import order explicit; longform rendering imports workflow modules.
    from autonomous_studio.rendering.longform import narration_windows

    windows = narration_windows(chapter, voices, strict=False)
    rate = 48000
    end = max((stop for _, _, _, stop in windows), default=0)
    samples = bytearray(round((end + 0.25) * rate) * 2)
    for block, _, start, _ in windows:
        with wave.open(str(paths[block.id]), "rb") as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (
                1,
                2,
                rate,
            ):
                raise ValueError(f"Unexpected PCM format: {block.id}")
            data = source.readframes(source.getnframes())
        offset = round(start * rate) * 2
        samples[offset : offset + len(data)] = data
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(samples)
    return len(samples) / 2 / rate


async def main(project_id, root):
    repo = Repository(root)
    project = repo.load(project_id)
    chapter = project.spec.chapters[0]
    output = repo.project_dir(project_id) / "comparisons" / "gemini-aoede-ch1"
    output.mkdir(parents=True, exist_ok=True)
    settings = Settings.environment()
    settings.require_gemini_key()
    provider = GeminiVoice(repo, settings, output / "cache", HTTP())
    spec = project.spec.brief.voice.model_copy(update={"provider": "gemini", "voice_id": "Aoede"})
    comparison_project = project.model_copy(deep=True)
    comparison_project.live_models["longform_tts_model"] = "gemini-3.8-flash-tts"
    original = {}
    generated = {}
    original_paths = {}
    generated_paths = {}
    for block in chapter.narration_blocks:
        prior = project.longform_voices[block.id]
        original[block.id] = prior
        original_paths[block.id] = repo.verify(project, prior.artifact)
        segment = VoiceSegment(
            shot_id=block.id,
            spoken_text=block.text,
            estimated_duration=block.estimated_duration,
            delivery_direction=block.delivery_direction,
        )
        path = output / f"aoede-{block.id}.wav"
        voice = await provider.synthesize(comparison_project, segment, spec, 0, path)
        generated[block.id] = voice
        generated_paths[block.id] = path
        print(f"{block.id}: {prior.duration:.2f}s original, {voice.duration:.2f}s Aoede")
    before = assemble(output / "original-chapter-1.wav", chapter, original, original_paths)
    after = assemble(output / "aoede-chapter-1.wav", chapter, generated, generated_paths)
    manifest = {
        "project_id": project.id,
        "project_version": project.version,
        "chapter_id": chapter.id,
        "approved_words_sha256": hashlib.sha256(
            "\n".join(block.text for block in chapter.narration_blocks).encode()
        ).hexdigest(),
        "original_seconds": before,
        "aoede_seconds": after,
        "provider": "gemini",
        "model": "gemini-3.8-flash-tts",
        "voice": "Aoede",
        "note": "Audio-only comparison. No video, approval, or saved project state was changed.",
    }
    (output / "comparison.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("--root", type=Path, default=Path("projects/live"))
    args = parser.parse_args()
    asyncio.run(main(args.project_id, args.root))
