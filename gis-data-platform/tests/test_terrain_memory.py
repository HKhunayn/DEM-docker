"""Terrain API memory bounds and defensive tilejson parsing (review item R1)."""
from __future__ import annotations

import importlib
import io
import json
import math
import tracemalloc

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

SAUDI = [31.6, 10.9, 60.5, 37.5]
PEAK_LIMIT = 64 * 1024 * 1024  # bytes; the old mosaic needed ~1.8 GB at z10


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("GIS_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("LEGACY_HEIGHTMAP_ROOT", str(tmp_path / "legacy"))
    import app.config
    import app.catalog
    import app.terrain
    import app.main
    importlib.reload(app.config)
    importlib.reload(app.catalog)
    importlib.reload(app.terrain)
    main = importlib.reload(app.main)
    main.TILE_DIR.mkdir(parents=True, exist_ok=True)
    return main


def _gpx(lon, lat, z):
    n = 256 * 2 ** z
    return ((lon + 180) / 360 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def _write_tile(root, z, x, y, meters=600.0):
    v = meters + 32768
    r, g = int(v // 256), int(v % 256)
    data = np.zeros((256, 256, 4), dtype=np.uint8)
    data[...] = (r, g, 0, 255)
    d = root / "terrarium" / str(z) / str(x)
    d.mkdir(parents=True, exist_ok=True)
    Image.fromarray(data, "RGBA").save(d / f"{y}.png")


def _heights(content):
    rgb = np.asarray(Image.open(io.BytesIO(content)).convert("RGB")).astype(np.float64)
    return rgb[..., 0] * 256 + rgb[..., 1] + rgb[..., 2] / 256 - 32768


def _saudi(tmp_path, monkeypatch, **extra):
    main = _load(tmp_path, monkeypatch)
    meta = {"bounds": SAUDI, "maxzoom": 13, "statistics": {"min": 0.0, "max": 3000.0}}
    meta.update(extra)
    (main.TILE_DIR / "tilejson.json").write_text(json.dumps(meta), encoding="utf-8")
    return main


@pytest.mark.parametrize("z", [13, 10])
def test_huge_bbox_tiles_have_bounded_peak_memory(tmp_path, monkeypatch, z):
    main = _saudi(tmp_path, monkeypatch)
    x0, y0 = _gpx(SAUDI[0], SAUDI[3], z)
    x1, y1 = _gpx(SAUDI[2], SAUDI[1], z)
    cx, cy = int((x0 + x1) / 2 // 256), int((y0 + y1) / 2 // 256)
    # a handful of real tiles in the middle of a huge coverage rectangle
    for dx, dy in [(0, 0), (1, 0), (0, 1)]:
        _write_tile(main.TILE_DIR, z, cx + dx, cy + dy)
    client = TestClient(main.app)
    requests = [
        (cx, cy),                       # real data
        (cx - 1, cy),                   # inside coverage, next to data
        (cx + 50, cy + 50),             # inside coverage, no data anywhere near
        (int(x0 // 256) - 1, cy),       # just outside the west edge
        (int(x0 // 256), int(y0 // 256)),  # NW corner
        (0, 0),                         # far outside: flat base
    ]
    tracemalloc.start()
    try:
        for x, y in requests:
            response = client.get(f"/tiles/terrain/{z}/{x}/{y}.png")
            assert response.status_code == 200, (x, y)
            heights = _heights(response.content)
            assert heights.shape == (256, 256)
            assert np.isfinite(heights).all() and heights.min() >= -1 and heights.max() <= 600.01
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < PEAK_LIMIT, f"peak {peak / 1e6:.1f} MB"
    assert np.allclose(_heights(client.get(f"/tiles/terrain/{z}/{cx}/{cy}.png").content), 600, atol=0.01)
    assert np.allclose(_heights(client.get(f"/tiles/terrain/{z}/0/0.png").content), 0, atol=0.01)


def test_field_and_tile_caches_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("TERRAIN_TILE_CACHE_MB", "1")
    monkeypatch.setenv("TERRAIN_FIELD_CACHE_ENTRIES", "4")
    main = _saudi(tmp_path, monkeypatch, maxzoom=10)
    client = TestClient(main.app)
    x0, y0 = _gpx(SAUDI[0], SAUDI[3], 10)
    tx, ty = int(x0 // 256), int(y0 // 256)
    for i in range(20):
        _write_tile(main.TILE_DIR, 10, tx + i, ty + 1, meters=100 + i)
        assert client.get(f"/tiles/terrain/10/{tx + i}/{ty + 1}.png").status_code == 200
    terrain = main.TERRAIN
    assert len(terrain._fields) <= 4
    assert terrain._tiles.bytes <= 1024 * 1024


def test_byte_lru_evicts_oldest_by_size():
    from app.terrain import _ByteLRU
    lru = _ByteLRU(10)
    lru.put(("a",), b"12345")
    lru.put(("b",), b"12345")
    assert lru.get(("a",)) == b"12345"  # a is now most recent
    lru.put(("c",), b"123")
    assert lru.get(("b",)) is None and lru.get(("a",)) and lru.get(("c",))
    assert lru.bytes == 8
    lru.put(("huge",), b"x" * 11)  # larger than the whole cache: not stored
    assert lru.get(("huge",)) is None


@pytest.mark.parametrize("stats", [
    '{"min": Infinity, "max": -Infinity}',
    '{"min": NaN, "max": NaN}',
    '{"min": null, "max": null}',
    '{"min": "low", "max": "high"}',
    '{"min": 10, "max": Infinity}',
    '{"min": 900, "max": 100}',
])
def test_degenerate_statistics_fall_back_to_default_maxzoom(tmp_path, monkeypatch, stats):
    main = _load(tmp_path, monkeypatch)
    (main.TILE_DIR / "tilejson.json").write_text(
        '{"bounds": [10, 10, 10.2, 10.2], "maxzoom": 5, "statistics": %s}' % stats, encoding="utf-8")
    client = TestClient(main.app)
    meta = main.TERRAIN.meta()
    assert meta.maxzoom == 13 and math.isfinite(meta.base)
    response = client.get("/tiles/terrain.json")
    assert response.status_code == 200
    body = response.json()  # strict JSON: no Infinity/NaN
    assert body["maxzoom"] == 13
    assert client.get("/tiles/terrain/8/0/0.png").status_code == 200


@pytest.mark.parametrize("maxzoom", ["null", '"abc"', "-3", "99", "Infinity", "7.5"])
def test_invalid_maxzoom_defaults_to_13(tmp_path, monkeypatch, maxzoom):
    main = _load(tmp_path, monkeypatch)
    (main.TILE_DIR / "tilejson.json").write_text(
        '{"bounds": [10, 10, 10.2, 10.2], "maxzoom": %s, "statistics": {"min": 1, "max": 2}}' % maxzoom,
        encoding="utf-8")
    client = TestClient(main.app)
    assert main.TERRAIN.meta().maxzoom == 13
    assert client.get("/tiles/terrain.json").json()["maxzoom"] == 13


@pytest.mark.parametrize("bounds", ['[-Infinity, 10, 20, NaN]', '[1, 2, 3]', '"x"', '[20, 10, 10, 20]'])
def test_invalid_bounds_are_ignored(tmp_path, monkeypatch, bounds):
    main = _load(tmp_path, monkeypatch)
    (main.TILE_DIR / "tilejson.json").write_text(
        '{"bounds": %s, "maxzoom": 8, "statistics": {"min": 5, "max": 6}}' % bounds, encoding="utf-8")
    client = TestClient(main.app)
    assert main.TERRAIN.meta().bounds is None
    body = client.get("/tiles/terrain.json").json()
    assert "bounds" not in body
    assert np.allclose(_heights(client.get("/tiles/terrain/3/1/1.png").content), 5, atol=0.01)


def test_out_of_range_bounds_are_clamped(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    (main.TILE_DIR / "tilejson.json").write_text(
        json.dumps({"bounds": [-200, -90, 200, 90], "maxzoom": 3, "statistics": {"min": 5, "max": 6}}),
        encoding="utf-8")
    bounds = main.TERRAIN.meta().bounds
    assert bounds[0] == -180 and bounds[2] == 180 and abs(bounds[1]) < 85.1 and abs(bounds[3]) < 85.1


def test_partially_written_tilejson_keeps_last_good_meta(tmp_path, monkeypatch):
    import os
    import time
    main = _load(tmp_path, monkeypatch)
    path = main.TILE_DIR / "tilejson.json"
    path.write_text(json.dumps({"bounds": [10, 10, 10.2, 10.2], "maxzoom": 9,
                                "statistics": {"min": 1, "max": 2}}), encoding="utf-8")
    client = TestClient(main.app)
    assert client.get("/tiles/terrain.json").status_code == 200
    good = main.TERRAIN.meta()
    assert good.maxzoom == 9
    path.write_text('{"bounds": [10, 10, 10.2', encoding="utf-8")  # engine mid-write
    later = time.time() + 5
    os.utime(path, (later, later))
    response = client.get("/tiles/terrain.json")
    assert response.status_code == 503 and response.headers.get("retry-after")
    assert main.TERRAIN.meta() is good  # tiles keep serving the previous build
    assert client.get("/tiles/terrain/9/0/0.png").status_code == 200
    # the broken state was not cached: finishing the write is picked up
    path.write_text(json.dumps({"bounds": [10, 10, 10.2, 10.2], "maxzoom": 7,
                                "statistics": {"min": 1, "max": 2}}), encoding="utf-8")
    os.utime(path, (later + 5, later + 5))
    assert client.get("/tiles/terrain.json").json()["maxzoom"] == 7
    assert client.get("/tiles/terrain/9/0/0.png").status_code == 404


def test_empty_tilejson_on_first_read_is_not_cached(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    path = main.TILE_DIR / "tilejson.json"
    path.write_text("", encoding="utf-8")
    client = TestClient(main.app)
    assert client.get("/tiles/terrain.json").status_code == 503
    assert main.TERRAIN._meta is None
    assert client.get("/tiles/terrain/0/0/0.png").status_code == 200  # defaults meanwhile


def test_tile_cache_headers(tmp_path, monkeypatch):
    main = _saudi(tmp_path, monkeypatch, maxzoom=5)
    client = TestClient(main.app)
    ok = client.get("/tiles/terrain/5/20/14.png")
    assert ok.status_code == 200
    assert "max-age" in ok.headers["cache-control"] and "immutable" not in ok.headers["cache-control"]
    missing = client.get("/tiles/terrain/6/0/0.png")
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"
    assert client.get("/tiles/terrain/2/9/0.png").headers["cache-control"] == "no-store"


def test_per_tile_windows_match_global_mosaic_with_holes(tmp_path, monkeypatch):
    """Hole filling + edge blur computed per tile (3x3 apron) must equal the
    whole-coverage computation exactly, so tiles stay seamless even when the
    pyramid is full of no-data holes."""
    main = _load(tmp_path, monkeypatch)
    import app.terrain as terrain
    bounds = [10.0, 10.0, 11.3, 11.3]
    z = 10
    (main.TILE_DIR / "tilejson.json").write_text(json.dumps(
        {"bounds": bounds, "maxzoom": z, "statistics": {"min": 100.0, "max": 900.0}}), encoding="utf-8")
    source = main.TERRAIN
    meta = source.meta()
    gx0, gy0, gx1, gy1 = source._rect(meta, z)
    rng = np.random.default_rng(7)
    mosaic = np.full((gy1 - gy0, gx1 - gx0), np.nan, dtype=np.float32)
    for ty in range(gy0 // 256, (gy1 - 1) // 256 + 1):
        for tx in range(gx0 // 256, (gx1 - 1) // 256 + 1):
            if rng.random() < 0.25:
                continue  # missing tile
            h = np.full((256, 256), np.nan)
            for _ in range(3):  # sparse blobs of data, rest no-data
                cy, cx, r = rng.integers(0, 256, 3)
                yy, xx = np.ogrid[:256, :256]
                h[(yy - cy) ** 2 + (xx - cx) ** 2 < (r // 3 + 5) ** 2] = rng.uniform(150, 850)
            h = np.round(h)  # integer metres survive the Terrarium encoding exactly
            rgba = np.zeros((256, 256, 4), dtype=np.uint8)
            ok = ~np.isnan(h)
            v = np.where(ok, h, 0) + 32768
            rgba[..., 0], rgba[..., 1], rgba[..., 3] = v // 256, v % 256, np.where(ok, 255, 0)
            d = main.TILE_DIR / "terrarium" / str(z) / str(tx)
            d.mkdir(parents=True, exist_ok=True)
            Image.fromarray(rgba, "RGBA").save(d / f"{ty}.png")
            ax0, ay0 = max(gx0, tx * 256), max(gy0, ty * 256)
            ax1, ay1 = min(gx1, tx * 256 + 256), min(gy1, ty * 256 + 256)
            mosaic[ay0 - gy0:ay1 - gy0, ax0 - gx0:ax1 - gx0] = h[ay0 - ty * 256:ay1 - ty * 256, ax0 - tx * 256:ax1 - tx * 256]
    reference = terrain._fill_holes(mosaic, terrain.FILL_MAX_PX)
    reference = np.where(np.isnan(reference), meta.base, reference)
    for ty in range(gy0 // 256, (gy1 - 1) // 256 + 1):
        for tx in range(gx0 // 256, (gx1 - 1) // 256 + 1):
            got = source.heights(z, tx, ty)
            ax0, ay0 = max(gx0, tx * 256), max(gy0, ty * 256)
            ax1, ay1 = min(gx1, tx * 256 + 256), min(gy1, ty * 256 + 256)
            want = reference[ay0 - gy0:ay1 - gy0, ax0 - gx0:ax1 - gx0]
            sub = got[ay0 - ty * 256:ay1 - ty * 256, ax0 - tx * 256:ax1 - tx * 256]
            assert np.allclose(sub, want, atol=1e-3), (tx, ty, float(np.abs(sub - want).max()))
    # Outside coverage the edge profile (blurred) is also window-independent:
    # tiles on both sides of an outside tile edge agree.
    ty = (gy0 + gy1) // 512
    left = source.heights(z, gx0 // 256 - 1, ty)
    edge = source.heights(z, gx0 // 256, ty)
    assert np.abs(left[:, -1] - edge[:, 0]).max() < 50
