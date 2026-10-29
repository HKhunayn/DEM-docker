"""Verify the Terrarium tile pyramid against the source DEM.

Terrarium tiles may be lossless WebP ({z}/{x}/{y}.webp, the default) or PNG;
--terrain-format auto takes the format from tilejson.json ("format"), else
from the files present.

Checks (all scoped to the AOI being verified — nothing is hard-coded to a
particular place):
  1. Terrarium encode/decode round-trip (in the pyramid's format) is
     bit-exact — including the RGB under transparent pixels — and within the
     quantization step; transparent (alpha = 0) pixels decode as no-data.
  2. EVERY zoom of this build: a sample of tiles (random ones plus tiles on
     the 1-degree source boundaries crossing the AOI, plus the tile at
     --lon/--lat or the AOI centre) is decoded on a pixel grid and compared
     with the source DEM:
       * a transparent pixel where the source has data is a failure
         (catches sources overwriting each other and misplaced reads);
       * the median |tile - source| per zoom must stay within a tolerance
         scaled by the zoom's pixel size (--tolerance at full resolution).
         Fine zooms compare against the source sampled at the pixel centre
         (nearest/bilinear); coarse zooms against the source averaged over
         the pixel footprint.
  3. min/max elevation over the tiles OF THIS BUILD matches tilejson.json.
     Up to --max-scan tiles are decoded (all of them for small builds, an
     exact comparison); bigger pyramids are sampled and the sampled range
     must lie inside the tilejson range. Tiles outside the AOI's tile ranges
     (earlier builds of other areas in a shared pyramid dir) are ignored.

Usage:
  python verify_tiles.py --src data/*.tif --bbox 46.49 24.7 46.9 25.3 \\
                         --tiles public/terrarium [--minzoom 0 --maxzoom 13]
"""

import argparse
import io
import json
import math
import os
import random
import sys

import numpy as np
import rasterio
import rasterio.windows
from PIL import Image
from rasterio.enums import Resampling

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from build_tiles import (  # noqa: E402
    NODATA, TERRAIN_FORMATS, TILE_PX, SourceIndex, encode_terrarium, lonlat_to_tile,
    pixel_center_lonlat, save_terrarium, terrarium_tile_path, tile_range, validate_bbox,
)

__all__ = ["lonlat_to_tile", "tile_range", "main"]

EQUATOR_RES = 156543.03392800014  # m/px at z0
SOURCE_RES_M = 30.0
GRID = 8                          # sample pixels per tile edge


def decode_png(path):
    """Heights + (w, h) of a Terrarium tile (PNG or WebP, despite the name)."""
    with Image.open(path) as im:
        arr = np.asarray(im.convert("RGBA")).astype(np.int64)
    h, w, _ = arr.shape
    v = arr[..., 0] * 256 * 256 + arr[..., 1] * 256 + arr[..., 2]
    heights = v / 256.0 - 32768.0
    heights[arr[..., 3] == 0] = NODATA
    return heights, (w, h)


decode_tile = decode_png


def detect_format(tiles_dir, meta=None, zooms=()):
    """Terrarium format of a pyramid: tilejson "format", else 'webp' when any
    .webp tile exists under the given zooms, else 'png' (legacy builds)."""
    fmt = (meta or {}).get("format") if isinstance(meta, dict) else None
    if fmt in TERRAIN_FORMATS:
        return fmt
    for z in zooms:
        zdir = os.path.join(tiles_dir, str(z))
        for xs in (os.listdir(zdir) if os.path.isdir(zdir) else ()):
            xdir = os.path.join(zdir, xs)
            if os.path.isdir(xdir) and any(f.endswith(".webp") for f in os.listdir(xdir)):
                return "webp"
    return "png"


def _valid(v):
    return v is not None and math.isfinite(v) and v > -1000.0


def sample_source(ds, lon, lat):
    """Return (nearest, bilinear) source heights at a coordinate (NaN where
    the source has no data)."""
    inv = ~ds.transform
    fx = inv.a * lon + inv.b * lat + inv.c
    fy = inv.d * lon + inv.e * lat + inv.f
    col, row = int(math.floor(fx)), int(math.floor(fy))
    win = rasterio.windows.Window(col - 1, row - 1, 3, 3)
    nod = ds.nodata if ds.nodata is not None else NODATA
    arr = ds.read(1, window=win, boundless=True, fill_value=nod).astype(np.float64)
    arr[(arr < -1000.0) | (arr == nod)] = np.nan
    nearest = float(arr[1, 1])
    # bilinear around the 4 pixel centers surrounding the coordinate
    cx, cy = fx - 0.5, fy - 0.5
    x0, y0 = math.floor(cx), math.floor(cy)
    dx, dy = cx - x0, cy - y0
    ix, iy = int(x0) - (col - 1), int(y0) - (row - 1)   # into the 3x3 window
    q = [[arr[iy + j, ix + i] for i in (0, 1)] for j in (0, 1)]
    bilinear = ((1 - dx) * (1 - dy) * q[0][0] + dx * (1 - dy) * q[0][1]
                + (1 - dx) * dy * q[1][0] + dx * dy * q[1][1])
    return nearest, float(bilinear)


def footprint_mean(ds, lon0, lat0, lon1, lat1):
    """Mean source height over a lon/lat box (overview-aware decimated read)."""
    win = rasterio.windows.from_bounds(lon0, lat0, lon1, lat1, transform=ds.transform)
    try:
        win = rasterio.windows.intersection(
            win, rasterio.windows.Window(0, 0, ds.width, ds.height))
    except Exception:  # noqa: BLE001 - disjoint
        return math.nan
    k = max(1, min(16, int(math.ceil(min(win.width, win.height)))))
    arr = ds.read(1, window=win, out_shape=(k, k),
                  resampling=Resampling.average).astype(np.float64)
    v = arr[arr > -1000.0]
    return float(v.mean()) if v.size else math.nan


class Source:
    """Point lookups against all sources."""

    def __init__(self, paths):
        self.idx = SourceIndex(paths)

    def close(self):
        self.idx.close()

    def covering(self, lon, lat):
        for ds in self.idx.sources_for(lon, lat, lon, lat):
            b = ds.bounds
            if b.left <= lon < b.right and b.bottom < lat <= b.top:
                return ds
        return None

    def covered(self, lon, lat):
        return self.covering(lon, lat) is not None


def zoom_tolerance(base, z, lat):
    """Allowed median |tile - source| at zoom z: base at (or finer than) the
    30 m source resolution, growing with the pixel's ground size."""
    pixel_m = EQUATOR_RES / 2**z * math.cos(math.radians(lat))
    return base * max(1.0, pixel_m / SOURCE_RES_M) ** 0.5, pixel_m


def pick_tiles(bbox, z, n, rng, extra_points):
    """Sample of tiles of zoom z in the bbox: points on every 1-degree line
    crossing the bbox, the extra points, then random tiles up to n."""
    x0, y0, x1, y1 = tile_range(bbox, z)
    minlon, minlat, maxlon, maxlat = bbox
    chosen = []

    def add(t):
        if t not in chosen and x0 <= t[0] <= x1 and y0 <= t[1] <= y1:
            chosen.append(t)

    for lon, lat in extra_points:
        add(lonlat_to_tile(lon, lat, z))
    for L in range(math.ceil(minlon), math.floor(maxlon) + 1):
        for lat in (minlat + (maxlat - minlat) * f for f in (0.25, 0.5, 0.75)):
            add(lonlat_to_tile(L, lat, z))
    for L in range(math.ceil(minlat), math.floor(maxlat) + 1):
        for lon in (minlon + (maxlon - minlon) * f for f in (0.25, 0.5, 0.75)):
            add(lonlat_to_tile(lon, L, z))
    total = (x1 - x0 + 1) * (y1 - y0 + 1)
    if total <= n:
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                add((x, y))
    else:
        tries = 0
        while len(chosen) < n and tries < n * 20:
            add((rng.randint(x0, x1), rng.randint(y0, y1)))
            tries += 1
    return chosen


def check_zoom(src, tiles_dir, bbox, z, tiles, base_tol, rng, fmt="png"):
    """Returns dict with counts and error stats for one zoom."""
    res = {"tiles": 0, "missing": 0, "points": 0, "nodata_where_src": 0,
           "errors": [], "examples": []}
    for x, y in tiles:
        path = terrarium_tile_path(tiles_dir, z, x, y, fmt)
        grid = [(c, r) for r in np.linspace(2, TILE_PX - 3, GRID).astype(int)
                for c in np.linspace(2, TILE_PX - 3, GRID).astype(int)]
        jitter = [(min(TILE_PX - 1, c + rng.randint(0, 3)),
                   min(TILE_PX - 1, r + rng.randint(0, 3))) for c, r in grid]
        heights = None
        if os.path.exists(path):
            heights, _ = decode_png(path)
            res["tiles"] += 1
        for col, row in jitter:
            lon, lat = pixel_center_lonlat(x, y, z, col, row)
            # pixel corners must be covered too: the coverage edge itself is
            # legitimately fuzzy
            hl, hr = pixel_center_lonlat(x, y, z, col - 0.5, row - 0.5), \
                pixel_center_lonlat(x, y, z, col + 0.5, row + 0.5)
            if not (src.covered(*hl) and src.covered(*hr)
                    and src.covered(hl[0], hr[1]) and src.covered(hr[0], hl[1])):
                continue
            ds = src.covering(lon, lat)
            nearest, bilin = sample_source(ds, lon, lat)
            if not (_valid(nearest) and _valid(bilin)):
                continue
            res["points"] += 1
            if heights is None:
                res["missing"] += 1
                if len(res["examples"]) < 5:
                    res["examples"].append(f"missing tile {z}/{x}/{y}")
                break
            t = float(heights[row, col])
            if t <= NODATA + 1.0:
                res["nodata_where_src"] += 1
                if len(res["examples"]) < 5:
                    res["examples"].append(
                        f"transparent pixel {z}/{x}/{y} px({col},{row}) at "
                        f"({lat:.5f}, {lon:.5f}); source {nearest:.1f} m")
                continue
            tol, pixel_m = zoom_tolerance(base_tol, z, lat)
            if pixel_m <= 2 * SOURCE_RES_M:
                err = min(abs(t - nearest), abs(t - bilin))
            else:
                truth = footprint_mean(ds, hl[0], hr[1], hr[0], hl[1])
                if not _valid(truth):
                    continue
                err = abs(t - truth)
            res["errors"].append(err)
    return res


def roundtrip_check(fmt="png"):
    """Terrarium encoder round-trip (in memory) in the given tile format.
    Returns (max height error on valid pixels, ok) where ok requires alpha 0
    exactly on no-data, 255 elsewhere, AND the decoded RGBA bit-identical to
    the encoder's (lossless, RGB kept under transparent pixels)."""
    rng = np.random.default_rng(42)
    synthetic = rng.uniform(-400, 4800, (256, 256)).astype(np.float32)
    synthetic[0, :5] = NODATA
    synthetic[100:120, 30:90] = NODATA
    image = encode_terrarium(synthetic)
    buf = io.BytesIO()
    save_terrarium(image, buf, fmt)
    buf.seek(0)
    with Image.open(buf) as im:
        arr = np.asarray(im.convert("RGBA")).astype(np.int64)
    v = arr[..., 0] * 65536 + arr[..., 1] * 256 + arr[..., 2]
    rt = v / 256.0 - 32768.0
    valid = synthetic > NODATA + 1.0
    err = float(np.abs(rt[valid] - synthetic[valid]).max())
    alpha_ok = bool((arr[..., 3][~valid] == 0).all() and (arr[..., 3][valid] == 255).all())
    exact = bool(np.array_equal(arr, np.asarray(image).astype(np.int64)))
    return err, alpha_ok and exact


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", nargs="+", required=True)
    ap.add_argument("--tiles", default="public/terrarium")
    ap.add_argument("--bbox", nargs=4, type=float, default=None,
                    metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"))
    ap.add_argument("--lon", type=float, default=None,
                    help="extra sample point (default: AOI center)")
    ap.add_argument("--lat", type=float, default=None)
    ap.add_argument("--tolerance", type=float, default=5.0,
                    help="median |tile - source| allowed at full resolution (m); "
                         "scaled up with the pixel size at coarser zooms")
    ap.add_argument("--minzoom", type=int, default=None,
                    help="zooms of THIS build — scope checks to them")
    ap.add_argument("--maxzoom", type=int, default=None)
    ap.add_argument("--tiles-per-zoom", type=int, default=12,
                    help="sampled tiles per zoom (1-degree boundary tiles are added)")
    ap.add_argument("--max-scan", type=int, default=3000,
                    help="tiles decoded for the min/max stats check (sampled above this)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--terrain-format", choices=("auto",) + TERRAIN_FORMATS,
                    default="auto",
                    help="Terrarium tile format (auto: tilejson.json 'format', "
                         "else the files present)")
    args = ap.parse_args(argv)

    failures = 0
    rng = random.Random(args.seed)

    meta_path = os.path.join(os.path.dirname(args.tiles.rstrip("/\\")), "tilejson.json")
    meta, meta_err = None, None
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except (OSError, ValueError) as e:
            meta_err = e
    all_zooms = sorted(int(z) for z in os.listdir(args.tiles) if z.isdigit()) \
        if os.path.isdir(args.tiles) else []
    fmt = (args.terrain_format if args.terrain_format != "auto"
           else detect_format(args.tiles, meta, all_zooms))

    # 0) encoder round-trip within the Terrarium quantization step (1/256 m)
    err, alpha_ok = roundtrip_check(fmt)
    ok = err <= 1 / 256 + 1e-6 and alpha_ok
    print(f"encoder round-trip ({fmt}) max error: {err:.6f} m, nodata->alpha 0 + "
          f"bit-exact RGBA: {'yes' if alpha_ok else 'NO'} ({'PASS' if ok else 'FAIL'}, "
          f"quantization step {1/256:.4f} m)")
    failures += 0 if ok else 1

    src = Source(args.src)
    try:
        if args.bbox:
            bbox = list(validate_bbox(args.bbox))
        else:
            b = [ds.bounds for ds in src.idx.datasets]
            bbox = [min(v.left for v in b), min(v.bottom for v in b),
                    max(v.right for v in b), max(v.top for v in b)]
        if args.lon is not None and args.lat is not None:
            extra = [(args.lon, args.lat)]
        else:
            extra = [((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)]

        zooms = list(all_zooms)
        if args.minzoom is not None:
            zooms = [z for z in zooms if z >= args.minzoom]
        if args.maxzoom is not None:
            zooms = [z for z in zooms if z <= args.maxzoom]
        if not zooms:
            print(f"no zoom levels of this build under {args.tiles} "
                  f"(wanted {args.minzoom or 0}..{args.maxzoom or 'max'})")
            return 1
        if args.minzoom is not None and args.maxzoom is not None:
            absent = sorted(set(range(args.minzoom, args.maxzoom + 1)) - set(zooms))
            if absent:
                print(f"zoom levels missing from the pyramid: {absent} (FAIL)")
                failures += 1

        # 1) per-zoom accuracy + coverage
        mid_lat = (bbox[1] + bbox[3]) / 2
        for z in zooms:
            tiles = pick_tiles(bbox, z, args.tiles_per_zoom, rng, extra)
            r = check_zoom(src, args.tiles, bbox, z, tiles, args.tolerance, rng, fmt)
            tol, pixel_m = zoom_tolerance(args.tolerance, z, mid_lat)
            e = np.array(r["errors"]) if r["errors"] else np.array([np.nan])
            med = float(np.median(e))
            p95 = float(np.percentile(e, 95))
            bad = []
            if r["missing"]:
                bad.append(f"{r['missing']} missing tile(s) where the source has data")
            if r["nodata_where_src"]:
                bad.append(f"{r['nodata_where_src']} transparent pixel(s) where the "
                           f"source has data")
            if r["errors"] and med > tol:
                bad.append(f"median error {med:.2f} m > {tol:.2f} m")
            if not r["errors"] and not r["missing"] and not r["nodata_where_src"]:
                print(f"z{z}: no sample point with source data in {len(tiles)} tile(s) "
                      f"— skipped")
                continue
            status = "FAIL" if bad else "PASS"
            print(f"z{z}: {r['tiles']}/{len(tiles)} tiles, {r['points']} points, "
                  f"pixel {pixel_m:.0f} m, median |tile-source| {med:.2f} m, "
                  f"p95 {p95:.2f} m, tolerance {tol:.2f} m, transparent-with-data "
                  f"{r['nodata_where_src']} ({status})")
            for b in bad:
                print(f"    {b}")
            for ex in r["examples"]:
                print(f"    e.g. {ex}")
            failures += 1 if bad else 0
    finally:
        src.close()

    # 2) stats over THIS BUILD's tiles only (a shared pyramid may hold tiles
    #    from earlier builds of other areas)
    files, stale = [], 0
    ext = f".{fmt}"
    for z in zooms:
        zdir = os.path.join(args.tiles, str(z))
        x0, y0, x1, y1 = tile_range(bbox, z)
        for xs in os.listdir(zdir):
            if not xs.isdigit():
                continue
            xi = int(xs)
            names = [f for f in os.listdir(os.path.join(zdir, xs)) if f.endswith(ext)]
            if not x0 <= xi <= x1:
                stale += len(names)
                continue
            for f in names:
                stem = f[:-len(ext)]
                yi = int(stem) if stem.isdigit() else -1
                if y0 <= yi <= y1:
                    files.append(os.path.join(zdir, xs, f))
                else:
                    stale += 1
    exact = len(files) <= args.max_scan
    scan = files if exact else rng.sample(files, args.max_scan)
    gmin, gmax, count = math.inf, -math.inf, 0
    for p in scan:
        hh, _ = decode_png(p)
        v = hh[hh > NODATA + 1.0]
        if v.size:
            gmin, gmax = min(gmin, float(v.min())), max(gmax, float(v.max()))
            count += 1

    if meta_err is not None:
        print(f"tilejson.json unreadable: {meta_err} (FAIL)")
        failures += 1
    scope = "all" if exact else f"a sample of {len(scan)} of {len(files)}"
    stale_txt = f" (ignored {stale} stale tiles from earlier builds)" if stale else ""
    if count:
        print(f"pyramid: decoded {scope} tiles in AOI, elevation {gmin:.1f}..{gmax:.1f} m"
              + stale_txt)
    else:
        print("pyramid: no tiles in AOI" + stale_txt)
    if meta and isinstance(meta.get("statistics"), dict):
        st = meta["statistics"]
        print(f"tilejson statistics: {st}")
        try:
            if exact:
                ok = (count > 0 and abs(gmin - st["min"]) < 0.5
                      and abs(gmax - st["max"]) < 0.5)
            else:
                ok = count > 0 and gmin >= st["min"] - 0.5 and gmax <= st["max"] + 0.5
        except (KeyError, TypeError):
            ok = False
    else:
        if meta is None:
            print("tilejson.json not found — skipping stats comparison")
        ok = count > 0
    print(f"stats match{'' if exact else ' (sampled: range inside tilejson)'}: "
          f"{'PASS' if ok else 'FAIL'}")
    failures += 0 if ok else 1

    print(f"verify: {'FAIL' if failures else 'PASS'} ({failures} failed check(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
