"""build_tiles core: multi-source merge, decimated-read placement, tile math,
encoders, hillshade, atomic writes."""

import io
import os
import time

import numpy as np
import pytest
from PIL import Image

from _dem_fixtures import height, make_pair, pixel_lonlat_grid, tile_of, write_source, cop_name

import build_tiles as bt
import verify_tiles


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    d = tmp_path_factory.mktemp("src")
    paths = make_pair(str(d))
    idx = bt.SourceIndex(paths)
    yield paths, idx
    idx.close()


def _inside_sources(lon, lat, margin):
    return ((lon > 46 + margin) & (lon < 48 - margin)
            & (lat > 24 + margin) & (lat < 25 - margin))


# ---- tile math -------------------------------------------------------------

def test_lonlat_to_tile_clamps_and_clips():
    assert bt.lonlat_to_tile(0, 90, 3) == (4, 0)          # lat clamped, no math error
    assert bt.lonlat_to_tile(0, -90, 3) == (4, 7)
    assert bt.lonlat_to_tile(180, 0, 3) == (7, 4)         # x clipped to 2^z-1
    assert bt.lonlat_to_tile(-180, 0, 3) == (0, 4)
    assert bt.lonlat_to_tile(46.3, 24.3, 13) == tile_of(46.3, 24.3, 13)
    assert verify_tiles.lonlat_to_tile is bt.lonlat_to_tile  # one shared helper


def test_tile_range_boundary_edges():
    # max edge exactly on a tile boundary must not add the next tile
    assert bt.tile_range((0, 0, 180, 85.05112878), 1) == (1, 0, 1, 0)
    assert bt.tile_range((-180, -90, 180, 90), 0) == (0, 0, 0, 0)
    assert bt.tile_range((-180, -90, 180, 90), 2) == (0, 0, 3, 3)
    # z13 lon 46 lies inside tile 5142 -> a bbox ending at 46 still includes it
    x0, _, x1, _ = bt.tile_range((45.9, 24.3, 46.0, 24.4), 13)
    assert x1 == tile_of(46.0, 24.35, 13)[0]
    # degenerate (point) bbox -> exactly one tile
    x0, y0, x1, y1 = bt.tile_range((46.3, 24.3, 46.3, 24.3), 10)
    assert (x0, y0) == (x1, y1) == bt.lonlat_to_tile(46.3, 24.3, 10)


def test_bbox_validation():
    with pytest.raises(ValueError, match="MINLON"):
        bt.validate_bbox((47, 24, 46, 25))
    with pytest.raises(ValueError, match="MINLAT"):
        bt.validate_bbox((46, 25, 47, 24))
    with pytest.raises(SystemExit):
        bt.parse_args(["--src", "x.tif", "--bbox", "47", "24", "46", "25"])


# ---- E1: sources must not erase each other ------------------------------------

@pytest.mark.parametrize("z", [9, 11, 13])
def test_tile_straddling_two_sources_has_no_nodata(pair, z):
    _, idx = pair
    x, y = tile_of(47.0, 24.5, z)
    lon0, lat0, lon1, lat1 = bt.tile_lonlat_bounds(x, y, z)
    assert lon0 < 47.0 < lon1, "tile must straddle the lon 47 source boundary"
    dst = bt.build_tile(idx, x, y, z)
    assert dst is not None
    lon, lat = pixel_lonlat_grid(x, y, z)
    inside = _inside_sources(lon, lat, 0.002)
    nodata = (dst <= bt.NODATA + 1) & inside
    assert nodata.sum() == 0, f"{nodata.sum()} nodata px inside the sources at z{z}"
    err = np.abs(dst[inside] - height(lon[inside], lat[inside]))
    assert np.median(err) < 0.5 and err.max() < 5.0


def test_first_valid_source_wins_and_holes_filled_by_neighbour(tmp_path):
    # two overlapping sources: A has a nodata hole, B covers it
    a = write_source(str(tmp_path / "a.tif"), 46, 24, nodata_box=(200, 400, 200, 400))
    b = write_source(str(tmp_path / "b.tif"), 46, 24)
    idx = bt.SourceIndex([a, b])
    try:
        x, y = tile_of(46.5, 24.5, 10)
        dst = bt.build_tile(idx, x, y, 10)
        lon, lat = pixel_lonlat_grid(x, y, 10)
        inside = _inside_sources(lon, lat, 0.01) & (lon < 47 - 0.01)
        assert (dst[inside] > bt.NODATA + 1).all()
    finally:
        idx.close()


# ---- E2: decimated reads must land in the right place -------------------------

@pytest.mark.parametrize("z,tol_med,tol_p99", [(6, 2.0, 10.0), (8, 1.0, 5.0), (10, 0.5, 3.0)])
def test_low_zoom_accuracy(pair, z, tol_med, tol_p99):
    _, idx = pair
    x, y = tile_of(47.0, 24.5, z)
    dst = bt.build_tile(idx, x, y, z)
    lon, lat = pixel_lonlat_grid(x, y, z)
    pix_deg = 360.0 / 2**z / 256
    inside = _inside_sources(lon, lat, 2 * pix_deg)
    assert inside.sum() > 50
    valid = dst > bt.NODATA + 1
    coverage = (valid & inside).sum() / inside.sum()
    assert coverage == 1.0, f"z{z}: only {coverage:.0%} of the source area has data"
    # nothing outside the sources (misplaced data would show up there)
    outside = ~_inside_sources(lon, lat, -2 * pix_deg)
    assert not (valid & outside).any()
    err = np.abs(dst[inside] - height(lon[inside], lat[inside]))
    assert np.median(err) < tol_med and np.percentile(err, 99) < tol_p99, \
        f"z{z}: median {np.median(err):.2f} p99 {np.percentile(err, 99):.2f}"


def test_read_patch_transform_rescaled(pair):
    paths, idx = pair
    ds = idx.datasets[0]
    arr, tr = bt.read_patch(ds, 46.0, 24.0, 47.0, 25.0)   # 600 px -> capped? no: whole
    assert arr.shape == (600, 600)
    old_cap = bt.READ_CAP
    try:
        bt.READ_CAP = 100                                  # force decimation
        arr, tr = bt.read_patch(ds, 46.0, 24.0, 47.0, 25.0)
    finally:
        bt.READ_CAP = old_cap
    assert arr.shape == (100, 100)
    # the transform must span the full degree, not 100 source pixels
    assert tr.a == pytest.approx(0.01) and tr.e == pytest.approx(-0.01)
    assert tr.c + 100 * tr.a == pytest.approx(47.0) and tr.f + 100 * tr.e == pytest.approx(24.0)


# ---- encoders -------------------------------------------------------------------

def test_terrarium_nodata_rgb_filled_alpha_zero(tmp_path):
    arr = np.full((256, 256), 512.25, np.float32)
    arr[:, :100] = bt.NODATA
    im = bt.encode_terrarium(arr)
    rgba = np.asarray(im).astype(np.int64)
    assert (rgba[:, :100, 3] == 0).all() and (rgba[:, 100:, 3] == 255).all()
    raw = rgba[..., 0] * 65536 + rgba[..., 1] * 256 + rgba[..., 2]
    h = raw / 256.0 - 32768.0
    assert np.allclose(h, 512.25)  # no 0 m cliff under the transparent pixels
    p = tmp_path / "t.png"
    im.save(p)
    dec = bt.decode_terrarium_png(str(p))
    assert (dec[:, :100] == bt.NODATA).all() and np.allclose(dec[:, 100:], 512.25)
    hv, _ = verify_tiles.decode_png(str(p))
    assert (hv[:, :100] == bt.NODATA).all()


def test_terrarium_all_nodata_tile_encodes():
    im = bt.encode_terrarium(np.full((4, 4), bt.NODATA, np.float32))
    assert (np.asarray(im)[..., 3] == 0).all()


def test_hillshade_buffer_is_seamless():
    # a curved surface: one-sided edge gradients would differ from central ones
    yy, xx = np.mgrid[0:262, 0:262].astype(np.float32)
    surf = (0.02 * (xx - 100) ** 2 + 0.03 * (yy - 50) ** 2).astype(np.float32)
    big = np.asarray(bt.hillshade(surf, 30.0))
    tile = np.asarray(bt.hillshade(surf[2:260, 2:260], 30.0, buffer=1))
    assert tile.shape == (256, 256, 2)          # 'LA': grey + alpha
    assert np.array_equal(tile, big[3:259, 3:259])


def test_hillshade_nodata_not_a_cliff():
    arr = np.full((50, 50), 800.0, np.float32)
    arr[20:30, 20:30] = bt.NODATA
    im = bt.hillshade(arr, 30.0)
    assert im.mode == "LA"
    out = np.asarray(im)
    flat = int(np.cos(np.radians(45.0)) * 255)
    valid = arr > bt.NODATA + 1
    assert (out[..., 0][valid] == flat).all()  # flat ground stays flat at the hole edge
    assert (out[..., 0][~valid] == 128).all()
    assert (out[..., 1][~valid] == 0).all() and (out[..., 1][valid] == 255).all()


def test_hillshade_uses_ground_cell_size():
    # at 60N the ground cell is half the Mercator cell: the same pixel steps
    # must shade as twice the slope
    z, y = 10, 300                               # some tile around 60-61N
    lat = bt._row_latitudes(y, z, 258, 1)
    assert 59 < np.degrees(lat).mean() < 62
    merc_cell = 2 * bt.ORIGIN_SHIFT / 2**z / 256
    ramp = np.tile(np.arange(258, dtype=np.float32) * 10.0, (258, 1))
    got = np.asarray(bt.tile_hillshade(ramp, 0, y, z, 1)).astype(int)
    ground = np.asarray(bt.hillshade(ramp, merc_cell * np.cos(lat), 1)).astype(int)
    merc = np.asarray(bt.hillshade(ramp, merc_cell, 1)).astype(int)
    assert np.array_equal(got, ground)
    assert not np.array_equal(got, merc)


def test_build_tile_buffer_matches_unbuffered(pair):
    _, idx = pair
    x, y = tile_of(47.0, 24.5, 12)
    a = bt.build_tile(idx, x, y, 12)
    b = bt.build_tile(idx, x, y, 12, buffer=1)
    assert b.shape == (258, 258)
    assert np.allclose(a, b[1:-1, 1:-1], atol=0.05)


# ---- atomic writes ----------------------------------------------------------------

def test_write_atomic_cleans_up_on_error(tmp_path):
    target = tmp_path / "z" / "t.png"

    def bad(f):
        f.write(b"partial")
        raise OSError("disk full")

    with pytest.raises(OSError):
        bt.write_atomic(str(target), bad)
    assert not target.exists()
    assert not list((tmp_path / "z").glob("*.part"))
    bt._write_png(Image.new("L", (2, 2)), str(target))
    assert target.exists() and not list((tmp_path / "z").glob("*.part"))


def test_sweep_parts_only_old(tmp_path):
    d = tmp_path / "8" / "1"
    d.mkdir(parents=True)
    old, new = d / "1.png.abc.part", d / "2.png.def.part"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    past = time.time() - 3600
    os.utime(old, (past, past))
    assert bt.sweep_parts(str(tmp_path)) == 1
    assert not old.exists() and new.exists()


def test_source_fingerprint_depends_on_names_and_sizes(tmp_path):
    a = write_source(str(tmp_path / cop_name(46, 24)), 46, 24, overviews=False)
    h1 = bt.source_fingerprint([a])
    assert h1 == bt.source_fingerprint([os.path.join(str(tmp_path), ".", os.path.basename(a))])
    with open(a, "ab") as f:
        f.write(b"\0")
    assert bt.source_fingerprint([a]) != h1


@pytest.mark.parametrize("fmt", ["webp", "png"])
def test_roundtrip_check_in_verify(fmt):
    err, alpha_ok = verify_tiles.roundtrip_check(fmt)
    assert err <= 1 / 256 + 1e-6 and alpha_ok


def test_png_bytes_roundtrip():
    arr = np.linspace(-100, 4000, 256 * 256, dtype=np.float32).reshape(256, 256)
    buf = io.BytesIO()
    bt.encode_terrarium(arr).save(buf, format="PNG")
    buf.seek(0)
    rgba = np.asarray(Image.open(buf)).astype(np.int64)
    h = (rgba[..., 0] * 65536 + rgba[..., 1] * 256 + rgba[..., 2]) / 256 - 32768
    assert np.abs(h - arr).max() <= 1 / 256
