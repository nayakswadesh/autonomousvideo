"""Data-driven phone layouts. Text boxes fail closed instead of overlapping or truncating."""

import json
import re
import textwrap
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps

from autonomous_studio.rendering.contrast import BACKING_COLORS, local_backing
from autonomous_studio.rendering.map_motion import camera as map_camera
from autonomous_studio.rendering.map_motion import geography_for
from autonomous_studio.rendering.map_motion import project as geo_project
from autonomous_studio.rendering.object_motion import (
    cue_onsets,
    object_count,
    timeline_motion_supported,
)
from autonomous_studio.rendering.object_motion import reveal as object_reveal

ASSETS = Path(__file__).resolve().parents[1] / "assets"
CAPTION_CHARS = 75


@lru_cache(maxsize=128)
def face(size, display=False):
    name = "BarlowCondensed-Bold.ttf" if display else "Barlow-Regular.ttf"
    return ImageFont.truetype(str(ASSETS / "fonts" / name), size)


def text_box(draw, text, rect, size, color, *, display=False, minimum=None, records=None,
             center_single_line=False):
    if not text:
        return
    x, y, right, bottom = rect
    minimum = minimum or max(16, round(size * 0.72))
    for current in range(size, minimum - 1, -1):
        font = face(current, display)
        lines = []
        for paragraph in text.splitlines():
            line = ""
            for word in paragraph.split():
                candidate = (line + " " + word).strip()
                if draw.textlength(candidate, font=font) > right - x and line:
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            lines.append(line)
        step = round(current * 1.14)
        if len(lines) * step > bottom - y:
            continue
        if any(draw.textlength(line, font=font) > right - x for line in lines):
            continue
        text_y = y
        if center_single_line and len(lines) == 1:
            bounds = draw.textbbox((0, 0), lines[0], font=font, anchor="lt")
            text_y = y + (bottom - y - (bounds[3] - bounds[1])) / 2
        if any(
            draw.textbbox((x, text_y + i * step), line, font=font, anchor="lt")[3] > bottom
            for i, line in enumerate(lines)
        ):
            continue
        for i, line in enumerate(lines):
            draw.text((x, text_y + i * step), line, font=font, fill=color, anchor="lt")
        if records is not None:
            records.append({"text": text, "box": rect, "font_size": current,
                            "lines": len(lines), "text_y": text_y})
        return
    raise ValueError(f"editorial text exceeds readable safe area: {text[:80]}")


@lru_cache(maxsize=512)
def _caption_chunks(value, width):
    compact = " ".join(value.split())
    if not compact:
        return ()
    clauses = re.split(r"(?<=[.!?;:])\s+", compact)
    cards = []
    for clause in clauses:
        candidate = f"{cards[-1]} {clause}" if cards else clause
        if cards and len(candidate) <= width:
            cards[-1] = candidate
            continue
        cards.extend(
            textwrap.wrap(
                clause,
                width=width,
                break_long_words=True,
                break_on_hyphens=False,
            )
        )
    return tuple(cards)


def caption_chunks(value, width=CAPTION_CHARS):
    """Split narration into stable, clause-aware cards without changing its words."""
    return list(_caption_chunks(value, width))


def _caption_weights(chunks):
    return [max(1, len(chunk)) for chunk in chunks]


def caption_at(value, elapsed, duration):
    """Select a caption card proportionally within one measured narration segment."""
    chunks = caption_chunks(value)
    if len(chunks) < 2 or duration <= 0:
        return chunks[0] if chunks else ""
    weights = _caption_weights(chunks)
    position = max(0, min(duration, elapsed)) / duration * sum(weights)
    boundary = 0
    for chunk, weight in zip(chunks, weights, strict=True):
        boundary += weight
        if position < boundary:
            return chunk
    return chunks[-1]


def caption_sample_progresses(project, shot):
    """Return one render sample inside every caption card for fail-closed layout QA."""
    points = [0.5]
    if not project.spec.closed_captions:
        return points
    for index, voice in enumerate(sorted(shot.voice_segments, key=lambda value: value.offset)):
        prepared = next(
            (
                value
                for value in project.prepared_voices
                if (value.shot_id, value.segment_index) == (shot.id, index)
            ),
            None,
        )
        duration = prepared.asset.duration if prepared else voice.estimated_duration
        chunks = caption_chunks(voice.spoken_text)
        weights = _caption_weights(chunks)
        total = sum(weights)
        elapsed = 0
        for weight in weights:
            local = voice.offset + duration * (elapsed + weight / 2) / total
            points.append(min(shot.duration - 1e-6, local) / shot.duration)
            elapsed += weight
    return list(dict.fromkeys(points))


@lru_cache(maxsize=1)
def _land_features():
    return json.loads((ASSETS / "maps" / "ne_110m_land.geojson").read_text(encoding="utf-8"))[
        "features"
    ]


@lru_cache(maxsize=128)
def world_map(size=(456, 240), viewport=None):
    """Equirectangular Natural Earth land polygons; no invented boundaries or geography."""
    im = Image.new("RGBA", size)
    draw = ImageDraw.Draw(im)
    w, h = size
    for feature in _land_features():
        geom = feature["geometry"]
        polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        for polygon in polys:
            for i, ring in enumerate(polygon):
                for shift in (-360, 0, 360) if viewport else (0,):
                    points = [
                        (
                            (0.5 + (lon + shift - viewport.longitude) / viewport.width) * (w - 1),
                            (0.5 - (lat - viewport.latitude) / viewport.height) * (h - 1),
                        )
                        if viewport
                        else ((lon + 180) / 360 * (w - 1), (90 - lat) / 180 * (h - 1))
                        for lon, lat, *_ in ring
                    ]
                    draw.polygon(points, fill=(78, 83, 82, 255) if i == 0 else (0, 0, 0, 0))
    return im


def map_point(latitude, longitude, rect):
    x, y, right, bottom = rect
    return x + (longitude + 180) / 360 * (right - x - 1), y + (90 - latitude) / 180 * (
        bottom - y - 1
    )


def _number(value):
    if float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.3f}".rstrip("0").rstrip(".")


def quantitative_display_values(brief, shot):
    """Use authored labels, or recover readable units from referenced numeric claims."""
    if shot.display_values:
        return shot.display_values
    claims = {
        claim.id: claim
        for claim in brief.research.claims
        if claim.kind == "numeric" and claim.id in shot.claim_ids
    }
    labels = []
    for value in shot.values:
        claim = next(
            (
                item
                for item in claims.values()
                if abs(item.value - value) <= max(1e-9, abs(value) * 1e-9)
            ),
            None,
        )
        if not claim:
            labels.append(_number(value))
            continue
        approximate = any(
            word in (claim.text + " " + claim.caveat).casefold()
            for word in (
                "approximately",
                "approximate",
                "about",
                "around",
                "roughly",
                "estimated",
            )
        )
        labels.append(f"{'≈' if approximate else ''}{_number(value)} {claim.unit}".strip())
    return labels


def metric_card_value(label, value):
    """A nearby label already supplies a repeated trailing category."""
    if value.casefold().endswith(label.casefold()):
        return value[: -len(label)].strip()
    return value


@lru_cache(maxsize=8)
def artwork(path):
    with Image.open(path) as im:
        return ImageOps.exif_transpose(im).convert("RGB")


@lru_cache(maxsize=4)
def shade(size=(540, 960)):
    width, height = size
    im = Image.new("RGBA", size)
    draw = ImageDraw.Draw(im)
    for y in range(height):
        normalized = y / height
        alpha = round(
            190 * max(0, 1 - normalized / 0.594) + 220 * max(0, (normalized - 0.521) / 0.479)
        )
        draw.line((0, y, width - 1, y), fill=(14, 17, 19, min(240, alpha)))
    return im


def _ease(value):
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def _camera_artwork(image, canvas, motion, progress):
    """Restrained, repeatable camera grammar; never modify source exposure."""
    if motion == "hold":
        start_zoom, end_zoom, start_focus, end_focus = 1.0, 1.0, (0.5, 0.5), (0.5, 0.5)
    elif motion == "parallax":
        start_zoom, end_zoom = 1.025, 1.105
        start_focus, end_focus = (0.2, 0.45), (0.8, 0.55)
    elif motion == "draw":
        start_zoom, end_zoom = 1.01, 1.075
        start_focus, end_focus = (0.5, 0.25), (0.5, 0.7)
    elif motion == "compress":
        start_zoom, end_zoom = 1.01, 1.09
        start_focus, end_focus = (0.35, 0.48), (0.65, 0.52)
    else:
        start_zoom, end_zoom = 1.01, 1.07
        start_focus, end_focus = (0.4, 0.48), (0.6, 0.52)
    position = _ease(progress)
    zoom = start_zoom + (end_zoom - start_zoom) * position
    full = ImageOps.fit(image, (round(canvas[0] * zoom), round(canvas[1] * zoom)))
    extra_x, extra_y = full.width - canvas[0], full.height - canvas[1]
    fx = start_focus[0] + (end_focus[0] - start_focus[0]) * position
    fy = start_focus[1] + (end_focus[1] - start_focus[1]) * position
    left, top = round(extra_x * fx), round(extra_y * fy)
    return full.crop((left, top, left + canvas[0], top + canvas[1]))


def _text_line_bounds(glyphs, size):
    """Keep contrast treatment close to each line, never one large multiline card."""
    mask = glyphs.getchannel("A")
    occupied = [y for y, present in enumerate(mask.getprojection()[1]) if present]
    if not occupied:
        return []
    bands = []
    first = previous = occupied[0]
    for y in occupied[1:]:
        if y - previous > max(6, round(size * 0.12)):
            bands.append((first, previous + 1))
            first = y
        previous = y
    bands.append((first, previous + 1))
    bounds = []
    for top, bottom in bands:
        local = mask.crop((0, top, mask.width, bottom)).getbbox()
        bounds.append(
            (
                max(0, local[0] - 7),
                max(0, top + local[1] - 5),
                min(mask.width, local[2] + 7),
                min(mask.height, top + local[3] + 5),
            )
        )
    return bounds


def scene(project, shot, progress=0.65, image_path=None, *, records=None):
    brief, presentation = project.spec.brief, project.spec.presentation
    look = next((s for s in presentation.shots if s.shot_id == shot.id), None)
    fg, accent = brief.visual_style.foreground, brief.visual_style.accent
    canvas = (brief.width, brief.height)
    background = Image.new("RGB", canvas, brief.visual_style.background)
    if image_path:
        background = _camera_artwork(artwork(str(image_path)), canvas, shot.motion, progress)
        background = Image.alpha_composite(background.convert("RGBA"), shade(canvas)).convert("RGB")
    # Editorial geometry remains in a stable 540x960 design space. Scaling the transparent
    # information layer preserves layout validation while retaining full source-image detail.
    layer = Image.new("RGBA", (540, 960))
    draw = ImageDraw.Draw(layer)
    background_small = background.resize((540, 960), Image.Resampling.BILINEAR)

    def text(value, rect, size=28, color=fg, display=False, minimum=None, opacity=1.0,
             center_single_line=False):
        if not value:
            return
        glyphs = Image.new("RGBA", (540, 960))
        before = len(records) if records is not None else 0
        text_box(
            ImageDraw.Draw(glyphs),
            value,
            rect,
            size,
            color,
            display=display,
            minimum=minimum,
            records=records,
            center_single_line=center_single_line,
        )
        bounds_by_line = _text_line_bounds(glyphs, size)
        if not bounds_by_line:
            return
        treatments = []
        for bounds in bounds_by_line:
            under = Image.alpha_composite(
                background_small.crop(bounds).convert("RGBA"), layer.crop(bounds)
            ).convert("RGB")
            alpha, ratio, backing = local_backing(under, (0, 0, under.width, under.height), color)
            treatments.append((bounds, alpha, ratio, backing))
        if records is not None and len(records) > before:
            records[-1].update(
                {
                    "contrast_estimate": round(min(item[2] for item in treatments), 2),
                    "backing_alpha": 0 if display else max(item[1] for item in treatments),
                    "outline_alpha": max(item[1] for item in treatments) if display else 0,
                }
            )
        if opacity <= 0:
            return
        if display and any(alpha for _, alpha, _, _ in treatments):
            # A narrow glyph halo preserves a bright picture far better than a
            # full-width display-title card. It is only used where contrast fails.
            alpha = min(255, max(item[1] for item in treatments) + 80)
            halo = Image.new("RGBA", (540, 960), BACKING_COLORS[0] + (0,))
            expanded = glyphs.getchannel("A").filter(ImageFilter.MaxFilter(7))
            halo.putalpha(expanded.point(lambda value: round(value * alpha / 255)))
            halo.alpha_composite(glyphs)
            glyphs = halo
        elif any(alpha for _, alpha, _, _ in treatments):
            panel = Image.new("RGBA", (540, 960))
            painter = ImageDraw.Draw(panel)
            for bounds, alpha, _, backing in treatments:
                if alpha:
                    painter.rounded_rectangle(bounds, radius=7, fill=(*backing, alpha))
            panel.alpha_composite(glyphs)
            glyphs = panel
        if opacity < 1:
            glyphs.putalpha(
                glyphs.getchannel("A").point(lambda value: round(value * min(1, opacity)))
            )
        layer.alpha_composite(glyphs)

    local_time = progress * shot.duration
    onsets = cue_onsets(project, shot) if shot.entrance_cues else []
    if onsets and len(onsets) != object_count(shot):
        raise ValueError(f"{shot.id}: entrance cue count must match visible objects")

    semantic_motion = (
        "hold"
        if shot.kind == "timeline" and not timeline_motion_supported(shot.labels)
        else shot.motion
    )

    def object_fraction(index, count):
        return object_reveal(
            local_time,
            shot.duration,
            index,
            count,
            motion=semantic_motion,
            onset=onsets[index] if onsets else None,
        )

    def entered_rect(rect, fraction):
        shift = round((1 - fraction) * 18)
        return (rect[0], rect[1] + shift, rect[2], rect[3] + shift)

    def outlined_rect(rect, fraction, *, radius=12):
        if fraction > 0:
            draw.rounded_rectangle(
                entered_rect(rect, fraction),
                radius=radius,
                outline=(*ImageColor.getrgb(accent), round(255 * fraction)),
                width=2,
            )

    if project.spec.shots.index(shot) == 0 and presentation.series_label:
        text(presentation.series_label.upper(), (42, 65, 498, 91), 18, accent)
        draw.line((42, 112, 498, 112), fill=accent, width=1)
    eyebrow = look.eyebrow if look else ""
    title = look.headline if look and look.headline is not None else shot.title
    subtitle = look.annotation if look and look.annotation is not None else shot.subtitle

    # Avoid repeating the same message in stacked text boxes. Captions still carry every spoken
    # word, while display copy is reserved for information the picture cannot convey alone.
    def normalize(value):
        return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())

    if normalize(subtitle) in {normalize(title), normalize(eyebrow)}:
        subtitle = ""
    text(eyebrow, (42, 144, 498, 191), 19, accent)
    if shot.kind in {"kinetic_title", "hero_image", "outro"}:
        # Prefer the spacious standard title, then use the otherwise quiet middle of the
        # portrait canvas for a long, exact opening promise. This preserves hook alignment
        # instead of making the planner paraphrase it merely to satisfy a rigid text box.
        try:
            text(title.upper(), (42, 210, 498, 477), 88, display=True, minimum=60)
            subtitle_box = (44, 499, 488, 589)
        except ValueError:
            text(title.upper(), (42, 210, 498, 560), 82, display=True, minimum=50)
            subtitle_box = (44, 584, 488, 660)
        text(subtitle, subtitle_box, 27)
    elif shot.kind == "big_metric":
        text(title.upper(), (42, 205, 498, 326), 58, display=True, minimum=42)
        visible = object_fraction(0, 1)
        text(
            shot.metric.upper(),
            entered_rect((42, 348, 498, 575), visible),
            104,
            display=True,
            minimum=64,
            opacity=visible,
        )
        if not image_path:
            # A neutral scale motif is safer than implying geography for an unrelated metric.
            for radius, alpha in ((94, 45), (62, 70), (30, 110)):
                animated_radius = round(radius * (0.72 + 0.28 * visible))
                draw.ellipse(
                    (
                        270 - animated_radius,
                        626 - animated_radius,
                        270 + animated_radius,
                        626 + animated_radius,
                    ),
                    outline=(*ImageColor.getrgb(accent), round(alpha * visible)),
                    width=3,
                )
        text(subtitle, (42, 700, 498, 762), 29)
    elif shot.kind == "metric_pair":
        text(title.upper(), (42, 205, 498, 338), 58, display=True, minimum=42)
        for i, metric in enumerate(shot.metrics):
            y = 378 + i * 145
            visible = object_fraction(i, len(shot.metrics))
            outlined_rect((42, y, 498, y + 122), visible)
            text(
                metric,
                entered_rect((60, y + 18, 480, y + 105), visible),
                44,
                display=True,
                minimum=29,
                opacity=visible,
                center_single_line=True,
            )
        text(subtitle, (42, 708, 498, 768), 24)
    elif shot.kind == "metric_set":
        text(title.upper(), (42, 205, 498, 365), 58, display=True, minimum=42)
        step = 306 / len(shot.labels)
        for i, (label, value) in enumerate(zip(shot.labels, shot.display_values, strict=True)):
            top = round(379 + i * step)
            bottom = round(379 + (i + 1) * step - 7)
            visible = object_fraction(i, len(shot.labels))
            outlined_rect((42, top, 498, bottom), visible)
            text(
                label.upper(),
                entered_rect((60, top + 7, 480, top + 32), visible),
                20,
                accent,
                minimum=17,
                opacity=visible,
            )
            text(
                metric_card_value(label, value),
                entered_rect((60, top + 32, 480, bottom - 5), visible),
                27 if len(shot.labels) < 4 else 22,
                display=True,
                minimum=18,
                opacity=visible,
            )
        text(subtitle, (42, 708, 498, 768), 24)
    elif shot.kind == "rank_reveal":
        text(f"#{shot.rank:02}", (42, 202, 498, 354), 132, accent, True)
        # Reserve two complete 55px lines (126px including line spacing), with
        # a 17px gap before the metric. A 124px box rejected every two-line title.
        text(title.upper(), (42, 370, 498, 500), 82, display=True, minimum=55)
        text(shot.metric, (42, 517, 498, 637), 43, display=True, minimum=30)
        text(subtitle, (42, 660, 498, 724), 25)
    elif shot.kind == "map":
        text(title.upper(), (42, 205, 498, 385), 70, display=True)
        geography = geography_for(shot)
        viewport = map_camera(geography, progress, motion=shot.motion)
        layer.alpha_composite(world_map(viewport=viewport), (42, 420))
        visible = object_fraction(0, 1)
        if geography.kind == "point" and visible > 0:
            px, py = geo_project(
                geography.latitude, geography.longitude, (42, 420, 498, 660), viewport
            )
            radius = 10 + round(22 * (1 - visible))
            color = (*ImageColor.getrgb(accent), round(255 * visible))
            draw.ellipse(
                (px - radius, py - radius, px + radius, py + radius), outline=color, width=2
            )
            draw.ellipse((px - 3, py - 3, px + 3, py + 3), fill=color)
        elif geography.kind == "route" and visible > 0:
            points = [
                geo_project(point.latitude, point.longitude, (42, 420, 498, 660), viewport)
                for point in geography.points
            ]
            length = (len(points) - 1) * visible
            end = points[-1]
            for i in range(int(length)):
                draw.line((points[i], points[i + 1]), fill=accent, width=3)
            if length < len(points) - 1:
                i = int(length)
                end = (
                    points[i][0] + (points[i + 1][0] - points[i][0]) * (length - i),
                    points[i][1] + (points[i + 1][1] - points[i][1]) * (length - i),
                )
                draw.line((points[i], end), fill=accent, width=3)
            for x, y, radius in ((*points[0], 3), (*end, 5)):
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=accent)
        text(geography.label, (42, 646, 498, 679), 22, accent, opacity=visible)
        text(subtitle, (42, 688, 498, 756), 26)
    elif shot.kind == "source_card":
        text(title.upper(), (42, 205, 498, 363), 61, display=True, minimum=45)
        text(subtitle, (42, 385, 498, 447), 27)
        sources = [s for s in brief.research.sources if s.id in shot.source_ids]
        if len(sources) > 5:
            raise ValueError("editorial source card supports at most five readable citations")
        source_step = 51 if len(sources) > 3 else 78
        for i, source in enumerate(sources):
            y = 468 + i * source_step
            draw.line((42, y, 498, y), fill="#505656", width=1)
            label = presentation.source_labels.get(source.id, urlparse(source.url).netloc)
            text(f"{i + 1:02} / {label}", (42, y + 8, 498, y + source_step - 3), 21)
        text(presentation.music_credit, (42, 736, 498, 810), 17, "#bfc3bf", minimum=16)
    elif shot.kind in {"comparison", "data_chart"}:
        text(title.upper(), (42, 210, 498, 390), 67, display=True)
        values = shot.values
        display_values = quantitative_display_values(brief, shot)
        maximum = max(values) or 1
        # Comparison labels are editorial data, not disposable decoration. Give both the
        # subject and its displayed value enough width for two readable lines instead of
        # forcing the subject into the old 148px single-line box. The row calculation is
        # data-driven for every supported two-to-four-item comparison.
        if shot.kind == "comparison":
            step = 252 / len(values)
            rows = []
            for i, value in enumerate(values):
                y = 420 + i * step
                copy_bottom = y + min(42, step - 19)
                rows.append(
                    (
                        value,
                        shot.labels[i],
                        display_values[i],
                        y,
                        copy_bottom,
                        copy_bottom + 7,
                        y + step - 5,
                    )
                )
        elif len(values) == 2:
            # Two-item charts can devote the entire row width to the category. This keeps
            # qualified scientific labels readable without asking the asset planner to
            # paraphrase immutable, approved shot data.
            rows = []
            for i, value in enumerate(values):
                y = 415 + i * 132
                rows.append((value, shot.labels[i], display_values[i], y, y + 46, y + 106, y + 121))
        else:
            step = min(95, 250 / len(values))
            rows = []
            for i, value in enumerate(values):
                y = 428 + i * step
                label = shot.labels[i] if shot.labels else str(i + 1)
                bar_top = y + min(36, step * 0.55)
                rows.append(
                    (
                        value,
                        label,
                        display_values[i],
                        y,
                        y + 28,
                        bar_top,
                        max(bar_top + 2, y + min(step - 4, 54)),
                    )
                )
        for i, row in enumerate(rows):
            value, label, display_value, y, copy_bottom, bar_top, bar_bottom = row
            visible = object_fraction(i, len(rows))
            if shot.kind == "comparison":
                text(
                    label,
                    entered_rect((42, y, 292, copy_bottom), visible),
                    20,
                    minimum=13,
                    opacity=visible,
                )
                text(
                    display_value,
                    entered_rect((304, y, 498, copy_bottom), visible),
                    19,
                    minimum=13,
                    opacity=visible,
                )
            elif shot.kind == "data_chart" and len(values) == 2:
                text(
                    label,
                    entered_rect((42, y, 498, copy_bottom), visible),
                    22,
                    minimum=17,
                    opacity=visible,
                )
                text(
                    display_value,
                    entered_rect((42, copy_bottom + 3, 498, bar_top - 4), visible),
                    18,
                    accent,
                    minimum=16,
                    opacity=visible,
                )
            else:
                text(
                    label,
                    entered_rect((42, y, 190, copy_bottom), visible),
                    21,
                    minimum=16,
                    opacity=visible,
                )
                text(
                    display_value,
                    entered_rect((198, y, 498, copy_bottom), visible),
                    21,
                    minimum=16,
                    opacity=visible,
                )
            if visible > 0:
                draw.rectangle(
                    (42, bar_top, 42 + max(3, 456 * value / maximum * visible), bar_bottom),
                    fill=accent,
                )
        if shot.kind == "data_chart" and len(values) == 2 and shot.takeaway:
            text(
                shot.takeaway + (" " + subtitle if subtitle else ""),
                (42, 682, 498, 776),
                20,
                minimum=17,
            )
        elif shot.takeaway:
            text(shot.takeaway, (42, 692, 498, 730), 24, accent)
            text(subtitle, (42, 736, 498, 772), 21)
        else:
            text(subtitle, (42, 712, 498, 768), 26)
    elif shot.kind == "timeline":
        text(title.upper(), (42, 210, 498, 390), 67, display=True)
        visibilities = [object_fraction(i, len(shot.labels)) for i in range(len(shot.labels))]
        line_fraction = max(0.0, (sum(visibilities) - 1) / max(1, len(shot.labels) - 1))
        if line_fraction > 0:
            draw.line((57, 430, 57, 430 + 220 * line_fraction), fill=accent, width=2)
        for i, label in enumerate(shot.labels):
            y = 430 + i * 220 / max(1, len(shot.labels) - 1)
            visible = visibilities[i]
            if visible > 0:
                radius = 6 + round(5 * (1 - visible))
                draw.ellipse(
                    (57 - radius, y - radius, 57 + radius, y + radius),
                    fill=(*ImageColor.getrgb(accent), round(255 * visible)),
                )
            text(label, entered_rect((84, y - 10, 498, y + 43), visible), 29, opacity=visible)
        text(subtitle, (42, 712, 498, 768), 26)
    elif shot.kind == "qualitative_comparison":
        text(title.upper(), (42, 210, 498, 362), 60, display=True)
        step = 248 / len(shot.alternatives)
        for i, alternative in enumerate(shot.alternatives):
            y = 405 + i * step
            visible = object_fraction(i, len(shot.alternatives))
            outlined_rect((42, y, 498, y + step - 13), visible)
            text(
                alternative,
                # Use the card's available height for wrapped explanatory copy.
                # The former 17/26px inset left only 40px in a three-card shot,
                # so two readable lines could never fit even when the card could.
                entered_rect((60, y + 8, 480, y + step - 16), visible),
                31,
                minimum=20,
                opacity=visible,
                center_single_line=True,
            )
        text(shot.takeaway or subtitle, (42, 700, 498, 775), 25)
    elif shot.kind == "study_design":
        text(title.upper(), (42, 210, 498, 365), 60, display=True)
        step = 260 / len(shot.steps)
        for i, item in enumerate(shot.steps):
            y = 409 + i * step
            visible = object_fraction(i, len(shot.steps))
            if visible > 0:
                draw.ellipse(
                    (42, y + 9, 55, y + 22), fill=(*ImageColor.getrgb(accent), round(255 * visible))
                )
            text(
                item,
                entered_rect((74, y, 498, y + step - 8), visible),
                25,
                minimum=17,
                opacity=visible,
            )
        text(subtitle, (42, 712, 498, 768), 24)
    else:
        raise ValueError(f"unsupported editorial primitive: {shot.kind}")
    # Captions are complete narration segments, timed to measured recordings (not fake word sync).
    if project.spec.closed_captions and shot.kind != "source_card":
        local = progress * shot.duration
        for index, voice in enumerate(sorted(shot.voice_segments, key=lambda v: v.offset)):
            prepared = next(
                (
                    v
                    for v in project.prepared_voices
                    if (v.shot_id, v.segment_index) == (shot.id, index)
                ),
                None,
            )
            duration = prepared.asset.duration if prepared else voice.estimated_duration
            if voice.offset <= local < voice.offset + duration:
                draw.rounded_rectangle((32, 782, 508, 850), radius=10, fill=(14, 17, 19, 238))
                caption = caption_at(voice.spoken_text, local - voice.offset, duration)
                text(caption, (46, 793, 494, 840), 23, minimum=19)
    draw.line((42, 924, 498, 924), fill="#505656", width=2)
    draw.line(
        (42, 924, 42 + 456 * (shot.start + progress * shot.duration) / brief.duration, 924),
        fill=accent,
        width=2,
    )
    # A brief restrained entrance affects the information layer only; artwork never flashes black.
    entry = min(1, max(0, progress * shot.duration / 0.18))
    layer.putalpha(layer.getchannel("A").point(lambda a: round(a * (0.25 + 0.75 * entry))))
    if layer.size != canvas:
        layer = layer.resize(canvas, Image.Resampling.LANCZOS)
    return Image.alpha_composite(background.convert("RGBA"), layer).convert("RGB")
