"""The thread list reads a transcript again only when it changed.

``GET /api/agents/{name}/threads`` used to parse every transcript an agent had
in full on every call, so the most-used agent was the slowest to open (the
mobile app timed out and opened an empty chat). These pin that the cached
summary labels a row exactly as the full transcript does, and that an
unchanged transcript is never read twice.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from olano.core.thread_labels import derive_label
from olano.runtime import thread_summaries


@pytest.fixture
def api_client(tmp_workspace, monkeypatch):
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))
    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    runtime = MagicMock()
    runtime._runners = {}
    runtime._channels = []
    return TestClient(create_api(runtime))


def _sessions():
    from olano.config.home import get_olano_paths

    path = get_olano_paths().sessions_dir / "aurelius"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


SHAPES = {
    # A system notice ahead of the first human turn, provenance on a later row.
    "notice-first": [
        {"ts": "t0", "role": "system", "content": "prompt changed"},
        {"ts": "t1", "role": "user", "content": "[from @ana (Ana, id=7)]: check the deadline"},
        {
            "ts": "t2",
            "role": "assistant",
            "content": "done",
            "channel": "telegram",
            "model": "olano:pro",
        },
        {"ts": "t3", "role": "tool_call", "content": "{}"},
        {"ts": "t4", "role": "tool_result", "content": "r" * 500},
        {
            "ts": "t5",
            "role": "assistant",
            "content": "later",
            "model": "olano:max",
            "reasoning_effort": "high",
        },
    ],
    # No human turn at all: the label and preview fall back to the last row.
    "no-user": [
        {"ts": "t0", "role": "system", "content": "boot"},
        {"ts": "t1", "role": "assistant", "content": "unprompted hello"},
    ],
    "peer": [
        {"ts": "t0", "role": "peer_inbound", "content": "from a teammate", "sender": "merli"},
        {"ts": "t1", "role": "assistant", "content": "ok"},
    ],
    "scheduled_aurelius_abcdef12@2026": [
        {
            "ts": "t0",
            "role": "user",
            "content": "[Autonomous scheduled task] sweep",
            "origin": "scheduled",
        },
        {"ts": "t1", "role": "assistant", "content": "swept"},
    ],
}


@pytest.mark.parametrize("thread_id", sorted(SHAPES))
def test_the_summary_labels_a_thread_as_the_full_transcript_does(thread_id):
    entries = SHAPES[thread_id]
    summary = thread_summaries.summarize(entries)
    task_names = {"abcdef12": "Daily sweep"}
    for hint in ("", "whatsapp"):
        assert derive_label(
            thread_id, summary.head, task_names=task_names, channel_hint=hint
        ) == derive_label(thread_id, entries, task_names=task_names, channel_hint=hint)
    first_user = next((e for e in entries if e["role"] in {"user", "peer_inbound"}), entries[-1])
    assert summary.preview_source == first_user
    assert summary.message_count == sum(
        1
        for e in entries
        if e["role"] in {"user", "peer_inbound", "assistant", "tool_call", "tool_result"}
    )
    assert summary.created_at == entries[0]["ts"]
    assert summary.last_ts == entries[-1]["ts"]


def test_an_unchanged_transcript_is_read_once(api_client, monkeypatch):
    sessions = _sessions()
    for i in range(3):
        _write(
            sessions / f"t{i}.jsonl",
            [{"ts": f"2026-10-0{i + 1}T00:00:00Z", "role": "user", "content": f"q{i}"}],
        )

    reads: list[str] = []
    real = thread_summaries.summarize

    def counting(entries, **kw):
        reads.append(entries[0]["content"] if entries else "")
        return real(entries, **kw)

    monkeypatch.setattr(thread_summaries, "summarize", counting)

    first = api_client.get("/api/agents/aurelius/threads").json()
    assert sorted(reads) == ["q0", "q1", "q2"]
    reads.clear()
    assert api_client.get("/api/agents/aurelius/threads").json() == first
    assert reads == []

    # A transcript that grew is read again, and only that one.
    with (sessions / "t1.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps({"ts": "2026-10-09T00:00:00Z", "role": "assistant", "content": "a1"}) + "\n"
        )
    rows = api_client.get("/api/agents/aurelius/threads").json()
    assert reads == ["q1"]
    assert rows[0]["id"] == "t1"
    assert rows[0]["updated_at"] == "2026-10-09T00:00:00Z"
    assert rows[0]["message_count"] == 2


def test_a_deleted_transcript_leaves_the_list_and_the_cache(api_client):
    sessions = _sessions()
    _write(sessions / "gone.jsonl", [{"ts": "t0", "role": "user", "content": "bye"}])
    assert [t["id"] for t in api_client.get("/api/agents/aurelius/threads").json()] == ["gone"]
    (sessions / "gone.jsonl").unlink()
    assert api_client.get("/api/agents/aurelius/threads").json() == []
    assert sessions / "gone.jsonl" not in thread_summaries._cache


def test_hidden_rows_stay_out_of_the_cached_row(api_client):
    sessions = _sessions()
    _write(
        sessions / "greet.jsonl",
        [
            {
                "ts": "t0",
                "role": "user",
                "content": "INTERNAL greeting instruction",
                "hidden": True,
            },
            {"ts": "t1", "role": "assistant", "content": "Hello there"},
        ],
    )
    row = api_client.get("/api/agents/aurelius/threads").json()[0]
    assert "INTERNAL" not in row["title"] + row["preview"]
    assert row["message_count"] == 1


def test_an_unreadable_transcript_sits_the_list_out(api_client):
    sessions = _sessions()
    _write(sessions / "fine.jsonl", [{"ts": "t0", "role": "user", "content": "ok"}])
    # A directory where a transcript should be: the read raises, the list does not.
    (sessions / "broken.jsonl").mkdir()
    assert [t["id"] for t in api_client.get("/api/agents/aurelius/threads").json()] == ["fine"]


def _count_reads(monkeypatch):
    reads: list[str] = []
    real = thread_summaries.summarize

    def counting(entries, **kw):
        reads.append(entries[0]["content"] if entries else "")
        return real(entries, **kw)

    monkeypatch.setattr(thread_summaries, "summarize", counting)
    return reads


def test_a_restart_reads_no_transcript_the_store_already_holds(api_client, monkeypatch):
    sessions = _sessions()
    for i in range(3):
        _write(
            sessions / f"t{i}.jsonl",
            [
                {"ts": f"2026-10-0{i + 1}T00:00:00Z", "role": "user", "content": f"q{i}"},
                {"ts": f"2026-10-0{i + 1}T00:01:00Z", "role": "assistant", "content": "a" * 9000},
            ],
        )
    reads = _count_reads(monkeypatch)
    first = api_client.get("/api/agents/aurelius/threads").json()
    assert sorted(reads) == ["q0", "q1", "q2"]
    assert thread_summaries.store_path().exists()

    # A restart: the in-process tier is gone, the file on disk is not.
    thread_summaries.clear()
    reads.clear()
    assert api_client.get("/api/agents/aurelius/threads").json() == first
    assert reads == []

    # A transcript that grew while the process was down is the one read again,
    # even though the store holds a row for it.
    thread_summaries.clear()
    with (sessions / "t2.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps({"ts": "2026-10-09T00:00:00Z", "role": "user", "content": "q2b"}) + "\n"
        )
    rows = api_client.get("/api/agents/aurelius/threads").json()
    assert reads == ["q2"]
    assert rows[0]["id"] == "t2"
    assert rows[0]["message_count"] == 3


def test_a_stored_summary_labels_the_row_as_the_fresh_one_does():
    for thread_id, entries in SHAPES.items():
        fresh = thread_summaries.summarize(entries, supersedes="old")
        back = thread_summaries.decode_summary(thread_summaries.encode_summary(fresh))
        assert back is not None
        assert back.preview_source == fresh.preview_source
        assert (back.message_count, back.created_at, back.last_ts) == (
            fresh.message_count,
            fresh.created_at,
            fresh.last_ts,
        )
        assert (back.model, back.reasoning_effort, back.supersedes) == (
            "olano:max",
            "high",
            "old",
        ) or (thread_id != "notice-first")
        assert derive_label(thread_id, back.head) == derive_label(thread_id, entries)
    assert thread_summaries.decode_summary(
        thread_summaries.encode_summary(thread_summaries.EMPTY)
    ) == (thread_summaries.EMPTY)
    assert thread_summaries.decode_summary("{not json") is None
    assert thread_summaries.decode_summary('{"head": []}') is None


def test_a_pasted_document_is_clipped_in_the_store_but_still_titles_the_row():
    long = "[Autonomous scheduled task] " + "x" * 20000
    entries = [
        {"ts": "t0", "role": "user", "content": long},
        {"ts": "t1", "role": "assistant", "content": "ok"},
    ]
    fresh = thread_summaries.summarize(entries)
    data = thread_summaries.encode_summary(fresh)
    assert len(data) < 2 * thread_summaries.STORED_FIELD_CHARS
    back = thread_summaries.decode_summary(data)
    assert back is not None
    assert derive_label("t", back.head) == derive_label("t", entries)


def test_a_deleted_transcript_leaves_the_store_too(api_client):
    sessions = _sessions()
    _write(sessions / "gone.jsonl", [{"ts": "t0", "role": "user", "content": "bye"}])
    _write(sessions / "kept.jsonl", [{"ts": "t1", "role": "user", "content": "hi"}])
    api_client.get("/api/agents/aurelius/threads")
    store = thread_summaries._store()
    assert store is not None
    assert {p.name for p in store.paths_in(sessions)} == {"gone.jsonl", "kept.jsonl"}
    (sessions / "gone.jsonl").unlink()
    api_client.get("/api/agents/aurelius/threads")
    assert {p.name for p in store.paths_in(sessions)} == {"kept.jsonl"}


def test_the_boot_warm_reads_everything_once_and_the_list_then_reads_nothing(
    api_client, monkeypatch
):
    sessions = _sessions()
    for i in range(4):
        _write(
            sessions / f"t{i}.jsonl",
            [{"ts": f"2026-10-0{i + 1}T00:00:00Z", "role": "user", "content": f"q{i}"}],
        )
    _write(sessions / "scrap.jsonl", [])
    assert thread_summaries.warm_directory(sessions) == 5
    assert thread_summaries.warm_directory(sessions) == 0
    thread_summaries.clear()
    reads = _count_reads(monkeypatch)
    rows = api_client.get("/api/agents/aurelius/threads").json()
    assert [t["id"] for t in rows] == ["t3", "t2", "t1", "t0"]
    assert reads == []
    assert thread_summaries.warm_directory(sessions / "missing") == 0


def test_an_unusable_store_costs_the_disk_tier_and_nothing_else(api_client, monkeypatch):
    sessions = _sessions()
    _write(sessions / "t0.jsonl", [{"ts": "t0", "role": "user", "content": "q0"}])
    thread_summaries.clear()
    # A directory where the database file should be: the open fails.
    thread_summaries.store_path().mkdir(parents=True, exist_ok=True)
    assert thread_summaries._store() is None
    assert [t["id"] for t in api_client.get("/api/agents/aurelius/threads").json()] == ["t0"]
    thread_summaries.clear()
    monkeypatch.setenv("LIONCLAW_THREAD_SUMMARY_DB", "0")
    assert thread_summaries._store() is None
    assert [t["id"] for t in api_client.get("/api/agents/aurelius/threads").json()] == ["t0"]


async def test_the_server_warm_covers_every_agent(api_client, monkeypatch):
    from olano.runtime.server import RuntimeServer

    sessions = _sessions()
    _write(sessions / "a.jsonl", [{"ts": "t0", "role": "user", "content": "qa"}])
    other = sessions.parent / "navi"
    other.mkdir()
    _write(other / "b.jsonl", [{"ts": "t0", "role": "user", "content": "qb"}])
    # A stray file in the sessions root (the checkpoint db lives there) is not an agent.
    (sessions.parent / "threads.db").write_bytes(b"")

    await RuntimeServer(agent_names=[])._warm_thread_summaries()

    thread_summaries.clear()
    reads = _count_reads(monkeypatch)
    assert [t["id"] for t in api_client.get("/api/agents/aurelius/threads").json()] == ["a"]
    assert [t["id"] for t in api_client.get("/api/agents/navi/threads").json()] == ["b"]
    assert reads == []


def test_eviction_keeps_the_directory_index_in_step(tmp_path, monkeypatch):
    monkeypatch.setattr(thread_summaries, "MAX_CACHED", 2)
    paths = [tmp_path / f"t{i}.jsonl" for i in range(3)]
    for p in paths:
        p.write_text("{}\n")
        thread_summaries.cached_summary(p, lambda _p: thread_summaries.EMPTY)
    assert paths[0] not in thread_summaries._cache
    assert thread_summaries._by_dir[tmp_path] == {paths[1], paths[2]}
    thread_summaries.forget_missing(tmp_path, [paths[2]])
    assert set(thread_summaries._cache) == {paths[2]}
