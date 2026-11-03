"""Settings page of the data-platform (/settings/) and its API.

GET  /api/settings          every setting: value, where it comes from
                            (saved / .env / default), when it applies
PUT  /api/settings          {"values": {key: value | null}, "reset": [key]}
                            (null / reset = back to .env / default)
GET  /api/settings/runtime  container limits + usage of the data-platform and
                            (through engine:8787) the engine, and the worker
                            count a build started now would use

Settings live in <data>/settings/settings.json on the shared volume; the
engine reads the same file (app/settings.py), so nothing needs a restart
unless a setting says so.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import handoff, settings
from .config import ROOT

router = APIRouter()
page_router = APIRouter()
SETTINGS_PAGE = ROOT / "app" / "static" / "settings" / "index.html"


@page_router.get("/settings", include_in_schema=False)
def settings_redirect() -> RedirectResponse:
    return RedirectResponse("/settings/", status_code=307)


@page_router.get("/settings/", include_in_schema=False)
@page_router.get("/settings/index.html", include_in_schema=False)
def settings_page() -> FileResponse:
    response = FileResponse(SETTINGS_PAGE, media_type="text/html")
    response.headers["Cache-Control"] = "no-cache"
    return response


class SettingsUpdate(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    reset: list[str] = Field(default_factory=list)


def _engine_runtime(timeout: float | None = None) -> tuple[dict[str, Any] | None, str]:
    try:
        status, payload = handoff.request("GET", "/api/runtime", timeout=timeout)
    except handoff.EngineUnavailable:
        return None, handoff.ENGINE_OFFLINE_MSG
    if status == 200 and isinstance(payload, dict):
        return payload, ""
    return None, f"engine answered HTTP {status} (older engine image? rebuild it)"


def _engine_view() -> dict[str, Any] | None:
    """How the engine resolves its settings (its own .env variables)."""
    payload, _ = _engine_runtime(timeout=3)
    view = payload.get("settings") if payload else None
    return view if isinstance(view, dict) else None


@router.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return settings.describe(_engine_view())


@router.put("/api/settings")
def put_settings(body: SettingsUpdate) -> dict[str, Any]:
    try:
        return settings.update(body.values, body.reset, engine_view_fn=_engine_view)
    except settings.SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/settings/runtime")
def runtime() -> dict[str, Any]:
    engine, message = _engine_runtime()
    return {"platform": settings.runtime(), "engine": engine, "engineMessage": message}
