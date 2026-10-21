"""Render one saved semantic beat into a new review cut without changing project state."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from pydantic import TypeAdapter

from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.rendering.longform_motion import _caption, _captions
from autonomous_studio.rendering.semantic_diagrams import Scene, render_frame, validate_scene_layout


def _plan(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--prior-plan", type=Path, action="append", default=[])
    parser.add_argument("--captions", type=Path, required=True)
    parser.add_argument("--beat", required=True)
    parser.add_argument("--clip", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.output.exists() or args.clip.exists():
        raise ValueError("review output and replacement clip must be new paths")
    plan = _plan(args.plan)
    fps, width, height = plan["fps"], plan["width"], plan["height"]
    if not isinstance(fps, int) or fps <= 0:
        raise ValueError("chapter plan needs an integer positive frame rate")
    shot = next((item for item in plan["shots"] if item["id"] == args.beat), None)
    if shot is None or shot["kind"] != "semantic_diagram":
        raise ValueError("beat must be a saved semantic diagram in the chapter plan")
    scene = TypeAdapter(Scene).validate_python(shot["scene"])
    validate_scene_layout(scene)
    prefix_frames = 0
    for path in args.prior_plan:
        previous = _plan(path)
        if previous["fps"] != fps or (previous["width"], previous["height"]) != (width, height):
            raise ValueError("prior chapter plan uses different video settings")
        prefix_frames += round(previous["shots"][-1]["end"] * fps)

    # Production chooses a shot with `now >= start`, frame by frame. Match that
    # boundary exactly so the new cut retains the original number of frames.
    local_start = math.ceil(shot["start"] * fps - 1e-8)
    local_end = math.ceil(shot["end"] * fps - 1e-8)
    frame_count = local_end - local_start
    global_start, global_end = prefix_frames + local_start, prefix_frames + local_end
    if frame_count <= 0 or abs(scene.duration - (shot["end"] - shot["start"])) > 0.02:
        raise ValueError("saved scene and beat durations disagree")
    captions = _captions(args.captions)
    ffmpeg = FFmpeg()
    source_info = ffmpeg.inspect(args.source)
    if not source_info["has_audio"] or not source_info["has_video"]:
        raise ValueError("source review needs both picture and audio")
    if (source_info["width"], source_info["height"]) != (width, height):
        raise ValueError("source review dimensions differ from the saved plan")
    if source_info["duration"] < global_end / fps - 0.2:
        raise ValueError("source review is shorter than the requested beat")

    args.clip.parent.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def replacement_frames():
        for frame_index in range(local_start, local_end):
            now = frame_index / fps
            frame = render_frame(
                scene, now - shot["start"], size=(width, height), presentation="production",
            )
            subtitle = next(
                (text for start, end, text in captions if start <= now < end), None,
            )
            if subtitle:
                _caption(frame, subtitle)
            yield frame

    ffmpeg.encode(replacement_frames(), width, height, fps, args.clip)
    filter_graph = (
        f"[0:v]trim=end_frame={global_start},setpts=PTS-STARTPTS[head];"
        "[1:v]setpts=PTS-STARTPTS[middle];"
        f"[0:v]trim=start_frame={global_end},setpts=PTS-STARTPTS[tail];"
        "[head][middle][tail]concat=n=3:v=1:a=0[v]"
    )
    ffmpeg.run(
        [
            "-i", args.source, "-i", args.clip,
            "-filter_complex", filter_graph,
            "-map", "[v]", "-map", "0:a:0",
            "-c:v", "libx264", "-threads", "2", "-preset", "veryfast",
            "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-map_metadata", "-1", "-movflags", "+faststart", args.output,
        ],
        timeout=1200,
    )
    result = ffmpeg.inspect(args.output)
    if (
        not result["decodable"] or not result["has_audio"] or not result["has_video"]
        or (result["width"], result["height"]) != (width, height)
        or abs(result["duration"] - source_info["duration"]) > 0.1
    ):
        raise ValueError(f"review cut failed video/audio QA: {result}")
    print(json.dumps({
        "output": str(args.output), "beat": args.beat,
        "replaced_frames": [global_start, global_end], "qa": result,
    }, indent=2))


if __name__ == "__main__":
    main()
