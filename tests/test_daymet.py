"""Daymet as a field-summary weather source, and Ontario crop heat units."""
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import requests
from fastapi.testclient import TestClient

import field_summary
import server
from environmental import daymet, indicators, terrain

# A field near Orillia, Ontario.
ORILLIA = {"type": "Polygon", "coordinates": [[[-79.16, 44.35], [-79.15, 44.35], [-79.15, 44.355], [-79.16, 44.355], [-79.16, 44.35]]]}
PAYLOAD = {"polygon": ORILLIA, "period": {"start": "20230101", "end": "20231231"}}


def daymet_body(tmax=25.0, tmin=15.0, prcp=3.0, year=2023):
    days = range(1, 366)
    return {"data": {"year": [year] * 365, "yday": list(days), "tmax (deg c)": [tmax] * 365,
                     "tmin (deg c)": [tmin] * 365, "prcp (mm/day)": [prcp] * 365}}


def power_body():
    dates = pd.date_range("2023-01-01", "2023-12-31").strftime("%Y%m%d")
    values = {"T2M": 20, "T2M_MAX": 25, "T2M_MIN": 15, "PRECTOTCORR": 2}
    return {"properties": {"parameter": {k: dict.fromkeys(dates, v) for k, v in values.items()}}}


class Providers:
    """Route patched requests.get calls by URL, and count them."""

    def __init__(self, daymet_fails=False):
        self.daymet_fails, self.calls = daymet_fails, []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        resp = MagicMock()
        if url == daymet.URL:
            if self.daymet_fails:
                raise requests.ConnectionError()
            resp.json.return_value = daymet_body()
        else:
            resp.json.return_value = power_body()
        return resp

    def sources(self):
        return ["daymet" if url == daymet.URL else "power" for url, _ in self.calls]


class TestDaymetSummary(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.providers = Providers()
        patch("requests.get", side_effect=self.providers).start()
        land = patch("environmental.terrain.requests.post").start()
        land.return_value.status_code = 200
        land.return_value.json.return_value = {"results": [{"elevation": 200} for _ in range(100)]}
        self.addCleanup(patch.stopall)
        field_summary._power.cache_clear()
        daymet._fetch.cache_clear()
        terrain._elevations.cache_clear()

    def post(self, **changes):
        return self.client.post("/field-summary", json={**PAYLOAD, **changes})

    def test_auto_uses_daymet_in_north_america(self):
        body = self.post().json()
        self.assertEqual(body["weatherSource"], "daymet")
        self.assertEqual(body["layers"]["weather"], {"status": "ok", "source": "daymet"})
        self.assertEqual(body["source"][0]["source"], "NASA Daymet v4")
        self.assertEqual(body["source"][0]["spatial_resolution_km"], 1)
        self.assertEqual(body["weatherCell"]["gridDeg"], daymet.GRID_DEG)
        self.assertAlmostEqual(body["climate"]["annual_precip_mm_median"], 1095)
        url, params = self.providers.calls[0]
        self.assertEqual((params["start"], params["end"]), ("2023-01-01", "2023-12-31"))

    def test_sample_point_is_snapped_to_about_1_km(self):
        body = self.post().json()
        self.assertAlmostEqual(body["weatherCell"]["lat"], 44.355)
        self.assertAlmostEqual(body["weatherCell"]["lon"], -79.155)
        centred = self.post(weatherGridDeg=0).json()
        self.assertEqual(centred["weatherCell"], {**centred["field"]["centroid"], "gridDeg": 0})

    def test_auto_uses_power_outside_coverage_or_for_unpublished_years(self):
        europe = {"type": "Polygon", "coordinates": [[[2.0, 48.0], [2.01, 48.0], [2.01, 48.01], [2.0, 48.01], [2.0, 48.0]]]}
        self.assertEqual(self.post(polygon=europe).json()["weatherSource"], "power")
        with patch.object(daymet, "last_published_year", return_value=2022):
            self.assertEqual(self.post().json()["weatherSource"], "power")
        self.assertEqual(self.post(weatherSource="power").json()["weatherSource"], "power")

    def test_forcing_daymet_where_it_cannot_apply_is_422(self):
        europe = {"type": "Polygon", "coordinates": [[[2.0, 48.0], [2.01, 48.0], [2.01, 48.01], [2.0, 48.01], [2.0, 48.0]]]}
        resp = self.post(polygon=europe, weatherSource="daymet")
        self.assertEqual(resp.status_code, 422)
        self.assertIn("Daymet", resp.json()["detail"])
        self.assertEqual(self.providers.calls, [])

    def test_auto_falls_back_to_power_when_daymet_fails(self):
        self.providers.daymet_fails = True
        body = self.post().json()
        self.assertEqual(body["weatherSource"], "power")
        self.assertEqual(body["layers"]["weather"]["fallbackFrom"], "daymet")
        self.assertEqual(self.providers.sources(), ["daymet", "power"])

    def test_forced_daymet_failure_is_502(self):
        self.providers.daymet_fails = True
        resp = self.post(weatherSource="daymet")
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(self.providers.sources(), ["daymet"])

    def test_repeat_requests_reuse_the_download(self):
        self.post()
        self.post()
        nearby = [[-79.159, 44.351], [-79.151, 44.351], [-79.151, 44.354], [-79.159, 44.354], [-79.159, 44.351]]
        self.post(polygon={"type": "Polygon", "coordinates": [nearby]})
        self.assertEqual(self.providers.sources(), ["daymet"])

    def test_bad_daymet_data_falls_back(self):
        with patch.object(Providers, "__call__", lambda s, url, params=None, timeout=None: (
                s.calls.append((url, params)) or MagicMock(json=MagicMock(return_value={"data": {}} if url == daymet.URL else power_body())))):
            body = self.post().json()
        self.assertEqual(body["weatherSource"], "power")


class TestCropHeatUnits(unittest.TestCase):
    def test_known_values(self):
        frame = pd.DataFrame({"tmax_c": [25.0, 30.0, 8.0, 40.0], "tmin_c": [15.0, 18.0, 2.0, 25.0]})
        chu = indicators.crop_heat_units(frame).round(3).tolist()
        # 25/15: day 3.33*15-0.084*225=31.05, night 1.8*10.6=19.08 -> 25.065
        self.assertEqual(chu[0], 25.065)
        # 30/18: day 66.6-33.6=33.0, night 24.48 -> 28.74
        self.assertEqual(chu[1], 28.74)
        self.assertEqual(chu[2], 0.0)  # both terms floored at zero
        # 40/25: day 99.9-75.6=24.3, night 37.08 -> 30.69 (the day curve falls above 30 C)
        self.assertEqual(chu[3], 30.69)

    def year(self, warm_from="2023-05-10", frost_on="2023-10-01"):
        idx = pd.date_range("2023-01-01", "2023-12-31")
        frame = pd.DataFrame({"tmax_c": 5.0, "tmin_c": 0.0, "precip_mm": 2.0}, index=idx)
        frame.loc[warm_from:"2023-09-30", ["tmax_c", "tmin_c"]] = (25.0, 15.0)
        if frost_on:
            frame.loc[frost_on, "tmin_c"] = -3.0
        return frame

    def test_ontario_window(self):
        # Warm from May 10: three days >= 12.8 C end May 12, so counting starts May 13
        # and runs to Sep 30, the day before the -2 C frost: 141 days of 25.065.
        frame = self.year()
        self.assertAlmostEqual(indicators.available_chu(frame), 25.065 * 141, places=1)
        _, summary = indicators.summarize(frame, 44.35)
        self.assertAlmostEqual(summary["climate"]["chu_available_median"], 25.065 * 141, places=1)

    def test_spring_frost_does_not_end_the_season(self):
        frame = self.year()
        frame.loc["2023-06-01", "tmin_c"] = -3.0  # before August: ignored
        self.assertGreater(indicators.available_chu(frame), 25.065 * 140)

    def test_april_warm_spell_does_not_start_the_season(self):
        frame = self.year()
        frame.loc["2023-04-10":"2023-04-14", ["tmax_c", "tmin_c"]] = (25.0, 15.0)
        self.assertAlmostEqual(indicators.available_chu(frame), 25.065 * 141, places=1)

    def test_incomplete_windows_are_null(self):
        self.assertTrue(pd.isna(indicators.available_chu(self.year(frost_on=None))))
        gappy = self.year()
        gappy.loc["2023-06-01":"2023-07-15", "tmax_c"] = float("nan")
        self.assertTrue(pd.isna(indicators.available_chu(gappy)))
        _, south = indicators.summarize(self.year(), -30.0)
        self.assertIsNone(south["climate"]["chu_available_median"])


class TestPublishedYear(unittest.TestCase):
    def test_last_complete_year(self):
        self.assertEqual(daymet.last_published_year(date(2026, 11, 14)), 2025)
        self.assertTrue(daymet.covers(44.35, -79.16))
        self.assertFalse(daymet.covers(48.0, 2.0))


if __name__ == "__main__":
    unittest.main()
