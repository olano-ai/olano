"""Slack tools for the SlackAgent subagent."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_SLACK_CREDS_MSG = (
    "[missing-credentials] No Slack token. Set SLACK_BOT_TOKEN (or SLACK_TOKEN) in the vault."
)


def _slack_client() -> tuple[Any | None, str | None]:
    """Return (client, error_string). One is always None."""
    sdk, err = optional_import("slack_sdk", purpose="Slack workspace access")
    if err:
        return None, err
    token = get_credential("SLACK_BOT_TOKEN") or get_credential("SLACK_TOKEN")
    if not token:
        return None, _SLACK_CREDS_MSG
    assert sdk is not None
    return sdk.WebClient(token=token), None


@tool
def slack_send_message(channel: str, text: str, thread_ts: str = "") -> str:
    """Send a message to a Slack channel or DM. Use thread_ts to reply in a thread."""
    client, err = _slack_client()
    if err:
        return err
    assert client is not None
    try:
        kwargs: dict = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        resp = client.chat_postMessage(**kwargs)
        return f"Message sent to {channel} (ts={resp.get('ts', '?')})"
    except Exception as exc:
        return f"[tool-error] slack_send_message: {exc}"


@tool
def slack_list_channels(limit: int = 20) -> str:
    """List public Slack channels."""
    client, err = _slack_client()
    if err:
        return err
    assert client is not None
    try:
        resp = client.conversations_list(limit=limit, types="public_channel")
        channels = resp.get("channels", [])
        lines = [
            f"#{c.get('name', '?')} (id={c.get('id', '?')}, members={c.get('num_members', '?')})"
            for c in channels
        ]
        return "\n".join(lines) or "No channels found."
    except Exception as exc:
        return f"[tool-error] slack_list_channels: {exc}"


@tool
def slack_get_messages(channel: str, limit: int = 10) -> str:
    """Get recent messages from a Slack channel."""
    client, err = _slack_client()
    if err:
        return err
    assert client is not None
    try:
        resp = client.conversations_history(channel=channel, limit=limit)
        messages = resp.get("messages", [])
        lines = []
        for m in reversed(messages):
            user = m.get("user", "bot")
            text = (m.get("text") or "")[:200]
            lines.append(f"[{user}] {text}")
        return "\n".join(lines) or "No messages."
    except Exception as exc:
        return f"[tool-error] slack_get_messages: {exc}"


@tool
def slack_search(query: str, count: int = 10) -> str:
    """Search Slack messages."""
    client, err = _slack_client()
    if err:
        return err
    assert client is not None
    try:
        resp = client.search_messages(query=query, count=count)
        matches = resp.get("messages", {}).get("matches", [])
        lines = [
            f"[#{m.get('channel', {}).get('name', '?')}] {(m.get('text') or '')[:150]}"
            for m in matches
        ]
        return "\n".join(lines) or "No results."
    except Exception as exc:
        return f"[tool-error] slack_search: {exc}"


@tool
def slack_set_status(status_text: str, status_emoji: str = ":speech_balloon:") -> str:
    """Set the bot's Slack status."""
    client, err = _slack_client()
    if err:
        return err
    assert client is not None
    try:
        client.users_profile_set(profile={"status_text": status_text, "status_emoji": status_emoji})
        return f"Status set to: {status_emoji} {status_text}"
    except Exception as exc:
        return f"[tool-error] slack_set_status: {exc}"


@tool
def slack_get_thread(channel: str, thread_ts: str, limit: int = 50) -> str:
    """Fetch all replies in a Slack thread."""
    client, err = _slack_client()
    if err:
        return err
    assert client is not None
    try:
        resp = client.conversations_replies(channel=channel, ts=thread_ts, limit=limit)
        messages = resp.get("messages", [])
        if not messages:
            return "No thread messages found."
        lines = []
        for m in messages:
            user = m.get("user", "bot")
            text = (m.get("text") or "")[:300]
            ts = m.get("ts", "?")
            lines.append(f"[{ts}] [{user}] {text}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] slack_get_thread: {exc}"


@tool
def slack_list_users(limit: int = 50) -> str:
    """List Slack workspace users (members)."""
    client, err = _slack_client()
    if err:
        return err
    assert client is not None
    try:
        resp = client.users_list(limit=limit)
        members = resp.get("members", [])
        lines = []
        for m in members:
            if m.get("deleted") or m.get("is_bot"):
                continue
            profile = m.get("profile", {})
            name = profile.get("real_name") or m.get("name", "?")
            email = profile.get("email", "")
            lines.append(f"- {name} (id={m.get('id', '?')}) {email}")
        return "\n".join(lines) or "No active human users found."
    except Exception as exc:
        return f"[tool-error] slack_list_users: {exc}"


@tool
def slack_add_reaction(channel: str, timestamp: str, emoji: str) -> str:
    """Add an emoji reaction to a Slack message. emoji=name without colons (e.g. 'thumbsup')."""
    client, err = _slack_client()
    if err:
        return err
    assert client is not None
    try:
        client.reactions_add(channel=channel, timestamp=timestamp, name=emoji.strip(":"))
        return f"Added :{emoji.strip(':')}: to {channel}/{timestamp}"
    except Exception as exc:
        return f"[tool-error] slack_add_reaction: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
