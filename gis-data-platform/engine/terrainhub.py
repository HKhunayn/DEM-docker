"""heightmap engine — fetch 30 m elevation data, store it locally, serve it to
any ArcGIS client (plus MapLibre/deck.gl/QGIS via the Terrarium pyramid).

Subcommands (all local, all open source):
  fetch    download Copernicus DEM COG tiles for a bbox (ESA open data)
  build    DEM -> Terrarium (lossless WebP) pyramid + ArcGIS LERC ImageServer
           pyramid (no hillshade: browsers shade the elevation tiles)
  verify   encoder round-trip + ground-truth checks
  serve    local server: ArcGIS REST elevation service + terrarium tiles
  publish  bake the current build into a static, seamless tile set
           (data/tiles/published/<version>/) that nginx serves directly. The
           raw Terrarium tiles MOVE into it (single copy on disk; --keep-raw
           keeps them) and only the new version is kept
  all      fetch + build + verify + publish in one go

Examples:
  python terrainhub.py all --bbox 6.6 45.6 7.2 46.0 --name local30m
  python terrainhub.py serve --port 8765
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import fetch_dem, fetch_fabdem, build_tiles, verify_tiles, serve, publish_tiles  # noqa: E402

# Single data root: gis-data-platform owns storage, this project is only the
# build engine. Sources land in <root>/dem, pyramids in <root>/tiles.
PLATFORM_DATA = os.environ.get(
    "GIS_DATA_ROOT",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "Documents", "DEM", "gis-data-platform", "data"),
)
if not os.path.isdir(PLATFORM_DATA):
    PLATFORM_DATA = os.path.join(
        os.environ.get("USERPROFILE", os.path.expanduser("~")),
        "Documents", "DEM", "gis-data-platform", "data",
    )
DATA_DIR = os.path.join(PLATFORM_DATA, "dem")
TILE_DIR = os.path.join(PLATFORM_DATA, "tiles")
PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")


def run(module_main, argv):
    old = sys.argv
    sys.argv = [module_main.__module__] + argv
    try:
        return module_main()
    finally:
        sys.argv = old


def bbox_src_files(bbox, res=30, out="data"):
    """Resolve the local COG files covering a bbox (after `fetch`)."""
    import os
    minlon, minlat, maxlon, maxlat = bbox
    files = []
    for lat in range(int(minlat // 1), int(maxlat // 1) + 1):
        for lon in range(int(minlon // 1), int(maxlon // 1) + 1):
            folder = fetch_dem.tile_folder(res, lat, lon)
            p = os.path.join(out, f"{folder}.tif")
            if os.path.exists(p):
                files.append(p)
    return files


def _name_cell(path):
    """(lat0, lon0) of a 1-degree DEM tile from its Copernicus/FABDEM file
    name (..._N24_00_E046_00_... or N24E046...), or None."""
    import re
    base = os.path.basename(path).upper()
    m = re.search(r"([NS])(\d{2})(?:_00)?_?([EW])(\d{3})", base)
    if not m:
        return None
    lat = int(m.group(2)) * (1 if m.group(1) == "N" else -1)
    lon = int(m.group(4)) * (1 if m.group(3) == "E" else -1)
    return lat, lon


def build_cover(bbox, minzoom):
    """lon/lat extent of the tiles a build touches: the tile range at the
    coarsest zoom covers every finer zoom."""
    x0, y0, x1, y1 = build_tiles.tile_range(bbox, max(0, minzoom))
    lon0, _, _, lat1 = build_tiles.tile_lonlat_bounds(x0, y0, max(0, minzoom))
    _, lat0, lon1, _ = build_tiles.tile_lonlat_bounds(x1, y1, max(0, minzoom))
    return lon0, max(lat0, -90.0), lon1, min(lat1, 90.0)


def files_in_bbox(files, bbox):
    """Source files whose footprint intersects the bbox (plus a small margin
    so tiles at the bbox edge still see their neighbours). Uses the file name
    when it encodes the 1-degree cell, else the raster bounds."""
    minlon, minlat, maxlon, maxlat = bbox
    m = 0.05
    out = []
    for p in files:
        cell = _name_cell(p)
        if cell is not None:
            b = (cell[1], cell[0], cell[1] + 1, cell[0] + 1)
        else:
            try:
                import rasterio
                with rasterio.open(p) as ds:
                    b = tuple(ds.bounds)
            except Exception:  # noqa: BLE001 - unreadable: let the build report it
                out.append(p)
                continue
        if b[0] <= maxlon + m and b[2] >= minlon - m and b[1] <= maxlat + m and b[3] >= minlat - m:
            out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--bbox", nargs=4, type=float, required=True,
                        metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"))
    common.add_argument("--res", type=int, default=30, choices=[30, 90])
    common.add_argument("--provider", default="copernicus",
                        choices=["copernicus", "fabdem"],
                        help="copernicus = GLO-30 DSM (free); fabdem = bare-earth DTM (CC BY-NC-SA)")
    common.add_argument("--minzoom", type=int, default=0)
    common.add_argument("--maxzoom", type=int, default=13)
    common.add_argument("--name", default="local30m")
    common.add_argument("--data", default=DATA_DIR)
    common.add_argument("--root", default=TILE_DIR)

    for c in ("fetch", "build", "verify", "publish", "all"):
        sp = sub.add_parser(c, parents=[common])
        if c == "fetch":
            sp.add_argument("--out", default=DATA_DIR, help="source DEM output dir")
            sp.add_argument("--check", action="store_true",
                            help="report cached vs missing source tiles without downloading")
        if c in ("build", "all"):
            sp.add_argument("--force", action="store_true",
                            help="rebuild tiles even when they already exist on disk")
        if c == "build":
            sp.add_argument("--lerc-only", action="store_true",
                            help="rebuild only the ArcGIS LERC service of this area; the "
                                 "published Terrarium set is left alone")
        if c in ("publish", "all"):
            sp.add_argument("--keep-raw", action="store_true",
                            help="keep the raw terrarium/ pyramid after publishing "
                                 "(dev/tests; default: deleted, the published set "
                                 "is the only copy)")

    sub.add_parser("status", parents=[common],
                   help="alias for `fetch --check`: show what is already downloaded")

    sp = sub.add_parser("serve")
    sp.add_argument("--root", default=PUBLIC_DIR, help="static UI pages (public/)")
    sp.add_argument("--tiles", default=TILE_DIR, help="tile data root (data/tiles)")
    sp.add_argument("--port", type=int, default=8765)

    args = ap.parse_args()
    bbox = getattr(args, "bbox", None)
    if bbox is not None:
        try:
            bbox = list(build_tiles.validate_bbox(bbox))
        except ValueError as e:
            ap.error(str(e))

    if args.cmd == "serve":
        return run(serve.main, ["--root", args.root, "--tiles", args.tiles,
                               "--port", str(args.port)])

    def fetch():
        check = ["--check"] if getattr(args, "check", False) else []
        if args.provider == "fabdem":
            return run(fetch_fabdem.main, ["--bbox", *map(str, bbox), "--out", args.data] + check)
        return run(fetch_dem.main, ["--bbox", *map(str, bbox), "--res", str(args.res),
                                    "--out", args.data] + check)

    def dataset():
        """Local source files + bbox + provider. The tile range ALWAYS comes
        from --bbox; data/manifest.json (if present) only supplies the file
        list (filtered to files intersecting the bbox) and the provider.
        Without a manifest the Copernicus naming convention is used."""
        mpath = os.path.join(args.data, "manifest.json")
        if os.path.exists(mpath):
            try:
                with open(mpath) as f:
                    m = json.load(f)
            except (OSError, ValueError) as e:
                print(f"WARNING: unreadable {mpath} ({e}); using file naming "
                      f"convention", file=sys.stderr)
                m = {}
            mb = m.get("bbox")
            if mb and [round(float(v), 6) for v in mb] != [round(float(v), 6) for v in bbox]:
                print(f"WARNING: --bbox {list(bbox)} differs from the manifest bbox "
                      f"{mb}; building --bbox (manifest used for the file list only)")
            files = [p for p in m.get("files", []) if os.path.exists(p)]
            # keep every file under the tiles being built: at low zooms a
            # tile reaches far outside the bbox and must keep its neighbours'
            # data (else a small-area rebuild would blank shared z0-z5 tiles)
            files = files_in_bbox(files, build_cover(bbox, args.minzoom))
            if files:
                return files, bbox, m.get("provider", args.provider)
        return bbox_src_files(bbox, args.res, args.data), bbox, args.provider

    def build():
        srcs, bb, provider = dataset()
        if not srcs:
            print("No source DEM files found — run `fetch` first.", file=sys.stderr)
            return 1
        argv = ["--src", *srcs, "--bbox", *map(str, bb),
                "--minzoom", str(args.minzoom), "--maxzoom", str(args.maxzoom)]
        force = ["--force"] if getattr(args, "force", False) else []
        if getattr(args, "lerc_only", False):
            force.append("--no-terrarium")
        # one pass: each tile is reprojected once for Terrarium and LERC
        # (ImageServer.json written at the end, as build_lerc does)
        return run(build_tiles.main, argv + force + [
            "--out", f"{args.root}/terrarium",
            "--lerc-out", args.root, "--lerc-name", args.name,
            "--provider", provider])

    def verify():
        srcs, bb, _provider = dataset()
        return run(verify_tiles.main, ["--src", *srcs, "--tiles", f"{args.root}/terrarium",
                                       "--bbox", *map(str, bb),
                                       "--minzoom", str(args.minzoom),
                                       "--maxzoom", str(args.maxzoom)])

    def publish():
        keep = ["--keep-raw"] if getattr(args, "keep_raw", False) else []
        return run(publish_tiles.main, ["--root", args.root] + keep)

    if args.cmd == "status":
        args.check = True
        return fetch()
    if args.cmd == "fetch":
        return fetch()
    if args.cmd == "build":
        return build()
    if args.cmd == "verify":
        return verify()
    if args.cmd == "publish":
        return publish()

    # all
    rc = fetch()
    if rc:
        return rc
    rc = build()
    if rc:
        return rc
    rc = verify()
    if rc:
        return rc
    return publish()


if __name__ == "__main__":
    raise SystemExit(main())
