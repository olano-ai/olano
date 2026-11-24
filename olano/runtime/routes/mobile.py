"""What the Olano mobile app shows, as the deployment's admin set it.

``GET /api/mobile/config`` serves the resolved ``config.yaml → mobile_app``
block (:class:`olano.config.system_settings.MobileAppSettings`) to any
signed-in person: every phone needs it to draw its tabs, so it is not under
the admin-only ``/api/system`` prefix. Admins edit it through
``PUT /api/system/config`` (dashboard Config → Mobile app).

The app reads it at launch and on every return to the foreground and keeps
the last copy, so a change reaches phones the next time they are opened
without an app update. ``revision`` is a hash of the values, so the app can
tell "nothing changed" from a new layout without comparing fields.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI


def mobile_config_payload() -> dict[str, Any]:
    from olano.config.system_settings import load_mobile_app_settings

    settings = load_mobile_app_settings()
    return {"revision": settings.revision(), **settings.model_dump()}


def register_mobile_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    @app.get("/api/mobile/config")
    async def mobile_config() -> dict[str, Any]:
        """The mobile app's layout: tabs, Inbox sections and Settings rows to show."""
        return await asyncio.to_thread(mobile_config_payload)
