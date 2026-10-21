import asyncio
import json
from pathlib import Path

import pytest

from autonomous_studio.audio.timing import NarrationTimingError, measured_plan, voice_signature
from autonomous_studio.domain.models import PreparedVoice, SoundCue, State, VoiceAsset, VoiceSegment
from autonomous_studio.providers.mock import MockAudio, MockDirector
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief


def test_voice_signature_ignores_text_models_but_detects_tts_changes(tmp_path):
    studio = Studio(tmp_path)
    segment = VoiceSegment(shot_id="shot_01", spoken_text="An unchanged recording.", estimated_duration=1)
    current = studio.create_topic("A test subject", duration=55,
                                  creation_settings={"tts_provider": "openai"})
    signature = voice_signature(current, segment)
    current.live_models["director_model"] = "another-writing-model"
    current.live_models["research_model"] = "another-research-model"
    assert voice_signature(current, segment) == signature
    current.live_models["tts_model"] = "another-tts-model"
    assert voice_signature(current, segment) != signature

    legacy = studio.create(load_brief(Path("examples/earthquake-full.yaml")), pipeline="live")
    output = studio.repo.attempt_dir(legacy, "voice-signature-fixture")
    path = output / "voice.wav"
    path.write_bytes(b"unchanged audio")
    artifact = studio.repo.artifact(legacy, path, "voice", provider="fixture", model="fixture")
    old_signature = voice_signature(legacy, segment)
    legacy.prepared_voices.append(
        PreparedVoice(
            shot_id=segment.shot_id,
            segment_index=0,
            signature=old_signature,
            asset=VoiceAsset(
                artifact=artifact,
                start=0,
                duration=1,
                spoken_text=segment.spoken_text,
                characters=len(segment.spoken_text),
                voice_id="fixture",
            ),
        )
    )
    studio.repo.save(legacy, "voice_signature_fixture", {})
    adopted = studio.adopt_shorts_stage_models(legacy.id)
    assert adopted.creation_settings["voice_signature_legacy_models"] == legacy.live_models
    assert voice_signature(adopted, segment) == old_signature
    adopted.live_models["tts_model"] = "another-tts-model"
    assert voice_signature(adopted, segment) != old_signature


class OfflineStudio(Studio):
    def configure_live(self, project):
        pass


class MeasuredVoice(MockAudio):
    def __init__(self, repo, durations=None, fail=None):
        super().__init__(repo)
        self.durations = durations or {"hook": 3.7, "subject": 1.0}
        self.fail, self.calls = fail, []

    async def synthesize(self, project, segment, spec, start, output):
        self.calls.append(segment.shot_id)
        if segment.shot_id == self.fail:
            raise RuntimeError("temporary speech failure")
        segment = segment.model_copy(
            update={"estimated_duration": self.durations.get(segment.shot_id, 1.0)}
        )
        return await super().synthesize(project, segment, spec, start, output)


def approved(studio, brief):
    project = studio.create(brief, pipeline="live")
    asyncio.run(studio.run(project.id))
    studio.decide(project.id, "story", "approve", actor="test-harness")
    asyncio.run(studio.run(project.id))
    studio.decide(project.id, "hero", "approve", actor="test-harness")
    return studio.repo.load(project.id)


def test_measured_retime_is_new_review_without_research_or_script_changes(tmp_path, brief):
    studio = OfflineStudio(tmp_path)
    voice = studio.voice = MeasuredVoice(studio.repo)
    old = approved(studio, brief)
    old_checksums = {a.id: a.sha256 for a in old.artifacts}
    result = asyncio.run(studio.run(old.id))
    assert result.state == State.STORY_APPROVAL
    assert result.version == 2
    assert result.timing_repairs == 1
    assert result.pending().version == 2
    assert result.spec.brief.research == old.spec.brief.research
    assert [(v.spoken_text) for s in result.spec.shots for v in s.voice_segments] == [
        v.spoken_text for s in old.spec.shots for v in s.voice_segments
    ]
    assert result.spec.brief.duration <= 15
    assert result.spec.shots[0].duration >= 3.7 + 0.15 + 0.1
    assert result.spec.shots[-1].duration >= old.spec.shots[-1].duration
    assert voice.calls == ["hook", "subject"]
    assert len(result.prepared_voices) == 2
    assert not result.spec.audio_assets
    for artifact in result.artifacts:
        studio.repo.verify(result, artifact)
        if artifact.id in old_checksums:
            assert artifact.sha256 == old_checksums[artifact.id]
    assert not measured_plan(result)[2]
    with pytest.raises(ValueError, match="stale"):
        studio.decide(result.id, "story", "approve", approval_id=old.approvals[0].id)
    restarted = OfflineStudio(tmp_path)
    assert asyncio.run(restarted.run(result.id)).version == 2


def test_late_decorative_sfx_is_moved_instead_of_forcing_narration_rewrite(tmp_path):
    full = load_brief(Path("examples/earthquake-full.yaml"))
    studio = OfflineStudio(tmp_path)
    project = studio.create(full, pipeline="live", review_policy="preview")
    project.spec = asyncio.run(MockDirector().develop(full, []))
    output = studio.repo.attempt_dir(project, "timing-fixture")
    for shot in project.spec.shots:
        if not shot.voice_segments:
            continue
        shot.sound_cues = [
            SoundCue(shot_id=shot.id, offset=max(0, shot.duration - 0.35), duration=0.35)
        ]
        for index, segment in enumerate(shot.voice_segments):
            duration = 17.0 if shot.id == "rank-5" else 1.0
            path = output / f"{shot.id}-{index}.wav"
            path.write_bytes(b"timing fixture")
            artifact = studio.repo.artifact(
                project, path, "voice", provider="test", model="fixture", duration=duration
            )
            project.prepared_voices.append(
                PreparedVoice(
                    shot_id=shot.id,
                    segment_index=index,
                    signature=voice_signature(project, segment),
                    asset=VoiceAsset(
                        artifact=artifact,
                        start=shot.start + segment.offset,
                        duration=duration,
                        spoken_text=segment.spoken_text,
                        characters=len(segment.spoken_text),
                        voice_id="fixture",
                    ),
                )
            )

    proposed, _, mismatch = measured_plan(project)

    assert mismatch
    assert proposed is not None and proposed.brief.duration <= 90
    assert all(
        cue.offset + cue.duration <= shot.duration + 1e-6
        for shot in proposed.shots
        for cue in shot.sound_cues
    )


@pytest.mark.integration
def test_repaired_audio_reused_after_approval_and_real_render(tmp_path, brief):
    studio = OfflineStudio(tmp_path)
    voice = studio.voice = MeasuredVoice(studio.repo)
    result = asyncio.run(studio.run(approved(studio, brief).id))
    studio.decide(result.id, "story", "approve", actor="test-harness")
    asyncio.run(studio.run(result.id))
    studio.decide(result.id, "hero", "approve", actor="test-harness")
    restarted = OfflineStudio(tmp_path)

    class NoSynthesis:
        async def synthesize(self, *args):
            raise AssertionError("Measured recordings must be reused after restart")

    restarted.voice = NoSynthesis()
    result = asyncio.run(restarted.run(result.id))
    assert result.state == State.PROTOTYPE_APPROVAL
    assert voice.calls == ["hook", "subject"]
    ids = [a.id for a in result.artifacts]
    assert len(ids) == len(set(ids))
    qa = json.loads(restarted.repo.verify(result, result.current_artifacts("qa")[0]).read_text())
    assert qa["video"]["duration"] == pytest.approx(result.spec.brief.duration, abs=0.1)
    assert qa["video"]["has_audio"]


def test_partial_synthesis_is_durable_on_failure(tmp_path, brief):
    studio = OfflineStudio(tmp_path)
    studio.voice = MeasuredVoice(studio.repo, fail="subject")
    project = approved(studio, brief)
    with pytest.raises(RuntimeError, match="temporary speech"):
        asyncio.run(studio.run(project.id))
    stored = studio.repo.load(project.id)
    assert len(stored.prepared_voices) == 1
    restarted = OfflineStudio(tmp_path)
    voice = restarted.voice = MeasuredVoice(restarted.repo)
    result = asyncio.run(restarted.run(project.id))
    assert voice.calls == ["subject"]
    assert result.state == State.STORY_APPROVAL


def test_timing_retry_blocked_and_repair_is_version_bound(tmp_path, brief):
    studio = OfflineStudio(tmp_path)
    project = approved(studio, brief)
    project.last_error = "synthesized narration exceeds its shot; rewrite it"
    studio.repo.save(project, "test_failure", {})
    with pytest.raises(ValueError, match="same overlong audio"):
        studio.request_retry(project.id)
    with pytest.raises(ValueError, match="stale"):
        studio.request_retry(project.id, repair=True, version=9)
    repaired = studio.request_retry(project.id, repair=True, version=1)
    assert repaired.last_error is None
    assert repaired.version == 1  # Queued, not silently approved or edited.


def test_over_budget_rewrites_story_but_does_not_rerun_research(tmp_path, brief):
    studio = OfflineStudio(tmp_path)
    studio.voice = MeasuredVoice(studio.repo, durations={"hook": 9, "subject": 9})
    project = approved(studio, brief)

    class RepairDirector:
        async def develop(self, supplied, feedback):
            assert supplied.research == project.spec.brief.research
            assert "actual speech" in feedback[0]
            supplied.hook_narration = "Human impact."
            for subject in supplied.research.subjects:
                subject.narration = "Consider the evidence."
            # Avoid changing research in the returned spec: narration is creative data, but
            # preserving all evidence exactly is a stronger contract for this test.
            spec = await MockDirector().develop(supplied, [])
            spec.brief.research = project.spec.brief.research.model_copy(deep=True)
            return spec

    studio.director = RepairDirector()
    result = asyncio.run(studio.run(project.id))
    assert result.version == 2
    assert result.pending().gate == "story"
    assert "shorter narration" in result.timing_repair_note
    assert not result.prepared_voices
    assert result.spec.brief.research == project.spec.brief.research


def test_bounded_repairs_require_explicit_revision(tmp_path, brief):
    studio = OfflineStudio(tmp_path)
    studio.voice = MeasuredVoice(studio.repo)
    project = approved(studio, brief)
    project.timing_repairs = 2
    studio.repo.save(project, "test_repair_limit", {})
    with pytest.raises(NarrationTimingError, match="two repair"):
        asyncio.run(studio.run(project.id))
    result = studio.repo.load(project.id)
    assert result.version == 1
    assert result.last_error_code == "narration_timing"
    with pytest.raises(ValueError, match="limit reached"):
        studio.request_retry(result.id, repair=True, version=1)
    revised = studio.revise(result.id, "Shorter spoken narration")
    assert revised.timing_repairs == 0
    assert not revised.prepared_voices


def test_failed_repair_delivery_leaves_original_version_resumable(tmp_path, brief):
    studio = OfflineStudio(tmp_path)
    studio.voice = MeasuredVoice(studio.repo)
    project = approved(studio, brief)

    class FailingHITL:
        async def send(self, *args):
            raise RuntimeError("local review write failed")

    studio.hitl = FailingHITL()
    with pytest.raises(RuntimeError, match="review write"):
        asyncio.run(studio.run(project.id))
    stored = studio.repo.load(project.id)
    assert stored.version == 1
    assert len(stored.prepared_voices) == 2
    assert not stored.pending()
    assert stored.timing_repairs == 0
    assert asyncio.run(OfflineStudio(tmp_path).run(project.id)).pending().version == 2
