import json
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import field_store
import server
from fields import area_hectares


class TestFieldStore(unittest.TestCase):
    def test_indexed_selection_pagination_and_coverage(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "fields.sqlite"
            db = sqlite3.connect(path)
            field_store.initialize(db)
            for n in range(3):
                x = -79.5 + n * 0.01
                geometry = {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [x, 44.5],
                            [x + 0.005, 44.5],
                            [x + 0.005, 44.505],
                            [x, 44.505],
                            [x, 44.5],
                        ]
                    ],
                }
                feature = {
                    "type": "Feature",
                    "id": f"field-{n}",
                    "properties": {"id": f"field-{n}", "score": 80},
                    "geometry": geometry,
                    "bbox": [x, 44.5, x + 0.005, 44.505],
                }
                for _ in range(2):
                    field_store.add_feature(db, feature, area_hectares(geometry), 80)
            db.execute(
                "INSERT INTO metadata VALUES (?,?)", ("region", json.dumps("Ontario"))
            )
            db.commit()
            db.close()
            with patch.dict(os.environ, FIELDS_DB=str(path)):
                client = TestClient(server.app)
                first = client.get("/fields", params={"limit": 2}).json()
                self.assertEqual(first["numberReturned"], 2)
                self.assertTrue(first["hasMore"])
                second = client.get(
                    "/fields", params={"limit": 2, "cursor": first["nextCursor"]}
                ).json()
                self.assertEqual(second["numberReturned"], 1)
                self.assertFalse(second["hasMore"])
                self.assertEqual(
                    len({f["id"] for f in first["features"] + second["features"]}), 3
                )
                self.assertEqual(client.get("/field-coverage").json()["fields"], 3)
                area = client.get("/fields/field-0")
                self.assertEqual(area.status_code, 200)
                self.assertGreater(area.json()["acres"], 0)
                self.assertEqual(client.get("/fields/missing").status_code, 404)
                empty = client.get("/fields", params={"bbox": "0,0,1,1"}).json()
                self.assertEqual(empty["features"], [])
                touching = client.get(
                    "/fields", params={"bbox": "-79.501,44.501,-79.499,44.502"}
                ).json()
                self.assertEqual([f["id"] for f in touching["features"]], ["field-0"])
                self.assertEqual(
                    client.get("/fields", params={"limit": 2001}).status_code, 422
                )
                self.assertEqual(
                    client.get("/fields", params={"cursor": -1}).status_code, 422
                )
            path.unlink()
            with patch.dict(os.environ, FIELDS_DB=str(path)):
                self.assertEqual(client.get("/fields").status_code, 503)
                self.assertEqual(client.get("/field-coverage").status_code, 503)
