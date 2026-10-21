from pathlib import Path
from typing import Protocol

from autonomous_studio.domain.models import (
    Artifact,
    AudioAsset,
    Brief,
    LongFormBrief,
    LongFormContinuityReport,
    LongFormResearchPlan,
    LongFormSpec,
    LongFormVisualPolicy,
    MusicSpec,
    ProviderResult,
    ResearchBrief,
    Shot,
    SoundCue,
    VideoProject,
    VideoSpec,
    VoiceSegment,
    VoiceSpec,
)


class ResearchProvider(Protocol):
    async def research(self, brief: Brief) -> ResearchBrief: ...


class DirectorProvider(Protocol):
    async def develop(self, brief: Brief, feedback: list[str]) -> VideoSpec: ...


class LongFormResearchProvider(Protocol):
    async def plan(self, brief: LongFormBrief) -> LongFormResearchPlan: ...

    async def research(self, brief: LongFormBrief, plan: LongFormResearchPlan) -> ResearchBrief: ...


class LongFormDirectorProvider(Protocol):
    async def develop_treatment(
        self, brief: LongFormBrief, plan: LongFormResearchPlan, feedback: list[str],
        *, story_contract_version: int = 1,
    ) -> LongFormSpec: ...

    async def write_script(
        self, spec: LongFormSpec, feedback: list[str]
    ) -> tuple[LongFormSpec, LongFormContinuityReport]: ...


class LongFormVisualPlannerProvider(Protocol):
    async def plan(self, spec: LongFormSpec, policy: LongFormVisualPolicy, feedback: list[str]): ...


class AuthenticMediaDiscoveryProvider(Protocol):
    async def discover(self, request, spec: LongFormSpec): ...


class ImageProvider(Protocol):
    async def generate(self, project: VideoProject, shot: Shot, output: Path) -> Artifact: ...


class VoiceProvider(Protocol):
    async def synthesize(
        self,
        project: VideoProject,
        segment: VoiceSegment,
        spec: VoiceSpec,
        start: float,
        output: Path,
    ) -> AudioAsset: ...


class MusicProvider(Protocol):
    async def create_or_select(
        self, project: VideoProject, spec: MusicSpec, output: Path
    ) -> AudioAsset: ...


class SfxProvider(Protocol):
    async def create(
        self, project: VideoProject, cue: SoundCue, start: float, output: Path
    ) -> AudioAsset: ...


class Renderer(Protocol):
    async def render(self, project: VideoProject, output: Path) -> ProviderResult: ...
