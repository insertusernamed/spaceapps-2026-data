"""Spatially indexed field boundaries, queried one map viewport at a time."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import zlib
from collections import OrderedDict
from contextlib import closing
from pathlib import Path

ORILLIA_BBOX = (-79.7, 44.35, -79.05, 44.85)


def connect(path: Path):
    return closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True))


def readable(path: Path) -> None:
    """Raise unless the index opens read-only and its field table can be queried."""
    with connect(path) as db:
        db.execute("SELECT 1 FROM fields LIMIT 1").fetchall()


def initialize(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS fields (
            rowid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
            feature TEXT NOT NULL, hectares REAL NOT NULL, score INTEGER
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS bounds USING rtree(rowid, west, east, south, north);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)


def add_feature(connection, feature, hectares, score=None):
    fid = feature["id"]
    cursor = connection.execute(
        "INSERT INTO fields(id,feature,hectares,score) VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING",
        (
            fid,
            sqlite3.Binary(
                zlib.compress(json.dumps(feature, separators=(",", ":")).encode())
            ),
            hectares,
            score,
        ),
    )
    if cursor.rowcount:
        west, south, east, north = feature["bbox"]
        connection.execute(
            "INSERT INTO bounds VALUES (?,?,?,?,?)",
            (cursor.lastrowid, west, east, south, north),
        )
    return bool(cursor.rowcount)


# Coordinates are stored with up to 15 decimals; 6 (about 0.1 m) is far finer than the
# 10 m imagery the outlines come from, and cuts the response size by about 40%.
_EXTRA_DECIMALS = re.compile(rb"(?<=\.\d{6})\d+")
_CACHE_BYTES = 64 * 1024 * 1024
_cache: OrderedDict[tuple[int, int], bytes] = OrderedDict()
_cache_size = 0
_cache_lock = threading.Lock()


def compact(feature_json: bytes) -> bytes:
    """Shorten every number in a GeoJSON text to at most 6 decimals (truncated, not rounded)."""
    return _EXTRA_DECIMALS.sub(b"", feature_json)


def _compact_row(rowid: int, stored) -> bytes:
    """Compacted feature JSON for one stored row, reused while the stored value is unchanged."""
    global _cache_size
    data = stored if isinstance(stored, bytes) else stored.encode()
    key = (rowid, zlib.crc32(data))
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    out = compact(zlib.decompress(data) if isinstance(stored, bytes) else data)
    with _cache_lock:
        if key not in _cache:
            _cache[key] = out
            _cache_size += len(out)
            while _cache_size > _CACHE_BYTES:
                _, old = _cache.popitem(last=False)
                _cache_size -= len(old)
    return out


def feature_collection(features: list[bytes], **meta) -> bytes:
    """A GeoJSON FeatureCollection from already-serialized features, plus paging fields."""
    tail = json.dumps(meta, separators=(",", ":"))[1:]
    return b'{"type":"FeatureCollection","features":[' + b",".join(features) + b"]" + (b"," + tail.encode() if meta else b"}")


def list_features(path: Path, bbox, limit=500, cursor=0) -> bytes:
    """One page of compacted features as JSON bytes, without parsing the stored GeoJSON."""
    west, south, east, north = bbox
    with connect(path) as db:
        rows = db.execute(
            """SELECT f.rowid, f.feature FROM bounds b JOIN fields f ON f.rowid=b.rowid
            WHERE b.west<=? AND b.east>=? AND b.south<=? AND b.north>=? AND f.rowid>?
            ORDER BY f.rowid LIMIT ?""",
            (east, west, north, south, cursor, limit + 1),
        ).fetchall()
    return feature_collection(
        [_compact_row(rowid, stored) for rowid, stored in rows[:limit]],
        numberReturned=min(len(rows), limit),
        hasMore=len(rows) > limit,
        nextCursor=rows[limit - 1][0] if len(rows) > limit else None,
        queryBbox=list(bbox),
    )


def feature(path: Path, field_id):
    """The stored GeoJSON feature for one field ID, or None."""
    with connect(path) as db:
        row = db.execute("SELECT feature FROM fields WHERE id=?", (field_id,)).fetchone()
    if row is None:
        return None
    return json.loads(zlib.decompress(row[0]) if isinstance(row[0], bytes) else row[0])


def selected(path: Path, field_id):
    with connect(path) as db:
        row = db.execute(
            "SELECT hectares FROM fields WHERE id=?", (field_id,)
        ).fetchone()
    if row is None:
        return None
    return {
        "id": field_id,
        "hectares": round(row[0], 2),
        "acres": round(row[0] / 0.40468564224, 2),
    }


def coverage(path: Path):
    with connect(path) as db:
        metadata = {
            key: json.loads(value)
            for key, value in db.execute("SELECT key,value FROM metadata")
        }
        metadata["fields"] = metadata.get("fieldCount")
        if metadata["fields"] is None:
            metadata["fields"] = db.execute("SELECT count(*) FROM fields").fetchone()[0]
    return metadata
