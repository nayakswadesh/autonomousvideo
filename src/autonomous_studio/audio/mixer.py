import os
import sys
import wave
from array import array
from math import isfinite, log10, sqrt
from pathlib import Path

from autonomous_studio.domain.models import VideoProject
from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.storage import Repository


def pcm_metrics(path: Path) -> dict:
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2:
            raise ValueError("QA requires 16-bit PCM")
        count, rate = source.getnframes(), source.getframerate()
        values = array("h", source.readframes(count))
    if sys.byteorder != "little":
        values.byteswap()
    peak = max(map(abs, values), default=0)
    rms = sqrt(sum(v * v for v in values) / max(1, len(values))) / 32768
    return {
        "duration": count / rate,
        "sample_peak_db": 20 * log10(max(peak / 32768, 1e-10)),
        "rms": rms,
        "clipped_samples": sum(abs(v) >= 32767 for v in values),
    }


def align_pcm_duration(path: Path, duration: float, maximum_adjustment: float = 0.25) -> float:
    """Correct small FFmpeg filter latency at the final 48 kHz PCM sample boundary."""
    with wave.open(str(path), "rb") as source:
        params = source.getparams()
        if (params.nchannels, params.sampwidth, params.framerate) != (1, 2, 48000):
            raise ValueError("chapter mix must be mono, 16-bit, 48 kHz PCM")
        expected = round(duration * params.framerate)
        difference = expected - params.nframes
        if abs(difference) / params.framerate > maximum_adjustment:
            raise ValueError("chapter audio differs too much to align safely")
        if difference == 0:
            return 0.0
        content = source.readframes(min(expected, params.nframes))
    temporary = path.with_suffix(".aligned.wav")
    with wave.open(str(temporary), "wb") as target:
        target.setparams(params)
        target.writeframes(content)
        if difference > 0:
            target.writeframes(b"\x00" * difference * params.sampwidth)
    os.replace(temporary, path)
    return difference / params.framerate


class Mixer:
    def __init__(self, repository: Repository, ffmpeg: FFmpeg):
        self.repo, self.ffmpeg = repository, ffmpeg

    def mix(self, project: VideoProject, output: Path) -> tuple[list[Path], dict]:
        brief = project.spec.brief
        policy, duration = brief.mix, brief.duration
        stems = []
        for kind in ("voice", "music", "sfx", "ambience"):
            assets = [a for a in project.spec.audio_assets if a.kind == kind]
            stem = output / f"{kind}.wav"
            if not assets:
                self.ffmpeg.run(
                    [
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=r=48000:cl=mono",
                        "-t",
                        duration,
                        "-c:a",
                        "pcm_s16le",
                        stem,
                    ]
                )
            else:
                inputs, filters, labels = [], [], []
                for index, asset in enumerate(assets):
                    path = self.repo.verify(project, asset.artifact)
                    metrics = pcm_metrics(path)
                    if abs(metrics["duration"] - asset.duration) > 0.03:
                        raise ValueError(f"audio duration mismatch: {asset.artifact.path}")
                    if asset.start + asset.duration > duration + 0.03:
                        raise ValueError("audio extends beyond video timeline")
                    if metrics["clipped_samples"]:
                        raise ValueError(f"clipped input audio: {asset.artifact.path}")
                    inputs.extend(["-i", path])
                    fade_in = min(policy.fade_in_ms / 1000, asset.duration / 2)
                    fade_out = min(policy.fade_out_ms / 1000, asset.duration / 2)
                    gain = getattr(policy, f"{kind}_gain_db")
                    filters.append(
                        f"[{index}:a]atrim=duration={asset.duration},asetpts=PTS-STARTPTS,"
                        f"aresample=48000,volume={gain}dB,"
                        f"afade=t=in:d={fade_in},afade=t=out:st={asset.duration - fade_out}:"
                        f"d={fade_out},adelay={round(asset.start * 1000)}:all=1,"
                        f"apad=whole_dur={duration},atrim=duration={duration}[a{index}]"
                    )
                    labels.append(f"[a{index}]")
                filters.append(
                    "".join(labels) + f"amix=inputs={len(assets)}:normalize=0:duration=longest,"
                    f"atrim=duration={duration}[stem]"
                )
                self.ffmpeg.run(
                    [
                        *inputs,
                        "-filter_complex",
                        ";".join(filters),
                        "-map",
                        "[stem]",
                        "-ar",
                        "48000",
                        "-ac",
                        "1",
                        "-c:a",
                        "pcm_s16le",
                        "-t",
                        duration,
                        stem,
                    ]
                )
            stems.append(stem)
        # Shot-level music direction is evaluated against timeline time, not random amplitudes.
        expression = "1"
        for shot in project.spec.shots:
            level = {"bed": 1, "rise": 1.25, "reduce": 0.4, "silence": 0}[shot.music_state]
            expression = (
                f"if(between(t,{shot.start},{shot.start + shot.duration}),{level},{expression})"
            )
        structure = "1"
        for beat in brief.music.structure:
            structure = f"if(gte(t,{beat.start}),{beat.energy},{structure})"
        expression = f"({expression})*({structure})"
        ducked = output / "music_ducked.wav"
        envelope = "1"
        if policy.music_ducking:
            gain = 10 ** (policy.duck_gain_db / 20)
            attack = policy.duck_attack_ms / 1000
            release = policy.duck_release_ms / 1000
            for asset in project.spec.audio_assets:
                if asset.kind != "voice":
                    continue
                start, end = asset.start, asset.start + asset.duration
                # Narration windows are authoritative. A single-input per-sample envelope
                # avoids platform-dependent sidechain EOF behavior and preserves the tail.
                window = (
                    f"if(lt(t,{start}),1,if(lt(t,{start + attack}),"
                    f"1-(1-{gain})*(t-{start})/{attack},if(lt(t,{end}),{gain},"
                    f"if(lt(t,{end + release}),{gain}+(1-{gain})*(t-{end})/{release},1))))"
                )
                envelope = f"min({envelope},{window})"
        self.ffmpeg.run(
            [
                "-i",
                stems[1],
                "-af",
                f"aeval='val(0)*({expression})*({envelope})'",
                "-t",
                duration,
                "-c:a",
                "pcm_s16le",
                ducked,
            ]
        )
        filters = ["[0:a][1:a][2:a][3:a]amix=inputs=4:normalize=0[combined]"]
        silence = "1"
        for shot in project.spec.shots:
            if shot.intentional_silence:
                silence = f"if(between(t,{shot.start},{shot.start + shot.duration}),0,{silence})"
        filters += [f"[combined]aeval='val(0)*({silence})'[final]"]
        raw = output / "mix_raw.wav"
        inputs = [part for stem in (stems[0], ducked, stems[2], stems[3]) for part in ("-i", stem)]
        self.ffmpeg.run(
            [
                *inputs,
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[final]",
                "-c:a",
                "pcm_s16le",
                raw,
            ]
        )
        measured = self.ffmpeg.loudness(raw, policy.target_loudness, policy.true_peak_db)
        loudnorm = (
            f"loudnorm=I={policy.target_loudness}:TP={policy.true_peak_db}:LRA=11:"
            f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
            f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
            f"offset={measured['target_offset']}:linear=true"
        )
        mixed = output / "mix.wav"
        normalization = ["-af", loudnorm] if isfinite(float(measured["input_i"])) else []
        self.ffmpeg.run(
            ["-i", raw, *normalization, "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", mixed]
        )
        metrics = pcm_metrics(mixed)
        metrics["loudness"] = self.ffmpeg.loudness(mixed)
        if abs(metrics["duration"] - duration) > 0.02:
            raise ValueError("final audio duration does not match timeline")
        metrics["speech_review"] = (
            "HUMAN REVIEW REQUIRED: AI-generated speech; listen for pronunciation and timing"
            if project.pipeline == "live"
            else "UNAVAILABLE: mock voice uses timing tones, not spoken narration"
        )
        if metrics["clipped_samples"]:
            raise ValueError("final mix clips")
        return [*stems, ducked, raw, mixed], metrics
