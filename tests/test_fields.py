import json
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import fields
import server


def square(x=0, size=0.01):
    return [[x, 0], [x + size, 0], [x + size, size], [x, size], [x, 0]]


class TestFields(unittest.TestCase):
    def test_area_holes_and_multipolygon(self):
        ring = square()
        expected = fields.EARTH_RADIUS_M ** 2 * math.radians(0.01) * math.sin(math.radians(0.01)) / 10000
        area = lambda rings: fields.area_hectares({'type': 'Polygon', 'coordinates': rings})
        self.assertAlmostEqual(area([ring]), expected, places=6)
        self.assertAlmostEqual(area([ring[::-1]]), expected, places=6)
        self.assertAlmostEqual(area([ring, square(size=0.005)]), expected - area([square(size=0.005)]), places=6)
        self.assertAlmostEqual(fields.area_hectares({'type': 'MultiPolygon', 'coordinates': [[ring], [square(1)]]}), 2 * expected, places=6)

    def test_selection_flow(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fields.geojson'
            features = [{'type': 'Feature', 'id': str(i), 'properties': {'crop': 'secret'},
                         'geometry': {'type': 'Polygon', 'coordinates': [square(i)]}} for i in range(2)]
            path.write_text(json.dumps({'type': 'FeatureCollection', 'features': features}))
            with patch.dict(os.environ, FIELDS_FILE=str(path)):
                client = TestClient(server.app)
                response = client.get('/fields')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()['features']), 2)
                self.assertEqual(response.json()['features'][0]['properties'], {'id': '0'})
                filtered = client.get('/fields', params={'bbox': '-0.01,-0.01,0.02,0.02'}).json()
                self.assertEqual([f['id'] for f in filtered['features']], ['0'])
                self.assertEqual(client.get('/fields', params={'bbox': '5,5,6,6'}).json()['features'], [])
                area = client.get('/fields/0').json()
                self.assertAlmostEqual(area['hectares'], 123.64, places=2)
                self.assertAlmostEqual(area['acres'], 305.53, delta=0.02)
                self.assertEqual(client.get('/fields/missing').status_code, 404)
                for bbox in ['bad', '1,2,3', 'nan,0,1,1', '0,0,181,1', '3,0,1,1']:
                    self.assertEqual(client.get('/fields', params={'bbox': bbox}).status_code, 422)
                path.unlink()
                self.assertEqual(client.get('/fields').status_code, 503)
                path.write_text('{broken')
                self.assertEqual(client.get('/fields').status_code, 503)

    def test_bundled_data(self):
        dataset = fields.load_fields(server.DEFAULT_FIELDS_FILE)
        self.assertEqual(len(dataset), 300)
        for feature in dataset.values():
            self.assertGreater(fields.acreage(feature)['hectares'], 0)
            self.assertEqual(set(feature['properties']), {'id'})
