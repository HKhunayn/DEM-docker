from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.environ.get("GIS_DATA_ROOT", ROOT / "data"))
SOURCE_ROOT = DATA_ROOT / "sources"
CATALOG_ROOT = DATA_ROOT / "catalog"
DEM_ROOT = DATA_ROOT / "dem"          # source DEM GeoTIFFs + manifest.json
TILE_ROOT = DATA_ROOT / "tiles"       # derived: terrarium/, hillshade/, arcgis/, tilejson.json
IMAGERY_ROOT = DATA_ROOT / "imagery"  # fetched imagery sets: <name>/{z}/{x}/{y}.jpg + imagery.json
CATALOG_PATH = CATALOG_ROOT / "catalog.json"

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

for directory in (SOURCE_ROOT, CATALOG_ROOT, DEM_ROOT, TILE_ROOT, IMAGERY_ROOT):
    directory.mkdir(parents=True, exist_ok=True)

SUPPORTED_KIND_HINTS = {
    "elevation": {"geotiff", "cog", "dem", "float32"},
    "imagery": {"geotiff", "cog", "jpeg", "png", "webp"},
    "vector": {"geojson", "geopackage", "shapefile", "kml", "gml"},
    "model": {"gltf", "glb", "3dtiles", "obj", "las", "laz"},
}


def public_base(request_base: str = "") -> str:
    return request_base.rstrip("/")
