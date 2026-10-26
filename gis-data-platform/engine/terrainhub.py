"""heightmap engine — fetch 30 m elevation data, store it locally, serve it to
any ArcGIS client (plus MapLibre/deck.gl/QGIS via the Terrarium pyramid).

Subcommands (all local, all open source):
  fetch    download Copernicus DEM COG tiles for a bbox (ESA open data)
  build    DEM -> terrarium PNG pyramid + ArcGIS LERC ImageServer pyramid
  verify   encoder round-trip + ground-truth checks
  serve    local server: ArcGIS REST elevation service + terrarium tiles
  all      fetch + build + verify in one go

Examples:
  python terrainhub.py all --bbox 6.6 45.6 7.2 46.0 --name local30m
  python terrainhub.py serve --port 8765
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import fetch_dem, fetch_fabdem, build_tiles, build_lerc, verify_tiles, serve  # noqa: E402,F401

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

    for c in ("fetch", "build", "verify", "all"):
        sp = sub.add_parser(c, parents=[common])
        if c == "fetch":
            sp.add_argument("--out", default=DATA_DIR, help="source DEM output dir")

    sp = sub.add_parser("serve")
    sp.add_argument("--root", default=PUBLIC_DIR, help="static UI pages (public/)")
    sp.add_argument("--tiles", default=TILE_DIR, help="tile data root (data/tiles)")
    sp.add_argument("--port", type=int, default=8765)

    args = ap.parse_args()
    bbox = getattr(args, "bbox", None)

    if args.cmd == "serve":
        return run(serve.main, ["--root", args.root, "--tiles", args.tiles,
                               "--port", str(args.port)])

    def fetch():
        if args.provider == "fabdem":
            return run(fetch_fabdem.main, ["--bbox", *map(str, bbox), "--out", args.data])
        return run(fetch_dem.main, ["--bbox", *map(str, bbox), "--res", str(args.res),
                                    "--out", args.data])

    def dataset():
        """Local source files + bbox + provider: data/manifest.json if present,
        else the Copernicus naming convention for the requested bbox."""
        mpath = os.path.join(args.data, "manifest.json")
        if os.path.exists(mpath):
            with open(mpath) as f:
                m = json.load(f)
            files = [p for p in m.get("files", []) if os.path.exists(p)]
            if files:
                return files, m.get("bbox", bbox), m.get("provider", args.provider)
        return bbox_src_files(bbox, args.res, args.data), bbox, args.provider

    def build():
        srcs, bb, provider = dataset()
        if not srcs:
            print("No source DEM files found — run `fetch` first.", file=sys.stderr)
            return 1
        argv = ["--src", *srcs, "--bbox", *map(str, bb),
                "--minzoom", str(args.minzoom), "--maxzoom", str(args.maxzoom)]
        rc = run(build_tiles.main, argv + ["--out", f"{args.root}/terrarium",
                                           "--hillshade-out", f"{args.root}/hillshade"])
        if rc:
            return rc
        return run(build_lerc.main, argv + ["--name", args.name, "--out", args.root,
                                            "--provider", provider])

    def verify():
        srcs, bb, _provider = dataset()
        return run(verify_tiles.main, ["--src", *srcs, "--tiles", f"{args.root}/terrarium",
                                       "--bbox", *map(str, bb),
                                       "--minzoom", str(args.minzoom),
                                       "--maxzoom", str(args.maxzoom)])

    if args.cmd == "fetch":
        return fetch()
    if args.cmd == "build":
        return build()
    if args.cmd == "verify":
        return verify()

    # all
    rc = fetch()
    if rc:
        return rc
    rc = build()
    if rc:
        return rc
    return verify()


if __name__ == "__main__":
    raise SystemExit(main())
