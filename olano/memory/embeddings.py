"""
Shared embedding helpers.

Backend-aware, single model per process. The **default backend is fastembed**
(ONNX runtime, no torch, no HF token) with ``BAAI/bge-small-en-v1.5``. Override
with the ``LIONCLAW_EMBEDDING_MODEL`` env var:

    (unset)                          → fastembed, BAAI/bge-small-en-v1.5  (default)
    fastembed:<hf-repo>              → a different fastembed model
    hf   /  hf:all-MiniLM-L6-v2      → legacy sentence-transformers backend
    sentence-transformers:<model>    → same as hf:<model>

Both the LangChain ``Embeddings`` object (for Chroma) and the raw
``embed_text`` / ``embed_texts`` helpers share ONE loaded model per process.

Usage:
    from olano.memory.embeddings import get_langchain_embeddings, embed_text

    emb = get_langchain_embeddings()           # reused across all RAG stores
    vec = embed_text("some text")              # numpy float32 (L2-normalized)
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import logging
import os
import re
import threading
from collections.abc import Callable
from typing import Any, cast

import numpy as np

logger = logging.getLogger(__name__)

# ── Backend defaults ─────────────────────────────────────────────────────────
_DEFAULT_BACKEND = "fastembed"
_DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
_DEFAULT_HF_MODEL = "all-MiniLM-L6-v2"
# Back-compat alias (previously the module-level constant).
_MODEL_NAME = _DEFAULT_HF_MODEL

_HF_ALIASES = {"hf", "huggingface", "sentence-transformers", "sentencetransformers", "st"}
_FE_ALIASES = {"fastembed", "fe", "qdrant"}

# How many chunks are fed to the model per forward pass.
#
# This is a MEMORY control, not a throughput one. The encoder's peak RSS scales
# ~linearly with batch size (transformer activations are batch x seq_len x
# hidden, held for every layer), and that peak is NOT reclaimable: it is
# retained inside onnxruntime's arena allocator, so gc + malloc_trim give back
# almost none of it and the process keeps the high-water mark for its lifetime.
# A single boot ingest could therefore add ~2 GB of permanent RSS — the direct
# cause of a 7.3 GB host filling its swap and being SIGKILLed. The first pass
# at this measured 16/32/64 on a 2-core box (207 x 1500-char chunks):
#
#     batch  16 →   464 MB over baseline, 132s
#     batch  32 →   983 MB over baseline, 129s
#     batch  64 →  1994 MB over baseline, 128s
#     unbatched → ~2265 MB over baseline   (fastembed's default is 256)
#
# and stopped at 16 as "conservative". It was not: the curve keeps going. The
# same sweep continued downward (4-core box, threads=1, 200 x 1500-char chunks
# through the raw encoder; scripts/bench_agent_memory.py has the method):
#
#     batch  16 →   508 MB over baseline, 54.8s
#     batch   8 →   253 MB,               47.4s
#     batch   4 →   152 MB,               44.0s
#     batch   2 →    75 MB,               42.4s
#     batch   1 →    36 MB,               42.1s
#
# and on the real path — RAGMemory.ingest_file → Chroma → encoder, on the real
# 272 KB USER_GUIDE.md every scaffolded workspace carries (234 chunks):
#
#     batch  16 →   577 MB over baseline, 137s
#     batch   1 →    95 MB over baseline,  80s
#
# Below 16, smaller is *faster*, not just lighter: with threads=1 (the default,
# see _DEFAULT_EMBED_THREADS) there is no intra-op parallelism for a wider
# batch to amortize, so a bigger batch only makes each allocation larger and
# the cache locality worse. Threads are the speed lever; batch is a pure
# memory cost. Query latency is unchanged (~5 ms p50 at every batch size).
#
# 1 is therefore the default: ~480 MB less permanent RSS per ingesting box —
# the single largest allocation in the process — for a faster ingest. The
# knob stays for a box with threads raised and RAM to spare, via
# LIONCLAW_EMBEDDING_BATCH_SIZE; the growth is ~32 MB per unit of batch at
# 1500-char chunks.
_DEFAULT_EMBED_BATCH_SIZE = 1


def embedding_batch_size() -> int:
    """Resolve the encoder batch size (``LIONCLAW_EMBEDDING_BATCH_SIZE``)."""
    raw = (os.environ.get("LIONCLAW_EMBEDDING_BATCH_SIZE") or "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning("Ignoring non-numeric LIONCLAW_EMBEDDING_BATCH_SIZE=%r", raw)
    return _DEFAULT_EMBED_BATCH_SIZE


# How many CPU threads the encoder may use per forward pass.
#
# This is a LATENCY-OF-EVERYTHING-ELSE control. Left unset, onnxruntime sizes
# its intra-op pool from the core count and an ingest saturates the whole box —
# measured on the 2-core prod CVM, embedding 96 x 1500-char chunks at batch 16:
#
#     threads=auto → 26.0s wall, 47.0s CPU  (1.80 cores busy)
#     threads=1    → 27.6s wall, 27.4s CPU  (0.99 cores busy)
#
# The second core buys 6% wall-clock and costs 80% of a core: this workload is
# memory-bandwidth bound, not compute bound. For a background warm-up that
# nobody is waiting on, that is a bad trade — those cores are what the event
# loop, the channel pollers, and live agent turns need. Peak RSS is unchanged
# either way (~566 MB at batch 16); threads only move CPU.
#
# 1 is the default everywhere. Raise it on a box with cores to spare, or when
# you genuinely want an ingest to finish as fast as possible, via
# LIONCLAW_EMBEDDING_THREADS.
_DEFAULT_EMBED_THREADS = 1


def embedding_threads() -> int:
    """Resolve the encoder's CPU thread budget (``LIONCLAW_EMBEDDING_THREADS``)."""
    raw = (os.environ.get("LIONCLAW_EMBEDDING_THREADS") or "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning("Ignoring non-numeric LIONCLAW_EMBEDDING_THREADS=%r", raw)
    return _DEFAULT_EMBED_THREADS


# ── Singletons (loaded once per process) ─────────────────────────────────────
_lc_embeddings: Any = None  # LangChain Embeddings object (wraps the encoder)
_encoder: _RawEncoder | None = None  # singleton (shared by LC + raw paths)
# The boot-time ingest runs in a background thread (see builder.py) and a process
# may build several RAG-enabled agents at once, so the singleton builds below can
# race. Serialize them so each singleton is built exactly once.
# Must be an RLock, not a Lock: get_langchain_embeddings() acquires it and then
# calls _get_encoder(), which acquires it again — with a plain Lock the first
# caller self-deadlocks while holding it, freezing every embeddings user in the
# process (including the runtime event loop).
_load_lock = threading.RLock()


def _load_offline_first(loader: Callable[[], Any], label: str) -> Any:
    """Load a HuggingFace/fastembed model from the local cache without touching
    the network.

    ``HF_HUB_OFFLINE=1`` makes ``huggingface_hub`` skip its "is there a newer
    revision?" round-trip — and the noisy *"sending unauthenticated requests to
    the HF Hub"* warning — when the model is already cached (which it is after the
    first boot). On a cold cache the offline load raises, so we restore the prior
    setting and retry online exactly once to download it. No HF_TOKEN is ever
    required for these public models; offline mode just avoids the per-boot probe.
    """
    sentinel = object()
    prev = os.environ.get("HF_HUB_OFFLINE", sentinel)

    def _restore() -> None:
        if prev is sentinel:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = prev  # type: ignore[assignment]

    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        return loader()
    except Exception as exc:
        logger.debug("Offline load of %s failed (%s) — downloading from HF Hub once.", label, exc)
        _restore()  # let the download reach the network
        return loader()
    finally:
        _restore()


@contextlib.contextmanager
def _quiet_embedding_load() -> Any:
    """Suppress noisy stdout/stderr emitted by transformers on model load."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    previous_verbosity = None
    transformers_logging: Any = None
    try:
        # Import under a fresh name, then bind — importing directly as
        # `transformers_logging` would redefine the pre-declared name (mypy no-redef).
        from transformers.utils import logging as _tlog

        transformers_logging = _tlog
        previous_verbosity = transformers_logging.get_verbosity()
        transformers_logging.set_verbosity_error()
    except Exception:
        transformers_logging = None

    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            yield
    finally:
        if transformers_logging is not None and previous_verbosity is not None:
            try:
                transformers_logging.set_verbosity(previous_verbosity)
            except Exception:
                pass


def _detect_device() -> str:
    """Return the best available torch device string (sentence-transformers path)."""
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"


# ── Backend resolution ───────────────────────────────────────────────────────
def _fastembed_available() -> bool:
    # find_spec (not __import__) so a broken optional import can't crash the check.
    return importlib.util.find_spec("fastembed") is not None


def _parse_spec() -> tuple[str, str]:
    """Parse ``LIONCLAW_EMBEDDING_MODEL`` into ``(backend, model)`` without any
    availability checks."""
    raw = os.environ.get("LIONCLAW_EMBEDDING_MODEL", "").strip()
    if not raw:
        return _DEFAULT_BACKEND, _DEFAULT_FASTEMBED_MODEL
    backend, sep, model = raw.partition(":")
    backend = backend.strip().lower()
    model = model.strip()
    if backend in _HF_ALIASES:
        return "hf", model or _DEFAULT_HF_MODEL
    if backend in _FE_ALIASES:
        return "fastembed", model or _DEFAULT_FASTEMBED_MODEL
    # No recognized backend prefix — treat the whole value as a fastembed model id.
    logger.debug(
        "Embedding spec %r has no known backend prefix — treating as fastembed model.", raw
    )
    return "fastembed", raw


def _resolve_spec() -> tuple[str, str]:
    """Resolve the effective ``(backend, model)``, downgrading fastembed → hf when
    fastembed isn't installed so an incomplete install still works. Both the
    encoder and the store fingerprint go through here, so they always agree."""
    backend, model = _parse_spec()
    if backend == "fastembed" and not _fastembed_available():
        logger.warning(
            "fastembed not installed — falling back to sentence-transformers (%s). "
            "Install it for faster, torch-free embeddings: uv add fastembed",
            _DEFAULT_HF_MODEL,
        )
        return "hf", _DEFAULT_HF_MODEL
    return backend, model


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48] or "model"


def embedding_fingerprint(explicit_model: str | None = None) -> str:
    """A short, stable slug identifying the active embedding model.

    Vector stores are namespaced by this so switching backends never mixes
    incompatible vectors in one collection — bge-small and MiniLM are BOTH
    384-dim, so a mismatch wouldn't raise; it would silently return garbage.
    ``explicit_model`` (a RAGMemory ``embedding_model`` like ``openai:...``)
    overrides the module default when set.
    """
    if explicit_model and explicit_model.strip().lower().startswith("openai:"):
        return "openai-" + _slug(explicit_model.split(":", 1)[1])
    backend, model = _resolve_spec()
    return f"{backend}-{_slug(model)}"


# ── Raw encoder (uniform .encode over the selected backend) ───────────────────
class _RawEncoder:
    """Loads the selected backend lazily and returns L2-normalized float32
    vectors from a uniform ``encode(list[str]) -> (N, D) ndarray`` method."""

    def __init__(self, backend: str, model: str) -> None:
        self.backend = backend
        self.model = model
        self._impl: Any = None
        # _ensure() runs lazily on first encode(), typically from several
        # boot-ingest threads at once — serialize so the model loads once.
        self._ensure_lock = threading.Lock()

    def _ensure(self) -> None:
        if self._impl is not None:
            return
        with self._ensure_lock:
            if self._impl is not None:
                return
            if self.backend == "fastembed":
                try:
                    from fastembed import TextEmbedding
                except ImportError as exc:
                    raise ImportError(
                        "fastembed not installed. Run: uv add fastembed "
                        "(or set LIONCLAW_EMBEDDING_MODEL=hf to use sentence-transformers)."
                    ) from exc
                threads = embedding_threads()
                logger.debug("Loading fastembed model '%s' (threads=%d)", self.model, threads)
                self._impl = _load_offline_first(
                    lambda: TextEmbedding(model_name=self.model, threads=threads), self.model
                )
            else:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as exc:
                    raise ImportError(
                        "The legacy 'hf' embedding backend needs sentence-transformers + torch, "
                        "which are not installed by default. Run: uv sync --group hf-embeddings "
                        "(or: uv add sentence-transformers) — or unset LIONCLAW_EMBEDDING_MODEL "
                        "to use the default fastembed backend."
                    ) from exc
                logger.debug(
                    "Loading sentence-transformers model '%s' on device '%s'",
                    self.model,
                    _detect_device(),
                )
                # torch sizes its intra-op pool from the core count too, so the
                # legacy backend needs the same cap (see _DEFAULT_EMBED_THREADS).
                try:
                    import torch

                    torch.set_num_threads(embedding_threads())
                except Exception:  # pragma: no cover - torch is optional
                    logger.debug("Could not cap torch thread count", exc_info=True)
                with _quiet_embedding_load():
                    self._impl = _load_offline_first(
                        lambda: SentenceTransformer(self.model), self.model
                    )
            logger.debug("Embedding model ready (%s:%s)", self.backend, self.model)

    def encode(self, texts: list[str]) -> np.ndarray:
        self._ensure()
        if not texts:
            return np.empty((0, 0), dtype=np.float32)
        # Bounded batches keep peak RSS flat regardless of how many chunks a
        # caller hands us. Without this, ingesting one large document embeds
        # every chunk in a single forward pass and permanently inflates the
        # process (see _DEFAULT_EMBED_BATCH_SIZE).
        batch_size = embedding_batch_size()
        if self.backend == "fastembed":
            vecs = np.asarray(
                list(self._impl.embed(list(texts), batch_size=batch_size)), dtype=np.float32
            )
        else:
            vecs = np.asarray(
                self._impl.encode(
                    list(texts),
                    batch_size=batch_size,
                    convert_to_numpy=True,
                    normalize_embeddings=True,
                ),
                dtype=np.float32,
            )
        # L2-normalize so the dot-product cosine helpers below and Chroma's cosine
        # space are correct regardless of whether the backend normalized already.
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return cast(np.ndarray, (vecs / norms).astype(np.float32))


def _get_encoder() -> _RawEncoder:
    """Return the shared raw-encoder singleton (loads the model once per process)."""
    global _encoder
    if _encoder is None:
        with _load_lock:
            if _encoder is None:
                backend, model = _resolve_spec()
                _encoder = _RawEncoder(backend, model)
    assert _encoder is not None
    return _encoder


def get_langchain_embeddings(model_name: str | None = None) -> Any:
    """
    Return a cached LangChain ``Embeddings`` object backed by the active encoder.

    The model is loaded exactly once per process and shared with
    ``embed_text`` / ``embed_texts`` so Chroma and the raw helpers never load two
    copies. The backend/model come from ``LIONCLAW_EMBEDDING_MODEL`` (see module
    docstring); the ``model_name`` argument is accepted for backwards
    compatibility but ignored — set the env var instead.
    """
    global _lc_embeddings
    if _lc_embeddings is None:
        with _load_lock:
            if _lc_embeddings is None:  # double-checked: another thread may have built it
                from langchain_core.embeddings import Embeddings

                encoder = _get_encoder()

                class _EncoderEmbeddings(Embeddings):
                    """Adapts the shared _RawEncoder to the LangChain interface."""

                    def embed_documents(self, texts: list[str]) -> list[list[float]]:
                        if not texts:
                            return []
                        return [[float(x) for x in row] for row in encoder.encode(list(texts))]

                    def embed_query(self, text: str) -> list[float]:
                        return [float(x) for x in encoder.encode([text])[0]]

                _lc_embeddings = _EncoderEmbeddings()
    return _lc_embeddings


def embed_text(text: str) -> np.ndarray:
    """
    Embed a single string → float32 numpy vector (L2-normalized, 384-dim for the
    default model). Uses the shared encoder (loaded once).
    """
    return cast(np.ndarray, _get_encoder().encode([text])[0])


def embed_texts(texts: list[str]) -> np.ndarray:
    """Batch embed texts → (N, D) float32 array (L2-normalized)."""
    if not texts:
        return np.empty((0, 0), dtype=np.float32)
    return _get_encoder().encode(list(texts))


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two normalized vectors (dot product)."""
    return float(np.dot(a, b))


def top_k_similar(
    query_vec: np.ndarray,
    candidate_vecs: list[np.ndarray],
    k: int = 5,
) -> list[tuple[int, float]]:
    """Return indices and scores of the top-k most similar candidate vectors."""
    if not candidate_vecs:
        return []
    matrix = np.stack(candidate_vecs)
    scores = matrix @ query_vec
    top_k = min(k, len(scores))
    indices = np.argsort(scores)[::-1][:top_k]
    return [(int(i), float(scores[i])) for i in indices]
