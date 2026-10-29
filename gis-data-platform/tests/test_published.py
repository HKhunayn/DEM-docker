"""Published (static, nginx-served) tile sets.

* parity: the engine's publish step (engine/scripts/publish_tiles.py) bakes
  exactly what the dynamic TerrainSource serves, pixel for pixel;
* terrain.json points at the published version when there is one;
* the API's fallback routes honour the same contract as nginx
  (missing terrain -> flat.webp, missing hillshade -> transparent.png);
* the one-off permission fixer makes legacy 0600 tiles world-readable.
"""
from __future__ import annotations

import importlib
import io
import json
import os
import stat
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

ENGINE_SCRIPTS = Path(__file__).resolve().parents[1] / "engine" / "scripts"
# A small area (~0.5 deg) so z0-z11 publishes in seconds yet has fully
# interior z11 tiles (hard-link path).
BOUNDS = [46.40, 24.40, 46.90, 24.90]
MAXZOOM = 11


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("GIS_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("GIS_FIX_TILE_PERMS", "0")
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


def _publish_module():
    sys.path.insert(0, str(ENGINE_SCRIPTS))
    try:
        import publish_tiles
        return importlib.reload(publish_tiles)
    finally:
        sys.path.remove(str(ENGINE_SCRIPTS))


def _gpx(lon, lat, z, pt):
    return pt.global_px(lon, lat, z)


def _make_pyramid(root: Path, pt, ext: str = "webp", hole: bool = True) -> None:
    """Raw Terrarium tiles for every tile touching BOUNDS, z0..MAXZOOM: a
    smooth slope (so fades/blends are non-trivial), alpha 0 outside BOUNDS
    (like real edge tiles) and an interior no-data hole."""
    (root / "tilejson.json").write_text(json.dumps({
        "bounds": BOUNDS, "minzoom": 0, "maxzoom": MAXZOOM,
        "statistics": {"min": 500.0, "max": 900.0}}), encoding="utf-8")
    west, south, east, north = BOUNDS
    for z in range(MAXZOOM + 1):
        fx0, fy0 = pt.global_px(west, north, z)
        fx1, fy1 = pt.global_px(east, south, z)
        for x in range(int(fx0 // 256), int((fx1 - 1e-9) // 256) + 1):
            for y in range(int(fy0 // 256), int((fy1 - 1e-9) // 256) + 1):
                gx = x * 256 + np.arange(256) + 0.5
                gy = y * 256 + np.arange(256) + 0.5
                h = 500.0 + (gx[None, :] - fx0) * 0.7 + (gy[:, None] - fy0) * 0.3
                h = np.clip(h, 500.0, 900.0)
                inside = ((gx >= fx0) & (gx <= fx1))[None, :] & ((gy >= fy0) & (gy <= fy1))[:, None]
                if hole and z == MAXZOOM and x == int(fx0 // 256) + 1 and y == int(fy0 // 256) + 1:
                    inside[100:110, 100:110] = False  # interior no-data hole
                v = np.round((h + 32768.0) * 256.0).astype(np.int64)
                rgba = np.zeros((256, 256, 4), np.uint8)
                rgba[..., 0] = (v >> 16) & 255
                rgba[..., 1] = (v >> 8) & 255
                rgba[..., 2] = v & 255
                rgba[..., 3] = np.where(inside, 255, 0)
                d = root / "terrarium" / str(z) / str(x)
                d.mkdir(parents=True, exist_ok=True)
                img = Image.fromarray(rgba, "RGBA")
                if ext == "webp":
                    img.save(d / f"{y}.webp", format="WEBP", lossless=True, exact=True, method=1, quality=25)
                else:
                    img.save(d / f"{y}.png")
                hs = np.full((256, 256), 180, np.uint8)
                hd = root / "hillshade" / str(z) / str(x)
                hd.mkdir(parents=True, exist_ok=True)
                Image.fromarray(hs, "L").save(hd / f"{y}.png")


def _decode(path_or_bytes) -> np.ndarray:
    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes
    rgb = np.asarray(Image.open(src).convert("RGB")).astype(np.float64)
    return rgb[..., 0] * 256 + rgb[..., 1] + rgb[..., 2] / 256 - 32768


@pytest.mark.parametrize("ext", ["webp", "png"])
def test_publish_matches_dynamic_terrain_source(tmp_path, monkeypatch, ext):
    main = _load(tmp_path, monkeypatch)
    pt = _publish_module()
    _make_pyramid(main.TILE_DIR, pt, ext)
    version = pt.publish(str(main.TILE_DIR), workers=2, keep=2, log=lambda *a, **k: None)
    out = main.TILE_DIR / "published" / version
    dynamic = main.TERRAIN
    checked = linked = 0
    for z in range(MAXZOOM + 1):
        gx0, gy0, gx1, gy1 = pt.coverage_rect(pt.read_meta(str(main.TILE_DIR)), z)
        pad = 4000.0 / pt.meters_per_px(pt.read_meta(str(main.TILE_DIR)), z) + 512
        for x in range(max(0, int((gx0 - pad) // 256)), min(2 ** z - 1, int((gx1 + pad) // 256)) + 1):
            for y in range(max(0, int((gy0 - pad) // 256)), min(2 ** z - 1, int((gy1 + pad) // 256)) + 1):
                f = out / "terrain" / str(z) / str(x) / f"{y}.webp"
                served = f if f.is_file() else out / "flat.webp"
                want = _decode(dynamic.tile_png(z, x, y))
                got = _decode(served)
                assert np.array_equal(got, want), (z, x, y, float(np.abs(got - want).max()))
                checked += 1
                if f.is_file() and os.stat(f).st_nlink > 1:
                    linked += 1
    assert checked > 50
    if ext == "webp":
        assert linked > 0  # fully-valid interior WebP tiles are hard links
    meta = json.loads((out / "meta.json").read_text())
    assert meta["version"] == version and meta["maxzoom"] == MAXZOOM
    cur = json.loads((main.TILE_DIR / "published" / "current.json").read_text())
    assert cur["version"] == version


def test_published_files_world_readable_and_hillshade_clipped(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    pt = _publish_module()
    _make_pyramid(main.TILE_DIR, pt, "webp")
    for p in (main.TILE_DIR / "terrarium").rglob("*.webp"):
        os.chmod(p, 0o600)  # legacy engines wrote 0600
    version = pt.publish(str(main.TILE_DIR), workers=1, log=lambda *a, **k: None)
    out = main.TILE_DIR / "published" / version
    files = [p for p in out.rglob("*") if p.is_file()]
    assert files and all(stat.S_IMODE(p.stat().st_mode) & 0o004 for p in files)
    hs = list((out / "hillshade").rglob("*.png"))
    assert hs
    edge = [np.asarray(Image.open(p)) for p in hs]
    assert all(a.ndim == 3 and a.shape[2] == 2 for a in edge)       # LA
    assert any((a[..., 1] == 0).any() for a in edge)                 # clipped outside coverage


def test_terrain_json_points_to_published_and_routes_fallback(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    pt = _publish_module()
    _make_pyramid(main.TILE_DIR, pt, "webp")
    client = TestClient(main.app)
    before = client.get("/tiles/terrain.json").json()
    assert before["tiles"][0].endswith(".png?v=" + before["tiles"][0].split("?v=")[1])
    version = pt.publish(str(main.TILE_DIR), workers=1, log=lambda *a, **k: None)
    tj = client.get("/tiles/terrain.json").json()
    assert tj["published"] == version and tj["format"] == "webp"
    assert tj["tiles"][0].endswith(f"/tiles/published/{version}/terrain/{{z}}/{{x}}/{{y}}.webp")
    assert tj["imagery"][0].endswith(f"/tiles/published/{version}/hillshade/{{z}}/{{x}}/{{y}}.png")
    # far away -> flat base tile, immutable
    r = client.get(f"/tiles/published/{version}/terrain/3/0/0.webp")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert "immutable" in r.headers["cache-control"]
    assert np.allclose(_decode(r.content), 500.0, atol=0.01)
    r = client.get(f"/tiles/published/{version}/hillshade/3/0/0.png")
    assert r.status_code == 200 and not np.asarray(Image.open(io.BytesIO(r.content)))[..., -1].any()
    for bad in ("nope", "../x", "20990101T000000-zzzzzzzz"):
        assert client.get(f"/tiles/published/{bad}/terrain/0/0/0.webp").status_code == 404
    assert client.get(f"/tiles/published/{version}/terrain/1/5/0.webp").status_code == 404


def test_publish_refuses_degenerate_build_and_keeps_versions(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    pt = _publish_module()
    _make_pyramid(main.TILE_DIR, pt, "webp", hole=False)
    root = str(main.TILE_DIR)
    v1 = pt.publish(root, workers=1, keep=2, log=lambda *a, **k: None)
    import time as _t
    _t.sleep(1.1)
    v2 = pt.publish(root, workers=1, keep=2, log=lambda *a, **k: None)
    _t.sleep(1.1)
    v3 = pt.publish(root, workers=1, keep=2, log=lambda *a, **k: None)
    pub = main.TILE_DIR / "published"
    assert not (pub / v1).exists() and (pub / v2).exists() and (pub / v3).exists()
    (main.TILE_DIR / "tilejson.json").write_text(json.dumps(
        {"bounds": BOUNDS, "maxzoom": 11, "statistics": {"min": None, "max": None}}), encoding="utf-8")
    assert pt.main(["--root", root]) == 1
    assert json.loads((pub / "current.json").read_text())["version"] == v3


def test_fix_tile_permissions_once(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    data = main.TILE_DIR.parent
    f = main.TILE_DIR / "arcgis" / "rest" / "services" / "X" / "tile" / "1" / "0" / "0"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"LERC")
    os.chmod(f, 0o600)
    assert main.fix_tile_permissions(data) == 1
    assert stat.S_IMODE(f.stat().st_mode) == 0o644
    os.chmod(f, 0o600)
    assert main.fix_tile_permissions(data) == 0  # marker: runs once per volume


def test_base_is_sea_level_when_dataset_spans_it(tmp_path, monkeypatch):
    """Open sea (no Copernicus tiles) and the outer fade sit at 0 m, not at
    the dataset minimum (e.g. -427 m at the Dead Sea for a Saudi build)."""
    main = _load(tmp_path, monkeypatch)
    pt = _publish_module()
    _make_pyramid(main.TILE_DIR, pt, "webp")
    (main.TILE_DIR / "tilejson.json").write_text(json.dumps({
        "bounds": BOUNDS, "minzoom": 0, "maxzoom": MAXZOOM,
        "statistics": {"min": -427.0, "max": 900.0}}), encoding="utf-8")
    assert pt.base_height(-427.0, 900.0) == 0.0 and pt.base_height(400.0, 900.0) == 400.0
    assert main.TERRAIN.meta().base == 0.0
    version = pt.publish(str(main.TILE_DIR), workers=1, log=lambda *a, **k: None)
    flat = _decode(main.TILE_DIR / "published" / version / "flat.webp")
    assert np.allclose(flat, 0.0, atol=0.01)
    assert np.allclose(_decode(main.TERRAIN.tile_png(3, 0, 0)), 0.0, atol=0.01)


def test_hillshade_clipped_to_terrarium_nodata(tmp_path, monkeypatch):
    """Legacy L hillshade paints no-data grey; publish must make it
    transparent wherever the Terrarium tile is no-data."""
    main = _load(tmp_path, monkeypatch)
    pt = _publish_module()
    _make_pyramid(main.TILE_DIR, pt, "webp")
    version = pt.publish(str(main.TILE_DIR), workers=1, log=lambda *a, **k: None)
    out = main.TILE_DIR / "published" / version / "hillshade" / str(MAXZOOM)
    west, south, east, north = BOUNDS
    fx0, fy0 = pt.global_px(west, north, MAXZOOM)
    x, y = int(fx0 // 256) + 1, int(fy0 // 256) + 1      # the tile with the hole
    la = np.asarray(Image.open(out / str(x) / f"{y}.png"))
    assert (la[100:110, 100:110, 1] == 0).all() and la[50, 50, 1] == 255


def test_republish_hard_links_unchanged_tiles(tmp_path, monkeypatch):
    """A republish of an unchanged build reuses the previous version's files
    (hard links): no second copy of the pyramid on disk."""
    main = _load(tmp_path, monkeypatch)
    pt = _publish_module()
    _make_pyramid(main.TILE_DIR, pt, "png")       # legacy PNG: every tile is re-encoded
    v1 = pt.publish(str(main.TILE_DIR), workers=1, log=lambda *a, **k: None)
    import time as _t
    _t.sleep(1.1)
    v2 = pt.publish(str(main.TILE_DIR), workers=1, log=lambda *a, **k: None)
    pub = main.TILE_DIR / "published"
    files = [p for p in (pub / v2).rglob("*") if p.is_file() and p.parent != pub / v2]
    assert files
    assert all(p.stat().st_nlink >= 2 for p in files)
    assert json.loads((pub / v2 / "meta.json").read_text())["counts"]["reused"] == len(files)
    for p in files:
        assert p.read_bytes() == (pub / v1 / p.relative_to(pub / v2)).read_bytes()
