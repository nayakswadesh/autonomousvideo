import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from autonomous_studio.domain.longform_feedback import (
    has_image_quota,
    relevant_longform_feedback,
    remove_image_quotas,
)
from autonomous_studio.domain.models import LongFormVisualPolicy
from autonomous_studio.providers.longform import (
    LongFormTreatmentDraft,
    MockLongFormDirector,
    MockLongFormResearch,
    OpenAILongFormDirector,
    TreatmentChapterDraft,
    treatment_spec,
)
from autonomous_studio.providers.longform_visuals import OpenAILongFormVisualPlanner
from autonomous_studio.workflow.engine import load_brief

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "longform-pilot.yaml"


def test_historical_image_budget_does_not_enter_new_creative_feedback():
    feedback = [
        SimpleNamespace(
            decision="revise",
            message=(
                "Replan with a generated-image budget of up to 14. "
                "Prefer authentic evidence when it truly fits."
            ),
        ),
        SimpleNamespace(
            decision="revise",
            message=(
                "treatment: Fresh story. Use 30-image cap on story-critical scenes, "
                "label places when spoken and keep images on screen while explained."
            ),
        ),
        SimpleNamespace(
            decision="revise",
            message="Remove numerical image allocations from the treatment.",
        ),
    ]
    messages = relevant_longform_feedback(SimpleNamespace(feedback=feedback))
    assert len(messages) == 2
    assert "14" not in " ".join(messages)
    assert "30-image" not in " ".join(messages)
    assert "label places" in messages[0]


def test_treatment_removes_all_generated_image_quotas_but_retains_story():
    brief = load_brief(EXAMPLE)
    director = MockLongFormDirector()
    research = asyncio.run(MockLongFormResearch().plan(brief))
    spec = asyncio.run(director.develop_treatment(brief, research, []))
    chapter_fields = set(TreatmentChapterDraft.model_fields)
    chapters = [
        TreatmentChapterDraft.model_validate(
            {
                **chapter.model_dump(include=chapter_fields),
                "purpose": chapter.purpose
                + " Reserve up to three generated images only where archives are unavailable.",
            }
        )
        for chapter in spec.chapters
    ]
    draft = LongFormTreatmentDraft(
        candidate_hooks=spec.candidate_hooks,
        chosen_hook=spec.narrative.hook,
        treatment_summary=(
            spec.treatment_summary
            + " Use no more than 14 newly generated images, favor archival material."
        ),
        narrative=spec.narrative,
        chapters=chapters,
        engagement_ending=spec.engagement_ending,
    )
    clean = treatment_spec(brief, draft)
    assert "14" not in clean.treatment_summary
    assert not has_image_quota(clean.treatment_summary)
    assert all(not has_image_quota(chapter.purpose) for chapter in clean.chapters)
    assert all("Reserve up to" not in chapter.purpose for chapter in clean.chapters)
    assert clean.narrative.hook == spec.narrative.hook
    assert [chapter.evidence_claim_ids for chapter in clean.chapters] == [
        chapter.evidence_claim_ids for chapter in spec.chapters
    ]
    assert remove_image_quotas("Explain the archive. Allocate four illustrations.") == (
        "Explain the archive."
    )


def test_provider_prompts_cannot_replay_old_image_counts():
    class Captured(Exception):
        pass

    class CaptureClient:
        settings = SimpleNamespace(director_model="fixture")
        prompt = ""

        async def response(self, model, prompt, **kwargs):
            self.prompt = prompt
            raise Captured

    brief = load_brief(EXAMPLE)
    research = asyncio.run(MockLongFormResearch().plan(brief))
    stale = ["Replan with a generated-image budget of up to 14. Keep archival evidence."]
    client = CaptureClient()
    with pytest.raises(Captured):
        asyncio.run(OpenAILongFormDirector(client).develop_treatment(brief, research, stale))
    assert "up to 14" not in client.prompt
    assert "Keep archival evidence" in client.prompt

    spec = asyncio.run(MockLongFormDirector().develop_treatment(brief, research, []))
    spec.treatment_summary += " Use no more than 14 newly generated images."
    spec.chapters[0].purpose += " Reserve up to three generated images."
    with pytest.raises(Captured):
        asyncio.run(
            OpenAILongFormVisualPlanner(client).plan(
                spec, LongFormVisualPolicy(maximum_generated_requests=30), stale
            )
        )
    assert "up to 14" not in client.prompt
    assert "Reserve up to three generated images" not in client.prompt
    assert '"maximum_generated_requests": 30' in client.prompt
