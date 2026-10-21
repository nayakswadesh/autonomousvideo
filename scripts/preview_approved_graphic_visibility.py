"""Compare saved long-form semantic frames with approved text/graphic visibility.

This is a read-only project diagnostic: it writes PNGs to an explicit output
directory but never changes project state, approvals, or saved decisions.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import TypeAdapter

from autonomous_studio.rendering.semantic_diagrams import Scene, render_frame
from autonomous_studio.workflow.semantic_scene_contract import (
    apply_approved_graphic_visibility,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path, help="Path to a saved project.json")
    parser.add_argument("output", type=Path, help="Directory for diagnostic PNGs")
    parser.add_argument("--beat", action="append", dest="beats", required=True)
    args = parser.parse_args()
    project = json.loads(args.project.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    adapter = TypeAdapter(Scene)
    findings = []
    for beat_id in args.beats:
        chapter = next(
            (chapter for chapter in project["spec"]["chapters"]
             if any(beat["id"] == beat_id for beat in chapter["visual_beats"])),
            None,
        )
        if chapter is None:
            raise ValueError(f"unknown visual beat: {beat_id}")
        beat = next(item for item in chapter["visual_beats"] if item["id"] == beat_id)
        decisions = project["longform_semantic_decisions"][chapter["id"]]["decisions"]
        decision = next(item for item in decisions if item["beat_id"] == beat_id)
        before = adapter.validate_json(decision["scene_json"])
        after = apply_approved_graphic_visibility(beat, before)
        now = min(before.duration - 0.02, max(0.02, before.duration * 0.75))
        render_frame(before, now, presentation="production").save(
            args.output / f"{beat_id}-before.png"
        )
        render_frame(after, now, presentation="production").save(
            args.output / f"{beat_id}-after.png"
        )
        findings.append({
            "beat_id": beat_id,
            "chapter_id": chapter["id"],
            "family": before.family,
            "treatment": before.treatment,
            "approved_graphic_modes": [
                item["mode"] for item in beat["screen_blueprint"]["graphic_intents"]
            ],
            "comparison_time": round(now, 2),
        })
    (args.output / "findings.json").write_text(
        json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
