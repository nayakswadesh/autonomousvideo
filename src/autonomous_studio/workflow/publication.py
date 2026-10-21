"""Create and clear a deterministic, reviewable YouTube publication package."""

import json
import os
import re
import shutil
from collections import defaultdict
from pathlib import Path
from uuid import uuid4

from autonomous_studio.domain.models import LongFormSpec, PublicationPackage, State, now
from autonomous_studio.storage.repository import digest, write_json

STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "before",
    "biggest",
    "by",
    "for",
    "from",
    "greatest",
    "how",
    "in",
    "into",
    "is",
    "it",
    "largest",
    "most",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "this",
    "to",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}


def _clean(value: str) -> str:
    return " ".join(value.split()).strip()


def _limit(value: str, length: int) -> str:
    value = _clean(value)
    if len(value) <= length:
        return value
    shortened = value[: length + 1].rsplit(" ", 1)[0].rstrip(" —:;,.!?")
    return shortened[:length]


def _write_latest_text(path, value):
    """Atomically refresh a human-facing convenience export outside immutable artifacts."""
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def title_candidates(project) -> list[str]:
    brief = project.spec.brief
    topic = _clean(brief.title or brief.topic)
    hook = _clean(project.spec.hook or brief.hook)
    choices = []
    if hook and topic.casefold() not in hook.casefold():
        choices.append(_limit(f"{hook} — {topic}", 100))
    choices.extend([_limit(hook, 100), _limit(topic, 100)])
    choices.extend(_limit(candidate, 100) for candidate in project.spec.candidate_hooks)
    return list(dict.fromkeys(choice for choice in choices if choice))[:5]


def hashtags(project) -> list[str]:
    words = [
        word
        for word in re.findall(r"[A-Za-z0-9]+", project.spec.brief.topic)
        if len(word) >= 3 and word.casefold() not in STOP_WORDS and not word.isdigit()
    ]
    unique = list(dict.fromkeys(word.casefold() for word in words))
    unique.sort(key=lambda word: -len(word))
    result = ["#Shorts"]
    for word in unique:
        tag = "#" + word[:30].title()
        if tag not in result:
            result.append(tag)
        if len(result) == 3:
            break
    return result


def _description(project, music_source, selected) -> str:
    spec, brief = project.spec, project.spec.brief
    opening = _clean(spec.hook or brief.hook)
    body = []
    for value in (spec.thesis or brief.thesis, spec.payoff or brief.payoff):
        sentence = _clean(value)
        if sentence and sentence.casefold() not in {item.casefold() for item in body}:
            body.append(sentence)
    if not body:
        body.append(f"A concise documentary Short about {_clean(brief.topic)}.")
    lines = [
        opening,
        "",
        " ".join(body),
        "",
        "Watch to the end, then tell us which part deserves a deeper story.",
        "",
        "Sources:",
    ]
    lines.extend(f"- {source.title}: {source.url}" for source in brief.research.sources)
    visual_credits = list(
        dict.fromkeys(
            artifact.provenance.attribution
            for artifact in project.current_artifacts("visual_asset")
            if artifact.provenance.origin == "library" and artifact.provenance.attribution
        )
    )
    if visual_credits:
        lines.extend(["", "Visual credits:"])
        lines.extend(f"- {credit}" for credit in visual_credits)
    lines.extend(["", "Music:"])
    title = getattr(selected, "title", "")
    artist = getattr(selected, "artist", "")
    library = getattr(selected, "source_library", "legacy")
    if library == "youtube_audio_library" and title and artist:
        lines.append(f"- {title} — {artist} (YouTube Audio Library)")
        if getattr(selected, "attribution_required", False):
            lines.append(music_source.provenance.attribution)
    else:
        lines.append(music_source.provenance.attribution)
        license_url = getattr(selected, "license_url", "")
        if license_url:
            lines.append(license_url)
    lines.extend(
        [
            "",
            "Production note:",
            "AI-generated narration and editorial illustrations are used; illustrations are "
            "not archival footage.",
            "",
            " ".join(hashtags(project)),
        ]
    )
    description = "\n".join(lines).strip() + "\n"
    if len(description) > 5000:
        raise ValueError("YouTube description exceeds the 5,000-character limit")
    return description


def create_publication_package(repo, project, output, music_source, selected, *, category_id="28"):
    """Write viewer metadata plus separate evidence manifests; never burn these into frames."""
    if not re.fullmatch(r"\d{1,3}", category_id):
        raise ValueError("YouTube category ID must contain 1–3 digits")
    candidates = title_candidates(project)
    description = _description(project, music_source, selected)
    title_path = output / "youtube-title.txt"
    title_path.write_text(candidates[0] + "\n", encoding="utf-8")
    project.artifacts.append(repo.artifact(project, title_path, "youtube_title", model="v0.8"))
    description_path = output / "youtube-description.txt"
    description_path.write_text(description, encoding="utf-8")
    project.artifacts.append(
        repo.artifact(project, description_path, "publication_description", model="v0.8")
    )

    metadata_path = output / "youtube-metadata.json"
    metadata = {
        "schema_version": 1,
        "title": candidates[0],
        "title_candidates": candidates,
        "description": description,
        "hashtags": hashtags(project),
        "upload": {
            "snippet": {
                "title": candidates[0],
                "description": description,
                "tags": [tag.removeprefix("#") for tag in hashtags(project)],
                "categoryId": category_id,
                "defaultLanguage": project.spec.brief.voice.language,
                "defaultAudioLanguage": project.spec.brief.voice.language,
            },
            "status": {
                "privacyStatus": "private",
                "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": True,
            },
            "paidProductPlacementDetails": {"hasPaidProductPlacement": False},
        },
        "review_notes": [
            "Confirm the recommended title and thumbnail before upload.",
            "Synthetic-media disclosure is enabled because the production uses generated media.",
        ],
    }
    write_json(metadata_path, metadata)
    project.artifacts.append(
        repo.artifact(project, metadata_path, "youtube_metadata", model="v0.8")
    )

    source_path = output / "source-manifest.json"
    write_json(
        source_path,
        {
            "schema_version": 1,
            "project_id": project.id,
            "version": project.version,
            "sources": [
                source.model_dump(mode="json") for source in project.spec.brief.research.sources
            ],
            "claims": [
                claim.model_dump(mode="json") for claim in project.spec.brief.research.claims
            ],
        },
    )
    project.artifacts.append(repo.artifact(project, source_path, "source_manifest", model="v0.8"))

    rights_path = output / "rights-manifest.json"
    relevant = [
        artifact
        for artifact in project.current_artifacts()
        if artifact.role
        in {
            "visual_asset",
            "archival_source",
            "archival_metadata",
            "visual_edit_manifest",
            "music_source",
            "voice",
        }
    ]
    unknown = [
        artifact.id for artifact in relevant if artifact.provenance.license_category == "unknown"
    ]
    context_review = [
        artifact.id
        for artifact in relevant
        if artifact.role == "visual_asset" and artifact.provenance.origin == "library"
    ]
    write_json(
        rights_path,
        {
            "schema_version": 1,
            "project_id": project.id,
            "version": project.version,
            "status": "review_required" if unknown or context_review else "documented",
            "unknown_artifact_ids": unknown,
            "context_review_artifact_ids": context_review,
            "assets": [
                {
                    "artifact_id": artifact.id,
                    "role": artifact.role,
                    "sha256": artifact.sha256,
                    "provider": artifact.provider,
                    "provenance": artifact.provenance.model_dump(mode="json"),
                }
                for artifact in relevant
            ],
        },
    )
    project.artifacts.append(repo.artifact(project, rights_path, "rights_manifest", model="v0.8"))


def _current(project, role):
    values = project.current_artifacts(role)
    if not values:
        raise ValueError(f"publication package is missing {role}")
    return values[-1]


def ensure_publication_package(repo, project, output):
    """Rebuild versioned metadata after a render-only refresh, without provider calls."""
    required = {
        "youtube_title",
        "publication_description",
        "youtube_metadata",
        "source_manifest",
        "rights_manifest",
    }
    if required <= {artifact.role for artifact in project.current_artifacts()}:
        return False

    from autonomous_studio.providers.music_catalog import CatalogTrack

    music_ref = project.spec.brief.music.artifact_ref
    music_source = next(
        (
            artifact
            for artifact in project.current_artifacts("music_source")
            if artifact.id == music_ref
        ),
        None,
    )
    if music_source is None:
        raise ValueError("publication package cannot be rebuilt without current music source")
    selection = json.loads(
        repo.verify(project, _current(project, "music_selection")).read_text(encoding="utf-8")
    )
    selected = CatalogTrack.model_validate(selection["selected"])
    create_publication_package(
        repo,
        project,
        output,
        music_source,
        selected,
        category_id=str(project.creation_settings.get("youtube_category_id", "28")),
    )
    return True


def recover_publication_package(studio, project_id, *, version, actor="local-operator"):
    """Repair an already-approved preview's missing metadata and queue clearance."""
    repo = studio.repo
    with repo.lock(project_id):
        project = repo.load(project_id)
        if project.version != version:
            raise ValueError("stale publication recovery version")
        if (
            project.state != State.PUBLICATION_CLEARANCE
            or project.pending()
            or not (project.last_error or "").startswith("publication package is missing")
        ):
            raise ValueError("publication recovery requires a missing-package clearance error")
        output = repo.attempt_dir(project, "publication-package-recovery")
        if not ensure_publication_package(repo, project, output):
            raise ValueError("current publication package is already complete")
        project.last_error = project.last_error_code = None
        repo.save(
            project,
            "publication_package_recovered",
            {"version": project.version, "actor": actor, "provider_calls": 0},
        )
        return project


def is_metadata_revision(message: str) -> bool:
    return bool(re.search(r"(?im)^(title|description|thumbnail)\s*:", message.strip()))


def revise_publication_package(repo, project, output, message):
    """Apply explicit metadata fields without rerunning any creative provider."""
    fields = {
        match.group(1).lower(): match.group(2).strip()
        for match in re.finditer(
            r"(?ims)^(title|description|thumbnail)\s*:\s*(.*?)"
            r"(?=^(?:title|description|thumbnail)\s*:|\Z)",
            message.strip(),
        )
    }
    if not fields:
        raise ValueError("metadata feedback requires title:, description:, or thumbnail:")
    metadata = json.loads(
        repo.verify(project, _current(project, "youtube_metadata")).read_text(encoding="utf-8")
    )
    title = _clean(fields.get("title", metadata["title"]))
    description = fields.get("description", metadata["description"]).strip() + "\n"
    if not title or len(title) > 100:
        raise ValueError("YouTube title must contain 1–100 characters")
    if len(description) > 5000:
        raise ValueError("YouTube description exceeds the 5,000-character limit")
    if len(re.findall(r"(?<!\w)#[A-Za-z0-9_]+", description)) > 3:
        raise ValueError("YouTube description may contain at most three hashtags")
    metadata["title"] = title
    metadata["description"] = description
    metadata["hashtags"] = re.findall(r"(?<!\w)#[A-Za-z0-9_]+", description)
    metadata["upload"]["snippet"].update(
        {
            "title": title,
            "description": description,
            "tags": [tag.removeprefix("#") for tag in metadata["hashtags"]],
        }
    )
    title_path = output / "youtube-title.txt"
    title_path.write_text(title + "\n", encoding="utf-8")
    description_path = output / "youtube-description.txt"
    description_path.write_text(description, encoding="utf-8")
    metadata_path = output / "youtube-metadata.json"
    write_json(metadata_path, metadata)
    title_artifact = repo.artifact(project, title_path, "youtube_title", model="v0.8-revision")
    description_artifact = repo.artifact(
        project, description_path, "publication_description", model="v0.8-revision"
    )
    metadata_artifact = repo.artifact(
        project, metadata_path, "youtube_metadata", model="v0.8-revision"
    )
    project.artifacts.extend([title_artifact, description_artifact, metadata_artifact])
    thumbnail = None
    if fields.get("thumbnail"):
        candidates = project.current_artifacts("thumbnail_candidate")
        requested = fields["thumbnail"].strip()
        if requested.isdigit() and 1 <= int(requested) <= len(candidates):
            thumbnail = candidates[int(requested) - 1]
        else:
            thumbnail = next(
                (
                    item
                    for item in candidates
                    if requested.casefold()
                    in {
                        Path(item.path).name.casefold(),
                        Path(item.path).stem.casefold(),
                    }
                ),
                None,
            )
        if thumbnail is None:
            raise ValueError("thumbnail must be a candidate number, filename, or filename stem")
    if project.publication:
        project.publication.title = title
        project.publication.description = description
        project.publication.hashtags = metadata["hashtags"]
        project.publication.title_artifact_id = title_artifact.id
        project.publication.description_artifact_id = description_artifact.id
        project.publication.metadata_artifact_id = metadata_artifact.id
        if thumbnail:
            project.publication.thumbnail_artifact_id = thumbnail.id


def _provider_usage(repo, project, *, all_versions=False):
    """Aggregate unique provider responses without guessing a dollar price."""
    responses = {}
    artifacts = project.artifacts if all_versions else project.current_artifacts()
    for artifact in artifacts:
        if artifact.role != "provider_usage":
            continue
        payload = json.loads(repo.verify(project, artifact).read_text(encoding="utf-8"))
        for entry in payload if isinstance(payload, list) else []:
            response = entry.get("response", {})
            response_id = response.get("id")
            if response_id:
                responses[response_id] = response
    if all_versions:
        # A failed validation stage may never commit a provider_usage artifact. Its
        # response receipt is still an incurred request and belongs in lifetime usage.
        cache = repo.project_dir(project.id) / "provider-cache"
        for path in cache.glob("*.json"):
            if path.stat().st_size > 2_000_000:
                continue
            try:
                response = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(response, dict) and response.get("id") and response.get("usage"):
                responses[response["id"]] = response
    models = defaultdict(
        lambda: {
            "requests": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "audio_tokens": 0,
        }
    )
    web_searches = 0
    for response in responses.values():
        model = response.get("model", "unknown")
        usage = response.get("usage") or {}
        item = models[model]
        item["requests"] += 1
        item["input_tokens"] += usage.get("input_tokens", 0) or 0
        item["output_tokens"] += usage.get("output_tokens", 0) or 0
        item["cached_input_tokens"] += (usage.get("input_tokens_details") or {}).get(
            "cached_tokens", 0
        ) or 0
        item["audio_tokens"] += (
            (usage.get("input_tokens_details") or {}).get("audio_tokens", 0) or 0
        ) + ((usage.get("output_tokens_details") or {}).get("audio_tokens", 0) or 0)
        web_searches += sum(
            output.get("type") == "web_search_call"
            for output in response.get("output", [])
            if isinstance(output, dict)
        )
    return responses, dict(models), web_searches


def create_usage_report(repo, project, output, *, rate_card: str | None = None):
    responses, models, web_searches = _provider_usage(repo, project)
    lifetime_responses, lifetime_models, lifetime_searches = _provider_usage(
        repo, project, all_versions=True
    )
    longform = isinstance(project.spec, LongFormSpec)
    voice = [asset for asset in project.spec.audio_assets if asset.kind == "voice"]
    costs = [artifact.cost for artifact in project.current_artifacts() if artifact.cost]
    events = [
        event
        for event in repo.events(project.id)
        if event["payload"].get("version") == project.version
    ]
    reserved = {
        event["payload"].get("fingerprint")
        for event in events
        if event["kind"] == "image_request_reserved"
    }
    prepared = {
        visual.fingerprint
        for visual in project.generated_visuals
        if visual.artifact.version == project.version
    }
    image_receipts = []
    for artifact in project.current_artifacts("image_usage"):
        image_receipts.append(
            json.loads(repo.verify(project, artifact).read_text(encoding="utf-8"))
        )
    unknown_cost_items = sorted(
        {
            f"{artifact.provider}:{artifact.model}:{artifact.role}"
            for artifact in project.current_artifacts()
            if artifact.cost is None
        }
    )
    report = {
        "schema_version": 1,
        "project_id": project.id,
        "project_version": project.version,
        "generated_at": now(),
        "usage": {
            "provider_requests": len(responses),
            "provider_request_ids": sorted(responses),
            "models": models,
            "web_search_calls": web_searches,
            "voice_requests": len(project.longform_voices) if longform else len(voice),
            "voice_characters": (
                sum(asset.characters for asset in project.longform_voices.values())
                if longform else sum(asset.characters for asset in voice)
            ),
            "voice_seconds": round(
                sum(asset.duration for asset in project.longform_voices.values())
                if longform else sum(asset.duration for asset in voice), 3
            ),
            "image_requests": (
                project.longform_image_requests if longform else project.image_requests
            ),
            "image_reservations": len(reserved),
            "image_successes": len(prepared),
            "image_cache_hits": sum(bool(item.get("cache_hit")) for item in image_receipts),
            "image_ambiguous_requests": len(reserved - prepared),
            "render_seconds": round(
                sum(a.latency_seconds for a in project.current_artifacts("prototype")), 3
            ),
        },
        "lifetime_usage": {
            "provider_requests": len(lifetime_responses),
            "provider_request_ids": sorted(lifetime_responses),
            "models": lifetime_models,
            "web_search_calls": lifetime_searches,
            "voice_artifacts": sum(a.role == "voice" for a in project.artifacts),
            "image_usage_receipts": sum(a.role == "image_usage" for a in project.artifacts),
            "note": (
                "Includes prior versions and cached failed responses; TTS charges may be unknown."
            ),
        },
        "cost": {
            "currency": "USD",
            "provider_reported_cost": round(sum(costs), 6) if costs else None,
            "estimated_cost": None,
            "status": "partially_known" if costs else "unknown",
            "note": "Unknown charges remain unknown; the studio does not invent prices.",
            "rate_card_source": None,
            "rate_card_effective_at": None,
            "rate_card_sha256": None,
            "calculation": None,
            "unknown_cost_items": unknown_cost_items,
        },
    }
    if rate_card:
        source = Path(rate_card).resolve()
        if source.is_file():
            card = json.loads(source.read_text(encoding="utf-8"))
            estimate = 0.0
            lifetime_estimate = 0.0
            missing = []
            for model, usage in models.items():
                prices = card.get("models", {}).get(model)
                if not prices:
                    missing.append(model)
                    continue
                estimate += usage["input_tokens"] / 1_000_000 * prices["input_per_million"]
                estimate += usage["output_tokens"] / 1_000_000 * prices["output_per_million"]
            for model, usage in lifetime_models.items():
                prices = card.get("models", {}).get(model)
                if not prices:
                    missing.append(model)
                    continue
                lifetime_estimate += usage["input_tokens"] / 1_000_000 * prices["input_per_million"]
                lifetime_estimate += (
                    usage["output_tokens"] / 1_000_000 * prices["output_per_million"]
                )
            report["cost"].update(
                {
                    "estimated_cost": round(estimate, 6),
                    "estimated_lifetime_text_cost": round(lifetime_estimate, 6),
                    "status": "estimated" if not missing else "partially_estimated",
                    "rate_card_source": str(source),
                    "rate_card_effective_at": card.get("effective_at"),
                    "rate_card_sha256": digest(source),
                    "calculation": "tokens / 1,000,000 × model rate",
                    "unknown_cost_items": sorted(set(unknown_cost_items + missing)),
                }
            )
    path = output / "usage-report.json"
    write_json(path, report)
    artifact = repo.artifact(project, path, "usage_report", model="v0.8")
    project.artifacts.append(artifact)
    return artifact, report


def create_release_status(repo, project, output, status, *, actor=None, approval=None, message=""):
    longform = isinstance(project.spec, LongFormSpec)
    rights = json.loads(
        repo.verify(project, _current(project, "rights_manifest")).read_text(encoding="utf-8")
    )
    data = {
        "schema_version": 1,
        "project_id": project.id,
        "project_version": project.version,
        "status": status,
        "generated_at": now(),
        "approved_by": actor,
        "approval_id": approval.id if approval else None,
        "feedback": message,
        "approved_artifacts": [
            {
                "artifact_id": artifact_id,
                "sha256": next(item.sha256 for item in project.artifacts if item.id == artifact_id),
            }
            for artifact_id in (approval.artifact_ids if approval and status == "cleared" else [])
        ],
        "checks": {
            "creative_preview_approved": any(
                a.gate == "prototype" and a.version == project.version and a.decision == "approve"
                for a in project.approvals
            ),
            "video_qa_passed": bool(project.current_artifacts(
                "longform_review_qa" if longform else "qa"
            )),
            "narration_timing_passed": bool(project.current_artifacts(
                "longform_narration_validation" if longform else "narration_validation"
            )),
            "metadata_present": bool(project.current_artifacts("youtube_metadata")),
            "thumbnail_selected": bool(project.publication),
            "rights_status": rights.get("status", "unknown"),
            "unknown_rights_artifact_ids": rights.get("unknown_artifact_ids", []),
        },
        "human_acknowledgement_required": rights.get("status") != "documented",
        "unresolved_blockers": list(
            dict.fromkeys(
                rights.get("unknown_artifact_ids", [])
                + rights.get("context_review_artifact_ids", [])
            )
        ),
    }
    path = output / "release-status.json"
    write_json(path, data)
    artifact = repo.artifact(project, path, "release_status", model="v0.8")
    project.artifacts.append(artifact)
    return artifact


def prepare_clearance(repo, project, output, *, rate_card=None):
    ensure_publication_package(repo, project, output)
    title = _current(project, "youtube_title")
    description = _current(project, "publication_description")
    metadata = _current(project, "youtube_metadata")
    metadata_data = json.loads(repo.verify(project, metadata).read_text(encoding="utf-8"))
    gallery = json.loads(
        repo.verify(project, _current(project, "thumbnail_gallery")).read_text(encoding="utf-8")
    )
    recommended = gallery.get("recommended_thumbnail")
    thumbnail = None
    if project.publication:
        thumbnail = next(
            (
                a
                for a in project.current_artifacts("thumbnail_candidate")
                if a.id == project.publication.thumbnail_artifact_id
            ),
            None,
        )
    thumbnail = thumbnail or next(
        (a for a in project.current_artifacts("thumbnail_candidate") if a.path == recommended),
        None,
    )
    if thumbnail is None:
        raise ValueError("publication package has no recommended thumbnail")
    usage, usage_data = create_usage_report(repo, project, output, rate_card=rate_card)
    project.publication = PublicationPackage(
        title=metadata_data["title"],
        description=metadata_data["description"],
        hashtags=metadata_data["hashtags"][:3],
        title_artifact_id=title.id,
        description_artifact_id=description.id,
        metadata_artifact_id=metadata.id,
        thumbnail_artifact_id=thumbnail.id,
        usage_artifact_id=usage.id,
        release_status_artifact_id="pending",
        usage_summary=(
            f"{usage_data['usage']['provider_requests']} provider requests; "
            f"{usage_data['usage']['image_requests']} image requests; cost "
            f"{usage_data['cost']['status']}"
        ),
    )
    release = create_release_status(repo, project, output, "pending")
    project.publication.release_status_artifact_id = release.id


def export_cleared_package(repo, project, release_artifact):
    if project.publication is None:
        raise ValueError("publication package has not been prepared")
    indexed = {a.id: a for a in project.current_artifacts()}
    mapping = {
        "final-video.mp4": _current(project, "prototype"),
        "youtube-title.txt": indexed[project.publication.title_artifact_id],
        "youtube-description.txt": indexed[project.publication.description_artifact_id],
        "youtube-metadata.json": indexed[project.publication.metadata_artifact_id],
        "selected-thumbnail.png": indexed[project.publication.thumbnail_artifact_id],
        "release-status.json": release_artifact,
        "usage-report.json": indexed[project.publication.usage_artifact_id],
    }
    root = repo.project_dir(project.id)
    for name, artifact in mapping.items():
        source = repo.verify(project, artifact)
        destination = root / name
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    return sorted(mapping)
