import shutil
from pathlib import Path
from time import perf_counter

from autonomous_studio.audio.mixer import Mixer
from autonomous_studio.domain.models import ProviderSuccess, validate_timeline
from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.rendering.scenes import scene
from autonomous_studio.storage import Repository
from autonomous_studio.storage.repository import digest, write_json


def _copy_gallery_image(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if digest(destination) != digest(source):
            raise ValueError(f"thumbnail gallery image changed: {destination.name}")
        return
    shutil.copyfile(source, destination)


def _thumbnail_gallery(repo, project, stills):
    """Collect raw artwork and composed shot stills in one stable, browseable folder."""
    root = repo.project_dir(project.id) / "images" / f"v{project.version:03d}"
    artifacts, items = [], []
    for index, (shot, source) in enumerate(zip(project.spec.shots, stills, strict=True), 1):
        destination = root / f"{index:02d}-{shot.id}.png"
        _copy_gallery_image(source, destination)
        artifact = repo.artifact(
            project,
            destination,
            "thumbnail_candidate",
            model="v0.6-editorial",
            width=project.spec.brief.width,
            height=project.spec.brief.height,
        )
        if project.spec.presentation:
            look = next(
                (item for item in project.spec.presentation.shots if item.shot_id == shot.id),
                None,
            )
            source_artifact = next(
                (
                    item
                    for item in project.artifacts
                    if look and look.image_ref and item.id == look.image_ref
                ),
                None,
            )
            if source_artifact:
                artifact.provenance = source_artifact.provenance.model_copy(
                    deep=True,
                    update={
                        "source": "Composited frame using " + source_artifact.provenance.source,
                        "asset_id": source_artifact.id,
                    },
                )
        artifacts.append(artifact)
        items.append(
            {
                "kind": "composited_frame",
                "shot_id": shot.id,
                "path": artifact.path,
                "recommended": index == 1,
            }
        )
    for index, visual in enumerate(project.generated_visuals, 1):
        if visual.artifact.version != project.version:
            continue
        source = repo.verify(project, visual.artifact)
        destination = root / f"source-{index:02d}-{visual.image_id}.png"
        _copy_gallery_image(source, destination)
        artifact = repo.artifact(
            project,
            destination,
            "thumbnail_candidate",
            provider=visual.artifact.provider,
            model=visual.artifact.model,
            width=visual.artifact.width,
            height=visual.artifact.height,
        )
        artifact.input_reference = f"gallery-copy:{visual.artifact.id}"
        artifact.provenance = visual.artifact.provenance.model_copy(deep=True)
        artifacts.append(artifact)
        items.append(
            {
                "kind": "source_visual",
                "image_id": visual.image_id,
                "path": artifact.path,
                "recommended": False,
            }
        )
        if visual.original_artifact_id:
            original = next(
                (item for item in project.artifacts if item.id == visual.original_artifact_id),
                None,
            )
            if original:
                original_source = repo.verify(project, original)
                original_destination = root / (
                    f"original-{index:02d}-{visual.image_id}{original_source.suffix.lower()}"
                )
                _copy_gallery_image(original_source, original_destination)
                original_copy = repo.artifact(
                    project,
                    original_destination,
                    "thumbnail_source_original",
                    provider=original.provider,
                    model=original.model,
                    width=original.width,
                    height=original.height,
                )
                original_copy.input_reference = f"gallery-copy:{original.id}"
                original_copy.provenance = original.provenance.model_copy(deep=True)
                artifacts.append(original_copy)
                items.append(
                    {
                        "kind": "archival_original",
                        "image_id": visual.image_id,
                        "path": original_copy.path,
                        "recommended": False,
                    }
                )
    manifest = root / "manifest.json"
    write_json(
        manifest,
        {
            "schema_version": 1,
            "project_id": project.id,
            "project_version": project.version,
            "recommended_thumbnail": items[0]["path"] if items else None,
            "images": items,
        },
    )
    artifacts.append(repo.artifact(project, manifest, "thumbnail_gallery", model="v0.6"))
    return artifacts


class MockFrames:
    def __init__(self, repo: Repository):
        self.repo = repo

    async def generate(self, project, shot, output):
        scene(project, shot).save(output)
        return self.repo.artifact(
            project,
            output,
            "hero_frame",
            provider="mock-image",
            width=project.spec.brief.width,
            height=project.spec.brief.height,
        )


class LocalRenderer:
    def __init__(self, repo: Repository, ffmpeg: FFmpeg | None = None):
        self.repo, self.ffmpeg = repo, ffmpeg or FFmpeg()

    async def render(self, project, output: Path):
        started = perf_counter()
        brief = project.spec.brief
        validate_timeline(project.spec.shots, brief.duration, brief.research)
        render_scene = scene
        if project.spec.presentation:
            from autonomous_studio.rendering.editorial import scene as editorial_scene

            render_scene = editorial_scene
        image_paths = {}
        for shot in project.spec.shots:
            reference = shot.image_ref if shot.kind == "hero_image" else None
            if project.spec.presentation:
                look = next(
                    (s for s in project.spec.presentation.shots if s.shot_id == shot.id), None
                )
                reference = look.image_ref if look and look.image_ref else reference
            if reference:
                artifact = next((a for a in project.artifacts if a.id == reference), None)
                if artifact is None or artifact.role not in {"visual_asset", "hero_frame"}:
                    raise ValueError("hero image references missing visual artifact")
                image_paths[shot.id] = self.repo.verify(project, artifact)
        layout = []
        stills = []
        if project.spec.presentation:
            # Validate every caption at its actual segment time before expensive mixing/encoding.
            from autonomous_studio.rendering.editorial import caption_sample_progresses
            from autonomous_studio.rendering.object_motion import motion_sample_progresses

            for shot in project.spec.shots:
                samples = set(caption_sample_progresses(project, shot))
                samples.update((0.05, 0.25, 0.5, 0.75, 0.95))
                samples.update(motion_sample_progresses(shot, project))
                for progress in sorted(samples):
                    boxes = []
                    render_scene(project, shot, progress, image_paths.get(shot.id), records=boxes)
                    layout.append({"shot_id": shot.id, "progress": progress, "text_boxes": boxes})
                path = output / f"frame-{shot.id}.png"
                render_scene(project, shot, 0.85, image_paths.get(shot.id)).save(path)
                stills.append(path)
            from autonomous_studio.rendering.contrast import TEXT_CONTRAST_TARGET

            estimates = [
                box["contrast_estimate"]
                for sample in layout
                for box in sample["text_boxes"]
                if "contrast_estimate" in box
            ]
            if estimates and min(estimates) < TEXT_CONTRAST_TARGET:
                raise ValueError("editorial text contrast is below the minimum target")
        gallery_artifacts = _thumbnail_gallery(self.repo, project, stills) if stills else []
        paths, qa = Mixer(self.repo, self.ffmpeg).mix(project, output)

        def frames():
            for frame in range(round(brief.duration * brief.fps)):
                t = frame / brief.fps
                shot = next(
                    s for s in project.spec.shots if s.start <= t < s.start + s.duration - 1e-8
                )
                yield render_scene(
                    project, shot, (t - shot.start) / shot.duration, image_paths.get(shot.id)
                )

        silent = output / "visual.mp4"
        self.ffmpeg.encode(frames(), brief.width, brief.height, brief.fps, silent)
        movie = output / "prototype.mp4"
        self.ffmpeg.mux(silent, output / "mix.wav", movie)
        qa["video"] = self.ffmpeg.inspect(movie)
        qa["encoded_audio_loudness"] = self.ffmpeg.loudness(movie)
        if (qa["video"]["width"], qa["video"]["height"]) != (brief.width, brief.height):
            raise ValueError("rendered dimensions do not match spec")
        if not qa["video"]["has_audio"] or abs(qa["video"]["duration"] - brief.duration) > 0.1:
            raise ValueError("rendered duration or audio stream is invalid")
        qa["publishable"] = False
        qa["production_mode"] = brief.production_mode
        qa["closed_captions"] = project.spec.closed_captions
        qa["story_scope"] = {
            "shot_count": len(project.spec.shots),
            "expected_rank_count": brief.expected_rank_count,
            "rank_order": [s.rank for s in project.spec.shots if s.kind == "rank_reveal"],
        }
        qa["limitations"] = project.warnings
        if project.spec.presentation:
            qa["presentation"] = {"theme": "editorial", "layout_passed": True, "samples": layout}
            qa["presentation"]["text_contrast_target"] = TEXT_CONTRAST_TARGET
            qa["presentation"]["text_contrast_estimate_min"] = min(estimates) if estimates else None
            qa["presentation"]["contrast_sampling"] = (
                "5th/95th-percentile grayscale estimate under each text group at sampled "
                "motion frames; not per-pixel WCAG certification"
            )
            qa["music_attribution"] = project.spec.presentation.music_credit
            qa["visual_disclosure"] = "AI illustration, not archival evidence"
        qa["renderer"] = self.ffmpeg.run(["-version"]).stdout.splitlines()[0]
        qa_path = output / "qa.json"
        write_json(qa_path, qa)
        artifacts = [
            self.repo.artifact(project, p, f"stem_{p.stem}", duration=brief.duration) for p in paths
        ]
        artifacts.extend(
            [
                self.repo.artifact(project, silent, "silent_video", duration=brief.duration),
                self.repo.artifact(
                    project,
                    movie,
                    "prototype",
                    duration=brief.duration,
                    width=brief.width,
                    height=brief.height,
                    latency_seconds=perf_counter() - started,
                ),
                self.repo.artifact(project, qa_path, "qa"),
            ]
        )
        artifacts.extend(
            self.repo.artifact(
                project, path, "preview_frame", width=brief.width, height=brief.height
            )
            for path in stills
        )
        artifacts.extend(gallery_artifacts)
        return ProviderSuccess(artifacts=artifacts)
