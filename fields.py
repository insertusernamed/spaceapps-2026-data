"""Read field boundaries and estimate their spherical area from WGS84 GeoJSON."""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

EARTH_RADIUS_M = 6_371_008.8
HECTARES_PER_ACRE = 0.40468564224


def parse_bbox(text: str) -> tuple[float, float, float, float]:
    try:
        values = tuple(float(v) for v in text.split(','))
        if len(values) != 4:
            raise ValueError
        west, south, east, north = values
        if not all(math.isfinite(v) for v in values):
            raise ValueError
        if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
            raise ValueError
        return west, south, east, north
    except ValueError as exc:
        raise ValueError('bbox must be west,south,east,north within longitude/latitude ranges') from exc


def _polygons(geometry: dict) -> list:
    kind = geometry.get('type')
    if kind == 'Polygon':
        return [geometry['coordinates']]
    if kind == 'MultiPolygon':
        return geometry['coordinates']
    raise ValueError('field geometry must be Polygon or MultiPolygon')


def _ring_area(ring: list) -> float:
    total = 0.0
    for first, second in zip(ring, ring[1:]):
        lon1, lat1 = map(math.radians, first[:2])
        lon2, lat2 = map(math.radians, second[:2])
        delta = (lon2 - lon1 + math.pi) % (2 * math.pi) - math.pi
        total += delta * (math.sin(lat1) + math.sin(lat2))
    return abs(total) * EARTH_RADIUS_M ** 2 / 2


def area_hectares(geometry: dict) -> float:
    return sum(_ring_area(rings[0]) - sum(_ring_area(hole) for hole in rings[1:])
               for rings in _polygons(geometry)) / 10_000


def load_fields(path: Path) -> dict[str, dict]:
    path = path.resolve()
    stat = path.stat()
    return _load_cached(str(path), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=4)
def _load_cached(path: str, _mtime: int, _size: int) -> dict[str, dict]:
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('type') != 'FeatureCollection' or not isinstance(data.get('features'), list):
        raise ValueError('field data must be a GeoJSON FeatureCollection')
    result = {}
    for feature in data['features']:
        field_id = feature.get('id')
        if field_id is None:
            field_id = (feature.get('properties') or {}).get('id')
        if field_id is None or str(field_id) in result:
            raise ValueError('every field must have a unique id')
        geometry = feature['geometry']
        polygons = _polygons(geometry)
        points = []
        if not polygons:
            raise ValueError('field geometry must not be empty')
        for rings in polygons:
            if not rings:
                raise ValueError('polygon must have an exterior ring')
            for ring in rings:
                if len(ring) < 4 or ring[0] != ring[-1]:
                    raise ValueError('polygon rings must be closed with at least four positions')
                for point in ring:
                    lon, lat = point[:2]
                    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
                        raise ValueError('invalid longitude/latitude')
                    points.append((lon, lat))
        if area_hectares(geometry) <= 0:
            raise ValueError('field area must be positive')
        field_id = str(field_id)
        result[field_id] = {
            'type': 'Feature', 'id': field_id, 'properties': {'id': field_id},
            'geometry': geometry,
            'bbox': [min(p[0] for p in points), min(p[1] for p in points),
                     max(p[0] for p in points), max(p[1] for p in points)],
        }
    return result


def field_map(fields: dict[str, dict], bbox: tuple | None = None) -> dict:
    def intersects(feature):
        west, south, east, north = feature['bbox']
        return bbox is None or (west <= bbox[2] and east >= bbox[0] and south <= bbox[3] and north >= bbox[1])
    return {'type': 'FeatureCollection', 'features': [f for f in fields.values() if intersects(f)]}


def acreage(feature: dict) -> dict:
    hectares = area_hectares(feature['geometry'])
    return {'id': feature['id'], 'hectares': round(hectares, 2),
            'acres': round(hectares / HECTARES_PER_ACRE, 2)}
