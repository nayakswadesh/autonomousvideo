import asyncio
from pathlib import Path

import pytest
from test_preview_policy import FakeRenderer, RecordingHITL

from autonomous_studio.domain.models import State
from autonomous_studio.rendering.ffmpeg import FFmpeg
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief


@pytest.mark.integration
def test_second_topic_renders_without_engine_changes(tmp_path):
    brief = load_brief(Path("examples/solar-system.yaml"))
    studio = Studio(tmp_path)
    project = studio.create(brief)
    for gate in ("story", "hero"):
        asyncio.run(studio.run(project.id))
        studio.decide(project.id, gate, "approve", actor="test-harness")
    project = asyncio.run(studio.render(project.id))
    assert project.state == State.PROTOTYPE_APPROVAL
    movie = studio.repo.verify(project, project.current_artifacts("prototype")[0])
    assert FFmpeg().inspect(movie)["duration"] == 10


def test_second_full_topic_uses_same_one_review_engine(tmp_path):
    brief = load_brief(Path("examples/solar-system-full.yaml"))
    studio = Studio(tmp_path)
    studio.renderer = FakeRenderer(studio.repo)
    studio.hitl = RecordingHITL()
    project = studio.create(brief, review_policy="preview")
    result = asyncio.run(studio.run(project.id))
    assert result.state == State.PROTOTYPE_APPROVAL
    assert result.spec.brief.production_mode == "full" and result.spec.brief.duration == 45
    assert len(result.spec.shots) == 6 and not any(
        shot.kind == "rank_reveal" for shot in result.spec.shots
    )
    assert len(result.approvals) == 1 and studio.hitl.gates == ["prototype"]
    assert result.current_artifacts("scope_validation")
