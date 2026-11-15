"""Offline field quality tests using real local rasters, masks and grid reprojection."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import numpy as np
import rasterio
from rasterio.transform import from_origin

from environmental import imagery_quality as quality


class TestFieldQuality(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.polygon = {"type": "Polygon", "coordinates": [[
            [-80, 44], [-79.999, 44], [-79.999, 43.999], [-80, 43.999], [-80, 44]]]}
        signer = patch.object(quality, "_href", side_effect=lambda item, band: item["assets"][band]["href"])
        signer.start()
        self.addCleanup(signer.stop)

    def write(self, name, values, nodata, resolution=0.0001):
        """Write a test raster with the same known geographic origin as the field."""
        data = np.asarray(values)
        if data.ndim == 2:
            data = data[np.newaxis]
        path = Path(self.tmp.name) / f"{name}.tif"
        with rasterio.open(path, "w", driver="GTiff", crs="EPSG:4326", nodata=nodata,
                           width=data.shape[2], height=data.shape[1], count=data.shape[0],
                           dtype=data.dtype, transform=from_origin(-80, 44, resolution, resolution)) as dst:
            dst.write(data)
        return {"href": str(path)}

    def scene(self, classes=None, native=False):
        """Build either HLS or Sentinel-2 with clear QA and valid RGB by default."""
        classes = np.full((10, 10), 4 if native else 0, dtype="uint8") if classes is None else classes
        qa = self.write("qa", classes, 0 if native else 255)
        if native:
            rgb = self.write("visual", np.full((3, 20, 20), 100, dtype="uint8"), 0, 0.00005)
            return {"collection": "sentinel-2-l2a", "assets": {"SCL": qa, "visual": rgb}}
        rgb = self.write("rgb", np.full((10, 10), 1000, dtype="int16"), -9999)
        return {"collection": "hls2-l30", "assets": {"Fmask": qa, "B04": rgb, "B03": rgb, "B02": rgb}}

    def test_clear_scenes_from_both_datasets(self):
        for native in (False, True):
            result = quality.read_quality(self.scene(native=native), self.polygon)
            self.assertEqual(result["fieldPixels"], 100)
            self.assertEqual(result["validShare"], 1)
            self.assertEqual(result["clearShare"], 1)
            self.assertTrue(quality.acceptable(result))

    def test_hls_flags_and_fill_are_rejected_but_water_and_aerosol_are_not_cloud(self):
        for code in (1, 2, 4, 8, 16, 255):
            result = quality.read_quality(self.scene(np.full((10, 10), code, dtype="uint8")), self.polygon)
            self.assertFalse(quality.acceptable(result), code)
        for code in (32, 64, 128):
            result = quality.read_quality(self.scene(np.full((10, 10), code, dtype="uint8")), self.polygon)
            self.assertTrue(quality.acceptable(result), code)

    def test_sentinel_cloud_shadow_snow_unclassified_and_invalid_codes(self):
        for code in (0, 1, 2, 3, 7, 8, 9, 10, 11):
            result = quality.read_quality(self.scene(np.full((10, 10), code, dtype="uint8"), True), self.polygon)
            self.assertFalse(quality.acceptable(result), code)

    def test_rgb_nodata_rejects_a_scene_even_when_qa_is_clear(self):
        item = self.scene()
        rgb = np.full((10, 10), 1000, dtype="int16")
        rgb[:2] = -9999
        item["assets"]["B02"] = self.write("missing_blue", rgb, -9999)
        result = quality.read_quality(item, self.polygon)
        self.assertAlmostEqual(result["validShare"], 0.8)
        self.assertFalse(quality.acceptable(result))

    def test_clouds_outside_the_polygon_and_inside_holes_do_not_count(self):
        classes = np.full((10, 10), 2, dtype="uint8")
        classes[:, :5] = 0
        item = self.scene(classes)
        half = {"type": "Polygon", "coordinates": [[
            [-80, 44], [-79.9995, 44], [-79.9995, 43.999], [-80, 43.999], [-80, 44]]]}
        self.assertTrue(quality.acceptable(quality.read_quality(item, half)))
        with_hole = {"type": "Polygon", "coordinates": [self.polygon["coordinates"][0], [
            [-79.9998, 43.9998], [-79.9992, 43.9998], [-79.9992, 43.9992],
            [-79.9998, 43.9992], [-79.9998, 43.9998]]]}
        classes[:] = 0
        classes[2:8, 2:8] = 2
        result = quality.read_quality(self.scene(classes), with_hole)
        self.assertEqual(result["fieldPixels"], 64)
        self.assertTrue(quality.acceptable(result))

    def test_missing_scene_coverage_counts_against_the_whole_field(self):
        item = self.scene(np.zeros((10, 5), dtype="uint8"))
        result = quality.read_quality(item, self.polygon)
        self.assertEqual(result["fieldPixels"], 100)
        self.assertAlmostEqual(result["validShare"], 0.5)
        self.assertFalse(quality.acceptable(result))

    def test_tiny_field_does_not_use_unrelated_bbox_pixels(self):
        tiny = {"type": "Polygon", "coordinates": [[
            [-80, 44], [-79.99999, 44], [-79.99999, 43.99999], [-80, 44]]]}
        result = quality.read_quality(self.scene(), tiny)
        self.assertEqual(result["fieldPixels"], 0)
        self.assertFalse(quality.acceptable(result))

    def test_api_combines_year_sampling_with_real_field_quality_reads(self):
        """Exercise catalogue selection, QA rasters and API serialization together offline."""
        from fastapi.testclient import TestClient
        import server
        from environmental import imagery

        cloudy, clear = self.scene(), self.scene()
        cloudy["assets"]["Fmask"] = self.write("cloudy_qa", np.full((10, 10), 2, dtype="uint8"), 255)
        for item, year in ((cloudy, 2020), (clear, 2021)):
            item.update(id=str(year), properties={"datetime": f"{year}-07-15T16:00:00Z",
                                                 "platform": "landsat-8", "eo:cloud_cover": 5})
        def catalogue(url, json, **kwargs):
            year = int(json["datetime"][:4])
            response = Mock()
            response.json.return_value = {"features": [cloudy if year == 2020 else clear], "links": []}
            return response
        with patch.object(server, "_field_geometry", return_value=self.polygon), \
             patch.object(imagery.requests, "post", side_effect=catalogue):
            response = TestClient(server.app).get("/fields/test/imagery?start=2020-01-01&end=2021-12-31")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([image["sceneId"] for image in body["images"]], ["2021"])
        self.assertEqual(body["images"][0]["fieldQuality"]["clearShare"], 1)
        self.assertEqual(body["yearsWithoutClearImages"], [2020])
        self.assertEqual(body["selection"], "yearly")

    def test_exact_clear_threshold(self):
        for rows, expected in ((1, True), (2, False)):
            classes = np.zeros((10, 10), dtype="uint8")
            classes[:rows] = 8
            result = quality.read_quality(self.scene(classes), self.polygon)
            self.assertEqual(quality.acceptable(result), expected)


if __name__ == "__main__":
    unittest.main()
