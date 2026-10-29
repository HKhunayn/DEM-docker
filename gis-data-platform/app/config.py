from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.environ.get("GIS_DATA_ROOT", ROOT / "data"))
SOURCE_ROOT = DATA_ROOT / "sources"
CATALOG_ROOT = DATA_ROOT / "catalog"
DEM_ROOT = DATA_ROOT / "dem"          # source DEM GeoTIFFs + manifest.json
TILE_ROOT = DATA_ROOT / "tiles"       # derived: published/, arcgis/, tilejson.json (+ terrarium/ until published)
IMAGERY_ROOT = DATA_ROOT / "imagery"  # fetched imagery sets: <name>/{z}/{x}/{y}.jpg + imagery.json
CATALOG_PATH = CATALOG_ROOT / "catalog.json"
CATALOG_LOCK_PATH = CATALOG_ROOT / ".catalog.lock"  # flock: catalog edits across processes
INCOMING_ROOT = DATA_ROOT / "incoming"   # default allow-listed directory for POST /api/ingest

# Which app this process is (one image, two containers sharing the volume):
#   platform - downloads only: fetch page, saved areas, DEM/imagery download
#              jobs; hands a downloaded area to the engine (ENGINE_URL)
#   engine   - builds (check -> build -> verify -> publish, never downloads)
#              and serves tiles/metadata (behind nginx on port 5000)
ROLES = ("engine", "platform")
ROLE = (os.environ.get("GIS_ROLE") or "engine").strip().lower()
if ROLE not in ROLES:
    raise RuntimeError(f"GIS_ROLE must be one of {ROLES}, not {ROLE!r}")

# Each role keeps its own job history + engine-process lock so the two
# containers never load or block on each other's jobs.
if ROLE == "platform":
    JOBS_ROOT = DATA_ROOT / "jobs"               # download jobs: <id>.json + <id>.log
    JOB_LOCK_PATH = DATA_ROOT / ".job.lock"      # PID of the running download subprocess
else:
    JOBS_ROOT = DATA_ROOT / "engine-jobs"        # build jobs: <id>.json + <id>.log
    JOB_LOCK_PATH = DATA_ROOT / ".engine-job.lock"

# Platform -> engine hand-off (compose network). Public URL of the engine for
# links shown on the fetch page: ENGINE_PUBLIC_URL, else the page's host name
# with ENGINE_PUBLIC_PORT (the published engine port, default 5000).
ENGINE_URL = (os.environ.get("ENGINE_URL") or "http://engine:8787").rstrip("/")
ENGINE_PUBLIC_URL = (os.environ.get("ENGINE_PUBLIC_URL") or "").rstrip("/")
ENGINE_PUBLIC_PORT = os.environ.get("ENGINE_PUBLIC_PORT") or "5000"


def engine_timeout_s() -> float:
    try:
        return float(os.environ.get("ENGINE_TIMEOUT_S", "5"))
    except ValueError:
        return 5.0


def ingest_root() -> Path:
    """Directory POST /api/ingest may read from (env GIS_INGEST_ROOT overrides)."""
    configured = os.environ.get("GIS_INGEST_ROOT")
    return Path(configured) if configured else INCOMING_ROOT


def ingest_max_bytes() -> int:
    """Largest file POST /api/ingest accepts (env GIS_INGEST_MAX_BYTES, default 20 GB)."""
    try:
        return int(os.environ.get("GIS_INGEST_MAX_BYTES", str(20 * 1024 ** 3)))
    except ValueError:
        return 20 * 1024 ** 3

# heightmap-30m engine: the existing fetch/build/verify CLI reused for jobs.
# Resolution order: explicit env var -> engine bundled with this project
# (gis-data-platform/engine, baked into the Docker image) -> legacy checkout.
def _default_engine_root() -> Path:
    bundled = ROOT / "engine"
    if (bundled / "terrainhub.py").is_file():
        return bundled
    return Path(r"C:\Users\hisham\heightmap-30m")


ENGINE_ROOT = Path(
    os.environ.get("HEIGHTMAP_ENGINE_ROOT")
    or os.environ.get("LEGACY_HEIGHTMAP_ROOT")
    or _default_engine_root()
)
HEIGHTMAP_PYTHON = os.environ.get("HEIGHTMAP_PYTHON")


# Leaflet is packaged separately in the container. Keep the engine path as a
# local-development fallback so the fetch page works in both environments.
def _default_leaflet_root() -> Path:
    for candidate in (
        ENGINE_ROOT / "node_modules" / "leaflet",
        Path(r"C:\Users\hisham\heightmap-30m") / "node_modules" / "leaflet",
    ):
        if candidate.exists():
            return candidate
    return ENGINE_ROOT / "node_modules" / "leaflet"


LEAFLET_ROOT = Path(os.environ.get("LEAFLET_ROOT") or _default_leaflet_root())


def engine_python() -> Path:
    if HEIGHTMAP_PYTHON:
        return Path(HEIGHTMAP_PYTHON)
    for candidate in (
        ENGINE_ROOT / ".venv" / "Scripts" / "python.exe",
        ENGINE_ROOT / ".venv" / "bin" / "python",
    ):
        if candidate.exists():
            return candidate
    return Path(sys.executable)

for directory in (SOURCE_ROOT, CATALOG_ROOT, DEM_ROOT, TILE_ROOT, IMAGERY_ROOT, JOBS_ROOT, INCOMING_ROOT):
    directory.mkdir(parents=True, exist_ok=True)

SUPPORTED_KIND_HINTS = {
    "elevation": {"geotiff", "cog", "dem", "float32"},
    "imagery": {"geotiff", "cog", "jpeg", "png", "webp"},
    "vector": {"geojson", "geopackage", "shapefile", "kml", "gml"},
    "model": {"gltf", "glb", "3dtiles", "obj", "las", "laz"},
}


def public_base(request_base: str = "") -> str:
    return request_base.rstrip("/")
