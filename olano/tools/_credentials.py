"""Per-agent credential resolution for native toolkits.

Toolkits historically read service credentials straight from the process
environment (``os.environ.get("X_API_KEY")``). Because the runtime boots every
agent in **one** process sharing a single ``os.environ``, that made it
impossible for two agents to use different accounts for the same service.

This module adds a thin, per-turn **credential overlay** keyed off the current
agent, plus a direct read of the encrypted **vault** scoped to that agent.
``AgentRunner`` (and the cognition worker) bind the running agent's identity +
``config.env`` (with ``${vault:...}`` references resolved) for the duration of a
turn via :func:`agent_runtime_context`. Toolkit helpers call
:func:`get_credential` instead of ``os.environ.get``; resolution is:

    config.env overlay  →  connector broker (live OAuth tokens)  →  vault
    (per-agent override → common)  →  os.environ  →  default

The connector-broker layer surfaces access tokens minted via the dashboard's
Connections tab (``olano/core/connectors``) through the credential names
toolkits already read (e.g. ``MS365_ACCESS_TOKEN``), auto-refreshing them near
expiry. It sits *above* the vault deliberately: a live "Connected" OAuth
session must supersede a stale hand-pasted token — that is the point of the
tab — while the per-turn ``config.env`` overlay still wins for agents pinned
to an explicit account. Names no provider exposes skip the layer via an O(1)
set check, so unrelated lookups cost nothing extra.

The vault is read directly (not via the global environment), so secrets need not
be hydrated into ``os.environ`` and stay out of subprocess / shell / MCP-child
environments. With no overlay bound and no vault unlocked (unit tests, single-
agent CLI) resolution falls straight through to ``os.environ``, so existing
behaviour and tests are unchanged.

See ``tools/google_tools.py`` for the original per-agent pattern (token files
keyed by ``AGENT_CTX``) that this generalises to every toolkit.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from collections.abc import Iterable, Iterator

# Per-agent credential overlay for the current turn. ``None`` (the default) means
# "no overlay" so resolution falls through to the vault / ``os.environ``. Never
# mutate the bound dict in place — always rebind via :func:`agent_runtime_context`.
current_agent_env: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "lc_current_agent_env", default=None
)

# The agent whose CREDENTIALS this turn resolves. Normally the running agent;
# for a sub-agent / cognition worker (``<parent>__cog__…``) it is the parent
# it acts for, so vault overrides and connector records keyed on the parent's
# name resolve instead of silently degrading to the common tier. Bound by
# :func:`agent_runtime_context` from ``AgentConfig.credential_scope``.
credential_agent: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lc_credential_agent", default=None
)


def credential_agent_name() -> str | None:
    """The identity every credential layer keys on: the scope, else the agent."""
    scope = credential_agent.get()
    if scope:
        return scope
    try:
        from olano.tools.talk_to import current_agent

        return current_agent.get() or None
    except Exception:
        return None


# Per-turn memo of vault lookups (name → resolved value or None for "absent"), so
# repeated reads of the same credential within a turn don't re-decrypt or re-query
# the vault. Bound to a fresh dict by :func:`agent_runtime_context`; ``None`` when
# no turn context is active (then the vault is consulted on each call).
_vault_cache: contextvars.ContextVar[dict[str, str | None] | None] = contextvars.ContextVar(
    "lc_vault_cache", default=None
)


def _vault_lookup(name: str) -> str | None:
    """Resolve ``name`` from the vault for the current agent (override → common).

    Returns the value, or ``None`` when the vault is unavailable/locked or the
    credential is absent. Memoised per turn via :data:`_vault_cache`. Never raises
    — credential resolution must not blow up a toolkit on a vault hiccup.
    """
    cache = _vault_cache.get()
    if cache is not None and name in cache:
        return cache[name]

    value: str | None = None
    try:
        from olano.vault.reserved import is_runtime_secret
        from olano.vault.resolver import get_vault

        vault = get_vault()
        if vault is not None and not is_runtime_secret(name):
            agent = credential_agent_name()
            # touch=False: the per-agent resolver runs on every toolkit call, so we
            # don't write last_used on each read.
            resolved = vault.get(name, agent=agent, touch=False)
            value = resolved or None
    except Exception:
        value = None

    if cache is not None:
        cache[name] = value
    return value


def _connector_lookup(name: str) -> str | None:
    """Resolve ``name`` from the OAuth connector broker for the current agent.

    Returns a live (auto-refreshed) access token or derived value when a
    registered provider exposes ``name`` and a token is connected at some scope
    (own → shared → system); ``None`` otherwise. Never raises — resolution must
    not blow up a toolkit on a broker hiccup.
    """
    try:
        from olano.core.connectors import exposed_credential_names, resolve_exposed_credential

        if name not in exposed_credential_names():
            return None
        return resolve_exposed_credential(name)
    except Exception:
        return None


def get_credential(name: str, default: str = "") -> str:
    """Resolve one credential for the current agent context.

    Precedence: ``config.env`` overlay (non-empty) → connector broker (live
    OAuth tokens) → vault (per-agent override → common) → ``os.environ`` →
    ``default``. Each layer only wins when it supplies a non-empty value, so a
    blank/misconfigured layer falls through rather than silently shadowing a
    working value below it.

    The runtime's own entries (``vault/reserved.py``) are the one exception:
    they resolve to ``default`` from every layer, the environment included.
    The gateway key spends the deployment's credits, and the modules that
    legitimately need it read it directly (``deployment/gateway_key.py``,
    ``core/mcp_relay.py``, ``deployment/sources.py``), never through here.
    """
    from olano.vault.reserved import is_runtime_secret

    if is_runtime_secret(name):
        return default
    # Per-customer credentials come FIRST — before even the config.env
    # overlay, which is an operator value. For a name the agent declared in
    # customer_service.customer_credential_names, a customer-bound turn
    # resolves to that customer's own vault entry or to `default` (fail
    # closed): silently borrowing the operator's token there is exactly the
    # cross-tenant accident the declaration exists to prevent. With no
    # customer bound, the chain below applies unchanged.
    try:
        from olano.tools.customer_credentials import resolve_customer_credential

        agent = credential_agent_name()
        if agent:
            handled, customer_value = resolve_customer_credential(agent, name)
            if handled:
                return customer_value or default
    except Exception:
        pass

    overlay = current_agent_env.get()
    if overlay is not None:
        value = overlay.get(name)
        if value:
            return value

    connector_value = _connector_lookup(name)
    if connector_value:
        return connector_value

    vault_value = _vault_lookup(name)
    if vault_value:
        return vault_value

    return os.environ.get(name, default)


def get_credentials(*names: str) -> dict[str, str]:
    """Resolve several credentials at once (each via :func:`get_credential`)."""
    return {name: get_credential(name) for name in names}


def require_credentials(names: Iterable[str], *, message: str) -> tuple[dict[str, str], str | None]:
    """Resolve every name; if any is missing/empty return ``({}, message)``.

    ``message`` should be the toolkit's existing ``[missing-credentials] …``
    string so the wording — and the ``[missing-credentials]`` marker that the
    chat surfaces and tests rely on — stays identical. On success returns
    ``(values, None)`` with every requested name resolved.
    """
    values = {name: get_credential(name) for name in names}
    if not all(values.values()):
        return {}, message
    return values, None


def _build_overlay(env: dict[str, str] | None) -> dict[str, str]:
    """Resolve ``${vault:...}`` references in an agent's ``env`` into values.

    Done once per turn (cheap). Best-effort: on any resolver failure the raw
    value is kept (``resolve_vault_refs`` itself already falls back to
    ``os.environ`` and returns the literal on a miss).
    """
    if not env:
        return {}
    try:
        from olano.vault.resolver import resolve_vault_refs

        return {key: resolve_vault_refs(value) for key, value in env.items()}
    except Exception:
        return dict(env)


@contextlib.contextmanager
def agent_runtime_context(
    name: str, env: dict[str, str] | None, *, credential_scope: str | None = None
) -> Iterator[None]:
    """Bind the running agent's identity + credential overlay for one turn.

    Sets five contextvars and restores them on exit:

    - ``current_agent`` (``tools.talk_to``) — peer routing / attribution,
    - ``credential_agent`` (this module) — the agent every credential layer
      keys on: ``credential_scope`` when given (a sub-agent acting for its
      parent), else ``name``,
    - ``AGENT_CTX`` (``tools._agent_ctx``) — the Google token scope, the
      same value,
    - ``current_agent_env`` (this module) — the credential overlay,
    - the per-turn vault memo.

    This is the single entry point every execution path should use so identity
    and credentials are always bound together. ``contextvars`` propagate into
    tasks spawned inside the ``with`` block, so concurrently-running agents stay
    isolated.
    """
    # Imported lazily to avoid import cycles within the tools package.
    from olano.tools._agent_ctx import AGENT_CTX
    from olano.tools.talk_to import current_agent

    scope = (credential_scope or "").strip() or name
    overlay = _build_overlay(env)
    ca_token = current_agent.set(name)
    scope_token = credential_agent.set(scope)
    google_token = AGENT_CTX.set(scope)
    env_token = current_agent_env.set(overlay)
    # Fresh per-turn vault memo so repeated reads don't re-decrypt; isolated per
    # task/turn so concurrent agents never share resolved secrets.
    cache_token = _vault_cache.set({})
    try:
        yield
    finally:
        current_agent.reset(ca_token)
        credential_agent.reset(scope_token)
        AGENT_CTX.reset(google_token)
        current_agent_env.reset(env_token)
        _vault_cache.reset(cache_token)
