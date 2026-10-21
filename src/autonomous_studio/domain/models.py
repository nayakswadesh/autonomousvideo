from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from autonomous_studio.domain.formats import get_format_profile, short_profile_id
from autonomous_studio.domain.longform_policy import LongFormCreativePolicy
from autonomous_studio.domain.semantic import SemanticDecisionBatch


def now() -> str:
    return datetime.now(UTC).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class LegacySourceTagModel(Model):
    """Read approved long-form artifacts with obsolete copied source tags."""

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_source_ids(cls, value):
        if isinstance(value, dict) and "source_ids" in value:
            return {key: item for key, item in value.items() if key != "source_ids"}
        return value


class State(StrEnum):
    CREATED = "CREATED"
    LONGFORM_FOUNDATION_READY = "LONGFORM_FOUNDATION_READY"
    LONGFORM_RESEARCHED = "LONGFORM_RESEARCHED"
    TREATMENT_APPROVAL = "TREATMENT_APPROVAL"
    LONGFORM_SCRIPTING = "LONGFORM_SCRIPTING"
    LONGFORM_NARRATIVE_READY = "LONGFORM_NARRATIVE_READY"
    LONGFORM_VISUAL_PLANNING = "LONGFORM_VISUAL_PLANNING"
    VISUAL_PLAN_APPROVAL = "VISUAL_PLAN_APPROVAL"
    LONGFORM_VISUAL_PLAN_READY = "LONGFORM_VISUAL_PLAN_READY"
    LONGFORM_ASSETS_PREPARING = "LONGFORM_ASSETS_PREPARING"
    LONGFORM_SEMANTIC_PLANNING = "LONGFORM_SEMANTIC_PLANNING"
    LONGFORM_AUDIO_PREPARING = "LONGFORM_AUDIO_PREPARING"
    LONGFORM_RUNTIME_APPROVAL = "LONGFORM_RUNTIME_APPROVAL"
    LONGFORM_CHAPTER_RENDERING = "LONGFORM_CHAPTER_RENDERING"
    LONGFORM_REVIEW_PREPARING = "LONGFORM_REVIEW_PREPARING"
    LONGFORM_REVIEW_APPROVAL = "LONGFORM_REVIEW_APPROVAL"
    LONGFORM_MASTER_READY = "LONGFORM_MASTER_READY"
    RESEARCHED = "RESEARCHED"
    STORY_APPROVAL = "STORY_APPROVAL"
    STORYBOARDED = "STORYBOARDED"
    HERO_APPROVAL = "HERO_APPROVAL"
    NARRATION_PREPARING = "NARRATION_PREPARING"
    NARRATION_APPROVAL = "NARRATION_APPROVAL"
    SCRIPT_APPROVAL = "SCRIPT_APPROVAL"
    ASSETS_PREPARING = "ASSETS_PREPARING"
    AUDIO_ASSEMBLING = "AUDIO_ASSEMBLING"
    AUDIO_PREPARED = "AUDIO_PREPARED"
    PROTOTYPE_RENDERED = "PROTOTYPE_RENDERED"
    PROTOTYPE_APPROVAL = "PROTOTYPE_APPROVAL"
    PUBLICATION_CLEARANCE = "PUBLICATION_CLEARANCE"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"


Gate = Literal[
    "story",
    "hero",
    "narration",
    "treatment",
    "script",
    "visual_plan",
    "runtime",
    "prototype",
    "publication",
]
GATES = {
    State.STORY_APPROVAL: "story",
    State.HERO_APPROVAL: "hero",
    State.NARRATION_APPROVAL: "narration",
    State.TREATMENT_APPROVAL: "treatment",
    State.SCRIPT_APPROVAL: "script",
    State.VISUAL_PLAN_APPROVAL: "visual_plan",
    State.LONGFORM_RUNTIME_APPROVAL: "runtime",
    State.LONGFORM_REVIEW_APPROVAL: "prototype",
    State.PROTOTYPE_APPROVAL: "prototype",
    State.PUBLICATION_CLEARANCE: "publication",
}


class Source(Model):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1)
    url: str = Field(pattern=r"^https?://")
    accessed: str
    evidence_notes: str = Field(min_length=1)


class ClaimBase(Model):
    id: str
    text: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    status: Literal["supported", "disputed", "unverified"]
    caveat: str = ""


class NumericClaim(ClaimBase):
    kind: Literal["numeric"] = "numeric"
    value: float
    unit: str
    lower: float | None = None
    upper: float | None = None

    @model_validator(mode="after")
    def bounds(self):
        if self.lower is not None and self.value < self.lower:
            raise ValueError("value is below lower bound")
        if self.upper is not None and self.value > self.upper:
            raise ValueError("value is above upper bound")
        return self


class QualitativeClaim(ClaimBase):
    kind: Literal["qualitative"] = "qualitative"


Claim = Annotated[NumericClaim | QualitativeClaim, Field(discriminator="kind")]


class Ranking(Model):
    metric: str = Field(min_length=1)
    methodology: str = Field(min_length=20)
    scope: str = Field(min_length=1)
    limitations: str = Field(min_length=1)
    direction: Literal["ascending", "descending"] = "descending"


def infer_numeric_ranking_direction(
    values: list[float],
) -> Literal["ascending", "descending"] | None:
    """Return the only monotonic direction established by rank-ordered values."""
    if len(values) < 2:
        return None
    ascending = all(left <= right for left, right in zip(values, values[1:]))
    descending = all(left >= right for left, right in zip(values, values[1:]))
    if ascending == descending:
        return None
    return "ascending" if ascending else "descending"


class Subject(Model):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    title: str
    subtitle: str
    claim_id: str
    narration: str
    rank: int | None = Field(default=None, ge=1)


class ResearchBrief(Model):
    requirements: list[str] = Field(default_factory=list)
    ranking: Ranking | None = None
    sources: list[Source] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    subjects: list[Subject] = Field(default_factory=list)

    @model_validator(mode="after")
    def references(self):
        for collection in (self.sources, self.claims, self.subjects):
            ids = [item.id for item in collection]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate research identifiers")
        sources = {s.id for s in self.sources}
        claims = {c.id: c for c in self.claims}
        for claim in self.claims:
            if not set(claim.source_ids) <= sources:
                raise ValueError(f"missing source for claim {claim.id}")
        for subject in self.subjects:
            if subject.claim_id not in claims:
                raise ValueError(f"missing claim for subject {subject.id}")
        ranks = [s.rank for s in self.subjects if s.rank is not None]
        if ranks:
            if not self.ranking:
                raise ValueError("ranked subjects require an explicit ranking methodology")
            if sorted(ranks) != list(range(1, len(self.subjects) + 1)):
                raise ValueError("ranking must cover all subjects with contiguous unique ranks")
            ordered = sorted(self.subjects, key=lambda s: s.rank)
            numeric = [claims[s.claim_id] for s in ordered]
            if all(isinstance(c, NumericClaim) for c in numeric):
                values = [c.value for c in numeric]
                inferred = infer_numeric_ranking_direction(values)
                if inferred is None:
                    raise ValueError(
                        "numeric claim values do not establish a monotonic ranking; "
                        "revise the metric or document a usable tie-break value"
                    )
                if self.ranking.direction != inferred:
                    best = "smallest" if self.ranking.direction == "ascending" else "largest"
                    raise ValueError(
                        f"ranks disagree with {self.ranking.direction} numeric claim values; "
                        f"rank 1 must have the {best} value"
                    )
        return self


class LongFormResearchQuestion(Model):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    question: str = Field(min_length=10, max_length=240)
    rationale: str = Field(min_length=10, max_length=500)
    required: bool = True


class LongFormStoryQuestion(Model):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    question: str = Field(min_length=10, max_length=240)
    audience_interest: str = Field(min_length=10, max_length=400)
    evidence_to_test: str = Field(min_length=10, max_length=500)
    contrary_evidence_to_check: str = Field(min_length=10, max_length=500)


class LongFormResearchPlan(Model):
    central_question: str = Field(min_length=10, max_length=300)
    questions: list[LongFormResearchQuestion] = Field(min_length=3, max_length=8)
    story_questions: list[LongFormStoryQuestion] = Field(default_factory=list, max_length=4)
    preferred_source_types: list[str] = Field(min_length=1, max_length=6)
    exclusions: list[str] = Field(default_factory=list, max_length=8)
    minimum_distinct_sources: int = Field(default=3, ge=2, le=12)

    @model_validator(mode="after")
    def unique_questions(self):
        ids = [question.id for question in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("long-form research question IDs must be unique")
        story_ids = [question.id for question in self.story_questions]
        if len(story_ids) != len(set(story_ids)):
            raise ValueError("long-form story question IDs must be unique")
        return self


Motion = Literal[
    "fade",
    "push",
    "zoom",
    "spring",
    "reveal",
    "wipe",
    "draw",
    "compress",
    "magnify",
    "parallax",
    "ken_burns",
    "impact",
    "stagger",
    "hold",
]


class VoiceSpec(Model):
    provider: str = "mock"
    voice_id: str = "timing-tone"
    style: str = "calm documentary"
    pace_wpm: int = Field(default=150, ge=80, le=210)
    language: str = "en"
    pronunciation_notes: list[str] = Field(default_factory=list)


class VoiceSegment(Model):
    shot_id: str
    spoken_text: str
    offset: float = Field(default=0.15, ge=0)
    estimated_duration: float = Field(gt=0)
    delivery_direction: str = "calm"
    important_pronunciations: list[str] = Field(default_factory=list)
    pause_after: float = Field(default=0.1, ge=0)


class MusicBeat(Model):
    start: float = Field(ge=0)
    energy: float = Field(ge=0, le=1)


class MusicSpec(Model):
    mood: str = "restrained, reflective"
    energy: str = "low"
    tempo: int = Field(default=72, ge=30, le=200)
    instrumentation: list[str] = Field(default_factory=lambda: ["soft sine pad"])
    structure: list[MusicBeat] = Field(default_factory=list)
    source_type: Literal["procedural", "generated", "library"] = "procedural"
    artifact_ref: str | None = None
    trim_start: float = Field(default=0, ge=0)


class SoundCue(Model):
    shot_id: str
    offset: float = Field(default=0, ge=0)
    cue_type: str = "rank_impact"
    description: str = "restrained low impact"
    intensity: float = Field(default=0.45, ge=0, le=1)
    duration: float = Field(default=0.35, gt=0, le=5)


class AudioMixSpec(Model):
    narration_priority: Literal[True] = True
    target_loudness: float = Field(default=-16, ge=-30, le=-10)
    true_peak_db: float = Field(default=-1.5, ge=-6, le=-1)
    music_ducking: bool = True
    duck_gain_db: float = Field(default=-14, ge=-40, le=0)
    duck_attack_ms: int = Field(default=10, ge=1, le=1000)
    duck_release_ms: int = Field(default=180, ge=1, le=2000)
    voice_gain_db: float = Field(default=0, ge=-20, le=6)
    music_gain_db: float = Field(default=-16, ge=-40, le=0)
    sfx_gain_db: float = Field(default=-10, ge=-40, le=0)
    ambience_gain_db: float = Field(default=-26, ge=-50, le=0)
    fade_in_ms: int = Field(default=200, ge=0, le=2000)
    fade_out_ms: int = Field(default=400, ge=0, le=2000)
    sample_rate: Literal[48000] = 48000


class ShotBase(Model):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    start: float = Field(ge=0)
    duration: float = Field(gt=0)
    title: str
    subtitle: str = ""
    # A short, user-facing account of why this renderable kind fits the spoken
    # idea. Empty keeps previously saved projects readable; live plans require it.
    shot_type_reasoning: str = Field(
        default="",
        max_length=280,
        description=(
            "Brief editorial rationale for this shot kind, grounded in its narration "
            "and visual data."
        ),
    )
    motion: Motion = "reveal"
    claim_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    voice_segments: list[VoiceSegment] = Field(default_factory=list)
    music_state: Literal["bed", "rise", "reduce", "silence"] = "bed"
    sound_cues: list[SoundCue] = Field(default_factory=list)
    intentional_silence: bool = False
    # Exact phrases in the approved narration, ordered to match visible objects.
    # Empty means the legacy shot-start animation is retained.
    entrance_cues: list[str] = Field(default_factory=list)


class TitleShot(ShotBase):
    kind: Literal["kinetic_title"] = "kinetic_title"


class HeroShot(ShotBase):
    kind: Literal["hero_image"] = "hero_image"
    image_ref: str | None = None


class RankShot(ShotBase):
    kind: Literal["rank_reveal"] = "rank_reveal"
    rank: int = Field(ge=1)
    metric: str


class MetricShot(ShotBase):
    kind: Literal["big_metric"] = "big_metric"
    metric: str


class MetricPairShot(ShotBase):
    kind: Literal["metric_pair"] = "metric_pair"
    metrics: list[str] = Field(min_length=2, max_length=2)


class MetricSetShot(ShotBase):
    """Independent measurements, without implying a shared numeric scale."""

    kind: Literal["metric_set"] = "metric_set"
    labels: list[str] = Field(min_length=2, max_length=4)
    display_values: list[str] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def paired(self):
        if len(self.labels) != len(self.display_values):
            raise ValueError("metric labels and displayed values must match")
        if any(not value.strip() for value in self.display_values):
            raise ValueError("metric values must be visible")
        return self


class MapPoint(Model):
    kind: Literal["point"] = "point"
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    label: str = Field(min_length=1)


class MapRegion(Model):
    kind: Literal["region"] = "region"
    west: float = Field(ge=-180, le=180)
    south: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)
    label: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_extent(self):
        if self.west == self.east or self.south >= self.north:
            raise ValueError("region extent must have nonzero width and south < north")
        return self


class MapRoute(Model):
    kind: Literal["route"] = "route"
    points: list[MapPoint] = Field(min_length=2, max_length=12)
    label: str = Field(min_length=1)


MapGeography = Annotated[MapPoint | MapRegion | MapRoute, Field(discriminator="kind")]


class MapShot(ShotBase):
    kind: Literal["map"] = "map"
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    location_label: str = ""
    geography: MapGeography | None = None

    @model_validator(mode="after")
    def complete_location(self):
        if self.geography is None and (self.latitude is None or self.longitude is None):
            raise ValueError("map needs point coordinates or explicit geography")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("legacy map latitude and longitude must appear together")
        return self


class ComparisonShot(ShotBase):
    kind: Literal["comparison"] = "comparison"
    labels: list[str] = Field(min_length=2, max_length=4)
    values: list[float] = Field(min_length=2, max_length=4)
    display_values: list[str] = Field(default_factory=list, max_length=4)
    takeaway: str = ""

    @model_validator(mode="after")
    def paired(self):
        if len(self.labels) != len(self.values):
            raise ValueError("comparison labels and values must match")
        if self.display_values and len(self.display_values) != len(self.values):
            raise ValueError("comparison display values must match plotted values")
        if any(value < 0 for value in self.values):
            raise ValueError("v0.1 bar comparisons require nonnegative values")
        return self


class TimelineShot(ShotBase):
    kind: Literal["timeline"] = "timeline"
    labels: list[str] = Field(min_length=2, max_length=4)


class QualitativeComparisonShot(ShotBase):
    """Competing concepts with no defensible numerical scale."""

    kind: Literal["qualitative_comparison"] = "qualitative_comparison"
    alternatives: list[str] = Field(min_length=2, max_length=3)
    takeaway: str = ""


class StudyDesignShot(ShotBase):
    """One study's setup or observations, not a fabricated chronology."""

    kind: Literal["study_design"] = "study_design"
    steps: list[str] = Field(min_length=2, max_length=4)


class ChartShot(ShotBase):
    kind: Literal["data_chart"] = "data_chart"
    values: list[float] = Field(min_length=2, max_length=20)
    labels: list[str] = Field(default_factory=list, max_length=20)
    display_values: list[str] = Field(default_factory=list, max_length=20)
    takeaway: str = ""

    @model_validator(mode="after")
    def nonnegative(self):
        if self.labels and len(self.labels) != len(self.values):
            raise ValueError("chart labels must match plotted values")
        if self.display_values and len(self.display_values) != len(self.values):
            raise ValueError("chart display values must match plotted values")
        if any(value < 0 for value in self.values):
            raise ValueError("v0.1 bar charts require nonnegative values")
        return self


class SourceShot(ShotBase):
    kind: Literal["source_card"] = "source_card"


class OutroShot(ShotBase):
    kind: Literal["outro"] = "outro"


Shot = Annotated[
    TitleShot
    | HeroShot
    | RankShot
    | MetricShot
    | MetricPairShot
    | MetricSetShot
    | MapShot
    | ComparisonShot
    | TimelineShot
    | QualitativeComparisonShot
    | StudyDesignShot
    | ChartShot
    | SourceShot
    | OutroShot,
    Field(discriminator="kind"),
]


class ShotTypeReview(Model):
    """Advisory comparison; proposed kinds are deliberately not renderable shots."""

    shot_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    suggested_kind: Literal[
        "keep_current", "process_flow", "spatial_cutaway", "before_after", "range_estimate"
    ]
    reason: str = Field(min_length=8)


class VisualStyle(Model):
    background: str = Field(default="#101923", pattern=r"^#[0-9a-fA-F]{6}$")
    foreground: str = Field(default="#f0efe8", pattern=r"^#[0-9a-fA-F]{6}$")
    accent: str = Field(default="#eab878", pattern=r"^#[0-9a-fA-F]{6}$")


class DurationPolicy(Model):
    minimum_seconds: float = Field(default=150, gt=0)
    target_seconds: float = Field(default=210, gt=0)
    maximum_seconds: float = Field(default=240, gt=0)
    expansion_maximum_seconds: float = Field(default=600, gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if not (
            self.minimum_seconds
            <= self.target_seconds
            <= self.maximum_seconds
            <= self.expansion_maximum_seconds
        ):
            raise ValueError(
                "duration policy must satisfy minimum <= target <= maximum <= expansion maximum"
            )
        return self


class EngagementEnding(Model):
    enabled: bool = True
    conclusion_bridge: str = ""
    subscribe_reason: str = "If you enjoy evidence-led stories like this, subscribe."
    comment_question: str = ""
    # Separate spoken actions for closing contract v3. Empty on older saved projects.
    like_prompt: str = ""
    share_prompt: str = ""
    like_and_share_prompt: str = (
        "If this was useful, like the video and share it with someone who would enjoy it."
    )
    suggested_pinned_comment: str = ""
    end_screen_target: str = "best_for_viewer"
    planned_duration_seconds: float = Field(default=12, ge=8, le=20)

    @model_validator(mode="after")
    def enabled_content(self):
        if self.enabled and not self.subscribe_reason.strip():
            raise ValueError("enabled engagement endings require a subscription reason")
        return self


class Brief(Model):
    schema_version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=160)
    topic: str = Field(min_length=1)
    format: Literal["vertical_short"] = "vertical_short"
    production_mode: Literal["prototype", "full"] = "prototype"
    duration: float = Field(default=12, ge=10, le=180)
    expected_rank_count: int | None = Field(default=None, ge=2, le=10)
    aspect_ratio: Literal["9:16"] = "9:16"
    width: int = Field(default=540, ge=180, le=1080)
    height: int = Field(default=960, ge=320, le=1920)
    fps: Literal[24, 30] = 24
    audience: str = "curious adults"
    editorial_objective: str
    hook: str
    thesis: str
    payoff: str
    hook_narration: str = "Look closer at the human impact."
    research: ResearchBrief
    voice: VoiceSpec = Field(default_factory=VoiceSpec)
    music: MusicSpec = Field(default_factory=MusicSpec)
    mix: AudioMixSpec = Field(default_factory=AudioMixSpec)
    visual_style: VisualStyle = Field(default_factory=VisualStyle)
    audio_direction: str = "Respectful; avoid sensational disaster sounds."
    ambience: bool = True
    shots: list[Shot] = Field(default_factory=list)

    @model_validator(mode="after")
    def canvas(self):
        profile = get_format_profile(short_profile_id(self.production_mode))
        profile.validate_duration(self.duration)
        profile.validate_canvas(self.width, self.height, self.fps)
        profile.validate_frame_alignment(self.duration, self.fps)
        if self.shots:
            validate_timeline(self.shots, self.duration, self.research)
        starts = [beat.start for beat in self.music.structure]
        if starts != sorted(set(starts)) or any(start >= self.duration for start in starts):
            raise ValueError("music structure requires unique ordered starts within the timeline")
        return self


class LongFormBrief(Model):
    schema_version: Literal[2] = 2
    title: str = Field(min_length=1, max_length=160)
    topic: str = Field(min_length=1)
    format: Literal["youtube_longform"] = "youtube_longform"
    profile: Literal["longform_pilot"] = "longform_pilot"
    duration_policy: DurationPolicy = Field(default_factory=DurationPolicy)
    aspect_ratio: Literal["16:9"] = "16:9"
    width: Literal[1920] = 1920
    height: Literal[1080] = 1080
    fps: Literal[24, 30] = 24
    audience: str = "curious adults"
    editorial_objective: str = Field(min_length=1)
    viewer_promise: str = Field(min_length=1)
    central_question: str = Field(min_length=1)
    research: ResearchBrief = Field(default_factory=ResearchBrief)
    voice: VoiceSpec = Field(default_factory=VoiceSpec)
    music: MusicSpec = Field(default_factory=MusicSpec)
    mix: AudioMixSpec = Field(default_factory=AudioMixSpec)
    visual_style: VisualStyle = Field(default_factory=VisualStyle)
    audio_direction: str = "Restrained documentary mix; preserve narration clarity."
    ambience: bool = True
    engagement_ending: EngagementEnding = Field(default_factory=EngagementEnding)

    @property
    def duration(self) -> float:
        return self.duration_policy.target_seconds

    @model_validator(mode="after")
    def profile_constraints(self):
        profile = get_format_profile(self.profile)
        if profile.format != self.format or profile.aspect_ratio != self.aspect_ratio:
            raise ValueError("long-form brief does not match its format profile")
        for duration in (
            self.duration_policy.minimum_seconds,
            self.duration_policy.target_seconds,
            self.duration_policy.maximum_seconds,
            self.duration_policy.expansion_maximum_seconds,
        ):
            profile.validate_duration(duration)
            profile.validate_frame_alignment(duration, self.fps)
        profile.validate_canvas(self.width, self.height, self.fps)
        if self.engagement_ending.planned_duration_seconds >= self.duration:
            raise ValueError("engagement ending must be shorter than the target duration")
        return self


ProjectBrief = Annotated[Brief | LongFormBrief, Field(discriminator="format")]


def validate_story_scope(spec: "VideoSpec"):
    """Validate production-level coverage separately from timeline shape."""
    brief = spec.brief
    if brief.production_mode != "full":
        if len(spec.shots) > 6:
            raise ValueError("prototype stories support at most six shots")
        return
    if not 6 <= len(spec.shots) <= 18:
        raise ValueError("full productions require 6–18 shots")
    research = brief.research
    ranked = [s for s in research.subjects if s.rank is not None]
    if brief.expected_rank_count is None and not ranked:
        return
    if research.ranking is None:
        raise ValueError("a full ranked production requires a ranking methodology")
    expected = brief.expected_rank_count or len(ranked)
    if len(ranked) != expected or {s.rank for s in ranked} != set(range(1, expected + 1)):
        raise ValueError("research does not contain the expected complete ranking")
    rank_shots = [s for s in spec.shots if s.kind == "rank_reveal"]
    if len(rank_shots) != expected or [s.rank for s in rank_shots] != list(range(expected, 0, -1)):
        raise ValueError("full countdown must present every rank once, in reverse order")
    by_rank = {s.rank: s for s in ranked}
    for shot in rank_shots:
        if by_rank[shot.rank].claim_id not in shot.claim_ids:
            raise ValueError("rank shot does not reference its ranked subject claim")
    # Sources remain attached to claims and are exported with publication metadata. They do not
    # need to consume the final seconds of a short as an on-screen card.


def validate_timeline(shots: list[Shot], duration: float, research: ResearchBrief):
    cursor = 0.0
    ids = set()
    claims = {c.id for c in research.claims}
    sources = {s.id for s in research.sources}
    for shot in shots:
        if shot.id in ids or abs(shot.start - cursor) > 1e-5:
            raise ValueError("shots require unique IDs and a contiguous, ordered timeline")
        ids.add(shot.id)
        if not set(shot.claim_ids) <= claims or not set(shot.source_ids) <= sources:
            raise ValueError("shot references missing claims or sources")
        if shot.intentional_silence and (shot.voice_segments or shot.sound_cues):
            raise ValueError("intentional silence cannot contain voice or SFX")
        voice_end = 0.0
        for voice in sorted(shot.voice_segments, key=lambda segment: segment.offset):
            if voice.shot_id != shot.id:
                raise ValueError("voice segment references wrong shot")
            # Adjacent decimal timestamps can differ by floating-point noise
            # (for example 0.2 + 6.4 + 0.5 > 7.1). Keep the same tiny
            # tolerance already used for shot boundaries and shot endings.
            if voice.offset < voice_end - 1e-5:
                raise ValueError("voice segments overlap")
            voice_end = voice.offset + voice.estimated_duration + voice.pause_after
            if voice.offset + voice.estimated_duration + voice.pause_after > shot.duration + 1e-5:
                raise ValueError("narration exceeds shot duration; rewrite rather than speed up")
        for cue in shot.sound_cues:
            if cue.shot_id != shot.id or cue.offset + cue.duration > shot.duration:
                raise ValueError("sound cue falls outside its shot")
        cursor += shot.duration
    if abs(cursor - duration) > 1e-5:
        raise ValueError("shots must fill target duration")


class Provenance(Model):
    source: str
    provider: str
    asset_id: str
    model: str | None = None
    license_category: Literal["procedural-original", "licensed", "unknown"] = "unknown"
    usage_restrictions: str = "UNKNOWN — DO NOT PUBLISH"
    attribution: str = ""
    origin: Literal["original", "generated", "library"] = "generated"

    @model_validator(mode="after")
    def unknown_license(self):
        if self.license_category == "unknown":
            self.usage_restrictions = "UNKNOWN — DO NOT PUBLISH"
        return self


class Artifact(Model):
    id: str
    path: str
    role: str
    version: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider: str
    model: str
    input_reference: str
    created_at: str
    latency_seconds: float = Field(default=0, ge=0)
    cost: float | None = Field(default=None, ge=0)
    currency: str = "USD"
    duration: float | None = Field(default=None, gt=0)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    provenance: Provenance


class AudioBase(Model):
    artifact: Artifact
    start: float = Field(ge=0)
    duration: float = Field(gt=0)


class VoiceAsset(AudioBase):
    kind: Literal["voice"] = "voice"
    spoken_text: str
    characters: int = Field(ge=0)
    voice_id: str


class MusicAsset(AudioBase):
    kind: Literal["music"] = "music"
    mood: str


class SfxAsset(AudioBase):
    kind: Literal["sfx"] = "sfx"
    cue_type: str


class AmbienceAsset(AudioBase):
    kind: Literal["ambience"] = "ambience"


AudioAsset = Annotated[
    VoiceAsset | MusicAsset | SfxAsset | AmbienceAsset, Field(discriminator="kind")
]


class ProviderSuccess(Model):
    kind: Literal["success"] = "success"
    artifacts: list[Artifact]
    warnings: list[str] = Field(default_factory=list)


class ProviderFailure(Model):
    kind: Literal["failure"] = "failure"
    provider: str
    message: str
    retryable: bool


ProviderResult = Annotated[ProviderSuccess | ProviderFailure, Field(discriminator="kind")]


class Approval(Model):
    id: str
    project_id: str
    gate: Gate
    version: int
    artifact_ids: list[str]
    created_at: str = Field(default_factory=now)
    decision: Literal["pending", "approve", "revise", "reject", "superseded"] = "pending"
    actor: str | None = None
    message: str = ""
    decided_at: str | None = None


class Feedback(Model):
    gate: Gate | None
    decision: Literal["approve", "revise", "reject"]
    message: str
    actor: str
    timestamp: str = Field(default_factory=now)
    version: int
    approval_id: str | None
    artifact_ids: list[str]
    external_id: str | None = None


class ShotLook(Model):
    shot_id: str
    image_ref: str | None = None
    eyebrow: str = ""
    headline: str | None = None
    annotation: str | None = None
    visual_strategy: Literal[
        "generated_image",
        "archival_image",
        "image_with_metric_overlay",
        "archival_image_with_metric_overlay",
        "image_with_timeline_overlay",
        "archival_image_with_timeline_overlay",
        "image_with_chart_overlay",
        "archival_image_with_chart_overlay",
        "map",
        "chart",
        "diagram",
        "image_with_diagram_overlay",
        "archival_image_with_diagram_overlay",
        "text_only_intentional",
    ] = "text_only_intentional"
    visual_rationale: str = ""


class Presentation(Model):
    theme: Literal["editorial"] = "editorial"
    series_label: str = "FIELD NOTES"
    shots: list[ShotLook] = Field(default_factory=list)
    source_labels: dict[str, str] = Field(default_factory=dict)
    music_credit: str = ""

    @model_validator(mode="after")
    def unique_shots(self):
        ids = [s.shot_id for s in self.shots]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate presentation shot IDs")
        return self


class LoopSpec(Model):
    strategy: Literal["none", "semantic"] = "none"
    bridge_line: str = ""
    match_opening_visual: bool = False

    @model_validator(mode="after")
    def complete(self):
        if self.strategy == "semantic" and not self.bridge_line.strip():
            raise ValueError("a semantic loop requires a complete bridge line")
        if self.strategy == "none" and (self.bridge_line or self.match_opening_visual):
            raise ValueError("loop details require the semantic strategy")
        return self


class VideoSpec(Model):
    schema_version: Literal[1] = 1
    format: Literal["vertical_short"] = "vertical_short"
    brief: Brief
    hook: str = ""
    thesis: str = ""
    payoff: str = ""
    narration_script: str = ""
    narrative_beats: list[str] = Field(default_factory=list)
    shots: list[Shot] = Field(default_factory=list)
    shot_type_reviews: list[ShotTypeReview] = Field(default_factory=list)
    audio_assets: list[AudioAsset] = Field(default_factory=list)
    candidate_hooks: list[str] = Field(default_factory=list)
    presentation: Presentation | None = None
    loop: LoopSpec = Field(default_factory=LoopSpec)
    closed_captions: bool = True


class NarrationBlock(Model):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    text: str = Field(min_length=1)
    estimated_duration: float = Field(gt=0)
    claim_ids: list[str] = Field(default_factory=list)
    delivery_direction: str = "conversational documentary"
    landing_after: Literal["ordinary", "idea_landing", "chapter_turn"] = "ordinary"


class GraphicIntent(Model):
    """What an approved cue's graphic must explain, not an icon or pixel position."""

    mode: Literal["none", "subject", "state"] = "none"
    subject: str = Field(default="", max_length=100)
    state: str = Field(default="", max_length=140)
    visual_change: Literal[
        "none", "fill_up", "fill_down", "expand", "contract", "highlight"
    ] = "none"

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_relation(cls, value):
        # Previously approved plans can still be read; new plans do not request it.
        if isinstance(value, dict) and "relation" in value:
            return {key: item for key, item in value.items() if key != "relation"}
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.mode == "none" and (self.subject or self.state):
            raise ValueError("a text-only cue cannot request a graphic subject or state")
        if self.mode == "none" and self.visual_change != "none":
            raise ValueError("a text-only cue cannot request a graphic change")
        if self.mode != "none" and not self.subject.strip():
            raise ValueError("a graphic intent needs its subject")
        if self.mode == "state" and not self.state.strip():
            raise ValueError("a changing-state graphic needs the spoken state/action")
        if self.mode == "subject":
            # A static subject has no state slot. Keep the raw provider response in
            # its diagnostic artifact, but do not pass this unused text downstream.
            self.state = ""
        if self.mode == "subject" and self.visual_change != "none":
            raise ValueError("a subject-only icon cannot request a state change")
        return self


SPECIAL_COPY_CHAR_LIMITS = {"change": 48, "document_label": 46, "chapter_label": 28}


class ScreenBlueprint(Model):
    """Optional supporting copy and graphics planned alongside the ordered reveal cues."""

    supporting_copy: list[str] = Field(default_factory=list, max_length=8)
    graphic_intents: list[GraphicIntent] = Field(default_factory=list, max_length=8)
    community_roles: list[Literal["comment", "like", "share", "subscribe", "like_share"]] = Field(
        default_factory=list, max_length=4
    )
    # Optional choices named in the approved audience question. Empty preserves
    # existing open-ended and previously approved community closes.
    community_options: list[str] = Field(default_factory=list, max_length=3)
    end_screen_side: Literal["right", "left"] = "right"
    special_copy: dict[Literal["change", "document_label", "chapter_label"], str] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def safe_copy(self):
        if self.community_options and len(self.community_options) not in (2, 3):
            raise ValueError("community close needs two or three spoken response choices")
        if any(not choice.strip() or len(choice) > 44 for choice in self.community_options):
            raise ValueError("community response choice exceeds its readable slot")
        if len({choice.casefold().strip() for choice in self.community_options}) != len(
            self.community_options
        ):
            raise ValueError("community response choices must be distinct")
        for key, value in self.special_copy.items():
            if not value.strip() or len(value) > SPECIAL_COPY_CHAR_LIMITS[key]:
                raise ValueError(f"{key} exceeds its renderer text slot")
        return self


class VisualSceneData(Model):
    """Approved factual values for data-led semantic renderers."""

    values: list[float] = Field(default_factory=list, max_length=5)
    unit: str = Field(default="", max_length=25)
    dates: list[str] = Field(default_factory=list, max_length=5)


class VisualModeAssessment(Model):
    """Compare complete image and designed scenes before choosing a visual mode."""

    image_candidate: str = Field(min_length=20, max_length=350)
    image_fits: bool
    designed_candidate: str = Field(min_length=20, max_length=350)
    designed_necessary: bool
    decision_reason: str = Field(min_length=20, max_length=400)


class VisualBeat(LegacySourceTagModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    chapter_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    start: float = Field(ge=0)
    duration: float = Field(gt=0)
    narration_block_ids: list[str] = Field(default_factory=list)
    spoken_unit_ids: list[str] = Field(default_factory=list)
    role_unit_groups: list[list[str]] = Field(default_factory=list)
    role_semantics: list[str] = Field(default_factory=list)
    visual_type: Literal[
        "authentic_image",
        "generated_image",
        "map",
        "timeline",
        "comparison",
        "data_chart",
        "document",
        "kinetic_title",
        "chapter_card",
        "outro",
    ]
    treatment: Literal[
        "hold",
        "ken_burns",
        "detail_crop",
        "annotated_reveal",
        "parallax",
        "progressive_highlight",
    ] = "hold"
    design_family: str | None = None
    design_treatment: str | None = None
    design_rationale: str | None = Field(default=None, max_length=600)
    mode_assessment: VisualModeAssessment | None = None
    purpose: str = Field(default="", max_length=500)
    screen_title: str = Field(default="", max_length=100)
    hook_headline: str = Field(default="", max_length=72)
    thumbnail_headline: str = Field(default="", max_length=72)
    display_layout: Literal["timeline", "flow", "stack", "hierarchy", "carousel"] | None = None
    asset_ref: str | None = None
    overlay_text: list[str] = Field(default_factory=list, max_length=8)
    narration_anchor: str = Field(default="", max_length=180)
    overlay_anchors: list[str] = Field(default_factory=list, max_length=8)
    screen_blueprint: ScreenBlueprint | None = None
    visual_data: VisualSceneData | None = None
    claim_ids: list[str] = Field(default_factory=list)


class LongFormVisualPolicy(Model):
    minimum_visual_beats: int = Field(default=20, ge=12, le=35)
    maximum_visual_beats: int = Field(default=35, ge=20, le=50)
    maximum_generated_requests: int = Field(default=30, ge=0, le=30)
    maximum_authentic_candidates: int = Field(default=12, ge=0, le=24)
    semantic_diagrams: bool = True

    @model_validator(mode="after")
    def ordered(self):
        if self.minimum_visual_beats > self.maximum_visual_beats:
            raise ValueError("visual policy minimum beats cannot exceed its maximum")
        return self


class AuthenticMediaCandidate(Model):
    kind: Literal["authentic_image"] = "authentic_image"
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    beat_ids: list[str] = Field(min_length=1)
    search_query: str = Field(min_length=3, max_length=300)
    title: str = Field(min_length=1, max_length=500)
    creator: str = Field(min_length=1, max_length=500)
    source_page: str = Field(pattern=r"^https://")
    preview_url: str = Field(pattern=r"^https://")
    license_name: str = Field(min_length=1, max_length=200)
    license_url: str = ""
    attribution: str = Field(min_length=1, max_length=1200)
    modifications_allowed: Literal[True] = True
    context_review_required: Literal[True] = True
    estimated_cost_usd: Literal[0.0] = 0.0


class ImageDirection(Model):
    """One spoken moment, composed as an image rather than a multi-step diagram."""

    spoken_idea: str = Field(min_length=1)
    focal_subject: str = Field(min_length=3, max_length=180)
    visual_moment: str = Field(min_length=8, max_length=300)
    setting: str = Field(min_length=3, max_length=180)
    framing: str = Field(min_length=5, max_length=180)
    focal_placement: str = Field(min_length=4, max_length=100)
    overlay_safe_region: Literal["left", "right", "upper", "lower", "none"]


class GeneratedMediaCandidate(Model):
    kind: Literal["generated_image"] = "generated_image"
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    beat_ids: list[str] = Field(min_length=1)
    prompt: str = Field(min_length=20, max_length=3500)
    direction: ImageDirection | None = None
    provider: str = "openai"
    model: str = "gpt-image-2"
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    disclosure_required: Literal[True] = True


class CodeRenderedCandidate(Model):
    kind: Literal["code_rendered"] = "code_rendered"
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    beat_ids: list[str] = Field(min_length=1)
    renderer: Literal["map", "timeline", "comparison", "data_chart", "document", "title"]
    claim_ids: list[str] = Field(default_factory=list)
    estimated_cost_usd: Literal[0.0] = 0.0


LongFormPlannedAsset = Annotated[
    AuthenticMediaCandidate | GeneratedMediaCandidate | CodeRenderedCandidate,
    Field(discriminator="kind"),
]


class LongFormCostEstimate(Model):
    generated_request_count: int = Field(ge=0)
    authentic_candidate_count: int = Field(ge=0)
    code_rendered_count: int = Field(ge=0)
    known_cost_usd: float = Field(default=0, ge=0)
    unknown_cost_items: list[str] = Field(default_factory=list)
    paid_asset_generation_started: Literal[False] = False


class LongFormRightsPlan(Model):
    status: Literal["documented", "review_required", "blocked"]
    attribution_lines: list[str] = Field(default_factory=list)
    context_review_asset_ids: list[str] = Field(default_factory=list)
    blocked_asset_ids: list[str] = Field(default_factory=list)
    note: str = "Copyright metadata does not replace human context and personality-rights review."


class LongFormVisualPlan(Model):
    schema_version: Literal[1] = 1
    style_direction: str = Field(min_length=20, max_length=1500)
    rationale: str = Field(min_length=20, max_length=1500)
    assets: list[LongFormPlannedAsset] = Field(min_length=1)
    cost: LongFormCostEstimate
    rights: LongFormRightsPlan

    @model_validator(mode="after")
    def unique_assets_and_beats(self):
        ids = [asset.id for asset in self.assets]
        beat_ids = [beat_id for asset in self.assets for beat_id in asset.beat_ids]
        if len(ids) != len(set(ids)):
            raise ValueError("long-form visual asset IDs must be unique")
        if len(beat_ids) != len(set(beat_ids)):
            raise ValueError("every visual beat must use exactly one planned asset")
        if self.rights.blocked_asset_ids and self.rights.status != "blocked":
            raise ValueError("blocked rights assets require blocked plan status")
        return self


class LongFormChapter(Model):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    spoken_unit_version: Literal[1, 2] = 1
    order: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=120)
    purpose: str = Field(min_length=1)
    question: str = Field(min_length=1)
    reveal: str = Field(min_length=1)
    transition: str = ""
    target_duration: float = Field(gt=0)
    evidence_claim_ids: list[str] = Field(default_factory=list)
    essential_claim_ids: list[str] = Field(default_factory=list)
    evidence_source_ids: list[str] = Field(default_factory=list)
    narration_blocks: list[NarrationBlock] = Field(default_factory=list)
    visual_beats: list[VisualBeat] = Field(default_factory=list)
    audio_direction: str = ""

    @model_validator(mode="after")
    def coherent_timeline(self):
        block_ids = [block.id for block in self.narration_blocks]
        beat_ids = [beat.id for beat in self.visual_beats]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("chapter narration block IDs must be unique")
        if len(beat_ids) != len(set(beat_ids)):
            raise ValueError("chapter visual beat IDs must be unique")
        if any(beat.chapter_id != self.id for beat in self.visual_beats):
            raise ValueError("visual beat references the wrong chapter")
        known_blocks = set(block_ids)
        if any(not set(beat.narration_block_ids) <= known_blocks for beat in self.visual_beats):
            raise ValueError("visual beat references a missing narration block")
        if self.visual_beats:
            cursor = 0.0
            for beat in sorted(self.visual_beats, key=lambda item: item.start):
                if abs(beat.start - cursor) > 1e-5:
                    raise ValueError("chapter visual beats must form a contiguous timeline")
                cursor += beat.duration
            if abs(cursor - self.target_duration) > 1e-5:
                raise ValueError("chapter visual beats must fill the chapter duration")
        return self


def hashable_longform_chapter(chapter: LongFormChapter) -> dict:
    """Keep old approved hashes stable when the new landing cue has its implicit default."""
    payload = chapter.model_dump(mode="json")
    for block in payload.get("narration_blocks", []):
        if block.get("landing_after") == "ordinary":
            block.pop("landing_after")
    return payload


class LongFormNarrative(Model):
    viewer_promise: str = ""
    central_question: str = ""
    thesis: str = ""
    hook: str = ""
    conclusion: str = ""
    next_video_bridge: str = ""


class LongFormStoryDesign(Model):
    central_tension: str = Field(min_length=15, max_length=500)
    payoff: str = Field(min_length=15, max_length=500)


class LongFormStoryChoice(Model):
    question_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    supporting_claim_ids: list[str] = Field(min_length=1)
    evidence_rationale: str = Field(min_length=20, max_length=700)
    limitation: str = Field(default="", max_length=500)


class LongFormEditorialDimension(Model):
    status: Literal["pass", "warning", "error"]
    finding: str = Field(min_length=10, max_length=500)
    chapter_ids: list[str] = Field(default_factory=list)


class LongFormEditorialReview(Model):
    factual_integrity: LongFormEditorialDimension
    promise_payoff: LongFormEditorialDimension
    newcomer_clarity: LongFormEditorialDimension
    narrative_momentum: LongFormEditorialDimension


class LongFormContinuityIssue(Model):
    code: str = Field(pattern=r"^[a-z0-9_]+$")
    message: str = Field(min_length=1, max_length=500)
    chapter_ids: list[str] = Field(default_factory=list)
    severity: Literal["warning", "error"] = "error"


class LongFormContinuityReport(Model):
    passed: bool
    total_words: int = Field(ge=0)
    estimated_duration_seconds: float = Field(ge=0)
    target_timeline_seconds: float = Field(default=0, ge=0)
    proposed_timeline_seconds: float = Field(default=0, ge=0)
    duration_status: Literal["within_target", "expansion_proposed"] = "within_target"
    covered_claim_ids: list[str] = Field(default_factory=list)
    editorial_review: LongFormEditorialReview | None = None
    issues: list[LongFormContinuityIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def result_matches_issues(self):
        has_errors = any(issue.severity == "error" for issue in self.issues)
        if self.passed == has_errors:
            raise ValueError("continuity pass must match its error issues")
        return self


class LongFormRuntimeProposal(Model):
    version: int = Field(ge=1)
    original_seconds: float = Field(gt=0)
    prior_ceiling_seconds: float = Field(gt=0)
    measured_speech_seconds: float = Field(gt=0)
    measured_minimum_seconds: float = Field(gt=0)
    proposed_seconds: float = Field(gt=0)
    chapter_minimums: dict[str, float]
    reason: str = "Measured narration exceeds the project's planning ceiling."

    @model_validator(mode="after")
    def extension_is_meaningful(self):
        if not self.proposed_seconds > max(
            self.prior_ceiling_seconds, self.measured_minimum_seconds
        ):
            raise ValueError("runtime proposal must exceed the ceiling and measured speech")
        get_format_profile("longform_pilot").validate_duration(self.proposed_seconds)
        return self


class LongFormSpec(Model):
    schema_version: Literal[2] = 2
    story_contract_version: Literal[1, 2] = 1
    closing_contract_version: Literal[1, 2, 3] = 1
    format: Literal["youtube_longform"] = "youtube_longform"
    brief: LongFormBrief
    narrative: LongFormNarrative = Field(default_factory=LongFormNarrative)
    story_design: LongFormStoryDesign | None = None
    story_choice: LongFormStoryChoice | None = None
    candidate_hooks: list[str] = Field(default_factory=list, max_length=4)
    treatment_summary: str = ""
    chapters: list[LongFormChapter] = Field(default_factory=list)
    audio_assets: list[AudioAsset] = Field(default_factory=list)
    engagement_ending: EngagementEnding | None = None
    closed_captions: bool = False

    @model_validator(mode="after")
    def chapter_scope(self):
        if not self.chapters:
            return self
        if self.story_contract_version == 2:
            if self.story_design is None:
                raise ValueError("story-first long-form treatment needs a story design")
            if any(not chapter.essential_claim_ids for chapter in self.chapters):
                raise ValueError("story-first chapters need designated essential claims")
        ids = [chapter.id for chapter in self.chapters]
        orders = [chapter.order for chapter in self.chapters]
        if len(ids) != len(set(ids)):
            raise ValueError("long-form chapter IDs must be unique")
        if orders != list(range(1, len(self.chapters) + 1)):
            raise ValueError("long-form chapters must use contiguous display order")
        if abs(sum(c.target_duration for c in self.chapters) - self.brief.duration) > 1e-5:
            raise ValueError("chapter durations must fill the long-form target duration")
        claim_sources = {
            claim.id: set(claim.source_ids) for claim in self.brief.research.claims
        }
        claims = set(claim_sources)
        sources = {source.id for source in self.brief.research.sources}
        narration_ids: set[str] = set()
        visual_ids: set[str] = set()
        scripted = [bool(chapter.narration_blocks) for chapter in self.chapters]
        if any(scripted) and not all(scripted):
            raise ValueError("long-form scripting must cover every approved chapter")
        for chapter in self.chapters:
            if not set(chapter.evidence_claim_ids) <= claims:
                raise ValueError("chapter references missing research claims")
            if not set(chapter.essential_claim_ids) <= set(chapter.evidence_claim_ids):
                raise ValueError("essential chapter claims must belong to its evidence ledger")
            if self.story_contract_version == 2:
                if len(chapter.essential_claim_ids) != len(set(chapter.essential_claim_ids)):
                    raise ValueError("essential chapter claim IDs must be unique")
                if any(
                    not claim_sources[claim_id] & set(chapter.evidence_source_ids)
                    for claim_id in chapter.essential_claim_ids
                ):
                    raise ValueError("essential chapter claims need matching reviewed sources")
            if not set(chapter.evidence_source_ids) <= sources:
                raise ValueError("chapter references missing research sources")
            for block in chapter.narration_blocks:
                if block.id in narration_ids or not set(block.claim_ids) <= claims:
                    raise ValueError("narration blocks require unique IDs and valid claims")
                narration_ids.add(block.id)
            for beat in chapter.visual_beats:
                if beat.id in visual_ids:
                    raise ValueError("visual beat IDs must be unique across the project")
                if not set(beat.claim_ids) <= claims:
                    raise ValueError("visual beat references missing research evidence")
                visual_ids.add(beat.id)
        return self


ProjectSpec = Annotated[VideoSpec | LongFormSpec, Field(discriminator="format")]


class PublicationPackage(Model):
    title: str
    description: str
    hashtags: list[str] = Field(max_length=3)
    title_artifact_id: str
    description_artifact_id: str
    metadata_artifact_id: str
    thumbnail_artifact_id: str
    usage_artifact_id: str
    release_status_artifact_id: str
    usage_summary: str = ""
    rights_summary: str = ""


class YouTubeUpload(Model):
    id: str
    project_version: int = Field(ge=1)
    status: Literal["uploading", "uploaded", "uploaded_thumbnail_failed", "ambiguous"]
    started_at: str = Field(default_factory=now)
    completed_at: str | None = None
    video_id: str | None = None
    channel_id: str | None = None
    privacy_status: Literal["private"] = "private"
    made_for_kids: Literal[False] = False
    thumbnail_set: bool = False
    source_sha256: dict[str, str] = Field(default_factory=dict)
    receipt_artifact_id: str | None = None
    message: str = ""


class PreparedVoice(Model):
    shot_id: str
    segment_index: int = Field(ge=0)
    signature: str
    asset: VoiceAsset


class AutoAssetPolicy(Model):
    image_model: str = "gpt-image-2"
    image_quality: Literal["low", "medium", "high"] = "medium"
    image_size: Literal["864x1536"] = "864x1536"
    max_image_requests: int = Field(default=2, ge=1, le=18)
    music_catalog: str


class ImageBrief(Model):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", max_length=50)
    prompt: str = Field(min_length=20, max_length=3500)
    shot_ids: list[str] = Field(min_length=1, max_length=18)
    source_type: Literal["generated", "archival"] = "generated"
    search_query: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def source_requirements(self):
        if self.source_type == "archival" and len(self.search_query.strip()) < 3:
            raise ValueError("archival images require a concrete search query")
        if self.source_type == "generated" and self.search_query:
            raise ValueError("generated images cannot contain an archival search query")
        return self


class AssetPlan(Model):
    series_label: str = Field(min_length=1, max_length=35)
    images: list[ImageBrief] = Field(min_length=1, max_length=18)
    music_tags: list[Literal["reflective", "tense", "uplifting", "neutral", "ambient"]] = Field(
        min_length=1, max_length=3
    )
    music_mood: str = Field(default="restrained documentary", min_length=3, max_length=120)
    music_energy: Literal["low", "medium", "high"] = "low"
    music_tempo_bpm: int = Field(default=72, ge=30, le=200)
    rationale: str = Field(min_length=10, max_length=1000)
    display: list[ShotLook] = Field(min_length=1, max_length=18)

    @model_validator(mode="after")
    def unique(self):
        ids = [i.id for i in self.images]
        shots = [s for image in self.images for s in image.shot_ids]
        if len(ids) != len(set(ids)) or len(shots) != len(set(shots)):
            raise ValueError("asset plan contains duplicate image or shot IDs")
        return self


class GeneratedVisual(Model):
    image_id: str
    fingerprint: str
    artifact: Artifact
    source_type: Literal["generated", "archival"] = "generated"
    original_artifact_id: str | None = None


class VideoProject(Model):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-f0-9]{12}$")
    created_at: str = Field(default_factory=now)
    state: State = State.CREATED
    revision: int = 0
    version: int = 1
    spec: ProjectSpec
    artifacts: list[Artifact] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)
    feedback: list[Feedback] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    last_error: str | None = None
    pipeline: Literal["mock", "live"] = "mock"
    live_models: dict[str, str] = Field(default_factory=dict)
    prepared_voices: list[PreparedVoice] = Field(default_factory=list)
    timing_repairs: int = Field(default=0, ge=0)
    timing_repair_note: str = ""
    last_error_code: str | None = None
    review_policy: Literal["staged", "preview"] = "staged"
    timing_rewrite_attempts: int = Field(default=0, ge=0)
    production_changes: list[str] = Field(default_factory=list)
    auto_assets: AutoAssetPolicy | None = None
    asset_plan: AssetPlan | None = None
    asset_plan_attempts: int = Field(default=0, ge=0)
    generated_visuals: list[GeneratedVisual] = Field(default_factory=list)
    image_requests: int = Field(default=0, ge=0)
    publication_clearance_enabled: bool = False
    publication: PublicationPackage | None = None
    youtube_uploads: list[YouTubeUpload] = Field(default_factory=list)
    script_approval_enabled: bool = False
    creation_settings: dict[str, object] = Field(default_factory=dict)
    longform_research_plan: LongFormResearchPlan | None = None
    longform_continuity: LongFormContinuityReport | None = None
    longform_visual_policy: LongFormVisualPolicy | None = None
    longform_creative_policy: LongFormCreativePolicy | None = None
    longform_visual_plan: LongFormVisualPlan | None = None
    longform_visual_plan_attempts: int = Field(default=0, ge=0)
    longform_media: dict[str, Artifact] = Field(default_factory=dict)
    longform_voices: dict[str, VoiceAsset] = Field(default_factory=dict)
    longform_word_timings: dict[str, list[dict]] = Field(default_factory=dict)
    longform_semantic_decisions: dict[str, SemanticDecisionBatch] = Field(default_factory=dict)
    longform_semantic_hashes: dict[str, str] = Field(default_factory=dict)
    longform_semantic_models: dict[str, str] = Field(default_factory=dict)
    longform_runtime_proposal: LongFormRuntimeProposal | None = None
    longform_runtime_approved_seconds: float | None = Field(default=None, gt=0)
    longform_chapters: dict[str, Artifact] = Field(default_factory=dict)
    longform_chapter_hashes: dict[str, str] = Field(default_factory=dict)
    longform_music: Artifact | None = None
    longform_image_requests: int = Field(default=0, ge=0)
    short_runtime_proposal_seconds: float | None = Field(default=None, ge=90, le=180)
    short_runtime_approved_seconds: float | None = Field(default=None, ge=90, le=180)
    short_revision_script: str = ""

    @model_validator(mode="before")
    @classmethod
    def migrate_spec_discriminator(cls, value):
        """Keep project rows written before format-profile support loadable."""
        if isinstance(value, dict):
            spec = value.get("spec")
            if isinstance(spec, dict) and "format" not in spec:
                brief = spec.get("brief")
                brief_format = brief.get("format") if isinstance(brief, dict) else None
                spec["format"] = brief_format or "vertical_short"
        return value

    def current_artifacts(self, role: str | None = None) -> list[Artifact]:
        return [
            a
            for a in self.artifacts
            if a.version == self.version and (role is None or a.role == role)
        ]

    def pending(self) -> Approval | None:
        return next(
            (
                a
                for a in reversed(self.approvals)
                if a.version == self.version and a.decision == "pending"
            ),
            None,
        )
