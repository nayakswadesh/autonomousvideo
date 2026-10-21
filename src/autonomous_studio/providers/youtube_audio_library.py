"""Import operator-downloaded YouTube Audio Library tracks into the local catalog."""

import os
import re
import shutil
from pathlib import Path

import yaml
from pydantic import Field, model_validator

from autonomous_studio.domain.models import Model, Provenance, now
from autonomous_studio.providers.music_catalog import CatalogTrack, MusicCatalog, load_catalog
from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.storage.repository import digest
from autonomous_studio.workflow.presentation import local_input

YOUTUBE_AUDIO_LIBRARY_HELP = "https://support.google.com/youtube/answer/3376882"
YOUTUBE_AUDIO_LIBRARY_SOURCE = "https://www.youtube.com/audiolibrary"
ALLOWED_TAGS = {"reflective", "tense", "uplifting", "neutral", "ambient"}


class YouTubeTrack(Model):
    file: str
    title: str = Field(min_length=1, max_length=200)
    artist: str = Field(min_length=1, max_length=200)
    genre: str = Field(min_length=1, max_length=100)
    moods: list[str] = Field(min_length=1, max_length=8)
    license_type: str = Field(pattern=r"^(standard|creative_commons)$")
    attribution: str = ""
    energy: str = Field(default="medium", pattern=r"^(low|medium|high)$")
    tempo_bpm: int | None = Field(default=None, ge=30, le=240)
    selection_tags: list[str] = Field(default_factory=list, max_length=5)
    id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{1,49}$")

    @model_validator(mode="after")
    def license_requirements(self):
        if self.license_type == "creative_commons" and not self.attribution.strip():
            raise ValueError("Creative Commons tracks require YouTube's exact attribution text")
        if not set(self.selection_tags) <= ALLOWED_TAGS:
            raise ValueError("selection_tags contain unsupported values")
        return self


class YouTubeImportManifest(Model):
    schema_version: int = 1
    tracks: list[YouTubeTrack] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def version(self):
        if self.schema_version != 1:
            raise ValueError("unsupported YouTube Audio Library import schema")
        return self


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:50]
    if len(result) < 2:
        raise ValueError("track title/artist cannot produce a stable catalog ID")
    return result


def _inferred_tags(track: YouTubeTrack) -> list[str]:
    words = " ".join([track.genre, *track.moods]).casefold()
    mapping = {
        "reflective": ("calm", "sad", "romantic", "reflective", "cinematic"),
        "tense": ("dark", "dramatic", "suspense", "mysterious", "angry"),
        "uplifting": ("happy", "inspirational", "bright", "positive", "hopeful"),
        "ambient": ("ambient", "calm", "dreamy", "mysterious", "cinematic"),
        "neutral": ("neutral", "documentary", "corporate", "acoustic"),
    }
    tags = [tag for tag, clues in mapping.items() if any(clue in words for clue in clues)]
    if track.energy == "high" and "uplifting" not in tags and "tense" not in tags:
        tags.append("uplifting")
    if track.energy == "low" and "ambient" not in tags:
        tags.append("ambient")
    return tags[:5] or ["neutral"]


def _write_catalog(path: Path, catalog: MusicCatalog):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(catalog.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def import_batch(manifest_path: Path, catalog_path: Path, ffmpeg=None) -> list[CatalogTrack]:
    if manifest_path.stat().st_size > 1_000_000:
        raise ValueError("music import manifest exceeds 1 MB")
    manifest = YouTubeImportManifest.model_validate(
        yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    )
    catalog = load_catalog(catalog_path) if catalog_path.exists() else MusicCatalog()
    existing_by_id = {track.id: track for track in catalog.tracks}
    existing_ids = {track.id for track in catalog.tracks}
    existing_hashes = {track.sha256 for track in catalog.tracks}
    prepared = []
    media = ffmpeg or FFmpeg()
    base = manifest_path.resolve().parent
    destination_root = catalog_path.resolve().parent / "assets"
    destination_root.mkdir(parents=True, exist_ok=True)
    created_paths = []
    try:
        for supplied in manifest.tracks:
            source = local_input(base, supplied.file)
            if source.suffix.casefold() not in {".mp3", ".m4a", ".wav"}:
                raise ValueError(f"unsupported audio format: {source.name}")
            track_id = supplied.id or _slug(f"{supplied.title}-{supplied.artist}")
            checksum = digest(source)
            if track_id in existing_ids:
                if existing_by_id[track_id].sha256 == checksum:
                    continue
                raise ValueError(f"catalog track ID has different audio: {track_id}")
            if checksum in existing_hashes:
                raise ValueError(f"audio is already present in the catalog: {source.name}")
            inspection = media.inspect(source)
            if not inspection["has_audio"] or inspection["has_video"]:
                raise ValueError(f"music import must be an audio-only file: {source.name}")
            duration = inspection["duration"]
            if not duration or duration < 60:
                raise ValueError(f"music track must be at least 60 seconds: {source.name}")
            destination = destination_root / f"{track_id}{source.suffix.casefold()}"
            evidence = destination_root / f"{track_id}-license.md"
            if destination.exists() or evidence.exists():
                raise ValueError(f"catalog destination already exists: {track_id}")
            shutil.copyfile(source, destination)
            created_paths.append(destination)
            attribution_required = supplied.license_type == "creative_commons"
            credit = supplied.attribution.strip() or (
                f"'{supplied.title}' by {supplied.artist}. Source: YouTube Audio Library "
                "(attribution not required)."
            )
            imported_at = now()
            created_paths.append(evidence)
            evidence.write_text(
                "\n".join(
                    [
                        f"# {supplied.title} — YouTube Audio Library download record",
                        "",
                        f"Artist: {supplied.artist}",
                        f"Genre: {supplied.genre}",
                        f"Mood: {', '.join(supplied.moods)}",
                        f"License type shown at download: {supplied.license_type}",
                        f"Imported at: {imported_at}",
                        f"SHA-256: {checksum}",
                        f"Source: {YOUTUBE_AUDIO_LIBRARY_SOURCE}",
                        f"License guidance: {YOUTUBE_AUDIO_LIBRARY_HELP}",
                        "",
                        "Attribution:",
                        credit,
                        "",
                        "Metadata was transcribed by the operator from YouTube Studio when the "
                        "audio was downloaded. Retain this record with the project artifacts.",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            relative_audio = destination.relative_to(catalog_path.resolve().parent).as_posix()
            relative_evidence = evidence.relative_to(catalog_path.resolve().parent).as_posix()
            track = CatalogTrack(
                id=track_id,
                file=relative_audio,
                sha256=digest(destination),
                license_url=YOUTUBE_AUDIO_LIBRARY_HELP,
                license_evidence_file=relative_evidence,
                tags=supplied.selection_tags or _inferred_tags(supplied),
                title=supplied.title,
                artist=supplied.artist,
                genre=supplied.genre,
                moods=supplied.moods,
                energy=supplied.energy,
                tempo_bpm=supplied.tempo_bpm,
                duration_seconds=duration,
                source_library="youtube_audio_library",
                license_type=(
                    "youtube_audio_library_creative_commons"
                    if attribution_required
                    else "youtube_audio_library_standard"
                ),
                attribution_required=attribution_required,
                imported_at=imported_at,
                provenance=Provenance(
                    source=YOUTUBE_AUDIO_LIBRARY_SOURCE,
                    provider="YouTube Audio Library",
                    asset_id=track_id,
                    license_category="licensed",
                    usage_restrictions=(
                        "Creative Commons track; retain the exact supplied attribution."
                        if attribution_required
                        else "YouTube Audio Library Standard License; attribution not required."
                    ),
                    attribution=credit,
                    origin="library",
                ),
            )
            catalog.tracks.append(track)
            prepared.append(track)
            existing_ids.add(track.id)
            existing_by_id[track.id] = track
            existing_hashes.add(track.sha256)
        catalog.schema_version = 2
        _write_catalog(catalog_path, catalog)
    except Exception:
        for path in reversed(created_paths):
            path.unlink(missing_ok=True)
        raise
    return prepared
