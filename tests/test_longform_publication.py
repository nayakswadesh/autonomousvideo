import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from autonomous_studio.config import Settings
from autonomous_studio.domain.models import Approval, LongFormChapter, NarrationBlock, State
from autonomous_studio.providers.openai import strict_schema, validate_strict_schema
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief
from autonomous_studio.workflow.longform_publication import LongFormMetadataDraft, _copy


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "longform-pilot.yaml"


def test_future_longform_metadata_uses_luna_and_keeps_sources_out_of_model_output(tmp_path):
    validate_strict_schema(strict_schema(LongFormMetadataDraft.model_json_schema()))
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    topic = studio.create_longform_topic("A curious science story")
    assert topic.publication_clearance_enabled
    assert topic.live_models["longform_metadata_model"] == "gpt-6-luna"

    project = studio.create(load_brief(EXAMPLE))
    project.pipeline = "live"
    project.spec.chapters = [LongFormChapter(
        id="chapter-1", order=1, title="The clue", purpose="Explain the clue",
        question="What was measured?", reveal="The measurement mattered.",
        target_duration=project.spec.brief.duration,
        narration_blocks=[NarrationBlock(
            id="block-1", text="The instrument showed a surprising result.",
            estimated_duration=4,
        )],
    )]
    calls = []

    class Client:
        settings = SimpleNamespace(longform_metadata_model="gpt-6-luna")

        async def response(self, model, prompt, **kwargs):
            calls.append((model, prompt, kwargs))
            draft = {
                "title": "The Instrument That Changed the Story",
                "opening": "What did one measurement reveal about a puzzling sample?",
                "story_summary": "Follow the investigation from its first clue to a careful conclusion.",
                "comment_question": "Which part of the investigation surprised you most?",
                "hashtags": ["#Science"],
            }
            return {
                "status": "completed",
                "output": [{"content": [{"type": "output_text", "text": json.dumps(draft)}]}],
            }

    title, description, tags = asyncio.run(_copy(studio.repo, project, Client()))
    assert title == "The Instrument That Changed the Story"
    assert "Sources:" in description and "#Science" in description
    assert tags == ["#Science"]
    assert len(calls) == 1 and calls[0][0] == "gpt-6-luna"
    assert calls[0][2]["schema"] and calls[0][2]["background"]
    assert "source links, music credits" in calls[0][1]


def test_longform_video_approval_queues_publication_without_auto_approval(tmp_path):
    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE), publication_clearance=True)
    root = studio.repo.project_dir(project.id)
    movie = root / "review.mp4"
    movie.write_bytes(b"approved film")
    artifact = studio.repo.artifact(project, movie, "prototype", duration=2.0)
    project.artifacts.append(artifact)
    project.state = State.LONGFORM_REVIEW_APPROVAL
    project.approvals.append(Approval(
        id="prototype-approval", project_id=project.id, gate="prototype",
        version=project.version, artifact_ids=[artifact.id],
    ))
    studio.repo.save(project, "test_review_ready", {})
    approved = studio.decide(project.id, "prototype", "approve", actor="test")
    assert approved.state == State.PUBLICATION_CLEARANCE
    assert approved.pending() is None
    assert (root / "final-video.mp4").read_bytes() == b"approved film"
    assert approved.current_artifacts("longform_master")


def test_longform_clearance_prepares_metadata_and_waits_for_human(tmp_path):
    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE), publication_clearance=True)
    root = studio.repo.project_dir(project.id)
    movie = root / "approved.mp4"
    movie.write_bytes(b"master fixture")
    thumbnail = root / "thumbnail.png"
    Image.new("RGB", (1280, 720), "#101b29").save(thumbnail)
    project.artifacts.extend((
        studio.repo.artifact(project, movie, "prototype", duration=2),
        studio.repo.artifact(project, movie, "longform_master", duration=2),
        studio.repo.artifact(project, thumbnail, "longform_thumbnail", width=1280, height=720),
    ))
    project.approvals.append(Approval(
        id="approved-video", project_id=project.id, gate="prototype",
        version=project.version, artifact_ids=[a.id for a in project.artifacts],
        decision="approve", actor="test",
    ))
    project.state = State.PUBLICATION_CLEARANCE
    studio.repo.save(project, "test_master_ready", {})
    prepared = asyncio.run(Studio(tmp_path).run(project.id))
    assert prepared.state == State.PUBLICATION_CLEARANCE
    assert prepared.pending().gate == "publication"
    assert prepared.publication.title
    assert prepared.current_artifacts("youtube_metadata")
    assert prepared.current_artifacts("rights_manifest")
    assert not (root / "release-status.json").exists()
