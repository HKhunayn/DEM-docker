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
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import autobuild, catalog, jobs

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
