"""Background fetch/build jobs: engine CLI steps, status API, area persistence."""

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


def test_job_runs_engine_steps_and_persists_area(client):
    response = client.post("/api/jobs", json=JOB)
    assert response.status_code == 202
    started = response.json()
    assert started["state"] == "running"
    assert started["name"] == "local30m"
    assert started["provider"] == "copernicus"
    assert started["bbox"] == [6.6, 45.6, 7.2, 46.0]

    # the saved area record exists while the job runs
    areas = client.get("/api/areas").json()
    assert areas["count"] == 1
    assert areas["areas"][0]["id"] == started["areaId"]
    assert areas["areas"][0]["name"] == "local30m"
    assert areas["areas"][0]["dataset"]["name"] == "local30m"
    assert areas["areas"][0]["dataset"]["state"] == "building"

    job = wait_done(client, started["id"])
    assert job["state"] == "done"
    assert job["step"] == "publish"
    assert [s["name"] for s in job["steps"]] == ["fetch", "build", "verify", "publish"]
    assert all(s["returncode"] == 0 for s in job["steps"])
    assert job["elapsed"] >= 0
    assert any("Done: 2 tile(s)" in line for line in job["log"])
    assert any("WROTE 42 tiles" in line for line in job["log"])
    assert job["result"] == {
        "sourceTiles": 2,
        "tilesBuilt": 42,
        "lercTiles": 42,
        "verified": True,
    }

    # dataset metadata is back-persisted on the saved area
    area = client.get(f"/api/areas/{started['areaId']}").json()
    assert area["dataset"]["state"] == "done"
    assert area["dataset"]["tilesBuilt"] == 42
    assert area["dataset"]["verified"] is True
    assert area["dataset"]["service"].endswith("/arcgis/rest/services/local30m/ImageServer")

    # history stays queryable
    listing = client.get("/api/jobs").json()
    assert listing["count"] == 1
    assert listing["jobs"][0]["id"] == started["id"]
    assert client.get("/api/jobs/current").json()["id"] == started["id"]


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
    first = client.post("/api/jobs", json=JOB)
    assert first.status_code == 202

    running = client.get("/api/status").json()
    assert running["state"] == "running"
    assert running["step"] in ("fetch", "build", "verify", "publish")
    assert running["elapsed"] >= 0

    second = client.post("/api/jobs", json={**JOB, "name": "other"})
    assert second.status_code == 409

    wait_done(client, first.json()["id"])
    third = client.post("/api/jobs", json={**JOB, "name": "other"})
    assert third.status_code == 202
    wait_done(client, third.json()["id"])


def test_failed_job_reports_error(client, monkeypatch):
    monkeypatch.setenv("STUB_FAIL", "build")
    started = client.post("/api/jobs", json=JOB).json()
    job = wait_done(client, started["id"])
    assert job["state"] == "error"
    assert "build" in job["error"]
    assert job["result"] is None
    area = client.get(f"/api/areas/{started['areaId']}").json()
    assert area["dataset"]["state"] == "error"


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
    wait_done(client, started["id"])
    assert client.get("/api/areas").json()["count"] == 0


def test_verify_failure_keeps_build_and_finishes_done_with_warning(client, monkeypatch, tmp_path):
    tiles = tmp_path / "data" / "tiles"
    (tiles / "tilejson.json").write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setenv("STUB_TILEJSON", '{"new": true}')
    monkeypatch.setenv("STUB_FAIL", "verify")
    started = client.post("/api/jobs", json=JOB).json()
    job = wait_done(client, started["id"])
    assert job["state"] == "done"
    assert job["result"]["verified"] is False
    assert job["result"]["tilesBuilt"] == 42
    assert "verify" in job["warning"]
    assert job["error"] is None
    assert job["steps"][2]["state"] == "error"
    # the new build is kept: no rollback for a verify-only failure
    assert (tiles / "tilejson.json").read_text(encoding="utf-8") == '{"new": true}'
    area = client.get(f"/api/areas/{started['areaId']}").json()
    assert area["dataset"]["state"] == "done" and area["dataset"]["verified"] is False
    assert "verify" in area["dataset"]["warning"]
    # publish still ran after the verify failure (served tiles follow the build)
    assert job["steps"][3]["name"] == "publish" and job["steps"][3]["state"] == "done"


def test_publish_failure_finishes_done_with_warning(client, monkeypatch):
    monkeypatch.setenv("STUB_FAIL", "publish")
    started = client.post("/api/jobs", json=JOB).json()
    job = wait_done(client, started["id"])
    assert job["state"] == "done"
    assert job["error"] is None
    assert "publish" in job["warning"] and "previously published" in job["warning"]
    assert job["result"]["verified"] is True
    assert job["steps"][3]["state"] == "error"

def test_build_failure_restores_previous_tilejson(client, monkeypatch, tmp_path):
    tiles = tmp_path / "data" / "tiles"
    (tiles / "tilejson.json").write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setenv("STUB_TILEJSON", '{"broken": true}')
    monkeypatch.setenv("STUB_FAIL", "build")
    job = wait_done(client, client.post("/api/jobs", json=JOB).json()["id"])
    assert job["state"] == "error"
    assert (tiles / "tilejson.json").read_text(encoding="utf-8") == '{"old": true}'
    assert not list(tiles.glob("*.restore"))


def test_fetch_failure_does_not_touch_tilejson(client, monkeypatch, tmp_path):
    tiles = tmp_path / "data" / "tiles"
    monkeypatch.setenv("STUB_FAIL", "fetch")
    job = wait_done(client, client.post("/api/jobs", json=JOB).json()["id"])
    assert job["state"] == "error" and "fetch" in job["error"]
    assert job["steps"][1]["state"] == "pending"
    assert not (tiles / "tilejson.json").exists()
