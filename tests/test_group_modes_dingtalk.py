"""DingTalk wiring for the uniform group listen/respond modes.

Mirrors the Telegram reference (``tests/test_group_modes.py``): mode
resolution (legacy ``require_mention`` mapping, new knobs winning), the
``group_respond`` gate in ``_should_process_message``, and the
``group_listen`` recording hook (``_maybe_record_observed`` → persistent
observed-messages store).

``record_observed_event`` writes inline when no event loop is running, so the
sync tests here observe the store immediately after the call.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from olano.platforms.chat_directory import get_chat_directory
from olano.platforms.config import PlatformConfig
from olano.platforms.dingtalk import DingTalkAdapter

AGENT = "dt-modes"
GROUP_CHAT = "cidAbCdEfGh=="
SENDER_ID = "$:LWCP_v1:$abc123"


def _adapter(tmp_path, monkeypatch, extra: dict[str, Any] | None = None) -> DingTalkAdapter:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    monkeypatch.delenv("DINGTALK_REQUIRE_MENTION", raising=False)
    monkeypatch.delenv("DINGTALK_FREE_RESPONSE_CHATS", raising=False)
    monkeypatch.delenv("DINGTALK_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("DINGTALK_MENTION_PATTERNS", raising=False)
    return DingTalkAdapter(PlatformConfig(enabled=True, extra=extra or {}), agent_name=AGENT)


class FakeChatbotMessage:
    """Minimal stand-in for dingtalk_stream.ChatbotMessage.

    ``text`` uses the legacy dict shape ``{"content": ...}`` which
    ``DingTalkAdapter._extract_text`` handles explicitly.
    """

    def __init__(
        self,
        text: str = "just chatting",
        *,
        is_group: bool = True,
        mentioned: bool = False,
        conversation_id: str = GROUP_CHAT,
        sender_id: str = SENDER_ID,
    ) -> None:
        self.message_id = uuid.uuid4().hex
        self.text = {"content": text}
        self.conversation_type = "2" if is_group else "1"
        self.conversation_id = conversation_id
        self.conversation_title = "Community Group" if is_group else None
        self.sender_id = sender_id
        self.sender_nick = "Ada Lovelace"
        self.sender_staff_id = "staff-ada"
        self.is_in_at_list = mentioned


def _group_message(text: str = "just chatting", **overrides: Any) -> FakeChatbotMessage:
    return FakeChatbotMessage(text, is_group=True, **overrides)


def _dm_message(text: str = "hi") -> FakeChatbotMessage:
    return FakeChatbotMessage(text, is_group=False, conversation_id="")


def _gate(adapter: DingTalkAdapter, msg: FakeChatbotMessage) -> bool:
    """Call _should_process_message the way _on_message does."""
    text = adapter._extract_text(msg) or ""
    is_group = str(msg.conversation_type) == "2"
    chat_id = msg.conversation_id or msg.sender_id
    return adapter._should_process_message(msg, text, is_group, chat_id)


# ─── Mode resolution ─────────────────────────────────────────────────────────


class TestModeResolution:
    def test_default_modes(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch)
        assert a._dingtalk_group_respond() == "all"  # legacy default: reply to everything
        assert a._dingtalk_group_listen() == "off"

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [(True, "mentions"), ("true", "mentions"), (False, "all"), ("false", "all")],
    )
    def test_legacy_require_mention_mapping(self, tmp_path, monkeypatch, configured, expected):
        a = _adapter(tmp_path, monkeypatch, {"require_mention": configured})
        assert a._dingtalk_group_respond() == expected

    def test_legacy_env_require_mention_mapping(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch)
        monkeypatch.setenv("DINGTALK_REQUIRE_MENTION", "true")
        assert a._dingtalk_group_respond() == "mentions"

    def test_group_respond_wins_over_require_mention(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"require_mention": True, "group_respond": "all"})
        assert a._dingtalk_group_respond() == "all"
        a = _adapter(tmp_path, monkeypatch, {"require_mention": False, "group_respond": "none"})
        assert a._dingtalk_group_respond() == "none"

    def test_listen_knob_and_no_legacy_passive_listen(self, tmp_path, monkeypatch):
        assert (
            _adapter(tmp_path, monkeypatch, {"group_listen": "mentions"})._dingtalk_group_listen()
            == "mentions"
        )
        # DingTalk never had passive_listen — setting it changes nothing.
        assert (
            _adapter(tmp_path, monkeypatch, {"passive_listen": True})._dingtalk_group_listen()
            == "off"
        )


# ─── Respond gating ──────────────────────────────────────────────────────────


class TestRespondGating:
    def test_respond_none_drops_group_messages(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none"})
        assert _gate(a, _group_message()) is False
        # Even an explicit @mention stays silent under "none".
        assert _gate(a, _group_message(mentioned=True)) is False

    def test_respond_none_leaves_dms_alone(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none"})
        assert _gate(a, _dm_message()) is True

    def test_respond_none_free_response_chat_still_answers(self, tmp_path, monkeypatch):
        """Specific per-chat exemption beats the general 'none' mode."""
        a = _adapter(
            tmp_path,
            monkeypatch,
            {"group_respond": "none", "free_response_chats": [GROUP_CHAT]},
        )
        assert _gate(a, _group_message()) is True

    def test_respond_mentions_matches_legacy_require_mention(self, tmp_path, monkeypatch):
        legacy = _adapter(
            tmp_path, monkeypatch, {"require_mention": True, "mention_patterns": ["^小马"]}
        )
        new = _adapter(
            tmp_path, monkeypatch, {"group_respond": "mentions", "mention_patterns": ["^小马"]}
        )
        cases = [
            _group_message(),
            _group_message(mentioned=True),
            _group_message("小马 status?"),
            _dm_message(),
        ]
        for msg in cases:
            assert _gate(legacy, msg) == _gate(new, msg)
        assert _gate(new, _group_message()) is False
        assert _gate(new, _group_message(mentioned=True)) is True
        assert _gate(new, _group_message("小马 status?")) is True

    def test_respond_all_matches_legacy_default(self, tmp_path, monkeypatch):
        legacy = _adapter(tmp_path, monkeypatch, {"require_mention": False})
        new = _adapter(tmp_path, monkeypatch, {"group_respond": "all"})
        for msg in (_group_message(), _group_message(mentioned=True), _dm_message()):
            assert _gate(legacy, msg) is True
            assert _gate(new, msg) is True

    async def test_on_message_drops_and_records(self, tmp_path, monkeypatch):
        """The inbound path routes a gated-out group message through the recorder."""
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none", "group_listen": "all"})
        a.handle_message = AsyncMock()  # type: ignore[method-assign]
        a._maybe_record_observed = MagicMock()  # type: ignore[method-assign]
        await a._on_message(_group_message("ignored chatter"))
        a.handle_message.assert_not_awaited()
        a._maybe_record_observed.assert_called_once()

    async def test_on_message_allowlist_drop_not_recorded(self, tmp_path, monkeypatch):
        """A sender the access lists rejected is never observed either."""
        a = _adapter(
            tmp_path,
            monkeypatch,
            {"group_respond": "none", "group_listen": "all", "allowed_users": ["someone-else"]},
        )
        a.handle_message = AsyncMock()  # type: ignore[method-assign]
        a._maybe_record_observed = MagicMock()  # type: ignore[method-assign]
        await a._on_message(_group_message("from a stranger"))
        a.handle_message.assert_not_awaited()
        a._maybe_record_observed.assert_not_called()

    async def test_on_message_free_chat_still_processed(self, tmp_path, monkeypatch):
        """The per-chat exemption reaches handle_message even under 'none'."""
        a = _adapter(
            tmp_path,
            monkeypatch,
            {"group_respond": "none", "free_response_chats": [GROUP_CHAT]},
        )
        a.handle_message = AsyncMock()  # type: ignore[method-assign]
        await a._on_message(_group_message("hello there"))
        a.handle_message.assert_awaited_once()


# ─── Observed recording ──────────────────────────────────────────────────────


class TestObservedRecording:
    def test_listen_all_records_dropped_group_message(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none", "group_listen": "all"})
        msg = _group_message("pizza night friday")
        assert _gate(a, msg) is False
        # Sync context → record_observed_event writes inline.
        a._maybe_record_observed(msg, "pizza night friday")
        rows = get_chat_directory().recent_observed(AGENT, chat_id=GROUP_CHAT)
        assert [r["text"] for r in rows] == ["pizza night friday"]
        assert rows[0]["sender_name"] == "Ada Lovelace"
        assert rows[0]["platform"] == "dingtalk"

    def test_listen_off_records_nothing(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none"})
        a._maybe_record_observed(_group_message("vanishes"), "vanishes")
        assert get_chat_directory().recent_observed(AGENT, chat_id=GROUP_CHAT) == []

    def test_listen_mentions_records_only_addressed(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none", "group_listen": "mentions"})
        a._maybe_record_observed(_group_message("unaddressed"), "unaddressed")
        a._maybe_record_observed(_group_message("hey bot", mentioned=True), "hey bot")
        rows = get_chat_directory().recent_observed(AGENT, chat_id=GROUP_CHAT)
        assert [r["text"] for r in rows] == ["hey bot"]

    def test_listen_mentions_wake_word_counts_as_addressed(self, tmp_path, monkeypatch):
        a = _adapter(
            tmp_path,
            monkeypatch,
            {"group_respond": "none", "group_listen": "mentions", "mention_patterns": ["^小马"]},
        )
        a._maybe_record_observed(_group_message("小马 report"), "小马 report")
        rows = get_chat_directory().recent_observed(AGENT, chat_id=GROUP_CHAT)
        assert [r["text"] for r in rows] == ["小马 report"]

    def test_dms_never_recorded(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_listen": "all"})
        a._maybe_record_observed(_dm_message("private"), "private")
        assert get_chat_directory().recent_observed(AGENT) == []

    def test_empty_text_skipped(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none", "group_listen": "all"})
        a._maybe_record_observed(_group_message(""), "")
        assert get_chat_directory().recent_observed(AGENT, chat_id=GROUP_CHAT) == []

    def test_recording_failure_never_raises(self, tmp_path, monkeypatch):
        a = _adapter(tmp_path, monkeypatch, {"group_respond": "none", "group_listen": "all"})
        monkeypatch.setattr(
            "olano.platforms.dingtalk.DingTalkAdapter.build_source",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        a._maybe_record_observed(_group_message("still fine"), "still fine")  # must not raise
