"""Offline, opt-in motion/contrast comparison for an existing approved Short preview.

This tool never changes project state, calls a provider, or publishes a video. A
small JSON plan carries topic-specific camera targets and editorial intent.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from autonomous_studio.rendering.editorial import caption_at, shade, text_box
from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.storage.repository import Repository, digest

SIZE = (540, 960)
INK = (13, 20, 28)
IVORY = "#f7f5f0"
GOLD = "#f0c788"
BLUE = "#9cdbe0"


def ease(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def luminance(rgb: tuple[int, int, int]) -> float:
    values = [v / 255 for v in rgb]
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
    return sum(a * b for a, b in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    first, second = sorted((luminance(a), luminance(b)), reverse=True)
    return (first + 0.05) / (second + 0.05)


def percentile(gray: Image.Image, fraction: float) -> int:
    histogram = gray.histogram()
    threshold = sum(histogram) * fraction
    count = 0
    for value, frequency in enumerate(histogram):
        count += frequency
        if count >= threshold:
            return value
    return 255


def panel_opacity(
    background: Image.Image, rect: tuple[int, int, int, int], color: str
) -> tuple[int, float]:
    """Choose a local scrim from high-end brightness, not the image's average."""
    from PIL import ImageColor

    light = percentile(background.crop(rect).convert("L"), 0.95)
    text_color = ImageColor.getrgb(color)
    alpha = 0
    while alpha <= 255:
        blended = tuple(round(channel * (1 - alpha / 255) + dark * alpha / 255)
                        for channel, dark in zip((light,) * 3, INK, strict=True))
        if contrast(text_color, blended) >= 4.5:
            break
        alpha = min(255, alpha + 8)
        if alpha == 255:
            blended = INK
            break
    return alpha, contrast(text_color, blended)


def camera_frame(source: Image.Image, config: dict, progress: float) -> Image.Image:
    """Move a crop toward an editorially selected focus without fake depth layers."""
    width, height = SIZE
    start = config.get("focus_start", [0.5, 0.5])
    end = config.get("focus_end", start)
    zoom = config.get("zoom_start", 1.02) + (
        config.get("zoom_end", 1.10) - config.get("zoom_start", 1.02)
    ) * ease(progress)
    aspect = width / height
    base_width = min(source.width, source.height * aspect)
    crop_width = base_width / zoom
    crop_height = crop_width / aspect
    cx = (start[0] + (end[0] - start[0]) * ease(progress)) * source.width
    cy = (start[1] + (end[1] - start[1]) * ease(progress)) * source.height
    left = max(0, min(source.width - crop_width, cx - crop_width / 2))
    top = max(0, min(source.height - crop_height, cy - crop_height / 2))
    picture = source.crop((left, top, left + crop_width, top + crop_height))
    picture = picture.resize(SIZE, Image.Resampling.BICUBIC)
    # Match the approved editorial grade. Readability corrections belong around
    # the glyphs, not in a global exposure change that damages the artwork.
    return Image.alpha_composite(picture.convert("RGBA"), shade(SIZE)).convert("RGB")


class Painter:
    def __init__(self, background: Image.Image, local_time: float, report: list[dict]):
        self.background = background
        self.local_time = local_time
        self.report = report
        self.layer = Image.new("RGBA", SIZE)

    def box(self, rect, *, color=IVORY, minimum=180, radius=10):
        opacity, _ = panel_opacity(self.background, rect, color)
        opacity = max(minimum, opacity)
        element = Image.new("RGBA", SIZE)
        ImageDraw.Draw(element).rounded_rectangle(rect, radius=radius,
                                                   fill=(*INK, opacity))
        self.layer = Image.alpha_composite(self.layer, element)
        light = percentile(self.background.crop(rect).convert("L"), 0.95)
        blended = tuple(round(channel * (1 - opacity / 255) + dark * opacity / 255)
                        for channel, dark in zip((light,) * 3, INK, strict=True))
        from PIL import ImageColor

        ratio = contrast(ImageColor.getrgb(color), blended)
        self.report.append({"text": "grouped text backing", "contrast_estimate": round(ratio, 2),
                            "panel_alpha": opacity})
        return opacity, ratio

    def text(self, value: str, rect, size: int, *, color=IVORY, display=False,
             at=0.0, fade=0.3, panel=True):
        if not value or self.local_time < at:
            return
        strength = ease((self.local_time - at) / max(0.01, fade))
        element = Image.new("RGBA", SIZE)
        ink = ImageDraw.Draw(element)
        text_layer = Image.new("RGBA", SIZE)
        text_ink = ImageDraw.Draw(text_layer)
        text_box(text_ink, value, rect, size, color, display=display,
                 minimum=max(18, round(size * 0.62)))
        if panel:
            glyph = text_layer.getbbox()
            bounds = (max(0, glyph[0] - 10), max(0, glyph[1] - 7),
                      min(SIZE[0], glyph[2] + 10), min(SIZE[1], glyph[3] + 7))
            opacity, ratio = panel_opacity(self.background, bounds, color)
            if opacity:
                ink.rounded_rectangle(bounds, radius=8, fill=(*INK, opacity))
            self.report.append({"text": value, "contrast_estimate": round(ratio, 2),
                                "panel_alpha": opacity})
        element = Image.alpha_composite(element, text_layer)
        element.putalpha(element.getchannel("A").point(lambda value: round(value * strength)))
        self.layer = Image.alpha_composite(self.layer, element)

    def line(self, points, *, at, color=BLUE, width=3):
        if self.local_time < at:
            return
        element = Image.new("RGBA", SIZE)
        ImageDraw.Draw(element).line(points, fill=color, width=width)
        self.layer = Image.alpha_composite(self.layer, element)

    def finish(self) -> Image.Image:
        return Image.alpha_composite(self.background.convert("RGBA"), self.layer).convert("RGB")


def display_value(label: str, value: str) -> str:
    """A card label already supplies a repeated trailing unit/category."""
    if value.casefold().endswith(label.casefold()):
        return value[: -len(label)].strip()
    return value


def caption(project, shot, painter: Painter):
    if not project.spec.closed_captions:
        return
    for index, voice in enumerate(sorted(shot.voice_segments, key=lambda item: item.offset)):
        prepared = next(
            (item for item in project.prepared_voices
             if (item.shot_id, item.segment_index) == (shot.id, index)), None,
        )
        duration = prepared.asset.duration if prepared else voice.estimated_duration
        if voice.offset <= painter.local_time < voice.offset + duration:
            line = caption_at(voice.spoken_text, painter.local_time - voice.offset, duration)
            painter.box((35, 779, 505, 859), minimum=190)
            painter.text(line, (42, 787, 498, 851), 22, panel=False, fade=0.01)
            break


def render_frame(project, shot, look, source, config, progress, report):
    local = progress * shot.duration
    background = camera_frame(source, config, progress)
    painter = Painter(background, local, report)
    eyebrow = look.eyebrow or ""
    heading = (look.headline or shot.title).upper()
    template = config["template"]
    if template in {"hook", "loop"}:
        painter.text(eyebrow, (42, 130, 498, 165), 19, color=GOLD, at=0.15)
        painter.text(heading, (42, 211, 498, 550), 61, display=True, at=0.3, fade=0.75)
        if template == "loop":
            painter.text(look.annotation or "", (42, 642, 498, 706), 24,
                         at=0.6, color=BLUE)
    else:
        painter.text(eyebrow, (42, 126, 498, 164), 18, color=GOLD, at=0.0)
        painter.text(heading, (42, 204, 498, 339), 55, display=True, at=0.12)
        if template == "process":
            steps = config.get("steps", shot.steps)
            for index, step in enumerate(steps):
                y = 407 + index * 153
                at = 1.25 + index * 3.1
                if local >= at:
                    painter.box((33, y - 6, 507, y + 111), minimum=190)
                painter.text(f"0{index + 1}  {step}", (42, y, 498, y + 104), 27,
                             at=at, color=IVORY, fade=0.45, panel=False)
                if index:
                    painter.line([(270, 519), (270, 554)], at=at, color=BLUE)
        elif template == "convergence":
            for index, x in enumerate((68, 150, 390, 472)):
                painter.line([(x, 632), (270, 560)], at=1.2 + index * 0.32,
                             color=BLUE, width=2)
            painter.text(look.annotation or shot.subtitle, (42, 659, 498, 730), 25,
                         at=4.0)
        elif template == "metrics":
            for index, (label, value) in enumerate(
                zip(shot.labels, shot.display_values, strict=True)
            ):
                y = 365 + index * 131
                at = 1.3 + index * 3.0
                if local >= at:
                    painter.box((33, y - 7, 507, y + 111), minimum=190)
                painter.text(label.upper(), (42, y, 498, y + 35), 20,
                             color=GOLD, at=at, panel=False)
                painter.text(display_value(label, value), (42, y + 39, 498, y + 107),
                             31, display=True, at=at + 0.16, panel=False)
        else:
            painter.text(look.annotation or shot.subtitle, (42, 620, 498, 713), 25,
                         at=2.0)
    caption(project, shot, painter)
    return painter.finish()


def render(root: Path, project_id: str, version: int, plan_path: Path):
    repo = Repository(root)
    project = repo.load(project_id)
    if project.version != version or not project.current_artifacts("prototype"):
        raise ValueError("pilot requires the exact existing preview version")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("project_id") != project_id or plan.get("version") != version:
        raise ValueError("motion plan does not match the selected project/version")
    configs = {item["shot_id"]: item for item in plan["shots"]}
    if set(configs) != {shot.id for shot in project.spec.shots}:
        raise ValueError("motion plan must cover each shot exactly once")
    looks = {look.shot_id: look for look in project.spec.presentation.shots}
    assets = {artifact.id: artifact for artifact in project.artifacts}
    pictures = {}
    for shot in project.spec.shots:
        look = looks[shot.id]
        if not look.image_ref:
            raise ValueError(f"pilot shot lacks a reusable image: {shot.id}")
        path = repo.verify(project, assets[look.image_ref])
        with Image.open(path) as original:
            pictures[shot.id] = ImageOps.exif_transpose(original).convert("RGB")
        if configs[shot.id]["template"] == "process":
            if sorted(configs[shot.id].get("steps", [])) != sorted(shot.steps):
                raise ValueError("process plan may reorder but not rewrite approved steps")
    baseline = repo.verify(project, project.current_artifacts("prototype")[-1])
    output = repo.project_dir(project_id) / "comparisons" / f"short-motion-v{version:03d}"
    output.mkdir(parents=True, exist_ok=True)
    silent = output / "motion-silent.mp4"
    movie = output / "motion-short.mp4"
    comparison = output / "side-by-side.mp4"
    ffmpeg = FFmpeg()
    fps = project.spec.brief.fps
    shots = project.spec.shots
    report = []

    def frames():
        for frame in range(round(project.spec.brief.duration * fps)):
            time = frame / fps
            shot = next(item for item in shots
                        if item.start <= time < item.start + item.duration - 1e-8)
            progress = (time - shot.start) / shot.duration
            picture = render_frame(project, shot, looks[shot.id], pictures[shot.id],
                                   configs[shot.id], progress, report)
            if shot.id == shots[-1].id and progress > 0.84:
                first = shots[0]
                opening = render_frame(project, first, looks[first.id], pictures[first.id],
                                       configs[first.id], 0.0, [])
                picture = Image.blend(picture, opening, ease((progress - 0.84) / 0.16))
            if frame in {round(shot.start * fps + shot.duration * fps / 2) for shot in shots}:
                picture.save(output / f"frame-{shot.id}.png")
            yield picture

    ffmpeg.encode(frames(), *SIZE, fps, silent)
    ffmpeg.run([
        "-i", silent, "-i", baseline, "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart", "-shortest", movie,
    ], timeout=240)
    ffmpeg.run([
        "-i", baseline, "-i", movie,
        "-filter_complex", "[0:v]scale=540:960[left];[1:v]scale=540:960[right];"
        "[left][right]hstack=inputs=2[v]",
        "-map", "[v]", "-map", "0:a:0", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "24", "-c:a", "copy", "-shortest", comparison,
    ], timeout=300)
    video = ffmpeg.inspect(movie)
    if (video["width"], video["height"]) != SIZE or not video["has_audio"]:
        raise ValueError("motion pilot has invalid streams")
    if abs(video["duration"] - project.spec.brief.duration) > 0.1:
        raise ValueError("motion pilot duration changed")
    worst_by_text = {}
    for item in report:
        key = item["text"]
        if key not in worst_by_text or item["contrast_estimate"] < worst_by_text[key]["contrast_estimate"]:
            worst_by_text[key] = item
    result = {
        "project_id": project_id, "source_version": version,
        "baseline_sha256": digest(baseline), "motion_sha256": digest(movie),
        "source_audio": "copied unchanged from baseline MP4",
        "provider_calls": 0, "project_state_changed": False,
        "video": video,
        "text_contrast_estimate_min": min(item["contrast_estimate"] for item in report),
        "text_samples": list(worst_by_text.values()),
        "limitations": [
            "Offline comparison at 540x960; not publication quality or clearance.",
            "Text contrast estimates sample the 95th-percentile underlying luminance;"
            " they are a screening check, not a full per-pixel accessibility audit.",
            "Camera movement is a crop of one image, not real parallax or new footage.",
        ],
    }
    (output / "qa.json").write_text(json.dumps(result, indent=2, ensure_ascii=False),
                                    encoding="utf-8")
    print(json.dumps({"motion": str(movie), "comparison": str(comparison),
                      "contrast_min": result["text_contrast_estimate_min"],
                      "video": video}, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("--root", type=Path, default=Path("projects/live"))
    args = parser.parse_args()
    data = json.loads(args.plan.read_text(encoding="utf-8"))
    render(args.root, data["project_id"], data["version"], args.plan)
