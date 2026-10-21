import asyncio
import json
from pathlib import Path

from autonomous_studio.config import Settings
from autonomous_studio.domain.models import LongFormVisualPolicy, State
from autonomous_studio.hitl.adapters import TelegramAdapter, TelegramConfig
from autonomous_studio.hitl.worker import TelegramWorker
from autonomous_studio.providers.longform import (
    MockLongFormDirector,
    MockLongFormResearch,
    OpenAILongFormDirector,
    OpenAILongFormResearch,
)
from autonomous_studio.providers.longform_visuals import OpenAILongFormVisualPlanner
from autonomous_studio.providers.openai import OpenAIClient, canonical_url
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "longform-pilot.yaml"


def completed(value, urls=()):
    return {
        "status": "completed",
        "output": [
            {"type": "web_search_call", "action": {"sources": [{"url": u} for u in urls]}},
            {"type": "message", "content": [{"type": "output_text", "text": value}]},
        ],
    }


class FakeHTTP:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def json(self, url, payload, headers=None, **kwargs):
        self.calls.append((url, payload))
        return self.replies.pop(0)


class FakeTelegram(TelegramAdapter):
    def __init__(self, repo):
        super().__init__(
            TelegramConfig(bot_token="123:" + "a" * 30, chat_id=123, allowed_user_ids=[42]),
            repo,
        )
        self.sent = []

    async def call(self, method, payload):
        self.sent.append((method, payload))
        return {"message_id": len(self.sent)}


class OfflineStudio(Studio):
    def configure_live(self, project):
        """Keep Telegram contract tests deterministic and offline."""
        from types import SimpleNamespace
        from autonomous_studio.providers.longform_visuals import MockAuthenticMediaDiscovery

        self.live_client = SimpleNamespace(settings=self.settings, trace=[])
        self.authentic_media_discovery = MockAuthenticMediaDiscovery()
        project.longform_creative_policy = None


class FixtureLongFormResearch(MockLongFormResearch):
    async def research(self, brief, plan):
        return load_brief(EXAMPLE).research.model_copy(deep=True)


def command(update_id, text):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": 123}, "from": {"id": 42}, "text": text},
    }


def callback(project, update_id):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": 42},
            "message": {"chat": {"id": 123}},
            "data": f"approve:{project.pending().id}",
        },
    }


def test_telegram_longform_defaults_captions_off_and_acknowledges_all_gates(tmp_path):
    studio = OfflineStudio(tmp_path, settings=Settings(api_key="test", closed_captions=True))
    studio.longform_research = FixtureLongFormResearch()
    adapter = FakeTelegram(studio.repo)
    worker = TelegramWorker(studio, adapter, lambda: OfflineStudio(tmp_path))

    asyncio.run(worker.process(command(1, "/longform Why the Moon exists")))
    created = studio.repo.all_projects()[0]
    assert created.spec.format == "youtube_longform"
    assert created.spec.closed_captions is False
    assert created.creation_settings["closed_captions_source"] == "longform_default"
    assert created.longform_visual_policy.maximum_generated_requests == 30
    assert created.creation_settings["longform_image_budget"] == 30
    assert created.creation_settings["longform_image_budget_source"] == "longform_default"
    assert created.auto_assets is None
    assert "treatment and chapter outline" in adapter.sent[-1][1]["text"]

    treatment = asyncio.run(studio.run(created.id))
    asyncio.run(worker.process(callback(treatment, 2)))
    messages = [payload["text"] for method, payload in adapter.sent if method == "sendMessage"]
    answers = [
        payload["text"] for method, payload in adapter.sent if method == "answerCallbackQuery"
    ]
    assert answers[-1] == "Outline approved"
    assert "Writing and validating" in messages[-1]

    scripted = asyncio.run(studio.run(created.id))
    assert scripted.state == State.SCRIPT_APPROVAL
    asyncio.run(worker.process(callback(scripted, 3)))
    messages = [payload["text"] for method, payload in adapter.sent if method == "sendMessage"]
    answers = [
        payload["text"] for method, payload in adapter.sent if method == "answerCallbackQuery"
    ]
    assert answers[-1] == "Script approved"
    assert "narrative package is ready" in messages[-1]
    assert "Preparing visuals" not in messages[-1]

    visual = asyncio.run(studio.run(created.id))
    assert visual.state == State.VISUAL_PLAN_APPROVAL
    asyncio.run(worker.process(callback(visual, 4)))
    messages = [payload["text"] for method, payload in adapter.sent if method == "sendMessage"]
    answers = [
        payload["text"] for method, payload in adapter.sent if method == "answerCallbackQuery"
    ]
    assert answers[-1] == "Visual plan approved"
    assert "Preparing narration and media" in messages[-1]


def test_telegram_longform_snapshots_explicit_caption_preference(tmp_path):
    studio = OfflineStudio(tmp_path, settings=Settings(api_key="test"))
    adapter = FakeTelegram(studio.repo)
    worker = TelegramWorker(studio, adapter, lambda: studio)

    asyncio.run(worker.process(command(1, "/closedcaption yes")))
    asyncio.run(worker.process(command(2, "/longform A captioned documentary")))
    project = studio.repo.all_projects()[0]
    assert project.spec.closed_captions is True
    assert project.creation_settings["closed_captions_source"] == "telegram_saved"


def test_telegram_longform_image_budget_persists_for_future_projects(tmp_path):
    studio = OfflineStudio(tmp_path, settings=Settings(api_key="test"))
    adapter = FakeTelegram(studio.repo)
    worker = TelegramWorker(studio, adapter, lambda: studio)

    asyncio.run(worker.process(command(1, "/longformimagebudget 18")))
    asyncio.run(worker.process(command(2, "/longform An image-rich documentary")))
    first = studio.repo.all_projects()[0]
    assert first.longform_visual_policy.maximum_generated_requests == 18
    assert first.creation_settings["longform_image_budget"] == 18
    assert first.creation_settings["longform_image_budget_source"] == "telegram_saved"

    restarted = TelegramWorker(studio, adapter, lambda: studio)
    assert restarted.project_longform_image_budget() == 18
    asyncio.run(restarted.process(command(3, "/longformimagebudget reset")))
    asyncio.run(restarted.process(command(4, "/longform Back to the default budget")))
    second = studio.repo.all_projects()[1]
    assert second.longform_visual_policy.maximum_generated_requests == 30
    assert second.creation_settings["longform_image_budget_source"] == "longform_default"
    asyncio.run(restarted.process(command(5, "/longformimagebudget 30")))
    asyncio.run(restarted.process(command(6, "/longform A third documentary")))
    third = studio.repo.all_projects()[2]
    assert third.longform_visual_policy.maximum_generated_requests == 30
    assert third.creation_settings["longform_image_budget_source"] == "telegram_saved"


def _treatment_payload(spec):
    chapter_fields = (
        "id",
        "order",
        "title",
        "purpose",
        "question",
        "reveal",
        "transition",
        "target_duration",
        "evidence_claim_ids",
        "evidence_source_ids",
        "audio_direction",
    )
    return {
        "candidate_hooks": spec.candidate_hooks,
        "chosen_hook": spec.narrative.hook,
        "treatment_summary": spec.treatment_summary,
        "narrative": spec.narrative.model_dump(mode="json"),
        "chapters": [
            {field: getattr(chapter, field) for field in chapter_fields}
            for chapter in spec.chapters
        ],
        "engagement_ending": spec.engagement_ending.model_dump(mode="json"),
    }


def test_openai_longform_providers_use_background_structured_contracts(tmp_path):
    brief = load_brief(EXAMPLE)
    mock_research = MockLongFormResearch()
    mock_director = MockLongFormDirector()
    plan = asyncio.run(mock_research.plan(brief))
    treatment = asyncio.run(mock_director.develop_treatment(brief, plan, []))
    scripted, _ = asyncio.run(mock_director.write_script(treatment, []))
    urls = [source.url for source in brief.research.sources]
    catalog_ids = {
        canonical_url(url): f"s{index}"
        for index, url in enumerate(sorted(canonical_url(url) for url in urls), start=1)
    }
    original_sources = {source.id: source for source in brief.research.sources}
    conversion_draft = brief.research.model_dump(mode="json")
    conversion_draft.pop("sources")
    for claim in conversion_draft["claims"]:
        claim["source_ids"] = [
            catalog_ids[canonical_url(original_sources[source_id].url)]
            for source_id in claim["source_ids"]
        ]
    conversion_draft["source_notes"] = [
        {
            "source_id": catalog_ids[canonical_url(source.url)],
            "evidence_notes": source.evidence_notes,
        }
        for source in brief.research.sources
    ]

    research_http = FakeHTTP(
        [
            completed(plan.model_dump_json()),
            completed("Evidence gathered from authoritative sources.", urls),
            completed(json.dumps(conversion_draft)),
        ]
    )
    research_client = OpenAIClient(Settings(api_key="test"), tmp_path / "research", research_http)
    provider = OpenAILongFormResearch(research_client)
    actual_plan = asyncio.run(provider.plan(brief))
    actual_research = asyncio.run(provider.research(brief, actual_plan))
    assert actual_plan == plan
    assert {claim.text for claim in actual_research.claims} == {
        claim.text for claim in brief.research.claims
    }
    assert {source.url for source in actual_research.sources} == {
        canonical_url(source.url) for source in brief.research.sources
    }
    assert all(payload["model"] == "gpt-6-luna" for _, payload in research_http.calls)
    assert all(payload["background"] is True for _, payload in research_http.calls)
    assert research_http.calls[1][1]["tools"] == [{"type": "web_search"}]
    assert "json_schema" == research_http.calls[0][1]["text"]["format"]["type"]

    chapter_replies = []
    for chapter in scripted.chapters:
        chapter_replies.append(
            completed(
                json.dumps(
                    {
                        "chapter_id": chapter.id,
                        "narration_blocks": [
                            {
                                "id": block.id,
                                "text": block.text,
                                "claim_ids": block.claim_ids,
                            }
                            for block in chapter.narration_blocks
                        ],
                    }
                )
            )
        )
    director_http = FakeHTTP(
        [
            completed(json.dumps(_treatment_payload(treatment))),
            *chapter_replies,
            completed(json.dumps({"passed": True, "issues": []})),
        ]
    )
    director_client = OpenAIClient(Settings(api_key="test"), tmp_path / "director", director_http)
    director = OpenAILongFormDirector(director_client)
    actual_treatment = asyncio.run(director.develop_treatment(brief, plan, []))
    actual_script, report = asyncio.run(director.write_script(actual_treatment, []))
    assert report.passed
    assert all(chapter.narration_blocks for chapter in actual_script.chapters)
    assert all(
        payload["model"] == "gpt-6.1-sol" for _, payload in director_http.calls[:-1]
    )
    assert director_http.calls[-1][1]["model"] == "gpt-6-luna"
    assert all(payload["background"] is True for _, payload in director_http.calls)
    assert all(
        payload["text"]["format"]["type"] == "json_schema" for _, payload in director_http.calls
    )
    chapter_prompts = [payload["input"] for _, payload in director_http.calls[1:-1]]
    audit_prompt = director_http.calls[-1][1]["input"]
    for rule in (
        "progression",
        "handoff",
        "grounding",
        "distinctions",
        "promise",
        "economy",
        "ending",
    ):
        marker = f'"code": "{rule}"'
        assert all(marker in prompt for prompt in [*chapter_prompts, audit_prompt])
    assert scripted.chapters[0].narration_blocks[0].text in chapter_prompts[-1]
    assert scripted.chapters[-1].purpose in chapter_prompts[0]
    assert treatment.engagement_ending.comment_question in chapter_prompts[-1]
    assert treatment.engagement_ending.comment_question in audit_prompt
    assert '"adjacent_chapter_boundaries"' in audit_prompt
    assert scripted.chapters[0].narration_blocks[-1].text in audit_prompt
    assert all(
        "at most one backward-looking bridge sentence" in prompt for prompt in chapter_prompts
    )
    assert "unsupported bridge between distinct processes" in audit_prompt


def _chapter_visual_replies(scripted, policy):
    """Exercise one chapter response with complete speech-first unit coverage."""
    from autonomous_studio.workflow.spoken_units import chapter_units

    chunks = []
    for chapter in scripted.chapters:
        beats, assets = [], []
        for index, unit in enumerate(chapter_units(chapter)):
            anchor = " ".join(unit["text"].split()[:5]).rstrip(".,;:!?")
            short_label = anchor[:34].rsplit(" ", 1)[0] if len(anchor) > 34 else anchor
            first = chapter.order == 1 and index == 0
            beat_id = f"{chapter.id}_beat_{index}"
            asset_id = f"{chapter.id}_asset_{index}"
            beats.append(
                {
                    "id": beat_id,
                    "chapter_id": chapter.id,
                    "narration_block_ids": [unit["block_id"]],
                    "spoken_unit_ids": [unit["id"]],
                    "role_unit_groups": [] if first else [[unit["id"]]],
                    "visual_type": "authentic_image" if first else "kinetic_title",
                    "treatment": "hold" if first else "progressive_highlight",
                    "design_family": None if first else "editorial_frame",
                    "design_treatment": None if first else "statement_reveal",
                    "design_rationale": (
                        "The image establishes the narrated subject directly."
                        if first
                        else "Editorial framing introduces the spoken idea without inventing "
                        "data or a comparison."
                    ),
                    "purpose": "Show the idea being spoken in this passage.",
                    "screen_title": "Following the spoken explanation",
                    "overlay_text": [] if first else [short_label],
                    "narration_anchor": anchor,
                    "overlay_anchors": [] if first else [anchor],
                    "claim_ids": [],
                    "source_ids": [],
                    "asset_id": asset_id,
                }
            )
            assets.append(
                {
                    "kind": "authentic_image" if first else "code_rendered",
                    "id": asset_id,
                    "beat_ids": [beat_id],
                    **(
                        {
                            "search_query": "historical subject portrait",
                            "fallback_prompt": (
                                "A contemporary editorial illustration of the narrated subject"
                            ),
                        }
                        if first
                        else {"renderer": "title"}
                    ),
                }
            )
        chunks.append(
            {
                "style_direction": "A restrained, clear documentary visual style.",
                "rationale": "Every planned shot follows its corresponding spoken beat.",
                "beats": beats,
                "assets": assets,
            }
        )
    return chunks


def test_openai_longform_visual_planner_uses_background_structured_contract(tmp_path):
    brief = load_brief(EXAMPLE)
    research = MockLongFormResearch()
    director = MockLongFormDirector()
    plan = asyncio.run(research.plan(brief))
    treatment = asyncio.run(director.develop_treatment(brief, plan, []))
    scripted, _ = asyncio.run(director.write_script(treatment, []))
    policy = LongFormVisualPolicy()
    chunks = _chapter_visual_replies(scripted, policy)
    http = FakeHTTP([completed(json.dumps(chunk)) for chunk in chunks])
    client = OpenAIClient(Settings(api_key="test"), tmp_path / "visual", http)

    actual = asyncio.run(OpenAILongFormVisualPlanner(client).plan(scripted, policy, []))

    assert len(actual.beats) == sum(len(chunk["beats"]) for chunk in chunks)
    assert all(beat.claim_ids for beat in actual.beats)
    assert all("source_ids" not in beat.model_dump() for beat in actual.beats)
    assert len(http.calls) == len(scripted.chapters)
    payload = http.calls[0][1]
    assert payload["model"] == "gpt-6-luna"
    assert payload["background"] is True
    assert payload["text"]["format"]["type"] == "json_schema"
    assert "web_search" not in json.dumps(payload)
    assert "approved_sources" not in payload["input"]
    assert "approved_claims" not in payload["input"]


def test_openai_longform_visual_planner_repairs_only_invalid_spoken_beats(tmp_path):
    brief = load_brief(EXAMPLE)
    plan = asyncio.run(MockLongFormResearch().plan(brief))
    treatment = asyncio.run(MockLongFormDirector().develop_treatment(brief, plan, []))
    scripted, _ = asyncio.run(MockLongFormDirector().write_script(treatment, []))
    policy = LongFormVisualPolicy()
    chunks = _chapter_visual_replies(scripted, policy)
    valid_anchor = chunks[0]["beats"][1]["narration_anchor"]
    chunks[0]["beats"][1]["narration_anchor"] = "not in this narration block"

    def patch(anchor):
        return completed(
            json.dumps(
                {
                    "narration_anchor": anchor,
                    "overlay_text": [anchor],
                    "overlay_anchors": [anchor],
                }
            )
        )

    http = FakeHTTP(
        [
            *[completed(json.dumps(chunk)) for chunk in chunks],
            patch(valid_anchor),
        ]
    )
    client = OpenAIClient(Settings(api_key="test"), tmp_path / "visual-repair", http)
    repaired = asyncio.run(OpenAILongFormVisualPlanner(client).plan(scripted, policy, []))

    assert repaired.beats[1].narration_anchor == valid_anchor
    assert len(http.calls) == len(chunks) + 1
    assert all(call[1]["model"] == "gpt-6-luna" for call in http.calls[:-1])
    assert http.calls[-1][1]["model"] == "gpt-6-luna"
    assert http.calls[-1][1]["text"]["format"]["type"] == "json_schema"
    resumed = asyncio.run(OpenAILongFormVisualPlanner(client).plan(scripted, policy, []))
    assert resumed == repaired
    assert len(http.calls) == len(chunks) + 1
