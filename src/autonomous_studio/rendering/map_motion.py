"""Geography-driven Short map camera and overlays; never infers a route or boundary."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Viewport:
    longitude: float
    latitude: float
    width: float
    height: float


WORLD = Viewport(0, 0, 360, 180)


def _wrap_delta(longitude: float, center: float) -> float:
    return (longitude - center + 180) % 360 - 180


def geography_for(shot):
    if shot.geography is not None:
        return shot.geography
    from autonomous_studio.domain.models import MapPoint

    return MapPoint(
        latitude=shot.latitude, longitude=shot.longitude, label=shot.location_label or shot.title
    )


def target_view(geography) -> Viewport:
    if geography.kind == "point":
        return Viewport(geography.longitude, geography.latitude, 72, 40)
    if geography.kind == "region":
        raw_width = geography.east - geography.west
        width = 360 if abs(raw_width) == 360 else raw_width % 360
        center = (geography.west + width / 2 + 180) % 360 - 180
        height = geography.north - geography.south
        return Viewport(
            center,
            (geography.south + geography.north) / 2,
            min(360, max(35, width * 1.35)),
            min(180, max(25, height * 1.35)),
        )
    start = geography.points[0].longitude
    lons = [start + _wrap_delta(point.longitude, start) for point in geography.points]
    lats = [point.latitude for point in geography.points]
    return Viewport(
        (min(lons) + max(lons)) / 2,
        (min(lats) + max(lats)) / 2,
        min(360, max(35, (max(lons) - min(lons)) * 1.5)),
        min(180, max(25, (max(lats) - min(lats)) * 1.5)),
    )


def camera(geography, progress: float, *, motion: str) -> Viewport:
    target = target_view(geography)
    if motion == "hold":
        return target
    # Overview then a slow move into the evidence-backed subject. The source
    # geometry, not a place-name lookup, determines framing anywhere on Earth.
    fraction = min(1.0, max(0.0, (progress - 0.03) / 0.78))
    fraction = fraction * fraction * (3 - 2 * fraction)
    lon = _wrap_delta(target.longitude, WORLD.longitude)
    return Viewport(
        lon * fraction,
        target.latitude * fraction,
        WORLD.width + (target.width - WORLD.width) * fraction,
        WORLD.height + (target.height - WORLD.height) * fraction,
    )


def project(latitude: float, longitude: float, rect, viewport: Viewport):
    x, y, right, bottom = rect
    return (
        x + (0.5 + _wrap_delta(longitude, viewport.longitude) / viewport.width) * (right - x),
        y + (0.5 - (latitude - viewport.latitude) / viewport.height) * (bottom - y),
    )
