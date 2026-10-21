"""The isolated design studies must be reusable and safe to review."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tools.render_point9_treatment_studies import RENDERERS, STUDIES, H, W, render_study


def test_every_candidate_has_sun_and_unrelated_example():
    assert len(RENDERERS) == 5
    assert len(STUDIES) == 10
    for treatment in RENDERERS:
        examples = [item for item in STUDIES if item.treatment == treatment]
        assert len(examples) == 2
        assert {item.topic == "sun" for item in examples} == {True, False}
        assert all(item.speech and item.roles and item.caveat for item in examples)


@pytest.mark.parametrize("study", STUDIES, ids=lambda item: item.stem)
def test_each_study_renders_through_shared_treatment(study):
    early = render_study(study, 0.0)
    late = render_study(study, 6.3)
    assert early.size == late.size == (W, H)
    assert early.mode == late.mode == "RGB"
    assert early.tobytes() != late.tobytes()


def test_treatment_is_not_tied_to_topic_name():
    for treatment in RENDERERS:
        study = next(
            item for item in STUDIES if item.treatment == treatment and item.topic != "sun"
        )
        renamed = replace(study, topic="a-new-topic")
        assert render_study(study, 6.3).tobytes() == render_study(renamed, 6.3).tobytes()


def test_unknown_candidate_fails_without_production_fallback():
    with pytest.raises(ValueError, match="unknown candidate treatment"):
        render_study(replace(STUDIES[0], treatment="unregistered"), 3.0)
