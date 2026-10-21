"""Locate a spoken feature on the resolved image used by a focal inset."""

from __future__ import annotations

import base64
import io
import json

from pydantic import BaseModel, Field

from autonomous_studio.providers.openai import output_text
from autonomous_studio.rendering.semantic_assets import default_catalog, raster_asset


class FocalLocation(BaseModel):
    found: bool
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


async def locate_focal_feature(client, asset_concept: str, feature: str) -> FocalLocation:
    """Return coordinates in the *actual* base illustration, never a pre-image guess."""
    catalog = default_catalog()
    choice = catalog.select(asset_concept)
    if choice.match != "exact":
        raise ValueError("focal inset has no exact base illustration")
    record = catalog.get(choice.asset_id)
    if record.kind == "code":
        raise ValueError("focal inset cannot locate a feature in a code symbol")
    if record.kind == "svg":
        image = raster_asset(record.id, 1024, "#ffffff")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        picture = buffer.getvalue()
    else:
        picture = catalog.path_for(record).read_bytes()
    prompt = (
        "Locate the visible center of this feature in the attached illustration: "
        f"{feature}. Return x and y normalized to the image width and height, "
        "with the top-left at 0,0. Mark found false if the feature cannot be "
        "visually identified; do not infer its position from the story. "
        "The answer determines a magnified crop of this same illustration."
    )
    response = await client.response(
        client.settings.semantic_layout_model,
        [{"role": "user", "content": [
            {"type": "input_text", "text": prompt},
            {"type": "input_image", "image_url":
             "data:image/png;base64," + base64.b64encode(picture).decode("ascii")},
        ]}],
        schema={"type": "object", "properties": {
            "found": {"type": "boolean"},
            "x": {"type": "number"},
            "y": {"type": "number"},
        }, "required": ["found", "x", "y"]},
        reasoning_effort="low",
    )
    location = FocalLocation.model_validate(json.loads(output_text(response)))
    if not location.found:
        raise ValueError(f"focal inset feature is not visible in its base illustration: {feature}")
    return location
