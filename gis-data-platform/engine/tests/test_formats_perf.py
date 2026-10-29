"""Tile formats + build performance changes: lossless WebP Terrarium (and the
--terrain-format png fallback), 'LA' hillshade with transparent no-data,
0644 files, in-process Lerc1 (ctypes) == CLI bytes, parallel DEM fetch."""

import io
import json
import os
import stat
import subprocess
import sys
import threading
import time

import numpy as np
import pytest
from PIL import Image

from _dem_fixtures import cop_name, make_pair, write_source

import build_lerc
import build_tiles as bt
import fetch_dem
import verify_tiles

BBOX = ["46.8", "24.4", "47.2", "24.6"]
POSIX = sys.platform != "win32"


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    return make_pair(str(tmp_path_factory.mktemp("src")))


@pytest.fixture(scope="module")
def holey(tmp_path_factory):
    """One source with a no-data box around (46.5E, 24.5N)."""
    d = str(tmp_path_factory.mktemp("holey"))
    # rows/cols of the 600 px/deg grid: 24.55..24.45N, 46.45..46.55E
    return [write_source(os.path.join(d, cop_name(46, 24)), 46, 24,
                         nodata_box=(270, 330, 270, 330))]


def build(capsys, srcs, root, *extra, zooms=("8", "10"), bbox=BBOX):
    rc = bt.main(["--src", *srcs, "--bbox", *bbox, "--minzoom", zooms[0],
                  "--maxzoom", zooms[1], "--out", os.path.join(root, "terrarium"),
                  "--hillshade-out", os.path.join(root, "hillshade"),
                  "--workers", "1", *extra])
    return rc, capsys.readouterr().out


def tiles_with_ext(root, ext):
    return [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs if f.endswith(ext)]


# ---- Terrarium WebP ------------------------------------------------------------

def _terrain_array():
    rng = np.random.default_rng(7)
    arr = rng.uniform(-430.0, 8800.0, (256, 256)).astype(np.float32)
    arr[:, :40] = bt.NODATA                    # transparent strip
    arr[200:230, 100:180] = bt.NODATA          # transparent hole
    return arr


def test_webp_lossless_bit_exact_including_transparent_rgb(tmp_path):
    arr = _terrain_array()
    im = bt.encode_terrarium(arr)
    want = np.asarray(im)
    valid = arr > bt.NODATA + 1
    # the transparent pixels carry nearest-fill heights, not zeros
    assert (want[..., 3][~valid] == 0).all() and want[..., :3][~valid].any()
    p = str(tmp_path / "1" / "2" / "3.webp")
    bt._write_terrarium(im, p, "webp")
    with Image.open(p) as got_im:
        assert got_im.format == "WEBP"
        got = np.asarray(got_im.convert("RGBA"))
    assert np.array_equal(got, want)           # lossless AND exact=True
    dec = bt.decode_terrarium(p)
    assert (dec[~valid] == bt.NODATA).all()
    assert np.abs(dec[valid] - arr[valid]).max() <= 1 / 256
    assert bt.decode_terrarium_png is bt.decode_terrarium   # old name kept
    # PNG output is bit-exact as well (zlib level 1)
    buf = io.BytesIO()
    bt.save_terrarium(im, buf, "png")
    assert np.array_equal(np.asarray(Image.open(io.BytesIO(buf.getvalue()))), want)


def test_webp_without_exact_would_lose_rgb():
    # documents WHY exact=True is mandatory: libwebp drops RGB under alpha 0
    im = bt.encode_terrarium(_terrain_array())
    buf = io.BytesIO()
    im.save(buf, format="WEBP", lossless=True, method=1, quality=25)
    lossy_rgb = np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("RGBA"))
    assert not np.array_equal(lossy_rgb, np.asarray(im))


def test_default_build_writes_webp_and_tilejson(tmp_path, capsys, sources):
    root = str(tmp_path)
    rc, out = build(capsys, sources, root)
    assert rc == 0, out
    assert tiles_with_ext(os.path.join(root, "terrarium"), ".webp")
    assert not tiles_with_ext(os.path.join(root, "terrarium"), ".png")
    assert tiles_with_ext(os.path.join(root, "hillshade"), ".png")
    tj = json.load(open(os.path.join(root, "tilejson.json")))
    assert tj["tiles"] == ["/terrarium/{z}/{x}/{y}.webp"] and tj["format"] == "webp"
    for k in ("bounds", "minzoom", "maxzoom", "statistics"):
        assert k in tj
    m = json.load(open(os.path.join(root, "terrarium", bt.MANIFEST_NAME)))
    assert m["params"]["terrain_format"] == "webp" and m["engine_version"] == bt.ENGINE_VERSION
    rc = verify_tiles.main(["--src", *sources, "--tiles", os.path.join(root, "terrarium"),
                            "--bbox", *BBOX, "--minzoom", "8", "--maxzoom", "10"])
    vout = capsys.readouterr().out
    assert rc == 0, vout
    assert "encoder round-trip (webp)" in vout and "verify: PASS" in vout


def test_terrain_format_png_keeps_old_layout(tmp_path, capsys, sources):
    root = str(tmp_path)
    rc, out = build(capsys, sources, root, "--terrain-format", "png")
    assert rc == 0, out
    pngs = tiles_with_ext(os.path.join(root, "terrarium"), ".png")
    assert pngs and not tiles_with_ext(os.path.join(root, "terrarium"), ".webp")
    with Image.open(pngs[0]) as im:
        assert im.format == "PNG" and im.mode == "RGBA"
    tj = json.load(open(os.path.join(root, "tilejson.json")))
    assert tj["tiles"] == ["/terrarium/{z}/{x}/{y}.png"] and tj["format"] == "png"
    rc = verify_tiles.main(["--src", *sources, "--tiles", os.path.join(root, "terrarium"),
                            "--bbox", *BBOX, "--minzoom", "8", "--maxzoom", "10"])
    vout = capsys.readouterr().out
    assert rc == 0 and "encoder round-trip (png)" in vout, vout
    # resume in png mode skips everything; switching to webp changes the
    # manifest fingerprint (terrain_format) and rebuilds
    rc, out = build(capsys, sources, root, "--terrain-format", "png")
    assert rc == 0 and "REBUILDING" not in out
    assert all("written=0" in line for line in out.splitlines() if line.startswith("z"))
    rc, out = build(capsys, sources, root)
    assert rc == 0 and "terrarium: REBUILDING" in out and "encoding params changed" in out
    assert "hillshade: REBUILDING" not in out


def test_resume_treats_legacy_png_as_not_done_in_webp_mode(tmp_path, sources):
    out_root = str(tmp_path / "terrarium")
    z, (x, y) = 10, bt.lonlat_to_tile(47.0, 24.5, 10)
    legacy = bt.terrarium_tile_path(out_root, z, x, y, "png")
    os.makedirs(os.path.dirname(legacy))
    bt._write_terrarium(bt.encode_terrarium(np.full((256, 256), 5.0, np.float32)),
                        legacy, "png")
    bt._init_worker(list(sources))
    try:
        # png mode: the existing .png is a finished tile
        assert bt._tile_job((z, x, y, out_root, None, False, None, "png"))[0] == "skip"
        # webp mode: a legacy .png does not count -> built as .webp
        status, st, _ = bt._tile_job((z, x, y, out_root, None, False, None, "webp"))
        assert status == "write" and st[0] > 100      # real heights, not the 5 m dummy
        webp = bt.terrarium_tile_path(out_root, z, x, y, "webp")
        assert os.path.exists(webp) and os.path.exists(legacy)
        # and now the .webp resumes
        assert bt._tile_job((z, x, y, out_root, None, False, None, "webp"))[0] == "skip"
        # old 6-tuple tasks default to webp
        assert bt._tile_job((z, x, y, out_root, None, False))[0] == "skip"
    finally:
        bt._IDX.close()


# ---- hillshade LA ----------------------------------------------------------------

def test_hillshade_tiles_are_LA_with_transparent_nodata(tmp_path, capsys, holey):
    root = str(tmp_path)
    bbox = ["46.4", "24.4", "46.6", "24.6"]
    rc, out = build(capsys, holey, root, zooms=("11", "11"), bbox=bbox)
    assert rc == 0, out
    x, y = bt.lonlat_to_tile(46.5, 24.5, 11)
    hs = os.path.join(root, "hillshade", "11", str(x), f"{y}.png")
    with Image.open(hs) as im:
        assert im.format == "PNG" and im.mode == "LA"
        la = np.asarray(im)
    ter = np.asarray(Image.open(bt.terrarium_tile_path(
        os.path.join(root, "terrarium"), 11, x, y)).convert("RGBA"))
    nod = ter[..., 3] == 0
    assert nod.any() and (~nod).any()
    assert np.array_equal(la[..., 1] == 0, nod)        # same no-data mask
    assert (la[..., 1][~nod] == 255).all()
    m = json.load(open(os.path.join(root, "hillshade", bt.MANIFEST_NAME)))
    assert m["params"]["mode"] == "LA"


# ---- file permissions -------------------------------------------------------------

@pytest.mark.skipif(not POSIX, reason="POSIX permissions")
def test_written_files_are_0644(tmp_path, capsys, sources):
    old = os.umask(0o022)
    try:
        root = str(tmp_path)
        extra = []
        if build_lerc.lerc1_backend() != "missing":
            extra = ["--lerc-out", root, "--lerc-name", "svc"]
        rc, out = build(capsys, sources, root, *extra, zooms=("9", "9"))
        assert rc == 0, out
    finally:
        os.umask(old)
    files = [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs]
    kinds = {os.path.splitext(f)[1] for f in files}
    assert {".webp", ".png", ".json"} <= kinds
    bad = [(f, oct(stat.S_IMODE(os.stat(f).st_mode))) for f in files
           if stat.S_IMODE(os.stat(f).st_mode) != 0o644]
    assert not bad, bad[:5]
    dirs = [d for d, _, _ in os.walk(root)][1:]
    assert all(stat.S_IMODE(os.stat(d).st_mode) == 0o755 for d in dirs)


@pytest.mark.skipif(not POSIX, reason="POSIX permissions")
def test_write_atomic_mode_ignores_restrictive_umask(tmp_path):
    old = os.umask(0o077)
    try:
        p = str(tmp_path / "a" / "x.json")
        bt.write_json_atomic(p, {"a": 1})
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o644


# ---- Lerc1: ctypes library == CLI ------------------------------------------------

def _cli_runs(path):
    if not path or not os.path.exists(path):
        return False
    try:
        subprocess.run([path], capture_output=True, timeout=10)
        return True
    except OSError:
        return False


HAVE_LIB = bool(build_lerc._load_lib())
HAVE_CLI = _cli_runs(build_lerc.LERC1ENC)


def _lerc_arrays():
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:256, 0:256].astype(np.float32)
    smooth = (600 + 40 * np.sin(xx / 30) * np.cos(yy / 20)).astype(np.float32)
    holes = smooth.copy()
    holes[:, :50] = bt.NODATA
    holes[120:140, 90:200] = bt.NODATA
    holes[7, 7] = np.nan
    steep = (xx * 37.5 - yy * 21.25 + rng.normal(0, 15, (256, 256))).astype(np.float32)
    real_ish = (rng.uniform(-20, 3000, (256, 256))).astype(np.float32)
    return {
        "smooth": smooth,
        "nodata-holes": holes,
        "constant": np.full((256, 256), 1234.5, np.float32),
        "all-nodata": np.full((256, 256), bt.NODATA, np.float32),
        "steep": steep,
        "noise": real_ish,
        "odd-shape": steep[:13, :257 - 2],
    }


@pytest.mark.skipif(not (HAVE_LIB and HAVE_CLI), reason="needs liblerc1enc + lerc1enc CLI")
@pytest.mark.parametrize("maxzerr", [0.01, 0.0])
@pytest.mark.parametrize("name", list(_lerc_arrays()))
def test_lerc1_ctypes_matches_cli_bytes(name, maxzerr):
    arr = _lerc_arrays()[name]
    lib = build_lerc.lerc1_blob_lib(arr, maxzerr)
    cli = build_lerc.lerc1_blob_cli(arr, maxzerr)
    assert lib.startswith(b"CntZImage")
    assert lib == cli
    ref = os.environ.get("LERC1ENC_REF")      # e.g. the ORIGINAL production binary
    if ref and _cli_runs(ref):
        old = build_lerc.LERC1ENC
        try:
            build_lerc.LERC1ENC = ref
            assert lib == build_lerc.lerc1_blob_cli(arr, maxzerr)
        finally:
            build_lerc.LERC1ENC = old


@pytest.mark.skipif(not HAVE_LIB, reason="needs liblerc1enc")
def test_lerc1_blob_prefers_library_and_falls_back(monkeypatch):
    assert build_lerc.lerc1_backend() == "library"
    arr = _lerc_arrays()["nodata-holes"]
    calls = []
    real_cli = build_lerc.lerc1_blob_cli
    monkeypatch.setattr(build_lerc, "lerc1_blob_cli",
                        lambda *a, **k: calls.append(1) or real_cli(*a, **k))
    a = build_lerc.lerc1_blob(arr, 0.01)
    assert not calls                                   # in-process, no subprocess
    monkeypatch.setattr(build_lerc, "_LIB", False)     # library unavailable
    if HAVE_CLI:
        assert build_lerc.lerc1_backend() == "cli"
        assert build_lerc.lerc1_blob(arr, 0.01) == a and calls
    with pytest.raises(ValueError):                    # 1-D input rejected
        build_lerc.lerc1_blob_lib(np.zeros(5, np.float32), 0.01, fn=lambda *a: 0)


def test_lerc1lib_env_off_disables_library(monkeypatch):
    monkeypatch.setenv("LERC1LIB", "off")
    monkeypatch.setattr(build_lerc, "_LIB", None)
    assert build_lerc._load_lib() is None
    monkeypatch.setattr(build_lerc, "_LIB", None)       # re-probe later normally


@pytest.mark.skipif(os.environ.get("LERC1LIB") is None, reason="LERC1LIB not set")
def test_docker_image_ships_library():
    assert os.path.exists(os.environ["LERC1LIB"]) and HAVE_LIB


# ---- parallel fetch ------------------------------------------------------------

def test_parallel_fetch_keeps_semantics(tmp_path, monkeypatch, capsys):
    # 3 x 3 tiles; deterministic outcomes per tile, random-ish durations so
    # completions arrive out of order
    tiles = fetch_dem.tile_list(30, [0.5, 0.5, 2.5, 2.5])
    assert len(tiles) == 9
    outcome = {t[1]: ("missing" if i in (1, 5) else "failed" if i == 7 else "ok")
               for i, t in enumerate(tiles)}
    delays = {t[1]: 0.02 * ((i * 5) % 9) for i, t in enumerate(tiles)}
    live = {"now": 0, "max": 0}
    lock = threading.Lock()

    def fake_download(url, dest, retries=5, timeout=60, backoff=2.0):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        try:
            for i in range(20):        # many lines: they must never interleave
                fetch_dem._say(f"  GET     {url} chunk {i:02d} " + "x" * 200)
            time.sleep(delays[url])
            kind = outcome[url]
            if kind == "missing":
                raise fetch_dem.MissingTile(url, 404)
            if kind == "failed":
                raise fetch_dem.DownloadError(f"HTTP 500 for {url}")
            with open(dest, "wb") as f:
                f.write(b"II*\x00data")
        finally:
            with lock:
                live["now"] -= 1

    monkeypatch.setattr(fetch_dem, "download", fake_download)
    out = tmp_path / "dem"
    # one tile already cached
    out.mkdir()
    (out / tiles[0][2]).write_bytes(b"II*\x00cached")
    rc = fetch_dem.main(["--bbox", "0.5", "0.5", "2.5", "2.5", "--out", str(out),
                         "--jobs", "4"])
    cap = capsys.readouterr()
    assert rc == 1                                    # one FAILED tile
    assert live["max"] > 1                            # really concurrent
    assert cap.err.count("  MISSING ") == 2 and cap.err.count("  FAILED  ") == 1
    assert "Done: 6 tile(s) (cached 1, downloaded 5, failed 1)" in cap.out
    for line in cap.out.splitlines():
        if "chunk" in line:
            assert line.startswith("  GET     http") and line.endswith("x" * 200)
    manifest = json.loads((out / "manifest.json").read_text())
    want = [str(out / t[2]) for i, t in enumerate(tiles) if outcome[t[1]] == "ok"]
    assert manifest["files"] == want                  # bbox order, not completion order

    # --jobs 1 gives the same manifest and exit code
    for t in tiles[1:]:
        p = out / t[2]
        if p.exists():
            p.unlink()
    rc1 = fetch_dem.main(["--bbox", "0.5", "0.5", "2.5", "2.5", "--out", str(out),
                          "--jobs", "1"])
    capsys.readouterr()
    assert rc1 == 1
    assert json.loads((out / "manifest.json").read_text())["files"] == want


def test_parallel_fetch_all_missing_exits_1(tmp_path, monkeypatch, capsys):
    def fake_download(url, dest, **kw):
        raise fetch_dem.MissingTile(url, 404)

    monkeypatch.setattr(fetch_dem, "download", fake_download)
    rc = fetch_dem.main(["--bbox", "0.5", "0.5", "1.5", "1.5", "--out", str(tmp_path)])
    cap = capsys.readouterr()
    assert rc == 1 and "No DEM tiles downloaded." in cap.err
    assert cap.err.count("MISSING") == 4
