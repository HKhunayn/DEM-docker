"""fetch_fabdem: atomic .part extraction with size + TIFF validation."""

import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
fetch_fabdem = pytest.importorskip("fetch_fabdem")  # needs remotezip

TIFF = b"II*\x00" + os.urandom(10000)
MEMBER = "N24E046_FABDEM_V1-2.tif"


def _zip(tmp_path, data=TIFF, name=MEMBER):
    zp = tmp_path / "block.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, data)
    return zp


def _parts(d):
    return [p for p in os.listdir(d) if p.endswith(".part")]


def test_extract_member_atomic(tmp_path):
    zp = _zip(tmp_path)
    dest = str(tmp_path / MEMBER)
    with zipfile.ZipFile(zp) as z:
        n = fetch_fabdem.extract_member(z, MEMBER, dest)
    assert n == len(TIFF)
    assert open(dest, "rb").read() == TIFF
    assert fetch_fabdem.is_cached(dest)
    assert _parts(tmp_path) == []


def test_extract_member_size_mismatch_leaves_nothing(tmp_path):
    zp = _zip(tmp_path)
    dest = str(tmp_path / MEMBER)

    class Lying(zipfile.ZipFile):
        def getinfo(self, name):
            info = super().getinfo(name)
            info.file_size += 1  # directory claims more than the stream yields
            return info

    with Lying(zp) as z, pytest.raises(Exception):
        # zipfile itself may detect the mismatch first; either way: no output
        fetch_fabdem.extract_member(z, MEMBER, dest)
    assert not os.path.exists(dest)
    assert _parts(tmp_path) == []


def test_extract_member_non_tiff_rejected(tmp_path):
    zp = _zip(tmp_path, data=b"<html>error page</html>")
    dest = str(tmp_path / MEMBER)
    with zipfile.ZipFile(zp) as z, pytest.raises(IOError, match="not a TIFF"):
        fetch_fabdem.extract_member(z, MEMBER, dest)
    assert not os.path.exists(dest)
    assert _parts(tmp_path) == []


def test_extract_member_stream_error_cleans_part(tmp_path):
    zp = _zip(tmp_path)
    dest = str(tmp_path / MEMBER)

    class Boom(zipfile.ZipFile):
        def open(self, *a, **kw):
            f = super().open(*a, **kw)
            orig = f.read

            def read(n=-1):
                orig(n)
                raise ConnectionResetError("peer reset")
            f.read = read
            return f

    with Boom(zp) as z, pytest.raises(ConnectionResetError):
        fetch_fabdem.extract_member(z, MEMBER, dest)
    assert not os.path.exists(dest)
    assert _parts(tmp_path) == []


def test_is_cached(tmp_path):
    p = tmp_path / "x.tif"
    assert not fetch_fabdem.is_cached(str(p))
    p.write_bytes(b"")
    assert not fetch_fabdem.is_cached(str(p))
    p.write_bytes(b"garbage")
    assert not fetch_fabdem.is_cached(str(p))
    p.write_bytes(TIFF)
    assert fetch_fabdem.is_cached(str(p))


def test_check_mode_offline(tmp_path, capsys):
    (tmp_path / MEMBER).write_bytes(TIFF)
    old = sys.argv
    sys.argv = ["fetch_fabdem", "--bbox", "46.2", "24.2", "47.5", "24.8",
                "--out", str(tmp_path), "--check"]
    try:
        rc = fetch_fabdem.main()
    finally:
        sys.argv = old
    out = capsys.readouterr().out
    assert rc == 0
    assert "check: 1/2 FABDEM tiles already downloaded" in out
