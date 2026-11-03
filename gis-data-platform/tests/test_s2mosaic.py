"""Raw Sentinel-2 scene providers: search, composite, stretch, tiles, API."""
import io
import sys
import time

import numpy as np
from PIL import Image

AREA = {"name": "s2_test", "provider": "s2-earthsearch", "bbox": [46.55, 24.78, 46.56, 24.79],
        "minzoom": 10, "maxzoom": 13}


def _item(i, cloud, when, bbox=(46.0, 24.0, 47.0, 25.0)):
    href = f"https://example.test/{i}"
    return {
        "id": f"S{i}", "bbox": list(bbox),
        "properties": {"eo:cloud_cover": cloud, "datetime": when, "s2:processing_baseline": "05.11"},
        "assets": {name: {"href": f"{href}/{name}.tif",
                          "raster:bands": [{"scale": 0.0001, "offset": -0.1}]}
                   for name in ("red", "green", "blue", "scl")},
    }


def _stub(monkeypatch, items, reflectance=0.3, scl_value=4, cloudy_left=False):
    s2 = sys.modules["app.s2mosaic"]
    searches, reads = [], []

    def fake_json(url, body=None, timeout=60.0):
        searches.append((url, body))
        return {"features": items}

    def fake_read(scene, signer, bounds, width, height):
        reads.append(scene["id"])
        dn = int((reflectance + 0.1) / 0.0001)
        rgb = np.full((3, height, width), dn, dtype=np.uint16)
        scl = np.full((height, width), scl_value, dtype=np.uint8)
        if cloudy_left and scene["id"] == "S1":
            scl[:, : width // 2] = 9          # high cloud on the left half
            rgb[:, :, : width // 2] = 12000   # bright cloud
        return rgb, scl

    monkeypatch.setattr(s2, "http_json", fake_json)
    monkeypatch.setattr(s2, "read_scene", fake_read)
    return searches, reads


def wait_done(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in ("done", "error", "cancelled", "interrupted"):
            return job
        time.sleep(0.05)
    raise AssertionError("imagery job did not finish")


def test_only_kept_providers_listed_earthsearch_first(client):
    providers = client.get("/api/imagery/providers").json()["providers"]
    assert [p["id"] for p in providers] == ["s2-earthsearch", "s2cloudless2024", "bluemarble"]
    es = providers[0]
    assert es["kind"] == "scenes" and es["maxzoom"] == 15 and es["available"]
    assert "stac" not in es and "assets" not in es
    assert providers[1]["kind"] == "tiles"
    for dropped in ("s2cloudless", "s2-planetary", "s2-cdse"):
        assert client.post("/api/imagery/jobs", json={**AREA, "provider": dropped}).status_code == 422
    # default provider is the recommended one
    body = {k: v for k, v in AREA.items() if k != "provider"}
    assert client.post("/api/imagery/jobs", json={**body, "maxzoom": 16}).status_code == 422  # s2 limit z15


def test_scene_job_options_validated(client):
    assert client.post("/api/imagery/jobs", json={**AREA, "dateFrom": "2025-13-01"}).status_code == 422
    assert client.post("/api/imagery/jobs", json={**AREA, "dateFrom": "2025-05-01",
                                                   "dateTo": "2025-01-01"}).status_code == 422
    assert client.post("/api/imagery/jobs", json={**AREA, "maxCloud": 101}).status_code == 422
    assert client.post("/api/imagery/jobs", json={**AREA, "maxzoom": 16}).status_code == 422


def test_scene_job_builds_tiles_and_serves_them(client, serve_client, monkeypatch):
    items = [_item(1, 0.5, "2025-06-01T07:00:00Z"), _item(2, 30.0, "2025-07-01T07:00:00Z")]
    searches, reads = _stub(monkeypatch, items)
    body = {**AREA, "dateFrom": "2025-01-01", "dateTo": "2025-12-31", "maxCloud": 10}
    job = wait_done(client, client.post("/api/imagery/jobs", json=body).json()["id"])
    assert job["state"] == "done", job
    url, query = searches[0]
    assert url.endswith("/search") and query["collections"] == ["sentinel-2-c1-l2a"]
    assert query["datetime"] == "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"
    assert "S2" not in reads                       # 30 % cloud filtered out locally
    meta = sys.modules["app.imagery"].get_set("s2_test")
    assert meta["provider"] == "s2-earthsearch" and meta["mosaic"]["scenes"] == 1
    assert "Copernicus Sentinel data 2025" in meta["attribution"]
    assert abs(meta["mosaic"]["white"] - 0.3) < 1e-3           # uniform sample -> its own 99.5th pct

    tj = serve_client.get("/tiles/imagery-sets/s2_test.json").json()
    assert tj["maxzoom"] == 13 and "Copernicus" in tj["attribution"]
    imagery = sys.modules["app.imagery"]
    x0, y0, x1, y1 = imagery.tile_range(AREA["bbox"], 13)
    tile = serve_client.get(f"/tiles/imagery-sets/s2_test/13/{x0}/{y0}")
    assert tile.status_code == 200 and tile.headers["content-type"] == "image/jpeg"
    pixel = Image.open(io.BytesIO(tile.content)).convert("RGB").getpixel((128, 128))
    assert all(235 <= c <= 250 for c in pixel)      # white point -> ~240 (soft shoulder above)
    # lower zooms: partial coverage -> WebP with alpha, transparent outside
    low = serve_client.get(f"/tiles/imagery-sets/s2_test/10/{x0 >> 3}/{y0 >> 3}")
    assert low.headers["content-type"] == "image/webp"
    image = Image.open(io.BytesIO(low.content))
    assert image.mode == "RGBA" and image.getextrema()[3] == (0, 255)
    # over-zoom of a partial tile keeps the transparency
    deep = serve_client.get(f"/tiles/imagery-sets/s2_test/14/{2 * x0}/{2 * y0}")
    assert deep.status_code == 200 and deep.headers["content-type"] in ("image/jpeg", "image/webp")

    # re-run resumes: finished blocks are not read again
    reads.clear()
    job2 = wait_done(client, client.post("/api/imagery/jobs", json=body).json()["id"])
    assert job2["state"] == "done" and reads == ["S1"] * len(reads) and len(reads) <= 1  # preview only


def test_scene_job_without_scenes_fails_clearly(client, monkeypatch):
    _stub(monkeypatch, [])
    job = wait_done(client, client.post("/api/imagery/jobs", json=AREA).json()["id"])
    assert job["state"] == "error" and "no Sentinel-2 scene" in job["error"]


def test_composite_median_skips_masked_clouds():
    s2 = sys.modules.get("app.s2mosaic") or __import__("app.s2mosaic", fromlist=["x"])
    h, w = 4, 4
    scenes = [{"id": f"S{i}", "scale": 1e-4, "offset": -0.1} for i in range(3)]
    def rgb(dn):
        return np.full((3, h, w), dn, np.uint16)
    clear = np.full((h, w), 4, np.uint8)
    cloud = np.full((h, w), 9, np.uint8)
    reads = [(scenes[0], rgb(12000), cloud), (scenes[1], rgb(3000), clear), (scenes[2], rgb(5000), clear)]
    refl, covered, used = s2.composite(reads, (h, w))
    assert covered.all() and used == ["S1", "S2"]
    assert np.allclose(refl, 0.3)                   # median(0.2, 0.4), cloud ignored
    # all samples cloudy -> median of everything rather than a hole
    refl, covered, _ = s2.composite([(scenes[0], rgb(3000), cloud)], (h, w))
    assert covered.all() and np.allclose(refl, 0.2)
    # no data anywhere -> not covered
    refl, covered, used = s2.composite([(scenes[0], rgb(0), clear)], (h, w))
    assert not covered.any() and used == []


def test_stretch_rank_and_signing():
    s2 = sys.modules.get("app.s2mosaic") or __import__("app.s2mosaic", fromlist=["x"])
    out = s2.to_rgb8(np.array([[[0.0, 0.15, 0.3, 0.9]]], np.float32), 0.3)
    assert out[0, 0, 0] == 0 and 120 < out[0, 0, 1] < 135 and 235 <= out[0, 0, 2] < 250 and out[0, 0, 3] == 255
    assert s2.white_point([np.full(1000, 2.0)]) == s2.WHITE_MAX
    a = {"id": "a", "cloud": 3.0, "datetime": "2025-01-01T00:00:00Z"}
    b = {"id": "b", "cloud": 1.0, "datetime": "2025-06-01T00:00:00Z"}
    c = {"id": "c", "cloud": 9.0, "datetime": "2025-09-01T00:00:00Z"}
    assert [s["id"] for s in sorted([a, b, c], key=s2.scene_rank)] == ["b", "a", "c"]
    winter = {"id": "w", "cloud": 0.0, "datetime": "2025-12-01T00:00:00Z", "sun": 8.0}
    summer = {"id": "s", "cloud": 2.0, "datetime": "2025-06-01T00:00:00Z", "sun": 45.0}
    assert [s["id"] for s in sorted([winter, summer], key=s2.scene_rank)] == ["s", "w"]  # high sun first
    signer = s2.Signer(s2.SCENE_PROVIDERS["s2-earthsearch"])
    assert signer.href("https://x/TCI.tif") == "https://x/TCI.tif"
    assert s2.gdal_env(s2.SCENE_PROVIDERS["s2-earthsearch"])["AWS_NO_SIGN_REQUEST"] == "YES"
    # items without raster:bands -> offset from the processing baseline
    item = _item(1, 0, "2025-01-01T00:00:00Z")
    for asset in item["assets"].values():
        asset.pop("raster:bands")
    info = {"assets": {"red": "red", "green": "green", "blue": "blue", "scl": "scl"}}
    assert s2.parse_item(item, info)["offset"] == -0.1
    item["properties"]["s2:processing_baseline"] = "03.01"
    assert s2.parse_item(item, info)["offset"] == 0.0


def test_broken_granule_and_footprint_driven_reads():
    s2 = sys.modules.get("app.s2mosaic") or __import__("app.s2mosaic", fromlist=["x"])
    h = w = 64
    clear = np.full((h, w), 4, np.uint8)
    broken = {"id": "B", "scale": 1e-4, "offset": -0.1}
    good = {"id": "G", "scale": 1e-4, "offset": -0.1}
    refl, covered, used = s2.composite([(broken, np.full((3, h, w), 128, np.uint16), clear),
                                        (good, np.full((3, h, w), 3000, np.uint16), clear)], (h, w))
    assert used == ["G"] and np.allclose(refl, 0.2)   # granule at DN 128 (reflectance < 0) rejected

    bounds = s2.tile_bounds(12, 2576, 1752, 1, 1)
    west, south = s2.mercator_to_lonlat(bounds[0], bounds[1])
    east, north = s2.mercator_to_lonlat(bounds[2], bounds[3])
    mid = (west + east) / 2
    def poly(x0, x1):
        return {"type": "Polygon", "coordinates": [[[x0, south - 1], [x1, south - 1], [x1, north + 1],
                                                    [x0, north + 1], [x0, south - 1]]]}
    left = {"id": "L", "scale": 1e-4, "offset": -0.1, "geometry": poly(west - 1, mid)}
    right = {"id": "R", "scale": 1e-4, "offset": -0.1, "geometry": poly(mid, east + 1)}
    away = {"id": "A", "scale": 1e-4, "offset": -0.1, "geometry": poly(east + 2, east + 3)}
    fp = s2.footprint_mask(left, bounds)
    assert fp[:, :15].all() and not fp[:, 17:].any()

    calls = []
    def read(scene, b, ww, hh):
        calls.append(scene["id"])
        rgb = np.zeros((3, hh, ww), np.uint16)
        half = slice(0, ww // 2) if scene["id"] == "L" else slice(ww // 2, ww)
        rgb[:, :, half] = 3000
        return rgb, np.full((hh, ww), 4, np.uint8)
    # five passes over the left half only, then the right half: the old
    # "first 5 scenes" rule left the right half empty
    scenes = [dict(left, id=f"L") for _ in range(5)] + [away, right]
    acc = s2.fill_block(scenes, read, bounds, w, h)
    refl, covered = acc.result()
    assert covered.all()
    assert "A" not in calls                 # footprint misses the block: never read
    assert calls.count("L") == 3            # MIN_CLEAR reached on the left -> left scenes skipped
    assert calls.index("R") == 1            # coverage first: the right half is read before depth


def test_search_follows_post_and_get_next_links(monkeypatch):
    s2 = sys.modules.get("app.s2mosaic") or __import__("app.s2mosaic", fromlist=["x"])
    pages = {
        None: {"features": [_item(1, 1, "2025-01-01T00:00:00Z")],
               "links": [{"rel": "next", "method": "POST", "href": "https://stac/search", "body": {"token": "p2"}}]},
        "p2": {"features": [_item(2, 0, "2025-02-01T00:00:00Z"), _item(1, 1, "2025-01-01T00:00:00Z")],
               "links": [{"rel": "next", "href": "https://stac/search?page=3"}]},
        "p3": {"features": [_item(3, 50, "2025-03-01T00:00:00Z")], "links": []},
    }
    calls = []
    def fake(url, body=None, timeout=60):
        calls.append((url, body))
        if body is None:
            return pages["p3"]
        assert body["collections"] == ["c"] and body["limit"] == s2.SEARCH_PAGE
        return pages[body.get("token")]
    monkeypatch.setattr(s2, "http_json", fake)
    info = {"stac": "https://stac", "collection": "c",
            "assets": {"red": "red", "green": "green", "blue": "blue", "scl": "scl"}}
    scenes = s2.search(info, [0, 0, 1, 1], "2025-01-01", "2025-12-31", 20)
    assert [s["id"] for s in scenes] == ["S2", "S1"]       # deduped, cloudy S3 dropped, best first
    assert len(calls) == 3 and calls[2] == ("https://stac/search?page=3", None)
