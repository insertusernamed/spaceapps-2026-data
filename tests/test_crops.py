import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
from environmental import crops
from environmental.errors import ProviderError

PREVIEW = Path(server.__file__).with_name("data") / "fields.geojson"


def square(lon, lat, d=0.003, clockwise=False):
    ring = [[lon, lat], [lon + d, lat], [lon + d, lat + d], [lon, lat + d], [lon, lat]]
    return {"type": "Polygon", "coordinates": [ring[::-1] if clockwise else ring]}


def fake_maps(by_source, years=None, fail=()):
    """Patch provider calls: by_source maps source key -> {year: ((code, count), ...)}."""
    years = years or {"aci": list(range(2009, 2026)), "cdl": list(range(2008, 2026))}

    def histogram(source, year, _rings):
        if (source.key, year) in fail or source.key in fail:
            raise ProviderError(f"{source.name} is unavailable")
        return by_source.get(source.key, {}).get(year, ())

    return (patch.object(crops, "_histogram", side_effect=histogram),
            patch.object(crops, "available_years", side_effect=lambda s: years[s.key]))


class TestRings(unittest.TestCase):
    def signed_area(self, ring):
        return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:], strict=False))

    def test_outer_clockwise_holes_counter_clockwise(self):
        for clockwise in (False, True):
            outer = square(-79.2, 44.4, 0.02, clockwise)["coordinates"][0]
            hole = square(-79.195, 44.405, 0.01, not clockwise)["coordinates"][0]
            rings = crops.esri_rings({"type": "Polygon", "coordinates": [outer, hole]})
            self.assertLess(self.signed_area(rings[0]), 0)
            self.assertGreater(self.signed_area(rings[1]), 0)

    def test_closes_open_rings(self):
        rings = crops.esri_rings([[-79.2, 44.4], [-79.19, 44.4], [-79.19, 44.41]])
        self.assertEqual(rings[0][0], rings[0][-1])


class TestYearRow(unittest.TestCase):
    def test_shares_ranked_and_small_classes_dropped(self):
        row = crops._year_row(crops.ACI, 2025, ((147, 27), (158, 899), (230, 4), (20, 70)))
        self.assertEqual((row["crop"], row["code"], row["isCrop"], row["share"]), ("Soybeans", 158, True, 0.899))
        self.assertEqual([c["name"] for c in row["classes"]], ["Soybeans", "Water", "Corn for grain"])
        self.assertFalse(row["classes"][1]["isCrop"])

    def test_unknown_code_is_labelled_not_guessed(self):
        row = crops._year_row(crops.CDL, 2025, ((253, 10),))
        self.assertEqual((row["crop"], row["isCrop"]), ("Class 253", None))

    def test_empty_year(self):
        self.assertIsNone(crops._year_row(crops.CDL, 2025, ()))


class TestHistory(unittest.TestCase):
    def test_canada_uses_aci(self):
        maps = {"aci": {2024: ((147, 90),), 2025: ((158, 95), (147, 5))}, "cdl": {2025: ((1, 50),)}}
        a, b = fake_maps(maps)
        with a, b:
            result = crops.history(square(-79.16, 44.35))
        self.assertEqual(result["source"]["name"], "AAFC Annual Crop Inventory")
        self.assertEqual(result["rotation"], ["Corn for grain", "Soybeans"])
        self.assertEqual(result["distinctCrops"], ["Corn for grain", "Soybeans"])
        self.assertEqual(result["cropYears"], 2)

    def test_us_border_field_falls_back_to_cdl(self):
        maps = {"cdl": {2024: ((1, 50), (5, 50)), 2025: ((5, 80), (176, 20))}}
        a, b = fake_maps(maps)
        with a, b:
            result = crops.history(square(-97.25, 48.985))
        self.assertEqual(result["source"]["name"], "USDA NASS Cropland Data Layer")
        self.assertEqual(result["rotation"], ["Corn", "Soybeans"])

    def test_outside_coverage(self):
        a, b = fake_maps({})
        with a, b:
            result = crops.history(square(-102.35, 21.95))
        self.assertIsNone(result["source"])
        self.assertEqual(result["years"], [])
        self.assertIn("Canada and the United States", result["note"])

    def test_failed_years_are_reported(self):
        maps = {"aci": {2023: ((158, 1),), 2024: ((147, 1),), 2025: ((158, 1),)}}
        a, b = fake_maps(maps, fail={("aci", 2024)})
        with a, b:
            result = crops.history(square(-79.16, 44.35))
        self.assertEqual([r["year"] for r in result["years"]], [2023, 2025])
        self.assertEqual(result["unavailableYears"], [2024])

    def test_all_providers_down(self):
        a, b = fake_maps({}, fail={"aci", "cdl"})
        with a, b, self.assertRaises(ProviderError):
            crops.history(square(-79.16, 44.35))


class TestRoutes(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        env = {k: v for k, v in os.environ.items() if k not in ("FIELDS_DB", "FIELDS_FILE")}
        self.env = patch.dict(os.environ, {**env, "FIELDS_FILE": str(PREVIEW)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_field_route_uses_stored_outline(self):
        seen = []

        def fake_history(polygon):
            seen.append(polygon)
            return {"years": [], "rotation": []}

        with patch.object(crops, "history", side_effect=fake_history):
            resp = self.client.get("/fields/ftw-2025-17TPK_0_0-13020/crop-history")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], "ftw-2025-17TPK_0_0-13020")
        self.assertEqual(seen[0]["type"], "Polygon")

    def test_field_route_unknown_id(self):
        self.assertEqual(self.client.get("/fields/nope/crop-history").status_code, 404)

    def test_outline_route_validates_and_maps_provider_errors(self):
        self.assertEqual(self.client.post("/crop-history", json={"polygon": [[0, 0], [1, 1]]}).status_code, 422)
        self.assertEqual(self.client.post("/crop-history", json={"polygon": square(0, 0), "extra": 1}).status_code, 422)
        with patch.object(crops, "history", side_effect=ProviderError("USDA NASS Cropland Data Layer is unavailable")):
            resp = self.client.post("/crop-history", json={"polygon": square(-88.95, 40.45)})
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json(), {"detail": "USDA NASS Cropland Data Layer is unavailable"})


class TestProviderParsing(unittest.TestCase):
    def test_histogram_request_and_parsing(self):
        calls = []

        def fake_get_json(provider, method, url, **kwargs):
            calls.append((url, kwargs["data"]))
            return {"histograms": [{"min": -0.5, "size": 6, "counts": [7, 0, 3, 0, 0, 9]}]}

        crops._histogram.cache_clear()
        with patch.object(crops, "_get_json", side_effect=fake_get_json):
            result = crops._histogram(crops.CDL, 2024, json.dumps([[[0, 0], [0, 1], [1, 1], [0, 0]]]))
        crops._histogram.cache_clear()
        self.assertEqual(result, ((2, 3), (5, 9)))  # background (0) dropped
        self.assertIn("Year=2024", calls[0][1]["mosaicRule"])

    def test_published_years(self):
        aci = {"services": [{"name": "annual_crop_inventory/2008"}, {"name": "annual_crop_inventory/2025"}, {"name": "annual_crop_inventory/x"}]}
        cdl = {"features": [{"attributes": {"Year": y}} for y in (1997, 2008, 2025, 2025)]}
        with patch.object(crops, "_get_json", side_effect=lambda p, m, u, **k: aci if "annual_crop" in u else cdl):
            self.assertEqual(crops._published_years(crops.ACI), [2025])
            self.assertEqual(crops._published_years(crops.CDL), [2008, 2025])


if __name__ == "__main__":
    unittest.main()
