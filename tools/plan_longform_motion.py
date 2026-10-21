"""Opt-in, offline story-aware chapter planning and comparison rendering."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from autonomous_studio.workflow.motion_planning import plan_motion_chapter
from tools.render_longform_motion_comparison import render


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id")
    parser.add_argument("chapter_id")
    parser.add_argument("--projects-root", type=Path, default=Path("projects/live"))
    parser.add_argument("--name", default=None, help="Comparison package name")
    parser.add_argument(
        "--render", action="store_true", help="Encode comparison videos after planning"
    )
    args = parser.parse_args()

    root = args.projects_root.resolve()
    project_dir = (root / args.project_id).resolve()
    if not project_dir.is_relative_to(root):
        raise ValueError("Project must remain under projects root")
    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    if project["id"] != args.project_id:
        raise ValueError("Project ID disagrees with project data")
    chapter_ref = project["longform_chapters"][args.chapter_id]
    chapter_path = (project_dir / chapter_ref["path"]).resolve()
    if not chapter_path.is_relative_to(project_dir):
        raise ValueError("Chapter must remain under project directory")
    qa = json.loads((chapter_path.parent / "qa.json").read_text(encoding="utf-8"))
    plan, report = plan_motion_chapter(
        project,
        args.chapter_id,
        qa["speech_windows"],
        comparison_name=args.name,
    )
    output = project_dir / "comparisons" / plan.comparison_name
    output.mkdir(parents=True, exist_ok=True)
    plan_path = output / "motion-plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (output / "planner-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(plan_path)
    if args.render:
        print(render(plan_path, root))


if __name__ == "__main__":
    main()
