"""Portable, explicit inputs. No URLs are fetched by a manifest or by the renderer."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from autonomous_studio.domain.models import Model, Presentation, Provenance


class MediaInput(Model):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    file: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provenance: Provenance
    prompt_file: str | None = None


class ImageInput(MediaInput):
    kind: Literal["image"] = "image"


class MusicInput(MediaInput):
    kind: Literal["music"] = "music"
    license_url: str = Field(pattern=r"^https://")
    license_evidence_file: str
    trim_start: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def licensed_music(self):
        if self.provenance.license_category != "licensed" or not self.provenance.attribution:
            raise ValueError("library music requires a documented license and attribution")
        return self


Media = Annotated[ImageInput | MusicInput, Field(discriminator="kind")]


class PresentationManifest(Model):
    schema_version: Literal[1] = 1
    presentation: Presentation
    assets: list[Media] = Field(default_factory=list)

    @model_validator(mode="after")
    def references(self):
        ids = [a.id for a in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate media IDs")
        images = {a.id for a in self.assets if a.kind == "image"}
        if any(s.image_ref and s.image_ref not in images for s in self.presentation.shots):
            raise ValueError("presentation references missing image")
        if sum(a.kind == "music" for a in self.assets) > 1:
            raise ValueError("v0.3 supports one library music bed")
        return self
