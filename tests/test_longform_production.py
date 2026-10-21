import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from autonomous_studio.config import Settings
from autonomous_studio.domain.models import (
    Approval,
    LongFormChapter,
    NarrationBlock,
    State,
    VoiceAsset,
    VoiceSegment,
)
from autonomous_studio.domain.semantic import SemanticDecisionBatch
from autonomous_studio.hitl.adapters import TelegramAdapter, TelegramConfig, review_message
from autonomous_studio.providers.longform import MockLongFormResearch
from autonomous_studio.providers.mock import tone
from autonomous_studio.providers.openai import speech_payload
from autonomous_studio.rendering.longform import (
    assemble_review,
    narration_required_duration,
    narration_windows,
    render_chapter,
)
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.engine import load_brief
from autonomous_studio.workflow.longform_production import (
    RuntimeExtensionRequired,
    _checkpoint_json,
    prepare_longform_audio,
    render_longform_chapters,
    retime_longform_narration,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "longform-pilot.yaml"


def test_selected_landing_points_space_measured_speech_without_changing_voice():
    blocks = [
        NarrationBlock(id="one", text="A measured finding.", estimated_duration=1,
                       landing_after="idea_landing"),
        NarrationBlock(id="two", text="The next finding.", estimated_duration=1,
                       landing_after="chapter_turn"),
    ]
    chapter = LongFormChapter(
        id="chapter-1", order=1, title="Turn", purpose="Explain the change",
        question="What changed?", reveal="A new finding", target_duration=4.0,
        narration_blocks=blocks,
    )
    voices = {block.id: SimpleNamespace(duration=1.0) for block in blocks}
    windows = narration_windows(chapter, voices)
    assert windows[0][2:] == pytest.approx((0.12, 1.12))
    assert windows[1][2:] == pytest.approx((1.77, 2.77))
    assert narration_required_duration(windows) == pytest.approx(3.67)
    chapter.target_duration = 3.5
    with pytest.raises(ValueError, match="Measured narration does not fit"):
        narration_windows(chapter, voices)
    assert [block.text for block in blocks] == ["A measured finding.", "The next finding."]


@pytest.mark.parametrize("receipt_present", [False, True])
def test_longform_tts_reservation_checks_the_actual_synthesis_receipt(
    tmp_path, receipt_present
):
    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE))
    project.pipeline = "live"
    project.longform_visual_plan = SimpleNamespace()
    project.live_models["tts_model"] = "gpt-4o-mini-tts"
    output = studio.repo.project_dir(project.id) / "audio" / f"v{project.version:03d}"
    output.mkdir(parents=True)
    music_path = output / "music.wav"
    tone(music_path, 0.2, "music")
    project.longform_music = studio.repo.artifact(project, music_path, "longform_music")
    project.spec.chapters = [
        LongFormChapter(
            id="chapter-1", order=1, title="Test", purpose="Test a narration receipt",
            question="Can it resume?", reveal="Yes", target_duration=2.0,
            narration_blocks=[
                NarrationBlock(id="test-block", text="A clear spoken idea.", estimated_duration=2.0)
            ],
        )
    ]
    block = project.spec.chapters[0].narration_blocks[0]
    segment = VoiceSegment(
        shot_id=block.id,
        spoken_text=block.text,
        estimated_duration=block.estimated_duration,
        delivery_direction=block.delivery_direction,
    )
    settings = Settings(api_key="test-key", tts_model="gpt-4o-mini-tts")
    cache = tmp_path / "provider-cache"
    cache.mkdir()
    studio.live_client = SimpleNamespace(settings=settings, cache=cache)
    payload = speech_payload(
        settings.tts_model, segment, project.spec.brief.voice, consistent_delivery=True
    )
    key = hashlib.sha256(
        json.dumps(
            {"payload": payload, "fresh_media_epoch": None}, sort_keys=True
        ).encode()
    ).hexdigest()
    if receipt_present:
        (cache / f"tts-{key}.wav").write_bytes(b"cached receipt")
    (output / f"{block.id}.job.json").write_text("{}", encoding="utf-8")

    class ReachedSynthesis(Exception):
        pass

    class RecordingVoice:
        async def synthesize(self, *_args):
            raise ReachedSynthesis

    studio.voice = RecordingVoice()
    if receipt_present:
        with pytest.raises(ReachedSynthesis):
            asyncio.run(prepare_longform_audio(studio, project))
    else:
        with pytest.raises(ValueError, match="no receipt and may have incurred usage"):
            asyncio.run(prepare_longform_audio(studio, project))


def test_retried_timeline_checkpoint_does_not_overwrite_hashed_artifact(tmp_path):
    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE))
    output = studio.repo.project_dir(project.id) / "audio"
    first = _checkpoint_json(studio, project, output, "timing", {"changed": True}, "timing")
    again = _checkpoint_json(studio, project, output, "timing", {"changed": True}, "timing")
    second = _checkpoint_json(studio, project, output, "timing", {"changed": False}, "timing")
    assert first == again and first != second
    assert len([artifact for artifact in project.artifacts if artifact.role == "timing"]) == 2
    for artifact in project.artifacts:
        if artifact.role == "timing":
            studio.repo.verify(project, artifact)


def test_chapter_render_captions_on_and_off(tmp_path):
    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE))
    root = studio.repo.project_dir(project.id)
    image_path = root / "test-image.png"
    Image.new("RGB", (1920, 1080), "#26394a").save(image_path)
    image = studio.repo.artifact(project, image_path, "longform_visual")
    voice_path = root / "test-voice.wav"
    tone(voice_path, 0.7, "voice")
    voice_artifact = studio.repo.artifact(project, voice_path, "voice", duration=0.7)
    voice = VoiceAsset(
        artifact=voice_artifact,
        start=0,
        duration=0.7,
        spoken_text="A short test narration.",
        characters=23,
        voice_id="mock",
    )
    beat = SimpleNamespace(
        asset_ref="asset_1",
        start=0,
        duration=2.0,
        narration_block_ids=["block_1"],
        treatment="ken_burns",
    )
    block = SimpleNamespace(id="block_1", text="A short test narration.")
    chapter = SimpleNamespace(
        id="chapter_1", order=1, target_duration=2.0, visual_beats=[beat], narration_blocks=[block]
    )
    project.longform_media["asset_1"] = image
    project.longform_voices["block_1"] = voice
    project.longform_word_timings["block_1"] = [
        {"word": word, "start": index * 0.15, "end": (index + 1) * 0.15}
        for index, word in enumerate(block.text.split())
    ]
    music_path = root / "test-music.wav"
    tone(music_path, 1.0, "music")
    music = studio.repo.artifact(project, music_path, "longform_music", duration=1.0)
    for captions in (False, True):
        project.spec.closed_captions = captions
        movie, qa = render_chapter(
            studio.repo, project, chapter, music, root / f"captions-{captions}"
        )
        assert movie.is_file()
        assert qa["video"]["decodable"]
        assert qa["video"]["has_audio"]
        assert qa["video"]["width"] == 1920
        assert qa["video"]["height"] == 1080
        assert qa["captions_enabled"] is captions
        assert qa["audio"]["clipped_samples"] == 0
        assert qa["caption_cues"] > 0 if captions else qa["caption_cues"] == 0
        assert (root / f"captions-{captions}" / "captions.srt").exists() is captions

    one = studio.repo.artifact(project, root / "captions-False" / "chapter.mp4", "longform_chapter")
    two = studio.repo.artifact(project, root / "captions-True" / "chapter.mp4", "longform_chapter")
    review_project = SimpleNamespace(
        id=project.id,
        spec=SimpleNamespace(
            chapters=[SimpleNamespace(id="one"), SimpleNamespace(id="two")],
            brief=SimpleNamespace(width=1920, height=1080, duration=4.0),
        ),
        longform_chapters={"one": one, "two": two},
    )
    review, info = assemble_review(studio.repo, review_project, root / "review")
    assert review.is_file()
    assert info["has_audio"] and info["decodable"]
    assert abs(info["duration"] - 4.0) < 0.3


class FixtureRenderer:
    def __init__(self):
        self.rendered = []

    def chapter(self, project, chapter, music, output):
        self.rendered.append(chapter.id)
        movie = output / "chapter.mp4"
        movie.write_bytes(f"chapter:{chapter.id}:{len(self.rendered)}".encode())
        qa = {
            "video": {"duration": chapter.target_duration},
            "captions_enabled": project.spec.closed_captions,
        }
        (output / "qa.json").write_text(json.dumps(qa), encoding="utf-8")
        return movie, qa

    def review(self, project, output):
        movie = output / "review.mp4"
        movie.write_bytes(b"fixture-review")
        info = {
            "duration": project.spec.brief.duration,
            "width": 1920,
            "height": 1080,
            "has_audio": True,
            "decodable": True,
        }
        (output / "qa.json").write_text(
            json.dumps({"video": info, "passed": True}), encoding="utf-8"
        )
        return movie, info


def test_approved_visual_plan_produces_review_and_master_with_restart(tmp_path):
    studio = Studio(tmp_path)
    project = asyncio.run(studio.run(studio.create(load_brief(EXAMPLE)).id))
    studio.decide(project.id, "treatment", "approve", actor="test")
    project = asyncio.run(studio.run(project.id))
    studio.decide(project.id, "script", "approve", actor="test")
    project = asyncio.run(studio.run(project.id))
    assert project.state == State.VISUAL_PLAN_APPROVAL
    project = studio.decide(project.id, "visual_plan", "approve", actor="test")
    renderer = FixtureRenderer()
    restarted = Studio(tmp_path, longform_renderer=renderer)
    project = asyncio.run(restarted.run(project.id))
    assert project.state == State.LONGFORM_REVIEW_APPROVAL
    assert len(renderer.rendered) == len(project.spec.chapters)
    assert len(project.longform_media) == len(project.longform_visual_plan.assets)
    assert len(project.longform_voices) == sum(
        len(c.narration_blocks) for c in project.spec.chapters
    )
    assert project.current_artifacts("longform_narration_validation")
    assert project.current_artifacts("longform_review_qa")
    assert project.pending().gate == "prototype"
    assert (
        asyncio.run(Studio(tmp_path, longform_renderer=renderer).run(project.id)).state
        == State.LONGFORM_REVIEW_APPROVAL
    )
    assert len(renderer.rendered) == len(project.spec.chapters)

    linked = project.model_copy(deep=True)
    linked.creation_settings["narration_linked_timeline"] = True
    for chapter in linked.spec.chapters:
        chapter.target_duration += 12
        chapter.visual_beats[-1].duration += 12
    linked.spec.brief.duration_policy.target_seconds += 12 * len(linked.spec.chapters)
    tail_repair = retime_longform_narration(linked)
    assert tail_repair["changed"]
    assert tail_repair["speech_cut_or_accelerated"] is False
    for chapter in linked.spec.chapters:
        assert chapter.target_duration < next(
            original.target_duration + 12
            for original in project.spec.chapters
            if original.id == chapter.id
        )

    retimed = project.model_copy(deep=True)
    first_block = retimed.spec.chapters[0].narration_blocks[0]
    retimed.longform_voices[first_block.id].duration += 20
    timing = retime_longform_narration(retimed)
    assert timing["changed"]
    assert timing["speech_cut_or_accelerated"] is False
    assert (
        retimed.spec.brief.duration <= retimed.spec.brief.duration_policy.expansion_maximum_seconds
    )
    for chapter in retimed.spec.chapters:
        narration_windows(chapter, retimed.longform_voices)

    impossible = project.model_copy(deep=True)
    for voice in impossible.longform_voices.values():
            # Exceed even the current 600-second expansion ceiling, regardless
            # of fixture chapter count or visual-start alignment.
            voice.duration = 80
    with pytest.raises(RuntimeExtensionRequired) as raised:
        retime_longform_narration(impossible)
    proposal = raised.value.proposal
    assert proposal.proposed_seconds > proposal.measured_minimum_seconds
    assert proposal.proposed_seconds > proposal.prior_ceiling_seconds
    impossible.longform_runtime_proposal = proposal
    runtime_review = review_message(
        impossible,
        Approval(
            id="runtime",
            project_id=impossible.id,
            gate="runtime",
            version=impossible.version,
            artifact_ids=[],
        ),
    )
    assert runtime_review.actions == ["approve", "trim", "reject"]
    assert "Keep script" in runtime_review.text
    assert "Trim script" in runtime_review.text
    buttons = TelegramAdapter(
        TelegramConfig(bot_token="123:" + "a" * 30, chat_id=123, allowed_user_ids=[42])
    ).payloads(runtime_review)[0]["reply_markup"]["inline_keyboard"][0]
    assert [button["text"] for button in buttons] == ["Keep script · extend", "Trim script", "Stop"]

    # Changed visual input invalidates only its chapter's render hash.
    first = project.spec.chapters[0].visual_beats[0]
    replacement_path = restarted.repo.project_dir(project.id) / "replacement.png"
    Image.new("RGB", (64, 64), "#cc0000").save(replacement_path)
    replacement = restarted.repo.artifact(project, replacement_path, "longform_visual")
    project.longform_media[first.asset_ref] = replacement
    project.artifacts.append(replacement)
    render_longform_chapters(restarted, project)
    assert len(renderer.rendered) == len(project.spec.chapters) + 1
    assert renderer.rendered[-1] == project.spec.chapters[0].id

    approved = studio.decide(project.id, "prototype", "approve", actor="test")
    assert approved.state == State.LONGFORM_MASTER_READY
    master = studio.repo.project_dir(project.id) / "final-video.mp4"
    assert master.read_bytes() == b"fixture-review"
    assert approved.current_artifacts("longform_master")

    approved.state = State.LONGFORM_RUNTIME_APPROVAL
    approved.longform_runtime_proposal = proposal
    approved.approvals.append(
        Approval(
            id="runtime-keep",
            project_id=approved.id,
            gate="runtime",
            version=approved.version,
            artifact_ids=[],
        )
    )
    studio.repo.save(approved, "test_runtime_review", {})
    kept = studio.decide(approved.id, "runtime", "approve", actor="test")
    assert kept.state == State.LONGFORM_AUDIO_PREPARING
    assert kept.longform_runtime_approved_seconds == proposal.proposed_seconds
    assert kept.spec.brief.duration_policy.expansion_maximum_seconds == proposal.proposed_seconds

    kept.state = State.LONGFORM_RUNTIME_APPROVAL
    kept.approvals.append(
        Approval(
            id="runtime-trim",
            project_id=kept.id,
            gate="runtime",
            version=kept.version,
            artifact_ids=[],
        )
    )
    studio.repo.save(kept, "test_runtime_review", {})
    trimmed = studio.revise(
        kept.id,
        "Trim repetition while preserving sourced claims",
        actor="test",
        approval_id="runtime-trim",
    )
    assert trimmed.state == State.LONGFORM_SCRIPTING
    assert trimmed.longform_runtime_proposal is None
    assert trimmed.longform_voices == {}


def test_review_variant_becomes_approved_longform_master(tmp_path, monkeypatch):
    from autonomous_studio.rendering.ffmpeg import FFmpeg

    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE))
    root = studio.repo.project_dir(project.id)
    original = root / "original.mp4"
    original.write_bytes(b"original review")
    artifact = studio.repo.artifact(project, original, "prototype", duration=2.0)
    project.artifacts.append(artifact)
    project.state = State.LONGFORM_REVIEW_APPROVAL
    project.approvals.append(Approval(
        id="review", project_id=project.id, gate="prototype",
        version=project.version, artifact_ids=[artifact.id],
    ))
    studio.repo.save(project, "test_review_ready", {})
    variant = root / "wrap-fix.mp4"
    variant.write_bytes(b"reviewed wrap fix")
    monkeypatch.setattr(FFmpeg, "inspect", lambda *_args, **_kwargs: {
        "has_video": True, "has_audio": True, "width": 1920,
        "height": 1080, "duration": 2.0,
    })

    with pytest.raises(ValueError, match="project"):
        studio.adopt_longform_review_variant(project.id, "../outside.mp4")
    adopted = studio.adopt_longform_review_variant(project.id, variant.name)
    assert len(adopted.current_artifacts("prototype")) == 2
    assert adopted.current_artifacts("prototype")[-1].id in adopted.pending().artifact_ids
    approved = Studio(tmp_path).decide(project.id, "prototype", "approve", actor="test")
    assert approved.state == State.LONGFORM_MASTER_READY
    assert (root / "final-video.mp4").read_bytes() == b"reviewed wrap fix"
    assert original.read_bytes() == b"original review"

    with pytest.raises(ValueError, match="title, description"):
        studio.register_longform_publication_package(project.id)
    (root / "youtube-title.txt").write_text("A Test Documentary\n", encoding="utf-8")
    (root / "youtube-description.txt").write_text("A useful description.", encoding="utf-8")
    Image.new("RGB", (1280, 720), "#18232c").save(root / "selected-thumbnail.png")
    packaged = studio.register_longform_publication_package(project.id)
    assert packaged.current_artifacts("longform_youtube_title")
    assert packaged.current_artifacts("longform_youtube_description")
    assert packaged.current_artifacts("longform_selected_thumbnail")
    assert Studio(tmp_path).repo.verify(
        packaged, packaged.current_artifacts("longform_selected_thumbnail")[-1]
    ).is_file()
    queued = studio.queue_longform_publication(project.id, actor="test")
    assert queued.state == State.PUBLICATION_CLEARANCE
    review = asyncio.run(Studio(tmp_path).run(project.id))
    assert review.state == State.PUBLICATION_CLEARANCE
    assert review.pending().gate == "publication"
    assert review.publication.title == "A Test Documentary"
    assert review.current_artifacts("rights_manifest")
    assert review.current_artifacts("usage_report")
    assert review_message(review, review.pending()).media
    revised = studio.revise(
        project.id, "title: A Better Documentary Title", actor="test",
        approval_id=review.pending().id,
    )
    assert revised.state == State.PUBLICATION_CLEARANCE and revised.pending() is None
    rereview = asyncio.run(Studio(tmp_path).run(project.id))
    assert rereview.pending().gate == "publication"
    assert rereview.publication.title == "A Better Documentary Title"
    finished = Studio(tmp_path).decide(project.id, "publication", "approve", actor="test")
    assert finished.state == State.COMPLETED
    assert (root / "youtube-metadata.json").is_file()
    assert (root / "release-status.json").is_file()
    assert (root / "youtube-title.txt").read_text(encoding="utf-8").strip() == "A Better Documentary Title"
    assert (root / "final-video.mp4").read_bytes() == b"reviewed wrap fix"


def test_oversized_longform_review_is_not_attached_to_telegram(tmp_path):
    studio = Studio(tmp_path)
    project = studio.create(load_brief(EXAMPLE))
    root = studio.repo.project_dir(project.id)
    preview = root / "preview.mp4"
    preview.write_bytes(b"fixture")
    artifact = studio.repo.artifact(project, preview, "prototype")
    root_copy = root / "review-video-v001.mp4"
    root_copy.write_bytes(b"fixture")
    project.artifacts.extend(
        [artifact, studio.repo.artifact(project, root_copy, "longform_review_root")]
    )
    approval = Approval(
        id="test",
        project_id=project.id,
        gate="prototype",
        version=1,
        artifact_ids=[artifact.id],
    )
    message = review_message(project, approval)
    assert message.media == []
    assert "review-video-v001.mp4" in message.text


def test_treatment_restart_does_not_reuse_previous_cut(tmp_path):
    studio = Studio(tmp_path, longform_renderer=FixtureRenderer())
    project = asyncio.run(studio.run(studio.create(load_brief(EXAMPLE)).id))
    studio.decide(project.id, "treatment", "approve", actor="test")
    project = asyncio.run(studio.run(project.id))
    studio.decide(project.id, "script", "approve", actor="test")
    project = asyncio.run(studio.run(project.id))
    studio.decide(project.id, "visual_plan", "approve", actor="test")
    project = asyncio.run(studio.run(project.id))
    assert project.state == State.LONGFORM_REVIEW_APPROVAL
    prior_media = set(project.longform_media)
    assert prior_media
    assert project.longform_music and project.longform_voices and project.longform_chapters
    project.longform_semantic_decisions[project.spec.chapters[0].id] = SemanticDecisionBatch(
        chapter_id=project.spec.chapters[0].id, decisions=[]
    )
    project.longform_semantic_hashes[project.spec.chapters[0].id] = "a" * 64
    studio.repo.save(project, "test_semantic_seed", {})

    fresh = studio.revise(
        project.id, "treatment: Rewrite around an evidence-led hook", actor="test"
    )
    assert fresh.state == State.LONGFORM_RESEARCHED
    assert fresh.version == project.version + 1
    assert fresh.spec.chapters == []
    assert fresh.longform_visual_plan is None
    assert fresh.longform_media == {}
    assert fresh.longform_semantic_decisions == {}
    assert fresh.longform_semantic_hashes == {}
    assert fresh.longform_voices == {}
    assert fresh.longform_word_timings == {}
    assert fresh.longform_music is None
    assert fresh.longform_chapters == {}
    assert fresh.creation_settings["fresh_media_epoch"]
    assert fresh.creation_settings["narration_linked_timeline"] is True
    assert (
        fresh.longform_visual_policy.maximum_generated_requests
        == project.longform_visual_policy.maximum_generated_requests
    )
    fresh = asyncio.run(Studio(tmp_path).run(project.id))
    assert fresh.state == State.TREATMENT_APPROVAL
    assert fresh.pending().gate == "treatment"


def test_research_restart_replans_from_original_brief_without_erasing_prior_version(tmp_path):
    studio = Studio(tmp_path)
    original = load_brief(EXAMPLE)
    original_policy = original.duration_policy.model_copy(deep=True)
    project = studio.create(original)
    project.spec.story_contract_version = 2
    project.spec.brief.duration_policy.target_seconds = 400
    project.spec.brief.duration_policy.maximum_seconds = 400
    project.longform_research_plan = asyncio.run(
        MockLongFormResearch().plan(project.spec.brief)
    )
    project.state = State.LONGFORM_REVIEW_APPROVAL
    project.approvals.append(Approval(
        id="old-review", project_id=project.id, gate="prototype",
        version=1, artifact_ids=[],
    ))
    studio.repo.save(project, "test_prior_cut", {})

    fresh = studio.revise(
        project.id, "research: Rebuild the story from new questions", actor="test"
    )
    assert fresh.version == 2
    assert fresh.state == State.LONGFORM_FOUNDATION_READY
    assert fresh.spec.brief.duration_policy.target_seconds == original_policy.target_seconds
    assert fresh.spec.brief.duration_policy.maximum_seconds == (
        original_policy.maximum_seconds
    )
    assert fresh.spec.story_contract_version == 2
    assert fresh.longform_research_plan is None
    assert fresh.approvals[0].decision == "revise"
    assert any(item.role == "brief" and item.version == 1 for item in fresh.artifacts)
    assert fresh.creation_settings["fresh_media_epoch"]

    next_gate = asyncio.run(studio.run(project.id))
    assert next_gate.state == State.TREATMENT_APPROVAL
    assert next_gate.version == 2
    assert len(next_gate.longform_research_plan.story_questions) >= 2
    assert next_gate.spec.story_choice is not None
