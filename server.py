"""Field selection and environmental summary API. Swagger UI is served at /docs."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date
from typing import Literal
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
from crop_history import (AgroWeatherRequest, CropCalendarRequest, CropHistoryRequest, DroughtIndicatorsRequest, FieldBatchRequest,
                          FieldSeasonRequest, GreennessRequest, ImageryRequest, SoilRequest, VegetationHistoryRequest)
from environmental import agro_weather, crop_calendar, crops, drought, greenness, hls, imagery, soil, vegetation_history
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


def _batch_response(ids: list[str], outcomes: dict, payload_key: str) -> dict:
    """One result per requested ID, in order: ok with its payload, or error (404 unknown field, provider status)."""
    results = []
    for fid in ids:
        outcome = outcomes.get(fid)
        if outcome is None:
            results.append({"id": fid, "status": "error", "error": {"code": 404, "detail": "Field not found"}})
        elif isinstance(outcome, DataError):
            results.append({"id": fid, "status": "error", "error": {"code": outcome.status, "detail": str(outcome)}})
        else:
            results.append({"id": fid, "status": "ok", payload_key: outcome})
    succeeded = sum(r["status"] == "ok" for r in results)
    return {"results": results, "requested": len(results), "succeeded": succeeded, "failed": len(results) - succeeded}


@app.post("/fields/crop-history", tags=["Crops"], **docs.CROP_HISTORY_BATCH)
def fields_crop_history(request: FieldBatchRequest) -> dict:
    return _batch_response(request.ids, crops.history_many(_field_geometries(request.ids)), "history")


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


def _soil(polygon) -> dict:
    try:
        return soil.field_soil(polygon)
    except DataError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.post("/fields/soil", tags=["Soil"], **docs.SOIL_BATCH)
def fields_soil(request: FieldBatchRequest) -> dict:
    return _batch_response(request.ids, soil.soil_many(_field_geometries(request.ids)), "soil")


@app.get("/fields/{field_id}/soil", tags=["Soil"], **docs.SOIL_FIELD)
def field_soil(field_id: str = PathParam(description=docs.FIELD_ID_DESCRIPTION, openapi_examples=docs.sample(docs.EXAMPLE_FIELD_ID))) -> dict:
    polygon = _field_geometry(field_id)
    if polygon is None:
        raise HTTPException(status_code=404, detail="Field not found")
    return {"id": field_id, **_soil(polygon)}


@app.post("/soil", tags=["Soil"], **docs.SOIL)
def outline_soil(request: SoilRequest) -> dict:
    return _soil(request.polygon)


def _imagery(polygon, start, end, max_cloud, limit, dataset="hls") -> dict:
    try:
        return imagery.images(polygon, start, end, max_cloud, limit, dataset)
    except DataError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.get("/fields/{field_id}/imagery", tags=["Imagery"], **docs.IMAGERY_FIELD)
def field_imagery(
    field_id: str = PathParam(description=docs.FIELD_ID_DESCRIPTION),
    dataset: Literal["hls", "sentinel-2"] = Query("hls", description="NASA HLS v2 at 30 m, or native Sentinel-2 RGB at 10 m."),
    start: date | None = Query(None, description="First acquisition date; defaults to one year before end."),
    end: date | None = Query(None, description="Last acquisition date; defaults to today."),
    maxCloud: float = Query(30, ge=0, le=100, description="Maximum whole-scene cloud percentage."),
    limit: int = Query(6, ge=1, le=20, description="Maximum number of matching satellite scenes to return."),
) -> dict:
    polygon = _field_geometry(field_id)
    if polygon is None:
        raise HTTPException(status_code=404, detail="Field not found")
    return {"id": field_id, **_imagery(polygon, start, end, maxCloud, limit, dataset)}


@app.post("/imagery", tags=["Imagery"], **docs.IMAGERY)
def outline_imagery(request: ImageryRequest) -> dict:
    return _imagery(request.polygon, request.start, request.end, request.maxCloud, request.limit, request.dataset)


@app.get("/fields/{field_id}/season", tags=["Imagery"], summary="Incremental HLS measurements for a field",
         description="Starts or polls a cached acquisition job. Returns native 30 m field measurements and QA fractions; crop interpretation and display quality belong to Java.")
def field_season(
    field_id: str = PathParam(description=docs.FIELD_ID_DESCRIPTION),
    start: date = Query(..., description="First acquisition date, inclusive."),
    end: date = Query(..., description="Last acquisition date, inclusive."),
    retry: bool = Query(False, description="Retry a terminal acquisition after failed scene reads."),
) -> dict:
    polygon = _field_geometry(field_id)
    if polygon is None:
        raise HTTPException(status_code=404, detail="Field not found")
    try:
        return {"id": field_id, **hls.season(polygon, start, end, identity=field_id, retry=retry)}
    except DataError as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from error


@app.post("/field-season", tags=["Imagery"], summary="Incremental HLS measurements for an outline",
          description="Acquires QA and numerical HLS observations incrementally over an explicit period. Repeat the same request to poll; fields share cached native raster blocks.")
def outline_season(request: FieldSeasonRequest) -> dict:
    try:
        return hls.season(request.polygon, request.start, request.end, retry=request.retry)
    except DataError as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from error


def _agro_weather(polygon, years: int) -> dict:
    try:
        return agro_weather.summary(polygon, years)
    except DataError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.get("/fields/{field_id}/agro-weather", tags=["Environment"], **docs.AGRO_WEATHER_FIELD)
def field_agro_weather(
    field_id: str = PathParam(description=docs.FIELD_ID_DESCRIPTION, openapi_examples=docs.sample(docs.EXAMPLE_FIELD_ID)),
    years: int = Query(20, ge=5, le=45, description="Years of history to read, ending today."),
) -> dict:
    polygon = _field_geometry(field_id)
    if polygon is None:
        raise HTTPException(status_code=404, detail="Field not found")
    return {"id": field_id, **_agro_weather(polygon, years)}


@app.post("/agro-weather", tags=["Environment"], **docs.AGRO_WEATHER)
def outline_agro_weather(request: AgroWeatherRequest) -> dict:
    return _agro_weather(request.polygon, request.years)


def _crop_calendar(polygon, system: str, wanted: list[str] | None) -> dict:
    try:
        return crop_calendar.calendar(polygon, system, wanted)
    except DataError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.get("/fields/{field_id}/crop-calendar", tags=["Crops"], **docs.CROP_CALENDAR_FIELD)
def field_crop_calendar(
    field_id: str = PathParam(description=docs.FIELD_ID_DESCRIPTION, openapi_examples=docs.sample(docs.EXAMPLE_FIELD_ID)),
    system: Literal["rainfed", "irrigated"] = Query("rainfed", description="Rainfed or irrigated farming."),
    crops: str | None = Query(None, description="Comma-separated calendar crop keys; all crops when omitted.",
                              openapi_examples=docs.sample("corn,soybeans,winter_wheat")),
) -> dict:
    polygon = _field_geometry(field_id)
    if polygon is None:
        raise HTTPException(status_code=404, detail="Field not found")
    wanted = [c.strip() for c in crops.split(",") if c.strip()] if crops else None
    return {"id": field_id, **_crop_calendar(polygon, system, wanted)}


@app.post("/crop-calendar", tags=["Crops"], **docs.CROP_CALENDAR)
def outline_crop_calendar(request: CropCalendarRequest) -> dict:
    return _crop_calendar(request.polygon, request.system, request.crops)


def _vegetation_history(polygon, years: int) -> dict:
    try:
        return vegetation_history.history(polygon, years)
    except DataError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.get("/fields/{field_id}/vegetation-history", tags=["Crops"], **docs.VEGETATION_HISTORY_FIELD)
def field_vegetation_history(
    field_id: str = PathParam(description=docs.FIELD_ID_DESCRIPTION, openapi_examples=docs.sample(docs.EXAMPLE_FIELD_ID)),
    years: int = Query(5, ge=1, le=10, description="Years of 16-day NDVI to read, ending today."),
) -> dict:
    polygon = _field_geometry(field_id)
    if polygon is None:
        raise HTTPException(status_code=404, detail="Field not found")
    return {"id": field_id, **_vegetation_history(polygon, years)}


@app.post("/vegetation-history", tags=["Crops"], **docs.VEGETATION_HISTORY)
def outline_vegetation_history(request: VegetationHistoryRequest) -> dict:
    return _vegetation_history(request.polygon, request.years)


def _drought(polygon, weeks: int, years: int) -> dict:
    try:
        return drought.indicators(polygon, weeks, years)
    except EarthdataError as exc:
        raise HTTPException(status_code=503, detail="Configure EARTHDATA_USERNAME and EARTHDATA_PASSWORD on the API host to fetch GRACE data") from exc
    except DataError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.get("/fields/{field_id}/drought-indicators", tags=["Environment"], **docs.DROUGHT_FIELD)
def field_drought_indicators(
    field_id: str = PathParam(description=docs.FIELD_ID_DESCRIPTION, openapi_examples=docs.sample(docs.EXAMPLE_FIELD_ID)),
    weeks: int = Query(12, ge=1, le=52, description="Recent weeks to return."),
    years: int = Query(10, ge=0, le=23, description="Earlier years to compare at the same time of year."),
) -> dict:
    polygon = _field_geometry(field_id)
    if polygon is None:
        raise HTTPException(status_code=404, detail="Field not found")
    return {"id": field_id, **_drought(polygon, weeks, years)}


@app.post("/drought-indicators", tags=["Environment"], **docs.DROUGHT)
def outline_drought_indicators(request: DroughtIndicatorsRequest) -> dict:
    return _drought(request.polygon, request.weeks, request.years)
