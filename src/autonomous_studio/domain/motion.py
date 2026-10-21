"""Story-aware, topic-independent motion contracts for long-form chapters."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from autonomous_studio.domain.models import LegacySourceTagModel, Model
from autonomous_studio.domain.semantic import SEMANTIC_RATIONALE_MAX_LENGTH

Intent = Literal[
    "hook",
    "establish",
    "locate",
    "compare",
    "explain_process",
    "examine_evidence",
    "foreshadow",
    "connect",
    "close",
]

Action = Literal[
    "show_line_1",
    "show_line_2",
    "show_question",
    "show_label",
    "draw_route",
    "highlight_detail",
    "reveal_left",
    "reveal_right",
    "reveal_step",
    "highlight_document",
    "start_move",
    "show_expected",
    "show_observed",
    "show_gap",
    "show_secondary_label",
]


class MotionCue(Model):
    at_seconds: float = Field(ge=0)
    narration_excerpt: str = Field(min_length=2, max_length=250)
    action: Action
    duration_seconds: float = Field(default=0.35, gt=0, le=20)
    step_index: int | None = Field(default=None, ge=0, le=7)


class ShotBase(LegacySourceTagModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    intent: Intent
    viewer_takeaway: str = Field(min_length=8, max_length=500)
    why_this_template: str = Field(min_length=12, max_length=SEMANTIC_RATIONALE_MAX_LENGTH)
    narration_block_ids: list[str] = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    cues: list[MotionCue] = Field(min_length=1)

    @model_validator(mode="after")
    def positive_duration(self):
        if self.end <= self.start:
            raise ValueError("motion shot must have a positive duration")
        return self


class TitleReveal(ShotBase):
    kind: Literal["title_reveal"]
    intent: Literal["hook"]
    eyebrow: str = Field(min_length=1, max_length=90)
    line_1: str = Field(min_length=1, max_length=90)
    line_2: str = Field(min_length=1, max_length=90)
    question: str = Field(min_length=1, max_length=120)


class CameraMove(ShotBase):
    kind: Literal["camera"]
    intent: Literal["establish", "foreshadow"]
    asset_ref: str
    presentation: Literal["fill", "contain"] = "fill"
    crop_bottom: float = Field(default=0, ge=0, le=0.45)
    zoom_start: float = Field(default=1, ge=1, le=2)
    zoom_end: float = Field(default=1.12, ge=1, le=2)
    focus_start: tuple[float, float] = (0.5, 0.5)
    focus_end: tuple[float, float] = (0.5, 0.5)
    label: str = Field(default="", max_length=80)
    secondary_label: str = Field(default="", max_length=100)
    hook_headline: str = Field(default="", max_length=72)
    thumbnail_headline: str = Field(default="", max_length=72)
    # Legacy defaults preserve saved approved plans when the renderer is upgraded.
    motion_style: Literal[
        "legacy", "push_in", "pull_back", "drift", "hold_then_push", "hold"
    ] = "legacy"
    hold_fraction: float = Field(default=0.0, ge=0, le=0.8)
    label_effect: Literal[
        "legacy", "fade_rise", "wipe", "slide_plate", "gentle_pop"
    ] = "legacy"
    text_zone: Literal[
        "upper_left", "middle_left", "upper_right", "middle_right"
    ] = "upper_left"

    @model_validator(mode="after")
    def camera_parameters(self):
        if any(
            not 0 <= value <= 1
            for point in (self.focus_start, self.focus_end)
            for value in point
        ):
            raise ValueError("camera focus must stay inside the source image")
        if self.motion_style != "hold_then_push" and self.hold_fraction:
            raise ValueError("hold_fraction requires hold_then_push")
        if self.motion_style == "hold" and (
            self.zoom_start != self.zoom_end or self.focus_start != self.focus_end
        ):
            raise ValueError("hold camera motion must have equal start/end framing")
        return self


class RouteReveal(ShotBase):
    kind: Literal["route_reveal"]
    intent: Literal["locate"]
    origin: str = Field(min_length=1, max_length=60)
    destination: str = Field(min_length=1, max_length=60)
    year: str = Field(default="", max_length=20)


class DetailReveal(CameraMove):
    kind: Literal["detail_reveal"]
    intent: Literal["examine_evidence", "foreshadow"]
    detail_marker: tuple[float, float]


class ComparisonBuild(ShotBase):
    kind: Literal["comparison_build"]
    intent: Literal["compare"]
    title: str = Field(min_length=1, max_length=100)
    left_label: str = Field(min_length=1, max_length=90)
    right_label: str = Field(min_length=1, max_length=90)
    left_value: float = Field(ge=0)
    right_value: float = Field(ge=0)
    unit: str = Field(default="", max_length=30)


class ProcessSteps(ShotBase):
    kind: Literal["process_steps"]
    intent: Literal["explain_process"]
    title: str = Field(min_length=1, max_length=100)
    steps: list[str] = Field(min_length=2, max_length=4)


class NarrationSequence(ShotBase):
    """Spoken-order visual steps; unlike a fact card, no planned item is dropped."""

    kind: Literal["narration_sequence"]
    intent: Literal["locate", "explain_process", "examine_evidence", "connect"]
    layout: Literal["timeline", "flow", "stack", "hierarchy", "carousel"]
    entries: list[str] = Field(min_length=2, max_length=8)
    heading: str = Field(default="", max_length=100)

    @model_validator(mode="after")
    def hierarchy_size(self):
        if self.layout == "hierarchy" and len(self.entries) not in {3, 4}:
            raise ValueError("A hierarchy needs a parent, two branches, and optionally an outcome")
        if self.layout == "timeline" and len(self.entries) > 4:
            raise ValueError("A timeline supports at most four simultaneous events")
        if self.layout in {"flow", "stack"} and len(self.entries) > 6:
            raise ValueError("A long sequence needs a rotating carousel layout")
        return self


class DocumentHighlight(ShotBase):
    kind: Literal["document_highlight"]
    intent: Literal["examine_evidence"]
    asset_ref: str
    label: str = Field(default="", max_length=80)
    # Rectangle is normalized to the source image: left, top, right, bottom.
    highlight_box: tuple[float, float, float, float]


class FactReveal(ShotBase):
    kind: Literal["fact_reveal"]
    intent: Literal["establish", "examine_evidence", "foreshadow"]
    eyebrow: str = Field(default="", max_length=100)
    line_1: str = Field(min_length=2, max_length=100)
    line_2: str = Field(default="", max_length=100)


class ConceptContrast(ShotBase):
    kind: Literal["concept_contrast"]
    intent: Literal["compare"]
    title: str = Field(min_length=1, max_length=90)
    left_label: str = Field(min_length=2, max_length=100)
    right_label: str = Field(min_length=2, max_length=100)
    verdict_style: Literal["neutral", "correction"] = "neutral"


class EvidenceGap(ShotBase):
    kind: Literal["evidence_gap"]
    intent: Literal["compare", "examine_evidence"]
    title: str = Field(min_length=1, max_length=90)
    expected_label: str = Field(min_length=2, max_length=100)
    observed_label: str = Field(min_length=2, max_length=100)
    gap_label: str = Field(min_length=2, max_length=90)


class RelationshipMap(ShotBase):
    kind: Literal["relationship_map"]
    intent: Literal["connect"]
    title: str = Field(min_length=1, max_length=90)
    nodes: list[str] = Field(min_length=2, max_length=4)


class BrandedOutro(ShotBase):
    kind: Literal["branded_outro"]
    intent: Literal["close"]
    recap_items: list[str] = Field(min_length=1, max_length=4)
    question: str = Field(min_length=5, max_length=180)
    cta: str = Field(min_length=5, max_length=100)


class GuidedMark(Model):
    """A small drawing action tied to a specific spoken idea."""

    kind: Literal["trace", "ring", "tag", "arrow"]
    narration_anchor: str = Field(min_length=3, max_length=120)
    label: str = Field(default="", max_length=48)
    points: list[tuple[float, float]] = Field(min_length=1, max_length=6)
    radius: float = Field(default=0.08, gt=0, le=0.25)

    @model_validator(mode="after")
    def geometry(self):
        minimum = 2 if self.kind in {"trace", "arrow"} else 1
        if len(self.points) < minimum:
            raise ValueError(f"{self.kind} needs at least {minimum} points")
        if self.kind in {"ring", "tag"} and len(self.points) != 1:
            raise ValueError(f"{self.kind} needs exactly one point")
        if self.kind == "tag" and not self.label.strip():
            raise ValueError("tag needs a visible label")
        if any(not 0.05 <= coordinate <= 0.95 for point in self.points for coordinate in point):
            raise ValueError("guided mark leaves safe image area")
        return self


class GuidedImage(ShotBase):
    """Narration-led annotations on a retained image, not a text-card template."""

    kind: Literal["guided_image"]
    intent: Literal["establish", "compare", "explain_process", "examine_evidence"]
    asset_ref: str
    visual_thread: str = Field(min_length=3, max_length=80)
    marks: list[GuidedMark] = Field(min_length=1, max_length=5)
    presentation: Literal["fill", "contain"] = "fill"
    crop_bottom: float = Field(default=0, ge=0, le=0.45)
    zoom_start: float = Field(default=1, ge=1, le=2)
    zoom_end: float = Field(default=1.08, ge=1, le=2)
    focus_start: tuple[float, float] = (0.5, 0.5)
    focus_end: tuple[float, float] = (0.5, 0.5)


class SemanticDiagram(ShotBase):
    """Validated topic-neutral relationship drawing, timed to measured narration."""

    kind: Literal["semantic_diagram"]
    intent: Intent
    scene: dict
    asset_status: Literal["ready"] = "ready"


MotionShot = Annotated[
    TitleReveal
    | DetailReveal
    | CameraMove
    | RouteReveal
    | ComparisonBuild
    | ProcessSteps
    | DocumentHighlight
    | FactReveal
    | ConceptContrast
    | EvidenceGap
    | RelationshipMap
    | BrandedOutro
    | GuidedImage
    | NarrationSequence
    | SemanticDiagram,
    Field(discriminator="kind"),
]


class ComparisonAsset(Model):
    path: str = Field(min_length=3)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: str = Field(min_length=8)


class MotionPlan(Model):
    schema_version: Literal[1] = 1
    project_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    chapter_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    comparison_name: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    width: int = Field(ge=640, le=1920)
    height: int = Field(ge=360, le=1080)
    fps: int = Field(ge=24, le=60)
    shots: list[MotionShot] = Field(min_length=1)
    comparison_assets: dict[str, ComparisonAsset] = Field(default_factory=dict)

    @model_validator(mode="after")
    def dimensions(self):
        if self.width % 2 or self.height % 2 or abs(self.width / self.height - 16 / 9) > 0.01:
            raise ValueError("motion comparison requires even 16:9 dimensions")
        return self


ACTION_BY_KIND = {
    "title_reveal": {"show_line_1", "show_line_2", "show_question"},
    "camera": {"show_label", "show_secondary_label", "start_move"},
    "route_reveal": {"draw_route"},
    "detail_reveal": {"show_label", "highlight_detail", "start_move"},
    "comparison_build": {"reveal_left", "reveal_right"},
    "process_steps": {"reveal_step"},
    "document_highlight": {"show_label", "highlight_document"},
    "fact_reveal": {"show_line_1", "show_line_2"},
    "concept_contrast": {"reveal_left", "reveal_right"},
    "evidence_gap": {"show_expected", "show_observed", "show_gap"},
    "relationship_map": {"reveal_step"},
    "branded_outro": {"reveal_step", "show_question", "show_label"},
    "guided_image": {"reveal_step"},
    "narration_sequence": {"reveal_step"},
    "semantic_diagram": {"reveal_step"},
}

REQUIRED_BY_KIND = {
    "title_reveal": {"show_line_1", "show_line_2", "show_question"},
    "camera": set(),
    "route_reveal": {"draw_route"},
    "detail_reveal": {"highlight_detail"},
    "comparison_build": {"reveal_left", "reveal_right"},
    "process_steps": {"reveal_step"},
    "document_highlight": {"highlight_document"},
    "fact_reveal": {"show_line_1"},
    "concept_contrast": {"reveal_left", "reveal_right"},
    "evidence_gap": {"show_expected", "show_observed", "show_gap"},
    "relationship_map": {"reveal_step"},
    "branded_outro": {"reveal_step", "show_question", "show_label"},
    "guided_image": {"reveal_step"},
    "narration_sequence": {"reveal_step"},
    "semantic_diagram": {"reveal_step"},
}


def _plain(value: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", "", value.casefold()).split())


def _contains_spoken_phrase(narration: str, excerpt: str) -> bool:
    """Check whole spoken tokens, allowing punctuation to vary in approved anchors."""
    spoken = re.findall(r"\w+", narration.casefold())
    phrase = re.findall(r"\w+", excerpt.casefold())
    return bool(phrase) and any(
        spoken[index : index + len(phrase)] == phrase
        for index in range(len(spoken) - len(phrase) + 1)
    )


def validate_motion_plan(plan: MotionPlan, project: dict, speech_windows: list[dict]):
    """Reject unsupported or ungrounded actions before any image or video render."""
    if not speech_windows:
        raise ValueError("Measured narration windows are required for motion cues")
    if plan.project_id != project["id"]:
        raise ValueError("Plan project ID does not match project")
    chapter = next(
        (item for item in project["spec"]["chapters"] if item["id"] == plan.chapter_id), None
    )
    if chapter is None:
        raise ValueError("Chapter not found")
    blocks = {item["id"]: item["text"] for item in chapter["narration_blocks"]}
    windows = {item["block_id"]: item for item in speech_windows}
    research = project["spec"].get("brief", {}).get("research", {})
    if "claims" in research:
        claims = {claim["id"] for claim in research["claims"]}
    else:
        # Older motion fixtures and plans predate the research ledger in this view.
        claims = {
            claim for item in project["spec"]["chapters"]
            for claim in item["evidence_claim_ids"]
        }
    seen_ids = set()
    end = 0.0
    for shot in plan.shots:
        if shot.id in seen_ids:
            raise ValueError("Motion shot IDs must be unique")
        seen_ids.add(shot.id)
        if abs(shot.start - end) > 0.01:
            raise ValueError("Shots must form a contiguous timeline")
        if not set(shot.narration_block_ids) <= blocks.keys():
            raise ValueError(f"Unknown narration block in {shot.id}")
        if not set(shot.narration_block_ids) <= windows.keys():
            raise ValueError(f"Missing measured narration window in {shot.id}")
        if not set(shot.claim_ids) <= claims:
            raise ValueError(f"Unknown claim in {shot.id}")
        if shot.kind in {
            "route_reveal",
            "comparison_build",
            "process_steps",
            "document_highlight",
            "evidence_gap",
            "concept_contrast",
            "relationship_map",
            "fact_reveal",
            "narration_sequence",
            "semantic_diagram",
        } and not shot.claim_ids:
            raise ValueError(f"Evidence-bearing shot {shot.id} needs claim IDs")
        known_assets = set(project["longform_media"]) | set(plan.comparison_assets)
        if hasattr(shot, "asset_ref") and shot.asset_ref not in known_assets:
            raise ValueError(f"Unknown asset in {shot.id}: {shot.asset_ref}")
        allowed = ACTION_BY_KIND[shot.kind]
        actions = {cue.action for cue in shot.cues}
        if not actions <= allowed or not REQUIRED_BY_KIND[shot.kind] <= actions:
            raise ValueError(f"Unsupported or missing motion action in {shot.id}")
        if shot.kind in {
            "process_steps",
            "relationship_map",
            "guided_image",
            "branded_outro",
            "narration_sequence",
            "semantic_diagram",
        }:
            indices = {cue.step_index for cue in shot.cues if cue.action == "reveal_step"}
            if shot.kind == "semantic_diagram":
                from pydantic import TypeAdapter

                from autonomous_studio.rendering.semantic_diagrams import (
                    Scene,
                    scene_asset_report,
                    scene_cues,
                )

                semantic = TypeAdapter(Scene).validate_python(shot.scene)
                items = scene_cues(semantic)
                if abs(semantic.duration - (shot.end - shot.start)) > 0.1:
                    raise ValueError(f"Semantic scene duration differs from {shot.id}")
                if any(
                    asset["status"] != "ready" or asset["match"] == "generic_fallback"
                    for asset in scene_asset_report(semantic)
                ):
                    raise ValueError(f"Semantic scene has unreviewed or missing assets: {shot.id}")
                if any(
                    abs(cue.at_seconds - shot.start - items[cue.step_index][1]) > 0.1
                    for cue in shot.cues
                    if cue.step_index is not None
                ):
                    raise ValueError(f"Semantic scene timing disagrees with cues in {shot.id}")
            else:
                items = (
                    shot.steps
                    if shot.kind == "process_steps"
                    else shot.nodes
                    if shot.kind == "relationship_map"
                    else shot.recap_items
                    if shot.kind == "branded_outro"
                    else shot.entries
                    if shot.kind == "narration_sequence"
                    else shot.marks
                )
            if indices != set(range(len(items))):
                raise ValueError("Every process or relationship item needs a reveal cue")
        if shot.kind == "guided_image":
            # The visual order is part of the story. A name or concept cannot be
            # revealed by a fixed template fraction before its spoken anchor.
            text = " ".join(blocks[block] for block in shot.narration_block_ids)
            plain_text = _plain(text)
            anchors = [_plain(mark.narration_anchor) for mark in shot.marks]
            positions = [plain_text.find(anchor) for anchor in anchors]
            if any(position < 0 for position in positions):
                raise ValueError(f"Guided mark anchor missing from narration in {shot.id}")
            if positions != sorted(positions) or len(positions) != len(set(positions)):
                raise ValueError(f"Guided marks disagree with spoken concept order in {shot.id}")
            marks_by_index = {
                cue.step_index: cue for cue in shot.cues if cue.action == "reveal_step"
            }
            cue_times = [marks_by_index[index].at_seconds for index in range(len(shot.marks))]
            if cue_times != sorted(cue_times):
                raise ValueError(f"Guided cues disagree with spoken concept order in {shot.id}")
        if shot.kind == "comparison_build":
            narration = _plain(" ".join(blocks[block] for block in shot.narration_block_ids))
            for value in (shot.left_value, shot.right_value):
                number = re.escape(_plain(f"{value:g}"))
                if not re.search(rf"(?<!\d){number}(?!\d)", narration):
                    raise ValueError("Comparison numbers must appear in approved narration")
        if shot.kind == "route_reveal":
            narration = _plain(" ".join(blocks[block] for block in shot.narration_block_ids))
            for label in (shot.origin, shot.destination, shot.year):
                if label and _plain(label) not in narration:
                    raise ValueError("Route labels must appear in approved narration")
        if shot.kind == "camera" and shot.label and "show_label" not in actions:
            raise ValueError("Camera label needs a show_label cue")
        if shot.kind == "camera" and shot.secondary_label and "show_secondary_label" not in actions:
            raise ValueError("Camera secondary label needs a show_secondary_label cue")
        if shot.kind == "detail_reveal" and shot.label and "show_label" not in actions:
            raise ValueError("Detail label needs a show_label cue")
        for cue in shot.cues:
            if not shot.start <= cue.at_seconds < shot.end:
                raise ValueError(f"Motion cue falls outside {shot.id}")
            if cue.at_seconds + cue.duration_seconds > shot.end + 0.1:
                raise ValueError(f"Motion cue overruns {shot.id}")
            narration = " ".join(blocks[block] for block in shot.narration_block_ids)
            if not _contains_spoken_phrase(narration, cue.narration_excerpt):
                raise ValueError(f"Cue excerpt is not in narration for {shot.id}")
            if not any(
                block in windows
                and windows[block]["start"] - 0.75 <= cue.at_seconds <= windows[block]["end"] + 0.75
                for block in shot.narration_block_ids
            ):
                raise ValueError(f"Motion cue is outside measured narration in {shot.id}")
        if hasattr(shot, "focus_start"):
            for pair in (shot.focus_start, shot.focus_end):
                if any(not 0 <= number <= 1 for number in pair):
                    raise ValueError(f"Camera focus leaves source image in {shot.id}")
        if hasattr(shot, "detail_marker") and any(
            not 0 <= number <= 1 for number in shot.detail_marker
        ):
            raise ValueError(f"Detail marker leaves frame in {shot.id}")
        if hasattr(shot, "highlight_box"):
            left, top, right, bottom = shot.highlight_box
            if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
                raise ValueError(f"Invalid document highlight box in {shot.id}")
        end = shot.end
    if abs(end - chapter["target_duration"]) > 0.1:
        raise ValueError("Plan duration differs from approved chapter duration")
    return chapter, end
