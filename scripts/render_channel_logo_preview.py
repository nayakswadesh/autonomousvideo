"""Render a saved closing scene with the current shared logo, outside project state."""

import argparse
import json
import math
import sqlite3
from pathlib import Path

from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.rendering.semantic_diagrams import EditorialCloseScene, render_frame


def main(project_id: str, root: Path):
    database = (root / "studio.sqlite3").resolve()
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT body FROM projects WHERE id=?", (project_id,)
        ).fetchone()
    if row is None:
        raise ValueError("Unknown project")
    project = json.loads(row[0])
    scenes = [
        json.loads(decision["scene_json"])
        for batch in project["longform_semantic_decisions"].values()
        for decision in batch["decisions"]
        if decision.get("scene_json")
    ]
    scene = EditorialCloseScene.model_validate(next(
        item for item in reversed(scenes)
        if item.get("family") == "editorial_close"
        and item.get("treatment") == "community_close"
    ))
    output = root.resolve() / project_id / "comparisons" / "channel-logo-preview"
    output.mkdir(parents=True, exist_ok=True)
    size = (1920, 1080)
    render_frame(scene, scene.duration - 0.1, size=size, presentation="production").save(
        output / "closing-shot.png"
    )
    fps = 24
    FFmpeg().encode(
        (
            render_frame(scene, frame / fps, size=size, presentation="production")
            for frame in range(math.ceil(scene.duration * fps))
        ),
        *size, fps, output / "closing-shot-silent.mp4",
    )
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("--root", type=Path, default=Path("projects/live"))
    args = parser.parse_args()
    main(args.project_id, args.root)
