"""A targeted visual repair must preserve approved speech and own every spoken unit."""

from autonomous_studio.audio.alignment import estimated_words
from autonomous_studio.domain.models import (
    Artifact,
    CodeRenderedCandidate,
    DurationPolicy,
    LongFormBrief,
    LongFormChapter,
    LongFormCostEstimate,
    LongFormRightsPlan,
    LongFormSpec,
    LongFormVisualPlan,
    LongFormVisualPolicy,
    NarrationBlock,
    Provenance,
    QualitativeClaim,
    ResearchBrief,
    Source,
    State,
    VideoProject,
    VisualBeat,
    VoiceAsset,
)
from autonomous_studio.workflow.longform_beat_repair import (
    BeatRetarget,
    BeatSplit,
    prepare_retarget,
    prepare_split,
)
from autonomous_studio.workflow.spoken_units import chapter_units, validate_unit_coverage

SPEECH = (
    "Early battery recycling was similarly mixed. Researchers were recovering useful "
    "materials, while uncertain handling, commercial exaggeration and unsafe disposal "
    "encouraged poorly supported claims. It was neither uniformly beneficial nor simply "
    "wasteful: genuine recovery and dangerous shortcuts existed together."
)


def _artifact(role):
    return Artifact(
        id=f"fixture-{role}",
        path=f"{role}.bin",
        role=role,
        version=1,
        sha256="a" * 64,
        provider="fixture",
        model="fixture",
        input_reference="fixture",
        created_at="2026-01-01T00:00:00Z",
        provenance=Provenance(
            source="fixture",
            provider="fixture",
            asset_id=role,
            license_category="procedural-original",
            usage_restrictions="",
            origin="original",
        ),
    )


def _project():
    block = NarrationBlock(id="b1", text=SPEECH, estimated_duration=22)
    chapter = LongFormChapter(
        id="ch1",
        order=1,
        title="Recycling",
        purpose="Explain the tradeoff",
        question="What changed?",
        reveal="A mixed record",
        target_duration=24,
        narration_blocks=[block],
        evidence_claim_ids=["claim1"],
        evidence_source_ids=["source1"],
    )
    units = [item["id"] for item in chapter_units(chapter)]
    chapter.visual_beats = [
        VisualBeat(
            id="beat1",
            chapter_id="ch1",
            start=0,
            duration=24,
            narration_block_ids=["b1"],
            spoken_unit_ids=units,
            role_unit_groups=[units[:2], units[2:]],
            visual_type="comparison",
            design_family="concept_contrast",
            design_treatment="juxtaposition",
            design_rationale="Contrast real recovery with unsafe shortcuts.",
            purpose="Explain mixed recycling claims",
            screen_title="Recovery and risk",
            asset_ref="asset1",
            overlay_text=["Useful recovery", "Unsafe shortcuts"],
            narration_anchor="Early battery recycling was similarly mixed",
            overlay_anchors=[
                "Researchers were recovering useful materials",
                "genuine recovery and dangerous shortcuts existed together",
            ],
            claim_ids=["claim1"],
            source_ids=["source1"],
        )
    ]
    later = [
        LongFormChapter(
            id=f"ch{number}",
            order=number,
            title=f"Chapter {number}",
            purpose="Continue the story",
            question="What next?",
            reveal="More context",
            target_duration=63,
            narration_blocks=[
                NarrationBlock(id=f"b{number}", text="The story continues.", estimated_duration=3)
            ],
        )
        for number in (2, 3)
    ]
    brief = LongFormBrief(
        title="Battery recycling",
        topic="Battery recycling",
        editorial_objective="Explain a mixed historical record.",
        viewer_promise="Understand the tradeoff behind a familiar practice.",
        central_question="How did useful recovery coexist with unsafe shortcuts?",
        research=ResearchBrief(
            sources=[
                Source(
                    id="source1",
                    title="Fixture study",
                    url="https://example.org/study",
                    accessed="2026-01-01",
                    evidence_notes="Fixture evidence for mixed outcomes.",
                )
            ],
            claims=[
                QualitativeClaim(
                    id="claim1",
                    text="Useful recovery and unsafe practices coexisted.",
                    source_ids=["source1"],
                    confidence=0.9,
                    status="supported",
                )
            ],
        ),
        duration_policy=DurationPolicy(
            minimum_seconds=150,
            target_seconds=150,
            maximum_seconds=150,
            expansion_maximum_seconds=180,
        ),
    )
    return VideoProject(
        id="123456abcdef",
        state=State.LONGFORM_SEMANTIC_PLANNING,
        pipeline="live",
        spec=LongFormSpec(brief=brief, chapters=[chapter, *later]),
        longform_visual_plan=LongFormVisualPlan(
            style_direction="Clear documentary graphics with restrained color.",
            rationale="Use paired ideas to explain two sides of the same practice.",
            assets=[CodeRenderedCandidate(id="asset1", beat_ids=["beat1"], renderer="comparison")],
            cost=LongFormCostEstimate(
                generated_request_count=0,
                authentic_candidate_count=0,
                code_rendered_count=1,
            ),
            rights=LongFormRightsPlan(status="documented"),
        ),
        longform_visual_policy=LongFormVisualPolicy(),
        longform_media={"asset1": _artifact("visual")},
        longform_voices={
            "b1": VoiceAsset(
                artifact=_artifact("voice"),
                start=0,
                duration=22,
                spoken_text=SPEECH,
                characters=len(SPEECH),
                voice_id="fixture",
            )
        },
        longform_word_timings={"b1": estimated_words(SPEECH, 22)},
    )


def _retarget_project():
    project = _project()
    project.spec.chapters[0].target_duration = 18
    project.spec.chapters[0].visual_beats[0].duration = 18
    for chapter in project.spec.chapters[1:]:
        chapter.target_duration = 66
    project.longform_voices["b1"].duration = 17
    project.longform_word_timings["b1"] = estimated_words(SPEECH, 17)
    return project


def test_split_repairs_middle_gap_without_rewriting_speech_or_rebuying_asset():
    project = _project()
    before = project.spec.chapters[0].narration_blocks[0].text
    proposal = BeatSplit(
        original_id="beat1",
        new_id="beat1b",
        split_anchor="It was neither uniformly beneficial nor simply wasteful",
        first_overlay_count=2,
        first_purpose="Contrast useful recovery with unsafe shortcuts.",
        first_screen_title="Recovery and risk",
        first_design_rationale="The first two visual roles follow spoken research and risk.",
        first_overlay_text=["Useful recovery", "Unsafe handling and disposal"],
        first_overlay_anchors=[
            "Researchers were recovering useful materials",
            "uncertain handling",
        ],
        first_role_unit_groups=[["b1_u1", "b1_u2"], ["b1_u3", "b1_u4"]],
        new_visual_type="kinetic_title",
        new_design_family="editorial_frame",
        new_design_treatment="statement_reveal",
        new_purpose="Explain the qualified conclusion.",
        new_screen_title="Not a simple verdict",
        new_design_rationale="Two statements follow the concluding spoken ideas.",
        new_narration_anchor="It was neither uniformly beneficial nor simply wasteful",
        new_overlay_text=["Neither wholly good nor wholly wasteful", "Both sides coexisted"],
        new_overlay_anchors=[
            "It was neither uniformly beneficial nor simply wasteful",
            "genuine recovery and dangerous shortcuts existed together",
        ],
        new_role_unit_groups=[["b1_u5"], ["b1_u6"]],
        new_duration_fraction=0.39,
    )

    repaired, report = prepare_split(project, proposal)
    beats = repaired.spec.chapters[0].visual_beats
    assert [beat.id for beat in beats] == ["beat1", "beat1b"]
    validate_unit_coverage(repaired.spec.chapters[0], beats)
    assert repaired.spec.chapters[0].narration_blocks[0].text == before
    assert project.spec.chapters[0].visual_beats[0].spoken_unit_ids == [
        item["id"] for item in chapter_units(project.spec.chapters[0])
    ]
    assert repaired.longform_media == project.longform_media
    assert repaired.longform_visual_plan.assets[0].beat_ids == ["beat1", "beat1b"]
    assert report["measured_shots"]["beat1"]["end"] == report["measured_shots"]["beat1b"]["start"]


def test_retarget_reveals_spoken_ideas_instead_of_leaving_long_middle_gap():
    project = _retarget_project()
    proposal = BeatRetarget(
        beat_id="beat1",
        new_visual_type="kinetic_title",
        new_design_family="editorial_frame",
        new_design_treatment="statement_reveal",
        new_purpose="Explain the mixed history in three spoken steps.",
        new_screen_title="A mixed record",
        new_design_rationale="Three claims arrive in speech order.",
        new_overlay_text=["Mixed record", "Unsafe handling", "Neither wholly good nor bad"],
        new_overlay_anchors=[
            "Early battery recycling",
            "uncertain handling",
            "It was neither uniformly beneficial",
        ],
        new_role_unit_groups=[
            ["b1_u1", "b1_u2"],
            ["b1_u3", "b1_u4"],
            ["b1_u5", "b1_u6"],
        ],
    )
    repaired, report = prepare_retarget(project, proposal)
    beat = repaired.spec.chapters[0].visual_beats[0]
    assert beat.design_family == "editorial_frame"
    assert len(report["measured_entrances"]) == 3
    assert repaired.spec.chapters[0].narration_blocks[0].text == SPEECH
    assert repaired.longform_media == project.longform_media


def test_retarget_supports_multi_role_synthesis_without_replanning_script():
    project = _retarget_project()
    proposal = BeatRetarget(
        beat_id="beat1",
        new_visual_type="kinetic_title",
        new_design_family="synthesis",
        new_design_treatment="mosaic",
        new_purpose="Bring several threads into one takeaway.",
        new_screen_title="A mixed record",
        new_design_rationale="Four spoken inputs precede a qualified conclusion.",
        new_overlay_text=[
            "Recycling record",
            "Useful recovery",
            "Uncertain handling",
            "Unsafe disposal",
            "A mixed outcome",
        ],
        new_overlay_anchors=[
            "Early battery recycling",
            "Researchers were recovering useful materials",
            "uncertain handling",
            "commercial exaggeration",
            "It was neither uniformly beneficial",
        ],
        new_role_unit_groups=[
            ["b1_u1"],
            ["b1_u2"],
            ["b1_u3"],
            ["b1_u4"],
            ["b1_u5", "b1_u6"],
        ],
    )
    repaired, report = prepare_retarget(project, proposal)
    assert repaired.spec.chapters[0].visual_beats[0].design_family == "synthesis"
    assert len(report["measured_entrances"]) == 5
    assert repaired.longform_visual_plan == project.longform_visual_plan
