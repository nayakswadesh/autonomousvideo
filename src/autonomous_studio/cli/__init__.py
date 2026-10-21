import asyncio
import json
from functools import wraps
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief

app = typer.Typer(no_args_is_help=True, help="Autonomous Studio v1.0 — audiovisual pipeline")


def guarded(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ValueError, RuntimeError, OSError, ValidationError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc

    return wrapped


@app.callback()
def main(
    ctx: typer.Context, root: Annotated[Path, typer.Option(envvar="STUDIO_ROOT")] = Path("projects")
):
    ctx.obj = root


def studio(ctx):
    return Studio(ctx.obj)


def display(project):
    pending = project.pending()
    brief = project.spec.brief
    production_mode = getattr(brief, "production_mode", None)
    profile = getattr(brief, "profile", None)
    typer.echo(
        json.dumps(
            {
                "id": project.id,
                "state": project.state,
                "version": project.version,
                "revision": project.revision,
                "review_policy": project.review_policy,
                "format": project.spec.format,
                "profile": profile,
                "production_mode": production_mode,
                "duration": brief.duration,
                "closed_captions": project.spec.closed_captions,
                "timing_rewrite_attempts": project.timing_rewrite_attempts,
                "short_runtime_proposal_seconds": project.short_runtime_proposal_seconds,
                "short_runtime_approved_seconds": project.short_runtime_approved_seconds,
                "pending": pending.model_dump(mode="json") if pending else None,
                "artifacts": [
                    {"role": a.role, "path": a.path} for a in project.current_artifacts()
                ],
                "warnings": project.warnings,
                "last_error": project.last_error,
                "automatic_assets": project.auto_assets is not None,
                "image_requests": project.image_requests,
                "images_prepared": len(project.generated_visuals),
                "chapters": len(getattr(project.spec, "chapters", [])),
                "continuity_passed": (
                    project.longform_continuity.passed
                    if project.longform_continuity is not None
                    else None
                ),
                "visual_beats": sum(
                    len(chapter.visual_beats) for chapter in getattr(project.spec, "chapters", [])
                ),
                "visual_plan_assets": (
                    len(project.longform_visual_plan.assets)
                    if project.longform_visual_plan is not None
                    else 0
                ),
                "visual_plan_rights": (
                    project.longform_visual_plan.rights.status
                    if project.longform_visual_plan is not None
                    else None
                ),
                "longform_image_requests": project.longform_image_requests,
                "longform_media_prepared": len(project.longform_media),
                "longform_narration_blocks_prepared": len(project.longform_voices),
                "longform_chapters_rendered": len(project.longform_chapters),
                "longform_review_in_project_root": bool(
                    project.current_artifacts("longform_review_root")
                ),
                "longform_master_in_project_root": bool(
                    project.current_artifacts("longform_master")
                ),
            },
            indent=2,
        )
    )


@app.command()
@guarded
def create(ctx: typer.Context, brief: Path):
    display(studio(ctx).create(load_brief(brief)))


@app.command()
@guarded
def run(ctx: typer.Context, project_id: str):
    display(asyncio.run(studio(ctx).run(project_id)))


@app.command("refresh-longform-archives")
@guarded
def refresh_longform_archives(ctx: typer.Context, project_id: str, sources: Path):
    """Recheck selected Commons files and replace an unproduced visual plan's fallbacks."""
    from autonomous_studio.workflow.longform_visuals import refresh_curated_visual_plan

    studio_instance = studio(ctx)
    data = json.loads(sources.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("sources"), dict):
        raise ValueError("curated sources need schema_version 1 and an asset-to-URL map")
    with studio_instance.repo.lock(project_id):
        project = studio_instance.repo.load(project_id)
        studio_instance.configure_live(project)
        project = asyncio.run(
            refresh_curated_visual_plan(studio_instance, project, data["sources"])
        )
    display(project)


@app.command()
@guarded
def approve(
    ctx: typer.Context,
    project_id: str,
    gate: str,
    request: str | None = None,
    version: int | None = None,
    actor: str = "local-user",
    message: str = "",
):
    display(
        studio(ctx).decide(
            project_id,
            gate,
            "approve",
            approval_id=request,
            version=version,
            actor=actor,
            message=message,
        )
    )


@app.command("adopt-longform-review-variant")
@guarded
def adopt_longform_review_variant(ctx: typer.Context, project_id: str, path: str):
    """Bind a project-local revised MP4 to its still-pending prototype review."""
    display(studio(ctx).adopt_longform_review_variant(project_id, path))


@app.command("register-longform-publication")
@guarded
def register_longform_publication(ctx: typer.Context, project_id: str):
    """Record manually reviewed YouTube metadata and thumbnail in a finished project."""
    display(studio(ctx).register_longform_publication_package(project_id))


@app.command("queue-longform-publication")
@guarded
def queue_longform_publication(ctx: typer.Context, project_id: str):
    """Queue publication review for an already approved long-form master."""
    display(studio(ctx).queue_longform_publication(project_id))


@app.command()
@guarded
def reject(
    ctx: typer.Context,
    project_id: str,
    gate: str,
    message: str = "",
    request: str | None = None,
    version: int | None = None,
    actor: str = "local-user",
):
    display(
        studio(ctx).decide(
            project_id,
            gate,
            "reject",
            message=message,
            approval_id=request,
            version=version,
            actor=actor,
        )
    )


@app.command()
@guarded
def revise(
    ctx: typer.Context,
    project_id: str,
    message: str,
    brief: Path | None = None,
    actor: str = "local-user",
):
    display(
        studio(ctx).revise(
            project_id, message, brief=load_brief(brief) if brief else None, actor=actor
        )
    )


@app.command()
@guarded
def status(ctx: typer.Context, project_id: str):
    display(studio(ctx).repo.load(project_id))


@app.command()
@guarded
def events(ctx: typer.Context, project_id: str):
    typer.echo(json.dumps(studio(ctx).repo.events(project_id), indent=2))


@app.command()
@guarded
def render(ctx: typer.Context, project_id: str):
    display(asyncio.run(studio(ctx).render(project_id)))


@app.command()
@guarded
def rebuild_longform(
    ctx: typer.Context,
    project_id: str,
    semantic_decisions: Annotated[
        Path | None, typer.Option(help="Reuse reviewed chapter decisions from a pilot run")
    ] = None,
    reuse_approved_semantic: Annotated[
        bool, typer.Option(help="Versioned presentation rebuild; reuse approved semantic decisions without model calls")
    ] = False,
):
    """Rebuild a pending long-form preview with the production visual director."""
    display(
        asyncio.run(
            studio(ctx).rebuild_longform(
                project_id,
                semantic_decisions=semantic_decisions,
                reuse_approved_semantic=reuse_approved_semantic,
            )
        )
    )


@app.command()
@guarded
def longform_image_budget(ctx: typer.Context, project_id: str, budget: int):
    """Change one existing long-form project's generated-image ceiling (4–30)."""
    display(studio(ctx).set_longform_image_budget(project_id, budget))


@app.command("longform-repair-limit")
@guarded
def longform_repair_limit(
    ctx: typer.Context, project_id: str, stage: str, limit: int, version: int
):
    """Set one persisted creative repair limit (0–3) for a new-policy project."""
    display(
        studio(ctx).set_longform_creative_repair_limit(
            project_id, stage, limit, version=version
        )
    )


@app.command("adopt-longform-stage-models")
@guarded
def adopt_longform_stage_models(ctx: typer.Context, project_id: str):
    """Apply current text-stage model settings to an unfinished long-form project."""
    display(studio(ctx).adopt_longform_stage_models(project_id))


@app.command()
@guarded
def preview_review(ctx: typer.Context, project_id: str, version: int):
    """Use one validated-preview approval, preserving historical decisions."""
    display(studio(ctx).enable_preview_review(project_id, version=version))


@app.command()
@guarded
def refresh_preview(
    ctx: typer.Context,
    project_id: str,
    version: int,
    reason: str,
    shot_overrides: Path | None = None,
):
    """Rerender a pending preview as a new version without new provider calls."""
    display(
        studio(ctx).refresh_preview(
            project_id,
            version=version,
            reason=reason,
            actor="local-user",
            shot_overrides=json.loads(shot_overrides.read_text(encoding="utf-8"))
            if shot_overrides
            else None,
        )
    )


@app.command()
@guarded
def reframe_semantic(ctx: typer.Context, project_id: str, version: int):
    """Create a render-only version with nonnumeric comparisons and single-study diagrams."""
    display(
        studio(ctx).refresh_preview(
            project_id,
            version=version,
            reason="Replace false quantitative or dated visuals with semantic diagrams",
            semantic_visuals=True,
        )
    )


@app.command()
@guarded
def repair(ctx: typer.Context, project_id: str, version: int):
    """Queue a version-bound timing repair; run or the worker prepares a new approval."""
    display(studio(ctx).request_retry(project_id, repair=True, version=version))


@app.command()
@guarded
def retry(ctx: typer.Context, project_id: str, version: int | None = None):
    """Queue a failed stage after checking its saved version; no provider work occurs here."""
    display(studio(ctx).request_retry(project_id, version=version))


@app.command()
@guarded
def assets(ctx: typer.Context, project_id: str, version: int):
    """Queue automatic artwork/music for an approved completed baseline."""
    display(studio(ctx).automate_assets(project_id, version=version))


@app.command()
@guarded
def recover_assets(ctx: typer.Context, project_id: str, response: Path, version: int):
    """Revalidate a saved provider response after a validator fix; never call the API."""
    from autonomous_studio.workflow.assets import recover_asset_plan

    display(recover_asset_plan(studio(ctx), project_id, response, version=version))


@app.command()
@guarded
def recover_publication(ctx: typer.Context, project_id: str, version: int):
    """Rebuild missing publication metadata from saved inputs; no provider calls."""
    from autonomous_studio.workflow.publication import recover_publication_package

    display(recover_publication_package(studio(ctx), project_id, version=version))


@app.command()
@guarded
def recover_research(
    ctx: typer.Context,
    project_id: str,
    evidence_response: Path,
    structured_response: Path,
    version: int,
):
    """Revalidate cached research after a validator fix; never call the API."""
    from autonomous_studio.workflow.research import recover_research as recover

    display(
        recover(
            studio(ctx),
            project_id,
            evidence_response,
            structured_response,
            version=version,
        )
    )


@app.command()
@guarded
def recover_timing_layout(ctx: typer.Context, project_id: str, version: int):
    """Re-evaluate a pending narration review after a local timing-layout fix."""
    display(studio(ctx).revalidate_narration_layout(project_id, version=version))


@app.command()
@guarded
def recover_director(ctx: typer.Context, project_id: str, response: Path, version: int):
    """Revalidate already-paid narrative/shot responses; never call the API."""
    from autonomous_studio.workflow.director_recovery import recover_director as recover

    display(asyncio.run(recover(studio(ctx), project_id, response, version=version)))


@app.command()
@guarded
def music_import_batch(
    manifest: Path,
    catalog: Annotated[Path | None, typer.Option()] = None,
):
    """Import downloaded YouTube Audio Library tracks and durable license metadata."""
    from autonomous_studio.config import Settings
    from autonomous_studio.providers.youtube_audio_library import import_batch

    target = catalog or Path(Settings.environment().music_catalog)
    imported = import_batch(manifest, target)
    typer.echo(
        json.dumps(
            {
                "catalog": str(target.resolve()),
                "imported": [
                    {
                        "id": track.id,
                        "title": track.title,
                        "artist": track.artist,
                        "duration_seconds": track.duration_seconds,
                        "tags": track.tags,
                        "license_type": track.license_type,
                    }
                    for track in imported
                ],
            },
            indent=2,
        )
    )


@app.command()
@guarded
def polish(ctx: typer.Context, project_id: str, manifest: Path, version: int):
    """Queue a presentation-only revision of an approved video; keep its measured narration."""
    display(studio(ctx).polish(project_id, manifest, version=version))


@app.command()
@guarded
def doctor():
    ffmpeg = FFmpeg()
    typer.echo(ffmpeg.run(["-version"]).stdout.splitlines()[0])
    typer.echo("Mock pipeline ready. Live mode requires OpenAI and Telegram configuration.")


@app.command("youtube-auth")
@guarded
def youtube_auth(expected_channel: str):
    """Authorize this laptop to upload private videos to the selected YouTube channel."""
    from autonomous_studio.providers.youtube import authorize_youtube

    channel = authorize_youtube(expected_channel)
    typer.echo(
        f"YouTube authorization saved for {channel['channel_title']} "
        f"({channel['channel_id']}). Private uploads are ready."
    )


@app.command("upload-youtube-short")
@guarded
def upload_youtube_short(ctx: typer.Context, project_id: str):
    """Upload one completed, cleared project to YouTube in private mode."""
    from autonomous_studio.providers.youtube import YouTubePublisher

    result = YouTubePublisher(studio(ctx).repo).publish(project_id)
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))


@app.command()
@guarded
def topic(ctx: typer.Context, text: str, duration: float = 12):
    """Create a live project. Run it locally or let the Telegram worker pick it up."""
    display(studio(ctx).create_topic(text, duration=duration))


@app.command()
@guarded
def full(
    ctx: typer.Context,
    text: str,
    duration: float = 55,
    rank_count: int | None = None,
    image_budget: int | None = None,
):
    """Create a Short targeting 45–60s; measured speech may extend it to 90s automatically."""
    display(
        studio(ctx).create_topic(
            text,
            duration=duration,
            production_mode="full",
            expected_rank_count=rank_count,
            image_budget=image_budget,
        )
    )


@app.command()
@guarded
def longform(ctx: typer.Context, text: str, duration: float = 210):
    """Create a 150–240 second long-form pilot; research begins on run/worker resume."""
    display(studio(ctx).create_longform_topic(text, target_duration=duration))


@app.command()
@guarded
def worker(ctx: typer.Context, once: bool = False):
    """Poll authorized Telegram feedback and resume live projects until their next gate."""
    from autonomous_studio.config import Settings, telegram_environment
    from autonomous_studio.hitl.adapters import TelegramAdapter
    from autonomous_studio.hitl.worker import TelegramWorker

    Settings.environment().require_key()
    engine = studio(ctx)
    adapter = TelegramAdapter(telegram_environment(), engine.repo)
    typer.echo("Telegram worker started. Ctrl+C stops polling; project checkpoints remain saved.")
    asyncio.run(TelegramWorker(engine, adapter, lambda: studio(ctx)).run(once=once))
