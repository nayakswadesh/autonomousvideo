"""Prepare, then explicitly apply the user-selected Commons photograph without new AI calls."""
import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlencode

from autonomous_studio.domain.models import Artifact, GeneratedVisual, State
from autonomous_studio.providers.archival import WikimediaCommonsMedia, archival_fingerprint
from autonomous_studio.providers.asset_planner import validate_asset_plan
from autonomous_studio.providers.http import HTTP
from autonomous_studio.storage.repository import write_json
from autonomous_studio.workflow import Studio

PROJECT = "77f45ea42919"
TITLE = "File:02010019 radioactive cesium source Goiânia accident.jpg"
QUERY = 'intitle:"02010019 radioactive cesium source"'
SOURCE = "https://commons.wikimedia.org/wiki/" + TITLE.replace(" ", "_")
NOTE = (
    "IAEA Imagebank photograph dated 6 October 2007, depicting the source involved in the "
    "1987 Goiânia accident. Contextual object photograph, not a photograph of the 1987 "
    "identification or evidence of the detector used. Cropped for portrait presentation."
)


def checked_project(studio):
    project = studio.repo.load(PROJECT)
    if (project.version != 2 or project.state != State.ASSETS_PREPARING
            or "moderation_blocked" not in (project.last_error or "")):
        raise ValueError("The reviewed v2 image-failure checkpoint has changed")
    if any(v.image_id == "img_identification" for v in project.generated_visuals):
        raise ValueError("Identification image is already prepared")
    studio._verify_script_approval(project)
    return project


async def prepare(studio):
    project = checked_project(studio)
    http = HTTP()
    params = {
        "action": "query", "format": "json", "formatversion": "2", "titles": TITLE,
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "origin": "*",
    }
    response = await http.get_json(
        WikimediaCommonsMedia.endpoint + "?" + urlencode(params),
        {"User-Agent": "AutonomousStudio/0.9 (local documentary production)"},
        service="Wikimedia Commons selected photo", timeout=60, attempts=1,
    )
    pages = response.get("query", {}).get("pages", [])
    if len(pages) != 1 or pages[0].get("title") != TITLE:
        raise ValueError("Commons did not return exactly the user-selected photograph")

    class PinnedHTTP:
        async def get_json(self, *args, **kwargs):
            return response

        async def get(self, *args, **kwargs):
            return await http.get(*args, **kwargs)

    image = next(i for i in project.asset_plan.images if i.id == "img_identification").model_copy()
    image.source_type, image.search_query = "archival", QUERY
    output = studio.repo.attempt_dir(project, "selected-iaea-photo")
    acquired = await WikimediaCommonsMedia(PinnedHTTP(), studio.repo).acquire(
        project, image, output / "img_identification.png"
    )
    artifacts = [acquired.original, acquired.visual, acquired.metadata, acquired.edit_manifest]
    for artifact in artifacts:
        artifact.provenance.usage_restrictions += " " + NOTE
    write_json(output / "selected-photo.json", {
        "project_version": 2, "source": SOURCE, "context": NOTE,
        "artifacts": [a.model_dump(mode="json") for a in artifacts],
    })
    print(output / "selected-photo.json")


def apply(studio, manifest):
    manifest = manifest.resolve()
    if not manifest.is_relative_to(studio.repo.project_dir(PROJECT)):
        raise ValueError("Manifest must be inside this project")
    selection = json.loads(manifest.read_text(encoding="utf-8"))
    if selection["source"] != SOURCE or selection["project_version"] != 2:
        raise ValueError("Unexpected selected photo")
    artifacts = [Artifact.model_validate(a) for a in selection["artifacts"]]
    for artifact in artifacts:
        artifact.provenance.attribution += (
            "; portrait crop used in video; contextual photograph dated 6 October 2007"
        )
    with studio.repo.lock(PROJECT):
        project = checked_project(studio)
        original_spec = project.spec.model_dump(mode="json")
        for artifact in artifacts:
            studio.repo.verify(project, artifact)
        plan = project.asset_plan.model_copy(deep=True)
        image = next(i for i in plan.images if i.id == "img_identification")
        before = image.model_dump(mode="json")
        image.source_type, image.search_query = "archival", QUERY
        for look in plan.display:
            if look.shot_id in image.shot_ids:
                look.visual_strategy = "archival_image_with_timeline_overlay"
                look.visual_rationale = NOTE
        project.asset_plan = validate_asset_plan(project, plan)
        original = next(a for a in artifacts if a.role == "archival_source")
        visual = next(a for a in artifacts if a.role == "visual_asset")
        project.artifacts.extend(artifacts)
        project.generated_visuals.append(GeneratedVisual(
            image_id=image.id, fingerprint=archival_fingerprint(image), artifact=visual,
            source_type="archival", original_artifact_id=original.id,
        ))
        project.production_changes.append("User selected an IAEA contextual photo. " + NOTE)
        studio._verify_script_approval(project)
        assert project.spec.model_dump(mode="json") == original_spec
        output = manifest.parent
        studio._json(project, output, "asset-plan.json", plan.model_dump(mode="json"), "asset_plan")
        studio.repo.save(project, "archival_replacement_approved", {
            "version": 2, "image_id": image.id, "actor": "user-via-codex",
            "before": before, "after": image.model_dump(mode="json"),
            "source": SOURCE, "context": NOTE, "new_image_generation_requests": 0,
        })
    studio.request_retry(PROJECT, version=2, actor="user-approved-iaea-photo")
    print("Selected photo applied and project queued; script and existing images preserved")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", type=Path)
    args = parser.parse_args()
    studio = Studio(Path("projects/live"))
    if args.apply:
        apply(studio, args.apply)
    else:
        asyncio.run(prepare(studio))
