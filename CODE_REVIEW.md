# Code review: DEM-docker (whole app)

Date: 2026-10-28. Scope: `gis-data-platform` (API, engine, tests), `gis-viewers`, Docker/compose/nginx, and the run scripts (~7k lines).
Method: three independent reviewers (backend, engine, viewers+deploy), plus the test suite, pyflakes, and runtime checks of the critical claims against the real Saudi data.

Legend: ✅ = verified at runtime, 📖 = verified by reading the code, ❔ = likely, not proven.

---

## 🔴 Critical: elevation data is wrong (engine)

| # | Issue | Where | Evidence | Fix |
|---|---|---|---|---|
| E1 ✅ | **Each source erases the previous one.** `reproject()` resets the whole `dst` to NODATA on every call, so in a tile covering several 1° COGs only the last source survives. | `engine/scripts/build_tiles.py` `build_tile` loop | Real Saudi z13 tiles on 1° lines are **49k–65k of 65,536 px transparent** while their neighbours have 0. A fresh rebuild of z13/5142/3520 has an empty left half. At z≤8 almost the whole tile is blank. | Reproject each source into a scratch array and merge only the valid pixels, or use `init_dest_nodata=False`. |
| E2 ✅ | **Downsampled reads are misplaced.** The `read_patch` transform is not rescaled when `out_shape` ≠ window size, so the data is squeezed into a corner. | `build_tiles.py` `read_patch` | Single source, compared with the COG: **z6 9% coverage / 226 m median error, z8 26% / 98 m, z10 69% / 48 m**; z11 0.8 m, z13 0.2 m. | `transform * Affine.scale(win.width/w, win.height/h)` |
| E3 📖 | Resume trusts any existing tile and has no engine version or source fingerprint, so after the fixes a normal run would **keep every broken tile**. | `build_tiles.py`, `build_lerc.py` | — | Write a `build_manifest.json` with ENGINE_VERSION + a source hash; force a rebuild on mismatch. |
| E4 📖 | `verify_tiles` samples only one point at the deepest zoom and passed with both bugs present. | `verify_tiles.py` | — | Sample every zoom plus points on 1° lines; fail on alpha=0 pixels where the source has data. |

**Impact:** all of z0–10, and every z11–13 tile touching a 1° line, in the Saudi pyramid (Terrarium, hillshade and LERC) needs a rebuild. The running LERC pass is building on the same bug.

## 🔴 Critical / high: security (only matters when the stack is reachable, e.g. EC2)

| # | Issue | Where | Fix |
|---|---|---|---|
| S1 📖 | **Any file on the server can be read.** `POST /api/ingest {path}` copies any server file and `GET /api/assets/{id}` serves it. No auth, and the port is published on 0.0.0.0. | `app/main.py` ingest, `app/catalog.py ingest_file`, compose ports | Allow only an `incoming/` dir (or use upload), or disable it via an env flag. |
| S2 📖 | No auth on any POST/DELETE (jobs, delete areas/imagery); CORS is `*` with all methods, so any web page can drive localhost. | `app/main.py` CORS, all mutating routes | Token dependency on writes, CORS limited to the viewer origin, publish as `127.0.0.1:8788:8787`. |
| S3 📖 | Stored XSS: server data (catalog `title`, names, provider labels, errors) inserted with `innerHTML`. | `gis-viewers/src/main.js`, `app/static/fetch/index.html` | `textContent` / `escapeHtml`; validate `title`. |
| S4 📖 | `serve.py` path filter misses `..\` on a Windows host (Docker/Linux not affected). | `engine/scripts/serve.py` translate_path | Reject os.sep in path segments plus a `commonpath` check. |

## 🟠 High: reliability

| # | Issue | Where | Fix |
|---|---|---|---|
| R1 📖 | **The terrain API can OOM the container.** `TerrainSource._field` builds a mosaic of the whole bbox per zoom in memory (Saudi z13 ≈ 116 GB, z10 ≈ 1.8 GB plus a float64 blur) and caches every zoom behind a global lock. One viewer zoom can kill the build container. | `app/terrain.py` | Compute per tile with a 1-tile apron and edge profiles; cap the size; per-key locks. |
| R2 📖 | Job state is memory-only: a restart loses history, and areas stay `building` forever. | `app/jobs.py` | Persist jobs and logs to `data/jobs/`; mark them `interrupted` at startup. |
| R3 📖 | An exception outside the try in `_run_job` leaves `_CURRENT_ID` set, so every new job gets 409 until restart. | `app/jobs.py` | try/finally that always clears it. |
| R4 📖 | No cancel and no timeout for the engine subprocess; orphans in local mode. | `app/jobs.py` | Cancel endpoint (process group), shutdown hook, lock file. |
| R5 📖 | One tile exception (bad COG, encoder error, ENOSPC) aborts a whole multi-hour build; only BrokenProcessPool is handled. | `engine/scripts/pool_runner.py` | Catch per tile, count and report, abort over a threshold. |
| R6 📖 | Fetch: no socket timeout (can hang forever), no retry on 5xx/429, no file validation; FABDEM writes non-atomically. | `fetch_dem.py`, `fetch_fabdem.py` | Timeouts, retry policy, `.part` + validate + replace. |
| R7 📖 | Fetch UI stops polling for good after one failed poll; Go stays disabled. | `app/static/fetch/index.html` | Backoff retry with "reconnecting… last update Ns ago"; treat 404 as job lost. |
| R8 📖 | No `restart: unless-stopped`, no `init: true`, no log rotation in compose. | `docker-compose.yml` | Add them. |
| R9 📖 | `gis-data-platform/Run.sh` is actually a Windows batch file. | `gis-data-platform/Run.sh` | Rewrite as POSIX shell. |

## 🟡 Medium

- **Engine:** `terrainhub build` uses the manifest bbox and ignores `--bbox` · hillshade uses Mercator metres (slopes too small by 1/cos(lat), +10% error at 25°N), edge seams, and NODATA treated as 0 m · the LERC pass re-reprojects every tile (≈2× build time) · `lerc1enc` runs a subprocess and tempdir per tile with no timeout, and stderr is lost · `serve.py` caches 404s for a year (`immutable`).
- **API:** inf/NaN statistics not treated as degenerate, and a null maxzoom causes a 500 · failed build only rolls back `tilejson.json` (tiles end up mixed), and a verify-only failure rolls back a good build · tile cache bounded by count, not bytes · tilejson read without error handling (a partial write gives a 500) · the job test can launch the real engine (test isolation).
- **Viewers:** Reload doesn't refresh terrain sources · viewer start-up errors are uncaught or misreported · `Promise.all` in loadState lets one optional 500 take down everything; no timeouts · fetch page has silent failures and no busy state on save/delete · polls download the entire job log every 1.5 s · Cesium decode lacks `premultiplyAlpha:"none"` and never closes bitmaps; FIFO cache.
- **Deploy:** nginx resolves `data-platform` once (502 after the container is recreated), missing security headers/gzip/caching and the 60 s proxy timeout · container runs as root, unpinned images and deps, Leaflet downloaded without a hash · Run.bat `up --build`/`restart` can kill a running job without warning.

## ⚪ Low (selection)

lat/lon not clamped in `lonlat_to_tile` (lat ±90 crashes or explodes, lon 180 overflows, antimeridian bbox → 0 tiles) · NODATA RGB decodes as 0 m in MapLibre · pool workers never grow back after shrinking · fixed `.part` names · `arcgis_tilemap` scans whole directories · `probe_providers` timeout not caught · imagery downloader trusts any 200 · delete of an imagery set while a job is running · fetch page: plain drag can't pan · Node 18+ message wrong (Vite needs 20.19+) · `.gitattributes` missing `*.sh eol=lf` · `.pyc` files committed to git · unused imports (pyflakes: 6).

## Test suite

`pytest`: **29 passed, 3 failed**. Two failures are the load tests, which need `aiohttp` (environment). One is real: `test_terrain_tiles_are_seamless_and_fade_to_base` expects `/tiles/terrain/11/0/0.png` → 404 and gets 200.
Missing coverage: the critical engine bugs (no multi-source or low-zoom test), ingest restriction, ArcGIS routes, job error paths, large-bbox terrain memory.

## Verified OK

No subprocess injection (argv list plus regex/Literal validation) · no SSRF in the imagery proxy (fixed URL templates) · tile/ArcGIS/imagery routes are safe from path traversal (`relative_to` checks) · API handlers are sync (no event-loop blocking) · Terrarium encode/decode math and hillshade gradient signs are correct.

## Recommended order

1. **Stop the Saudi job; fix E1 + E2 and add a regression test** (two sources across a 1° line, plus a z8 accuracy check); add the engine fingerprint (E3) and the stronger verify (E4); rebuild with `--force`.
2. R1 (terrain API OOM), because it can kill the rebuild.
3. S1–S3 before any EC2 exposure.
4. R2–R9 reliability.
5. Medium and low items.
