import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image

from autonomous_studio.audio.alignment import estimated_words, phrase_start
from autonomous_studio.config import Settings
from autonomous_studio.domain.models import LongFormVisualPolicy
from autonomous_studio.providers.longform import MockLongFormDirector, MockLongFormResearch
from autonomous_studio.providers.longform_visuals import (
    MockLongFormVisualPlanner,
    validate_spoken_visual_anchors,
)
from autonomous_studio.providers.openai import OpenAIClient
from autonomous_studio.rendering.longform import _timed_caption_block
from autonomous_studio.rendering.longform_motion import frame_for_shot
from autonomous_studio.workflow.engine import load_brief
from autonomous_studio.workflow.motion_planning import _historical_simulation, plan_motion_chapter

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "longform-pilot.yaml"


def test_live_word_transcription_uses_timestamped_response_and_cache(tmp_path):
    class StubHTTP:
        calls = 0

        async def post(self, url, body, headers, **kwargs):
            self.calls += 1
            assert url.endswith("/audio/transcriptions")
            assert b"timestamp_granularities[]" in body
            assert b"whisper-1" in body
            assert headers["Content-Type"].startswith("multipart/form-data;")
            return json.dumps(
                {
                    "words": [
                        {"word": "Warsaw", "start": 0.1, "end": 0.6},
                        {"word": "Paris", "start": 0.7, "end": 1.1},
                    ]
                }
            ).encode()

    audio = tmp_path / "narration.wav"
    audio.write_bytes(b"RIFF" + b"fixture-wave-data")
    http = StubHTTP()
    client = OpenAIClient(Settings(api_key="fixture-key"), tmp_path / "cache", http=http)
    first = asyncio.run(client.transcribe_words(audio))
    second = asyncio.run(client.transcribe_words(audio))
    assert first == second
    assert http.calls == 1
    assert phrase_start("Warsaw Paris", "Paris", first) == 0.7


def test_live_word_transcription_keeps_zero_length_boundary_word(tmp_path):
    class StubHTTP:
        async def post(self, *args, **kwargs):
            return json.dumps(
                {
                    "words": [
                        {"word": "the", "start": 1.0, "end": 1.4},
                        {"word": "ore", "start": 1.4, "end": 1.4},
                        {"word": "glowed", "start": 1.43, "end": 1.9},
                    ]
                }
            ).encode()

    audio = tmp_path / "narration.wav"
    audio.write_bytes(b"RIFF" + b"fixture-wave-data")
    client = OpenAIClient(Settings(api_key="fixture-key"), tmp_path / "cache", http=StubHTTP())
    words = asyncio.run(client.transcribe_words(audio))
    assert words[1] == {"word": "ore", "start": 1.4, "end": 1.42}
    assert phrase_start("the ore glowed", "ore", words) == 1.4


def test_live_word_transcription_tolerates_overlapping_spans_and_small_onset_jitter(tmp_path):
    class StubHTTP:
        async def post(self, *args, **kwargs):
            return json.dumps(
                {
                    "words": [
                        {"word": "investigating", "start": 5.56, "end": 5.9},
                        {"word": "matter", "start": 5.5, "end": 6.28},
                    ]
                }
            ).encode()

    audio = tmp_path / "narration.wav"
    audio.write_bytes(b"RIFF" + b"fixture-wave-data")
    client = OpenAIClient(Settings(api_key="fixture-key"), tmp_path / "cache", http=StubHTTP())
    words = asyncio.run(client.transcribe_words(audio))
    assert words[1]["start"] == 5.56
    assert phrase_start("investigating matter", "matter", words) == 5.56


def test_approved_legacy_anchor_with_unique_truncated_last_word_can_align():
    speech = "The measurements informed particular decisions."
    words = estimated_words(speech, 4.0)
    assert phrase_start(speech, "informed particular dec", words) == words[2]["start"]
    with pytest.raises(ValueError, match="absent from approved narration"):
        phrase_start(speech, "informed particular nonexistent", words)


def test_new_visual_plan_rejects_truncated_final_anchor_token():
    spec = _scripted()
    draft = asyncio.run(MockLongFormVisualPlanner().plan(spec, LongFormVisualPolicy(), []))
    beat = next(
        item for item in draft.beats[1:]
        if len(item.narration_anchor.split()[-1]) > 5
    )
    words = beat.narration_anchor.split()
    beat.narration_anchor = " ".join([*words[:-1], words[-1][:-2]])
    with pytest.raises(ValueError, match="anchor absent from approved narration"):
        validate_spoken_visual_anchors(spec, draft)


def _scripted():
    brief = load_brief(EXAMPLE)
    research = MockLongFormResearch()
    director = MockLongFormDirector()
    plan = asyncio.run(research.plan(brief))
    treatment = asyncio.run(director.develop_treatment(brief, plan, []))
    return asyncio.run(director.write_script(treatment, []))[0]


def test_live_visual_contract_rejects_unspoken_numbers_and_broken_hook():
    spec = _scripted()
    draft = asyncio.run(MockLongFormVisualPlanner().plan(spec, LongFormVisualPolicy(), []))
    validate_spoken_visual_anchors(spec, draft)
    broken = draft.model_copy(deep=True)
    broken.beats[0].narration_anchor = "An unrelated opening"
    with pytest.raises(ValueError, match="spoken hook"):
        validate_spoken_visual_anchors(spec, broken)
    broken = draft.model_copy(deep=True)
    code = next(beat for beat in broken.beats if beat.visual_type == "comparison")
    code.overlay_text = ["18–20"]
    with pytest.raises(ValueError, match="unspoken number"):
        validate_spoken_visual_anchors(spec, broken)
    broken = draft.model_copy(deep=True)
    code = next(beat for beat in broken.beats if beat.visual_type == "comparison")
    code.screen_title = "KEY IDEA"
    with pytest.raises(ValueError, match="story-specific screen title"):
        validate_spoken_visual_anchors(spec, broken)


def test_measured_words_control_image_labels_not_fixed_scene_fractions():
    speech = "In 1891, she moved from Warsaw to Paris."
    words = [
        {"word": word, "start": time, "end": time + 0.3}
        for word, time in zip(
            ("In", "1891", "she", "moved", "from", "Warsaw", "to", "Paris"),
            (0.1, 0.4, 1.0, 1.4, 1.8, 2.4, 2.8, 3.2),
            strict=True,
        )
    ]
    project = {
        "id": "phrase-test",
        "spec": {
            "chapters": [
                {
                    "id": "ch1",
                    "target_duration": 6.0,
                    "narration_blocks": [{"id": "b1", "text": speech}],
                    "evidence_claim_ids": ["c1"],
                    "evidence_source_ids": ["s1"],
                    "visual_beats": [
                        {
                            "id": "v1",
                            "start": 0,
                            "duration": 6,
                            "narration_block_ids": ["b1"],
                            "visual_type": "generated_image",
                            "purpose": "Locate the historical journey in the narration.",
                            "asset_ref": "city",
                            "overlay_text": ["1891", "Warsaw to Paris"],
                            "narration_anchor": "In 1891",
                            "overlay_anchors": ["1891", "Warsaw to Paris"],
                            "claim_ids": ["c1"],
                            "source_ids": ["s1"],
                        }
                    ],
                }
            ]
        },
        "longform_visual_plan": {
            "assets": [{"id": "city", "kind": "generated_image", "prompt": "A city street"}]
        },
        "longform_media": {"city": {"path": "city.png"}},
    }
    window = [{"block_id": "b1", "start": 0.1, "end": 3.6, "words": words}]
    plan, _ = plan_motion_chapter(project, "ch1", window)
    shot = plan.shots[0]
    assert shot.kind == "camera"
    assert shot.label == "1891"
    assert shot.secondary_label == "Warsaw to Paris"
    assert abs(shot.cues[1].at_seconds - 0.5) < 0.01
    assert abs(shot.cues[2].at_seconds - 2.5) < 0.01
    assert phrase_start(speech, "Warsaw to Paris", words) == 2.4
    source = Image.new("RGB", (640, 360), "#314b62")
    before = frame_for_shot(shot.model_dump(mode="json"), 0.3, (640, 360), {"city": source})
    after = frame_for_shot(shot.model_dump(mode="json"), 3.0, (640, 360), {"city": source})
    assert before.tobytes() != after.tobytes()


def test_family_first_scene_waits_for_next_block_after_narration_pause():
    project = {
        "id": "pause-test",
        "spec": {"chapters": [{
            "id": "ch1", "target_duration": 18.0,
            "narration_blocks": [
                {"id": "b1", "text": "First idea is measured."},
                {"id": "b2", "text": "Second idea becomes visible."},
            ],
            "evidence_claim_ids": ["c1"], "evidence_source_ids": ["s1"],
            "visual_beats": [
                {
                    "id": f"v{index}", "start": (index - 1) * 9.0,
                    "duration": 9.0, "narration_block_ids": [f"b{index}"],
                    "visual_type": "kinetic_title", "purpose": "Explain the spoken idea.",
                    "asset_ref": f"a{index}", "overlay_text": [anchor],
                    "narration_anchor": anchor, "overlay_anchors": [anchor],
                    "claim_ids": ["c1"], "source_ids": ["s1"],
                }
                for index, anchor in ((1, "First idea"), (2, "Second idea"))
            ],
        }]},
        "longform_visual_plan": {"assets": [
            {"id": "a1", "kind": "code_rendered"},
            {"id": "a2", "kind": "code_rendered"},
        ]},
        "longform_media": {"a1": {}, "a2": {}},
    }
    windows = [
        {"block_id": "b1", "start": 0.1, "end": 5.0,
         "words": estimated_words("First idea is measured.", 4.9)},
        {"block_id": "b2", "start": 10.0, "end": 15.0,
         "words": estimated_words("Second idea becomes visible.", 5.0)},
    ]
    plan, _ = plan_motion_chapter(project, "ch1", windows, family_first=True)
    assert plan.shots[0].end == 10.0
    assert plan.shots[1].start == 10.0
    assert plan.shots[1].cues[0].at_seconds >= 10.0


def test_spoken_recap_uses_branded_close_and_supplied_logo():
    speech = (
        "Measurement and collaboration changed the field. Which part changed your view? "
        "Subscribe for more histories. Like and share this story."
    )
    words = [
        {"word": word, "start": index * 0.3, "end": (index + 1) * 0.3}
        for index, word in enumerate(speech.split())
    ]
    project = {
        "id": "outro-test",
        "spec": {
            "chapters": [
                {
                    "id": "ch1",
                    "target_duration": 9.0,
                    "narration_blocks": [{"id": "b1", "text": speech}],
                    "evidence_claim_ids": ["c1"],
                    "evidence_source_ids": ["s1"],
                    "visual_beats": [
                        {
                            "id": "end",
                            "start": 0,
                            "duration": 9,
                            "narration_block_ids": ["b1"],
                            "visual_type": "outro",
                            "purpose": "Recap the contributions and invite a viewer response.",
                            "asset_ref": "card",
                            "overlay_text": ["Measurement", "collaboration"],
                            "narration_anchor": "Measurement",
                            "overlay_anchors": ["Measurement", "collaboration"],
                            "claim_ids": ["c1"],
                            "source_ids": ["s1"],
                        }
                    ],
                }
            ]
        },
        "longform_visual_plan": {"assets": [{"id": "card", "kind": "code_rendered"}]},
        "longform_media": {"card": {"path": "card.png"}},
    }
    plan, _ = plan_motion_chapter(
        project, "ch1", [{"block_id": "b1", "start": 0.1, "end": 6.0, "words": words}]
    )
    shot = plan.shots[0]
    assert shot.kind == "branded_outro"
    assert [cue.action for cue in shot.cues] == [
        "reveal_step",
        "reveal_step",
        "show_question",
        "show_label",
    ]
    frame = frame_for_shot(shot.model_dump(mode="json"), 6.0, (640, 360), {})
    assert frame.size == (640, 360)
    assert frame.getpixel((565, 115)) != (19, 29, 39)


def test_captions_follow_measured_phrase_onsets_not_uniform_block_time():
    text = (
        "Images locate foreign objects and assess fractures—information that could guide treatment."
    )
    spoken = text.replace("—", "— ").split()
    # A deliberate pause occurs before "information". Equal-duration chunks
    # would display it before the voice reaches that word.
    starts = [0.2, 0.5, 0.8, 1.1, 1.4, 1.7, 2.0, 4.2, 4.5, 4.8, 5.1, 5.4]
    words = [
        {"word": word, "start": at, "end": at + 0.22}
        for word, at in zip(spoken, starts, strict=True)
    ]
    cues = _timed_caption_block(text, words, 10.0, 16.0)
    assert len(cues) == 2
    assert cues[0][2].endswith("fractures—")
    assert cues[0][1] < 14.2
    assert cues[1][2].startswith("information")
    assert abs(cues[1][0] - 14.2) < 0.01


def test_code_sequence_preserves_three_spoken_items_in_order():
    speech = "She was born in Warsaw, moved to Paris in 1891, and studied at the Sorbonne."
    spoken = speech.split()
    words = [
        {"word": word, "start": i * 0.4, "end": i * 0.4 + 0.3} for i, word in enumerate(spoken)
    ]
    project = {
        "id": "sequence-test",
        "spec": {
            "chapters": [
                {
                    "id": "ch1",
                    "target_duration": 9.0,
                    "narration_blocks": [{"id": "b1", "text": speech}],
                    "evidence_claim_ids": ["c1"],
                    "evidence_source_ids": ["s1"],
                    "visual_beats": [
                        {
                            "id": "v1",
                            "start": 0,
                            "duration": 9,
                            "narration_block_ids": ["b1"],
                            "visual_type": "timeline",
                            "purpose": "Show an education journey in spoken order.",
                            "asset_ref": "graphic",
                            "overlay_text": ["Warsaw", "Paris · 1891", "Sorbonne"],
                            "narration_anchor": "She was born in Warsaw",
                            "overlay_anchors": [
                                "born in Warsaw",
                                "Paris in 1891",
                                "studied at the Sorbonne",
                            ],
                            "claim_ids": ["c1"],
                            "source_ids": ["s1"],
                        }
                    ],
                }
            ]
        },
        "longform_visual_plan": {"assets": [{"id": "graphic", "kind": "code_rendered"}]},
        "longform_media": {"graphic": {"path": "graphic.png"}},
    }
    plan, report = plan_motion_chapter(
        project, "ch1", [{"block_id": "b1", "start": 0.0, "end": 6.8, "words": words}]
    )
    shot = plan.shots[0]
    assert shot.kind == "narration_sequence"
    assert shot.entries == ["Warsaw", "Paris · 1891", "Sorbonne"]
    assert not report["decisions"][0]["labels_outside_scene"]
    before = frame_for_shot(shot.model_dump(mode="json"), 0, (640, 360), {})
    after = frame_for_shot(shot.model_dump(mode="json"), 6, (640, 360), {})
    assert before.tobytes() != after.tobytes()


def test_negative_prompt_does_not_turn_concept_image_into_fake_archive():
    assert not _historical_simulation(
        "A contemporary explanatory mineral illustration; avoid fake archival styling."
    )
    assert _historical_simulation(
        "An authentic historical photograph of a laboratory; avoid modern clothing."
    )
    assert not _historical_simulation(
        "Contemporary editorial illustration of a closed archival notebook inside a "
        "protective cradle; no readable handwriting or reproduced pages."
    )
    assert _historical_simulation(
        "Contemporary editorial illustration of a closed archival notebook with a "
        "recreated manuscript page; no modern clothing."
    )
    assert not _historical_simulation(
        "Contemporary editorial illustration of a museum conservation workspace "
        "with gloved hands, an archival storage box, a mineral sample and a precise "
        "muted-green measurement trace; no visible manuscript, historical handwriting, "
        "portrait or faux archival styling."
    )
    assert _historical_simulation(
        "Contemporary illustration of an archival-looking manuscript page; "
        "no modern clothing."
    )
