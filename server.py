"""Field selection and environmental summary API. Swagger UI is served at /docs."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse, RedirectResponse, Response

import api_docs as docs
import field_info
import field_store
import middleware
import smap
from app import EarthdataError, EarthdataSession, fetch_daily_point, load_credentials
from crop_history import CropHistoryRequest, FieldCropHistoryBatchRequest, GreennessRequest
from environmental import crops, greenness
from environmental.errors import DataError
from field_summary import FieldSummaryRequest, summarize_field
from fields import acreage, field_map, load_fields, parse_bbox

GEOJSON = "application/json"  # GeoJSON body; plain JSON type so every client parses it as before

app = FastAPI(title="SpaceApps 2026 Data API", version=docs.VERSION, description=docs.DESCRIPTION, openapi_tags=docs.TAGS)
middleware.install(app)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/health", tags=["Service"], **docs.HEALTH)
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready", tags=["Service"], **docs.READY)
def ready() -> JSONResponse:
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


@app.get("/daily-point", tags=["Environment"], **docs.DAILY_POINT)
def daily_point(
    lat: float = Query(..., ge=-90, le=90, description="Latitude in degrees.", openapi_examples=docs.sample(44.6)),
    lon: float = Query(..., ge=-180, le=180, description="Longitude in degrees.", openapi_examples=docs.sample(-79.42)),
    start: str = Query(..., pattern=r"^\d{8}$", description="First day, YYYYMMDD.", openapi_examples=docs.sample("20240101")),
    end: str = Query(..., pattern=r"^\d{8}$", description="Last day, YYYYMMDD.", openapi_examples=docs.sample("20240107")),
    parameters: str = Query("T2M,PRECTOTCORR", description="Comma-separated NASA POWER variable codes."),
) -> dict:
    try:
        session = EarthdataSession(*load_credentials())
        return fetch_daily_point(session, lat, lon, start, end, tuple(parameters.split(",")))
    except EarthdataError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


DEFAULT_FIELDS_DB = Path(__file__).with_name("data") / "fields.sqlite"


@app.get("/soil-moisture", tags=["Environment"], **docs.SOIL_MOISTURE)
def soil_moisture() -> dict:
    try:
        return smap.get_layer()
    except EarthdataError as exc:
        raise HTTPException(status_code=503, detail="Configure EARTHDATA_USERNAME and EARTHDATA_PASSWORD on the API host to fetch SMAP data") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="NASA SMAP request failed; retry later") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="SMAP layer could not be acquired or read; check dependencies, cache permissions, and Earthdata access on the API host") from exc


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


@app.get("/fields", tags=["Fields"], response_class=Response, **docs.FIELDS)
def list_fields(
    bbox: str | None = Query(None, description=docs.BBOX_DESCRIPTION, openapi_examples=docs.sample(docs.EXAMPLE_BBOX)),
    limit: int = Query(50, ge=1, le=2000, description=docs.LIMIT_DESCRIPTION),
    cursor: int = Query(0, ge=0, description=docs.CURSOR_DESCRIPTION),
) -> Response:
    try:
        bounds = parse_bbox(bbox) if bbox is not None else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    database = _database()
    if database is not None:
        try:
            body = field_store.list_features(database, bounds or field_store.ORILLIA_BBOX, limit, cursor)
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HTTPException(status_code=503, detail="Field boundary database is unavailable or invalid") from exc
        return Response(body, media_type=GEOJSON)
    features = field_map(_fields(), bounds)["features"]
    page = features[cursor:cursor + limit]
    more = len(features) > cursor + limit
    facts = field_info.describe_many(page)
    body = field_store.feature_collection(
        [field_store.with_facts(field_store.compact(json.dumps(f, separators=(",", ":")).encode()), fact)
         for f, fact in zip(page, facts, strict=True)],
        numberReturned=len(page), hasMore=more, nextCursor=cursor + limit if more else None)
    return Response(body, media_type=GEOJSON)


@app.get("/fields/{field_id}", tags=["Fields"], **docs.FIELD)
def selected_field(field_id: str = PathParam(description=docs.FIELD_ID_DESCRIPTION, openapi_examples=docs.sample(docs.EXAMPLE_FIELD_ID))) -> dict:
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


@app.get("/field-coverage", tags=["Fields"], **docs.COVERAGE)
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


@app.post("/field-summary", tags=["Environment"], **docs.SUMMARY)
def field_summary(request: FieldSummaryRequest) -> dict:
    try:
        return summarize_field(request)
    except DataError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _field_geometry(field_id: str) -> dict | None:
    database = _database()
    if database is not None:
        try:
            feature = field_store.feature(database, field_id)
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HTTPException(status_code=503, detail="Field boundary database is unavailable or invalid") from exc
    else:
        feature = _fields().get(field_id)
    return None if feature is None else feature["geometry"]


def _crop_history(polygon) -> dict:
    try:
        return crops.history(polygon)
    except DataError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _field_geometries(field_ids: list[str]) -> dict[str, dict]:
    database = _database()
    if database is not None:
        try:
            found = field_store.features(database, field_ids)
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HTTPException(status_code=503, detail="Field boundary database is unavailable or invalid") from exc
    else:
        loaded = _fields()
        found = {fid: loaded[fid] for fid in field_ids if fid in loaded}
    return {fid: feature["geometry"] for fid, feature in found.items()}


@app.post("/fields/crop-history", tags=["Crops"], **docs.CROP_HISTORY_BATCH)
def fields_crop_history(request: FieldCropHistoryBatchRequest) -> dict:
    polygons = _field_geometries(request.ids)
    histories = crops.history_many(polygons)
    results = []
    for fid in request.ids:
        outcome = histories.get(fid)
        if outcome is None:
            results.append({"id": fid, "status": "error", "error": {"code": 404, "detail": "Field not found"}})
        elif isinstance(outcome, DataError):
            results.append({"id": fid, "status": "error", "error": {"code": outcome.status, "detail": str(outcome)}})
        else:
            results.append({"id": fid, "status": "ok", "history": outcome})
    succeeded = sum(r["status"] == "ok" for r in results)
    return {"results": results, "requested": len(results), "succeeded": succeeded, "failed": len(results) - succeeded}


@app.get("/fields/{field_id}/crop-history", tags=["Crops"], **docs.CROP_HISTORY_FIELD)
def field_crop_history(field_id: str = PathParam(description=docs.FIELD_ID_DESCRIPTION, openapi_examples=docs.sample(docs.EXAMPLE_FIELD_ID))) -> dict:
    polygon = _field_geometry(field_id)
    if polygon is None:
        raise HTTPException(status_code=404, detail="Field not found")
    return {"id": field_id, **_crop_history(polygon)}


@app.post("/crop-history", tags=["Crops"], **docs.CROP_HISTORY)
def crop_history(request: CropHistoryRequest) -> dict:
    return _crop_history(request.polygon)


def _greenness(polygon, year: int | None) -> dict:
    try:
        return greenness.season(polygon, year)
    except DataError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.get("/fields/{field_id}/greenness", tags=["Crops"], **docs.GREENNESS_FIELD)
def field_greenness(
    field_id: str = PathParam(description=docs.FIELD_ID_DESCRIPTION, openapi_examples=docs.sample(docs.EXAMPLE_FIELD_ID)),
    year: int | None = Query(None, description=docs.GREENNESS_YEAR_DESCRIPTION, openapi_examples=docs.sample(2025)),
) -> dict:
    polygon = _field_geometry(field_id)
    if polygon is None:
        raise HTTPException(status_code=404, detail="Field not found")
    return {"id": field_id, **_greenness(polygon, year)}


@app.post("/greenness", tags=["Crops"], **docs.GREENNESS)
def outline_greenness(request: GreennessRequest) -> dict:
    return _greenness(request.polygon, request.year)
