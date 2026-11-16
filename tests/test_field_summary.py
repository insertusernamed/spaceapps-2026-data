import unittest
from unittest.mock import patch

import pandas as pd
import requests
from fastapi.testclient import TestClient

import field_summary
import server
from environmental import daymet, indicators, terrain

POLYGON = {
    "type": "Polygon",
    "coordinates": [
        [
            [-91.94, 41.36],
            [-91.93, 41.36],
            [-91.93, 41.37],
            [-91.94, 41.37],
            [-91.94, 41.36],
        ]
    ],
}
# These tests cover the NASA POWER path; Daymet has its own tests in test_daymet.py.
PAYLOAD = {"polygon": POLYGON, "period": {"start": "20230101", "end": "20231231"}, "weatherSource": "power"}


def daily_response():
    dates = pd.date_range("2023-01-01", "2023-12-31").strftime("%Y%m%d")
    return {
        "properties": {
            "parameter": {
                key: dict.fromkeys(dates, value)
                for key, value in [
                    ("T2M", 20),
                    ("T2M_MAX", 25),
                    ("T2M_MIN", 15),
                    ("PRECTOTCORR", 2),
                ]
            }
        }
    }


class TestFieldSummary(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.weather = patch("field_summary.requests.get").start()
        self.weather.return_value.json.return_value = daily_response()
        self.land = patch("environmental.terrain.requests.post").start()
        self.land.return_value.status_code = 200
        self.land.return_value.json.return_value = {
            "results": [{"elevation": 200} for _ in range(100)]
        }
        self.addCleanup(patch.stopall)
        field_summary._power.cache_clear()
        daymet._fetch.cache_clear()
        terrain._elevations.cache_clear()

    def test_full_summary_from_provider_responses(self):
        response = self.client.post("/field-summary", json=PAYLOAD)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertAlmostEqual(result["climate"]["gdd_annual_median"], 3650)
        self.assertAlmostEqual(result["climate"]["annual_precip_mm_median"], 730)
        self.assertAlmostEqual(
            result["climate"]["growing_season_precip_mm_median"], 306
        )
        self.assertGreater(result["water"]["season_et0_mm_median"], 0)
        self.assertEqual(len(result["monthly"]), 12)
        self.assertEqual(result["coverage"]["years_with_temperature"], 1)
        self.assertEqual(result["terrain"]["elevation_m_mean"], 200)
        self.assertEqual(result["terrain"]["slope_pct_mean"], 0)
        self.assertNotIn("grid", result["terrain"])
        self.assertGreater(result["field"]["areaHa"], 0)
        self.assertEqual(result["layers"]["terrain"]["status"], "ok")
        self.assertEqual(result["source"][0]["source"], "NASA POWER")
        self.assertNotIn("cropHistory", result)
        self.assertEqual(self.weather.call_args.kwargs["params"]["start"], "20230101")

    def test_terrain_failure_preserves_weather(self):
        self.land.side_effect = requests.Timeout()
        response = self.client.post("/field-summary", json=PAYLOAD)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["terrain"])
        self.assertEqual(response.json()["layers"]["terrain"]["status"], "error")
        self.assertEqual(response.json()["layers"]["weather"]["status"], "ok")

    def test_weather_failure_is_502(self):
        self.weather.side_effect = requests.Timeout()
        self.assertEqual(
            self.client.post("/field-summary", json=PAYLOAD).status_code, 502
        )

    def test_bad_provider_data_is_502(self):
        for data in [{}, {"properties": {"parameter": {}}}]:
            self.weather.return_value.json.return_value = data
            self.assertEqual(
                self.client.post("/field-summary", json=PAYLOAD).status_code, 502
            )

    def test_validation_happens_before_provider_calls(self):
        invalid = [
            {},
            {"polygon": []},
            {"polygon": None},
            {"polygon": [[0, 91], [1, 91], [1, 92]]},
            {**PAYLOAD, "cropHistory": ["corn"]},
            {**PAYLOAD, "weatherGridDeg": -1},
            {**PAYLOAD, "period": {"start": "20230230"}},
            {**PAYLOAD, "period": {"start": "20240101", "end": "20230101"}},
            {**PAYLOAD, "period": {"start": "19800101"}},
            {**PAYLOAD, "season": {"months": [5, 7]}},
            {**PAYLOAD, "season": {"months": [True, 2]}},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                response = self.client.post("/field-summary", json=payload)
                self.assertEqual(response.status_code, 422, response.text)
        self.weather.assert_not_called()
        self.land.assert_not_called()

    def test_centroid_sampling_and_southern_season(self):
        payload = {**PAYLOAD, "weatherGridDeg": 0}
        response = self.client.post("/field-summary", json=payload).json()
        self.assertEqual(
            response["weatherCell"]["lat"], response["field"]["centroid"]["lat"]
        )
        self.assertEqual(
            response["weatherCell"]["lon"], response["field"]["centroid"]["lon"]
        )
        self.assertEqual(indicators.default_season(-30), (11, 12, 1, 2, 3))

    def test_missing_rain_is_not_zero(self):
        raw = daily_response()
        raw["properties"]["parameter"]["PRECTOTCORR"] = dict.fromkeys(
            raw["properties"]["parameter"]["PRECTOTCORR"], -999
        )
        self.weather.return_value.json.return_value = raw
        response = self.client.post("/field-summary", json=PAYLOAD)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["climate"]["annual_precip_mm_median"])
        self.assertIsNone(response.json()["water"]["water_stress_index"])
        self.assertEqual(response.json()["coverage"]["missing_precip_days"], 365)

    def test_stored_field_summary_by_id(self):
        with patch.object(server, "_field_geometry", return_value=POLYGON) as lookup:
            response = self.client.get("/fields/field-1/summary?weatherSource=power")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        lookup.assert_called_once_with("field-1")
        self.assertEqual(result["id"], "field-1")
        self.assertEqual(result["weatherSource"], "power")
        self.assertEqual(result["period"], {"start": "20150101", "end": "20241231"})
        self.assertGreater(result["field"]["areaHa"], 0)
        self.assertEqual(len(result["monthly"]), 12)

    def test_stored_field_summary_errors(self):
        with patch.object(server, "_field_geometry", return_value=None):
            self.assertEqual(self.client.get("/fields/missing/summary").status_code, 404)
        with patch.object(server, "_field_geometry", return_value=POLYGON):
            self.assertEqual(self.client.get("/fields/field-1/summary?weatherSource=goes").status_code, 422)
        flat = {"type": "Polygon", "coordinates": [[[-91.94, 41.36], [-91.93, 41.36], [-91.94, 41.36]]]}
        with patch.object(server, "_field_geometry", return_value=flat):
            response = self.client.get("/fields/field-1/summary")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "The stored field outline cannot be summarized")
        self.weather.side_effect = requests.ConnectionError("down")
        with patch.object(server, "_field_geometry", return_value=POLYGON):
            self.assertEqual(self.client.get("/fields/field-1/summary?weatherSource=power").status_code, 502)

    def test_swagger_exposes_only_new_summary_route(self):
        schema = self.client.get("/openapi.json").json()
        self.assertIn("/field-summary", schema["paths"])
        self.assertIn("/fields/{field_id}/summary", schema["paths"])
        self.assertIn("FieldSummaryRequest", schema["components"]["schemas"])
