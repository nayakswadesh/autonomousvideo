"""Isolated point-12 visual trial using the approved Sun video's final soundtrack.

This does not modify project state, narration, or the approved render.
"""

from __future__ import annotations

from pathlib import Path

from PIL import ImageDraw

from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.rendering.semantic_diagrams import EditorialCloseScene, render_frame

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "projects/live/f2230c84373c/review-video-v001-5b317598.mp4"
OUTPUT = ROOT / "out/longform-point10-12-sun-close-trial"
DURATION = 18.84
FPS = 24


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    narration = (
        "Which would you most like explained next: how meteorites date early solids, "
        "or how neutrinos reveal solar fusion? Subscribe for astronomy stories that "
        "distinguish evidence from prediction. Like this if it clarified the Sun’s "
        "future, and share it with someone curious about stars."
    )
    scene = EditorialCloseScene.model_validate({
        "id": "sun-approved-close-trial",
        "topic": "The story of the Sun",
        "narration": narration,
        "title": "What should we explore next?",
        "duration": DURATION,
        "family": "editorial_close",
        "treatment": "community_close",
        "end_screen_side": "right",
        "actions": [
            {
                "role": "comment",
                "label": "What next: meteorite dating or solar neutrinos?",
                "anchor": "Which would you most like explained next",
                "at": 0.1,
            },
            {
                "role": "subscribe",
                "label": "Subscribe for astronomy stories",
                "anchor": "Subscribe for astronomy stories",
                "at": 9.5,
            },
            {
                "role": "like_share",
                "label": "Like & share if this helped",
                "anchor": "Like this if it clarified",
                "at": 14.12,
            },
        ],
    })
    ffmpeg = FFmpeg()
    baseline = OUTPUT / "approved-close-baseline.mp4"
    silent = OUTPUT / "integrated-close-silent.mp4"
    candidate = OUTPUT / "integrated-close-approved-audio.mp4"
    comparison = OUTPUT / "side-by-side.mp4"

    ffmpeg.run(["-sseof", f"-{DURATION}", "-i", SOURCE, "-t", str(DURATION),
                "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "21", "-c:a", "aac", baseline],
               timeout=180)
    ffmpeg.encode(
        (render_frame(scene, i / FPS, presentation="production")
         for i in range(round(DURATION * FPS))),
        960, 540, FPS, silent,
    )
    ffmpeg.run(["-i", silent, "-i", baseline, "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-shortest", candidate], timeout=180)
    ffmpeg.run([
        "-i", baseline, "-i", candidate,
        "-filter_complex", "[0:v]scale=960:540[left];[left][1:v]hstack=inputs=2[out]",
        "-map", "[out]", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "21", "-c:a", "copy", "-shortest", comparison,
    ], timeout=180)
    final = render_frame(scene, 17.5, presentation="production")
    final.save(OUTPUT / "close-final-frame.png")
    mock = final.copy()
    draw = ImageDraw.Draw(mock)
    draw.rounded_rectangle((663, 149, 937, 300), radius=12, fill="#294356",
                           outline="#9ab2bd", width=2)
    draw.text((680, 216), "MOCK NEXT VIDEO", fill="white")
    draw.ellipse((747, 336, 828, 417), fill="#294356", outline="#9ab2bd", width=2)
    draw.text((700, 430), "MOCK SUBSCRIBE", fill="white")
    mock.save(OUTPUT / "close-with-mock-youtube-end-screen.png")
    print(comparison)


if __name__ == "__main__":
    main()
