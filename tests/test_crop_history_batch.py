"""POST /fields/crop-history: many fields per request, isolated failures, bounded upstream load."""
import os
import sqlite3
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import field_store
import server
from crop_history import MAX_BATCH_FIELDS
from environmental import crops
from environmental.errors import ProviderError


def feature(n):
    x = -79.5 + n * 0.01
    ring = [[x, 44.5], [x + 0.005, 44.5], [x + 0.005, 44.505], [x, 44.505], [x, 44.5]]
    return {"type": "Feature", "id": f"field-{n}", "properties": {"id": f"field-{n}"},
            "geometry": {"type": "Polygon", "coordinates": [ring]}, "bbox": [x, 44.5, x + 0.005, 44.505]}


class TestBatchRoute(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "fields.sqlite"
        with closing(sqlite3.connect(self.db)) as db:
            field_store.initialize(db)
            for n in range(5):
                field_store.add_feature(db, feature(n), 1.0)
            db.commit()
        env = patch.dict(os.environ, {"FIELDS_DB": str(self.db)})
        env.start()
        self.addCleanup(env.stop)

    def post(self, ids):
        return self.client.post("/fields/crop-history", json={"ids": ids})

    def fake_history(self, fail=()):
        def history(polygon):
            x = polygon["coordinates"][0][0][0]
            n = round((x + 79.5) / 0.01)
            if n in fail:
                raise ProviderError("AAFC Annual Crop Inventory is unavailable")
            return {"rotation": [f"crop-{n}"], "years": []}
        return patch.object(crops, "history", side_effect=history)

    def test_results_follow_request_order_with_duplicates_once(self):
        with self.fake_history():
            body = self.post(["field-3", "field-0", "field-3", " field-1 "]).json()
        self.assertEqual([r["id"] for r in body["results"]], ["field-3", "field-0", "field-1"])
        self.assertEqual([r["history"]["rotation"] for r in body["results"]], [["crop-3"], ["crop-0"], ["crop-1"]])
        self.assertEqual((body["requested"], body["succeeded"], body["failed"]), (3, 3, 0))

    def test_unknown_and_failing_fields_do_not_fail_the_batch(self):
        with self.fake_history(fail={2}) as history:
            resp = self.post(["field-1", "missing", "field-2"])
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["results"][0]["status"], "ok")
        self.assertEqual(body["results"][1], {"id": "missing", "status": "error", "error": {"code": 404, "detail": "Field not found"}})
        self.assertEqual(body["results"][2]["error"], {"code": 502, "detail": "AAFC Annual Crop Inventory is unavailable"})
        self.assertEqual((body["succeeded"], body["failed"]), (1, 2))
        self.assertEqual(history.call_count, 2)  # unknown IDs never reach the providers

    def test_outlines_are_read_in_one_query(self):
        with self.fake_history(), patch.object(field_store, "features", wraps=field_store.features) as lookup:
            self.post([f"field-{n}" for n in range(5)])
        self.assertEqual(lookup.call_count, 1)

    def test_request_validation(self):
        for ids in ([], [""], ["   "], ["x" * 201], [f"f{n}" for n in range(MAX_BATCH_FIELDS + 1)]):
            with self.subTest(n=len(ids)):
                self.assertEqual(self.post(ids).status_code, 422)
        self.assertEqual(self.client.post("/fields/crop-history", json={"ids": ["field-1"], "extra": 1}).status_code, 422)
        self.assertEqual(self.client.post("/fields/crop-history", json={"ids": "field-1"}).status_code, 422)
        with self.fake_history():
            self.assertEqual(self.post([f"f{n}" for n in range(MAX_BATCH_FIELDS)]).status_code, 200)

    def test_database_unavailable_is_503(self):
        with patch.dict(os.environ, {"FIELDS_DB": "/nonexistent/fields.sqlite"}):
            self.assertEqual(self.post(["field-1"]).status_code, 503)


class TestUpstreamLimit(unittest.TestCase):
    def test_concurrent_provider_requests_are_capped(self):
        active, peak, lock = 0, 0, threading.Lock()

        def slow_request(method, url, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            raise crops.requests.ConnectionError()

        def call(_):
            try:
                crops._get_json("test", "get", "https://example.invalid")
            except ProviderError:
                pass

        with patch.object(crops._session, "request", side_effect=slow_request):
            threads = [threading.Thread(target=call, args=(i,)) for i in range(40)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(peak, crops.UPSTREAM_LIMIT)

    def test_history_many_isolates_failures(self):
        def history(polygon):
            if polygon == "bad":
                raise ProviderError("down")
            return {"ok": polygon}

        with patch.object(crops, "history", side_effect=history):
            out = crops.history_many({"a": "good", "b": "bad"})
        self.assertEqual(out["a"], {"ok": "good"})
        self.assertIsInstance(out["b"], ProviderError)


class TestFeatureLookup(unittest.TestCase):
    def test_features_returns_only_known_ids(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fields.sqlite"
            with closing(sqlite3.connect(db_path)) as db:
                field_store.initialize(db)
                field_store.add_feature(db, feature(1), 1.0)
                db.commit()
            found = field_store.features(db_path, ["field-1", "nope"])
            self.assertEqual(list(found), ["field-1"])
            self.assertEqual(found["field-1"]["geometry"]["type"], "Polygon")
            self.assertEqual(field_store.features(db_path, []), {})


if __name__ == "__main__":
    unittest.main()
