"""A2A client built on the official ``a2a-sdk`` (``a2a.client``).

What the SDK gives us and we deliberately reuse rather than re-implement: Agent
Card resolution (``A2ACardResolver``), transport negotiation from the card
(``ClientFactory`` — JSON-RPC and HTTP+JSON bindings), the request/response
protobuf types, and the interceptor hook credentials are injected through.

What Olano adds on top is policy — the part no SDK can decide for a
deployment:

* **Allow-list, not URL.** A message goes only to a name in the calling
  agent's ``remote_agents``; the model never supplies a URL to send to.
* **HTTPS + SSRF at two points.** The configured base URL *and* every service
  endpoint the fetched card advertises are checked (``security/ssrf``), and
  the card's endpoint must share origin with the configured URL unless the
  operator opts out — a hostile card cannot redirect the credential.
* **Credentials from the vault**, resolved per agent with the same
  ``${vault:NAME}`` precedence remote MCP headers use, attached only to the
  vetted endpoint, never logged.
* **Bounded I/O.** Per-remote timeout, reply truncation, message cap.
* **Audited.** Every send/reply is published as an ``agent_message`` frame
  (kind ``remote``) so it appears in the Agent to Agent view and the
  ``a2a_activity.jsonl`` history alongside peer traffic.

Imports of ``a2a`` are kept inside functions: the SDK costs ~0.6s to import
and most agents never configure a remote agent.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from olano.config.system_settings import RemoteAgentSettings, load_remote_agent_settings
from olano.security.ssrf import SSRFBlockedError, assert_safe_outbound_url

if TYPE_CHECKING:
    import httpx

    from olano.config.agent_config import AgentConfig, RemoteAgentConfig

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
_CARD_TTL_SECONDS = 600
_CONTEXT_TTL_SECONDS = 24 * 3600
_TERMINAL_STATES = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
}
_WAITING_STATES = {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}


class RemoteAgentError(RuntimeError):
    """The remote agent could not be reached or answered with an error."""


class RemoteAgentPolicyError(RemoteAgentError):
    """A deployment policy refused the call before anything left the box.

    Non-retryable: the model cannot fix an allow-list, a scheme or an SSRF
    decision by trying again, and the message says which operator knob would.
    """


@dataclass
class RemoteAgentReply:
    """What one ``send``/``get_task`` produced, flattened for the model."""

    remote: str
    text: str
    state: str = ""
    task_id: str = ""
    context_id: str = ""
    truncated: bool = False

    def render(self) -> str:
        lines = []
        if self.text:
            lines.append(self.text)
        else:
            lines.append("(the remote agent returned no text)")
        meta = []
        if self.state:
            meta.append(f"state={_short_state(self.state)}")
        if self.task_id:
            meta.append(f"task_id={self.task_id}")
        if self.context_id:
            meta.append(f"context_id={self.context_id}")
        if meta:
            lines.append(f"[a2a {self.remote}: {' '.join(meta)}]")
        if self.truncated:
            lines.append("[a2a note] reply truncated to the configured maximum length")
        if _short_state(self.state) in {"submitted", "working"}:
            lines.append(
                "[a2a note] the remote task is still running — call a2a_get_task with this "
                "task_id to fetch the result later (wait_n_seconds in between)."
            )
        elif _short_state(self.state) in {"input_required", "auth_required"}:
            lines.append(
                "[a2a note] the remote agent is waiting on the caller — answer its question "
                "with another a2a_send_message carrying the same context_id."
            )
        return "\n".join(lines)


@dataclass
class _CardCacheEntry:
    card: Any
    fetched_at: float


@dataclass
class _ContextEntry:
    context_id: str
    seen_at: float


# (caller, remote) → cached Agent Card. Cards change rarely; refetching per
# message would double every call's latency.
_CARDS: dict[tuple[str, str], _CardCacheEntry] = {}
# (caller, remote, olano session) → last A2A context id, so a follow-up from the
# same conversation continues the same remote conversation without the model
# having to carry the id itself.
_CONTEXTS: dict[tuple[str, str, str], _ContextEntry] = {}
# Test seam: an httpx.AsyncClient factory. Production builds a fresh client
# per call so a remote that hangs never pins a shared pool.
_HTTPX_CLIENT_FACTORY: Any | None = None


def _short_state(state: str) -> str:
    return state.removeprefix("TASK_STATE_").lower()


def _norm(name: str) -> str:
    return (name or "").strip().casefold()


def _publish(event_type: str, payload: dict[str, Any]) -> None:
    """Best-effort SSE/activity-log publish; never lets telemetry break a call."""
    try:
        from olano.tools.talk_to import publish_activity_event

        publish_activity_event(event_type, payload)
    except Exception:  # pragma: no cover - telemetry never breaks the call
        logger.debug("a2a: activity publish failed", exc_info=True)


# ── Config access ──────────────────────────────────────────────────────────────


def _load_agent_config(agent_name: str) -> AgentConfig | None:
    """The calling agent's live config: the booted runner's copy when the
    runtime is bound (so a dashboard edit is seen after the agent's own
    reload, exactly like every other field), else the on-disk file."""
    try:
        from olano.tools.talk_to import get_bound_runtime

        runtime = get_bound_runtime()
        runner = getattr(runtime, "_runners", {}).get(agent_name) if runtime else None
        if runner is not None and getattr(runner, "config", None) is not None:
            return cast("AgentConfig", runner.config)
    except Exception:  # pragma: no cover - fall through to disk
        pass
    try:
        from olano.config.loader import load_agent_config

        return load_agent_config(agent_name)
    except Exception:
        return None


def _entries(agent_name: str) -> list[RemoteAgentConfig]:
    config = _load_agent_config(agent_name)
    if config is None:
        return []
    return [e for e in (config.remote_agents or []) if e.enabled]


def _security_lists(agent_name: str) -> tuple[list[str], list[str]]:
    config = _load_agent_config(agent_name)
    sec: Any = getattr(config, "security", None)
    if sec is None:
        return [], []
    if isinstance(sec, dict):
        return list(sec.get("ssrf_allowlist") or []), list(sec.get("ssrf_blocklist") or [])
    return (
        list(getattr(sec, "ssrf_allowlist", []) or []),
        list(getattr(sec, "ssrf_blocklist", []) or []),
    )


def _settings() -> RemoteAgentSettings:
    return load_remote_agent_settings()


def _find_entry(agent_name: str, remote: str) -> RemoteAgentConfig:
    wanted = _norm(remote)
    entries = _entries(agent_name)
    for entry in entries:
        if _norm(entry.name) == wanted:
            return entry
    known = ", ".join(sorted(e.name for e in entries)) or "(none configured)"
    raise RemoteAgentPolicyError(
        f"'{remote}' is not one of this agent's configured remote agents. Known: {known}. "
        "Remote agents are added by an operator under agent.json5 → remote_agents "
        "(dashboard: agent → Advanced → Remote Agents); the model cannot add one."
    )


# ── URL policy ─────────────────────────────────────────────────────────────────


def _is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in _LOOPBACK_HOSTS


def _check_url(url: str, entry: RemoteAgentConfig, agent_name: str, *, what: str) -> None:
    """HTTPS + SSRF policy for one URL. ``what`` names it in the error."""
    parsed = urlparse(url)
    if parsed.scheme == "http":
        if not (entry.allow_insecure_http and _is_loopback(url)):
            raise RemoteAgentPolicyError(
                f"Refusing plain http:// for the {what} of remote agent '{entry.name}' "
                f"({url}): A2A credentials travel in headers, so the endpoint must be "
                "https://. For a local development agent set allow_insecure_http: true "
                "(loopback hosts only)."
            )
        return  # explicit loopback dev exemption — SSRF check would refuse 127.0.0.1
    if parsed.scheme != "https":
        raise RemoteAgentPolicyError(
            f"Unsupported URL scheme '{parsed.scheme}' in the {what} of remote agent "
            f"'{entry.name}'."
        )
    allowlist, blocklist = _security_lists(agent_name)
    try:
        assert_safe_outbound_url(url, allowlist=allowlist, blocklist=blocklist)
    except SSRFBlockedError as exc:
        raise RemoteAgentPolicyError(
            f"Refusing the {what} of remote agent '{entry.name}' ({url}): {exc}"
        ) from exc


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    port_a = pa.port or (443 if pa.scheme == "https" else 80)
    port_b = pb.port or (443 if pb.scheme == "https" else 80)
    return (
        pa.scheme == pb.scheme
        and (pa.hostname or "").lower() == (pb.hostname or "").lower()
        and port_a == port_b
    )


def _check_card_endpoints(card: Any, entry: RemoteAgentConfig, agent_name: str) -> None:
    """Every service endpoint the card advertises passes the same policy as the
    base URL, plus the same-origin pin. Checked *before* a client is built, so
    no transport ever exists for an endpoint we would refuse."""
    interfaces = list(getattr(card, "supported_interfaces", []) or [])
    urls = [str(getattr(i, "url", "") or "") for i in interfaces]
    urls = [u for u in urls if u]
    if not urls:
        raise RemoteAgentError(
            f"The Agent Card of remote agent '{entry.name}' advertises no service endpoint "
            "(supported_interfaces is empty) — nothing to send to."
        )
    for url in urls:
        _check_url(url, entry, agent_name, what="service endpoint advertised by its Agent Card")
        if entry.same_origin_endpoint and not _same_origin(url, entry.url):
            raise RemoteAgentPolicyError(
                f"The Agent Card of remote agent '{entry.name}' points its service endpoint at "
                f"{url}, a different origin than the configured {entry.url}. Refusing so the "
                "credential is never sent to a host the operator did not name. If this "
                "provider really serves its endpoint elsewhere, set same_origin_endpoint: "
                "false on the entry after checking where it points."
            )


# ── Credentials ────────────────────────────────────────────────────────────────


def _resolve_credential(entry: RemoteAgentConfig, agent_name: str) -> str:
    if entry.auth == "none":
        return ""
    raw = (entry.credential or "").strip()
    if not raw:
        raise RemoteAgentPolicyError(
            f"Remote agent '{entry.name}' is configured with auth: {entry.auth} but has no "
            "credential. Set credential to a vault reference like ${vault:NAME} "
            "(store the token in the vault first)."
        )
    if "${vault:" in raw:
        try:
            from olano.core.mcp_setup import _resolve_agent_ref
            from olano.vault.resolver import get_vault

            resolved = _resolve_agent_ref(raw, agent_name, get_vault())
        except Exception as exc:
            raise RemoteAgentError(
                f"Could not read the vault credential for remote agent '{entry.name}': {exc}"
            ) from exc
        if "${vault:" in resolved:
            raise RemoteAgentPolicyError(
                f"The vault has no value for the credential of remote agent '{entry.name}' "
                f"({raw}). Store it with the vault (secret_set / Config → Credentials) for "
                "this agent or as a shared entry."
            )
        return resolved
    return raw


def _credential_header(entry: RemoteAgentConfig, credential: str) -> tuple[str, str] | None:
    if not credential:
        return None
    if entry.auth == "bearer":
        return "Authorization", f"Bearer {credential}"
    if entry.auth == "api_key":
        return (entry.api_key_header or "X-API-Key").strip(), credential
    return None


class _StaticCredentialInterceptor:
    """``a2a.client.ClientCallInterceptor`` that attaches one configured header.

    The SDK's own ``AuthInterceptor`` applies a credential only when the Agent
    Card *declares* a matching security scheme. Many cards declare none while
    the deployment still fronts the agent with an API gateway, and the operator
    has stated the scheme explicitly in ``auth:`` — so we honour the operator's
    choice unconditionally. The header only ever reaches endpoints that passed
    :func:`_check_card_endpoints`.
    """

    def __init__(self, header: tuple[str, str] | None) -> None:
        self._header = header

    async def before(self, args: Any) -> None:
        if self._header is None:
            return
        from a2a.client import ClientCallContext

        if args.context is None:
            args.context = ClientCallContext()
        if args.context.service_parameters is None:
            args.context.service_parameters = {}
        name, value = self._header
        args.context.service_parameters[name] = value

    async def after(self, args: Any) -> None:  # pragma: no cover - nothing to do
        return None


# ── SDK plumbing ───────────────────────────────────────────────────────────────


def _httpx_client(entry: RemoteAgentConfig) -> httpx.AsyncClient:
    import httpx

    if _HTTPX_CLIENT_FACTORY is not None:
        return cast("httpx.AsyncClient", _HTTPX_CLIENT_FACTORY(entry))
    return httpx.AsyncClient(
        timeout=httpx.Timeout(float(entry.timeout_seconds), connect=15.0),
        follow_redirects=False,  # a redirect could carry the credential elsewhere
        headers={"User-Agent": "olano-a2a/1"},
    )


async def _fetch_card(
    client: httpx.AsyncClient, entry: RemoteAgentConfig, agent_name: str, *, force: bool = False
) -> Any:
    key = (_norm(agent_name), _norm(entry.name))
    cached = _CARDS.get(key)
    if cached and not force and time.monotonic() - cached.fetched_at < _CARD_TTL_SECONDS:
        return cached.card
    from a2a.client import A2ACardResolver, AgentCardResolutionError

    resolver = A2ACardResolver(client, entry.url)
    try:
        card = await resolver.get_agent_card()
    except AgentCardResolutionError as exc:
        raise RemoteAgentError(
            f"Could not fetch the Agent Card of remote agent '{entry.name}' from "
            f"{entry.url}/.well-known/agent-card.json: {exc}"
        ) from exc
    _check_card_endpoints(card, entry, agent_name)
    _CARDS[key] = _CardCacheEntry(card=card, fetched_at=time.monotonic())
    return card


def _build_client(client: httpx.AsyncClient, card: Any, entry: RemoteAgentConfig, credential: str):
    from a2a.client import ClientConfig, ClientFactory

    config = ClientConfig(
        httpx_client=client,
        # Blocking SendMessage: the remote answers when its task settles (or we
        # hit timeout_seconds and return the task id for polling). Streaming
        # would only add SSE plumbing for a reply the model reads whole.
        streaming=False,
        polling=False,
    )
    factory = ClientFactory(config)
    interceptors = [_StaticCredentialInterceptor(_credential_header(entry, credential))]
    try:
        return factory.create(card, interceptors=interceptors)  # type: ignore[arg-type]
    except ValueError as exc:
        raise RemoteAgentError(
            f"No compatible transport for remote agent '{entry.name}': {exc} "
            "(Olano speaks A2A over JSON-RPC and HTTP+JSON; gRPC-only agents are not supported)."
        ) from exc


def _text_of_parts(parts: Any) -> str:
    chunks: list[str] = []
    for part in parts or []:
        text = getattr(part, "text", "")
        if text:
            chunks.append(str(text))
            continue
        # Structured data parts render as JSON so the model still sees them.
        if part.HasField("data") if hasattr(part, "HasField") else False:
            try:
                import json

                from google.protobuf.json_format import MessageToDict

                chunks.append(json.dumps(MessageToDict(part.data), ensure_ascii=False))
            except Exception:  # pragma: no cover - best effort
                pass
        elif getattr(part, "url", ""):
            chunks.append(f"[file: {part.url}]")
        elif getattr(part, "filename", ""):
            chunks.append(f"[file: {part.filename}]")
    return "\n".join(chunks).strip()


def _reply_from_task(task: Any, remote: str) -> RemoteAgentReply:
    from a2a.types import TaskState

    state = TaskState.Name(task.status.state) if task.status else ""
    texts: list[str] = []
    for artifact in task.artifacts or []:
        t = _text_of_parts(artifact.parts)
        if t:
            texts.append(t)
    status_text = (
        _text_of_parts(task.status.message.parts) if task.status.HasField("message") else ""
    )
    if status_text and (not texts or state in _WAITING_STATES or state == "TASK_STATE_FAILED"):
        texts.append(status_text)
    if not texts:
        # Fall back to the last agent message in history.
        for msg in reversed(list(task.history or [])):
            if getattr(msg, "role", None) == 2:  # Role.ROLE_AGENT
                t = _text_of_parts(msg.parts)
                if t:
                    texts.append(t)
                    break
    return RemoteAgentReply(
        remote=remote,
        text="\n\n".join(texts),
        state=state,
        task_id=str(task.id or ""),
        context_id=str(task.context_id or ""),
    )


def _reply_from_message(message: Any, remote: str) -> RemoteAgentReply:
    return RemoteAgentReply(
        remote=remote,
        text=_text_of_parts(message.parts),
        state="",
        task_id=str(message.task_id or ""),
        context_id=str(message.context_id or ""),
    )


def _truncate(reply: RemoteAgentReply, limit: int) -> RemoteAgentReply:
    if len(reply.text) > limit:
        reply.text = reply.text[:limit].rstrip() + " …"
        reply.truncated = True
    return reply


def _remember_context(caller: str, remote: str, session_id: str, context_id: str) -> None:
    if not context_id or not session_id:
        return
    now = time.monotonic()
    _CONTEXTS[(_norm(caller), _norm(remote), session_id)] = _ContextEntry(context_id, now)
    # Opportunistic sweep — the map is tiny, so a linear pass is fine.
    for key, entry in list(_CONTEXTS.items()):
        if now - entry.seen_at > _CONTEXT_TTL_SECONDS:
            _CONTEXTS.pop(key, None)


def _recall_context(caller: str, remote: str, session_id: str) -> str:
    entry = _CONTEXTS.get((_norm(caller), _norm(remote), session_id))
    if entry is None or time.monotonic() - entry.seen_at > _CONTEXT_TTL_SECONDS:
        return ""
    return entry.context_id


def _current_session_id() -> str:
    try:
        from olano.security.request_context import get_request_context

        return get_request_context().session_id or ""
    except Exception:  # pragma: no cover
        return ""


# ── Public API ─────────────────────────────────────────────────────────────────


def list_remote_agents(agent_name: str) -> list[dict[str, Any]]:
    """The calling agent's enabled remote agents, with cached card details."""
    out: list[dict[str, Any]] = []
    for entry in _entries(agent_name):
        row: dict[str, Any] = {
            "name": entry.name,
            "url": entry.url,
            "description": entry.description,
            "auth": entry.auth,
        }
        cached = _CARDS.get((_norm(agent_name), _norm(entry.name)))
        if cached is not None:
            card = cached.card
            row["card_name"] = str(getattr(card, "name", "") or "")
            if not row["description"]:
                row["description"] = str(getattr(card, "description", "") or "")
            row["skills"] = [
                {"id": s.id, "name": s.name, "description": s.description}
                for s in list(getattr(card, "skills", []) or [])[:20]
            ]
        out.append(row)
    return out


async def discover_agent_card(agent_name: str, target: str) -> dict[str, Any]:
    """Fetch and summarize an Agent Card.

    ``target`` is a configured remote-agent name, or — only when the home-wide
    ``remote_agents.allow_ad_hoc_discovery`` is on — an ``https://`` base URL.
    Ad-hoc URLs still pass the SSRF policy; nothing is ever sent to them.
    """
    settings = _settings()
    if not settings.enabled:
        raise RemoteAgentPolicyError(
            "Remote agents are disabled home-wide (config.yaml → remote_agents.enabled: false)."
        )
    from olano.config.agent_config import RemoteAgentConfig

    try:
        entry = _find_entry(agent_name, target)
    except RemoteAgentPolicyError:
        if not target.lower().startswith(("https://", "http://")):
            raise
        if not settings.allow_ad_hoc_discovery:
            raise RemoteAgentPolicyError(
                "Discovering an arbitrary URL is off (config.yaml → remote_agents."
                "allow_ad_hoc_discovery: false). Ask the operator to add the agent under "
                "remote_agents, or to enable ad-hoc discovery."
            ) from None
        entry = RemoteAgentConfig(name="ad-hoc", url=target)
    _check_url(entry.url, entry, agent_name, what="base URL")
    client = _httpx_client(entry)
    try:
        card = await asyncio.wait_for(
            _fetch_card(client, entry, agent_name, force=True), timeout=entry.timeout_seconds
        )
    except TimeoutError as exc:
        raise RemoteAgentError(
            f"Timed out fetching the Agent Card of '{entry.name}' after {entry.timeout_seconds}s."
        ) from exc
    finally:
        await client.aclose()
    schemes = []
    for name, scheme in dict(getattr(card, "security_schemes", {}) or {}).items():
        kind = scheme.WhichOneof("scheme") if hasattr(scheme, "WhichOneof") else "?"
        schemes.append({"name": name, "type": str(kind or "").replace("_security_scheme", "")})
    return {
        "name": str(card.name or ""),
        "description": str(card.description or ""),
        "version": str(card.version or ""),
        "endpoints": [
            {"url": i.url, "binding": i.protocol_binding, "protocol_version": i.protocol_version}
            for i in card.supported_interfaces
        ],
        "streaming": bool(card.capabilities.streaming) if card.HasField("capabilities") else False,
        "security_schemes": schemes,
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "tags": list(s.tags),
                "examples": list(s.examples)[:3],
            }
            for s in card.skills
        ],
        "input_modes": list(card.default_input_modes),
        "output_modes": list(card.default_output_modes),
    }


async def send_remote_message(
    agent_name: str,
    remote: str,
    message: str,
    *,
    context_id: str = "",
) -> RemoteAgentReply:
    """Send ``message`` to the configured remote agent and return its reply.

    Continues the remote conversation named by ``context_id`` when given,
    otherwise the last one this Olano conversation had with that remote agent
    (remembered for 24h), otherwise starts a new one.
    """
    settings = _settings()
    if not settings.enabled:
        raise RemoteAgentPolicyError(
            "Remote agents are disabled home-wide (config.yaml → remote_agents.enabled: false)."
        )
    message = (message or "").strip()
    if not message:
        raise RemoteAgentPolicyError("The message to the remote agent is empty.")
    if len(message) > settings.max_message_chars:
        raise RemoteAgentPolicyError(
            f"Message is {len(message)} characters; the limit is {settings.max_message_chars} "
            "(config.yaml → remote_agents.max_message_chars)."
        )
    entry = _find_entry(agent_name, remote)
    _check_url(entry.url, entry, agent_name, what="base URL")
    credential = _resolve_credential(entry, agent_name)
    session_id = _current_session_id()
    context_id = (context_id or "").strip() or _recall_context(agent_name, entry.name, session_id)

    started = datetime.now(timezone.utc).isoformat()
    _publish(
        "agent_message",
        {
            "kind": "remote",
            "protocol": "a2a",
            "phase": "send",
            "from": agent_name,
            "to": f"a2a:{entry.name}",
            "message": message,
            "remote_url": entry.url,
            "context_id": context_id,
            "ts": started,
        },
    )
    _publish(
        "agent_activity", {"agent": agent_name, "status": "talking", "peer": f"a2a:{entry.name}"}
    )

    client = _httpx_client(entry)
    reply: RemoteAgentReply | None = None
    error: str = ""
    try:
        try:
            reply = await asyncio.wait_for(
                _send(client, entry, agent_name, credential, message, context_id),
                timeout=float(entry.timeout_seconds),
            )
        except TimeoutError as exc:
            raise RemoteAgentError(
                f"Remote agent '{entry.name}' did not answer within {entry.timeout_seconds}s "
                "(remote_agents[].timeout_seconds)."
            ) from exc
    except RemoteAgentError as exc:
        error = str(exc)
        raise
    except Exception as exc:  # pragma: no cover - SDK/transport surprises
        error = f"{type(exc).__name__}: {exc}"
        raise RemoteAgentError(f"Calling remote agent '{entry.name}' failed: {error}") from exc
    finally:
        await client.aclose()
        finished = datetime.now(timezone.utc).isoformat()
        payload: dict[str, Any] = {
            "kind": "remote",
            "protocol": "a2a",
            "phase": "reply",
            "from": agent_name,
            "to": f"a2a:{entry.name}",
            "message": message,
            "remote_url": entry.url,
            "ts_sent": started,
            "ts_reply": finished,
        }
        if reply is not None:
            payload.update(
                {
                    "reply": reply.text,
                    "state": _short_state(reply.state),
                    "task_id": reply.task_id,
                    "context_id": reply.context_id,
                }
            )
        else:
            payload.update({"reply": f"[error] {error}", "error": error})
        _publish("agent_message", payload)
        _publish("agent_activity", {"agent": agent_name, "status": "idle"})

    _remember_context(agent_name, entry.name, session_id, reply.context_id)
    return _truncate(reply, settings.max_reply_chars)


async def _send(
    client: httpx.AsyncClient,
    entry: RemoteAgentConfig,
    agent_name: str,
    credential: str,
    message: str,
    context_id: str,
) -> RemoteAgentReply:
    from a2a.types import Message, Part, Role, SendMessageRequest

    card = await _fetch_card(client, entry, agent_name)
    a2a_client = _build_client(client, card, entry, credential)
    msg = Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
        parts=[Part(text=message)],
    )
    if context_id:
        msg.context_id = context_id
    request = SendMessageRequest(message=msg)
    reply: RemoteAgentReply | None = None
    try:
        async for event in a2a_client.send_message(request):
            if event.HasField("task"):
                reply = _reply_from_task(event.task, entry.name)
            elif event.HasField("message"):
                reply = _reply_from_message(event.message, entry.name)
            elif event.HasField("status_update"):  # pragma: no cover - streaming off
                continue
    except Exception as exc:
        raise _wrap_sdk_error(exc, entry) from exc
    finally:
        try:
            await a2a_client.close()
        except Exception:  # pragma: no cover
            pass
    if reply is None:
        raise RemoteAgentError(
            f"Remote agent '{entry.name}' returned neither a task nor a message."
        )
    return reply


async def get_remote_task(agent_name: str, remote: str, task_id: str) -> RemoteAgentReply:
    """Fetch the current state of a remote task returned earlier as in-progress."""
    settings = _settings()
    if not settings.enabled:
        raise RemoteAgentPolicyError(
            "Remote agents are disabled home-wide (config.yaml → remote_agents.enabled: false)."
        )
    task_id = (task_id or "").strip()
    if not task_id:
        raise RemoteAgentPolicyError("task_id is required.")
    entry = _find_entry(agent_name, remote)
    _check_url(entry.url, entry, agent_name, what="base URL")
    credential = _resolve_credential(entry, agent_name)
    client = _httpx_client(entry)
    try:
        from a2a.types import GetTaskRequest

        card = await _fetch_card(client, entry, agent_name)
        a2a_client = _build_client(client, card, entry, credential)
        try:
            task = await asyncio.wait_for(
                a2a_client.get_task(GetTaskRequest(id=task_id)),
                timeout=float(entry.timeout_seconds),
            )
        except TimeoutError as exc:
            raise RemoteAgentError(
                f"Remote agent '{entry.name}' did not answer within {entry.timeout_seconds}s."
            ) from exc
        except RemoteAgentError:
            raise
        except Exception as exc:
            raise _wrap_sdk_error(exc, entry) from exc
        finally:
            try:
                await a2a_client.close()
            except Exception:  # pragma: no cover
                pass
    finally:
        await client.aclose()
    reply = _reply_from_task(task, entry.name)
    _publish(
        "agent_message",
        {
            "kind": "remote",
            "protocol": "a2a",
            "phase": "reply",
            "from": agent_name,
            "to": f"a2a:{entry.name}",
            "message": f"[get_task {task_id}]",
            "reply": reply.text,
            "state": _short_state(reply.state),
            "task_id": reply.task_id,
            "context_id": reply.context_id,
            "remote_url": entry.url,
            "ts_sent": datetime.now(timezone.utc).isoformat(),
            "ts_reply": datetime.now(timezone.utc).isoformat(),
        },
    )
    return _truncate(reply, settings.max_reply_chars)


def _wrap_sdk_error(exc: Exception, entry: RemoteAgentConfig) -> RemoteAgentError:
    if isinstance(exc, RemoteAgentError):
        return exc
    detail = str(exc) or type(exc).__name__
    lowered = detail.lower()
    if "401" in detail or "unauthorized" in lowered:
        return RemoteAgentError(
            f"Remote agent '{entry.name}' refused the credential (401 Unauthorized). Check the "
            "remote_agents entry's auth mode and vault credential."
        )
    if "403" in detail or "forbidden" in lowered:
        return RemoteAgentError(
            f"Remote agent '{entry.name}' refused the call (403 Forbidden): the credential is "
            "valid but lacks permission for this skill."
        )
    return RemoteAgentError(f"Calling remote agent '{entry.name}' failed: {detail}")


def reset_caches() -> None:
    """Test hook: forget cached cards and conversation contexts."""
    _CARDS.clear()
    _CONTEXTS.clear()


__all__ = [
    "RemoteAgentError",
    "RemoteAgentPolicyError",
    "RemoteAgentReply",
    "discover_agent_card",
    "get_remote_task",
    "list_remote_agents",
    "reset_caches",
    "send_remote_message",
]
