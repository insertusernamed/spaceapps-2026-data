"""A field's long vegetation record from NASA MODIS: greenness every 16 days, and yearly growth and water use.

Three MODIS/Terra products are read through Microsoft's Planetary Computer, which hosts NASA's files as
cloud-optimized images with no account:

- MOD13Q1 (250 m, every 16 days, from 2000): NDVI, the standard greenness index. About 0.1-0.2 is bare soil,
  0.6-0.9 a full canopy. It shows when the field greens up, peaks and browns down each year, and which parts of
  the year it usually has little green cover.
- MOD17A3HGF (500 m, yearly, from 2001): net primary production, the carbon plants fixed in the year.
- MOD16A3GF (500 m, yearly, from 2001): evapotranspiration (water actually used) and potential
  evapotranspiration (what the atmosphere could have taken).

MODIS pixels are 250-500 m, so a small field shares its values with its surroundings; the Sentinel-2
greenness route is sharper but only reaches 2017. Only the pixels over the field are read from each file.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import numpy as np
import requests

from . import geometry
from .errors import ProviderError, ValidationError

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{account}/{container}"
NDVI_COLLECTION, NPP_COLLECTION, ET_COLLECTION = "modis-13Q1-061", "modis-17A3HGF-061", "modis-16A3GF-061"
NDVI_ASSET, RELIABILITY_ASSET = "250m_16_days_NDVI", "250m_16_days_pixel_reliability"
FIRST_NDVI_YEAR, FIRST_ANNUAL_YEAR = 2000, 2001
# Each year of NDVI is about 46 file reads (roughly 4 seconds); 10 years keeps a first request under a minute.
DEFAULT_YEARS, MAX_YEARS = 5, 10
LOW_COVER_NDVI = 0.3  # below this the field has little green cover: bare soil, stubble or seedlings
USABLE = (0, 1)  # pixel reliability: good or marginal
SNOW = 2
MIN_USABLE_SHARE = 0.5  # a composite counts when at least half of the field's pixels are usable
TYPICAL_SHARE = 0.5  # a 16-day period is "usually low cover" when at least this share of years were
PARALLEL = 32
MAX_READS = 48  # file reads in flight across all requests
TTL_S = 12 * 3600

GDAL_OPTIONS = {
    # Without these GDAL looks for cloud credentials before the first read, which stalls for about 30 seconds.
    "AWS_NO_SIGN_REQUEST": "YES",
    "AZURE_NO_SIGN_REQUEST": "YES",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_TIMEOUT": "30",
    "GDAL_HTTP_MAX_RETRY": "2",
    "GDAL_INGESTED_BYTES_AT_OPEN": "32768",
    "VSI_CACHE": "TRUE",
}

_session = requests.Session()
_lock = threading.Lock()
_token_lock = threading.Lock()
_tokens: dict[tuple[str, str], tuple[datetime, str]] = {}
_cache: dict[tuple[str, int], tuple[float, dict]] = {}
_reads = threading.BoundedSemaphore(MAX_READS)


def _token(href: str) -> str:
    """Read token for the storage container an asset lives in, reused until shortly before it expires.

    Fetched by one thread at a time: the token service limits anonymous requests, and a field's files
    are read by many threads that would otherwise all ask at once.
    """
    parsed = urlparse(href)
    key = (parsed.netloc.split(".")[0], parsed.path.lstrip("/").split("/")[0])

    def cached():
        hit = _tokens.get(key)
        return hit[1] if hit and hit[0] - datetime.now(timezone.utc) > timedelta(minutes=5) else None

    with _lock:
        token = cached()
    if token:
        return token
    with _token_lock:
        with _lock:
            token = cached()
        if token:
            return token
        try:
            resp = _session.get(TOKEN_URL.format(account=key[0], container=key[1]), timeout=(10, 30))
            resp.raise_for_status()
            body = resp.json()
            expiry = datetime.fromisoformat(body["msft:expiry"].replace("Z", "+00:00"))
            token = body["token"]
        except (requests.RequestException, ValueError, KeyError) as exc:
            raise ProviderError("Planetary Computer (MODIS files) is unavailable") from exc
        with _lock:
            _tokens[key] = (expiry, token)
        return token


def _search(collection: str, lon: float, lat: float, start: str, end: str) -> list[dict]:
    """MODIS/Terra items over a point, oldest first."""
    body = {"collections": [collection], "intersects": {"type": "Point", "coordinates": [lon, lat]},
            "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z", "limit": 1000}
    items: list[dict] = []
    for _ in range(5):
        try:
            resp = _session.post(STAC_URL, json=body, timeout=(10, 60))
            resp.raise_for_status()
            page = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError("Planetary Computer (MODIS catalogue) is unavailable") from exc
        items += page.get("features", [])
        nxt = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
        if not nxt or not nxt.get("body"):
            break
        body = nxt["body"]
    terra = {item["properties"]["start_datetime"][:10]: item for item in items if item["id"].startswith("MOD")}
    return [terra[day] for day in sorted(terra)]


def _field_values(href: str, shape: dict) -> np.ndarray:
    """Raw values of the pixels whose centres fall in the outline (or the pixel the outline sits in)."""
    import rasterio
    from rasterio import features, windows
    from rasterio.warp import transform_geom

    with _reads, rasterio.Env(**GDAL_OPTIONS), rasterio.open(href) as src:
        local = transform_geom("EPSG:4326", src.crs, shape)
        win = windows.from_bounds(*features.bounds(local), src.transform).round_offsets().round_lengths()
        win = windows.Window(win.col_off, win.row_off, max(win.width, 1), max(win.height, 1))
        data = src.read(1, window=win, boundless=True, fill_value=src.nodata if src.nodata is not None else 0)
        inside = features.geometry_mask([local], data.shape, src.window_transform(win), invert=True)
    if not inside.any():
        inside = np.ones(data.shape, dtype=bool)
    return data[inside].astype("float64")


def _signed(item: dict, asset: str) -> str:
    href = item["assets"][asset]["href"]
    return f"{href}?{_token(href)}"


def _ndvi_observation(item: dict, shape: dict) -> dict | None:
    """One 16-day composite over the field: mean NDVI of usable pixels, or a snow flag, or None when unusable."""
    ndvi = _field_values(_signed(item, NDVI_ASSET), shape)
    rank = _field_values(_signed(item, RELIABILITY_ASSET), shape)
    usable = np.isin(rank, USABLE) & (ndvi >= -2000) & (ndvi <= 10000)
    share = float(usable.sum() / usable.size) if usable.size else 0.0
    day = item["properties"]["start_datetime"][:10]
    if share >= MIN_USABLE_SHARE:
        return {"date": day, "ndvi": round(float(ndvi[usable].mean()) * 0.0001, 3), "snow": False,
                "usableShare": round(share, 2)}
    if rank.size and float((rank == SNOW).sum() / rank.size) >= MIN_USABLE_SHARE:
        return {"date": day, "ndvi": None, "snow": True, "usableShare": round(share, 2)}
    return None


def _annual_value(item: dict, asset: str, shape: dict, scale: float, valid_max: float) -> float | None:
    values = _field_values(_signed(item, asset), shape)
    values = values[(values >= -30000) & (values <= valid_max)]
    return None if not values.size else round(float(values.mean()) * scale, 3)


def _annual_row(year: int, npp_item: dict | None, et_item: dict | None, shape: dict) -> dict:
    npp = _annual_value(npp_item, "Npp_500m", shape, 0.0001, 32700) if npp_item else None
    et = _annual_value(et_item, "ET_500m", shape, 0.1, 65500) if et_item else None
    pet = _annual_value(et_item, "PET_500m", shape, 0.1, 65500) if et_item else None
    return {"year": year, "nppKgCPerM2": npp, "etMm": None if et is None else round(et, 1),
            "petMm": None if pet is None else round(pet, 1),
            "etShareOfPet": None if not et or not pet else round(et / pet, 2)}


def _low_cover(observation: dict) -> bool:
    return observation["snow"] or observation["ndvi"] < LOW_COVER_NDVI


def by_year(observations: list[dict]) -> list[dict]:
    """Peak, green-up and brown-down for each year with enough composites."""
    years: dict[int, list[dict]] = {}
    for obs in observations:
        years.setdefault(int(obs["date"][:4]), []).append(obs)
    rows = []
    for year, group in sorted(years.items()):
        green = [o for o in group if o["ndvi"] is not None]
        row = {"year": year, "composites": len(group), "peakNdvi": None, "peakDate": None, "greenUpDate": None,
               "brownDownDate": None, "greenDays": None, "lowCoverDays": min(365, 16 * sum(map(_low_cover, group)))}
        if len(green) >= 12:  # about half the year's 23 composites
            peak = max(green, key=lambda o: o["ndvi"])
            floor = min(o["ndvi"] for o in green)
            row.update(peakNdvi=peak["ndvi"], peakDate=peak["date"])
            if peak["ndvi"] - floor >= 0.2:  # otherwise there is no clear season, for example permanent grass
                half = floor + (peak["ndvi"] - floor) / 2
                up = next(o["date"] for o in green if o["ndvi"] >= half)
                down = next(o["date"] for o in reversed(green) if o["ndvi"] >= half)
                row.update(greenUpDate=up, brownDownDate=down,
                           greenDays=(date.fromisoformat(down) - date.fromisoformat(up)).days + 16)
        rows.append(row)
    return rows


def calendar(observations: list[dict]) -> list[dict]:
    """For each 16-day period of the year: the usual NDVI and how often the field had little green cover."""
    periods: dict[int, list[dict]] = {}
    for obs in observations:
        periods.setdefault(date.fromisoformat(obs["date"]).timetuple().tm_yday, []).append(obs)
    rows = []
    for start_day in sorted(periods):
        group = periods[start_day]
        values = [o["ndvi"] for o in group if o["ndvi"] is not None]
        rows.append({
            "startDay": start_day,
            "startDate": (date(2001, 1, 1) + timedelta(days=start_day - 1)).strftime("%m-%d"),
            "medianNdvi": None if not values else round(float(np.median(values)), 3),
            "lowCoverShare": round(sum(map(_low_cover, group)) / len(group), 2),
            "snowShare": round(sum(o["snow"] for o in group) / len(group), 2),
            "years": len(group),
        })
    return rows


def low_cover_windows(periods: list[dict]) -> list[dict]:
    """Stretches of the year that usually have little green cover, joined across the new year."""
    flags = [p["lowCoverShare"] >= TYPICAL_SHARE for p in periods]
    if not periods or not any(flags):
        return []
    if all(flags):
        return [{"startDate": "01-01", "endDate": "12-31", "days": 365}]
    first_clear = flags.index(False)  # start after a clear period so a window that wraps is found whole
    order = list(range(first_clear, len(periods))) + list(range(first_clear))
    windows, run = [], []
    for index in order + [first_clear]:
        if flags[index]:
            run.append(index)
        elif run:
            start = periods[run[0]]["startDay"]
            end = min(365, periods[run[-1]]["startDay"] + 15)
            windows.append({"startDate": periods[run[0]]["startDate"],
                            "endDate": (date(2001, 1, 1) + timedelta(days=end - 1)).strftime("%m-%d"),
                            "days": (end - start) % 365 + 1})
            run = []
    return sorted(windows, key=lambda w: -w["days"])


def _median(values: list) -> float | None:
    valid = [v for v in values if v is not None]
    return None if not valid else round(float(np.median(valid)), 3)


def _trend(rows: list[dict], key: str) -> float | None:
    points = [(r["year"], r[key]) for r in rows if r[key] is not None]
    if len(points) < 5:
        return None
    return round(float(np.polyfit([p[0] for p in points], [p[1] for p in points], 1)[0]), 5)


def history(polygon, years: int = DEFAULT_YEARS, today: date | None = None) -> dict:
    """NDVI every 16 days for recent years, and yearly growth and water use since 2001, over a field outline."""
    today = today or date.today()
    if not 1 <= years <= MAX_YEARS:
        raise ValidationError(f"years must be between 1 and {MAX_YEARS}", "years")
    shape = {"type": "MultiPolygon", "coordinates": [
        [[list(p[:2]) for p in ring] + ([list(ring[0][:2])] if list(ring[0][:2]) != list(ring[-1][:2]) else [])
         for ring in (outer, *holes)] for outer, holes in geometry.parts(polygon)]}
    key = (json.dumps(shape, separators=(",", ":")), years)
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < TTL_S:
            return hit[1]

    lon, lat = geometry.centroid(polygon)
    first_year = max(FIRST_NDVI_YEAR, today.year - years + 1)
    ndvi_items = _search(NDVI_COLLECTION, lon, lat, f"{first_year}-01-01", today.isoformat())
    npp_items = {int(i["properties"]["start_datetime"][:4]): i
                 for i in _search(NPP_COLLECTION, lon, lat, f"{FIRST_ANNUAL_YEAR}-01-01", today.isoformat())}
    et_items = {int(i["properties"]["start_datetime"][:4]): i
                for i in _search(ET_COLLECTION, lon, lat, f"{FIRST_ANNUAL_YEAR}-01-01", today.isoformat())}

    def guarded(job):
        try:
            return job()
        except ProviderError:
            raise
        except Exception:  # noqa: BLE001  one unreadable file must not lose the record
            return "failed"

    annual_years = sorted(set(npp_items) | set(et_items))
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        ndvi_jobs = [pool.submit(guarded, lambda item=item: _ndvi_observation(item, shape)) for item in ndvi_items]
        annual_jobs = [pool.submit(guarded, lambda y=y: _annual_row(y, npp_items.get(y), et_items.get(y), shape))
                       for y in annual_years]
        ndvi_results = [job.result() for job in ndvi_jobs]
        annual_results = [job.result() for job in annual_jobs]
    failed = sum(r == "failed" for r in ndvi_results) + sum(r == "failed" for r in annual_results)
    if (ndvi_items or annual_years) and failed == len(ndvi_results) + len(annual_results):
        raise ProviderError("MODIS files could not be read")
    observations = [r for r in ndvi_results if isinstance(r, dict)]
    yearly = [r for r in annual_results if isinstance(r, dict)]
    periods = calendar(observations)
    seasons = by_year(observations)

    result = {
        "location": {"lat": round(lat, 6), "lon": round(lon, 6)},
        "ndvi": {
            "period": {"start": f"{first_year}-01-01", "end": today.isoformat()},
            "lowCoverThreshold": LOW_COVER_NDVI,
            "observations": observations,
            "byYear": seasons,
            "calendar": periods,
            "lowCoverWindows": low_cover_windows(periods),
            "typical": {"peakNdvi": _median([s["peakNdvi"] for s in seasons]),
                        "greenDays": _median([s["greenDays"] for s in seasons]),
                        "lowCoverDays": _median([s["lowCoverDays"] for s in seasons if s["composites"] >= 20])},
        },
        "yearly": yearly,
        "yearlySummary": {
            "nppMedianKgCPerM2": _median([r["nppKgCPerM2"] for r in yearly]),
            "nppTrendKgCPerM2PerYear": _trend(yearly, "nppKgCPerM2"),
            "etMedianMm": _median([r["etMm"] for r in yearly]),
            "petMedianMm": _median([r["petMm"] for r in yearly]),
            "etShareOfPetMedian": _median([r["etShareOfPet"] for r in yearly]),
        },
        "filesChecked": len(ndvi_results) + len(annual_results),
        "filesUnreadable": failed,
        "source": [
            {"name": "NASA MODIS/Terra Vegetation Indices 16-Day (MOD13Q1 v6.1)", "collection": NDVI_COLLECTION,
             "resolutionM": 250, "from": FIRST_NDVI_YEAR},
            {"name": "NASA MODIS/Terra Net Primary Production Gap-Filled Yearly (MOD17A3HGF v6.1)",
             "collection": NPP_COLLECTION, "resolutionM": 500, "from": FIRST_ANNUAL_YEAR},
            {"name": "NASA MODIS/Terra Net Evapotranspiration Gap-Filled Yearly (MOD16A3GF v6.1)",
             "collection": ET_COLLECTION, "resolutionM": 500, "from": FIRST_ANNUAL_YEAR},
        ],
        "provider": {"name": "Microsoft Planetary Computer", "url": "https://planetarycomputer.microsoft.com"},
        "note": ("MODIS pixels are 250 m (NDVI) and 500 m (yearly values), so a small field shares its values with "
                 "neighbouring land. NDVI below 0.3 or snow counts as little green cover; that includes stubble "
                 "and young crops, not only bare soil. A composite is dropped when under half of the field's "
                 "pixels are usable (cloud). Yearly growth and water use are modelled from satellite data."),
    }
    with _lock:
        _cache[key] = (time.monotonic(), result)
    return result
