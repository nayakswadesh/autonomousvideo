import asyncio
from pathlib import Path

import pytest

from autonomous_studio.domain.models import State
from autonomous_studio.storage.repository import digest, write_json
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief


def run(studio, project):
    return asyncio.run(studio.run(project.id))


def test_restart_gates_revisions_and_stale_feedback(tmp_path, brief):
    studio = Studio(tmp_path)
    project = studio.create(brief)
    assert run(studio, project).state == State.STORY_APPROVAL
    project = studio.repo.load(project.id)
    old_request = project.pending().id
    revision = project.revision
    assert run(Studio(tmp_path), project).revision == revision  # no duplicate request
    with pytest.raises(ValueError, match="pending"):
        studio.decide(project.id, "hero", "approve")
    with pytest.raises(ValueError, match="render requires"):
        asyncio.run(studio.render(project.id))
    snapshots = {a.path: a.sha256 for a in project.artifacts}
    updated = brief.model_copy(deep=True)
    updated.hook = "Changed perspective"
    revised = studio.revise(project.id, "Change the hook", brief=updated)
    assert revised.version == 2
    revised = run(Studio(tmp_path), revised)
    assert revised.spec.hook == "Changed perspective"
    with pytest.raises(ValueError, match="stale"):
        studio.decide(revised.id, "story", "approve", approval_id=old_request)
    with pytest.raises(ValueError, match="stale"):
        studio.decide(revised.id, "story", "approve", version=1)
    studio.decide(revised.id, "story", "approve", actor="test-harness")
    hero = run(Studio(tmp_path), revised)
    assert hero.state == State.HERO_APPROVAL
    assert len(hero.current_artifacts("hero_frame")) == 4
    for path, checksum in snapshots.items():
        assert digest(studio.repo.resolve(project.id, path)) == checksum
    rejected = studio.decide(hero.id, "hero", "reject", message="Needs a new art direction")
    assert rejected.state == State.REJECTED
    assert run(studio, rejected).revision == rejected.revision
    resumed = studio.revise(rejected.id, "Try again")
    assert resumed.state == State.CREATED
    assert resumed.feedback[-1].decision == "revise"


def test_failed_stage_retains_checkpoint_and_retry(tmp_path, brief):
    class FailingDirector:
        async def develop(self, brief, feedback):
            raise RuntimeError("simulated provider failure")

    studio = Studio(tmp_path, director=FailingDirector())
    project = studio.create(brief)
    with pytest.raises(RuntimeError, match="simulated"):
        run(studio, project)
    stored = Studio(tmp_path).repo.load(project.id)
    assert stored.state == State.RESEARCHED
    assert stored.last_error
    assert run(Studio(tmp_path), project).state == State.STORY_APPROVAL
    assert sum(e["payload"].get("to") == "RESEARCHED" for e in studio.repo.events(project.id)) == 1


def test_tampered_artifact_blocks_approval(tmp_path, brief):
    studio = Studio(tmp_path)
    project = run(studio, studio.create(brief))
    path = studio.repo.resolve(project.id, project.current_artifacts("story")[0].path)
    path.write_text("modified", encoding="utf-8")
    with pytest.raises(ValueError, match="modified artifact"):
        studio.decide(project.id, "story", "approve")


def test_second_topic_uses_same_engine(tmp_path):
    brief = load_brief(Path("examples/solar-system.yaml"))
    studio = Studio(tmp_path)
    project = run(studio, studio.create(brief))
    assert project.spec.shots[1].title == "Jupiter"
    assert all("earthquake" not in s.title.lower() for s in project.spec.shots)
    studio.decide(project.id, "story", "approve")
    assert run(studio, project).state == State.HERO_APPROVAL


def test_optimistic_concurrency_and_project_path(tmp_path, brief):
    studio = Studio(tmp_path)
    project = studio.create(brief)
    stale = studio.repo.load(project.id)
    studio.repo.save(project, "test", {})
    with pytest.raises(RuntimeError, match="concurrent"):
        studio.repo.save(stale, "test", {})
    with pytest.raises(ValueError):
        studio.repo.resolve(project.id, "../../outside.txt")
    with pytest.raises(ValueError):
        studio.repo.load("../outside")


def test_project_lock_is_exclusive(tmp_path, brief):
    studio = Studio(tmp_path)
    project = studio.create(brief)
    with studio.repo.lock(project.id):
        with pytest.raises(RuntimeError, match="busy"):
            with Studio(tmp_path).repo.lock(project.id):
                pass


def test_full_script_gate_locks_validated_narration_before_assets(tmp_path):
    full = load_brief(Path("examples/earthquake-full.yaml"))
    studio = Studio(tmp_path)
    project = studio.create(full, review_policy="preview", script_approval=True)
    project.spec = asyncio.run(studio.director.develop(full, []))
    project.spec.narration_script = " ".join(
        segment.spoken_text for shot in project.spec.shots for segment in shot.voice_segments
    )
    project.spec.candidate_hooks = [project.spec.hook]
    project.spec.shots[0].shot_type_reasoning = (
        "The spoken opening promise needs an immediate visible title."
    )
    from autonomous_studio.domain.models import ShotTypeReview

    project.spec.shot_type_reviews = [
        ShotTypeReview(
            shot_id=project.spec.shots[0].id,
            suggested_kind="spatial_cutaway",
            reason="An interior view would clarify the described layers.",
        )
    ]
    output = studio.repo.attempt_dir(project, "script-test")
    timing_path = output / "narration-validation.json"
    write_json(
        timing_path,
        {"passed": True, "duration": full.duration, "version": project.version},
    )
    project.artifacts.append(studio.repo.artifact(project, timing_path, "narration_validation"))

    asyncio.run(studio._request_script_approval(project, output))

    stored = studio.repo.load(project.id)
    assert stored.state == State.SCRIPT_APPROVAL and stored.pending().gate == "script"
    assert stored.image_requests == 0 and not stored.current_artifacts("visual_asset")
    request = stored.current_artifacts("review_script")[-1]
    review = studio.repo.resolve(stored.id, request.path).read_text(encoding="utf-8")
    assert "Timing-validated script" in review
    assert "advisory only" in review
    assert "spatial_cutaway" in review
    assert "Chosen shot types (why):" in review
    assert project.spec.shots[0].shot_type_reasoning in review
    assert all(
        segment.spoken_text in review
        for shot in project.spec.shots
        for segment in shot.voice_segments
    )
    studio.decide(stored.id, "script", "approve", actor="test")

    changed = studio.repo.load(stored.id)
    changed.spec.narration_script += " Changed after approval."
    with pytest.raises(ValueError, match="no longer matches"):
        studio._verify_script_approval(changed)


def test_rejected_short_script_can_request_fresh_version_from_saved_research(tmp_path):
    full = load_brief(Path("examples/earthquake-full.yaml"))
    studio = Studio(tmp_path)
    project = studio.create(full, review_policy="preview", script_approval=True)
    project.spec = asyncio.run(studio.director.develop(full, []))
    project.spec.narration_script = " ".join(
        segment.spoken_text for shot in project.spec.shots for segment in shot.voice_segments
    )
    old_script = project.spec.narration_script
    research = project.spec.brief.research.model_dump(mode="json")
    output = studio.repo.attempt_dir(project, "script-rejection-test")
    studio._json(
        project, output, "narration-validation.json", {"passed": True}, "narration_validation"
    )
    asyncio.run(studio._request_script_approval(project, output))
    rejected = studio.decide(project.id, "script", "reject", actor="test")

    revised = studio.revise(
        project.id,
        "Write a fresh supported-wonder script from the saved evidence.",
        actor="test",
    )
    assert rejected.state == State.REJECTED
    assert revised.state == State.RESEARCHED and revised.version == 2
    assert revised.spec.brief.research.model_dump(mode="json") == research
    assert revised.spec.brief.hook == ""
    assert revised.spec.brief.hook_narration == ""
    assert revised.spec.brief.thesis == ""
    assert revised.spec.brief.payoff == ""
    assert revised.short_revision_script == old_script
    assert revised.approvals[-1].decision == "reject"
    assert revised.feedback[-1].gate == "script"
    assert revised.current_artifacts("voice") == []
    assert studio.repo.events(project.id)[-1]["kind"] == "script_revision_requested"

    captured = []
    original_director = studio.director

    class CapturingDirector:
        async def develop(self, brief, feedback):
            captured.extend(feedback)
            return await original_director.develop(brief, feedback)

    studio.director = CapturingDirector()
    asyncio.run(studio._step(revised))
    assert captured == ["Write a fresh supported-wonder script from the saved evidence."]


def test_pending_short_script_revision_discards_old_creative_fields(tmp_path):
    full = load_brief(Path("examples/earthquake-full.yaml"))
    studio = Studio(tmp_path)
    project = studio.create(full, review_policy="preview", script_approval=True)
    project.spec = asyncio.run(studio.director.develop(full, []))
    project.spec.narration_script = " ".join(
        segment.spoken_text for shot in project.spec.shots for segment in shot.voice_segments
    )
    old_script = project.spec.narration_script
    assert project.spec.hook and project.spec.brief.hook
    research = project.spec.brief.research.model_dump(mode="json")
    output = studio.repo.attempt_dir(project, "pending-script-revision-test")
    studio._json(
        project, output, "narration-validation.json", {"passed": True}, "narration_validation"
    )
    asyncio.run(studio._request_script_approval(project, output))

    revised = studio.revise(project.id, "Replace the opening with an unresolved question.")
    assert revised.version == 2 and revised.state == State.RESEARCHED
    assert revised.spec.brief.research.model_dump(mode="json") == research
    assert revised.spec.brief.hook == ""
    assert revised.spec.brief.hook_narration == ""
    assert revised.spec.brief.thesis == ""
    assert revised.spec.brief.payoff == ""
    assert revised.short_revision_script == old_script
    assert revised.approvals[-1].decision == "revise"

    captured = []
    original_director = studio.director

    class CapturingDirector:
        async def develop(self, brief, feedback):
            captured.extend(feedback)
            return await original_director.develop(brief, feedback)

    studio.director = CapturingDirector()
    asyncio.run(studio._step(revised))
    assert captured[-1] == "Replace the opening with an unresolved question."
    assert "context only" in captured[0]
    assert old_script in captured[0]
