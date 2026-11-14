import importlib.util
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import field_store
from pipeline import north_america as worker

HAS_BUILD_DEPS = (
    importlib.util.find_spec("duckdb") is not None
    and importlib.util.find_spec("shapely") is not None
)


@unittest.skipUnless(
    HAS_BUILD_DEPS, "install pipeline/requirements.txt for acquisition checks"
)
class TestBackgroundFields(unittest.TestCase):
    def test_plan_uses_whole_tiles_across_priority_region_edges(self):
        from shapely.geometry import box

        boundaries = {
            "can": box(-120, 48, -80, 60),
            "usa": box(-100, 36, -90, 40),
            "mex": box(-100, 20, -90, 24),
        }
        plan = worker.make_plan(boundaries)
        self.assertEqual(len(plan), len({t["key"] for t in plan}))
        for tile in plan:
            self.assertAlmostEqual(tile["bbox"][2] - tile["bbox"][0], 6)
            self.assertAlmostEqual(tile["bbox"][3] - tile["bbox"][1], 2)
        self.assertTrue(
            any(
                t["country"] == "can" and t["bbox"][0] == -90 and t["bbox"][2] == -84
                for t in plan
            )
        )

    def test_disk_guard_pauses_before_running_out_of_space(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "fields.sqlite"
            with patch(
                "pipeline.north_america.shutil.disk_usage",
                return_value=SimpleNamespace(free=1024),
            ):
                with self.assertRaisesRegex(RuntimeError, "reserve"):
                    worker.guard_disk(path, 12, 30)
            path.write_bytes(b"data")
            with self.assertRaisesRegex(RuntimeError, "size budget"):
                worker.guard_disk(path, 0, 0)

    def test_worker_resume_and_compressed_api_reads(self):
        import duckdb
        from shapely.geometry import box, mapping

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data/boundaries").mkdir(parents=True)
            shapes = {
                "can": box(-79.9, 44.1, -78.1, 45.9),
                "usa": box(-89.9, 40.1, -88.1, 41.9),
                "mex": box(-99.9, 20.1, -98.1, 21.9),
            }
            for country, geom in shapes.items():
                (root / f"data/boundaries/{country}.geojson").write_text(
                    json.dumps(
                        {"type": "Feature", "properties": {}, "geometry": mapping(geom)}
                    )
                )
            source = root / "source.parquet"
            con = duckdb.connect()
            con.execute("INSTALL spatial; LOAD spatial")
            con.execute(
                "CREATE TABLE source(id VARCHAR,score INTEGER,geometry GEOMETRY)"
            )
            for country, geom in shapes.items():
                con.execute(
                    "INSERT INTO source VALUES (?,80,ST_GeomFromText(?))",
                    [country, geom.wkt],
                )
            con.execute(
                "COPY (SELECT *, struct_pack(xmin:=ST_XMin(geometry),xmax:=ST_XMax(geometry),ymin:=ST_YMin(geometry),ymax:=ST_YMax(geometry)) AS bbox FROM source) TO ? (FORMAT PARQUET)",
                [str(source)],
            )
            con.close()
            job = root / "data/background"
            dbpath = root / "data/fields.sqlite"
            with (
                patch.multiple(
                    worker,
                    ROOT=root,
                    JOB=job,
                    STATE=job / "status.json",
                    DATABASE=dbpath,
                    SOURCE=str(source),
                    STOP=False,
                ),
                patch("pipeline.north_america.signal.signal"),
                patch("pipeline.north_america.shutil.which", return_value=None),
            ):
                args = SimpleNamespace(min_free_gb=0, max_database_gb=30)
                worker.run(args)
                first = json.loads((job / "status.json").read_text())
                self.assertEqual(first["status"], "complete")
                self.assertEqual(first["fields"], 3)
                worker.run(args)
                second = json.loads((job / "status.json").read_text())
                self.assertEqual(first["completed"], second["completed"])
                self.assertEqual(second["fields"], 3)
                page = field_store.list_features(dbpath, (-180, 0, 0, 90), limit=10)
                self.assertEqual(len(page["features"]), 3)
                with sqlite3.connect(dbpath) as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT typeof(feature) FROM fields LIMIT 1"
                        ).fetchone()[0],
                        "blob",
                    )
                    feature = page["features"][0]
                    db.execute(
                        "UPDATE fields SET feature=? WHERE id=?",
                        (json.dumps(feature), feature["id"]),
                    )
                self.assertEqual(
                    len(
                        field_store.list_features(dbpath, (-180, 0, 0, 90), limit=10)[
                            "features"
                        ]
                    ),
                    3,
                )
