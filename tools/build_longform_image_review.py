"""Build isolated A/B samples from an approved long-form cut; never mutate project state."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.rendering.longform_motion import _camera, _opening_headline, _photo
from autonomous_studio.workflow.motion_planning import _present_image_shot


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "projects/live/f2230c84373c"
OUTPUT = ROOT / "out/longform-image-review-f2230c84373c"
PLANS = {
    "opening": PROJECT / "artifacts/v001-chapter-1-48d19198/motion-plan.json",
    "ordinary_text": PROJECT / "artifacts/v001-chapter-1-48d19198/motion-plan.json",
}
SHOT_IDS = {"opening": "beat-1", "ordinary_text": "beat-3"}


def _sample(name: str) -> tuple[dict, dict, Image.Image]:
    project = json.loads((PROJECT / "project.json").read_text(encoding="utf-8"))
    plan = json.loads(PLANS[name].read_text(encoding="utf-8"))
    before = next(shot for shot in plan["shots"] if shot["id"] == SHOT_IDS[name])
    after = json.loads(json.dumps(before))
    asset = next(
        item for item in project["longform_visual_plan"]["assets"]
        if item["id"] == before["asset_ref"]
    )
    _present_image_shot(after, asset, None)
    if name == "opening":
        after["hook_headline"] = "WILL THE SUN EXPLODE?"
        after["thumbnail_headline"] = "HOW WILL THE SUN END?"
    image_path = PROJECT / "images/v001" / f"{before['asset_ref']}.png"
    with Image.open(image_path) as opened:
        source = opened.convert("RGB")
    return before, after, source


def _pair(source, before, after, local_seconds: float) -> Image.Image:
    size = (640, 360)
    now = before["start"] + local_seconds
    baseline = _photo(source, before, now, size)
    updated = _photo(source, after, now, size)
    joined = Image.new("RGB", (1280, 394), (13, 19, 28))
    joined.paste(baseline, (0, 34))
    joined.paste(updated, (640, 34))
    draw = ImageDraw.Draw(joined)
    font = ImageFont.truetype(
        ROOT / "src/autonomous_studio/assets/fonts/BarlowCondensed-Bold.ttf", 24
    )
    draw.text((20, 2), "APPROVED BASELINE", fill="white", font=font)
    draw.text((660, 2), "PROPOSED IMAGE TREATMENT", fill="white", font=font)
    return joined


def _video(name: str, source, before, after) -> Path:
    fps = 6
    duration = min(8.0, before["end"] - before["start"])
    path = OUTPUT / f"{name}-side-by-side.mp4"
    command = FFmpeg().command([
        "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", "1280x394",
        "-framerate", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
        "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", path,
    ])
    with (OUTPUT / f"{name}-encode.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log
        )
        try:
            for index in range(round(duration * fps)):
                process.stdin.write(_pair(source, before, after, index / fps).tobytes())
            process.stdin.close()
            if process.wait(timeout=600):
                raise RuntimeError(f"Comparison encode failed: {name}")
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
    return path


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest = {"project": "f2230c84373c", "approved_project_mutated": False, "samples": []}
    for name in PLANS:
        before, after, source = _sample(name)
        duration = before["end"] - before["start"]
        for second in (0.0, min(1.0, duration - 0.01), min(5.5, duration - 0.01)):
            _pair(source, before, after, second).save(
                OUTPUT / f"{name}-{second:.1f}s.png"
            )
        video = _video(name, source, before, after)
        manifest["samples"].append({
            "name": name, "video": video.name, "motion_style": after["motion_style"],
            "label_effect": after["label_effect"], "text_zone": after["text_zone"],
            "opening_headline": after.get("hook_headline", ""),
            "thumbnail_headline": after.get("thumbnail_headline", ""),
        })
        if name == "opening":
            frame = _camera(source, after, after["start"] + 0.2, (1280, 720))
            _opening_headline(frame, after["thumbnail_headline"])
            frame.save(OUTPUT / "thumbnail-candidate.png")
        if name == "ordinary_text":
            # Four reusable entrance designs, with the approved words and cue intact.
            effects = ("fade_rise", "wipe", "slide_plate", "gentle_pop")
            montage = Image.new("RGB", (1280, 788), (13, 19, 28))
            cue = next(c for c in after["cues"] if c["action"] == "show_label")
            for index, effect in enumerate(effects):
                variant = {**after, "label_effect": effect}
                frame = _photo(source, variant, cue["at_seconds"] + 0.2, (640, 360))
                x = (index % 2) * 640
                y = (index // 2) * 394
                montage.paste(frame, (x, y + 34))
                ImageDraw.Draw(montage).text(
                    (x + 18, y + 7), effect.replace("_", " ").upper(), fill="white",
                    font=ImageFont.truetype(
                        ROOT / "src/autonomous_studio/assets/fonts/BarlowCondensed-Bold.ttf", 22
                    ),
                )
            montage.save(OUTPUT / "four-text-entrances.png")
            fps = 8
            effects_video = OUTPUT / "four-text-entrances.mp4"
            command = FFmpeg().command([
                "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", "640x394",
                "-framerate", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", effects_video,
            ])
            with (OUTPUT / "four-text-entrances-encode.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command, stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=log,
                )
                try:
                    for effect in effects:
                        variant = {**after, "label_effect": effect}
                        for index in range(2 * fps):
                            now = cue["at_seconds"] - 0.15 + index / fps
                            frame = Image.new("RGB", (640, 394), (13, 19, 28))
                            frame.paste(_photo(source, variant, now, (640, 360)), (0, 34))
                            ImageDraw.Draw(frame).text(
                                (18, 7), effect.replace("_", " ").upper(), fill="white",
                                font=ImageFont.truetype(
                                    ROOT / "src/autonomous_studio/assets/fonts/BarlowCondensed-Bold.ttf", 22
                                ),
                            )
                            process.stdin.write(frame.tobytes())
                    process.stdin.close()
                    if process.wait(timeout=600):
                        raise RuntimeError("Four-effects comparison encode failed")
                except BaseException:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    raise
            manifest["samples"].append({
                "name": "four_text_entrances", "video": effects_video.name,
                "same_approved_words_and_cue": True,
            })
    revised_earth = OUTPUT / "earth-prediction/generated-frame.png"
    if revised_earth.is_file():
        original_earth = PROJECT / "images/v001/ch4-beat-1_image.png"
        with Image.open(original_earth) as opened:
            old = opened.convert("RGB").resize((640, 360), Image.Resampling.LANCZOS)
        with Image.open(revised_earth) as opened:
            new = opened.convert("RGB").resize((640, 360), Image.Resampling.LANCZOS)
        comparison = Image.new("RGB", (1280, 394), (13, 19, 28))
        comparison.paste(old, (0, 34))
        comparison.paste(new, (640, 34))
        draw = ImageDraw.Draw(comparison)
        font = ImageFont.truetype(
            ROOT / "src/autonomous_studio/assets/fonts/BarlowCondensed-Bold.ttf", 24
        )
        draw.text((20, 2), "APPROVED BASELINE", fill="white", font=font)
        draw.text((660, 2), "NEW MODELLED-SCENARIO IMAGE", fill="white", font=font)
        comparison.save(OUTPUT / "earth-prediction-side-by-side.png")
        project = json.loads((PROJECT / "project.json").read_text(encoding="utf-8"))
        chapter_plan = json.loads(
            (PROJECT / "artifacts/v001-chapter-4-08589c52/motion-plan.json").read_text(
                encoding="utf-8"
            )
        )
        shot = next(s for s in chapter_plan["shots"] if s["id"] == "ch4-beat-1")
        asset = next(
            a for a in project["longform_visual_plan"]["assets"]
            if a["id"] == shot["asset_ref"]
        )
        shot = json.loads(json.dumps(shot))
        _present_image_shot(shot, asset, None)
        with Image.open(revised_earth) as opened:
            rendered = _photo(opened.convert("RGB"), shot, shot["start"] + 1.2, (1280, 720))
        rendered.save(OUTPUT / "earth-prediction-with-spoken-qualifier.png")
        manifest["samples"].append({
            "name": "earth_prediction", "still": "earth-prediction-side-by-side.png",
            "qualified_still": "earth-prediction-with-spoken-qualifier.png",
            "provider_receipt": "earth-prediction/receipt.json", "new_api_images": 1,
        })
    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
