"""
Agent self-awareness tools.

make_whoami_tool() is built at agent-construction time so it can close over
the agent's AgentConfig and workspace path. The returned StructuredTool is
registered globally so subagents can resolve it by name.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig


def make_whoami_tool(config: AgentConfig, workspace_root: Any = None) -> Any:
    """Return a no-arg StructuredTool that describes this agent."""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel

    class _NoInput(BaseModel):
        pass

    def _whoami() -> str:
        parts: list[str] = [f"Name: {config.label}"]
        # The handle is what `talk_to_agent(agent_name=…)` and every
        # /api/agents/{name}/… path take, so an agent whose display name
        # differs must be told both — answering "what are you called" with a
        # string that doesn't resolve is worse than not answering.
        if config.label != config.name:
            parts.append(f"Handle: {config.name}")
        # The position — org.title, central store first — so "who are you"
        # can be answered as "Hillary, Recruitment Specialist" and not as one
        # or the other.
        try:
            from olano.config.org_store import resolve_title

            title = resolve_title(config.name, getattr(config, "org", None))
        except Exception:
            title = ""
        if title:
            parts.append(f"Title: {title}")
        parts.append(f"Model: {config.model}")
        if config.description:
            parts.append(f"Description: {config.description}")

        if workspace_root:
            parts.append(f"Workspace: {workspace_root}")

        # Configured tools (after default injection) PLUS the multimodal
        # defaults, which the builder grants outside `tools:` and only when
        # their provider key resolves — an agent asked "what can you do" has to
        # hear that it can see/draw/speak/listen when it actually can.
        from olano.browser.policy import filter_browser_tool_names

        tool_names = filter_browser_tool_names(list(config.tools))
        try:
            from olano.config.multimodal_defaults import available_multimodal_tool_names

            tool_names += available_multimodal_tool_names(config)
        except Exception:
            pass
        # Outbound A2A tools: builder-granted when a remote agent is listed.
        try:
            from olano.config.internal_tools import (
                available_a2a_tool_names,
                available_whatsapp_business_tool_names,
            )

            tool_names += available_a2a_tool_names(config)
            # WhatsApp template send tools: builder-granted with the connector.
            tool_names += available_whatsapp_business_tool_names(config)
        except Exception:
            pass
        # The self-configuration defaults are builder-granted the same way, so
        # whoami must report them under the same predicate.
        try:
            from olano.config.autonomy_defaults import (
                available_magic_link_tool_names,
                available_self_config_tool_names,
            )

            tool_names += available_self_config_tool_names(config)
            tool_names += available_magic_link_tool_names(config)
        except Exception:
            pass
        if tool_names:
            parts.append(f"Tools ({len(tool_names)}): {', '.join(sorted(set(tool_names)))}")

        if config.disabled_tools:
            parts.append(f"Disabled tools: {', '.join(sorted(config.disabled_tools))}")

        if config.subagents:
            parts.append(f"Subagents: {', '.join(s.name for s in config.subagents)}")

        if config.skills:
            parts.append(f"Skills: {', '.join(config.skills)}")

        # Active channels from the delivery registry
        try:
            from olano.platforms.delivery import get_channel_registry

            channels = get_channel_registry().list_channels()
            my_channels = [ctype for aname, ctype in channels if aname == config.name]
            if my_channels:
                parts.append(f"Channels: {', '.join(my_channels)}")
        except Exception:
            pass

        # Scheduler tasks owned by this agent
        try:
            from olano.scheduler.db import SchedulerDB

            db = SchedulerDB()
            tasks = db.list_tasks(caller=config.name)
            if tasks:
                enabled = sum(1 for t in tasks if t.get("enabled"))
                parts.append(f"Scheduled tasks: {len(tasks)} ({enabled} enabled)")
        except Exception:
            pass

        return "\n".join(parts)

    return StructuredTool.from_function(
        func=_whoami,
        name="whoami",
        description=(
            "Returns information about this agent: its name, model, description, "
            "configured tools, subagents, skills, active channels, workspace path, "
            "and scheduled task count."
        ),
        args_schema=_NoInput,
    )
