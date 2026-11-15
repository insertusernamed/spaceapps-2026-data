"""NASA HLS v2 and native Sentinel-2 RGB views via Planetary Computer."""
from datetime import date, timedelta
from urllib.parse import urlencode

import requests

from . import geometry
from .errors import ProviderError, ValidationError

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
TILER = "https://planetarycomputer.microsoft.com/api/data/v1/item"
COLLECTION = "sentinel-2-l2a"
HLS_COLLECTIONS = ["hls2-l30", "hls2-s30"]


def images(polygon, start=None, end=None, max_cloud=30, limit=6, dataset="hls"):
    if dataset not in ("hls", "sentinel-2"):
        raise ValidationError("dataset must be hls or sentinel-2")
    is_hls = dataset == "hls"
    collections = HLS_COLLECTIONS if is_hls else [COLLECTION]
    first = date(2013, 4, 11) if is_hls else date(2015, 6, 27)
    resolution = 30 if is_hls else 10
    end = end or date.today()
    start = start or max(first, end - timedelta(days=365))
    if not first <= start <= end <= date.today():
        raise ValidationError(f"dates must be ordered, from {first} through today")
    if (end - start).days > 366:
        raise ValidationError("imagery searches may span at most 366 days")
    if not 0 <= max_cloud <= 100 or not 1 <= limit <= 20:
        raise ValidationError("maxCloud must be 0-100 and limit 1-20")
    bounds = geometry.bbox(polygon)
    if geometry.area_ha(polygon) <= 0 or bounds[2] - bounds[0] > 1 or bounds[3] - bounds[1] > 1:
        raise ValidationError("outline must have positive area and fit within 1 by 1 degree")
    shape = {"type": "MultiPolygon", "coordinates": [
        [ring + [ring[0]] for ring in [outer, *holes]]
        for outer, holes in geometry.parts(polygon)]}
    body = {
        "collections": collections, "intersects": shape,
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lte": max_cloud}},
        "sortby": [{"field": "datetime", "direction": "desc"}], "limit": limit,
    }
    try:
        response = requests.post(f"{STAC}/search", json=body, timeout=(10, 40))
        response.raise_for_status()
        page = response.json()
        scenes = []
        for item in page["features"][:limit]:
            props = item["properties"]
            collection = item["collection"]
            if collection not in collections:
                raise ValueError("unexpected collection")
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
                "sceneBbox": item.get("bbox"),
                "sceneUrl": f"{STAC}/collections/{collection}/items/{item['id']}",
            })
        has_more = any(link.get("rel") == "next" for link in page.get("links", []))
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise ProviderError(f"{dataset} imagery catalogue is unavailable or returned invalid data") from exc
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "bbox": list(bounds), "images": scenes, "hasMore": has_more,
        "dataset": dataset,
        "source": {"name": "NASA Harmonized Landsat Sentinel-2 v2.0" if is_hls else "Copernicus Sentinel-2 Level-2A",
                   "provider": "Microsoft Planetary Computer", "collections": collections,
                   "resolutionM": resolution,
                   "url": "https://hls.gsfc.nasa.gov/" if is_hls else "https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a"},
        "note": "Newest matching scenes first. RGB images are cropped to the field bounding box, not masked to the polygon. "
                "The top-level bbox describes the requested crop; sceneBbox describes the original satellite scene extent. "
                "Cloud percentage is for the whole scene; the field may still be cloudy or partly outside the scene. "
                f"Source pixels are {resolution} m; crops are capped at 1024 pixels and small crops are not enlarged. Image URLs render on demand at the provider. "
                "If hasMore is true, narrow the dates to retrieve older scenes.",
    }
