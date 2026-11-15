"""MODIS vegetation history, tested offline."""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin

import server
from environmental import vegetation_history as vh
from environmental.errors import ProviderError, ValidationError

SQUARE = {"type": "Polygon", "coordinates": [[[-79.5, 44.5], [-79.49, 44.5], [-79.49, 44.51], [-79.5, 44.51], [-79.5, 44.5]]]}
TODAY = date(2025, 12, 31)


def _item(day, prefix="MOD13Q1"):
    return {"id": f"{prefix}.A{day}", "properties": {"start_datetime": f"{day}T00:00:00Z"},
            "assets": {name: {"href": f"https://acct.blob.core.windows.net/box/{prefix}/{day}_{name}.tif"}
                       for name in (vh.NDVI_ASSET, vh.RELIABILITY_ASSET, "Npp_500m", "ET_500m", "PET_500m")}}


def _season(year):
    """Observations for one year: snow in winter, bare in spring and late autumn, a crop in summer."""
    out = []
    for n in range(23):
        day = date(year, 1, 1) + timedelta(days=16 * n)
        if n < 5 or n >= 21:
            out.append({"date": day.isoformat(), "ndvi": None, "snow": True, "usableShare": 0.0})
        else:
            ndvi = 0.15 if n < 9 or n >= 19 else 0.15 + 0.65 * np.sin((n - 9) / 10 * np.pi)
            out.append({"date": day.isoformat(), "ndvi": round(float(ndvi), 3), "snow": False, "usableShare": 1.0})
    return out


def test_by_year_finds_peak_green_up_and_brown_down():
    row = vh.by_year(_season(2024))[0]
    assert row["year"] == 2024 and row["composites"] == 23
    assert row["peakNdvi"] == pytest.approx(0.8, abs=0.01) and row["peakDate"] == "2024-08-12"
    assert row["greenUpDate"] < row["peakDate"] < row["brownDownDate"]
    assert 60 < row["greenDays"] < 160
    assert row["lowCoverDays"] == 16 * 14  # 7 snow + 7 composites under 0.3


def test_year_without_a_clear_season_has_no_green_up():
    flat = [{"date": (date(2024, 1, 1) + timedelta(days=16 * n)).isoformat(), "ndvi": 0.6, "snow": False,
             "usableShare": 1.0} for n in range(23)]
    row = vh.by_year(flat)[0]
    assert row["peakNdvi"] == 0.6 and row["greenUpDate"] is None and row["greenDays"] is None and row["lowCoverDays"] == 0


def test_calendar_and_low_cover_window_wrap_the_new_year():
    periods = vh.calendar(_season(2023) + _season(2024))
    assert len(periods) == 23 and periods[0] == {"startDay": 1, "startDate": "01-01", "medianNdvi": None,
                                                 "lowCoverShare": 1.0, "snowShare": 1.0, "years": 2}
    summer = next(p for p in periods if p["startDate"] == "07-12")
    assert summer["lowCoverShare"] == 0.0 and summer["medianNdvi"] > 0.6
    windows = vh.low_cover_windows(periods)
    assert len(windows) == 1
    assert windows[0] == {"startDate": "11-01", "endDate": "06-09", "days": 221}
    assert vh.low_cover_windows([dict(p, lowCoverShare=0.0) for p in periods]) == []
    assert vh.low_cover_windows([dict(p, lowCoverShare=1.0) for p in periods])[0]["days"] == 365


def test_ndvi_observation_uses_reliable_pixels_and_flags_snow():
    def values(ndvi, rank):
        return lambda href, shape: np.array(rank if "reliability" in href else ndvi, dtype="float64")

    with patch.object(vh, "_token", return_value="sig"):
        with patch.object(vh, "_field_values", side_effect=values([8000, 6000, -3000, 2000], [0, 1, 3, 3])):
            obs = vh._ndvi_observation(_item("2024-07-11"), SQUARE)
        assert obs == {"date": "2024-07-11", "ndvi": 0.7, "snow": False, "usableShare": 0.5}
        with patch.object(vh, "_field_values", side_effect=values([500, 400, 300, 200], [2, 2, 2, 0])):
            assert vh._ndvi_observation(_item("2024-01-01"), SQUARE)["snow"] is True
        with patch.object(vh, "_field_values", side_effect=values([500, 400, 300, 200], [3, 3, 3, 0])):
            assert vh._ndvi_observation(_item("2024-06-01"), SQUARE) is None


def test_field_values_reads_pixels_inside_the_outline(tmp_path):
    path = tmp_path / "ndvi.tif"
    data = np.arange(100, dtype="int16").reshape(10, 10)
    with rasterio.open(path, "w", driver="GTiff", width=10, height=10, count=1, dtype="int16", crs="EPSG:4326",
                       transform=from_origin(-79.52, 44.53, 0.005, 0.005), nodata=-3000) as dst:
        dst.write(data, 1)
    inside = vh._field_values(str(path), SQUARE)
    assert inside.tolist() == [44.0, 45.0, 54.0, 55.0]
    tiny = {"type": "Polygon", "coordinates": [[[-79.4991, 44.5091], [-79.499, 44.5091], [-79.499, 44.5092],
                                                [-79.4991, 44.5092], [-79.4991, 44.5091]]]}
    assert vh._field_values(str(path), tiny).size == 1


def test_token_is_reused_until_it_nears_expiry():
    vh._tokens.clear()
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    response = MagicMock(json=lambda: {"token": "sig=1", "msft:expiry": expiry})
    href = "https://acct.blob.core.windows.net/box/MOD13Q1/a.tif"
    with patch.object(vh._session, "get", return_value=response) as get:
        assert vh._token(href) == "sig=1" and vh._token(href) == "sig=1"
    assert get.call_count == 1 and get.call_args.args[0].endswith("/token/acct/box")
    vh._tokens.clear()
    with patch.object(vh._session, "get", return_value=MagicMock(json=lambda: {})), pytest.raises(ProviderError):
        vh._token(href)


def test_search_keeps_terra_items_in_date_order():
    page = {"features": [_item("2024-01-17"), _item("2024-01-09", "MYD13Q1"), _item("2024-01-01")], "links": []}
    with patch.object(vh._session, "post", return_value=MagicMock(json=lambda: page)) as post:
        items = vh._search(vh.NDVI_COLLECTION, -79.5, 44.5, "2024-01-01", "2024-02-01")
    assert [i["properties"]["start_datetime"][:10] for i in items] == ["2024-01-01", "2024-01-17"]
    assert post.call_args.kwargs["json"]["intersects"]["coordinates"] == [-79.5, 44.5]


def _history(years=2, fail=()):
    vh._cache.clear()
    season = {o["date"]: o for y in (2024, 2025) for o in _season(y)}

    def search(collection, lon, lat, start, end):
        if collection == vh.NDVI_COLLECTION:
            return [_item(day) for day in sorted(season) if day >= start]
        return [_item(f"{year}-01-01", "MOD17A3HGF") for year in range(2001, 2026)]

    def observation(item, shape):
        day = item["properties"]["start_datetime"][:10]
        if day in fail:
            raise OSError("unreadable")
        return season[day]

    def annual(item, asset, shape, scale, valid_max):
        year = int(item["properties"]["start_datetime"][:4])
        if asset == "Npp_500m":
            return round(0.5 + 0.01 * (year - 2001), 3)
        return 500.0 if asset == "ET_500m" else 800.0

    with patch.object(vh, "_search", side_effect=search), patch.object(vh, "_ndvi_observation", side_effect=observation), \
            patch.object(vh, "_annual_value", side_effect=annual):
        return vh.history(SQUARE, years, TODAY)


def test_history_combines_ndvi_and_yearly_values():
    out = _history()
    assert len(out["ndvi"]["observations"]) == 46 and out["ndvi"]["period"]["start"] == "2024-01-01"
    assert [s["year"] for s in out["ndvi"]["byYear"]] == [2024, 2025]
    assert out["ndvi"]["lowCoverWindows"][0]["startDate"] == "11-01"
    assert out["ndvi"]["typical"]["lowCoverDays"] == 224
    assert len(out["yearly"]) == 25 and out["yearly"][0] == {
        "year": 2001, "nppKgCPerM2": 0.5, "etMm": 500.0, "petMm": 800.0, "etShareOfPet": 0.62}
    assert out["yearlySummary"]["nppTrendKgCPerM2PerYear"] == pytest.approx(0.01, abs=1e-4)
    assert out["yearlySummary"]["etMedianMm"] == 500.0
    assert out["filesChecked"] == 71 and out["filesUnreadable"] == 0


def test_history_survives_unreadable_files_and_validates_years():
    out = _history(fail=("2024-07-11", "2025-07-12"))
    assert out["filesUnreadable"] == 2 and len(out["ndvi"]["observations"]) == 44
    with pytest.raises(ValidationError):
        vh.history(SQUARE, 0, TODAY)
    with pytest.raises(ValidationError):
        vh.history(SQUARE, 11, TODAY)


def test_routes():
    client = TestClient(server.app)
    with patch.object(vh, "history", return_value={"ndvi": {}, "yearly": []}) as history:
        posted = client.post("/vegetation-history", json={"polygon": SQUARE, "years": 3})
        assert posted.status_code == 200 and history.call_args.args[1] == 3
        assert client.post("/vegetation-history", json={"polygon": SQUARE, "years": 40}).status_code == 422
        assert client.get("/fields/nope/vegetation-history").status_code == 404
    with patch.object(vh, "history", side_effect=ProviderError("Planetary Computer (MODIS catalogue) is unavailable")):
        assert client.post("/vegetation-history", json={"polygon": SQUARE}).status_code == 502
