"""Live progress rendering for messaging platforms.

A long multi-tool turn used to look like a single ``💭 Thinking…`` bubble that
sat there for minutes.  This module turns the typed events streamed by
:meth:`AgentRunner.astream_events` into a **live progress view** that is pushed
to the chat while the agent works — the plan (``write_todos``) as a rendered
checklist, plus the tool calls as they happen with the inputs that matter.

Two layers, deliberately split:

- this module is **pure** (no I/O, no olano imports beyond the equally-pure
  :mod:`olano.platforms.verbosity`) — it accumulates events into a
  :class:`ProgressState` and renders it to text for a given verbosity + markup
  dialect.  Cheap to import, trivial to unit-test.
- :mod:`olano.platforms.progress_reporter` does the delivery: throttling,
  edit-in-place vs append, and the hand-off to the final answer.

The view grows like a log: each new tool call appends a line to the same
rendered message, and the oldest steps roll off (behind a "… N earlier
step(s)" line) only when the size budget forces it. Verbosity shapes how much
of each step is shown (``quiet`` shows nothing at all — the mode exists for
noisy group chats):

- ``normal``  — the checklist, plus the steps rendered one line each: an
  icon, a friendly verb, and the **single most salient input** (the path
  being read, the query being searched, the command being run), truncated
  hard.
- ``verbose`` — every step, all inputs, and an excerpt of what each tool
  returned.

Tool naming across an agent fleet is not standardized (deepagents builtins,
olano toolkits, arbitrary MCP servers), so :func:`describe_tool` matches on
normalized name fragments rather than an exhaustive registry, and degrades to
the raw tool name — an unknown tool still renders a useful line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from olano.platforms.verbosity import (
    DEFAULT_VERBOSITY,
    PEER_TOOL_NAMES,
    SUBAGENT_TOOL_NAME,
    MarkupDialect,
    _resolve_dialect,
    normalize_verbosity,
)

# deepagents' planning tool. Its args carry the whole todo list, which we
# render as a checklist rather than as "called write_todos".
TODO_TOOL_NAMES: tuple[str, ...] = ("write_todos", "write_todo", "todo_write")

# Caps. A progress view is a *status line*, not a transcript — it is re-sent
# (or re-edited) many times per turn, so it has to stay small. Telegram's hard
# limit is 4096 chars; every platform is happier well under that.
MAX_NORMAL_ARG_CHARS = 90
MAX_VERBOSE_ARG_CHARS = 220
MAX_VERBOSE_RESULT_CHARS = 300
MAX_TODO_ITEMS = 12
MAX_TODO_CHARS = 90
# How many steps stay visible. The view *appends* — each new tool call joins
# the list, so the user watches the log grow in one message — and only rolls
# off the oldest steps once the list would blow the size budget below.
MAX_NORMAL_STEPS = 20
MAX_VERBOSE_STEPS = 20
# When even that overflows MAX_PROGRESS_CHARS, halve the visible window (never
# below this floor) — the newest steps are the ones a waiting user came for.
_MIN_VISIBLE_STEPS = 3
# Absolute ceiling on a rendered progress message.
MAX_PROGRESS_CHARS = 3000


# ───────────────────────────────── todos ────────────────────────────────────


@dataclass
class TodoItem:
    content: str
    status: str = "pending"  # pending | in_progress | completed


def _coerce_todos(raw: Any) -> list[TodoItem]:
    """Best-effort ``[{content, status}, …]`` out of whatever the tool passed."""
    if not isinstance(raw, list | tuple):
        return []
    out: list[TodoItem] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or item.get("task") or "").strip()
        if not content:
            continue
        status = str(item.get("status") or "pending").strip().lower()
        if status not in ("pending", "in_progress", "completed"):
            status = "pending"
        out.append(TodoItem(content=content, status=status))
    return out


def parse_todos(args: dict[str, Any]) -> list[TodoItem]:
    """Extract a todo list from ``write_todos`` args.

    Accepts ``{"todos": [...]}`` (deepagents' shape) and a few tolerated
    variants, including a JSON string in place of the list — smaller models
    double-encode it often enough to be worth handling here rather than
    losing the whole checklist to a shrug.
    """
    for key in ("todos", "todo", "items", "plan"):
        value = args.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                continue
        todos = _coerce_todos(value)
        if todos:
            return todos
    return _coerce_todos(args.get("_") if isinstance(args.get("_"), list) else None)


def settle_todos(todos: list[TodoItem]) -> list[TodoItem]:
    """Turn-end view: an item left ``in_progress`` reads as "stuck on step N".

    Smaller models routinely skip the final bookkeeping ``write_todos`` call,
    so at turn end an ``in_progress`` item almost always means "finished but
    not reported". Pending items are left alone so the count stays honest.
    Mirrors ``dashboard_2.0/src/views/chat/todos.ts:settleTodos``.
    """
    return [
        TodoItem(content=t.content, status="completed") if t.status == "in_progress" else t
        for t in todos
    ]


# ─────────────────────────── tool descriptions ──────────────────────────────


@dataclass(frozen=True)
class ToolDescription:
    """How one tool call renders: an icon, a verb, and its salient input."""

    icon: str
    verb: str
    detail: str = ""


# (name fragments, icon, verb, arg keys in order of preference).
#
# Matched against the *normalized* tool name (lowercased, MCP/namespace
# prefixes stripped), longest fragment first, so ``mcp__github__search_code``
# and a bare ``search`` both land somewhere sensible. Order matters: earlier
# entries win, so put specific families above generic verbs.
_TOOL_RULES: tuple[tuple[tuple[str, ...], str, str, tuple[str, ...]], ...] = (
    # Memory / knowledge: the user explicitly wants these visible. Writes and
    # erasures sit above the searches, and `remember` / `forget_me` have rules
    # of their own: `remember` used to share the search rule and read
    # "Searching memory" while it was saving a fact.
    (("forget_me", "forget"), "🧠", "Forgetting what is stored", ()),
    (
        (
            "remember",
            "memory_save",
            "memory_append",
            "profile_save",
            "preference_save",
            "guidance_save",
        ),
        "🧠",
        "Saving to memory",
        ("fact", "content", "text", "entry"),
    ),
    (("memory_compact",), "🧠", "Compacting memory", ()),
    (("recall_all", "memory_read"), "🧠", "Reading memory", ("path",)),
    (
        ("search_memory", "memory_search", "recall"),
        "🧠",
        "Searching memory",
        ("query", "q", "text", "content", "search"),
    ),
    (("memory",), "🧠", "Reading memory", ("query", "q", "path")),
    (
        ("search_knowledge", "knowledge", "rag", "retrieve"),
        "📚",
        "Searching knowledge",
        ("query", "q", "text", "search"),
    ),
    # Git network verbs (a credentialed job container; local git is `execute`).
    (("git_clone",), "🐙", "Cloning", ("url", "dest")),
    (("git_push",), "🐙", "Pushing", ("repo_path", "branch", "remote")),
    (("git_pull",), "🐙", "Pulling", ("repo_path", "branch", "remote")),
    (("git_fetch",), "🐙", "Fetching", ("repo_path", "remote")),
    # Web.
    (
        ("web_search", "tavily", "brave_search", "google_search", "duckduckgo"),
        "🌐",
        "Searching the web",
        ("query", "q", "search_query", "text"),
    ),
    (
        ("web_fetch", "fetch_url", "browse", "http_get", "scrape"),
        "🌐",
        "Fetching",
        ("url", "uri", "link"),
    ),
    # Filesystem.
    (("read_file", "read"), "📖", "Reading", ("file_path", "path", "file", "filename")),
    (
        ("write_file", "create_file", "write"),
        "✏️",
        "Writing",
        ("file_path", "path", "file", "filename"),
    ),
    (("edit_file", "str_replace", "edit"), "✏️", "Editing", ("file_path", "path", "file")),
    (("delete_file", "remove_file", "delete"), "🗑️", "Deleting", ("file_path", "path", "file")),
    (("glob", "find_files", "list_files", "ls"), "📁", "Listing", ("pattern", "path", "glob")),
    (("grep", "ripgrep", "search_code", "search"), "🔎", "Searching", ("pattern", "query", "q")),
    # Shell.
    (
        ("execute", "shell", "bash", "run_command", "terminal", "command"),
        "⚙️",
        "Running",
        ("command", "cmd", "script", "code"),
    ),
    # Outbound messaging / notifications.
    (
        ("send_message", "send_email", "notify", "send_text", "post_message"),
        "📤",
        "Sending",
        ("to", "recipient", "chat_id", "subject", "channel"),
    ),
    # Scheduling.
    (("schedule", "cron", "reminder", "timer"), "⏰", "Scheduling", ("name", "when", "cron")),
    # Code hosting.
    (("pull_request", "issue", "repo", "commit", "github", "gitlab"), "🐙", "GitHub", ()),
)

_FALLBACK_ARG_KEYS: tuple[str, ...] = (
    "query",
    "q",
    "path",
    "file_path",
    "url",
    "command",
    "text",
    "name",
    "content",
    "message",
    "prompt",
    "description",
    "input",
)


def normalize_tool_name(name: str) -> str:
    """Strip MCP/namespace prefixes so rules match the meaningful part.

    ``mcp__github__search_code`` → ``search_code``; ``server.tool`` →
    ``tool``. The display name keeps its original spelling — this is only the
    matching key.
    """
    cleaned = str(name or "").strip().lower()
    for sep in ("__", "::", "/", "."):
        if sep in cleaned:
            cleaned = cleaned.rsplit(sep, 1)[-1]
    return cleaned


def _pick_detail(args: dict[str, Any], preferred: tuple[str, ...]) -> str:
    """The single most informative input value, as a flat string."""
    for key in (*preferred, *_FALLBACK_ARG_KEYS):
        if key not in args:
            continue
        value = args[key]
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
        if isinstance(value, int | float | bool):
            return str(value)
    # Nothing recognized — fall back to the first non-empty scalar arg, so an
    # unknown MCP tool still shows *something* about what it was asked to do.
    for key, value in args.items():
        if str(key).startswith("_"):
            continue
        if isinstance(value, str) and value.strip():
            return f"{key}={' '.join(value.split())}"
    return ""


def describe_tool(name: str, args: dict[str, Any]) -> ToolDescription:
    """Icon + human verb + salient input for one tool call."""
    key = normalize_tool_name(name)

    if name in PEER_TOOL_NAMES or key in PEER_TOOL_NAMES:
        target = str(args.get("agent_name") or args.get("agent") or "agent").strip()
        message = str(args.get("message") or args.get("content") or "").strip()
        return ToolDescription("💬", f"Asking {target}", " ".join(message.split()))
    if name == SUBAGENT_TOOL_NAME or key == SUBAGENT_TOOL_NAME:
        sub = str(args.get("subagent_type") or "subagent").strip()
        description = str(args.get("description") or "").strip()
        return ToolDescription("🤖", f"Delegating to {sub}", " ".join(description.split()))

    for fragments, icon, verb, arg_keys in _TOOL_RULES:
        if any(f in key for f in fragments):
            return ToolDescription(icon, verb, _pick_detail(args, arg_keys))

    # Unknown tool: show the tool's own name as the verb. Underscores read
    # fine here because the name is code-spanned at render time.
    return ToolDescription("🔧", str(name or "tool"), _pick_detail(args, ()))


# ───────────────────────────────── state ────────────────────────────────────


@dataclass
class ProgressStep:
    """One tool call, plus its result once it lands."""

    call_id: str
    name: str
    args: dict[str, Any]
    result: str = ""
    finished: bool = False
    failed: bool = False


@dataclass
class ProgressState:
    """Accumulated, renderable view of a turn in flight.

    ``revision`` increments whenever something *visible* changes, so the
    reporter can skip a push when nothing worth showing happened.
    """

    steps: list[ProgressStep] = field(default_factory=list)
    todos: list[TodoItem] = field(default_factory=list)
    revision: int = 0
    _by_id: dict[str, ProgressStep] = field(default_factory=dict, repr=False)
    _peers: set[str] = field(default_factory=set, repr=False)

    @property
    def has_content(self) -> bool:
        """Whether there is anything worth showing yet."""
        return bool(self.steps or self.todos)

    @property
    def done_count(self) -> int:
        return sum(1 for s in self.steps if s.finished)


def _looks_like_error(content: str) -> bool:
    head = content.strip()[:200].lower()
    return head.startswith("error") or "traceback (most recent call last)" in head


def _parse_args(payload: Any) -> dict[str, Any]:
    """Structured view of a ``tool_call`` payload's ``args``.

    ``astream_events`` yields ``args`` as the raw JSON string the model
    produced, which can be truncated mid-stream — a parse failure is normal,
    not exceptional, and costs only the detail on one line.
    """
    if not isinstance(payload, dict):
        return {}
    args = payload.get("args")
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip():
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def feed_event(state: ProgressState, kind: str, payload: Any) -> bool:
    """Fold one ``astream_events`` tuple into *state*.

    Returns ``True`` when the visible state changed and the view should be
    re-rendered.
    """
    if kind == "tool_call":
        if not isinstance(payload, dict):
            return False
        name = str(payload.get("name") or "tool")
        args = _parse_args(payload)
        call_id = str(payload.get("id") or f"call-{len(state.steps)}")

        if normalize_tool_name(name) in TODO_TOOL_NAMES or name in TODO_TOOL_NAMES:
            todos = parse_todos(args)
            if not todos:
                return False
            # Each call replaces the list wholesale (deepagents semantics).
            state.todos = todos
            state.revision += 1
            return True

        step = ProgressStep(call_id=call_id, name=name, args=args)
        state.steps.append(step)
        state._by_id[call_id] = step
        state.revision += 1
        return True

    if kind == "tool_result":
        if not isinstance(payload, dict):
            return False
        call_id = str(payload.get("id") or "")
        pending = state._by_id.get(call_id)
        if pending is None:
            # A result with no matching call (todo calls, replayed history).
            return False
        content = str(payload.get("content") or "")
        pending.result = content
        pending.finished = True
        pending.failed = _looks_like_error(content)
        state.revision += 1
        return True

    if kind == "subagent_text":
        # Raw peer/subagent text is never spliced in (concurrent agents
        # streaming into one chat is unreadable) — but the *first* chunk from
        # each peer is a useful liveness signal, so it bumps the revision to
        # refresh the "Delegating to …" line's elapsed clock.
        agents: list[str] = []
        if isinstance(payload, dict):
            raw = payload.get("agents")
            if isinstance(raw, list | tuple):
                agents = [str(a).strip() for a in raw if str(a).strip()]
        key = ",".join(sorted(agents)) if agents else "subagent"
        if key in state._peers:
            return False
        state._peers.add(key)
        state.revision += 1
        return True

    return False


# ──────────────────────────────── rendering ─────────────────────────────────


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _render_todos(todos: list[TodoItem], d: MarkupDialect) -> list[str]:
    if not todos:
        return []
    done = sum(1 for t in todos if t.status == "completed")
    lines = [d.b(f"📋 Plan — {done}/{len(todos)}")]
    for todo in todos[:MAX_TODO_ITEMS]:
        text = _truncate(todo.content, MAX_TODO_CHARS)
        if todo.status == "completed":
            lines.append(f"✅ {text}")
        elif todo.status == "in_progress":
            # The active step is the one thing a waiting user most wants to
            # see, so it gets the only emphasis in the block.
            lines.append(f"⏳ {d.b(text)}")
        else:
            lines.append(f"⬜ {text}")
    if len(todos) > MAX_TODO_ITEMS:
        lines.append(d.i(f"… +{len(todos) - MAX_TODO_ITEMS} more"))
    return lines


def _step_line(step: ProgressStep, d: MarkupDialect, *, verbose: bool) -> str:
    desc = describe_tool(step.name, step.args)
    limit = MAX_VERBOSE_ARG_CHARS if verbose else MAX_NORMAL_ARG_CHARS
    marker = "•" if not step.finished else ("⚠️" if step.failed else "✓")
    head = f"{marker} {desc.icon} {desc.verb}"
    if desc.detail:
        head += f" {d.c(_truncate(desc.detail, limit))}"
    return head


def _verbose_extras(step: ProgressStep, d: MarkupDialect) -> list[str]:
    """Full inputs + returned output, for ``verbose`` only."""
    lines: list[str] = []
    shown = {k: v for k, v in step.args.items() if not str(k).startswith("_")}
    if shown:
        try:
            rendered = json.dumps(shown, ensure_ascii=False, default=str)
        except Exception:
            rendered = str(shown)
        lines.append(d.c(_truncate(rendered, MAX_VERBOSE_ARG_CHARS)))
    if step.result:
        lines.append(f"↳ {d.c(_truncate(step.result, MAX_VERBOSE_RESULT_CHARS))}")
    return lines


def _assemble(
    state: ProgressState,
    todos: list[TodoItem],
    d: MarkupDialect,
    *,
    verbose: bool,
    settled: bool,
    header: str,
    step_limit: int,
) -> str:
    blocks: list[str] = []
    todo_lines = _render_todos(todos, d)
    if todo_lines:
        blocks.append("\n".join(todo_lines))

    if state.steps:
        visible = state.steps[-step_limit:]
        hidden = len(state.steps) - len(visible)
        lines: list[str] = []
        if hidden > 0:
            lines.append(d.i(f"… {hidden} earlier step(s)"))
        for step in visible:
            lines.append(_step_line(step, d, verbose=verbose))
            if verbose:
                lines.extend(f"   {extra}" for extra in _verbose_extras(step, d))
        blocks.append("\n".join(lines))

    if not blocks:
        return ""

    if not settled:
        pending = sum(1 for s in state.steps if not s.finished)
        suffix = f" · {state.done_count}/{len(state.steps)} steps" if state.steps else ""
        if pending:
            suffix += " ⏳"
        blocks.insert(0, d.b(f"🧭 {header}{suffix}"))

    return "\n\n".join(b for b in blocks if b).strip()


def render_progress(
    state: ProgressState,
    verbosity: Any = DEFAULT_VERBOSITY,
    dialect: MarkupDialect | str | None = None,
    *,
    header: str = "Working…",
    settled: bool = False,
) -> str:
    """Render *state* as the live progress message.

    *settled* renders the turn-end view: no spinner-ish header, and dangling
    ``in_progress`` todos are shown as completed (see :func:`settle_todos`).
    Returns ``""`` for ``quiet`` or an empty state — the caller treats that as
    "nothing to show" and leaves the chat alone.
    """
    mode = normalize_verbosity(verbosity) or DEFAULT_VERBOSITY
    if mode == "quiet" or not state.has_content:
        return ""
    d = _resolve_dialect(dialect)
    verbose = mode == "verbose"
    todos = settle_todos(state.todos) if settled else state.todos

    limit = MAX_VERBOSE_STEPS if verbose else MAX_NORMAL_STEPS
    text = _assemble(
        state, todos, d, verbose=verbose, settled=settled, header=header, step_limit=limit
    )
    # Over budget: roll the *oldest* steps off (the "… N earlier step(s)"
    # line absorbs them) — end-truncation would cut the newest lines, which
    # are exactly what a waiting user is watching for.
    while len(text) > MAX_PROGRESS_CHARS and limit > _MIN_VISIBLE_STEPS:
        limit = max(_MIN_VISIBLE_STEPS, limit // 2)
        text = _assemble(
            state, todos, d, verbose=verbose, settled=settled, header=header, step_limit=limit
        )
    if len(text) > MAX_PROGRESS_CHARS:
        text = text[: MAX_PROGRESS_CHARS - 1].rstrip() + "…"
    return text
