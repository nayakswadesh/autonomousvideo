"""Render a data-driven, offline motion comparison for an existing long-form chapter.

The project's approved audio and original chapter are reused unchanged. A separate
JSON shot plan supplies editorial timing, camera paths, and reveal instructions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from autonomous_studio.domain.motion import MotionPlan, validate_motion_plan
from autonomous_studio.rendering.ffmpeg import FFmpeg

NAVY = (19, 29, 39)
GOLD = (224, 188, 127)
WHITE = (247, 245, 240)


def _at(shot: dict, action: str, now: float | None, fallback: float, progress: float) -> bool:
    cue = next((item for item in shot.get("cues", []) if item["action"] == action), None)
    return now >= cue["at_seconds"] if cue and now is not None else progress >= fallback


def _cue(shot: dict, action: str) -> dict | None:
    return next((item for item in shot.get("cues", []) if item["action"] == action), None)


def _text_wipe(frame: Image.Image, xy: tuple[int, int], value: str, font, color, amount: float):
    if amount <= 0:
        return
    layer = Image.new("RGB", frame.size, NAVY)
    ImageDraw.Draw(layer).text(xy, value, font=font, fill=color)
    box = ImageDraw.Draw(layer).textbbox(xy, value, font=font)
    right = xy[0] + (box[2] - xy[0]) * _ease(amount)
    frame.paste(layer.crop((xy[0], xy[1], int(right), box[3] + 3)), (xy[0], xy[1]))


def _ease(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    root = Path(__file__).resolve().parents[1]
    return ImageFont.truetype(root / "src/autonomous_studio/assets/fonts" / name, size)


def _timestamp(value: str) -> float:
    hours, minutes, seconds, millis = map(int, re.split(r"[:,]", value))
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def read_captions(path: Path) -> list[tuple[float, float, str]]:
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig")):
        lines = block.strip().splitlines()
        if len(lines) < 3 or " --> " not in lines[1]:
            continue
        start, end = lines[1].split(" --> ", 1)
        cues.append((_timestamp(start), _timestamp(end), " ".join(lines[2:])))
    return cues


def _wrapped(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        attempt = f"{current} {word}".strip()
        if draw.textbbox((0, 0), attempt, font=font)[2] > max_width and current:
            lines.append(current)
            current = word
        else:
            current = attempt
    if current:
        lines.append(current)
    return lines


def _draw_heading(draw: ImageDraw.ImageDraw, value: str, size: tuple[int, int], color) -> None:
    width, height = size
    scale = height / 540
    font_size = round(37 * scale)
    while True:
        font = _font("BarlowCondensed-Bold.ttf", font_size)
        lines = _wrapped(draw, value, font, width - round(84 * scale))
        if len(lines) <= 2 or font_size <= round(25 * scale):
            break
        font_size -= 2
    if len(lines) > 2:
        raise ValueError("Motion heading requires more than two lines")
    for index, line in enumerate(lines):
        draw.text(
            (round(42 * scale), round((48 + 42 * index) * scale)),
            line,
            font=font,
            fill=color,
        )


def _camera_crop(
    source: Image.Image, shot: dict, progress: float, size: tuple[int, int]
) -> tuple[float, float, float, float]:
    width, height = size
    source_width, source_height = source.size
    usable_height = source_height * (1 - shot.get("crop_bottom", 0))
    aspect = width / height
    base_width = min(source_width, usable_height * aspect)
    zoom = shot.get("zoom_start", 1.0) + (
        shot.get("zoom_end", 1.12) - shot.get("zoom_start", 1.0)
    ) * _ease(progress)
    crop_width = base_width / zoom
    crop_height = crop_width / aspect
    start = shot.get("focus_start", [0.5, 0.5])
    end = shot.get("focus_end", start)
    center_x = (start[0] + (end[0] - start[0]) * _ease(progress)) * source_width
    center_y = (start[1] + (end[1] - start[1]) * _ease(progress)) * source_height
    left = max(0, min(source_width - crop_width, center_x - crop_width / 2))
    top = max(0, min(usable_height - crop_height, center_y - crop_height / 2))
    return (left, top, left + crop_width, top + crop_height)


def _camera(source: Image.Image, shot: dict, progress: float, size: tuple[int, int]) -> Image.Image:
    if shot.get("presentation") == "contain":
        usable_height = round(source.height * (1 - shot.get("crop_bottom", 0)))
        content = source.crop((0, 0, source.width, usable_height))
        width, height = size
        zoom = shot.get("zoom_start", 1.0) + (
            shot.get("zoom_end", 1.04) - shot.get("zoom_start", 1.0)
        ) * _ease(progress)
        fitted = ImageOps.contain(
            content,
            (round(width * zoom), round(height * zoom)),
            Image.Resampling.BICUBIC,
        )
        frame = Image.new("RGB", size, NAVY)
        frame.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
        return frame
    return source.crop(_camera_crop(source, shot, progress, size)).resize(
        size, Image.Resampling.BICUBIC
    )


def _title_frame(
    shot: dict, progress: float, size: tuple[int, int], now: float | None = None
) -> Image.Image:
    width, height = size
    scale = height / 540

    def n(value):
        return round(value * scale)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    draw.rectangle((n(40), n(42), width - n(40), n(45)), fill=GOLD)
    draw.text(
        (n(42), n(70)),
        shot["eyebrow"],
        font=_font("BarlowCondensed-Bold.ttf", n(24)),
        fill=GOLD,
    )
    for action, key, y, color, fallback in (
        ("show_line_1", "line_1", 172, WHITE, 0.06),
        ("show_line_2", "line_2", 253, GOLD, 0.34),
        ("show_question", "question", 377, WHITE, 0.62),
    ):
        if not _at(shot, action, now, fallback, progress):
            continue
        cue = _cue(shot, action)
        amount = (
            (now - cue["at_seconds"]) / cue["duration_seconds"] if cue and now is not None else 1.0
        )
        font_name = "Barlow-Regular.ttf" if key == "question" else "BarlowCondensed-Bold.ttf"
        font_size = 30 if key == "question" else 68
        _text_wipe(frame, (n(42), n(y)), shot[key], _font(font_name, n(font_size)), color, amount)
    if _at(shot, "show_question", now, 0.62, progress):
        draw = ImageDraw.Draw(frame)
        draw.rectangle((n(42), n(352), width - n(42), n(356)), fill=GOLD)
    return frame


def _route_frame(
    shot: dict, progress: float, size: tuple[int, int], now: float | None = None
) -> Image.Image:
    width, height = size
    scale = height / 540

    def n(value):
        return round(value * scale)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    draw.text(
        (n(42), n(62)), shot["year"], font=_font("BarlowCondensed-Bold.ttf", n(60)), fill=GOLD
    )
    left, right, y = n(150), width - n(150), n(255)
    draw.line((left, y, right, y), fill=(80, 94, 101), width=n(4))
    cue = _cue(shot, "draw_route")
    fraction = (
        (now - cue["at_seconds"]) / cue["duration_seconds"]
        if cue and now is not None
        else (progress - 0.05) / 0.40
    )
    reveal = _ease(fraction)
    draw.line((left, y, left + (right - left) * reveal, y), fill=GOLD, width=n(8))
    for x, label, visible in (
        (left, shot["origin"], True),
        (right, shot["destination"], reveal > 0.92),
    ):
        draw.ellipse(
            (x - n(13), y - n(13), x + n(13), y + n(13)), fill=GOLD if visible else (80, 94, 101)
        )
        if visible:
            font = _font("BarlowCondensed-Bold.ttf", n(43))
            box = draw.textbbox((0, 0), label, font=font)
            draw.text(
                (x - (box[2] - box[0]) / 2, y + n(37)),
                label,
                font=font,
                fill=WHITE,
            )
    if reveal < 0.99:
        x = left + (right - left) * reveal
        draw.ellipse((x - n(8), y - n(8), x + n(8), y + n(8)), fill=WHITE)
    return frame


def _photo_frame(
    source: Image.Image,
    shot: dict,
    progress: float,
    size: tuple[int, int],
    now: float | None = None,
) -> Image.Image:
    frame = _camera(source, shot, progress, size)
    draw = ImageDraw.Draw(frame, "RGBA")
    width, height = size
    scale = height / 540

    def n(value):
        return round(value * scale)

    if shot.get("label") and _at(shot, "show_label", now, 0.0, progress):
        draw.rounded_rectangle(
            (n(32), n(32), min(width - n(32), n(52 + len(shot["label"]) * 16)), n(83)),
            radius=n(8),
            fill=(19, 29, 39, 210),
        )
        draw.text(
            (n(46), n(43)), shot["label"], font=_font("BarlowCondensed-Bold.ttf", n(27)), fill=WHITE
        )
    if shot.get("detail_marker") and _at(shot, "highlight_detail", now, 0.58, progress):
        marker = shot["detail_marker"]
        # Source-relative coordinates keep the pointer on the object as the camera moves.
        left, top, right, bottom = _camera_crop(source, shot, progress, size)
        x = int((marker[0] * source.width - left) * width / (right - left))
        y = int((marker[1] * source.height - top) * height / (bottom - top))
        cue = _cue(shot, "highlight_detail")
        fraction = (
            (now - cue["at_seconds"]) / cue["duration_seconds"]
            if cue and now is not None
            else (progress - 0.58) / 0.25
        )
        radius = n(25 + 24 * _ease(fraction))
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            outline=(*GOLD, 230),
            width=n(4),
        )
    return frame


def _draw_progressive_path(draw, points, fraction, *, fill, width):
    """Draw only the travelled fraction of a doodle path."""
    lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
    remaining = sum(lengths) * _ease(fraction)
    for a, b, length in zip(points, points[1:], lengths):
        if remaining <= 0:
            break
        travel = min(1.0, remaining / max(length, 1e-6))
        end = (a[0] + (b[0] - a[0]) * travel, a[1] + (b[1] - a[1]) * travel)
        draw.line((a, end), fill=fill, width=width, joint="curve")
        remaining -= length


def _guided_frame(source: Image.Image, shot: dict, size: tuple[int, int], now: float):
    """Annotate evidence as spoken; never replace the scene with a list of text."""
    progress = (now - shot["start"]) / (shot["end"] - shot["start"])
    frame = _camera(source, shot, progress, size)
    draw = ImageDraw.Draw(frame, "RGBA")
    width, height = size
    scale = height / 540
    stroke = max(3, round(4 * scale))
    for index, mark in enumerate(shot["marks"]):
        cue = next((item for item in shot["cues"]
                    if item["action"] == "reveal_step" and item["step_index"] == index), None)
        if cue is None or now < cue["at_seconds"]:
            continue
        amount = _ease((now - cue["at_seconds"]) / cue["duration_seconds"])
        points = [(round(x * width), round(y * height)) for x, y in mark["points"]]
        if mark["kind"] in {"trace", "arrow"}:
            _draw_progressive_path(draw, points, amount, fill=(*GOLD, 235), width=stroke)
            if mark["kind"] == "arrow" and amount >= 0.95:
                start, end = points[-2:]
                angle = math.atan2(end[1] - start[1], end[0] - start[0])
                length = round(15 * scale)
                wings = [
                    (end[0] - length * math.cos(angle + delta),
                     end[1] - length * math.sin(angle + delta))
                    for delta in (-0.55, 0.55)
                ]
                draw.line((wings[0], end, wings[1]), fill=(*GOLD, 235), width=stroke)
        elif mark["kind"] == "ring":
            x, y = points[0]
            radius = round(mark["radius"] * height)
            draw.arc((x - radius, y - radius, x + radius, y + radius),
                     start=-90, end=-90 + 360 * amount, fill=(*GOLD, 245), width=stroke)
        else:
            x, y = points[0]
            font = _font("BarlowCondensed-Bold.ttf", round(27 * scale))
            lines = _wrapped(draw, mark["label"], font, round(width * 0.35))
            if len(lines) > 2:
                raise ValueError(f"Guided tag is too long in {shot['id']}")
            line_height = round(31 * scale)
            panel_width = min(width - x - round(22 * scale),
                              max(draw.textbbox((0, 0), line, font=font)[2]
                                  for line in lines) + round(30 * scale))
            if panel_width < round(100 * scale):
                raise ValueError(f"Guided tag leaves frame in {shot['id']}")
            panel_height = line_height * len(lines) + round(15 * scale)
            draw.rounded_rectangle((x, y, x + panel_width, y + panel_height),
                                   radius=round(7 * scale), fill=(*NAVY, 226))
            for row, line in enumerate(lines):
                draw.text((x + round(14 * scale), y + round(5 * scale) + row * line_height),
                          line, font=font, fill=(*WHITE, round(255 * amount)))
    return frame


def _comparison_frame(shot: dict, size: tuple[int, int], now: float) -> Image.Image:
    """Reveal actual data values, deriving bar lengths rather than inventing proportions."""
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    title_font = _font("BarlowCondensed-Bold.ttf", n(37))
    body_font = _font("Barlow-Regular.ttf", n(27))
    draw.text((n(42), n(48)), shot["title"], font=title_font, fill=WHITE)
    maximum = max(shot["left_value"], shot["right_value"], 1e-9)
    for row, (action, label, value) in enumerate(
        (
            ("reveal_left", shot["left_label"], shot["left_value"]),
            ("reveal_right", shot["right_label"], shot["right_value"]),
        )
    ):
        cue = _cue(shot, action)
        if cue is None or now < cue["at_seconds"]:
            continue
        fraction = _ease((now - cue["at_seconds"]) / cue["duration_seconds"])
        y = n(155 + row * 145)
        draw.text((n(43), y - n(43)), label, font=body_font, fill=WHITE)
        draw.rounded_rectangle((n(42), y, width - n(42), y + n(42)), radius=n(8), fill=(56, 68, 78))
        extent = (width - n(84)) * (value / maximum) * fraction
        if extent > 2:
            draw.rounded_rectangle((n(42), y, n(42) + extent, y + n(42)), radius=n(8), fill=GOLD)
        formatted = f"{value:g} {shot['unit']}".strip()
        draw.text((n(42), y + n(48)), formatted, font=body_font, fill=GOLD)
    return frame


def _fact_frame(shot: dict, size: tuple[int, int], now: float) -> Image.Image:
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    draw.rectangle((n(42), n(55), width - n(42), n(59)), fill=GOLD)
    draw.text(
        (n(42), n(82)), shot["eyebrow"], font=_font("BarlowCondensed-Bold.ttf", n(27)), fill=GOLD
    )
    for action, key, y, color in (
        ("show_line_1", "line_1", 175, WHITE),
        ("show_line_2", "line_2", 285, GOLD),
    ):
        cue = _cue(shot, action)
        if cue and now >= cue["at_seconds"]:
            fraction = (now - cue["at_seconds"]) / cue["duration_seconds"]
            font_size = n(59)
            font = _font("BarlowCondensed-Bold.ttf", font_size)
            while draw.textbbox((0, 0), shot[key], font=font)[2] > width - n(84):
                font_size -= n(2)
                if font_size < n(30):
                    raise ValueError(f"Fact text is too long for shot {shot.get('id', '')}")
                font = _font("BarlowCondensed-Bold.ttf", font_size)
            _text_wipe(
                frame,
                (n(42), n(y)),
                shot[key],
                font,
                color,
                fraction,
            )
    return frame


def _concept_frame(shot: dict, size: tuple[int, int], now: float) -> Image.Image:
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    _draw_heading(draw, shot["title"], size, GOLD)
    margin, gap = n(42), n(25)
    card_width = (width - 2 * margin - gap) // 2
    font = _font("BarlowCondensed-Bold.ttf", n(36))
    for index, (action, key) in enumerate(
        (("reveal_left", "left_label"), ("reveal_right", "right_label"))
    ):
        cue = _cue(shot, action)
        if cue is None or now < cue["at_seconds"]:
            continue
        fraction = _ease((now - cue["at_seconds"]) / cue["duration_seconds"])
        x = margin + index * (card_width + gap)
        top, bottom = n(170), n(365)
        draw.rounded_rectangle(
            (x, top + n(25) * (1 - fraction), x + card_width, bottom),
            radius=n(12),
            fill=(42, 56, 68),
            outline=GOLD if index else (115, 132, 143),
            width=n(3),
        )
        lines = _wrapped(draw, shot[key], font, card_width - n(42))
        for line_number, line in enumerate(lines[:3]):
            draw.text((x + n(22), n(214) + line_number * n(44)), line, font=font, fill=WHITE)
    return frame


def _evidence_gap_frame(shot: dict, size: tuple[int, int], now: float) -> Image.Image:
    """Qualitative comparison: never encode invented numerical magnitude."""
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    _draw_heading(draw, shot["title"], size, WHITE)
    rows = (
        ("show_expected", "expected_label", 153, (105, 121, 132)),
        ("show_observed", "observed_label", 290, GOLD),
    )
    for action, key, y, color in rows:
        cue = _cue(shot, action)
        if cue and now >= cue["at_seconds"]:
            draw.rounded_rectangle(
                (n(42), n(y), width - n(42), n(y + 94)),
                radius=n(10),
                fill=(42, 56, 68),
                outline=color,
                width=n(3),
            )
            draw.text(
                (n(65), n(y + 21)),
                shot[key],
                font=_font("BarlowCondensed-Bold.ttf", n(34)),
                fill=WHITE,
            )
    if _at(shot, "show_gap", now, 1, 0):
        draw.rounded_rectangle(
            (width - n(240), n(232), width - n(42), n(290)),
            radius=n(8),
            fill=GOLD,
        )
        draw.text(
            (width - n(224), n(239)),
            shot["gap_label"],
            font=_font("BarlowCondensed-Bold.ttf", n(29)),
            fill=NAVY,
        )
    return frame


def _relationship_frame(shot: dict, size: tuple[int, int], now: float) -> Image.Image:
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    _draw_heading(draw, shot["title"], size, WHITE)
    gap, margin = n(16), n(42)
    count = len(shot["nodes"])
    card_width = (width - 2 * margin - gap * (count - 1)) // count
    font = _font("BarlowCondensed-Bold.ttf", n(29))
    for index, label in enumerate(shot["nodes"]):
        cue = next(
            (c for c in shot["cues"] if c["action"] == "reveal_step" and c["step_index"] == index),
            None,
        )
        if cue is None or now < cue["at_seconds"]:
            continue
        x = margin + index * (card_width + gap)
        draw.rounded_rectangle(
            (x, n(190), x + card_width, n(330)),
            radius=n(10),
            fill=(42, 56, 68),
            outline=GOLD,
            width=n(3),
        )
        for line_number, line in enumerate(_wrapped(draw, label, font, card_width - n(26))[:3]):
            draw.text((x + n(13), n(218) + line_number * n(34)), line, font=font, fill=WHITE)
        if index:
            draw.line((x - gap, n(260), x, n(260)), fill=GOLD, width=n(3))
    return frame


def _process_frame(shot: dict, size: tuple[int, int], now: float) -> Image.Image:
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    draw.text(
        (n(42), n(50)), shot["title"], font=_font("BarlowCondensed-Bold.ttf", n(37)), fill=WHITE
    )
    gap = n(16)
    total = len(shot["steps"])
    card_width = (width - n(84) - gap * (total - 1)) // total
    font = _font("Barlow-Regular.ttf", n(24))
    for index, step in enumerate(shot["steps"]):
        cue = next(
            (
                c
                for c in shot["cues"]
                if c["action"] == "reveal_step" and c.get("step_index") == index
            ),
            None,
        )
        if cue is None or now < cue["at_seconds"]:
            continue
        fraction = _ease((now - cue["at_seconds"]) / cue["duration_seconds"])
        x = n(42) + index * (card_width + gap)
        bottom = n(335)
        top = bottom - n(130) * fraction
        draw.rounded_rectangle(
            (x, top, x + card_width, bottom),
            radius=n(10),
            fill=(42, 56, 68),
            outline=GOLD,
            width=n(2),
        )
        if fraction > 0.85:
            lines = _wrapped(draw, step, font, card_width - n(28))
            for line_number, line in enumerate(lines[:3]):
                draw.text(
                    (x + n(14), top + n(18) + line_number * n(28)), line, font=font, fill=WHITE
                )
        if index > 0:
            draw.line((x - gap + n(3), n(270), x - n(3), n(270)), fill=GOLD, width=n(3))
    return frame


def _document_frame(source: Image.Image, shot: dict, size: tuple[int, int], now: float):
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    fitted = ImageOps.contain(source, size, Image.Resampling.BICUBIC)
    offset_x, offset_y = (width - fitted.width) // 2, (height - fitted.height) // 2
    frame.paste(fitted, (offset_x, offset_y))
    draw = ImageDraw.Draw(frame, "RGBA")
    if shot.get("label") and _at(shot, "show_label", now, 1, 0):
        draw.rounded_rectangle(
            (n(30), n(30), width - n(30), n(78)), radius=n(8), fill=(19, 29, 39, 215)
        )
        draw.text(
            (n(42), n(37)), shot["label"], font=_font("BarlowCondensed-Bold.ttf", n(27)), fill=WHITE
        )
    cue = _cue(shot, "highlight_document")
    if cue and now >= cue["at_seconds"]:
        amount = _ease((now - cue["at_seconds"]) / cue["duration_seconds"])
        left, top, right, bottom = shot["highlight_box"]
        rectangle = (
            offset_x + left * fitted.width,
            offset_y + top * fitted.height,
            offset_x + (left + (right - left) * amount) * fitted.width,
            offset_y + bottom * fitted.height,
        )
        draw.rectangle(rectangle, outline=(*GOLD, 240), width=n(4))
    return frame


def _caption(frame: Image.Image, text: str) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")
    width, height = frame.size
    scale = height / 540

    def n(value):
        return round(value * scale)

    font = _font("Barlow-Regular.ttf", n(24))
    lines = _wrapped(draw, text, font, width - n(100))
    line_height = n(30)
    top = height - len(lines) * line_height - n(28)
    draw.rounded_rectangle(
        (n(30), top - n(10), width - n(30), height - n(13)),
        radius=n(8),
        fill=(12, 18, 25, 215),
    )
    for index, line in enumerate(lines):
        box = draw.textbbox((0, 0), line, font=font)
        draw.text(
            ((width - (box[2] - box[0])) / 2, top + index * line_height),
            line,
            font=font,
            fill=WHITE,
        )


def validate_plan(plan: dict, project: dict) -> tuple[dict, float]:
    if plan["project_id"] != project["id"]:
        raise ValueError("Plan project ID does not match project")
    chapter = next((x for x in project["spec"]["chapters"] if x["id"] == plan["chapter_id"]), None)
    if chapter is None:
        raise ValueError("Chapter not found")
    known_assets = set(project["longform_media"])
    end = 0.0
    for shot in plan["shots"]:
        if abs(shot["start"] - end) > 0.01 or shot["end"] <= shot["start"]:
            raise ValueError("Shots must form a contiguous, positive timeline")
        if shot["kind"] not in {"title_reveal", "camera", "route_reveal", "detail_reveal"}:
            raise ValueError(f"Unsupported shot kind: {shot['kind']}")
        if shot.get("asset_ref") and shot["asset_ref"] not in known_assets:
            raise ValueError(f"Unknown asset: {shot['asset_ref']}")
        if shot["kind"] in {"camera", "detail_reveal"} and not shot.get("asset_ref"):
            raise ValueError("Camera shots require an asset")
        end = shot["end"]
    if abs(end - chapter["target_duration"]) > 0.1:
        raise ValueError("Plan duration differs from approved chapter duration")
    return chapter, end


def render(plan_file: Path, projects_root: Path) -> Path:
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    project_dir = (projects_root / plan["project_id"]).resolve()
    if not project_dir.is_relative_to(projects_root.resolve()):
        raise ValueError("Project must remain under projects root")
    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    if plan.get("schema_version") == 1:
        motion = MotionPlan.model_validate(plan)
        original_qa = project["longform_chapters"][motion.chapter_id]["path"]
        qa_path = (project_dir / original_qa).parent / "qa.json"
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
        chapter, duration = validate_motion_plan(motion, project, qa.get("speech_windows", []))
        plan = motion.model_dump(mode="json")
    else:
        chapter, duration = validate_plan(plan, project)
    original_rel = project["longform_chapters"][chapter["id"]]["path"]
    original = (project_dir / original_rel).resolve()
    if not original.is_relative_to(project_dir) or not original.exists():
        raise FileNotFoundError(original)
    caption_file = original.parent / "captions.srt"
    captions = (
        read_captions(caption_file)
        if project["spec"]["closed_captions"] and caption_file.exists()
        else []
    )
    output_dir = project_dir / "comparisons" / plan["comparison_name"]
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline = output_dir / "baseline-chapter.mp4"
    improved = output_dir / "motion-chapter.mp4"
    silent = output_dir / "motion-silent.mp4"
    side_by_side = output_dir / "side-by-side.mp4"
    shutil.copy2(original, baseline)
    width, height, fps = plan["width"], plan["height"], plan["fps"]
    sources = {}
    for shot in plan["shots"]:
        if "asset_ref" in shot:
            asset = plan.get("comparison_assets", {}).get(shot["asset_ref"])
            if asset is None:
                asset = project["longform_media"][shot["asset_ref"]]
            path = (project_dir / asset["path"]).resolve()
            if not path.is_relative_to(project_dir):
                raise ValueError("Asset must remain under project directory")
            if shot["asset_ref"] in plan.get("comparison_assets", {}):
                if hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
                    raise ValueError("Comparison asset hash changed")
            sources[shot["asset_ref"]] = Image.open(path).convert("RGB")
        if (
            plan.get("schema_version") == 1
            and shot["kind"] == "detail_reveal"
            and "detail_marker" in shot
        ):
            source = sources[shot["asset_ref"]]
            cue = _cue(shot, "highlight_detail")
            for when in (cue["at_seconds"], shot["end"] - 0.001):
                progress = (when - shot["start"]) / (shot["end"] - shot["start"])
                left, top, right, bottom = _camera_crop(source, shot, progress, (width, height))
                marker_x = shot["detail_marker"][0] * source.width
                marker_y = shot["detail_marker"][1] * source.height
                if not (left <= marker_x <= right and top <= marker_y <= bottom):
                    raise ValueError(f"Detail marker leaves the camera crop in {shot['id']}")
    ffmpeg = FFmpeg()
    command = ffmpeg.command(
        [
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgb24",
            "-video_size",
            f"{width}x{height}",
            "-framerate",
            str(fps),
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            silent,
        ]
    )
    with (output_dir / "encode.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log
        )
        try:
            shot_index = 0
            for frame_index in range(round(duration * fps)):
                now = frame_index / fps
                while (
                    shot_index + 1 < len(plan["shots"]) and now >= plan["shots"][shot_index]["end"]
                ):
                    shot_index += 1
                shot = plan["shots"][shot_index]
                progress = (now - shot["start"]) / (shot["end"] - shot["start"])
                if shot["kind"] == "title_reveal":
                    frame = _title_frame(shot, progress, (width, height), now)
                elif shot["kind"] == "fact_reveal":
                    frame = _fact_frame(shot, (width, height), now)
                elif shot["kind"] == "route_reveal":
                    frame = _route_frame(shot, progress, (width, height), now)
                elif shot["kind"] == "comparison_build":
                    frame = _comparison_frame(shot, (width, height), now)
                elif shot["kind"] == "concept_contrast":
                    frame = _concept_frame(shot, (width, height), now)
                elif shot["kind"] == "evidence_gap":
                    frame = _evidence_gap_frame(shot, (width, height), now)
                elif shot["kind"] == "relationship_map":
                    frame = _relationship_frame(shot, (width, height), now)
                elif shot["kind"] == "process_steps":
                    frame = _process_frame(shot, (width, height), now)
                elif shot["kind"] == "document_highlight":
                    frame = _document_frame(sources[shot["asset_ref"]], shot, (width, height), now)
                elif shot["kind"] == "guided_image":
                    frame = _guided_frame(sources[shot["asset_ref"]], shot,
                                          (width, height), now)
                else:
                    move_cue = _cue(shot, "start_move")
                    if move_cue:
                        progress = max(
                            0,
                            (now - move_cue["at_seconds"]) / (shot["end"] - move_cue["at_seconds"]),
                        )
                    frame = _photo_frame(
                        sources[shot["asset_ref"]], shot, progress, (width, height), now
                    )
                cue = next((text for start, end, text in captions if start <= now < end), "")
                if cue:
                    _caption(frame, cue)
                process.stdin.write(frame.tobytes())
            process.stdin.close()
            code = process.wait(timeout=600)
            if code:
                raise RuntimeError(f"Motion encode failed; see {output_dir / 'encode.log'}")
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
    ffmpeg.run(
        [
            "-i",
            silent,
            "-i",
            baseline,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-shortest",
            improved,
        ],
        timeout=600,
    )
    ffmpeg.run(
        [
            "-i",
            baseline,
            "-i",
            improved,
            "-filter_complex",
            f"[0:v]scale={width}:{height}[left];[1:v]scale={width}:{height}[right];"
            "[left][right]hstack=inputs=2[compare]",
            "-map",
            "[compare]",
            "-map",
            "0:a:0",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "copy",
            side_by_side,
        ],
        timeout=600,
    )

    def audio_hash(path: Path) -> str:
        return ffmpeg.run(
            [
                "-v",
                "error",
                "-i",
                path,
                "-map",
                "0:a:0",
                "-c",
                "copy",
                "-f",
                "hash",
                "-hash",
                "SHA256",
                "-",
            ],
            timeout=180,
        ).stdout.strip()

    media_qa = {
        name: ffmpeg.inspect(path, timeout=600)
        for name, path in (
            ("baseline", baseline),
            ("motion", improved),
            ("comparison", side_by_side),
        )
    }
    original_audio_hash = audio_hash(baseline)
    revised_audio_hash = audio_hash(improved)
    if original_audio_hash != revised_audio_hash:
        raise RuntimeError("The motion cut did not preserve the approved audio stream")
    if any(
        not item["decodable"] or not item["has_audio"] or abs(item["duration"] - duration) > 0.15
        for item in media_qa.values()
    ):
        raise RuntimeError("Comparison video failed duration or stream validation")
    (output_dir / "render-qa.json").write_text(
        json.dumps(
            {
                "schema_version": plan.get("schema_version", "legacy"),
                "project_id": project["id"],
                "chapter_id": chapter["id"],
                "approved_audio_sha256": original_audio_hash,
                "audio_preserved": True,
                "media": media_qa,
                "shot_intents": [
                    {"shot_id": shot["id"], "intent": shot["intent"], "kind": shot["kind"]}
                    for shot in plan["shots"]
                ]
                if plan.get("schema_version") == 1
                else [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    guided_count = sum(shot["kind"] == "guided_image" for shot in plan["shots"])
    visual_note = (
        f"The first {guided_count} story-led shots draw short, narration-anchored marks "
        "on imagery; later shots retain the existing opt-in motion treatment. "
        "Mark timing is estimated from measured speech windows and should be reviewed "
        "by listening, not treated as forced word alignment. "
        if guided_count else
        "The motion cut uses story-intent shots with narration-timed cues, directed "
        "camera movement, and schematic graphics. "
    )
    (output_dir / "comparison-notes.md").write_text(
        "# Long-form motion comparison\n\n"
        f"Project: {project['id']} / {chapter['id']} ({duration:.2f}s).\n\n"
        "In side-by-side.mp4, the original is LEFT and the revised motion cut is RIGHT. "
        "Both use the same approved chapter narration and mixed audio. "
        + visual_note + "Original project files and approval state "
        "are unchanged. Some reused imagery is AI-generated editorial illustration, "
        "not authentic archival photography; this is a quality comparison, not a "
        "publication-cleared release. See render-qa.json for stream and audio checks.\n",
        encoding="utf-8",
    )
    return side_by_side


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--projects-root", type=Path, default=Path("projects/live"))
    args = parser.parse_args()
    print(render(args.plan, args.projects_root))


if __name__ == "__main__":
    main()
