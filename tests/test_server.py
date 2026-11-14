import unittest

from fastapi.testclient import TestClient

import server


class TestServer(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def test_health(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

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
