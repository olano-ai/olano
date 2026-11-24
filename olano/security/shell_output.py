"""Helpers for compacting large shell outputs into workspace artifacts.

A command's full stdout and stderr are always written to a log file under the
workspace (``<workspace>/.olano/artifacts/shell/<date>/<time>/``) when the output
is too large to inline, and the tool result names that path. What the model
sees inline is a *preview*, and for a **failing** command the preview is chosen
for diagnosis rather than by position: a plain head/tail cut once dropped the
only ``AttributeError`` line of a 41-line unittest report into its
"[... 13 lines omitted ...]" gap while spending 450 characters of the budget on
the progress dots, and the agent re-ran the same suite three times without
reading the log. See :func:`_diagnostic_preview`.

Four rules keep the result from costing the agent extra tool calls, each seen
on one managed box where a two-field change took 23 calls:

- **Ordinary output stays inline.** The limits were 1,200 characters and 24
  lines, so a 31-line ``--help`` and a 26-line JSON record went to a log while
  the preview dropped the three lines that mattered; the agent asked again.
- **The log path is the host path** the model sees everywhere else, never the
  container alias ``/workspace/...``: an agent that saw the alias "corrected"
  it to a host path that did not exist and had the read refused.
- **"Read the log" is said only when the preview lost something.** A preview
  that holds the whole output says so instead, or the agent reads a log for
  nothing.
- **A shortened search result says how to get the exact text.** ``rg``/``grep``
  lines are what the next ``edit_file`` copies from, and a line clipped to 240
  characters gave the agent a guessed ``old_string`` and a failed edit.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from olano.config.workspace import WORKSPACE_STATE_DIRNAME

# Output at or under both limits is returned verbatim. About 2k tokens: well
# under deepagents' own large-result eviction (~20k tokens), and enough for a
# CLI's --help, a JSON record or a short test report.
INLINE_MAX_CHARS = 8_000
INLINE_MAX_LINES = 200
PREVIEW_HEAD_LINES = 8
PREVIEW_TAIL_LINES = 20
PREVIEW_MAX_CHARS = 1_600
SUMMARY_MAX_CHARS = 3_200
# A failing command gets a larger inline budget: the diagnosis is what the next
# step depends on, and the alternative is another run of the same command.
FAILURE_PREVIEW_MAX_CHARS = 4_000
FAILURE_SUMMARY_MAX_CHARS = 6_000
# One line never eats the budget (a unittest progress line of 450 dots, a
# minified bundle). The full line is in the log.
PREVIEW_LINE_MAX_CHARS = 240
# Context a diagnostic preview always keeps: how the run started and how it
# ended (the "Ran 345 tests" / "FAILED (errors=4)" summary lives in the tail).
DIAGNOSTIC_HEAD_LINES = 3
DIAGNOSTIC_TAIL_LINES = 6

SUCCESS_HINT = "Use read_file on that path if you need the full output."
FAILURE_HINT = (
    "The preview below is partial. Read the full log with read_file before "
    "re-running the same command."
)
# The preview holds the whole output (only surrounding whitespace dropped).
COMPLETE_HINT = "The output below is complete; the log is only a copy, no need to read it."
# Appended when a search command's preview lost text: the lines are what the
# next edit_file copies its old_string from.
SEARCH_HINT = (
    "Search lines in this preview are shortened or omitted, so do not copy an "
    'edit_file old_string from them: use the grep tool with output_mode="content" '
    "(it returns whole lines) or read_file at the line number first."
)

_SHELL_ARTIFACT_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]+")

# Programs whose output lines are `path:line:text` matches.
_SEARCH_PROGRAMS = frozenset({"rg", "grep", "egrep", "fgrep", "ag", "ack", "ack-grep"})
# Shell operators that start a new simple command.
_COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")"})
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_COMMAND_PREFIXES = frozenset({"sudo", "env", "time", "nice", "nohup", "command", "exec"})

# Lines worth keeping from a failing command's output, across the common
# toolchains: Python tracebacks (and pytest's ``E   `` lines), JS/Java stack
# frames, compiler and linter diagnostics, shell and package-manager refusals.
_DIAGNOSTIC_LINE_RE = re.compile(
    r"""
    ^\s*Traceback\b
    | ^\s*File\ "            # Python frame
    | ^\s*at\ \S+            # JS / Java frame
    | ^E\s                   # pytest assertion detail
    | ^\s*(?:ERROR|FAIL|FAILED|FATAL|PANIC|CRITICAL)\b
    | \bERR\b                # npm ERR!
    | \b(?:[A-Za-z]+Error|[A-Za-z]+Exception|[A-Za-z]+Warning)\b
    | \b(?:error|errors|failed|failure|fatal|panic|exception|traceback|assert(?:ion)?)\b
    | \b(?:not\ found|no\ such\ file|permission\ denied|command\ not\ found)\b
    | \b(?:cannot|can't|unable\ to|refused|rejected|invalid|unexpected|missing)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
# A Python frame line is followed by the source line (and, on 3.11+, a caret
# line); the source line is the one that names the call that failed.
_FRAME_LINE_RE = re.compile(r'^\s*File ".*", line \d+')


@dataclass(frozen=True)
class ShellOutputSummary:
    content: str
    artifact: dict[str, str] | None
    large_output: bool


def _shell_line_count(text: str) -> int:
    return len(text.splitlines()) if text else 0


def shell_output_is_large(stdout: str, stderr: str) -> bool:
    total_text = f"{stdout}{stderr}"
    total_lines = _shell_line_count(stdout) + _shell_line_count(stderr)
    return len(total_text) > INLINE_MAX_CHARS or total_lines > INLINE_MAX_LINES


def _truncate_preview_text(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _clip_line(line: str) -> str:
    if len(line) <= PREVIEW_LINE_MAX_CHARS:
        return line
    return line[: PREVIEW_LINE_MAX_CHARS - 1].rstrip() + "…"


def _render_selected(lines: list[str], keep: list[int]) -> str:
    """Render the kept line indexes in order, naming every gap between them."""
    parts: list[str] = []
    previous = -1
    for idx in keep:
        gap = idx - previous - 1
        if gap > 0:
            parts.append(f"[... {gap} lines omitted ...]")
        parts.append(_clip_line(lines[idx]))
        previous = idx
    trailing = len(lines) - previous - 1
    if trailing > 0:
        parts.append(f"[... {trailing} lines omitted ...]")
    return "\n".join(parts)


def _fit_selection(lines: list[str], keep: list[int], *, max_chars: int) -> str:
    """Render ``keep``; while over budget, drop selected lines from the middle.

    The first and last selected lines are the context (how the run started and
    ended) and the earliest diagnostics are usually the root cause, so the
    middle goes first.
    """
    keep = sorted(set(keep))
    rendered = _render_selected(lines, keep)
    while len(rendered) > max_chars and len(keep) > 2:
        del keep[len(keep) // 2]
        rendered = _render_selected(lines, keep)
    return _truncate_preview_text(rendered, limit=max_chars)


def _diagnostic_indexes(lines: list[str]) -> list[int]:
    keep: set[int] = set()
    for idx, line in enumerate(lines):
        if not _DIAGNOSTIC_LINE_RE.search(line):
            continue
        keep.add(idx)
        if _FRAME_LINE_RE.match(line):
            # The source line under the frame, and its caret line when present.
            if idx + 1 < len(lines) and lines[idx + 1].strip():
                keep.add(idx + 1)
            if idx + 2 < len(lines) and _is_caret_line(lines[idx + 2]):
                keep.add(idx + 2)
    return sorted(keep)


def _is_caret_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and set(stripped) <= {"^", "~"}


def _diagnostic_preview(stripped: str, *, max_chars: int) -> str | None:
    """A preview that keeps the lines a failure is diagnosed from.

    Every line matching :data:`_DIAGNOSTIC_LINE_RE` (plus the source line under
    a Python frame), with the first and last few lines for context, gaps named,
    long lines clipped, the middle dropped first when over budget. ``None``
    when nothing in the output looks diagnostic, so the caller falls back to the
    positional head/tail cut.
    """
    lines = stripped.splitlines()
    diagnostic = _diagnostic_indexes(lines)
    if not diagnostic:
        return None
    keep = set(diagnostic)
    keep.update(range(min(DIAGNOSTIC_HEAD_LINES, len(lines))))
    keep.update(range(max(0, len(lines) - DIAGNOSTIC_TAIL_LINES), len(lines)))
    return _fit_selection(lines, sorted(keep), max_chars=max_chars)


def _shell_preview(
    label: str,
    text: str,
    *,
    diagnostic: bool = False,
    max_chars: int = PREVIEW_MAX_CHARS,
) -> tuple[str, bool]:
    """The preview block for one stream, and whether it holds the whole stream.

    ``True`` only when the stream is shown verbatim (surrounding whitespace
    aside); any selection, clipped line or budget cut makes it ``False``.
    """
    stripped = text.strip()
    if not stripped:
        return "", True

    lines = stripped.splitlines()
    if len(lines) <= (PREVIEW_HEAD_LINES + PREVIEW_TAIL_LINES + 1) and len(stripped) <= max_chars:
        return f"{label} preview:\n{stripped}", True

    chosen: str | None = None
    if diagnostic:
        chosen = _diagnostic_preview(stripped, max_chars=max_chars)
    if chosen is None:
        head = list(range(min(PREVIEW_HEAD_LINES, len(lines))))
        tail = list(range(max(0, len(lines) - PREVIEW_TAIL_LINES), len(lines)))
        chosen = _fit_selection(lines, head + tail, max_chars=max_chars)
    return f"{label} preview:\n{chosen}", False


def _shell_preview_block(
    label: str,
    text: str,
    *,
    diagnostic: bool = False,
    max_chars: int = PREVIEW_MAX_CHARS,
) -> str:
    return _shell_preview(label, text, diagnostic=diagnostic, max_chars=max_chars)[0]


def is_search_command(command: str) -> bool:
    """True when any simple command in ``command`` runs a text search.

    ``rg``/``grep``/``ag``/``ack`` and ``git grep``, also behind env
    assignments, ``sudo``/``env``/``timeout``-style prefixes, a path
    (``/usr/bin/rg``) or a pipe. Unparseable shell is not a search.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False

    at_start = True
    after_prefix = False
    for idx, token in enumerate(tokens):
        if token in _COMMAND_SEPARATORS:
            at_start, after_prefix = True, False
            continue
        if not at_start:
            continue
        if _ENV_ASSIGNMENT_RE.match(token):
            continue
        if after_prefix and (token.startswith("-") or re.fullmatch(r"\d+(\.\d+)?[smhd]?", token)):
            continue
        program = token.rsplit("/", 1)[-1]
        if program in _COMMAND_PREFIXES or program == "timeout":
            after_prefix = True
            continue
        if program in _SEARCH_PROGRAMS:
            return True
        if program == "git" and "grep" in tokens[idx + 1 : idx + 4]:
            return True
        at_start, after_prefix = False, False
    return False


def _write_shell_artifact(
    *,
    tool_name: str,
    tool_call_id: str,
    workspace_root: Path,
    command: str,
    runtime_command: str,
    timeout: int,
    stdout: str,
    stderr: str,
    returncode: int,
) -> tuple[Path, str]:
    created_at = datetime.now(timezone.utc)
    artifact_dir = (
        workspace_root
        / WORKSPACE_STATE_DIRNAME
        / "artifacts"
        / "shell"
        / created_at.strftime("%Y-%m-%d")
        / created_at.strftime("%H-%M-%S")
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    slug = _SHELL_ARTIFACT_SAFE_RE.sub(
        "-",
        f"{tool_name}-{tool_call_id or 'shell'}",
    ).strip("-.")[:20]
    if not slug:
        slug = "shell"
    artifact_path = artifact_dir / f"{slug}.log"
    counter = 2
    while artifact_path.exists():
        artifact_path = artifact_dir / f"{slug}-{counter}.log"
        counter += 1
    artifact_body = [
        "# Olano shell execution log",
        f"created_at: {created_at.isoformat()}",
        f"tool_name: {tool_name}",
        f"tool_call_id: {tool_call_id}",
        f"returncode: {returncode}",
        f"timeout_seconds: {timeout}",
        f"command: {command}",
        f"runtime_command: {runtime_command}",
        "",
        "[stdout]",
        stdout if stdout else "(empty)",
        "",
        "[stderr]",
        stderr if stderr else "(empty)",
        "",
    ]
    artifact_path.write_text("\n".join(artifact_body), encoding="utf-8")
    # The host path, like every other path the model sees: the sandbox mounts
    # the workspace at its host path, and `/workspace` is only an alias there.
    return artifact_path, str(artifact_path.absolute())


def summarize_shell_output(
    *,
    tool_name: str,
    tool_call_id: str,
    workspace_root: Path,
    command: str,
    runtime_command: str,
    timeout: int,
    stdout: str,
    stderr: str,
    returncode: int,
    include_previews: bool = True,
    hint: str | None = None,
) -> ShellOutputSummary:
    combined = stdout
    if stderr:
        combined = f"{combined}\n[stderr]\n{stderr}" if combined else stderr
    if returncode != 0 and not combined:
        combined = f"Command failed with exit code {returncode}"

    if not shell_output_is_large(stdout, stderr):
        return ShellOutputSummary(
            content=combined or "(no output)", artifact=None, large_output=False
        )

    artifact_path, artifact_ref = _write_shell_artifact(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        workspace_root=workspace_root,
        command=command,
        runtime_command=runtime_command,
        timeout=timeout,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
    )
    failed = returncode != 0
    preview_max = FAILURE_PREVIEW_MAX_CHARS if failed else PREVIEW_MAX_CHARS
    summary_max = FAILURE_SUMMARY_MAX_CHARS if failed else SUMMARY_MAX_CHARS

    previews: list[str] = []
    complete = False
    if include_previews:
        stdout_preview, stdout_complete = _shell_preview(
            "stdout", stdout, diagnostic=failed, max_chars=preview_max
        )
        stderr_preview, stderr_complete = _shell_preview(
            "stderr", stderr, diagnostic=failed, max_chars=preview_max
        )
        previews = [p for p in (stdout_preview, stderr_preview) if p]
        complete = stdout_complete and stderr_complete
        if not previews and combined:
            preview, complete = _shell_preview(
                "output", combined, diagnostic=failed, max_chars=preview_max
            )
            previews = [preview] if preview else []

    header = [
        (
            f"Command failed with exit code {returncode}."
            if failed
            else "Command completed with large output."
        ),
        f"Full log: {artifact_ref}",
    ]
    counts = [
        f"stdout: {_shell_line_count(stdout)} lines, {len(stdout)} chars",
        f"stderr: {_shell_line_count(stderr)} lines, {len(stderr)} chars",
    ]
    body: list[str] = []
    for preview in previews:
        body.extend(["", preview])

    def _render(hints: list[str]) -> str:
        return "\n".join(header + hints + counts + body).strip()

    summary = ""
    if hint is not None:
        summary = _render([hint])
    elif complete:
        summary = _render([COMPLETE_HINT])
    if not summary or len(summary) > summary_max:
        # Something was left out (or the overall cap below is about to cut the
        # preview): point at the log, and at whole lines for a search.
        hints = [hint] if hint is not None else [FAILURE_HINT if failed else SUCCESS_HINT]
        if hint is None and is_search_command(command):
            hints.append(SEARCH_HINT)
        summary = _render(hints)
    summary = _truncate_preview_text(summary, limit=summary_max)
    return ShellOutputSummary(
        content=summary,
        artifact={"log_path": str(artifact_path), "workspace_log_path": artifact_ref},
        large_output=True,
    )
