"""What a tool does when the service it is signed in to refuses the sign-in.

A ``401`` from Salesforce, GitHub, Microsoft 365, Google or an OAuth MCP
server is not a bug in the request and not a fact the agent should merely
relay. It means the saved sign-in no longer works: the org revoked the
session, the grant died with a password change, or the token simply outlived
what the file says about it. The Connections card and the MCP tab cannot see
any of that (they read files and the last boot), so they kept saying
*Connected* while every call failed, and the person's only route was to
notice, open the dashboard, Disconnect and Connect again by hand.

Two layers, one story:

- **Toolkit-side calls** (the Salesforce toolkit uses them directly):
  :func:`recover_connector_credential` refreshes the connector record behind
  a credential name regardless of what the file says about expiry and hands
  back the new value for one retry; :func:`connector_auth_failure` marks the
  record when the retry is refused too and returns the tool result the model
  needs.
- **The generic seam every tool passes through** (the central error boundary,
  ``tools/_error_boundary.py``): :func:`repair_refusal_sync` /
  :func:`repair_refusal_async` look at what a tool returned or raised,
  recognise a refused sign-in for the service behind that tool (a connector
  toolkit, the Google toolkit, an OAuth MCP server, resolved by
  :func:`connected_service_for_tool`), attempt the service's own recovery
  (a forced connector refresh, a Google token refresh, an MCP token refresh),
  retry the call once, and otherwise mark the connection and return the
  ``[connector-auth]`` result. No toolkit has to know any of this.

The result is the same shape everywhere: what happened, that retrying is
pointless, and the fixes to *offer* the person in one message, in the order
that suits this box (``magic_link_connect_service`` first, a sign-in code or a
pasted token where the provider allows one, the dashboard's Disconnect then
Connect as the manual route) plus the alternatives ``self_find_integration``
can list when they cannot reconnect right now.

Runtime-free and never raising: every function here runs inside a tool call
that must produce a result, and a broker hiccup must not turn the report of
one failure into a second one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Every failure message starts with this so a reader (or a test) can tell a
#: connection refusal from any other tool error at a glance. The runner's
#: loop breaker counts a result carrying it as a failed call.
CONNECTOR_AUTH_MARKER = "[connector-auth]"

#: Returned by the repair functions when the outcome was not a refused
#: sign-in at all: the boundary then handles it as it always did.
NOT_A_REFUSAL = object()

# What a refused sign-in reads like, across the services the toolkits speak to
# (GitHub "Bad credentials", Graph "InvalidAuthenticationToken", Salesforce
# "INVALID_SESSION_ID", Slack "invalid_auth"/"token_revoked", Meta
# "OAuthException" 190, LinkedIn "EXPIRED_ACCESS_TOKEN", Shopify "Invalid API
# key or access token", google-auth "invalid_grant", an MCP server's own
# words). Deliberately without "403"/"forbidden": a permission the grant never
# had is a scope problem, and a reconnect would not change it.
_REFUSAL_MARKERS: tuple[str, ...] = (
    "401",
    "unauthorized",
    "unauthorised",
    "unauthenticated",
    "invalid_token",
    "invalid token",
    "token expired",
    "token has expired",
    "expired token",
    "expired_token",
    "expired_access_token",
    "access token has expired",
    "access token is expired",
    "invalid_grant",
    "invalid_session_id",
    "invalid session",
    "session expired",
    "session has expired",
    "bad credentials",
    "invalid credentials",
    "invalidauthenticationtoken",
    "authentication failed",
    "authenticationfailed",
    "authentication error",
    "not authenticated",
    "not_authed",
    "invalid_auth",
    "token_revoked",
    "token_expired",
    "account_inactive",
    "error validating access token",
    "oauthexception",
    "invalid api key or access token",
    "re-authorize",
    "reauthorize",
    "authorization required",
    "needs authorization",
    "invalid_client",
)
# A credential that is absent is not one that was refused: the missing-key
# guidance the toolkits already carry covers that case, and a reconnect story
# would send the person to fix a connection that never existed.
_MISSING_MARKERS: tuple[str, ...] = (
    "[missing-credentials]",
    "not set",
    "credentials not found",
    "no credentials",
    "is not configured",
)
_ERROR_PREFIXES: tuple[str, ...] = ("[tool-error]", "error", "⚠️", "[blocked]", "failed")
_REFUSAL_TEXT_LIMIT = 4000
_DETAIL_LIMIT = 300


@dataclass(frozen=True)
class ConnectorRecovery:
    """Outcome of :func:`recover_connector_credential`."""

    provider_id: str
    provider_name: str
    #: A fresh value to retry with, when the broker could refresh the record.
    value: str | None
    #: Why nothing fresh could be produced (empty when ``value`` is set).
    detail: str

    @property
    def recovered(self) -> bool:
        return bool(self.value)


@dataclass(frozen=True)
class ConnectedService:
    """The signed-in service behind a tool: who refused, and how it reconnects.

    ``kind`` is ``"connector"`` (the OAuth connector broker), ``"google"``
    (the bespoke Google flow) or ``"mcp"`` (an OAuth MCP server). ``id`` is
    what ``magic_link_connect_service`` takes for it: the provider id,
    ``"google"``, or the server URL. ``credentials`` are the names the
    toolkit reads (connector kind), so the message can name the one a pasted
    token would land under.
    """

    kind: str
    id: str
    name: str
    credentials: tuple[str, ...] = ()


# ── helpers ─────────────────────────────────────────────────────────────────


def _credential_agent() -> str | None:
    try:
        from olano.tools._credentials import credential_agent_name

        return credential_agent_name()
    except Exception:
        return None


def _running_agent() -> str | None:
    """The agent whose config (and MCP summary) this turn belongs to."""
    try:
        from olano.tools.talk_to import current_agent

        name = current_agent.get()
        if name:
            return str(name)
    except Exception:
        pass
    return _credential_agent()


def _clip(text: str, limit: int = _DETAIL_LIMIT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def connector_for_credential(name: str) -> Any | None:
    """The connector provider that serves credential *name*, or ``None``.

    Registry-only (does not say whether a connection is stored): it is how the
    failure message knows which ``magic_link_connect_service`` id to name even
    for a credential that today comes from the vault.
    """
    try:
        from olano.core.connectors import exposing_provider

        found = exposing_provider(name)
    except Exception:
        return None
    return found[0] if found else None


def _toolkit_of(tool_name: str) -> str:
    try:
        from olano.tools._name_index import NAME_INDEX

        module = str(NAME_INDEX.get(tool_name) or "")
    except Exception:
        return ""
    return module.rsplit(".", 1)[-1].removesuffix("_tools") if module else ""


def _toolkit_credentials(toolkit: str) -> tuple[str, ...]:
    try:
        from olano.tools.requirements import BUNDLE_REQUIREMENTS

        specs = (BUNDLE_REQUIREMENTS.get(toolkit) or {}).get("env_vars") or []
        return tuple(str(s.get("name")) for s in specs if s.get("name"))
    except Exception:
        return ()


def connected_service_for_tool(
    tool_name: str, metadata: Mapping[str, Any] | None = None
) -> ConnectedService | None:
    """The signed-in service *tool_name* authenticates with, or ``None``.

    An MCP tool carries its server on ``metadata`` (``mcp_server_url`` +
    ``mcp_auth``, set by the builder at connect time); only OAuth servers
    count, since a header-authenticated one has nothing to reconnect. A
    native tool resolves through the generated name index to its toolkit,
    then to a connector by the credentials that toolkit reads (any name a
    registered provider exposes) or by the finder's toolkit map; the Google
    toolkit is its own kind. Anything else (a keyed toolkit, a local tool)
    returns ``None`` and the boundary's ordinary hints apply.
    """
    meta = dict(metadata or {})
    url = str(meta.get("mcp_server_url") or "")
    if url:
        if str(meta.get("mcp_auth") or "none") != "oauth":
            return None
        host = urlparse(url).hostname or url
        return ConnectedService(kind="mcp", id=url, name=host)
    toolkit = _toolkit_of(tool_name)
    if not toolkit:
        return None
    try:
        from olano.admin.integrations import GOOGLE_TOOLKITS, TOOLKIT_PROVIDER
    except Exception:  # pragma: no cover, admin package unavailable
        return None
    if toolkit == "google" or (toolkit in GOOGLE_TOOLKITS and toolkit != "youtube"):
        return ConnectedService(kind="google", id="google", name="Google")
    names = _toolkit_credentials(toolkit)
    provider = None
    served: list[str] = []
    for name in names:
        candidate = connector_for_credential(name)
        if candidate is not None:
            provider = provider or candidate
            if candidate.id == provider.id:
                served.append(name)
    if provider is None:
        provider_id = TOOLKIT_PROVIDER.get(toolkit)
        if not provider_id:
            return None
        try:
            from olano.core.connectors import get_provider

            provider = get_provider(provider_id)
        except Exception:
            provider = None
        if provider is None:
            return None
        served = [n for n in provider.exposes if n in names] or list(provider.exposes)[:1]
    return ConnectedService(
        kind="connector", id=provider.id, name=provider.name, credentials=tuple(served)
    )


# ── recognising a refusal ───────────────────────────────────────────────────


def is_auth_refusal(text: str) -> bool:
    """Whether *text* reads as a service refusing the credential it was sent."""
    low = str(text or "").lower()
    if not low or any(m in low for m in _MISSING_MARKERS):
        return False
    return any(m in low for m in _REFUSAL_MARKERS)


def _result_text(result: Any) -> str | None:
    """The text of a tool result, when it has one: a string, or content blocks."""
    if isinstance(result, str):
        return result
    if isinstance(result, tuple) and result:
        return _result_text(result[0])
    if isinstance(result, list):
        parts: list[str] = []
        for block in result:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts) if parts else None
    return None


def refusal_text(result: Any) -> str | None:
    """The refusal in a tool *result*, or ``None`` when it is not one.

    Only an error-shaped result counts (a ``[tool-error]`` / ``Error …``
    string, or a short block list), and never one already carrying
    :data:`CONNECTOR_AUTH_MARKER`: a real payload that happens to mention
    "401" must pass through untouched.
    """
    text = _result_text(result)
    if text is None:
        return None
    stripped = text.strip()
    if not stripped or len(stripped) > _REFUSAL_TEXT_LIMIT:
        return None
    if stripped.startswith(CONNECTOR_AUTH_MARKER):
        return None
    low = stripped.lower()
    error_shaped = low.startswith(_ERROR_PREFIXES) or "[tool-error]" in low or len(low) < 600
    if not error_shaped or not is_auth_refusal(stripped):
        return None
    return _clip(stripped)


def refusal_text_from_exception(exc: BaseException) -> str | None:
    """The refusal an exception carries, or ``None`` when it is another failure."""
    try:
        from olano.core.mcp_setup import needs_authorization

        if needs_authorization(exc):
            return "OAuth authorization required: the server no longer accepts the stored grant"
    except Exception:
        pass
    text = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, BaseExceptionGroup):
        text = " | ".join(f"{type(e).__name__}: {e}" for e in exc.exceptions) or text
    return _clip(text) if is_auth_refusal(text) else None


# ── recovery ────────────────────────────────────────────────────────────────


def recover_connector_credential(name: str, *, error: str = "") -> ConnectorRecovery | None:
    """Try to obtain a working value for *name* after the service refused the current one.

    ``None`` when *name* is not served by a stored connector record for the
    current agent (the value came from the vault, ``config.env`` or the
    environment, or the provider is unknown to the broker): there is nothing
    to refresh, and the caller goes straight to :func:`connector_auth_failure`.
    Otherwise a :class:`ConnectorRecovery` whose ``value`` is the refreshed
    credential, or ``None`` with ``detail`` saying why the refresh could not
    help.
    """
    try:
        from olano.core.connectors import (
            exposing_provider,
            extract_exposed_field,
            get_live_record,
            refresh_record_now,
        )

        found = exposing_provider(name)
        if found is None:
            return None
        provider, field_path = found
        agent = _credential_agent()
        if get_live_record(provider, agent) is None:
            return None
        logger.info(
            "connector[%s]: %s refused by the service (%s); refreshing the token",
            provider.id,
            name,
            _clip(error, 160) or "no detail",
        )
        fresh, why = refresh_record_now(provider, agent)
        if fresh is None:
            return ConnectorRecovery(provider.id, provider.name, None, why)
        return ConnectorRecovery(
            provider.id, provider.name, extract_exposed_field(fresh, field_path), ""
        )
    except Exception as exc:  # the report of one failure must not become a second
        logger.debug("recover_connector_credential(%s) failed: %s", name, exc)
        return None


def _connector_source(service: ConnectedService, agent: str | None) -> str:
    """``"record"`` (a broker record), ``"hand_set"`` (vault/env value) or ``"none"``."""
    try:
        from olano.core.connectors import get_live_record, get_provider

        provider = get_provider(service.id)
        if provider is not None and get_live_record(provider, agent) is not None:
            return "record"
    except Exception:
        pass
    try:
        from olano.tools._credentials import get_credential

        if any(get_credential(name) for name in service.credentials):
            return "hand_set"
    except Exception:
        pass
    return "none"


def try_recover(service: ConnectedService) -> tuple[bool, str]:
    """Attempt the service's own repair for a refused sign-in (blocking).

    Connector: a forced record refresh. Google: a forced token refresh. MCP:
    a token refresh, when no event loop runs on this thread (the MCP tools
    are async, so the async variant is the one that matters). Returns
    ``(recovered, why_not)``.
    """
    try:
        if service.kind == "connector":
            from olano.core.connectors import get_provider, refresh_record_now

            provider = get_provider(service.id)
            if provider is None:
                return False, "unknown connector"
            fresh, why = refresh_record_now(provider, _credential_agent())
            return (fresh is not None), why
        if service.kind == "google":
            from olano.tools.google_tools import refresh_credentials_now

            return refresh_credentials_now()
        if service.kind == "mcp":
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(_refresh_mcp(service))
            return False, "token refresh needs the async path"
    except Exception as exc:
        return False, f"recovery failed: {exc}"
    return False, "no recovery for this service"


async def try_recover_async(service: ConnectedService) -> tuple[bool, str]:
    """Async twin of :func:`try_recover`; the MCP refresh runs natively here."""
    if service.kind == "mcp":
        try:
            return await _refresh_mcp(service)
        except Exception as exc:
            return False, f"recovery failed: {exc}"
    return await asyncio.to_thread(try_recover, service)


async def _refresh_mcp(service: ConnectedService) -> tuple[bool, str]:
    from olano.core.mcp_setup import refresh_oauth_token

    agent = _credential_agent() or _running_agent() or ""
    outcome = await refresh_oauth_token(agent, service.id)
    if outcome.get("ok"):
        return True, ""
    return False, f"{outcome.get('status', 'refresh failed')}: {outcome.get('message', '')}".strip(
        ": "
    )


# ── marking + the message ───────────────────────────────────────────────────


def _mark(service: ConnectedService, *, tool: str, detail: str) -> str:
    """Make the dashboard honest about the refusal; returns the scope phrase."""
    stamp = f"{tool}: {detail}"
    try:
        if service.kind == "connector":
            from olano.core.connectors import connector_auth_status, get_provider, mark_auth_failed

            provider = get_provider(service.id)
            agent = _credential_agent()
            if provider is None:
                return "the stored connection"
            mark_auth_failed(provider, agent, error=stamp)
            scope = str(connector_auth_status(provider, agent).get("scope") or "")
            return {
                "agent": "this agent's own connection",
                "shared": "a connection shared from another agent",
                "system": "the deployment-wide (System scope) connection",
            }.get(scope, "the stored connection")
        if service.kind == "google":
            from olano.tools.google_tools import mark_google_auth_failed

            mark_google_auth_failed(stamp)
            return "the Google connection"
        if service.kind == "mcp":
            from olano.core.builder import mark_mcp_auth_required

            agent = _running_agent()
            if agent:
                mark_mcp_auth_required(agent, service.id, stamp)
            return "this agent's authorization for the server"
    except Exception as exc:
        logger.debug("marking %s after a refusal failed: %s", service.id, exc)
    return "the stored connection"


def _connector_options(service: ConnectedService) -> list[str]:
    """The reconnect routes this box actually offers for a connector, in order."""
    official = byo = device = False
    try:
        from olano.core.connectors import connect_methods, get_provider

        provider = get_provider(service.id)
        if provider is not None:
            methods = connect_methods(provider)
            official = bool(methods.get("official_oauth"))
            byo = bool(methods.get("byo_oauth"))
            device = bool(methods.get("device"))
    except Exception:
        pass
    via = (
        "through Olano's own app"
        if official
        else "through the operator's OAuth app"
        if byo
        else "(the link says which OAuth client this box still needs, if any)"
    )
    options = [
        f'send a reconnect link with magic_link_connect_service("{service.id}") {via}, so the '
        "user signs in again from their own browser; the new sign-in replaces the broken "
        "connection in place, no Disconnect needed"
    ]
    if device:
        options.append(
            f'a sign-in code with magic_link_connect_service("{service.id}", method="device"): '
            f"the user types a short code on {service.name}'s site, no app to register"
        )
    if service.credentials:
        options.append(
            f"if the user already holds a personal access token for {service.name}, store it "
            f'with magic_link_request_secret("{service.credentials[0]}"); it works until it expires '
            "and nothing renews it"
        )
    options.append(
        f"or the user opens the dashboard's Connections page, {service.name}, Disconnect, then Connect again"
    )
    return options


def _numbered(options: list[str]) -> str:
    return " ".join(f"({i}) {text}." for i, text in enumerate(options, start=1))


def refusal_message(
    service: ConnectedService,
    *,
    tool: str,
    error: str,
    recovered: bool | None,
    detail: str = "",
    source: str = "record",
) -> str:
    """The tool result for a sign-in the service refuses even after recovery.

    Marks the connection (so the Connections card or MCP tab reads *Needs
    reconnecting* / *Needs authorization*) and tells the model what happened,
    that retrying is pointless, and what to offer: the reconnect routes this
    box has, then the alternatives ``self_find_integration`` can list. *source*
    is ``"record"`` (a stored connection), ``"hand_set"`` (a vault/env token,
    nothing renews it) or ``"none"`` (no connection at all).
    """
    detail_text = _clip(error) or "the service refused the credential"
    if recovered is None:
        why = "nothing could be refreshed"
    elif recovered:
        why = f"a token refresh was attempted and {service.name} still refused the result"
    else:
        why = f"it could not be renewed ({_clip(detail, 200) or 'no refresh grant'})"
    common_tail = (
        "Nothing about this request was wrong, and retrying it unchanged will fail the same "
        "way. Do not stop at reporting the error: tell the user plainly that the sign-in for "
        f"{service.name} has expired or been revoked, and offer the fix in the same message. "
    )
    alternatives = (
        f'If they cannot reconnect right now, self_find_integration("{service.name}") lists '
        "every other supported route to the same service (an MCP server, a pasted key), and "
        "you can say which parts of their request are possible without it. Once reconnected, "
        "retry this request."
    )

    if service.kind == "mcp":
        scope = _mark(service, tool=tool, detail=detail_text)
        options = [
            f'send a sign-in link with magic_link_connect_service("{service.id}"): a '
            "PIN-protected page the user opens in their own browser; when they finish, your "
            "tools from that server reload on their own and this chat gets a notice",
            f'or self_authorize_mcp_server("{service.id}") for the bare provider sign-in URL '
            "with the same watcher",
            "or the user opens this agent's MCP tab in the dashboard, finds the server and "
            "clicks Authorize",
        ]
        return (
            f"{CONNECTOR_AUTH_MARKER} The MCP server {service.name} ({service.id}) refused the "
            f"saved authorization ({detail_text}) and {why}, so {scope} now reads "
            f'"Needs authorization" in the MCP tab. {common_tail}{_numbered(options)} '
            f"{alternatives}"
        )

    if service.kind == "google":
        if source == "none":
            return (
                f"{CONNECTOR_AUTH_MARKER} No Google connection is stored for this agent "
                f"({detail_text}). {common_tail}"
                + _numbered(
                    [
                        'connect Google with magic_link_connect_service("google"): the user '
                        "signs in from their own browser and the tools work on the next call",
                        "or the user opens the dashboard's Connections page, Google, Connect",
                    ]
                )
                + f" {alternatives}"
            )
        scope = _mark(service, tool=tool, detail=detail_text)
        options = [
            'send a reconnect link with magic_link_connect_service("google"), so the user '
            "signs in again from their own browser; the new sign-in replaces the broken one in "
            "place, no Disconnect needed",
            "or the user opens the dashboard's Connections page, Google, Disconnect, then "
            "Connect again",
        ]
        return (
            f"{CONNECTOR_AUTH_MARKER} Google refused the saved sign-in ({detail_text}) and "
            f'{why}, so the Connections card for Google was stale: it now reads "Needs '
            f'reconnecting" ({scope}). {common_tail}{_numbered(options)} {alternatives}'
        )

    # connector kind
    if source == "hand_set":
        name = service.credentials[0] if service.credentials else "the credential"
        return (
            f"{CONNECTOR_AUTH_MARKER} {service.name} refused the {name} credential "
            f"({detail_text}). That value is a hand-set token (vault, config env or "
            "environment) and the service expires those on its own schedule; nothing renews "
            f"them. {common_tail}"
            + _numbered(
                [
                    f"the durable route is to connect {service.name} properly with "
                    f'magic_link_connect_service("{service.id}") (or the dashboard\'s '
                    "Connections page), which mints tokens that refresh on their own",
                    f'the quick one is a fresh token stored with magic_link_request_secret("{name}")',
                ]
            )
            + f" {alternatives}"
        )
    if source == "none":
        return (
            f"{CONNECTOR_AUTH_MARKER} No {service.name} connection is stored for this agent "
            f"({detail_text}). {common_tail}"
            + _numbered(
                _connector_options(service)[:1]
                + ["or the user opens the dashboard's Connections page and connects it there"]
            )
            + f" {alternatives}"
        )
    scope = _mark(service, tool=tool, detail=detail_text)
    return (
        f"{CONNECTOR_AUTH_MARKER} {service.name} refused the saved sign-in ({detail_text}) and "
        f'{why}, so the Connections tab\'s "Connected" badge was stale: it now reads '
        f'"Needs reconnecting" ({scope}). {common_tail}{_numbered(_connector_options(service))} '
        f"{alternatives}"
    )


def connector_auth_failure(
    name: str,
    *,
    tool: str,
    error: str,
    recovery: ConnectorRecovery | None,
) -> str:
    """Toolkit-side entry: the result for credential *name* the service refuses.

    When *recovery* is not ``None`` the credential is a connector record and
    the message is the reconnect story; otherwise the credential was hand-set
    (a vault/env token) and the fix is a proper connection or a fresh token.
    """
    provider = connector_for_credential(name)
    if recovery is not None:
        service = ConnectedService(
            kind="connector",
            id=recovery.provider_id,
            name=recovery.provider_name,
            credentials=(name,),
        )
        return refusal_message(
            service,
            tool=tool,
            error=error,
            recovered=recovery.recovered,
            detail=recovery.detail,
            source="record",
        )
    service = ConnectedService(
        kind="connector",
        id=provider.id if provider else "",
        name=provider.name if provider else name,
        credentials=(name,),
    )
    return refusal_message(service, tool=tool, error=error, recovered=None, source="hand_set")


# ── the generic seam (called by the error boundary) ─────────────────────────


def _source_for(service: ConnectedService) -> str:
    if service.kind == "connector":
        return _connector_source(service, _credential_agent())
    if service.kind == "google":
        try:
            from olano.tools.google_tools import _token_path

            return "record" if _token_path().exists() else "none"
        except Exception:
            return "record"
    return "record"


def repair_refusal_sync(
    tool_name: str,
    metadata: Mapping[str, Any] | None,
    *,
    result: Any = None,
    exc: BaseException | None = None,
    retry: Callable[[], Any] | None = None,
) -> Any:
    """Repair or explain a refused sign-in behind a finished tool call (blocking).

    *result* is what the tool returned, or *exc* what it raised. Returns
    :data:`NOT_A_REFUSAL` when the outcome is not a refused sign-in for a
    connected service; otherwise the retried call's result when the service's
    own recovery worked, or the ``[connector-auth]`` message. A retry that
    raises something other than a refusal propagates, so the boundary treats
    it as it would any exception.
    """
    service = connected_service_for_tool(tool_name, metadata)
    if service is None:
        return NOT_A_REFUSAL
    text = refusal_text_from_exception(exc) if exc is not None else refusal_text(result)
    if text is None:
        return NOT_A_REFUSAL
    source = _source_for(service)
    recovered: bool | None = None
    detail = ""
    if source == "record":
        recovered, detail = try_recover(service)
        if recovered and retry is not None:
            try:
                second = retry()
            except Exception as exc2:
                again = refusal_text_from_exception(exc2)
                if again is None:
                    raise
                text = again
            else:
                again = refusal_text(second)
                if again is None:
                    logger.info("tool %r: repaired a refused sign-in for %s", tool_name, service.id)
                    return second
                text = again
    return refusal_message(
        service, tool=tool_name, error=text, recovered=recovered, detail=detail, source=source
    )


async def repair_refusal_async(
    tool_name: str,
    metadata: Mapping[str, Any] | None,
    *,
    result: Any = None,
    exc: BaseException | None = None,
    retry: Callable[[], Awaitable[Any]] | None = None,
) -> Any:
    """Async twin of :func:`repair_refusal_sync` (the path every MCP tool takes)."""
    service = connected_service_for_tool(tool_name, metadata)
    if service is None:
        return NOT_A_REFUSAL
    text = refusal_text_from_exception(exc) if exc is not None else refusal_text(result)
    if text is None:
        return NOT_A_REFUSAL
    source = _source_for(service)
    recovered: bool | None = None
    detail = ""
    if source == "record":
        recovered, detail = await try_recover_async(service)
        if recovered and retry is not None:
            try:
                second = await retry()
            except Exception as exc2:
                again = refusal_text_from_exception(exc2)
                if again is None:
                    raise
                text = again
            else:
                again = refusal_text(second)
                if again is None:
                    logger.info("tool %r: repaired a refused sign-in for %s", tool_name, service.id)
                    return second
                text = again
    return refusal_message(
        service, tool=tool_name, error=text, recovered=recovered, detail=detail, source=source
    )
