"""Cached SPL4SMGP V008 soil moisture, served as native-grid GeoJSON cells.

Acquisition dependencies are optional; the API only reads the generated JSON.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_FILE = ROOT / "data" / "smap" / "soil-moisture.geojson"
ORILLIA_BBOX = (-79.7, 44.35, -79.05, 44.85)
CMR_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"
PRODUCT_URL = "https://nsidc.org/data/spl4smgp/versions/8"
_acquisition_lock = threading.Lock()
SCHEMA_VERSION = 2

# Native NASA names and units. Bounds are from the V008 product specification;
# temperature and flux values must not use the moisture-only 0..1 check.
GEOPHYSICAL = {
    "sm_surface": ("m3/m3", 0, 0.9, "Soil moisture, 0–5 cm"),
    "sm_rootzone": ("m3/m3", 0, 0.9, "Soil moisture, 0–100 cm"),
    "sm_profile": ("m3/m3", 0, 0.9, "Soil moisture from surface to model bedrock"),
    **{f"sm_{depth}_wetness": ("1", 0, 1, f"{depth} relative saturation: 0 dry, 1 saturated")
       for depth in ("surface", "rootzone", "profile")},
    **{f"sm_{depth}_pctl": ("percent", 0, 100, f"{depth} moisture percentile relative to seasonal climatology")
       for depth in ("rootzone", "profile")},
    "surface_temp": ("K", 180, 350, "Mean land surface temperature, including snow-covered land"),
    **{f"soil_temp_layer{i}": ("K", lo, hi, f"Soil heat diffusion model temperature, layer {i}")
       for i, (lo, hi) in enumerate(((210, 340), (210, 330), (215, 325), (220, 325), (225, 325), (230, 320)), 1)},
    "soil_water_infiltration_flux": ("kg m-2 s-1", 0, 0.05, "Soil water infiltration rate"),
    "overland_runoff_flux": ("kg m-2 s-1", 0, 0.05, "Overland runoff rate"),
    "baseflow_flux": ("kg m-2 s-1", 0, 0.01, "Baseflow rate"),
    "land_evapotranspiration_flux": ("kg m-2 s-1", -0.001, 0.001, "Land evapotranspiration rate"),
    "land_fraction_saturated": ("1", 0, 1, "Fraction of grid-cell land area saturated and snow-free"),
    "land_fraction_wilting": ("1", 0, 1, "Fraction of grid-cell land area wilting and snow-free"),
}
CONSTANTS = {
    "clsm_poros": ("m3/m3", 0.3, 0.93, "Land model soil porosity"),
    "clsm_wp": ("m3/m3", 0.001, 0.35, "Land model soil wilting point"),
    "mwrtm_clay": ("1", 0, 1, "Microwave model clay fraction"),
    "mwrtm_sand": ("1", 0, 1, "Microwave model sand fraction"),
}


def variable_metadata():
    return {name: {"units": units, "description": description,
                   "product": "SPL4SMGP" if name in GEOPHYSICAL else "SPL4SMLM",
                   "static": name in CONSTANTS}
            for name, (units, _, _, description) in {**GEOPHYSICAL, **CONSTANTS}.items()}


def layer_path(path=None):
    return Path(path or os.environ.get("SMAP_FILE", DEFAULT_FILE))


def validate_bounds(bounds):
    west, south, east, north = bounds
    if not all(math.isfinite(v) for v in bounds) or not (
        -179 <= west < east <= 179 and -85 <= south < north <= 85
    ):
        raise ValueError("SMAP bbox must have positive area within -179..179 longitude and -85..85 latitude")
    if east - west > 10 or north - south > 10:
        raise ValueError("Fetch at most a 10 by 10 degree region per layer")


def discover(bounds, day=None):
    """Public metadata request; select one latest 3-hour average, not a daily mean."""
    import requests

    validate_bounds(bounds)
    params = {"short_name": "SPL4SMGP", "version": "008", "page_size": 1,
              "sort_key": "-start_date", "bounding_box": ",".join(map(str, bounds))}
    if day:
        parsed = datetime.strptime(day, "%Y-%m-%d").date()
        if parsed < datetime(2015, 3, 31).date() or parsed > datetime.now(timezone.utc).date():
            raise ValueError("Date must be between 2015-03-31 and today")
        params["temporal"] = f"{day}T00:00:00Z,{day}T23:59:59Z"
    response = requests.get(CMR_URL, params=params, timeout=(10, 60))
    response.raise_for_status()
    entries = response.json()["feed"]["entry"]
    if not entries:
        raise ValueError("No SPL4SMGP V008 files available for this region/date")
    return entries[0]


def _value(value, fill=-9999.0, valid_min=0, valid_max=1, digits=6):
    value = float(value)
    if not math.isfinite(value) or value == float(fill) or not valid_min <= value <= valid_max:
        return None
    return round(value, digits)


def convert(file, bounds, metadata, constants_file=None):
    """Read a seekable HDF5 stream (or local path) into native-grid polygons.

    Only coordinate axes and regional variable slices are requested. Remote
    transfer size still depends on the source HDF5 chunk layout and read cache.
    """
    import h5py
    import numpy as np
    from pyproj import Transformer

    validate_bounds(bounds)
    forward = Transformer.from_crs(4326, 6933, always_xy=True)
    inverse = Transformer.from_crs(6933, 4326, always_xy=True)
    west, south, east, north = bounds
    min_x, min_y = forward.transform(west, south)
    max_x, max_y = forward.transform(east, north)
    features = []
    with ExitStack() as stack:
        source = stack.enter_context(h5py.File(file, "r"))
        constants = stack.enter_context(h5py.File(constants_file, "r")) if constants_file is not None else None
        lat = source["cell_lat"]
        lon = source["cell_lon"]
        if lat.shape != lon.shape or len(lat.shape) != 2 or min(lat.shape) < 2:
            raise ValueError("Unexpected SPL4SMGP coordinate grid")
        # This cylindrical grid has one latitude per row, one longitude per column.
        lats, lons = lat[:, 0], lon[0, :]
        xs, _ = forward.transform(lons, np.zeros_like(lons))
        _, ys = forward.transform(np.zeros_like(lats), lats)
        dx, dy = abs(float(np.median(np.diff(xs)))), abs(float(np.median(np.diff(ys))))
        rows = np.flatnonzero((ys + dy / 2 > min_y) & (ys - dy / 2 < max_y))
        cols = np.flatnonzero((xs + dx / 2 > min_x) & (xs - dx / 2 < max_x))
        if not len(rows) or not len(cols):
            raise ValueError("No SMAP cells intersect the requested region")
        region = (slice(int(rows[0]), int(rows[-1]) + 1),
                  slice(int(cols[0]), int(cols[-1]) + 1))
        if constants is not None and (
            constants["cell_lat"].shape != lat.shape or constants["cell_lon"].shape != lon.shape
            or not np.allclose(constants["cell_lat"][:, 0], lats, atol=1e-5, rtol=0)
            or not np.allclose(constants["cell_lon"][0, :], lons, atol=1e-5, rtol=0)
        ):
            raise ValueError("SMAP soil constants do not match the geophysical grid")
        arrays, fills, missing = {}, {}, []
        specs = {**GEOPHYSICAL, **CONSTANTS}
        for name in specs:
            owner = source if name in GEOPHYSICAL else constants
            # Operational V008 files use a hyphenated group name; also accept
            # the unhyphenated spelling shown in the product specification.
            group = ("Geophysical_Data" if name in GEOPHYSICAL else
                     "Land-Model-Constants_Data" if owner is not None and "Land-Model-Constants_Data" in owner
                     else "LandModelConstants_Data")
            key = f"{group}/{name}"
            if owner is None or key not in owner:
                if name in ("sm_surface", "sm_rootzone"):
                    raise ValueError(f"Required SMAP variable missing: {name}")
                missing.append(name)
                continue
            dataset = owner[key]
            if dataset.shape != lat.shape:
                raise ValueError(f"SMAP variable {name} does not match the coordinate grid")
            arrays[name] = dataset[region]
            fills[name] = dataset.attrs.get("_FillValue", -9999.0)
        for i, r in enumerate(rows):
            for j, c in enumerate(cols):
                values = {name: (_value(arrays[name][i, j], fills[name], spec[1], spec[2],
                                        digits=12 if spec[0] == "kg m-2 s-1" else 6)
                                 if name in arrays else None) for name, spec in specs.items()}
                if values["sm_surface"] is None and values["sm_rootzone"] is None:
                    continue
                x, y = float(xs[c]), float(ys[r])
                ring = [list(inverse.transform(a, b)) for a, b in (
                    (x-dx/2, y-dy/2), (x+dx/2, y-dy/2),
                    (x+dx/2, y+dy/2), (x-dx/2, y+dy/2), (x-dx/2, y-dy/2))]
                features.append({"type": "Feature", "id": f"smap-{r}-{c}",
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                    "properties": {"row": int(r), "column": int(c),
                        **values}})
    if not features:
        raise ValueError("No valid soil moisture cells in the requested region")
    return {"type": "FeatureCollection", "features": features,
        "metadata": {"source": "NASA SMAP L4", "product": "SPL4SMGP", "version": "008",
            "schema_version": SCHEMA_VERSION, "variables": variable_metadata(),
            "missing_variables": missing,
            "doi": "10.5067/T5RUATAQREF8", "url": PRODUCT_URL,
            "spatial_resolution_km": 9, "temporal_resolution_hours": 3,
            "units": "m3/m3", "depth_cm": {"sm_surface": [0, 5], "sm_rootzone": [0, 100]},
            "requested_bbox": list(bounds), "granule": metadata["producer_granule_id"],
            "time_start": metadata["time_start"], "time_end": metadata["time_end"],
            "built": datetime.now(timezone.utc).isoformat(),
            "limitations": "One 3-hour average; model-assimilated 9 km cells, not field-scale measurements. Ancillary dynamic fields are research outputs with different validation status from core soil moisture. Soil constants are static model inputs, not measured soil tests. Temperatures are kelvin; fluxes are rates, not daily totals. Missing values are null; cells with both core moisture variables missing are omitted."}}


def open_hdf(stack, earthaccess, results):
    if not results:
        raise RuntimeError("Selected SMAP granule could not be resolved for streaming")
    streams = earthaccess.open(results)
    for stream in streams:
        stack.callback(stream.close)
    hdf = [stream for stream in streams
           if urlparse(str(getattr(stream, "path", ""))).path.endswith(".h5")]
    if len(hdf) != 1:
        raise RuntimeError("Expected exactly one remote SMAP HDF5 stream")
    return hdf[0]


def acquire(bounds=ORILLIA_BBOX, day=None, output=None):
    """Stream protected HDF5 through Earthaccess; persist only regional GeoJSON."""
    try:
        import earthaccess
        import h5py  # Check all dependencies before making network requests.
        import pyproj
    except ImportError as exc:
        raise RuntimeError("Install SMAP dependencies: pip install -r requirements.txt") from exc
    from app import ENV_PATH, load_credentials

    # Also accept the existing local .env. filename without copying its secrets.
    load_credentials(ENV_PATH if ENV_PATH.exists() else ROOT / ".env.")
    metadata = discover(bounds, day)
    auth = earthaccess.login(strategy="environment", persist=False)
    if not auth.authenticated:
        raise RuntimeError("Earthdata authentication failed")
    results = earthaccess.search_data(short_name="SPL4SMGP", version="008",
        granule_name=metadata["producer_granule_id"], count=1)
    science_version = re.search(r"_V(v\d{4})_", metadata["producer_granule_id"])
    if not science_version:
        raise RuntimeError("SMAP filename is missing its science version")
    constants_results = earthaccess.search_data(short_name="SPL4SMLM", version="008",
        granule_name=f"SMAP_L4_SM_lmc_*_V{science_version[1]}_*.h5", count=1)
    if not constants_results:
        raise RuntimeError("No compatible SPL4SMLM soil constants found for this SMAP science version")
    with ExitStack() as stack:
        stream = open_hdf(stack, earthaccess, results)
        constants_stream = open_hdf(stack, earthaccess, constants_results)
        layer = convert(stream, bounds, metadata, constants_file=constants_stream)
        layer["metadata"]["soil_constants"] = {
            "product": "SPL4SMLM", "version": "008", "science_version": science_version[1],
            "granule": Path(urlparse(str(constants_stream.path)).path).name,
            "url": "https://nsidc.org/data/spl4smlm/versions/8", "static": True}
    output = layer_path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Stage only the regional JSON so a failed refresh preserves the last layer.
    with tempfile.TemporaryDirectory(prefix="smap-", dir=output.parent) as folder:
        staged = Path(folder) / "layer.geojson"
        staged.write_text(json.dumps(layer, allow_nan=False), encoding="utf-8")
        os.replace(staged, output)
    return layer


def read_layer(path=None):
    path = layer_path(path)
    layer = json.loads(path.read_text(encoding="utf-8"))
    if layer.get("type") != "FeatureCollection" or not isinstance(layer.get("features"), list):
        raise ValueError("Invalid soil moisture layer")
    if layer.get("metadata", {}).get("product") != "SPL4SMGP":
        raise ValueError("Soil moisture layer is missing SPL4SMGP provenance")
    return layer


def get_layer():
    """Populate/upgrade the cache on first use; serialize acquisition per worker."""
    try:
        layer = read_layer()
        if layer["metadata"].get("schema_version") == SCHEMA_VERSION:
            return layer
    except FileNotFoundError:
        pass
    with _acquisition_lock:
        layer = None
        try:
            layer = read_layer()
            if layer["metadata"].get("schema_version") == SCHEMA_VERSION:
                return layer
        except FileNotFoundError:
            pass
        if layer is not None:
            # Preserve the existing region and day when upgrading old layers.
            meta = layer["metadata"]
            return acquire(bounds=meta.get("requested_bbox", ORILLIA_BBOX),
                           day=meta.get("time_start", "")[:10] or None, output=layer_path())
        return acquire(output=layer_path())
