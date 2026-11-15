"""Incremental NASA HLS acquisition: native-grid measurements, QA and shared block caches."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import io
import json
import math
import os
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import requests

from . import geometry
from .errors import ProviderError, ValidationError

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/"
COLLECTIONS = {"hls2-l30": "B05", "hls2-s30": "B8A"}
VERSION = 1
BLOCK = 256
MAX_SCENES = 500
MAX_PIXELS_ACROSS = 512
MAX_ACTIVE = 8
JOBS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hls-job")
SCENES = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hls-scene")
BANDS = ThreadPoolExecutor(max_workers=8, thread_name_prefix="hls-band")
READ_SLOTS = threading.BoundedSemaphore(8)
GUARD = threading.Lock()
ACTIVE: set[str] = set()
LOCKS: dict[str, threading.Lock] = {}
TOKENS: dict[str, tuple[float, str]] = {}


def cache_dir() -> Path:
    return Path(os.environ.get("HLS_CACHE_DIR", Path(__file__).resolve().parent.parent / "data" / "hls"))


def _lock(key):
    with GUARD:
        if len(LOCKS) > 8192:
            for old in list(LOCKS):
                if not LOCKS[old].locked():
                    LOCKS.pop(old, None)
        return LOCKS.setdefault(key, threading.Lock())


def _atomic(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _save(path, value):
    _atomic(path, json.dumps(value, allow_nan=False).encode())


def _load(path, age=None):
    try:
        if age is not None and time.time() - path.stat().st_mtime > age:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _json(method, url, **kwargs):
    for attempt in range(3):
        try:
            response = requests.request(method, url, timeout=(10, 45), **kwargs)
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(min(5, 0.5 * 2 ** attempt))
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise ProviderError("NASA HLS catalogue or access-token service is unavailable") from None
            time.sleep(0.5 * 2 ** attempt)


def _shape(polygon):
    return {"type": "MultiPolygon", "coordinates": [
        [ring + [ring[0]] for ring in [outer, *holes]] for outer, holes in geometry.parts(polygon)]}


def _key(shape, start, end, identity=None):
    value = {"version": VERSION, "identity": identity, "geometry": shape, "start": str(start), "end": str(end)}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def search(shape, start, end, refresh=False):
    bounds = geometry.bbox(shape)
    bbox = [math.floor(bounds[0] * 10) / 10, math.floor(bounds[1] * 10) / 10,
            math.ceil(bounds[2] * 10) / 10, math.ceil(bounds[3] * 10) / 10]
    params = {"bbox": bbox, "start": str(start), "end": str(end), "v": VERSION}
    key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    path = cache_dir() / "catalogue" / f"{key}.json"
    with _lock(f"catalogue:{key}"):
        cached = _load(path, 6 * 3600 if (datetime.now(timezone.utc).date() - end).days < 10 else None)
        if cached is not None and not refresh:
            return cached
        body = {"collections": list(COLLECTIONS), "bbox": bbox,
                "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z", "limit": 250}
        items, method, url = {}, "POST", STAC
        for _ in range(20):
            page = _json(method, url, **({"json": body} if method.upper() == "POST" else {}))
            for item in page["features"]:
                collection = item["collection"]
                if collection not in COLLECTIONS:
                    continue
                bands = ("Fmask", "B04", COLLECTIONS[collection])
                if not all(band in item["assets"] for band in bands):
                    continue
                scene = {"id": item["id"], "collection": collection, "tile": item["id"].split(".")[2],
                         "datetime": item["properties"]["datetime"],
                         "cloud": item["properties"].get("eo:cloud_cover"),
                         "assets": {band: item["assets"][band]["href"] for band in bands}}
                items[scene["id"]] = scene
            link = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
            if not link:
                break
            url, method = link["href"], link.get("method", "GET")
            body = {**body, **link.get("body", {})} if link.get("merge") else link.get("body", body)
        else:
            raise ProviderError("HLS catalogue exceeded the supported page count")
        result = sorted(items.values(), key=lambda item: (item["datetime"], item["id"]))
        _save(path, result)
        return result


def _href(item, band):
    collection = item["collection"]
    with _lock(f"token:{collection}"):
        expires, token = TOKENS.get(collection, (0, ""))
        if expires < time.time():
            response = _json("GET", TOKEN_URL + collection)
            token = response["token"]
            expiry = datetime.fromisoformat(response["msft:expiry"].replace("Z", "+00:00")).timestamp()
            TOKENS[collection] = (expiry - 300, token)
    return "/vsicurl/" + item["assets"][band] + "?" + token


def _env():
    import rasterio
    return rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                        CPL_VSIL_CURL_USE_HEAD="NO", GDAL_HTTP_MAX_RETRY="2", GDAL_HTTP_RETRY_DELAY="1",
                        GDAL_HTTP_TIMEOUT="30", GDAL_INGESTED_BYTES_AT_OPEN="32768")


def tile_grid(item):
    import rasterio
    path = cache_dir() / "grids" / f"{item['tile']}.json"
    with _lock(f"grid:{item['tile']}"):
        grid = _load(path)
        if grid:
            return grid
        with READ_SLOTS, _env(), rasterio.open(_href(item, "Fmask")) as src:
            grid = {"crs": src.crs.to_string(), "transform": list(src.transform)[:6],
                    "width": src.width, "height": src.height, "tile": item["tile"]}
        _save(path, grid)
        return grid


def field_grid(shape, grid):
    from rasterio.features import geometry_mask
    from rasterio.transform import Affine
    from rasterio.warp import transform_geom
    local = transform_geom("EPSG:4326", grid["crs"], shape)
    points = [point for polygon in local["coordinates"] for ring in polygon for point in ring]
    a, _, x0, _, e, y0 = grid["transform"]
    col = math.floor((min(p[0] for p in points) - x0) / a + 1e-6)
    row = math.floor((max(p[1] for p in points) - y0) / e + 1e-6)
    right = math.ceil((max(p[0] for p in points) - x0) / a - 1e-6)
    bottom = math.ceil((min(p[1] for p in points) - y0) / e - 1e-6)
    width, height = max(1, right - col), max(1, bottom - row)
    right, bottom = col + width, row + height
    if width > MAX_PIXELS_ACROSS or height > MAX_PIXELS_ACROSS:
        raise ValidationError("HLS field windows may be at most 15.36 km across")
    if col < 0 or row < 0 or right > grid["width"] or bottom > grid["height"]:
        return None
    transform = Affine(a, 0, x0 + col * a, 0, e, y0 + row * e)
    inside = geometry_mask([local], (height, width), transform, invert=True)
    padded = np.pad(inside, 1)
    core = inside.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            core &= padded[1 + dy:1 + dy + height, 1 + dx:1 + dx + width]
    interior = int(core.sum()) >= 4
    return {"col": col, "row": row, "width": width, "height": height,
            "inside": inside, "sample": core if interior else inside, "interiorOnly": interior,
            "crs": grid["crs"], "tileTransform": grid["transform"]}


def read_band(item, band, window):
    import rasterio
    from rasterio.windows import Window
    col, row, width, height = (window[key] for key in ("col", "row", "width", "height"))
    blocks = [(bx, by) for by in range(row // BLOCK, (row + height - 1) // BLOCK + 1)
              for bx in range(col // BLOCK, (col + width - 1) // BLOCK + 1)]
    result = None
    for bx, by in blocks:
        path = cache_dir() / "blocks" / item["id"] / f"{band}-{bx}-{by}.npz"
        with _lock(str(path)):
            array = None
            try:
                with np.load(path) as saved:
                    array = saved["data"]
                os.utime(path, None)
            except (OSError, ValueError, KeyError):
                pass
            if array is None:
                with READ_SLOTS, _env(), rasterio.open(_href(item, band)) as src:
                    if src.crs.to_string() != window["crs"] or not np.allclose(list(src.transform)[:6], window["tileTransform"], rtol=0, atol=1e-6):
                        raise ProviderError("HLS bands are not on the expected native grid")
                    if band != "Fmask" and src.scales[0] not in (0, 1, 0.0001):
                        raise ProviderError("HLS band has an unexpected reflectance scale")
                    array = src.read(1, window=Window(bx * BLOCK, by * BLOCK,
                                     min(BLOCK, src.width - bx * BLOCK), min(BLOCK, src.height - by * BLOCK)))
                buffer = io.BytesIO()
                np.savez_compressed(buffer, data=array)
                _atomic(path, buffer.getvalue())
        if result is None:
            result = np.full((height, width), 255 if band == "Fmask" else -9999, dtype=array.dtype)
        left, top = max(col, bx * BLOCK), max(row, by * BLOCK)
        right, bottom = min(col + width, bx * BLOCK + array.shape[1]), min(row + height, by * BLOCK + array.shape[0])
        result[top - row:bottom - row, left - col:right - col] = array[top - by * BLOCK:bottom - by * BLOCK, left - bx * BLOCK:right - bx * BLOCK]
    return result


def observe(item, window):
    """Decode QA and calculate measurements; application quality categories belong to Java."""
    qa = read_band(item, "Fmask", window)
    inside, sample = window["inside"], window["sample"]
    imaged = qa != 255
    atmospheric = imaged & ((qa & 0b011110) == 0) & ((qa >> 6) != 3)
    land = atmospheric & ((qa & 0b100000) == 0)
    fraction = lambda mask: round(float(mask[inside].mean()), 4)
    result = {"sceneId": item["id"], "date": item["datetime"][:10], "datetime": item["datetime"],
              "sensor": "Landsat" if item["collection"] == "hls2-l30" else "Sentinel-2",
              "collection": item["collection"], "coverageShare": fraction(imaged),
              "clearShare": fraction(atmospheric), "cloudShare": fraction(imaged & ((qa & 0b001110) != 0)),
              "snowShare": fraction(imaged & ((qa & 0b010000) != 0) & ((qa & 0b001110) == 0)),
              "waterShare": fraction(atmospheric & ((qa & 0b100000) != 0)),
              "sampleClearShare": 0.0, "validPixels": 0, "ndviMean": None, "ndviP10": None, "ndviP90": None}
    # Preserve QA for every pass; skip spectral transfers with fewer than 20% candidate land pixels.
    if not sample.any() or float(land[sample].mean()) < 0.2:
        return result
    red_future = BANDS.submit(read_band, item, "B04", window)
    nir_future = BANDS.submit(read_band, item, COLLECTIONS[item["collection"]], window)
    raw_red, raw_nir = red_future.result(), nir_future.result()
    red, nir = raw_red.astype("float32") * 0.0001, raw_nir.astype("float32") * 0.0001
    valid = land & (raw_red != -9999) & (raw_nir != -9999) & ((red + nir) > 0)
    ndvi = np.divide(nir - red, nir + red, out=np.full_like(red, np.nan), where=valid)
    valid &= np.isfinite(ndvi) & (ndvi >= -1) & (ndvi <= 1)
    values = ndvi[valid & sample]
    if values.size:
        result.update(ndviMean=round(float(values.mean()), 4), ndviP10=round(float(np.percentile(values, 10)), 4),
                      ndviP90=round(float(np.percentile(values, 90)), 4), validPixels=int(values.size),
                      sampleClearShare=round(float(values.size / sample.sum()), 4))
    return result


def _spread(items):
    ordered = sorted(items, key=lambda item: (item["datetime"], item["id"]))
    # Process dates across the whole season before filling the intervals between them.
    output, queue = [], [(0, len(ordered))]
    while queue:
        lo, hi = queue.pop(0)
        if hi <= lo:
            continue
        mid = (lo + hi) // 2
        output.append(ordered[mid])
        queue.extend([(lo, mid), (mid + 1, hi)])
    return output


def _trim():
    files = list((cache_dir() / "blocks").glob("*/*.npz"))
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            pass
    cap = int(os.environ.get("HLS_CACHE_MB", "2000")) * 1_000_000
    if total > cap:
        for path in sorted(files, key=lambda p: p.stat().st_mtime if p.exists() else 0):
            try:
                size = path.stat().st_size
                path.unlink()
                total -= size
                if total <= cap * 0.8:
                    break
            except FileNotFoundError:
                pass


def _run(key, shape, start, end, state, refresh=False):
    path = cache_dir() / "jobs" / f"{key}.json"
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            started = time.monotonic()
            state.update(status="running", updatedAt=time.time())
            _save(path, state)
            items = search(shape, start, end, refresh=refresh)
            counts = Counter(item["tile"] for item in items)
            window, selected = None, []
            for tile, _ in counts.most_common():
                item = next(item for item in items if item["tile"] == tile)
                grid = tile_grid(item)
                window = field_grid(shape, grid)
                if window is not None:
                    selected = [item for item in items if item["tile"] == tile]
                    break
            if items and window is None:
                raise ValidationError("No single HLS tile contains this complete field")
            if len(selected) > MAX_SCENES:
                raise ValidationError("More than 500 HLS scenes match; request a shorter period")
            state["progress"]["total"] = len(selected)
            if window is not None:
                state["grid"] = {"resolutionM": 30, "fieldPixels": int(window["inside"].sum()),
                                 "samplePixels": int(window["sample"].sum()), "interiorOnly": window["interiorOnly"]}
                if not window["inside"].any():
                    selected = []
                    state["progress"]["total"] = 0
            _save(path, state)
            queued = iter(_spread(selected))
            pending = {}
            def schedule():
                item = next(queued, None)
                if item:
                    pending[SCENES.submit(observe, item, window)] = item
            for _ in range(4):
                schedule()
            while pending:
                done, _ = wait(pending, timeout=10, return_when=FIRST_COMPLETED)
                for future in done:
                    item = pending.pop(future)
                    try:
                        state["observations"].append(future.result())
                        state.setdefault("firstObservationSeconds", round(time.monotonic() - started, 3))
                    except Exception:
                        state["failures"].append({"sceneId": item["id"], "message": "Scene could not be read; no measurement was substituted."})
                    state["progress"]["processed"] += 1
                    schedule()
                state["updatedAt"] = time.time()
                _save(path, state)
            state.update(status="partial" if state["failures"] else "done", finishedAt=time.time(),
                         elapsedSeconds=round(time.monotonic() - started, 3))
            _save(path, state)
    except Exception as error:
        state.update(status="failed", finishedAt=time.time(), error=str(error) if isinstance(error, (ProviderError, ValidationError)) else "HLS acquisition failed", updatedAt=time.time())
        _save(path, state)
    finally:
        with GUARD:
            ACTIVE.discard(key)
        _trim()


def season(polygon, start, end, identity=None, retry=False):
    """Start a bounded job or return its persisted snapshot; polling never repeats acquisition."""
    if not date(2013, 4, 11) <= start <= end <= datetime.now(timezone.utc).date() or (end - start).days > 365:
        raise ValidationError("HLS dates must be ordered, from 2013-04-11 through today, and span at most 366 days")
    shape = _shape(polygon)
    bounds = geometry.bbox(shape)
    if geometry.area_ha(shape) <= 0 or bounds[2] - bounds[0] > 1 or bounds[3] - bounds[1] > 1:
        raise ValidationError("HLS outline must have positive area and fit within 1 by 1 degree")
    key = _key(shape, start, end, identity)
    path = cache_dir() / "jobs" / f"{key}.json"
    with GUARD:
        state = _load(path)
        if key in ACTIVE:
            return state
        if state:
            recent = (datetime.now(timezone.utc).date() - end).days < 10
            fresh = time.time() - state.get("finishedAt", 0) < 6 * 3600 or not recent
            running = state["status"] in ("queued", "running") and time.time() - state["updatedAt"] < 120
            if running or state["status"] in ("done", "partial", "failed") and fresh and not retry:
                return state
        if len(ACTIVE) >= MAX_ACTIVE:
            raise ProviderError("HLS acquisition queue is full; retry shortly")
        state = {"jobId": key, "status": "queued", "period": {"start": str(start), "end": str(end)},
                 "progress": {"processed": 0, "total": None}, "grid": None, "observations": [], "failures": [],
                 "updatedAt": time.time(), "source": {"name": "NASA HLS v2.0", "provider": "Microsoft Planetary Computer",
                 "resolutionM": 30, "url": "https://hls.gsfc.nasa.gov/", "collections": list(COLLECTIONS)}}
        _save(path, state)
        ACTIVE.add(key)
        JOBS.submit(_run, key, copy.deepcopy(shape), start, end, copy.deepcopy(state), retry)
        return state
