import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import smap

META = {"producer_granule_id": "SMAP_L4_SM_gph_20250101T223000_Vv8010_001.h5",
        "time_start": "2025-01-01T21:00:00Z", "time_end": "2025-01-02T00:00:00Z"}


def remote_stream(data=b"remote HDF5", path="granule.h5"):
    stream = io.BytesIO(data)
    stream.path = path
    return stream


class TestSMAP(unittest.TestCase):
    def test_discovery_selects_one_latest_interval(self):
        response = Mock()
        response.json.return_value = {"feed": {"entry": [META]}}
        with patch("requests.get", return_value=response) as get:
            self.assertEqual(smap.discover(smap.ORILLIA_BBOX, "2025-01-01"), META)
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["version"], "008")
        self.assertEqual(params["sort_key"], "-start_date")
        self.assertEqual(params["page_size"], 1)
        self.assertEqual(params["temporal"], "2025-01-01T00:00:00Z,2025-01-01T23:59:59Z")

    def test_invalid_request_never_contacts_provider(self):
        with patch("requests.get") as get:
            for bounds, day in [((0, 0, 0, 1), None), ((-180, 0, 180, 1), None),
                                (smap.ORILLIA_BBOX, "2014-01-01"),
                                (smap.ORILLIA_BBOX, "2025-02-30")]:
                with self.subTest(bounds=bounds, day=day), self.assertRaises(ValueError):
                    smap.discover(bounds, day)
            get.assert_not_called()

    def test_no_granules_is_explicit(self):
        response = Mock()
        response.json.return_value = {"feed": {"entry": []}}
        with patch("requests.get", return_value=response), self.assertRaisesRegex(ValueError, "No SPL4SMGP"):
            smap.discover(smap.ORILLIA_BBOX)

    def test_missing_values_do_not_become_zero(self):
        for raw in [-9999, float("nan"), float("inf"), -0.1, 1.1, 0, 0.25]:
            value = smap._value(raw)
            self.assertEqual(value, raw if raw in (0, 0.25) else None)

    def test_cache_requires_product_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "layer.geojson"
            path.write_text('{"type":"FeatureCollection","features":[]}')
            with self.assertRaises(ValueError):
                smap.read_layer(path)
            expected = {"type": "FeatureCollection", "features": [], "metadata": {"product": "SPL4SMGP"}}
            path.write_text(json.dumps(expected))
            self.assertEqual(smap.read_layer(path), expected)

    def test_failed_conversion_preserves_previous_layer(self):
        earthaccess = SimpleNamespace(login=Mock(return_value=SimpleNamespace(authenticated=True)),
                                     search_data=Mock(return_value=[{}]),
                                     open=Mock(return_value=[remote_stream()]),
                                     download=Mock(side_effect=AssertionError("Full file download forbidden")))
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "layer.geojson"
            output.write_text("previous layer")
            with patch.dict("sys.modules", earthaccess=earthaccess, h5py=SimpleNamespace(), pyproj=SimpleNamespace()), \
                 patch("smap.discover", return_value=META), \
                 patch("app.load_credentials", return_value=("test", "test")), \
                 patch("smap.convert", side_effect=ValueError("bad source")):
                with self.assertRaisesRegex(ValueError, "bad source"):
                    smap.acquire(output=output)
            self.assertEqual(output.read_text(), "previous layer")
            self.assertEqual(list(Path(folder).iterdir()), [output])
            self.assertTrue(earthaccess.open.return_value[0].closed)
            earthaccess.download.assert_not_called()

    def test_streamed_acquisition_saves_only_geojson(self):
        stream = remote_stream()
        sidecars = [remote_stream(b"QA", "granule.qa"), remote_stream(b"XML", "granule.h5.iso.xml")]
        earthaccess = SimpleNamespace(login=Mock(return_value=SimpleNamespace(authenticated=True)),
                                     search_data=Mock(return_value=[{"granule": "selected"}]),
                                     open=Mock(return_value=[*sidecars, stream]),
                                     download=Mock(side_effect=AssertionError("Full file download forbidden")))
        layer = {"type": "FeatureCollection", "features": [], "metadata": {"product": "SPL4SMGP", "schema_version": smap.SCHEMA_VERSION}}
        def convert(file, bounds, metadata, constants_file=None):
            self.assertIs(file, stream)
            self.assertFalse(stream.closed)
            self.assertEqual(metadata, META)
            return layer
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "layer.geojson"
            with patch.dict("sys.modules", earthaccess=earthaccess, h5py=SimpleNamespace(), pyproj=SimpleNamespace()), \
                 patch("smap.discover", return_value=META), \
                 patch("app.load_credentials", return_value=("test", "test")), \
                 patch("smap.convert", side_effect=convert):
                self.assertEqual(smap.acquire(output=output), layer)
            self.assertTrue(stream.closed)
            self.assertTrue(all(s.closed for s in sidecars))
            self.assertEqual(earthaccess.open.call_count, 2)
            earthaccess.search_data.assert_any_call(short_name="SPL4SMLM", version="008",
                granule_name="SMAP_L4_SM_lmc_*_Vv8010_*.h5", count=1)
            earthaccess.download.assert_not_called()
            self.assertEqual(json.loads(output.read_text()), layer)
            self.assertEqual(list(Path(folder).iterdir()), [output])

    def test_unexpected_stream_count_closes_all_handles(self):
        streams = [remote_stream(b"first"), remote_stream(b"second")]
        earthaccess = SimpleNamespace(login=Mock(return_value=SimpleNamespace(authenticated=True)),
                                     search_data=Mock(return_value=[{}]), open=Mock(return_value=streams))
        with patch.dict("sys.modules", earthaccess=earthaccess, h5py=SimpleNamespace(), pyproj=SimpleNamespace()), \
             patch("smap.discover", return_value=META), \
             patch("app.load_credentials", return_value=("test", "test")):
            with self.assertRaisesRegex(RuntimeError, "exactly one remote"):
                smap.acquire()
        self.assertTrue(all(stream.closed for stream in streams))

    def test_missing_compatible_constants_preserves_cache(self):
        earthaccess = SimpleNamespace(login=Mock(return_value=SimpleNamespace(authenticated=True)),
                                     search_data=Mock(side_effect=[[{}], []]), open=Mock())
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "layer.geojson"
            output.write_text("previous layer")
            with patch.dict("sys.modules", earthaccess=earthaccess, h5py=SimpleNamespace(), pyproj=SimpleNamespace()), \
                 patch("smap.discover", return_value=META), \
                 patch("app.load_credentials", return_value=("test", "test")):
                with self.assertRaisesRegex(RuntimeError, "No compatible SPL4SMLM"):
                    smap.acquire(output=output)
            earthaccess.open.assert_not_called()
            self.assertEqual(output.read_text(), "previous layer")

    def test_api_serves_cache_without_authentication(self):
        import server
        layer = {"type": "FeatureCollection", "features": [], "metadata": {"product": "SPL4SMGP", "schema_version": smap.SCHEMA_VERSION}}
        with patch("smap.read_layer", return_value=layer), patch("server.load_credentials") as auth:
            self.assertEqual(server.soil_moisture(), layer)
            auth.assert_not_called()
        with patch("smap.read_layer", side_effect=FileNotFoundError), \
             patch("smap.acquire", side_effect=RuntimeError("SMAP dependencies missing")):
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as raised:
                server.soil_moisture()
            self.assertEqual(raised.exception.status_code, 503)

    def test_api_acquires_missing_cache_and_reuses_it(self):
        import os
        import server
        layer = {"type": "FeatureCollection", "features": [], "metadata": {"product": "SPL4SMGP", "schema_version": smap.SCHEMA_VERSION}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "custom.geojson"
            def acquire(output):
                self.assertEqual(output, path)
                output.write_text(json.dumps(layer))
                return layer
            with patch.dict(os.environ, SMAP_FILE=str(path)), patch("smap.acquire", side_effect=acquire) as fetch:
                self.assertEqual(server.soil_moisture(), layer)
                self.assertEqual(server.soil_moisture(), layer)
                fetch.assert_called_once_with(output=path)

    def test_old_cache_is_upgraded_with_original_region_and_day(self):
        import os
        old = {"type": "FeatureCollection", "features": [], "metadata": {
            "product": "SPL4SMGP", "requested_bbox": [-80, 44, -79, 45],
            "time_start": "2025-01-01T21:00:00Z"}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.geojson"
            path.write_text(json.dumps(old))
            with patch.dict(os.environ, SMAP_FILE=str(path)), \
                 patch("smap.acquire", side_effect=RuntimeError("upstream unavailable")) as fetch:
                with self.assertRaises(RuntimeError):
                    smap.get_layer()
                fetch.assert_called_once_with(bounds=[-80, 44, -79, 45], day="2025-01-01", output=path)
            self.assertEqual(json.loads(path.read_text()), old)

    def test_api_reports_credentials_and_upstream_failures(self):
        import requests
        import server
        from fastapi import HTTPException
        from app import EarthdataError
        for error, code, detail in [
            (EarthdataError("missing credentials"), 503, "Configure EARTHDATA_USERNAME"),
            (requests.ConnectionError("private upstream URL"), 502, "NASA SMAP request failed"),
        ]:
            with self.subTest(code=code), patch("smap.get_layer", side_effect=error):
                with self.assertRaises(HTTPException) as raised:
                    server.soil_moisture()
                self.assertEqual(raised.exception.status_code, code)
                self.assertIn(detail, raised.exception.detail)
                self.assertNotIn("private upstream URL", raised.exception.detail)


@unittest.skipUnless(all(importlib.util.find_spec(m) for m in ("h5py", "pyproj", "numpy")),
                     "Install pipeline/requirements-smap.txt for HDF5 conversion tests")
class TestConversion(unittest.TestCase):
    def test_native_cells_and_nulls(self):
        import h5py
        import numpy as np
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.h5"
            with h5py.File(path, "w") as source:
                source["cell_lat"] = np.array([[44.7]*3, [44.6]*3, [44.5]*3])
                source["cell_lon"] = np.array([[-79.6, -79.5, -79.4]]*3)
                surface = source.create_dataset("Geophysical_Data/sm_surface", data=np.full((3, 3), 0.25))
                surface.attrs["_FillValue"] = -9999.0
                surface[1, 1] = -9999
                source["Geophysical_Data/sm_rootzone"] = np.full((3, 3), 0.4)
                source["Geophysical_Data/sm_rootzone_pctl"] = np.full((3, 3), 73.0)
                source["Geophysical_Data/sm_surface_wetness"] = np.full((3, 3), 0.6)
                source["Geophysical_Data/soil_temp_layer1"] = np.full((3, 3), 280.15)
                source["Geophysical_Data/land_evapotranspiration_flux"] = np.full((3, 3), -0.000000123)
                source["Geophysical_Data/soil_water_infiltration_flux"] = np.full((3, 3), 0.000000456)
                source["Geophysical_Data/soil_temp_layer1"][0, 0] = -9999
                source["Geophysical_Data/sm_rootzone_pctl"][0, 0] = 101
            constants_path = Path(folder) / "constants.h5"
            with h5py.File(constants_path, "w") as constants:
                constants["cell_lat"] = np.array([[44.7]*3, [44.6]*3, [44.5]*3])
                constants["cell_lon"] = np.array([[-79.6, -79.5, -79.4]]*3)
                for name, value in {"clsm_poros": 0.45, "clsm_wp": 0.12, "mwrtm_clay": 0.25, "mwrtm_sand": 0.55}.items():
                    constants[f"Land-Model-Constants_Data/{name}"] = np.full((3, 3), value)
            # A seekable file-like object exercises h5py's remote-stream driver.
            with io.BytesIO(path.read_bytes()) as stream, io.BytesIO(constants_path.read_bytes()) as static:
                layer = smap.convert(stream, smap.ORILLIA_BBOX, META, constants_file=static)
                self.assertFalse(stream.closed)
                small = smap.convert(stream, (-79.51, 44.59, -79.49, 44.61), META)
            self.assertEqual([f["id"] for f in small["features"]], ["smap-1-1"])
            self.assertEqual(len(layer["features"]), 9)
            middle = next(f for f in layer["features"] if f["id"] == "smap-1-1")
            self.assertIsNone(middle["properties"]["sm_surface"])
            self.assertEqual(middle["properties"]["sm_rootzone"], 0.4)
            self.assertEqual(middle["properties"]["soil_temp_layer1"], 280.15)
            self.assertEqual(middle["properties"]["sm_rootzone_pctl"], 73)
            self.assertEqual(middle["properties"]["sm_surface_wetness"], 0.6)
            self.assertEqual(middle["properties"]["clsm_poros"], 0.45)
            self.assertEqual(middle["properties"]["mwrtm_clay"], 0.25)
            self.assertEqual(middle["properties"]["land_evapotranspiration_flux"], -0.000000123)
            self.assertEqual(middle["properties"]["soil_water_infiltration_flux"], 0.000000456)
            self.assertIsNone(layer["features"][0]["properties"]["soil_temp_layer1"])
            self.assertIsNone(layer["features"][0]["properties"]["sm_rootzone_pctl"])
            self.assertIsNone(middle["properties"]["soil_temp_layer6"])
            self.assertIn("soil_temp_layer6", layer["metadata"]["missing_variables"])
            self.assertEqual(layer["metadata"]["variables"]["soil_temp_layer1"]["units"], "K")
            self.assertTrue(layer["metadata"]["variables"]["clsm_poros"]["static"])
            ring = middle["geometry"]["coordinates"][0]
            self.assertEqual(ring[0], ring[-1])
            self.assertLess(ring[0][0], -79.5)
            self.assertGreater(ring[1][0], -79.5)
            self.assertLess(ring[0][1], 44.6)
            self.assertGreater(ring[2][1], 44.6)
            json.dumps(layer, allow_nan=False)
            with h5py.File(constants_path, "a") as constants:
                constants["cell_lat"][:] = np.array([[43.7]*3, [43.6]*3, [43.5]*3])
            with self.assertRaisesRegex(ValueError, "do not match"):
                smap.convert(path, smap.ORILLIA_BBOX, META, constants_file=constants_path)
