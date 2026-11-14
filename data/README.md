# Sample field boundaries

`fields.geojson` contains 55 real USDA NASS Crop Sequence Boundaries (CSB) fields,
extracted from fieldpicker's local `pipeline/out/ia.geojsonl`. The extract selects
features whose first vertex is within `[-91.96, 41.34, -91.90, 41.40]` (Iowa,
WGS84). Only IDs and geometry are retained; crop attributes have been removed.

Attribution: USDA National Agricultural Statistics Service, Crop Sequence Boundaries.
US government data. These remotely derived agricultural fields are not cadastral
parcels. Source area attributes are not used; the API estimates outline area.
Replace this sample or configure `FIELDS_FILE` for broader coverage.
