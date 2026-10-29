"""Published (static, nginx-served) tile sets.

* parity: the engine's publish step (engine/scripts/publish_tiles.py) bakes
  exactly what the dynamic TerrainSource serves, pixel for pixel;
* terrain.json points at the published version when there is one (no
  hillshade / `imagery` key: browsers shade the elevation tiles);
* the API's fallback route honours the same contract as nginx
  (missing terrain -> flat.webp);
* single copy on disk: publish moves the raw pyramid into the version (raw
  tiles + build manifest + older versions deleted after the switch,
  tilejson.json kept; --keep-raw keeps the raw pyramid); a publish without a
  raw pyramid exits non-zero and changes nothing;
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
    (root / "terrarium" / "build_manifest.json").write_text(
        json.dumps({"engine_version": 4, "complete": True}), encoding="utf-8")


def _quiet(*_a, **_k):
    return None


def _tree(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def _decode(path_or_bytes) -> np.ndarray:
    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes
    rgb = np.asarray(Image.open(src).convert("RGB")).astype(np.float64)
    return rgb[..., 0] * 256 + rgb[..., 1] + rgb[..., 2] / 256 - 32768


@pytest.mark.parametrize("ext", ["webp", "png"])
def test_publish_matches_dynamic_terrain_source(tmp_path, monkeypatch, ext):
    """Published tiles == dynamic TerrainSource (computed from the raw
    pyramid before it is deleted), pixel for pixel - also after the default
    publish has removed the raw tiles (hard links keep the data)."""
    main = _load(tmp_path, monkeypatch)
    pt = _publish_module()
    _make_pyramid(main.TILE_DIR, pt, ext)
    meta = pt.read_meta(str(main.TILE_DIR))
    dynamic = main.TERRAIN
    want = {}
    for z in range(MAXZOOM + 1):
        gx0, gy0, gx1, gy1 = pt.coverage_rect(meta, z)
        pad = 4000.0 / pt.meters_per_px(meta, z) + 512
        for x in range(max(0, int((gx0 - pad) // 256)), min(2 ** z - 1, int((gx1 + pad) // 256)) + 1):
            for y in range(max(0, int((gy0 - pad) // 256)), min(2 ** z - 1, int((gy1 + pad) // 256)) + 1):
                want[(z, x, y)] = _decode(dynamic.tile_png(z, x, y))
    version = pt.publish(str(main.TILE_DIR), workers=2, log=_quiet)
    assert not (main.TILE_DIR / "terrarium" / str(MAXZOOM)).exists()   # raw moved, not copied
    out = main.TILE_DIR / "published" / version
    for (z, x, y), expected in want.items():
        f = out / "terrain" / str(z) / str(x) / f"{y}.webp"
        got = _decode(f if f.is_file() else out / "flat.webp")
        assert np.array_equal(got, expected), (z, x, y, float(np.abs(got - expected).max()))
    assert len(want) > 50
    meta_json = json.loads((out / "meta.json").read_text())
    if ext == "webp":
        assert meta_json["counts"]["linked"] > 0  # fully-valid interior WebP tiles: hard links
    assert meta_json["version"] == version and meta_json["maxzoom"] == MAXZOOM
    cur = json.loads((main.TILE_DIR / "published" / "current.json").read_text())
    assert cur["version"] == version


def test_published_files_world_readable_and_no_hillshade(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    pt = _publish_module()
    _make_pyramid(main.TILE_DIR, pt, "webp")
    for p in (main.TILE_DIR / "terrarium").rglob("*.webp"):
        os.chmod(p, 0o600)  # legacy engines wrote 0600
    version = pt.publish(str(main.TILE_DIR), workers=1, log=_quiet)
    out = main.TILE_DIR / "published" / version
    files = [p for p in out.rglob("*") if p.is_file()]
    assert files and all(stat.S_IMODE(p.stat().st_mode) & 0o004 for p in files)
    assert sorted(p.name for p in out.iterdir()) == ["flat.webp", "meta.json", "terrain"]
    assert not list(main.TILE_DIR.rglob("*hillshade*")) and not list(main.TILE_DIR.rglob("transparent.png"))
    assert "hillshade" not in json.loads((out / "meta.json").read_text())["counts"]


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
    assert "imagery" not in tj and "hillshade" not in json.dumps(tj)
    # far away -> flat base tile, immutable
    r = client.get(f"/tiles/published/{version}/terrain/3/0/0.webp")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert "immutable" in r.headers["cache-control"]
    assert np.allclose(_decode(r.content), 500.0, atol=0.01)
    assert client.get(f"/tiles/published/{version}/hillshade/3/0/0.png").status_code == 404
    assert client.get("/tiles/imagery/3/0/0.png").status_code == 404
    # raw pyramid gone after the publish: the dynamic route still answers
    # (flat base), terrain.json keeps pointing at the published set
    assert not (main.TILE_DIR / "terrarium" / "0").exists()
    assert client.get("/tiles/terrain/3/0/0.png").status_code == 200
    assert client.get("/tiles/terrain.json").json()["published"] == version
    for bad in ("nope", "../x", "20990101T000000-zzzzzzzz"):
        assert client.get(f"/tiles/published/{bad}/terrain/0/0/0.webp").status_code == 404
    assert client.get(f"/tiles/published/{version}/terrain/1/5/0.webp").status_code == 404


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


def test_publish_refuses_degenerate_build_and_keeps_one_version(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    pt = _publish_module()
    _make_pyramid(main.TILE_DIR, pt, "webp", hole=False)
    root = str(main.TILE_DIR)
    import time as _t
    v1 = pt.publish(root, workers=1, log=_quiet, keep_raw=True)
    _t.sleep(1.1)
    v2 = pt.publish(root, workers=1, log=_quiet, keep_raw=True)
    pub = main.TILE_DIR / "published"
    assert not (pub / v1).exists() and (pub / v2).exists()          # only ONE version
    assert {p.name for p in pub.iterdir()} == {"current.json", v2}
    (main.TILE_DIR / "tilejson.json").write_text(json.dumps(
        {"bounds": BOUNDS, "maxzoom": 11, "statistics": {"min": None, "max": None}}), encoding="utf-8")
    assert pt.main(["--root", root]) == 1
    assert json.loads((pub / "current.json").read_text())["version"] == v2
    assert (main.TILE_DIR / "terrarium" / "0").is_dir()             # failed: nothing deleted


def test_publish_moves_raw_pyramid_single_copy(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    pt = _publish_module()
    _make_pyramid(main.TILE_DIR, pt, "webp", hole=False)
    root = main.TILE_DIR
    legacy_hs = root / "hillshade" / "3" / "0"
    legacy_hs.mkdir(parents=True)
    Image.new("L", (256, 256), 128).save(legacy_hs / "0.png")
    pub = root / "published"
    old = pub / "20000101T000000-deadbeef"
    old.mkdir(parents=True)
    (old / "meta.json").write_text("{}", encoding="utf-8")
    (pub / ".staging-20000101T000001-deadbeef").mkdir()
    # a fully-valid interior raw tile: it must end up in the version (same inode)
    meta = pt.read_meta(str(root))
    gx0, gy0, gx1, gy1 = pt.coverage_rect(meta, MAXZOOM)
    x, y = gx0 // 256 + 1, gy0 // 256 + 1
    raw_tile = root / "terrarium" / str(MAXZOOM) / str(x) / f"{y}.webp"
    raw_bytes, raw_inode = raw_tile.read_bytes(), raw_tile.stat().st_ino

    version = pt.publish(str(root), workers=1, log=_quiet)

    assert {p.name for p in pub.iterdir()} == {"current.json", version}   # older + staging gone
    terrarium = root / "terrarium"
    assert [p.name for p in terrarium.iterdir()] == []        # zoom dirs + build_manifest.json gone
    assert (root / "tilejson.json").is_file()                  # kept: API + publish meta need it
    assert not (root / "hillshade").exists()                   # legacy hillshade pyramid removed
    moved = pub / version / "terrain" / str(MAXZOOM) / str(x) / f"{y}.webp"
    assert moved.read_bytes() == raw_bytes and moved.stat().st_ino == raw_inode
    assert moved.stat().st_nlink == 1                          # the ONLY copy on disk
    # the API: no raw pyramid but a published version -> published URLs
    client = TestClient(main.app)
    tj = client.get("/tiles/terrain.json").json()
    assert tj["published"] == version and "imagery" not in tj
    assert client.get(f"/tiles/published/{version}/terrain/{MAXZOOM}/{x}/{y}.webp").content == raw_bytes


def test_publish_keep_raw_keeps_raw_pyramid(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    pt = _publish_module()
    _make_pyramid(main.TILE_DIR, pt, "webp", hole=False)
    root = main.TILE_DIR
    before = _tree(root / "terrarium")
    assert pt.main(["--root", str(root), "--workers", "1", "--keep-raw"]) == 0
    assert _tree(root / "terrarium") == before
    assert (root / "terrarium" / "build_manifest.json").is_file()
    version = json.loads((root / "published" / "current.json").read_text())["version"]
    linked = [p for p in (root / "published" / version / "terrain").rglob("*.webp") if p.stat().st_nlink == 2]
    assert linked  # interior tiles shared with the raw pyramid, not copied


def test_publish_without_raw_pyramid_exits_and_changes_nothing(tmp_path, monkeypatch, capsys):
    main = _load(tmp_path, monkeypatch)
    pt = _publish_module()
    _make_pyramid(main.TILE_DIR, pt, "webp", hole=False)
    root = main.TILE_DIR
    version = pt.publish(str(root), workers=1, log=_quiet)       # moves the raw pyramid
    snapshot = _tree(root)
    current = (root / "published" / "current.json").read_bytes()
    capsys.readouterr()
    assert pt.main(["--root", str(root), "--workers", "1"]) == 2
    err = capsys.readouterr().err
    assert "no raw Terrarium pyramid" in err and "build" in err
    assert _tree(root) == snapshot
    assert (root / "published" / "current.json").read_bytes() == current
    assert TestClient(main.app).get("/tiles/terrain.json").json()["published"] == version


def test_failed_publish_deletes_nothing(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    pt = _publish_module()
    _make_pyramid(main.TILE_DIR, pt, "webp", hole=False)
    root = main.TILE_DIR
    v1 = pt.publish(str(root), workers=1, log=_quiet, keep_raw=True)
    snapshot = _tree(root)

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(pt, "_publish_into", boom)
    assert pt.main(["--root", str(root), "--workers", "1"]) == 1
    assert _tree(root) == snapshot                               # raw, v1, manifest all intact
    assert json.loads((root / "published" / "current.json").read_text())["version"] == v1


def test_terrain_json_without_raw_or_published(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    client = TestClient(main.app)
    tj = client.get("/tiles/terrain.json").json()
    assert "published" not in tj and "imagery" not in tj
    assert "/tiles/terrain/{z}/{x}/{y}.png?v=" in tj["tiles"][0]
    assert client.get("/tiles/terrain/0/0/0.png").status_code == 200


def test_republish_hard_links_unchanged_tiles(tmp_path, monkeypatch):
    """A republish of an unchanged build reuses the previous version's files
    (hard links) before that version is deleted: no second copy written."""
    main = _load(tmp_path, monkeypatch)
    pt = _publish_module()
    _make_pyramid(main.TILE_DIR, pt, "png")       # legacy PNG: every tile is re-encoded
    v1 = pt.publish(str(main.TILE_DIR), workers=1, log=_quiet, keep_raw=True)
    pub = main.TILE_DIR / "published"
    first = {p.relative_to(pub / v1): p.read_bytes() for p in (pub / v1).rglob("*")
             if p.is_file() and p.parent != pub / v1}
    import time as _t
    _t.sleep(1.1)
    v2 = pt.publish(str(main.TILE_DIR), workers=1, log=_quiet)
    assert not (pub / v1).exists()
    files = {p.relative_to(pub / v2): p for p in (pub / v2).rglob("*") if p.is_file() and p.parent != pub / v2}
    assert files and set(files) == set(first)
    assert json.loads((pub / v2 / "meta.json").read_text())["counts"]["reused"] == len(files)
    for rel, p in files.items():
        assert p.read_bytes() == first[rel]
