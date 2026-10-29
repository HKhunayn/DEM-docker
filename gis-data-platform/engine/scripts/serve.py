"""Local tile server exposing the heightmap engine's outputs:

  /terrarium/{z}/{x}/{y}.png                          Terrarium PNG pyramid
  /tilejson.json                                      pyramid metadata
  /arcgis/rest/services/<name>/ImageServer            ArcGIS elevation service JSON
  /arcgis/rest/services/<name>/ImageServer/tile/{z}/{row}/{col}   LERC tile
  /demo/                                              test pages

Everything is read from a static root directory (default: ./public), so the same
files can be dropped on any static host later. This server adds:
  - the ImageServer metadata route (URL must end exactly in /ImageServer),
  - CORS for all origins (Next.js dev server on another port works),
  - a one-day Cache-Control on successfully served tiles (never on 404s or
    errors; tiles get rebuilt, so they are not `immutable`),
  - path containment: tile/vendor paths cannot escape their root directory.

Usage:  python serve.py [--root public] [--port 8765]
"""

import argparse
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse

# UI pages (index.html, viewer/, demo/, fetch/) live in this project's public/;
# the tile data they consume lives in the platform's data root.
ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLATFORM_DATA = os.environ.get(
    "GIS_DATA_ROOT",
    os.path.join(os.path.expanduser("~"), "Documents", "DEM", "gis-data-platform", "data"),
)
TILE_DIR = os.path.join(PLATFORM_DATA, "tiles")
PUBLIC_DIR = os.path.join(ENGINE_ROOT, "public")

# background fetch/build job state (one at a time)
JOB = {
    "state": "idle",  # idle | running | done | error
    "provider": None,
    "bbox": None,
    "log": [],
    "started": None,
    "finished": None,
}
JOB_LOCK = threading.Lock()

TILE_PREFIXES = ("/terrarium/", "/arcgis/", "/tilejson")
TILE_CACHE_CONTROL = "public, max-age=86400"


def _bad_segment(seg: str) -> bool:
    """A decoded URL path segment that could escape or alias a directory."""
    if seg in (".", ".."):
        return True
    if "\x00" in seg or ":" in seg or "\\" in seg or "/" in seg:
        return True
    if os.sep in seg or (os.altsep and os.altsep in seg):
        return True
    return False


def safe_join(base: str, *segments: str) -> str | None:
    """Join decoded path segments under base, or None if any segment is unsafe
    (``..``, separators, drive letters/ADS ``:``, NUL) or the resolved real path
    (symlinks followed) leaves base."""
    parts = [s for s in segments if s != ""]
    if any(_bad_segment(s) for s in parts):
        return None
    base_real = os.path.realpath(base)
    candidate = os.path.join(base_real, *parts)
    try:
        real = os.path.realpath(candidate)
        if os.path.commonpath([base_real, real]) != base_real:
            return None
    except ValueError:  # different drives on Windows, NUL, ...
        return None
    return candidate


def make_handler(root: str, tiles_root: str | None = None):
    project_root = os.path.dirname(os.path.abspath(root))
    tiles = os.path.abspath(tiles_root or root)

    class Handler(http.server.SimpleHTTPRequestHandler):
        extensions_map = {
            **http.server.SimpleHTTPRequestHandler.extensions_map,
            ".js": "text/javascript", ".mjs": "text/javascript",
            ".wasm": "application/wasm", ".json": "application/json",
            ".geojson": "application/geo+json",
        }

        _status = None
        _forbidden = False

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def send_response(self, code, message=None):
            self._status = code
            super().send_response(code, message)

        def _reject(self) -> str:
            """Mark the request forbidden; send_head answers 403."""
            self._forbidden = True
            return os.path.join(tiles, "__forbidden__")

        def translate_path(self, path):
            p = urllib.parse.urlparse(path).path
            if p.startswith("/vendor/"):  # node_modules (@arcgis/core, leaflet)
                rel = urllib.parse.unquote(p[len("/vendor/"):])
                safe = safe_join(os.path.join(project_root, "node_modules"), *rel.split("/"))
                return safe if safe is not None else self._reject()
            # Tile data lives in the platform's data root, UI pages in public/.
            if p.startswith(TILE_PREFIXES):
                rel = urllib.parse.unquote(p.lstrip("/"))
                safe = safe_join(tiles, *rel.split("/"))
                return safe if safe is not None else self._reject()
            return super().translate_path(p)

        def send_head(self):
            self._forbidden = False
            self.translate_path(self.path)  # sets self._forbidden
            if self._forbidden:
                self.send_error(403, "Forbidden")
                return None
            return super().send_head()

        def _send_file(self, path: str, ctype: str):
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                self.send_error(404, "File not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _send_json(self, obj, code=200):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _api_info(self):
            info = {
                "providers": [
                    {"id": "copernicus",
                     "label": "Copernicus DEM GLO-30 — 30 m, surface model (DSM)",
                     "license": "ESA free licence — attribution, commercial use OK",
                     "source": "AWS Open Data (s3://copernicus-dem-30m)"},
                    {"id": "fabdem",
                     "label": "FABDEM V1-2 — 30 m, bare-earth (buildings/canopy removed)",
                     "license": "CC BY-NC-SA 4.0 — NON-COMMERCIAL only",
                     "source": "University of Bristol (data.bris.ac.uk)"},
                ],
            }
            try:
                with open(os.path.join(tiles, "tilejson.json")) as f:
                    info["dataset"] = json.load(f)
            except OSError:
                info["dataset"] = None
            svc = None
            try:
                services = []
                sdir = os.path.join(tiles, "arcgis", "rest", "services")
                for name in sorted(os.listdir(sdir)):
                    p = os.path.join(sdir, name, "ImageServer.json")
                    if not os.path.exists(p):
                        continue
                    try:
                        with open(p) as f:
                            meta = json.load(f)
                    except (OSError, ValueError):
                        continue
                    services.append({
                        "name": name,
                        "url": f"/arcgis/rest/services/{name}/ImageServer",
                        "minValues": meta.get("minValues"),
                        "maxValues": meta.get("maxValues"),
                        "copyrightText": meta.get("copyrightText"),
                        "description": meta.get("serviceDescription"),
                        "fullExtent": meta.get("fullExtent"),
                    })
                info["services"] = services
                svc = services[0] if services else None
            except OSError:
                info["services"] = []
            info["service"] = svc
            with JOB_LOCK:
                info["job"] = {"state": JOB["state"], "provider": JOB["provider"],
                               "bbox": JOB["bbox"]}
            return info

        def _run_job(self, provider, bbox, minzoom, maxzoom, name):
            cmd = [sys.executable, os.path.join(project_root, "terrainhub.py"), "all",
                   "--bbox", *map(str, bbox), "--provider", provider,
                   "--minzoom", str(minzoom), "--maxzoom", str(maxzoom), "--name", name]
            try:
                proc = subprocess.Popen(
                    cmd, cwd=project_root, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace",
                )
                for line in proc.stdout:
                    with JOB_LOCK:
                        JOB["log"].append(line.rstrip())
                rc = proc.wait()
                state = "done" if rc == 0 else "error"
            except Exception as exc:  # noqa: BLE001
                with JOB_LOCK:
                    JOB["log"].append(f"FATAL {exc}")
                state = "error"
            with JOB_LOCK:
                JOB.update(state=state, finished=time.time())

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path != "/api/fetch":
                self.send_error(404, "Not found")
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                bbox = [float(v) for v in body["bbox"]]
                assert len(bbox) == 4
                provider = body.get("provider", "copernicus")
                assert provider in ("copernicus", "fabdem")
                minzoom = int(body.get("minzoom", 0))
                maxzoom = int(body.get("maxzoom", 13))
                name = ("".join(c if c.isalnum() or c in "-_" else "_"
                                for c in str(body.get("name") or "local30m"))
                        .strip("_")[:64] or "local30m")
            except Exception:
                self.send_error(400, "expected JSON: {provider, bbox[4], minzoom, maxzoom, name}")
                return
            with JOB_LOCK:
                if JOB["state"] == "running":
                    self.send_error(409, "a fetch job is already running")
                    return
                JOB.update(state="running", provider=provider, bbox=bbox,
                           log=[], started=time.time(), finished=None)
            threading.Thread(target=self._run_job,
                             args=(provider, bbox, minzoom, maxzoom, name),
                             daemon=True).start()
            self._send_json({"ok": True, "state": "running"})

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)

            # .css requested as an ES module (calcite's runtime imports its
            # stylesheet) -> shim with an empty JS module. Real stylesheets
            # arrive via <link> (Sec-Fetch-Dest: style) and stay untouched.
            if path.endswith(".css") and self.headers.get("Sec-Fetch-Dest") == "script":
                body = b'export default "";\n'
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/info":
                self._send_json(self._api_info())
                return
            if path == "/api/status":
                with JOB_LOCK:
                    snap = dict(JOB)
                    snap["log"] = JOB["log"][-60:]
                self._send_json(snap)
                return

            # ArcGIS REST service metadata: .../ImageServer (?f=json|pjson ignored)
            m = re.fullmatch(r"/arcgis/rest/services/([^/]+)/ImageServer/?", path)
            if m:
                fpath = safe_join(tiles, "arcgis", "rest", "services", m.group(1),
                                  "ImageServer.json")
                if fpath is None:
                    self.send_error(403, "Forbidden")
                    return
                self._send_file(fpath, "application/json; charset=utf-8")
                return

            # ArcGIS REST tile op: .../ImageServer/tile/{level}/{row}/{col}
            m = re.fullmatch(r"/arcgis/rest/services/([^/]+)/ImageServer/tile/(\d+)/(\d+)/(\d+)", path)
            if m:
                name, z, row, col = m.groups()
                fpath = safe_join(tiles, "arcgis", "rest", "services", name, "tile", z, row, col)
                if fpath is None:
                    self.send_error(403, "Forbidden")
                    return
                self._send_file(fpath, "application/octet-stream")
                return

            # everything else: plain static files (query strings ignored).
            # Keep the path percent-encoded: translate_path decodes it exactly
            # once (decoding here too would turn %252e%252e into "..").
            self.path = parsed.path
            super().do_GET()

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            # Cache only successful tile responses, and not forever: tiles get
            # rebuilt. Errors/404s must never be cached (data may appear later).
            if self._status == 200 and (
                    "/tile/" in self.path or "/terrarium/" in self.path):
                self.send_header("Cache-Control", TILE_CACHE_CONTROL)
            super().end_headers()

        def do_OPTIONS(self):
            self.send_response(204)
            self.end_headers()

        def log_message(self, fmt, *a):
            try:
                msg = (fmt % a) if a else str(fmt)
            except Exception:
                msg = f"{fmt} {a!r}"
            if "favicon" in msg:
                return
            path = getattr(self, "path", "") or ""
            # tiles outside the built pyramid are expected to 404 (client shows
            # no data there) — don't spam the console about them
            if "404" in msg and ("/tile/" in path or "/terrarium/" in path):
                return
            super().log_message("%s", msg)

    return Handler


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=PUBLIC_DIR, help="static UI pages (index.html, viewer/, demo/)")
    ap.add_argument("--tiles", default=TILE_DIR, help="tile data root (terrarium/, arcgis/, tilejson.json)")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        raise SystemExit(f"root not found: {root}")
    tiles = os.path.abspath(args.tiles)
    if not os.path.isdir(tiles):
        raise SystemExit(f"tiles root not found: {tiles}")
    handler = make_handler(root, tiles)
    with Server(("0.0.0.0", args.port), handler) as httpd:
        print(f"heightmap engine serving UI {root}")
        print(f"                    tiles {tiles}")
        print(f"  demo:     http://localhost:{args.port}/demo/")
        print(f"  terrarium http://localhost:{args.port}/terrarium/{{z}}/{{x}}/{{y}}.png")
        print(f"  ArcGIS:   http://localhost:{args.port}/arcgis/rest/services/<name>/ImageServer")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
