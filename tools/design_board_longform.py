"""Render offline, non-production motion studies for four explanatory-visual families.

The examples paraphrase the approved Marie Curie script. Shapes are explanatory
schematics, not reconstructions of historical equipment or numerical datasets.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from autonomous_studio.rendering.ffmpeg import FFmpeg

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "projects/live/154080f0be98/comparisons/code-visual-design-board"
FONT = ROOT / "src/autonomous_studio/assets/fonts"
W, H, FPS, SECONDS = 960, 540, 12, 5
INK = "#132736"
MUTED = "#496274"
PAPER = "#f5f1e8"
CYAN = "#23bed0"
BLUE = "#3179b9"
ORANGE = "#e58c54"
GOLD = "#edc378"
GREEN = "#60b994"
RED = "#dc6e68"


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT / ("BarlowCondensed-Bold.ttf" if bold else "Barlow-Regular.ttf"), size)


def ease(v: float) -> float:
    v = max(0, min(1, v))
    return v * v * (3 - 2 * v)


def phase(t: float, start: float, length: float = 0.55) -> float:
    return ease((t - start) / length)


def text(d, xy, value, size=25, fill=INK, bold=False, anchor=None):
    d.text(xy, value, font=font(size, bold), fill=fill, anchor=anchor)


def rect(d, box, fill, radius=15, outline=None, width=2):
    d.rounded_rectangle(tuple(map(int, box)), radius, fill=fill, outline=outline, width=width)


def line(d, points, color=INK, width=4):
    d.line(points, fill=color, width=width, joint="curve")


def arrow(d, x1, y1, x2, y2, color=INK, width=4):
    line(d, [(x1, y1), (x2, y2)], color, width)
    a = math.atan2(y2 - y1, x2 - x1)
    for delta in (-0.58, 0.58):
        x = x2 - 14 * math.cos(a + delta)
        y = y2 - 14 * math.sin(a + delta)
        line(d, [(x2, y2), (x, y)], color, width)


def base(family: str, variant: str, title: str, light=True):
    bg = PAPER if light else "#0b1925"
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    fg = INK if light else "#f7f3eb"
    subtle = MUTED if light else "#a9bfca"
    text(d, (42, 25), family.upper(), 17, subtle, True)
    text(d, (918, 25), variant.upper(), 17, subtle, True, "ra")
    text(d, (42, 59), title, 38, fg, True)
    line(d, [(42, 114), (918, 114)], "#c6d4d5" if light else "#355165", 2)
    return img, d, fg, subtle


def footer(d, value: str, light=True):
    text(d, (42, 509), value, 18, MUTED if light else "#a9bfca")


def process_working(t):
    im, d, fg, sub = base("Process", "A  /  working diagram", "Follow the activity")
    stages = [
        (165, "ORE SAMPLE", "Start with the material"),
        (480, "MEASURE", "Record its activity"),
        (795, "SEPARATE", "Test the portions"),
    ]
    for i, (x, label, caption) in enumerate(stages):
        active = phase(t, 0.35 + i * 1.18)
        d.ellipse((x - 54, 195, x + 54, 303), fill="#dce4df" if i == 0 else "#e4ebec", outline=INK, width=3)
        if i == 0:
            for px, py, r in ((-25, -10, 8), (12, -19, 12), (5, 19, 10), (32, 14, 7)):
                d.ellipse((x + px - r, 249 + py - r, x + px + r, 249 + py + r), fill="#886b5d")
        elif i == 1:
            line(d, [(x - 25, 264), (x - 8, 264), (x - 1, 225), (x + 9, 280), (x + 26, 280)], BLUE, 5)
        else:
            rect(d, (x - 32, 240, x - 4, 275), "#dfb281", 4)
            rect(d, (x + 9, 229, x + 36, 275), "#b78969", 4)
        if active:
            d.arc((x - 61, 188, x + 61, 310), 205, 205 + int(330 * active), fill=CYAN, width=8)
        text(d, (x, 332), label, 25, fg, True, "ma")
        text(d, (x, 370), caption, 19, sub, anchor="ma")
        if i < 2:
            arrow(d, x + 69, 249, x + 233, 249, CYAN if phase(t, 1.12 + i * 1.18) > 0 else "#c6d4d5")
    footer(d, "Schematic: the search is guided by repeated measurements")
    return im


def process_transform(t):
    im, d, fg, sub = base("Process", "B  /  transformation", "One sample, successive tests", False)
    cx, cy = 475, 295
    split = phase(t, 1.15)
    if split < 0.95:
        d.ellipse((cx - 112, cy - 75, cx + 112, cy + 75), fill="#15344a", outline="#5f8995", width=3)
        d.ellipse((cx - 80, cy - 50, cx + 80, cy + 50), fill="#936e60")
        for dx, dy, r in ((-43, -19, 12), (18, -26, 15), (40, 19, 9), (-15, 25, 10)):
            d.ellipse((cx + dx - r, cy + dy - r, cx + dx + r, cy + dy + r), fill="#d9ad7f")
    if split > 0.05:
        for direction, label in ((-1, "PORTION A"), (1, "PORTION B")):
            x = cx + direction * 178 * split
            rect(d, (x - 106, 235, x + 106, 345), "#284d58", 13, "#90aeb1")
            d.ellipse((x - 25, 253, x + 25, 294), fill="#d5ad7d")
            text(d, (x, 321), label, 23, "#f7f3eb", True, "ma")
    if phase(t, 2.35):
        p = phase(t, 2.35)
        text(d, (475, 414), "Re-measure each portion", 29, GOLD, True, "ma")
        for x in (297, 653):
            line(d, [(x - 100, 366), (x + 100, 366)], "#385366", 5)
            line(d, [(x - 100, 366), (x - 100 + 200 * p, 366)], CYAN, 5)
    footer(d, "Explanatory schematic, not a reconstruction of Curie's apparatus", False)
    return im


def evidence_competing(t):
    im, d, fg, sub = base("Evidence", "A  /  competing explanations", "What could explain the activity?", False)
    rect(d, (55, 158, 440, 392), "#1c3444", 18, "#53707d")
    rect(d, (520, 158, 905, 392), "#1c3444", 18, "#53707d")
    text(d, (83, 185), "URANIUM ALONE", 27, "#e8edf0", True)
    text(d, (548, 185), "ANOTHER ACTIVE SOURCE", 27, "#e8edf0", True)
    text(d, (83, 239), "Expected explanation", 22, "#afc4cb")
    text(d, (548, 239), "Possible explanation", 22, "#afc4cb")
    if phase(t, .55):
        p = phase(t, .55)
        rect(d, (340, 300, 620, 355), GOLD, 12)
        text(d, (480, 326), "ORE: UNEXPECTEDLY STRONG", 22, INK, True, "ma")
        line(d, [(340, 328), (270, 328)], RED, int(2 + 3 * p))
        line(d, [(620, 328), (690, 328)], GREEN, int(2 + 3 * p))
    if phase(t, 2.0):
        text(d, (83, 438), "Cannot account for the result alone", 21, "#f0a8a2")
        text(d, (548, 438), "A lead to investigate", 21, "#8fe0b4")
    footer(d, "An anomaly motivates a search; it is not yet proof of a specific element", False)
    return im


def evidence_prediction(t):
    im, d, fg, sub = base("Evidence", "B  /  prediction vs observation", "The result did not fit")
    x0, y0 = 200, 398
    line(d, [(x0, y0), (782, y0)], MUTED, 2)
    text(d, (90, 218), "ACTIVITY", 21, MUTED, True)
    expected = 140 * phase(t, .45)
    observed = 220 * phase(t, 1.65)
    if expected:
        rect(d, (290, y0 - expected, 410, y0), "#91b0c1", 8)
    if observed:
        rect(d, (550, y0 - observed, 670, y0), ORANGE, 8)
    text(d, (350, 433), "Expected", 24, fg, True, "ma")
    text(d, (610, 433), "Observed", 24, fg, True, "ma")
    if phase(t, 2.4):
        arrow(d, 475, 250, 535, 210, RED)
        text(d, (490, 150), "A discrepancy worth investigating", 24, RED, True, "ma")
    footer(d, "Relative schematic only—no historical numeric readings are implied")
    return im


def apparatus(d, shift=0, dark=False):
    metal = "#a5c1c3" if dark else "#628895"
    surface = "#d5aa79" if dark else "#b18a65"
    rect(d, (170 + shift, 299, 790 + shift, 323), metal, 5)
    for x in (225, 735):
        line(d, [(x + shift, 323), (x + shift, 387)], metal, 9)
    d.ellipse((280 + shift, 230, 390 + shift, 283), fill=surface)
    d.ellipse((320 + shift, 233, 360 + shift, 263), fill="#826249")
    rect(d, (548 + shift, 211, 695 + shift, 286), "#264c60" if dark else "#dce9e8", 9, metal)
    line(d, [(400 + shift, 260), (545 + shift, 260)], CYAN, 4)
    line(d, [(581 + shift, 250), (607 + shift, 250), (616 + shift, 226), (632 + shift, 269), (650 + shift, 246)], GOLD if dark else BLUE, 3)


def cutaway_guided(t):
    im, d, fg, sub = base("Annotated cutaway", "A  /  guided inspection", "Where was the activity measured?")
    rect(d, (75, 148, 885, 435), "#e1e9e6", 16)
    apparatus(d)
    if phase(t, .7):
        p = phase(t, .7)
        d.arc((250, 201, 421, 316), 20, 20 + int(320 * p), fill=ORANGE, width=6)
        line(d, [(300, 202), (220, 180)], ORANGE, 3)
        text(d, (118, 156), "Sample", 24, INK, True)
    if phase(t, 2.15):
        p = phase(t, 2.15)
        d.arc((520, 184, 722, 311), 20, 20 + int(320 * p), fill=BLUE, width=6)
        line(d, [(706, 217), (760, 179)], BLUE, 3)
        text(d, (768, 152), "Reading", 24, INK, True)
    footer(d, "Conceptual measurement setup—not a historical apparatus drawing")
    return im


def cutaway_exploded(t):
    im, d, fg, sub = base("Annotated cutaway", "B  /  exploded arrangement", "What makes a measurement?", False)
    p = phase(t, .6, 1.2)
    centers = [(250 - 25 * p, "SAMPLE", "material"), (480, "SIGNAL", "ionizing effect"), (710 + 25 * p, "READING", "comparable value")]
    for i, (x, label, caption) in enumerate(centers):
        rect(d, (x - 88, 207 - 16*p, x + 88, 348 + 16*p), "#1a394c", 13, "#5b8291")
        if i == 0:
            d.ellipse((x - 35, 243, x + 35, 305), fill="#bb936f")
        elif i == 1:
            for k in range(3):
                d.arc((x - 43 + k*20, 241, x + 11 + k*20, 309), 260, 100, fill=CYAN, width=4)
        else:
            line(d, [(x - 42, 285), (x - 20, 285), (x - 12, 255), (x + 2, 311), (x + 16, 269), (x + 40, 269)], GOLD, 4)
        text(d, (x, 368), label, 26, "#f7f3eb", True, "ma")
        text(d, (x, 400), caption, 18, "#a9bfca", anchor="ma")
    if p:
        arrow(d, 345, 278, 379, 278, CYAN)
        arrow(d, 575, 278, 609, 278, CYAN)
    footer(d, "Abstract relationship diagram; components are not a replica", False)
    return im


def car(d, x, y, scale=1, color=BLUE):
    rect(d, (x, y+26*scale, x+225*scale, y+82*scale), color, 8*scale)
    rect(d, (x+130*scale, y, x+210*scale, y+56*scale), color, 7*scale)
    rect(d, (x+140*scale, y+10*scale, x+196*scale, y+32*scale), "#d6e7e5", 3*scale)
    for xx in (45, 180):
        d.ellipse((x+(xx-17)*scale, y+68*scale, x+(xx+17)*scale, y+102*scale), fill=INK)


def range_bounded(t):
    im, d, fg, sub = base("Estimate & range", "A  /  bounded scale", "Accounts differ: 18–20 cars")
    xs = (230, 395, 560, 725)
    line(d, [(xs[0], 343), (xs[-1], 343)], "#9aadae", 4)
    for x, n in zip(xs, (17, 18, 19, 20)):
        line(d, [(x, 330), (x, 356)], MUTED, 3)
        text(d, (x, 378), str(n), 29, INK, True, "ma")
    p = phase(t, .7, 1.3)
    if p:
        right = xs[1] + (xs[-1] - xs[1]) * p
        rect(d, (xs[1], 308, right, 328), ORANGE, 9)
        d.ellipse((xs[1]-9, 305, xs[1]+9, 333), fill=ORANGE)
        d.ellipse((right-9, 305, right+9, 333), fill=ORANGE)
    if phase(t, 2.3):
        text(d, (480, 226), "18–20 mobile radiology cars", 31, ORANGE, True, "ma")
        text(d, (480, 457), "An approximate historical count", 22, MUTED, anchor="ma")
    footer(d, "A bounded historical report, not an exact fleet census")
    return im


def range_context(t):
    im, d, fg, sub = base("Estimate & range", "B  /  contextual tag", "Radiology went mobile", False)
    rect(d, (95, 157, 865, 406), "#153143", 18)
    line(d, [(130, 362), (815, 362)], "#64838e", 3)
    car(d, 245, 229, 1.45, "#6b99a2")
    if phase(t, .55):
        arrow(d, 620, 279, 720, 279, GOLD)
        text(d, (670, 241), "MOBILE X-RAY", 23, GOLD, True, "ma")
    p = phase(t, 1.9)
    if p:
        rect(d, (637, 327, 823, 388), "#f2dfbc", 12)
        text(d, (730, 344), "ABOUT 18–20", 24, INK, True, "ma")
        text(d, (730, 365), "reported cars", 17, INK, anchor="ma")
    footer(d, "Illustrative vehicle silhouette, not an archival photograph", False)
    return im


DESIGNS = [
    ("01-process-working", process_working),
    ("02-process-transformation", process_transform),
    ("03-evidence-competing", evidence_competing),
    ("04-evidence-prediction", evidence_prediction),
    ("05-cutaway-guided", cutaway_guided),
    ("06-cutaway-exploded", cutaway_exploded),
    ("07-range-bounded", range_bounded),
    ("08-range-context", range_context),
]


def render(output: Path, videos: bool):
    output.mkdir(parents=True, exist_ok=True)
    sheets = []
    ffmpeg = FFmpeg()
    for index in range(0, len(DESIGNS), 2):
        pair = DESIGNS[index:index+2]
        sheet = Image.new("RGB", (W*2, H), "#ffffff")
        for j, (name, draw_frame) in enumerate(pair):
            cover = draw_frame(3.8)
            cover.save(output / f"{name}.png")
            sheet.paste(cover, (j*W, 0))
            if videos:
                ffmpeg.encode((draw_frame(i/FPS) for i in range(FPS*SECONDS)), W, H, FPS, output / f"{name}.mp4")
        family = pair[0][0].split("-")[1]
        sheet.save(output / f"board-{family}.png")
        sheets.append(sheet)
    overview = Image.new("RGB", (W*2, H*4), "#ffffff")
    for i, sheet in enumerate(sheets):
        overview.paste(sheet, (0, i*H))
    overview.save(output / "overview.png")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--stills-only", action="store_true")
    args = parser.parse_args()
    render(args.output, not args.stills_only)
