"""Semantic layer over the issue store — hybrid search + duplicate detection.

A sidecar by design: embeddings live in their own table keyed by issue id
(``issue_embeddings``), the store stays plain SQL, and nothing here is part
of the Phase-2 backend-swap contract — a Gitea-backed store would keep this
exact module as the olano-side index.

Mechanics, sized to this corpus (thousands of short rows, not millions):

* Vectors come from the shared embedding runtime
  (:mod:`olano.memory.embeddings` — fastembed/ONNX, local, no keys) and
  are stored as float32 BLOBs. Similarity is brute-force numpy cosine over
  the whole table — sub-millisecond at this scale, and it avoids both a
  Chroma dependency and the ``sqlite-vec`` loadable-extension gotcha
  (stdlib ``sqlite3`` on some macOS builds lacks ``enable_load_extension``).
* An issue's embedding text is its **title + body** only. Comments drift
  off-topic and are already covered by the FTS5 keyword index.
* Indexing is never on a write's critical path: mutations call
  :func:`notify_index`, a daemon thread drains the missing-vector worklist
  (house pattern: module lock + inflight flag + ``os.nice``). A failed or
  unavailable embedder degrades every entry point to keyword-only.
* :func:`hybrid_ranked_ids` fuses FTS5 (BM25) and vector rankings with
  Reciprocal Rank Fusion — the same recipe as
  ``memory/knowledge_vault.hybrid_search``.
* :func:`similar_issues` powers duplicate detection: ``issue_create`` warns
  the filing agent when an open issue scores above
  :data:`DUPLICATE_THRESHOLD`, and the dashboard's new-issue modal shows the
  same matches as a hint.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Cosine similarity (L2-normalized vectors → dot product) above which an open
# issue is surfaced as a probable duplicate at filing time. bge-small scores
# unrelated short texts ~0.3-0.5 and near-paraphrases ~0.7+; 0.62 favors
# catching rephrasings over blocking genuinely new issues.
DUPLICATE_THRESHOLD = 0.62

_INDEX_LOCK = threading.Lock()
_INDEX_INFLIGHT = False


def semantic_available() -> bool:
    """True when an embedding backend is importable (never loads the model)."""
    import importlib.util

    return bool(
        importlib.util.find_spec("fastembed") or importlib.util.find_spec("sentence_transformers")
    )


def _model_name() -> str:
    try:
        from olano.memory import embeddings

        return str(getattr(embeddings, "_MODEL_NAME", "unknown"))
    except Exception:
        return "unknown"


def _embed_texts(texts: list[str]) -> np.ndarray | None:
    """L2-normalized float32 vectors, or None when embedding is unavailable.

    Single seam to the embedding runtime — tests monkeypatch this to a
    deterministic stub so no model is ever downloaded in CI.
    """
    try:
        from olano.memory.embeddings import embed_texts

        return np.asarray(embed_texts(texts), dtype=np.float32)
    except Exception:
        logger.debug("issue embedding unavailable", exc_info=True)
        return None


def _issue_text(issue: dict[str, Any]) -> str:
    body = str(issue.get("body") or "")[:2000]
    return f"{issue.get('title', '')}\n{body}".strip()


# ── indexing ──────────────────────────────────────────────────────────────


def index_pending(limit: int = 500) -> int:
    """Embed every issue missing a current-model vector. Returns count indexed.

    Synchronous — callers wanting the background path use notify_index().
    """
    from olano.issues.store import get_issue_store

    store = get_issue_store()
    model = _model_name()
    ids = store.ids_missing_vector(model, limit=limit)
    if not ids:
        return 0
    issues = [store.get(issue_id) for issue_id in ids]
    pairs = [
        (issue_id, issue) for issue_id, issue in zip(ids, issues, strict=False) if issue is not None
    ]
    vectors = _embed_texts([_issue_text(issue) for _, issue in pairs]) if pairs else None
    if vectors is None:
        return 0
    for (issue_id, _), vector in zip(pairs, vectors, strict=False):
        store.set_vector(issue_id, model, np.asarray(vector, dtype=np.float32).tobytes())
    return len(pairs)


def notify_index(issue_id: int | None = None) -> None:
    """Queue a background drain of the missing-vector worklist. Never raises.

    ``LIONCLAW_SYNC_ISSUE_EMBED=1`` runs inline (tests, one-shot CLIs). The
    worklist covers every missing id, so the ``issue_id`` hint needs no
    per-issue bookkeeping and a crashed drain retries on the next notify.
    """
    del issue_id  # the drain covers all missing rows
    if os.environ.get("LIONCLAW_SYNC_ISSUE_EMBED"):
        try:
            index_pending()
        except Exception:
            logger.debug("sync issue embed failed", exc_info=True)
        return
    if not semantic_available():
        return
    global _INDEX_INFLIGHT
    with _INDEX_LOCK:
        if _INDEX_INFLIGHT:
            return
        _INDEX_INFLIGHT = True

    def _run() -> None:
        global _INDEX_INFLIGHT
        try:
            os.nice(10)
        except (AttributeError, OSError):
            pass
        try:
            index_pending()
        except Exception:
            logger.debug("background issue embed failed", exc_info=True)
        finally:
            with _INDEX_LOCK:
                _INDEX_INFLIGHT = False

    threading.Thread(target=_run, name="issue-embed", daemon=True).start()


# ── search ────────────────────────────────────────────────────────────────


def _vector_ranked(query: str, limit: int) -> list[tuple[int, float]] | None:
    """(issue_id, cosine) best-first over the whole sidecar, or None when
    embeddings are unavailable/empty."""
    from olano.issues.store import get_issue_store

    rows = get_issue_store().vector_rows(_model_name())
    if not rows:
        return None
    query_vec = _embed_texts([query])
    if query_vec is None:
        return None
    matrix = np.stack([np.frombuffer(blob, dtype=np.float32) for _, blob in rows])
    scores = matrix @ query_vec[0]
    order = np.argsort(-scores)[: max(1, int(limit))]
    return [(rows[i][0], float(scores[i])) for i in order]


def hybrid_ranked_ids(query: str, limit: int = 50, rrf_k: int = 60) -> list[int] | None:
    """RRF-fused FTS5 + vector ranking, best first.

    Returns None when neither ranking is available (caller keeps the store's
    own keyword path); falls back to whichever single ranking exists.
    """
    from olano.issues.store import get_issue_store

    fts_ids = get_issue_store().search_ids(query, limit=limit) or []
    vector = _vector_ranked(query, limit) or []
    if not fts_ids and not vector:
        return None
    scores: dict[int, float] = {}
    for rank, issue_id in enumerate(fts_ids, start=1):
        scores[issue_id] = scores.get(issue_id, 0.0) + 1.0 / (rrf_k + rank)
    for rank, (issue_id, _) in enumerate(vector, start=1):
        scores[issue_id] = scores.get(issue_id, 0.0) + 1.0 / (rrf_k + rank)
    return sorted(scores, key=lambda issue_id: scores[issue_id], reverse=True)[: max(1, int(limit))]


def similar_issues(
    text: str,
    *,
    limit: int = 5,
    exclude_id: int | None = None,
    state: str = "open",
) -> list[dict[str, Any]]:
    """Semantically closest issues to ``text``, each with a ``score`` key.

    Empty when embeddings are unavailable — duplicate detection silently
    degrades rather than blocking anything.
    """
    from olano.issues.store import get_issue_store

    ranked = _vector_ranked(text, limit * 4)
    if not ranked:
        return []
    store = get_issue_store()
    out: list[dict[str, Any]] = []
    for issue_id, score in ranked:
        if exclude_id is not None and issue_id == exclude_id:
            continue
        issue = store.get(issue_id)
        if issue is None or (state and issue["state"] != state):
            continue
        issue["score"] = round(score, 3)
        out.append(issue)
        if len(out) >= limit:
            break
    return out


def duplicate_candidates(title: str, body: str = "") -> list[dict[str, Any]]:
    """Open issues similar enough to block/warn at filing time."""
    text = f"{title}\n{body[:2000]}".strip()
    if not text:
        return []
    return [
        issue
        for issue in similar_issues(text, limit=3, state="open")
        if issue["score"] >= DUPLICATE_THRESHOLD
    ]
