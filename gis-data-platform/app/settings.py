"""Engine / platform settings editable from the data-platform's Settings page.

One JSON file on the shared volume (``<data>/settings/settings.json``), read
by both containers. Resolution per setting: saved value > environment
variable (docker-compose / .env) > built-in default. Each setting says when a
change takes effect (``applies``):

* ``live``      - read on every use (automation loops, watchdogs)
* ``next-job``  - read when a build / download job starts (passed to the
                  engine subprocess as environment variables)
* ``restart``   - read once at start-up (shown, editable, needs a restart)

Container hard limits (Docker ``mem_limit`` / ``cpus``) are NOT settings: a
container cannot change its own cgroup. They are reported read-only by the
runtime endpoints and set in ``.env``. The soft build memory budget below
caps the engine inside that limit and works without a restart.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from . import config

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"mtime": None, "values": {}}

# key, env, type, default, min, max, step, unit, applies, group, label, help
_SPECS: list[dict[str, Any]] = [
    # ---- engine: tile builds -------------------------------------------------
    dict(key="buildMemBudgetGB", env="BUILD_MEM_BUDGET_GB", type="float", default=0.0, min=0, max=1024,
         step=0.5, unit="GB", applies="next-job", group="build",
         label="Build RAM limit",
         help="Memory the engine may use for a tile build. Workers are sized to fit and new work pauses "
              "above the high-water mark of this limit. 0 = use the whole container limit."),
    dict(key="buildWorkers", env="BUILD_MAX_WORKERS", type="int", default=0, min=0, max=256, step=1,
         unit="workers", applies="next-job", group="build",
         label="Max build workers",
         help="Upper bound on parallel build processes. 0 = automatic (CPU count, limited by RAM)."),
    dict(key="buildWorkerGB", env="BUILD_WORKER_GB", type="float", default=2.0, min=0.25, max=64, step=0.25,
         unit="GB", applies="next-job", group="build",
         label="RAM per worker",
         help="Memory reserved per build worker when sizing the pool. Lower = more workers (more risk of "
              "memory pressure), higher = fewer workers."),
    dict(key="buildMemHigh", env="BUILD_MEM_HIGH", type="float", default=0.75, min=0.3, max=0.95,
         step=0.05, unit="fraction", applies="next-job", group="build",
         label="Memory high-water mark",
         help="Stop handing out new tiles above this fraction of the RAM limit (the watchdog restarts the "
              "pool with fewer workers if it persists)."),
    dict(key="gdalCacheMB", env="GDAL_CACHEMAX", type="int", default=128, min=16, max=16384, step=16,
         unit="MB", applies="next-job", group="build",
         label="GDAL cache per worker",
         help="Raster block cache of each build worker. Counts against RAM per worker."),
    dict(key="buildRecycleChunks", env="BUILD_RECYCLE_CHUNKS", type="int", default=100, min=1,
         max=100000, step=1, unit="chunks", applies="next-job", group="build",
         label="Recycle workers every",
         help="Restart each worker after this many 64-tile chunks (bounds memory growth)."),
    dict(key="buildMaxFailFrac", env="BUILD_MAX_FAIL_FRAC", type="float", default=0.01, min=0, max=1,
         step=0.005, unit="fraction", applies="next-job", group="build",
         label="Abort when tiles fail above",
         help="A build stops when more than this fraction of its tiles fail (minimum 5 tiles)."),
    dict(key="buildProgressS", env="BUILD_PROGRESS_EVERY_S", type="float", default=3.0, min=0.5, max=600,
         step=0.5, unit="s", applies="next-job", group="build",
         label="Progress line every",
         help="How often a running build reports its progress (live % in the Data library)."),
    dict(key="jobIdleTimeoutS", env="GIS_JOB_IDLE_TIMEOUT_S", type="int", default=1800, min=60,
         max=86400, step=60, unit="s", applies="live", group="build",
         label="Kill a silent job after",
         help="A build or download step that prints nothing for this long is terminated."),
    # ---- engine: automation --------------------------------------------------
    dict(key="autobuild", env="GIS_AUTOBUILD", type="bool", default=True, applies="live", group="auto",
         label="Convert automatically",
         help="The engine builds missing / newly downloaded areas by itself. Off = only “Convert now” in the "
              "Data library and download hand-offs build."),
    dict(key="autobuildIntervalS", env="GIS_AUTOBUILD_INTERVAL_S", type="float", default=60.0, min=0,
         max=86400, step=5, unit="s", applies="live", group="auto",
         label="Check for new areas every",
         help="How often the engine looks for areas that need tiles (0 = only at engine start-up)."),
    dict(key="autobuildMinFreeGB", env="GIS_AUTOBUILD_MIN_FREE_GB", type="float", default=10.0, min=0,
         max=100000, step=1, unit="GB", applies="live", group="auto",
         label="Minimum free disk",
         help="Automatic conversion stops when the data disk has less free space than this."),
    dict(key="conversionWatchS", env="GIS_CONVERSION_WATCH_S", type="float", default=15.0, min=0,
         max=3600, step=1, unit="s", applies="restart", group="auto",
         label="Library tile-count refresh",
         help="Background refresh of the Data library's tile counts (0 = only when the page asks). "
              "Needs an engine restart."),
    # ---- data-platform: downloads ------------------------------------------------
    dict(key="imageryWorkers", env="GIS_IMAGERY_WORKERS", type="int", default=6, min=1, max=32, step=1,
         unit="connections", applies="next-job", group="downloads",
         label="Imagery tile downloads in parallel",
         help="Parallel HTTP downloads for ready-tile providers (EOX, Blue Marble)."),
    dict(key="s2Workers", env="GIS_S2_WORKERS", type="int", default=3, min=1, max=16, step=1,
         unit="blocks", applies="next-job", group="downloads",
         label="Sentinel-2 blocks in parallel",
         help="Blocks of 8x8 tiles built at once from raw scenes. Each needs ~350 MB; the data-platform "
              "container has a 2 GB limit by default."),
    dict(key="s2MaxCloud", env="GIS_S2_MAX_CLOUD", type="float", default=20.0, min=0, max=100, step=1,
         unit="%", applies="next-job", group="downloads",
         label="Sentinel-2 default max cloud",
         help="Scene cloud limit when the download form leaves it empty."),
    dict(key="s2Days", env="GIS_S2_DAYS", type="int", default=365, min=7, max=4000, step=1,
         unit="days", applies="next-job", group="downloads",
         label="Sentinel-2 default date range",
         help="Scenes from the last N days when the download form leaves the dates empty."),
    dict(key="s2MedianScenes", env="GIS_S2_MEDIAN_SCENES", type="int", default=5, min=1, max=15, step=1,
         unit="scenes", applies="next-job", group="downloads",
         label="Sentinel-2 scenes per pixel",
         help="Clear looks combined (median) per pixel. More = fewer leftover clouds, slower."),
    dict(key="maxImageryTiles", env="GIS_MAX_IMAGERY_TILES", type="int", default=50000, min=100,
         max=2000000, step=1000, unit="tiles", applies="next-job", group="downloads",
         label="Max imagery tiles per job",
         help="Largest imagery job accepted (all zoom levels)."),
]
SPECS: dict[str, dict[str, Any]] = {spec["key"]: spec for spec in _SPECS}
GROUPS = [
    ("build", "Engine — tile builds", "Resources and safety limits of a build (applied when the next build starts)."),
    ("auto", "Engine — automatic conversion", "What the engine does by itself."),
    ("downloads", "Data-platform — downloads", "Imagery and Sentinel-2 download jobs."),
]


class SettingsError(ValueError):
    pass


def path() -> Path:
    return config.DATA_ROOT / "settings" / "settings.json"


def _saved() -> dict[str, Any]:
    p = path()
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        return {}
    key = (str(p), mtime)
    with _LOCK:
        if _CACHE["mtime"] == key:
            return dict(_CACHE["values"])
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    values = data.get("values") if isinstance(data, dict) else None
    values = {k: v for k, v in (values or {}).items() if k in SPECS}
    with _LOCK:
        _CACHE.update(mtime=key, values=values)
    return dict(values)


def _coerce(spec: dict[str, Any], raw: Any, check_range: bool = True) -> Any:
    kind = spec["type"]
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise SettingsError(f"{spec['label']}: expected on/off")
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = float(raw)
    except (TypeError, ValueError):
        raise SettingsError(f"{spec['label']}: expected a number") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise SettingsError(f"{spec['label']}: expected a finite number")
    if kind == "int":
        if value != int(value):
            raise SettingsError(f"{spec['label']}: expected a whole number")
        value = int(value)
    lo, hi = spec.get("min"), spec.get("max")
    if check_range and (lo is not None and value < lo or hi is not None and value > hi):
        raise SettingsError(f"{spec['label']}: must be between {lo} and {hi} {spec.get('unit', '')}".rstrip())
    return value


def _from_env(spec: dict[str, Any]) -> Any:
    raw = os.environ.get(spec["env"])
    if raw is None or raw == "":
        return None
    try:
        return _coerce(spec, raw, check_range=False)   # .env is the admin's call: no UI range
    except SettingsError:
        return None


def resolve(key: str) -> tuple[Any, str]:
    """(value, source) with source 'saved' | 'env' | 'default'."""
    spec = SPECS[key]
    saved = _saved()
    if key in saved:
        try:
            return _coerce(spec, saved[key]), "saved"
        except SettingsError:
            pass
    env = _from_env(spec)
    if env is not None:
        return env, "env"
    return spec["default"], "default"


def get(key: str) -> Any:
    return resolve(key)[0]


def saved_or(key: str, fallback: Any) -> Any:
    """The saved value, else `fallback` (callers that keep their own env/default)."""
    saved = _saved()
    if key in saved:
        try:
            return _coerce(SPECS[key], saved[key])
        except SettingsError:
            pass
    return fallback


ENGINE_GROUPS = ("build", "auto")


def effective(groups: tuple[str, ...] = ENGINE_GROUPS) -> dict[str, dict[str, Any]]:
    """{key: {value, source, envValue}} as THIS process resolves them (the
    engine container has its own .env variables)."""
    out = {}
    for spec in _SPECS:
        if spec["group"] in groups:
            value, source = resolve(spec["key"])
            out[spec["key"]] = {"value": value, "source": source, "envValue": _from_env(spec)}
    return out


def describe(engine_view: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """All settings for the page. `engine_view` (from the engine's
    /api/runtime) replaces how engine settings resolve: .env variables of the
    engine container are not visible in the data-platform container."""
    groups = []
    for gid, title, text in GROUPS:
        items = []
        for spec in _SPECS:
            if spec["group"] != gid:
                continue
            value, source = resolve(spec["key"])
            env = _from_env(spec)
            seen = (engine_view or {}).get(spec["key"])
            if isinstance(seen, dict) and "value" in seen:
                value, source, env = seen["value"], seen.get("source", source), seen.get("envValue")
            items.append({**{k: v for k, v in spec.items() if k != "group"},
                          "value": value, "source": source,
                          "envValue": env, "fallback": env if env is not None else spec["default"]})
        groups.append({"id": gid, "title": title, "description": text, "items": items})
    return {"groups": groups, "path": str(path()), "updated": _meta().get("updated"),
            "engineView": engine_view is not None}


def _meta() -> dict[str, Any]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update(values: dict[str, Any], reset: list[str] | None = None,
           engine_view_fn=None) -> dict[str, Any]:
    """Validate and save `values` (None = remove = back to env/default);
    `reset` keys are removed too. All-or-nothing."""
    unknown = [k for k in list(values) + list(reset or []) if k not in SPECS]
    if unknown:
        raise SettingsError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    current = _saved()
    for key, raw in values.items():
        if raw is None:
            current.pop(key, None)
        else:
            current[key] = _coerce(SPECS[key], raw)
    for key in reset or []:
        current.pop(key, None)
    _write(current)
    return describe(engine_view_fn() if engine_view_fn else None)


def _write(values: dict[str, Any]) -> None:
    from datetime import datetime, timezone

    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps({"values": values, "updated": datetime.now(timezone.utc).isoformat()},
                               indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(temp, 0o644)
    os.replace(temp, p)
    with _LOCK:
        _CACHE.update(mtime=None, values={})


def job_env() -> dict[str, str]:
    """Environment for an engine build subprocess (next-job build settings)."""
    out: dict[str, str] = {}
    for spec in _SPECS:
        if spec["group"] == "build" and spec["applies"] == "next-job":
            value = get(spec["key"])
            out[spec["env"]] = str(value)
    return out


# ---- runtime facts (read-only) ---------------------------------------------------

def _read(path_: str) -> str | None:
    try:
        with open(path_, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def runtime() -> dict[str, Any]:
    """This container's limits and usage (cgroup v2 when available)."""
    import shutil

    mem_max = _read("/sys/fs/cgroup/memory.max")
    mem_cur = _read("/sys/fs/cgroup/memory.current")
    cpu_max = _read("/sys/fs/cgroup/cpu.max")
    meminfo: dict[str, int] = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        name, _, rest = line.partition(":")
        try:
            meminfo[name] = int(rest.split()[0]) * 1024
        except (IndexError, ValueError):
            pass
    cpus_quota = None
    if cpu_max and not cpu_max.startswith("max"):
        try:
            quota, period = cpu_max.split()
            cpus_quota = round(int(quota) / int(period), 2)
        except ValueError:
            pass
    try:
        disk = shutil.disk_usage(config.DATA_ROOT)
        disk_info = {"total": disk.total, "free": disk.free, "used": disk.used}
    except OSError:
        disk_info = None
    limit = int(mem_max) if mem_max and mem_max.isdigit() else None
    return {
        "role": config.ROLE,
        "memoryLimit": limit,                      # bytes, None = unlimited (VM RAM)
        "memoryUsed": int(mem_cur) if mem_cur and mem_cur.isdigit() else None,
        "vmMemoryTotal": meminfo.get("MemTotal"),
        "vmMemoryAvailable": meminfo.get("MemAvailable"),
        "cpuCount": os.cpu_count(),
        "cpuQuota": cpus_quota,                    # None = all CPUs
        "disk": disk_info,
    }
