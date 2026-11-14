"""Run a resumable background field import for Canada, the US and Mexico."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from field_store import add_feature, initialize
from fields import area_hectares

JOB = Path(os.environ.get("FIELD_INGEST_DIR", ROOT / "data/background"))
STATE = JOB / "status.json"
DATABASE = Path(os.environ.get("FIELDS_DB", ROOT / "data/fields.sqlite"))
SOURCE = "https://data.source.coop/ftw/global-data-2e/vector/2025/zone={zone:02d}/utm{zone:02d}.parquet"
REGIONS = [
    ("Eastern Canada", "can", (-80, 44, -52, 62)),
    ("Canadian Prairies", "can", (-120, 48, -89, 60)),
    ("US Great Lakes and Midwest", "usa", (-106, 36, -66, 50)),
    ("Western Canada", "can", (-142, 48, -80, 60)),
    ("Contiguous US", "usa", (-125, 24, -66, 50)),
    ("Mexico", "mex", (-118, 14, -86, 34)),
    ("Northern Canada", "can", (-142, 60, -52, 74)),
    ("Alaska", "usa", (-170, 50, -129, 72)),
    ("Hawaii", "usa", (-161, 18, -154, 24)),
]
STOP = False


def now():
    return datetime.now(timezone.utc).isoformat()


def write_state(state):
    state["updatedAt"] = now()
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n")
    temp.replace(STATE)


def make_plan(boundaries):
    from shapely.geometry import box

    seen = set()
    plan = []
    for region, country, bounds in REGIONS:
        west, south, east, north = bounds
        for zone in range(
            math.floor((west + 180) / 6) + 1, math.floor((east + 180) / 6) + 2
        ):
            for y in range(math.floor(south / 2) * 2, math.ceil(north / 2) * 2, 2):
                key = f"{country}-2025-z{zone:02d}-lat{y}"
                if key in seen:
                    continue
                tile = box(-180 + (zone - 1) * 6, y, -180 + zone * 6, y + 2)
                if (
                    tile.is_empty
                    or tile.area == 0
                    or not boundaries[country].intersects(tile)
                ):
                    continue
                seen.add(key)
                plan.append(
                    {
                        "key": key,
                        "region": region,
                        "country": country,
                        "zone": zone,
                        "bbox": list(tile.bounds),
                    }
                )
    return plan


def stopping(_signum, _frame):
    global STOP
    STOP = True


def persist(db, state):
    public = {
        key: value
        for key, value in state.items()
        if key not in ("completed", "failed", "pid")
    }
    metadata = {
        "region": "North America (Canada, United States, Mexico)",
        "bbox": [-170, 14, -52, 74],
        "targetCountries": ["Canada", "United States", "Mexico"],
        "status": state["status"],
        "source": "Fields of the World, Global 2nd Edition, 2025",
        "sourceUrl": "https://source.coop/ftw/global-data-2e",
        "license": "CC-BY-4.0",
        "description": "Predicted agricultural field units; not farm ownership parcels or an exhaustive farm registry.",
        "fieldCount": state["fields"],
        "ingestion": {
            **public,
            "completedTileKeys": state["completed"],
            "failedTiles": len(state["failed"]),
        },
    }
    db.execute("DELETE FROM metadata WHERE key IN ('zonesCompleted', 'builtAt')")
    db.executemany(
        "INSERT OR REPLACE INTO metadata VALUES (?,?)",
        [(k, json.dumps(v)) for k, v in metadata.items()],
    )
    db.commit()
    write_state(state)


def guard_disk(path, min_free_gb, max_database_gb):
    if shutil.disk_usage(path.parent).free < min_free_gb * 1024**3:
        raise RuntimeError(f"free disk space is below the {min_free_gb} GiB reserve")
    if path.exists() and path.stat().st_size >= max_database_gb * 1024**3:
        raise RuntimeError(f"database reached the {max_database_gb} GiB size budget")


def run(args):
    import duckdb
    from shapely.geometry import shape
    from shapely.ops import unary_union
    from shapely.prepared import prep

    JOB.mkdir(parents=True, exist_ok=True)
    lock = (JOB / "worker.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("A field importer is already running.")
    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    if shutil.which("caffeinate"):
        subprocess.Popen(
            ["caffeinate", "-i", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    boundaries = {}
    for country in ("can", "usa", "mex"):
        data = json.loads((ROOT / f"data/boundaries/{country}.geojson").read_text())
        features = data["features"] if data["type"] == "FeatureCollection" else [data]
        boundaries[country] = unary_union([shape(f["geometry"]) for f in features])
    filters = {country: prep(geom) for country, geom in boundaries.items()}
    plan = make_plan(boundaries)
    (JOB / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    old = json.loads(STATE.read_text()) if STATE.exists() else {}
    state = {
        "status": "building",
        "pid": os.getpid(),
        "startedAt": now(),
        "completed": old.get("completed", []),
        "failed": {},
        "totalTiles": len(plan),
        "currentTile": None,
        "minFreeGiB": args.min_free_gb,
        "maxDatabaseGiB": args.max_database_gb,
        "fields": 0,
        "completedTiles": 0,
    }
    db = sqlite3.connect(DATABASE, timeout=30)
    initialize(db)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    state["fields"] = db.execute("SELECT count(*) FROM fields").fetchone()[0]
    state["completedTiles"] = len(state["completed"])
    persist(db, state)
    con = duckdb.connect()
    con.execute(
        "INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET threads=2; SET memory_limit='1GB'; SET preserve_insertion_order=false"
    )
    con.execute("SET temp_directory=?", [str(JOB / "scratch")])
    try:
        for tile in plan:
            if STOP:
                state["status"] = "stopped"
                break
            if tile["key"] in state["completed"]:
                continue
            guard_disk(DATABASE, args.min_free_gb, args.max_database_gb)
            state["currentTile"] = tile
            persist(db, state)
            print(now(), tile["region"], tile["key"], "starting", flush=True)
            extract = JOB / "current.parquet"
            last_error = None
            for attempt in range(3):
                try:
                    extract.unlink(missing_ok=True)
                    west, south, east, north = tile["bbox"]
                    con.execute(
                        """COPY (SELECT id,score,ST_AsWKB(geometry) AS wkb FROM read_parquet($url)
                        WHERE bbox.xmax >= $west AND bbox.xmin <= $east AND bbox.ymax >= $south AND bbox.ymin <= $north)
                        TO $output (FORMAT PARQUET, ROW_GROUP_SIZE 5000)""",
                        {
                            "url": SOURCE.format(zone=tile["zone"]),
                            "west": west,
                            "east": east,
                            "south": south,
                            "north": north,
                            "output": str(extract),
                        },
                    )
                    last_error = None
                    break
                except Exception as exc:
                    last_error = str(exc)
                    print(
                        now(),
                        "download attempt failed",
                        attempt + 1,
                        last_error,
                        flush=True,
                    )
                    if STOP:
                        break
                    time.sleep(5 * (attempt + 1))
            if last_error:
                state["failed"][tile["key"]] = last_error
                persist(db, state)
                continue
            cursor = con.execute(
                "SELECT id,score,wkb FROM read_parquet(?)", [str(extract)]
            )
            import shapely

            accepted = 0
            while rows := cursor.fetchmany(500):
                if STOP:
                    break
                guard_disk(DATABASE, args.min_free_gb, args.max_database_gb)
                for fid, score, wkb in rows:
                    field_id = f"ftw-2025-{fid}"
                    if db.execute(
                        "SELECT 1 FROM fields WHERE id=?", (field_id,)
                    ).fetchone():
                        continue
                    geom = shapely.from_wkb(bytes(wkb))
                    if (
                        geom.is_empty
                        or not geom.is_valid
                        or geom.geom_type not in ("Polygon", "MultiPolygon")
                        or not filters[tile["country"]].intersects(geom)
                    ):
                        continue
                    geometry = shapely.geometry.mapping(geom)
                    hectares = area_hectares(geometry)
                    if hectares <= 0:
                        continue
                    feature = {
                        "type": "Feature",
                        "id": field_id,
                        "properties": {"id": field_id, "score": score},
                        "geometry": geometry,
                        "bbox": list(geom.bounds),
                    }
                    if add_feature(db, feature, hectares, score):
                        state["fields"] += 1
                        accepted += 1
                persist(db, state)
            if STOP:
                state["status"] = "stopped"
                break
            state["completed"].append(tile["key"])
            state["completedTiles"] = len(state["completed"])
            persist(db, state)
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            extract.unlink(missing_ok=True)
            print(
                now(),
                tile["key"],
                "complete;",
                accepted,
                "new fields;",
                state["fields"],
                "total",
                flush=True,
            )
        else:
            state["status"] = (
                "complete" if not state["failed"] else "completed_with_errors"
            )
    except Exception as exc:
        state["status"] = "paused_disk" if isinstance(exc, RuntimeError) else "failed"
        state["error"] = str(exc)
        print(now(), state["status"], str(exc), flush=True)
    finally:
        state["currentTile"] = None
        persist(db, state)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
        con.close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        print(now(), state["status"], flush=True)


def alive(state):
    pid = state.get("pid")
    if not pid:
        return False
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
    )
    return (
        result.returncode == 0
        and str(Path(__file__).resolve()) in result.stdout
        and "worker" in result.stdout
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "worker", "status", "stop"])
    parser.add_argument("--min-free-gb", type=float, default=12)
    parser.add_argument("--max-database-gb", type=float, default=30)
    args = parser.parse_args()
    JOB.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if args.command == "worker":
        try:
            run(args)
        except Exception as exc:
            write_state(
                {**state, "status": "failed", "error": str(exc), "pid": os.getpid()}
            )
            raise
    elif args.command == "status":
        print(json.dumps({**state, "running": alive(state)}, indent=2))
    elif args.command == "stop":
        if alive(state):
            os.kill(state["pid"], signal.SIGTERM)
            print("Stop requested; committed fields and completed tiles are retained.")
        else:
            print("No importer is running.")
    elif alive(state):
        print("Importer already running:", state["pid"])
    else:
        with (JOB / "worker.log").open("a") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "worker",
                    "--min-free-gb",
                    str(args.min_free_gb),
                    "--max-database-gb",
                    str(args.max_database_gb),
                ],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        print("Background importer started:", process.pid, "log:", JOB / "worker.log")


if __name__ == "__main__":
    main()
