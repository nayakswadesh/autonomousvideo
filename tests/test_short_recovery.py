import asyncio
import json
from pathlib import Path

from test_live import directing_replies, response
from test_preview_policy import FakeRenderer

from autonomous_studio.audio.timing import measured_plan, voice_signature
from autonomous_studio.domain.models import (
    Approval,
    ComparisonShot,
    MetricSetShot,
    PreparedVoice,
    Presentation,
    ShotLook,
    State,
    TimelineShot,
    VoiceAsset,
)
from autonomous_studio.providers.mock import MockDirector
from autonomous_studio.providers.openai import ShotPlanDraft, normalize_shot_plan
from autonomous_studio.rendering.editorial import metric_card_value
from autonomous_studio.rendering.editorial import scene as editorial_scene
from autonomous_studio.workflow import Studio
from autonomous_studio.workflow.director_recovery import recover_director
from autonomous_studio.workflow.engine import load_brief
from autonomous_studio.workflow.publication import _provider_usage

FULL = Path(__file__).resolve().parents[1] / "examples/earthquake-full.yaml"


def full_project(tmp_path):
    studio = Studio(tmp_path)
    brief = load_brief(FULL)
    project = studio.create(brief, review_policy="preview", script_approval=True)
    project.spec = asyncio.run(MockDirector().develop(brief, []))
    return studio, project


def test_legacy_integer_script_lock_survives_float_reload(tmp_path):
    studio, project = full_project(tmp_path)
    output = studio.repo.attempt_dir(project, "lock-test")
    studio._json(project, output, "narration-validation.json", {"passed": True},
                 "narration_validation")
    lock = studio._script_lock_payload(project)
    lock["timeline_duration"] = int(project.spec.brief.duration)  # Old writer's numeric type.
    lock["lock_sha256"] = "legacy-numeric-hash"
    studio._json(project, output, "script-lock.json", lock, "script_lock")
    project.approvals.append(Approval(
        id="approved-script", project_id=project.id, gate="script", version=project.version,
        decision="approve", artifact_ids=[project.current_artifacts("script_lock")[-1].id],
    ))
    project.state = State.ASSETS_PREPARING
    studio.repo.save(project, "fixture", {})

    studio._verify_script_approval(studio.repo.load(project.id))


def test_full_short_retains_speech_and_breathing_room_under_ninety(tmp_path):
    studio, project = full_project(tmp_path)
    output = studio.repo.attempt_dir(project, "voice-fixture")
    for shot in project.spec.shots:
        for index, segment in enumerate(shot.voice_segments):
            path = output / f"{shot.id}-{index}.wav"
            path.write_bytes(b"fixture")
            duration = shot.duration + 1
            artifact = studio.repo.artifact(project, path, "voice", duration=duration)
            project.prepared_voices.append(PreparedVoice(
                shot_id=shot.id, segment_index=index,
                signature=voice_signature(project, segment),
                asset=VoiceAsset(
                    artifact=artifact, start=shot.start + segment.offset,
                    duration=duration, spoken_text=segment.spoken_text,
                    characters=len(segment.spoken_text), voice_id="fixture",
                ),
            ))
    proposed, _, mismatch = measured_plan(project)
    assert mismatch and proposed is not None
    assert 60 < proposed.brief.duration <= 90
    assert [v.spoken_text for s in proposed.shots for v in s.voice_segments] == [
        v.spoken_text for s in project.spec.shots for v in s.voice_segments
    ]


def test_longer_short_requires_explicit_runtime_approval(tmp_path):
    studio, project = full_project(tmp_path)
    project.state = State.NARRATION_PREPARING
    studio.repo.save(project, "fixture", {})
    output = studio.repo.attempt_dir(project, "runtime-choice")

    asyncio.run(studio._request_narration_review(
        project, output, [], proposal_seconds=112.5,
    ))
    pending = studio.repo.load(project.id)
    assert pending.pending().gate == "narration"
    assert pending.short_runtime_approved_seconds is None
    approved = studio.decide(project.id, "narration", "approve")
    assert approved.state == State.NARRATION_PREPARING
    assert approved.short_runtime_approved_seconds == 112.5


def test_semantic_shots_do_not_turn_disputes_or_one_study_into_fake_charts():
    plan = ShotPlanDraft.model_validate({"shots": [
        {"id": "a", "kind": "comparison", "start": 0, "duration": 8,
         "title": "Contested origins", "labels": ["Dissolution", "Mechanical erosion"],
         "values": [1, 1], "display_values": ["1 disputed mechanism"] * 2},
        {"id": "b", "kind": "timeline", "start": 8, "duration": 8,
         "title": "The study", "labels": [
             "2025 — visited and undisturbed sites compared",
             "2025 — tourism-associated waste documented",
             "2025 — microbial communities examined",
         ]},
        {"id": "c", "kind": "big_metric", "start": 16, "duration": 8,
         "title": "A distant star", "metric": "≈860 light-years away • ≈74 solar radii"},
    ]})
    normalize_shot_plan(plan)
    assert [shot.kind for shot in plan.shots] == [
        "qualitative_comparison", "study_design", "metric_pair"
    ]
    assert plan.shots[0].alternatives == ["Dissolution", "Mechanical erosion"]
    assert all(not step.startswith("2025") for step in plan.shots[1].steps)
    assert plan.shots[2].metrics == ["≈860 light-years away", "≈74 solar radii"]


def test_mixed_unit_comparison_becomes_independent_metrics():
    plan = ShotPlanDraft.model_validate({"shots": [
        {"id": "a", "kind": "comparison", "start": 0, "duration": 8,
         "title": "Scale estimates", "labels": ["Extent", "Large galaxies", "Mass"],
         "values": [160, 100000, 1e17],
         "display_values": ["≈160 megaparsecs", "≈100,000 large galaxies",
                            "≈100 quadrillion solar masses"]},
        {"id": "b", "kind": "comparison", "start": 8, "duration": 8,
         "title": "Comparable distances", "labels": ["A", "B"],
         "values": [100, 200], "display_values": ["100 km", "200 km"]},
    ]})
    normalize_shot_plan(plan)
    assert isinstance(plan.shots[0], MetricSetShot)
    assert plan.shots[0].display_values == [
        "≈160 megaparsecs", "≈100,000 large galaxies",
        "≈100 quadrillion solar masses",
    ]
    assert plan.shots[1].kind == "comparison"


def test_independent_metric_values_must_match_labels():
    from pydantic import ValidationError

    try:
        MetricSetShot(id="a", start=0, duration=8, title="Estimates",
                      labels=["Extent", "Mass"], display_values=["≈160 megaparsecs"])
    except ValidationError:
        pass
    else:
        raise AssertionError("mismatched metric cards should fail validation")


def test_two_item_chart_keeps_full_qualified_labels_readable(tmp_path):
    studio, project = full_project(tmp_path)
    from autonomous_studio.domain.models import ChartShot

    shot = ChartShot(
        id="chart", start=0, duration=10, title="Direct searches",
        subtitle="No candidate planet detected.",
        labels=["Excluded by combined survey constraints",
                "Not excluded within the modeled population"],
        values=[78, 22],
        display_values=["78 percent of modeled parameter space excluded",
                        "22 percent of modeled parameter space not excluded"],
        takeaway="The surveys excluded much of one modeled population, not every possibility.",
    )
    project.spec.shots = [shot]
    project.spec.presentation = Presentation(shots=[
        ShotLook(shot_id=shot.id, visual_strategy="chart", headline="Direct searches")
    ])
    editorial_scene(project, shot)


def test_mixed_unit_metric_cards_fit_editorial_layout(tmp_path):
    studio, project = full_project(tmp_path)
    shot = MetricSetShot(
        id="estimates", start=0, duration=8, title="Estimated scale",
        labels=["Extent", "Large galaxies", "Mass"],
        display_values=["≈160 megaparsecs", "≈100,000 large galaxies",
                        "≈100 quadrillion solar masses"],
    )
    project.spec.shots = [shot]
    project.spec.presentation = Presentation(shots=[
        ShotLook(shot_id=shot.id, visual_strategy="diagram", headline="Estimated scale")
    ])
    records = []
    editorial_scene(project, shot, records=records)
    rendered = {record["text"] for record in records}
    assert all(label.upper() in rendered for label in shot.labels)
    assert all(metric_card_value(label, value) in rendered
               for label, value in zip(shot.labels, shot.display_values, strict=True))
    assert "≈100,000 large galaxies" not in rendered  # Category is already the card label.


def test_semantic_preview_reframe_reuses_audio_and_requires_new_review(tmp_path):
    studio = Studio(tmp_path)
    studio.renderer = FakeRenderer(studio.repo)
    brief = load_brief(FULL)
    original = asyncio.run(studio.run(
        studio.create(brief, review_policy="preview").id
    ))
    first, second = original.spec.shots[1:3]
    original.spec.shots[1] = ComparisonShot.model_validate({
        **first.model_dump(mode="json", exclude={"kind", "rank", "metric"}),
        "kind": "comparison", "labels": ["Dissolution", "Mechanical erosion"],
        "values": [1, 1], "display_values": ["1 disputed mechanism"] * 2,
    })
    original.spec.shots[2] = TimelineShot.model_validate({
        **second.model_dump(mode="json", exclude={"kind", "rank", "metric"}),
        "kind": "timeline", "labels": ["2025 — sites compared", "2025 — waste documented"],
    })
    original.spec.presentation = Presentation(shots=[
        ShotLook(shot_id=shot.id, visual_strategy="chart")
        for shot in original.spec.shots
    ])
    studio.repo.save(original, "legacy_visual_fixture", {})
    old_voice_hashes = [voice.asset.artifact.sha256 for voice in original.prepared_voices]

    refreshed = studio.refresh_preview(
        original.id, version=original.version, reason="Fix false visual encodings",
        semantic_visuals=True,
    )
    assert refreshed.state == State.AUDIO_PREPARED
    assert [shot.kind for shot in refreshed.spec.shots[1:3]] == [
        "qualitative_comparison", "study_design"
    ]
    assert [voice.asset.artifact.sha256 for voice in refreshed.prepared_voices] == old_voice_hashes
    assert original.pending().id == refreshed.approvals[-1].id
    assert refreshed.approvals[-1].decision == "superseded"
    assert all(
        look.visual_strategy == "diagram"
        for look in refreshed.spec.presentation.shots[1:3]
    )


def test_lifetime_usage_includes_uncommitted_validation_response(tmp_path):
    studio, project = full_project(tmp_path)
    cache = studio.repo.project_dir(project.id) / "provider-cache"
    cache.mkdir()
    (cache / "failed.json").write_text(json.dumps({
        "id": "response-paid-but-rejected", "model": "fixture-model",
        "usage": {"input_tokens": 123, "output_tokens": 45},
    }), encoding="utf-8")
    assert len(_provider_usage(studio.repo, project)[0]) == 0
    _, models, _ = _provider_usage(studio.repo, project, all_versions=True)
    assert models["fixture-model"]["input_tokens"] == 123


def test_cached_director_recovery_never_requests_an_api(tmp_path):
    studio = Studio(tmp_path)
    brief = load_brief(FULL)
    project = studio.create(brief, pipeline="live", review_policy="preview")
    fixture = asyncio.run(MockDirector().develop(brief, []))
    narrative, plan = directing_replies(fixture)
    cache = studio.repo.project_dir(project.id) / "provider-cache"
    cache.mkdir()
    (cache / "old.validated-narrative.json").write_text(json.dumps({
        "narrative": narrative,
        "provider_response": response(json.dumps(narrative)),
    }), encoding="utf-8")
    shot_path = cache / "failed-shot-plan.json"
    shot_path.write_text(json.dumps(response(json.dumps(plan))), encoding="utf-8")
    project.state = State.RESEARCHED
    project.last_error = "Director shot plan failed validation after bounded internal repairs"
    project.last_error_code = "provider_validation"
    studio.repo.save(project, "fixture", {})

    recovered = asyncio.run(recover_director(
        studio, project.id, shot_path, version=project.version,
    ))
    assert recovered.state == State.NARRATION_PREPARING
    assert recovered.spec.narration_script == narrative["narration_script"]
    assert studio.repo.events(project.id)[-1]["payload"]["additional_api_calls"] == 0
