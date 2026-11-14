"""Environmental summary for a selected field, using NASA POWER and SRTM."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

import pandas as pd
import requests
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app import POWER_URL
from environmental import geometry, indicators, terrain
from environmental.errors import DataError, ProviderError


class Period(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date = date(2015, 1, 1)
    end: date = date(2024, 12, 31)

    @field_validator("start", "end", mode="before")
    @classmethod
    def compact_date(cls, value):
        if isinstance(value, str) and len(value) == 8 and value.isdigit():
            return f"{value[:4]}-{value[4:6]}-{value[6:]}"
        if isinstance(value, (int, float)):
            raise ValueError("use YYYYMMDD or YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def valid_range(self):
        if (
            self.start < date(1981, 1, 1)
            or self.end > date.today()
            or self.start > self.end
        ):
            raise ValueError(
                "period must be ordered, begin in 1981 or later, and end no later than today"
            )
        if (self.end - self.start).days > 45 * 366:
            raise ValueError("period must not exceed 45 years")
        return self


class Season(BaseModel):
    model_config = ConfigDict(extra="forbid")
    months: list[int] = Field(min_length=1, max_length=12, strict=True)

    @field_validator("months", mode="before")
    @classmethod
    def valid_months(cls, value):
        if not isinstance(value, list) or any(type(m) is not int for m in value):
            raise ValueError("months must be integers")
        return list(indicators.validate_season(value))


class FieldSummaryRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "polygon": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-91.94, 41.36],
                                [-91.93, 41.36],
                                [-91.93, 41.37],
                                [-91.94, 41.37],
                                [-91.94, 41.36],
                            ]
                        ],
                    },
                    "period": {"start": "2023-01-01", "end": "2023-12-31"},
                }
            ]
        },
    )
    polygon: dict[str, Any] | list[list[float]] = Field(
        description="GeoJSON Polygon, MultiPolygon or Feature, or a ring of [longitude, latitude] positions."
    )
    period: Period = Field(default_factory=Period)
    season: Season | None = None
    weatherGridDeg: float = Field(
        default=0.05,
        ge=0,
        le=1,
        allow_inf_nan=False,
        description="Weather sampling grid in degrees. Zero uses the polygon centroid; this does not change the source resolution.",
    )

    @field_validator("polygon", mode="before")
    @classmethod
    def valid_polygon(cls, value):
        try:
            geometry.parts(value)
            if geometry.area_ha(value) <= 0:
                raise ValueError("polygon must have positive area")
        except (DataError, TypeError, IndexError, KeyError) as exc:
            raise ValueError(str(exc)) from exc
        return value


VARIABLES = {
    "T2M": "tmean_c",
    "T2M_MAX": "tmax_c",
    "T2M_MIN": "tmin_c",
    "PRECTOTCORR": "precip_mm",
}


def fetch_weather(lat: float, lon: float, start: str, end: str) -> pd.DataFrame:
    try:
        response = requests.get(
            POWER_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "start": start,
                "end": end,
                "parameters": ",".join(VARIABLES),
                "community": "AG",
                "format": "JSON",
            },
            timeout=(10, 60),
        )
        response.raise_for_status()
        raw = response.json()["properties"]["parameter"]
        frame = pd.DataFrame(raw).rename(columns=VARIABLES)
        if frame.empty or not set(VARIABLES.values()).issubset(frame.columns):
            raise ValueError("missing daily weather columns")
        frame.index = pd.to_datetime(frame.index, format="%Y%m%d")
        frame = (
            frame.loc[start:end]
            if frame.index.is_monotonic_increasing
            else frame.sort_index().loc[start:end]
        )
        if frame.empty:
            raise ValueError("no observations in requested period")
        return frame.replace(-999, float("nan")).astype(float)
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        raise ProviderError(
            "NASA POWER daily weather is unavailable or invalid"
        ) from exc


def summarize_field(request: FieldSummaryRequest) -> dict:
    lon, lat = geometry.centroid(request.polygon)
    grid = request.weatherGridDeg
    wlat, wlon = (
        (lat, lon)
        if grid == 0
        else (
            (math.floor(lat / grid) + 0.5) * grid,
            (math.floor(lon / grid) + 0.5) * grid,
        )
    )
    wlat, wlon = max(-90, min(90, wlat)), max(-180, min(180, wlon))
    start, end = (
        request.period.start.strftime("%Y%m%d"),
        request.period.end.strftime("%Y%m%d"),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        weather = pool.submit(fetch_weather, wlat, wlon, start, end)
        land = pool.submit(terrain.fetch_terrain, lat, lon, request.polygon)
        _, summary = indicators.summarize(
            weather.result(),
            wlat,
            tuple(request.season.months) if request.season else None,
        )
        try:
            terrain_block = land.result()
            terrain_status = {"status": "ok"}
        except DataError as exc:
            terrain_block = None
            terrain_status = {"status": "error", "message": str(exc)}
    return {
        "field": {
            "centroid": {"lat": round(lat, 6), "lon": round(lon, 6)},
            "areaHa": round(geometry.area_ha(request.polygon), 2),
        },
        "weatherCell": {"lat": round(wlat, 6), "lon": round(wlon, 6), "gridDeg": grid},
        "period": {"start": start, "end": end},
        **{
            key: summary[key]
            for key in ("season", "climate", "water", "frost", "monthly", "coverage")
        },
        "terrain": terrain_block,
        "layers": {"weather": {"status": "ok"}, "terrain": terrain_status},
        "source": [
            {
                "source": "NASA POWER",
                "url": POWER_URL,
                "variables": list(VARIABLES),
                "spatial_resolution_km": 50,
                "temporal_resolution": "daily",
                "period": f"{start}-{end}",
                "location": {"lat": round(wlat, 6), "lon": round(wlon, 6)},
            },
            {
                "source": "Hargreaves ET0 (FAO-56)",
                "note": "Reference evapotranspiration derived from temperature and solar geometry; not measured crop water use.",
            },
            terrain.provenance(lat, lon),
        ],
    }
