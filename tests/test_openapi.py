"""Keep Swagger UI truthful: every route is described, and its examples work."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api_docs
import server
from field_summary import FieldSummaryRequest

PREVIEW = Path(server.__file__).with_name("data") / "fields.geojson"


def preview_env():
    """Serve the committed preview, whatever database exists on this machine."""
    env = {k: v for k, v in os.environ.items() if k not in ("FIELDS_DB", "FIELDS_FILE")}
    return patch.dict(os.environ, {**env, "FIELDS_FILE": str(PREVIEW)}, clear=True)


class TestOpenAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.spec = self.client.get("/openapi.json").json()
        self.operations = [(path, method, op) for path, ops in self.spec["paths"].items() for method, op in ops.items()]

    def test_every_route_is_tagged_and_described(self):
        known = {tag["name"] for tag in self.spec["tags"]}
        for path, method, op in self.operations:
            with self.subTest(route=f"{method.upper()} {path}"):
                self.assertTrue(op.get("summary"))
                self.assertGreater(len(op.get("description", "")), 40)
                self.assertEqual(len(op.get("tags", [])), 1)
                self.assertIn(op["tags"][0], known)
                for param in op.get("parameters", []):
                    self.assertTrue(param.get("description"), param["name"])
                    for sample in param.get("examples", {}).values():
                        self.assertIn("value", sample, param["name"])

    def test_every_tag_is_used(self):
        used = {op["tags"][0] for _, _, op in self.operations}
        self.assertEqual(used, {tag["name"] for tag in self.spec["tags"]})

    def test_root_redirects_to_docs(self):
        resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 307)
        self.assertEqual(resp.headers["location"], "/docs")

    def test_field_examples_work_against_preview(self):
        with preview_env():
            fields = self.client.get("/fields", params={"bbox": api_docs.EXAMPLE_BBOX, "limit": 5})
            field = self.client.get(f"/fields/{api_docs.EXAMPLE_FIELD_ID}")
            coverage = self.client.get("/field-coverage")
        self.assertEqual(fields.status_code, 200)
        self.assertGreater(fields.json()["numberReturned"], 0)
        self.assertEqual(field.status_code, 200)
        self.assertEqual(field.json(), api_docs.FIELD["responses"][200]["content"]["application/json"]["example"])
        self.assertEqual(coverage.status_code, 200)

    def test_documented_error_examples_match_responses(self):
        def example(route, code):
            return route["responses"][code]["content"]["application/json"]["example"]

        with preview_env():
            missing = self.client.get("/fields/not-a-field")
            bad_bbox = self.client.get("/fields", params={"bbox": "1,2"})
        self.assertEqual(missing.json(), example(api_docs.FIELD, 404))
        self.assertEqual(bad_bbox.status_code, 422)
        self.assertEqual(bad_bbox.json(), example(api_docs.FIELDS, 422))
        with patch.dict(os.environ, FIELDS_DB="/nonexistent/fields.sqlite"):
            unavailable = self.client.get("/fields")
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.json(), example(api_docs.FIELDS, 503))

    def test_ready_examples_match_responses(self):
        with preview_env():
            body = self.client.get("/ready").json()
        documented = api_docs.READY["responses"][200]["content"]["application/json"]["example"]
        self.assertEqual(set(body), set(documented))
        self.assertEqual(set(body["checks"]), set(documented["checks"]))

    def test_field_summary_examples_are_valid_requests(self):
        examples = FieldSummaryRequest.model_config["json_schema_extra"]["examples"]
        self.assertTrue(examples)
        for body in examples:
            FieldSummaryRequest.model_validate(body)

    def test_daily_point_parameter_examples_are_accepted(self):
        params = {}
        for param in self.spec["paths"]["/daily-point"]["get"]["parameters"]:
            if param.get("required"):
                self.assertIn("examples", param, param["name"])
                params[param["name"]] = next(iter(param["examples"].values()))["value"]
        with patch("server.load_credentials", return_value=("u", "p")), \
             patch("server.EarthdataSession"), \
             patch("server.fetch_daily_point", return_value={"ok": True}):
            resp = self.client.get("/daily-point", params=params)
        self.assertEqual(resp.status_code, 200, resp.text)


if __name__ == "__main__":
    unittest.main()
