"""Sentinel-2 L2A imagery sets built locally from raw scenes (~10 m, worldwide).

Source: Element 84 Earth Search on AWS (``s2-earthsearch``) - public COGs,
no account, global land + coastal water between ~56 S and ~84 N (open ocean
is not imaged). Planetary Computer and Copernicus Data Space serve the same
ESA scenes; they were tried and dropped (broken DN-128 granules, account +
slow JPEG2000 reads respectively).

Pipeline per job (all local, nothing is re-served from the internet):

1. For every 8x8 block of max-zoom tiles ("metatile") search the catalogue for
   scenes over it in the date range / under the cloud limit, best first
   (cloud cover bucket, then newest - neighbours in one granule pick the same
   scene, so blocks don't patchwork).
2. Warp red/green/blue (10 m) + SCL (20 m scene classification) straight into
   the metatile's Web-Mercator grid; composite per pixel, skipping cloud,
   cloud shadow, cirrus, saturated and no-data pixels (cloudy pixels are only
   used where no clear one exists).
3. Reflectance -> 8-bit with ONE stretch per set (white point = the 98th
   percentile of clear land pixels of a preview sample, clamped), so deserts are
   not blown out and forests are not black. Full tiles -> JPEG, partially
   covered tiles -> WebP with alpha, empty tiles are not written.
4. Lower zooms are averaged down from their four children.
"""
from __future__ import annotations

import io
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

WEB_MERCATOR_R = 6378137.0 * math.pi
META = 8                     # metatile = META x META tiles of 256 px
MOSAIC_VERSION = 7          # bump when written pixels change (invalidates resume)
STRIP = 256                 # rows per median strip (memory)
MIN_CLEAR = 3               # keep reading scenes until each pixel has this many clear samples
MAX_READS = 20              # ... or this many scenes were read for the block
FOOTPRINT_GRID = 32         # resolution of the footprint-vs-needed test
BROKEN_REFLECTANCE = 0.0    # scene median below this = broken product
MEDIAN_SCENES = 5            # per-pixel median of the best N scenes per block
SEARCH_PAGE = 100           # items per STAC page
SEARCH_MAX_ITEMS = 400      # per block: enough for every granule/orbit over it
HTTP_TIMEOUT_S = 60.0
USER_AGENT = "gis-data-platform/0.1 (local open GIS platform)"
# SCL classes never used when a clear pixel exists: 0 no data, 1 saturated,
# 3 cloud shadow, 8/9 cloud medium/high, 10 thin cirrus.
SCL_BAD = (0, 1, 3, 8, 9, 10)
# Stretch sample: vegetation + bare soil only - snow, water and cloud would
# set the white point (snow made Tromso almost black).
SCL_LAND = (4, 5)
WHITE_MIN, WHITE_MAX = 0.18, 0.60   # reflectance clamp of the white point
# 98th (not 99.5th) percentile: snow fields / roofs classified as land
# otherwise set the white point (Tromso: 0.47 at p99.5 vs 0.22 at p98).
WHITE_PERCENTILE = 98.0
WHITE_MEDIAN_FACTOR = 3.5
JPEG_QUALITY = 88
WEBP_QUALITY = 85
ATTRIBUTION = "Contains modified Copernicus Sentinel data {year} (ESA)"
LICENSE = "Copernicus open data licence — free, commercial use OK with attribution"

SCENE_PROVIDERS: dict[str, dict[str, Any]] = {
    "s2-earthsearch": {
        "label": "Sentinel-2 L2A (ESA) via AWS Earth Search — ~10 m, worldwide land, recent, built locally (recommended)",
        "source": "earth-search.aws.element84.com (STAC) + AWS open data COGs",
        "stac": "https://earth-search.aws.element84.com/v1",
        "collection": "sentinel-2-c1-l2a",
        "assets": {"red": "red", "green": "green", "blue": "blue", "scl": "scl"},
    },
}
for _info in SCENE_PROVIDERS.values():
    _info.update(kind="scenes", license=LICENSE, maxzoom=15, format="jpg",
                 attribution="Contains modified Copernicus Sentinel data (ESA)")

DEFAULT_MAX_CLOUD = 20.0
DEFAULT_DAYS = 365


class NoScenesError(RuntimeError):
    pass


def provider_status(provider: str) -> tuple[bool, str]:
    return True, ""


def default_dates(today: date | None = None, days: int | None = None) -> tuple[str, str]:
    today = today or datetime.now(timezone.utc).date()
    days = DEFAULT_DAYS if days is None else days
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def _setting(key: str, fallback: Any) -> Any:
    """Settings page value (saved > env), else the module default."""
    from . import settings
    value, source = settings.resolve(key)
    return fallback if source == "default" else value


# ---- tile maths ---------------------------------------------------------------

def tile_bounds(z: int, x: int, y: int, nx: int = 1, ny: int = 1) -> tuple[float, float, float, float]:
    """EPSG:3857 bounds of the nx*ny tile block whose top-left tile is (x, y)."""
    size = 2 * WEB_MERCATOR_R / 2 ** z
    return (-WEB_MERCATOR_R + x * size, WEB_MERCATOR_R - (y + ny) * size,
            -WEB_MERCATOR_R + (x + nx) * size, WEB_MERCATOR_R - y * size)


def mercator_to_lonlat(mx: float, my: float) -> tuple[float, float]:
    lon = mx / WEB_MERCATOR_R * 180.0
    lat = math.degrees(math.atan(math.sinh(my / WEB_MERCATOR_R * math.pi)))
    return lon, lat


def metatiles(tile_range: tuple[int, int, int, int]):
    """(x0, y0, nx, ny) blocks aligned on META, clipped to the tile range."""
    x0, y0, x1, y1 = tile_range
    for by in range(y0 // META * META, y1 + 1, META):
        for bx in range(x0 // META * META, x1 + 1, META):
            ax, ay = max(bx, x0), max(by, y0)
            ex, ey = min(bx + META - 1, x1), min(by + META - 1, y1)
            yield ax, ay, ex - ax + 1, ey - ay + 1


# ---- STAC ---------------------------------------------------------------------

def http_json(url: str, body: dict | None = None, timeout: float = HTTP_TIMEOUT_S) -> Any:
    """GET/POST JSON. Module-level so tests can replace it."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    last: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except (OSError, ValueError) as exc:
            last = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def _scale_offset(item: dict, asset: dict) -> tuple[float, float]:
    bands = asset.get("raster:bands")
    if isinstance(bands, list) and bands and isinstance(bands[0], dict) and "scale" in bands[0]:
        return float(bands[0].get("scale", 1e-4)), float(bands[0].get("offset", 0.0))
    if "raster:scale" in asset:
        return float(asset["raster:scale"]), float(asset.get("raster:offset", 0.0))
    # No scale metadata: raw DN; processing baseline 04.00+ adds +1000.
    baseline = str(item.get("properties", {}).get("s2:processing_baseline") or "")
    try:
        offset = -0.1 if float(baseline) >= 4.0 else 0.0
    except ValueError:
        offset = -0.1
    return 1e-4, offset


def parse_item(item: dict, info: dict) -> dict | None:
    """Scene record (hrefs, footprint bbox, cloud, date, reflectance scaling)."""
    props = item.get("properties") or {}
    assets = item.get("assets") or {}
    hrefs = {}
    for key, name in info["assets"].items():
        asset = assets.get(name)
        if not isinstance(asset, dict) or not asset.get("href"):
            return None
        hrefs[key] = asset["href"]
    bbox = item.get("bbox")
    if not (isinstance(bbox, list) and len(bbox) >= 4):
        return None
    scale, offset = _scale_offset(item, assets[info["assets"]["red"]])
    cloud = props.get("eo:cloud_cover")
    sun = props.get("view:sun_elevation")
    if not isinstance(sun, (int, float)) and isinstance(props.get("s2:mean_solar_zenith"), (int, float)):
        sun = 90.0 - float(props["s2:mean_solar_zenith"])
    return {
        "id": str(item.get("id")),
        "datetime": str(props.get("datetime") or ""),
        "cloud": float(cloud) if isinstance(cloud, (int, float)) else 100.0,
        "sun": float(sun) if isinstance(sun, (int, float)) else None,
        "bbox": [float(v) for v in (bbox[:4] if len(bbox) == 4 else (bbox[0], bbox[1], bbox[3], bbox[4]))],
        "geometry": item.get("geometry") if isinstance(item.get("geometry"), dict) else None,
        "hrefs": hrefs,
        "scale": scale,
        "offset": offset,
    }


def scene_rank(scene: dict) -> tuple:
    """Best first: 5 %-cloud bucket, then high sun (10 deg buckets: summer
    light, short shadows, no low-sun haze/snow at high latitudes), then newest
    (neighbouring blocks in one granule pick the same pass)."""
    sun = scene.get("sun")
    sun_bucket = -int(sun // 10) if isinstance(sun, (int, float)) else 0
    return (int(scene["cloud"] // 5), sun_bucket, _neg_date(scene["datetime"]), scene["id"])


def _neg_date(value: str) -> float:
    try:
        return -datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def search(info: dict, bbox: list[float], date_from: str, date_to: str, max_cloud: float,
           max_items: int = SEARCH_MAX_ITEMS) -> list[dict]:
    """All scenes over `bbox` (paged, up to max_items), best first.

    One page is not enough: the newest 50-100 items over a block can all come
    from a few granules / one orbit, leaving the rest of the block empty.
    """
    body = {
        "collections": [info["collection"]],
        "bbox": [round(v, 6) for v in bbox],
        "datetime": f"{date_from}T00:00:00Z/{date_to}T23:59:59Z",
        "limit": SEARCH_PAGE,
        "query": {"eo:cloud_cover": {"lte": max_cloud}},
    }
    url, scenes, seen = f"{info['stac']}/search", [], set()
    request: tuple[str, dict | None] | None = (url, body)
    while request is not None and len(seen) < max_items:
        data = http_json(*request)
        features = data.get("features") or []
        for item in features:
            scene = parse_item(item, info)
            if scene and scene["id"] not in seen and scene["cloud"] <= max_cloud:  # some servers ignore `query`
                seen.add(scene["id"])
                scenes.append(scene)
        request = _next_page(data, body) if features else None
    return sorted(scenes, key=scene_rank)


def _next_page(data: dict, body: dict) -> tuple[str, dict | None] | None:
    for link in data.get("links") or []:
        if isinstance(link, dict) and link.get("rel") == "next" and link.get("href"):
            if str(link.get("method", "GET")).upper() == "POST":
                nxt = dict(body) if link.get("merge", True) else {}
                nxt.update(link.get("body") or {})
                return link["href"], nxt
            return link["href"], None
    return None


# ---- asset access (signing + GDAL environment) --------------------------------

class Signer:
    """Turns an asset href into a GDAL path (Earth Search: public, unchanged)."""

    def __init__(self, info: dict):
        self.collection = info["collection"]

    def href(self, href: str) -> str:
        return href


def gdal_env(info: dict) -> dict[str, Any]:
    env = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF,.tiff",
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        "GDAL_HTTP_MULTIPLEX": "YES",
        "GDAL_HTTP_VERSION": "2",
        "GDAL_HTTP_MAX_RETRY": "5",
        "GDAL_HTTP_RETRY_DELAY": "2",
        "GDAL_HTTP_TIMEOUT": "60",
        "VSI_CACHE": "TRUE",
        "VSI_CACHE_SIZE": str(8 * 1024 * 1024),     # per open file
        "GDAL_CACHEMAX": 128,
        "GDAL_HTTP_USERAGENT": USER_AGENT,
    }
    env["AWS_NO_SIGN_REQUEST"] = "YES"
    return env


def read_scene(scene: dict, signer: Signer, bounds, width: int, height: int):
    """(rgb uint16 [3,h,w], scl uint8 [h,w]) warped into the EPSG:3857 grid."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.vrt import WarpedVRT

    transform = from_bounds(*bounds, width, height)
    # metres per output pixel on the ground (Mercator scale at the centre)
    _, lat = mercator_to_lonlat((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
    ground = (bounds[2] - bounds[0]) / width * math.cos(math.radians(lat))
    out = []
    for key in ("red", "green", "blue", "scl"):
        resampling = Resampling.nearest if key == "scl" else Resampling.bilinear
        href = signer.href(scene["hrefs"][key])
        level = overview_level(ground, 20.0 if key == "scl" else 10.0)
        if level is not None:
            with rasterio.open(href) as probe:
                count = len(probe.overviews(1))
            level = min(level, count - 1) if count else None
        kwargs = {"overview_level": level} if level is not None else {}
        with rasterio.open(href, **kwargs) as src, WarpedVRT(
            src, crs="EPSG:3857", transform=transform, width=width, height=height,
            resampling=resampling, src_nodata=0, nodata=0,
        ) as vrt:
            out.append(vrt.read(1))
    return np.stack(out[:3]).astype(np.uint16), out[3].astype(np.uint8)


def overview_level(ground_m: float, native_m: float) -> int | None:
    """0-based index of the coarsest 2x overview still at least as fine as
    the output pixel (None = full resolution). Below z14 this reads 4-64x less."""
    ratio = ground_m / native_m
    if ratio < 2.0:
        return None
    return int(math.log2(ratio)) - 1


# ---- composite + stretch --------------------------------------------------------

class Composite:
    """Per-pixel median of up to MEDIAN_SCENES clear samples.

    A median (not "first clear scene wins") hides what the scene
    classification misses or over-flags - bright desert sand is often marked
    as cloud - and leaves no patchwork where one scene's mask ends. Samples are
    kept as harmonised DN (reflectance * 10000 + 1000, uint16) so memory stays
    bounded however many scenes are read. Pixels without any clear sample use
    the first (cloudy) sample instead of staying empty.
    """

    def __init__(self, h: int, w: int, k: int = MEDIAN_SCENES):
        self.k = k
        self.samples = np.zeros((k, 3, h, w), np.uint16)
        self.count = np.zeros((h, w), np.uint8)
        self.fallback = np.zeros((3, h, w), np.uint16)
        self.land = np.zeros((h, w), dtype=bool)   # seen as vegetation / bare soil (stretch sample)
        self.used: list[str] = []

    def add(self, scene: dict, rgb: np.ndarray, scl: np.ndarray) -> None:
        refl = rgb.astype(np.float32) * scene["scale"] + scene["offset"]
        data = (rgb > 0).all(axis=0)
        if data.any() and np.median(refl[:, data]) < BROKEN_REFLECTANCE:
            return                       # broken granule (seen: all DN 128), not ground
        hdn = np.clip(refl * 10000.0 + 1000.0, 1, 65535).astype(np.uint16)
        del refl
        good = data & ~np.isin(scl, SCL_BAD) & (self.count < self.k)
        if good.any():
            for slot in range(self.k):
                sel = good & (self.count == slot)
                if sel.any():
                    self.samples[slot][:, sel] = hdn[:, sel]
            self.count[good] += 1
            self.land |= good & np.isin(scl, SCL_LAND)
            self.used.append(scene["id"])
        first = data & (self.fallback[0] == 0)
        self.fallback[:, first] = hdn[:, first]

    def needed(self, min_clear: int = MIN_CLEAR) -> np.ndarray:
        return self.count < min_clear

    def result(self) -> tuple[np.ndarray, np.ndarray]:
        """Reflectance float32 [3,h,w] (NaN where empty) + covered mask."""
        _, _, h, w = self.samples.shape
        refl = np.full((3, h, w), np.nan, dtype=np.float32)
        with np.errstate(all="ignore"), _quiet_nan_warnings():
            for r0 in range(0, h, STRIP):          # strips keep the float stack small
                r1 = min(h, r0 + STRIP)
                stack = self.samples[:, :, r0:r1].astype(np.float32)
                stack[stack == 0] = np.nan
                refl[:, r0:r1] = (np.nanmedian(stack, axis=0) - 1000.0) / 10000.0
        rest = (self.count == 0) & (self.fallback[0] > 0)
        refl[:, rest] = (self.fallback[:, rest].astype(np.float32) - 1000.0) / 10000.0
        return refl, (self.count > 0) | rest


def composite(reads, shape) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Composite of a list of (scene, rgb, scl) reads, best first."""
    acc = Composite(*shape)
    for scene, rgb, scl in reads:
        acc.add(scene, rgb, scl)
    refl, covered = acc.result()
    return refl, covered, acc.used


def footprint_mask(scene: dict, bounds, n: int = FOOTPRINT_GRID) -> np.ndarray:
    """n x n grid over `bounds` (EPSG:3857): True where the scene's STAC
    footprint polygon covers the cell centre (all True without a geometry)."""
    geom = scene.get("geometry") or {}
    if geom.get("type") == "Polygon":
        polygons = [geom.get("coordinates") or []]
    elif geom.get("type") == "MultiPolygon":
        polygons = geom.get("coordinates") or []
    else:
        return np.ones((n, n), dtype=bool)
    xs = bounds[0] + (np.arange(n) + 0.5) / n * (bounds[2] - bounds[0])
    ys = bounds[3] - (np.arange(n) + 0.5) / n * (bounds[3] - bounds[1])
    lon = np.broadcast_to(xs / WEB_MERCATOR_R * 180.0, (n, n))
    lat = np.degrees(np.arctan(np.sinh(ys / WEB_MERCATOR_R * np.pi)))[:, None] * np.ones((1, n))
    inside = np.zeros((n, n), dtype=bool)
    for polygon in polygons:
        if not polygon:
            continue
        ring = np.asarray(polygon[0], dtype=np.float64)[:, :2]   # exterior ring
        hit = np.zeros((n, n), dtype=bool)
        x1, y1 = ring[:-1, 0], ring[:-1, 1]
        x2, y2 = ring[1:, 0], ring[1:, 1]
        for ax, ay, bx, by in zip(x1, y1, x2, y2):
            if ay == by:
                continue
            crosses = (ay > lat) != (by > lat)
            xcross = ax + (lat - ay) * (bx - ax) / (by - ay)
            hit ^= crosses & (lon < xcross)
        inside |= hit
    return inside


def coarse_any(mask: np.ndarray, n: int = FOOTPRINT_GRID) -> np.ndarray:
    """n x n grid: True where any pixel of the cell is set."""
    h, w = mask.shape
    rows = np.linspace(0, h, n + 1).astype(int)
    cols = np.linspace(0, w, n + 1).astype(int)
    return np.logical_or.reduceat(np.logical_or.reduceat(mask, rows[:-1], axis=0), cols[:-1], axis=1)


def fill_block(scenes: list[dict], read: Callable, bounds, width: int, height: int,
               max_reads: int = MAX_READS, stopped: Callable[[], bool] = lambda: False,
               min_clear: int = MIN_CLEAR, k: int = MEDIAN_SCENES) -> Composite:
    """Read scenes until every pixel has `min_clear` clear samples.

    Coverage first, then depth: pass k (1..min_clear) walks the scenes best
    first but only reads one whose footprint reaches a pixel that still has
    fewer than k clear samples. Without this, the best-ranked scenes (often
    4 granules of one orbit, several dates) use up the read budget on one part
    of the block and leave the part only another orbit sees empty.
    """
    min_clear = min(min_clear, k)
    acc = Composite(height, width, k)
    footprints = [footprint_mask(scene, bounds) for scene in scenes]
    done: set[int] = set()
    reads = 0
    for level in range(1, min_clear + 1):
        for index, scene in enumerate(scenes):
            if reads >= max_reads or stopped():
                return acc
            need = acc.needed(level)
            if not need.any():
                break
            if index in done or not (footprints[index] & coarse_any(need)).any():
                continue
            done.add(index)
            rgb, scl = read(scene, bounds, width, height)
            reads += 1
            if rgb.any():
                acc.add(scene, rgb, scl)
    return acc


class _quiet_nan_warnings:
    def __enter__(self):
        import warnings
        self._ctx = warnings.catch_warnings()
        self._ctx.__enter__()
        warnings.simplefilter("ignore", RuntimeWarning)

    def __exit__(self, *exc):
        return self._ctx.__exit__(*exc)


def white_point(samples: list[np.ndarray]) -> float:
    values = np.concatenate([s[np.isfinite(s)].ravel() for s in samples]) if samples else np.empty(0)
    if values.size < 100:
        return 0.35
    # min(p98, 3.5 x median): a few snow / roof pixels can't darken the scene
    # (Tromso p98 0.43, median 0.07 -> 0.25); deserts keep p98 (Riyadh 0.50).
    white = min(np.percentile(values, WHITE_PERCENTILE), WHITE_MEDIAN_FACTOR * np.median(values))
    return float(np.clip(white, WHITE_MIN, WHITE_MAX))


KNEE = 0.75   # linear below 75 % of the white point, soft shoulder above


def to_rgb8(refl: np.ndarray, white: float) -> np.ndarray:
    """Reflectance -> 8 bit: linear, with a soft shoulder instead of a hard
    clip so bright sand / roofs keep their texture."""
    v = np.maximum(np.nan_to_num(refl, nan=0.0) / white, 0.0)
    span = 1.0 - KNEE
    v = np.where(v <= KNEE, v, KNEE + span * np.tanh((v - KNEE) / span))
    return (np.clip(v, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


# ---- tile writing ----------------------------------------------------------------

TILE_EXTS = ("jpg", "webp", "png")


def existing_tile(root: Path, z: int, x: int, y: int) -> Path | None:
    for ext in TILE_EXTS:
        path = root / str(z) / str(x) / f"{y}.{ext}"
        if path.is_file():
            return path
    return None


def write_tile(root: Path, z: int, x: int, y: int, rgb: np.ndarray, alpha: np.ndarray) -> int:
    """JPEG when fully covered, WebP+alpha when partial, nothing when empty."""
    from PIL import Image

    for ext in TILE_EXTS:   # a rebuilt tile may change format
        old = root / str(z) / str(x) / f"{y}.{ext}"
        if old.exists():
            old.unlink()
    if not alpha.any():
        return 0
    image = Image.fromarray(np.moveaxis(rgb, 0, -1), "RGB")
    buffer = io.BytesIO()
    if alpha.all():
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        ext = "jpg"
    else:
        image.putalpha(Image.fromarray(np.where(alpha, 255, 0).astype(np.uint8), "L"))
        image.save(buffer, format="WEBP", quality=WEBP_QUALITY, method=4)
        ext = "webp"
    target = root / str(z) / str(x) / f"{y}.{ext}"
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.part")
    temp.write_bytes(buffer.getvalue())
    os.chmod(temp, 0o644)
    temp.replace(target)
    return len(buffer.getvalue())


def build_overview_tile(root: Path, z: int, x: int, y: int) -> int:
    """Average the four z+1 children into tile (z, x, y)."""
    from PIL import Image

    canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    found = False
    for dx in (0, 1):
        for dy in (0, 1):
            child = existing_tile(root, z + 1, 2 * x + dx, 2 * y + dy)
            if child is None:
                continue
            try:
                with Image.open(child) as image:
                    canvas.paste(image.convert("RGBA"), (dx * 256, dy * 256))
                    found = True
            except (OSError, ValueError):
                continue
    if not found:
        write_tile(root, z, x, y, np.zeros((3, 256, 256), np.uint8), np.zeros((256, 256), bool))
        return 0
    arr = np.asarray(canvas, dtype=np.float32).reshape(256, 2, 256, 2, 4)
    alpha = arr[..., 3] / 255.0
    weight = alpha.sum(axis=(1, 3))
    rgb = (arr[..., :3] * alpha[..., None]).sum(axis=(1, 3)) / np.maximum(weight, 1e-6)[..., None]
    rgb8 = np.clip(rgb + 0.5, 0, 255).astype(np.uint8)
    covered = weight >= 0.999   # at least one fully imaged child pixel
    return write_tile(root, z, x, y, np.moveaxis(rgb8, -1, 0), covered)


# ---- job --------------------------------------------------------------------------

def build_set(
    root: Path,
    provider: str,
    bbox: list[float],
    minzoom: int,
    maxzoom: int,
    tile_range: Callable[[list[float], int], tuple[int, int, int, int]],
    log: Callable[[str], None],
    stop: threading.Event | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    max_cloud: float | None = None,
    workers: int | None = None,   # default 3 (Settings): ~350 MB each; data-platform limit 2 GB
) -> dict[str, Any]:
    """Build the set's tiles under `root`; returns stats for imagery.json."""
    import rasterio

    info = SCENE_PROVIDERS[provider]
    ok, reason = provider_status(provider)
    if not ok:
        raise RuntimeError(f"{provider} is not configured: {reason}")
    dflt_from, dflt_to = default_dates(days=int(_setting("s2Days", DEFAULT_DAYS)))
    date_from, date_to = date_from or dflt_from, date_to or dflt_to
    max_cloud = float(_setting("s2MaxCloud", DEFAULT_MAX_CLOUD)) if max_cloud is None else float(max_cloud)
    median = int(_setting("s2MedianScenes", MEDIAN_SCENES))
    if workers is None:
        workers = int(_setting("s2Workers", 3))
    signer = Signer(info)
    env = gdal_env(info)
    t0 = time.time()

    blocks = list(metatiles(tile_range(bbox, maxzoom)))
    log(f"  Sentinel-2 L2A from {info['stac']} ({info['collection']}), {date_from}..{date_to}, "
        f"cloud <= {max_cloud:g}%, {len(blocks)} block(s) of up to {META}x{META} z{maxzoom} tiles")

    def stopped() -> bool:
        return stop is not None and stop.is_set()

    def block_search(block):
        x, y, nx, ny = block
        b = tile_bounds(maxzoom, x, y, nx, ny)
        west, south = mercator_to_lonlat(b[0], b[1])
        east, north = mercator_to_lonlat(b[2], b[3])
        return search(info, [west, south, east, north], date_from, date_to, max_cloud)

    # 1. scenes per block (cheap JSON), 2. one white point from a preview
    scenes_by_block: dict[tuple, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(block_search, block): block for block in blocks}
        for future in as_completed(futures):
            if stopped():
                pool.shutdown(wait=True, cancel_futures=True)
                raise RuntimeError("download cancelled")
            scenes_by_block[futures[future]] = future.result()
    all_ids = {s["id"] for scenes in scenes_by_block.values() for s in scenes}
    if not all_ids:
        raise NoScenesError(
            f"no Sentinel-2 scene with cloud <= {max_cloud:g}% between {date_from} and {date_to} "
            "covers this area (open ocean is not imaged; try a wider date range or a higher cloud limit)")
    log(f"  {len(all_ids)} candidate scene(s) found in {time.time() - t0:.0f} s")

    white = None
    with rasterio.Env(**env):
        samples = []
        for block, scenes in sorted(scenes_by_block.items(), key=lambda kv: -len(kv[1]))[:3]:
            if not scenes:
                continue
            b = tile_bounds(maxzoom, *block)
            try:
                acc = fill_block(scenes, lambda sc, bb, w, h: read_scene(sc, signer, bb, w, h),
                                 b, 256, 256, max_reads=median, stopped=stopped, k=median)
            except Exception as exc:  # noqa: BLE001
                log(f"  preview read failed: {exc}")
                continue
            refl, covered = acc.result()
            land = covered & acc.land
            samples.append(refl[:, land if land.sum() >= 0.05 * land.size else covered])
        white = white_point(samples)
    log(f"  colour stretch: reflectance 0..{white:.3f} -> 0..255")

    # 3. max-zoom blocks (resumable: finished blocks are listed in .blocks-done)
    done_file = root / ".blocks-done"
    key = f"{MOSAIC_VERSION}|{provider}|{date_from}|{date_to}|{max_cloud:g}|{white:.3f}|m{median}|z{maxzoom}"
    done: set[str] = set()
    if done_file.is_file():
        lines = done_file.read_text(encoding="utf-8").splitlines()
        if lines and lines[0] == key:
            done = set(lines[1:])
    if not done:
        done_file.write_text(key + "\n", encoding="utf-8")
    done_lock = threading.Lock()
    stats = {"blocks": 0, "tiles": 0, "bytes": 0, "failed": 0, "scenes": set()}
    errors: list[str] = []

    def run_block(block) -> tuple[int, int, set[str]]:
        x, y, nx, ny = block
        tag = f"{x}/{y}/{nx}/{ny}"
        if tag in done or stopped():
            return 0, 0, set()
        w, h = nx * 256, ny * 256
        b = tile_bounds(maxzoom, x, y, nx, ny)
        with rasterio.Env(**env):
            acc = fill_block(scenes_by_block.get(block, []),
                             lambda sc, bb, ww, hh: read_scene(sc, signer, bb, ww, hh),
                             b, w, h, stopped=stopped, k=median)
        if stopped():
            return 0, 0, set()
        refl, covered = acc.result()
        used = acc.used
        del acc
        rgb8 = to_rgb8(refl, white)
        tiles = size = 0
        for i in range(nx):
            for j in range(ny):
                sl = (slice(j * 256, (j + 1) * 256), slice(i * 256, (i + 1) * 256))
                written = write_tile(root, maxzoom, x + i, y + j, rgb8[:, sl[0], sl[1]], covered[sl])
                if written:
                    tiles += 1
                    size += written
        with done_lock:
            with done_file.open("a", encoding="utf-8") as fh:
                fh.write(tag + "\n")
        return tiles, size, set(used)

    pending = [b for b in blocks if f"{b[0]}/{b[1]}/{b[2]}/{b[3]}" not in done]
    if len(pending) < len(blocks):
        log(f"  {len(blocks) - len(pending)} block(s) already built, kept")
    last_log = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(run_block, block): block for block in pending}
        for n, future in enumerate(as_completed(futures), 1):
            if stopped():
                pool.shutdown(wait=True, cancel_futures=True)
                log("  download cancelled")
                raise RuntimeError("download cancelled")
            try:
                tiles, size, used = future.result()
                stats["tiles"] += tiles
                stats["bytes"] += size
                stats["scenes"] |= used
            except Exception as exc:  # noqa: BLE001
                stats["failed"] += 1
                if len(errors) < 10:
                    errors.append(str(exc))
                log(f"  FAILED block {futures[future]}: {exc}")
            if time.time() - last_log >= 3 or n == len(futures):
                last_log = time.time()
                log(f"  built {n}/{len(futures)} block(s), {stats['tiles']} z{maxzoom} tile(s), "
                    f"{stats['bytes'] / 1e6:.1f} MB, {time.time() - t0:.0f} s")
    if stats["failed"]:
        raise RuntimeError(f"{stats['failed']} block(s) failed: {errors[0]} (re-run the job to retry them)")

    # 4. lower zooms from their children
    for z in range(maxzoom - 1, minzoom - 1, -1):
        if stopped():
            raise RuntimeError("download cancelled")
        x0, y0, x1, y1 = tile_range(bbox, z)
        count = 0
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                if build_overview_tile(root, z, x, y):
                    count += 1
        log(f"  z{z}: {count} overview tile(s)")
    return {
        "white": white,
        "dateFrom": date_from,
        "dateTo": date_to,
        "maxCloud": max_cloud,
        "scenes": len(stats["scenes"]),
        "sceneIds": sorted(stats["scenes"])[:200],
        "attribution": ATTRIBUTION.format(year=date_to[:4] if date_from[:4] == date_to[:4]
                                          else f"{date_from[:4]}-{date_to[:4]}"),
    }
