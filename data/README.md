# Ontario agricultural field boundaries

Field source: **Fields of the World, Global 2nd Edition, 2025**,
https://source.coop/ftw/global-data-2e, licensed CC-BY-4.0.
Attribution: Fields of the World contributors / Radiant Earth, via Source Cooperative.

`fields.geojson` contains 300 real field predictions sampled across the Orillia
area as a portable preview. It replaces the earlier Iowa sample. It is not the
complete Orillia or Ontario dataset.

`pipeline/build_fields.py orillia` builds the complete available source extract
for bbox `[-79.7,44.35,-79.05,44.85]`. `pipeline/build_fields.py ontario` expands
the index across Ontario, querying all overlapping UTM zones (15–18). Both
use 2025 source polygons, preserving whole fields intersecting Ontario's
simplified provincial boundary. See [boundary attribution](boundaries/README.md).

Outputs:

- `fields.sqlite`: generated SQLite RTree index, ignored by Git. This is the
  dataset automatically served by the API when present.
- `fields.coverage.json`: completed-build source, region, date and field count.
- `downloads/`: ignored regional GeoParquet extracts for resumable acquisition.

To reproduce, install `pipeline/requirements.txt` and run the commands above
from the repository root. First acquisition needs network access and DuckDB's
httpfs/spatial extensions. Database queries and acreage lookup then work locally.
No NASA login or crop-history acquisition is involved.

Geometry, field ID and model score are retained. Crop properties are excluded.
IDs are prefixed `ftw-2025-` and only identify this source edition/year.
The score is the mean model output inside a polygon, not the probability a farm
exists. Empty/invalid geometries are excluded; IDs already present are deduplicated.

These outlines are satellite-model predictions of field units, not ownership or
farm-business records. Several fields may belong to one farm; some fields can
be missed or incorrectly outlined. Areas are estimates from the outlines. Ontario
coverage does not imply coverage of all Canada or North America.
