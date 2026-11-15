"""Request models for field and outline routes (crop history, greenness, soil); the lookups live in environmental."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from environmental import geometry
from environmental.errors import DataError

# A cropped square in central Illinois with a corn/soybean rotation.
EXAMPLE_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[-88.95, 40.45], [-88.947, 40.45], [-88.947, 40.453], [-88.95, 40.453], [-88.95, 40.45]]],
}


class CropHistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"polygon": EXAMPLE_POLYGON}]})
    polygon: dict[str, Any] | list[list[float]] = Field(
        description="GeoJSON Polygon, MultiPolygon or Feature, or a ring of [longitude, latitude] positions."
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


class ImageryRequest(CropHistoryRequest):
    dataset: Literal["hls", "sentinel-2"] = "hls"
    start: date | None = None
    end: date | None = None
    maxCloud: float = Field(default=30, ge=0, le=100)
    limit: int = Field(default=20, ge=1, le=20)


MAX_BATCH_FIELDS = 100
FieldId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class FieldBatchRequest(BaseModel):
    """Up to MAX_BATCH_FIELDS field IDs, for routes that answer several fields at once."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "ids": ["ftw-2025-17TPK_0_0-13020", "ftw-2025-17TPK_0_0-3837"]}]})
    ids: list[FieldId] = Field(
        min_length=1, max_length=MAX_BATCH_FIELDS,
        description=f"Field IDs from /fields, 1 to {MAX_BATCH_FIELDS}. Duplicates are answered once, in first-seen order.",
    )

    @field_validator("ids")
    @classmethod
    def unique(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


# Original name, kept for existing imports.
FieldCropHistoryBatchRequest = FieldBatchRequest


class GreennessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"polygon": EXAMPLE_POLYGON, "year": 2024}]})
    polygon: dict[str, Any] | list[list[float]] = Field(
        description="GeoJSON Polygon, MultiPolygon or Feature, or a ring of [longitude, latitude] positions."
    )
    year: int | None = Field(default=None, description="Growing season year; defaults to the most recent season.")

    @field_validator("polygon", mode="before")
    @classmethod
    def valid_polygon(cls, value):
        return CropHistoryRequest.valid_polygon(value)


class SoilRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"polygon": EXAMPLE_POLYGON}]})
    polygon: dict[str, Any] | list[list[float]] = Field(
        description="GeoJSON Polygon, MultiPolygon or Feature, or a ring of [longitude, latitude] positions."
    )

    @field_validator("polygon", mode="before")
    @classmethod
    def valid_polygon(cls, value):
        return CropHistoryRequest.valid_polygon(value)


class FieldSeasonRequest(CropHistoryRequest):
    """Explicit observation period; application season choices belong to Java."""
    start: date = Field(description="First observation date, inclusive.")
    end: date = Field(description="Last observation date, inclusive.")
    retry: bool = Field(False, description="Retry a completed acquisition that had failures.")


class AgroWeatherRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"polygon": EXAMPLE_POLYGON, "years": 20}]})
    polygon: dict[str, Any] | list[list[float]] = Field(
        description="GeoJSON Polygon, MultiPolygon or Feature, or a ring of [longitude, latitude] positions."
    )
    years: int = Field(default=20, ge=5, le=45, description="Years of history to read, ending today.")

    @field_validator("polygon", mode="before")
    @classmethod
    def valid_polygon(cls, value):
        return CropHistoryRequest.valid_polygon(value)


class CropCalendarRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"polygon": EXAMPLE_POLYGON, "system": "rainfed", "crops": ["corn", "soybeans", "winter_wheat"]}]})
    polygon: dict[str, Any] | list[list[float]] = Field(
        description="GeoJSON Polygon, MultiPolygon or Feature, or a ring of [longitude, latitude] positions."
    )
    system: Literal["rainfed", "irrigated"] = Field(default="rainfed", description="Rainfed or irrigated farming.")
    crops: list[str] | None = Field(default=None, min_length=1, max_length=20,
                                    description="Calendar crop keys to return; all crops when omitted.")

    @field_validator("polygon", mode="before")
    @classmethod
    def valid_polygon(cls, value):
        return CropHistoryRequest.valid_polygon(value)


class VegetationHistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"polygon": EXAMPLE_POLYGON, "years": 5}]})
    polygon: dict[str, Any] | list[list[float]] = Field(
        description="GeoJSON Polygon, MultiPolygon or Feature, or a ring of [longitude, latitude] positions."
    )
    years: int = Field(default=5, ge=1, le=10, description="Years of 16-day NDVI to read, ending today.")

    @field_validator("polygon", mode="before")
    @classmethod
    def valid_polygon(cls, value):
        return CropHistoryRequest.valid_polygon(value)


class DroughtIndicatorsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [
        {"polygon": EXAMPLE_POLYGON, "weeks": 12, "years": 10}]})
    polygon: dict[str, Any] | list[list[float]] = Field(
        description="GeoJSON Polygon, MultiPolygon or Feature, or a ring of [longitude, latitude] positions."
    )
    weeks: int = Field(default=12, ge=1, le=52, description="Recent weeks to return.")
    years: int = Field(default=10, ge=0, le=23, description="Earlier years to compare at the same time of year.")

    @field_validator("polygon", mode="before")
    @classmethod
    def valid_polygon(cls, value):
        return CropHistoryRequest.valid_polygon(value)
