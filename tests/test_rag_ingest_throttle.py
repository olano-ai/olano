"""The RAG ingest is a background warm-up and must behave like one.

Nobody waits on it, so it must not spend the whole box: the encoder is capped to
a small thread budget and the ingest rests between embed batches to hold a duty
cycle. Prod symptom this guards (2026-07-30, 2-core CVM): a boot warm-up pinned
both cores for minutes, and `/api/status` — which the dashboard polls — took
2.1s to answer while it ran.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from olano.memory.embeddings import embedding_threads
from olano.memory.rag_memory import RAGMemory, _pace, ingest_duty_cycle

# ── Settings resolution ───────────────────────────────────────────────────────


def test_duty_cycle_defaults_to_half(monkeypatch):
    monkeypatch.delenv("LIONCLAW_RAG_INGEST_DUTY", raising=False)
    assert ingest_duty_cycle() == 0.5


@pytest.mark.parametrize("raw,expected", [("1.0", 1.0), ("0.25", 0.25), ("0.9", 0.9)])
def test_duty_cycle_honors_env(monkeypatch, raw, expected):
    monkeypatch.setenv("LIONCLAW_RAG_INGEST_DUTY", raw)
    assert ingest_duty_cycle() == expected


@pytest.mark.parametrize("raw", ["0", "-1", "1.5", "banana", ""])
def test_duty_cycle_rejects_nonsense(monkeypatch, raw):
    """A bad value must fall back to the default, never disable the throttle."""
    monkeypatch.setenv("LIONCLAW_RAG_INGEST_DUTY", raw)
    assert ingest_duty_cycle() == 0.5


def test_embedding_threads_defaults_to_one(monkeypatch):
    """The second thread buys ~6% wall-clock and costs ~80% of a core."""
    monkeypatch.delenv("LIONCLAW_EMBEDDING_THREADS", raising=False)
    assert embedding_threads() == 1


@pytest.mark.parametrize("raw,expected", [("4", 4), ("0", 1), ("-2", 1), ("nope", 1)])
def test_embedding_threads_env(monkeypatch, raw, expected):
    monkeypatch.setenv("LIONCLAW_EMBEDDING_THREADS", raw)
    assert embedding_threads() == expected


# ── Pacing ────────────────────────────────────────────────────────────────────


def test_pace_rests_in_proportion_to_work(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("olano.memory.rag_memory.time.sleep", slept.append)
    monkeypatch.setenv("LIONCLAW_RAG_INGEST_DUTY", "0.5")

    _pace(2.0)  # 50% duty → an equal rest

    assert slept == [2.0]


def test_pace_is_a_noop_at_full_duty(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("olano.memory.rag_memory.time.sleep", slept.append)
    monkeypatch.setenv("LIONCLAW_RAG_INGEST_DUTY", "1.0")

    _pace(5.0)

    assert slept == []


def test_pace_skips_negligible_naps(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("olano.memory.rag_memory.time.sleep", slept.append)
    monkeypatch.setenv("LIONCLAW_RAG_INGEST_DUTY", "0.5")

    _pace(0.001)  # a 1ms rest isn't worth a syscall

    assert slept == []


# ── Upsert: batched + paced ───────────────────────────────────────────────────


def _chunks(n: int) -> list:
    from langchain_core.documents import Document

    return [Document(page_content=f"chunk {i}", metadata={"source": "f.md"}) for i in range(n)]


def _fake_clock(monkeypatch, tick: float = 1.0) -> None:
    """Make every monotonic() call advance by `tick`, so one embed batch costs
    exactly `tick` seconds of measured work (a MagicMock store returns instantly)."""
    now = iter(t * tick for t in range(1, 10_000))
    monkeypatch.setattr("olano.memory.rag_memory.time.monotonic", lambda: next(now))


def test_upsert_embeds_in_batches_and_rests_between_them(tmp_path, monkeypatch):
    store = MagicMock()
    store.get.return_value = {"ids": []}
    rag = RAGMemory(persist_dir=tmp_path / "rag")
    monkeypatch.setattr(rag, "_get_store", lambda collection=None: store)
    monkeypatch.setattr("olano.memory.embeddings.embedding_batch_size", lambda: 16)
    monkeypatch.setenv("LIONCLAW_RAG_INGEST_DUTY", "0.5")
    slept: list[float] = []
    monkeypatch.setattr("olano.memory.rag_memory.time.sleep", slept.append)
    _fake_clock(monkeypatch)

    rag._upsert_chunks(_chunks(40), "f.md", "workspace")

    # 40 chunks at 16 per pass → 16 / 16 / 8, with a rest after all but the last.
    assert [len(call.args[0]) for call in store.add_documents.call_args_list] == [16, 16, 8]
    # 1s of work per batch at 50% duty → a 1s rest after each of the first two.
    assert slept == [1.0, 1.0]


def test_upsert_does_not_rest_after_the_final_batch(tmp_path, monkeypatch):
    store = MagicMock()
    store.get.return_value = {"ids": []}
    rag = RAGMemory(persist_dir=tmp_path / "rag")
    monkeypatch.setattr(rag, "_get_store", lambda collection=None: store)
    monkeypatch.setattr("olano.memory.embeddings.embedding_batch_size", lambda: 16)
    monkeypatch.setenv("LIONCLAW_RAG_INGEST_DUTY", "0.5")
    slept: list[float] = []
    monkeypatch.setattr("olano.memory.rag_memory.time.sleep", slept.append)
    _fake_clock(monkeypatch)

    rag._upsert_chunks(_chunks(10), "f.md", "workspace")

    assert store.add_documents.call_count == 1
    assert slept == []


def test_upsert_replaces_previous_chunks_for_the_source(tmp_path, monkeypatch):
    """Batching must not have broken the delete-then-add contract."""
    store = MagicMock()
    store.get.return_value = {"ids": ["old-1", "old-2"]}
    rag = RAGMemory(persist_dir=tmp_path / "rag")
    monkeypatch.setattr(rag, "_get_store", lambda collection=None: store)
    monkeypatch.setenv("LIONCLAW_RAG_INGEST_DUTY", "1.0")

    rag._upsert_chunks(_chunks(3), "f.md", "workspace")

    store.delete.assert_called_once_with(ids=["old-1", "old-2"])
    # Batch-size agnostic: the contract is "old chunks gone, every new chunk
    # added, in that order" — how many add_documents calls that takes is the
    # batch size's business (the default is 1, so here it is three).
    added = [doc for call in store.add_documents.call_args_list for doc in call.args[0]]
    assert added == _chunks(3)
    order = [name for name, _args, _kwargs in store.method_calls]
    assert order.index("delete") < order.index("add_documents")


def test_background_gate_is_shared_by_boot_and_watchers():
    """One semaphore, or a watcher event can embed alongside the boot warm-up."""
    from olano.core.builder import _rag_ingest_gate
    from olano.memory.rag_memory import BACKGROUND_INGEST_GATE

    assert _rag_ingest_gate() is BACKGROUND_INGEST_GATE
