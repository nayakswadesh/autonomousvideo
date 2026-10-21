import asyncio
import json
import wave
from array import array

import pytest

from autonomous_studio.audio.mixer import pcm_metrics
from autonomous_studio.domain.models import State
from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.storage.repository import digest
from autonomous_studio.workflow import Studio


def window_rms(path, start, duration):
    with wave.open(str(path), "rb") as source:
        rate = source.getframerate()
        source.setpos(round(start * rate))
        samples = array("h", source.readframes(round(duration * rate)))
    return (sum(x * x for x in samples) / len(samples)) ** 0.5


@pytest.mark.integration
def test_complete_render_determinism_and_audio_qa(tmp_path, brief):
    studio = Studio(tmp_path)
    project = studio.create(brief)
    for gate in ("story", "hero"):
        project = asyncio.run(Studio(tmp_path).run(project.id))
        studio.decide(project.id, gate, "approve", actor="test-harness")
    project = asyncio.run(Studio(tmp_path).render(project.id))
    assert project.state == State.PROTOTYPE_APPROVAL
    artifact = project.current_artifacts("prototype")[0]
    movie = studio.repo.verify(project, artifact)
    info = FFmpeg().inspect(movie)
    assert info["has_audio"] and info["decodable"]
    assert (info["width"], info["height"]) == (540, 960)
    assert abs(info["duration"] - 12) < 0.1
    qa = json.loads(studio.repo.verify(project, project.current_artifacts("qa")[0]).read_text())
    assert qa["clipped_samples"] == 0
    assert abs(float(qa["loudness"]["input_i"]) - brief.mix.target_loudness) < 1
    assert float(qa["loudness"]["input_tp"]) <= brief.mix.true_peak_db + 0.2
    assert not qa["publishable"]
    music = studio.repo.verify(project, project.current_artifacts("stem_music")[0])
    ducked = studio.repo.verify(project, project.current_artifacts("stem_music_ducked")[0])
    assert window_rms(ducked, 0.5, 0.15) < window_rms(music, 0.5, 0.15) * 0.8
    # Render the same approved spec to a different directory; compare bytes, not filenames.
    # Renderer artifacts must remain under the project directory.
    repeat = studio.repo.attempt_dir(project, "determinism")
    asyncio.run(studio.renderer.render(project, repeat))
    assert digest(movie) == digest(repeat / "prototype.mp4")
    completed = studio.decide(project.id, "prototype", "approve", actor="test-harness")
    assert completed.state == State.COMPLETED
    final_video = studio.repo.project_dir(project.id) / "final-video.mp4"
    assert final_video.is_file() and digest(final_video) == digest(movie)
    event = studio.repo.events(project.id)[-1]
    assert event["payload"]["final_video"] == "final-video.mp4"
    assert asyncio.run(studio.run(project.id)).revision == completed.revision
    assert all(a.version == 1 for a in completed.artifacts)
    assert all(studio.repo.verify(completed, a).is_file() for a in completed.artifacts)


def test_clipping_detection(tmp_path):
    path = tmp_path / "clipped.wav"
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        output.writeframes(array("h", [32767, -32768] * 480).tobytes())
    assert pcm_metrics(path)["clipped_samples"] == 960
