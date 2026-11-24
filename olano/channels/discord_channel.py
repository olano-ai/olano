"""
Discord channel adapter using discord.py async API.

Features:
- Guild messages and DMs
- App slash commands (/chat, /new, /clear, /status)
- File attachment handling
- Role-based access control
- Long message splitting via ``truncate_message`` (code-fence aware)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from olano.channels.base import (
    BaseChannel,
    ChannelMessage,
    SendResult,
    is_retryable_error,
    truncate_message,
)
from olano.vault.resolver import lookup_credential

logger = logging.getLogger(__name__)

# (agent_name, discord_channel_id) → thread_id
#
# Namespaced by agent so two agents that each own a Discord bot don't
# share threads when their bots happen to be in the same channel.
_CHANNEL_THREADS: dict[tuple[str, str], str] = {}


def _thread_for_channel(channel_id: str, agent_name: str = "") -> str:
    key = (agent_name, channel_id)
    if key not in _CHANNEL_THREADS:
        import uuid

        _CHANNEL_THREADS[key] = f"dc_{uuid.uuid4().hex[:8]}"
    return _CHANNEL_THREADS[key]


def _downloads_dir() -> Path:
    from olano.config.workspace import get_workspace

    d = get_workspace().root / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


from olano.channels._health import HealthWatchdogMixin


class DiscordChannel(HealthWatchdogMixin, BaseChannel):
    """
    Full Discord bot adapter.

    Config keys (agent.json5 / env):
        token (str): Bot token — DISCORD_BOT_TOKEN env fallback
        guild_ids (list[int]): optional guild ID allowlist (servers)
        allowed_channels (list[int]): optional channel ID allowlist (any guild).
            Empty = all channels in allowed guilds. DMs always pass this gate.
        allowed_roles (list[str]): role names required to interact with the bot
        command_prefix (str): message prefix for commands (default: "!")
        max_message_length (int): per-message char limit (default 1900; Discord caps at 2000)
    """

    SUPPORTS_PROGRESS = True

    name = "discord"
    # Discord's hard limit is 2000; leave 100 chars of margin for the (N/M) indicator.
    max_message_length: int = 1900

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._client: Any = None
        self._tree: Any = None
        override = config.get("max_message_length")
        if override:
            self.max_message_length = int(override)

    async def start(self) -> None:
        try:
            import discord
            from discord import app_commands
        except ImportError:
            raise ImportError("discord.py not installed. Run: uv add discord.py")

        # Injected per-agent by the runtime; this resolves the common value for
        # a channel constructed outside the boot path (see telegram_channel).
        token = self.config.get("token") or lookup_credential("DISCORD_BOT_TOKEN")
        if not token:
            raise ValueError("Discord token not configured (set DISCORD_BOT_TOKEN)")

        guild_ids: list[int] = [int(x) for x in self.config.get("guild_ids", []) or []]
        allowed_channels: list[int] = [
            int(x) for x in self.config.get("allowed_channels", []) or []
        ]
        allowed_roles: list[str] = self.config.get("allowed_roles", []) or []

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        self._client = discord.Client(intents=intents)
        self._tree = app_commands.CommandTree(self._client)

        def _is_allowed_member(member: Any) -> bool:
            if not allowed_roles:
                return True
            if not member or not hasattr(member, "roles"):
                return True
            member_roles = {r.name for r in member.roles}
            return bool(member_roles & set(allowed_roles))

        def _is_allowed_guild(guild_id: int | None) -> bool:
            if not guild_ids:
                return True
            return guild_id in guild_ids

        def _is_allowed_channel(channel_id: int | None, is_dm: bool) -> bool:
            if not allowed_channels:
                return True
            # DMs bypass the channel allowlist — restrict via allowed_roles
            # or guild_ids if you don't want them. The allowlist only filters
            # noisy guild channels, not direct conversations with the bot.
            if is_dm:
                return True
            return channel_id in allowed_channels

        # ── Slash commands ──

        @self._tree.command(name="chat", description="Chat with the AI agent")
        async def slash_chat(interaction: discord.Interaction, message: str) -> None:
            if not _is_allowed_guild(interaction.guild_id):
                await interaction.response.send_message(
                    "Not authorized in this server.", ephemeral=True
                )
                return
            if not _is_allowed_channel(interaction.channel_id, is_dm=interaction.guild_id is None):
                await interaction.response.send_message(
                    "This bot is not enabled in this channel.", ephemeral=True
                )
                return
            if not _is_allowed_member(interaction.user):
                await interaction.response.send_message(
                    "You don't have the required role.", ephemeral=True
                )
                return

            await interaction.response.defer(thinking=True)
            channel_id = str(interaction.channel_id)
            thread_id = _thread_for_channel(channel_id, self.agent_name)
            msg = ChannelMessage(
                text=message,
                user_id=str(interaction.user.id),
                channel="discord",
                thread_id=thread_id,
                raw=interaction,
            )
            try:
                response = await self.handle(msg)
                for chunk in truncate_message(response, self.max_message_length):
                    await interaction.followup.send(chunk)
            except Exception as e:
                from olano.core.actionable_errors import channel_text

                await interaction.followup.send(channel_text(e))

        async def _start_new_thread(interaction: discord.Interaction) -> None:
            if not _is_allowed_member(interaction.user):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            import uuid

            channel_id = str(interaction.channel_id)
            key = (self.agent_name, channel_id)
            _CHANNEL_THREADS[key] = f"dc_{uuid.uuid4().hex[:8]}"
            await interaction.response.send_message(
                f"New session started. Thread: `{_CHANNEL_THREADS[key]}`",
                ephemeral=True,
            )

        @self._tree.command(name="new", description="Start a new conversation thread")
        async def slash_new(interaction: discord.Interaction) -> None:
            await _start_new_thread(interaction)

        @self._tree.command(name="clear", description="Start a new conversation thread")
        async def slash_clear(interaction: discord.Interaction) -> None:
            await _start_new_thread(interaction)

        @self._tree.command(name="status", description="Show agent status")
        async def slash_status(interaction: discord.Interaction) -> None:
            from datetime import datetime, timezone

            channel_id = str(interaction.channel_id)
            thread_id = _thread_for_channel(channel_id, self.agent_name)
            await interaction.response.send_message(
                f"Thread: `{thread_id}`\nTime: {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
                ephemeral=True,
            )

        # ── Regular message handler ──

        @self._client.event
        async def on_ready() -> None:
            logger.info("Discord bot connected as %s", self._client.user)
            # Sync slash commands
            try:
                if guild_ids:
                    ok = 0
                    for gid in guild_ids:
                        guild = discord.Object(id=gid)
                        try:
                            self._tree.copy_global_to(guild=guild)
                            await self._tree.sync(guild=guild)
                            ok += 1
                            logger.info("Discord slash commands synced for guild %s", gid)
                        except Exception as e:
                            logger.warning(
                                "Failed to sync Discord slash commands for guild %s: %s",
                                gid,
                                e,
                            )
                    if ok == len(guild_ids):
                        logger.info(
                            "Discord slash commands synced for all %d guild(s)",
                            len(guild_ids),
                        )
                    elif ok:
                        logger.warning(
                            "Discord slash commands: synced %d/%d guild(s); "
                            "others failed (often 50001 Missing Access — wrong guild id, "
                            "bot not in server, or invite missing applications.commands)",
                            ok,
                            len(guild_ids),
                        )
                    else:
                        logger.warning(
                            "Discord slash commands failed for every guild_ids entry; "
                            "verify numeric guild IDs, bot membership, and OAuth scopes "
                            "(bot + applications.commands)"
                        )
                else:
                    await self._tree.sync()
                    logger.info("Discord slash commands synced (global)")
            except Exception as e:
                logger.warning("Failed to sync Discord slash commands: %s", e)

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            if message.author == self._client.user:
                return

            # Skip slash command invocations (handled above)
            if message.content.startswith("/"):
                return

            if not _is_allowed_guild(message.guild.id if message.guild else None):
                return

            if not _is_allowed_channel(message.channel.id, is_dm=message.guild is None):
                return

            if not _is_allowed_member(message.author):
                return

            text = (message.content or "").strip()

            # Without **Message Content Intent** (Developer Portal → Bot), Discord does not
            # send guild message bodies — content is empty, so @mentions look "ignored".
            if (
                message.guild is not None
                and not text
                and not message.attachments
                and self._client.user
                and self._client.user in message.mentions
            ):
                try:
                    await message.reply(
                        "I'm online, but **I can't read message text in this server** until "
                        "**Message Content Intent** is enabled.\n\n"
                        "1. Open https://discord.com/developers/applications → your bot → **Bot**.\n"
                        "2. Under **Privileged Gateway Intents**, turn on **Message Content Intent** → Save.\n"
                        "3. **Restart Olano** (`olano serve` / dashboard runtime).\n\n"
                        "**Workaround:** use `/chat your question` — slash commands work without that intent."
                    )
                except Exception as exc:
                    logger.warning("Discord Message Content Intent hint reply failed: %s", exc)
                return

            # Handle file attachments
            attachment_notes = []
            if message.attachments:
                for att in message.attachments:
                    try:
                        import httpx

                        resp = httpx.get(att.url, follow_redirects=True, timeout=30)
                        dl_path = _downloads_dir() / att.filename
                        dl_path.write_bytes(resp.content)
                        attachment_notes.append(f"[Attachment '{att.filename}' saved to {dl_path}]")
                    except Exception as e:
                        attachment_notes.append(
                            f"[Attachment '{att.filename}' could not be downloaded: {e}]"
                        )

            if attachment_notes:
                text = "\n".join(attachment_notes) + ("\n" + text if text else "")

            if not text:
                return

            channel_id = str(message.channel.id)
            thread_id = _thread_for_channel(channel_id, self.agent_name)
            msg = ChannelMessage(
                text=text,
                user_id=str(message.author.id),
                channel="discord",
                thread_id=thread_id,
                raw=message,
            )

            try:
                async with message.channel.typing():
                    response = await self.handle(msg)
                await self.send_with_retry(channel_id, response)
            except Exception as e:
                from olano.core.actionable_errors import channel_text

                logger.error("Discord message handler error: %s", e)
                await self.send_with_retry(channel_id, channel_text(e))

        asyncio.create_task(self._client.start(token))
        logger.info("Discord channel starting")

        # Watchdog: probe the gateway state; rebuild on persistent
        # failure so a wedged websocket / stale DNS heals without a
        # daemon restart. See olano/channels/_health.py.
        self._start_health_watchdog()

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Structured status for Mission Control: gateway, identity, config hints, channel visibility."""
        checks: list[dict[str, Any]] = []
        client = self._client
        if client is None:
            checks.append(
                {
                    "id": "client",
                    "ok": False,
                    "message": "Discord client not created — adapter failed during start()",
                }
            )
            return {"adapter": "discord", "live": False, "bot_user": None, "checks": checks}

        ready = False
        try:
            ready = bool(client.is_ready())
        except Exception:
            pass
        checks.append(
            {
                "id": "gateway",
                "ok": ready,
                "message": "Gateway connected and ready"
                if ready
                else "Not ready yet (still connecting) or disconnected",
            }
        )

        bot_user: str | None = None
        try:
            bot_user = str(client.user) if client.user else None
        except Exception:
            pass
        checks.append(
            {
                "id": "identity",
                "ok": bool(bot_user),
                "message": f"Logged in as {bot_user}" if bot_user else "No bot user on session",
            }
        )

        lat = getattr(client, "latency", float("nan"))
        lat_ok = lat == lat and lat < 15.0
        checks.append(
            {
                "id": "heartbeat",
                "ok": lat_ok,
                "message": f"Gateway latency ~{lat * 1000:.0f} ms"
                if lat == lat
                else "Latency not available yet",
            }
        )

        guild_ids = [int(x) for x in self.config.get("guild_ids", []) or []]
        allowed_channels = [int(x) for x in self.config.get("allowed_channels", []) or []]
        allowed_roles = self.config.get("allowed_roles", []) or []

        checks.append(
            {
                "id": "config_guilds",
                "ok": True,
                "message": (
                    f"guild_ids lists {len(guild_ids)} server(s) — bot only accepts messages there"
                    if guild_ids
                    else "guild_ids is empty — all servers the bot joined are allowed"
                ),
            }
        )
        checks.append(
            {
                "id": "config_channels",
                "ok": True,
                "message": (
                    f"allowed_channels lists {len(allowed_channels)} channel(s) — "
                    "messages elsewhere are ignored (copy ID from #channel → Copy ID)"
                    if allowed_channels
                    else "allowed_channels is empty — every channel in allowed guilds works",
                ),
            }
        )
        if allowed_roles:
            checks.append(
                {
                    "id": "config_roles",
                    "ok": True,
                    "message": f"allowed_roles={allowed_roles!r} — users need one of these role names",
                }
            )

        checks.append(
            {
                "id": "message_content_intent",
                "ok": None,
                "message": "If @mentions get no reply, enable Message Content Intent (Developer Portal → Bot) or use /chat",
            }
        )

        if ready and allowed_channels:
            for cid in allowed_channels:
                ch_obj = None
                try:
                    ch_obj = client.get_channel(cid)
                except Exception:
                    ch_obj = None
                name = getattr(ch_obj, "name", None) if ch_obj is not None else None
                if ch_obj is None:
                    checks.append(
                        {
                            "id": f"channel_visibility:{cid}",
                            "ok": False,
                            "message": (
                                f"Channel id {cid} is NOT visible to the bot — wrong ID, bot not in server, "
                                "or missing View Channel permission. Right-click the real #channel → Copy ID."
                            ),
                        }
                    )
                else:
                    checks.append(
                        {
                            "id": f"channel_visibility:{cid}",
                            "ok": True,
                            "message": f"Channel {cid} visible as #{name}"
                            if name
                            else f"Channel {cid} visible",
                        }
                    )

        if ready and guild_ids:
            for gid in guild_ids:
                g = None
                try:
                    g = client.get_guild(gid)
                except Exception:
                    g = None
                if g is None:
                    checks.append(
                        {
                            "id": f"guild_membership:{gid}",
                            "ok": False,
                            "message": (
                                f"Bot is not in guild {gid} or ID is wrong — re-run OAuth invite with this server"
                            ),
                        }
                    )
                else:
                    checks.append(
                        {
                            "id": f"guild_membership:{gid}",
                            "ok": True,
                            "message": f"In server “{g.name}” (id {gid})",
                        }
                    )

        live = ready and bot_user is not None
        return {
            "adapter": "discord",
            "live": live,
            "bot_user": bot_user,
            "checks": checks,
        }

    async def _health_check(self) -> None:
        # discord.py's Client exposes both a sync `is_ready()` and a
        # heartbeat `latency` (seconds, NaN before first heartbeat). A
        # closed or never-ready client is the clear "wedged" signal we
        # want to act on. Latency > 5s suggests a half-dead connection
        # (rare but does happen on flaky networks).
        if self._client is None or self._client.is_closed():
            raise RuntimeError("Discord client closed")
        if not self._client.is_ready():
            raise RuntimeError("Discord client not ready")
        lat = getattr(self._client, "latency", None)
        if lat is not None and lat == lat and lat > 5.0:  # `lat == lat` filters NaN
            raise RuntimeError(f"Discord gateway latency too high ({lat:.1f}s)")

    async def _health_rebuild(self) -> None:
        old = self._client
        self._client = None
        self._tree = None
        if old is not None:
            try:
                await old.close()
            except Exception as e:
                logger.debug("Discord rebuild close: %s", e)
        await self.start()

    async def stop(self) -> None:
        await self.cancel_background_tasks()
        self._watchdog_task = None
        if self._client:
            await self._client.close()

    async def _resolve_target(self, recipient: str) -> Any:
        """Resolve a recipient string to a channel or DM target. Returns None if not found."""
        if not self._client:
            return None
        target = self._client.get_channel(int(recipient))
        if target is not None:
            return target
        try:
            user = await self._client.fetch_user(int(recipient))
            return await user.create_dm()
        except Exception:
            return None

    async def send_message(self, recipient: str, text: str) -> SendResult:
        """Send a single chunk and return a SendResult with the Discord message ID."""
        target = await self._resolve_target(recipient)
        if target is None:
            return SendResult(
                success=False,
                error=f"Discord: no channel or user {recipient}",
                retryable=False,
            )
        try:
            sent = await target.send(text)
            return SendResult(success=True, message_id=str(getattr(sent, "id", "") or ""))
        except Exception as e:
            err = str(e)
            return SendResult(
                success=False,
                error=err,
                retryable=is_retryable_error(err),
            )

    async def send(self, recipient: str, text: str) -> None:
        """Proactively send a message to a channel or user by ID.

        Preserved for callers using the abstract ``send`` API. For retry/chunking,
        prefer ``send_with_retry``.
        """
        if not self._client:
            return
        target = await self._resolve_target(recipient)
        if target is None:
            logger.warning("Discord: could not find channel/user %s", recipient)
            return
        for chunk in truncate_message(text, self.max_message_length):
            await target.send(chunk)
