"""One-shot, isolated image trial for the approved Sun speech; never writes live state."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path

from PIL import Image, ImageOps

from autonomous_studio.config import Settings
from autonomous_studio.domain.models import VideoProject
from autonomous_studio.providers.http import HTTP
from autonomous_studio.providers.longform_media import longform_image_payload


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "projects/live/f2230c84373c/project.json"
OUTPUT = ROOT / "out/longform-image-review-f2230c84373c/earth-prediction"


def request_payload():
    project = VideoProject.model_validate_json(PROJECT.read_text(encoding="utf-8"))
    asset = next(
        item for item in project.longform_visual_plan.assets
        if item.id == "ch4-beat-1_image"
    )
    direction = asset.direction.model_copy(update={
        "visual_moment": (
            "Artist's conception of the modeled future scenario: the expanding "
            "outer atmosphere of a red giant Sun reaches and partly envelopes "
            "Earth at its orbit. Make the encroachment unmistakable without "
            "depicting a sudden impact or presenting the prediction as observation."
        ),
        "framing": (
            "Wide cinematic view of the immense red giant and a recognizable "
            "Earth at the advancing luminous edge of its outer atmosphere."
        ),
    })
    pilot = asset.model_copy(update={
        "prompt": (
            "A scientific editorial illustration of a 2008 calculation's predicted "
            "future scenario. Show an enlarged red giant Sun whose luminous outer "
            "envelope has reached Earth's orbit and begins to envelop the Earth. "
            "The event should be visually understandable, not merely two bodies "
            "near each other. This is an artist's conception of a prediction, not "
            "a recorded or certain event. One coherent frame; no text or labels."
        ),
        "direction": direction,
    })
    settings = Settings.environment()
    payload = longform_image_payload(project, pilot, settings)
    if payload["model"] != "gpt-image-2" or payload["n"] != 1:
        raise ValueError("This trial is authorized for one gpt-image-2 image only")
    if "A calculation published in 2008 predicts" not in payload["prompt"]:
        raise ValueError("The complete approved uncertainty qualifier is missing")
    return payload, settings


async def generate(payload, settings):
    attempt = OUTPUT / "attempt.json"
    if attempt.exists():
        raise ValueError("An image request was already attempted; inspect it before any retry")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    attempt.write_text(json.dumps({
        "model": payload["model"], "n": 1,
        "payload_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        "status": "request_sent_or_pending; do not repeat automatically",
    }, indent=2), encoding="utf-8")
    response = await HTTP().json(
        "https://api.openai.com/v1/images/generations", payload,
        {"Authorization": f"Bearer {settings.require_key()}"},
        timeout=300, service="OpenAI Images", attempts=1,
    )
    items = response.get("data") or []
    if len(items) != 1 or not items[0].get("b64_json"):
        raise ValueError("Provider returned no usable image; do not retry automatically")
    content = base64.b64decode(items[0]["b64_json"], validate=True)
    with Image.open(io.BytesIO(content)) as opened:
        opened.verify()
        if opened.format != "PNG" or opened.size != (1536, 1024):
            raise ValueError("Provider image format or dimensions were unexpected")
    (OUTPUT / "generated-original.png").write_bytes(content)
    with Image.open(io.BytesIO(content)) as opened:
        ImageOps.fit(opened.convert("RGB"), (1920, 1080), method=Image.Resampling.LANCZOS).save(
            OUTPUT / "generated-frame.png", format="PNG", optimize=True
        )
    (OUTPUT / "receipt.json").write_text(json.dumps({
        "model": payload["model"], "n": 1,
        "sha256": hashlib.sha256(content).hexdigest(),
        "usage": response.get("usage"),
    }, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate", action="store_true")
    args = parser.parse_args()
    payload, settings = request_payload()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "request.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if args.generate:
        asyncio.run(generate(payload, settings))
    else:
        print("Prepared one gpt-image-2 request. No API call made.")


if __name__ == "__main__":
    main()
