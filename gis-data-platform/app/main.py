from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .catalog import catalog_items, delete_item, find_item, ingest_file, item_path, load_catalog
from .config import TILE_ROOT, public_base
from .fetch_api import router as fetch_router
from .imagery_api import router as imagery_router
from .terrain import TerrainSource

app = FastAPI(title="Local Open GIS Data Platform", version="0.1.0")
app.include_router(fetch_router)
app.include_router(imagery_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)

# Derived tile pyramids + ArcGIS services live under the platform's own data
# root (data/tiles/), alongside data/dem/ (source GeoTIFFs) and data/catalog/.
# The heightmap-30m engine is only the build tool; it no longer owns storage.
TILE_DIR = TILE_ROOT
TERRAIN = TerrainSource(TILE_DIR)


class IngestRequest(BaseModel):
    path: str
    kind: str = Field(default="unknown", pattern=r"^[A-Za-z0-9_-]{1,32}$")
    title: str | None = None
    crs: str | None = None


class DeleteResponse(BaseModel):
    deleted: bool


def _finite(obj: Any) -> Any:
    """Recursively replace non-finite floats (inf/nan) with None.

    JSON has no infinity/NaN. A degenerate build (e.g. zero tiles written,
    so the tile statistics stay at math.inf) must not 500 the endpoints that
    serve metadata read from disk.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_finite(v) for v in obj]
    return obj


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _tile_file(relative: str) -> Path | None:
    candidate = (TILE_DIR / relative).resolve()
    try:
        candidate.relative_to(TILE_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "gis-data-platform", "version": app.version}


@app.get("/api/catalog")
def catalog(request: Request) -> dict[str, Any]:
    result = load_catalog()
    result["links"] = [
        {"rel": "self", "href": f"{_base(request)}/api/catalog"},
        {"rel": "items", "href": f"{_base(request)}/api/items"},
    ]
    return result


@app.get("/api/items")
def items(request: Request) -> dict[str, Any]:
    features = []
    for item in catalog_items():
        feature = dict(item)
        feature["assets"] = dict(feature.get("assets", {}))
        if "source" in feature["assets"]:
            feature["assets"]["source"] = dict(feature["assets"]["source"])
            feature["assets"]["source"]["href"] = f"{_base(request)}/api/assets/{item['id']}"
        features.append(feature)
    return {"type": "FeatureCollection", "features": features, "numberMatched": len(features), "numberReturned": len(features)}


@app.post("/api/ingest", status_code=201)
def ingest(body: IngestRequest, request: Request) -> dict[str, Any]:
    try:
        item = ingest_file(Path(body.path), body.kind, body.title, body.crs)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="source path does not exist")
    item["assets"]["source"]["href"] = f"{_base(request)}/api/assets/{item['id']}"
    return item


@app.delete("/api/items/{item_id}", response_model=DeleteResponse)
def remove(item_id: str) -> DeleteResponse:
    if not delete_item(item_id):
        raise HTTPException(status_code=404, detail="item not found")
    return DeleteResponse(deleted=True)


@app.get("/api/assets/{item_id}")
def asset(item_id: str) -> FileResponse:
    item = find_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    path = item_path(item)
    if not path.exists():
        raise HTTPException(status_code=410, detail="asset is missing")
    return FileResponse(path, media_type=item.get("mediaType"), filename=item["filename"])


@app.get("/api/services")
def services(request: Request) -> dict[str, Any]:
    result: list[dict[str, Any]] = []
    service_root = TILE_DIR / "arcgis" / "rest" / "services"
    if service_root.is_dir():
        for meta in sorted(service_root.glob("*/ImageServer.json")):
            name = meta.parent.name
            result.append({"name": name, "url": f"/arcgis/rest/services/{name}/ImageServer"})
    return {"services": result}


def _png_bytes(image: Any) -> bytes:
    import io
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


_TRANSPARENT_TILE: bytes | None = None


def _transparent_tile() -> bytes:
    global _TRANSPARENT_TILE
    if _TRANSPARENT_TILE is None:
        from PIL import Image
        _TRANSPARENT_TILE = _png_bytes(Image.new("RGBA", (256, 256), (0, 0, 0, 0)))
    return _TRANSPARENT_TILE


# Tiles change whenever a new build runs ("last build wins"), so they must be
# revalidated, never cached as immutable.
_REVALIDATE = "no-cache"


@app.get("/tiles/terrain/{z}/{x}/{y}.png")
def terrain(z: int, x: int, y: int) -> Response:
    if z < 0 or z > 24 or not (0 <= x < 2 ** z) or not (0 <= y < 2 ** z):
        raise HTTPException(status_code=404, detail="terrain tile not found")
    # Seamless field: real heights inside coverage, edge heights eased to a
    # flat base outside. Above maxzoom -> 404 so clients over-zoom the parent.
    payload = TERRAIN.tile_png(z, x, y)
    if payload is None:
        raise HTTPException(status_code=404, detail="above terrain maxzoom")
    response = Response(content=payload, media_type="image/png")
    response.headers["Cache-Control"] = _REVALIDATE
    return response


@app.get("/tiles/terrain.json")
def terrain_tilejson(request: Request) -> JSONResponse:
    old = TILE_DIR / "tilejson.json"
    if old.exists():
        import json
        data = json.loads(old.read_text(encoding="utf-8"))
    else:
        data = {"tilejson": "3.0.0", "name": "local terrain", "minzoom": 0, "maxzoom": 13, "encoding": "terrarium", "tileSize": 256}
    # Versioned URLs: the token changes with every build (and with the tile
    # algorithm revision), so browsers never reuse stale tiles - including
    # copies cached as `immutable` by earlier platform versions or saved
    # without CORS headers by a non-CORS request.
    version = TERRAIN.version()
    data["tiles"] = [f"{_base(request)}/tiles/terrain/{{z}}/{{x}}/{{y}}.png?v={version}"]
    data["imagery"] = [f"{_base(request)}/tiles/imagery/{{z}}/{{x}}/{{y}}.png?v={version}"]
    response = JSONResponse(_finite(data))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/tiles/imagery/{z}/{x}/{y}.png")
def imagery(z: int, x: int, y: int) -> Response:
    # Hillshade drape for the current build only. Tiles from older builds
    # outside the current bounds (or above maxzoom) are not served: they would
    # float over unrelated terrain. Empty coverage is a 256x256 transparent
    # PNG (a 1x1 image is not a usable web-mercator tile for CesiumJS).
    path = _tile_file(f"hillshade/{z}/{x}/{y}.png") if TERRAIN.tile_in_coverage(z, x, y) else None
    if path:
        response: Response = Response(content=TERRAIN.hillshade_png(path, z, x, y), media_type="image/png")
    else:
        response = Response(content=_transparent_tile(), media_type="image/png")
    response.headers["Cache-Control"] = _REVALIDATE
    return response


@app.get("/arcgis/rest/services/{name}/ImageServer")
def arcgis_metadata(name: str) -> JSONResponse:
    import json
    path = _tile_file(f"arcgis/rest/services/{name}/ImageServer.json")
    if not path:
        raise HTTPException(status_code=404, detail="ImageServer not found")
    data = json.loads(path.read_text(encoding="utf-8"))
    # Real ImageServers expose `extent`; CesiumJS's
    # ArcGISTiledElevationTerrainProvider requires it (reads extent.xmax).
    if "extent" not in data and "fullExtent" in data:
        data["extent"] = data["fullExtent"]
    return JSONResponse(_finite(data))


@app.get("/arcgis/rest/services/{name}/ImageServer/tilemap/{level}/{row}/{col}/{height}/{width}")
def arcgis_tilemap(name: str, level: int, row: int, col: int, height: int, width: int) -> JSONResponse:
    # The service metadata advertises the "Tilemap" capability; CesiumJS (and
    # the ArcGIS SDK) request this before any tile. A 404 here makes CesiumJS
    # reject every terrain tile, leaving an empty globe.
    if not (0 <= level <= 24 and 0 < height <= 256 and 0 < width <= 256 and row >= 0 and col >= 0):
        raise HTTPException(status_code=400, detail="invalid tilemap request")
    root = _tile_file(f"arcgis/rest/services/{name}/ImageServer.json")
    if not root:
        raise HTTPException(status_code=404, detail="ImageServer not found")
    level_dir = root.parent / "tile" / str(level)
    present: set[tuple[int, int]] = set()
    if level_dir.is_dir():
        for row_dir in level_dir.iterdir():
            if row_dir.is_dir() and row_dir.name.isdigit() and row <= int(row_dir.name) < row + height:
                r = int(row_dir.name)
                for tile in row_dir.iterdir():
                    if tile.is_file() and tile.name.isdigit() and col <= int(tile.name) < col + width:
                        present.add((r, int(tile.name)))
    data = [1 if (r, c) in present else 0 for r in range(row, row + height) for c in range(col, col + width)]
    return JSONResponse({"adjusted": False, "location": {"left": col, "top": row, "width": width, "height": height}, "data": data})


@app.get("/arcgis/rest/services/{name}/ImageServer/tile/{z}/{row}/{col}")
def arcgis_tile(name: str, z: int, row: int, col: int) -> FileResponse:
    path = _tile_file(f"arcgis/rest/services/{name}/tile/{z}/{row}/{col}")
    if not path:
        raise HTTPException(status_code=404, detail="ArcGIS tile not found")
    response = FileResponse(path, media_type="application/octet-stream")
    response.headers["Cache-Control"] = _REVALIDATE
    return response


@app.get("/")
def root() -> dict[str, str]:
    return {"service": app.title, "docs": "/docs", "health": "/health", "catalog": "/api/catalog", "fetch": "/fetch/", "imagery": "/api/imagery"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8787, reload=False)
