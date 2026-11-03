"""Data-platform home page + data library (conversion status).

- ``/``          home: two big buttons - Download data (/fetch/) and
                 Data library (/library/) - engine status, public URLs.
- ``/library/``  every saved elevation area: source DEMs on disk, its ArcGIS
                 (Lerc ImageServer) and MapLibre/Cesium (Terrarium WebP)
                 outputs - state, percent converted, tiles, size, live
                 progress - and "Convert now" (only what is missing); imagery
                 sets with their size.

The tile counts come from the ENGINE (it owns the tiles; GET
engine:8787/api/conversion on the compose network, cached + measured in the
background there). The data-platform only adds what it owns: the source DEMs
of each area and the imagery sets. Everything here is local: the data-platform
port is bound to 127.0.0.1 by default (GIS_DATA_BIND).
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import config, handoff, imagery, jobs
from .catalog import list_areas
from .config import DATA_ROOT, DEM_ROOT
from .tilestats import Measurer, dir_signature, mtime_ns

page_router = APIRouter()
router = APIRouter()

STATIC = Path(__file__).resolve().parent / "static"
HOME_PAGE = STATIC / "home" / "index.html"
LIBRARY_PAGE = STATIC / "library" / "index.html"

ENGINE_DOWN_CONVERT = ("the engine is not running - conversion happens in the engine. Start it "
                       "(Run.bat up engine / docker compose up -d engine); it also converts missing "
                       "areas by itself when it starts")

_LAST_ENGINE: dict[str, Any] = {"payload": None, "t": None}
_LAST_LOCK = threading.Lock()
_IMAGERY_MEASURER: Measurer | None = None


def _imagery_measurer() -> Measurer:
    global _IMAGERY_MEASURER
    if _IMAGERY_MEASURER is None:
        _IMAGERY_MEASURER = Measurer(DATA_ROOT / "stats" / "imagery-stats.json")
    return _IMAGERY_MEASURER


def _page(path: Path) -> FileResponse:
    response = FileResponse(path, media_type="text/html; charset=utf-8")
    response.headers["Cache-Control"] = "no-cache"
    return response


@page_router.get("/", include_in_schema=False)
def home_page() -> FileResponse:
    return _page(HOME_PAGE)


@page_router.get("/library", include_in_schema=False)
def library_redirect() -> RedirectResponse:
    return RedirectResponse("/library/", status_code=307)


@page_router.get("/library/", include_in_schema=False)
@page_router.get("/library/index.html", include_in_schema=False)
def library_page() -> FileResponse:
    return _page(LIBRARY_PAGE)


# ---- source DEMs ------------------------------------------------------------------

def dem_filename(provider: str, lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    if provider == "fabdem":
        return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}_FABDEM_V1-2.tif"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM.tif"


_MISSING_AT_SOURCE = re.compile(r"MISSING \S*?([A-Za-z0-9_-]+\.tif)\S* \(not at source")
_MISSING_FABDEM = re.compile(r"missing (\S+\.tif) \(may be ocean")
_UNAVAILABLE_CACHE: dict[str, tuple[Any, set[str]]] = {}


def _unavailable_cells(name: str) -> set[str]:
    """DEM files the last download of this area reported as not existing at
    the source (sea / void cells): never downloadable, not 'missing'."""
    latest = next((j for j in jobs.list_jobs() if j.get("name") == name and j.get("mode") == "download"
                   and j.get("state") == "done"), None)
    if not latest:
        return set()
    path = jobs.JOBS_ROOT / f"{latest['id']}.log"
    stamp = (latest["id"], mtime_ns(path))
    cached = _UNAVAILABLE_CACHE.get(name)
    if cached and cached[0] == stamp:
        return cached[1]
    out: set[str] = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _MISSING_AT_SOURCE.search(line) or _MISSING_FABDEM.search(line)
                if m:
                    out.add(m.group(1))
    except OSError:
        pass
    _UNAVAILABLE_CACHE[name] = (stamp, out)
    return out


def source_status(area: dict[str, Any]) -> dict[str, Any]:
    """The 1-degree source DEM files the area needs vs. those on disk."""
    provider = area.get("provider") or "copernicus"
    try:
        minlon, minlat, maxlon, maxlat = (float(v) for v in area["bbox"])
    except (KeyError, TypeError, ValueError):
        return {"needed": 0, "present": 0, "missing": 0, "bytes": 0, "missingFiles": []}
    needed = present = nbytes = 0
    missing: list[str] = []
    for lat in range(int(math.floor(minlat)), int(math.floor(maxlat)) + 1):
        for lon in range(int(math.floor(minlon)), int(math.floor(maxlon)) + 1):
            needed += 1
            fname = dem_filename(provider, lat, lon)
            try:
                size = os.stat(DEM_ROOT / fname).st_size
            except OSError:
                size = 0
            if size > 0:
                present += 1
                nbytes += size
            else:
                missing.append(fname)
    unavailable = _unavailable_cells(str(area.get("name"))) & set(missing)
    downloadable = [f for f in missing if f not in unavailable]
    return {"provider": provider, "needed": needed, "present": present, "missing": len(missing),
            "unavailable": len(unavailable), "downloadable": len(downloadable),
            "missingFiles": downloadable[:20], "bytes": nbytes,
            "state": "complete" if not missing else ("complete-at-source" if not downloadable else
                                                      ("partial" if present else "missing"))}


# ---- imagery sets -------------------------------------------------------------------

_TILE_EXT = (".jpg", ".jpeg", ".png", ".webp")


def _measure_imagery(root: Path) -> dict[str, int]:
    tiles = files = nbytes = 0
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            try:
                size = os.stat(os.path.join(dirpath, n)).st_size
            except OSError:
                continue
            files += 1
            nbytes += size
            if n.lower().endswith(_TILE_EXT):
                tiles += 1
    return {"tiles": tiles, "files": files, "bytes": nbytes}


def imagery_status(public: str) -> list[dict[str, Any]]:
    out = []
    for meta in imagery.list_sets():
        name = str(meta.get("name") or "")
        if not name:
            continue
        root = imagery.set_dir(name)
        sig = [mtime_ns(root / "imagery.json"), dir_signature(root)]
        busy = jobs.imagery_set_busy(name)
        stats, info = _imagery_measurer().get(f"imagery:{name}", sig, lambda r=root: _measure_imagery(r),
                                              max_age=10.0 if busy else None)
        out.append({
            "name": name, "provider": meta.get("provider"), "label": meta.get("label") or meta.get("providerLabel"),
            "bbox": meta.get("bbox"), "minzoom": meta.get("minzoom"), "maxzoom": meta.get("maxzoom"),
            "state": meta.get("state") or ("downloading" if busy else "done"), "downloading": busy,
            "expectedTiles": meta.get("tiles") or meta.get("tileCount"),
            "tiles": stats.get("tiles") if stats else None, "bytes": stats.get("bytes") if stats else None,
            "measured": info, "url": f"{public}/tiles/imagery-sets/{name}/{{z}}/{{x}}/{{y}}",
            "tilejson": f"{public}/tiles/imagery-sets/{name}.json",
        })
    return out


# ---- the library -------------------------------------------------------------------

def _public_urls(request: Request) -> dict[str, str]:
    from .fetch_api import engine_public_url
    pub = engine_public_url(request)
    host = request.url.hostname or "localhost"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    viewer = os.environ.get("VIEWER_PUBLIC_URL") or \
        f"{request.url.scheme}://{host}:{os.environ.get('VIEWER_PUBLIC_PORT') or '3100'}"
    return {"engine": pub, "terrain": f"{pub}/tiles/terrain.json",
            "arcgis": f"{pub}/arcgis/rest/services/<area>/ImageServer",
            "services": f"{pub}/api/services", "viewer": viewer.rstrip("/")}


def _engine_conversion() -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """(engine conversion payload or last known one, engine status)."""
    try:
        status, payload = handoff.request("GET", "/api/conversion", timeout=max(config.engine_timeout_s(), 10.0))
    except handoff.EngineUnavailable as exc:
        with _LAST_LOCK:
            last, t = _LAST_ENGINE["payload"], _LAST_ENGINE["t"]
        return last, {"running": False, "error": str(exc), "lastKnownAgeS": round(time.time() - t, 1) if t else None,
                      "message": "engine not running - conversion status unavailable (the engine counts its "
                                 "own tiles); " + ("showing the last known status" if last else "no status known yet")}
    if status != 200 or not isinstance(payload, dict):
        detail = payload.get("detail") if isinstance(payload, dict) else None
        return None, {"running": True, "error": f"engine answered HTTP {status}: {detail or 'no detail'}",
                      "message": "engine running, but its conversion status failed"
                                 + (" (an older engine without /api/conversion? rebuild it)" if status == 404 else "")}
    with _LAST_LOCK:
        _LAST_ENGINE.update(payload=payload, t=time.time())
    return payload, {"running": True, "message": "engine running"}


@router.get("/api/library/urls")
def library_urls(request: Request) -> dict[str, str]:
    """Public serving URLs (home page footer)."""
    return _public_urls(request)


@router.get("/api/library")
def library(request: Request) -> JSONResponse:
    public = _public_urls(request)
    conv, engine = _engine_conversion()
    by_name = {a["name"]: a for a in (conv or {}).get("areas", [])}
    rows = []
    for area in list_areas():
        if (area.get("kind") or "elevation") != "elevation" or not area.get("name"):
            continue
        name = area["name"]
        c = by_name.get(name)
        row = {"name": name, "id": area.get("id"), "bbox": area.get("bbox"), "provider": area.get("provider"),
               "minzoom": area.get("minzoom"), "maxzoom": area.get("maxzoom"),
               "dataset": {k: (area.get("dataset") or {}).get(k)
                           for k in ("state", "message", "error", "warning", "pendingBuild", "mode")},
               "updated": area.get("updated"),
               "sources": source_status(area),
               "arcgisUrl": f"{public['engine']}/arcgis/rest/services/{name}/ImageServer",
               "conversion": c, "conversionLive": bool(c) and engine["running"]}
        rows.append(row)
    down = jobs.current_job(tail=0)
    download = None
    if down and down.get("state") in jobs.ACTIVE_STATES:
        download = {k: down.get(k) for k in ("id", "kind", "name", "step", "state", "elapsed", "mode")}
    payload = {
        "engine": engine,
        "engineVersion": (conv or {}).get("engineVersion"),
        "primary": (conv or {}).get("primary"),
        "published": (conv or {}).get("published"),
        "job": (conv or {}).get("job") if engine["running"] else None,
        "busy": bool((conv or {}).get("busy")) and engine["running"],
        "measuring": bool((conv or {}).get("measuring")),
        "autobuild": (conv or {}).get("autobuild"),
        "download": download,
        "public": public,
        "areas": rows,
        "imagery": imagery_status(public["engine"]),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    from .main import _finite
    response = JSONResponse(_finite(payload))
    response.headers["Cache-Control"] = "no-store"
    return response


class ConvertBody(BaseModel):
    publish: bool = False


def _area(name: str) -> dict[str, Any]:
    area = next((a for a in list_areas() if a.get("name") == name and (a.get("kind") or "elevation") == "elevation"),
                None)
    if not area:
        raise HTTPException(status_code=404, detail=f"no saved elevation area '{name}'")
    return area


@router.post("/api/library/{name}/convert", status_code=202)
def convert_now(name: str, body: ConvertBody | None = None) -> dict[str, Any]:
    """Ask the engine to convert only what is missing for this area."""
    _area(name)
    try:
        status, payload = handoff.request("POST", f"/api/conversion/{handoff.quote(name)}/convert",
                                          {"publish": bool(body and body.publish)}, timeout=30)
    except handoff.EngineUnavailable as exc:
        raise HTTPException(status_code=503, detail=ENGINE_DOWN_CONVERT,
                            headers={"X-Engine-Error": str(exc)[:200].encode("ascii", "replace").decode()})
    if status >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        raise HTTPException(status_code=status, detail=detail or f"engine answered HTTP {status}")
    return payload if isinstance(payload, dict) else {"state": "started"}


@router.post("/api/library/{name}/download", status_code=202)
def download_missing(name: str) -> dict[str, Any]:
    """Download the source DEMs this area still misses (cached files are
    skipped), then hand it to the engine - the normal download job."""
    from .fetch_api import JobInput, _start
    area = _area(name)
    src = source_status(area)
    if not src["downloadable"]:
        raise HTTPException(status_code=409, detail="every source DEM of this area is on disk"
                            + (f" ({src['unavailable']} cell(s) do not exist at the source - sea/void)"
                               if src.get("unavailable") else ""))
    job = _start(JobInput(name=name, bbox=tuple(area["bbox"]), provider=area.get("provider") or "copernicus",
                          minzoom=int(area.get("minzoom", 0)), maxzoom=int(area.get("maxzoom", 13)), saveArea=True))
    return {"state": "started", "job": {k: job.get(k) for k in ("id", "name", "step", "state")},
            "message": f"downloading {src['downloadable']} missing DEM file(s) of {name}; "
                       f"the engine builds it afterwards"}


@router.post("/api/library/refresh", status_code=202)
def refresh() -> dict[str, Any]:
    """Re-count tiles (engine) and imagery sets (here) in the background."""
    _imagery_measurer().invalidate()
    try:
        status, payload = handoff.request("POST", "/api/conversion/refresh", timeout=10)
    except handoff.EngineUnavailable:
        return {"state": "measuring", "engine": False, "message": "imagery re-measured; engine not running"}
    return {"state": "measuring", "engine": status < 400}
