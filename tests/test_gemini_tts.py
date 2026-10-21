import asyncio
import base64
import json
import wave

import pytest
from test_longform_live import FakeTelegram, OfflineStudio, command

from autonomous_studio.config import Settings
from autonomous_studio.domain.models import VoiceSegment, VoiceSpec
from autonomous_studio.hitl.worker import TelegramWorker
from autonomous_studio.providers.gemini_tts import (
    ENDPOINT,
    GeminiVoice,
    extract_wav,
    gemini_speech_payload,
)
from autonomous_studio.providers.mock import tone
from autonomous_studio.workflow import Studio


def test_gemini_payload_keeps_approved_words_verbatim():
    segment = VoiceSegment(
        shot_id="ch1-block1",
        spoken_text="What did Curie find?",
        estimated_duration=2.0,
        delivery_direction="Pause before the question.",
    )
    payload = gemini_speech_payload(
        "gemini-3.8-flash-tts",
        segment,
        VoiceSpec(provider="gemini", voice_id="Aoede"),
    )
    assert payload["input"][0]["content"][0]["text"] == segment.spoken_text
    assert payload["generation_config"]["speech_config"] == [{"voice": "Aoede"}]
    assert "Pause before" in payload["input"][0]["content"][0]["annotations"][0]["style"]
    assert payload["response_format"] == {"type": "audio"}


def test_extract_wav_requires_model_audio():
    wav = b"RIFF" + b"0" * 20
    response = {
        "steps": [
            {
                "type": "model_output",
                "content": [{"type": "audio", "data": base64.b64encode(wav).decode()}],
            }
        ]
    }
    assert extract_wav(json.dumps(response).encode()) == wav
    with pytest.raises(ValueError, match="no valid audio"):
        extract_wav(b'{"steps": []}')


@pytest.mark.parametrize("format", ["longform", "shorts"])
def test_gemini_voice_cache_and_project_provider_isolation(tmp_path, format):
    studio = Studio(tmp_path, settings=Settings(api_key="test", gemini_api_key="test-gemini"))
    create = studio.create_longform_topic if format == "longform" else studio.create_topic
    project = create("The Sun")
    assert project.spec.brief.voice.provider == "gemini"
    assert project.spec.brief.voice.voice_id == "Aoede"
    sample = tmp_path / "sample.wav"
    tone(sample, 0.4, "voice")

    class FakeHTTP:
        def __init__(self):
            self.calls = []

        async def post(self, url, body, headers, **kwargs):
            self.calls.append((url, json.loads(body), headers))
            return json.dumps(
                {
                    "steps": [
                        {
                            "type": "model_output",
                            "content": [
                                {
                                    "type": "audio",
                                    "data": base64.b64encode(sample.read_bytes()).decode(),
                                }
                            ],
                        }
                    ]
                }
            ).encode()

    http = FakeHTTP()
    provider = GeminiVoice(studio.repo, studio.settings, tmp_path / "cache", http)
    segment = VoiceSegment(
        shot_id="one", spoken_text="The Sun began in a cloud.", estimated_duration=2
    )
    out = studio.repo.project_dir(project.id) / "one.wav"
    first = asyncio.run(provider.synthesize(project, segment, project.spec.brief.voice, 0, out))
    second = asyncio.run(provider.synthesize(project, segment, project.spec.brief.voice, 0, out))
    assert len(http.calls) == 1
    assert http.calls[0][0] == ENDPOINT
    assert first.artifact.provider == "gemini"
    assert second.duration > 0
    with wave.open(str(out), "rb") as source:
        assert source.getframerate() == 48000
    old = studio.create_longform_topic(
        "A second story", creation_settings={"longform_tts_provider": "openai"}
    )
    assert old.spec.brief.voice.provider == "openai"
    assert old.spec.brief.voice.voice_id == "coral"


def test_live_longform_voice_routing_preserves_older_openai_projects(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test", gemini_api_key="gemini-test"))
    gemini = studio.create_longform_topic("A new documentary")
    studio.configure_live(gemini)
    assert isinstance(studio.voice, GeminiVoice)
    older = studio.create_longform_topic(
        "An older documentary", creation_settings={"longform_tts_provider": "openai"}
    )
    studio.configure_live(older)
    from autonomous_studio.providers.openai import OpenAIVoice

    assert isinstance(studio.voice, OpenAIVoice)


def test_short_creation_defaults_to_gemini_and_preserves_saved_openai(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test", gemini_api_key="gemini-test"))
    short = studio.create_topic("A short science story", duration=55, production_mode="full")
    assert short.spec.brief.voice.provider == "gemini"
    assert short.spec.brief.voice.voice_id == "Aoede"
    studio.configure_live(short)
    assert isinstance(studio.voice, GeminiVoice)
    older = studio.create_topic("An older voice", creation_settings={"tts_provider": "openai"})
    studio.settings.tts_provider = "gemini"
    studio.configure_live(studio.repo.load(older.id))
    from autonomous_studio.providers.openai import OpenAIVoice

    assert isinstance(studio.voice, OpenAIVoice)
    assert older.spec.brief.voice.voice_id == "coral"


def test_telegram_longform_tts_preference_persists_for_future_projects(tmp_path):
    studio = OfflineStudio(tmp_path, settings=Settings(api_key="test"))
    adapter = FakeTelegram(studio.repo)
    worker = TelegramWorker(studio, adapter, lambda: studio)
    asyncio.run(worker.process(command(1, "/ttsprovider openai")))
    asyncio.run(worker.process(command(2, "/longform OpenAI-voiced story")))
    first = studio.repo.all_projects()[0]
    assert first.spec.brief.voice.provider == "openai"
    restarted = TelegramWorker(studio, adapter, lambda: studio)
    asyncio.run(restarted.process(command(3, "/ttsprovider gemini")))
    asyncio.run(restarted.process(command(4, "/longform Gemini-voiced story")))
    second = studio.repo.all_projects()[1]
    assert second.spec.brief.voice.provider == "gemini"
    assert second.spec.brief.voice.voice_id == "Aoede"
    assert studio.repo.load(first.id).spec.brief.voice.provider == "openai"


def test_shared_provider_controls_both_formats_and_reset_clears_legacy_preference(tmp_path):
    studio = OfflineStudio(tmp_path, settings=Settings(api_key="test"))
    adapter = FakeTelegram(studio.repo)
    worker = TelegramWorker(studio, adapter, lambda: studio)
    studio.repo.set_runtime(worker.longform_tts_key, "openai")
    assert worker.creation_settings()["tts_provider"] == "openai"
    for n, provider in enumerate(("gemini", "openai"), start=1):
        asyncio.run(worker.process(command(n, f"/ttsprovider {provider}")))
        # Simulate worker restart before project creation.
        worker = TelegramWorker(studio, adapter, lambda: studio)
        prefs = worker.creation_settings()
        short = studio.create_topic("A short story", creation_settings=prefs)
        long = studio.create_longform_topic("A long story", creation_settings=prefs)
        assert short.spec.brief.voice.provider == long.spec.brief.voice.provider == provider
    asyncio.run(worker.process(command(3, "/ttsprovider reset")))
    assert worker.creation_settings()["tts_provider"] == "gemini"
    assert studio.repo.get_runtime(worker.longform_tts_key) is None
    assert "Shorts and long-form" in worker.settings_text()


def test_short_voice_signature_tracks_only_selected_speech_model(tmp_path):
    from autonomous_studio.audio.timing import voice_signature

    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_topic("A story")
    segment = VoiceSegment(shot_id="one", spoken_text="The Sun shines.", estimated_duration=1)
    before = voice_signature(project, segment)
    project.live_models["tts_model"] = "unused-openai-model"
    project.live_models["director_model"] = "unused-text-model"
    assert voice_signature(project, segment) == before
    project.live_models["longform_tts_model"] = "another-gemini-model"
    assert voice_signature(project, segment) != before


def test_existing_schema_two_recording_hash_is_unchanged(tmp_path):
    import hashlib

    from autonomous_studio.audio.timing import voice_signature

    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_topic("Old saved short", creation_settings={"tts_provider": "openai"})
    project.creation_settings["voice_signature_schema"] = 2
    segment = VoiceSegment(shot_id="one", spoken_text="An approved line.", estimated_duration=1)
    old_data = {
        "voice": project.spec.brief.voice.model_dump(mode="json"),
        "models": {"tts_model": project.live_models["tts_model"],
                   "voice": project.live_models["voice"]},
        "text": segment.spoken_text, "delivery": segment.delivery_direction,
        "pronunciation": segment.important_pronunciations,
    }
    expected = hashlib.sha256(json.dumps(old_data, sort_keys=True).encode()).hexdigest()
    studio.settings.tts_provider = "gemini"
    assert voice_signature(project, segment) == expected

