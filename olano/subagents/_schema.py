"""Pydantic models for ToolkitAgent definitions and per-install overrides."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from olano.config.agent_config import SecurityConfig


class ToolkitAgentDef(BaseModel):
    """A declarative specialist agent bound to a single toolkit.

    Fields mirror what an installer needs to either compose the agent as a
    subagent (`name` + `description` + `system_prompt` + resolved `tools`) or
    materialize it as a top-level agent.json5 (everything plus `security`).

    `default_tools="all"` resolves to whatever the toolkit's ``get_tools()``
    returns at runtime, so newly added tools in a toolkit show up
    automatically without touching the def.
    """

    name: str
    toolkit: str
    description: str
    emoji: str = "🦁"
    system_prompt: str
    default_tools: Literal["all"] | list[str] = "all"
    security: SecurityConfig | None = None  # None → use _defaults.default_security()
    category: str = "Other"
    catalog_visible: bool = True
    credential_env_vars: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    # Back-compat aliases — additional lookup keys for this def (e.g. legacy
    # short names from older agent.json5 configs).
    aliases: list[str] = Field(default_factory=list)


class ToolkitAgentOverride(BaseModel):
    """Per-install customization for a ToolkitAgent.

    Used both when composing as a subagent (entries in `builtin_subagents`)
    and when installing as a top-level agent (CLI flags / dashboard form).
    None / empty fields mean "inherit from the def."
    """

    name: str
    extra_tools: list[str] = Field(default_factory=list)
    disabled_tools: list[str] = Field(default_factory=list)
    model: str | None = None
    system_prompt: str | None = None
    # Appended to the effective prompt (the override above when set, the def's
    # prompt otherwise) — keeps the def's prompt live while adding owner
    # instructions on top, unlike the full `system_prompt` override.
    system_prompt_extension: str | None = None
