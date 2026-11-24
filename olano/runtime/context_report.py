"""Context-usage breakdown for the dashboard chat ``/context`` command.

Estimates how the agent's context window is spent **before** any conversation
turn — the static footprint of the system prompt, tool schemas, subagents,
memory files and skill summaries — broken down by category the same way Claude
Code's ``/context`` does.

These are *estimates* (a chars-per-token heuristic over the real source files),
never billed numbers. The live conversation fill (``Messages`` / ``Free space``)
is derived on the client from the last turn's actual ``usage.context``; this
module only owns the static, server-side pieces.

The public entry point is :func:`build_context_report` (load-by-name, used by the
API); the heavy lifting lives in :func:`report_for` so it can be unit-tested with
an in-memory ``AgentConfig`` + ``Workspace`` pair.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig
    from olano.config.workspace import Workspace

# Rough chars-per-token ratio used across LLM tooling for back-of-envelope
# estimates. The breakdown is explicitly labelled "estimated", so this needn't be
# exact — it just has to be stable and in the right ballpark.
_CHARS_PER_TOKEN = 4

# Flat token overhead per tool for the JSON-schema envelope (name + description +
# parameter schema wrapper) so the system-tools bucket isn't undercounted when a
# tool's own text is short. Deepagents built-ins (write_todos, task, ls, read_file,
# …) aren't in olano's registry and have no resolvable object, so they're
# charged this baseline alone.
_TOOL_SCHEMA_OVERHEAD = 40
_BUILTIN_TOOL_TOKENS = 80


def estimate_tokens(text: str) -> int:
    """Approximate token count of ``text`` via a chars-per-token heuristic."""
    if not text:
        return 0
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


def _system_prompt_tokens(config: AgentConfig, workspace: Workspace) -> int:
    """L0 framing + the agent's instructions + persona files (not memory)."""
    total = 0
    try:
        from olano.core.prompt_assembler import PromptAssembler

        assembler = PromptAssembler(config, workspace)
        total += estimate_tokens(assembler.build_l0())
        # Effective instructions = config.system_prompt + an authored AGENTS.md
        # (additive). Delegate to the assembler so this estimate matches the real
        # prompt exactly — including the pristine-scaffold guard.
        total += estimate_tokens(assembler._effective_system_prompt())
    except Exception:
        pass
    # Persona / operating files folded into the prompt at session startup.
    for fname in ("SOUL.md", "IDENTITY.md", "USER.md", "TOOLS.md"):
        try:
            total += estimate_tokens(workspace.read_file(fname) or "")
        except Exception:
            continue
    return total


def _memory_tokens(workspace: Workspace) -> tuple[int, int]:
    """MEMORY.md + today's & yesterday's daily logs. Returns (tokens, file count)."""
    tokens = 0
    count = 0
    try:
        mem = workspace.read_file("MEMORY.md")
        if mem:
            tokens += estimate_tokens(mem)
            count += 1
    except Exception:
        pass
    today = date.today()
    for d in (today, today - timedelta(days=1)):
        try:
            log = workspace.read_file(f"memories/{d.isoformat()}.md")
        except Exception:
            log = ""
        if log:
            tokens += estimate_tokens(log)
            count += 1
    return tokens, count


def _skill_tokens(config: AgentConfig, workspace: Workspace) -> tuple[int, int]:
    """Injected skill summaries (name + description per skill). Returns (tokens, count)."""
    try:
        from pathlib import Path

        from olano.skills.loader import discover_skills, load_skills_for_agent
    except Exception:
        return 0, 0
    tokens = 0
    seen: set[str] = set()
    try:
        skill_dirs = load_skills_for_agent(config, workspace)
    except Exception:
        return 0, 0
    for d in skill_dirs:
        try:
            found = discover_skills(Path(d))
        except Exception:
            continue
        for s in found:
            name = s.get("name", "")
            if not name or name in seen:
                continue
            seen.add(name)
            tokens += estimate_tokens(f"{name}: {s.get('description', '')}")
    return tokens, len(seen)


def _subagent_tokens(config: AgentConfig) -> tuple[int, int]:
    """Each enabled subagent's name + description + prompt. Returns (tokens, count)."""
    tokens = 0
    count = 0
    for sub in getattr(config, "subagents", None) or []:
        if not getattr(sub, "enabled", True):
            continue
        count += 1
        parts = [
            getattr(sub, "name", "") or "",
            getattr(sub, "description", "") or "",
            getattr(sub, "system_prompt", "") or "",
        ]
        tokens += estimate_tokens(" ".join(parts))
    return tokens, count


def tool_schema_tokens(tool: Any) -> int:
    """Estimated request cost of one bound tool: name + description + args schema.

    The same pricing ``_tool_tokens`` applies to olano's own toolkits, exposed
    so the builder can price each MCP tool at connect time (``_build_mcp_tools``
    records it on the summary row) and this report can add it up without
    holding the tool objects.
    """
    name = getattr(tool, "name", "") or ""
    desc = getattr(tool, "description", "") or ""
    schema = ""
    try:
        args = getattr(tool, "args", None)
        schema = json.dumps(args) if args else ""
    except Exception:
        schema = ""
    return estimate_tokens(f"{name} {desc} {schema}") + _TOOL_SCHEMA_OVERHEAD


def _mcp_tool_tokens(mcp_summary: Iterable[dict[str, Any]] | None) -> tuple[int, int, int]:
    """Bound MCP tool schemas. Returns (tokens, tool count, connected servers).

    Every tool a connected server exposes is bound into every request, exactly
    like a system tool — this bucket used to be hardcoded to 0 as "on-demand",
    which pushed Composio's and Apollo's schemas into the *Messages* bar on the
    client, since that bar is the measured window minus the static estimate.
    """
    tokens = 0
    count = 0
    servers = 0
    for row in mcp_summary or []:
        if not row.get("ok"):
            continue
        servers += 1
        details = row.get("tool_details") or []
        if details:
            tokens += sum(int(d.get("tokens", 0) or 0) for d in details)
            count += len(details)
        else:
            names = row.get("tools") or []
            tokens += len(names) * _BUILTIN_TOOL_TOKENS
            count += len(names)
    return tokens, count, servers


def _tool_tokens(config: AgentConfig) -> tuple[int, int]:
    """Resolved tool schemas. Returns (tokens, count).

    Counts every tool the agent actually runs with (configured tools + universal
    defaults + deepagents built-ins), pricing resolvable tools by their real
    name/description/args schema and charging built-ins a flat baseline.
    """
    try:
        from olano.config.ops_defaults import resolve_effective_tool_names

        effective = resolve_effective_tool_names(
            list(getattr(config, "tools", []) or []),
            getattr(config, "disabled_tools", None) or [],
            config,
        )
    except Exception:
        effective = [t for t in (getattr(config, "tools", []) or []) if t]

    priced: dict[str, int] = {}
    try:
        from olano.tools.registry import resolve_tools

        for tool in resolve_tools(effective):
            name = getattr(tool, "name", "") or ""
            if not name:
                continue
            priced[name] = tool_schema_tokens(tool)
    except Exception:
        priced = {}

    tokens = sum(priced.get(name, _BUILTIN_TOOL_TOKENS) for name in effective)
    return tokens, len(effective)


def report_for(
    config: AgentConfig,
    workspace: Workspace,
    *,
    mcp_summary: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the context-usage breakdown for an in-memory config + workspace.

    ``mcp_summary`` is the agent's live per-server connect summary
    (``builder.get_mcp_summary``); without it (agent not running) the MCP
    bucket reads 0 tools, which is also what the model is bound to then.
    """
    sys_tokens = _system_prompt_tokens(config, workspace)
    tool_tokens, tool_count = _tool_tokens(config)
    sub_tokens, sub_count = _subagent_tokens(config)
    mem_tokens, mem_count = _memory_tokens(workspace)
    skill_tokens, skill_count = _skill_tokens(config, workspace)
    mcp_tokens, mcp_count, mcp_servers = _mcp_tool_tokens(mcp_summary)

    return {
        "model": getattr(config, "model", "") or "",
        # Ordered to match the dashboard legend / Claude Code layout.
        "categories": [
            {"key": "system_prompt", "tokens": sys_tokens},
            {"key": "system_tools", "tokens": tool_tokens, "count": tool_count},
            {"key": "subagents", "tokens": sub_tokens, "count": sub_count},
            {"key": "memory_files", "tokens": mem_tokens, "count": mem_count},
            {"key": "skills", "tokens": skill_tokens, "count": skill_count},
            # Every tool a connected MCP server exposes is bound into every
            # request, so it is priced like a system tool. `servers` is the
            # connected-server count for the label.
            {
                "key": "mcp_tools",
                "tokens": mcp_tokens,
                "count": mcp_count,
                "servers": mcp_servers,
            },
        ],
    }


def build_context_report(name: str) -> dict[str, Any]:
    """Load agent ``name`` from disk and build its context-usage breakdown."""
    from olano.config.loader import load_agent_config
    from olano.config.workspace import get_workspace
    from olano.core.builder import get_mcp_summary

    config = load_agent_config(name)
    workspace = get_workspace(config.workspace)
    return report_for(config, workspace, mcp_summary=get_mcp_summary(name))


def thread_message_tokens(messages: Iterable[Any]) -> int:
    """Estimate the live conversation fill from LangChain messages.

    Sums the chars-per-token estimate over the *effective* message list of a
    thread (after a compaction: the summary plus the kept tail), tool calls and
    tool results included — the JSONL-based :func:`session_message_tokens`
    below cannot see those. Used by the ``/compact`` endpoint to report the
    post-compaction size, because the chat header otherwise keeps showing the
    last streamed turn's measured fill until another turn runs.
    """
    total = 0
    for msg in messages or []:
        try:
            content = getattr(msg, "content", msg)
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = json.dumps(content, ensure_ascii=False, default=str)
            else:
                text = str(content or "")
            total += estimate_tokens(text) + 3
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                total += estimate_tokens(json.dumps(tool_calls, ensure_ascii=False, default=str))
        except Exception:  # pragma: no cover - defensive over exotic messages
            continue
    return total


# ── text rendering for the chat ``/context`` command ──────────────────────────
#
# The dashboard renders the same ``report_for`` payload as a coloured grid card.
# Chat platforms (Telegram, WhatsApp, Discord, Slack, SMS, …) have no such
# surface, so :func:`render_context_report_text` turns the payload
# into a plain-text breakdown that reads well in both monospace (code blocks,
# terminal) and proportional (most chat clients) fonts.


def model_context_window(model: str | None) -> int | None:
    """Context-window size (tokens) for a ``provider:model`` string.

    Resolves per-model from the synced cloud catalog first; the provider-prefix
    ladder below is the fallback for models the catalog doesn't know (it mirrors
    the dashboard's ``modelContextWindow()`` in ``dashboard_2.0/src/views/chat/
    models.ts`` so the chat ``/context`` percentages match the card).
    Returns ``None`` for providers without a known window — the renderer then
    shows raw token counts instead of a percent-full bar.
    """
    if not model:
        return None
    try:
        # `default:*` aliases have no window of their own — the target does.
        from olano.models.aliases import resolve_model_alias

        model = resolve_model_alias(model)
    except Exception:  # an unresolvable alias must never break /context
        pass
    try:
        from olano.catalog import models_registry

        window = models_registry.model_context_window(model)
        if window is not None:
            return window
    except Exception:  # catalog trouble must never break /context
        pass
    mid = model.lower()
    if mid.startswith("olano:"):
        # Managed tiers can't be catalog-resolved until the cloud publishes
        # them as rows; the static table is the conservative floor (min across
        # each tier's routed targets — see OLANO_TIER_INFO).
        from olano.models.olano_gateway import tier_context_window

        return tier_context_window(mid)
    if mid.startswith("anthropic:"):
        return 200_000
    if mid.startswith("openai:"):
        # Reasoning models (o1/o3) ship a 200K window; the GPT-4o/4.1/5 family 128K.
        if "o1" in mid or "o3" in mid:
            return 200_000
        return 128_000
    if mid.startswith("google:") or mid.startswith("gemini:"):
        return 1_000_000
    if mid.startswith("deepseek:"):
        return 1_000_000
    if mid.startswith("mistral:"):
        return 128_000
    if mid.startswith("xai:") or "grok" in mid:
        return 131_072
    if mid.startswith("ollama:"):
        return 32_768
    return None


def session_message_tokens(entries: Iterable[dict[str, Any]] | None) -> int:
    """Estimate the live conversation fill from persisted session entries.

    Sums the chars-per-token estimate over ``user``/``assistant`` turns of a
    thread's JSONL session (``AgentRunner.load_session``). ``system`` entries are
    skipped — the system prompt is already priced in the ``system_prompt``
    bucket, so counting it here would double it. This is an estimate (the JSONL
    doesn't capture tool-call/result traffic), matching the "estimated" framing
    of the rest of the report.
    """
    total = 0
    for entry in entries or []:
        try:
            if str(entry.get("role", "")) == "system":
                continue
            total += estimate_tokens(str(entry.get("content", "") or ""))
        except Exception:  # pragma: no cover - defensive over malformed rows
            continue
    return total


_CATEGORY_LABELS: dict[str, str] = {
    "system_prompt": "System prompt",
    "system_tools": "System tools",
    "subagents": "Subagents",
    "memory_files": "Memory files",
    "skills": "Skills",
    "mcp_tools": "MCP tools",
    "messages": "Messages",
    "free": "Free space",
}

# Noun for the "· N <noun>" count suffix on each row (blank → number only).
_CATEGORY_COUNT_NOUN: dict[str, str] = {
    "system_tools": "tools",
    "subagents": "subagents",
    "memory_files": "files",
    "skills": "skills",
    "mcp_tools": "tools",
}

_LABEL_WIDTH = max(len(v) for v in _CATEGORY_LABELS.values())


def _fmt_tokens(n: int) -> str:
    """Compact human token count: ``1234`` → ``1.2k``, ``950`` → ``950``."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _usage_bar(used: int, total: int, width: int = 28) -> str:
    """A proportional ``█``/``░`` bar (renders in every chat client + terminal)."""
    if total <= 0:
        return ""
    filled = min(width, max(0, round(used / total * width)))
    return "█" * filled + "░" * (width - filled)


def render_context_report_text(
    report: dict[str, Any],
    *,
    message_tokens: int = 0,
    show_model: bool = False,
) -> str:
    """Render :func:`report_for` output as a chat-friendly text breakdown.

    ``message_tokens`` is the estimated live conversation fill (see
    :func:`session_message_tokens`); pass ``0`` for a fresh thread. The static
    footprint plus messages gives "used"; the model's context window (when known)
    gives the percent-full bar and free space.

    ``show_model`` names the model in the header. It defaults to **off**: on a
    chat platform ``/model`` is a *sensitive* command precisely so a bot doesn't
    advertise which provider backs it, and ``/context`` — exposed by default —
    used to print it anyway. Callers on an operator surface (no per-sender
    identity) pass ``True``, where it is a diagnostic the operator is
    entitled to and the window size only means something next to its model.
    """
    model = str(report.get("model") or "")
    categories = list(report.get("categories") or [])
    static_tokens = sum(int(c.get("tokens", 0) or 0) for c in categories)
    messages = max(0, int(message_tokens))
    used = static_tokens + messages
    window = model_context_window(model)

    # Ordered segments: static categories, then live messages, then free space.
    segments: list[dict[str, Any]] = [
        {
            "key": c["key"],
            "tokens": int(c.get("tokens", 0) or 0),
            "count": c.get("count"),
            "on_demand": bool(c.get("on_demand", False)),
        }
        for c in categories
    ]
    segments.append({"key": "messages", "tokens": messages, "count": None, "on_demand": False})
    if window:
        segments.append(
            {
                "key": "free",
                "tokens": max(0, window - used),
                "count": None,
                "on_demand": False,
            }
        )

    total = window or used  # percent basis; falls back to "used" when no window

    header = "📊 Context"
    if show_model:
        header += f" — {model or 'unknown model'}"
    lines: list[str] = [header]
    if window:
        pct = (used / window * 100) if window else 0.0
        lines.append(f"~{_fmt_tokens(used)} / {_fmt_tokens(window)} tokens used ({pct:.0f}%)")
        lines.append(_usage_bar(used, window))
    else:
        lines.append(f"~{_fmt_tokens(used)} tokens used · context window unknown for this model")
    lines.append("")

    for seg in segments:
        key = str(seg["key"])
        label = _CATEGORY_LABELS.get(key, key)
        dot = "○" if key == "free" else "●"
        if seg["on_demand"]:
            value = "on-demand"
        elif window:
            value = f"{_fmt_tokens(int(seg['tokens']))} ({int(seg['tokens']) / total * 100:.0f}%)"
        else:
            value = _fmt_tokens(int(seg["tokens"]))
        row = f"  {dot} {label:<{_LABEL_WIDTH}}  {value}"
        count = seg.get("count")
        if count is not None:
            noun = _CATEGORY_COUNT_NOUN.get(key, "")
            if noun and count == 1:
                noun = noun[:-1]  # tools → tool, files → file (only exact 1)
            row += f"  · {count} {noun}".rstrip()
        lines.append(row)

    lines.append("")
    lines.append("Estimated footprint — actual usage is measured per turn.")
    return "\n".join(lines)
