"""Build an ArcGIS-compatible elevation tile pyramid (LERC) from DEM COGs.

Windowed, resumable and parallel like build_tiles.py: each tile reads only
its own footprint from the sources (constant memory at any AOI size), tiles
already on disk are skipped unless --force is given (or the service dir's
build_manifest.json does not match this engine/sources/params), and encoding
runs in a memory-safe process pool (--workers).

build_tiles.py --lerc-out writes the pyramid in the same pass as the
Terrarium tiles (one reprojection per tile, on a grid with a 1-px apron that
is cropped: build_tiles.REPROJECT_BUFFER); this script is the standalone
LERC-only builder (no apron, so decimated low zooms can differ slightly).

Output layout (served by scripts/serve.py):
  <out>/arcgis/rest/services/<name>/ImageServer.json      <- service metadata
  <out>/arcgis/rest/services/<name>/tile/{z}/{row}/{col}  <- Lerc1 blobs (float32 meters)

The metadata mirrors the fields of Esri's WorldElevation3D/Terrain3D ImageServer
(fetched and checked field-by-field), so ANY ArcGIS client (ArcGIS Maps SDK for
JavaScript/Qt/.NET/iOS/Android, ArcGIS Pro, ArcGIS Earth) can consume it as a
plain ElevationLayer URL — no custom code.

Usage:
  python build_lerc.py --src dem1.tif [dem2.tif ...] --bbox 6.6 45.6 7.2 46.0 \\
      --maxzoom 13 --name local30m --out ../public
"""

import argparse
import atexit
import ctypes
import json
import os
import shutil
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
    NODATA, TILE_PX, BuildManifest, SourceIndex, _default_workers, build_tile,
    lerc_params, lerc_tile_path, source_fingerprint, source_stats, sweep_parts,
    tile_range, validate_bbox, write_atomic, write_json_atomic,
)
from pool_runner import PoolAborted, describe_limits, run_pool  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
LERC1ENC_TIMEOUT_S = float(os.environ.get("LERC1ENC_TIMEOUT", "60"))


def _find_lerc1enc() -> str:
    """Locate the bundled Lerc1 encoder: LERC1ENC env override, then the
    platform-named binary built by build_lerc1enc.bat (Windows) or
    build_lerc1enc.sh (Linux/macOS, used by the Docker image build)."""
    env = os.environ.get("LERC1ENC")
    if env:
        return env
    # a Windows .exe is useless on Linux (WSL interop fails inside Docker)
    names = ("lerc1enc.exe", "lerc1enc") if os.name == "nt" else ("lerc1enc",)
    for name in names:
        candidate = os.path.join(HERE, name)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(HERE, "lerc1enc.exe" if os.name == "nt" else "lerc1enc")


LERC1ENC = _find_lerc1enc()
LIB_NAMES = {"nt": ("lerc1enc.dll", "liblerc1enc.dll")}.get(
    os.name, ("liblerc1enc.dylib", "liblerc1enc.so") if sys.platform == "darwin"
    else ("liblerc1enc.so",))
LERC2_PROFILE = dict(
    driver="GTiff", width=TILE_PX, height=TILE_PX, count=1, dtype="float32",
    crs="EPSG:4326", transform=rasterio.transform.from_origin(0, TILE_PX, 1, 1),
    compress="lerc", tiled=True, blockxsize=TILE_PX, blockysize=TILE_PX,
    nodata=NODATA,
)


def _lib_candidates() -> list[str]:
    """LERC1LIB env (a path; "0"/"off"/"none" disables the library), then
    liblerc1enc next to this script, then next to the CLI."""
    env = os.environ.get("LERC1LIB")
    if env is not None and env.strip():
        return [] if env.strip().lower() in ("0", "off", "none", "false") else [env]
    dirs = [HERE, os.path.dirname(os.path.abspath(LERC1ENC))]
    out = []
    for d in dirs:
        for n in LIB_NAMES:
            c = os.path.join(d, n)
            if c not in out:
                out.append(c)
    return out


# ctypes handle, loaded lazily once per process (pool workers are spawned, so
# each loads its own copy on first use). False = tried and unavailable.
_LIB = None
LERC1_OK, LERC1_E_ENCODE, LERC1_E_ARGS, LERC1_E_SMALL, LERC1_E_NOMEM = range(5)


def _load_lib():
    global _LIB
    if _LIB is None:
        _LIB = False
        for path in _lib_candidates():
            if not os.path.exists(path):
                continue
            try:
                lib = ctypes.CDLL(os.path.abspath(path))
                fn = lib.lerc1_encode
            except (OSError, AttributeError):
                continue
            fn.restype = ctypes.c_int
            fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_double,
                           ctypes.c_float, ctypes.c_void_p, ctypes.c_size_t,
                           ctypes.POINTER(ctypes.c_size_t)]
            _LIB = fn
            break
    return _LIB or None


def lerc1_backend() -> str:
    """'library' (in-process ctypes), 'cli' (subprocess) or 'missing'."""
    if _load_lib():
        return "library"
    return "cli" if os.path.exists(LERC1ENC) else "missing"


def require_lerc1enc() -> None:
    if lerc1_backend() == "missing":
        raise RuntimeError(
            f"lerc1enc encoder missing ({LERC1ENC}; no liblerc1enc library) — run "
            f"scripts/build_lerc1enc.bat (Windows) or scripts/build_lerc1enc.sh "
            f"(Linux/macOS) first, or set LERC1ENC / LERC1LIB"
        )


_WORK_DIR: str | None = None


def _work_dir() -> str:
    """One scratch dir per process (not per tile), removed at exit."""
    global _WORK_DIR
    if _WORK_DIR is None or not os.path.isdir(_WORK_DIR):
        _WORK_DIR = tempfile.mkdtemp(prefix="lerc1enc-")
        atexit.register(shutil.rmtree, _WORK_DIR, True)
    return _WORK_DIR


def lerc1_blob(arr: np.ndarray, maxzerr: float, timeout: float | None = None) -> bytes:
    """Encode a float32 tile as a Lerc1 CntZImage blob: in-process through
    liblerc1enc (ctypes) when available, else via the lerc1enc CLI. Both run
    the same encoder (scripts/lerc1enc.cpp) and give identical bytes."""
    fn = _load_lib()
    if fn:
        return lerc1_blob_lib(arr, maxzerr, fn)
    return lerc1_blob_cli(arr, maxzerr, timeout)


def lerc1_blob_lib(arr: np.ndarray, maxzerr: float, fn=None) -> bytes:
    """Lerc1 blob from the shared library (no fork/exec, no temp files)."""
    fn = fn or _load_lib()
    if not fn:
        raise RuntimeError("liblerc1enc not available (set LERC1LIB)")
    a = np.ascontiguousarray(arr, dtype=np.float32)
    if a.ndim != 2:
        raise ValueError(f"lerc1: expected a 2-D array, got shape {a.shape}")
    rows, cols = a.shape
    # raw floats + bit mask + headers is the worst case; the encoder reports
    # the exact size if that is ever not enough
    cap = a.size * 4 + a.size // 8 + 4096
    for _ in range(2):
        out = ctypes.create_string_buffer(cap)
        n = ctypes.c_size_t(0)
        rc = fn(a.ctypes.data, cols, rows, float(maxzerr), float(NODATA), out, cap,
                ctypes.byref(n))
        if rc == LERC1_E_SMALL and n.value > cap:
            cap = n.value
            continue
        break
    if rc != LERC1_OK:
        raise RuntimeError(f"lerc1_encode failed with code {rc} "
                           f"({cols}x{rows}, maxzerr {maxzerr})")
    if not n.value:
        raise RuntimeError("lerc1_encode produced an empty blob")
    return ctypes.string_at(out, n.value)


def lerc1_blob_cli(arr: np.ndarray, maxzerr: float, timeout: float | None = None) -> bytes:
    """Lerc1 blob via the bundled CLI (built from GDAL's frmts/mrf/LERCV1 —
    see scripts/build_lerc1enc.bat). CLI: lerc1enc RAW_IN COLS ROWS OUT_FILE
    MAXZERR NODATA."""
    if not os.path.exists(LERC1ENC):
        raise RuntimeError(
            f"lerc1enc encoder missing ({LERC1ENC}) — run scripts/build_lerc1enc.bat "
            f"(Windows) or scripts/build_lerc1enc.sh (Linux/macOS) first, or set LERC1ENC")
    td = _work_dir()
    raw_path = os.path.join(td, "t.raw")
    out_path = os.path.join(td, "t.lerc1")
    np.ascontiguousarray(arr, dtype="<f4").tofile(raw_path)
    try:
        if os.path.exists(out_path):
            os.unlink(out_path)
        cmd = [LERC1ENC, raw_path, str(arr.shape[1]), str(arr.shape[0]), out_path,
               str(maxzerr), str(NODATA)]
        try:
            proc = subprocess.run(cmd, capture_output=True,
                                  timeout=timeout or LERC1ENC_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            err = (e.stderr or b"").decode(errors="replace").strip()
            raise RuntimeError(f"lerc1enc timed out after {e.timeout:g} s"
                               + (f": {err}" if err else "")) from None
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or b"").decode(errors="replace").strip()
            raise RuntimeError(f"lerc1enc exited with {proc.returncode}: "
                               f"{err or '(no output)'}")
        with open(out_path, "rb") as f:
            blob = f.read()
        if not blob:
            raise RuntimeError("lerc1enc produced an empty blob")
        return blob
    finally:
        for p in (raw_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


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


def encode_blob(arr: np.ndarray, flavor: str, maxzerr: float) -> bytes:
    return lerc1_blob(arr, maxzerr) if flavor == "lerc1" else lerc2_blob(arr)


def service_root(out: str, name: str) -> str:
    return os.path.join(out, "arcgis", "rest", "services", name)


COPYRIGHT = {
    "copernicus": "Copernicus DEM GLO-30 (ESA) — locally hosted elevation",
    "fabdem": "FABDEM V1-2 (University of Bristol / Fathom Global) — "
              "CC BY-NC-SA 4.0, non-commercial — locally hosted elevation",
}


def service_json(name, description, provider, minzoom, maxzoom, bbox, stats) -> dict:
    """ArcGIS ImageServer metadata (field-for-field compatible with
    WorldElevation3D/Terrain3D; lods limited to what we actually serve)."""
    minlon, minlat, maxlon, maxlat = bbox
    lods = []
    for level in range(minzoom, maxzoom + 1):
        res = 156543.03392800014 / 2**level
        lods.append({"level": level, "resolution": res, "scale": res * 96 * 37.7952755906})

    x0, y0, x1, y1 = transform_bounds("EPSG:4326", "EPSG:3857", minlon, minlat, maxlon, maxlat)
    sr = {"wkid": 102100, "latestWkid": 3857}
    return {
        "currentVersion": 11.5,
        "name": name,
        "description": description,
        "serviceDescription": description,
        "copyrightText": COPYRIGHT.get(provider, "Locally hosted elevation"),
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


def write_service_json(svc_root, **kw) -> str:
    path = os.path.join(svc_root, "ImageServer.json")
    write_json_atomic(path, service_json(**kw))
    return path


# ---- worker pool -----------------------------------------------------------

_IDX: SourceIndex | None = None


def _init_worker(src_paths):
    global _IDX
    _IDX = SourceIndex(src_paths)
    atexit.register(_IDX.close)


def _lerc_job(task):
    z, x, y, tile_root, force, flavor, maxzerr = task
    tile_path = lerc_tile_path(tile_root, z, x, y)
    if (not force) and os.path.exists(tile_path) and os.path.getsize(tile_path) > 0:
        return "skip", os.path.getsize(tile_path)
    dst = build_tile(_IDX, x, y, z)
    if dst is None:
        return "empty", 0
    blob = encode_blob(dst, flavor, maxzerr)
    write_atomic(tile_path, lambda f: f.write(blob))
    return "write", len(blob)


def main(argv=None) -> int:
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
                    help="build processes (default: sized from memory)")
    args = ap.parse_args(argv)
    try:
        args.bbox = list(validate_bbox(args.bbox))
    except ValueError as e:
        ap.error(str(e))

    workers = args.workers or _default_workers()
    if args.flavor == "lerc1":
        try:
            require_lerc1enc()
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1

    idx = SourceIndex(args.src)
    src_hash = source_fingerprint(args.src)
    print(f"sources: {len(idx.datasets)} DEM file(s) (fingerprint {src_hash}); "
          f"windowed LERC build, resume={'off' if args.force else 'on'}, "
          f"workers={workers}"
          + (f", lerc1 encoder={lerc1_backend()}" if args.flavor == "lerc1" else "")
          + f"; {describe_limits()}")
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

    svc_root = service_root(args.out, args.name)
    tile_root = os.path.join(svc_root, "tile")
    manifest = BuildManifest(
        svc_root, "lerc", lerc_params(args.flavor, args.maxzerr), source_hash=src_hash,
        bbox=args.bbox, minzoom=args.minzoom, maxzoom=args.maxzoom,
        user_force=args.force)
    sys.stdout.flush()
    removed = sweep_parts(tile_root)
    if removed:
        print(f"removed {removed} stale *.part temp file(s) under {tile_root}")
    written_total = skipped_total = failed_total = nbytes = 0

    for z in range(args.minzoom, args.maxzoom + 1):
        x0, y0, x1, y1 = tile_range(args.bbox, z)
        force = manifest.force_zoom(z)
        tasks = [(z, x, y, tile_root, force, args.flavor, args.maxzerr)
                 for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
        c = {"w": 0, "s": 0, "b": 0, "f": 0}

        def on_result(res, c=c):
            status, size = res
            c["b"] += size
            if status == "write":
                c["w"] += 1
            elif status == "skip":
                c["s"] += 1

        def on_error(_desc, _msg, c=c):
            c["f"] += 1

        try:
            workers = run_pool(_lerc_job, tasks, workers, _init_worker,
                               (list(args.src),), on_result, f"lerc z{z}",
                               on_error=on_error)
        except PoolAborted as e:
            print(f"ERROR: lerc z{z}: {e}. Build aborted.")
            return 1
        nbytes += c["b"]
        written_total += c["w"]
        skipped_total += c["s"]
        failed_total += c["f"]
        if c["f"] == 0:
            manifest.zoom_done(z)
        print(f"z{z}: x={x0}..{x1} y={y0}..{y1} written={c['w']} skipped={c['s']} "
              f"failed={c['f']} (cumulative written {written_total})")
        sys.stdout.flush()

    if failed_total:
        print(f"ERROR: {failed_total} LERC tile(s) failed (listed above); re-run to "
              f"retry them. ImageServer.json not updated.")
        return 1

    write_service_json(svc_root, name=args.name, description=args.description,
                       provider=args.provider, minzoom=args.minzoom,
                       maxzoom=args.maxzoom, bbox=args.bbox, stats=stats)
    manifest.finish()
    print(f"WROTE {written_total} LERC tiles, skipped {skipped_total} existing "
          f"({nbytes / 1e6:.2f} MB on disk) + ImageServer.json -> {svc_root}")
    print(f"SERVICE URL: {args.out}/arcgis/rest/services/{args.name} "
          f"(serve with: python ../terrainhub.py serve --root {args.out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
