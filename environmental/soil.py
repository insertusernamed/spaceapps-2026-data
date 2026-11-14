"""Topsoil properties for a field outline, from ISRIC SoilGrids 2.0 (250 m, global, no account).

SoilGrids maps soil properties worldwide from soil profiles with machine learning. The
values are estimates for 250 m cells, not samples from the field: a reasonable start
when nothing better exists, and a soil test always wins.

The maps are read as cloud-optimized GeoTIFFs rather than through the REST service,
which allows only five requests a minute. Each property and depth has a tile index
(a VRT of about 6 MB). Indexes are downloaded once, their tile paths made absolute,
and kept in SOIL_INDEX_DIR, so later reads only fetch the few tiles under a field.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from . import geometry
from .errors import ProviderError

BASE_URL = "https://files.isric.org/soilgrids/latest/data"
DEPTHS = (("0-5cm", 5), ("5-15cm", 10), ("15-30cm", 15))  # (label, thickness in cm)
# SoilGrids layer: (response key, unit, divisor from stored integers to that unit)
PROPERTIES = {
    "phh2o": ("ph", "pH", 10),
    "clay": ("clayPct", "%", 10),
    "sand": ("sandPct", "%", 10),
    "silt": ("siltPct", "%", 10),
    "soc": ("organicCarbonGPerKg", "g/kg", 10),
    "nitrogen": ("nitrogenGPerKg", "g/kg", 100),
    "bdod": ("bulkDensityGPerCm3", "g/cm3", 100),
    "cec": ("cecCmolPerKg", "cmol(+)/kg", 10),
}
NODATA = -32768
READERS = 16  # shared reader threads: the cap on layer reads in flight across all requests
INDEX_DOWNLOAD_TIMEOUT = (10, 180)

GDAL_OPTIONS = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_INGESTED_BYTES_AT_OPEN": "32768",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_VERSION": "2",
    "GDAL_HTTP_TIMEOUT": "30",
    "GDAL_HTTP_MAX_RETRY": "2",
    "VSI_CACHE": "TRUE",
}

_local = threading.local()  # each reader thread keeps its layers open, so indexes are parsed once per thread
_readers = ThreadPoolExecutor(max_workers=READERS, thread_name_prefix="soilgrids")
_index_locks: dict[str, threading.Lock] = {}
_index_locks_guard = threading.Lock()
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()
_SOURCE = re.compile(r'<SourceFilename relativeToVRT="1">\.?/?([^<]+)</SourceFilename>')


def index_dir() -> Path:
    """Where tile indexes are kept: SOIL_INDEX_DIR, else beside the field database, else data/."""
    if os.environ.get("SOIL_INDEX_DIR"):
        return Path(os.environ["SOIL_INDEX_DIR"])
    if os.environ.get("FIELDS_DB"):
        return Path(os.environ["FIELDS_DB"]).parent / "soilgrids-index"
    return Path(__file__).resolve().parent.parent / "data" / "soilgrids-index"


def _index(layer: str, depth: str) -> Path:
    """Local tile index for one layer, downloading and rewriting it on first use."""
    name = f"{layer}_{depth}_mean"
    path = index_dir() / f"{name}.vrt"
    if path.exists():
        return path
    with _index_locks_guard:
        lock = _index_locks.setdefault(name, threading.Lock())
    with lock:
        if path.exists():
            return path
        folder = f"{BASE_URL}/{layer}/"
        try:
            resp = requests.get(f"{folder}{name}.vrt", timeout=INDEX_DOWNLOAD_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderError("SoilGrids is unavailable") from exc
        text, count = _SOURCE.subn(lambda m: f'<SourceFilename relativeToVRT="0">/vsicurl/{folder}{m.group(1)}</SourceFilename>', resp.text)
        if not count:
            raise ProviderError("SoilGrids returned an unexpected tile index")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".tmp", delete=False) as tmp:
            tmp.write(text)
        os.replace(tmp.name, path)  # atomic, so a half-written index is never used
    return path


def _read_layer(layer: str, depth: str, shapes: list[dict]) -> list[float | None]:
    """Mean of the layer over every 250 m cell each outline touches, in stored units; None over water.

    All outlines are read with one window, so nearby fields cost a single read per layer.
    """
    import rasterio
    from rasterio import features, windows
    from rasterio.warp import transform_geom

    path = _index(layer, depth)
    opened = getattr(_local, "datasets", None)
    if opened is None:
        opened = _local.datasets = {}
    with rasterio.Env(**GDAL_OPTIONS):
        ds = opened.get(path)
        if ds is None:
            ds = opened[path] = rasterio.open(path)
        local = [transform_geom("EPSG:4326", ds.crs, shape) for shape in shapes]
        bounds = [features.bounds(g) for g in local]
        union = (min(b[0] for b in bounds), min(b[1] for b in bounds), max(b[2] for b in bounds), max(b[3] for b in bounds))
        win = windows.from_bounds(*union, ds.transform).round_offsets().round_lengths()
        win = windows.Window(win.col_off - 1, win.row_off - 1, max(win.width, 1) + 2, max(win.height, 1) + 2)
        data = ds.read(1, window=win, boundless=True, fill_value=NODATA)
        transform = ds.window_transform(win)
    valid = data != NODATA
    means = []
    for g in local:
        inside = features.geometry_mask([g], data.shape, transform, invert=True, all_touched=True)
        values = data[inside & valid]
        means.append(float(values.mean()) if values.size else None)
    return means


def texture_class(sand: float, silt: float, clay: float) -> str:
    """USDA soil texture class from sand, silt and clay percentages."""
    if silt + 1.5 * clay < 15:
        return "Sand"
    if silt + 2 * clay < 30:
        return "Loamy sand"
    if (7 <= clay < 20 and sand > 52 and silt + 2 * clay >= 30) or (clay < 7 and silt < 50 and silt + 2 * clay >= 30):
        return "Sandy loam"
    if 7 <= clay < 27 and 28 <= silt < 50 and sand <= 52:
        return "Loam"
    if (silt >= 50 and 12 <= clay < 27) or (50 <= silt < 80 and clay < 12):
        return "Silt loam"
    if silt >= 80 and clay < 12:
        return "Silt"
    if 20 <= clay < 35 and silt < 28 and sand > 45:
        return "Sandy clay loam"
    if 27 <= clay < 40 and 20 < sand <= 45:
        return "Clay loam"
    if 27 <= clay < 40 and sand <= 20:
        return "Silty clay loam"
    if clay >= 35 and sand > 45:
        return "Sandy clay"
    if clay >= 40 and silt >= 40:
        return "Silty clay"
    return "Clay"


def ph_class(ph: float) -> str:
    """USDA soil reaction class."""
    for limit, label in ((4.5, "Extremely acid"), (5.1, "Very strongly acid"), (5.6, "Strongly acid"), (6.1, "Moderately acid"),
                         (6.6, "Slightly acid"), (7.4, "Neutral"), (7.9, "Slightly alkaline"), (8.5, "Moderately alkaline")):
        if ph < limit:
            return label
    return "Strongly alkaline"


def available_water_mm(sand_pct: float, clay_pct: float, organic_matter_pct: float, depth_cm: float = 30) -> float:
    """Plant-available water held in the layer (field capacity minus wilting point), Saxton & Rawls (2006)."""
    s, c, om = sand_pct / 100, clay_pct / 100, min(organic_matter_pct, 8.0)
    wp_t = -0.024 * s + 0.487 * c + 0.006 * om + 0.005 * s * om - 0.013 * c * om + 0.068 * s * c + 0.031
    wilting = wp_t + (0.14 * wp_t - 0.02)
    fc_t = -0.251 * s + 0.195 * c + 0.011 * om + 0.006 * s * om - 0.027 * c * om + 0.452 * s * c + 0.299
    capacity = fc_t + (1.283 * fc_t**2 - 0.374 * fc_t - 0.015)
    return max(capacity - wilting, 0.0) * depth_cm * 10


def _derived(topsoil: dict) -> dict:
    sand, silt, clay, soc, ph = (topsoil.get(k) for k in ("sandPct", "siltPct", "clayPct", "organicCarbonGPerKg", "ph"))
    om = round(soc * 1.724 / 10, 1) if soc is not None else None  # van Bemmelen factor, g/kg -> %
    texture = None
    if None not in (sand, silt, clay) and sand + silt + clay > 0:
        total = sand + silt + clay  # SoilGrids fractions need not sum to exactly 100
        texture = texture_class(100 * sand / total, 100 * silt / total, 100 * clay / total)
    return {
        "textureClass": texture,
        "organicMatterPct": om,
        "phClass": ph_class(ph) if ph is not None else None,
        "availableWaterMm": round(available_water_mm(sand, clay, om)) if None not in (sand, clay, om) else None,
    }


GROUP_SPAN_DEG = 0.25  # outlines read together must fit in a box this size (about 25 km)


def _shape(polygon) -> dict:
    def closed(ring):
        ring = [list(p[:2]) for p in ring]
        return ring + ([ring[0]] if ring[0] != ring[-1] else [])

    return {"type": "MultiPolygon", "coordinates": [[closed(o), *map(closed, h)] for o, h in geometry.parts(polygon)]}


def _groups(shapes: dict[str, dict]) -> list[list[str]]:
    """Split outlines into nearby groups, each within GROUP_SPAN_DEG in both directions."""
    centres = {}
    for key, shape in shapes.items():
        points = [p for poly in shape["coordinates"] for p in poly[0]]
        centres[key] = (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))
    groups: list[list[str]] = []
    boxes: list[list[float]] = []
    for key in sorted(shapes, key=lambda k: centres[k]):
        lon, lat = centres[key]
        for group, box in zip(groups, boxes, strict=True):
            w, s_, e, n = min(box[0], lon), min(box[1], lat), max(box[2], lon), max(box[3], lat)
            if e - w <= GROUP_SPAN_DEG and n - s_ <= GROUP_SPAN_DEG:
                group.append(key)
                box[:] = [w, s_, e, n]
                break
        else:
            groups.append([key])
            boxes.append([lon, lat, lon, lat])
    return groups


def _result(raw: dict) -> dict:
    """Response for one outline from its stored layer means keyed by (layer, depth)."""
    depths = {label: {} for label, _ in DEPTHS}
    for (layer, depth), value in raw.items():
        key_name, _, divisor = PROPERTIES[layer]
        depths[depth][key_name] = None if value is None else round(value / divisor, 2)
    topsoil = {}
    for key_name, _, _ in PROPERTIES.values():
        pairs = [(depths[label][key_name], cm) for label, cm in DEPTHS if depths[label][key_name] is not None]
        topsoil[key_name] = round(sum(v * cm for v, cm in pairs) / sum(cm for _, cm in pairs), 2) if pairs else None
    result = {
        "topsoil": topsoil,
        "derived": _derived(topsoil),
        "depths": depths,
        "units": {key_name: unit for key_name, unit, _ in PROPERTIES.values()} | {"availableWaterMm": "mm in 0-30 cm"},
        "source": {"name": "ISRIC SoilGrids 2.0", "url": "https://soilgrids.org", "resolutionM": 250, "license": "CC BY 4.0"},
        "note": ("Machine-learning estimates for 250 m cells, averaged over the cells the field touches; not a soil test. "
                 "Topsoil is the thickness-weighted 0-30 cm mean."),
    }
    if all(v is None for v in topsoil.values()):
        result["note"] = "SoilGrids has no soil estimate here (water, ice or built-up land)."
    return result


def soil_many(polygons: dict[str, object]) -> dict[str, dict | ProviderError]:
    """Soil for several outlines, keyed like the input; a provider failure is returned per group of fields.

    Uncached outlines are grouped by proximity and each group costs one read per layer.
    """
    shapes = {key: _shape(polygon) for key, polygon in polygons.items()}
    keys = {key: json.dumps(shape, separators=(",", ":")) for key, shape in shapes.items()}
    out: dict[str, dict | ProviderError] = {}
    with _cache_lock:
        for key, cache_key in keys.items():
            if cache_key in _cache:
                out[key] = _cache[cache_key]
    todo = {key: shape for key, shape in shapes.items() if key not in out}
    jobs = [(layer, depth) for layer in PROPERTIES for depth, _ in DEPTHS]

    def read(job, members):
        try:
            return job, _read_layer(*job, [todo[m] for m in members])
        except ProviderError as exc:
            return job, exc
        except Exception:  # noqa: BLE001  any read failure becomes one clear provider error
            return job, ProviderError("SoilGrids could not be read")

    for members in _groups(todo):
        layers = dict(_readers.map(lambda job, members=members: read(job, members), jobs))
        failure = next((v for v in layers.values() if isinstance(v, ProviderError)), None)
        for i, key in enumerate(members):
            if failure is not None:
                out[key] = failure
                continue
            out[key] = _result({job: values[i] for job, values in layers.items()})
            with _cache_lock:
                _cache[keys[key]] = out[key]
    return out


def field_soil(polygon) -> dict:
    """Topsoil (0-30 cm) estimates over a field outline, with values by depth and plain-language classes."""
    result = soil_many({"field": polygon})["field"]
    if isinstance(result, ProviderError):
        raise result
    return result
