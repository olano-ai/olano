"""Push notification device registry for the Olano mobile app.

The app loads this box's dashboard, gets a token from Apple or Google, and
registers it here under the signed-in person. Everything is scoped to the
caller: a person lists, renames, mutes and deletes their own phones, and an
admin may delete anyone's (a lost phone, a departed member). Delivery itself is
:mod:`olano.push`; these routes only manage who receives it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from olano.runtime import auth as auth_layer


class DeviceRegisterRequest(BaseModel):
    token: str
    platform: str
    name: str | None = None
    app_version: str | None = None
    events: list[str] | None = None


class DevicePatchRequest(BaseModel):
    name: str | None = None
    events: list[str] | None = None


def _status_payload(user_id: str) -> dict[str, Any]:
    from olano.config.system_settings import load_push_settings
    from olano.deployment.sources import cloud_settings_config
    from olano.push import EVENT_KINDS
    from olano.push.dispatcher import get_push_dispatcher
    from olano.push.store import get_device_store

    settings = load_push_settings()
    dispatcher = get_push_dispatcher(start=False)
    stats = dispatcher.status() if dispatcher is not None else {}
    reason: str | None
    if dispatcher is not None:
        # Running: the only thing worth reporting is what the relay last said.
        reason = str(stats["last_error"]) if stats.get("last_error") else None
    elif not settings.enabled:
        reason = "disabled"
    elif cloud_settings_config() is None:
        reason = "not_managed"
    else:
        reason = "not_running"
    return {
        "enabled": settings.enabled,
        "available": dispatcher is not None,
        "reason": reason,
        "previews": settings.previews,
        "events": list(EVENT_KINDS),
        "last_sent_at": stats.get("last_sent_at"),
        "devices": [d.public() for d in get_device_store().for_user(user_id)],
    }


def register_push_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the push routes to ``app``."""

    def _me() -> Any:
        user = auth_layer.current_user()
        if user is None:
            raise HTTPException(status_code=401, detail="auth_required")
        return user

    def _owned(device_id: str, *, allow_admin: bool = False) -> Any:
        from olano.push.store import get_device_store

        user = _me()
        device = get_device_store().get(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="No such device")
        if device.user_id != user.id and not (allow_admin and user.is_admin):
            # 404, not 403: another person's device ids are not disclosed.
            raise HTTPException(status_code=404, detail="No such device")
        return device

    @app.get("/api/push/status")
    async def push_status() -> dict:
        """Whether this box can push, and the caller's registered phones.

        ``available`` is false on a box without the Olano Cloud relay (a
        self-hosted install), when ``push.enabled`` is off, or before the
        dispatcher has started; ``reason`` says which. A delivery problem the
        relay reported (``cloud_auth``, ``rate_limited``, ``network``, …) also
        lands in ``reason`` so the app can show it next to the switch.
        """
        user = _me()
        return await asyncio.to_thread(_status_payload, user.id)

    @app.post("/api/push/devices")
    async def push_register(body: DeviceRegisterRequest) -> dict:
        """Register (or refresh) the caller's phone. Called on every app launch.

        ``events`` (optional) is the subset of ``replies``, ``approvals``,
        ``cortex`` and ``alerts`` the phone wants; omitted keeps the current
        choice, or all of them for a new phone.
        """
        from olano.push.store import InvalidDevice, get_device_store

        user = _me()
        try:
            device = await asyncio.to_thread(
                get_device_store().register,
                user_id=user.id,
                token=body.token,
                platform=body.platform,
                name=body.name or "",
                app_version=body.app_version or "",
                events=body.events,
            )
        except InvalidDevice as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"device": device.public()}

    @app.patch("/api/push/devices/{device_id}")
    async def push_update(device_id: str, body: DevicePatchRequest) -> dict:
        from olano.push.store import InvalidDevice, get_device_store

        _owned(device_id)
        try:
            device = await asyncio.to_thread(
                get_device_store().update, device_id, name=body.name, events=body.events
            )
        except InvalidDevice as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="No such device") from exc
        return {"device": device.public()}

    @app.delete("/api/push/devices/{device_id}")
    async def push_delete(device_id: str) -> dict:
        """Stop pushing to a phone. The app calls it on sign-out; an admin may
        remove anyone's."""
        from olano.push.store import get_device_store

        _owned(device_id, allow_admin=True)
        removed = await asyncio.to_thread(get_device_store().delete, device_id)
        return {"ok": removed}

    @app.post("/api/push/test")
    async def push_test() -> dict:
        """Send "Notifications are working" to the caller's phones, now.

        Synchronous on purpose: the app shows the outcome (and the relay's
        reason when it failed) right under the button that asked.
        """
        from olano.push.dispatcher import get_push_dispatcher
        from olano.push.events import test_message

        user = _me()
        dispatcher = get_push_dispatcher(start=False)
        if dispatcher is None:
            status = await asyncio.to_thread(_status_payload, user.id)
            raise HTTPException(
                status_code=409,
                detail={"code": "push_unavailable", "reason": status["reason"]},
            )
        message = await asyncio.to_thread(test_message, user_id=user.id)
        if not message.devices:
            raise HTTPException(
                status_code=409,
                detail={"code": "no_devices", "reason": "no_devices"},
            )
        summary = await asyncio.to_thread(dispatcher.send, [message])
        return {
            "sent": summary["sent"],
            "failed": summary["failed"],
            "error": summary["error"],
            "pruned": len(summary["invalid_tokens"]),
        }
