"""Durable asset preparation after measured speech, before final mixing and approval."""

import json
import re
import shutil
from pathlib import Path

from autonomous_studio.audio.mixer import pcm_metrics
from autonomous_studio.domain.models import (
    AssetPlan,
    GeneratedVisual,
    Presentation,
    QualitativeComparisonShot,
    State,
)
from autonomous_studio.providers.archival import (
    ArchivalMediaError,
    WikimediaCommonsMedia,
    archival_fingerprint,
)
from autonomous_studio.providers.asset_planner import (
    OpenAIAssetPlanner,
    normalize_asset_plan,
    validate_asset_plan,
)
from autonomous_studio.providers.images import AssetPreparationError, OpenAIImages, fingerprint
from autonomous_studio.providers.library import LibraryMusic
from autonomous_studio.providers.music_catalog import (
    CatalogTrack,
    recent_music_track_ids,
    select_music,
)
from autonomous_studio.providers.openai import output_text
from autonomous_studio.storage.repository import digest


def recover_asset_plan(
    studio,
    project_id,
    response_path,
    *,
    version,
    actor="local-user",
    visual_repair: QualitativeComparisonShot | None = None,
):
    """Revalidate cached planning, optionally repairing one conceptual metric shot.

    The visual-only repair keeps every approved word, timing, voice asset and script lock.
    It is committed only if the cached asset plan validates against the repaired shot.
    """
    repo = studio.repo
    with repo.lock(project_id):
        project = repo.load(project_id)
        if project.version != version:
            raise ValueError("stale project version")
        if project.state != State.ASSETS_PREPARING or not project.auto_assets:
            raise ValueError("cached plan recovery requires ASSETS_PREPARING")
        if project.asset_plan is not None or project.image_requests or project.generated_visuals:
            raise ValueError("cannot replace an accepted plan or dispatched images")
        studio._verify_script_approval(project)
        path = Path(response_path).resolve()
        cache = (repo.project_dir(project_id) / "provider-cache").resolve()
        if not path.is_relative_to(cache) or path.suffix != ".json":
            raise ValueError("response must be a JSON file in this project's provider cache")
        if path.stat().st_size > 2_000_000:
            raise ValueError("cached response exceeds 2 MB")
        raw = json.loads(path.read_text(encoding="utf-8"))
        repair_record = None
        if visual_repair is not None:
            if not isinstance(visual_repair, QualitativeComparisonShot):
                raise ValueError("visual repair must be a qualitative comparison shot")
            index = next(
                (i for i, shot in enumerate(project.spec.shots) if shot.id == visual_repair.id),
                None,
            )
            if index is None:
                raise ValueError("visual repair references an unknown shot")
            original = project.spec.shots[index]
            if original.kind != "big_metric" or re.search(r"\d", original.metric):
                raise ValueError("only a nonnumeric big_metric can use this visual repair")
            immutable = (
                "id", "start", "duration", "title", "subtitle", "motion", "claim_ids",
                "source_ids", "voice_segments", "music_state", "sound_cues",
                "intentional_silence", "entrance_cues",
            )
            if any(getattr(original, key) != getattr(visual_repair, key) for key in immutable):
                raise ValueError("visual repair cannot change approved narration or shot timing")
            if len(visual_repair.alternatives) != 2 or len(
                {item.strip().casefold() for item in visual_repair.alternatives}
            ) != 2:
                raise ValueError("visual repair needs two distinct conceptual alternatives")
            if len(visual_repair.shot_type_reasoning.strip()) < 18:
                raise ValueError("visual repair needs an editorial shot-type rationale")
            repair_record = {
                "shot_id": original.id,
                "before": original.model_dump(mode="json"),
                "after": visual_repair.model_dump(mode="json"),
            }
            project.spec.shots[index] = visual_repair
            studio._verify_script_approval(project)
        plan = normalize_asset_plan(project, AssetPlan.model_validate_json(output_text(raw)))
        plan = validate_asset_plan(project, plan)
        output = repo.attempt_dir(project, "asset-plan-recovery")
        if repair_record is not None:
            studio._json(project, output, "visual-repair.json", repair_record, "visual_repair")
        studio._json(project, output, "cached-response.json", raw, "provider_usage")
        project.artifacts[-1].provider = "openai"
        project.artifacts[-1].model = raw.get(
            "model", project.live_models.get("director_model", "unknown")
        )
        project.artifacts[-1].cost = None
        studio._json(project, output, "asset-plan.json", plan.model_dump(mode="json"), "asset_plan")
        project.asset_plan = plan
        project.last_error = project.last_error_code = None
        repo.save(
            project,
            "asset_plan_revalidated",
            {
                "version": version,
                "actor": actor,
                "response_sha256": digest(path),
                "response_path": path.relative_to(repo.project_dir(project_id)).as_posix(),
                "attempts_preserved": project.asset_plan_attempts,
                "additional_api_calls": 0,
                "visual_repair_shot_id": repair_record["shot_id"] if repair_record else None,
            },
        )
        return project


async def prepare_assets(studio, project, output):
    repo = studio.repo
    if project.asset_plan is None:
        planner = studio.asset_planner or OpenAIAssetPlanner(studio.live_client)
        feedback = ""
        while project.asset_plan_attempts < 2:
            project.asset_plan_attempts += 1
            repo.save(
                project,
                "asset_plan_requested",
                {
                    "version": project.version,
                    "attempt": project.asset_plan_attempts,
                },
            )
            try:
                plan = await planner.plan(project, feedback)
                project.asset_plan = validate_asset_plan(project, plan)
                break
            except ValueError as exc:
                feedback = str(exc)[:500]
                studio.record_trace(
                    project, output, f"asset-planner-attempt-{project.asset_plan_attempts}"
                )
                repo.save(
                    project,
                    "asset_plan_rejected",
                    {
                        "version": project.version,
                        "attempt": project.asset_plan_attempts,
                        "error": feedback,
                    },
                )
        if project.asset_plan is None:
            raise AssetPreparationError(
                "Asset planning failed after two attempts; revise the brief"
            )
        studio._json(
            project,
            output,
            "asset-plan.json",
            project.asset_plan.model_dump(mode="json"),
            "asset_plan",
        )
        studio.record_trace(project, output, "asset-planner")
        repo.save(project, "asset_plan_prepared", {"version": project.version})
    plan = project.asset_plan
    music_direction = project.spec.brief.music
    music_direction.mood = plan.music_mood
    music_direction.energy = plan.music_energy
    music_direction.tempo = plan.music_tempo_bpm
    # Music validation happens before any image expenditure. Selection and evidence are snapshotted.
    if not project.current_artifacts("music_selection"):
        track, source, evidence, scores = select_music(
            Path(project.auto_assets.music_catalog),
            plan.music_tags,
            spec=music_direction,
            recent_track_ids=recent_music_track_ids(repo, project.id),
            required_duration=project.spec.brief.duration,
        )
        destination = output / f"catalog-{track.id}{source.suffix}"
        shutil.copyfile(source, destination)
        if digest(destination) != track.sha256:
            raise AssetPreparationError("Music changed during import")
        artifact = repo.artifact(
            project, destination, "music_source", provider="music-catalog", model="v0.7"
        )
        artifact.provenance = track.provenance.model_copy(deep=True)
        project.artifacts.append(artifact)
        license_path = output / "music-license.md"
        shutil.copyfile(evidence, license_path)
        project.artifacts.append(repo.artifact(project, license_path, "license_evidence"))
        music = project.spec.brief.music
        music.source_type, music.artifact_ref, music.trim_start = (
            "library",
            artifact.id,
            track.trim_start,
        )
        checked = await LibraryMusic(repo).create_or_select(
            project, music, output / "music-check.wav"
        )
        if pcm_metrics(repo.resolve(project.id, checked.artifact.path))["clipped_samples"]:
            raise AssetPreparationError(
                "Selected music clips before mixing; choose a new catalog track"
            )
        project.artifacts.append(checked.artifact.model_copy(update={"role": "music_preflight"}))
        studio._json(
            project,
            output,
            "music-selection.json",
            {
                "catalog_sha256": digest(Path(project.auto_assets.music_catalog)),
                "requested_tags": plan.music_tags,
                "scores": scores,
                "selected": track.model_dump(mode="json"),
                "music_source_id": artifact.id,
            },
            "music_selection",
        )
        repo.save(project, "music_selected", {"version": project.version, "track_id": track.id})
    images = studio.auto_images or OpenAIImages(studio.live_client, repo)
    for image in plan.images:
        key = (
            archival_fingerprint(image)
            if image.source_type == "archival"
            else fingerprint(project, image)
        )
        prepared = next((v for v in project.generated_visuals if v.image_id == image.id), None)
        if prepared:
            if prepared.fingerprint != key:
                raise AssetPreparationError("Prepared image prompt changed; create a new revision")
            repo.verify(project, prepared.artifact)
            continue
        source_type = image.source_type
        original_artifact_id = None
        if source_type == "archival":
            from autonomous_studio.providers.http import HTTP

            media = studio.media or WikimediaCommonsMedia(
                studio.live_client.http if studio.live_client else HTTP(), repo
            )
            try:
                acquired = await media.acquire(project, image, output / f"{image.id}.png")
                artifact = acquired.visual
                original_artifact_id = acquired.original.id
                project.artifacts.extend(
                    [
                        acquired.original,
                        acquired.visual,
                        acquired.metadata,
                        acquired.edit_manifest,
                    ]
                )
            except ArchivalMediaError as exc:
                if project.image_requests >= project.auto_assets.max_image_requests:
                    raise AssetPreparationError(
                        "Archival image unavailable and generated-image budget is exhausted"
                    ) from exc
                image.source_type = "generated"
                image.search_query = ""
                source_type = "generated"
                key = fingerprint(project, image)
                for look in plan.display:
                    if look.shot_id not in image.shot_ids:
                        continue
                    look.visual_strategy = (
                        "generated_image"
                        if look.visual_strategy == "archival_image"
                        else look.visual_strategy.removeprefix("archival_")
                    )
                project.production_changes.append(
                    f"No rights-compatible archival result for {image.id}; used a generated "
                    "editorial illustration instead."
                )
        if source_type == "generated":
            prompt = output / f"{image.id}.prompt.json"
            from autonomous_studio.providers.images import image_payload
            from autonomous_studio.storage.repository import write_json

            write_json(prompt, image_payload(project, image))
            artifact = await images.generate(project, image, output / f"{image.id}.png")
            project.artifacts.extend([artifact, repo.artifact(project, prompt, "image_prompt")])
        project.generated_visuals.append(
            GeneratedVisual(
                image_id=image.id,
                fingerprint=key,
                artifact=artifact,
                source_type=source_type,
                original_artifact_id=original_artifact_id,
            )
        )
        repo.save(
            project,
            "image_prepared",
            {"version": project.version, "image_id": image.id, "artifact_id": artifact.id},
        )
    looks = [s.model_copy(deep=True) for s in plan.display]
    by_image = {i.image_id: i.artifact.id for i in project.generated_visuals}
    for image in plan.images:
        for look in looks:
            if look.shot_id in image.shot_ids:
                look.image_ref = by_image[image.id]
    music_source = next(
        a for a in project.artifacts if a.id == project.spec.brief.music.artifact_ref
    )
    project.spec.presentation = Presentation(
        series_label=plan.series_label,
        shots=looks,
        music_credit=music_source.provenance.attribution,
    )
    publication_roles = {
        "youtube_title",
        "publication_description",
        "youtube_metadata",
        "source_manifest",
        "rights_manifest",
    }
    selection = json.loads(
        repo.verify(project, project.current_artifacts("music_selection")[-1]).read_text(
            encoding="utf-8"
        )
    )
    selected = CatalogTrack.model_validate(selection["selected"])
    if not publication_roles <= {artifact.role for artifact in project.current_artifacts()}:
        from autonomous_studio.workflow.publication import create_publication_package

        create_publication_package(
            repo,
            project,
            output,
            music_source,
            selected,
            category_id=str(project.creation_settings.get("youtube_category_id", "28")),
        )
    project.production_changes.append(
        f"Prepared {len(plan.images)} sourced or generated visuals, matched licensed music "
        "and a "
        "YouTube publication package automatically. "
        f"Image requests used: {project.image_requests}/{project.auto_assets.max_image_requests}."
    )
    project.warnings = studio.warnings_for(project)
    studio._json(
        project,
        output,
        "asset-validation.json",
        {
            "passed": True,
            "version": project.version,
            "images": len(project.generated_visuals),
            "image_requests": project.image_requests,
            "image_request_cap": project.auto_assets.max_image_requests,
            "license_evidence": True,
            "music_preflight": True,
            "music_source_library": selected.source_library,
            "youtube_publication_package": True,
            "semantic_review": "Human review required; technical QA is not factual verification",
        },
        "asset_validation",
    )
