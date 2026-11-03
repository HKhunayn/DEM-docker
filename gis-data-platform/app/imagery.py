"""Imagery fetch + delivery: download open XYZ imagery into local tile sets.

An imagery set is a Web-Mercator XYZ pyramid fetched from an open provider and
stored as-is (the provider's JPEGs are the source of truth — no re-encoding):

    data/imagery/<name>/imagery.json          set metadata (bbox, zooms, provider, counts)
    data/imagery/<name>/{z}/{x}/{y}.jpg       tiles

Delivery over-zooms beyond the fetched maxzoom server-side (crop + upscale of
the ancestor tile), so every client — including ArcGIS WebTileLayer, which has
no client-side over-zoom — sees continuous imagery.
"""
from __future__ import annotations

import io
import json
import math
import shutil
import threading
import os
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import IMAGERY_ROOT

MERCATOR_MAX_LAT = 85.05112878
MAX_TILES_PER_JOB = 50_000   # default; Settings page "Max imagery tiles per job"


def max_tiles_per_job() -> int:
    from . import settings
    value, source = settings.resolve("maxImageryTiles")
    return MAX_TILES_PER_JOB if source == "default" else int(value)
USER_AGENT = "gis-data-platform/0.1 (local open GIS platform)"
MAX_TILE_BYTES = 4 * 1024 * 1024
HTTP_TIMEOUT_S = 30.0
PROVIDER_MARKER = ".provider"  # written when a download starts, before imagery.json exists

from . import s2mosaic  # noqa: E402  (raw Sentinel-2 scenes, mosaicked locally)

# Kept: the providers that cover the whole world incl. the Middle East and
# looked right there. Order = fetch-page order; the first is the default.
#   s2-earthsearch  ~10 m, recent, open licence (built locally, slower)
#   s2cloudless2024 ~10 m ready tiles, fast, NON-COMMERCIAL
#   bluemarble      ~500 m, whole globe incl. oceans (world / low zoom)
# Dropped: s2cloudless 2016 (washed out pale yellow over deserts), Planetary
# Computer + Copernicus Data Space (same scenes as Earth Search; broken
# granules / account + slow JP2). Sets fetched with them keep being served.
PROVIDERS: dict[str, dict[str, Any]] = {
    **s2mosaic.SCENE_PROVIDERS,
    "s2cloudless2024": {
        "label": "Sentinel-2 cloudless 2024 (EOX) — ~10 m, ready tiles, fast (non-commercial)",
        "license": "CC BY-NC-SA 4.0 — non-commercial",
        "attribution": "Sentinel-2 cloudless - https://s2maps.eu by EOX IT Services GmbH "
                       "(Contains modified Copernicus Sentinel data 2024)",
        "source": "tiles.maps.eox.at (WMTS, GoogleMapsCompatible)",
        "url": "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2024_3857/default/g/{z}/{y}/{x}.jpg",
        "maxzoom": 15,
        "format": "jpg",
    },
    "bluemarble": {
        "label": "NASA Blue Marble (GIBS) — ~500 m, whole globe incl. oceans, for world view",
        "license": "Public domain (NASA)",
        "attribution": "NASA Earth Observatory / GIBS Blue Marble",
        "source": "gibs.earthdata.nasa.gov (WMTS, GoogleMapsCompatible_Level8)",
        "url": "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/BlueMarble_ShadedRelief_Bathymetry/"
               "default/2004-08/GoogleMapsCompatible_Level8/{z}/{y}/{x}.jpeg",
        "maxzoom": 8,
        "format": "jpg",
    },
}

def provider_status(provider: str) -> tuple[bool, str]:
    """(available, reason) - scene providers may need credentials."""
    if PROVIDERS.get(provider, {}).get("kind") == "scenes":
        return s2mosaic.provider_status(provider)
    return True, ""


_META_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- tile math ---------------------------------------------------------------

def clamp_bbox(bbox: list[float]) -> list[float]:
    minlon, minlat, maxlon, maxlat = bbox
    return [
        max(-180.0, minlon), max(-MERCATOR_MAX_LAT, minlat),
        min(180.0, maxlon), min(MERCATOR_MAX_LAT, maxlat),
    ]


def _lon_to_x(lon: float, z: int) -> int:
    n = 2 ** z
    return min(n - 1, max(0, int(math.floor((lon + 180.0) / 360.0 * n))))


def _lat_to_y(lat: float, z: int) -> int:
    n = 2 ** z
    r = math.radians(lat)
    y = (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n
    return min(n - 1, max(0, int(math.floor(y))))


def tile_range(bbox: list[float], z: int) -> tuple[int, int, int, int]:
    minlon, minlat, maxlon, maxlat = clamp_bbox(bbox)
    return _lon_to_x(minlon, z), _lat_to_y(maxlat, z), _lon_to_x(maxlon, z), _lat_to_y(minlat, z)


def iter_tiles(bbox: list[float], minzoom: int, maxzoom: int):
    for z in range(minzoom, maxzoom + 1):
        x0, y0, x1, y1 = tile_range(bbox, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield z, x, y


def count_tiles(bbox: list[float], minzoom: int, maxzoom: int) -> int:
    total = 0
    for z in range(minzoom, maxzoom + 1):
        x0, y0, x1, y1 = tile_range(bbox, z)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total


# ---- fetch ---------------------------------------------------------------------

class BadTileError(ValueError):
    """The provider answered, but not with a usable image tile (not retried)."""


def http_get(url: str, timeout: float = HTTP_TIMEOUT_S) -> bytes:
    """Download one tile. Module-level so tests can replace it.

    Only image/* responses up to MAX_TILE_BYTES are accepted: an HTML error
    page or captive portal answering 200 must not be stored as a tile.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            raise BadTileError(f"unexpected Content-Type {ctype or '(none)'!r}")
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > MAX_TILE_BYTES:
            raise BadTileError(f"tile too large ({length} bytes)")
        payload = response.read(MAX_TILE_BYTES + 1)
        if len(payload) > MAX_TILE_BYTES:
            raise BadTileError(f"tile larger than {MAX_TILE_BYTES} bytes")
        return payload


JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def check_tile_payload(payload: bytes) -> None:
    """Raise BadTileError unless `payload` is a JPEG or PNG within the size cap."""
    if not payload:
        raise BadTileError("empty response")
    if len(payload) > MAX_TILE_BYTES:
        raise BadTileError(f"tile larger than {MAX_TILE_BYTES} bytes")
    if not (payload.startswith(JPEG_MAGIC) or payload.startswith(PNG_MAGIC)):
        raise BadTileError("response is not a JPEG/PNG image")


def _fetch_one(url: str, target: Path, retries: int = 3) -> int:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            payload = http_get(url)
            check_tile_payload(payload)
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")
            temp.write_bytes(payload)
            temp.replace(target)
            return len(payload)
        except BadTileError as exc:
            raise RuntimeError(f"{url}: {exc}") from None
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def set_dir(name: str) -> Path:
    return IMAGERY_ROOT / name


def download_set(
    name: str,
    bbox: list[float],
    provider: str,
    minzoom: int,
    maxzoom: int,
    log: Callable[[str], None],
    workers: int | None = None,
    stop: threading.Event | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Download (resumable: existing tiles are kept) and write imagery.json.

    Setting `stop` cancels the pending downloads and raises RuntimeError.
    `options` (scene providers only): dateFrom, dateTo, maxCloud.
    """
    info = PROVIDERS[provider]
    bbox = clamp_bbox(list(bbox))
    total = count_tiles(bbox, minzoom, maxzoom)
    if total > max_tiles_per_job():
        raise ValueError(f"{total} tiles requested; the limit is {max_tiles_per_job()} per job "
                         "(reduce the area or the max zoom)")
    root = set_dir(name)
    root.mkdir(parents=True, exist_ok=True)
    (root / PROVIDER_MARKER).write_text(provider, encoding="utf-8")
    ext = info["format"]
    log(f"imagery '{name}': {provider} z{minzoom}-{maxzoom}, {total} tile(s), bbox {bbox}")
    if info.get("kind") == "scenes":
        opts = options or {}
        built = s2mosaic.build_set(root, provider, bbox, minzoom, maxzoom, tile_range, log, stop=stop,
                                   date_from=opts.get("dateFrom"), date_to=opts.get("dateTo"),
                                   max_cloud=opts.get("maxCloud"))
        tiles, size = _count_set(root)
        meta = _set_meta(name, provider, info, bbox, minzoom, maxzoom, ext, tiles, size, 0)
        meta.update(attribution=built.pop("attribution"), mosaic=built)
        _write_meta(root, meta)
        log(f"WROTE {tiles} imagery tiles ({size / 1e6:.2f} MB) from {built['scenes']} scene(s) -> {root}")
        return {"tiles": tiles, "downloaded": tiles, "skipped": 0, "failed": 0, "bytes": size}

    pending = []
    skipped = 0
    for z, x, y in iter_tiles(bbox, minzoom, maxzoom):
        target = root / str(z) / str(x) / f"{y}.{ext}"
        if target.is_file() and target.stat().st_size > 0:
            skipped += 1
            continue
        pending.append((info["url"].format(z=z, x=x, y=y), target))
    if skipped:
        log(f"  {skipped} tile(s) already present, kept")

    done = failed = 0
    downloaded = 0
    errors: list[str] = []
    def fetch(url: str, target: Path) -> int:
        if stop is not None and stop.is_set():
            return 0
        return _fetch_one(url, target)

    if workers is None:
        from . import settings
        workers = int(settings.get("imageryWorkers"))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch, url, target): url for url, target in pending}
        step = max(1, len(futures) // 20)
        for future in as_completed(futures):
            if stop is not None and stop.is_set():
                pool.shutdown(wait=True, cancel_futures=True)
                log("  download cancelled")
                raise RuntimeError("download cancelled")
            try:
                downloaded += future.result()
                done += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if len(errors) < 10:
                    errors.append(str(exc))
                log(f"  FAILED {exc}")
            if (done + failed) % step == 0 or done + failed == len(futures):
                log(f"  downloaded {done + failed}/{len(futures)} ({downloaded / 1e6:.2f} MB)")

    tiles, size = _count_set(root)
    meta = _set_meta(name, provider, info, bbox, minzoom, maxzoom, ext, tiles, size, failed)
    _write_meta(root, meta)
    log(f"WROTE {tiles} imagery tiles ({size / 1e6:.2f} MB) -> {root}")
    if failed:
        raise RuntimeError(f"{failed} tile(s) failed to download: {errors[0]}")
    return {"tiles": tiles, "downloaded": done, "skipped": skipped, "failed": failed, "bytes": size}


TILE_EXTS = ("jpg", "png", "webp")   # same order as nginx try_files


def _count_set(root: Path) -> tuple[int, int]:
    files = [p for ext in TILE_EXTS for p in root.rglob(f"*.{ext}")]
    return len(files), sum(p.stat().st_size for p in files)


def _write_meta(root: Path, meta: dict[str, Any]) -> None:
    with _META_LOCK:
        temp = root / f"imagery.json.{uuid.uuid4().hex}.tmp"
        temp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.replace(temp, root / "imagery.json")


def _set_meta(name, provider, info, bbox, minzoom, maxzoom, ext, tiles, size, failed) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "imagery",
        "provider": provider,
        "label": info["label"],
        "license": info["license"],
        "attribution": info["attribution"],
        "bbox": bbox,
        "minzoom": minzoom,
        "maxzoom": maxzoom,
        "format": ext,
        "tileSize": 256,
        "scheme": "xyz",
        "crs": "EPSG:3857",
        "tiles": tiles,
        "bytes": size,
        "failed": failed,
        "updated": _now(),
    }


# ---- catalog of local sets -------------------------------------------------------

def _valid_name(name: str) -> bool:
    return bool(name) and all(c.isalnum() or c in "_-" for c in name) and len(name) <= 64


_SET_CACHE: dict[str, tuple[int, dict[str, Any]]] = {}


def get_set(name: str) -> dict[str, Any] | None:
    """imagery.json of a set, cached by mtime (tile requests hit this constantly)."""
    if not _valid_name(name):
        return None
    path = set_dir(name) / "imagery.json"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        _SET_CACHE.pop(name, None)
        return None
    cached = _SET_CACHE.get(name)
    if cached is not None and cached[0] == mtime:
        return dict(cached[1])
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    _SET_CACHE[name] = (mtime, meta)
    return dict(meta)


def set_provider(name: str) -> str | None:
    """Provider an existing (possibly partial) set was fetched from, else None."""
    if not _valid_name(name):
        return None
    meta = get_set(name)
    if meta and meta.get("provider"):
        return str(meta["provider"])
    try:
        return (set_dir(name) / PROVIDER_MARKER).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def list_sets() -> list[dict[str, Any]]:
    out = []
    for meta in IMAGERY_ROOT.glob("*/imagery.json"):
        try:
            out.append(json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    # newest first
    return sorted(out, key=lambda m: m.get("updated", ""), reverse=True)


def delete_set(name: str) -> bool:
    """Remove a set directory, including partial downloads without imagery.json."""
    if not _valid_name(name):
        return False
    root = set_dir(name)
    _SET_CACHE.pop(name, None)
    if not root.is_dir():
        return False
    shutil.rmtree(root)
    return True


# ---- delivery ------------------------------------------------------------------------

_TRANSPARENT: bytes | None = None


def transparent_png() -> bytes:
    global _TRANSPARENT
    if _TRANSPARENT is None:
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(buffer, format="PNG")
        _TRANSPARENT = buffer.getvalue()
    return _TRANSPARENT


def tile(name: str, z: int, x: int, y: int) -> tuple[bytes, str] | None:
    """Tile bytes + media type; None when the set does not exist.

    Outside the fetched area or below minzoom: transparent PNG. Above maxzoom:
    the maxzoom ancestor cropped and upscaled (server-side over-zoom).
    """
    meta = get_set(name)
    if meta is None:
        return None
    if z < 0 or z > 24 or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        return transparent_png(), "image/png"
    root = set_dir(name)
    try:
        maxzoom = int(meta["maxzoom"])
    except (KeyError, TypeError, ValueError):
        return transparent_png(), "image/png"
    if z <= maxzoom:
        path = _tile_file(root, z, x, y)
        try:
            return path.read_bytes(), _MIME[path.suffix[1:]]
        except (OSError, AttributeError):
            return transparent_png(), "image/png"
    shift = z - maxzoom
    ax, ay = x >> shift, y >> shift
    parent = _tile_file(root, maxzoom, ax, ay)
    if parent is None:
        return transparent_png(), "image/png"
    from PIL import Image
    scale = 2 ** shift
    size = 256 / scale
    ox, oy = (x - (ax << shift)) * size, (y - (ay << shift)) * size
    try:
        with Image.open(parent) as image:
            alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
            image = image.convert("RGBA" if alpha else "RGB")
            # float box: works even when the ancestor footprint is < 1 px
            crop = image.resize((256, 256), Image.BILINEAR, box=(ox, oy, ox + size, oy + size))
        buffer = io.BytesIO()
        if alpha:   # partially covered ancestor: keep the transparency
            crop.save(buffer, format="WEBP", quality=85)
            return buffer.getvalue(), "image/webp"
        crop.save(buffer, format="JPEG", quality=88)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        # corrupt/truncated ancestor tile: degrade to transparent, not a 500
        return transparent_png(), "image/png"
    return buffer.getvalue(), "image/jpeg"


_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def _tile_file(root: Path, z: int, x: int, y: int) -> Path | None:
    for ext in TILE_EXTS:
        path = root / str(z) / str(x) / f"{y}.{ext}"
        if path.is_file():
            return path
    return None
