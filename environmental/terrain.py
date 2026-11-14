"""Slope and low spots from NASA SRTM: where a field may erode or hold cold air.

SRTM GL1 gives elevation at about 30 m. OpenTopoData serves it with no account
(public limit: 1 request per second, 1000 per day, 100 points per request). A
grid of elevations over the field or around a pin gives slope, relief, and
simple zones.

SRTM has several metres of vertical noise and sees the 2000-era surface, so on
nearly level ground the zones are a prompt to walk the field, not a survey.

Docs: https://www.opentopodata.org/api/
"""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache

import requests

from . import geometry
from .errors import ProviderError

URL = "https://api.opentopodata.org/v1/srtm30m"

GRID_POINTS = 10

MIN_SPACING_M = 60.0

PIN_SPACING_M = 90.0

PIN_POINTS = 5

SLOPE_MODERATE_PCT = 2.0

SLOPE_HIGH_PCT = 6.0

LOW_SPOT_MIN_M = 1.5

METRES_PER_DEG_LAT = 111_320.0

_rate_lock = threading.Lock()

_last_call = 0.0


def _grid_axes(polygon, lat: float, lon: float) -> tuple[list[float], list[float]]:
    """Latitudes and longitudes of the sampling grid, south to north and west to east."""
    if polygon is None:
        half = (PIN_POINTS - 1) / 2
        dlat = PIN_SPACING_M / METRES_PER_DEG_LAT
        dlon = dlat / max(math.cos(math.radians(lat)), 0.01)
        return (
            [lat + (i - half) * dlat for i in range(PIN_POINTS)],
            [lon + (i - half) * dlon for i in range(PIN_POINTS)],
        )
    west, south, east, north = geometry.bbox(polygon)
    mid = math.radians((south + north) / 2)
    height, width = (
        (north - south) * METRES_PER_DEG_LAT,
        (east - west) * METRES_PER_DEG_LAT * math.cos(mid),
    )

    def axis(lo: float, hi: float, metres: float) -> list[float]:
        n = max(3, min(GRID_POINTS, int(metres / MIN_SPACING_M)))
        return [lo + (hi - lo) * (i + 0.5) / n for i in range(n)]  # cell centres

    return axis(south, north, height), axis(west, east, width)


def _fetch(points: list[tuple[float, float]], timeout: int) -> list[float | None]:
    global _last_call
    with _rate_lock:  # the public service allows one request a second
        wait = _last_call + 1.05 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
    locations = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in points)
    try:
        resp = requests.post(
            URL,
            json={"locations": locations, "interpolation": "bilinear"},
            timeout=(10, timeout),
        )
    except requests.RequestException as exc:
        raise ProviderError(f"elevation request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise ProviderError(f"elevation service returned HTTP {resp.status_code}")
    try:
        return [r["elevation"] for r in resp.json()["results"]]
    except (ValueError, KeyError, TypeError) as exc:
        raise ProviderError(
            "elevation service returned an unexpected response"
        ) from exc


def _slopes(lats, lons, elevation) -> list[list[float | None]]:
    """Slope in percent at each grid point, from central differences."""
    nlat, nlon = len(lats), len(lons)
    dy = (lats[-1] - lats[0]) / (nlat - 1) * METRES_PER_DEG_LAT
    dx = (
        (lons[-1] - lons[0])
        / (nlon - 1)
        * METRES_PER_DEG_LAT
        * math.cos(math.radians(sum(lats) / nlat))
    )

    def diff(a, b, spacing):
        return None if a is None or b is None or spacing == 0 else (b - a) / spacing

    out: list[list[float | None]] = []
    for i in range(nlat):
        row = []
        for j in range(nlon):
            south, north = max(i - 1, 0), min(i + 1, nlat - 1)
            west, east = max(j - 1, 0), min(j + 1, nlon - 1)
            gy = diff(elevation[south][j], elevation[north][j], (north - south) * dy)
            gx = diff(elevation[i][west], elevation[i][east], (east - west) * dx)
            row.append(
                None if gx is None or gy is None else round(math.hypot(gx, gy) * 100, 1)
            )
        out.append(row)
    return out


def summarize(lats, lons, elevation, inside=None) -> dict:
    """Slope, relief, a rating, and a zone per grid point inside the field."""
    slopes = _slopes(lats, lons, elevation)
    keep = [
        (i, j)
        for i in range(len(lats))
        for j in range(len(lons))
        if elevation[i][j] is not None
        and slopes[i][j] is not None
        and (inside is None or inside[i][j])
    ]
    if not keep:
        raise ProviderError("no elevation values inside this field")
    heights = [elevation[i][j] for i, j in keep]
    values = sorted(slopes[i][j] for i, j in keep)
    mean_height = sum(heights) / len(heights)
    relief = max(heights) - min(heights)
    mean_slope = sum(values) / len(values)
    steep_tenth = values[min(len(values) - 1, int(len(values) * 0.9))]
    # A low spot must stand out from both the field's relief and SRTM's noise.
    low_below = mean_height - max(LOW_SPOT_MIN_M, 0.25 * relief)

    zones: list[list[str | None]] = [[None] * len(lons) for _ in lats]
    for i, j in keep:
        if slopes[i][j] >= SLOPE_HIGH_PCT:
            zones[i][j] = "steep"
        elif elevation[i][j] <= low_below:
            zones[i][j] = "low"
        else:
            zones[i][j] = "ok"
    flat = [z for row in zones for z in row if z]
    rating = (
        "high"
        if steep_tenth >= SLOPE_HIGH_PCT
        else "moderate"
        if mean_slope >= SLOPE_MODERATE_PCT
        else "low"
    )
    return {
        "elevation_m_mean": round(mean_height, 1),
        "relief_m": round(relief, 1),
        "slope_pct_mean": round(mean_slope, 1),
        "slope_pct_p90": steep_tenth,
        "erosionRating": rating,
        "steepShare": round(flat.count("steep") / len(flat), 2),
        "lowSpotShare": round(flat.count("low") / len(flat), 2),
        "grid": {
            "lats": [round(x, 6) for x in lats],
            "lons": [round(x, 6) for x in lons],
            "elevation_m": elevation,
            "slope_pct": slopes,
            "zones": zones,
        },
    }


def provenance(lat: float, lon: float) -> dict:
    return {
        "source": "NASA SRTM GL1",
        "product": "30 m elevation, via OpenTopoData",
        "url": URL,
        "spatial_resolution_km": 0.03,
        "kind": "satellite radar observation, 2000",
        "location": {"lat": lat, "lon": lon},
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@lru_cache(maxsize=512)
def _elevations(points: tuple[tuple[float, float], ...]) -> tuple[float | None, ...]:
    """Elevations for a sample grid. SRTM does not change, so repeat clicks skip the rate-limited service."""
    return tuple(_fetch(list(points), timeout=30))


def fetch_terrain(lat: float, lon: float, polygon) -> dict:
    """Sample elevation inside the outline and return summary statistics."""
    lats, lons = _grid_axes(polygon, lat, lon)
    values = list(_elevations(tuple((a, b) for a in lats for b in lons)))
    if len(values) != len(lats) * len(lons):
        raise ProviderError("elevation service returned the wrong number of points")
    if any(
        v is not None and (not isinstance(v, (int, float)) or not math.isfinite(v))
        for v in values
    ):
        raise ProviderError("elevation service returned invalid values")
    elevations = [values[i * len(lons) : (i + 1) * len(lons)] for i in range(len(lats))]
    inside = [[geometry.contains(polygon, b, a) for b in lons] for a in lats]
    result = summarize(lats, lons, elevations, inside)
    result.pop("grid", None)
    return result
