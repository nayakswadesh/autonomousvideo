"""Render an isolated closing-shot layout from the approved v6 scene.

No project state, script, audio, or approved artifact is modified.
"""

from __future__ import annotations

import json
from pathlib import Path

from autonomous_studio.rendering.semantic_diagrams import EditorialCloseScene, render_frame


ROOT = Path(__file__).resolve().parents[1]
DECISIONS = (ROOT / "projects/live/f2230c84373c/artifacts/"
             "v006-semantic-chapter-3-760597eb/decisions.json")
OUTPUT = ROOT / "projects/live/f2230c84373c/comparisons/choice-close-v6"


def main() -> None:
    decisions = json.loads(DECISIONS.read_text(encoding="utf-8"))
    saved = next(item for item in decisions["decisions"]
                 if item["beat_id"] == "ch3-beat10")
    scene_data = json.loads(saved["scene_json"])
    scene_data["response_options"] = ["Meteorite dating", "Solar neutrinos"]
    scene = EditorialCloseScene.model_validate(scene_data)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, second in (("question", 2.0), ("subscribe-cue", 8.5),
                         ("final", 12.0)):
        render_frame(scene, second, presentation="production").save(
            OUTPUT / f"{name}.png"
        )
    print(OUTPUT)


if __name__ == "__main__":
    main()
