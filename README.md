# Local Open GIS Data Platform

Two projects are included:

- `gis-data-platform/` — ingest, catalog, tile, and serve heterogeneous geospatial data.
- `gis-viewers/` — one viewer application with MapLibre GL JS, ArcGIS Maps SDK for JavaScript, and CesiumJS adapters.

The platform is open-source friendly and keeps original files as the source of truth. It publishes interoperable derivatives instead of forcing every client to read every source format directly.

## Quick start with Docker Compose

The repository includes separate images for the API (`gis-data-platform`) and
static viewer (`gis-viewers`). Runtime data is stored in the named
`gis-data` volume; it is not copied into either image or the build context.

```bash
./Run.sh up
# Windows: Run.bat
```

`Run.bat` creates `.env` from `.env.example` when it is missing. With no Docker
installed it falls back to a local dev run (API via `gis-data-platform\.venv`,
viewer via `npm run dev`).

Open:

- viewer: `http://127.0.0.1:5173/`
- fetch page: `http://127.0.0.1:5173/fetch/`
- API docs: `http://127.0.0.1:8787/docs`

Useful commands are `./Run.sh down`, `./Run.sh logs`, and `./Run.sh build`
(or the corresponding `Run.bat` command). Set `GIS_PLATFORM_PORT` and
`GIS_VIEWER_PORT` in `.env` to change host ports.

The DEM engine is bundled in `gis-data-platform/engine/` (the `heightmap-30m`
`terrainhub.py` + `scripts/`, including the Lerc1 encoder sources) and baked
into the API image at `/opt/heightmap-30m`, so fetch/build/verify jobs work in
the container with no bind mount. The composed deployment uses same-origin
viewer requests through nginx; set `VITE_PLATFORM_URL` only when the viewer is
hosted separately.

## What is baked into the images

| Image | Contents |
| --- | --- |
| `gis-data-platform` | Python API + `requirements.txt` (fastapi, uvicorn, numpy, Pillow, rasterio, requests, remotezip, httpx2/httpx, pytest), the `engine/` DEM build toolchain with `lerc1enc` compiled from source, Leaflet 1.9.4 vendor assets |
| `gis-viewers` | Vite build of the MapLibre / ArcGIS / CesiumJS viewer, served by nginx with `/api`, `/tiles`, `/arcgis`, `/fetch`, `/vendor` proxied to the API |

Runtime data (DEM tiles, pyramids, catalog) lives in the named `gis-data`
volume and is never copied into an image.

## Local development

`Run.bat` runs the stack locally when Docker is absent: it creates
`gis-data-platform\.venv` with `requirements.txt` on first run, then starts the
API and the Vite dev server. The original per-project commands remain available
in each project's `README.md` and `Run.bat` files.

## Architecture

```text
source files -> ingest/catalog -> COG/object storage + STAC
                                      |
                       +--------------+----------------+
                       |                               |
                 raster/vector APIs             viewer adapters
                 TileJSON/XYZ/WMS/WCS           MapLibre/ArcGIS/Cesium
```

For production, put the API behind a reverse proxy and replace the local filesystem repository with object storage plus PostgreSQL/PostGIS metadata. The API contract remains the same.
