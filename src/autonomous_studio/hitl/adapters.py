from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, SecretStr, field_validator

from autonomous_studio.domain.models import (
    Approval,
    Artifact,
    LongFormSpec,
    Model,
    VideoProject,
)
from autonomous_studio.providers.http import RemoteError
from autonomous_studio.storage import Repository
from autonomous_studio.storage.repository import write_json


class ReviewMessage(Model):
    project_id: str
    approval_id: str
    version: int
    gate: str
    text: str
    media: list[Artifact]
    actions: list[Literal["approve", "revise", "trim", "reject"]] = ["approve", "revise", "reject"]


class HITLProvider(Protocol):
    async def send(self, message: ReviewMessage, output: Path) -> Path: ...


class LocalHITL:
    async def send(self, message: ReviewMessage, output: Path) -> Path:
        path = output / "review-request.json"
        write_json(path, message.model_dump(mode="json"))
        return path


class TelegramConfig(Model):
    bot_token: SecretStr
    chat_id: int
    allowed_user_ids: list[int] = Field(min_length=1)

    @field_validator("bot_token")
    @classmethod
    def token_shape(cls, token):
        import re

        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token.get_secret_value()):
            raise ValueError("invalid Telegram bot token format")
        return token


class TelegramAdapter:
    """Allowlisted Telegram transport with a checkpointed, at-least-once review outbox."""

    def __init__(self, config: TelegramConfig, repo=None, http=None):
        self.config = config
        self.repo = repo
        from autonomous_studio.providers.http import HTTP

        self.http = http or HTTP()

    def payloads(self, message: ReviewMessage) -> list[dict]:
        runtime_labels = {
            "approve": "Keep script · extend",
            "trim": "Trim script",
            "reject": "Stop",
        }
        short_runtime_labels = {
            "approve": "Keep script · extend",
            "revise": "Request edits",
            "reject": "Stop",
        }
        buttons = [
            [
                {
                    "text": (
                        runtime_labels[action]
                        if message.gate == "runtime"
                        else short_runtime_labels[action]
                        if message.gate == "narration" and "approve" in message.actions
                        else action.title()
                    ),
                    "callback_data": f"{action}:{message.approval_id}",
                }
                for action in message.actions
            ]
        ]
        payloads = [
            {
                "method": "sendMessage",
                "chat_id": self.config.chat_id,
                "text": message.text,
                "reply_markup": {"inline_keyboard": buttons},
            }
        ]
        methods = {
            ".png": "sendPhoto",
            ".jpg": "sendPhoto",
            ".wav": "sendDocument",
            ".m4a": "sendAudio",
            ".mp3": "sendAudio",
            ".mp4": "sendVideo",
        }
        for asset in message.media:
            method = methods.get(Path(asset.path).suffix)
            if method:
                payloads.append(
                    {"method": method, "chat_id": self.config.chat_id, "local_artifact": asset.path}
                )
        return payloads

    def validate_sender(self, chat_id: int, user_id: int):
        if chat_id != self.config.chat_id or user_id not in self.config.allowed_user_ids:
            raise ValueError("unauthorized Telegram sender")

    async def send(self, message: ReviewMessage, output: Path) -> Path:
        if self.repo is None:
            raise NotImplementedError("Telegram delivery requires a project repository")
        path = await LocalHITL().send(message, output)
        await self.deliver(message)
        return path

    async def call(self, method, payload):
        url = f"https://api.telegram.org/bot{self.config.bot_token.get_secret_value()}/{method}"
        result = await self.http.json(url, payload, timeout=40, service="Telegram")
        if not result.get("ok"):
            raise RuntimeError(f"Telegram {method} failed (code {result.get('error_code')})")
        return result.get("result")

    async def text(self, text, **extra):
        return await self.call(
            "sendMessage", {"chat_id": self.config.chat_id, "text": text[:3500], **extra}
        )

    async def upload(self, method, path, caption):
        import json
        import mimetypes
        from uuid import uuid4

        if path.stat().st_size > 49_000_000:
            raise ValueError("Telegram artifact exceeds upload size limit")
        boundary = uuid4().hex
        body = bytearray()
        for key, value in {"chat_id": self.config.chat_id, "caption": caption[:900]}.items():
            body.extend(
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
                    f"\r\n\r\n{value}\r\n"
                ).encode()
            )
        field = {
            "sendPhoto": "photo",
            "sendVideo": "video",
            "sendAudio": "audio",
            "sendDocument": "document",
        }[method]
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body.extend(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                f'filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'
            ).encode()
        )
        body.extend(path.read_bytes())
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        result = json.loads(
            await self.http.post(
                f"https://api.telegram.org/bot{self.config.bot_token.get_secret_value()}/{method}",
                bytes(body),
                {"Content-Type": f"multipart/form-data; boundary={boundary}"},
                timeout=90,
                service="Telegram upload",
            )
        )
        if not result.get("ok"):
            raise RuntimeError("Telegram upload failed")
        return result["result"]

    async def deliver(self, message):
        project = self.repo.load(message.project_id)
        approval = project.pending()
        if not approval or approval.id != message.approval_id:
            return
        verify_approval(self.repo, project, approval)
        roles = {
            "story": set(),
            "hero": {"hero_frame"},
            "narration": set(),
            "treatment": set(),
            "script": set(),
            "visual_plan": {"longform_contact_sheet"},
            "runtime": set(),
            "prototype": {"prototype"},
            "publication": {"thumbnail_candidate"},
        }[message.gate]
        media = [a for a in message.media if a.role in roles]
        items = [
            ("text", text)
            for text in [message.text[i : i + 2000] for i in range(0, len(message.text), 2000)]
        ]
        items += [("media", asset) for asset in media]
        items += [("buttons", None)]  # Actions follow the evidence they approve.
        bot = self.config.bot_token.get_secret_value().split(":", 1)[0]
        for index, (kind, value) in enumerate(items):
            key = f"delivery:{bot}:{message.approval_id}:{index}"
            if self.repo.get_runtime(key):
                continue
            if kind == "text":
                result = await self.text(value)
            elif kind == "buttons":
                buttons = self.payloads(message)[0]["reply_markup"]
                instruction = (
                    "This script is not timing-valid yet, so approval is unavailable. "
                    "Revise to identify what should be trimmed, or reject to stop."
                    if message.gate == "narration" and "approve" not in message.actions
                    else (
                        "Keep the approved script at the proposed length, request a trimmed "
                        "script, or stop."
                    )
                    if message.gate == "runtime"
                    else (
                        "Keep this script at the proposed runtime, request edits, or stop."
                        if message.gate == "narration" and "approve" in message.actions
                        else "Approve to continue, revise to send feedback, or reject to stop."
                    )
                )
                result = await self.text(
                    f"{project.id} / v{project.version} / {message.gate}\n" + instruction,
                    reply_markup=buttons,
                )
            else:
                path = self.repo.verify(project, value)
                if (
                    path.suffix.lower() == ".mp4"
                    and path.stat().st_size > 49_000_000
                    and not isinstance(project.spec, LongFormSpec)
                ):
                    import shutil

                    from autonomous_studio.storage.repository import digest

                    local = self.repo.project_dir(project.id) / (
                        f"review-video-v{project.version:03d}.mp4"
                    )
                    if local.exists():
                        if digest(local) != value.sha256:
                            raise ValueError("existing local review copy differs from approved MP4")
                    else:
                        shutil.copyfile(path, local)
                        if digest(local) != value.sha256:
                            raise ValueError("local review copy checksum mismatch")
                    result = await self.text(
                        "The validated MP4 exceeds Telegram's upload limit. "
                        f"Review the unchanged local copy at {local} before approving."
                    )
                    self.repo.set_runtime(key, result.get("message_id", True))
                    continue
                method = {
                    ".png": "sendPhoto",
                    ".jpg": "sendPhoto",
                    ".mp4": "sendVideo",
                    ".mp3": "sendAudio",
                    ".m4a": "sendAudio",
                }.get(path.suffix, "sendDocument")
                try:
                    result = await self.upload(
                        method, path, f"{message.gate} / {value.role} / v{project.version}"
                    )
                except RemoteError as exc:
                    if (
                        not isinstance(project.spec, LongFormSpec)
                        or message.gate != "prototype"
                        or path.suffix.lower() != ".mp4"
                        or exc.status is not None
                    ):
                        raise
                    # A timeout is ambiguous: Telegram may have accepted the upload.
                    # Do not retry the same large file indefinitely; give the reviewer
                    # an unchanged, checksum-verified local copy and continue to buttons.
                    import shutil

                    from autonomous_studio.storage.repository import digest

                    local = self.repo.project_dir(project.id) / (
                        f"review-video-v{project.version:03d}-{value.sha256[:8]}.mp4"
                    )
                    if local.exists():
                        if digest(local) != value.sha256:
                            raise ValueError("existing local review copy differs from approved MP4")
                    else:
                        shutil.copyfile(path, local)
                        if digest(local) != value.sha256:
                            raise ValueError("local review copy checksum mismatch")
                    result = await self.text(
                        "Telegram could not confirm the video upload. Review the unchanged "
                        f"validated copy at {local} before approving."
                    )
            self.repo.set_runtime(key, result.get("message_id", True))


def complete_script(project: VideoProject) -> str:
    if isinstance(project.spec, LongFormSpec):
        script = "\n\n".join(
            block.text
            for chapter in sorted(project.spec.chapters, key=lambda item: item.order)
            for block in chapter.narration_blocks
        )
        return script or "(No narration.)"
    segments = sorted(
        (
            (shot.start + segment.offset, segment.spoken_text)
            for shot in project.spec.shots
            for segment in shot.voice_segments
        ),
        key=lambda item: item[0],
    )
    return "\n\n".join(text for _, text in segments) or "(No narration.)"


def shot_choice_summary(project: VideoProject) -> str:
    """Human-facing rationale, separate from the speculative new-type audit."""
    return "\n".join(
        f"{shot.id}: {shot.kind} — "
        f"{shot.shot_type_reasoning or '(not recorded in this older plan)'}"
        for shot in project.spec.shots
    )


def review_message(project: VideoProject, approval: Approval) -> ReviewMessage:
    if approval.gate == "publication":
        package = project.publication
        if package is None:
            raise ValueError("publication review requires a prepared package")
        indexed = {a.id: a for a in project.artifacts}
        return ReviewMessage(
            project_id=project.id,
            approval_id=approval.id,
            version=approval.version,
            gate=approval.gate,
            text=(
                "Publication clearance\n\nYouTube title:\n"
                + package.title
                + "\n\nYouTube description:\n"
                + package.description
                + "\nUsage: "
                + package.usage_summary
                + (
                    "\nRights: " + (
                        package.rights_summary or "Review the rights manifest before publishing."
                    )
                    if isinstance(project.spec, LongFormSpec) else ""
                )
                + "\n\nApprove only after checking the title, description, thumbnail, "
                "facts, pronunciation and rights notes. Approval creates the root upload package."
                + (" It does not upload the video." if isinstance(project.spec, LongFormSpec) else "")
            ),
            media=[indexed[package.thumbnail_artifact_id]],
        )
    hooks = "\n".join(
        f"{index}. {hook}" for index, hook in enumerate(project.spec.candidate_hooks, 1)
    )
    if isinstance(project.spec, LongFormSpec) and approval.gate == "treatment":
        from autonomous_studio.domain.models import LongFormVisualPolicy

        ending = project.spec.engagement_ending or project.spec.brief.engagement_ending
        claim_texts = {
            claim.id: claim.text for claim in project.spec.brief.research.claims
        }
        chapters = "\n\n".join(
            (
                f"{chapter.order}. {chapter.title} ({chapter.target_duration:.0f}s)\n"
                f"Purpose: {chapter.purpose}\n"
                f"Question: {chapter.question}\n"
                f"Reveal: {chapter.reveal}\n"
                f"Transition: {chapter.transition or '(conclusion)'}"
                + (
                    "\nEssential evidence: "
                    + "; ".join(claim_texts[claim_id] for claim_id in chapter.essential_claim_ids)
                    if project.spec.story_contract_version == 2 else ""
                )
            )
            for chapter in project.spec.chapters
        )
        story_note = (
            "\n\nCentral tension:\n"
            + project.spec.story_design.central_tension
            + "\n\nPayoff:\n"
            + project.spec.story_design.payoff
            if project.spec.story_contract_version == 2 and project.spec.story_design
            else ""
        )
        story_choice = project.spec.story_choice
        angle_note = ""
        if story_choice:
            candidates = (
                project.longform_research_plan.story_questions
                if project.longform_research_plan else []
            )
            angle_note = (
                (
                    "\n\nCandidate story questions:\n"
                    + "\n".join(f"- {item.question}" for item in candidates)
                    if candidates else ""
                )
                + "\n\nChosen story question:\n"
                + project.spec.narrative.central_question
                + "\nWhy this angle:\n"
                + story_choice.evidence_rationale
                + (
                    "\nEvidence limit:\n" + story_choice.limitation
                    if story_choice.limitation else ""
                )
            )
        return ReviewMessage(
            project_id=project.id,
            approval_id=approval.id,
            version=approval.version,
            gate=approval.gate,
            text=(
                "Long-form treatment and chapter outline\n\nCandidate hooks:\n"
                + (hooks or "(No alternatives supplied.)")
                + "\n\nGenerated-image allowance: up to "
                + str(
                    (
                        project.longform_visual_policy or LongFormVisualPolicy()
                    ).maximum_generated_requests
                )
                + "; use only where helpful."
                + "\n\nChosen hook:\n"
                + project.spec.narrative.hook
                + "\n\nViewer promise:\n"
                + project.spec.narrative.viewer_promise
                + angle_note
                + story_note
                + "\n\nTreatment:\n"
                + project.spec.treatment_summary
                + "\n\nChapters:\n"
                + chapters
                + "\n\nConclusion:\n"
                + project.spec.narrative.conclusion
                + "\n\nAudience question:\n"
                + ending.comment_question
                + "\n\nApprove this structure before the complete narration is written."
            ),
            media=[],
        )
    if isinstance(project.spec, LongFormSpec) and approval.gate == "visual_plan":
        plan = project.longform_visual_plan
        if plan is None:
            raise ValueError("visual-plan review requires a prepared plan")
        counts = {
            kind: sum(asset.kind == kind for asset in plan.assets)
            for kind in ("authentic_image", "generated_image", "code_rendered")
        }
        beats = sum(len(chapter.visual_beats) for chapter in project.spec.chapters)
        unknown = "\n".join(f"- {item}" for item in plan.cost.unknown_cost_items) or "- None"
        rights = (
            f"{plan.rights.status}; "
            f"{len(plan.rights.context_review_asset_ids)} authentic candidate(s) require "
            "human context/personality-rights review before publication"
        )
        return ReviewMessage(
            project_id=project.id,
            approval_id=approval.id,
            version=approval.version,
            gate=approval.gate,
            text=(
                "Long-form mixed-media visual plan\n\n"
                f"Visual beats: {beats}\n"
                f"Authentic-media candidates: {counts['authentic_image']}\n"
                f"Generated-image requests proposed: {counts['generated_image']}\n"
                f"Code-rendered visuals: {counts['code_rendered']}\n"
                f"Known paid-asset cost: USD {plan.cost.known_cost_usd:.2f}\n"
                "Unknown-cost items:\n"
                + unknown
                + "\n\nRights status: "
                + rights
                + "\n\nNo paid image generation, production media acquisition, TTS or rendering "
                "has started. Review the contact sheets for narrative alignment and visual "
                "variety. Approval locks this plan for LF v0.4 acquisition and rendering."
            ),
            media=project.current_artifacts("longform_contact_sheet"),
        )
    if isinstance(project.spec, LongFormSpec) and approval.gate == "runtime":
        proposal = project.longform_runtime_proposal
        if proposal is None or proposal.version != project.version:
            raise ValueError("runtime review requires a current measured proposal")
        chapters = "\n".join(
            f"{chapter.order}. {chapter.title}: "
            f"{proposal.chapter_minimums[chapter.id]:.1f}s minimum"
            for chapter in project.spec.chapters
        )
        return ReviewMessage(
            project_id=project.id,
            approval_id=approval.id,
            version=approval.version,
            gate=approval.gate,
            text=(
                "Long-form narration timing choice\n\n"
                "Your approved script and synthesized narration are unchanged.\n"
                f"Measured speech: {proposal.measured_speech_seconds:.1f}s. "
                f"Minimum with transitions: {proposal.measured_minimum_seconds:.1f}s.\n"
                f"Original plan: {proposal.original_seconds:.1f}s; saved planning ceiling: "
                f"{proposal.prior_ceiling_seconds:.1f}s.\n"
                f"Proposed finished video: {proposal.proposed_seconds:.0f}s "
                f"({proposal.proposed_seconds / 60:.1f} minutes), with visual breathing room.\n\n"
                + chapters
                + "\n\nKeep script: extend the timeline and continue using the saved narration. "
                "Trim script: draft a shorter version and send it for a fresh script approval "
                "before any images are requested. No speech will be cut or sped up."
            ),
            media=[],
            actions=["approve", "trim", "reject"],
        )
    if isinstance(project.spec, LongFormSpec) and approval.gate == "prototype":
        previews = project.current_artifacts("prototype")
        if not previews:
            raise ValueError("long-form review requires a validated video")
        preview = previews[-1]
        # The artifact itself carries its project-relative location. The transport
        # enforces the 49 MB upload ceiling; larger files are reviewed locally.
        oversized = bool(project.current_artifacts("longform_review_root"))
        thumbnail = project.current_artifacts("longform_thumbnail")
        return ReviewMessage(
            project_id=project.id,
            approval_id=approval.id,
            version=approval.version,
            gate=approval.gate,
            text=(
                "Long-form review video is ready. Check factual accuracy, narration, "
                "visual alignment, captions and source rights before approving. "
                "For a narration rewrite, begin revision feedback with 'script:'; "
                "other revision feedback replans visuals.\n\n"
                + (
                    project.production_changes[-1] + "\n\n"
                    if project.production_changes
                    and project.production_changes[-1].startswith("Measured narration retimed")
                    else ""
                )
                + (
                    f"The video exceeds Telegram's review upload limit. Open "
                    f"review-video-v{project.version:03d}.mp4 in this project's root folder "
                    "for manual review."
                    if oversized
                    else "The validated MP4 follows this message."
                )
                + (
                    f" Thumbnail candidate: {thumbnail[-1].path.split('/')[-1]} "
                    "in the project root."
                    if thumbnail else ""
                )
            ),
            media=([thumbnail[-1]] if thumbnail else []) + ([] if oversized else [preview]),
        )
    if approval.gate == "script":
        if isinstance(project.spec, LongFormSpec):
            report = project.longform_continuity
            if report is None or not report.passed:
                raise ValueError("long-form script review requires passed continuity validation")
            target = report.target_timeline_seconds or project.spec.brief.duration
            proposed = report.proposed_timeline_seconds or target
            duration_note = (
                f"\nOriginal target: {target:.1f}s. Proposed finished timeline: "
                f"{proposed:.1f}s. Approval accepts this expanded runtime."
                if report.duration_status == "expansion_proposed"
                else ""
            )
            chapters = "\n\n".join(
                f"CHAPTER {chapter.order}: {chapter.title}\n"
                + "\n\n".join(block.text for block in chapter.narration_blocks)
                for chapter in project.spec.chapters
            )
            editorial_note = ""
            if project.spec.story_contract_version == 2 and report.editorial_review:
                review = report.editorial_review
                editorial_note = (
                    "\n\nEditorial audit: "
                    f"facts {review.factual_integrity.status}; "
                    f"promise/payoff {review.promise_payoff.status}; "
                    f"newcomer clarity {review.newcomer_clarity.status}; "
                    f"narrative momentum {review.narrative_momentum.status}."
                )
            script_warnings = [
                issue for issue in report.issues
                if issue.severity == "warning" and issue.code != "duration_expansion_proposed"
            ]
            if script_warnings:
                editorial_note += "\n\nFor your review (not blocking approval):\n" + "\n".join(
                    f"- {', '.join(issue.chapter_ids) or 'Whole script'}: {issue.message}"
                    for issue in script_warnings[:6]
                )
            return ReviewMessage(
                project_id=project.id,
                approval_id=approval.id,
                version=approval.version,
                gate=approval.gate,
                text=(
                    "Continuity-validated long-form script\n\nCandidate hooks:\n"
                    + (hooks or "(No alternatives supplied.)")
                    + "\n\nChosen hook:\n"
                    + project.spec.narrative.hook
                    + "\n\nComplete script:\n"
                    + chapters
                    + editorial_note
                    + duration_note + "\n"
                    "Approve to lock the narrative before visual planning begins."
                ),
                media=[],
            )
        measured = sum(voice.asset.duration for voice in project.prepared_voices)
        kinds = {shot.id: shot.kind for shot in project.spec.shots}
        shot_choices = shot_choice_summary(project)
        reviews = project.spec.shot_type_reviews
        opportunities = [item for item in reviews if item.suggested_kind != "keep_current"]
        visual_audit = (
            "\n\nVisual-type audit (advisory only; new types are not rendered):\n"
            + f"{len(reviews)}/{len(kinds)} shots assessed; "
            + f"{len(opportunities)} potential new-type opportunities."
        )
        if opportunities:
            visual_audit += "\n" + "\n".join(
                f"{item.shot_id}: {kinds[item.shot_id]} → {item.suggested_kind} — {item.reason}"
                for item in opportunities
                if item.shot_id in kinds
            )
        elif reviews:
            visual_audit += " Current types were preferred for assessed shots."
        else:
            visual_audit += " No assessment was returned; current shots are unchanged."
        return ReviewMessage(
            project_id=project.id,
            approval_id=approval.id,
            version=approval.version,
            gate=approval.gate,
            text=(
                "Timing-validated script\n\nCandidate hooks:\n"
                + (hooks or "(No alternatives supplied.)")
                + "\n\nChosen hook:\n"
                + project.spec.hook
                + "\n\nComplete script:\n"
                + complete_script(project)
                + f"\n\nMeasured speech: {measured:.1f}s across a "
                f"{project.spec.brief.duration:.1f}s timeline."
                + "\n\nChosen shot types (why):\n"
                + shot_choices
                + visual_audit
                + "\n\n"
                "Approve to lock this script before image preparation."
            ),
            media=[],
        )
    if approval.gate == "narration":
        measured = sum(voice.asset.duration for voice in project.prepared_voices)
        limit = 15 if project.spec.brief.production_mode == "prototype" else 90
        proposed = project.short_runtime_proposal_seconds
        if proposed is not None:
            return ReviewMessage(
                project_id=project.id,
                approval_id=approval.id,
                version=approval.version,
                gate=approval.gate,
                text=(
                    "Runtime choice — script can remain intact\n\n"
                    f"Measured speech: {measured:.1f}s. Natural scene timing proposes "
                    f"{proposed:.1f}s, above the {limit}s automatic ceiling.\n\n"
                    "Complete script:\n" + complete_script(project) + "\n\n"
                    "Keep script to approve the longer Short, or request edits. "
                    "No images have been prepared."
                ),
                media=[],
                actions=["approve", "revise", "reject"],
            )
        return ReviewMessage(
            project_id=project.id,
            approval_id=approval.id,
            version=approval.version,
            gate=approval.gate,
            text=(
                "Script needs timing revision — not yet approvable\n\n"
                "The studio used both internal repair attempts, but the measured narration "
                "and required scene timing still could not fit the production limit. No images "
                "were prepared.\n\nCandidate hooks:\n"
                + (hooks or "(No alternatives supplied.)")
                + "\n\nChosen hook:\n"
                + project.spec.hook
                + "\n\nComplete script:\n"
                + complete_script(project)
                + f"\n\nMeasured speech: {measured:.1f}s. Timeline limit: {limit}s.\n"
                "Choose Revise and reply with what may be shortened or rewritten. A new "
                "timing-valid script will then return with an Approve button."
            ),
            media=[],
            actions=["revise", "reject"],
        )
    return ReviewMessage(
        project_id=project.id,
        approval_id=approval.id,
        version=approval.version,
        gate=approval.gate,
        text=(
            "Candidate hooks:\n"
            + (hooks or "(No alternatives supplied.)")
            + "\n\nChosen hook:\n"
            + project.spec.hook
            + "\n\nComplete script:\n"
            + complete_script(project)
            + (
                "\n\nChosen shot types (why):\n" + shot_choice_summary(project)
                if approval.gate == "story" and not isinstance(project.spec, LongFormSpec)
                else ""
            )
        ),
        media=[a for a in project.artifacts if a.id in approval.artifact_ids],
        actions=["approve", "revise", "reject"],
    )


def verify_approval(repo: Repository, project: VideoProject, approval: Approval):
    indexed = {a.id: a for a in project.artifacts}
    for artifact_id in approval.artifact_ids:
        if artifact_id not in indexed:
            raise ValueError("approval references missing artifact")
        repo.verify(project, indexed[artifact_id])
