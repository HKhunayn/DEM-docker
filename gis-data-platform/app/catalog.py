from __future__ import annotations
import json
import mimetypes
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CATALOG_PATH, SOURCE_ROOT

# The catalog is read-modify-written from API requests and job threads; every
# edit holds this lock and writes through a unique temp file before replacing.
_EDIT_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def load_catalog() -> dict[str, Any]:
    if not CATALOG_PATH.exists():
        return {"type": "Catalog", "id": "local-gis", "version": "1.0.0", "description": "Local geospatial catalog", "items": []}
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def save_catalog(catalog: dict[str, Any]) -> None:
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _EDIT_LOCK:
        temp = CATALOG_PATH.with_name(f"{CATALOG_PATH.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
        temp.replace(CATALOG_PATH)

def catalog_items() -> list[dict[str, Any]]:
    return load_catalog().get("items", [])

def item_path(item: dict[str, Any]) -> Path:
    return SOURCE_ROOT / item["id"] / item["filename"]

def ingest_file(source: Path, kind: str = "unknown", title: str | None = None, crs: str | None = None) -> dict[str, Any]:
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(str(source))
    item_id = uuid.uuid4().hex
    destination = SOURCE_ROOT / item_id / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    mime, _ = mimetypes.guess_type(source.name)
    item: dict[str, Any] = {
        "type": "Feature",
        "id": item_id,
        "title": title or source.stem,
        "kind": kind,
        "filename": source.name,
        "mediaType": mime or "application/octet-stream",
        "sizeBytes": destination.stat().st_size,
        "created": _now(),
        "crs": crs,
        "sourcePath": str(source),
        "assets": {
            "source": {
                "href": f"/api/assets/{item_id}",
                "type": mime or "application/octet-stream",
                "roles": [kind if kind != "unknown" else "data"],
            }
        },
        "links": [],
    }
    with _EDIT_LOCK:
        catalog = load_catalog()
        catalog["items"] = [*catalog.get("items", []), item]
        save_catalog(catalog)
    return item

def delete_item(item_id: str) -> bool:
    with _EDIT_LOCK:
        catalog = load_catalog()
        old = catalog.get("items", [])
        kept = [item for item in old if item.get("id") != item_id]
        if len(kept) == len(old):
            return False
        item_dir = SOURCE_ROOT / item_id
        if item_dir.exists():
            shutil.rmtree(item_dir)
        catalog["items"] = kept
        save_catalog(catalog)
    return True

def find_item(item_id: str) -> dict[str, Any] | None:
    return next((item for item in catalog_items() if item.get("id") == item_id), None)


# ---- saved areas (fetched dataset footprints, persisted in the catalog) ----

def list_areas() -> list[dict[str, Any]]:
    return load_catalog().get("areas", [])


def find_area(area_id: str) -> dict[str, Any] | None:
    return next((area for area in list_areas() if area.get("id") == area_id), None)


def create_area(
    name: str,
    bbox: list[float],
    provider: str,
    minzoom: int,
    maxzoom: int,
    dataset: dict[str, Any] | None = None,
    kind: str = "elevation",
) -> dict[str, Any]:
    now = _now()
    area: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "name": name,
        "kind": kind,
        "bbox": list(bbox),
        "provider": provider,
        "minzoom": minzoom,
        "maxzoom": maxzoom,
        "crs": "EPSG:4326",
        "dataset": dataset,
        "created": now,
        "updated": now,
    }
    with _EDIT_LOCK:
        catalog = load_catalog()
        catalog["areas"] = [*catalog.get("areas", []), area]
        save_catalog(catalog)
    return area


def upsert_area(
    name: str,
    bbox: list[float],
    provider: str,
    minzoom: int,
    maxzoom: int,
    dataset: dict[str, Any] | None = None,
    kind: str = "elevation",
) -> dict[str, Any]:
    """Save a fetch result without duplicating: a saved area is identified by
    (name, provider), so re-running a fetch refreshes that area instead of
    leaving a new row with the same name in the viewer's selection list."""
    with _EDIT_LOCK:
        catalog = load_catalog()
        existing = next(
            (a for a in catalog.get("areas", []) if a.get("name") == name and a.get("provider") == provider),
            None,
        )
        now = _now()
        if existing:
            existing.update({
                "bbox": list(bbox),
                "minzoom": minzoom,
                "maxzoom": maxzoom,
                "dataset": dataset,
                "kind": kind,
                "updated": now,
            })
            save_catalog(catalog)
            return existing
    return create_area(name, bbox, provider, minzoom, maxzoom, dataset, kind)


def update_area(area_id: str, **fields: Any) -> dict[str, Any] | None:
    with _EDIT_LOCK:
        catalog = load_catalog()
        for area in catalog.get("areas", []):
            if area.get("id") == area_id:
                area.update(fields)
                area["updated"] = _now()
                save_catalog(catalog)
                return area
    return None


def delete_area(area_id: str) -> bool:
    with _EDIT_LOCK:
        catalog = load_catalog()
        old = catalog.get("areas", [])
        kept = [area for area in old if area.get("id") != area_id]
        if len(kept) == len(old):
            return False
        catalog["areas"] = kept
        save_catalog(catalog)
    return True
