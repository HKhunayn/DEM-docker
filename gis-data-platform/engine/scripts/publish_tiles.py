"""Publish the current build as a STATIC, seamless tile set that nginx serves
directly (no Python per request).

Input  (<root> = data/tiles):
  tilejson.json                      bounds / maxzoom / statistics of the build
  terrarium/{z}/{x}/{y}.webp|.png    raw Terrarium pyramid (.webp preferred)

Output (<root>/published/<version>/, swapped in atomically):
  terrain/{z}/{x}/{y}.webp   seamless Terrarium (lossless WebP): real heights
                             inside coverage, edge heights eased to a flat
                             base outside (no cliffs) - the exact algorithm
                             of app/terrain.py TerrainSource, baked once
  flat.webp                  base-height tile: nginx's fallback for every
                             terrain tile not written (beyond the fade)
  meta.json                  version, bounds, maxzoom, base
and <root>/published/current.json -> the live version (written last).
No hillshade: MapLibre and Cesium shade the elevation tiles in the browser.

SINGLE COPY ON DISK. Interior tiles whose raw tile is already WebP with no
transparent pixels are hard-linked into the new version (zero copy);
everything else is computed. Only after current.json points at the new
version are
  * all older published versions (and leftover .staging-* dirs) deleted
    (--keep, default 1 = only the live version), and
  * the raw pyramid deleted: terrarium/<z>/ dirs + terrarium/build_manifest.json
    (so the next build is a clean full build) and any legacy hillshade/
    pyramid. tilejson.json is kept (the API and publish meta read it).
--keep-raw skips the raw deletion (dev/tests). A failed publish deletes
nothing but its own staging dir. Publishing needs the raw pyramid: without it
the script exits 2 and changes nothing (the live version keeps serving) -
rebuild first.

usage: publish_tiles.py --root data/tiles [--workers N] [--keep 1] [--keep-raw]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import time
from collections import OrderedDict
from datetime import datetime, timezone

import numpy as np
from PIL import Image

# ---- constants: MUST match app/terrain.py (parity test enforces it) --------
TILE = 256
FADE_METERS = 4000.0
SMOOTH_METERS = 1200.0
BLEND_METERS = 600.0
EARTH_CIRCUMFERENCE = 40075016.686
MAX_LAT = 85.05112878
MAX_BLUR_RADIUS = 64
FILL_MAX_PX = TILE - MAX_BLUR_RADIUS
PUBLISH_REVISION = 3          # bump when published bytes change for the same build
WEBP_KW = dict(format="WEBP", lossless=True, exact=True, method=1, quality=25)
PROGRESS_EVERY = 4096
FIELD_CACHE = 64


# ---- numeric helpers (verbatim semantics of app/terrain.py) ----------------
def box_blur(a: np.ndarray, radius: int) -> np.ndarray:
    if radius < 1:
        return a.copy()
    out = a.astype(np.float64)
    for axis in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (radius + 1, radius)
        padded = np.pad(out, pad, mode="edge")
        csum = np.cumsum(padded, axis=axis)
        size = out.shape[axis]
        hi = np.take(csum, np.arange(2 * radius + 1, 2 * radius + 1 + size), axis=axis)
        lo = np.take(csum, np.arange(0, size), axis=axis)
        out = (hi - lo) / (2 * radius + 1)
    return out.astype(np.float32)


def global_px(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = TILE * (2 ** z)
    lat = max(min(lat, MAX_LAT), -MAX_LAT)
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def decode_terrarium(rgba: np.ndarray) -> np.ndarray:
    rgb = rgba[..., :3].astype(np.float64)
    heights = rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0
    nodata = heights < -1000.0
    if rgba.shape[-1] == 4:
        nodata |= rgba[..., 3] == 0
    heights[nodata] = np.nan
    return heights.astype(np.float32)


def terrarium_rgb(heights: np.ndarray) -> np.ndarray:
    v = np.clip(heights.astype(np.float64) + 32768.0, 0.0, 65535.996)
    r = np.floor(v / 256.0)
    g = np.floor(v - r * 256.0)
    b = np.floor((v - r * 256.0 - g) * 256.0)
    return np.stack([r, g, b], axis=-1).astype(np.uint8)


def webp_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, **WEBP_KW)
    return buf.getvalue()


def fill_holes(a: np.ndarray, max_dist: int | None = None) -> np.ndarray:
    if not np.isnan(a).any():
        return a

    def ffill(x: np.ndarray) -> np.ndarray:
        mask = np.isnan(x)
        cols = np.arange(x.shape[1])[None, :]
        idx = np.where(~mask, cols, -1)
        np.maximum.accumulate(idx, axis=1, out=idx)
        out = x[np.arange(x.shape[0])[:, None], np.maximum(idx, 0)]
        too_far = idx < 0
        if max_dist is not None:
            too_far |= (cols - idx) > max_dist
        return np.where(too_far, np.nan, out)

    def fill_rows(x: np.ndarray) -> np.ndarray:
        fwd = ffill(x)
        bwd = ffill(x[:, ::-1])[:, ::-1]
        return np.where(np.isnan(fwd), bwd, np.where(np.isnan(bwd), fwd, np.where(np.isnan(x), (fwd + bwd) / 2, x)))

    out = fill_rows(a)
    if np.isnan(out).any():
        out = fill_rows(out.T).T
    return out


# ---- build metadata ---------------------------------------------------------
def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def read_meta(root: str) -> dict:
    """bounds/maxzoom/base from tilejson.json; raises ValueError when the
    build metadata is missing or degenerate (never publish a broken build)."""
    with open(os.path.join(root, "tilejson.json"), encoding="utf-8") as f:
        data = json.load(f)
    stats = data.get("statistics") or {}
    base, top = _number(stats.get("min")), _number(stats.get("max"))
    if base is None or top is None or base > top:
        raise ValueError(f"degenerate statistics in tilejson: {stats}")
    bounds = data.get("bounds")
    if not isinstance(bounds, list) or len(bounds) != 4 or any(_number(v) is None for v in bounds):
        raise ValueError(f"invalid bounds in tilejson: {bounds}")
    west, south, east, north = (float(v) for v in bounds)
    west, east = (max(-180.0, min(180.0, v)) for v in (west, east))
    south, north = (max(-MAX_LAT, min(MAX_LAT, v)) for v in (south, north))
    if not (west < east and south < north):
        raise ValueError(f"empty bounds in tilejson: {bounds}")
    maxzoom = data.get("maxzoom", 13)
    if isinstance(maxzoom, bool) or not isinstance(maxzoom, int) or not 0 <= maxzoom <= 24:
        raise ValueError(f"invalid maxzoom in tilejson: {maxzoom}")
    return {"bounds": (west, south, east, north), "maxzoom": maxzoom, "base": base_height(base, top),
            "minzoom": int(data.get("minzoom", 0) or 0), "statistics": {"min": base, "max": top}}


def base_height(stat_min: float, stat_max: float) -> float:
    """Sea level when the dataset spans it (open sea has no Copernicus
    tiles), else the dataset minimum. Mirrors app/terrain.py base_height."""
    return 0.0 if stat_min <= 0.0 <= stat_max else stat_min


def coverage_rect(meta: dict, z: int) -> tuple[int, int, int, int]:
    west, south, east, north = meta["bounds"]
    fx0, fy0 = global_px(west, north, z)
    fx1, fy1 = global_px(east, south, z)
    gx0, gy0 = int(math.floor(fx0 + 0.5)), int(math.floor(fy0 + 0.5))
    gx1, gy1 = max(gx0 + 1, int(math.ceil(fx1 - 0.5))), max(gy0 + 1, int(math.ceil(fy1 - 0.5)))
    return gx0, gy0, gx1, gy1


def meters_per_px(meta: dict, z: int) -> float:
    lat = (meta["bounds"][1] + meta["bounds"][3]) / 2.0
    return EARTH_CIRCUMFERENCE * math.cos(math.radians(lat)) / (TILE * 2 ** z)


def raw_tile_path(root: str, z: int, x: int, y: int) -> tuple[str | None, str | None]:
    for ext in ("webp", "png"):
        p = os.path.join(root, "terrarium", str(z), str(x), f"{y}.{ext}")
        if os.path.exists(p):
            return p, ext
    return None, None


def load_rgba(path: str) -> np.ndarray | None:
    try:
        with Image.open(path) as im:
            rgba = np.asarray(im.convert("RGBA"))
    except (OSError, ValueError, SyntaxError):
        return None
    return rgba if rgba.shape[:2] == (TILE, TILE) else None


# ---- seamless height field (TerrainSource.heights, file-based) -------------
class Seamless:
    def __init__(self, root: str, meta: dict) -> None:
        self.root, self.meta = root, meta
        self._fields: OrderedDict = OrderedDict()
        self._raw: OrderedDict = OrderedDict()

    def _load(self, z, tx, ty):
        key = (z, tx, ty)
        if key in self._raw:
            self._raw.move_to_end(key)
            return self._raw[key]
        path, _ = raw_tile_path(self.root, z, tx, ty)
        rgba = load_rgba(path) if path else None
        value = decode_terrarium(rgba) if rgba is not None else None
        self._raw[key] = value
        if len(self._raw) > 4 * FIELD_CACHE:
            self._raw.popitem(last=False)
        return value

    def _field(self, z, tx, ty, rect):
        key = (z, tx, ty)
        if key in self._fields:
            self._fields.move_to_end(key)
            return self._fields[key]
        gx0, gy0, gx1, gy1 = rect
        wx0, wx1 = max(gx0, (tx - 1) * TILE), min(gx1, (tx + 2) * TILE)
        wy0, wy1 = max(gy0, (ty - 1) * TILE), min(gy1, (ty + 2) * TILE)
        field = None
        if wx1 > wx0 and wy1 > wy0:
            window = np.full((wy1 - wy0, wx1 - wx0), np.nan, dtype=np.float32)
            for sty in range(wy0 // TILE, (wy1 - 1) // TILE + 1):
                for stx in range(wx0 // TILE, (wx1 - 1) // TILE + 1):
                    tile = self._load(z, stx, sty)
                    if tile is None:
                        continue
                    ax0, ay0 = max(wx0, stx * TILE), max(wy0, sty * TILE)
                    ax1, ay1 = min(wx1, (stx + 1) * TILE), min(wy1, (sty + 1) * TILE)
                    window[ay0 - wy0:ay1 - wy0, ax0 - wx0:ax1 - wx0] = \
                        tile[ay0 - sty * TILE:ay1 - sty * TILE, ax0 - stx * TILE:ax1 - stx * TILE]
            if not np.isnan(window).all():
                filled = fill_holes(window, FILL_MAX_PX)
                filled = np.where(np.isnan(filled), np.float32(self.meta["base"]), filled).astype(np.float32)
                radius = int(min(MAX_BLUR_RADIUS, max(0, round(SMOOTH_METERS / meters_per_px(self.meta, z)))))
                smooth = box_blur(filled, radius)
                ox0, ox1 = max(gx0, tx * TILE), min(gx1, (tx + 1) * TILE)
                oy0, oy1 = max(gy0, ty * TILE), min(gy1, (ty + 1) * TILE)
                sl = (slice(oy0 - wy0, oy1 - wy0), slice(ox0 - wx0, ox1 - wx0))
                field = (ox0, oy0, np.ascontiguousarray(filled[sl]), np.ascontiguousarray(smooth[sl]))
        self._fields[key] = field
        if len(self._fields) > FIELD_CACHE:
            self._fields.popitem(last=False)
        return field

    def heights(self, z: int, x: int, y: int) -> np.ndarray | None:
        """Seamless heights, or None when the tile is exactly the flat base."""
        meta = self.meta
        rect = coverage_rect(meta, z)
        gx0, gy0, gx1, gy1 = rect
        mpp = meters_per_px(meta, z)
        dx = max(0, gx0 - (x * TILE + TILE - 1), x * TILE - (gx1 - 1))
        dy = max(0, gy0 - (y * TILE + TILE - 1), y * TILE - (gy1 - 1))
        if math.hypot(dx, dy) * mpp >= FADE_METERS:
            return None
        ctx = min(max(x, gx0 // TILE), (gx1 - 1) // TILE)
        cty = min(max(y, gy0 // TILE), (gy1 - 1) // TILE)
        field = self._field(z, ctx, cty, rect)
        if field is None:
            return None
        fx0, fy0, fh, fs = field
        gx = x * TILE + np.arange(TILE)
        gy = y * TILE + np.arange(TILE)
        cx = np.clip(gx, gx0, gx1 - 1)
        cy = np.clip(gy, gy0, gy1 - 1)
        iy = (cy - fy0)[:, None]
        ix = (cx - fx0)[None, :]
        values = fh[iy, ix].astype(np.float64)
        dist_px = np.hypot((gx - cx)[None, :], (gy - cy)[:, None])
        if not dist_px.any():
            return values.astype(np.float32)
        dist_m = dist_px * mpp
        smooth = fs[iy, ix].astype(np.float64)
        w = np.clip(dist_m / BLEND_METERS, 0.0, 1.0)
        edge = values * (1.0 - w) + smooth * w
        t = np.clip(dist_m / FADE_METERS, 0.0, 1.0)
        ease = t * t * (3.0 - 2.0 * t)
        return (edge + (meta["base"] - edge) * ease).astype(np.float32)


# ---- file output -------------------------------------------------------------
def write_file(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def link_or_copy(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.chmod(src, 0o644)  # hard links share the inode: make it world-readable
    except OSError:
        pass
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o644)


# ---- worker ------------------------------------------------------------------
_W: dict = {}


def _init(root, out, meta, prev=None):
    _W.update(root=root, out=out, meta=meta, seam=Seamless(root, meta), prev=prev, reused=0)


def emit(dst: str, data: bytes) -> None:
    """Write a published file - or hard-link the previous version's file when
    it is byte-identical (republishing an unchanged area then costs almost no
    disk: only tiles that really changed take new space)."""
    prev = _W.get("prev")
    if prev:
        old = os.path.join(prev, os.path.relpath(dst, _W["out"]))
        try:
            if os.path.getsize(old) == len(data):
                with open(old, "rb") as f:
                    same = f.read() == data
                if same:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    os.link(old, dst)
                    _W["reused"] += 1
                    return
        except OSError:
            pass
    write_file(dst, data)


def _terrain_job(task):
    """task = (z, y, x0, x1): one row segment (keeps field-cache locality)."""
    z, y, x0, x1 = task
    root, out, meta, seam = _W["root"], _W["out"], _W["meta"], _W["seam"]
    gx0, gy0, gx1, gy1 = coverage_rect(meta, z)
    counts = {"linked": 0, "computed": 0, "flat": 0}
    _W["reused"] = 0
    for x in range(x0, x1):
        dst = os.path.join(out, "terrain", str(z), str(x), f"{y}.webp")
        inside = (x * TILE >= gx0 and (x + 1) * TILE <= gx1 and y * TILE >= gy0 and (y + 1) * TILE <= gy1)
        if inside:
            path, ext = raw_tile_path(root, z, x, y)
            rgba = load_rgba(path) if path else None
            if rgba is not None and (rgba[..., 3] == 255).all():
                # Fully valid interior tile: the seamless field equals the raw
                # heights, so skip the 3x3 field. WebP -> hard link; legacy
                # PNG -> lossless re-encode of the same RGB (same bits).
                if ext == "webp":
                    link_or_copy(path, dst)
                    counts["linked"] += 1
                else:
                    emit(dst, webp_bytes(Image.fromarray(np.ascontiguousarray(rgba[..., :3]), "RGB")))
                    counts["computed"] += 1
                continue
        h = seam.heights(z, x, y)
        if h is None:
            counts["flat"] += 1  # nginx serves flat.webp
            continue
        emit(dst, webp_bytes(Image.fromarray(terrarium_rgb(h), "RGB")))
        counts["computed"] += 1
    counts["reused"] = _W["reused"]
    return counts


def zoom_tasks(meta: dict, z: int, pad_px: float) -> list:
    gx0, gy0, gx1, gy1 = coverage_rect(meta, z)
    n = 2 ** z
    tx0 = max(0, int((gx0 - pad_px) // TILE))
    tx1 = min(n - 1, int((gx1 - 1 + pad_px) // TILE))
    ty0 = max(0, int((gy0 - pad_px) // TILE))
    ty1 = min(n - 1, int((gy1 - 1 + pad_px) // TILE))
    step = 64
    return [(z, y, x, min(x + step, tx1 + 1)) for y in range(ty0, ty1 + 1) for x in range(tx0, tx1 + 1, step)]


def _default_workers() -> int:
    try:
        cpus = len(os.sched_getaffinity(0))
    except AttributeError:
        cpus = os.cpu_count() or 2
    return max(1, min(8, cpus))


class NoRawPyramid(ValueError):
    """The raw Terrarium pyramid is absent (already published = moved into
    published/, or never built): there is nothing to (re)publish."""


def has_raw_pyramid(root: str) -> bool:
    """True when <root>/terrarium holds at least one raw tile."""
    raw = os.path.join(root, "terrarium")
    try:
        zooms = [e.path for e in os.scandir(raw) if e.is_dir() and e.name.isdigit()]
    except OSError:
        return False
    for zdir in zooms:
        for _dirpath, _dirs, files in os.walk(zdir):
            if any(f.endswith((".webp", ".png")) for f in files):
                return True
    return False


def _rmtree(path: str, log) -> None:
    def report(_func, p, exc):
        exc = exc[1] if isinstance(exc, tuple) else exc
        log(f"  WARNING: could not delete {p}: {exc}", flush=True)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=report)
    else:
        shutil.rmtree(path, onerror=report)


def delete_raw(root: str, log=print) -> None:
    """Drop the raw pyramid once its tiles live on in the published version:
    the zoom dirs, the build resume manifest (it must not claim tiles that
    are gone, so the next build is a clean full build) and leftover *.part
    files - plus any legacy hillshade/ pyramid. tilejson.json stays."""
    raw = os.path.join(root, "terrarium")
    try:
        entries = list(os.scandir(raw))
    except OSError:
        entries = []
    for e in entries:
        if e.is_dir(follow_symlinks=False) and e.name.isdigit():
            _rmtree(e.path, log)
        elif e.name == "build_manifest.json" or e.name.endswith(".part"):
            try:
                os.unlink(e.path)
            except OSError as exc:
                log(f"  WARNING: could not delete {e.path}: {exc}", flush=True)
    legacy_hs = os.path.join(root, "hillshade")
    if os.path.isdir(legacy_hs):
        _rmtree(legacy_hs, log)
        log("removed legacy hillshade/ pyramid (no longer built or served)", flush=True)
    log(f"removed raw Terrarium pyramid {raw} (the published version is the only copy)",
        flush=True)


def publish(root: str, workers: int | None = None, keep: int = 1, log=print,
            keep_raw: bool = False) -> str:
    meta = read_meta(root)
    if not has_raw_pyramid(root):
        raise NoRawPyramid(
            f"no raw Terrarium pyramid under {os.path.join(root, 'terrarium')}: it was "
            f"already published (moved into published/) or never built. Run a build "
            f"first. Nothing was changed; the current published version keeps serving.")
    pub = os.path.join(root, "published")
    os.makedirs(pub, exist_ok=True)
    with open(os.path.join(root, "tilejson.json"), "rb") as f:
        digest = hashlib.sha1(f.read() + f"rev{PUBLISH_REVISION}".encode()).hexdigest()[:8]
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + digest
    staging = os.path.join(pub, f".staging-{version}")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    os.chmod(staging, 0o755)
    t0 = time.monotonic()
    try:
        totals = _publish_into(root, pub, staging, meta, workers, log)
        info = {"version": version, "created": datetime.now(timezone.utc).isoformat(),
                "bounds": list(meta["bounds"]), "minzoom": 0, "maxzoom": meta["maxzoom"],
                "base": meta["base"], "statistics": meta["statistics"], "terrainFormat": "webp",
                "encoding": "terrarium", "revision": PUBLISH_REVISION, "counts": totals}
        write_file(os.path.join(staging, "meta.json"), json.dumps(info, indent=2).encode())
        final = os.path.join(pub, version)
        os.replace(staging, final)
    except BaseException:
        # failed publish: drop only our own partial output, delete nothing else
        shutil.rmtree(staging, ignore_errors=True)
        raise
    write_file(os.path.join(pub, "current.json"), json.dumps(info, indent=2).encode())
    log(f"published {version}: {totals} in {time.monotonic() - t0:.0f}s -> {final}", flush=True)

    # ---- only after the switch: retention + single copy of the elevation ----
    keep = max(1, keep)
    versions = sorted(d for d in os.listdir(pub)
                      if os.path.isdir(os.path.join(pub, d)) and not d.startswith("."))
    for old in versions[:-keep]:
        if old != version:
            _rmtree(os.path.join(pub, old), log)
            log(f"removed old published version {old}", flush=True)
    for d in os.listdir(pub):  # interrupted earlier publishes
        if d.startswith(".staging-"):
            _rmtree(os.path.join(pub, d), log)
    if keep_raw:
        log("--keep-raw: raw Terrarium pyramid kept", flush=True)
    else:
        delete_raw(root, log)
    return version


def _publish_into(root: str, pub: str, staging: str, meta: dict, workers, log) -> dict:
    flat = np.full((TILE, TILE), meta["base"], dtype=np.float32)
    write_file(os.path.join(staging, "flat.webp"), webp_bytes(Image.fromarray(terrarium_rgb(flat), "RGB")))

    workers = workers or _default_workers()
    t0 = time.monotonic()
    totals = {"linked": 0, "computed": 0, "flat": 0, "reused": 0}
    prev = None
    try:
        with open(os.path.join(pub, "current.json"), encoding="utf-8") as f:
            cand = os.path.join(pub, str(json.load(f).get("version", "")))
        if os.path.isfile(os.path.join(cand, "meta.json")):
            prev = cand
            log(f"  dedupe against previous version {os.path.basename(prev)}", flush=True)
    except (OSError, ValueError):
        pass
    ctx = mp.get_context("fork") if hasattr(os, "fork") else mp.get_context()
    with ctx.Pool(workers, initializer=_init, initargs=(root, staging, meta, prev), maxtasksperchild=2000) as pool:
        for z in range(0, meta["maxzoom"] + 1):
            pad = FADE_METERS / meters_per_px(meta, z)
            tasks = zoom_tasks(meta, z, pad)
            done = 0
            for c in pool.imap_unordered(_terrain_job, tasks, chunksize=4):
                for k, v in c.items():
                    totals[k] += v
                done += 1
                if done % max(1, PROGRESS_EVERY // 64) == 0:
                    log(f"  publish z{z} terrain {done}/{len(tasks)} row-segments "
                        f"({time.monotonic() - t0:.0f}s)", flush=True)
            log(f"  publish z{z} done: linked={totals['linked']} computed={totals['computed']} "
                f"flat={totals['flat']} ({time.monotonic() - t0:.0f}s)", flush=True)
    return totals


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="tile root (data/tiles)")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--keep", type=int, default=1,
                    help="published versions to keep (>=1; default 1 = only the live one)")
    ap.add_argument("--keep-raw", action="store_true",
                    help="do not delete the raw terrarium/ pyramid after publishing (dev/tests)")
    args = ap.parse_args(argv)
    try:
        publish(args.root, args.workers, max(1, args.keep), keep_raw=args.keep_raw)
    except NoRawPyramid as e:
        print(f"publish REFUSED: {e}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print(f"publish FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
