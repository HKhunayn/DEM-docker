"""Imagery APIs.

data-platform (``router``): providers, download jobs, delete a set.
engine (``serve_router``): list the local sets, TileJSON and the XYZ tile
fallback (nginx serves the stored tiles; FastAPI over-zooms / crops).
"""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from . import catalog, imagery, jobs

router = APIRouter()        # data-platform
serve_router = APIRouter()  # engine


class ImageryJobInput(BaseModel):
    name: str = Field(default="world_imagery", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    bbox: tuple[float, float, float, float] = (-180.0, -85.05112878, 180.0, 85.05112878)
    provider: str = "s2-earthsearch"
    minzoom: int = Field(default=0, ge=0, le=18)
    maxzoom: int = Field(default=1, ge=0, le=18)
    saveArea: bool = True
    # Raw-scene providers (s2-*): acquisition window + scene cloud limit.
    dateFrom: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    dateTo: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    maxCloud: float | None = Field(default=None, ge=0, le=100)

    @field_validator("provider")
    @classmethod
    def _check_provider(cls, value):
        if value not in imagery.PROVIDERS:
            raise ValueError(f"unknown provider; one of: {', '.join(imagery.PROVIDERS)}")
        return value

    @field_validator("bbox")
    @classmethod
    def _check_bbox(cls, value):
        minlon, minlat, maxlon, maxlat = value
        if not (-180.0 <= minlon < maxlon <= 180.0 and -90.0 <= minlat < maxlat <= 90.0):
            raise ValueError("bbox must satisfy -180<=minlon<maxlon<=180 and -90<=minlat<maxlat<=90")
        return value

    @model_validator(mode="after")
    def _check(self) -> "ImageryJobInput":
        if self.minzoom > self.maxzoom:
            raise ValueError("minzoom must be <= maxzoom")
        limit = imagery.PROVIDERS[self.provider]["maxzoom"]
        if self.maxzoom > limit:
            raise ValueError(f"{self.provider} provides tiles up to z{limit}")
        dates = []
        for value in (self.dateFrom, self.dateTo):
            if value is not None:
                try:
                    dates.append(date.fromisoformat(value))
                except ValueError:
                    raise ValueError(f"invalid date {value!r}") from None
        if len(dates) == 2 and dates[0] > dates[1]:
            raise ValueError("dateFrom must be <= dateTo")
        return self

    def options(self) -> dict[str, Any] | None:
        if imagery.PROVIDERS[self.provider].get("kind") != "scenes":
            return None
        return {"dateFrom": self.dateFrom, "dateTo": self.dateTo, "maxCloud": self.maxCloud}


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _version(meta: dict[str, Any]) -> str:
    return "".join(c for c in str(meta.get("updated", "")) if c.isdigit())[:14] or "0"


def _tilejson(meta: dict[str, Any], base: str) -> dict[str, Any]:
    name = meta["name"]
    return {
        "tilejson": "3.0.0",
        "name": name,
        "kind": "imagery",
        "provider": meta.get("provider"),
        "attribution": meta.get("attribution", ""),
        "license": meta.get("license", ""),
        "bounds": meta["bbox"],
        "minzoom": meta["minzoom"],
        "maxzoom": meta["maxzoom"],
        "tileSize": 256,
        "scheme": "xyz",
        "tiles": [f"{base}/tiles/imagery-sets/{name}/{{z}}/{{x}}/{{y}}?v={_version(meta)}"],
        "tileCount": meta.get("tiles"),
        "bytes": meta.get("bytes"),
        "updated": meta.get("updated"),
    }


@router.get("/api/imagery/providers")
def imagery_providers() -> dict[str, Any]:
    out = []
    for pid, info in imagery.PROVIDERS.items():
        available, reason = imagery.provider_status(pid)
        public = {k: v for k, v in info.items() if k in _PUBLIC_KEYS}
        public.setdefault("kind", "tiles")
        out.append({"id": pid, **public, "available": available, "reason": reason})
    return {"providers": out}


_PUBLIC_KEYS = ("label", "license", "attribution", "source", "maxzoom", "format", "kind")


@router.post("/api/imagery/jobs", status_code=202)
def start_imagery_job(body: ImageryJobInput) -> dict[str, Any]:
    try:
        return jobs.start_imagery_job(
            name=body.name, bbox=list(body.bbox), provider=body.provider,
            minzoom=body.minzoom, maxzoom=body.maxzoom, save_area=body.saveArea,
            options=body.options(),
        )
    except (jobs.JobRunningError, catalog.NameConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@serve_router.get("/api/imagery")
def imagery_sets(request: Request) -> dict[str, Any]:
    sets = [_tilejson(meta, _base(request)) for meta in imagery.list_sets()]
    return {"sets": sets, "count": len(sets)}


@router.delete("/api/imagery/{name}")
def delete_imagery_set(name: str) -> dict[str, Any]:
    if jobs.imagery_set_busy(name):
        raise HTTPException(status_code=409, detail=f"an imagery job is still writing '{name}'; "
                                                    "cancel it or wait for it to finish")
    removed = imagery.delete_set(name)
    areas = catalog.delete_areas(name, kind="imagery")
    if not removed and not areas:
        raise HTTPException(status_code=404, detail="imagery set not found")
    return {"deleted": True}


@serve_router.get("/tiles/imagery-sets/{name}.json")
def imagery_tilejson(name: str, request: Request) -> JSONResponse:
    meta = imagery.get_set(name)
    if not meta:
        raise HTTPException(status_code=404, detail="imagery set not found")
    response = JSONResponse(_tilejson(meta, _base(request)))
    response.headers["Cache-Control"] = "no-store"
    return response


@serve_router.get("/tiles/imagery-sets/{name}/{z}/{x}/{y}")
def imagery_tile(name: str, z: int, x: int, y: int) -> Response:
    out = imagery.tile(name, z, x, y)
    if out is None:
        raise HTTPException(status_code=404, detail="imagery set not found")
    payload, mime = out
    response = Response(content=payload, media_type=mime)
    response.headers["Cache-Control"] = "no-cache"
    return response
