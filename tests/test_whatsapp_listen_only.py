"""WhatsApp listen-only (monitoring) mode + group-listening config.

``listen_only`` turns the adapter into a one-way tap: inbound messages still
flow to the agent (memory, transcripts, and self-learning all see them), but
nothing is ever sent back — no replies, no media, no typing indicators. The
suppressed sends report *success* so the gateway records the agent's
would-have-been reply in the session transcript instead of treating the
suppression as a delivery failure and retrying.

Also pins the group-listening defaults the dashboard's "Group listening"
select relies on: without ``require_mention`` the agent handles every group
message (no @mention needed), and the select may store the value as a string.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from olano.platforms.config import PlatformConfig
from olano.platforms.whatsapp import WhatsAppAdapter


def _adapter(tmp_path, monkeypatch, extra: dict[str, Any] | None = None) -> WhatsAppAdapter:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    monkeypatch.delenv("WHATSAPP_LISTEN_ONLY", raising=False)
    monkeypatch.delenv("WHATSAPP_REQUIRE_MENTION", raising=False)
    return WhatsAppAdapter(PlatformConfig(enabled=True, extra=extra or {}), agent_name="watcher")


class _Resp:
    status = 200

    async def json(self) -> dict[str, Any]:
        return {"messageId": "abc"}


class _RespCM:
    async def __aenter__(self) -> _Resp:
        return _Resp()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _live_session() -> MagicMock:
    session = MagicMock()
    session.post = MagicMock(return_value=_RespCM())
    return session


def _group_message(body: str = "just chatting") -> dict[str, Any]:
    return {
        "isGroup": True,
        "chatId": "120363012345678901@g.us",
        "senderId": "12025550188@s.whatsapp.net",
        "body": body,
        "botIds": ["6511111111@s.whatsapp.net"],
        "mentionedIds": [],
    }


# ── Outbound suppression ─────────────────────────────────────────────────────


async def test_listen_only_suppresses_text_send(tmp_path, monkeypatch) -> None:
    adapter = _adapter(tmp_path, monkeypatch, {"listen_only": True})
    adapter._running = True
    adapter._http_session = _live_session()

    result = await adapter.send("120363012345678901@g.us", "hello group")

    assert result.success is True
    assert result.message_id is None
    adapter._http_session.post.assert_not_called()


async def test_listen_only_suppresses_edit(tmp_path, monkeypatch) -> None:
    adapter = _adapter(tmp_path, monkeypatch, {"listen_only": True})
    adapter._running = True
    adapter._http_session = _live_session()

    result = await adapter.edit_message("chat@g.us", "MSG1", "edited")

    assert result.success is True
    assert result.message_id == "MSG1"
    adapter._http_session.post.assert_not_called()


async def test_listen_only_suppresses_media(tmp_path, monkeypatch) -> None:
    """Returns before the file-exists check — nothing outbound is attempted."""
    adapter = _adapter(tmp_path, monkeypatch, {"listen_only": True})
    adapter._running = True
    adapter._http_session = _live_session()

    result = await adapter._send_media_to_bridge(
        "chat@g.us", str(tmp_path / "missing.png"), "image"
    )

    assert result.success is True
    adapter._http_session.post.assert_not_called()


async def test_listen_only_suppresses_typing(tmp_path, monkeypatch) -> None:
    """Monitoring mode must not reveal presence via 'composing…'."""
    adapter = _adapter(tmp_path, monkeypatch, {"listen_only": True})
    adapter._running = True
    adapter._http_session = _live_session()

    await adapter.send_typing("chat@g.us")

    adapter._http_session.post.assert_not_called()


async def test_normal_mode_still_sends_via_bridge(tmp_path, monkeypatch) -> None:
    adapter = _adapter(tmp_path, monkeypatch)
    adapter._running = True
    adapter._http_session = _live_session()

    result = await adapter.send("chat@g.us", "hello")

    assert result.success is True
    assert result.message_id == "abc"
    adapter._http_session.post.assert_called()


# ── Config resolution ────────────────────────────────────────────────────────


def test_listen_only_defaults_off(tmp_path, monkeypatch) -> None:
    adapter = _adapter(tmp_path, monkeypatch)
    assert adapter._whatsapp_listen_only() is False


@pytest.mark.parametrize("configured", [True, "true", "1", "yes", "on"])
def test_listen_only_truthy_config_forms(tmp_path, monkeypatch, configured) -> None:
    adapter = _adapter(tmp_path, monkeypatch, {"listen_only": configured})
    assert adapter._whatsapp_listen_only() is True


def test_listen_only_env_var_fallback(tmp_path, monkeypatch) -> None:
    adapter = _adapter(tmp_path, monkeypatch)
    monkeypatch.setenv("WHATSAPP_LISTEN_ONLY", "1")
    assert adapter._whatsapp_listen_only() is True


def test_listen_only_explicit_config_beats_env(tmp_path, monkeypatch) -> None:
    adapter = _adapter(tmp_path, monkeypatch, {"listen_only": False})
    monkeypatch.setenv("WHATSAPP_LISTEN_ONLY", "1")
    assert adapter._whatsapp_listen_only() is False


# ── Inbound gate is untouched ────────────────────────────────────────────────


def test_listen_only_does_not_gate_inbound(tmp_path, monkeypatch) -> None:
    adapter = _adapter(tmp_path, monkeypatch, {"listen_only": True})
    assert adapter._should_process_message(_group_message()) is True


def test_group_messages_processed_without_mention_by_default(tmp_path, monkeypatch) -> None:
    """The 'All messages' default: no @mention needed in groups."""
    adapter = _adapter(tmp_path, monkeypatch)
    assert adapter._should_process_message(_group_message()) is True


@pytest.mark.parametrize("configured", [True, "true"])
def test_require_mention_drops_unaddressed_group_messages(
    tmp_path, monkeypatch, configured
) -> None:
    """Bool from legacy configs AND string from the dashboard select both work."""
    adapter = _adapter(tmp_path, monkeypatch, {"require_mention": configured})
    assert adapter._should_process_message(_group_message()) is False


@pytest.mark.parametrize("configured", [False, "false"])
def test_require_mention_off_processes_everything(tmp_path, monkeypatch, configured) -> None:
    adapter = _adapter(tmp_path, monkeypatch, {"require_mention": configured})
    assert adapter._should_process_message(_group_message()) is True


def test_require_mention_still_lets_mentions_through(tmp_path, monkeypatch) -> None:
    adapter = _adapter(tmp_path, monkeypatch, {"require_mention": True})
    msg = _group_message()
    msg["mentionedIds"] = ["6511111111@s.whatsapp.net"]
    assert adapter._should_process_message(msg) is True


def test_require_mention_matches_bot_lid_form(tmp_path, monkeypatch) -> None:
    """Regression: in phone-number-privacy groups WhatsApp addresses every
    participant — including the bot — by LID, not phone JID. The bridge must
    stamp both forms into ``botIds`` so a LID-only mention still matches;
    without it, require_mention silently drops every group message."""
    adapter = _adapter(tmp_path, monkeypatch, {"require_mention": True})
    msg = _group_message()
    msg["botIds"] = ["6511111111@s.whatsapp.net", "57750801363057@lid"]
    msg["mentionedIds"] = ["57750801363057@lid"]
    assert adapter._should_process_message(msg) is True


# ── Diagnostics ──────────────────────────────────────────────────────────────


def test_diagnostic_snapshot_reports_listen_only(tmp_path, monkeypatch) -> None:
    adapter = _adapter(tmp_path, monkeypatch, {"listen_only": True})
    snapshot = adapter.diagnostic_snapshot()
    ids = [check["id"] for check in snapshot["checks"]]
    assert "listen_only" in ids


def test_diagnostic_snapshot_quiet_when_listen_only_off(tmp_path, monkeypatch) -> None:
    adapter = _adapter(tmp_path, monkeypatch)
    snapshot = adapter.diagnostic_snapshot()
    ids = [check["id"] for check in snapshot["checks"]]
    assert "listen_only" not in ids


def test_listen_only_disables_live_progress(tmp_path, monkeypatch):
    """Monitoring mode must not even attempt progress updates.

    send()/edit_message() already suppress in listen-only, but they report
    success-with-no-id, which the progress reporter would read as failed
    pushes and retire only after three no-op attempts (plus a misleading
    "progress disabled after N failures" log).
    """
    from olano.platforms.progress_reporter import resolve_progress_mode

    adapter = _adapter(tmp_path, monkeypatch, {"listen_only": True})
    assert adapter.PROGRESS_MODE == "off"
    assert resolve_progress_mode(adapter) == "off"


def test_normal_mode_keeps_live_progress_enabled(tmp_path, monkeypatch):
    from olano.platforms.progress_reporter import resolve_progress_mode

    adapter = _adapter(tmp_path, monkeypatch, {"listen_only": False})
    assert adapter.PROGRESS_MODE == "edit"
    assert resolve_progress_mode(adapter) == "edit"
