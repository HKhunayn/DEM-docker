"""POST /api/ingest source restrictions (catalog.ingest_file) - no auth, just an allow-listed dir."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def catalog_mod():
    return sys.modules["app.catalog"]


@pytest.fixture()
def incoming(tmp_path, monkeypatch):
    root = tmp_path / "incoming"
    root.mkdir()
    monkeypatch.setenv("GIS_INGEST_ROOT", str(root))
    return root


def test_default_ingest_root_is_data_incoming_and_created(tmp_path, monkeypatch):
    monkeypatch.delenv("GIS_INGEST_ROOT")
    config = sys.modules["app.config"]
    assert config.ingest_root() == config.DATA_ROOT / "incoming"
    assert (tmp_path / "data" / "incoming").is_dir()


def test_ingest_inside_root_works_relative_and_absolute(incoming):
    catalog = catalog_mod()
    (incoming / "sub").mkdir()
    (incoming / "sub" / "dem.tif").write_bytes(b"raster")
    item = catalog.ingest_file(Path("sub/dem.tif"), "elevation", "  My DEM  ")
    assert item["title"] == "My DEM"
    assert catalog.item_path(item).read_bytes() == b"raster"
    assert not list(catalog.item_path(item).parent.glob("*.part"))
    item2 = catalog.ingest_file(incoming / "sub" / "dem.tif", "elevation")
    assert item2["title"] == "dem"


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.tif", "sub/../../outside.tif"])
def test_ingest_outside_root_is_forbidden(incoming, path):
    (incoming.parent / "outside.tif").write_bytes(b"secret")
    (incoming / "sub").mkdir(exist_ok=True)
    with pytest.raises(PermissionError):
        catalog_mod().ingest_file(Path(path))
    assert catalog_mod().catalog_items() == []


def test_ingest_nonexistent_path_outside_root_is_forbidden_not_404(incoming):
    with pytest.raises(PermissionError):
        catalog_mod().ingest_file(Path("/no/such/file.tif"))
    with pytest.raises(FileNotFoundError):
        catalog_mod().ingest_file(incoming / "missing.tif")


@pytest.mark.skipif(os.name != "posix", reason="symlinks need privileges on Windows")
def test_symlink_escaping_root_is_forbidden(incoming):
    target = incoming.parent / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    (incoming / "link.tif").symlink_to(target)
    (incoming / "dirlink").symlink_to(incoming.parent, target_is_directory=True)
    with pytest.raises(PermissionError):
        catalog_mod().ingest_file(incoming / "link.tif")
    with pytest.raises(PermissionError):
        catalog_mod().ingest_file(incoming / "dirlink" / "secret.txt")
    # a link that stays inside the root is fine
    (incoming / "real.tif").write_bytes(b"ok")
    (incoming / "inner.tif").symlink_to(incoming / "real.tif")
    assert catalog_mod().ingest_file(incoming / "inner.tif")["filename"] == "real.tif"


def test_non_regular_and_oversized_files_rejected(incoming, monkeypatch):
    (incoming / "folder").mkdir()
    with pytest.raises(ValueError):
        catalog_mod().ingest_file(incoming / "folder")
    if hasattr(os, "mkfifo"):
        os.mkfifo(incoming / "pipe")
        with pytest.raises(ValueError):
            catalog_mod().ingest_file(incoming / "pipe")
    (incoming / "big.tif").write_bytes(b"x" * 100)
    monkeypatch.setenv("GIS_INGEST_MAX_BYTES", "10")
    with pytest.raises(ValueError, match="too large"):
        catalog_mod().ingest_file(incoming / "big.tif")
    with pytest.raises(ValueError):
        catalog_mod().ingest_file(Path("bad\x00name.tif"))


@pytest.mark.parametrize("title", ["x" * 201, "evil\ntitle", "bell\x07", "esc\x1b[31m"])
def test_bad_titles_rejected(incoming, title):
    (incoming / "a.tif").write_bytes(b"a")
    with pytest.raises(ValueError):
        catalog_mod().ingest_file(incoming / "a.tif", "elevation", title)
    assert catalog_mod().catalog_items() == []


def test_unicode_title_allowed(incoming):
    (incoming / "a.tif").write_bytes(b"a")
    assert catalog_mod().ingest_file(incoming / "a.tif", "elevation", "جدة DEM")["title"] == "جدة DEM"
