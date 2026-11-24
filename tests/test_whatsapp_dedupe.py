"""Inbound dedupe on the WhatsApp QR connector.

WhatsApp replays offline messages when a linked device reconnects, and the
bridge process outlives the runtime that spawned it, so a runtime restart is
exactly when an already-processed message comes back. Without dedupe by
message id the agent ran it a second time (a duplicate trade instruction, a
second charge). These pin that the id is remembered across a restart and that
the file storing it stays bounded.
"""

from __future__ import annotations

from typing import Any

from olano.platforms.config import PlatformConfig
from olano.platforms.helpers import PersistentMessageDeduplicator
from olano.platforms.whatsapp import WhatsAppAdapter


def _adapter(tmp_path, monkeypatch) -> WhatsAppAdapter:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    return WhatsAppAdapter(PlatformConfig(enabled=True, extra={}), agent_name="desk")


def _msg(msg_id: str, chat: str = "6591234567@s.whatsapp.net") -> dict[str, Any]:
    return {"messageId": msg_id, "chatId": chat, "body": "approve trade 1"}


def test_a_redelivered_message_is_dropped_even_after_a_restart(tmp_path, monkeypatch) -> None:
    first = _adapter(tmp_path, monkeypatch)
    assert first._is_redelivery(_msg("3EB0A1")) is False
    assert first._is_redelivery(_msg("3EB0A1")) is True

    restarted = _adapter(tmp_path, monkeypatch)
    assert restarted._is_redelivery(_msg("3EB0A1")) is True
    assert restarted._is_redelivery(_msg("3EB0A2")) is False


def test_the_same_id_in_another_chat_is_a_different_message(tmp_path, monkeypatch) -> None:
    adapter = _adapter(tmp_path, monkeypatch)
    assert adapter._is_redelivery(_msg("3EB0A1", chat="a@s.whatsapp.net")) is False
    assert adapter._is_redelivery(_msg("3EB0A1", chat="b@s.whatsapp.net")) is False


def test_a_message_without_an_id_is_never_dropped(tmp_path, monkeypatch) -> None:
    adapter = _adapter(tmp_path, monkeypatch)
    assert adapter._is_redelivery({"chatId": "a", "body": "hi"}) is False
    assert adapter._is_redelivery({"chatId": "a", "body": "hi"}) is False


def test_the_store_stays_bounded_and_keeps_the_newest(tmp_path) -> None:
    path = tmp_path / "seen"
    store = PersistentMessageDeduplicator(path, limit=3)
    for i in range(10):
        assert store.is_duplicate(f"id{i}") is False

    assert len(path.read_text().splitlines()) <= 6
    reloaded = PersistentMessageDeduplicator(path, limit=3)
    assert reloaded.is_duplicate("id9") is True
    assert reloaded.is_duplicate("id0") is False


def test_an_unwritable_store_degrades_to_memory(tmp_path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    store = PersistentMessageDeduplicator(blocker / "seen")
    assert store.is_duplicate("x") is False
    assert store.is_duplicate("x") is True
