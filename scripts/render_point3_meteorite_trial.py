"""Isolated Sun point-3 handoff using existing assets, speech, and production renderers.

This comparison does not alter the approved project or call a provider. The
existing caveat image is borrowed only for this focused proof; a future visual
plan must decide whether to reuse it or request distinct art for the caveat.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image
from pydantic import TypeAdapter

from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.rendering.longform_motion import frame_for_shot
from autonomous_studio.rendering.semantic_diagrams import Scene, render_frame

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "projects/live/f2230c84373c"
OUTPUT = ROOT / "out/point3-review"
FPS = 8
START = 8.54
SPLIT = 17.20  # After the spoken inclusion explanation, before the age estimate.
END = 24.44


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    existing = json.loads(
        (PROJECT / "artifacts/v001-semantic-chapter-1-cd770eef/decisions.json")
        .read_text(encoding="utf-8")
    )["decisions"][0]["scene_json"]
    saved = TypeAdapter(Scene).validate_json(existing)
    revised = saved.model_dump(mode="json")
    revised.update(
        id="point3-age-result",
        narration=(
            "One widely used estimate dates these early solids to about "
            "4.567 billion years ago."
        ),
        title="Estimated age of early solids",
        duration=round(END - SPLIT, 2),
        opening_label="One widely used estimate",
        opening_anchor="One widely used estimate",
        opening_at=0.22,
    )
    revised["points"][0].update(
        label="Early solids",
        at=round(20.86 - SPLIT, 2),
    )
    metric = TypeAdapter(Scene).validate_python(revised)
    source = Image.open(PROJECT / "images/v001/beat-3_image.png").convert("RGB")
    first = {
        "id": "point3-meteorite-evidence", "kind": "camera", "asset_ref": "meteorite",
        "start": 0.0, "end": SPLIT - START, "presentation": "fill",
        "crop_bottom": 0.26, "zoom_start": 1.02, "zoom_end": 1.08,
        "focus_start": [0.5, 0.45], "focus_end": [0.54, 0.43],
        "label": "Ancient meteorites",
        "secondary_label": "Calcium-aluminium-rich inclusions",
        "label_effect": "slide_plate", "text_zone": "upper_left",
        "cues": [
            {"action": "start_move", "at_seconds": 0, "duration_seconds": 0.4},
            {"action": "show_label", "at_seconds": round(10.18 - START, 2),
             "duration_seconds": 0.4},
            {"action": "show_secondary_label", "at_seconds": round(14.18 - START, 2),
             "duration_seconds": 0.4},
        ],
    }

    def frames():
        for index in range(round((END - START) * FPS)):
            now = index / FPS
            if now < SPLIT - START:
                yield frame_for_shot(first, now, (960, 540), {"meteorite": source})
            else:
                yield render_frame(
                    metric, now - (SPLIT - START), presentation="production"
                )

    frame_for_shot(first, 6.3, (960, 540), {"meteorite": source}).save(
        OUTPUT / "evidence-beat.png"
    )
    render_frame(metric, 4.4, size=(1920, 1080), presentation="production").save(
        OUTPUT / "corrected-age-beat.png"
    )
    ffmpeg = FFmpeg()
    silent = OUTPUT / "point3-handoff-silent.mp4"
    ffmpeg.encode(frames(), 960, 540, FPS, silent)
    excerpt = OUTPUT / "narration-excerpt.wav"
    ffmpeg.run([
        "-ss", START,
        "-i", PROJECT / "audio/v001/chapter-1-block-1.wav",
        "-vn", "-af", "apad=pad_dur=0.5", "-t", END - START,
        "-c:a", "pcm_s16le", excerpt,
    ])
    final = OUTPUT / "point3-handoff-with-narration.mp4"
    ffmpeg.mux(silent, excerpt, final)
    print(final)


if __name__ == "__main__":
    main()
