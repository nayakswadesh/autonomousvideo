"""Read-only measured cue audit for a long-form project's planned visual beats."""

import sys
from pathlib import Path

from autonomous_studio.audio.alignment import phrase_start
from autonomous_studio.workflow import Studio


def main(project_id: str, root: Path) -> None:
    from autonomous_studio.rendering.longform import narration_windows

    studio = Studio(root)
    project = studio.repo.load(project_id)
    for chapter in project.spec.chapters:
        windows = {
            block.id: start
            for block, _, start, _ in narration_windows(chapter, project.longform_voices)
        }
        blocks = {block.id: block for block in chapter.narration_blocks}
        for block_id, block in blocks.items():
            beats = [
                beat for beat in chapter.visual_beats if block_id in beat.narration_block_ids
            ]
            if len(beats) < 2:
                continue
            ordered = sorted(
                (
                    phrase_start(
                        block.text, beat.narration_anchor,
                        project.longform_word_timings[block_id], offset=windows[block_id],
                    ),
                    beat,
                )
                for beat in beats
            )
            for (_, beat), (boundary, _) in zip(
                ordered[:-1], ordered[1:], strict=True
            ):
                for label, anchor in zip(
                    beat.overlay_text, beat.overlay_anchors, strict=True
                ):
                    at = phrase_start(
                        block.text, anchor, project.longform_word_timings[block_id],
                        offset=windows[block_id],
                    )
                    if at > boundary + 0.1:
                        print(
                            chapter.id, beat.id, repr(label),
                            f"cue={at:.2f}s next_beat={boundary:.2f}s",
                        )


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
