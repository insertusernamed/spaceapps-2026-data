import importlib
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import middleware
import server

PREFLIGHT = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}


def client_with(cors_origins=None):
    env = {k: v for k, v in os.environ.items() if k != "CORS_ORIGINS"}
    if cors_origins is not None:
        env["CORS_ORIGINS"] = cors_origins
    with patch.dict(os.environ, env, clear=True):
        return TestClient(importlib.reload(server).app)


class TestRequestId(unittest.TestCase):
    def setUp(self):
        self.client = client_with()

    def test_generates_an_id(self):
        first = self.client.get("/health").headers["x-request-id"]
        second = self.client.get("/health").headers["x-request-id"]
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertNotEqual(first, second)

    def test_echoes_a_safe_caller_id(self):
        resp = self.client.get("/health", headers={"X-Request-Id": "frontend-42.a:b"})
        self.assertEqual(resp.headers["x-request-id"], "frontend-42.a:b")

    def test_replaces_unsafe_caller_ids(self):
        for bad in ["has space", "x" * 129, "<script>", "line\tbreak"]:
            with self.subTest(bad=bad):
                resp = self.client.get("/health", headers={"X-Request-Id": bad})
                self.assertRegex(resp.headers["x-request-id"], r"^[0-9a-f]{32}$")

    def test_errors_carry_an_id(self):
        resp = self.client.get("/fields/not-a-field")
        self.assertIn(resp.status_code, (404, 503))
        self.assertIn("x-request-id", resp.headers)

    def test_logs_the_request(self):
        with self.assertLogs("uvicorn.error.requests", level="INFO") as logs:
            self.client.get("/health", headers={"X-Request-Id": "trace-me"})
        self.assertIn("trace-me GET /health 200", logs.output[0])


class TestCors(unittest.TestCase):
    def tearDown(self):
        client_with()  # restore the default app for other tests

    def test_off_by_default(self):
        client = client_with()
        for origin in ["http://localhost:5173", "https://example.com"]:
            with self.subTest(origin=origin):
                resp = client.get("/health", headers={"Origin": origin})
                self.assertEqual(resp.status_code, 200)
                self.assertNotIn("access-control-allow-origin", resp.headers)
                self.assertIn("x-request-id", resp.headers)
        preflight = client.options("/field-summary", headers={"Origin": "http://localhost:5173", **PREFLIGHT})
        self.assertNotIn("access-control-allow-origin", preflight.headers)

    def test_empty_setting_stays_off(self):
        resp = client_with(" , ").get("/health", headers={"Origin": "http://localhost:5173"})
        self.assertNotIn("access-control-allow-origin", resp.headers)

    def test_localhost_keyword_allows_any_local_port(self):
        client = client_with("localhost")
        for origin in ["http://localhost:5173", "http://127.0.0.1:3000", "http://localhost"]:
            with self.subTest(origin=origin):
                resp = client.options("/field-summary", headers={"Origin": origin, **PREFLIGHT})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.headers["access-control-allow-origin"], origin)
        resp = client.get("/health", headers={"Origin": "http://localhost:5173"})
        self.assertIn("x-request-id", resp.headers["access-control-expose-headers"].lower())
        for origin in ["https://example.com", "http://localhost.evil.com", "http://evil.com/http://localhost"]:
            with self.subTest(origin=origin):
                self.assertNotIn("access-control-allow-origin", client.get("/health", headers={"Origin": origin}).headers)

    def test_listed_origins_only(self):
        client = client_with("https://app.example.org/, https://other.example.org")
        for origin in ["https://app.example.org", "https://other.example.org"]:
            with self.subTest(origin=origin):
                resp = client.get("/health", headers={"Origin": origin})
                self.assertEqual(resp.headers["access-control-allow-origin"], origin)
        for origin in ["https://example.com", "http://localhost:5173"]:
            with self.subTest(origin=origin):
                self.assertNotIn("access-control-allow-origin", client.get("/health", headers={"Origin": origin}).headers)

    def test_wildcard_allows_any_origin(self):
        resp = client_with("*").get("/health", headers={"Origin": "https://anything.example"})
        self.assertEqual(resp.headers["access-control-allow-origin"], "*")

    def test_parse(self):
        self.assertEqual(middleware.cors_origins(None), [])
        self.assertEqual(middleware.cors_origins(" a/ ,, b "), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
