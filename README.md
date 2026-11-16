# SpaceApps 2026 Data Service

The data service is the only part of the system that talks to NASA and other
public data providers. It finds farm field outlines, retrieves satellite,
climate, soil and crop-map data for a field, and returns it as measured values
with their source details. Its only consumer is the Java application backend;
the frontend never calls it directly.

## What it is responsible for

- **Getting data.** Searching, downloading, streaming and caching data from NASA
  and other providers, including Earthdata logins.
- **Measuring a field.** Reducing images and grids to numbers for a field outline
  (for example mean NDVI, soil texture over the outline, or the share of each crop
  class), and standard indicators such as growing-degree days, frost dates and
  reference evapotranspiration.
- **Reporting the source.** Every response says which dataset answered, at what
  resolution, for which dates and under which licence, plus known limitations.

## What it is not responsible for

- Recommendations, scoring or crop rotation planning (Java backend).
- Interpreting observations, for example naming crop groups, inferring rotations
  or choosing display quality for season charts (Java backend).
- Anything shown to the farmer, or chat (Java backend and frontend).

It never invents a missing value. A year or source with no data comes back empty
or marked unavailable, with the reason.

## Fields

Field outlines come from **Fields of The World (Global, 2nd edition, 2025)**.
These are predicted agricultural fields, not farms or ownership parcels. They
are imported ahead of time into a spatial index covering Ontario, with
background import extending to the rest of Canada, the United States and Mexico.

- `GET /fields`: outlines in a map viewport, with size and shape facts (area,
  perimeter, orientation, compactness)
- `GET /fields/{id}`: area of one field
- `GET /field-coverage`: which region is loaded and how far the import has got

## Field data

Most data is available two ways: by stored field (`/fields/{id}/...`) or for any
drawn outline (`POST /...`).

| Area | Routes | Source | Provides |
|---|---|---|---|
| Climate summary | `/field-summary`, `/fields/{id}/summary` | NASA Daymet (1 km, North America), falling back to NASA POWER (about 50 km, global); NASA SRTM 30 m via OpenTopoData | Climate indicators, water balance, frost dates, crop heat units, year-by-year values and percentiles, slope and low spots |
| Agro-weather | `/agro-weather` | NASA POWER | Recent root-zone and surface soil wetness ranked against history, Penman-Monteith reference ET, radiation, wind, humidity |
| Daily weather | `GET /daily-point` | NASA POWER | Raw daily values for a point |
| Soil moisture map | `GET /soil-moisture` | NASA SMAP L4 (SPL4SMGP + SPL4SMLM) | 9 km soil moisture, wetness, temperature and water flux, as a map layer |
| Drought | `/drought-indicators` | NASA GRACE/GRACE-FO drought indicators | Weekly groundwater and soil moisture percentiles compared with 1948-2012 |
| Soil | `/soil`, `/fields/soil` | ISRIC SoilGrids 2.0 (250 m) | Topsoil pH, texture, organic carbon, nitrogen, bulk density, CEC; texture class, organic matter, available water |
| Crop history | `/crop-history`, `/fields/crop-history` | AAFC Annual Crop Inventory (Canada), USDA Cropland Data Layer (US) | Share of the field in each crop class per year |
| Crop calendar | `/crop-calendar` | GGCMI Phase 3 (Zenodo) | Usual planting and maturity dates for 18 crops |
| Greenness | `/greenness` | Copernicus Sentinel-2 L2A via Earth Search (10 m) | NDVI per clear pass over one season, peak, latest value, green-up date |
| Vegetation history | `/vegetation-history` | NASA MODIS (MOD13Q1, MOD17A3HGF, MOD16A3GF) via Planetary Computer | 16-day NDVI over up to 10 years, low-cover periods, yearly plant growth (NPP) and evapotranspiration |
| Season measurements | `/fields/{id}/season`, `/field-season` | NASA HLS (Landsat + Sentinel-2, 30 m) via Planetary Computer | Per-pass NDVI with cloud, snow and water fractions, built in the background |
| Imagery | `/imagery` | NASA HLS or Sentinel-2 via Planetary Computer | Links to field images that passed a cloud check over the field itself |

`/health` and `/ready` report whether the service is running and whether its
field data can be read.

## How to read the data

- **Satellite and model data describe an area, not a field.** Resolution ranges
  from 10 m (Sentinel-2) to about 50 km (POWER). SMAP, GRACE and POWER describe
  the district, not one field.
- **Some values are modelled or estimated, not measured.** SMAP and GRACE combine
  satellite data with land models; SoilGrids is a machine-learning estimate; crop
  maps are image classifications and any one year can be wrong, especially at
  field edges.
- **Ground checks still matter.** A soil test, field walk, local weather station
  and the farmer's own records should confirm anything a decision depends on.

Full route details are in the OpenAPI docs at `/docs`. Dataset licences and
citations are in [data/README.md](data/README.md).

## AI Usage

AI was used during this project for:

 - Brainstorming and ideas
 - Research
 - Data interpretation
 - Code generation
 - Writing and editing