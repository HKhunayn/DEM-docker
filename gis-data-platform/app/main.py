from __future__ import annotations

import json
import math
import os
import re
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .catalog import catalog_items, delete_item, find_item, ingest_file, item_path, load_catalog
from .config import TILE_ROOT
from .fetch_api import router as fetch_router
from .imagery_api import router as imagery_router
from .terrain import TerrainSource

# Older engines wrote every tile 0600 (tempfile.mkstemp), which a non-root
# nginx serving the volume read-only cannot open (403). Once per data volume,
# make the served trees world-readable in the background (the marker file
# records that it ran; new files are written 0644 by the engine).
_PERMS_MARKER = ".perms-world-readable-v1"


def fix_tile_permissions(data_root: Path) -> int:
    marker = data_root / _PERMS_MARKER
    if marker.exists():
        return 0
    changed = 0
    for top in (data_root / "tiles", data_root / "imagery"):
        for dirpath, dirnames, filenames in os.walk(top):
            for name in dirnames:
                p = os.path.join(dirpath, name)
                try:
                    if os.stat(p).st_mode & 0o005 != 0o005:
                        os.chmod(p, 0o755)
                except OSError:
                    pass
            for name in filenames:
                p = os.path.join(dirpath, name)
                try:
                    if not os.stat(p).st_mode & 0o004:
                        os.chmod(p, 0o644)
                        changed += 1
                except OSError:
                    pass
    try:
        marker.write_text(f"{changed} files made world-readable", encoding="utf-8")
    except OSError:
        pass
    return changed


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if os.environ.get("GIS_FIX_TILE_PERMS", "1") == "1":
        threading.Thread(target=fix_tile_permissions, args=(TILE_ROOT.parent,),
                         name="fix-tile-perms", daemon=True).start()
    # Build whatever saved area has no (or stale) tiles, e.g. after importing
    # a TRUTH backup that carries only the DEMs + catalog.
    from . import autobuild
    if autobuild.enabled():
        autobuild.start(delay=float(os.environ.get("GIS_AUTOBUILD_DELAY_S", "5")))
    yield
    autobuild.stop()
    # Terminate running engine subprocesses on shutdown, if the jobs module
    # provides a hook (getattr: older jobs modules don't have one).
    try:
        from . import jobs
    except Exception:  # pragma: no cover - jobs module optional here
        return
    shutdown = getattr(jobs, "shutdown", None)
    if callable(shutdown):
        shutdown()


app = FastAPI(title="Local Open GIS Data Platform", version="0.1.0", lifespan=_lifespan)
app.include_router(fetch_router)
app.include_router(imagery_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)

# Derived tile pyramids + ArcGIS services live under the platform's own data
# root (data/tiles/), alongside data/dem/ (source GeoTIFFs) and data/catalog/.
# The heightmap-30m engine is only the build tool; it no longer owns storage.
TILE_DIR = TILE_ROOT
TERRAIN = TerrainSource(TILE_DIR)


class IngestRequest(BaseModel):
    path: str
    kind: str = Field(default="unknown", pattern=r"^[A-Za-z0-9_-]{1,32}$")
    title: str | None = None
    crs: str | None = None


class DeleteResponse(BaseModel):
    deleted: bool


def _finite(obj: Any) -> Any:
    """Recursively replace non-finite floats (inf/nan) with None.

    JSON has no infinity/NaN. A degenerate build (e.g. zero tiles written,
    so the tile statistics stay at math.inf) must not 500 the endpoints that
    serve metadata read from disk.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_finite(v) for v in obj]
    return obj


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


# ArcGIS service names: one plain path segment (no '.', '..', separators).
_SERVICE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


def _service_dir(name: str) -> Path:
    if not _SERVICE_NAME.match(name) or ".." in name:
        raise HTTPException(status_code=404, detail="ImageServer not found", headers=_NO_STORE)
    return TILE_DIR / "arcgis" / "rest" / "services" / name


def _read_json(path: Path) -> Any:
    """Parse a JSON file another process may be rewriting.
    Raises 503 (retry shortly) on a partial/unreadable file instead of 500."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status_code=503, detail="metadata is being rewritten, retry",
                            headers={"Retry-After": "2", "Cache-Control": "no-store"})


def _tile_file(relative: str) -> Path | None:
    candidate = (TILE_DIR / relative).resolve()
    try:
        candidate.relative_to(TILE_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "gis-data-platform", "version": app.version}


@app.get("/api/tiles/status")
def tiles_status() -> JSONResponse:
    """Are the saved areas tiled? (published Terrarium set + each area's
    ArcGIS LERC service) and the automatic rebuild's progress."""
    from . import autobuild
    response = JSONResponse(_finite(autobuild.status()))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/tiles/rebuild-missing", status_code=202)
def tiles_rebuild_missing() -> dict[str, Any]:
    """Queue builds for every saved area whose tiles are missing or stale."""
    from . import autobuild
    if not autobuild.start():
        raise HTTPException(status_code=409, detail="the automatic rebuild is already running")
    return {"started": True}


@app.get("/api/catalog")
def catalog(request: Request) -> dict[str, Any]:
    result = load_catalog()
    result["links"] = [
        {"rel": "self", "href": f"{_base(request)}/api/catalog"},
        {"rel": "items", "href": f"{_base(request)}/api/items"},
    ]
    return result


@app.get("/api/items")
def items(request: Request) -> dict[str, Any]:
    features = []
    for item in catalog_items():
        feature = dict(item)
        feature["assets"] = dict(feature.get("assets", {}))
        if "source" in feature["assets"]:
            feature["assets"]["source"] = dict(feature["assets"]["source"])
            feature["assets"]["source"]["href"] = f"{_base(request)}/api/assets/{item['id']}"
        features.append(feature)
    return {"type": "FeatureCollection", "features": features, "numberMatched": len(features), "numberReturned": len(features)}


@app.post("/api/ingest", status_code=201)
def ingest(body: IngestRequest, request: Request) -> dict[str, Any]:
    try:
        item = ingest_file(Path(body.path), body.kind, body.title, body.crs)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="source path does not exist")
    except PermissionError as exc:  # path outside the allowed ingest root
        raise HTTPException(status_code=403, detail=str(exc) or "path not allowed")
    except ValueError as exc:  # bad input (not a file, unsupported type, ...)
        raise HTTPException(status_code=400, detail=str(exc) or "invalid ingest request")
    item["assets"]["source"]["href"] = f"{_base(request)}/api/assets/{item['id']}"
    return item


@app.delete("/api/items/{item_id}", response_model=DeleteResponse)
def remove(item_id: str) -> DeleteResponse:
    if not delete_item(item_id):
        raise HTTPException(status_code=404, detail="item not found")
    return DeleteResponse(deleted=True)


@app.get("/api/assets/{item_id}")
def asset(item_id: str) -> FileResponse:
    item = find_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    path = item_path(item)
    if not path.exists():
        raise HTTPException(status_code=410, detail="asset is missing")
    return FileResponse(path, media_type=item.get("mediaType"), filename=item["filename"])


@app.get("/api/services")
def services(request: Request) -> dict[str, Any]:
    result: list[dict[str, Any]] = []
    service_root = TILE_DIR / "arcgis" / "rest" / "services"
    if service_root.is_dir():
        for meta in sorted(service_root.glob("*/ImageServer.json")):
            name = meta.parent.name
            result.append({"name": name, "url": f"/arcgis/rest/services/{name}/ImageServer"})
    return {"services": result}


# Tiles change whenever a new build runs ("last build wins"): never
# `immutable`. Dynamic terrain URLs carry a per-build ?v= token, so a day is
# safe; ArcGIS tile URLs are unversioned, so they get a shorter max-age (the
# FileResponse ETag/Last-Modified make revalidation cheap). Errors: no-store.
_TILE_CACHE = "public, max-age=86400"
_ARCGIS_TILE_CACHE = "public, max-age=3600"
_NO_STORE = {"Cache-Control": "no-store"}
_DEFAULT_TILEJSON: dict[str, Any] = {"tilejson": "3.0.0", "name": "local terrain", "minzoom": 0, "maxzoom": 13,
                                     "encoding": "terrarium", "tileSize": 256}


@app.get("/tiles/terrain/{z}/{x}/{y}.png")
def terrain(z: int, x: int, y: int) -> Response:
    if z < 0 or z > 24 or not (0 <= x < 2 ** z) or not (0 <= y < 2 ** z):
        raise HTTPException(status_code=404, detail="terrain tile not found", headers=_NO_STORE)
    # Seamless field: real heights inside coverage, edge heights eased to a
    # flat base outside. Above maxzoom -> 404 so clients over-zoom the parent.
    payload = TERRAIN.tile_png(z, x, y)
    if payload is None:
        raise HTTPException(status_code=404, detail="above terrain maxzoom", headers=_NO_STORE)
    response = Response(content=payload, media_type="image/png")
    response.headers["Cache-Control"] = _TILE_CACHE
    return response


# ---- published (static, nginx-served) tile sets ---------------------------
# The engine's publish step bakes the current build into
# data/tiles/published/<version>/ (seamless Terrarium WebP + flat.webp
# fallback) and points current.json at it. In the composed deployment nginx
# serves those files directly; the route below is the same contract for
# clients that talk to the API port directly. There is no server hillshade:
# MapLibre and Cesium shade the elevation tiles in the browser. After a
# publish the raw terrarium/ pyramid is gone (single copy on disk), so the
# dynamic /tiles/terrain route only matters when nothing is published.
_PUBLISHED = TILE_DIR / "published"
_VERSION_NAME = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{8}$")
_IMMUTABLE = "public, max-age=31536000, immutable"


def published_current() -> dict[str, Any] | None:
    """current.json of the live published version, if valid and present."""
    try:
        data = json.loads((_PUBLISHED / "current.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not _VERSION_NAME.match(str(data.get("version", ""))):
        return None
    if not (_PUBLISHED / data["version"] / "meta.json").is_file():
        return None
    return data


def _published_file(version: str, z: int, x: int, y: int) -> Path:
    if not _VERSION_NAME.match(version) or z < 0 or z > 24 or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(status_code=404, detail="tile not found", headers=_NO_STORE)
    root = _PUBLISHED / version
    if not (root / "meta.json").is_file():
        raise HTTPException(status_code=404, detail="published version not found", headers=_NO_STORE)
    path = root / "terrain" / str(z) / str(x) / f"{y}.webp"
    if path.is_file():
        return path
    return root / "flat.webp"


@app.get("/tiles/published/{version}/terrain/{z}/{x}/{y}.webp")
def published_terrain(version: str, z: int, x: int, y: int) -> FileResponse:
    response = FileResponse(_published_file(version, z, x, y), media_type="image/webp")
    response.headers["Cache-Control"] = _IMMUTABLE
    return response


@app.get("/tiles/terrain.json")
def terrain_tilejson(request: Request) -> JSONResponse:
    path = TILE_DIR / "tilejson.json"
    data: Any = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = dict(_DEFAULT_TILEJSON)
    except (OSError, ValueError):
        data = None  # the engine is rewriting it right now
    if not isinstance(data, dict):
        raise HTTPException(status_code=503, detail="terrain metadata is being rewritten, retry",
                            headers={"Retry-After": "2", "Cache-Control": "no-store"})
    meta = TERRAIN.meta()
    # Advertise what is actually served (degenerate stats / invalid maxzoom
    # fall back to the default) and only finite bounds.
    data["maxzoom"] = meta.maxzoom
    if meta.bounds is not None:
        data["bounds"] = list(meta.bounds)
    else:
        data.pop("bounds", None)
    # Versioned URLs: the token changes with every build (and with the tile
    # algorithm revision), so browsers never reuse stale tiles - including
    # copies cached as `immutable` by earlier platform versions or saved
    # without CORS headers by a non-CORS request.
    published = published_current()
    if published is not None:
        # Static, versioned tile set (nginx-served; immutable URLs).
        ver = published["version"]
        data["tiles"] = [f"{_base(request)}/tiles/published/{ver}/terrain/{{z}}/{{x}}/{{y}}.webp"]
        data["maxzoom"] = published.get("maxzoom", data.get("maxzoom"))
        if isinstance(published.get("bounds"), list):
            data["bounds"] = published["bounds"]
        data["format"] = "webp"
        data["published"] = ver
        response = JSONResponse(_finite(data))
        response.headers["Cache-Control"] = "no-store"
        return response
    version = TERRAIN.version()
    data["tiles"] = [f"{_base(request)}/tiles/terrain/{{z}}/{{x}}/{{y}}.png?v={version}"]
    data["format"] = "png"
    response = JSONResponse(_finite(data))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/arcgis/rest/services/{name}/ImageServer")
def arcgis_metadata(name: str) -> JSONResponse:
    service = _service_dir(name)
    path = _tile_file(f"arcgis/rest/services/{service.name}/ImageServer.json")
    if not path:
        raise HTTPException(status_code=404, detail="ImageServer not found", headers=_NO_STORE)
    data = _read_json(path)
    if not isinstance(data, dict):
        raise HTTPException(status_code=503, detail="metadata is being rewritten, retry",
                            headers={"Retry-After": "2", "Cache-Control": "no-store"})
    # Real ImageServers expose `extent`; CesiumJS's
    # ArcGISTiledElevationTerrainProvider requires it (reads extent.xmax).
    if "extent" not in data and "fullExtent" in data:
        data["extent"] = data["fullExtent"]
    return JSONResponse(_finite(data))


@app.get("/arcgis/rest/services/{name}/ImageServer/tilemap/{level}/{row}/{col}/{height}/{width}")
def arcgis_tilemap(name: str, level: int, row: int, col: int, height: int, width: int) -> JSONResponse:
    # The service metadata advertises the "Tilemap" capability; CesiumJS (and
    # the ArcGIS SDK) request this before any tile. A 404 here makes CesiumJS
    # reject every terrain tile, leaving an empty globe.
    if not (0 <= level <= 24 and 0 < height <= 256 and 0 < width <= 256 and row >= 0 and col >= 0):
        raise HTTPException(status_code=400, detail="invalid tilemap request")
    service = _service_dir(name)
    root = _tile_file(f"arcgis/rest/services/{service.name}/ImageServer.json")
    if not root:
        raise HTTPException(status_code=404, detail="ImageServer not found", headers=_NO_STORE)
    try:
        stamp = root.stat().st_mtime_ns
    except OSError:
        stamp = 0
    key = (service.name, stamp, level, row, col, height, width)
    data = _tilemap_cache_get(key)
    if data is None:
        data = _scan_tilemap(root.parent / "tile" / str(level), row, col, height, width)
        _tilemap_cache_put(key, data)
    return JSONResponse({"adjusted": False, "location": {"left": col, "top": row, "width": width, "height": height}, "data": data})


# Tilemap answers keyed by ImageServer.json mtime (rewritten when a LERC
# build completes), so repeated Cesium/ArcGIS requests don't re-scan disk.
_TILEMAP_CACHE: "OrderedDict[tuple, list[int]]" = OrderedDict()
_TILEMAP_LOCK = threading.Lock()
_TILEMAP_MAX = 1024


def _tilemap_cache_get(key: tuple) -> list[int] | None:
    with _TILEMAP_LOCK:
        data = _TILEMAP_CACHE.get(key)
        if data is not None:
            _TILEMAP_CACHE.move_to_end(key)
        return data


def _tilemap_cache_put(key: tuple, data: list[int]) -> None:
    with _TILEMAP_LOCK:
        _TILEMAP_CACHE[key] = data
        while len(_TILEMAP_CACHE) > _TILEMAP_MAX:
            _TILEMAP_CACHE.popitem(last=False)


def _scan_tilemap(level_dir: Path, row: int, col: int, height: int, width: int) -> list[int]:
    """Presence bitmap for rows [row, row+height) x cols [col, col+width).
    Only the requested row directories are listed (never the whole level)."""
    data = [0] * (height * width)
    for i, r in enumerate(range(row, row + height)):
        try:
            entries = os.scandir(level_dir / str(r))
        except OSError:
            continue  # row absent (or not a directory)
        with entries:
            for entry in entries:
                name = entry.name
                if not name.isdigit():
                    continue
                c = int(name)
                if col <= c < col + width:
                    try:
                        if entry.is_file():
                            data[i * width + (c - col)] = 1
                    except OSError:
                        pass
    return data


@app.get("/arcgis/rest/services/{name}/ImageServer/tile/{z}/{row}/{col}")
def arcgis_tile(name: str, z: int, row: int, col: int) -> FileResponse:
    service = _service_dir(name)
    if z < 0 or row < 0 or col < 0:
        raise HTTPException(status_code=404, detail="ArcGIS tile not found", headers=_NO_STORE)
    path = _tile_file(f"arcgis/rest/services/{service.name}/tile/{z}/{row}/{col}")
    if not path:
        raise HTTPException(status_code=404, detail="ArcGIS tile not found", headers=_NO_STORE)
    response = FileResponse(path, media_type="application/octet-stream")
    response.headers["Cache-Control"] = _ARCGIS_TILE_CACHE
    return response


@app.get("/")
def root() -> dict[str, str]:
    return {"service": app.title, "docs": "/docs", "health": "/health", "catalog": "/api/catalog", "fetch": "/fetch/", "imagery": "/api/imagery"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8787, reload=False)
