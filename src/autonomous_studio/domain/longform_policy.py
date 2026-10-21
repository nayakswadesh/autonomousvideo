"""Versioned, long-form-only limits for creative validation repairs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CREATIVE_STAGES = (
    "research_plan",
    "research_conversion",
    "treatment",
    "script",
    "visual_partition",
    "visual_chapter",
    "spoken_anchor",
    "semantic_decision",
    "semantic_scene",
)


class LongFormCreativePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1, 2] = 2
    repair_limits: dict[str, int] = Field(
        default_factory=lambda: {stage: 0 for stage in CREATIVE_STAGES}
    )

    @model_validator(mode="after")
    def complete(self):
        expected = set(CREATIVE_STAGES)
        if self.schema_version == 1:
            expected.remove("visual_partition")
        if set(self.repair_limits) != expected:
            raise ValueError("creative repair policy must cover every long-form stage")
        if any(not 0 <= limit <= 3 for limit in self.repair_limits.values()):
            raise ValueError("creative repair limits must be between 0 and 3")
        return self


def creative_attempts(client, stage: str, *, legacy: int = 2) -> int:
    """Older projects retain their saved behavior; new projects pin explicit limits."""
    policy = getattr(client, "longform_creative_policy", None)
    return 1 + policy.repair_limits[stage] if policy is not None else legacy


def tag_last_trace(client, **identifiers: str) -> None:
    """Attach context when the provider records requests; mocks need not expose a trace."""
    trace = getattr(client, "trace", None)
    if trace:
        trace[-1].update(identifiers)
