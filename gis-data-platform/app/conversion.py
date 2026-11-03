"""Conversion status of every saved elevation area (runs in the ENGINE).

For each area and each output:

- ArcGIS (Lerc1 ImageServer)  tiles/arcgis/rest/services/<name>/tile/<z>/<row>/<col>
- MapLibre/Cesium (Terrarium lossless WebP)  the single published set
  tiles/published/<version>/terrain/ (one area at a time = the primary area),
  or the raw tiles/terrarium/ pyramid of a full build that has not been
  published yet,

report state, percent converted, tiles done / expected and size on disk, the
live progress of a running conversion, and what "Convert now" would do
(``convert_plan``) - always the autobuild plan: only what is missing, never
redoing baked tiles (the build resumes from its manifests).

Percent: expected = sum over z in [minzoom, maxzoom] of the bbox tile range
(build_tiles.tile_range). Done = tiles a resumed build would trust:
manifest complete + current engine version -> 100 %; ``force_pending`` (an
interrupted forced rebuild) -> tiles of the zooms in ``zooms_done`` only; an
unfinished normal build -> the tiles present; tiles without a manifest or from
an older engine -> 0 % trusted ("stale", they are rebuilt).

Counting is done by app.tilestats.Measurer in a background thread; requests
only read the cache (``measuring`` while a count runs).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import autobuild, catalog, jobs
from .config import DATA_ROOT, TILE_ROOT
from .tilestats import Measurer, dir_signature, expected_tiles, mtime_ns, scan_pyramid, scan_tree

log = logging.getLogger(__name__)

STATS_PATH = DATA_ROOT / "stats" / "conversion-stats.json"
_MEASURER: Measurer | None = None
_MEASURER_LOCK = threading.Lock()
ACTIVE_REMEASURE_S = 3.0      # re-count an area's tiles this often while it converts
                              # (at least 5x the last count's duration)
_WATCH: threading.Thread | None = None
_WATCH_STOP = threading.Event()

ZOOM_LINE = re.compile(r"^z(\d+): x=\S+ y=\S+ tiles=(\d+) written=(\d+) skipped=(\d+) empty=(\d+) failed=(\d+)")
PROGRESS_LINE = re.compile(r"^\s*z(\d+) progress (\d+)/(\d+)")
PUBLISH_LINE = re.compile(r"^\s*publish z(\d+) done")
VERIFY_LINE = re.compile(r"^z(\d+): (?:\d+/\d+ tiles|no sample point)")


def measurer() -> Measurer:
    global _MEASURER
    with _MEASURER_LOCK:
        if _MEASURER is None:
            _MEASURER = Measurer(STATS_PATH)
        return _MEASURER


def reset_measurer(path: Path | None = None) -> Measurer:
    """Tests: a fresh cache (optionally at another path)."""
    global _MEASURER
    with _MEASURER_LOCK:
        _MEASURER = Measurer(path if path is not None else STATS_PATH)
        return _MEASURER


def _services_root() -> Path:
    return TILE_ROOT / "arcgis" / "rest" / "services"


def _pct(done: int, expected: int, complete: bool = False) -> int:
    if complete:
        return 100
    if expected <= 0:
        return 0
    return min(99, int(done * 100 // expected))


def _sum_present(scan: dict[str, Any] | None, zooms: set[int] | None = None) -> int:
    if not scan:
        return 0
    total = 0
    for z, v in scan.get("zooms", {}).items():
        if "expected" not in v:
            continue
        if zooms is not None and int(z) not in zooms:
            continue
        total += min(int(v.get("present", 0)), int(v["expected"]))
    return total


# ---- job progress -------------------------------------------------------------

def job_progress(job: dict[str, Any] | None) -> dict[str, Any] | None:
    """Live progress of an engine build job from its log: per-zoom totals
    (``z{z}: ... tiles=N written=W skipped=S``) + the running zoom's
    ``z{z} progress done/total``; publish zooms for the publish step."""
    if not job:
        return None
    bbox, minz, maxz = job.get("bbox"), job.get("minzoom"), job.get("maxzoom")
    try:
        expected = sum(expected_tiles(bbox, int(minz), int(maxz)).values())
    except (TypeError, ValueError):
        expected = 0
    finished: dict[int, int] = {}
    written = skipped = empty = failed = 0
    running_zoom, running_done = None, 0
    publish_done = verify_done = -1
    for line in job.get("log") or []:
        m = ZOOM_LINE.match(line)
        if m:
            z = int(m.group(1))
            finished[z] = int(m.group(2))
            written += int(m.group(3))
            skipped += int(m.group(4))
            empty += int(m.group(5))
            failed += int(m.group(6))
            if running_zoom == z:
                running_zoom, running_done = None, 0
            continue
        m = PROGRESS_LINE.match(line)
        if m:
            z = int(m.group(1))
            if z not in finished:
                running_zoom, running_done = z, int(m.group(2))
            continue
        m = PUBLISH_LINE.match(line)
        if m:
            publish_done = max(publish_done, int(m.group(1)))
            continue
        m = VERIFY_LINE.match(line)
        if m:
            verify_done = max(verify_done, int(m.group(1)))
    done = sum(finished.values()) + (running_done if running_zoom is not None else 0)
    build_pct = min(100.0, done * 100.0 / expected) if expected else 0.0
    steps = [s.get("name") for s in job.get("steps") or []]
    step = job.get("step")
    state = job.get("state")
    full = not job.get("lercOnly") and job.get("mode") != "lerc"
    step_states = {s.get("name"): s.get("state") for s in job.get("steps") or []}
    if step_states.get("build") == "done" or step in ("verify", "publish"):
        build_pct = 100.0
    publish_pct = 0.0
    if publish_done >= 0 and maxz is not None:
        publish_pct = min(100.0, (publish_done + 1) * 100.0 / (int(maxz) + 1))
    if step_states.get("publish") == "done":
        publish_pct = 100.0
    verify_pct = 0.0
    if verify_done >= 0 and maxz is not None and minz is not None:
        verify_pct = min(100.0, (verify_done - int(minz) + 1) * 100.0 / (int(maxz) - int(minz) + 1))
    # overall: check ~1 %, build the bulk, verify + publish the rest (full)
    if not full:
        overall = 1 + build_pct * 0.99 if step != "check" else 0.5
    elif step == "check":
        overall = 0.5
    elif step == "build":
        overall = 1 + build_pct * 0.84
    elif step == "verify":
        overall = 85 + verify_pct * 0.05
    else:
        overall = 90 + publish_pct * 0.10
    if state == "done":
        overall = 100.0
    zoom = running_zoom if running_zoom is not None else (max(finished) + 1 if finished else minz)
    return {
        "id": job.get("id"), "name": job.get("name"), "state": state, "step": step, "steps": steps,
        "mode": "full" if full else "lerc", "origin": job.get("origin"), "started": job.get("started"),
        "finished": job.get("finished"), "elapsed": job.get("elapsed"), "error": job.get("error"),
        "warning": job.get("warning"),
        "tilesDone": done, "tilesExpected": expected, "buildPercent": round(build_pct, 1),
        "written": written, "skipped": skipped, "empty": empty, "failed": failed,
        "zoom": zoom, "zoomsFinished": sorted(finished), "minzoom": minz, "maxzoom": maxz,
        "publishPercent": round(publish_pct, 1), "verifyPercent": round(verify_pct, 1),
        "percent": round(min(overall, 100.0), 1),
        "logTail": [ln for ln in (job.get("log") or [])[-4:]],
    }


def active_job() -> dict[str, Any] | None:
    job = jobs.current_job(tail=None)
    if job and job.get("state") in jobs.ACTIVE_STATES and job.get("kind", "elevation") == "elevation":
        return job
    return None


def _last_job_stamp(name: str) -> str:
    for j in jobs.list_jobs():
        if j.get("name") == name and j.get("kind", "elevation") == "elevation":
            return f"{j.get('id')}:{j.get('state')}:{j.get('finished')}"
    return ""


# ---- measurements -------------------------------------------------------------

def _measure_lerc(name: str, bbox, minz: int, maxz: int) -> dict[str, Any]:
    svc = _services_root() / name
    scan = scan_pyramid(svc / "tile", "y", bbox, minz, maxz)
    meta = scan_tree(svc)   # incl. ImageServer.json + manifest
    scan["files"], scan["bytes"] = meta["files"], meta["bytes"]
    return scan


def _measure_raw(bbox, minz: int, maxz: int) -> dict[str, Any]:
    return scan_pyramid(TILE_ROOT / "terrarium", "x", bbox, minz, maxz, ext=".webp")


def _measure_published(version: str) -> dict[str, Any]:
    return scan_tree(TILE_ROOT / "published" / version)


def _lerc_entry(area: dict[str, Any], expected: int, busy_here: dict[str, Any] | None) -> dict[str, Any]:
    name = str(area["name"])
    bbox, minz, maxz = area["bbox"], int(area.get("minzoom", 0)), int(area.get("maxzoom", 13))
    svc = _services_root() / name
    manifest = autobuild._read_json(svc / "build_manifest.json")
    status = autobuild.lerc_status(area)
    has_meta = (svc / "ImageServer.json").is_file()
    tile_root = svc / "tile"
    sig = [mtime_ns(svc / "build_manifest.json"), mtime_ns(svc / "ImageServer.json"),
           dir_signature(tile_root), bbox, minz, maxz, _last_job_stamp(name)]
    max_age = ACTIVE_REMEASURE_S if busy_here else None
    scan, info = measurer().get(f"lerc:{name}", sig,
                                lambda: _measure_lerc(name, bbox, minz, maxz), max_age=max_age)
    present = _sum_present(scan)
    want = autobuild.engine_version()
    entry: dict[str, Any] = {"expected": expected, "measured": info,
                             "files": scan.get("files") if scan else None,
                             "bytes": scan.get("bytes") if scan else None,
                             "tilesPresent": present if scan else None,
                             "url": f"/arcgis/rest/services/{name}/ImageServer",
                             "reason": status["reason"]}
    if isinstance(manifest, dict):
        entry["manifest"] = {"engineVersion": manifest.get("engine_version"),
                             "complete": bool(manifest.get("complete")),
                             "forcePending": manifest.get("force_pending")}
    exists = has_meta or isinstance(manifest, dict) or (scan is not None and (scan.get("files") or 0) > 0) \
        or tile_root.is_dir()
    if status["ok"]:
        state, done = "converted", expected
    elif not exists:
        state, done = "missing", 0
        entry["reason"] = "not converted yet"
    elif not isinstance(manifest, dict) or (want is not None and manifest.get("engine_version") != want):
        state, done = "stale", 0
    elif not autobuild._same_bbox(manifest.get("bbox"), area.get("bbox")) \
            or manifest.get("minzoom") != area.get("minzoom") or manifest.get("maxzoom") != area.get("maxzoom"):
        state, done = "stale", 0
    elif manifest.get("force_pending"):
        zd = {int(z) for z in (manifest["force_pending"].get("zooms_done") or [])}
        state, done = "partial", _sum_present(scan, zd)
        entry["reason"] = (f"an interrupted rebuild: zooms {sorted(zd) or 'none'} finished; "
                           f"the other zooms are rebuilt")
    else:
        state, done = "partial", present
        entry["reason"] = status["reason"] if manifest.get("complete") else \
            "an interrupted conversion - Convert now resumes it (finished tiles are kept)"
    entry["state"] = state
    entry["tilesDone"] = done if (scan is not None or state in ("converted", "missing", "stale")) else None
    entry["percent"] = _pct(done, expected, state == "converted") if entry["tilesDone"] is not None else None
    return entry


def _webp_entry(area: dict[str, Any], expected: int, published: dict[str, Any],
                primary: str | None, busy_here: dict[str, Any] | None) -> dict[str, Any]:
    bbox, minz, maxz = area["bbox"], int(area.get("minzoom", 0)), int(area.get("maxzoom", 13))
    current = published.get("current") or None
    entry: dict[str, Any] = {"expected": expected, "primary": primary}
    if current:
        entry["publishedVersion"] = current.get("version")
        entry["publishedBounds"] = current.get("bounds")
    own = bool(current) and autobuild._same_bbox(current.get("bounds"), bbox)
    raw_root = TILE_ROOT / "terrarium"
    raw_manifest = autobuild._read_json(raw_root / "build_manifest.json")
    raw_mine = isinstance(raw_manifest, dict) and autobuild._same_bbox(raw_manifest.get("bbox"), bbox) \
        and raw_manifest.get("minzoom") == minz and raw_manifest.get("maxzoom") == maxz
    if own and published.get("ok"):
        ver = current["version"]
        stats, info = measurer().get(f"published:{ver}", [ver, mtime_ns(TILE_ROOT / "published" / "current.json")],
                                     lambda: _measure_published(ver))
        counts = current.get("counts") or {}
        tiles = int(counts.get("linked", 0)) + int(counts.get("computed", 0)) if counts else None
        entry.update(state="converted", percent=100, tilesDone=tiles if tiles is not None else expected,
                     tilesPublished=tiles, flatTiles=counts.get("flat"), measured=info,
                     files=stats.get("files") if stats else None, bytes=stats.get("bytes") if stats else None,
                     reason="published (served to MapLibre/Cesium)", published=True)
        entry["expected"] = tiles if tiles else expected
        return entry
    if raw_mine and (raw_root.is_dir()):
        sig = [mtime_ns(raw_root / "build_manifest.json"), dir_signature(raw_root), bbox, minz, maxz,
               _last_job_stamp(str(area["name"]))]
        scan, info = measurer().get("raw:terrarium", sig, lambda: _measure_raw(bbox, minz, maxz),
                                    max_age=ACTIVE_REMEASURE_S if busy_here else None)
        want = autobuild.engine_version()
        if want is not None and raw_manifest.get("engine_version") != want:
            done, reason = 0, "raw tiles of an older engine (rebuilt)"
        elif raw_manifest.get("force_pending"):
            zd = {int(z) for z in (raw_manifest["force_pending"].get("zooms_done") or [])}
            done, reason = _sum_present(scan, zd), "an interrupted rebuild (not published yet)"
        else:
            done = expected if raw_manifest.get("complete") else _sum_present(scan)
            reason = ("built, not published yet" if raw_manifest.get("complete")
                      else "an interrupted build (not published yet)")
        entry.update(state="partial", tilesDone=done if scan is not None or raw_manifest.get("complete") else None,
                     measured=info, files=scan.get("files") if scan else None,
                     bytes=scan.get("bytes") if scan else None, reason=reason, published=False)
        entry["percent"] = _pct(done, expected) if entry["tilesDone"] is not None else None
        if own and not published.get("ok"):
            entry["reason"] += f"; the published set is outdated: {published.get('reason')}"
        return entry
    if own:   # published for this area, but invalid / older layout
        entry.update(state="stale", percent=0, tilesDone=0, reason=published.get("reason"), published=True)
        return entry
    if current and published.get("ok"):
        b = current.get("bounds") or [0, 0, 0, 0]
        inside = (float(bbox[0]) >= float(b[0]) - 1e-9 and float(bbox[1]) >= float(b[1]) - 1e-9
                  and float(bbox[2]) <= float(b[2]) + 1e-9 and float(bbox[3]) <= float(b[3]) + 1e-9)
        owner = primary or "another area"
        if inside:
            entry.update(state="covered", percent=None, tilesDone=None, published=False, coveredBy=owner,
                         reason=f"inside the published set of {owner} (MapLibre/Cesium already show it)")
        else:
            entry.update(state="not-published", percent=None, tilesDone=None, published=False,
                         reason=(f"not published - only one area is published at a time "
                                 f"(currently {owner})"))
        return entry
    entry.update(state="missing", percent=0, tilesDone=0, published=False,
                 reason=published.get("reason") or "nothing published")
    return entry


# ---- convert-now plan ---------------------------------------------------------

def convert_plan(area: dict[str, Any], plan: dict[str, Any], arcgis: dict[str, Any], webp: dict[str, Any],
                 busy: dict[str, Any] | None, publish: bool = False) -> dict[str, Any]:
    """What "Convert now" does for this area (never redoes baked tiles)."""
    name = area["name"]
    task = next((t for t in plan.get("tasks", []) if t["name"] == name), None)
    primary = plan.get("primary")
    out: dict[str, Any] = {"available": False, "mode": None, "publishAvailable": False}
    missing_lerc = None
    if arcgis.get("tilesDone") is not None:
        missing_lerc = max(0, int(arcgis["expected"]) - int(arcgis["tilesDone"]))
    out["missingArcgisTiles"] = missing_lerc
    lerc_line = ""
    if arcgis.get("state") == "stale":
        lerc_line = (f"rebuilds the ArcGIS tiles of this area ({arcgis.get('reason')}: existing tiles "
                     f"are not trusted)")
    elif arcgis.get("state") != "converted":
        lerc_line = (f"converts only the missing ArcGIS tiles"
                     + (f" ({missing_lerc:,} of {int(arcgis['expected']):,})" if missing_lerc is not None else "")
                     + (f"; the {int(arcgis['tilesDone']):,} tiles already baked are kept"
                        if arcgis.get("tilesDone") else ""))
    replaces = None
    cur = webp.get("publishedVersion")
    if cur and not webp.get("published") and primary and primary != name:
        replaces = primary
    if busy:
        out["reason"] = (f"the engine is converting {busy.get('name')} ({busy.get('step')}) - "
                         f"wait until it finishes")
        out["busy"] = True
    mode = None
    if task:
        mode = task["mode"]
        out["planReason"] = task["reason"]
    if publish and webp.get("state") not in ("converted",):
        mode = "full"
    if mode == "lerc":
        out["message"] = (lerc_line or "converts the missing ArcGIS tiles") + \
            ". The MapLibre/Cesium (WebP) set is not touched."
    elif mode == "full":
        parts = ["builds the MapLibre/Cesium (WebP) tiles of this area and publishes them"]
        if webp.get("state") == "partial" and webp.get("tilesDone"):
            parts.append(f"resuming: {int(webp['tilesDone']):,} WebP tiles already built are kept")
        if lerc_line:
            parts.append(lerc_line)
        elif arcgis.get("state") == "converted":
            parts.append("the ArcGIS tiles are already complete and are kept (not rewritten)")
        msg = "; ".join(parts) + "."
        if replaces:
            msg += (f" Only one area is published at a time: this REPLACES the published set of {replaces} "
                    f"(MapLibre/Cesium will show {name} instead; ArcGIS services are not affected).")
        out["message"] = msg
        out["replaces"] = replaces
    if mode:
        out["mode"] = mode
        out["available"] = not busy
    else:
        out["reason"] = out.get("reason") or "fully converted - nothing to do"
        out["fullyConverted"] = arcgis.get("state") == "converted"
    # publishing a non-primary area is a separate, explicit choice
    if webp.get("state") in ("covered", "not-published") and mode != "full":
        out["publishAvailable"] = not busy
        out["publishMessage"] = (
            f"Publish {name} for MapLibre/Cesium: builds its WebP tiles and publishes them. Only one area is "
            f"published at a time: this REPLACES the published set of {primary or 'the current area'}. "
            + ("ArcGIS tiles already baked are kept." if arcgis.get("state") == "converted" else lerc_line + "."))
    return out


# ---- overview -----------------------------------------------------------------

def _area_entry(area: dict[str, Any], plan: dict[str, Any], published: dict[str, Any],
                job: dict[str, Any] | None, progress: dict[str, Any] | None) -> dict[str, Any]:
    name = str(area["name"])
    bbox, minz, maxz = area["bbox"], int(area.get("minzoom", 0)), int(area.get("maxzoom", 13))
    try:
        per_zoom = expected_tiles(bbox, minz, maxz)
    except (TypeError, ValueError):
        per_zoom = {}
    expected = sum(per_zoom.values())
    busy_here = job if job and job.get("name") == name else None
    arcgis = _lerc_entry(area, expected, busy_here)
    webp = _webp_entry(area, expected, published, plan.get("primary"), busy_here)
    if busy_here and progress:
        step = progress.get("step")
        building = step == "build"
        live_pct = min(99.9, float(progress["buildPercent"])) if building else None
        # the log reports per zoom (+ every few s); the on-disk count of trusted
        # tiles (re-measured every few s while converting) can be ahead of it
        def live(entry: dict[str, Any]) -> tuple[Any, Any]:
            done = max(int(progress["tilesDone"]), int(entry.get("tilesDone") or 0)) if entry.get("state") != "stale" \
                else int(progress["tilesDone"])
            done = min(done, expected)
            return done, min(99.9, round(done * 100.0 / expected, 1)) if expected else live_pct
        note = (f"converting now: {progress['tilesDone']:,} of {progress['tilesExpected']:,} tiles processed "
                f"({progress['written']:,} written, {progress['skipped']:,} already baked and skipped)")
        if building or step == "check":
            a_done, a_pct = live(arcgis) if building else (arcgis.get("tilesDone"), arcgis.get("percent"))
            arcgis.update(state="converting", percent=a_pct, tilesDone=a_done,
                          reason=note if building else "converting now: checking the source DEMs")
            arcgis["live"] = True
        if progress.get("mode") == "full":
            if building or step == "check":
                w_done, w_pct = live(webp if webp.get("state") == "partial" else {}) if building else (0, 0)
                webp.update(state="converting", percent=w_pct, tilesDone=w_done, expected=expected,
                            reason=note if building else "converting now: checking the source DEMs")
            elif step == "publish":
                webp.update(state="publishing", percent=min(99.9, float(progress.get("publishPercent") or 0)),
                            reason="built - publishing the seamless WebP set (MapLibre/Cesium switch when it ends)")
            else:
                webp.update(state="verifying", percent=99,
                            reason=f"built - verifying the tiles against the DEMs ({progress.get('verifyPercent')}%)")
            webp["live"] = True
    row = next((r for r in plan.get("areas", []) if r["name"] == name), {})
    entry = {
        "name": name, "id": area.get("id"), "bbox": bbox, "minzoom": minz, "maxzoom": maxz,
        "provider": area.get("provider") or "copernicus", "primary": bool(row.get("primary")),
        "expectedTiles": expected, "expectedPerZoom": {str(z): n for z, n in per_zoom.items()},
        "datasetState": (area.get("dataset") or {}).get("state"),
        "arcgis": arcgis, "webp": webp, "job": progress if busy_here else None,
    }
    entry["convert"] = convert_plan(area, plan, arcgis, webp, job and {"name": job.get("name"), "step": job.get("step")})
    return entry


def overview(name: str | None = None) -> dict[str, Any]:
    """Conversion status of all saved elevation areas (or one)."""
    areas = autobuild.elevation_areas()
    if name is not None:
        areas = [a for a in areas if a.get("name") == name]
    published = autobuild.published_status()
    plan = autobuild.plan()
    job = active_job()
    progress = job_progress(job) if job else None
    rows = [_area_entry(a, plan, published, job, progress) for a in areas]
    status = autobuild.status() if name is None else None
    auto = (status or {}).get("autobuild") or {}
    m = measurer()
    return {
        "engineVersion": autobuild.engine_version(),
        "publishRevision": autobuild.publish_revision(),
        "primary": plan.get("primary"),
        "published": plan.get("published"),
        "job": progress,
        "busy": job is not None,
        "measuring": not m.idle(),
        "autobuild": {k: auto.get(k) for k in ("enabled", "state", "message", "intervalS", "queue")} if auto else None,
        "areas": rows,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def start_conversion(name: str, publish: bool = False) -> dict[str, Any]:
    """Start the engine job that converts only what is missing for `name`.
    Raises LookupError (no such area), RuntimeError (busy / nothing to do)."""
    area = catalog.find_area_by_name(name, "elevation")
    if not area or not (isinstance(area.get("bbox"), list) and len(area["bbox"]) == 4):
        raise LookupError(f"no saved elevation area '{name}'")
    info = overview(name)
    entry = info["areas"][0] if info["areas"] else None
    if entry is None:
        raise LookupError(f"no saved elevation area '{name}'")
    conv = convert_plan(area, autobuild.plan(), entry["arcgis"], entry["webp"],
                        info["job"] and {"name": info["job"]["name"], "step": info["job"]["step"]},
                        publish=publish)
    if conv.get("busy"):
        raise BusyError(conv["reason"])
    mode = conv.get("mode")
    if not mode:
        raise NothingToDo(conv.get("reason") or "fully converted - nothing to do")
    autobuild.forget_failure(name)
    snap = jobs.start_job(name, list(area["bbox"]), area.get("provider") or "copernicus",
                          int(area.get("minzoom", 0)), int(area.get("maxzoom", 13)),
                          lerc_only=mode == "lerc", origin="user")
    return {"state": "started", "mode": mode, "job": {k: snap.get(k) for k in ("id", "name", "step", "state", "mode")},
            "message": conv.get("message"), "replaces": conv.get("replaces")}


class BusyError(RuntimeError):
    pass


class NothingToDo(RuntimeError):
    pass


# ---- background watcher ---------------------------------------------------------

def _watch(interval: float) -> None:
    """Keep the cache warm: every `interval` s compute the overview (cheap
    stat() signatures), which schedules a re-count of whatever changed - after
    each job, when manifests change, and every ACTIVE_REMEASURE_S while an
    area converts."""
    while not _WATCH_STOP.wait(interval):
        try:
            overview()
        except Exception:  # noqa: BLE001
            log.exception("conversion status refresh failed")


def start_watch(interval: float | None = None) -> bool:
    global _WATCH
    if interval is None:
        try:
            interval = float(os.environ.get("GIS_CONVERSION_WATCH_S", "15"))
        except ValueError:
            interval = 15.0
    if interval <= 0:
        return False
    if _WATCH is not None and _WATCH.is_alive():
        return False
    _WATCH_STOP.clear()

    def run() -> None:
        try:
            overview()      # first pass right away (counts in the background)
        except Exception:  # noqa: BLE001
            log.exception("conversion status refresh failed")
        _watch(interval)

    _WATCH = threading.Thread(target=run, name="conversion-watch", daemon=True)
    _WATCH.start()
    return True


def stop_watch() -> None:
    _WATCH_STOP.set()
