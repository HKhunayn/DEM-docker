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
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import IMAGERY_ROOT

MERCATOR_MAX_LAT = 85.05112878
MAX_TILES_PER_JOB = 50_000
USER_AGENT = "gis-data-platform/0.1 (local open GIS platform)"

PROVIDERS: dict[str, dict[str, Any]] = {
    "s2cloudless": {
        "label": "Sentinel-2 cloudless 2016 (EOX) — ~10 m, true colour",
        "license": "CC BY 4.0 — attribution, commercial use OK",
        "attribution": "Sentinel-2 cloudless - https://s2maps.eu by EOX IT Services GmbH "
                       "(Contains modified Copernicus Sentinel data 2016)",
        "source": "tiles.maps.eox.at (WMTS, GoogleMapsCompatible)",
        "url": "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/{z}/{y}/{x}.jpg",
        "maxzoom": 15,
        "format": "jpg",
    },
    "bluemarble": {
        "label": "NASA Blue Marble (GIBS) — ~500 m, shaded relief + bathymetry",
        "license": "Public domain (NASA)",
        "attribution": "NASA Earth Observatory / GIBS Blue Marble",
        "source": "gibs.earthdata.nasa.gov (WMTS, GoogleMapsCompatible_Level8)",
        "url": "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/BlueMarble_ShadedRelief_Bathymetry/"
               "default/2004-08/GoogleMapsCompatible_Level8/{z}/{y}/{x}.jpeg",
        "maxzoom": 8,
        "format": "jpg",
    },
}

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

def http_get(url: str, timeout: float = 30.0) -> bytes:
    """Download one tile. Module-level so tests can replace it."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _fetch_one(url: str, target: Path, retries: int = 3) -> int:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            payload = http_get(url)
            if not payload:
                raise ValueError("empty response")
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(target.suffix + ".part")
            temp.write_bytes(payload)
            temp.replace(target)
            return len(payload)
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
    workers: int = 6,
) -> dict[str, Any]:
    """Download (resumable: existing tiles are kept) and write imagery.json."""
    info = PROVIDERS[provider]
    bbox = clamp_bbox(list(bbox))
    total = count_tiles(bbox, minzoom, maxzoom)
    if total > MAX_TILES_PER_JOB:
        raise ValueError(f"{total} tiles requested; the limit is {MAX_TILES_PER_JOB} per job "
                         "(reduce the area or the max zoom)")
    root = set_dir(name)
    root.mkdir(parents=True, exist_ok=True)
    ext = info["format"]
    log(f"imagery '{name}': {provider} z{minzoom}-{maxzoom}, {total} tile(s), bbox {bbox}")

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
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_fetch_one, url, target): url for url, target in pending}
        step = max(1, len(futures) // 20)
        for future in as_completed(futures):
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

    tiles = sum(1 for _ in root.rglob(f"*.{ext}"))
    size = sum(p.stat().st_size for p in root.rglob(f"*.{ext}"))
    meta = {
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
    with _META_LOCK:
        (root / "imagery.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log(f"WROTE {tiles} imagery tiles ({size / 1e6:.2f} MB) -> {root}")
    if failed:
        raise RuntimeError(f"{failed} tile(s) failed to download: {errors[0]}")
    return {"tiles": tiles, "downloaded": done, "skipped": skipped, "failed": failed, "bytes": size}


# ---- catalog of local sets -------------------------------------------------------

def _valid_name(name: str) -> bool:
    return bool(name) and all(c.isalnum() or c in "_-" for c in name) and len(name) <= 64


def get_set(name: str) -> dict[str, Any] | None:
    if not _valid_name(name):
        return None
    path = set_dir(name) / "imagery.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


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
    if not get_set(name):
        return False
    shutil.rmtree(set_dir(name))
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
    ext = meta.get("format", "jpg")
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
    root = set_dir(name)
    maxzoom = int(meta["maxzoom"])
    if z <= maxzoom:
        path = root / str(z) / str(x) / f"{y}.{ext}"
        if path.is_file():
            return path.read_bytes(), mime
        return transparent_png(), "image/png"
    shift = z - maxzoom
    ax, ay = x >> shift, y >> shift
    parent = root / str(maxzoom) / str(ax) / f"{ay}.{ext}"
    if not parent.is_file():
        return transparent_png(), "image/png"
    from PIL import Image
    scale = 2 ** shift
    size = 256 / scale
    ox, oy = (x - (ax << shift)) * size, (y - (ay << shift)) * size
    with Image.open(parent) as image:
        image = image.convert("RGB")
        # float box: works even when the ancestor footprint is < 1 px
        crop = image.resize((256, 256), Image.BILINEAR, box=(ox, oy, ox + size, oy + size))
    buffer = io.BytesIO()
    crop.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue(), "image/jpeg"
