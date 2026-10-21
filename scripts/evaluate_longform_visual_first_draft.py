"""Read-only, bounded Luna visual-plan evaluation against a saved approved script.

Never runs a workflow stage, changes a project, or grants an approval. The provider
cache and report live in a separate evaluation directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from autonomous_studio.config import Settings
from autonomous_studio.domain.longform_feedback import remove_image_quotas
from autonomous_studio.domain.longform_policy import LongFormCreativePolicy
from autonomous_studio.domain.models import LongFormSpec
from autonomous_studio.providers.longform_visuals import (
    OpenAILongFormVisualPlanner,
    materialize_visual_plan,
)
from autonomous_studio.providers.longform_media import longform_image_payload
from autonomous_studio.providers.openai import OpenAIClient
from autonomous_studio.storage.repository import Repository, write_json
from autonomous_studio.workflow.visual_reveal_timing import (
    validate_saved_speech_against_visual_plan,
)


class SavedResponseReplay:
    """Revalidate previously billed responses without making another API request."""

    def __init__(self, settings, report_path: Path):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.settings = settings
        self.longform_creative_policy = LongFormCreativePolicy()
        self._responses = [
            (item["cache_path"], json.loads(Path(item["cache_path"]).read_text(encoding="utf-8")))
            for item in report["requests"]
        ]
        self.trace = []

    async def response(self, model, _prompt, **_kwargs):
        if not self._responses:
            raise RuntimeError("saved response replay exhausted; no API call permitted")
        path, response = self._responses.pop(0)
        self.trace.append({
            "requested_model": model,
            "response": response,
            "cache_hit": True,
            "cache_path": path,
        })
        return response

    def reject_last(self, reason):
        self.trace[-1]["rejected_reason"] = reason


async def evaluate(args) -> int:
    def progress(stage):
        if args.verbose:
            print(f"evaluation: {stage}", flush=True)

    progress("load project")
    repo = Repository(args.root)
    project = repo.load(args.project_id)
    if not isinstance(project.spec, LongFormSpec):
        raise ValueError("evaluation requires a saved long-form project")
    chapter = next(
        (item for item in project.spec.chapters if item.order == args.chapter_order), None
    )
    if chapter is None or not chapter.narration_blocks:
        raise ValueError("selected chapter needs an existing approved script")
    spec = project.spec.model_copy(deep=True)
    spec.chapters = [chapter.model_copy(deep=True)]
    if args.unit_v2:
        spec.chapters[0].spoken_unit_version = 2
    spec.treatment_summary = remove_image_quotas(spec.treatment_summary)
    spec.chapters[0].purpose = remove_image_quotas(spec.chapters[0].purpose)
    settings = Settings.environment().model_copy(
        update={**project.live_models, "visual_plan_model": "gpt-6-luna"}
    )
    if args.replay_report is None:
        settings.require_key()
    output = args.output / f"{args.project_id}-ch{args.chapter_order}"
    output.mkdir(parents=True, exist_ok=True)
    client = (
        SavedResponseReplay(settings, args.replay_report)
        if args.replay_report is not None
        else OpenAIClient(settings, output / "provider-cache")
    )
    client.longform_creative_policy = LongFormCreativePolicy()
    report = {
        "project_id": project.id,
        "project_version_read_only": project.version,
        "chapter_id": chapter.id,
        "spoken_unit_version": spec.chapters[0].spoken_unit_version,
        "model": "gpt-6-luna",
        "creative_repairs": 0,
        "baseline_beats": len(chapter.visual_beats),
    }
    try:
        progress("plan from saved script")
        draft = await OpenAILongFormVisualPlanner(client)._plan_by_chapter(
            spec,
            project.longform_visual_policy,
            [],
            original_chapters=project.spec.chapters,
        )
        progress("materialize visual plan")
        write_json(output / "first-draft.json", draft.model_dump(mode="json"))
        proposed_spec, visual_plan = materialize_visual_plan(
            spec,
            draft,
            project.longform_visual_policy,
            {},
            image_model=project.live_models.get("image_model", "gpt-image-2"),
            authentic_failures={
                asset.id: "offline evaluation: Commons discovery not run"
                for asset in draft.assets
                if asset.kind == "authentic_image"
            },
            allow_partial=True,
        )
        report["materialized_pass"] = True
        progress("validate saved TTS")
        candidate = project.model_copy(deep=True)
        candidate.spec = proposed_spec
        candidate.longform_visual_plan = visual_plan
        candidate.longform_creative_policy = LongFormCreativePolicy()
        image_payloads = {
            asset.id: longform_image_payload(candidate, asset, settings)
            for asset in visual_plan.assets
            if asset.kind == "generated_image"
        }
        write_json(output / "final-image-prompts.json", image_payloads)
        report["planned_generated_images"] = len(image_payloads)
        validate_saved_speech_against_visual_plan(candidate, proposed_spec, visual_plan)
        report["saved_tts_preflight_pass"] = True
        progress("complete")
        report.update(
            passed=True,
            proposed_beats=len(draft.beats),
            designed_beats=sum(
                beat.visual_type not in {"authentic_image", "generated_image"}
                for beat in draft.beats
            ),
            image_beats=sum(
                beat.visual_type in {"authentic_image", "generated_image"}
                for beat in draft.beats
            ),
        )
    except Exception as exc:
        report.update(passed=False, error_type=type(exc).__name__, error=str(exc)[:2000])
    finally:
        report["requests"] = [
            {
                "model": item.get("requested_model"),
                "chapter_id": item.get("chapter_id"),
                "input_tokens": (item.get("response", {}).get("usage") or {}).get(
                    "input_tokens"
                ),
                "output_tokens": (item.get("response", {}).get("usage") or {}).get(
                    "output_tokens"
                ),
                "cache_hit": item.get("cache_hit", False),
                "cache_path": item.get("cache_path"),
            }
            for item in client.trace
        ]
        report["total_input_tokens"] = sum(
            item["input_tokens"] or 0 for item in report["requests"]
        )
        report["total_output_tokens"] = sum(
            item["output_tokens"] or 0 for item in report["requests"]
        )
        write_json(output / "report.json", report)
        print(json.dumps({key: value for key, value in report.items() if key != "requests"}))
    return 0 if report["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id")
    parser.add_argument("--chapter-order", type=int, default=1)
    parser.add_argument("--root", type=Path, default=Path("projects/live"))
    parser.add_argument("--output", type=Path, default=Path("out/evals/longform-visual"))
    parser.add_argument(
        "--replay-report", type=Path,
        help="Replay the two saved provider responses offline; never call OpenAI",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--unit-v2", action="store_true",
        help="Evaluate clause-level units on a copy of a legacy approved script",
    )
    return asyncio.run(evaluate(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
