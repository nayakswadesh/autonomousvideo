from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from uuid import uuid4

import yaml
from pydantic import TypeAdapter

from autonomous_studio.audio.timing import (
    MAX_TIMING_REPAIRS,
    NarrationTimingError,
    is_timing_failure,
    measured_plan,
    voice_signature,
)
from autonomous_studio.domain.formats import short_profile_id
from autonomous_studio.domain.longform_policy import CREATIVE_STAGES, LongFormCreativePolicy
from autonomous_studio.domain.models import (
    GATES,
    Approval,
    Brief,
    DurationPolicy,
    EngagementEnding,
    Feedback,
    LongFormBrief,
    LongFormSpec,
    LongFormVisualPolicy,
    PreparedVoice,
    ProjectBrief,
    ResearchBrief,
    State,
    VideoProject,
    VideoSpec,
    VoiceSpec,
    now,
    validate_story_scope,
)
from autonomous_studio.hitl.adapters import LocalHITL, review_message, verify_approval
from autonomous_studio.providers.longform import (
    MockLongFormDirector,
    MockLongFormResearch,
)
from autonomous_studio.providers.mock import MockAudio, MockDirector, MockResearch
from autonomous_studio.providers.openai import (
    IncompleteResponseError,
    ProviderTimeoutError,
    ProviderValidationError,
)
from autonomous_studio.rendering.longform import LongFormRenderer
from autonomous_studio.rendering.renderer import LocalRenderer, MockFrames
from autonomous_studio.research.quality import audio_warnings, research_warnings
from autonomous_studio.storage import Repository
from autonomous_studio.storage.repository import write_json


def load_brief(path: Path) -> ProjectBrief:
    if path.stat().st_size > 2_000_000:
        raise ValueError("brief exceeds 2 MB")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "format" not in data:
        data["format"] = "vertical_short"
    return TypeAdapter(ProjectBrief).validate_python(data)


def is_legacy_longform_duration_failure(project: VideoProject) -> bool:
    """Allow an explicit retry after v0.2.5 replaced the old hard duration failure."""
    return bool(
        isinstance(project.spec, LongFormSpec)
        and project.state == State.LONGFORM_SCRIPTING
        and project.last_error_code == "provider_validation"
        and project.last_error
        and "Estimated speech is" in project.last_error
        and "keep it at or below" in project.last_error
    )


def is_visual_provider_transport_failure(project: VideoProject) -> bool:
    """Recover an older visual attempt spent on a definitive provider outage."""
    return bool(
        isinstance(project.spec, LongFormSpec)
        and project.state == State.LONGFORM_VISUAL_PLANNING
        and project.longform_visual_plan is None
        and project.longform_visual_plan_attempts > 0
        and project.last_error
        and any(
            project.last_error.startswith(f"OpenAI request failed (HTTP {status}")
            for status in (429, 500, 502, 503, 504)
        )
    )


class Studio:
    def __init__(
        self,
        root: Path,
        *,
        research=None,
        director=None,
        images=None,
        voice=None,
        music=None,
        sfx=None,
        renderer=None,
        hitl=None,
        settings=None,
        live_http=None,
        asset_planner=None,
        auto_images=None,
        media=None,
        longform_research=None,
        longform_director=None,
        longform_visual_planner=None,
        longform_semantic_director=None,
        authentic_media_discovery=None,
        longform_renderer=None,
    ):
        self.repo = Repository(root)
        self.research = research or MockResearch()
        self.director = director or MockDirector()
        self.images = images or MockFrames(self.repo)
        self.voice = voice or MockAudio(self.repo)
        self.music = music or MockAudio(self.repo)
        self.sfx = sfx or MockAudio(self.repo)
        self.ambience = MockAudio(self.repo)
        self.renderer = renderer or LocalRenderer(self.repo)
        self.hitl = hitl or LocalHITL()
        self.settings = settings
        self.live_http = live_http
        self.live_client = None
        self.asset_planner, self.auto_images = asset_planner, auto_images
        self.media = media
        self.longform_research = longform_research or MockLongFormResearch()
        self.longform_director = longform_director or MockLongFormDirector()
        self.longform_visual_planner = longform_visual_planner
        from autonomous_studio.providers.semantic_director import MockSemanticDirector

        self.longform_semantic_director = longform_semantic_director or MockSemanticDirector()
        self.authentic_media_discovery = authentic_media_discovery
        self.longform_renderer = longform_renderer or LongFormRenderer(self.repo)

    def configure_live(self, project):
        from autonomous_studio.config import Settings
        from autonomous_studio.providers.openai import (
            OpenAIClient,
            OpenAIDirector,
            OpenAIResearch,
            OpenAIVoice,
        )

        settings = self.settings or Settings.environment()
        settings = settings.model_copy(update=project.live_models)
        settings.require_key()
        self.live_client = OpenAIClient(
            settings, self.repo.project_dir(project.id) / "provider-cache", self.live_http
        )
        if isinstance(project.spec, LongFormSpec):
            self.live_client.longform_creative_policy = project.longform_creative_policy
        self.research = OpenAIResearch(self.live_client)
        self.director = OpenAIDirector(self.live_client)
        self.voice = OpenAIVoice(self.live_client, self.repo)
        if project.spec.brief.voice.provider == "gemini":
            from autonomous_studio.providers.gemini_tts import GeminiVoice

            self.voice = GeminiVoice(
                self.repo, settings, self.live_client.cache, self.live_client.http
            )
        if isinstance(project.spec, LongFormSpec):
            from autonomous_studio.providers.longform import (
                OpenAILongFormDirector,
                OpenAILongFormResearch,
            )
            from autonomous_studio.providers.longform_visuals import (
                OpenAILongFormVisualPlanner,
                WikimediaCommonsDiscovery,
            )
            from autonomous_studio.providers.semantic_director import OpenAIFamilyDirector

            self.longform_research = OpenAILongFormResearch(self.live_client)
            self.longform_director = OpenAILongFormDirector(self.live_client)
            self.longform_visual_planner = OpenAILongFormVisualPlanner(self.live_client)
            self.longform_semantic_director = OpenAIFamilyDirector(self.live_client)
            self.authentic_media_discovery = WikimediaCommonsDiscovery(self.live_client.http)

    def create_topic(
        self,
        topic,
        *,
        duration=12,
        project_id=None,
        production_mode=None,
        expected_rank_count=None,
        image_budget=None,
        closed_captions=None,
        creation_settings=None,
    ):
        from autonomous_studio.config import Settings

        settings = self.settings or Settings.environment()
        if not topic.strip() or len(topic) > 2000:
            raise ValueError("topic must contain 1–2000 characters")
        mode = production_mode or ("full" if duration >= 45 else "prototype")
        provider = (creation_settings or {}).get("tts_provider", settings.tts_provider)
        if provider not in {"gemini", "openai"}:
            raise ValueError("TTS provider must be gemini or openai")
        brief = Brief(
            title=topic[:160],
            topic=topic,
            production_mode=mode,
            duration=duration,
            width=1080 if mode == "full" else 540,
            height=1920 if mode == "full" else 960,
            expected_rank_count=expected_rank_count,
            editorial_objective="Research and develop a sourced documentary excerpt.",
            hook="",
            thesis="",
            payoff="",
            research=ResearchBrief(),
            voice=VoiceSpec(
                provider=provider,
                voice_id=settings.longform_tts_voice if provider == "gemini" else settings.voice,
            ),
        )
        policy = settings.asset_policy() if settings.auto_assets else None
        if image_budget is not None:
            if policy is None:
                raise ValueError("image budget requires automatic assets")
            policy = type(policy).model_validate(
                {**policy.model_dump(mode="json"), "max_image_requests": image_budget}
            )
        creation_settings = {
            "youtube_category_id": settings.youtube_category_id,
            "youtube_category_source": "environment_default",
            **(creation_settings or {}),
            "tts_provider": provider,
            "voice_signature_schema": 3,
        }
        models = settings.model_ids()
        models["research_model"] = settings.shorts_research_model
        models["director_model"] = settings.shorts_creative_model
        return self.create(
            brief,
            pipeline="live",
            project_id=project_id,
            live_models=models,
            review_policy="preview",
            auto_assets=policy,
            closed_captions=(
                settings.closed_captions if closed_captions is None else closed_captions
            ),
            publication_clearance=True,
            script_approval=(production_mode == "full" or duration >= 45),
            creation_settings=creation_settings,
        )

    def create_longform_topic(
        self,
        topic,
        *,
        target_duration=210,
        project_id=None,
        image_budget=None,
        closed_captions=None,
        creation_settings=None,
    ):
        from autonomous_studio.config import Settings

        settings = self.settings or Settings.environment()
        if not topic.strip() or len(topic) > 2000:
            raise ValueError("topic must contain 1–2000 characters")
        if not 150 <= target_duration <= 240:
            raise ValueError("long-form pilot duration must be between 150 and 240 seconds")
        longform_provider = (creation_settings or {}).get("tts_provider") or (
            creation_settings or {}
        ).get("longform_tts_provider", settings.tts_provider)
        if longform_provider not in {"gemini", "openai"}:
            raise ValueError("long-form TTS provider must be gemini or openai")
        longform_voice = (creation_settings or {}).get(
            "longform_tts_voice",
            settings.longform_tts_voice if longform_provider == "gemini" else settings.voice,
        )
        brief = LongFormBrief(
            title=topic[:160],
            topic=topic,
            duration_policy=DurationPolicy(
                minimum_seconds=150,
                target_seconds=target_duration,
                maximum_seconds=240,
                expansion_maximum_seconds=600,
            ),
            editorial_objective=(
                "Research and develop a coherent, evidence-led horizontal documentary."
            ),
            viewer_promise=(
                "Give a new viewer enough context to understand the evidence and its meaning."
            ),
            central_question=f"What should a new viewer understand about {topic}?",
            research=ResearchBrief(),
            voice=VoiceSpec(provider=longform_provider, voice_id=longform_voice),
            engagement_ending=EngagementEnding(
                like_prompt="If this story sparked your curiosity, like the video.",
                share_prompt="Share it with someone who would enjoy it.",
            ),
        )
        creation_settings = {
            "youtube_category_id": settings.youtube_category_id,
            "youtube_category_source": "environment_default",
            "narration_linked_timeline": True,
            **(creation_settings or {}),
        }
        visual_policy = (
            LongFormVisualPolicy(maximum_generated_requests=image_budget)
            if image_budget is not None
            else LongFormVisualPolicy()
        )
        return self.create(
            brief,
            pipeline="live",
            project_id=project_id,
            live_models=settings.model_ids(),
            review_policy="staged",
            auto_assets=None,
            closed_captions=(False if closed_captions is None else closed_captions),
            publication_clearance=True,
            script_approval=True,
            creation_settings=creation_settings,
            longform_visual_policy=visual_policy,
            longform_creative_policy=LongFormCreativePolicy(),
        )

    def adopt_longform_runtime_ceiling(
        self, project_id: str, *, actor="local-operator"
    ) -> VideoProject:
        """Raise an existing long-form ceiling without changing its approved target or story."""
        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            if not isinstance(project.spec, LongFormSpec):
                raise ValueError("runtime ceiling adoption requires a long-form project")
            if project.state in (State.COMPLETED, State.REJECTED):
                raise ValueError("completed or rejected projects cannot change runtime policy")
            policy = project.spec.brief.duration_policy
            previous = policy.expansion_maximum_seconds
            if previous >= 600:
                return project
            policy.expansion_maximum_seconds = 600
            ceiling_failure = (
                project.state == State.LONGFORM_SCRIPTING
                and project.last_error_code == "provider_validation"
                and project.last_error is not None
                and "production ceiling" in project.last_error
                and "Estimated speech" in project.last_error
            )
            if ceiling_failure:
                project.last_error = project.last_error_code = None
            self.repo.save(
                project,
                "longform_runtime_ceiling_adopted",
                {
                    "actor": actor,
                    "version": project.version,
                    "previous": previous,
                    "current": 600,
                    "target_seconds": policy.target_seconds,
                    "resumed_ceiling_failure": ceiling_failure,
                },
            )
            return project

    def set_longform_image_budget(
        self, project_id: str, budget: int, *, actor="local-operator"
    ) -> VideoProject:
        """Update one long-form project's planning cap without altering its approved narrative."""
        if not 4 <= budget <= 30:
            raise ValueError("long-form image budget must be between 4 and 30")
        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            if not isinstance(project.spec, LongFormSpec):
                raise ValueError("long-form image budget requires a long-form project")
            old_policy = project.longform_visual_policy or LongFormVisualPolicy()
            previous = old_policy.maximum_generated_requests
            project.longform_visual_policy = old_policy.model_copy(
                update={"maximum_generated_requests": budget}
            )
            project.creation_settings["longform_image_budget"] = budget
            project.creation_settings["longform_image_budget_source"] = "project_revision"
            self.repo.save(
                project,
                "longform_image_budget_updated",
                {
                    "previous": previous,
                    "current": budget,
                    "actor": actor,
                    "version": project.version,
                },
            )
            return project

    def set_longform_creative_repair_limit(
        self, project_id: str, stage: str, limit: int, *, version: int, actor="local-operator"
    ) -> VideoProject:
        """Explicitly change a future-stage limit on a project using the new policy."""
        if stage not in CREATIVE_STAGES:
            raise ValueError(f"unknown creative stage: {stage}")
        if not 0 <= limit <= 3:
            raise ValueError("creative repair limit must be between 0 and 3")
        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            if project.version != version:
                raise ValueError("stale project version")
            if project.pipeline != "live" or not isinstance(project.spec, LongFormSpec):
                raise ValueError("creative repair policy requires a live long-form project")
            if project.longform_creative_policy is None:
                raise ValueError("legacy project needs a separate versioned policy adoption")
            if stage not in project.longform_creative_policy.repair_limits:
                raise ValueError("creative stage is unavailable in this project's saved policy")
            if project.state in (State.COMPLETED, State.REJECTED):
                raise ValueError("terminal project cannot change creative repair policy")
            previous = project.longform_creative_policy.repair_limits[stage]
            limits = dict(project.longform_creative_policy.repair_limits)
            limits[stage] = limit
            project.longform_creative_policy = LongFormCreativePolicy(
                schema_version=project.longform_creative_policy.schema_version,
                repair_limits=limits,
            )
            self.repo.save(
                project,
                "longform_creative_repair_limit_updated",
                {
                    "actor": actor,
                    "version": version,
                    "stage": stage,
                    "previous": previous,
                    "current": limit,
                },
            )
            return project

    def adopt_shorts_stage_models(self, project_id: str, *, actor="local-operator"):
        """Pin shorts text roles without changing approved story or media models."""
        from autonomous_studio.config import Settings

        selected = self.settings or Settings.environment()
        after = {
            "research_model": selected.shorts_research_model,
            "director_model": selected.shorts_creative_model,
            "shorts_repair_model": selected.shorts_repair_model,
        }
        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            if project.pipeline != "live" or isinstance(project.spec, LongFormSpec):
                raise ValueError("shorts model adoption requires a live short project")
            if project.state in (State.COMPLETED, State.REJECTED):
                raise ValueError("completed or rejected projects cannot change model policy")
            before = {key: project.live_models.get(key) for key in after}
            if (
                project.prepared_voices
                and project.creation_settings.get("voice_signature_schema") not in (2, 3)
            ):
                project.creation_settings.setdefault(
                    "voice_signature_legacy_models", dict(project.live_models)
                )
            project.live_models.update(after)
            self.repo.save(
                project,
                "shorts_stage_models_adopted",
                {
                    "actor": actor,
                    "version": project.version,
                    "previous": before,
                    "current": after,
                },
            )
            return project

    def adopt_longform_stage_models(self, project_id: str, *, actor="local-operator"):
        """Apply configured text-stage models without rebuying saved semantic chapters."""
        from autonomous_studio.config import Settings

        selected = self.settings or Settings.environment()
        keys = (
            "research_model",
            "story_model",
            "continuity_model",
            "visual_plan_model",
            "visual_repair_model",
            "semantic_layout_model",
        )
        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            if project.pipeline != "live" or not isinstance(project.spec, LongFormSpec):
                raise ValueError("stage model adoption requires a live long-form project")
            if project.state in (State.COMPLETED, State.REJECTED):
                raise ValueError("completed or rejected projects cannot change model policy")
            for chapter_id in project.longform_semantic_decisions:
                project.longform_semantic_models.setdefault(
                    chapter_id, project.live_models.get("director_model", "mock-director")
                )
            before = {key: project.live_models.get(key) for key in keys}
            after = {key: getattr(selected, key) for key in keys}
            project.live_models.update(after)
            self.repo.save(
                project,
                "longform_stage_models_adopted",
                {
                    "actor": actor,
                    "version": project.version,
                    "previous": before,
                    "current": after,
                    "pinned_semantic_chapters": sorted(project.longform_semantic_decisions),
                },
            )
            return project

    def warnings_for(self, project):
        warnings = research_warnings(project.spec.brief)
        if project.pipeline == "live":
            warnings[0] = (
                "Live web research with tool-observed citations; human fact-check required."
            )
            if isinstance(project.spec, LongFormSpec):
                warnings.append(
                    "AI-generated long-form research and narration; human fact-check required."
                )
            else:
                warnings += [
                    "AI-generated speech and illustration; human fact-check required."
                    if project.spec.presentation
                    else "AI-generated speech; visuals and music remain procedural placeholders."
                ]
        else:
            warnings += ["Mock voice is timing tones, not speech."]
        warnings.extend(
            f"visual {a.id}: UNKNOWN — DO NOT PUBLISH"
            for a in project.current_artifacts("visual_asset")
            if a.provenance.license_category == "unknown"
        )
        warnings.extend(
            f"archival visual {a.id}: verify identity, context, privacy and personality rights"
            for a in project.current_artifacts("visual_asset")
            if a.provenance.origin == "library"
        )
        return warnings

    def record_trace(self, project, output, role):
        if self.live_client and self.live_client.trace:
            records = (
                self.live_client.trace[getattr(self, "_stage_trace_start", 0):]
                if isinstance(project.spec, LongFormSpec)
                and project.longform_creative_policy is not None
                else self.live_client.trace
            )
            if not records:
                return
            path = output / f"{role}-provider-responses.json"
            write_json(path, records)
            artifact = self.repo.artifact(
                project,
                path,
                "provider_usage",
                provider="openai",
                model=records[-1]["requested_model"],
            )
            artifact.cost = None
            project.artifacts.append(artifact)
            if isinstance(project.spec, LongFormSpec) and project.longform_creative_policy:
                prices = {}
                rate_card = self.live_client.settings.rate_card
                if rate_card and Path(rate_card).is_file():
                    prices = json.loads(Path(rate_card).read_text(encoding="utf-8")).get(
                        "models", {}
                    )

                def request_usage(item):
                    usage = item["response"].get("usage") or {}
                    model = item["requested_model"]
                    rates = prices.get(model)
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    cost = (
                        round(
                            (input_tokens * rates["input_per_million"]
                             + output_tokens * rates["output_per_million"]) / 1_000_000,
                            6,
                        )
                        if rates and usage
                        else None
                    )
                    return {
                        "model": model,
                        "chapter_id": item.get("chapter_id"),
                        "beat_id": item.get("beat_id"),
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_hit": item.get("cache_hit", False),
                        "estimated_cost_usd": cost,
                    }

                self._json(
                    project,
                    output,
                    f"{role}-usage-summary.json",
                    {
                        "stage": role,
                        "version": project.version,
                        "requests": [request_usage(item) for item in records],
                    },
                    "longform_stage_usage",
                )
            self._stage_trace_start = len(self.live_client.trace)

    def create(
        self,
        brief: ProjectBrief,
        *,
        pipeline="mock",
        project_id=None,
        live_models=None,
        review_policy="staged",
        auto_assets=None,
        closed_captions=None,
        publication_clearance=False,
        script_approval=False,
        creation_settings=None,
        longform_visual_policy=None,
        longform_creative_policy=None,
    ) -> VideoProject:
        if pipeline == "mock" and isinstance(brief, Brief) and not brief.research.subjects:
            raise ValueError("mock briefs require researched subjects; use topic for live research")
        if pipeline == "mock" and isinstance(brief, LongFormBrief) and not brief.research.claims:
            raise ValueError("mock long-form briefs require researched claims")
        if (
            pipeline == "mock"
            and isinstance(self.voice, MockAudio)
            and brief.voice.provider != "mock"
        ):
            raise ValueError(
                "default CLI supports voice.provider=mock; inject a live adapter in Python"
            )
        if closed_captions is None:
            closed_captions = not isinstance(brief, LongFormBrief)
        if isinstance(brief, LongFormBrief) and auto_assets is not None:
            raise ValueError("long-form automatic assets begin in LF v0.3")
        creation_settings = {
            "format": brief.format,
            "format_profile": (
                brief.profile
                if isinstance(brief, LongFormBrief)
                else short_profile_id(brief.production_mode)
            ),
            **(creation_settings or {}),
        }
        spec = (
            LongFormSpec(
                story_contract_version=2 if pipeline == "live" else 1,
                brief=brief,
                narrative={
                    "viewer_promise": brief.viewer_promise,
                    "central_question": brief.central_question,
                },
                engagement_ending=brief.engagement_ending,
                closed_captions=closed_captions,
            )
            if isinstance(brief, LongFormBrief)
            else VideoSpec(brief=brief, closed_captions=closed_captions)
        )
        project = VideoProject(
            id=project_id or uuid4().hex[:12],
            spec=spec,
            pipeline=pipeline,
            live_models=live_models or {},
            review_policy=review_policy,
            auto_assets=auto_assets,
            publication_clearance_enabled=publication_clearance,
            script_approval_enabled=script_approval,
            creation_settings=creation_settings,
            longform_visual_policy=(
                longform_visual_policy if isinstance(brief, LongFormBrief) else None
            ),
            longform_creative_policy=(
                longform_creative_policy if isinstance(brief, LongFormBrief) else None
            ),
        )
        project.warnings = self.warnings_for(project)
        output = self.repo.attempt_dir(project, "brief")
        path = output / "brief.json"
        write_json(path, brief.model_dump(mode="json"))
        project.artifacts.append(self.repo.artifact(project, path, "brief"))
        self.repo.create(project)
        return project

    def _transition(self, project, state):
        old = project.state
        project.state = state
        project.last_error = None
        project.last_error_code = None
        self.repo.save(
            project, "transition", {"from": old, "to": state, "version": project.version}
        )

    def _json(self, project, directory, name, value, role):
        path = directory / name
        write_json(path, value)
        project.artifacts.append(self.repo.artifact(project, path, role))

    async def _request(self, project, gate, output):
        if project.review_policy == "preview" and gate not in (
            "narration",
            "script",
            "prototype",
            "publication",
        ):
            raise ValueError("preview policy cannot request intermediate approvals")
        if project.review_policy == "preview" and gate == "prototype":
            import json

            required = {"prototype", "qa", "narration_validation"}
            if project.spec.brief.production_mode == "full":
                required.add("scope_validation")
            if not required <= {a.role for a in project.current_artifacts()}:
                raise ValueError("validated video, QA and narration report required before review")
            qa = json.loads(
                self.repo.verify(project, project.current_artifacts("qa")[-1]).read_text(
                    encoding="utf-8"
                )
            )
            timing = json.loads(
                self.repo.verify(
                    project, project.current_artifacts("narration_validation")[-1]
                ).read_text(encoding="utf-8")
            )
            scope = {}
            if project.spec.brief.production_mode == "full":
                scope = json.loads(
                    self.repo.verify(
                        project, project.current_artifacts("scope_validation")[-1]
                    ).read_text(encoding="utf-8")
                )
            video = qa.get("video", {})
            brief = project.spec.brief
            if (
                not timing.get("passed")
                or timing.get("version") != project.version
                or (
                    brief.production_mode == "full"
                    and (
                        not scope.get("passed")
                        or scope.get("version") != project.version
                        or scope.get("production_mode") != "full"
                        or scope.get("rank_order")
                        != [s.rank for s in project.spec.shots if s.kind == "rank_reveal"]
                    )
                )
                or qa.get("clipped_samples") != 0
                or not video.get("has_audio")
                or (video.get("width"), video.get("height")) != (brief.width, brief.height)
                or abs(video.get("duration", 0) - brief.duration) > 0.1
            ):
                raise ValueError("preview QA failed; no approval request was sent")
            if project.spec.presentation and not qa.get("presentation", {}).get("layout_passed"):
                raise ValueError("editorial layout QA required before preview approval")
            if project.auto_assets:
                v06_assets = any(
                    change.startswith("v0.6 prepared") for change in project.production_changes
                )
                v07_assets = any(
                    change.startswith("v0.7 prepared") for change in project.production_changes
                )
                validations = project.current_artifacts("asset_validation")
                assets_qa = (
                    json.loads(
                        self.repo.verify(project, validations[-1]).read_text(encoding="utf-8")
                    )
                    if validations
                    else {}
                )
                if (
                    not assets_qa.get("passed")
                    or assets_qa.get("version") != project.version
                    or not project.spec.presentation
                    or not project.generated_visuals
                    or (
                        (v06_assets or v07_assets)
                        and (
                            not project.current_artifacts("publication_description")
                            or not project.current_artifacts("thumbnail_gallery")
                            or not project.current_artifacts("thumbnail_candidate")
                            or (
                                v07_assets
                                and not {
                                    "youtube_title",
                                    "youtube_metadata",
                                    "source_manifest",
                                    "rights_manifest",
                                }
                                <= {a.role for a in project.current_artifacts()}
                            )
                        )
                    )
                ):
                    raise ValueError("automatic asset validation required before approval")
        approval = Approval(
            id=uuid4().hex,
            project_id=project.id,
            gate=gate,
            version=project.version,
            artifact_ids=[a.id for a in project.current_artifacts()],
        )
        request = await self.hitl.send(review_message(project, approval), output)
        project.artifacts.append(self.repo.artifact(project, request, f"review_{gate}"))
        project.approvals.append(approval)

    def _script_lock_payload(self, project):
        timing = project.current_artifacts("narration_validation")
        if not timing:
            raise ValueError("script approval requires current narration validation")
        payload = {
            "schema_version": 1,
            "project_id": project.id,
            "version": project.version,
            "research": project.spec.brief.research.model_dump(mode="json"),
            "hook": project.spec.hook,
            "narration_script": project.spec.narration_script,
            "voice_segments": [
                {
                    "shot_id": shot.id,
                    "text": segment.spoken_text,
                    "offset": segment.offset,
                    "duration": segment.estimated_duration,
                }
                for shot in project.spec.shots
                for segment in shot.voice_segments
            ],
            "prepared_voices": [
                {
                    "shot_id": voice.shot_id,
                    "segment_index": voice.segment_index,
                    "signature": voice.signature,
                    "duration": voice.asset.duration,
                    "sha256": voice.asset.artifact.sha256,
                }
                for voice in project.prepared_voices
            ],
            # Pydantic rehydrates Brief.duration as float. Hash its canonical form so
            # an approved 60-second script remains locked after a process restart.
            "timeline_duration": float(project.spec.brief.duration),
            "narration_validation_sha256": timing[-1].sha256,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        payload["lock_sha256"] = hashlib.sha256(canonical).hexdigest()
        return payload

    async def _request_script_approval(self, project, output):
        self._json(
            project,
            output,
            "script-lock.json",
            self._script_lock_payload(project),
            "script_lock",
        )
        await self._request(project, "script", output)
        self._transition(project, State.SCRIPT_APPROVAL)

    def _verify_script_approval(self, project):
        if not project.script_approval_enabled:
            return
        locks = project.current_artifacts("script_lock")
        approved = next(
            (
                approval
                for approval in reversed(project.approvals)
                if approval.version == project.version
                and approval.gate == "script"
                and approval.decision == "approve"
            ),
            None,
        )
        if not locks or approved is None or locks[-1].id not in approved.artifact_ids:
            raise ValueError("automatic assets require an approved script lock")
        path = self.repo.verify(project, locks[-1])
        stored = json.loads(path.read_text(encoding="utf-8"))
        current = self._script_lock_payload(project)
        stored_content = {key: value for key, value in stored.items() if key != "lock_sha256"}
        current_content = {key: value for key, value in current.items() if key != "lock_sha256"}
        if stored_content != current_content:
            raise ValueError("approved script lock no longer matches narration")

    async def run(self, project_id: str) -> VideoProject:
        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            terminal_states = {
                State.COMPLETED,
                State.REJECTED,
                State.LONGFORM_MASTER_READY,
            }
            if project.pending() or project.state in terminal_states:
                return project
            stage_trace_start = 0
            try:
                if project.pipeline == "live":
                    self.configure_live(project)
                if project.last_error:
                    project.last_error = project.last_error_code = None
                    self.repo.save(project, "stage_resumed", {"state": project.state})
                while not project.pending() and project.state not in terminal_states:
                    # Verify committed dependencies before every resumed stage.
                    for artifact in project.current_artifacts():
                        self.repo.verify(project, artifact)
                    stage_trace_start = len(self.live_client.trace) if self.live_client else 0
                    self._stage_trace_start = stage_trace_start
                    if isinstance(project.spec, LongFormSpec):
                        await self._step_longform(project)
                    else:
                        await self._step(project)
            except Exception as exc:
                # Drop uncommitted changes/artifact references, retain last successful checkpoint.
                stored = self.repo.load(project_id)
                stored.last_error = str(exc)
                stored.last_error_code = (
                    "narration_timing"
                    if isinstance(exc, NarrationTimingError)
                    else (
                        "provider_validation"
                        if isinstance(exc, ProviderValidationError)
                        else (
                            "provider_incomplete"
                            if isinstance(exc, IncompleteResponseError)
                            else (
                                "provider_timeout"
                                if isinstance(exc, ProviderTimeoutError)
                                else (
                                    "asset_planning"
                                    if "Asset planning failed after two attempts" in str(exc)
                                    else (
                                        "longform_visual_planning"
                                        if "Long-form visual planning failed after two attempts"
                                        in str(exc)
                                        else None
                                    )
                                )
                            )
                        )
                    )
                )
                if (
                    isinstance(stored.spec, LongFormSpec)
                    and "above the saved" in str(exc)
                    and "pilot ceiling" in str(exc)
                ):
                    stored.last_error_code = "longform_narration_ceiling"
                if (
                    isinstance(stored.spec, LongFormSpec)
                    and self.live_client is not None
                    and len(self.live_client.trace) > stage_trace_start
                ):
                    stage_records = self.live_client.trace[stage_trace_start:]
                    last = stage_records[-1]
                    response = last.get("response") or {}
                    diagnostic_output = self.repo.attempt_dir(stored, "failure-diagnostic")
                    self._json(
                        stored,
                        diagnostic_output,
                        "provider-failure.json",
                        {
                            "schema_version": 1,
                            "stage": stored.state,
                            "version": stored.version,
                            "error": str(exc),
                            "requested_model": last.get("requested_model"),
                            "cache_path": last.get("cache_path"),
                            "cache_hit": last.get("cache_hit"),
                            "response_id": response.get("id"),
                            "usage": response.get("usage"),
                            "response": response,
                            "requests": [
                                {
                                    "model": item.get("requested_model"),
                                    "chapter_id": item.get("chapter_id"),
                                    "beat_id": item.get("beat_id"),
                                    "cache_path": item.get("cache_path"),
                                    "cache_hit": item.get("cache_hit", False),
                                    "response": item.get("response"),
                                }
                                for item in stage_records
                            ],
                        },
                        "longform_provider_failure",
                    )
                self.repo.save(stored, "stage_failed", {"state": stored.state, "error": str(exc)})
                raise
            return project

    async def _step_longform(self, project):
        spec = project.spec
        if not isinstance(spec, LongFormSpec):
            raise ValueError("long-form workflow requires LongFormSpec")
        brief = spec.brief
        output = self.repo.attempt_dir(project, project.state.value.lower())

        if project.state == State.CREATED:
            self._transition(project, State.LONGFORM_FOUNDATION_READY)
            return

        if project.state == State.LONGFORM_FOUNDATION_READY:
            if project.longform_research_plan is None:
                plan = await self.longform_research.plan(brief)
                project.longform_research_plan = plan
                self._json(
                    project,
                    output,
                    "research-plan.json",
                    plan.model_dump(mode="json"),
                    "longform_research_plan",
                )
                self.record_trace(project, output, "longform-research-plan")
                self.repo.save(
                    project,
                    "longform_research_planned",
                    {
                        "version": project.version,
                        "questions": len(plan.questions),
                        "minimum_distinct_sources": plan.minimum_distinct_sources,
                    },
                )
            plan = project.longform_research_plan
            research = await self.longform_research.research(brief, plan)
            brief.research = research
            project.warnings = self.warnings_for(project)
            self.record_trace(project, output, "longform-research")
            self._json(
                project,
                output,
                "research.json",
                research.model_dump(mode="json"),
                "research",
            )
            findings = output / "findings.md"
            findings.write_text(
                "# Long-form research evidence\n\n"
                + "\n\n".join(
                    f"- {claim.text}\n  Status: {claim.status}. {claim.caveat}"
                    for claim in research.claims
                ),
                encoding="utf-8",
            )
            project.artifacts.append(self.repo.artifact(project, findings, "findings"))
            self._json(
                project,
                output,
                "research-coverage.json",
                {
                    "passed": bool(research.claims and research.sources),
                    "questions": [q.id for q in plan.questions],
                    "claim_count": len(research.claims),
                    "source_count": len(research.sources),
                    "version": project.version,
                },
                "longform_research_coverage",
            )
            self._transition(project, State.LONGFORM_RESEARCHED)
            return

        if project.state == State.LONGFORM_RESEARCHED:
            from autonomous_studio.domain.longform_feedback import relevant_longform_feedback

            feedback = relevant_longform_feedback(project)
            treatment = await self.longform_director.develop_treatment(
                brief, project.longform_research_plan, feedback,
                story_contract_version=spec.story_contract_version,
            )
            if (
                project.longform_creative_policy is not None
                and project.longform_creative_policy.schema_version >= 2
            ):
                for chapter in treatment.chapters:
                    chapter.spoken_unit_version = 2
            treatment.closed_captions = spec.closed_captions
            project.spec = treatment
            self.record_trace(project, output, "longform-treatment")
            self._json(
                project,
                output,
                "treatment.json",
                treatment.model_dump(mode="json"),
                "longform_treatment",
            )
            outline = output / "treatment-review.md"
            image_budget = (
                project.longform_visual_policy or LongFormVisualPolicy()
            ).maximum_generated_requests
            outline.write_text(
                self._longform_treatment_text(treatment, image_budget),
                encoding="utf-8",
            )
            project.artifacts.append(self.repo.artifact(project, outline, "treatment_review"))
            await self._request(project, "treatment", output)
            self._transition(project, State.TREATMENT_APPROVAL)
            return

        if project.state == State.LONGFORM_SCRIPTING:
            from autonomous_studio.domain.longform_feedback import relevant_longform_feedback

            feedback = relevant_longform_feedback(project)
            scripted, report = await self.longform_director.write_script(spec, feedback)
            if (
                project.longform_creative_policy is not None
                and project.longform_creative_policy.schema_version >= 2
            ):
                for chapter in scripted.chapters:
                    chapter.spoken_unit_version = 2
            scripted.closed_captions = spec.closed_captions
            project.spec = scripted
            project.longform_continuity = report
            self.record_trace(project, output, "longform-script")
            self._json(
                project,
                output,
                "longform-script.json",
                scripted.model_dump(mode="json"),
                "longform_script",
            )
            script_path = output / "script.txt"
            script_path.write_text(self._longform_script_text(scripted), encoding="utf-8")
            project.artifacts.append(self.repo.artifact(project, script_path, "review_script"))
            self._json(
                project,
                output,
                "continuity-report.json",
                report.model_dump(mode="json"),
                "longform_continuity",
            )
            await self._request(project, "script", output)
            self._transition(project, State.SCRIPT_APPROVAL)
            return

        if project.state == State.LONGFORM_NARRATIVE_READY:
            if project.longform_visual_policy is None:
                project.longform_visual_policy = LongFormVisualPolicy()
                self.repo.save(
                    project,
                    "longform_visual_policy_initialized",
                    project.longform_visual_policy.model_dump(mode="json"),
                )
            self._transition(project, State.LONGFORM_VISUAL_PLANNING)
            return

        if project.state == State.LONGFORM_VISUAL_PLANNING:
            from autonomous_studio.workflow.longform_visuals import (
                prepare_longform_visual_plan,
            )

            await prepare_longform_visual_plan(self, project, output)
            await self._request(project, "visual_plan", output)
            self._transition(project, State.VISUAL_PLAN_APPROVAL)
            return

        if project.state == State.LONGFORM_VISUAL_PLAN_READY:
            self._transition(project, State.LONGFORM_AUDIO_PREPARING)
            return

        if project.state == State.LONGFORM_AUDIO_PREPARING:
            from autonomous_studio.workflow.longform_production import (
                RuntimeExtensionRequired,
                prepare_longform_audio,
            )

            try:
                await prepare_longform_audio(self, project)
            except RuntimeExtensionRequired as exc:
                project.longform_runtime_proposal = exc.proposal
                self._json(
                    project,
                    output,
                    "runtime-proposal.json",
                    exc.proposal.model_dump(mode="json"),
                    "longform_runtime_proposal",
                )
                await self._request(project, "runtime", output)
                self._transition(project, State.LONGFORM_RUNTIME_APPROVAL)
                return
            self._transition(project, State.LONGFORM_ASSETS_PREPARING)
            return

        if project.state == State.LONGFORM_ASSETS_PREPARING:
            from autonomous_studio.workflow.longform_production import (
                prepare_longform_visual_assets,
            )

            await prepare_longform_visual_assets(self, project)
            self._transition(project, State.LONGFORM_SEMANTIC_PLANNING)
            return

        if project.state == State.LONGFORM_SEMANTIC_PLANNING:
            from autonomous_studio.workflow.longform_production import (
                prepare_longform_semantic_decisions,
            )

            await prepare_longform_semantic_decisions(self, project)
            self._transition(project, State.LONGFORM_CHAPTER_RENDERING)
            return

        if project.state == State.LONGFORM_CHAPTER_RENDERING:
            from autonomous_studio.workflow.longform_production import render_longform_chapters

            render_longform_chapters(self, project)
            self._transition(project, State.LONGFORM_REVIEW_PREPARING)
            return

        if project.state == State.LONGFORM_REVIEW_PREPARING:
            from autonomous_studio.workflow.longform_production import prepare_longform_review

            await prepare_longform_review(self, project)
            await self._request(project, "prototype", output)
            self._transition(project, State.LONGFORM_REVIEW_APPROVAL)
            return

        if project.state == State.PUBLICATION_CLEARANCE:
            from autonomous_studio.config import Settings
            from autonomous_studio.workflow.longform_publication import prepare_longform_clearance

            settings = self.settings or Settings.environment()
            await prepare_longform_clearance(self, project, output, rate_card=settings.rate_card)
            await self._request(project, "publication", output)
            self.repo.save(project, "publication_clearance_requested", {
                "version": project.version,
                "approval_id": project.pending().id,
                "format": "youtube_longform",
            })
            return

        raise ValueError(f"no long-form handler for state {project.state}")

    @staticmethod
    def _longform_treatment_text(spec: LongFormSpec, image_budget: int | None = None) -> str:
        chapters = "\n\n".join(
            (
                f"## {chapter.order}. {chapter.title} ({chapter.target_duration:.0f}s)\n"
                f"Purpose: {chapter.purpose}\n"
                f"Question: {chapter.question}\n"
                f"Reveal: {chapter.reveal}\n"
                f"Transition: {chapter.transition or '(conclusion)'}\n"
                + (
                    f"Essential claims: {', '.join(chapter.essential_claim_ids)}"
                    if spec.story_contract_version == 2
                    else f"Claims: {', '.join(chapter.evidence_claim_ids)}"
                )
            )
            for chapter in spec.chapters
        )
        ending = spec.engagement_ending or spec.brief.engagement_ending
        return (
            "# Long-form treatment\n\n"
            + (
                f"Generated-image allowance: up to {image_budget}; use only where helpful.\n\n"
                if image_budget is not None
                else ""
            )
            + f"Chosen hook: {spec.narrative.hook}\n\n"
            f"Viewer promise: {spec.narrative.viewer_promise}\n\n"
            + (
                f"Chosen story question: {spec.narrative.central_question}\n"
                f"Why this angle: {spec.story_choice.evidence_rationale}\n"
                f"Supporting claims: {', '.join(spec.story_choice.supporting_claim_ids)}\n"
                + (
                    f"Evidence limit: {spec.story_choice.limitation}\n"
                    if spec.story_choice.limitation else ""
                )
                + "\n"
                if spec.story_choice else ""
            )
            + (
                f"Central tension: {spec.story_design.central_tension}\n\n"
                f"Payoff: {spec.story_design.payoff}\n\n"
                if spec.story_contract_version == 2 and spec.story_design else ""
            )
            + f"Thesis: {spec.narrative.thesis}\n\n"
            f"Treatment: {spec.treatment_summary}\n\n"
            + chapters
            + "\n\n## Ending\n"
            + f"Conclusion: {spec.narrative.conclusion}\n"
            + f"Comment question: {ending.comment_question}\n"
            + f"Subscribe: {ending.subscribe_reason}\n"
            + f"Like/share: {ending.like_and_share_prompt}\n"
        )

    @staticmethod
    def _longform_script_text(spec: LongFormSpec) -> str:
        sections = []
        for chapter in spec.chapters:
            body = "\n\n".join(block.text for block in chapter.narration_blocks)
            sections.append(f"# {chapter.order}. {chapter.title}\n\n{body}")
        return "\n\n".join(sections) + "\n"

    async def _step(self, project):
        brief = project.spec.brief
        output = self.repo.attempt_dir(project, project.state.value.lower())
        if project.state == State.CREATED:
            research = await self.research.research(brief)
            brief.research = research
            project.warnings = self.warnings_for(project)
            self.record_trace(project, output, "research")
            repairs = getattr(self.research, "repairs", [])
            if repairs:
                self._json(project, output, "research-repairs.json", repairs, "research_repair")
            self._json(
                project, output, "research.json", research.model_dump(mode="json"), "research"
            )
            findings = output / "findings.md"
            findings.write_text(
                "# Research evidence\n\n"
                + "\n\n".join(
                    f"- {c.text}\n  Status: {c.status}. {c.caveat}" for c in research.claims
                ),
                encoding="utf-8",
            )
            project.artifacts.append(self.repo.artifact(project, findings, "findings"))
            self._transition(project, State.RESEARCHED)
        elif project.state == State.RESEARCHED:
            feedback = [
                (
                    "NARRATION TRIM FEEDBACK: Preserve the research and requested story scope, "
                    "but shorten the current narration according to this human direction: "
                    + item.message
                    if item.gate == "narration"
                    else item.message
                )
                for item in project.feedback
            ]
            if project.short_revision_script and feedback:
                rejected_script = any(
                    approval.id == project.feedback[-1].approval_id
                    and approval.decision == "reject"
                    for approval in project.approvals
                )
                if rejected_script:
                    # A rejected script is not an accepted creative baseline.
                    feedback = [feedback[-1]]
                else:
                    # The previous draft is context, not an instruction to preserve
                    # choices the latest human feedback asks us to replace.
                    feedback = [
                        "Previous script for context only; preserve only what the latest "
                        "feedback requests: " + project.short_revision_script,
                        feedback[-1],
                    ]
            closed_captions = project.spec.closed_captions
            project.spec = await self.director.develop(brief, feedback)
            project.spec.closed_captions = closed_captions
            validate_story_scope(project.spec)
            self.record_trace(project, output, "director")
            self._json(project, output, "story.json", project.spec.model_dump(mode="json"), "story")
            self._json(
                project,
                output,
                "shot-type-review.json",
                [item.model_dump(mode="json") for item in project.spec.shot_type_reviews],
                "shot_type_review",
            )
            self._json(
                project,
                output,
                "shot-type-reasoning.json",
                [
                    {
                        "shot_id": shot.id,
                        "kind": shot.kind,
                        "shot_type_reasoning": shot.shot_type_reasoning,
                    }
                    for shot in project.spec.shots
                ],
                "shot_type_reasoning",
            )
            self._json(
                project,
                output,
                "scope-validation.json",
                {
                    "passed": True,
                    "production_mode": project.spec.brief.production_mode,
                    "duration": project.spec.brief.duration,
                    "shots": len(project.spec.shots),
                    "expected_rank_count": project.spec.brief.expected_rank_count,
                    "rank_order": [
                        shot.rank for shot in project.spec.shots if shot.kind == "rank_reveal"
                    ],
                    "version": project.version,
                },
                "scope_validation",
            )
            voiceover = output / "voiceover.txt"
            voiceover.write_text(
                "\n\n".join(
                    f"[{shot.id} / {shot.start:.3f}s] {segment.spoken_text}"
                    for shot in project.spec.shots
                    for segment in shot.voice_segments
                ),
                encoding="utf-8",
            )
            project.artifacts.append(self.repo.artifact(project, voiceover, "voiceover_script"))
            self._json(
                project,
                output,
                "feedback-context.json",
                [f.model_dump(mode="json") for f in project.feedback],
                "feedback_context",
            )
            if project.review_policy == "preview":
                self._transition(project, State.NARRATION_PREPARING)
            else:
                await self._request(project, "story", output)
                self._transition(project, State.STORY_APPROVAL)
        elif project.state == State.STORYBOARDED:
            self._json(
                project,
                output,
                "storyboard.json",
                [s.model_dump(mode="json") for s in project.spec.shots],
                "storyboard",
            )
            for shot in project.spec.shots:
                artifact = await self.images.generate(project, shot, output / f"{shot.id}.png")
                project.artifacts.append(artifact)
            if project.review_policy == "preview":
                self._transition(project, State.NARRATION_PREPARING)
            else:
                await self._request(project, "hero", output)
                self._transition(project, State.HERO_APPROVAL)
        elif project.state == State.ASSETS_PREPARING:
            from autonomous_studio.workflow.assets import prepare_assets

            self._verify_script_approval(project)
            await prepare_assets(self, project, output)
            self._transition(project, State.AUDIO_ASSEMBLING)
        elif project.state in (
            State.HERO_APPROVAL,
            State.NARRATION_PREPARING,
            State.AUDIO_ASSEMBLING,
        ):
            if not project.spec.audio_assets:
                if project.state != State.AUDIO_ASSEMBLING and (
                    project.pipeline == "live" or project.review_policy == "preview"
                ):
                    # Finish and checkpoint all approved speech before deciding on one repair.
                    for shot in project.spec.shots:
                        for i, voice in enumerate(
                            sorted(shot.voice_segments, key=lambda v: v.offset)
                        ):
                            await self._voice_asset(project, shot, i, voice, output)
                    from autonomous_studio.audio.timing import SHORT_REVIEW_MAX_SECONDS

                    proposed, report, mismatch = measured_plan(
                        project, limit_seconds=project.short_runtime_approved_seconds
                    )
                    if mismatch:
                        if proposed is None and brief.production_mode == "full":
                            extended, _, _ = measured_plan(
                                project, limit_seconds=SHORT_REVIEW_MAX_SECONDS
                            )
                            if extended is not None and project.review_policy == "preview":
                                await self._request_narration_review(
                                    project,
                                    output,
                                    report,
                                    proposal_seconds=extended.brief.duration,
                                )
                                return
                        if (
                            proposed is None
                            and project.review_policy == "preview"
                            and project.timing_rewrite_attempts >= MAX_TIMING_REPAIRS
                        ):
                            await self._request_narration_review(project, output, report)
                            return
                        while True:
                            try:
                                await self._propose_timing_repair(project, proposed, report)
                                break
                            except NarrationTimingError:
                                if (
                                    project.review_policy == "preview"
                                    and project.spec.presentation is None
                                    and project.timing_rewrite_attempts >= MAX_TIMING_REPAIRS
                                ):
                                    await self._request_narration_review(project, output, report)
                                    return
                                if (
                                    project.review_policy != "preview"
                                    or project.spec.presentation is not None
                                    or proposed is not None
                                    or project.timing_rewrite_attempts >= MAX_TIMING_REPAIRS
                                ):
                                    raise
                        return
                    self._json(
                        project,
                        output,
                        "narration-validation.json",
                        {
                            "passed": True,
                            "measurements": report,
                            "duration": brief.duration,
                            "version": project.version,
                            "rewrite_attempts": project.timing_rewrite_attempts,
                            "changes": project.production_changes,
                        },
                        "narration_validation",
                    )
                    if (
                        project.script_approval_enabled
                        and brief.production_mode == "full"
                        and project.review_policy == "preview"
                    ):
                        await self._request_script_approval(project, output)
                        return
                    if project.auto_assets:
                        self._transition(project, State.ASSETS_PREPARING)
                        return
                for shot in project.spec.shots:
                    actual_voice_end = 0.0
                    for i, voice in enumerate(sorted(shot.voice_segments, key=lambda v: v.offset)):
                        asset = await self._voice_asset(project, shot, i, voice, output)
                        if (
                            abs(asset.start - shot.start - voice.offset) > 0.001
                            or voice.offset < actual_voice_end - 0.001
                            or asset.duration + voice.offset + voice.pause_after
                            > shot.duration + 0.001
                        ):
                            raise NarrationTimingError(
                                f"synthesized narration exceeds its shot {shot.id}; rewrite it"
                            )
                        actual_voice_end = voice.offset + asset.duration + voice.pause_after
                        project.spec.audio_assets.append(asset)
                    for i, cue in enumerate(shot.sound_cues):
                        asset = await self.sfx.create(
                            project, cue, shot.start + cue.offset, output / f"sfx-{shot.id}-{i}.wav"
                        )
                        project.spec.audio_assets.append(asset)
                project.spec.audio_assets.append(
                    await self._music_provider(brief).create_or_select(
                        project, brief.music, output / "music.wav"
                    )
                )
                if brief.ambience:
                    project.spec.audio_assets.append(
                        await self.ambience.ambience(project, output / "ambience.wav")
                    )
                known = {a.id for a in project.artifacts}
                project.artifacts.extend(
                    a.artifact for a in project.spec.audio_assets if a.artifact.id not in known
                )
                project.warnings.extend(audio_warnings(project.spec.audio_assets))
                self._json(
                    project,
                    output,
                    "audio.json",
                    [a.model_dump(mode="json") for a in project.spec.audio_assets],
                    "audio",
                )
            self._transition(project, State.AUDIO_PREPARED)
        elif project.state == State.AUDIO_PREPARED:
            result = await self.renderer.render(project, output)
            if result.kind == "failure":
                raise RuntimeError(f"{result.provider}: {result.message}")
            project.artifacts.extend(result.artifacts)
            project.warnings.extend(result.warnings)
            self._transition(project, State.PROTOTYPE_RENDERED)
        elif project.state == State.PROTOTYPE_RENDERED:
            await self._request(project, "prototype", output)
            self._transition(project, State.PROTOTYPE_APPROVAL)
        elif project.state == State.PUBLICATION_CLEARANCE:
            from autonomous_studio.config import Settings
            from autonomous_studio.workflow.publication import prepare_clearance

            settings = self.settings or Settings.environment()
            prepare_clearance(self.repo, project, output, rate_card=settings.rate_card)
            await self._request(project, "publication", output)
            self.repo.save(
                project,
                "publication_clearance_requested",
                {"version": project.version, "approval_id": project.pending().id},
            )
        else:
            raise ValueError(f"no handler for state {project.state}")

    def _music_provider(self, brief):
        if brief.music.source_type == "library":
            from autonomous_studio.providers.library import LibraryMusic

            return LibraryMusic(self.repo)
        return self.music

    async def _request_narration_review(self, project, output, report, proposal_seconds=None):
        from autonomous_studio.audio.timing import SHORT_AUTO_MAX_SECONDS

        limit = 15 if project.spec.brief.production_mode == "prototype" else SHORT_AUTO_MAX_SECONDS
        measured_speech = sum(item["actual_seconds"] for item in report)
        project.short_runtime_proposal_seconds = proposal_seconds
        self._json(
            project,
            output,
            "narration-review.json",
            {
                "reason": (
                    f"measured narration requires a {proposal_seconds:.1f}-second Short; "
                    f"approval is needed above the {limit}-second automatic ceiling"
                    if proposal_seconds is not None
                    else f"measured narration cannot fit the {limit}-second runtime policy"
                ),
                "measured_speech_seconds": measured_speech,
                "timeline_limit_seconds": limit,
                "proposed_runtime_seconds": proposal_seconds,
                "rewrite_attempts": project.timing_rewrite_attempts,
                "measurements": report,
                "version": project.version,
            },
            "narration_review",
        )
        await self._request(project, "narration", output)
        self._transition(project, State.NARRATION_APPROVAL)

    def polish(self, project_id, manifest, *, version, actor="local-user"):
        from autonomous_studio.workflow.presentation import polish

        return polish(self, project_id, manifest, version=version, actor=actor)

    def refresh_preview(
        self,
        project_id,
        *,
        version,
        reason,
        actor="local-user",
        closed_captions=None,
        semantic_visuals=False,
        shot_overrides=None,
    ):
        from autonomous_studio.workflow.presentation import refresh_preview

        return refresh_preview(
            self,
            project_id,
            version=version,
            reason=reason,
            actor=actor,
            closed_captions=closed_captions,
            semantic_visuals=semantic_visuals,
            shot_overrides=shot_overrides,
        )

    def automate_assets(self, project_id, *, version, actor="local-user"):
        from autonomous_studio.config import Settings
        from autonomous_studio.workflow.presentation import polish

        settings = self.settings or Settings.environment()
        return polish(
            self,
            project_id,
            None,
            version=version,
            actor=actor,
            auto_assets=settings.asset_policy(),
        )

    async def _voice_asset(self, project, shot, index, segment, output):
        signature = voice_signature(project, segment)
        if project.pipeline == "live" or project.review_policy == "preview":
            for prepared in project.prepared_voices:
                if (prepared.shot_id, prepared.segment_index) == (shot.id, index):
                    if prepared.signature != signature:
                        raise ValueError("prepared voice changed; create a new script revision")
                    self.repo.verify(project, prepared.asset.artifact)
                    return prepared.asset.model_copy(
                        deep=True,
                        update={
                            "start": shot.start + segment.offset,
                        },
                    )
        asset = await self.voice.synthesize(
            project,
            segment,
            project.spec.brief.voice,
            shot.start + segment.offset,
            output / f"voice-{shot.id}-{index}.wav",
        )
        if project.pipeline == "live" or project.review_policy == "preview":
            project.prepared_voices.append(
                PreparedVoice(
                    shot_id=shot.id, segment_index=index, signature=signature, asset=asset
                )
            )
            project.artifacts.append(asset.artifact)
            self.repo.save(
                project,
                "narration_measured",
                {
                    "shot_id": shot.id,
                    "segment_index": index,
                    "duration": asset.duration,
                    "artifact_id": asset.artifact.id,
                },
            )
        return asset

    async def _propose_timing_repair(self, project, proposed, report):
        internal = project.review_policy == "preview"
        # Closed captions are a project-level publication preference, not a creative
        # choice for the director. A rewrite returns a fresh VideoSpec whose schema
        # default may differ from the setting captured when the project was created.
        closed_captions = project.spec.closed_captions
        # A presentation-only revision reuses already measured, approved speech. Never silently
        # replace its custom art direction with a new director draft on an inconsistent checkpoint.
        if proposed is None and project.spec.presentation:
            raise NarrationTimingError(
                "Presentation narration no longer fits; revise the source story before polishing."
            )
        if not internal and project.timing_repairs >= MAX_TIMING_REPAIRS:
            raise NarrationTimingError(
                "Narration still does not fit after two repair proposals. "
                "Use revise with a shorter script; retry cannot fix these recordings."
            )
        previous_version, previous_duration = project.version, project.spec.brief.duration
        rewrite = proposed is None
        if rewrite:
            if internal:
                if project.timing_rewrite_attempts >= MAX_TIMING_REPAIRS:
                    raise NarrationTimingError(
                        "Narration cannot fit after two internal rewrite attempts. "
                        "No preview was sent. Please revise the narration or brief."
                    )
                # Persist BEFORE invoking the director. Restarts cannot reset the spend bound.
                project.timing_rewrite_attempts += 1
                self.repo.save(
                    project,
                    "timing_rewrite_started",
                    {
                        "attempt": project.timing_rewrite_attempts,
                        "version": project.version,
                    },
                )
            # Reuse the existing evidence; no fresh research or approval impersonation.
            import json

            revised_brief = project.spec.brief.model_copy(deep=True)
            duration_limit = 15 if revised_brief.production_mode == "prototype" else 90
            revised_brief.duration = duration_limit
            revised_brief.shots = []
            if revised_brief.production_mode == "full":
                word_budget = 105 if project.timing_rewrite_attempts <= 1 else 90
            else:
                word_budget = (
                    (12 if project.timing_rewrite_attempts <= 1 else 8) if internal else 16
                )
            repair_feedback = (
                f"TIMING REPAIR: actual speech exceeds the {duration_limit}-second "
                f"{revised_brief.production_mode} limit. "
                "Keep the current evidence, scope, claims and central story. Rewrite the complete "
                "voiceover as one cohesive narration rather than trimming each shot independently. "
                "Remove secondary details before connective words; retain causal/temporal flow, "
                "varied sentence lengths, the exact audiovisual hook contract, and a payoff that "
                "resolves it. Do not produce clipped fact fragments. Large numbers take longer to "
                f"speak than one written word. Use at most {word_budget} spoken words across the "
                "whole production, with generous scene timing. Do not accelerate speech or remove "
                "source context. "
                "Here is the current script and measured timing: "
                + json.dumps(
                    {
                        "hook": project.spec.hook,
                        "script": project.spec.narration_script,
                        "loop_bridge": project.spec.loop.bridge_line,
                        "timing": [
                            {
                                key: row[key]
                                for key in (
                                    "shot_id",
                                    "text",
                                    "actual_seconds",
                                    "available_seconds",
                                )
                            }
                            for row in report
                        ],
                    }
                )
            )
            try:
                proposed = await self.director.develop(revised_brief, [repair_feedback])
            except ValueError as exc:
                if not internal:
                    raise
                raise NarrationTimingError("Internal timing rewrite failed validation") from exc
            if proposed.brief.research != project.spec.brief.research:
                raise NarrationTimingError("Timing repair must preserve the existing research")
            if (
                sum(len(v.spoken_text.split()) for s in proposed.shots for v in s.voice_segments)
                > word_budget
            ):
                raise NarrationTimingError(
                    f"The timing rewrite did not meet its {word_budget}-word budget. "
                    "Please use revise for a shorter script; unchanged retry cannot fix this."
                )
        note = (
            f"Timing repair v{previous_version} -> v{previous_version + 1}: "
            + (
                "proposed shorter narration; existing research retained. "
                if rewrite
                else "script unchanged; scene timing now follows measured narration. "
            )
            + f"Duration: {previous_duration:.2f}s -> {proposed.brief.duration:.2f}s. "
            + (
                "No speech was cut or sped up. Changes will be included in the final preview.\n"
                if internal
                else "No speech was cut or sped up. Please review the new story and hero timing.\n"
            )
            + "\n".join(
                f"{r['shot_id']}: audio {r['actual_seconds']:.2f}s, "
                f"previous allowance {r['available_seconds']:.2f}s"
                for r in report
            )
        )
        old_artifacts = project.current_artifacts()
        old_prepared = project.prepared_voices
        project.version += 1
        project.timing_repairs += 1
        project.timing_repair_note = note
        if internal:
            project.production_changes.append(note)
        proposed.closed_captions = closed_captions
        project.spec = proposed
        validate_story_scope(project.spec)
        project.spec.audio_assets = []
        project.prepared_voices = []
        project.warnings = self.warnings_for(project)
        output = self.repo.attempt_dir(project, "timing-repair")
        self._json(
            project,
            output,
            "scope-validation.json",
            {
                "passed": True,
                "production_mode": project.spec.brief.production_mode,
                "duration": project.spec.brief.duration,
                "shots": len(project.spec.shots),
                "expected_rank_count": project.spec.brief.expected_rank_count,
                "rank_order": [
                    shot.rank for shot in project.spec.shots if shot.kind == "rank_reveal"
                ],
                "version": project.version,
                "after_timing_repair": True,
            },
            "scope_validation",
        )
        # Reuse evidence/audio as separately versioned files, keeping old approvals immutable.
        reused = {}
        for artifact in old_artifacts:
            roles = (
                {"research", "findings"}
                if rewrite
                else {
                    "research",
                    "findings",
                    "voice",
                    "visual_asset",
                    "music_source",
                    "license_evidence",
                    "image_prompt",
                    "presentation_manifest",
                    "baseline",
                }
            )
            if artifact.role not in roles:
                continue
            source = self.repo.verify(project, artifact)
            destination = output / f"reused-{artifact.id}{source.suffix}"
            shutil.copyfile(source, destination)
            copied = artifact.model_copy(
                deep=True,
                update={
                    "id": uuid4().hex,
                    "version": project.version,
                    "path": destination.relative_to(self.repo.project_dir(project.id)).as_posix(),
                    "input_reference": f"reuse:{artifact.id}",
                    "created_at": now(),
                    "cost": 0,
                },
            )
            project.artifacts.append(copied)
            reused[artifact.id] = copied
        if project.spec.presentation:
            for look in project.spec.presentation.shots:
                if look.image_ref:
                    look.image_ref = reused[look.image_ref].id
            if project.spec.brief.music.artifact_ref:
                ref = project.spec.brief.music.artifact_ref
                project.spec.brief.music.artifact_ref = reused[ref].id
            project.warnings = self.warnings_for(project)
        if not rewrite:
            shots = {s.id: s for s in proposed.shots}
            for prepared in old_prepared:
                shot = shots[prepared.shot_id]
                segment = sorted(shot.voice_segments, key=lambda v: v.offset)[
                    prepared.segment_index
                ]
                asset = prepared.asset.model_copy(
                    deep=True,
                    update={
                        "artifact": reused[prepared.asset.artifact.id],
                        "start": shot.start + segment.offset,
                    },
                )
                project.prepared_voices.append(
                    prepared.model_copy(deep=True, update={"asset": asset})
                )
        else:
            self.record_trace(project, output, "timing-director")
        self._json(
            project,
            output,
            "timing-repair.json",
            {
                "previous_version": previous_version,
                "summary": note,
                "measurements": report,
                "previous_duration": previous_duration,
                "proposed_duration": proposed.brief.duration,
                "mode": "rewrite" if rewrite else "retime",
            },
            "timing_repair",
        )
        self._json(project, output, "brief.json", proposed.brief.model_dump(mode="json"), "brief")
        self._json(project, output, "story.json", proposed.model_dump(mode="json"), "story")
        script = output / "voiceover.txt"
        script.write_text(
            "\n\n".join(
                f"[{s.id} / {s.start:.3f}s / scene {s.duration:.3f}s] {v.spoken_text}"
                for s in proposed.shots
                for v in s.voice_segments
            ),
            encoding="utf-8",
        )
        project.artifacts.append(self.repo.artifact(project, script, "voiceover_script"))
        if not internal:
            await self._request(project, "story", output)
        old_state, project.state = (
            project.state,
            (State.NARRATION_PREPARING if internal else State.STORY_APPROVAL),
        )
        project.last_error = project.last_error_code = None
        self.repo.save(
            project,
            "timing_repair_applied" if internal else "timing_repair_proposed",
            {
                "from": old_state,
                "to": project.state,
                "previous_version": previous_version,
                "version": project.version,
                "mode": "rewrite" if rewrite else "retime",
                "summary": note,
            },
        )

    def request_retry(self, project_id, *, actor="local-user", repair=False, version=None):
        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            visual_anchor_replay = None
            visual_replay_allow_continuation = False
            if (
                isinstance(project.spec, LongFormSpec)
                and project.state == State.LONGFORM_VISUAL_PLANNING
                and project.last_error_code == "provider_validation"
                and project.longform_visual_plan is None
                and project.longform_visual_plan_attempts > 0
            ):
                failures = [
                    event["payload"].get("error", "")
                    for event in self.repo.events(project.id)
                    if event["kind"] == "longform_visual_plan_rejected"
                    and event["payload"].get("version") == project.version
                ]
                if failures and "image direction is not anchored in" in failures[-1]:
                    from autonomous_studio.workflow.longform_visuals import saved_visual_responses

                    try:
                        visual_anchor_replay, _ = saved_visual_responses(
                            self, project, allow_partial=True
                        )
                    except ValueError:
                        pass
                    visual_replay_allow_continuation = visual_anchor_replay is not None
                elif failures and "displays labels out of spoken order" in failures[-1]:
                    from autonomous_studio.workflow.longform_visuals import saved_visual_responses

                    try:
                        visual_anchor_replay, _ = saved_visual_responses(self, project)
                    except ValueError:
                        pass
                elif failures and "reveals must cover its speech once in order" in failures[-1]:
                    from autonomous_studio.workflow.longform_visuals import (
                        saved_image_role_recovery,
                    )

                    try:
                        visual_anchor_replay = saved_image_role_recovery(
                            self, project, failures[-1]
                        )
                    except (KeyError, TypeError, ValueError):
                        pass
                    visual_replay_allow_continuation = visual_anchor_replay is not None
                elif failures and (
                    "designed reveal label exceeds renderer slot" in failures[-1]
                    or "concept_contrast/juxtaposition supporting_copy" in failures[-1]
                ):
                    # Revalidate the already-paid chapter responses after a
                    # treatment text-slot contract/renderer fix. Never turn a
                    # copy-layout recovery into another creative model request.
                    from autonomous_studio.workflow.longform_visuals import saved_visual_responses

                    try:
                        visual_anchor_replay, _ = saved_visual_responses(self, project)
                    except ValueError:
                        pass
            visual_budget_policy_recovery = bool(
                isinstance(project.spec, LongFormSpec)
                and project.state == State.LONGFORM_VISUAL_PLANNING
                and project.last_error_code == "longform_visual_planning"
                and any(
                    event["kind"] == "longform_visual_plan_rejected"
                    and event["payload"].get("version") == project.version
                    and "generated-image request budget" in event["payload"].get("error", "")
                    for event in reversed(self.repo.events(project.id))
                )
            )
            visual_beat_recovery = bool(
                isinstance(project.spec, LongFormSpec)
                and project.state == State.LONGFORM_VISUAL_PLANNING
                and project.longform_visual_plan_attempts >= 2
                and (
                    errors := [
                        event["payload"].get("error", "")
                        for event in self.repo.events(project.id)
                        if event["kind"] == "longform_visual_plan_rejected"
                        and event["payload"].get("version") == project.version
                    ]
                )
                and any(
                    "Long-form visual plan failed validation after bounded internal repairs:"
                    in error
                    and (
                        "uses an anchor absent from approved narration" in error
                        or "needs at most two image labels" in error
                    )
                    for error in errors
                )
            )
            visual_transport_recovery = is_visual_provider_transport_failure(project)
            visual_relation_validator_recovery = bool(
                isinstance(project.spec, LongFormSpec)
                and project.state == State.LONGFORM_VISUAL_PLANNING
                and project.longform_visual_plan is None
                and project.longform_visual_plan_attempts > 0
                and project.last_error_code == "provider_validation"
                and project.last_error
                and "same-subject progression changed graphic subject" in project.last_error
            )
            visual_partition_id_recovery = bool(
                isinstance(project.spec, LongFormSpec)
                and project.state == State.LONGFORM_VISUAL_PLANNING
                and project.longform_visual_plan is None
                and project.longform_visual_plan_attempts > 0
                and project.last_error_code == "provider_validation"
                and project.last_error
                and "partition reuses an earlier chapter beat ID" in project.last_error
            )
            visual_unused_copy_recovery = bool(
                isinstance(project.spec, LongFormSpec)
                and project.state == State.LONGFORM_VISUAL_PLANNING
                and project.longform_visual_plan is None
                and project.longform_visual_plan_attempts > 0
                and project.last_error_code == "provider_validation"
                and project.last_error
                and "has no supporting-copy slot; every supporting_copy entry must be empty"
                in project.last_error
            )
            semantic_response_choice_recovery = bool(
                isinstance(project.spec, LongFormSpec)
                and project.state == State.LONGFORM_SEMANTIC_PLANNING
                and project.last_error_code == "provider_validation"
                and project.last_error
                and "response choice needs one ordered occurrence in approved speech"
                in project.last_error
            )
            if version is not None and project.version != version:
                raise ValueError("stale repair version")
            if project.pending() or project.state in (State.COMPLETED, State.REJECTED):
                raise ValueError("No failed stage to retry; use the current review or revise")
            if is_timing_failure(project):
                if not repair:
                    raise ValueError(
                        "Retry would reuse the same overlong audio. Request "
                        f"/repair {project.id} {project.version} instead."
                    )
                used_repairs = (
                    project.timing_rewrite_attempts
                    if project.review_policy == "preview"
                    else project.timing_repairs
                )
                if used_repairs >= MAX_TIMING_REPAIRS:
                    if project.review_policy == "preview":
                        project.last_error = project.last_error_code = None
                        self.repo.save(
                            project,
                            "narration_review_resume_requested",
                            {"actor": actor, "version": project.version},
                        )
                        return project
                    raise ValueError(
                        "Repair limit reached; use revise to request shorter narration"
                    )
            elif repair:
                raise ValueError("No narration timing failure to repair")
            elif project.last_error_code in {
                "provider_validation",
                "asset_planning",
                "longform_visual_planning",
            } and not (
                is_legacy_longform_duration_failure(project)
                or visual_budget_policy_recovery
                or visual_beat_recovery
                or visual_relation_validator_recovery
                or visual_partition_id_recovery
                or visual_unused_copy_recovery
                or visual_anchor_replay is not None
                or semantic_response_choice_recovery
            ):
                if project.longform_creative_policy is not None:
                    raise ValueError(
                        "Unchanged retry would repeat the invalid first draft; inspect its "
                        "diagnostic and revise the plan or fix the framework"
                    )
                raise ValueError(
                    "Unchanged retry would repeat an exhausted validation stage; use revise"
                )
            if (
                visual_budget_policy_recovery
                or visual_beat_recovery
                or visual_relation_validator_recovery
                or visual_partition_id_recovery
                or visual_unused_copy_recovery
                or visual_transport_recovery
                or visual_anchor_replay is not None
            ):
                project.longform_visual_plan_attempts = 0
            if visual_anchor_replay is not None:
                project.creation_settings["visual_plan_replay_artifact"] = (
                    visual_anchor_replay.path
                )
                if visual_replay_allow_continuation:
                    project.creation_settings["visual_plan_replay_allow_continuation"] = True
            project.last_error = project.last_error_code = None
            self.repo.save(
                project,
                (
                    "visual_plan_policy_recovery_requested"
                    if visual_budget_policy_recovery
                    else "visual_plan_beat_recovery_requested"
                    if visual_beat_recovery
                    else "visual_plan_validator_recovery_requested"
                    if visual_relation_validator_recovery
                    else "visual_plan_partition_id_recovery_requested"
                    if visual_partition_id_recovery
                    else "visual_plan_unused_copy_recovery_requested"
                    if visual_unused_copy_recovery
                    else "semantic_response_choice_recovery_requested"
                    if semantic_response_choice_recovery
                    else "visual_plan_saved_response_recovery_requested"
                    if visual_anchor_replay is not None
                    else "visual_plan_transport_recovery_requested"
                    if visual_transport_recovery
                    else "timing_repair_requested"
                    if repair
                    else "retry_requested"
                ),
                {
                    "actor": actor,
                    "version": project.version,
                    **(
                        {"saved_response_artifact": visual_anchor_replay.path,
                         "new_model_requests_allowed": visual_replay_allow_continuation}
                        if visual_anchor_replay is not None else {}
                    ),
                },
            )
            return project

    def revalidate_narration_layout(
        self, project_id, *, version, actor="local-operator"
    ) -> VideoProject:
        """Resume a narration review after a deterministic timing-layout policy correction.

        This never approves or edits the script. It supersedes the obsolete exception review and
        asks the engine to re-evaluate the existing measured recordings under current local rules.
        """
        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            if project.version != version:
                raise ValueError("stale project version")
            pending = project.pending()
            if (
                project.state != State.NARRATION_APPROVAL
                or pending is None
                or pending.gate != "narration"
            ):
                raise ValueError("project is not waiting at a narration timing review")
            pending.decision = "superseded"
            pending.actor = actor
            pending.message = "Revalidate existing measured narration under corrected layout rules."
            pending.decided_at = now()
            old = project.state
            project.state = State.NARRATION_PREPARING
            project.last_error = project.last_error_code = None
            project.production_changes.append(
                "Obsolete narration review superseded after a deterministic timing-layout fix; "
                "script, research and measured speech were unchanged."
            )
            self.repo.save(
                project,
                "narration_layout_revalidation_requested",
                {
                    "from": old,
                    "to": project.state,
                    "version": project.version,
                    "approval_id": pending.id,
                    "actor": actor,
                    "provider_calls": 0,
                    "script_changed": False,
                },
            )
            return project

    def enable_preview_review(self, project_id, *, version, actor="local-user"):
        """Explicit, idempotent policy migration. Supersede requests, never invent approval."""
        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            if project.version != version:
                raise ValueError("stale project version")
            if project.review_policy == "preview":
                return project
            if project.state in (State.COMPLETED, State.REJECTED):
                raise ValueError("terminal projects require an explicit revision first")
            project.review_policy = "preview"
            project.timing_rewrite_attempts = 0
            old = project.state
            pending = project.pending()
            if pending and pending.gate != "prototype":
                pending.decision = "superseded"
                pending.actor, pending.decided_at = actor, now()
                pending.message = "Intermediate review superseded by validated-preview policy"
            if project.state in (State.STORY_APPROVAL, State.STORYBOARDED, State.HERO_APPROVAL):
                project.state = State.NARRATION_PREPARING
            project.production_changes.append(
                "Review policy changed: finish timing and bounded repairs internally, then "
                "review one validated audiovisual preview. Previous approvals are retained."
            )
            self.repo.save(
                project,
                "review_policy_changed",
                {
                    "actor": actor,
                    "version": version,
                    "policy": "preview",
                    "from": old,
                    "to": project.state,
                    "superseded_request": pending.id
                    if pending and pending.decision == "superseded"
                    else None,
                    "retained_approvals": [
                        a.id for a in project.approvals if a.decision == "approve"
                    ],
                },
            )
            return project

    def decide(
        self,
        project_id: str,
        gate: str,
        decision: str,
        *,
        message="",
        actor="local-user",
        approval_id: str | None = None,
        version: int | None = None,
    ) -> VideoProject:
        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            approval = project.pending()
            if not approval or GATES.get(project.state) != gate or approval.gate != gate:
                raise ValueError("no pending approval for this gate")
            if approval_id and approval.id != approval_id:
                raise ValueError("stale or mismatched approval ID")
            if version is not None and approval.version != version:
                raise ValueError("stale approval version")
            if decision not in ("approve", "reject"):
                raise ValueError("use revise for revision feedback")
            if (
                gate == "narration"
                and decision == "approve"
                and not (
                    project.short_runtime_proposal_seconds is not None
                    and project.spec.brief.production_mode == "full"
                )
            ):
                raise ValueError("narration review requires trimming feedback or rejection")
            verify_approval(self.repo, project, approval)
            final_video = None
            approval.decision, approval.actor = decision, actor
            approval.message, approval.decided_at = message, now()
            project.feedback.append(
                Feedback(
                    gate=gate,
                    decision=decision,
                    message=message,
                    actor=actor,
                    version=project.version,
                    approval_id=approval.id,
                    artifact_ids=approval.artifact_ids,
                )
            )
            duration_expansion = None
            if (
                decision == "approve"
                and gate == "script"
                and isinstance(project.spec, LongFormSpec)
            ):
                duration_expansion = self._adopt_longform_runtime(project)
            if decision == "approve" and gate == "runtime":
                proposal = project.longform_runtime_proposal
                if proposal is None or proposal.version != project.version:
                    raise ValueError("runtime approval requires a current measured proposal")
                project.longform_runtime_approved_seconds = proposal.proposed_seconds
                project.spec.brief.duration_policy.expansion_maximum_seconds = (
                    proposal.proposed_seconds
                )
                duration_expansion = {
                    "measured_minimum_seconds": proposal.measured_minimum_seconds,
                    "approved_seconds": proposal.proposed_seconds,
                    "basis": "measured_narration",
                }
            if decision == "approve" and gate == "narration":
                project.short_runtime_approved_seconds = project.short_runtime_proposal_seconds
                duration_expansion = {
                    "approved_seconds": project.short_runtime_approved_seconds,
                    "basis": "measured_short_narration",
                }
            if decision == "approve" and isinstance(project.spec, LongFormSpec):
                next_state = {
                    "treatment": State.LONGFORM_SCRIPTING,
                    "script": State.LONGFORM_NARRATIVE_READY,
                    "visual_plan": State.LONGFORM_VISUAL_PLAN_READY,
                    "runtime": State.LONGFORM_AUDIO_PREPARING,
                    "prototype": (
                        State.PUBLICATION_CLEARANCE
                        if project.publication_clearance_enabled
                        else State.LONGFORM_MASTER_READY
                    ),
                    "publication": State.COMPLETED,
                }.get(gate)
                if next_state is None:
                    raise ValueError("unsupported long-form approval gate")
            else:
                next_state = (
                    {
                        "story": State.STORYBOARDED,
                        "hero": State.HERO_APPROVAL,
                        "narration": State.NARRATION_PREPARING,
                        "script": (
                            State.ASSETS_PREPARING
                            if project.auto_assets
                            else State.AUDIO_ASSEMBLING
                        ),
                        "prototype": (
                            State.PUBLICATION_CLEARANCE
                            if project.publication_clearance_enabled
                            else State.COMPLETED
                        ),
                        "publication": State.COMPLETED,
                    }[gate]
                    if decision == "approve"
                    else State.REJECTED
                )
            publication_files = None
            if (
                gate == "prototype"
                and decision == "approve"
                and (
                    isinstance(project.spec, LongFormSpec)
                    or not project.publication_clearance_enabled
                )
            ):
                final_video = self._export_final_video(project)
                if isinstance(project.spec, LongFormSpec):
                    master = self.repo.artifact(
                        project,
                        self.repo.project_dir(project.id) / final_video,
                        "longform_master",
                        provider="local",
                        model="lf-v0.4-master",
                        duration=project.spec.brief.duration,
                        width=1920,
                        height=1080,
                    )
                    project.artifacts.append(master)
            if gate == "publication":
                from autonomous_studio.workflow.publication import (
                    create_release_status,
                    export_cleared_package,
                )

                release_decision = "cleared" if decision == "approve" else "rejected"
                output = self.repo.attempt_dir(project, f"publication-{release_decision}")
                release = create_release_status(
                    self.repo,
                    project,
                    output,
                    release_decision,
                    actor=actor,
                    approval=approval,
                    message=message,
                )
                project.publication.release_status_artifact_id = release.id
                if decision == "approve":
                    publication_files = export_cleared_package(self.repo, project, release)
                    final_video = "final-video.mp4"
            old = project.state
            project.state = next_state
            self.repo.save(
                project,
                "approval_decided",
                {
                    "approval_id": approval.id,
                    "decision": decision,
                    "from": old,
                    "to": next_state,
                    "gate": gate,
                    "version": project.version,
                    "actor": actor,
                    "message": message,
                    "final_video": final_video,
                    "publication_files": publication_files,
                    "duration_expansion": duration_expansion,
                },
            )
            return project

    def adopt_longform_review_variant(self, project_id: str, relative_path: str) -> VideoProject:
        """Bind a locally revised review cut to the pending, versioned approval.

        The original review remains immutable. Approval/export will use this latest
        prototype only after the user explicitly approves it.
        """
        from autonomous_studio.rendering.ffmpeg import FFmpeg

        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            pending = project.pending()
            if (
                not isinstance(project.spec, LongFormSpec)
                or project.state != State.LONGFORM_REVIEW_APPROVAL
                or pending is None
                or pending.gate != "prototype"
            ):
                raise ValueError("long-form review variant requires pending prototype approval")
            source = self.repo.resolve(project_id, relative_path)
            if not source.is_file() or source.suffix.lower() != ".mp4":
                raise ValueError("review variant must be an existing project-local MP4")
            originals = project.current_artifacts("prototype")
            if not originals:
                raise ValueError("no original review prototype to replace")
            original = originals[-1]
            self.repo.verify(project, original)
            inspection = FFmpeg().inspect(source)
            if (
                not inspection["has_video"]
                or not inspection["has_audio"]
                or (inspection["width"], inspection["height"]) != (1920, 1080)
                or inspection["duration"] is None
                or original.duration is None
                or abs(inspection["duration"] - original.duration) > 0.15
            ):
                raise ValueError("review variant fails video/audio/duration contract")
            variant = self.repo.artifact(
                project, source, "prototype", provider="local",
                model="longform-reviewed-local-variant",
                duration=inspection["duration"], width=inspection["width"],
                height=inspection["height"],
            )
            if variant.sha256 == original.sha256:
                raise ValueError("review variant is identical to the current prototype")
            project.artifacts.append(variant)
            pending.artifact_ids.append(variant.id)
            self.repo.save(project, "longform_review_variant_adopted", {
                "version": project.version,
                "original_artifact_id": original.id,
                "variant_artifact_id": variant.id,
                "variant_sha256": variant.sha256,
            })
            return project

    def register_longform_publication_package(self, project_id: str) -> VideoProject:
        """Record reviewed, project-root YouTube copy and thumbnail for a finished film.

        This records prepared files; it deliberately does not clear rights or upload.
        """
        from PIL import Image

        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            if not isinstance(project.spec, LongFormSpec) or project.state != State.LONGFORM_MASTER_READY:
                raise ValueError("long-form publication files need an approved master")
            if not project.current_artifacts("longform_master"):
                raise ValueError("approved long-form master is missing")
            root = self.repo.project_dir(project_id)
            title_path = root / "youtube-title.txt"
            description_path = root / "youtube-description.txt"
            thumbnail_path = root / "selected-thumbnail.png"
            if not all(path.is_file() for path in (title_path, description_path, thumbnail_path)):
                raise ValueError("title, description and selected thumbnail must be in project root")
            title = title_path.read_text(encoding="utf-8").strip()
            description = description_path.read_text(encoding="utf-8").strip()
            if not title or "\n" in title or len(title) > 100:
                raise ValueError("YouTube title must be one line of at most 100 characters")
            if not description or len(description) > 5000:
                raise ValueError("YouTube description must be at most 5000 characters")
            with Image.open(thumbnail_path) as image:
                if image.format != "PNG" or image.size != (1280, 720):
                    raise ValueError("selected thumbnail must be a 1280x720 PNG")
            roles = (
                ("longform_youtube_title", title_path),
                ("longform_youtube_description", description_path),
                ("longform_selected_thumbnail", thumbnail_path),
            )
            current = {a.role: a for a in project.current_artifacts()}
            registered = []
            for role, path in roles:
                candidate = self.repo.artifact(project, path, role)
                existing = current.get(role)
                if existing and existing.sha256 == candidate.sha256:
                    registered.append(existing.id)
                    continue
                project.artifacts.append(candidate)
                registered.append(candidate.id)
            self.repo.save(project, "longform_publication_files_registered", {
                "version": project.version,
                "artifact_ids": registered,
                "rights_clearance": "not_assessed",
                "uploaded": False,
            })
            return project

    def queue_longform_publication(self, project_id: str, *, actor="local-operator") -> VideoProject:
        """Move a previously approved long-form master into publication review."""
        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            if not isinstance(project.spec, LongFormSpec) or project.state != State.LONGFORM_MASTER_READY:
                raise ValueError("publication backfill requires an approved long-form master")
            if project.pending() or not project.current_artifacts("longform_master"):
                raise ValueError("publication backfill requires a durable master without pending review")
            if not any(
                a.gate == "prototype" and a.decision == "approve" and a.version == project.version
                for a in project.approvals
            ):
                raise ValueError("long-form prototype approval is missing")
            project.publication_clearance_enabled = True
            project.state = State.PUBLICATION_CLEARANCE
            self.repo.save(project, "longform_publication_queued", {
                "version": project.version, "actor": actor,
                "prior_state": State.LONGFORM_MASTER_READY,
            })
            return project

    @staticmethod
    def _adopt_longform_runtime(project: VideoProject) -> dict | None:
        report = project.longform_continuity
        if report is None or not report.passed:
            raise ValueError("long-form script approval requires passed continuity validation")
        if report.duration_status != "expansion_proposed":
            return None
        spec = project.spec.model_copy(deep=True)
        previous = spec.brief.duration_policy.target_seconds
        proposed = report.proposed_timeline_seconds
        ceiling = spec.brief.duration_policy.expansion_maximum_seconds
        if not previous < proposed <= ceiling:
            raise ValueError("proposed long-form runtime is outside the approved expansion policy")
        total = sum(chapter.target_duration for chapter in spec.chapters)
        allocated = 0.0
        for chapter in spec.chapters[:-1]:
            chapter.target_duration = (
                round((chapter.target_duration / total * proposed) * spec.brief.fps)
                / spec.brief.fps
            )
            allocated += chapter.target_duration
        spec.chapters[-1].target_duration = proposed - allocated
        spec.brief.duration_policy.target_seconds = proposed
        spec.brief.duration_policy.maximum_seconds = max(
            spec.brief.duration_policy.maximum_seconds, proposed
        )
        project.spec = LongFormSpec.model_validate(spec.model_dump(mode="json"))
        return {
            "previous_target_seconds": previous,
            "approved_target_seconds": proposed,
            "estimated_speech_seconds": report.estimated_duration_seconds,
            "policy": "elastic_longform_v0.2.5",
        }

    def _export_final_video(self, project: VideoProject) -> str:
        """Expose the approved render at a stable project-root path.

        The versioned prototype remains the immutable artifact of record. The root file is a
        replaceable convenience export that always represents the latest approved version.
        """
        movies = project.current_artifacts("prototype")
        if not movies:
            raise ValueError("prototype approval requires a rendered video")
        source = self.repo.verify(project, movies[-1])
        destination = self.repo.project_dir(project.id) / "final-video.mp4"
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            shutil.copyfile(source, temporary)
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination.name

    def revise(
        self,
        project_id: str,
        message: str,
        *,
        brief: ProjectBrief | None = None,
        actor="local-user",
        approval_id=None,
        expected_version=None,
        external_id=None,
    ) -> VideoProject:
        if not message.strip():
            raise ValueError("revision feedback cannot be empty")
        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            if external_id and any(f.external_id == external_id for f in project.feedback):
                return project
            pending_approval = project.pending()
            rejected_script_review = (
                next(
                    (
                        item
                        for item in reversed(project.approvals)
                        if item.version == project.version
                        and item.gate in {"script", "narration"}
                        and item.decision == "reject"
                    ),
                    None,
                )
                if project.state == State.REJECTED and isinstance(project.spec, VideoSpec)
                else None
            )
            approval = pending_approval or rejected_script_review
            if approval_id and (not approval or approval.id != approval_id):
                raise ValueError("stale revision request")
            if expected_version is not None and project.version != expected_version:
                raise ValueError("stale revision version")
            if (
                isinstance(project.spec, LongFormSpec)
                and approval is not None
                and approval.gate == "publication"
            ):
                from autonomous_studio.workflow.publication import is_metadata_revision

                if not is_metadata_revision(message):
                    raise ValueError(
                        "long-form publication feedback needs title:, description:, "
                        "or thumbnail:; creative changes require a new film revision"
                    )
            project.feedback.append(
                Feedback(
                    gate=approval.gate if approval else None,
                    decision="revise",
                    message=message,
                    actor=actor,
                    version=project.version,
                    approval_id=approval.id if approval else None,
                    artifact_ids=approval.artifact_ids if approval else [],
                    external_id=external_id,
                )
            )
            if pending_approval:
                pending_approval.decision, pending_approval.actor = "revise", actor
                pending_approval.message, pending_approval.decided_at = message, now()
            if approval and approval.gate == "publication":
                from autonomous_studio.workflow.publication import (
                    is_metadata_revision,
                    revise_publication_package,
                )

                if is_metadata_revision(message):
                    output = self.repo.attempt_dir(project, "publication-revision")
                    revise_publication_package(self.repo, project, output, message)
                    self._json(
                        project,
                        output,
                        "feedback.json",
                        project.feedback[-1].model_dump(mode="json"),
                        "feedback",
                    )
                    self.repo.save(
                        project,
                        "publication_metadata_revised",
                        {
                            "version": project.version,
                            "message": message,
                            "actor": actor,
                            "creative_assets_reused": True,
                        },
                    )
                    return project
            if (
                isinstance(project.spec, LongFormSpec)
                and approval
                and approval.gate in {"treatment", "script", "visual_plan", "runtime", "prototype"}
            ):
                script_revision = (
                    approval.gate == "prototype"
                    and message.lstrip().lower().startswith(("script:", "narration:"))
                )
                treatment_restart = (
                    approval.gate == "prototype"
                    and message.lstrip().lower().startswith(("treatment:", "restart:"))
                )
                research_restart = (
                    approval.gate == "prototype"
                    and message.lstrip().lower().startswith("research:")
                )
                previous = project.spec.model_copy(deep=True)
                original_brief = None
                if research_restart:
                    initial = next(
                        (item for item in project.artifacts
                         if item.role == "brief" and item.version == 1),
                        None,
                    )
                    if initial is None:
                        raise ValueError("research restart requires the original brief artifact")
                    original_brief = LongFormBrief.model_validate_json(
                        self.repo.verify(project, initial).read_text(encoding="utf-8")
                    )
                project.version += 1
                if (
                    approval.gate in {"treatment", "script", "runtime"}
                    or script_revision
                    or treatment_restart
                    or research_restart
                ):
                    project.longform_continuity = None
                    project.longform_voices = {}
                    project.longform_word_timings = {}
                project.longform_runtime_proposal = None
                project.longform_runtime_approved_seconds = None
                project.longform_visual_plan = None
                project.longform_visual_plan_attempts = 0
                project.longform_media = {}
                project.longform_semantic_decisions = {}
                project.longform_semantic_hashes = {}
                project.longform_semantic_models = {}
                project.longform_chapters = {}
                project.longform_chapter_hashes = {}
                project.longform_image_requests = 0
                project.last_error = project.last_error_code = None
                if treatment_restart or research_restart:
                    # A creative restart keeps reviewed research, but no prior production
                    # asset, narration, music selection, or chapter can enter the new cut.
                    from uuid import uuid4

                    project.creation_settings["fresh_media_epoch"] = uuid4().hex
                    project.creation_settings["narration_linked_timeline"] = True
                    project.longform_music = None
                    project.longform_chapters = {}
                    project.longform_chapter_hashes = {}
                if research_restart:
                    fresh_brief = original_brief.model_copy(deep=True)
                    project.longform_research_plan = None
                    project.spec = LongFormSpec(
                        story_contract_version=previous.story_contract_version,
                        brief=fresh_brief,
                        narrative={
                            "viewer_promise": fresh_brief.viewer_promise,
                            "central_question": fresh_brief.central_question,
                        },
                        engagement_ending=fresh_brief.engagement_ending,
                        closed_captions=previous.closed_captions,
                    )
                    next_state = State.LONGFORM_FOUNDATION_READY
                    stage = "longform-research-restart"
                elif approval.gate == "treatment" or treatment_restart:
                    if treatment_restart:
                        # The previous render's exact ceiling is not an editorial limit
                        # on the newly written story. A longer script still needs review.
                        previous.brief.duration_policy.expansion_maximum_seconds = max(
                            600, previous.brief.duration_policy.expansion_maximum_seconds
                        )
                    project.spec = LongFormSpec(
                        story_contract_version=previous.story_contract_version,
                        brief=previous.brief,
                        narrative={
                            "viewer_promise": previous.brief.viewer_promise,
                            "central_question": previous.brief.central_question,
                        },
                        engagement_ending=previous.brief.engagement_ending,
                        closed_captions=previous.closed_captions,
                    )
                    next_state = State.LONGFORM_RESEARCHED
                    stage = (
                        "longform-treatment-restart"
                        if treatment_restart
                        else "longform-treatment-revision"
                    )
                elif approval.gate in {"script", "runtime"} or script_revision:
                    for chapter in previous.chapters:
                        chapter.narration_blocks = []
                        chapter.visual_beats = []
                    project.spec = previous
                    next_state = State.LONGFORM_SCRIPTING
                    stage = "longform-script-revision"
                else:
                    for chapter in previous.chapters:
                        chapter.visual_beats = []
                    project.spec = previous
                    next_state = State.LONGFORM_VISUAL_PLANNING
                    stage = "longform-visual-plan-revision"
                old, project.state = project.state, next_state
                output = self.repo.attempt_dir(project, stage)
                self._json(
                    project,
                    output,
                    "research.json",
                    project.spec.brief.research.model_dump(mode="json"),
                    "research",
                )
                if approval.gate in {"script", "visual_plan", "runtime", "prototype"}:
                    self._json(
                        project,
                        output,
                        "treatment.json",
                        project.spec.model_dump(mode="json"),
                        "longform_treatment",
                    )
                if approval.gate in {"visual_plan", "prototype"}:
                    self._json(
                        project,
                        output,
                        "approved-script.json",
                        project.spec.model_dump(mode="json"),
                        "longform_script",
                    )
                self._json(
                    project,
                    output,
                    "feedback.json",
                    project.feedback[-1].model_dump(mode="json"),
                    "feedback",
                )
                self.repo.save(
                    project,
                    f"longform_{approval.gate}_revision_requested",
                    {
                        "from": old,
                        "to": next_state,
                        "version": project.version,
                        "message": message,
                        "actor": actor,
                        "research_reused": not research_restart,
                    },
                )
                return project
            if approval and approval.gate in {"narration", "script"}:
                previous_research = project.spec.brief.research.model_copy(deep=True)
                project.short_revision_script = project.spec.narration_script
                project.version += 1
                brief_for_revision = project.spec.brief.model_copy(deep=True)
                brief_for_revision.shots = []
                if approval.gate == "script":
                    # Generated creative fields from the prior script must not anchor
                    # its replacement. The topic, editorial scope and research survive.
                    brief_for_revision.hook = ""
                    brief_for_revision.hook_narration = ""
                    brief_for_revision.thesis = ""
                    brief_for_revision.payoff = ""
                project.spec = VideoSpec(
                    brief=brief_for_revision,
                    closed_captions=project.spec.closed_captions,
                )
                project.prepared_voices = []
                project.timing_repairs = 0
                project.timing_repair_note = ""
                project.timing_rewrite_attempts = 0
                project.short_runtime_proposal_seconds = None
                project.short_runtime_approved_seconds = None
                project.production_changes = [
                    (
                        "Human reviewed the overlong narration and requested targeted trimming."
                        if approval.gate == "narration"
                        else "Human reviewed the timing-validated script and requested a rewrite."
                    )
                ]
                project.asset_plan = None
                project.asset_plan_attempts = project.image_requests = 0
                project.generated_visuals = []
                project.last_error = project.last_error_code = None
                project.warnings = self.warnings_for(project)
                old, project.state = project.state, State.RESEARCHED
                output = self.repo.attempt_dir(project, "narration-revision")
                self._json(
                    project,
                    output,
                    "research.json",
                    previous_research.model_dump(mode="json"),
                    "research",
                )
                findings = output / "findings.md"
                findings.write_text(
                    "# Research evidence\n\n"
                    + "\n\n".join(
                        f"- {claim.text}\n  Status: {claim.status}. {claim.caveat}"
                        for claim in previous_research.claims
                    ),
                    encoding="utf-8",
                )
                project.artifacts.append(self.repo.artifact(project, findings, "findings"))
                self._json(
                    project,
                    output,
                    "brief.json",
                    project.spec.brief.model_dump(mode="json"),
                    "brief",
                )
                self._json(
                    project,
                    output,
                    "feedback.json",
                    project.feedback[-1].model_dump(mode="json"),
                    "feedback",
                )
                self.repo.save(
                    project,
                    (
                        "narration_revision_requested"
                        if approval.gate == "narration"
                        else "script_revision_requested"
                    ),
                    {
                        "from": old,
                        "to": project.state,
                        "version": project.version,
                        "message": message,
                        "actor": actor,
                        "research_reused": True,
                    },
                )
                return project
            if isinstance(project.spec, LongFormSpec):
                previous = project.spec.model_copy(deep=True)
                was_ceiling_error = project.last_error_code == "longform_narration_ceiling"
                treatment_replan_from_script_failure = (
                    project.state == State.LONGFORM_SCRIPTING
                    and bool(project.last_error)
                    and message.lstrip().lower().startswith("treatment:")
                )
                project.version += 1
                project.last_error = project.last_error_code = None
                if treatment_replan_from_script_failure:
                    # Explicitly return to the human treatment gate. An ordinary
                    # script revision keeps the approved treatment instead.
                    project.spec = LongFormSpec(
                        story_contract_version=previous.story_contract_version,
                        brief=previous.brief,
                        narrative={
                            "viewer_promise": previous.brief.viewer_promise,
                            "central_question": previous.brief.central_question,
                        },
                        engagement_ending=previous.brief.engagement_ending,
                        closed_captions=previous.closed_captions,
                    )
                    project.longform_continuity = None
                    project.longform_voices = {}
                    project.longform_word_timings = {}
                    project.longform_runtime_proposal = None
                    project.longform_runtime_approved_seconds = None
                    project.longform_visual_plan = None
                    project.longform_visual_plan_attempts = 0
                    project.longform_media = {}
                    project.longform_semantic_decisions = {}
                    project.longform_semantic_hashes = {}
                    project.longform_semantic_models = {}
                    project.longform_chapters = {}
                    project.longform_chapter_hashes = {}
                    project.longform_image_requests = 0
                    next_state = State.LONGFORM_RESEARCHED
                    research_reused = True
                elif (
                    project.state
                    in {
                        State.LONGFORM_VISUAL_PLANNING,
                        State.VISUAL_PLAN_APPROVAL,
                        State.LONGFORM_VISUAL_PLAN_READY,
                        State.LONGFORM_AUDIO_PREPARING,
                        State.LONGFORM_ASSETS_PREPARING,
                        State.LONGFORM_SEMANTIC_PLANNING,
                        State.LONGFORM_CHAPTER_RENDERING,
                        State.LONGFORM_REVIEW_PREPARING,
                        State.LONGFORM_REVIEW_APPROVAL,
                        State.LONGFORM_MASTER_READY,
                    }
                    and previous.chapters
                ):
                    for chapter in previous.chapters:
                        chapter.visual_beats = []
                    project.spec = previous
                    project.longform_visual_plan = None
                    project.longform_visual_plan_attempts = 0
                    project.longform_media = {}
                    project.longform_semantic_decisions = {}
                    project.longform_semantic_hashes = {}
                    project.longform_semantic_models = {}
                    project.longform_chapters = {}
                    project.longform_chapter_hashes = {}
                    project.longform_image_requests = 0
                    next_state = State.LONGFORM_VISUAL_PLANNING
                    research_reused = True
                elif (
                    project.state
                    in {
                        State.LONGFORM_SCRIPTING,
                        State.SCRIPT_APPROVAL,
                        State.LONGFORM_NARRATIVE_READY,
                    }
                    or was_ceiling_error
                ) and previous.chapters:
                    project.longform_continuity = None
                    project.longform_voices = {}
                    project.longform_word_timings = {}
                    for chapter in previous.chapters:
                        chapter.narration_blocks = []
                        chapter.visual_beats = []
                    project.spec = previous
                    next_state = State.LONGFORM_SCRIPTING
                    research_reused = True
                elif project.state in {
                    State.LONGFORM_RESEARCHED,
                    State.TREATMENT_APPROVAL,
                }:
                    project.longform_continuity = None
                    project.spec = LongFormSpec(
                        story_contract_version=previous.story_contract_version,
                        brief=previous.brief,
                        narrative={
                            "viewer_promise": previous.brief.viewer_promise,
                            "central_question": previous.brief.central_question,
                        },
                        engagement_ending=previous.brief.engagement_ending,
                        closed_captions=previous.closed_captions,
                    )
                    next_state = State.LONGFORM_RESEARCHED
                    research_reused = True
                else:
                    project.longform_continuity = None
                    revised_brief = brief or previous.brief
                    revised_brief.research.requirements.append("Human feedback: " + message)
                    project.spec = LongFormSpec(
                        story_contract_version=previous.story_contract_version,
                        brief=revised_brief,
                        narrative={
                            "viewer_promise": revised_brief.viewer_promise,
                            "central_question": revised_brief.central_question,
                        },
                        engagement_ending=revised_brief.engagement_ending,
                        closed_captions=previous.closed_captions,
                    )
                    project.longform_research_plan = None
                    next_state = State.LONGFORM_FOUNDATION_READY
                    research_reused = False
                old, project.state = project.state, next_state
                output = self.repo.attempt_dir(project, "longform-revision")
                self._json(
                    project,
                    output,
                    "brief.json",
                    project.spec.brief.model_dump(mode="json"),
                    "brief",
                )
                self._json(
                    project,
                    output,
                    "feedback.json",
                    project.feedback[-1].model_dump(mode="json"),
                    "feedback",
                )
                self.repo.save(
                    project,
                    "longform_revised",
                    {
                        "from": old,
                        "to": next_state,
                        "version": project.version,
                        "message": message,
                        "actor": actor,
                        "research_reused": research_reused,
                    },
                )
                return project
            project.version += 1
            project.spec = VideoSpec(
                brief=brief or project.spec.brief,
                closed_captions=project.spec.closed_captions,
            )
            project.prepared_voices = []
            project.timing_repairs = 0
            project.timing_repair_note = ""
            project.timing_rewrite_attempts = 0
            project.production_changes = []
            project.asset_plan = None
            project.asset_plan_attempts = project.image_requests = 0
            project.generated_visuals = []
            project.publication = None
            if project.auto_assets:
                project.spec.brief.music.source_type = "procedural"
                project.spec.brief.music.artifact_ref = None
            project.last_error_code = None
            if project.pipeline == "live":
                project.spec.brief.research.requirements.append("Human feedback: " + message)
            project.warnings = self.warnings_for(project)
            project.last_error = None
            old, project.state = project.state, State.CREATED
            output = self.repo.attempt_dir(project, "revision")
            self._json(
                project, output, "brief.json", project.spec.brief.model_dump(mode="json"), "brief"
            )
            self._json(
                project,
                output,
                "feedback.json",
                project.feedback[-1].model_dump(mode="json"),
                "feedback",
            )
            self.repo.save(
                project,
                "revised",
                {
                    "from": old,
                    "to": State.CREATED,
                    "version": project.version,
                    "message": message,
                    "actor": actor,
                },
            )
            return project

    async def render(self, project_id: str) -> VideoProject:
        project = self.repo.load(project_id)
        if project.review_policy == "preview" and project.state in (
            State.NARRATION_PREPARING,
            State.ASSETS_PREPARING,
            State.AUDIO_ASSEMBLING,
        ):
            return await self.run(project_id)
        if project.state == State.AUDIO_PREPARED or (
            project.state == State.HERO_APPROVAL and not project.pending()
        ):
            return await self.run(project_id)
        if project.state in (State.PROTOTYPE_RENDERED, State.PROTOTYPE_APPROVAL, State.COMPLETED):
            for artifact in project.current_artifacts("prototype"):
                self.repo.verify(project, artifact)
            return project
        raise ValueError("render requires approved story and hero gates; run/approve them first")

    async def rebuild_longform(
        self,
        project_id: str,
        *,
        actor: str = "local-operator",
        semantic_decisions: Path | None = None,
        reuse_approved_semantic: bool = False,
    ) -> VideoProject:
        """Re-render an approved long-form cut using its locked narration and acquired media.

        The previous review remains on disk. Its pending approval is superseded so an
        outdated preview cannot be approved after the new render is ready.
        """
        with self.repo.lock(project_id):
            project = self.repo.load(project_id)
            if (
                not isinstance(project.spec, LongFormSpec)
                or project.state != State.LONGFORM_REVIEW_APPROVAL
            ):
                raise ValueError("rebuild requires a long-form project awaiting prototype review")
            if (
                not project.longform_visual_plan
                or not project.longform_voices
                or not project.longform_media
            ):
                raise ValueError("rebuild requires approved visuals, acquired images and narration")
            pending = project.pending()
            if pending is None or pending.gate != "prototype":
                raise ValueError("rebuild requires a pending prototype approval")
            if reuse_approved_semantic and semantic_decisions is not None:
                raise ValueError("choose saved semantic decisions or an import, not both")
            if reuse_approved_semantic:
                from autonomous_studio.domain.semantic import SemanticDecisionBatch
                from autonomous_studio.rendering.semantic_diagrams import Scene
                from autonomous_studio.rendering.longform import narration_windows
                from autonomous_studio.workflow.motion_planning import plan_motion_chapter
                from autonomous_studio.workflow.semantic_integration import (
                    semantic_input_hash,
                    validate_decision_inputs,
                )
                from autonomous_studio.workflow.semantic_scene_contract import (
                    apply_approved_graphic_visibility,
                )
                from pydantic import TypeAdapter

                adapter = TypeAdapter(Scene)
                revised = {}
                for chapter in project.spec.chapters:
                    batch = project.longform_semantic_decisions.get(chapter.id)
                    if batch is None or batch.schema_version != 2:
                        raise ValueError(
                            f"{chapter.id} has no approved v2 semantic decisions to reuse"
                        )
                    beats = {beat.id: beat.model_dump(mode="json") for beat in chapter.visual_beats}
                    updated = batch.model_copy(deep=True)
                    for decision in updated.decisions:
                        if decision.mode != "diagram":
                            continue
                        beat = beats.get(decision.beat_id)
                        if beat is None:
                            raise ValueError("saved semantic decision has no approved beat")
                        scene = adapter.validate_json(decision.scene_json)
                        scene = apply_approved_graphic_visibility(beat, scene)
                        decision.scene_json = scene.model_dump_json()
                    updated = SemanticDecisionBatch.model_validate(updated)
                    windows = narration_windows(chapter, project.longform_voices)
                    speech_windows = [
                        {
                            "block_id": block.id,
                            "start": start,
                            "end": end,
                            "words": project.longform_word_timings.get(block.id, []),
                        }
                        for block, _, start, end in windows
                    ]
                    base, _ = plan_motion_chapter(
                        project.model_dump(mode="json"), chapter.id, speech_windows,
                        comparison_name=f"production-{chapter.id}",
                        width=project.spec.brief.width,
                        height=project.spec.brief.height,
                        fps=project.spec.brief.fps,
                        family_first=True,
                    )
                    key = semantic_input_hash(project, chapter, base, schema_version=2)
                    if project.longform_semantic_hashes.get(chapter.id) != key:
                        raise ValueError(
                            f"{chapter.id} saved semantic inputs changed; no-call replay "
                            "requires the original approved inputs"
                        )
                    validate_decision_inputs(
                        project.model_dump(mode="json"), chapter.id, updated, base
                    )
                    revised[chapter.id] = updated
                project.longform_semantic_decisions = revised
            if semantic_decisions is not None:
                from autonomous_studio.domain.semantic import SemanticDecisionBatch
                from autonomous_studio.rendering.longform import narration_windows
                from autonomous_studio.workflow.motion_planning import plan_motion_chapter
                from autonomous_studio.workflow.semantic_integration import (
                    semantic_input_hash,
                    validate_decision_inputs,
                )

                if not project.longform_visual_policy.semantic_diagrams:
                    raise ValueError("semantic decisions require semantic diagrams to be enabled")
                imported, hashes = {}, {}
                for chapter in project.spec.chapters:
                    source = semantic_decisions / chapter.id / "semantic-decisions.json"
                    if not source.is_file():
                        raise ValueError(f"missing pilot decisions for {chapter.id}: {source}")
                    batch = SemanticDecisionBatch.model_validate_json(
                        source.read_text(encoding="utf-8")
                    )
                    validate_decision_inputs(project.model_dump(mode="json"), chapter.id, batch)
                    windows = narration_windows(chapter, project.longform_voices)
                    speech_windows = [
                        {
                            "block_id": block.id,
                            "start": start,
                            "end": end,
                            "words": project.longform_word_timings.get(block.id, []),
                        }
                        for block, _, start, end in windows
                    ]
                    base, _ = plan_motion_chapter(
                        project.model_dump(mode="json"),
                        chapter.id,
                        speech_windows,
                        comparison_name=f"production-{chapter.id}",
                        width=project.spec.brief.width,
                        height=project.spec.brief.height,
                        fps=project.spec.brief.fps,
                    )
                    imported[chapter.id] = batch
                    hashes[chapter.id] = semantic_input_hash(project, chapter, base)
                project.longform_semantic_decisions = imported
                project.longform_semantic_hashes = hashes
                project.longform_semantic_models = {
                    chapter_id: project.live_models.get(
                        "semantic_layout_model",
                        project.live_models.get("director_model", "mock-director"),
                    )
                    for chapter_id in imported
                }
            else:
                if not reuse_approved_semantic:
                    project.longform_semantic_decisions = {}
                    project.longform_semantic_hashes = {}
                    project.longform_semantic_models = {}
            pending.decision = "superseded"
            pending.actor = actor
            pending.message = "Superseded by long-form production rebuild"
            pending.decided_at = now()
            if reuse_approved_semantic:
                project.version += 1
                project.creation_settings["semantic_replay_only_version"] = project.version
            project.longform_chapter_hashes = {}
            project.longform_chapters = {}
            project.last_error = project.last_error_code = None
            previous = project.state
            project.state = State.LONGFORM_SEMANTIC_PLANNING
            self.repo.save(
                project,
                "longform_motion_rebuild_requested",
                {
                    "from": previous,
                    "to": project.state,
                    "actor": actor,
                    "version": project.version,
                    "superseded_approval_id": pending.id,
                    "semantic_decisions_imported": len(project.longform_semantic_decisions),
                    "approved_semantic_reused": reuse_approved_semantic,
                },
            )
        return await self.run(project_id)
