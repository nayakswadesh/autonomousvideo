import wave

import pytest

from autonomous_studio.audio.mixer import align_pcm_duration, pcm_metrics


def _wav(path, frames):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(b"\x01\x00" * frames)


def test_align_small_short_mix_without_changing_existing_samples(tmp_path):
    path = tmp_path / "mix.wav"
    _wav(path, 48000 - 3200)
    assert align_pcm_duration(path, 1.0) == pytest.approx(3200 / 48000)
    with wave.open(str(path), "rb") as aligned:
        assert aligned.getnframes() == 48000
        samples = aligned.readframes(48000)
        assert samples[:4] == b"\x01\x00\x01\x00"
        assert samples[-4:] == b"\x00\x00\x00\x00"
    assert pcm_metrics(path)["duration"] == 1.0


def test_rejects_large_audio_gap(tmp_path):
    path = tmp_path / "mix.wav"
    _wav(path, 24000)
    with pytest.raises(ValueError, match="too much"):
        align_pcm_duration(path, 1.0)
