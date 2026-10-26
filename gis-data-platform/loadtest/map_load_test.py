#!/usr/bin/env python3
"""Simulate many map viewers against a GIS tile/API server.

This is an HTTP load generator, not a browser test. Each virtual user repeatedly
moves to a new location, zooms in/out, fetches the same metadata a viewer uses,
and requests the visible XYZ terrain/imagery tiles. Run it from another machine:

  python map_load_test.py --base-url http://SERVER:8787 --users 50 --duration 300

Only point this at a server you own or are authorized to test.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

LOG = logging.getLogger("map-load-test")


@dataclass
class LoadTestConfig:
    base_url: str
    users: int = 10
    duration: int = 60
    viewport_width: int = 1280
    viewport_height: int = 720
    min_zoom: int = 2
    max_zoom: int = 13
    min_pause: float = 0.5
    max_pause: float = 2.0
    requests_per_view: int = 1
    timeout: float = 20.0
    concurrency: int = 0
    seed: int = 42
    imagery: bool = True
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Sources:
    terrain_template: str | None = None
    imagery_template: str | None = None
    max_zoom: int = 13
    center: tuple[float, float] = (46.76, 24.74)


@dataclass
class UserResult:
    user_id: int
    requests: int = 0
    errors: int = 0
    bytes_received: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    statuses: Counter = field(default_factory=Counter)
    error_samples: list[str] = field(default_factory=list)

    def record(self, status: int, latency_ms: float, size: int) -> None:
        self.requests += 1
        self.statuses[status] += 1
        self.latencies_ms.append(latency_ms)
        self.bytes_received += size
        if status >= 400:
            self.errors += 1



def tile_coords(z: int, lon: float, lat: float, viewport_width: int = 1280, viewport_height: int = 720) -> list[tuple[int, int, int]]:
    """Return visible XYZ tiles for a Web Mercator view centered at lon/lat."""
    n = 2 ** z
    lat = max(-85.05112878, min(85.05112878, lat))
    center_x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    center_y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    radius_x = viewport_width / 512.0 / 2.0 + 1.0
    radius_y = viewport_height / 512.0 / 2.0 + 1.0
    result = []
    for ty in range(max(0, math.floor(center_y - radius_y)), min(n - 1, math.ceil(center_y + radius_y)) + 1):
        for tx_raw in range(math.floor(center_x - radius_x), math.ceil(center_x + radius_x) + 1):
            result.append((z, tx_raw % n, ty))
    return list(dict.fromkeys(result))


def _expand(template: str, z: int, x: int, y: int) -> str:
    return (template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
            .replace("{level}", str(z)).replace("{col}", str(x)).replace("{row}", str(y)))


def _absolute(base: str, path: str) -> str:
    return urljoin(base.rstrip("/") + "/", path)


def percentile(values: list[float], percent: float) -> float | None:
    """Nearest-rank percentile, suitable for a readable load-test report."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((percent / 100.0) * len(ordered)))
    return ordered[rank - 1]


def format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} {unit}"
        amount /= 1024
    return f"{value} B"


async def _get_json(session: aiohttp.ClientSession, url: str) -> tuple[Any, int, float, int]:
    started = time.perf_counter()
    async with session.get(url) as response:
        payload = json.loads(await response.text())
        return payload, response.status, (time.perf_counter() - started) * 1000, len(json.dumps(payload))


async def discover_sources(config: LoadTestConfig) -> Sources:
    if aiohttp is None:
        raise RuntimeError("Missing dependency: install aiohttp (see loadtest/requirements.txt)")
    timeout = aiohttp.ClientTimeout(total=config.timeout)
    async with aiohttp.ClientSession(timeout=timeout, headers=config.headers) as session:
        terrain, terrain_status, _, _ = await _get_json(session, _absolute(config.base_url, "/tiles/terrain.json"))
        if terrain_status >= 400:
            raise RuntimeError(f"terrain TileJSON returned HTTP {terrain_status}")
        terrain_templates = terrain.get("tiles", []) if isinstance(terrain, dict) else []
        terrain_template = _absolute(config.base_url, terrain_templates[0]) if terrain_templates else None
        imagery_template = None
        if config.imagery:
            try:
                imagery, status, _, _ = await _get_json(session, _absolute(config.base_url, "/api/imagery"))
                sets = imagery.get("sets", []) if status < 400 and isinstance(imagery, dict) else []
                if sets and sets[0].get("tiles"):
                    imagery_template = _absolute(config.base_url, sets[0]["tiles"][0])
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                LOG.warning("Imagery discovery failed; continuing with terrain only")
        center = terrain.get("center", [46.76, 24.74]) if isinstance(terrain, dict) else [46.76, 24.74]
        return Sources(terrain_template, imagery_template, int(terrain.get("maxzoom", config.max_zoom)), (float(center[0]), float(center[1])))


class VirtualUser:
    def __init__(self, user_id: int, config: LoadTestConfig, sources: Sources):
        self.user_id = user_id
        self.config = config
        self.sources = sources
        self.random = random.Random(config.seed + user_id)
        self.result = UserResult(user_id)
        self.lon, self.lat = sources.center
        self._session = None
        # Clamp the zoom range to the server's advertised limits so the
        # test never requests tiles the server intentionally 404s.
        self._eff_max_zoom = min(config.max_zoom, sources.max_zoom)
        self._eff_min_zoom = min(config.min_zoom, self._eff_max_zoom)
        self.zoom = max(self._eff_min_zoom, min(self._eff_max_zoom, 2))

    async def _request(self, session: aiohttp.ClientSession, url: str) -> None:
        started = time.perf_counter()
        try:
            async with session.get(url) as response:
                body = await response.read()
                self.result.record(response.status, (time.perf_counter() - started) * 1000, len(body))
                if response.status >= 400 and len(self.result.error_samples) < 3:
                    self.result.error_samples.append(f"HTTP {response.status} {url}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self.result.errors += 1
            self.result.error_samples.append(f"{type(exc).__name__} {url}")

    def _move(self) -> None:
        self.lon = max(-179.0, min(179.0, self.lon + self.random.uniform(-8.0, 8.0)))
        self.lat = max(-80.0, min(80.0, self.lat + self.random.uniform(-5.0, 5.0)))
        self.zoom = max(self._eff_min_zoom, min(self._eff_max_zoom, self.zoom + self.random.choice([-2, -1, 1, 1, 2])))

    async def run_once(self) -> UserResult:
        if aiohttp is None:
            raise RuntimeError("Missing dependency: install aiohttp (see loadtest/requirements.txt)")
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout, headers=self.config.headers)
        session = self._session
        await self._request(session, _absolute(self.config.base_url, "/health"))
        await self._request(session, _absolute(self.config.base_url, "/tiles/terrain.json"))
        if self.config.imagery:
            await self._request(session, _absolute(self.config.base_url, "/api/imagery"))
        tile_list = tile_coords(self.zoom, self.lon, self.lat, self.config.viewport_width, self.config.viewport_height)
        tile_list = tile_list[: max(1, self.config.requests_per_view * 8)]
        urls = []
        for z, x, y in tile_list:
            if self.sources.terrain_template:
                urls.append(_expand(self.sources.terrain_template, z, x, y))
            if self.sources.imagery_template and self.config.imagery:
                urls.append(_expand(self.sources.imagery_template, z, x, y))
        await asyncio.gather(*(self._request(session, url) for url in urls))
        return self.result

    async def run(self) -> UserResult:
        try:
            deadline = time.monotonic() + self.config.duration
            while time.monotonic() < deadline:
                self._move()
                await self.run_once()
                await asyncio.sleep(self.random.uniform(self.config.min_pause, self.config.max_pause))
            return self.result
        finally:
            if self._session is not None:
                await self._session.close()
                self._session = None


async def run_test(config: LoadTestConfig, sources: Sources) -> list[UserResult]:
    semaphore = asyncio.Semaphore(config.concurrency or config.users)

    async def run_one(user_id: int) -> UserResult:
        async with semaphore:
            return await VirtualUser(user_id, config, sources).run()

    return await asyncio.gather(*(run_one(i) for i in range(config.users)))


def report(results: list[UserResult], elapsed: float) -> int:
    total = sum(r.requests for r in results)
    errors = sum(r.errors for r in results)
    bytes_received = sum(r.bytes_received for r in results)
    latencies = [v for r in results for v in r.latencies_ms]
    statuses = Counter()
    for result in results:
        statuses.update(result.statuses)
    success = total - errors
    error_rate = (errors / total * 100.0) if total else 0.0
    print("\n" + "=" * 64)
    print("MAP LOAD TEST REPORT")
    print("=" * 64)
    print(f"Users             : {len(results)}")
    print(f"Duration          : {elapsed:.1f} s")
    print(f"Requests          : {total:,}")
    print(f"Successful        : {success:,}")
    print(f"Errors            : {errors:,} ({error_rate:.2f}%)")
    print(f"Throughput        : {total / elapsed:.2f} requests/s" if elapsed else "Throughput        : n/a")
    print(f"Data received     : {format_bytes(bytes_received)}")
    print("-" * 64)
    if latencies:
        print("LATENCY")
        print(f"Min               : {min(latencies):.1f} ms")
        print(f"Average           : {sum(latencies) / len(latencies):.1f} ms")
        print(f"P50               : {percentile(latencies, 50):.1f} ms")
        print(f"P95               : {percentile(latencies, 95):.1f} ms")
        print(f"P99               : {percentile(latencies, 99):.1f} ms")
        print(f"Max               : {max(latencies):.1f} ms")
    else:
        print("LATENCY           : no completed HTTP requests")
    print("-" * 64)
    print("HTTP STATUS       : " + (", ".join(f"{code}={count:,}" for code, count in sorted(statuses.items())) or "none"))
    samples = [sample for r in results for sample in r.error_samples][:10]
    if samples:
        print("-" * 64)
        print("ERROR SAMPLES")
        print("\n".join(f"  {sample}" for sample in samples))
    print("=" * 64)
    return 1 if errors else 0


def parse_args() -> LoadTestConfig:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="GIS platform origin, e.g. http://192.168.1.50:8787")
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--duration", type=int, default=60, help="seconds per virtual user")
    parser.add_argument("--concurrency", type=int, default=0, help="max active users; default is all users")
    parser.add_argument("--min-zoom", type=int, default=2)
    parser.add_argument("--max-zoom", type=int, default=13)
    parser.add_argument("--min-pause", type=float, default=0.5)
    parser.add_argument("--max-pause", type=float, default=2.0)
    parser.add_argument("--requests-per-view", type=int, default=1, help="tile fan-out multiplier; 1 requests up to 8 terrain + 8 imagery tiles")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--no-imagery", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.users < 1 or args.duration < 1 or args.min_pause < 0 or args.max_pause < args.min_pause:
        parser.error("users/duration must be positive and max-pause must be >= min-pause")
    return LoadTestConfig(args.base_url, args.users, args.duration, min_zoom=args.min_zoom, max_zoom=args.max_zoom,
                          min_pause=args.min_pause, max_pause=args.max_pause, requests_per_view=args.requests_per_view,
                          timeout=args.timeout, concurrency=args.concurrency, seed=args.seed, imagery=not args.no_imagery)


async def main() -> int:
    config = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        sources = await discover_sources(config)
        print(f"target={config.base_url} terrain={'yes' if sources.terrain_template else 'no'} imagery={'yes' if sources.imagery_template else 'no'}")
        print(f"starting {config.users} virtual users for {config.duration}s")
        started = time.perf_counter()
        results = await run_test(config, sources)
        return report(results, time.perf_counter() - started)
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
        print(f"startup failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
