"""End-to-end build_tiles / build_lerc / verify_tiles / terrainhub runs on
synthetic sources: resume + manifest fingerprint, single-pass LERC, verify."""

import json
import os
import shutil
import sys

import numpy as np
import pytest
from PIL import Image

from _dem_fixtures import make_pair

import build_lerc
import build_tiles as bt
import verify_tiles

BBOX = ["46.8", "24.4", "47.2", "24.6"]   # straddles the lon 47 source boundary


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    return make_pair(str(tmp_path_factory.mktemp("src")))


def run_build(capsys, sources, root, *extra, zooms=("8", "10")):
    rc = bt.main(["--src", *sources, "--bbox", *BBOX, "--minzoom", zooms[0],
                  "--maxzoom", zooms[1], "--out", os.path.join(root, "terrarium"),
                  "--hillshade-out", os.path.join(root, "hillshade"),
                  "--workers", "1", *extra])
    return rc, capsys.readouterr().out


def zline(out, z):
    return next(line for line in out.splitlines() if line.startswith(f"z{z}:"))


def test_build_writes_manifest_and_resumes(tmp_path, capsys, sources):
    root = str(tmp_path)
    rc, out = run_build(capsys, sources, root)
    assert rc == 0, out
    assert "written=0" not in zline(out, 10)
    for d in ("terrarium", "hillshade"):
        m = json.load(open(os.path.join(root, d, bt.MANIFEST_NAME)))
        assert m["engine_version"] == bt.ENGINE_VERSION and m["complete"] is True
        assert m["source_hash"] == bt.source_fingerprint(sources)
        assert m["bbox"] == [float(v) for v in BBOX] and m["maxzoom"] == 10
    tj = json.load(open(os.path.join(root, "tilejson.json")))
    assert tj["bounds"] == [float(v) for v in BBOX]
    assert not list(tmp_path.rglob("*.part"))

    # second run: everything resumed
    rc, out = run_build(capsys, sources, root)
    assert rc == 0 and "REBUILDING" not in out
    assert "written=0" in zline(out, 8) and "written=0" in zline(out, 10)

    # engine version mismatch -> forced rebuild
    mp = os.path.join(root, "terrarium", bt.MANIFEST_NAME)
    m = json.load(open(mp))
    m["engine_version"] = 1
    json.dump(m, open(mp, "w"))
    rc, out = run_build(capsys, sources, root)
    assert rc == 0 and "terrarium: REBUILDING" in out and "engine version" in out
    assert "hillshade: REBUILDING" not in out
    assert "written=0" not in zline(out, 10)
    assert json.load(open(mp))["engine_version"] == bt.ENGINE_VERSION

    # source set changed -> forced rebuild
    rc, out = run_build(capsys, list(reversed(sources)), root)
    assert "REBUILDING" not in out           # same set, same hash
    rc, out = run_build(capsys, sources[:1], root)
    assert "source files changed" in out


def test_tiles_without_manifest_are_stale(tmp_path, capsys, sources):
    root = str(tmp_path)
    rc, out = run_build(capsys, sources, root, zooms=("9", "9"))
    assert rc == 0
    os.remove(os.path.join(root, "terrarium", bt.MANIFEST_NAME))
    # poison a tile: a resumed run would keep it, a stale-run must replace it
    x, y = bt.lonlat_to_tile(47.0, 24.5, 9)
    p = os.path.join(root, "terrarium", "9", str(x), f"{y}.png")
    Image.new("RGBA", (256, 256)).save(p)
    rc, out = run_build(capsys, sources, root, zooms=("9", "9"))
    assert rc == 0 and "no build_manifest.json" in out
    assert (np.asarray(Image.open(p))[..., 3] == 255).any()


def test_interrupted_forced_rebuild_resumes_by_zoom(tmp_path, capsys, sources):
    root = str(tmp_path)
    assert run_build(capsys, sources, root)[0] == 0
    for d in ("terrarium", "hillshade"):
        mp = os.path.join(root, d, bt.MANIFEST_NAME)
        m = json.load(open(mp))
        m["complete"] = False
        m["force_pending"] = {"bbox": m["bbox"], "minzoom": 8, "maxzoom": 10,
                              "zooms_done": [8, 9]}
        json.dump(m, open(mp, "w"))
    rc, out = run_build(capsys, sources, root)
    assert rc == 0 and "resuming an interrupted forced rebuild" in out
    assert "written=0" in zline(out, 8) and "written=0" in zline(out, 9)
    assert "written=0" not in zline(out, 10)
    assert "force_pending" not in json.load(open(os.path.join(root, "terrarium",
                                                              bt.MANIFEST_NAME)))


def test_single_pass_lerc_matches_standalone(tmp_path, capsys, sources):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    # no hillshade here: then the single pass reprojects exactly like the
    # standalone builder (no 1-px buffer) and the blobs must be identical
    rc = bt.main(["--src", *sources, "--bbox", *BBOX, "--minzoom", "8", "--maxzoom", "10",
                  "--out", os.path.join(a, "terrarium"), "--workers", "1",
                  "--lerc-out", a, "--lerc-name", "svc", "--flavor", "lerc2"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "WROTE" in out and "LERC tiles" in out
    svc = build_lerc.service_root(a, "svc")
    meta = json.load(open(os.path.join(svc, "ImageServer.json")))
    assert json.load(open(os.path.join(svc, bt.MANIFEST_NAME)))["params"]["flavor"] == "lerc2"

    # standalone build_lerc on the same inputs: same metadata fields and tiles
    os.makedirs(b)
    shutil.copy(os.path.join(a, "tilejson.json"), b)
    rc = build_lerc.main(["--src", *sources, "--bbox", *BBOX, "--minzoom", "8",
                          "--maxzoom", "10", "--name", "svc", "--out", b,
                          "--flavor", "lerc2", "--workers", "1"])
    capsys.readouterr()
    assert rc == 0
    meta_b = json.load(open(os.path.join(build_lerc.service_root(b, "svc"),
                                         "ImageServer.json")))
    assert set(meta) == set(meta_b)
    for k in ("minValues", "maxValues", "meanValues", "stdvValues", "fullExtent",
              "tileInfo", "noDataValue", "pixelSizeX"):
        assert meta[k] == meta_b[k], k
    ta = sorted(p.relative_to(svc) for p in (tmp_path / "a").rglob("tile/*/*/*"))
    tb = sorted(p.relative_to(build_lerc.service_root(b, "svc"))
                for p in (tmp_path / "b").rglob("tile/*/*/*"))
    assert ta == tb and ta
    for rel in ta:
        assert (open(os.path.join(svc, rel), "rb").read()
                == open(os.path.join(build_lerc.service_root(b, "svc"), rel), "rb").read())


def _lerc1enc_runs():
    import subprocess
    if not os.path.exists(build_lerc.LERC1ENC):
        return False
    try:
        subprocess.run([build_lerc.LERC1ENC], capture_output=True, timeout=10)
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _lerc1enc_runs(), reason="lerc1enc not built for this OS")
def test_single_pass_lerc1(tmp_path, capsys, sources):
    root = str(tmp_path)
    rc, out = run_build(capsys, sources, root, "--lerc-out", root, zooms=("9", "9"))
    assert rc == 0, out
    tiles = list(tmp_path.rglob("services/local30m/tile/9/*/*"))
    assert tiles and all(open(p, "rb").read(10).startswith(b"CntZImage") for p in tiles)


@pytest.mark.skipif(sys.platform == "win32", reason="needs /bin/sh")
def test_lerc1_blob_errors_include_stderr_and_time_out(tmp_path, monkeypatch):
    bad = tmp_path / "bad.sh"
    bad.write_text("#!/bin/sh\necho 'cannot open input' >&2\nexit 3\n")
    bad.chmod(0o755)
    monkeypatch.setattr(build_lerc, "LERC1ENC", str(bad))
    with pytest.raises(RuntimeError, match="exited with 3: cannot open input"):
        build_lerc.lerc1_blob(np.zeros((4, 4), np.float32), 0.01)
    slow = tmp_path / "slow.sh"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    monkeypatch.setattr(build_lerc, "LERC1ENC", str(slow))
    with pytest.raises(RuntimeError, match="timed out"):
        build_lerc.lerc1_blob(np.zeros((4, 4), np.float32), 0.01, timeout=0.5)
    # one scratch dir per process, not one per tile
    assert os.path.isdir(build_lerc._work_dir())
    assert build_lerc._work_dir() == build_lerc._work_dir()


def test_failing_tile_reported_and_exit_nonzero(tmp_path, capsys, sources, monkeypatch):
    # make the tile's hillshade write fail: the build must finish, list the
    # failure, write no tilejson and return 1
    root = str(tmp_path)
    rc, out = run_build(capsys, sources, root, "--workers", "1", zooms=("8", "8"))
    assert rc == 0
    os.remove(os.path.join(root, "tilejson.json"))
    hs8 = os.path.join(root, "hillshade", "8")
    shutil.rmtree(hs8)
    with open(hs8, "w") as f:      # a FILE where the zoom dir should be
        f.write("x")
    rc, out = run_build(capsys, sources, root, zooms=("8", "8"))
    assert rc == 1
    assert "TILE FAILED 8/" in out
    assert not os.path.exists(os.path.join(root, "tilejson.json"))


def test_verify_passes_and_catches_transparent_pixels(tmp_path, capsys, sources):
    root = str(tmp_path)
    rc, out = run_build(capsys, sources, root, zooms=("5", "12"))
    assert rc == 0, out
    args = ["--src", *sources, "--tiles", os.path.join(root, "terrarium"),
            "--bbox", *BBOX, "--minzoom", "5", "--maxzoom", "12"]
    rc = verify_tiles.main(args)
    out = capsys.readouterr().out
    assert rc == 0, out
    for z in range(5, 13):
        assert f"z{z}:" in out
    # punch transparent holes in a boundary tile at z8 (what E1 looked like)
    x, y = bt.lonlat_to_tile(47.0, 24.5, 8)
    p = os.path.join(root, "terrarium", "8", str(x), f"{y}.png")
    rgba = np.asarray(Image.open(p)).copy()
    rgba[..., 3] = 0
    Image.fromarray(rgba, "RGBA").save(p)
    rc = verify_tiles.main(args)
    out = capsys.readouterr().out
    assert rc == 1 and "transparent pixel" in out


def test_terrainhub_build_uses_cli_bbox(tmp_path, monkeypatch, sources):
    import terrainhub
    data = tmp_path / "data"
    data.mkdir()
    far = str(data / "Copernicus_DSM_COG_10_N10_00_E031_00_DEM.tif")
    open(far, "wb").close()
    files = list(sources) + [far]
    json.dump({"provider": "copernicus", "bbox": [31, 10, 48, 25], "files": files},
              open(data / "manifest.json", "w"))
    seen = {}

    def fake_main():
        seen["argv"] = list(sys.argv)
        return 0

    monkeypatch.setattr(terrainhub.build_tiles, "main", fake_main)
    monkeypatch.setattr(sys, "argv", ["terrainhub.py", "build", "--bbox", *BBOX,
                                      "--minzoom", "8", "--maxzoom", "10",
                                      "--data", str(data), "--root", str(tmp_path / "t")])
    assert terrainhub.main() == 0
    argv = seen["argv"]
    i = argv.index("--bbox")
    assert argv[i + 1:i + 5] == [str(float(v)) for v in BBOX]
    assert "--lerc-out" in argv                     # single pass
    assert far not in argv                          # filtered to the build area
    assert all(s in argv for s in sources)


def test_terrainhub_rejects_swapped_bbox(monkeypatch, capsys):
    import terrainhub
    monkeypatch.setattr(sys, "argv", ["terrainhub.py", "build", "--bbox",
                                      "47", "24", "46", "25"])
    with pytest.raises(SystemExit):
        terrainhub.main()
    assert "MINLON" in capsys.readouterr().err


def test_files_in_bbox_keeps_low_zoom_neighbours():
    import terrainhub
    files = ["/d/Copernicus_DSM_COG_10_N24_00_E046_00_DEM.tif",
             "/d/Copernicus_DSM_COG_10_N10_00_E031_00_DEM.tif",
             "/d/Copernicus_DSM_COG_10_S05_00_W010_00_DEM.tif"]
    assert terrainhub._name_cell(files[2]) == (-5, -10)
    bb = (46.1, 24.1, 46.2, 24.2)
    assert terrainhub.files_in_bbox(files, terrainhub.build_cover(bb, 10)) == files[:1]
    # a z0 build touches the whole world: keep everything
    assert terrainhub.files_in_bbox(files, terrainhub.build_cover(bb, 0)) == files
