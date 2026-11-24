"""Non-interactive CLI execution for Olano."""

from __future__ import annotations

import contextlib
import json
import logging
import shlex
import sys
import uuid
from dataclasses import dataclass
from typing import Any

from rich.console import Console

from olano.cli._quiet import run_quietly_async
from olano.security.shell_guard import normalize_command, validate_command

logger = logging.getLogger(__name__)

_SHELL_ACTION_NAMES = frozenset({"execute", "safe_shell", "safe_shell_tool", "node_run"})
_RECOMMENDED_ALLOW_LIST = (
    "exec:pwd",
    "exec:ls",
    "exec:cat",
    "exec:head",
    "exec:tail",
    "exec:grep",
    "exec:rg",
    "exec:find",
    "exact:git status",
    "prefix:uv run pytest",
    "prefix:uv run ruff",
)


@dataclass(frozen=True)
class ShellAllowRule:
    kind: str
    value: str = ""


def parse_shell_allow_list(spec: str | None) -> tuple[ShellAllowRule, ...]:
    """Parse CLI shell allow-list rules."""
    if not spec or not spec.strip():
        return ()

    parsed: list[ShellAllowRule] = []
    seen: set[tuple[str, str]] = set()

    def _append(rule: ShellAllowRule) -> None:
        key = (rule.kind, rule.value)
        if key not in seen:
            seen.add(key)
            parsed.append(rule)

    for raw_entry in spec.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        lowered = entry.lower()
        if lowered == "recommended":
            for recommended in _RECOMMENDED_ALLOW_LIST:
                for rule in parse_shell_allow_list(recommended):
                    _append(rule)
            continue
        if lowered == "all":
            _append(ShellAllowRule("all", ""))
            continue

        if ":" not in entry:
            raise ValueError(
                f"Invalid shell allow-list entry '{entry}'. Use exec:, prefix:, exact:, recommended, or all."
            )
        kind, value = entry.split(":", 1)
        kind = kind.strip().lower()
        value = value.strip()
        if kind not in {"exec", "prefix", "exact"} or not value:
            raise ValueError(
                f"Invalid shell allow-list entry '{entry}'. Use exec:, prefix:, exact:, recommended, or all."
            )
        _append(ShellAllowRule(kind, normalize_command(value)))

    return tuple(parsed)


def _command_matches_rule(command: str, rule: ShellAllowRule) -> bool:
    normalized = normalize_command(command)
    if rule.kind == "all":
        return True
    if rule.kind == "exact":
        return normalized == rule.value
    if rule.kind == "prefix":
        return normalized.startswith(rule.value)
    if rule.kind == "exec":
        try:
            parts = shlex.split(normalized)
        except ValueError:
            return False
        return bool(parts) and parts[0] == rule.value
    return False


def is_shell_command_allowed(command: str, rules: tuple[ShellAllowRule, ...]) -> bool:
    return any(_command_matches_rule(command, rule) for rule in rules)


def _write_text(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _write_newline() -> None:
    sys.stdout.write("\n")
    sys.stdout.flush()


def _extract_action_command(action: dict[str, Any]) -> str:
    args = action.get("args", {})
    if isinstance(args, dict):
        return str(args.get("command") or args.get("cmd") or "")
    if isinstance(args, str):
        raw = args.strip()
        if raw.startswith("{"):
            with contextlib.suppress(Exception):
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return str(parsed.get("command") or parsed.get("cmd") or raw)
        return raw
    return ""


async def _run_quietly_async(fn, *args, **kwargs):
    return await run_quietly_async(fn, *args, **kwargs)


def _clone_non_interactive_config(config):
    cloned = config.model_copy(deep=True)
    cloned.security.hitl_enabled = True
    cloned.security.hitl_shell = True
    cloned.security.hitl_auto_approve_low_risk = False
    return cloned


def _format_usage(usage: dict[str, Any]) -> str:
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    total_tokens = usage.get("total_tokens", input_tokens + output_tokens)
    return f"Tokens: in={input_tokens} out={output_tokens} total={total_tokens}"


def _quiet_diagnostics_enabled() -> bool:
    """Return True when quiet-mode diagnostics should still be emitted."""
    stderr = sys.stderr
    if stderr is None:
        return False
    try:
        return not stderr.isatty()
    except (AttributeError, OSError, ValueError):
        return False


async def run_non_interactive(
    agent_name: str,
    message: str,
    *,
    thread_id: str | None = None,
    model_override: str | None = None,
    quiet: bool = False,
    stream: bool = True,
    shell_allow_list: str | None = None,
    config_source: str = "auto",
) -> int:
    """Run a single task non-interactively and return a process exit code."""
    from olano.config.loader import load_agent_config
    from olano.core.builder import build_agent_async, build_agent_with_mcp, stop_rag_watchers
    from olano.core.checkpoints import aclose_checkpointers
    from olano.core.runner import AgentError, AgentRunner
    from olano.core.stores import aclose_stores

    console = Console(stderr=True) if quiet else Console()
    emit_diagnostics = (not quiet) or _quiet_diagnostics_enabled()
    rules = parse_shell_allow_list(shell_allow_list)
    selected_thread_id = thread_id or str(uuid.uuid4())

    try:
        config = load_agent_config(agent_name, source=config_source)
    except FileNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        return 1

    if model_override:
        config.model = model_override
    runtime_config = _clone_non_interactive_config(config)

    if emit_diagnostics:
        console.print(
            f"[dim]Agent:[/dim] [bold]{runtime_config.emoji} {runtime_config.name}[/bold]  "
            f"[dim]model={runtime_config.model} thread={selected_thread_id}[/dim]"
        )

    async def _approval_handler(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        decisions: list[dict[str, Any]] = []
        for action in actions:
            action_name = str(action.get("name", "tool"))
            if action_name not in _SHELL_ACTION_NAMES:
                if emit_diagnostics:
                    console.print(f"[dim]✓ Auto-approved action: {action_name}[/dim]")
                decisions.append({"type": "approve"})
                continue

            command = _extract_action_command(action)
            allowed, reason = validate_command(command)
            if not allowed:
                if emit_diagnostics:
                    console.print(f"[red]Shell command rejected:[/red] {command}")
                    console.print(f"[yellow]{reason}[/yellow]")
                decisions.append({"type": "reject", "message": reason})
                continue

            if not rules:
                message_text = (
                    "Shell commands are not permitted in non-interactive mode "
                    "without --shell-allow-list."
                )
                if emit_diagnostics:
                    console.print(f"[red]Shell command rejected:[/red] {command}")
                    console.print(f"[yellow]{message_text}[/yellow]")
                decisions.append({"type": "reject", "message": message_text})
                continue

            if is_shell_command_allowed(command, rules):
                if emit_diagnostics:
                    console.print(f"[dim]✓ Auto-approved shell: {command}[/dim]")
                decisions.append({"type": "approve"})
                continue

            message_text = "Shell command rejected by allow-list policy."
            if emit_diagnostics:
                console.print(f"[red]Shell command rejected:[/red] {command}")
                console.print(f"[yellow]{message_text}[/yellow]")
            decisions.append({"type": "reject", "message": message_text})
        return decisions

    buffered_text: list[str] = []
    saw_text = False
    usage: dict[str, Any] | None = None

    try:
        try:
            builder = build_agent_with_mcp if runtime_config.mcp_servers else build_agent_async
            agent = await _run_quietly_async(builder, runtime_config)
        except Exception as exc:
            console.print(f"[red]Failed to build agent:[/red] {exc}")
            return 1

        runner = AgentRunner(agent, runtime_config)

        try:
            async for event_type, payload in runner.astream_events(
                message,
                thread_id=selected_thread_id,
                approval_handler=_approval_handler,
            ):
                if event_type == "text":
                    text = str(payload)
                    saw_text = True
                    if stream:
                        _write_text(text)
                    else:
                        buffered_text.append(text)
                    continue

                if event_type == "usage":
                    usage = payload if isinstance(payload, dict) else None
                    continue

                if not emit_diagnostics:
                    continue

                if event_type == "tool_call" and isinstance(payload, dict):
                    console.print(f"[dim]Calling tool: {payload.get('name', 'tool')}[/dim]")
                elif event_type == "tool_result" and isinstance(payload, dict):
                    content = str(payload.get("content", ""))
                    if content:
                        console.print(
                            f"[dim]{payload.get('name', 'tool')} -> {content[:200]}[/dim]"
                        )
                elif event_type == "status" and isinstance(payload, dict):
                    console.print(f"[dim]{payload.get('message', '')}[/dim]")
                elif event_type == "thread_switch" and isinstance(payload, dict):
                    console.print(
                        f"[dim]Thread switched to {payload.get('new_thread_id', selected_thread_id)}[/dim]"
                    )
        except AgentError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            return 1

        if buffered_text:
            _write_text("".join(buffered_text))
        if saw_text or buffered_text:
            _write_newline()

        if emit_diagnostics:
            console.print("[green]✓ Task completed[/green]")
            if usage:
                console.print(f"[dim]{_format_usage(usage)}[/dim]")
        return 0
    finally:
        stop_rag_watchers()
        await aclose_checkpointers()
        await aclose_stores()
