# Local Open GIS Data Platform

Two projects are included:

- `gis-data-platform/` — one Python package, two apps (`GIS_ROLE`): the **data-platform** (downloads, catalog, ingest) and the **engine** (tile builds + serving).
- `gis-viewers/` — one viewer application with MapLibre GL JS, ArcGIS Maps SDK for JavaScript, and CesiumJS adapters.

The platform is open-source friendly and keeps original files as the source of truth. It publishes interoperable derivatives instead of forcing every client to read every source format directly.

## Three apps, one data volume

Run only what you need: each app starts on its own and they share the named
`gis-data` volume (DEMs, catalog, tiles).

```text
                        your ArcGIS / MapLibre / Cesium apps
                                       |
                                       v
 +------------------+  hand-off  +------------------------------------+
 | data-platform    |----------->| engine            host :5000       |
 | host :5100       | POST       |  nginx :80  - tile files (sendfile) |
 | DOWNLOADS only   | engine:8787|  FastAPI    - ImageServer metadata, |
 | fetch page,      | /api/jobs  |    tilemap, terrain.json, catalog,  |
 | saved areas,     |            |    tile status, BUILD jobs          |
 | DEM + imagery    |            |  builds: DEM -> lossless WebP +     |
 | downloads        |            |    ArcGIS Lerc (never downloads)    |
 +--------+---------+            +-----------------+------------------+
          |   writes dem/, imagery/, catalog        |  reads dem/, writes tiles/
          +-------------------+---------------------+
                              v
                    volume dem-docker_gis-data
                              ^
 +------------------+         |  (no volume: proxies)
 | viewer :3100     |  /api /tiles /arcgis /health -> engine
 | MapLibre/ArcGIS/ |  /fetch/ /vendor/            -> data-platform
 | Cesium           |
 +------------------+
```

| App | Host port (`.env`) | Start alone | Does |
| --- | --- | --- | --- |
| `engine` | `GIS_ENGINE_PORT` = **5000** | `docker compose up -d engine` | serves every tile + metadata to apps, builds tiles (check → build → verify → publish); builds missing / newly downloaded areas on start-up and every 60 s while idle |
| `data-platform` | `GIS_DATA_PORT` = **5100** | `docker compose up -d data-platform` | fetch page, saved areas, DEM and imagery downloads, ingest; hands each finished DEM download to the engine |
| `viewer` | `GIS_VIEWER_PORT` = **3100** | `docker compose up -d viewer` (starts the engine too) | optional web viewer; talks to the engine only |

**Point your apps at the engine:**

- ArcGIS elevation: `http://<server-ip>:5000/arcgis/rest/services/<area>/ImageServer`
- Terrarium WebP TileJSON (MapLibre/Cesium/deck.gl): `http://<server-ip>:5000/tiles/terrain.json`
- API docs: `http://<server-ip>:5000/docs`

Download new areas on the data-platform's fetch page:
`http://<server-ip>:5100/fetch/` (also reachable through the viewer at
`http://<server-ip>:3100/fetch/`). Optional web viewer: `http://<server-ip>:3100/`.

### Hand-off: download -> build

1. The fetch page starts a **download job** on the data-platform
   (`POST /api/jobs`, steps `fetch` → `handoff`). The source DEMs land in the
   volume (`dem/`) and the saved area becomes `downloaded` (pending a build).
2. `handoff` asks the engine (`ENGINE_URL`, default `http://engine:8787`) to
   build the area: `POST /api/jobs {"name": <area>}` on the engine.
   - engine idle: it starts the build (`check` → `build` → `verify` → `publish`);
   - engine busy: the area is queued and built right after the running build;
   - **engine not running**: the area stays `downloaded` with the message
     *"engine not running - it builds this area when it starts"*; the engine
     builds it by itself on its next start (start-up + periodic check).
3. The fetch page follows the whole pipeline - download → handed to engine →
   check → build → verify → publish, with step, elapsed time and log - through
   the platform's `/api/engine/...` proxy (no CORS, no internal address in the
   browser). When the engine is down it says so and keeps waiting.

The engine never downloads: its first step (`terrainhub.py sources`) only
checks that the area's DEMs are on the volume and fails with a clear message
when they are not ("download the area with the data-platform first"). An area
whose build failed is not retried by the periodic check until it is handed
over again (or `POST /api/tiles/rebuild-missing`).

Both apps write the shared volume: `catalog/catalog.json` edits hold an
exclusive `flock` on `catalog/.catalog.lock` (load → modify → atomic replace);
each app keeps its own job history and engine-process lock
(`jobs/` + `.job.lock` for the data-platform, `engine-jobs/` +
`.engine-job.lock` for the engine).

## Quick start with Docker Compose

```bash
./Run.sh up                     # all three; Windows: Run.bat up
./Run.sh up engine              # serve + build only (no downloads)
./Run.sh up data-platform       # downloads only
./Run.sh up viewer              # viewer (+ engine)
./Run.sh down engine            # stop one app; ./Run.sh down = all
```

`Run.bat` / `Run.sh` create `.env` from `.env.example` when it is missing,
wait for each started app's `/health` and print its URLs. With no Docker
installed `Run.bat` falls back to a local dev run (engine API via
`gis-data-platform\.venv`, viewer via `npm run dev`; for local downloads run a
second API with `GIS_ROLE=platform`). Other commands: `logs`, `build`,
`restart`, `status`, each optionally followed by service names. Ports:
`GIS_ENGINE_PORT`, `GIS_DATA_PORT`, `GIS_VIEWER_PORT` in `.env` (the old
`GIS_PLATFORM_PORT` still sets the engine port when `GIS_ENGINE_PORT` is not
set). Upgrading from the single-platform layout: `Run.bat up` passes
`--remove-orphans`, which removes the old `gateway` container (the engine now
owns port 5000).

Stopping/recreating the engine interrupts a running build, the data-platform a
running download (both resume: the engine rebuilds an interrupted full build
by itself when it starts). The launchers check `/api/jobs/current` on the
affected apps first and ask for confirmation; pass `-y` to skip the prompt.
`Run.bat` also finds Docker Desktop's `docker.exe` when it is not on `PATH`,
and pauses with a hint on errors (e.g. Docker Desktop not running).

All containers use `restart: unless-stopped` and json-file log rotation
(5 x 20 MB). The viewer's nginx resolves `engine` / `data-platform` per
request, so recreating either needs no viewer restart; with the data-platform
stopped, `/fetch/` on the viewer shows a page saying so.

The DEM engine is bundled in `gis-data-platform/engine/` (the `heightmap-30m`
`terrainhub.py` + `scripts/`, including the Lerc1 encoder sources) and baked
into both images at `/opt/heightmap-30m`. The composed deployment uses
same-origin viewer requests through nginx; set `VITE_PLATFORM_URL` only when
the viewer is hosted separately.

## What is baked into the images

| Image | Contents |
| --- | --- |
| `local-gis-data-platform` (Dockerfile target `platform`, `GIS_ROLE=platform`) | Python API + pinned runtime `requirements.txt` (fastapi, uvicorn, numpy, Pillow, rasterio, requests, remotezip; test tools are in `requirements-dev.txt`, not in the image), the `engine/` DEM toolchain with `lerc1enc` compiled from source, Leaflet 1.9.4 vendor assets (SHA-256 verified) |
| `local-gis-engine` (target `engine`, `GIS_ROLE=engine`) | the same layers + Debian nginx (`nginx/nginx.conf`, `nginx/engine.conf`, `nginx/gis-tiles.conf`) + tini running uvicorn and nginx (`engine-entrypoint.sh`: if either exits, the container exits and Docker restarts it) |
| `local-gis-viewer` | Vite build of the MapLibre / ArcGIS / CesiumJS viewer, served by nginx with `/api`, `/tiles`, `/arcgis`, `/health` proxied to the engine and `/fetch`, `/vendor` to the data-platform |

Runtime data (DEM tiles, pyramids, catalog) lives in the named `gis-data`
volume and is never copied into an image.

## Local development

`Run.bat` runs the stack locally when Docker is absent: it creates
`gis-data-platform\.venv` with `requirements.txt` on first run, then starts the
engine API and the Vite dev server. The original per-project commands remain
available in each project's `README.md` and `Run.bat` files.

## Tile serving (static, nginx)

Tiles are served by the **engine's nginx straight from the data volume**
(mounted read-only at `/data`) on :5000 (the viewer on :3100 proxies to it);
FastAPI only answers metadata, jobs and fallbacks. Through the :5000 nginx
(oha, 50 concurrent, 3,000 z12 Saudi tiles): ArcGIS Lerc **149,659 req/s, p50
0.22 ms**, WebP **149,444 req/s** - versus 631 req/s, p50 80 ms when FastAPI
answered :5000 directly. Measured on this host (oha, 50 concurrent, same 3,492 Riyadh tiles):

| Tile type | Before (FastAPI) | After (nginx static) |
|---|---:|---:|
| Terrain (MapLibre/Cesium) | 82 req/s, p50 775 ms (dynamic PNG) | **134,301 req/s, p50 0.25 ms** (WebP) |
| ArcGIS Lerc (ArcGIS/Cesium) | 646 req/s, p50 79 ms | **141,353 req/s, p50 0.24 ms** |

How it works:

* Every engine build runs `check → build → verify → publish` (the
  data-platform's download job runs `fetch` before and hands the area over).
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
  `docker compose exec engine python /opt/heightmap-30m/terrainhub.py publish --bbox 0 0 1 1 --root /app/data/tiles`
  (bbox is ignored by publish; the build's tilejson defines the area).

## Moving the data to another machine (automatic tiling)

`docker-volume-tool.bat` exports a **TRUTH** backup (DEMs + catalog + jobs, no
tiles) or a FULL one. After an import, just start the engine: on every start
(and every `GIS_AUTOBUILD_INTERVAL_S`, default 60 s, while idle) the engine
compares the saved areas with the tiles on disk and builds whatever is missing,
was made by an older engine or was downloaded by the data-platform and not
built yet, one job at a time:

- areas downloaded by the data-platform (state `downloaded`) and full builds an
  engine restart cut short get a full check/build/verify/publish first;
- the area of the published elevation set (the one MapLibre/Cesium show) gets a
  full check/build/verify/publish when nothing valid is published;
- every other area gets a LERC-only build of its ArcGIS ImageServer
  (`terrainhub.py build --lerc-only`), which never replaces the published set.

The DEMs are already in the volume, so nothing is downloaded. Progress: a banner
in the viewer, `GET /api/tiles/status` or `/api/jobs/current` on the engine
(:5000), or the engine line on the fetch page.
`POST /api/tiles/rebuild-missing` runs the check again. Builds stop when the
data disk has less than `GIS_AUTOBUILD_MIN_FREE_GB` (default 10) free;
`GIS_AUTOBUILD=0` turns the automatic builds off.
