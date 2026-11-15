"""Groundwater and soil moisture compared with history, from NASA's GRACE and GRACE-FO drought indicators.

The GRACE satellites weigh the water stored on and under the land. NASA Goddard combines those measurements
with a land model and publishes, each week, how wet or dry three layers are compared with the same time of
year over 1948-2012: groundwater, root-zone soil moisture (the top metre) and surface soil moisture (the top
few centimetres). Each is a percentile: 50 is typical, 10 means drier than nine years in ten, 90 wetter.

The global product (GRACEDADM_CLSM025GL_7D) has cells of 0.25 degrees, about 25 km, so it describes the
district's water, not one field, and it runs about two months behind. Weekly files are small (about 2 MB,
whole world) and are downloaded with a NASA Earthdata login into GRACE_DIR (default data/grace), so later
requests for any field read them from disk.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from . import geometry
from .errors import ProviderError, ValidationError

SHORT_NAME, VERSION = "GRACEDADM_CLSM025GL_7D", "3.0"
FIRST_DAY = date(2003, 2, 3)
LAYERS = {"gws_inst": "groundwater", "rtzsm_inst": "rootZone", "sfsm_inst": "surface"}
DEFAULT_WEEKS, MAX_WEEKS = 12, 52
DEFAULT_YEARS, MAX_YEARS = 10, 23
INDEX_TTL_S = 6 * 3600
PRODUCT_URL = "https://disc.gsfc.nasa.gov/datasets/GRACEDADM_CLSM025GL_7D_3.0/summary"

_lock = threading.Lock()
_download_lock = threading.Lock()
_index_cache: tuple[float, list[tuple[date, object]]] | None = None

# Upper percentile of each class, driest first; the dry classes match the U.S. Drought Monitor's D4 to D0.
CONDITIONS = (
    (2, "exceptionally dry"), (5, "extremely dry"), (10, "severely dry"), (20, "moderately dry"),
    (30, "abnormally dry"), (70, "normal"), (90, "wetter than normal"), (100, "much wetter than normal"),
)


def condition(percentile: float | None) -> str | None:
    """Plain-language class for a percentile."""
    if percentile is None:
        return None
    return next(label for upper, label in CONDITIONS if percentile <= upper)


def directory() -> Path:
    """Where weekly files are kept: GRACE_DIR, else data/grace beside the code."""
    if os.environ.get("GRACE_DIR"):
        return Path(os.environ["GRACE_DIR"])
    return Path(__file__).resolve().parent.parent / "data" / "grace"


def _login():
    """Earthdata session from the configured credentials; raises EarthdataError when they are missing."""
    import earthaccess
    from app import ENV_PATH, load_credentials

    load_credentials(ENV_PATH)
    auth = earthaccess.login(strategy="environment", persist=False)
    if not auth or not auth.authenticated:
        raise ProviderError("NASA Earthdata login failed")
    return earthaccess


def _index(today: date) -> list[tuple[date, object]]:
    """Every published week as (first day, granule), oldest first. Refreshed every few hours."""
    global _index_cache
    with _lock:
        if _index_cache and time.monotonic() - _index_cache[0] < INDEX_TTL_S:
            return _index_cache[1]
    earthaccess = _login()
    try:
        found = earthaccess.search_data(short_name=SHORT_NAME, version=VERSION,
                                        temporal=(FIRST_DAY.isoformat(), today.isoformat()))
        weeks = sorted(
            ((date.fromisoformat(g["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"][:10]), g) for g in found),
            key=lambda pair: pair[0])
    except (KeyError, ValueError, TypeError) as exc:
        raise ProviderError("NASA GRACE drought indicator catalogue returned unusable data") from exc
    except Exception as exc:  # noqa: BLE001  earthaccess raises several unrelated types for network failures
        raise ProviderError("NASA GRACE drought indicator catalogue is unavailable") from exc
    if not weeks:
        raise ProviderError("NASA GRACE drought indicator catalogue lists no weeks")
    with _lock:
        _index_cache = (time.monotonic(), weeks)
    return weeks


def _name(week: date) -> str:
    return f"{SHORT_NAME}.A{week:%Y%m%d}.030.nc4"


def _files(weeks: list[tuple[date, object]]) -> dict[date, Path]:
    """Paths of the weekly files, downloading the ones not on disk yet."""
    folder = directory()
    paths = {week: folder / _name(week) for week, _ in weeks}
    with _download_lock:
        missing = [granule for week, granule in weeks if not paths[week].exists()]
        if missing:
            earthaccess = _login()
            try:
                folder.mkdir(parents=True, exist_ok=True)
                earthaccess.download(missing, str(folder))
            except Exception as exc:  # noqa: BLE001  earthaccess raises several unrelated types
                raise ProviderError("NASA GRACE drought indicator files could not be downloaded") from exc
    return {week: path for week, path in paths.items() if path.exists()}


def _read(path: Path, lat: float, lon: float) -> dict:
    """Percentiles in the cell containing a point, and that cell's centre. Missing values are None."""
    import h5py

    try:
        with h5py.File(path, "r") as f:
            lats, lons = f["lat"][:], f["lon"][:]
            i, j = int(abs(lats - lat).argmin()), int(abs(lons - lon).argmin())
            outside = abs(float(lats[i]) - lat) > 0.25 or abs(float(lons[j]) - lon) > 0.25
            values = {}
            for layer, key in LAYERS.items():
                value = float(f[layer][0, i, j])
                values[key] = None if outside or value != value or value < 0 or value > 100 else round(value, 1)
            return {"lat": round(float(lats[i]), 3), "lon": round(float(lons[j]), 3), **values}
    except (OSError, KeyError, ValueError, IndexError) as exc:
        raise ProviderError("NASA GRACE drought indicator file could not be read") from exc


def _row(week: date, cell: dict) -> dict:
    return {"weekStart": week.isoformat(), "groundwaterPercentile": cell["groundwater"],
            "rootZonePercentile": cell["rootZone"], "surfacePercentile": cell["surface"]}


def _same_week(weeks: list[tuple[date, object]], latest: date, years: int) -> list[tuple[date, object]]:
    """For each earlier year, the published week closest to the latest week's time of year."""
    picked = []
    for year in range(latest.year - years, latest.year):
        try:
            target = latest.replace(year=year)
        except ValueError:  # 29 February
            target = latest.replace(year=year, day=28)
        near = min(weeks, key=lambda pair: abs((pair[0] - target).days))
        if abs((near[0] - target).days) <= 10:
            picked.append(near)
    return picked


def indicators(polygon, weeks: int = DEFAULT_WEEKS, years: int = DEFAULT_YEARS, today: date | None = None) -> dict:
    """Groundwater and soil moisture percentiles around a field: recent weeks, and this time of year in earlier years."""
    today = today or date.today()
    if not 1 <= weeks <= MAX_WEEKS:
        raise ValidationError(f"weeks must be between 1 and {MAX_WEEKS}", "weeks")
    if not 0 <= years <= MAX_YEARS:
        raise ValidationError(f"years must be between 0 and {MAX_YEARS}", "years")
    lon, lat = geometry.centroid(polygon)
    published = _index(today)
    recent = published[-weeks:]
    latest_week = recent[-1][0]
    earlier = _same_week(published, latest_week, years) if years else []
    files = _files(list({week: (week, granule) for week, granule in recent + earlier}.values()))

    cells = {week: _read(path, lat, lon) for week, path in files.items()}
    if latest_week not in cells:
        raise ProviderError("NASA GRACE drought indicator files could not be downloaded")
    latest = cells[latest_week]
    return {
        "location": {"lat": round(lat, 6), "lon": round(lon, 6)},
        "cell": {"lat": latest["lat"], "lon": latest["lon"], "sizeDeg": 0.25},
        "latest": {
            "weekStart": latest_week.isoformat(),
            "ageDays": (today - latest_week).days,
            **{key: {"percentile": latest[key], "condition": condition(latest[key])} for key in LAYERS.values()},
        },
        "weekly": [_row(week, cells[week]) for week, _ in recent if week in cells],
        "sameWeekByYear": [_row(week, cells[week]) for week, _ in earlier if week in cells],
        "conditions": [{"upToPercentile": upper, "condition": label} for upper, label in CONDITIONS],
        "baseline": "1948-2012",
        "source": {"name": "NASA GRACE and GRACE-FO Data Assimilation drought indicators (Global, V3.0)",
                   "product": SHORT_NAME, "url": PRODUCT_URL, "resolutionDeg": 0.25, "frequency": "weekly",
                   "institution": "NASA Goddard Space Flight Center"},
        "note": ("Percentiles compare each layer with the same time of year over 1948-2012: 50 is typical, low is "
                 "dry. Cells are about 25 km, so this describes the district's water rather than one field, and the "
                 "newest week is usually about two months old. Groundwater changes slowly and matters for wells and "
                 "irrigation; root-zone moisture is what crops draw on. Values are null over ocean and ice."),
    }
