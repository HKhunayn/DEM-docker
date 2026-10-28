"""Build an ArcGIS-compatible elevation tile pyramid (LERC) from DEM COGs.

Windowed, resumable and parallel like build_tiles.py: each tile reads only
its own footprint from the sources (constant memory at any AOI size), tiles
already on disk are skipped unless --force is given, and encoding runs in a
process pool (--workers, default: all CPUs).

Output layout (served by scripts/serve.py):
  <out>/arcgis/rest/services/<name>/ImageServer.json      <- service metadata
  <out>/arcgis/rest/services/<name>/tile/{z}/{row}/{col}  <- Lerc1 blobs (float32 meters)

The metadata mirrors the fields of Esri's WorldElevation3D/Terrain3D ImageServer
(fetched and checked field-by-field), so ANY ArcGIS client (ArcGIS Maps SDK for
JavaScript/Qt/.NET/iOS/Android, ArcGIS Pro, ArcGIS Earth) can consume it as a
plain ElevationLayer URL — no custom code.

Usage:
  python build_lerc.py --src dem1.tif [dem2.tif ...] --bbox 6.6 45.6 7.2 46.0 \
      --maxzoom 13 --name local30m --out ../public
"""

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile

# Cap GDAL's block cache per process (MB) before rasterio/GDAL initialize —
# the default is 5% of RAM, which OOMs the VM when many worker processes
# each open hundreds of COGs.
os.environ.setdefault("GDAL_CACHEMAX", "128")

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.warp import transform_bounds

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from build_tiles import (  # noqa: E402
    NODATA, TILE_PX, SourceIndex, _default_workers, build_tile,
    lonlat_to_tile, source_stats,
)
from pool_runner import describe_limits, run_pool  # noqa: E402

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


# ---- worker pool -----------------------------------------------------------

_IDX: SourceIndex | None = None


def _init_worker(src_paths):
    global _IDX
    _IDX = SourceIndex(src_paths)
    import atexit
    atexit.register(_IDX.close)


def _lerc_job(task):
    z, x, y, tile_root, force, flavor, maxzerr = task
    tile_path = os.path.join(tile_root, str(z), str(y), str(x))
    if (not force) and os.path.exists(tile_path) and os.path.getsize(tile_path) > 0:
        return "skip", os.path.getsize(tile_path)
    dst = build_tile(_IDX, x, y, z)
    if dst is None:
        return "empty", 0
    blob = lerc1_blob(dst, maxzerr) if flavor == "lerc1" else lerc2_blob(dst)
    os.makedirs(os.path.dirname(tile_path), exist_ok=True)
    tmp_path = tile_path + ".part"
    with open(tmp_path, "wb") as f:
        f.write(blob)
    os.replace(tmp_path, tile_path)
    return "write", len(blob)


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
    ap.add_argument("--force", action="store_true",
                    help="re-encode tiles even when they already exist on disk")
    ap.add_argument("--workers", type=int, default=0,
                    help="build processes (default: number of CPUs)")
    args = ap.parse_args()

    minlon, minlat, maxlon, maxlat = args.bbox
    workers = args.workers or _default_workers()

    idx = SourceIndex(args.src)
    print(f"sources: {len(idx.datasets)} DEM file(s); windowed LERC build, "
          f"resume={'off' if args.force else 'on'}, workers={workers}; "
          f"{describe_limits()}")
    sys.stdout.flush()

    # Statistics: prefer the Terrarium tilejson written by build_tiles for the
    # same footprint (exact min/max); fall back to overview reads of the sources.
    overview = source_stats(idx)
    tj_path = os.path.join(args.out, "tilejson.json")
    tj = None
    if os.path.exists(tj_path):
        try:
            with open(tj_path) as f:
                tj = json.load(f).get("statistics")
        except Exception:  # noqa: BLE001
            tj = None
    if tj and overview and all(k in tj for k in ("min", "max")):
        stats = dict(min=float(tj["min"]), max=float(tj["max"]),
                     mean=overview["mean"], stdv=overview["stdv"])
    elif overview:
        stats = overview
    else:
        print("ERROR: no elevation data found in the sources", file=sys.stderr)
        return 1
    print(f"elevation {stats['min']:.1f}..{stats['max']:.1f} m "
          f"(mean {stats['mean']:.1f}, stdv {stats['stdv']:.1f})")
    sys.stdout.flush()
    idx.close()

    svc_root = os.path.join(args.out, "arcgis", "rest", "services", args.name)
    tile_root = os.path.join(svc_root, "tile")
    written_total = skipped_total = nbytes = 0

    for z in range(args.minzoom, args.maxzoom + 1):
        x0, y0 = lonlat_to_tile(minlon, maxlat, z)
        x1, y1 = lonlat_to_tile(maxlon, minlat, z)
        tasks = [(z, x, y, tile_root, args.force, args.flavor, args.maxzerr)
                 for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
        c = {"w": 0, "s": 0, "b": 0}

        def on_result(res, c=c):
            status, size = res
            c["b"] += size
            if status == "write":
                c["w"] += 1
            elif status == "skip":
                c["s"] += 1

        workers = run_pool(_lerc_job, tasks, workers, _init_worker,
                           (list(args.src),), on_result, f"lerc z{z}")
        nbytes += c["b"]
        written_total += c["w"]
        skipped_total += c["s"]
        print(f"z{z}: x={x0}..{x1} y={y0}..{y1} written={c['w']} skipped={c['s']} "
              f"(cumulative written {written_total})")
        sys.stdout.flush()

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

    print(f"WROTE {written_total} LERC tiles, skipped {skipped_total} existing "
          f"({nbytes / 1e6:.2f} MB on disk) + ImageServer.json -> {svc_root}")
    print(f"SERVICE URL: {args.out}/arcgis/rest/services/{args.name} "
          f"(serve with: python ../terrainhub.py serve --root {args.out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())