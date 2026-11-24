"""
Skills middleware — intercepts agent tool calls to log skill usage and
provide dynamic skill loading hints.

Register this as a deepagents middleware to get skill usage analytics
and auto-inject skill context when the agent invokes read_file on a SKILL.md.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SkillsMiddleware:
    """
    Middleware that tracks when agents read SKILL.md files and
    logs skill usage for analytics / self-learning purposes.

    Compatible with deepagents middleware interface:
        async def before_tool_call(tool_name, tool_input) -> dict | None
        async def after_tool_call(tool_name, tool_input, tool_output) -> str | None
    """

    def __init__(self, skill_dirs: list[str]) -> None:
        self.skill_dirs = skill_dirs
        self._usage: dict[str, int] = {}

    def _is_skill_read(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """Return skill name if this tool call is reading a SKILL.md."""
        if tool_name not in ("read_file", "read"):
            return None
        path = tool_input.get("path") or tool_input.get("file_path") or ""
        if path.endswith("SKILL.md"):
            return Path(path).parent.name
        return None

    async def before_tool_call(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Called before each tool invocation. Return None to proceed normally."""
        skill = self._is_skill_read(tool_name, tool_input)
        if skill:
            logger.debug("Agent loading skill: %s", skill)
        return None

    async def after_tool_call(
        self, tool_name: str, tool_input: dict[str, Any], tool_output: Any
    ) -> Any | None:
        """Called after each tool invocation. Return None to use original output."""
        skill = self._is_skill_read(tool_name, tool_input)
        if skill:
            self._usage[skill] = self._usage.get(skill, 0) + 1
            logger.info("Skill '%s' loaded (total uses: %d)", skill, self._usage[skill])
        return None

    def usage_report(self) -> dict[str, int]:
        return dict(self._usage)
