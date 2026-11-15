"""NASA HLS v2 and native Sentinel-2 RGB views via Planetary Computer."""
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import json
import math
from datetime import date
from urllib.parse import urlencode

import requests

from . import geometry, imagery_quality
from .errors import ProviderError, ValidationError

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
TILER = "https://planetarycomputer.microsoft.com/api/data/v1/item"
COLLECTION = "sentinel-2-l2a"
HLS_COLLECTIONS = ["hls2-l30", "hls2-s30"]
MAX_CANDIDATES = 40
MAX_PAGES = 5
QUALITY_WORKERS = 4
HISTORY_CANDIDATES_PER_YEAR = 8
ARCHIVE_START = {"hls": date(2020, 1, 1), "sentinel-2": date(2015, 6, 27)}


@lru_cache(maxsize=512)
def _cached_quality(item_json, shape_json):
    """Reuse immutable scene QA measurements while keeping provider failures retryable."""
    return imagery_quality.read_quality(json.loads(item_json), json.loads(shape_json))


def _quality(item, shape):
    """Return field quality, or None when that scene's assets could not be checked."""
    try:
        return _cached_quality(json.dumps(item, sort_keys=True), json.dumps(shape, sort_keys=True))
    except Exception:  # One corrupt or unavailable scene must not discard other usable scenes.
        return None


def _spread(items):
    """Visit middle, oldest, newest, then progressively fill temporal gaps."""
    ordered = sorted(items, key=lambda item: (item["properties"]["datetime"], item["id"]))
    if not ordered:
        return []
    indices = [len(ordered) // 2, 0, len(ordered) - 1]
    chosen = list(dict.fromkeys(indices))
    gaps = [(0, len(ordered) // 2), (len(ordered) // 2, len(ordered) - 1)]
    while gaps:
        left, right = gaps.pop(0)
        if right - left > 1:
            middle = (left + right) // 2
            chosen.append(middle)
            gaps.extend(((left, middle), (middle, right)))
    return [ordered[index] for index in chosen]


def _select(body, shape, limit, max_candidates=MAX_CANDIDATES, spread=False):
    """Scan bounded catalogue pages, replacing rejected scenes with older candidates.

    hasMore also covers candidates not checked within the work budget; it does not
    promise that those candidates will pass field-level quality checks.
    """
    selected, seen = [], set()
    checked = unreadable = 0
    method, url, payload = "POST", f"{STAC}/search", body
    has_more = False
    with ThreadPoolExecutor(max_workers=QUALITY_WORKERS) as pool:
        for _ in range(MAX_PAGES):
            response = (requests.post(url, json=payload, timeout=(10, 40)) if method == "POST"
                        else requests.get(url, timeout=(10, 40)))
            response.raise_for_status()
            page = response.json()
            items = []
            for item in page["features"]:
                if item["collection"] not in body["collections"]:
                    raise ValueError("unexpected collection")
                identity = (item["collection"], item["id"])
                if identity not in seen:
                    seen.add(identity)
                    items.append(item)
            if spread:
                items = _spread(items)
            next_link = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
            for offset in range(0, len(items), QUALITY_WORKERS):
                batch = items[offset:offset + min(QUALITY_WORKERS, max_candidates - checked)]
                results = list(pool.map(lambda item: _quality(item, shape), batch))
                checked += len(batch)
                for item, quality in zip(batch, results):
                    if quality is None:
                        unreadable += 1
                    elif imagery_quality.acceptable(quality):
                        selected.append((item, quality))
                if len(selected) >= limit or checked >= max_candidates:
                    has_more = bool(len(selected) > limit or offset + len(batch) < len(items) or next_link)
                    break
            else:
                if not next_link:
                    has_more = False
                    break
                url, method = next_link["href"], next_link.get("method", "GET").upper()
                if method not in ("GET", "POST"):
                    raise ValueError("unsupported pagination method")
                payload = ({**payload, **next_link.get("body", {})} if next_link.get("merge")
                           else next_link.get("body", payload))
                has_more = True
                continue
            break
    if checked and unreadable == checked:
        raise ProviderError("Satellite field quality assets could not be read")
    return selected[:limit], has_more, checked, unreadable


def _history(body, shape, start, end, limit):
    """Allocate image slots evenly across calendar years, preserving archive endpoints.

    Each selected year has an independent catalogue search and an eight-scene QA
    budget. Dates within each year are sampled across the catalogue page instead
    of only trying the newest (often winter) observations. Missing years are
    reported; accepted scenes from one year never crowd out the other years.
    """
    years = list(range(start.year, end.year + 1))
    if len(years) > limit:
        indices = ([len(years) - 1] if limit == 1 else
                   [round(i * (len(years) - 1) / (limit - 1)) for i in range(limit)])
        years = [years[i] for i in indices]
    quotas = {year: limit // len(years) + (index < limit % len(years))
              for index, year in enumerate(years)}

    def one(year):
        first, last = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
        search = {**body, "datetime": f"{first}T00:00:00Z/{last}T23:59:59Z", "limit": 1000}
        try:
            return year, _select(search, shape, quotas[year], HISTORY_CANDIDATES_PER_YEAR, spread=True)
        except (ProviderError, requests.RequestException, ValueError, KeyError, TypeError):
            return year, None

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(one, years))
    selected, missing, failed = [], [], []
    has_more = len(years) < end.year - start.year + 1
    checked = unreadable = 0
    for year, result in results:
        if result is None:
            failed.append(year)
            continue
        images, more, count, errors = result
        selected.extend(images)
        has_more |= more
        checked += count
        unreadable += errors
        if not images:
            missing.append(year)
    if failed and not selected and not checked:
        raise ProviderError("Satellite historical imagery or field quality assets could not be read")
    selected.sort(key=lambda pair: pair[0]["properties"]["datetime"], reverse=True)
    return selected, has_more, checked, unreadable, missing, failed


def _crop_bounds(bounds):
    """Include surrounding land without changing the polygon used for scene QA.

    Pad each edge by a full field span, with at least 180 metres of context.
    Clamp at geographic limits and preserve longitude/latitude ordering.
    """
    west, south, east, north = bounds
    latitude = (south + north) / 2
    dy = max(north - south, 180 / 111_320)
    dx = max(east - west, 180 / (111_320 * max(0.01, math.cos(math.radians(latitude)))))
    return max(-180, west - dx), max(-90, south - dy), min(180, east + dx), min(90, north + dy)


def images(polygon, start=None, end=None, max_cloud=30, limit=20, dataset="hls"):
    """Return quality-screened RGB scenes spread across the requested archive years."""
    if dataset not in ("hls", "sentinel-2"):
        raise ValidationError("dataset must be hls or sentinel-2")
    is_hls = dataset == "hls"
    collections = HLS_COLLECTIONS if is_hls else [COLLECTION]
    first = ARCHIVE_START[dataset]
    resolution = 30 if is_hls else 10
    end = end or date.today()
    start = start or first
    if not first <= start <= end <= date.today():
        raise ValidationError(f"dates must be ordered, from {first} through today")
    if not 0 <= max_cloud <= 100 or not 1 <= limit <= 20:
        raise ValidationError("maxCloud must be 0-100 and limit 1-20")
    bounds = geometry.bbox(polygon)
    if geometry.area_ha(polygon) <= 0 or bounds[2] - bounds[0] > 1 or bounds[3] - bounds[1] > 1:
        raise ValidationError("outline must have positive area and fit within 1 by 1 degree")
    bounds = _crop_bounds(bounds)
    shape = {"type": "MultiPolygon", "coordinates": [
        [ring + [ring[0]] for ring in [outer, *holes]]
        for outer, holes in geometry.parts(polygon)]}
    body = {
        "collections": collections, "intersects": shape,
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lte": max_cloud}},
        "sortby": [{"field": "datetime", "direction": "desc"}], "limit": min(MAX_CANDIDATES, max(20, limit * 2)),
    }
    try:
        if start.year != end.year:
            selected, has_more, checked, unreadable, missing_years, failed_years = _history(body, shape, start, end, limit)
        else:
            selected, has_more, checked, unreadable = _select(body, shape, limit)
            missing_years, failed_years = [], []
        scenes = []
        for item, quality in selected:
            props = item["properties"]
            collection = item["collection"]
            render = ({"assets": ["B04", "B03", "B02"],
                       # HLS stores reflectance * 10000 as signed int16. Stretch
                       # 0-0.3 reflectance explicitly, rather than the dtype range.
                       "rescale": "0,3000", "nodata": -9999,
                       "color_formula": "gamma RGB 1.4"}
                      if is_hls else {"assets": "visual", "asset_bidx": "visual|1,2,3", "nodata": 0})
            params = urlencode({
                "collection": collection, "item": item["id"], "max_size": 1024, **render,
            }, doseq=True)
            scenes.append({
                "sceneId": item["id"], "datetime": props["datetime"],
                "collection": collection,
                "platform": props["platform"], "sceneCloudCoverPct": props["eo:cloud_cover"],
                "imageUrl": f"{TILER}/bbox/{','.join(str(v) for v in bounds)}.png?{params}",
                "fieldQuality": quality,
                "sceneBbox": item.get("bbox"),
                "sceneUrl": f"{STAC}/collections/{collection}/items/{item['id']}",
            })
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise ProviderError(f"{dataset} imagery catalogue is unavailable or returned invalid data") from exc
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "bbox": list(bounds), "images": scenes, "hasMore": has_more,
        "dataset": dataset,
        "scenesChecked": checked, "scenesUnreadable": unreadable,
        "yearsWithoutClearImages": missing_years, "yearsUnreadable": failed_years,
        "selection": "yearly" if start.year != end.year else "newest",
        "qualityFilter": {"minValidShare": imagery_quality.MIN_VALID_SHARE,
                          "minClearShare": imagery_quality.MIN_CLEAR_SHARE,
                          "maxCandidates": MAX_CANDIDATES,
                          "maxCandidatesPerYear": HISTORY_CANDIDATES_PER_YEAR},
        "source": {"name": "NASA Harmonized Landsat Sentinel-2 v2.0" if is_hls else "Copernicus Sentinel-2 Level-2A",
                   "provider": "Microsoft Planetary Computer", "collections": collections,
                   "resolutionM": resolution,
                   "url": "https://hls.gsfc.nasa.gov/" if is_hls else "https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a"},
        "note": "Images are selected across calendar years when the period spans multiple years, listed newest first. RGB images include context around the field, not masked to the polygon. "
                "The top-level bbox describes the padded image crop; sceneBbox describes the original satellite scene extent. "
                "sceneCloudCoverPct is the whole-scene filter. Selected scenes also require at least 90% valid RGB "
                "and 90% clear pixels inside the field, excluding clouds, cloud adjacency (HLS), shadows, snow and fill. "
                "Quality is sampled on the native QA grid (HLS 30 m, Sentinel-2 SCL 20 m), capped at 1024 pixels per side. "
                "Fields containing no QA pixel centres are not returned. RGB images themselves are not quality-masked. "
                f"Source pixels are {resolution} m; crops are capped at 1024 pixels and small crops are not enlarged. Image URLs render on demand at the provider. "
                "hasMore includes unchecked candidates; narrow the dates to search older scenes. "
                "Unreadable quality assets are skipped; if every checked scene is unreadable, the request fails. "
                f"Years with no passing image within the sample budget: {missing_years}. Unreadable years: {failed_years}.",
    }
