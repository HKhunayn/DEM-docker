"""Data-platform DOWNLOAD jobs: fetch -> hand-off to the engine, status API,
area persistence, and the platform's route surface (no tiles, no builds)."""

import sys
import time

JOB = {
    "name": "local30m",
    "bbox": [6.6, 45.6, 7.2, 46.0],
    "provider": "copernicus",
    "minzoom": 0,
    "maxzoom": 13,
}


def wait_done(client, job_id, timeout=20.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        if last["state"] in ("done", "error", "cancelled", "interrupted"):
            return last
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {last}")


def fake_engine(monkeypatch, status=202, payload=None):
    """Replace the HTTP call to the engine; returns the list of calls."""
    handoff = sys.modules["app.handoff"]
    calls = []

    def request(method, path, body=None, timeout=None):
        calls.append((method, path, body))
        return status, payload

    monkeypatch.setattr(handoff, "request", request)
    return calls


def test_download_job_fetches_and_waits_for_a_stopped_engine(client):
    response = client.post("/api/jobs", json=JOB)
    assert response.status_code == 202
    started = response.json()
    assert started["state"] == "running" and started["mode"] == "download"
    assert [s["name"] for s in started["steps"]] == ["fetch", "handoff"]

    # the saved area exists while the job runs
    areas = client.get("/api/areas").json()
    assert areas["count"] == 1
    assert areas["areas"][0]["id"] == started["areaId"]
    assert areas["areas"][0]["dataset"]["state"] in ("downloading", "downloaded")

    job = wait_done(client, started["id"])
    assert job["state"] == "done"
    assert job["step"] == "handoff"
    assert all(s["state"] == "done" for s in job["steps"])
    assert any("Done: 2 tile(s)" in line for line in job["log"])
    assert not any(line.startswith("stub build") for line in job["log"])   # never builds
    assert job["result"]["sourceTiles"] == 2
    assert job["result"]["handoff"]["state"] == "engine-offline"
    # engine not running: the download is kept, the engine builds it when it starts
    assert "engine not running - it builds this area when it starts" in job["warning"]

    area = client.get(f"/api/areas/{started['areaId']}").json()
    assert area["dataset"]["state"] == "downloaded"
    assert area["dataset"]["pendingBuild"] == "full"
    assert area["dataset"]["message"] == "engine not running - it builds this area when it starts"
    assert area["dataset"]["sourceTiles"] == 2

    listing = client.get("/api/jobs").json()
    assert listing["count"] == 1 and listing["jobs"][0]["id"] == started["id"]
    assert client.get("/api/jobs/current").json()["id"] == started["id"]


def test_download_job_hands_off_to_a_running_engine(client, monkeypatch):
    calls = fake_engine(monkeypatch, 202, {"state": "started", "message": "the engine is building 'local30m'",
                                           "job": {"id": "e" * 32, "state": "running"}})
    job = wait_done(client, client.post("/api/jobs", json=JOB).json()["id"])
    assert job["state"] == "done" and job["warning"] is None
    assert calls == [("POST", "/api/jobs", {"name": "local30m", "provider": "copernicus", "origin": "handoff"})]
    assert job["result"]["handoff"] == {"state": "started", "engineJobId": "e" * 32,
                                        "message": "the engine is building 'local30m'"}
    assert any(line.startswith("HANDOFF started") for line in job["log"])
    area = client.get(f"/api/areas/{job['areaId']}").json()
    # the platform never overwrites the dataset once the engine owns the area
    assert area["dataset"]["state"] == "downloaded" and area["dataset"]["pendingBuild"] == "full"


def test_download_job_queued_behind_a_busy_engine(client, monkeypatch):
    fake_engine(monkeypatch, 202, {"state": "queued", "job": None,
                                   "message": "queued in the engine - a job is already running"})
    job = wait_done(client, client.post("/api/jobs", json=JOB).json()["id"])
    assert job["state"] == "done" and job["warning"] is None
    assert job["result"]["handoff"]["state"] == "queued"
    area = client.get(f"/api/areas/{job['areaId']}").json()
    assert area["dataset"]["message"].startswith("queued in the engine")


def test_download_job_reports_an_engine_refusal(client, monkeypatch):
    fake_engine(monkeypatch, 404, {"detail": "no saved elevation area"})
    job = wait_done(client, client.post("/api/jobs", json=JOB).json()["id"])
    assert job["state"] == "done"
    assert job["result"]["handoff"]["state"] == "error"
    assert "refused" in job["warning"] and "404" in job["warning"]


def test_handoff_client_engine_down_is_offline_not_an_exception(client):
    handoff = sys.modules["app.handoff"]
    result = handoff.request_build("x", "copernicus")      # ENGINE_URL: nothing listens
    assert result["state"] == "engine-offline"
    assert result["message"] == handoff.ENGINE_OFFLINE_MSG


def test_status_endpoint_matches_legacy_shape(client):
    idle = client.get("/api/status").json()
    assert idle["state"] == "idle"
    assert idle["log"] == []

    started = client.post("/api/jobs", json=JOB).json()
    wait_done(client, started["id"])
    status = client.get("/api/status").json()
    assert status["state"] == "done"
    assert status["provider"] == "copernicus"
    assert status["bbox"] == [6.6, 45.6, 7.2, 46.0]
    assert status["log"] and isinstance(status["log"], list)
    assert status["started"] and status["finished"]


def test_second_job_rejected_while_one_runs(client, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "2")
    monkeypatch.setenv("STUB_SLEEP_STEP", "fetch")
    first = client.post("/api/jobs", json=JOB)
    assert first.status_code == 202

    running = client.get("/api/status").json()
    assert running["state"] == "running"
    assert running["step"] == "fetch"
    assert running["elapsed"] >= 0

    second = client.post("/api/jobs", json={**JOB, "name": "other"})
    assert second.status_code == 409
    assert "already running" in second.json()["detail"]

    wait_done(client, first.json()["id"])
    third = client.post("/api/jobs", json={**JOB, "name": "other"})
    assert third.status_code == 202
    wait_done(client, third.json()["id"])


def test_failed_download_reports_error(client, monkeypatch):
    monkeypatch.setenv("STUB_FAIL", "fetch")
    started = client.post("/api/jobs", json=JOB).json()
    job = wait_done(client, started["id"])
    assert job["state"] == "error"
    assert "fetch" in job["error"]
    assert job["result"] is None
    assert job["steps"][1]["state"] == "pending"           # nothing handed over
    area = client.get(f"/api/areas/{started['areaId']}").json()
    assert area["dataset"]["state"] == "error"
    assert "pendingBuild" not in area["dataset"]


def test_job_validation_and_provider_gate(client, monkeypatch):
    assert client.post("/api/jobs", json={**JOB, "bbox": [1, 2, 3]}).status_code == 422
    assert client.post("/api/jobs", json={**JOB, "bbox": [2.0, 1.0, 1.0, 2.0]}).status_code == 422
    assert client.post("/api/jobs", json={**JOB, "provider": "srtm"}).status_code == 422

    monkeypatch.setattr(
        "app.fetch_api.probe_providers",
        lambda: {"copernicus": (True, ""), "fabdem": (False, "remotezip/requests missing")},
    )
    response = client.post("/api/jobs", json={**JOB, "provider": "fabdem"})
    assert response.status_code == 400
    assert "fabdem" in response.json()["detail"]


def test_job_can_skip_area_save(client):
    started = client.post("/api/jobs", json={**JOB, "name": "nosave", "saveArea": False}).json()
    assert started["areaId"] is None
    job = wait_done(client, started["id"])
    assert job["result"]["handoff"]["state"] == "skipped"
    assert client.get("/api/areas").json()["count"] == 0


def test_fetch_failure_does_not_touch_tilejson(client, monkeypatch, tmp_path):
    tiles = tmp_path / "data" / "tiles"
    monkeypatch.setenv("STUB_FAIL", "fetch")
    job = wait_done(client, client.post("/api/jobs", json=JOB).json()["id"])
    assert job["state"] == "error" and "fetch" in job["error"]
    assert job["steps"][1]["state"] == "pending"
    assert not (tiles / "tilejson.json").exists()


def test_platform_serves_no_tiles_and_builds_nothing(client):
    for path in ("/tiles/terrain.json", "/tiles/terrain/0/0/0.png", "/arcgis/rest/services/x/ImageServer",
                 "/arcgis/rest/services/x/ImageServer/tile/0/0/0", "/api/services", "/api/tiles/status",
                 "/api/build/x", "/tiles/imagery-sets/x.json",
                 "/tiles/published/20260101T000000-0123abcd/terrain/0/0/0.webp"):
        assert client.get(path).status_code == 404, path
    assert client.post("/api/tiles/rebuild-missing").status_code in (404, 405)
    assert client.get("/health").json()["role"] == "platform"


def test_fetch_page_api_also_under_fetch_prefix(client):
    """The page uses relative urls (api/...), so behind the viewer's /fetch/
    proxy every call it makes lands under /fetch/api/..."""
    assert client.get("/fetch/api/areas").json()["count"] == 0
    assert client.get("/fetch/api/fetch/providers").status_code == 200
    assert client.get("/fetch/api/imagery/providers").status_code == 200
    started = client.post("/fetch/api/jobs", json=JOB)
    assert started.status_code == 202
    job = wait_done(client, started.json()["id"])
    assert client.get(f"/fetch/api/jobs/{job['id']}").json()["state"] == "done"
    assert client.get("/fetch/vendor/leaflet/dist/leaflet.js").status_code == 200


def test_engine_proxy_routes_when_engine_down(client):
    status = client.get("/api/engine/status").json()
    assert status["running"] is False
    assert status["message"] == "engine not running - it builds this area when it starts"
    assert status["publicUrl"] == "http://testserver:5000"
    response = client.get("/fetch/api/engine/build/local30m")
    assert response.status_code == 503
    assert response.json()["detail"] == "engine not running - it builds this area when it starts"


def test_engine_proxy_routes_when_engine_up(client, monkeypatch):
    calls = fake_engine(monkeypatch, 200, {"name": "local30m", "job": {"id": "e" * 32, "state": "running",
                                                                       "step": "build"}})
    body = client.get("/api/engine/build/local30m?tail=5").json()
    assert body["job"]["step"] == "build"
    assert calls[-1] == ("GET", "/api/build/local30m?tail=5", None)
    assert client.get("/api/engine/status").json()["running"] is True
