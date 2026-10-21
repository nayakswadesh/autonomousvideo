"""Preflight immutable story copy with the same layout used for live shorts.

The asset planner may replace ordinary headlines and annotations, but it cannot
change a shot's comparison items, steps, values, takeaway, or loop hook. Check
those fields before story approval using production geometry and fonts. The
later asset check still validates the actual art-directed display copy.
"""

from autonomous_studio.domain.models import Presentation, ShotLook, VideoProject, VideoSpec
from autonomous_studio.rendering.editorial import caption_sample_progresses, scene


def validate_story_production_layout(spec: VideoSpec) -> None:
    if not spec.shots:
        return
    fixed_titles = {spec.shots[0].id}
    fixed_titles.update(shot.id for shot in spec.shots if shot.kind in {"outro", "source_card"})
    provisional = spec.model_copy(deep=True)
    provisional.presentation = Presentation(
        series_label="",
        shots=[
            ShotLook(
                shot_id=shot.id,
                # Other titles and annotations are owned by the asset planner.
                # Empty values expose the immutable shot fields without
                # prematurely rejecting copy it is allowed to shorten.
                headline=shot.title if shot.id in fixed_titles else "",
                annotation=shot.subtitle if shot.kind == "source_card" else "",
            )
            for shot in spec.shots
        ],
    )
    preview = VideoProject(id="000000000000", spec=provisional, pipeline="live")
    for shot in preview.spec.shots:
        samples = {0.05, 0.25, 0.5, 0.75, 0.95}
        samples.update(caption_sample_progresses(preview, shot))
        for progress in sorted(samples):
            try:
                scene(preview, shot, progress)
            except ValueError as exc:
                raise ValueError(
                    f"{shot.id} production layout at {progress:.2f}: {exc}"
                ) from exc
