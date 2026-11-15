import unittest
from datetime import date
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import requests
from fastapi.testclient import TestClient

import server
from crop_history import EXAMPLE_POLYGON
from environmental import imagery
from environmental.errors import ProviderError, ValidationError


CLEAR = {"fieldPixels": 100, "validPixels": 100, "clearPixels": 100,
         "validShare": 1.0, "clearShare": 1.0}


class TestImagery(unittest.TestCase):
    def setUp(self):
        """Catalogue tests mock raster reads; pixel behavior has independent GeoTIFF tests."""
        quality = patch.object(imagery, "_quality", return_value=CLEAR)
        self.quality = quality.start()
        self.addCleanup(quality.stop)

    def test_hls_combines_collections_and_renders_each_correctly(self):
        response = Mock()
        response.json.return_value = {"features": [
            {"id": f"HLS.{kind}.TEST.v2.0", "collection": collection,
             "properties": {"datetime": f"2025-07-0{day}T16:00:00Z", "platform": platform, "eo:cloud_cover": 5}}
            for kind, collection, platform, day in [
                ("S30", "hls2-s30", "sentinel-2b", 2), ("L30", "hls2-l30", "landsat-9", 1)]]}
        with patch.object(imagery.requests, "post", return_value=response) as post:
            result = imagery.images(EXAMPLE_POLYGON, date(2025, 6, 1), date(2025, 9, 1))
        self.assertEqual(post.call_args.kwargs["json"]["collections"], ["hls2-l30", "hls2-s30"])
        self.assertEqual(result["dataset"], "hls")
        self.assertEqual(result["source"]["resolutionM"], 30)
        for scene in result["images"]:
            query = parse_qs(urlparse(scene["imageUrl"]).query)
            self.assertEqual(query["collection"], [scene["collection"]])
            self.assertEqual(query["assets"], ["B04", "B03", "B02"])
            crop = [float(v) for v in urlparse(scene["imageUrl"]).path.rsplit("/", 1)[1][:-4].split(",")]
            self.assertEqual(crop, result["bbox"])
            self.assertEqual(query["max_size"], ["1024"])
            self.assertEqual(query["rescale"], ["0,3000"])
            self.assertEqual(query["color_formula"], ["gamma RGB 1.4"])
            self.assertEqual(query["nodata"], ["-9999"])
            self.assertIn(f"/collections/{scene['collection']}/", scene["sceneUrl"])

    def test_search_and_rgb_scene_contract(self):
        item = {"id": "S2C_TEST", "collection": "sentinel-2-l2a", "properties": {
            "datetime": "2025-07-01T16:00:00Z", "platform": "sentinel-2c", "eo:cloud_cover": 12}}
        response = Mock()
        response.json.return_value = {"features": [item], "links": [{"rel": "next", "href": "https://example.test/next"}]}
        with patch.object(imagery.requests, "post", return_value=response) as post:
            result = imagery.images(EXAMPLE_POLYGON, date(2025, 6, 1), date(2025, 9, 1), 20, 1, "sentinel-2")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["collections"], ["sentinel-2-l2a"])
        self.assertNotIn("platform", body["query"])
        self.assertEqual(body["query"]["eo:cloud_cover"], {"lte": 20})
        self.assertEqual(body["sortby"], [{"field": "datetime", "direction": "desc"}])
        self.assertEqual(body["intersects"]["coordinates"][0], EXAMPLE_POLYGON["coordinates"])
        url = urlparse(result["images"][0]["imageUrl"])
        crop = [float(v) for v in url.path.rsplit("/", 1)[1][:-4].split(",")]
        self.assertEqual(crop, result["bbox"])
        self.assertLess(crop[0], -88.95)
        self.assertLess(crop[1], 40.45)
        self.assertGreater(crop[2], -88.947)
        self.assertGreater(crop[3], 40.453)
        self.assertEqual(parse_qs(url.query)["assets"], ["visual"])
        self.assertEqual(parse_qs(url.query)["asset_bidx"], ["visual|1,2,3"])
        self.assertNotIn("color_formula", parse_qs(url.query))
        self.assertEqual(result["source"]["resolutionM"], 10)
        self.assertTrue(result["hasMore"])
        self.assertEqual(result["images"][0]["sceneCloudCoverPct"], 12)

    def test_empty_and_bad_provider(self):
        for payload in [{"features": [], "links": []}, {}, {"features": None}]:
            response = Mock()
            response.json.return_value = payload
            with patch.object(imagery.requests, "post", return_value=response):
                if "links" in payload:
                    self.assertEqual(imagery.images(EXAMPLE_POLYGON, date(2025, 6, 1), date(2025, 9, 1))["images"], [])
                else:
                    with self.assertRaises(ProviderError):
                        imagery.images(EXAMPLE_POLYGON, date(2025, 6, 1), date(2025, 9, 1))
        with patch.object(imagery.requests, "post", side_effect=requests.Timeout):
            with self.assertRaises(ProviderError):
                imagery.images(EXAMPLE_POLYGON, date(2025, 6, 1), date(2025, 9, 1))

    def test_rejected_recent_scenes_are_replaced_by_older_clear_scenes(self):
        def item(name):
            return {"id": name, "collection": "hls2-l30", "properties": {
                "datetime": "2025-07-01T16:00:00Z", "platform": "landsat-9", "eo:cloud_cover": 5}}
        first, second = Mock(), Mock()
        first.json.return_value = {"features": [item("cloudy"), item("shadowed")],
                                   "links": [{"rel": "next", "href": "https://example.test/page2", "method": "POST",
                                              "merge": True, "body": {"token": "older"}}]}
        second.json.return_value = {"features": [item("clear")], "links": []}
        self.quality.side_effect = lambda scene, shape: (CLEAR if scene["id"] == "clear" else
                                                        {**CLEAR, "clearShare": 0.5})
        with patch.object(imagery.requests, "post", side_effect=[first, second]) as post:
            result = imagery.images(EXAMPLE_POLYGON, date(2025, 6, 1), date(2025, 9, 1), limit=1)
        self.assertEqual([s["sceneId"] for s in result["images"]], ["clear"])
        self.assertEqual(result["scenesChecked"], 3)
        self.assertFalse(result["hasMore"])
        self.assertEqual(post.call_args.kwargs["json"]["token"], "older")
        self.assertEqual(post.call_args.kwargs["json"]["collections"], imagery.HLS_COLLECTIONS)

    def test_unreadable_quality_is_not_silently_returned_as_clear(self):
        response = Mock()
        response.json.return_value = {"features": [{"id": "broken", "collection": "hls2-l30"}]}
        self.quality.return_value = None
        with patch.object(imagery.requests, "post", return_value=response):
            with self.assertRaisesRegex(ProviderError, "quality assets"):
                imagery.images(EXAMPLE_POLYGON, date(2025, 6, 1), date(2025, 9, 1))

    def test_candidate_budget_is_bounded_and_reports_more(self):
        response = Mock()
        response.json.return_value = {"features": [
            {"id": str(i), "collection": "hls2-l30"} for i in range(50)]}
        self.quality.return_value = {**CLEAR, "validShare": 0.4}
        with patch.object(imagery.requests, "post", return_value=response):
            result = imagery.images(EXAMPLE_POLYGON, date(2025, 6, 1), date(2025, 9, 1))
        self.assertEqual(result["images"], [])
        self.assertEqual(result["scenesChecked"], imagery.MAX_CANDIDATES)
        self.assertTrue(result["hasMore"])

    def test_invalid_dates_do_not_contact_provider(self):
        with patch.object(imagery.requests, "post") as post:
            for start, end in [(date(2010, 1, 1), date(2010, 2, 1)),
                               (date(2019, 1, 1), date(2025, 9, 1)),
                               (date(2025, 9, 1), date(2025, 1, 1))]:
                with self.assertRaises(ValidationError):
                    imagery.images(EXAMPLE_POLYGON, start, end)
            post.assert_not_called()

    def test_historical_selection_allocates_slots_across_years(self):
        def catalogue(url, json, **kwargs):
            year = int(json["datetime"][:4])
            response = Mock()
            response.json.return_value = {"features": [
                {"id": f"{year}-{month:02}", "collection": "hls2-l30", "properties": {
                    "datetime": f"{year}-{month:02}-15T16:00:00Z", "platform": "landsat-9", "eo:cloud_cover": 5}}
                for month in range(12, 0, -1)], "links": []}
            return response
        with patch.object(imagery.requests, "post", side_effect=catalogue):
            result = imagery.images(EXAMPLE_POLYGON, date(2020, 1, 1), date(2022, 12, 31), limit=6)
        self.assertEqual(len(result["images"]), 6)
        self.assertEqual([image["datetime"][:4] for image in result["images"]],
                         ["2022", "2022", "2021", "2021", "2020", "2020"])
        self.assertEqual(result["selection"], "yearly")
        self.assertEqual(result["yearsWithoutClearImages"], [])
        self.assertTrue(all(image["fieldQuality"]["clearShare"] == 1 for image in result["images"]))

    def test_history_keeps_archive_endpoints_when_limit_is_smaller_than_year_count(self):
        body = {"collections": imagery.HLS_COLLECTIONS}
        with patch.object(imagery, "_select", return_value=([], False, 0, 0)) as select:
            imagery._history(body, {}, date(2020, 1, 1), date(2026, 11, 15), 3)
        years = sorted(int(call.args[0]["datetime"][:4]) for call in select.call_args_list)
        self.assertEqual(years, [2020, 2023, 2026])

    def test_crop_includes_context_without_changing_field_search_or_quality(self):
        response = Mock()
        response.json.return_value = {"features": []}
        with patch.object(imagery.requests, "post", return_value=response) as post:
            result = imagery.images(EXAMPLE_POLYGON, date(2025, 6, 1), date(2025, 9, 1))
        self.assertEqual(post.call_args.kwargs["json"]["intersects"]["coordinates"][0], EXAMPLE_POLYGON["coordinates"])
        west, south, east, north = result["bbox"]
        self.assertAlmostEqual(west, -88.953)
        self.assertAlmostEqual(south, 40.447)
        self.assertAlmostEqual(east, -88.944)
        self.assertAlmostEqual(north, 40.456)
        tiny = imagery._crop_bounds((-80, 44, -79.99999, 44.00001))
        self.assertGreater(tiny[2] - tiny[0], 0.002)
        self.assertGreater(tiny[3] - tiny[1], 0.001)

    def test_default_period_uses_hosted_archive_start_not_mission_launch(self):
        for dataset, expected in (("hls", date(2020, 1, 1)), ("sentinel-2", date(2015, 6, 27))):
            with patch.object(imagery, "_history", return_value=([], False, 0, 0, [], [])) as history:
                result = imagery.images(EXAMPLE_POLYGON, dataset=dataset)
            self.assertEqual(history.call_args.args[2], expected)
            self.assertEqual(history.call_args.args[4], 20)
            self.assertEqual(result["period"]["start"], str(expected))

    def test_missing_and_unreadable_years_do_not_fabricate_observations(self):
        item = {"id": "clear", "properties": {"datetime": "2021-07-15T00:00:00Z"}}
        def select(body, *args, **kwargs):
            year = int(body["datetime"][:4])
            if year == 2020:
                return [], False, 8, 0
            if year == 2022:
                raise ProviderError("provider down")
            return [(item, CLEAR)], False, 1, 0
        with patch.object(imagery, "_select", side_effect=select):
            result = imagery._history({"collections": imagery.HLS_COLLECTIONS}, {},
                                      date(2020, 1, 1), date(2022, 12, 31), 6)
        self.assertEqual(len(result[0]), 1)
        self.assertEqual(result[4], [2020])
        self.assertEqual(result[5], [2022])

    def test_spread_visits_each_candidate_once(self):
        for count in range(1, 30):
            items = [{"id": str(i), "properties": {"datetime": f"2025-07-{i+1:02}"}} for i in range(count)]
            result = imagery._spread(items)
            self.assertEqual(len(result), count)
            self.assertEqual({item["id"] for item in result}, {item["id"] for item in items})

    def test_routes_and_validation(self):
        client = TestClient(server.app)
        with patch.object(server, "_field_geometry", return_value=EXAMPLE_POLYGON), \
             patch.object(imagery, "images", return_value={"images": []}) as provider:
            self.assertEqual(client.get("/fields/example/imagery").json(), {"id": "example", "images": []})
            self.assertEqual(client.post("/imagery", json={"polygon": EXAMPLE_POLYGON}).status_code, 200)
            provider.side_effect = ProviderError("unavailable")
            self.assertEqual(client.get("/fields/example/imagery").status_code, 502)
            for query in ["maxCloud=101", "maxCloud=nan", "limit=21", "start=bad", "dataset=unknown"]:
                self.assertEqual(client.get(f"/fields/example/imagery?{query}").status_code, 422)
        with patch.object(server, "_field_geometry", return_value=None):
            self.assertEqual(client.get("/fields/missing/imagery").status_code, 404)
        for payload in [{"polygon": {}}, {"polygon": EXAMPLE_POLYGON, "unknown": True},
                        {"polygon": EXAMPLE_POLYGON, "start": "2025-09-01", "end": "2025-01-01"}]:
            self.assertEqual(client.post("/imagery", json=payload).status_code, 422)
