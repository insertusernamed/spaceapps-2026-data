# NASA POWER skeleton

Small Python client for the NASA POWER daily point API, with Earthdata credentials
loaded from `.env`.

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

## Tests

```sh
python -m unittest discover tests
```

## Field selection API

Run `python -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload`.
Swagger UI: <http://127.0.0.1:8000/docs>. The development server restarts automatically
when Python files change; refresh Swagger to see route changes.

- `GET /fields`: WGS84 GeoJSON outlines with IDs, without crop information.
- `GET /fields?bbox=-91.96,41.34,-91.90,41.40`: filter by bounding-box intersection
  (`west,south,east,north`). Split boxes crossing the antimeridian into two queries.
- `GET /fields/{field_id}`: selected field's `id`, `hectares` and `acres`.
  Use the clicked GeoJSON feature's ID. Unknown IDs return 404.

These routes need no Earthdata login. Areas are spherical estimates using a mean
Earth radius, rounded to two decimals, rather than surveyed acreage.

Bundled coverage is **55 real sample fields in Iowa**, near -91.93, 41.37, rather
than global coverage. See [data/README.md](data/README.md) for provenance.
Set `FIELDS_FILE=/absolute/path/to/fields.geojson` before startup for other data.
Use a WGS84 FeatureCollection with Polygon/MultiPolygon geometries, closed rings,
and unique feature `id` or `properties.id` values. Holes are subtracted. Source
properties other than ID are stripped. Missing or invalid datasets return 503.

This API serves GeoJSON to a map client. The existing fieldpicker component uses
PMTiles and a separate metadata contract; these routes do not replace its static
files. Render `/fields`, then request acreage when a field is clicked.

## Field environmental summary

`POST /field-summary` accepts the geometry of a selected field from `/fields`.
It returns area and centroid, the weather sampling coordinate, climate indicators,
seasonal water balance, frost dates, monthly climate averages, data coverage,
terrain statistics, and source metadata. It does not return crop information.
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

NASA POWER provides daily temperature and precipitation on a coarse grid
(approximately 50 km). `weatherGridDeg` chooses a sampling coordinate on a
0.05-degree grid by default; zero uses the outline's centre. It does **not**
provide weather at that finer resolution. Temperature-based Hargreaves ET0
(FAO-56) estimates reference evapotranspiration, not actual crop water use.
Growing-degree days use a 10 C base. A year/season needs at least 90% valid
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
