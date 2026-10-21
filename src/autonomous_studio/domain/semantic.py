"""Durable, versioned decisions for narration-led long-form diagrams."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SEMANTIC_RATIONALE_MAX_LENGTH = 500


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SemanticDecision(Model):
    beat_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    mode: Literal["keep", "diagram"]
    scene_json: str
    chosen_because: str = Field(min_length=12, max_length=SEMANTIC_RATIONALE_MAX_LENGTH)
    rejected_alternative: str = Field(min_length=8, max_length=300)

    @model_validator(mode="after")
    def coherent(self):
        if self.mode == "diagram" and not self.scene_json.strip():
            raise ValueError("diagram decision needs a scene")
        if self.mode == "keep" and self.scene_json.strip():
            raise ValueError("keep decision cannot smuggle in a scene")
        return self


class SemanticDecisionBatch(Model):
    schema_version: Literal[1, 2] = 1
    chapter_id: str
    decisions: list[SemanticDecision]
