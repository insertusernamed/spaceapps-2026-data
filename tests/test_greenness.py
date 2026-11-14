"""Field greenness from Sentinel-2, tested offline with small local GeoTIFFs and a fake catalogue."""
import os
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin
from rasterio.warp import transform

import server
from environmental import greenness
from environmental.errors import ProviderError, ValidationError

PREVIEW = Path(server.__file__).with_name("data") / "fields.geojson"
X0, Y0 = 600000.0, 4910000.0  # UTM zone 17N, near Orillia


def field_polygon(size_m=100.0):
    """A square field inside the test rasters, in longitude and latitude."""
    xs = [X0 + 20, X0 + 20 + size_m, X0 + 20 + size_m, X0 + 20, X0 + 20]
    ys = [Y0 - 20, Y0 - 20, Y0 - 20 - size_m, Y0 - 20 - size_m, Y0 - 20]
    lons, lats = transform("EPSG:32617", "EPSG:4326", xs, ys)
    return {"type": "Polygon", "coordinates": [[[lon, lat] for lon, lat in zip(lons, lats, strict=True)]]}


def write(path, data, res):
    with rasterio.open(path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0], count=1, dtype=data.dtype,
                       crs="EPSG:32617", transform=from_origin(X0, Y0, res, res), nodata=0) as ds:
        ds.write(data, 1)


def scene(tmp, name, red, nir, scl=4, offset=0.0, applied=None, cloudy_rows=0):
    """Write one scene's bands (raw digital numbers) and return a STAC-like item pointing at them."""
    shape10 = (20, 20)
    write(f"{tmp}/{name}_red.tif", np.full(shape10, red, dtype="uint16"), 10)
    write(f"{tmp}/{name}_nir.tif", np.full(shape10, nir, dtype="uint16"), 10)
    classes = np.full((10, 10), scl, dtype="uint8")
    classes[:cloudy_rows, :] = 9  # cloud
    write(f"{tmp}/{name}_scl.tif", classes, 20)
    bands = [{"scale": 0.0001, "offset": offset}]
    props = {"datetime": f"{name}T16:00:00Z", "eo:cloud_cover": 5.0}
    if applied is not None:
        props["earthsearch:boa_offset_applied"] = applied
    return {"properties": props, "assets": {
        "red": {"href": f"{tmp}/{name}_red.tif", "raster:bands": bands},
        "nir": {"href": f"{tmp}/{name}_nir.tif", "raster:bands": bands},
        "scl": {"href": f"{tmp}/{name}_scl.tif"}}}


class TestPixels(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.shape = {"type": "MultiPolygon", "coordinates": [field_polygon()["coordinates"]]}

    def test_ndvi_of_a_clear_scene(self):
        out = greenness._read(scene(self.tmp.name, "2024-07-01", red=500, nir=4500), self.shape)
        self.assertEqual(out, {"date": "2024-07-01", "ndvi": 0.8, "ndviP10": 0.8, "ndviP90": 0.8, "clearShare": 1.0})

    def test_offset_is_applied_once(self):
        # Collection 1 style: raw values carry +1000, listed as offset -0.1 and not yet applied.
        out = greenness._read(scene(self.tmp.name, "2024-07-02", red=1500, nir=5500, offset=-0.1), self.shape)
        self.assertEqual(out["ndvi"], 0.8)
        # Original-collection style: offset already applied to the pixels; the listed offset is ignored.
        out = greenness._read(scene(self.tmp.name, "2024-07-03", red=500, nir=4500, offset=-0.1, applied=True), self.shape)
        self.assertEqual(out["ndvi"], 0.8)

    def test_mostly_cloudy_field_is_skipped(self):
        cloudy = scene(self.tmp.name, "2024-07-04", red=500, nir=4500, cloudy_rows=6)
        self.assertIsNone(greenness._read(cloudy, self.shape))
        self.assertIsNone(greenness._read(scene(self.tmp.name, "2024-07-05", red=500, nir=4500, scl=8), self.shape))

    def test_partly_cloudy_field_uses_clear_pixels(self):
        out = greenness._read(scene(self.tmp.name, "2024-07-06", red=500, nir=4500, cloudy_rows=2), self.shape)
        self.assertEqual(out["ndvi"], 0.8)
        self.assertLess(out["clearShare"], 1.0)
        self.assertGreaterEqual(out["clearShare"], greenness.MIN_CLEAR_SHARE)


def obs(day, ndvi):
    return {"date": day, "ndvi": ndvi, "ndviP10": ndvi - 0.05, "ndviP90": ndvi + 0.05, "clearShare": 1.0}


class TestSeason(unittest.TestCase):
    def setUp(self):
        greenness._cache.clear()
        self.addCleanup(greenness._cache.clear)
        self.polygon = field_polygon()

    def items(self, days):
        return [{"properties": {"datetime": f"{d}T16:00:00Z"}} for d in days]

    def test_series_summary_and_cache(self):
        days = ["2024-05-01", "2024-06-01", "2024-06-20", "2024-07-15", "2024-09-01", "2024-10-01"]
        values = {"2024-05-01": obs("2024-05-01", 0.18), "2024-06-01": obs("2024-06-01", 0.2), "2024-06-20": obs("2024-06-20", 0.55),
                  "2024-07-15": obs("2024-07-15", 0.85), "2024-09-01": None, "2024-10-01": obs("2024-10-01", 0.3)}
        with patch.object(greenness, "_search", return_value=self.items(days)) as search, \
             patch.object(greenness, "_read", side_effect=lambda item, shape: values[item["properties"]["datetime"][:10]]):
            result = greenness.season(self.polygon, 2024)
            again = greenness.season(self.polygon, 2024)
        self.assertIs(again, result)
        self.assertEqual(search.call_count, 1)  # a finished season is cached
        self.assertEqual([o["date"] for o in result["observations"]], [d for d in days if values[d]])
        self.assertEqual(result["summary"], {"peakNdvi": 0.85, "peakDate": "2024-07-15", "latestNdvi": 0.3,
                                             "latestDate": "2024-10-01", "greenUpDate": "2024-06-20"})
        self.assertEqual((result["scenesChecked"], result["scenesUnreadable"]), (6, 0))
        self.assertEqual(result["period"], {"start": "2024-04-01", "end": "2024-11-15"})

    def test_flat_season_has_no_green_up(self):
        flat = [obs("2024-06-01", 0.8), obs("2024-07-01", 0.84), obs("2024-08-01", 0.82)]
        self.assertIsNone(greenness._summary(flat)["greenUpDate"])
        self.assertEqual(greenness._summary([])["peakNdvi"], None)

    def test_unreadable_scenes_are_counted_and_total_failure_is_502(self):
        def flaky(item, shape):
            if item["properties"]["datetime"].startswith("2024-06"):
                raise rasterio.errors.RasterioIOError("timeout")
            return obs(item["properties"]["datetime"][:10], 0.5)

        with patch.object(greenness, "_search", return_value=self.items(["2024-05-01", "2024-06-01"])), \
             patch.object(greenness, "_read", side_effect=flaky):
            result = greenness.season(self.polygon, 2024)
        self.assertEqual((len(result["observations"]), result["scenesUnreadable"]), (1, 1))
        greenness._cache.clear()
        with patch.object(greenness, "_search", return_value=self.items(["2024-06-01"])), \
             patch.object(greenness, "_read", side_effect=flaky), self.assertRaises(ProviderError):
            greenness.season(self.polygon, 2024)

    def test_falls_back_to_the_original_collection(self):
        calls = []

        def search(shape, start, end, collection):
            calls.append(collection)
            return [] if collection == "sentinel-2-c1-l2a" else self.items(["2022-07-01"])

        with patch.object(greenness, "_search", side_effect=search), \
             patch.object(greenness, "_read", return_value=obs("2022-07-01", 0.7)):
            result = greenness.season(self.polygon, 2022)
        self.assertEqual(calls, list(greenness.COLLECTIONS))
        self.assertEqual(result["source"]["collection"], "sentinel-2-l2a")

    def test_years_and_windows(self):
        for year in (2016, date.today().year + 1):
            with self.subTest(year=year), self.assertRaises(ValidationError):
                greenness.season(self.polygon, year)
        self.assertEqual(greenness.default_year(date(2026, 4, 30)), 2025)
        self.assertEqual(greenness.default_year(date(2026, 5, 1)), 2026)
        self.assertEqual(greenness.season_window(2024, -30), ("2023-10-01", "2024-05-15"))

    def test_search_rings_are_closed(self):
        seen = {}

        def search(shape, start, end, collection):
            seen["shape"] = shape
            return []

        open_ring = field_polygon()["coordinates"][0][:-1]
        with patch.object(greenness, "_search", side_effect=search):
            greenness.season(open_ring, 2024)
        ring = seen["shape"]["coordinates"][0][0]
        self.assertEqual(ring[0], ring[-1])


class TestRoutes(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        env = {k: v for k, v in os.environ.items() if k not in ("FIELDS_DB", "FIELDS_FILE")}
        p = patch.dict(os.environ, {**env, "FIELDS_FILE": str(PREVIEW)}, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_field_route(self):
        with patch.object(greenness, "season", return_value={"year": 2024, "observations": []}) as season:
            resp = self.client.get("/fields/ftw-2025-17TPK_0_0-13020/greenness", params={"year": 2024})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"id": "ftw-2025-17TPK_0_0-13020", "year": 2024, "observations": []})
        self.assertEqual(season.call_args.args[1], 2024)
        self.assertEqual(self.client.get("/fields/nope/greenness").status_code, 404)

    def test_errors_map_to_statuses(self):
        with patch.object(greenness, "season", side_effect=ValidationError("year must be between 2017 and 2026", "year")):
            self.assertEqual(self.client.get("/fields/ftw-2025-17TPK_0_0-13020/greenness?year=2010").status_code, 422)
        with patch.object(greenness, "season", side_effect=ProviderError("Earth Search (Sentinel-2 catalogue) is unavailable")):
            resp = self.client.post("/greenness", json={"polygon": field_polygon()})
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(self.client.post("/greenness", json={"polygon": [[0, 0], [1, 1]]}).status_code, 422)
        self.assertEqual(self.client.post("/greenness", json={"polygon": field_polygon(), "extra": 1}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
