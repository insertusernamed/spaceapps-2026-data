"""Spatially indexed field boundaries, queried one map viewport at a time."""

from __future__ import annotations

import json
import sqlite3
import zlib
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


def list_features(path: Path, bbox, limit=500, cursor=0):
    west, south, east, north = bbox
    with connect(path) as db:
        rows = db.execute(
            """SELECT f.rowid, f.feature FROM bounds b JOIN fields f ON f.rowid=b.rowid
            WHERE b.west<=? AND b.east>=? AND b.south<=? AND b.north>=? AND f.rowid>?
            ORDER BY f.rowid LIMIT ?""",
            (east, west, north, south, cursor, limit + 1),
        ).fetchall()
    return {
        "type": "FeatureCollection",
        "features": [
            json.loads(zlib.decompress(row[1]) if isinstance(row[1], bytes) else row[1])
            for row in rows[:limit]
        ],
        "numberReturned": min(len(rows), limit),
        "hasMore": len(rows) > limit,
        "nextCursor": rows[limit - 1][0] if len(rows) > limit else None,
        "queryBbox": list(bbox),
    }


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
