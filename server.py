"""HTTP wrapper around the NASA POWER client. Swagger UI is served at /docs."""
from __future__ import annotations

import os
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Query

from app import EarthdataError, EarthdataSession, fetch_daily_point, load_credentials
from environmental.errors import DataError
from field_summary import FieldSummaryRequest, summarize_field
from fields import acreage, field_map, load_fields, parse_bbox

app = FastAPI(title="SpaceApps 2026 Data API", version="0.1")


@app.get("/health", summary="Liveness check")
def health() -> dict:
    return {"status": "ok"}


@app.get("/daily-point", summary="NASA POWER daily point data")
def daily_point(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    start: str = Query(..., pattern=r"^\d{8}$", description="YYYYMMDD"),
    end: str = Query(..., pattern=r"^\d{8}$", description="YYYYMMDD"),
    parameters: str = Query("T2M,PRECTOTCORR"),
) -> dict:
    try:
        session = EarthdataSession(*load_credentials())
        return fetch_daily_point(session, lat, lon, start, end, tuple(parameters.split(",")))
    except EarthdataError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


DEFAULT_FIELDS_FILE = Path(__file__).with_name("data") / "fields.geojson"


def _fields() -> dict:
    try:
        return load_fields(Path(os.environ.get("FIELDS_FILE", DEFAULT_FIELDS_FILE)))
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise HTTPException(status_code=503, detail="Field boundary data is unavailable or invalid") from exc


@app.get("/fields", summary="Field outlines for the map (no crop information)")
def list_fields(
    bbox: str | None = Query(None, description="west,south,east,north in WGS84 degrees; filters by bounding-box intersection"),
) -> dict:
    try:
        bounds = parse_bbox(bbox) if bbox is not None else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return field_map(_fields(), bounds)


@app.get("/fields/{field_id}", summary="Estimated hectares and acres of a selected field")
def selected_field(field_id: str) -> dict:
    feature = _fields().get(field_id)
    if feature is None:
        raise HTTPException(status_code=404, detail="Field not found")
    return acreage(feature)


@app.post('/field-summary', summary='Climate, water balance, frost and terrain for a selected field')
def field_summary(request: FieldSummaryRequest) -> dict:
    try:
        return summarize_field(request)
    except DataError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
