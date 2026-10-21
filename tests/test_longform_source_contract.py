from autonomous_studio.domain.models import VisualBeat
from autonomous_studio.domain.motion import ShotBase
from autonomous_studio.providers.longform_visuals import VisualBeatDraft, VisualBeatSelection
from autonomous_studio.rendering.semantic_diagrams import (
    ChronologyScene,
    DocumentFocusScene,
    GeoPlace,
    GeoRegion,
    QuantitativeDisplayScene,
    RelationshipScene,
)
from autonomous_studio.research.provenance import source_ids_for_spoken_blocks


def test_longform_visual_and_motion_contracts_have_no_copied_source_ids():
    for model in (VisualBeatDraft, VisualBeatSelection, VisualBeat, ShotBase):
        assert "source_ids" not in model.model_fields
        assert "source_ids" not in model.model_json_schema()["properties"]
    for model in (
        ChronologyScene,
        GeoPlace,
        GeoRegion,
        QuantitativeDisplayScene,
        DocumentFocusScene,
        RelationshipScene,
    ):
        assert "source_ref" not in model.model_fields


def test_legacy_approved_beat_loads_without_source_tag():
    beat = VisualBeat.model_validate({
        "id": "beat-3", "chapter_id": "chapter-1", "start": 0,
        "duration": 6, "visual_type": "generated_image",
        "narration_block_ids": ["n1"], "claim_ids": ["c1"],
        "source_ids": ["s1"],
    })
    assert beat.claim_ids == ["c1"]
    assert "source_ids" not in beat.model_dump()


def test_sources_can_be_derived_from_approved_spoken_claims():
    research = {"claims": [
        {"id": "c1", "source_ids": ["s3"]},
        {"id": "c2", "source_ids": ["s4", "s3"]},
    ]}
    chapter = {"narration_blocks": [
        {"id": "n1", "claim_ids": ["c1", "c2"]},
        {"id": "n2", "claim_ids": []},
    ]}
    assert source_ids_for_spoken_blocks(research, chapter, ["n1"]) == ["s3", "s4"]
    assert source_ids_for_spoken_blocks(research, chapter, ["n2"]) == []
