"""Crop-map acquisition preserves observations and leaves application inference to Java."""
import json
import unittest
from unittest.mock import patch

from environmental import crops


class TestCropObservations(unittest.TestCase):
    def test_cloud_pixels_are_excluded_without_inventing_a_crop(self):
        def fake_get_json(provider, method, url, **kwargs):
            counts = [0] * 160
            counts[10], counts[147], counts[158] = 600, 300, 100
            return {"histograms": [{"min": -0.5, "counts": counts}]}

        crops._histogram.cache_clear()
        try:
            with patch.object(crops, "_get_json", side_effect=fake_get_json):
                counts = crops._histogram(crops.ACI, 2024, json.dumps([[[0, 0], [0, 1], [1, 1], [0, 0]]]))
            row = crops._year_row(crops.ACI, 2024, counts)
            self.assertEqual((row["crop"], row["share"], row["cropShare"]), ("Corn for grain", 0.75, 1.0))
            self.assertNotIn("group", row)
            self.assertNotIn("groupLabel", row)
        finally:
            crops._histogram.cache_clear()

    def test_history_returns_observations_without_application_inference(self):
        maps = {2023: ((147, 9),), 2024: ((158, 9),), 2025: ((147, 9),)}
        with patch.object(crops, "_histogram", side_effect=lambda source, year, rings: maps.get(year, ()) if source is crops.ACI else ()), \
             patch.object(crops, "available_years", return_value=[2023, 2024, 2025]):
            result = crops.history({"type": "Polygon", "coordinates": [[[-79.2, 44.4], [-79.19, 44.4], [-79.19, 44.41], [-79.2, 44.4]]]})
        self.assertEqual([row["crop"] for row in result["years"]], ["Corn for grain", "Soybeans", "Corn for grain"])
        self.assertNotIn("summary", result)
        self.assertNotIn("expectedThisYear", result)
        self.assertTrue(all("group" not in row for row in result["years"]))

    def test_no_coverage_has_no_inferred_crop(self):
        result = crops._result(None, [], [], "Outside coverage")
        self.assertIsNone(result["source"])
        self.assertEqual(result["years"], [])
        self.assertNotIn("summary", result)
