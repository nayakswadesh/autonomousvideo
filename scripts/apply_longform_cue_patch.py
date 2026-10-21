"""Apply a bounded visual-cue repair without changing approved narration or assets."""

import json
import sys
from pathlib import Path

from autonomous_studio.audio.alignment import phrase_start
from autonomous_studio.domain.models import LongFormSpec, State
from autonomous_studio.providers.longform_media import _editorial_card
from autonomous_studio.storage.repository import write_json
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.motion_planning import plan_motion_chapter


def main(project_id: str, root: Path, patch_path: Path) -> None:
    from autonomous_studio.rendering.longform import narration_windows

    patch = json.loads(patch_path.read_text(encoding="utf-8"))
    if patch.get("schema_version") != 1 or not patch.get("beats"):
        raise ValueError("cue patch needs schema_version 1 and nonempty beats")
    studio = Studio(root)
    with studio.repo.lock(project_id):
        project = studio.repo.load(project_id)
        if project.state != State.LONGFORM_CHAPTER_RENDERING:
            raise ValueError("cue patch requires a stopped chapter-rendering stage")
        if not project.last_error or "outside its scene" not in project.last_error:
            raise ValueError("cue patch requires a measured scene-boundary failure")
        spec = project.spec.model_copy(deep=True)
        beats = {
            beat.id: (chapter, beat)
            for chapter in spec.chapters for beat in chapter.visual_beats
        }
        if not set(patch["beats"]) <= set(beats):
            raise ValueError("cue patch references an unknown visual beat")
        allowed = {"narration_anchor", "overlay_text", "overlay_anchors"}
        changed_chapters = set()
        changed_code_assets = set()
        for beat_id, fields in patch["beats"].items():
            if not fields or not set(fields) <= allowed:
                raise ValueError("cue patch may alter only spoken anchors and overlay labels")
            chapter, beat = beats[beat_id]
            updated = beat.model_copy(update=fields)
            block = next(
                block for block in chapter.narration_blocks
                if block.id == updated.narration_block_ids[0]
            )
            start = next(
                start for item, _, start, _ in narration_windows(chapter, project.longform_voices)
                if item.id == block.id
            )
            for phrase in [updated.narration_anchor, *updated.overlay_anchors]:
                phrase_start(
                    block.text, phrase, project.longform_word_timings[block.id], offset=start
                )
            if (
                "overlay_text" in fields
                and len(updated.overlay_text) != len(updated.overlay_anchors)
            ):
                raise ValueError("each repaired overlay requires one spoken anchor")
            chapter.visual_beats[chapter.visual_beats.index(beat)] = updated
            changed_chapters.add(chapter.id)
            asset = next(a for a in project.longform_visual_plan.assets if a.id == beat.asset_ref)
            if "overlay_text" in fields and asset.kind == "code_rendered":
                changed_code_assets.add(asset.id)
        project.spec = LongFormSpec.model_validate(spec.model_dump(mode="json"))
        snapshot = project.model_dump(mode="json")
        for chapter in project.spec.chapters:
            if chapter.id not in changed_chapters:
                continue
            windows = [
                {
                    "block_id": block.id, "start": start, "end": end,
                    "words": project.longform_word_timings[block.id],
                }
                for block, _, start, end in narration_windows(chapter, project.longform_voices)
            ]
            plan_motion_chapter(
                snapshot, chapter.id, windows, width=project.spec.brief.width,
                height=project.spec.brief.height, fps=project.spec.brief.fps,
            )
        output = studio.repo.attempt_dir(project, "motion-cue-repair")
        for asset_id in sorted(changed_code_assets):
            asset = next(a for a in project.longform_visual_plan.assets if a.id == asset_id)
            image = output / f"{asset_id}.png"
            _editorial_card(project, asset, image)
            artifact = studio.repo.artifact(
                project, image, "longform_visual", provider="local", model="lf-v0.4-cue-repair",
                width=1920, height=1080,
            )
            project.longform_media[asset_id] = artifact
            project.artifacts.append(artifact)
        for chapter_id in changed_chapters:
            project.longform_chapters.pop(chapter_id, None)
            project.longform_chapter_hashes.pop(chapter_id, None)
        copy = output / "cue-repair.json"
        write_json(copy, patch)
        project.artifacts.append(studio.repo.artifact(project, copy, "longform_motion_cue_repair"))
        project.last_error = project.last_error_code = None
        studio.repo.save(
            project, "longform_motion_cues_repaired",
            {
                "version": project.version, "beat_ids": sorted(patch["beats"]),
                "chapters": sorted(changed_chapters),
                "narration_changed": False, "generated_assets_reused": True,
            },
        )
    print("repaired", sorted(patch["beats"]), "state", project.state)


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
