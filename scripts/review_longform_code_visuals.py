"""Render existing code-shot checkpoints without changing project state or paid media."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

from autonomous_studio.workflow import Studio


def main() -> None:
    # Import after Studio: workflow initialization registers the renderer first.
    from autonomous_studio.rendering.ffmpeg import FFmpeg
    from autonomous_studio.rendering.longform import narration_windows, render_chapter
    from autonomous_studio.rendering.longform_motion import frame_for_shot
    from autonomous_studio.workflow.motion_planning import plan_motion_chapter

    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("--root", type=Path, default=Path("projects/live"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--shot", action="append", default=[])
    parser.add_argument("--overrides", type=Path)
    parser.add_argument("--render-chapter")
    parser.add_argument("--render-all", action="store_true")
    args = parser.parse_args()
    project = Studio(args.root).repo.load(args.project_id)
    if args.overrides:
        overrides = json.loads(args.overrides.read_text(encoding="utf-8"))
        beats = {
            beat.id: beat for chapter in project.spec.chapters for beat in chapter.visual_beats
        }
        for beat_id, fields in overrides.items():
            if beat_id not in beats:
                raise ValueError(f"Unknown visual beat: {beat_id}")
            for field, value in fields.items():
                if field not in {
                    "screen_title",
                    "display_layout",
                    "overlay_text",
                    "overlay_anchors",
                }:
                    raise ValueError(f"Unsupported visual override field: {field}")
                setattr(beats[beat_id], field, value)
    cards = []
    reports = []
    for chapter in project.spec.chapters:
        windows = [
            {
                "block_id": block.id,
                "start": start,
                "end": end,
                "words": project.longform_word_timings[block.id],
            }
            for block, _, start, end in narration_windows(chapter, project.longform_voices)
        ]
        plan, report = plan_motion_chapter(
            project.model_dump(mode="json"), chapter.id, windows, width=960, height=540
        )
        reports.append(
            {
                "chapter_id": chapter.id,
                "quality_flags": report["quality_flags"],
            }
        )
        for model in plan.shots:
            if args.shot and model.id not in args.shot:
                continue
            if model.kind not in {
                "narration_sequence",
                "fact_reveal",
                "relationship_map",
                "concept_contrast",
                "evidence_gap",
            }:
                continue
            shot = model.model_dump(mode="json")
            checkpoints = [
                min(shot["end"] - 0.05, shot["start"] + 0.08),
                min(shot["end"] - 0.05, shot["cues"][0]["at_seconds"] + 0.55),
                min(shot["end"] - 0.05, shot["cues"][-1]["at_seconds"] + 0.55),
            ]
            strip = Image.new("RGB", (960 * 3, 590), "#101923")
            draw = ImageDraw.Draw(strip)
            for index, now in enumerate(checkpoints):
                strip.paste(frame_for_shot(shot, now, (960, 540), {}), (960 * index, 50))
                draw.text(
                    (960 * index + 15, 12), f"{chapter.id} / {model.id} / {now:.1f}s", fill="white"
                )
            cards.append(strip)
    if not cards:
        raise ValueError("No matching code-rendered shots")
    sheet = Image.new("RGB", (2880, 590 * len(cards)), "#101923")
    for row, card in enumerate(cards):
        sheet.paste(card, (0, 590 * row))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.output)
    print(args.output)
    args.output.with_suffix(".json").write_text(
        json.dumps({"project_id": project.id, "chapters": reports}, indent=2),
        encoding="utf-8",
    )
    if args.render_chapter or args.render_all:
        selected = (
            project.spec.chapters
            if args.render_all
            else [chapter for chapter in project.spec.chapters if chapter.id == args.render_chapter]
        )
        if not selected:
            raise ValueError(f"Unknown chapter: {args.render_chapter}")
        movies = []
        for chapter in selected:
            movie, _ = render_chapter(
                Studio(args.root).repo,
                project,
                chapter,
                project.longform_music,
                args.output.parent / f"{chapter.id}-preview",
            )
            movies.append(movie)
            print(movie, flush=True)
        if args.render_all:
            playlist = args.output.parent / "preview-chapters.txt"
            playlist.write_text(
                "\n".join(f"file '{movie.resolve().as_posix()}'" for movie in movies) + "\n",
                encoding="utf-8",
            )
            review = args.output.parent / "step1-review.mp4"
            ffmpeg = FFmpeg()
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
                    review,
                ],
                timeout=900,
            )
            print(review, ffmpeg.inspect(review, timeout=900), flush=True)


if __name__ == "__main__":
    main()
