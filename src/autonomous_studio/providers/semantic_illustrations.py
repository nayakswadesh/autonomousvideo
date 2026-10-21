"""Receipted, budgeted cutouts for otherwise unresolved semantic diagram concepts."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re

from PIL import Image

from autonomous_studio.domain.models import Provenance, now
from autonomous_studio.rendering.semantic_assets import default_catalog
from autonomous_studio.storage.repository import digest, write_json

UNSUITABLE = re.compile(
    r"\b(portrait|person|people|photograph|photo|archiv(?:e|al)|manuscript|"
    r"document|handwriting|map|logo|brand|signature)\b",
    re.IGNORECASE,
)
EMBEDDED_COPY = re.compile(
    r"\b(?:text|words?|letters?|labels?|labelled|labeled|captions?|typography|"
    r"write|written|spell(?:ed)?|numbers?|digits?)\b",
    re.IGNORECASE,
)


class SemanticIllustrations:
    def __init__(self, studio, project):
        self.studio = studio
        self.project = project

    async def resolve(self, concept: str) -> str | None:
        """Return a ready catalog ID, or None for the neutral labelled fallback."""
        catalog = default_catalog()
        choice = catalog.select(concept)
        if choice.match == "exact" and catalog.get(choice.asset_id).status == "ready":
            return concept
        # Generation is a paid, explicit planner request. An approximate icon
        # search or arbitrary unmatched phrase must not silently spend budget.
        if not concept.startswith("illustrate:"):
            return None
        concept = concept.removeprefix("illustrate:").strip()
        if EMBEDDED_COPY.search(concept):
            raise ValueError(
                "semantic illustration direction requests embedded text; "
                "approved typography must be rendered separately"
            )
        if (
            self.project.pipeline != "live"
            or len(concept) < 5
            or UNSUITABLE.search(concept)
            or not re.search(r"[A-Za-z]{3}", concept)
        ):
            return None
        # Diagram cutouts require alpha. Keep their model independent from the
        # general-purpose scene-image model, whose transparency may be unavailable.
        model = self.project.live_models.get(
            "semantic_illustration_model", "gpt-image-2.5-flare"
        )
        prompt = (
            "Create ONE isolated, transparent-background editorial diagram cutout of: "
            + concept
            + ". Use a crisp, restrained cyan-and-amber duotone vector-like style, "
            "frontal view, recognizable silhouette, no scene background. No text, labels, "
            "numbers, logos, watermarks, photographs, recognizable people, historical "
            "facsimiles, unsupported mechanisms, or fabricated evidence. This is an "
            "illustrative symbol, not an archival source or an exact scientific diagram."
        )
        payload = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
            "quality": "medium",
            "background": "transparent",
            "output_format": "png",
        }
        key = hashlib.sha256(
            json.dumps(
                {
                    "payload": payload,
                    "fresh_media_epoch": self.project.creation_settings.get("fresh_media_epoch"),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:24]
        asset_id = f"generated/{self.project.id}/{key}"
        folder = self.studio.repo.project_dir(self.project.id) / "semantic-illustrations"
        picture, receipt, journal = (
            folder / f"{key}{suffix}" for suffix in (".png", ".json", ".job.json")
        )
        if receipt.is_file():
            catalog.get(asset_id)  # verifies bytes against the durable receipt
            return asset_id
        if journal.exists():
            # A previous request may have incurred usage. Never repeat it blindly
            # or silently replace the requested illustration with a generic icon.
            raise RuntimeError(
                f"semantic illustration request {asset_id} is unresolved; "
                "inspect its request journal before retrying"
            )
        if (
            self.project.longform_image_requests
            >= self.project.longform_visual_policy.maximum_generated_requests
        ):
            return None
        folder.mkdir(parents=True, exist_ok=True)
        self.project.longform_image_requests += 1
        self.studio.repo.save(
            self.project,
            "semantic_illustration_request_reserved",
            {"asset_id": asset_id, "concept": concept, "version": self.project.version},
        )
        write_json(journal, {"asset_id": asset_id, "concept": concept, "reserved_at": now()})
        response = await self.studio.live_client.http.json(
            "https://api.openai.com/v1/images/generations",
            payload,
            {"Authorization": f"Bearer {self.studio.live_client.settings.require_key()}"},
            timeout=300,
            service="OpenAI Semantic Illustration",
            attempts=1,
        )
        items = response.get("data") or []
        if len(items) != 1 or not items[0].get("b64_json"):
            raise ValueError("semantic illustration provider returned no image")
        content = base64.b64decode(items[0]["b64_json"], validate=True)
        if len(content) > 25_000_000:
            raise ValueError("semantic illustration exceeds response size limit")
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            if image.format != "PNG" or image.size != (1024, 1024) or image.mode != "RGBA":
                raise ValueError("semantic illustration must be a transparent 1024px PNG")
            alpha = image.getchannel("A").getextrema()
            if alpha[0] == 255 or alpha[1] == 0:
                raise ValueError("semantic illustration has no usable cutout alpha")
        temporary = picture.with_suffix(".tmp")
        temporary.write_bytes(content)
        os.replace(temporary, picture)
        write_json(
            receipt,
            {
                "asset_id": asset_id,
                "concept": concept,
                "sha256": digest(picture),
                "source": "OpenAI Images API; illustrative diagram cutout",
                "model": model,
                "prompt": prompt,
                "created_at": now(),
                "usage": response.get("usage"),
                "technical_qa": "passed",
                "publication_review": "required",
            },
        )
        artifact = self.studio.repo.artifact(
            self.project,
            picture,
            "semantic_illustration",
            provider="openai",
            model=model,
            width=1024,
            height=1024,
        )
        artifact.provenance = Provenance(
            source="OpenAI Images API; generated diagram cutout",
            provider="openai",
            asset_id=asset_id,
            model=model,
            license_category="unknown",
            origin="generated",
            usage_restrictions="Illustrative only; review depiction and publication rights.",
        )
        self.project.artifacts.append(artifact)
        self.studio.repo.save(
            self.project,
            "semantic_illustration_prepared",
            {"asset_id": asset_id, "concept": concept, "version": self.project.version},
        )
        return asset_id
