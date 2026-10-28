"""Fetch Copernicus DEM (GLO-30 / GLO-90) COG tiles covering a WGS84 bbox.

Source: AWS Open Data buckets (no AWS account needed, plain HTTPS).
  GLO-30 (30 m): https://copernicus-dem-30m.s3.amazonaws.com
  GLO-90 (90 m): https://copernicus-dem-90m.s3.amazonaws.com

Already-downloaded tiles are skipped (cache check before any network access),
failed downloads are retried 3x, and --check reports what is cached vs
missing without downloading anything.

Usage:
  python fetch_dem.py --bbox 6.6 45.6 7.2 46.0 --res 30 --out ../data
  python fetch_dem.py --bbox 6.6 45.6 7.2 46.0 --out ../data --check
"""

import argparse
import json
import math
import os
import sys
import time
import urllib.error
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


def tile_list(res: int, bbox) -> list[tuple[str, str, str]]:
    """(folder, url, dest-basename) for every 1° tile covering the bbox."""
    minlon, minlat, maxlon, maxlat = bbox
    out = []
    for lat in range(int(math.floor(minlat)), int(math.floor(maxlat)) + 1):
        for lon in range(int(math.floor(minlon)), int(math.floor(maxlon)) + 1):
            folder = tile_folder(res, lat, lon)
            out.append((folder, f"{BUCKETS[res]}/{folder}/{folder}.tif", f"{folder}.tif"))
    return out


def download(url: str, dest: str, retries: int = 3) -> None:
    """Download with retries. A 404 is raised immediately (tile genuinely
    absent from the source, e.g. ocean); other errors retry with backoff."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return
    tmp = dest + ".part"
    for attempt in range(1, retries + 1):
        try:
            print(f"  GET     {url}" if attempt == 1 else f"  RETRY {attempt}/{retries} {url}")
            urllib.request.urlretrieve(url, tmp)
            os.replace(tmp, dest)
            print(f"  saved   {dest} ({os.path.getsize(dest) / 1e6:.1f} MB)")
            return
        except urllib.error.HTTPError:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        except Exception as exc:  # noqa: BLE001
            if os.path.exists(tmp):
                os.remove(tmp)
            if attempt == retries:
                raise
            print(f"  retry {attempt}/{retries} for {url}: {exc}", file=sys.stderr)
            time.sleep(3 * attempt)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"))
    ap.add_argument("--res", type=int, default=30, choices=[30, 90])
    ap.add_argument("--out", default="data")
    ap.add_argument("--check", action="store_true",
                    help="report cached vs missing tiles without downloading")
    args = ap.parse_args()

    tiles = tile_list(args.res, args.bbox)
    cached, missing = [], []
    for folder, url, fname in tiles:
        dest = os.path.join(args.out, fname)
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            cached.append((folder, url, dest))
        else:
            missing.append((folder, url, dest))
    print(f"tiles for bbox: {len(tiles)} | cached: {len(cached)} | missing: {len(missing)}")

    if args.check:
        for folder, _, dest in cached:
            print(f"  cached  {folder}.tif ({os.path.getsize(dest) / 1e6:.1f} MB)")
        for folder, _, _ in missing:
            print(f"  missing {folder}.tif")
        print(f"check: {len(cached)}/{len(tiles)} tiles already downloaded")
        return 0

    os.makedirs(args.out, exist_ok=True)
    paths = []
    downloaded = failed = 0
    for folder, url, dest in cached:
        print(f"  cached  {dest}")
        paths.append(dest)
    for folder, url, dest in missing:
        try:
            download(url, dest)
            paths.append(dest)
            downloaded += 1
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                print(f"  MISSING {url} (not at source — ocean/void)", file=sys.stderr)
            else:
                print(f"  FAILED  {url} (HTTP {exc.code})", file=sys.stderr)
                failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED  {url} ({exc})", file=sys.stderr)
            failed += 1

    if not paths:
        print("No DEM tiles downloaded.", file=sys.stderr)
        return 1

    manifest = {"provider": "copernicus", "res": args.res,
                "bbox": list(args.bbox), "files": paths}
    with open(os.path.join(args.out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Done: {len(paths)} tile(s) (cached {len(cached)}, downloaded {downloaded}, "
          f"failed {failed}) -> manifest {args.out}/manifest.json")
    for p in paths:
        print(f"  {p}")
    if failed:
        print("Some tiles failed to download — re-run to retry only the missing ones.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())