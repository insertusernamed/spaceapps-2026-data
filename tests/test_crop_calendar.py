"""Crop calendar lookups, tested offline against small synthetic NetCDF files."""
from unittest.mock import MagicMock, patch

import h5py
import numpy as np
import pytest
from fastapi.testclient import TestClient

import server
from environmental import crop_calendar
from environmental.errors import ProviderError, ValidationError

SQUARE = {"type": "Polygon", "coordinates": [[[-79.5, 44.2], [-79.49, 44.2], [-79.49, 44.21], [-79.5, 44.21], [-79.5, 44.2]]]}
FILL = np.float32(1e20)


def _write(path, planting, maturity, length, source=1.0):
    """A global half-degree file with one value everywhere and no value in the southern hemisphere."""
    lats = np.arange(89.75, -90, -0.5)
    lons = np.arange(-179.75, 180, 0.5)
    with h5py.File(path, "w") as f:
        f["lat"], f["lon"] = lats, lons
        for name, value in (("planting_day", planting), ("maturity_day", maturity),
                            ("growing_season_length", length), ("data_source_used", source)):
            grid = np.full((360, 720), value, dtype="float32")
            grid[lats < 0, :] = FILL
            f[name] = grid


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setenv("CROP_CALENDAR_DIR", str(tmp_path))
    crop_calendar._entry.cache_clear()
    values = {"mai": (121, 283, 162, 1.0), "wwh": (284, 208, 289, 2.0)}
    for code in crop_calendar.CROPS:
        for system in ("rf", "ir"):
            planting, maturity, length, source = values.get(code, (130, 250, 120, 3.0))
            _write(tmp_path / crop_calendar.FILE_NAME.format(code=code, system=system),
                   planting + (5 if system == "ir" else 0), maturity, length, source)
    yield tmp_path
    crop_calendar._entry.cache_clear()


def test_returns_every_crop_with_dates(files):
    out = crop_calendar.calendar(SQUARE)
    assert len(out["crops"]) == 20 and out["system"] == "rainfed"
    assert out["cell"] == {"lat": 44.25, "lon": -79.25, "sizeDeg": 0.5}
    corn = next(c for c in out["crops"] if c["crop"] == "corn")
    assert corn == {"crop": "corn", "label": "Corn", "plantingDay": 121, "plantingDate": "05-01", "maturityDay": 283,
                    "maturityDate": "10-10", "growingSeasonDays": 162, "offSeasonDays": 203, "crossesNewYear": False,
                    "dataSource": "MIRCA2000"}


def test_winter_wheat_crosses_the_new_year_and_crops_can_be_chosen(files):
    out = crop_calendar.calendar(SQUARE, "irrigated", ["winter_wheat", "corn", "corn"])
    assert [c["crop"] for c in out["crops"]] == ["winter_wheat", "corn"]
    wheat = out["crops"][0]
    assert wheat["crossesNewYear"] is True and wheat["plantingDate"] == "10-16" and wheat["dataSource"] == "SAGE"


def test_cells_without_a_value_are_null(files):
    south = {"type": "Polygon", "coordinates": [[[10, -20], [10.01, -20], [10.01, -19.99], [10, -19.99], [10, -20]]]}
    corn = crop_calendar.calendar(south, crops=["corn"])["crops"][0]
    assert corn["plantingDay"] is None and corn["plantingDate"] is None and corn["crossesNewYear"] is None
    assert corn["offSeasonDays"] is None and corn["dataSource"] is None


def test_validates_system_and_crops(files):
    with pytest.raises(ValidationError):
        crop_calendar.calendar(SQUARE, "flooded")
    with pytest.raises(ValidationError):
        crop_calendar.calendar(SQUARE, crops=["corn", "oats"])


def test_downloads_missing_files_once_and_rejects_bad_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("CROP_CALENDAR_DIR", str(tmp_path / "cache"))
    source = tmp_path / "source.nc4"
    _write(source, 121, 283, 162)
    with patch.object(crop_calendar._session, "get", return_value=MagicMock(content=source.read_bytes())) as get:
        first = crop_calendar._file("mai", "rainfed")
        assert crop_calendar._file("mai", "rainfed") == first and get.call_count == 1
    assert "mai_rf_ggcmi_crop_calendar_phase3_v1.01.nc4" in get.call_args.args[0]
    assert crop_calendar._read(first, 44.3, -79.2)["planting_day"] == 121
    with patch.object(crop_calendar._session, "get", return_value=MagicMock(content=b"<html>not found</html>")), \
            pytest.raises(ProviderError):
        crop_calendar._file("soy", "rainfed")
    assert not (tmp_path / "cache" / "soy_rf_ggcmi_crop_calendar_phase3_v1.01.nc4").exists()


def test_routes(files):
    client = TestClient(server.app)
    posted = client.post("/crop-calendar", json={"polygon": SQUARE, "crops": ["corn", "soybeans"]})
    assert posted.status_code == 200 and [c["crop"] for c in posted.json()["crops"]] == ["corn", "soybeans"]
    assert client.post("/crop-calendar", json={"polygon": SQUARE, "system": "flooded"}).status_code == 422
    assert client.post("/crop-calendar", json={"polygon": SQUARE, "crops": ["oats"]}).status_code == 422
    assert client.get("/fields/nope/crop-calendar").status_code == 404
    with patch.object(crop_calendar, "calendar", side_effect=ProviderError("GGCMI crop calendar could not be downloaded")):
        assert client.post("/crop-calendar", json={"polygon": SQUARE}).status_code == 502
