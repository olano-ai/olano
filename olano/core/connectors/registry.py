"""Connector provider registry.

Provider modules under ``providers/`` call :func:`register` at import time;
importing :mod:`olano.core.connectors` (the package) loads them all. The
registry also maintains the exposed-credential index consulted by
``tools/_credentials.get_credential`` on every toolkit call — membership
checks against :func:`exposed_credential_names` must stay O(1).

Google is deliberately NOT in this registry: it predates the broker and keeps
its bespoke flow in ``tools/google_tools.py`` (the runtime API dispatches
``service == "google"`` there). ``provider_ids()`` therefore lists only the
generic providers; API layers merge Google in themselves.
"""

from __future__ import annotations

from olano.core.connectors.base import (
    ConnectorProvider,
    connector_auth_status,
    extract_exposed_field,
    get_live_record,
)

_PROVIDERS: dict[str, ConnectorProvider] = {}
# credential name → (provider id, field path); rebuilt on every register().
_EXPOSED: dict[str, tuple[str, str]] = {}
_EXPOSED_NAMES: frozenset[str] = frozenset()


def register(provider: ConnectorProvider) -> ConnectorProvider:
    """Register a provider (idempotent by id) and index its exposed credentials."""
    global _EXPOSED_NAMES
    _PROVIDERS[provider.id] = provider
    for name, field_path in provider.exposes.items():
        _EXPOSED[name] = (provider.id, field_path)
    _EXPOSED_NAMES = frozenset(_EXPOSED)
    return provider


def get_provider(service: str) -> ConnectorProvider | None:
    return _PROVIDERS.get(service)


def provider_ids() -> list[str]:
    return sorted(_PROVIDERS)


def exposed_credential_names() -> frozenset[str]:
    return _EXPOSED_NAMES


def exposing_provider(name: str) -> tuple[ConnectorProvider, str] | None:
    """``(provider, field path)`` for the provider that serves credential *name*.

    ``None`` when no registered provider exposes it. Registry-only: says which
    connection a toolkit's credential *would* come from, not whether one is
    stored, that is :func:`get_live_record`'s question.
    """
    mapping = _EXPOSED.get(name)
    if mapping is None:
        return None
    provider = _PROVIDERS.get(mapping[0])
    if provider is None:  # pragma: no cover, register() keeps these in sync
        return None
    return provider, mapping[1]


def resolve_exposed_credential(name: str) -> str | None:
    """Resolve a credential name to a live brokered value for the current agent.

    Fast-path returns ``None`` when no provider exposes ``name``. Keys on the
    credential agent (the same identity the vault lookup uses — the parent for
    a sub-agent acting on its behalf) so each agent resolves its own → shared →
    system token. Never raises.
    """
    mapping = _EXPOSED.get(name)
    if mapping is None:
        return None
    service, field_path = mapping
    provider = _PROVIDERS.get(service)
    if provider is None:  # pragma: no cover — register() keeps these in sync
        return None
    try:
        from olano.tools._credentials import credential_agent_name

        agent = credential_agent_name()
    except Exception:
        agent = None
    try:
        record = get_live_record(provider, agent)
    except Exception:
        return None
    if record is None:
        return None
    return extract_exposed_field(record, field_path)


def all_statuses(agent: str | None = None) -> dict[str, dict]:
    """Status dict for every registered provider (keyed by service id)."""
    return {
        pid: connector_auth_status(provider, agent) for pid, provider in sorted(_PROVIDERS.items())
    }
