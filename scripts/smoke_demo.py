"""Developer demo: approvals below are explicitly simulated, never human decisions."""

import asyncio
import json
from pathlib import Path

from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief


async def main():
    root = Path(__file__).resolve().parents[1]
    studio = Studio(root / "projects" / "demo")
    project = studio.create(load_brief(root / "examples" / "earthquake-countdown.yaml"))
    for gate in ("story", "hero"):
        project = await studio.run(project.id)
        studio.decide(
            project.id,
            gate,
            "approve",
            actor="demo-harness (simulated)",
            message="Developer smoke test; not human editorial approval.",
        )
    project = await studio.render(project.id)
    movie = studio.repo.verify(project, project.current_artifacts("prototype")[0])
    print(
        json.dumps(
            {
                "project_id": project.id,
                "state": project.state,
                "prototype": str(movie),
                "root": str(studio.repo.root),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
