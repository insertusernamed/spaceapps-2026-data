"""Soil wetness, solar radiation, wind, humidity and Penman-Monteith ET0 for a field, from NASA POWER.

NASA POWER (MERRA-2 and CERES, about 50 km) carries variables the 1 km Daymet weather lacks: root-zone and
surface soil wetness, solar radiation, wind and humidity, daily from 1981 to a few days ago. They describe the
region around the field, not the field itself.

Soil wetness (0 = dry, 1 = saturated) is read three ways: the usual value for each calendar month, the growing
season mean for each year, and how the latest 30 days compare with the same 30 days in earlier years. Reference
evapotranspiration uses FAO-56 Penman-Monteith, which needs the radiation, wind and humidity that Hargreaves
(used in the field summary) only approximates from temperature.
"""
from __future__ import annotations

import math
import threading
import time
from datetime import date

import numpy as np
import pandas as pd
import requests

from . import geometry
from .errors import ProviderError, ValidationError
from .indicators import default_season

POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
VARIABLES = {
    "T2M_MAX": "tmax_c", "T2M_MIN": "tmin_c", "RH2M": "rh_pct", "WS2M": "wind_ms",
    "ALLSKY_SFC_SW_DWN": "solar_mj", "GWETROOT": "root_zone", "GWETTOP": "surface",
}
FIRST_YEAR = 1981
DEFAULT_YEARS = 20
RECENT_DAYS = 30
MIN_SEASON_SHARE = 0.9  # a season counts when at least this share of its days has values
TTL_S = 6 * 3600

_session = requests.Session()
_lock = threading.Lock()
_cache: dict[tuple, tuple[float, pd.DataFrame, float | None]] = {}


def fetch(lat: float, lon: float, start: date, end: date) -> tuple[pd.DataFrame, float | None]:
    """Daily POWER values for a point, and the grid cell's elevation in metres. Missing values are NaN."""
    key = (round(lat, 2), round(lon, 2), start, end)
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < TTL_S:
            return hit[1], hit[2]
    try:
        resp = _session.get(POWER_URL, timeout=(10, 90), params={
            "latitude": lat, "longitude": lon, "start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
            "parameters": ",".join(VARIABLES), "community": "AG", "format": "JSON"})
        resp.raise_for_status()
        body = resp.json()
        frame = pd.DataFrame(body["properties"]["parameter"]).rename(columns=VARIABLES)
        if frame.empty or not set(VARIABLES.values()) <= set(frame.columns):
            raise ValueError("missing POWER variables")
        frame.index = pd.to_datetime(frame.index, format="%Y%m%d")
        frame = frame.sort_index().astype(float).replace(-999.0, np.nan)
        coordinates = body.get("geometry", {}).get("coordinates", [])
        elevation = float(coordinates[2]) if len(coordinates) > 2 else None
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        raise ProviderError("NASA POWER agro-weather is unavailable or invalid") from exc
    with _lock:
        _cache[key] = (time.monotonic(), frame, elevation)
    return frame, elevation


def et0_penman_monteith(frame: pd.DataFrame, lat: float, elevation_m: float) -> pd.Series:
    """Daily FAO-56 Penman-Monteith reference evapotranspiration in mm (equation 6, daily step, G = 0)."""
    tmax, tmin = frame["tmax_c"], frame["tmin_c"]
    tmean = (tmax + tmin) / 2

    def svp(t):
        return 0.6108 * np.exp(17.27 * t / (t + 237.3))

    es = (svp(tmax) + svp(tmin)) / 2
    ea = es * frame["rh_pct"] / 100
    delta = 4098 * svp(tmean) / (tmean + 237.3) ** 2
    pressure = 101.3 * ((293 - 0.0065 * elevation_m) / 293) ** 5.26
    gamma = 0.000665 * pressure

    doy = frame.index.dayofyear.to_numpy()
    phi = math.radians(lat)
    dr = 1 + 0.033 * np.cos(2 * np.pi * doy / 365)
    decl = 0.409 * np.sin(2 * np.pi * doy / 365 - 1.39)
    ws = np.arccos(np.clip(-np.tan(phi) * np.tan(decl), -1, 1))
    ra = 24 * 60 / np.pi * 0.0820 * dr * (ws * np.sin(phi) * np.sin(decl) + np.cos(phi) * np.cos(decl) * np.sin(ws))
    rso = (0.75 + 2e-5 * elevation_m) * ra
    rs = frame["solar_mj"]
    rns = 0.77 * rs
    ratio = (rs / pd.Series(rso, index=frame.index).replace(0, np.nan)).clip(0.25, 1.0)
    sigma = 4.903e-9
    rnl = (sigma * ((tmax + 273.16) ** 4 + (tmin + 273.16) ** 4) / 2
           * (0.34 - 0.14 * np.sqrt(ea.clip(lower=0))) * (1.35 * ratio - 0.35))
    rn = rns - rnl
    u2 = frame["wind_ms"]
    et0 = (0.408 * delta * rn + gamma * 900 / (tmean + 273) * u2 * (es - ea)) / (delta + gamma * (1 + 0.34 * u2))
    return et0.clip(lower=0)


def _number(value, digits=3):
    return None if value is None or pd.isna(value) else round(float(value), digits)


def _percentiles(values: pd.Series) -> dict | None:
    valid = values.dropna()
    if valid.empty:
        return None
    return {"p10": _number(valid.quantile(0.1)), "p50": _number(valid.quantile(0.5)),
            "p90": _number(valid.quantile(0.9)), "years": int(len(valid))}


def _recent(frame: pd.DataFrame) -> dict | None:
    """The latest 30 days of root-zone wetness against the same 30 days in earlier years."""
    root = frame["root_zone"].dropna()
    if root.empty:
        return None
    last = root.index[-1]
    window = root[root.index > last - pd.Timedelta(days=RECENT_DAYS)]
    mean = window.mean()
    past = []
    for year in sorted(set(root.index.year) - {last.year}):
        try:
            end = last.replace(year=year)
        except ValueError:  # 29 February
            end = last.replace(year=year, day=28)
        chunk = root[(root.index > end - pd.Timedelta(days=RECENT_DAYS)) & (root.index <= end)]
        if len(chunk) >= RECENT_DAYS * MIN_SEASON_SHARE:
            past.append(chunk.mean())
    percentile = round(100 * sum(v <= mean for v in past) / len(past)) if len(past) >= 5 else None
    status = None if percentile is None else "dry" if percentile <= 20 else "wet" if percentile >= 80 else "normal"
    surface = frame["surface"].dropna()
    return {"date": last.date().isoformat(), "rootZone": _number(root.iloc[-1]),
            "surface": _number(surface.iloc[-1]) if not surface.empty else None,
            "rootZone30DayMean": _number(mean), "percentile": percentile, "comparedYears": len(past),
            "status": status}


def summary(polygon, years: int = DEFAULT_YEARS, today: date | None = None) -> dict:
    """Soil wetness, radiation, wind, humidity and Penman-Monteith ET0 around a field outline."""
    today = today or date.today()
    if not 5 <= years <= today.year - FIRST_YEAR:
        raise ValidationError(f"years must be between 5 and {today.year - FIRST_YEAR}", "years")
    lon, lat = geometry.centroid(polygon)
    start = date(today.year - years, 1, 1)
    frame, elevation = fetch(lat, lon, start, today)
    season = default_season(lat)
    frame = frame.assign(et0_mm=et0_penman_monteith(frame, lat, elevation or 0.0))

    in_season = frame[frame.index.month.isin(season)]
    season_year = in_season.index.year - (in_season.index.month < season[0])
    grouped = in_season.groupby(season_year)
    expected = pd.Series({y: sum(pd.Timestamp(y + (m < season[0]), m, 1).days_in_month for m in season)
                          for y in grouped.groups})
    complete = (grouped.count().div(expected, axis=0) >= MIN_SEASON_SHARE)
    means = grouped.mean().where(complete)
    et0_total = (grouped["et0_mm"].mean() * expected).where(complete["et0_mm"])

    by_year = [{"year": int(y), "rootZone": _number(means.at[y, "root_zone"]),
                "surface": _number(means.at[y, "surface"]), "et0Mm": _number(et0_total.at[y], 1),
                "solarMjM2Day": _number(means.at[y, "solar_mj"], 2), "windMs": _number(means.at[y, "wind_ms"], 2),
                "humidityPct": _number(means.at[y, "rh_pct"], 1)} for y in means.index]
    monthly = frame.groupby(frame.index.month)[["root_zone", "surface", "et0_mm", "solar_mj"]].mean()
    return {
        "location": {"lat": round(lat, 6), "lon": round(lon, 6), "elevationM": _number(elevation, 1)},
        "period": {"start": start.isoformat(), "end": frame.index[-1].date().isoformat()},
        "season": {"months": list(season)},
        "soilWetness": {
            "latest": _recent(frame),
            "monthly": [{"month": int(m), "rootZone": _number(r["root_zone"]), "surface": _number(r["surface"])}
                        for m, r in monthly.iterrows()],
            "seasonPercentiles": {"rootZone": _percentiles(means["root_zone"]),
                                  "surface": _percentiles(means["surface"])},
        },
        "et0": {"method": "FAO-56 Penman-Monteith", "seasonPercentilesMm": _percentiles(et0_total),
                "monthlyMmPerDay": [{"month": int(m), "et0": _number(r["et0_mm"], 2)} for m, r in monthly.iterrows()]},
        "seasonMeans": {"solarMjM2Day": _number(means["solar_mj"].median(), 2),
                        "windMs": _number(means["wind_ms"].median(), 2),
                        "humidityPct": _number(means["rh_pct"].median(), 1)},
        "seasonByYear": by_year,
        "units": {"rootZone": "0-1 (share of saturation)", "surface": "0-1 (share of saturation)", "et0Mm": "mm per season",
                  "solarMjM2Day": "MJ/m2/day", "windMs": "m/s at 2 m", "humidityPct": "%"},
        "source": {"name": "NASA POWER (MERRA-2, CERES)", "url": POWER_URL, "resolutionKm": 50,
                   "variables": list(VARIABLES)},
        "note": ("Regional values for the roughly 50 km cell around the field, not field measurements. Soil wetness is "
                 "modelled; 0 is dry and 1 is saturated. The latest values are a few days old and come from POWER's "
                 "near-real-time stream, which can read differently from the long record, so treat the latest "
                 "percentile as a prompt to check the field rather than a measurement."),
    }
