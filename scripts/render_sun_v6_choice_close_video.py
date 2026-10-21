"""Replace only the Sun v6 closing scene in an isolated review variant.

The approved project state, source video, narration and soundtrack remain intact.
"""

from __future__ import annotations

import json
from pathlib import Path

from autonomous_studio.domain.models import VideoProject
from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.rendering.semantic_diagrams import EditorialCloseScene, render_frame
from autonomous_studio.workflow.semantic_integration import align_scene_to_measured_speech
from autonomous_studio.rendering.longform import narration_windows


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "projects/live/f2230c84373c"
SOURCE = PROJECT / "review-video-v006-a275c1f0.mp4"
MOTION = PROJECT / "artifacts/v006-semantic-chapter-3-760597eb/motion-plan.json"
DECISIONS = PROJECT / "artifacts/v006-semantic-chapter-3-760597eb/decisions.json"
PROJECT_STATE = PROJECT / "project.json"
OUTPUT = PROJECT / "comparisons/choice-close-v6"
FPS = 24
SIZE = (1920, 1080)
# Verified against the three v6 chapter durations and the 6,325-frame source.
GLOBAL_CLOSING_FRAME = 5963
SOURCE_FRAMES = 6325


def main() -> None:
    plan = json.loads(MOTION.read_text(encoding="utf-8"))
    shot = next(item for item in plan["shots"] if item["id"] == "ch3-beat10")
    closing_frames = round((shot["end"] - shot["start"]) * FPS)
    if closing_frames != SOURCE_FRAMES - GLOBAL_CLOSING_FRAME:
        raise ValueError("saved closing duration no longer matches the source video")
    decisions = json.loads(DECISIONS.read_text(encoding="utf-8"))
    saved = next(item for item in decisions["decisions"]
                 if item["beat_id"] == "ch3-beat10")
    scene_data = json.loads(saved["scene_json"])
    scene_data["response_options"] = ["Meteorite dating", "Solar neutrinos"]
    scene = EditorialCloseScene.model_validate(scene_data)
    project = VideoProject.model_validate_json(PROJECT_STATE.read_text(encoding="utf-8"))
    chapter = project.spec.chapters[-1]
    closing_block = next(block for block in chapter.narration_blocks
                         if block.id == "chapter-3-block-3")
    window = next(
        {"start": start, "end": end,
         "words": project.longform_word_timings[block.id]}
        for block, _, start, end in narration_windows(chapter, project.longform_voices)
        if block.id == closing_block.id
    )
    scene, _ = align_scene_to_measured_speech(
        scene, scene.narration, window, shot["start"], shot["end"],
    )
    if len(scene.response_option_ats) != 2:
        raise ValueError("both closing choices need measured entrances")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    replacement = OUTPUT / "closing-video-v006-r2-timed.mp4"
    result = OUTPUT / "review-video-v006-choice-close-r2-timed.mp4"
    (OUTPUT / "measured-choice-times.json").write_text(json.dumps({
        "response_options": scene.response_options,
        "response_option_ats": scene.response_option_ats,
        "actions": [action.model_dump(mode="json") for action in scene.actions],
    }, indent=2), encoding="utf-8")
    ffmpeg = FFmpeg()
    ffmpeg.encode(
        (render_frame(scene, frame / FPS, size=SIZE, presentation="production")
         for frame in range(closing_frames)),
        *SIZE, FPS, replacement,
    )
    ffmpeg.run([
        "-i", SOURCE, "-i", replacement,
        "-filter_complex",
        f"[0:v]trim=end_frame={GLOBAL_CLOSING_FRAME},setpts=PTS-STARTPTS[head];"
        "[1:v]setpts=PTS-STARTPTS[tail];"
        "[head][tail]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-map", "0:a:0",
        "-c:v", "libx264", "-threads", "1", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-map_metadata", "-1",
        result,
    ], timeout=600)
    print(result)


if __name__ == "__main__":
    main()
