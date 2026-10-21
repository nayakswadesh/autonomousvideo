"""Experimental, topic-neutral explanatory graphics driven by validated scene data.

This renderer draws semantic roles, not named historical subjects. It intentionally
does not infer a diagram from free-form narration; a director must supply a plan.
"""

from __future__ import annotations

import hashlib
import io
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, get_args

from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, Field, model_validator

from autonomous_studio.audio.alignment import tokens
from autonomous_studio.rendering.semantic_assets import CODE_SYMBOLS, default_catalog, raster_asset

W, H = 960, 540
_RENDER_SCALE: ContextVar[float] = ContextVar("semantic_render_scale", default=1.0)
_PRESENTATION: ContextVar[str] = ContextVar("semantic_presentation", default="study")
_NOW: ContextVar[float] = ContextVar("semantic_now", default=0.0)
_STRICT_TEXT_FIT: ContextVar[bool] = ContextVar("semantic_strict_text_fit", default=False)
FONT_DIR = Path(__file__).resolve().parents[1] / "assets/fonts"
CHANNEL_LOGO = Path(__file__).resolve().parents[1] / "assets/branding/channel-logo.png"
INK, NAVY, PAPER = "#142c38", "#0b1a27", "#f4f0e8"
CYAN, GOLD, ORANGE, BLUE, GREEN = "#2eb8c7", "#edc17a", "#e59162", "#5a91b5", "#70c4a1"
SYMBOLS = CODE_SYMBOLS


class Step(BaseModel):
    label: str = Field(min_length=2, max_length=34)
    detail: str = Field(default="", max_length=42)
    anchor: str = Field(min_length=2)
    symbol: str
    asset_concept: str | None = None
    # Injected from the approved visual blueprint. Legacy scenes default to visible.
    graphic_visible: bool = True
    state_visual: Literal[
        "none", "fill_up", "fill_down", "expand", "contract", "highlight"
    ] = "none"
    at: float = Field(ge=0, le=40)

    @model_validator(mode="after")
    def known_symbol(self):
        if self.symbol not in SYMBOLS:
            raise ValueError(f"unsupported symbol: {self.symbol}")
        return self


class BoundaryLimitStep(Step):
    """The wide lower banner in a bounded statement, not a general Step slot."""

    label: str = Field(min_length=2, max_length=45)


class ContrastStep(Step):
    """Allow a wrapped detail only for juxtaposition's larger card."""

    detail: str = Field(default="", max_length=60)


class CueAnchor(BaseModel):
    anchor: str = Field(min_length=2)
    at: float = Field(ge=0, le=40)


class SceneBase(BaseModel):
    id: str = Field(min_length=2, max_length=60)
    topic: str = Field(min_length=2, max_length=65)
    narration: str = Field(min_length=15)
    title: str = Field(min_length=3, max_length=66)
    duration: float = Field(gt=2, le=40)
    provenance: Literal["illustrative", "verified"] = "illustrative"
    opening_label: str = Field(default="", max_length=42)
    opening_anchor: str = ""
    opening_at: float = Field(default=0, ge=0, le=40)

    @model_validator(mode="after")
    def valid_opening(self):
        if bool(self.opening_label) != bool(self.opening_anchor):
            raise ValueError("opening label and spoken anchor must be supplied together")
        if self.opening_anchor:
            _ordered_anchor_positions(
                self.narration, [self.opening_anchor],
                missing_error="opening anchor must occur in approved narration",
                order_error="opening anchor must occur in approved narration",
            )
            if self.opening_at >= self.duration:
                raise ValueError("opening cue must occur within the shot")
        return self


class ProcessScene(SceneBase):
    family: Literal["process"]
    treatment: Literal["working", "transformation"]
    steps: list[Step] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def ordered(self):
        times = [s.at for s in self.steps]
        if times != sorted(set(times)) or times[-1] >= self.duration:
            raise ValueError("step cues must be ordered, unique, and within the shot")
        _ordered_anchor_positions(
            self.narration, [step.anchor for step in self.steps],
            missing_error="every process step must bind to narration",
            order_error="process cues must follow narration order",
        )
        return self


class EvidenceScene(SceneBase):
    family: Literal["evidence"]
    treatment: Literal["competing", "prediction", "signal_to_inference"]
    expected: str = Field(default="", max_length=48)
    observed: str = Field(min_length=2, max_length=48)
    inference: str = Field(min_length=3, max_length=70)
    expected_anchor: str = ""
    observed_anchor: str = Field(min_length=2)
    inference_anchor: str = Field(min_length=2)
    expected_at: float = Field(default=0, ge=0, le=40)
    reveal_at: float = Field(ge=0, le=40)
    inference_at: float = Field(ge=0, le=40)
    expected_value: float | None = None
    observed_value: float | None = None
    unit: str | None = Field(default=None, max_length=16)
    expected_asset_concept: str | None = None
    observed_asset_concept: str | None = None
    inference_asset_concept: str | None = None

    @model_validator(mode="after")
    def valid_comparison(self):
        if self.treatment == "signal_to_inference":
            if (self.expected or self.expected_anchor or self.expected_value is not None
                    or self.expected_asset_concept):
                raise ValueError("signal-to-inference has no invented expectation")
            cues = [CueAnchor(anchor=self.observed_anchor, at=self.reveal_at),
                    CueAnchor(anchor=self.inference_anchor, at=self.inference_at)]
            _validate_ordered_anchors(self.narration, cues, self.duration)
            if self.observed_value is not None or self.unit:
                raise ValueError("signal-to-inference does not synthesize numeric measurements")
            return self
        if len(self.expected) < 2 or len(self.expected_anchor) < 2:
            raise ValueError("comparison evidence needs a spoken expectation")
        if not 0 <= self.expected_at < self.reveal_at < self.inference_at < self.duration:
            raise ValueError("evidence cues must be ordered and within the shot")
        _ordered_anchor_positions(
            self.narration,
            [self.expected_anchor, self.observed_anchor, self.inference_anchor],
            missing_error="every evidence cue must bind to narration",
            order_error="evidence cues must follow narration order",
        )
        values = (self.expected_value, self.observed_value)
        if any(value is not None for value in values):
            if (
                self.treatment != "prediction"
                or any(value is None or value < 0 for value in values)
                or not self.unit
            ):
                raise ValueError(
                    "quantitative prediction requires two nonnegative values and a unit"
                )
        return self


class Part(BaseModel):
    label: str = Field(min_length=2, max_length=31)
    detail: str = Field(default="", max_length=42)
    anchor: str = Field(min_length=2)
    symbol: str = "box"
    asset_concept: str | None = None
    graphic_visible: bool = True
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    at: float = Field(ge=0, le=40)

    @model_validator(mode="after")
    def known_symbol(self):
        if self.symbol not in SYMBOLS:
            raise ValueError(f"unsupported symbol: {self.symbol}")
        return self


class CutawayScene(SceneBase):
    family: Literal["cutaway"]
    treatment: Literal["guided", "exploded", "focal_inset"]
    symbol: str
    base_asset_concept: str | None = None
    parts: list[Part] = Field(min_length=1, max_length=3)
    depiction: Literal["schematic", "verified_physical"] = "schematic"

    @model_validator(mode="after")
    def valid_parts(self):
        if self.symbol not in SYMBOLS:
            raise ValueError(f"unsupported symbol: {self.symbol}")
        if self.treatment == "focal_inset":
            if len(self.parts) != 1 or not self.base_asset_concept or not self.opening_anchor:
                raise ValueError("focal inset needs one feature and a resolved subject asset")
        elif len(self.parts) < 2:
            raise ValueError("guided and exploded cutaways need at least two parts")
        times = [part.at for part in self.parts]
        if times != sorted(set(times)) or times[-1] >= self.duration:
            raise ValueError("part cues must be ordered, unique, and within the shot")
        _ordered_anchor_positions(
            self.narration, [part.anchor for part in self.parts],
            missing_error="every cutaway part must bind to narration",
            order_error="cutaway cues must follow narration order",
        )
        if self.depiction == "verified_physical" and self.provenance != "verified":
            raise ValueError("physical depiction requires verified provenance")
        return self


class RangeScene(SceneBase):
    family: Literal["range"]
    treatment: Literal["scale", "context"]
    low: float = Field(ge=0)
    high: float = Field(ge=0)
    unit: str = Field(min_length=1, max_length=25)
    qualifier: str = Field(min_length=2, max_length=46)
    anchor: str = Field(min_length=2)
    symbol: str
    asset_concept: str | None = None
    reveal_at: float = Field(ge=0, le=40)

    @model_validator(mode="after")
    def valid_range(self):
        if self.high <= self.low or self.reveal_at >= self.duration:
            raise ValueError("range must be increasing and revealed within the shot")
        if self.symbol not in SYMBOLS:
            raise ValueError(f"unsupported symbol: {self.symbol}")
        _ordered_anchor_positions(
            self.narration, [self.anchor],
            missing_error="range must bind to narration",
            order_error="range must bind to narration",
        )
        return self


def _ordered_anchor_positions(
    narration: str, anchors: list[str], *, missing_error: str, order_error: str
) -> list[int]:
    """Bind cues to successive spoken words, ignoring punctuation and spacing."""
    spoken = tokens(narration)
    positions = []
    previous = -1
    for anchor in anchors:
        phrase = tokens(anchor)
        matches = [
            index for index in range(len(spoken) - len(phrase) + 1)
            if phrase and spoken[index : index + len(phrase)] == phrase
        ]
        if not matches:
            raise ValueError(missing_error)
        position = next((index for index in matches if index > previous), None)
        if position is None:
            raise ValueError(order_error)
        positions.append(position)
        previous = position
    return positions


def _validate_ordered_anchors(narration: str, cues: list[Step], duration: float) -> None:
    times = [cue.at for cue in cues]
    if times != sorted(set(times)) or times[-1] >= duration:
        raise ValueError("cues must be ordered, unique, and within the shot")
    _ordered_anchor_positions(
        narration, [cue.anchor for cue in cues],
        missing_error="every cue must bind to narration",
        order_error="cue order must follow narration order",
    )


class CausalScene(SceneBase):
    family: Literal["causal"]
    treatment: Literal["chain", "mechanism"]
    stages: list[Step] = Field(min_length=3, max_length=4)

    @model_validator(mode="after")
    def valid_stages(self):
        _validate_ordered_anchors(self.narration, self.stages, self.duration)
        if self.treatment == "mechanism" and len(self.stages) != 3:
            raise ValueError("mechanism needs cause, mediator, and effect")
        return self


class BeforeAfterScene(SceneBase):
    family: Literal["before_after"]
    treatment: Literal["split", "wipe"]
    before: Step
    after: Step
    change: str = Field(min_length=3, max_length=48)
    visual_relation: Literal["replacement", "route"] = "replacement"

    @model_validator(mode="after")
    def valid_change(self):
        _validate_ordered_anchors(self.narration, [self.before, self.after], self.duration)
        if self.visual_relation == "route" and self.treatment != "split":
            raise ValueError("route comparison requires a split treatment")
        return self


class ParallelScene(SceneBase):
    family: Literal["parallel"]
    treatment: Literal["swimlanes", "converge"]
    tracks: list[Step] = Field(min_length=2, max_length=3)
    outcome: str = Field(min_length=3, max_length=50)
    outcome_anchor: str = Field(min_length=2)
    outcome_at: float = Field(ge=0, le=40)

    @model_validator(mode="after")
    def valid_tracks(self):
        _validate_ordered_anchors(self.narration, self.tracks, self.duration)
        if not self.tracks[-1].at < self.outcome_at < self.duration:
            raise ValueError("outcome must follow tracks within the shot")
        _ordered_anchor_positions(
            self.narration,
            [track.anchor for track in self.tracks] + [self.outcome_anchor],
            missing_error="outcome must bind after the tracks in narration",
            order_error="outcome must bind after the tracks in narration",
        )
        return self


class Place(Step):
    x: float = Field(ge=0.08, le=0.92)
    y: float = Field(ge=0.08, le=0.92)


class JourneyScene(SceneBase):
    family: Literal["journey"]
    treatment: Literal["locator", "route"]
    places: list[Place] = Field(min_length=2, max_length=4)
    geometry: Literal["schematic"] = "schematic"

    @model_validator(mode="after")
    def valid_places(self):
        _validate_ordered_anchors(self.narration, self.places, self.duration)
        return self


class ScaleItem(Step):
    value: float = Field(gt=0)


class ScaleLadderScene(SceneBase):
    family: Literal["scale_ladder"]
    treatment: Literal["bars", "zoom"]
    items: list[ScaleItem] = Field(min_length=2, max_length=3)
    unit: str = Field(min_length=1, max_length=18)

    @model_validator(mode="after")
    def valid_values(self):
        _validate_ordered_anchors(self.narration, self.items, self.duration)
        values = [item.value for item in self.items]
        if values != sorted(set(values)):
            raise ValueError("scale values must increase without duplicates")
        return self


class SynthesisScene(SceneBase):
    family: Literal["synthesis"]
    treatment: Literal["mosaic", "convergence"]
    inputs: list[Step] = Field(min_length=3, max_length=4)
    takeaway: str = Field(min_length=3, max_length=64)
    takeaway_anchor: str = Field(min_length=2)
    takeaway_at: float = Field(ge=0, le=40)

    @model_validator(mode="after")
    def valid_takeaway(self):
        _validate_ordered_anchors(self.narration, self.inputs, self.duration)
        if not self.inputs[-1].at < self.takeaway_at < self.duration:
            raise ValueError("takeaway must follow inputs within the shot")
        _ordered_anchor_positions(
            self.narration,
            [item.anchor for item in self.inputs] + [self.takeaway_anchor],
            missing_error="takeaway must follow inputs in narration",
            order_error="takeaway must follow inputs in narration",
        )
        return self


class ChronologyEvent(Step):
    date: str = Field(min_length=2, max_length=24)


class ChronologyScene(SceneBase):
    family: Literal["chronology"]
    treatment: Literal["event_track", "event_cards"]
    events: list[ChronologyEvent] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def valid_events(self):
        _validate_ordered_anchors(self.narration, self.events, self.duration)
        return self


class GeoPlace(BaseModel):
    label: str = Field(min_length=2, max_length=35)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    anchor: str = Field(min_length=2)
    at: float = Field(ge=0, le=40)


class GeoCoordinate(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class GeoRegion(BaseModel):
    label: str = Field(min_length=2, max_length=35)
    west: float = Field(ge=-180, le=180)
    east: float = Field(ge=-180, le=180)
    south: float = Field(ge=-90, le=90)
    north: float = Field(ge=-90, le=90)
    anchor: str = Field(min_length=2)
    at: float = Field(ge=0, le=40)

    @model_validator(mode="after")
    def valid_bounds(self):
        if self.west >= self.east or self.south >= self.north:
            raise ValueError("region bounds must increase")
        return self


class GeographyScene(SceneBase):
    family: Literal["geography"]
    treatment: Literal["map_locator", "region_focus", "verified_route"]
    places: list[GeoPlace] = Field(default_factory=list, max_length=4)
    region: GeoRegion | None = None
    route_source: str | None = None
    route_path: list[GeoCoordinate] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def valid_geometry(self):
        if self.treatment == "map_locator" and len(self.places) != 1:
            raise ValueError("map locator needs exactly one sourced point")
        if self.treatment == "region_focus" and self.region is None:
            raise ValueError("region focus needs sourced bounds")
        if self.treatment == "verified_route":
            if (
                len(self.places) != 2
                or len(self.route_path) < 2
                or not self.route_source
                or self.provenance != "verified"
            ):
                raise ValueError("mapped route needs sourced points and verified route geometry")
            first, last = self.route_path[0], self.route_path[-1]
            if (first.latitude, first.longitude) != (
                self.places[0].latitude,
                self.places[0].longitude,
            ) or (last.latitude, last.longitude) != (
                self.places[-1].latitude,
                self.places[-1].longitude,
            ):
                raise ValueError("verified route geometry must meet the narrated endpoints")
            if any(
                abs(right.longitude - left.longitude) > 180
                for left, right in zip(self.route_path, self.route_path[1:])
            ):
                raise ValueError("dateline-crossing routes need segmented geometry")
        cues = self.places if self.treatment != "region_focus" else [self.region]
        _validate_ordered_anchors(self.narration, cues, self.duration)
        return self


class ConceptContrastScene(SceneBase):
    family: Literal["concept_contrast"]
    treatment: Literal["juxtaposition", "paired_contributions", "correction_reveal"]
    left: ContrastStep
    right: ContrastStep
    resolution: str = Field(default="", max_length=60)
    resolution_anchor: str = ""
    resolution_at: float = Field(default=0, ge=0, le=40)

    @model_validator(mode="after")
    def valid_distinction(self):
        _validate_ordered_anchors(self.narration, [self.left, self.right], self.duration)
        if self.treatment != "juxtaposition" and any(
            len(item.detail) > 42 for item in (self.left, self.right)
        ):
            raise ValueError("supporting copy exceeds this contrast treatment's limit")
        if self.treatment == "juxtaposition":
            for item in (self.left, self.right):
                juxtaposition_detail_lines(item.detail)
        if self.treatment == "correction_reveal":
            if not self.resolution or not self.resolution_anchor:
                raise ValueError("correction requires a spoken resolution")
            _validate_ordered_anchors(
                self.narration,
                [
                    self.left,
                    self.right,
                    CueAnchor(anchor=self.resolution_anchor, at=self.resolution_at),
                ],
                self.duration,
            )
        return self


class QuantPoint(BaseModel):
    label: str = Field(min_length=2, max_length=34)
    value: float = Field(ge=0)
    anchor: str = Field(min_length=2)
    at: float = Field(ge=0, le=40)
    date: str = Field(default="", max_length=20)


class QuantitativeDisplayScene(SceneBase):
    family: Literal["quantitative_display"]
    treatment: Literal["metric_reveal", "comparable_bars", "time_trend"]
    points: list[QuantPoint] = Field(min_length=1, max_length=5)
    unit: str = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def valid_series(self):
        count = len(self.points)
        if (
            (self.treatment == "metric_reveal" and count != 1)
            or (self.treatment == "comparable_bars" and not 2 <= count <= 4)
            or (self.treatment == "time_trend" and not 3 <= count <= 5)
        ):
            raise ValueError("quantitative treatment has an incompatible number of values")
        if self.treatment == "time_trend" and (
            any(not point.date for point in self.points)
            or len({point.date for point in self.points}) != count
        ):
            raise ValueError("time trend needs distinct dated observations")
        _validate_ordered_anchors(self.narration, self.points, self.duration)
        return self


class DocumentFocusScene(SceneBase):
    family: Literal["document_focus"]
    treatment: Literal["guided_highlight", "extracted_finding"]
    document_label: str = Field(min_length=2, max_length=46)
    excerpt: str = Field(min_length=3, max_length=90)
    excerpt_anchor: str = Field(min_length=2)
    excerpt_at: float = Field(ge=0, le=40)
    finding: str = Field(min_length=3, max_length=65)
    finding_anchor: str = Field(min_length=2)
    finding_at: float = Field(ge=0, le=40)

    @model_validator(mode="after")
    def valid_finding(self):
        if not 0 <= self.excerpt_at < self.finding_at < self.duration:
            raise ValueError("document finding must follow excerpt")
        _ordered_anchor_positions(
            self.narration, [self.excerpt_anchor, self.finding_anchor],
            missing_error="document cues must follow narration",
            order_error="document cues must follow narration",
        )
        return self


class RelationshipNode(Step):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    parent_id: str | None = None


class RelationshipLink(BaseModel):
    from_id: str
    to_id: str


class RelationshipScene(SceneBase):
    family: Literal["relationship"]
    treatment: Literal["hierarchy_build", "network_build", "nested_scope"]
    nodes: list[RelationshipNode] = Field(min_length=2, max_length=5)
    links: list[RelationshipLink] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_links(self):
        _validate_ordered_anchors(self.narration, self.nodes, self.duration)
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("relationship nodes must have unique IDs")
        if self.treatment == "nested_scope":
            if len(self.nodes) != 3 or any(
                node.parent_id != (self.nodes[index + 1].id if index < 2 else None)
                for index, node in enumerate(self.nodes)
            ) or self.links:
                raise ValueError("nested scope needs three spoken inner-to-outer levels")
        elif self.treatment == "hierarchy_build":
            if len(self.nodes) < 3:
                raise ValueError("hierarchy needs a parent and at least two children")
            roots = [node for node in self.nodes if node.parent_id is None]
            if len(roots) != 1 or any(
                node.parent_id != roots[0].id for node in self.nodes if node.parent_id is not None
            ):
                raise ValueError("hierarchy treatment supports one parent and direct children")
        elif len(self.nodes) < 3 or len(self.links) < 2 or any(
            link.from_id not in ids or link.to_id not in ids or link.from_id == link.to_id
            for link in self.links
        ):
            raise ValueError("network needs at least two supported node links")
        return self


class ClaimBoundaryScene(SceneBase):
    family: Literal["claim_boundary"]
    treatment: Literal["bounded_statement"]
    claim: Step
    limit: BoundaryLimitStep

    @model_validator(mode="after")
    def valid_boundary(self):
        _validate_ordered_anchors(self.narration, [self.claim, self.limit], self.duration)
        return self


class ScenarioBranchScene(SceneBase):
    family: Literal["scenario_branch"]
    treatment: Literal["conditional_fork"]
    condition: Step
    first_outcome: Step
    second_outcome: Step

    @model_validator(mode="after")
    def valid_branch(self):
        _validate_ordered_anchors(
            self.narration,
            [self.condition, self.first_outcome, self.second_outcome],
            self.duration,
        )
        return self


class EditorialLine(Step):
    # Editorial frames use wide, fitted typography, unlike narrow diagram nodes.
    label: str = Field(min_length=2, max_length=66)


class EditorialFrameScene(SceneBase):
    family: Literal["editorial_frame"]
    treatment: Literal["kinetic_question", "statement_reveal", "chapter_marker"]
    lines: list[EditorialLine] = Field(min_length=1, max_length=3)
    chapter_label: str = Field(default="", max_length=28)

    @model_validator(mode="after")
    def valid_lines(self):
        _validate_ordered_anchors(self.narration, self.lines, self.duration)
        if self.treatment == "kinetic_question" and "?" not in self.narration:
            raise ValueError("kinetic question needs a spoken question")
        if self.treatment == "chapter_marker" and not self.chapter_label:
            raise ValueError("chapter marker needs a chapter label")
        return self


class CommunityAction(BaseModel):
    role: Literal["comment", "like", "share", "subscribe", "like_share"]
    label: str = Field(min_length=2, max_length=100)
    anchor: str = Field(min_length=2)
    at: float = Field(ge=0, le=40)


class EditorialCloseScene(SceneBase):
    family: Literal["editorial_close"]
    treatment: Literal["recap_grid", "loop_bridge", "community_close"]
    recap: list[Step] = Field(default_factory=list, max_length=4)
    actions: list[CommunityAction] = Field(default_factory=list, max_length=4)
    response_options: list[str] = Field(default_factory=list, max_length=3)
    # Derived from measured speech after semantic layout; absent on saved older scenes.
    response_option_ats: list[float] = Field(default_factory=list, max_length=3)
    end_screen_side: Literal["right", "left"] = "right"
    question: str = Field(default="", max_length=100)
    question_anchor: str = ""
    question_at: float = Field(default=0, ge=0, le=40)
    invitation: str = Field(default="", max_length=100)
    invitation_anchor: str = ""
    invitation_at: float = Field(default=0, ge=0, le=40)
    bridge: str = Field(default="", max_length=100)
    bridge_anchor: str = ""
    bridge_at: float = Field(default=0, ge=0, le=40)

    @model_validator(mode="after")
    def valid_close(self):
        if self.treatment == "community_close":
            if self.recap or any((self.question, self.invitation, self.bridge)):
                raise ValueError("community close cannot invent recap or legacy invitation")
            if not self.actions:
                raise ValueError("community close needs a spoken audience action")
            roles = [item.role for item in self.actions]
            if len(set(roles)) != len(roles):
                raise ValueError("community close repeats an audience action")
            if "comment" in roles and roles[0] != "comment":
                raise ValueError("community question must lead the closing actions")
            if self.response_options:
                if "comment" not in roles or len(self.response_options) not in (2, 3):
                    raise ValueError("response choices need a spoken community question")
                if any(not item.strip() or len(item) > 44 for item in self.response_options):
                    raise ValueError("response choice exceeds its readable slot")
                _ordered_anchor_positions(
                    self.narration, self.response_options,
                    missing_error="response choice must be spoken",
                    order_error="response choices must follow spoken order",
                )
            if self.response_option_ats:
                comment = next((item for item in self.actions if item.role == "comment"), None)
                if (len(self.response_option_ats) != len(self.response_options)
                    or self.response_option_ats != sorted(set(self.response_option_ats))
                    or any(at < 0 or at >= self.duration for at in self.response_option_ats)
                    or comment is None or self.response_option_ats[-1] >= comment.at):
                    raise ValueError("response choice entrances must precede the comment cue")
            _validate_ordered_anchors(self.narration, self.actions, self.duration)
            return self
        if (self.actions or self.response_options or self.response_option_ats
                or not self.recap or not self.question or not self.invitation):
            raise ValueError("legacy editorial close needs recap, question, and invitation")
        cues = list(self.recap) + [
            CueAnchor(anchor=self.question_anchor, at=self.question_at),
            CueAnchor(anchor=self.invitation_anchor, at=self.invitation_at),
        ]
        if self.treatment == "loop_bridge":
            if not self.bridge or not self.bridge_anchor:
                raise ValueError("loop bridge needs a spoken return to the opening")
            cues.append(CueAnchor(anchor=self.bridge_anchor, at=self.bridge_at))
        _validate_ordered_anchors(self.narration, cues, self.duration)
        return self


Scene = Annotated[
    ProcessScene
    | EvidenceScene
    | CutawayScene
    | RangeScene
    | CausalScene
    | BeforeAfterScene
    | ParallelScene
    | JourneyScene
    | ScaleLadderScene
    | SynthesisScene
    | ChronologyScene
    | GeographyScene
    | ConceptContrastScene
    | QuantitativeDisplayScene
    | DocumentFocusScene
    | RelationshipScene
    | ClaimBoundaryScene
    | ScenarioBranchScene
    | EditorialFrameScene
    | EditorialCloseScene,
    Field(discriminator="family"),
]

SCENE_MODELS = {
    get_args(model.model_fields["family"].annotation)[0]: model
    for model in (
        ProcessScene,
        EvidenceScene,
        CutawayScene,
        RangeScene,
        CausalScene,
        BeforeAfterScene,
        ParallelScene,
        JourneyScene,
        ScaleLadderScene,
        SynthesisScene,
        ChronologyScene,
        GeographyScene,
        ConceptContrastScene,
        QuantitativeDisplayScene,
        DocumentFocusScene,
        RelationshipScene,
        ClaimBoundaryScene,
        ScenarioBranchScene,
        EditorialFrameScene,
        EditorialCloseScene,
    )
}


def _px(value: float) -> int:
    return round(value * _RENDER_SCALE.get())


def _point(point):
    return (_px(point[0]), _px(point[1]))


class _Draw:
    """Draw virtual 960×540 coordinates directly at the target raster resolution."""

    def __init__(self, image: Image.Image):
        self.raw = ImageDraw.Draw(image)

    def rounded_rectangle(self, box, *, radius=0, width=1, **kwargs):
        self.raw.rounded_rectangle(
            tuple(_px(v) for v in box), radius=_px(radius), width=max(1, _px(width)), **kwargs
        )

    def ellipse(self, box, *, width=1, **kwargs):
        self.raw.ellipse(tuple(_px(v) for v in box), width=max(1, _px(width)), **kwargs)

    def arc(self, box, start, end, *, width=1, **kwargs):
        self.raw.arc(tuple(_px(v) for v in box), start, end, width=max(1, _px(width)), **kwargs)

    def polygon(self, points, *, width=1, **kwargs):
        self.raw.polygon([_point(point) for point in points], width=max(1, _px(width)), **kwargs)

    def line(self, points, *, width=1, **kwargs):
        self.raw.line([_point(point) for point in points], width=max(1, _px(width)), **kwargs)


def _font(size: int, bold=False):
    return ImageFont.truetype(
        FONT_DIR / ("BarlowCondensed-Bold.ttf" if bold else "Barlow-Regular.ttf"), _px(size)
    )


def _text(draw, xy, value, size=24, fill=INK, bold=False, anchor=None):
    if _STRICT_TEXT_FIT.get() and value:
        validate_display_glyphs(value, size=size, bold=bold)
        font = _font(size, bold)
        bounds = draw.raw.textbbox(_point(xy), value, font=font, anchor=anchor)
        if not (0 <= bounds[0] and bounds[2] <= _px(W)
                and 0 <= bounds[1] and bounds[3] <= _px(H)):
            raise ValueError(f"semantic text extends outside the video frame: {value[:80]}")
    draw.raw.text(_point(xy), value, font=_font(size, bold), fill=fill, anchor=anchor)


def validate_display_glyphs(value: str, *, size: int = 20, bold: bool = False) -> None:
    """Reject unavailable glyphs instead of rendering replacement boxes."""
    font = _font(size, bold)
    missing = bytes(font.getmask("\ufffd"))
    for char in value:
        if char != "\ufffd" and not char.isspace() and bytes(font.getmask(char)) == missing:
            raise ValueError(f"renderer font cannot display U+{ord(char):04X}")


def _box(draw, xy, fill, outline=None, radius=12):
    draw.rounded_rectangle(tuple(map(int, xy)), radius=radius, fill=fill, outline=outline, width=2)


def _ease(value: float):
    value = max(0, min(1, value))
    return value * value * (3 - 2 * value)


def _progress(now, at, length=0.55):
    return _ease((now - at) / length)


def _symbol(draw, kind: str, x: float, y: float, s: float, color: str):
    """Small neutral glyphs; never pass these off as a named person or real object."""
    r = max(2, int(5 * s))
    if kind == "people":
        for dx in (-17, 0, 17):
            cx = x + dx * s
            draw.ellipse((cx - r, y - 23 * s - r, cx + r, y - 23 * s + r), fill=color)
            draw.rounded_rectangle(
                (cx - 5 * s, y - 15 * s, cx + 5 * s, y + 11 * s), radius=r, fill=color
            )
    elif kind == "book":
        draw.polygon(
            [
                (x - 29 * s, y - 16 * s),
                (x - 3 * s, y - 10 * s),
                (x - 3 * s, y + 19 * s),
                (x - 29 * s, y + 13 * s),
            ],
            outline=color,
            width=3,
        )
        draw.polygon(
            [
                (x + 3 * s, y - 10 * s),
                (x + 29 * s, y - 16 * s),
                (x + 29 * s, y + 13 * s),
                (x + 3 * s, y + 19 * s),
            ],
            outline=color,
            width=3,
        )
    elif kind == "branch":
        draw.line([(x, y - 25 * s), (x, y + 23 * s)], fill=color, width=max(2, int(3 * s)))
        for dx in (-25, 25):
            draw.line(
                [(x, y - 3 * s), (x + dx * s, y - 18 * s)], fill=color, width=max(2, int(3 * s))
            )
            draw.ellipse(
                (x + dx * s - 4 * s, y - 22 * s, x + dx * s + 4 * s, y - 14 * s), fill=color
            )
    elif kind == "pin":
        draw.ellipse((x - 14 * s, y - 23 * s, x + 14 * s, y + 5 * s), outline=color, width=3)
        draw.polygon([(x - 10 * s, y), (x, y + 27 * s), (x + 10 * s, y)], fill=color)
        draw.ellipse((x - 4 * s, y - 13 * s, x + 4 * s, y - 5 * s), fill=PAPER)
    elif kind == "drop":
        draw.polygon(
            [
                (x, y - 31 * s),
                (x - 17 * s, y + 1 * s),
                (x - 13 * s, y + 17 * s),
                (x, y + 24 * s),
                (x + 13 * s, y + 17 * s),
                (x + 17 * s, y + 1 * s),
            ],
            fill=color,
        )
    elif kind == "cloud":
        for dx, dy, rr in ((-16, 2, 14), (0, -8, 18), (18, 2, 14)):
            draw.ellipse(
                (x + (dx - rr) * s, y + (dy - rr) * s, x + (dx + rr) * s, y + (dy + rr) * s),
                fill=color,
            )
    elif kind == "leaf":
        draw.ellipse((x - 23 * s, y - 17 * s, x + 23 * s, y + 17 * s), outline=color, width=3)
        draw.line([(x - 24 * s, y + 19 * s), (x + 24 * s, y - 19 * s)], fill=color, width=3)
    elif kind == "pump":
        _box(draw, (x - 28 * s, y - 16 * s, x + 18 * s, y + 16 * s), color, radius=5)
        draw.line([(x - 42 * s, y), (x - 28 * s, y)], fill=color, width=4)
        draw.line([(x + 18 * s, y), (x + 40 * s, y)], fill=color, width=4)
        draw.ellipse((x - 9 * s, y - 9 * s, x + 9 * s, y + 9 * s), fill=PAPER)
    elif kind == "signal":
        draw.line(
            [
                (x - 30 * s, y),
                (x - 9 * s, y),
                (x - 3 * s, y - 23 * s),
                (x + 6 * s, y + 20 * s),
                (x + 12 * s, y),
                (x + 30 * s, y),
            ],
            fill=color,
            width=4,
        )
    elif kind == "car":
        _box(draw, (x - 31 * s, y - 5 * s, x + 31 * s, y + 17 * s), color, radius=5)
        _box(draw, (x + 6 * s, y - 19 * s, x + 24 * s, y + 2 * s), color, radius=3)
        for dx in (-18, 18):
            draw.ellipse((x + (dx - 6) * s, y + 10 * s, x + (dx + 6) * s, y + 22 * s), fill=INK)
    elif kind == "box":
        _box(draw, (x - 25 * s, y - 23 * s, x + 25 * s, y + 23 * s), color, radius=5)
    else:
        draw.ellipse((x - 21 * s, y - 21 * s, x + 21 * s, y + 21 * s), outline=color, width=4)
        _text(draw, (x, y), "?", 30, color, True, "mm")


def _paint_asset(
    im: Image.Image, concept: str | None, symbol: str, x: float, y: float, size: int, color: str
):
    if concept is None:
        _symbol(_Draw(im), symbol, x, y, size / 70, color)
        return (x - size / 2, y - size / 2, size, size)
    catalog = default_catalog()
    choice = catalog.select(concept)
    record = catalog.get(choice.asset_id)
    if record.kind == "code":
        _symbol(_Draw(im), record.id.removeprefix("code/"), x, y, size / 70, color)
        return (x - size / 2, y - size / 2, size, size)
    asset = raster_asset(record.id, _px(size), color)
    left, top = round(_px(x) - asset.width / 2), round(_px(y) - asset.height / 2)
    im.paste(asset, (left, top), asset)
    scale = _RENDER_SCALE.get()
    return (left / scale, top / scale, asset.width / scale, asset.height / scale)


def _paint_step_asset(
    im: Image.Image, step: Step, x: float, y: float, size: int, color: str
):
    if not step.graphic_visible:
        return None
    return _paint_asset(im, step.asset_concept, step.symbol, x, y, size, color)


def scene_asset_report(scene: Scene) -> list[dict]:
    """Return choices and publication gates; never silently hide a fallback."""
    requests: list[tuple[str | None, str]] = []
    def visible(items):
        return [
            (item.asset_concept, item.symbol)
            for item in items if getattr(item, "graphic_visible", True)
        ]

    if scene.family == "process":
        requests = visible(scene.steps)
    elif scene.family == "evidence":
        requests = [
            (x, "box") for x in (
                scene.expected_asset_concept, scene.observed_asset_concept,
                scene.inference_asset_concept,
            ) if x
        ]
    elif scene.family == "cutaway":
        requests = [(scene.base_asset_concept, scene.symbol)]
        if scene.treatment != "focal_inset":
            requests.extend(visible(scene.parts))
    elif scene.family == "range":
        requests = [(scene.asset_concept, scene.symbol)]
    elif scene.family == "causal":
        requests = visible(scene.stages)
    elif scene.family == "before_after":
        requests = visible((scene.before, scene.after))
    elif scene.family == "parallel":
        requests = visible(scene.tracks)
    elif scene.family == "journey":
        requests = visible(scene.places)
    elif scene.family == "scale_ladder":
        requests = visible(scene.items)
    elif scene.family == "synthesis":
        requests = visible(scene.inputs)
    elif scene.family == "chronology":
        requests = visible(scene.events)
    elif scene.family == "concept_contrast":
        requests = visible((scene.left, scene.right))
    elif scene.family == "relationship":
        requests = visible(scene.nodes)
    elif scene.family == "editorial_frame":
        requests = visible(scene.lines)
    elif scene.family == "editorial_close":
        requests = visible(scene.recap)
    elif scene.family == "claim_boundary":
        requests = visible((scene.claim, scene.limit))
    elif scene.family == "scenario_branch":
        requests = visible((scene.condition, scene.first_outcome, scene.second_outcome))
    catalog = default_catalog()
    report = []
    for concept, symbol in requests:
        if concept:
            choice = catalog.select(concept)
            record = catalog.get(choice.asset_id)
            match, reason = choice.match, choice.reason
        else:
            record = catalog.get(f"code/{symbol}")
            match, reason = "manual", "native code-drawn primitive"
        report.append(
            {
                "concept": concept or symbol,
                "asset_id": record.id,
                "kind": record.kind,
                "match": match,
                "reason": reason,
                "source": record.source,
                "license": record.license,
                "status": record.status,
                "style": record.style,
                "sha256": (
                    hashlib.sha256(catalog.path_for(record).read_bytes()).hexdigest()
                    if record.path
                    else None
                ),
            }
        )
    if scene.family == "geography":
        report.append(
            {
                "concept": "land outlines",
                "asset_id": "natural-earth/ne_110m_land",
                "kind": "dataset",
                "match": "exact",
                "reason": "bundled public-domain geographic data",
                "license": "public domain",
                "status": "ready",
            }
        )
    return report


def _family_cues(scene: Scene) -> list[tuple[str, float]]:
    """Return the spoken anchors and relative reveal times for every semantic role."""
    if scene.family == "process":
        return [(item.anchor, item.at) for item in scene.steps]
    if scene.family == "evidence":
        if scene.treatment == "signal_to_inference":
            return [
                (scene.observed_anchor, scene.reveal_at),
                (scene.inference_anchor, scene.inference_at),
            ]
        return [
            (scene.expected_anchor, scene.expected_at),
            (scene.observed_anchor, scene.reveal_at),
            (scene.inference_anchor, scene.inference_at),
        ]
    if scene.family == "cutaway":
        return [(item.anchor, item.at) for item in scene.parts]
    if scene.family == "range":
        return [(scene.anchor, scene.reveal_at)]
    if scene.family == "causal":
        return [(item.anchor, item.at) for item in scene.stages]
    if scene.family == "before_after":
        return [(item.anchor, item.at) for item in (scene.before, scene.after)]
    if scene.family == "parallel":
        return [(item.anchor, item.at) for item in scene.tracks] + [
            (scene.outcome_anchor, scene.outcome_at)
        ]
    if scene.family == "journey":
        return [(item.anchor, item.at) for item in scene.places]
    if scene.family == "scale_ladder":
        return [(item.anchor, item.at) for item in scene.items]
    if scene.family == "synthesis":
        return [(item.anchor, item.at) for item in scene.inputs] + [
            (scene.takeaway_anchor, scene.takeaway_at)
        ]
    if scene.family == "chronology":
        return [(item.anchor, item.at) for item in scene.events]
    if scene.family == "geography":
        items = [scene.region] if scene.treatment == "region_focus" else scene.places
        return [(item.anchor, item.at) for item in items]
    if scene.family == "concept_contrast":
        cues = [(item.anchor, item.at) for item in (scene.left, scene.right)]
        return cues + (
            [(scene.resolution_anchor, scene.resolution_at)]
            if scene.treatment == "correction_reveal"
            else []
        )
    if scene.family == "quantitative_display":
        return [(item.anchor, item.at) for item in scene.points]
    if scene.family == "document_focus":
        return [
            (scene.excerpt_anchor, scene.excerpt_at),
            (scene.finding_anchor, scene.finding_at),
        ]
    if scene.family == "relationship":
        return [(item.anchor, item.at) for item in scene.nodes]
    if scene.family == "claim_boundary":
        return [(item.anchor, item.at) for item in (scene.claim, scene.limit)]
    if scene.family == "scenario_branch":
        return [
            (item.anchor, item.at)
            for item in (scene.condition, scene.first_outcome, scene.second_outcome)
        ]
    if scene.family == "editorial_frame":
        return [(item.anchor, item.at) for item in scene.lines]
    if scene.family == "editorial_close":
        if scene.treatment == "community_close":
            return [(item.anchor, item.at) for item in scene.actions]
        cues = [(item.anchor, item.at) for item in scene.recap] + [
            (scene.question_anchor, scene.question_at),
            (scene.invitation_anchor, scene.invitation_at),
        ]
        return cues + (
            [(scene.bridge_anchor, scene.bridge_at)] if scene.treatment == "loop_bridge" else []
        )
    raise ValueError(f"unregistered scene family: {scene.family}")


def scene_cues(scene: Scene) -> list[tuple[str, float]]:
    """Include optional opening context before the family-specific narrated roles."""
    cues = _family_cues(scene)
    return ([(scene.opening_anchor, scene.opening_at)] if scene.opening_anchor else []) + cues


def _base(scene: Scene, dark=False):
    bg = NAVY if dark else PAPER
    fg = "#f7f3ec" if dark else INK
    sub = "#a8c2ca" if dark else "#55717c"
    im = Image.new("RGB", (_px(W), _px(H)), bg)
    d = _Draw(im)
    if _PRESENTATION.get() == "study":
        _text(d, (42, 24), scene.family.upper() + "  /  " + scene.treatment.upper(), 17, sub, True)
        _fit_text(d, (42, 59), scene.title, 876, 37, fg, True)
        d.line([(42, 114), (918, 114)], fill="#365266" if dark else "#c5d4d4", width=2)
        _text(d, (42, 507), "ILLUSTRATIVE SEMANTIC TEST  •  " + scene.topic.upper(), 17, sub, True)
    else:
        _fit_text(d, (42, 35), scene.title, 876, 37, fg, True)
        d.line([(42, 96), (918, 96)], fill="#365266" if dark else "#c5d4d4", width=2)
    if scene.opening_label and not (
        scene.family == "cutaway" and scene.treatment == "focal_inset"
    ):
        first_family_at = _family_cues(scene)[0][1]
        if scene.opening_at <= _NOW.get() < first_family_at + 0.6:
            _box(d, (42, 118, 502, 158), "#28495b" if dark else "#dce9e8")
            _fit_text(d, (58, 139), scene.opening_label, 425, 22, fg, True, "lm")
    return im, d, fg, sub


def _process_copy(draw, text: str, *, x: float, y: float, width: float,
                  role: str, bold: bool, color: str, size: int, minimum: int) -> None:
    """Wrap exact approved process copy within a measured node box."""
    if not text:
        return
    words = text.split()
    failure = None
    for font_size in range(size, minimum - 1, -1):
        font = _font(font_size, bold)
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}" if current else word
            box = draw.raw.textbbox((0, 0), candidate, font=font)
            if box[2] - box[0] <= _px(width):
                current = candidate
            else:
                if not current:
                    continue
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        if len(lines) <= 2 and " ".join(lines) == " ".join(words) and all(
            draw.raw.textbbox((0, 0), line, font=font)[2]
            - draw.raw.textbbox((0, 0), line, font=font)[0] <= _px(width)
            for line in lines
        ):
            placements = []
            for index, line in enumerate(lines):
                position = (x, y + index * (font_size + 3))
                bounds = draw.raw.textbbox(_point(position), line, font=font, anchor="ma")
                if bounds[0] < _px(20) or bounds[2] > _px(W - 20):
                    failure = f"process {role} exceeds the frame-safe text area"
                    break
                if bounds[1] < _px(110) or bounds[3] > _px(440):
                    failure = f"process {role} overlaps title or caption-safe area"
                    break
                placements.append((position, line))
            else:
                for position, line in placements:
                    _text(draw, position, line, font_size, color, bold, "ma")
                return
    raise ValueError(
        failure or f"process {role} cannot fit its {width:.0f}px node box: {text[:80]}"
    )


def validate_process_slots(title: str, labels: list[str], details: list[str]) -> None:
    """Check planned process text in the actual two-to-four-node renderer boxes."""
    n = len(labels)
    if n not in {2, 3, 4} or len(details) != n:
        raise ValueError("process blueprint needs two to four paired label/detail slots")
    draw = _Draw(Image.new("RGB", (W, H), PAPER))
    width = min(200, 710 / (n - 1) - 38)
    token = _STRICT_TEXT_FIT.set(True)
    try:
        _fit_text(draw, (42, 35), title, 876, 37, INK, True)
        for index, (label, detail) in enumerate(zip(labels, details, strict=True)):
            x = int(125 + index * 710 / (n - 1))
            _process_copy(draw, label, x=x, y=343, width=width,
                          role=f"step {index + 1} label", bold=True,
                          color=INK, size=26, minimum=17)
            _process_copy(draw, detail, x=x, y=400, width=width,
                          role=f"step {index + 1} detail", bold=False,
                          color=INK, size=18, minimum=15)
    finally:
        _STRICT_TEXT_FIT.reset(token)


def _process_state(draw, x: float, y: float, change: str, color: str) -> None:
    """A restrained, subject-neutral visual change chosen by the approved plan."""
    if change in {"fill_up", "fill_down"}:
        _box(draw, (x + 49, y - 42, x + 66, y + 42), "#183343", "#9ab9c0", radius=5)
        fraction = 0.78 if change == "fill_up" else 0.22
        top = y + 38 - 76 * fraction
        _box(draw, (x + 52, top, x + 63, y + 38), color, radius=3)
    elif change in {"expand", "contract"}:
        radius = 68 if change == "expand" else 41
        draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                     outline=color, width=4)
    elif change == "highlight":
        draw.arc((x - 61, y - 61, x + 61, y + 61), 30, 325, fill=color, width=5)


def _process(scene: ProcessScene, now: float):
    im, d, fg, sub = _base(scene, scene.treatment == "transformation")
    n = len(scene.steps)
    xs = [int(125 + i * 710 / max(1, n - 1)) for i in range(n)]
    node_width = min(200, 710 / max(1, n - 1) - 38)
    for i, (x, step) in enumerate(zip(xs, scene.steps)):
        p = _progress(now, step.at)
        if scene.treatment == "working":
            if step.graphic_visible:
                d.ellipse((x - 51, 211, x + 51, 313), fill="#e2e9e8", outline="#7a9ca1", width=2)
                if p:
                    _process_state(d, x, 262, step.state_visual, CYAN)
                _paint_step_asset(im, step, x, 262, 72, CYAN if p else "#8eacb0")
                if p:
                    d.arc((x - 57, 205, x + 57, 319), 210, 210 + int(330 * p), fill=CYAN, width=6)
            else:
                d.ellipse((x - 22, 240, x + 22, 284), fill=CYAN if p else "#d8e5e4")
                if now >= step.at:
                    _text(d, (x, 262), str(i + 1), 24, PAPER, True, "mm")
            if now >= step.at:
                _process_copy(d, step.label, x=x, y=330 if not step.graphic_visible else 342, width=node_width,
                              role=f"step {i + 1} label", bold=True, color=fg,
                              size=24, minimum=17)
                _process_copy(d, step.detail, x=x, y=393 if not step.graphic_visible else 399, width=node_width,
                              role=f"step {i + 1} detail", bold=False, color=sub,
                              size=18, minimum=15)
            if i < n - 1:
                d.line(
                    [(x + 58, 262), (xs[i + 1] - 58, 262)],
                    fill=CYAN if now >= scene.steps[i + 1].at else "#b8c9ca",
                    width=4,
                )
        else:
            current = max((j for j, s in enumerate(scene.steps) if now >= s.at), default=-1)
            if i == current:
                _box(d, (x - 91, 190, x + 91, 332), "#23465a", "#79a5b2")
                _process_state(d, x, 257, step.state_visual, GOLD)
                _paint_step_asset(im, step, x, 257, 94, GOLD)
                _process_copy(d, step.label, x=x, y=343, width=node_width,
                              role=f"step {i + 1} label", bold=True, color=fg,
                              size=26, minimum=17)
                _process_copy(d, step.detail, x=x, y=400, width=node_width,
                              role=f"step {i + 1} detail", bold=False, color=sub,
                              size=18, minimum=15)
            else:
                d.ellipse((x - 12, 245, x + 12, 269), fill=CYAN if i < current else "#365366")
                if now >= step.at:
                    _process_copy(d, step.label, x=x, y=354, width=node_width,
                                  role=f"step {i + 1} label", bold=True, color=sub,
                                  size=21, minimum=17)
    return im


def _evidence(scene: EvidenceScene, now: float):
    if scene.treatment == "signal_to_inference":
        im, d, fg, sub = _base(scene, True)
        _box(d, (66, 172, 412, 403), "#1d394b", CYAN)
        _box(d, (548, 172, 894, 403), "#1d394b", GOLD)
        _text(d, (88, 196), "OBSERVED", 18, CYAN, True)
        _text(d, (570, 196), "SUPPORTED INFERENCE", 18, GOLD, True)
        if now >= scene.reveal_at:
            if scene.observed_asset_concept:
                _paint_asset(im, scene.observed_asset_concept, "box", 239, 282, 90, CYAN)
            _fit_text(d, (239, 320 if not scene.observed_asset_concept else 360),
                      scene.observed, 300, 25, fg, True, "ma")
        if now >= scene.inference_at:
            span = _progress(now, scene.inference_at, 0.7)
            d.line([(427, 288), (427 + int(106 * span), 288)], fill=GOLD, width=5)
            if scene.inference_asset_concept:
                _paint_asset(im, scene.inference_asset_concept, "box", 721, 282, 90, GOLD)
            _fit_text(d, (721, 320 if not scene.inference_asset_concept else 360),
                      scene.inference, 300, 25, fg, True, "ma")
        return im
    im, d, fg, sub = _base(scene, scene.treatment == "competing")
    revealed = now >= scene.reveal_at
    inferred = now >= scene.inference_at
    if scene.treatment == "prediction" and scene.expected_value is not None:
        high = max(scene.expected_value, scene.observed_value, 1) * 1.12
        d.line([(180, 397), (795, 397)], fill=sub, width=2)
        for x, value, label, color in (
            (350, scene.expected_value, scene.expected, BLUE),
            (610, scene.observed_value, scene.observed, ORANGE),
        ):
            height = (
                205
                * (value / high)
                * (
                    _progress(now, scene.expected_at)
                    if x == 350
                    else _progress(now, scene.reveal_at)
                )
            )
            if height > 1:
                _box(d, (x - 62, 397 - height, x + 62, 397), color, radius=7)
            if x != 350 or now >= scene.expected_at:
                _text(d, (x, 431), label, 21, fg, True, "ma")
            if (x == 350 or revealed) and height > 1:
                _text(d, (x, 380 - height), f"{value:g} {scene.unit}", 20, fg, True, "ma")
            concept = scene.expected_asset_concept if x == 350 else scene.observed_asset_concept
            if concept and (x == 350 or revealed):
                _paint_asset(im, concept, "box", x, 158, 54, color)
    else:
        for x, label, color in ((55, scene.expected, BLUE), (510, scene.observed, ORANGE)):
            if (x == 510 and not revealed) or (x == 55 and now < scene.expected_at):
                continue
            _box(
                d,
                (x, 184, x + 395, 376),
                "#1e394c" if scene.treatment == "competing" else "#e2e9e7",
                color,
            )
            _text(d, (x + 23, 210), "EXPECTED" if x == 55 else "OBSERVED", 18, color, True)
            words = label.split()
            middle = max(1, len(words) // 2)
            for k, line in enumerate((" ".join(words[:middle]), " ".join(words[middle:]))):
                if line:
                    _text(d, (x + 23, 268 + k * 31), line, 25, fg, True)
            concept = scene.expected_asset_concept if x == 55 else scene.observed_asset_concept
            if concept:
                _paint_asset(im, concept, "box", x + 332, 257, 68, color)
    if inferred:
        _box(d, (214, 411, 746, 470), "#f1d49a" if scene.treatment == "competing" else "#dcebdc")
        _text(d, (480, 437), scene.inference, 23, INK, True, "ma")
    return im


def _cutaway(scene: CutawayScene, now: float):
    if scene.treatment == "focal_inset":
        im, d, fg, sub = _base(scene, True)
        _box(d, (66, 145, 480, 455), "#15364b", "#527586")
        bbox = _paint_asset(im, scene.base_asset_concept, scene.symbol, 273, 300, 285, CYAN)
        if now >= scene.opening_at:
            _fit_text(d, (273, 484), scene.opening_label, 350, 27, fg, True, "ma")
        part = scene.parts[0]
        if now >= part.at:
            focus_x = bbox[0] + part.x * bbox[2]
            focus_y = bbox[1] + part.y * bbox[3]
            d.ellipse((focus_x - 26, focus_y - 26, focus_x + 26, focus_y + 26),
                      outline=CYAN, width=4)
            scale = _RENDER_SCALE.get()
            cx, cy = round(focus_x * scale), round(focus_y * scale)
            radius = round(58 * scale)
            crop = im.crop((cx - radius, cy - radius, cx + radius, cy + radius))
            inset_size = round(210 * scale)
            crop = crop.resize((inset_size, inset_size), Image.Resampling.LANCZOS)
            left, top = round(608 * scale), round(188 * scale)
            im.paste(crop, (left, top))
            d = _Draw(im)
            _box(d, (598, 178, 828, 408), None, GOLD)
            d.line([(focus_x + 26, focus_y), (598, 260)], fill=CYAN, width=3)
            _fit_text(d, (714, 484), part.label, 300, 25, GOLD, True, "ma")
        return im
    dark = scene.treatment == "exploded" or scene.base_asset_concept is not None
    im, d, fg, sub = _base(scene, dark)
    if scene.treatment == "guided":
        _box(d, (80, 140, 880, 455), "#15364b" if dark else "#e0e9e7")
        if scene.base_asset_concept:
            subject_box = _paint_asset(
                im, scene.base_asset_concept, scene.symbol, 315, 295, 310, BLUE
            )
            positions = [(610, 160), (610, 259), (610, 358)]
        else:
            subject_box = _paint_asset(im, None, scene.symbol, 480, 290, 235, BLUE)
            positions = [(100, 186), (670, 186), (350, 350)]
        for i, part in enumerate(scene.parts):
            if now < part.at:
                continue
            px, py = positions[i]
            ax = (
                subject_box[0] + part.x * subject_box[2]
                if scene.base_asset_concept
                else 170 + part.x * 620
            )
            ay = (
                subject_box[1] + part.y * subject_box[3]
                if scene.base_asset_concept
                else 203 + part.y * 175
            )
            d.line(
                [(ax, ay), (px if scene.base_asset_concept else px + 90, py + 30)],
                fill=ORANGE if i % 2 else CYAN,
                width=3,
            )
            d.ellipse((ax - 6, ay - 6, ax + 6, ay + 6), fill=ORANGE if i % 2 else CYAN)
            _box(d, (px, py, px + 220, py + 66), PAPER)
            label_x = px + 12
            if part.graphic_visible and part.asset_concept:
                _paint_asset(im, part.asset_concept, part.symbol, px + 30, py + 33, 34, BLUE)
                label_x = px + 53
            _text(d, (label_x, py + 8), part.label, 23, INK, True)
            _text(d, (label_x, py + 37), part.detail, 16, "#47636c")
    else:
        n = len(scene.parts)
        xs = [210 + i * 540 / max(1, n - 1) for i in range(n)]
        for i, (x, part) in enumerate(zip(xs, scene.parts)):
            _box(d, (x - 94, 199, x + 94, 351), "#1d3c50", "#5c8a99")
            if part.graphic_visible:
                _paint_asset(
                    im, part.asset_concept, part.symbol, x, 263, 78,
                    GOLD if now >= part.at else "#6b8492",
                )
            if now >= part.at:
                _text(d, (x, 377), part.label, 23, fg, True, "ma")
                _text(d, (x, 405), part.detail, 18, sub, anchor="ma")
            if i < n - 1:
                d.line(
                    [(x + 98, 277), (xs[i + 1] - 98, 277)],
                    fill=CYAN if now >= scene.parts[i + 1].at else "#3b596b",
                    width=4,
                )
    return im


def _range(scene: RangeScene, now: float):
    dark = scene.treatment == "context"
    im, d, fg, sub = _base(scene, dark)
    p = _progress(now, scene.reveal_at, 0.9)
    if scene.treatment == "scale":
        lo, hi = scene.low, scene.high
        spread = hi - lo
        min_axis = max(0, lo - spread * 0.45)
        max_axis = hi + spread * 0.45

        def to_x(value: float) -> float:
            return 170 + (value - min_axis) / (max_axis - min_axis) * 620

        y = 330
        d.line([(170, y), (790, y)], fill=sub, width=3)
        for v in (lo, hi):
            x = to_x(v)
            d.line([(x, y - 12), (x, y + 12)], fill=sub, width=3)
            _text(d, (x, 369), f"{v:g}", 22, fg, True, "ma")
        if p:
            d.line(
                [(to_x(lo), y - 18), (to_x(lo) + (to_x(hi) - to_x(lo)) * p, y - 18)],
                fill=ORANGE,
                width=14,
            )
        if p:
            _text(d, (480, 226), f"{lo:g}–{hi:g} {scene.unit}", 32, ORANGE, True, "ma")
            _text(d, (480, 445), scene.qualifier, 21, sub, anchor="ma")
        if scene.asset_concept:
            _paint_asset(im, scene.asset_concept, scene.symbol, 190, 221, 78, BLUE)
    else:
        _box(d, (95, 155, 865, 425), "#15364b")
        if scene.asset_concept or scene.symbol != "box":
            _paint_asset(im, scene.asset_concept, scene.symbol, 340, 282, 186, "#7cabb6")
        _text(d, (625, 235), scene.qualifier, 23, GOLD, True, "ma")
        if p:
            _box(d, (522, 303, 816, 379), "#f2dcaf")
            _text(d, (669, 340), f"{scene.low:g}–{scene.high:g} {scene.unit}", 30, INK, True, "ma")
    return im


def _causal(scene: CausalScene, now: float):
    dark = scene.treatment == "mechanism"
    im, d, fg, sub = _base(scene, dark)
    if scene.treatment == "chain":
        xs = [120 + i * 720 / (len(scene.stages) - 1) for i in range(len(scene.stages))]
        for i, (x, stage) in enumerate(zip(xs, scene.stages)):
            active = now >= stage.at
            _box(d, (x - 85, 197, x + 85, 353), "#e0e9e7" if active else "#e9e9e5")
            _paint_step_asset(im, stage, x, 263, 72, CYAN if active else "#91a9ac")
            if active:
                _text(d, (x, 384), stage.label, 23, fg, True, "ma")
                _text(d, (x, 411), stage.detail, 17, sub, anchor="ma")
            if i:
                color = ORANGE if active else "#bcc9c8"
                d.line([(xs[i - 1] + 90, 276), (x - 93, 276)], fill=color, width=5)
                d.polygon([(x - 96, 267), (x - 82, 276), (x - 96, 285)], fill=color)
    else:
        xs = (174, 480, 786)
        for i, (x, stage) in enumerate(zip(xs, scene.stages)):
            active = now >= stage.at
            if i:
                color = CYAN if active else "#365266"
                d.line([(xs[i - 1] + 94, 283), (x - 95, 283)], fill=color, width=5)
                if active:
                    pulse = _progress(now, stage.at, 0.8)
                    px = xs[i - 1] + 94 + pulse * (x - xs[i - 1] - 189)
                    d.ellipse((px - 7, 276, px + 7, 290), fill=GOLD)
            _box(
                d,
                (x - 91, 186, x + 91, 364),
                "#24465a" if active else "#1b3343",
                CYAN if active else "#4a6879",
            )
            _paint_step_asset(im, stage, x, 253, 78, GOLD if active else "#6f8995")
            if active:
                _text(d, (x, 325), stage.label, 24, fg, True, "ma")
                _text(d, (x, 400), stage.detail, 18, sub, anchor="ma")
        _text(d, (480, 448), "CAUSE  →  MEDIATOR  →  EFFECT", 18, sub, True, "ma")
    return im


def _before_after(scene: BeforeAfterScene, now: float):
    dark = scene.treatment == "wipe"
    im, d, fg, sub = _base(scene, dark)
    if scene.treatment == "split":
        for x, item, color in ((75, scene.before, BLUE), (510, scene.after, GREEN)):
            active = now >= item.at
            _box(d, (x, 165, x + 375, 405), "#e2e9e7" if active else "#e8e9e6", color)
            if active:
                _paint_step_asset(
                    im, item, x + 188,
                    237 if scene.visual_relation == "route" else 254,
                    80 if scene.visual_relation == "route" else 112, color,
                )
                if scene.visual_relation == "route":
                    points = (
                        [
                            (x + 47, 312),
                            (x + 95, 283),
                            (x + 181, 310),
                            (x + 268, 283),
                            (x + 328, 312),
                        ]
                        if x == 75
                        else [(x + 47, 312), (x + 328, 312)]
                    )
                    d.line(points, fill=color, width=5, joint="curve")
                    d.ellipse((x + 40, 305, x + 54, 319), fill=color)
                    d.ellipse((x + 321, 305, x + 335, 319), fill=color)
            _text(d, (x + 22, 184), "BEFORE" if x == 75 else "AFTER", 18, color, True)
            if active:
                _text(d, (x + 188, 349), item.label, 27, fg, True, "ma")
                _text(d, (x + 188, 379), item.detail, 18, sub, anchor="ma")
        d.line([(460, 284), (498, 284)], fill=ORANGE, width=5)
        d.polygon([(494, 275), (508, 284), (494, 293)], fill=ORANGE)
        _text(d, (480, 442), scene.change, 21, sub, True, "ma")
    else:
        _box(d, (165, 157, 795, 425), "#213b4c")
        _paint_step_asset(im, scene.before, 480, 267, 220, BLUE)
        if now >= scene.before.at:
            _text(d, (480, 372), scene.before.label, 30, fg, True, "ma")
        p = _progress(now, scene.after.at, 1.25)
        if p:
            overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
            od = _Draw(overlay)
            _box(od, (165, 157, 795, 425), "#e0e9e5")
            _paint_step_asset(overlay, scene.after, 480, 267, 220, GREEN)
            _text(od, (480, 372), scene.after.label, 30, INK, True, "ma")
            edge = int(165 + 630 * p)
            section = overlay.crop(tuple(_px(value) for value in (165, 157, edge, 425)))
            im.paste(section, (_px(165), _px(157)), section)
            d = _Draw(im)
            d.line([(edge, 158), (edge, 424)], fill=GOLD, width=5)
        _text(d, (480, 452), scene.change, 20, sub, True, "ma")
    return im


def _wrapped_card_lines(draw, value: str, width: float, *, size: int,
                        minimum: int, max_lines: int, bold: bool = True):
    """Fit complete words to a card; never clip or silently truncate copy."""
    if not value:
        return size, []
    words = value.split()
    for font_size in range(size, minimum - 1, -1):
        font = _font(font_size, bold)
        lines = []
        current = ""
        for word in words:
            if draw.raw.textbbox((0, 0), word, font=font)[2] > _px(width):
                break
            candidate = f"{current} {word}" if current else word
            if draw.raw.textbbox((0, 0), candidate, font=font)[2] <= _px(width):
                current = candidate
            else:
                lines.append(current)
                current = word
        else:
            if current:
                lines.append(current)
            if len(lines) <= max_lines:
                return font_size, lines
    raise ValueError(f"parallel card copy cannot fit without clipping: {value[:80]}")


def _parallel_card_copy(draw, track: Step, *, y: float, fg: str, sub: str):
    left, right, top, bottom = 183, 440, y - 45, y + 55
    width = right - left - 18
    for label_size in range(23, 15, -1):
        try:
            actual_label_size, labels = _wrapped_card_lines(
                draw, track.label, width, size=label_size,
                minimum=label_size, max_lines=3,
            )
        except ValueError:
            continue
        for detail_size in range(16, 13, -1):
            try:
                actual_detail_size, details = _wrapped_card_lines(
                    draw, track.detail, width, size=detail_size,
                    minimum=detail_size, max_lines=2, bold=False,
                )
            except ValueError:
                continue
            label_height = len(labels) * (actual_label_size + 3)
            detail_height = len(details) * (actual_detail_size + 3)
            gap = 4 if details else 0
            total = label_height + gap + detail_height
            if total > bottom - top - 20:
                continue
            line_y = top + (bottom - top - total) / 2
            for line in labels:
                _text(draw, (left, line_y), line, actual_label_size, fg, True, "lt")
                line_y += actual_label_size + 3
            line_y += gap
            for line in details:
                _text(draw, (left, line_y), line, actual_detail_size, sub, False, "lt")
                line_y += actual_detail_size + 3
            return
    raise ValueError(f"parallel track copy exceeds its card: {track.label[:80]}")


def _parallel_outcome_copy(draw, value: str):
    left, right, top, bottom = 627, 900, 255, 348
    size, lines = _wrapped_card_lines(
        draw, value, right - left - 24, size=24, minimum=16, max_lines=3,
    )
    line_height = size + 3
    if len(lines) * line_height > bottom - top - 18:
        raise ValueError(f"parallel outcome copy exceeds its card: {value[:80]}")
    y = top + (bottom - top - len(lines) * line_height) / 2
    for line in lines:
        _text(draw, ((left + right) / 2, y), line, size, INK, True, "mt")
        y += line_height


def _parallel(scene: ParallelScene, now: float):
    dark = scene.treatment == "converge"
    im, d, fg, sub = _base(scene, dark)
    n = len(scene.tracks)
    if scene.treatment == "swimlanes":
        ys = [185 + i * 94 for i in range(n)]
        for i, (y, track) in enumerate(zip(ys, scene.tracks)):
            active = now >= track.at
            _box(d, (95, y, 865, y + 75), "#e0e9e7" if active else "#e9e9e5")
            _paint_step_asset(
                im, track, 145, y + 38, 50,
                (CYAN, ORANGE, BLUE)[i] if active else "#91a9ac",
            )
            if active:
                _text(d, (194, y + 12), track.label, 25, fg, True)
                _text(d, (194, y + 43), track.detail, 18, sub)
            d.line([(535, y + 38), (800, y + 38)], fill="#bbc9c8", width=4)
            if active:
                d.line(
                    [(535, y + 38), (535 + 265 * _progress(now, track.at, 1.1), y + 38)],
                    fill=(CYAN, ORANGE, BLUE)[i],
                    width=6,
                )
        if now >= scene.outcome_at:
            _box(d, (276, 460, 684, 494), "#dcebdc")
            _text(d, (480, 474), scene.outcome, 20, INK, True, "ma")
    else:
        ys = [190 + i * 110 for i in range(n)]
        for i, (y, track) in enumerate(zip(ys, scene.tracks)):
            active = now >= track.at
            # Connectors stay behind both cards and their copy.
            d.line(
                [(440, y + 5), (620, 300)],
                fill=(CYAN, ORANGE, GREEN)[i] if active else "#365266",
                width=3,
            )
            _box(d, (95, y - 45, 440, y + 55), "#23475b" if active else "#1b3444")
            _paint_step_asset(
                im, track, 143, y + 5, 50,
                (CYAN, ORANGE, GREEN)[i] if active else "#728c96",
            )
            if active:
                _parallel_card_copy(d, track, y=y, fg=fg, sub=sub)
        _box(d, (627, 255, 900, 348), "#ecd7ae" if now >= scene.outcome_at else "#314b5a")
        if now >= scene.outcome_at:
            _parallel_outcome_copy(d, scene.outcome)
    return im


def _journey(scene: JourneyScene, now: float):
    dark = scene.treatment == "route"
    im, d, fg, sub = _base(scene, dark)
    _text(d, (905, 147), "SCHEMATIC • NOT A MAP", 16, sub, True, "ra")
    if scene.treatment == "locator":
        _box(d, (83, 170, 559, 425), "#e0e9e7")
        for x in range(140, 540, 80):
            d.line([(x, 174), (x, 421)], fill="#cad8d6", width=1)
        for y in range(202, 425, 55):
            d.line([(86, y), (555, y)], fill="#cad8d6", width=1)
        coords = [(110 + p.x * 415, 178 + p.y * 234) for p in scene.places]
        for i, (x, y) in enumerate(coords):
            if i and now >= scene.places[i].at:
                d.line([coords[i - 1], (x, y)], fill=CYAN, width=5)
            if now >= scene.places[i].at:
                d.ellipse((x - 10, y - 10, x + 10, y + 10), fill=ORANGE if i else BLUE)
                _text(d, (x + 13, y - 28), scene.places[i].label, 18, INK, True)
        last = max((i for i, p in enumerate(scene.places) if now >= p.at), default=0)
        place = scene.places[last]
        _box(d, (610, 193, 879, 406), "#dcebdc")
        _paint_step_asset(im, place, 745, 270, 100, BLUE)
        _text(d, (745, 365), place.label, 27, INK, True, "ma")
    else:
        _box(d, (79, 169, 881, 429), "#173448")
        coords = [(134 + p.x * 730, 175 + p.y * 240) for p in scene.places]
        for i, (x, y) in enumerate(coords):
            if i and now >= scene.places[i].at:
                d.line([coords[i - 1], (x, y)], fill=CYAN, width=5)
            if now >= scene.places[i].at:
                d.ellipse(
                    (x - 16, y - 16, x + 16, y + 16), fill=ORANGE if i == len(coords) - 1 else GOLD
                )
                _text(d, (x, y - 41), scene.places[i].label, 21, fg, True, "ma")
        if len(coords) >= 2:
            current = max((i for i, p in enumerate(scene.places) if now >= p.at), default=0)
            x, y = coords[current]
            _paint_step_asset(im, scene.places[current], x, y + 46, 45, GOLD)
    return im


def _scale_ladder(scene: ScaleLadderScene, now: float):
    dark = scene.treatment == "zoom"
    im, d, fg, sub = _base(scene, dark)
    if scene.treatment == "bars":
        maximum = scene.items[-1].value
        for i, item in enumerate(scene.items):
            y = 188 + i * 91
            _paint_step_asset(im, item, 112, y + 24, 47, BLUE)
            if now >= item.at:
                _text(d, (147, y - 13), item.label, 22, fg, True)
            _box(d, (148, y + 20, 780, y + 58), "#dbe4e3")
            width = 632 * item.value / maximum * _progress(now, item.at, 0.8)
            if width >= 2:
                _box(d, (148, y + 20, 148 + width, y + 58), (CYAN, BLUE, ORANGE)[i], radius=5)
            if now >= item.at:
                _text(d, (810, y + 26), f"{item.value:g} {scene.unit}", 22, fg, True, "la")
    else:
        active = max((i for i, item in enumerate(scene.items) if now >= item.at), default=0)
        item = scene.items[active]
        _box(d, (244, 172, 716, 418), "#23465a", "#5f8799")
        if now >= item.at:
            _paint_step_asset(im, item, 480, 263, 114, GOLD)
            _text(d, (480, 347), item.label, 28, fg, True, "ma")
            _text(d, (480, 387), f"{item.value:g} {scene.unit}", 29, GOLD, True, "ma")
        for i, past in enumerate(scene.items):
            x = 310 + i * 170
            color = CYAN if i == active else "#567484"
            d.ellipse((x - 10, 446, x + 10, 466), fill=color)
            _text(d, (x, 479), f"{past.value:g}", 18, color, True, "ma")
        if active:
            ratio = item.value / scene.items[active - 1].value
            _text(d, (480, 143), f"{ratio:g}× the previous level", 18, sub, True, "ma")
    return im


def _synthesis(scene: SynthesisScene, now: float):
    dark = scene.treatment == "convergence"
    im, d, fg, sub = _base(scene, dark)
    n = len(scene.inputs)
    if scene.treatment == "mosaic":
        width = 755 / n
        for i, item in enumerate(scene.inputs):
            x = 85 + i * width
            active = now >= item.at
            _box(d, (x, 194, x + width - 18, 382), "#e0e9e7" if active else "#e9e9e5")
            _paint_step_asset(
                im, item, x + (width - 18) / 2, 259, 80,
                (BLUE, CYAN, ORANGE, GREEN)[i],
            )
            if active:
                _fit_text(
                    d, (x + (width - 18) / 2, 341 if item.graphic_visible else 290),
                    item.label, width - 42, 23, fg, True, "ma",
                )
        if now >= scene.takeaway_at:
            _box(d, (205, 412, 755, 474), "#dcebdc")
            _text(d, (480, 441), scene.takeaway, 23, INK, True, "ma")
    else:
        coords = [(210, 218), (745, 218), (210, 394), (745, 394)][:n]
        for i, (item, (x, y)) in enumerate(zip(scene.inputs, coords)):
            active = now >= item.at
            _box(d, (x - 94, y - 45, x + 94, y + 45), "#23465a" if active else "#1b3444")
            _paint_step_asset(
                im, item, x - 50, y, 46, (CYAN, ORANGE, GREEN, GOLD)[i]
            )
            if active:
                _text(d, (x - 18, y), item.label, 20, fg, True, "lm")
            d.line(
                [(x + (70 if x < 480 else -70), y), (480, 303)],
                fill=(CYAN, ORANGE, GREEN, GOLD)[i] if active else "#365266",
                width=3,
            )
        _box(d, (365, 267, 595, 339), "#efd9ad" if now >= scene.takeaway_at else "#304b5b")
        if now >= scene.takeaway_at:
            _text(d, (480, 303), scene.takeaway, 21, INK, True, "ma")
    return im


def _fit_text(draw, xy, value, width, size=24, fill=INK, bold=False, anchor=None):
    font_size = size
    while font_size > 14:
        font = _font(font_size, bold)
        box = draw.raw.textbbox((0, 0), value, font=font)
        if box[2] - box[0] <= _px(width):
            break
        font_size -= 1
    if _STRICT_TEXT_FIT.get():
        box = draw.raw.textbbox((0, 0), value, font=_font(font_size, bold))
        if box[2] - box[0] > _px(width):
            raise ValueError(f"semantic label overflows its renderer slot: {value[:80]}")
    bounds = draw.raw.textbbox(_point(xy), value, font=_font(font_size, bold), anchor=anchor)
    _text(draw, xy, value, font_size, fill, bold, anchor)
    return bounds


def juxtaposition_detail_lines(value: str) -> list[str]:
    """Wrap approved copy into at most two 19px lines in a 320px card slot."""
    words = value.split()
    if not words:
        return []
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    font = _font(19)
    width = _px(320)
    lines: list[str] = []
    current = ""
    for word in words:
        if measure.textbbox((0, 0), word, font=font)[2] > width:
            raise ValueError("juxtaposition supporting copy contains an unwrappable word")
        candidate = f"{current} {word}" if current else word
        if measure.textbbox((0, 0), candidate, font=font)[2] <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    if len(lines) > 2:
        raise ValueError("juxtaposition supporting copy needs more than two readable lines")
    return lines


def _metric_rule_y(text_bounds: tuple[int, int, int, int]) -> int | None:
    """Place decoration beneath the actual glyphs, or omit it if it cannot fit."""
    text_bottom = text_bounds[3] / _RENDER_SCALE.get()
    rule_y = max(391, round(text_bottom + 28))
    return rule_y if rule_y <= 472 else None


def validate_scene_layout(scene: Scene) -> None:
    """Exercise the registered renderer with strict text fit before final video render."""
    def reveal_times(value):
        if isinstance(value, dict):
            at = value.get("at")
            if isinstance(at, (int, float)) and not isinstance(at, bool):
                yield float(at)
            for child in value.values():
                yield from reveal_times(child)
        elif isinstance(value, list):
            for child in value:
                yield from reveal_times(child)

    last = max(0.0, scene.duration - 0.02)
    samples = {0.0, last}
    for at in reveal_times(scene.model_dump(mode="json")):
        if 0 <= at <= last:
            samples.add(min(last, at + 0.02))
    token = _STRICT_TEXT_FIT.set(True)
    try:
        # A middle reveal can overflow before the final frame switches to a
        # shorter label; opening and final frames alone would miss that defect.
        for now in sorted(samples):
            render_frame(scene, now, presentation="production")
    finally:
        _STRICT_TEXT_FIT.reset(token)


def _chronology(scene: ChronologyScene, now: float):
    dark = scene.treatment == "event_cards"
    im, d, fg, sub = _base(scene, dark)
    events = scene.events
    if scene.treatment == "event_track":
        y = 282
        xs = [130 + i * 700 / (len(events) - 1) for i in range(len(events))]
        d.line([(xs[0], y), (xs[-1], y)], fill="#b7c9c7", width=5)
        for i, (x, event) in enumerate(zip(xs, events)):
            if now < event.at:
                continue
            if i:
                d.line([(xs[i - 1], y), (x, y)], fill=CYAN, width=6)
            d.ellipse((x - 13, y - 13, x + 13, y + 13), fill=ORANGE)
            _text(d, (x, 204), event.date, 24, BLUE, True, "ma")
            _fit_text(d, (x, 342), event.label, 190, 24, fg, True, "ma")
            _fit_text(d, (x, 378), event.detail, 190, 17, sub, anchor="ma")
    else:
        width = 755 / len(events)
        for i, event in enumerate(events):
            x = 85 + i * width
            active = now >= event.at
            _box(d, (x, 187, x + width - 15, 408), "#25495b" if active else "#1b3443")
            _text(d, (x + 16, 208), event.date, 26, GOLD if active else sub, True)
            if active:
                _fit_text(d, (x + 16, 286), event.label, width - 48, 27, fg, True)
                _fit_text(d, (x + 16, 339), event.detail, width - 48, 18, sub)
            if i and active:
                d.line([(x - 15, 302), (x - 2, 302)], fill=CYAN, width=5)
    return im


def _geography(scene: GeographyScene, now: float):
    from autonomous_studio.rendering.editorial import map_point, world_map
    from autonomous_studio.rendering.map_motion import Viewport

    dark = scene.treatment == "verified_route"
    im, d, fg, sub = _base(scene, dark)
    rect = (80, 152, 880, 429)
    if scene.treatment == "region_focus":
        region = scene.region
        width = max(30, (region.east - region.west) * 1.45)
        height = max(24, (region.north - region.south) * 1.45)
        viewport = Viewport(
            (region.east + region.west) / 2,
            (region.north + region.south) / 2,
            min(360, width),
            min(180, height),
        )
        overlay = world_map((_px(800), _px(277)), viewport=viewport)
        im.paste(overlay, _point((80, 152)), overlay)
        if now >= region.at:
            x1 = 80 + (0.5 + (region.west - viewport.longitude) / viewport.width) * 800
            x2 = 80 + (0.5 + (region.east - viewport.longitude) / viewport.width) * 800
            y1 = 152 + (0.5 - (region.north - viewport.latitude) / viewport.height) * 277
            y2 = 152 + (0.5 - (region.south - viewport.latitude) / viewport.height) * 277
            d.rounded_rectangle((x1, y1, x2, y2), radius=3, outline=ORANGE, width=5)
            _box(d, (504, 438, 871, 484), "#e6eee9")
            _fit_text(d, (687, 461), region.label, 335, 25, INK, True, "mm")
    else:
        overlay = world_map((_px(800), _px(277)))
        im.paste(overlay, _point((80, 152)), overlay)
        points = [map_point(place.latitude, place.longitude, rect) for place in scene.places]
        if scene.treatment == "verified_route":
            path = [map_point(point.latitude, point.longitude, rect) for point in scene.route_path]
            visible = sum(now >= place.at for place in scene.places)
            if visible >= 2:
                # Show only supplied geometry, never infer an endpoint-to-endpoint shortcut.
                d.line(path, fill=CYAN, width=5)
        for i, (place, (x, y)) in enumerate(zip(scene.places, points)):
            if now < place.at:
                continue
            d.ellipse((x - 10, y - 10, x + 10, y + 10), fill=ORANGE)
            _box(
                d,
                (
                    max(86, min(x - 85, 705)),
                    max(158, y - 56),
                    max(246, min(x + 85, 865)),
                    max(191, y - 20),
                ),
                "#e5eee9",
            )
            _fit_text(d, (x, max(175, y - 39)), place.label, 150, 19, INK, True, "mm")
    if _PRESENTATION.get() == "study":
        note = (
            "Sourced route geometry" if scene.treatment == "verified_route" else "Sourced geography"
        )
        _text(d, (80, 473), note, 16, sub)
    return im


def _concept_contrast(scene: ConceptContrastScene, now: float):
    correction = scene.treatment == "correction_reveal"
    paired = scene.treatment == "paired_contributions"
    im, d, fg, sub = _base(scene, correction)
    for item, x, color in ((scene.left, 79, ORANGE), (scene.right, 508, GREEN)):
        active = now >= item.at
        _box(d, (x, 164, x + 373, 421), "#28495b" if correction else "#e2e9e7", color)
        _paint_step_asset(im, item, x + 185, 259, 106, color)
        if active:
            _fit_text(d, (x + 185, 286 if not item.graphic_visible else 342),
                      item.label, 320, 31, fg, True, "ma")
            detail_y = 362 if not item.graphic_visible else 383
            if scene.treatment == "juxtaposition":
                lines = juxtaposition_detail_lines(item.detail)
                for index, line in enumerate(lines):
                    y = detail_y + (index - (len(lines) - 1) / 2) * 24
                    bounds = _fit_text(d, (x + 185, y), line, 320, 19, sub, anchor="ma")
                    if _STRICT_TEXT_FIT.get() and bounds[3] > _px(415):
                        raise ValueError("juxtaposition supporting copy exceeds card height")
            else:
                _fit_text(d, (x + 185, detail_y), item.detail, 320, 19, sub, anchor="ma")
    if correction:
        if now >= scene.right.at:
            d.line([(130, 202), (392, 385)], fill=ORANGE, width=7)
            d.line([(130, 385), (392, 202)], fill=ORANGE, width=7)
        if now >= scene.resolution_at:
            _box(d, (281, 444, 679, 488), "#d8eadb")
            _fit_text(d, (480, 466), scene.resolution, 365, 22, INK, True, "mm")
    elif paired:
        if now >= scene.right.at:
            d.line([(459, 288), (495, 288)], fill=BLUE, width=4)
            d.ellipse((489, 282, 501, 294), fill=BLUE)
    else:
        _text(d, (480, 285), "≠", 40, BLUE, True, "mm")
    return im


def _quantitative_display(scene: QuantitativeDisplayScene, now: float):
    dark = scene.treatment == "metric_reveal"
    im, d, fg, sub = _base(scene, dark)
    points = scene.points
    maximum = max(point.value for point in points) or 1
    if scene.treatment == "metric_reveal":
        point = points[0]
        if now >= point.at:
            _fit_text(d, (480, 223), point.label, 700, 35, sub, True, "ma")
            value_bounds = _fit_text(
                d, (480, 320), f"{point.value:g} {scene.unit}", 720, 79, GOLD, True, "ma"
            )
            rule_y = _metric_rule_y(value_bounds)
            if rule_y is not None:
                d.line(
                    [(245, rule_y), (245 + 470 * _progress(now, point.at, 1.0), rule_y)],
                    fill=CYAN, width=8,
                )
    elif scene.treatment == "comparable_bars":
        for i, point in enumerate(points):
            y = 170 + i * 79
            if now < point.at:
                continue
            _fit_text(d, (88, y + 18), point.label, 180, 22, fg, True)
            _box(d, (271, y + 4, 737, y + 49), "#dce5e5")
            length = 466 * point.value / maximum * _progress(now, point.at, 0.8)
            if length >= 2:
                _box(d, (271, y + 4, 271 + length, y + 49), (CYAN, BLUE, ORANGE, GREEN)[i])
            _text(d, (754, y + 25), f"{point.value:g} {scene.unit}", 21, fg, True, "lm")
    else:
        coords = [
            (150 + i * 650 / (len(points) - 1), 392 - point.value / maximum * 205)
            for i, point in enumerate(points)
        ]
        d.line([(150, 406), (817, 406)], fill=sub, width=3)
        d.line([(150, 172), (150, 406)], fill=sub, width=3)
        for i, (point, (x, y)) in enumerate(zip(points, coords)):
            if now < point.at:
                continue
            if i:
                d.line([coords[i - 1], (x, y)], fill=CYAN, width=5)
            d.ellipse((x - 8, y - 8, x + 8, y + 8), fill=ORANGE)
            _text(d, (x, 436), point.date, 20, fg, True, "ma")
            _text(d, (x, y - 26), f"{point.value:g}", 18, BLUE, True, "ma")
        _text(d, (857, 219), scene.unit, 17, sub, True, "ra")
    return im


def _document_focus(scene: DocumentFocusScene, now: float):
    dark = scene.treatment == "extracted_finding"
    im, d, fg, sub = _base(scene, dark)
    _box(d, (110, 151, 621 if dark else 850, 440), "#f3f0e9")
    _fit_text(d, (139, 177), scene.document_label, 440, 27, INK, True)
    d.line([(139, 222), (581 if dark else 820, 222)], fill="#b6c6c5", width=2)
    for i in range(4):
        d.line([(139, 259 + i * 36), (562 if dark else 801, 259 + i * 36)], fill="#c6d2cf", width=3)
    if _PRESENTATION.get() == "study":
        _text(d, (139, 405), "ILLUSTRATIVE DOCUMENT VIEW", 15, "#5d737b", True)
    if now >= scene.excerpt_at:
        _box(d, (135, 246, 590 if dark else 814, 298), "#f4dfa8")
        _fit_text(d, (155, 272), scene.excerpt, 420 if dark else 630, 22, INK, True, "lm")
    if now >= scene.finding_at:
        if dark:
            _box(d, (650, 214, 883, 400), "#244a5b", CYAN)
            _fit_text(d, (766, 302), scene.finding, 201, 26, fg, True, "mm")
            d.line([(595, 274), (650, 274)], fill=CYAN, width=5)
        else:
            _box(d, (210, 447, 750, 491), "#dbeadc")
            _fit_text(d, (480, 468), scene.finding, 500, 23, INK, True, "mm")
    return im


def _relationship(scene: RelationshipScene, now: float):
    if scene.treatment == "nested_scope":
        im, d, fg, sub = _base(scene, True)
        boxes = ((405, 281, 555, 384), (298, 218, 663, 425), (190, 154, 770, 467))
        colors = (GOLD, CYAN, BLUE)
        for index, node in enumerate(scene.nodes):
            if now < node.at:
                continue
            box = boxes[index]
            _box(d, box, None, colors[index], radius=18)
            _fit_text(d, ((box[0] + box[2]) / 2, box[1] + 22), node.label,
                      box[2] - box[0] - 24, 22, colors[index], True, "ma")
        return im
    dark = scene.treatment == "network_build"
    im, d, fg, sub = _base(scene, dark)
    nodes = scene.nodes
    if scene.treatment == "hierarchy_build":
        root = next(node for node in nodes if node.parent_id is None)
        children = [node for node in nodes if node.id != root.id]
        root_xy = (480, 211)
        positions = {root.id: root_xy}
        xs = [145 + i * 670 / max(1, len(children) - 1) for i in range(len(children))]
        for node, x in zip(children, xs):
            positions[node.id] = (x, 366)
        for node in children:
            if now >= node.at:
                d.line([positions[node.parent_id], positions[node.id]], fill=CYAN, width=4)
        for node in nodes:
            if now < node.at:
                continue
            x, y = positions[node.id]
            _box(d, (x - 112, y - 42, x + 112, y + 42), "#dbe7e6", BLUE)
            _fit_text(d, (x, y - 4), node.label, 192, 22, INK, True, "mm")
            _fit_text(d, (x, y + 22), node.detail, 192, 15, "#536e76", anchor="mm")
    else:
        positions = [(202, 218), (753, 208), (484, 349), (210, 394), (759, 395)][: len(nodes)]
        coords = dict(zip((node.id for node in nodes), positions))
        active = {node.id for node in nodes if now >= node.at}
        for link in scene.links:
            if link.from_id in active and link.to_id in active:
                d.line([coords[link.from_id], coords[link.to_id]], fill=CYAN, width=4)
        for node in nodes:
            if node.id not in active:
                continue
            x, y = coords[node.id]
            _box(d, (x - 99, y - 40, x + 99, y + 40), "#244b5e", GOLD)
            _fit_text(d, (x, y - 5), node.label, 174, 21, fg, True, "mm")
            _fit_text(d, (x, y + 21), node.detail, 174, 15, sub, anchor="mm")
    return im


def _claim_boundary(scene: ClaimBoundaryScene, now: float):
    im, d, fg, sub = _base(scene, True)
    _text(d, (88, 169), "WHAT IS SUPPORTED", 20, GREEN, True)
    _box(d, (78, 201, 882, 326), "#1d394b", GREEN)
    if now >= scene.claim.at:
        _paint_step_asset(im, scene.claim, 145, 265, 62, GREEN)
        _fit_text(d, (185 if scene.claim.graphic_visible else 105, 263),
                  scene.claim.label, 645 if scene.claim.graphic_visible else 735,
                  36, fg, True, "lm")
    if now >= scene.limit.at:
        extent = int(804 * _progress(now, scene.limit.at, 0.7))
        d.line([(78, 357), (78 + extent, 357)], fill=ORANGE, width=5)
        _text(d, (88, 385), "WHERE THE CLAIM STOPS", 20, ORANGE, True)
        _box(d, (78, 415, 882, 482), "#42303a", ORANGE)
        _fit_text(d, (105, 448), scene.limit.label, 750, 29, fg, True, "lm")
    return im


def _scenario_branch(scene: ScenarioBranchScene, now: float):
    im, d, fg, sub = _base(scene, True)
    _box(d, (207, 158, 753, 261), "#1d394b", GOLD)
    _text(d, (231, 176), "CONDITION / UNCERTAINTY", 17, GOLD, True)
    if now >= scene.condition.at:
        _fit_text(d, (231, 219), scene.condition.label, 490, 27, fg, True, "lm")
    for index, outcome in enumerate((scene.first_outcome, scene.second_outcome)):
        if now < outcome.at:
            continue
        x0 = 72 if index == 0 else 522
        color = CYAN if index == 0 else ORANGE
        _box(d, (x0, 348, x0 + 366, 437), "#1d394b", color)
        _fit_text(d, (x0 + 18, 391), outcome.label, 330, 28, fg, True, "lm")
        endpoint = x0 + 183
        length = _progress(now, outcome.at, 0.8)
        d.line([(480, 261), (480 + (endpoint - 480) * length, 341 * length + 261 * (1 - length))],
               fill=color, width=4)
    if now >= scene.first_outcome.at:
        _text(d, (480, 468), "POSSIBILITIES — LAYOUT DOES NOT SHOW ODDS", 17, sub, True, "ma")
    return im


def _editorial_frame(scene: EditorialFrameScene, now: float):
    dark = scene.treatment != "statement_reveal"
    im, d, fg, sub = _base(scene, dark)
    if scene.treatment == "chapter_marker":
        _box(d, (86, 166, 874, 432), "#24485b")
        _text(d, (125, 205), scene.chapter_label.upper(), 25, CYAN, True)
        d.line([(125, 239), (830, 239)], fill="#4f7888", width=2)
        for i, line in enumerate(scene.lines):
            if now >= line.at:
                _fit_text(d, (125, 286 + i * 57), line.label, 700, 39, fg, True)
    elif scene.treatment == "kinetic_question":
        _text(d, (96, 194), "A QUESTION WORTH FOLLOWING", 22, CYAN, True)
        for i, line in enumerate(scene.lines):
            if now >= line.at:
                _fit_text(d, (480, 270 + i * 70), line.label, 770, 49, fg, True, "mm")
        d.line(
            [(130, 437), (130 + 700 * _progress(now, scene.lines[0].at, 1.2), 437)],
            fill=ORANGE,
            width=7,
        )
    else:
        _box(d, (82, 164, 878, 428), "#e0e9e7", CYAN)
        for i, line in enumerate(scene.lines):
            if now >= line.at:
                _fit_text(
                    d, (480, 239 + i * 74), line.label, 745, 47 if i == 0 else 34, fg, True, "mm"
                )
    return im


def _editorial_close(scene: EditorialCloseScene, now: float):
    if scene.treatment == "community_close":
        return _community_close(scene, now)
    dark = scene.treatment == "loop_bridge"
    im, d, fg, sub = _base(scene, dark)
    if scene.treatment == "recap_grid":
        width = 770 / len(scene.recap)
        for i, item in enumerate(scene.recap):
            x = 89 + i * width
            active = now >= item.at
            _box(d, (x, 166, x + width - 15, 319), "#dce8e6" if active else "#e9ece9")
            if active:
                _fit_text(
                    d, (x + (width - 15) / 2, 240), item.label, width - 38, 25, INK, True, "mm"
                )
        if now >= scene.question_at:
            _fit_text(d, (480, 367), scene.question, 810, 29, fg, True, "ma")
        if now >= scene.invitation_at:
            _box(d, (222, 422, 738, 480), "#d8ebdb")
            _fit_text(d, (480, 451), scene.invitation, 475, 23, INK, True, "mm")
    else:
        for i, item in enumerate(scene.recap):
            if now >= item.at:
                _fit_text(d, (94, 195 + i * 50), item.label, 430, 24, fg, True)
                d.ellipse((63, 185 + i * 50, 79, 201 + i * 50), fill=CYAN)
        if now >= scene.question_at:
            _box(d, (555, 162, 876, 312), "#23485b", GOLD)
            _fit_text(d, (715, 237), scene.question, 278, 24, fg, True, "mm")
        if now >= scene.invitation_at:
            _fit_text(d, (480, 418), scene.invitation, 750, 24, GOLD, True, "ma")
        if now >= scene.bridge_at:
            d.line([(110, 463), (850, 463)], fill=CYAN, width=5)
            _fit_text(d, (480, 488), scene.bridge, 780, 25, fg, True, "ma")
    return im


def _community_question(draw, label: str, x: int) -> None:
    """Draw exact approved copy in a bounded, phone-readable box."""
    for size in range(30, 21, -1):
        font = _font(size, True)
        lines = []
        current = ""
        for word in label.split():
            candidate = f"{current} {word}" if current else word
            bounds = draw.raw.textbbox((0, 0), candidate, font=font)
            if bounds[2] - bounds[0] <= _px(480):
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        if len(lines) <= 3 and all(
            draw.raw.textbbox((0, 0), line, font=font)[2] <= _px(480)
            for line in lines
        ):
            for index, line in enumerate(lines):
                _text(draw, (x, 157 + index * (size + 5)), line, size, "#f7f3ec", True)
            return
    raise ValueError("community question cannot fit its phone-readable text box")


def _community_action_icon(draw, role: str, y: int, x: int) -> None:
    """Simple non-clickable action glyphs, independent of topic and asset APIs."""
    color = GOLD if role == "subscribe" else CYAN
    _box(draw, (x, y - 15, x + (46 if role == "like_share" else 34), y + 19),
         "#23485b", color, radius=7)
    if role == "subscribe":
        draw.polygon([(x + 12, y - 7), (x + 12, y + 11), (x + 26, y + 2)], fill=color)
    elif role == "comment":
        draw.rounded_rectangle((x + 7, y - 7, x + 26, y + 7), radius=4,
                               outline=color, width=2)
        draw.line([(x + 11, y + 7), (x + 9, y + 12), (x + 17, y + 7)],
                  fill=color, width=2)
    elif role == "like_share":
        draw.polygon(
            [(x + 6, y + 9), (x + 6, y - 1), (x + 10, y - 1), (x + 14, y - 9),
             (x + 18, y - 7), (x + 16, y - 1), (x + 23, y - 1), (x + 20, y + 9)],
            outline=color,
        )
        draw.line([(x + 26, y + 8), (x + 39, y - 5), (x + 39, y + 1)],
                  fill=color, width=2)
        draw.line([(x + 33, y - 5), (x + 39, y - 5)], fill=color, width=2)


def _community_close(scene: EditorialCloseScene, now: float):
    if scene.response_options:
        return _community_close_with_choices(scene, now)
    im, d, fg, sub = _base(scene, True)
    # The opposite 320px remain empty for future native YouTube end-screen widgets.
    left = 42 if scene.end_screen_side == "right" else 364
    question = next((item for item in scene.actions if item.role == "comment"), None)
    if question and now >= question.at:
        _box(d, (left, 128, left + 554, 268), "#23485b", CYAN)
        _community_question(d, question.label, left + 26)
    others = [item for item in scene.actions if item.role != "comment"]
    separate_actions = any(item.role in {"like", "share"} for item in others)
    for index, action in enumerate(others):
        if now < action.at:
            continue
        y = (298 if question else 177) + index * 48
        if separate_actions:
            icon = _community_svg_icon(action.role, _px(34), CYAN)
            im.paste(icon, (_px(left + 12), _px(y - 12)), icon)
        else:
            _community_action_icon(d, action.role, y, left + 12)
        _fit_text(d, (left + 74, y + 3), action.label, 364, 25, fg, True, "lm")
    if others and now >= others[0].at and CHANNEL_LOGO.is_file():
        with Image.open(CHANNEL_LOGO) as source:
            logo = source.convert("RGBA")
            logo.thumbnail((_px(72), _px(72)))
            im.paste(logo, (_px(left + 472), _px(331 if question else 211)), logo)
    return im


@lru_cache(maxsize=9)
def _community_svg_icon(name: str, size_px: int, color: str) -> Image.Image:
    """Use the repo's pinned, MIT-licensed Phosphor SVGs; never draw fake UI icons."""
    import resvg_py

    filenames = {
        "like": "thumbs-up.svg",
        "share": "share.svg",
        "subscribe": "user-plus.svg",
    }
    source = Path(__file__).resolve().parents[1] / "assets/semantic/phosphor/regular"
    svg = (source / filenames[name]).read_text(encoding="utf-8")
    svg = svg.replace("currentColor", color)
    return Image.open(io.BytesIO(resvg_py.svg_to_bytes(
        svg_string=svg, width=size_px, height=size_px,
    ))).convert("RGBA")


def _community_close_with_choices(scene: EditorialCloseScene, now: float) -> Image.Image:
    """Optional choice-led close. Older saved scenes retain the original renderer."""
    im = Image.new("RGB", (_px(W), _px(H)), NAVY)
    d = _Draw(im)
    left = 42 if scene.end_screen_side == "right" else 360
    width = 558
    # The other 320 logical pixels stay empty for native YouTube end screens.
    d.rounded_rectangle((left, 40, left + width, 355), radius=20,
                        fill="#152d3c", outline="#315363", width=2)
    d.rounded_rectangle((left, 40, left + 6, 355), radius=2, fill=CYAN)
    _text(d, (left + 25, 64), "YOUR TAKE", 18, GOLD, True)
    question = scene.title.strip()
    words = question.split()
    lines = []
    current = ""
    for word in words:
        trial = f"{current} {word}" if current else word
        if d.raw.textbbox((0, 0), trial, font=_font(36, True))[2] <= _px(485):
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > 2:
        raise ValueError("community question needs a shorter approved screen title")
    for index, line in enumerate(lines):
        _fit_text(d, (left + 25, 100 + 39 * index), line, 485, 36, PAPER, True)
    first_y = 181 if len(lines) == 1 else 207
    option_height = 42 if len(scene.response_options) == 3 else 54
    for index, option in enumerate(scene.response_options):
        if scene.response_option_ats and now < scene.response_option_ats[index]:
            continue
        top = first_y + index * (option_height + 6)
        d.rounded_rectangle((left + 24, top, left + width - 24,
                             top + option_height), radius=11,
                            fill="#224253", outline="#426a77", width=1)
        d.ellipse((left + 38, top + option_height / 2 - 4,
                   left + 46, top + option_height / 2 + 4), fill=CYAN)
        _fit_text(d, (left + 63, top + option_height / 2), option,
                  width - 106, 24, PAPER, True, "lm")
    comment = next((item for item in scene.actions if item.role == "comment"), None)
    if comment and now >= comment.at:
        d.rounded_rectangle((left, 369, left + width, 413), radius=12,
                            fill="#124d5d", outline=CYAN, width=2)
        _fit_text(d, (left + 20, 391), comment.label, width - 40,
                  24, PAPER, True, "lm")
    active = {item.role for item in scene.actions if now >= item.at}
    items = (
        ("like", "Like", "like"),
        ("share", "Share", "share"),
        ("subscribe", "Subscribe", "subscribe"),
    )
    for index, (icon_name, label, role) in enumerate(items):
        if role not in active and not (role in {"like", "share"} and "like_share" in active):
            continue
        x = left + index * 186
        d.rounded_rectangle((x, 438, x + 174, 494), radius=12,
                            fill="#224253", outline="#416574", width=1)
        icon = _community_svg_icon(icon_name, _px(27), GOLD if role == "subscribe" else CYAN)
        im.paste(icon, (_px(x + 16), _px(452)), icon)
        _text(d, (x + 55, 467), label, 22, PAPER, True, "lm")
    if CHANNEL_LOGO.is_file():
        with Image.open(CHANNEL_LOGO) as source:
            logo = ImageOps.fit(source.convert("RGBA"), (_px(64), _px(64)))
        mask = Image.new("L", logo.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, logo.width - 1, logo.height - 1), fill=255)
        im.paste(logo, (_px(left + width - 90), _px(55)), mask)
    return im


def render_frame(
    scene: Scene,
    now: float,
    *,
    size: tuple[int, int] = (W, H),
    presentation: str = "study",
) -> Image.Image:
    from autonomous_studio.rendering.visual_registry import capability

    if size[0] < W or size[1] < H or size[0] * H != size[1] * W:
        raise ValueError("semantic output must be at least 960×540 and exactly 16:9")
    if presentation not in {"study", "production"}:
        raise ValueError("unknown semantic presentation mode")
    token = _RENDER_SCALE.set(size[0] / W)
    presentation_token = _PRESENTATION.set(presentation)
    now_token = _NOW.set(now)
    try:
        renderer = globals()[capability(scene.family).renderer]
        return renderer(scene, now)
    finally:
        _NOW.reset(now_token)
        _PRESENTATION.reset(presentation_token)
        _RENDER_SCALE.reset(token)
