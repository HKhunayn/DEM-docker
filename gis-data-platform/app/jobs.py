"""Background fetch/build jobs driven by the heightmap-30m engine CLI.

The DEM download/build/verify logic lives in the existing heightmap-30m project
(terrainhub.py + scripts/). This module schedules those CLI steps as one
background job and records step / elapsed / log / result for the fetch page.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .catalog import update_area, upsert_area
from .config import DEM_ROOT, ENGINE_ROOT, TILE_ROOT, engine_python  # noqa: F401  (re-exported)

STEPS = ("fetch", "build", "verify")
HISTORY_LIMIT = 20

SOURCE_TILES_RE = re.compile(r"Done: (\d+) tile")
TILES_RE = re.compile(r"WROTE (\d+) tiles")
LERC_TILES_RE = re.compile(r"WROTE (\d+) LERC tiles")

_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_ORDER: list[str] = []
_CURRENT_ID: str | None = None
_PROVIDER_CACHE: dict[str, tuple[bool, str]] | None = None


class JobRunningError(RuntimeError):
    """Raised when a job is requested while another one is still running."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_command(step: str, bbox, provider: str, minzoom: int, maxzoom: int, name: str) -> list[str]:
    # `--data` and `--root` pin every engine step to the platform's data root, so
    # fetch writes sources to data/dem/ and build/verify read+write data/tiles/.
    # Without them the engine falls back to its own ./data and ./public.
    return [
        str(engine_python()),
        str(ENGINE_ROOT / "terrainhub.py"),
        step,
        "--bbox", *map(str, bbox),
        "--provider", provider,
        "--minzoom", str(minzoom),
        "--maxzoom", str(maxzoom),
        "--name", name,
        "--data", str(DEM_ROOT),
        "--root", str(TILE_ROOT),
    ]


def probe_providers() -> dict[str, tuple[bool, str]]:
    """Availability of each DEM provider on this engine install."""
    global _PROVIDER_CACHE
    if _PROVIDER_CACHE is not None:
        return _PROVIDER_CACHE
    engine_ok = (ENGINE_ROOT / "terrainhub.py").is_file()
    copernicus = (engine_ok, "" if engine_ok else f"engine not found at {ENGINE_ROOT}")
    if engine_ok:
        probe = subprocess.run(
            [str(engine_python()), "-c", "import remotezip, requests"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if probe.returncode == 0:
            fabdem = (True, "")
        else:
            fabdem = (False, "remotezip/requests not installed in engine python")
    else:
        fabdem = (False, "engine not found")
    _PROVIDER_CACHE = {"copernicus": copernicus, "fabdem": fabdem}
    return _PROVIDER_CACHE


def _snapshot(job: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    out["log"] = list(job["log"])
    out["steps"] = [dict(step) for step in job["steps"]]
    t0 = job.get("_t0")
    finished = job.get("_finished_t")
    out["elapsed"] = round((finished if finished is not None else time.time()) - t0, 3) if t0 else 0.0
    return out


def start_job(
    name: str,
    bbox: list[float],
    provider: str = "copernicus",
    minzoom: int = 0,
    maxzoom: int = 13,
    save_area: bool = True,
) -> dict[str, Any]:
    global _CURRENT_ID
    with _LOCK:
        if _CURRENT_ID is not None and _JOBS.get(_CURRENT_ID, {}).get("state") == "running":
            raise JobRunningError("a fetch job is already running")
        area = None
        if save_area:
            area = upsert_area(
                name=name,
                bbox=list(bbox),
                provider=provider,
                minzoom=minzoom,
                maxzoom=maxzoom,
                dataset={
                    "name": name,
                    "state": "building",
                    "provider": provider,
                    "bbox": list(bbox),
                    "minzoom": minzoom,
                    "maxzoom": maxzoom,
                },
            )
        job: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "kind": "elevation",
            "name": name,
            "provider": provider,
            "bbox": list(bbox),
            "minzoom": minzoom,
            "maxzoom": maxzoom,
            "areaId": area["id"] if area else None,
            "state": "running",
            "step": STEPS[0],
            "steps": [{"name": step, "state": "pending", "returncode": None} for step in STEPS],
            "started": _now(),
            "finished": None,
            "log": [],
            "result": None,
            "error": None,
            "_t0": time.time(),
            "_finished_t": None,
        }
        _JOBS[job["id"]] = job
        _ORDER.append(job["id"])
        while len(_ORDER) > HISTORY_LIMIT:
            _JOBS.pop(_ORDER.pop(0), None)
        _CURRENT_ID = job["id"]
    threading.Thread(
        target=_run_job,
        args=(job["id"], name, bbox, provider, minzoom, maxzoom),
        daemon=True,
    ).start()
    return _snapshot(job)


def _run_job(
    job_id: str,
    name: str,
    bbox: list[float],
    provider: str,
    minzoom: int,
    maxzoom: int,
) -> None:
    job = _JOBS[job_id]
    outcome = "done"
    error: str | None = None
    try:
        for entry, step in zip(job["steps"], STEPS):
            entry["state"] = "running"
            job["step"] = step
            cmd = build_command(step, bbox, provider, minzoom, maxzoom, name)
            proc = subprocess.Popen(
                cmd,
                cwd=str(ENGINE_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                with _LOCK:
                    job["log"].append(line.rstrip())
            rc = proc.wait()
            entry["returncode"] = rc
            entry["state"] = "done" if rc == 0 else "error"
            if rc != 0:
                outcome = "error"
                error = f"{step} step failed (exit {rc})"
                break
    except Exception as exc:  # noqa: BLE001
        outcome = "error"
        error = f"{job['step']} step failed ({exc})"
        with _LOCK:
            job["log"].append(f"FATAL {exc}")

    result = None
    if outcome == "done":
        log = "\n".join(job["log"])
        source = SOURCE_TILES_RE.search(log)
        tiles = TILES_RE.search(log)
        lerc = LERC_TILES_RE.search(log)
        result = {
            "sourceTiles": int(source.group(1)) if source else None,
            "tilesBuilt": int(tiles.group(1)) if tiles else None,
            "lercTiles": int(lerc.group(1)) if lerc else None,
            "verified": "stats match: PASS" in log,
        }

    with _LOCK:
        job.update(
            state=outcome,
            error=error,
            result=result,
            finished=_now(),
            _finished_t=time.time(),
        )
        global _CURRENT_ID
        if _CURRENT_ID == job_id:
            _CURRENT_ID = None

    if job["areaId"]:
        dataset: dict[str, Any] = {
            "name": name,
            "state": outcome,
            "provider": provider,
            "bbox": list(bbox),
            "minzoom": minzoom,
            "maxzoom": maxzoom,
            "service": f"/arcgis/rest/services/{name}/ImageServer",
        }
        if result:
            dataset.update(
                sourceTiles=result["sourceTiles"],
                tilesBuilt=result["tilesBuilt"],
                lercTiles=result["lercTiles"],
                verified=result["verified"],
            )
        if error:
            dataset["error"] = error
        update_area(job["areaId"], dataset=dataset)


# ---- imagery jobs (in-process download, same registry/status shape) --------

IMAGERY_STEPS = ("download",)


def start_imagery_job(
    name: str,
    bbox: list[float],
    provider: str,
    minzoom: int = 0,
    maxzoom: int = 1,
    save_area: bool = True,
) -> dict[str, Any]:
    from . import imagery

    global _CURRENT_ID
    total = imagery.count_tiles(bbox, minzoom, maxzoom)
    if total > imagery.MAX_TILES_PER_JOB:
        raise ValueError(f"{total} tiles requested; limit is {imagery.MAX_TILES_PER_JOB} per job")
    with _LOCK:
        if _CURRENT_ID is not None and _JOBS.get(_CURRENT_ID, {}).get("state") == "running":
            raise JobRunningError("a fetch job is already running")
        dataset = _imagery_dataset(name, provider, bbox, minzoom, maxzoom, "building")
        area = upsert_area(name=name, bbox=list(bbox), provider=provider, minzoom=minzoom,
                           maxzoom=maxzoom, dataset=dataset, kind="imagery") if save_area else None
        job: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "kind": "imagery",
            "name": name,
            "provider": provider,
            "bbox": list(bbox),
            "minzoom": minzoom,
            "maxzoom": maxzoom,
            "areaId": area["id"] if area else None,
            "state": "running",
            "step": IMAGERY_STEPS[0],
            "steps": [{"name": step, "state": "pending", "returncode": None} for step in IMAGERY_STEPS],
            "started": _now(),
            "finished": None,
            "log": [],
            "result": None,
            "error": None,
            "_t0": time.time(),
            "_finished_t": None,
        }
        _JOBS[job["id"]] = job
        _ORDER.append(job["id"])
        while len(_ORDER) > HISTORY_LIMIT:
            _JOBS.pop(_ORDER.pop(0), None)
        _CURRENT_ID = job["id"]
    threading.Thread(target=_run_imagery_job, args=(job["id"],), daemon=True).start()
    return _snapshot(job)


def _imagery_dataset(name, provider, bbox, minzoom, maxzoom, state) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "imagery",
        "state": state,
        "provider": provider,
        "bbox": list(bbox),
        "minzoom": minzoom,
        "maxzoom": maxzoom,
        "tilejson": f"/tiles/imagery-sets/{name}.json",
    }


def _run_imagery_job(job_id: str) -> None:
    from . import imagery

    job = _JOBS[job_id]
    entry = job["steps"][0]
    entry["state"] = "running"

    def log(line: str) -> None:
        with _LOCK:
            job["log"].append(line)

    outcome, error, result = "done", None, None
    try:
        result = imagery.download_set(job["name"], job["bbox"], job["provider"],
                                      job["minzoom"], job["maxzoom"], log)
        entry.update(state="done", returncode=0)
    except Exception as exc:  # noqa: BLE001
        outcome, error = "error", f"download step failed ({exc})"
        entry.update(state="error", returncode=1)
        log(f"FATAL {exc}")
    with _LOCK:
        job.update(state=outcome, error=error, result=result, finished=_now(), _finished_t=time.time())
        global _CURRENT_ID
        if _CURRENT_ID == job_id:
            _CURRENT_ID = None
    if job["areaId"]:
        dataset = _imagery_dataset(job["name"], job["provider"], job["bbox"], job["minzoom"], job["maxzoom"], outcome)
        if result:
            dataset.update(tiles=result["tiles"], bytes=result["bytes"])
        if error:
            dataset["error"] = error
        update_area(job["areaId"], dataset=dataset)


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return _snapshot(job) if job else None


def list_jobs() -> list[dict[str, Any]]:
    with _LOCK:
        return [_snapshot(_JOBS[job_id]) for job_id in reversed(_ORDER)]


def current_job() -> dict[str, Any] | None:
    with _LOCK:
        if not _ORDER:
            return None
        return _snapshot(_JOBS[_ORDER[-1]])


def legacy_status() -> dict[str, Any]:
    """Old fetch-page /api/status shape: idle | running | done | error."""
    job = current_job()
    if not job:
        return {
            "state": "idle",
            "provider": None,
            "bbox": None,
            "log": [],
            "started": None,
            "finished": None,
            "step": None,
            "elapsed": 0.0,
            "result": None,
            "jobId": None,
            "name": None,
        }
    return {
        "state": job["state"],
        "provider": job["provider"],
        "bbox": job["bbox"],
        "log": job["log"][-60:],
        "started": job["started"],
        "finished": job["finished"],
        "step": job["step"],
        "elapsed": job["elapsed"],
        "result": job["result"],
        "jobId": job["id"],
        "name": job["name"],
        "kind": job.get("kind", "elevation"),
    }
