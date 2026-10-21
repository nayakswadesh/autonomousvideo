from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FormatProfile:
    """Immutable production constraints shared by schemas, workflow and renderers."""

    id: str
    format: str
    aspect_ratio: str
    minimum_seconds: float
    maximum_seconds: float
    supported_canvases: tuple[tuple[int, int], ...]
    supported_fps: tuple[int, ...] = (24, 30)
    chaptered: bool = False

    def validate_duration(self, duration: float) -> None:
        if not self.minimum_seconds <= duration <= self.maximum_seconds:
            raise ValueError(
                f"{self.id} duration must be between "
                f"{self.minimum_seconds:g} and {self.maximum_seconds:g} seconds"
            )

    def validate_canvas(self, width: int, height: int, fps: int) -> None:
        if (width, height) not in self.supported_canvases:
            canvases = ", ".join(f"{w}x{h}" for w, h in self.supported_canvases)
            raise ValueError(f"{self.id} canvas must be one of: {canvases}")
        if width % 2 or height % 2:
            raise ValueError("canvas dimensions must be even")
        if fps not in self.supported_fps:
            choices = ", ".join(str(value) for value in self.supported_fps)
            raise ValueError(f"{self.id} frame rate must be one of: {choices}")

    def validate_frame_alignment(self, duration: float, fps: int) -> None:
        if abs(duration * fps - round(duration * fps)) > 1e-6:
            raise ValueError("duration must align to a video frame")


SHORT_CANVASES = tuple((width, width * 16 // 9) for width in range(180, 1081, 18))


FORMAT_PROFILES: dict[str, FormatProfile] = {
    "short_prototype": FormatProfile(
        id="short_prototype",
        format="vertical_short",
        aspect_ratio="9:16",
        minimum_seconds=10,
        maximum_seconds=15,
        supported_canvases=SHORT_CANVASES,
    ),
    "short_full": FormatProfile(
        id="short_full",
        format="vertical_short",
        aspect_ratio="9:16",
        minimum_seconds=45,
        # 60 seconds is the editorial target. The measured timeline may grow to 90
        # automatically, or up to 180 after an explicit runtime review.
        maximum_seconds=180,
        supported_canvases=SHORT_CANVASES,
    ),
    "longform_pilot": FormatProfile(
        id="longform_pilot",
        format="youtube_longform",
        aspect_ratio="16:9",
        minimum_seconds=150,
        # Operational safety limit, not an editorial recommendation or YouTube limit.
        # The default project policy still requests human approval above 300 seconds.
        maximum_seconds=1200,
        supported_canvases=((1920, 1080),),
        chaptered=True,
    ),
}


def get_format_profile(profile_id: str) -> FormatProfile:
    try:
        return FORMAT_PROFILES[profile_id]
    except KeyError as exc:
        raise ValueError(f"unknown format profile: {profile_id}") from exc


def short_profile_id(production_mode: str) -> str:
    if production_mode not in {"prototype", "full"}:
        raise ValueError(f"unknown short production mode: {production_mode}")
    return f"short_{production_mode}"
