"""How green a field was through a growing season, from Sentinel-2 at 10 m.

Each clear Sentinel-2 overpass (about every 2-5 days) gives the field's mean NDVI,
the standard greenness index: about 0.1-0.2 for bare soil, rising past 0.6-0.8 for a
full crop canopy, and falling again at maturity and harvest. Scenes come from Element
84's Earth Search catalogue of Copernicus Sentinel-2 L2A data, with no account. Only
the pixels over the field are read from each cloud-optimized file, and cloud, shadow,
snow and water pixels are removed with the scene classification layer.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import numpy as np
import requests

from . import geometry
from .errors import ProviderError, ValidationError

STAC_URL = "https://earth-search.aws.element84.com/v1/search"
# Collection 1 is the consistent reprocessing, but it has gaps (2017 and 2022 are missing); the original
# collection fills them. Both record the reflectance offset in their band metadata, which is applied below.
COLLECTIONS = ("sentinel-2-c1-l2a", "sentinel-2-l2a")
FIRST_YEAR = 2017  # Sentinel-2 L2A is complete over North America from 2017
CLEAR_SCL = (4, 5)  # vegetation and bare soil; cloud, shadow, water, snow and defects are dropped
MAX_SCENE_CLOUD = 60  # skip scenes with more cloud overall; most of their fields are hidden
MIN_CLEAR_SHARE = 0.5  # an overpass counts when at least half of the field is clear
PARALLEL = 8  # scenes read at once for one field
MAX_READS = 16  # scene reads in flight across all requests, so concurrent users share bandwidth fairly
CURRENT_SEASON_TTL_S = 6 * 3600
_session = requests.Session()
_cache: dict[tuple[str, int], tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_reads = threading.BoundedSemaphore(MAX_READS)

GDAL_OPTIONS = {
    "AWS_NO_SIGN_REQUEST": "YES",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_TIMEOUT": "30",
    "GDAL_HTTP_MAX_RETRY": "2",
    "GDAL_INGESTED_BYTES_AT_OPEN": "32768",  # read COG headers in one request
    "GDAL_HTTP_VERSION": "2",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "VSI_CACHE": "TRUE",
}


def season_window(year: int, lat: float) -> tuple[str, str]:
    """April 1 to November 15 north of the equator; October 1 to May 15 (ending in `year`) south of it."""
    if lat >= 0:
        return f"{year}-04-01", f"{year}-11-15"
    return f"{year - 1}-10-01", f"{year}-05-15"


def default_year(today: date | None = None) -> int:
    """The most recent season with observations: this year from May, otherwise last year."""
    today = today or date.today()
    return today.year if today.month >= 5 else today.year - 1


def _search(shape: dict, start: str, end: str, collection: str = COLLECTIONS[0]) -> list[dict]:
    """Scenes over the field, oldest first, keeping the clearest when a day has several."""
    body = {
        "collections": [collection],
        "intersects": shape,
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": MAX_SCENE_CLOUD}},
        "limit": 200,
    }
    items: list[dict] = []
    for _ in range(5):  # a season is normally one page
        try:
            resp = _session.post(STAC_URL, json=body, timeout=(10, 40))
            resp.raise_for_status()
            page = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError("Earth Search (Sentinel-2 catalogue) is unavailable") from exc
        items += page.get("features", [])
        nxt = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
        if not nxt or not nxt.get("body"):
            break
        body = nxt["body"]
    best: dict[str, dict] = {}
    for item in items:
        day = item["properties"]["datetime"][:10]
        if day not in best or item["properties"]["eo:cloud_cover"] < best[day]["properties"]["eo:cloud_cover"]:
            best[day] = item
    return [best[d] for d in sorted(best)]


def _scale(item: dict, asset: dict) -> tuple[float, float]:
    """Reflectance = raw * scale + offset.

    From processing baseline 04.00 (2022) ESA stores reflectance with a +1000 offset, listed in each band's
    metadata. In the original collection Earth Search has already removed it from the pixels for some
    scenes (`earthsearch:boa_offset_applied`), so applying it again would double-count it.
    """
    band = (asset.get("raster:bands") or [{}])[0]
    offset = 0.0 if item["properties"].get("earthsearch:boa_offset_applied") is True else float(band.get("offset", 0.0))
    return float(band.get("scale", 0.0001)), offset


def _read(item: dict, shape: dict) -> dict | None:
    """Mean, 10th and 90th percentile NDVI over the clear part of the field, or None if unreadable."""
    import rasterio
    from rasterio import features, windows
    from rasterio.enums import Resampling
    from rasterio.warp import transform_geom

    assets = item["assets"]
    with rasterio.Env(**GDAL_OPTIONS), rasterio.open(assets["red"]["href"]) as red:
        local = transform_geom("EPSG:4326", red.crs, shape)
        win = windows.from_bounds(*features.bounds(local), red.transform).round_offsets().round_lengths()
        win = windows.Window(win.col_off, win.row_off, max(win.width, 1), max(win.height, 1))
        transform = red.window_transform(win)
        red_raw = red.read(1, window=win, boundless=True, fill_value=0).astype("float64")
        with rasterio.open(assets["nir"]["href"]) as nir:
            nir_raw = nir.read(1, window=win, boundless=True, fill_value=0).astype("float64")
        with rasterio.open(assets["scl"]["href"]) as scl:
            scl_win = windows.from_bounds(*windows.bounds(win, red.transform), scl.transform)
            scl_raw = scl.read(1, window=scl_win, out_shape=red_raw.shape, boundless=True, fill_value=0,
                               resampling=Resampling.nearest)
    inside = features.geometry_mask([local], red_raw.shape, transform, invert=True)
    if not inside.any():
        inside = np.ones(red_raw.shape, dtype=bool)  # very small outline: use the pixel it falls in
    (rs, ro), (ns, no) = _scale(item, assets["red"]), _scale(item, assets["nir"])
    r, n = red_raw * rs + ro, nir_raw * ns + no
    # Physical reflectance only: drops fill, saturated and offset-artefact pixels.
    clear = inside & np.isin(scl_raw, CLEAR_SCL) & (r > 0) & (n > 0) & (r <= 1.2) & (n <= 1.2)
    share = float(clear.sum() / inside.sum())
    if share < MIN_CLEAR_SHARE:
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = ((n - r) / (n + r))[clear]
    ndvi = ndvi[np.isfinite(ndvi)]
    if not ndvi.size:
        return None
    p10, p90 = np.percentile(ndvi, [10, 90])
    return {
        "date": item["properties"]["datetime"][:10],
        "ndvi": round(float(ndvi.mean()), 3),
        "ndviP10": round(float(p10), 3),
        "ndviP90": round(float(p90), 3),
        "clearShare": round(share, 2),
    }


def _summary(observations: list[dict]) -> dict:
    if not observations:
        return {"peakNdvi": None, "peakDate": None, "latestNdvi": None, "latestDate": None, "greenUpDate": None}
    peak = max(observations, key=lambda o: o["ndvi"])
    floor = min(o["ndvi"] for o in observations if o["date"] <= peak["date"])
    # Green-up: first overpass halfway from the spring low to the peak.
    half = floor + (peak["ndvi"] - floor) / 2
    green_up = next((o["date"] for o in observations if o["ndvi"] >= half and o["date"] <= peak["date"]), None)
    latest = observations[-1]
    return {"peakNdvi": peak["ndvi"], "peakDate": peak["date"], "latestNdvi": latest["ndvi"],
            "latestDate": latest["date"], "greenUpDate": green_up if peak["ndvi"] - floor >= 0.2 else None}


def season(polygon, year: int | None = None) -> dict:
    """Clear-sky NDVI observations over a field outline for one growing season, with a summary."""
    lon, lat = geometry.centroid(polygon)
    year = year or default_year()
    if year < FIRST_YEAR or year > date.today().year:
        raise ValidationError(f"year must be between {FIRST_YEAR} and {date.today().year}", "year")
    def closed(ring):
        return [list(p[:2]) for p in ring] + ([list(ring[0][:2])] if list(ring[0][:2]) != list(ring[-1][:2]) else [])

    shape = {"type": "MultiPolygon",
             "coordinates": [[closed(outer), *map(closed, holes)] for outer, holes in geometry.parts(polygon)]}
    key = (json.dumps(shape, separators=(",", ":")), year)
    finished = date.fromisoformat(season_window(year, lat)[1]) < date.today()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (finished or time.monotonic() - hit[0] < CURRENT_SEASON_TTL_S):
            return hit[1]

    start, end = season_window(year, lat)
    end = min(end, date.today().isoformat())
    for collection in COLLECTIONS:
        scenes = _search(shape, start, end, collection)
        if scenes:
            break
    try:
        import rasterio  # noqa: F401  (fail clearly if the image was built without it)
    except ImportError as exc:  # pragma: no cover
        raise ProviderError("field greenness needs rasterio on the server") from exc

    def one(item):
        try:
            with _reads:
                return _read(item, shape)
        except Exception:  # noqa: BLE001  one unreadable scene must not lose the season
            return "failed"

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        results = list(pool.map(one, scenes))
    observations = [r for r in results if isinstance(r, dict)]
    failed = sum(r == "failed" for r in results)
    if scenes and failed == len(scenes):
        raise ProviderError("Sentinel-2 imagery could not be read")
    result = {
        "year": year,
        "period": {"start": start, "end": end},
        "observations": observations,
        "summary": _summary(observations),
        "scenesChecked": len(scenes),
        "scenesUnreadable": failed,
        "source": {"name": "Copernicus Sentinel-2 L2A via Earth Search", "collection": collection, "url": STAC_URL, "resolutionM": 10,
                   "license": "Copernicus Sentinel data, free and open"},
        "note": ("Mean NDVI over the clear part of the field per overpass; an overpass counts when at least half "
                 "of the field is clear. Bare soil is about 0.1-0.2 and a full canopy 0.6-0.9."),
    }
    with _cache_lock:
        _cache[key] = (time.monotonic(), result)
    return result

