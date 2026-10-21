"""Reveal timing is advisory; incorrect speech anchors remain invalid."""

from types import SimpleNamespace

import pytest

from autonomous_studio.audio.alignment import estimated_words, phrase_start
from autonomous_studio.domain.models import LongFormChapter, NarrationBlock, VisualBeat
from autonomous_studio.rendering.visual_registry import planner_catalog, role_capacity
from autonomous_studio.workflow.spoken_units import chapter_units
from autonomous_studio.workflow.visual_reveal_timing import (
    validate_estimated_reveal_tails,
    validate_measured_reveal_tails,
    validate_saved_speech_against_visual_plan,
)

SCRIPT = (
    "The new elements mattered, but the interpretation went beyond adding elements. "
    "The scientist argued that the effect came from atoms themselves, not their arrangement. "
    "That insight changed what later measurements could reveal about matter."
)


@pytest.mark.parametrize("treatment_id", ["convergence", "mosaic"])
def test_synthesis_catalog_matches_renderer_role_capacity(treatment_id):
    assert role_capacity("synthesis", treatment_id) == (4, 5)
    synthesis = next(item for item in planner_catalog() if item["family"] == "synthesis")
    treatment = next(item for item in synthesis["treatments"] if item["id"] == treatment_id)
    assert treatment["min_roles"] == 4


def _chapter():
    chapter = LongFormChapter(
        id="c1",
        order=1,
        title="A new interpretation",
        purpose="Explain the discovery",
        question="What changed?",
        reveal="An atomic interpretation",
        target_duration=25,
        narration_blocks=[NarrationBlock(id="b1", text=SCRIPT, estimated_duration=21)],
    )
    ids = [item["id"] for item in chapter_units(chapter)]
    beat = VisualBeat(
        id="v1",
        chapter_id="c1",
        start=0,
        duration=25,
        narration_block_ids=["b1"],
        spoken_unit_ids=ids,
        role_unit_groups=[ids[:1], ids[1:]],
        visual_type="comparison",
        design_family="concept_contrast",
        design_treatment="juxtaposition",
        overlay_text=["Elements", "Atomic property"],
        overlay_anchors=["adding elements", "came from atoms themselves"],
    )
    chapter.visual_beats = [beat]
    return chapter, beat


def test_script_only_planner_warns_for_early_last_reveal(caplog):
    chapter, beat = _chapter()
    validate_estimated_reveal_tails(chapter, [beat], 24)
    assert "v1 estimated reveal gap ending" in caplog.text
    beat.overlay_anchors[-1] = "what later measurements could reveal about matter"
    validate_estimated_reveal_tails(chapter, [beat], 24)
    assert "v1 estimated reveal gap middle" in caplog.text
    beat.overlay_anchors[0] = "came from atoms themselves"
    validate_estimated_reveal_tails(chapter, [beat], 24)


def test_measured_preflight_warns_before_semantic_provider_call(caplog):
    chapter, beat = _chapter()
    words = estimated_words(SCRIPT, 21)
    windows = [{"block_id": "b1", "start": 0.1, "end": 21.1, "words": words}]
    plan = SimpleNamespace(shots=[SimpleNamespace(id="v1", kind="fact_reveal", end=25)])
    validate_measured_reveal_tails(chapter.model_dump(mode="json"), plan, windows)
    assert "v1 measured reveal gap ending" in caplog.text
    beat.overlay_anchors[-1] = "what later measurements could reveal about matter"
    validate_measured_reveal_tails(chapter.model_dump(mode="json"), plan, windows)
    assert "v1 measured reveal gap middle" in caplog.text
    beat.overlay_anchors[0] = "came from atoms themselves"
    validate_measured_reveal_tails(chapter.model_dump(mode="json"), plan, windows)


def test_planning_proxy_warns_on_borderline_anchor(caplog):
    chapter, beat = _chapter()
    words = estimated_words(SCRIPT, 21)
    windows = [{"block_id": "b1", "start": 0.1, "end": 21.1, "words": words}]
    last = phrase_start(SCRIPT, beat.overlay_anchors[-1], words, offset=0.1)
    plan = SimpleNamespace(shots=[SimpleNamespace(id="v1", kind="fact_reveal", end=last + 8.6)])
    validate_measured_reveal_tails(chapter.model_dump(mode="json"), plan, windows)
    assert "v1 measured reveal gap ending" in caplog.text
    validate_measured_reveal_tails(
        chapter.model_dump(mode="json"), plan, windows, planning_allowance_seconds=0.5
    )


def test_existing_tts_preflights_revised_visual_plan_before_approval(monkeypatch):
    from autonomous_studio.workflow import motion_planning

    chapter, _ = _chapter()
    block = chapter.narration_blocks[0]
    words = estimated_words(SCRIPT, 21)
    project = SimpleNamespace(
        longform_voices={"b1": SimpleNamespace(duration=21)},
        longform_word_timings={"b1": words},
        model_copy=lambda **_: project,
        model_dump=lambda **_: {"spec": {}, "longform_media": {}},
    )
    spec = SimpleNamespace(
        chapters=[chapter], brief=SimpleNamespace(width=1920, height=1080, fps=24)
    )
    plan = SimpleNamespace(assets=[SimpleNamespace(id="a1")])
    monkeypatch.setattr(
        motion_planning,
        "plan_motion_chapter",
        lambda *_args, **_kwargs: (
            SimpleNamespace(shots=[SimpleNamespace(id="v1", kind="fact_reveal", end=25)]),
            {},
        ),
    )
    from autonomous_studio.rendering import longform

    monkeypatch.setattr(longform, "narration_windows", lambda *_args: [(block, None, 0.1, 21.1)])
    validate_saved_speech_against_visual_plan(project, spec, plan)


def test_gap_warning_does_not_disable_spoken_anchor_validation():
    chapter, beat = _chapter()
    beat.overlay_anchors = ["came from atoms themselves", "adding elements"]
    with pytest.raises(ValueError, match="planned reveals must follow speech order"):
        validate_estimated_reveal_tails(chapter, [beat], 24)
