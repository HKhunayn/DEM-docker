"""serve.py: path containment for tile/vendor routes and cache headers."""

import http.client
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
import serve  # noqa: E402

SECRET = b"TOP-SECRET-CONTENT"


@pytest.fixture()
def srv(tmp_path):
    root = tmp_path / "public"
    tiles = tmp_path / "tiles"
    (root).mkdir()
    (root / "index.html").write_text("<h1>ui</h1>")
    (tiles / "terrarium" / "0" / "0").mkdir(parents=True)
    (tiles / "terrarium" / "0" / "0" / "0.png").write_bytes(b"\x89PNG tile")
    (tiles / "hillshade" / "0" / "0").mkdir(parents=True)
    (tiles / "hillshade" / "0" / "0" / "0.png").write_bytes(b"\x89PNG hs")
    svc = tiles / "arcgis" / "rest" / "services" / "dem"
    (svc / "tile" / "0" / "0").mkdir(parents=True)
    (svc / "tile" / "0" / "0" / "0").write_bytes(b"CntZImage blob")
    (svc / "ImageServer.json").write_text('{"name": "dem"}')
    (tiles / "tilejson.json").write_text('{"tiles": []}')
    # files that must never be reachable
    (tmp_path / "secret.txt").write_bytes(SECRET)
    (tmp_path / "arcgis_secret").mkdir()
    (tmp_path / "arcgis_secret" / "ImageServer.json").write_bytes(SECRET)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.png").write_bytes(SECRET)
    try:
        os.symlink(outside, tiles / "terrarium" / "evil", target_is_directory=True)
    except (OSError, NotImplementedError):
        pass

    handler = serve.make_handler(str(root), str(tiles))
    httpd = serve.Server(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd.server_address[1], tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()


def get(port, raw_path, method="GET"):
    """Send the request-target verbatim (no client-side normalisation)."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest(method, raw_path, skip_accept_encoding=True)
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict((k.lower(), v) for k, v in r.getheaders()), body


@pytest.mark.parametrize("path", [
    "/terrarium/0/0/0.png",
    "/hillshade/0/0/0.png",
    "/arcgis/rest/services/dem/ImageServer/tile/0/0/0",
])
def test_tiles_200_with_bounded_cache(srv, path):
    port, _ = srv
    status, headers, body = get(port, path)
    assert status == 200 and body
    cc = headers.get("cache-control", "")
    assert "max-age=86400" in cc
    assert "immutable" not in cc and "31536000" not in cc


def test_head_tile(srv):
    port, _ = srv
    status, headers, _ = get(port, "/terrarium/0/0/0.png", "HEAD")
    assert status == 200 and "max-age=86400" in headers.get("cache-control", "")


@pytest.mark.parametrize("path", [
    "/terrarium/9/9/9.png",
    "/hillshade/9/9/9.png",
    "/arcgis/rest/services/dem/ImageServer/tile/9/9/9",
    "/arcgis/rest/services/nope/ImageServer",
])
def test_404_never_cached(srv, path):
    port, _ = srv
    status, headers, _ = get(port, path)
    assert status == 404
    assert "cache-control" not in headers


def test_metadata_routes(srv):
    port, _ = srv
    assert get(port, "/arcgis/rest/services/dem/ImageServer?f=json")[0] == 200
    assert get(port, "/tilejson.json")[0] == 200
    assert get(port, "/index.html")[0] == 200


TRAVERSALS = [
    "/terrarium/../secret.txt",
    "/terrarium/../../secret.txt",
    "/terrarium/%2e%2e/%2e%2e/secret.txt",
    "/terrarium/%2E%2E/%2E%2E/secret.txt",
    "/terrarium/%252e%252e/%252e%252e/secret.txt",
    "/terrarium/..%5c..%5csecret.txt",
    "/terrarium/..%5C..%5Csecret.txt",
    "/terrarium/..\\..\\secret.txt",
    "/terrarium/0/..%2f..%2f..%2fsecret.txt",
    "/terrarium//etc/passwd",
    "/terrarium/%2fetc%2fpasswd",
    "/tilejson/..%2f..%2fsecret.txt",
    "/hillshade/C:%5Cwindows%5Cwin.ini",
    "/hillshade/C:/windows/win.ini",
    "/hillshade/x%00.png",
    "/terrarium/evil/leak.png",  # symlink pointing outside the tiles root
    "/vendor/../../secret.txt",
    "/vendor/..%5c..%5csecret.txt",
    "/arcgis/rest/services/..%5C..%5C..%5C..%5Carcgis_secret/ImageServer",
    "/arcgis/rest/services/..%5C..%5C..%5C..%5Coutside/ImageServer/tile/0/0/0",
    "/arcgis/rest/services/%2e%2e/ImageServer",
    "/../secret.txt",
    "/%2e%2e/secret.txt",
    "/..%5csecret.txt",
    "http://127.0.0.1/terrarium/../../secret.txt",
]


@pytest.mark.parametrize("path", TRAVERSALS)
def test_traversal_blocked(srv, path):
    port, _ = srv
    status, headers, body = get(port, path)
    assert status in (400, 403, 404), (path, status)
    assert SECRET not in body
    assert b"root:" not in body
    assert "cache-control" not in headers


def test_safe_join_unit(tmp_path):
    base = str(tmp_path)
    assert serve.safe_join(base, "a", "b.png") == os.path.join(os.path.realpath(base), "a", "b.png")
    assert serve.safe_join(base, "", "a", "") is not None
    for bad in ("..", ".", "a\\b", "c:", "x\x00", "a/b", os.sep + "etc"):
        assert serve.safe_join(base, "ok", bad) is None, bad
