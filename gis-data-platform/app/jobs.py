"""Background fetch/build jobs driven by the heightmap-30m engine CLI.

The DEM download/build/verify logic lives in the existing heightmap-30m project
(terrainhub.py + scripts/). This module schedules those CLI steps as one
background job and records step / elapsed / log / result for the fetch page.

Restart safety:
- every job record (without its log) is persisted as DATA_ROOT/jobs/<id>.json on
  each state transition (atomic tmp + os.replace); the full log is streamed to
  DATA_ROOT/jobs/<id>.log and only a bounded tail is kept in memory;
- at import the history is reloaded; jobs that were running/queued become
  ``interrupted`` and saved areas stuck in ``building`` are marked the same;
- the engine runs in its own process group so cancel/shutdown can terminate
  the whole tree; DATA_ROOT/.job.lock records the engine PID so a restarted API
  refuses to start a second engine while an orphan from the previous run lives.
"""

from __future__ import annotations

import atexit
import itertools
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import catalog
from .config import (  # noqa: F401  (re-exported)
    DEM_ROOT,
    ENGINE_ROOT,
    JOB_LOCK_PATH,
    JOBS_ROOT,
    TILE_ROOT,
    engine_python,
)

log = logging.getLogger(__name__)

STEPS = ("fetch", "build", "verify", "publish")
# Rebuild only an area's ArcGIS LERC service: verify/publish work on the shared
# Terrarium set, which a LERC-only build does not touch.
LERC_ONLY_STEPS = ("fetch", "build")
HISTORY_LIMIT = 20
LOG_TAIL_LIMIT = 5000          # lines kept in memory per job (full log is on disk)
DEFAULT_TAIL = 200
TERM_GRACE_S = 10.0            # SIGTERM -> SIGKILL grace for the engine group
SHUTDOWN_GRACE_S = 5.0         # shorter: docker stop SIGKILLs after 10 s
IDLE_TIMEOUT_S = float(os.environ.get("GIS_JOB_IDLE_TIMEOUT_S", "1800"))  # no-output watchdog
PROVIDER_CACHE_TTL_S = 300.0
STATES = ("queued", "running", "done", "error", "cancelled", "interrupted")
ACTIVE_STATES = ("queued", "running")
INTERRUPTED_MSG = "interrupted by server restart - start the same area again to resume (built tiles are kept)"
CANCELLED_MSG = "cancelled by user"

SOURCE_TILES_RE = re.compile(r"Done: (\d+) tile")
TILES_RE = re.compile(r"WROTE (\d+) tiles")
LERC_TILES_RE = re.compile(r"WROTE (\d+) LERC tiles")
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_LOCK = threading.Lock()
_PERSIST_LOCK = threading.Lock()
_PROBE_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_ORDER: list[str] = []
_CURRENT_ID: str | None = None
_PROVIDER_CACHE: dict[str, tuple[bool, str]] | None = None
_PROVIDER_CACHE_T: float = 0.0


class JobRunningError(RuntimeError):
    """Raised when a job is requested while another one is still running."""


class JobNotRunningError(RuntimeError):
    """Raised when cancelling a job that is not running."""


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


# ---- providers ----------------------------------------------------------------

def probe_providers() -> dict[str, tuple[bool, str]]:
    """Availability of each DEM provider on this engine install.

    Cached (failures included) for PROVIDER_CACHE_TTL_S so requests never
    re-spawn the engine python; a probe that times out or cannot start reports
    the provider unavailable with the reason instead of raising.
    """
    global _PROVIDER_CACHE, _PROVIDER_CACHE_T
    with _PROBE_LOCK:
        if _PROVIDER_CACHE is not None and time.monotonic() - _PROVIDER_CACHE_T < PROVIDER_CACHE_TTL_S:
            return _PROVIDER_CACHE
        engine_ok = (ENGINE_ROOT / "terrainhub.py").is_file()
        copernicus = (engine_ok, "" if engine_ok else f"engine not found at {ENGINE_ROOT}")
        if engine_ok:
            try:
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
            except subprocess.TimeoutExpired:
                fabdem = (False, "engine python probe timed out")
            except OSError as exc:
                fabdem = (False, f"engine python could not be started: {exc}")
        else:
            fabdem = (False, "engine not found")
        _PROVIDER_CACHE = {"copernicus": copernicus, "fabdem": fabdem}
        _PROVIDER_CACHE_T = time.monotonic()
        return _PROVIDER_CACHE


# ---- snapshots + persistence --------------------------------------------------

_PERSISTED_PRIVATE = ("_t0", "_finished_t", "_log_total")


def _clamp_tail(tail: int | None) -> int:
    if tail is None:
        return LOG_TAIL_LIMIT
    return max(0, min(int(tail), LOG_TAIL_LIMIT))


def _snapshot(job: dict[str, Any], tail: int | None = DEFAULT_TAIL, include_log: bool = True) -> dict[str, Any]:
    out = {k: v for k, v in job.items() if not k.startswith("_") and k != "log"}
    out["steps"] = [dict(step) for step in job["steps"]]
    if isinstance(out.get("result"), dict):
        out["result"] = dict(out["result"])
    lines = job["log"]
    if include_log:
        n = _clamp_tail(tail)
        out["log"] = list(itertools.islice(lines, max(0, len(lines) - n), None)) if n else []
    out["log_total"] = job.get("_log_total", len(lines))
    t0 = job.get("_t0")
    finished = job.get("_finished_t")
    out["elapsed"] = round((finished if finished is not None else time.time()) - t0, 3) if t0 else 0.0
    return out


def _record(job: dict[str, Any]) -> dict[str, Any]:
    rec = {k: v for k, v in job.items() if not k.startswith("_") and k != "log"}
    rec["steps"] = [dict(step) for step in job["steps"]]
    for key in _PERSISTED_PRIVATE:
        rec[key] = job.get(key)
    return rec


def _job_file(job_id: str, suffix: str) -> Path:
    return JOBS_ROOT / f"{job_id}{suffix}"


def _persist(job: dict[str, Any]) -> None:
    """Atomically write the job record (no log) to DATA_ROOT/jobs/<id>.json."""
    with _PERSIST_LOCK:
        with _LOCK:
            rec = _record(job)
        path = _job_file(rec["id"], ".json")
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError):
            log.exception("could not persist job %s", rec["id"])
            try:
                tmp.unlink()
            except OSError:
                pass


def _forget(job_id: str) -> None:
    for suffix in (".json", ".log"):
        try:
            _job_file(job_id, suffix).unlink()
        except OSError:
            pass


def _append_log(job: dict[str, Any], line: str) -> None:
    with _LOCK:
        job["log"].append(line)
        job["_log_total"] = job.get("_log_total", 0) + 1
        job["_last_output"] = time.monotonic()
        fh = job.get("_log_fh")
        if fh is not None:
            try:
                fh.write(line + "\n")
            except (OSError, ValueError):
                pass


def _open_log(job: dict[str, Any]) -> None:
    try:
        JOBS_ROOT.mkdir(parents=True, exist_ok=True)
        fh = open(_job_file(job["id"], ".log"), "a", encoding="utf-8", buffering=1)
    except OSError:
        log.exception("could not open log file for job %s", job["id"])
        fh = None
    with _LOCK:
        job["_log_fh"] = fh


def _close_log(job: dict[str, Any]) -> None:
    with _LOCK:
        fh = job.pop("_log_fh", None)
    if fh is not None:
        try:
            fh.close()
        except OSError:
            pass


def _register(job: dict[str, Any]) -> None:
    """Add a job to the in-memory registry (caller holds _LOCK)."""
    _JOBS[job["id"]] = job
    _ORDER.append(job["id"])
    while len(_ORDER) > HISTORY_LIMIT:
        old = _ORDER.pop(0)
        _JOBS.pop(old, None)
        _forget(old)


def _new_job(kind: str, name: str, provider: str, bbox, minzoom: int, maxzoom: int,
             area_id: str | None, steps: tuple[str, ...]) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "name": name,
        "provider": provider,
        "bbox": list(bbox),
        "minzoom": minzoom,
        "maxzoom": maxzoom,
        "areaId": area_id,
        "state": "running",
        "step": steps[0],
        "steps": [{"name": step, "state": "pending", "returncode": None} for step in steps],
        "started": _now(),
        "finished": None,
        "log": deque(maxlen=LOG_TAIL_LIMIT),
        "result": None,
        "error": None,
        "warning": None,
        "_t0": time.time(),
        "_finished_t": None,
        "_log_total": 0,
        "_last_output": time.monotonic(),
        "_abort": None,
        "_proc": None,
        "_stop": threading.Event(),
    }


# ---- process group + lock file ------------------------------------------------

def _popen_group_kwargs() -> dict[str, Any]:
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}


def _signal_group(proc: subprocess.Popen, hard: bool) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
        elif hard:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30)
            proc.kill()
        else:
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
    except (OSError, subprocess.SubprocessError):
        pass


def _terminate_group(proc: subprocess.Popen, grace: float = TERM_GRACE_S) -> None:
    """SIGTERM the engine's process group, SIGKILL it after `grace` seconds."""
    if proc.poll() is None:
        _signal_group(proc, hard=False)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            _signal_group(proc, hard=True)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.error("engine process %s did not exit after SIGKILL", proc.pid)
    if os.name == "posix":
        # Pool workers share the group and may outlive the leader.
        _signal_group(proc, hard=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    # Windows: os.kill(pid, 0) would TerminateProcess - query the exit code instead.
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # access denied -> exists
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False


def _is_engine_process(pid: int) -> bool:
    """Guard against PID reuse (e.g. a fresh container): the PID must be terrainhub."""
    if not Path("/proc").is_dir():
        return True
    try:
        if Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] == "Z":
            return False  # zombie: exited, waiting to be reaped
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return any(arg.endswith(b"terrainhub.py") for arg in argv)
    except (OSError, IndexError):
        return False


def _write_lock(pid: int, job_id: str, step: str) -> None:
    info = {"pid": pid, "jobId": job_id, "step": step, "apiPid": os.getpid(), "started": _now()}
    tmp = JOB_LOCK_PATH.with_name(f"{JOB_LOCK_PATH.name}.{uuid.uuid4().hex}.tmp")
    try:
        JOB_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(info), encoding="utf-8")
        os.replace(tmp, JOB_LOCK_PATH)
    except OSError:
        log.exception("could not write job lock %s", JOB_LOCK_PATH)


def _remove_lock(job_id: str | None = None) -> None:
    try:
        if job_id is not None:
            info = json.loads(JOB_LOCK_PATH.read_text(encoding="utf-8"))
            if info.get("jobId") != job_id:
                return
        JOB_LOCK_PATH.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def orphan_engine_pid() -> int | None:
    """PID of a live engine started by a previous API process, else None.

    A stale lock (dead PID, PID reused by another program, unreadable) is removed.
    """
    try:
        info = json.loads(JOB_LOCK_PATH.read_text(encoding="utf-8"))
        pid = int(info.get("pid", 0))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, AttributeError):
        pid = 0
    with _LOCK:
        own = {job["_proc"].pid for job in _JOBS.values() if job.get("_proc") is not None}
    if pid and pid in own:
        return None
    if pid and _pid_alive(pid) and _is_engine_process(pid):
        return pid
    _remove_lock()
    return None


def _check_can_start() -> None:
    """Caller holds _LOCK."""
    if _CURRENT_ID is not None and _JOBS.get(_CURRENT_ID, {}).get("state") in ACTIVE_STATES:
        raise JobRunningError("a fetch job is already running")


def _check_no_orphan() -> None:
    pid = orphan_engine_pid()
    if pid is not None:
        raise JobRunningError(
            f"an engine process from a previous server run is still running (PID {pid}, "
            f"lock file {JOB_LOCK_PATH}); wait for it to finish or stop it, then start the job again"
        )


# ---- elevation jobs -----------------------------------------------------------

def start_job(
    name: str,
    bbox: list[float],
    provider: str = "copernicus",
    minzoom: int = 0,
    maxzoom: int = 13,
    save_area: bool = True,
    lerc_only: bool = False,
    origin: str = "user",
) -> dict[str, Any]:
    """Start fetch -> build -> verify -> publish for an area. ``lerc_only``
    rebuilds just the area's ArcGIS LERC service (the published Terrarium set,
    which holds a single area, stays as it is). ``origin`` records who asked
    ("user" or "autobuild")."""
    global _CURRENT_ID
    catalog.check_area_provider(name, provider, kind="elevation")
    with _LOCK:
        _check_can_start()
    _check_no_orphan()
    with _LOCK:
        _check_can_start()
        area = None
        if save_area:
            area = catalog.upsert_area(
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
        job = _new_job("elevation", name, provider, bbox, minzoom, maxzoom,
                       area["id"] if area else None, LERC_ONLY_STEPS if lerc_only else STEPS)
        job["lercOnly"] = lerc_only
        job["origin"] = origin
        _register(job)
        _CURRENT_ID = job["id"]
        snap = _snapshot(job)
        thread = threading.Thread(
            target=_run_job,
            args=(job["id"], name, list(bbox), provider, minzoom, maxzoom, lerc_only),
            daemon=True,
            name=f"job-{job['id'][:8]}",
        )
        job["_thread"] = thread
    _persist(job)
    thread.start()
    return snap


def _watchdog(job: dict[str, Any]) -> None:
    """Terminate the engine when it prints nothing for IDLE_TIMEOUT_S."""
    timeout = IDLE_TIMEOUT_S
    interval = max(0.1, min(30.0, timeout / 10))
    while not job["_stop"].wait(interval):
        with _LOCK:
            proc = job.get("_proc")
            idle = time.monotonic() - job["_last_output"]
        if proc is None or proc.poll() is not None or idle < timeout:
            continue
        with _LOCK:
            if job["_abort"] is None:
                job["_abort"] = "stalled"
        _append_log(job, f"WATCHDOG engine printed nothing for {_fmt_duration(timeout)} - terminating it")
        _terminate_group(proc)


def _fmt_duration(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 120 else f"{seconds:.0f} s"


def _restore_tilejson(path: Path, backup: bytes | None) -> None:
    if backup is not None:
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.restore")
        tmp.write_bytes(backup)
        os.replace(tmp, path)
    elif path.exists():
        path.unlink()


def _run_job(
    job_id: str,
    name: str,
    bbox: list[float],
    provider: str,
    minzoom: int,
    maxzoom: int,
    lerc_only: bool = False,
) -> None:
    global _CURRENT_ID
    job = _JOBS[job_id]
    outcome = "error"
    error: str | None = None
    warning: str | None = None
    result: dict[str, Any] | None = None
    tilejson_path = TILE_ROOT / "tilejson.json"
    tilejson_backup: bytes | None = None
    build_started = build_ok = False
    matches: dict[str, int | None] = {"sourceTiles": None, "tilesBuilt": None, "lercTiles": None}
    verified_pass = False
    failed_step: str | None = None
    failed_rc: int | None = None
    verify_failed_rc: int | None = None
    try:
        _open_log(job)
        threading.Thread(target=_watchdog, args=(job,), daemon=True).start()
        # Preserve the previous tilejson so a failed build doesn't corrupt it.
        tilejson_backup = tilejson_path.read_bytes() if tilejson_path.exists() else None
        for entry in job["steps"]:
            step = entry["name"]
            if job["_abort"]:
                break
            with _LOCK:
                entry["state"] = "running"
                job["step"] = step
            _persist(job)
            if step == "build":
                build_started = True
            cmd = build_command(step, bbox, provider, minzoom, maxzoom, name)
            if lerc_only and step == "build":
                cmd.append("--lerc-only")
            proc = subprocess.Popen(
                cmd,
                cwd=str(ENGINE_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                # unbuffered: live log lines, and the watchdog sees real progress
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                **_popen_group_kwargs(),
            )
            with _LOCK:
                job["_proc"] = proc
                job["_last_output"] = time.monotonic()
                aborted = job["_abort"]
            _write_lock(proc.pid, job_id, step)
            if aborted:  # cancel raced with the spawn
                threading.Thread(target=_terminate_group, args=(proc,), daemon=True).start()
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip()
                _append_log(job, line)
                for key, regex in (("sourceTiles", SOURCE_TILES_RE), ("tilesBuilt", TILES_RE),
                                   ("lercTiles", LERC_TILES_RE)):
                    m = regex.search(line)
                    if m:
                        matches[key] = int(m.group(1))  # keep the last match
                # "stats match: PASS" (exact) or "stats match (sampled: ...): PASS"
                if line.startswith("stats match") and line.endswith(": PASS"):
                    verified_pass = True
            rc = proc.wait()
            with _LOCK:
                job["_proc"] = None
                entry["returncode"] = rc
                abort = job["_abort"]
                if abort:
                    entry["state"] = "cancelled" if abort == "cancelled" else "error"
                else:
                    entry["state"] = "done" if rc == 0 else "error"
            if abort:
                break
            if rc != 0:
                if step == "verify":
                    # A verify failure keeps the new build; still publish it so
                    # the served tiles match the metadata (warning below).
                    verify_failed_rc = rc
                    continue
                failed_step, failed_rc = step, rc
                break
            if step == "build":
                build_ok = True

        abort = job["_abort"]
        if abort == "cancelled":
            outcome, error = "cancelled", CANCELLED_MSG
        elif abort == "interrupted":
            outcome, error = "interrupted", INTERRUPTED_MSG
        elif abort == "stalled":
            outcome = "error"
            error = (f"{job['step']} step stalled: the engine printed nothing for "
                     f"{_fmt_duration(IDLE_TIMEOUT_S)} and was terminated")
        elif failed_step == "publish":
            outcome = "done"
            warning = (f"publish step failed (exit {failed_rc}); the new build was kept but "
                       "the previously published tiles are still served - check the log")
        elif failed_step is not None:
            outcome, error = "error", f"{failed_step} step failed (exit {failed_rc})"
        else:
            outcome = "done"
        if outcome == "done" and verify_failed_rc is not None:
            note = (f"verify step failed (exit {verify_failed_rc}); the new build was kept - "
                    "check the log for the verify differences")
            warning = f"{note}; {warning}" if warning else note
        if outcome == "done":
            result = {**matches, "verified": verified_pass and verify_failed_rc is None}
            if lerc_only:
                result["lercOnly"] = True
    except BaseException as exc:  # noqa: BLE001
        outcome, result = "error", None
        error = f"{job.get('step')} step failed ({exc})"
        _append_log(job, f"FATAL {exc}")
        log.exception("job %s crashed", job_id)
    finally:
        proc = job.get("_proc")
        if proc is not None:  # an exception left the engine running
            try:
                _terminate_group(proc)
            except Exception:  # noqa: BLE001
                log.exception("could not terminate engine for job %s", job_id)
        job["_stop"].set()
        if build_started and not build_ok:
            # Only a failed/cancelled BUILD rolls back the metadata, so a broken
            # build doesn't leave degenerate tilejson (e.g. maxzoom=0) that 404s
            # all terrain tiles. A verify-only failure keeps the new build.
            try:
                _restore_tilejson(tilejson_path, tilejson_backup)
            except OSError:
                log.exception("could not restore %s", tilejson_path)
        with _LOCK:
            job.update(
                state=outcome,
                error=error,
                warning=warning,
                result=result,
                finished=_now(),
                _finished_t=time.time(),
                _proc=None,
            )
            for entry in job["steps"]:
                if entry["state"] == "running":
                    entry["state"] = "error"
            if _CURRENT_ID == job_id:
                _CURRENT_ID = None
        _remove_lock(job_id)
        _close_log(job)
        _persist(job)

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
        if warning:
            dataset["warning"] = warning
        try:
            catalog.update_area(job["areaId"], dataset=dataset)
        except Exception:  # noqa: BLE001
            log.exception("could not update area %s for job %s", job["areaId"], job_id)


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
    existing = imagery.set_provider(name)
    if existing and existing != provider:
        raise catalog.NameConflictError(
            f"imagery set '{name}' already exists with provider '{existing}'; "
            f"delete it first (DELETE /api/imagery/{name}) or choose another name"
        )
    catalog.check_area_provider(name, provider, kind="imagery")
    with _LOCK:
        _check_can_start()
        dataset = _imagery_dataset(name, provider, bbox, minzoom, maxzoom, "building")
        area = catalog.upsert_area(name=name, bbox=list(bbox), provider=provider, minzoom=minzoom,
                                   maxzoom=maxzoom, dataset=dataset, kind="imagery") if save_area else None
        job = _new_job("imagery", name, provider, bbox, minzoom, maxzoom,
                       area["id"] if area else None, IMAGERY_STEPS)
        _register(job)
        _CURRENT_ID = job["id"]
        snap = _snapshot(job)
        thread = threading.Thread(target=_run_imagery_job, args=(job["id"],), daemon=True,
                                  name=f"imagery-{job['id'][:8]}")
        job["_thread"] = thread
    _persist(job)
    thread.start()
    return snap


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

    global _CURRENT_ID
    job = _JOBS[job_id]
    entry = job["steps"][0]
    outcome, error, result = "error", None, None
    try:
        _open_log(job)
        with _LOCK:
            entry["state"] = "running"
        result = imagery.download_set(job["name"], job["bbox"], job["provider"],
                                      job["minzoom"], job["maxzoom"],
                                      lambda line: _append_log(job, line), stop=job["_stop"])
        outcome = "done"
        entry.update(state="done", returncode=0)
    except BaseException as exc:  # noqa: BLE001
        entry.update(state="error", returncode=1)
        error = f"download step failed ({exc})"
        _append_log(job, f"FATAL {exc}")
    finally:
        abort = job["_abort"]
        if abort == "cancelled":
            outcome, error, result = "cancelled", CANCELLED_MSG, None
            entry["state"] = "cancelled"
        elif abort == "interrupted":
            outcome, error, result = "interrupted", INTERRUPTED_MSG, None
        with _LOCK:
            job.update(state=outcome, error=error, result=result, finished=_now(), _finished_t=time.time())
            if _CURRENT_ID == job_id:
                _CURRENT_ID = None
        job["_stop"].set()
        _close_log(job)
        _persist(job)
    if job["areaId"]:
        dataset = _imagery_dataset(job["name"], job["provider"], job["bbox"], job["minzoom"], job["maxzoom"], outcome)
        if result:
            dataset.update(tiles=result["tiles"], bytes=result["bytes"])
        if error:
            dataset["error"] = error
        try:
            catalog.update_area(job["areaId"], dataset=dataset)
        except Exception:  # noqa: BLE001
            log.exception("could not update area %s for imagery job %s", job["areaId"], job_id)


def imagery_set_busy(name: str) -> bool:
    """True while an imagery job is writing the set `name`."""
    with _LOCK:
        return any(
            job.get("kind") == "imagery" and job.get("name") == name and job.get("state") in ACTIVE_STATES
            for job in _JOBS.values()
        )


# ---- cancel / shutdown --------------------------------------------------------

def _abort_job(job: dict[str, Any], reason: str, grace: float) -> None:
    with _LOCK:
        if job["_abort"] is None:
            job["_abort"] = reason
        proc = job.get("_proc")
    if job.get("kind") == "imagery":
        job["_stop"].set()  # download_set polls it; elevation jobs stop via the engine exit
    if proc is not None:
        _terminate_group(proc, grace=grace)


def cancel_job(job_id: str, wait: float = TERM_GRACE_S + 6.0) -> dict[str, Any]:
    """Cancel a running job: terminate the engine group (SIGTERM, then SIGKILL).

    Raises KeyError for an unknown id and JobNotRunningError when not running.
    Returns the snapshot once the job settled (or after `wait` seconds).
    """
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["state"] not in ACTIVE_STATES:
            raise JobNotRunningError(f"job {job_id} is not running (state: {job['state']})")
    _append_log(job, "CANCEL requested by user")
    _abort_job(job, "cancelled", TERM_GRACE_S)
    thread = job.get("_thread")
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=wait)
    with _LOCK:
        return _snapshot(job)


def shutdown(grace: float = SHUTDOWN_GRACE_S, wait: float = 3.0) -> None:
    """Terminate any running engine process group (API shutdown hook).

    The job is recorded as ``interrupted`` so the next start can resume it.
    """
    with _LOCK:
        active = [job for job in _JOBS.values() if job.get("state") in ACTIVE_STATES]
    for job in active:
        _append_log(job, "SHUTDOWN server is stopping - terminating the engine")
        _abort_job(job, "interrupted", grace)
        thread = job.get("_thread")
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=wait)
        with _LOCK:
            still_active = job.get("state") in ACTIVE_STATES
            if still_active:
                job.update(state="interrupted", error=INTERRUPTED_MSG, finished=_now(), _finished_t=time.time())
        if still_active:
            _persist(job)
            _remove_lock(job["id"])


atexit.register(shutdown)


# ---- queries --------------------------------------------------------------------

def get_job(job_id: str, tail: int | None = DEFAULT_TAIL) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return _snapshot(job, tail) if job else None


def list_jobs() -> list[dict[str, Any]]:
    with _LOCK:
        return [_snapshot(_JOBS[job_id], include_log=False) for job_id in reversed(_ORDER)]


def current_job(tail: int | None = DEFAULT_TAIL) -> dict[str, Any] | None:
    with _LOCK:
        if not _ORDER:
            return None
        return _snapshot(_JOBS[_ORDER[-1]], tail)


def legacy_status() -> dict[str, Any]:
    """Old fetch-page /api/status shape: idle | running | done | error (+ cancelled/interrupted)."""
    job = current_job(tail=60)
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
        "log": job["log"],
        "started": job["started"],
        "finished": job["finished"],
        "step": job["step"],
        "elapsed": job["elapsed"],
        "result": job["result"],
        "jobId": job["id"],
        "name": job["name"],
        "kind": job.get("kind", "elevation"),
        "error": job.get("error"),
        "warning": job.get("warning"),
    }


# ---- startup reconciliation -----------------------------------------------------

def _read_log_tail(job_id: str) -> tuple[deque, int] | None:
    try:
        with open(_job_file(job_id, ".log"), encoding="utf-8", errors="replace") as fh:
            tail: deque = deque(maxlen=LOG_TAIL_LIMIT)
            total = 0
            for line in fh:
                tail.append(line.rstrip("\r\n"))
                total += 1
            return tail, total
    except OSError:
        return None


def _load_history() -> None:
    """Reload persisted jobs; mark jobs that a restart cut short as interrupted."""
    if not JOBS_ROOT.is_dir():
        return
    records: list[dict[str, Any]] = []
    for path in JOBS_ROOT.glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("ignoring unreadable job record %s", path)
            continue
        if not isinstance(rec, dict) or not _JOB_ID_RE.match(str(rec.get("id", ""))) \
                or path.stem != rec["id"] or not isinstance(rec.get("steps"), list):
            continue
        records.append(rec)
    records.sort(key=lambda r: str(r.get("started") or ""))
    for rec in records[:-HISTORY_LIMIT]:
        _forget(rec["id"])
    for rec in records[-HISTORY_LIMIT:]:
        job = dict(rec)
        for key, default in (("result", None), ("error", None), ("warning", None), ("finished", None),
                             ("kind", "elevation"), ("areaId", None), ("step", None), ("name", None),
                             ("provider", None), ("bbox", None), ("started", None)):
            job.setdefault(key, default)
        loaded = _read_log_tail(rec["id"])
        if loaded is not None:
            job["log"], job["_log_total"] = loaded
        else:
            job["log"], job["_log_total"] = deque(maxlen=LOG_TAIL_LIMIT), int(rec.get("_log_total") or 0)
        job.update(_abort=None, _proc=None, _stop=threading.Event())
        job["_stop"].set()
        stale = job.get("state") in ACTIVE_STATES or job.get("state") not in STATES
        if stale:
            mtimes = [p.stat().st_mtime for p in (_job_file(rec["id"], ".json"), _job_file(rec["id"], ".log"))
                      if p.exists()]
            job.update(state="interrupted", error=INTERRUPTED_MSG, result=None, finished=_now(),
                       _finished_t=max(mtimes) if mtimes else time.time())
            for entry in job["steps"]:
                if entry.get("state") == "running":
                    entry["state"] = "interrupted"
        _JOBS[job["id"]] = job
        _ORDER.append(job["id"])
        if stale:
            _persist(job)


def init() -> None:
    """Load job history and reconcile state left over from a previous run."""
    try:
        _load_history()
    except Exception:  # noqa: BLE001
        log.exception("could not load job history from %s", JOBS_ROOT)
    try:
        catalog.mark_interrupted_areas(INTERRUPTED_MSG)
    except Exception:  # noqa: BLE001
        log.exception("could not reconcile saved areas")
    try:
        orphan_engine_pid()  # drops a stale lock early
    except Exception:  # noqa: BLE001
        log.exception("could not check %s", JOB_LOCK_PATH)


init()


def _wait_threads(timeout: float = 10.0) -> None:
    """Test helper: wait for job threads to finish."""
    deadline = time.time() + timeout
    with _LOCK:
        threads = [job.get("_thread") for job in _JOBS.values()]
    for thread in threads:
        if isinstance(thread, threading.Thread) and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.time()))
