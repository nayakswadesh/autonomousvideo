"""Durable LF v0.3 visual planning, rights records and contact sheets."""

from __future__ import annotations

import io
import json
import re
import textwrap
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw, ImageOps

from autonomous_studio.domain.longform_feedback import relevant_longform_feedback
from autonomous_studio.domain.models import Approval, LongFormSpec, State, now
from autonomous_studio.providers.http import RemoteError
from autonomous_studio.providers.longform_visuals import (
    AuthenticRequestDraft,
    LongFormVisualPlanDraft,
    MockAuthenticMediaDiscovery,
    MockLongFormVisualPlanner,
    WikimediaCommonsDiscovery,
    materialize_visual_plan,
    validate_visual_choice_rationales,
)
from autonomous_studio.providers.openai import ProviderValidationError, output_text
from autonomous_studio.rendering.editorial import face


async def refresh_curated_visual_plan(studio, project, selections: dict[str, str]):
    """Replace unproduced search fallbacks with verified, project-curated files.

    This keeps approved narration and visual beats intact. A new approval request is
    recorded because the rights/cost package has materially changed.
    """
    if project.state != State.VISUAL_PLAN_APPROVAL or not project.pending():
        raise ValueError("curated refresh requires a pending long-form visual review")
    if project.longform_media or project.longform_image_requests:
        raise ValueError("cannot refresh archival choices after media production has begun")
    drafts = project.current_artifacts("longform_visual_plan_draft")
    if not drafts:
        raise ValueError("the original visual-plan draft is missing")
    draft = LongFormVisualPlanDraft.model_validate_json(
        studio.repo.verify(project, drafts[-1]).read_text(encoding="utf-8")
    )
    authentic = {
        request.id: request
        for request in draft.assets
        if isinstance(request, AuthenticRequestDraft)
    }
    if not set(selections) <= set(authentic):
        raise ValueError("curated source IDs must identify authentic requests in the draft")
    discovery = studio.authentic_media_discovery or WikimediaCommonsDiscovery(
        studio.live_client.http
    )
    found = {}
    previously_verified = {
        asset.id: asset
        for asset in project.longform_visual_plan.assets
        if asset.kind == "authentic_image"
    }
    for asset_id, source_page in selections.items():
        prior = previously_verified.get(asset_id)
        found[asset_id] = (
            prior
            if prior is not None and prior.source_page == source_page
            else await discovery.discover_exact(authentic[asset_id], source_page)
        )
    discovery_failures = {}
    if not selections:
        throttled = False
        for asset_id, request in authentic.items():
            if throttled:
                discovery_failures[asset_id] = "Wikimedia Commons search throttled"
                continue
            try:
                found[asset_id] = await discovery.discover(request, project.spec)
            except ValueError as exc:
                discovery_failures[asset_id] = str(exc)[:300]
            except RemoteError as exc:
                discovery_failures[asset_id] = str(exc)[:300]
                throttled = exc.status == 429
    fallbacks = {
        asset_id: discovery_failures.get(
            asset_id, "No period-appropriate rights-compatible source was selected"
        )
        for asset_id in authentic
        if asset_id not in found
    }
    spec, plan = materialize_visual_plan(
        project.spec,
        draft,
        project.longform_visual_policy,
        found,
        image_model=project.live_models.get("image_model", "gpt-image-2"),
        authentic_failures=fallbacks,
    )
    output = studio.repo.attempt_dir(project, "curated-archive-refresh")
    project.spec, project.longform_visual_plan = spec, plan
    studio._json(
        project, output, "visual-plan.json", plan.model_dump(mode="json"), "longform_visual_plan"
    )
    studio._json(
        project,
        output,
        "rights-plan.json",
        plan.rights.model_dump(mode="json"),
        "longform_rights_plan",
    )
    studio._json(
        project,
        output,
        "cost-estimate.json",
        plan.cost.model_dump(mode="json"),
        "longform_cost_estimate",
    )
    studio._json(
        project,
        output,
        "curated-sources.json",
        {"schema_version": 1, "sources": selections, "fallbacks": fallbacks},
        "longform_curated_sources",
    )
    await _save_authentic_previews(studio, project, output)
    for path in render_contact_sheets(project, output, studio.repo):
        project.artifacts.append(
            studio.repo.artifact(
                project,
                path,
                "longform_contact_sheet",
                provider="local",
                model="lf-v0.3-contact-sheet",
                width=1600,
                height=1000,
            )
        )
    pending = project.pending()
    pending.decision, pending.actor, pending.decided_at = "superseded", "local-curation", now()
    pending.message = "Superseded by refreshed archival search and safe fallback plan"
    replacement = Approval(
        id=uuid4().hex,
        project_id=project.id,
        gate="visual_plan",
        version=project.version,
        artifact_ids=[artifact.id for artifact in project.current_artifacts()],
    )
    project.approvals.append(replacement)
    studio.repo.save(
        project,
        "longform_visual_plan_sources_curated",
        {
            "version": project.version,
            "superseded_approval_id": pending.id,
            "approval_id": replacement.id,
            "authentic_sources": sorted(found),
            "generated_requests": plan.cost.generated_request_count,
            "paid_asset_generation_started": False,
        },
    )
    return project


async def repair_unacquired_authentic_source(
    studio, project, asset_id: str, source_page: str, *, actor: str
):
    """Audit an explicitly authorized source correction without re-buying prepared media."""
    if project.state != State.LONGFORM_ASSETS_PREPARING:
        raise ValueError("source repair requires the long-form asset acquisition checkpoint")
    if asset_id in project.longform_media:
        raise ValueError("cannot replace an authentic source after that asset was acquired")
    if not any(
        approval.gate == "visual_plan"
        and approval.version == project.version
        and approval.decision == "approve"
        for approval in project.approvals
    ):
        raise ValueError("approved visual plan required for source repair")
    if not project.longform_visual_plan:
        raise ValueError("visual plan is missing")
    prior = next(
        (
            asset
            for asset in project.longform_visual_plan.assets
            if asset.id == asset_id and asset.kind == "authentic_image"
        ),
        None,
    )
    if prior is None or prior.source_page == source_page:
        raise ValueError("source repair needs an unacquired, changed authentic candidate")
    if studio.live_client is None:
        raise ValueError("live Wikimedia client required for source repair")
    request = AuthenticRequestDraft(
        id=prior.id,
        beat_ids=prior.beat_ids,
        search_query=prior.search_query,
        fallback_prompt="Contemporary editorial illustration of the approved visual idea.",
    )
    discovery = WikimediaCommonsDiscovery(studio.live_client.http)
    replacement = await discovery.discover_exact(request, source_page)
    if not discovery._relevant({"title": replacement.title}, {}, prior.search_query):
        raise ValueError("replacement source does not match the approved visual intent")
    plan = project.longform_visual_plan
    plan.assets = [replacement if asset.id == asset_id else asset for asset in plan.assets]
    plan.rights.attribution_lines = [
        replacement.attribution if line == prior.attribution else line
        for line in plan.rights.attribution_lines
    ]
    output = studio.repo.attempt_dir(project, "authentic-source-repair")
    studio._json(
        project, output, "visual-plan.json", plan.model_dump(mode="json"),
        "longform_visual_plan",
    )
    studio._json(
        project, output, "rights-plan.json", plan.rights.model_dump(mode="json"),
        "longform_rights_plan",
    )
    studio._json(
        project, output, "source-repair.json",
        {
            "asset_id": asset_id,
            "prior_source": prior.source_page,
            "replacement_source": replacement.source_page,
            "actor": actor,
            "reason": "approved candidate failed production source validation",
        },
        "longform_authentic_source_repair",
    )
    project.last_error = None
    studio.repo.save(
        project,
        "longform_authentic_source_repaired",
        {
            "version": project.version,
            "asset_id": asset_id,
            "prior_source": prior.source_page,
            "replacement_source": replacement.source_page,
            "actor": actor,
        },
    )
    return project


def repair_unrendered_design_beat(
    studio, project, beat_id: str, changes: dict, *, actor: str, reason: str
):
    """Repair only an unrendered code beat after explicit user authorization."""
    from autonomous_studio.domain.models import VisualBeat
    from autonomous_studio.providers.longform import longform_script
    from autonomous_studio.providers.longform_media import _editorial_card
    from autonomous_studio.providers.longform_visuals import (
        ChapterVisualPlanDraft,
        CodeRequestDraft,
        VisualBeatDraft,
        validate_family_choices,
    )
    from autonomous_studio.workflow.spoken_units import (
        beat_words,
        unique_phrase_position,
        validate_unit_coverage,
    )

    if project.state != State.LONGFORM_SEMANTIC_PLANNING:
        raise ValueError("design-beat repair requires the semantic-planning checkpoint")
    allowed = {
        "design_family", "design_treatment", "design_rationale", "purpose",
        "screen_title", "display_layout", "role_unit_groups", "role_semantics",
        "overlay_text", "overlay_anchors", "narration_anchor",
    }
    if not changes or not set(changes) <= allowed:
        raise ValueError("design-beat repair may change only visual-planning attributes")
    chapter = next(
        (item for item in project.spec.chapters if any(b.id == beat_id for b in item.visual_beats)),
        None,
    )
    if chapter is None:
        raise ValueError("unknown visual beat")
    if chapter.id in project.longform_semantic_decisions or chapter.id in project.longform_chapters:
        raise ValueError("cannot repair a beat after its chapter was planned or rendered")
    original = next(item for item in chapter.visual_beats if item.id == beat_id)
    asset = next(
        (item for item in project.longform_visual_plan.assets if item.id == original.asset_ref),
        None,
    )
    if asset is None or asset.kind != "code_rendered":
        raise ValueError("only an approved code-rendered beat can use this repair")
    updated = VisualBeat.model_validate({**original.model_dump(mode="json"), **changes})
    if updated.spoken_unit_ids:
        spoken_words = beat_words(chapter, updated)
    else:
        blocks = [
            block for block in chapter.narration_blocks
            if block.id in updated.narration_block_ids
        ]
        spoken_words = re.findall(r"\w+", " ".join(block.text for block in blocks).casefold())
    unique_phrase_position(spoken_words, updated.narration_anchor)
    draft_data = {
        key: value for key, value in updated.model_dump(mode="json").items()
        if key in VisualBeatDraft.model_fields and key != "asset_id"
    }
    draft_data["asset_id"] = updated.asset_ref
    draft = ChapterVisualPlanDraft(
        style_direction="Approved documentary style retained during a local design-beat repair.",
        rationale="Only the unrendered beat's visual treatment and reveals were repaired.",
        beats=[VisualBeatDraft.model_validate(draft_data)],
        assets=[CodeRequestDraft(
            id=asset.id, beat_ids=[beat_id], renderer=asset.renderer
        )],
    )
    validate_family_choices(draft, project.spec, require_rationale=True, require_role_contract=True)
    revised_spec = project.spec.model_copy(deep=True)
    revised_chapter = next(item for item in revised_spec.chapters if item.id == chapter.id)
    revised_chapter.visual_beats = [
        updated if item.id == beat_id else item for item in revised_chapter.visual_beats
    ]
    if any(beat.spoken_unit_ids for beat in revised_chapter.visual_beats):
        validate_unit_coverage(revised_chapter, revised_chapter.visual_beats)
    if longform_script(revised_spec) != longform_script(project.spec):
        raise ValueError("design-beat repair cannot change approved narration")
    output = studio.repo.attempt_dir(project, "design-beat-repair")
    image_path = (
        studio.repo.project_dir(project.id) / "images" / f"v{project.version:03d}"
        / f"{asset.id}.repair-{uuid4().hex[:8]}.png"
    )
    image_path.parent.mkdir(parents=True, exist_ok=True)
    project.spec = revised_spec
    _editorial_card(project, asset, image_path)
    visual = studio.repo.artifact(
        project, image_path, "longform_visual", provider="local",
        model="lf-v0.4-beat-repair", width=1920, height=1080,
    )
    project.artifacts.append(visual)
    project.longform_media[asset.id] = visual
    studio._json(
        project, output, "beat-repair.json",
        {
            "version": project.version,
            "beat_id": beat_id,
            "chapter_id": chapter.id,
            "actor": actor,
            "reason": reason,
            "before": original.model_dump(mode="json"),
            "after": updated.model_dump(mode="json"),
            "approved_script_unchanged": True,
        },
        "longform_visual_beat_repair",
    )
    project.last_error = project.last_error_code = None
    studio.repo.save(
        project, "longform_visual_beat_repaired",
        {"version": project.version, "beat_id": beat_id, "actor": actor, "reason": reason},
    )
    return project


def _card_preview(canvas: Image.Image, draw: ImageDraw.ImageDraw, box, asset, preview=None):
    left, top, right, bottom = box
    colors = {
        "authentic_image": "#34495a",
        "generated_image": "#8f623f",
        "code_rendered": "#345c55",
    }
    draw.rounded_rectangle(box, radius=18, fill=colors[asset.kind], outline="#d6b178", width=2)
    if preview is not None:
        fitted = ImageOps.fit(preview.convert("RGB"), (right - left, bottom - top))
        canvas.paste(fitted, (left, top))
        draw.rectangle((left, top, right, top + 42), fill="#101923")
    label = {
        "authentic_image": "AUTHENTIC MEDIA CANDIDATE",
        "generated_image": "GENERATED IMAGE — NOT YET REQUESTED",
        "code_rendered": "CODE-RENDERED VISUAL",
    }[asset.kind]
    draw.text((left + 24, top + 18), label, font=face(24, True), fill="#f0efe8")
    if asset.kind == "code_rendered":
        for index, width in enumerate((0.25, 0.55, 0.8)):
            y = top + 65 + index * 24
            draw.rounded_rectangle(
                (left + 34, y, left + 34 + int((right - left - 68) * width), y + 13),
                radius=6,
                fill="#d6b178",
            )


def render_contact_sheets(project, output: Path, repo) -> list[Path]:
    spec = project.spec
    plan = project.longform_visual_plan
    assets = {asset.id: asset for asset in plan.assets}
    previews = {}
    for artifact in project.current_artifacts("visual_plan_preview"):
        asset_id = artifact.provenance.asset_id
        try:
            with Image.open(repo.resolve(project.id, artifact.path)) as opened:
                previews[asset_id] = opened.convert("RGB").copy()
        except (OSError, ValueError):
            continue
    beats = [beat for chapter in spec.chapters for beat in chapter.visual_beats]
    paths = []
    page_size = 6
    for page_index in range(0, len(beats), page_size):
        page_beats = beats[page_index : page_index + page_size]
        image = Image.new("RGB", (1600, 1000), "#101923")
        draw = ImageDraw.Draw(image)
        draw.text(
            (55, 30),
            f"{spec.brief.title} — VISUAL PLAN {page_index // page_size + 1}",
            font=face(34, True),
            fill="#f0efe8",
        )
        draw.text(
            (55, 75),
            "Planning placeholders only • no paid image generation has started",
            font=face(20),
            fill="#d6b178",
        )
        for card_index, beat in enumerate(page_beats):
            col, row = card_index % 2, card_index // 2
            x = 55 + col * 775
            y = 125 + row * 280
            asset = assets[beat.asset_ref]
            _card_preview(
                image,
                draw,
                (x, y, x + 735, y + 145),
                asset,
                previews.get(asset.id),
            )
            chapter = next(item for item in spec.chapters if item.id == beat.chapter_id)
            visual_label = (
                f"{beat.design_family} / {beat.design_treatment}"
                if beat.design_family
                else beat.visual_type.replace("_", " ")
            )
            draw.text(
                (x, y + 162),
                f"CH {chapter.order} • {beat.start:.1f}–{beat.start + beat.duration:.1f}s • "
                + visual_label.upper(),
                font=face(19, True),
                fill="#d6b178",
            )
            purpose = textwrap.shorten(beat.purpose, width=115, placeholder="…")
            draw.multiline_text(
                (x, y + 195),
                textwrap.fill(purpose, width=68),
                font=face(18),
                fill="#f0efe8",
                spacing=5,
            )
        path = output / f"contact-sheet-{page_index // page_size + 1:02d}.png"
        image.save(path, format="PNG", optimize=True)
        paths.append(path)
    return paths


async def _save_authentic_previews(studio, project, output):
    if project.pipeline != "live" or studio.live_client is None:
        return
    for asset in project.longform_visual_plan.assets:
        if asset.kind != "authentic_image":
            continue
        try:
            content = await studio.live_client.http.get(
                asset.preview_url,
                {"User-Agent": "AutonomousStudio/1.0 (visual plan preview)"},
                timeout=60,
                service="Wikimedia Commons preview",
                attempts=2,
            )
            if len(content) > 4_000_000:
                continue
            with Image.open(io.BytesIO(content)) as opened:
                opened.load()
                preview = ImageOps.fit(
                    opened.convert("RGB"), (640, 360), method=Image.Resampling.LANCZOS
                )
            path = output / f"{asset.id}-candidate-preview.jpg"
            preview.save(path, format="JPEG", quality=82, optimize=True)
            artifact = studio.repo.artifact(
                project,
                path,
                "visual_plan_preview",
                provider="wikimedia-commons",
                model="candidate-preview-v1",
                width=640,
                height=360,
            )
            artifact.provenance.source = asset.source_page
            artifact.provenance.provider = "wikimedia-commons"
            artifact.provenance.asset_id = asset.id
            artifact.provenance.license_category = "licensed"
            artifact.provenance.usage_restrictions = (
                "Planning preview only; verify context and personality rights before publication."
            )
            artifact.provenance.attribution = asset.attribution
            artifact.provenance.origin = "library"
            project.artifacts.append(artifact)
        except Exception:
            # A contact sheet remains useful with the rights-record placeholder. Preview failure
            # must not silently replace or approve the candidate itself.
            continue


def _cached_visual_draft(studio, project):
    """Recover the latest structurally complete plan after a beat-only validation failure."""
    recoverable = (
        "uses an anchor absent from approved narration",
        "needs at most two image labels",
    )
    failures = [
        event["payload"].get("error", "")
        for event in studio.repo.events(project.id)
        if event["kind"] == "longform_visual_plan_rejected"
        and event["payload"].get("version") == project.version
    ]
    if not any(any(term in error for term in recoverable) for error in failures):
        return None
    traces = [
        artifact
        for artifact in project.current_artifacts("provider_usage")
        if "longform-visual-plan-attempt-" in artifact.path
    ]
    for artifact in reversed(traces):
        records = json.loads(studio.repo.verify(project, artifact).read_text(encoding="utf-8"))
        for record in reversed(records):
            try:
                return LongFormVisualPlanDraft.model_validate_json(output_text(record["response"]))
            except (KeyError, ValueError):
                continue
    return None


def saved_visual_responses(studio, project, artifact_path=None, *, allow_partial=False):
    """Read a verified, complete first draft for validation-only recovery."""
    traces = [
        artifact for artifact in project.current_artifacts("provider_usage")
        if "longform-visual-plan-attempt-" in artifact.path
    ]
    if not traces:
        raise ValueError("saved long-form visual responses are unavailable")
    artifact = next(
        (item for item in traces if item.path == artifact_path), None
    ) if artifact_path else traces[-1]
    if artifact is None:
        raise ValueError("saved visual replay artifact is unavailable")
    records = json.loads(studio.repo.verify(project, artifact).read_text(encoding="utf-8"))
    if not isinstance(records, list) or not records or (
        not allow_partial and len(records) < len(project.spec.chapters)
    ):
        raise ValueError("saved long-form visual responses are incomplete")
    for record in records:
        if (
            not isinstance(record, dict)
            or not record.get("chapter_id")
            or not record.get("requested_model")
        ):
            raise ValueError("saved long-form visual response metadata is incomplete")
        output_text(record["response"])
    return artifact, records


def saved_image_role_recovery(studio, project, failure):
    """Reuse a rejected image draft when only the obsolete role-group check failed."""
    match = re.search(r"\b([\w-]+) reveals must cover its speech once in order\b", failure)
    if match is None:
        return None
    artifact, records = saved_visual_responses(studio, project)
    selection = json.loads(output_text(records[-1]["response"]))
    if any(
        beat.get("id") == match.group(1)
        and beat.get("visual_type") in {"authentic_image", "generated_image"}
        for beat in selection.get("beats", [])
    ):
        return artifact
    return None


class SavedVisualResponseReplay:
    """Replay validated historical outputs, optionally continuing unfinished chapters live."""

    def __init__(self, client, records, *, allow_continuation=False):
        self.settings = client.settings
        self.longform_creative_policy = client.longform_creative_policy
        self.cache = client.cache
        self.trace = []
        self._records = iter(records)
        self._remaining = len(records)
        self._live_client = client if allow_continuation else None
        self._last_was_live = False

    async def response(self, model, prompt, **kwargs):
        try:
            record = next(self._records)
        except StopIteration as exc:
            if self._live_client is None:
                raise ValueError(
                    "saved visual responses exhausted; no new model call allowed"
                ) from exc
            response = await self._live_client.response(model, prompt, **kwargs)
            self.trace.append(dict(self._live_client.trace[-1]))
            self._last_was_live = True
            return response
        self._remaining -= 1
        marker = prompt.find('{"story":')
        if marker < 0:
            raise ValueError("saved visual response no longer matches the planner request")
        payload, _ = json.JSONDecoder().raw_decode(prompt[marker:])
        chapter_id = payload.get("chapter", {}).get("id")
        if record["requested_model"] != model or record["chapter_id"] != chapter_id:
            raise ValueError("saved visual response no longer matches the planner chapter/model")
        response = record["response"]
        output_text(response)
        self._last_was_live = False
        self.trace.append({
            "response": response,
            "cache_hit": True,
            "saved_response_replay": True,
            "requested_model": model,
            "cache_key": record.get("cache_key"),
        })
        return response

    def reject_last(self, _reason):
        if self._last_was_live:
            self._live_client.reject_last(_reason)
        # A validation failure must not poison or overwrite the historical cache.

    def assert_complete(self):
        if self._remaining:
            raise ValueError("saved visual responses were not fully consumed")


async def prepare_longform_visual_plan(studio, project, output):
    if not isinstance(project.spec, LongFormSpec):
        raise ValueError("long-form visual planning requires LongFormSpec")
    if not project.longform_visual_policy:
        from autonomous_studio.domain.models import LongFormVisualPolicy

        project.longform_visual_policy = LongFormVisualPolicy()
    planner = studio.longform_visual_planner or MockLongFormVisualPlanner()
    discovery = studio.authentic_media_discovery
    if discovery is None:
        discovery = (
            WikimediaCommonsDiscovery(studio.live_client.http)
            if project.pipeline == "live" and studio.live_client
            else MockAuthenticMediaDiscovery()
        )
    feedback = relevant_longform_feedback(project)
    replay_artifact = project.creation_settings.get("visual_plan_replay_artifact")
    replay_records = None
    if replay_artifact:
        _, replay_records = saved_visual_responses(
            studio, project, replay_artifact,
            allow_partial=bool(project.creation_settings.get("visual_plan_replay_allow_continuation")),
        )
    failure = ""
    max_attempts = (
        1 + project.longform_creative_policy.repair_limits["visual_chapter"]
        if project.longform_creative_policy is not None
        else 2
    )
    while project.longform_visual_plan_attempts < max_attempts:
        project.longform_visual_plan_attempts += 1
        studio.repo.save(
            project,
            "longform_visual_plan_requested",
            {
                "version": project.version,
                "attempt": project.longform_visual_plan_attempts,
                "paid_asset_generation_started": False,
            },
        )
        try:
            cached = (
                _cached_visual_draft(studio, project)
                if replay_records is None and project.longform_visual_plan_attempts == 1
                and hasattr(planner, "_repair_spoken_beats")
                else None
            )
            if replay_records is not None:
                if not hasattr(planner, "client"):
                    raise ValueError("saved visual replay requires the original live planner")
                original_client = planner.client
                allow_continuation = bool(project.creation_settings.get(
                    "visual_plan_replay_allow_continuation"
                ))
                trace_start = len(original_client.trace)
                replay_client = SavedVisualResponseReplay(
                    original_client, replay_records,
                    allow_continuation=allow_continuation,
                )
                planner.client = replay_client
                try:
                    draft = await planner.plan(
                        project.spec, project.longform_visual_policy, feedback
                    )
                    replay_client.assert_complete()
                finally:
                    planner.client = original_client
                    if studio.live_client is not None:
                        if allow_continuation:
                            del original_client.trace[trace_start:]
                        studio.live_client.trace.extend(replay_client.trace)
            else:
                draft = (
                    await planner._repair_spoken_beats(project.spec, cached)
                    if cached is not None
                    else await planner.plan(
                        project.spec,
                        project.longform_visual_policy,
                        feedback + ([failure] if failure else []),
                    )
                )
            validate_visual_choice_rationales(draft)
            found = {}
            discovery_failures = {}
            for request in draft.assets:
                if not isinstance(request, AuthenticRequestDraft):
                    continue
                try:
                    found[request.id] = await discovery.discover(request, project.spec)
                except ValueError as exc:
                    # Materialization applies the approved generated fallback and counts it
                    # against the standing generated-image budget.
                    discovery_failures[request.id] = str(exc)[:300]
                except RemoteError as exc:
                    # Candidate discovery is a read-only planning aid. A throttled or unavailable
                    # library must not discard the approved script or force an unchanged retry;
                    # use the declared fallback and make the reason visible in the plan.
                    discovery_failures[request.id] = str(exc)[:300]
            spec, plan = materialize_visual_plan(
                project.spec,
                draft,
                project.longform_visual_policy,
                found,
                image_model=(
                    project.live_models.get("image_model", "gpt-image-2")
                    if project.pipeline == "live"
                    else "mock-image"
                ),
                authentic_failures=discovery_failures,
            )
            if project.pipeline == "live":
                from autonomous_studio.workflow.visual_reveal_timing import (
                    validate_saved_speech_against_visual_plan,
                )

                validate_saved_speech_against_visual_plan(project, spec, plan)
            final_prompts = None
            if project.pipeline == "live" and project.longform_creative_policy is not None:
                from autonomous_studio.providers.longform_media import longform_image_payload

                trial = project.model_copy(
                    update={"spec": spec, "longform_visual_plan": plan}
                )
                final_prompts = {
                    asset.id: longform_image_payload(
                        trial, asset, studio.live_client.settings
                    )
                    for asset in plan.assets
                    if asset.kind == "generated_image"
                }
            project.spec = spec
            project.longform_visual_plan = plan
            project.creation_settings.pop("visual_plan_replay_artifact", None)
            project.creation_settings.pop("visual_plan_replay_allow_continuation", None)
            if final_prompts is not None:
                studio._json(
                    project,
                    output,
                    "final-image-prompts.json",
                    final_prompts,
                    "longform_image_prompt_plan",
                )
            studio._json(
                project,
                output,
                f"visual-plan-draft-{project.longform_visual_plan_attempts}.json",
                draft.model_dump(mode="json"),
                "longform_visual_plan_draft",
            )
            if discovery_failures:
                replacements = {asset.id: asset.kind for asset in plan.assets}
                studio._json(
                    project,
                    output,
                    "authentic-discovery-report.json",
                    {
                        "schema_version": 1,
                        "project_id": project.id,
                        "version": project.version,
                        "fallbacks": [
                            {
                                "asset_id": asset_id,
                                "reason": reason,
                                "replacement": replacements[asset_id],
                            }
                            for asset_id, reason in sorted(discovery_failures.items())
                        ],
                    },
                    "longform_authentic_discovery_report",
                )
            break
        except RemoteError as exc:
            if exc.status in {429, 500, 502, 503, 504}:
                # A provider response is not a rejected creative draft. Preserve the
                # approved script and leave the validation allowance available for
                # an explicit retry after the service recovers.
                project.longform_visual_plan_attempts -= 1
                studio.repo.save(
                    project,
                    "longform_visual_plan_service_interrupted",
                    {
                        "version": project.version,
                        "status": exc.status,
                        "paid_asset_generation_started": False,
                    },
                )
            raise
        except ValueError as exc:
            failure = str(exc)[:1200]
            studio.record_trace(
                project,
                output,
                f"longform-visual-plan-attempt-{project.longform_visual_plan_attempts}",
            )
            studio.repo.save(
                project,
                "longform_visual_plan_rejected",
                {
                    "version": project.version,
                    "attempt": project.longform_visual_plan_attempts,
                    "error": failure,
                },
            )
    if project.longform_visual_plan is None:
        if project.longform_creative_policy is not None:
            raise ProviderValidationError(
                "Long-form visual plan", failure, repair_count=max_attempts - 1
            )
        raise ValueError("Long-form visual planning failed after two attempts; revise the plan")
    studio.record_trace(project, output, "longform-visual-plan")
    studio._json(
        project,
        output,
        "visual-plan.json",
        project.longform_visual_plan.model_dump(mode="json"),
        "longform_visual_plan",
    )
    studio._json(
        project,
        output,
        "rights-plan.json",
        project.longform_visual_plan.rights.model_dump(mode="json"),
        "longform_rights_plan",
    )
    studio._json(
        project,
        output,
        "cost-estimate.json",
        project.longform_visual_plan.cost.model_dump(mode="json"),
        "longform_cost_estimate",
    )
    studio._json(
        project,
        output,
        "visual-evidence-map.json",
        {
            "schema_version": 1,
            "project_id": project.id,
            "version": project.version,
            "chapters": [
                {
                    "chapter_id": chapter.id,
                    "beats": [beat.model_dump(mode="json") for beat in chapter.visual_beats],
                }
                for chapter in project.spec.chapters
            ],
        },
        "longform_visual_evidence_map",
    )
    studio._json(
        project,
        output,
        "visual-design-decisions.json",
        {
            "schema_version": 1,
            "project_id": project.id,
            "version": project.version,
            "beats": [
                {
                    "id": beat.id,
                    "chapter_id": beat.chapter_id,
                    "narration_block_ids": beat.narration_block_ids,
                    "visual_type": beat.visual_type,
                    "design_family": beat.design_family,
                    "design_treatment": beat.design_treatment,
                    "design_rationale": beat.design_rationale,
                    "mode_assessment": (
                        beat.mode_assessment.model_dump(mode="json")
                        if beat.mode_assessment is not None else None
                    ),
                    "purpose": beat.purpose,
                }
                for chapter in project.spec.chapters
                for beat in chapter.visual_beats
            ],
        },
        "longform_visual_design_decisions",
    )
    await _save_authentic_previews(studio, project, output)
    for path in render_contact_sheets(project, output, studio.repo):
        project.artifacts.append(
            studio.repo.artifact(
                project,
                path,
                "longform_contact_sheet",
                provider="local",
                model="lf-v0.3-contact-sheet",
                width=1600,
                height=1000,
            )
        )
    studio.repo.save(
        project,
        "longform_visual_plan_prepared",
        {
            "version": project.version,
            "visual_beats": sum(len(c.visual_beats) for c in project.spec.chapters),
            "assets": len(project.longform_visual_plan.assets),
            "generated_requests": project.longform_visual_plan.cost.generated_request_count,
            "paid_asset_generation_started": False,
        },
    )
    return project
