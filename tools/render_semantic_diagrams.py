"""Render cross-topic semantic diagram fixtures without API or project-state writes."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pydantic import TypeAdapter

from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.rendering.semantic_diagrams import (
    H,
    Scene,
    W,
    render_frame,
    scene_asset_report,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "examples/semantic_diagrams.json"
DEFAULT_OUTPUT = ROOT / "projects/live/154080f0be98/comparisons/semantic-engine-cross-topic"
FONT = ROOT / "src/autonomous_studio/assets/fonts/Barlow-Regular.ttf"
FPS = 12


def render(fixtures: Path, output: Path, stills_only: bool = False):
    scenes = TypeAdapter(list[Scene]).validate_json(fixtures.read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=True)
    ffmpeg = FFmpeg()
    montage = Image.new("RGB", (W * 2, (H + 100) * math.ceil(len(scenes) / 2)), "#e8eceb")
    font = ImageFont.truetype(FONT, 19)
    for i, scene in enumerate(scenes):
        stem = f"{i + 1:02d}-{scene.id}"
        frame = render_frame(scene, scene.duration * 0.8)
        frame.save(output / f"{stem}.png")
        if not stills_only:
            ffmpeg.encode(
                (render_frame(scene, j / FPS) for j in range(int(scene.duration * FPS))),
                W,
                H,
                FPS,
                output / f"{stem}.mp4",
            )
        col, row = i % 2, i // 2
        montage.paste(frame, (col * W, row * (H + 100)))
        draw = ImageDraw.Draw(montage)
        text = scene.narration
        # Two short lines under each scene, strictly as review-board notes.
        words, lines, current = text.split(), [], ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if draw.textbbox((0, 0), candidate, font=font)[2] > W - 80 and current:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        for j, value in enumerate(lines[:3]):
            draw.text(
                (col * W + 42, row * (H + 100) + H + 12 + j * 26), value, font=font, fill="#142c38"
            )
    montage.save(output / "cross-topic-overview.png")
    (output / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "id": s.id,
                    "topic": s.topic,
                    "family": s.family,
                    "treatment": s.treatment,
                    "narration": s.narration,
                    "provenance": s.provenance,
                    "assets": scene_asset_report(s),
                    "publication_safe": s.provenance == "verified"
                    and all(
                        a["status"] == "ready" and a["match"] != "generic_fallback"
                        for a in scene_asset_report(s)
                    ),
                }
                for s in scenes
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    return scenes


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stills-only", action="store_true")
    args = parser.parse_args()
    render(args.fixtures, args.output, args.stills_only)
