import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import field_store
import server


class TestServer(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def test_health(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_ready_with_readable_database(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "fields.sqlite"
            with closing(sqlite3.connect(path)) as db:
                field_store.initialize(db)
            with patch.dict(os.environ, FIELDS_DB=str(path)):
                resp = self.client.get("/ready")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ready": True, "checks": {"fields": "ok", "source": "database"}})

    def test_ready_fails_when_database_missing_or_invalid(self):
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.sqlite"
            empty = Path(tmp) / "empty.sqlite"
            sqlite3.connect(empty).close()
            for path in (missing, empty):
                with self.subTest(path=path.name), patch.dict(os.environ, FIELDS_DB=str(path)):
                    resp = self.client.get("/ready")
                    self.assertEqual(resp.status_code, 503)
                    self.assertEqual(resp.json()["checks"]["fields"], "unavailable")
                    self.assertNotIn(tmp, resp.text)

    def test_ready_with_geojson_file(self):
        with TemporaryDirectory() as tmp:
            good = Path(tmp) / "fields.geojson"
            good.write_text('{"type": "FeatureCollection", "features": []}')
            bad = Path(tmp) / "bad.geojson"
            bad.write_text("not json")
            env = {k: v for k, v in os.environ.items() if k != "FIELDS_DB"}
            with patch.dict(os.environ, {**env, "FIELDS_FILE": str(good)}, clear=True):
                self.assertEqual(self.client.get("/ready").json(), {"ready": True, "checks": {"fields": "ok", "source": "geojson"}})
            with patch.dict(os.environ, {**env, "FIELDS_FILE": str(bad)}, clear=True):
                self.assertEqual(self.client.get("/ready").status_code, 503)

    def test_daily_point_passes_query_through(self):
        captured = {}

        def fake_fetch(session, lat, lon, start, end, parameters):
            captured.update(lat=lat, lon=lon, start=start, end=end, parameters=parameters)
            return {"ok": True}

        original = (server.load_credentials, server.fetch_daily_point)
        server.load_credentials = lambda: ("u", "p")
        server.fetch_daily_point = fake_fetch
        try:
            resp = self.client.get(
                "/daily-point",
                params={"lat": 34.05, "lon": -118.24, "start": "20240101", "end": "20240107"},
            )
        finally:
            server.load_credentials, server.fetch_daily_point = original

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual(captured["parameters"], ("T2M", "PRECTOTCORR"))

    def test_daily_point_rejects_bad_lat(self):
        self.assertEqual(
            self.client.get(
                "/daily-point",
                params={"lat": 120, "lon": 0, "start": "20240101", "end": "20240107"},
            ).status_code,
            422,
        )


if __name__ == "__main__":
    unittest.main()
