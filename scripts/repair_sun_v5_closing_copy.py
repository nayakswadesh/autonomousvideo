"""One-off, audited local repair of Sun v5's overlong closing label."""

import json
from pathlib import Path

from autonomous_studio.domain.models import State
from autonomous_studio.providers.longform_visuals import ChapterVisualSelection
from autonomous_studio.providers.openai import output_text
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.longform_visuals import saved_visual_responses

PROJECT_ID = "f2230c84373c"
ORIGINAL = "Like and share if this clarified the Sun’s story."
CORRECTED = ORIGINAL.removesuffix(".")


def main() -> None:
    studio = Studio(Path("projects/live"))
    with studio.repo.lock(PROJECT_ID):
        project = studio.repo.load(PROJECT_ID)
        if (
            project.version != 5
            or project.state != State.LONGFORM_VISUAL_PLANNING
            or project.last_error_code != "provider_validation"
            or not project.last_error
            or "community close action text exceeds its readable slot"
            not in project.last_error
        ):
            raise ValueError("Sun v5 is not at the expected closing-copy failure")
        original_artifact, records = saved_visual_responses(studio, project)
        if len(records) != 2 * len(project.spec.chapters):
            raise ValueError("expected one saved partition and selection per chapter")
        last = records[-1]
        if last["chapter_id"] != project.spec.chapters[-1].id:
            raise ValueError("the last saved response is not the closing chapter")
        selection = json.loads(output_text(last["response"]))
        closing = selection["beats"][-1]
        if (
            closing["id"] != "ch3-beat10"
            or closing["design_choice"] != "editorial_close/community_close"
            or closing["screen_blueprint"]["community_roles"][-1] != "like_share"
        ):
            raise ValueError("unexpected closing beat or action role")
        cue = closing["reveal_cues"][-1]
        if cue["label"] != ORIGINAL or cue["short_label"] != ORIGINAL:
            raise ValueError("closing labels no longer match the diagnosed failure")
        if len(ORIGINAL) != 49 or len(CORRECTED) != 48:
            raise ValueError("the punctuation-only edit does not meet the slot limit")
        cue["label"] = cue["short_label"] = CORRECTED
        ChapterVisualSelection.model_validate(selection)
        updated_blocks = 0
        for item in last["response"]["output"]:
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    content["text"] = json.dumps(selection, ensure_ascii=False)
                    updated_blocks += 1
        if updated_blocks != 1:
            raise ValueError("expected exactly one response text block")
        last["local_patch"] = {
            "kind": "closing_terminal_punctuation",
            "beat_id": closing["id"],
            "role": "like_share",
            "original_label": ORIGINAL,
            "corrected_label": CORRECTED,
            "original_artifact": original_artifact.path,
        }
        output = studio.repo.attempt_dir(project, "local-closing-copy-replay")
        studio._json(
            project,
            output,
            "longform-visual-plan-attempt-1-local-provider-responses.json",
            records,
            "provider_usage",
        )
        patched_artifact = project.artifacts[-1]
        project.creation_settings["visual_plan_replay_artifact"] = patched_artifact.path
        project.creation_settings.pop("visual_plan_replay_allow_continuation", None)
        project.longform_visual_plan_attempts = 0
        project.last_error = project.last_error_code = None
        studio.repo.save(project, "sun_v5_local_closing_copy_replay_requested", {
            "version": project.version,
            "original_artifact": original_artifact.path,
            "patched_artifact": patched_artifact.path,
            "beat_id": closing["id"],
            "original_label": ORIGINAL,
            "corrected_label": CORRECTED,
            "new_model_calls_allowed": False,
        })
        print(json.dumps({
            "project_id": project.id,
            "version": project.version,
            "patched_artifact": patched_artifact.path,
            "corrected_label": CORRECTED,
            "new_model_calls_allowed": False,
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
