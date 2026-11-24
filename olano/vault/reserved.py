"""Vault entries the runtime writes for itself, which no person or agent may read.

The vault is one store with two kinds of row. Most rows are credentials an
operator put there for agents to use: provider keys, bot tokens, service
passwords. A few are written by the runtime about its own standing with the
control plane, under the ``system`` category:

- the per-deployment model-gateway key and URL and the digest of a revoked
  gateway key (``deployment/gateway_key.py``),
- the Ed25519 key this instance signs MCP relay requests with
  (``core/mcp_relay_enroll.py``, or ``core/mcp_relay_selfhost.py`` on a
  self-hosted box) and the older shared relay secret, plus the self-hosted
  relay enrollment record,
- the deployment token, when it is read from the vault instead of the env,
- the GitHub access token the revision history pushes and pulls with
  (``revisions/remote.py``): it can rewrite the user's config through a pull,
  so it is the dashboard's to use and no agent's to read.

Nothing distinguishes the two kinds at the storage layer, so every surface
that listed, revealed or resolved vault entries handed these out too: the
dashboard's Vault page showed the gateway key with Reveal, Edit, Delete and
Override, the agent Variables picker offered to override it, and
``get_credential`` / ``${vault:NAME}`` / the whole-vault hand-off to a stdio
MCP server resolved it for any agent that asked. A gateway key outside the
runtime spends the deployment's credits with no metering, which is the reason
``security/shell_guard.py`` strips it from every agent shell; the vault routes
undid that from the other side.

This module is the one definition of "runtime-owned". Every exit from the
vault asks :func:`is_runtime_secret` and skips or refuses these names; the
runtime itself keeps reading them through ``VaultStore.get`` directly, so it
is unaffected. The set is by **name**, not by category: an operator can file
their own entry under ``system`` from the CLI, and that entry must stay theirs.

Both prefix spellings count (``OLANO_`` and ``LIONCLAW_``, see
``env_aliases``), and the pre-rename gateway names stay listed for the vault
rows that ``migrations/0025_gateway_vault_key_rename`` has not moved yet.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from olano.env_aliases import spellings

# Literal names, never imported from the modules that write them: this set
# must stay importable from anywhere (the credential resolver, the API routes,
# an agent tool) without pulling the deployment layer in, and a test pins that
# every writer's constant is a member.
_RUNTIME_SECRET_BASES: tuple[str, ...] = (
    "OLANO_GATEWAY_KEY",
    "OLANO_GATEWAY_URL",
    "OLANO_GATEWAY_KEY_REVOKED_DIGEST",
    # The account API key a self-hosted box spends from (deployment/gateway_key.py).
    # Outside the runtime it spends the account's credits with no metering, the
    # same hazard as the enrolled key.
    "OLANO_CREDITS_KEY",
    # Pre-rename spellings of the three above.
    "LIONCLAW_OLANO_GATEWAY_KEY",
    "LIONCLAW_OLANO_GATEWAY_URL",
    "LIONCLAW_OLANO_GATEWAY_KEY_REVOKED_DIGEST",
    "LIONCLAW_MCP_RELAY_PRIVATE_KEY",
    "LIONCLAW_MCP_RELAY_SECRET",
    # What a self-hosted box enrolled with the MCP sign-in relay over its account
    # API key (core/mcp_relay_selfhost.py). No secret in it, but it decides where
    # MCP sign-ins go, so it is the runtime's bookkeeping and no agent's to edit.
    "OLANO_MCP_RELAY_SELFHOST",
    "LIONCLAW_DEPLOYMENT_TOKEN",
    "OLANO_REVISIONS_REMOTE_TOKEN",
    # Langfuse keys the agent turn traces export with (observability/credentials.py),
    # and the salt behind the pseudonymous user ids sent there. A Langfuse secret
    # key reads every trace in its project, which in full capture is every traced
    # agent's conversations, so it is the Observability card's to write and no
    # agent's to read.
    "OLANO_LANGFUSE_PUBLIC_KEY",
    "OLANO_LANGFUSE_SECRET_KEY",
    "OLANO_LANGFUSE_HOST",
    "OLANO_LANGFUSE_USER_SALT",
    "OLANO_LANGFUSE_PROJECT_ID",
)

RUNTIME_SECRET_NAMES: frozenset[str] = frozenset(
    spelling for base in _RUNTIME_SECRET_BASES for spelling in spellings(base)
)

# The sentence every refusing surface shows. One spelling, so the dashboard,
# the agent tools and the API cannot describe the same rule three ways.
RUNTIME_SECRET_REFUSAL = (
    "This entry is managed by the runtime for this deployment's own connections "
    "(Olano Cloud, the revision history's GitHub sync, and Langfuse tracing). It "
    "cannot be read, changed, removed or overridden here, and agents cannot use it."
)


def is_runtime_secret(name: str | None) -> bool:
    """True when ``name`` is a vault entry the runtime owns."""
    return bool(name) and name in RUNTIME_SECRET_NAMES


def without_runtime_secrets(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """``rows`` (vault metadata dicts with a ``name``) minus the runtime-owned ones."""
    return [row for row in rows if not is_runtime_secret(row.get("name"))]
