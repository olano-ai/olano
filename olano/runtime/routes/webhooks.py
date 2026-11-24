"""Inbound webhooks: config, test and ingress.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import JSONResponse

from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def register_webhooks_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the webhooks routes to ``app``."""
    # ── Webhooks (outbound + inbound) ───────────────────────────────────────
    #
    # CRUD over ``agent.json5``'s ``webhooks`` list plus a live Test button and
    # per-webhook history. The agent config is the source of truth, so webhooks
    # survive restarts; the runtime dispatcher is re-synced on every mutation so
    # edits take effect without a reboot.
    #
    # Outbound webhooks POST agent events to a URL. Inbound webhooks expose a
    # self-authenticating ingress URL (``/api/webhooks/ingress/…``) that an
    # external sender POSTs arbitrary payloads to; the agent processes each one
    # using the webhook's ``prompt`` with the full payload appended.

    def _ingress_path(name: str, wh: Any) -> str:
        return f"/api/webhooks/ingress/{name}/{wh.id}/{wh.ingress_token}"

    def _interpolate_webhook(entry: dict) -> dict:
        """Resolve ``${VAR}`` placeholders (matches the config loader)."""
        from olano.config.loader import _interpolate_env

        try:
            interpolated: dict = _interpolate_env(entry)
            return interpolated
        except Exception:
            return entry

    def _build_webhook(entry: dict) -> Any:
        from olano.config.agent_config import WebhookConfig

        return WebhookConfig.model_validate(_interpolate_webhook(entry))

    def _prune_webhook_entry(entry: dict) -> dict:
        """Drop fields irrelevant to the webhook's direction (tidy agent.json5)."""
        inbound_only = {"prompt", "respond", "accumulate_thread", "ingress_token", "max_body_bytes"}
        outbound_only = {
            "url",
            "events",
            "headers",
            "timeout_seconds",
            "max_retries",
            "verify_tls",
            "block_private_addresses",
        }
        drop = outbound_only if entry.get("direction") == "inbound" else inbound_only
        for key in drop:
            entry.pop(key, None)
        return entry

    def _resync_webhooks(name: str, data: dict) -> None:
        """Push the saved webhook set into the live dispatcher."""
        dispatcher = getattr(runtime, "webhook_dispatcher", None)
        if dispatcher is None:
            return
        try:
            hooks = [_build_webhook(w) for w in (data.get("webhooks") or [])]
            dispatcher.register_agent(name, hooks)
        except Exception as exc:  # never block a config save on resync
            _api.logger.warning("Webhook resync for '%s' failed: %s", name, exc)

    def _webhook_db() -> Any:
        from olano.webhooks.db import get_webhook_db

        return get_webhook_db()

    @app.get("/api/agents/{name}/webhooks")
    async def list_webhooks(name: str) -> dict:
        from olano.config.agent_config import WEBHOOK_EVENTS, WebhookConfig

        # The listing exposes each inbound webhook's self-authenticating
        # ingress URL (token included), so it must be scoped to the agent's
        # granted humans.
        auth_layer.assert_agent_access(name)
        try:
            data = _api._read_agent_config(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        db = _webhook_db()
        items: list[dict] = []
        for raw in data.get("webhooks") or []:
            try:
                wh = WebhookConfig.model_validate(raw)
            except Exception as exc:
                items.append({"name": raw.get("name", "?"), "invalid": str(exc), "raw": raw})
                continue
            view = wh.redacted()
            view["stats"] = db.stats(name, wh.id)
            if wh.direction == "inbound":
                view["ingress_path"] = _ingress_path(name, wh)
            items.append(view)
        return {"webhooks": items, "available_events": list(WEBHOOK_EVENTS)}

    @app.post("/api/agents/{name}/webhooks")
    async def add_webhook(name: str, body: _api.WebhookCreateRequest) -> dict:
        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        entry = body.model_dump()
        # Validate (also catches bad url/events) and mint a stable id.
        try:
            wh = _build_webhook(entry)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid webhook: {exc}")
        entry["id"] = wh.id
        if wh.direction == "inbound":
            entry["ingress_token"] = wh.ingress_token
        if not entry.get("secret"):
            entry.pop("secret", None)
        _prune_webhook_entry(entry)

        webhooks = data.setdefault("webhooks", []) or []
        if any((w.get("name") == body.name) for w in webhooks):
            raise HTTPException(status_code=409, detail=f"Webhook '{body.name}' already exists")
        webhooks.append(entry)
        data["webhooks"] = webhooks
        await _api._write_agent_config_async(name, data)
        _resync_webhooks(name, data)
        return {"ok": True, "id": wh.id}

    @app.put("/api/agents/{name}/webhooks/{webhook_id}")
    async def update_webhook(name: str, webhook_id: str, body: _api.WebhookCreateRequest) -> dict:
        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        webhooks = data.get("webhooks", []) or []
        for i, w in enumerate(webhooks):
            if w.get("id") == webhook_id:
                entry = body.model_dump()
                entry["id"] = webhook_id
                # Keep the ingress token stable across edits so the URL the
                # sender was given doesn't break.
                if w.get("ingress_token"):
                    entry["ingress_token"] = w["ingress_token"]
                # A null secret means "keep the existing one"; "" clears it.
                if body.secret is None:
                    if w.get("secret"):
                        entry["secret"] = w["secret"]
                    else:
                        entry.pop("secret", None)
                elif not body.secret:
                    entry.pop("secret", None)
                try:
                    _build_webhook(entry)
                except Exception as exc:
                    raise HTTPException(status_code=400, detail=f"Invalid webhook: {exc}")
                _prune_webhook_entry(entry)
                webhooks[i] = entry
                data["webhooks"] = webhooks
                await _api._write_agent_config_async(name, data)
                _resync_webhooks(name, data)
                return {"ok": True}

        raise HTTPException(status_code=404, detail=f"Webhook '{webhook_id}' not found")

    @app.delete("/api/agents/{name}/webhooks/{webhook_id}")
    async def delete_webhook(name: str, webhook_id: str) -> dict:
        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        webhooks = data.get("webhooks", []) or []
        new_list = [w for w in webhooks if w.get("id") != webhook_id]
        if len(new_list) == len(webhooks):
            raise HTTPException(status_code=404, detail=f"Webhook '{webhook_id}' not found")
        data["webhooks"] = new_list
        await _api._write_agent_config_async(name, data)
        _resync_webhooks(name, data)
        try:
            _webhook_db().purge_webhook(name, webhook_id)
        except Exception as exc:
            _api.logger.debug("Webhook log purge failed: %s", exc)
        return {"ok": True}

    @app.get("/api/agents/{name}/webhooks/{webhook_id}/logs")
    async def webhook_logs(name: str, webhook_id: str, limit: int = 50) -> dict:
        auth_layer.assert_agent_access(name)
        return {"logs": _webhook_db().history(name, webhook_id, limit=limit)}

    def _dispatcher() -> Any:
        from olano.webhooks.dispatcher import get_dispatcher

        return getattr(runtime, "webhook_dispatcher", None) or get_dispatcher()

    def _inbound_dispatch(name: str) -> Any:
        """Build an ``async (message, thread_id) -> reply`` bound to the agent."""

        async def dispatch(message: str, thread_id: str) -> str:
            runner = getattr(runtime, "_runners", {}).get(name)
            if runner is None:
                raise RuntimeError(f"agent '{name}' is not running")
            reply = await runner.ainvoke(message, thread_id=thread_id)
            return str(reply)

        return dispatch

    async def _run_webhook_test(name: str, wh: Any) -> dict:
        """Test a webhook: deliver out (outbound) or simulate ingest (inbound)."""
        if wh.direction == "inbound":
            sample = json.dumps(
                {"example": True, "message": "Test payload from Olano", "value": 42}
            ).encode()
            # Run synchronously so the dashboard can show the agent's reply, but
            # only actually invoke the agent when it's booted.
            dispatch = _inbound_dispatch(name) if name in getattr(runtime, "_runners", {}) else None
            ingested: dict = await _dispatcher().ingest(
                wh,
                name,
                raw_body=sample,
                content_type="application/json",
                headers={"X-Olano-Test": "true"},
                dispatch=dispatch,
                is_test=True,
            )
            return ingested
        tested: dict = await _dispatcher().test(wh, name)
        return tested

    @app.post("/api/agents/{name}/webhooks/{webhook_id}/test")
    async def test_saved_webhook(name: str, webhook_id: str) -> dict:
        try:
            data = _api._read_agent_config(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        match = next((w for w in (data.get("webhooks") or []) if w.get("id") == webhook_id), None)
        if match is None:
            raise HTTPException(status_code=404, detail=f"Webhook '{webhook_id}' not found")
        try:
            wh = _build_webhook(match)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid webhook: {exc}")

        return {"result": await _run_webhook_test(name, wh)}

    @app.post("/api/agents/{name}/webhooks/test")
    async def test_adhoc_webhook(name: str, body: _api.WebhookTestRequest) -> dict:
        """Test an unsaved webhook config (used by the Add/Edit dialog)."""
        try:
            wh = _build_webhook(body.model_dump())
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid webhook: {exc}")

        return {"result": await _run_webhook_test(name, wh)}

    @app.post("/api/webhooks/ingress/{name}/{webhook_id}/{token}")
    async def webhook_ingress(name: str, webhook_id: str, token: str, request: Request) -> Any:
        """Public ingress for inbound webhooks (self-authenticating via token).

        Reachable without a dashboard session — the middleware skips the auth
        gates for this path. Security: unguessable per-webhook token (constant-
        time compared), body-size cap, and per-webhook rate limiting.
        """
        import hmac as _hmac

        try:
            # Cached read: this is an unauthenticated external POST, so an
            # uncached parse would let any sender burn loop-adjacent CPU.
            data = await asyncio.to_thread(_api._read_agent_config_cached, name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found")

        match = next((w for w in (data.get("webhooks") or []) if w.get("id") == webhook_id), None)
        if match is None:
            raise HTTPException(status_code=404, detail="not found")
        try:
            wh = _build_webhook(match)
        except Exception:
            raise HTTPException(status_code=404, detail="not found")

        if wh.direction != "inbound" or not wh.enabled:
            raise HTTPException(status_code=404, detail="not found")
        # Constant-time token check — wrong/expired token looks like a 404.
        if not _hmac.compare_digest(str(token), str(wh.ingress_token)):
            raise HTTPException(status_code=404, detail="not found")

        raw_body = await request.body()
        if len(raw_body) > wh.max_body_bytes:
            raise HTTPException(status_code=413, detail="payload too large")

        headers = {k: v for k, v in request.headers.items()}
        content_type = request.headers.get("content-type", "")

        if wh.respond:
            # Run the agent now and return its reply (bounded so a slow agent
            # can't hold the sender's connection open forever).
            try:
                result = await asyncio.wait_for(
                    _dispatcher().ingest(
                        wh,
                        name,
                        raw_body=raw_body,
                        content_type=content_type,
                        headers=headers,
                        dispatch=_inbound_dispatch(name),
                    ),
                    timeout=max(5.0, wh.timeout_seconds * 6),
                )
            except TimeoutError:
                raise HTTPException(status_code=504, detail="agent timed out")
            if not result.get("ok"):
                raise HTTPException(status_code=502, detail=result.get("error") or "agent error")
            return {
                "ok": True,
                "response": result.get("response"),
                "thread_id": result.get("thread_id"),
            }

        # Fire-and-forget: ack immediately, process in the background.
        async def _bg() -> None:
            from olano.security.request_context import push_request_context

            with push_request_context(source="webhook", channel="webhook"):
                await _dispatcher().ingest(
                    wh,
                    name,
                    raw_body=raw_body,
                    content_type=content_type,
                    headers=headers,
                    dispatch=_inbound_dispatch(name),
                )

        asyncio.ensure_future(_bg())
        return JSONResponse(status_code=202, content={"ok": True, "queued": True})
