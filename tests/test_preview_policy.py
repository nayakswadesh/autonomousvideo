import asyncio
import json
from pathlib import Path

import pytest
from test_live import FakeTelegram, callback
from test_timing_repair import MeasuredVoice, OfflineStudio, approved

from autonomous_studio.domain.models import ProviderSuccess, State
from autonomous_studio.hitl.adapters import LocalHITL, review_message
from autonomous_studio.hitl.worker import TelegramWorker
from autonomous_studio.providers.mock import MockDirector
from autonomous_studio.storage.repository import write_json
from autonomous_studio.workflow.engine import load_brief


class RecordingHITL(LocalHITL):
    def __init__(self):
        self.gates = []

    async def send(self, message, output):
        self.gates.append(message.gate)
        return await super().send(message, output)


class FakeRenderer:
    """No-codec unit fixture; the integration test below exercises actual video validation."""

    def __init__(self, repo):
        self.repo = repo

    async def render(self, project, output):
        movie = output / "prototype.mp4"
        movie.write_bytes(b"unit-test-video")
        qa = output / "qa.json"
        brief = project.spec.brief
        write_json(
            qa,
            {
                "clipped_samples": 0,
                "video": {
                    "has_audio": True,
                    "width": brief.width,
                    "height": brief.height,
                    "duration": brief.duration,
                },
                "unicode_probe": "The “deadliest” answer is disputed.",
            },
        )
        return ProviderSuccess(
            artifacts=[
                self.repo.artifact(project, movie, "prototype"),
                self.repo.artifact(project, qa, "qa"),
            ]
        )


def test_full_internal_reflow_preserves_complete_countdown(tmp_path):
    brief = load_brief(Path("examples/earthquake-full.yaml"))
    studio = OfflineStudio(tmp_path)
    studio.voice = MeasuredVoice(studio.repo, durations={"rank-5": 8.0})
    studio.renderer = FakeRenderer(studio.repo)
    hitl = studio.hitl = RecordingHITL()
    project = studio.create(brief, pipeline="live", review_policy="preview")
    result = asyncio.run(studio.run(project.id))
    assert result.state == State.PROTOTYPE_APPROVAL and result.version == 2
    assert result.spec.brief.production_mode == "full"
    assert 55 < result.spec.brief.duration <= 60
    assert [s.rank for s in result.spec.shots if s.kind == "rank_reveal"] == [5, 4, 3, 2, 1]
    assert len(result.current_artifacts("scope_validation")) == 1
    assert hitl.gates == ["prototype"] and len(result.approvals) == 1


def test_preview_gate_reads_persisted_json_as_utf8(tmp_path, brief, monkeypatch):
    original = Path.read_text

    def require_explicit_encoding(path, *args, **kwargs):
        encoding = args[0] if args else kwargs.get("encoding")
        if path.suffix == ".json" and encoding is None:
            raise UnicodeError("persisted JSON must be read with an explicit encoding")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", require_explicit_encoding)
    studio = OfflineStudio(tmp_path)
    studio.voice = MeasuredVoice(studio.repo, durations={"hook": 0.5, "subject": 0.5})
    studio.renderer = FakeRenderer(studio.repo)
    studio.hitl = RecordingHITL()

    result = asyncio.run(
        studio.run(studio.create(brief, pipeline="live", review_policy="preview").id)
    )

    assert result.state == State.PROTOTYPE_APPROVAL
    qa = studio.repo.verify(result, result.current_artifacts("qa")[-1])
    assert "“deadliest”" in qa.read_text(encoding="utf-8")


@pytest.mark.integration
def test_internal_retime_delivers_one_validated_real_preview(tmp_path, brief):
    studio = OfflineStudio(tmp_path)
    studio.voice = MeasuredVoice(studio.repo)
    hitl = studio.hitl = RecordingHITL()
    project = studio.create(brief, pipeline="live", review_policy="preview")
    result = asyncio.run(studio.run(project.id))
    assert result.state == State.PROTOTYPE_APPROVAL
    assert result.version == 2  # Timing was repaired internally.
    assert hitl.gates == ["prototype"]
    assert len(result.approvals) == 1
    assert not result.feedback  # No human decisions forged.
    assert result.timing_rewrite_attempts == 0
    assert len(result.production_changes) == 1
    report = json.loads(
        studio.repo.verify(result, result.current_artifacts("narration_validation")[0]).read_text()
    )
    assert report["passed"] and len(report["measurements"]) == 2
    revision = result.revision
    assert asyncio.run(OfflineStudio(tmp_path).run(project.id)).revision == revision
    text = review_message(result, result.pending()).text
    assert "Candidate hooks:" in text and "Chosen hook:" in text
    assert "Complete script:" in text and "previous allowance" not in text


def test_migration_preserves_decisions_and_supersedes_pending_without_approval(tmp_path, brief):
    studio = OfflineStudio(tmp_path)
    project = approved(studio, brief)
    # The next timing repair requests story approval under the old policy.
    studio.voice = MeasuredVoice(studio.repo)
    old = asyncio.run(studio.run(project.id))
    pending = old.pending()
    decisions = [a.model_dump() for a in old.approvals if a.decision == "approve"]
    changed = studio.enable_preview_review(old.id, version=old.version)
    assert changed.state == State.NARRATION_PREPARING
    assert not changed.pending()
    assert next(a for a in changed.approvals if a.id == pending.id).decision == "superseded"
    assert [a.model_dump() for a in changed.approvals if a.decision == "approve"] == decisions
    assert len(changed.feedback) == len(old.feedback)
    assert studio.enable_preview_review(old.id, version=old.version).revision == changed.revision
    worker = TelegramWorker(studio, FakeTelegram(studio.repo), lambda: studio)
    asyncio.run(worker.process(callback(old)))
    assert studio.repo.load(old.id).revision == changed.revision


def test_rewrite_failure_bound_requests_targeted_narration_review(tmp_path, brief):
    studio = OfflineStudio(tmp_path)
    studio.voice = MeasuredVoice(studio.repo, durations={"hook": 9, "subject": 9})
    project = approved(studio, brief)
    studio.enable_preview_review(project.id, version=1)
    hitl = studio.hitl = RecordingHITL()

    class InvalidDirector:
        calls = 0

        async def develop(self, *args):
            self.calls += 1
            raise ValueError("invalid rewrite")

    director = studio.director = InvalidDirector()
    result = asyncio.run(studio.run(project.id))
    assert director.calls == 2
    assert result.timing_rewrite_attempts == 2
    assert result.state == State.NARRATION_APPROVAL
    assert result.pending().gate == "narration" and hitl.gates == ["narration"]
    assert result.last_error is None and result.last_error_code is None
    message = review_message(result, result.pending())
    assert message.actions == ["revise", "reject"]
    assert "Complete script:" in message.text and "previous allowance" not in message.text
    assert "not yet approvable" in message.text and "No images were prepared" in message.text
    revision = result.revision
    assert asyncio.run(studio.run(project.id)).revision == revision
    assert director.calls == 2  # A pending human review cannot repeat paid rewrites.
    with pytest.raises(ValueError, match="requires trimming feedback"):
        studio.decide(project.id, "narration", "approve")

    research = result.spec.brief.research.model_copy(deep=True)
    revised = studio.revise(
        result.id,
        "Remove the prize list and keep the collaboration theme.",
        approval_id=result.pending().id,
        actor="test-human",
    )
    assert revised.state == State.RESEARCHED and revised.version == result.version + 1
    assert revised.spec.brief.research == research
    assert revised.timing_rewrite_attempts == 0 and not revised.prepared_voices
    assert revised.feedback[-1].gate == "narration"

    class NoResearch:
        async def research(self, *args):
            raise AssertionError("human narration feedback must reuse research")

    class ShortDirector:
        feedback = None

        async def develop(self, supplied, feedback):
            self.feedback = feedback
            spec = await MockDirector().develop(supplied, [])
            for shot in spec.shots:
                for segment in shot.voice_segments:
                    segment.spoken_text = "Keep the essential point."
                    segment.estimated_duration = 1
            return spec

    short = studio.director = ShortDirector()
    studio.research = NoResearch()
    studio.voice.durations = {"hook": 1, "subject": 1}
    studio.renderer = FakeRenderer(studio.repo)
    finished = asyncio.run(studio.run(project.id))
    assert finished.state == State.PROTOTYPE_APPROVAL
    assert "NARRATION TRIM FEEDBACK" in short.feedback[-1]


def test_two_valid_but_still_long_versions_escalate_to_script_review(tmp_path, brief):
    studio = OfflineStudio(tmp_path)
    studio.voice = MeasuredVoice(studio.repo, durations={"hook": 9, "subject": 9})
    project = approved(studio, brief)
    studio.enable_preview_review(project.id, version=1)
    hitl = studio.hitl = RecordingHITL()

    class StillLongDirector:
        calls = 0

        async def develop(self, supplied, feedback):
            self.calls += 1
            spec = await MockDirector().develop(supplied, [])
            for shot in spec.shots:
                for segment in shot.voice_segments:
                    segment.spoken_text = "Keep this point."
                    segment.estimated_duration = 1
            return spec

    director = studio.director = StillLongDirector()
    result = asyncio.run(studio.run(project.id))

    assert result.version == 3 and director.calls == 2
    assert result.timing_rewrite_attempts == 2
    assert result.state == State.NARRATION_APPROVAL
    assert result.pending().gate == "narration" and hitl.gates == ["narration"]


def test_internal_rewrite_then_retime_finishes_without_intermediate_gate(tmp_path, brief):
    studio = OfflineStudio(tmp_path)
    voice = studio.voice = MeasuredVoice(studio.repo, durations={"hook": 9, "subject": 9})
    studio.renderer = FakeRenderer(studio.repo)
    project = approved(studio, brief)
    project.spec.closed_captions = False
    studio.repo.save(project, "captions_disabled", {"closed_captions": False})
    studio.enable_preview_review(project.id, version=1)
    hitl = studio.hitl = RecordingHITL()

    class ShortDirector:
        async def develop(self, supplied, feedback):
            spec = await MockDirector().develop(supplied, [])
            for shot in spec.shots:
                for segment in shot.voice_segments:
                    segment.spoken_text = "Human impact."
                    segment.estimated_duration = 1
            # New recordings are shorter, but still need a local scene reflow.
            voice.durations = {"hook": 4.0, "subject": 1.0}
            return spec

    studio.director = ShortDirector()
    result = asyncio.run(studio.run(project.id))
    assert result.state == State.PROTOTYPE_APPROVAL
    assert hitl.gates == ["prototype"]
    assert result.timing_rewrite_attempts == 1
    assert result.timing_repairs == 2
    assert result.spec.closed_captions is False
    assert len(result.production_changes) == 3  # policy change, rewrite, retime


def test_missing_qa_blocks_approval(tmp_path, brief):
    studio = OfflineStudio(tmp_path)
    studio.voice = MeasuredVoice(studio.repo, durations={"hook": 0.5, "subject": 0.5})

    class MissingQA:
        async def render(self, project, output):
            return ProviderSuccess(artifacts=[])

    studio.renderer = MissingQA()
    hitl = studio.hitl = RecordingHITL()
    project = studio.create(brief, pipeline="live", review_policy="preview")
    with pytest.raises(ValueError, match="QA"):
        asyncio.run(studio.run(project.id))
    assert not hitl.gates
    assert not studio.repo.load(project.id).pending()
