"""Topic-neutral image-shot presentation and backwards compatibility."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from autonomous_studio.domain.motion import CameraMove
from autonomous_studio.providers.longform_media import (
    acquire_longform_media,
    longform_image_payload,
)
from autonomous_studio.rendering.longform_motion import (
    _camera,
    _photo,
    render_motion_thumbnail,
)
from autonomous_studio.storage.repository import Repository, digest
from autonomous_studio.workflow.motion_planning import _present_image_shot


def _shot(**changes):
    shot = {
        "id": "image-beat",
        "kind": "camera",
        "intent": "establish",
        "start": 0.0,
        "end": 8.0,
        "viewer_takeaway": "A visible relationship explains the spoken line.",
        "why_this_template": "An image illustrates the approved narration.",
        "narration_block_ids": ["block-1"],
        "asset_ref": "image-1",
        "label": "A model predicts a change",
        "secondary_label": "The outcome is uncertain",
        "cues": [
            {"action": "start_move", "at_seconds": 0.0, "duration_seconds": 0.35,
             "narration_excerpt": "A model predicts"},
            {"action": "show_label", "at_seconds": 0.6, "duration_seconds": 0.4,
             "narration_excerpt": "A model predicts"},
            {"action": "show_secondary_label", "at_seconds": 2.0, "duration_seconds": 0.4,
             "narration_excerpt": "outcome is uncertain"},
        ],
    }
    shot.update(changes)
    return shot


def _image():
    image = Image.new("RGB", (640, 360))
    for x in range(640):
        for y in range(360):
            image.putpixel((x, y), (x % 256, y % 256, 80))
    return image


def test_old_saved_camera_plan_keeps_legacy_presentation():
    parsed = CameraMove.model_validate(_shot())
    assert parsed.motion_style == "legacy"
    assert parsed.label_effect == "legacy"


def test_image_presentation_is_deterministic_and_contain_does_not_crop():
    asset = {"direction": {"overlay_safe_region": "left"}}
    first = _shot()
    _present_image_shot(first, asset, None)
    again = _shot()
    _present_image_shot(again, asset, None)
    assert first == again
    assert first["label_effect"] in {"fade_rise", "wipe", "slide_plate", "gentle_pop"}
    assert first["motion_style"] in {"push_in", "pull_back", "hold_then_push", "hold"}
    archival = _shot(presentation="contain")
    _present_image_shot(archival, {"kind": "authentic_image"}, first)
    assert archival["motion_style"] == "hold"
    assert archival["zoom_start"] == archival["zoom_end"] == 1.0
    assert archival["label_effect"] == "slide_plate"


def test_new_image_plan_keeps_a_low_corner_subject_in_frame():
    source = Image.new("RGB", (640, 360), "navy")
    source.paste(Image.new("RGB", (75, 75), "green"), (540, 275))
    shot = _shot(
        crop_bottom=0.26, presentation="fill", label="", secondary_label="",
    )
    asset = {"direction": {
        "overlay_safe_region": "upper",
        "focal_placement": "Small and low in the right foreground",
    }}
    _present_image_shot(shot, asset, None)
    assert shot["crop_bottom"] == 0.0
    assert shot["motion_style"] == "hold"
    assert shot["zoom_start"] == shot["zoom_end"] == 1.0
    rendered = _camera(source, shot, 2.0, (640, 360))
    assert rendered.getpixel((580, 310)) == (0, 128, 0)


@pytest.mark.parametrize("effect", ["fade_rise", "wipe", "slide_plate", "gentle_pop"])
def test_every_image_text_entrance_uses_its_speech_cue(effect):
    image = _image()
    shot = _shot(label_effect=effect, text_zone="middle_left", motion_style="hold",
                 zoom_start=1.0, zoom_end=1.0)
    before = _photo(image, shot, 0.5, (960, 540))
    bare = _camera(image, shot, 0.5, (960, 540))
    assert before.tobytes() == bare.tobytes()
    after = _photo(image, shot, 1.2, (960, 540))
    assert after.tobytes() != bare.tobytes()
    assert _photo(image, shot, 2.6, (960, 540)).tobytes() != after.tobytes()


def test_hold_then_push_moves_only_after_the_hold():
    image = _image()
    shot = _shot(motion_style="hold_then_push", hold_fraction=0.4,
                 zoom_start=1.02, zoom_end=1.09,
                 focus_start=(0.5, 0.5), focus_end=(0.5, 0.5))
    assert _camera(image, shot, 0.0, (960, 540)).tobytes() == _camera(
        image, shot, 2.0, (960, 540)
    ).tobytes()
    assert _camera(image, shot, 7.9, (960, 540)).tobytes() != _camera(
        image, shot, 2.0, (960, 540)
    ).tobytes()


def test_busy_or_bright_image_gets_local_text_protection_only():
    source = Image.new("RGB", (640, 360), "white")
    shot = _shot(
        label_effect="fade_rise", text_zone="upper_left", secondary_label="",
        motion_style="hold", zoom_start=1.0, zoom_end=1.0,
    )
    frame = _photo(source, shot, 1.5, (960, 540))
    assert frame.getpixel((38, 42)) != (255, 255, 255)
    assert frame.getpixel((850, 400)) == (255, 255, 255)


def test_invalid_image_motion_contract_is_rejected():
    with pytest.raises(ValidationError, match="hold_fraction requires hold_then_push"):
        CameraMove.model_validate(_shot(hold_fraction=0.5))
    with pytest.raises(ValidationError, match="hold camera motion"):
        CameraMove.model_validate(_shot(motion_style="hold", zoom_end=1.1))
    with pytest.raises(ValidationError, match="camera focus"):
        CameraMove.model_validate(_shot(focus_end=(1.3, 0.5)))


def test_opening_promise_is_visible_before_spoken_labels_and_has_separate_thumbnail(tmp_path):
    source = tmp_path / "hero.png"
    clean_source = tmp_path / "hero.clean.png"
    Image.new("RGB", (640, 360), "#ad1010").save(source)
    Image.new("RGB", (640, 360), "#e8e8e8").save(clean_source)
    shot = CameraMove.model_validate(
        _shot(hook_headline="HOW DOES THIS STAR END?",
              thumbnail_headline="THE STAR'S FINAL ACT")
    )
    raw = shot.model_dump(mode="json")
    frame = _photo(_image(), raw, 0.0, (960, 540))
    assert frame.tobytes() != _camera(_image(), raw, 0.0, (960, 540)).tobytes()
    clean = SimpleNamespace(provenance=SimpleNamespace(asset_id="image-1"))
    repo = SimpleNamespace(
        verify=lambda _project, artifact: clean_source if artifact is clean else source
    )
    project = SimpleNamespace(
        longform_media={"image-1": object()},
        current_artifacts=lambda role: [clean] if role == "longform_visual_clean" else [],
    )
    result = tmp_path / "thumbnail.png"
    assert render_motion_thumbnail(repo, project, SimpleNamespace(shots=[shot]), result)
    with Image.open(result) as rendered:
        assert rendered.size == (1280, 720)
        # The publication thumbnail must not inherit the video-ready red plate.
        assert rendered.getpixel((1100, 650)) == (232, 232, 232)
    assert result.is_file()


def test_legacy_generated_thumbnail_recovers_verified_uncaptioned_provider_image(tmp_path):
    repo = Repository(tmp_path)
    project = SimpleNamespace(
        id="a" * 12, version=1, pipeline="live", creation_settings={},
        current_artifacts=lambda role: project.artifacts if role == "longform_image_prompt" else [],
        longform_visual_plan=SimpleNamespace(assets=[SimpleNamespace(
            id="image-1", kind="generated_image",
        )]),
        longform_media={}, artifacts=[],
    )
    root = repo.project_dir(project.id)
    images = root / "images" / "v001"
    images.mkdir(parents=True)
    labelled = images / "image-1.png"
    Image.new("RGB", (1920, 1080), "#ad1010").save(labelled)
    project.longform_media["image-1"] = repo.artifact(project, labelled, "longform_visual")
    payload = {"model": "gpt-image-2", "prompt": "No text", "size": "1536x1024"}
    prompt_path = labelled.with_suffix(".prompt.json")
    prompt_path.write_text(json.dumps(payload), encoding="utf-8")
    project.artifacts.append(repo.artifact(project, prompt_path, "longform_image_prompt"))
    key = hashlib.sha256(json.dumps(
        {"payload": payload, "fresh_media_epoch": None}, sort_keys=True,
    ).encode()).hexdigest()
    cache = root / "provider-cache" / "longform-images"
    cache.mkdir(parents=True)
    raw = cache / f"{key}.png"
    Image.new("RGB", (1536, 1024), "#e8e8e8").save(raw)
    (cache / f"{key}.json").write_text(
        json.dumps({"request": payload, "sha256": digest(raw)}), encoding="utf-8",
    )
    shot = CameraMove.model_validate(_shot(
        hook_headline="HOW DOES IT END?", thumbnail_headline="HOW DOES IT END?",
    ))
    result = tmp_path / "legacy-thumbnail.png"
    assert render_motion_thumbnail(repo, project, SimpleNamespace(shots=[shot]), result)
    with Image.open(result) as rendered:
        assert rendered.getpixel((1100, 650)) == (232, 232, 232)
    raw.unlink()
    with pytest.raises(ValueError, match="no verified clean thumbnail source"):
        render_motion_thumbnail(repo, project, SimpleNamespace(shots=[shot]), result)


def test_rebuilt_thumbnail_uses_verified_clean_art_from_reused_media_version(tmp_path):
    repo = Repository(tmp_path)
    project = SimpleNamespace(id="a" * 12, version=5, pipeline="live", artifacts=[])
    root = repo.project_dir(project.id)
    source = root / "images" / "v005" / "image-1.png"
    source.parent.mkdir(parents=True)
    clean_source = source.with_suffix(".clean.png")
    Image.new("RGB", (640, 360), "#ad1010").save(source)
    Image.new("RGB", (640, 360), "#e8e8e8").save(clean_source)
    media = repo.artifact(project, source, "longform_visual")
    clean = repo.artifact(project, clean_source, "longform_visual_clean")
    clean.provenance.asset_id = "image-1"
    project.artifacts.append(clean)
    project.longform_media = {"image-1": media}
    project.version = 6  # Only the presentation was rebuilt, not the approved media.
    project.current_artifacts = lambda role: [
        item for item in project.artifacts if item.role == role and item.version == project.version
    ]
    shot = CameraMove.model_validate(_shot(
        hook_headline="HOW DOES IT END?", thumbnail_headline="HOW DOES IT END?",
    ))
    output = root / "rebuilt-thumbnail.png"
    assert render_motion_thumbnail(repo, project, SimpleNamespace(shots=[shot]), output)
    with Image.open(output) as rendered:
        assert rendered.getpixel((1100, 650)) == (232, 232, 232)
    Image.new("RGB", (640, 360), "#000000").save(clean_source)
    with pytest.raises(ValueError, match="missing or modified artifact"):
        render_motion_thumbnail(repo, project, SimpleNamespace(shots=[shot]), output)


def test_acquisition_preserves_clean_art_and_video_caption_separately(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    monkeypatch.setattr(repo, "save", lambda *_args, **_kwargs: None)
    beat = SimpleNamespace(id="beat-1", overlay_text=["Revealed later"])
    asset = SimpleNamespace(
        id="image-1", kind="generated_image", beat_ids=["beat-1"],
        model="gpt-image-2",
    )
    project = SimpleNamespace(
        id="a" * 12, version=1, pipeline="live", creation_settings={},
        approvals=[SimpleNamespace(gate="visual_plan", version=1, decision="approve")],
        spec=SimpleNamespace(chapters=[SimpleNamespace(visual_beats=[beat])]),
        longform_visual_plan=SimpleNamespace(
            rights=SimpleNamespace(status="clear"), assets=[asset],
        ),
        longform_visual_policy=SimpleNamespace(maximum_generated_requests=1),
        longform_media={}, artifacts=[],
    )

    async def fake_image(_studio, _project, _asset, output):
        Image.new("RGB", (1920, 1080), "#e8e8e8").save(output)
        output.with_suffix(".prompt.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr("autonomous_studio.providers.longform_media._openai_image", fake_image)
    # The stubbed provider has no suspended I/O; exercise the coroutine without
    # creating a Windows socket-backed event loop in restricted test runners.
    operation = acquire_longform_media(SimpleNamespace(repo=repo), project)
    with pytest.raises(StopIteration):
        operation.send(None)
    clean = next(a for a in project.artifacts if a.role == "longform_visual_clean")
    with Image.open(repo.verify(project, clean)) as source:
        assert source.getpixel((100, 900)) == (232, 232, 232)
    with Image.open(repo.verify(project, project.longform_media["image-1"])) as video_ready:
        assert video_ready.getpixel((100, 900)) != (232, 232, 232)


@pytest.mark.parametrize(
    "narration",
    [
        "A model predicts that the ocean may rise around the city.",
        "The camera recorded the shoreline retreating in 2024.",
        "Researchers disagree on which mechanism caused the change.",
        "Imagine a river of time flowing through these decisions.",
    ],
)
def test_image_prompt_carries_complete_approved_spoken_claim(narration):
    beat = SimpleNamespace(
        id="beat-a", chapter_id="chapter-a", spoken_unit_ids=[],
        narration_block_ids=["block-a"],
    )
    chapter = SimpleNamespace(
        id="chapter-a", visual_beats=[beat],
        narration_blocks=[SimpleNamespace(id="block-a", text=narration)],
    )
    direction = SimpleNamespace(
        overlay_safe_region="left", spoken_idea="the ocean may rise",
        focal_subject="the shoreline", visual_moment="An event in progress",
        setting="coastal scene", framing="wide shot", focal_placement="right side",
    )
    asset = SimpleNamespace(
        id="asset-a", beat_ids=["beat-a"], model="gpt-image-2",
        prompt="A precise documentary illustration", direction=direction,
    )
    project = SimpleNamespace(
        spec=SimpleNamespace(chapters=[chapter]),
        longform_creative_policy=object(),
        longform_visual_plan=SimpleNamespace(style_direction="Luminous science illustration"),
    )
    settings = SimpleNamespace(image_quality="high")
    payload = longform_image_payload(project, asset, settings)
    assert narration in payload["prompt"]
    assert "Preserve uncertainty" in payload["prompt"]
    assert payload["model"] == "gpt-image-2"
