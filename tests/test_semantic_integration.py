from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from autonomous_studio.audio.alignment import estimated_words
from autonomous_studio.domain.motion import MotionPlan
from autonomous_studio.domain.semantic import SemanticDecision, SemanticDecisionBatch
from autonomous_studio.providers.semantic_director import (
    OpenAIFamilyDirector,
    _salvage_valid_decisions,
)
from autonomous_studio.rendering.semantic_diagrams import (
    ConceptContrastScene,
    JourneyScene,
    render_frame,
    scene_cues,
)
from autonomous_studio.workflow.semantic_integration import (
    align_scene_to_measured_speech,
    apply_semantic_decisions,
    normalize_scene_time_placeholders,
    validate_decision_inputs,
)
from autonomous_studio.workflow.semantic_scene_contract import build_scene_contract

ROOT = Path(__file__).resolve().parents[1]


def test_late_place_gets_measured_subject_opening_without_early_place_reveal(caplog):
    narration = (
        "The team gathered supplies before reaching Valparaiso, "
        "then traveled to Santiago to inspect the lab."
    )
    words = [
        {"word": word, "start": index * 0.5, "end": (index + 1) * 0.5}
        for index, word in enumerate(narration.split())
    ]
    data = {
        "id": "generic-journey", "topic": "A survey expedition", "title": "The journey",
        "narration": narration, "duration": 8.0, "family": "journey", "treatment": "route",
        "opening_label": "The survey team", "opening_anchor": "The team", "opening_at": 0.2,
        "places": [
            {"label": "Valparaiso", "detail": "First stop", "anchor": "Valparaiso",
             "symbol": "pin", "at": 1.0, "x": 0.2, "y": 0.5},
            {"label": "Santiago", "detail": "Next stop", "anchor": "Santiago",
             "symbol": "pin", "at": 4.0, "x": 0.8, "y": 0.5},
        ],
    }
    scene = JourneyScene.model_validate(data)
    without_opening = scene.model_copy(update={"opening_label": "", "opening_anchor": ""})
    align_scene_to_measured_speech(
        without_opening, narration, {"start": 0, "words": words}, 0, 8
    )
    assert "measured reveal gap opening" in caplog.text
    aligned, absolute = align_scene_to_measured_speech(
        scene, narration, {"start": 0, "words": words}, 0, 8
    )
    assert absolute == pytest.approx([0.04, 3.0, 5.0])
    assert [anchor for anchor, _ in scene_cues(aligned)] == [
        "The team", "Valparaiso", "Santiago"
    ]
    assert aligned.places[0].at == 3.0
    assert render_frame(aligned, 0.5, presentation="production").tobytes() != render_frame(
        without_opening, 0.5, presentation="production"
    ).tobytes()


def test_adjacent_spoken_roles_with_tied_asr_onsets_keep_script_order():
    narration = (
        "A clue revealed elements. Henri Becquerel found the rays. Curie measured them."
    )
    words = estimated_words(narration, 7.0)
    words[4]["start"] = words[3]["start"]  # "Henri" and "elements" share an ASR onset.
    scene = ConceptContrastScene.model_validate({
        "id": "tied-words", "topic": "Scientific discoveries", "title": "Two contributions",
        "narration": narration, "duration": 8.0, "family": "concept_contrast",
        "treatment": "juxtaposition", "opening_label": "The clue",
        "opening_anchor": "elements", "opening_at": 1.0,
        "left": {"label": "Becquerel", "anchor": "Henri Becquerel", "symbol": "box",
                 "at": 2.0},
        "right": {"label": "Curie", "anchor": "Curie measured", "symbol": "box",
                  "at": 4.0},
    })
    aligned, times = align_scene_to_measured_speech(
        scene, narration, {"start": 0.0, "words": words}, 0.0, 8.0
    )
    assert times[0] < times[1] < times[2]
    assert times[1] - times[0] == pytest.approx(0.04)
    assert aligned.left.at > aligned.opening_at
    _, boundary_times = align_scene_to_measured_speech(
        scene, narration, {"start": 0.0, "words": words}, times[0], 8.0
    )
    assert boundary_times[0] < boundary_times[1] < boundary_times[2]


def test_repeated_word_anchor_requires_a_phrase_from_the_intended_spoken_slice():
    narration = (
        "An early measurement puzzled the team. "
        "Curie's significance rests not on myth but on measurement and collaboration."
    )
    words = estimated_words(narration, 10.0)
    opening = "Curie's significance"
    shot_start = 10 * len(narration.split("Curie's")[0].split()) / len(narration.split())
    data = {
        "id": "repeated-anchor", "topic": "A scientist's legacy", "title": "The synthesis",
        "narration": narration, "duration": 10.0, "family": "concept_contrast",
        "treatment": "juxtaposition", "opening_label": "Significance",
        "opening_anchor": opening, "opening_at": 0.2,
        "left": {"label": "Measurement", "anchor": "measurement", "symbol": "box", "at": 2.0},
        "right": {"label": "Collaboration", "anchor": "collaboration", "symbol": "box", "at": 4.0},
    }
    scene = ConceptContrastScene.model_validate(data)
    with pytest.raises(ValueError, match="'measurement'@word 2.*longer, unambiguous phrases"):
        align_scene_to_measured_speech(
            scene, narration, {"start": 0.0, "words": words}, shot_start, 10.0
        )
    scene.left.anchor = "but on measurement"
    aligned, entrances = align_scene_to_measured_speech(
        scene, narration, {"start": 0.0, "words": words}, shot_start, 10.0
    )
    assert aligned.left.anchor == "but on measurement"
    assert entrances == sorted(entrances)


def test_director_absolute_time_guesses_are_replaced_before_scene_validation():
    narration = "Maria left Warsaw and then reached Paris for her studies."
    payload = {
        "id": "journey-clock", "topic": "A scientist's journey", "title": "Warsaw to Paris",
        "narration": narration, "duration": 12.0, "family": "journey",
        "treatment": "route", "opening_label": "Maria's journey",
        "opening_anchor": "Maria", "opening_at": 19.87,
        "places": [
            {"label": "Warsaw", "detail": "Departure", "anchor": "Warsaw",
             "symbol": "pin", "at": 21.75, "x": 0.2, "y": 0.5},
            {"label": "Paris", "detail": "Arrival", "anchor": "Paris",
             "symbol": "pin", "at": 28.51, "x": 0.8, "y": 0.5},
        ],
    }
    with pytest.raises(ValueError, match="opening cue must occur within the shot"):
        JourneyScene.model_validate(payload)
    normalized = normalize_scene_time_placeholders(payload, 8.0)
    scene = JourneyScene.model_validate(normalized)
    assert 0 < scene.opening_at < scene.places[0].at < scene.places[1].at < 8.0
    assert scene.narration == narration


def test_duplicate_opening_anchor_is_removed_when_first_role_is_already_early():
    narration = "Henri Becquerel found rays. Curie measured them."
    scene = ConceptContrastScene.model_validate({
        "id": "duplicate-opening", "topic": "Scientific measurement",
        "title": "Two contributions", "narration": narration, "duration": 7.0,
        "family": "concept_contrast", "treatment": "juxtaposition",
        "opening_label": "Becquerel", "opening_anchor": "Henri Becquerel",
        "opening_at": 0.2,
        "left": {"label": "Becquerel", "anchor": "Henri Becquerel",
                 "symbol": "box", "at": 1.0},
        "right": {"label": "Curie", "anchor": "Curie measured",
                  "symbol": "box", "at": 4.0},
    })
    aligned, times = align_scene_to_measured_speech(
        scene, narration, {"start": 0.0, "words": estimated_words(narration, 5.0)},
        0.0, 7.0,
    )
    assert aligned.opening_anchor == ""
    assert len(times) == 2 and times[0] < times[1]


def test_opening_prefix_of_first_role_is_removed_without_another_model_call():
    narration = "Henri Becquerel found rays. Curie measured them."
    scene = ConceptContrastScene.model_validate({
        "id": "overlapping-opening", "topic": "Scientific measurement",
        "title": "Two contributions", "narration": narration, "duration": 7.0,
        "family": "concept_contrast", "treatment": "juxtaposition",
        "opening_label": "Becquerel", "opening_anchor": "Henri Becquerel",
        "opening_at": 0.2,
        "left": {"label": "Discovered rays", "anchor": "Henri Becquerel found rays",
                 "symbol": "box", "at": 1.0},
        "right": {"label": "Measured them", "anchor": "Curie measured them",
                  "symbol": "box", "at": 4.0},
    })
    aligned, times = align_scene_to_measured_speech(
        scene, narration, {"start": 0.0, "words": estimated_words(narration, 5.0)},
        0.0, 7.0,
    )
    assert aligned.opening_anchor == ""
    assert len(times) == 2 and times[0] < times[1]


def _fixture():
    scene = json.loads((ROOT / "examples/semantic_asset_studies.json").read_text(encoding="utf-8"))[
        0
    ]
    narration = scene["narration"]
    project = {
        "id": "fixture",
        "spec": {
            "chapters": [
                {
                    "id": "ch1",
                    "target_duration": 7.0,
                    "evidence_claim_ids": ["c1"],
                    "evidence_source_ids": ["s1"],
                    "narration_blocks": [{"id": "b1", "text": narration}],
                    "visual_beats": [
                        {
                            "id": "v1",
                            "visual_type": "timeline",
                            "start": 0,
                            "duration": 7,
                            "narration_block_ids": ["b1"],
                            "claim_ids": ["c1"],
                            "source_ids": ["s1"],
                        }
                    ],
                }
            ]
        },
        "longform_media": {},
    }
    plan = MotionPlan.model_validate(
        {
            "project_id": "fixture",
            "chapter_id": "ch1",
            "comparison_name": "semantic-test",
            "width": 1920,
            "height": 1080,
            "fps": 24,
            "shots": [
                {
                    "kind": "process_steps",
                    "id": "v1",
                    "start": 0,
                    "end": 7,
                    "intent": "explain_process",
                    "viewer_takeaway": "A library return has stages.",
                    "why_this_template": "The old template used simple labeled steps.",
                    "narration_block_ids": ["b1"],
                    "claim_ids": ["c1"],
                    "source_ids": ["s1"],
                    "title": "Book return",
                    "steps": ["Check", "Shelve"],
                    "cues": [
                        {
                            "at_seconds": 0.2,
                            "narration_excerpt": "Returned books",
                            "action": "reveal_step",
                            "step_index": 0,
                        },
                        {
                            "at_seconds": 4.2,
                            "narration_excerpt": "placed back on shelves",
                            "action": "reveal_step",
                            "step_index": 1,
                        },
                    ],
                }
            ],
        }
    )
    windows = [
        {"block_id": "b1", "start": 0.1, "end": 6.9, "words": estimated_words(narration, 6.8)}
    ]
    decision = SemanticDecision(
        beat_id="v1",
        mode="diagram",
        scene_json=json.dumps(scene),
        chosen_because="The working diagram retains each step and reveals it on speech.",
        rejected_alternative="A static card hides the step sequence.",
    )
    batch = SemanticDecisionBatch(chapter_id="ch1", decisions=[decision])
    return project, plan, windows, batch


def test_measured_words_convert_proposal_to_native_semantic_motion():
    project, base, windows, batch = _fixture()
    result, report = apply_semantic_decisions(project, "ch1", base, windows, batch)
    assert report["applied"] == 1
    shot = result.shots[0]
    assert shot.kind == "semantic_diagram"
    assert shot.scene["family"] == "process"
    assert shot.scene["steps"][1]["at"] == pytest.approx(shot.cues[1].at_seconds - shot.start)
    assert [cue.narration_excerpt for cue in shot.cues] == [
        item["anchor"] for item in shot.scene["steps"]
    ]
    assert report["decisions"][0]["cue_alignment"][0]["phrase"] == shot.cues[0].narration_excerpt
    assert report["decisions"][0]["claim_ids"] == ["c1"]


def test_semantic_rationale_survives_motion_handoff_at_full_allowed_length():
    project, base, windows, batch = _fixture()
    rationale = "The approved design uses measured speech. " + "Asset provenance verified. " * 20
    rationale = rationale[:500]
    assert len(rationale) == 500
    batch.decisions[0].chosen_because = rationale
    result, report = apply_semantic_decisions(project, "ch1", base, windows, batch)
    assert result.shots[0].why_this_template == rationale
    assert report["decisions"][0]["reason"] == rationale


def test_family_first_requires_every_designed_beat_and_eliminates_legacy_shots():
    project, base, windows, batch = _fixture()
    project["spec"]["chapters"][0]["visual_beats"][0].update(
        design_family="process", design_treatment="working"
    )
    batch.schema_version = 2
    result, report = apply_semantic_decisions(project, "ch1", base, windows, batch)
    assert report["applied"] == 1
    assert all(shot.kind == "semantic_diagram" for shot in result.shots)
    batch.decisions = []
    with pytest.raises(ValueError, match="every code-rendered beat"):
        validate_decision_inputs(project, "ch1", batch, base)


def test_family_first_requires_an_approved_family_on_each_code_beat():
    project, base, _, batch = _fixture()
    batch.schema_version = 2
    with pytest.raises(ValueError, match="no approved family"):
        validate_decision_inputs(project, "ch1", batch, base)


def test_family_first_rejects_mismatched_treatment_and_off_window_scene():
    project, base, windows, batch = _fixture()
    project["spec"]["chapters"][0]["visual_beats"][0].update(
        design_family="process", design_treatment="transformation"
    )
    batch.schema_version = 2
    with pytest.raises(ValueError, match="approved family"):
        validate_decision_inputs(project, "ch1", batch, base)
    project["spec"]["chapters"][0]["visual_beats"][0]["design_treatment"] = "working"
    windows[0]["words"] = estimated_words(
        project["spec"]["chapters"][0]["narration_blocks"][0]["text"], 16.0
    )
    with pytest.raises(ValueError, match="failed alignment"):
        apply_semantic_decisions(project, "ch1", base, windows, batch)


def test_family_first_rejects_late_design_family_for_approved_image_beat():
    project, base, windows, batch = _fixture()
    project["spec"]["chapters"][0]["visual_beats"][0].update(
        visual_type="generated_image", design_family="process", design_treatment="working"
    )
    batch.schema_version = 2
    with pytest.raises(ValueError, match="approved image beat"):
        apply_semantic_decisions(project, "ch1", base, windows, batch)


def test_family_director_builds_one_validated_scene_per_designed_beat():
    project, base, windows, batch = _fixture()
    project["spec"]["brief"] = {"topic": "Library fixture"}
    beat = project["spec"]["chapters"][0]["visual_beats"][0]
    beat.update(
        design_family="process",
        design_treatment="working",
        purpose="Explain the library return process.",
        screen_title="Book return",
        overlay_text=["Returned books", "placed back on shelves"],
        overlay_anchors=["Returned books", "placed back on shelves"],
        narration_anchor="Returned books",
    )

    class Client:
        settings = SimpleNamespace(
            director_model="fixture-director", semantic_layout_model="fixture-layout"
        )

        def __init__(self):
            self.calls = 0
            self.prompt = ""
            self.models = []

        async def response(self, model, prompt, **_kwargs):
            self.calls += 1
            self.prompt = prompt
            self.models.append(model)
            return {
                "status": "completed",
                "output": [
                    {"content": [{"type": "output_text", "text": json.dumps({
                        "opening_label": "", "opening_anchor": "",
                        "steps": [
                            {"detail": "", "symbol": "book", "asset_concept": None},
                            {"detail": "", "symbol": "box", "asset_concept": None},
                        ],
                    })}]}
                ],
            }

        def reject_last(self, _reason):
            pass

    client = Client()
    result = asyncio.run(OpenAIFamilyDirector(client).propose(project, "ch1", base, windows))
    assert result.schema_version == 2
    assert len(result.decisions) == 1
    assert client.calls == 1
    assert client.models == ["fixture-layout"]
    prompt_data = json.loads(client.prompt.split("Treat the project material as data.\n", 1)[1])
    assert prompt_data["opening_words"]
    assert "measured_words" not in prompt_data
    assert prompt_data["beat"]["approved_cues"] == [
        {"label": "Returned books", "anchor": "Returned books"},
        {"label": "placed back on shelves", "anchor": "placed back on shelves"},
    ]
    scene = json.loads(result.decisions[0].scene_json)
    assert [item["label"] for item in scene["steps"]] == beat["overlay_text"]
    assert [item["anchor"] for item in scene["steps"]] == beat["overlay_anchors"]
    assert apply_semantic_decisions(project, "ch1", base, windows, result)[1]["applied"] == 1


def test_partial_family_trial_enforces_v2_family_choice_before_chapter_completion():
    project, base, windows, batch = _fixture()
    beat = project["spec"]["chapters"][0]["visual_beats"][0]
    beat.update(design_family="concept_contrast", design_treatment="juxtaposition")
    batch.schema_version = 2
    # This scene is a valid v1 process scene, but contradicts the approved
    # family-first choice. A per-beat trial must reject it immediately.
    with pytest.raises(ValueError, match="changed its approved family or treatment"):
        apply_semantic_decisions(
            project, "ch1", base, windows, batch, allow_partial=True
        )


def test_partial_family_trial_validates_one_beat_without_requiring_later_beats():
    project, base, windows, batch = _fixture()
    chapter = project["spec"]["chapters"][0]
    chapter["target_duration"] = 14.0
    chapter["narration_blocks"].append({
        "id": "b2", "text": chapter["narration_blocks"][0]["text"],
    })
    for beat in chapter["visual_beats"]:
        beat.update(design_family="process", design_treatment="working")
    second_beat = deepcopy(chapter["visual_beats"][0])
    second_beat.update(id="v2", start=7, narration_block_ids=["b2"])
    chapter["visual_beats"].append(second_beat)
    second_shot = deepcopy(base.shots[0].model_dump(mode="json"))
    second_shot.update(id="v2", start=7, end=14, narration_block_ids=["b2"])
    for cue in second_shot["cues"]:
        cue["at_seconds"] += 7
    base = MotionPlan.model_validate({
        **base.model_dump(mode="json"),
        "shots": [base.shots[0].model_dump(mode="json"), second_shot],
    })
    windows.append({
        "block_id": "b2", "start": 7.1, "end": 13.9,
        "words": estimated_words(chapter["narration_blocks"][1]["text"], 6.8),
    })
    batch.schema_version = 2

    partial, report = apply_semantic_decisions(
        project, "ch1", base, windows, batch, allow_partial=True
    )
    assert report["applied"] == 1
    assert [shot.kind for shot in partial.shots] == ["semantic_diagram", "process_steps"]
    with pytest.raises(ValueError, match="every code-rendered beat"):
        apply_semantic_decisions(project, "ch1", base, windows, batch)

    second_scene = json.loads(batch.decisions[0].scene_json)
    second_scene["id"] = "v2"
    batch.decisions.append(batch.decisions[0].model_copy(update={
        "beat_id": "v2", "scene_json": json.dumps(second_scene),
    }))
    completed, final_report = apply_semantic_decisions(
        project, "ch1", base, windows, batch
    )
    assert final_report["applied"] == 2
    assert all(shot.kind == "semantic_diagram" for shot in completed.shots)


def test_family_director_rejects_model_owned_narration():
    project, base, windows, batch = _fixture()
    project["spec"]["brief"] = {"topic": "Library fixture"}
    beat = project["spec"]["chapters"][0]["visual_beats"][0]
    beat.update(
        design_family="process", design_treatment="working",
        purpose="Explain the library return process.", screen_title="Book return",
        overlay_text=["Returned books", "placed back on shelves"],
        overlay_anchors=["Returned books", "placed back on shelves"],
        narration_anchor="Returned books",
    )
    scene = {
        "narration": "A model-generated paraphrase that must not enter production.",
        "opening_label": "", "opening_anchor": "",
        "steps": [
            {"detail": "", "symbol": "book", "asset_concept": None},
            {"detail": "", "symbol": "box", "asset_concept": None},
        ],
    }

    class Client:
        settings = SimpleNamespace(director_model="fixture-director")

        async def response(self, _model, _prompt, **_kwargs):
            return {
                "status": "completed",
                "output": [{"content": [{"type": "output_text", "text": json.dumps(scene)}]}],
            }

        def reject_last(self, _reason):
            pass

    with pytest.raises(ValueError, match="project-owned scene fields"):
        asyncio.run(OpenAIFamilyDirector(Client()).propose(project, "ch1", base, windows))


def test_semantic_request_excludes_known_fields_and_fixes_reveal_count():
    beat = {
        "id": "beat-1", "screen_title": "A process", "overlay_text": ["Cloud collapses", "Disk forms"],
        "overlay_anchors": ["cloud collapses", "disk forms"],
    }
    contract = build_scene_contract(beat, "process", "working")
    assert set(contract.response_schema["properties"]) == {
        "opening_label", "opening_anchor", "steps",
    }
    assert contract.response_schema["properties"]["steps"]["maxItems"] == 2
    step_fields = contract.response_schema["$defs"]["Step"]["properties"]
    assert not {"label", "anchor", "at"}.intersection(step_fields)
    payload = contract.assemble(
        {
            "opening_label": "", "opening_anchor": "",
            "steps": [
                {"detail": "", "symbol": "cloud", "asset_concept": None},
                {"detail": "", "symbol": "box", "asset_concept": None},
            ],
        },
        beat=beat, narration="The cloud collapses and then a disk forms.",
        topic="A star", duration=8.0,
    )
    assert [item["label"] for item in payload["steps"]] == beat["overlay_text"]
    with pytest.raises(ValueError, match="changed the approved cue count"):
        contract.assemble(
            {"opening_label": "", "opening_anchor": "", "steps": [{}]},
            beat=beat, narration="The cloud collapses and then a disk forms.",
            topic="A star", duration=8.0,
        )
    with pytest.raises(ValueError, match="changed the approved cue count"):
        contract.assemble(
            {"opening_label": "", "opening_anchor": "", "steps": [{}, {}, {}]},
            beat=beat, narration="The cloud collapses and then a disk forms.",
            topic="A star", duration=8.0,
        )


def test_metric_reveal_uses_approved_subject_and_number_cues_without_new_labels():
    beat = {
        "id": "metric-beat", "screen_title": "Early solids",
        "overlay_text": ["Meteorite inclusions", "About 4.567 billion years"],
        "overlay_anchors": ["ancient meteorites", "about 4.567 billion years ago"],
    }
    contract = build_scene_contract(beat, "quantitative_display", "metric_reveal")
    assert contract.opening == (
        "Meteorite inclusions", "ancient meteorites",
    )
    assert "opening_label" not in contract.response_schema["properties"]
    assert contract.response_schema["properties"]["points"]["maxItems"] == 1
    assembled = contract.assemble(
        {"points": [{"value": 4.567, "date": ""}], "unit": "billion years"},
        beat=beat,
        narration="Clues lie in ancient meteorites, about 4.567 billion years ago.",
        topic="The Sun", duration=7.0,
    )
    assert assembled["opening_label"] == beat["overlay_text"][0]
    assert assembled["points"][0]["label"] == beat["overlay_text"][1]
    assert assembled["points"][0]["anchor"] == beat["overlay_anchors"][1]


def test_new_metric_plan_supplies_approved_value_and_unit_without_model_output():
    beat = {
        "id": "metric-beat", "screen_title": "Early solids",
        "overlay_text": ["Meteorite inclusions", "About 4.567 billion years"],
        "overlay_anchors": ["ancient meteorites", "about 4.567 billion years ago"],
        "visual_data": {"values": [4.567], "unit": "billion years", "dates": []},
    }
    contract = build_scene_contract(beat, "quantitative_display", "metric_reveal")
    assert "unit" not in contract.response_schema["properties"]
    point_fields = contract.response_schema["$defs"]["QuantPoint"]["properties"]
    assert "value" not in point_fields
    data = contract.assemble(
        {"points": [{"date": ""}]}, beat=beat,
        narration="Ancient meteorites formed about 4.567 billion years ago.",
        topic="The Sun", duration=7.0,
    )
    assert data["points"][0]["value"] == 4.567
    assert data["unit"] == "billion years"


def test_approved_legacy_process_cue_wraps_locally_without_dropping_words():
    long_label = "Red giant → uncertain Earth → white dwarf"
    beat = {
        "id": "sun-future", "screen_title": "Future of the Sun",
        "overlay_text": ["The Sun expands", long_label],
        "overlay_anchors": ["the Sun expands", "red giant then white dwarf"],
    }
    contract = build_scene_contract(beat, "process", "working")
    data = contract.assemble(
        {"opening_label": "", "opening_anchor": "", "steps": [
            {"detail": "", "symbol": "sun", "asset_concept": None},
            {"detail": "", "symbol": "box", "asset_concept": None},
        ]},
        beat=beat,
        narration="First the Sun expands; then a red giant then white dwarf.",
        topic="The Sun", duration=8.0,
    )
    second = data["steps"][1]
    assert second["label"] + " " + second["detail"] == long_label
    assert len(second["label"]) <= 34
    assert len(second["detail"]) <= 42


def test_unreviewed_generated_asset_cannot_enter_production():
    project, _, _, batch = _fixture()
    scene = json.loads(batch.decisions[0].scene_json)
    scene["steps"][0]["asset_concept"] = "solar streetlight"
    batch.decisions[0].scene_json = json.dumps(scene)
    with pytest.raises(ValueError, match="unreviewed"):
        validate_decision_inputs(project, "ch1", batch)


def test_unspoken_number_and_changed_script_fail_before_render():
    project, _, _, batch = _fixture()
    scene = json.loads(batch.decisions[0].scene_json)
    scene["steps"][0]["label"] = "12 books"
    batch.decisions[0].scene_json = json.dumps(scene)
    with pytest.raises(ValueError, match="unspoken number"):
        validate_decision_inputs(project, "ch1", batch)
    scene["narration"] = (
        "A new sentence. " + project["spec"]["chapters"][0]["narration_blocks"][0]["text"]
    )
    batch.decisions[0].scene_json = json.dumps(scene)
    with pytest.raises(ValueError, match="changed approved narration"):
        validate_decision_inputs(project, "ch1", batch)


def test_off_window_proposal_abstains_without_mutating_original_plan():
    project, base, windows, batch = _fixture()
    original = deepcopy(base.model_dump(mode="json"))
    windows[0]["end"] = 12.1
    windows[0]["words"] = estimated_words(
        project["spec"]["chapters"][0]["narration_blocks"][0]["text"], 16.0
    )
    result, report = apply_semantic_decisions(project, "ch1", base, windows, batch)
    assert report["applied"] == 0
    assert result.shots[0].kind == "process_steps"
    assert base.model_dump(mode="json") == original


def test_late_first_reveal_is_advisory_not_semantic_rejection(caplog):
    project, base, windows, batch = _fixture()
    words = windows[0]["words"]
    shift = 3.0
    for word in words:
        word["start"] += shift
        word["end"] += shift
    result, report = apply_semantic_decisions(project, "ch1", base, windows, batch)
    assert "measured reveal gap opening" in caplog.text
    assert report["applied"] == 1
    assert result.shots[0].kind == "semantic_diagram"


def test_long_semantic_middle_and_end_gaps_warn_without_changing_speech(caplog):
    narration = (
        "An early measurement puzzled the team. "
        "Much later the team identified the source. "
        "That result changed the investigation."
    )
    scene = ConceptContrastScene.model_validate({
        "id": "long-hold", "topic": "An investigation", "title": "Two moments",
        "narration": narration, "duration": 30.0, "family": "concept_contrast",
        "treatment": "juxtaposition",
        "left": {"label": "Early measurement", "anchor": "An early measurement",
                 "symbol": "box", "at": 1.0},
        "right": {"label": "Source identified", "anchor": "the team identified the source",
                  "symbol": "box", "at": 18.0},
    })
    aligned, times = align_scene_to_measured_speech(
        scene, narration, {"start": 0.0, "words": estimated_words(narration, 26.0)},
        0.0, 30.0,
    )
    assert aligned.narration == narration
    assert times == sorted(times)
    assert "measured reveal gap middle" in caplog.text
    assert "measured reveal gap ending" in caplog.text


def test_invalid_generated_diagram_abstains_without_losing_valid_chapter_decisions():
    project, _, _, batch = _fixture()
    invalid = deepcopy(batch.decisions[0])
    scene = json.loads(invalid.scene_json)
    scene["steps"][0]["detail"] = "An excessively long detail that cannot fit in this layout"
    invalid.scene_json = json.dumps(scene)
    broken = SemanticDecisionBatch(chapter_id="ch1", decisions=[invalid])
    result = _salvage_valid_decisions(project, "ch1", broken, "layout error")
    assert result.decisions[0].mode == "keep"
    assert result.decisions[0].scene_json == ""
