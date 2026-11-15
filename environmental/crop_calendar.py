"""Usual planting and maturity dates for crops at a location, from the GGCMI Phase 3 crop calendar.

The calendar (Jägermeyr, Müller, Minoli, Ray and Siebert 2021; NASA GISS, PIK and others; CC BY 4.0) gives a
planting day, maturity day and growing-season length for 18 crops on a 0.5 degree grid (about 50 km), for
rainfed and irrigated farming. Winter and spring wheat, and two rice seasons, are separate entries. Values are
multi-year averages around the year 2000, not a forecast, and cells where a crop is not grown are filled in from
neighbouring areas, so a date here does not mean the crop is grown locally.

One small file per crop and system is downloaded from Zenodo on first use and kept in CROP_CALENDAR_DIR (default
data/crop-calendar). A lookup reads one cell from each file.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import requests

from . import geometry
from .errors import ProviderError, ValidationError

RECORD_URL = "https://zenodo.org/records/5062513"
FILE_URL = "https://zenodo.org/api/records/5062513/files/{name}/content"
FILE_NAME = "{code}_{system}_ggcmi_crop_calendar_phase3_v1.01.nc4"
SYSTEMS = {"rainfed": "rf", "irrigated": "ir"}
FILL_ABOVE = 1e19  # the files mark "no value" with 1e20
TIMEOUT = (10, 60)
PARALLEL = 8

# file code: (key used in responses, label)
CROPS = {
    "mai": ("corn", "Corn"),
    "soy": ("soybeans", "Soybeans"),
    "wwh": ("winter_wheat", "Winter wheat"),
    "swh": ("spring_wheat", "Spring wheat"),
    "bar": ("barley", "Barley"),
    "rye": ("rye", "Rye"),
    "rap": ("canola", "Canola (rapeseed)"),
    "pot": ("potatoes", "Potatoes"),
    "bea": ("beans", "Dry beans"),
    "pea": ("peas", "Field peas"),
    "sun": ("sunflower", "Sunflower"),
    "sgb": ("sugar_beet", "Sugar beet"),
    "sor": ("sorghum", "Sorghum"),
    "mil": ("millet", "Millet"),
    "ri1": ("rice_first_season", "Rice (first season)"),
    "ri2": ("rice_second_season", "Rice (second season)"),
    "cot": ("cotton", "Cotton"),
    "nut": ("groundnut", "Groundnut"),
    "cas": ("cassava", "Cassava"),
    "sgc": ("sugarcane", "Sugarcane"),
}
KEYS = {key: code for code, (key, _) in CROPS.items()}
DATA_SOURCES = {
    1: "MIRCA2000", 2: "SAGE", 3: "Iizumi et al. 2019", 4: "RiceAtlas", 5: "Dimou et al. 2018",
    6: "ChinaCropPhen", 7: "India AgStat", 8: "Brazil CONAB", 9: "Australia ABARES",
}

_session = requests.Session()
_download_lock = threading.Lock()


def directory() -> Path:
    """Where calendar files are kept: CROP_CALENDAR_DIR, else data/crop-calendar beside the code."""
    if os.environ.get("CROP_CALENDAR_DIR"):
        return Path(os.environ["CROP_CALENDAR_DIR"])
    return Path(__file__).resolve().parent.parent / "data" / "crop-calendar"


def _file(code: str, system: str) -> Path:
    """Path of one crop's file, downloading it first if it is not there yet."""
    name = FILE_NAME.format(code=code, system=SYSTEMS[system])
    path = directory() / name
    if path.exists():
        return path
    with _download_lock:
        if path.exists():
            return path
        try:
            resp = _session.get(FILE_URL.format(name=name), timeout=TIMEOUT)
            resp.raise_for_status()
            if not resp.content.startswith(b"\x89HDF"):
                raise ValueError("not a NetCDF-4 file")
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(".part")
            partial.write_bytes(resp.content)
            partial.replace(path)
        except (requests.RequestException, OSError, ValueError) as exc:
            raise ProviderError("GGCMI crop calendar could not be downloaded") from exc
    return path


def _month_day(day_of_year: int) -> str:
    """Day of year as MM-DD in a non-leap year."""
    return (date(2001, 1, 1) + timedelta(days=day_of_year - 1)).strftime("%m-%d")


def _read(path: Path, lat: float, lon: float) -> dict:
    """The calendar values in the grid cell containing a point, and that cell's centre."""
    import h5py  # imported here so the API starts without it

    try:
        with h5py.File(path, "r") as f:
            lats, lons = f["lat"][:], f["lon"][:]
            i = int(abs(lats - lat).argmin())
            j = int(abs(lons - lon).argmin())
            values = {name: float(f[name][i, j]) for name in
                      ("planting_day", "maturity_day", "growing_season_length", "data_source_used")}
            return {"lat": float(lats[i]), "lon": float(lons[j]), **values}
    except (OSError, KeyError, ValueError, IndexError) as exc:
        raise ProviderError("GGCMI crop calendar file could not be read") from exc


@lru_cache(maxsize=4096)
def _entry(code: str, system: str, lat: float, lon: float) -> tuple:
    """One crop's row and its grid cell. Cached per half-degree cell (callers pass snapped coordinates)."""
    raw = _read(_file(code, system), lat, lon)
    key, label = CROPS[code]

    def whole(name):
        value = raw[name]
        return None if value != value or abs(value) > FILL_ABOVE else int(round(value))

    planting, maturity, length = whole("planting_day"), whole("maturity_day"), whole("growing_season_length")
    source = whole("data_source_used")
    row = {
        "crop": key,
        "label": label,
        "plantingDay": planting,
        "plantingDate": None if planting is None else _month_day(planting),
        "maturityDay": maturity,
        "maturityDate": None if maturity is None else _month_day(maturity),
        "growingSeasonDays": length,
        "offSeasonDays": None if length is None else 365 - length,
        "crossesNewYear": None if planting is None or maturity is None else maturity < planting,
        "dataSource": DATA_SOURCES.get(source),
    }
    return row, raw["lat"], raw["lon"]


def _snap(value: float) -> float:
    """Centre of the half-degree cell containing a coordinate."""
    return round((value // 0.5) * 0.5 + 0.25, 2)


def calendar(polygon, system: str = "rainfed", crops: list[str] | None = None) -> dict:
    """Usual planting and maturity dates around a field outline for every calendar crop, or the ones named."""
    if system not in SYSTEMS:
        raise ValidationError("system must be rainfed or irrigated", "system")
    unknown = [c for c in crops or [] if c not in KEYS]
    if unknown:
        raise ValidationError(f"unknown crops: {', '.join(unknown)}; choose from {', '.join(KEYS)}", "crops")
    codes = [KEYS[c] for c in dict.fromkeys(crops)] if crops else list(CROPS)
    lon, lat = geometry.centroid(polygon)
    cell_lat, cell_lon = _snap(lat), _snap(lon)
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        results = list(pool.map(lambda code: _entry(code, system, cell_lat, cell_lon), codes))
    rows = [row for row, _, _ in results]
    return {
        "location": {"lat": round(lat, 6), "lon": round(lon, 6)},
        "cell": {"lat": results[0][1], "lon": results[0][2], "sizeDeg": 0.5},
        "system": system,
        "crops": rows,
        "source": {"name": "GGCMI Phase 3 crop calendar", "url": RECORD_URL, "doi": "10.5281/zenodo.5062513",
                   "version": "1.01", "resolutionDeg": 0.5, "license": "CC BY 4.0",
                   "citation": "Jägermeyr, Müller, Minoli, Ray and Siebert (2021)"},
        "note": ("Usual planting and maturity dates for the roughly 50 km cell around the field, as multi-year "
                 "averages around the year 2000. They are a guide to the normal season, not a forecast, and do not "
                 "reflect local varieties or recent shifts. Cells where a crop is not grown are filled in from "
                 "neighbouring areas, so a date does not mean the crop is grown here. Days are counted from 1 "
                 "January; dates are month-day."),
    }
