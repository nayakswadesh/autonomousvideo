from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


class FFmpeg:
    def __init__(self, executable: str | None = None):
        candidate = executable or os.environ.get("STUDIO_FFMPEG") or shutil.which("ffmpeg")
        if not candidate:
            import imageio_ffmpeg

            candidate = imageio_ffmpeg.get_ffmpeg_exe()
        self.executable = str(candidate)

    def command(self, args):
        return [
            self.executable,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-filter_threads",
            "1",
            "-filter_complex_threads",
            "1",
            *map(str, args),
        ]

    def run(self, args, timeout=180):
        try:
            result = subprocess.run(
                self.command(args),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"FFmpeg unavailable or timed out: {exc}") from exc
        if result.returncode:
            raise RuntimeError(f"FFmpeg failed: {result.stderr[-4000:]}")
        return result

    def encode(self, frames, width, height, fps, output: Path):
        args = [
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgb24",
            "-video_size",
            f"{width}x{height}",
            "-framerate",
            fps,
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            "-flags:v",
            "+bitexact",
            output,
        ]
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                self.command(args), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors
            )
            try:
                for frame in frames:
                    process.stdin.write(frame.tobytes())
                process.stdin.close()
                code = process.wait(timeout=180)
                if code:
                    errors.seek(0)
                    raise RuntimeError(errors.read().decode("utf-8", "replace")[-4000:])
            except BaseException:
                if process.poll() is None:
                    process.kill()
                process.wait()
                raise

    def mux(self, video: Path, audio: Path, output: Path):
        self.run(
            [
                "-i",
                video,
                "-i",
                audio,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-threads",
                "1",
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                "-flags:a",
                "+bitexact",
                "-movflags",
                "+faststart",
                "-shortest",
                output,
            ]
        )

    def inspect(self, path: Path, *, timeout=180) -> dict:
        # Decode the whole container; works even when a separate ffprobe isn't installed.
        result = self.run(
            [
                "-v",
                "info",
                "-xerror",
                "-i",
                path,
                "-map",
                "0:v:0?",
                "-map",
                "0:a:0?",
                "-f",
                "null",
                "-",
            ],
            timeout=timeout,
        )
        duration = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
        video = re.search(r"Video: [^\n]+?\b(\d{2,5})x(\d{2,5})\b", result.stderr)
        return {
            "decodable": True,
            "has_audio": "Audio:" in result.stderr,
            "has_video": bool(video),
            "width": int(video[1]) if video else None,
            "height": int(video[2]) if video else None,
            "duration": (int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3]))
            if duration
            else None,
        }

    def loudness(self, path: Path, target=-16, peak=-1.5) -> dict:
        result = self.run(
            [
                "-i",
                path,
                "-af",
                f"loudnorm=I={target}:TP={peak}:LRA=11:print_format=json",
                "-f",
                "null",
                "-",
            ]
        )
        match = re.search(r'\{\s*"input_i".*?\}', result.stderr, re.S)
        if not match:
            raise RuntimeError("FFmpeg did not return loudness measurements")
        return json.loads(match[0])
