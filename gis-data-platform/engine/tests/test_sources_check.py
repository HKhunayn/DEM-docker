"""`terrainhub sources`: the build engine's offline source check. It must
never touch the network and must fail clearly when the DEMs are missing."""
import os
import sys

import pytest

_ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ENGINE, os.path.join(_ENGINE, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture()
def no_network(monkeypatch):
    import fetch_dem

    def boom(*args, **kwargs):
        raise AssertionError("terrainhub sources must not download anything")

    monkeypatch.setattr(fetch_dem.urllib.request, "urlopen", boom)
    monkeypatch.setattr(fetch_dem, "download", boom)
    monkeypatch.setattr(fetch_dem, "fetch_missing", boom)


def run(monkeypatch, *argv):
    import terrainhub
    monkeypatch.setattr(sys, "argv", ["terrainhub.py", "sources", *argv])
    return terrainhub.main()


def test_sources_without_dems_fails_clearly(tmp_path, monkeypatch, capsys, no_network):
    data = tmp_path / "dem"
    data.mkdir()
    rc = run(monkeypatch, "--bbox", "46.7", "24.6", "46.8", "24.7", "--data", str(data),
             "--root", str(tmp_path / "tiles"))
    out = capsys.readouterr()
    assert rc == 3
    assert "missing Copernicus_DSM_COG_10_N24_00_E046_00_DEM.tif" in out.out
    assert "MISSING source DEMs" in out.err and "does not download" in out.err


def test_sources_with_dems_on_disk_passes(tmp_path, monkeypatch, capsys, no_network):
    data = tmp_path / "dem"
    data.mkdir()
    (data / "Copernicus_DSM_COG_10_N24_00_E046_00_DEM.tif").write_bytes(b"II*\x00" + b"\x00" * 64)
    rc = run(monkeypatch, "--bbox", "46.7", "24.6", "46.8", "24.7", "--data", str(data),
             "--root", str(tmp_path / "tiles"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "cached  Copernicus_DSM_COG_10_N24_00_E046_00_DEM.tif" in out
    assert "sources OK: 1 DEM file(s)" in out
