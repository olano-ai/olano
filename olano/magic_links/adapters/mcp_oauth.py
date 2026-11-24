"""MCP server OAuth — Zoho, HubSpot, and any other remote MCP.

MCP OAuth was already chat-reachable via ``olano_authorize_mcp_server``: the
tool returns a sign-in URL, the agent pastes it into the chat, and a watcher
finishes the job. This adapter wraps the same
:func:`olano.core.mcp_setup.start_mcp_authorization` behind the magic link
page so it gains what the bare link lacks — a PIN gate, live status the user can
watch, and one consistent look with every other service.

**Two phases, one link.** Most remote servers register a client on the fly
(Dynamic Client Registration), so the page is a single hand-off button. Some —
HubSpot is the common case — refuse DCR and only accept an OAuth app the user
created themselves, which means a **Client ID** and **Client secret** and a
redirect URL registered on the provider's side. The page used to learn that
the hard way: the flow failed, the link died, and the chat got a paragraph
telling the user to go find the MCP server settings. Now:

* a server the directory marks ``oauthClient: preregistered`` opens the link
  **on the credentials form** (``mode: credentials``) — the redirect URL to
  register, a link to the provider's console, the two fields — and the
  sign-in starts the moment they are saved;
* a server nobody knew about that turns out to refuse DCR **comes back to the
  same form** instead of ending the link (``state()`` maps the session's
  ``dcr_unsupported`` onto ``pending`` + ``needs_client``), so one link
  survives the discovery;
* an entry that already carries a client id (typed in the dashboard) skips
  the form and signs in with it — ``start()`` merges the agent's configured
  entry, which the previous version never read.

The form also offers **Composio** as the no-OAuth-app route: a ready-made
message the person can paste back to the agent, worded for whether the agent
already has the Composio MCP connected.

Credentials go **into the agent's MCP server entry**, never into the link
record: the runtime needs them there anyway (a static client's token refresh
sends the secret), it is where the dashboard's own Authorize dialog puts them,
and ``links.json`` must never hold a secret. That write happens when the user
saves the form — the one deliberate exception to "an abandoned link writes
nothing", because typing a client secret *is* the configuration act.

The underlying redirect precedence is untouched (**relay → public origin**), so
a deployment already configured for MCP OAuth keeps working exactly as before.
``post_complete`` hot-reloads the agent's MCP client, which is what makes the
newly authorized tools usable without a restart — the same reload the dashboard
fires when its poll observes ``authorized``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from olano.magic_links.flows import (
    APPLY_NONE,
    APPLY_RELOADED,
    APPLY_RESTART_NEEDED,
    APPLY_SELF,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_EXPIRED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    BaseFlow,
    FlowField,
    register,
)
from olano.magic_links.store import LinkRecord

logger = logging.getLogger(__name__)

_STATUS_MAP = {
    "starting": STATUS_IN_PROGRESS,
    "awaiting_authorization": STATUS_IN_PROGRESS,
    "exchanging": STATUS_IN_PROGRESS,
    "authorized": STATUS_DONE,
    "error": STATUS_ERROR,
    "expired": STATUS_EXPIRED,
}

#: Directory value meaning "bring your own OAuth app" (the other is ``dcr``).
PREREGISTERED = "preregistered"

#: The session error code the OAuth flow sets when a provider refuses DCR.
DCR_UNSUPPORTED = "dcr_unsupported"

#: Why the credentials form is showing — decides the lead sentence.
REASON_KNOWN = "known"  # the directory said so up front
REASON_REJECTED = "rejected"  # the provider refused automatic registration
REASON_FAILED = "failed"  # a sign-in with the entered credentials failed

# ── Composio, the no-OAuth-app alternative ───────────────────────────────────
# Fallbacks only: the live values come from the directory row (`get_server`),
# so a cloud-side fix reaches every box without a release.
COMPOSIO_SLUG = "composio"
COMPOSIO_MCP_URL = "https://connect.composio.dev/mcp"
COMPOSIO_SIGNUP_URL = "https://composio.dev"
#: Composio's free plan as of September 2026 ("Totally Free": 20,000 tool
#: calls a month, no card). Shown as a number on the page, so keep it current.
COMPOSIO_FREE_CALLS_PER_MONTH = 20_000

_MAX_CREDENTIAL_CHARS = 512

_FIELDS = (
    FlowField(
        name="client_id",
        label="Client ID",
        placeholder="The app's Client ID",
        help=(
            "Issued by the provider when you create the OAuth app. Copied as-is: "
            "letters, digits and dashes, no spaces."
        ),
    ),
    FlowField(
        name="client_secret",
        label="Client secret",
        secret=True,
        required=False,
        placeholder="The app's Client secret",
        help=(
            "Shown once when the app is created. Stored encrypted in the vault, for "
            "this agent only — never in this chat or in a config file. Leave it empty "
            "only if the provider issued no secret (a public, PKCE-only app)."
        ),
    ),
)


def _host(url: str) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def directory_facts(url: str) -> dict[str, str]:
    """What the MCP directory knows about a server URL, trimmed to what the
    page needs. Empty strings when the server is not in the directory."""
    from olano.core import mcp_registry

    try:
        entry = mcp_registry.find_by_url(url) or {}
    except Exception:  # the directory must never break a mint
        logger.debug("magic-link: directory lookup failed for %s", url, exc_info=True)
        entry = {}
    return {
        "slug": str(entry.get("slug") or ""),
        "name": str(entry.get("name") or ""),
        "oauth_client": str(entry.get("oauthClient") or ""),
        "console_url": str(entry.get("appConsoleUrl") or ""),
        "console_label": str(entry.get("appConsoleLabel") or ""),
        "docs_url": str(entry.get("docsUrl") or ""),
        "notes": str(entry.get("notes") or ""),
        "transport": str(entry.get("transport") or ""),
    }


def configured_entry(agent: str, url: str) -> dict[str, Any] | None:
    """The agent's own entry for ``url`` (a ``MCPServerConfig`` dump), or ``None``."""
    if not agent or not url:
        return None
    try:
        from olano.admin.mcp import list_mcp_servers

        return next((s for s in list_mcp_servers(agent) if s.get("url") == url), None)
    except Exception:  # unknown agent, unreadable config: no entry
        logger.debug("magic-link: could not read %s's MCP servers", agent, exc_info=True)
        return None


def composio_status(agent: str) -> dict[str, Any]:
    """Whether ``agent`` already has the Composio MCP connected, plus the URLs
    the page links to. Connected = a composio.dev server on the agent that is
    enabled and, for OAuth, holds tokens."""
    from olano.core import mcp_registry

    entry: dict[str, Any] = {}
    try:
        entry = mcp_registry.get_server(COMPOSIO_SLUG) or {}
    except Exception:
        entry = {}
    connected = False
    try:
        from olano.admin.mcp import list_mcp_servers
        from olano.core.mcp_setup import has_oauth_tokens

        for server in list_mcp_servers(agent) if agent else []:
            url = str(server.get("url") or "")
            if not server.get("enabled", True) or not _host(url).endswith("composio.dev"):
                continue
            if str(server.get("auth") or "none") == "oauth":
                connected = has_oauth_tokens(agent, url)
            else:  # an API-key instance: the header is the credential
                connected = True
            if connected:
                break
    except Exception:
        logger.debug("magic-link: composio status failed for %s", agent, exc_info=True)
    return {
        "connected": connected,
        "url": str(entry.get("url") or COMPOSIO_MCP_URL),
        "signup_url": str(entry.get("homepageUrl") or COMPOSIO_SIGNUP_URL),
    }


def composio_message(service_name: str, *, connected: bool) -> str:
    """The message the person pastes back to the agent to take the Composio route."""
    target = service_name or "that platform"
    if connected:
        return f"Help me connect to {target} via the Composio MCP."
    return (
        "First give me a magic link to connect to the Composio MCP, and after "
        f"that help me connect to {target} via the Composio MCP."
    )


def redirect_uri() -> str:
    """The callback the OAuth app must register — exactly the one the flow
    will send (relay → public origin). Empty when neither is configured."""
    from olano.core.mcp_setup import _resolve_redirect_uri

    try:
        return _resolve_redirect_uri({}, None)[0]
    except ValueError:
        return ""


class McpOAuthFlow(BaseFlow):
    id = "mcp-oauth"
    label = "MCP server"
    #: A bespoke page kind: a credentials form when the server needs an OAuth
    #: app of the user's own, the redirect-style hand-off otherwise.
    kind = "mcp"
    # post_complete hot-reloads the agent's MCP client — the whole point of
    # that hook is that the new tools are usable without a restart.
    applies = APPLY_SELF
    instructions = "Approve access at the provider; the agent's new tools appear right after."
    fields = _FIELDS

    # ── mint ─────────────────────────────────────────────────────────────────

    def validate(self, params: dict[str, Any]) -> dict[str, Any]:
        agent = str(params.get("agent") or "").strip()
        url = str(params.get("url") or "").strip()
        if not agent:
            raise ValueError("An agent is required — MCP tokens are stored per agent.")
        if not url:
            raise ValueError("The MCP server URL is required.")
        out: dict[str, Any] = {"agent": agent, "url": url, "service": url}
        for key in ("transport", "oauth_scope", "oauth_client_id", "oauth_client_secret"):
            value = str(params.get(key) or "").strip()
            if value:
                out[key] = value

        facts = directory_facts(url)
        out["directory"] = facts
        existing = configured_entry(agent, url)
        has_client = bool(out.get("oauth_client_id") or (existing or {}).get("oauth_client_id"))
        # Open on the form only when the directory says the server wants an
        # app of the user's own AND nothing already supplies one. An unknown
        # server starts with the hand-off and falls back to the form if the
        # provider refuses DCR (see `state`).
        out["needs_client"] = facts["oauth_client"] == PREREGISTERED and not has_client
        out["retry_reason"] = REASON_KNOWN if out["needs_client"] else ""
        out["retry_error"] = ""
        out["client_submitted"] = False
        out["composio"] = composio_status(agent)
        return out

    # ── presentation ─────────────────────────────────────────────────────────

    def _label(self, link: LinkRecord) -> str:
        """The directory's product name, else the host — precise for a server
        nobody has catalogued."""
        facts = link.params.get("directory") or {}
        if facts.get("name"):
            return str(facts["name"])
        url = str(link.params.get("url") or "")
        return _host(url) or url or "the MCP server"

    def descriptor(self, link: LinkRecord) -> dict[str, Any]:
        base = super().descriptor(link)
        name = self._label(link)
        facts = link.params.get("directory") or {}
        base["service"] = {"id": str(facts.get("slug") or "mcp"), "name": name}
        base["title"] = f"Connect {name}"
        if not link.params.get("needs_client"):
            base["mode"] = "signin"
            base["fields"] = []
            return base

        agent = str(link.params.get("agent") or "the agent")
        composio = dict(link.params.get("composio") or {})
        reason = str(link.params.get("retry_reason") or REASON_KNOWN)
        base["mode"] = "credentials"
        base["reason"] = reason
        base["instructions"] = self._credentials_lead(name, reason)
        base["retry_error"] = str(link.params.get("retry_error") or "")
        base["cta"] = f"Save and continue to {name}"
        base["redirect_uri"] = redirect_uri()
        base["guide"] = {
            "console_url": str(facts.get("console_url") or ""),
            "console_label": str(facts.get("console_label") or ""),
            "docs_url": str(facts.get("docs_url") or ""),
            "notes": str(facts.get("notes") or ""),
        }
        connected = bool(composio.get("connected"))
        base["composio"] = {
            "connected": connected,
            "agent": agent,
            "signup_url": str(composio.get("signup_url") or COMPOSIO_SIGNUP_URL),
            "free_calls_per_month": COMPOSIO_FREE_CALLS_PER_MONTH,
            "message": composio_message(name, connected=connected),
        }
        return base

    @staticmethod
    def _credentials_lead(name: str, reason: str) -> str:
        if reason == REASON_REJECTED:
            return (
                f"{name} refused the automatic sign-in: it only accepts an OAuth app "
                "of your own. Create one, paste its details below, and the sign-in "
                "continues from here."
            )
        if reason == REASON_FAILED:
            return (
                f"The sign-in with those credentials did not go through. Check the "
                f"Client ID, the secret and the redirect URL registered in {name}, "
                "then try again."
            )
        return (
            f"{name} does not register apps automatically, so it needs an OAuth app "
            "of your own. Paste its Client ID and secret below; the sign-in "
            "continues from here."
        )

    # ── the underlying session ───────────────────────────────────────────────

    def _flow_entry(self, link: LinkRecord, saved: dict[str, Any] | None = None) -> dict[str, Any]:
        """The server entry the OAuth session runs with: the agent's configured
        entry (its transport, headers, scope and any client credentials typed
        in the dashboard) with the link's own params layered on top."""
        agent = str(link.params.get("agent") or "")
        url = str(link.params.get("url") or "")
        entry = dict(saved or configured_entry(agent, url) or {})
        if not entry:
            entry = {"url": url, "transport": link.params.get("transport") or "auto"}
        elif link.params.get("transport"):
            entry["transport"] = link.params["transport"]
        entry["url"] = url
        entry["auth"] = "oauth"
        for key in ("oauth_scope", "oauth_client_id", "oauth_client_secret"):
            if link.params.get(key):
                entry[key] = link.params[key]
        return entry

    def start(self, link: LinkRecord) -> dict[str, Any]:
        if link.params.get("needs_client"):
            raise ValueError("Enter the Client ID and Client secret first.")
        from olano.core.mcp_setup import start_mcp_authorization

        session = start_mcp_authorization(str(link.params["agent"]), self._flow_entry(link))
        return {"session_id": session.session_id}

    def state(self, link: LinkRecord) -> dict[str, Any]:
        from olano.core.mcp_setup import poll_oauth_flow

        if not link.session_id:
            return {"status": link.status}
        session = poll_oauth_flow(link.session_id)
        if session is None:
            return {"status": STATUS_EXPIRED, "error": "This sign-in attempt expired."}
        status = _STATUS_MAP.get(session.status, STATUS_IN_PROGRESS)
        if status == STATUS_ERROR and self._recoverable(link, session):
            # Not the end of the link: the provider wants an OAuth app of the
            # user's own (or the one they entered did not work). Back to the
            # form, with a fresh session once they save it.
            self._back_to_form(link, session)
            return {"status": STATUS_PENDING}
        out: dict[str, Any] = {
            "status": status,
            "authorization_url": session.authorization_url or "",
        }
        if status == STATUS_DONE:
            count = int(getattr(session, "tool_count", 0) or 0)
            tools = f" ({count} tool{'s' if count != 1 else ''})" if count else ""
            out["result"] = f"{link.params['agent']} is connected to {self._label(link)}{tools}."
        elif status in (STATUS_ERROR, STATUS_EXPIRED):
            out["error"] = session.error or ""
        return out

    @staticmethod
    def _recoverable(link: LinkRecord, session: Any) -> bool:
        """A failure the credentials form can fix.

        The DCR refusal always is — that is the whole reason the form exists.
        After the user has entered credentials on this link, any other failure
        is most likely a wrong id, secret or redirect URL, so the form comes
        back with the provider's words instead of a dead link.
        """
        if getattr(session, "error_code", None) == DCR_UNSUPPORTED:
            return True
        return bool(link.params.get("client_submitted"))

    def _back_to_form(self, link: LinkRecord, session: Any) -> None:
        rejected = getattr(session, "error_code", None) == DCR_UNSUPPORTED
        error = "" if rejected else str(getattr(session, "error", "") or "")
        agent = str(link.params.get("agent") or "")
        composio = composio_status(agent)

        def _apply(current: LinkRecord) -> None:
            current.params["needs_client"] = True
            current.params["retry_reason"] = REASON_REJECTED if rejected else REASON_FAILED
            current.params["retry_error"] = error[:300]
            current.params["composio"] = composio
            current.session_id = None

        self._mutate(link, _apply)
        logger.info(
            "magic-link: %s asked for client credentials on %s (%s)",
            self._label(link),
            agent,
            "DCR refused" if rejected else "sign-in failed",
        )

    # ── the credentials form ─────────────────────────────────────────────────

    async def submit(self, link: LinkRecord, fields: dict[str, str]) -> dict[str, Any]:
        if not link.params.get("needs_client"):
            raise ValueError("This link has nothing to fill in — tap Start instead.")
        client_id = str(fields.get("client_id") or "").strip()
        client_secret = str(fields.get("client_secret") or "").strip()
        if not client_id:
            raise ValueError("The Client ID is required.")
        if len(client_id) > _MAX_CREDENTIAL_CHARS or len(client_secret) > _MAX_CREDENTIAL_CHARS:
            raise ValueError("That does not look like a client credential — it is too long.")
        if any(ch.isspace() for ch in client_id):
            raise ValueError("A Client ID has no spaces — check what you pasted.")

        # Off the loop: the config write takes the interprocess deployment lock.
        saved = await asyncio.to_thread(self._store_client, link, client_id, client_secret)

        from olano.core.mcp_setup import start_mcp_authorization

        # Starting before flipping the form off keeps the form (and the
        # credentials just saved) in place if the flow cannot even begin —
        # the error then shows above the fields instead of on a dead page.
        session = start_mcp_authorization(str(link.params["agent"]), self._flow_entry(link, saved))

        def _apply(current: LinkRecord) -> None:
            current.params["needs_client"] = False
            current.params["client_submitted"] = True
            current.params["retry_reason"] = ""
            current.params["retry_error"] = ""
            current.params["oauth_client_id"] = client_id
            current.session_id = session.session_id

        self._mutate(link, _apply)
        return {"status": STATUS_IN_PROGRESS}

    def _store_client(self, link: LinkRecord, client_id: str, client_secret: str) -> dict[str, Any]:
        """Write the credentials onto the agent's server entry (creating the
        entry when the agent has none yet) and return the saved entry."""
        from olano.config.agent_config import MCPServerConfig
        from olano.config.loader import mutate_agent_config

        agent = str(link.params.get("agent") or "")
        url = str(link.params.get("url") or "")
        facts = link.params.get("directory") or {}
        transport = str(link.params.get("transport") or facts.get("transport") or "").lower()
        if transport not in ("sse", "http"):
            transport = "http"
        saved: dict[str, Any] = {}

        def _apply(cfg: Any) -> None:
            for server in cfg.mcp_servers:
                if server.url == url:
                    server.auth = "oauth"
                    server.oauth_client_id = client_id
                    server.oauth_client_secret = client_secret or None
                    if link.params.get("oauth_scope") and not server.oauth_scope:
                        server.oauth_scope = str(link.params["oauth_scope"])
                    saved.update(server.model_dump())
                    return
            entry = MCPServerConfig(
                url=url,
                transport=transport,
                auth="oauth",
                oauth_scope=str(link.params.get("oauth_scope") or "") or None,
                oauth_client_id=client_id,
                oauth_client_secret=client_secret or None,
            )
            cfg.mcp_servers.append(entry)
            saved.update(entry.model_dump())

        try:
            mutate_agent_config(agent, _apply)
        except Exception as exc:
            logger.warning("magic-link: could not store MCP client for %s: %s", agent, exc)
            detail = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else ""
            raise ValueError(
                f"Could not save the credentials to {agent or 'the agent'}'s configuration"
                + (f": {detail}" if detail else ".")
            ) from exc
        logger.info("magic-link: stored an OAuth client for %s on %s", url, agent)
        return saved

    def _mutate(self, link: LinkRecord, apply: Callable[[LinkRecord], None]) -> None:
        """Persist a change and mirror it onto the in-hand record."""
        from olano.magic_links import store

        updated = store.mutate(link.id, apply)
        if updated is not None:
            link.params.clear()
            link.params.update(updated.params)
            link.session_id = updated.session_id
        else:  # store-less link (unit tests) — apply in place
            apply(link)

    # ── after the sign-in ────────────────────────────────────────────────────

    @staticmethod
    def _session_transport(link: LinkRecord) -> str:
        """The config-file transport for the connection the flow just made.

        The session stores the SDK name ("streamable_http"/"sse"); the config
        speaks "http"/"sse". Fall back to "http" — normalize_transport("auto")
        resolves to streamable HTTP, so that is what an auto-flow connected
        over when the session has already been pruned.
        """
        from olano.core.mcp_setup import poll_oauth_flow

        try:
            session = poll_oauth_flow(str(link.session_id or ""))
            resolved = str(getattr(session, "transport", "") or "")
        except Exception:
            resolved = ""
        return {"streamable_http": "http", "sse": "sse"}.get(resolved, "http")

    def _ensure_configured(self, link: LinkRecord) -> None:
        """Make sure the authorized server is actually in the agent's config.

        Tokens are stored per ``(agent, server-url)``, but nothing in this flow
        ever wrote the server into ``agent.json5`` — so a link minted for a URL
        the agent does not have left the user signed in to a server their agent
        would never load, with no error anywhere. The sibling path
        (``admin.start_mcp_authorization``) refuses an unconfigured URL; here we
        close the same hole from the other end, and only once the user has
        actually authorized: an abandoned link writes nothing. (The credentials
        form is the one earlier write — saving a client secret is itself the
        configuration act — and it leaves an entry this finds and keeps.)

        An existing entry is left exactly as it is — the user may have set a
        transport, headers or client credentials we would otherwise clobber.
        """
        from olano.admin.mcp import add_mcp_server, list_mcp_servers

        agent = str(link.params.get("agent") or "")
        url = str(link.params.get("url") or "")
        if not agent or not url:
            return
        if any(s.get("url") == url for s in list_mcp_servers(agent)):
            return
        transport = str(link.params.get("transport") or "").strip().lower()
        if transport not in {"sse", "http", "stdio"}:
            # No minter passes a transport, so this is the normal path, and the
            # right answer is whatever the sign-in itself just connected over:
            # `start()` passed "auto", which start_mcp_authorization normalizes
            # and records on the flow session. Read it back rather than guess —
            # the runtime connect path has no http↔sse fallback, so a wrong
            # value here pins the entry to an endpoint the server doesn't serve.
            transport = self._session_transport(link)
        add_mcp_server(
            agent,
            url,
            transport=transport,
            auth="oauth",
            oauth_scope=str(link.params.get("oauth_scope") or "") or None,
            oauth_client_id=str(link.params.get("oauth_client_id") or "") or None,
            oauth_client_secret=str(link.params.get("oauth_client_secret") or "") or None,
        )
        logger.info("magic-link: registered newly authorized MCP server %s on %s", url, agent)

    async def post_complete(self, link: LinkRecord) -> str | None:
        """Persist the server, then hot-reload so its tools are usable now.

        Reports the reload it managed, for the same reason channel-connect
        reports its restart: a hot-reload that never ran leaves the agent
        signed in and still blind to the server's tools, and the person who
        just authorized it is the one who should be told.
        """
        from olano.tools.talk_to import get_bound_runtime

        agent = str(link.params.get("agent") or "")
        configured = True
        try:
            # Off the loop: save_agent_config takes deployment_write_lock (a
            # blocking flock busy-wait, and on managed boxes a fleet walk) —
            # inline it would stall every in-flight request for up to 10s.
            await asyncio.to_thread(self._ensure_configured, link)
        except Exception as exc:  # a config hiccup never invalidates the token
            configured = False
            logger.warning("magic-link: could not register MCP server for %s: %s", agent, exc)
            # finalize() builds the ✅ notice from link.result AFTER this hook
            # returns — degrade it, or a server that was never written reports
            # as connected (e.g. DeploymentLimitExceeded at the MCP cap).
            caveat = (
                f"Sign-in succeeded, but the server could not be added to "
                f"{agent or 'the agent'}'s configuration: {exc}. "
                "Add it (olano_add_mcp_server or the dashboard MCP tab) and reload."
            )
            link.result = f"{link.result} {caveat}".strip() if link.result else caveat

        if not configured:
            # The server never reached the config, so neither a reload nor a
            # restart can surface its tools — offering one would send the user
            # to press a button that cannot work. The caveat appended to
            # `link.result` above is the actionable answer instead.
            return APPLY_NONE

        runtime = get_bound_runtime()
        if runtime is None or not agent or not hasattr(runtime, "reload_agent_mcp"):
            return APPLY_RESTART_NEEDED if agent else APPLY_NONE
        try:
            await runtime.reload_agent_mcp(agent)
            return APPLY_RELOADED
        except Exception as exc:  # a reload hiccup never invalidates the token
            logger.warning("magic-link: MCP reload failed for %s: %s", agent, exc)
            return APPLY_RESTART_NEEDED


register(McpOAuthFlow())
