"""Text, tags and examples shown in Swagger UI at /docs.

Route logic lives in server.py; this module only describes it. Examples use real
identifiers from the committed Orillia preview, so "Try it out" works without a
full database build.
"""
from __future__ import annotations

from pydantic import BaseModel

VERSION = "0.2"

DESCRIPTION = """
Agricultural field outlines, field areas and environmental summaries for map-based applications.

### Typical flow
1. **Load fields for the visible map area** with `GET /fields?bbox=west,south,east,north`.
   Follow `nextCursor` while `hasMore` is true.
2. **Show a clicked field's area** with `GET /fields/{field_id}`.
3. **Summarise its environment** by posting the field's `geometry` to `POST /field-summary`.

### Conventions
- Coordinates are WGS84 degrees. GeoJSON positions are `[longitude, latitude]`.
- A bbox is `west,south,east,north`. Split boxes that cross the antimeridian into two requests.
- Errors return JSON `{"detail": ...}` with a 4xx or 5xx status.
- Responses over 1 KB are gzip-compressed when the client sends `Accept-Encoding: gzip`.
- Every response has an `X-Request-Id` header. Send your own (up to 128 letters, digits or `._:-`)
  to trace a request, and quote it when reporting a problem.
- Cross-origin browser calls are off unless the server sets `CORS_ORIGINS`. Server-side callers and same-origin proxies are unaffected.
- Missing or unavailable values are `null`, never zero.

### What the data is
Each outline is a **predicted agricultural field** from Fields of the World (Global 2nd Edition, 2025, CC-BY-4.0).
It is not a farm, an ownership parcel or a complete farm registry, and field IDs belong to one source edition.
Areas are estimated from outlines, not surveyed. Outlines carry no crop labels; what was planted each year comes
from national crop maps via the crop history routes.
"""

TAGS = [
    {"name": "Fields", "description": "Predicted agricultural field outlines and their estimated areas."},
    {"name": "Crops", "description": "What was planted in a field each year, from Canadian and US 30 m crop inventory maps."},
    {"name": "Environment", "description": "Climate, water balance, frost and terrain summaries from NASA data."},
    {"name": "Service", "description": "Liveness and readiness probes for monitoring and deployment."},
]

EXAMPLE_FIELD_ID = "ftw-2025-17TPK_0_0-13020"
EXAMPLE_BBOX = "-79.7,44.35,-79.05,44.85"


class ErrorResponse(BaseModel):
    detail: str


def sample(value, summary: str = "Example") -> dict:
    """A parameter example that Swagger UI fills in for Try it out."""
    return {"default": {"summary": summary, "value": value}}


def error(description: str, example: str) -> dict:
    return {"model": ErrorResponse, "description": description,
            "content": {"application/json": {"example": {"detail": example}}}}


def ok(description: str, example: dict) -> dict:
    return {"description": description, "content": {"application/json": {"example": example}}}


FIELDS_UNAVAILABLE = error("Field boundary data is missing or unreadable on the server.",
                           "Field boundary database is unavailable or invalid")

HEALTH = {
    "summary": "Liveness check",
    "description": "Answers 200 whenever the process is running. Use `/ready` to check that requests can be served.",
    "responses": {200: ok("The process is running.", {"status": "ok"})},
}

READY = {
    "summary": "Readiness check",
    "description": (
        "Answers 200 when the configured field data can be read and 503 when it cannot.\n\n"
        "NASA POWER and the terrain provider are **not** contacted, so their outages do not make the API unready. "
        "`source` is `database` for the indexed build or `geojson` for a file such as the Orillia preview."
    ),
    "responses": {
        200: ok("Field routes can answer.", {"ready": True, "checks": {"fields": "ok", "source": "database"}}),
        503: ok("Field data is unavailable; field routes will fail.", {"ready": False, "checks": {"fields": "unavailable", "source": "database"}}),
    },
}

FIELDS = {
    "summary": "Field outlines in a map area",
    "description": (
        "Returns a GeoJSON FeatureCollection of predicted field outlines that intersect `bbox`, ready to add to a map. "
        "Outlines are complete, so they can extend beyond the box.\n\n"
        "**Pagination:** while `hasMore` is true, request again with the same `bbox` and `limit` and with "
        "`cursor` set to `nextCursor`. When the map moves, start again from `cursor=0`. Cursors are positions in "
        "the current database and are not stable across rebuilds.\n\n"
        "Each feature has `id` and, for the indexed source, `properties.score`: the 0–100 mean model score. "
        "It is not a calibrated probability. Outlines carry no crop labels; use `/fields/{field_id}/crop-history`.\n\n"
        "Coordinates are cut to 6 decimal places (about 0.1 m), well below the outlines' own accuracy. "
        "The response is gzip-compressed for clients that send `Accept-Encoding: gzip`."
    ),
    "responses": {
        200: ok("A page of field outlines.", {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature", "id": EXAMPLE_FIELD_ID,
                "properties": {"id": EXAMPLE_FIELD_ID, "score": 80},
                "geometry": {"type": "Polygon", "coordinates": [[
                    [-79.159, 44.349], [-79.150, 44.349], [-79.150, 44.355], [-79.159, 44.355], [-79.159, 44.349]]]},
            }],
            "numberReturned": 1, "hasMore": True, "nextCursor": 1234,
        }),
        422: error("The bbox is malformed or out of range.", "bbox must be west,south,east,north within longitude/latitude ranges"),
        503: FIELDS_UNAVAILABLE,
    },
}

BBOX_DESCRIPTION = ("Visible map area as `west,south,east,north` in degrees. "
                    "If omitted, defaults to the Orillia, Ontario area.")
LIMIT_DESCRIPTION = "Maximum features per page (1–2000, default 50). Map clients usually ask for more per request."
CURSOR_DESCRIPTION = "`nextCursor` from the previous page. Use 0 for the first page, and keep the same bbox and limit."

FIELD = {
    "summary": "Estimated area of one field",
    "description": (
        "Returns the estimated area of a field selected on the map, in hectares and acres, rounded to two decimals. "
        "The area is computed from the outline, subtracting any holes. It is an estimate, not surveyed acreage."
    ),
    "responses": {
        200: ok("The field's estimated area.", {"id": EXAMPLE_FIELD_ID, "hectares": 42.72, "acres": 105.57}),
        404: error("No field has this ID in the loaded data.", "Field not found"),
        503: FIELDS_UNAVAILABLE,
    },
}

FIELD_ID_DESCRIPTION = "A feature `id` from `/fields`. IDs belong to one source edition and are not stable across years."

COVERAGE = {
    "summary": "Loaded coverage and build status",
    "description": (
        "Describes the field data the server has loaded: region, bounding box, field count, source, licence, and "
        "whether loading is still in progress.\n\n"
        "`status` reports the build state, for example `complete`, `building`, `stopped`, or `sample` for the small preview. "
        "While data is still loading, `ingestion` reports progress, and new fields appear in `/fields` without a restart."
    ),
    "responses": {
        200: ok("Coverage of the loaded field data.", {
            "region": "North America (Canada, United States, Mexico)", "status": "building",
            "bbox": [-95.16, 41.68, -74.34, 56.86], "fieldCount": 413393,
            "source": "Fields of the World, Global 2nd Edition, 2025",
            "sourceUrl": "https://source.coop/ftw/global-data-2e", "license": "CC-BY-4.0",
            "description": "Predicted agricultural field units; not farm ownership parcels or an exhaustive farm registry.",
            "ingestion": {"status": "running", "totalTiles": 371, "completedTiles": 9},
        }),
        503: FIELDS_UNAVAILABLE,
    },
}

SUMMARY = {
    "summary": "Environmental summary of a field",
    "description": (
        "Post a field's `geometry` from `/fields` to get its climate, seasonal water balance, frost dates, monthly "
        "climate averages, data coverage and terrain, with source metadata. For crops, use the crop history routes.\n\n"
        "**Defaults:** years 2015–2024; growing season May–September north of the equator, November–March south of it. "
        "Dates accept `YYYY-MM-DD` or `YYYYMMDD`, must fall between 1981 and today, and may span at most 45 years. "
        "Season months must be consecutive and may cross December.\n\n"
        "**Resolution and methods:**\n"
        "- Weather is NASA POWER daily data on a grid of about 50 km. `weatherGridDeg` only chooses the sampling "
        "coordinate; it does not make the weather finer.\n"
        "- Reference evapotranspiration uses Hargreaves (FAO-56). It estimates ET0, not actual crop water use.\n"
        "- Growing-degree days use a 10 °C base.\n"
        "- A year or season needs at least 90% valid days to count; otherwise the statistic is `null`.\n"
        "- Monthly values are averages over the requested years, not a dated time series.\n"
        "- Terrain is NASA SRTM 30 m elevation sampled within the outline. Slope and erosion ratings are approximate.\n\n"
        "**Partial results:** if terrain fails, `terrain` is `null` and `layers.terrain` explains why; weather is still "
        "returned. If weather fails, the request returns 502. The first request for a place can take several seconds."
    ),
    "responses": {
        422: {"description": "Invalid polygon, dates, season or unknown request property, or no usable weather for the period."},
        502: error("NASA POWER weather could not be reached or returned unusable data.",
                   "NASA POWER daily weather is unavailable or invalid"),
    },
}

DAILY_POINT = {
    "summary": "Raw NASA POWER daily data for a point",
    "description": (
        "Proxies the NASA POWER daily point API and returns its JSON unchanged. Use it for variables or dates that "
        "`/field-summary` does not cover.\n\n"
        "`parameters` is a comma-separated list of POWER variable codes, for example `T2M` (2 m air temperature, °C) "
        "and `PRECTOTCORR` (precipitation, mm/day). Values are on POWER's grid of about 50 km. "
        "This route needs Earthdata credentials configured on the server."
    ),
    "responses": {
        401: error("Earthdata credentials are missing or rejected on the server.", "Earthdata login failed (HTTP 401)"),
        422: {"description": "Coordinates out of range or dates not in YYYYMMDD form."},
        502: {"model": ErrorResponse, "description": "NASA POWER could not be reached or returned an error."},
    },
}

CROP_HISTORY_TEXT = (
    "One entry per year with the class that covers most of the field, its `share` of the field (0–1), and up to five "
    "classes covering at least 1% each. `rotation` lists the main class per year; `distinctCrops` and `cropYears` "
    "count only crop classes, not grass, forest, water or developed land.\n\n"
    "**Sources** (30 m, no account): Canada uses the AAFC Annual Crop Inventory (Ontario from 2011, other provinces "
    "from 2009). The United States uses the USDA NASS Cropland Data Layer (from 2008). The most recent published year "
    "is picked up automatically. Class names are each provider's own, so they differ between countries "
    "(for example `Corn for grain` and `Corn`).\n\n"
    "**Limits:** these are satellite classifications. Any one year can be wrong, most often at field edges and for less "
    "common crops, and a low `share` usually means mixed pixels or an outline that has changed. Outside Canada and the "
    "United States, including Mexico, `years` is empty and `source` is null. Years a provider fails to return are "
    "listed in `unavailableYears`. The first request for a field takes about a second; repeats are cached."
)

_CROP_EXAMPLE = {
    "source": {"name": "AAFC Annual Crop Inventory", "url": "https://open.canada.ca/data/en/dataset/ba2645d5-4458-414d-b196-6303ac06c1c9",
               "license": "Open Government Licence - Canada", "resolutionM": 30},
    "years": [
        {"year": 2024, "crop": "Corn for grain", "code": 147, "isCrop": True, "share": 0.877, "classes": [
            {"code": 147, "name": "Corn for grain", "isCrop": True, "share": 0.877},
            {"code": 145, "name": "Winter wheat", "isCrop": True, "share": 0.06},
            {"code": 136, "name": "Oats", "isCrop": True, "share": 0.039}]},
        {"year": 2025, "crop": "Soybeans", "code": 158, "isCrop": True, "share": 0.967, "classes": [
            {"code": 158, "name": "Soybeans", "isCrop": True, "share": 0.967},
            {"code": 147, "name": "Corn for grain", "isCrop": True, "share": 0.029}]},
    ],
    "rotation": ["Corn for grain", "Soybeans"],
    "distinctCrops": ["Corn for grain", "Soybeans"],
    "cropYears": 2,
    "unavailableYears": [],
    "note": "Satellite classification: any single year can be wrong, especially at field edges.",
}

CROP_PROVIDER_ERROR = error("The crop map service could not be reached.", "AAFC Annual Crop Inventory is unavailable")

CROP_HISTORY_FIELD = {
    "summary": "Crop history of a field",
    "description": "What was planted in a field from `/fields`, year by year.\n\n" + CROP_HISTORY_TEXT,
    "responses": {
        200: ok("Year-by-year crop classes for the field (example trimmed to two years).", {"id": EXAMPLE_FIELD_ID, **_CROP_EXAMPLE}),
        404: error("No field has this ID in the loaded data.", "Field not found"),
        502: CROP_PROVIDER_ERROR,
        503: FIELDS_UNAVAILABLE,
    },
}

CROP_HISTORY = {
    "summary": "Crop history of any outline",
    "description": ("What was planted inside any outline, year by year. Use it for areas drawn by the user; for fields "
                    "from `/fields`, `GET /fields/{field_id}/crop-history` avoids sending the geometry.\n\n" + CROP_HISTORY_TEXT),
    "responses": {
        200: ok("Year-by-year crop classes for the outline (example trimmed to two years).", _CROP_EXAMPLE),
        422: {"description": "Invalid polygon or unknown request property."},
        502: CROP_PROVIDER_ERROR,
    },
}
