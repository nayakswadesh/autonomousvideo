"""Select music from a local, evidence-backed catalog."""

import json
import re
from pathlib import Path

import yaml
from pydantic import Field, model_validator

from autonomous_studio.domain.media import MusicInput
from autonomous_studio.domain.models import Model, MusicSpec
from autonomous_studio.storage.repository import digest
from autonomous_studio.workflow.presentation import local_input


class CatalogTrack(MusicInput):
    tags: list[str] = Field(min_length=1, max_length=12)
    title: str = ""
    artist: str = ""
    genre: str = ""
    moods: list[str] = Field(default_factory=list, max_length=8)
    energy: str = "medium"
    tempo_bpm: int | None = Field(default=None, ge=30, le=240)
    duration_seconds: float | None = Field(default=None, gt=0)
    source_library: str = "legacy"
    license_type: str = "legacy"
    attribution_required: bool = True
    imported_at: str | None = None


class MusicCatalog(Model):
    schema_version: int = 2
    tracks: list[CatalogTrack] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def unique(self):
        if self.schema_version not in (1, 2):
            raise ValueError("unsupported music catalog schema")
        if len({t.id for t in self.tracks}) != len(self.tracks):
            raise ValueError("duplicate music catalog IDs")
        if len({t.sha256 for t in self.tracks}) != len(self.tracks):
            raise ValueError("duplicate music catalog audio")
        return self


def load_catalog(path: Path) -> MusicCatalog:
    if not path.is_file():
        raise ValueError("music catalog is missing; import YouTube Audio Library tracks first")
    if path.stat().st_size > 2_000_000:
        raise ValueError("music catalog exceeds 2 MB")
    return MusicCatalog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def _words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def _score(track: CatalogTrack, tags, spec: MusicSpec | None, recent_track_ids) -> dict:
    requested = {str(tag).casefold() for tag in tags}
    track_tags = {tag.casefold() for tag in track.tags}
    overlap = len(requested & track_tags)
    mood_words = _words(spec.mood) if spec else set()
    metadata_words = _words(" ".join([track.genre, *track.moods, *track.tags]))
    mood_overlap = len(mood_words & metadata_words)
    energy_match = bool(spec and spec.energy.casefold() == track.energy.casefold())
    tempo_score = 0
    if spec and track.tempo_bpm:
        tempo_score = max(0, 6 - abs(spec.tempo - track.tempo_bpm) // 12)
    source_bonus = 8 if track.source_library == "youtube_audio_library" else 0
    recent_penalty = 0
    if track.id in recent_track_ids:
        # The newest use is most expensive; older uses decay but still break deterministic ties.
        recent_penalty = max(8, 40 - recent_track_ids.index(track.id) * 8)
    # Creative fit is intentionally independent from reuse. Reuse and library source are
    # tie-breakers only, so a previously used track still wins when it fits this story better.
    total = overlap * 20 + mood_overlap * 3 + int(energy_match) * 5 + tempo_score
    return {
        "id": track.id,
        "score": total,
        "tag_matches": overlap,
        "mood_matches": mood_overlap,
        "energy_match": energy_match,
        "tempo_score": tempo_score,
        "source_bonus": source_bonus,
        "recent_use_penalty": recent_penalty,
        "source_library": track.source_library,
    }


def recent_music_track_ids(repo, current_project_id: str, limit: int = 6) -> list[str]:
    """Return newest-first track IDs without making old-project corruption a new-project blocker."""
    used = []
    for project in reversed(repo.all_projects()):
        if project.id == current_project_id:
            continue
        selections = project.current_artifacts("music_selection")
        if not selections:
            continue
        try:
            data = json.loads(repo.verify(project, selections[-1]).read_text(encoding="utf-8"))
            track_id = data.get("selected", {}).get("id")
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if track_id and track_id not in used:
            used.append(track_id)
        if len(used) == limit:
            break
    return used


def select_music(
    path: Path,
    tags,
    *,
    spec: MusicSpec | None = None,
    recent_track_ids: list[str] | None = None,
    required_duration: float | None = None,
):
    catalog = load_catalog(path)
    if not catalog.tracks:
        raise ValueError(
            "No YouTube Audio Library tracks are imported. Download tracks into music/inbox "
            "and run studio music-import-batch."
        )
    recent = recent_track_ids or []
    scored = [(track, _score(track, tags, spec, recent)) for track in catalog.tracks]
    eligible = [
        item
        for item in scored
        if item[1]["tag_matches"]
        and (
            required_duration is None
            or item[0].duration_seconds is None
            or item[0].duration_seconds - item[0].trim_start >= required_duration
        )
    ]
    if not eligible:
        raise ValueError("No catalog track matches the requested music tags and duration")
    eligible.sort(
        key=lambda item: (
            -item[1]["score"],
            item[1]["recent_use_penalty"],
            -item[1]["source_bonus"],
            item[0].id,
        )
    )
    selected = eligible[0][0]
    scores = [
        score
        for _, score in sorted(
            scored,
            key=lambda item: (
                -item[1]["score"],
                item[1]["recent_use_penalty"],
                -item[1]["source_bonus"],
                item[0].id,
            ),
        )
    ]
    audio = local_input(path.resolve().parent, selected.file)
    evidence = local_input(path.resolve().parent, selected.license_evidence_file)
    if digest(audio) != selected.sha256:
        raise ValueError("selected catalog audio checksum mismatch")
    if not evidence.read_text(encoding="utf-8").strip():
        raise ValueError("music license evidence cannot be empty")
    return selected, audio, evidence, scores
