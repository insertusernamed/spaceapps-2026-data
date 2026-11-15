"""Verify native-pixel acquisition, masking, caching and resumable polling without network access."""
import tempfile
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
from environmental import hls
from environmental.errors import ValidationError

SHAPE = {"type": "Polygon", "coordinates": [[[-79.1, 44.5], [-79.09, 44.5], [-79.09, 44.51], [-79.1, 44.51], [-79.1, 44.5]]]}
ITEM = {"id": "HLS.L30.T17TPK.2026200T150000.v2.0", "tile": "T17TPK", "collection": "hls2-l30", "datetime": "2026-07-19T15:00:00Z"}


class HlsMeasurementsTest(unittest.TestCase):
    def measure(self, qa, red=None, nir=None, collection="hls2-l30"):
        qa = np.array([qa], dtype="uint8")
        arrays = {"Fmask": qa, "B04": np.array([red if red is not None else [1000] * qa.size]),
                  hls.COLLECTIONS[collection]: np.array([nir if nir is not None else [3000] * qa.size])}
        window = {"inside": np.ones_like(qa, dtype=bool), "sample": np.ones_like(qa, dtype=bool)}
        with patch.object(hls, "read_band", side_effect=lambda item, band, grid: arrays[band]) as reader:
            result = hls.observe({**ITEM, "collection": collection}, window)
        return result, [call.args[1] for call in reader.call_args_list]

    def test_cloud_snow_water_aerosol_and_fill_do_not_become_land_ndvi(self):
        result, bands = self.measure([0, 2, 4, 8, 16, 32, 192, 255, 0, 0],
                                     nir=[3000, 9000, 9000, 9000, 9000, 9000, 9000, 9000, -9999, 3000])
        self.assertEqual(result["validPixels"], 2)
        self.assertEqual(result["ndviMean"], 0.5)
        self.assertEqual(result["sampleClearShare"], 0.2)
        self.assertEqual(result["coverageShare"], 0.9)
        self.assertEqual(result["snowShare"], 0.1)
        self.assertEqual(result["waterShare"], 0.1)
        self.assertCountEqual(bands, ["Fmask", "B04", "B05"])

    def test_snow_and_water_passes_skip_spectral_transfers(self):
        for flag in (2, 16, 32, 192, 255):
            with self.subTest(flag=flag):
                result, bands = self.measure([flag] * 10)
                self.assertIsNone(result["ndviMean"])
                self.assertEqual(bands, ["Fmask"])

    def test_sentinel_uses_narrow_nir_and_negative_land_values_are_preserved(self):
        result, bands = self.measure([0] * 4, red=[3000] * 4, nir=[1000] * 4, collection="hls2-s30")
        self.assertEqual(result["ndviMean"], -0.5)
        self.assertIn("B8A", bands)
        self.assertNotIn("B05", bands)

    def test_native_grid_preserves_holes_and_excludes_edges(self):
        from rasterio.warp import transform_geom
        local = {"type": "MultiPolygon", "coordinates": [[[[0, 0], [300, 0], [300, 300], [0, 300], [0, 0]],
                   [[120, 120], [180, 120], [180, 180], [120, 180], [120, 120]]]]}
        geographic = transform_geom("EPSG:32617", "EPSG:4326", local)
        grid = {"crs": "EPSG:32617", "transform": [30, 0, 0, 0, -30, 300], "width": 10, "height": 10}
        window = hls.field_grid(geographic, grid)
        self.assertEqual(window["inside"].sum(), 96)
        self.assertFalse(window["inside"][4, 4])
        self.assertTrue(window["interiorOnly"])
        self.assertLess(window["sample"].sum(), window["inside"].sum())
        self.assertNotEqual(hls._key(geographic, date(2025, 1, 1), date(2025, 12, 31)),
                            hls._key({**geographic, "coordinates": [[geographic["coordinates"][0][0]]]}, date(2025, 1, 1), date(2025, 12, 31)))

    def test_neighbouring_windows_reuse_a_native_block(self):
        from rasterio.io import MemoryFile
        from rasterio.transform import from_origin
        pixels = np.arange(256 * 256, dtype="int32").reshape(256, 256)
        with tempfile.TemporaryDirectory() as cache, MemoryFile() as memory:
            with memory.open(driver="GTiff", height=256, width=256, count=1, dtype="int32", crs="EPSG:32617", transform=from_origin(0, 7680, 30, 30)) as writer:
                writer.write(pixels, 1)
            window = {"col": 10, "row": 20, "width": 3, "height": 2, "crs": "EPSG:32617", "tileTransform": [30, 0, 0, 0, -30, 7680]}
            with patch.object(hls, "cache_dir", return_value=Path(cache)), patch.object(hls, "_href", return_value=memory.name) as remote:
                first = hls.read_band(ITEM, "B04", window)
                second = hls.read_band(ITEM, "B04", {**window, "col": 11})
            np.testing.assert_array_equal(first, pixels[20:22, 10:13])
            np.testing.assert_array_equal(second, pixels[20:22, 11:14])
            self.assertEqual(remote.call_count, 1)

    def test_polling_keeps_completed_job_and_failed_scene_explicit(self):
        window = {"inside": np.ones((2, 2), dtype=bool), "sample": np.ones((2, 2), dtype=bool), "interiorOnly": False}
        with tempfile.TemporaryDirectory() as cache, patch.object(hls, "cache_dir", return_value=Path(cache)), \
             patch.object(hls, "search", return_value=[ITEM]) as catalogue, patch.object(hls, "tile_grid"), \
             patch.object(hls, "field_grid", return_value=window), patch.object(hls, "observe", side_effect=RuntimeError("read failed")):
            queued = hls.season(SHAPE, date(2025, 1, 1), date(2025, 12, 31))
            self.assertEqual(queued["status"], "queued")
            limit = time.monotonic() + 3
            while time.monotonic() < limit:
                state = hls.season(SHAPE, date(2025, 1, 1), date(2025, 12, 31))
                if state["status"] == "partial":
                    break
                time.sleep(0.01)
            self.assertEqual(state["status"], "partial")
            self.assertEqual(state["progress"], {"processed": 1, "total": 1})
            self.assertEqual(state["observations"], [])
            self.assertEqual(len(state["failures"]), 1)
            self.assertEqual(hls.season(SHAPE, date(2025, 1, 1), date(2025, 12, 31))["status"], "partial")
            self.assertEqual(catalogue.call_count, 1)
            limit = time.monotonic() + 3
            while queued["jobId"] in hls.ACTIVE and time.monotonic() < limit:
                time.sleep(.01)

    def test_utc_today_is_accepted_independently_of_local_midnight(self):
        today = datetime.now(timezone.utc).date()
        with tempfile.TemporaryDirectory() as cache, patch.object(hls, "cache_dir", return_value=Path(cache)), patch.object(hls.JOBS, "submit"):
            state = hls.season(SHAPE, today, today)
            self.assertEqual(state["period"]["end"], str(today))
            with hls.GUARD:
                hls.ACTIVE.discard(state["jobId"])
        with self.assertRaises(ValidationError):
            hls.season(SHAPE, date(2023, 1, 1), date(2025, 1, 1))


class HlsApiTest(unittest.TestCase):
    def test_poll_contract_and_field_validation(self):
        from fastapi.testclient import TestClient
        import server
        client = TestClient(server.app)
        with patch.object(server, "_field_geometry", return_value=SHAPE), patch.object(hls, "season", return_value={"status": "running", "progress": {"processed": 3, "total": 20}}) as season:
            response = client.get("/fields/field-1/season", params={"start": "2025-01-01", "end": "2025-12-31", "retry": True})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["progress"], {"processed": 3, "total": 20})
            self.assertEqual(response.json()["id"], "field-1")
            season.assert_called_once_with(SHAPE, date(2025, 1, 1), date(2025, 12, 31), identity="field-1", retry=True)
        with patch.object(server, "_field_geometry", return_value=None):
            self.assertEqual(client.get("/fields/missing/season?start=2025-01-01&end=2025-12-31").status_code, 404)
        self.assertEqual(client.get("/fields/field-1/season?start=nonsense&end=2025-12-31").status_code, 422)
        self.assertEqual(client.post("/field-season", json={"polygon": SHAPE, "start": "2025-12-31", "end": "2025-01-01"}).status_code, 422)
