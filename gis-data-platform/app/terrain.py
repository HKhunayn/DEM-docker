"""Seamless Terrarium terrain delivery for the legacy heightmap pyramid.

The legacy build writes Terrarium PNGs only around the last-built area, with
transparent (no-data) pixels at its edges. Rendered raw, a 3-D client shows
that as cliffs: no-data decodes to -32768 m, a missing tile to 0 m, and a
constant fill leaves a plateau with vertical walls at the coverage edge.

This module serves every tile (z <= maxzoom) from the current build's
coverage rectangle:

* inside the coverage rectangle: the original heights (interior holes filled
  from neighbours);
* outside it: the nearest edge height, eased with a smoothstep towards a flat
  base (dataset minimum) over ``FADE_METERS`` — no walls, no seams between
  tiles, because every pixel is a function of its global position only.

Tiles from older builds outside the current bounds are ignored ("last build
wins"), so stale slivers from earlier areas no longer appear.

Memory is bounded per request: the height field is never mosaicked for the
whole bbox (Saudi Arabia at z13 would be ~116 GB). Each request decodes only
the coverage tile its pixels clamp to, plus a one-tile apron (3x3 tiles) for
hole filling and the edge blur (radius <= 64 px < 256 px apron). Tiles
outside coverage reuse the cached local field of the coverage tile they clamp
to (an edge segment), and tiles beyond the fade distance are flat base without
touching disk. Local fields and encoded tiles live in bounded LRU caches.

This dynamic path is only the fallback while nothing is published: a
successful publish moves the raw terrarium/ tiles into
tiles/published/<version>/ (single copy on disk) and /tiles/terrain.json then
advertises the published set. With the raw pyramid absent every tile here is
simply the flat base (no errors).
"""
from __future__ import annotations

import io
import json
import math
import os
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np
from PIL import Image

TILE = 256
FADE_METERS = 4000.0
SMOOTH_METERS = 1200.0  # box-blur radius of the edge profile that is extended
BLEND_METERS = 600.0    # raw edge -> smoothed edge transition distance
EARTH_CIRCUMFERENCE = 40075016.686
# Bump when the served tile content changes for the same build (part of the
# URL version token, so clients refetch).
ALGORITHM_REVISION = 5
DEFAULT_MAXZOOM = 13
MAX_LAT = 85.05112878
# Hard cap on pixels allocated for one local field window (3x3 tiles is
# 0.59 Mpx; this guards against any future change blowing up again).
MAX_WINDOW_PX = 64 * 1024 * 1024
MAX_BLUR_RADIUS = 64  # must stay below TILE so the 1-tile apron covers it
# Holes are filled from data at most this far away (row, then column), so a
# pixel's value depends only on data within the 1-tile apron (+ blur radius).
# Larger holes fall back to the flat base.
FILL_MAX_PX = TILE - MAX_BLUR_RADIUS


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


TILE_CACHE_BYTES = _env_int("TERRAIN_TILE_CACHE_MB", 128) * 1024 * 1024
FIELD_CACHE_ENTRIES = _env_int("TERRAIN_FIELD_CACHE_ENTRIES", 64)


@dataclass
class _Meta:
    key: tuple
    bounds: tuple[float, float, float, float] | None
    maxzoom: int
    base: float


@dataclass
class _Field:
    """Local height field for (the coverage part of) one tile."""
    gx0: int
    gy0: int
    heights: np.ndarray  # float32, filled, shape (h, w) <= (TILE, TILE)
    smooth: np.ndarray   # box-blurred heights, used for the outside extension


def _number(value: Any) -> float | None:
    """Finite float or None (json.loads accepts Infinity/NaN; bools aren't numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_maxzoom(value: Any) -> int:
    number = _number(value)
    if number is None or number != int(number) or not 0 <= number <= 24:
        return DEFAULT_MAXZOOM
    return int(number)


def _parse_bounds(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    numbers = [_number(v) for v in value]
    if any(n is None for n in numbers):
        return None
    west, south, east, north = numbers  # type: ignore[misc]
    west, east = (max(-180.0, min(180.0, v)) for v in (west, east))
    south, north = (max(-MAX_LAT, min(MAX_LAT, v)) for v in (south, north))
    if not (west < east and south < north):
        return None
    return (west, south, east, north)


def parse_meta(data: Any, key: tuple = ("parsed",)) -> _Meta:
    """Build metadata from a (possibly hostile/degenerate) tilejson dict."""
    if not isinstance(data, dict):
        data = {}
    stats = data.get("statistics")
    if not isinstance(stats, dict):
        stats = {}
    stat_min = _number(stats.get("min"))
    # A failed or degenerate build writes null/inf statistics (json.loads
    # turns Infinity into float inf). Don't trust its maxzoom: fall back to
    # the default so terrain still serves from the previous good pyramid.
    # A missing "max" alone is not degenerate (only "min" is used here).
    degenerate = stat_min is None or ("max" in stats and _number(stats.get("max")) is None)
    if not degenerate and _number(stats.get("max")) is not None and stat_min > _number(stats["max"]):  # type: ignore[operator]
        degenerate = True
    return _Meta(
        key=key,
        bounds=_parse_bounds(data.get("bounds")),
        maxzoom=DEFAULT_MAXZOOM if degenerate else _parse_maxzoom(data.get("maxzoom", DEFAULT_MAXZOOM)),
        base=base_height(stat_min, _number(stats.get("max"))),
    )


def base_height(stat_min: float | None, stat_max: float | None) -> float:
    """Height used for no-data inside coverage (open sea: Copernicus has no
    ocean tiles) and as the flat level the edges fade to: sea level when the
    dataset spans it, else the dataset minimum (an inland area keeps a plateau
    instead of a cliff down to 0 m). Mirrors engine publish_tiles.base_height."""
    if stat_min is None:
        return 0.0
    if stat_max is not None and stat_min <= 0.0 <= stat_max:
        return 0.0
    return stat_min


class _ByteLRU:
    """Thread-safe LRU of bytes payloads bounded by total size."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.bytes = 0
        self._data: OrderedDict[tuple, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> bytes | None:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key: tuple, value: bytes) -> None:
        if len(value) > self.max_bytes:
            return
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self.bytes -= len(old)
            self._data[key] = value
            self.bytes += len(value)
            while self.bytes > self.max_bytes and self._data:
                _, evicted = self._data.popitem(last=False)
                self.bytes -= len(evicted)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.bytes = 0

    def __len__(self) -> int:
        return len(self._data)


class _KeyLocks:
    """Per-key locks (refcounted, so the dict doesn't grow without bound).
    Lets concurrent requests for the same tile compute it once."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[tuple, list] = {}

    @contextmanager
    def hold(self, key: tuple) -> Iterator[None]:
        with self._guard:
            entry = self._locks.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    self._locks.pop(key, None)


def _box_blur(a: np.ndarray, radius: int) -> np.ndarray:
    """Separable box blur with edge clamping (cumulative sums, O(n))."""
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


def _global_px(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = TILE * (2 ** z)
    lat = max(min(lat, 85.05112878), -85.05112878)
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


def encode_terrarium(heights: np.ndarray) -> bytes:
    v = np.clip(heights.astype(np.float64) + 32768.0, 0.0, 65535.996)
    r = np.floor(v / 256.0)
    g = np.floor(v - r * 256.0)
    b = np.floor((v - r * 256.0 - g) * 256.0)
    rgb = np.stack([r, g, b], axis=-1).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def _fill_holes(a: np.ndarray, max_dist: int | None = None) -> np.ndarray:
    """Fill NaNs from the nearest valid value along rows, then columns.

    With `max_dist`, only valid pixels at most that far away (in the row,
    then in the column) are used and farther holes stay NaN. That makes the
    result for a pixel depend only on its +-2*max_dist neighbourhood, never on
    where the processing window happens to end - so per-tile windows give
    the same values on both sides of a tile edge (no seams).
    """
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


class TerrainSource:
    def __init__(self, public_root: Path) -> None:
        self.public = public_root
        self._meta_lock = threading.Lock()
        self._meta: _Meta | None = None
        self._tiles = _ByteLRU(TILE_CACHE_BYTES)
        self._fields: OrderedDict[tuple, _Field | None] = OrderedDict()
        self._fields_lock = threading.Lock()
        self._keylocks = _KeyLocks()

    # -- metadata -----------------------------------------------------------
    def version(self) -> str:
        """Cache-busting token: build metadata identity + algorithm revision."""
        import hashlib
        raw = f"{ALGORITHM_REVISION}:{self.meta().key}".encode()
        return hashlib.sha1(raw).hexdigest()[:10]

    def meta(self) -> _Meta:
        path = self.public / "tilejson.json"
        try:
            stat = path.stat()
            key: tuple = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            key = ("missing",)
        current = self._meta
        if current is not None and current.key == key:
            return current
        with self._meta_lock:
            if self._meta is not None and self._meta.key == key:
                return self._meta
            data: Any = {}
            if key != ("missing",):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("tilejson is not an object")
                except (OSError, ValueError):
                    # Partially written (the engine rewrites it in place) or
                    # unreadable: keep serving the last good metadata and do
                    # NOT cache this state under the new mtime key, so the
                    # next request re-reads the finished file.
                    if self._meta is not None:
                        return self._meta
                    return parse_meta({}, key=("unreadable",))
            meta = parse_meta(data, key=key)
            self._meta = meta
            self._tiles.clear()
            with self._fields_lock:
                self._fields.clear()
            return meta

    # -- geometry -----------------------------------------------------------
    @staticmethod
    def _rect(meta: _Meta, z: int) -> tuple[int, int, int, int] | None:
        """Coverage rectangle [gx0, gx1) x [gy0, gy1) in global pixels at z,
        shrunk half a pixel so resampled edge pixels (often no-data) are
        excluded."""
        if not meta.bounds:
            return None
        west, south, east, north = meta.bounds
        fx0, fy0 = _global_px(west, north, z)
        fx1, fy1 = _global_px(east, south, z)
        gx0, gy0 = int(math.floor(fx0 + 0.5)), int(math.floor(fy0 + 0.5))
        gx1, gy1 = max(gx0 + 1, int(math.ceil(fx1 - 0.5))), max(gy0 + 1, int(math.ceil(fy1 - 0.5)))
        return gx0, gy0, gx1, gy1

    @staticmethod
    def _meters_per_px(meta: _Meta, z: int) -> float:
        lat = (meta.bounds[1] + meta.bounds[3]) / 2.0 if meta.bounds else 0.0
        return EARTH_CIRCUMFERENCE * math.cos(math.radians(lat)) / (TILE * 2 ** z)

    def _load_tile(self, z: int, tx: int, ty: int) -> np.ndarray | None:
        # Lossless WebP since engine v3 (.png for older builds).
        base = self.public / "terrarium" / str(z) / str(tx)
        path = base / f"{ty}.webp"
        if not path.is_file():
            path = base / f"{ty}.png"
        try:
            with Image.open(path) as image:
                rgba = np.asarray(image.convert("RGBA"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, SyntaxError):
            return None  # truncated / being rewritten by a running build
        if rgba.shape[:2] != (TILE, TILE):
            return None
        return decode_terrarium(rgba)

    # -- local height field (one tile + 1-tile apron) ------------------------
    def _local_field(self, meta: _Meta, z: int, tx: int, ty: int,
                     rect: tuple[int, int, int, int]) -> _Field | None:
        """Filled + blurred heights for the coverage part of tile (tx, ty).
        Bounded memory: decodes at most the 3x3 tiles around it."""
        key = (meta.key, z, tx, ty)
        with self._fields_lock:
            if key in self._fields:
                self._fields.move_to_end(key)
                return self._fields[key]
        with self._keylocks.hold(("field",) + key):
            with self._fields_lock:
                if key in self._fields:
                    return self._fields[key]
            field = self._compute_field(meta, z, tx, ty, rect)
            with self._fields_lock:
                self._fields[key] = field
                while len(self._fields) > FIELD_CACHE_ENTRIES:
                    self._fields.popitem(last=False)
            return field

    def _compute_field(self, meta: _Meta, z: int, tx: int, ty: int,
                       rect: tuple[int, int, int, int]) -> _Field | None:
        gx0, gy0, gx1, gy1 = rect
        # Window: coverage ∩ 3x3 tiles around (tx, ty).
        wx0, wx1 = max(gx0, (tx - 1) * TILE), min(gx1, (tx + 2) * TILE)
        wy0, wy1 = max(gy0, (ty - 1) * TILE), min(gy1, (ty + 2) * TILE)
        if wx1 <= wx0 or wy1 <= wy0:
            return None
        if (wx1 - wx0) * (wy1 - wy0) > MAX_WINDOW_PX:
            raise MemoryError("terrain window exceeds MAX_WINDOW_PX")
        window = np.full((wy1 - wy0, wx1 - wx0), np.nan, dtype=np.float32)
        for sty in range(wy0 // TILE, (wy1 - 1) // TILE + 1):
            for stx in range(wx0 // TILE, (wx1 - 1) // TILE + 1):
                tile = self._load_tile(z, stx, sty)
                if tile is None:
                    continue
                ax0, ay0 = max(wx0, stx * TILE), max(wy0, sty * TILE)
                ax1, ay1 = min(wx1, (stx + 1) * TILE), min(wy1, (sty + 1) * TILE)
                window[ay0 - wy0:ay1 - wy0, ax0 - wx0:ax1 - wx0] = \
                    tile[ay0 - sty * TILE:ay1 - sty * TILE, ax0 - stx * TILE:ax1 - stx * TILE]
        if np.isnan(window).all():
            return None  # no data near here: callers use the flat base
        filled = _fill_holes(window, FILL_MAX_PX)
        filled = np.where(np.isnan(filled), np.float32(meta.base), filled).astype(np.float32)
        radius = int(min(MAX_BLUR_RADIUS, max(0, round(SMOOTH_METERS / self._meters_per_px(meta, z)))))
        smooth = _box_blur(filled, radius)
        # Keep only the coverage part of the centre tile.
        ox0, ox1 = max(gx0, tx * TILE), min(gx1, (tx + 1) * TILE)
        oy0, oy1 = max(gy0, ty * TILE), min(gy1, (ty + 1) * TILE)
        sl = (slice(oy0 - wy0, oy1 - wy0), slice(ox0 - wx0, ox1 - wx0))
        return _Field(ox0, oy0, np.ascontiguousarray(filled[sl], dtype=np.float32),
                      np.ascontiguousarray(smooth[sl], dtype=np.float32))

    # -- tiles --------------------------------------------------------------
    def heights(self, z: int, x: int, y: int) -> np.ndarray:
        meta = self.meta()
        flat = np.full((TILE, TILE), meta.base, dtype=np.float32)
        rect = self._rect(meta, z)
        if rect is None:
            return flat
        gx0, gy0, gx1, gy1 = rect
        meters_per_px = self._meters_per_px(meta, z)
        # Distance (px) from this tile to the coverage rectangle: beyond the
        # fade every pixel is exactly the base - no need to touch any data.
        dx = max(0, gx0 - (x * TILE + TILE - 1), x * TILE - (gx1 - 1))
        dy = max(0, gy0 - (y * TILE + TILE - 1), y * TILE - (gy1 - 1))
        if math.hypot(dx, dy) * meters_per_px >= FADE_METERS:
            return flat
        # Every pixel clamps into the coverage part of one tile (an edge
        # segment for tiles outside coverage, the tile itself inside).
        ctx = min(max(x, gx0 // TILE), (gx1 - 1) // TILE)
        cty = min(max(y, gy0 // TILE), (gy1 - 1) // TILE)
        field = self._local_field(meta, z, ctx, cty, rect)
        if field is None:
            return flat
        gx = x * TILE + np.arange(TILE)
        gy = y * TILE + np.arange(TILE)
        cx = np.clip(gx, gx0, gx1 - 1)
        cy = np.clip(gy, gy0, gy1 - 1)
        iy = (cy - field.gy0)[:, None]
        ix = (cx - field.gx0)[None, :]
        values = field.heights[iy, ix].astype(np.float64)
        dist_px = np.hypot((gx - cx)[None, :], (gy - cy)[:, None])
        if not dist_px.any():
            return values.astype(np.float32)
        dist_m = dist_px * meters_per_px
        # Clamp-to-edge copies edge pixels outward as streaks; blend from the
        # raw edge to a smoothed edge profile within BLEND_METERS, then ease
        # the result to the flat base over FADE_METERS.
        smooth = field.smooth[iy, ix].astype(np.float64)
        w = np.clip(dist_m / BLEND_METERS, 0.0, 1.0)
        edge = values * (1.0 - w) + smooth * w
        t = np.clip(dist_m / FADE_METERS, 0.0, 1.0)
        ease = t * t * (3.0 - 2.0 * t)
        return (edge + (meta.base - edge) * ease).astype(np.float32)

    def _cached(self, key: tuple, compute: Callable[[], bytes]) -> bytes:
        cached = self._tiles.get(key)
        if cached is not None:
            return cached
        with self._keylocks.hold(("tile",) + key):
            cached = self._tiles.get(key)
            if cached is not None:
                return cached
            payload = compute()
            self._tiles.put(key, payload)
            return payload

    def tile_png(self, z: int, x: int, y: int) -> bytes | None:
        """Encoded Terrarium tile, or None above the pyramid's maxzoom or
        outside the world (clients then over-zoom their parent tile)."""
        meta = self.meta()
        if z < 0 or z > meta.maxzoom or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
            return None
        return self._cached((meta.key, z, x, y), lambda: encode_terrarium(self.heights(z, x, y)))
