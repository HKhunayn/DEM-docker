"""Download FABDEM V1-2 1-degree tiles (Forest And Buildings removed DEM).

Source: University of Bristol data.bris.ac.uk — tiles are published as 10°×10°
ZIP blocks. The blocks are 1–8 GB each, but the server supports HTTP range
requests, so `remotezip` fetches the ZIP index + only the 1° members we need
(tens of MB instead of gigabytes).

Licence: CC BY-NC-SA 4.0 (NON-COMMERCIAL — contact fabdem@fathom.global for
commercial use). Heights are bare-earth: buildings and canopy removed from
Copernicus GLO-30 via ML.

Usage:
  python fetch_fabdem.py --bbox 6.6 45.6 7.2 46.0 --out ../data
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request

from remotezip import RemoteZip

DATASET_PAGE = "https://data.bris.ac.uk/data/dataset/s5hqmjcdj8yo2ibzi9b4ew3sn"
BLOCK_RE = re.compile(
    r"([NS])(\d{2})([EW])(\d{3})-([NS])(\d{2})([EW])(\d{3})_FABDEM_V1-2\.zip"
)


def parse_latlon(ns, lat, ew, lon):
    return ((-1 if ns == "S" else 1) * int(lat), (-1 if ew == "W" else 1) * int(lon))


def tile_member(lat, lon):
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}_FABDEM_V1-2.tif"


def discover_blocks(cache_path):
    """Scrape the dataset page for block name -> (bbox, direct URL)."""
    html = None
    if os.path.exists(cache_path):
        html = open(cache_path, encoding="utf-8").read()
    if not html or "FABDEM_V1-2.zip" not in html:
        print(f"fetching block index: {DATASET_PAGE}")
        import requests
        last = None
        for attempt in range(1, 4):
            try:
                r = requests.get(DATASET_PAGE, timeout=(30, 180))
                r.raise_for_status()
                html = r.text
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
                print(f"  retry {attempt}/3 for block index: {exc}", file=sys.stderr)
                time.sleep(3 * attempt)
        if html is None:
            raise RuntimeError(f"could not fetch FABDEM block index: {last}")
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(html)

    blocks = {}
    for url in re.findall(r"https://data\.bris\.ac\.uk/datasets/[^\"' ]*FABDEM_V1-2\.zip", html):
        name = url.rsplit("/", 1)[-1]
        m = BLOCK_RE.match(name)
        if not m:
            continue
        lat0, lon0 = parse_latlon(m.group(1), m.group(2), m.group(3), m.group(4))
        lat1, lon1 = parse_latlon(m.group(5), m.group(6), m.group(7), m.group(8))
        blocks[name] = {"bbox": [lon0, lat0, lon1, lat1], "url": url}
    if not blocks:
        raise RuntimeError("could not parse FABDEM block index from the dataset page")
    return blocks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"))
    ap.add_argument("--out", default="data")
    ap.add_argument("--check", action="store_true",
                    help="report cached vs missing tiles without downloading")
    args = ap.parse_args()
    minlon, minlat, maxlon, maxlat = args.bbox
    os.makedirs(args.out, exist_ok=True)

    if args.check:
        tiles = [(lat, lon)
                 for lat in range(int(minlat // 1), int(maxlat // 1) + 1)
                 for lon in range(int(minlon // 1), int(maxlon // 1) + 1)]
        cached = missing = 0
        for lat, lon in tiles:
            member = tile_member(lat, lon)
            dest = os.path.join(args.out, member)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                print(f"  cached  {member} ({os.path.getsize(dest) / 1e6:.1f} MB)")
                cached += 1
            else:
                print(f"  missing {member} (may be ocean/void)")
                missing += 1
        print(f"check: {cached}/{len(tiles)} FABDEM tiles already downloaded")
        return 0

    blocks = discover_blocks(os.path.join(args.out, "fabdem_index.html"))
    print(f"block index: {len(blocks)} FABDEM blocks")

    # 1-degree tiles covering the AOI
    tiles = [(lat, lon)
             for lat in range(int(minlat // 1), int(maxlat // 1) + 1)
             for lon in range(int(minlon // 1), int(maxlon // 1) + 1)]
    files = []
    for lat, lon in tiles:
        member = tile_member(lat, lon)
        dest = os.path.join(args.out, member)
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            print(f"  cached  {member}")
            files.append(dest)
            continue

        # find the block containing this tile
        hit = None
        for name, info in blocks.items():
            blon0, blat0, blon1, blat1 = info["bbox"]
            if blat0 <= lat < blat1 and blon0 <= lon < blon1:
                hit = (name, info)
                break
        if not hit:
            print(f"  MISSING no FABDEM block covers {member} (ocean?)", file=sys.stderr)
            continue

        name, info = hit
        print(f"  {member} <- {name}")
        ok = False
        for attempt in range(1, 4):
            try:
                # the blocks are 1-8 GB; we only range-read the index + this member
                with RemoteZip(info["url"], timeout=(30, 600)) as z:
                    names = z.namelist()
                    match = [n for n in names if n.endswith(member)]
                    if not match:
                        print(f"  MISSING {member} not in {name} (ocean?)", file=sys.stderr)
                        ok = True  # nothing to download
                        break
                    with z.open(match[0]) as src, open(dest, "wb") as out:
                        out.write(src.read())
                print(f"  saved   {dest} ({os.path.getsize(dest) / 1e6:.1f} MB)")
                files.append(dest)
                ok = True
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  retry {attempt}/3 for {member}: {exc}", file=sys.stderr)
                time.sleep(3 * attempt)
        if not ok:
            print(f"  FAILED  {member} after 3 attempts", file=sys.stderr)

    if not files:
        print("No FABDEM tiles downloaded.", file=sys.stderr)
        return 1

    manifest = {"provider": "fabdem", "res": 30,
                "bbox": [minlon, minlat, maxlon, maxlat], "files": files}
    with open(os.path.join(args.out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Done: {len(files)} tile(s) -> manifest {args.out}/manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
