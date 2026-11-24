"""Tests for agent-scoped checkpoint keys.

Two agents the same human talks to must never share a LangGraph checkpoint lineage
(the bug where "cindy" resumed "marcus"'s thread and answered as marcus). These
cover the scoping helpers, the scoped delete, the in-place scoping migration, and
the at-rest encryption every saver opts into through ``LANGGRAPH_AES_KEY``, and the
strict mode (``LIONCLAW_CHECKPOINT_REQUIRE_ENCRYPTED``) that refuses plaintext rows.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde import _msgpack as lg_msgpack
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from olano.core.checkpoints import (
    CHECKPOINT_AES_KEY_ENV,
    CHECKPOINT_REQUIRE_ENCRYPTED_ENV,
    CheckpointerFactory,
    PlaintextCheckpointRefused,
    StrictEncryptedSerializer,
    checkpoint_serde,
    checkpoint_serde_kwargs,
    get_thread_db_path,
    require_encrypted_checkpoints,
    scope_thread,
    unscope_thread,
)

# ─────────────────────────────── helpers ─────────────────────────────────────


class TestScopeThread:
    def test_basic_prefix(self) -> None:
        assert scope_thread("cindy", "5550001234") == "cindy::5550001234"

    def test_idempotent(self) -> None:
        once = scope_thread("cindy", "5550001234")
        assert scope_thread("cindy", once) == once

    def test_two_agents_disjoint_for_same_thread(self) -> None:
        assert scope_thread("cindy", "5550001234") != scope_thread("marcus", "5550001234")

    # The uuid is written out rather than generated. A parametrize value
    # computed at COLLECTION time puts a different test id in every process,
    # which is what `pytest -n auto` reports as "Different tests were collected
    # between gw1 and gw2" — it compares the workers' collections and refuses to
    # run when they disagree. It also moved the id on every serial run, so
    # `--lf`, `--deselect` and any per-test history could never name this case.
    # The assertion is a pure string round-trip, so a fixed hex of the right
    # shape tests exactly what a fresh one did.
    @pytest.mark.parametrize(
        "raw",
        [
            "5550001234",
            "u:owner:448fd544b448440a8c80be19cccd36c9",
            "acp::cindy",
            "heartbeat_x",
            "tg_deadbeef",
        ],
    )
    def test_roundtrip(self, raw: str) -> None:
        scoped = scope_thread("cindy", raw)
        assert scoped.startswith("cindy::")
        assert unscope_thread("cindy", scoped) == raw

    def test_single_colon_scheme_not_mistaken_for_scoped(self) -> None:
        # ``u:<user>:<uuid>`` uses single colons — must still get a fresh prefix.
        scoped = scope_thread("cindy", "u:owner:abc")
        assert scoped == "cindy::u:owner:abc"

    def test_unscope_noop_when_unprefixed(self) -> None:
        assert unscope_thread("cindy", "raw123") == "raw123"


# ─────────────────────────────── scoped delete ───────────────────────────────


def test_delete_agent_threads_purges_only_that_agents_prefix(lionclaw_home: Path) -> None:
    """Deleting an agent takes its whole checkpoint history with it: every
    thread under ``<agent>::``, nothing under a neighbour whose name merely
    starts the same way (``LIKE`` would match ``cindy-2::`` and ``cindy_x::``,
    the prefix rule does not)."""
    from olano.core.checkpoints import delete_agent_threads

    factory = CheckpointerFactory()
    saver = factory.get("cindy", lionclaw_home)
    meta = {"source": "input", "step": -1, "parents": {}}
    for tid in ("cindy::t1", "cindy::t2", "cindy_x::t1", "cindy-2::t1", "other::t1"):
        cfg = {"configurable": {"thread_id": tid, "checkpoint_ns": ""}}
        saver.put(cfg, _checkpoint_with("row"), meta, {"messages": 1})
    try:
        assert delete_agent_threads("cindy", lionclaw_home) == 2
        db = get_thread_db_path(lionclaw_home)
        with sqlite3.connect(db) as conn:
            left = sorted(r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints"))
        assert left == ["cindy-2::t1", "cindy_x::t1", "other::t1"]
        # A second purge finds nothing and says so.
        assert delete_agent_threads("cindy", lionclaw_home) == 0
    finally:
        factory.reset()


def test_delete_agent_threads_takes_the_agents_background_runs(lionclaw_home: Path) -> None:
    """A cognition worker and a ``delegate_task`` child write under a raw
    ``cog::<parent>__cog__…`` id, never ``<parent>::``, so the prefix rule
    alone left every background run of a deleted agent behind. Runs of an
    agent whose name merely starts the same way stay."""
    from olano.core.checkpoints import delete_agent_threads

    factory = CheckpointerFactory()
    saver = factory.get("cindy", lionclaw_home)
    meta = {"source": "input", "step": -1, "parents": {}}
    for tid in (
        "cindy::t1",
        "cog::cindy__cog__pulse_agent__ab12cd",
        "cog::cindy__sub__research__ef34ab",
        "cog::cindy",
        "cog::cindyx__cog__pulse_agent__000000",
        "cog::cindy_x__cog__pulse_agent__111111",
        "cog::other__cog__pulse_agent__222222",
    ):
        cfg = {"configurable": {"thread_id": tid, "checkpoint_ns": ""}}
        saver.put(cfg, _checkpoint_with("row"), meta, {"messages": 1})
    try:
        assert delete_agent_threads("cindy", lionclaw_home) == 4
        db = get_thread_db_path(lionclaw_home)
        with sqlite3.connect(db) as conn:
            left = sorted(r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints"))
        assert left == [
            "cog::cindy_x__cog__pulse_agent__111111",
            "cog::cindyx__cog__pulse_agent__000000",
            "cog::other__cog__pulse_agent__222222",
        ]
    finally:
        factory.reset()


def test_thread_owner_reads_a_background_run_as_its_parents() -> None:
    from olano.core.checkpoints import thread_owner

    assert thread_owner("merli::u:abc:def") == "merli"
    assert thread_owner("merli::acp::aurelius::merli") == "merli"
    assert thread_owner("cog::merli__cog__pulse_agent__ab12cd") == "merli"
    assert thread_owner("cog::merli__sub__research__ab12cd") == "merli"
    assert thread_owner("sub::merli__sub__research__ab12cd") == "merli"
    assert thread_owner("cog::merli") == "merli"
    assert thread_owner("cog::zoho-email__cog__triage__ab12cd") == "zoho-email"
    assert thread_owner("raw-unscoped-id") is None
    assert thread_owner("") is None


async def test_adelete_scopes_thread_id() -> None:
    """``CheckpointerFactory.adelete`` must target the agent-scoped key."""
    factory = CheckpointerFactory()
    fake_saver = AsyncMock()
    factory.aget = AsyncMock(return_value=fake_saver)  # type: ignore[method-assign]

    await factory.adelete("cindy", "5550001234")

    fake_saver.adelete_thread.assert_awaited_once_with("cindy::5550001234")


# ─────────────────────────────── per-loop savers ─────────────────────────────


async def test_aget_saver_is_keyed_per_event_loop(lionclaw_home: Path) -> None:
    """A saver cached on one loop must never be handed to another loop.

    AsyncSqliteSaver's internal ``asyncio.Lock`` binds to the loop that first
    uses it; reusing the cached saver from a transient ``asyncio.run`` loop
    (e.g. a thread-origin scheduler run-now) used to crash with
    "is bound to a different event loop".
    """
    factory = CheckpointerFactory()
    config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    saver_main = await factory.aget("cindy", lionclaw_home)
    # Exercise the saver once so its lock binds to this loop.
    await saver_main.aget_tuple(config)

    def _use_from_foreign_loop() -> bool:
        async def _inner() -> bool:
            saver = await factory.aget("cindy", lionclaw_home)
            assert saver is not saver_main
            await saver.aget_tuple(config)  # raised RuntimeError on the shared saver
            return True

        return asyncio.run(_inner())

    assert await asyncio.to_thread(_use_from_foreign_loop)
    await factory.aclose()


async def test_aget_saver_cached_within_one_loop(lionclaw_home: Path) -> None:
    factory = CheckpointerFactory()
    first = await factory.aget("cindy", lionclaw_home)
    second = await factory.aget("cindy", lionclaw_home)
    assert first is second
    await factory.aclose()


async def test_aclose_agent_evicts_savers_on_every_loop(lionclaw_home: Path) -> None:
    factory = CheckpointerFactory()
    await factory.aget("cog::worker", lionclaw_home)
    await asyncio.to_thread(lambda: asyncio.run(factory.aget("cog::worker", lionclaw_home)))
    assert len(factory._async_conns) == 2

    await factory.aclose_agent("cog::worker", lionclaw_home)

    assert not factory._async_conns
    assert not factory._async_savers


# ─────────────────────────────── migration ───────────────────────────────────


def _make_threads_db(path: Path, thread_ids: list[str]) -> None:
    """Create a minimal langgraph-shaped checkpoint DB (checkpoints + writes)."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
        "parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB)"
    )
    con.execute(
        "CREATE TABLE writes (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
        "task_id TEXT, idx INTEGER, channel TEXT, type TEXT, value BLOB)"
    )
    for tid in thread_ids:
        con.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id) VALUES (?, '', 'c1')",
            (tid,),
        )
        con.execute(
            "INSERT INTO writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx) "
            "VALUES (?, '', 'c1', 't1', 0)",
            (tid,),
        )
    con.commit()
    con.close()


def _make_sessions(sessions_dir: Path, mapping: dict[str, list[str]]) -> None:
    for agent, stems in mapping.items():
        d = sessions_dir / agent
        d.mkdir(parents=True, exist_ok=True)
        for stem in stems:
            (d / f"{stem}.jsonl").write_text("{}\n", encoding="utf-8")


def _thread_ids(path: Path) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT DISTINCT thread_id FROM checkpoints")}
    finally:
        con.close()


def _write_thread_ids(path: Path) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT DISTINCT thread_id FROM writes")}
    finally:
        con.close()


class TestScopeMigration:
    def _setup(self, tmp_path: Path) -> tuple[Path, Path]:
        db = tmp_path / "threads.db"
        sessions = tmp_path / "sessions"
        # "solo" is owned by cindy only; "5550001234" by both cindy AND marcus
        # (contaminated); "orphan" has no transcript; "cog::x" is cognition.
        _make_threads_db(db, ["solo", "5550001234", "orphan", "cog::runner"])
        _make_sessions(
            sessions,
            {"cindy": ["solo", "5550001234"], "marcus": ["5550001234"]},
        )
        return db, sessions

    def test_rename_reset_and_leave(self, tmp_path: Path) -> None:
        from olano.core.checkpoint_migrate import scope_checkpoints_by_agent

        db, sessions = self._setup(tmp_path)
        res = scope_checkpoints_by_agent(db_path=db, sessions_dir=sessions, log=lambda _m: None)

        assert res.renamed == 1  # solo -> cindy::solo
        assert res.reset_contaminated == 1  # 5550001234 dropped (cindy+marcus)
        assert res.left_cognition == 1  # cog::runner untouched
        assert res.left_orphans == 1  # orphan kept (drop_orphans=False)

        tids = _thread_ids(db)
        assert "cindy::solo" in tids  # renamed
        assert "solo" not in tids
        assert "5550001234" not in tids  # contaminated → reset
        assert "cog::runner" in tids  # cognition preserved
        assert "orphan" in tids  # orphan preserved by default
        # The rename propagated to the writes table too.
        assert "cindy::solo" in _write_thread_ids(db)

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        from olano.core.checkpoint_migrate import scope_checkpoints_by_agent

        db, sessions = self._setup(tmp_path)
        before = _thread_ids(db)
        res = scope_checkpoints_by_agent(
            db_path=db, sessions_dir=sessions, dry_run=True, log=lambda _m: None
        )
        assert res.renamed == 1 and res.reset_contaminated == 1
        assert _thread_ids(db) == before  # unchanged on disk

    def test_drop_orphans_flag(self, tmp_path: Path) -> None:
        from olano.core.checkpoint_migrate import scope_checkpoints_by_agent

        db, sessions = self._setup(tmp_path)
        res = scope_checkpoints_by_agent(
            db_path=db, sessions_dir=sessions, drop_orphans=True, log=lambda _m: None
        )
        assert res.dropped_orphans == 1
        assert "orphan" not in _thread_ids(db)

    def test_idempotent(self, tmp_path: Path) -> None:
        from olano.core.checkpoint_migrate import scope_checkpoints_by_agent

        db, sessions = self._setup(tmp_path)
        scope_checkpoints_by_agent(db_path=db, sessions_dir=sessions, log=lambda _m: None)
        after_first = _thread_ids(db)
        res2 = scope_checkpoints_by_agent(db_path=db, sessions_dir=sessions, log=lambda _m: None)
        assert res2.renamed == 0  # nothing left to rename
        assert res2.left_scoped >= 1  # cindy::solo recognised as already scoped
        assert _thread_ids(db) == after_first  # stable

    def test_missing_db_is_noop(self, tmp_path: Path) -> None:
        from olano.core.checkpoint_migrate import scope_checkpoints_by_agent

        res = scope_checkpoints_by_agent(
            db_path=tmp_path / "nope.db", sessions_dir=tmp_path / "s", log=lambda _m: None
        )
        assert res.renamed == 0 and res.reset_contaminated == 0


# ─────────────────────────────── at-rest encryption ──────────────────────────
#
# LANGGRAPH_AES_KEY wraps every saver's JsonPlusSerializer in langgraph's
# EncryptedSerializer. These pin the three properties the sealed-volume rollout
# relies on: rows written under a key are ciphertext on disk, rows written
# before the key existed stay readable once it is set (enabling it on a live
# box loses nothing), and an unset key changes nothing at all.

_AES_KEY = "0123456789abcdef0123456789abcdef"  # 32 bytes -> AES-256, gitleaks:allow
_MARKER = "PLAINTEXT-MARKER-7f3a9c"
_CONFIG = {"configurable": {"thread_id": "cindy::t1", "checkpoint_ns": ""}}


def _checkpoint_with(marker: str) -> dict:
    cp = empty_checkpoint()
    cp["channel_values"] = {"messages": [marker]}
    cp["channel_versions"] = {"messages": 1}
    return cp


def _put_marker(saver) -> None:
    metadata = {"source": "input", "step": -1, "parents": {}}
    saver.put(_CONFIG, _checkpoint_with(_MARKER), metadata, {"messages": 1})


def _stored_rows(db: Path, table: str, blob_col: str) -> list[tuple[str, bytes]]:
    con = sqlite3.connect(db)
    try:
        rows = con.execute(f"SELECT type, {blob_col} FROM {table}").fetchall()
    finally:
        con.close()
    return [(r[0], bytes(r[1])) for r in rows]


def test_checkpoint_serde_is_none_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CHECKPOINT_AES_KEY_ENV, raising=False)
    assert checkpoint_serde() is None
    # Savers are then constructed with no serde argument at all — as before.
    assert checkpoint_serde_kwargs() == {}


def test_checkpoint_serde_is_cached_per_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CHECKPOINT_AES_KEY_ENV, _AES_KEY)
    first = checkpoint_serde()
    assert first is not None
    assert checkpoint_serde() is first
    assert checkpoint_serde_kwargs() == {"serde": first}


def test_wrong_length_key_names_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CHECKPOINT_AES_KEY_ENV, "too-short")
    with pytest.raises(ValueError, match=CHECKPOINT_AES_KEY_ENV):
        checkpoint_serde()


def test_sqlite_rows_are_ciphertext_under_a_key(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CHECKPOINT_AES_KEY_ENV, _AES_KEY)
    factory = CheckpointerFactory()
    saver = factory.get("cindy", lionclaw_home)
    _put_marker(saver)
    tup = saver.get_tuple(_CONFIG)
    assert tup is not None
    saver.put_writes(tup.config, [("messages", _MARKER)], "task-1")

    # Round-trips through the saver …
    assert tup.checkpoint["channel_values"]["messages"] == [_MARKER]
    again = saver.get_tuple(_CONFIG)
    assert again is not None and again.pending_writes is not None
    assert [w[2] for w in again.pending_writes] == [_MARKER]
    factory.reset()

    # … but on disk every blob is tagged with the cipher and holds no plaintext.
    db = get_thread_db_path(lionclaw_home)
    rows = _stored_rows(db, "checkpoints", "checkpoint") + _stored_rows(db, "writes", "value")
    assert len(rows) == 2
    for typ, blob in rows:
        assert typ.endswith("+aes"), typ
        assert _MARKER.encode() not in blob


def test_rows_written_without_a_key_stay_readable_after_it_is_set(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CHECKPOINT_AES_KEY_ENV, raising=False)
    factory = CheckpointerFactory()
    _put_marker(factory.get("cindy", lionclaw_home))
    factory.reset()
    db = get_thread_db_path(lionclaw_home)
    ((legacy_type, legacy_blob),) = _stored_rows(db, "checkpoints", "checkpoint")
    assert "+" not in legacy_type  # plaintext row, exactly as before this existed
    assert _MARKER.encode() in legacy_blob

    monkeypatch.setenv(CHECKPOINT_AES_KEY_ENV, _AES_KEY)
    factory = CheckpointerFactory()
    tup = factory.get("cindy", lionclaw_home).get_tuple(_CONFIG)
    factory.reset()
    assert tup is not None
    assert tup.checkpoint["channel_values"]["messages"] == [_MARKER]


# ─────────────────────────────── strict mode ─────────────────────────────────
#
# LIONCLAW_CHECKPOINT_REQUIRE_ENCRYPTED turns the "stays readable after the key
# is set" fallback pinned above into a refusal. On a box that promises every row
# is encrypted, a plaintext row is a row written without the key, and decoding
# it as if nothing were wrong is the leak the flag exists to close. These pin the
# refusal, that encrypted rows are untouched by it, that the flag without a key
# is refused, and the two seams that could quietly undo it: the per-mode cache
# and langgraph's allowlist rebuild of the wrapper.

_PLAINTEXT_ROW = JsonPlusSerializer().dumps_typed({"messages": [_MARKER]})


def _strict(monkeypatch: pytest.MonkeyPatch, key: str | None = _AES_KEY) -> None:
    monkeypatch.setenv(CHECKPOINT_REQUIRE_ENCRYPTED_ENV, "1")
    if key is None:
        monkeypatch.delenv(CHECKPOINT_AES_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(CHECKPOINT_AES_KEY_ENV, key)


@pytest.mark.parametrize("raw", ["1", "true", "YES", " On "])
def test_require_encrypted_truthy_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(CHECKPOINT_REQUIRE_ENCRYPTED_ENV, raw)
    assert require_encrypted_checkpoints() is True


@pytest.mark.parametrize("raw", ["", "0", "false", "off", "no", "strict"])
def test_require_encrypted_falsy_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(CHECKPOINT_REQUIRE_ENCRYPTED_ENV, raw)
    assert require_encrypted_checkpoints() is False


def test_strict_without_a_key_names_both_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    _strict(monkeypatch, key=None)
    with pytest.raises(ValueError) as excinfo:
        checkpoint_serde()
    assert CHECKPOINT_AES_KEY_ENV in str(excinfo.value)
    assert CHECKPOINT_REQUIRE_ENCRYPTED_ENV in str(excinfo.value)
    # Every saver constructor goes through the kwargs helper, so a box claiming
    # strictness with no key fails before a single row could be written.
    with pytest.raises(ValueError, match=CHECKPOINT_REQUIRE_ENCRYPTED_ENV):
        checkpoint_serde_kwargs()
    # An explicit permissive request is still "no key, no serde", exactly as before.
    assert checkpoint_serde(strict=False) is None
    assert checkpoint_serde_kwargs(strict=False) == {}


def test_strict_and_permissive_serializers_are_cached_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _strict(monkeypatch)
    strict = checkpoint_serde()
    permissive = checkpoint_serde(strict=False)
    assert isinstance(strict, StrictEncryptedSerializer)
    # Explicit ``strict=False`` yields what a flagless build makes, even with the
    # flag set: the migrator's source saver relies on exactly this.
    assert type(permissive) is EncryptedSerializer
    assert strict is not permissive
    assert checkpoint_serde() is strict
    assert checkpoint_serde(strict=True) is strict
    assert checkpoint_serde(strict=False) is permissive
    assert checkpoint_serde_kwargs() == {"serde": strict}
    assert checkpoint_serde_kwargs(strict=False) == {"serde": permissive}


def test_strict_inner_allowlist_is_explicit_not_env_timed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The strict inner serializer is pinned to langgraph's built-in allowlist.

    langgraph reads ``LANGGRAPH_STRICT_MSGPACK`` once, at import time, so a
    sentinel-default ``JsonPlusSerializer`` follows whatever the variable was
    when this process first imported langgraph (permissive, ``True``, in the
    suite). The strict inner is ``None`` regardless: olano's strictness never
    depends on when the variable was set relative to that first import.
    """
    _strict(monkeypatch)
    strict = checkpoint_serde()
    assert isinstance(strict, StrictEncryptedSerializer)
    assert strict.serde._allowed_msgpack_modules is None
    env_default = None if lg_msgpack.STRICT_MSGPACK_ENABLED else True
    assert JsonPlusSerializer()._allowed_msgpack_modules is env_default


def test_strict_refuses_a_plaintext_row_written_without_a_key(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CHECKPOINT_AES_KEY_ENV, raising=False)
    factory = CheckpointerFactory()
    _put_marker(factory.get("cindy", lionclaw_home))
    factory.reset()

    _strict(monkeypatch)
    factory = CheckpointerFactory()
    saver = factory.get("cindy", lionclaw_home)  # constructing the saver is fine
    with pytest.raises(PlaintextCheckpointRefused) as excinfo:
        saver.get_tuple(_CONFIG)
    factory.reset()
    err = excinfo.value
    assert isinstance(err, ValueError)
    assert CHECKPOINT_REQUIRE_ENCRYPTED_ENV in str(err)
    assert CHECKPOINT_AES_KEY_ENV in str(err)
    assert "+" not in err.row_type and repr(err.row_type) in str(err)


def test_strict_decodes_rows_written_encrypted(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rows written under the key alone (every box that only set the key) are
    # ``+aes`` on disk, so turning the flag on afterwards loses nothing.
    monkeypatch.setenv(CHECKPOINT_AES_KEY_ENV, _AES_KEY)
    factory = CheckpointerFactory()
    saver = factory.get("cindy", lionclaw_home)
    _put_marker(saver)
    tup = saver.get_tuple(_CONFIG)
    assert tup is not None
    saver.put_writes(tup.config, [("messages", _MARKER)], "task-1")
    factory.reset()

    _strict(monkeypatch)
    factory = CheckpointerFactory()
    saver = factory.get("cindy", lionclaw_home)
    assert isinstance(saver.serde, StrictEncryptedSerializer)
    tup = saver.get_tuple(_CONFIG)
    assert tup is not None and tup.pending_writes is not None
    assert tup.checkpoint["channel_values"]["messages"] == [_MARKER]
    assert [w[2] for w in tup.pending_writes] == [_MARKER]
    # And what the strict saver writes itself is ciphertext like any other row.
    other = {"configurable": {"thread_id": "cindy::t2", "checkpoint_ns": ""}}
    saver.put(other, _checkpoint_with(_MARKER), {"source": "input", "step": -1}, {"messages": 1})
    factory.reset()
    db = get_thread_db_path(lionclaw_home)
    rows = _stored_rows(db, "checkpoints", "checkpoint") + _stored_rows(db, "writes", "value")
    assert len(rows) == 3
    for typ, blob in rows:
        assert typ.endswith("+aes"), typ
        assert _MARKER.encode() not in blob


def test_strict_survives_langgraph_allowlist_rebuild(
    lionclaw_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What ``StateGraph.compile()`` and every ``stream``/``get_state`` do when
    ``LANGGRAPH_STRICT_MSGPACK`` is on: ``BaseCheckpointSaver.with_allowlist``
    pushes the graph's state types into the serializer, and rebuilds an
    ``EncryptedSerializer`` as the BASE class whenever its inner serializer
    changed. The strict inner merges in place and returns itself, so the strict
    wrapper stays, on the same saver, with the graph's types allowed."""
    _strict(monkeypatch)
    factory = CheckpointerFactory()
    saver = factory.get("cindy", lionclaw_home)
    strict = saver.serde
    assert isinstance(strict, StrictEncryptedSerializer)
    clone = saver.with_allowlist({("olano.tests", "Marker")})
    assert clone is saver
    assert clone.serde is strict
    assert ("olano.tests", "Marker") in strict.serde._allowed_msgpack_modules
    # Repeated per call by langgraph; nothing new merges, same saver back.
    assert saver.with_allowlist({("olano.tests", "Marker")}) is saver
    with pytest.raises(PlaintextCheckpointRefused):
        clone.serde.loads_typed(_PLAINTEXT_ROW)
    # The permissive wrapper takes upstream's rebuild as before (it has nothing
    # to lose): still the base class, still decoding plaintext.
    permissive = factory.get("marcus", lionclaw_home)  # same cache key: same serde
    factory.reset()
    assert permissive.serde is strict  # one serde per (key, strict) across savers
    rebuilt = EncryptedSerializer(strict.cipher, JsonPlusSerializer()).serde
    assert rebuilt.loads_typed(_PLAINTEXT_ROW) == {"messages": [_MARKER]}


def test_migrator_source_reads_permissively_and_destination_follows_the_box() -> None:
    """The one deliberate exception: a legacy ``threads.db`` is plaintext by
    definition, so the migrator's SOURCE saver must not be strict or it would
    refuse the very rows being moved; the DESTINATION follows the box's mode so
    those rows land encrypted where the box requires it."""
    import inspect

    from olano.core.checkpoint_migrate import migrate_sqlite_to_postgres

    src = inspect.getsource(migrate_sqlite_to_postgres)
    assert "checkpoint_serde_kwargs(strict=False)" in src
    assert "checkpoint_serde_kwargs()" in src
    assert src.index("checkpoint_serde_kwargs(strict=False)") < src.index("SqliteSaver(src_conn")
