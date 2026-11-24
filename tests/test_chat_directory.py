"""Chat directory: store, passive recorder, tools, and target resolution."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from olano.platforms import chat_directory as cd
from olano.platforms.chat_directory import get_chat_directory
from olano.platforms.config import Platform
from olano.platforms.session import SessionSource
from olano.platforms.targets import (
    TargetResolutionError,
    looks_like_address,
    resolve_target,
)


@pytest.fixture(autouse=True)
def _clear_throttle():
    cd._recent_writes.clear()
    yield
    cd._recent_writes.clear()


def _group_source(chat_id: str = "-100123", name: str = "Community Group") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_name=name,
        chat_type="group",
        user_id="42",
        user_name="Ada Lovelace",
        username="ada",
    )


# ─── Store ───────────────────────────────────────────────────────────────────


class TestChatDirectoryStore:
    def test_record_message_upserts_chat_and_participant(self):
        d = get_chat_directory()
        d.record_message("agent-a", "telegram", _group_source())
        d.record_message("agent-a", "telegram", _group_source())

        chats = d.list_chats("agent-a")
        assert len(chats) == 1
        assert chats[0]["chat_name"] == "Community Group"
        assert chats[0]["msg_count"] == 2

        users = d.find_users("agent-a", "ada")
        assert len(users) == 1
        assert users[0]["user_id"] == "42"
        assert users[0]["display_name"] == "Ada Lovelace"

    def test_rename_updates_but_empty_name_never_erases(self):
        d = get_chat_directory()
        d.record_message("agent-a", "telegram", _group_source(name="Old Name"))
        d.record_message("agent-a", "telegram", _group_source(name="New Name"))
        assert d.list_chats("agent-a")[0]["chat_name"] == "New Name"

        d.record_message("agent-a", "telegram", _group_source(name=""))
        assert d.list_chats("agent-a")[0]["chat_name"] == "New Name"

    def test_agent_scoping(self):
        d = get_chat_directory()
        d.record_message("agent-a", "telegram", _group_source())
        assert d.list_chats("agent-b") == []
        assert d.find_chats("agent-b", "Community") == []

    def test_membership_join_and_leave(self):
        d = get_chat_directory()
        d.record_membership("agent-a", "telegram", "-100999", chat_name="Silent Group", joined=True)
        assert d.list_chats("agent-a")[0]["msg_count"] == 0  # no message seen yet

        d.record_membership("agent-a", "telegram", "-100999", joined=False)
        assert d.list_chats("agent-a") == []  # inactive rows drop out
        assert d.list_chats("agent-a", include_inactive=True)[0]["active"] == 0

    def test_migration_records_successor_and_resolves_through(self):
        d = get_chat_directory()
        d.record_message("agent-a", "telegram", _group_source(chat_id="-123", name="Team"))
        d.record_migration("agent-a", "telegram", "-123", "-100456")

        old = d.get_chat("agent-a", "telegram", "-123")
        assert old is not None and old["migrated_to"] == "-100456" and old["active"] == 0
        new = d.get_chat("agent-a", "telegram", "-100456")
        assert new is not None and new["chat_name"] == "Team" and new["active"] == 1

        resolved, _ = d.resolve_chat_id("agent-a", "telegram", "Team")
        assert resolved == "-100456"

    def test_enumeration_merges_without_bumping_counts(self):
        d = get_chat_directory()
        d.record_enumerated(
            "agent-a",
            "whatsapp",
            [{"chat_id": "1@g.us", "name": "Family", "type": "group"}],
        )
        rows = d.list_chats("agent-a", platform="whatsapp")
        assert rows[0]["chat_name"] == "Family"
        assert rows[0]["msg_count"] == 0

    def test_find_ranks_exact_name_before_substring(self):
        d = get_chat_directory()
        d.record_membership("agent-a", "telegram", "-1", chat_name="team announcements")
        d.record_membership("agent-a", "telegram", "-2", chat_name="team")
        rows = d.find_chats("agent-a", "team")
        assert rows[0]["chat_id"] == "-2"

    def test_find_matches_exact_id(self):
        d = get_chat_directory()
        d.record_membership("agent-a", "telegram", "-100777", chat_name="Ops")
        rows = d.find_chats("agent-a", "-100777")
        assert rows and rows[0]["chat_name"] == "Ops"

    def test_resolve_ambiguous_returns_candidates(self):
        d = get_chat_directory()
        d.record_membership("agent-a", "telegram", "-1", chat_name="Book Club East")
        d.record_membership("agent-a", "telegram", "-2", chat_name="Book Club West")
        resolved, candidates = d.resolve_chat_id("agent-a", "telegram", "Book Club")
        assert resolved is None
        assert len(candidates) == 2

    def test_like_wildcards_are_literal(self):
        d = get_chat_directory()
        d.record_membership("agent-a", "telegram", "-1", chat_name="100% legit")
        d.record_membership("agent-a", "telegram", "-2", chat_name="other")
        rows = d.find_chats("agent-a", "100%")
        assert [r["chat_id"] for r in rows] == ["-1"]


# ─── Passive recorder ────────────────────────────────────────────────────────


class TestRecorder:
    def test_record_message_event_writes_through_fake_adapter(self):
        adapter = SimpleNamespace(agent_name="agent-a")
        event = SimpleNamespace(source=_group_source())
        cd.record_message_event(adapter, event)
        assert get_chat_directory().list_chats("agent-a")[0]["chat_id"] == "-100123"

    def test_recorder_throttles_repeat_writes(self):
        adapter = SimpleNamespace(agent_name="agent-a")
        event = SimpleNamespace(source=_group_source())
        cd.record_message_event(adapter, event)
        cd.record_message_event(adapter, event)  # throttled — same names, same window
        assert get_chat_directory().list_chats("agent-a")[0]["msg_count"] == 1

    def test_recorder_rename_bypasses_throttle(self):
        adapter = SimpleNamespace(agent_name="agent-a")
        cd.record_message_event(adapter, SimpleNamespace(source=_group_source(name="One")))
        cd.record_message_event(adapter, SimpleNamespace(source=_group_source(name="Two")))
        assert get_chat_directory().list_chats("agent-a")[0]["chat_name"] == "Two"

    def test_recorder_without_agent_is_a_noop(self):
        adapter = SimpleNamespace(agent_name="")
        cd.record_message_event(adapter, SimpleNamespace(source=_group_source()))
        assert get_chat_directory().list_chats("") == []

    def test_membership_event_uses_adapter_platform(self):
        adapter = SimpleNamespace(agent_name="agent-a", platform=Platform.TELEGRAM)
        cd.record_membership_event(adapter, "-100555", chat_name="Joined Group")
        rows = get_chat_directory().list_chats("agent-a", platform="telegram")
        assert rows[0]["chat_id"] == "-100555"


# ─── Target resolution (point 4 — cold sends must never be blocked) ─────────


class TestLooksLikeAddress:
    @pytest.mark.parametrize(
        ("channel", "target"),
        [
            ("telegram", "-1001234567890"),
            ("telegram", "123456789"),
            ("telegram", "biz:conn1:12345"),
            ("whatsapp", "12345@g.us"),
            ("whatsapp", "491701234567@s.whatsapp.net"),
            ("whatsapp", "+49 170 1234567"),
            ("whatsapp", "4917012345678"),
            ("discord", "112233445566778899"),  # gitleaks:allow
            ("slack", "C0123456789"),
            ("slack", "U0AB12CD3"),
            ("matrix", "!room:example.org"),
            ("matrix", "@user:example.org"),
            ("signal", "group:abcdef=="),
            ("signal", "+15551234567"),
            ("email", "user@example.com"),
            ("mattermost", "8soyabwthjnf9qibfztje5a36h"),  # lenient default, gitleaks:allow
        ],
    )
    def test_addresses_pass(self, channel: str, target: str):
        assert looks_like_address(channel, target) is True

    @pytest.mark.parametrize(
        ("channel", "target"),
        [
            ("telegram", "Community Group"),
            ("telegram", "@somehandle"),
            ("whatsapp", "Family"),
            ("whatsapp", "@marketing"),
            ("discord", "general"),
            ("slack", "#general"),
            ("signal", "Book Club"),
            ("email", "@not-an-email"),
        ],
    )
    def test_names_do_not_pass(self, channel: str, target: str):
        assert looks_like_address(channel, target) is False


class TestResolveTarget:
    def test_explicit_id_passes_through_even_when_unknown(self):
        # Cold send: an id the directory has never seen must NOT be blocked.
        assert resolve_target("agent-a", "telegram", "-100987654") == "-100987654"
        assert resolve_target("agent-a", "whatsapp", "+15551230000") == "+15551230000"

    def test_name_resolves_via_directory(self):
        get_chat_directory().record_membership(
            "agent-a", "telegram", "-100123", chat_name="Community Group"
        )
        assert resolve_target("agent-a", "telegram", "Community Group") == "-100123"

    def test_name_resolution_follows_migration(self):
        d = get_chat_directory()
        d.record_membership("agent-a", "telegram", "-123", chat_name="Team")
        d.record_migration("agent-a", "telegram", "-123", "-100456")
        assert resolve_target("agent-a", "telegram", "Team") == "-100456"

    def test_ambiguous_name_raises_with_candidates(self):
        d = get_chat_directory()
        d.record_membership("agent-a", "telegram", "-1", chat_name="Book Club East")
        d.record_membership("agent-a", "telegram", "-2", chat_name="Book Club West")
        with pytest.raises(TargetResolutionError, match="ambiguous"):
            resolve_target("agent-a", "telegram", "Book Club")

    def test_unknown_spaced_name_on_strict_channel_raises_actionably(self):
        with pytest.raises(TargetResolutionError, match="chats_find"):
            resolve_target("agent-a", "telegram", "No Such Group")

    def test_unknown_handle_on_strict_channel_raises_actionably(self):
        # An @handle the directory can't resolve used to fall through to the
        # adapter, where Telegram's int('@nobody') raised a bare ValueError
        # the calling agent couldn't act on. It must fail here instead, with
        # the fact that matters: the directory only knows users who have
        # already messaged this agent.
        with pytest.raises(TargetResolutionError, match="never messaged"):
            resolve_target("agent-a", "telegram", "@nobody")

    def test_fuzzy_handle_with_candidates_still_reports_ambiguity(self):
        get_chat_directory().record_participant(
            "agent-a", "telegram", "555", username="adalovely", display_name="Ada L"
        )
        get_chat_directory().record_participant(
            "agent-a", "telegram", "556", username="adalace", display_name="Ada Lace"
        )
        # "@ada" fuzzy-matches both users and is an exact handle of neither:
        # the ambiguity error (listing candidates with ids) outranks the
        # unknown-handle failure.
        with pytest.raises(TargetResolutionError, match="ambiguous"):
            resolve_target("agent-a", "telegram", "@ada")

    def test_unknown_single_token_passes_through_even_on_strict_channels(self):
        # An unconventional-but-maybe-valid id (test fakes, custom adapters)
        # must reach the adapter — only impossible addresses fail early.
        assert resolve_target("agent-a", "mattermost", "chat-1") == "chat-1"

    @pytest.mark.parametrize(
        ("channel", "target"),
        [
            ("telegram", "jrivera"),  # a bare handle, as an agent reads it off a label
            ("telegram", "chat-1"),
            ("discord", "general"),
        ],
    )
    def test_bare_name_on_int_addressed_channel_raises_actionably(self, channel, target):
        # Telegram and Discord address chats by an INTEGER id, so a token that
        # isn't one is guaranteed to die in the adapter as
        # `int('jrivera')` — a bare ValueError the calling agent can't act on.
        # Only the leading "@" used to be caught here, so the same handle
        # written without it fell straight through.
        with pytest.raises(TargetResolutionError, match="never messaged"):
            resolve_target("agent-a", channel, target)

    def test_bare_handle_resolves_to_known_user(self):
        # The "@" is optional on the way in — the directory strips it anyway.
        get_chat_directory().record_participant(
            "agent-a", "telegram", "5550001234", username="jrivera", display_name="Jamie"
        )
        assert resolve_target("agent-a", "telegram", "jrivera") == "5550001234"

    def test_unknown_name_on_lenient_channel_passes_through(self):
        assert resolve_target("agent-a", "mattermost", "town-square") == "town-square"

    def test_handle_resolves_to_known_user(self):
        get_chat_directory().record_participant(
            "agent-a", "telegram", "424242", username="ada", display_name="Ada"
        )
        assert resolve_target("agent-a", "telegram", "@ada") == "424242"


class TestOwnerFallback:
    """The last-resort resolution of target='owner' from the channel config."""

    def test_owner_users_outranks_allowed_users(self):
        from olano.platforms.targets import _from_allowed_users

        entries = [
            {
                "type": "platform:telegram",
                "extra": {"allowed_users": ["111", "222"], "owner_users": ["999"]},
            }
        ]
        assert _from_allowed_users(entries) == "999"

    def test_first_allowed_user_when_no_owner_users(self):
        from olano.platforms.targets import _from_allowed_users

        # @handle forms are skipped — adapters address chats by id.
        entries = [{"type": "telegram", "allowed_users": ["@handle", "333"]}]
        assert _from_allowed_users(entries) == "333"


# ─── Chat-context prefix (point 5) ──────────────────────────────────────────


class TestChatPrefix:
    def test_dm_has_no_chat_prefix(self):
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="dm")
        assert source.format_chat_prefix() == ""

    def test_group_prefix_carries_name_and_id(self):
        prefix = _group_source().format_chat_prefix()
        assert "Community Group" in prefix
        assert "chat id=-100123" in prefix

    def test_unnamed_channel_prefix_still_carries_id(self):
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="-100777", chat_type="channel")
        assert source.format_chat_prefix() == "[in channel, chat id=-100777]"


# ─── Agent tools ─────────────────────────────────────────────────────────────


class TestTools:
    @pytest.fixture(autouse=True)
    def _bind_agent(self):
        from olano.tools.talk_to import current_agent

        token = current_agent.set("agent-a")
        yield
        current_agent.reset(token)

    def test_chats_find_lists_matches_with_ids(self):
        get_chat_directory().record_membership(
            "agent-a", "telegram", "-100123", chat_name="Community Group"
        )
        from olano.tools.chat_directory_tools import chats_find

        out = chats_find.invoke({"query": "community"})
        assert "Community Group" in out
        assert "id=-100123" in out

    def test_chats_find_miss_gives_guidance(self):
        from olano.tools.chat_directory_tools import chats_find

        out = chats_find.invoke({"query": "nothing-here"})
        assert "No known chat" in out

    def test_chats_list_orders_and_filters(self):
        d = get_chat_directory()
        d.record_membership("agent-a", "telegram", "-1", chat_name="TG Group")
        d.record_enumerated("agent-a", "whatsapp", [{"chat_id": "9@g.us", "name": "WA Group"}])
        from olano.tools.chat_directory_tools import chats_list

        out = chats_list.invoke({"platform": "whatsapp"})
        assert "WA Group" in out
        assert "TG Group" not in out

    def test_chats_find_user(self):
        get_chat_directory().record_participant(
            "agent-a", "telegram", "42", username="ada", display_name="Ada Lovelace"
        )
        from olano.tools.chat_directory_tools import chats_find_user

        out = chats_find_user.invoke({"query": "ada"})
        assert "id=42" in out

    def test_chat_info_without_adapter_reports_directory_state(self):
        get_chat_directory().record_membership(
            "agent-a", "telegram", "-100123", chat_name="Community Group"
        )
        from olano.tools.chat_directory_tools import chat_info

        out = chat_info.invoke({"chat_id": "-100123", "channel": "telegram"})
        assert "Community Group" in out
        assert "No live 'telegram' adapter" in out

    def test_chat_info_reports_migration(self):
        d = get_chat_directory()
        d.record_membership("agent-a", "telegram", "-123", chat_name="Team")
        d.record_migration("agent-a", "telegram", "-123", "-100456")
        from olano.tools.chat_directory_tools import chat_info

        out = chat_info.invoke({"chat_id": "-123", "channel": "telegram"})
        assert "id=-100456" in out


# ─── Forum topics ────────────────────────────────────────────────────────────


def _topic_source(thread_id: str = "3", topic: str = "Events") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-100123",
        chat_name="Community Group",
        chat_type="group",
        user_id="42",
        thread_id=thread_id,
        chat_topic=topic,
    )


class TestForumTopics:
    def test_topic_message_records_topic_row(self):
        d = get_chat_directory()
        d.record_message("agent-a", "telegram", _topic_source())
        row = d.get_chat("agent-a", "telegram", "-100123:3")
        assert row is not None
        assert row["chat_type"] == "topic"
        assert row["chat_name"] == "Community Group / Events"
        # The parent group row is recorded alongside.
        assert d.get_chat("agent-a", "telegram", "-100123") is not None

    def test_non_telegram_threads_do_not_mint_topic_rows(self):
        d = get_chat_directory()
        source = SessionSource(
            platform=Platform.SLACK,
            chat_id="C0123456789",
            chat_name="#general",
            chat_type="group",
            thread_id="1722.0001",
        )
        d.record_message("agent-a", "slack", source)
        assert d.get_chat("agent-a", "slack", "C0123456789:1722.0001") is None

    def test_topic_name_resolves_to_topic_target(self):
        d = get_chat_directory()
        d.record_message("agent-a", "telegram", _topic_source())
        assert resolve_target("agent-a", "telegram", "Events") == "-100123:3"

    def test_topic_ref_is_an_address(self):
        assert looks_like_address("telegram", "-1004366376806:3") is True
        assert looks_like_address("telegram", "-100123:3:4") is False

    def test_chat_prefix_includes_topic(self):
        prefix = _topic_source().format_chat_prefix()
        assert "chat id=-100123" in prefix
        assert 'topic "Events"' in prefix
        assert "thread id=3" in prefix

    def test_recorder_first_topic_message_bypasses_group_throttle(self):
        adapter = SimpleNamespace(agent_name="agent-a")
        cd.record_message_event(adapter, SimpleNamespace(source=_group_source()))
        cd.record_message_event(adapter, SimpleNamespace(source=_topic_source()))
        assert get_chat_directory().get_chat("agent-a", "telegram", "-100123:3") is not None


class TestTelegramTopicRefs:
    @pytest.fixture()
    def adapter_cls(self):
        pytest.importorskip("telegram")
        from olano.platforms.telegram import TelegramAdapter

        return TelegramAdapter

    def test_split_topic_ref(self, adapter_cls):
        assert adapter_cls._split_topic_ref("-1004366376806:3") == ("-1004366376806", "3")
        assert adapter_cls._split_topic_ref("-1004366376806") == ("-1004366376806", None)
        assert adapter_cls._split_topic_ref("biz:conn1:123") == ("biz:conn1:123", None)
        assert adapter_cls._split_topic_ref("12345") == ("12345", None)

    def test_apply_topic_ref_injects_thread_metadata(self, adapter_cls):
        adapter = object.__new__(adapter_cls)  # no config needed for this helper
        target, kwargs = adapter._apply_topic_ref("-100123:3", {})
        assert target == "-100123"
        assert kwargs["metadata"]["thread_id"] == "3"

        # Explicit metadata thread wins over the target suffix.
        target, kwargs = adapter._apply_topic_ref("-100123:3", {"metadata": {"thread_id": "9"}})
        assert kwargs["metadata"]["thread_id"] == "9"

        # Plain targets pass through untouched.
        target, kwargs = adapter._apply_topic_ref("-100123", {"metadata": None})
        assert target == "-100123" and kwargs == {"metadata": None}


class TestEnvelopeMetadataForwarding:
    async def test_channel_registry_forwards_metadata(self):
        from olano.platforms.delivery import (
            BaseChannelAdapter,
            ChannelRegistry,
            MessageEnvelope,
        )

        seen: dict = {}

        class _Recorder(BaseChannelAdapter):
            channel_type = "fake"

            async def send_text(self, text, target, **kwargs):
                seen.update({"target": target, "metadata": kwargs.get("metadata")})
                return "msg-1"

        registry = ChannelRegistry()
        registry.register("agent-a", "fake", _Recorder())
        envelope = MessageEnvelope(
            text="hi",
            target="chat-1",
            channel_type="fake",
            agent_name="agent-a",
            metadata={"thread_id": "3"},
        )
        result = await registry.send(envelope, raise_on_error=True)
        assert result == "msg-1"
        assert seen["metadata"] == {"thread_id": "3"}


# ─── Base adapter contract ──────────────────────────────────────────────────


async def test_base_list_chats_defaults_to_none():
    from olano.platforms.base import BasePlatformAdapter

    method = BasePlatformAdapter.list_chats
    result = await method(SimpleNamespace())  # default impl ignores self
    assert result is None
