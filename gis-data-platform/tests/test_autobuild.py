"""Automatic tiling of saved areas whose tiles are missing or stale (e.g. after
importing a TRUTH backup that carries the DEMs + catalog but no tiles/)."""

from __future__ import annotations

import json
import sys
import time

import pytest

from .conftest import reload_platform

SAUDI = [31.5967, 10.9125, 60.4688, 37.4736]
RIYADH = [46.1755, 24.3193, 47.2714, 25.1849]


@pytest.fixture()
def env(stub_engine, tmp_path):
    scripts = stub_engine / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "build_tiles.py").write_text("ENGINE_VERSION = 4\n", encoding="utf-8")
    (scripts / "publish_tiles.py").write_text("PUBLISH_REVISION = 3\n", encoding="utf-8")
    jobs = reload_platform()
    auto = sys.modules["app.autobuild"]
    auto.POLL_S = 0.05
    catalog = sys.modules["app.catalog"]
    for name, bbox in (("Saudi_Arabia", SAUDI), ("Riyadh", RIYADH)):
        catalog.upsert_area(name=name, bbox=bbox, provider="copernicus", minzoom=0, maxzoom=13,
                            dataset={"name": name, "state": "done"})
    return auto, jobs, sys.modules["app.config"].TILE_ROOT


def publish(tile_root, bounds, revision=3):
    version = "20261029T103817-65e246d5"
    root = tile_root / "published" / version
    (root / "terrain").mkdir(parents=True)
    (root / "meta.json").write_text("{}", encoding="utf-8")
    (tile_root / "published" / "current.json").write_text(
        json.dumps({"version": version, "bounds": bounds, "revision": revision}), encoding="utf-8")


def lerc(tile_root, name, bbox, engine_version=4, complete=True):
    svc = tile_root / "arcgis" / "rest" / "services" / name
    (svc / "tile" / "13").mkdir(parents=True)
    (svc / "ImageServer.json").write_text("{}", encoding="utf-8")
    (svc / "build_manifest.json").write_text(json.dumps(
        {"engine_version": engine_version, "bbox": bbox, "minzoom": 0, "maxzoom": 13,
         "complete": complete}), encoding="utf-8")


def test_empty_volume_plans_full_build_of_primary_then_lerc_of_others(env):
    auto, _jobs, _tiles = env
    plan = auto.plan()
    assert plan["published"]["ok"] is False
    # no job history: the largest area owns the published set
    assert plan["primary"] == "Saudi_Arabia"
    assert [(t["name"], t["mode"]) for t in plan["tasks"]] == [("Saudi_Arabia", "full"), ("Riyadh", "lerc")]


def test_everything_tiled_needs_nothing(env):
    auto, _jobs, tiles = env
    publish(tiles, SAUDI)
    lerc(tiles, "Saudi_Arabia", SAUDI)
    lerc(tiles, "Riyadh", RIYADH)
    status = auto.status()
    assert status["ready"] is True and status["tasks"] == []
    assert status["primary"] == "Saudi_Arabia"


@pytest.mark.parametrize("kwargs, reason", [
    ({"engine_version": 2}, "engine v2"),
    ({"complete": False}, "did not finish"),
])
def test_stale_lerc_gets_lerc_only_rebuild_without_republish(env, kwargs, reason):
    auto, _jobs, tiles = env
    publish(tiles, SAUDI)
    lerc(tiles, "Saudi_Arabia", SAUDI)
    lerc(tiles, "Riyadh", RIYADH, **kwargs)
    tasks = auto.plan()["tasks"]
    assert [(t["name"], t["mode"]) for t in tasks] == [("Riyadh", "lerc")]
    assert reason in tasks[0]["reason"]


def test_lerc_without_manifest_is_an_older_engine(env):
    auto, _jobs, tiles = env
    publish(tiles, SAUDI)
    lerc(tiles, "Saudi_Arabia", SAUDI)
    svc = tiles / "arcgis" / "rest" / "services" / "Riyadh"
    (svc / "tile" / "13").mkdir(parents=True)
    (svc / "ImageServer.json").write_text("{}", encoding="utf-8")
    assert auto.plan()["tasks"][0]["reason"] == "built by an older engine (no build manifest)"


def test_primary_with_valid_publish_but_stale_lerc_is_lerc_only(env):
    auto, _jobs, tiles = env
    publish(tiles, SAUDI)
    lerc(tiles, "Saudi_Arabia", SAUDI, engine_version=3)
    lerc(tiles, "Riyadh", RIYADH)
    assert [(t["name"], t["mode"]) for t in auto.plan()["tasks"]] == [("Saudi_Arabia", "lerc")]


def test_older_publish_layout_rebuilds_primary(env):
    auto, _jobs, tiles = env
    publish(tiles, SAUDI, revision=2)
    lerc(tiles, "Saudi_Arabia", SAUDI)
    lerc(tiles, "Riyadh", RIYADH)
    tasks = auto.plan()["tasks"]
    assert [(t["name"], t["mode"]) for t in tasks] == [("Saudi_Arabia", "full")]
    assert "revision 2" in tasks[0]["reason"]


def test_runner_builds_missing_areas_one_job_at_a_time(env):
    auto, jobs, _tiles = env
    assert auto.start() is True
    assert auto.start() is False          # one pass at a time
    auto.wait(timeout=60)
    state = auto.status()["autobuild"]
    assert state["state"] == "done", state
    assert state["done"] == ["Saudi_Arabia (full)", "Riyadh (lerc)"]
    history = jobs.list_jobs()[::-1]   # newest first -> run order
    assert [(j["name"], [s["name"] for s in j["steps"]], j["origin"]) for j in history] == [
        ("Saudi_Arabia", ["check", "build", "verify", "publish"], "autobuild"),
        ("Riyadh", ["check", "build"], "autobuild"),
    ]
    assert all(s["state"] == "done" for j in history for s in j["steps"])
    lerc_log = jobs.get_job(history[1]["id"], tail=50)["log"]
    assert any(line.startswith("stub build:") and "--lerc-only" in line for line in lerc_log)
    full_log = jobs.get_job(history[0]["id"], tail=50)["log"]
    assert not any("--lerc-only" in line for line in full_log)
    assert history[1]["result"]["lercOnly"] is True


def test_runner_stops_when_disk_is_low(env, monkeypatch):
    auto, jobs, _tiles = env
    monkeypatch.setenv("GIS_AUTOBUILD_MIN_FREE_GB", "1e12")
    auto.start()
    auto.wait(timeout=30)
    state = auto.status()["autobuild"]
    assert state["state"] == "blocked" and "GB free" in state["message"]
    assert jobs.list_jobs() == []


def test_tiles_status_and_rebuild_endpoints(env, engine):
    client = engine
    sys.modules["app.autobuild"].POLL_S = 0.05     # client reloaded the module
    status = client.get("/api/tiles/status")
    assert status.status_code == 200
    body = status.json()
    assert body["ready"] is False and body["published"]["ok"] is False
    assert {a["name"] for a in body["areas"]} == {"Saudi_Arabia", "Riyadh"}
    assert body["autobuild"]["enabled"] is False     # disabled in tests (conftest)
    started = client.post("/api/tiles/rebuild-missing")
    assert started.status_code == 202
    sys.modules["app.autobuild"].wait(timeout=60)
    deadline = time.time() + 10
    while client.get("/api/tiles/status").json()["autobuild"]["state"] != "done" and time.time() < deadline:
        time.sleep(0.1)
    assert client.get("/api/tiles/status").json()["autobuild"]["state"] == "done"


# ---- areas handed over by the data-platform ------------------------------------

def mark_downloaded(name):
    catalog = sys.modules["app.catalog"]
    area = catalog.find_area_by_name(name)
    catalog.update_area_dataset(area["id"], state="downloaded", pendingBuild="full",
                                message="engine not running - it builds this area when it starts")


def wait_for(predicate, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def test_downloaded_area_is_planned_as_full_build_first(env):
    auto, _jobs, tiles = env
    publish(tiles, SAUDI)
    lerc(tiles, "Saudi_Arabia", SAUDI)
    lerc(tiles, "Riyadh", RIYADH)
    mark_downloaded("Riyadh")
    tasks = auto.plan()["tasks"]
    assert [(t["name"], t["mode"]) for t in tasks] == [("Riyadh", "full")]
    assert "downloaded by the data-platform" in tasks[0]["reason"]


def test_periodic_check_builds_an_area_downloaded_while_running(env):
    auto, jobs, tiles = env
    publish(tiles, SAUDI)
    lerc(tiles, "Saudi_Arabia", SAUDI)
    lerc(tiles, "Riyadh", RIYADH)
    assert auto.start_periodic(interval=0.1) is True
    try:
        time.sleep(0.4)
        assert jobs.list_jobs() == []                    # everything tiled: nothing to do
        mark_downloaded("Riyadh")                         # the data-platform hands one over
        job = wait_for(lambda: next((j for j in jobs.list_jobs() if j["state"] == "done"), None))
        assert job["name"] == "Riyadh" and job["origin"] == "autobuild"
        assert [s["name"] for s in job["steps"]] == ["check", "build", "verify", "publish"]
        dataset = sys.modules["app.catalog"].find_area_by_name("Riyadh")["dataset"]
        assert dataset["state"] == "done" and "pendingBuild" not in dataset
    finally:
        auto.stop()


def test_periodic_check_does_not_retry_a_failed_area(env, monkeypatch):
    auto, jobs, tiles = env
    publish(tiles, SAUDI)
    lerc(tiles, "Saudi_Arabia", SAUDI)
    mark_downloaded("Riyadh")
    monkeypatch.setenv("STUB_NO_SOURCES", "1")          # its DEMs are not on the volume
    auto.start_periodic(interval=0.1)
    try:
        wait_for(lambda: auto.status()["autobuild"]["state"] == "error")
        time.sleep(1.0)                                   # several periodic ticks
        history = jobs.list_jobs()
        assert len(history) == 1 and history[0]["state"] == "error"
        assert "never downloads" in history[0]["error"]
        assert auto.status()["autobuild"]["skipped"] == ["Riyadh"]
    finally:
        auto.stop()


def test_engine_restart_resumes_an_interrupted_full_build(env, monkeypatch):
    _auto, _jobs, tiles = env
    publish(tiles, SAUDI)
    lerc(tiles, "Saudi_Arabia", SAUDI)
    lerc(tiles, "Riyadh", RIYADH)
    catalog = sys.modules["app.catalog"]
    area = catalog.find_area_by_name("Riyadh")
    catalog.update_area_dataset(area["id"], state="building", pendingBuild="full", mode="full")
    reload_platform()                                     # == engine restart
    auto = sys.modules["app.autobuild"]
    dataset = sys.modules["app.catalog"].find_area_by_name("Riyadh")["dataset"]
    assert dataset["state"] == "interrupted" and dataset["pendingBuild"] == "full"
    tasks = auto.plan()["tasks"]
    assert [(t["name"], t["mode"]) for t in tasks] == [("Riyadh", "full")]
    assert "resuming" in tasks[0]["reason"]


def test_platform_restart_leaves_engine_builds_alone(env, monkeypatch):
    catalog = sys.modules["app.catalog"]
    area = catalog.find_area_by_name("Riyadh")
    catalog.update_area_dataset(area["id"], state="building")
    monkeypatch.setenv("GIS_ROLE", "platform")
    reload_platform()                                     # the data-platform (re)starts
    assert sys.modules["app.catalog"].find_area_by_name("Riyadh")["dataset"]["state"] == "building"
