"""Render an offline object-motion pilot beside one existing approved Short scene.

This is a comparison tool, not a production renderer. It reads a saved project,
reuses its exact scene timing and audio, and writes only under comparisons/.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from autonomous_studio.rendering.editorial import shade
from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.storage.repository import Repository, digest
from tools.render_short_motion_comparison import (
    BLUE,
    GOLD,
    INK,
    SIZE,
    Painter,
    caption,
    ease,
)

SUPPORTED = {
    "comparison", "data_chart", "timeline", "qualitative_comparison",
    "study_design", "big_metric", "metric_pair", "metric_set", "map",
}


def object_progress(local_time: float, index: int, duration: float) -> float:
    """Give every narrated object a readable entrance without rushing later ones."""
    onset = min(0.9 + index * 0.72, duration * 0.62)
    return ease((local_time - onset) / 0.85)


def background(project, look, repo, artifacts) -> Image.Image:
    if look.image_ref:
        path = repo.verify(project, artifacts[look.image_ref])
        with Image.open(path) as original:
            image = ImageOps.fit(ImageOps.exif_transpose(original).convert("RGB"), SIZE)
        return Image.alpha_composite(image.convert("RGBA"), shade(SIZE)).convert("RGB")
    return Image.new("RGB", SIZE, INK)


def _card(painter: Painter, rect, fraction: float):
    if fraction <= 0:
        return None
    lift = round((1 - fraction) * 42)
    moved = (rect[0], rect[1] + lift, rect[2], rect[3] + lift)
    painter.box(moved, minimum=210)
    return moved


def _bars(shot, painter: Painter, local: float):
    count = len(shot.values)
    maximum = max(max(shot.values), 0.001)
    top, bottom = 345, 735
    step = (bottom - top) / count
    for index, value in enumerate(shot.values):
        fraction = object_progress(local, index, shot.duration)
        y = round(top + index * step)
        rect = _card(painter, (34, y, 506, round(y + step - 12)), fraction)
        if rect is None:
            continue
        label = shot.labels[index] if index < len(shot.labels) else f"MEASURE {index + 1}"
        display = (
            shot.display_values[index]
            if index < len(shot.display_values) and shot.display_values[index]
            else f"{value:,.4g}"
        )
        painter.text(label, (47, rect[1] + 7, 492, rect[1] + 50),
                     23, color=GOLD, panel=False, fade=0.01)
        width = round(437 * value / maximum * fraction)
        if width:
            ImageDraw.Draw(painter.layer).rounded_rectangle(
                (49, rect[1] + 63, 49 + max(3, width), rect[1] + 82),
                radius=7, fill=BLUE,
            )
        painter.text(display, (49, rect[1] + 89, 492, rect[3] - 5),
                     24, panel=False, fade=0.01)


def _steps(shot, painter: Painter, local: float):
    items = shot.alternatives if shot.kind == "qualitative_comparison" else shot.steps
    step = 380 / len(items)
    for index, item in enumerate(items):
        fraction = object_progress(local, index, shot.duration)
        y = round(350 + index * step)
        rect = _card(painter, (34, y, 506, round(y + step - 15)), fraction)
        if rect:
            painter.text(item, (49, rect[1] + 12, 490, rect[3] - 10),
                         29, panel=False, fade=0.01)


def _timeline(shot, painter: Painter, local: float):
    items = shot.labels
    top, bottom = 350, 640
    reached = object_progress(local, len(items) - 1, shot.duration)
    ImageDraw.Draw(painter.layer).line(
        [(67, top), (67, top + round((bottom - top) * reached))], fill=BLUE, width=5,
    )
    for index, item in enumerate(items):
        fraction = object_progress(local, index, shot.duration)
        if not fraction:
            continue
        y = round(top + index * (bottom - top) / max(1, len(items) - 1))
        radius = 8 + round(5 * (1 - fraction))
        ImageDraw.Draw(painter.layer).ellipse(
            (67 - radius, y - radius, 67 + radius, y + radius), fill=GOLD,
        )
        rect = _card(painter, (94, y - 42, 505, y + 48), fraction)
        painter.text(item, (108, rect[1] + 5, 491, rect[3] - 5),
                     25, panel=False, fade=0.01)


def _metrics(shot, painter: Painter, local: float):
    if shot.kind == "big_metric":
        items = [("KEY MEASURE", shot.metric)]
    elif shot.kind == "metric_pair":
        items = [(f"MEASURE {i + 1}", value) for i, value in enumerate(shot.metrics)]
    else:
        items = list(zip(shot.labels, shot.display_values, strict=True))
    step = 380 / len(items)
    for index, (label, value) in enumerate(items):
        fraction = object_progress(local, index, shot.duration)
        y = round(350 + index * step)
        rect = _card(painter, (34, y, 506, round(y + step - 14)), fraction)
        if rect:
            painter.text(label, (48, rect[1] + 7, 492, rect[1] + 45),
                         21, color=GOLD, panel=False, fade=0.01)
            painter.text(value, (48, rect[1] + 45, 492, rect[3] - 9),
                         39 if len(items) == 1 else 29, display=True,
                         panel=False, fade=0.01)


def _map(shot, painter: Painter, local: float):
    fraction = object_progress(local, 0, shot.duration)
    if not fraction:
        return
    ink = ImageDraw.Draw(painter.layer)
    rect = (35, 365, 505, 686)
    painter.box(rect, minimum=210)
    for index in range(1, 5):
        x = 35 + index * 94
        ink.line([(x, 365), (x, 686)], fill=(101, 130, 140, 255), width=1)
    x = round(35 + (shot.longitude + 180) / 360 * 470)
    y = round(365 + (90 - shot.latitude) / 180 * 321)
    radius = round(9 + (1 - fraction) * 30)
    ink.ellipse((x - radius, y - radius, x + radius, y + radius),
                outline=BLUE, width=3)
    ink.ellipse((x - 8, y - 8, x + 8, y + 8), fill=GOLD)
    painter.text(shot.location_label, (46, 694, 495, 755), 27,
                 panel=True, fade=0.01)


def render_frame(project, shot, look, base: Image.Image, local: float,
                 report: list[dict] | None = None) -> Image.Image:
    validate_pilot_shot(shot)
    painter = Painter(base.copy(), local, report if report is not None else [])
    painter.text((look.eyebrow or "").upper(), (42, 107, 498, 151),
                 18, color=GOLD, at=0, fade=0.01)
    painter.text((look.headline or shot.title).upper(), (42, 165, 498, 312),
                 51, display=True, at=0, fade=0.01)
    if shot.kind in {"comparison", "data_chart"}:
        _bars(shot, painter, local)
    elif shot.kind in {"qualitative_comparison", "study_design"}:
        _steps(shot, painter, local)
    elif shot.kind == "timeline":
        _timeline(shot, painter, local)
    elif shot.kind == "map":
        _map(shot, painter, local)
    else:
        _metrics(shot, painter, local)
    painter.text(shot.subtitle, (40, 756, 500, 808), 22, at=0.2)
    caption(project, shot, painter)
    return painter.finish()


def validate_pilot_shot(shot):
    if shot.kind not in SUPPORTED:
        raise ValueError(f"no object-motion pilot for {shot.kind}")
    if shot.kind in {"comparison", "data_chart"} and (
        len(shot.display_values) != len(shot.values)
        or not all(value.strip() for value in shot.display_values)
    ):
        raise ValueError("quantitative pilot requires visible values and units for every bar")
    if shot.kind == "timeline" and any(
        "—" not in label and ":" not in label for label in shot.labels
    ):
        raise ValueError(
            "timeline pilot requires event labels; mixed dates and measurements are not chronology"
        )


def render(root: Path, project_id: str, shot_id: str, *, production: bool = False):
    repo = Repository(root)
    project = repo.load(project_id)
    if not project.spec.presentation:
        raise ValueError("pilot requires an existing editorial presentation")
    shot = next((item for item in project.spec.shots if item.id == shot_id), None)
    if shot is None:
        raise ValueError("choose an existing object-friendly shot")
    validate_pilot_shot(shot)
    look = next(item for item in project.spec.presentation.shots if item.shot_id == shot_id)
    baseline = repo.verify(project, project.current_artifacts("prototype")[-1])
    assets = {item.id: item for item in project.artifacts}
    image_path = repo.verify(project, assets[look.image_ref]) if look.image_ref else None
    base = None if production else background(project, look, repo, assets)
    output = repo.project_dir(project_id) / "comparisons" / (
        f"{'production-object-motion' if production else 'object-motion'}"
        f"-v{project.version:03d}-{shot_id}"
    )
    output.mkdir(parents=True, exist_ok=True)
    fps = project.spec.brief.fps
    silent = output / "pilot-silent.mp4"
    pilot = output / "pilot.mp4"
    comparison = output / "side-by-side.mp4"
    samples = {}
    contrast_min = None
    if production:
        from autonomous_studio.rendering.contrast import TEXT_CONTRAST_TARGET
        from autonomous_studio.rendering.editorial import (
            caption_sample_progresses,
        )
        from autonomous_studio.rendering.editorial import scene as production_scene
        from autonomous_studio.rendering.object_motion import motion_sample_progresses

        points = set(caption_sample_progresses(project, shot))
        points.update((0.05, 0.25, 0.5, 0.75, 0.95))
        points.update(motion_sample_progresses(shot))
        estimates = []
        for progress in sorted(points):
            records = []
            production_scene(project, shot, progress, image_path, records=records)
            estimates.extend(item["contrast_estimate"] for item in records
                             if "contrast_estimate" in item)
        contrast_min = min(estimates) if estimates else None
        if contrast_min is None or contrast_min < TEXT_CONTRAST_TARGET:
            raise ValueError("production motion failed sampled text contrast QA")

    def frames():
        for frame in range(round(shot.duration * fps)):
            local = frame / fps
            if production:
                image = production_scene(project, shot, local / shot.duration, image_path)
                image = image.resize(SIZE, Image.Resampling.LANCZOS)
            else:
                image = render_frame(project, shot, look, base, local)
            for name, target in (("start", 0.2), ("middle", shot.duration / 2),
                                 ("end", shot.duration - 0.3)):
                if name not in samples and local >= target:
                    path = output / f"{name}.png"
                    image.save(path)
                    samples[name] = str(path)
            yield image

    ffmpeg = FFmpeg()
    ffmpeg.encode(frames(), *SIZE, fps, silent)
    ffmpeg.run([
        "-i", silent, "-ss", shot.start, "-t", shot.duration, "-i", baseline,
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
        "-b:a", "128k", "-shortest", pilot,
    ], timeout=240)
    ffmpeg.run([
        "-ss", shot.start, "-t", shot.duration, "-i", baseline, "-i", pilot,
        "-filter_complex", "[0:v]scale=540:960[left];[1:v]scale=540:960[right];"
        "[left][right]hstack=inputs=2[v]", "-map", "[v]", "-map", "0:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-c:a", "aac",
        "-b:a", "128k", "-shortest", comparison,
    ], timeout=240)
    info = ffmpeg.inspect(pilot)
    if info["width"] != SIZE[0] or info["height"] != SIZE[1] or not info["has_audio"]:
        raise ValueError("pilot stream verification failed")
    if abs(info["duration"] - shot.duration) > 0.2:
        raise ValueError("pilot duration differs from approved shot")
    result = {
        "project_id": project_id, "version": project.version,
        "shot_id": shot_id, "shot_kind": shot.kind,
        "baseline_sha256": digest(baseline), "pilot_sha256": digest(pilot),
        "pilot": str(pilot), "side_by_side": str(comparison),
        "stills": samples, "video": info, "provider_calls": 0,
        "project_state_changed": False,
        "production_renderer": production,
        "sampled_text_contrast_min": contrast_min,
        "limitations": (
            ["Production scene downsampled to 540x960 for comparison, not publication clearance.",
             "Object entrances are staged within shots, not word-level synchronized."]
            if production else [
                "Offline 540x960 pilot, not a production export or publication clearance.",
                "Object timing is evenly staged; future production should align "
                "entrances to speech.",
                "A generated image is not separated into true depth layers.",
            ]
        ),
    }
    (output / "qa.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("shot_id")
    parser.add_argument("--root", type=Path, default=Path("projects/live"))
    parser.add_argument("--production", action="store_true")
    args = parser.parse_args()
    render(args.root, args.project_id, args.shot_id, production=args.production)
