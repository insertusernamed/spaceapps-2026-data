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

Run `python -m uvicorn server:app --host 127.0.0.1 --port 8000`.
Swagger UI: <http://127.0.0.1:8000/docs>.

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
