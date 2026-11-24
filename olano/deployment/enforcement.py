"""Enforcement decisions for the Deployment Settings layer.

Pure decision helpers: each returns a :class:`Decision` (never raises for a
policy outcome). Callers translate denials into their surface's failure shape
— HTTP 403/503 via :mod:`olano.deployment.guard`, an
:class:`~olano.core.runner.AgentError` in the runner, a printed message in
the CLI.

Failure semantics (deployment-settings design record, §6): the **open** posture
short-circuits to allowed before anything else — it is the only fail-open
state. In a managed posture an internal counting failure yields
``policy_unavailable`` (reject), never a silent allow.

Two restriction postures (§5):

- *Licensed cap reduction*: existing resources keep working; a mutation is
  rejected only when it **increases** a count beyond its cap
  (``new > limit and new > old``) — shrinking or re-saving an over-limit
  config always succeeds.
- *Quarantine*: creations are refused outright, and only the default agent
  may take turns (:func:`agent_turn_allowed`).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from olano.deployment import resolver
from olano.deployment.resolver import SUPPORT_CONTACT, DeploymentStatus

logger = logging.getLogger(__name__)

REMEDIATION = f"Upgrade your olano plan or contact {SUPPORT_CONTACT} to raise this limit."
QUARANTINE_REMEDIATION = (
    "This deployment has no valid license (or it was suspended, or its "
    "installation manifest is unreadable). Install a valid license or restore "
    f"cloud connectivity to resume service. Contact {SUPPORT_CONTACT} for help."
)

_COUNTS_TTL = 2.0


@dataclass(frozen=True)
class Decision:
    """The outcome of one enforcement question."""

    allowed: bool
    code: str = "ok"
    message: str = ""
    remediation: str = ""
    limit: int | None = None
    current: int | None = None

    def to_detail(self) -> dict[str, Any]:
        """The structured payload surfaced in HTTP 403/503 responses."""
        return {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
            "limit": self.limit,
            "current": self.current,
        }


ALLOW = Decision(allowed=True)


class DeploymentLimitExceeded(Exception):
    """Raised by the central persistence guard when a write is denied."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.message or decision.code)
        self.decision = decision


def _deny(
    code: str,
    message: str,
    *,
    limit: int | None = None,
    current: int | None = None,
    remediation: str = REMEDIATION,
) -> Decision:
    return Decision(
        allowed=False,
        code=code,
        message=message,
        remediation=remediation,
        limit=limit,
        current=current,
    )


def _quarantine_deny(action: str) -> Decision:
    return _deny(
        "deployment_quarantined",
        f"This deployment is quarantined — {action} is disabled until it is re-licensed.",
        remediation=QUARANTINE_REMEDIATION,
    )


def _policy_unavailable(what: str) -> Decision:
    return _deny(
        "policy_unavailable",
        f"Could not evaluate deployment limits ({what}); the operation was rejected. Retry shortly.",
        remediation="Retry; if this persists, check the runtime logs.",
    )


# ── Resource counting ────────────────────────────────────────────────────────


@dataclass
class ResourceCounts:
    agents: int = 0
    connectors_by_agent: dict[str, int] = field(default_factory=dict)
    connectors_total: int = 0
    mcp_total: int = 0
    webhooks_total: int = 0


_counts_memo: tuple[float, ResourceCounts] | None = None


def invalidate_counts() -> None:
    global _counts_memo
    _counts_memo = None


def _count_from_raw(raw: dict) -> tuple[int, int, int]:
    """(connectors, mcp_servers, webhooks) declared in one raw agent config."""
    channels = raw.get("channels") or []
    mcp = raw.get("mcp_servers") or []
    webhooks = raw.get("webhooks") or []
    return (
        len(channels) if isinstance(channels, list) else 0,
        len(mcp) if isinstance(mcp, list) else 0,
        len(webhooks) if isinstance(webhooks, list) else 0,
    )


def count_deployment_objects(*, fresh: bool = False) -> ResourceCounts:
    """Count agents/connectors/MCP/webhooks across every agent config.

    Counts come from the raw JSON5 (no Pydantic validation) so a single broken
    agent config cannot make the whole deployment uncountable.

    ``hidden: true`` agents are excluded entirely. They are the per-provider
    test agents shipped in the image: the runtime never boots them, they never
    hold a channel or an MCP connection, and every other consumer of the roster
    already skips them (``server._should_boot_on_start``, the boot allowance in
    ``server._resolve_deployment_boot_set``, ``/api/agents``, the org chart).
    Counting them here would charge a plan for our own fixtures: a box with
    zero real agents would report seven and refuse every install.

    An agent whose config cannot be found or parsed still counts (fail closed):
    we cannot prove it is hidden, and a limit that a broken file can dodge is
    not a limit.
    """
    global _counts_memo
    if not fresh and _counts_memo is not None:
        stamped, cached = _counts_memo
        if time.monotonic() - stamped < _COUNTS_TTL:
            return cached

    from olano.config.loader import _load_json5, find_agent_config_path, list_agents

    counts = ResourceCounts()
    # Full set: the hidden skip below must keep its own fail-closed semantics
    # (an unparseable config COUNTS — the loader's filter reads it as visible,
    # but never rely on another module for a billing boundary).
    for name in list_agents(include_hidden=True):
        path = find_agent_config_path(name)
        if path is None:
            counts.agents += 1
            continue
        try:
            raw = _load_json5(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("uncountable agent config for %r; counting as zero", name)
            counts.agents += 1
            continue
        # Identity test against True, mirroring runtime/api._agent_is_hidden:
        # only an explicit opt-in hides an agent (a MagicMock is truthy).
        if (raw.get("hidden") if isinstance(raw, dict) else None) is True:
            continue
        counts.agents += 1
        connectors, mcp, webhooks = _count_from_raw(raw)
        counts.connectors_by_agent[name] = connectors
        counts.connectors_total += connectors
        counts.mcp_total += mcp
        counts.webhooks_total += webhooks

    _counts_memo = (time.monotonic(), counts)
    return counts


# ── Feature / tab toggles ────────────────────────────────────────────────────


def feature_enabled(name: str) -> bool:
    """Deployment-level feature toggle. Open posture ⇒ always True."""
    return resolver.get_effective_settings().feature_enabled(name)


def service_entitled(service: str) -> bool:
    """Per-service Olano entitlement (``olano_vision``/``olano_image``/``olano_tts``/
    ``olano_stt``/``olano_web_search``). Open posture / missing key ⇒ True, so a
    plan withholds one managed service by setting its flag False; every existing
    deployment keeps them all. This is the selection gate the availability probes
    consult — advisory (the gateway re-authorizes every call), but it turns "your
    plan doesn't include this service" into a clean local decision instead of
    leading with an olano rung that will 402."""
    return feature_enabled(f"olano_{service}")


def tab_enabled(name: str) -> bool:
    return resolver.get_effective_settings().tab_enabled(name)


def feature_decision(name: str, *, label: str | None = None) -> Decision:
    if feature_enabled(name):
        return ALLOW
    return _deny(
        f"deployment_feature_{name}_disabled",
        f"The {label or name.replace('_', ' ')} feature is disabled for this deployment.",
    )


# ── Execution isolation (source / secret protection on managed boxes) ─────────
# On a managed deployment the customer reaches the box only through the
# dashboard + agent chat, and the runtime container holds the product source
# (/app) + secrets. An agent `execute`/shell running in that container is a de
# facto root shell that can copy the source and read the vault, so under a
# managed posture agents must NOT run raw shell in the runtime container. They
# either get no execution (fail-closed default) or an isolated sandbox that
# contains neither the source nor secrets — see core/builder._build_deepagents_backend.


#: An unmanaged box's opt-in to per-agent sandbox containers (the self-host
#: ``setup.sh --sandbox`` writes ``OLANO_SANDBOX_EXEC=1``). Both spellings are
#: read by name: the deploy layer writes the new one, and env_aliases' mirror
#: is not guaranteed for a value set after import.
SANDBOX_EXEC_ENV = "OLANO_SANDBOX_EXEC"
LEGACY_SANDBOX_EXEC_ENV = "LIONCLAW_SANDBOX_EXEC"


def open_box_sandbox_requested() -> bool:
    """Whether the operator of an unmanaged box asked for sandboxed execution.

    An open box normally runs agents' shell commands in the runtime's own
    container (or on the host for a bare install). Setting
    ``OLANO_SANDBOX_EXEC=1`` makes it behave like a managed box with the
    sandbox enabled: every agent shell, the Terminal, the chat ``!`` lane and
    stdio MCP servers run in per-agent sandbox containers started through the
    Docker socket, and when a sandbox cannot start the agent gets no shell
    rather than falling back to the runtime container. It needs the Docker
    socket, the sandbox image and the home mounted at its host path
    (``deploy/self-host/docker-compose.sandbox.yml``).
    """
    raw = os.environ.get(SANDBOX_EXEC_ENV) or os.environ.get(LEGACY_SANDBOX_EXEC_ENV) or ""
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def require_sandboxed_exec() -> bool:
    """True when agents must not run raw shell in the runtime container.

    Managed posture (enforced/quarantine) ⇒ True. Open/unmanaged (the operator's
    own box, e.g. internal use) ⇒ False, unless its operator opted in to
    per-agent sandboxes (:func:`open_box_sandbox_requested`), in which case the
    open box takes the managed path's exec rules too (``sandbox_exec_enabled``
    is always True there, so it gets a sandbox, never "no shell by policy").

    **Fails closed.** ``deployment_status()`` already resolves "am I managed?"
    without raising (its own fail-safe path quarantines anything it cannot prove
    unmanaged), so reaching this ``except`` means the entitlements layer is
    broken outright. Answering "unmanaged" there would hand out a raw host shell
    in the container holding /app + the vault, so an unresolvable posture
    restricts execution instead.
    """
    try:
        if resolver.deployment_status().posture != "open":
            return True
    except Exception:
        logger.exception("posture resolution failed; requiring sandboxed exec (fail closed)")
        return True
    return open_box_sandbox_requested()


def sandbox_exec_enabled() -> bool:
    """Whether an isolated docker sandbox may PROVIDE execution on a managed box.

    Default **False**: a managed box has no code execution until the operator has
    validated the sandbox on the box and the control plane opts in
    (`features.sandbox_exec = true` in the signed settings). Until then managed
    agents fail closed to a filesystem backend (no shell) rather than fall back
    to a host root shell. Open/unmanaged posture always returns True (its "exec"
    is the ordinary host shell, which is fine on the operator's own machine).
    """
    try:
        status = resolver.deployment_status()
        if status.posture == "open":
            return True
        # Explicit opt-in only — NOT the open-map "missing ⇒ enabled" default.
        return bool(status.settings.features.get("sandbox_exec", False))
    except Exception:
        logger.exception("posture resolution failed; sandbox exec treated as disabled")
        return False


# ── Creation / addition gates ────────────────────────────────────────────────


def can_create_agent() -> Decision:
    status = resolver.deployment_status()
    if status.posture == "open":
        return ALLOW
    if status.quarantined:
        return _quarantine_deny("creating agents")
    limit = status.settings.resources.max_agents
    if limit is None:
        return ALLOW
    try:
        current = count_deployment_objects(fresh=True).agents
    except Exception:
        logger.exception("agent count failed")
        return _policy_unavailable("agent count failed")
    if current >= limit:
        return _deny(
            "deployment_limit_max_agents",
            f"This deployment is limited to {limit} agent(s); {current} already exist.",
            limit=limit,
            current=current,
        )
    return ALLOW


def _capped_add(
    *,
    what: str,
    code: str,
    limit: int | None,
    current: int,
) -> Decision:
    if limit is None or current < limit:
        return ALLOW
    return _deny(
        code,
        f"This deployment is limited to {limit} {what}; {current} already exist.",
        limit=limit,
        current=current,
    )


def can_add_connector(agent_name: str) -> Decision:
    """May one more connector (messaging channel) be added to *agent_name*?"""
    status = resolver.deployment_status()
    if status.posture == "open":
        return ALLOW
    if status.quarantined:
        return _quarantine_deny("adding connectors")
    r = status.settings.resources
    if r.max_connectors_per_agent is None and r.max_connectors_total is None:
        return ALLOW
    try:
        counts = count_deployment_objects(fresh=True)
    except Exception:
        logger.exception("connector count failed")
        return _policy_unavailable("connector count failed")
    per_agent = counts.connectors_by_agent.get(agent_name, 0)
    decision = _capped_add(
        what="connector(s) per agent",
        code="deployment_limit_max_connectors_per_agent",
        limit=r.max_connectors_per_agent,
        current=per_agent,
    )
    if not decision.allowed:
        return decision
    return _capped_add(
        what="connector(s) in total",
        code="deployment_limit_max_connectors_total",
        limit=r.max_connectors_total,
        current=counts.connectors_total,
    )


def can_add_mcp() -> Decision:
    status = resolver.deployment_status()
    if status.posture == "open":
        return ALLOW
    if status.quarantined:
        return _quarantine_deny("adding MCP servers")
    limit = status.settings.resources.max_mcp_servers_total
    if limit is None:
        return ALLOW
    try:
        current = count_deployment_objects(fresh=True).mcp_total
    except Exception:
        logger.exception("MCP count failed")
        return _policy_unavailable("MCP count failed")
    return _capped_add(
        what="MCP server(s)",
        code="deployment_limit_max_mcp_servers_total",
        limit=limit,
        current=current,
    )


def can_add_webhook() -> Decision:
    status = resolver.deployment_status()
    if status.posture == "open":
        return ALLOW
    if status.quarantined:
        return _quarantine_deny("adding webhooks")
    limit = status.settings.resources.max_webhooks_total
    if limit is None:
        return ALLOW
    try:
        current = count_deployment_objects(fresh=True).webhooks_total
    except Exception:
        logger.exception("webhook count failed")
        return _policy_unavailable("webhook count failed")
    return _capped_add(
        what="webhook(s)",
        code="deployment_limit_max_webhooks_total",
        limit=limit,
        current=current,
    )


def can_create_user(current_count: int) -> Decision:
    """Cap dashboard user accounts. Caller supplies the count from UsersDB
    (inside its own transaction where possible)."""
    status = resolver.deployment_status()
    if status.posture == "open":
        return ALLOW
    if status.quarantined:
        return _quarantine_deny("creating users")
    return _capped_add(
        what="user account(s)",
        code="deployment_limit_max_users",
        limit=status.settings.resources.max_users,
        current=current_count,
    )


# ── Central config-write guard ───────────────────────────────────────────────


def validate_config_write(
    agent_name: str,
    old_raw: dict | None,
    new_raw: dict,
) -> Decision:
    """Gate any agent-config persistence (structured or raw).

    Rejects only when a countable resource **increases beyond its cap** —
    shrinking, or re-saving an already-over-limit config, always succeeds.
    Called from the central persistence path (config.loader.save_agent_config)
    under the deployment write lock; endpoint-level checks are UX sugar on
    top of this backstop.
    """
    status = resolver.deployment_status()
    if status.posture == "open":
        return ALLOW

    old_connectors, old_mcp, old_webhooks = _count_from_raw(old_raw or {})
    new_connectors, new_mcp, new_webhooks = _count_from_raw(new_raw)
    is_new_agent = old_raw is None

    if status.quarantined:
        # Existing agents may re-save (e.g. to shrink); nothing may be created
        # or grown.
        if is_new_agent:
            return _quarantine_deny("creating agents")
        if new_connectors > old_connectors or new_mcp > old_mcp or new_webhooks > old_webhooks:
            return _quarantine_deny("adding connectors, MCP servers, or webhooks")
        return ALLOW

    r = status.settings.resources
    try:
        counts = count_deployment_objects(fresh=True)
    except Exception:
        logger.exception("deployment counts failed during config write")
        return _policy_unavailable("resource count failed")

    if is_new_agent and r.max_agents is not None and counts.agents >= r.max_agents:
        return _deny(
            "deployment_limit_max_agents",
            f"This deployment is limited to {r.max_agents} agent(s);"
            f" {counts.agents} already exist.",
            limit=r.max_agents,
            current=counts.agents,
        )

    checks = (
        (
            "deployment_limit_max_connectors_per_agent",
            "connector(s) on this agent",
            r.max_connectors_per_agent,
            old_connectors,
            new_connectors,
        ),
        (
            "deployment_limit_max_connectors_total",
            "connector(s) in total",
            r.max_connectors_total,
            counts.connectors_total,
            counts.connectors_total - old_connectors + new_connectors,
        ),
        (
            "deployment_limit_max_mcp_servers_total",
            "MCP server(s) in total",
            r.max_mcp_servers_total,
            counts.mcp_total,
            counts.mcp_total - old_mcp + new_mcp,
        ),
        (
            "deployment_limit_max_webhooks_total",
            "webhook(s) in total",
            r.max_webhooks_total,
            counts.webhooks_total,
            counts.webhooks_total - old_webhooks + new_webhooks,
        ),
    )
    for code, what, limit, old_count, new_count in checks:
        if limit is not None and new_count > limit and new_count > old_count:
            return _deny(
                code,
                f"Saving would exceed the deployment limit of {limit} {what}.",
                limit=limit,
                current=new_count,
            )
    return ALLOW


# ── Turn gating (quarantine) ─────────────────────────────────────────────────


def agent_turn_allowed(agent_name: str) -> Decision:
    """In quarantine only the default agent may take turns (recovery surface)."""
    status = resolver.deployment_status()
    if not status.quarantined:
        return ALLOW
    try:
        from olano.config.home import load_home_config

        default_agent = load_home_config().default_agent
    except Exception:
        default_agent = None
    if default_agent and agent_name == default_agent:
        return ALLOW
    return _deny(
        "deployment_quarantined",
        f"Agent '{agent_name}' is paused: this deployment is quarantined and only the "
        f"default agent ({default_agent or 'unknown'}) remains available for recovery.",
        remediation=QUARANTINE_REMEDIATION,
    )


def deployment_posture() -> DeploymentStatus:
    """Convenience passthrough for callers that need the whole status."""
    return resolver.deployment_status()
