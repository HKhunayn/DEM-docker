"""Convert a Copernicus DEM GeoTIFF (or several 1-degree tiles) into a
Web-Mercator XYZ pyramid of Terrarium-encoded PNG elevation tiles.

Terrarium encoding (used by Mapzen/AWS terrain tiles, MapLibre, deck.gl):
    height_m = (R * 256 + G + B / 256) - 32768
No-data pixels are transparent (alpha = 0) and decoded as noDataValue.

Usage:
  python build_tiles.py --src ../data/Copernicus_DSM_COG_10_N45_00_E006_00_DEM.tif \
      --bbox 6.6 45.6 7.2 46.0 --minzoom 8 --maxzoom 13 --out ../tiles/terrarium
"""

import argparse
import json
import math
import os

import numpy as np
import rasterio
from PIL import Image
from rasterio.crs import CRS
from rasterio.merge import merge
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject

NODATA = -32768.0
TILE_PX = 256
ORIGIN_SHIFT = 20037508.342789244  # half earth circumference in EPSG:3857 meters
MERC = CRS.from_epsg(3857)


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
    args = ap.parse_args()

    minlon, minlat, maxlon, maxlat = args.bbox

    # 1) mosaic the source DEM tiles (WGS84), clipped to the AOI bbox
    datasets = [rasterio.open(p) for p in args.src]
    mosaic, transform = merge(
        datasets,
        bounds=(minlon, minlat, maxlon, maxlat),
        resampling=Resampling.bilinear,
        nodata=NODATA,
    )
    src_arr = mosaic[0].astype(np.float32)
    src_arr[src_arr < -1000.0] = NODATA
    src_transform = transform
    src_crs = datasets[0].crs
    for ds in datasets:
        ds.close()

    tminlon, tminlat, tmaxlon, tmaxlat = rasterio.transform.array_bounds(
        src_arr.shape[0], src_arr.shape[1], src_transform
    )
    print(f"mosaic: {src_arr.shape[1]}x{src_arr.shape[0]} px, "
          f"bounds=({tminlon:.4f},{tminlat:.4f},{tmaxlon:.4f},{tmaxlat:.4f})")

    # 2) reproject mosaic into Web-Mercator XYZ Terrarium tiles
    total = 0
    gmin, gmax = math.inf, -math.inf
    for z in range(args.minzoom, args.maxzoom + 1):
        x0, y0 = lonlat_to_tile(minlon, maxlat, z)  # top-left
        x1, y1 = lonlat_to_tile(maxlon, minlat, z)  # bottom-right
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                dst = np.full((TILE_PX, TILE_PX), NODATA, np.float32)
                minx, miny, maxx, maxy = tile_merc_bounds(x, y, z)
                reproject(
                    source=src_arr,
                    destination=dst,
                    src_transform=src_transform,
                    src_crs=src_crs,
                    src_nodata=NODATA,
                    dst_transform=from_bounds(minx, miny, maxx, maxy, TILE_PX, TILE_PX),
                    dst_crs=MERC,
                    dst_nodata=NODATA,
                    resampling=Resampling.bilinear,
                )
                if not (dst > NODATA + 1.0).any():
                    continue  # empty tile outside the data footprint
                out_dir = os.path.join(args.out, str(z), str(x))
                os.makedirs(out_dir, exist_ok=True)
                encode_terrarium(dst).save(os.path.join(out_dir, f"{y}.png"), optimize=True)
                if args.hillshade_out:
                    hs_dir = os.path.join(args.hillshade_out, str(z), str(x))
                    os.makedirs(hs_dir, exist_ok=True)
                    cell_m = (maxx - minx) / TILE_PX
                    hillshade(dst, cell_m).save(os.path.join(hs_dir, f"{y}.png"),
                                                optimize=True)
                valid = dst[dst > NODATA + 1.0]
                gmin, gmax = min(gmin, float(valid.min())), max(gmax, float(valid.max()))
                total += 1
        print(f"z{z}: tiles x={x0}..{x1} y={y0}..{y1} (cumulative {total})")

    # 3) TileJSON metadata for the client
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
    print(f"WROTE {total} tiles z{args.minzoom}-{args.maxzoom}, "
          f"elevation range {gmin:.1f}..{gmax:.1f} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
