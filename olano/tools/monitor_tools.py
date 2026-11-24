"""Generic monitoring-state tools for scheduled watchdog agents.

A *watchdog* agent (price monitors, job-board scrapers, public-tender radars,
release trackers, …) runs on a schedule and must stay **quiet unless something
new appears**. The hard part is not fetching — it is remembering what was
already surfaced across runs and process restarts, so a twice-a-day job only
reports genuinely new items.

This toolkit is that memory. It is deliberately **domain-agnostic**: every
operation is keyed by a free-form ``namespace`` (e.g. ``"sicop_servicios"``)
so one agent can run several independent monitors, and any agent can reuse it.

State lives as one JSON file per namespace under the running agent's workspace:

    <workspace>/memories/monitor_state/<namespace>.json

    {
      "namespace": "sicop_servicios",
      "seen":     {"<id>": {"first_seen": "<iso>", "meta": {...}}},
      "runs":     [{"ts": "<iso>", "found": N, "new": M, "note": "..."}],
      "feedback": [{"ts": "<iso>", "id": "...", "label": "irrelevant",
                    "reason": "..."}]
    }

Writes are atomic (temp file + ``Path.replace``) so an interrupted run can never
corrupt the seen-set. The file is human-readable on purpose — it doubles as the
audit trail the operator can inspect directly.

Typical loop each run:

    1. ``monitor_seen_check(ns, ids)``      → which of these are NEW?
    2. …process only the new ones…
    3. ``monitor_seen_add(ns, new_ids)``    → remember them so next run is quiet
    4. ``monitor_run_log(ns, found, new)``  → append an audit record

``monitor_feedback_add`` lets the operator teach the agent ("this one was
irrelevant"); ``monitor_feedback_list`` surfaces those patterns back into the
agent's relevance judgement on later runs.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import tool

from olano.config.workspace import get_workspace
from olano.memory.markdown_memory import atomic_write_text
from olano.security.request_context import get_request_context

logger = logging.getLogger(__name__)

# Keep run/feedback history bounded so the state file cannot grow without limit.
_MAX_RUNS = 200
_MAX_FEEDBACK = 500
_NS_RE = re.compile(r"[^a-z0-9_-]+")

# A monitor-state write can still lose a race against another writer (a
# scheduled run and a live chat both touching the same namespace at once).
# Retry a few times before giving up so a transient collision self-heals.
_SAVE_RETRIES = 3
_SAVE_BACKOFF = 0.02  # seconds, multiplied by (attempt + 1)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _workspace_root() -> Path:
    """Resolve the *running* agent's workspace root from the request context.

    Mirrors ``browser._workspace_root``: tools execute inside a request context
    that carries the agent's real workspace root, so a bare ``get_workspace()``
    would resolve the legacy/global workspace instead of the per-agent one.
    """
    workspace_root = get_request_context().metadata.get("workspace_root")
    return get_workspace(str(workspace_root) if workspace_root else None).root


def _slug(namespace: str) -> str:
    slug = _NS_RE.sub("-", namespace.strip().lower()).strip("-_")
    return slug or "default"


def _state_path(namespace: str) -> Path:
    d = _workspace_root() / "memories" / "monitor_state"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_slug(namespace)}.json"


def _load(namespace: str) -> dict[str, Any]:
    path = _state_path(namespace)
    if not path.exists():
        return {"namespace": namespace, "seen": {}, "runs": [], "feedback": []}
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Never let a corrupt file wedge a run; start fresh but keep the bad
        # copy aside for forensics.
        try:
            path.rename(path.with_suffix(".json.corrupt"))
        except OSError:
            pass
        return {"namespace": namespace, "seen": {}, "runs": [], "feedback": []}
    data.setdefault("namespace", namespace)
    data.setdefault("seen", {})
    data.setdefault("runs", [])
    data.setdefault("feedback", [])
    return data


def _save(namespace: str, data: dict[str, Any]) -> None:
    """Atomically persist state to ``<ns>.json``.

    Uses a **unique** temp file per write (via ``atomic_write_text``) instead of
    a shared ``<ns>.json.tmp``. A fixed temp name races when two writers hit the
    same namespace at once — e.g. a scheduled run and a live Telegram chat both
    driving the same monitor: both stage into the same temp, the first
    ``replace`` renames it away, and the second blows up with
    ``FileNotFoundError: '<ns>.json.tmp' -> '<ns>.json'``. A unique temp per
    write removes that collision (``atomic_write_text`` also fsyncs and cleans
    up its temp on failure).
    """
    path = _state_path(namespace)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    atomic_write_text(path, payload)


def _persist(namespace: str, data: dict[str, Any]) -> str | None:
    """Persist state, retrying transient filesystem errors.

    Returns ``None`` on success, or a short operator-facing message on
    persistent failure. Callers surface that message instead of letting the
    exception escape: an unhandled tool error aborts the whole agent turn (the
    user sees ``AgentError`` in chat), whereas a returned string lets the agent
    report the hiccup and carry on — the state is retried on the next run.
    """
    last_exc: Exception | None = None
    for attempt in range(_SAVE_RETRIES):
        try:
            _save(namespace, data)
            return None
        except OSError as exc:
            last_exc = exc
            if attempt + 1 < _SAVE_RETRIES:
                time.sleep(_SAVE_BACKOFF * (attempt + 1))
    logger.warning(
        "monitor_state save failed for %r after %d attempts: %s",
        namespace,
        _SAVE_RETRIES,
        last_exc,
    )
    return (
        f"⚠️ Could not persist monitor state for '{namespace}' "
        f"({type(last_exc).__name__}: {last_exc}). The result above stands for "
        "now but may not have been saved — it will be retried on the next run."
    )


def _clean_ids(ids: list[str]) -> list[str]:
    """Normalize an id list: stringify, strip, drop blanks, de-dupe (order-stable)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in ids:
        key = str(raw).strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


@tool
def monitor_seen_check(
    namespace: Annotated[
        str, "Monitor namespace, e.g. 'sicop_servicios'. Groups one monitor's state."
    ],
    ids: Annotated[list[str], "Candidate item ids observed this run (e.g. tender numbers)."],
) -> str:
    """Split a batch of observed item ids into NEW vs ALREADY-SEEN.

    Read-only — it does NOT record anything. Call this first each run to decide
    which items deserve processing, then call ``monitor_seen_add`` for the ones
    you actually surface. Returns the new ids, the count already seen, and the
    total tracked, so a run with nothing new is obvious.
    """
    data = _load(namespace)
    seen_set = data["seen"]
    candidates = _clean_ids(ids)
    new = [i for i in candidates if i not in seen_set]
    already = [i for i in candidates if i in seen_set]
    lines = [
        f"Namespace '{namespace}': {len(new)} new, {len(already)} already seen "
        f"(of {len(candidates)} checked; {len(seen_set)} tracked total).",
    ]
    if new:
        lines.append("NEW ids:")
        lines.extend(f"  - {i}" for i in new)
    else:
        lines.append("No new ids in this batch.")
    return "\n".join(lines)


@tool
def monitor_seen_add(
    namespace: Annotated[str, "Monitor namespace."],
    ids: Annotated[list[str], "Item ids to mark as seen so future runs skip them."],
    note: Annotated[str, "Optional short label stored alongside these ids (e.g. run date)."] = "",
) -> str:
    """Record item ids as seen so subsequent runs treat them as known.

    Idempotent: re-adding an existing id keeps its original ``first_seen`` time.
    Call this only for items you have finished processing/surfacing, so a crash
    mid-run doesn't hide items you never actually reported.
    """
    data = _load(namespace)
    seen_set = data["seen"]
    added = 0
    ts = _now_iso()
    for i in _clean_ids(ids):
        if i not in seen_set:
            entry: dict[str, Any] = {"first_seen": ts}
            if note.strip():
                entry["note"] = note.strip()
            seen_set[i] = entry
            added += 1
    err = _persist(namespace, data)
    if err:
        return err
    return (
        f"Namespace '{namespace}': marked {added} new id(s) as seen "
        f"({len(seen_set)} tracked total)."
    )


@tool
def monitor_seen_list(
    namespace: Annotated[str, "Monitor namespace."],
    limit: Annotated[int, "Max ids to return, most recently seen first."] = 50,
) -> str:
    """List tracked (already-seen) ids for a namespace, newest first — for audit."""
    data = _load(namespace)
    seen_set: dict[str, Any] = data["seen"]
    if not seen_set:
        return f"Namespace '{namespace}': nothing tracked yet."
    items = sorted(
        seen_set.items(),
        key=lambda kv: kv[1].get("first_seen", ""),
        reverse=True,
    )
    limit = max(1, min(limit, 500))
    lines = [f"Namespace '{namespace}': {len(seen_set)} tracked (showing up to {limit})."]
    for i, meta in items[:limit]:
        first = meta.get("first_seen", "?")
        note = f" — {meta['note']}" if meta.get("note") else ""
        lines.append(f"  - {i}  (first seen {first}){note}")
    return "\n".join(lines)


@tool
def monitor_run_log(
    namespace: Annotated[str, "Monitor namespace."],
    found: Annotated[int, "How many items the run pulled in total."],
    new: Annotated[int, "How many were new (surfaced)."],
    note: Annotated[str, "Short status note, e.g. 'delivered' / 'no-op' / 'portal changed'."] = "",
) -> str:
    """Append an audit record of a completed run (found / new / status).

    Builds the historical log the operator uses to confirm runs happened even
    when there was nothing to report. Keeps the most recent runs only.
    """
    data = _load(namespace)
    runs: list[dict[str, Any]] = data["runs"]
    runs.append({"ts": _now_iso(), "found": int(found), "new": int(new), "note": note.strip()})
    del runs[:-_MAX_RUNS]
    err = _persist(namespace, data)
    if err:
        return err
    return (
        f"Logged run for '{namespace}': found={found}, new={new}"
        + (f", note={note.strip()!r}" if note.strip() else "")
        + f". ({len(runs)} run(s) in history.)"
    )


@tool
def monitor_run_history(
    namespace: Annotated[str, "Monitor namespace."],
    limit: Annotated[int, "Max recent runs to show, newest first."] = 20,
) -> str:
    """Show the recent run history (found / new / status) for a namespace."""
    data = _load(namespace)
    runs: list[dict[str, Any]] = data["runs"]
    if not runs:
        return f"Namespace '{namespace}': no runs logged yet."
    limit = max(1, min(limit, _MAX_RUNS))
    lines = [f"Namespace '{namespace}': {len(runs)} run(s) logged (newest first)."]
    for r in reversed(runs[-limit:]):
        note = f" — {r['note']}" if r.get("note") else ""
        lines.append(
            f"  - {r.get('ts', '?')}: found={r.get('found', 0)}, new={r.get('new', 0)}{note}"
        )
    return "\n".join(lines)


@tool
def monitor_feedback_add(
    namespace: Annotated[str, "Monitor namespace."],
    item_id: Annotated[str, "The item the operator is giving feedback on (e.g. tender number)."],
    label: Annotated[str, "Verdict, e.g. 'irrelevant', 'relevant', 'duplicate', 'spam'."],
    reason: Annotated[
        str, "One line on WHY, so the pattern can be reused in future filtering."
    ] = "",
) -> str:
    """Record operator feedback on a surfaced item so the agent can learn.

    Use this when the operator says a surfaced item was wrong (or right). On
    later runs, read it back with ``monitor_feedback_list`` and fold the
    patterns into your relevance judgement to cut similar noise.
    """
    data = _load(namespace)
    fb: list[dict[str, Any]] = data["feedback"]
    fb.append(
        {
            "ts": _now_iso(),
            "id": str(item_id).strip(),
            "label": label.strip().lower(),
            "reason": reason.strip(),
        }
    )
    del fb[:-_MAX_FEEDBACK]
    err = _persist(namespace, data)
    if err:
        return err
    return (
        f"Recorded feedback for '{namespace}' on {item_id!r}: "
        f"{label.strip().lower()}"
        + (f" ({reason.strip()})" if reason.strip() else "")
        + f". ({len(fb)} feedback item(s) stored.)"
    )


@tool
def monitor_feedback_list(
    namespace: Annotated[str, "Monitor namespace."],
    label: Annotated[str, "Optional filter, e.g. 'irrelevant'. Empty = all feedback."] = "",
    limit: Annotated[int, "Max feedback entries to show, newest first."] = 50,
) -> str:
    """List stored operator feedback for a namespace (optionally filtered by label).

    Read this at the start of a run to bias your relevance filter with what the
    operator has already accepted or rejected.
    """
    data = _load(namespace)
    fb: list[dict[str, Any]] = data["feedback"]
    want = label.strip().lower()
    if want:
        fb = [f for f in fb if f.get("label") == want]
    if not fb:
        scope = f" with label '{want}'" if want else ""
        return f"Namespace '{namespace}': no feedback{scope} yet."
    limit = max(1, min(limit, _MAX_FEEDBACK))
    lines = [f"Namespace '{namespace}': {len(fb)} feedback item(s) (newest first)."]
    for f in reversed(fb[-limit:]):
        reason = f" — {f['reason']}" if f.get("reason") else ""
        lines.append(
            f"  - [{f.get('label', '?')}] {f.get('id', '?')}{reason}  ({f.get('ts', '?')})"
        )
    return "\n".join(lines)


@tool
def monitor_state_summary(
    namespace: Annotated[str, "Monitor namespace."],
) -> str:
    """One-glance summary of a monitor: tracked count, last run, feedback totals."""
    data = _load(namespace)
    seen_n = len(data["seen"])
    runs: list[dict[str, Any]] = data["runs"]
    fb: list[dict[str, Any]] = data["feedback"]
    last = runs[-1] if runs else None
    last_str = (
        f"{last.get('ts', '?')} (found={last.get('found', 0)}, new={last.get('new', 0)}"
        + (f", {last['note']}" if last.get("note") else "")
        + ")"
        if last
        else "never"
    )
    irrelevant = sum(1 for f in fb if f.get("label") == "irrelevant")
    return (
        f"Monitor '{namespace}':\n"
        f"  tracked ids: {seen_n}\n"
        f"  runs logged: {len(runs)} — last: {last_str}\n"
        f"  feedback: {len(fb)} total ({irrelevant} marked irrelevant)"
    )


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
