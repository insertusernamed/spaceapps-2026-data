"""Daily weather on a 1 km grid for North America, from NASA Daymet (ORNL DAAC).

About 50 times finer than NASA POWER's grid, which matters most for rainfall and near
lakes. Daymet interpolates weather-station records, covers North America, Hawaii and
Puerto Rico from 1980, and is published up to the last complete calendar year. No
account; a location's whole period is one request.

Daymet years always have 365 days: in leap years December 31 is omitted, so that day
is missing here too. Columns match the NASA POWER client, so indicators work with either.
Docs: https://daymet.ornl.gov/web_services
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache

import pandas as pd
import requests

from .errors import ProviderError

URL = "https://daymet.ornl.gov/single-pixel/api/data"
FIRST_YEAR = 1980
# Daymet's North American domain, slightly inside its published bounds.
SOUTH, NORTH, WEST, EAST = 14.1, 82.9, -178.0, -53.0
GRID_DEG = 0.01  # about 1 km; snapping to it lets neighbouring clicks share a download


def covers(lat: float, lon: float) -> bool:
    return SOUTH <= lat <= NORTH and WEST <= lon <= EAST


def last_published_year(today: date | None = None) -> int:
    """Daymet appears a few months after each year ends; assume the last complete year."""
    return (today or date.today()).year - 1


def fetch_daily(lat: float, lon: float, start: str, end: str) -> pd.DataFrame:
    """Date-indexed tmax_c, tmin_c, tmean_c, precip_mm for YYYYMMDD start..end."""
    return _fetch(round(lat, 4), round(lon, 4), start, end).copy()


@lru_cache(maxsize=256)
def _fetch(lat: float, lon: float, start: str, end: str) -> pd.DataFrame:
    try:
        resp = requests.get(URL, timeout=(10, 60), params={
            "lat": lat, "lon": lon, "vars": "tmax,tmin,prcp", "format": "json",
            "start": f"{start[:4]}-{start[4:6]}-{start[6:]}", "end": f"{end[:4]}-{end[4:6]}-{end[6:]}"})
        resp.raise_for_status()
        raw = pd.DataFrame(resp.json()["data"])
        dates = pd.to_datetime(raw["year"].astype(int).astype(str) + raw["yday"].astype(int).astype(str).str.zfill(3), format="%Y%j")
        frame = pd.DataFrame({
            "tmax_c": raw["tmax (deg c)"].to_numpy(float),
            "tmin_c": raw["tmin (deg c)"].to_numpy(float),
            "precip_mm": raw["prcp (mm/day)"].to_numpy(float),
        }, index=pd.DatetimeIndex(dates, name="date")).sort_index()
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise ProviderError("NASA Daymet daily weather is unavailable or invalid") from exc
    frame["tmean_c"] = (frame["tmax_c"] + frame["tmin_c"]) / 2
    frame = frame.loc[pd.Timestamp(start):pd.Timestamp(end)]
    if frame.empty:
        raise ProviderError("NASA Daymet returned no days for this period")
    return frame


def provenance(lat: float, lon: float, start: str, end: str) -> dict:
    return {
        "source": "NASA Daymet v4",
        "url": URL,
        "variables": ["tmax", "tmin", "prcp"],
        "spatial_resolution_km": 1,
        "temporal_resolution": "daily",
        "kind": "interpolated from weather stations",
        "period": f"{start}-{end}",
        "location": {"lat": round(lat, 6), "lon": round(lon, 6)},
    }
