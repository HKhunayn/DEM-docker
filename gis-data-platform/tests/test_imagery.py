"""Imagery fetch jobs, local imagery sets, TileJSON + XYZ delivery."""
import io
import sys
import time

from PIL import Image

WORLD = {"name": "world_test", "provider": "s2cloudless", "minzoom": 0, "maxzoom": 1}


def _jpeg(color):
    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _stub_http(monkeypatch, fail=False):
    calls = []
    imagery = sys.modules["app.imagery"]

    def fake_get(url, timeout=30.0):
        calls.append(url)
        if fail:
            raise OSError("network down")
        return _jpeg((10, 120, 200))

    monkeypatch.setattr(imagery, "http_get", fake_get)
    monkeypatch.setattr(imagery.time, "sleep", lambda s: None)
    return calls


def wait_done(client, job_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("imagery job did not finish")


def test_imagery_providers_listed(client):
    data = client.get("/api/imagery/providers").json()
    ids = {p["id"] for p in data["providers"]}
    assert {"s2cloudless", "bluemarble"} <= ids
    assert all("url" not in p for p in data["providers"])


def test_world_z0_z1_fetch_creates_set_area_and_tiles(client, monkeypatch):
    calls = _stub_http(monkeypatch)
    response = client.post("/api/imagery/jobs", json=WORLD)
    assert response.status_code == 202, response.text
    job = wait_done(client, response.json()["id"])
    assert job["state"] == "done", job
    assert job["kind"] == "imagery"
    assert job["result"]["tiles"] == 5          # 1 tile at z0 + 4 at z1
    assert len(calls) == 5
    assert any("/1/1/0.jpg" in url for url in calls)  # {z}/{y}/{x} WMTS order

    sets = client.get("/api/imagery").json()
    assert sets["count"] == 1
    tj = sets["sets"][0]
    assert tj["name"] == "world_test" and tj["maxzoom"] == 1
    assert "/tiles/imagery-sets/world_test/{z}/{x}/{y}" in tj["tiles"][0]
    assert client.get("/tiles/imagery-sets/world_test.json").json()["attribution"]

    area = client.get(f"/api/areas/{job['areaId']}").json()
    assert area["kind"] == "imagery"
    assert area["dataset"]["state"] == "done"
    assert area["dataset"]["tilejson"] == "/tiles/imagery-sets/world_test.json"

    tile = client.get("/tiles/imagery-sets/world_test/1/1/0")
    assert tile.status_code == 200 and tile.headers["content-type"] == "image/jpeg"

    # over-zoom above maxzoom is served from the ancestor
    deep = client.get("/tiles/imagery-sets/world_test/12/2048/1300")
    assert deep.status_code == 200 and deep.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(deep.content)).size == (256, 256)

    # re-run keeps existing tiles (resumable)
    calls.clear()
    job2 = wait_done(client, client.post("/api/imagery/jobs", json=WORLD).json()["id"])
    assert job2["result"]["skipped"] == 5 and calls == []
    assert len(client.get("/api/areas").json()["areas"]) == 1


def test_imagery_missing_set_and_out_of_area_tiles(client, monkeypatch):
    _stub_http(monkeypatch)
    assert client.get("/tiles/imagery-sets/nope/0/0/0").status_code == 404
    small = {"name": "small", "provider": "bluemarble", "bbox": [40, 20, 41, 21], "minzoom": 2, "maxzoom": 3}
    job = wait_done(client, client.post("/api/imagery/jobs", json=small).json()["id"])
    assert job["state"] == "done"
    outside = client.get("/tiles/imagery-sets/small/3/0/0")
    assert outside.headers["content-type"] == "image/png"   # transparent fill


def test_imagery_job_validation_and_failure(client, monkeypatch):
    _stub_http(monkeypatch, fail=True)
    assert client.post("/api/imagery/jobs", json={**WORLD, "maxzoom": 9, "provider": "bluemarble"}).status_code == 422
    assert client.post("/api/imagery/jobs", json={**WORLD, "provider": "esri"}).status_code == 422
    assert client.post("/api/imagery/jobs", json={**WORLD, "maxzoom": 9}).status_code == 400  # > 50k tiles
    job = wait_done(client, client.post("/api/imagery/jobs", json=WORLD).json()["id"])
    assert job["state"] == "error" and "failed" in job["error"]
