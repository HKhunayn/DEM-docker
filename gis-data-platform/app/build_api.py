"""Engine build-job API: build a saved area whose DEMs are on the volume.

The engine never downloads. ``POST /api/jobs {"name": ...}`` starts
check -> build -> verify -> publish (``mode: "lerc"``: check -> build, the
area's ArcGIS service only) for the saved area of that name; ``check`` fails
with a clear message when the DEMs are missing. While another build runs the
request is queued (the area is marked ``downloaded`` / pending and the
automatic build picks it up as soon as the engine is idle).

Status/history/cancel: /api/jobs, /api/jobs/current, /api/jobs/{id},
/api/jobs/{id}/cancel (shared router in fetch_api.py); per area:
GET /api/build/{name}.

Conversion status (data-platform library page, through its proxy):
GET /api/conversion, GET /api/conversion/{name}, POST /api/conversion/refresh,
POST /api/conversion/{name}/convert {"publish": false} - converts only what is
missing (app/conversion.py). All of these are INTERNAL: the engine's public
nginx (port 5000) answers 403 for them; the data-platform calls engine:8787.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import autobuild, catalog, conversion, jobs

router = APIRouter()


class BuildInput(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    provider: Literal["copernicus", "fabdem"] | None = None
    mode: Literal["full", "lerc"] = "full"
    origin: Literal["user", "handoff"] = "user"


def _area(name: str, provider: str | None = None) -> dict[str, Any]:
    area = catalog.find_area_by_name(name, "elevation", provider)
    if not area:
        raise HTTPException(status_code=404, detail=(
            f"no saved elevation area '{name}' - download it with the data-platform "
            f"(fetch page) first; the engine does not download"))
    if not (isinstance(area.get("bbox"), list) and len(area["bbox"]) == 4):
        raise HTTPException(status_code=400, detail=f"saved area '{name}' has no valid bbox")
    return area


@router.post("/api/jobs", status_code=202)
def start_build(body: BuildInput) -> dict[str, Any]:
    """Build the saved area `name` now, or queue it behind the running build."""
    area = _area(body.name, body.provider)
    provider = area.get("provider") or "copernicus"
    autobuild.forget_failure(body.name)       # an explicit request retries a failed area
    try:
        snap = jobs.start_job(body.name, list(area["bbox"]), provider, int(area.get("minzoom", 0)),
                              int(area.get("maxzoom", 13)), lerc_only=body.mode == "lerc", origin=body.origin)
    except catalog.NameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except jobs.JobRunningError as exc:
        if body.mode == "full":
            fields: dict[str, Any] = {"pendingBuild": "full",
                                      "message": f"queued in the engine - {exc}; it builds this area next"}
            if (area.get("dataset") or {}).get("state") != "building":
                fields["state"] = "downloaded"
            catalog.update_area_dataset(area["id"], **fields)
        autobuild.wake()
        return {"state": "queued", "job": None,
                "message": f"queued in the engine - {exc}; it builds '{body.name}' next"}
    return {"state": "started", "job": snap,
            "message": f"the engine is building '{body.name}' (job {snap['id'][:8]})"}


@router.get("/api/build/{name}")
def build_state(name: str, tail: int = 30) -> dict[str, Any]:
    """Build state of a saved area: its dataset record, its latest build job
    (with the last `tail` log lines) and what the engine is busy with."""
    area = _area(name)
    tail = max(0, min(int(tail), 5000))
    latest = next((j for j in jobs.list_jobs() if j.get("name") == name and j.get("kind", "elevation") == "elevation"),
                  None)
    job = jobs.get_job(latest["id"], tail=tail) if latest else None
    current = jobs.current_job(tail=0)
    busy = None
    if current and current.get("state") in jobs.ACTIVE_STATES and current.get("name") != name:
        busy = {k: current.get(k) for k in ("id", "name", "step", "elapsed", "origin")}
    status = autobuild.status()
    auto = status.get("autobuild", {})
    return {
        "name": name,
        "dataset": area.get("dataset"),
        "job": job,
        "busyWith": busy,
        "service": f"/arcgis/rest/services/{name}/ImageServer",
        "published": status.get("published"),
        "autobuild": {k: auto.get(k) for k in ("state", "message", "current", "intervalS")},
    }


# ---- conversion status (internal; proxied by the data-platform) -------------

class ConvertInput(BaseModel):
    publish: bool = False


def _no_store(payload: dict[str, Any]) -> JSONResponse:
    from .main import _finite
    response = JSONResponse(_finite(payload))
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/api/conversion")
def conversion_overview() -> JSONResponse:
    """Per area: ArcGIS + WebP state, percent, tiles done/expected, size,
    live job progress and what Convert now would do. Never blocks on disk
    counting (cached; ``measured.measuring`` while a count runs)."""
    return _no_store(conversion.overview())


@router.post("/api/conversion/refresh", status_code=202)
def conversion_refresh() -> dict[str, Any]:
    """Re-count every area's tiles in the background."""
    n = conversion.measurer().invalidate()
    conversion.overview()
    return {"state": "measuring", "invalidated": n}


@router.get("/api/conversion/{name}")
def conversion_area(name: str) -> JSONResponse:
    info = conversion.overview(name)
    if not info["areas"]:
        raise HTTPException(status_code=404, detail=f"no saved elevation area '{name}'")
    return _no_store({**info, "area": info["areas"][0]})


@router.post("/api/conversion/{name}/convert", status_code=202)
def conversion_convert(name: str, body: ConvertInput | None = None) -> dict[str, Any]:
    """Convert only what is missing for this area (lerc-only when only the
    ArcGIS tiles are missing/partial/stale, full when its WebP set is; with
    publish=true a non-primary area is built + published, replacing the
    single published set). 409 when busy or nothing is missing."""
    try:
        return conversion.start_conversion(name, publish=bool(body and body.publish))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (conversion.BusyError, jobs.JobRunningError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except conversion.NothingToDo as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except catalog.NameConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
