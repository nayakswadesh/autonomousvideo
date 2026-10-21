"""Offline v0.5 demo: a complete full production with simulated providers and no approval."""

import asyncio
import json
from pathlib import Path

from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief


async def main():
    root = Path(__file__).resolve().parents[1]
    studio = Studio(root / "projects" / "v05-demo")
    project = studio.create(
        load_brief(root / "examples" / "earthquake-full.yaml"), review_policy="preview"
    )
    project = await studio.run(project.id)
    movie = studio.repo.verify(project, project.current_artifacts("prototype")[-1])
    qa = studio.repo.verify(project, project.current_artifacts("qa")[-1])
    print(
        json.dumps(
            {
                "project_id": project.id,
                "state": project.state,
                "production_mode": project.spec.brief.production_mode,
                "duration": project.spec.brief.duration,
                "shots": len(project.spec.shots),
                "rank_order": [
                    shot.rank for shot in project.spec.shots if shot.kind == "rank_reveal"
                ],
                "prototype": str(movie),
                "qa": str(qa),
                "review": "local simulated-provider checkpoint; no human decision recorded",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
