import os
import stat
import tempfile
import unittest
from pathlib import Path

import app


class FakeResponse:
    def __init__(self, payload=None):
        self.payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.last = None

    def get(self, url, params=None, timeout=None):
        self.last = (url, params)
        return FakeResponse({"ok": True})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / ".env"
        self._saved = {
            k: os.environ.pop(k, None) for k in ("EARTHDATA_USERNAME", "EARTHDATA_PASSWORD")
        }

    def tearDown(self):
        self.tmp.cleanup()
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_write_env_file_is_private(self):
        app.write_env_file("alice", "secret", self.env)
        self.assertEqual(self.env.read_text(), "EARTHDATA_USERNAME=alice\nEARTHDATA_PASSWORD=secret\n")
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)

    def test_load_credentials_reads_env(self):
        app.write_env_file("alice", "secret", self.env)
        self.assertEqual(app.load_credentials(self.env), ("alice", "secret"))

    def test_load_credentials_rejects_missing_vars(self):
        with self.assertRaises(app.EarthdataError):
            app.load_credentials(self.env)

    def test_fetch_daily_point_builds_params(self):
        session = FakeSession()
        app.fetch_daily_point(session, 34.05, -118.24, "20240101", "20240107")
        url, params = session.last
        self.assertEqual(url, app.POWER_URL)
        self.assertEqual(
            params,
            {
                "parameters": "T2M,PRECTOTCORR",
                "community": "RE",
                "latitude": 34.05,
                "longitude": -118.24,
                "start": "20240101",
                "end": "20240107",
                "format": "JSON",
            },
        )


if __name__ == "__main__":
    unittest.main()
