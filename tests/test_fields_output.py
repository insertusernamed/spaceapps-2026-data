"""/fields output: compact coordinates, unchanged structure, compression, caching."""
import json
import os
import sqlite3
import unittest
import zlib
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import field_store
import server

PREVIEW = Path(server.__file__).with_name("data") / "fields.geojson"


def feature(n, x=-79.5):
    ring = [[x + n * 0.01 + 0.123456789012, 44.5 + 0.987654321098], [x + n * 0.01 + 0.0051, 44.5],
            [x + n * 0.01 + 0.0051, 44.505], [x + n * 0.01, 44.505], [x + n * 0.01 + 0.123456789012, 44.5 + 0.987654321098]]
    return {"type": "Feature", "id": f"ftw-2025-17TPK_0_0-{n}", "properties": {"id": f"ftw-2025-17TPK_0_0-{n}", "score": 80},
            "geometry": {"type": "Polygon", "coordinates": [ring]}, "bbox": [x, 44.5, x + 0.13, 45.49]}


def build(path, count):
    with closing(sqlite3.connect(path)) as db:
        field_store.initialize(db)
        for n in range(count):
            field_store.add_feature(db, feature(n), 1.0, 80)
        db.commit()


class TestCompact(unittest.TestCase):
    def test_truncates_numbers_only(self):
        text = json.dumps(feature(7)).encode()
        out = json.loads(field_store.compact(text))
        self.assertEqual(out["id"], "ftw-2025-17TPK_0_0-7")
        self.assertEqual(out["properties"]["score"], 80)
        self.assertEqual(out["geometry"]["coordinates"][0][0], [-79.306543, 45.487654])
        self.assertEqual(out["geometry"]["coordinates"][0][1], [-79.4249, 44.5])
        for a, b in zip(sum(feature(7)["geometry"]["coordinates"], []), sum(out["geometry"]["coordinates"], []), strict=True):
            self.assertLess(abs(a[0] - b[0]), 1e-6)
            self.assertLess(abs(a[1] - b[1]), 1e-6)

    def test_negative_and_exponent_free(self):
        self.assertEqual(field_store.compact(b"[-0.1234567890,12.5,3]"), b"[-0.123456,12.5,3]")


class TestFieldsRoute(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "fields.sqlite"
        build(self.db, 120)
        env = patch.dict(os.environ, {"FIELDS_DB": str(self.db)})
        env.start()
        self.addCleanup(env.stop)

    def test_database_page_is_valid_geojson(self):
        resp = self.client.get("/fields", params={"bbox": "-80,44,-78,46", "limit": 100})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/json")
        body = resp.json()
        self.assertEqual(body["type"], "FeatureCollection")
        self.assertEqual((body["numberReturned"], body["hasMore"]), (100, True))
        self.assertEqual(len(body["features"]), 100)
        self.assertEqual(body["queryBbox"], [-80, 44, -78, 46])
        self.assertEqual(body["features"][0]["geometry"]["coordinates"][0][0], [-79.376543, 45.487654])
        last = self.client.get("/fields", params={"bbox": "-80,44,-78,46", "limit": 100, "cursor": body["nextCursor"]}).json()
        self.assertEqual((last["numberReturned"], last["hasMore"], last["nextCursor"]), (20, False, None))

    def test_default_page_is_small(self):
        body = self.client.get("/fields", params={"bbox": "-80,44,-78,46"}).json()
        self.assertEqual(body["numberReturned"], 50)

    def test_gzip_when_accepted(self):
        params = {"bbox": "-80,44,-78,46", "limit": 100}
        plain = self.client.get("/fields", params=params, headers={"Accept-Encoding": "identity"})
        zipped = self.client.get("/fields", params=params, headers={"Accept-Encoding": "gzip"})
        self.assertNotIn("content-encoding", plain.headers)
        self.assertEqual(zipped.headers["content-encoding"], "gzip")
        self.assertIn("x-request-id", zipped.headers)
        self.assertEqual(zipped.json(), plain.json())  # the client decompresses transparently

    def test_cache_follows_rebuilt_rows(self):
        first = self.client.get("/fields", params={"bbox": "-80,44,-78,46", "limit": 1}).json()["features"][0]
        moved = feature(0, x=-79.6)
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("UPDATE fields SET feature=? WHERE rowid=1", (zlib.compress(json.dumps(moved).encode()),))
            db.commit()
        second = self.client.get("/fields", params={"bbox": "-80,44,-78,46", "limit": 1}).json()["features"][0]
        self.assertNotEqual(first["geometry"], second["geometry"])
        self.assertEqual(second["geometry"]["coordinates"][0][1][0], -79.5949)

    def test_preview_file_is_compacted_too(self):
        env = {k: v for k, v in os.environ.items() if k != "FIELDS_DB"}
        with patch.dict(os.environ, {**env, "FIELDS_FILE": str(PREVIEW)}, clear=True):
            resp = self.client.get("/fields", params={"limit": 3})
        body = resp.json()
        self.assertEqual(resp.headers["content-type"], "application/json")
        self.assertEqual(body["numberReturned"], 3)
        coords = json.dumps(body["features"])
        self.assertNotRegex(coords, r"\.\d{7}")


if __name__ == "__main__":
    unittest.main()
