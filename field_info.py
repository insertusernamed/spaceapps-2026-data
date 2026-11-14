"""Quick facts about one field outline, computed locally from its geometry.

No network calls: every value comes from the outline itself, in about a tenth of a
millisecond per field, so the facts can travel with every outline served by
/fields. Distances use a local equirectangular projection centred on the field,
accurate to a fraction of a percent at field scale.
"""
from __future__ import annotations

import math
import re

import numpy as np

EARTH_RADIUS_M = 6371008.8
METRES_PER_DEGREE = math.radians(1) * EARTH_RADIUS_M
HECTARES_PER_ACRE = 0.40468564224
_SOURCE_YEAR = re.compile(r"^ftw-(\d{4})-")
LONG_EDGES = 4  # longest outer edges tried as rectangle directions, besides the main axis


def _parts(geometry: dict) -> list:
    """Polygon parts as [[outer, hole, ...], ...]."""
    return [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]


def describe_many(features: list[dict], hectares: list[float | None] | None = None) -> list[dict]:
    """Area, shape and location facts for many GeoJSON field features in one vectorised pass.

    `hectares` optionally reuses already computed areas (for example the stored ones).
    Length, width and orientation come from the smallest rectangle aligned with the
    field's main axis or one of its longest edges: exact for rectangular fields and
    within a few percent of the true minimum rectangle for typical field shapes.
    """
    if not features:
        return []
    xs, ys, ring_feature, ring_hole, ring_sizes, origins = [], [], [], [], [], []
    for f, feature in enumerate(features):
        parts = _parts(feature["geometry"])
        first = parts[0][0]
        lon0 = sum(p[0] for p in first) / len(first)
        lat0 = sum(p[1] for p in first) / len(first)
        kx = METRES_PER_DEGREE * math.cos(math.radians(lat0))
        origins.append((lon0, lat0, kx))
        for rings in parts:
            for r, ring in enumerate(rings):
                pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
                xs.extend((p[0] - lon0) * kx for p in pts)
                ys.extend((p[1] - lat0) * METRES_PER_DEGREE for p in pts)
                ring_feature.append(f)
                ring_hole.append(r > 0)
                ring_sizes.append(len(pts))
    x, y = np.asarray(xs), np.asarray(ys)
    sizes = np.asarray(ring_sizes)
    starts = np.concatenate(([0], np.cumsum(sizes)[:-1]))
    nxt = np.arange(len(x)) + 1
    nxt[starts + sizes - 1] = starts  # close each ring
    xn, yn = x[nxt], y[nxt]
    cross = x * yn - xn * y
    seg = np.hypot(xn - x, yn - y)
    ring_signed = np.add.reduceat(cross, starts) / 2
    ring_perimeter = np.add.reduceat(seg, starts)
    with np.errstate(divide="ignore", invalid="ignore"):
        ring_cx = np.add.reduceat((x + xn) * cross, starts) / (6 * ring_signed)
        ring_cy = np.add.reduceat((y + yn) * cross, starts) / (6 * ring_signed)
    rf, hole = np.asarray(ring_feature), np.asarray(ring_hole)
    n = len(features)
    ring_area = np.abs(ring_signed)
    shell_area = np.where(hole, 0.0, ring_area)
    area = np.bincount(rf, shell_area - np.where(hole, ring_area, 0.0), n)
    perimeter = np.bincount(rf, ring_perimeter, n)
    weight = np.bincount(rf, shell_area, n)
    safe = np.where(weight > 0, weight, 1.0)
    cx = np.bincount(rf, np.nan_to_num(ring_cx) * shell_area, n) / safe
    cy = np.bincount(rf, np.nan_to_num(ring_cy) * shell_area, n) / safe
    holes = np.bincount(rf, hole.astype(float), n).astype(int)
    shells = np.bincount(rf, (~hole).astype(float), n).astype(int)

    # Main axis from length-weighted second moments of the outer boundary.
    vertex_feature = np.repeat(rf, sizes)
    vertex_shell = np.repeat(~hole, sizes)
    w = np.where(vertex_shell, seg, 0.0)
    mx, my = (x + xn) / 2, (y + yn) / 2
    wsum = np.bincount(vertex_feature, w, n)
    wsum = np.where(wsum > 0, wsum, 1.0)
    ex = np.bincount(vertex_feature, w * mx, n) / wsum
    ey = np.bincount(vertex_feature, w * my, n) / wsum
    dx, dy = mx - ex[vertex_feature], my - ey[vertex_feature]
    sxx = np.bincount(vertex_feature, w * dx * dx, n)
    syy = np.bincount(vertex_feature, w * dy * dy, n)
    sxy = np.bincount(vertex_feature, w * dx * dy, n)
    theta = 0.5 * np.arctan2(2 * sxy, sxx - syy)

    # Candidate directions: the main axis and the field's longest outer edges. The
    # smallest enclosing rectangle of a field almost always lines up with one of them.
    edge_angle = np.arctan2(yn - y, xn - x)
    order = np.lexsort((-w, vertex_feature))
    rank = np.arange(len(order)) - np.repeat(np.searchsorted(vertex_feature[order], np.arange(n)), np.bincount(vertex_feature, minlength=n))
    candidates = [theta]
    for k in range(LONG_EDGES):
        pick = order[rank == k]
        angle = theta.copy()
        angle[vertex_feature[pick]] = edge_angle[pick]
        candidates.append(angle)
    first_vertex = np.concatenate(([0], np.flatnonzero(np.diff(vertex_feature)) + 1))
    best_area = np.full(n, np.inf)
    span_along = span_across = best_theta = np.zeros(n)
    for angle in candidates:
        c, s = np.cos(angle)[vertex_feature], np.sin(angle)[vertex_feature]
        along, across = x * c + y * s, -x * s + y * c
        sa = np.maximum.reduceat(along, first_vertex) - np.minimum.reduceat(along, first_vertex)
        sc = np.maximum.reduceat(across, first_vertex) - np.minimum.reduceat(across, first_vertex)
        better = sa * sc < best_area
        best_area = np.where(better, sa * sc, best_area)
        span_along, span_across = np.where(better, sa, span_along), np.where(better, sc, span_across)
        best_theta = np.where(better, angle, best_theta)
    long_first = span_along >= span_across
    length = np.where(long_first, span_along, span_across)
    width = np.where(long_first, span_across, span_along)
    long_angle = np.where(long_first, best_theta, best_theta + math.pi / 2)
    bearing = np.mod(90 - np.degrees(long_angle), 180)

    out = []
    for f, feature in enumerate(features):
        lon0, lat0, kx = origins[f]
        ha = hectares[f] if hectares is not None and hectares[f] is not None else area[f] / 10_000
        year = _SOURCE_YEAR.match(str(feature.get("id", "")))
        p = float(perimeter[f])
        out.append({
            "areaHa": round(float(ha), 2),
            "areaAcres": round(float(ha) / HECTARES_PER_ACRE, 2),
            "perimeterM": round(p),
            "lengthM": round(float(length[f])),
            "widthM": round(float(width[f])),
            "orientationDeg": round(float(bearing[f])) % 180,
            "compactness": round(min(1.0, 4 * math.pi * max(float(area[f]), 0.0) / p**2), 2) if p else None,
            "holes": int(holes[f]),
            "parts": int(shells[f]),
            "centroid": [round(lon0 + float(cx[f]) / kx, 6), round(lat0 + float(cy[f]) / METRES_PER_DEGREE, 6)],
            "sourceYear": int(year.group(1)) if year else None,
        })
    return out


def describe(feature: dict, hectares: float | None = None) -> dict:
    """Facts for one feature; see describe_many."""
    return describe_many([feature], [hectares])[0]
