import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from autonomous_studio.domain.longform_policy import LongFormCreativePolicy
from autonomous_studio.domain.models import (
    LongFormChapter,
    LongFormVisualPlan,
    LongFormVisualPolicy,
    NarrationBlock,
    State,
    VisualSceneData,
)
from autonomous_studio.hitl.adapters import review_message
from autonomous_studio.providers.http import RemoteError
from autonomous_studio.providers.longform import MockLongFormDirector, MockLongFormResearch
from autonomous_studio.providers.longform_visuals import (
    EVIDENCE_METRIC_PARTITION_RULE,
    AuthenticRequestDraft,
    ChapterVisualPlanDraft,
    MockLongFormVisualPlanner,
    OpenAILongFormVisualPlanner,
    WikimediaCommonsDiscovery,
    materialize_visual_plan,
    validate_beat_partition,
    validate_family_choices,
)
from autonomous_studio.rendering.visual_registry import REGISTRY
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief
from autonomous_studio.workflow.longform_visuals import (
    repair_unacquired_authentic_source,
    repair_unrendered_design_beat,
)
from autonomous_studio.workflow.spoken_units import chapter_units


@pytest.mark.parametrize(
    "subject,result",
    [
        ("a meteorite", "an estimated age of 4.567 billion years"),
        ("a tree core", "an estimated age of 240 years"),
    ],
)
def test_partition_contract_supports_evidence_then_later_single_metric(subject, result):
    chapter = LongFormChapter(
        id="chapter-2", order=2, title="Evidence and estimate", purpose="Explain a result",
        question="What does the sample show?", reveal="A physical clue supports an estimate.",
        target_duration=16,
        narration_blocks=[NarrationBlock(
            id="block-1", estimated_duration=16,
            text=(f"Scientists examined {subject}. Its visible layers preserved a clue. "
                  f"Only later did measurements yield {result}."),
        )],
    )
    units = chapter_units(chapter)
    from autonomous_studio.providers.longform_visuals import ChapterBeatPartition

    partition = ChapterBeatPartition.model_validate({"beats": [
        {"id": "evidence", "spoken_unit_ids": [units[0]["id"], units[1]["id"]],
         "role_unit_groups": [[units[0]["id"], units[1]["id"]]],
         "visual_intent": "Show the physical sample and the spoken feature."},
        {"id": "estimate", "spoken_unit_ids": [units[2]["id"]],
         "role_unit_groups": [[units[2]["id"]]],
         "visual_intent": "Reveal the one estimated numeric result."},
    ]})
    menu = validate_beat_partition(chapter, partition)
    assert menu["evidence"]["image_allowed"]
    assert any(
        item["family"] == "quantitative_display" and item["treatment"] == "metric_reveal"
        for item in menu["estimate"]["treatments"]
    )

    assert "concrete evidence or a physical subject" in EVIDENCE_METRIC_PARTITION_RULE
    assert "Do not make a metric screen sit" in EVIDENCE_METRIC_PARTITION_RULE
    assert "Do not split only to satisfy a timer" in EVIDENCE_METRIC_PARTITION_RULE

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "longform-pilot.yaml"


def run(studio, project):
    return asyncio.run(studio.run(project.id))


def visual_review_project(tmp_path, *, planner=None, discovery=None):
    studio = Studio(
        tmp_path,
        longform_visual_planner=planner,
        authentic_media_discovery=discovery,
    )
    project = run(studio, studio.create(load_brief(EXAMPLE)))
    studio.decide(project.id, "treatment", "approve", actor="test")
    project = run(studio, project)
    project = studio.decide(project.id, "script", "approve", actor="test")
    assert project.state == State.LONGFORM_NARRATIVE_READY
    return studio, run(studio, project)


def test_visual_plan_covers_narration_rights_cost_and_contact_sheets(tmp_path):
    studio, project = visual_review_project(tmp_path)

    assert project.state == State.VISUAL_PLAN_APPROVAL
    assert project.pending().gate == "visual_plan"
    plan = project.longform_visual_plan
    beats = [beat for chapter in project.spec.chapters for beat in chapter.visual_beats]
    assert 20 <= len(beats) <= 35
    assert {asset.kind for asset in plan.assets} == {
        "authentic_image",
        "generated_image",
        "code_rendered",
    }
    assert len(plan.assets) == len(beats)
    for beat in beats:
        assert beat.design_rationale
        if beat.visual_type in {"authentic_image", "generated_image"}:
            assert beat.design_family is None and beat.design_treatment is None
        else:
            assert beat.design_family in REGISTRY
            assert beat.design_treatment in {
                treatment.id for treatment in REGISTRY[beat.design_family].treatments
            }
    assert plan.rights.status == "review_required"
    assert plan.rights.attribution_lines
    assert plan.cost.paid_asset_generation_started is False
    assert (
        plan.cost.generated_request_count
        <= project.longform_visual_policy.maximum_generated_requests
    )
    assert plan.cost.unknown_cost_items
    assert project.image_requests == 0 and not project.generated_visuals

    for chapter in project.spec.chapters:
        assert sum(beat.duration for beat in chapter.visual_beats) == pytest.approx(
            chapter.target_duration
        )
        assert {block.id for block in chapter.narration_blocks} == {
            block_id for beat in chapter.visual_beats for block_id in beat.narration_block_ids
        }

    roles = {artifact.role for artifact in project.current_artifacts()}
    assert {
        "longform_visual_plan",
        "longform_rights_plan",
        "longform_cost_estimate",
        "longform_visual_evidence_map",
        "longform_visual_design_decisions",
        "longform_contact_sheet",
    } <= roles
    decisions = json.loads(
        studio.repo.verify(
            project, project.current_artifacts("longform_visual_design_decisions")[0]
        ).read_text(encoding="utf-8")
    )
    assert len(decisions["beats"]) == len(beats)
    assert all(item["design_rationale"] for item in decisions["beats"])
    sheets = project.current_artifacts("longform_contact_sheet")
    assert len(sheets) >= 4
    for sheet in sheets:
        with Image.open(studio.repo.verify(project, sheet)) as image:
            assert image.size == (1600, 1000)

    message = review_message(project, project.pending())
    assert "No paid image generation" in message.text
    assert "Unknown-cost items" in message.text
    assert message.media == sheets

    approved = studio.decide(project.id, "visual_plan", "approve", actor="test")
    assert approved.state == State.LONGFORM_VISUAL_PLAN_READY
    assert approved.pending() is None

    invalid = plan.model_dump(mode="json")
    invalid["assets"][0]["kind"] = "unsupported_media"
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        LongFormVisualPlan.model_validate(invalid)


def test_new_visual_plan_rejects_unregistered_family_and_image_family_mix(tmp_path):
    studio, project = visual_review_project(tmp_path)
    draft = asyncio.run(
        MockLongFormVisualPlanner().plan(project.spec, project.longform_visual_policy, [])
    )
    image = next(beat for beat in draft.beats if beat.visual_type == "generated_image")
    generated = next(asset for asset in draft.assets if asset.id == image.asset_id)
    generated.prompt = "An editorial illustration of a laboratory measurement with clear objects"
    code = next(beat for beat in draft.beats if beat.design_family)
    code.design_treatment = "invented_treatment"
    with pytest.raises(ValueError, match="registered family"):
        validate_family_choices(draft)
    code.design_treatment = (
        "working"
        if code.design_family == "process"
        else REGISTRY[code.design_family].treatments[0].id
    )
    image.design_family = "process"
    image.design_treatment = "working"
    with pytest.raises(ValueError, match="image-led"):
        validate_family_choices(draft)
    image.design_family = image.design_treatment = None
    rationale = image.design_rationale
    image.design_rationale = None
    with pytest.raises(ValueError, match="visual-choice rationale"):
        validate_family_choices(draft, require_rationale=True)
    image.design_rationale = rationale
    image.visual_type = "authentic_image"
    with pytest.raises(ValueError, match="image type disagrees"):
        validate_family_choices(draft)
    image.visual_type = "generated_image"
    generated.prompt = "Archival portrait of a historical scientist in a laboratory"
    with pytest.raises(ValueError, match="archival-looking generated image"):
        validate_family_choices(draft)


def test_written_source_alone_does_not_justify_document_focus(tmp_path):
    studio, project = visual_review_project(tmp_path)
    spec = project.spec.model_copy(deep=True)
    for chapter in spec.chapters:
        chapter.visual_beats = []
    draft = asyncio.run(MockLongFormVisualPlanner().plan(spec, project.longform_visual_policy, []))
    beat = next(
        beat
        for beat in draft.beats
        if beat.visual_type not in {"authentic_image", "generated_image"}
    )
    draft.beats = [beat]
    draft.assets = [asset for asset in draft.assets if asset.id == beat.asset_id]
    block = next(
        block
        for chapter in spec.chapters
        if chapter.id == beat.chapter_id
        for block in chapter.narration_blocks
        if block.id in beat.narration_block_ids
    )
    block.text = "The measurements revealed a surprising pattern in the mineral."
    beat.design_family = "document_focus"
    beat.design_treatment = "extracted_finding"
    with pytest.raises(ValueError, match="without a document in the spoken beat"):
        validate_family_choices(draft, spec)
    block.text = "Her notebook recorded a surprising pattern in the mineral."
    validate_family_choices(draft, spec)


def test_fixed_reveal_roles_reject_unspoken_expectation_before_visual_approval(tmp_path):
    studio, project = visual_review_project(tmp_path)
    spec = project.spec.model_copy(deep=True)
    chapter = spec.chapters[0]
    block = chapter.narration_blocks[0]
    block.text = (
        "Tests suggested an unfamiliar substance. Separation narrowed the candidates. "
        "Repeated measurements guided the search. The ore was too active for uranium alone."
    )
    units = [unit for unit in chapter_units(chapter) if unit["block_id"] == block.id]
    ids = [unit["id"] for unit in units]
    payload = {
        "id": "v1", "chapter_id": chapter.id, "narration_block_ids": [block.id],
        "spoken_unit_ids": ids, "role_unit_groups": [[ids[0]], [ids[1]], ids[2:]],
        "role_semantics": ["expected", "observed", "inference"],
        "visual_type": "comparison", "treatment": "progressive_highlight",
        "design_family": "evidence", "design_treatment": "competing",
        "design_rationale": (
            "The planner incorrectly treated the first observation as a prediction."
        ),
        "purpose": "Explain how measured activity changed the interpretation.",
        "overlay_text": ["An unfamiliar substance", "Separation", "Uranium was insufficient"],
        "overlay_anchors": [
            "Tests suggested an unfamiliar substance", "Separation narrowed the candidates",
            "The ore was too active for uranium alone",
        ],
        "asset_id": "a1",
    }
    asset = {"kind": "code_rendered", "id": "a1", "beat_ids": ["v1"],
             "renderer": "comparison"}
    draft = ChapterVisualPlanDraft(
        style_direction="A consistent scientific documentary style with restrained accents.",
        rationale="The spoken beat needs a design whose reveals actually follow the words.",
        beats=[payload], assets=[asset],
    )
    with pytest.raises(ValueError, match="no spoken expectation"):
        validate_family_choices(draft, spec, require_role_contract=True)
    draft.beats[0].design_family = "synthesis"
    draft.beats[0].design_treatment = "convergence"
    draft.beats[0].role_unit_groups = [[item] for item in ids]
    draft.beats[0].role_semantics = ["input", "input", "input", "takeaway"]
    draft.beats[0].overlay_text = [
        "An unfamiliar substance", "Narrow the candidates", "Measure the activity",
        "Uranium alone cannot explain it",
    ]
    draft.beats[0].overlay_anchors = [
        "Tests suggested an unfamiliar substance", "Separation narrowed the candidates",
        "Repeated measurements guided the search", "The ore was too active for uranium alone",
    ]
    validate_family_choices(draft, spec, require_role_contract=True)
    draft.beats[0].overlay_anchors[0] = "Repeated measurements guided the search"
    with pytest.raises(ValueError, match="outside its assigned spoken role"):
        validate_family_choices(draft, spec, require_role_contract=True)
    draft.beats[0].overlay_anchors[0] = "Tests suggested an unfamiliar substance"
    draft.beats[0].overlay_text[0] = "An unfamiliar substance that was measured repeatedly"
    with pytest.raises(ValueError, match="longer than 34 characters"):
        validate_family_choices(draft, spec, require_role_contract=True)


def test_numeric_visual_plan_binds_spoken_values_before_approval(tmp_path):
    _, project = visual_review_project(tmp_path)
    spec = project.spec.model_copy(deep=True)
    chapter = spec.chapters[0]
    block = chapter.narration_blocks[0]
    block.text = "The measured span was about 42 kilometers."
    unit = chapter_units(chapter)[0]["id"]
    beat = {
        "id": "metric_beat", "chapter_id": chapter.id,
        "narration_block_ids": [block.id], "spoken_unit_ids": [unit],
        "role_unit_groups": [[unit]], "visual_type": "data_chart",
        "treatment": "progressive_highlight",
        "design_family": "quantitative_display", "design_treatment": "metric_reveal",
        "purpose": "Show the measured distance as a single numeric visual.",
        "screen_title": "Measured span",
        "overlay_text": ["About 42 kilometers"],
        "overlay_anchors": ["about 42 kilometers"],
        "asset_id": "metric_asset",
    }
    draft = ChapterVisualPlanDraft(
        style_direction="A restrained explanatory visual with readable type and simple motion.",
        rationale="One spoken measurement needs one metric reveal.",
        beats=[beat],
        assets=[{"kind": "code_rendered", "id": "metric_asset", "beat_ids": ["metric_beat"],
                 "renderer": "data_chart"}],
    )
    with pytest.raises(ValueError, match="approved numeric values"):
        validate_family_choices(draft, spec, require_role_contract=True)
    draft.beats[0].visual_data = VisualSceneData(values=[42], unit="kilometers")
    validate_family_choices(draft, spec, require_role_contract=True)
    draft.beats[0].visual_data = VisualSceneData(values=[42], unit="x" * 21)
    with pytest.raises(ValueError, match="unit exceeds renderer width"):
        validate_family_choices(draft, spec, require_role_contract=True)
    draft.beats[0].visual_data = VisualSceneData(values=[43], unit="kilometers")
    with pytest.raises(ValueError, match="unspoken number"):
        validate_family_choices(draft, spec, require_role_contract=True)


@pytest.mark.parametrize("chapter_limit", [1, None])
def test_live_planner_uses_speech_units_in_each_chapter(chapter_limit):
    brief = load_brief(EXAMPLE)
    research = asyncio.run(MockLongFormResearch().plan(brief))
    treatment = asyncio.run(MockLongFormDirector().develop_treatment(brief, research, []))
    spec, _ = asyncio.run(MockLongFormDirector().write_script(treatment, []))
    if chapter_limit:
        spec.chapters = spec.chapters[:chapter_limit]
    policy = LongFormVisualPolicy(minimum_visual_beats=20, maximum_visual_beats=25)

    class Client:
        settings = SimpleNamespace(director_model="test-model")

        def __init__(self):
            self.calls = []

        async def response(self, _model, prompt, **_kwargs):
            payload = json.loads(prompt.split("\n", 1)[1])
            self.calls.append(payload["chapter"]["id"])
            assert "approved_sources" not in payload
            assert "approved_claims" not in payload
            assert "evidence_source_ids" not in payload["chapter"]
            assert "evidence_claim_ids" not in payload["chapter"]
            assert "claim_ids" not in payload["chapter"]["narration_blocks"][0]
            assert "requested_beat_count" not in payload
            chapter = payload["chapter"]
            beats, assets = [], []
            for index, unit in enumerate(payload["spoken_units"]):
                first = not payload["prior_ids"]["beats"] and index == 0
                beat_id = f"{chapter['id']}_beat_{index}"
                asset_id = f"{chapter['id']}_asset_{index}"
                beats.append(
                    {
                        "id": beat_id,
                        "chapter_id": chapter["id"],
                        "narration_block_ids": [unit["block_id"]],
                        "spoken_unit_ids": [unit["id"]],
                        "role_unit_groups": [] if first else [[unit["id"]]],
                        "visual_type": "authentic_image" if first else "kinetic_title",
                        "treatment": "hold",
                        "design_family": None if first else "editorial_frame",
                        "design_treatment": None if first else "statement_reveal",
                        "design_rationale": (
                            "The image directly establishes the narrated subject."
                            if first
                            else "Editorial framing reveals this spoken idea without implying "
                            "a measured comparison."
                        ),
                        "purpose": "Explain the next supported story point.",
                        "narration_anchor": " ".join(unit["text"].split()[:4]),
                        "asset_id": asset_id,
                        "claim_ids": [],
                        "source_ids": [],
                    }
                )
                assets.append(
                    {
                        "kind": "authentic_image" if first else "code_rendered",
                        "id": asset_id,
                        "beat_ids": [beat_id],
                        **(
                            {
                                "search_query": "Historical source image",
                                "fallback_prompt": (
                                    "An editorial image illustrating the historical subject clearly"
                                ),
                            }
                            if first
                            else {"renderer": "title"}
                        ),
                    }
                )
            result = {
                "style_direction": "A coherent restrained documentary visual style.",
                "rationale": "Each chapter explains approved narration with distinct beats.",
                "beats": beats,
                "assets": assets,
            }
            return {
                "status": "completed",
                "output": [{"content": [{"type": "output_text", "text": json.dumps(result)}]}],
            }

        def reject_last(self, _reason):
            pytest.fail("A valid chapter response was rejected")

    client = Client()
    planner = OpenAILongFormVisualPlanner(client)

    async def no_repairs(_spec, draft):
        return draft

    planner._repair_spoken_beats = no_repairs
    draft = asyncio.run(planner.plan(spec, policy, []))
    assert client.calls == [chapter.id for chapter in spec.chapters]
    assert len(draft.beats) == sum(len(chapter_units(chapter)) for chapter in spec.chapters)
    assert len({beat.id for beat in draft.beats}) == len(draft.beats)
    assert any(beat.claim_ids for beat in draft.beats)
    assert all("source_ids" not in beat.model_dump() for beat in draft.beats)


def test_visual_plan_revision_preserves_script_and_replans_only_visuals(tmp_path):
    studio, project = visual_review_project(tmp_path)
    script = [block.text for chapter in project.spec.chapters for block in chapter.narration_blocks]
    continuity = project.longform_continuity.model_dump(mode="json")
    studio.set_longform_image_budget(project.id, 18, actor="test")

    revised = studio.revise(
        project.id,
        "Use more authentic documents and fewer generated conceptual images.",
        approval_id=project.pending().id,
        actor="test",
    )
    assert revised.state == State.LONGFORM_VISUAL_PLANNING
    assert revised.version == 2
    assert revised.longform_visual_policy.maximum_generated_requests == 18
    assert revised.creation_settings["longform_image_budget_source"] == "project_revision"
    assert revised.longform_visual_plan is None
    assert revised.longform_visual_plan_attempts == 0
    assert all(not chapter.visual_beats for chapter in revised.spec.chapters)
    assert script == [
        block.text for chapter in revised.spec.chapters for block in chapter.narration_blocks
    ]
    assert revised.longform_continuity.model_dump(mode="json") == continuity

    replacement = run(Studio(tmp_path), revised)
    assert replacement.state == State.VISUAL_PLAN_APPROVAL
    assert replacement.version == 2
    assert any("more authentic" in item.message for item in replacement.feedback)


def test_failed_semantic_stage_can_request_versioned_visual_replan(tmp_path):
    studio, project = visual_review_project(tmp_path)
    studio.decide(project.id, "visual_plan", "approve", actor="test")
    saved = studio.repo.load(project.id)
    script = [block.text for chapter in saved.spec.chapters for block in chapter.narration_blocks]
    saved.state = State.LONGFORM_SEMANTIC_PLANNING
    saved.last_error = "document treatment did not match speech"
    saved.longform_image_requests = 2
    studio.repo.save(saved, "semantic_failure_fixture", {})

    revised = studio.revise(
        saved.id,
        "Choose visual treatments from spoken meaning, not research sources.",
        expected_version=saved.version,
        actor="test",
    )
    assert revised.version == saved.version + 1
    assert revised.state == State.LONGFORM_VISUAL_PLANNING
    assert revised.longform_visual_plan is None
    assert revised.longform_image_requests == 0
    assert revised.longform_semantic_decisions == {}
    assert revised.longform_media == {}
    assert script == [
        block.text for chapter in revised.spec.chapters for block in chapter.narration_blocks
    ]


def test_longform_generated_image_budget_defaults_to_30_and_rejects_31(tmp_path):
    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE), longform_visual_policy=LongFormVisualPolicy())
    assert project.longform_visual_policy.maximum_generated_requests == 30
    revised = studio.set_longform_image_budget(project.id, 30, actor="test")
    assert revised.longform_visual_policy.maximum_generated_requests == 30
    assert revised.creation_settings["longform_image_budget"] == 30
    with pytest.raises(ValueError, match="between 4 and 30"):
        studio.set_longform_image_budget(project.id, 31, actor="test")
    with pytest.raises(ValidationError):
        LongFormVisualPolicy(maximum_generated_requests=31)


def test_missing_authentic_candidate_uses_generated_fallback_within_budget(tmp_path):
    async def prepare():
        studio = Studio(tmp_path)
        brief = load_brief(EXAMPLE)
        project = studio.create(brief)
        project = await studio.run(project.id)
        studio.decide(project.id, "treatment", "approve", actor="test")
        project = await studio.run(project.id)
        return project.spec

    spec = asyncio.run(prepare())
    policy = LongFormVisualPolicy(maximum_generated_requests=10)
    draft = asyncio.run(MockLongFormVisualPlanner().plan(spec, policy, []))
    planned, plan = materialize_visual_plan(spec, draft, policy, authentic={})
    assert any(asset.kind == "generated_image" for asset in plan.assets)
    assert plan.rights.status == "documented"
    assert plan.cost.generated_request_count <= policy.maximum_generated_requests
    assert all(chapter.visual_beats for chapter in planned.chapters)


def test_archive_lookup_instruction_fails_instead_of_becoming_document(tmp_path):
    studio, project = visual_review_project(tmp_path)
    spec = project.spec.model_copy(deep=True)
    for chapter in spec.chapters:
        chapter.visual_beats = []
    policy = LongFormVisualPolicy(maximum_generated_requests=10)
    draft = asyncio.run(MockLongFormVisualPlanner().plan(spec, policy, []))
    request = next(asset for asset in draft.assets if asset.kind == "authentic_image")
    request.fallback_prompt = (
        "Search institutional archives for an authenticated photograph of the subject."
    )
    with pytest.raises(ValueError, match="source lookup or fabricated evidence"):
        materialize_visual_plan(spec, draft, policy, authentic={})


def test_authentic_fallback_rejects_simulated_historical_image_before_approval(tmp_path):
    studio, project = visual_review_project(tmp_path)
    spec = project.spec.model_copy(deep=True)
    for chapter in spec.chapters:
        chapter.visual_beats = []
    policy = LongFormVisualPolicy(maximum_generated_requests=10)
    draft = asyncio.run(MockLongFormVisualPlanner().plan(spec, policy, []))
    request = next(asset for asset in draft.assets if asset.kind == "authentic_image")
    request.fallback_prompt = (
        "A generated archival-looking manuscript page with convincing historical "
        "handwriting and period marks."
    )
    with pytest.raises(ValueError, match="simulated historical image"):
        materialize_visual_plan(spec, draft, policy, authentic={})


def test_commons_search_uses_subject_first_and_ignores_search_boilerplate():
    queries = WikimediaCommonsDiscovery._queries(
        "public domain authentic photograph Marie Curie laboratory Wikimedia Commons"
    )
    assert queries[0].startswith("Marie Curie")
    assert "public domain" not in queries[0].lower()
    assert WikimediaCommonsDiscovery._relevant(
        {"title": "File:Marie Curie in her laboratory.jpg"},
        {},
        "public domain authentic photograph Marie Curie laboratory Wikimedia Commons",
    )
    assert not WikimediaCommonsDiscovery._relevant(
        {"title": "File:Marie Curie in her laboratory.jpg"},
        {},
        "Musée Curie laboratory notebook record 1898",
    )
    assert not WikimediaCommonsDiscovery._relevant(
        {"title": "File:Marie Curie 1900 portrait.jpg"},
        {},
        "Marie Curie portrait circa 1911",
    )


def test_authentic_discovery_throttling_uses_visible_generated_fallback(tmp_path):
    class ThrottledDiscovery:
        async def discover(self, request, spec):
            raise RemoteError("Wikimedia Commons", 429)

    studio, project = visual_review_project(tmp_path, discovery=ThrottledDiscovery())
    assert project.state == State.VISUAL_PLAN_APPROVAL
    assert project.last_error is None
    assert all(asset.kind != "authentic_image" for asset in project.longform_visual_plan.assets)
    assert (
        project.longform_visual_plan.cost.generated_request_count
        <= project.longform_visual_policy.maximum_generated_requests
    )
    assert "HTTP 429" in project.longform_visual_plan.rationale
    reports = project.current_artifacts("longform_authentic_discovery_report")
    assert len(reports) == 1


def test_generated_fallback_cannot_exceed_budget(tmp_path):
    studio, project = visual_review_project(tmp_path)
    spec = project.spec.model_copy(deep=True)
    for chapter in spec.chapters:
        chapter.visual_beats = []
    policy = LongFormVisualPolicy(maximum_generated_requests=0)
    draft = asyncio.run(
        MockLongFormVisualPlanner().plan(
            spec,
            LongFormVisualPolicy(maximum_generated_requests=10),
            [],
        )
    )
    with pytest.raises(ValueError, match="generated-image request budget"):
        materialize_visual_plan(spec, draft, policy, authentic={})


def test_visual_planning_resumes_after_transient_interruption(tmp_path):
    class InterruptedOnce:
        def __init__(self):
            self.calls = 0
            self.delegate = MockLongFormVisualPlanner()

        async def plan(self, spec, policy, feedback):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary provider interruption")
            return await self.delegate.plan(spec, policy, feedback)

    planner = InterruptedOnce()
    studio = Studio(tmp_path, longform_visual_planner=planner)
    project = run(studio, studio.create(load_brief(EXAMPLE)))
    studio.decide(project.id, "treatment", "approve", actor="test")
    project = run(studio, project)
    project = studio.decide(project.id, "script", "approve", actor="test")

    with pytest.raises(RuntimeError, match="temporary provider interruption"):
        run(studio, project)
    interrupted = studio.repo.load(project.id)
    assert interrupted.state == State.LONGFORM_VISUAL_PLANNING
    assert interrupted.longform_visual_plan_attempts == 1
    assert interrupted.last_error == "temporary provider interruption"

    restarted = Studio(tmp_path, longform_visual_planner=planner)
    restarted.request_retry(project.id, actor="test")
    recovered = run(restarted, interrupted)
    assert recovered.state == State.VISUAL_PLAN_APPROVAL
    assert recovered.longform_visual_plan_attempts == 2
    assert planner.calls == 2


def test_provider_http_500_does_not_exhaust_creative_visual_attempt(tmp_path):
    class ServerErrorOnce:
        def __init__(self):
            self.calls = 0
            self.delegate = MockLongFormVisualPlanner()

        async def plan(self, spec, policy, feedback):
            self.calls += 1
            if self.calls == 1:
                raise RemoteError("OpenAI", 500)
            return await self.delegate.plan(spec, policy, feedback)

    planner = ServerErrorOnce()
    studio = Studio(tmp_path, longform_visual_planner=planner)
    project = run(studio, studio.create(load_brief(EXAMPLE)))
    studio.decide(project.id, "treatment", "approve", actor="test")
    project = run(studio, project)
    project = studio.decide(project.id, "script", "approve", actor="test")
    project.longform_creative_policy = LongFormCreativePolicy()
    studio.repo.save(project, "creative_policy_fixture", {})

    with pytest.raises(RemoteError, match="HTTP 500"):
        run(studio, project)
    failed = studio.repo.load(project.id)
    assert failed.state == State.LONGFORM_VISUAL_PLANNING
    assert failed.longform_visual_plan_attempts == 0
    assert failed.longform_visual_plan is None
    assert failed.last_error == "OpenAI request failed (HTTP 500)"
    assert any(
        event["kind"] == "longform_visual_plan_service_interrupted"
        for event in studio.repo.events(project.id)
    )

    # An older run may already have persisted the spent attempt before this fix.
    failed.longform_visual_plan_attempts = 1
    studio.repo.save(failed, "legacy_transport_attempt_fixture", {})
    recovered = Studio(tmp_path, longform_visual_planner=planner)
    retried = recovered.request_retry(project.id, actor="test")
    assert retried.longform_visual_plan_attempts == 0
    assert recovered.repo.events(project.id)[-1]["kind"] == (
        "visual_plan_transport_recovery_requested"
    )
    finished = run(recovered, retried)
    assert finished.state == State.VISUAL_PLAN_APPROVAL
    assert finished.pending().gate == "visual_plan"
    assert finished.longform_visual_plan_attempts == 1
    assert planner.calls == 2


def test_exhausted_visual_validation_requires_revision(tmp_path):
    class InvalidPlanner:
        async def plan(self, spec, policy, feedback):
            raise ValueError("invalid visual plan fixture")

    studio = Studio(tmp_path, longform_visual_planner=InvalidPlanner())
    project = run(studio, studio.create(load_brief(EXAMPLE)))
    studio.decide(project.id, "treatment", "approve", actor="test")
    project = run(studio, project)
    project = studio.decide(project.id, "script", "approve", actor="test")

    with pytest.raises(ValueError, match="failed after two attempts"):
        run(studio, project)
    failed = studio.repo.load(project.id)
    assert failed.longform_visual_plan_attempts == 2
    assert failed.last_error_code == "longform_visual_planning"
    with pytest.raises(ValueError, match="use revise"):
        studio.request_retry(project.id, actor="test")

    revised = studio.revise(
        project.id,
        "Use a simpler evidence-led visual plan.",
        expected_version=failed.version,
        actor="test",
    )
    assert revised.state == State.LONGFORM_VISUAL_PLANNING
    assert revised.longform_visual_plan_attempts == 0


def test_legacy_fallback_budget_failure_can_recover_under_new_policy(tmp_path):
    class LegacyBudgetFailure:
        async def plan(self, spec, policy, feedback):
            raise ValueError("visual plan exceeds the generated-image request budget")

    studio = Studio(tmp_path, longform_visual_planner=LegacyBudgetFailure())
    project = run(studio, studio.create(load_brief(EXAMPLE)))
    studio.decide(project.id, "treatment", "approve", actor="test")
    project = run(studio, project)
    project = studio.decide(project.id, "script", "approve", actor="test")
    with pytest.raises(ValueError, match="failed after two attempts"):
        run(studio, project)

    recovered = studio.request_retry(project.id, actor="test")
    assert recovered.state == State.LONGFORM_VISUAL_PLANNING
    assert recovered.longform_visual_plan_attempts == 0
    assert recovered.last_error is None
    assert studio.repo.events(project.id)[-1]["kind"] == ("visual_plan_policy_recovery_requested")


def test_spoken_beat_validation_failure_can_retry_after_planner_fix(tmp_path):
    class InvalidAnchorPlanner:
        async def plan(self, spec, policy, feedback):
            raise ValueError(
                "Long-form visual plan failed validation after bounded internal repairs: "
                "vb02 uses an anchor absent from approved narration"
            )

    studio = Studio(tmp_path, longform_visual_planner=InvalidAnchorPlanner())
    project = run(studio, studio.create(load_brief(EXAMPLE)))
    studio.decide(project.id, "treatment", "approve", actor="test")
    project = run(studio, project)
    project = studio.decide(project.id, "script", "approve", actor="test")
    with pytest.raises(ValueError, match="failed after two attempts"):
        run(studio, project)

    recovered = studio.request_retry(project.id, actor="test")
    assert recovered.version == project.version
    assert recovered.state == State.LONGFORM_VISUAL_PLANNING
    assert recovered.longform_visual_plan_attempts == 0
    assert recovered.last_error is None
    assert studio.repo.events(project.id)[-1]["kind"] == "visual_plan_beat_recovery_requested"


def test_visual_recovery_uses_cached_complete_draft_without_full_regeneration(tmp_path):
    class InvalidAnchorPlanner:
        async def plan(self, spec, policy, feedback):
            raise ValueError(
                "Long-form visual plan failed validation after bounded internal repairs: "
                "vb02 uses an anchor absent from approved narration"
            )

    class CachedRepairPlanner:
        def __init__(self, valid):
            self.valid = valid
            self.repaired = False

        async def _repair_spoken_beats(self, spec, draft):
            self.repaired = True
            assert draft.beats[1].narration_anchor == "unspoken phrase"
            return self.valid

        async def plan(self, spec, policy, feedback):
            raise AssertionError("a complete cached draft must not be regenerated")

    studio = Studio(tmp_path, longform_visual_planner=InvalidAnchorPlanner())
    project = run(studio, studio.create(load_brief(EXAMPLE)))
    studio.decide(project.id, "treatment", "approve", actor="test")
    project = run(studio, project)
    project = studio.decide(project.id, "script", "approve", actor="test")
    with pytest.raises(ValueError, match="failed after two attempts"):
        run(studio, project)

    recovered = studio.request_retry(project.id, actor="test")
    valid = asyncio.run(
        MockLongFormVisualPlanner().plan(recovered.spec, recovered.longform_visual_policy, [])
    )
    invalid = valid.model_copy(deep=True)
    invalid.beats[1].narration_anchor = "unspoken phrase"
    output = studio.repo.attempt_dir(recovered, "cached-visual-draft-test")
    studio._json(
        recovered,
        output,
        "longform-visual-plan-attempt-2-provider-responses.json",
        [
            {
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": invalid.model_dump_json()}],
                        }
                    ],
                }
            }
        ],
        "provider_usage",
    )
    studio.repo.save(recovered, "cached_visual_draft_test_added", {})
    planner = CachedRepairPlanner(valid)
    studio.longform_visual_planner = planner

    reviewed = run(studio, recovered)
    assert planner.repaired
    assert reviewed.state == State.VISUAL_PLAN_APPROVAL
    assert reviewed.longform_visual_plan is not None


def test_wikimedia_discovery_records_exact_candidate_rights_metadata():
    class HTTP:
        async def get_json(self, url, headers=None, **kwargs):
            assert "extmetadata" in url and "iiurlwidth=640" in url
            return {
                "query": {
                    "pages": [
                        {
                            "pageid": 42,
                            "title": "File:Documentary evidence.jpg",
                            "imageinfo": [
                                {
                                    "mime": "image/jpeg",
                                    "width": 1200,
                                    "height": 800,
                                    "thumburl": "https://upload.wikimedia.org/preview.jpg",
                                    "descriptionurl": (
                                        "https://commons.wikimedia.org/wiki/"
                                        "File:Documentary_evidence.jpg"
                                    ),
                                    "extmetadata": {
                                        "LicenseShortName": {"value": "CC BY 4.0"},
                                        "LicenseUrl": {
                                            "value": "https://creativecommons.org/licenses/by/4.0/"
                                        },
                                        "Artist": {"value": "Example Creator"},
                                        "ObjectName": {"value": "Documentary evidence"},
                                    },
                                }
                            ],
                        }
                    ]
                }
            }

    request = AuthenticRequestDraft(
        id="auth_1",
        beat_ids=["beat_1"],
        search_query="documentary evidence",
        fallback_prompt="A restrained editorial illustration of documentary evidence.",
    )
    candidate = asyncio.run(WikimediaCommonsDiscovery(HTTP()).discover(request, None))
    assert candidate.creator == "Example Creator"
    assert candidate.license_name == "CC BY 4.0"
    assert "creativecommons.org" in candidate.attribution
    assert candidate.context_review_required is True
    assert candidate.modifications_allowed is True


def test_wikimedia_discovery_rejects_tiny_or_semantically_wrong_portrait():
    metadata = {"LicenseShortName": {"value": "Public domain"}}
    def page(title, width, height):
        return {
            "title": f"File:{title}",
            "imageinfo": [{
                "mime": "image/png", "width": width, "height": height,
                "thumburl": "https://upload.wikimedia.org/preview.png",
                "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{title}",
                "extmetadata": metadata,
            }],
        }

    discovery = WikimediaCommonsDiscovery(None)
    tiny_signature = page("Marie_Curie_Signature.png", 273, 83)
    assert discovery._candidate(tiny_signature) is None
    assert not discovery._relevant(
        page("Marie_Curie_Signature.png", 1200, 800), metadata,
        "Marie Curie portrait circa 1911 Nobel Prize official archive photograph",
    )
    assert discovery._candidate(page("Marie_Curie_Portrait.png", 1200, 800)) is not None


def test_repair_unacquired_authentic_source_preserves_approved_beats_and_audits_change(
    tmp_path, monkeypatch
):
    studio, project = visual_review_project(tmp_path)
    project = studio.decide(project.id, "visual_plan", "approve", actor="test")
    project.state = State.LONGFORM_ASSETS_PREPARING
    prior = next(
        asset for asset in project.longform_visual_plan.assets if asset.kind == "authentic_image"
    )
    replacement = prior.model_copy(
        update={
            "title": prior.search_query,
            "source_page": "https://commons.wikimedia.org/wiki/File:Marie_Curie_laboratory_portrait.jpg",
            "attribution": "Marie Curie laboratory portrait — Public domain",
        }
    )

    async def discover_exact(self, request, source_page):
        assert request.id == prior.id
        assert source_page == replacement.source_page
        return replacement

    monkeypatch.setattr(WikimediaCommonsDiscovery, "discover_exact", discover_exact)
    studio.live_client = SimpleNamespace(http=object())
    beat_ids = [beat.id for chapter in project.spec.chapters for beat in chapter.visual_beats]
    revised = asyncio.run(
        repair_unacquired_authentic_source(
            studio, project, prior.id, replacement.source_page, actor="test"
        )
    )
    assert [
        beat.id for chapter in revised.spec.chapters for beat in chapter.visual_beats
    ] == beat_ids
    assert revised.longform_visual_plan.assets[
        next(i for i, a in enumerate(revised.longform_visual_plan.assets) if a.id == prior.id)
    ].source_page == replacement.source_page
    assert replacement.attribution in revised.longform_visual_plan.rights.attribution_lines
    assert prior.attribution not in revised.longform_visual_plan.rights.attribution_lines
    assert studio.repo.events(project.id)[-1]["kind"] == "longform_authentic_source_repaired"
    with pytest.raises(ValueError, match="changed authentic candidate"):
        asyncio.run(
            repair_unacquired_authentic_source(
                studio, revised, prior.id, replacement.source_page, actor="test"
            )
        )


def test_repair_unrendered_design_beat_preserves_script_and_other_chapters(tmp_path):
    from autonomous_studio.providers.longform import longform_script
    from autonomous_studio.providers.longform_media import _editorial_card

    studio, project = visual_review_project(tmp_path)
    project = studio.decide(project.id, "visual_plan", "approve", actor="test")
    beat = next(
        beat for chapter in project.spec.chapters for beat in chapter.visual_beats
        if beat.design_family and beat.visual_type != "outro"
    )
    asset = next(
        asset for asset in project.longform_visual_plan.assets if asset.id == beat.asset_ref
    )
    path = studio.repo.project_dir(project.id) / "images" / "fixture-code.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    _editorial_card(project, asset, path)
    project.longform_media[asset.id] = studio.repo.artifact(
        project, path, "longform_visual", width=1920, height=1080
    )
    project.state = State.LONGFORM_SEMANTIC_PLANNING
    studio.repo.save(project, "test_semantic_checkpoint", {})
    script = longform_script(project.spec)
    other_beats = {
        item.id: item.model_dump(mode="json")
        for chapter in project.spec.chapters for item in chapter.visual_beats if item.id != beat.id
    }
    old_path = project.longform_media[asset.id].path
    revised = repair_unrendered_design_beat(
        studio, project, beat.id,
        {
            "design_family": "editorial_frame",
            "design_treatment": "statement_reveal",
            "design_rationale": "The spoken point works as a concise statement reveal.",
            "role_semantics": [],
        },
        actor="test", reason="Treatment must follow the approved speech",
    )
    assert longform_script(revised.spec) == script
    assert revised.longform_media[asset.id].path != old_path
    assert studio.repo.project_dir(project.id).joinpath(old_path).exists()
    assert {
        item.id: item.model_dump(mode="json")
        for chapter in revised.spec.chapters for item in chapter.visual_beats if item.id != beat.id
    } == other_beats
    assert studio.repo.events(project.id)[-1]["kind"] == "longform_visual_beat_repaired"
    with pytest.raises(ValueError, match="only visual-planning attributes"):
        repair_unrendered_design_beat(
            studio, revised, beat.id, {"purpose": "changed", "asset_ref": "other"},
            actor="test", reason="invalid",
        )


def test_wikimedia_discovery_rejects_licensed_but_unrelated_result():
    class HTTP:
        async def get_json(self, url, headers=None, **kwargs):
            return {
                "query": {
                    "pages": [
                        {
                            "title": "File:Modern hospital radiology.jpg",
                            "imageinfo": [
                                {
                                    "mime": "image/jpeg",
                                    "width": 1200,
                                    "height": 800,
                                    "thumburl": "https://upload.wikimedia.org/preview.jpg",
                                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:Modern_hospital_radiology.jpg",
                                    "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}},
                                }
                            ],
                        }
                    ]
                }
            }

    request = AuthenticRequestDraft(
        id="auth_1",
        beat_ids=["beat_1"],
        search_query="Marie Curie First World War hospital X-ray photograph",
        fallback_prompt="A restrained editorial illustration of a historical hospital.",
    )
    with pytest.raises(ValueError, match="No compatible"):
        asyncio.run(WikimediaCommonsDiscovery(HTTP()).discover(request, None))


def test_wikimedia_exact_source_rejects_substitution():
    class HTTP:
        async def get_json(self, url, headers=None, **kwargs):
            return {
                "query": {
                    "pages": [
                        {
                            "title": "File:Other.jpg",
                            "imageinfo": [
                                {
                                    "mime": "image/jpeg",
                                    "width": 1200,
                                    "height": 800,
                                    "thumburl": "https://upload.wikimedia.org/other.jpg",
                                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:Other.jpg",
                                    "extmetadata": {"LicenseShortName": {"value": "Public domain"}},
                                }
                            ],
                        }
                    ]
                }
            }

    request = AuthenticRequestDraft(
        id="auth_1",
        beat_ids=["beat_1"],
        search_query="Marie Curie portrait",
        fallback_prompt="A restrained editorial illustration of Marie Curie.",
    )
    with pytest.raises(ValueError, match="curated Commons file"):
        asyncio.run(
            WikimediaCommonsDiscovery(HTTP()).discover_exact(
                request, "https://commons.wikimedia.org/wiki/File:Marie_Curie.jpg"
            )
        )
