"""Who may run page commands, and where. One answer for every surface.

``pages_deploy`` is command execution with a URL attached: its ``build`` and
``start`` strings run exactly where the agent's own shell runs. So the rules
are the shell's rules, asked through the same predicates, never re-derived:

* **Shell access.** An agent whose config grants no shell (no shell tool, no
  shell backend, no live sandbox) cannot deploy: otherwise ``pages_deploy``
  would be a shell for an agent that was deliberately given none.
* **Where.** The agent's live sandbox when it has one (a sidecar counts), the
  host only on the open posture, and nowhere on a managed box whose sandbox
  is down: the same fail-closed rule as ``tools/_sandbox_exec.sandboxed_run``
  and the security middleware.
* **The switch.** ``config.yaml → pages.services_enabled``.

The tool asks this before recording a deploy, the security middleware asks it
before the tool body runs, and the supervisor asks it again before every build
and start, so a shell taken away later also stops the page.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVICES_DISABLED = (
    "Deployed pages are switched off on this deployment (Config → Pages → "
    "Run page builds and servers). Plain pages published with pages_start still work."
)
NO_SHELL = (
    "This agent has no shell access, so it cannot run build or start commands for a "
    "page. Publish already-built files with pages_start, or ask an agent with shell "
    "access to deploy it."
)
SANDBOX_DOWN = (
    "This agent's sandbox is not running, so no page command can run right now. "
    "The page starts as soon as the sandbox is back (restarting the agent rebuilds it)."
)
NO_SANDBOX_MANAGED = (
    "Managed deployment: page commands run only inside the agent's isolated sandbox, "
    "and this agent has none. Ask the operator to enable the sandbox execution backend."
)

_SHELL_BACKEND_TYPES = frozenset({"local_shell", "filesystem+shell", "lionclaw_docker"})


def services_enabled() -> bool:
    try:
        from olano.config.system_settings import load_pages_settings

        return bool(load_pages_settings().services_enabled)
    except Exception:  # pragma: no cover - load_pages_settings never raises
        return True


def _backend_type(config: Any) -> str:
    backend = getattr(config, "backend", None)
    return str(getattr(backend, "type", None) or (backend if isinstance(backend, str) else ""))


def config_grants_shell(config: Any, *, live_sandbox: bool) -> bool:
    """The security middleware's own shell-access rule, for a config."""
    from olano.security.deepagents_middleware import _SHELL_TOOL_NAMES

    tools = set(getattr(config, "tools", []) or [])
    return (
        bool(tools & _SHELL_TOOL_NAMES)
        or _backend_type(config) in _SHELL_BACKEND_TYPES
        or live_sandbox
    )


def _registered_sandbox(agent: str) -> Any | None:
    try:
        from olano.core.builder import get_sandbox_backend

        return get_sandbox_backend(agent)
    except Exception:
        return None


def live_sandbox(agent: str) -> Any | None:
    from olano.tools._sandbox_exec import _live_sandbox_for

    return _live_sandbox_for(agent)


def host_execution_forbidden() -> bool:
    from olano.tools._sandbox_exec import _host_execution_forbidden

    return _host_execution_forbidden()


def command_denial(agent: str, config: Any | None = None) -> str | None:
    """Why ``agent`` may not run page commands at all, or ``None`` when it may.

    Says nothing about whether the sandbox is up right now: a deploy is
    accepted while the sandbox restarts and runs once it is back
    (:func:`execution_blocker` answers that part).
    """
    if not services_enabled():
        return SERVICES_DISABLED
    if config is None:
        try:
            from olano.config.loader import load_agent_config

            config = load_agent_config(agent)
        except Exception as exc:
            logger.debug("pages: cannot load config for %s: %s", agent, exc)
            return f"Agent '{agent}' has no readable configuration."
    sandbox = live_sandbox(agent)
    if not config_grants_shell(config, live_sandbox=sandbox is not None):
        return NO_SHELL
    if sandbox is None and _registered_sandbox(agent) is None and host_execution_forbidden():
        return NO_SANDBOX_MANAGED
    return None


def execution_blocker(agent: str) -> str | None:
    """Why a command for ``agent`` cannot run *right now*, or ``None``.

    A configured sandbox that is down blocks (never fall back to the host for
    an agent whose config asked for a container); no sandbox on a managed box
    blocks; the open posture without a sandbox runs on the host.
    """
    if live_sandbox(agent) is not None:
        return None
    if _registered_sandbox(agent) is not None:
        return SANDBOX_DOWN
    if host_execution_forbidden():
        return NO_SANDBOX_MANAGED
    return None
