"""Acquire rights-compatible documentary stills from Wikimedia Commons."""

from __future__ import annotations

import hashlib
import html
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from PIL import Image, ImageOps

from autonomous_studio.domain.models import Artifact, Provenance
from autonomous_studio.storage.repository import write_json


class ArchivalMediaError(ValueError):
    pass


@dataclass
class AcquiredMedia:
    visual: Artifact
    original: Artifact
    metadata: Artifact
    edit_manifest: Artifact


def _plain(value) -> str:
    if isinstance(value, dict):
        value = value.get("value", "")
    value = html.unescape(str(value or ""))
    return " ".join(re.sub(r"<[^>]+>", " ", value).split())


def _field(metadata, name) -> str:
    return _plain((metadata or {}).get(name, {}))


def _compatible_license(metadata) -> tuple[str, str] | None:
    short = _field(metadata, "LicenseShortName")
    url = _field(metadata, "LicenseUrl")
    folded = short.casefold().replace("-", " ")
    if "public domain" in folded or "cc0" in folded:
        return short or "Public domain", url
    if "cc by" in folded and not any(term in folded for term in ("nc", "nd", "sa")):
        return short, url
    return None


def archival_fingerprint(image) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "provider": "wikimedia-commons",
                "query": image.search_query.strip(),
                "image_id": image.id,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


class WikimediaCommonsMedia:
    """Search Commons, retain the original, and create a traceable portrait derivative."""

    endpoint = "https://commons.wikimedia.org/w/api.php"

    def __init__(self, http, repo):
        self.http, self.repo = http, repo

    async def acquire(self, project, image, output: Path) -> AcquiredMedia:
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "generator": "search",
            "gsrnamespace": "6",
            "gsrlimit": "12",
            "gsrsearch": image.search_query.strip() + " filetype:bitmap",
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": "1600",
            "origin": "*",
        }
        query_url = self.endpoint + "?" + urlencode(params)
        response = await self.http.get_json(
            query_url,
            {"User-Agent": "AutonomousStudio/0.9 (local documentary production)"},
            timeout=60,
            service="Wikimedia Commons",
        )
        candidates = []
        for page in (response.get("query") or {}).get("pages", []):
            info = (page.get("imageinfo") or [{}])[0]
            metadata = info.get("extmetadata") or {}
            license_info = _compatible_license(metadata)
            if not license_info:
                continue
            mime = info.get("mime", "")
            if mime not in {"image/jpeg", "image/png"}:
                continue
            url = info.get("thumburl") or info.get("url")
            source_page = info.get("descriptionurl")
            if not url or not source_page:
                continue
            candidates.append((page, info, metadata, license_info, url, source_page))
        if not candidates:
            raise ArchivalMediaError(
                "No compatible public-domain, CC0 or CC BY archival image was found"
            )

        page, info, metadata, license_info, url, source_page = candidates[0]
        if urlsplit(url).scheme != "https" or urlsplit(source_page).scheme != "https":
            raise ArchivalMediaError("Archival media requires HTTPS source URLs")
        content = await self.http.get(
            url,
            {"User-Agent": "AutonomousStudio/0.9 (local documentary production)"},
            timeout=120,
            service="Wikimedia Commons media",
            attempts=2,
        )
        if len(content) > 25_000_000:
            raise ArchivalMediaError("Archival image exceeds the 25 MB source cap")
        try:
            with Image.open(io.BytesIO(content)) as opened:
                opened.load()
                if opened.format not in {"JPEG", "PNG"} or min(opened.size) < 400:
                    raise ValueError("unsupported image format or resolution")
                source_format = opened.format
                source_width, source_height = opened.size
                portrait = ImageOps.fit(
                    opened.convert("RGB"),
                    (864, 1536),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
        except (OSError, ValueError) as exc:
            raise ArchivalMediaError("Archival source failed image validation") from exc

        suffix = ".jpg" if source_format == "JPEG" else ".png"
        original_path = output.with_name(output.stem + ".original" + suffix)
        original_path.write_bytes(content)
        portrait.save(output, format="PNG", optimize=True)

        license_name, license_url = license_info
        creator = _field(metadata, "Artist") or _field(metadata, "Credit") or "Unknown creator"
        title = _field(metadata, "ObjectName") or _plain(page.get("title"))
        attribution = f"{title} — {creator}; {license_name}"
        if license_url:
            attribution += f" ({license_url})"
        restrictions = _field(metadata, "Restrictions")
        provenance = Provenance(
            source=source_page,
            provider="wikimedia-commons",
            asset_id=str(page.get("pageid", page.get("title", "commons"))),
            model="media-search-v1",
            license_category="licensed",
            usage_restrictions=(
                f"Derivative permitted under {license_name}. Verify personality, privacy, "
                "trademark and contextual-use rights before publication."
                + (f" Source restrictions: {restrictions}" if restrictions else "")
            ),
            attribution=attribution,
            origin="library",
        )
        original = self.repo.artifact(
            project,
            original_path,
            "archival_source",
            provider="wikimedia-commons",
            model="media-search-v1",
            width=source_width,
            height=source_height,
        )
        original.provenance = provenance.model_copy(deep=True)
        visual = self.repo.artifact(
            project,
            output,
            "visual_asset",
            provider="wikimedia-commons",
            model="portrait-crop-v1",
            width=864,
            height=1536,
        )
        visual.provenance = provenance.model_copy(
            deep=True,
            update={
                "source": "Portrait derivative of " + source_page,
                "asset_id": original.id,
            },
        )
        visual.input_reference = original.id

        metadata_path = output.with_suffix(".source.json")
        write_json(
            metadata_path,
            {
                "schema_version": 1,
                "provider": "wikimedia-commons",
                "query": image.search_query,
                "source_page": source_page,
                "download_url": url,
                "page_id": page.get("pageid"),
                "title": title,
                "creator": creator,
                "license": license_name,
                "license_url": license_url,
                "attribution": attribution,
                "commercial_use": True,
                "derivatives_allowed": True,
                "personality_privacy_review": "required",
                "original_artifact_id": original.id,
                "visual_artifact_id": visual.id,
            },
        )
        metadata_artifact = self.repo.artifact(
            project, metadata_path, "archival_metadata", provider="wikimedia-commons"
        )
        metadata_artifact.provenance = provenance.model_copy(deep=True)
        edit_path = output.with_suffix(".edit.json")
        write_json(
            edit_path,
            {
                "schema_version": 1,
                "operation": "center-crop-and-resize",
                "source_artifact_id": original.id,
                "output_artifact_id": visual.id,
                "output_dimensions": [864, 1536],
                "content_alteration": "No objects or people were added or removed.",
            },
        )
        edit_artifact = self.repo.artifact(
            project, edit_path, "visual_edit_manifest", provider="local", model="v0.9"
        )
        return AcquiredMedia(visual, original, metadata_artifact, edit_artifact)
