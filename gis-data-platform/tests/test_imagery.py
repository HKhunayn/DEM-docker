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
        if job["state"] in ("done", "error", "cancelled", "interrupted"):
            return job
        time.sleep(0.05)
    raise AssertionError("imagery job did not finish")


def test_imagery_providers_listed(client):
    data = client.get("/api/imagery/providers").json()
    ids = {p["id"] for p in data["providers"]}
    assert {"s2cloudless", "bluemarble"} <= ids
    assert all("url" not in p for p in data["providers"])


def test_world_z0_z1_fetch_creates_set_area_and_tiles(client, serve_client, monkeypatch):
    calls = _stub_http(monkeypatch)
    response = client.post("/api/imagery/jobs", json=WORLD)
    assert response.status_code == 202, response.text
    job = wait_done(client, response.json()["id"])
    assert job["state"] == "done", job
    assert job["kind"] == "imagery"
    assert job["result"]["tiles"] == 5          # 1 tile at z0 + 4 at z1
    assert len(calls) == 5
    assert any("/1/1/0.jpg" in url for url in calls)  # {z}/{y}/{x} WMTS order

    sets = serve_client.get("/api/imagery").json()   # served by the engine
    assert sets["count"] == 1
    tj = sets["sets"][0]
    assert tj["name"] == "world_test" and tj["maxzoom"] == 1
    assert "/tiles/imagery-sets/world_test/{z}/{x}/{y}" in tj["tiles"][0]
    assert serve_client.get("/tiles/imagery-sets/world_test.json").json()["attribution"]

    area = client.get(f"/api/areas/{job['areaId']}").json()
    assert area["kind"] == "imagery"
    assert area["dataset"]["state"] == "done"
    assert area["dataset"]["tilejson"] == "/tiles/imagery-sets/world_test.json"

    tile = serve_client.get("/tiles/imagery-sets/world_test/1/1/0")
    assert tile.status_code == 200 and tile.headers["content-type"] == "image/jpeg"

    # over-zoom above maxzoom is served from the ancestor
    deep = serve_client.get("/tiles/imagery-sets/world_test/12/2048/1300")
    assert deep.status_code == 200 and deep.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(deep.content)).size == (256, 256)

    # re-run keeps existing tiles (resumable)
    calls.clear()
    job2 = wait_done(client, client.post("/api/imagery/jobs", json=WORLD).json()["id"])
    assert job2["result"]["skipped"] == 5 and calls == []
    assert len(client.get("/api/areas").json()["areas"]) == 1


def test_imagery_missing_set_and_out_of_area_tiles(client, serve_client, monkeypatch):
    _stub_http(monkeypatch)
    assert serve_client.get("/tiles/imagery-sets/nope/0/0/0").status_code == 404
    small = {"name": "small", "provider": "bluemarble", "bbox": [40, 20, 41, 21], "minzoom": 2, "maxzoom": 3}
    job = wait_done(client, client.post("/api/imagery/jobs", json=small).json()["id"])
    assert job["state"] == "done"
    outside = serve_client.get("/tiles/imagery-sets/small/3/0/0")
    assert outside.headers["content-type"] == "image/png"   # transparent fill


def test_imagery_job_validation_and_failure(client, monkeypatch):
    _stub_http(monkeypatch, fail=True)
    assert client.post("/api/imagery/jobs", json={**WORLD, "maxzoom": 9, "provider": "bluemarble"}).status_code == 422
    assert client.post("/api/imagery/jobs", json={**WORLD, "provider": "esri"}).status_code == 422
    assert client.post("/api/imagery/jobs", json={**WORLD, "maxzoom": 9}).status_code == 400  # > 50k tiles
    job = wait_done(client, client.post("/api/imagery/jobs", json=WORLD).json()["id"])
    assert job["state"] == "error" and "failed" in job["error"]


def test_html_error_page_is_not_stored_as_tile(client, monkeypatch):
    imagery = sys.modules["app.imagery"]
    calls = []

    def fake_get(url, timeout=30.0):
        calls.append(url)
        return b"<html>quota exceeded</html>"

    monkeypatch.setattr(imagery, "http_get", fake_get)
    monkeypatch.setattr(imagery.time, "sleep", lambda s: None)
    job = wait_done(client, client.post("/api/imagery/jobs", json={**WORLD, "maxzoom": 0}).json()["id"])
    assert job["state"] == "error"
    assert any("not a JPEG/PNG" in line for line in job["log"])
    assert len(calls) == 1  # a bad payload is not retried
    assert not list(imagery.set_dir("world_test").rglob("*.jpg"))


def test_png_payload_accepted_and_oversized_rejected():
    imagery = sys.modules["app.imagery"]
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, format="PNG")
    imagery.check_tile_payload(buffer.getvalue())
    imagery.check_tile_payload(_jpeg((1, 2, 3)))
    import pytest
    for bad in (b"", b"GIF89a....", b"\xff\xd8\xff" + b"0" * (imagery.MAX_TILE_BYTES + 1)):
        with pytest.raises(imagery.BadTileError):
            imagery.check_tile_payload(bad)


def test_http_get_checks_content_type_and_size(monkeypatch):
    import pytest
    imagery = sys.modules["app.imagery"]

    class FakeResponse:
        def __init__(self, ctype, body, length=None):
            self.headers = {"Content-Type": ctype}
            if length is not None:
                self.headers["Content-Length"] = str(length)
            self._body = body

        def read(self, n=-1):
            return self._body if n < 0 else self._body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    responses = []
    seen_timeouts = []

    def fake_urlopen(request, timeout=None):
        seen_timeouts.append(timeout)
        return responses.pop(0)

    monkeypatch.setattr(imagery.urllib.request, "urlopen", fake_urlopen)
    responses.append(FakeResponse("image/jpeg", _jpeg((1, 1, 1))))
    assert imagery.http_get("https://x/0/0/0.jpg")[:3] == b"\xff\xd8\xff"
    assert seen_timeouts[0] and seen_timeouts[0] <= 60
    responses.append(FakeResponse("text/html; charset=utf-8", b"<html>"))
    with pytest.raises(imagery.BadTileError, match="Content-Type"):
        imagery.http_get("https://x/0/0/0.jpg")
    responses.append(FakeResponse("image/jpeg", b"x", length=imagery.MAX_TILE_BYTES + 1))
    with pytest.raises(imagery.BadTileError, match="too large"):
        imagery.http_get("https://x/0/0/0.jpg")
    responses.append(FakeResponse("image/jpeg", b"x" * (imagery.MAX_TILE_BYTES + 10)))
    with pytest.raises(imagery.BadTileError):
        imagery.http_get("https://x/0/0/0.jpg")


def test_corrupt_ancestor_tile_gives_transparent_png_not_500(client, serve_client, monkeypatch):
    _stub_http(monkeypatch)
    job = wait_done(client, client.post("/api/imagery/jobs", json=WORLD).json()["id"])
    assert job["state"] == "done"
    imagery = sys.modules["app.imagery"]
    (imagery.set_dir("world_test") / "1" / "1" / "0.jpg").write_bytes(b"\xff\xd8\xff truncated garbage")
    deep = serve_client.get("/tiles/imagery-sets/world_test/12/2048/1300")
    assert deep.status_code == 200 and deep.headers["content-type"] == "image/png"


def test_get_set_is_cached_by_mtime(client, monkeypatch):
    import json
    import os
    _stub_http(monkeypatch)
    wait_done(client, client.post("/api/imagery/jobs", json=WORLD).json()["id"])
    imagery = sys.modules["app.imagery"]
    meta_path = imagery.set_dir("world_test") / "imagery.json"
    reads = []
    original = type(meta_path).read_text

    def counting_read(self, *args, **kwargs):
        if self.name == "imagery.json":
            reads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(meta_path), "read_text", counting_read)
    imagery._SET_CACHE.clear()
    for _ in range(5):
        assert imagery.get_set("world_test")["maxzoom"] == 1
    assert len(reads) == 1
    meta = json.loads(original(meta_path, encoding="utf-8"))
    meta["maxzoom"] = 0
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    later = os.stat(meta_path).st_mtime + 5
    os.utime(meta_path, (later, later))
    assert imagery.get_set("world_test")["maxzoom"] == 0


def test_imagery_set_provider_clash_and_delete(client, monkeypatch):
    _stub_http(monkeypatch)
    wait_done(client, client.post("/api/imagery/jobs", json={**WORLD, "maxzoom": 0}).json()["id"])
    clash = client.post("/api/imagery/jobs", json={**WORLD, "maxzoom": 0, "provider": "bluemarble"})
    assert clash.status_code == 409
    assert "s2cloudless" in clash.json()["detail"] and "delete it first" in clash.json()["detail"]
    imagery = sys.modules["app.imagery"]
    assert imagery.get_set("world_test")["provider"] == "s2cloudless"

    assert client.delete("/api/imagery/world_test").status_code == 200
    assert not imagery.set_dir("world_test").exists()
    assert [a for a in client.get("/api/areas").json()["areas"] if a["name"] == "world_test"] == []
    assert client.delete("/api/imagery/world_test").status_code == 404

    ok = client.post("/api/imagery/jobs", json={**WORLD, "maxzoom": 0, "provider": "bluemarble"})
    assert ok.status_code == 202
    assert wait_done(client, ok.json()["id"])["state"] == "done"


def test_delete_imagery_set_while_job_writes_it_is_409(client, monkeypatch):
    import threading
    imagery = sys.modules["app.imagery"]
    gate = threading.Event()

    def slow_get(url, timeout=30.0):
        gate.wait(10)
        return _jpeg((5, 5, 5))

    monkeypatch.setattr(imagery, "http_get", slow_get)
    started = client.post("/api/imagery/jobs", json=WORLD).json()
    try:
        response = client.delete("/api/imagery/world_test")
        assert response.status_code == 409
        assert "still writing" in response.json()["detail"]
    finally:
        gate.set()
    assert wait_done(client, started["id"])["state"] == "done"
    assert client.delete("/api/imagery/world_test").status_code == 200


def test_imagery_job_cancel(client, monkeypatch):
    import threading
    imagery = sys.modules["app.imagery"]
    gate = threading.Event()

    def slow_get(url, timeout=30.0):
        gate.wait(10)
        return _jpeg((5, 5, 5))

    monkeypatch.setattr(imagery, "http_get", slow_get)
    started = client.post("/api/imagery/jobs", json={**WORLD, "maxzoom": 3}).json()
    threading.Timer(0.3, gate.set).start()
    job = client.post(f"/api/jobs/{started['id']}/cancel").json()
    assert job["state"] == "cancelled"
    area = client.get(f"/api/areas/{started['areaId']}").json()
    assert area["dataset"]["state"] == "cancelled"


def test_imagery_routes_split_between_apps(client, serve_client):
    """Downloads on the data-platform, tiles/TileJSON on the engine."""
    assert client.get("/api/imagery").status_code in (404, 405)
    assert client.get("/tiles/imagery-sets/x/0/0/0").status_code == 404
    assert serve_client.post("/api/imagery/jobs", json=WORLD).status_code in (404, 405)
    assert serve_client.get("/api/imagery/providers").status_code == 404
