"""ArcGIS ImageServer routes: metadata, tilemap, tiles, sanitizing, traversal."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

NAME = "Terrain3D"


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("GIS_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("LEGACY_HEIGHTMAP_ROOT", str(tmp_path / "legacy"))
    import app.config
    import app.catalog
    import app.main
    importlib.reload(app.config)
    importlib.reload(app.catalog)
    return importlib.reload(app.main)


@pytest.fixture()
def arcgis(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    service = main.TILE_DIR / "arcgis" / "rest" / "services" / NAME
    service.mkdir(parents=True)
    (service / "ImageServer.json").write_text(
        '{"name": "Terrain3D", "fullExtent": {"xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10},'
        ' "minValues": [Infinity], "maxValues": [NaN], "capabilities": "Image,Tilemap"}', encoding="utf-8")
    for row, col in [(3, 4), (3, 6), (5, 4), (9, 9)]:
        d = service / "tile" / "7" / str(row)
        d.mkdir(parents=True, exist_ok=True)
        (d / str(col)).write_bytes(b"LERC" + bytes([row, col]))
    # a file at the level of row dirs (must be ignored), and a non-numeric entry
    (service / "tile" / "7" / "README").write_text("x", encoding="utf-8")
    (service / "tile" / "7" / "3" / "junk").write_text("x", encoding="utf-8")
    # something outside the services dir that traversal would reach
    (main.TILE_DIR / "arcgis" / "rest" / "ImageServer.json").write_text('{"secret": true}', encoding="utf-8")
    return main, service, TestClient(main.app)


def test_metadata_sanitizes_non_finite_and_adds_extent(arcgis):
    _, _, client = arcgis
    response = client.get(f"/arcgis/rest/services/{NAME}/ImageServer")
    assert response.status_code == 200
    data = response.json()
    assert data["minValues"] == [None] and data["maxValues"] == [None]
    assert data["extent"] == data["fullExtent"]
    assert client.get("/api/services").json()["services"][0]["name"] == NAME


def test_tilemap_reports_present_tiles(arcgis):
    _, _, client = arcgis
    response = client.get(f"/arcgis/rest/services/{NAME}/ImageServer/tilemap/7/3/4/3/3")
    assert response.status_code == 200
    body = response.json()
    assert body["location"] == {"left": 4, "top": 3, "width": 3, "height": 3}
    # rows 3..5 x cols 4..6
    assert body["data"] == [1, 0, 1,
                            0, 0, 0,
                            1, 0, 0]
    empty = client.get(f"/arcgis/rest/services/{NAME}/ImageServer/tilemap/8/0/0/4/4").json()
    assert empty["data"] == [0] * 16


def test_tilemap_cache_invalidated_by_new_build(arcgis):
    import os
    import time
    _, service, client = arcgis
    url = f"/arcgis/rest/services/{NAME}/ImageServer/tilemap/7/9/9/1/2"
    assert client.get(url).json()["data"] == [1, 0]
    (service / "tile" / "7" / "9" / "10").write_bytes(b"new")
    later = time.time() + 5
    os.utime(service / "ImageServer.json", (later, later))  # build finished
    assert client.get(url).json()["data"] == [1, 1]


def test_tilemap_rejects_bad_sizes(arcgis):
    _, _, client = arcgis
    assert client.get(f"/arcgis/rest/services/{NAME}/ImageServer/tilemap/7/0/0/0/4").status_code == 400
    assert client.get(f"/arcgis/rest/services/{NAME}/ImageServer/tilemap/7/0/0/4/999").status_code == 400
    assert client.get("/arcgis/rest/services/Nope/ImageServer/tilemap/7/0/0/4/4").status_code == 404


def test_tile_endpoint(arcgis):
    _, _, client = arcgis
    response = client.get(f"/arcgis/rest/services/{NAME}/ImageServer/tile/7/3/4")
    assert response.status_code == 200
    assert response.content == b"LERC\x03\x04"
    assert "max-age" in response.headers["cache-control"]
    assert "immutable" not in response.headers["cache-control"]
    missing = client.get(f"/arcgis/rest/services/{NAME}/ImageServer/tile/7/3/5")
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("name", ["..", "%2e%2e", "%2E%2E", ".", "a..b", "..%2f..", "%2e%2e%2f%2e%2e"])
def test_service_name_traversal_is_rejected(arcgis, name):
    _, _, client = arcgis
    for suffix in ["", "/tilemap/7/3/4/3/3", "/tile/7/3/4"]:
        response = client.get(f"/arcgis/rest/services/{name}/ImageServer{suffix}")
        assert response.status_code == 404, (name, suffix, response.status_code)
        assert b"secret" not in response.content


def test_partially_written_metadata_is_503_not_500(arcgis):
    _, service, client = arcgis
    (service / "ImageServer.json").write_text('{"name": "Terr', encoding="utf-8")
    response = client.get(f"/arcgis/rest/services/{NAME}/ImageServer")
    assert response.status_code == 503 and response.headers.get("retry-after")


def test_finite_sanitizer():
    import math
    from app.main import _finite
    data = {"a": math.inf, "b": [1.0, -math.inf, {"c": math.nan}], "d": "inf", "e": 3}
    assert _finite(data) == {"a": None, "b": [1.0, None, {"c": None}], "d": "inf", "e": 3}


def test_ingest_maps_errors(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    client = TestClient(main.app)

    def raise_(exc):
        def fn(*args, **kwargs):
            raise exc
        return fn

    monkeypatch.setattr(main, "ingest_file", raise_(PermissionError("outside allowed root")))
    assert client.post("/api/ingest", json={"path": "/etc/passwd"}).status_code == 403
    monkeypatch.setattr(main, "ingest_file", raise_(ValueError("not a file")))
    assert client.post("/api/ingest", json={"path": "/tmp"}).status_code == 400
    monkeypatch.setattr(main, "ingest_file", raise_(FileNotFoundError()))
    assert client.post("/api/ingest", json={"path": "/nope"}).status_code == 404


def test_lifespan_calls_jobs_shutdown(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    import app.jobs
    called = []
    monkeypatch.setattr(app.jobs, "shutdown", lambda: called.append(True), raising=False)
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
    assert called == [True]
