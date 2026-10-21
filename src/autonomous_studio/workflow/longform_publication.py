"""Versioned, reviewable YouTube package for an approved long-form film."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field

from autonomous_studio.domain.models import LongFormSpec, PublicationPackage, now
from autonomous_studio.providers.openai import output_text
from autonomous_studio.storage.repository import write_json
from autonomous_studio.workflow.publication import create_usage_report


class LongFormMetadataDraft(BaseModel):
    title: str = Field(min_length=10, max_length=100)
    opening: str = Field(min_length=20, max_length=300)
    story_summary: str = Field(min_length=40, max_length=900)
    comment_question: str = Field(min_length=10, max_length=180)
    hashtags: list[str] = Field(min_length=1, max_length=3)


def _clean_url(url: str) -> str:
    parsed = urlsplit(url)
    query = urlencode([
        (key, value) for key, value in parse_qsl(parsed.query)
        if not key.casefold().startswith("utm_")
    ])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _sources(project) -> list[str]:
    lines, seen = [], set()
    for source in project.spec.brief.research.sources:
        url = _clean_url(source.url)
        if url in seen:
            continue
        lines.append(f"- {source.title}: {url}")
        seen.add(url)
        if len(lines) == 6:
            break
    return lines


def _music_credit(repo, project) -> str:
    selections = project.current_artifacts("longform_music_selection")
    if not selections:
        return "Music: see the project's audio licence record."
    data = json.loads(repo.verify(project, selections[-1]).read_text(encoding="utf-8"))
    track = data.get("track") or data.get("selected") or {}
    title, artist = track.get("title"), track.get("artist")
    if title and artist:
        credit = f"Music: {title} by {artist} (YouTube Audio Library)."
        attribution = (track.get("provenance") or {}).get("attribution")
        if track.get("attribution_required") and attribution:
            credit += " " + attribution
        return credit
    return "Music: see the project's audio licence record."


def _validate_metadata(title: str, description: str) -> list[str]:
    title = title.strip()
    if not title or len(title) > 100 or "\n" in title:
        raise ValueError("long-form YouTube title must be one line of at most 100 characters")
    if not description.strip() or len(description) > 5000:
        raise ValueError("long-form YouTube description must be at most 5000 characters")
    tags = re.findall(r"(?<!\w)#[A-Za-z0-9_]+", description)
    if len(tags) > 3:
        raise ValueError("long-form YouTube description may contain at most three hashtags")
    return tags


async def _copy(repo, project, client) -> tuple[str, str, list[str]]:
    existing_title = project.current_artifacts("youtube_title") or project.current_artifacts("longform_youtube_title")
    existing_description = (
        project.current_artifacts("publication_description")
        or project.current_artifacts("longform_youtube_description")
    )
    if existing_title and existing_description:
        title = repo.verify(project, existing_title[-1]).read_text(encoding="utf-8").strip()
        description = repo.verify(project, existing_description[-1]).read_text(encoding="utf-8")
        return title, description, _validate_metadata(title, description)

    spec = project.spec
    if project.pipeline == "mock":
        title = spec.brief.title.strip()[:100]
        hook = spec.narrative.hook or spec.narrative.central_question or spec.brief.viewer_promise
        summary = spec.narrative.conclusion or (
            spec.story_design.payoff if spec.story_design else ""
        )
        description = "\n\n".join(filter(None, (hook, summary)))
        description += "\n\nWhat stood out to you? Tell us in the comments."
        tags = ["#Science"]
    else:
        if client is None:
            raise ValueError("live long-form publication needs the configured metadata writer")
        script = "\n\n".join(
            block.text for chapter in spec.chapters for block in chapter.narration_blocks
        )
        if not script.strip():
            raise ValueError("long-form metadata requires the approved narration")
        prompt = (
            "Write YouTube publication copy for this approved long-form documentary. "
            "Use only the approved narration and story promise. Make the title inviting and "
            "specific, aligned with the opening and thumbnail, but do not reveal an answer "
            "the film deliberately develops. No clickbait, invented facts, or claims beyond "
            "the approved speech. Write two concise, natural description paragraphs: an "
            "opening curiosity prompt and a summary of what the viewer will learn. Ask a "
            "topic-specific question that invites comments. Supply 1–3 relevant hashtags. "
            "Do not write source links, music credits, rights claims, or upload settings; "
            "the studio adds those from its verified records. Return only the schema.\n"
            + json.dumps({
                "topic": spec.brief.topic,
                "viewer_promise": spec.narrative.viewer_promise,
                "hook": spec.narrative.hook,
                "conclusion": spec.narrative.conclusion,
                "chapter_titles": [c.title for c in spec.chapters],
                "approved_narration": script,
            }, ensure_ascii=False)
        )
        response = await client.response(
            getattr(client.settings, "longform_metadata_model", "gpt-6-luna"),
            prompt, schema=LongFormMetadataDraft.model_json_schema(),
            background=True, reasoning_effort="low",
        )
        draft = LongFormMetadataDraft.model_validate_json(output_text(response))
        title = " ".join(draft.title.split())
        tags = draft.hashtags
        if any(not re.fullmatch(r"#[A-Za-z0-9_]{2,30}", tag) for tag in tags):
            raise ValueError("long-form metadata contains an invalid hashtag")
        description = "\n\n".join((
            draft.opening.strip(), draft.story_summary.strip(),
            draft.comment_question.strip(),
        ))
    source_lines = _sources(project)
    if source_lines:
        description += "\n\nSources:\n" + "\n".join(source_lines)
    description += "\n\n" + _music_credit(repo, project)
    description += (
        "\nVisuals include original diagrams and AI-generated illustrations; "
        "illustrations are not archival footage. Narration is AI-generated."
    )
    description += "\n\n" + " ".join(tags) + "\n"
    return title, description, _validate_metadata(title, description)


async def prepare_longform_clearance(studio, project, output: Path, *, rate_card=None):
    """Prepare one immutable review package; no upload or implicit approval."""
    repo = studio.repo
    if not isinstance(project.spec, LongFormSpec):
        raise ValueError("long-form clearance requires a long-form project")
    masters = project.current_artifacts("longform_master")
    selected_artifact = None
    if project.publication:
        selected_artifact = next((
            a for a in project.current_artifacts("thumbnail_candidate")
            if a.id == project.publication.thumbnail_artifact_id
        ), None)
    thumbnails = [selected_artifact] if selected_artifact else (
        project.current_artifacts("longform_selected_thumbnail")
        or project.current_artifacts("longform_thumbnail")
    )
    if not masters or not thumbnails:
        raise ValueError("approved master and selected thumbnail are required")
    repo.verify(project, masters[-1])
    selected = repo.verify(project, thumbnails[-1])
    title, description, tags = await _copy(repo, project, studio.live_client)
    title_path = output / "youtube-title.txt"
    title_path.write_text(title + "\n", encoding="utf-8")
    description_path = output / "youtube-description.txt"
    description_path.write_text(description, encoding="utf-8")
    thumbnail_path = output / "selected-thumbnail.png"
    shutil.copyfile(selected, thumbnail_path)
    title_artifact = repo.artifact(project, title_path, "youtube_title", model="longform-publication-v1")
    description_artifact = repo.artifact(
        project, description_path, "publication_description", model="longform-publication-v1"
    )
    thumbnail_artifact = repo.artifact(
        project, thumbnail_path, "thumbnail_candidate", model="longform-publication-v1",
        width=1280, height=720,
    )
    project.artifacts.extend((title_artifact, description_artifact, thumbnail_artifact))
    metadata_path = output / "youtube-metadata.json"
    write_json(metadata_path, {
        "schema_version": 1, "title": title, "description": description,
        "hashtags": tags,
        "upload": {
            "snippet": {
                "title": title, "description": description,
                "tags": [tag.removeprefix("#") for tag in tags],
                "categoryId": str(project.creation_settings.get("youtube_category_id", "28")),
                "defaultLanguage": project.spec.brief.voice.language,
            },
            "status": {
                "privacyStatus": "private", "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": True,
            },
        },
    })
    metadata_artifact = repo.artifact(project, metadata_path, "youtube_metadata", model="longform-publication-v1")
    project.artifacts.append(metadata_artifact)
    source_path = output / "source-manifest.json"
    write_json(source_path, {
        "schema_version": 1, "project_id": project.id, "version": project.version,
        "sources": [s.model_dump(mode="json") for s in project.spec.brief.research.sources],
        "claims": [c.model_dump(mode="json") for c in project.spec.brief.research.claims],
    })
    project.artifacts.append(repo.artifact(project, source_path, "source_manifest"))
    relevant_roles = {"longform_visual", "semantic_illustration", "longform_music", "voice"}
    relevant = [a for a in project.current_artifacts() if a.role in relevant_roles]
    unknown = [a.id for a in relevant if a.provenance.license_category == "unknown"]
    context_review = [
        a.id for a in relevant
        if a.provenance.origin == "library" and a.role in {"longform_visual", "semantic_illustration"}
    ]
    rights_path = output / "rights-manifest.json"
    write_json(rights_path, {
        "schema_version": 1, "project_id": project.id, "version": project.version,
        "status": "review_required" if unknown or context_review else "documented",
        "unknown_artifact_ids": unknown,
        "context_review_artifact_ids": context_review,
        "assets": [{
            "artifact_id": a.id, "role": a.role, "sha256": a.sha256,
            "provider": a.provider, "provenance": a.provenance.model_dump(mode="json"),
        } for a in relevant],
    })
    project.artifacts.append(repo.artifact(project, rights_path, "rights_manifest"))
    usage, usage_data = create_usage_report(repo, project, output, rate_card=rate_card)
    project.publication = PublicationPackage(
        title=title, description=description, hashtags=tags,
        title_artifact_id=title_artifact.id,
        description_artifact_id=description_artifact.id,
        metadata_artifact_id=metadata_artifact.id,
        thumbnail_artifact_id=thumbnail_artifact.id,
        usage_artifact_id=usage.id,
        release_status_artifact_id="pending",
        usage_summary=(
            f"{usage_data['usage']['provider_requests']} provider requests; "
            f"cost {usage_data['cost']['status']}"
        ),
        rights_summary=(
            f"Review required: {len(unknown)} asset(s) have unknown licence/provenance; "
            f"{len(context_review)} library visual(s) need context review."
            if unknown or context_review else "Documented asset provenance."
        ),
    )
    from autonomous_studio.workflow.publication import create_release_status

    pending = create_release_status(repo, project, output, "pending")
    project.publication.release_status_artifact_id = pending.id
