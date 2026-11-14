"""HTTP wrapper around the NASA POWER client. Swagger UI is served at /docs."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

import field_store
from app import EarthdataError, EarthdataSession, fetch_daily_point, load_credentials
from environmental.errors import DataError
from field_summary import FieldSummaryRequest, summarize_field
from fields import acreage, field_map, load_fields, parse_bbox

app = FastAPI(title="SpaceApps 2026 Data API", version="0.1")


@app.get("/health", summary="Liveness check")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready", summary="Readiness check", responses={503: {"description": "Field boundary data is unavailable"}})
def ready() -> JSONResponse:
    """Report whether the configured field boundary data can be read.

    Returns 200 when field routes can answer and 503 when they cannot. NASA POWER and
    terrain providers are not contacted, so upstream outages do not affect readiness.
    """
    database = _database()
    source = "database" if database is not None else "geojson"
    try:
        if database is not None:
            field_store.readable(database)
        else:
            load_fields(Path(os.environ.get("FIELDS_FILE", DEFAULT_FIELDS_FILE)))
        status = "ok"
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, IndexError):
        status = "unavailable"
    ok = status == "ok"
    return JSONResponse({"ready": ok, "checks": {"fields": status, "source": source}}, status_code=200 if ok else 503)


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


DEFAULT_FIELDS_DB = Path(__file__).with_name("data") / "fields.sqlite"


DEFAULT_FIELDS_FILE = Path(__file__).with_name("data") / "fields.geojson"


def _fields() -> dict:
    try:
        return load_fields(Path(os.environ.get("FIELDS_FILE", DEFAULT_FIELDS_FILE)))
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise HTTPException(status_code=503, detail="Field boundary data is unavailable or invalid") from exc


def _database() -> Path | None:
    if "FIELDS_DB" in os.environ:
        return Path(os.environ["FIELDS_DB"])
    if "FIELDS_FILE" not in os.environ and DEFAULT_FIELDS_DB.exists():
        return DEFAULT_FIELDS_DB
    return None


@app.get("/fields", summary="Field outlines in a map viewport, with pagination")
def list_fields(
    bbox: str | None = Query(None, description="west,south,east,north; defaults to the Orillia area for indexed coverage"),
    limit: int = Query(500, ge=1, le=2000),
    cursor: int = Query(0, ge=0, description="nextCursor from the previous page; retain the same bbox"),
) -> dict:
    try:
        bounds = parse_bbox(bbox) if bbox is not None else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    database = _database()
    if database is not None:
        try:
            return field_store.list_features(database, bounds or field_store.ORILLIA_BBOX, limit, cursor)
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HTTPException(status_code=503, detail="Field boundary database is unavailable or invalid") from exc
    features = field_map(_fields(), bounds)["features"]
    page = features[cursor:cursor + limit]
    more = len(features) > cursor + limit
    return {"type": "FeatureCollection", "features": page, "numberReturned": len(page),
            "hasMore": more, "nextCursor": cursor + limit if more else None}


@app.get("/fields/{field_id}", summary="Estimated hectares and acres of a selected field")
def selected_field(field_id: str) -> dict:
    database = _database()
    if database is not None:
        try:
            result = field_store.selected(database, field_id)
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(status_code=503, detail="Field boundary database is unavailable or invalid") from exc
    else:
        feature = _fields().get(field_id)
        result = acreage(feature) if feature is not None else None
    if result is None:
        raise HTTPException(status_code=404, detail="Field not found")
    return result


@app.get("/field-coverage", summary="Loaded field coverage, source and build status")
def field_coverage() -> dict:
    database = _database()
    if database is not None:
        try:
            return field_store.coverage(database)
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HTTPException(status_code=503, detail="Field boundary database is unavailable or invalid") from exc
    return {"region": "custom GeoJSON" if "FIELDS_FILE" in os.environ else "Orillia preview",
            "status": "sample", "fields": len(_fields()),
            "description": "Limited sample; build the indexed Ontario dataset for province-wide coverage.",
            **({} if "FIELDS_FILE" in os.environ else {
                "source": "Fields of the World, Global 2nd Edition, 2025",
                "sourceUrl": "https://source.coop/ftw/global-data-2e", "license": "CC-BY-4.0"})}


@app.post('/field-summary', summary='Climate, water balance, frost and terrain for a selected field')
def field_summary(request: FieldSummaryRequest) -> dict:
    try:
        return summarize_field(request)
    except DataError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
