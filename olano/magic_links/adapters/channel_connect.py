"""Channel-connect — a customer wires their messengers to an agent, no call.

The operator mints one link choosing which platforms this customer's page
offers (``params.platforms``); the customer opens it on their phone, enters the
PIN, and sees one button per messenger:

- token platforms (Telegram/Discord/Slack) open a small credential form that is
  live-validated against the platform API ("✓ connected as @HerShopBot")
  before anything is saved;
- QR platforms (WhatsApp/WeChat) show a live QR fed by a background pairing
  session (:mod:`olano.platforms.channel_setup`), with phone-number pairing
  as the WhatsApp fallback for people with only one device.

Each success persists a channel entry into the agent's ``agent.json5`` through
the validated admin path (secrets are absorbed into the vault, never left in
the file) and is marked on the page. "Finish setup" completes the link;
``post_complete`` restarts the agent so the new channels boot.

Platform coverage comes entirely from the connector registry — adding Signal or
DingTalk there makes it mintable here with no change to this file or the page.
"""

from __future__ import annotations

import logging
from typing import Any

from olano.magic_links.flows import (
    APPLY_NONE,
    APPLY_RESTART_NEEDED,
    APPLY_RESTARTED,
    APPLY_SELF,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    BaseFlow,
    register,
)
from olano.magic_links.store import LinkRecord
from olano.platforms import channel_setup

logger = logging.getLogger(__name__)


def _pairing_key(link: LinkRecord, platform: str) -> str:
    return f"{link.id}:{platform}"


def _persist_channel_entry(agent: str, entry: dict[str, Any]) -> None:
    """Merge *entry* into the agent's ``channels`` (replace same-type entry,
    else append) through the validated admin path. Secrets in the entry are
    vaulted by ``_vault_channel_secrets`` inside ``update_agent``."""
    from olano.admin import agents as admin_agents
    from olano.config.loader import load_agent_config

    cfg = load_agent_config(agent)
    channels: list[dict[str, Any]] = []
    replaced = False
    for existing in cfg.channels or []:
        if hasattr(existing, "to_dict"):
            raw = dict(existing.to_dict())
        elif isinstance(existing, dict):
            raw = dict(existing)
        else:  # bare string form ({"type": "telegram"} shorthand)
            raw = {"type": str(existing)}
        if str(raw.get("type")) == str(entry.get("type")) and not replaced:
            merged = {**raw, **entry}
            channels.append(merged)
            replaced = True
        else:
            channels.append(raw)
    if not replaced:
        channels.append(entry)
    admin_agents.update_agent(agent, {"channels": channels})


class ChannelConnectFlow(BaseFlow):
    id = "channel-connect"
    label = "Connect channels"
    kind = "connect"
    # post_complete restarts the agent, so by the time anyone reads the notice
    # the new channels are already booting.
    applies = APPLY_SELF
    #: Setup sessions are longer than sign-ins — someone may be creating a
    #: BotFather bot mid-flow. Overridable per mint via ``ttl_minutes``.
    default_ttl_minutes = 60

    # ── mint ─────────────────────────────────────────────────────────────────

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        agent = str(params.get("agent") or "").strip()
        if not agent:
            raise ValueError("An agent name is required for a channel-connect link.")
        try:
            from olano.config.loader import load_agent_config

            load_agent_config(agent)
        except Exception as exc:
            raise ValueError(f"Agent '{agent}' was not found: {exc}") from exc

        raw_platforms = params.get("platforms") or []
        if isinstance(raw_platforms, str):
            raw_platforms = [p.strip() for p in raw_platforms.split(",") if p.strip()]
        platforms: list[str] = []
        for p in raw_platforms:
            key = str(p).strip().lower()
            if channel_setup.get_connector(key) is None:
                known = ", ".join(c.platform for c in channel_setup.all_connectors())
                raise ValueError(f"Unknown platform '{p}'. Available: {known}.")
            if key not in platforms:
                platforms.append(key)
        if not platforms:
            known = ", ".join(c.platform for c in channel_setup.all_connectors())
            raise ValueError(f"Pick at least one platform to offer ({known}).")

        out: dict[str, Any] = {
            "agent": agent,
            "platforms": platforms,
            "service": "channel-connect",
            "active": "",
            "completed": {},
        }
        # Operator-supplied per-platform entry overrides, merged into the
        # channel entry on success (e.g. {"telegram": {"allowed_users": [123]}}).
        # Mint surfaces are owner/admin-gated, so this is trusted config.
        entries = params.get("entries")
        if isinstance(entries, dict):
            out["entries"] = {
                str(k).strip().lower(): dict(v) for k, v in entries.items() if isinstance(v, dict)
            }
        return out

    # ── page payload ─────────────────────────────────────────────────────────

    def descriptor(self, link: LinkRecord) -> dict[str, Any]:
        base = super().descriptor(link)
        agent = str(link.params.get("agent") or "")
        base["service"] = {"id": "channel-connect", "name": agent}
        base["title"] = f"Connect {agent} to your apps"
        base["instructions"] = "Pick an app to connect. You can do one or all of them, then finish."
        base.update(self._connect_state(link))
        return base

    def state(self, link: LinkRecord) -> dict[str, Any]:
        payload = self._connect_state(link)
        payload["status"] = STATUS_IN_PROGRESS if link.session_id else link.status
        return payload

    def _connect_state(self, link: LinkRecord) -> dict[str, Any]:
        agent = str(link.params.get("agent") or "")
        completed: dict[str, str] = dict(link.params.get("completed") or {})
        active_key = str(link.params.get("active") or "")

        # A finished pairing session persists its channel entry exactly once,
        # keyed off the completed map (page polls are idempotent re-entries).
        if active_key:
            connector = channel_setup.get_connector(active_key)
            if connector is not None and connector.mode == "qr" and active_key not in completed:
                pairing = channel_setup.pairing_state(_pairing_key(link, active_key))
                if pairing is not None and pairing.status == "done":
                    detail = self._complete_platform(
                        link, connector, identity=pairing.identity, creds={}
                    )
                    completed[active_key] = detail
                    active_key = ""

        platforms: list[dict[str, Any]] = []
        for key in link.params.get("platforms") or []:
            connector = channel_setup.get_connector(str(key))
            if connector is None:
                continue
            row = connector.public()
            row["status"] = "done" if key in completed else "pending"
            row["detail"] = completed.get(key, "")
            platforms.append(row)

        active: dict[str, Any] | None = None
        connector = channel_setup.get_connector(active_key) if active_key else None
        if connector is not None:
            active = connector.public()
            active["instructions"] = connector.instructions
            if connector.mode == "token":
                active["fields"] = [f.public() for f in connector.fields]
                active["cta"] = f"Connect {connector.label}"
            else:
                pairing = channel_setup.pairing_state(_pairing_key(link, active_key))
                live = pairing.public() if pairing else {"status": "starting", "detail": "…"}
                active.update(live)
                active["pair_code_supported"] = hasattr(connector, "request_pair_code")

        return {
            "agent": agent,
            "platforms": platforms,
            "active": active,
            "any_completed": bool(completed),
        }

    # ── actions ──────────────────────────────────────────────────────────────

    def start(self, link: LinkRecord) -> dict[str, Any]:
        return {"session_id": f"connect:{link.id}"}

    async def submit(self, link: LinkRecord, fields: dict[str, str]) -> dict[str, Any]:
        action = str(fields.get("_action") or "").strip()
        if action == "open":
            return await self._open(link, str(fields.get("platform") or ""))
        if action == "back":
            self._set_active(link, "")
            return {"status": STATUS_IN_PROGRESS}
        if action == "save":
            return await self._save(link, fields)
        if action == "paircode":
            return await self._pair_code(link, fields)
        if action == "finish":
            return self._finish(link)
        raise ValueError("Unknown action.")

    async def _open(self, link: LinkRecord, platform: str) -> dict[str, Any]:
        connector = self._connector_for(link, platform)
        self._set_active(link, connector.platform)
        if connector.mode == "qr":
            entry_overrides = (link.params.get("entries") or {}).get(connector.platform) or {}
            await connector.start_pairing(
                _pairing_key(link, connector.platform),
                str(link.params.get("agent") or ""),
                dict(entry_overrides),
            )
        return {"status": STATUS_IN_PROGRESS}

    async def _save(self, link: LinkRecord, fields: dict[str, str]) -> dict[str, Any]:
        platform = str(fields.get("platform") or link.params.get("active") or "")
        connector = self._connector_for(link, platform)
        if connector.mode != "token":
            raise ValueError(f"{connector.label} pairs by QR — nothing to save here.")
        creds = {f.name: str(fields.get(f.name) or "").strip() for f in connector.fields}
        missing = [f.label for f in connector.fields if not creds.get(f.name)]
        if missing:
            raise ValueError(f"Missing: {', '.join(missing)}.")
        identity = await connector.validate(creds)
        detail = self._complete_platform(link, connector, identity=identity, creds=creds)
        self._set_active(link, "")

        def _apply(current: LinkRecord) -> None:
            completed = dict(current.params.get("completed") or {})
            completed[connector.platform] = detail
            current.params["completed"] = completed

        self._mutate(link, _apply)
        return {"status": STATUS_IN_PROGRESS}

    async def _pair_code(self, link: LinkRecord, fields: dict[str, str]) -> dict[str, Any]:
        platform = str(fields.get("platform") or link.params.get("active") or "")
        connector = self._connector_for(link, platform)
        request = getattr(connector, "request_pair_code", None)
        if not callable(request):
            raise ValueError(f"{connector.label} does not support phone-number pairing.")
        await request(
            _pairing_key(link, connector.platform),
            str(link.params.get("agent") or ""),
            str(fields.get("number") or ""),
        )
        return {"status": STATUS_IN_PROGRESS}

    def _finish(self, link: LinkRecord) -> dict[str, Any]:
        completed = dict(link.params.get("completed") or {})
        if not completed:
            raise ValueError("Nothing is connected yet — connect at least one app first.")
        agent = str(link.params.get("agent") or "")
        summary = "; ".join(f"{k}: {v}" for k, v in completed.items())
        # The connection, and nothing about the restart: whether one happened is
        # decided *after* this by post_complete, and asserting it here made the
        # notice claim a restart that may never have run. `watch` appends the
        # real consequence per surface instead.
        return {"status": STATUS_DONE, "result": f"{agent} is connected — {summary}."}

    def cancel(self, link: LinkRecord) -> None:
        for key in link.params.get("platforms") or []:
            channel_setup.clear_pairing(_pairing_key(link, str(key)))

    async def post_complete(self, link: LinkRecord) -> str | None:
        """Boot the new channels: restart the agent when the runtime is up.

        Returns what actually happened, because the two outcomes are not the
        same story to tell. A restart that worked means the channels are live
        and the notice should say so; one that could not run — no bound runtime
        (CLI/tests), or a boot error — leaves a "connected" agent that is still
        serving its old config, and used to do so in silence behind a
        ``logger.warning`` nobody reads. Saying ``APPLY_RESTART_NEEDED`` is what
        turns that into a button the user can press.
        """
        self.cancel(link)  # pairing sessions are finished with; reap spawns
        agent = str(link.params.get("agent") or "")
        if not agent:
            return APPLY_NONE
        try:
            from olano.tools.talk_to import get_bound_runtime

            runtime = get_bound_runtime()
            if runtime is None:
                return APPLY_RESTART_NEEDED
            from olano.runtime import agent_lifecycle

            await agent_lifecycle.restart_agent(runtime, agent)
            logger.info("channel-connect: restarted agent '%s' with new channels", agent)
            return APPLY_RESTARTED
        except Exception as exc:  # the link already succeeded; log, don't fail
            logger.warning("channel-connect: restart of '%s' failed: %s", agent, exc)
            return APPLY_RESTART_NEEDED

    # ── helpers ──────────────────────────────────────────────────────────────

    def _connector_for(self, link: LinkRecord, platform: str) -> channel_setup.ChannelConnector:
        key = str(platform or "").strip().lower()
        if key not in (link.params.get("platforms") or []):
            raise ValueError("That app isn't offered on this link.")
        connector = channel_setup.get_connector(key)
        if connector is None:
            raise ValueError("That app is no longer available.")
        return connector

    def _complete_platform(
        self,
        link: LinkRecord,
        connector: channel_setup.ChannelConnector,
        *,
        identity: str,
        creds: dict[str, str],
    ) -> str:
        agent = str(link.params.get("agent") or "")
        entry = connector.build_entry(creds, identity=identity)
        overrides = (link.params.get("entries") or {}).get(connector.platform)
        if isinstance(overrides, dict):
            merged = dict(overrides)
            # Deep-merge `extra` so an operator override (allowlists, policy)
            # doesn't clobber what the connector pinned there (weixin bot_id).
            if isinstance(merged.get("extra"), dict) and isinstance(entry.get("extra"), dict):
                merged["extra"] = {**entry["extra"], **merged["extra"]}
            entry.update(merged)
        try:
            _persist_channel_entry(agent, entry)
        except Exception as exc:
            logger.warning(
                "channel-connect: persist failed for %s/%s: %s", agent, connector.platform, exc
            )
            raise ValueError(
                f"Could not save the {connector.label} connection. Ask the operator "
                "to check the server."
            ) from exc
        detail = f"connected as {identity}" if identity else "connected"

        def _apply(current: LinkRecord) -> None:
            completed = dict(current.params.get("completed") or {})
            completed[connector.platform] = detail
            current.params["completed"] = completed
            if current.params.get("active") == connector.platform:
                current.params["active"] = ""

        self._mutate(link, _apply)
        return detail

    def _set_active(self, link: LinkRecord, platform: str) -> None:
        def _apply(current: LinkRecord) -> None:
            current.params["active"] = platform

        self._mutate(link, _apply)

    def _mutate(self, link: LinkRecord, apply: Any) -> None:
        """Persist a params change and mirror it onto the in-hand record."""
        from olano.magic_links import store

        updated = store.mutate(link.id, apply)
        if updated is not None:
            link.params.clear()
            link.params.update(updated.params)
        else:  # store-less link (unit tests) — apply in place
            apply(link)


register(ChannelConnectFlow())
