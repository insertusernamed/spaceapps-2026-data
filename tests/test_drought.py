"""GRACE drought indicators, tested offline against small synthetic NetCDF files."""
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import h5py
import numpy as np
import pytest
from fastapi.testclient import TestClient

import server
from app import EarthdataError
from environmental import drought
from environmental.errors import ProviderError, ValidationError

SQUARE = {"type": "Polygon", "coordinates": [[[-79.2, 44.3], [-79.19, 44.3], [-79.19, 44.31], [-79.2, 44.31], [-79.2, 44.3]]]}
TODAY = date(2026, 11, 14)
LATEST = date(2026, 8, 31)


def _write(path, groundwater, root, surface):
    lats = np.arange(-59.875, 90, 0.25, dtype="float32")
    lons = np.arange(-179.875, 180, 0.25, dtype="float32")
    with h5py.File(path, "w") as f:
        f["lat"], f["lon"] = lats, lons
        for name, value in (("gws_inst", groundwater), ("rtzsm_inst", root), ("sfsm_inst", surface)):
            grid = np.full((1, 600, 1440), value, dtype="float32")
            grid[0, :, lons > 0] = -999.0  # no value in the eastern hemisphere
            f[name] = grid


@pytest.fixture
def weeks(tmp_path, monkeypatch):
    """Weekly files for the 20 weeks up to the latest, and the same week in each of the 5 years before."""
    monkeypatch.setenv("GRACE_DIR", str(tmp_path))
    recent = [LATEST - timedelta(days=7 * n) for n in range(19, -1, -1)]
    earlier = [date(year, 8, 31) + timedelta(days=offset) for year, offset in
               ((2021, -1), (2022, -2), (2023, -3), (2024, 2), (2025, 1))]
    published = sorted(set(recent + earlier))
    for n, week in enumerate(published):
        _write(tmp_path / drought._name(week), 5.0 + n, 40.0, 95.0)
    index = [(week, f"granule-{week}") for week in published]
    with patch.object(drought, "_index", return_value=index), \
            patch.object(drought, "_login", side_effect=AssertionError("nothing should be downloaded")):
        yield published


def test_conditions_follow_drought_monitor_classes():
    assert [drought.condition(p) for p in (1, 2, 4, 9, 20, 25, 50, 70, 85, 100, None)] == [
        "exceptionally dry", "exceptionally dry", "extremely dry", "severely dry", "moderately dry", "abnormally dry",
        "normal", "normal", "wetter than normal", "much wetter than normal", None]


def test_latest_recent_weeks_and_same_week_in_earlier_years(weeks):
    out = drought.indicators(SQUARE, 4, 5, TODAY)
    assert out["cell"] == {"lat": 44.375, "lon": -79.125, "sizeDeg": 0.25}
    assert out["latest"]["weekStart"] == "2026-08-31" and out["latest"]["ageDays"] == 75
    assert out["latest"]["groundwater"] == {"percentile": 29.0, "condition": "abnormally dry"}
    assert out["latest"]["rootZone"] == {"percentile": 40.0, "condition": "normal"}
    assert out["latest"]["surface"]["condition"] == "much wetter than normal"
    assert [w["weekStart"] for w in out["weekly"]] == ["2026-08-10", "2026-08-17", "2026-08-24", "2026-08-31"]
    assert [w["weekStart"] for w in out["sameWeekByYear"]] == ["2021-08-30", "2022-08-29", "2023-08-28", "2024-09-02", "2025-09-01"]
    assert out["sameWeekByYear"][0]["groundwaterPercentile"] == 5.0
    assert out["conditions"][0] == {"upToPercentile": 2, "condition": "exceptionally dry"}


def test_years_zero_skips_the_comparison_and_missing_years_are_left_out(weeks):
    assert drought.indicators(SQUARE, 2, 0, TODAY)["sameWeekByYear"] == []
    assert len(drought.indicators(SQUARE, 2, 10, TODAY)["sameWeekByYear"]) == 5  # nothing published before 2021 here


def test_cells_without_a_value_are_null(weeks):
    east = {"type": "Polygon", "coordinates": [[[10, 50], [10.01, 50], [10.01, 50.01], [10, 50.01], [10, 50]]]}
    latest = drought.indicators(east, 1, 0, TODAY)["latest"]
    assert latest["groundwater"] == {"percentile": None, "condition": None}
    far_south = {"type": "Polygon", "coordinates": [[[-70, -75], [-69.99, -75], [-69.99, -74.99], [-70, -74.99], [-70, -75]]]}
    assert drought.indicators(far_south, 1, 0, TODAY)["latest"]["rootZone"]["percentile"] is None


def test_validates_weeks_and_years(weeks):
    for bad_weeks, bad_years in ((0, 5), (53, 5), (4, -1), (4, 24)):
        with pytest.raises(ValidationError):
            drought.indicators(SQUARE, bad_weeks, bad_years, TODAY)


def test_downloads_only_missing_weeks(tmp_path, monkeypatch):
    monkeypatch.setenv("GRACE_DIR", str(tmp_path))
    have, need = date(2026, 8, 24), date(2026, 8, 31)
    _write(tmp_path / drought._name(have), 10, 10, 10)
    earthaccess = MagicMock()
    earthaccess.download.side_effect = lambda granules, folder: _write(tmp_path / drought._name(need), 20, 20, 20)
    with patch.object(drought, "_login", return_value=earthaccess):
        files = drought._files([(have, "g-have"), (need, "g-need")])
    assert set(files) == {have, need} and earthaccess.download.call_args.args[0] == ["g-need"]
    earthaccess.download.side_effect = OSError("network")
    with patch.object(drought, "_login", return_value=earthaccess), pytest.raises(ProviderError):
        drought._files([(date(2026, 9, 7), "g-new")])


def test_routes(weeks):
    client = TestClient(server.app)
    posted = client.post("/drought-indicators", json={"polygon": SQUARE, "weeks": 2, "years": 1})
    assert posted.status_code == 200 and len(posted.json()["weekly"]) == 2
    assert client.post("/drought-indicators", json={"polygon": SQUARE, "weeks": 60}).status_code == 422
    assert client.get("/fields/nope/drought-indicators").status_code == 404
    with patch.object(drought, "indicators", side_effect=EarthdataError("Missing EARTHDATA_USERNAME")):
        missing = client.post("/drought-indicators", json={"polygon": SQUARE})
        assert missing.status_code == 503 and "EARTHDATA_USERNAME" in missing.json()["detail"]
    with patch.object(drought, "indicators", side_effect=ProviderError("NASA GRACE drought indicator catalogue is unavailable")):
        assert client.post("/drought-indicators", json={"polygon": SQUARE}).status_code == 502
