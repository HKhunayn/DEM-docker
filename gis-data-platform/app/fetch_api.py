"""Data-platform APIs: fetch page, saved areas, providers, DOWNLOAD jobs and
the engine hand-off/proxy routes the fetch page uses to follow a build.

Shared read-only routers (saved areas, job status) are also mounted by the
engine app (its /api/jobs are BUILD jobs; see build_api.py).

The fetch page uses RELATIVE urls (``api/...``, ``vendor/...``) so it works on
the platform's own port (/fetch/) and behind the viewer's /fetch/ proxy: the
platform mounts its API routers both at / and under /fetch (see main.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from . import config, handoff, jobs
from .catalog import NameConflictError, delete_area, find_area, list_areas, upsert_area
from .config import LEAFLET_ROOT
from .jobs import probe_providers

page_router = APIRouter()     # data-platform: fetch page + Leaflet assets
router = APIRouter()          # data-platform API (also mounted under /fetch)
areas_router = APIRouter()    # both apps (read-only)
jobs_router = APIRouter()     # both apps: status/history/cancel of the app's own jobs

FETCH_PAGE = Path(__file__).resolve().parent / "static" / "fetch" / "index.html"

PROVIDER_INFO = {
    "copernicus": {
        "label": "Copernicus DEM GLO-30 — 30 m, surface model (DSM)",
        "license": "ESA free licence — attribution, commercial use OK",
        "source": "AWS Open Data (s3://copernicus-dem-30m)",
    },
    "fabdem": {
        "label": "FABDEM V1-2 — 30 m, bare-earth (buildings/canopy removed)",
        "license": "CC BY-NC-SA 4.0 — NON-COMMERCIAL only",
        "source": "University of Bristol (data.bris.ac.uk)",
    },
}


class AreaSpec(BaseModel):
    bbox: tuple[float, float, float, float]
    provider: Literal["copernicus", "fabdem"] = "copernicus"
    minzoom: int = Field(default=0, ge=0, le=16)
    maxzoom: int = Field(default=13, ge=0, le=16)

    @field_validator("bbox")
    @classmethod
    def _check_bbox(cls, value: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        minlon, minlat, maxlon, maxlat = value
        if not (-180.0 <= minlon < maxlon <= 180.0 and -90.0 <= minlat < maxlat <= 90.0):
            raise ValueError(
                "bbox must satisfy -180<=minlon<maxlon<=180 and -90<=minlat<maxlat<=90"
            )
        return value

    @model_validator(mode="after")
    def _check_zooms(self) -> "AreaSpec":
        if self.minzoom > self.maxzoom:
            raise ValueError("minzoom must be <= maxzoom")
        return self


class AreaInput(AreaSpec):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class JobInput(AreaSpec):
    name: str = Field(default="local30m", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    saveArea: bool = True


# ---- fetch page + local vendor assets --------------------------------------

@page_router.get("/fetch", include_in_schema=False)
def fetch_redirect() -> RedirectResponse:
    # relative urls in the page need the trailing slash
    return RedirectResponse("/fetch/", status_code=307)


@page_router.get("/fetch/", include_in_schema=False)
@page_router.get("/fetch/index.html", include_in_schema=False)
def fetch_page() -> FileResponse:
    response = FileResponse(FETCH_PAGE, media_type="text/html; charset=utf-8")
    response.headers["Cache-Control"] = "no-cache"
    return response


@page_router.get("/vendor/leaflet/{asset_path:path}", include_in_schema=False)
@page_router.get("/fetch/vendor/leaflet/{asset_path:path}", include_in_schema=False)
def leaflet_asset(asset_path: str) -> FileResponse:
    root = LEAFLET_ROOT.resolve()
    parts = [p for p in asset_path.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise HTTPException(status_code=404, detail="asset not found")
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=404, detail="asset not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(candidate)


# ---- saved areas -----------------------------------------------------------

@areas_router.get("/api/areas")
def areas() -> dict[str, Any]:
    items = list_areas()
    return {"areas": items, "count": len(items)}


@areas_router.get("/api/areas/{area_id}")
def get_area(area_id: str) -> dict[str, Any]:
    area = find_area(area_id)
    if not area:
        raise HTTPException(status_code=404, detail="area not found")
    return area


@router.post("/api/areas", status_code=201)
def add_area(body: AreaInput) -> dict[str, Any]:
    try:
        return upsert_area(body.name, list(body.bbox), body.provider, body.minzoom, body.maxzoom)
    except NameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.delete("/api/areas/{area_id}")
def remove_area(area_id: str) -> dict[str, Any]:
    if not delete_area(area_id):
        raise HTTPException(status_code=404, detail="area not found")
    return {"deleted": True}


# ---- providers -------------------------------------------------------------

@router.get("/api/fetch/providers")
def providers() -> dict[str, Any]:
    availability = probe_providers()
    out = []
    for provider_id, info in PROVIDER_INFO.items():
        available, reason = availability.get(provider_id, (False, "unknown provider"))
        out.append({"id": provider_id, **info, "available": available, "reason": reason})
    return {"providers": out}


# ---- job status (both apps) ------------------------------------------------

MAX_TAIL = 5000


def _tail(tail: int) -> int:
    """`tail` query: last N log lines (default 200, clamped to 0..5000)."""
    return max(0, min(int(tail), MAX_TAIL))


@jobs_router.get("/api/jobs")
def job_history() -> dict[str, Any]:
    items = jobs.list_jobs()  # no `log` (use /api/jobs/{id}?tail=N); `log_total` included
    return {"jobs": items, "count": len(items)}


@jobs_router.get("/api/jobs/current")
def job_current(tail: int = jobs.DEFAULT_TAIL) -> dict[str, Any]:
    job = jobs.current_job(tail=_tail(tail))
    if not job:
        raise HTTPException(status_code=404, detail="no job has been started")
    return job


@jobs_router.get("/api/jobs/{job_id}")
def job_status(job_id: str, tail: int = jobs.DEFAULT_TAIL) -> dict[str, Any]:
    job = jobs.get_job(job_id, tail=_tail(tail))
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@jobs_router.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str) -> dict[str, Any]:
    try:
        return jobs.cancel_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found")
    except jobs.JobNotRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ---- download jobs (data-platform) -----------------------------------------

def _start(body: JobInput) -> dict[str, Any]:
    available, reason = probe_providers().get(body.provider, (False, "unknown provider"))
    if not available:
        raise HTTPException(
            status_code=400,
            detail=f"provider '{body.provider}' is not available on this engine: {reason}",
        )
    try:
        return jobs.start_download_job(
            name=body.name,
            bbox=list(body.bbox),
            provider=body.provider,
            minzoom=body.minzoom,
            maxzoom=body.maxzoom,
            save_area=body.saveArea,
        )
    except jobs.JobRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except NameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/api/jobs", status_code=202)
def start_job(body: JobInput) -> dict[str, Any]:
    """Download the area's source DEMs, then hand it to the engine to build."""
    return _start(body)


# ---- legacy fetch-page endpoints -------------------------------------------

@router.get("/api/status")
def legacy_status() -> dict[str, Any]:
    return jobs.legacy_status()


@router.post("/api/fetch")
def legacy_fetch(body: JobInput) -> dict[str, Any]:
    job = _start(body)
    return {"ok": True, "state": job["state"], "jobId": job["id"]}


# ---- the engine, seen from the platform (fetch page progress) ---------------

def engine_public_url(request: Request) -> str:
    """Engine URL for links in the browser (other port on the same host)."""
    if config.ENGINE_PUBLIC_URL:
        return config.ENGINE_PUBLIC_URL
    host = request.url.hostname or "localhost"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{request.url.scheme}://{host}:{config.ENGINE_PUBLIC_PORT}"


def _engine_call(method: str, path: str, timeout: float | None = None) -> Any:
    try:
        status, payload = handoff.request(method, path, timeout=timeout)
    except handoff.EngineUnavailable as exc:
        raise HTTPException(status_code=503, detail=handoff.ENGINE_OFFLINE_MSG,
                            headers={"X-Engine-Error": str(exc)[:200].encode("ascii", "replace").decode(),
                                     "Cache-Control": "no-store"})
    if status >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        raise HTTPException(status_code=status, detail=detail or f"engine answered HTTP {status}")
    return payload


@router.get("/api/engine/status")
def engine_status(request: Request) -> dict[str, Any]:
    """Is the engine running, what is it building, where do apps reach it."""
    out: dict[str, Any] = {"publicUrl": engine_public_url(request), "url": handoff.engine_url()}
    try:
        status, current = handoff.request("GET", "/api/jobs/current?tail=0")
    except handoff.EngineUnavailable as exc:
        return {**out, "running": False, "message": handoff.ENGINE_OFFLINE_MSG, "error": str(exc)}
    out.update(running=True, message="engine running",
               current=current if status == 200 and isinstance(current, dict) else None)
    return out


@router.get("/api/engine/build/{name}")
def engine_build(name: str, tail: int = 30) -> dict[str, Any]:
    """The engine's build state for a saved area (latest build job + dataset).
    503 while the engine is not running."""
    return _engine_call("GET", f"/api/build/{handoff.quote(name)}?tail={_tail(tail)}")


@router.post("/api/engine/build/{name}", status_code=202)
def engine_build_now(name: str) -> dict[str, Any]:
    """Hand a downloaded area to the engine again (e.g. after it refused)."""
    area = next((a for a in list_areas() if a.get("name") == name and (a.get("kind") or "elevation") == "elevation"),
                None)
    if not area:
        raise HTTPException(status_code=404, detail="area not found")
    return handoff.request_build(name, area.get("provider") or "copernicus")


@router.post("/api/engine/jobs/{job_id}/cancel")
def engine_cancel(job_id: str) -> dict[str, Any]:
    return _engine_call("POST", f"/api/jobs/{handoff.quote(job_id)}/cancel", timeout=30)
