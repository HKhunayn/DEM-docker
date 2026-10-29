# GIS Data Platform

Open-source-friendly ingestion, catalog, and delivery API for elevation, imagery, vectors, and 3D assets.

## Current capabilities

- Catalog metadata in STAC-like JSON.
- Ingest arbitrary files while preserving the original bytes.
- Serve assets with stable IDs.
- Serve the existing `heightmap-30m` Terrarium elevation pyramid and ArcGIS ImageServer output through one API.
- CORS-enabled HTTP API suitable for a separate viewer or reverse proxy.
- Health endpoint and OpenAPI docs.

## Data model

All data lives under a single root: `data/` in this project.

```text
data/
├── catalog/catalog.json   STAC-like catalog + saved areas
├── sources/<uuid>/        ingested files (original bytes preserved)
├── dem/                   source DEM GeoTIFFs + manifest.json
├── imagery/<name>/        fetched imagery sets: {z}/{x}/{y}.jpg + imagery.json
└── tiles/                 derived: published/<version>/ (served terrain, the only
                           copy), arcgis/ (Lerc ImageServer), tilejson.json;
                           terrarium/ only between build and publish
```

The `heightmap-30m` project is only the build engine (`terrainhub.py` + `scripts/`); it no longer owns storage. This is intentionally replaceable: for production, use object storage/COGs for large rasters and PostgreSQL/PostGIS for catalog, permissions, vector data, and jobs. Do not put every large DEM pixel into a relational database unless raster SQL is required.

## Run

From the repository root, the recommended deployment is Docker Compose:

```bash
./Run.sh up
# Windows: Run.bat
```

This starts the API at `http://127.0.0.1:8787` and the viewer/reverse proxy at
`http://127.0.0.1:5173`. The API image contains no data; Compose stores runtime
content in the named `gis-data` volume. The DEM build engine is bundled in
`engine/` and baked into the API image at `/opt/heightmap-30m`, so fetch/build
jobs work inside the container without any bind mount.

For local development without Docker:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m app --host 127.0.0.1 --port 8787
```

On Linux/macOS `./Run.sh` does the same (creates `.venv`, installs
`requirements.txt`, then runs `.venv/bin/python -m app`; override with
`HOST=0.0.0.0 PORT=8790 ./Run.sh`).

`requirements.txt` holds the pinned runtime dependencies (what the Docker image
installs). For the test suite install the dev extras as well:

```bash
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest
```

The elevation is served through `/tiles/terrain.json` (keys: TileJSON fields +
`tiles` = `/tiles/published/<version>/terrain/{z}/{x}/{y}.webp`, `format`
`webp`, `published` = version; without a published version the dynamic
`/tiles/terrain/{z}/{x}/{y}.png?v=` route and `format` `png`) and
`/arcgis/rest/services/<name>/ImageServer`, read from `data/tiles/`. There is
no server hillshade (no `imagery` key, no `/tiles/imagery/...` or
`/tiles/published/<v>/hillshade/...` routes): MapLibre and Cesium shade the
elevation tiles in the browser.

Jobs run `fetch → build → verify → publish`. Publish moves the raw Terrarium
tiles into `data/tiles/published/<version>/` (hard links + computed edge
tiles), switches `current.json`, then deletes the raw `terrarium/` pyramid,
its `build_manifest.json` and every older published version: one copy of the
elevation on disk, one published version. `publish_tiles.py --keep-raw` /
`terrainhub.py publish --keep-raw` keep the raw pyramid (dev/tests).

## Ingest

```bash
curl -X POST http://127.0.0.1:8787/api/ingest \
  -H 'content-type: application/json' \
  -d '{"path":"C:/data/elevation.tif","kind":"elevation","crs":"EPSG:4326"}'
```

Supported input is intentionally extension-agnostic at this layer. Processing adapters should be added behind this API for GDAL-supported rasters/vectors, point clouds, and 3D models. Unknown formats are still preserved and catalogued rather than silently rejected.

## Fetch page (first project)

`http://127.0.0.1:8787/fetch/` is the elevation fetch UI (same behavior as the
`heightmap-30m/public/fetch` page): drag a bbox on the map or type coordinates,
pick a provider (Copernicus DEM GLO-30, or FABDEM when the engine can serve it),
pick a dataset name, then **Fetch & build**. The page shows the current step,
elapsed time, live log, and the build result.

Jobs run the existing `heightmap-30m` CLI (`terrainhub.py fetch|build|verify|publish`)
as background subprocesses — the platform adds scheduling, status, and catalog
records; it does not reimplement DEM download logic.

### Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `GIS_DATA_ROOT` | `./data` | single data root: `dem/`, `tiles/`, `catalog/`, `sources/` |
| `HEIGHTMAP_ENGINE_ROOT` | bundled `engine/` (else `C:\Users\hisham\heightmap-30m`) | build engine (`terrainhub.py`, `scripts/`) — code only, no data |
| `HEIGHTMAP_PYTHON` | engine `.venv` python, else current | interpreter used for engine jobs |
| `LEAFLET_ROOT` | engine `node_modules/leaflet` (image: `/opt/leaflet`) | Leaflet assets for the fetch page |

The engine CLI (`terrainhub.py fetch|build|verify|serve`) resolves the same
`GIS_DATA_ROOT`, so manual runs and platform jobs write to the same place.

### APIs

- `GET  /api/fetch/providers` — provider list with `available` flags (FABDEM needs `remotezip`+`requests` in the engine python).
- `POST /api/jobs` — start a fetch/build/verify job `{name, bbox[4], provider, minzoom, maxzoom, saveArea}` (202; 409 while another job runs). Also `POST /api/fetch` (legacy shape).
- `GET  /api/jobs`, `GET /api/jobs/current`, `GET /api/jobs/{id}` — job status: `state`, `step`, `elapsed`, `log`, `result`, per-step return codes.
- `GET  /api/status` — legacy single-job snapshot (`idle|running|done|error`).
- `GET/POST /api/areas`, `GET/DELETE /api/areas/{id}` — saved area records persisted in `data/catalog/catalog.json` (bbox, provider, zooms, dataset metadata: state, tile counts, service URL).

Each job auto-saves its area and back-fills the dataset metadata when it
finishes; the fetch page's "Saved areas" selector reloads any saved bbox.

## Imagery fetch

The fetch page has an **Imagery** tab: pick an area (or **whole world**), a
provider and a zoom range (default z0–1), then **Fetch imagery**. Tiles are
downloaded as delivered by the provider (JPEG, Web Mercator XYZ) into
`data/imagery/<name>/`; re-running a fetch keeps tiles already present.

| Provider | Max zoom | Licence |
| --- | --- | --- |
| `s2cloudless` — EOX Sentinel-2 cloudless 2016 | 15 | CC BY 4.0 |
| `bluemarble` — NASA GIBS Blue Marble | 8 | public domain |

- `GET  /api/imagery/providers` — imagery providers.
- `POST /api/imagery/jobs` — `{name, bbox[4], provider, minzoom, maxzoom, saveArea}` (202; 409 while any job runs; 400 above 50,000 tiles). Poll with `GET /api/jobs/{id}`.
- `GET  /api/imagery` — local imagery sets as TileJSON; `DELETE /api/imagery/{name}`.
- `GET  /tiles/imagery-sets/{name}.json` — TileJSON; `GET /tiles/imagery-sets/{name}/{z}/{x}/{y}` — tile. Above the set's maxzoom the platform over-zooms server-side (needed by ArcGIS `WebTileLayer`); outside the set: transparent PNG.

Each imagery job saves an area with `kind: "imagery"`; the viewers list it and
show the selected imagery set in MapLibre, ArcGIS and CesiumJS.
