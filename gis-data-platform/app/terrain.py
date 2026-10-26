"""Seamless Terrarium terrain delivery for the legacy heightmap pyramid.

The legacy build writes Terrarium PNGs only around the last-built area, with
transparent (no-data) pixels at its edges. Rendered raw, a 3-D client shows
that as cliffs: no-data decodes to -32768 m, a missing tile to 0 m, and a
constant fill leaves a plateau with vertical walls at the coverage edge.

This module serves every tile (z <= maxzoom) from a per-zoom mosaic of the
current build's coverage:

* inside the coverage rectangle: the original heights (interior holes filled
  from neighbours);
* outside it: the nearest edge height, eased with a smoothstep towards a flat
  base (dataset minimum) over ``FADE_METERS`` — no walls, no seams between
  tiles, because every pixel is a function of its global position only.

Tiles from older builds outside the current bounds are ignored ("last build
wins"), so stale slivers from earlier areas no longer appear.
"""
from __future__ import annotations

import io
import json
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

TILE = 256
FADE_METERS = 4000.0
SMOOTH_METERS = 1200.0  # box-blur radius of the edge profile that is extended
BLEND_METERS = 600.0    # raw edge -> smoothed edge transition distance
EARTH_CIRCUMFERENCE = 40075016.686
# Bump when the served tile content changes for the same build (part of the
# URL version token, so clients refetch).
ALGORITHM_REVISION = 3


@dataclass
class _Meta:
    key: tuple
    bounds: tuple[float, float, float, float] | None
    maxzoom: int
    base: float


@dataclass
class _Field:
    gx0: int
    gy0: int
    heights: np.ndarray  # float32, filled, shape (h, w)
    smooth: np.ndarray   # box-blurred heights, used for the outside extension


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


def _fill_holes(a: np.ndarray) -> np.ndarray:
    """Fill NaNs from the nearest valid value along rows, then columns."""
    if not np.isnan(a).any():
        return a

    def ffill(x: np.ndarray) -> np.ndarray:
        mask = np.isnan(x)
        idx = np.where(~mask, np.arange(x.shape[1])[None, :], 0)
        np.maximum.accumulate(idx, axis=1, out=idx)
        out = x[np.arange(x.shape[0])[:, None], idx]
        return out

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
        self._lock = threading.Lock()
        self._meta: _Meta | None = None
        self._fields: dict[int, _Field | None] = {}
        self._tiles: dict[tuple, bytes] = {}

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
            key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            key = ("missing",)
        with self._lock:
            if self._meta is not None and self._meta.key == key:
                return self._meta
            data: dict[str, Any] = {}
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            bounds = data.get("bounds")
            stats = data.get("statistics") or {}
            meta = _Meta(
                key=key,
                bounds=tuple(float(v) for v in bounds) if isinstance(bounds, list) and len(bounds) == 4 else None,
                maxzoom=int(data.get("maxzoom", 13)),
                base=float(stats.get("min", 0.0) or 0.0),
            )
            self._meta, self._fields, self._tiles = meta, {}, {}
            return meta

    # -- per-zoom height field ----------------------------------------------
    def _field(self, z: int, meta: _Meta) -> _Field | None:
        if z in self._fields:
            return self._fields[z]
        field = None
        if meta.bounds:
            west, south, east, north = meta.bounds
            fx0, fy0 = _global_px(west, north, z)
            fx1, fy1 = _global_px(east, south, z)
            # Coverage rectangle in global pixels, shrunk half a pixel so
            # resampled edge pixels (often no-data) are excluded.
            gx0, gy0 = int(math.floor(fx0 + 0.5)), int(math.floor(fy0 + 0.5))
            gx1, gy1 = max(gx0 + 1, int(math.ceil(fx1 - 0.5))), max(gy0 + 1, int(math.ceil(fy1 - 0.5)))
            heights = np.full((gy1 - gy0, gx1 - gx0), np.nan, dtype=np.float32)
            for ty in range(gy0 // TILE, (gy1 - 1) // TILE + 1):
                for tx in range(gx0 // TILE, (gx1 - 1) // TILE + 1):
                    path = self.public / "terrarium" / str(z) / str(tx) / f"{ty}.png"
                    if not path.is_file():
                        continue
                    tile = decode_terrarium(np.asarray(Image.open(path).convert("RGBA")))
                    # Intersection of this tile with the rectangle.
                    ax0, ay0 = max(gx0, tx * TILE), max(gy0, ty * TILE)
                    ax1, ay1 = min(gx1, (tx + 1) * TILE), min(gy1, (ty + 1) * TILE)
                    heights[ay0 - gy0:ay1 - gy0, ax0 - gx0:ax1 - gx0] = tile[ay0 - ty * TILE:ay1 - ty * TILE, ax0 - tx * TILE:ax1 - tx * TILE]
            if not np.isnan(heights).all():
                filled = _fill_holes(heights)
                lat = (south + north) / 2.0
                meters_per_px = EARTH_CIRCUMFERENCE * math.cos(math.radians(lat)) / (TILE * 2 ** z)
                radius = int(min(64, max(0, round(SMOOTH_METERS / meters_per_px))))
                field = _Field(gx0, gy0, filled, _box_blur(filled, radius))
        self._fields[z] = field
        return field

    # -- tiles --------------------------------------------------------------
    def heights(self, z: int, x: int, y: int) -> np.ndarray:
        meta = self.meta()
        with self._lock:
            field = self._field(z, meta)
        if field is None:
            return np.full((TILE, TILE), meta.base, dtype=np.float32)
        h, w = field.heights.shape
        gx = x * TILE + np.arange(TILE)
        gy = y * TILE + np.arange(TILE)
        cx = np.clip(gx, field.gx0, field.gx0 + w - 1)
        cy = np.clip(gy, field.gy0, field.gy0 + h - 1)
        iy = (cy - field.gy0)[:, None]
        ix = (cx - field.gx0)[None, :]
        values = field.heights[iy, ix].astype(np.float64)
        dist_px = np.hypot((gx - cx)[None, :], (gy - cy)[:, None])
        if not dist_px.any():
            return values.astype(np.float32)
        lat = 0.0
        if meta.bounds:
            lat = (meta.bounds[1] + meta.bounds[3]) / 2.0
        meters_per_px = EARTH_CIRCUMFERENCE * math.cos(math.radians(lat)) / (TILE * 2 ** z)
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

    def tile_png(self, z: int, x: int, y: int) -> bytes | None:
        """Encoded Terrarium tile, or None above the pyramid's maxzoom
        (clients then over-zoom their parent tile, which is correct)."""
        meta = self.meta()
        if z > meta.maxzoom:
            return None
        key = (meta.key, z, x, y)
        cached = self._tiles.get(key)
        if cached is not None:
            return cached
        payload = encode_terrarium(self.heights(z, x, y))
        with self._lock:
            if len(self._tiles) > 4096:
                self._tiles.clear()
            self._tiles[key] = payload
        return payload

    def coverage_mask(self, z: int, x: int, y: int, inset_px: float = 3.0) -> np.ndarray:
        """Boolean 256x256 mask of pixels inside the current coverage, inset
        by `inset_px` so resampling-edge pixels (black/no-data) are excluded."""
        meta = self.meta()
        if not meta.bounds:
            return np.zeros((TILE, TILE), dtype=bool)
        west, south, east, north = meta.bounds
        fx0, fy0 = _global_px(west, north, z)
        fx1, fy1 = _global_px(east, south, z)
        gx = x * TILE + np.arange(TILE) + 0.5
        gy = y * TILE + np.arange(TILE) + 0.5
        inside_x = (gx >= fx0 + inset_px) & (gx <= fx1 - inset_px)
        inside_y = (gy >= fy0 + inset_px) & (gy <= fy1 - inset_px)
        return inside_y[:, None] & inside_x[None, :]

    def hillshade_png(self, path: Path, z: int, x: int, y: int) -> bytes:
        """Hillshade tile clipped to coverage with real transparency (the
        legacy tiles are opaque greyscale: no-data is painted grey/black)."""
        stat = path.stat()
        key = ("hs", self.meta().key, z, x, y, stat.st_mtime_ns)
        cached = self._tiles.get(key)
        if cached is not None:
            return cached
        grey = np.asarray(Image.open(path).convert("L"))
        alpha = np.where(self.coverage_mask(z, x, y), 255, 0).astype(np.uint8)
        rgba = np.dstack([grey, grey, grey, alpha])
        buffer = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(buffer, format="PNG")
        payload = buffer.getvalue()
        with self._lock:
            if len(self._tiles) > 4096:
                self._tiles.clear()
            self._tiles[key] = payload
        return payload

    def tile_in_coverage(self, z: int, x: int, y: int) -> bool:
        meta = self.meta()
        if not meta.bounds or z > meta.maxzoom:
            return False
        west, south, east, north = meta.bounds
        fx0, fy0 = _global_px(west, north, z)
        fx1, fy1 = _global_px(east, south, z)
        return not (x * TILE >= fx1 or (x + 1) * TILE <= fx0 or y * TILE >= fy1 or (y + 1) * TILE <= fy0)
