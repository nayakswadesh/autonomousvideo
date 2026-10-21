"""Read-only still proof of the metric-pair alignment fix at 00:45."""
import sqlite3
from pathlib import Path

from autonomous_studio.domain.models import VideoProject
from autonomous_studio.rendering.editorial import scene


def main():
    root = Path("projects/live").resolve()
    with sqlite3.connect(f"{(root / 'studio.sqlite3').as_uri()}?mode=ro", uri=True) as db:
        body = db.execute("SELECT body FROM projects WHERE id=?", ("77f45ea42919",)).fetchone()[0]
    project = VideoProject.model_validate_json(body)
    shot = next(s for s in project.spec.shots if s.id == "sh6")
    look = next(s for s in project.spec.presentation.shots if s.shot_id == shot.id)
    image = next(a for a in project.artifacts if a.id == look.image_ref)
    out = root / project.id / "comparisons/metric-pair-centering"
    out.mkdir(parents=True, exist_ok=True)
    frame = scene(project, shot, (45 - shot.start) / shot.duration,
                  image_path=root / project.id / image.path)
    frame.save(out / "shot-6-after.png")
    print(out / "shot-6-after.png")


if __name__ == "__main__":
    main()
