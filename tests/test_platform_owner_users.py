"""owner_users admission parity on the non-Telegram hard-allowlist adapters.

``owner_users`` designates owners without gating admission (trust is unioned
in ``runtime/server.py``; Telegram's adapter half lives in
test_platform_telegram_access.py). On every adapter that enforces
``allowed_users`` as a hard admission gate — WhatsApp, DeBox, DingTalk — the
same two properties must hold:

* owner_users alone never closes the bot, and
* a configured allowlist never locks out a designated owner.
"""

from __future__ import annotations

from olano.platforms.config import PlatformConfig
from olano.platforms.debox import DeBoxAdapter
from olano.platforms.dingtalk import DingTalkAdapter
from olano.platforms.whatsapp import WhatsAppAdapter


def _config(**extra) -> PlatformConfig:
    return PlatformConfig(enabled=True, token="tok", extra=extra)


class TestWhatsApp:
    @staticmethod
    def _msg(phone: str) -> dict:
        return {"senderId": f"{phone}@s.whatsapp.net", "chatId": f"{phone}@s.whatsapp.net"}

    def test_owner_users_alone_never_closes_the_bot(self):
        adapter = WhatsAppAdapter(_config(owner_users=["+12025550188"]), agent_name="t")
        assert adapter._is_allowed(self._msg("111")) is True

    def test_owner_admitted_even_when_allowlist_omits_them(self):
        adapter = WhatsAppAdapter(
            _config(allowed_users=["+111"], owner_users=["+12025550188"]), agent_name="t"
        )
        assert adapter._is_allowed(self._msg("12025550188")) is True
        assert adapter._is_allowed(self._msg("111")) is True
        assert adapter._is_allowed(self._msg("222")) is False

    def test_owner_entries_normalize_like_allowed_entries(self):
        adapter = WhatsAppAdapter(_config(owner_users=["+1 202-555-0188"]), agent_name="t")
        assert adapter._owner_users == {"12025550188@s.whatsapp.net"}


class TestDeBox:
    def test_owner_users_alone_never_closes_the_bot(self):
        adapter = DeBoxAdapter(_config(owner_users=["boss1"]), agent_name="t")
        assert adapter._allowed_users == set()
        assert adapter._owner_users == {"boss1"}

    def test_owner_unioned_into_admission(self):
        adapter = DeBoxAdapter(
            _config(allowed_users=["user1"], owner_users=["Boss1"]), agent_name="t"
        )
        admitted = adapter._allowed_users | adapter._owner_users
        assert "boss1" in admitted
        assert "user1" in admitted
        assert "stranger" not in admitted


class TestDingTalk:
    def test_owner_users_alone_never_closes_the_bot(self):
        adapter = DingTalkAdapter(_config(owner_users=["boss1"]), agent_name="t")
        assert adapter._is_user_allowed("stranger", "") is True

    def test_owner_admitted_even_when_allowlist_omits_them(self):
        adapter = DingTalkAdapter(
            _config(allowed_users=["staff1"], owner_users=["Boss1"]), agent_name="t"
        )
        assert adapter._is_user_allowed("boss1", "") is True
        assert adapter._is_user_allowed("", "staff1") is True
        assert adapter._is_user_allowed("stranger", "") is False

    def test_comma_separated_owner_string_parsed(self):
        adapter = DingTalkAdapter(_config(owner_users="Boss1, boss2"), agent_name="t")
        assert adapter._owner_users == {"boss1", "boss2"}
