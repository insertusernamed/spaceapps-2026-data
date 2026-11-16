"""Text, tags and examples shown in Swagger UI at /docs.

Route logic lives in server.py; this module only describes it. Examples use real
identifiers from the committed Orillia preview, so "Try it out" works without a
full database build.
"""
from __future__ import annotations

from pydantic import BaseModel

VERSION = "0.2"

IMAGERY_TEXT = (
    "NASA HLS v2 RGB images (L30 and S30) via Microsoft Planetary Computer, no credentials. "
    "Returns imageUrl links rendered by the provider, acquisition datetime, sceneCloudCoverPct and WGS84 bbox. "
    "Images include context around the field (its full span per edge, at least 180 m), not masked to the polygon. Quality checks still use only the field. Top-level bbox is the padded image crop; images[].sceneBbox is the original scene extent. Crops are capped at 1024 pixels; small crops are not enlarged. "
    "Default dataset=hls has 30 m pixels; dataset=sentinel-2 selects native 10 m RGB. HLS harmonizes observations across sensors; it does not increase spatial detail. maxCloud first filters whole-scene cloud cover. Field-level Fmask (HLS) or SCL (Sentinel-2) checks then require at least 90% valid RGB pixels and 90% clear pixels inside the polygon, excluding clouds, shadows, snow and fill. QA is sampled at native resolution, capped at 1024 pixels per side. Images are not quality-masked. "
    "Default 20 images, maximum 20. Image slots are allocated across calendar years; scenes within each year are sampled across dates and checked for quality. Up to eight candidate scenes per year are checked. Years with no passing sampled image or unavailable quality data are reported. Results are listed newest first. "
    "An empty images list means no sampled scene passed. hasMore includes unchecked candidates; narrow the date range for a deeper search. All checked quality assets being unreadable returns 502. "
    "Dates default to the full hosted archive: HLS from 2020-01-01, native Sentinel-2 from 2015-06-27, through today. Explicit multi-year date ranges are supported. Actual clear-image coverage varies by field. "
    "Outlines must fit within 1 by 1 degree. Image rendering availability depends on the external provider."
)
IMAGERY = {
    "summary": "HLS or Sentinel-2 images for an outline", "description": IMAGERY_TEXT,
    "responses": {422: {"description": "Invalid outline, dates or search options."},
                  502: {"description": "Imagery catalogue or field quality assets unavailable or invalid."}},
}
IMAGERY_FIELD = {
    **IMAGERY, "summary": "HLS or Sentinel-2 images for a field",
    "responses": {**IMAGERY["responses"], 404: {"description": "Field not found."},
                  503: {"description": "Field boundary data unavailable."}},
}

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
    {"name": "Imagery", "description": "NASA HLS v2 (30 m) or Sentinel-2 (10 m) RGB field images with dates and cloud cover."},
    {"name": "Fields", "description": "Predicted agricultural field outlines and their estimated areas."},
    {"name": "Crops", "description": "What was planted in a field each year, from Canadian and US 30 m crop inventory maps."},
    {"name": "Soil", "description": "Topsoil texture, pH, organic matter and water holding for fields, from SoilGrids."},
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


SOIL_MOISTURE = {
    "summary": "SMAP soil moisture map layer",
    "description": "Return SMAP V008 native 9 km cells as GeoJSON. Each feature includes surface/root zone/profile moisture, relative wetness, moisture percentiles, surface and six soil-layer temperatures (K), infiltration/runoff/baseflow/evapotranspiration rates (kg m-2 s-1), saturated/wilting land fractions, and static porosity/wilting point/clay/sand properties from science-version-matched SPL4SMLM. metadata.variables describes each field's units and source; missing values are null. The legacy metadata.units applies only to volumetric moisture. Ancillary dynamic outputs have different validation status from core moisture; static properties are model inputs. Missing caches stream the latest Orillia-area data through Earthaccess. Older caches upgrade while preserving the region/day. Initial requests need Earthdata credentials on the API host and can take longer; subsequent requests serve the saved layer.",
    "responses": {
        502: error("NASA request failed", "NASA SMAP request failed; retry later"),
        503: error("Layer or configuration unavailable", "Configure EARTHDATA_USERNAME and EARTHDATA_PASSWORD on the API host to fetch SMAP data"),
    },
}


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
        "**Field facts for the sidebar** travel with every outline in `properties`, so a client can show them on click "
        "without another request. All are computed from the outline itself:\n"
        "- `areaHa`, `areaAcres`: estimated area (outline minus holes), not surveyed.\n"
        "- `perimeterM`: boundary length in metres, including holes (fencing, headlands).\n"
        "- `lengthM`, `widthM`, `orientationDeg`: the smallest enclosing rectangle aligned with the field's main axis or "
        "longest edges. `orientationDeg` is the long side's bearing, 0–179° clockwise from north (0 = north–south, "
        "90 = east–west), a guide to working direction.\n"
        "- `compactness`: 4π·area/perimeter², from 0 to 1 (circle 1, square 0.79); low values mean irregular or narrow "
        "fields with more turning and edge.\n"
        "- `holes`: areas inside the outline that are not field (wet spots, trees, buildings); `parts`: separate pieces.\n"
        "- `centroid`: `[longitude, latitude]` of the field's centre, for labels.\n"
        "- `sourceYear`: edition of the boundary data.\n\n"
        "Each feature also has `id` and, for the indexed source, `properties.score`: the 0–100 mean model score. "
        "It is not a calibrated probability. Outlines carry no crop labels; use `/fields/{field_id}/crop-history`.\n\n"
        "Coordinates are cut to 6 decimal places (about 0.1 m), well below the outlines' own accuracy. "
        "The response is gzip-compressed for clients that send `Accept-Encoding: gzip`."
    ),
    "responses": {
        200: ok("A page of field outlines.", {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature", "id": EXAMPLE_FIELD_ID,
                "properties": {"areaHa": 42.72, "areaAcres": 105.56, "perimeterM": 4109, "lengthM": 1205, "widthM": 416,
                               "orientationDeg": 162, "compactness": 0.32, "holes": 0, "parts": 1,
                               "centroid": [-79.156222, 44.345239], "sourceYear": 2025, "id": EXAMPLE_FIELD_ID, "score": 80},
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
        "- **Weather source:** with `weatherSource: auto` (default), fields in North America use NASA Daymet at 1 km "
        "when the period ends by the last complete year; elsewhere, or for the current year, NASA POWER at about 50 km. "
        "`weatherSource` in the response says which was used, and if Daymet fails `layers.weather.fallbackFrom` "
        "explains the switch to POWER. Daymet samples the field's 0.01° (about 1 km) cell; POWER uses `weatherGridDeg`, "
        "which only chooses the sampling coordinate and does not make the weather finer. Repeat requests reuse downloads.\n"
        "- Reference evapotranspiration uses Hargreaves (FAO-56). It estimates ET0, not actual crop water use.\n"
        "- Growing-degree days use a 10 °C base.\n"
        "- `chu_available_median` is Ontario crop heat units, the scale used to rate corn and soybean varieties. Each year "
        "counts from three days averaging at least 12.8 °C (no earlier than May 1) to the first fall frost of −2 °C. "
        "It reflects the requested years, so recent periods read higher than older published CHU maps. Null south of "
        "the equator.\n"
        "- A year or season needs at least 90% valid days to count; otherwise the statistic is `null`.\n"
        "- Monthly values are averages over the requested years, not a dated time series.\n"
        "- `yearly.years` has one row per year (crop heat units, growing-degree days, season and annual rain, ET0, "
        "water deficit and stress, frost-free days, heat, dry-spell and heavy-rain days); season values belong to the "
        "season's start year and a value is `null` for a year without enough valid days. `yearly.percentiles` gives "
        "p10, p50 and p90 across valid years with the number of `years` used. Use these to ask how often a field "
        "reaches what a crop needs, rather than relying on the median alone.\n"
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

SUMMARY_FIELD = {
    "summary": "Environmental summary of a stored field",
    "description": (
        "The same summary as `POST /field-summary`, for a field from `/fields` by its ID, with the default years "
        "(2015–2024) and growing season. The response also has the field's `id`. Use the POST route to choose the "
        "period or season.\n\n" + SUMMARY["description"].split("\n\n", 1)[1]
    ),
    "responses": {
        404: error("No field has this ID in the loaded data.", "Field not found"),
        422: {"description": "Unknown weatherSource, the stored outline cannot be summarized, or no usable weather."},
        502: SUMMARY["responses"][502],
        503: FIELDS_UNAVAILABLE,
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
    "Returns acquired crop-map observations and source metadata. Application crop grouping, rotation inference "
    "and current-season estimates belong to the Java backend; this service does not return an inferred `summary`. "
    "`cropShare` is the share of mapped pixels assigned to crop classes. Cloud and unmapped pixels are excluded.\n\n"
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
        {"year": 2024, "crop": "Corn for grain", "code": 147, "isCrop": True, "share": 0.877, "cropShare": 0.986, "classes": [
            {"code": 147, "name": "Corn for grain", "isCrop": True, "share": 0.877},
            {"code": 145, "name": "Winter wheat", "isCrop": True, "share": 0.06},
            {"code": 136, "name": "Oats", "isCrop": True, "share": 0.039}]},
        {"year": 2025, "crop": "Soybeans", "code": 158, "isCrop": True, "share": 0.967, "cropShare": 0.996, "classes": [
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

CROP_HISTORY_BATCH = {
    "summary": "Crop history of several fields",
    "description": (
        "Crop history for up to 100 fields from `/fields` in one request, for example a box selection on the map. "
        "Outlines are read in one lookup and fields are processed in parallel, while the server keeps a fixed limit "
        "on requests to the crop map services. Cached fields return immediately; new ones take about a second each, "
        "shared across the batch.\n\n"
        "`results` follows the order of `ids`, with duplicates answered once. Each entry has `status` `ok` with the same "
        "`history` as `GET /fields/{field_id}/crop-history`, or `error` with an HTTP-style `code` and `detail`: 404 for "
        "an unknown ID, 502 when a crop map service failed for that field. A failed field never fails the others, so the "
        "response is 200 whenever the request itself is valid; check `failed`.\n\n" + CROP_HISTORY_TEXT
    ),
    "responses": {
        200: ok("One result per requested field (example history trimmed to two years).", {
            "results": [
                {"id": EXAMPLE_FIELD_ID, "status": "ok", "history": _CROP_EXAMPLE},
                {"id": "ftw-2025-17TPK_0_0-0", "status": "error", "error": {"code": 404, "detail": "Field not found"}},
            ],
            "requested": 2, "succeeded": 1, "failed": 1,
        }),
        422: {"description": "No IDs, more than 100 IDs, an empty ID, or an unknown request property."},
        503: FIELDS_UNAVAILABLE,
    },
}

GREENNESS_YEAR_DESCRIPTION = ("Growing season year, 2017 to this year. Defaults to the most recent season: this year from May, "
                              "otherwise last year.")

GREENNESS_TEXT = (
    "One entry per clear Sentinel-2 overpass (about every 2–5 days; fewer when cloudy): the field's mean NDVI and its "
    "10th and 90th percentiles across the field's 10 m pixels, with the share of the field that was clear. NDVI is the "
    "standard greenness index: about 0.1–0.2 for bare soil, 0.6–0.9 for a full crop canopy, falling again at maturity "
    "and harvest. A wide p10–p90 spread means an uneven field.\n\n"
    "**Season:** April 1 to November 15 (October 1 to May 15 south of the equator), up to today for the current year. "
    "An overpass counts when at least half the field is clear; cloud, shadow, snow and water pixels are removed.\n\n"
    "**Summary:** `peakNdvi` and `peakDate`, `latestNdvi` and `latestDate`, and `greenUpDate`, the first overpass halfway "
    "from the spring low to the peak (null when the season varies by less than 0.2, for example permanent grass).\n\n"
    "**Speed:** the first request for a field and season reads about 20–30 satellite scenes and takes about 10 seconds; "
    "repeats return at once (finished seasons are kept, the current one is refreshed every 6 hours). Show a loading "
    "state.\n\n"
    "**Source:** Copernicus Sentinel-2 L2A from Earth Search, no account. Collection 1 is used where available; seasons "
    "it lacks (2017, 2022) come from the original collection, named in `source.collection`."
)

_GREENNESS_EXAMPLE = {
    "year": 2025,
    "period": {"start": "2025-04-01", "end": "2025-11-14"},
    "observations": [
        {"date": "2025-07-14", "ndvi": 0.59, "ndviP10": 0.41, "ndviP90": 0.72, "clearShare": 1.0},
        {"date": "2025-08-30", "ndvi": 0.78, "ndviP10": 0.69, "ndviP90": 0.85, "clearShare": 1.0},
    ],
    "summary": {"peakNdvi": 0.78, "peakDate": "2025-08-30", "latestNdvi": 0.208, "latestDate": "2025-11-06",
                "greenUpDate": "2025-07-14"},
    "scenesChecked": 28,
    "scenesUnreadable": 0,
    "source": {"name": "Copernicus Sentinel-2 L2A via Earth Search", "collection": "sentinel-2-c1-l2a",
               "url": "https://earth-search.aws.element84.com/v1/search", "resolutionM": 10,
               "license": "Copernicus Sentinel data, free and open"},
    "note": "Mean NDVI over the clear part of the field per overpass; an overpass counts when at least half of the field is clear.",
}

GREENNESS_PROVIDER_ERROR = error("The Sentinel-2 catalogue or imagery could not be reached.",
                                 "Earth Search (Sentinel-2 catalogue) is unavailable")

GREENNESS_FIELD = {
    "summary": "Greenness of a field through a season",
    "description": "Sentinel-2 NDVI through one growing season for a field from `/fields`.\n\n" + GREENNESS_TEXT,
    "responses": {
        200: ok("Clear-sky NDVI observations and a season summary (example trimmed to two overpasses).",
                {"id": EXAMPLE_FIELD_ID, **_GREENNESS_EXAMPLE}),
        404: error("No field has this ID in the loaded data.", "Field not found"),
        422: {"description": "Year outside 2017 to this year."},
        502: GREENNESS_PROVIDER_ERROR,
        503: FIELDS_UNAVAILABLE,
    },
}

GREENNESS = {
    "summary": "Greenness of any outline through a season",
    "description": "Sentinel-2 NDVI through one growing season inside any outline.\n\n" + GREENNESS_TEXT,
    "responses": {
        200: ok("Clear-sky NDVI observations and a season summary (example trimmed to two overpasses).", _GREENNESS_EXAMPLE),
        422: {"description": "Invalid polygon, year outside 2017 to this year, or unknown request property."},
        502: GREENNESS_PROVIDER_ERROR,
    },
}

SOIL_TEXT = (
    "Topsoil estimates from ISRIC SoilGrids 2.0 at 250 m, averaged over every cell the field touches. These are "
    "machine-learning estimates, not a soil test; a farmer's own soil test should take precedence.\n\n"
    "- `topsoil`: the thickness-weighted 0–30 cm mean of pH (water), clay, sand and silt (%), organic carbon and "
    "total nitrogen (g/kg), bulk density (g/cm³) and cation exchange capacity (cmol(+)/kg).\n"
    "- `derived`: `textureClass` (USDA texture triangle), `organicMatterPct` (organic carbon × 1.724), `phClass` "
    "(USDA soil reaction class) and `availableWaterMm`, the plant-available water the top 30 cm can hold "
    "(field capacity minus wilting point, Saxton & Rawls 2006).\n"
    "- `depths`: the same properties for 0–5, 5–15 and 15–30 cm.\n\n"
    "Values are `null` over water, ice or built-up land. Cells are 250 m, so small fields share values with their "
    "surroundings. The first request for a field takes about 2 seconds; repeats return at once."
)

_SOIL_EXAMPLE = {
    "topsoil": {"ph": 6.82, "clayPct": 16.45, "sandPct": 52.31, "siltPct": 31.24, "organicCarbonGPerKg": 28.7,
                "nitrogenGPerKg": 2.76, "bulkDensityGPerCm3": 1.48, "cecCmolPerKg": 17.05},
    "derived": {"textureClass": "Sandy loam", "organicMatterPct": 4.9, "phClass": "Neutral", "availableWaterMm": 40},
    "depths": {"0-5cm": {"ph": 6.75, "clayPct": 15.4}, "5-15cm": {"ph": 6.8, "clayPct": 15.32}, "15-30cm": {"ph": 6.85, "clayPct": 17.6}},
    "units": {"ph": "pH", "clayPct": "%", "sandPct": "%", "siltPct": "%", "organicCarbonGPerKg": "g/kg", "nitrogenGPerKg": "g/kg",
              "bulkDensityGPerCm3": "g/cm3", "cecCmolPerKg": "cmol(+)/kg", "availableWaterMm": "mm in 0-30 cm"},
    "source": {"name": "ISRIC SoilGrids 2.0", "url": "https://soilgrids.org", "resolutionM": 250, "license": "CC BY 4.0"},
    "note": "Machine-learning estimates for 250 m cells, averaged over the cells the field touches; not a soil test.",
}

SOIL_PROVIDER_ERROR = error("SoilGrids could not be reached or read.", "SoilGrids is unavailable")

SOIL_FIELD = {
    "summary": "Topsoil of a field",
    "description": "Topsoil properties for a field from `/fields`.\n\n" + SOIL_TEXT,
    "responses": {
        200: ok("Topsoil estimates for the field (depths trimmed in this example).", {"id": EXAMPLE_FIELD_ID, **_SOIL_EXAMPLE}),
        404: error("No field has this ID in the loaded data.", "Field not found"),
        502: SOIL_PROVIDER_ERROR,
        503: FIELDS_UNAVAILABLE,
    },
}

SOIL_BATCH = {
    "summary": "Topsoil of several fields",
    "description": (
        "Topsoil for up to 100 fields from `/fields` in one request, for example a map selection. `results` follows the "
        "order of `ids`, with duplicates answered once. Each entry has `status` `ok` with `soil` in the same shape as "
        "`GET /fields/{field_id}/soil`, or `error` with a `code` (404 unknown field, 502 SoilGrids failure) and `detail`; "
        "one failed field never fails the others.\n\n" + SOIL_TEXT
    ),
    "responses": {
        200: ok("One result per requested field.", {
            "results": [{"id": EXAMPLE_FIELD_ID, "status": "ok", "soil": _SOIL_EXAMPLE},
                        {"id": "ftw-2025-17TPK_0_0-0", "status": "error", "error": {"code": 404, "detail": "Field not found"}}],
            "requested": 2, "succeeded": 1, "failed": 1}),
        422: {"description": "No IDs, more than 100 IDs, an empty ID, or an unknown request property."},
        503: FIELDS_UNAVAILABLE,
    },
}

SOIL = {
    "summary": "Topsoil of any outline",
    "description": "Topsoil properties inside any outline.\n\n" + SOIL_TEXT,
    "responses": {
        200: ok("Topsoil estimates for the outline (depths trimmed in this example).", _SOIL_EXAMPLE),
        422: {"description": "Invalid polygon or unknown request property."},
        502: SOIL_PROVIDER_ERROR,
    },
}

AGRO_WEATHER_TEXT = (
    "Soil wetness, solar radiation, wind, humidity and reference evapotranspiration around a field from NASA POWER "
    "(MERRA-2 and CERES, about 50 km), daily from 1981 to a few days ago. These are regional, modelled values, not "
    "field measurements.\n\n"
    "- `soilWetness.latest`: the newest root-zone and surface wetness (0 dry, 1 saturated), the mean of the latest 30 "
    "days, and its `percentile` against the same 30 days in earlier years, with `status` `dry` (20th percentile or "
    "lower), `normal` or `wet` (80th or higher). `percentile` is null with fewer than 5 earlier years.\n"
    "- `soilWetness.monthly`: the usual wetness for each calendar month; `seasonPercentiles` gives p10, p50 and p90 of "
    "the growing-season mean across years.\n"
    "- `et0`: FAO-56 Penman-Monteith reference evapotranspiration, which uses radiation, wind and humidity; "
    "`/field-summary` uses the temperature-only Hargreaves estimate.\n"
    "- `seasonByYear`: growing-season wetness, ET0 (mm), radiation, wind and humidity for each year; null for a "
    "season with under 90% of its days.\n\n"
    "Growing season is May-September north of the equator, November-March south of it. The first request for a place "
    "takes a few seconds; repeats are cached for 6 hours."
)
AGRO_WEATHER_ERROR = error("NASA POWER could not be reached or returned unusable data.",
                           "NASA POWER agro-weather is unavailable or invalid")
AGRO_WEATHER_FIELD = {
    "summary": "Soil wetness, radiation and ET0 around a field",
    "description": AGRO_WEATHER_TEXT,
    "responses": {404: error("No field has this ID in the loaded data.", "Field not found"),
                  422: {"description": "years outside 5 to 45."}, 502: AGRO_WEATHER_ERROR, 503: FIELDS_UNAVAILABLE},
}
AGRO_WEATHER = {
    "summary": "Soil wetness, radiation and ET0 around any outline",
    "description": AGRO_WEATHER_TEXT,
    "responses": {422: {"description": "Invalid polygon, years outside 5 to 45, or unknown request property."},
                  502: AGRO_WEATHER_ERROR},
}

CROP_CALENDAR_TEXT = (
    "Usual planting and maturity dates for crops around a field, from the GGCMI Phase 3 crop calendar (NASA GISS and "
    "partners, CC BY 4.0) on a 0.5 degree grid (about 50 km).\n\n"
    "Each crop has `plantingDay` and `maturityDay` (day of year), the same as `plantingDate` and `maturityDate` "
    "(`MM-DD`), `growingSeasonDays`, `offSeasonDays` (365 minus the season: how long the ground is free of this crop), "
    "`crossesNewYear` (true for crops such as winter wheat that are planted in autumn and mature the next summer) and "
    "the `dataSource` the calendar used for that cell. Values are `null` where the calendar has none.\n\n"
    "Crop keys: `corn`, `soybeans`, `winter_wheat`, `spring_wheat`, `barley`, `rye`, `canola`, `potatoes`, `beans`, "
    "`peas`, `sunflower`, `sugar_beet`, `sorghum`, `millet`, `rice_first_season`, `rice_second_season`, `cotton`, "
    "`groundnut`, `cassava`, `sugarcane`. Oats, hay and fruit are not in the calendar.\n\n"
    "**Limits:** multi-year averages around the year 2000, not a forecast and not local variety advice. Cells where a "
    "crop is not grown are filled in from neighbouring areas, so a date does not mean the crop is grown there. "
    "The first request downloads the calendar files (about 3 MB) and takes a few seconds."
)
CROP_CALENDAR_ERROR = error("The crop calendar files could not be downloaded or read.",
                            "GGCMI crop calendar could not be downloaded")
CROP_CALENDAR_FIELD = {
    "summary": "Usual planting and maturity dates around a field",
    "description": CROP_CALENDAR_TEXT,
    "responses": {404: error("No field has this ID in the loaded data.", "Field not found"),
                  422: {"description": "Unknown system or crop key."}, 502: CROP_CALENDAR_ERROR, 503: FIELDS_UNAVAILABLE},
}
CROP_CALENDAR = {
    "summary": "Usual planting and maturity dates around any outline",
    "description": CROP_CALENDAR_TEXT,
    "responses": {422: {"description": "Invalid polygon, unknown system or crop key, or unknown request property."},
                  502: CROP_CALENDAR_ERROR},
}

VEGETATION_HISTORY_TEXT = (
    "A field's long vegetation record from NASA MODIS/Terra, read through Microsoft Planetary Computer with no "
    "account.\n\n"
    "- `ndvi.observations`: NDVI every 16 days at 250 m for the last `years` years (MOD13Q1; 1 to 10 years, default 5). "
    "`snow` is true, with `ndvi` null, when the field was snow-covered. Composites with under half the field usable "
    "(cloud) are left out.\n"
    "- `ndvi.byYear`: each year's `peakNdvi` and `peakDate`, `greenUpDate` and `brownDownDate` (first and last "
    "composite at least halfway from the year's low to its peak; null when the year varies by less than 0.2), "
    "`greenDays` between them and `lowCoverDays`.\n"
    "- `ndvi.calendar`: for each 16-day period of the year, the median NDVI and `lowCoverShare`, the share of years "
    "with NDVI under 0.3 or snow. `ndvi.lowCoverWindows` joins the periods where that share is at least half into "
    "date ranges, longest first: when the field usually has little green cover.\n"
    "- `yearly`: net primary production (kg of carbon per square metre, MOD17A3HGF), evapotranspiration and "
    "potential evapotranspiration (mm, MOD16A3GF) for every year from 2001, with `etShareOfPet`; "
    "`yearlySummary` gives medians and the production trend per year.\n\n"
    "**Limits:** pixels are 250 to 500 m, so small fields share values with their surroundings; use the "
    "Sentinel-2 greenness route for detail since 2017. Low cover includes stubble and young crops, not only bare "
    "soil. Yearly values are modelled from satellite data.\n\n"
    "**Speed:** the first request reads about 46 files per year of NDVI plus 75 yearly files: about 30 seconds for "
    "5 years and 45 seconds for 10. Repeats are cached for 12 hours. Show a loading state."
)
VEGETATION_HISTORY_ERROR = error("The MODIS catalogue or files could not be reached.",
                                 "Planetary Computer (MODIS catalogue) is unavailable")
VEGETATION_HISTORY_FIELD = {
    "summary": "Long vegetation record of a field",
    "description": VEGETATION_HISTORY_TEXT,
    "responses": {404: error("No field has this ID in the loaded data.", "Field not found"),
                  422: {"description": "years outside 1 to 10."}, 502: VEGETATION_HISTORY_ERROR, 503: FIELDS_UNAVAILABLE},
}
VEGETATION_HISTORY = {
    "summary": "Long vegetation record of any outline",
    "description": VEGETATION_HISTORY_TEXT,
    "responses": {422: {"description": "Invalid polygon, years outside 1 to 10, or unknown request property."},
                  502: VEGETATION_HISTORY_ERROR},
}

DROUGHT_TEXT = (
    "How wet or dry the groundwater, root-zone soil moisture and surface soil moisture are around a field, from "
    "NASA's GRACE and GRACE-FO drought indicators (global, 0.25 degrees, weekly).\n\n"
    "Each value is a percentile against the same time of year over 1948-2012: 50 is typical, 10 is drier than nine "
    "years in ten. `latest` gives the newest week with a plain-language `condition` for each layer (`exceptionally "
    "dry` up to the 2nd percentile, then `extremely`, `severely`, `moderately` and `abnormally dry` up to the 5th, "
    "10th, 20th and 30th, `normal` to the 70th, `wetter than normal` to the 90th, `much wetter than normal` above); "
    "`conditions` lists these limits. `weekly` is the last `weeks` weeks and `sameWeekByYear` is the same time of "
    "year in each of the last `years` years, for the longer trend.\n\n"
    "**Limits:** cells are about 25 km, so this is the district's water, not one field. The newest week is usually "
    "about two months old (`latest.ageDays`). Values are `null` over ocean and ice and south of 60 degrees south.\n\n"
    "**Speed:** weekly files (about 2 MB each, whole world) are downloaded with the server's NASA Earthdata login "
    "the first time they are needed, about 20 seconds for the defaults; after that any field answers at once."
)
DROUGHT_ERROR = error("NASA's catalogue or files could not be reached.",
                      "NASA GRACE drought indicator catalogue is unavailable")
DROUGHT_CREDENTIALS = error("Earthdata credentials are not configured on the server.",
                            "Configure EARTHDATA_USERNAME and EARTHDATA_PASSWORD on the API host to fetch GRACE data")
DROUGHT_FIELD = {
    "summary": "Groundwater and soil moisture percentiles around a field",
    "description": DROUGHT_TEXT,
    "responses": {404: error("No field has this ID in the loaded data.", "Field not found"),
                  422: {"description": "weeks outside 1 to 52 or years outside 0 to 23."},
                  502: DROUGHT_ERROR, 503: DROUGHT_CREDENTIALS},
}
DROUGHT = {
    "summary": "Groundwater and soil moisture percentiles around any outline",
    "description": DROUGHT_TEXT,
    "responses": {422: {"description": "Invalid polygon, weeks or years out of range, or unknown request property."},
                  502: DROUGHT_ERROR, 503: DROUGHT_CREDENTIALS},
}
