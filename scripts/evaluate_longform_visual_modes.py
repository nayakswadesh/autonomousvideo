"""Read-only chapter planning probe for the image-versus-designed choice contract.

This requests visual text planning only. It does not materialize media or save
changes to the project's approved spec or workflow state.
"""

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from autonomous_studio.config import Settings
from autonomous_studio.providers.longform_visuals import OpenAILongFormVisualPlanner
from autonomous_studio.providers.openai import OpenAIClient
from autonomous_studio.workflow import Studio


async def evaluate(project_id: str, chapter_order: int, root: Path) -> None:
    studio = Studio(root)
    project = studio.repo.load(project_id)
    if project.pipeline != "live":
        raise ValueError("Visual-mode probe requires a live project")
    chapter = next(
        item for item in project.spec.chapters if item.order == chapter_order
    )
    # Isolating chapter one makes it appear to be the last chapter. Disable only
    # the end-of-film close detector; the approved narration is unchanged.
    spec = project.spec.model_copy(update={
        "chapters": [chapter], "closing_contract_version": 1,
    })
    settings = Settings.environment().model_copy(update=project.live_models)
    settings.require_key()
    with TemporaryDirectory(prefix="visual-mode-probe-") as cache_dir:
        client = OpenAIClient(settings, Path(cache_dir))
        client.longform_creative_policy = project.longform_creative_policy
        planner = OpenAILongFormVisualPlanner(client)
        draft = await planner._plan_by_chapter(
            spec, project.longform_visual_policy, []
        )
    print(json.dumps({
        "project_id": project_id,
        "project_version": project.version,
        "chapter": chapter.id,
        "beats": [
            {
                "id": beat.id,
                "visual_type": beat.visual_type,
                "design_choice": (
                    f"{beat.design_family}/{beat.design_treatment}"
                    if beat.design_family else None
                ),
                "image_candidate": beat.mode_assessment.image_candidate,
                "image_fits": beat.mode_assessment.image_fits,
                "designed_candidate": beat.mode_assessment.designed_candidate,
                "designed_necessary": beat.mode_assessment.designed_necessary,
                "decision_reason": beat.mode_assessment.decision_reason,
            }
            for beat in draft.beats
        ],
    }, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("chapter_order", type=int)
    parser.add_argument("--root", type=Path, default=Path("projects/live"))
    args = parser.parse_args()
    asyncio.run(evaluate(args.project_id, args.chapter_order, args.root))


if __name__ == "__main__":
    main()
