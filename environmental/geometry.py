"""Polygon helpers, standard library only.

Coordinates are always (lon, lat), matching GeoJSON. Callers that need
(lat, lon), such as the AppEEARS request, swap explicitly.

A field can be given as a plain ring ([[lon, lat], ...]) or as GeoJSON: a
Polygon (holes allowed), a MultiPolygon, or a Feature wrapping either.
"""

from __future__ import annotations

import math

from .errors import ValidationError

EARTH_RADIUS_M = 6_371_008.8


def _ring(polygon: list) -> list:
    """Drop the duplicated closing vertex if present."""
    if polygon and polygon[0] == polygon[-1]:
        return polygon[:-1]
    return polygon


def _check_ring(ring, where: str) -> list:
    if not isinstance(ring, list) or not all(
        isinstance(p, (list, tuple)) and len(p) >= 2 for p in ring
    ):
        raise ValidationError("a ring is a list of [lon, lat] pairs", where)
    for lon, lat, *_ in ring:
        if (
            isinstance(lon, bool)
            or isinstance(lat, bool)
            or not all(isinstance(v, (int, float)) for v in (lon, lat))
        ):
            raise ValidationError("coordinates must be numbers", where)
        if not (
            math.isfinite(lon)
            and math.isfinite(lat)
            and -180 <= lon <= 180
            and -90 <= lat <= 90
        ):
            raise ValidationError(
                f"coordinate {[lon, lat]} is out of range; use [lon, lat] order", where
            )
    ring = [[float(p[0]), float(p[1])] for p in _ring(ring)]
    if len(ring) < 3:
        raise ValidationError("polygon needs at least 3 distinct vertices", where)
    lons = [p[0] for p in ring]
    if max(lons) - min(lons) > 180:
        raise ValidationError(
            "fields crossing the antimeridian are not supported", where
        )
    return ring


def parts(geom) -> list[tuple[list, list[list]]]:
    """Normalize any accepted field geometry to [(outer ring, [hole rings])]."""
    if isinstance(geom, dict):
        if geom.get("type") == "Feature":
            return parts(geom.get("geometry"))
        if geom.get("type") == "Polygon":
            rings = geom.get("coordinates") or []
            if not rings:
                raise ValidationError("Polygon has no coordinates", "polygon")
            return [
                (
                    _check_ring(rings[0], "polygon"),
                    [_check_ring(h, "polygon.hole") for h in rings[1:]],
                )
            ]
        if geom.get("type") == "MultiPolygon":
            out = []
            for poly in geom.get("coordinates") or []:
                out += parts({"type": "Polygon", "coordinates": poly})
            if not out:
                raise ValidationError("MultiPolygon has no polygons", "polygon")
            return out
        raise ValidationError(
            "polygon must be a ring, or a GeoJSON Polygon, MultiPolygon or Feature",
            "polygon",
        )
    return [(_check_ring(geom, "polygon"), [])]


def bbox(polygon) -> tuple[float, float, float, float]:
    rings = [outer for outer, _ in parts(polygon)]
    lons = [p[0] for r in rings for p in r]
    lats = [p[1] for r in rings for p in r]
    return min(lons), min(lats), max(lons), max(lats)


def point_in_polygon(lon: float, lat: float, polygon: list) -> bool:
    """Ray-casting test. Polygon is a list of [lon, lat] vertices."""
    ring = _ring(polygon)
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def contains(polygon, lon: float, lat: float) -> bool:
    """True if the point is inside any part and outside that part's holes."""
    return any(
        point_in_polygon(lon, lat, outer)
        and not any(point_in_polygon(lon, lat, h) for h in holes)
        for outer, holes in parts(polygon)
    )


def centroid(polygon) -> tuple[float, float]:
    """Mean of the vertices as (lon, lat). Approximate: not area-weighted."""
    ring = parts(polygon)[0][0] if isinstance(polygon, dict) else _ring(polygon)
    lon = sum(p[0] for p in ring) / len(ring)
    lat = sum(p[1] for p in ring) / len(ring)
    return lon, lat


def _ring_area_m2(ring: list, lat0: float) -> float:
    k = math.cos(math.radians(lat0))
    pts = [
        (math.radians(x) * EARTH_RADIUS_M * k, math.radians(y) * EARTH_RADIUS_M)
        for x, y in ring
    ]
    s = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def area_ha(polygon) -> float:
    """Field area in hectares (local equirectangular projection; fine at field scale)."""
    total = 0.0
    for outer, holes in parts(polygon):
        lat0 = sum(p[1] for p in outer) / len(outer)
        total += _ring_area_m2(outer, lat0) - sum(_ring_area_m2(h, lat0) for h in holes)
    return total / 10_000
