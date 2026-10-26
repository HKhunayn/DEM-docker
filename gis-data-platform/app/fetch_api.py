"""Fetch-page APIs: saved areas, provider availability, background jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from . import jobs
from .catalog import delete_area, find_area, list_areas, upsert_area
from .config import LEAFLET_ROOT
from .jobs import probe_providers

router = APIRouter()

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

@router.get("/fetch", include_in_schema=False)
@router.get("/fetch/", include_in_schema=False)
@router.get("/fetch/index.html", include_in_schema=False)
def fetch_page() -> FileResponse:
    return FileResponse(FETCH_PAGE, media_type="text/html; charset=utf-8")


@router.get("/vendor/leaflet/{asset_path:path}", include_in_schema=False)
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

@router.get("/api/areas")
def areas() -> dict[str, Any]:
    items = list_areas()
    return {"areas": items, "count": len(items)}


@router.post("/api/areas", status_code=201)
def add_area(body: AreaInput) -> dict[str, Any]:
    return upsert_area(body.name, list(body.bbox), body.provider, body.minzoom, body.maxzoom)


@router.get("/api/areas/{area_id}")
def get_area(area_id: str) -> dict[str, Any]:
    area = find_area(area_id)
    if not area:
        raise HTTPException(status_code=404, detail="area not found")
    return area


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


# ---- background jobs -------------------------------------------------------

@router.post("/api/jobs", status_code=202)
def start_job(body: JobInput) -> dict[str, Any]:
    available, reason = probe_providers().get(body.provider, (False, "unknown provider"))
    if not available:
        raise HTTPException(
            status_code=400,
            detail=f"provider '{body.provider}' is not available on this engine: {reason}",
        )
    try:
        return jobs.start_job(
            name=body.name,
            bbox=list(body.bbox),
            provider=body.provider,
            minzoom=body.minzoom,
            maxzoom=body.maxzoom,
            save_area=body.saveArea,
        )
    except jobs.JobRunningError:
        raise HTTPException(status_code=409, detail="a fetch job is already running")


@router.get("/api/jobs")
def job_history() -> dict[str, Any]:
    items = jobs.list_jobs()
    return {"jobs": items, "count": len(items)}


@router.get("/api/jobs/current")
def job_current() -> dict[str, Any]:
    job = jobs.current_job()
    if not job:
        raise HTTPException(status_code=404, detail="no job has been started")
    return job


@router.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


# ---- legacy fetch-page endpoints -------------------------------------------

@router.get("/api/status")
def legacy_status() -> dict[str, Any]:
    return jobs.legacy_status()


@router.post("/api/fetch")
def legacy_fetch(body: JobInput) -> dict[str, Any]:
    available, reason = probe_providers().get(body.provider, (False, "unknown provider"))
    if not available:
        raise HTTPException(
            status_code=400,
            detail=f"provider '{body.provider}' is not available on this engine: {reason}",
        )
    try:
        job = jobs.start_job(
            name=body.name,
            bbox=list(body.bbox),
            provider=body.provider,
            minzoom=body.minzoom,
            maxzoom=body.maxzoom,
            save_area=body.saveArea,
        )
    except jobs.JobRunningError:
        raise HTTPException(status_code=409, detail="a fetch job is already running")
    return {"ok": True, "state": job["state"], "jobId": job["id"]}
