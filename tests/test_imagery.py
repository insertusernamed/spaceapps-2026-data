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


class TestImagery(unittest.TestCase):
    def test_hls_combines_collections_and_renders_each_correctly(self):
        response = Mock()
        response.json.return_value = {"features": [
            {"id": f"HLS.{kind}.TEST.v2.0", "collection": collection,
             "properties": {"datetime": f"2025-07-0{day}T16:00:00Z", "platform": platform, "eo:cloud_cover": 5}}
            for kind, collection, platform, day in [
                ("S30", "hls2-s30", "sentinel-2b", 2), ("L30", "hls2-l30", "landsat-9", 1)]]}
        with patch.object(imagery.requests, "post", return_value=response) as post:
            result = imagery.images(EXAMPLE_POLYGON)
        self.assertEqual(post.call_args.kwargs["json"]["collections"], ["hls2-l30", "hls2-s30"])
        self.assertEqual(result["dataset"], "hls")
        self.assertEqual(result["source"]["resolutionM"], 30)
        for scene in result["images"]:
            query = parse_qs(urlparse(scene["imageUrl"]).query)
            self.assertEqual(query["collection"], [scene["collection"]])
            self.assertEqual(query["assets"], ["B04", "B03", "B02"])
            self.assertEqual(urlparse(scene["imageUrl"]).path, "/api/data/v1/item/bbox/-88.95,40.45,-88.947,40.453.png")
            self.assertEqual(query["max_size"], ["1024"])
            self.assertEqual(query["rescale"], ["0,3000"])
            self.assertEqual(query["color_formula"], ["gamma RGB 1.4"])
            self.assertEqual(query["nodata"], ["-9999"])
            self.assertIn(f"/collections/{scene['collection']}/", scene["sceneUrl"])

    def test_search_and_rgb_scene_contract(self):
        item = {"id": "S2C_TEST", "collection": "sentinel-2-l2a", "properties": {
            "datetime": "2025-07-01T16:00:00Z", "platform": "sentinel-2c", "eo:cloud_cover": 12}}
        response = Mock()
        response.json.return_value = {"features": [item], "links": [{"rel": "next"}]}
        with patch.object(imagery.requests, "post", return_value=response) as post:
            result = imagery.images(EXAMPLE_POLYGON, date(2025, 6, 1), date(2025, 9, 1), 20, 1, "sentinel-2")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["collections"], ["sentinel-2-l2a"])
        self.assertNotIn("platform", body["query"])
        self.assertEqual(body["query"]["eo:cloud_cover"], {"lte": 20})
        self.assertEqual(body["sortby"], [{"field": "datetime", "direction": "desc"}])
        self.assertEqual(body["intersects"]["coordinates"][0], EXAMPLE_POLYGON["coordinates"])
        url = urlparse(result["images"][0]["imageUrl"])
        self.assertEqual(url.path, "/api/data/v1/item/bbox/-88.95,40.45,-88.947,40.453.png")
        self.assertEqual(result["bbox"], [-88.95, 40.45, -88.947, 40.453])
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
                    self.assertEqual(imagery.images(EXAMPLE_POLYGON)["images"], [])
                else:
                    with self.assertRaises(ProviderError):
                        imagery.images(EXAMPLE_POLYGON)
        with patch.object(imagery.requests, "post", side_effect=requests.Timeout):
            with self.assertRaises(ProviderError):
                imagery.images(EXAMPLE_POLYGON)

    def test_invalid_dates_do_not_contact_provider(self):
        with patch.object(imagery.requests, "post") as post:
            for start, end in [(date(2010, 1, 1), date(2010, 2, 1)),
                               (date(2024, 1, 1), date(2025, 9, 1)),
                               (date(2025, 9, 1), date(2025, 1, 1))]:
                with self.assertRaises(ValidationError):
                    imagery.images(EXAMPLE_POLYGON, start, end)
            post.assert_not_called()

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
