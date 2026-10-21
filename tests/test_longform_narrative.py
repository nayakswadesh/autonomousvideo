import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from autonomous_studio.domain.models import (
    LongFormContinuityIssue,
    LongFormSpec,
    NarrationBlock,
    State,
)
from autonomous_studio.hitl.adapters import review_message
from autonomous_studio.providers.longform import (
    ContinuityAuditDraft,
    MockLongFormDirector,
    MockLongFormResearch,
    OpenAILongFormDirector,
    chapter_handoff_contract,
    continuity_report,
    longform_script,
    normalize_continuity_audit,
    repair_chapters_for_issues,
    resolve_treatment_hook,
)
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "longform-pilot.yaml"


def load_example():
    return load_brief(EXAMPLE)


def test_treatment_hook_recovers_complete_first_sentence_from_truncated_selection():
    full = (
        "A mineral sample was more active than its uranium content could explain. "
        "That mismatch prompted a search for unknown elements."
    )
    draft = SimpleNamespace(
        candidate_hooks=[full, "What did Curie measure that changed the explanation?"],
        chosen_hook=full[:111],
        narrative=SimpleNamespace(hook=full),
    )
    candidates, chosen = resolve_treatment_hook(draft)
    assert chosen == "A mineral sample was more active than its uranium content could explain."
    assert candidates[0] == chosen


def test_failed_continuity_audit_with_only_repetition_warning_does_not_require_repair():
    audit = ContinuityAuditDraft(
        passed=False,
        issues=[
            LongFormContinuityIssue(
                code="repeated_material",
                message="Closing sequence repeats the same recap.",
                chapter_ids=["ch5"],
                severity="warning",
            )
        ],
    )
    issues = normalize_continuity_audit(audit)
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert issues[0].chapter_ids == ["ch5"]


def test_sun_style_repetition_findings_are_advisory_but_other_errors_remain_blocking():
    audit = ContinuityAuditDraft(
        passed=False,
        issues=[
            LongFormContinuityIssue(
                code="progression",
                message="Chapter 4 repeats the prior chapter before advancing to Earth's fate.",
                chapter_ids=["chapter-3", "chapter-4"],
            ),
            LongFormContinuityIssue(
                code="missing_chapter_evidence",
                message="An essential claim is missing.",
                chapter_ids=["chapter-5"],
            ),
        ],
    )
    issues = normalize_continuity_audit(audit)
    assert [issue.severity for issue in issues] == ["warning", "error"]


def test_failed_continuity_audit_without_issues_fails_closed():
    issues = normalize_continuity_audit(ContinuityAuditDraft(passed=False))
    assert issues[0].severity == "error"
    assert issues[0].code == "audit_failed_without_details"


def test_failed_handoff_rewrites_only_affected_chapter(monkeypatch):
    import autonomous_studio.providers.longform as longform_module

    brief = load_example()
    plan = asyncio.run(MockLongFormResearch().plan(brief))
    spec = asyncio.run(MockLongFormDirector().develop_treatment(brief, plan, []))
    ids = [chapter.id for chapter in spec.chapters]

    def report(_spec, issues=None):
        return SimpleNamespace(passed=not issues, issues=issues or [])

    monkeypatch.setattr(longform_module, "continuity_report", report)

    class FakeDirector(OpenAILongFormDirector):
        def __init__(self):
            self.client = SimpleNamespace(reject_last=lambda reason: None)
            self.written = []
            self.audits = 0

        async def _chapter_script(self, spec, chapter, previous_ending, feedback, repair=""):
            self.written.append(chapter.id)
            return [
                NarrationBlock(
                    id=f"{chapter.id}_block",
                    text=f"A clear chapter about {chapter.title} and its evidence.",
                    estimated_duration=3,
                )
            ]

        async def _audit(self, spec):
            self.audits += 1
            return (
                [
                    LongFormContinuityIssue(
                        code="unexplained_dependency",
                        message="Explain the transition between the two ideas.",
                        chapter_ids=ids[1:],
                    )
                ]
                if self.audits == 1
                else []
            )

    director = FakeDirector()
    _, result = asyncio.run(director.write_script(spec, []))
    assert result.passed
    assert director.written == ids + [ids[-1]]


def test_boundary_contract_and_repair_target_the_author_of_each_error(monkeypatch):
    import autonomous_studio.providers.longform as longform_module

    brief = load_example()
    plan = asyncio.run(MockLongFormResearch().plan(brief))
    spec = asyncio.run(MockLongFormDirector().develop_treatment(brief, plan, []))
    ids = [chapter.id for chapter in spec.chapters]
    handoff = chapter_handoff_contract(spec, spec.chapters[1])
    assert handoff["prior_chapter_id"] == ids[0]
    assert handoff["next_chapter_id"] == ids[2]
    assert "neutral editorial pivot" in handoff["closing_rule"]
    issues = [
        LongFormContinuityIssue(
            code="progression", message="The second chapter repeats the first.",
            chapter_ids=ids[:2],
        ),
        LongFormContinuityIssue(
            code="distinctions", message="The second chapter's bridge implies a false link.",
            chapter_ids=ids[1:],
        ),
    ]
    assert repair_chapters_for_issues(issues) == {ids[1]}

    def report(_spec, provider_issues=None):
        return SimpleNamespace(passed=not provider_issues, issues=provider_issues or [])

    monkeypatch.setattr(longform_module, "continuity_report", report)

    class FakeDirector(OpenAILongFormDirector):
        def __init__(self):
            self.client = SimpleNamespace(reject_last=lambda reason: None)
            self.written = []
            self.repairs = []
            self.audits = 0

        async def _chapter_script(self, spec, chapter, previous_ending, feedback, repair=""):
            self.written.append(chapter.id)
            self.repairs.append(repair)
            return [NarrationBlock(
                id=f"{chapter.id}_block", text="A grounded chapter moves the story forward.",
                estimated_duration=3,
            )]

        async def _audit(self, spec):
            self.audits += 1
            return issues if self.audits == 1 else []

    director = FakeDirector()
    asyncio.run(director.write_script(spec, []))
    assert director.written == ids + [ids[1]]
    assert "repeats" in director.repairs[-1] and "false link" in director.repairs[-1]


def run(studio, project):
    return asyncio.run(studio.run(project.id))


def test_longform_research_treatment_script_and_approval_resume(tmp_path):
    studio = Studio(tmp_path)
    project = run(studio, studio.create(load_example()))

    assert project.state == State.TREATMENT_APPROVAL
    assert project.pending().gate == "treatment"
    assert isinstance(project.spec, LongFormSpec)
    assert len(project.spec.chapters) == 3
    assert sum(chapter.target_duration for chapter in project.spec.chapters) == 210
    assert project.current_artifacts("longform_research_plan")
    assert project.current_artifacts("longform_research_coverage")
    treatment_review = review_message(project, project.pending())
    assert "Long-form treatment and chapter outline" in treatment_review.text
    assert "Generated-image allowance: up to 30" in treatment_review.text
    assert "Audience question" in treatment_review.text
    assert not project.current_artifacts("visual_asset")

    studio.decide(project.id, "treatment", "approve", actor="test")
    scripted = run(Studio(tmp_path), project)
    assert scripted.state == State.SCRIPT_APPROVAL
    assert scripted.pending().gate == "script"
    assert scripted.longform_continuity and scripted.longform_continuity.passed
    assert all(chapter.narration_blocks for chapter in scripted.spec.chapters)
    assert scripted.current_artifacts("review_script")
    assert scripted.current_artifacts("longform_continuity")
    assert not scripted.current_artifacts("visual_asset")
    assert not scripted.current_artifacts("voice")

    script_review = review_message(scripted, scripted.pending())
    assert "Continuity-validated long-form script" in script_review.text
    assert "Estimated speech:" not in script_review.text
    assert "Planned timeline:" not in script_review.text
    assert scripted.spec.narrative.hook in script_review.text
    assert scripted.spec.engagement_ending.comment_question in longform_script(scripted.spec)
    scripted.longform_continuity.issues.append(LongFormContinuityIssue(
        code="repetition",
        message="The opening may repeat the previous chapter's question.",
        chapter_ids=[scripted.spec.chapters[1].id],
        severity="warning",
    ))
    assert "For your review (not blocking approval)" in review_message(
        scripted, scripted.pending()
    ).text

    studio = Studio(tmp_path)
    ready = studio.decide(scripted.id, "script", "approve", actor="test")
    assert ready.state == State.LONGFORM_NARRATIVE_READY
    revision = ready.revision
    assert Studio(tmp_path).repo.load(ready.id).revision == revision


def test_longform_treatment_revision_reuses_research(tmp_path):
    studio = Studio(tmp_path)
    project = run(studio, studio.create(load_example()))
    research = project.spec.brief.research.model_dump(mode="json")
    first_plan = project.longform_research_plan.model_dump(mode="json")

    revised = studio.revise(
        project.id,
        "Make the middle chapter more causal and less chronological.",
        approval_id=project.pending().id,
        actor="test",
    )
    assert revised.state == State.LONGFORM_RESEARCHED
    assert revised.version == 2
    assert revised.spec.brief.research.model_dump(mode="json") == research
    assert revised.longform_research_plan.model_dump(mode="json") == first_plan

    replacement = run(Studio(tmp_path), revised)
    assert replacement.state == State.TREATMENT_APPROVAL
    assert replacement.version == 2
    assert any("more causal" in item.message for item in replacement.feedback)


def test_failed_longform_script_can_explicitly_replan_treatment_without_research(tmp_path):
    studio = Studio(tmp_path)
    project = run(studio, studio.create(load_example()))
    original_research = project.spec.brief.research.model_dump(mode="json")
    original_plan = project.longform_research_plan.model_dump(mode="json")
    studio.decide(project.id, "treatment", "approve", actor="test")
    with studio.repo.lock(project.id):
        failed = studio.repo.load(project.id)
        failed.last_error = "Long-form script failed first-draft validation"
        failed.last_error_code = "provider_validation"
        studio.repo.save(failed, "script_draft_failed", {})

    revised = studio.revise(
        project.id,
        "treatment: Replan the chapter reveal after a truncated instruction.",
        actor="test",
    )
    assert revised.version == 2
    assert revised.state == State.LONGFORM_RESEARCHED
    assert revised.spec.brief.research.model_dump(mode="json") == original_research
    assert revised.longform_research_plan.model_dump(mode="json") == original_plan
    assert revised.longform_continuity is None
    assert not revised.spec.chapters
    assert revised.last_error is None
    replacement = run(Studio(tmp_path), revised)
    assert replacement.state == State.TREATMENT_APPROVAL
    assert replacement.version == 2
    assert not replacement.spec.chapters[0].narration_blocks


def test_longform_script_revision_preserves_treatment_and_clears_script(tmp_path):
    studio = Studio(tmp_path)
    project = run(studio, studio.create(load_example()))
    studio.decide(project.id, "treatment", "approve", actor="test")
    project = run(studio, project)
    treatment = [
        (chapter.id, chapter.title, chapter.question, chapter.reveal)
        for chapter in project.spec.chapters
    ]

    revised = studio.revise(
        project.id,
        "Use warmer transitions and explain the uncertainty more naturally.",
        approval_id=project.pending().id,
        actor="test",
    )
    assert revised.state == State.LONGFORM_SCRIPTING
    assert revised.longform_continuity is None
    assert all(not chapter.narration_blocks for chapter in revised.spec.chapters)
    assert treatment == [
        (chapter.id, chapter.title, chapter.question, chapter.reveal)
        for chapter in revised.spec.chapters
    ]

    replacement = run(Studio(tmp_path), revised)
    assert replacement.state == State.SCRIPT_APPROVAL
    assert replacement.longform_continuity.passed


def test_longform_research_plan_checkpoint_survives_provider_failure(tmp_path):
    class FailingOnceResearch(MockLongFormResearch):
        def __init__(self):
            self.plan_calls = 0
            self.research_calls = 0

        async def plan(self, brief):
            self.plan_calls += 1
            return await super().plan(brief)

        async def research(self, brief, plan):
            self.research_calls += 1
            if self.research_calls == 1:
                raise RuntimeError("temporary research failure")
            return await super().research(brief, plan)

    provider = FailingOnceResearch()
    studio = Studio(tmp_path, longform_research=provider)
    project = studio.create(load_example())
    with pytest.raises(RuntimeError, match="temporary research failure"):
        run(studio, project)

    failed = studio.repo.load(project.id)
    assert failed.state == State.LONGFORM_FOUNDATION_READY
    assert failed.longform_research_plan is not None
    resumed = run(Studio(tmp_path, longform_research=provider), failed)
    assert resumed.state == State.TREATMENT_APPROVAL
    assert provider.plan_calls == 1
    assert provider.research_calls == 2


class ExpandedMockDirector(MockLongFormDirector):
    async def write_script(self, spec, feedback):
        scripted, _ = await super().write_script(spec, feedback)
        current_words = len(longform_script(scripted).split())
        extra = max(0, 630 - current_words)
        scripted.chapters[1].narration_blocks[-1].text += (
            " " + " ".join(f"context{index}" for index in range(extra)) + "."
        )
        scripted.chapters[1].narration_blocks[-1].estimated_duration = (
            len(scripted.chapters[1].narration_blocks[-1].text.split())
            / scripted.brief.voice.pace_wpm
            * 60
        )
        return scripted, continuity_report(scripted)


def test_substantive_overrun_reaches_script_review_and_approval_expands_timeline(tmp_path):
    studio = Studio(tmp_path, longform_director=ExpandedMockDirector())
    project = run(studio, studio.create(load_example()))
    studio.decide(project.id, "treatment", "approve", actor="test")

    scripted = run(studio, project)
    report = scripted.longform_continuity
    assert scripted.state == State.SCRIPT_APPROVAL
    assert report.passed and report.duration_status == "expansion_proposed"
    assert report.target_timeline_seconds == 210
    assert 210 < report.proposed_timeline_seconds <= 600
    assert any(
        issue.code == "duration_expansion_proposed" and issue.severity == "warning"
        for issue in report.issues
    )
    review = review_message(scripted, scripted.pending()).text
    assert "Original target: 210.0s" in review
    assert "Approval accepts this expanded runtime" in review

    approved = studio.decide(scripted.id, "script", "approve", actor="test")
    assert approved.state == State.LONGFORM_NARRATIVE_READY
    assert approved.spec.brief.duration == report.proposed_timeline_seconds
    assert sum(chapter.target_duration for chapter in approved.spec.chapters) == pytest.approx(
        report.proposed_timeline_seconds
    )
    event = studio.repo.events(approved.id)[-1]
    assert event["payload"]["duration_expansion"]["policy"] == "elastic_longform_v0.2.5"


def test_script_beyond_configured_expansion_ceiling_remains_invalid():
    brief = load_example()
    plan = asyncio.run(MockLongFormResearch().plan(brief))
    director = MockLongFormDirector()
    treatment = asyncio.run(director.develop_treatment(brief, plan, []))
    scripted, _ = asyncio.run(director.write_script(treatment, []))
    current_words = len(longform_script(scripted).split())
    over_ceiling_words = round(
        brief.duration_policy.expansion_maximum_seconds * brief.voice.pace_wpm / 60
    ) + 50
    scripted.chapters[1].narration_blocks[-1].text += (
        " "
        + " ".join(f"detail{index}" for index in range(max(0, over_ceiling_words - current_words)))
        + "."
    )

    report = continuity_report(scripted)
    assert not report.passed
    assert any(issue.code == "script_exceeds_expansion_limit" for issue in report.issues)


def test_340_second_script_proposes_expansion_with_600_second_ceiling():
    brief = load_example()
    plan = asyncio.run(MockLongFormResearch().plan(brief))
    director = MockLongFormDirector()
    treatment = asyncio.run(director.develop_treatment(brief, plan, []))
    scripted, _ = asyncio.run(director.write_script(treatment, []))
    desired_words = round(340 * brief.voice.pace_wpm / 60)
    current_words = len(longform_script(scripted).split())
    scripted.chapters[1].narration_blocks[-1].text += (
        " "
        + " ".join(f"detail{index}" for index in range(max(0, desired_words - current_words)))
        + "."
    )

    report = continuity_report(scripted)
    assert report.passed
    assert report.duration_status == "expansion_proposed"
    assert 340 < report.proposed_timeline_seconds < 600
    assert not any(issue.code == "script_exceeds_expansion_limit" for issue in report.issues)


def test_legacy_duration_validation_failure_can_be_retried_after_policy_upgrade(tmp_path):
    studio = Studio(tmp_path)
    project = run(studio, studio.create(load_example()))
    studio.decide(project.id, "treatment", "approve", actor="test")
    project = studio.repo.load(project.id)
    project.last_error_code = "provider_validation"
    project.last_error = (
        "Long-form script failed validation after bounded internal repairs: Estimated speech is "
        "253.2s; keep it at or below 199.5s to preserve visual breathing room."
    )
    studio.repo.save(project, "legacy_duration_failure_fixture", {})

    retried = studio.request_retry(project.id, actor="test")
    assert retried.state == State.LONGFORM_SCRIPTING
    assert retried.last_error is None and retried.last_error_code is None
