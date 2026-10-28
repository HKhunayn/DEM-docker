from __future__ import annotations

import importlib
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

# Stub heightmap engine: stands in for heightmap-30m/terrainhub.py so jobs run
# through the real subprocess path without downloading DEM tiles.
#   STUB_SLEEP=N        sleep N s (silently) in the STUB_SLEEP_STEP step (default build)
#   STUB_FAIL=step      that step prints FAIL and exits 1
#   STUB_SPAWN_CHILD=1  the slept step starts a grandchild that sleeps too
#                       (cancel must take the whole process group down)
STUB_TERRAINHUB = textwrap.dedent(
    '''
    import os, subprocess, sys, time
    cmd = sys.argv[1]
    sleep = float(os.environ.get("STUB_SLEEP", "0"))
    print(f"stub {cmd}: " + " ".join(sys.argv[2:]), flush=True)
    if cmd == os.environ.get("STUB_SLEEP_STEP", "build") and sleep:
        if os.environ.get("STUB_SPAWN_CHILD"):
            child = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({sleep})"])
            with open(os.environ["STUB_CHILD_PIDFILE"], "w") as fh:
                fh.write(str(child.pid))
        time.sleep(sleep)
    if os.environ.get("STUB_FAIL") == cmd:
        print("FAIL stub failure")
        raise SystemExit(1)
    if cmd == "fetch":
        print("  saved   data/tile.tif (1.0 MB)")
        print("Done: 2 tile(s) -> manifest data/manifest.json")
    elif cmd == "build":
        print("WROTE 42 tiles z0-13, 1.2 MB")
        print("WROTE 42 LERC tiles (0.5 MB) + ImageServer.json -> public")
        if os.environ.get("STUB_TILEJSON"):
            root = sys.argv[sys.argv.index("--root") + 1]
            with open(os.path.join(root, "tilejson.json"), "w") as fh:
                fh.write(os.environ["STUB_TILEJSON"])
    elif cmd == "verify":
        print("stats match: PASS")
    print(f"stub {cmd}: done")
    '''
)

# Modules that capture DATA_ROOT / ENGINE_ROOT at import time.
PLATFORM_MODULES = ["app.config", "app.catalog", "app.imagery", "app.jobs", "app.fetch_api", "app.imagery_api"]


@pytest.fixture()
def stub_engine(tmp_path):
    root = tmp_path / "engine"
    leaflet = root / "node_modules" / "leaflet" / "dist"
    leaflet.mkdir(parents=True, exist_ok=True)
    (leaflet / "leaflet.js").write_text("/* leaflet js */", encoding="utf-8")
    (leaflet / "leaflet.css").write_text("/* leaflet css */", encoding="utf-8")
    (root / "terrainhub.py").write_text(STUB_TERRAINHUB, encoding="utf-8")
    return root


def reload_platform(include_main: bool = False):
    modules = PLATFORM_MODULES + (["app.main"] if include_main else [])
    for name in modules:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    return sys.modules["app.jobs"]


@pytest.fixture(autouse=True)
def isolated_platform(tmp_path, monkeypatch, stub_engine):
    """Every test gets its own data root, the stub engine and fresh job state.

    Applies to ALL tests (including ones that reload app.main themselves), so
    no test can ever launch the real heightmap engine or see another test's
    jobs, provider cache or lock file.
    """
    monkeypatch.setenv("GIS_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("LEGACY_HEIGHTMAP_ROOT", str(tmp_path / "legacy"))
    monkeypatch.setenv("HEIGHTMAP_ENGINE_ROOT", str(stub_engine))
    monkeypatch.setenv("HEIGHTMAP_PYTHON", sys.executable)
    # ingest is restricted to an allow-listed dir; tests ingest from tmp_path
    monkeypatch.setenv("GIS_INGEST_ROOT", str(tmp_path))
    for var in ("STUB_SLEEP", "STUB_SLEEP_STEP", "STUB_FAIL", "STUB_SPAWN_CHILD", "STUB_TILEJSON",
                "GIS_JOB_IDLE_TIMEOUT_S", "GIS_INGEST_MAX_BYTES"):
        monkeypatch.delenv(var, raising=False)
    jobs = reload_platform()
    assert str(jobs.ENGINE_ROOT) == str(stub_engine)
    with jobs._LOCK:
        jobs._JOBS.clear()
        jobs._ORDER.clear()
        jobs._CURRENT_ID = None
    jobs._PROVIDER_CACHE = None
    jobs._PROVIDER_CACHE_T = 0.0
    jobs._remove_lock()
    # captured now: a test may monkeypatch jobs.shutdown (undone after this teardown)
    real_shutdown, wait_threads = jobs.shutdown, jobs._wait_threads
    yield
    real_shutdown(grace=2.0, wait=5.0)
    wait_threads(timeout=10.0)
    current = sys.modules["app.jobs"]
    if current.shutdown is not real_shutdown and getattr(current, "_wait_threads", None):
        current._wait_threads(timeout=10.0)  # the test reloaded app.jobs


@pytest.fixture()
def client(isolated_platform):
    reload_platform(include_main=True)
    return TestClient(sys.modules["app.main"].app)
