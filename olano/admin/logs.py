"""Read-only log & session inspection.

Bounded reads of the home log files and per-agent JSONL session transcripts so
AgentFather can diagnose problems without shelling out.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from olano.config.home import get_olano_paths

_MAX_LINES = 2000
_ERROR_NEEDLES = ("ERROR", "Error", "Traceback", "CRITICAL", "WARNING")


def _tail(path: Path, lines: int) -> list[str]:
    if not path.exists():
        return []
    lines = max(1, min(int(lines), _MAX_LINES))
    # Small log files: a full read + slice is simplest and correct.
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return text[-lines:]


def tail_log(lines: int = 100, *, which: str = "main") -> dict[str, Any]:
    """Return the last ``lines`` of a home log file.

    ``which`` is ``main`` (``logs/olano.log``) or ``runtime``
    (``logs/runtime.log``).
    """
    paths = get_olano_paths()
    path = paths.runtime_log_path if which == "runtime" else paths.logs_dir / "olano.log"
    return {"which": which, "path": str(path), "lines": _tail(path, lines)}


def recent_errors(lines: int = 50, *, which: str = "main") -> dict[str, Any]:
    """Return recent log lines that look like errors/warnings."""
    result = tail_log(_MAX_LINES, which=which)
    hits = [ln for ln in result["lines"] if any(n in ln for n in _ERROR_NEEDLES)]
    lines = max(1, min(int(lines), _MAX_LINES))
    return {"which": which, "path": result["path"], "errors": hits[-lines:]}


def agent_errors(agent: str, lines: int = 50) -> dict[str, Any]:
    """Return recent log lines (main + runtime) that mention ``agent``.

    Error/warning lines are preferred; if none mention the agent, any matching
    line is returned so the caller still gets context. Useful for narrowing a
    noisy shared log down to one agent when diagnosing a boot or runtime fault.
    """
    pattern = re.compile(rf"\b{re.escape(agent)}\b")
    matched: list[dict[str, str]] = []
    for which in ("main", "runtime"):
        for ln in tail_log(_MAX_LINES, which=which)["lines"]:
            if pattern.search(ln):
                matched.append({"which": which, "line": ln})
    errs = [m for m in matched if any(n in m["line"] for n in _ERROR_NEEDLES)]
    chosen = errs or matched
    lines = max(1, min(int(lines), _MAX_LINES))
    return {"agent": agent, "matched": len(matched), "errors": chosen[-lines:]}


def list_sessions(agent: str) -> list[dict[str, Any]]:
    """List the JSONL session transcripts recorded for ``agent``."""
    sessions_dir = get_olano_paths().sessions_dir / agent
    if not sessions_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(sessions_dir.glob("*.jsonl")):
        out.append({"thread_id": p.stem, "path": str(p), "size_bytes": p.stat().st_size})
    return out


def read_session(agent: str, thread_id: str, *, lines: int = 100) -> dict[str, Any]:
    """Return the last ``lines`` parsed records of a JSONL session transcript."""
    path = get_olano_paths().sessions_dir / agent / f"{thread_id}.jsonl"
    raw = _tail(path, lines)
    records: list[Any] = []
    for ln in raw:
        try:
            records.append(json.loads(ln))
        except Exception:
            records.append({"raw": ln})
    return {"agent": agent, "thread_id": thread_id, "path": str(path), "records": records}
