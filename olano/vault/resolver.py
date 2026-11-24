"""Resolve ${vault:name} and bare ${NAME} references in config strings."""

from __future__ import annotations

import logging
import os
import re
from typing import Any, cast

logger = logging.getLogger(__name__)

# Explicit vault reference: ``${vault:NAME}``.
_VAULT_PATTERN = re.compile(r"\$\{vault:([^}]+)\}")

# Bare env-style placeholder: ``${NAME}`` where NAME is a plain identifier.
# The identifier class (no ``:``) is what keeps this from half-matching a
# ``${vault:NAME}`` reference — that one always carries a colon before ``}``.
_BARE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Module-level vault instance (set by the runtime at startup)
_vault_instance = None


def set_vault(vault: Any) -> None:
    global _vault_instance
    _vault_instance = vault


def get_vault() -> Any:
    return _vault_instance


def lookup_credential(name: str, agent: str | None = None) -> str | None:
    """Resolve ``name`` to a value, vault-first then ``os.environ``.

    Precedence mirrors :func:`olano.tools._credentials.get_credential`:
    vault (per-agent override → common) → ``os.environ`` → ``None``. The vault
    is the higher-priority source because secrets live there and are no longer
    hydrated into the process environment. Never raises — a vault hiccup must
    not break channel boot or credential resolution.

    A runtime-owned entry (``vault/reserved.py``: the gateway key, the relay
    signing key, the deployment token) resolves to ``None`` from both layers.
    This is the path behind every ``${vault:NAME}`` / ``${NAME}`` in an
    ``agent.json5``, so without that rule a config could route the
    deployment's credit-spending key into an MCP header or a channel token.
    """
    from olano.vault.reserved import is_runtime_secret

    if is_runtime_secret(name):
        return None
    if _vault_instance is not None:
        try:
            # agent= enables per-agent overrides; touch defaults True so the
            # dashboard's "used …" timestamp reflects real resolution.
            value = _vault_instance.get(name, agent=agent) if agent else _vault_instance.get(name)
            if value:
                return cast(str, value)
        except Exception as e:
            logger.debug(
                "Vault: could not resolve '%s'%s: %s",
                name,
                f" for agent '{agent}'" if agent else "",
                e,
            )
    env_val = os.environ.get(name)
    if env_val:
        return env_val
    return None


# Historic private name — the channel-credential convention needs this from
# outside the module, so the public spelling above is the one to use.
_lookup = lookup_credential


def resolve_vault_refs(value: str, agent: str | None = None) -> str:
    """Replace ``${vault:NAME}`` and bare ``${NAME}`` references with values.

    - ``${vault:NAME}`` — explicit vault reference. Resolves vault → env; on a
      miss the literal is kept and a warning is logged (a vault ref that does
      not resolve is almost always a misconfiguration worth surfacing).
    - ``${NAME}`` — plain env-style placeholder (e.g. an ``agent.json5`` channel
      ``token``). Resolves vault → env, with the **vault taking priority**; on a
      miss the literal is kept *silently* so the caller (e.g. the runtime's
      ``_flag_unresolved_secrets``) can decide whether it matters.

    ``agent`` scopes vault lookups to that agent's per-agent overrides, falling
    back to the common value.
    """

    def vault_replacer(match: re.Match) -> str:
        name = match.group(1)
        resolved = _lookup(name, agent)
        if resolved is not None:
            return resolved
        logger.warning("Vault: unresolved reference '${vault:%s}'", name)
        return cast(str, match.group(0))

    def bare_replacer(match: re.Match) -> str:
        name = match.group(1)
        resolved = _lookup(name, agent)
        # Leave the placeholder verbatim on a miss — bare ${NAME} is common
        # templating syntax, so an absent value is not necessarily an error.
        return resolved if resolved is not None else cast(str, match.group(0))

    value = _VAULT_PATTERN.sub(vault_replacer, value)
    value = _BARE_PATTERN.sub(bare_replacer, value)
    return value


def resolve_config_dict(config: dict[str, Any], agent: str | None = None) -> dict[str, Any]:
    """Recursively resolve ``${vault:name}`` / ``${NAME}`` references in a config.

    ``agent`` is threaded through so per-agent vault overrides win over the
    common value (used by the runtime when booting an agent's channels).
    """
    result: dict[str, Any] = {}
    for k, v in config.items():
        if isinstance(v, str):
            result[k] = resolve_vault_refs(v, agent)
        elif isinstance(v, dict):
            result[k] = resolve_config_dict(v, agent)
        elif isinstance(v, list):
            result[k] = [
                resolve_vault_refs(item, agent) if isinstance(item, str) else item for item in v
            ]
        else:
            result[k] = v
    return result
