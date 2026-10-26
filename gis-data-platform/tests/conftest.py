from __future__ import annotations

import importlib
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

# Stub heightmap engine: stands in for heightmap-30m/terrainhub.py so jobs run
# through the real subprocess path without downloading DEM tiles.
STUB_TERRAINHUB = textwrap.dedent(
    '''
    import os, sys, time
    cmd = sys.argv[1]
    sleep = float(os.environ.get("STUB_SLEEP", "0"))
    if cmd == "build" and sleep:
        time.sleep(sleep)
    print(f"stub {cmd}: " + " ".join(sys.argv[2:]))
    if os.environ.get("STUB_FAIL") == cmd:
        print("FAIL stub failure")
        raise SystemExit(1)
    if cmd == "fetch":
        print("  saved   data/tile.tif (1.0 MB)")
        print("Done: 2 tile(s) -> manifest data/manifest.json")
    elif cmd == "build":
        print("WROTE 42 tiles z0-13, 1.2 MB")
        print("WROTE 42 LERC tiles (0.5 MB) + ImageServer.json -> public")
    elif cmd == "verify":
        print("stats match: PASS")
    print(f"stub {cmd}: done")
    '''
)


@pytest.fixture()
def stub_engine(tmp_path):
    root = tmp_path / "engine"
    leaflet = root / "node_modules" / "leaflet" / "dist"
    leaflet.mkdir(parents=True)
    (leaflet / "leaflet.js").write_text("/* leaflet js */", encoding="utf-8")
    (leaflet / "leaflet.css").write_text("/* leaflet css */", encoding="utf-8")
    (root / "terrainhub.py").write_text(STUB_TERRAINHUB, encoding="utf-8")
    return root


@pytest.fixture()
def client(tmp_path, monkeypatch, stub_engine):
    monkeypatch.setenv("GIS_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("LEGACY_HEIGHTMAP_ROOT", str(tmp_path / "legacy"))
    monkeypatch.setenv("HEIGHTMAP_ENGINE_ROOT", str(stub_engine))
    monkeypatch.setenv("HEIGHTMAP_PYTHON", sys.executable)
    modules = ["app.config", "app.catalog", "app.imagery", "app.jobs", "app.fetch_api", "app.imagery_api", "app.main"]
    for name in modules:
        importlib.import_module(name)
    for name in modules:
        importlib.reload(sys.modules[name])
    return TestClient(sys.modules["app.main"].app)
