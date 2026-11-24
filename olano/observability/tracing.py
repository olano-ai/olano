"""Langfuse clients and the per-run tracing API the runtime calls.

The only module that imports ``langfuse``, and only inside functions, so a
process with tracing off never loads it.

One traced run is an :class:`ActiveTrace`: a root span (non-current, so no
OpenTelemetry context is attached across the runner's async generator), a
LangChain ``CallbackHandler`` parented to it, and the run-metadata keys that
carry session, user, tags and trace name onto every span the handler starts.
The active trace lives in a contextvar for the duration of the run, which is
how a model call that ``stream_isolation`` detached from the turn's callbacks
(vision, cognition workers, ``delegate_task``) finds its way back under it:
see :func:`nested_callbacks`.

Masking is decided per trace id (:func:`_mask_context_for`), not per client,
because agents with different capture modes can share one Langfuse project
and therefore one client.

Every public function here is best-effort: it logs and returns a neutral
value rather than raise, because tracing must never fail or slow a turn.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from olano.observability.ids import (
    pseudonymous_user,
    sampled,
    session_url,
    trace_id_for,
    trace_url,
    turn_seed,
)
from olano.observability.masking import STRICT, MaskContext
from olano.observability.policy import TracePolicy

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)

#: How long a finished trace's mask context is kept for spans still queued in
#: the batch processor (it flushes every few seconds; this is a wide margin).
_MASK_CONTEXT_TTL_S = 600.0
#: How often one failing client may log a warning.
_WARN_EVERY_S = 3600.0
#: Background origins that ``trace_background: false`` leaves untraced.
BACKGROUND_ORIGINS = frozenset({"cognition"})


@dataclass
class _Client:
    langfuse: Any
    public_key: str
    secret_digest: str
    host: str
    environment: str
    stats: Any
    created_at: float = field(default_factory=time.time)


@dataclass
class ActiveTrace:
    """One traced run (a turn, a background worker run)."""

    client: _Client
    policy: TracePolicy
    trace_id: str
    root: Any
    session_id: str
    run_metadata: dict[str, Any]
    url: str
    _handler: Any = None

    def handler(self) -> Any:
        """The run's LangChain callback handler, created on first use."""
        if self._handler is None:
            self._handler = self.nested_handler()
        return self._handler

    def nested_handler(self) -> Any:
        """A fresh handler under the same root, for a call outside the graph's
        callback tree (one handler instance tracks one run tree)."""
        from langfuse.langchain import CallbackHandler

        handler = CallbackHandler(
            public_key=self.client.public_key,
            trace_context={"trace_id": self.trace_id, "parent_span_id": self.root.id},
        )
        # LangChain runs a sync handler of an async run through a thread-pool
        # hop per callback, ~95 per tool-calling turn, which was most of the
        # measured per-turn cost. In metadata capture the handler serializes
        # almost nothing (`_install_media_guard`), so it runs inline; in full
        # capture it keeps the hop, so serializing a long prompt never holds
        # the event loop.
        handler.run_inline = not self.policy.full_capture
        return handler


_ACTIVE: contextvars.ContextVar[ActiveTrace | None] = contextvars.ContextVar(
    "olano_active_trace", default=None
)

_clients: dict[str, _Client] = {}
_clients_lock = threading.Lock()
_mask_contexts: dict[str, tuple[MaskContext, float]] = {}
_mask_lock = threading.Lock()
_last_warning: dict[str, float] = {}


# ── clients ───────────────────────────────────────────────────────────────


def _digest(secret: str) -> str:
    import hashlib

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _mask_context_for(trace_hex: str) -> MaskContext:
    now = time.monotonic()
    with _mask_lock:
        hit = _mask_contexts.get(trace_hex)
        if hit is None:
            return STRICT
        ctx, expires = hit
        if expires and expires < now:
            _mask_contexts.pop(trace_hex, None)
            return STRICT
        return ctx


def _register_mask(trace_hex: str, ctx: MaskContext) -> None:
    with _mask_lock:
        _mask_contexts[trace_hex] = (ctx, 0.0)
        if len(_mask_contexts) > 256:
            now = time.monotonic()
            for key, (_c, exp) in list(_mask_contexts.items()):
                if exp and exp < now:
                    _mask_contexts.pop(key, None)


def _expire_mask(trace_hex: str) -> None:
    with _mask_lock:
        hit = _mask_contexts.get(trace_hex)
        if hit is not None:
            _mask_contexts[trace_hex] = (hit[0], time.monotonic() + _MASK_CONTEXT_TTL_S)


def _drop_sdk_instance(public_key: str) -> None:
    """Forget the SDK's process-wide resource manager for ``public_key``.

    The SDK keeps one per public key and silently reuses it for a second
    ``Langfuse(public_key=…)``, whatever host or secret that one names; a
    changed credential would keep exporting with the old one until restart.
    """
    try:
        from langfuse._client.resource_manager import LangfuseResourceManager

        with LangfuseResourceManager._lock:
            inst = LangfuseResourceManager._instances.pop(public_key, None)
        if inst is not None:
            inst.shutdown()
    except Exception:
        logger.debug("observability: could not reset the SDK instance", exc_info=True)


def _client_for(policy: TracePolicy) -> _Client | None:
    digest = _digest(policy.secret_key)
    with _clients_lock:
        existing = _clients.get(policy.public_key)
        if (
            existing is not None
            and existing.secret_digest == digest
            and existing.host == policy.host
            and existing.environment == policy.environment
        ):
            return existing
        if existing is not None:
            _clients.pop(policy.public_key, None)
            try:
                existing.langfuse.shutdown()
            except Exception:
                pass
        _drop_sdk_instance(policy.public_key)
        try:
            client = _build_client(policy, digest)
        except Exception:
            logger.warning("observability: could not start a Langfuse client", exc_info=True)
            return None
        _clients[policy.public_key] = client
        return client


def _build_client(policy: TracePolicy, digest: str) -> _Client:
    from langfuse import Langfuse
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    from olano.observability.exporter import ExportStats, MaskingSpanExporter, build_otlp_exporter

    stats = ExportStats()
    exporter = MaskingSpanExporter(
        build_otlp_exporter(policy.host, policy.public_key, policy.secret_key),
        _mask_context_for,
        stats,
    )
    resource_attrs: dict[str, Any] = {"service.name": "olano"}
    if policy.environment:
        resource_attrs["langfuse.environment"] = policy.environment
    release = _olano_version()
    if release:
        resource_attrs["langfuse.release"] = release
    # Our own provider, never the global one: chromadb instruments with
    # OpenTelemetry too, and nothing but Langfuse spans belongs in this export.
    provider = TracerProvider(resource=Resource.create(resource_attrs))
    lf = Langfuse(
        public_key=policy.public_key,
        secret_key=policy.secret_key,
        base_url=policy.host,
        tracer_provider=provider,
        span_exporter=exporter,
        environment=policy.environment or None,
        release=release or None,
        flush_interval=5.0,
        timeout=10,
        tracing_enabled=True,
    )
    _install_media_guard(lf)
    return _Client(
        langfuse=lf,
        public_key=policy.public_key,
        secret_digest=digest,
        host=policy.host,
        environment=policy.environment,
        stats=stats,
    )


class _DropNoneAttributeWarnings(logging.Filter):
    """The SDK warns on every turn that LangChain's ``lc_agent_name`` run
    metadata is ``None``; it drops the value either way. Not an operator's
    problem, so not in their log."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "Propagated attribute" not in record.getMessage()


def _install_media_guard(lf: Any) -> None:
    """Route the SDK's media handling through Olano's rules, on this client.

    Every path that hands data to Langfuse (span creation, the LangChain
    handler, the export-time transform) calls the client's
    ``MediaManager._find_and_process_media``, which would upload any embedded
    image to Langfuse's storage. Replaced on this instance only: media is
    stripped and never uploaded, and in ``metadata`` capture an input or
    output is withheld before the SDK serializes it (the full message list
    with the system prompt, every model call), which is where nearly all of
    tracing's per-turn cost was. The exporter's mask stays the authority;
    this only means less is ever built. ``test_media_guard_is_installed``
    fails if the SDK stops routing through this method.
    """
    from olano.observability.masking import WITHHELD, strip_media

    manager = lf._resources._media_manager

    def _guard(*, data: Any, field: str, trace_id: str = "", observation_id: Any = None) -> Any:
        if field in ("input", "output"):
            active = _ACTIVE.get()
            if active is not None and not active.policy.full_capture:
                return WITHHELD
        return strip_media(data)

    manager._find_and_process_media = _guard
    sdk_logger = logging.getLogger("langfuse")
    if not any(isinstance(f, _DropNoneAttributeWarnings) for f in sdk_logger.filters):
        sdk_logger.addFilter(_DropNoneAttributeWarnings())


def _olano_version() -> str:
    try:
        from importlib.metadata import version

        return version("olano")
    except Exception:
        return ""


def prewarm_if_enabled() -> None:
    """Import the SDK ahead of the first traced turn (boot warm thread).

    Importing ``langfuse`` and its LangChain handler costs ~0.2 s in a warm
    runtime; paid on the first traced turn it would stall the event loop for
    every agent. Only when the deployment has tracing on: a box with it off
    must never load the SDK. An agent that turns it on alone pays on its
    first turn instead.
    """
    try:
        from olano.config.system_settings import load_observability_settings

        if not load_observability_settings().langfuse.enabled:
            return
        import langfuse
        import langfuse.langchain  # noqa: F401

        import olano.observability.exporter  # noqa: F401
    except Exception:
        logger.debug("observability: prewarm skipped", exc_info=True)


def shutdown_all(timeout_s: float = 5.0) -> None:
    """Flush and stop every client (runtime shutdown). Bounded by ``timeout_s``."""
    with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()
    if not clients:
        return

    def _stop() -> None:
        for client in clients:
            try:
                client.langfuse.shutdown()
            except Exception:
                logger.debug("observability: shutdown failed", exc_info=True)

    worker = threading.Thread(target=_stop, name="olano-langfuse-shutdown", daemon=True)
    worker.start()
    worker.join(timeout_s)


def client_stats() -> list[dict[str, Any]]:
    """Per-client export counters for the status endpoint (no secrets)."""
    with _clients_lock:
        clients = list(_clients.values())
    out = []
    for c in clients:
        snap = c.stats.snapshot()
        snap.update({"host": c.host, "public_key": _mask_key(c.public_key)})
        out.append(snap)
    return out


def _mask_key(public_key: str) -> str:
    return public_key[:10] + "…" if len(public_key) > 12 else public_key


def _warn_once(public_key: str, message: str) -> None:
    now = time.monotonic()
    last = _last_warning.get(public_key, 0.0)
    if now - last >= _WARN_EVERY_S:
        _last_warning[public_key] = now
        logger.warning(message)


# ── the run API ─────────────────────────────────────────────────────────────


def current_trace() -> ActiveTrace | None:
    return _ACTIVE.get()


def _secret_candidates(agent: str, env_overlay: dict[str, str] | None) -> list[str]:
    """Every secret value a run as ``agent`` could have seen, for the mask."""
    import os

    from olano.observability.masking import MIN_SECRET_LEN

    values: list[str] = []
    suffixes = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASS", "_PAT", "_CREDENTIALS")
    for name, value in os.environ.items():
        if value and len(value) >= MIN_SECRET_LEN and name.upper().endswith(suffixes):
            values.append(value)
    for value in (env_overlay or {}).values():
        if value and len(str(value)) >= MIN_SECRET_LEN:
            values.append(str(value))
    values.extend(_vault_values(agent))
    return values


_vault_values_cache: dict[str, tuple[float, list[str]]] = {}


def _vault_values(agent: str) -> list[str]:
    """Every vault value ``agent`` resolves (common and its overrides), cached
    for a minute: a full decrypt per turn would be wasteful, and a value saved
    a minute ago that slips through is masked by shape in most cases."""
    now = time.monotonic()
    hit = _vault_values_cache.get(agent)
    if hit is not None and now - hit[0] < 60.0:
        return hit[1]
    values: list[str] = []
    try:
        from olano.observability.credentials import _vault

        vault = _vault()
        if vault is not None:
            for row in vault.list_names():
                name = row.get("name")
                if not name:
                    continue
                try:
                    value = vault.get(name, agent=agent or None, touch=False)
                except Exception:
                    continue
                if value:
                    values.append(str(value))
    except Exception:
        logger.debug("observability: vault values unavailable for the mask", exc_info=True)
    _vault_values_cache[agent] = (now, values)
    return values


def begin_run(
    policy: TracePolicy | None,
    *,
    seed: str,
    name: str,
    session_id: str,
    sender: str = "",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    tier: str = "",
    env_overlay: dict[str, str] | None = None,
) -> tuple[ActiveTrace | None, contextvars.Token | None]:
    """Start a traced run and make it the active one.

    Returns ``(None, None)`` when there is nothing to trace (no policy, not
    sampled) or when anything goes wrong. Pair every call with
    :func:`end_run` passing the token back, in a ``finally``.
    """
    if policy is None:
        return None, None
    trace_id = trace_id_for(seed)
    if not sampled(trace_id, policy.sample_rate):
        return None, None
    try:
        client = _client_for(policy)
        if client is None:
            return None, None
        from olano.observability.masking import secret_values

        ctx = MaskContext(
            capture=policy.capture,
            secrets=secret_values(
                [policy.secret_key, *_secret_candidates(policy.agent, env_overlay)]
            ),
            tier=tier,
        )
        _register_mask(trace_id, ctx)
        user = ""
        if sender:
            if policy.full_capture:
                user = sender
            else:
                from olano.observability.credentials import user_salt

                user = pseudonymous_user(sender, user_salt())
        run_meta: dict[str, Any] = {
            "langfuse_session_id": session_id,
            "langfuse_trace_name": name,
            "langfuse_tags": list(tags or []),
        }
        if user:
            run_meta["langfuse_user_id"] = user
        from langfuse import propagate_attributes

        # Entered and left without yielding, so the context it attaches is
        # detached on this same frame: the root span picks the trace
        # attributes up at start, and nothing leaks into the caller.
        with propagate_attributes(
            session_id=session_id,
            user_id=user or None,
            tags=list(tags or []) or None,
            trace_name=name,
        ):
            root = client.langfuse.start_observation(
                name=name,
                as_type="span",
                trace_context={"trace_id": trace_id},
                metadata={k: v for k, v in (metadata or {}).items() if v not in (None, "")},
            )
        trace = ActiveTrace(
            client=client,
            policy=policy,
            trace_id=trace_id,
            root=root,
            session_id=session_id,
            run_metadata=run_meta,
            url=trace_url(policy.host, policy.project_id, trace_id),
        )
        token = _ACTIVE.set(trace)
        return trace, token
    except Exception:
        _warn_once(policy.public_key, "observability: a run could not be traced (see debug log)")
        logger.debug("observability: begin_run failed", exc_info=True)
        return None, None


def end_run(
    trace: ActiveTrace | None,
    token: contextvars.Token | None,
    *,
    metadata: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    """Close a run started by :func:`begin_run` (safe with ``None``)."""
    if token is not None:
        try:
            _ACTIVE.reset(token)
        except ValueError:
            # Reset from another context (an abandoned generator finalized
            # elsewhere): clear what this context sees instead.
            _ACTIVE.set(None)
    if trace is None:
        return
    try:
        update: dict[str, Any] = {}
        meta = {k: v for k, v in (metadata or {}).items() if v not in (None, "")}
        if meta:
            update["metadata"] = meta
        if error is not None:
            update["level"] = "ERROR"
            update["status_message"] = f"{type(error).__name__}: {error}"
        if update:
            trace.root.update(**update)
        trace.root.end()
    except Exception:
        logger.debug("observability: end_run failed", exc_info=True)
    finally:
        _expire_mask(trace.trace_id)


def begin_background_run(
    config: Any,
    *,
    name: str,
    session_id: str,
    model: str = "",
) -> tuple[ActiveTrace | None, contextvars.Token | None, list[Any]]:
    """Tracing for a run on the raw graph (a Cortex worker, ``delegate_task``).

    Inside a traced turn, the run nests under it: no new trace, one handler
    returned. Outside one, it is its own trace when the agent's policy traces
    background runs. Returns ``(trace, token, callbacks)``; pass the first two
    to :func:`end_run` (both ``None`` when nested or untraced).
    """
    active = _ACTIVE.get()
    if active is not None:
        try:
            return None, None, [active.nested_handler()]
        except Exception:
            logger.debug("observability: nested worker handler failed", exc_info=True)
            return None, None, []
    try:
        import uuid

        from olano.models.olano_gateway import is_olano_model
        from olano.observability.policy import effective_trace_policy

        policy = effective_trace_policy(config)
        if policy is None or not policy.trace_background:
            return None, None, []
        trace, token = begin_run(
            policy,
            seed=f"olano:{policy.agent}:{session_id}:{uuid.uuid4().hex}",
            name=name,
            session_id=session_id,
            tags=[f"agent:{policy.agent}", "origin:cognition"],
            metadata={
                "olano_agent": policy.agent,
                "olano_origin": "cognition",
                "olano_model": model,
            },
            tier=model if is_olano_model(model) else "",
            env_overlay=None,
        )
        if trace is None:
            return None, None, []
        return trace, token, [trace.handler()]
    except Exception:
        logger.debug("observability: background run not traced", exc_info=True)
        return None, None, []


def run_config_additions(base_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """``callbacks`` + ``metadata`` to merge into a graph run's config while a
    trace is active (``{}`` otherwise)."""
    trace = _ACTIVE.get()
    if trace is None:
        return {}
    try:
        return {
            "callbacks": [trace.handler()],
            "metadata": {**(base_metadata or {}), **trace.run_metadata},
        }
    except Exception:
        logger.debug("observability: run config additions failed", exc_info=True)
        return {}


def nested_config(base: Mapping[str, Any] | None = None) -> RunnableConfig:
    """``base`` plus the active trace's handler, for a model call that runs
    detached from the turn's callbacks (``stream_isolation``). The handler is
    a fresh one under the turn's root; it is never a streaming handler, so the
    detachment keeps doing its job."""
    out: dict[str, Any] = dict(base or {})
    trace = _ACTIVE.get()
    if trace is None:
        return cast("RunnableConfig", out)
    try:
        callbacks = list(out.get("callbacks") or [])
        callbacks.append(trace.nested_handler())
        out["callbacks"] = callbacks
        out["metadata"] = {**dict(out.get("metadata") or {}), **trace.run_metadata}
    except Exception:
        logger.debug("observability: nested config failed", exc_info=True)
    return cast("RunnableConfig", out)


def score(
    trace_id: str,
    policy: TracePolicy,
    *,
    value: float,
    comment: str = "",
    score_id: str | None = None,
) -> bool:
    """Send a ``user_feedback`` score for ``trace_id``. ``comment`` is sent
    only in full capture (it is user content), and the caller has already
    masked it: scores go through Langfuse's ingestion API, never through
    :class:`MaskingSpanExporter`. A repeated ``score_id`` replaces the earlier
    score, which is how a changed vote stays one score. Returns whether it was
    queued."""
    try:
        client = _client_for(policy)
        if client is None:
            return False
        client.langfuse.create_score(
            name="user_feedback",
            value=float(value),
            trace_id=trace_id,
            score_id=score_id,
            data_type="NUMERIC",
            comment=(comment or None) if policy.full_capture else None,
        )
        return True
    except Exception:
        logger.debug("observability: score failed", exc_info=True)
        return False


def clear_score(policy: TracePolicy, score_id: str) -> bool:
    """Delete a score (a vote taken back). Flushes first, so a create still
    queued for the same id cannot land after the delete. Blocking: call it
    off the event loop."""
    try:
        client = _client_for(policy)
        if client is None:
            return False
        client.langfuse.flush()
        client.langfuse.api.scores.delete(score_id)
        return True
    except Exception:
        logger.debug("observability: score delete failed", exc_info=True)
        return False


def links_for(policy: TracePolicy | None, trace_id: str, session_id: str) -> dict[str, str]:
    if policy is None:
        return {}
    return {
        "trace_url": trace_url(policy.host, policy.project_id, trace_id),
        "session_url": session_url(policy.host, policy.project_id, session_id),
    }


def turn_trace_id(agent: str, scoped_thread_id: str, turn_id: str) -> str:
    return trace_id_for(turn_seed(agent, scoped_thread_id, turn_id))


def _reset_for_tests() -> None:
    with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()
    for c in clients:
        try:
            c.langfuse.shutdown()
        except Exception:
            pass
        _drop_sdk_instance(c.public_key)
    with _mask_lock:
        _mask_contexts.clear()
    _vault_values_cache.clear()
    _last_warning.clear()
