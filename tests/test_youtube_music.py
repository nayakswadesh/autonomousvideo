from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from autonomous_studio.domain.models import MusicSpec, Provenance
from autonomous_studio.providers.music_catalog import CatalogTrack, MusicCatalog, select_music
from autonomous_studio.providers.youtube_audio_library import (
    YouTubeImportManifest,
    import_batch,
)
from autonomous_studio.storage.repository import digest


class AudioInspection:
    def inspect(self, path):
        return {"has_audio": True, "has_video": False, "duration": 93.5}


def write_manifest(path: Path, *, license_type="standard", attribution=""):
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "tracks": [
                    {
                        "file": "quiet-sky.mp3",
                        "title": "Quiet Sky",
                        "artist": "Example Artist",
                        "genre": "Cinematic",
                        "moods": ["Calm"],
                        "license_type": license_type,
                        "attribution": attribution,
                        "energy": "low",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_batch_import_records_youtube_metadata_and_evidence(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "quiet-sky.mp3").write_bytes(b"fixture-audio")
    manifest = inbox / "tracks.yaml"
    write_manifest(manifest)
    catalog_path = tmp_path / "library" / "youtube-audio-library.yaml"

    imported = import_batch(manifest, catalog_path, ffmpeg=AudioInspection())
    catalog = MusicCatalog.model_validate(yaml.safe_load(catalog_path.read_text(encoding="utf-8")))
    track = catalog.tracks[0]
    assert imported[0].id == track.id == "quiet-sky-example-artist"
    assert track.source_library == "youtube_audio_library"
    assert track.license_type == "youtube_audio_library_standard"
    assert track.duration_seconds == 93.5 and track.tags == ["reflective", "ambient"]
    assert (catalog_path.parent / track.file).read_bytes() == b"fixture-audio"
    evidence = (catalog_path.parent / track.license_evidence_file).read_text(encoding="utf-8")
    assert "Attribution not required" in evidence or "attribution not required" in evidence
    assert import_batch(manifest, catalog_path, ffmpeg=AudioInspection()) == []
    (inbox / "quiet-sky.mp3").write_bytes(b"different-audio")
    with pytest.raises(ValueError, match="different audio"):
        import_batch(manifest, catalog_path, ffmpeg=AudioInspection())


def test_creative_commons_import_requires_exact_attribution(tmp_path):
    manifest = tmp_path / "tracks.yaml"
    write_manifest(manifest, license_type="creative_commons")
    with pytest.raises(ValidationError, match="exact attribution"):
        YouTubeImportManifest.model_validate(yaml.safe_load(manifest.read_text()))


def catalog_track(tmp_path, track_id, content, *, source_library="youtube_audio_library"):
    audio = tmp_path / f"{track_id}.mp3"
    evidence = tmp_path / f"{track_id}.md"
    audio.write_bytes(content)
    evidence.write_text("YouTube Audio Library download evidence", encoding="utf-8")
    return CatalogTrack(
        id=track_id,
        file=audio.name,
        sha256=digest(audio),
        license_url="https://support.google.com/youtube/answer/3376882",
        license_evidence_file=evidence.name,
        tags=["reflective", "ambient"],
        title=track_id.title(),
        artist="Artist",
        genre="Cinematic",
        moods=["Calm"],
        energy="low",
        duration_seconds=90,
        source_library=source_library,
        license_type="youtube_audio_library_standard",
        attribution_required=False,
        provenance=Provenance(
            source="https://www.youtube.com/audiolibrary",
            provider="YouTube Audio Library",
            asset_id=track_id,
            license_category="licensed",
            usage_restrictions="Audio Library license",
            attribution="Audio Library track",
            origin="library",
        ),
    )


def test_matching_prefers_youtube_and_avoids_recent_track(tmp_path):
    first = catalog_track(tmp_path, "first", b"first")
    second = catalog_track(tmp_path, "second", b"second")
    legacy = catalog_track(tmp_path, "legacy", b"legacy", source_library="legacy")
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        yaml.safe_dump(
            MusicCatalog(tracks=[first, second, legacy]).model_dump(mode="json"),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    selected, _, _, scores = select_music(
        catalog,
        ["reflective"],
        spec=MusicSpec(mood="calm reflective", energy="low", tempo=72),
        recent_track_ids=["first"],
        required_duration=60,
    )
    assert selected.id == "second"
    assert scores[0]["source_library"] == "youtube_audio_library"
    recent = next(score for score in scores if score["id"] == "first")
    assert recent["recent_use_penalty"] == 40


def test_better_mood_fit_beats_reuse_penalty(tmp_path):
    best = catalog_track(tmp_path, "best", b"best")
    alternative = catalog_track(tmp_path, "alternative", b"alternative")
    best.tags = ["reflective", "ambient"]
    alternative.tags = ["reflective"]
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        yaml.safe_dump(
            MusicCatalog(tracks=[best, alternative]).model_dump(mode="json"), sort_keys=False
        ),
        encoding="utf-8",
    )
    selected, _, _, _ = select_music(
        catalog,
        ["reflective", "ambient"],
        spec=MusicSpec(mood="calm reflective", energy="low"),
        recent_track_ids=["best"],
        required_duration=60,
    )
    assert selected.id == "best"


def test_empty_catalog_gives_actionable_setup_error(tmp_path):
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text("schema_version: 2\ntracks: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="music-import-batch"):
        select_music(catalog, ["reflective"])
