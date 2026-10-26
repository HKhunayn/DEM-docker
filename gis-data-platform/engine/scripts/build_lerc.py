"""Build an ArcGIS-compatible elevation tile pyramid (LERC) from a DEM mosaic.

Output layout (served by scripts/serve.py):
  <out>/arcgis/rest/services/<name>/ImageServer.json      <- service metadata
  <out>/arcgis/rest/services/<name>/tile/{z}/{row}/{col}  <- Lerc2 blobs (float32 meters)

The metadata mirrors the fields of Esri's WorldElevation3D/Terrain3D ImageServer
(fetched and checked field-by-field), so ANY ArcGIS client (ArcGIS Maps SDK for
JavaScript/Qt/.NET/iOS/Android, ArcGIS Pro, ArcGIS Earth) can consume it as a
plain ElevationLayer URL — no custom code.

LERC encoding is done with GDAL (libLERC, Apache-2.0): each tile is written as a
single-tile GeoTIFF with COMPRESS=LERC and the raw Lerc2 blob is carved out.

Usage:
  python build_lerc.py --src dem1.tif [dem2.tif ...] --bbox 6.6 45.6 7.2 46.0 \
      --maxzoom 13 --name local30m --out ../public
"""

import argparse
import json
import math
import os
import struct
import subprocess
import sys
import tempfile

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.io import MemoryFile
from rasterio.merge import merge
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from build_tiles import (  # noqa: E402
    NODATA, MERC, ORIGIN_SHIFT, TILE_PX, lonlat_to_tile, tile_merc_bounds,
)

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_lerc1enc() -> str:
    """Locate the bundled Lerc1 encoder: LERC1ENC env override, then the
    platform-named binary built by build_lerc1enc.bat (Windows) or
    build_lerc1enc.sh (Linux/macOS, used by the Docker image build)."""
    env = os.environ.get("LERC1ENC")
    if env:
        return env
    names = ("lerc1enc.exe", "lerc1enc") if os.name == "nt" else ("lerc1enc", "lerc1enc.exe")
    for name in names:
        candidate = os.path.join(HERE, name)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(HERE, "lerc1enc.exe" if os.name == "nt" else "lerc1enc")


LERC1ENC = _find_lerc1enc()
LERC2_PROFILE = dict(
    driver="GTiff", width=TILE_PX, height=TILE_PX, count=1, dtype="float32",
    crs="EPSG:4326", transform=rasterio.transform.from_origin(0, TILE_PX, 1, 1),
    compress="lerc", tiled=True, blockxsize=TILE_PX, blockysize=TILE_PX,
    nodata=NODATA,
)


def lerc1_blob(arr: np.ndarray, maxzerr: float) -> bytes:
    """Encode a float32 tile as a Lerc1 CntZImage blob via the bundled CLI
    (built from GDAL's frmts/mrf/LERCV1 — see scripts/build_lerc1enc.bat)."""
    if not os.path.exists(LERC1ENC):
        raise RuntimeError(
            "lerc1enc encoder missing — run scripts/build_lerc1enc.bat (Windows) "
            "or scripts/build_lerc1enc.sh (Linux/macOS) first"
        )
    with tempfile.TemporaryDirectory() as td:
        raw_path = os.path.join(td, "t.raw")
        out_path = os.path.join(td, "t.lerc1")
        arr.astype("<f4").tofile(raw_path)
        subprocess.run(
            [LERC1ENC, raw_path, str(arr.shape[1]), str(arr.shape[0]), out_path,
             str(maxzerr), str(NODATA)],
            check=True, capture_output=True,
        )
        with open(out_path, "rb") as f:
            return f.read()


def lerc2_blob(arr: np.ndarray) -> bytes:
    """Fallback: GDAL Lerc2 blob (carved from a single-tile GeoTIFF). NOTE: the
    ArcGIS Maps SDK for JavaScript's elevation decoder is Lerc1-only; Lerc2 is
    kept for future clients/tools that accept it."""
    with MemoryFile() as mem:
        with mem.open(**LERC2_PROFILE) as ds:
            ds.write(arr.astype(np.float32), 1)
        raw = mem.read()
    off = raw.find(b"Lerc2 ")
    if off < 0:
        raise RuntimeError("GDAL did not produce a LERC2 payload")
    return raw[off:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", nargs="+", required=True)
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"))
    ap.add_argument("--minzoom", type=int, default=0)
    ap.add_argument("--maxzoom", type=int, default=13)
    ap.add_argument("--provider", default="copernicus",
                    choices=["copernicus", "fabdem"],
                    help="source provider, for the service copyright text")
    ap.add_argument("--name", default="local30m")
    ap.add_argument("--out", default="public")
    ap.add_argument("--description", default="Local 30 m heightmap (Copernicus DEM GLO-30)")
    ap.add_argument("--flavor", choices=["lerc1", "lerc2"], default="lerc1",
                    help="lerc1 (CntZImage) = what ArcGIS clients decode; lerc2 = experimental")
    ap.add_argument("--maxzerr", type=float, default=0.01,
                    help="Lerc1 quantization bound in meters (0 = lossless raw floats)")
    args = ap.parse_args()

    minlon, minlat, maxlon, maxlat = args.bbox

    datasets = [rasterio.open(p) for p in args.src]
    mosaic, transform = merge(datasets, bounds=(minlon, minlat, maxlon, maxlat),
                             resampling=Resampling.bilinear, nodata=NODATA)
    src_arr = mosaic[0].astype(np.float32)
    src_arr[src_arr < -1000.0] = NODATA
    src_crs = datasets[0].crs
    for ds in datasets:
        ds.close()

    valid = src_arr > NODATA + 1.0
    v = src_arr[valid]
    stats = dict(min=float(v.min()), max=float(v.max()),
                 mean=float(v.mean()), stdv=float(v.std()))
    print(f"mosaic {src_arr.shape[1]}x{src_arr.shape[0]} px, "
          f"elevation {stats['min']:.1f}..{stats['max']:.1f} m")

    svc_root = os.path.join(args.out, "arcgis", "rest", "services", args.name)
    tile_root = os.path.join(svc_root, "tile")
    total, nbytes = 0, 0

    for z in range(args.minzoom, args.maxzoom + 1):
        x0, y0 = lonlat_to_tile(minlon, maxlat, z)
        x1, y1 = lonlat_to_tile(maxlon, minlat, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                dst = np.full((TILE_PX, TILE_PX), NODATA, np.float32)
                minx, miny, maxx, maxy = tile_merc_bounds(x, y, z)
                reproject(
                    source=src_arr, destination=dst,
                    src_transform=transform, src_crs=src_crs, src_nodata=NODATA,
                    dst_transform=from_bounds(minx, miny, maxx, maxy, TILE_PX, TILE_PX),
                    dst_crs=MERC, dst_nodata=NODATA, resampling=Resampling.bilinear,
                )
                if not (dst > NODATA + 1.0).any():
                    continue
                blob = (lerc1_blob(dst, args.maxzerr) if args.flavor == "lerc1"
                        else lerc2_blob(dst))
                out_dir = os.path.join(tile_root, str(z), str(y))
                os.makedirs(out_dir, exist_ok=True)
                with open(os.path.join(out_dir, str(x)), "wb") as f:
                    f.write(blob)
                total += 1
                nbytes += len(blob)
        print(f"z{z}: x={x0}..{x1} y={y0}..{y1} (cumulative {total})")

    # ---- ArcGIS ImageServer metadata (field-for-field compatible with
    #      WorldElevation3D/Terrain3D; lods limited to what we actually serve)
    lods = []
    for level in range(args.minzoom, args.maxzoom + 1):
        res = 156543.03392800014 / 2**level
        lods.append({"level": level, "resolution": res, "scale": res * 96 * 37.7952755906})

    x0, y0, x1, y1 = transform_bounds("EPSG:4326", "EPSG:3857", minlon, minlat, maxlon, maxlat)
    sr = {"wkid": 102100, "latestWkid": 3857}
    meta = {
        "currentVersion": 11.5,
        "name": args.name,
        "description": args.description,
        "serviceDescription": args.description,
        "copyrightText": {
            "copernicus": "Copernicus DEM GLO-30 (ESA) — locally hosted elevation",
            "fabdem": "FABDEM V1-2 (University of Bristol / Fathom Global) — "
                      "CC BY-NC-SA 4.0, non-commercial — locally hosted elevation",
        }.get(args.provider, "Locally hosted elevation"),
        "capabilities": "Image,Tilemap,Mensuration",
        "serviceDataType": "esriImageServiceDataTypeElevation",
        "pixelType": "F32",
        "bandCount": 1,
        "minValues": [stats["min"]],
        "maxValues": [stats["max"]],
        "meanValues": [stats["mean"]],
        "stdvValues": [stats["stdv"]],
        "pixelSizeX": lods[-1]["resolution"],
        "pixelSizeY": lods[-1]["resolution"],
        "noDataValue": NODATA,
        "supportedImageFormatTypes": "LERC",
        "allowRasterFunction": False,
        "exportTilesAllowed": False,
        "maxRecordCount": 1,
        "spatialReference": sr,
        "fullExtent": {"xmin": x0, "ymin": y0, "xmax": x1, "ymax": y1, "spatialReference": sr},
        "initialExtent": {"xmin": x0, "ymin": y0, "xmax": x1, "ymax": y1, "spatialReference": sr},
        "tileInfo": {
            "rows": TILE_PX, "cols": TILE_PX, "dpi": 96, "format": "LERC",
            "compressionQuality": 0,
            "origin": {"x": -20037508.342787, "y": 20037508.342787},
            "spatialReference": sr,
            "lods": lods,
        },
    }
    os.makedirs(svc_root, exist_ok=True)
    with open(os.path.join(svc_root, "ImageServer.json"), "w") as f:
        json.dump(meta, f, indent=2)

    print(f"WROTE {total} LERC tiles ({nbytes / 1e6:.2f} MB) + ImageServer.json -> {svc_root}")
    print(f"SERVICE URL: {args.out}/arcgis/rest/services/{args.name} "
          f"(serve with: python ../terrainhub.py serve --root {args.out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
