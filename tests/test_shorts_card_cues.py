from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from pydantic import TypeAdapter

from autonomous_studio.domain.models import Presentation, Shot, VoiceSegment
from autonomous_studio.rendering.editorial import face, scene, text_box
from autonomous_studio.rendering.object_motion import cue_onsets, reveal


@pytest.mark.parametrize("value,centered", [
    ("One short label", True),
    ("A longer explanation that requires several lines inside this card", False),
])
def test_card_centers_only_single_line_after_wrapping(value, centered):
    canvas = Image.new("RGBA", (300, 200))
    records = []
    text_box(ImageDraw.Draw(canvas), value, (10, 20, 290, 180), 31, "white",
             center_single_line=True, records=records)
    record = records[0]
    bounds = canvas.getbbox()
    if centered:
        assert record["lines"] == 1
        assert abs((bounds[1] + bounds[3]) / 2 - 100) <= 1
    else:
        assert record["lines"] > 1
        assert bounds[1] == 20


def test_study_items_enter_at_estimated_spoken_phrases_not_fixed_schedule():
    shot = TypeAdapter(Shot).validate_python({
        "id": "study", "kind": "study_design", "start": 0, "duration": 10,
        "title": "Estimating the unseen", "motion": "stagger",
        "steps": ["Method: models", "Depend on assumptions", "Not a complete census"],
        "entrance_cues": ["used models", "depend on assumptions", "rather than a complete census"],
        "voice_segments": [VoiceSegment(
            shot_id="study", offset=.2, estimated_duration=8.45,
            spoken_text="To estimate that larger total, the researchers used models, which depend "
                        "on assumptions rather than a complete census.",
        )],
    })
    onsets = cue_onsets(SimpleNamespace(prepared_voices=[]), shot)
    assert onsets == pytest.approx([.2 + 8.45 * n / 18 for n in (7, 10, 13)])
    assert reveal(3, 10, 2, 3, motion="stagger", onset=onsets[2]) == 0
    assert reveal(7, 10, 2, 3, motion="stagger", onset=onsets[2]) == 1
    # Historical plans without cues still use the existing schedule.
    shot.entrance_cues = []
    assert cue_onsets(SimpleNamespace(prepared_voices=[]), shot) == []


def test_metric_pair_uses_centering_only_for_single_line_values():
    shot = TypeAdapter(Shot).validate_python({
        "id": "metrics", "kind": "metric_pair", "start": 0, "duration": 9,
        "title": "The human impact", "motion": "hold",
        "metrics": ["4 deaths", "112,800 people monitored for possible contamination"],
    })
    brief = SimpleNamespace(
        width=540, height=960, duration=9,
        visual_style=SimpleNamespace(
            background="#101923", foreground="#f0efe8", accent="#eab878"
        ),
    )
    project = SimpleNamespace(
        prepared_voices=[],
        spec=SimpleNamespace(brief=brief, shots=[shot], presentation=Presentation(),
                             closed_captions=False),
    )
    records = []
    scene(project, shot, .9, records=records)
    single, wrapped = [r for r in records if r["text"] in shot.metrics]
    assert single["lines"] == 1
    bounds = ImageDraw.Draw(Image.new("RGBA", (540, 960))).textbbox(
        (0, 0), single["text"], font=face(single["font_size"], True), anchor="lt"
    )
    assert single["text_y"] + (bounds[3] - bounds[1]) / 2 == pytest.approx(
        (single["box"][1] + single["box"][3]) / 2
    )
    assert wrapped["lines"] > 1
    assert wrapped["text_y"] == wrapped["box"][1]
    assert all(r["contrast_estimate"] >= 4.5 for r in (single, wrapped))

