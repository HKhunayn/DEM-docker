"""fetch_dem downloader: retry / 404 / timeout / validation behaviour against a
local http.server (no internet needed)."""

import http.server
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
import fetch_dem  # noqa: E402

TIFF = b"II*\x00" + b"\x08\x00\x00\x00" + os.urandom(4096)


class _State:
    def __init__(self):
        self.hits = {}
        self.lock = threading.Lock()

    def hit(self, path):
        with self.lock:
            self.hits[path] = self.hits.get(path, 0) + 1
            return self.hits[path]


@pytest.fixture()
def server():
    state = _State()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _body(self, code, body, ctype="image/tiff", length=None, headers=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body) if length is None else length))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: C901
            path = self.path.split("?")[0]
            n = state.hit(path)
            name = path.rsplit("/", 1)[-1]
            if "always500" in path:
                return self._body(500, b"boom", "text/plain")
            if name.startswith("ok") or "Copernicus_DSM_COG_10_N00_00_E000" in path:
                return self._body(200, TIFF)
            if name.startswith("missing") or "N00_00_E001" in path:
                return self._body(404, b"<Error><Code>NoSuchKey</Code></Error>", "application/xml")
            if name.startswith("forbidden"):
                return self._body(403, b"<Error><Code>AccessDenied</Code></Error>", "application/xml")
            if name.startswith("bad-request"):
                return self._body(400, b"nope", "text/plain")
            if name.startswith("flaky"):  # 503, 429, then OK
                if n == 1:
                    return self._body(503, b"SlowDown", "text/plain")
                if n == 2:
                    return self._body(429, b"Too many", "text/plain", headers=[("Retry-After", "0")])
                return self._body(200, TIFF)
            if name.startswith("always500"):
                return self._body(500, b"boom", "text/plain")
            if name.startswith("html"):
                return self._body(200, b"<html>captive portal</html>", "text/html")
            if name.startswith("short"):  # truncated body: Content-Length lies
                if n == 1:
                    self._body(200, TIFF[:100], length=len(TIFF))
                    self.close_connection = True
                    return
                return self._body(200, TIFF)
            if name.startswith("slow"):  # stall before headers on first attempt
                if n == 1:
                    time.sleep(2.0)
                return self._body(200, TIFF)
            return self._body(404, b"", "text/plain")

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, state
    finally:
        httpd.shutdown()
        httpd.server_close()


def _parts(d):
    return [p for p in os.listdir(d) if p.endswith(".part")]


def test_ok_download_atomic(server, tmp_path):
    base, state = server
    dest = str(tmp_path / "ok.tif")
    fetch_dem.download(f"{base}/ok.tif", dest, backoff=0)
    assert open(dest, "rb").read() == TIFF
    assert _parts(tmp_path) == []


def test_404_is_missing_no_retry(server, tmp_path):
    base, state = server
    dest = str(tmp_path / "missing.tif")
    with pytest.raises(fetch_dem.MissingTile) as ei:
        fetch_dem.download(f"{base}/missing.tif", dest, backoff=0)
    assert ei.value.code == 404
    assert state.hits["/missing.tif"] == 1
    assert not os.path.exists(dest) and _parts(tmp_path) == []


def test_403_is_failure_not_ocean_no_retry(server, tmp_path):
    # the Copernicus buckets answer 404 (not 403) for absent keys, so a 403 is
    # an access problem that must surface as FAILED, not be skipped as ocean
    base, state = server
    with pytest.raises(fetch_dem.DownloadError, match="HTTP 403"):
        fetch_dem.download(f"{base}/forbidden.tif", str(tmp_path / "f.tif"), backoff=0)
    assert state.hits["/forbidden.tif"] == 1


def test_other_4xx_not_retried(server, tmp_path):
    base, state = server
    with pytest.raises(fetch_dem.DownloadError):
        fetch_dem.download(f"{base}/bad-request.tif", str(tmp_path / "b.tif"), backoff=0)
    assert state.hits["/bad-request.tif"] == 1


def test_5xx_and_429_retried_then_succeed(server, tmp_path):
    base, state = server
    dest = str(tmp_path / "flaky.tif")
    fetch_dem.download(f"{base}/flaky.tif", dest, backoff=0)
    assert state.hits["/flaky.tif"] == 3
    assert open(dest, "rb").read() == TIFF
    assert _parts(tmp_path) == []


def test_retries_exhausted_cleans_up(server, tmp_path):
    base, state = server
    dest = str(tmp_path / "always500.tif")
    with pytest.raises(fetch_dem.DownloadError, match="gave up after 3"):
        fetch_dem.download(f"{base}/always500.tif", dest, retries=3, backoff=0)
    assert state.hits["/always500.tif"] == 3
    assert not os.path.exists(dest) and _parts(tmp_path) == []


def test_non_tiff_body_rejected(server, tmp_path):
    base, state = server
    dest = str(tmp_path / "html.tif")
    with pytest.raises(fetch_dem.DownloadError, match="not a TIFF"):
        fetch_dem.download(f"{base}/html.tif", dest, retries=2, backoff=0)
    assert not os.path.exists(dest) and _parts(tmp_path) == []


def test_truncated_body_retried(server, tmp_path):
    base, state = server
    dest = str(tmp_path / "short.tif")
    fetch_dem.download(f"{base}/short.tif", dest, backoff=0)
    assert state.hits["/short.tif"] == 2
    assert open(dest, "rb").read() == TIFF


def test_timeout_retried(server, tmp_path):
    base, state = server
    dest = str(tmp_path / "slow.tif")
    t0 = time.monotonic()
    fetch_dem.download(f"{base}/slow.tif", dest, timeout=0.5, backoff=0)
    assert state.hits["/slow.tif"] == 2
    assert time.monotonic() - t0 < 10
    assert open(dest, "rb").read() == TIFF


def test_connection_refused_retried_then_fails(tmp_path):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    with pytest.raises(fetch_dem.DownloadError):
        fetch_dem.download(f"http://127.0.0.1:{port}/x.tif", str(tmp_path / "x.tif"),
                           retries=2, timeout=2, backoff=0)
    assert _parts(tmp_path) == []


def test_valid_cache_skips_network(server, tmp_path):
    base, state = server
    dest = tmp_path / "ok-cached.tif"
    dest.write_bytes(TIFF)
    fetch_dem.download(f"{base}/ok-cached.tif", str(dest), backoff=0)
    assert "/ok-cached.tif" not in state.hits


def test_invalid_cache_redownloaded(server, tmp_path):
    base, state = server
    dest = tmp_path / "ok-corrupt.tif"
    dest.write_bytes(b"<html>not a tiff</html>")
    assert not fetch_dem.is_cached(str(dest))
    fetch_dem.download(f"{base}/ok-corrupt.tif", str(dest), backoff=0)
    assert state.hits["/ok-corrupt.tif"] == 1
    assert dest.read_bytes() == TIFF


def test_stale_part_files_removed(server, tmp_path):
    base, _ = server
    dest = str(tmp_path / "ok.tif")
    stale = dest + ".123.deadbeef.part"
    fresh = dest + ".456.cafebabe.part"
    for p in (stale, fresh):
        open(p, "wb").write(b"x")
    old = time.time() - 2 * fetch_dem.STALE_PART_AGE
    os.utime(stale, (old, old))
    fetch_dem.download(f"{base}/ok.tif", dest, backoff=0)
    assert not os.path.exists(stale)
    assert os.path.exists(fresh)  # may belong to a concurrent download


def test_main_end_to_end(server, tmp_path, monkeypatch, capsys):
    base, state = server
    monkeypatch.setitem(fetch_dem.BUCKETS, 30, base)
    out = tmp_path / "dem"
    # bbox covering N00E000 (served) and N00E001 (404 -> ocean)
    rc = fetch_dem.main(["--bbox", "0.2", "0.2", "1.5", "0.8", "--out", str(out)])
    cap = capsys.readouterr()
    assert rc == 0
    assert "Done: 1 tile(s)" in cap.out  # app/jobs.py parses r"Done: (\d+) tile"
    assert "MISSING" in cap.err
    manifest = json.loads((out / "manifest.json").read_text())
    assert len(manifest["files"]) == 1 and manifest["provider"] == "copernicus"
    assert _parts(out) == []

    rc = fetch_dem.main(["--bbox", "0.2", "0.2", "1.5", "0.8", "--out", str(out), "--check"])
    cap = capsys.readouterr()
    assert rc == 0
    assert "check: 1/2 tiles already downloaded" in cap.out


def test_main_reports_failures(server, tmp_path, monkeypatch, capsys):
    base, _ = server
    monkeypatch.setitem(fetch_dem.BUCKETS, 30, base + "/always500")
    monkeypatch.setattr(fetch_dem, "BACKOFF", 0)
    monkeypatch.setattr(fetch_dem.time, "sleep", lambda s: None)
    rc = fetch_dem.main(["--bbox", "0.2", "0.2", "0.8", "0.8", "--out", str(tmp_path),
                         "--retries", "2"])
    cap = capsys.readouterr()
    assert rc == 1
    assert "FAILED" in cap.err
