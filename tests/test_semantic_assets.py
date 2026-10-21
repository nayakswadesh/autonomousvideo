from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from pydantic import TypeAdapter

from autonomous_studio.providers.http import RemoteError
from autonomous_studio.providers.semantic_illustrations import SemanticIllustrations
from autonomous_studio.rendering import semantic_assets
from autonomous_studio.rendering.semantic_assets import (
    PHOSPHOR,
    default_catalog,
    raster_asset,
    resolve_scene_symbols,
    resolve_unresolved_scene_assets,
)
from autonomous_studio.rendering.semantic_diagrams import Scene, render_frame, scene_asset_report

ROOT = Path(__file__).resolve().parents[1]
STUDIES = ROOT / "examples/semantic_asset_studies.json"


def test_offline_svg_family_is_pinned_and_searchable():
    assert len(list((PHOSPHOR / "regular").glob("*.svg"))) >= 1500
    catalog = default_catalog()
    choice = catalog.select("books")
    assert choice.asset_id == "phosphor/books"
    assert catalog.get(choice.asset_id).license == "MIT"
    assert raster_asset(choice.asset_id, 72, "#2eb8c7").getbbox()
    assert catalog.search("light")
    assert all(record.kind == "svg" for record in catalog.search("light"))


def test_unknown_asset_uses_explicit_generic_fallback():
    catalog = default_catalog()
    choice = catalog.select("unmapped refinery tower")
    assert choice.match == "generic_fallback"
    assert choice.asset_id == "code/box"
    with pytest.raises(ValueError):
        catalog.get("phosphor/../../secret")


def test_explicit_required_illustration_never_silently_becomes_a_box():
    class NoAsset:
        async def resolve(self, concept):
            return None

    with pytest.raises(ValueError, match="required illustration could not be resolved"):
        asyncio.run(resolve_unresolved_scene_assets(
            {"parts": [{"symbol": "box", "asset_concept": "illustrate:Sun’s core"}]},
            NoAsset(),
        ))


def test_generated_cutout_stays_review_only_and_preserves_alpha():
    catalog = default_catalog()
    choice = catalog.select("solar streetlight")
    record = catalog.get(choice.asset_id)
    assert record.kind == "generated"
    assert record.status == "review_only"
    image = raster_asset(record.id, 220, "#2eb8c7")
    assert image.mode == "RGBA"
    assert image.getchannel("A").getextrema()[0] == 0


def test_eight_new_scripts_use_code_svg_and_generated_sources():
    scenes = TypeAdapter(list[Scene]).validate_json(STUDIES.read_text(encoding="utf-8"))
    assert len(scenes) == 8
    reports = [asset for scene in scenes for asset in scene_asset_report(scene)]
    assert {asset["kind"] for asset in reports} == {"code", "svg", "generated"}
    assert all(asset["match"] != "generic_fallback" for asset in reports)
    assert all(render_frame(scene, scene.duration * 0.8).size == (960, 540) for scene in scenes)
    assert any(asset["status"] == "review_only" for asset in reports)


def test_guided_callout_renders_its_asset_without_a_base_illustration():
    scenes = TypeAdapter(list[Scene]).validate_json(
        (ROOT / "examples/semantic_diagrams.json").read_text(encoding="utf-8")
    )
    scene = next(item for item in scenes if item.id == "water-pump-parts")
    assert scene.base_asset_concept is None
    without_icon = render_frame(scene, 5).tobytes()
    scene.parts[0].asset_concept = "sun"
    assert render_frame(scene, 5).tobytes() != without_icon


def test_unregistered_symbols_are_resolved_before_scene_validation():
    payload = {"left": {"symbol": "✕", "asset_concept": None}}
    resolved, decisions = resolve_scene_symbols(payload, neutralize_verdict=True)
    assert resolved["left"] == {"symbol": "box", "asset_concept": None}
    assert decisions[0]["reason"] == "neutral paired claims"
    resolved, _ = resolve_scene_symbols(
        {"left": {"symbol": "box", "asset_concept": "check"}}, neutralize_verdict=True
    )
    assert resolved["left"]["asset_concept"] is None
    resolved, decisions = resolve_scene_symbols({"left": {"symbol": "⚙", "asset_concept": "gear"}})
    assert resolved["left"] == {"symbol": "box", "asset_concept": "gear"}
    assert decisions[0]["asset_id"] == "phosphor/gear"


@pytest.mark.parametrize(
    "concept",
    [
        "refinery distillation tower",
        (
            "A detailed primitive meteorite fragment with one small calcium-aluminium-rich "
            "inclusion visible within the material; retain the whole fragment beside a "
            "magnified view of that same inclusion."
        ),
    ],
)
def test_generated_illustration_is_budgeted_receipted_and_reused(
    tmp_path, monkeypatch, concept
):
    monkeypatch.setattr(semantic_assets, "ROOT", tmp_path)
    image = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((200, 200, 824, 824), fill=(42, 180, 193, 255))
    memory = io.BytesIO()
    image.save(memory, format="PNG")
    calls = []

    class HTTP:
        async def json(self, url, payload, headers, **kwargs):
            calls.append((url, payload))
            return {"data": [{"b64_json": base64.b64encode(memory.getvalue()).decode()}]}

    class Repo:
        def project_dir(self, project_id):
            return tmp_path / "projects" / "live" / project_id

        def save(self, project, event, payload):
            pass

        def artifact(self, project, path, role, **kwargs):
            return SimpleNamespace(provenance=None)

    project = SimpleNamespace(
        id="123456abcdef", pipeline="live", live_models={"image_model": "gpt-image-2"},
        creation_settings={}, longform_image_requests=0,
        longform_visual_policy=SimpleNamespace(maximum_generated_requests=1),
        version=2, artifacts=[],
    )
    studio = SimpleNamespace(
        repo=Repo(), live_client=SimpleNamespace(
            http=HTTP(), settings=SimpleNamespace(require_key=lambda: "test-key")
        ),
    )
    resolver = SemanticIllustrations(studio, project)
    assert asyncio.run(resolver.resolve("refinery distillation tower")) is None
    asset_id = asyncio.run(resolver.resolve(f"illustrate:{concept}"))
    assert asset_id.startswith("generated/123456abcdef/")
    assert default_catalog().select(asset_id).asset_id == asset_id
    assert default_catalog().get(asset_id).kind == "generated"
    assert project.longform_image_requests == 1
    assert len(calls) == 1
    assert calls[0][1]["model"] == "gpt-image-2.5-flare"
    assert concept in calls[0][1]["prompt"]
    assert asyncio.run(resolver.resolve(f"illustrate:{concept}")) == asset_id
    assert len(calls) == 1
    assert asyncio.run(resolver.resolve("illustrate:another custom machine")) is None


def test_failed_illustration_is_not_silently_replaced_or_rebought(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic_assets, "ROOT", tmp_path)
    calls = []

    class HTTP:
        async def json(self, url, payload, headers, **kwargs):
            calls.append(payload)
            raise RemoteError("OpenAI Images", 400, provider_code="invalid_value")

    class Repo:
        def project_dir(self, project_id):
            return tmp_path / "projects" / project_id

        def save(self, project, event, payload):
            pass

    project = SimpleNamespace(
        id="123456abcdef", pipeline="live", live_models={}, creation_settings={},
        longform_image_requests=0,
        longform_visual_policy=SimpleNamespace(maximum_generated_requests=2),
        version=1,
    )
    resolver = SemanticIllustrations(
        SimpleNamespace(
            repo=Repo(),
            live_client=SimpleNamespace(
                http=HTTP(), settings=SimpleNamespace(require_key=lambda: "test-key")
            ),
        ),
        project,
    )
    with pytest.raises(RemoteError, match="HTTP 400"):
        asyncio.run(resolver.resolve("illustrate:chemical apparatus"))
    with pytest.raises(RuntimeError, match="unresolved"):
        asyncio.run(resolver.resolve("illustrate:chemical apparatus"))
    assert len(calls) == 1
