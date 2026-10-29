"""catalog.json is edited by two processes (data-platform + engine): every
load-modify-write holds an flock, so no process loses another's edit."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

WRITER = textwrap.dedent("""
    import sys
    from app import catalog
    prefix, n = sys.argv[1], int(sys.argv[2])
    for i in range(n):
        catalog.upsert_area(name=f"{prefix}{i}", bbox=[0, 0, 1, 1], provider="copernicus", minzoom=0, maxzoom=1)
        area = catalog.find_area_by_name(f"{prefix}{i}")
        catalog.update_area_dataset(area["id"], state="downloaded", n=i)
""")


@pytest.mark.skipif(os.name != "posix", reason="flock is POSIX")
def test_two_processes_editing_the_catalog_lose_nothing(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "GIS_DATA_ROOT": str(tmp_path / "shared"), "PYTHONPATH": str(root)}
    n = 40
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, prefix, str(n)], env=env, cwd=str(root))
             for prefix in ("platform_", "engine_")]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    out = subprocess.run([sys.executable, "-c",
                          "from app import catalog; import json; print(json.dumps(catalog.list_areas()))"],
                         env=env, cwd=str(root), capture_output=True, text=True, check=True)
    import json
    areas = json.loads(out.stdout)
    assert len(areas) == 2 * n
    assert all(a["dataset"]["state"] == "downloaded" for a in areas)
    assert (tmp_path / "shared" / "catalog" / ".catalog.lock").exists()


def test_edit_lock_is_reentrant(tmp_path):
    catalog = sys.modules["app.catalog"]
    with catalog.edit_lock():
        with catalog.edit_lock():
            catalog.upsert_area(name="r", bbox=[0, 0, 1, 1], provider="copernicus", minzoom=0, maxzoom=1)
    assert catalog.find_area_by_name("r") is not None
