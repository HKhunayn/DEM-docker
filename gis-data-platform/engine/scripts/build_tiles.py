"""Convert Copernicus/FABDEM DEM GeoTIFFs (1-degree COG tiles) into a
Web-Mercator XYZ pyramid of Terrarium-encoded elevation tiles (lossless
WebP by default, PNG with --terrain-format png) (plus, optionally, a
hillshade pyramid and the ArcGIS LERC pyramid — all
three from ONE reprojection per tile).

WINDOWED, RESUMABLE, PARALLEL:
  * each output tile reads only its own footprint from the source COGs
    (rasterio windowed reads, overview-aware downsampling) — memory stays
    constant no matter how large the AOI is;
  * tiles already on disk are skipped, so an interrupted run resumes where
    it stopped (use --force to rebuild everything). Every output dir holds a
    build_manifest.json (engine version + source fingerprint + encoding
    params); when it is missing or does not match, the existing tiles in the
    bbox are treated as stale and rebuilt;
  * tiles are built in a memory-safe process pool (--workers); a failing
    tile is reported and skipped, and the run exits non-zero at the end;
  * Terrarium tiles are LOSSLESS WebP ({z}/{x}/{y}.webp; exact=True keeps
    the RGB under transparent pixels): ~30% smaller and ~3x faster to encode
    than PNG level 6. Lossy WebP would corrupt the packed heights — never.
  * PNGs (hillshade, --terrain-format png) use zlib level 1 and no
    optimize=True: level 6 costs ~5x CPU for ~10% size, optimize ~10x CPU
    for ~3%.
  * every file is written atomically and world-readable (0644) so a
    non-root web server can serve the pyramid.

Terrarium encoding (used by Mapzen/AWS terrain tiles, MapLibre, deck.gl):
    height_m = (R * 256 + G + B / 256) - 32768
No-data pixels are transparent (alpha = 0) and decoded as noDataValue. Their
RGB carries the nearest valid height, so clients that ignore alpha do not
draw cliffs down to 0 m.

Hillshade tiles are greyscale+alpha PNGs ('LA'): alpha 0 where the source
has no data, 255 elsewhere.

Usage:
  python build_tiles.py --src ../data/dem/*.tif \\
      --bbox 34.5 16.3 55.7 32.2 --minzoom 0 --maxzoom 13 --out ../tiles/terrarium \\
      [--hillshade-out ../tiles/hillshade] [--lerc-out ../tiles --lerc-name local30m]
"""

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import time

# Cap GDAL's block cache per process (MB). Must be set before rasterio/GDAL
# initialize: the default is 5% of RAM, which OOMs the VM when many worker
# processes each open hundreds of COGs (32 workers x ~1.2 GB killed a run).
os.environ.setdefault("GDAL_CACHEMAX", "128")

import numpy as np
import rasterio
import rasterio.windows
from affine import Affine
from PIL import Image
from rasterio.crs import CRS
from rasterio.fill import fillnodata
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from pool_runner import PoolAborted, default_workers, describe_limits, run_pool  # noqa: E402

# Bump whenever the pixels written for the same sources change: existing
# pyramids carrying another version are rebuilt instead of resumed.
#   2 = multi-source merge fix, decimated-read transform fix, ground-scaled
#       seamless hillshade, filled RGB under transparent pixels.
#   3 = Terrarium as lossless WebP (default), hillshade 'LA' with transparent
#       no-data, PNG zlib level 1, files written 0644.
ENGINE_VERSION = 3
MANIFEST_NAME = "build_manifest.json"
TERRAIN_FORMATS = ("webp", "png")
DEFAULT_TERRAIN_FORMAT = "webp"
FILE_MODE = 0o644         # tiles/json must be readable by a non-root server
PNG_COMPRESS_LEVEL = 1    # zlib level: 6 costs ~5x CPU for ~10% size


def _default_workers() -> int:
    """Kept for callers/tests; sizing now lives in pool_runner."""
    return default_workers()


NODATA = -32768.0
TILE_PX = 256
ORIGIN_SHIFT = 20037508.342789244  # half earth circumference in EPSG:3857 meters
MAX_LAT = 85.05112878             # Web Mercator latitude limit
MERC = CRS.from_epsg(3857)
WGS84 = CRS.from_epsg(4326)
READ_CAP = 1024           # max source pixels read per source per tile
EDGE_PAD = 2.0 / TILE_PX  # fraction of tile extent read extra so bilinear
                          # resampling never hits the edge of a source window
HS_BUFFER = 1             # extra pixels around a tile for seamless hillshade
HS_AZIMUTH, HS_ALTITUDE = 315.0, 45.0
PART_MAX_AGE_S = 60       # *.part files older than this are leftovers


# ---- tile math -------------------------------------------------------------

def validate_bbox(bbox) -> tuple[float, float, float, float]:
    minlon, minlat, maxlon, maxlat = (float(v) for v in bbox)
    if not all(math.isfinite(v) for v in (minlon, minlat, maxlon, maxlat)):
        raise ValueError(f"bbox has non-finite values: {list(bbox)}")
    if minlon > maxlon:
        raise ValueError(
            f"bbox MINLON {minlon} > MAXLON {maxlon}: swapped or antimeridian-"
            f"crossing bboxes are not supported (order: MINLON MINLAT MAXLON MAXLAT)")
    if minlat > maxlat:
        raise ValueError(f"bbox MINLAT {minlat} > MAXLAT {maxlat} "
                         f"(order: MINLON MINLAT MAXLON MAXLAT)")
    if minlon < -180 or maxlon > 180 or minlat < -90 or maxlat > 90:
        raise ValueError(f"bbox {list(bbox)} outside lon [-180, 180] / lat [-90, 90]")
    return minlon, minlat, maxlon, maxlat


def _frac_tile(lon: float, lat: float, z: int) -> tuple[float, float]:
    """Fractional XYZ tile coordinates (lat clamped to the Mercator limit)."""
    n = 2**z
    lon = min(max(lon, -180.0), 180.0)
    lat = min(max(lat, -MAX_LAT), MAX_LAT)
    lat_r = math.radians(lat)
    fx = (lon + 180.0) / 360.0 * n
    fy = (1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n
    return fx, fy


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    """Tile containing a point; lat clamped to +-85.0511, x/y to [0, 2^z-1]."""
    n = 2**z
    fx, fy = _frac_tile(lon, lat, z)
    return (min(max(int(math.floor(fx)), 0), n - 1),
            min(max(int(math.floor(fy)), 0), n - 1))


def tile_range(bbox, z: int) -> tuple[int, int, int, int]:
    """Inclusive tile range (x0, y0, x1, y1) covering a lon/lat bbox. A max
    edge lying exactly on a tile boundary does not add the next tile."""
    minlon, minlat, maxlon, maxlat = validate_bbox(bbox)
    n = 2**z
    x0, y0 = lonlat_to_tile(minlon, maxlat, z)
    fx, fy = _frac_tile(maxlon, minlat, z)
    eps = 1e-9
    x1 = min(max(int(math.ceil(fx - eps)) - 1, x0), n - 1)
    y1 = min(max(int(math.ceil(fy - eps)) - 1, y0), n - 1)
    return x0, y0, x1, y1


def tile_merc_bounds(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    size = 2 * ORIGIN_SHIFT / 2**z
    minx = x * size - ORIGIN_SHIFT
    maxx = (x + 1) * size - ORIGIN_SHIFT
    maxy = ORIGIN_SHIFT - y * size
    miny = ORIGIN_SHIFT - (y + 1) * size
    return minx, miny, maxx, maxy


def tile_lonlat_bounds(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = tile_merc_bounds(x, y, z)
    return transform_bounds(MERC, WGS84, minx, miny, maxx, maxy)


def pixel_center_lonlat(x: int, y: int, z: int, col: float, row: float):
    """lon/lat of pixel (col, row) centre of tile z/x/y."""
    n = 2**z * TILE_PX
    gx, gy = x * TILE_PX + col + 0.5, y * TILE_PX + row + 0.5
    lon = gx / n * 360.0 - 180.0
    lat = math.degrees(2 * math.atan(math.exp(math.pi * (1 - 2 * gy / n))) - math.pi / 2)
    return lon, lat


# ---- sources ---------------------------------------------------------------

class SourceIndex:
    """All source DEMs, bucketed by 1-degree cell for fast footprint lookup."""

    def __init__(self, paths):
        self.grid: dict[tuple[int, int], list] = {}
        self.datasets = []
        for p in paths:
            ds = rasterio.open(p)
            self.datasets.append(ds)
            b = ds.bounds
            for la in range(math.floor(b.bottom), math.floor(b.top) + 1):
                for lo in range(math.floor(b.left), math.floor(b.right) + 1):
                    self.grid.setdefault((la, lo), []).append(ds)

    def close(self):
        for ds in self.datasets:
            ds.close()

    def sources_for(self, lon0, lat0, lon1, lat1):
        seen, out = set(), []
        for la in range(math.floor(lat0), math.floor(lat1) + 1):
            for lo in range(math.floor(lon0), math.floor(lon1) + 1):
                for ds in self.grid.get((la, lo), ()):
                    if id(ds) not in seen:
                        seen.add(id(ds))
                        out.append(ds)
        return out


def source_fingerprint(paths) -> str:
    """Hash of the sorted source basenames + sizes (cheap, path-independent)."""
    items = []
    for p in paths:
        try:
            size = os.path.getsize(p)
        except OSError:
            size = -1
        items.append(f"{os.path.basename(p)}:{size}")
    return hashlib.sha256("\n".join(sorted(items)).encode()).hexdigest()[:16]


def _compose(a, b):
    """a then-after b (affine >= 2.4 prefers @ over *)."""
    try:
        return a @ b
    except TypeError:
        return a * b


def read_patch(ds, lon0, lat0, lon1, lat1):
    """Windowed, overview-aware read of the source coverage inside a bbox.
    Returns (float32 array, transform) or None when nothing overlaps."""
    win = rasterio.windows.from_bounds(lon0, lat0, lon1, lat1, transform=ds.transform)
    try:
        win = rasterio.windows.intersection(
            win, rasterio.windows.Window(0, 0, ds.width, ds.height))
    except Exception:  # rasterio.errors.WindowError: disjoint windows
        return None
    if win is None or win.width < 0.5 or win.height < 0.5:
        return None
    h = max(1, min(READ_CAP, int(math.ceil(win.height))))
    w = max(1, min(READ_CAP, int(math.ceil(win.width))))
    arr = ds.read(1, window=win, out_shape=(h, w),
                  resampling=Resampling.bilinear, boundless=False)
    # The window transform describes source-resolution pixels; a decimated
    # out_shape read returns bigger pixels, so rescale it to the array.
    transform = _compose(rasterio.windows.transform(win, ds.transform),
                         Affine.scale(win.width / w, win.height / h))
    arr = arr.astype(np.float32)
    arr[arr < -1000.0] = NODATA
    return arr, transform


def build_tile(idx: SourceIndex, x: int, y: int, z: int, buffer: int = 0):
    """Reproject the source coverage for one XYZ tile. Returns a float32
    array of (TILE_PX + 2*buffer)^2 pixels on the tile's pixel grid, extended
    by `buffer` pixels on each side, or None when no source data touches the
    tile itself."""
    minx, miny, maxx, maxy = tile_merc_bounds(x, y, z)
    cell = (maxx - minx) / TILE_PX
    minx, miny = minx - buffer * cell, miny - buffer * cell
    maxx, maxy = maxx + buffer * cell, maxy + buffer * cell
    size = TILE_PX + 2 * buffer
    lim = ORIGIN_SHIFT
    lon0, lat0, lon1, lat1 = transform_bounds(
        MERC, WGS84, max(minx, -lim), max(miny, -lim), min(maxx, lim), min(maxy, lim))
    pad_lon = (lon1 - lon0) * EDGE_PAD
    pad_lat = (lat1 - lat0) * EDGE_PAD
    lon0p, lat0p = lon0 - pad_lon, lat0 - pad_lat
    lon1p, lat1p = lon1 + pad_lon, lat1 + pad_lat
    sources = idx.sources_for(lon0p, lat0p, lon1p, lat1p)
    if not sources:
        return None
    dst = np.full((size, size), NODATA, np.float32)
    scratch = np.empty_like(dst)
    dst_transform = from_bounds(minx, miny, maxx, maxy, size, size)
    for ds in sources:
        patch = read_patch(ds, lon0p, lat0p, lon1p, lat1p)
        if patch is None:
            continue
        arr, src_transform = patch
        # reproject() resets its whole destination to nodata on every call
        # (init_dest_nodata), so each source goes into a scratch array and
        # only pixels still empty in dst are taken from it: first valid wins.
        scratch.fill(NODATA)
        reproject(
            source=arr, destination=scratch,
            src_transform=src_transform, src_crs=ds.crs, src_nodata=NODATA,
            dst_transform=dst_transform, dst_crs=MERC, dst_nodata=NODATA,
            resampling=Resampling.bilinear,
        )
        take = (scratch > NODATA + 1.0) & (dst <= NODATA + 1.0)
        dst[take] = scratch[take]
        if not (dst <= NODATA + 1.0).any():
            break
    inner = dst[buffer:buffer + TILE_PX, buffer:buffer + TILE_PX]
    if not (inner > NODATA + 1.0).any():
        return None
    return dst


def source_stats(idx: SourceIndex, cap: int = 512) -> dict | None:
    """min/max/mean/stdv over all sources via overview-scale reads."""
    n, s, s2 = 0, 0.0, 0.0
    mn, mx = math.inf, -math.inf
    for ds in idx.datasets:
        oh, ow = min(cap, ds.height), min(cap, ds.width)
        arr = ds.read(1, out_shape=(oh, ow), resampling=Resampling.bilinear)
        arr = arr.astype(np.float32)
        arr[arr < -1000.0] = NODATA
        v = arr[arr > NODATA + 1.0]
        if not v.size:
            continue
        v64 = v.astype(np.float64)
        n += v64.size
        s += float(v64.sum())
        s2 += float((v64 * v64).sum())
        mn = min(mn, float(v64.min()))
        mx = max(mx, float(v64.max()))
    if n == 0:
        return None
    mean = s / n
    var = max(0.0, s2 / n - mean * mean)
    return dict(min=mn, max=mx, mean=mean, stdv=math.sqrt(var))


# ---- encoders --------------------------------------------------------------

def fill_nodata(arr: np.ndarray) -> np.ndarray:
    """Copy of arr with NODATA pixels replaced by the nearest valid height
    (GDAL FillNodata); anything still unreachable gets the valid mean.
    Returns arr itself when it is all-valid or all-nodata."""
    valid = arr > NODATA + 1.0
    if valid.all() or not valid.any():
        return arr
    filled = fillnodata(arr.astype(np.float32, copy=True),
                        mask=valid.astype(np.uint8),
                        max_search_distance=float(max(arr.shape)) * 1.5,
                        smoothing_iterations=0)
    still = filled <= NODATA + 1.0
    if still.any():
        filled[still] = float(arr[valid].mean())
    return filled


def _row_latitudes(y: int, z: int, rows: int, buffer: int) -> np.ndarray:
    """Latitude (radians) of each pixel-row centre of a (buffered) tile."""
    n = 2**z * TILE_PX
    gy = y * TILE_PX - buffer + np.arange(rows) + 0.5
    return 2 * np.arctan(np.exp(np.pi * (1 - 2 * gy / n))) - np.pi / 2


def hillshade(arr: np.ndarray, cell_m, buffer: int = 0) -> Image.Image:
    """ESRI-style hillshade (azimuth 315°, altitude 45°) -> 'LA' image
    (grey + alpha).

    cell_m is the GROUND cell size in metres: a scalar, or one value per row
    (Web Mercator cell * cos(lat)). NODATA pixels are filled from the nearest
    valid height before the gradient (no cliffs to 0 m), drawn mid-gray and
    made transparent (alpha 0; alpha 255 elsewhere).
    `buffer` pixels on each side feed the gradient and are then cropped, so
    adjacent tiles shade seamlessly."""
    valid = arr > NODATA + 1.0
    z = fill_nodata(arr).astype(np.float64) if valid.any() else np.zeros(arr.shape)
    cell = np.asarray(cell_m, np.float64)
    if cell.ndim:
        cell = cell.reshape(-1, 1)
    gy, gx = np.gradient(z)
    zx, zy = gx / cell, gy / cell
    slope = np.arctan(np.hypot(zx, zy))
    aspect = np.arctan2(-zx, zy)
    zenith = np.radians(90.0 - HS_ALTITUDE)
    az = np.radians(HS_AZIMUTH)
    hs = (np.cos(zenith) * np.cos(slope)
          + np.sin(zenith) * np.sin(slope) * np.cos(az - aspect))
    hs = np.clip(hs, 0.0, 1.0)
    out = (hs * 255).astype(np.uint8)
    out[~valid] = 128
    alpha = np.where(valid, 255, 0).astype(np.uint8)
    if buffer:
        out = out[buffer:-buffer, buffer:-buffer]
        alpha = alpha[buffer:-buffer, buffer:-buffer]
    return Image.merge("LA", (Image.fromarray(np.ascontiguousarray(out)),
                              Image.fromarray(np.ascontiguousarray(alpha))))


def tile_hillshade(dst: np.ndarray, x: int, y: int, z: int, buffer: int) -> Image.Image:
    """Hillshade of a (buffered) build_tile() array with per-row ground cells."""
    minx, _, maxx, _ = tile_merc_bounds(x, y, z)
    merc_cell = (maxx - minx) / TILE_PX
    lat = _row_latitudes(y, z, dst.shape[0], buffer)
    return hillshade(dst, merc_cell * np.cos(lat), buffer)


def encode_terrarium(arr: np.ndarray) -> Image.Image:
    h, w = arr.shape
    valid = arr > NODATA + 1.0
    # transparent pixels still carry the nearest valid height in their RGB
    heights = fill_nodata(arr) if valid.any() else np.zeros_like(arr)
    heights = np.where(heights > NODATA + 1.0, heights, 0.0)
    v = np.round((heights + 32768.0) * 256.0).astype(np.int64)
    np.clip(v, 0, 2**24 - 1, out=v)
    out = np.zeros((h, w, 4), np.uint8)
    out[..., 0] = (v >> 16) & 255
    out[..., 1] = (v >> 8) & 255
    out[..., 2] = v & 255
    out[..., 3] = np.where(valid, 255, 0)
    return Image.fromarray(out, "RGBA")


def decode_terrarium(path: str):
    """Heights (float32) from an existing Terrarium tile (WebP or PNG); None
    if unreadable (a truncated tile from an interrupted build is treated as
    missing). alpha = 0 is no-data whatever the RGB holds."""
    try:
        with Image.open(path) as im:
            arr = np.asarray(im.convert("RGBA")).astype(np.int64)
    except Exception:  # noqa: BLE001
        return None
    v = arr[..., 0] * 256 * 256 + arr[..., 1] * 256 + arr[..., 2]
    heights = (v / 256.0 - 32768.0).astype(np.float32)
    heights[arr[..., 3] == 0] = NODATA
    return heights


decode_terrarium_png = decode_terrarium  # backwards-compatible name


def terrarium_tile_path(root: str, z: int, x: int, y: int,
                        fmt: str = DEFAULT_TERRAIN_FORMAT) -> str:
    """{root}/{z}/{x}/{y}.{webp|png}"""
    return os.path.join(root, str(z), str(x), f"{y}.{fmt}")


def save_terrarium(image: Image.Image, f, fmt: str = DEFAULT_TERRAIN_FORMAT) -> None:
    """Terrarium tile to a file object. WebP MUST be lossless (heights are
    packed into RGB) and exact (transparent pixels keep their filled RGB)."""
    if fmt == "webp":
        image.save(f, format="WEBP", lossless=True, exact=True, method=1, quality=25)
    elif fmt == "png":
        image.save(f, format="PNG", compress_level=PNG_COMPRESS_LEVEL)
    else:
        raise ValueError(f"unknown terrain format {fmt!r} (expected one of {TERRAIN_FORMATS})")


def _tile_stats(arr: np.ndarray):
    valid = arr[arr > NODATA + 1.0]
    if not valid.size:
        return None
    return float(valid.min()), float(valid.max())


# ---- atomic file writes ----------------------------------------------------

def write_atomic(path: str, writer) -> None:
    """writer(fileobj) into a unique temp file next to path, then os.replace.
    The temp file is removed on any error."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".",
                               suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            writer(f)
        # mkstemp creates 0600: a non-root web server would answer 403
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_png(image: Image.Image, path: str) -> None:
    """Atomic write, zlib level 1. No optimize=True: it spends ~10x CPU for
    ~3% size."""
    write_atomic(path, lambda f: image.save(f, format="PNG",
                                            compress_level=PNG_COMPRESS_LEVEL))


def _write_terrarium(image: Image.Image, path: str,
                     fmt: str = DEFAULT_TERRAIN_FORMAT) -> None:
    write_atomic(path, lambda f: save_terrarium(image, f, fmt))


def write_json_atomic(path: str, obj) -> None:
    data = json.dumps(obj, indent=2).encode()
    write_atomic(path, lambda f: f.write(data))


def sweep_parts(root: str, max_age_s: float = PART_MAX_AGE_S) -> int:
    """Delete leftover *.part temp files (interrupted writes) under root."""
    if not root or not os.path.isdir(root):
        return 0
    now, removed = time.time(), 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".part"):
                p = os.path.join(dirpath, f)
                try:
                    if now - os.path.getmtime(p) > max_age_s:
                        os.unlink(p)
                        removed += 1
                except OSError:
                    pass
    return removed


# ---- build manifests (resume fingerprint) ----------------------------------

def _has_tiles(out_dir: str) -> bool:
    try:
        return any(e.is_dir() and e.name.isdigit() for e in os.scandir(out_dir))
    except OSError:
        return False


class BuildManifest:
    """OUT_DIR/build_manifest.json ties the tiles in a dir to the engine
    version, sources and encoding params that produced them.

    Resume trusts existing tiles only when the stored engine version, source
    fingerprint and params match. Otherwise (or with --force, or tiles but no
    manifest) every zoom of this bbox is forced; finished forced zooms are
    recorded, so an interrupted forced rebuild resumes without redoing them
    (and without trusting the stale zooms it has not reached yet)."""

    def __init__(self, out_dir, label, params, source_hash, bbox, minzoom,
                 maxzoom, user_force=False, log=print):
        self.path = os.path.join(out_dir, MANIFEST_NAME)
        bbox = [float(v) for v in bbox]
        self.data = {
            "engine_version": ENGINE_VERSION,
            "source_hash": source_hash,
            "params": params,
            "bbox": bbox,
            "minzoom": minzoom,
            "maxzoom": maxzoom,
        }
        old = self._load()
        reason, done = None, []
        if user_force:
            reason = "--force"
        elif old is None:
            if _has_tiles(out_dir):
                reason = "existing tiles have no build_manifest.json (older engine)"
        elif old.get("engine_version") != ENGINE_VERSION:
            reason = (f"engine version changed ({old.get('engine_version')} -> "
                      f"{ENGINE_VERSION})")
        elif old.get("source_hash") != source_hash:
            reason = (f"source files changed (fingerprint {old.get('source_hash')} "
                      f"-> {source_hash})")
        elif old.get("params") != params:
            reason = f"encoding params changed ({old.get('params')} -> {params})"
        elif old.get("force_pending"):
            pend = old["force_pending"]
            if (pend.get("bbox") == bbox and pend.get("minzoom") == minzoom
                    and pend.get("maxzoom") == maxzoom):
                done = list(pend.get("zooms_done", []))
                reason = (f"resuming an interrupted forced rebuild "
                          f"(zooms already rebuilt: {done or 'none'})")
            else:
                reason = "an interrupted forced rebuild of another bbox/zoom range"
        self.forcing = reason is not None
        self.zooms_done = set(done)
        if self.forcing:
            log(f"{label}: REBUILDING existing tiles in this bbox (treated as "
                f"--force): {reason}")
            self.data["force_pending"] = {"bbox": bbox, "minzoom": minzoom,
                                          "maxzoom": maxzoom, "zooms_done": sorted(done)}
        self.data["complete"] = False
        self._save()

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _save(self):
        write_json_atomic(self.path, self.data)

    def force_zoom(self, z: int) -> bool:
        return self.forcing and z not in self.zooms_done

    def zoom_done(self, z: int) -> None:
        if self.forcing:
            self.zooms_done.add(z)
            self.data["force_pending"]["zooms_done"] = sorted(self.zooms_done)
            self._save()

    def finish(self) -> None:
        self.data.pop("force_pending", None)
        self.data["complete"] = True
        self._save()


def terrarium_params(fmt: str = DEFAULT_TERRAIN_FORMAT) -> dict:
    return {"encoding": "terrarium", "tile_px": TILE_PX, "nodata": NODATA,
            "resampling": "bilinear", "nodata_rgb": "nearest", "terrain_format": fmt}


TERRARIUM_PARAMS = terrarium_params(DEFAULT_TERRAIN_FORMAT)
HILLSHADE_PARAMS = {"azimuth": HS_AZIMUTH, "altitude": HS_ALTITUDE,
                    "cell": "ground", "buffer": HS_BUFFER, "mode": "LA"}


def lerc_params(flavor: str, maxzerr: float) -> dict:
    return {"flavor": flavor, "maxzerr": maxzerr, "tile_px": TILE_PX, "nodata": NODATA}


# ---- worker pool -----------------------------------------------------------

_IDX: SourceIndex | None = None


def _init_worker(src_paths):
    global _IDX
    _IDX = SourceIndex(src_paths)
    # Close datasets before interpreter teardown when the pool recycles this
    # worker; otherwise GDAL finalizers run late and print excepthook noise.
    import atexit
    atexit.register(_IDX.close)


def lerc_tile_path(tile_root, z, x, y):
    return os.path.join(tile_root, str(z), str(y), str(x))


def _tile_job(task):
    """One tile -> Terrarium WebP/PNG (+ hillshade PNG + LERC blob) from a
    single reprojection. task = (z, x, y, out_root, hs_root, force[, lerc[,
    terrain_format]]); force is a bool or a (terrarium, hillshade, lerc)
    tuple; lerc is None or (tile_root, flavor, maxzerr). Only a tile in the
    requested terrain format counts as done (a legacy .png does not satisfy a
    webp build)."""
    z, x, y, out_root, hs_root, force = task[:6]
    lerc = task[6] if len(task) > 6 else None
    fmt = task[7] if len(task) > 7 else DEFAULT_TERRAIN_FORMAT
    f_ter, f_hs, f_lerc = force if isinstance(force, tuple) else (force,) * 3
    terr_path = terrarium_tile_path(out_root, z, x, y, fmt)
    hs_path = os.path.join(hs_root, str(z), str(x), f"{y}.png") if hs_root else None
    lerc_path = lerc_tile_path(lerc[0], z, x, y) if lerc else None
    need_ter = f_ter or not os.path.exists(terr_path)
    need_hs = hs_path is not None and (f_hs or not os.path.exists(hs_path))
    need_lerc = lerc_path is not None and (
        f_lerc or not (os.path.exists(lerc_path) and os.path.getsize(lerc_path) > 0))

    if not need_ter and not need_hs and not need_lerc:
        heights = decode_terrarium(terr_path)
        if heights is not None:
            return "skip", _tile_stats(heights), 0
        need_ter = True  # truncated/corrupt tile -> rebuild it

    buffer = HS_BUFFER if need_hs else 0
    dst = build_tile(_IDX, x, y, z, buffer=buffer)
    if dst is None:
        return "empty", None, 0
    inner = dst[buffer:buffer + TILE_PX, buffer:buffer + TILE_PX] if buffer else dst
    st = _tile_stats(inner)
    if need_ter:
        _write_terrarium(encode_terrarium(inner), terr_path, fmt)
    if need_hs:
        _write_png(tile_hillshade(dst, x, y, z, buffer), hs_path)
    if need_lerc:
        import build_lerc  # lazy: build_lerc imports this module
        blob = build_lerc.encode_blob(np.ascontiguousarray(inner), lerc[1], lerc[2])
        write_atomic(lerc_path, lambda f: f.write(blob))
    return "write", st, int(need_lerc)


def _tilejson(args, gmin, gmax):
    minlon, minlat, maxlon, maxlat = args.bbox
    fmt = getattr(args, "terrain_format", DEFAULT_TERRAIN_FORMAT)
    return {
        "tilejson": "3.0.0",
        "name": "Copernicus DEM 30m (Terrarium)",
        "description": f"Local heightmap pyramid, Terrarium-encoded "
                       f"{'lossless WebP' if fmt == 'webp' else 'PNG'} tiles",
        "tiles": [f"/terrarium/{{z}}/{{x}}/{{y}}.{fmt}"],
        "format": fmt,
        "minzoom": args.minzoom,
        "maxzoom": args.maxzoom,
        "bounds": [minlon, minlat, maxlon, maxlat],
        "center": [(minlon + maxlon) / 2, (minlat + maxlat) / 2, args.maxzoom - 2],
        "encoding": "terrarium",
        "noDataValue": NODATA,
        "tileSize": TILE_PX,
        "statistics": {"min": round(gmin, 2), "max": round(gmax, 2)},
    }


def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", nargs="+", required=True, help="source GeoTIFF path(s)")
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"))
    ap.add_argument("--minzoom", type=int, default=8)
    ap.add_argument("--maxzoom", type=int, default=13)
    ap.add_argument("--out", default="public/terrarium")
    ap.add_argument("--terrain-format", choices=TERRAIN_FORMATS,
                    default=DEFAULT_TERRAIN_FORMAT,
                    help="Terrarium tile format: lossless WebP {z}/{x}/{y}.webp "
                         "(default) or PNG {z}/{x}/{y}.png")
    ap.add_argument("--hillshade-out", default=None,
                    help="also write a grayscale hillshade pyramid, e.g. public/hillshade")
    ap.add_argument("--lerc-out", default=None,
                    help="also write the ArcGIS LERC ImageServer pyramid in the same "
                         "pass (like build_lerc.py --out), e.g. public")
    ap.add_argument("--lerc-name", default="local30m", help="LERC service name")
    ap.add_argument("--flavor", choices=["lerc1", "lerc2"], default="lerc1")
    ap.add_argument("--maxzerr", type=float, default=0.01,
                    help="Lerc1 quantization bound in meters (0 = lossless)")
    ap.add_argument("--provider", default="copernicus", choices=["copernicus", "fabdem"],
                    help="source provider, for the LERC service copyright text")
    ap.add_argument("--description", default="Local 30 m heightmap (Copernicus DEM GLO-30)",
                    help="LERC service description")
    ap.add_argument("--force", action="store_true",
                    help="rebuild tiles even when they already exist on disk")
    ap.add_argument("--workers", type=int, default=0,
                    help="build processes (default: sized from memory)")
    args = ap.parse_args(argv)
    try:
        args.bbox = list(validate_bbox(args.bbox))
    except ValueError as e:
        ap.error(str(e))
    if args.minzoom > args.maxzoom:
        ap.error(f"--minzoom {args.minzoom} > --maxzoom {args.maxzoom}")
    return args


def _log(msg):
    print(msg)
    sys.stdout.flush()


def main(argv=None) -> int:
    args = parse_args(argv)
    workers = args.workers or _default_workers()

    probe = SourceIndex(args.src)
    n_src = len(probe.datasets)
    overview = source_stats(probe) if args.lerc_out else None
    probe.close()
    src_hash = source_fingerprint(args.src)
    _log(f"sources: {n_src} DEM file(s) (fingerprint {src_hash}); engine "
         f"v{ENGINE_VERSION}; windowed build, resume={'off' if args.force else 'on'}, "
         f"workers={workers}, terrain format={args.terrain_format}; "
         f"{describe_limits()}")

    common = dict(source_hash=src_hash, bbox=args.bbox, minzoom=args.minzoom,
                  maxzoom=args.maxzoom, user_force=args.force, log=_log)
    products = {"ter": BuildManifest(args.out, "terrarium",
                                     terrarium_params(args.terrain_format), **common)}
    roots = [args.out]
    if args.hillshade_out:
        products["hs"] = BuildManifest(args.hillshade_out, "hillshade",
                                       HILLSHADE_PARAMS, **common)
        roots.append(args.hillshade_out)
    lerc = svc_root = None
    if args.lerc_out:
        import build_lerc
        if args.flavor == "lerc1":
            build_lerc.require_lerc1enc()
            _log(f"lerc1 encoder: {build_lerc.lerc1_backend()}")
        svc_root = build_lerc.service_root(args.lerc_out, args.lerc_name)
        lerc = (os.path.join(svc_root, "tile"), args.flavor, args.maxzerr)
        products["lerc"] = BuildManifest(svc_root, "lerc",
                                         lerc_params(args.flavor, args.maxzerr), **common)
        roots.append(lerc[0])
    for r in roots:
        n = sweep_parts(r)
        if n:
            _log(f"removed {n} stale *.part temp file(s) under {r}")

    written_total = skipped_total = empty_total = failed_total = lerc_total = 0
    gmin, gmax = math.inf, -math.inf

    for z in range(args.minzoom, args.maxzoom + 1):
        x0, y0, x1, y1 = tile_range(args.bbox, z)
        span = (x1 - x0 + 1) * (y1 - y0 + 1)
        force = tuple(products[k].force_zoom(z) if k in products else False
                      for k in ("ter", "hs", "lerc"))
        tasks = [(z, x, y, args.out, args.hillshade_out, force, lerc, args.terrain_format)
                 for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
        c = {"w": 0, "s": 0, "e": 0, "f": 0, "l": 0, "min": gmin, "max": gmax}

        def on_result(res, c=c):
            status, st = res[0], res[1]
            c["l"] += res[2] if len(res) > 2 else 0
            if st:
                c["min"], c["max"] = min(c["min"], st[0]), max(c["max"], st[1])
            c["w" if status == "write" else "s" if status == "skip" else "e"] += 1

        def on_error(_desc, _msg, c=c):
            c["f"] += 1

        try:
            workers = run_pool(_tile_job, tasks, workers, _init_worker,
                               (list(args.src),), on_result, f"z{z}", on_error=on_error)
        except PoolAborted as e:
            _log(f"ERROR: z{z}: {e}. Build aborted; no tilejson.json written.")
            return 1
        gmin, gmax = c["min"], c["max"]
        written_total += c["w"]
        skipped_total += c["s"]
        empty_total += c["e"]
        failed_total += c["f"]
        lerc_total += c["l"]
        if c["f"] == 0:
            for m in products.values():
                m.zoom_done(z)
        _log(f"z{z}: x={x0}..{x1} y={y0}..{y1} tiles={span} "
             f"written={c['w']} skipped={c['s']} empty={c['e']} failed={c['f']} "
             f"(cumulative written {written_total})")

    if failed_total:
        _log(f"ERROR: {failed_total} tile(s) failed (listed above); re-run to retry "
             f"them (finished tiles are kept). No tilejson.json written.")
        return 1
    # TileJSON metadata for the client (counts include resumed tiles)
    total = written_total + skipped_total
    if total == 0 or not (math.isfinite(gmin) and math.isfinite(gmax)):
        _log(f"ERROR: wrote {total} tiles, elevation range {gmin}..{gmax}. "
             f"The bbox probably does not intersect the source DEM footprint. "
             f"No tilejson.json written.")
        return 1
    write_json_atomic(os.path.join(os.path.dirname(args.out.rstrip("/\\")), "tilejson.json"),
                      _tilejson(args, gmin, gmax))
    if lerc:
        import build_lerc
        # rounded like tilejson statistics (what build_lerc.py reads back)
        stats = dict(min=round(gmin, 2), max=round(gmax, 2),
                     mean=overview["mean"] if overview else (gmin + gmax) / 2,
                     stdv=overview["stdv"] if overview else 0.0)
        build_lerc.write_service_json(
            svc_root, name=args.lerc_name, description=args.description,
            provider=args.provider, minzoom=args.minzoom, maxzoom=args.maxzoom,
            bbox=args.bbox, stats=stats)
        _log(f"WROTE {lerc_total} LERC tiles ({args.flavor}) + ImageServer.json "
             f"-> {svc_root}")
    for m in products.values():
        m.finish()
    _log(f"WROTE {written_total} tiles, skipped {skipped_total} existing "
         f"({empty_total} empty) z{args.minzoom}-{args.maxzoom}, "
         f"elevation range {gmin:.1f}..{gmax:.1f} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
