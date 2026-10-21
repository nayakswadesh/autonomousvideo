"""Recover cached chapter drafts and repair one failed long-form handoff.

Usage: python scripts/recover_longform_script.py PROJECT_ID MANIFEST.json
The manifest names the exact provider cache receipts and one chapter to repair.
The ordinary Studio workflow still validates and persists the resulting script review.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path

from autonomous_studio.domain.models import NarrationBlock, State
from autonomous_studio.providers.longform import (
    ChapterScriptDraft,
    _words,
    continuity_report,
)
from autonomous_studio.providers.openai import ProviderValidationError, output_text
from autonomous_studio.workflow import Studio


class CachedChapterRepair:
    def __init__(self, director, repo, project_id: str, manifest: dict):
        self.director = director
        self.repo = repo
        self.project_id = project_id
        self.manifest = manifest

    async def write_script(self, spec, feedback):
        result = deepcopy(spec)
        cache = self.repo.project_dir(self.project_id) / "provider-cache"
        keys = self.manifest["chapter_response_keys"]
        if set(keys) != {chapter.id for chapter in result.chapters}:
            raise ValueError("Recovery manifest must name exactly the treatment chapters")
        for chapter in result.chapters:
            key = keys[chapter.id]
            if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
                raise ValueError("Recovery manifest contains an invalid cache key")
            response = json.loads((cache / f"{key}.json").read_text(encoding="utf-8"))
            draft = ChapterScriptDraft.model_validate_json(output_text(response))
            if draft.chapter_id != chapter.id:
                raise ValueError("Cached response has the wrong chapter ID")
            allowed = set(chapter.evidence_claim_ids)
            chapter.narration_blocks = []
            for block in draft.narration_blocks:
                if not set(block.claim_ids) <= allowed:
                    raise ValueError("Cached narration cites evidence outside its chapter")
                chapter.narration_blocks.append(
                    NarrationBlock(
                        **block.model_dump(mode="json"),
                        estimated_duration=(
                            len(_words(block.text)) / result.brief.voice.pace_wpm * 60
                        ),
                    )
                )
        before = continuity_report(result)
        if not before.passed:
            raise ValueError("Cached script fails deterministic continuity checks")
        replacements = self.manifest.get("editorial_replacements", [])
        if replacements:
            blocks = {
                block.id: block for chapter in result.chapters for block in chapter.narration_blocks
            }
            for change in replacements:
                block = blocks[change["block_id"]]
                if block.text != change["before"]:
                    raise ValueError("Editorial replacement does not match cached narration")
                block.text = change["after"]
                block.estimated_duration = (
                    len(_words(block.text)) / result.brief.voice.pace_wpm * 60
                )
        else:
            target_id = self.manifest["repair_chapter_id"]
            target_index = next(
                (index for index, chapter in enumerate(result.chapters) if chapter.id == target_id),
                None,
            )
            if target_index is None:
                raise ValueError("Recovery manifest names an unknown repair chapter")
            chapter = result.chapters[target_index]
            previous = (
                result.chapters[target_index - 1].narration_blocks[-1].text if target_index else ""
            )
            chapter.narration_blocks = await self.director._chapter_script(
                result,
                chapter,
                previous,
                feedback,
                self.manifest["repair_issue"],
            )
        local = continuity_report(result)
        if not local.passed:
            problems = [issue.message for issue in local.issues if issue.severity == "error"]
            raise ProviderValidationError("Targeted long-form repair", "; ".join(problems))
        audit_issues = await self.director._audit(result)
        report = continuity_report(result, audit_issues)
        if not report.passed:
            problems = [issue.message for issue in report.issues if issue.severity == "error"]
            raise ProviderValidationError("Targeted long-form repair", "; ".join(problems))
        return result, report


class RecoveryStudio(Studio):
    def __init__(self, root: Path, project_id: str, manifest: dict):
        super().__init__(root)
        self.recovery_project_id = project_id
        self.manifest = manifest

    def configure_live(self, project):
        super().configure_live(project)
        if project.id == self.recovery_project_id:
            self.longform_director = CachedChapterRepair(
                self.longform_director, self.repo, project.id, self.manifest
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--root", type=Path, default=Path("projects/live"))
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    studio = RecoveryStudio(args.root, args.project_id, manifest)
    project = studio.repo.load(args.project_id)
    if (
        project.version != manifest["version"]
        or project.state != State.LONGFORM_SCRIPTING
        or not (
            project.last_error_code == "provider_validation"
            or project.last_error == "failed continuity audit requires an error issue"
        )
    ):
        raise ValueError("Recovery requires the exact failed script version and stage")
    result = asyncio.run(studio.run(args.project_id))
    gate = result.pending().gate if result.pending() else "none"
    print(result.id, result.version, result.state, gate)


if __name__ == "__main__":
    main()
