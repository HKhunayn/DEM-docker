"""Settings page + API: precedence, validation, live effect on the engine."""
import importlib
import json
import sys


def test_settings_page_and_nav(client):
    page = client.get("/settings/")
    assert page.status_code == 200 and "Engine settings" in page.text
    assert client.get("/settings", follow_redirects=False).status_code == 307
    for path in ("/", "/library/"):
        assert 'href="/settings/"' in client.get(path).text


def test_get_settings_lists_groups_with_sources(client, monkeypatch):
    monkeypatch.setenv("BUILD_WORKER_GB", "3")
    data = client.get("/api/settings").json()
    assert [g["id"] for g in data["groups"]] == ["build", "auto", "downloads"]
    items = {it["key"]: it for g in data["groups"] for it in g["items"]}
    assert items["buildMemBudgetGB"]["value"] == 0 and items["buildMemBudgetGB"]["source"] == "default"
    assert items["buildWorkerGB"]["value"] == 3 and items["buildWorkerGB"]["source"] == "env"
    assert items["autobuild"]["value"] is False and items["autobuild"]["source"] == "env"   # conftest: GIS_AUTOBUILD=0
    assert all(it["applies"] in ("live", "next-job", "restart") for it in items.values())


def test_put_validates_saves_and_resets(client, monkeypatch):
    monkeypatch.setenv("BUILD_WORKER_GB", "3")
    bad = client.put("/api/settings", json={"values": {"buildMemBudgetGB": -1}})
    assert bad.status_code == 400 and "between" in bad.json()["detail"]
    assert client.put("/api/settings", json={"values": {"buildWorkers": 2.5}}).status_code == 400
    assert client.put("/api/settings", json={"values": {"nope": 1}}).status_code == 400
    assert client.put("/api/settings", json={"values": {"autobuild": "maybe"}}).status_code == 400

    ok = client.put("/api/settings", json={"values": {"buildMemBudgetGB": 6, "buildWorkerGB": 1.5,
                                                      "autobuild": True}})
    assert ok.status_code == 200
    items = {it["key"]: it for g in ok.json()["groups"] for it in g["items"]}
    assert items["buildMemBudgetGB"]["value"] == 6 and items["buildMemBudgetGB"]["source"] == "saved"
    assert items["buildWorkerGB"]["value"] == 1.5 and items["buildWorkerGB"]["fallback"] == 3
    settings = sys.modules["app.settings"]
    saved = json.loads(settings.path().read_text())
    assert saved["values"] == {"autobuild": True, "buildMemBudgetGB": 6.0, "buildWorkerGB": 1.5}

    env = settings.job_env()
    assert env["BUILD_MEM_BUDGET_GB"] == "6.0" and env["BUILD_WORKER_GB"] == "1.5"
    assert env["GDAL_CACHEMAX"] == "128" and "GIS_JOB_IDLE_TIMEOUT_S" not in env

    reset = client.put("/api/settings", json={"reset": ["buildWorkerGB"], "values": {"autobuild": None}})
    items = {it["key"]: it for g in reset.json()["groups"] for it in g["items"]}
    assert items["buildWorkerGB"]["value"] == 3 and items["buildWorkerGB"]["source"] == "env"
    assert items["autobuild"]["source"] == "env"
    assert items["buildMemBudgetGB"]["source"] == "saved"


def test_settings_change_engine_behaviour_live(client, monkeypatch):
    client.put("/api/settings", json={"values": {"autobuild": True, "autobuildIntervalS": 5,
                                                 "autobuildMinFreeGB": 2, "jobIdleTimeoutS": 120,
                                                 "maxImageryTiles": 1000, "imageryWorkers": 2}})
    autobuild = sys.modules["app.autobuild"]
    assert autobuild.enabled() is True and autobuild.interval_s() == 5 and autobuild.min_free_gb() == 2
    assert sys.modules["app.jobs"].idle_timeout_s() == 120
    imagery = sys.modules["app.imagery"]
    assert imagery.max_tiles_per_job() == 1000
    response = client.post("/api/imagery/jobs", json={"name": "big", "provider": "bluemarble",
                                                      "minzoom": 0, "maxzoom": 6})
    assert response.status_code == 400 and "1000" in response.json()["detail"]


def test_runtime_endpoints(client, serve_client):
    # engine side (internal): limits + build estimate
    engine = serve_client.get("/api/runtime").json()
    assert engine["cpuCount"] >= 1 and "build" in engine and engine["build"]["workers"] >= 1
    assert "autobuild" in engine
    # platform side: engine unreachable in tests -> message, platform facts still there
    data = client.get("/api/settings/runtime").json()
    assert data["platform"]["cpuCount"] >= 1
    assert data["engine"] is None and data["engineMessage"]


def test_build_estimate_follows_budget(serve_client, client):
    build_api = sys.modules["app.build_api"]
    info = {"cpuCount": 16, "memoryLimit": 20 * 1024 ** 3, "memoryUsed": 0,
            "vmMemoryTotal": 32 * 1024 ** 3, "vmMemoryAvailable": 30 * 1024 ** 3}
    assert build_api.estimate_build(info)["workers"] == 7          # 20 GB * 0.75 / 2 GB
    client.put("/api/settings", json={"values": {"buildMemBudgetGB": 8}})
    est = build_api.estimate_build(info)
    assert est["workers"] == 3 and est["limitBy"] == "build RAM limit (setting)"
    client.put("/api/settings", json={"values": {"buildMemBudgetGB": 0, "buildWorkers": 2}})
    est = build_api.estimate_build(info)
    assert est["workers"] == 2 and est["limitBy"] == "max build workers (setting)"


def test_pool_runner_honours_budget_and_max_workers(monkeypatch):
    import os
    engine_scripts = os.path.join(os.path.dirname(__file__), "..", "engine", "scripts")
    monkeypatch.syspath_prepend(engine_scripts)
    monkeypatch.setenv("BUILD_MEM_BUDGET_GB", "4")
    monkeypatch.setenv("BUILD_MAX_WORKERS", "3")
    monkeypatch.setenv("BUILD_WORKER_GB", "1")
    pool_runner = importlib.import_module("pool_runner")
    pool_runner = importlib.reload(pool_runner)
    if pool_runner.mem_candidates():           # Linux: budget is one of the limits
        assert any(limit == 4 * 1024 ** 3 for _, limit in pool_runner.mem_candidates())
        assert pool_runner.default_workers() <= 3
    monkeypatch.delenv("BUILD_MEM_BUDGET_GB")
    monkeypatch.delenv("BUILD_MAX_WORKERS")
    importlib.reload(pool_runner)


def test_engine_view_overrides_engine_settings(client, monkeypatch):
    """The engine's .env (e.g. GIS_AUTOBUILD=0 there) wins over what the
    data-platform container sees, for engine settings only."""
    handoff = sys.modules["app.handoff"]
    view = {"autobuild": {"value": False, "source": "env", "envValue": False},
            "buildWorkerGB": {"value": 4.0, "source": "env", "envValue": 4.0}}

    def fake_request(method, path, body=None, timeout=None):
        assert path == "/api/runtime"
        return 200, {"settings": view, "cpuCount": 8}

    monkeypatch.setattr(handoff, "request", fake_request)
    monkeypatch.delenv("GIS_AUTOBUILD", raising=False)
    data = client.get("/api/settings").json()
    assert data["engineView"] is True
    items = {it["key"]: it for g in data["groups"] for it in g["items"]}
    assert items["autobuild"]["value"] is False and items["autobuild"]["source"] == "env"
    assert items["buildWorkerGB"]["value"] == 4.0 and items["buildWorkerGB"]["fallback"] == 4.0
    assert items["imageryWorkers"]["source"] == "default"        # platform setting: local view
    assert client.get("/api/settings/runtime").json()["engine"]["cpuCount"] == 8
