"""Agro-weather from NASA POWER, tested offline."""
from datetime import date
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import server
from environmental import agro_weather
from environmental.errors import ProviderError, ValidationError

SQUARE = {"type": "Polygon", "coordinates": [[[-79.5, 44.5], [-79.49, 44.5], [-79.49, 44.51], [-79.5, 44.51], [-79.5, 44.5]]]}
TODAY = date(2026, 11, 10)


def _frame(start="2016-01-01", end="2026-11-07", dry_recent=False):
    days = pd.date_range(start, end, freq="D")
    doy = days.dayofyear.to_numpy()
    warm = 8 + 14 * np.sin((doy - 100) / 365 * 2 * np.pi)
    root = 0.6 + 0.02 * (days.year.to_numpy() - 2016) / 10 + 0 * doy
    frame = pd.DataFrame({"tmax_c": warm + 6, "tmin_c": warm - 6, "rh_pct": 70.0, "wind_ms": 2.0,
                          "solar_mj": 12 + 8 * np.sin((doy - 80) / 365 * 2 * np.pi), "root_zone": root,
                          "surface": root - 0.1}, index=days)
    if dry_recent:
        frame.loc[frame.index[-30:], "root_zone"] = 0.2
    return frame


def _summary(frame):
    with patch.object(agro_weather, "fetch", return_value=(frame, 250.0)):
        return agro_weather.summary(SQUARE, 10, TODAY)


def test_penman_monteith_matches_fao56_example_18():
    # FAO-56 example 18: Brussels, 6 July, 50.8 N, 100 m: ET0 about 3.9 mm/day.
    frame = pd.DataFrame({"tmax_c": [21.5], "tmin_c": [12.3], "rh_pct": [73.5], "wind_ms": [2.078],
                          "solar_mj": [22.07]}, index=pd.to_datetime(["2020-07-06"]))
    assert agro_weather.et0_penman_monteith(frame, 50.8, 100).iloc[0] == pytest.approx(3.9, abs=0.25)


def test_summary_shapes_and_seasons():
    out = _summary(_frame())
    assert out["season"]["months"] == [5, 6, 7, 8, 9]
    assert [r["year"] for r in out["seasonByYear"]] == list(range(2016, 2027))
    assert out["seasonByYear"][0]["rootZone"] == pytest.approx(0.6, abs=0.001)
    assert 300 < out["seasonByYear"][0]["et0Mm"] < 800
    assert len(out["soilWetness"]["monthly"]) == 12 and len(out["et0"]["monthlyMmPerDay"]) == 12
    assert out["soilWetness"]["seasonPercentiles"]["rootZone"]["years"] == 11
    assert out["location"]["elevationM"] == 250.0
    assert out["period"]["end"] == "2026-11-07"


def test_recent_wetness_is_ranked_against_the_same_days_in_earlier_years():
    wet = _summary(_frame())["soilWetness"]["latest"]
    assert wet["percentile"] == 100 and wet["status"] == "wet" and wet["comparedYears"] == 10
    dry = _summary(_frame(dry_recent=True))["soilWetness"]["latest"]
    assert dry["percentile"] == 0 and dry["status"] == "dry" and dry["rootZone30DayMean"] == pytest.approx(0.2)


def test_incomplete_season_is_null():
    frame = _frame()
    frame.loc["2020-06":"2020-08", ["root_zone", "tmax_c"]] = np.nan
    row = next(r for r in _summary(frame)["seasonByYear"] if r["year"] == 2020)
    assert row["rootZone"] is None and row["et0Mm"] is None and row["windMs"] is not None


def test_years_are_validated():
    with pytest.raises(ValidationError):
        agro_weather.summary(SQUARE, 2, TODAY)


def test_fetch_parses_power_and_maps_failures():
    agro_weather._cache.clear()
    body = {"geometry": {"coordinates": [-79.5, 44.5, 251.3]},
            "properties": {"parameter": {code: {"20240101": 1.0, "20240102": -999.0} for code in agro_weather.VARIABLES}}}
    response = MagicMock(json=lambda: body)
    with patch.object(agro_weather._session, "get", return_value=response) as get:
        frame, elevation = agro_weather.fetch(44.5, -79.5, date(2024, 1, 1), date(2024, 1, 2))
    assert elevation == 251.3 and np.isnan(frame["root_zone"].iloc[1]) and frame["root_zone"].iloc[0] == 1.0
    assert "GWETROOT" in get.call_args.kwargs["params"]["parameters"]
    agro_weather._cache.clear()
    with patch.object(agro_weather._session, "get", return_value=MagicMock(json=lambda: {})), pytest.raises(ProviderError):
        agro_weather.fetch(44.5, -79.5, date(2024, 1, 1), date(2024, 1, 2))


def test_routes():
    client = TestClient(server.app)
    with patch.object(agro_weather, "fetch", return_value=(_frame(end=date.today().isoformat()), 250.0)):
        posted = client.post("/agro-weather", json={"polygon": SQUARE, "years": 10})
        assert posted.status_code == 200 and "soilWetness" in posted.json()
        assert client.post("/agro-weather", json={"polygon": SQUARE, "years": 2}).status_code == 422
        assert client.get("/fields/nope/agro-weather").status_code == 404
    with patch.object(agro_weather, "fetch", side_effect=ProviderError("NASA POWER agro-weather is unavailable or invalid")):
        assert client.post("/agro-weather", json={"polygon": SQUARE}).status_code == 502
