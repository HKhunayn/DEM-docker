"""Verify the Terrarium tile pyramid against the source DEM.

Checks (all scoped to the AOI being verified — nothing is hard-coded to a
particular mountain):
  1. PNG encode/decode round-trip is lossless within the quantization step.
  2. The decoded height at a sample point inside the AOI (default: bbox center)
     matches the source GeoTIFF at the same coordinate (nearest + bilinear).
  3. min/max elevation over the tiles OF THIS BUILD matches tilejson.json.
     Tiles outside the AOI's tile ranges (left over from earlier builds of
     other areas in the shared pyramid dir) are ignored and counted.

Usage:
  python verify_tiles.py --src data/*.tif --bbox 24.7 46.49 25.3 46.9 \\
                         --tiles public/terrarium
"""

import argparse
import json
import math
import os
import sys

import numpy as np
import rasterio
from PIL import Image

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from build_tiles import NODATA, encode_terrarium  # noqa: E402


def lonlat_to_tile(lon, lat, z):
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
    return x, y


def decode_png(path):
    arr = np.asarray(Image.open(path).convert("RGBA")).astype(np.int64)
    h, w, _ = arr.shape
    v = arr[..., 0] * 256 * 256 + arr[..., 1] * 256 + arr[..., 2]
    heights = v / 256.0 - 32768.0
    heights[arr[..., 3] == 0] = -32768.0
    return heights, (w, h)


def sample_source(ds, lon, lat):
    """Return (nearest, bilinear) source heights at a coordinate."""
    fx, fy = ~ds.transform * (lon, lat)
    col, row = int(math.floor(fx)), int(math.floor(fy))
    win = rasterio.windows.Window(col - 1, row - 1, 3, 3)
    nod = ds.nodata if ds.nodata is not None else NODATA
    arr = ds.read(1, window=win, boundless=True, fill_value=nod).astype(np.float64)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", nargs="+", required=True)
    ap.add_argument("--tiles", default="public/terrarium")
    ap.add_argument("--bbox", nargs=4, type=float, default=None,
                    metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"))
    ap.add_argument("--lon", type=float, default=None,
                    help="sample point (default: AOI center)")
    ap.add_argument("--lat", type=float, default=None)
    ap.add_argument("--tolerance", type=float, default=5.0)
    ap.add_argument("--minzoom", type=int, default=None,
                    help="zooms of THIS build — scope checks to them")
    ap.add_argument("--maxzoom", type=int, default=None)
    args = ap.parse_args()

    failures = 0

    # 0) encoder round-trip: encode used by build_tiles must decode back within
    #    the Terrarium quantization step (1/256 m)
    rng = np.random.default_rng(42)
    synthetic = rng.uniform(-400, 4800, (256, 256)).astype(np.float32)
    synthetic[0, 0] = NODATA
    tmp_png = os.path.join(args.tiles, "..", "_roundtrip.png")
    os.makedirs(os.path.dirname(tmp_png), exist_ok=True)
    encode_terrarium(synthetic).save(tmp_png)
    rt, _ = decode_png(tmp_png)
    valid = synthetic > NODATA + 1.0
    err = float(np.abs(rt[valid] - synthetic[valid]).max())
    os.remove(tmp_png)
    ok = err <= 1 / 256 + 1e-6
    print(f"encoder round-trip max error: {err:.6f} m "
          f"({'PASS' if ok else 'FAIL'}, quantization step {1/256:.4f} m)")
    failures += 0 if ok else 1

    # sample point: explicit, else AOI center, else source bounds center
    if args.lon is not None and args.lat is not None:
        lon, lat = args.lon, args.lat
    elif args.bbox:
        lon = (args.bbox[0] + args.bbox[2]) / 2
        lat = (args.bbox[1] + args.bbox[3]) / 2
    else:
        with rasterio.open(args.src[0]) as ds:
            lon = (ds.bounds.left + ds.bounds.right) / 2
            lat = (ds.bounds.bottom + ds.bounds.top) / 2

    # 1) deepest zoom OF THIS BUILD, tile at the sample point
    zooms = [int(z) for z in os.listdir(args.tiles) if z.isdigit()]
    if args.minzoom is not None:
        zooms = [z for z in zooms if z >= args.minzoom]
    if args.maxzoom is not None:
        zooms = [z for z in zooms if z <= args.maxzoom]
    if not zooms:
        print(f"no zoom levels of this build under {args.tiles} "
              f"(wanted {args.minzoom or 0}..{args.maxzoom or 'max'})")
        return 1
    zoom = max(zooms)
    x, y = lonlat_to_tile(lon, lat, zoom)
    tile_path = os.path.join(args.tiles, str(zoom), str(x), f"{y}.png")
    if not os.path.exists(tile_path):
        print(f"sample tile missing: {tile_path} (sample point outside the build?)")
        failures += 1
        tile_h = None
    else:
        heights, (w, h) = decode_png(tile_path)
        n = 2**zoom
        wx = (lon + 180.0) / 360.0 * n * w
        lat_r = math.radians(lat)
        wy = (1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n * h
        px, py = int(wx - x * w), int(wy - y * h)
        px, py = min(max(px, 0), w - 1), min(max(py, 0), h - 1)
        tile_h = float(heights[py, px])
        # the tile value describes the pixel CENTER — evaluate the source there
        # too, so the comparison measures encoder error, not point-vs-center offset
        gx, gy = x * w + px + 0.5, y * h + py + 0.5
        lon = gx / (n * w) * 360.0 - 180.0
        lat = math.degrees(2 * math.atan(math.exp(math.pi * (1 - 2 * gy / (n * h))))
                           - math.pi / 2)
        print(f"tile z{zoom} {tile_path}: {w}x{h}, decoded height at pixel "
              f"center ({lat:.5f}, {lon:.5f}) = {tile_h:.2f} m")

    # 2) source value at the same point (nearest + bilinear; the pyramid is
    #    built with bilinear resampling, so either may legitimately match)
    src_h = src_b = None
    for p in args.src:
        with rasterio.open(p) as ds:
            if (ds.bounds.left <= lon <= ds.bounds.right
                    and ds.bounds.bottom <= lat <= ds.bounds.top):
                src_h, src_b = sample_source(ds, lon, lat)
                break
    if src_h is None:
        print(f"no source tile covers the sample point ({lat:.4f}, {lon:.4f})")
        failures += 1
    elif tile_h is not None:
        print(f"source DEM height: nearest {src_h:.2f} m, bilinear {src_b:.2f} m")
        diff = min(abs(tile_h - src_h), abs(tile_h - src_b))
        ok = diff < args.tolerance
        print(f"difference: {diff:.3f} m "
              f"({'PASS' if ok else 'FAIL'} at {args.tolerance:g} m tolerance)")
        failures += 0 if ok else 1

    # 3) stats over THIS BUILD's tiles only (shared pyramid may hold tiles
    #    from earlier builds of other areas)
    ranges = {}
    if args.bbox:
        for z in zooms:
            x0, y1 = lonlat_to_tile(args.bbox[0], args.bbox[1], z)
            x1, y0 = lonlat_to_tile(args.bbox[2], args.bbox[3], z)
            ranges[z] = (range(x0, x1 + 1), range(y0, y1 + 1))

    gmin, gmax, count, stale = math.inf, -math.inf, 0, 0
    for z in zooms:
        zdir = os.path.join(args.tiles, str(z))
        xr, yr = ranges.get(z, (None, None))
        for xs in os.listdir(zdir):
            if not xs.isdigit():
                continue
            xi = int(xs)
            if xr is not None and xi not in xr:
                stale += sum(1 for f in os.listdir(os.path.join(zdir, xs))
                             if f.endswith(".png"))
                continue
            for f in os.listdir(os.path.join(zdir, xs)):
                if not f.endswith(".png"):
                    continue
                yi = int(f[:-4])
                if yr is not None and yi not in yr:
                    stale += 1
                    continue
                hh, _ = decode_png(os.path.join(zdir, xs, f))
                v = hh[hh > -32767.0]
                if v.size:
                    gmin, gmax = min(gmin, float(v.min())), max(gmax, float(v.max()))
                    count += 1

    meta_path = os.path.join(os.path.dirname(args.tiles.rstrip("/\\")), "tilejson.json")
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else None
    print(f"pyramid: {count} tiles in AOI, elevation {gmin:.1f}..{gmax:.1f} m"
          + (f" (ignored {stale} stale tiles from earlier builds)" if stale else ""))
    if meta:
        print(f"tilejson statistics: {meta['statistics']}")
        ok = (count > 0
              and abs(gmin - meta["statistics"]["min"]) < 0.5
              and abs(gmax - meta["statistics"]["max"]) < 0.5)
    else:
        print("tilejson.json not found — skipping stats comparison")
        ok = count > 0
    print(f"stats match: {'PASS' if ok else 'FAIL'}")
    failures += 0 if ok else 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
