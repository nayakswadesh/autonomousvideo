"""Presentation-only revisions preserve approved content and measured speech."""

import json
import shutil
from pathlib import Path
from uuid import uuid4

import yaml
from PIL import Image

from autonomous_studio.domain.media import PresentationManifest
from autonomous_studio.domain.models import (
    Presentation,
    State,
    now,
    validate_story_scope,
    validate_timeline,
)
from autonomous_studio.storage.repository import digest, write_json


def local_input(base: Path, relative: str) -> Path:
    if Path(relative).is_absolute():
        raise ValueError("manifest inputs must be relative to the manifest directory")
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()) or not path.is_file():
        raise ValueError("missing media input or path escapes manifest directory")
    return path


def refresh_preview(
    studio,
    project_id,
    *,
    version,
    reason,
    actor="local-user",
    closed_captions=None,
    semantic_visuals=False,
    shot_overrides=None,
):
    """Create a new render-only version from verified preview inputs."""
    if not reason.strip():
        raise ValueError("preview refresh reason cannot be empty")
    with studio.repo.lock(project_id):
        project = studio.repo.load(project_id)
        if project.version != version:
            raise ValueError("stale preview version; inspect status before refreshing")
        source_state = project.state
        pending = project.pending()
        valid_pending = (
            source_state == State.PROTOTYPE_APPROVAL and pending and pending.gate == "prototype"
        ) or (
            source_state == State.PUBLICATION_CLEARANCE
            and pending
            and pending.gate == "publication"
        )
        valid_completed = source_state == State.COMPLETED and pending is None
        if not valid_pending and not valid_completed:
            raise ValueError(
                "preview refresh requires a pending prototype/publication approval "
                "or an approved completed baseline"
            )
        if semantic_visuals:
            from autonomous_studio.providers.openai import ShotPlanDraft, normalize_shot_plan

            if project.spec.presentation is None:
                raise ValueError("semantic reframe requires an editorial preview")
            inspected = normalize_shot_plan(
                ShotPlanDraft(shots=[shot.model_copy(deep=True) for shot in project.spec.shots])
            ).shots
            if not any(
                new.kind != old.kind for old, new in zip(project.spec.shots, inspected, strict=True)
            ):
                raise ValueError("preview contains no semantic chart or timeline to reframe")
        current = project.current_artifacts()
        for artifact in current:
            studio.repo.verify(project, artifact)
        replacements = {}
        if shot_overrides:
            from types import SimpleNamespace

            from autonomous_studio.rendering.object_motion import cue_onsets, object_count

            by_id = {shot.id: shot for shot in project.spec.shots}
            if set(shot_overrides) - set(by_id):
                raise ValueError("shot overrides contain unknown shot IDs")
            for shot_id, changes in shot_overrides.items():
                if set(changes) - {"entrance_cues", "geography"}:
                    raise ValueError("only entrance_cues and geography may be refreshed")
                old = by_id[shot_id]
                if "geography" in changes and old.kind != "map":
                    raise ValueError("geography override requires a map shot")
                new = type(old).model_validate({**old.model_dump(mode="python"), **changes})
                if new.entrance_cues:
                    if len(new.entrance_cues) != object_count(new):
                        raise ValueError("entrance cue count must match visible objects")
                    cue_onsets(SimpleNamespace(prepared_voices=[]), new)
                replacements[shot_id] = new

        old_version = project.version
        if pending:
            pending.decision, pending.actor, pending.decided_at = "superseded", actor, now()
            pending.message = f"Superseded by render-only refresh: {reason}"
        project.version += 1
        output = studio.repo.attempt_dir(project, "preview-refresh")
        excluded = {
            "audio",
            "prototype",
            "qa",
            "silent_video",
            "preview_frame",
            "thumbnail_candidate",
            "thumbnail_gallery",
            "publication_description",
            "release_status",
            "usage_report",
            "youtube_metadata",
            "youtube_title",
            "source_manifest",
            "rights_manifest",
            "youtube_upload_receipt",
        }
        if replacements:
            excluded.add("story")
        mapping = {}
        for artifact in current:
            if artifact.role in excluded or artifact.role.startswith(("stem_", "review_")):
                continue
            source = studio.repo.verify(project, artifact)
            destination = output / f"{artifact.id}-{source.name}"
            shutil.copyfile(source, destination)
            copied = artifact.model_copy(
                deep=True,
                update={
                    "id": uuid4().hex,
                    "version": project.version,
                    "created_at": now(),
                    "path": destination.relative_to(studio.repo.project_dir(project.id)).as_posix(),
                },
            )
            if artifact.role in {
                "narration_validation",
                "scope_validation",
                "asset_validation",
            }:
                report = json.loads(destination.read_text(encoding="utf-8"))
                report.update(
                    {
                        "version": project.version,
                        "preview_refresh": True,
                        "source_version": old_version,
                    }
                )
                write_json(destination, report)
                copied.sha256 = digest(destination)
            mapping[artifact.id] = copied
            project.artifacts.append(copied)

        def mapped(artifact):
            if artifact.id not in mapping:
                raise ValueError(f"preview input was not carried forward: {artifact.role}")
            return mapping[artifact.id]

        for voice in project.prepared_voices:
            voice.asset.artifact = mapped(voice.asset.artifact)
        if replacements:
            project.spec.shots = [replacements.get(shot.id, shot) for shot in project.spec.shots]
            studio._json(
                project, output, "render-story.json", project.spec.model_dump(mode="json"), "story"
            )
        for audio in project.spec.audio_assets:
            audio.artifact = mapped(audio.artifact)
        for visual in project.generated_visuals:
            visual.artifact = mapped(visual.artifact)
        if project.spec.presentation:
            for look in project.spec.presentation.shots:
                if look.image_ref:
                    look.image_ref = mapped(next(a for a in current if a.id == look.image_ref)).id
        music = project.spec.brief.music
        if music.artifact_ref:
            music.artifact_ref = mapped(next(a for a in current if a.id == music.artifact_ref)).id

        if closed_captions is not None:
            project.spec.closed_captions = closed_captions
        if semantic_visuals:
            from autonomous_studio.providers.openai import ShotPlanDraft, normalize_shot_plan

            old_kinds = {shot.id: shot.kind for shot in project.spec.shots}
            project.spec.shots = normalize_shot_plan(ShotPlanDraft(shots=project.spec.shots)).shots
            converted = {shot.id for shot in project.spec.shots if shot.kind != old_kinds[shot.id]}
            if not converted:
                raise ValueError("preview contains no semantic chart or timeline to reframe")
            for look in project.spec.presentation.shots:
                if look.shot_id in converted:
                    look.visual_strategy = (
                        "image_with_diagram_overlay" if look.image_ref else "diagram"
                    )
                    if (
                        next(shot for shot in project.spec.shots if shot.id == look.shot_id).kind
                        == "study_design"
                    ):
                        # The process steps already carry the information; the old
                        # timeline annotation usually restates one of those steps.
                        look.annotation = ""
            validate_timeline(
                project.spec.shots, project.spec.brief.duration, project.spec.brief.research
            )
            studio._json(
                project,
                output,
                "semantic-story.json",
                project.spec.model_dump(mode="json"),
                "story",
            )
        project.publication = None
        project.state = State.AUDIO_PREPARED
        project.last_error = project.last_error_code = None
        project.production_changes.append(
            f"Render-only preview refresh from v{old_version}: {reason}. "
            "Research, narration, music and images reused unchanged."
        )
        studio.repo.save(
            project,
            "preview_refreshed",
            {
                "from": source_state,
                "to": State.AUDIO_PREPARED,
                "version": project.version,
                "source_version": old_version,
                "superseded_approval": pending.id if pending else None,
                "reason": reason,
                "actor": actor,
                "closed_captions": project.spec.closed_captions,
                "provider_calls": 0,
                "semantic_visuals": semantic_visuals,
                "shot_overrides": sorted(replacements),
            },
        )
        return project


def polish(
    studio,
    project_id: str,
    path: Path | None,
    *,
    version: int,
    actor="local-user",
    auto_assets=None,
):
    if path and path.stat().st_size > 2_000_000:
        raise ValueError("presentation manifest exceeds 2 MB")
    manifest = (
        PresentationManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if path
        else PresentationManifest(presentation=Presentation())
    )
    base = path.resolve().parent if path else Path.cwd()
    # Fail before advancing the project if any source input is missing or modified.
    for media in manifest.assets:
        source = local_input(base, media.file)
        if digest(source) != media.sha256:
            raise ValueError(f"media checksum mismatch: {media.id}")
        if media.kind == "image":
            with Image.open(source) as im:
                im.verify()
        else:
            local_input(base, media.license_evidence_file)
        if media.prompt_file:
            local_input(base, media.prompt_file)

    with studio.repo.lock(project_id):
        project = studio.repo.load(project_id)
        if project.version != version:
            raise ValueError("stale presentation version; inspect status before retrying")
        if project.state != State.COMPLETED:
            raise ValueError("presentation revision requires an approved completed baseline")
        if not project.current_artifacts("prototype"):
            raise ValueError("completed baseline has no video")
        shot_ids = {s.id for s in project.spec.shots}
        source_ids = {s.id for s in project.spec.brief.research.sources}
        if not {s.shot_id for s in manifest.presentation.shots} <= shot_ids:
            raise ValueError("presentation references an unknown shot")
        if not set(manifest.presentation.source_labels) <= source_ids:
            raise ValueError("presentation references an unknown research source")
        validate_timeline(
            project.spec.shots, project.spec.brief.duration, project.spec.brief.research
        )
        previous = project.current_artifacts()
        for artifact in previous:
            studio.repo.verify(project, artifact)
        old_movie = project.current_artifacts("prototype")[-1]
        project.version += 1
        output = studio.repo.attempt_dir(project, "presentation")
        mapping = {}
        # New version owns copies; old versions and human decisions are never modified.
        for artifact in previous:
            if artifact.role not in {
                "research",
                "findings",
                "voice",
                "provider_usage",
                "music_source",
                "license_evidence",
            }:
                continue
            src = studio.repo.verify(project, artifact)
            dst = output / f"{artifact.id}-{src.name}"
            shutil.copyfile(src, dst)
            copied = artifact.model_copy(
                deep=True,
                update={
                    "id": uuid4().hex,
                    "version": project.version,
                    "created_at": now(),
                    "path": dst.relative_to(studio.repo.project_dir(project.id)).as_posix(),
                },
            )
            mapping[artifact.id] = copied
            project.artifacts.append(copied)
        music = project.spec.brief.music
        if music.artifact_ref:
            if music.artifact_ref not in mapping:
                raise ValueError("baseline library music is missing from its version")
            carried_music = mapping[music.artifact_ref]
            music.artifact_ref = carried_music.id
            manifest.presentation.music_credit = carried_music.provenance.attribution
        for voice in project.prepared_voices:
            original = voice.asset.artifact
            if original.id not in mapping:
                raise ValueError("prepared narration is not referenced by the baseline version")
            voice.asset.artifact = mapping[original.id]
        imported = {}
        for media in manifest.assets:
            src = local_input(base, media.file)
            dst = output / f"{media.id}{src.suffix.lower()}"
            shutil.copyfile(src, dst)
            if digest(dst) != media.sha256:
                raise ValueError(f"media changed during import: {media.id}")
            artifact = studio.repo.artifact(
                project,
                dst,
                "visual_asset" if media.kind == "image" else "music_source",
                provider=media.provenance.provider,
                model=media.provenance.model or "unspecified",
            )
            artifact.provenance = media.provenance.model_copy(deep=True)
            artifact.cost = None
            artifact.input_reference = media.id
            if media.kind == "image":
                with Image.open(dst) as im:
                    artifact.width, artifact.height = im.size
            else:
                project.spec.brief.music.source_type = "library"
                project.spec.brief.music.artifact_ref = artifact.id
                project.spec.brief.music.trim_start = media.trim_start
                manifest.presentation.music_credit = media.provenance.attribution
            project.artifacts.append(artifact)
            imported[media.id] = artifact.id
            extras = [(media.prompt_file, "image_prompt")]
            if media.kind == "music":
                extras.append((media.license_evidence_file, "license_evidence"))
            for relative, role in extras:
                if relative:
                    src = local_input(base, relative)
                    dst = output / f"{media.id}-{role}{src.suffix}"
                    shutil.copyfile(src, dst)
                    project.artifacts.append(studio.repo.artifact(project, dst, role))
        presentation = manifest.presentation.model_copy(deep=True)
        for look in presentation.shots:
            if look.image_ref:
                look.image_ref = imported[look.image_ref]
        project.spec.presentation = presentation if auto_assets is None else None
        project.auto_assets = auto_assets
        project.asset_plan = None
        project.asset_plan_attempts = project.image_requests = 0
        project.generated_visuals = []
        if auto_assets:
            music.source_type, music.artifact_ref = "procedural", None
        project.spec.audio_assets = []
        project.spec.brief.visual_style.background = "#171a1c"
        project.spec.brief.visual_style.foreground = "#f2eadc"
        project.spec.brief.visual_style.accent = "#e6a663"
        project.review_policy = "preview"
        project.last_error = project.last_error_code = None
        project.state = State.NARRATION_PREPARING
        project.production_changes = [
            f"v0.3 presentation revision of approved v{version}; "
            "script and shot timings unchanged.",
            "Editorial typography, generated illustration and source-linked graphics.",
            "Measured narration reused; library music carries attribution and license evidence."
            if music.source_type == "library"
            else "Procedural music retained.",
        ]
        if auto_assets:
            project.production_changes = [
                f"v0.5 automatic asset revision of approved v{version}; "
                "research, spoken script and measured speech retained."
            ]
        project.warnings = studio.warnings_for(project)
        validate_story_scope(project.spec)
        studio._json(
            project,
            output,
            "presentation-input.json",
            manifest.model_dump(mode="json"),
            "presentation_manifest",
        )
        studio._json(
            project,
            output,
            "baseline.json",
            {
                "version": version,
                "prototype": old_movie.model_dump(mode="json"),
                "narration_reused": len(project.prepared_voices),
            },
            "baseline",
        )
        studio._json(project, output, "story.json", project.spec.model_dump(mode="json"), "story")
        studio._json(
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
                "presentation_revision": True,
            },
            "scope_validation",
        )
        studio._json(
            project, output, "brief.json", project.spec.brief.model_dump(mode="json"), "brief"
        )
        studio.repo.save(
            project,
            "presentation_revised",
            {
                "from": State.COMPLETED,
                "to": project.state,
                "version": project.version,
                "baseline_version": version,
                "actor": actor,
            },
        )
        return project
