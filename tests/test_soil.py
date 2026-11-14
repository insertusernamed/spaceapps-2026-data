"""Field soil from SoilGrids, tested offline."""
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import numpy as np
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin

import server
from environmental import soil
from environmental.errors import ProviderError

PREVIEW = Path(server.__file__).with_name("data") / "fields.geojson"
SQUARE = {"type": "Polygon", "coordinates": [[[-79.5, 44.5], [-79.49, 44.5], [-79.49, 44.51], [-79.5, 44.51], [-79.5, 44.5]]]}


class TestClasses(unittest.TestCase):
    def test_usda_texture_triangle(self):
        cases = {(40, 40, 20): "Loam", (92, 4, 4): "Sand", (82, 12, 6): "Loamy sand", (65, 25, 10): "Sandy loam",
                 (20, 65, 15): "Silt loam", (5, 88, 7): "Silt", (60, 12, 28): "Sandy clay loam", (32, 34, 34): "Clay loam",
                 (10, 58, 32): "Silty clay loam", (50, 5, 45): "Sandy clay", (8, 47, 45): "Silty clay", (20, 20, 60): "Clay"}
        for (sand, silt, clay), expected in cases.items():
            with self.subTest(sand=sand, silt=silt, clay=clay):
                self.assertEqual(soil.texture_class(sand, silt, clay), expected)

    def test_ph_classes(self):
        self.assertEqual([soil.ph_class(v) for v in (4.2, 5.8, 6.3, 7.0, 7.6, 9.0)],
                         ["Extremely acid", "Moderately acid", "Slightly acid", "Neutral", "Slightly alkaline", "Strongly alkaline"])

    def test_available_water(self):
        loam = soil.available_water_mm(40, 20, 2.5)
        sand = soil.available_water_mm(90, 5, 1.0)
        self.assertTrue(40 <= loam <= 50, loam)  # about 0.14-0.16 of the volume over 30 cm
        self.assertLess(sand, loam / 2)


class TestIndex(unittest.TestCase):
    def test_downloaded_once_with_absolute_tile_paths(self):
        vrt = ('<VRTDataset><SimpleSource><SourceFilename relativeToVRT="1">./clay_0-5cm_mean/tileSG-1/t.tif'
               '</SourceFilename></SimpleSource></VRTDataset>')
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"SOIL_INDEX_DIR": tmp}), \
                patch.object(soil.requests, "get", return_value=MagicMock(text=vrt)) as get:
            path = soil._index("clay", "0-5cm")
            again = soil._index("clay", "0-5cm")
            text = path.read_text()
        self.assertEqual(path, again)
        self.assertEqual(get.call_count, 1)
        self.assertIn('relativeToVRT="0">/vsicurl/https://files.isric.org/soilgrids/latest/data/clay/clay_0-5cm_mean/tileSG-1/t.tif', text)

    def test_index_location(self):
        env = {k: v for k, v in os.environ.items() if k not in ("SOIL_INDEX_DIR", "FIELDS_DB")}
        with patch.dict(os.environ, {**env, "FIELDS_DB": "/var/lib/spaceapps-fields/fields.sqlite"}, clear=True):
            self.assertEqual(soil.index_dir(), Path("/var/lib/spaceapps-fields/soilgrids-index"))
        with patch.dict(os.environ, {**env, "SOIL_INDEX_DIR": "/tmp/x"}, clear=True):
            self.assertEqual(soil.index_dir(), Path("/tmp/x"))

    def test_unavailable_or_unexpected_index(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"SOIL_INDEX_DIR": tmp}):
            with patch.object(soil.requests, "get", side_effect=soil.requests.ConnectionError()), self.assertRaises(ProviderError):
                soil._index("sand", "0-5cm")
            with patch.object(soil.requests, "get", return_value=MagicMock(text="<html>maintenance</html>")), self.assertRaises(ProviderError):
                soil._index("sand", "0-5cm")
            self.assertEqual(list(Path(tmp).iterdir()), [])  # nothing half-written is kept


class TestRead(unittest.TestCase):
    def test_mean_over_touched_cells_ignores_nodata(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "layer.tif"
            data = np.full((40, 40), 300, dtype="int16")
            data[:25, :] = 100  # the square (rows 20-30, columns 20-30) straddles 100 and 300
            data[21:24, 21:24] = soil.NODATA  # missing cells inside the square must be ignored
            data[1:6, 1:6] = soil.NODATA  # a patch of water elsewhere
            with rasterio.open(path, "w", driver="GTiff", width=40, height=40, count=1, dtype="int16", crs="EPSG:4326",
                               transform=from_origin(-79.52, 44.53, 0.001, 0.001), nodata=soil.NODATA) as ds:
                ds.write(data, 1)
            shape = {"type": "MultiPolygon", "coordinates": [SQUARE["coordinates"]]}
            water = {"type": "MultiPolygon", "coordinates": [[[[-79.5175, 44.5275], [-79.5155, 44.5275], [-79.5155, 44.5265],
                                                               [-79.5175, 44.5265], [-79.5175, 44.5275]]]]}
            with patch.object(soil, "_index", return_value=path):
                value, over_water = soil._read_layer("clay", "0-5cm", [shape, water])
        self.assertTrue(100 < value < 300, value)
        self.assertIsNone(over_water)  # only missing cells: no estimate


class TestFieldSoil(unittest.TestCase):
    def setUp(self):
        soil._cache.clear()
        self.addCleanup(soil._cache.clear)

    def fake(self, by_depth):
        stored = {"phh2o": 70, "clay": 200, "sand": 400, "silt": 400, "soc": 150, "nitrogen": 150, "bdod": 140, "cec": 180}
        return lambda layer, depth, shapes: [stored[layer] * (by_depth[depth] if layer == "clay" else 1.0)] * len(shapes)

    def test_scaling_weighting_and_cache(self):
        reader = self.fake({"0-5cm": 1.0, "5-15cm": 1.0, "15-30cm": 0.5})
        with patch.object(soil, "_read_layer", side_effect=reader) as read:
            result = soil.field_soil(SQUARE)
            again = soil.field_soil(SQUARE)
        self.assertIs(again, result)
        self.assertEqual(read.call_count, 24)
        self.assertEqual(result["depths"]["0-5cm"]["ph"], 7.0)
        self.assertEqual(result["depths"]["0-5cm"]["nitrogenGPerKg"], 1.5)
        self.assertEqual(result["depths"]["15-30cm"]["clayPct"], 10.0)
        self.assertEqual(result["topsoil"]["clayPct"], 15.0)  # (20*5 + 20*10 + 10*15) / 30
        self.assertEqual(result["derived"]["textureClass"], "Loam")
        self.assertEqual(result["derived"]["phClass"], "Neutral")
        self.assertEqual(result["units"]["availableWaterMm"], "mm in 0-30 cm")

    def test_water_gives_nulls_and_a_note(self):
        with patch.object(soil, "_read_layer", side_effect=lambda layer, depth, shapes: [None] * len(shapes)):
            result = soil.field_soil(SQUARE)
        self.assertTrue(all(v is None for v in result["topsoil"].values()))
        self.assertEqual(result["derived"], {"textureClass": None, "organicMatterPct": None, "phClass": None, "availableWaterMm": None})
        self.assertIn("no soil estimate", result["note"])

    def test_read_failure_is_a_provider_error(self):
        with patch.object(soil, "_read_layer", side_effect=rasterio.errors.RasterioIOError("timeout")), self.assertRaises(ProviderError):
            soil.field_soil(SQUARE)

    def test_nearby_fields_share_one_read_per_layer(self):
        fields = {f"f{i}": {"type": "Polygon", "coordinates": [[[-79.5 + i * 0.01, 44.5], [-79.495 + i * 0.01, 44.5],
                                                                  [-79.495 + i * 0.01, 44.505], [-79.5 + i * 0.01, 44.5]]]}
                  for i in range(5)}
        far = {"type": "Polygon", "coordinates": [[[-90.0, 40.0], [-89.99, 40.0], [-89.99, 40.01], [-90.0, 40.0]]]}
        calls = []

        def reader(layer, depth, shapes):
            calls.append(len(shapes))
            return [100.0] * len(shapes)

        with patch.object(soil, "_read_layer", side_effect=reader):
            out = soil.soil_many({**fields, "far": far})
        self.assertEqual(sorted(set(calls)), [1, 5])  # one group of five, one of one
        self.assertEqual(len(calls), 2 * 24)  # one read per layer per group
        self.assertEqual(set(out), {*fields, "far"})

    def test_a_failing_group_does_not_fail_the_others(self):
        near = {"type": "Polygon", "coordinates": [[[-79.5, 44.5], [-79.49, 44.5], [-79.49, 44.51], [-79.5, 44.5]]]}
        far = {"type": "Polygon", "coordinates": [[[-90.0, 40.0], [-89.99, 40.0], [-89.99, 40.01], [-90.0, 40.0]]]}

        def reader(layer, depth, shapes):
            if shapes[0]["coordinates"][0][0][0][0] < -89:
                raise rasterio.errors.RasterioIOError("timeout")
            return [100.0] * len(shapes)

        with patch.object(soil, "_read_layer", side_effect=reader):
            out = soil.soil_many({"near": near, "far": far})
        self.assertIn("topsoil", out["near"])
        self.assertIsInstance(out["far"], ProviderError)

    def test_groups_respect_the_span(self):
        shapes = {k: soil._shape({"type": "Polygon", "coordinates": [[[lon, 44.5], [lon + 0.001, 44.5], [lon, 44.501], [lon, 44.5]]]})
                  for k, lon in (("a", -79.5), ("b", -79.4), ("c", -79.2), ("d", -79.0))}
        groups = soil._groups(shapes)
        for g in groups:
            lons = [shapes[k]["coordinates"][0][0][0][0] for k in g]
            self.assertLessEqual(max(lons) - min(lons), soil.GROUP_SPAN_DEG + 0.001)
        self.assertEqual(sorted(k for g in groups for k in g), ["a", "b", "c", "d"])


class TestRoutes(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        env = {k: v for k, v in os.environ.items() if k not in ("FIELDS_DB", "FIELDS_FILE")}
        p = patch.dict(os.environ, {**env, "FIELDS_FILE": str(PREVIEW)}, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_field_route(self):
        with patch.object(soil, "field_soil", return_value={"topsoil": {"ph": 6.5}}):
            resp = self.client.get("/fields/ftw-2025-17TPK_0_0-13020/soil")
        self.assertEqual(resp.json(), {"id": "ftw-2025-17TPK_0_0-13020", "topsoil": {"ph": 6.5}})
        self.assertEqual(self.client.get("/fields/nope/soil").status_code, 404)

    def test_batch_route(self):
        ids = ["ftw-2025-17TPK_0_0-13020", "nope", "ftw-2025-17TPK_0_0-13020"]
        with patch.object(soil, "soil_many", side_effect=lambda polygons: {k: {"topsoil": {"ph": 6.5}} for k in polygons}) as many:
            body = self.client.post("/fields/soil", json={"ids": ids}).json()
        self.assertEqual(list(many.call_args.args[0]), ids[:1])  # unknown IDs never reach SoilGrids
        self.assertEqual([r["id"] for r in body["results"]], ids[:2])
        self.assertEqual(body["results"][0]["soil"], {"topsoil": {"ph": 6.5}})
        self.assertEqual(body["results"][1]["error"], {"code": 404, "detail": "Field not found"})
        with patch.object(soil, "soil_many", side_effect=lambda polygons: {k: ProviderError("SoilGrids is unavailable") for k in polygons}):
            body = self.client.post("/fields/soil", json={"ids": ids[:1]}).json()
        self.assertEqual(body["results"][0]["error"], {"code": 502, "detail": "SoilGrids is unavailable"})
        self.assertEqual(self.client.post("/fields/soil", json={"ids": []}).status_code, 422)

    def test_outline_route(self):
        with patch.object(soil, "field_soil", side_effect=ProviderError("SoilGrids is unavailable")):
            self.assertEqual(self.client.post("/soil", json={"polygon": SQUARE}).status_code, 502)
        self.assertEqual(self.client.post("/soil", json={"polygon": [[0, 0], [1, 1]]}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
