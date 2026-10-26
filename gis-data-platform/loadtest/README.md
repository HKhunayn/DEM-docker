# Map load test

`map_load_test.py` simulates concurrent map viewers over HTTP. Each virtual user:

1. discovers the platform terrain TileJSON and optional imagery set;
2. moves to a new random map position;
3. zooms in or out;
4. requests `/health`, metadata, and the visible XYZ terrain/imagery tiles;
5. repeats until its duration expires.

It does not open real browser windows. It measures server/API/tile capacity without the CPU and GPU cost of rendering N browsers.

## Run from a separate machine

Install Python 3.11+ and the dependency:

On Windows, double-click `RunLoadTest.bat`. It installs `aiohttp` automatically if needed.

Or run it from Command Prompt:

```bat
RunLoadTest.bat http://SERVER_IP:8787 100 300
```

The arguments are:

```text
RunLoadTest.bat SERVER_URL USERS DURATION_SECONDS [extra options]
```

Examples:

```bat
RunLoadTest.bat http://192.168.1.50:8787 10 60
RunLoadTest.bat http://192.168.1.50:8787 100 300 --min-pause 1 --max-pause 3
RunLoadTest.bat http://192.168.1.50:8787 100 300 --no-imagery
```

For manual installation or Linux/macOS:

```bash
python -m pip install -r requirements.txt
```

Run a small smoke test:

```bash
python map_load_test.py --base-url http://SERVER_IP:8787 --users 2 --duration 15
```

Run 100 concurrent users for five minutes:

```bash
python map_load_test.py \
  --base-url http://SERVER_IP:8787 \
  --users 100 \
  --duration 300 \
  --min-pause 1 \
  --max-pause 3
```

For terrain-only testing:

```bash
python map_load_test.py --base-url http://SERVER_IP:8787 --users 100 --duration 300 --no-imagery
```

`--users` is the number of virtual users. `--concurrency` can cap active users if the load generator machine is small. `--requests-per-view 2` increases tile fan-out per view and is useful for a more aggressive test.

The process exits with code 0 when all requests succeeded and code 1 when any HTTP/network errors occurred. The report includes request rate, status counts, bytes received, and latency percentiles including P50, P95, and P99.

## LAN setup

On the server machine, start the API bound to all network interfaces:

```bat
cd gis-data-platform
Run.bat 8787 0.0.0.0
```

Find the server's IPv4 address with `ipconfig`, for example `192.168.1.50`. From the other machine, first verify:

```bat
curl http://192.168.1.50:8787/health
curl http://192.168.1.50:8787/tiles/terrain.json
```

Then run the load test using that LAN address:

```bat
RunLoadTest.bat http://192.168.1.50:8787 100 300
```

Do not use `localhost` or `127.0.0.1` in the load-test URL: those addresses refer to the machine running the load test. If the health request fails remotely, allow TCP port 8787 through Windows Firewall on the server and confirm both machines are on the same reachable network.

Only run this against a server you own or are authorized to test. Start with 2–10 users, then increase in steps while watching CPU, memory, network, disk, worker count, and server logs on the target machine.
