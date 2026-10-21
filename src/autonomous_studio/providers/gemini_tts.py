"""Gemini 3.8 speech adapter for Shorts and long-form narration.

The request carries the approved words verbatim. Direction is metadata, never
an extra spoken preamble. The audio receipt is cached before normalization.
"""

import base64
import hashlib
import json
from pathlib import Path
from time import perf_counter

from autonomous_studio.audio.mixer import pcm_metrics
from autonomous_studio.domain.models import VoiceAsset
from autonomous_studio.rendering.ffmpeg import FFmpeg

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"


def gemini_speech_payload(model, segment, spec):
    if not segment.spoken_text.strip():
        raise ValueError("Gemini TTS requires nonempty approved narration")
    style = (
        f"{spec.style}. {segment.delivery_direction}. "
        "One consistent documentary narrator across adjoining clips. "
        "Speak naturally with meaningful emphasis and room to absorb important ideas; "
        "do not rush or add, omit, or paraphrase words. "
        f"Language: {spec.language}."
    )
    if spec.pronunciation_notes or segment.important_pronunciations:
        style += " Pronunciation: " + "; ".join(
            spec.pronunciation_notes + segment.important_pronunciations
        )
    return {
        "model": model,
        "input": [
            {
                "type": "user_input",
                "content": [
                    {
                        "type": "text",
                        "text": segment.spoken_text,
                        "annotations": [{"type": "speech_metadata", "style": style}],
                    }
                ],
            }
        ],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": spec.voice_id}]},
    }


def gemini_cache_path(cache_dir: Path, payload: dict, epoch=None) -> Path:
    key = hashlib.sha256(
        json.dumps({"payload": payload, "fresh_media_epoch": epoch}, sort_keys=True).encode()
    ).hexdigest()
    return cache_dir / f"gemini-tts-{key}.wav"


def extract_wav(response: bytes) -> bytes:
    try:
        document = json.loads(response)
        audio = [
            part["data"]
            for step in document["steps"]
            if step.get("type") == "model_output"
            for part in step.get("content", [])
            if part.get("type") == "audio" and isinstance(part.get("data"), str)
        ]
        wav = base64.b64decode(audio[-1], validate=True)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ValueError("Gemini TTS response contains no valid audio") from exc
    if not wav.startswith(b"RIFF"):
        raise ValueError("Gemini TTS did not return WAV audio")
    return wav


class GeminiVoice:
    def __init__(self, repo, settings, cache: Path, http):
        self.repo, self.settings, self.cache, self.http = repo, settings, cache, http

    async def synthesize(self, project, segment, spec, start, output):
        model = project.live_models.get("longform_tts_model", self.settings.longform_tts_model)
        payload = gemini_speech_payload(model, segment, spec)
        cache = gemini_cache_path(
            self.cache, payload, project.creation_settings.get("fresh_media_epoch")
        )
        started = perf_counter()
        if not cache.is_file():
            response = await self.http.post(
                ENDPOINT,
                json.dumps(payload).encode(),
                {
                    "Content-Type": "application/json",
                    "x-goog-api-key": self.settings.require_gemini_key(),
                },
                timeout=180,
                service="Gemini TTS",
            )
            wav = extract_wav(response)
            cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_suffix(".tmp")
            temporary.write_bytes(wav)
            temporary.replace(cache)
        FFmpeg().run(["-i", cache, "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", output])
        duration = pcm_metrics(output)["duration"]
        artifact = self.repo.artifact(
            project,
            output,
            "voice",
            provider="gemini",
            model=model,
            duration=duration,
            latency_seconds=perf_counter() - started,
        )
        artifact.provenance.origin = "generated"
        artifact.provenance.source = "Google Gemini speech API"
        artifact.provenance.license_category = "unknown"
        artifact.provenance.usage_restrictions = "UNKNOWN — DO NOT PUBLISH"
        return VoiceAsset(
            artifact=artifact,
            start=start,
            duration=duration,
            spoken_text=segment.spoken_text,
            characters=len(segment.spoken_text),
            voice_id=spec.voice_id,
        )
