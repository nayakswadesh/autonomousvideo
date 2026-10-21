"""Replay already-paid director responses through current validators, without network access."""

import json
from pathlib import Path
from uuid import uuid4

from autonomous_studio.config import Settings
from autonomous_studio.domain.models import State
from autonomous_studio.providers.openai import OpenAIDirector, output_text


class CachedDirectorClient:
    def __init__(self, cache: Path, model: str, responses: list[dict]):
        self.cache = cache
        self.settings = Settings(director_model=model)
        self.responses = responses
        self.trace = []

    async def response(self, model, prompt, **kwargs):
        if not self.responses:
            raise ValueError("cached director replay exhausted; no live request was made")
        response = self.responses.pop(0)
        output_text(response)
        self.trace.append({"response": response, "cache_hit": True, "requested_model": model})
        return response

    def reject_last(self, reason):
        raise ValueError(f"cached director response still fails validation: {reason}")


async def recover_director(studio, project_id, response_path, *, version, actor="local-operator"):
    """Apply a cached narrative and shot plan only if the complete new validation passes."""
    repo = studio.repo
    with repo.lock(project_id):
        project = repo.load(project_id)
        if project.version != version:
            raise ValueError("stale project version")
        if project.state != State.RESEARCHED or "Director shot plan" not in (
            project.last_error or ""
        ):
            raise ValueError("cached director recovery requires a failed shot-plan stage")
        cache = (repo.project_dir(project_id) / "provider-cache").resolve()
        path = Path(response_path).resolve()
        if (
            not path.is_relative_to(cache)
            or path.suffix != ".json"
            or path.stat().st_size > 2_000_000
        ):
            raise ValueError("shot response must be a bounded JSON file in this project's cache")
        checkpoints = sorted(
            cache.glob("*.validated-narrative.json"), key=lambda item: item.stat().st_mtime
        )
        if not checkpoints:
            raise ValueError("no validated narrative checkpoint is available")
        narrative = json.loads(checkpoints[-1].read_text(encoding="utf-8"))
        first = narrative.get("provider_response")
        second = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(first, dict) or not isinstance(second, dict):
            raise ValueError("cached director responses are incomplete")
        output_text(first)
        output_text(second)
        replay_cache = cache / f"replay-v{version}-{uuid4().hex[:8]}"
        client = CachedDirectorClient(
            replay_cache,
            project.live_models.get("director_model", "unknown"),
            [first, second],
        )
        studio.director = OpenAIDirector(client)
        studio.live_client = client
        original_captions = project.spec.closed_captions
        await studio._step(project)
        if project.spec.closed_captions != original_captions:
            raise ValueError("cached replay changed the project's caption preference")
        repo.save(
            project,
            "director_cached_response_revalidated",
            {
                "version": version,
                "actor": actor,
                "response_path": path.relative_to(repo.project_dir(project_id)).as_posix(),
                "additional_api_calls": 0,
            },
        )
        return project
