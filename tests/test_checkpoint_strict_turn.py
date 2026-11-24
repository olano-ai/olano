"""Two agent turns on one thread under strict checkpoint encryption.

This is the sealed-box runtime env end to end: ``LANGGRAPH_AES_KEY`` set,
``LIONCLAW_CHECKPOINT_REQUIRE_ENCRYPTED=1``, ``LANGGRAPH_STRICT_MSGPACK=true``. The
second turn resumes the first turn's checkpoint, which is the deserialization
path the strict serializer guards, on the deterministic ``fake:instant`` model
(no API key), through the real sqlite saver.

``LANGGRAPH_STRICT_MSGPACK`` is read by langgraph ONCE, at import time
(``langgraph.checkpoint.serde._msgpack.STRICT_MSGPACK_ENABLED``, and the copy
``langgraph._internal._serde`` takes of it), and this process imported langgraph
long before this test ran (conftest imports ``olano.core.checkpoints``). Setting
the env var here therefore changes nothing on its own, so the test also flips the
two module flags langgraph derived from it: that is the state a process started
with the variable set is in, and it makes the compile-time and per-call allowlist
plumbing (``Pregel._apply_checkpointer_allowlist`` →
``BaseCheckpointSaver.with_allowlist``) really run against olano's strict
serializer, which is the path that would silently replace it with the permissive
class if the strict inner serializer did not merge in place. olano's own
strictness does not depend on that timing at all: the strict inner serializer is
built with an explicit ``allowed_msgpack_modules=None``.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from langgraph._internal import _serde as lg_serde
from langgraph.checkpoint.serde import _msgpack as lg_msgpack
from langgraph.checkpoint.serde.event_hooks import register_serde_event_listener

from olano.core.checkpoints import (
    CHECKPOINT_AES_KEY_ENV,
    CHECKPOINT_REQUIRE_ENCRYPTED_ENV,
    CheckpointerFactory,
    StrictEncryptedSerializer,
    aclose_checkpointers,
    get_thread_db_path,
    scope_thread,
)

_AES_KEY = "sealed-box-checkpoint-key-32byte"  # exactly 32 bytes: AES-256
_THREAD = "strict-e2e"
_FIRST = "first turn SEALED-MARKER-3b7e"
_SECOND = "second turn SEALED-MARKER-9c1d"
# Every allowlist decision the serializer makes goes through this hook, with no
# dedup; the log warnings it also emits are deduped per type for the life of the
# process, so an earlier test could hide one from caplog.
_ALLOWLIST_EVENTS = {"msgpack_blocked", "msgpack_unregistered_allowed", "msgpack_method_blocked"}
_ALLOWLIST_LOG_FRAGMENTS = (
    "allowed_msgpack_modules",
    "not in the deserialization allowlist",
    "does not support msgpack allowlist",
    "does not support with_allowlist",
)


async def _turn(runner: Any, message: str) -> tuple[str, dict[str, int]]:
    text, events = "", {}
    async for etype, payload in runner.astream_events(message, thread_id=_THREAD):
        events[etype] = events.get(etype, 0) + 1
        if etype == "text":
            text += payload
    return text, events


def _rows(db: Path, table: str, blob_col: str) -> list[tuple[str, bytes, bytes]]:
    """``(type, blob, every column of the row as bytes)`` for each row of ``table``.

    The third element covers the columns the cipher does not (the sqlite saver
    writes ``metadata`` as plain JSON), so a user message leaking through one of
    them fails the test rather than passing unseen.
    """
    con = sqlite3.connect(db)
    try:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        rows = con.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        con.close()
    out = []
    for row in rows:
        rec = dict(zip(cols, row))
        whole = b"".join(
            v if isinstance(v, bytes) else str(v).encode() for v in row if v is not None
        )
        out.append((rec["type"], bytes(rec[blob_col]), whole))
    return out


async def test_two_turns_resume_under_strict_encryption(
    base_config, lionclaw_home: Path, monkeypatch, caplog
) -> None:
    assert len(_AES_KEY.encode()) == 32
    monkeypatch.setenv(CHECKPOINT_AES_KEY_ENV, _AES_KEY)
    monkeypatch.setenv(CHECKPOINT_REQUIRE_ENCRYPTED_ENV, "1")
    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    # Import-time flag (see the module docstring): put langgraph in the state it
    # would be in had the variable been set before the process started.
    monkeypatch.setattr(lg_msgpack, "STRICT_MSGPACK_ENABLED", True)
    monkeypatch.setattr(lg_serde, "STRICT_MSGPACK_ENABLED", True)
    monkeypatch.setenv("LIONCLAW_FAKE_TOKENS", "8")
    base_config.model = "fake:instant"

    from olano.core.builder import build_agent_async
    from olano.core.runner import AgentRunner

    serde_events: list[dict[str, Any]] = []
    unregister = register_serde_event_listener(serde_events.append)
    fresh = CheckpointerFactory()
    try:
        with caplog.at_level(logging.WARNING, logger="langgraph"):
            agent = await build_agent_async(base_config)
            runner = AgentRunner(agent, base_config)
            text1, _ = await _turn(runner, _FIRST)
            text2, events2 = await _turn(runner, _SECOND)

        # Both turns answered; the fake model's per-instance counter shows the
        # second turn is the second model call, not a replay of the first.
        assert "Fake response #0" in text1
        assert "Fake response #1" in text2
        assert events2.get("usage", 0) >= 1

        # The thread state the second turn resumed holds both turns. This is the
        # real resume path: langgraph 1.x keeps ``messages`` as a delta snapshot
        # plus per-step deltas in ``writes`` (no checkpoint row's channel_values
        # ever carries the list), and ``aget_state`` assembles it through the
        # saver, so every one of those rows was decoded by the strict serializer.
        cfg = {"configurable": {"thread_id": scope_thread(base_config.name, _THREAD)}}
        state = await agent.aget_state(cfg)
        contents = [str(m.content) for m in state.values["messages"]]
        assert len(contents) == 4, contents
        assert contents[0] == _FIRST and contents[2] == _SECOND
        assert "Fake response #0" in contents[1]
        assert "Fake response #1" in contents[3]

        # A fresh strict reader (new connection, new saver) decodes the latest
        # checkpoint tuple, its pending writes, and every checkpoint of the
        # thread's history; each is one ``loads_typed`` the strict class refused
        # nothing on.
        saver = fresh.get(base_config.name, lionclaw_home)
        assert isinstance(saver.serde, StrictEncryptedSerializer)
        tup = saver.get_tuple(cfg)
        assert tup is not None
        history = list(saver.list(cfg))
        assert len(history) >= 2
        assert history[0].checkpoint["id"] == tup.checkpoint["id"]
        assert {t.metadata.get("source") for t in history} >= {"input", "loop"}

        # Nothing was blocked or let through with a warning by the msgpack
        # allowlist (the graph's own state types were merged into the strict
        # serializer), and langgraph never fell back to "allowlist unsupported"
        # on our serializer.
        assert [e for e in serde_events if e["kind"] in _ALLOWLIST_EVENTS] == []
        offending = [
            r.getMessage()
            for r in caplog.records
            if r.name.startswith("langgraph")
            and any(frag in r.getMessage() for frag in _ALLOWLIST_LOG_FRAGMENTS)
        ]
        assert offending == []
    finally:
        unregister()
        fresh.reset()
        # aiosqlite's worker is a non-daemon thread: an unclosed connection
        # after a failed assertion would keep the whole pytest process alive.
        await aclose_checkpointers()

    # On disk every row of both tables is ciphertext, tagged with the cipher,
    # and no column of any row carries either user message in the clear.
    db = get_thread_db_path(lionclaw_home)
    checkpoints = _rows(db, "checkpoints", "checkpoint")
    writes = _rows(db, "writes", "value")
    assert checkpoints and writes
    for typ, blob, whole in checkpoints + writes:
        assert typ.endswith("+aes"), typ
        for secret in (_FIRST.encode(), _SECOND.encode()):
            assert secret not in blob
            assert secret not in whole
