"""Shared helpers for the engine build tests (synthetic 1-degree DEM COGs).

Not a conftest.py on purpose: other test modules in this dir are owned by
other components."""

import math
import os
import sys

import numpy as np

ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ENGINE, "scripts")
for p in (SCRIPTS, ENGINE):
    if p not in sys.path:
        sys.path.insert(0, p)

PX_PER_DEG = 600          # 6 arc-second synthetic sources (fast, still decimated at z<=10)
SOURCE_NODATA = -32767.0  # Copernicus-style nodata value in the file


def height(lon, lat):
    """Smooth analytic terrain (metres): lets tests compare any pixel exactly."""
    lon = np.asarray(lon, np.float64)
    lat = np.asarray(lat, np.float64)
    return (600.0 + 40.0 * (lon - 46.0) + 25.0 * (lat - 24.0)
            + 60.0 * np.sin(lon * 2.0) * np.cos(lat * 3.0))


def write_source(path, lon0, lat0, nodata_box=None, overviews=True):
    """One 1-degree GeoTIFF COG-like source [lon0, lon0+1] x [lat0, lat0+1]
    sampled from height() at pixel centres (pixel-is-area, like Copernicus)."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin
    n = PX_PER_DEG
    res = 1.0 / n
    cols = lon0 + (np.arange(n) + 0.5) * res
    rows = lat0 + 1 - (np.arange(n) + 0.5) * res
    lon, lat = np.meshgrid(cols, rows)
    data = height(lon, lat).astype(np.float32)
    if nodata_box is not None:
        r0, r1, c0, c1 = nodata_box
        data[r0:r1, c0:c1] = SOURCE_NODATA
    profile = dict(driver="GTiff", width=n, height=n, count=1, dtype="float32",
                   crs="EPSG:4326", transform=from_origin(lon0, lat0 + 1, res, res),
                   nodata=SOURCE_NODATA, tiled=True, blockxsize=256, blockysize=256,
                   compress="deflate")
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(data, 1)
        if overviews:
            ds.build_overviews([2, 4, 8, 16], Resampling.average)
    return path


def cop_name(lon0, lat0):
    return (f"Copernicus_DSM_COG_10_{'N' if lat0 >= 0 else 'S'}{abs(lat0):02d}_00_"
            f"{'E' if lon0 >= 0 else 'W'}{abs(lon0):03d}_00_DEM.tif")


def make_pair(tmpdir):
    """Two adjacent sources sharing the lon 47 line: 46-47E and 47-48E, 24-25N."""
    a = write_source(os.path.join(tmpdir, cop_name(46, 24)), 46, 24)
    b = write_source(os.path.join(tmpdir, cop_name(47, 24)), 47, 24)
    return [a, b]


def pixel_lonlat_grid(x, y, z, size=256):
    n = 2**z * 256
    g = np.arange(size) + 0.5
    lon = (x * 256 + g) / n * 360.0 - 180.0
    gy = y * 256 + g
    lat = np.degrees(2 * np.arctan(np.exp(np.pi * (1 - 2 * gy / n))) - np.pi / 2)
    return np.meshgrid(lon, lat)


def tile_of(lon, lat, z):
    n = 2**z
    x = int((lon + 180) / 360 * n)
    lat_r = math.radians(lat)
    y = int((1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
    return x, y
