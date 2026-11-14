"""Turn a daily weather table into agricultural indicators.

Everything here is a pure function over a POWER-shaped DataFrame, so it is
easy to test and easy to swap in a different provider later.

Missing data is never treated as an observation. A calendar year (or season)
contributes to a statistic only when at least `min_coverage` of its days have
valid values; sums from qualifying years are scaled from the valid days to the
full period. Statistics with no qualifying years are None, not 0.
"""

from __future__ import annotations

import calendar

import numpy as np
import pandas as pd

from .errors import InsufficientDataError, ValidationError

NORTH_SEASON = (5, 6, 7, 8, 9)

SOUTH_SEASON = (11, 12, 1, 2, 3)


BASE_TEMP_C = 10.0

MIN_COVERAGE = 0.9

HEAVY_RAIN_MM = 20.0


REQUIRED_COLUMNS = ("tmax_c", "tmin_c", "precip_mm")


def default_season(lat: float) -> tuple[int, ...]:
    """May-Sep in the northern hemisphere, Nov-Mar in the southern."""
    return NORTH_SEASON if lat >= 0 else SOUTH_SEASON


def validate_season(months) -> tuple[int, ...]:
    """Months in growing order, consecutive, may wrap past December."""
    try:
        months = tuple(int(m) for m in months)
    except (TypeError, ValueError):
        raise ValidationError(
            "season months must be integers 1-12", "season.months"
        ) from None
    if (
        not months
        or len(set(months)) != len(months)
        or not all(1 <= m <= 12 for m in months)
    ):
        raise ValidationError(
            "season months must be distinct integers 1-12", "season.months"
        )
    for a, b in zip(months, months[1:], strict=False):
        if b != a % 12 + 1:
            raise ValidationError(
                "season months must be consecutive in growing order, e.g. [11, 12, 1, 2, 3]",
                "season.months",
            )
    return months


def extraterrestrial_radiation(lat: float, doy) -> np.ndarray:
    """FAO-56 extra-terrestrial radiation Ra (MJ/m^2/day) for a latitude."""
    gsc = 0.0820  # solar constant, MJ m^-2 min^-1
    phi = np.radians(lat)
    dr = 1 + 0.033 * np.cos(2 * np.pi / 365 * doy)
    delta = 0.409 * np.sin(2 * np.pi / 365 * doy - 1.39)
    ws = np.arccos(np.clip(-np.tan(phi) * np.tan(delta), -1, 1))
    return (
        (24 * 60 / np.pi)
        * gsc
        * dr
        * (ws * np.sin(phi) * np.sin(delta) + np.cos(phi) * np.cos(delta) * np.sin(ws))
    )


def et0_hargreaves(df: pd.DataFrame, lat: float) -> pd.Series:
    """Reference ET0 via Hargreaves-Samani (FAO-56), mm/day.

    Used because POWER does not ship soil moisture or ET; this gives a real
    water-balance term from temperature + solar geometry alone. Note the
    0.408 factor converting Ra from MJ/m^2/day to mm/day equivalent.
    """
    doy = df.index.day_of_year.values
    ra_mm = 0.408 * extraterrestrial_radiation(lat, doy)
    tmean = df["tmean_c"].to_numpy()
    trange = (df["tmax_c"] - df["tmin_c"]).clip(lower=0).to_numpy()
    et0 = 0.0023 * ra_mm * (tmean + 17.8) * np.sqrt(trange)
    return pd.Series(np.clip(et0, 0, None), index=df.index, name="et0_mm")


def growing_degree_days(df: pd.DataFrame, base_temp: float = BASE_TEMP_C) -> pd.Series:
    """Daily GDD = max(0, (Tmax+Tmin)/2 - base)."""
    gdd = (((df["tmax_c"] + df["tmin_c"]) / 2) - base_temp).clip(lower=0)
    return gdd.rename("gdd")


def crop_heat_units(df: pd.DataFrame) -> pd.Series:
    """Daily Ontario crop heat units (CHU), the AAFC/OMAFRA equation used to rate corn and soybean varieties.

    Day = 3.33(Tmax-10) - 0.084(Tmax-10)^2, night = 1.8(Tmin-4.4); each is floored at zero and the two are averaged.
    """
    day = (df["tmax_c"].clip(lower=10) - 10)
    daytime = (3.33 * day - 0.084 * day**2).clip(lower=0)
    nighttime = (1.8 * (df["tmin_c"] - 4.4)).clip(lower=0)
    return ((daytime + nighttime) / 2).rename("chu")


CHU_START_MEAN_C = 12.8
CHU_KILLING_FROST_C = -2.0


def available_chu(year: pd.DataFrame, min_coverage: float = MIN_COVERAGE) -> float:
    """Ontario available crop heat units for one calendar year (northern hemisphere).

    Accumulation starts after the first run of three days with mean temperature at least
    12.8 C, no earlier than May 1 (the seed-rating convention), and stops at the first fall
    frost of -2 C or colder (from August on), as in OMAFRA's CHU maps. NaN when the window
    is incomplete or less than `min_coverage` of its days have temperatures.
    """
    t = year[["tmax_c", "tmin_c"]]
    mean = (t["tmax_c"] + t["tmin_c"]) / 2
    warm = (mean >= CHU_START_MEAN_C) & (year.index.month >= 5)
    run = warm.astype(int).groupby((~warm).cumsum()).cumsum()
    started = run[run >= 3]
    if started.empty:
        return float("nan")
    start = started.index[0] + pd.Timedelta(days=1)
    frost = year.index[(t["tmin_c"] <= CHU_KILLING_FROST_C) & (year.index > start) & (year.index.month >= 8)]
    if frost.empty:
        return float("nan")
    window = year.loc[start:frost[0] - pd.Timedelta(days=1)]
    valid = window["tmax_c"].notna() & window["tmin_c"].notna()
    if window.empty or valid.mean() < min_coverage:
        return float("nan")
    return float(crop_heat_units(window[valid]).sum() / valid.mean())


def longest_run(mask: np.ndarray) -> int:
    """Longest run of True in a boolean array."""
    best = cur = 0
    for flag in mask:
        cur = cur + 1 if flag else 0
        best = max(best, cur)
    return best


def longest_dry_spell(precip_mm: pd.Series, threshold_mm: float = 1.0) -> int:
    """Longest run of days with precipitation below `threshold_mm`.

    A missing day ends a run: it is not counted as dry.
    """
    return longest_run((precip_mm < threshold_mm).to_numpy())


def frost_free_days(tmin_c: pd.Series) -> int:
    """Longest run of days without a freeze (Tmin > 0)."""
    return longest_run((tmin_c > 0).to_numpy())


def clean(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Reindex to whole calendar years and blank physically impossible values.

    Returns the cleaned frame and counts of what was changed.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValidationError(
            f"weather frame is missing columns: {', '.join(missing)}", "frame"
        )
    if len(df) == 0:
        raise InsufficientDataError("weather frame is empty", "frame")
    d = df.copy()
    d.index = pd.DatetimeIndex(d.index).normalize()
    dupes = int(d.index.duplicated().sum())
    d = d[~d.index.duplicated(keep="first")].sort_index()
    for col in ("tmax_c", "tmin_c", "precip_mm", "tmean_c"):
        if col in d:
            d[col] = pd.to_numeric(d[col], errors="coerce").astype("float64")

    bad_temp = (
        (d["tmax_c"] < d["tmin_c"])
        | ~d["tmax_c"].between(-90, 60)
        | ~d["tmin_c"].between(-90, 60)
    )
    bad_temp &= d["tmax_c"].notna() & d["tmin_c"].notna()
    d.loc[bad_temp, ["tmax_c", "tmin_c"]] = np.nan
    bad_rain = (d["precip_mm"] < 0) | (d["precip_mm"] > 1000)
    d.loc[bad_rain, "precip_mm"] = np.nan
    if "tmean_c" not in d:
        d["tmean_c"] = np.nan
    d["tmean_c"] = d["tmean_c"].where(
        d["tmean_c"].notna(), (d["tmax_c"] + d["tmin_c"]) / 2
    )

    full = pd.date_range(
        f"{d.index[0].year}-01-01", f"{d.index[-1].year}-12-31", freq="D"
    )
    d = d.reindex(full)
    d.index.name = "date"
    return d, {
        "duplicate_days_dropped": dupes,
        "impossible_temperature_days": int(bad_temp.sum()),
        "impossible_precip_days": int(bad_rain.sum()),
    }


def _season_days(season_year: int, months: tuple[int, ...]) -> int:
    start = months[0]
    return sum(calendar.monthrange(season_year + (m < start), m)[1] for m in months)


def _scaled(
    values: pd.Series,
    valid: np.ndarray,
    expected: pd.Series,
    frac: pd.Series,
    min_cov: float,
) -> pd.Series:
    """Sum of valid values scaled to the expected day count; NaN below coverage.

    `values` is indexed by year (repeated per day); `valid` is a positional mask.
    """
    total = values.where(valid, 0.0).groupby(level=0).sum()
    n_valid = pd.Series(valid, index=values.index).groupby(level=0).sum()
    out = total / n_valid.replace(0, np.nan) * expected
    return out.where(frac >= min_cov)


def _yearly(
    df: pd.DataFrame,
    lat: float,
    season: tuple[int, ...] = NORTH_SEASON,
    base_temp: float = BASE_TEMP_C,
    min_coverage: float = MIN_COVERAGE,
) -> pd.DataFrame:
    """Per-year indicator table. Season columns are indexed by the season's start year."""
    d = df.copy()
    d["et0_mm"] = et0_hargreaves(d, lat)
    d["gdd"] = growing_degree_days(d, base_temp)
    temp_ok = d["tmax_c"].notna() & d["tmin_c"].notna()
    rain_ok = d["precip_mm"].notna()

    year = pd.Series(d.index.year, index=d.index)
    by_year = d.set_index(year.to_numpy())
    t_ok, r_ok = temp_ok.to_numpy(), rain_ok.to_numpy()
    expected = by_year.groupby(level=0).size().astype(float)
    t_frac = pd.Series(t_ok, index=by_year.index).groupby(level=0).mean()
    r_frac = pd.Series(r_ok, index=by_year.index).groupby(level=0).mean()

    def count(mask: pd.Series, valid: np.ndarray, frac: pd.Series) -> pd.Series:
        return _scaled(mask.astype(float), valid, expected, frac, min_coverage).round()

    tmax, tmin = by_year["tmax_c"], by_year["tmin_c"]
    out = pd.DataFrame(
        {
            "precip_mm": _scaled(
                by_year["precip_mm"], r_ok, expected, r_frac, min_coverage
            ),
            "gdd_annual": _scaled(by_year["gdd"], t_ok, expected, t_frac, min_coverage),
            "heat_days_30": count(tmax >= 30, t_ok, t_frac),
            "heat_days_35": count(tmax >= 35, t_ok, t_frac),
            "frost_days": count(tmin <= 0, t_ok, t_frac),
            "heavy_rain_days": count(
                by_year["precip_mm"] >= HEAVY_RAIN_MM, r_ok, r_frac
            ),
            "max_1day_precip_mm": by_year["precip_mm"]
            .groupby(level=0)
            .max()
            .where(r_frac >= min_coverage),
            "dry_spell_days": by_year["precip_mm"]
            .groupby(level=0)
            .apply(longest_dry_spell)
            .astype(float)
            .where(r_frac >= min_coverage),
            "frost_free_days": by_year["tmin_c"]
            .groupby(level=0)
            .apply(frost_free_days)
            .astype(float)
            .where(t_frac >= min_coverage),
            "temp_coverage": t_frac,
            "precip_coverage": r_frac,
        }
    )

    # Growing season, possibly crossing the new year.
    in_season = d.index.month.isin(season)
    s = d[in_season]
    s_year = (s.index.year - (s.index.month < season[0])).to_numpy()
    s = s.set_index(s_year)
    s_t = temp_ok.to_numpy()[in_season]
    s_r = rain_ok.to_numpy()[in_season]
    s_expected = pd.Series(
        {y: float(_season_days(y, season)) for y in np.unique(s_year)}
    )
    s_tf = pd.Series(s_t, index=s_year).groupby(level=0).sum() / s_expected
    s_rf = pd.Series(s_r, index=s_year).groupby(level=0).sum() / s_expected
    season_tbl = pd.DataFrame(
        {
            "season_precip_mm": _scaled(
                s["precip_mm"], s_r, s_expected, s_rf, min_coverage
            ),
            "season_et0_mm": _scaled(s["et0_mm"], s_t, s_expected, s_tf, min_coverage),
            "gdd_season": _scaled(s["gdd"], s_t, s_expected, s_tf, min_coverage),
        }
    )
    out = out.join(season_tbl, how="outer")
    if lat >= 0:
        out["chu_available"] = d.groupby(d.index.year).apply(lambda y: available_chu(y, min_coverage))
    else:
        out["chu_available"] = np.nan
    out["water_deficit_mm"] = (out["season_et0_mm"] - out["season_precip_mm"]).clip(
        lower=0
    )
    out["aridity_index"] = out["season_precip_mm"] / out["season_et0_mm"].replace(
        0, np.nan
    )
    out["water_stress_index"] = (
        out["water_deficit_mm"] / out["season_et0_mm"].replace(0, np.nan)
    ).clip(0, 1)
    out.index.name = "year"
    return out


def _median(series: pd.Series) -> float | None:
    y = series.dropna()
    return float(y.median()) if len(y) else None


def _trend(series: pd.Series) -> float | None:
    """Least-squares slope of a yearly series (units per year). None below 3 years."""
    y = series.dropna()
    if len(y) < 3:
        return None
    x = y.index.to_numpy(dtype=float)
    return float(np.polyfit(x, y.to_numpy(), 1)[0])


def _recent_vs_early(series: pd.Series, window: int = 5) -> float | None:
    """Fractional change of the last `window` years vs the first `window`.

    None when there are fewer than 2*window valid years or the early mean is 0.
    """
    y = series.dropna()
    if len(y) < 2 * window:
        return None
    early, recent = y.iloc[:window].mean(), y.iloc[-window:].mean()
    if early == 0:
        return None
    return float((recent - early) / abs(early))


YEAR_FIELDS = (
    "season_precip_mm",
    "season_et0_mm",
    "water_stress_index",
    "gdd_annual",
    "gdd_season",
    "chu_available",
    "heat_days_30",
    "dry_spell_days",
    "precip_mm",
    "heavy_rain_days",
)


def _month_day(doy: float | None, shift_days: int) -> str | None:
    """Day of a (shifted) year back to a calendar 'MM-DD' string."""
    if doy is None:
        return None
    date = pd.Timestamp("2001-01-01") + pd.Timedelta(days=round(doy) - 1 - shift_days)
    return date.strftime("%m-%d")


def _common_doy(day: pd.Timestamp) -> int:
    """Day of year on a 365-day calendar, so leap years line up with other years."""
    return day.day_of_year - (day.is_leap_year and day.month > 2)


def frost_dates(
    frame: pd.DataFrame, lat: float, min_coverage: float = MIN_COVERAGE
) -> dict:
    """Typical last spring frost and first fall frost (Tmin <= 0 C).

    South of the equator the year is shifted by six months so spring and fall
    keep their meaning. Days of year are in that shifted calendar; the MM-DD
    strings are real calendar dates. Years with too many missing days are
    skipped. A year with no frost on one side contributes nothing to it.
    """
    shift = 0 if lat >= 0 else 182
    tmin = frame["tmin_c"].copy()
    tmin.index = tmin.index + pd.Timedelta(days=shift)
    springs, falls, by_year = [], [], {}
    for year, days in tmin.groupby(tmin.index.year):
        if days.notna().mean() < min_coverage or len(days) < 360:
            continue
        frost = days[days <= 0]
        spring = frost[frost.index.month <= 7]
        fall = frost[frost.index.month >= 8]
        if len(spring):
            springs.append(_common_doy(spring.index[-1]))
            by_year[str(int(year))] = int(springs[-1])
        if len(fall):
            falls.append(_common_doy(fall.index[0]))
    last = float(np.median(springs)) if springs else None
    first = float(np.median(falls)) if falls else None
    return {
        "last_spring_frost": _month_day(last, shift),
        "first_fall_frost": _month_day(first, shift),
        "last_spring_frost_doy": last,
        "first_fall_frost_doy": first,
        "frost_free_season_days": (first - last)
        if last is not None and first is not None
        else None,
        "spring_frost_years": len(springs),
        "last_spring_frost_doy_by_year": by_year,
        "calendar_shift_days": shift,
    }


def month_table(
    frame: pd.DataFrame,
    lat: float,
    min_coverage: float = MIN_COVERAGE,
    base_temp: float = BASE_TEMP_C,
) -> pd.DataFrame:
    """One row per (year, month). Months below `min_coverage` are NaN."""
    d = frame.copy()
    d["et0_mm"] = et0_hargreaves(d, lat)
    d["gdd"] = growing_degree_days(d, base_temp)
    t_ok = d["tmax_c"].notna() & d["tmin_c"].notna()
    r_ok = d["precip_mm"].notna()
    key = [d.index.year, d.index.month]
    days = d.groupby(key).size()
    t_frac = t_ok.groupby(key).sum() / days
    r_frac = r_ok.groupby(key).sum() / days

    def scaled(values: pd.Series, ok: pd.Series, frac: pd.Series) -> pd.Series:
        total = values.where(ok, 0.0).groupby(key).sum()
        return (total / ok.groupby(key).sum().replace(0, np.nan) * days).where(
            frac >= min_coverage
        )

    return pd.DataFrame(
        {
            "precip_mm": scaled(d["precip_mm"], r_ok, r_frac),
            "heavy_rain_days": scaled(
                (d["precip_mm"] >= HEAVY_RAIN_MM).astype(float), r_ok, r_frac
            ),
            "et0_mm": scaled(d["et0_mm"], t_ok, t_frac),
            "frost_days": scaled((d["tmin_c"] <= 0).astype(float), t_ok, t_frac),
            "heat_days_30": scaled((d["tmax_c"] >= 30).astype(float), t_ok, t_frac),
            "gdd": scaled(d["gdd"], t_ok, t_frac),
            "tmean_c": d["tmean_c"]
            .where(t_ok)
            .groupby(key)
            .mean()
            .where(t_frac >= min_coverage),
            "tmax_c": d["tmax_c"]
            .where(t_ok)
            .groupby(key)
            .mean()
            .where(t_frac >= min_coverage),
            "tmin_c": d["tmin_c"]
            .where(t_ok)
            .groupby(key)
            .mean()
            .where(t_frac >= min_coverage),
        }
    )


def monthly_climatology(table: pd.DataFrame) -> list[dict]:
    """Average month over the years in `_month_table`. None when no month qualifies."""
    means = table.groupby(level=1).mean()
    rows = []
    for month in range(1, 13):
        row = {"month": month}
        for col, digits in (
            ("precip_mm", 1),
            ("heavy_rain_days", 2),
            ("et0_mm", 1),
            ("frost_days", 1),
            ("tmean_c", 1),
        ):
            value = means[col].get(month) if month in means.index else None
            row[col] = (
                None if value is None or pd.isna(value) else round(float(value), digits)
            )
        rows.append(row)
    return rows


def summarize(
    df: pd.DataFrame,
    lat: float,
    growing_months: tuple[int, ...] | None = None,
    base_temp: float = BASE_TEMP_C,
    min_coverage: float = MIN_COVERAGE,
) -> tuple[pd.DataFrame, dict]:
    """Return (per-year table, indicator dict).

    The dict has climate, water, frost, monthly, season and coverage blocks.
    `growing_months` defaults to the hemisphere's season (see default_season).
    """
    if lat is None:
        raise ValidationError("latitude is required to compute ET0", "location.lat")
    season = (
        validate_season(growing_months)
        if growing_months is not None
        else default_season(lat)
    )
    frame, cleaning = clean(df)
    yearly = _yearly(frame, lat, season, base_temp, min_coverage)

    climate = {
        "gdd_annual_median": _median(yearly["gdd_annual"]),
        "gdd_season_median": _median(yearly["gdd_season"]),
        "chu_available_median": _median(yearly["chu_available"]),
        "growing_season_precip_mm_median": _median(yearly["season_precip_mm"]),
        "annual_precip_mm_median": _median(yearly["precip_mm"]),
        "median_dry_spell_days": _median(yearly["dry_spell_days"]),
        "frost_free_days_median": _median(yearly["frost_free_days"]),
        "heat_days_30_median": _median(yearly["heat_days_30"]),
        "heavy_rain_days_median": _median(yearly["heavy_rain_days"]),
        "max_1day_precip_mm_median": _median(yearly["max_1day_precip_mm"]),
        # trends, in units per year
        "precip_trend_mm_per_year": _trend(yearly["precip_mm"]),
        "season_precip_trend_mm_per_year": _trend(yearly["season_precip_mm"]),
        "heat_days_30_trend": _trend(yearly["heat_days_30"]),
        "dry_spell_trend_days_per_year": _trend(yearly["dry_spell_days"]),
        "heavy_rain_days_trend": _trend(yearly["heavy_rain_days"]),
        # recent-vs-early fractional changes (climate shift signal)
        "precip_shift_pct": _recent_vs_early(yearly["season_precip_mm"]),
        "heat_shift_pct": _recent_vs_early(yearly["heat_days_30"]),
        "dry_spell_shift_pct": _recent_vs_early(yearly["dry_spell_days"]),
    }

    water = {
        "season_et0_mm_median": _median(yearly["season_et0_mm"]),
        "water_deficit_mm_median": _median(yearly["water_deficit_mm"]),
        "aridity_index_median": _median(yearly["aridity_index"]),
        "water_stress_index": _median(yearly["water_stress_index"]),
        "water_stress_shift_pct": _recent_vs_early(yearly["water_stress_index"]),
    }

    temp_years = int(yearly["gdd_annual"].notna().sum())
    water_years = int(yearly["water_stress_index"].notna().sum())
    coverage = {
        "first_date": str(df.index.min().date()) if len(df) else None,
        "last_date": str(df.index.max().date()) if len(df) else None,
        "calendar_years": int(len(yearly["temp_coverage"].dropna())),
        "years_with_temperature": temp_years,
        "years_with_water_balance": water_years,
        "min_coverage": min_coverage,
        "missing_temperature_days": int(
            (frame["tmax_c"].isna() | frame["tmin_c"].isna()).sum()
        ),
        "missing_precip_days": int(frame["precip_mm"].isna().sum()),
        **cleaning,
    }
    season_info = {
        "months": list(season),
        "crosses_year": season[-1] < season[0],
        "base_temp_c": base_temp,
        "label": f"{calendar.month_abbr[season[0]]}-{calendar.month_abbr[season[-1]]}",
    }
    months = month_table(frame, lat, min_coverage, base_temp)
    return yearly, {
        "climate": climate,
        "water": water,
        "frost": frost_dates(frame, lat, min_coverage),
        "monthly": monthly_climatology(months),
        "season": season_info,
        "coverage": coverage,
    }
