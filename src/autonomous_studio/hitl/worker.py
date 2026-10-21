"""Single-worker Telegram polling with durable offsets and project-based job recovery."""

import asyncio
import hashlib

from google.auth.exceptions import RefreshError

from autonomous_studio.audio.timing import MAX_TIMING_REPAIRS, is_timing_failure
from autonomous_studio.domain.models import LongFormSpec, State
from autonomous_studio.hitl.adapters import review_message
from autonomous_studio.providers.http import RemoteError


def legacy_longform_duration_failure(project):
    return bool(
        project.spec.format == "youtube_longform"
        and project.state == State.LONGFORM_SCRIPTING
        and project.last_error_code == "provider_validation"
        and project.last_error
        and "Estimated speech is" in project.last_error
        and "keep it at or below" in project.last_error
    )


class TelegramWorker:
    def __init__(self, studio, adapter, factory, youtube=None):
        self.studio, self.adapter, self.factory = studio, adapter, factory
        self.repo = studio.repo
        bot = adapter.config.bot_token.get_secret_value().split(":", 1)[0]
        self.prefix = f"telegram:{bot}"
        self.tasks = {}
        self.youtube = youtube
        self.upload_tasks = {}

    def youtube_publisher(self):
        if self.youtube is None:
            from autonomous_studio.providers.youtube import YouTubePublisher

            self.youtube = YouTubePublisher(self.repo)
        return self.youtube

    async def upload_youtube_short(self, project_id):
        try:
            result = await asyncio.to_thread(self.youtube_publisher().publish, project_id)
            if result.status == "uploaded":
                await self.adapter.text(
                    f"Project {project_id} uploaded privately to YouTube.\n"
                    f"Video ID: {result.video_id}\n"
                    f"Open: https://studio.youtube.com/video/{result.video_id}/edit"
                )
            elif result.status == "uploaded_thumbnail_failed":
                await self.adapter.text(
                    f"Project {project_id} was uploaded privately as {result.video_id}, but the "
                    "custom thumbnail failed. Run the same command to retry only the thumbnail."
                )
            else:
                await self.adapter.text(
                    f"Project {project_id} upload outcome is uncertain. No automatic retry will "
                    "occur. Inspect YouTube Studio before taking further action."
                )
        except RefreshError:
            await self.adapter.text(
                f"Could not upload project {project_id}: the saved YouTube access token "
                "could not be refreshed. No upload was started. Try /refreshyoutubeauth; "
                "if Google rejects the refresh token, run `studio youtube-auth` locally."
            )
        except (ValueError, RuntimeError, OSError) as exc:
            await self.adapter.text(f"Could not upload project {project_id}: {str(exc)[:500]}")
        except Exception:
            await self.adapter.text(
                f"Could not confirm the YouTube upload for {project_id}. Check YouTube "
                "Studio and the project's upload receipt before sending the command again."
            )

    async def refresh_youtube_authorization(self):
        try:
            result = await asyncio.to_thread(self.youtube_publisher().refresh_authorization)
        except ValueError as exc:
            await self.adapter.text(f"YouTube token refresh failed: {str(exc)[:500]}")
        except Exception:
            await self.adapter.text(
                "YouTube token refresh failed while contacting Google or verifying the "
                "channel. No token was saved and no video was uploaded. Try again when "
                "the connection is stable."
            )
        else:
            await self.adapter.text(
                "YouTube access token renewed for "
                f"{result['channel_title']}. "
                f"Expires at {result['expires_at_utc'] or 'an unspecified time'} (UTC). "
                "No video was uploaded."
            )

    @property
    def image_budget_key(self):
        return self.prefix + ":image_budget"

    def preferred_image_budget(self):
        return self.repo.get_runtime(self.image_budget_key)

    @property
    def longform_image_budget_key(self):
        return self.prefix + ":longform_image_budget"

    def preferred_longform_image_budget(self):
        return self.repo.get_runtime(self.longform_image_budget_key)

    @property
    def longform_tts_key(self):
        return self.prefix + ":longform_tts_provider"

    @property
    def tts_provider_key(self):
        return self.prefix + ":tts_provider"

    def preferred_tts_provider(self):
        # Carry forward a previously saved /longformtts preference without
        # mutating any existing project or requiring a settings migration.
        return self.repo.get_runtime(self.tts_provider_key) or self.repo.get_runtime(
            self.longform_tts_key
        )

    def project_longform_tts(self):
        from autonomous_studio.config import Settings

        return self.preferred_tts_provider() or (
            self.studio.settings or Settings.environment()
        ).tts_provider

    def project_longform_image_budget(self):
        from autonomous_studio.domain.models import LongFormVisualPolicy

        saved = self.preferred_longform_image_budget()
        return (
            int(saved) if saved is not None else LongFormVisualPolicy().maximum_generated_requests
        )

    @property
    def closed_caption_key(self):
        return self.prefix + ":closed_captions"

    def preferred_closed_captions(self):
        return self.repo.get_runtime(self.closed_caption_key)

    def project_closed_captions(self):
        from autonomous_studio.config import Settings

        saved = self.preferred_closed_captions()
        if saved is not None:
            return bool(saved)
        return (self.studio.settings or Settings.environment()).closed_captions

    def project_longform_closed_captions(self):
        saved = self.preferred_closed_captions()
        return bool(saved) if saved is not None else False

    def project_image_budget(self):
        from autonomous_studio.config import Settings

        settings = self.studio.settings or Settings.environment()
        return self.preferred_image_budget() if settings.auto_assets else None

    def creation_settings(self):
        from autonomous_studio.config import Settings

        settings = self.studio.settings or Settings.environment()
        saved_budget = self.preferred_image_budget()
        saved_longform_budget = self.preferred_longform_image_budget()
        saved_captions = self.preferred_closed_captions()
        return {
            "closed_captions": self.project_closed_captions(),
            "closed_captions_source": (
                "telegram_saved" if saved_captions is not None else "environment_default"
            ),
            "image_budget": (
                saved_budget if saved_budget is not None else settings.max_image_requests
            ),
            "image_budget_source": (
                "telegram_saved" if saved_budget is not None else "environment_default"
            ),
            "longform_image_budget": self.project_longform_image_budget(),
            "longform_image_budget_source": (
                "telegram_saved" if saved_longform_budget is not None else "longform_default"
            ),
            "tts_provider": self.project_longform_tts(),
            "tts_provider_source": "telegram_saved" if self.preferred_tts_provider() else "default",
            "longform_tts_provider": self.project_longform_tts(),
            "longform_tts_source": (
                "telegram_saved" if self.preferred_tts_provider() else "default"
            ),
            "youtube_category_id": settings.youtube_category_id,
            "youtube_category_source": "environment_default",
        }

    def settings_text(self):
        current = self.creation_settings()
        return (
            "Future-project settings:\n"
            f"Captions: {'yes' if current['closed_captions'] else 'no'} "
            f"({current['closed_captions_source']})\n"
            f"Generated-image budget: {current['image_budget']} "
            f"({current['image_budget_source']})\n"
            f"Long-form generated-image budget: {current['longform_image_budget']} "
            f"({current['longform_image_budget_source']})\n"
            f"Narration (Shorts and long-form): {current['tts_provider']} "
            f"({current['tts_provider_source']})\n"
            f"YouTube category: Science & Technology "
            f"({current['youtube_category_id']})"
        )

    def approval_project(self, approval_id):
        for project in self.repo.all_projects():
            if any(a.id == approval_id for a in project.approvals):
                return project
        raise ValueError("Unknown review request")

    async def answer(self, callback_id, text, alert=False):
        try:
            await self.adapter.call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_id,
                    "text": text[:150],
                    "show_alert": alert,
                },
            )
        except RemoteError as exc:
            # Expired callback acknowledgements must not pin the durable update offset.
            if exc.status != 400:
                raise

    async def process(self, update):
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        sender = callback.get("from", {}) if callback else message.get("from", {})
        try:
            self.adapter.validate_sender(message.get("chat", {}).get("id"), sender.get("id"))
        except ValueError:
            return  # Unauthorized updates never receive project information.
        external = f"{self.prefix}:{update['update_id']}"
        if self.repo.get_runtime(external):
            return
        actor = f"telegram:{sender['id']}"
        try:
            if callback:
                action, approval_id = callback.get("data", "").split(":", 1)
                project = self.approval_project(approval_id)
                pending = project.pending()
                if not pending or pending.id != approval_id:
                    raise ValueError("This review request is no longer pending")
                if action == "revise":
                    instruction = (
                        "Reply with title:, description:, and/or thumbnail: <candidate number> "
                        "for a metadata-only revision. Other feedback starts a creative revision."
                        if pending.gate == "publication"
                        else "Reply with feedback for this review."
                    )
                    prompt = await self.adapter.text(
                        instruction,
                        reply_markup={"force_reply": True, "selective": True},
                    )
                    self.repo.set_runtime(
                        f"{self.prefix}:reply:{prompt['message_id']}", approval_id
                    )
                elif action == "trim":
                    if pending.gate != "runtime":
                        raise ValueError("trim is available only at long-form runtime review")
                    proposal = project.longform_runtime_proposal
                    target = max(150, proposal.prior_ceiling_seconds - 12)
                    self.studio.revise(
                        project.id,
                        (
                            f"Trim the approved narration so measured speech can fit about "
                            f"{target:.0f} seconds with visual breathing room. Preserve the "
                            "story, sourced claims, natural voice, and engagement ending. "
                            "Cut repetition and unnecessary recap, especially in chapters "
                            "whose measured speech exceeds their visual allocation. Present "
                            "the complete revised script for fresh approval before images."
                        ),
                        actor=actor,
                        approval_id=approval_id,
                        expected_version=pending.version,
                        external_id=external,
                    )
                    await self.answer(callback["id"], "Script trim queued")
                    await self.adapter.text(
                        f"{project.id}: Trim requested. I’ll send the revised complete script "
                        "for approval before generating images."
                    )
                elif action in ("approve", "reject"):
                    gate = pending.gate
                    project = self.studio.decide(
                        project.id,
                        gate,
                        action,
                        actor=actor,
                        approval_id=approval_id,
                        version=pending.version,
                    )
                    await self.answer(
                        callback["id"],
                        (
                            "Script approved"
                            if action == "approve" and gate == "script"
                            else (
                                "Outline approved"
                                if action == "approve" and gate == "treatment"
                                else (
                                    "Visual plan approved"
                                    if action == "approve" and gate == "visual_plan"
                                    else "Longer runtime approved"
                                    if action == "approve" and gate == "runtime"
                                    else "Recorded"
                                )
                            )
                        ),
                    )
                    if action == "approve" and gate == "script":
                        if project.spec.format == "youtube_longform":
                            report = project.longform_continuity
                            runtime = (
                                f" Approved timeline: {project.spec.brief.duration:.1f}s."
                                if report and report.duration_status == "expansion_proposed"
                                else ""
                            )
                            await self.adapter.text(
                                f"{project.id}: Long-form script approved. The narrative package "
                                f"is ready. Preparing the rights-aware visual plan and contact "
                                f"sheets now.{runtime}"
                            )
                        else:
                            await self.adapter.text(
                                f"{project.id}: Script approved. Preparing visuals, music, "
                                "audio mix, and the final preview now."
                            )
                    elif action == "approve" and gate == "treatment":
                        await self.adapter.text(
                            f"{project.id}: Treatment approved. Writing and validating the "
                            "complete chapter-based narration now."
                        )
                    elif action == "approve" and gate == "visual_plan":
                        await self.adapter.text(
                            f"{project.id}: Visual plan approved. Preparing narration and media, "
                            "then rendering a chapter-validated review video."
                        )
                    elif action == "approve" and gate == "runtime":
                        await self.adapter.text(
                            f"{project.id}: Keeping the approved script. Extending the timeline "
                            f"to about {project.longform_runtime_approved_seconds:.0f}s, then "
                            "continuing image preparation and rendering."
                        )
                    elif (
                        action == "approve"
                        and gate == "prototype"
                        and isinstance(project.spec, LongFormSpec)
                        and project.state == State.PUBLICATION_CLEARANCE
                    ):
                        await self.adapter.text(
                            f"{project.id}: Video approved. Preparing the YouTube title, "
                            "description, thumbnail and rights notes for a separate "
                            "publication review. No upload will occur automatically."
                        )
                else:
                    raise ValueError("Unknown review action")
                if action == "revise":
                    await self.answer(callback["id"], "Recorded")
            else:
                text = message.get("text", "").strip()
                reply_id = message.get("reply_to_message", {}).get("message_id")
                approval_id = (
                    self.repo.get_runtime(f"{self.prefix}:reply:{reply_id}") if reply_id else None
                )
                if approval_id:
                    project = self.approval_project(approval_id)
                    self.studio.revise(
                        project.id, text, actor=actor, approval_id=approval_id, external_id=external
                    )
                    await self.adapter.text("Feedback saved. I’ll develop a new version.")
                elif text.startswith("/topic "):
                    topic = text.split(" ", 1)[1].strip()
                    project_id = hashlib.sha256(external.encode()).hexdigest()[:12]
                    if not any(p.id == project_id for p in self.repo.all_projects()):
                        project = self.studio.create_topic(
                            topic,
                            project_id=project_id,
                            image_budget=self.project_image_budget(),
                            closed_captions=self.project_closed_captions(),
                            creation_settings=self.creation_settings(),
                        )
                    else:
                        project = self.repo.load(project_id)
                    await self.adapter.text(
                        f"Project {project_id} created. Research is queued.\n"
                        f"Captions: {'yes' if project.spec.closed_captions else 'no'}\n"
                        "Generated-image budget: "
                        + (
                            str(project.auto_assets.max_image_requests)
                            if project.auto_assets
                            else "disabled"
                        )
                    )
                elif text.startswith("/full "):
                    request = text.split(" ", 1)[1].strip()
                    first, separator, remainder = request.partition(" ")
                    rank_count = int(first) if first.isdigit() and separator else None
                    topic = remainder.strip() if rank_count is not None else request
                    project_id = hashlib.sha256(external.encode()).hexdigest()[:12]
                    if not any(p.id == project_id for p in self.repo.all_projects()):
                        project = self.studio.create_topic(
                            topic,
                            project_id=project_id,
                            duration=55,
                            production_mode="full",
                            expected_rank_count=rank_count,
                            image_budget=self.project_image_budget(),
                            closed_captions=self.project_closed_captions(),
                            creation_settings=self.creation_settings(),
                        )
                    else:
                        project = self.repo.load(project_id)
                    await self.adapter.text(
                        f"Full production {project_id} created. Research is queued.\n"
                        f"Captions: {'yes' if project.spec.closed_captions else 'no'}\n"
                        "Generated-image budget: "
                        + (
                            str(project.auto_assets.max_image_requests)
                            if project.auto_assets
                            else "disabled"
                        )
                        + "\n"
                        "A timing-validated script review will arrive before images are prepared."
                    )
                elif text.startswith("/longform "):
                    topic = text.split(" ", 1)[1].strip()
                    project_id = hashlib.sha256(external.encode()).hexdigest()[:12]
                    captions = self.project_longform_closed_captions()
                    settings = self.creation_settings()
                    settings.update(
                        {
                            "closed_captions": captions,
                            "closed_captions_source": (
                                "telegram_saved"
                                if self.preferred_closed_captions() is not None
                                else "longform_default"
                            ),
                        }
                    )
                    if not any(p.id == project_id for p in self.repo.all_projects()):
                        project = self.studio.create_longform_topic(
                            topic,
                            project_id=project_id,
                            image_budget=self.project_longform_image_budget(),
                            closed_captions=captions,
                            creation_settings=settings,
                        )
                    else:
                        project = self.repo.load(project_id)
                    await self.adapter.text(
                        f"Long-form pilot {project_id} created. Research is queued.\n"
                        f"Target: {project.spec.brief.duration:.0f}s at 1920×1080.\n"
                        f"Captions: {'yes' if project.spec.closed_captions else 'no'}.\n"
                        "Generated-image planning budget: "
                        f"{project.longform_visual_policy.maximum_generated_requests}.\n"
                        "A treatment and chapter outline will arrive for approval before the "
                        "complete script is written."
                    )
                elif text == "/settings":
                    await self.adapter.text(self.settings_text())
                elif text and text.split()[0] in {"/ttsprovider", "/longformtts"}:
                    parts = text.split()
                    if len(parts) == 1:
                        await self.adapter.text(
                            "Future Shorts and long-form narration: "
                            f"{self.project_longform_tts()}. "
                            "Gemini uses Aoede; OpenAI uses its configured voice. "
                            "Existing projects are unchanged."
                        )
                    elif len(parts) == 2 and parts[1].lower() in {"gemini", "openai", "reset"}:
                        provider = None if parts[1].lower() == "reset" else parts[1].lower()
                        self.repo.set_runtime_audited(
                            self.tts_provider_key, provider, actor=actor, external_id=external
                        )
                        if provider is None:
                            self.repo.set_runtime_audited(
                                self.longform_tts_key, None, actor=actor, external_id=external
                            )
                        await self.adapter.text(
                            "Future Shorts and long-form narration: "
                            f"{self.project_longform_tts()}. "
                            "Existing projects are unchanged."
                        )
                    else:
                        raise ValueError("use /ttsprovider [gemini|openai|reset]")
                elif text == "/longformimagebudget" or text.startswith("/longformimagebudget "):
                    parts = text.split()
                    if len(parts) == 1:
                        saved = self.preferred_longform_image_budget()
                        default = self.project_longform_image_budget()
                        label = f"{saved} (saved)" if saved is not None else f"{default} (default)"
                        await self.adapter.text(
                            f"Future long-form image budget: {label}. Allowed range: 4–30."
                        )
                    elif len(parts) == 2 and parts[1].lower() == "reset":
                        self.repo.set_runtime_audited(
                            self.longform_image_budget_key,
                            None,
                            actor=actor,
                            external_id=external,
                        )
                        await self.adapter.text(
                            "Saved long-form image budget cleared. Future long-form projects "
                            "will use the default of 30; existing projects are unchanged."
                        )
                    elif len(parts) == 2 and parts[1].isdigit() and 4 <= int(parts[1]) <= 30:
                        budget = int(parts[1])
                        self.repo.set_runtime_audited(
                            self.longform_image_budget_key,
                            budget,
                            actor=actor,
                            external_id=external,
                        )
                        await self.adapter.text(
                            f"Future long-form projects may request up to {budget} generated "
                            "images each. Existing projects are unchanged."
                        )
                    else:
                        raise ValueError(
                            "use /longformimagebudget <4-30>, /longformimagebudget, or "
                            "/longformimagebudget reset"
                        )
                elif text == "/imagebudget" or text.startswith("/imagebudget "):
                    from autonomous_studio.config import Settings

                    parts = text.split()
                    if len(parts) == 1:
                        saved = self.preferred_image_budget()
                        default = (
                            self.studio.settings or Settings.environment()
                        ).max_image_requests
                        label = f"{saved} (saved)" if saved is not None else f"{default} (default)"
                        await self.adapter.text(
                            f"Future-project image budget: {label}. Allowed range: 1–18."
                        )
                    elif len(parts) == 2 and parts[1].lower() == "reset":
                        self.repo.set_runtime_audited(
                            self.image_budget_key,
                            None,
                            actor=actor,
                            external_id=external,
                        )
                        await self.adapter.text(
                            "Saved image budget cleared. Future projects will use the configured "
                            "default; existing projects are unchanged."
                        )
                    elif len(parts) == 2 and parts[1].isdigit() and 1 <= int(parts[1]) <= 18:
                        budget = int(parts[1])
                        self.repo.set_runtime_audited(
                            self.image_budget_key,
                            budget,
                            actor=actor,
                            external_id=external,
                        )
                        await self.adapter.text(
                            f"Future projects may request up to {budget} generated images each. "
                            "Existing projects are unchanged."
                        )
                    else:
                        raise ValueError(
                            "use /imagebudget <1-18>, /imagebudget, or /imagebudget reset"
                        )
                elif (
                    text in {"/closedcaption", "/closed-caption"}
                    or text.startswith("/closedcaption ")
                    or text.startswith("/closed-caption ")
                ):
                    parts = text.split()
                    if len(parts) == 1:
                        state = "yes" if self.project_closed_captions() else "no"
                        source = (
                            "saved" if self.preferred_closed_captions() is not None else "default"
                        )
                        await self.adapter.text(
                            f"Future-project burned-in captions: {state} ({source}). "
                            "Existing projects are unchanged."
                        )
                    elif len(parts) == 2 and parts[1].lower() in {"yes", "no"}:
                        enabled = parts[1].lower() == "yes"
                        self.repo.set_runtime_audited(
                            self.closed_caption_key,
                            enabled,
                            actor=actor,
                            external_id=external,
                        )
                        await self.adapter.text(
                            "Burned-in captions will be "
                            + ("included" if enabled else "omitted")
                            + " for future projects. Existing projects are unchanged."
                        )
                    else:
                        raise ValueError(
                            "use /closedcaption yes, /closedcaption no, or /closedcaption"
                        )
                elif text.startswith("/status "):
                    project = self.repo.load(text.split()[1])
                    await self.adapter.text(
                        f"{project.id}: {project.state}, version {project.version}.\n"
                        + (project.last_error or "")
                    )
                elif text.startswith("/uploadyoutubeshorts "):
                    parts = text.split()
                    if len(parts) != 2:
                        raise ValueError("use /uploadyoutubeshorts <project-id>")
                    project_id = parts[1]
                    project = self.repo.load(project_id)
                    if project.state != State.COMPLETED:
                        raise ValueError("YouTube upload requires a completed project")
                    active = self.upload_tasks.get(project_id)
                    if active and not active.done():
                        raise ValueError("a YouTube upload is already running for this project")
                    await self.adapter.text(
                        f"Private YouTube upload queued for {project_id}. I’ll send the video link "
                        "after YouTube accepts it."
                    )
                    self.upload_tasks[project_id] = asyncio.create_task(
                        self.upload_youtube_short(project_id)
                    )
                elif text == "/refreshyoutubeauth":
                    if any(not task.done() for task in self.upload_tasks.values()):
                        raise ValueError("Wait for the active YouTube upload to finish")
                    await self.adapter.text(
                        "Refreshing saved YouTube access token; no upload will start."
                    )
                    await self.refresh_youtube_authorization()
                elif text.startswith("/retry "):
                    project_id = text.split()[1]
                    self.studio.request_retry(project_id, actor=actor)
                    await self.adapter.text("Retry queued.")
                elif text.startswith("/repair "):
                    _, project_id, version = text.split()
                    self.studio.request_retry(
                        project_id, actor=actor, repair=True, version=int(version)
                    )
                    await self.adapter.text(
                        "Timing repair queued. Any changed script or timing "
                        "will come back for approval; no speech is sped up."
                    )
                elif text.startswith("/revise "):
                    # Explicit version prevents delayed commands revising newer work.
                    _, project_id, version, feedback = text.split(" ", 3)
                    self.studio.revise(
                        project_id,
                        feedback,
                        expected_version=int(version),
                        actor=actor,
                        external_id=external,
                    )
                    await self.adapter.text("Revision queued.")
                else:
                    await self.adapter.text(
                        "Autonomous Studio is ready.\n/topic <topic>\n"
                        "/full [rank-count] <topic>\n"
                        "/longform <topic>\n"
                        "/imagebudget <1-18> (show with /imagebudget; clear with reset)\n"
                        "/longformimagebudget <4-30> (default 30; persisted for future projects)\n"
                        "/ttsprovider gemini|openai|reset (show with /ttsprovider)\n"
                        "/closedcaption yes|no (alias: /closed-caption)\n"
                        "/uploadyoutubeshorts <completed-project-id>\n"
                        "/refreshyoutubeauth (renew saved YouTube access token)\n"
                        "/settings\n"
                        "/status <project-id>\n"
                        "/retry <project-id>\n/revise <project-id> <version> <feedback>\n"
                        "/repair <project-id> <version>\n"
                        "Use the review buttons to approve, revise or reject."
                    )
        except (ValueError, IndexError, RuntimeError) as exc:
            if isinstance(exc, RemoteError):
                raise
            if callback:
                await self.answer(callback["id"], str(exc), alert=True)
            else:
                await self.adapter.text("Could not apply that command: " + str(exc)[:500])
        self.repo.set_runtime(external, True)

    async def poll(self, timeout=10):
        offset = self.repo.get_runtime(self.prefix + ":offset", 0)
        updates = await self.adapter.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        for update in sorted(updates, key=lambda u: u["update_id"]):
            await self.process(update)
            self.repo.set_runtime(self.prefix + ":offset", update["update_id"] + 1)

    async def work(self, launch=True):
        for project_id, task in list(self.upload_tasks.items()):
            if task.done():
                try:
                    task.result()
                except Exception:
                    pass
                del self.upload_tasks[project_id]
        for project_id, task in list(self.tasks.items()):
            if task.done():
                try:
                    task.result()
                except Exception:
                    pass  # Studio persisted a sanitized failure; notification follows below.
                del self.tasks[project_id]
        for project in self.repo.all_projects():
            if project.pipeline != "live":
                continue
            if project.pending():
                await self.adapter.deliver(review_message(project, project.pending()))
            elif project.last_error:
                if (
                    is_timing_failure(project)
                    and project.review_policy == "preview"
                    and project.timing_rewrite_attempts >= MAX_TIMING_REPAIRS
                ):
                    self.studio.request_retry(project.id, actor="telegram-worker", repair=True)
                    continue
                key = f"{self.prefix}:error:{project.id}:{project.revision}"
                if not self.repo.get_runtime(key):
                    if is_timing_failure(project):
                        used_repairs = (
                            project.timing_rewrite_attempts
                            if project.review_policy == "preview"
                            else project.timing_repairs
                        )
                        action = (
                            f"/repair {project.id} {project.version}\n"
                            if used_repairs < MAX_TIMING_REPAIRS
                            else ""
                        )
                        action += "Unchanged retry cannot fix narration timing.\n"
                    elif legacy_longform_duration_failure(project):
                        action = (
                            f"/retry {project.id}\n"
                            "The elastic-duration policy can now reassess this script.\n"
                        )
                    elif project.last_error_code in {
                        "provider_validation",
                        "asset_planning",
                        "longform_visual_planning",
                        "longform_narration_ceiling",
                    }:
                        action = (
                            "The first creative draft failed validation; unchanged retry is "
                            "not useful. Review the saved diagnostic before revising.\n"
                            if project.longform_creative_policy is not None
                            else "Internal validation retries are exhausted; unchanged retry "
                            "is not useful.\n"
                        )
                    else:
                        action = f"/retry {project.id}\n"
                    await self.adapter.text(
                        f"Project {project.id} needs attention:\n"
                        f"{project.last_error[:800]}\n{action}"
                        f"Or /revise {project.id} {project.version} <feedback>"
                    )
                    self.repo.set_runtime(key, True)
            elif project.state in (
                State.COMPLETED,
                State.REJECTED,
                State.LONGFORM_MASTER_READY,
            ):
                key = f"{self.prefix}:terminal:{project.id}:{project.revision}"
                if not self.repo.get_runtime(key):
                    message = f"Project {project.id}: {project.state}."
                    await self.adapter.text(message)
                    self.repo.set_runtime(key, True)
            elif launch and not self.tasks:
                # Rendering/SQLite use blocking native libraries. Run the production job on
                # a separate event-loop thread so Telegram remains responsive during it.
                self.tasks[project.id] = asyncio.create_task(
                    asyncio.to_thread(lambda pid=project.id: asyncio.run(self.factory().run(pid)))
                )

    async def run(self, once=False):
        info = await self.adapter.call("getWebhookInfo", {})
        if info.get("url"):
            raise ValueError("Bot has an active webhook; remove it before starting long polling")
        # A separate named OS lock prevents two workers sharing the bot offset/outbox.
        lock_id = hashlib.sha256(self.prefix.encode()).hexdigest()[:12]
        with self.repo.lock(lock_id):
            while True:
                try:
                    await self.poll(0 if once else 10)
                    await self.work()
                except RuntimeError as exc:
                    if once or not isinstance(exc, RemoteError) or exc.status in (401, 403, 409):
                        raise
                    print(f"Temporary transport failure: {exc}. Retrying in 5 seconds.", flush=True)
                    await asyncio.sleep(5)
                    continue
                if once:
                    if self.tasks:
                        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
                    if self.upload_tasks:
                        await asyncio.gather(*self.upload_tasks.values(), return_exceptions=True)
                    await self.work(launch=False)
                    return
