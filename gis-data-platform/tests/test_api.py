import importlib

from fastapi.testclient import TestClient


def load_app(tmp_path, monkeypatch, role="engine"):
    monkeypatch.setenv("GIS_ROLE", role)
    monkeypatch.setenv("GIS_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("LEGACY_HEIGHTMAP_ROOT", str(tmp_path / "legacy"))
    import app.config
    import app.catalog
    import app.main
    importlib.reload(app.config)
    importlib.reload(app.catalog)
    return importlib.reload(app.main).app


def test_health_and_empty_catalog(tmp_path, monkeypatch):
    client = TestClient(load_app(tmp_path, monkeypatch))
    assert client.get("/health").json()["status"] == "ok"
    response = client.get("/api/items")
    assert response.status_code == 200
    assert response.json()["features"] == []


def test_ingest_registers_source_and_serves_it(tmp_path, monkeypatch):
    app = load_app(tmp_path, monkeypatch, role="platform")
    source = tmp_path / "sample.tif"
    source.write_bytes(b"test-raster")
    client = TestClient(app)

    response = client.post("/api/ingest", json={"path": str(source), "kind": "elevation"})
    assert response.status_code == 201
    item = response.json()
    assert item["kind"] == "elevation"
    asset = client.get(item["assets"]["source"]["href"])
    assert asset.status_code == 200
    assert asset.content == b"test-raster"


def _decode_terrarium(content):
    import io
    import numpy as np
    from PIL import Image
    rgba = np.asarray(Image.open(io.BytesIO(content)).convert("RGBA")).astype(np.float64)
    return rgba, rgba[..., 0] * 256 + rgba[..., 1] + rgba[..., 2] / 256 - 32768


def test_missing_terrain_tile_is_flat_fill_not_404(tmp_path, monkeypatch):
    # A 404 renders as 0 m in MapLibre and creates stepped walls; serve flat fill.
    client = TestClient(load_app(tmp_path, monkeypatch))
    response = client.get("/tiles/terrain/0/0/0.png")
    assert response.status_code == 200
    rgba, heights = _decode_terrarium(response.content)
    assert heights.min() == heights.max() > -1000  # no tilejson: flat base
    assert client.get("/tiles/terrain/2/9/0.png").status_code == 404


def _legacy_terrain(tmp_path, monkeypatch, bounds, maxzoom=5, base=500.0):
    """Build a legacy pyramid at z=maxzoom: 600 m inside `bounds`, no-data elsewhere."""
    import json
    import math
    import numpy as np
    from PIL import Image
    app = load_app(tmp_path, monkeypatch)
    import app.main as main
    public = main.TILE_DIR
    public.mkdir(parents=True, exist_ok=True)
    (public / "tilejson.json").write_text(json.dumps(
        {"bounds": bounds, "maxzoom": maxzoom, "statistics": {"min": base}}), encoding="utf-8")
    z, n = maxzoom, 256 * 2 ** maxzoom

    def gpx(lon, lat):
        return ((lon + 180) / 360 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)

    x0, y0 = gpx(bounds[0], bounds[3])
    x1, y1 = gpx(bounds[2], bounds[1])
    for tx in range(int(x0 // 256), int(x1 // 256) + 1):
        for ty in range(int(y0 // 256), int(y1 // 256) + 1):
            gx = tx * 256 + np.arange(256)[None, :] + 0.5
            gy = ty * 256 + np.arange(256)[:, None] + 0.5
            inside = (gx >= x0) & (gx < x1) & (gy >= y0) & (gy < y1)
            data = np.zeros((256, 256, 4), dtype=np.uint8)
            data[inside] = (130, 88, 0, 255)  # 600 m
            (public / "terrarium" / str(z) / str(tx)).mkdir(parents=True, exist_ok=True)
            Image.fromarray(data, "RGBA").save(public / "terrarium" / str(z) / str(tx) / f"{ty}.png")
    return TestClient(app), (x0, y0, x1, y1)


def test_terrain_has_no_walls_at_coverage_edge(tmp_path, monkeypatch):
    import numpy as np
    client, (x0, y0, x1, y1) = _legacy_terrain(tmp_path, monkeypatch, [10.0, 10.0, 10.2, 10.2], maxzoom=10)
    tx, ty = int(x0 // 256), int(y0 // 256)
    _, heights = _decode_terrarium(client.get(f"/tiles/terrain/10/{tx}/{ty}.png").content)
    # Interior keeps the real height; no-data never decodes as a sentinel.
    cx, cy = int(x0 - tx * 256) + 5, int(y0 - ty * 256) + 5
    assert abs(heights[cy, cx] - 600) < 0.01
    assert heights.min() >= 499.9 and heights.max() <= 600.01
    # Neighbouring pixels never jump by a cliff (max step between pixels).
    assert np.abs(np.diff(heights, axis=0)).max() < 20
    assert np.abs(np.diff(heights, axis=1)).max() < 20


def test_terrain_tiles_are_seamless_and_fade_to_base(tmp_path, monkeypatch):
    import numpy as np
    client, (x0, y0, x1, y1) = _legacy_terrain(tmp_path, monkeypatch, [10.0, 10.0, 10.2, 10.2], maxzoom=10)
    tx, ty = int(x0 // 256), int(y0 // 256)
    _, left = _decode_terrarium(client.get(f"/tiles/terrain/10/{tx - 1}/{ty}.png").content)
    _, right = _decode_terrarium(client.get(f"/tiles/terrain/10/{tx}/{ty}.png").content)
    assert np.abs(left[:, -1] - right[:, 0]).max() < 20  # continuous across the tile edge
    _, far = _decode_terrarium(client.get(f"/tiles/terrain/10/{tx - 3}/{ty}.png").content)
    assert np.allclose(far, 500, atol=0.01)  # far away: flat base, not a 0 m pit
    assert client.get("/tiles/terrain/11/0/0.png").status_code == 404  # above maxzoom -> over-zoom


def test_tilejson_urls_are_versioned_per_build(tmp_path, monkeypatch):
    import os
    import time
    client, _ = _legacy_terrain(tmp_path, monkeypatch, [10.0, 10.0, 10.2, 10.2], maxzoom=10)
    import app.main as main
    first = client.get("/tiles/terrain.json")
    assert first.headers["cache-control"] == "no-store"
    data = first.json()
    assert "?v=" in data["tiles"][0] and "imagery" not in data
    meta = main.TILE_DIR / "tilejson.json"
    later = time.time() + 5
    os.utime(meta, (later, later))  # a new build rewrites tilejson.json
    assert client.get("/tiles/terrain.json").json()["tiles"][0] != data["tiles"][0]
    assert client.get(data["tiles"][0].replace("{z}/{x}/{y}", "10/0/0")).status_code == 200


def test_no_server_hillshade_routes(tmp_path, monkeypatch):
    """Hillshade is computed in the browser now: no imagery key, no routes."""
    client, _ = _legacy_terrain(tmp_path, monkeypatch, [10.0, 10.0, 10.2, 10.2], maxzoom=10)
    data = client.get("/tiles/terrain.json").json()
    assert "imagery" not in data and "hillshade" not in str(data)
    assert client.get("/tiles/imagery/5/0/0.png").status_code == 404
    assert client.get("/tiles/published/20260101T000000-0123abcd/hillshade/0/0/0.png").status_code == 404


def test_repeated_fetch_refreshes_area_instead_of_duplicating(tmp_path, monkeypatch):
    app = load_app(tmp_path, monkeypatch, role="platform")
    client = TestClient(app)
    payload = {"name": "same_name", "bbox": [1, 2, 3, 4], "provider": "copernicus",
               "minzoom": 0, "maxzoom": 13}
    created = client.post("/api/areas", json=payload)
    assert created.status_code in (200, 201), created.text
    started = client.post("/api/jobs", json=payload)
    assert started.status_code in (200, 201, 202), started.text
    # whether the area is created via the API or via a job, the name stays unique;
    # the second job may be refused (409) while the first one still runs.
    again = client.post("/api/jobs", json={**payload, "bbox": [5, 6, 7, 8]})
    assert again.status_code in (200, 201, 202, 409), again.text
    names = [a["name"] for a in client.get("/api/areas").json()["areas"]]
    assert names.count("same_name") == 1, f"duplicate saved areas: {names}"
