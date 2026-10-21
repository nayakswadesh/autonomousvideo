from __future__ import annotations

from functools import lru_cache
from math import exp, sin

from PIL import Image, ImageDraw, ImageFont

from autonomous_studio.domain.models import Shot, VideoProject


@lru_cache(maxsize=128)
def font(size):
    # Pillow's bundled font avoids machine-specific font discovery.
    return ImageFont.load_default(size=size)


def block(draw, text, x, y, width, size, color, max_lines=4):
    """Fit actual measured glyphs; reject text that cannot fit at readable sizes."""
    for current in range(size, max(4, int(size * 0.6)) - 1, -1):
        face = font(current)
        lines = []
        for paragraph in text.splitlines() or [""]:
            line = ""
            for word in paragraph.split():
                candidate = f"{line} {word}".strip()
                if draw.textlength(candidate, font=face) > width and line:
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            lines.append(line)
        if len(lines) <= max_lines and all(draw.textlength(s, font=face) <= width for s in lines):
            for line in lines:
                draw.text((x, y), line, font=face, fill=color)
                y += current * 1.24
            return y
    raise ValueError(f"text does not fit readable safe area: {text[:70]}")


def motion_transform(image, motion, progress):
    progress = max(0, min(1, progress))
    entry = min(1, progress * 5)
    ease = 1 - (1 - entry) ** 3
    width, height = image.size
    if motion == "hold":
        return image
    if motion in ("fade", "reveal", "wipe", "draw", "stagger"):
        if motion == "fade":
            alpha = image.getchannel("A").point(lambda a: round(a * ease))
            image.putalpha(alpha)
        else:
            mask = Image.new("L", image.size)
            md = ImageDraw.Draw(mask)
            if motion in ("reveal", "stagger"):
                md.rectangle((0, 0, width, round(height * ease)), fill=255)
            else:
                md.rectangle((0, 0, round(width * ease), height), fill=255)
            from PIL import ImageChops

            image.putalpha(ImageChops.multiply(image.getchannel("A"), mask))
        return image
    if motion in ("push", "spring", "impact", "parallax"):
        if motion == "spring":
            shift = round(width * 0.08 * exp(-8 * entry) * sin(12 * entry))
        elif motion == "impact":
            shift = round(height * 0.018 * exp(-10 * entry) * sin(20 * entry))
        elif motion == "parallax":
            shift = round(width * 0.02 * (1 - progress))
        else:
            shift = round(width * 0.08 * (1 - ease))
        layer = Image.new("RGBA", image.size)
        layer.alpha_composite(image, (shift, 0))
        return layer
    scale = {
        "zoom": 0.96 + 0.04 * ease,
        "magnify": 0.94 + 0.06 * ease,
        "ken_burns": 0.97 + 0.03 * progress,
        "compress": 1.04 - 0.04 * ease,
    }[motion]
    resized = image.resize((round(width * scale), round(height * scale)), Image.Resampling.BICUBIC)
    layer = Image.new("RGBA", image.size)
    layer.alpha_composite(resized, ((width - resized.width) // 2, (height - resized.height) // 2))
    return layer


def scene(project: VideoProject, shot: Shot, progress=0.65, image_path=None):
    brief = project.spec.brief
    width, height = brief.width, brief.height
    scale = width / 540
    style = brief.visual_style
    fg, accent = style.foreground, style.accent
    base = Image.new("RGB", (width, height), style.background)
    draw = ImageDraw.Draw(base)
    margin = round(width * 0.085)
    usable = width - 2 * margin
    # Quiet editorial grid with consistent typography and generous mobile-safe margins.
    for x in range(margin, width - margin, max(1, round(56 * scale))):
        draw.line((x, height * 0.19, x, height * 0.80), fill="#202b37")
    draw.line(
        (margin, height * 0.13, width - margin, height * 0.13),
        fill=accent,
        width=max(1, round(scale * 2)),
    )
    draw.text(
        (margin, height * 0.085), "AUTONOMOUS / STUDIO", font=font(round(15 * scale)), fill=accent
    )
    layer = Image.new("RGBA", base.size)
    ink = ImageDraw.Draw(layer)
    size = round(50 * scale)
    y = height * 0.22
    if shot.kind == "kinetic_title":
        y = block(ink, shot.title.upper(), margin, y, usable, round(57 * scale), fg, 5)
        block(
            ink,
            shot.subtitle,
            margin,
            max(y + height * 0.045, height * 0.57),
            usable,
            round(18 * scale),
            accent,
            4,
        )
    elif shot.kind == "rank_reveal":
        ink.text(
            (margin, height * 0.17),
            f"0{shot.rank}" if shot.rank < 10 else str(shot.rank),
            font=font(round(155 * scale)),
            fill=accent,
        )
        y = block(ink, shot.title.upper(), margin, height * 0.40, usable, size, fg, 3)
        block(ink, shot.subtitle, margin, y + height * 0.025, usable, round(23 * scale), fg, 3)
    elif shot.kind == "big_metric":
        block(ink, shot.metric, margin, height * 0.27, usable, round(88 * scale), accent, 2)
        y = block(ink, shot.title, margin, height * 0.46, usable, round(30 * scale), fg, 3)
        block(ink, shot.subtitle, margin, y + height * 0.035, usable, round(19 * scale), fg, 3)
    elif shot.kind == "metric_pair":
        y = block(ink, shot.title, margin, height * 0.19, usable, round(36 * scale), fg, 3)
        for i, metric in enumerate(shot.metrics):
            block(
                ink,
                metric,
                margin,
                max(y + height * 0.05, height * (0.36 + i * 0.17)),
                usable,
                round(34 * scale),
                accent,
                3,
            )
        block(ink, shot.subtitle, margin, height * 0.72, usable, round(18 * scale), fg, 3)
    elif shot.kind == "metric_set":
        block(ink, shot.title, margin, height * 0.19, usable, round(36 * scale), fg, 3)
        step = height * 0.30 / len(shot.labels)
        for i, (label, value) in enumerate(zip(shot.labels, shot.display_values, strict=True)):
            top = height * 0.38 + i * step
            ink.rounded_rectangle(
                (margin, top, width - margin, top + step - 7 * scale),
                radius=8 * scale,
                outline=accent,
                width=max(1, round(scale)),
            )
            block(
                ink,
                label,
                margin + 14 * scale,
                top + 5 * scale,
                usable - 28 * scale,
                round(16 * scale),
                accent,
                2,
            )
            block(
                ink,
                value,
                margin + 14 * scale,
                top + 29 * scale,
                usable - 28 * scale,
                round(22 * scale),
                fg,
                2,
            )
        block(ink, shot.subtitle, margin, height * 0.74, usable, round(18 * scale), fg, 3)
    elif shot.kind == "source_card":
        block(ink, shot.title, margin, y, usable, round(38 * scale), fg, 3)
        y = height * 0.39
        source_ids = shot.source_ids[:5]
        source_size = round((19 if len(source_ids) > 3 else 23) * scale)
        for source_id in source_ids:
            source = next(s for s in brief.research.sources if s.id == source_id)
            y = block(ink, source.title, margin, y, usable, source_size, accent, 2) + height * (
                0.012 if len(source_ids) > 3 else 0.025
            )
        block(
            ink,
            shot.subtitle,
            margin,
            max(y + height * 0.02, height * 0.68),
            usable,
            round(18 * scale),
            fg,
            3,
        )
    else:
        block(ink, shot.title, margin, height * 0.19, usable, round(36 * scale), fg, 3)
        top, bottom = height * 0.39, height * 0.65
        if shot.kind == "map":
            ink.rectangle((margin, top, width - margin, bottom), outline=accent)
            for n in range(1, 6):
                x = margin + usable * n / 6
                ink.line((x, top, x, bottom), fill="#586270")
            x = margin + (shot.longitude + 180) / 360 * usable
            y = top + (90 - shot.latitude) / 180 * (bottom - top)
            ink.ellipse((x - 6 * scale, y - 6 * scale, x + 6 * scale, y + 6 * scale), fill=accent)
            ink.text(
                (margin, bottom + 10 * scale),
                "SCHEMATIC COORDINATE GRID",
                font=font(round(14 * scale)),
                fill=fg,
            )
        elif shot.kind in ("comparison", "data_chart"):
            values = shot.values
            maximum = max(max(map(abs, values)), 1)
            step = usable / len(values)
            for i, value in enumerate(values):
                x = margin + i * step
                bar_height = abs(value) / maximum * (bottom - top)
                ink.rectangle((x, bottom - bar_height, x + step * 0.65, bottom), fill=accent)
                if shot.kind == "comparison":
                    block(
                        ink,
                        shot.labels[i],
                        x,
                        bottom + 8 * scale,
                        step * 0.85,
                        round(15 * scale),
                        fg,
                        3,
                    )
        elif shot.kind == "timeline":
            ink.line((margin, top, width - margin, top), fill=accent, width=2)
            for i, label in enumerate(shot.labels):
                x = margin + i * usable / len(shot.labels)
                ink.ellipse((x, top - 5, x + 10, top + 5), fill=accent)
                block(
                    ink,
                    label,
                    x,
                    top + height * 0.04,
                    usable / len(shot.labels) * 0.9,
                    round(19 * scale),
                    fg,
                    3,
                )
        elif shot.kind in {"qualitative_comparison", "study_design"}:
            items = shot.alternatives if shot.kind == "qualitative_comparison" else shot.steps
            for i, item in enumerate(items):
                row = top + i * (bottom - top) / len(items)
                ink.rounded_rectangle(
                    (margin, row, width - margin, row + (bottom - top) / len(items) - 8 * scale),
                    radius=8 * scale,
                    outline=accent,
                    width=max(1, round(scale)),
                )
                block(
                    ink,
                    item,
                    margin + 16 * scale,
                    row + 13 * scale,
                    usable - 32 * scale,
                    round(20 * scale),
                    fg,
                    3,
                )
        elif shot.kind == "hero_image" and image_path:
            from PIL import ImageOps

            with Image.open(image_path) as original:
                picture = ImageOps.fit(original.convert("RGB"), (usable, round(bottom - top)))
                layer.paste(picture, (margin, round(top)))
        else:
            # Original deterministic abstract hero graphic, also used by mock image provider.
            for i in range(5):
                radius = round((30 + i * 19) * scale)
                cx, cy = width / 2, height * 0.50
                ink.ellipse(
                    (cx - radius, cy - radius, cx + radius, cy + radius), outline=accent, width=2
                )
        block(ink, shot.subtitle, margin, height * 0.71, usable, round(18 * scale), fg, 3)
    layer = motion_transform(layer, shot.motion, progress)
    base = Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")
    draw = ImageDraw.Draw(base)
    fallback_label = (
        "FULL PRODUCTION" if project.spec.brief.production_mode == "full" else "PROTOTYPE EXCERPT"
    )
    source_text = " / ".join(shot.source_ids) if shot.source_ids else fallback_label
    block(draw, source_text.upper(), margin, height * 0.81, usable, round(13 * scale), accent, 2)
    draw.line((margin, height * 0.855, width - margin, height * 0.855), fill="#586270")
    block(
        draw,
        (
            "AI VOICE / PLACEHOLDER VISUALS"
            if project.pipeline == "live"
            else "MOCK PREVIEW / VOICE = TIMING TONES"
        ),
        margin,
        height * 0.88,
        usable,
        round(12 * scale),
        fg,
        2,
    )
    return base
