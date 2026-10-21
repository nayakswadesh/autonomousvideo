"""Apply the user-approved single-image revision; retain all historical receipts."""
from pathlib import Path

from autonomous_studio.domain.models import State
from autonomous_studio.providers.asset_planner import validate_asset_plan
from autonomous_studio.providers.images import fingerprint
from autonomous_studio.workflow import Studio


def main():
    studio = Studio(Path("projects/live"))
    project_id = "77f45ea42919"
    with studio.repo.lock(project_id):
        project = studio.repo.load(project_id)
        if project.version != 2 or project.state != State.ASSETS_PREPARING:
            raise ValueError("Project is no longer at the reviewed v2 checkpoint")
        if "moderation_blocked" not in (project.last_error or ""):
            raise ValueError("The reviewed image failure is no longer current")
        if any(item.image_id == "img_loop" for item in project.generated_visuals):
            raise ValueError("Cannot replace an already prepared image")
        studio._verify_script_approval(project)
        original_spec = project.spec.model_dump(mode="json")
        voices = [item.model_dump(mode="json") for item in project.prepared_voices]
        before = project.asset_plan.model_dump(mode="json")
        plan = project.asset_plan.model_copy(deep=True)
        image = next(item for item in plan.images if item.id == "img_loop")
        old_key = fingerprint(project, image)
        image.prompt = (
            "A clearly illustrated, non-technical view of a discarded medical machine in an "
            "abandoned treatment room. Ordinary daylight, worn metal surfaces, and a small "
            "blue accent in the composition. No exposed internal mechanism, people, injuries, "
            "text, or labels. Portrait framing with space for the opening headline. "
            "Use this same illustration for the opening and closing loop."
        )
        for look in plan.display:
            if look.shot_id in image.shot_ids:
                look.visual_rationale = (
                    "The user-approved non-technical illustration of discarded medical equipment "
                    "introduces the salvage mystery. Reuse exactly the same framing for the "
                    "closing loop; do not present it as an archival photograph."
                )
        project.asset_plan = validate_asset_plan(project, plan)
        new_key = fingerprint(project, image)
        if old_key == new_key:
            raise ValueError("Replacement must be a new image request, not an unchanged replay")
        studio._verify_script_approval(project)
        assert project.spec.model_dump(mode="json") == original_spec
        assert [item.model_dump(mode="json") for item in project.prepared_voices] == voices
        output = studio.repo.attempt_dir(project, "approved-image-revision")
        studio._json(project, output, "image-revision.json", {
            "actor": "user-via-codex", "image_id": image.id,
            "reason": "User-approved replacement after provider moderation rejection",
            "before": before, "after": plan.model_dump(mode="json"),
            "old_fingerprint": old_key, "new_fingerprint": new_key,
            "original_journal_preserved": True,
        }, "image_revision")
        studio._json(project, output, "asset-plan.json", plan.model_dump(mode="json"), "asset_plan")
        studio.repo.save(project, "image_revision_approved", {
            "version": project.version, "image_id": image.id, "actor": "user-via-codex",
            "old_fingerprint": old_key, "new_fingerprint": new_key,
            "script_and_narration_preserved": True,
            "request_counter_preserved": project.image_requests,
        })
    queued = studio.request_retry(project_id, actor="user-approved-image-revision", version=2)
    print(f"Queued {queued.id} v{queued.version}: {queued.state}; no API calls made by this script")


if __name__ == "__main__":
    main()
