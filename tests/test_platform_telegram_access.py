"""Telegram sender gates: ``allowed_users`` (admission) vs ``owner_users`` (trust).

``allowed_users`` decides who may message the bot at all AND marks those
senders as owners. ``owner_users`` designates owners WITHOUT closing the bot —
the open-bot-with-a-real-owner configuration. The trust union itself is
computed in ``runtime/server.py`` (see test_platform_control.py); this module
covers the adapter-side gates: message admission and gated inline buttons.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from olano.platforms.config import PlatformConfig
from olano.platforms.telegram import TelegramAdapter


@pytest.fixture(autouse=True)
def _no_env_allowlist(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)


def _adapter(**extra) -> TelegramAdapter:
    return TelegramAdapter(
        PlatformConfig(enabled=True, token="123:abc", extra=extra), agent_name="t"
    )


def _msg(user_id: int = 1, username: str = ""):
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id, username=username))


class TestSenderAdmission:
    def test_open_bot_admits_everyone(self):
        assert _adapter()._sender_is_allowed(_msg(7)) is True

    def test_owner_users_alone_never_closes_the_bot(self):
        # Designating an owner must not turn the bot allowlist-only.
        adapter = _adapter(owner_users=["42"])
        assert adapter._sender_is_allowed(_msg(7)) is True

    def test_allowlist_blocks_strangers_and_admits_listed(self):
        adapter = _adapter(allowed_users=["1"])
        assert adapter._sender_is_allowed(_msg(1)) is True
        assert adapter._sender_is_allowed(_msg(2)) is False

    def test_owner_admitted_even_when_allowlist_omits_them(self):
        # Designating an owner must never lock that owner out.
        adapter = _adapter(allowed_users=["1"], owner_users=["42"])
        assert adapter._sender_is_allowed(_msg(42)) is True
        assert adapter._sender_is_allowed(_msg(2)) is False

    def test_owner_username_admitted_case_insensitively(self):
        adapter = _adapter(allowed_users=["1"], owner_users=["@Boss"])
        assert adapter._sender_is_allowed(_msg(9, "BOSS")) is True

    def test_scalar_owner_users_parsed_as_single_entry(self):
        adapter = _adapter(owner_users="@Boss")
        assert adapter._owner_usernames == {"boss"}
        assert adapter._owner_ids == set()


class TestCallbackAuthorization:
    """Gated inline buttons (exec approvals, update prompts)."""

    def test_open_bot_without_any_list_is_open(self):
        assert _adapter()._is_callback_user_authorized("7") is True

    def test_owner_users_gates_buttons_to_owner_ids(self):
        adapter = _adapter(owner_users=["42"])
        assert adapter._is_callback_user_authorized("42") is True
        assert adapter._is_callback_user_authorized("7") is False

    def test_allowed_and_owner_ids_both_authorized(self):
        adapter = _adapter(allowed_users=["1"], owner_users=["42"])
        assert adapter._is_callback_user_authorized("1") is True
        assert adapter._is_callback_user_authorized("42") is True
        assert adapter._is_callback_user_authorized("2") is False

    def test_username_only_owner_locks_buttons(self):
        # Buttons honor numeric IDs only (usernames are freely changeable),
        # so a handle-only owner list locks the buttons rather than opening
        # them — same shape as a handle-only allowed_users list.
        adapter = _adapter(owner_users=["@boss"])
        assert adapter._is_callback_user_authorized("7") is False
