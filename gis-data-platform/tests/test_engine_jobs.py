"""Engine BUILD jobs: check -> build -> verify -> publish for a saved area whose
DEMs are on the volume; the engine never downloads; queueing; route surface."""

import sys
import time

AREA = {"name": "local30m", "bbox": [6.6, 45.6, 7.2, 46.0], "provider": "copernicus", "minzoom": 0, "maxzoom": 13}


def save_area(name="local30m", **dataset):
    catalog = sys.modules["app.catalog"]
    return catalog.upsert_area(name=name, bbox=AREA["bbox"], provider="copernicus", minzoom=0, maxzoom=13,
                               dataset={"name": name, **dataset} if dataset else None)


def wait_done(client, job_id, timeout=20.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        if last["state"] in ("done", "error", "cancelled", "interrupted"):
            return last
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {last}")


def build(engine, name="local30m", **extra):
    response = engine.post("/api/jobs", json={"name": name, **extra})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["state"] == "started", body
    return body["job"]


def test_build_job_runs_check_build_verify_publish(engine):
    area = save_area(state="downloaded", pendingBuild="full")
    started = build(engine)
    assert started["areaId"] == area["id"] and started["mode"] == "full"
    job = wait_done(engine, started["id"])
    assert job["state"] == "done"
    assert [s["name"] for s in job["steps"]] == ["check", "build", "verify", "publish"]
    assert all(s["returncode"] == 0 for s in job["steps"])
    assert any(line.startswith("stub sources:") for line in job["log"])
    assert not any(line.startswith("stub fetch") for line in job["log"])     # never downloads
    assert job["result"] == {"sourceTiles": None, "tilesBuilt": 42, "lercTiles": 42, "verified": True}
    dataset = engine.get(f"/api/areas/{area['id']}").json()["dataset"]
    assert dataset["state"] == "done" and dataset["verified"] is True
    assert "pendingBuild" not in dataset
    assert dataset["service"].endswith("/arcgis/rest/services/local30m/ImageServer")
    # per-area build view (what the fetch page follows through the platform)
    view = engine.get("/api/build/local30m?tail=3").json()
    assert view["job"]["id"] == job["id"] and len(view["job"]["log"]) == 3
    assert view["dataset"]["state"] == "done" and view["busyWith"] is None


def test_build_job_refuses_to_download_missing_dems(engine, monkeypatch):
    save_area(state="downloaded", pendingBuild="full")
    monkeypatch.setenv("STUB_NO_SOURCES", "1")
    job = wait_done(engine, build(engine)["id"])
    assert job["state"] == "error"
    assert "not on the volume" in job["error"] and "never downloads" in job["error"]
    assert job["steps"][0]["name"] == "check" and job["steps"][0]["state"] == "error"
    assert job["steps"][0]["returncode"] == 3
    assert job["steps"][1]["state"] == "pending"
    assert any("MISSING source DEMs" in line for line in job["log"])
    assert not any(line.startswith("stub fetch") for line in job["log"])
    dataset = engine.get("/api/areas").json()["areas"][0]["dataset"]
    assert dataset["state"] == "error" and "pendingBuild" not in dataset   # not retried forever


def test_lerc_only_build(engine):
    save_area(state="done")
    job = wait_done(engine, build(engine, mode="lerc")["id"])
    assert [s["name"] for s in job["steps"]] == ["check", "build"]
    assert any("--lerc-only" in line for line in job["log"] if line.startswith("stub build:"))
    assert job["result"]["lercOnly"] is True


def test_build_of_unknown_area_points_at_the_data_platform(engine):
    response = engine.post("/api/jobs", json={"name": "nowhere"})
    assert response.status_code == 404
    assert "download it with the data-platform" in response.json()["detail"]
    assert engine.get("/api/build/nowhere").status_code == 404


def test_second_build_is_queued_and_marked_pending(engine, monkeypatch):
    save_area("first", state="done")
    save_area("second", state="downloaded", pendingBuild="full")
    monkeypatch.setenv("STUB_SLEEP", "2")
    first = build(engine, "first")
    queued = engine.post("/api/jobs", json={"name": "second", "origin": "handoff"})
    assert queued.status_code == 202
    body = queued.json()
    assert body["state"] == "queued" and body["job"] is None
    assert "builds 'second' next" in body["message"]
    dataset = next(a for a in engine.get("/api/areas").json()["areas"] if a["name"] == "second")["dataset"]
    assert dataset["state"] == "downloaded" and dataset["pendingBuild"] == "full"
    view = engine.get("/api/build/second").json()
    assert view["busyWith"]["name"] == "first"
    wait_done(engine, first["id"])


def test_verify_failure_keeps_build_and_finishes_done_with_warning(engine, monkeypatch, tmp_path):
    area = save_area()
    tiles = tmp_path / "data" / "tiles"
    (tiles / "tilejson.json").write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setenv("STUB_TILEJSON", '{"new": true}')
    monkeypatch.setenv("STUB_FAIL", "verify")
    job = wait_done(engine, build(engine)["id"])
    assert job["state"] == "done"
    assert job["result"]["verified"] is False
    assert job["result"]["tilesBuilt"] == 42
    assert "verify" in job["warning"]
    assert job["error"] is None
    assert job["steps"][2]["state"] == "error"
    assert (tiles / "tilejson.json").read_text(encoding="utf-8") == '{"new": true}'
    dataset = engine.get(f"/api/areas/{area['id']}").json()["dataset"]
    assert dataset["state"] == "done" and dataset["verified"] is False
    assert "verify" in dataset["warning"]
    assert job["steps"][3]["name"] == "publish" and job["steps"][3]["state"] == "done"


def test_publish_failure_finishes_done_with_warning(engine, monkeypatch):
    save_area()
    monkeypatch.setenv("STUB_FAIL", "publish")
    job = wait_done(engine, build(engine)["id"])
    assert job["state"] == "done"
    assert job["error"] is None
    assert "publish" in job["warning"] and "previously published" in job["warning"]
    assert job["result"]["verified"] is True
    assert job["steps"][3]["state"] == "error"


def test_build_failure_restores_previous_tilejson(engine, monkeypatch, tmp_path):
    save_area()
    tiles = tmp_path / "data" / "tiles"
    (tiles / "tilejson.json").write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setenv("STUB_TILEJSON", '{"broken": true}')
    monkeypatch.setenv("STUB_FAIL", "build")
    job = wait_done(engine, build(engine)["id"])
    assert job["state"] == "error" and "build" in job["error"]
    assert (tiles / "tilejson.json").read_text(encoding="utf-8") == '{"old": true}'
    assert not list(tiles.glob("*.restore"))


def test_engine_job_files_are_separate_from_the_platform(engine, tmp_path):
    save_area()
    job = wait_done(engine, build(engine)["id"])
    data = tmp_path / "data"
    assert (data / "engine-jobs" / f"{job['id']}.json").is_file()
    assert not (data / "jobs" / f"{job['id']}.json").exists()
    assert not (data / ".engine-job.lock").exists()


def test_engine_has_no_download_or_write_routes(engine):
    assert engine.get("/health").json()["role"] == "engine"
    for path in ("/fetch/", "/api/fetch/providers", "/api/imagery/providers", "/api/status",
                 "/api/engine/status", "/vendor/leaflet/dist/leaflet.js"):
        assert engine.get(path).status_code == 404, path
    # a download request (bbox, no saved area) is not a download on the engine
    response = engine.post("/api/jobs", json={**AREA, "name": "brand_new"})
    assert response.status_code == 404
    assert engine.post("/api/areas", json=AREA).status_code == 405
    assert engine.post("/api/imagery/jobs", json={"name": "x"}).status_code in (404, 405)
    assert engine.post("/api/ingest", json={"path": "x"}).status_code in (404, 405)
    assert engine.post("/api/fetch", json=AREA).status_code in (404, 405)
    # read-only views for the viewers
    assert engine.get("/api/areas").status_code == 200
    assert engine.get("/api/catalog").status_code == 200
    assert engine.get("/tiles/terrain.json").status_code == 200
