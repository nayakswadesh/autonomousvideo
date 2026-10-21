"""Offline, non-production motion studies for proposed explanatory treatments.

These examples are intentionally NOT registered as production scene types. Their
renderers depend on semantic roles and supplied assets, never on the story topic.
No model or image API is called. Existing Sun stills are used only as review
references, not as inputs to the candidate renderer.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from autonomous_studio.rendering.ffmpeg import FFmpeg

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "out/point9-treatment-studies"
FONTS = ROOT / "src/autonomous_studio/assets/fonts"
SUN_STILLS = ROOT / "projects/live/f2230c84373c/images/v001"
W, H, FPS, SECONDS = 960, 540, 8, 8

NAVY = "#0b1723"
INK = "#f6f1e8"
MUTED = "#a8bdc8"
CYAN = "#59d6df"
GOLD = "#efc276"
CORAL = "#f09678"
GREEN = "#8fd5af"
PANEL = "#1a2e3d"
LINE = "#476273"


@dataclass(frozen=True)
class Study:
    treatment: str
    topic: str
    title: str
    speech: str
    roles: tuple[str, ...]
    baseline_still: str = ""
    asset_kind: str = ""
    caveat: str = "ILLUSTRATIVE DIAGRAM • NOT A MEASUREMENT"

    @property
    def stem(self) -> str:
        return f"{self.treatment}--{self.topic}"


STUDIES = (
    Study(
        "signal_to_inference", "sun", "How do we know fusion happens?",
        "Borexino's neutrino measurements test energy-producing reactions inside the Sun.",
        ("NEUTRINOS DETECTED", "EVIDENCE FOR SOLAR FUSION"),
        "ch2-beat-7_asset.png", caveat="OBSERVATION TO INFERENCE - NO DATA TRACE",
    ),
    Study(
        "signal_to_inference", "archaeology", "What does the ash tell us?",
        "An ash layer in the soil is evidence of a past fire; it is not an eyewitness record.",
        ("ASH LAYER FOUND", "PAST FIRE INFERRED"),
        caveat="ILLUSTRATIVE EXAMPLE • NOT A SPECIFIC EXCAVATION",
    ),
    Study(
        "focal_inset", "sun", "Look inside the Sun",
        "Hydrogen fusion occurs in the Sun's core, beyond the surface we can see.",
        ("THE SUN", "FUSION IN THE CORE"),
        "ch2-beat-4_image.png", "sun", "SCHEMATIC INTERIOR • NOT A PHOTOGRAPH",
    ),
    Study(
        "focal_inset", "bicycle", "Where does the brake act?",
        "The brake pad presses against the rotor; that contact slows the wheel.",
        ("DISC BRAKE", "PAD–ROTOR CONTACT"),
        asset_kind="brake", caveat="SCHEMATIC PARTS • NOT A PRODUCT PHOTO",
    ),
    Study(
        "bounded_statement", "sun", "A supported endpoint, not a timetable",
        "The Sun is expected to leave a white dwarf, but the timing and late-stage "
        "details remain model-dependent.",
        ("WHITE DWARF EXPECTED", "TIMING REMAINS MODEL-DEPENDENT"),
        "ch5-beat-4_asset.png", caveat="SUPPORTED ENDPOINT IS NOT AN EXACT SCHEDULE",
    ),
    Study(
        "bounded_statement", "transport", "Open—but for whom?",
        "The bridge is open to pedestrians, not to vehicles.",
        ("BRIDGE OPEN", "PEDESTRIANS ONLY"),
        caveat="ILLUSTRATIVE EXAMPLE • NO REAL BRIDGE CLAIMED",
    ),
    Study(
        "conditional_fork", "sun", "Earth's fate is unresolved",
        "Uncertain tidal effects and solar mass loss leave Earth's engulfment unresolved.",
        ("TIDES + MASS LOSS UNCERTAIN", "ENGULFMENT POSSIBLE", "SURVIVAL POSSIBLE"),
        "ch4-beat-3_asset.png", caveat="POSSIBLE OUTCOMES • WIDTH DOES NOT ENCODE ODDS",
    ),
    Study(
        "conditional_fork", "shipment", "What happens after inspection?",
        "Depending on the inspection, the shipment may be released or held for review.",
        ("INSPECTION OUTCOME UNKNOWN", "RELEASED", "HELD FOR REVIEW"),
        caveat="ILLUSTRATIVE DECISION • NO PROBABILITIES IMPLIED",
    ),
    Study(
        "nested_scope", "sun", "What actually ends?",
        "The Sun's stellar life ends; that does not mean the Solar System or universe ends.",
        ("SUN'S STELLAR LIFE", "SOLAR SYSTEM", "UNIVERSE"),
        "ch5-beat-9_asset.png", caveat="PART–WHOLE SCOPE • NOT TO PHYSICAL SCALE",
    ),
    Study(
        "nested_scope", "library", "One branch is not the whole network",
        "A branch library can close without the entire library network or city service ending.",
        ("ONE BRANCH", "LIBRARY NETWORK", "CITY SERVICES"),
        caveat="ILLUSTRATIVE ORGANIZATION • NOT A REAL CLOSURE",
    ),
)


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "BarlowCondensed-Bold.ttf" if bold else "Barlow-Regular.ttf"
    return ImageFont.truetype(FONTS / name, size)


def fit_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], value: str,
             width: int, size: int, color: str = INK, *, bold: bool = True,
             minimum: int = 18) -> None:
    for candidate in range(size, minimum - 1, -1):
        face = font(candidate, bold=bold)
        if draw.textbbox((0, 0), value, font=face)[2] <= width:
            draw.text(xy, value, font=face, fill=color)
            return
    raise ValueError(f"study text does not fit: {value}")


def ease(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def phase(t: float, onset: float, length: float = 0.65) -> float:
    return ease((t - onset) / length)


def base(study: Study) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (W, H), NAVY)
    draw = ImageDraw.Draw(image)
    for x in range(0, W, 60):
        draw.line((x, 0, x, H), fill="#102433", width=1)
    for y in range(0, H, 60):
        draw.line((0, y, W, y), fill="#102433", width=1)
    draw.rounded_rectangle((26, 24, 934, 516), radius=22, fill="#112331", outline=LINE, width=2)
    draw.text((50, 42), "EXPLANATORY MOTION STUDY", font=font(15, bold=True), fill=CYAN)
    fit_text(draw, (50, 70), study.title, 850, 39)
    draw.line((50, 127, 910, 127), fill=LINE, width=2)
    fit_text(draw, (50, 478), study.caveat, 840, 16, MUTED, minimum=13)
    return image, draw


def arrow(draw: ImageDraw.ImageDraw, a: tuple[int, int], b: tuple[int, int],
          color: str, progress: float, width: int = 5) -> None:
    end = (int(a[0] + (b[0] - a[0]) * progress), int(a[1] + (b[1] - a[1]) * progress))
    draw.line((*a, *end), fill=color, width=width)
    if progress > 0.92:
        angle = math.atan2(b[1] - a[1], b[0] - a[0])
        for direction in (-0.55, 0.55):
            draw.line((end[0], end[1], end[0] - int(16 * math.cos(angle + direction)),
                       end[1] - int(16 * math.sin(angle + direction))), fill=color, width=width)


def signal_to_inference(study: Study, t: float) -> Image.Image:
    image, draw = base(study)
    draw.rounded_rectangle((48, 163, 405, 434), radius=20, fill=PANEL, outline=CYAN, width=2)
    draw.rounded_rectangle((555, 163, 912, 434), radius=20, fill=PANEL, outline=GOLD, width=2)
    draw.text((73, 184), "OBSERVED SIGNAL", font=font(18, bold=True), fill=CYAN)
    draw.text((580, 184), "SUPPORTED INFERENCE", font=font(18, bold=True), fill=GOLD)
    # A schematic observation mark avoids inventing a plausible-looking data trace.
    halo = round(5 * phase(t, 0.45, 1.0))
    draw.ellipse((175, 232, 301, 358), outline=CYAN, width=max(2, halo))
    draw.line((289, 345, 340, 386), fill=CYAN, width=8)
    for index, (x, y) in enumerate(((205, 291), (238, 261), (268, 310))):
        if t >= 0.8 + index * 0.55:
            draw.ellipse((x-9, y-9, x+9, y+9), fill=GOLD)
    if t >= 1.1:
        fit_text(draw, (72, 368), study.roles[0], 310, 24, INK)
    arrow(draw, (419, 298), (538, 298), GOLD, phase(t, 2.8, 1.2))
    if t >= 3.75:
        draw.ellipse((670, 235, 788, 353), fill="#30495a", outline=GOLD, width=4)
        draw.ellipse((714, 278, 743, 307), fill=GOLD)
        fit_text(draw, (579, 371), study.roles[1], 315, 25, INK)
    return image


def _subject_art(kind: str, scale: int = 3) -> Image.Image:
    art = Image.new("RGB", (420 * scale, 310 * scale), "#152c3a")
    d = ImageDraw.Draw(art)
    def s(values):
        return tuple(value * scale for value in values)

    if kind == "sun":
        for radius in range(137, 15, -3):
            warmth = 160 + round(80 * (1 - radius / 137))
            d.ellipse(s((210-radius, 155-radius, 210+radius, 155+radius)),
                      fill=(warmth, 112 + round(100 * (1-radius/137)), 48))
        d.ellipse(s((154, 99, 266, 211)), outline=CYAN, width=5 * scale)
        d.ellipse(s((189, 134, 231, 176)), fill="#fff2b0")
    elif kind == "brake":
        d.ellipse(s((75, 25, 345, 295)), fill="#7e96a2", outline="#e2eced", width=8*scale)
        d.ellipse(s((115, 65, 305, 255)), fill="#253d4b", outline="#bcd4d8", width=5*scale)
        d.ellipse(s((182, 127, 238, 183)), fill="#c4d7db")
        d.rounded_rectangle(s((264, 70, 351, 204)), radius=15*scale,
                            fill=CORAL, outline=INK, width=4*scale)
        d.rounded_rectangle(s((277, 85, 308, 191)), radius=5*scale, fill="#783f3b")
        d.rectangle(s((213, 119, 272, 161)), fill="#f6ca88")
    else:
        raise ValueError(f"unknown fixture art {kind}")
    return art


def focal_inset(study: Study, t: float) -> Image.Image:
    image, draw = base(study)
    art_large = _subject_art(study.asset_kind)
    art = art_large.resize((420, 310), Image.Resampling.LANCZOS)
    image.paste(art, (60, 152))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((60, 152, 480, 462), radius=18, outline=LINE, width=3)
    fit_text(draw, (77, 170), study.roles[0], 370, 26, INK)
    focus = (154, 99, 266, 211) if study.asset_kind == "sun" else (220, 95, 330, 190)
    fx0, fy0, fx1, fy1 = (focus[0]+60, focus[1]+152, focus[2]+60, focus[3]+152)
    p = phase(t, 1.05, 0.8)
    if p:
        draw.ellipse((fx0, fy0, fx1, fy1), outline=CYAN, width=max(1, round(5*p)))
    inset = ImageOps.fit(
        art_large.crop(tuple(value * 3 for value in focus)),
        (275, 205), method=Image.Resampling.LANCZOS,
    )
    if t >= 3.0:
        draw.line((fx1, fy0, 580, 192), fill=CYAN, width=3)
        draw.line((fx1, fy1, 580, 397), fill=CYAN, width=3)
        image.paste(inset, (585, 190))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((580, 185, 865, 400), radius=14, outline=GOLD, width=4)
        fit_text(draw, (583, 419), study.roles[1], 320, 27, GOLD)
    return image


def bounded_statement(study: Study, t: float) -> Image.Image:
    image, draw = base(study)
    draw.text((59, 161), "WHAT WE CAN SAY", font=font(20, bold=True), fill=GREEN)
    draw.rounded_rectangle((53, 199, 907, 319), radius=19, fill=PANEL,
                           outline=GREEN, width=4)
    fit_text(draw, (84, 232), study.roles[0], 790, 52, INK)
    if t >= 2.5:
        p = phase(t, 2.5, 0.7)
        draw.line((55, 342, 55 + int(850*p), 342), fill=CORAL, width=5)
        draw.text((59, 364), "WHERE THE CLAIM STOPS", font=font(20, bold=True), fill=CORAL)
    if t >= 3.5:
        draw.rounded_rectangle((53, 397, 907, 453), radius=14, fill="#402f38",
                               outline=CORAL, width=2)
        fit_text(draw, (75, 407), study.roles[1], 805, 31, INK)
    return image


def conditional_fork(study: Study, t: float) -> Image.Image:
    image, draw = base(study)
    draw.rounded_rectangle((225, 161, 735, 257), radius=18, fill=PANEL,
                           outline=GOLD, width=3)
    draw.text((252, 177), "CONDITION / UNKNOWN", font=font(17, bold=True), fill=GOLD)
    fit_text(draw, (252, 204), study.roles[0], 460, 28, INK)
    p = phase(t, 2.15, 1.2)
    arrow(draw, (480, 257), (274, 342), CYAN, p, 4)
    arrow(draw, (480, 257), (686, 342), CORAL, p, 4)
    if t >= 3.15:
        draw.rounded_rectangle((65, 341, 441, 432), radius=17, fill=PANEL,
                               outline=CYAN, width=3)
        draw.rounded_rectangle((519, 341, 895, 432), radius=17, fill=PANEL,
                               outline=CORAL, width=3)
        fit_text(draw, (85, 365), study.roles[1], 335, 30, INK)
        fit_text(draw, (539, 365), study.roles[2], 335, 30, INK)
    return image


def nested_scope(study: Study, t: float) -> Image.Image:
    image, draw = base(study)
    levels = (
        ((220, 174, 740, 448), MUTED, study.roles[2], (246, 179)),
        ((307, 230, 676, 423), CYAN, study.roles[1], (326, 236)),
        ((412, 291, 582, 392), GOLD, study.roles[0], (426, 303)),
    )
    # Outer boxes appear first as context; a moving outline then travels outward.
    for box, color, label, label_pos in levels:
        draw.rounded_rectangle(box, radius=20, fill=PANEL, outline=LINE, width=2)
        fit_text(draw, label_pos, label, box[2]-box[0]-35, 22, color, minimum=16)
    at = 0 if t < 2.3 else 1 if t < 4.5 else 2
    box, color, _, _ = levels[2-at]
    p = phase(t, (0.45, 2.3, 4.5)[at])
    if p:
        draw.rounded_rectangle(box, radius=20, outline=color, width=max(2, round(6*p)))
    draw.text((52, 451), "ONE PART  /  ITS SYSTEM  /  THE WIDER WHOLE",
              font=font(19, bold=True), fill=GOLD)
    return image


RENDERERS = {
    "signal_to_inference": signal_to_inference,
    "focal_inset": focal_inset,
    "bounded_statement": bounded_statement,
    "conditional_fork": conditional_fork,
    "nested_scope": nested_scope,
}


def render_study(study: Study, t: float) -> Image.Image:
    if study.treatment not in RENDERERS:
        raise ValueError(f"unknown candidate treatment: {study.treatment}")
    return RENDERERS[study.treatment](study, t)


def _frames(study: Study):
    for index in range(SECONDS * FPS):
        yield render_study(study, index / FPS)


def comparison_board(study: Study, candidate: Image.Image) -> Image.Image:
    baseline = SUN_STILLS / study.baseline_still
    if not baseline.is_file():
        raise FileNotFoundError(baseline)
    with Image.open(baseline) as source:
        before = ImageOps.fit(source.convert("RGB"), (W, H), method=Image.Resampling.LANCZOS)
    board = Image.new("RGB", (W * 2, H + 95), "#e9e5dc")
    d = ImageDraw.Draw(board)
    d.text(
        (22, 13), "CURRENT SUN CUT — SAVED SCENE STILL",
        font=font(23, bold=True), fill="#203344",
    )
    d.text(
        (W + 22, 13), "POINT 9 — CANDIDATE MOTION FRAME",
        font=font(23, bold=True), fill="#203344",
    )
    board.paste(before, (0, 47))
    board.paste(candidate, (W, 47))
    d = ImageDraw.Draw(board)
    d.text((24, H + 55), "Visual comparison only; timing is in the paired silent MP4.",
           font=font(19), fill="#203344")
    return board


def overview(studies: tuple[Study, ...]) -> Image.Image:
    sun = [item for item in studies if item.topic == "sun"]
    board = Image.new("RGB", (1280, 5 * 272 + 68), "#e9e5dc")
    draw = ImageDraw.Draw(board)
    draw.text((25, 16), "POINT 9  /  FIVE CANDIDATE EXPLANATORY TREATMENTS",
              font=font(33, bold=True), fill="#203344")
    for index, item in enumerate(sun):
        with Image.open(OUTPUT / f"{item.stem}-comparison.png") as source:
            strip = source.convert("RGB").resize((1240, 240), Image.Resampling.LANCZOS)
        board.paste(strip, (20, 67 + index * 272))
        draw = ImageDraw.Draw(board)
        draw.text((26, 67 + index * 272 + 242), item.treatment.replace("_", " ").upper(),
                  font=font(21, bold=True), fill="#203344")
    return board


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    ffmpeg = FFmpeg()
    manifest = {"status": "experimental, not production", "fps": FPS,
                "seconds": SECONDS, "size": [W, H], "studies": []}
    for study in STUDIES:
        preview = OUTPUT / f"{study.stem}.mp4"
        ffmpeg.encode(_frames(study), W, H, FPS, preview)
        still = render_study(study, 6.3)
        still.save(OUTPUT / f"{study.stem}.png")
        if study.baseline_still:
            comparison_board(study, still).save(OUTPUT / f"{study.stem}-comparison.png")
        manifest["studies"].append({
            "candidate": study.treatment, "topic": study.topic,
            "spoken_example": study.speech, "roles": list(study.roles),
            "caveat": study.caveat, "preview": preview.name,
            "still": f"{study.stem}.png",
            "current_sun_still": study.baseline_still or None,
        })
        print(f"rendered {preview}", flush=True)
    overview(STUDIES).save(OUTPUT / "overview.png")
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
