"""Workspace middleware loader — loads and executes Python hook files.

The three-zone model has two middleware directories:

  system/middleware/*.py   — system-managed hooks (updated by olano server)
  extensions/middleware/*.py — user-defined hooks (survive system updates)

Each .py file must define at least one of these callables:

  async def on_input(text: str, config, context: dict) -> str
      Called before user input is sent to the agent. Return (possibly modified) text.
      Raise InputBlockedError to block the message entirely.

  async def on_output(text: str, config, context: dict) -> str
      Called after the agent produces output. Return (possibly modified) text.

  async def on_tool_call(name: str, args: dict, config, context: dict) -> dict | None
      Called before a tool is executed. Return modified args dict, or None to block.

  async def on_tool_result(name: str, result: str, config, context: dict) -> str
      Called after a tool result is returned. Return (possibly modified) result.

Hook files are loaded in alphabetical order within each zone. Extension hooks
run AFTER system hooks so they can override/augment system behavior.

Example system/middleware/01_pii_check.py::

    async def on_input(text, config, context):
        from olano.security.pii_middleware import apply

        cleaned, _ = apply(text, config.security.pii_action)
        return cleaned

Example extensions/middleware/my_hook.py::

    async def on_output(text, config, context):
        # Always append a disclaimer in production
        if config.security.profile == "production":
            return text + "\\n\\n_AI-generated content. Verify before use._"
        return text
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig
    from olano.config.workspace import Workspace

logger = logging.getLogger(__name__)


class InputBlockedError(Exception):
    """Raised by an on_input hook to block a message from reaching the agent."""


class MiddlewareHooks:
    """Container for collected hook callables loaded from middleware directories."""

    def __init__(self) -> None:
        self.on_input: list[Any] = []
        self.on_output: list[Any] = []
        self.on_tool_call: list[Any] = []
        self.on_tool_result: list[Any] = []

    def is_empty(self) -> bool:
        return not any([self.on_input, self.on_output, self.on_tool_call, self.on_tool_result])


def load_middleware(workspace: Workspace, config: AgentConfig) -> MiddlewareHooks:
    """
    Load all middleware hooks from system/middleware/ and extensions/middleware/.

    Extension hooks take precedence by running after system hooks, allowing
    user code to override or augment system behavior.
    """
    hooks = MiddlewareHooks()

    # Load in zone order: system first, then extensions (so extensions can override)
    for zone_dir in [
        workspace.system_dir / "middleware",
        workspace.extensions_dir / "middleware",
    ]:
        if not zone_dir.exists():
            continue

        py_files = sorted(zone_dir.glob("*.py"))
        for py_file in py_files:
            if py_file.name.startswith("_"):
                continue  # skip __init__.py, etc.
            _load_hook_file(py_file, hooks)

    if not hooks.is_empty():
        counts = {
            "input": len(hooks.on_input),
            "output": len(hooks.on_output),
            "tool_call": len(hooks.on_tool_call),
            "tool_result": len(hooks.on_tool_result),
        }
        logger.info("Loaded middleware hooks for %s: %s", config.name, counts)

    return hooks


def _load_hook_file(path: Path, hooks: MiddlewareHooks) -> None:
    """Import a middleware .py file and collect its hook functions."""
    module_name = f"_lionclaw_mw_{path.stem}_{hash(str(path)) & 0xFFFF:04x}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            return
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]

        for hook_name, target_list in [
            ("on_input", hooks.on_input),
            ("on_output", hooks.on_output),
            ("on_tool_call", hooks.on_tool_call),
            ("on_tool_result", hooks.on_tool_result),
        ]:
            fn = getattr(mod, hook_name, None)
            if callable(fn):
                target_list.append(fn)
                logger.debug("  Loaded %s.%s from %s", module_name, hook_name, path.name)

    except Exception as e:
        logger.warning("Failed to load middleware %s: %s", path.name, e)


# ── Hook execution helpers ────────────────────────────────────────────────────


async def run_input_hooks(
    text: str,
    hooks: MiddlewareHooks,
    config: AgentConfig,
    context: dict | None = None,
) -> str:
    """Run all on_input hooks in order. May raise InputBlockedError."""
    ctx = context or {}
    for fn in hooks.on_input:
        try:
            result = fn(text, config, ctx)
            if hasattr(result, "__await__"):
                result = await result
            if result is not None:
                text = result
        except InputBlockedError:
            raise
        except Exception as e:
            logger.warning("on_input hook %s failed: %s", fn.__module__, e)
    return text


async def run_output_hooks(
    text: str,
    hooks: MiddlewareHooks,
    config: AgentConfig,
    context: dict | None = None,
) -> str:
    """Run all on_output hooks in order."""
    ctx = context or {}
    for fn in hooks.on_output:
        try:
            result = fn(text, config, ctx)
            if hasattr(result, "__await__"):
                result = await result
            if result is not None:
                text = result
        except Exception as e:
            logger.warning("on_output hook %s failed: %s", fn.__module__, e)
    return text


async def run_tool_call_hooks(
    name: str,
    args: dict,
    hooks: MiddlewareHooks,
    config: AgentConfig,
    context: dict | None = None,
) -> dict | None:
    """Run all on_tool_call hooks. Returns None if any hook blocks the call."""
    ctx = context or {}
    for fn in hooks.on_tool_call:
        try:
            result = fn(name, args, config, ctx)
            if hasattr(result, "__await__"):
                result = await result
            if result is None:
                logger.info("on_tool_call hook blocked tool '%s'", name)
                return None
            args = result
        except Exception as e:
            logger.warning("on_tool_call hook %s failed: %s", fn.__module__, e)
    return args


async def run_tool_result_hooks(
    name: str,
    result: str,
    hooks: MiddlewareHooks,
    config: AgentConfig,
    context: dict | None = None,
) -> str:
    """Run all on_tool_result hooks in order."""
    ctx = context or {}
    for fn in hooks.on_tool_result:
        try:
            out = fn(name, result, config, ctx)
            if hasattr(out, "__await__"):
                out = await out
            if out is not None:
                result = out
        except Exception as e:
            logger.warning("on_tool_result hook %s failed: %s", fn.__module__, e)
    return result
