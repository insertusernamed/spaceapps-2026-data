"""Year-by-year indicators returned with a field summary."""
import numpy as np
import pandas as pd

from environmental import indicators


def _frame(years=(2020, 2021, 2022)):
    days = pd.date_range(f"{years[0]}-01-01", f"{years[-1]}-12-31", freq="D")
    doy = days.dayofyear.to_numpy()
    warm = 10 + 15 * np.sin((doy - 100) / 365 * 2 * np.pi)
    return pd.DataFrame({"tmean_c": warm, "tmax_c": warm + 5, "tmin_c": warm - 5,
                         "precip_mm": np.where(doy % 4 == 0, 8.0, 0.0)}, index=days)


def test_yearly_block_has_one_row_per_year_and_percentiles():
    _, summary = indicators.summarize(_frame(), 44.6)
    block = summary["yearly"]
    assert [row["year"] for row in block["years"]] == [2020, 2021, 2022]
    assert set(indicators.YEARLY_COLUMNS) <= set(block["years"][0])
    chu = block["percentiles"]["chu_available"]
    assert chu["years"] == 3 and chu["p10"] <= chu["p50"] <= chu["p90"]


def test_yearly_block_reports_missing_years_as_none():
    frame = _frame()
    frame.loc["2021", ["tmax_c", "tmin_c", "tmean_c"]] = np.nan
    _, summary = indicators.summarize(frame, 44.6)
    row = next(r for r in summary["yearly"]["years"] if r["year"] == 2021)
    assert row["gdd_annual"] is None and row["precip_mm"] is not None
    assert summary["yearly"]["percentiles"]["gdd_annual"]["years"] == 2
