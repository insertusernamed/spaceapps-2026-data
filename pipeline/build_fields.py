"""Build indexed Ontario field coverage from public 2025 boundary predictions."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from shapely.geometry import box, shape
from shapely.prepared import prep

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from field_store import ORILLIA_BBOX, add_feature, initialize
from fields import area_hectares

SOURCE = "https://data.source.coop/ftw/global-data-2e/vector/2025/zone={zone:02d}/utm{zone:02d}.parquet"


def build(region, output):
    boundary = json.loads((ROOT / "data/boundaries/ontario.geojson").read_text())
    province = shape(boundary["geometry"])
    province_filter = prep(province)
    bbox = ORILLIA_BBOX if region == "orillia" else province.bounds
    west, south, east, north = bbox
    zones = range(int((west + 180) // 6) + 1, int((east + 180) // 6) + 2)
    cache = ROOT / "data/downloads"
    cache.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(output)
    initialize(db)
    metadata = {
        "region": region,
        "status": "building",
        "bbox": list(bbox),
        "source": "Fields of the World, Global 2nd Edition, 2025",
        "sourceUrl": "https://source.coop/ftw/global-data-2e",
        "license": "CC-BY-4.0",
        "description": "Predicted agricultural field units; not farm ownership parcels or an exhaustive farm registry.",
        "builtAt": datetime.now(timezone.utc).isoformat(),
        "zonesCompleted": [],
    }

    def save_metadata():
        db.executemany(
            "INSERT OR REPLACE INTO metadata VALUES (?,?)",
            [(k, json.dumps(v)) for k, v in metadata.items()],
        )
        db.commit()

    save_metadata()
    for zone in zones:
        print(f"{region}: reading UTM zone {zone}", flush=True)
        zone_region = province.intersection(
            box(-180 + (zone - 1) * 6, south, -180 + zone * 6, north)
        )
        if zone_region.is_empty:
            continue
        zw, zs, ze, zn = zone_region.bounds
        query_bbox = (max(west, zw), max(south, zs), min(east, ze), min(north, zn))
        con = duckdb.connect()
        con.execute(
            "INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET threads=2; SET memory_limit='2GB'; SET preserve_insertion_order=false"
        )
        extract = cache / f"{region}-2025-zone-{zone}.parquet"
        if not extract.exists():
            temp = extract.with_suffix(".partial.parquet")
            temp.unlink(missing_ok=True)
            con.execute(
                """COPY (SELECT id, score, ST_AsGeoJSON(geometry) geometry FROM read_parquet($url)
                WHERE bbox.xmax >= $west AND bbox.xmin <= $east AND bbox.ymax >= $south AND bbox.ymin <= $north
                ) TO $output (FORMAT PARQUET, ROW_GROUP_SIZE 10000)""",
                {
                    "url": SOURCE.format(zone=zone),
                    "west": query_bbox[0],
                    "east": query_bbox[2],
                    "south": query_bbox[1],
                    "north": query_bbox[3],
                    "output": str(temp),
                },
            )
            temp.replace(extract)
        cursor = con.execute(
            "SELECT id,score,geometry FROM read_parquet(?)", [str(extract)]
        )
        total = 0
        while rows := cursor.fetchmany(1000):
            for fid, score, raw in rows:
                geometry = json.loads(raw)
                geom = shape(geometry)
                if (
                    geom.is_empty
                    or not geom.is_valid
                    or not province_filter.intersects(geom)
                    or geom.geom_type not in ("Polygon", "MultiPolygon")
                ):
                    continue
                hectares = area_hectares(geometry)
                if hectares <= 0:
                    continue
                field_id = f"ftw-2025-{fid}"
                feature = {
                    "type": "Feature",
                    "id": field_id,
                    "properties": {"id": field_id, "score": score},
                    "geometry": geometry,
                    "bbox": list(geom.bounds),
                }
                add_feature(db, feature, hectares, score)
                total += 1
            db.commit()
        metadata["zonesCompleted"].append(zone)
        save_metadata()
        print(f"{region}: zone {zone}: {total:,} accepted boundaries", flush=True)
        con.close()
    metadata["status"] = "complete"
    save_metadata()
    count = db.execute("SELECT count(*) FROM fields").fetchone()[0]
    db.close()
    print(f"{region}: {count:,} fields in {output}", flush=True)
    (output.with_suffix(".coverage.json")).write_text(
        json.dumps({**metadata, "fields": count}, indent=2) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("region", choices=["orillia", "ontario"])
    parser.add_argument("--output", type=Path, default=ROOT / "data/fields.sqlite")
    args = parser.parse_args()
    build(args.region, args.output)
