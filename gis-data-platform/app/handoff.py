"""Data-platform -> engine HTTP client (stdlib only).

The data-platform only downloads. When a DEM download finishes it asks the
engine (``ENGINE_URL``, default http://engine:8787 on the compose network) to
build the area. The engine is optional at that moment: when it is not running
the area stays ``downloaded`` and the engine builds it by itself when it starts
(its periodic autobuild picks up every downloaded area).

The fetch page follows the engine's build through the platform
(``/api/engine/...`` proxy routes), so the browser never needs the engine's
internal address or CORS.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import config

ENGINE_OFFLINE_MSG = "engine not running - it builds this area when it starts"


class EngineUnavailable(Exception):
    """The engine did not answer (container stopped, starting, network)."""


def engine_url() -> str:
    return config.ENGINE_URL


def request(method: str, path: str, body: Any = None, timeout: float | None = None) -> tuple[int, Any]:
    """(HTTP status, parsed JSON body or None). Raises EngineUnavailable when
    the engine cannot be reached at all."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        engine_url() + path, data=data, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or config.engine_timeout_s()) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"null")
        except ValueError:
            payload = None
        if exc.code in (502, 503, 504) and not isinstance(payload, dict):
            raise EngineUnavailable(f"HTTP {exc.code}") from None
        return exc.code, payload
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise EngineUnavailable(str(reason)) from None
    try:
        return status, json.loads(raw or b"null")
    except ValueError:
        return status, None


def request_build(name: str, provider: str) -> dict[str, Any]:
    """Ask the engine to build the saved area `name`. Never raises:
    {state: started | queued | engine-offline | error, message, engineJobId?}."""
    try:
        status, payload = request("POST", "/api/jobs", {"name": name, "provider": provider, "origin": "handoff"})
    except EngineUnavailable as exc:
        return {"state": "engine-offline", "message": ENGINE_OFFLINE_MSG, "detail": str(exc)}
    if status in (200, 201, 202) and isinstance(payload, dict):
        job = payload.get("job") if isinstance(payload.get("job"), dict) else None
        if payload.get("state") == "started" and job:
            return {"state": "started", "engineJobId": job.get("id"),
                    "message": payload.get("message") or f"the engine is building '{name}'"}
        return {"state": "queued",
                "message": payload.get("message") or f"queued in the engine: it builds '{name}' next"}
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return {"state": "error", "message": f"the engine refused the build (HTTP {status}): {detail or 'no detail'}"}


def quote(name: str) -> str:
    return urllib.parse.quote(name, safe="")
