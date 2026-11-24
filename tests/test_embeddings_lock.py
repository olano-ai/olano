"""Regression tests for the embeddings singleton locking.

get_langchain_embeddings() acquires the module load lock and then calls
_get_encoder(), which acquires it again. With a non-reentrant lock the first
caller self-deadlocked while holding it, freezing every embeddings user in
the process — including the runtime event loop (2026-07-08 outage: API,
Telegram, and scheduler all froze together).
"""

from __future__ import annotations

import threading

from olano.memory import embeddings as emb


def _reset_singletons(monkeypatch) -> None:
    monkeypatch.setattr(emb, "_encoder", None)
    monkeypatch.setattr(emb, "_lc_embeddings", None)


def test_langchain_path_first_does_not_deadlock(monkeypatch):
    """The LangChain wrapper must be buildable when no singleton exists yet."""
    _reset_singletons(monkeypatch)

    result: list = []
    t = threading.Thread(target=lambda: result.append(emb.get_langchain_embeddings()), daemon=True)
    t.start()
    t.join(timeout=15)

    assert result, "get_langchain_embeddings() deadlocked on first call"
    assert result[0] is emb.get_langchain_embeddings()  # singleton is cached


def test_concurrent_first_callers_build_one_encoder(monkeypatch):
    """Racing langchain-path and raw-path callers must converge on one encoder."""
    _reset_singletons(monkeypatch)

    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def _call(fn) -> None:
        try:
            barrier.wait(timeout=5)
            fn()
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=_call, args=(fn,), daemon=True)
        for fn in (
            emb.get_langchain_embeddings,
            emb.get_langchain_embeddings,
            emb._get_encoder,
            emb._get_encoder,
        )
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors
    assert all(not t.is_alive() for t in threads), "a caller deadlocked"
    assert emb._encoder is not None


# ── batch size (memory control) ──────────────────────────────────────────────
#
# The encoder's peak RSS scales ~linearly with batch size and that peak is
# retained by onnxruntime's arena allocator — it is never handed back. Embedding
# a whole document in one unbounded pass added ~2 GB of permanent RSS and was the
# direct cause of a 7.3 GB host exhausting its swap. Throughput on CPU is flat
# across batch sizes, so the bound is close to free; these guard that it is
# actually applied and stays tunable.


def test_embedding_batch_size_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("LIONCLAW_EMBEDDING_BATCH_SIZE", raising=False)
    assert emb.embedding_batch_size() == emb._DEFAULT_EMBED_BATCH_SIZE

    monkeypatch.setenv("LIONCLAW_EMBEDDING_BATCH_SIZE", "64")
    assert emb.embedding_batch_size() == 64

    # Garbage and non-positive values fall back rather than propagating a crash
    # (or a 0 batch) into every ingest.
    for bad in ("abc", "0", "-4", ""):
        monkeypatch.setenv("LIONCLAW_EMBEDDING_BATCH_SIZE", bad)
        assert emb.embedding_batch_size() == emb._DEFAULT_EMBED_BATCH_SIZE


def test_encoder_passes_bounded_batch_size_to_backend(monkeypatch):
    """encode() must hand the resolved batch size to the backend, not embed the
    caller's whole list in one unbounded forward pass."""
    monkeypatch.setenv("LIONCLAW_EMBEDDING_BATCH_SIZE", "8")
    seen: dict = {}

    class _FakeImpl:
        def embed(self, texts, batch_size=None):
            seen["batch_size"] = batch_size
            seen["n"] = len(texts)
            return [[1.0, 0.0] for _ in texts]

    encoder = emb._RawEncoder("fastembed", "fake-model")
    encoder._impl = _FakeImpl()  # pre-set so _ensure() never loads a real model

    vecs = encoder.encode([f"chunk {i}" for i in range(50)])

    assert seen["batch_size"] == 8, "batch size was not forwarded to the backend"
    assert seen["n"] == 50
    assert vecs.shape == (50, 2)
