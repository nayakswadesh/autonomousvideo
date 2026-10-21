"""Recovery of completed research responses after local validation improvements."""

from __future__ import annotations

import json
from pathlib import Path

from autonomous_studio.domain.models import State
from autonomous_studio.providers.openai import (
    OpenAIResearch,
    canonical_url,
    observed_urls,
    output_text,
)
from autonomous_studio.storage.repository import digest


def _cached_response(repo, project_id, path):
    resolved = Path(path).resolve()
    cache = (repo.project_dir(project_id) / "provider-cache").resolve()
    if not resolved.is_relative_to(cache) or resolved.suffix != ".json":
        raise ValueError("response must be a JSON file in this project's provider cache")
    if resolved.stat().st_size > 2_000_000:
        raise ValueError("cached response exceeds 2 MB")
    return resolved, json.loads(resolved.read_text(encoding="utf-8"))


def recover_research(
    studio,
    project_id,
    evidence_response_path,
    structured_response_path,
    *,
    version,
    actor="local-user",
):
    """Revalidate two cached responses and advance without making provider calls."""
    repo = studio.repo
    with repo.lock(project_id):
        project = repo.load(project_id)
        if project.version != version:
            raise ValueError("stale project version")
        if project.pipeline != "live" or project.state != State.CREATED:
            raise ValueError("cached research recovery requires a live project in CREATED")
        if project.current_artifacts("research"):
            raise ValueError("cannot replace accepted research")

        evidence_path, evidence_raw = _cached_response(repo, project_id, evidence_response_path)
        structured_path, structured_raw = _cached_response(
            repo, project_id, structured_response_path
        )
        evidence = output_text(evidence_raw)
        urls = observed_urls(evidence_raw)
        if not urls:
            raise ValueError("cached evidence has no tool-observed source URLs")
        research, repair = OpenAIResearch.validate_structured(structured_raw, urls)
        if any(canonical_url(source.url) not in urls for source in research.sources):
            raise ValueError("Research cited a URL not observed in web tool results")

        output = repo.attempt_dir(project, "research-recovery")
        for name, raw, source in (
            ("evidence-provider-response.json", evidence_raw, evidence_path),
            ("structured-provider-response.json", structured_raw, structured_path),
        ):
            studio._json(project, output, name, raw, "provider_usage")
            artifact = project.artifacts[-1]
            artifact.provider = "openai"
            artifact.model = raw.get("model", project.live_models.get("research_model", "unknown"))
            artifact.cost = None
            artifact.input_reference = source.relative_to(repo.project_dir(project_id)).as_posix()

        evidence_copy = output / "evidence.md"
        evidence_copy.write_text(evidence, encoding="utf-8")
        project.artifacts.append(repo.artifact(project, evidence_copy, "research_evidence"))
        studio._json(project, output, "research.json", research.model_dump(mode="json"), "research")
        if repair:
            studio._json(project, output, "research-repairs.json", [repair], "research_repair")
        findings = output / "findings.md"
        findings.write_text(
            "# Research evidence\n\n"
            + "\n\n".join(
                f"- {claim.text}\n  Status: {claim.status}. {claim.caveat}"
                for claim in research.claims
            ),
            encoding="utf-8",
        )
        project.artifacts.append(repo.artifact(project, findings, "findings"))
        project.spec.brief.research = research
        project.warnings = studio.warnings_for(project)
        old = project.state
        project.state = State.RESEARCHED
        project.last_error = project.last_error_code = None
        repo.save(
            project,
            "research_revalidated",
            {
                "from": old,
                "to": project.state,
                "version": version,
                "actor": actor,
                "evidence_response_sha256": digest(evidence_path),
                "structured_response_sha256": digest(structured_path),
                "direction_repair": repair,
                "additional_api_calls": 0,
            },
        )
        return project
