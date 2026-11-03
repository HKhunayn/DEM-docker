"""Disk statistics of tile pyramids (counts + sizes) without blocking requests.

Used by the engine's conversion status (app/conversion.py) and the
data-platform's library page (imagery sets). Counting a country-scale
pyramid (~530k files) takes seconds, so every measurement runs in ONE
background worker thread and is cached together with a cheap *signature*
(a few stat() calls). A request only ever reads the cache: when the
signature changed or the value is too old it schedules a re-measurement and
returns the previous value flagged ``measuring``.

Cached values are persisted (JSON in the data volume) so a restart does not
re-count an unchanged pyramid.
"""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

MAX_LAT = 85.0511287798066


# ---- tile math (same as engine/scripts/build_tiles.py; parity is tested) ----

def _frac_tile(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = 2 ** z
    lon = min(max(lon, -180.0), 180.0)
    lat = min(max(lat, -MAX_LAT), MAX_LAT)
    lat_r = math.radians(lat)
    fx = (lon + 180.0) / 360.0 * n
    fy = (1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n
    return fx, fy


def tile_range(bbox, z: int) -> tuple[int, int, int, int]:
    """Inclusive (x0, y0, x1, y1) of the tiles a build of `bbox` writes at z."""
    minlon, minlat, maxlon, maxlat = (float(v) for v in bbox)
    n = 2 ** z
    fx0, fy0 = _frac_tile(minlon, maxlat, z)
    x0 = min(max(int(math.floor(fx0)), 0), n - 1)
    y0 = min(max(int(math.floor(fy0)), 0), n - 1)
    fx, fy = _frac_tile(maxlon, minlat, z)
    eps = 1e-9
    x1 = min(max(int(math.ceil(fx - eps)) - 1, x0), n - 1)
    y1 = min(max(int(math.ceil(fy - eps)) - 1, y0), n - 1)
    return x0, y0, x1, y1


def expected_tiles(bbox, minzoom: int, maxzoom: int) -> dict[int, int]:
    """Tiles a build writes (or skips/finds empty) per zoom."""
    out = {}
    for z in range(int(minzoom), int(maxzoom) + 1):
        x0, y0, x1, y1 = tile_range(bbox, z)
        out[z] = (x1 - x0 + 1) * (y1 - y0 + 1)
    return out


# ---- counting ---------------------------------------------------------------

def scan_level(level_dir: str | Path, outer: str, rng: tuple[int, int, int, int] | None,
               ext: str = "") -> dict[str, int]:
    """One zoom level laid out <level>/<outer>/<inner><ext>.

    outer = "y" (ArcGIS tile/<z>/<row>/<col>) or "x" (XYZ <z>/<x>/<y>.webp).
    Returns {files, bytes, present}: present = non-empty tiles inside `rng`
    with the expected extension (what a resumed build would skip)."""
    files = nbytes = present = 0
    try:
        top = os.scandir(level_dir)
    except OSError:
        return {"files": 0, "bytes": 0, "present": 0}
    with top:
        for d in top:
            if not d.name.isdigit():
                continue
            try:
                if not d.is_dir():
                    continue
            except OSError:
                continue
            o = int(d.name)
            in_outer = rng is not None and (rng[1] <= o <= rng[3] if outer == "y" else rng[0] <= o <= rng[2])
            try:
                inner_it = os.scandir(d.path)
            except OSError:
                continue
            with inner_it:
                for f in inner_it:
                    name = f.name
                    if name.endswith(".part") or name.endswith(".tmp"):
                        continue
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    if not f.is_file():
                        continue
                    files += 1
                    nbytes += st.st_size
                    if not in_outer or rng is None or st.st_size <= 0:
                        continue
                    if ext:
                        if not name.endswith(ext):
                            continue
                        stem = name[: -len(ext)]
                    else:
                        stem = name
                    if not stem.isdigit():
                        continue
                    i = int(stem)
                    if rng[0] <= i <= rng[2] if outer == "y" else rng[1] <= i <= rng[3]:
                        present += 1
    return {"files": files, "bytes": nbytes, "present": present}


def scan_pyramid(root: str | Path, outer: str, bbox=None, minzoom: int = 0, maxzoom: int = -1,
                 ext: str = "") -> dict[str, Any]:
    """Every zoom directory under `root`: per-zoom {files, bytes, present,
    expected} (present/expected only for zooms in [minzoom, maxzoom] of bbox)
    plus totals."""
    zooms: dict[int, dict[str, int]] = {}
    try:
        names = [e.name for e in os.scandir(root) if e.name.isdigit() and e.is_dir()]
    except OSError:
        names = []
    wanted = set(range(int(minzoom), int(maxzoom) + 1)) if bbox is not None else set()
    for z in sorted({int(n) for n in names} | wanted):
        rng = tile_range(bbox, z) if z in wanted else None
        stats = scan_level(os.path.join(root, str(z)), outer, rng, ext) if str(z) in names \
            else {"files": 0, "bytes": 0, "present": 0}
        if rng is not None:
            stats["expected"] = (rng[2] - rng[0] + 1) * (rng[3] - rng[1] + 1)
        zooms[z] = stats
    return {"zooms": {str(z): v for z, v in zooms.items()},
            "files": sum(v["files"] for v in zooms.values()),
            "bytes": sum(v["bytes"] for v in zooms.values())}


def scan_tree(root: str | Path) -> dict[str, int]:
    """All regular files under root: {files, bytes}."""
    files = nbytes = 0
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        files += 1
                        nbytes += e.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
    return {"files": files, "bytes": nbytes}


def mtime_ns(path: str | Path) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def dir_signature(root: str | Path) -> list:
    """Cheap change marker of a pyramid: mtimes of root and its zoom dirs
    (they change when a row/column directory is added or removed)."""
    out: list = [mtime_ns(root)]
    try:
        with os.scandir(root) as it:
            for e in sorted(it, key=lambda e: e.name):
                if e.name.isdigit():
                    out.append([e.name, mtime_ns(e.path)])
    except OSError:
        pass
    return out


# ---- background measurement cache -------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Measurer:
    """Cache of slow measurements keyed by name, refreshed by one worker thread.

    get(key, signature, fn, max_age) never blocks on `fn`: it returns the
    cached value (None before the first measurement) and an info dict
    {measuredAt, seconds, measuring, ageS}; when the signature differs or the
    value is older than max_age it queues `fn` (once per key)."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, tuple[str, Callable[[], Any]]] = {}
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._load()

    # persistence
    def _load(self) -> None:
        if not self.path:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self._cache = {k: v for k, v in data.items() if isinstance(v, dict) and "signature" in v}

    def _save(self) -> None:
        if not self.path:
            return
        with self._lock:
            data = json.dumps(self._cache)
        tmp = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            log.exception("could not save %s", self.path)
            try:
                tmp.unlink()
            except OSError:
                pass

    # worker
    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._work, name="tile-stats", daemon=True)
        self._thread.start()

    def _work(self) -> None:
        while True:
            key = self._queue.get()
            with self._lock:
                job = self._pending.get(key)
            if job is None:
                continue
            sig, fn = job
            t0 = time.monotonic()
            try:
                value, error = fn(), None
            except Exception as exc:  # noqa: BLE001 - reported in the status
                log.exception("measuring %s failed", key)
                value, error = None, str(exc)
            seconds = round(time.monotonic() - t0, 3)
            with self._lock:
                self._pending.pop(key, None)
                entry = {"signature": sig, "value": value, "measuredAt": _now_iso(),
                         "t": time.time(), "seconds": seconds}
                if error:
                    entry["error"] = error
                self._cache[key] = entry
            self._save()

    def get(self, key: str, signature: Any, fn: Callable[[], Any],
            max_age: float | None = None) -> tuple[Any, dict[str, Any]]:
        sig = json.dumps(signature, sort_keys=True, default=str)
        with self._lock:
            entry = self._cache.get(key)
            # max_age adapts to the cost of a count (never more than ~20 % busy)
            limit = None if max_age is None else max(max_age, 5 * float((entry or {}).get("seconds") or 0))
            fresh = entry is not None and entry.get("signature") == sig and (
                limit is None or time.time() - float(entry.get("t") or 0) < limit)
            measuring = key in self._pending
            if not fresh and not measuring:
                self._pending[key] = (sig, fn)
                self._queue.put(key)
                measuring = True
                self._ensure_thread()
        info = {"measuredAt": entry.get("measuredAt") if entry else None,
                "seconds": entry.get("seconds") if entry else None,
                "ageS": round(time.time() - float(entry.get("t") or 0), 1) if entry else None,
                "measuring": measuring, "upToDate": fresh}
        if entry and entry.get("error"):
            info["error"] = entry["error"]
        return (entry.get("value") if entry else None), info

    def invalidate(self, prefix: str = "") -> int:
        """Forget cached values (next get() re-measures)."""
        with self._lock:
            keys = [k for k in self._cache if k.startswith(prefix)]
            for k in keys:
                self._cache[k]["signature"] = None
        return len(keys)

    def idle(self) -> bool:
        with self._lock:
            return not self._pending

    def wait_idle(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.idle():
                return True
            time.sleep(0.02)
        return self.idle()
