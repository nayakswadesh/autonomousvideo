"""Opt-in semantic long-form comparison; never saves the project or sends Telegram actions."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from autonomous_studio.config import Settings
from autonomous_studio.domain.semantic import SemanticDecisionBatch
from autonomous_studio.providers.openai import OpenAIClient
from autonomous_studio.providers.semantic_director import OpenAISemanticDirector
from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.rendering.longform import narration_windows, render_chapter
from autonomous_studio.storage import Repository
from autonomous_studio.storage.repository import write_json
from autonomous_studio.workflow.motion_planning import plan_motion_chapter
from autonomous_studio.workflow.semantic_integration import (
    apply_semantic_decisions,
)


async def run(args):
    repo = Repository(args.root)
    project = repo.load(args.project_id)
    if project.spec.brief.format != "youtube_longform":
        raise ValueError("semantic pilot requires a long-form project")
    original = (project.version, project.revision, project.state)
    chapters = (
        project.spec.chapters
        if args.chapters == "all"
        else [
            chapter
            for chapter in project.spec.chapters
            if chapter.id in set(args.chapters.split(","))
        ]
    )
    if not chapters or (
        args.chapters != "all" and len(chapters) != len(set(args.chapters.split(",")))
    ):
        raise ValueError("requested chapter IDs do not match the project")
    run_name = args.run_name or datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M%S")
    output = repo.project_dir(project.id) / "comparisons" / "semantic-production-pilot" / run_name
    if output.exists() and not args.resume:
        raise ValueError(f"comparison already exists; choose another run name: {output}")
    output.mkdir(parents=True, exist_ok=True)
    original_hash = {
        "project_id": project.id,
        "project_version": project.version,
        "revision": project.revision,
        "state": project.state.value,
        "script_unchanged": True,
        "external_actions": "none",
    }
    lock_path = output / "project-lock.json"
    if lock_path.is_file():
        if json.loads(lock_path.read_text(encoding="utf-8")) != original_hash:
            raise ValueError("comparison belongs to a different project revision or state")
    else:
        write_json(lock_path, original_hash)
    decisions_dir = args.decisions
    decision_sources = {}
    for item in args.decision_source:
        chapter_id, separator, source = item.partition("=")
        if not separator or chapter_id not in {chapter.id for chapter in chapters}:
            raise ValueError(
                "--decision-source must be CHAPTER_ID=JSON_PATH for a selected chapter"
            )
        decision_sources[chapter_id] = Path(source)
    needs_live_director = decisions_dir is None and any(
        chapter.id not in decision_sources
        and not (args.resume and (output / chapter.id / "semantic-decisions.json").is_file())
        for chapter in chapters
    )
    director = None
    if needs_live_director:
        settings = Settings.environment()
        settings.require_key()
        client = OpenAIClient(settings, repo.project_dir(project.id) / "provider-cache")
        director = OpenAISemanticDirector(client)
    chapter_files = []
    overview_path = output / "overview.json"
    overview = {
        item["chapter_id"]: item
        for item in (
            json.loads(overview_path.read_text(encoding="utf-8")) if overview_path.is_file() else []
        )
    }
    for chapter in chapters:
        chapter_output = output / chapter.id
        chapter_output.mkdir(parents=True, exist_ok=True)
        windows = narration_windows(chapter, project.longform_voices)
        speech_windows = [
            {
                "block_id": block.id,
                "start": start,
                "end": end,
                "words": project.longform_word_timings.get(block.id, []),
            }
            for block, _, start, end in windows
        ]
        base, base_report = plan_motion_chapter(
            project.model_dump(mode="json"),
            chapter.id,
            speech_windows,
            comparison_name=f"semantic-pilot-{chapter.id}",
            width=project.spec.brief.width,
            height=project.spec.brief.height,
            fps=project.spec.brief.fps,
        )
        write_json(chapter_output / "baseline-motion-plan.json", base.model_dump(mode="json"))
        write_json(chapter_output / "baseline-planner-report.json", base_report)
        if chapter.id in decision_sources:
            batch = SemanticDecisionBatch.model_validate_json(
                decision_sources[chapter.id].read_text(encoding="utf-8")
            )
        elif decisions_dir is not None:
            source = decisions_dir / f"{chapter.id}.json"
            batch = SemanticDecisionBatch.model_validate_json(source.read_text(encoding="utf-8"))
        elif args.resume and (chapter_output / "semantic-decisions.json").is_file():
            batch = SemanticDecisionBatch.model_validate_json(
                (chapter_output / "semantic-decisions.json").read_text(encoding="utf-8")
            )
        else:
            batch = await director.propose(
                project.model_dump(mode="json"), chapter.id, base, speech_windows
            )
        write_json(chapter_output / "semantic-decisions.json", batch.model_dump(mode="json"))
        plan, report = apply_semantic_decisions(
            project.model_dump(mode="json"), chapter.id, base, speech_windows, batch
        )
        write_json(chapter_output / "semantic-motion-plan.json", plan.model_dump(mode="json"))
        write_json(chapter_output / "decision-report.json", report)
        overview[chapter.id] = {
            "chapter_id": chapter.id,
            "applied": report["applied"],
            "decisions": report["decisions"],
        }
        if args.render:
            movie = chapter_output / "chapter.mp4"
            qa_path = chapter_output / "render-qa.json"
            motion_path = chapter_output / "motion-plan.json"
            reusable = (
                args.resume
                and movie.is_file()
                and qa_path.is_file()
                and motion_path.is_file()
                and json.loads(motion_path.read_text(encoding="utf-8"))
                == plan.model_dump(mode="json")
            )
            if not reusable:
                movie, qa = render_chapter(
                    repo,
                    project,
                    chapter,
                    project.longform_music,
                    chapter_output,
                    motion_plan_override=plan,
                )
                write_json(qa_path, qa)
            chapter_files.append(movie)
    write_json(overview_path, list(overview.values()))
    if args.render and len(chapter_files) > 1:
        ffmpeg = FFmpeg()
        playlist = output / "chapters.txt"
        playlist.write_text(
            "\n".join(
                "file '" + path.as_posix().replace("'", "'\\''") + "'" for path in chapter_files
            )
            + "\n",
            encoding="utf-8",
        )
        movie = output / ("full-review.mp4" if args.chapters == "all" else "chapter-review.mp4")
        ffmpeg.run(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                playlist,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                movie,
            ],
            timeout=900,
        )
        write_json(output / "movie-qa.json", ffmpeg.inspect(movie, timeout=900))
    current = repo.load(project.id)
    if (current.version, current.revision, current.state) != original:
        raise RuntimeError("project changed during pilot; comparison must be reviewed before reuse")
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id")
    parser.add_argument("--root", type=Path, default=Path("projects/live"))
    parser.add_argument("--chapters", default="ch2,ch3", help="comma-separated chapter IDs or all")
    parser.add_argument(
        "--decisions", type=Path, help="directory containing chN.json decision batches"
    )
    parser.add_argument(
        "--decision-source",
        action="append",
        default=[],
        help="override one chapter with CHAPTER_ID=JSON_PATH",
    )
    parser.add_argument(
        "--render", action="store_true", help="render MP4s after validating decisions"
    )
    parser.add_argument("--run-name", help="output directory name; default is a UTC timestamp")
    parser.add_argument("--resume", action="store_true", help="reuse an existing comparison run")
    asyncio.run(run(parser.parse_args()))
