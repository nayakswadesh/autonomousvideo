"""Evaluate the current Shorts writer against saved research without mutating projects.

Usage: python scripts/evaluate_shorts_supported_wonder.py <project-id> [...]
The normal response cache is written under out/; live project directories are read-only.
"""

import asyncio
import json
import sys
from pathlib import Path

from autonomous_studio.config import Settings
from autonomous_studio.domain.models import Brief, VideoProject
from autonomous_studio.providers.openai import (
    NarrativeDraft,
    OpenAIClient,
    build_narrative_prompt,
    output_text,
    validate_narrative_draft,
)


async def evaluate(project_ids: list[str]) -> None:
    settings = Settings.environment().model_copy(update={"max_output_tokens": 6000})
    client = OpenAIClient(settings, Path("out/shorts-supported-wonder-eval/cache"))
    for project_id in project_ids:
        project_path = Path("projects/live") / project_id / "project.json"
        project = VideoProject.model_validate_json(project_path.read_text(encoding="utf-8"))
        brief = project.spec.brief
        if not isinstance(brief, Brief) or brief.production_mode != "full":
            raise ValueError(f"{project_id} is not a full Shorts project")
        prompt = build_narrative_prompt(brief, [])
        response = await client.response(
            settings.shorts_creative_model,
            prompt,
            schema=NarrativeDraft.model_json_schema(),
            reasoning_effort="medium",
        )
        draft = NarrativeDraft.model_validate_json(output_text(response))
        try:
            validate_narrative_draft(brief, draft, [])
            validation = "passed"
        except ValueError as exc:
            validation = f"failed: {exc}"
        print(
            json.dumps(
                {
                    "project_id": project_id,
                    "topic": brief.topic,
                    "model": response.get("model"),
                    "usage": response.get("usage"),
                    "validation": validation,
                    "hook": draft.hook,
                    "script": draft.narration_script,
                    "payoff": draft.payoff,
                },
                ensure_ascii=True,
            ),
            flush=True,
        )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Provide one or more saved project IDs")
    asyncio.run(evaluate(sys.argv[1:]))
