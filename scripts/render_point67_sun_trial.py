"""Render isolated, non-approved point 6/7 Sun comparisons without provider calls.

Outputs are deliberately outside projects/live. They are editorial trials only.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import imageio_ffmpeg

from autonomous_studio.rendering.semantic_diagrams import (
    ProcessScene,
    render_frame,
    validate_scene_layout,
)

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "projects/live/f2230c84373c"
OUTPUT = ROOT / "out/point67-sun-trial"
SOURCE = (
    PROJECT / "artifacts/v001-semantic-chapter-3-b8ac5066/decisions.json"
)
AUDIO = PROJECT / "audio/v001/chapter-3-block-3.wav"
BASELINE = PROJECT / "review-video-v001-5b317598.mp4"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def candidate(treatment: str, narration: str) -> ProcessScene:
    labels = [
        "Approximate model estimate",
        "Core hydrogen dwindles",
        "Stable phase ends",
        "Sun expands",
    ]
    anchors = [
        "That five-to-six-billion-year range",
        "core hydrogen dwindles",
        "the stable phase ends",
        "the Sun expands",
    ]
    changes = ["none", "fill_down", "highlight", "expand"]
    steps = [
        {
            "label": label,
            "detail": "" if index else "Broad prediction, not a countdown",
            "anchor": anchor,
            "symbol": "box",
            "asset_concept": "time" if index == 0 else "sun",
            "state_visual": changes[index],
            "at": (0.04, 9.58, 11.1, 12.82)[index],
        }
        for index, (label, anchor) in enumerate(zip(labels, anchors, strict=True))
    ]
    return ProcessScene.model_validate(
        {
            "id": "ch3-beat-4-trial",
            "topic": "The story of the Sun",
            "narration": narration,
            "title": "A prediction, then a sequence",
            "duration": 16.5,
            "family": "process",
            "treatment": treatment,
            "steps": steps,
        }
    )


def render(scene: ProcessScene, destination: Path) -> None:
    validate_scene_layout(scene)
    fps = 8
    command = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "960x540",
        "-r", str(fps), "-i", "-", "-i", str(AUDIO),
        "-t", str(scene.duration), "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        str(destination),
    ]
    with subprocess.Popen(command, stdin=subprocess.PIPE) as process:
        assert process.stdin is not None
        for frame in range(round(scene.duration * fps)):
            image = render_frame(scene, frame / fps, presentation="production")
            process.stdin.write(image.convert("RGB").tobytes())
        process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError("trial encode failed")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    original = next(item for item in data["decisions"] if item["beat_id"] == "ch3-beat-4")
    narration = json.loads(original["scene_json"])["narration"]
    for treatment in ("transformation", "working"):
        render(candidate(treatment, narration), OUTPUT / f"trial-{treatment}.mp4")
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-ss", "178.125", "-i", str(BASELINE), "-t", "16.5",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
         "-c:a", "aac", str(OUTPUT / "baseline-four-node.mp4")],
        check=True,
    )


if __name__ == "__main__":
    main()
