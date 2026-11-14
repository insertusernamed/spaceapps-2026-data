"""Plain-language reading of crop histories: groups, rotation, this year's likely crop, flags."""
import json
import unittest
from datetime import date
from unittest.mock import patch

from environmental import crop_patterns as cp
from environmental import crops

NAMES = {"C": "Corn for grain", "S": "Soybeans", "W": "Winter wheat", "P": "Pasture/forages", "F": "Mixedwood"}
TODAY = date(2026, 11, 14)


def rows(sequence, share=0.95, start=2011):
    return [{"year": start + i, "crop": NAMES[c], "group": cp.group_of(NAMES[c]), "share": share,
             "cropShare": 0.0 if c == "F" else share} for i, c in enumerate(sequence)]


def read(sequence, **kwargs):
    return cp.summarize(rows(sequence, **kwargs), TODAY)


class TestGroups(unittest.TestCase):
    def test_both_countries_map_to_the_same_groups(self):
        cases = {"Corn for grain": "corn", "Corn": "corn", "Sweet Corn": "corn", "Soybeans": "soybeans",
                 "Dbl Crop WinWht/Soybeans": "wheat", "Winter wheat": "wheat", "Pasture/forages": "pasture",
                 "Grass/Pasture": "pasture", "Alfalfa": "hay", "Sod": "hay", "Canola/rapeseed": "canola", "Dry Beans": "pulses",
                 "Barley": "other_grains", "Mixedwood": "natural", "Herbaceous Wetlands": "natural", "Urban/developed": "developed"}
        for name, group in cases.items():
            with self.subTest(name=name):
                self.assertEqual(cp.group_of(name), group)


class TestPatterns(unittest.TestCase):
    def expect(self, summary):
        e = summary["expectedThisYear"]
        return e and (e["crop"], e["confidence"])

    def test_three_year_rotation(self):
        s = read("CCCSCSCCSCCSCCS")
        self.assertEqual(s["pattern"]["text"], "A 3-year rotation: corn, corn, then soybeans.")
        self.assertEqual(self.expect(s), ("Corn", "high"))
        self.assertIn("Soybeans in 2025, so the pattern points to corn this year.", s["expectedThisYear"]["reason"])

    def test_alternation_flips(self):
        s = read("SCSCSCSCSCSCSCS")
        self.assertEqual(s["pattern"]["kind"], "alternating")
        self.assertEqual(self.expect(s), ("Corn", "high"))

    def test_stable_crop_and_one_odd_year(self):
        self.assertEqual(self.expect(read("PPPPPPPPPPPPPPP")), ("Pasture", "high"))
        odd = read("PPPPPPPPPCPPPPP")
        self.assertEqual(odd["pattern"]["text"], "Mostly pasture in recent years.")
        self.assertEqual(self.expect(odd), ("Pasture", "medium"))

    def test_a_change_last_season_is_described_not_guessed(self):
        for seq, text in (("PPPPPPPPPPPPPPC", "Mostly pasture until 2024, then corn in 2025."),
                          ("CSCSCSCSCSCSCSS", "A regular rotation until 2024, then soybeans in 2025.")):
            with self.subTest(seq=seq):
                s = read(seq)
                self.assertEqual((s["pattern"]["kind"], s["pattern"]["text"]), ("changed", text))
                self.assertIsNone(s["expectedThisYear"])
                self.assertIn("The pattern changed last season, so this year's crop is not estimated.", s["notes"])

    def test_mixed_history_is_not_guessed(self):
        s = read("CSWPCWSPCSWPCWS")
        self.assertEqual(s["pattern"]["kind"], "mixed")
        self.assertIsNone(s["expectedThisYear"])

    def test_non_crop_land(self):
        s = read("FFFFFFFFFFFFFFF")
        self.assertIsNone(s["expectedThisYear"])
        self.assertTrue(s["flags"]["mostlyNotCropland"])

    def test_possibly_merged_fields(self):
        self.assertTrue(read("CSCSCSCSCSCSCSC", share=0.45)["flags"]["possiblyMerged"])
        self.assertFalse(read("CSCSCSCSCSCSCSC")["flags"]["possiblyMerged"])

    def test_estimate_two_seasons_ahead_and_stale_maps(self):
        two_ahead = cp.summarize(rows("CSCSCSCSCSCSCS"), TODAY)  # last map 2024, this year 2026
        self.assertEqual(two_ahead["expectedThisYear"]["crop"], "Soybeans")
        stale = cp.summarize(rows("CSCSCSCSCS"), TODAY)  # last map 2020: too old to project
        self.assertIsNone(stale["expectedThisYear"])

    def test_basic_fields_and_empty_history(self):
        s = read("CSCSCSCSCSCSCSC")
        self.assertEqual((s["yearsOfData"], s["firstYear"]), (15, 2011))
        self.assertEqual(s["lastSeason"], {"year": 2025, "crop": "Corn for grain", "group": "corn", "groupLabel": "Corn"})
        self.assertIn("The newest crop map is 2025 (last season); 2026 is not mapped yet.", s["notes"])
        empty = cp.summarize([], TODAY)
        self.assertEqual((empty["yearsOfData"], empty["pattern"], empty["expectedThisYear"]), (0, None, None))


class TestCloudsAndSummary(unittest.TestCase):
    def test_cloud_pixels_are_left_out_of_shares(self):
        def fake_get_json(provider, method, url, **kwargs):
            counts = [0] * 160
            counts[10], counts[147], counts[158] = 600, 300, 100  # mostly cloud over a corn field
            return {"histograms": [{"min": -0.5, "counts": counts}]}

        crops._histogram.cache_clear()
        with patch.object(crops, "_get_json", side_effect=fake_get_json):
            counts = crops._histogram(crops.ACI, 2024, json.dumps([[[0, 0], [0, 1], [1, 1], [0, 0]]]))
        crops._histogram.cache_clear()
        row = crops._year_row(crops.ACI, 2024, counts)
        self.assertEqual((row["crop"], row["share"], row["group"], row["cropShare"]), ("Corn for grain", 0.75, "corn", 1.0))

    def test_history_includes_summary(self):
        maps = {2023: ((147, 9),), 2024: ((158, 9),), 2025: ((147, 9),)}
        with patch.object(crops, "_histogram", side_effect=lambda s, y, r: maps.get(y, ()) if s is crops.ACI else ()), \
             patch.object(crops, "available_years", return_value=[2023, 2024, 2025]):
            result = crops.history({"type": "Polygon", "coordinates": [[[-79.2, 44.4], [-79.19, 44.4], [-79.19, 44.41], [-79.2, 44.4]]]})
        self.assertEqual([y["group"] for y in result["years"]], ["corn", "soybeans", "corn"])
        self.assertEqual([y["groupLabel"] for y in result["years"]], ["Corn", "Soybeans", "Corn"])
        self.assertEqual(result["summary"]["lastSeason"]["groupLabel"], "Corn")
        self.assertEqual(result["summary"]["yearsOfData"], 3)


if __name__ == "__main__":
    unittest.main()
