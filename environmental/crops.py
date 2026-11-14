"""What was planted in a field each year, from national 30 m crop inventory maps.

Canada: Agriculture and Agri-Food Canada (AAFC) Annual Crop Inventory, from 2009.
United States: USDA NASS Cropland Data Layer (CDL), national coverage from 2008.
Neither needs an account. Both are classifications of satellite imagery, so any one
year can be wrong, most often at field edges and for less common crops.

Each year is one request that summarises every map pixel inside the outline, so the
result reports the share of the field in each class rather than sampling points.
Pixels are 30 m in Web Mercator, so shares are meaningful but raw counts are not areas.
Mexico and other places have no public field-level crop map; they return no years.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache

import requests

from . import geometry
from .crop_classes import ACI_CLASSES, ACI_NOT_CROPS, CDL_CLASSES, CDL_NOT_CROPS
from .errors import ProviderError

TIMEOUT = (10, 30)
PARALLEL = 8
MIN_SHARE = 0.01
MAX_CLASSES = 5
YEARS_TTL_S = 24 * 3600

_session = requests.Session()


@dataclass(frozen=True, eq=False)  # hashed by identity, for the result cache
class Source:
    key: str
    name: str
    url: str
    histogram_url: str
    license: str
    first_year: int
    classes: dict
    not_crops: frozenset
    no_data: frozenset  # pixel values meaning "not mapped here"
    south: float
    north: float


ACI = Source(
    key="aci",
    name="AAFC Annual Crop Inventory",
    url="https://open.canada.ca/data/en/dataset/ba2645d5-4458-414d-b196-6303ac06c1c9",
    histogram_url="https://agriculture.canada.ca/imagery-images/rest/services/annual_crop_inventory/{year}/ImageServer/computeHistograms",
    license="Open Government Licence - Canada",
    first_year=2009,
    classes=ACI_CLASSES,
    not_crops=ACI_NOT_CROPS,
    no_data=frozenset({0}),
    south=41.5,
    north=84.0,
)
CDL = Source(
    key="cdl",
    name="USDA NASS Cropland Data Layer",
    url="https://www.nass.usda.gov/Research_and_Science/Cropland/SARS1a.php",
    histogram_url="https://pdi.scinet.usda.gov/image/rest/services/CDL_WM/ImageServer/computeHistograms",
    license="Public domain (USDA NASS)",
    first_year=2008,
    classes=CDL_CLASSES,
    not_crops=CDL_NOT_CROPS,
    no_data=frozenset({0}),
    south=24.0,
    north=49.5,
)
SOURCES = (ACI, CDL)

ACI_SERVICES_URL = "https://agriculture.canada.ca/imagery-images/rest/services/annual_crop_inventory"
CDL_QUERY_URL = "https://pdi.scinet.usda.gov/image/rest/services/CDL_WM/ImageServer/query"


def _get_json(provider: str, method: str, url: str, **kwargs) -> dict:
    try:
        resp = _session.request(method, url, timeout=TIMEOUT, **kwargs)
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise ProviderError(f"{provider} is unavailable") from exc
    if not isinstance(body, dict) or "error" in body:
        raise ProviderError(f"{provider} returned an error")
    return body


def esri_rings(polygon) -> list[list[list[float]]]:
    """GeoJSON-style geometry to ArcGIS rings: outer rings clockwise, holes counter-clockwise."""

    def signed_area(ring):
        return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:], strict=False))

    def oriented(ring, clockwise):
        ring = [list(p[:2]) for p in ring]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        return ring if (signed_area(ring) < 0) == clockwise else ring[::-1]

    rings = []
    for outer, holes in geometry.parts(polygon):
        rings.append(oriented(outer, True))
        rings += [oriented(h, False) for h in holes]
    return rings


_years_lock = threading.Lock()
_years_cache: dict[str, tuple[float, list[int]]] = {}


def _published_years(source: Source) -> list[int]:
    if source is ACI:
        body = _get_json(source.name, "get", ACI_SERVICES_URL, params={"f": "json"})
        years = [int(s["name"].rsplit("/", 1)[-1]) for s in body.get("services", []) if s["name"].rsplit("/", 1)[-1].isdigit()]
    else:
        body = _get_json(source.name, "get", CDL_QUERY_URL, params={
            "where": "Category=1", "outFields": "Year", "returnGeometry": "false", "f": "json"})
        years = [int(f["attributes"]["Year"]) for f in body.get("features", [])]
    return sorted({y for y in years if y >= source.first_year})


def available_years(source: Source) -> list[int]:
    """Years the provider currently publishes, refreshed daily."""
    with _years_lock:
        hit = _years_cache.get(source.key)
        if hit and time.monotonic() - hit[0] < YEARS_TTL_S:
            return hit[1]
    years = _published_years(source)
    if not years:
        raise ProviderError(f"{source.name} lists no years")
    with _years_lock:
        _years_cache[source.key] = (time.monotonic(), years)
    return years


@lru_cache(maxsize=4096)
def _histogram(source: Source, year: int, rings_json: str) -> tuple[tuple[int, int], ...]:
    """(class code, pixel count) inside the outline for one year. Past maps do not change, so results are cached."""
    data = {
        "geometry": json.dumps({"rings": json.loads(rings_json), "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryPolygon",
        "f": "json",
    }
    if source is CDL:
        data["mosaicRule"] = json.dumps({"mosaicMethod": "esriMosaicNone", "where": f"Year={year} AND Category=1"})
    body = _get_json(source.name, "post", source.histogram_url.format(year=year), data=data)
    histograms = body.get("histograms") or []
    if not histograms:
        return ()
    h = histograms[0]
    offset = round(h.get("min", -0.5) + 0.5)
    return tuple((i + offset, int(c)) for i, c in enumerate(h.get("counts", [])) if c and i + offset not in source.no_data)


def _year_row(source: Source, year: int, counts) -> dict | None:
    total = sum(c for _, c in counts)
    if not total:
        return None
    ranked = sorted(counts, key=lambda kv: -kv[1])

    def entry(code, count):
        return {"code": code, "name": source.classes.get(code, f"Class {code}"),
                "isCrop": code not in source.not_crops if code in source.classes else None,
                "share": round(count / total, 3)}

    top = entry(*ranked[0])
    return {
        "year": year,
        "crop": top["name"],
        "code": top["code"],
        "isCrop": top["isCrop"],
        "share": top["share"],
        "classes": [entry(code, n) for code, n in ranked[:MAX_CLASSES] if n / total >= MIN_SHARE],
    }


def _candidates(lat: float) -> list[Source]:
    return [s for s in SOURCES if s.south <= lat <= s.north]


def history(polygon) -> dict:
    """Year-by-year crop classes for a field outline, from whichever national map covers it."""
    rings_json = json.dumps(esri_rings(polygon), separators=(",", ":"))
    lon, lat = geometry.centroid(polygon)
    candidates = [s for s in _candidates(lat) if -170 <= lon <= -52]

    errors = []
    chosen, latest = None, None
    # Near the border both maps may apply; keep the one that actually maps this field.
    for source in candidates:
        try:
            year = available_years(source)[-1]
            row = _year_row(source, year, _histogram(source, year, rings_json))
        except ProviderError as exc:
            errors.append(str(exc))
            continue
        if row and (latest is None or row["year"] >= latest["year"]):
            chosen, latest = source, row
            if source is ACI and row["crop"] != "Cloud":
                break

    if chosen is None:
        if errors and len(errors) == len(candidates):
            raise ProviderError("; ".join(errors))
        return _result(None, [], [], "No public field-level crop map covers this location. Coverage is Canada and the United States.")

    years = [y for y in available_years(chosen) if y != latest["year"]]
    missing = []

    def fetch(year):
        try:
            return year, _year_row(chosen, year, _histogram(chosen, year, rings_json))
        except ProviderError:
            return year, False

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        rows = [latest]
        for year, row in pool.map(fetch, years):
            if row is False:
                missing.append(year)
            elif row:
                rows.append(row)
    rows.sort(key=lambda r: r["year"])
    return _result(chosen, rows, missing, "Satellite classification: any single year can be wrong, especially at field edges.")


def _result(source: Source | None, rows: list[dict], missing: list[int], note: str) -> dict:
    crops = [r for r in rows if r["isCrop"]]
    return {
        "source": None if source is None else {
            "name": source.name, "url": source.url, "license": source.license, "resolutionM": 30},
        "years": rows,
        "rotation": [r["crop"] for r in rows],
        "distinctCrops": sorted({r["crop"] for r in crops}),
        "cropYears": len(crops),
        "unavailableYears": sorted(missing),
        "note": note,
    }
