"""Render the registry's new treatment fixtures as silent, reviewable motion clips.

The fixture data lives beside its validation tests so preview and contract tests
exercise the same topic-agnostic examples. This does not touch live projects.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from collections import defaultdict
from pathlib import Path

from pydantic import TypeAdapter

from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.rendering.semantic_diagrams import Scene, render_frame
from autonomous_studio.rendering.visual_registry import FAMILIES

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "examples" / "visual-registry-previews"
FPS = 8
SECONDS_PER_TREATMENT = 11.5
ADAPTER = TypeAdapter(Scene)


def _fixtures() -> list[dict]:
    source = ROOT / "tests" / "test_visual_family_registry.py"
    spec = importlib.util.spec_from_file_location("visual_registry_fixture", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load preview fixture: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.new_scene_examples()


def _frames(scenes):
    frame_count = round(SECONDS_PER_TREATMENT * FPS)
    for scene in scenes:
        for frame in range(frame_count):
            yield render_frame(scene, frame / FPS, presentation="study")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--point9", action="store_true", help="render only five new treatments")
    options = parser.parse_args()
    fixtures = _fixtures()
    if options.point9:
        selected = {
            ("evidence", "signal_to_inference"), ("cutaway", "focal_inset"),
            ("relationship", "nested_scope"), ("claim_boundary", "bounded_statement"),
            ("scenario_branch", "conditional_fork"),
        }
        fixtures = [
            item for item in fixtures
            if (item["family"], item["treatment"]) in selected
        ]
    scenes = [ADAPTER.validate_python(item) for item in fixtures]
    by_family = defaultdict(list)
    for scene in scenes:
        by_family[scene.family].append(scene)
    actual = {family: {scene.treatment for scene in items} for family, items in by_family.items()}
    registered = {
        family.id: {treatment.id for treatment in family.treatments}
        for family in FAMILIES
    }
    if any(not treatments <= registered[family] for family, treatments in actual.items()):
        raise ValueError("preview fixture references an unregistered treatment")
    new_treatments = {
        ("evidence", "signal_to_inference"), ("cutaway", "focal_inset"),
        ("relationship", "nested_scope"), ("claim_boundary", "bounded_statement"),
        ("scenario_branch", "conditional_fork"),
    }
    if not new_treatments <= {
        (family, treatment) for family, treatments in actual.items() for treatment in treatments
    }:
        raise ValueError("preview fixtures omit a newly registered treatment")

    output = ROOT / "out" / "point9-production-previews" if options.point9 else OUTPUT
    output.mkdir(parents=True, exist_ok=True)
    ffmpeg = FFmpeg()
    ordered = [family.id for family in FAMILIES if family.id in by_family]
    manifest = {"fps": FPS, "seconds_per_treatment": SECONDS_PER_TREATMENT, "families": []}
    clips = []
    for family in ordered:
        items = by_family[family]
        clip = output / f"{family}.mp4"
        ffmpeg.encode(_frames(items), 960, 540, FPS, clip)
        if options.point9:
            render_frame(items[0], 8.0, presentation="production").save(
                output / f"{family}.png"
            )
        clips.append(clip)
        manifest["families"].append(
            {"family": family, "file": clip.name, "treatments": [item.treatment for item in items]}
        )
        print(f"rendered {clip}", flush=True)

    playlist = output / "reel.txt"
    playlist.write_text("\n".join(f"file '{clip.name}'" for clip in clips) + "\n", encoding="utf-8")
    ffmpeg.run(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            playlist,
            "-c",
            "copy",
            output / "all-new-families.mp4",
        ],
        timeout=180,
    )
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"rendered {output / 'all-new-families.mp4'}", flush=True)


if __name__ == "__main__":
    main()
