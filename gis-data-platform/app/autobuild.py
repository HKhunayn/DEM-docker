"""Keep every saved elevation area tiled.

Tiles are derived data: a TRUTH backup (docker-volume-tool export) carries the
DEMs, catalog and job history but no tiles/, and an old FULL backup may carry
tiles written by an older engine. After such an import the viewers would show
nothing. On startup (and on POST /api/tiles/rebuild-missing) this module
compares the saved areas with what is on disk and queues the builds that are
missing, one job at a time, through the normal job runner:

- the *primary* area (the one the single published Terrarium set belongs to)
  gets a full fetch -> build -> verify -> publish job when nothing valid is
  published;
- every other area, and the primary when only its LERC is stale, gets a
  LERC-only job (its ArcGIS ImageServer), which never replaces the published
  set.

The DEM sources are normally already in the volume, so ``fetch`` only checks
them. Builds stop (state ``blocked``) when the data disk is low.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import catalog, jobs
from .config import ENGINE_ROOT, TILE_ROOT

log = logging.getLogger(__name__)

POLL_S = 5.0
_ROUND = 4      # bbox comparison precision (degrees)
_VERSION_NAME = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{8}$")

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"state": "idle", "message": "", "queue": [], "current": None,
                          "done": [], "failed": [], "started": None, "finished": None}
_THREAD: threading.Thread | None = None


def enabled() -> bool:
    return os.environ.get("GIS_AUTOBUILD", "1") not in ("0", "false", "no", "off")


def min_free_gb() -> float:
    try:
        return float(os.environ.get("GIS_AUTOBUILD_MIN_FREE_GB", "10"))
    except ValueError:
        return 10.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int_constant(path: Path, name: str) -> int | None:
    try:
        m = re.search(rf"^{name}\s*=\s*(\d+)", path.read_text(encoding="utf-8"), re.M)
    except OSError:
        return None
    return int(m.group(1)) if m else None


def engine_version() -> int | None:
    return _int_constant(ENGINE_ROOT / "scripts" / "build_tiles.py", "ENGINE_VERSION")


def publish_revision() -> int | None:
    return _int_constant(ENGINE_ROOT / "scripts" / "publish_tiles.py", "PUBLISH_REVISION")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _same_bbox(a: Any, b: Any) -> bool:
    try:
        return [round(float(v), _ROUND) for v in a] == [round(float(v), _ROUND) for v in b]
    except (TypeError, ValueError):
        return False


# ---- what is on disk ---------------------------------------------------------

def published_status() -> dict[str, Any]:
    """The published Terrarium set: ok + reason."""
    current = _read_json(TILE_ROOT / "published" / "current.json")
    if not isinstance(current, dict) or not _VERSION_NAME.match(str(current.get("version", ""))):
        return {"ok": False, "reason": "nothing published (no tiles in the volume)", "current": None}
    root = TILE_ROOT / "published" / current["version"]
    if not (root / "meta.json").is_file() or not (root / "terrain").is_dir():
        return {"ok": False, "reason": f"published version {current['version']} is missing on disk",
                "current": None}
    want = publish_revision()
    have = current.get("revision")
    if want is not None and have != want:
        return {"ok": False, "reason": f"published with an older layout (revision {have}, now {want})",
                "current": current}
    return {"ok": True, "reason": "", "current": current}


def lerc_status(area: dict[str, Any]) -> dict[str, Any]:
    """The area's ArcGIS LERC ImageServer: ok + reason."""
    svc = TILE_ROOT / "arcgis" / "rest" / "services" / str(area.get("name"))
    if not (svc / "ImageServer.json").is_file():
        return {"ok": False, "reason": "no ArcGIS service"}
    manifest = _read_json(svc / "build_manifest.json")
    if not isinstance(manifest, dict):
        return {"ok": False, "reason": "built by an older engine (no build manifest)"}
    want = engine_version()
    if want is not None and manifest.get("engine_version") != want:
        return {"ok": False, "reason": f"built by engine v{manifest.get('engine_version')} (now v{want})"}
    if not manifest.get("complete"):
        return {"ok": False, "reason": "the last build did not finish"}
    if not _same_bbox(manifest.get("bbox"), area.get("bbox")) \
            or manifest.get("minzoom") != area.get("minzoom") or manifest.get("maxzoom") != area.get("maxzoom"):
        return {"ok": False, "reason": "built for a different extent or zoom range"}
    if not (svc / "tile" / str(area.get("maxzoom"))).is_dir():
        return {"ok": False, "reason": f"zoom {area.get('maxzoom')} tiles are missing"}
    return {"ok": True, "reason": ""}


def elevation_areas() -> list[dict[str, Any]]:
    try:
        areas = catalog.load_catalog().get("areas", [])
    except Exception:  # noqa: BLE001
        log.exception("could not read the saved areas")
        return []
    out = []
    for a in areas:
        if a.get("kind", "elevation") != "elevation" or not a.get("name"):
            continue
        bbox = a.get("bbox")
        if not (isinstance(bbox, list) and len(bbox) == 4):
            continue
        out.append(a)
    return out


def primary_area(areas: list[dict[str, Any]], published: dict[str, Any]) -> dict[str, Any] | None:
    """The area the single published Terrarium set belongs to (or should)."""
    current = published.get("current")
    if current:
        match = [a for a in areas if _same_bbox(a.get("bbox"), current.get("bounds"))]
        if match:
            return match[0]
        if published["ok"]:
            return None     # published set of an area that is no longer saved: leave it
    by_name = {a["name"]: a for a in areas}
    # the area of the newest full job that finished (what was published last)
    history = sorted((j for j in jobs.list_jobs()
                      if j.get("kind", "elevation") == "elevation" and j.get("state") == "done"
                      and not j.get("lercOnly") and j.get("name") in by_name),
                     key=lambda j: str(j.get("finished") or ""))
    if history:
        return by_name[history[-1]["name"]]
    if not areas:
        return None

    def size(a):
        b = a["bbox"]
        return (b[2] - b[0]) * (b[3] - b[1])
    return max(areas, key=size)


def plan() -> dict[str, Any]:
    """What is missing and the jobs that would fix it (primary first)."""
    areas = elevation_areas()
    published = published_status()
    primary = primary_area(areas, published)
    rows, tasks = [], []
    for a in areas:
        lerc = lerc_status(a)
        is_primary = primary is not None and a.get("id") == primary.get("id")
        row = {"name": a["name"], "bbox": a["bbox"], "minzoom": a.get("minzoom"), "maxzoom": a.get("maxzoom"),
               "primary": is_primary, "lerc": lerc}
        rows.append(row)
        task = None
        if is_primary and not published["ok"]:
            task = {"mode": "full", "reason": published["reason"]}
        elif not lerc["ok"]:
            task = {"mode": "lerc", "reason": lerc["reason"]}
        if task:
            task.update(name=a["name"], bbox=list(a["bbox"]), provider=a.get("provider") or "copernicus",
                        minzoom=int(a.get("minzoom", 0)), maxzoom=int(a.get("maxzoom", 13)))
            (tasks.insert(0, task) if is_primary else tasks.append(task))
            row["needs"] = task["mode"]
    return {"published": {k: v for k, v in published.items() if k != "current"} | {
                "version": (published.get("current") or {}).get("version"),
                "bounds": (published.get("current") or {}).get("bounds")},
            "primary": primary["name"] if primary else None,
            "areas": rows, "tasks": tasks}


# ---- runner -----------------------------------------------------------------------

def status() -> dict[str, Any]:
    p = plan()
    with _LOCK:
        auto = {k: (list(v) if isinstance(v, list) else v) for k, v in _STATE.items()}
    current = auto.get("current")
    if current and current.get("jobId"):
        job = jobs.get_job(current["jobId"], tail=0)
        if job:
            current = {**current, "step": job.get("step"), "elapsed": job.get("elapsed"),
                       "jobState": job.get("state")}
            auto["current"] = current
    auto["enabled"] = enabled()
    return {"ready": p["published"]["ok"] and not p["tasks"], **p, "autobuild": auto}


def _set(**kw: Any) -> None:
    with _LOCK:
        _STATE.update(kw)


def _free_gb() -> float:
    try:
        return shutil.disk_usage(TILE_ROOT).free / 1e9
    except OSError:
        return float("inf")


def _wait_idle(stop: threading.Event) -> None:
    while not stop.is_set():
        latest = jobs.current_job(tail=0)   # newest job, finished or not
        busy = latest is not None and latest.get("state") in jobs.ACTIVE_STATES
        if not busy and jobs.orphan_engine_pid() is None:
            return
        stop.wait(POLL_S)


def _run(stop: threading.Event) -> None:
    tasks = plan()["tasks"]
    _set(state="running" if tasks else "done", queue=[f"{t['name']} ({t['mode']})" for t in tasks],
         current=None, done=[], failed=[], started=_now(), finished=None,
         message=f"{len(tasks)} build(s) queued" if tasks else "all saved areas are tiled")
    if not tasks:
        _set(finished=_now())
        return
    log.warning("autobuild: %d build(s) needed: %s", len(tasks),
                ", ".join(f"{t['name']} {t['mode']} ({t['reason']})" for t in tasks))
    for i, task in enumerate(tasks, 1):
        label = f"{task['name']} ({task['mode']})"
        _wait_idle(stop)
        if stop.is_set():
            return
        # still needed? (a user job may have built it meanwhile)
        if not any(t["name"] == task["name"] and t["mode"] == task["mode"] for t in plan()["tasks"]):
            with _LOCK:
                _STATE["queue"] = [q for q in _STATE["queue"] if q != label]
            continue
        free = _free_gb()
        if free < min_free_gb():
            msg = (f"stopped: only {free:.1f} GB free on the data disk (needs {min_free_gb():.0f} GB); "
                   f"free space, then POST /api/tiles/rebuild-missing")
            log.error("autobuild %s", msg)
            _set(state="blocked", message=msg, current=None, finished=_now())
            return
        try:
            snap = jobs.start_job(task["name"], task["bbox"], task["provider"], task["minzoom"],
                                  task["maxzoom"], lerc_only=task["mode"] == "lerc", origin="autobuild")
        except jobs.JobRunningError:
            stop.wait(POLL_S)
            _wait_idle(stop)
            try:
                snap = jobs.start_job(task["name"], task["bbox"], task["provider"], task["minzoom"],
                                      task["maxzoom"], lerc_only=task["mode"] == "lerc", origin="autobuild")
            except Exception as exc:  # noqa: BLE001
                with _LOCK:
                    _STATE["failed"].append(f"{label}: {exc}")
                continue
        except Exception as exc:  # noqa: BLE001
            log.exception("autobuild could not start %s", label)
            with _LOCK:
                _STATE["failed"].append(f"{label}: {exc}")
            continue
        _set(current={"name": task["name"], "mode": task["mode"], "reason": task["reason"],
                      "jobId": snap["id"], "index": i, "total": len(tasks)},
             message=f"building {label} ({i}/{len(tasks)}): {task['reason']}")
        while not stop.is_set():
            job = jobs.get_job(snap["id"], tail=0)
            if job is None or job.get("state") not in jobs.ACTIVE_STATES:
                break
            stop.wait(POLL_S)
        job = jobs.get_job(snap["id"], tail=0) or {}
        with _LOCK:
            _STATE["queue"] = [q for q in _STATE["queue"] if q != label]
            if job.get("state") == "done":
                _STATE["done"].append(label)
            else:
                _STATE["failed"].append(f"{label}: {job.get('error') or job.get('state')}")
            _STATE["current"] = None
    with _LOCK:
        failed = list(_STATE["failed"])
        _STATE.update(state="error" if failed else "done", finished=_now(),
                      message=(f"{len(failed)} build(s) failed - see the job log" if failed
                               else "all saved areas are tiled"))


_STOP = threading.Event()


def start(delay: float = 0.0) -> bool:
    """Start one reconcile pass in the background; False if one is running."""
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return False
        _STATE.update(state="checking", message="checking which areas need tiles")

        def target() -> None:
            if delay and _STOP.wait(delay):
                return
            try:
                _run(_STOP)
            except Exception as exc:  # noqa: BLE001
                log.exception("autobuild crashed")
                _set(state="error", message=f"autobuild crashed: {exc}", current=None, finished=_now())

        _THREAD = threading.Thread(target=target, name="autobuild", daemon=True)
        _THREAD.start()
    return True


def stop() -> None:
    _STOP.set()


def wait(timeout: float = 30.0) -> None:
    thread = _THREAD
    if thread is not None:
        thread.join(timeout)
    time.sleep(0)
