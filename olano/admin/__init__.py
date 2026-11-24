"""Validated management operations for olano itself.

This is the single source of truth for "how to manage a olano deployment":
create/update/delete agents, scaffold from archetypes, wire tools and MCP
servers, author skills, read/patch the home config, and inspect logs.

Three consumers build on this one layer:

* ``olano/tools/olano_admin.py`` — wraps these functions as LangChain
  tools for the **AgentFather** agent.
* the ``olano`` CLI — thin command wrappers.
* ``skills/built_in/olano-admin/SKILL.md`` — documents the same operations
  for external systems (Claude Code, Codex CLI).

Every function validates its inputs through the Pydantic models in
``olano.config`` *before* writing anything, returns plain
JSON-serialisable values, and avoids any dependency on a running runtime so it
can be unit-tested in isolation.
"""

from __future__ import annotations

# Exposed as submodules, not flattened: their verbs (list_tasks / delete_task,
# list_items / promote_item) would read as agent operations next to
# admin.list_agents. Callers say admin.scheduler.list_tasks(...) and
# admin.cortex.list_items(...).
from olano.admin import cortex, scheduler
from olano.admin.agent_exec import run_agent_command
from olano.admin.agents import (
    create_agent,
    delete_agent,
    get_agent,
    list_agents,
    set_external_access,
    update_agent,
)
from olano.admin.archetypes import list_archetypes, read_archetype, scaffold_from_archetype
from olano.admin.config import get_home_config, update_home_config
from olano.admin.diagnostics import (
    agent_activity,
    agent_status,
    diagnose_agent,
    mcp_status,
    runtime_status,
)
from olano.admin.disk import (
    disk_report,
    set_disk_monitor,
)
from olano.admin.integrations import (
    add_toolkit,
    find_integration,
    inspect_external,
    remove_toolkit,
)
from olano.admin.lifecycle import require_agent, restart_agent, start_agent, stop_agent
from olano.admin.logs import (
    agent_errors,
    list_sessions,
    read_session,
    recent_errors,
    tail_log,
)
from olano.admin.mcp import (
    add_mcp_server,
    authorize_mcp_server,
    get_mcp_directory_entry,
    list_mcp_servers,
    mcp_authorization_status,
    remove_mcp_server,
    search_mcp_directory,
    start_mcp_authorization,
    test_mcp_server,
)
from olano.admin.memory import (
    memory_report,
    set_memory_monitor,
)
from olano.admin.models import (
    cheapest_model,
    list_models,
    model_provider_connected,
    provider_connection,
)
from olano.admin.platforms import list_platforms
from olano.admin.skills import (
    create_skill,
    install_skill,
    list_agent_skills,
    list_builtin_skills,
    list_skills,
    repair_skill_names,
    set_skill_enabled,
)
from olano.admin.subagents import (
    add_subagent,
    list_builtin_agents,
    list_subagents,
    toggle_subagent,
)
from olano.admin.system import doctor, lionclaw_version
from olano.admin.tools import (
    describe_tool,
    find_tools,
    list_bundles,
    list_integrations,
    list_tools,
    set_tool_enabled,
)
from olano.admin.users import (
    access_overview,
    create_user,
    delete_user,
    get_user,
    grant_agent_access,
    list_users,
    reset_user_password,
    revoke_agent_access,
    set_user_role,
    update_user,
)
from olano.admin.workspace_files import (
    list_agent_workspace,
    read_agent_workspace_file,
    write_agent_workspace_file,
)

__all__ = [
    "access_overview",
    "add_mcp_server",
    "add_subagent",
    "add_toolkit",
    "agent_activity",
    "agent_errors",
    "agent_status",
    "authorize_mcp_server",
    "cheapest_model",
    "cortex",
    "create_agent",
    "create_skill",
    "create_user",
    "delete_agent",
    "delete_user",
    "describe_tool",
    "diagnose_agent",
    "disk_report",
    "doctor",
    "find_integration",
    "find_tools",
    "get_agent",
    "get_home_config",
    "get_mcp_directory_entry",
    "get_user",
    "grant_agent_access",
    "inspect_external",
    "install_skill",
    "lionclaw_version",
    "list_agent_skills",
    "list_agent_workspace",
    "list_agents",
    "list_archetypes",
    "list_builtin_agents",
    "list_builtin_skills",
    "list_bundles",
    "list_integrations",
    "list_mcp_servers",
    "list_models",
    "list_platforms",
    "list_sessions",
    "list_skills",
    "list_subagents",
    "list_tools",
    "list_users",
    "mcp_authorization_status",
    "mcp_status",
    "memory_report",
    "model_provider_connected",
    "provider_connection",
    "read_agent_workspace_file",
    "read_archetype",
    "read_session",
    "recent_errors",
    "remove_mcp_server",
    "remove_toolkit",
    "repair_skill_names",
    "require_agent",
    "reset_user_password",
    "restart_agent",
    "revoke_agent_access",
    "run_agent_command",
    "runtime_status",
    "scaffold_from_archetype",
    "scheduler",
    "search_mcp_directory",
    "set_disk_monitor",
    "set_external_access",
    "set_memory_monitor",
    "set_skill_enabled",
    "set_tool_enabled",
    "set_user_role",
    "start_agent",
    "start_mcp_authorization",
    "stop_agent",
    "tail_log",
    "test_mcp_server",
    "toggle_subagent",
    "update_agent",
    "update_home_config",
    "update_user",
    "write_agent_workspace_file",
]
