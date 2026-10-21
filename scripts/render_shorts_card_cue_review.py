"""Read-only, two-shot local proof using approved narration and the original video audio."""
import json
import sqlite3
from pathlib import Path

from autonomous_studio.domain.models import VideoProject
from autonomous_studio.rendering.editorial import scene
from autonomous_studio.rendering.ffmpeg import FFmpeg


def main():
    root = Path("projects/live").resolve()
    with sqlite3.connect(f"{(root / 'studio.sqlite3').as_uri()}?mode=ro", uri=True) as db:
        body = db.execute("SELECT body FROM projects WHERE id=?", ("99a941f3caca",)).fetchone()[0]
    project = VideoProject.model_validate_json(body)
    out = root / project.id / "comparisons/card-alignment-and-cues"
    out.mkdir(parents=True, exist_ok=True)
    ff = FFmpeg()
    report = []
    for index in (3, 4):
        shot = project.spec.shots[index].model_copy(deep=True)
        if index == 4:
            shot.entrance_cues = [
                "used models", "depend on assumptions", "rather than a complete census"
            ]
        project.spec.shots[index] = shot
        prefix = out / f"shot-{index + 1}"
        ff.run(["-ss", shot.start, "-i", root / project.id / "final-video.mp4",
                "-t", shot.duration, "-c:v", "libx264", "-c:a", "aac", f"{prefix}-before.mp4"])
        fps = project.spec.brief.fps
        ff.encode((scene(project, shot, frame / (shot.duration * fps))
                   for frame in range(round(shot.duration * fps))),
                  project.spec.brief.width, project.spec.brief.height, fps,
                  Path(f"{prefix}-silent.mp4"))
        ff.run(["-i", f"{prefix}-silent.mp4", "-ss", shot.start,
                "-i", root / project.id / "final-video.mp4", "-t", shot.duration,
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                f"{prefix}-after.mp4"])
        scene(project, shot, .65).save(f"{prefix}-after.png")
        report.append({"shot": shot.id, "entrance_cues": shot.entrance_cues})
    (out / "review.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
