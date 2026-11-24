"""Langfuse credentials: reserved vault rows first, then the process env.

A Langfuse secret key reads **every** trace in its project, which in ``full``
capture is every traced agent's conversations. So the keys are reserved
runtime rows (:mod:`olano.vault.reserved`): no agent resolves them through
``get_credential`` / ``${vault:…}``, the Vault page does not list them, and
the only writer is the admin Observability route. The secret is also stripped
from every agent shell (``security/shell_guard._ALWAYS_STRIP_ENV``).

Resolution, first match wins, per field:

1. the agent's own override row (``VaultStore.set(..., agent=<name>)``),
2. the deployment row,
3. the process env: ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``,
   ``LANGFUSE_BASE_URL`` (the SDK's name), then the legacy ``LANGFUSE_HOST``.

The public and secret key always come from the same layer: mixing a public
key from one project with a secret from another would authenticate nowhere
and fail on every export, so a layer counts only when it holds both.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

PUBLIC_KEY_NAME = "OLANO_LANGFUSE_PUBLIC_KEY"
SECRET_KEY_NAME = "OLANO_LANGFUSE_SECRET_KEY"
HOST_NAME = "OLANO_LANGFUSE_HOST"
#: Salt for the HMAC that stands in for a sender id in ``metadata`` capture.
USER_SALT_NAME = "OLANO_LANGFUSE_USER_SALT"
#: The project an agent's OWN keys belong to (from Test connection), so links
#: from that agent's replies open the right project. The deployment's project
#: id is not a secret and lives in config.yaml instead.
PROJECT_ID_NAME = "OLANO_LANGFUSE_PROJECT_ID"

ENV_PUBLIC_KEY = "LANGFUSE_PUBLIC_KEY"
ENV_SECRET_KEY = "LANGFUSE_SECRET_KEY"
ENV_BASE_URL = "LANGFUSE_BASE_URL"
ENV_HOST_LEGACY = "LANGFUSE_HOST"

#: Seconds a resolved credential set is reused before the vault is read again.
#: Short, so a key saved in the dashboard reaches the next turn quickly;
#: :func:`invalidate` drops it at once from the route that saves.
_CACHE_TTL_S = 30.0


@dataclass(frozen=True)
class LangfuseCredentials:
    public_key: str
    secret_key: str
    host: str
    #: ``agent vault`` / ``vault`` / ``env``: shown on the status endpoint,
    #: never the values.
    source: str


_cache: dict[str, tuple[float, LangfuseCredentials | None]] = {}
_cache_lock = threading.Lock()
_salt_lock = threading.Lock()


def _vault() -> Any:
    from olano.vault.resolver import get_vault, set_vault

    vault = get_vault()
    if vault is not None:
        return vault
    try:
        from olano.vault.runtime import unlock_vault_for_session

        result = unlock_vault_for_session(allow_prompt=False)
        if result and result.vault is not None:
            set_vault(result.vault)
            return result.vault
    except Exception:
        logger.debug("observability: vault unlock failed", exc_info=True)
    return None


def _vault_get(vault: Any, name: str, agent: str | None) -> str:
    try:
        if agent:
            return str(vault.get_override(name, agent, touch=False) or "")
        return str(vault.get(name, touch=False) or "")
    except Exception:
        return ""


def _from_vault(vault: Any, agent: str | None, source: str) -> LangfuseCredentials | None:
    pk = _vault_get(vault, PUBLIC_KEY_NAME, agent).strip()
    sk = _vault_get(vault, SECRET_KEY_NAME, agent).strip()
    if not (pk and sk):
        return None
    host = _vault_get(vault, HOST_NAME, agent).strip()
    return LangfuseCredentials(public_key=pk, secret_key=sk, host=host, source=source)


def _from_env() -> LangfuseCredentials | None:
    pk = os.environ.get(ENV_PUBLIC_KEY, "").strip()
    sk = os.environ.get(ENV_SECRET_KEY, "").strip()
    if not (pk and sk):
        return None
    host = (os.environ.get(ENV_BASE_URL) or os.environ.get(ENV_HOST_LEGACY) or "").strip()
    return LangfuseCredentials(public_key=pk, secret_key=sk, host=host, source="env")


def resolve_langfuse_credentials(agent: str | None) -> LangfuseCredentials | None:
    """The Langfuse keys ``agent``'s turns export with, or ``None``.

    Never raises; a locked vault falls through to the env. Cached for
    :data:`_CACHE_TTL_S` per agent, because the runner asks on every turn.
    """
    key = agent or ""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    creds: LangfuseCredentials | None = None
    try:
        vault = _vault()
        if vault is not None:
            if agent:
                creds = _from_vault(vault, agent, "agent vault")
            if creds is None:
                creds = _from_vault(vault, None, "vault")
    except Exception:
        logger.debug("observability: vault read failed", exc_info=True)
    if creds is None:
        creds = _from_env()
    with _cache_lock:
        _cache[key] = (now, creds)
    return creds


def invalidate() -> None:
    """Forget every cached credential set (a key was saved or cleared)."""
    with _cache_lock:
        _cache.clear()


def credential_source(agent: str | None = None) -> str:
    """Where ``agent``'s keys come from: ``agent vault``, ``vault``, ``env`` or ``none``."""
    creds = resolve_langfuse_credentials(agent)
    return creds.source if creds else "none"


def store_credentials(
    public_key: str,
    secret_key: str,
    host: str = "",
    *,
    agent: str | None = None,
) -> None:
    """Save a key pair (and optional host) as reserved vault rows.

    ``agent`` writes that agent's override; ``None`` the deployment's. Raises
    ``RuntimeError`` when the vault is locked: there is nowhere safe to keep
    a secret key then, and the env is the operator's to edit.
    """
    vault = _vault()
    if vault is None:
        raise RuntimeError(
            "The vault is locked, so the Langfuse keys have nowhere safe to live. "
            "Unlock the vault (Vault page) and save them again."
        )
    desc = "Langfuse key the agent turn traces export with"
    vault.set(PUBLIC_KEY_NAME, public_key, description=desc, category="system", agent=agent)
    vault.set(SECRET_KEY_NAME, secret_key, description=desc, category="system", agent=agent)
    if host:
        vault.set(HOST_NAME, host, description=desc, category="system", agent=agent)
    else:
        try:
            vault.delete(HOST_NAME, agent=agent)
        except Exception:
            pass
    invalidate()


def clear_credentials(*, agent: str | None = None) -> None:
    """Remove the stored key pair (the env, if any, applies again)."""
    vault = _vault()
    if vault is None:
        return
    for name in (PUBLIC_KEY_NAME, SECRET_KEY_NAME, HOST_NAME, PROJECT_ID_NAME):
        try:
            vault.delete(name, agent=agent)
        except Exception:
            logger.debug("observability: delete %s failed", name, exc_info=True)
    invalidate()


_salt_memo: bytes | None = None


def user_salt() -> bytes:
    """The deployment's salt for pseudonymous sender ids, created on first use.

    Kept in the vault so the same sender maps to the same id across restarts
    (Langfuse can then group a user's sessions). With the vault locked, a
    process-lifetime salt is used: ids then change on restart, which loses
    grouping but never leaks the sender.
    """
    global _salt_memo
    if _salt_memo is not None:
        return _salt_memo
    with _salt_lock:
        if _salt_memo is not None:
            return _salt_memo
        vault = _vault()
        value = _vault_get(vault, USER_SALT_NAME, None) if vault is not None else ""
        if not value:
            value = secrets.token_hex(32)
            if vault is not None:
                try:
                    vault.set(
                        USER_SALT_NAME,
                        value,
                        description="Salt for the pseudonymous user ids sent to Langfuse",
                        category="system",
                    )
                except Exception:
                    logger.debug("observability: salt not persisted", exc_info=True)
        _salt_memo = value.encode("utf-8")
        return _salt_memo


def _reset_for_tests() -> None:
    global _salt_memo
    invalidate()
    _salt_memo = None


def store_agent_project_id(agent: str, project_id: str) -> None:
    """Remember which project ``agent``'s own keys belong to (Test connection)."""
    vault = _vault()
    if vault is None or not agent:
        return
    vault.set(
        PROJECT_ID_NAME,
        project_id,
        description="Langfuse project of this agent's own keys",
        category="system",
        agent=agent,
    )


def agent_project_id(agent: str) -> str:
    vault = _vault()
    if vault is None or not agent:
        return ""
    return _vault_get(vault, PROJECT_ID_NAME, agent).strip()
