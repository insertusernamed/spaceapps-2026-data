"""Field-level RGB coverage and cloud/shadow checks on native satellite QA grids."""
from __future__ import annotations

import math
import threading
from contextlib import ExitStack

import numpy as np

from . import hls

MIN_VALID_SHARE = 0.9
MIN_CLEAR_SHARE = 0.9
MAX_GRID_SIDE = 1024
READ_SLOTS = threading.BoundedSemaphore(4)


def _href(item, band):
    """Reuse the HLS access-token cache for either Planetary Computer collection."""
    return hls._href({"collection": item["collection"],
                      "assets": {band: item["assets"][band]["href"]}}, band)


def read_quality(item, shape):
    """Measure usable pixels inside the polygon, including holes and missing scene coverage.

    QA is read at native resolution (30 m HLS, 20 m SCL), capped at 1024 pixels
    per side for large outlines. RGB validity is aligned to that exact grid with
    nearest-neighbour resampling. Fields with no pixel centres inside are rejected;
    surrounding bounding-box pixels must never stand in for field observations.
    Read errors propagate so an outage is distinguishable from rejected scenes.
    """
    import rasterio
    from rasterio import features, windows
    from rasterio.enums import Resampling
    from rasterio.transform import Affine
    from rasterio.vrt import WarpedVRT
    from rasterio.warp import transform_geom

    is_hls = item["collection"] in hls.COLLECTIONS
    qa_band = "Fmask" if is_hls else "SCL"
    rgb_bands = ("B04", "B03", "B02") if is_hls else ("visual",)
    with READ_SLOTS, hls._env(), ExitStack() as stack:
        qa = stack.enter_context(rasterio.open(_href(item, qa_band)))
        local = transform_geom("EPSG:4326", qa.crs, shape)
        raw_window = windows.from_bounds(*features.bounds(local), qa.transform)
        left, top = math.floor(raw_window.col_off), math.floor(raw_window.row_off)
        right = math.ceil(raw_window.col_off + raw_window.width)
        bottom = math.ceil(raw_window.row_off + raw_window.height)
        window = windows.Window(left, top, max(1, right - left), max(1, bottom - top))
        factor = max(1, window.width / MAX_GRID_SIDE, window.height / MAX_GRID_SIDE)
        width, height = math.ceil(window.width / factor), math.ceil(window.height / factor)
        transform = qa.window_transform(window) * Affine.scale(window.width / width, window.height / height)
        inside = features.geometry_mask([local], (height, width), transform, invert=True)
        field_pixels = int(inside.sum())
        classes = qa.read(1, window=window, out_shape=(height, width), boundless=True,
                          masked=True, resampling=Resampling.nearest)
        codes = classes.filled(255 if is_hls else 0)
        valid = inside & ~np.ma.getmaskarray(classes)
        valid &= codes != (255 if is_hls else 0)
        if not is_hls:
            valid &= (codes >= 2) & (codes <= 11)  # no-data and saturated/defective pixels are invalid
        for band in rgb_bands:
            src = stack.enter_context(rasterio.open(_href(item, band)))
            vrt = stack.enter_context(WarpedVRT(src, crs=qa.crs, transform=transform,
                                              width=width, height=height,
                                              nodata=-9999 if is_hls else 0,
                                              resampling=Resampling.nearest))
            data = vrt.read((1,) if is_hls else (1, 2, 3), masked=True)
            valid &= np.all(~np.ma.getmaskarray(data) & np.isfinite(data.data), axis=0)
        if is_hls:
            # Fmask: cloud (1), cloud adjacency (2), shadow (3), snow (4).
            # Bit 0 is reserved in v2, but excluded conservatively if set.
            cloud = (codes & 0b00111) != 0
            shadow = (codes & 0b01000) != 0
            clear = (codes & 0b11111) == 0
        else:
            cloud = np.isin(codes, (8, 9, 10))
            shadow = np.isin(codes, (2, 3))
            clear = np.isin(codes, (4, 5, 6))  # vegetation, bare soil, water; not unclassified/snow
        clear_pixels = int((valid & clear).sum())
        valid_pixels = int(valid.sum())
        denominator = field_pixels or 1
        return {
            "fieldPixels": field_pixels,
            "validPixels": valid_pixels,
            "clearPixels": clear_pixels,
            "validShare": valid_pixels / denominator,
            "clearShare": clear_pixels / denominator,
            "cloudShare": int((inside & cloud & (codes != 255)).sum()) / denominator,
            "shadowShare": int((inside & shadow & (codes != 255)).sum()) / denominator,
            "sampled": factor > 1,
        }


def acceptable(quality):
    """Keep scenes only when RGB coverage and clear pixels each cover 90% of the field."""
    return (quality["fieldPixels"] > 0 and quality["validShare"] >= MIN_VALID_SHARE
            and quality["clearShare"] >= MIN_CLEAR_SHARE)
