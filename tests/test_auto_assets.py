import asyncio
import base64
import io
from pathlib import Path

import pytest
import yaml
from PIL import Image
from pydantic import ValidationError
from test_live import FakeHTTP, response
from test_preview_policy import FakeRenderer

from autonomous_studio.config import Settings
from autonomous_studio.domain.models import (
    AssetPlan,
    AutoAssetPolicy,
    ImageBrief,
    LoopSpec,
    MapShot,
    MetricPairShot,
    QualitativeComparisonShot,
    ShotLook,
    State,
    StudyDesignShot,
    VoiceSegment,
)
from autonomous_studio.providers.archival import WikimediaCommonsMedia
from autonomous_studio.providers.asset_planner import (
    OpenAIAssetPlanner,
    normalize_asset_plan,
    validate_asset_plan,
)
from autonomous_studio.providers.http import RemoteError
from autonomous_studio.providers.images import AssetPreparationError, OpenAIImages
from autonomous_studio.providers.mock import MockDirector, tone
from autonomous_studio.providers.music_catalog import select_music
from autonomous_studio.providers.openai import OpenAIClient, strict_schema
from autonomous_studio.storage.repository import digest, write_json
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.assets import recover_asset_plan
from autonomous_studio.workflow.engine import load_brief
from autonomous_studio.workflow.publication import recover_publication_package
from autonomous_studio.workflow.short_layout import validate_story_production_layout


@pytest.fixture
def catalog(tmp_path):
    audio = tmp_path / "track.wav"
    tone(audio, 60, "music")
    evidence = tmp_path / "license.md"
    evidence.write_text("Original fixture licensed for testing only.")
    track = dict(
        kind="music",
        id="reflective",
        file=audio.name,
        sha256=digest(audio),
        license_url="https://example.org/license",
        license_evidence_file=evidence.name,
        tags=["reflective", "ambient"],
        provenance=dict(
            source="fixture",
            provider="test",
            asset_id="track",
            origin="original",
            license_category="licensed",
            attribution="Test fixture author",
        ),
    )
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump({"tracks": [track]}))
    return path


def image_response():
    stream = io.BytesIO()
    Image.new("RGB", (864, 1536), "#7e6558").save(stream, format="PNG")
    return {
        "created": 123,
        "data": [{"b64_json": base64.b64encode(stream.getvalue()).decode()}],
        "usage": {"total_tokens": 42},
    }


def test_archival_media_retains_source_derivative_and_rights(tmp_path, brief, catalog):
    studio, project = setup_project(tmp_path, brief, catalog)
    stream = io.BytesIO()
    Image.new("RGB", (1200, 800), "#4b6170").save(stream, format="JPEG")

    class ArchiveHTTP:
        async def get_json(self, *args, **kwargs):
            return {
                "query": {
                    "pages": [
                        {
                            "pageid": 123,
                            "title": "File:Documentary subject.jpg",
                            "imageinfo": [
                                {
                                    "mime": "image/jpeg",
                                    "url": "https://upload.wikimedia.org/source.jpg",
                                    "thumburl": "https://upload.wikimedia.org/thumb.jpg",
                                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:Subject",
                                    "extmetadata": {
                                        "ObjectName": {"value": "Documentary subject"},
                                        "Artist": {"value": "Example photographer"},
                                        "LicenseShortName": {"value": "CC BY 4.0"},
                                        "LicenseUrl": {
                                            "value": "https://creativecommons.org/licenses/by/4.0/"
                                        },
                                    },
                                }
                            ],
                        }
                    ]
                }
            }

        async def get(self, *args, **kwargs):
            return stream.getvalue()

    image = ImageBrief(
        id="archive-one",
        prompt="A generated editorial fallback showing the documented subject",
        source_type="archival",
        search_query="documentary subject exact place",
        shot_ids=[project.spec.shots[0].id],
    )
    output = studio.repo.attempt_dir(project, "archive") / "archive-one.png"
    acquired = asyncio.run(
        WikimediaCommonsMedia(ArchiveHTTP(), studio.repo).acquire(project, image, output)
    )

    assert output.is_file() and acquired.original.width == 1200
    assert acquired.original.height == 800 and acquired.visual.width == 864
    assert acquired.visual.provenance.origin == "library"
    assert acquired.visual.provenance.license_category == "licensed"
    metadata = yaml.safe_load(
        studio.repo.resolve(project.id, acquired.metadata.path).read_text(encoding="utf-8")
    )
    assert metadata["license"] == "CC BY 4.0"
    assert metadata["personality_privacy_review"] == "required"
    edit = yaml.safe_load(
        studio.repo.resolve(project.id, acquired.edit_manifest.path).read_text(encoding="utf-8")
    )
    assert edit["source_artifact_id"] == acquired.original.id


def test_metric_headline_is_normalized_out_of_its_dedicated_slot(
    tmp_path, brief, catalog, monkeypatch
):
    _, project = setup_project(tmp_path, brief, catalog)
    monkeypatch.setattr("autonomous_studio.providers.asset_planner.scene", lambda *args: None)
    plan = asyncio.run(Planner().plan(project))
    metric = next(shot for shot in project.spec.shots if shot.kind == "big_metric")
    look = next(item for item in plan.display if item.shot_id == metric.id)
    look.headline = metric.metric
    look.visual_strategy = "chart"

    normalized = normalize_asset_plan(project, plan)

    normalized_look = next(item for item in normalized.display if item.shot_id == metric.id)
    assert normalized_look.headline is None
    assert normalized_look.visual_strategy == "text_only_intentional"


def test_numeric_slot_normalizer_uses_approved_subtitle_and_one_metric_slot(
    tmp_path, brief, catalog, monkeypatch
):
    _, project = setup_project(tmp_path, brief, catalog)
    monkeypatch.setattr("autonomous_studio.providers.asset_planner.scene", lambda *args: None)
    plan = asyncio.run(Planner().plan(project))
    ranked = next(shot for shot in project.spec.shots if shot.kind == "rank_reveal")
    look = next(item for item in plan.display if item.shot_id == ranked.id)
    look.headline = f"About {ranked.metric} reported deaths"
    look.annotation = ranked.subtitle + " · day 16"

    normalized = normalize_asset_plan(project, plan)
    normalized_look = next(item for item in normalized.display if item.shot_id == ranked.id)

    assert normalized_look.headline is None
    assert normalized_look.annotation == ranked.subtitle
    assert validate_asset_plan(project, normalized) is normalized


def test_metric_pair_numbers_are_approved_but_not_duplicated_or_invented(
    tmp_path, brief, catalog, monkeypatch
):
    _, project = setup_project(tmp_path, brief, catalog)
    monkeypatch.setattr("autonomous_studio.providers.asset_planner.scene", lambda *args: None)
    original = project.spec.shots[1]
    base = original.model_dump(include=set(MetricPairShot.model_fields) - {"kind", "metrics"})
    base["subtitle"] = ""
    pair = MetricPairShot(**base, metrics=["5,160 sensors", "≈1 cubic kilometer of ice"])
    project.spec.shots[1] = pair
    plan = asyncio.run(Planner().plan(project))
    look = next(item for item in plan.display if item.shot_id == pair.id)
    look.visual_strategy = "diagram"
    look.headline = "5,160 sensors"
    look.annotation = "1 cubic kilometer"
    for image in plan.images:
        image.shot_ids = [shot_id for shot_id in image.shot_ids if shot_id != pair.id]
    plan.images = [image for image in plan.images if image.shot_ids]

    normalized = normalize_asset_plan(project, plan)
    clean_look = next(item for item in normalized.display if item.shot_id == pair.id)
    assert clean_look.headline is None
    assert clean_look.annotation == ""
    assert validate_asset_plan(project, normalized) is normalized

    clean_look.annotation = "6,000 sensors"
    with pytest.raises(ValueError, match="unsupported numeric values"):
        validate_asset_plan(project, normalized)


def test_study_design_numbers_in_rendered_steps_are_not_repeated(
    tmp_path, brief, catalog, monkeypatch
):
    _, project = setup_project(tmp_path, brief, catalog)
    monkeypatch.setattr("autonomous_studio.providers.asset_planner.scene", lambda *args: None)
    original = project.spec.shots[1]
    base = original.model_dump(include=set(StudyDesignShot.model_fields) - {"kind", "steps"})
    base["subtitle"] = "Evidence for cosmic particles"
    project.spec.shots[1] = StudyDesignShot(
        **base, steps=["Sample: 28 events", "Finding: cosmic particles"]
    )
    plan = asyncio.run(Planner().plan(project))
    look = next(item for item in plan.display if item.shot_id == original.id)
    look.visual_strategy = "diagram"
    look.headline = "28 events"
    for image in plan.images:
        image.shot_ids = [shot_id for shot_id in image.shot_ids if shot_id != original.id]
    plan.images = [image for image in plan.images if image.shot_ids]

    normalized = normalize_asset_plan(project, plan)
    clean_look = next(item for item in normalized.display if item.shot_id == original.id)
    assert clean_look.headline is None
    assert validate_asset_plan(project, normalized) is normalized


def setup_project(tmp_path, brief, catalog, *, publication_clearance=False, closed_captions=True):
    studio = Studio(tmp_path / "projects")
    project = studio.create(
        brief,
        review_policy="preview",
        auto_assets=AutoAssetPolicy(music_catalog=str(catalog)),
        publication_clearance=publication_clearance,
        closed_captions=closed_captions,
    )
    project.spec = asyncio.run(MockDirector().develop(brief, []))
    project.spec.closed_captions = closed_captions
    studio.repo.save(project, "test_story", {})
    return studio, project


class Planner:
    calls = 0

    async def plan(self, project, feedback=""):
        self.calls += 1
        eligible = [
            s
            for s in project.spec.shots
            if s.kind in {"kinetic_title", "hero_image", "rank_reveal", "outro"}
        ][:2]
        return AssetPlan(
            series_label="AUTOMATIC STUDIO",
            music_tags=["reflective"],
            rationale="Respectful visual interpretation of the supplied topic.",
            images=[
                ImageBrief(id=f"image-{i}", prompt=f"An illustration of {s.title}", shot_ids=[s.id])
                for i, s in enumerate(eligible)
            ],
            display=[
                ShotLook(
                    shot_id=s.id,
                    headline=(
                        s.title
                        if s.id == project.spec.shots[0].id
                        or s.kind == "source_card"
                        or any(word in s.title.lower() for word in ("provisional", "working"))
                        else "A clear title"
                    ),
                    annotation=s.subtitle,
                )
                for s in project.spec.shots
            ],
        )


def test_asset_plan_rejects_competing_opening_headline(tmp_path, brief, catalog):
    _, project = setup_project(tmp_path, brief, catalog)
    plan = asyncio.run(Planner().plan(project))
    opening = next(look for look in plan.display if look.shot_id == project.spec.shots[0].id)
    opening.headline = "A different promise"

    with pytest.raises(ValueError, match="chosen hook word for word"):
        validate_asset_plan(project, plan)


def test_image_cache_budget_and_metadata(tmp_path, brief, catalog):
    studio, p = setup_project(tmp_path, brief, catalog)
    http = FakeHTTP([image_response(), image_response()])
    client = OpenAIClient(Settings(api_key="test"), tmp_path / "cache", http)
    provider = OpenAIImages(client, studio.repo)
    output = studio.repo.attempt_dir(p, "images")
    a = ImageBrief(id="one", prompt="A quiet illustrated landscape", shot_ids=[p.spec.shots[0].id])
    first = asyncio.run(provider.generate(p, a, output / "a.png"))
    again = asyncio.run(provider.generate(p, a, output / "b.png"))
    assert first.sha256 == again.sha256 and again.cost == 0
    assert first.width == 864 and first.height == 1536 and first.cost is None
    assert first.provenance.license_category == "unknown"
    assert p.image_requests == 1 and len(http.calls) == 1
    assert http.calls[0][1]["n"] == 1 and "response_format" not in http.calls[0][1]
    asyncio.run(
        provider.generate(
            p, a.model_copy(update={"prompt": "A different landscape"}), output / "c.png"
        )
    )
    with pytest.raises(AssetPreparationError, match="budget"):
        asyncio.run(
            provider.generate(
                p, a.model_copy(update={"prompt": "A third landscape"}), output / "d.png"
            )
        )
    assert len(http.calls) == 2 and studio.repo.load(p.id).image_requests == 2


def test_ambiguous_request_is_never_replayed(tmp_path, brief, catalog):
    studio, p = setup_project(tmp_path, brief, catalog)

    class FailingHTTP:
        calls = 0

        async def json(self, *args, **kwargs):
            self.calls += 1
            assert kwargs["attempts"] == 1
            raise RemoteError("test")

    http = FailingHTTP()
    client = OpenAIClient(Settings(api_key="test"), tmp_path / "cache", http)
    output = studio.repo.attempt_dir(p, "ambiguous")
    a = ImageBrief(id="one", prompt="A quiet illustrated landscape", shot_ids=[p.spec.shots[0].id])
    with pytest.raises(RemoteError):
        asyncio.run(OpenAIImages(client, studio.repo).generate(p, a, output / "a.png"))
    reloaded = studio.repo.load(p.id)
    with pytest.raises(AssetPreparationError, match="No automatic replay"):
        asyncio.run(OpenAIImages(client, studio.repo).generate(reloaded, a, output / "b.png"))
    assert http.calls == 1 and reloaded.image_requests == 1


def test_image_validation_and_cache_tampering(tmp_path, brief, catalog):
    studio, p = setup_project(tmp_path, brief, catalog)
    bad = {"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]}
    http = FakeHTTP([bad])
    client = OpenAIClient(Settings(api_key="test"), tmp_path / "cache", http)
    a = ImageBrief(id="one", prompt="A quiet illustrated landscape", shot_ids=[p.spec.shots[0].id])
    out = studio.repo.attempt_dir(p, "bad")
    with pytest.raises(AssetPreparationError, match="PNG"):
        asyncio.run(OpenAIImages(client, studio.repo).generate(p, a, out / "bad.png"))
    http.replies = [image_response()]
    a.prompt = "A different illustrated landscape"
    asyncio.run(OpenAIImages(client, studio.repo).generate(p, a, out / "good.png"))
    for image in (client.cache / "images").glob("*.png"):
        image.write_bytes(b"tampered")
    with pytest.raises(AssetPreparationError, match="Cached image changed"):
        asyncio.run(OpenAIImages(client, studio.repo).generate(p, a, out / "repeat.png"))
    assert len(http.calls) == 2


def test_music_selection_and_license_validation(catalog):
    track, source, evidence, scores = select_music(catalog, ["reflective"])
    assert track.id == "reflective" and source.is_file() and evidence.is_file()
    assert scores[0]["id"] == "reflective"
    assert scores[0]["tag_matches"] == 1 and scores[0]["score"] > 0
    with pytest.raises(ValueError, match="matches"):
        select_music(catalog, ["uplifting"])
    content = yaml.safe_load(catalog.read_text())
    content["tracks"][0]["provenance"]["license_category"] = "unknown"
    catalog.write_text(yaml.safe_dump(content))
    with pytest.raises(ValidationError, match="documented license"):
        select_music(catalog, ["reflective"])


def test_plan_contract_and_reference_validation(tmp_path, brief, catalog):
    studio, p = setup_project(tmp_path, brief, catalog)
    plan = asyncio.run(Planner().plan(p))
    http = FakeHTTP([response(plan.model_dump_json())])
    planner = OpenAIAssetPlanner(OpenAIClient(Settings(api_key="test"), tmp_path / "cache", http))
    assert asyncio.run(planner.plan(p)).images == plan.images
    assert strict_schema(AssetPlan.model_json_schema())["additionalProperties"] is False
    plan.images[0].shot_ids = ["missing"]
    with pytest.raises(ValueError, match="existing"):
        validate_asset_plan(p, plan)


def test_asset_plan_repairs_use_luna_without_changing_approved_story(
    tmp_path, brief, catalog
):
    _, project = setup_project(tmp_path, brief, catalog)
    plan = asyncio.run(Planner().plan(project))
    project.live_models.update(
        director_model="gpt-6.1-sol", shorts_repair_model="gpt-6-luna"
    )
    http = FakeHTTP([response(plan.model_dump_json()), response(plan.model_dump_json())])
    planner = OpenAIAssetPlanner(OpenAIClient(Settings(api_key="test"), tmp_path / "cache", http))
    approved_story = project.spec.model_dump(mode="json")
    asyncio.run(planner.plan(project))
    asyncio.run(planner.plan(project, "shorten one headline"))
    assert [call[1]["model"] for call in http.calls] == ["gpt-6.1-sol", "gpt-6-luna"]
    assert project.spec.model_dump(mode="json") == approved_story


def test_story_preflight_uses_production_comparison_card_geometry(tmp_path, brief, catalog):
    _, project = setup_project(tmp_path, brief, catalog, closed_captions=False)
    spec = project.spec.model_copy(deep=True)
    spec.brief.width, spec.brief.height, spec.brief.duration = 540, 960, 5
    spec.shots = [
        QualitativeComparisonShot(
            id="shot_01",
            start=0,
            duration=5,
            title="The evidence",
            alternatives=[
                "One kind of memory",
                "Another kind of memory",
                "Not established: protection for every kind of memory",
            ],
        )
    ]
    validate_story_production_layout(spec)
    spec.shots[0].alternatives[2] = " ".join(["Unsupported memory protection claim"] * 9)
    with pytest.raises(ValueError, match="shot_01 production layout"):
        validate_story_production_layout(spec)


def test_display_numbers_unicode_and_unsupported_values(tmp_path, brief, catalog, monkeypatch):
    _, p = setup_project(tmp_path, brief, catalog)
    monkeypatch.setattr("autonomous_studio.providers.asset_planner.scene", lambda *args: None)
    p.spec.shots[0].title = "Ranks 1–3 remain provisional"
    p.spec.shots[0].subtitle = "≈243,000 reported deaths"
    plan = asyncio.run(Planner().plan(p))
    plan.display[0].headline = p.spec.shots[0].title
    assert validate_asset_plan(p, plan) is plan
    plan.display[0].annotation = "≈243000 reported deaths"
    assert validate_asset_plan(p, plan) is plan
    for annotation in ("243 deaths", "2013 deaths", "243.000 deaths", "3.75 deaths"):
        plan.display[0].annotation = annotation
        with pytest.raises(ValueError, match="unsupported numeric"):
            validate_asset_plan(p, plan)


def test_decorative_eyebrow_numbers_are_removed_without_changing_editorial_copy(
    tmp_path, brief, catalog, monkeypatch
):
    _, p = setup_project(tmp_path, brief, catalog)
    monkeypatch.setattr("autonomous_studio.providers.asset_planner.scene", lambda *args: None)
    plan = asyncio.run(Planner().plan(p))
    rank = next(s for s in p.spec.shots if s.kind == "rank_reveal")
    look = next(s for s in plan.display if s.shot_id == rank.id)
    look.eyebrow = f"Rank {rank.rank} of {rank.rank}"
    plan.series_label = "Ranks 5→1"
    title_look = next(s for s in plan.display if s.shot_id == p.spec.shots[0].id)
    title_look.eyebrow = "USGS-hosted 2009 study"
    title_look.annotation = p.spec.shots[0].voice_segments[0].spoken_text

    normalized = normalize_asset_plan(p, plan)
    normalized_rank = next(s for s in normalized.display if s.shot_id == rank.id)
    normalized_title = next(s for s in normalized.display if s.shot_id == p.spec.shots[0].id)
    assert normalized_rank.eyebrow == f"Rank {rank.rank}"
    assert normalized_title.eyebrow == "USGS-hosted study"
    assert normalized_title.annotation == ""
    assert normalized.series_label == "Ranks 5 TO 1"
    assert normalized_rank.headline == look.headline
    assert normalized_rank.annotation == look.annotation
    assert validate_asset_plan(p, normalized) is normalized


def test_procedural_map_drops_only_its_unused_image_assignment(tmp_path, brief, catalog):
    _, project = setup_project(tmp_path, brief, catalog)
    original = project.spec.shots[1]
    project.spec.shots[1] = MapShot.model_validate({
        **original.model_dump(mode="json", exclude={"kind", "rank", "metric", "image_ref"}),
        "kind": "map",
        "latitude": 11.35,
        "longitude": 142.2,
        "location_label": "Mariana Trench",
    })
    map_id = project.spec.shots[1].id
    plan = asyncio.run(Planner().plan(project))
    next(look for look in plan.display if look.shot_id == map_id).visual_strategy = "map"

    shared = plan.model_copy(deep=True)
    shared.images[0].shot_ids.append(map_id)
    normalized = normalize_asset_plan(project, shared)
    assert normalized.images[0].shot_ids == plan.images[0].shot_ids
    assert map_id in shared.images[0].shot_ids  # The saved response is never mutated.

    map_only = plan.model_copy(deep=True)
    map_only.images.append(ImageBrief(
        id="unneeded-map-background",
        prompt="An archival bathymetric view of the Mariana Trench for context.",
        shot_ids=[map_id],
        source_type="archival",
        search_query="Mariana Trench bathymetry",
    ))
    normalized = normalize_asset_plan(project, map_only)
    assert all(map_id not in image.shot_ids for image in normalized.images)
    assert len(normalized.images) == len(plan.images)
    assert validate_asset_plan(project, normalized) is normalized


def test_cached_plan_recovery_preserves_bounds_and_checks_version(tmp_path, brief, catalog):
    studio, p = setup_project(tmp_path, brief, catalog)
    plan = asyncio.run(Planner().plan(p))
    p.state, p.asset_plan_attempts, p.last_error = State.ASSETS_PREPARING, 2, "validation failed"
    studio.repo.save(p, "test_failed_plan", {})
    cache = studio.repo.project_dir(p.id) / "provider-cache" / "saved.json"
    write_json(cache, response(plan.model_dump_json()))
    with pytest.raises(ValueError, match="stale"):
        recover_asset_plan(studio, p.id, cache, version=99)
    with pytest.raises(ValueError, match="provider cache"):
        recover_asset_plan(studio, p.id, tmp_path / "elsewhere.json", version=p.version)
    recovered = recover_asset_plan(studio, p.id, cache, version=p.version)
    assert recovered.asset_plan == plan and recovered.asset_plan_attempts == 2
    assert recovered.image_requests == 0 and recovered.last_error is None
    assert not recovered.pending()
    assert all(studio.repo.verify(recovered, a) for a in recovered.current_artifacts())
    assert studio.repo.events(p.id)[-1]["payload"]["additional_api_calls"] == 0
    with pytest.raises(ValueError, match="accepted plan"):
        recover_asset_plan(studio, p.id, cache, version=p.version)


def test_cached_plan_recovery_repairs_conceptual_metric_without_rewriting_script(
    tmp_path, brief, catalog,
):
    studio, project = setup_project(tmp_path, brief, catalog)
    index = next(i for i, shot in enumerate(project.spec.shots) if shot.kind == "big_metric")
    original = project.spec.shots[index]
    original.metric = "Not proof"
    original.title = "Possibility is not proof"
    original.shot_type_reasoning = "This is a conceptual distinction, not a measurement."
    original.voice_segments = [VoiceSegment(
        shot_id=original.id,
        spoken_text="A possible route is not proof of what happened.",
        estimated_duration=2,
    )]
    script = project.spec.narration_script
    spoken = [segment.spoken_text for shot in project.spec.shots for segment in shot.voice_segments]
    plan = asyncio.run(Planner().plan(project))
    project.state = State.ASSETS_PREPARING
    project.asset_plan_attempts = 2
    project.last_error = "Asset planning failed after two attempts; revise the brief"
    project.last_error_code = "asset_planning"
    studio.repo.save(project, "test_failed_plan", {})
    cache = studio.repo.project_dir(project.id) / "provider-cache" / "saved.json"
    write_json(cache, response(plan.model_dump_json()))
    replacement = QualitativeComparisonShot.model_validate({
        **original.model_dump(mode="json", exclude={"kind", "metric"}),
        "kind": "qualitative_comparison",
        "alternatives": ["Modelled possibility", "Observed history"],
        "takeaway": "A possible route is not proof of the event.",
        "shot_type_reasoning": (
            "A two-part distinction explains that a simulated route is not evidence "
            "of what actually occurred."
        ),
    })
    changed_voice = replacement.model_copy(deep=True)
    changed_voice.voice_segments[0].spoken_text = "Changed narration"
    with pytest.raises(ValueError, match="approved narration or shot timing"):
        recover_asset_plan(
            studio, project.id, cache, version=project.version, visual_repair=changed_voice
        )
    assert studio.repo.load(project.id).spec.shots[index].kind == "big_metric"

    recovered = recover_asset_plan(
        studio, project.id, cache, version=project.version, visual_repair=replacement
    )
    assert recovered.spec.shots[index].kind == "qualitative_comparison"
    assert recovered.spec.narration_script == script
    assert [segment.spoken_text for shot in recovered.spec.shots
            for segment in shot.voice_segments] == spoken
    assert recovered.asset_plan_attempts == 2
    assert recovered.asset_plan is not None and recovered.last_error is None
    assert recovered.current_artifacts("visual_repair")
    assert studio.repo.events(project.id)[-1]["payload"]["additional_api_calls"] == 0


def test_rank_headline_fits_two_minimum_size_lines(tmp_path, brief, catalog):
    _, p = setup_project(tmp_path, brief, catalog)
    plan = asyncio.run(Planner().plan(p))
    rank = next(s for s in p.spec.shots if s.kind == "rank_reveal")
    look = next(s for s in plan.display if s.shot_id == rank.id)
    look.headline = "Tangshan, provisionally"
    assert validate_asset_plan(p, plan) is plan


def test_v06_visual_plan_covers_narration_and_matches_loop_art(tmp_path, catalog):
    brief = load_brief(Path("examples/earthquake-full.yaml"))
    _, project = setup_project(tmp_path, brief, catalog)
    source_duration = sum(s.duration for s in project.spec.shots if s.kind == "source_card")
    project.spec.shots = [s for s in project.spec.shots if s.kind != "source_card"]
    project.spec.shots[-1].duration += source_duration
    project.spec.shots[-1].title = project.spec.shots[0].title
    project.spec.loop = LoopSpec(
        strategy="semantic",
        bridge_line=project.spec.shots[-1].voice_segments[-1].spoken_text,
        match_opening_visual=True,
    )
    plan = asyncio.run(Planner().plan(project))
    narrated = [s.id for s in project.spec.shots if s.voice_segments]
    plan.images[0].shot_ids = [narrated[0], narrated[-1]]
    plan.images[1].shot_ids = narrated[1:-1]
    for look in plan.display:
        look.visual_strategy = "generated_image"
        look.visual_rationale = "The illustration directly depicts this narrated story beat."
    assert validate_asset_plan(project, plan) is plan
    plan.images[1].shot_ids.pop()
    with pytest.raises(ValueError, match="has no image"):
        validate_asset_plan(project, plan)


@pytest.mark.parametrize("manual_recovery", [False, True])
def test_render_only_refresh_rebuilds_current_publication_package(
    tmp_path, brief, catalog, manual_recovery,
):
    class GalleryRenderer(FakeRenderer):
        async def render(self, project, output):
            result = await super().render(project, output)
            qa_path = output / "qa.json"
            qa = yaml.safe_load(qa_path.read_text(encoding="utf-8"))
            qa["presentation"] = {"layout_passed": True}
            write_json(qa_path, qa)
            next(a for a in result.artifacts if a.role == "qa").sha256 = digest(qa_path)
            thumbnail_path = output / "thumbnail.png"
            Image.new("RGB", (180, 320), "#4b6170").save(thumbnail_path)
            thumbnail = self.repo.artifact(project, thumbnail_path, "thumbnail_candidate")
            gallery_path = output / "thumbnail-gallery.json"
            write_json(gallery_path, {"recommended_thumbnail": thumbnail.path})
            result.artifacts.extend([
                thumbnail,
                self.repo.artifact(project, gallery_path, "thumbnail_gallery"),
            ])
            return result

    studio, project = setup_project(
        tmp_path, brief, catalog, publication_clearance=True,
    )
    studio.renderer = GalleryRenderer(studio.repo)
    studio.asset_planner = Planner()
    http = FakeHTTP([image_response(), image_response()])
    client = OpenAIClient(Settings(api_key="test"), tmp_path / "image-cache", http)
    studio.auto_images = OpenAIImages(client, studio.repo)

    original = asyncio.run(studio.run(project.id))
    assert original.state == State.PROTOTYPE_APPROVAL
    original_title = studio.repo.verify(
        original, original.current_artifacts("youtube_title")[-1]
    ).read_text(encoding="utf-8")
    voice_hashes = [voice.asset.artifact.sha256 for voice in original.prepared_voices]
    image_calls = len(http.calls)

    refreshed = studio.refresh_preview(
        project.id, version=original.version, reason="Correct the visual comparison",
    )
    assert refreshed.state == State.AUDIO_PREPARED
    assert not refreshed.current_artifacts("youtube_title")
    assert not refreshed.current_artifacts("rights_manifest")
    assert [voice.asset.artifact.sha256 for voice in refreshed.prepared_voices] == voice_hashes

    preview = asyncio.run(studio.run(project.id))
    assert preview.state == State.PROTOTYPE_APPROVAL
    studio.decide(project.id, "prototype", "approve", version=preview.version)
    if manual_recovery:
        failed = studio.repo.load(project.id)
        failed.last_error = "publication package is missing youtube_title"
        studio.repo.save(failed, "simulated_legacy_publication_failure", {})
        recovered = recover_publication_package(
            studio, project.id, version=preview.version,
        )
        assert recovered.state == State.PUBLICATION_CLEARANCE
        assert not recovered.last_error
        assert recovered.current_artifacts("youtube_title")
    clearance = asyncio.run(studio.run(project.id))

    assert clearance.pending().gate == "publication"
    assert not clearance.last_error
    assert len(http.calls) == image_calls
    assert studio.repo.verify(
        clearance, clearance.current_artifacts("youtube_title")[-1]
    ).read_text(encoding="utf-8") == original_title
    for role in (
        "publication_description", "youtube_metadata", "source_manifest", "rights_manifest",
    ):
        assert clearance.current_artifacts(role)[-1].version == clearance.version
    rights = yaml.safe_load(studio.repo.verify(
        clearance, clearance.current_artifacts("rights_manifest")[-1]
    ).read_text(encoding="utf-8"))
    assert rights["version"] == clearance.version
    assert {item["artifact_id"] for item in rights["assets"]} <= {
        artifact.id for artifact in clearance.current_artifacts()
    }


@pytest.mark.parametrize("topic", ["earthquake-countdown", "solar-system", "earthquake-full"])
@pytest.mark.integration
def test_automatic_assets_end_to_end_and_restart(tmp_path, catalog, topic):
    brief = load_brief(Path(__file__).parents[1] / "examples" / f"{topic}.yaml")
    studio, p = setup_project(
        tmp_path,
        brief,
        catalog,
        publication_clearance=True,
        closed_captions=topic != "solar-system",
    )
    http = FakeHTTP([image_response(), image_response()])
    client = OpenAIClient(Settings(api_key="test"), tmp_path / "image-cache", http)
    planner = studio.asset_planner = Planner()

    class InterruptedImages(OpenAIImages):
        calls = 0

        async def generate(self, *args):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("simulated interruption before second dispatch")
            return await super().generate(*args)

    studio.auto_images = InterruptedImages(client, studio.repo)
    with pytest.raises(RuntimeError, match="interruption"):
        asyncio.run(studio.run(p.id))
    saved = studio.repo.load(p.id)
    assert saved.state == State.ASSETS_PREPARING and not saved.pending()
    assert len(saved.generated_visuals) == 1 and saved.image_requests == 1
    restarted = Studio(
        studio.repo.root, asset_planner=planner, auto_images=OpenAIImages(client, studio.repo)
    )
    completed = asyncio.run(restarted.run(p.id))
    assert completed.state == State.PROTOTYPE_APPROVAL
    assert completed.image_requests == 2 and len(completed.approvals) == 1
    assert planner.calls == 1 and len(http.calls) == 2
    assert completed.current_artifacts("asset_validation") and completed.current_artifacts(
        "prototype"
    )
    assert completed.current_artifacts("publication_description")
    assert completed.current_artifacts("youtube_title")
    assert completed.current_artifacts("youtube_metadata")
    assert completed.current_artifacts("source_manifest")
    assert completed.current_artifacts("rights_manifest")
    qa = yaml.safe_load(
        restarted.repo.verify(completed, completed.current_artifacts("qa")[-1]).read_text(
            encoding="utf-8"
        )
    )
    assert qa["closed_captions"] is (topic != "solar-system")
    gallery = completed.current_artifacts("thumbnail_gallery")[-1]
    gallery_data = yaml.safe_load(restarted.repo.verify(completed, gallery).read_text())
    candidates = completed.current_artifacts("thumbnail_candidate")
    assert len(candidates) == len(completed.spec.shots) + len(completed.generated_visuals)
    assert gallery_data["recommended_thumbnail"] == candidates[0].path
    description = restarted.repo.verify(
        completed, completed.current_artifacts("publication_description")[-1]
    ).read_text(encoding="utf-8")
    assert "Sources:" in description and "Music:" in description
    assert "#Shorts" in description and "Production note:" in description
    assert all(source.url in description for source in completed.spec.brief.research.sources)
    metadata = yaml.safe_load(
        restarted.repo.verify(
            completed, completed.current_artifacts("youtube_metadata")[-1]
        ).read_text(encoding="utf-8")
    )
    project_root = restarted.repo.project_dir(completed.id)
    assert not (project_root / "youtube-description.txt").exists()
    assert metadata["upload"]["status"]["privacyStatus"] == "private"
    assert metadata["upload"]["status"]["containsSyntheticMedia"] is True
    assert metadata["upload"]["snippet"]["categoryId"] == "28"
    assert metadata["upload"]["snippet"]["defaultAudioLanguage"] == "en"
    assert metadata["upload"]["paidProductPlacementDetails"]["hasPaidProductPlacement"] is False
    assert len(metadata["title"]) <= 100 and len(metadata["hashtags"]) <= 3
    assert asyncio.run(restarted.run(p.id)).revision == completed.revision
    assert len(http.calls) == 2
    assert all(restarted.repo.verify(completed, a).is_file() for a in completed.current_artifacts())
    cleared = restarted.decide(p.id, "prototype", "approve", actor="test-harness")
    assert cleared.state == State.PUBLICATION_CLEARANCE and not cleared.pending()
    pending = asyncio.run(restarted.run(p.id))
    assert pending.pending().gate == "publication"
    assert not (project_root / "final-video.mp4").exists()
    prototype_id = pending.current_artifacts("prototype")[-1].id
    revision = restarted.revise(
        p.id,
        "title: A clearer test title",
        actor="test-harness",
        approval_id=pending.pending().id,
    )
    assert revision.version == pending.version
    assert revision.current_artifacts("prototype")[-1].id == prototype_id
    pending = asyncio.run(restarted.run(p.id))
    assert pending.pending().gate == "publication"
    assert pending.publication.title == "A clearer test title"
    metadata = yaml.safe_load(
        restarted.repo.verify(pending, pending.current_artifacts("youtube_metadata")[-1]).read_text(
            encoding="utf-8"
        )
    )
    assert len(http.calls) == 2
    cleared = restarted.decide(p.id, "publication", "approve", actor="test-harness")
    assert cleared.state == State.COMPLETED
    expected = {
        "final-video.mp4",
        "youtube-title.txt",
        "youtube-description.txt",
        "youtube-metadata.json",
        "selected-thumbnail.png",
        "release-status.json",
        "usage-report.json",
    }
    assert expected <= {path.name for path in project_root.iterdir()}
    assert (project_root / "youtube-description.txt").read_text(encoding="utf-8") == description
    assert (
        yaml.safe_load((project_root / "youtube-metadata.json").read_text(encoding="utf-8"))
        == metadata
    )
    release = yaml.safe_load((project_root / "release-status.json").read_text(encoding="utf-8"))
    assert release["status"] == "cleared"
    assert release["approved_artifacts"] and release["checks"]["video_qa_passed"]
    usage = yaml.safe_load((project_root / "usage-report.json").read_text(encoding="utf-8"))
    assert usage["usage"]["image_reservations"] == 2
    assert usage["usage"]["image_successes"] == 2
    assert usage["usage"]["image_ambiguous_requests"] == 0
    assert usage["cost"]["estimated_cost"] is None
    assert usage["cost"]["unknown_cost_items"]
    assert (
        digest(project_root / "final-video.mp4")
        == pending.current_artifacts("prototype")[-1].sha256
    )
    assert digest(project_root / "selected-thumbnail.png") == next(
        artifact.sha256
        for artifact in pending.current_artifacts("thumbnail_candidate")
        if artifact.id == pending.publication.thumbnail_artifact_id
    )


def test_failed_planning_bound_and_topic_defaults(tmp_path, brief, catalog):
    studio, p = setup_project(tmp_path, brief, catalog)

    class InvalidPlanner:
        calls = 0

        async def plan(self, *args):
            self.calls += 1
            raise ValueError("invalid art plan")

    planner = studio.asset_planner = InvalidPlanner()
    for _ in range(2):
        with pytest.raises(AssetPreparationError, match="two attempts"):
            asyncio.run(studio.run(p.id))
    assert planner.calls == 2 and not studio.repo.load(p.id).pending()
    assert studio.repo.load(p.id).image_requests == 0
    live = Studio(tmp_path / "live", settings=Settings(api_key="test", music_catalog=str(catalog)))
    new = live.create_topic("A different subject")
    assert new.auto_assets.max_image_requests == 2 and new.review_policy == "preview"
    assert (
        "image_model" not in new.live_models
    )  # Image settings cannot invalidate speech signatures.


def test_full_project_creation_persists_explicit_budget_without_calls(tmp_path, catalog):
    settings = Settings(api_key="test", music_catalog=str(catalog))
    studio = Studio(tmp_path / "live", settings=settings)
    project = studio.create_topic(
        "Five ranked events",
        production_mode="full",
        duration=55,
        expected_rank_count=5,
        image_budget=4,
    )
    assert project.state == State.CREATED and project.spec.brief.production_mode == "full"
    assert project.spec.brief.expected_rank_count == 5
    assert project.auto_assets.max_image_requests == 4 and project.image_requests == 0
    assert not (studio.repo.project_dir(project.id) / "provider-cache").exists()
    with pytest.raises(ValidationError):
        studio.create_topic("Too much", production_mode="full", duration=55, image_budget=19)
