"""The new story contract is pinned to future long-form cuts, not old approvals."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from autonomous_studio.config import Settings
from autonomous_studio.domain.longform_policy import LongFormCreativePolicy
from autonomous_studio.domain.models import (
    LongFormResearchPlan,
    LongFormSpec,
    State,
    hashable_longform_chapter,
)
from autonomous_studio.hitl.adapters import review_message
from autonomous_studio.providers.longform import (
    ChapterScriptDraft,
    LongFormTreatmentDraft,
    MockLongFormDirector,
    MockLongFormResearch,
    OpenAILongFormDirector,
    OpenAILongFormResearch,
    StoryFirstTreatmentDraft,
    continuity_report,
    script_editorial_contract,
    treatment_spec,
)
from autonomous_studio.providers.openai import (
    ProviderValidationError,
    strict_schema,
    validate_strict_schema,
)
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "longform-pilot.yaml"


def fixture_story():
    brief = load_brief(EXAMPLE)
    plan = asyncio.run(MockLongFormResearch().plan(brief))
    director = MockLongFormDirector()
    treatment = asyncio.run(director.develop_treatment(
        brief, plan, [], story_contract_version=2
    ))
    scripted, _ = asyncio.run(director.write_script(treatment, []))
    return brief, plan, treatment, scripted


def test_story_and_landing_response_schemas_fit_openai_strict_subset():
    for model in (LongFormResearchPlan, StoryFirstTreatmentDraft, ChapterScriptDraft):
        validate_strict_schema(strict_schema(model.model_json_schema()))


def test_default_landing_preserves_old_chapter_hash_input():
    _, _, _, scripted = fixture_story()
    chapter = scripted.chapters[0]
    old_compatible = hashable_longform_chapter(chapter)
    assert all("landing_after" not in block for block in old_compatible["narration_blocks"])
    chapter.narration_blocks[0].landing_after = "idea_landing"
    marked = hashable_longform_chapter(chapter)
    assert marked["narration_blocks"][0]["landing_after"] == "idea_landing"


@pytest.mark.parametrize("chapter_count", [1, 2, 5, 6])
def test_story_first_chapter_count_follows_story_not_a_fixed_range(chapter_count):
    brief, plan, treatment, _ = fixture_story()
    payload = treatment_payload(treatment)
    template = payload["chapters"][0]
    payload["chapters"] = [
        {**template, "id": f"chapter_{index}", "order": index,
         "title": f"Distinct turn {index}",
         "target_duration": brief.duration / chapter_count}
        for index in range(1, chapter_count + 1)
    ]
    draft = StoryFirstTreatmentDraft.model_validate(payload)
    result = treatment_spec(brief, draft, story_contract_version=2)
    restored = LongFormSpec.model_validate(result.model_dump(mode="json"))
    assert len(restored.chapters) == chapter_count
    client = FakeClient([payload])
    live_result = asyncio.run(OpenAILongFormDirector(client).develop_treatment(
        brief, plan, [], story_contract_version=2,
    ))
    assert len(live_result.chapters) == chapter_count
    assert "fewest chapters needed for distinct" in client.calls[0][1]
    assert "3–5 chapter" not in client.calls[0][1]


def completed(payload):
    return {
        "status": "completed",
        "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": json.dumps(payload)}],
        }],
    }


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.settings = Settings(api_key="test")
        self.longform_creative_policy = LongFormCreativePolicy()
        self.trace = []

    async def response(self, model, prompt, **kwargs):
        self.calls.append((model, prompt, kwargs))
        return completed(self.replies.pop(0))

    def reject_last(self, _reason):
        pass


def treatment_payload(spec):
    return {
        "candidate_hooks": spec.candidate_hooks,
        "chosen_hook": spec.narrative.hook,
        "treatment_summary": spec.treatment_summary,
        "narrative": spec.narrative.model_dump(mode="json"),
        "story_design": spec.story_design.model_dump(mode="json"),
        "story_choice": (
            spec.story_choice.model_dump(mode="json") if spec.story_choice else None
        ),
        "chapters": [
            chapter.model_dump(
                mode="json",
                exclude={"spoken_unit_version", "narration_blocks", "visual_beats"},
            )
            for chapter in spec.chapters
        ],
        "engagement_ending": spec.engagement_ending.model_dump(mode="json"),
    }


def test_story_first_reveal_has_room_for_complete_guidance_without_saving_truncation():
    brief, plan, treatment, _ = fixture_story()
    payload = treatment_payload(treatment)
    complete = ("Explain the observation and its significance. " * 15).strip()
    assert 500 < len(complete) < 1500
    payload["chapters"][0]["reveal"] = complete
    draft = StoryFirstTreatmentDraft.model_validate(payload)
    assert treatment_spec(brief, draft, story_contract_version=2).chapters[0].reveal == complete

    payload["chapters"][0]["reveal"] = ("Explain the observation. " * 25)[:497] + " an"
    with pytest.raises(ValidationError, match="ends mid-sentence"):
        StoryFirstTreatmentDraft.model_validate(payload)


def test_research_plan_tests_story_questions_before_treatment_selection():
    brief, plan, _, _ = fixture_story()
    client = FakeClient([plan.model_dump(mode="json")])
    result = asyncio.run(OpenAILongFormResearch(client).plan(brief))
    assert len(result.story_questions) == 2
    assert result.story_questions[0].contrary_evidence_to_check
    assert client.calls[0][0] == "gpt-6-luna"
    assert "Do not choose the winner yet" in client.calls[0][1]


def test_story_choice_is_grounded_and_explained_at_treatment_gate():
    brief, plan, treatment, _ = fixture_story()
    payload = treatment_payload(treatment)
    client = FakeClient([payload])
    chosen = asyncio.run(OpenAILongFormDirector(client).develop_treatment(
        brief, plan, [], story_contract_version=2,
    ))
    assert client.calls[0][0] == "gpt-6.1-sol"
    assert "select the strongest evidence-supported candidate" in client.calls[0][1]
    assert chosen.story_choice.question_id == plan.story_questions[0].id
    assert chosen.narrative.central_question == plan.story_questions[0].question
    from autonomous_studio.workflow.engine import Studio

    review = Studio._longform_treatment_text(chosen)
    assert "Chosen story question:" in review
    assert "Why this angle:" in review


@pytest.mark.parametrize("invalid_choice", ["missing", "unknown_question", "unknown_claim"])
def test_treatment_rejects_unsubstantiated_story_choice(invalid_choice):
    brief, plan, treatment, _ = fixture_story()
    payload = treatment_payload(treatment)
    if invalid_choice == "missing":
        payload["story_choice"] = None
    elif invalid_choice == "unknown_question":
        payload["story_choice"]["question_id"] = "unplanned"
    else:
        payload["story_choice"]["supporting_claim_ids"] = ["unresearched"]
    with pytest.raises(ValueError):
        treatment_spec(
            brief, StoryFirstTreatmentDraft.model_validate(payload),
            story_contract_version=2, plan=plan,
        )


def test_older_research_plan_without_story_questions_remains_loadable():
    brief, plan, treatment, _ = fixture_story()
    old_plan = plan.model_dump(mode="json")
    old_plan.pop("story_questions")
    restored = LongFormResearchPlan.model_validate(old_plan)
    payload = treatment_payload(treatment)
    payload["story_choice"] = None
    result = treatment_spec(
        brief, StoryFirstTreatmentDraft.model_validate(payload),
        story_contract_version=2, plan=restored,
    )
    assert result.story_choice is None


def script_replies(spec):
    return [
        {
            "chapter_id": chapter.id,
            "narration_blocks": [
                {
                    "id": block.id,
                    "text": block.text,
                    "claim_ids": block.claim_ids,
                    "delivery_direction": block.delivery_direction,
                }
                for block in chapter.narration_blocks
            ],
        }
        for chapter in spec.chapters
    ]


def test_chapter_writer_carries_selective_landing_cues_into_saved_blocks():
    _, _, treatment, scripted = fixture_story()
    reply = script_replies(scripted)[0]
    reply["narration_blocks"][0]["landing_after"] = "idea_landing"
    reply["narration_blocks"][-1]["landing_after"] = "chapter_turn"
    client = FakeClient([reply])
    blocks = asyncio.run(OpenAILongFormDirector(client)._chapter_script(
        treatment, treatment.chapters[0], "", [],
    ))
    assert blocks[0].landing_after == "idea_landing"
    assert blocks[-1].landing_after == "chapter_turn"
    assert "End blocks at natural thought boundaries" in client.calls[0][1]
    restored = type(blocks[0]).model_validate({
        key: value for key, value in blocks[0].model_dump().items()
        if key != "landing_after"
    })
    assert restored.landing_after == "ordinary"


def audit_payload(*, factual_status="pass"):
    dimensions = {
        key: {
            "status": "pass",
            "finding": "The reviewed script satisfies this editorial dimension.",
            "chapter_ids": [],
        }
        for key in (
            "factual_integrity", "promise_payoff", "newcomer_clarity", "narrative_momentum"
        )
    }
    dimensions["factual_integrity"]["status"] = factual_status
    if factual_status == "error":
        dimensions["factual_integrity"]["finding"] = (
            "The script invents a consequential cause not supported by the approved evidence."
        )
        dimensions["factual_integrity"]["chapter_ids"] = ["chapter_1"]
    return {"passed": True, "issues": [], "editorial_review": dimensions}


def test_new_live_topic_pins_story_contract_without_migrating_old_projects(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    new = studio.create_longform_topic("How the Moon formed")
    assert new.spec.story_contract_version == 2
    assert Studio(tmp_path).repo.load(new.id).spec.story_contract_version == 2

    old = studio.create(load_brief(EXAMPLE))
    saved = old.spec.model_dump(mode="json")
    saved.pop("story_contract_version")
    saved.pop("story_design")
    assert LongFormSpec.model_validate(saved).story_contract_version == 1


def test_new_topic_keeps_story_contract_through_treatment_and_script_gates(tmp_path):
    class OfflineStudio(Studio):
        def configure_live(self, _project):
            pass

    class FixtureResearch(MockLongFormResearch):
        async def research(self, _brief, _plan):
            return load_brief(EXAMPLE).research.model_copy(deep=True)

    studio = OfflineStudio(tmp_path, settings=Settings(api_key="test"))
    studio.longform_research = FixtureResearch()
    created = studio.create_longform_topic("How the Moon formed")
    treatment = asyncio.run(studio.run(created.id))
    assert treatment.state == State.TREATMENT_APPROVAL
    assert treatment.spec.story_contract_version == 2
    assert treatment.spec.story_design is not None
    assert all(chapter.essential_claim_ids for chapter in treatment.spec.chapters)
    treatment_review = review_message(treatment, treatment.pending()).text
    assert "Candidate story questions:" in treatment_review
    assert "Chosen story question:" in treatment_review
    assert "Why this angle:" in treatment_review
    assert treatment.spec.narrative.central_question in treatment_review
    assert "Central tension:" in treatment_review
    assert "Payoff:" in treatment_review
    assert "Essential evidence:" in treatment_review

    studio.decide(created.id, "treatment", "approve", actor="test")
    scripted = asyncio.run(studio.run(created.id))
    assert scripted.state == State.SCRIPT_APPROVAL
    assert scripted.spec.story_contract_version == 2
    assert scripted.longform_continuity.passed
    assert OfflineStudio(tmp_path).repo.load(created.id).spec.story_contract_version == 2


def test_story_first_treatment_designates_only_essential_claims():
    brief, _, treatment, scripted = fixture_story()
    assert treatment.story_contract_version == 2
    assert treatment.story_design.central_tension
    assert treatment.story_design.payoff
    assert all(chapter.essential_claim_ids for chapter in treatment.chapters)
    assert all(set(chapter.essential_claim_ids) <= set(chapter.evidence_claim_ids)
               for chapter in treatment.chapters)

    scripted.chapters[0].evidence_claim_ids.append("c2")
    assert continuity_report(scripted).passed  # c2 is background, not required speech.
    scripted.chapters[0].essential_claim_ids.append("c2")
    report = continuity_report(scripted)
    assert not report.passed
    assert any(issue.code == "missing_chapter_evidence" and "c2" in issue.message
               for issue in report.issues)

    legacy_payload = treatment_payload(treatment)
    legacy_payload.pop("story_design")
    legacy_payload.pop("story_choice")
    for chapter in legacy_payload["chapters"]:
        chapter.pop("essential_claim_ids")
    legacy_draft = LongFormTreatmentDraft(**legacy_payload)
    with pytest.raises(ValueError, match="story-first treatment requires"):
        treatment_spec(brief, legacy_draft, story_contract_version=2)
    with pytest.raises(ValidationError, match="designated essential claims"):
        LongFormSpec.model_validate({
            **treatment.model_dump(mode="json"),
            "chapters": [
                {**chapter.model_dump(mode="json"), "essential_claim_ids": []}
                if index == 0 else chapter.model_dump(mode="json")
                for index, chapter in enumerate(treatment.chapters)
            ],
        })
    mismatched = treatment.model_dump(mode="json")
    mismatched["chapters"][0]["evidence_source_ids"] = ["nasa_moon_facts"]
    with pytest.raises(ValidationError, match="matching reviewed sources"):
        LongFormSpec.model_validate(mismatched)


def test_story_first_writer_and_luna_audit_share_contract_and_persist_dimensions():
    brief, plan, treatment, scripted = fixture_story()
    client = FakeClient([
        treatment_payload(treatment), *script_replies(scripted), audit_payload(),
    ])
    director = OpenAILongFormDirector(client)
    result = asyncio.run(director.develop_treatment(
        brief, plan, [], story_contract_version=2
    ))
    result, report = asyncio.run(director.write_script(result, []))

    assert result.story_contract_version == 2 and report.passed
    assert report.editorial_review is not None
    assert report.editorial_review.newcomer_clarity.status == "pass"
    assert client.calls[0][0] == "gpt-6.1-sol"
    assert all(call[0] == "gpt-6.1-sol" for call in client.calls[1:-1])
    assert client.calls[-1][0] == "gpt-6-luna"
    assert "essential_claim_ids" in client.calls[0][1]
    assert "supporting background, not a checklist" in client.calls[1][1]
    assert "narrative_momentum" in client.calls[-1][1]
    assert "essential_claim_ids_by_chapter" in client.calls[-1][1]
    assert "research_roles" in script_editorial_contract(result)


def test_repetition_only_luna_audit_allows_script_review_without_rewriting():
    _, _, treatment, scripted = fixture_story()
    audit = audit_payload()
    audit["passed"] = False
    audit["issues"] = [{
        "code": "progression",
        "message": "The next chapter repeats the previous transition before advancing.",
        "chapter_ids": [scripted.chapters[1].id, scripted.chapters[2].id],
        "severity": "error",
    }]
    audit["editorial_review"]["narrative_momentum"] = {
        "status": "error",
        "finding": "The chapter boundary repeats its setup and could move faster.",
        "chapter_ids": [scripted.chapters[2].id],
    }
    client = FakeClient([*script_replies(scripted), audit])
    _, report = asyncio.run(OpenAILongFormDirector(client).write_script(treatment, []))

    assert report.passed
    assert all(issue.severity == "warning" for issue in report.issues)
    assert report.editorial_review.narrative_momentum.status == "warning"
    assert len(client.calls) == len(scripted.chapters) + 1
    assert client.calls[-1][0] == "gpt-6-luna"


def test_exact_duplicate_sentence_is_a_review_warning():
    _, _, _, scripted = fixture_story()
    first_sentence = scripted.chapters[0].narration_blocks[0].text.split(".")[0] + "."
    scripted.chapters[1].narration_blocks[0].text += " " + first_sentence

    report = continuity_report(scripted)
    assert report.passed
    assert any(issue.code == "duplicate_sentence" and issue.severity == "warning"
               for issue in report.issues)


def test_factual_audit_failure_stops_after_one_script_draft():
    _, _, treatment, scripted = fixture_story()
    client = FakeClient([*script_replies(scripted), audit_payload(factual_status="error")])
    with pytest.raises(ProviderValidationError, match="consequential cause"):
        asyncio.run(OpenAILongFormDirector(client).write_script(treatment, []))
    assert len(client.calls) == len(treatment.chapters) + 1
    assert client.replies == []


def test_invalid_essential_treatment_fails_without_redrafting():
    brief, plan, treatment, _ = fixture_story()
    invalid = treatment_payload(treatment)
    invalid["chapters"][0]["essential_claim_ids"] = ["unreviewed_claim"]
    client = FakeClient([invalid, treatment_payload(treatment)])

    with pytest.raises(ProviderValidationError, match="essential chapter claims"):
        asyncio.run(OpenAILongFormDirector(client).develop_treatment(
            brief, plan, [], story_contract_version=2
        ))

    assert len(client.calls) == 1
    assert len(client.replies) == 1
    assert "chosen hook's promised question" in client.calls[0][1]
