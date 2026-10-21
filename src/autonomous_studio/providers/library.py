from pathlib import Path

from autonomous_studio.domain.models import MusicAsset
from autonomous_studio.rendering.ffmpeg import FFmpeg


class LibraryMusic:
    """Resolve an immutable project asset, never a network URL or arbitrary filesystem path."""

    def __init__(self, repo, ffmpeg=None):
        self.repo = repo
        self.ffmpeg = ffmpeg or FFmpeg()

    async def create_or_select(self, project, spec, output: Path):
        source = next((a for a in project.artifacts if a.id == spec.artifact_ref), None)
        if source is None or source.role != "music_source":
            raise ValueError("library music references missing music_source artifact")
        if source.provenance.license_category != "licensed" or not source.provenance.attribution:
            raise ValueError("UNKNOWN — DO NOT PUBLISH: music license/attribution missing")
        path = self.repo.verify(project, source)
        duration = project.spec.brief.duration
        self.ffmpeg.run(
            [
                "-ss",
                spec.trim_start,
                "-i",
                path,
                "-t",
                duration,
                "-vn",
                "-ac",
                1,
                "-ar",
                48000,
                "-c:a",
                "pcm_s16le",
                "-map_metadata",
                "-1",
                output,
            ]
        )
        import wave

        with wave.open(str(output), "rb") as wav:
            measured = wav.getnframes() / wav.getframerate()
        if abs(measured - duration) > 0.01:
            raise ValueError(
                "library music is shorter than the requested excerpt; choose a new trim"
            )
        artifact = self.repo.artifact(
            project,
            output,
            "music",
            provider="local-library",
            model="v0.7",
            duration=measured,
        )
        artifact.provenance = source.provenance.model_copy(deep=True)
        artifact.input_reference = source.id
        return MusicAsset(artifact=artifact, start=0, duration=measured, mood=spec.mood)
