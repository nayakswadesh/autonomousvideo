from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from autonomous_studio.providers.focal_locator import locate_focal_feature
from autonomous_studio.rendering.semantic_assets import resolve_unresolved_scene_assets
from autonomous_studio.rendering.semantic_diagrams import CutawayScene, scene_asset_report
from autonomous_studio.workflow.semantic_scene_contract import build_scene_contract


def _beat():
    return {
        "id": "generic-feature", "screen_title": "Inside the subject",
        "overlay_text": ["The subject", "Distinct feature"],
        "overlay_anchors": ["The subject", "distinct feature"],
    }


def _scene():
    beat = _beat()
    contract = build_scene_contract(beat, "cutaway", "focal_inset")
    data = contract.assemble(
        {"symbol": "box", "base_asset_concept": "solar cross-section study",
         "parts": [{"detail": "", "asset_concept": "illustrate:unneeded second image",
                    "symbol": "book", "x": 0.9, "y": 0.1}]},
        beat=beat,
        narration="The subject contains a distinct feature near its center.",
        topic="Generic example", duration=7.0,
    )
    data["parts"][0]["at"] = 3.0
    return contract, data


def test_focal_inset_contract_requests_one_image_not_second_feature_or_guessed_coordinates():
    contract, data = _scene()
    fields = contract.response_schema["$defs"]["Part"]["properties"]
    assert not {"asset_concept", "symbol", "x", "y"} & fields.keys()
    assert data["parts"][0]["asset_concept"] is None
    assert data["parts"][0]["symbol"] == "box"
    assert (data["parts"][0]["x"], data["parts"][0]["y"]) == (0.5, 0.5)
    scene = CutawayScene.model_validate(data)
    report = scene_asset_report(scene)
    assert len(report) == 1
    assert report[0]["asset_id"] == "studio/solar-cross-section"


def test_focal_inset_resolver_ignores_unused_feature_asset():
    _, data = _scene()
    data["parts"][0]["asset_concept"] = "illustrate:unneeded second image"

    class Resolver:
        async def resolve(self, _concept):
            raise AssertionError("second focal-inset asset must not be requested")

    resolved, choices = asyncio.run(resolve_unresolved_scene_assets(data, Resolver()))
    assert resolved["parts"][0]["asset_concept"] is None
    assert choices == []


def test_focal_locator_uses_actual_illustration_and_approved_feature():
    class Client:
        settings = SimpleNamespace(semantic_layout_model="gpt-6-luna")

        async def response(self, model, prompt, **kwargs):
            assert model == "gpt-6-luna"
            assert "distinct core" in prompt[0]["content"][0]["text"]
            assert prompt[0]["content"][1]["image_url"].startswith("data:image/png;base64,")
            assert kwargs["schema"]["properties"]["x"]["type"] == "number"
            return {"status": "completed", "output": [{"content": [
                {"type": "output_text", "text": json.dumps({
                    "found": True, "x": 0.61, "y": 0.45,
                })}
            ]}]}

    located = asyncio.run(locate_focal_feature(
        Client(), "solar cross-section study", "distinct core"
    ))
    assert (located.x, located.y) == (0.61, 0.45)


def test_focal_locator_fails_when_requested_feature_is_not_visible():
    class Client:
        settings = SimpleNamespace(semantic_layout_model="gpt-6-luna")

        async def response(self, *_args, **_kwargs):
            return {"status": "completed", "output": [{"content": [
                {"type": "output_text", "text": '{"found":false,"x":0.5,"y":0.5}'}
            ]}]}

    with pytest.raises(ValueError, match="not visible"):
        asyncio.run(locate_focal_feature(
            Client(), "solar cross-section study", "feature not shown"
        ))
