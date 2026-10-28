"""Convert Copernicus/FABDEM DEM GeoTIFFs (1-degree COG tiles) into a
Web-Mercator XYZ pyramid of Terrarium-encoded PNG elevation tiles.

WINDOWED, RESUMABLE, PARALLEL:
  * each output tile reads only its own footprint from the source COGs
    (rasterio windowed reads, overview-aware downsampling) — memory stays
    constant no matter how large the AOI is;
  * tiles already on disk are skipped, so an interrupted run resumes where
    it stopped (use --force to rebuild everything);
  * tiles are built in a process pool (--workers, default: all CPUs).
  * PNGs are written without optimize=True: it costs ~10x CPU for ~3%
    size on elevation tiles.

Terrarium encoding (used by Mapzen/AWS terrain tiles, MapLibre, deck.gl):
    height_m = (R * 256 + G + B / 256) - 32768
No-data pixels are transparent (alpha = 0) and decoded as noDataValue.

Usage:
  python build_tiles.py --src ../data/dem/*.tif \
      --bbox 34.5 16.3 55.7 32.2 --minzoom 0 --maxzoom 13 --out ../tiles/terrarium
"""

import argparse
import json
import math
import os
import sys

# Cap GDAL's block cache per process (MB). Must be set before rasterio/GDAL
# initialize: the default is 5% of RAM, which OOMs the VM when many worker
# processes each open hundreds of COGs (32 workers x ~1.2 GB killed a run).
os.environ.setdefault("GDAL_CACHEMAX", "128")

import numpy as np
import rasterio
import rasterio.windows
from PIL import Image
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from pool_runner import default_workers, describe_limits, run_pool  # noqa: E402


def _default_workers() -> int:
    """Kept for callers/tests; sizing now lives in pool_runner."""
    return default_workers()


NODATA = -32768.0
TILE_PX = 256
ORIGIN_SHIFT = 20037508.342789244  # half earth circumference in EPSG:3857 meters
MERC = CRS.from_epsg(3857)
WGS84 = CRS.from_epsg(4326)
READ_CAP = 1024           # max source pixels read per source per tile
EDGE_PAD = 2.0 / TILE_PX  # fraction of tile extent read extra so bilinear
                          # resampling never hits the edge of a source window


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
    return x, y


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
    transform = rasterio.windows.transform(win, ds.transform)
    arr = arr.astype(np.float32)
    arr[arr < -1000.0] = NODATA
    return arr, transform


def build_tile(idx: SourceIndex, x: int, y: int, z: int):
    """Reproject the source coverage for one XYZ tile. Returns the float32
    tile or None when no source data touches it."""
    lon0, lat0, lon1, lat1 = tile_lonlat_bounds(x, y, z)
    pad_lon = (lon1 - lon0) * EDGE_PAD
    pad_lat = (lat1 - lat0) * EDGE_PAD
    lon0p, lat0p = lon0 - pad_lon, lat0 - pad_lat
    lon1p, lat1p = lon1 + pad_lon, lat1 + pad_lat
    sources = idx.sources_for(lon0p, lat0p, lon1p, lat1p)
    if not sources:
        return None
    dst = np.full((TILE_PX, TILE_PX), NODATA, np.float32)
    minx, miny, maxx, maxy = tile_merc_bounds(x, y, z)
    dst_transform = from_bounds(minx, miny, maxx, maxy, TILE_PX, TILE_PX)
    for ds in sources:
        patch = read_patch(ds, lon0p, lat0p, lon1p, lat1p)
        if patch is None:
            continue
        arr, src_transform = patch
        reproject(
            source=arr, destination=dst,
            src_transform=src_transform, src_crs=ds.crs, src_nodata=NODATA,
            dst_transform=dst_transform, dst_crs=MERC, dst_nodata=NODATA,
            resampling=Resampling.bilinear,
        )
    if not (dst > NODATA + 1.0).any():
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


def hillshade(arr: np.ndarray, cell_m: float) -> Image.Image:
    """ESRI-style hillshade (azimuth 315°, altitude 45°) -> grayscale PNG."""
    valid = arr > NODATA + 1.0
    z = np.where(valid, arr, 0.0)
    zy, zx = np.gradient(z, cell_m)
    slope = np.arctan(np.hypot(zx, zy))
    aspect = np.arctan2(-zx, zy)
    zenith = np.radians(45.0)   # 90 - altitude(45)
    az = np.radians(315.0)
    hs = (np.cos(zenith) * np.cos(slope)
          + np.sin(zenith) * np.sin(slope) * np.cos(az - aspect))
    hs = np.clip(hs, 0.0, 1.0)
    out = (hs * 255).astype(np.uint8)
    out[~valid] = 128
    return Image.fromarray(out, "L")


def encode_terrarium(arr: np.ndarray) -> Image.Image:
    h, w = arr.shape
    valid = arr > NODATA + 1.0
    v = np.round((np.where(valid, arr, 0.0) + 32768.0) * 256.0).astype(np.int64)
    np.clip(v, 0, 2**24 - 1, out=v)
    out = np.zeros((h, w, 4), np.uint8)
    out[..., 0] = (v >> 16) & 255
    out[..., 1] = (v >> 8) & 255
    out[..., 2] = v & 255
    out[..., 3] = np.where(valid, 255, 0)
    return Image.fromarray(out, "RGBA")


def decode_terrarium_png(path: str):
    """Heights (float32) from an existing Terrarium tile; None if unreadable
    (a truncated PNG from an interrupted build is treated as missing)."""
    try:
        with Image.open(path) as im:
            arr = np.asarray(im.convert("RGBA")).astype(np.int64)
    except Exception:  # noqa: BLE001
        return None
    v = arr[..., 0] * 256 * 256 + arr[..., 1] * 256 + arr[..., 2]
    heights = (v / 256.0 - 32768.0).astype(np.float32)
    heights[arr[..., 3] == 0] = NODATA
    return heights


def _tile_stats(arr: np.ndarray):
    valid = arr[arr > NODATA + 1.0]
    if not valid.size:
        return None
    return float(valid.min()), float(valid.max())


def _write_png(image: Image.Image, path: str) -> None:
    """Atomic write. No optimize=True: it spends ~10x CPU for ~3% size."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    image.save(tmp, format="PNG")
    os.replace(tmp, path)


# ---- worker pool -----------------------------------------------------------

_IDX: SourceIndex | None = None


def _init_worker(src_paths):
    global _IDX
    _IDX = SourceIndex(src_paths)
    # Close datasets before interpreter teardown when the pool recycles this
    # worker; otherwise GDAL finalizers run late and print excepthook noise.
    import atexit
    atexit.register(_IDX.close)


def _tile_job(task):
    z, x, y, out_root, hs_root, force = task
    terr_path = os.path.join(out_root, str(z), str(x), f"{y}.png")
    hs_path = os.path.join(hs_root, str(z), str(x), f"{y}.png") if hs_root else None
    need_ter = force or not os.path.exists(terr_path)
    need_hs = hs_path is not None and (force or not os.path.exists(hs_path))

    if not need_ter and not need_hs:
        heights = decode_terrarium_png(terr_path)
        if heights is not None:
            return "skip", _tile_stats(heights)
        need_ter = True  # truncated/corrupt PNG -> rebuild it

    dst = build_tile(_IDX, x, y, z)
    if dst is None:
        return "empty", None
    st = _tile_stats(dst)
    if need_ter:
        _write_png(encode_terrarium(dst), terr_path)
    if need_hs and hs_path:
        minx, miny, maxx, maxy = tile_merc_bounds(x, y, z)
        cell_m = (maxx - minx) / TILE_PX
        _write_png(hillshade(dst, cell_m), hs_path)
    return "write", st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", nargs="+", required=True, help="source GeoTIFF path(s)")
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"))
    ap.add_argument("--minzoom", type=int, default=8)
    ap.add_argument("--maxzoom", type=int, default=13)
    ap.add_argument("--out", default="public/terrarium")
    ap.add_argument("--hillshade-out", default=None,
                    help="also write a grayscale hillshade pyramid, e.g. public/hillshade")
    ap.add_argument("--force", action="store_true",
                    help="rebuild tiles even when they already exist on disk")
    ap.add_argument("--workers", type=int, default=0,
                    help="build processes (default: number of CPUs)")
    args = ap.parse_args()

    minlon, minlat, maxlon, maxlat = args.bbox
    workers = args.workers or _default_workers()

    probe = SourceIndex(args.src)
    print(f"sources: {len(probe.datasets)} DEM file(s); windowed build, "
          f"resume={'off' if args.force else 'on'}, workers={workers}; "
          f"{describe_limits()}")
    sys.stdout.flush()
    probe.close()

    written_total = skipped_total = empty_total = 0
    gmin, gmax = math.inf, -math.inf

    for z in range(args.minzoom, args.maxzoom + 1):
        x0, y0 = lonlat_to_tile(minlon, maxlat, z)  # top-left
        x1, y1 = lonlat_to_tile(maxlon, minlat, z)  # bottom-right
        span = (x1 - x0 + 1) * (y1 - y0 + 1)
        tasks = [(z, x, y, args.out, args.hillshade_out, args.force)
                 for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
        c = {"w": 0, "s": 0, "e": 0, "min": gmin, "max": gmax}

        def on_result(res, c=c):
            status, st = res
            if st:
                c["min"], c["max"] = min(c["min"], st[0]), max(c["max"], st[1])
            c["w" if status == "write" else "s" if status == "skip" else "e"] += 1

        workers = run_pool(_tile_job, tasks, workers, _init_worker,
                           (list(args.src),), on_result, f"z{z}")
        gmin, gmax = c["min"], c["max"]
        written_total += c["w"]
        skipped_total += c["s"]
        empty_total += c["e"]
        print(f"z{z}: x={x0}..{x1} y={y0}..{y1} tiles={span} "
              f"written={c['w']} skipped={c['s']} empty={c['e']} "
              f"(cumulative written {written_total})")
        sys.stdout.flush()

    # TileJSON metadata for the client (counts include resumed tiles)
    total = written_total + skipped_total
    if total == 0 or not (math.isfinite(gmin) and math.isfinite(gmax)):
        print(f"ERROR: wrote {total} tiles, elevation range {gmin}..{gmax}. "
              f"The bbox probably does not intersect the source DEM footprint. "
              f"No tilejson.json written.")
        return 1
    meta = {
        "tilejson": "3.0.0",
        "name": "Copernicus DEM 30m (Terrarium)",
        "description": "Local heightmap pyramid, Terrarium-encoded PNG tiles",
        "tiles": ["/terrarium/{z}/{x}/{y}.png"],
        "minzoom": args.minzoom,
        "maxzoom": args.maxzoom,
        "bounds": [minlon, minlat, maxlon, maxlat],
        "center": [(minlon + maxlon) / 2, (minlat + maxlat) / 2, args.maxzoom - 2],
        "encoding": "terrarium",
        "noDataValue": NODATA,
        "tileSize": TILE_PX,
        "statistics": {"min": round(gmin, 2), "max": round(gmax, 2)},
    }
    with open(os.path.join(os.path.dirname(args.out.rstrip("/\\")), "tilejson.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print(f"WROTE {written_total} tiles, skipped {skipped_total} existing "
          f"({empty_total} empty) z{args.minzoom}-{args.maxzoom}, "
          f"elevation range {gmin:.1f}..{gmax:.1f} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())