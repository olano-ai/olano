"""Dashboard API route modules.

``olano.runtime.api.create_api`` used to hold every route of the runtime API as
closures inside one function. The routes now live here, one module per topic,
each exposing ``register_<topic>_routes(app, runtime, closures)``; the body of
every handler is unchanged. Two conventions hold the split together:

- Helpers that still live in ``olano.runtime.api`` are reached through the
  module object (``_api.<name>``) rather than imported by name, so tests that
  monkeypatch them on ``olano.runtime.api`` keep working. Moving those helpers
  into their own modules, and importing them directly, is the natural next step.
- A handful of closures are used by more than one topic. The module that
  defines one publishes it in ``closures`` and the modules that need it bind it
  from there, which is why ``register_all_routes`` registers the modules in a
  fixed order (definers first).

Older route modules (``groups_routes``, ``issues_routes``, ``pages_routes``, the
webhook modules) predate this package and stay where they are.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from olano.runtime.routes import (
    a2a,
    agents,
    analytics,
    archetypes,
    avatar,
    browser,
    catalog,
    channels,
    chat,
    cognition,
    connections,
    credits,
    customers,
    feedback,
    files,
    hitl,
    lifecycle,
    mcp,
    mobile,
    observability,
    olano_credits,
    pairing,
    pulse,
    push,
    revisions,
    scheduler,
    self_learning,
    shell,
    status,
    subagents,
    system_config,
    terminal,
    threads,
    vault,
    webhooks,
)

__all__ = ["register_all_routes"]


def register_all_routes(app: FastAPI, runtime: Any) -> None:
    """Attach every dashboard API route to ``app`` in dependency order."""
    closures: dict[str, Any] = {}
    terminal.register_terminal_routes(app, runtime, closures)
    browser.register_browser_routes(app, runtime, closures)
    avatar.register_avatar_routes(app, runtime, closures)
    status.register_status_routes(app, runtime, closures)
    agents.register_agents_routes(app, runtime, closures)
    a2a.register_a2a_routes(app, runtime, closures)
    analytics.register_analytics_routes(app, runtime, closures)
    chat.register_chat_routes(app, runtime, closures)
    lifecycle.register_lifecycle_routes(app, runtime, closures)
    archetypes.register_archetypes_routes(app, runtime, closures)
    subagents.register_subagents_routes(app, runtime, closures)
    channels.register_channels_routes(app, runtime, closures)
    customers.register_customers_routes(app, runtime, closures)
    webhooks.register_webhooks_routes(app, runtime, closures)
    mcp.register_mcp_routes(app, runtime, closures)
    threads.register_threads_routes(app, runtime, closures)
    feedback.register_feedback_routes(app, runtime, closures)
    catalog.register_catalog_routes(app, runtime, closures)
    hitl.register_hitl_routes(app, runtime, closures)
    pairing.register_pairing_routes(app, runtime, closures)
    credits.register_credits_routes(app, runtime, closures)
    olano_credits.register_olano_credits_routes(app, runtime, closures)
    scheduler.register_scheduler_routes(app, runtime, closures)
    files.register_files_routes(app, runtime, closures)
    vault.register_vault_routes(app, runtime, closures)
    connections.register_connections_routes(app, runtime, closures)
    shell.register_shell_routes(app, runtime, closures)
    pulse.register_pulse_routes(app, runtime, closures)
    push.register_push_routes(app, runtime, closures)
    mobile.register_mobile_routes(app, runtime, closures)
    cognition.register_cognition_routes(app, runtime, closures)
    system_config.register_system_config_routes(app, runtime, closures)
    revisions.register_revisions_routes(app, runtime, closures)
    observability.register_observability_routes(app, runtime, closures)
    self_learning.register_self_learning_routes(app, runtime, closures)
