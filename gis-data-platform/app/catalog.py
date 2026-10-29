from __future__ import annotations
import contextlib
import json
import mimetypes
import os
import shutil
import stat
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CATALOG_LOCK_PATH, CATALOG_PATH, SOURCE_ROOT, ingest_max_bytes, ingest_root

try:  # POSIX (the containers); a Windows dev run only has the thread lock
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

TITLE_MAX_LEN = 200
CRS_MAX_LEN = 100


class NameConflictError(Exception):
    """A saved area / imagery set name is already used with another provider (HTTP 409)."""

# The catalog is read-modify-written from API requests and job threads of TWO
# processes (data-platform + engine containers share the volume). Every edit
# holds edit_lock(): a thread RLock plus an exclusive flock on
# catalog/.catalog.lock, and writes through a unique temp file + os.replace,
# so readers never see a partial file and no process loses another's edit.
_EDIT_LOCK = threading.RLock()
_LOCAL = threading.local()


@contextlib.contextmanager
def edit_lock():
    """Cross-thread + cross-process lock around a catalog load-modify-write.
    Re-entrant within a thread (the flock is taken by the outermost call)."""
    with _EDIT_LOCK:
        depth = getattr(_LOCAL, "depth", 0)
        handle = None
        if depth == 0 and fcntl is not None:
            CATALOG_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            handle = open(CATALOG_LOCK_PATH, "a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _LOCAL.depth = depth
            if handle is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def load_catalog() -> dict[str, Any]:
    if not CATALOG_PATH.exists():
        return {"type": "Catalog", "id": "local-gis", "version": "1.0.0", "description": "Local geospatial catalog", "items": []}
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def save_catalog(catalog: dict[str, Any]) -> None:
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with edit_lock():
        temp = CATALOG_PATH.with_name(f"{CATALOG_PATH.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
        temp.replace(CATALOG_PATH)

def catalog_items() -> list[dict[str, Any]]:
    return load_catalog().get("items", [])

def item_path(item: dict[str, Any]) -> Path:
    return SOURCE_ROOT / item["id"] / item["filename"]

def _clean_text(value: str | None, field: str, max_len: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if len(value) > max_len:
        raise ValueError(f"{field} is too long (max {max_len} characters)")
    if any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value):
        raise ValueError(f"{field} must not contain control characters")
    return value or None


def resolve_ingest_source(source: Path | str) -> Path:
    """Resolve an ingest path inside the allow-listed ingest root.

    Relative paths are taken relative to the root. Raises PermissionError for
    anything resolving outside the root (including symlinks that escape it),
    FileNotFoundError when it does not exist, ValueError for non-regular files,
    oversized files or malformed paths.
    """
    raw = str(source)
    if not raw.strip() or "\x00" in raw:
        raise ValueError("invalid source path")
    root = ingest_root().resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()  # follows symlinks: an escaping link resolves outside
    if not resolved.is_relative_to(root):
        raise PermissionError(f"ingest is restricted to files under {root}")
    try:
        info = resolved.stat()
    except FileNotFoundError:
        raise FileNotFoundError(str(source)) from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("source path is not a regular file")
    limit = ingest_max_bytes()
    if info.st_size > limit:
        raise ValueError(f"source file is too large ({info.st_size} bytes, limit {limit})")
    return resolved


def ingest_file(source: Path, kind: str = "unknown", title: str | None = None, crs: str | None = None) -> dict[str, Any]:
    title = _clean_text(title, "title", TITLE_MAX_LEN)
    crs = _clean_text(crs, "crs", CRS_MAX_LEN)
    source = resolve_ingest_source(source)
    item_id = uuid.uuid4().hex
    destination = SOURCE_ROOT / item_id / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    try:
        shutil.copy2(source, partial)
        os.replace(partial, destination)
    except BaseException:
        shutil.rmtree(destination.parent, ignore_errors=True)
        raise
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
    with edit_lock():
        catalog = load_catalog()
        catalog["items"] = [*catalog.get("items", []), item]
        save_catalog(catalog)
    return item

def delete_item(item_id: str) -> bool:
    with edit_lock():
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


def find_area_by_name(name: str, kind: str = "elevation", provider: str | None = None) -> dict[str, Any] | None:
    """The saved area called `name` (newest first when several providers)."""
    matches = [a for a in list_areas() if a.get("name") == name and (a.get("kind") or "elevation") == kind
               and (provider is None or a.get("provider") == provider)]
    matches.sort(key=lambda a: str(a.get("updated") or ""), reverse=True)
    return matches[0] if matches else None


def update_area_dataset(area_id: str, if_state: str | None = None, **fields: Any) -> dict[str, Any] | None:
    """Merge `fields` into the area's dataset (other dataset keys are kept);
    a None value removes the key. With `if_state` the change is only applied
    while the dataset is still in that state (another process may have moved
    it on meanwhile, e.g. the engine started building it)."""
    with edit_lock():
        catalog = load_catalog()
        for area in catalog.get("areas", []):
            if area.get("id") == area_id:
                dataset = dict(area.get("dataset") or {})
                if if_state is not None and dataset.get("state") != if_state:
                    return area
                for key, value in fields.items():
                    if value is None:
                        dataset.pop(key, None)
                    else:
                        dataset[key] = value
                area["dataset"] = dataset
                area["updated"] = _now()
                save_catalog(catalog)
                return area
    return None


def _area_kind(area: dict[str, Any]) -> str:
    return area.get("kind") or "elevation"


def check_area_provider(name: str, provider: str, kind: str = "elevation") -> None:
    """Raise NameConflictError when `name` is saved with a different provider.

    Areas are keyed (name, provider), but the ArcGIS service dir / imagery set
    dir is keyed by name only, so two providers would overwrite each other.
    """
    for area in list_areas():
        if area.get("name") == name and _area_kind(area) == kind and area.get("provider") != provider:
            raise NameConflictError(
                f"{'imagery set' if kind == 'imagery' else 'area'} '{name}' already exists with provider "
                f"'{area.get('provider')}'; delete it first or choose another name"
            )


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
    with edit_lock():
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
    with edit_lock():
        check_area_provider(name, provider, kind)
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
    with edit_lock():
        catalog = load_catalog()
        for area in catalog.get("areas", []):
            if area.get("id") == area_id:
                area.update(fields)
                area["updated"] = _now()
                save_catalog(catalog)
                return area
    return None


def delete_area(area_id: str) -> bool:
    with edit_lock():
        catalog = load_catalog()
        old = catalog.get("areas", [])
        kept = [area for area in old if area.get("id") != area_id]
        if len(kept) == len(old):
            return False
        catalog["areas"] = kept
        save_catalog(catalog)
    return True


def delete_areas(name: str, kind: str) -> int:
    """Remove every saved area called `name` of `kind`; returns how many."""
    with edit_lock():
        catalog = load_catalog()
        old = catalog.get("areas", [])
        kept = [a for a in old if not (a.get("name") == name and _area_kind(a) == kind)]
        if len(kept) != len(old):
            catalog["areas"] = kept
            save_catalog(catalog)
        return len(old) - len(kept)


def mark_interrupted_areas(message: str, states: tuple[str, ...] = ("building",)) -> int:
    """Startup: datasets left in one of `states` by a dead job -> 'interrupted'
    (each role passes only the states its own jobs write)."""
    with edit_lock():
        if not CATALOG_PATH.exists():
            return 0
        catalog = load_catalog()
        changed = 0
        for area in catalog.get("areas", []):
            dataset = area.get("dataset")
            if isinstance(dataset, dict) and dataset.get("state") in states:
                dataset["state"] = "interrupted"
                dataset["error"] = message
                area["updated"] = _now()
                changed += 1
        if changed:
            save_catalog(catalog)
        return changed
