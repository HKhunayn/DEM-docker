"""Fetch Copernicus DEM (GLO-30 / GLO-90) COG tiles covering a WGS84 bbox.

Source: AWS Open Data buckets (no AWS account needed, plain HTTPS).
  GLO-30 (30 m): https://copernicus-dem-30m.s3.amazonaws.com
  GLO-90 (90 m): https://copernicus-dem-90m.s3.amazonaws.com

Already-downloaded tiles are skipped (cache check before any network access;
a cached file that is not a TIFF is re-downloaded). Downloads stream into a
unique ``.part`` file with a socket timeout, are validated (Content-Length and
TIFF magic) and only then moved into place with ``os.replace``. A 404 means the
tile does not exist at the source (ocean/void) and is not retried; 5xx, 429,
timeouts and connection errors are retried with exponential backoff. --check
reports what is cached vs missing without downloading anything. Missing tiles
are downloaded concurrently (--jobs, default 8: the transfer is network-bound);
log lines stay whole and the manifest keeps the tile order of the bbox.

Usage:
  python fetch_dem.py --bbox 6.6 45.6 7.2 46.0 --res 30 --out ../data
  python fetch_dem.py --bbox 6.6 45.6 7.2 46.0 --out ../data --check
"""

import argparse
import glob
import http.client
import json
import math
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

BUCKETS = {
    30: "https://copernicus-dem-30m.s3.amazonaws.com",
    90: "https://copernicus-dem-90m.s3.amazonaws.com",
}
# naming uses the DGED "resolution in arc seconds" code: 10 -> GLO-30, 30 -> GLO-90
RES_CODE = {30: 10, 90: 30}

TIMEOUT = 60          # seconds per socket operation (connect / each read)
RETRIES = 5           # total attempts per tile
BACKOFF = 2.0         # first retry delay in seconds, doubled each attempt
MAX_BACKOFF = 60.0
CHUNK = 1 << 20
# classic TIFF and BigTIFF, little / big endian
TIFF_MAGIC = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
# HTTP codes that mean "the object is not there". The Copernicus buckets grant
# ListBucket, so S3 answers 404 NoSuchKey for ocean tiles (verified against
# the live bucket). A 403 is therefore a real access problem (proxy, WAF, bucket
# policy) and is reported as FAILED, not silently skipped as ocean.
MISSING_CODES = (404,)
RETRY_CODES = (408, 425, 429, 500, 502, 503, 504)
STALE_PART_AGE = 3600  # seconds; older orphaned .part files are removed
JOBS = 8              # concurrent downloads

_PRINT_LOCK = threading.Lock()


def _say(msg: str, err: bool = False) -> None:
    """Print one whole line (one write under a lock), so lines from
    concurrent downloads never interleave."""
    stream = sys.stderr if err else sys.stdout
    with _PRINT_LOCK:
        stream.write(msg + "\n")
        stream.flush()


class MissingTile(Exception):
    """The tile does not exist at the source (ocean / void) — don't retry."""

    def __init__(self, url: str, code: int):
        super().__init__(f"HTTP {code} for {url}")
        self.url = url
        self.code = code


class DownloadError(Exception):
    """Download failed permanently (non-retryable or retries exhausted)."""


class _Retryable(Exception):
    def __init__(self, msg: str, delay: float | None = None):
        super().__init__(msg)
        self.delay = delay


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


def has_tiff_magic(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) in TIFF_MAGIC
    except OSError:
        return False


def is_cached(path: str) -> bool:
    """A usable cached tile: non-empty and starts with a TIFF header."""
    try:
        return os.path.getsize(path) > 0 and has_tiff_magic(path)
    except OSError:
        return False


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _clean_stale_parts(dest: str) -> None:
    """Remove .part files orphaned by killed runs (fresh ones may be in use)."""
    now = time.time()
    for p in glob.glob(glob.escape(dest) + ".*.part"):
        try:
            if now - os.path.getmtime(p) > STALE_PART_AGE:
                os.remove(p)
        except OSError:
            pass


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    try:
        v = exc.headers.get("Retry-After") if exc.headers else None
        return max(0.0, min(float(v), MAX_BACKOFF)) if v else None
    except (TypeError, ValueError):
        return None


_NET_ERRORS = (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
               http.client.HTTPException, OSError)


def _fetch_once(url: str, tmp: str, timeout: float) -> int:
    """One attempt: stream url -> tmp and validate. Returns bytes written."""
    req = urllib.request.Request(url, headers={"User-Agent": "heightmap-30m/fetch_dem"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        delay = _retry_after(exc)
        exc.close()
        if exc.code in MISSING_CODES:
            raise MissingTile(url, exc.code) from None
        if exc.code in RETRY_CODES:
            raise _Retryable(f"HTTP {exc.code}", delay) from None
        raise DownloadError(f"HTTP {exc.code} for {url}") from None
    except _NET_ERRORS as exc:
        raise _Retryable(f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None

    with resp:
        length = resp.headers.get("Content-Length")
        expected = int(length) if length and length.strip().isdigit() else None
        written = 0
        with open(tmp, "wb") as out:  # local disk errors propagate (not retried)
            while True:
                try:
                    chunk = resp.read(CHUNK)
                except _NET_ERRORS as exc:
                    raise _Retryable(f"read failed after {written} bytes: "
                                     f"{type(exc).__name__}: {exc}") from None
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)

    if expected is not None and written != expected:
        raise _Retryable(f"size mismatch: got {written} bytes, Content-Length {expected}")
    if not has_tiff_magic(tmp):
        raise _Retryable(f"response is not a TIFF ({written} bytes)")
    return written


def download(url: str, dest: str, retries: int = RETRIES, timeout: float = TIMEOUT,
             backoff: float = BACKOFF) -> None:
    """Download url to dest atomically, with retries.

    Raises MissingTile for 404 (tile absent at the source — not retried)
    and DownloadError for other 4xx (not retried) or once retries are exhausted.
    """
    if is_cached(dest):
        return
    if os.path.exists(dest):
        _say(f"  INVALID cached {dest} (not a TIFF) — re-downloading", err=True)
    _clean_stale_parts(dest)
    retries = max(1, int(retries))
    for attempt in range(1, retries + 1):
        tmp = f"{dest}.{os.getpid()}.{uuid.uuid4().hex[:8]}.part"
        _say(f"  GET     {url}" if attempt == 1 else f"  RETRY {attempt}/{retries} {url}")
        try:
            _fetch_once(url, tmp, timeout)
            os.replace(tmp, dest)
            _say(f"  saved   {dest} ({os.path.getsize(dest) / 1e6:.1f} MB)")
            return
        except _Retryable as exc:
            _remove(tmp)
            if attempt == retries:
                raise DownloadError(f"{exc} (gave up after {retries} attempts)") from None
            delay = exc.delay
            if delay is None:
                delay = min(backoff * (2 ** (attempt - 1)), MAX_BACKOFF)
            _say(f"  retry {attempt}/{retries} for {url}: {exc} (waiting {delay:g}s)",
                 err=True)
            time.sleep(delay)
        except BaseException:
            _remove(tmp)
            raise


def _fetch_tile(url: str, dest: str, retries: int, timeout: float) -> str:
    """One tile: 'ok', 'missing' (404, ocean/void) or 'failed' — the
    MISSING/FAILED line is printed here as soon as the tile finishes."""
    try:
        download(url, dest, retries=retries, timeout=timeout)
        return "ok"
    except MissingTile:
        _say(f"  MISSING {url} (not at source — ocean/void)", err=True)
        return "missing"
    except Exception as exc:  # noqa: BLE001
        _say(f"  FAILED  {url} ({exc})", err=True)
        return "failed"


def fetch_missing(missing, retries: int, timeout: float, jobs: int = JOBS) -> list[str]:
    """Download (folder, url, dest) tiles with up to `jobs` threads. Returns
    one status per tile, in the input order."""
    jobs = max(1, min(int(jobs), len(missing) or 1))
    if jobs == 1:
        return [_fetch_tile(url, dest, retries, timeout) for _, url, dest in missing]
    ex = ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="fetch")
    try:
        futures = [ex.submit(_fetch_tile, url, dest, retries, timeout)
                   for _, url, dest in missing]
        results = [f.result() for f in futures]
    except BaseException:
        # Ctrl-C / SystemExit: drop the queued tiles (running ones finish
        # into their own .part files and are moved/cleaned up as usual)
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown(wait=True)
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"))
    ap.add_argument("--res", type=int, default=30, choices=[30, 90])
    ap.add_argument("--out", default="data")
    ap.add_argument("--check", action="store_true",
                    help="report cached vs missing tiles without downloading")
    ap.add_argument("--timeout", type=float, default=TIMEOUT,
                    help=f"socket timeout in seconds (default {TIMEOUT})")
    ap.add_argument("--retries", type=int, default=RETRIES,
                    help=f"attempts per tile (default {RETRIES})")
    ap.add_argument("--jobs", type=int, default=JOBS,
                    help=f"concurrent downloads (default {JOBS})")
    args = ap.parse_args(argv)

    tiles = tile_list(args.res, args.bbox)
    cached, missing = [], []
    for folder, url, fname in tiles:
        dest = os.path.join(args.out, fname)
        if is_cached(dest):
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
    statuses = fetch_missing(missing, args.retries, args.timeout, args.jobs)
    for (folder, url, dest), status in zip(missing, statuses):
        if status == "ok":
            paths.append(dest)
            downloaded += 1
        elif status == "failed":
            failed += 1

    if not paths:
        print("No DEM tiles downloaded.", file=sys.stderr)
        return 1

    manifest = {"provider": "copernicus", "res": args.res,
                "bbox": list(args.bbox), "files": paths}
    manifest_path = os.path.join(args.out, "manifest.json")
    tmp_manifest = f"{manifest_path}.{os.getpid()}.part"
    with open(tmp_manifest, "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp_manifest, manifest_path)

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
