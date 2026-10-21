import asyncio

import pytest

from autonomous_studio.audio.mixer import Mixer, pcm_metrics
from autonomous_studio.domain.models import ProviderFailure, State, TitleShot
from autonomous_studio.providers.mock import MockAudio
from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.research.quality import audio_warnings
from autonomous_studio.workflow import Studio


class StoppedRenderer:
    async def render(self, project, output):
        return ProviderFailure(provider="test", message="stop before render", retryable=True)


def prepared(tmp_path, brief):
    studio = Studio(tmp_path, renderer=StoppedRenderer())
    project = studio.create(brief)
    for gate in ("story", "hero"):
        asyncio.run(studio.run(project.id))
        studio.decide(project.id, gate, "approve", actor="test-harness")
    with pytest.raises(RuntimeError, match="stop before render"):
        asyncio.run(studio.run(project.id))
    return studio, studio.repo.load(project.id)


def test_missing_audio_and_failed_render_checkpoint(tmp_path, brief):
    studio, project = prepared(tmp_path, brief)
    assert project.state == State.AUDIO_PREPARED
    assert project.spec.audio_assets
    path = studio.repo.resolve(project.id, project.spec.audio_assets[0].artifact.path)
    path.unlink()
    with pytest.raises(ValueError, match="missing or modified"):
        asyncio.run(Studio(tmp_path).run(project.id))
    assert studio.repo.load(project.id).state == State.AUDIO_PREPARED


def test_unknown_music_license_is_explicit(tmp_path, brief):
    studio = Studio(tmp_path)
    project = studio.create(brief)
    output = studio.repo.attempt_dir(project, "test-audio")
    music = asyncio.run(
        MockAudio(studio.repo).create_or_select(project, brief.music, output / "music.wav")
    )
    provenance = music.artifact.provenance.model_dump()
    provenance.update(license_category="unknown", usage_restrictions="unreliable input")
    music.artifact.provenance = type(music.artifact.provenance).model_validate(provenance)
    assert music.artifact.provenance.usage_restrictions == "UNKNOWN — DO NOT PUBLISH"
    assert "UNKNOWN" in audio_warnings([music])[0]


@pytest.mark.integration
def test_deliberate_silence_and_ducking_disabled(tmp_path, brief):
    brief.shots = [
        TitleShot(
            id="silence",
            start=0,
            duration=brief.duration,
            title="Intentional silence",
            intentional_silence=True,
        )
    ]
    brief.mix.music_ducking = False
    studio, project = prepared(tmp_path, brief)
    output = studio.repo.attempt_dir(project, "silence-mix")
    _, qa = Mixer(studio.repo, FFmpeg()).mix(project, output)
    assert qa["duration"] == brief.duration
    assert pcm_metrics(output / "mix.wav")["rms"] == 0
