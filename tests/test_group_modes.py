"""Group listen/respond modes + the persistent observed-messages store."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from olano.platforms.chat_directory import (
    get_chat_directory,
    record_observed_event,
)
from olano.platforms.config import Platform
from olano.platforms.group_modes import (
    resolve_group_listen,
    resolve_group_respond,
    should_record,
    should_respond,
)
from olano.platforms.session import SessionSource

# ─── Mode resolution ─────────────────────────────────────────────────────────


class TestResolveModes:
    @pytest.mark.parametrize(
        ("extra", "legacy", "expected"),
        [
            ({}, False, "all"),  # legacy default: reply to everything
            ({}, True, "mentions"),  # legacy require_mention on
            ({"group_respond": "none"}, False, "none"),
            ({"group_respond": "never"}, False, "none"),  # synonym
            ({"group_respond": "mentions"}, False, "mentions"),
            ({"group_respond": "all"}, True, "all"),  # knob beats legacy
            ({"group_respond": "bogus"}, True, "mentions"),  # invalid → legacy
            ({"group_respond": ""}, True, "mentions"),
        ],
    )
    def test_respond_resolution(self, extra, legacy, expected):
        assert resolve_group_respond(extra, legacy_require_mention=legacy) == expected

    @pytest.mark.parametrize(
        ("extra", "legacy", "expected"),
        [
            ({}, False, "off"),
            ({}, True, "all"),  # legacy passive_listen maps to all
            ({"group_listen": "all"}, False, "all"),
            ({"group_listen": "mentions"}, False, "mentions"),
            ({"group_listen": "off"}, True, "off"),  # knob beats legacy
            ({"group_listen": "none"}, True, "off"),  # synonym
            ({"group_listen": "junk"}, False, "off"),
        ],
    )
    def test_listen_resolution(self, extra, legacy, expected):
        assert resolve_group_listen(extra, legacy_passive_listen=legacy) == expected

    def test_should_respond_matrix(self):
        assert should_respond("all", mentioned=False) is True
        assert should_respond("mentions", mentioned=True) is True
        assert should_respond("mentions", mentioned=False) is False
        assert should_respond("none", mentioned=True) is False

    def test_should_record_matrix(self):
        assert should_record("off", mentioned=True) is False
        assert should_record("mentions", mentioned=False) is False
        assert should_record("mentions", mentioned=True) is True
        assert should_record("all", mentioned=False) is True


# ─── Observed-messages store ─────────────────────────────────────────────────


def _obs_source(chat_id: str = "-100123", user: str = "42") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_name="Community Group",
        chat_type="group",
        user_id=user,
        user_name="Ada Lovelace",
        username="ada",
    )


class TestObservedStore:
    def test_record_and_recent_roundtrip(self):
        d = get_chat_directory()
        d.record_observed("agent-a", "telegram", chat_id="-100123", text="first msg")
        d.record_observed(
            "agent-a", "telegram", chat_id="-100123", text="second msg", sender_name="Ada"
        )
        rows = d.recent_observed("agent-a", chat_id="-100123")
        assert [r["text"] for r in rows] == ["first msg", "second msg"]  # oldest-first
        assert rows[1]["sender_name"] == "Ada"

    def test_agent_scoping(self):
        d = get_chat_directory()
        d.record_observed("agent-a", "telegram", chat_id="-1", text="private stuff")
        assert d.recent_observed("agent-b", chat_id="-1") == []

    def test_per_chat_cap_prunes_oldest(self):
        d = get_chat_directory()
        for i in range(6):
            d.record_observed("agent-a", "telegram", chat_id="-1", text=f"msg {i}", max_per_chat=3)
        rows = d.recent_observed("agent-a", chat_id="-1")
        assert [r["text"] for r in rows] == ["msg 3", "msg 4", "msg 5"]

    def test_thread_filter(self):
        d = get_chat_directory()
        d.record_observed("agent-a", "telegram", chat_id="-1", thread_id="3", text="in topic")
        d.record_observed("agent-a", "telegram", chat_id="-1", text="in general")
        rows = d.recent_observed("agent-a", chat_id="-1", thread_id="3")
        assert [r["text"] for r in rows] == ["in topic"]

    def test_search_matches_text_and_sender(self):
        d = get_chat_directory()
        d.record_observed(
            "agent-a", "telegram", chat_id="-1", text="the event is friday", sender_name="Ada"
        )
        d.record_observed("agent-a", "telegram", chat_id="-1", text="unrelated", sender_name="Bob")
        assert len(d.search_observed("agent-a", "event")) == 1
        assert len(d.search_observed("agent-a", "ada")) == 1
        assert d.search_observed("agent-a", "nothing") == []

    def test_search_escapes_like_wildcards(self):
        d = get_chat_directory()
        d.record_observed("agent-a", "telegram", chat_id="-1", text="100% sure")
        d.record_observed("agent-a", "telegram", chat_id="-1", text="other")
        assert len(d.search_observed("agent-a", "100%")) == 1

    def test_long_text_truncated(self):
        d = get_chat_directory()
        d.record_observed("agent-a", "telegram", chat_id="-1", text="x" * 5000)
        row = d.recent_observed("agent-a", chat_id="-1")[0]
        assert len(row["text"]) <= 1201

    def test_purge(self):
        d = get_chat_directory()
        d.record_observed("agent-a", "telegram", chat_id="-1", text="a")
        d.record_observed("agent-a", "telegram", chat_id="-2", text="b")
        assert d.purge_observed("agent-a", chat_id="-1") == 1
        assert d.recent_observed("agent-a", chat_id="-2")


class TestObservedRecorder:
    def test_record_observed_event_writes_store_and_directory(self):
        adapter = SimpleNamespace(agent_name="agent-a", config=SimpleNamespace(extra={}))
        record_observed_event(adapter, _obs_source(), "hello from the group")
        d = get_chat_directory()
        rows = d.recent_observed("agent-a", chat_id="-100123")
        assert rows and rows[0]["text"] == "hello from the group"
        # Directory metadata learned the chat + sender too.
        assert d.get_chat("agent-a", "telegram", "-100123") is not None
        assert d.find_users("agent-a", "ada")

    def test_retention_knobs_read_from_connector_extra(self):
        adapter = SimpleNamespace(
            agent_name="agent-a",
            config=SimpleNamespace(extra={"group_listen_max_messages": 2}),
        )
        for i in range(4):
            record_observed_event(adapter, _obs_source(user=str(i)), f"msg {i}")
        rows = get_chat_directory().recent_observed("agent-a", chat_id="-100123")
        assert [r["text"] for r in rows] == ["msg 2", "msg 3"]

    def test_recorder_without_agent_is_noop(self):
        adapter = SimpleNamespace(agent_name="", config=SimpleNamespace(extra={}))
        record_observed_event(adapter, _obs_source(), "ignored")
        assert get_chat_directory().recent_observed("", chat_id="-100123") == []


# ─── Telegram reference wiring ───────────────────────────────────────────────


class TestTelegramGroupModes:
    @pytest.fixture()
    def adapter(self):
        pytest.importorskip("telegram")
        from olano.platforms.config import PlatformConfig
        from olano.platforms.telegram import TelegramAdapter

        def make(extra: dict):
            config = PlatformConfig(enabled=True, token="x", extra=extra)
            return TelegramAdapter(config, agent_name="test-telegram")

        return make

    def test_default_modes(self, adapter):
        a = adapter({})
        assert a._telegram_group_respond() == "all"
        assert a._telegram_group_listen() == "off"

    def test_legacy_mapping(self, adapter):
        a = adapter({"require_mention": True, "passive_listen": True})
        assert a._telegram_group_respond() == "mentions"
        assert a._telegram_group_listen() == "all"

    def test_new_knobs_win(self, adapter):
        a = adapter({"require_mention": True, "group_respond": "none", "group_listen": "mentions"})
        assert a._telegram_group_respond() == "none"
        assert a._telegram_group_listen() == "mentions"

    def test_buffer_only_active_in_listen_all(self, adapter):
        assert adapter({"group_listen": "all"})._observed_buffer() is not None
        assert adapter({"group_listen": "mentions"})._observed_buffer() is None
        assert adapter({"passive_listen": True})._observed_buffer() is not None
        assert adapter({})._observed_buffer() is None


# ─── History tools ───────────────────────────────────────────────────────────


class TestHistoryTools:
    @pytest.fixture(autouse=True)
    def _bind_agent(self):
        from olano.tools.talk_to import current_agent

        token = current_agent.set("agent-a")
        yield
        current_agent.reset(token)

    def test_recent_by_chat_name_wraps_untrusted(self):
        d = get_chat_directory()
        d.record_membership("agent-a", "telegram", "-100123", chat_name="Community Group")
        d.record_observed(
            "agent-a",
            "telegram",
            chat_id="-100123",
            text="event friday!",
            sender_name="Ada",
        )
        from olano.tools.chat_directory_tools import chat_messages_recent

        out = chat_messages_recent.invoke({"chat": "Community Group"})
        assert "event friday!" in out
        assert "Ada" in out
        assert "untrusted" in out.lower() or "Context only" in out

    def test_recent_empty_explains_group_listen(self):
        from olano.tools.chat_directory_tools import chat_messages_recent

        out = chat_messages_recent.invoke({"chat": "-100999"})
        assert "group_listen" in out

    def test_search_across_chats(self):
        d = get_chat_directory()
        d.record_observed("agent-a", "telegram", chat_id="-1", text="pizza night friday")
        d.record_observed("agent-a", "whatsapp", chat_id="1@g.us", text="pizza again")
        from olano.tools.chat_directory_tools import chat_messages_search

        out = chat_messages_search.invoke({"query": "pizza"})
        assert out.count("pizza") >= 2

    def test_search_ambiguous_chat_name_lists_candidates(self):
        d = get_chat_directory()
        d.record_membership("agent-a", "telegram", "-1", chat_name="Book Club East")
        d.record_membership("agent-a", "telegram", "-2", chat_name="Book Club West")
        from olano.tools.chat_directory_tools import chat_messages_search

        out = chat_messages_search.invoke({"query": "x", "chat": "Book Club"})
        assert "several chats" in out
        assert "id=-1" in out and "id=-2" in out
