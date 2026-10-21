import asyncio
import json
from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageDraw
from pydantic import TypeAdapter, ValidationError
from test_preview_policy import FakeRenderer
from test_render import window_rms

from autonomous_studio.domain.media import PresentationManifest
from autonomous_studio.domain.models import Presentation, Shot, State
from autonomous_studio.hitl.adapters import review_message
from autonomous_studio.providers.library import LibraryMusic
from autonomous_studio.providers.mock import tone
from autonomous_studio.rendering.editorial import (
    caption_chunks,
    caption_sample_progresses,
    map_point,
    quantitative_display_values,
    scene,
    text_box,
    world_map,
)
from autonomous_studio.rendering.renderer import LocalRenderer
from autonomous_studio.storage.repository import digest
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief
from autonomous_studio.workflow.presentation import local_input


def baseline(tmp_path, brief):
    studio = Studio(tmp_path / "store")
    studio.renderer = FakeRenderer(studio.repo)
    project = studio.create(brief, review_policy="preview")
    project = asyncio.run(studio.run(project.id))
    project = studio.decide(project.id, "prototype", "approve", actor="test-harness")
    return studio, project


def manifest(tmp_path, project):
    image = tmp_path / "hero.png"
    Image.new("RGB", (240, 400), "#6e5143").save(image)
    music = tmp_path / "library.wav"
    tone(music, 15, "music")
    evidence = tmp_path / "license.txt"
    evidence.write_text("Test-only original tone; not third-party licensing evidence.")
    provenance = dict(
        source="test fixture",
        provider="test",
        asset_id="fixture",
        license_category="licensed",
        attribution="Test fixture author",
        usage_restrictions="Test only",
        origin="library",
    )
    data = {
        "presentation": {
            "series_label": "Test series",
            "shots": [
                {
                    "shot_id": project.spec.shots[0].id,
                    "image_ref": "hero",
                    "headline": "A CLEAR TITLE",
                }
            ],
        },
        "assets": [
            {
                "kind": "image",
                "id": "hero",
                "file": image.name,
                "sha256": digest(image),
                "provenance": {**provenance, "license_category": "unknown"},
            },
            {
                "kind": "music",
                "id": "bed",
                "file": music.name,
                "sha256": digest(music),
                "license_url": "https://example.org/license",
                "license_evidence_file": evidence.name,
                "provenance": provenance,
            },
        ],
    }
    path = tmp_path / "presentation.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path, data


@pytest.mark.parametrize("fault", ["missing_image", "unknown_license", "no_credit", "union"])
def test_media_schema_fails_closed(tmp_path, brief, fault):
    _, project = baseline(tmp_path, brief)
    _, data = manifest(tmp_path, project)
    if fault == "missing_image":
        data["presentation"]["shots"][0]["image_ref"] = "absent"
    elif fault == "unknown_license":
        data["assets"][1]["provenance"]["license_category"] = "unknown"
    elif fault == "no_credit":
        data["assets"][1]["provenance"]["attribution"] = ""
    else:
        data["assets"][0]["trim_start"] = 1
    with pytest.raises(ValidationError):
        PresentationManifest.model_validate(data)


def test_import_safety_and_stale_revision(tmp_path, brief):
    studio, project = baseline(tmp_path, brief)
    path, _ = manifest(tmp_path, project)
    with pytest.raises(ValueError, match="escapes"):
        local_input(tmp_path, "../elsewhere.png")
    with pytest.raises(ValueError, match="relative"):
        local_input(tmp_path, str(path.resolve()))
    (tmp_path / "hero.png").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        studio.polish(project.id, path, version=project.version)
    assert studio.repo.load(project.id) == project
    path, _ = manifest(tmp_path, project)
    with pytest.raises(ValueError, match="stale"):
        studio.polish(project.id, path, version=99)
    assert studio.repo.load(project.id) == project


@pytest.mark.parametrize("topic", ["earthquake-countdown", "solar-system"])
@pytest.mark.integration
def test_polish_resumes_reuses_narration_and_renders_deterministically(tmp_path, topic):
    brief = load_brief(Path(__file__).parents[1] / "examples" / f"{topic}.yaml")
    studio, old = baseline(tmp_path, brief)
    path, _ = manifest(tmp_path, old)
    old_artifacts = [a.model_dump() for a in old.artifacts]
    old_approvals = [a.model_dump() for a in old.approvals]
    changed = studio.polish(old.id, path, version=old.version)
    assert changed.state == State.NARRATION_PREPARING and changed.pending() is None
    assert (
        changed.spec.shots == old.spec.shots
        and changed.spec.brief.research == old.spec.brief.research
    )
    assert [v.asset.artifact.sha256 for v in changed.prepared_voices] == [
        v.asset.artifact.sha256 for v in old.prepared_voices
    ]
    assert all(v.asset.artifact.version == changed.version for v in changed.prepared_voices)
    assert [a.model_dump() for a in changed.artifacts[: len(old.artifacts)]] == old_artifacts
    assert [a.model_dump() for a in changed.approvals] == old_approvals
    with pytest.raises(ValueError, match="stale"):
        studio.polish(old.id, path, version=old.version)

    class NoSpeechCalls:
        async def synthesize(self, *args):
            raise AssertionError("narration must be reused")

    resumed = Studio(studio.repo.root, voice=NoSpeechCalls())
    result = asyncio.run(resumed.run(old.id))
    assert result.state == State.PROTOTYPE_APPROVAL
    assert len(result.approvals) == len(old.approvals) + 1
    assert result.pending().gate == "prototype"
    review = review_message(result, result.pending()).text
    assert "Complete script:" in review and "UNKNOWN — DO NOT PUBLISH" not in review
    assert any("UNKNOWN" in warning for warning in result.warnings)
    movie = resumed.repo.verify(result, result.current_artifacts("prototype")[-1])
    qa = json.loads(resumed.repo.verify(result, result.current_artifacts("qa")[-1]).read_text())
    assert qa["presentation"]["layout_passed"] and qa["video"]["decodable"]
    assert qa["clipped_samples"] == 0 and not qa["publishable"]
    music = resumed.repo.verify(result, result.current_artifacts("stem_music")[-1])
    ducked = resumed.repo.verify(result, result.current_artifacts("stem_music_ducked")[-1])
    voice_start = result.prepared_voices[0].asset.start + 0.2
    assert window_rms(ducked, voice_start, 0.1) < window_rms(music, voice_start, 0.1) * 0.8
    repeat = resumed.repo.attempt_dir(result, "determinism")
    asyncio.run(LocalRenderer(resumed.repo).render(result, repeat))
    assert digest(movie) == digest(repeat / "prototype.mp4")
    assert asyncio.run(resumed.run(old.id)).revision == result.revision
    assert all(resumed.repo.verify(result, a).is_file() for a in result.artifacts)


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "kinetic_title"},
        {"kind": "hero_image"},
        {"kind": "outro"},
        {"kind": "rank_reveal", "rank": 1, "metric": "10 units"},
        {"kind": "big_metric", "metric": "10 UNITS"},
        {"kind": "map", "latitude": 30, "longitude": 105},
        {"kind": "comparison", "labels": ["A", "B"], "values": [10, 30]},
        {"kind": "timeline", "labels": ["1900", "2000", "2020"]},
        {"kind": "data_chart", "values": [2, 3, 1, 5]},
        {"kind": "source_card"},
    ],
)
def test_editorial_primitives(tmp_path, brief, fields):
    _, project = baseline(tmp_path, brief)
    shot = TypeAdapter(Shot).validate_python(
        dict(
            id="test",
            start=0,
            duration=4,
            title="Test title",
            subtitle="A short subtitle",
            **fields,
        )
    )
    project.spec.shots = [shot]
    project.spec.presentation = Presentation()
    boxes = []
    first = scene(project, shot, 0.5, records=boxes)
    assert first.size == (540, 960)
    assert first.tobytes() == scene(project, shot, 0.5).tobytes()
    assert all(0 <= b["box"][0] < b["box"][2] <= 540 for b in boxes)


def test_layout_overflow_rejected_and_map_projection():
    draw = ImageDraw.Draw(Image.new("RGB", (540, 960)))
    with pytest.raises(ValueError, match="safe area"):
        text_box(draw, "unreadable " * 100, (42, 42, 498, 90), 40, "white")
    assert map_point(90, -180, (0, 0, 360, 180)) == (0, 0)
    assert map_point(-90, 180, (0, 0, 360, 180)) == (359, 179)
    assert world_map().getbbox() is not None


def test_long_exact_opening_hook_uses_expanded_readable_title_area(tmp_path, brief):
    _, project = baseline(tmp_path, brief)
    title = "Two deaths forced Los Alamos to rethink nuclear safety."
    shot = TypeAdapter(Shot).validate_python(
        {
            "kind": "kinetic_title",
            "id": "opening",
            "start": 0,
            "duration": 6,
            "title": title,
            "subtitle": "Los Alamos · 1945–1946",
        }
    )
    project.spec.shots = [shot]
    project.spec.presentation = Presentation()
    records = []

    rendered = scene(project, shot, records=records)

    title_record = next(record for record in records if record["text"] == title.upper())
    assert rendered.size == (540, 960)
    assert title_record["box"] == (42, 210, 498, 560)
    assert title_record["font_size"] >= 50


def test_metric_frame_has_no_generic_map_or_technical_footer(tmp_path, brief, monkeypatch):
    _, project = baseline(tmp_path, brief)
    shot = TypeAdapter(Shot).validate_python(
        {
            "kind": "big_metric",
            "id": "metric",
            "start": 0,
            "duration": 4,
            "title": "A measured depth",
            "subtitle": "Below the visible clouds",
            "metric": "3,000 KM",
        }
    )
    project.spec.shots = [shot]
    project.spec.presentation = Presentation()
    monkeypatch.setattr(
        "autonomous_studio.rendering.editorial.world_map",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("map fallback used")),
    )
    records = []
    scene(project, shot, records=records)
    copy = {record["text"] for record in records}
    assert "RESEARCH EXCERPT / REVIEW COPY" not in copy
    assert "AI ILLUSTRATION / NOT ARCHIVAL" not in copy
    assert not any("Natural Earth" in value for value in copy)


def test_comparison_recovers_visible_values_and_units_from_claims(tmp_path, brief):
    _, project = baseline(tmp_path, brief)
    shot = TypeAdapter(Shot).validate_python(
        {
            "kind": "comparison",
            "id": "comparison",
            "start": 0,
            "duration": 5,
            "title": "Reported impact",
            "labels": ["Shaanxi", "Haiyuan"],
            "values": [830000, 200000],
            "claim_ids": ["shaanxi-toll", "haiyuan-toll"],
        }
    )
    project.spec.shots = [shot]
    project.spec.presentation = Presentation()
    values = quantitative_display_values(project.spec.brief, shot)
    assert values == ["≈830,000 reported deaths", "≈200,000 reported deaths"]
    records = []
    scene(project, shot, records=records)
    rendered = {record["text"] for record in records}
    assert set(values) <= rendered


def test_comparison_supports_descriptive_labels_and_display_values(tmp_path, brief):
    _, project = baseline(tmp_path, brief)
    shot = TypeAdapter(Shot).validate_python(
        {
            "kind": "comparison",
            "id": "europa-water",
            "start": 0,
            "duration": 8,
            "title": "Models Suggest Twice the Water",
            "subtitle": "The inferred ocean remains unmeasured",
            "labels": ["Europa's modeled water inventory", "All of Earth's oceans"],
            "values": [2, 1],
            "display_values": [
                "≈2 Earth-ocean water volumes",
                "1 Earth-ocean water volume",
            ],
            "takeaway": "Models suggest Europa may hold ≈2 times Earth's ocean water.",
        }
    )
    project.spec.shots = [shot]
    project.spec.presentation = Presentation()
    records = []
    scene(project, shot, records=records)
    rendered = {record["text"] for record in records}
    assert set(shot.labels + shot.display_values) <= rendered
    assert all(record["font_size"] >= 13 for record in records)


def test_four_item_comparison_keeps_labels_and_values_readable(tmp_path, brief):
    _, project = baseline(tmp_path, brief)
    shot = TypeAdapter(Shot).validate_python(
        {
            "kind": "comparison",
            "id": "four-items",
            "start": 0,
            "duration": 8,
            "title": "Four-way comparison",
            "labels": [
                "First modeled inventory",
                "Second modeled inventory",
                "Third modeled inventory",
                "Fourth modeled inventory",
            ],
            "values": [4, 3, 2, 1],
            "display_values": ["400 units", "300 units", "200 units", "100 units"],
        }
    )
    project.spec.shots = [shot]
    project.spec.presentation = Presentation()
    records = []
    scene(project, shot, records=records)
    rendered = {record["text"] for record in records}
    assert set(shot.labels + shot.display_values) <= rendered


def test_long_narration_uses_time_aligned_readable_caption_cards(tmp_path, brief):
    _, project = baseline(tmp_path, brief)
    narration = (
        "Cassini supports a model-dependent age of ten to one hundred million years; "
        "older recycling models remain possible."
    )
    shot = TypeAdapter(Shot).validate_python(
        {
            "kind": "timeline",
            "id": "age",
            "start": 0,
            "duration": 10,
            "title": "The Age Is Still Unsettled",
            "subtitle": "Competing interpretations",
            "labels": ["Young model", "Older recycling model"],
            "voice_segments": [
                {
                    "shot_id": "age",
                    "spoken_text": narration,
                    "offset": 0.5,
                    "estimated_duration": 8.5,
                }
            ],
        }
    )
    project.spec.shots = [shot]
    project.prepared_voices = []
    project.spec.presentation = Presentation()
    chunks = caption_chunks(narration)
    rendered = []
    for progress in caption_sample_progresses(project, shot):
        boxes = []
        scene(project, shot, progress, records=boxes)
        rendered.extend(record["text"] for record in boxes if record["box"] == (46, 793, 494, 840))

    assert chunks == [
        "Cassini supports a model-dependent age of ten to one hundred million years;",
        "older recycling models remain possible.",
    ]
    assert rendered[-len(chunks) :] == chunks
    assert " ".join(chunks) == narration

    project.spec.closed_captions = False
    boxes = []
    scene(project, shot, 0.5, records=boxes)
    assert caption_sample_progresses(project, shot) == [0.5]
    assert not any(record["box"] == (46, 793, 494, 840) for record in boxes)


def test_editorial_scene_renders_full_hd_without_losing_source_canvas(tmp_path, brief):
    studio = Studio(tmp_path)
    project = studio.create(brief)
    project.spec = asyncio.run(studio.director.develop(brief, []))
    project.spec.brief.width = 1080
    project.spec.brief.height = 1920
    project.spec.presentation = Presentation(series_label="Full HD")
    source = tmp_path / "full-hd-source.png"
    Image.new("RGB", (1080, 1920), "#72513f").save(source)

    frame = scene(project, project.spec.shots[0], image_path=source)

    assert frame.size == (1080, 1920)


def test_pending_preview_refresh_is_versioned_and_reuses_provider_inputs(tmp_path, brief):
    studio = Studio(tmp_path / "refresh")
    studio.renderer = FakeRenderer(studio.repo)
    original = asyncio.run(studio.run(studio.create(brief, review_policy="preview").id))
    old_approval = original.pending()
    voice_hashes = [voice.asset.artifact.sha256 for voice in original.prepared_voices]
    audio_hashes = [audio.artifact.sha256 for audio in original.spec.audio_assets]

    refreshed = studio.refresh_preview(
        original.id,
        version=original.version,
        reason="Improve caption boundaries",
        actor="test-harness",
    )

    assert refreshed.version == original.version + 1
    assert refreshed.state == State.AUDIO_PREPARED and not refreshed.pending()
    assert next(a for a in refreshed.approvals if a.id == old_approval.id).decision == "superseded"
    assert [voice.asset.artifact.sha256 for voice in refreshed.prepared_voices] == voice_hashes
    assert [audio.artifact.sha256 for audio in refreshed.spec.audio_assets] == audio_hashes
    assert not refreshed.feedback
    assert all(audio.artifact.version == refreshed.version for audio in refreshed.spec.audio_assets)
    result = asyncio.run(studio.run(refreshed.id))
    assert result.state == State.PROTOTYPE_APPROVAL
    assert result.pending().version == result.version
    assert len([a for a in result.approvals if a.decision == "pending"]) == 1


def test_completed_preview_refresh_can_correct_captions_without_provider_calls(tmp_path, brief):
    studio = Studio(tmp_path / "completed-refresh")
    studio.renderer = FakeRenderer(studio.repo)
    original = asyncio.run(studio.run(studio.create(brief, review_policy="preview").id))
    completed = studio.decide(original.id, "prototype", "approve", version=original.version)
    assert completed.state == State.COMPLETED
    receipt_path = studio.repo.project_dir(completed.id) / "old-upload.json"
    receipt_path.write_text('{"video_id":"previous-upload"}', encoding="utf-8")
    completed.artifacts.append(studio.repo.artifact(completed, receipt_path,
                                                   "youtube_upload_receipt"))
    studio.repo.save(completed, "test_prior_upload", {})

    refreshed = studio.refresh_preview(
        completed.id,
        version=completed.version,
        reason="Restore the saved closed-caption preference",
        actor="test-harness",
        closed_captions=False,
    )

    assert refreshed.version == completed.version + 1
    assert refreshed.state == State.AUDIO_PREPARED and not refreshed.pending()
    assert refreshed.spec.closed_captions is False
    assert refreshed.publication is None
    assert not refreshed.current_artifacts("prototype")
    assert not refreshed.current_artifacts("youtube_upload_receipt")
    result = asyncio.run(studio.run(refreshed.id))
    assert result.state == State.PROTOTYPE_APPROVAL
    assert result.pending().gate == "prototype"


def test_short_music_and_missing_evidence_rejected(tmp_path, brief):
    studio, old = baseline(tmp_path, brief)
    path, data = manifest(tmp_path, old)
    (tmp_path / "license.txt").unlink()
    with pytest.raises(ValueError, match="missing"):
        studio.polish(old.id, path, version=old.version)
    path, data = manifest(tmp_path, old)
    data["assets"][1]["trim_start"] = 14
    path.write_text(yaml.safe_dump(data))
    changed = studio.polish(old.id, path, version=old.version)
    output = studio.repo.attempt_dir(changed, "short-music")
    with pytest.raises(ValueError, match="shorter"):
        asyncio.run(
            LibraryMusic(studio.repo).create_or_select(
                changed, changed.spec.brief.music, output / "music.wav"
            )
        )
