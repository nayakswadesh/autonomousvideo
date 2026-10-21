"""Long-form text stages have explicit, durable model choices."""

from autonomous_studio.config import Settings
from autonomous_studio.domain.semantic import SemanticDecisionBatch
from autonomous_studio.workflow import Studio


def test_new_longform_project_snapshots_distinct_stage_models(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_longform_topic("How a telescope works")
    assert {key: project.live_models[key] for key in (
        "research_model", "story_model", "continuity_model", "visual_plan_model",
        "visual_repair_model", "semantic_layout_model",
    )} == {
        "research_model": "gpt-6.1-sol",
        "story_model": "gpt-6.1-sol",
        "continuity_model": "gpt-6-luna",
        "visual_plan_model": "gpt-6.1-sol",
        "visual_repair_model": "gpt-6-luna",
        "semantic_layout_model": "gpt-6-luna",
    }
    assert project.live_models["director_model"] == "gpt-5.6-sol"
    assert project.live_models["tts_model"] == "gpt-4o-mini-tts"


def test_adoption_pins_paid_chapters_and_persists_stage_policy(tmp_path):
    studio = Studio(tmp_path, settings=Settings(api_key="test"))
    project = studio.create_longform_topic("How a telescope works")
    project.live_models = {
        "director_model": "gpt-5.6-sol", "research_model": "gpt-5.6-sol",
        "tts_model": "gpt-4o-mini-tts", "voice": "coral",
    }
    project.longform_semantic_decisions["ch1"] = SemanticDecisionBatch(
        schema_version=2, chapter_id="ch1", decisions=[]
    )
    project.longform_semantic_hashes["ch1"] = "saved-paid-decision"
    studio.repo.save(project, "test_legacy_checkpoint", {})

    updated = studio.adopt_longform_stage_models(project.id)
    reloaded = studio.repo.load(project.id)
    assert reloaded.live_models["semantic_layout_model"] == "gpt-6-luna"
    assert reloaded.live_models["story_model"] == "gpt-6.1-sol"
    assert reloaded.longform_semantic_models == {"ch1": "gpt-5.6-sol"}
    assert reloaded.longform_semantic_hashes["ch1"] == "saved-paid-decision"
    assert reloaded.longform_semantic_decisions == updated.longform_semantic_decisions
    assert studio.repo.events(project.id)[-1]["kind"] == "longform_stage_models_adopted"
