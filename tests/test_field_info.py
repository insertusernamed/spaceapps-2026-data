"""Quick field facts attached to every outline served by /fields."""
import json
import math
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import field_info
import field_store
import server

M_PER_DEG = field_info.METRES_PER_DEGREE


def rectangle(lon, lat, east_m, north_m, angle_deg=0.0, hole=None):
    """A rectangle east_m x north_m (before rotating counter-clockwise by angle_deg) centred at lon, lat."""
    a = math.radians(angle_deg)
    kx = M_PER_DEG * math.cos(math.radians(lat))

    def ring(w, h):
        pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2), (-w / 2, -h / 2)]
        return [[lon + (x * math.cos(a) - y * math.sin(a)) / kx, lat + (x * math.sin(a) + y * math.cos(a)) / M_PER_DEG] for x, y in pts]

    rings = [ring(east_m, north_m)] + ([ring(*hole)[::-1]] if hole else [])
    return {"type": "Feature", "id": "ftw-2025-17TPK_0_0-1", "properties": {"id": "ftw-2025-17TPK_0_0-1", "score": 80},
            "geometry": {"type": "Polygon", "coordinates": rings}}


class TestDescribe(unittest.TestCase):
    def test_rectangle_facts(self):
        facts = field_info.describe(rectangle(-79.5, 44.5, 400, 100))
        self.assertEqual((facts["lengthM"], facts["widthM"], facts["perimeterM"]), (400, 100, 1000))
        self.assertEqual(facts["orientationDeg"], 90)  # long side runs east-west
        self.assertAlmostEqual(facts["areaHa"], 4.0, places=2)
        self.assertAlmostEqual(facts["areaAcres"], 9.88, places=2)
        self.assertAlmostEqual(facts["compactness"], 0.5, places=2)  # 4*pi*40000/1000^2
        self.assertEqual((facts["holes"], facts["parts"], facts["sourceYear"]), (0, 1, 2025))
        self.assertAlmostEqual(facts["centroid"][0], -79.5, places=5)
        self.assertAlmostEqual(facts["centroid"][1], 44.5, places=5)

    def test_rotated_fields_report_their_long_axis(self):
        for angle, bearing in ((30, 60), (90, 0), (135, 135)):
            with self.subTest(angle=angle):
                facts = field_info.describe(rectangle(-79.5, 44.5, 300, 120, angle))
                self.assertEqual((facts["lengthM"], facts["widthM"]), (300, 120))
                self.assertEqual(facts["orientationDeg"], bearing)

    def test_holes_reduce_area_and_add_perimeter(self):
        facts = field_info.describe(rectangle(-79.5, 44.5, 200, 200, hole=(50, 50)))
        self.assertEqual(facts["holes"], 1)
        self.assertAlmostEqual(facts["areaHa"], 3.75, places=2)
        self.assertEqual(facts["perimeterM"], 1000)

    def test_multipolygon_and_stored_area(self):
        a, b = rectangle(-79.5, 44.5, 100, 100), rectangle(-79.49, 44.5, 100, 100)
        multi = {"type": "Feature", "id": "x", "properties": {},
                 "geometry": {"type": "MultiPolygon", "coordinates": [a["geometry"]["coordinates"], b["geometry"]["coordinates"]]}}
        facts = field_info.describe(multi, hectares=2.5)
        self.assertEqual((facts["parts"], facts["areaHa"], facts["sourceYear"]), (2, 2.5, None))

    def test_batch_matches_single(self):
        feats = [rectangle(-79.5 + i * 0.01, 44.5, 100 + 50 * i, 80, 15 * i) for i in range(6)]
        self.assertEqual(field_info.describe_many(feats), [field_info.describe(f) for f in feats])
        self.assertEqual(field_info.describe_many([]), [])


class TestServedFacts(unittest.TestCase):
    def test_facts_travel_with_outlines(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fields.sqlite"
            with closing(sqlite3.connect(db_path)) as db:
                field_store.initialize(db)
                f = rectangle(-79.5, 44.5, 400, 100)
                field_store.add_feature(db, {**f, "bbox": [-79.51, 44.49, -79.49, 44.51]}, 4.01, 80)
                empty = {**rectangle(-79.4, 44.5, 100, 100), "id": "other", "properties": {}, "bbox": [-79.41, 44.49, -79.39, 44.51]}
                field_store.add_feature(db, empty, 1.0)
                db.commit()
            with patch.dict(os.environ, {"FIELDS_DB": str(db_path)}):
                body = TestClient(server.app).get("/fields", params={"bbox": "-80,44,-79,45"}).json()
        first, second = (feature["properties"] for feature in body["features"])
        self.assertEqual(first["id"], "ftw-2025-17TPK_0_0-1")
        self.assertEqual(first["score"], 80)
        self.assertEqual(first["areaHa"], 4.01)  # the stored area is reused
        self.assertEqual((first["lengthM"], first["widthM"], first["orientationDeg"]), (400, 100, 90))
        self.assertEqual(second["areaHa"], 1.0)  # empty properties get facts too
        self.assertEqual(second["holes"], 0)

    def test_with_facts_keeps_valid_json(self):
        facts = {"areaHa": 1.5, "centroid": [-79.1, 44.2]}
        for raw in (b'{"type":"Feature","properties":{"id":"a"},"geometry":null}',
                    b'{"type":"Feature","properties":{},"geometry":null}',
                    b'{"type":"Feature","geometry":null}'):
            with self.subTest(raw=raw):
                out = json.loads(field_store.with_facts(raw, facts))
                self.assertEqual(out["properties"]["areaHa"], 1.5)
                self.assertEqual(out["properties"]["centroid"], [-79.1, 44.2])


if __name__ == "__main__":
    unittest.main()
