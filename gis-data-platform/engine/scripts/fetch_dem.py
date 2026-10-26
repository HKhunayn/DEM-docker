"""Fetch Copernicus DEM (GLO-30 / GLO-90) COG tiles covering a WGS84 bbox.

Source: AWS Open Data buckets (no AWS account needed, plain HTTPS).
  GLO-30 (30 m): https://copernicus-dem-30m.s3.amazonaws.com
  GLO-90 (90 m): https://copernicus-dem-90m.s3.amazonaws.com

Usage:
  python fetch_dem.py --bbox 6.6 45.6 7.2 46.0 --res 30 --out ../data
"""

import argparse
import json
import os
import sys
import urllib.request

BUCKETS = {
    30: "https://copernicus-dem-30m.s3.amazonaws.com",
    90: "https://copernicus-dem-90m.s3.amazonaws.com",
}
# naming uses the DGED "resolution in arc seconds" code: 10 -> GLO-30, 30 -> GLO-90
RES_CODE = {30: 10, 90: 30}


def tile_folder(res: int, lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_{RES_CODE[res]:02d}_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def download(url: str, dest: str) -> None:
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  cached  {dest}")
        return
    print(f"  GET     {url}")
    tmp = dest + ".part"
    urllib.request.urlretrieve(url, tmp)
    os.replace(tmp, dest)
    print(f"  saved   {dest} ({os.path.getsize(dest) / 1e6:.1f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"))
    ap.add_argument("--res", type=int, default=30, choices=[30, 90])
    ap.add_argument("--out", default="data")
    args = ap.parse_args()

    minlon, minlat, maxlon, maxlat = args.bbox
    base = BUCKETS[args.res]
    os.makedirs(args.out, exist_ok=True)

    paths = []
    for lat in range(int(minlat // 1), int(maxlat // 1) + 1):
        for lon in range(int(minlon // 1), int(maxlon // 1) + 1):
            folder = tile_folder(args.res, lat, lon)
            url = f"{base}/{folder}/{folder}.tif"
            dest = os.path.join(args.out, f"{folder}.tif")
            try:
                download(url, dest)
                paths.append(dest)
            except Exception as exc:  # noqa: BLE001
                print(f"  MISSING {url} ({exc})", file=sys.stderr)

    if not paths:
        print("No DEM tiles downloaded.", file=sys.stderr)
        return 1

    manifest = {"provider": "copernicus", "res": args.res,
                "bbox": [minlon, minlat, maxlon, maxlat], "files": paths}
    with open(os.path.join(args.out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Done: {len(paths)} tile(s) -> manifest {args.out}/manifest.json")
    for p in paths:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
