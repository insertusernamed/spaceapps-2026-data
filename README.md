# SpaceApps 2026 Data API

Field boundaries, acreage and environmental summaries, plus a NASA POWER daily
point client. The standalone client loads Earthdata credentials from `.env`;
field selection and environmental summary routes do not need them.

## Setup

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py init-env        # prompts, writes .env (mode 600)
```

`.env` is gitignored; `.env.example` shows the two variable names.

## Use

```sh
python app.py check-auth      # verifies the Earthdata login
python app.py fetch --lat 34.05 --lon -118.24 --start 20240101 --end 20240107
```

`fetch` prints the raw POWER JSON. Edit `fetch_daily_point` in `app.py` to add
parameters or switch to the monthly/climatology endpoints.

## Docker

```sh
docker build -t spaceapps-data .
docker run --rm -p 8091:8091 spaceapps-data
```

Swagger UI is at <http://localhost:8091/docs>. Without a database the image serves
the committed Orillia preview. To serve an indexed build, mount the directory that
holds it (so SQLite WAL/SHM files are shared) and set `FIELDS_DB`:

```sh
docker run --rm -p 8091:8091 -v "$PWD/data:/var/lib/spaceapps-fields" \
  -e FIELDS_DB=/var/lib/spaceapps-fields/fields.sqlite spaceapps-data
```

The container runs as UID/GID 1000; the mounted directory must be writable by it.
Credentials are never built into the image; mount `.env` at `/app/.env` for
`/daily-point`.

## Tests

```sh
python -m unittest discover tests
```

## Field selection API

Run `python -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload`.
Swagger UI: <http://127.0.0.1:8000/docs> (the root URL redirects there). Python
changes restart the development server automatically; refresh Swagger to load
changed routes. Route descriptions, tags and examples shown in Swagger live in
`api_docs.py`.

- `GET /health` answers 200 whenever the process is running.
- `GET /ready` answers 200 when the configured field data can be read, and 503
  otherwise. It does not contact NASA POWER or terrain providers, so their
  outages do not mark the API unready.

- `GET /fields?bbox=-79.7,44.35,-79.05,44.85&limit=500` returns WGS84 field
  outlines in the visible map area, without crop information. Bbox order is
  west,south,east,north. Split boxes crossing the antimeridian into two requests.
- Follow `nextCursor` using the same bbox and limit while `hasMore` is true.
  Pages default to 50 features, capped at 2,000. Changing the viewport resets
  the cursor to zero. A missing bbox defaults to the Orillia area for the database.
- Each outline's `properties` carry quick field facts for a sidebar, computed from
  the outline: `areaHa`, `areaAcres`, `perimeterM`, `lengthM`, `widthM`,
  `orientationDeg` (long side's bearing from north, 0-179), `compactness` (0-1,
  circle 1), `holes`, `parts`, `centroid` ([lon, lat]) and `sourceYear`. They are
  computed once per field on first request and cached (about 0.05 ms a field).
- Coordinates are cut to 6 decimal places (about 0.1 m). Responses over 1 KB are
  gzip-compressed for clients that send `Accept-Encoding: gzip`.
- `GET /fields/{field_id}` returns estimated hectares and acres for a clicked ID.
  Unknown IDs return 404.
- `GET /field-coverage` reports the loaded region, field count, source, licence,
  completed geographic partitions and whether acquisition is still building.

These routes need no Earthdata credentials. Each source polygon represents a
**predicted agricultural field**, not a whole farm, ownership parcel or farm
business. Source coverage and model errors mean this is not an exhaustive farm
registry. IDs refer to one source edition/year and should not be matched across years.

### Browser access and request IDs

CORS is off by default: browsers on other origins cannot call the API directly,
so front ends call it through a same-origin proxy (as the Vite dev server does).
To enable it, set `CORS_ORIGINS` to a comma-separated list of origins, for example
`CORS_ORIGINS=https://app.example.org,localhost`. The word `localhost` allows any
`localhost`/`127.0.0.1` port, and `*` allows any origin.

Every response carries an `X-Request-Id` header. A caller may send its own ID
(up to 128 letters, digits or `._:-`); otherwise one is generated. Each request
is logged with its ID, method, path, status and duration.

### Load Orillia, then Ontario

The completed local build contains **4,105 Orillia-area fields** and
**353,809 fields across Ontario** (2025 source edition, built 2026-11-14).
These counts describe detected field units, not numbers of farm businesses.
The full Ontario index is approximately 1.9 GB; plan for additional space for
the downloaded extracts. See [coverage manifest](data/fields.coverage.json).

A 300-field Orillia-area preview is committed for quick startup. The full spatial
index is generated locally from public sources, rather than committed as a large
binary. Install acquisition dependencies and build the first region:

```sh
pip install -r pipeline/requirements.txt
python pipeline/build_fields.py orillia
```

Then expand the same database to Ontario:

```sh
python pipeline/build_fields.py ontario
```

The pipeline reads 2025 field predictions from Fields of the World, Global 2nd
Edition. Ontario is filtered against its provincial boundary across UTM zones
15–18, preserving complete intersecting field outlines. Invalid/empty geometries
are excluded. Previously imported IDs are deduplicated. Completed remote extracts
are reused on rerun; `/field-coverage` distinguishes a partial build from completion.
The Orillia region is a surrounding agricultural area, bbox
`[-79.7,44.35,-79.05,44.85]`, rather than just city limits.

The API automatically uses `data/fields.sqlite` when present. SQLite RTree queries
read only matching outlines rather than loading the province into Python memory.
`FIELDS_DB=/absolute/path/fields.sqlite` selects another index. A server restart
is not needed after a build. Keep the database unchanged while traversing pages;
a rebuild can change pagination positions.

For a custom small dataset, `FIELDS_FILE=/absolute/path/fields.geojson` overrides
the default database (an explicit `FIELDS_DB` takes precedence). Use a WGS84
FeatureCollection of closed Polygon/MultiPolygon features with unique feature
`id` or `properties.id`. The GeoJSON adapter strips all properties except ID;
the indexed source additionally exposes its `score` (0–100 mean model score,
not a calibrated probability). Missing/invalid configured data returns 503.

Area is estimated from complete outlines, subtracting holes, and rounded to two
decimals; it is not surveyed acreage. Source, licence and limitations are documented
in [data/README.md](data/README.md). The API serves GeoJSON; a PMTiles client needs
its own static tile contract. Fetch each viewport, then request acreage on click.

## Field environmental summary

`POST /field-summary` accepts the geometry of a selected field from `/fields`.
It returns area and centroid, the weather sampling coordinate, climate indicators,
seasonal water balance, frost dates, monthly climate averages, data coverage,
terrain statistics, and source metadata. Crops are covered by the crop history
routes below.
No Earthdata credentials are needed.

Example request (also available in Swagger):

```json
{
  "polygon": {
    "type": "Polygon",
    "coordinates": [[[-91.94, 41.36], [-91.93, 41.36], [-91.93, 41.37], [-91.94, 41.37], [-91.94, 41.36]]]
  },
  "period": {"start": "2023-01-01", "end": "2023-12-31"},
  "season": {"months": [5, 6, 7, 8, 9]},
  "weatherGridDeg": 0.05
}
```

`polygon` is required: a GeoJSON Polygon, MultiPolygon, Feature, or a ring of
`[longitude, latitude]` coordinates. Defaults are 2015–2024 and May–September
north of the equator, November–March south of it. Dates accept YYYY-MM-DD or
YYYYMMDD; they must be ordered, within 1981 through today, and span at most
45 years. Custom season months must be consecutive and can cross December.
Unknown request properties and invalid polygons/dates return 422.

Weather comes from NASA Daymet (1 km, North America, 1980 to the last complete
year) or NASA POWER (about 50 km, global, up to today). `weatherSource` is `auto`
by default, which picks Daymet when it covers the field and period and otherwise
POWER; `daymet` or `power` forces one (forcing Daymet where it cannot apply
returns 422). If Daymet fails under `auto`, POWER is used and
`layers.weather.fallbackFrom` says so. Daymet samples the field's ~1 km cell.
For POWER, `weatherGridDeg` chooses a sampling coordinate on a 0.05-degree grid
by default; zero uses the outline's centre. It does **not** provide weather at
that finer resolution. Weather and elevation downloads are cached in memory, so
repeat requests for a field are fast. Temperature-based Hargreaves ET0
(FAO-56) estimates reference evapotranspiration, not actual crop water use.
Growing-degree days use a 10 C base. `chu_available_median` gives Ontario crop
heat units (the corn and soybean variety rating scale), counted each year from
three days averaging at least 12.8 C (no earlier than May 1) to the first -2 C
fall frost; it reflects the requested years. A year/season needs at least 90% valid
observations to contribute to its statistic; unavailable statistics are `null`.
Full calendar years are recommended for annual summaries. Monthly values are
climatological averages over the requested years, not a dated time series.

Terrain uses NASA SRTM 30 m elevation via OpenTopoData, sampled within the
outline. Slope and erosion ratings are approximate context rather than surveyed
measurements. Weather and terrain are fetched concurrently. A terrain failure
returns `terrain: null` with an error in `layers.terrain` while preserving weather;
a weather provider failure returns 502. First requests depend on upstream latency.

Install test dependencies with `pip install -r requirements-dev.txt`, then run
`python -m unittest discover tests -v`.

## SMAP soil moisture layer

The acquisition command uses the same `EARTHDATA_USERNAME` and
`EARTHDATA_PASSWORD` as the Python setup above. It reads `.env` (or the existing
local `.env.` filename). Credentials remain on the backend.

```sh
source .venv/bin/activate
pip install -r requirements.txt
python app.py fetch-smap
# Optional date/region; negative bbox values need the equals sign:
python app.py fetch-smap --date 2025-01-01 --bbox=-79.7,44.35,-79.05,44.85
```

Defaults to the Orillia field-preview region and the latest available 3-hour
interval. An explicit date selects the latest interval that day, not a daily
average. `earthaccess.open()` opens an authenticated, seekable remote HDF5
stream. The converter reads coordinate axes and regional slices of the two
soil moisture variables through that stream. It does not save a global HDF5
file locally. HDF5 chunks and caching determine the actual bytes transferred;
streaming can still read more data than the requested pixels. Only the regional
GeoJSON is saved. The regional result replaces
`data/smap/soil-moisture.geojson` atomically after successful conversion; failed
acquisition preserves the previous layer. Re-run to refresh it. There is no
scheduled refresh. Regions are limited to 10 by 10 degrees per fetch.

`GET /soil-moisture` streams the latest Orillia-area data through Earthaccess
and creates the GeoJSON automatically when its cache is missing. Older response
schemas are upgraded while preserving the cached region and day. The first
request requires Earthdata credentials and can take longer; subsequent requests
serve the cache without contacting NASA. To choose another region or date, or
refresh an existing cache, use `python app.py fetch-smap`. `SMAP_FILE` selects
the cache path for both acquisition and serving. Python 3.12 is recommended;
newer interpreters may need native build tools for HDF5 dependencies.
For Docker, rebuild the image after dependency changes and mount `.env` at
`/app/.env` (or pass the two Earthdata environment variables). Mount a writable
layer directory at `/app/data/smap` to retain the cache across container changes
or set `SMAP_FILE` to a file on your existing data volume. The frontend's Vite
proxy exposes the endpoint as `/api/soil-moisture`; the map can use the response
as a MapLibre GeoJSON source. A visual map toggle is not included.

Each feature is one native EASE-Grid cell transformed to a WGS84 polygon.
Feature properties retain NASA variable names:

| Properties | Meaning / units |
| --- | --- |
| `sm_surface`, `sm_rootzone`, `sm_profile` | Moisture in m³/m³: 0–5 cm, 0–100 cm, and surface to model bedrock |
| `sm_surface_wetness`, `sm_rootzone_wetness`, `sm_profile_wetness` | Relative saturation, 0–1 |
| `sm_rootzone_pctl`, `sm_profile_pctl` | Seasonal moisture percentiles, 0–100; not water content percentages |
| `surface_temp`, `soil_temp_layer1` through `soil_temp_layer6` | Model temperatures in kelvin; subtract 273.15 for Celsius |
| `soil_water_infiltration_flux`, `overland_runoff_flux`, `baseflow_flux`, `land_evapotranspiration_flux` | Water flux rates in kg m⁻² s⁻¹; not daily totals |
| `land_fraction_saturated`, `land_fraction_wilting` | Fractions of grid-cell land area saturated/wilting and snow-free, 0–1 |
| `clsm_poros`, `clsm_wp` | Static land model porosity and wilting point, m³/m³ |
| `mwrtm_clay`, `mwrtm_sand` | Static microwave-model clay and sand fractions, 0–1 |

`metadata.variables` documents units, descriptions, source product, and whether
each variable is static. The legacy `metadata.units` applies to moisture only.
Static properties are streamed from SPL4SMLM matching the geophysical filename's
science version and checked against its coordinate grid. `metadata.soil_constants`
records that file's provenance; these are model inputs, not soil test results.
Missing values are `null`; `metadata.missing_variables` lists absent datasets.
Cells missing both core moisture variables are omitted. The previous cache is
preserved if either product cannot be acquired or their grids do not match.
Metadata includes the actual time interval, source filename, acquisition time,
resolution and DOI. These are model-assimilated 9 km grid values, not individual
field measurements. Ancillary dynamic fields have different validation status
from the core soil moisture variables.
The cache covers only the requested region; it does not expand when panning.

Sources: [SPL4SMGP Version 8](https://nsidc.org/data/spl4smgp/versions/8),
[CMR API](https://cmr.earthdata.nasa.gov/search/site/docs/search/api.html),
[Earthaccess](https://earthaccess.readthedocs.io/en/latest/api/).
Use the product's required citation when publishing results.

## Crop history

`GET /fields/{field_id}/crop-history` returns what was planted in a field each
year; `POST /crop-history` with `{"polygon": ...}` does the same for any outline.
Each year reports the class covering most of the field, its share (0–1), and up
to five classes covering at least 1%. `rotation` lists the main class per year.

Canada uses the AAFC Annual Crop Inventory (Ontario from 2011, other provinces
from 2009); the United States uses the USDA NASS Cropland Data Layer (from 2008).
Both are 30 m satellite classifications that need no account, and the newest
published year is detected automatically. One request per year summarises every
map pixel inside the outline, so a field takes about a second the first time and
is cached afterwards. Class names are each provider's own. Any single year can be
misclassified, especially at field edges. Locations outside Canada and the United
States, including Mexico, return no years. Legends are in
`environmental/crop_classes.py`.

For several fields at once (for example a box selection), `POST /fields/crop-history`
with `{"ids": [...]}` accepts up to 100 field IDs. Outlines are read in one query
and fields run in parallel, while the service keeps at most 8 requests in flight to
the crop map services across all callers. `results` follows the order of `ids`
(duplicates answered once); each entry has `status: ok` with its `history`, or
`status: error` with a `code` (404 unknown ID, 502 provider failure) and `detail`.
One failed field never fails the batch, so a valid request returns 200; check `failed`.

## Field greenness

`GET /fields/{field_id}/greenness?year=2025` returns how green a field was through
one growing season, from Copernicus Sentinel-2 L2A at 10 m (Earth Search, no
account); `POST /greenness` with `{"polygon": ..., "year": ...}` does the same for
any outline. Each clear overpass (about every 2-5 days) gives the field's mean
NDVI with its 10th/90th percentiles and the clear share of the field. A summary
gives the peak, the latest value and the green-up date. Bare soil is about
0.1-0.2 NDVI and a full canopy 0.6-0.9.

The season is April 1 to November 15 (October 1 to May 15 south of the equator),
up to today for the current year; `year` defaults to the most recent season.
Cloud, shadow, snow and water pixels are removed, and an overpass counts when at
least half of the field is clear. Only the pixels over the field are read from
each cloud-optimized file, but a first request still reads 20-30 scenes and takes
about 10 seconds; finished seasons are then cached, and the current season is
refreshed every 6 hours. Collection 1 is used where available; seasons it lacks
(2017, 2022) come from the original collection. Needs `rasterio`
(in `requirements.txt`).

## Field soil

`GET /fields/{field_id}/soil` returns topsoil estimates for a field from ISRIC
SoilGrids 2.0 (250 m, global, CC BY 4.0); `POST /fields/soil` with `{"ids": [...]}`
answers up to 100 fields in request order (per-field `status`, like crop history),
and `POST /soil` takes any outline. Values are averaged over every 250 m cell the
outline touches:

- `topsoil`: thickness-weighted 0-30 cm pH, clay/sand/silt (%), organic carbon and
  nitrogen (g/kg), bulk density (g/cm3) and CEC (cmol(+)/kg); `depths` gives the
  same for 0-5, 5-15 and 15-30 cm.
- `derived`: USDA texture class, organic matter % (organic carbon x 1.724), USDA pH
  class, and plant-available water in the top 30 cm (Saxton & Rawls 2006).

These are machine-learning estimates, not a soil test, and are null over water,
ice or built-up land. The maps are read as cloud-optimized GeoTIFFs rather than
through the SoilGrids REST service, which allows only five requests a minute. Each
of the 24 layers has a tile index (about 6 MB, 144 MB in total) that is downloaded
once and kept in `SOIL_INDEX_DIR` (default: a `soilgrids-index` folder beside
`FIELDS_DB`, else `data/soilgrids-index`). The first request after a fresh install
downloads them (about 40 seconds); later fields take about 2 seconds, and repeats
are cached.

## Background North American acquisition

Install acquisition dependencies with `pip install -r pipeline/requirements.txt`.
Start or resume the detached worker:

```sh
python pipeline/north_america.py start
python pipeline/north_america.py status
python pipeline/north_america.py stop
```

The queue covers Canada, the United States and Mexico, beginning with Eastern
Canada, the Canadian Prairies, and the US Great Lakes/Midwest. Remaining areas
follow, including northern Canada, Alaska and Hawaii. Other North American
countries/territories are outside this queue. These are predicted field units;
completing the queue does not establish an exhaustive registry of farms.

The worker imports small geographic tiles into the same database while the API
continues serving already-loaded fields. New outlines are compressed; existing
Ontario rows remain readable. Completed tiles are checkpointed, IDs deduplicated,
and temporary downloads removed after import. Restarting retries incomplete or
failed tiles. Source downloads retry three times before recording a tile error
and continuing. `/field-coverage` exposes live counts and ingestion progress;
`data/background/status.json` and `data/background/worker.log` contain local detail.
A `completed_with_errors` status requires another run to retry failed areas.

Defaults retain **12 GiB of free disk space** and pause at a **30 GiB database
budget**. A `paused_disk` job requires freeing space or explicitly changing these
limits before running `start` again. Acquisition progress is retained. Stop is
graceful after the current download/batch. This is a local background process;
it survives closing its launching terminal, but must be started again after a
reboot. On macOS it prevents idle sleep while running; closing the laptop or
powering it off can still interrupt work.

### Importing on a separate API host

Set `FIELDS_DB` to the persistent SQLite path and `FIELD_INGEST_DIR` to a persistent
worker directory before starting the importer. The API's `FIELDS_DB` must refer
to that same database (using its container mount path if containerized). Mount the
whole data directory so SQLite WAL/SHM files are shared, and run the API and importer
with compatible file ownership. The API serves new committed fields immediately.
A Git clone alone contains the preview and source code, not the full dataset.

On the current Linux host, persistent field data is stored under
`/mnt/5tb_drive/spaceapps-2026/data/`, separate from the small system disk.
The background service is `spaceapps-field-import.service` in the user's systemd
manager. It uses a 50 GiB free-space reserve and a 250 GiB database budget.
Check it with `systemctl --user status spaceapps-field-import.service`, and inspect
live API ingestion status at `/field-coverage`.

The host's user service definition is versioned at
[`deploy/spaceapps-field-import.service`](deploy/spaceapps-field-import.service).
Install it into `~/.config/systemd/user/`, then run `systemctl --user daemon-reload`
and `systemctl --user enable --now spaceapps-field-import.service`. User lingering
is enabled on the current host so the job survives SSH logout and starts after boot.
The existing `spaceapps-data` Docker service uses port 8091 and mounts the persistent
field directory at `/var/lib/spaceapps-fields`, with
`FIELDS_DB=/var/lib/spaceapps-fields/fields.sqlite`. Both processes use UID/GID 1000
for shared SQLite file access. Use systemd to start/stop the managed worker rather
than starting a second worker manually.
