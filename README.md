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

- **platform (point your apps here): `http://<server-ip>:5000`**
  - ArcGIS elevation: `http://<server-ip>:5000/arcgis/rest/services/<area>/ImageServer`
  - Terrarium WebP TileJSON (MapLibre/Cesium/deck.gl): `http://<server-ip>:5000/tiles/terrain.json`
  - API docs: `http://<server-ip>:5000/docs`, fetch page: `http://<server-ip>:5000/fetch/`
- optional web viewer: `http://<server-ip>:3100/`

Port 5000 is an nginx **gateway** in front of FastAPI: every tile file is sent
by nginx straight from the data volume, everything else (REST API, ImageServer
metadata/tilemap, jobs, fetch page, tiles not on disk) is proxied to FastAPI,
which is no longer published on the host. The tile rules live once in
`nginx/gis-tiles.conf` and are shared by the gateway and the viewer.

Useful commands are `./Run.sh down`, `./Run.sh logs`, and `./Run.sh build`
(or the corresponding `Run.bat` command). Set `GIS_PLATFORM_PORT` and
`GIS_VIEWER_PORT` in `.env` to change host ports; both launchers print the URLs
for the ports in `.env`.

`up`, `restart` and `down` recreate/stop the API container, which kills a
running fetch/build job (job state is in memory). Both launchers check
`/api/jobs/current` first and ask for confirmation when a job is running; pass
`-y` (`./Run.sh -y up`, `Run.bat -y up`) to skip the prompt. `Run.bat` also
finds Docker Desktop's `docker.exe` when it is not on `PATH`, and pauses with a
hint on errors (e.g. Docker Desktop not running).

Both containers use `restart: unless-stopped` and json-file log rotation
(5 x 20 MB). The viewer's nginx resolves `data-platform` per request, so
recreating the API container needs no viewer restart.

The DEM engine is bundled in `gis-data-platform/engine/` (the `heightmap-30m`
`terrainhub.py` + `scripts/`, including the Lerc1 encoder sources) and baked
into the API image at `/opt/heightmap-30m`, so fetch/build/verify jobs work in
the container with no bind mount. The composed deployment uses same-origin
viewer requests through nginx; set `VITE_PLATFORM_URL` only when the viewer is
hosted separately.

## What is baked into the images

| Image | Contents |
| --- | --- |
| `gis-data-platform` | Python API + pinned runtime `requirements.txt` (fastapi, uvicorn, numpy, Pillow, rasterio, requests, remotezip; test tools are in `requirements-dev.txt`, not in the image), the `engine/` DEM build toolchain with `lerc1enc` compiled from source, Leaflet 1.9.4 vendor assets (SHA-256 verified) |
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

## Tile serving (static, nginx)

Tiles are served by **nginx straight from the data volume** (mounted read-only
at `/data`) on both the platform gateway (:5000) and the viewer (:3100);
FastAPI only answers metadata, jobs and fallbacks. Through the :5000 gateway
(oha, 50 concurrent, 3,000 z12 Saudi tiles): ArcGIS Lerc **149,659 req/s, p50
0.22 ms**, WebP **149,444 req/s** - versus 631 req/s, p50 80 ms when FastAPI
answered :5000 directly. Measured on this host (oha, 50 concurrent, same 3,492 Riyadh tiles):

| Tile type | Before (FastAPI) | After (nginx static) |
|---|---:|---:|
| Terrain (MapLibre/Cesium) | 82 req/s, p50 775 ms (dynamic PNG) | **134,301 req/s, p50 0.25 ms** (WebP) |
| ArcGIS Lerc (ArcGIS/Cesium) | 646 req/s, p50 79 ms | **141,353 req/s, p50 0.24 ms** |

How it works:

* Every elevation job runs `fetch → build → verify → publish`.
  `publish` (`engine/scripts/publish_tiles.py`, also `terrainhub.py publish`)
  bakes the seamless edge fade that `/tiles/terrain` used to compute per
  request into `data/tiles/published/<version>/` (lossless WebP Terrarium +
  `flat.webp` fallback) and switches `published/current.json` atomically.
* **No server-side hillshade.** MapLibre and Cesium compute shading in the
  browser from the elevation tiles; the build writes no hillshade pyramid and
  the API has no hillshade routes / `imagery` key in `/tiles/terrain.json`.
* **One copy of the elevation on disk.** Publish hard-links every fully-valid
  raw tile into the new version and computes only the edge/no-data tiles.
  After `current.json` points at the new version it deletes the raw pyramid
  (`data/tiles/terrarium/<z>/` + `build_manifest.json`, so the next build is a
  clean full build), every older published version (only ONE version is
  kept) and any legacy `data/tiles/hillshade/`. `data/tiles/tilejson.json`
  and the ArcGIS Lerc pyramid are kept. A failed publish deletes nothing;
  `--keep-raw` keeps the raw pyramid (dev/tests).
* `/tiles/terrain.json` advertises the published, versioned (immutable) URLs
  when a published version exists, else the legacy dynamic route. Clients
  must re-read it after a new publish: the previous version's URLs are gone.
* nginx serves `/tiles/published/...`, `/arcgis/rest/services/<name>/ImageServer/tile/...`
  and `/tiles/imagery-sets/...` from disk (`sendfile`), falling back to the API
  only for metadata, tilemaps, imagery over-zoom and misses.
* The engine writes Terrarium as **lossless WebP** (`method=1, quality=25,
  exact=True`: −29 % bytes vs PNG, bit-exact) and every file **0644** so a
  non-root nginx can read it. On first start the API makes older 0600 tiles
  world-readable once (marker `data/.perms-world-readable-v1`;
  `GIS_FIX_TILE_PERMS=0` disables).
* Publishing consumes the raw pyramid, so a re-publish needs a rebuild
  first: `terrainhub.py publish` without raw tiles exits non-zero with a
  clear message and changes nothing (the live version keeps serving). To
  publish a build that still has its raw tiles:
  `docker compose exec data-platform python /opt/heightmap-30m/terrainhub.py publish --bbox 0 0 1 1 --root /app/data/tiles`
  (bbox is ignored by publish; the build's tilejson defines the area).

## Moving the data to another machine (automatic tiling)

`docker-volume-tool.bat` exports a **TRUTH** backup (DEMs + catalog + jobs, no
tiles) or a FULL one. After an import, just start the stack: on every start the
platform compares the saved areas with the tiles on disk and builds whatever is
missing or was made by an older engine, one job at a time:

- the area of the published elevation set (the one MapLibre/Cesium show) gets a
  full fetch/build/verify/publish when nothing valid is published;
- every other area gets a LERC-only build of its ArcGIS ImageServer
  (`terrainhub.py build --lerc-only`), which never replaces the published set.

The DEMs are already in the volume, so nothing is downloaded. Progress: a banner
in the viewer, the job list on `/fetch/`, or `GET /api/tiles/status`.
`POST /api/tiles/rebuild-missing` runs the check again. Builds stop when the
data disk has less than `GIS_AUTOBUILD_MIN_FREE_GB` (default 10) free;
`GIS_AUTOBUILD=0` turns the start-up check off.
