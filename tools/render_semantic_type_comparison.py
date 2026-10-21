"""Offline A/B pilot for declarative spatial-cutaway and process-flow Short scenes.

Only the named shots are redrawn. Approved script/audio, project state and
publication files remain untouched. Topic-specific geometry lives in a JSON plan.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Annotated, Literal

from PIL import Image, ImageDraw, ImageOps
from pydantic import Field, model_validator

from autonomous_studio.domain.models import Model
from autonomous_studio.rendering.contrast import TEXT_CONTRAST_TARGET
from autonomous_studio.rendering.editorial import scene as original_scene
from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.rendering.object_motion import cue_onsets
from autonomous_studio.storage.repository import Repository, digest
from tools.render_short_motion_comparison import (
    BLUE,
    GOLD,
    INK,
    IVORY,
    SIZE,
    Painter,
    camera_frame,
    caption,
    ease,
)

FRAME = (35, 330, 505, 715)


class Component(Model):
    label: str = Field(min_length=2, max_length=45)
    shape: Literal["circle", "rectangle", "line", "arc"]
    start_box: tuple[int, int, int, int]
    end_box: tuple[int, int, int, int] | None = None
    label_box: tuple[int, int, int, int]
    cue: str | None = None
    color: Literal["gold", "blue", "ivory"] = "blue"
    arc_start: int = Field(default=0, ge=0, le=360)
    arc_end: int = Field(default=180, ge=0, le=360)

    @model_validator(mode="after")
    def bounds(self):
        for box in (self.start_box, self.end_box, self.label_box):
            if box is None:
                continue
            if not (FRAME[0] <= box[0] < box[2] <= FRAME[2]
                    and FRAME[1] <= box[1] < box[3] <= FRAME[3]):
                raise ValueError("diagram components must stay inside the mobile diagram frame")
        if self.end_box and not self.cue:
            raise ValueError("moving components require an exact narration cue")
        return self


class CutawayPlan(Model):
    kind: Literal["spatial_cutaway"] = "spatial_cutaway"
    shot_id: str
    heading: str = Field(min_length=3, max_length=38)
    components: list[Component] = Field(min_length=2, max_length=7)
    source_ids: list[str] = Field(min_length=1)
    accuracy_note: str = Field(min_length=12)


class FlowStep(Model):
    label: str = Field(min_length=3, max_length=44)
    cue: str = Field(min_length=2)


class FlowPlan(Model):
    kind: Literal["process_flow"] = "process_flow"
    shot_id: str
    heading: str = Field(min_length=3, max_length=38)
    steps: list[FlowStep] = Field(min_length=2, max_length=4)
    source_ids: list[str] = Field(min_length=1)
    loop_crossfade_seconds: float = Field(default=2.6, ge=1, le=4)


PilotShot = Annotated[CutawayPlan | FlowPlan, Field(discriminator="kind")]


class PilotPlan(Model):
    project_id: str
    project_version: int = Field(ge=1)
    baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    shots: list[PilotShot] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_shots(self):
        ids = [shot.shot_id for shot in self.shots]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate pilot shot IDs")
        return self


def phrase_time(project, shot, phrase: str) -> float:
    proxy = shot.model_copy(update={"entrance_cues": [phrase]})
    return cue_onsets(project, proxy)[0]


def validate_plan(project, plan: PilotPlan, baseline: Path):
    if (plan.project_id, plan.project_version, plan.baseline_sha256) != (
        project.id, project.version, digest(baseline)
    ):
        raise ValueError("pilot plan does not match the immutable baseline preview")
    if project.state != "COMPLETED" or not project.spec.presentation:
        raise ValueError("pilot requires a completed editorial Short")
    by_id = {shot.id: shot for shot in project.spec.shots}
    source_ids = {source.id for source in project.spec.brief.research.sources}
    for proposal in plan.shots:
        shot = by_id.get(proposal.shot_id)
        if shot is None or shot.kind not in {"hero_image", "outro"}:
            raise ValueError("pilot targets must be existing hero or outro shots")
        if proposal.kind == "process_flow" and shot.kind != "outro":
            raise ValueError("loop-aware process-flow pilot requires an outro")
        if not set(proposal.source_ids) <= set(shot.source_ids) & source_ids:
            raise ValueError("pilot diagram lacks shot-level source support")
        phrases = (
            [component.cue for component in proposal.components if component.cue]
            if proposal.kind == "spatial_cutaway"
            else [step.cue for step in proposal.steps]
        )
        times = [phrase_time(project, shot, phrase) for phrase in phrases]
        if times != sorted(times):
            raise ValueError("diagram cues must follow narration order")
        if proposal.kind == "process_flow" and project.spec.loop.strategy == "semantic":
            if not project.spec.loop.match_opening_visual:
                raise ValueError("process-flow pilot would break the approved loop visual")


def _interpolate(start, end, fraction):
    return tuple(round(a + (b - a) * fraction) for a, b in zip(start, end, strict=True))


def _component(painter: Painter, item: Component, fraction: float):
    box = _interpolate(item.start_box, item.end_box or item.start_box, fraction)
    color = {"gold": GOLD, "blue": BLUE, "ivory": IVORY}[item.color]
    draw = ImageDraw.Draw(painter.layer)
    if item.shape == "circle":
        draw.ellipse(box, outline=color, width=6)
        inset = 10
        if box[2] - box[0] > 2 * inset and box[3] - box[1] > 2 * inset:
            draw.ellipse((box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset),
                         outline=color, width=2)
    elif item.shape == "rectangle":
        draw.rounded_rectangle(box, radius=9, outline=color, width=6)
    elif item.shape == "arc":
        draw.arc(box, start=item.arc_start, end=item.arc_end, fill=color, width=9)
    else:
        draw.line((box[0], box[1], box[2], box[3]), fill=color, width=5)


def _source_background(project, proposal, repo, assets):
    look = next(x for x in project.spec.presentation.shots if x.shot_id == proposal.shot_id)
    if not look.image_ref:
        return Image.new("RGB", SIZE, INK), look, None
    source = repo.verify(project, assets[look.image_ref])
    with Image.open(source) as opened:
        return ImageOps.exif_transpose(opened).convert("RGB"), look, source


def render_frame(project, shot, proposal, source, look, source_path, local, report=None):
    progress = min(0.999999, local / shot.duration)
    background = camera_frame(source, {
        "focus_start": [0.48, 0.48], "focus_end": [0.52, 0.52],
        "zoom_start": 1.01, "zoom_end": 1.055,
    }, progress)
    painter = Painter(background, local, report if report is not None else [])
    painter.text((look.eyebrow or "").upper(), (42, 116, 498, 153), 18,
                 color=GOLD, at=0, fade=0.01)
    painter.text(proposal.heading.upper(), (42, 180, 498, 285), 56,
                 display=True, at=0, fade=0.01)
    if proposal.kind == "spatial_cutaway":
        painter.box(FRAME, minimum=205)
        painter.text("SCHEMATIC - NOT TO SCALE", (49, 344, 490, 382), 18,
                     color=GOLD, panel=False, fade=0.01)
        for component in proposal.components:
            start = phrase_time(project, shot, component.cue) if component.cue else 0
            fraction = ease((local - start) / 0.9)
            if component.cue and local < start:
                continue
            _component(painter, component, fraction)
            painter.text(component.label, component.label_box, 19,
                         color=GOLD if component.color == "gold" else IVORY,
                         at=start, fade=0.3)
    else:
        top, spacing = 305, 101
        for index, step in enumerate(proposal.steps):
            start = phrase_time(project, shot, step.cue)
            fraction = ease((local - start) / 0.55)
            if fraction <= 0:
                continue
            y = top + index * spacing
            painter.box((42, y, 498, y + 81), minimum=180)
            painter.text(step.label, (58, y + 13, 481, y + 72), 27,
                         at=start, fade=0.35)
            if index:
                ImageDraw.Draw(painter.layer).line((270, y - 18, 270, y - 3),
                                                   fill=BLUE, width=3)
    caption(project, shot, painter)
    image = painter.finish()
    if proposal.kind == "process_flow":
        fade_start = shot.duration - proposal.loop_crossfade_seconds
        blend = ease((local - fade_start) / proposal.loop_crossfade_seconds)
        if blend > 0:
            original = original_scene(project, shot, progress, source_path)
            original = original.resize(SIZE, Image.Resampling.LANCZOS)
            image = Image.blend(image, original, blend)
    return image


def _frame_count(seconds, fps):
    return round(seconds * fps)


def render(root: Path, plan_path: Path, *, stills_only: bool = False):
    plan = PilotPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    repo = Repository(root)
    project = repo.load(plan.project_id)
    baseline = repo.verify(project, project.current_artifacts("prototype")[-1])
    validate_plan(project, plan, baseline)
    project_file = repo.project_dir(project.id) / "project.json"
    before = digest(project_file)
    output = repo.project_dir(project.id) / "comparisons" / (
        f"semantic-types-v{project.version:03d}"
    )
    output.mkdir(parents=True, exist_ok=True)
    assets = {artifact.id: artifact for artifact in project.artifacts}
    ffmpeg = FFmpeg()
    fps = project.spec.brief.fps
    by_id = {shot.id: shot for shot in project.spec.shots}
    clip_paths = {}
    samples = {}
    contrast = []
    for proposal in plan.shots:
        shot = by_id[proposal.shot_id]
        source, look, source_path = _source_background(project, proposal, repo, assets)
        cue_times = (
            [phrase_time(project, shot, x.cue) for x in proposal.components if x.cue]
            if proposal.kind == "spatial_cutaway"
            else [phrase_time(project, shot, x.cue) for x in proposal.steps]
        )
        sample_times = {0.1, shot.duration * 0.25, shot.duration * 0.5,
                        shot.duration * 0.85, shot.duration - 0.08}
        sample_times.update(min(shot.duration - 0.08, t + 0.4) for t in cue_times)
        shot_contrast = []
        for local in sorted(sample_times):
            report = []
            render_frame(project, shot, proposal, source, look, source_path, local, report)
            shot_contrast.extend(item["contrast_estimate"] for item in report
                                 if "contrast_estimate" in item)
        if not shot_contrast or min(shot_contrast) < TEXT_CONTRAST_TARGET:
            raise ValueError(f"{shot.id}: pilot fails sampled text contrast")
        contrast.extend(shot_contrast)

        if stills_only:
            for index, local in enumerate(sorted(sample_times)):
                path = output / f"{shot.id}-sample-{index:02}.png"
                render_frame(project, shot, proposal, source, look, source_path, local).save(path)
                samples.setdefault(shot.id, []).append(str(path))
            continue

        def frames():
            for frame in range(_frame_count(shot.duration, fps)):
                local = frame / fps
                image = render_frame(project, shot, proposal, source, look, source_path, local)
                if frame in (round(fps * 0.8), round(fps * shot.duration * 0.55),
                             _frame_count(shot.duration, fps) - 2):
                    path = output / f"{shot.id}-frame-{frame:04}.png"
                    image.save(path)
                    samples.setdefault(shot.id, []).append(str(path))
                yield image

        path = output / f"{shot.id}-alternative-silent.mp4"
        ffmpeg.encode(frames(), *SIZE, fps, path)
        clip_paths[shot.id] = path

    if stills_only:
        result = {"stills": samples, "sampled_contrast_min": min(contrast),
                  "project_state_changed": digest(project_file) != before}
        print(json.dumps(result, indent=2))
        return result

    # Reuse the baseline pixels in every unchanged interval; only pilot shots
    # are redrawn. Exact frame boundaries prevent audio/visual drift.
    segments = []
    cursor = 0
    inputs = [baseline]
    filters = []
    for proposal in sorted(plan.shots, key=lambda x: by_id[x.shot_id].start):
        shot = by_id[proposal.shot_id]
        start = _frame_count(shot.start, fps)
        if start > cursor:
            label = f"b{len(segments)}"
            filters.append(
                f"[0:v]trim=start_frame={cursor}:end_frame={start},"
                f"scale={SIZE[0]}:{SIZE[1]},setpts=PTS-STARTPTS[{label}]"
            )
            segments.append(label)
        inputs.append(clip_paths[shot.id])
        label = f"p{len(segments)}"
        filters.append(f"[{len(inputs) - 1}:v]setpts=PTS-STARTPTS[{label}]")
        segments.append(label)
        cursor = start + _frame_count(shot.duration, fps)
    total = _frame_count(project.spec.brief.duration, fps)
    if cursor < total:
        label = f"b{len(segments)}"
        filters.append(
            f"[0:v]trim=start_frame={cursor}:end_frame={total},"
            f"scale={SIZE[0]}:{SIZE[1]},setpts=PTS-STARTPTS[{label}]"
        )
        segments.append(label)
    filters.append("".join(f"[{label}]" for label in segments) +
                   f"concat=n={len(segments)}:v=1:a=0[v]")
    variant = output / "alternative-full.mp4"
    ffmpeg.run([
        *[part for path in inputs for part in ("-i", path)],
        "-filter_complex", ";".join(filters), "-map", "[v]", "-map", "0:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", variant,
    ], timeout=600)
    comparison = output / "full-side-by-side.mp4"
    ffmpeg.run([
        "-i", baseline, "-i", variant,
        "-filter_complex", "[0:v]scale=540:960[left];[1:v]scale=540:960[right];"
        "[left][right]hstack=inputs=2[v]", "-map", "[v]", "-map", "0:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "copy", "-movflags", "+faststart", comparison,
    ], timeout=600)
    pairs = {}
    for proposal in plan.shots:
        shot = by_id[proposal.shot_id]
        path = output / f"{shot.id}-side-by-side.mp4"
        ffmpeg.run([
            "-i", comparison, "-ss", shot.start, "-t", shot.duration,
            "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "23", "-c:a", "copy", path,
        ], timeout=300)
        pairs[shot.id] = str(path)
    info = ffmpeg.inspect(variant)
    side_info = ffmpeg.inspect(comparison)
    if (info["width"], info["height"], info["has_audio"]) != (*SIZE, True):
        raise ValueError("alternative video streams are invalid")
    if abs(info["duration"] - project.spec.brief.duration) > 0.08:
        raise ValueError("alternative duration drifted from approved narration")
    if not side_info["has_audio"] or abs(side_info["duration"] - info["duration"]) > 0.08:
        raise ValueError("side-by-side comparison audio or duration is invalid")
    if digest(project_file) != before or digest(baseline) != plan.baseline_sha256:
        raise ValueError("pilot unexpectedly changed the completed project")
    result = {
        "project_id": project.id, "version": project.version,
        "baseline": str(baseline), "baseline_sha256": plan.baseline_sha256,
        "alternative": str(variant), "alternative_sha256": digest(variant),
        "side_by_side": str(comparison), "pairs": pairs, "stills": samples,
        "alternative_video": info, "comparison_video": side_info,
        "sampled_contrast_min": min(contrast), "provider_calls": 0,
        "project_state_changed": False, "publishable": False,
        "limitations": [
            "Pilot diagrams are explanatory schematics, not-to-scale historical reconstructions.",
            "Pilot is 540x960; production QA and rights clearance have not been applied.",
            "Narration/audio are the unchanged approved baseline; pilot visuals need human review.",
        ],
    }
    (output / "qa.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("--root", type=Path, default=Path("projects/live"))
    parser.add_argument("--stills-only", action="store_true")
    args = parser.parse_args()
    render(args.root, args.plan, stills_only=args.stills_only)
