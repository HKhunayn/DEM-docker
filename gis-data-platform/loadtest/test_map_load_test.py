import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from map_load_test import LoadTestConfig, VirtualUser, discover_sources, percentile, tile_coords


class _Handler(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        path = urlsplit(self.path).path
        type(self).requests.append(path)
        if path == "/health":
            body = b'{"status":"ok"}'
        elif path == "/tiles/terrain.json":
            body = json.dumps({
                "tiles": ["/tiles/terrain/{z}/{x}/{y}.png?v=abc"],
                "maxzoom": 13,
                "center": [46.76, 24.74, 8],
            }).encode()
        elif path == "/api/imagery":
            body = b'{"sets": [{"name":"demo","tiles":["/tiles/imagery-sets/demo/{z}/{x}/{y}.jpg"]}]}'
        elif path.startswith("/tiles/"):
            body = b"tile"
        elif path.startswith("/api/"):
            body = b"{}"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class MapLoadTestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()

    def test_percentile_uses_nearest_rank_and_handles_empty(self):
        self.assertEqual(percentile([10, 20, 30, 40], 95), 40)
        self.assertEqual(percentile([10, 20, 30, 40], 99), 40)
        self.assertIsNone(percentile([], 95))

    def test_tile_coords_wraps_x_and_clamps_y(self):
        coords = tile_coords(0, 180, 85, viewport_width=512, viewport_height=512)
        self.assertTrue(coords)
        self.assertTrue(all(0 <= x < 1 and 0 <= y < 1 for _, x, y in coords))

    def test_load_test_config_accepts_lan_url(self):
        config = LoadTestConfig(base_url="http://192.168.1.50:8787", users=2)
        self.assertEqual(config.base_url, "http://192.168.1.50:8787")
        self.assertNotIn("127.0.0.1", config.base_url)
        self.assertNotIn("localhost", config.base_url)

    def test_discover_sources_reads_tilejson_and_imagery(self):
        config = LoadTestConfig(base_url=self.base_url, users=1, duration=1)
        sources = asyncio.run(discover_sources(config))
        self.assertEqual(sources.terrain_template, self.base_url + "/tiles/terrain/{z}/{x}/{y}.png?v=abc")
        self.assertEqual(sources.imagery_template, self.base_url + "/tiles/imagery-sets/demo/{z}/{x}/{y}.jpg")

    def test_virtual_user_runs_one_iteration(self):
        config = LoadTestConfig(
            base_url=self.base_url,
            users=1,
            duration=1,
            min_pause=0,
            max_pause=0,
            requests_per_view=1,
        )
        sources = asyncio.run(discover_sources(config))
        user = VirtualUser(0, config, sources)
        result = asyncio.run(user.run_once())
        asyncio.run(user._session.close())
        self.assertGreater(result.requests, 0)
        self.assertEqual(result.errors, 0)


if __name__ == "__main__":
    unittest.main()
