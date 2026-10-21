"""Offline asset resolution for semantic diagrams, with provenance and safe fallbacks."""

from __future__ import annotations

import hashlib
import io
import json
import re
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Literal

import resvg_py
from PIL import Image
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[3]
PHOSPHOR = ROOT / "src/autonomous_studio/assets/semantic/phosphor"
CATALOG = ROOT / "src/autonomous_studio/assets/semantic/catalog.json"
CODE_SYMBOLS = {
    "people",
    "book",
    "branch",
    "pin",
    "drop",
    "cloud",
    "leaf",
    "pump",
    "signal",
    "box",
    "car",
    "question",
}
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
GENERATED_ID = re.compile(r"^generated/([0-9a-f]{12})/([0-9a-f]{24})$")


class AssetRecord(BaseModel):
    id: str
    kind: Literal["code", "svg", "generated"]
    label: str
    concepts: list[str] = Field(default_factory=list)
    path: str | None = None
    source: str
    license: str
    style: str
    status: Literal["ready", "review_only"]


class AssetChoice(BaseModel):
    requested_concept: str
    asset_id: str
    match: Literal["exact", "generic_fallback", "manual"]
    reason: str


class AssetCatalog:
    def __init__(self, records: list[AssetRecord]):
        self.records = {record.id: record for record in records}

    @classmethod
    def load(cls, path: Path = CATALOG):
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls([AssetRecord.model_validate(item) for item in data["assets"]])

    def get(self, asset_id: str) -> AssetRecord:
        generated = GENERATED_ID.fullmatch(asset_id)
        if generated:
            project_id, key = generated.groups()
            folder = ROOT / "projects" / "live" / project_id / "semantic-illustrations"
            receipt = folder / f"{key}.json"
            picture = folder / f"{key}.png"
            if not receipt.is_file() or not picture.is_file():
                raise ValueError(f"Generated illustration is missing: {asset_id}")
            metadata = json.loads(receipt.read_text(encoding="utf-8"))
            if (
                metadata.get("asset_id") != asset_id
                or metadata.get("sha256") != hashlib.sha256(picture.read_bytes()).hexdigest()
            ):
                raise ValueError(f"Generated illustration receipt mismatch: {asset_id}")
            return AssetRecord(
                id=asset_id,
                kind="generated",
                label=metadata["concept"],
                concepts=[asset_id],
                path=str(picture.relative_to(ROOT)),
                source=metadata["source"],
                license="AI-generated — publication rights and factual depiction require review",
                style="editorial-duotone",
                status="ready",
            )
        if asset_id.startswith("code/"):
            symbol = asset_id.removeprefix("code/")
            if symbol not in CODE_SYMBOLS:
                raise ValueError(f"Unknown code symbol: {symbol}")
            return AssetRecord(
                id=asset_id,
                kind="code",
                label=symbol,
                concepts=[symbol],
                source="autonomous_studio",
                license="project code",
                style="duotone",
                status="ready",
            )
        if asset_id.startswith("phosphor/"):
            name = asset_id.removeprefix("phosphor/")
            if not SAFE_ID.fullmatch(name):
                raise ValueError("Invalid SVG asset id")
            path = PHOSPHOR / "regular" / f"{name}.svg"
            if not path.is_file():
                raise ValueError(f"SVG not installed: {name}")
            curated = asset_id in self.records
            return (
                self.records[asset_id]
                if curated
                else AssetRecord(
                    id=asset_id,
                    kind="svg",
                    label=name.replace("-", " "),
                    concepts=[name.replace("-", " ")],
                    path=str(path.relative_to(ROOT)),
                    source=(
                        "Phosphor Icons pinned revision 2b75f3ad12b420c9504ef05df8d2564a28f8500e"
                    ),
                    license="MIT",
                    style="phosphor-regular",
                    status="ready",
                )
            )
        if asset_id not in self.records:
            raise ValueError(f"Unknown asset id: {asset_id}")
        return self.records[asset_id]

    def select(self, concept: str, *, allow_generated: bool = True) -> AssetChoice:
        key = concept.casefold().strip()
        if allow_generated and GENERATED_ID.fullmatch(key):
            record = self.get(key)
            return AssetChoice(
                requested_concept=concept,
                asset_id=record.id,
                match="exact",
                reason="receipted generated illustration",
            )
        matches = [
            record
            for record in self.records.values()
            if key in {item.casefold() for item in record.concepts}
            and (allow_generated or record.kind != "generated")
        ]
        matches.sort(key=lambda r: (r.status != "ready", r.kind == "generated"))
        if matches:
            chosen = matches[0]
            return AssetChoice(
                requested_concept=concept,
                asset_id=chosen.id,
                match="exact",
                reason="curated concept match",
            )
        # The pinned SVG bundle has a known licence and fixed paths. Resolve an
        # exact icon name without needing a hand-written catalog entry per icon.
        slug = re.sub(r"[\s_]+", "-", key.removeprefix("phosphor/"))
        if SAFE_ID.fullmatch(slug):
            try:
                record = self.get(f"phosphor/{slug}")
            except ValueError:
                pass
            else:
                return AssetChoice(
                    requested_concept=concept,
                    asset_id=record.id,
                    match="exact",
                    reason="exact icon in pinned SVG bundle",
                )
        return AssetChoice(
            requested_concept=concept,
            asset_id="code/box",
            match="generic_fallback",
            reason=(
                "no vetted subject illustration; use a labeled neutral shape or request an asset"
            ),
        )

    def search(self, query: str, *, limit: int = 12) -> list[AssetRecord]:
        """Find SVG candidates offline. A search hit is not an exact concept match."""
        tokens = [token for token in re.split(r"[^a-z0-9]+", query.casefold()) if token]
        if not tokens or limit < 1:
            return []
        candidates = []
        for path in (PHOSPHOR / "regular").glob("*.svg"):
            name = path.stem
            words = set(name.split("-"))
            score = sum(2 if token in words else 1 if token in name else 0 for token in tokens)
            if score:
                candidates.append((score, name))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return [self.get(f"phosphor/{name}") for _, name in candidates[:limit]]

    def path_for(self, record: AssetRecord) -> Path:
        if not record.path:
            raise ValueError(f"Asset has no file: {record.id}")
        path = (ROOT / record.path).resolve()
        if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
            raise ValueError(f"Unsafe or missing asset path: {record.id}")
        return path


SYMBOL_ALIASES = {
    "✓": "check",
    "✔": "check",
    "✕": "x",
    "✗": "x",
    "×": "x",
}


def resolve_scene_symbols(
    payload: dict, *, neutralize_verdict: bool = False, preserve_unresolved: bool = False
) -> tuple[dict, list[dict]]:
    """Resolve proposed glyphs before the strict scene schema validates them."""
    resolved = deepcopy(payload)
    catalog = default_catalog()
    decisions = []

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        if isinstance(node.get("symbol"), str):
            raw = node["symbol"].strip()
            concept_key = "base_asset_concept" if "base_asset_concept" in node else "asset_concept"
            concept = node.get(concept_key)
            if neutralize_verdict and (
                raw in SYMBOL_ALIASES
                or raw.casefold() in {"check", "x", "cross", "tick"}
                or str(concept).casefold() in {"check", "x", "cross", "tick"}
            ):
                node["symbol"], node[concept_key] = "box", None
                decisions.append(
                    {"requested": raw, "asset_id": "code/box", "reason": "neutral paired claims"}
                )
            elif raw in CODE_SYMBOLS:
                if concept:
                    choice = catalog.select(concept)
                    if (
                        choice.match == "generic_fallback"
                        or catalog.get(choice.asset_id).status != "ready"
                    ):
                        if not preserve_unresolved:
                            node[concept_key] = None
                        decisions.append(
                            {
                                "requested": concept,
                                "asset_id": f"code/{raw}",
                                "reason": "no exact ready asset",
                            }
                        )
            else:
                request = concept or SYMBOL_ALIASES.get(raw, raw)
                choice = catalog.select(request)
                record = catalog.get(choice.asset_id)
                node["symbol"] = "box"
                node[concept_key] = (
                    request
                    if choice.match == "exact" and record.status == "ready"
                    else concept
                    if preserve_unresolved and concept
                    else None
                )
                decisions.append(
                    {
                        "requested": raw,
                        "asset_id": record.id if node[concept_key] else "code/box",
                        "reason": choice.reason if node[concept_key] else "no exact ready asset",
                    }
                )
        for child in node.values():
            visit(child)

    visit(resolved)
    return resolved, decisions


async def resolve_unresolved_scene_assets(payload: dict, resolver) -> tuple[dict, list[dict]]:
    """Resolve explicit diagram concepts after scene validation, before evidence gating."""
    result = deepcopy(payload)
    if (result.get("family"), result.get("treatment")) == ("cutaway", "focal_inset"):
        for part in result.get("parts", []):
            part["asset_concept"] = None
    catalog = default_catalog()
    decisions = []
    cache: dict[str, str | None] = {}

    async def visit(node):
        if isinstance(node, list):
            for item in node:
                await visit(item)
            return
        if not isinstance(node, dict):
            return
        for key in (
            "asset_concept",
            "base_asset_concept",
            "expected_asset_concept",
            "observed_asset_concept",
            "inference_asset_concept",
        ):
            concept = node.get(key)
            if not isinstance(concept, str) or not concept.strip():
                continue
            choice = catalog.select(concept)
            if choice.match == "exact" and catalog.get(choice.asset_id).status == "ready":
                continue
            if concept not in cache:
                cache[concept] = await resolver.resolve(concept) if resolver else None
            if concept.startswith("illustrate:") and not cache[concept]:
                raise ValueError(
                    f"required illustration could not be resolved: {concept}"
                )
            node[key] = cache[concept]
            decisions.append(
                {
                    "requested": concept,
                    "asset_id": cache[concept] or "code/box",
                    "reason": "generated illustration"
                    if cache[concept]
                    else "neutral code fallback",
                }
            )
        for child in node.values():
            await visit(child)

    await visit(result)
    return result, decisions


@lru_cache(maxsize=1)
def default_catalog() -> AssetCatalog:
    return AssetCatalog.load()


def detailed_subject_asset(concept: str | None) -> bool:
    """A focal inset cannot magnify a generic icon as if it depicted real anatomy."""
    if not concept:
        return False
    catalog = default_catalog()
    choice = catalog.select(concept)
    if choice.match != "exact":
        return False
    record = catalog.get(choice.asset_id)
    return record.status == "ready" and (
        record.kind == "generated" or record.style.startswith("detailed-")
    )


@lru_cache(maxsize=256)
def raster_asset(asset_id: str, size: int, color: str) -> Image.Image:
    catalog = default_catalog()
    record = catalog.get(asset_id)
    if record.kind == "code":
        raise ValueError("Code symbols are drawn directly")
    path = catalog.path_for(record)
    if record.kind == "svg":
        svg = path.read_text(encoding="utf-8")
        if "<script" in svg.casefold() or "foreignobject" in svg.casefold():
            raise ValueError("SVG contains unsupported active content")
        svg = svg.replace("currentColor", color)
        data = resvg_py.svg_to_bytes(svg_string=svg, width=size, height=size)
        image = Image.open(io.BytesIO(data)).convert("RGBA")
    else:
        image = Image.open(path).convert("RGBA")
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
    return image
