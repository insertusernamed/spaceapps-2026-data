"""Request model for crop history by outline; the lookups live in environmental.crops."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
