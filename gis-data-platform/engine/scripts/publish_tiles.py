"""Publish the current build as a STATIC, seamless tile set that nginx serves
directly (no Python per request).

Input  (<root> = data/tiles):
  tilejson.json                      bounds / maxzoom / statistics of the build
  terrarium/{z}/{x}/{y}.webp|.png    raw Terrarium pyramid (.webp preferred)
  hillshade/{z}/{x}/{y}.png          hillshade (LA = baked alpha, or legacy L)

Output (<root>/published/<version>/, swapped in atomically):
  terrain/{z}/{x}/{y}.webp   seamless Terrarium (lossless WebP): real heights
                             inside coverage, edge heights eased to a flat
                             base outside (no cliffs) - the exact algorithm
                             of app/terrain.py TerrainSource, baked once
  hillshade/{z}/{x}/{y}.png  hillshade clipped to coverage with real alpha
  flat.webp                  base-height tile: nginx's fallback for every
                             terrain tile not written (beyond the fade)
  transparent.png            nginx's fallback for missing hillshade tiles
  meta.json                  version, bounds, maxzoom, base
and <root>/published/current.json -> the live version (written last).

Interior tiles whose raw tile is already WebP with no transparent pixels are
hard-linked (zero copy); everything else is computed. Older versions are
deleted (keeping --keep, default 2, so open clients finish their session).

usage: publish_tiles.py --root data/tiles [--workers N] [--keep 2]
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
PUBLISH_REVISION = 1          # bump when published bytes change for the same build
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
    return {"bounds": (west, south, east, north), "maxzoom": maxzoom, "base": base,
            "minzoom": int(data.get("minzoom", 0) or 0), "statistics": {"min": base, "max": top}}


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


def coverage_alpha(meta: dict, z: int, x: int, y: int, inset_px: float = 3.0) -> np.ndarray:
    west, south, east, north = meta["bounds"]
    fx0, fy0 = global_px(west, north, z)
    fx1, fy1 = global_px(east, south, z)
    gx = x * TILE + np.arange(TILE) + 0.5
    gy = y * TILE + np.arange(TILE) + 0.5
    ix = (gx >= fx0 + inset_px) & (gx <= fx1 - inset_px)
    iy = (gy >= fy0 + inset_px) & (gy <= fy1 - inset_px)
    return np.where(iy[:, None] & ix[None, :], 255, 0).astype(np.uint8)


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


def _init(root, out, meta):
    _W.update(root=root, out=out, meta=meta, seam=Seamless(root, meta))


def _terrain_job(task):
    """task = (z, y, x0, x1): one row segment (keeps field-cache locality)."""
    z, y, x0, x1 = task
    root, out, meta, seam = _W["root"], _W["out"], _W["meta"], _W["seam"]
    gx0, gy0, gx1, gy1 = coverage_rect(meta, z)
    counts = {"linked": 0, "computed": 0, "flat": 0}
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
                    write_file(dst, webp_bytes(Image.fromarray(np.ascontiguousarray(rgba[..., :3]), "RGB")))
                    counts["computed"] += 1
                continue
        h = seam.heights(z, x, y)
        if h is None:
            counts["flat"] += 1  # nginx serves flat.webp
            continue
        write_file(dst, webp_bytes(Image.fromarray(terrarium_rgb(h), "RGB")))
        counts["computed"] += 1
    return counts


def _hillshade_job(task):
    z, y, x0, x1 = task
    root, out, meta = _W["root"], _W["out"], _W["meta"]
    n = 0
    for x in range(x0, x1):
        src = os.path.join(root, "hillshade", str(z), str(x), f"{y}.png")
        if not os.path.exists(src):
            continue
        dst = os.path.join(out, "hillshade", str(z), str(x), f"{y}.png")
        alpha = coverage_alpha(meta, z, x, y)
        try:
            with Image.open(src) as im:
                mode = im.mode
                if mode == "LA" and alpha.all():
                    link_or_copy(src, dst)
                    n += 1
                    continue
                la = np.asarray(im.convert("LA")).copy()
        except (OSError, ValueError, SyntaxError):
            continue
        if mode != "LA":
            la[..., 1] = 255
        la[..., 1] = np.minimum(la[..., 1], alpha)
        if not la[..., 1].any():
            continue
        buf = io.BytesIO()
        Image.fromarray(la, "LA").save(buf, format="PNG", compress_level=1)
        write_file(dst, buf.getvalue())
        n += 1
    return {"hillshade": n}


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


def publish(root: str, workers: int | None = None, keep: int = 2, log=print) -> str:
    meta = read_meta(root)
    pub = os.path.join(root, "published")
    os.makedirs(pub, exist_ok=True)
    with open(os.path.join(root, "tilejson.json"), "rb") as f:
        digest = hashlib.sha1(f.read() + f"rev{PUBLISH_REVISION}".encode()).hexdigest()[:8]
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + digest
    staging = os.path.join(pub, f".staging-{version}")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    os.chmod(staging, 0o755)

    flat = np.full((TILE, TILE), meta["base"], dtype=np.float32)
    write_file(os.path.join(staging, "flat.webp"), webp_bytes(Image.fromarray(terrarium_rgb(flat), "RGB")))
    buf = io.BytesIO()
    Image.new("LA", (TILE, TILE), (0, 0)).save(buf, format="PNG")
    write_file(os.path.join(staging, "transparent.png"), buf.getvalue())

    workers = workers or _default_workers()
    t0 = time.monotonic()
    totals = {"linked": 0, "computed": 0, "flat": 0, "hillshade": 0}
    ctx = mp.get_context("fork") if hasattr(os, "fork") else mp.get_context()
    with ctx.Pool(workers, initializer=_init, initargs=(root, staging, meta), maxtasksperchild=2000) as pool:
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
            for c in pool.imap_unordered(_hillshade_job, zoom_tasks(meta, z, 0), chunksize=4):
                totals["hillshade"] += c["hillshade"]
            log(f"  publish z{z} done: linked={totals['linked']} computed={totals['computed']} "
                f"flat={totals['flat']} hillshade={totals['hillshade']} "
                f"({time.monotonic() - t0:.0f}s)", flush=True)

    info = {"version": version, "created": datetime.now(timezone.utc).isoformat(),
            "bounds": list(meta["bounds"]), "minzoom": 0, "maxzoom": meta["maxzoom"],
            "base": meta["base"], "statistics": meta["statistics"], "terrainFormat": "webp",
            "encoding": "terrarium", "revision": PUBLISH_REVISION, "counts": totals}
    write_file(os.path.join(staging, "meta.json"), json.dumps(info, indent=2).encode())
    final = os.path.join(pub, version)
    os.replace(staging, final)
    write_file(os.path.join(pub, "current.json"), json.dumps(info, indent=2).encode())
    log(f"published {version}: {totals} in {time.monotonic() - t0:.0f}s -> {final}", flush=True)

    # retention: newest `keep` versions (by name = timestamp) incl. current
    versions = sorted(d for d in os.listdir(pub)
                      if os.path.isdir(os.path.join(pub, d)) and not d.startswith("."))
    for old in versions[:-keep] if keep > 0 else []:
        if old != version:
            shutil.rmtree(os.path.join(pub, old), ignore_errors=True)
            log(f"removed old published version {old}", flush=True)
    for d in os.listdir(pub):  # interrupted earlier publishes
        if d.startswith(".staging-") and d != f".staging-{version}":
            shutil.rmtree(os.path.join(pub, d), ignore_errors=True)
    return version


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="tile root (data/tiles)")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--keep", type=int, default=2, help="published versions to keep (>=1)")
    args = ap.parse_args(argv)
    try:
        publish(args.root, args.workers, max(1, args.keep))
    except (OSError, ValueError) as e:
        print(f"publish FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
