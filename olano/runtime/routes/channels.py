"""Chat channel (connector) config on an agent.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException

from olano.runtime import api as _api


def register_channels_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the channels routes to ``app``."""
    # ── Channel CRUD ──────────────────────────────────────────────────────────

    def _absorb_and_write_channels(name: str, data: dict) -> dict:
        """Write the config, reporting which credentials went into the vault.

        ``_write_agent_config`` absorbs them anyway; doing it here first is what
        lets the connector form tell the user *"stored in your vault as
        TELEGRAM_BOT_TOKEN"* — or warn that a locked vault left the token in
        plain text. The second absorb inside the write is a no-op.
        """
        from olano.config.channel_secrets import absorb_channel_secrets

        report = absorb_channel_secrets(name, data)
        _api._write_agent_config(name, data)
        return {
            "vaulted": [{"channel": c, "field": f, "key": k} for c, f, k in report.vaulted],
            "vault_locked": not report.vault_available,
            "plaintext_fields": [{"channel": c, "field": f} for c, f in report.left_in_config],
        }

    @app.post("/api/agents/{name}/channels")
    async def add_channel(name: str, body: _api.ChannelCreateRequest) -> dict:
        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        channels = data.setdefault("channels", []) or []

        # Reject duplicates by type
        def _ch_type(c: Any) -> str:
            return c if isinstance(c, str) else c.get("type", "")

        if any(_ch_type(c) == body.type for c in channels):
            raise HTTPException(status_code=409, detail=f"Channel '{body.type}' already configured")

        entry: dict = {"type": body.type}
        entry.update(body.config or {})
        channels.append(entry)
        data["channels"] = channels
        async with _api._config_write_lock():
            report = await asyncio.to_thread(_absorb_and_write_channels, name, data)
        return {"ok": True, **report}

    @app.put("/api/agents/{name}/channels/{ch_type}")
    async def update_channel(name: str, ch_type: str, body: _api.ChannelCreateRequest) -> dict:
        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        channels = data.get("channels", []) or []
        for i, c in enumerate(channels):
            ct = c if isinstance(c, str) else c.get("type")
            if ct == ch_type:
                entry: dict = {"type": body.type}
                entry.update(body.config or {})
                channels[i] = entry
                data["channels"] = channels
                async with _api._config_write_lock():
                    report = await asyncio.to_thread(_absorb_and_write_channels, name, data)
                return {"ok": True, **report}

        raise HTTPException(status_code=404, detail=f"Channel '{ch_type}' not found")

    @app.delete("/api/agents/{name}/channels/{ch_type}")
    async def delete_channel(name: str, ch_type: str) -> dict:
        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        channels = data.get("channels", []) or []

        def _ch_type(c: Any) -> str:
            return c if isinstance(c, str) else c.get("type", "")

        new_list = [c for c in channels if _ch_type(c) != ch_type]
        if len(new_list) == len(channels):
            raise HTTPException(status_code=404, detail=f"Channel '{ch_type}' not found")

        data["channels"] = new_list
        await _api._write_agent_config_async(name, data)
        return {"ok": True}
