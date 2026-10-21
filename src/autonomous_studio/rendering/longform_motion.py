"""Production renderer for deterministic story-aware long-form motion plans.

Only the automatic planner's supported templates are rendered here. The rejected
guided-doodle experiment remains comparison-only and cannot enter production.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import TypeAdapter

from autonomous_studio.domain.motion import CameraMove, MotionPlan
from autonomous_studio.rendering.contrast import local_backing
from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.rendering.semantic_diagrams import Scene
from autonomous_studio.rendering.semantic_diagrams import render_frame as render_semantic_frame

NAVY = (19, 29, 39)
GOLD = (224, 188, 127)
WHITE = (247, 245, 240)
PRODUCTION_MOTION_VERSION = "auto-motion-production-5-family-first"


def _font(name: str, size: int):
    return ImageFont.truetype(
        Path(__file__).resolve().parents[1] / "assets/fonts" / name, max(12, size)
    )


def _ease(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def _cue(shot: dict, action: str, step: int | None = None):
    return next(
        (
            cue
            for cue in shot["cues"]
            if cue["action"] == action and (step is None or cue.get("step_index") == step)
        ),
        None,
    )


def _amount(shot: dict, action: str, now: float, step: int | None = None) -> float:
    cue = _cue(shot, action, step)
    return _ease((now - cue["at_seconds"]) / cue["duration_seconds"]) if cue else 0.0


def _visible(shot: dict, action: str, now: float, step: int | None = None) -> bool:
    cue = _cue(shot, action, step)
    return cue is not None and now >= cue["at_seconds"]


def _wrapped(draw, text: str, font, width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _wipe(frame: Image.Image, xy, text, font, color, amount):
    if amount <= 0:
        return
    layer = Image.new("RGB", frame.size, NAVY)
    draw = ImageDraw.Draw(layer)
    draw.text(xy, text, font=font, fill=color)
    box = draw.textbbox(xy, text, font=font)
    right = max(xy[0] + 1, round(xy[0] + (box[2] - xy[0]) * amount))
    frame.paste(layer.crop((xy[0], xy[1], right, box[3] + 3)), xy)


def _heading(draw, text, size, color=WHITE):
    width, height = size
    scale = height / 540
    font_size = round(37 * scale)
    while True:
        font = _font("BarlowCondensed-Bold.ttf", font_size)
        lines = _wrapped(draw, text, font, width - round(84 * scale))
        if len(lines) <= 2 or font_size <= round(25 * scale):
            break
        font_size -= 2
    if len(lines) > 2:
        raise ValueError("Motion heading cannot fit in two lines")
    for index, line in enumerate(lines):
        draw.text(
            (round(42 * scale), round((48 + 42 * index) * scale)), line, font=font, fill=color
        )


def _camera(source: Image.Image, shot: dict, now: float, size):
    move_cue = _cue(shot, "start_move")
    move_start = move_cue["at_seconds"] if move_cue else shot["start"]
    if shot.get("motion_style") == "hold_then_push":
        move_start += (shot["end"] - move_start) * shot.get("hold_fraction", 0.0)
    progress = _ease((now - move_start) / max(0.001, shot["end"] - move_start))
    start = shot.get("focus_start", [0.5, 0.5])
    end = shot.get("focus_end", start)
    zoom = (
        shot.get("zoom_start", 1.0)
        + (shot.get("zoom_end", 1.12) - shot.get("zoom_start", 1.0)) * progress
    )
    usable = round(source.height * (1 - shot.get("crop_bottom", 0)))
    if shot.get("presentation") == "contain":
        fitted = ImageOps.contain(
            source.crop((0, 0, source.width, usable)),
            (round(size[0] * zoom), round(size[1] * zoom)),
            Image.Resampling.BICUBIC,
        )
        frame = Image.new("RGB", size, NAVY)
        frame.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
        return frame
    crop_width = min(source.width, usable * size[0] / size[1]) / zoom
    crop_height = crop_width * size[1] / size[0]
    center_x = (start[0] + (end[0] - start[0]) * progress) * source.width
    center_y = (start[1] + (end[1] - start[1]) * progress) * source.height
    left = max(0, min(source.width - crop_width, center_x - crop_width / 2))
    top = max(0, min(usable - crop_height, center_y - crop_height / 2))
    return source.crop((left, top, left + crop_width, top + crop_height)).resize(
        size, Image.Resampling.BICUBIC
    )


def _photo_label_cards(shot: dict, size):
    width, height = size
    scale = height / 540
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    max_width = round(width * 0.59)
    cards = []
    for key, action in (("label", "show_label"), ("secondary_label", "show_secondary_label")):
        value = shot.get(key, "")
        if not value:
            continue
        for font_size in range(round(30 * scale), round(19 * scale) - 1, -max(1, round(scale))):
            font = _font("BarlowCondensed-Bold.ttf", font_size)
            lines = _wrapped(measure, value, font, max_width - round(38 * scale))
            if len(lines) <= 3 and all(
                measure.textbbox((0, 0), line, font=font)[2] <= max_width - round(38 * scale)
                for line in lines
            ):
                break
        else:
            raise ValueError(f"Image label cannot fit without truncation: {shot['id']}")
        pad = round(18 * scale)
        line_height = round(font.size * 1.14)
        card_width = max(
            round(90 * scale),
            max(measure.textbbox((0, 0), line, font=font)[2] for line in lines) + 2 * pad,
        )
        card_height = 2 * pad + line_height * len(lines)
        card = Image.new("RGBA", (card_width, card_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(card)
        if shot.get("label_effect") == "slide_plate":
            draw.rounded_rectangle(
                (0, 0, card_width - 1, card_height - 1),
                radius=round(9 * scale), fill=(*NAVY, 222),
            )
        for row, line in enumerate(lines):
            draw.text(
                (pad, pad + row * line_height), line, font=font, fill=(*WHITE, 255),
                stroke_width=max(2, round(2 * scale)), stroke_fill=(*NAVY, 255),
            )
        cards.append((action, card))
    return cards


def _animated_photo_label(frame, card, shot, action, now, xy):
    amount = _amount(shot, action, now)
    if amount <= 0:
        return
    effect = shot.get("label_effect", "fade_rise")
    x, y = xy
    if effect == "wipe":
        exposed = max(1, round(card.width * amount))
        piece = card.crop((0, 0, exposed, card.height))
        frame.paste(piece, (x, y), piece)
        accent = ImageDraw.Draw(frame)
        accent.rectangle(
            (x + exposed - 2, y + 4, x + exposed + 1, y + card.height - 4), fill=GOLD
        )
        return
    if effect == "gentle_pop":
        factor = 0.93 + 0.07 * amount
        piece = card.resize(
            (max(1, round(card.width * factor)), max(1, round(card.height * factor))),
            Image.Resampling.BICUBIC,
        )
        x += (card.width - piece.width) // 2
        y += (card.height - piece.height) // 2
    else:
        piece = card.copy()
        if effect == "fade_rise":
            y += round((1 - amount) * 14 * frame.height / 540)
        elif effect == "slide_plate":
            direction = -1 if shot.get("text_zone", "upper_left").endswith("left") else 1
            x += round(direction * (1 - amount) * 45 * frame.height / 540)
    if amount < 1:
        piece.putalpha(piece.getchannel("A").point(lambda value: round(value * amount)))
    frame.paste(piece, (x, y), piece)


def _opening_headline(frame: Image.Image, headline: str) -> Image.Image:
    """A legible first-frame/thumbnail treatment; the image model draws no text."""
    width, height = frame.size
    scale = height / 540
    draw = ImageDraw.Draw(frame)
    max_text = round(width * 0.63)
    for font_size in range(round(57 * scale), round(34 * scale) - 1, -max(1, round(scale))):
        font = _font("BarlowCondensed-Bold.ttf", font_size)
        lines = _wrapped(draw, headline, font, max_text)
        if len(lines) <= 3:
            break
    else:
        raise ValueError("Opening headline does not fit the thumbnail treatment")
    x, y = round(width * 0.045), round(height * 0.21)
    line_height = round(font.size * 1.06)
    plate_width = min(round(width * 0.73), max_text + round(42 * scale))
    plate_height = line_height * len(lines) + round(43 * scale)
    plate = Image.new("RGBA", (plate_width, plate_height), (0, 0, 0, 0))
    painter = ImageDraw.Draw(plate)
    painter.rounded_rectangle(
        (0, 0, plate_width - 1, plate_height - 1),
        radius=round(13 * scale), fill=(*NAVY, 206),
    )
    painter.rectangle(
        (0, 0, round(5 * scale), plate_height), fill=(*GOLD, 255)
    )
    for index, line in enumerate(lines):
        painter.text(
            (round(21 * scale), round(14 * scale) + index * line_height),
            line, font=font, fill=(*WHITE, 255),
        )
    frame.paste(plate, (x, y), plate)
    return frame


def render_motion_thumbnail(repo, project, plan: MotionPlan, output: Path) -> bool:
    """Render a separate 16:9 publication still from clean opening art."""
    first = plan.shots[0]
    if not isinstance(first, CameraMove) or not first.hook_headline:
        return False
    shot = first.model_dump(mode="json")
    from autonomous_studio.providers.longform_media import _clean_thumbnail_source

    source = _clean_thumbnail_source(repo, project, first.asset_ref)
    frame = _camera(source, shot, shot["start"] + 0.2, (1280, 720))
    _opening_headline(frame, first.thumbnail_headline or first.hook_headline)
    output.parent.mkdir(parents=True, exist_ok=True)
    frame.save(output, format="PNG")
    return True


def _photo(source, shot, now, size):
    frame = _camera(source, shot, now, size)
    if shot.get("hook_headline"):
        # Promise immediately; then hand the screen to the phrase-aligned facts.
        secondary = _cue(shot, "show_secondary_label")
        switch = (
            secondary["at_seconds"] if secondary else
            shot["start"] + (shot["end"] - shot["start"]) * 0.57
        )
        if now < switch:
            headline = _opening_headline(frame.copy(), shot["hook_headline"])
            opacity = min(1.0, max(0.0, (switch - now) / 0.45))
            frame = Image.blend(frame, headline, opacity)
            if shot.get("label") and _visible(shot, "show_label", now):
                scale = size[1] / 540
                draw = ImageDraw.Draw(frame, "RGBA")
                label_font = _font("BarlowCondensed-Bold.ttf", round(23 * scale))
                x, y = round(0.045 * size[0]), round(0.05 * size[1])
                width = draw.textbbox((0, 0), shot["label"], font=label_font)[2]
                draw.rounded_rectangle(
                    (x, y, x + width + round(24 * scale), y + round(36 * scale)),
                    radius=round(6 * scale), fill=(*NAVY, 208),
                )
                draw.text(
                    (x + round(12 * scale), y + round(4 * scale)),
                    shot["label"], font=label_font, fill=WHITE,
                )
            return frame
    if shot.get("label") or shot.get("secondary_label"):
        if shot.get("label_effect", "legacy") != "legacy":
            cards = _photo_label_cards(shot, size)
            total_height = sum(card.height for _, card in cards)
            total_height += max(0, len(cards) - 1) * round(12 * size[1] / 540)
            zone = shot.get("text_zone", "upper_left")
            desired = round(size[1] * (0.33 if zone.startswith("middle") else 0.07))
            top = min(desired, round(size[1] * 0.76) - total_height)
            if top < round(size[1] * 0.035):
                raise ValueError(f"Image labels overlap caption-safe region: {shot['id']}")
            for action, card in cards:
                x = round(size[0] * 0.035)
                if zone.endswith("right"):
                    x = size[0] - card.width - x
                # Protect only the pixels beneath each label when the actual
                # rendered image cannot meet the shared contrast target.
                if shot.get("label_effect") != "slide_plate":
                    alpha, _, backing = local_backing(
                        frame, (x, top, x + card.width, top + card.height), WHITE
                    )
                    if alpha:
                        protected = Image.new("RGBA", card.size, (0, 0, 0, 0))
                        ImageDraw.Draw(protected).rounded_rectangle(
                            (0, 0, card.width - 1, card.height - 1),
                            radius=round(8 * size[1] / 540),
                            fill=(*backing, min(230, alpha + 24)),
                        )
                        card = Image.alpha_composite(protected, card)
                _animated_photo_label(frame, card, shot, action, now, (x, top))
                top += card.height + round(12 * size[1] / 540)
            return frame
        draw = ImageDraw.Draw(frame, "RGBA")
        scale = size[1] / 540

        def n(value):
            return round(value * scale)

        for index, (key, action) in enumerate(
            (("label", "show_label"), ("secondary_label", "show_secondary_label"))
        ):
            value = shot.get(key, "")
            if not value or not _visible(shot, action, now):
                continue
            font = _font("BarlowCondensed-Bold.ttf", n(27))
            while draw.textbbox((0, 0), value, font=font)[2] > size[0] - n(100):
                font = _font("BarlowCondensed-Bold.ttf", max(n(18), font.size - n(2)))
            top = n(32 + 61 * index)
            text_width = draw.textbbox((0, 0), value, font=font)[2]
            draw.rounded_rectangle(
                (n(32), top, min(size[0] - n(32), n(72) + text_width), top + n(51)),
                radius=n(8),
                fill=(*NAVY, 215),
            )
            draw.text((n(46), top + n(9)), value, font=font, fill=WHITE)
    return frame


def _fact(shot, now, size):
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    draw.rectangle((n(42), n(55), width - n(42), n(59)), fill=GOLD)
    if shot.get("eyebrow"):
        draw.text(
            (n(42), n(82)),
            shot["eyebrow"],
            font=_font("BarlowCondensed-Bold.ttf", n(27)),
            fill=GOLD,
        )
    for action, key, y, color in (
        ("show_line_1", "line_1", 175, WHITE),
        ("show_line_2", "line_2", 285, GOLD),
    ):
        if not _visible(shot, action, now):
            continue
        font_size = n(59)
        while True:
            font = _font("BarlowCondensed-Bold.ttf", font_size)
            if draw.textbbox((0, 0), shot[key], font=font)[2] <= width - n(84):
                break
            font_size -= n(2)
            if font_size < n(30):
                raise ValueError(f"Fact text is too long for {shot['id']}")
        _wipe(frame, (n(42), n(y)), shot[key], font, color, _amount(shot, action, now))
    return frame


def _concept(shot, now, size):
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    _heading(draw, shot["title"], size, GOLD)
    margin, gap = n(42), n(25)
    card_width = (width - 2 * margin - gap) // 2
    font = _font("BarlowCondensed-Bold.ttf", n(36))
    for index, (action, key) in enumerate(
        (("reveal_left", "left_label"), ("reveal_right", "right_label"))
    ):
        if not _visible(shot, action, now):
            continue
        amount = _amount(shot, action, now)
        x = margin + index * (card_width + gap)
        top, bottom = n(170) + n(25) * (1 - amount), n(365)
        correction = shot.get("verdict_style") == "correction"
        accent = ((201, 75, 77) if index == 0 else (67, 189, 133)) if correction else GOLD
        draw.rounded_rectangle(
            (x, top, x + card_width, bottom),
            radius=n(12),
            fill=(42, 56, 68),
            outline=accent if correction or index else (115, 132, 143),
            width=n(3),
        )
        if correction:
            icon_x, icon_y = x + card_width - n(49), n(202)
            if index == 0:
                draw.line(
                    (icon_x - n(11), icon_y - n(11), icon_x + n(11), icon_y + n(11)),
                    fill=accent,
                    width=n(5),
                )
                draw.line(
                    (icon_x + n(11), icon_y - n(11), icon_x - n(11), icon_y + n(11)),
                    fill=accent,
                    width=n(5),
                )
            else:
                draw.line(
                    (
                        icon_x - n(12),
                        icon_y,
                        icon_x - n(2),
                        icon_y + n(10),
                        icon_x + n(16),
                        icon_y - n(13),
                    ),
                    fill=accent,
                    width=n(5),
                    joint="curve",
                )
        for row, line in enumerate(_wrapped(draw, shot[key], font, card_width - n(42))[:3]):
            draw.text((x + n(22), n(214) + row * n(44)), line, font=font, fill=WHITE)
    return frame


def _evidence_gap(shot, now, size):
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    _heading(draw, shot["title"], size)
    for action, key, y, color in (
        ("show_expected", "expected_label", 153, (105, 121, 132)),
        ("show_observed", "observed_label", 290, GOLD),
    ):
        if _visible(shot, action, now):
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
    if _visible(shot, "show_gap", now):
        draw.rounded_rectangle(
            (width - n(240), n(232), width - n(42), n(290)), radius=n(8), fill=GOLD
        )
        draw.text(
            (width - n(224), n(239)),
            shot["gap_label"],
            font=_font("BarlowCondensed-Bold.ttf", n(29)),
            fill=NAVY,
        )
    return frame


def _relationship(shot, now, size):
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    _heading(draw, shot["title"], size)
    gap, margin = n(16), n(42)
    count = len(shot["nodes"])
    card_width = (width - 2 * margin - gap * (count - 1)) // count
    font = _font("BarlowCondensed-Bold.ttf", n(29))
    for index, label in enumerate(shot["nodes"]):
        if not _visible(shot, "reveal_step", now, index):
            continue
        x = margin + index * (card_width + gap)
        draw.rounded_rectangle(
            (x, n(190), x + card_width, n(330)),
            radius=n(10),
            fill=(42, 56, 68),
            outline=GOLD,
            width=n(3),
        )
        for row, line in enumerate(_wrapped(draw, label, font, card_width - n(26))[:3]):
            draw.text((x + n(13), n(218) + row * n(34)), line, font=font, fill=WHITE)
        if index:
            draw.line((x - gap, n(260), x, n(260)), fill=GOLD, width=n(3))
    return frame


@lru_cache(maxsize=4)
def _channel_logo(size: int):
    path = Path(__file__).resolve().parents[1] / "assets/branding/channel-logo.png"
    if not path.is_file():
        return None
    with Image.open(path) as source:
        return ImageOps.contain(source.convert("RGB"), (size, size), Image.Resampling.LANCZOS)


def _outro(shot, now, size):
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame, "RGBA")
    draw.rectangle((n(42), n(45), width - n(42), n(49)), fill=GOLD)
    draw.text(
        (n(46), n(68)),
        "THE STORY SO FAR",
        font=_font("BarlowCondensed-Bold.ttf", n(36)),
        fill=WHITE,
    )
    logo = _channel_logo(n(175))
    if logo is not None:
        frame.paste(logo, (width - n(240), n(88)))
    for index, label in enumerate(shot["recap_items"]):
        if not _visible(shot, "reveal_step", now, index):
            continue
        y = n(130 + index * 59)
        draw.rounded_rectangle(
            (n(46), y, width - n(290), y + n(49)), radius=n(7), fill=(40, 56, 68, 235)
        )
        draw.ellipse((n(62), y + n(14), n(78), y + n(30)), fill=GOLD)
        font = _font("BarlowCondensed-Bold.ttf", n(29))
        draw.text((n(96), y + n(6)), label[:70], font=font, fill=WHITE)
    if _visible(shot, "show_question", now):
        draw.text(
            (n(47), n(375)),
            shot["question"][:95],
            font=_font("BarlowCondensed-Bold.ttf", n(25)),
            fill=GOLD,
        )
    if _visible(shot, "show_label", now):
        x, y = n(48), n(417)
        for kind, label in (("like", "LIKE"), ("share", "SHARE"), ("subscribe", "SUBSCRIBE")):
            color = (221, 55, 55) if kind == "subscribe" else GOLD
            draw.rounded_rectangle((x, y, x + n(31), y + n(28)), radius=n(5), fill=color)
            if kind == "subscribe":
                draw.polygon(
                    [(x + n(12), y + n(7)), (x + n(12), y + n(21)), (x + n(23), y + n(14))],
                    fill=WHITE,
                )
            elif kind == "share":
                draw.line((x + n(7), y + n(20), x + n(22), y + n(9)), fill=NAVY, width=n(3))
                draw.polygon(
                    [(x + n(18), y + n(7)), (x + n(25), y + n(6)), (x + n(24), y + n(14))],
                    fill=NAVY,
                )
            else:
                draw.polygon(
                    [
                        (x + n(7), y + n(13)),
                        (x + n(12), y + n(13)),
                        (x + n(16), y + n(6)),
                        (x + n(21), y + n(8)),
                        (x + n(19), y + n(13)),
                        (x + n(25), y + n(13)),
                        (x + n(22), y + n(23)),
                        (x + n(9), y + n(23)),
                    ],
                    fill=NAVY,
                )
            draw.text(
                (x + n(39), y), label, font=_font("BarlowCondensed-Bold.ttf", n(23)), fill=WHITE
            )
            x += n(150)
    return frame


def _process(shot, now, size):
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    draw.text(
        (n(42), n(50)), shot["title"], font=_font("BarlowCondensed-Bold.ttf", n(37)), fill=WHITE
    )
    gap = n(16)
    count = len(shot["steps"])
    card_width = (width - n(84) - gap * (count - 1)) // count
    font = _font("Barlow-Regular.ttf", n(24))
    for index, step in enumerate(shot["steps"]):
        if not _visible(shot, "reveal_step", now, index):
            continue
        amount = _amount(shot, "reveal_step", now, index)
        x = n(42) + index * (card_width + gap)
        bottom = n(335)
        top = bottom - n(130) * amount
        draw.rounded_rectangle(
            (x, top, x + card_width, bottom),
            radius=n(10),
            fill=(42, 56, 68),
            outline=GOLD,
            width=n(2),
        )
        if amount > 0.85:
            for row, line in enumerate(_wrapped(draw, step, font, card_width - n(28))[:3]):
                draw.text((x + n(14), top + n(18) + row * n(28)), line, font=font, fill=WHITE)
        if index:
            draw.line((x - gap + n(3), n(270), x - n(3), n(270)), fill=GOLD, width=n(3))
    return frame


def _sequence(shot, now, size):
    """A persistent visual scaffold with speech-timed, readable information steps."""
    if shot["layout"] == "hierarchy":
        return _hierarchy(shot, now, size)
    if shot["layout"] == "carousel":
        return _carousel(shot, now, size)
    width, height = size
    scale = height / 540

    def n(value):
        return round(value * scale)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    entries = shot["entries"]
    count = len(entries)
    layout = shot["layout"]
    columns = (
        min(count, 4)
        if layout == "timeline"
        else 3
        if layout == "flow" and count == 3
        else 2
        if count > 3
        else 1
    )
    rows = (count + columns - 1) // columns
    margin, gap = n(48), n(18)
    card_width = (width - 2 * margin - (columns - 1) * gap) // columns
    usable_height = n(305)
    card_height = (
        n(150) if layout == "timeline" else min(n(160), (usable_height - (rows - 1) * gap) // rows)
    )
    top = (
        n(260)
        if layout == "timeline"
        else n(105) + max(0, (usable_height - rows * card_height - (rows - 1) * gap) // 2)
    )
    # A visible but unlabeled structure starts on frame zero. No key information
    # enters before its measured spoken anchor.
    heading = shot.get("heading", "")
    if heading:
        font = _font("BarlowCondensed-Bold.ttf", n(32))
        if draw.textbbox((0, 0), heading, font=font)[2] > width - 2 * margin:
            raise ValueError(f"Sequence heading cannot fit in {shot['id']}")
        draw.text((margin, n(17)), heading, font=font, fill=WHITE)
    draw.line((margin, n(76), width - margin, n(76)), fill=(83, 102, 113), width=n(3))
    progress = max(0.0, min(1.0, (now - shot["start"]) / (shot["end"] - shot["start"])))
    draw.line(
        (margin, n(76), margin + (width - 2 * margin) * progress, n(76)), fill=GOLD, width=n(5)
    )
    if layout == "timeline":
        line_y = n(223)
        draw.line(
            (margin + card_width // 2, line_y, width - margin - card_width // 2, line_y),
            fill=(92, 113, 123),
            width=n(4),
        )
        draw.line(
            (
                margin + card_width // 2,
                line_y,
                margin + card_width // 2 + (width - 2 * margin - card_width) * progress,
                line_y,
            ),
            fill=GOLD,
            width=n(5),
        )
    revealed = [i for i in range(count) if _visible(shot, "reveal_step", now, i)]
    active = revealed[-1] if revealed else -1
    for index, label in enumerate(entries):
        row, column = divmod(index, columns)
        x = margin + column * (card_width + gap)
        y = top + row * (card_height + gap)
        visible = index in revealed
        accent = GOLD if index == active else (112, 130, 139)
        if layout == "timeline":
            dot_x = x + card_width // 2
            draw.ellipse(
                (dot_x - n(13), n(210), dot_x + n(13), n(236)),
                fill=accent if visible else (70, 84, 94),
            )
        elif layout == "flow" and index and column:
            arrow_y = y + card_height // 2
            draw.polygon(
                [(x - n(15), arrow_y - n(8)), (x - n(4), arrow_y), (x - n(15), arrow_y + n(8))],
                fill=GOLD,
            )
        draw.rounded_rectangle(
            (x, y, x + card_width, y + card_height),
            radius=n(11),
            fill=(39, 55, 67) if visible else (27, 40, 51),
            outline=accent if visible else (70, 84, 94),
            width=n(3),
        )
        if rows < 3:
            draw.ellipse(
                (x + n(17), y + n(18), x + n(35), y + n(36)),
                fill=accent if visible else (70, 84, 94),
            )
        if not visible:
            continue
        font_size = n(26 if rows >= 3 else 33)
        max_lines = 2 if rows >= 2 else 3
        text_offset = n(
            14 if rows >= 3 else 30 if rows == 2 else 38 if layout == "timeline" else 53
        )
        while True:
            font = _font("BarlowCondensed-Bold.ttf", font_size)
            lines = _wrapped(draw, label, font, card_width - n(52))
            line_height = min(n(43), font_size + n(3))
            if (
                len(lines) <= max_lines
                and text_offset + len(lines) * line_height <= card_height - n(5)
            ) or font_size <= n(19):
                break
            font_size -= n(2)
        if len(lines) > max_lines:
            raise ValueError(f"Sequence label cannot fit in {shot['id']}: {label}")
        text_top = y + text_offset
        if text_top + len(lines) * line_height > y + card_height - n(5):
            raise ValueError(f"Sequence label overflows {shot['id']}: {label}")
        for line_index, line in enumerate(lines):
            draw.text((x + n(23), text_top + line_index * line_height), line, font=font, fill=WHITE)
    # The marker keeps the scene gently moving during spoken explanation without
    # inventing an extra fact or prematurely revealing a label.
    marker_x = margin + (width - 2 * margin) * progress
    draw.ellipse((marker_x - n(6), n(70), marker_x + n(6), n(82)), fill=WHITE)
    return frame


def _carousel(shot, now, size):
    """A long recap keeps only the current point and two predecessors on screen."""
    width, height = size
    scale = height / 540

    def n(value):
        return round(value * scale)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    heading = shot.get("heading", "")
    if heading:
        font = _font("BarlowCondensed-Bold.ttf", n(32))
        if draw.textbbox((0, 0), heading, font=font)[2] > width - n(90):
            raise ValueError(f"Carousel heading cannot fit in {shot['id']}")
        draw.text((n(45), n(17)), heading, font=font, fill=WHITE)
    draw.line((n(45), n(76), width - n(45), n(76)), fill=(83, 102, 113), width=n(3))
    progress = max(0, min(1, (now - shot["start"]) / (shot["end"] - shot["start"])))
    draw.line((n(45), n(76), n(45) + (width - n(90)) * progress, n(76)), fill=GOLD, width=n(5))
    revealed = [
        index for index in range(len(shot["entries"])) if _visible(shot, "reveal_step", now, index)
    ]
    active = revealed[-1] if revealed else -1
    current = revealed[-3:]
    for row in range(3):
        top = n(114 + row * 101)
        bottom = top + n(85)
        entry_index = current[row] if row < len(current) else None
        focused = entry_index == active
        draw.rounded_rectangle(
            (n(45), top, width - n(45), bottom),
            radius=n(9),
            fill=(39, 55, 67) if entry_index is not None else (27, 40, 51),
            outline=GOLD
            if focused
            else (113, 132, 141)
            if entry_index is not None
            else (70, 84, 94),
            width=n(3),
        )
        if entry_index is None:
            continue
        label = shot["entries"][entry_index]
        font_size = n(37 if focused else 31)
        while True:
            font = _font("BarlowCondensed-Bold.ttf", font_size)
            lines = _wrapped(draw, label, font, width - n(155))
            if len(lines) <= 2 or font_size <= n(20):
                break
            font_size -= n(2)
        if len(lines) > 2:
            raise ValueError(f"Carousel label cannot fit in {shot['id']}: {label}")
        line_height = font_size + n(3)
        y = top + (n(85) - len(lines) * line_height) // 2
        for line in lines:
            draw.text((n(75), y), line, font=font, fill=WHITE)
            y += line_height
    return frame


def _hierarchy(shot, now, size):
    """Parent, parallel branches, and optional descendant—not a false sequence."""
    width, height = size
    scale = height / 540

    def n(value):
        return round(value * scale)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    heading = shot.get("heading", "")
    if heading:
        font = _font("BarlowCondensed-Bold.ttf", n(32))
        if draw.textbbox((0, 0), heading, font=font)[2] > width - n(90):
            raise ValueError(f"Hierarchy heading cannot fit in {shot['id']}")
        draw.text((n(45), n(17)), heading, font=font, fill=WHITE)
    progress = max(0, min(1, (now - shot["start"]) / (shot["end"] - shot["start"])))
    draw.line((n(45), n(76), width - n(45), n(76)), fill=(83, 102, 113), width=n(3))
    draw.line((n(45), n(76), n(45) + (width - n(90)) * progress, n(76)), fill=GOLD, width=n(5))
    boxes = [
        (n(310), n(100), width - n(310), n(170)),
        (n(45), n(205), width // 2 - n(15), n(290)),
        (width // 2 + n(15), n(205), width - n(45), n(290)),
    ]
    if len(shot["entries"]) == 4:
        boxes.append((n(310), n(335), width - n(310), n(410)))
    root_x = width // 2
    for index in (1, 2):
        left, top, right, _ = boxes[index]
        draw.line(
            (root_x, boxes[0][3], (left + right) // 2, top),
            fill=GOLD if _visible(shot, "reveal_step", now, index) else (76, 94, 104),
            width=n(3),
        )
    if len(boxes) == 4:
        for index in (1, 2):
            left, _, right, bottom = boxes[index]
            draw.line(
                ((left + right) // 2, bottom, root_x, boxes[3][1]),
                fill=GOLD if _visible(shot, "reveal_step", now, 3) else (76, 94, 104),
                width=n(3),
            )
    revealed = [i for i in range(len(boxes)) if _visible(shot, "reveal_step", now, i)]
    active = revealed[-1] if revealed else -1
    for index, (left, top, right, bottom) in enumerate(boxes):
        visible = index in revealed
        draw.rounded_rectangle(
            (left, top, right, bottom),
            radius=n(10),
            fill=(39, 55, 67) if visible else (27, 40, 51),
            outline=GOLD if index == active else (113, 132, 141) if visible else (70, 84, 94),
            width=n(3),
        )
        if not visible:
            continue
        label = shot["entries"][index]
        font_size = n(29)
        while True:
            font = _font("BarlowCondensed-Bold.ttf", font_size)
            lines = _wrapped(draw, label, font, right - left - n(30))
            if len(lines) <= 2 or font_size <= n(18):
                break
            font_size -= n(2)
        if len(lines) > 2:
            raise ValueError(f"Hierarchy label cannot fit in {shot['id']}: {label}")
        line_height = font_size + n(3)
        y = top + ((bottom - top) - len(lines) * line_height) // 2
        for line in lines:
            draw.text((left + n(15), y), line, font=font, fill=WHITE)
            y += line_height
    return frame


def _title(shot, now, size):
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    draw.rectangle((n(40), n(42), width - n(40), n(45)), fill=GOLD)
    draw.text(
        (n(42), n(70)), shot["eyebrow"], font=_font("BarlowCondensed-Bold.ttf", n(24)), fill=GOLD
    )
    for action, key, y, color, font_name, font_size in (
        ("show_line_1", "line_1", 172, WHITE, "BarlowCondensed-Bold.ttf", 68),
        ("show_line_2", "line_2", 253, GOLD, "BarlowCondensed-Bold.ttf", 68),
        ("show_question", "question", 377, WHITE, "Barlow-Regular.ttf", 30),
    ):
        if _visible(shot, action, now):
            _wipe(
                frame,
                (n(42), n(y)),
                shot[key],
                _font(font_name, n(font_size)),
                color,
                _amount(shot, action, now),
            )
    if _visible(shot, "show_question", now):
        ImageDraw.Draw(frame).rectangle((n(42), n(352), width - n(42), n(356)), fill=GOLD)
    return frame


def _route(shot, now, size):
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    draw.text(
        (n(42), n(62)), shot["year"], font=_font("BarlowCondensed-Bold.ttf", n(60)), fill=GOLD
    )
    left, right, y = n(150), width - n(150), n(255)
    draw.line((left, y, right, y), fill=(80, 94, 101), width=n(4))
    amount = _amount(shot, "draw_route", now)
    draw.line((left, y, left + (right - left) * amount, y), fill=GOLD, width=n(8))
    for x, label, shown in (
        (left, shot["origin"], True),
        (right, shot["destination"], amount > 0.92),
    ):
        draw.ellipse(
            (x - n(13), y - n(13), x + n(13), y + n(13)), fill=GOLD if shown else (80, 94, 101)
        )
        if shown:
            font = _font("BarlowCondensed-Bold.ttf", n(43))
            box = draw.textbbox((0, 0), label, font=font)
            draw.text((x - (box[2] - box[0]) / 2, y + n(37)), label, font=font, fill=WHITE)
    return frame


def _comparison(shot, now, size):
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    draw = ImageDraw.Draw(frame)
    draw.text(
        (n(42), n(48)), shot["title"], font=_font("BarlowCondensed-Bold.ttf", n(37)), fill=WHITE
    )
    maximum = max(shot["left_value"], shot["right_value"], 1e-9)
    for row, (action, label, value) in enumerate(
        (
            ("reveal_left", shot["left_label"], shot["left_value"]),
            ("reveal_right", shot["right_label"], shot["right_value"]),
        )
    ):
        if not _visible(shot, action, now):
            continue
        y = n(155 + row * 145)
        font = _font("Barlow-Regular.ttf", n(27))
        draw.text((n(43), y - n(43)), label, font=font, fill=WHITE)
        draw.rounded_rectangle((n(42), y, width - n(42), y + n(42)), radius=n(8), fill=(56, 68, 78))
        extent = (width - n(84)) * (value / maximum) * _amount(shot, action, now)
        if extent > 2:
            draw.rounded_rectangle((n(42), y, n(42) + extent, y + n(42)), radius=n(8), fill=GOLD)
        draw.text((n(42), y + n(48)), f"{value:g} {shot['unit']}".strip(), font=font, fill=GOLD)
    return frame


def _document(source, shot, now, size):
    width, height = size

    def n(value):
        return round(value * height / 540)

    frame = Image.new("RGB", size, NAVY)
    fitted = ImageOps.contain(source, size, Image.Resampling.BICUBIC)
    ox, oy = (width - fitted.width) // 2, (height - fitted.height) // 2
    frame.paste(fitted, (ox, oy))
    draw = ImageDraw.Draw(frame, "RGBA")
    if shot.get("label") and _visible(shot, "show_label", now):
        draw.rounded_rectangle((n(30), n(30), width - n(30), n(78)), radius=n(8), fill=(*NAVY, 215))
        draw.text(
            (n(42), n(37)), shot["label"], font=_font("BarlowCondensed-Bold.ttf", n(27)), fill=WHITE
        )
    if _visible(shot, "highlight_document", now):
        left, top, right, bottom = shot["highlight_box"]
        amount = _amount(shot, "highlight_document", now)
        draw.rectangle(
            (
                ox + left * fitted.width,
                oy + top * fitted.height,
                ox + (left + (right - left) * amount) * fitted.width,
                oy + bottom * fitted.height,
            ),
            outline=(*GOLD, 240),
            width=n(4),
        )
    return frame


def _caption(frame, text):
    draw = ImageDraw.Draw(frame, "RGBA")
    width, height = frame.size

    def n(value):
        return round(value * height / 540)

    font = _font("Barlow-Regular.ttf", n(24))
    lines = _wrapped(draw, text, font, width - n(100))
    top = height - len(lines) * n(30) - n(28)
    draw.rounded_rectangle(
        (n(30), top - n(10), width - n(30), height - n(13)), radius=n(8), fill=(12, 18, 25, 215)
    )
    for row, line in enumerate(lines):
        box = draw.textbbox((0, 0), line, font=font)
        draw.text(((width - (box[2] - box[0])) / 2, top + row * n(30)), line, font=font, fill=WHITE)


def _captions(path: Path):
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig")):
        lines = block.strip().splitlines()
        if len(lines) < 3 or " --> " not in lines[1]:
            continue

        def seconds(stamp):
            hour, minute, second, milli = map(int, re.split(r"[:,]", stamp))
            return hour * 3600 + minute * 60 + second + milli / 1000

        start, end = lines[1].split(" --> ", 1)
        cues.append((seconds(start), seconds(end), " ".join(lines[2:])))
    return cues


def frame_for_shot(shot: dict, now: float, size, sources):
    kind = shot["kind"]
    if kind == "semantic_diagram":
        frame = render_semantic_frame(
            shot["_semantic_scene"],
            now - shot["start"],
            size=size,
            presentation="production",
        )
    elif kind == "camera":
        frame = _photo(sources[shot["asset_ref"]], shot, now, size)
    elif kind == "fact_reveal":
        frame = _fact(shot, now, size)
    elif kind == "concept_contrast":
        frame = _concept(shot, now, size)
    elif kind == "evidence_gap":
        frame = _evidence_gap(shot, now, size)
    elif kind == "relationship_map":
        frame = _relationship(shot, now, size)
    elif kind == "branded_outro":
        frame = _outro(shot, now, size)
    elif kind == "process_steps":
        frame = _process(shot, now, size)
    elif kind == "narration_sequence":
        frame = _sequence(shot, now, size)
    elif kind == "title_reveal":
        frame = _title(shot, now, size)
    elif kind == "route_reveal":
        frame = _route(shot, now, size)
    elif kind == "comparison_build":
        frame = _comparison(shot, now, size)
    elif kind == "document_highlight":
        frame = _document(sources[shot["asset_ref"]], shot, now, size)
    else:
        raise ValueError(f"Unsupported production motion kind: {kind}")
    return frame


def render_motion_silent(
    repo,
    project,
    plan: MotionPlan,
    output: Path,
    *,
    ffmpeg: FFmpeg | None = None,
    captions_path: Path | None = None,
):
    """Render validated automatic motion directly to the chapter's silent stream."""
    ffmpeg = ffmpeg or FFmpeg()
    if plan.comparison_assets:
        raise ValueError("Comparison-only assets cannot enter production")
    if any(shot.kind == "guided_image" for shot in plan.shots):
        raise ValueError("The guided-doodle experiment is not a production shot type")
    size = (plan.width, plan.height)
    shots = [shot.model_dump(mode="json") for shot in plan.shots]
    for shot in shots:
        if shot["kind"] == "semantic_diagram":
            shot["_semantic_scene"] = TypeAdapter(Scene).validate_python(shot["scene"])
    sources = {}
    for shot in shots:
        asset_ref = shot.get("asset_ref")
        if asset_ref and asset_ref not in sources:
            asset_path = repo.verify(project, project.longform_media[asset_ref])
            sources[asset_ref] = Image.open(asset_path).convert("RGB")
    captions = _captions(captions_path) if captions_path else []
    command = ffmpeg.command(
        [
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgb24",
            "-video_size",
            f"{size[0]}x{size[1]}",
            "-framerate",
            str(plan.fps),
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
            "21",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            output,
        ]
    )
    log_path = output.parent / "motion-encode.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log
        )
        try:
            shot_index = 0
            caption_index = 0
            for frame_index in range(round(shots[-1]["end"] * plan.fps)):
                now = frame_index / plan.fps
                while shot_index + 1 < len(shots) and now >= shots[shot_index]["end"]:
                    shot_index += 1
                frame = frame_for_shot(shots[shot_index], now, size, sources)
                while caption_index + 1 < len(captions) and now >= captions[caption_index][1]:
                    caption_index += 1
                if captions and captions[caption_index][0] <= now < captions[caption_index][1]:
                    _caption(frame, captions[caption_index][2])
                process.stdin.write(frame.tobytes())
            process.stdin.close()
            if process.wait(timeout=1800):
                raise RuntimeError(f"Motion encode failed; see {log_path}")
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
    for source in sources.values():
        source.close()
