"""Human-in-the-loop approval flows and remembered approval rules.

Pending requests live in memory because each one is an ``asyncio`` future an
agent turn is blocked on: a restart ends the turn, so there is nothing to
resume. What must outlive the process is the **record**: every request and
its outcome (who decided, through which surface, when, with what note) is
appended to ``<home>/audit/approvals.jsonl``, one JSON object per line:

- ``{"event": "requested", "id", "agent", "action", "description", "risk",
  "root_source", "args", "pid", "at"}``
- ``{"event": "decided", "id", "agent", "action", "approved", "decided_by",
  "via", "note", "at"}``. ``via`` is the surface the decision came through
  (``dashboard``, ``whatsapp``, ``telegram``, ``trusted-sender``, ``timeout``).
  ``decided_by`` is the human principal (``human-<username>``) or the
  messenger identity; a timeout reads ``system``.
- ``{"event": "abandoned", "id", "at", "reason"}`` for a request whose
  process died before anyone answered (written by the next process).

The file is append-only: nothing in the runtime rewrites or truncates it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)


@dataclass
class ApprovalRequest:
    """A request for human approval before an agent action."""

    id: str
    agent_name: str
    action: str
    description: str
    payload: dict[str, Any] = field(default_factory=dict)
    risk: str = "low"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    timeout_seconds: int = 300
    approved: bool | None = None
    resolved_at: datetime | None = None
    resolver_note: str = ""
    # Who decided and through which surface; see the module docstring.
    decided_by: str = ""
    decided_via: str = ""


_pending: dict[str, ApprovalRequest] = {}
_callbacks: dict[str, asyncio.Future] = {}
_delivery_fn: Callable[[ApprovalRequest], Coroutine] | None = None
_APPROVAL_RULES_CACHE: dict[str, dict[str, list[str]]] | None = None


_AUDIT_LOCK = threading.Lock()
_AUDIT_RECONCILED = False


def audit_log_path() -> Path:
    """``<home>/audit/approvals.jsonl``: the append-only approval record."""
    return get_olano_paths().root / "audit" / "approvals.jsonl"


_SECRET_KEY_RE = None  # compiled lazily; see redact_args


def redact_args(action: str, args: Any) -> Any:
    """Tool arguments made safe to store or show to a person in a chat.

    The approval record is a plain file and a chat prompt is a message on
    someone's phone, so neither may carry a credential. Three layers: every
    argument value of a secret tool (``secret_set``'s value is the secret), any
    argument whose name says it holds one (``password``, ``api_key``,
    ``token``), and credential-shaped text anywhere else (the output
    scrubber's patterns and its high-entropy check).
    """
    import re

    global _SECRET_KEY_RE
    if _SECRET_KEY_RE is None:
        _SECRET_KEY_RE = re.compile(
            r"(secret|password|passwd|token|api[_-]?key|private[_-]?key|credential)", re.I
        )
    try:
        from olano.security.hitl_policy import CATEGORY_TOOLS

        secret_tool = action in CATEGORY_TOOLS.get("secret_write", frozenset()) or (
            action in CATEGORY_TOOLS.get("secret_read", frozenset())
        )
    except Exception:  # pragma: no cover - defensive
        secret_tool = False
    from olano.security.output_scrub import scrub_text

    def _clean(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {k: _clean(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [_clean(v, key) for v in value]
        if not isinstance(value, str):
            return value
        if key and _SECRET_KEY_RE.search(key):
            return "[REDACTED]"
        if secret_tool and key not in ("name", "key_name", "agent", "description", "scope"):
            return "[REDACTED]"
        return scrub_text(value)[0]

    try:
        return _clean(args)
    except Exception:  # never fail an approval over its record
        return "[REDACTED]"


def _audit(event: dict[str, Any]) -> None:
    """Append one event to the approval record. Never raises."""
    event = {**event, "at": event.get("at") or datetime.now(UTC).isoformat()}
    if "args" in event:
        event["args"] = redact_args(str(event.get("action") or ""), event["args"])
    try:
        path = audit_log_path()
        line = json.dumps(event, default=str, ensure_ascii=False) + "\n"
        with _AUDIT_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception as exc:  # the record must never break an approval
        logger.warning("HITL: could not append to the approval record: %s", exc)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:  # EPERM etc.: it exists
        return True
    return True


def _reconcile_audit_log() -> None:
    """Mark requests whose process died unanswered as ``abandoned``.

    Runs once per process, before its first request. A request still open in
    a process that is alive (a one-shot run beside the server) is left alone.
    """
    global _AUDIT_RECONCILED
    if _AUDIT_RECONCILED:
        return
    _AUDIT_RECONCILED = True
    try:
        path = audit_log_path()
        if not path.is_file():
            return
        open_reqs: dict[str, int] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            rid = str(row.get("id") or "")
            if row.get("event") == "requested":
                open_reqs[rid] = int(row.get("pid") or 0)
            elif row.get("event") in ("decided", "abandoned"):
                open_reqs.pop(rid, None)
        for rid, pid in open_reqs.items():
            if pid != os.getpid() and not (pid and _pid_alive(pid)):
                _audit({"event": "abandoned", "id": rid, "reason": "runtime stopped first"})
    except Exception as exc:
        logger.debug("HITL: approval record reconcile skipped: %s", exc)


def read_audit_log(
    *, agent: str | None = None, agents: set[str] | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """Recent approval records, newest first, one row per request.

    Each row merges the request with its outcome: ``status`` is ``pending``,
    ``approved``, ``rejected``, ``timed_out`` or ``abandoned``. ``agents``
    limits rows to that set (a member's grants).
    """
    path = audit_log_path()
    if not path.is_file():
        return []
    rows: dict[str, dict[str, Any]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        rid = str(ev.get("id") or "")
        if not rid:
            continue
        if ev.get("event") == "requested":
            rows[rid] = {
                "id": rid,
                "agent": ev.get("agent", ""),
                "action": ev.get("action", ""),
                "description": ev.get("description", ""),
                "risk": ev.get("risk", ""),
                "root_source": ev.get("root_source", ""),
                "args": ev.get("args"),
                "requested_at": ev.get("at"),
                "status": "pending",
                "decided_by": "",
                "via": "",
                "note": "",
                "decided_at": None,
            }
        elif ev.get("event") == "decided":
            row = rows.setdefault(
                rid,
                {"id": rid, "agent": ev.get("agent", ""), "action": ev.get("action", "")},
            )
            if ev.get("via") == "timeout":
                status = "timed_out"
            else:
                status = "approved" if ev.get("approved") else "rejected"
            row.update(
                status=status,
                decided_by=ev.get("decided_by", ""),
                via=ev.get("via", ""),
                note=ev.get("note", ""),
                decided_at=ev.get("at"),
            )
        elif ev.get("event") == "abandoned" and rid in rows:
            rows[rid].update(status="abandoned", note=ev.get("reason", ""), decided_at=ev.get("at"))
    out = list(rows.values())
    if agent:
        out = [r for r in out if r.get("agent") == agent]
    if agents is not None:
        out = [r for r in out if r.get("agent") in agents]
    out.reverse()
    return out[: max(1, limit)]


def record_decision(
    *,
    agent_name: str,
    action: str,
    approved: bool,
    decided_by: str,
    via: str,
    note: str = "",
    args: Any = None,
    risk: str = "",
) -> None:
    """Record a decision made outside the approvals queue.

    Messenger approval prompts and trusted-sender auto-approval decide in the
    turn itself without a queued request; they call this so the record still
    holds every gated action and who let it through.
    """
    import uuid

    rid = "d-" + uuid.uuid4().hex[:10]
    _audit(
        {
            "event": "requested",
            "id": rid,
            "agent": agent_name,
            "action": action,
            "description": action,
            "risk": risk,
            "args": args,
            "pid": os.getpid(),
        }
    )
    _audit(
        {
            "event": "decided",
            "id": rid,
            "agent": agent_name,
            "action": action,
            "approved": bool(approved),
            "decided_by": decided_by,
            "via": via,
            "note": note,
        }
    )


def _approval_rules_path() -> Path:
    return get_olano_paths().approval_rules_path


def load_approval_rules() -> dict[str, dict[str, list[str]]]:
    """Load persisted tool approval rules for the active Olano home."""
    global _APPROVAL_RULES_CACHE
    if _APPROVAL_RULES_CACHE is not None:
        return _APPROVAL_RULES_CACHE

    path = _approval_rules_path()
    if path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            rules = raw if isinstance(raw, dict) else {}
        except Exception as exc:
            logger.warning("HITL: failed to read approval rules: %s", exc)
            rules = {}
    else:
        rules = {}
    _APPROVAL_RULES_CACHE = rules
    return rules


def save_approval_rules(rules: dict[str, dict[str, list[str]]]) -> None:
    """Persist tool approval rules for the active Olano home."""
    global _APPROVAL_RULES_CACHE
    path = _approval_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(rules, sort_keys=False), encoding="utf-8")
    _APPROVAL_RULES_CACHE = rules


def is_tool_rule_allowed(agent_name: str, tool_name: str, rule_key: str) -> bool:
    """Return True if the active home already remembers this approval rule."""
    rules = load_approval_rules()
    agent_rules = rules.get(agent_name, {})
    allowed = set(agent_rules.get(tool_name, []))
    return "*" in allowed or rule_key in allowed


def remember_tool_rule(agent_name: str, tool_name: str, rule_key: str) -> None:
    """Persist an always-allow rule for an agent/tool/rule key tuple."""
    rules = load_approval_rules()
    agent_rules = rules.setdefault(agent_name, {})
    values = set(agent_rules.get(tool_name, []))
    values.add(rule_key)
    agent_rules[tool_name] = sorted(values)
    save_approval_rules(rules)


def clear_approval_cache() -> None:
    """Clear the in-process rules cache for tests or config reloads."""
    global _APPROVAL_RULES_CACHE
    _APPROVAL_RULES_CACHE = None


def set_delivery(fn: Callable[[ApprovalRequest], Coroutine] | None) -> None:
    """Register a coroutine that delivers approval requests."""
    global _delivery_fn
    _delivery_fn = fn


def has_delivery() -> bool:
    """True when an approval-delivery channel (e.g. dashboard inbox) is wired.

    Used by the deferral path to avoid blocking a turn on an approval that no
    one can resolve (e.g. a one-shot CLI run with no runtime inbox)."""
    return _delivery_fn is not None


async def request_approval(
    agent_name: str,
    action: str,
    description: str,
    payload: dict[str, Any] | None = None,
    risk: str = "low",
    timeout_seconds: int = 300,
) -> bool:
    """Request approval through the currently configured delivery channel."""
    import uuid

    req_id = str(uuid.uuid4())[:8]
    req = ApprovalRequest(
        id=req_id,
        agent_name=agent_name,
        action=action,
        description=description,
        payload=payload or {},
        risk=risk,
        timeout_seconds=timeout_seconds,
    )
    _pending[req_id] = req
    _reconcile_audit_log()
    origin = (req.payload or {}).get("origin") or {}
    _audit(
        {
            "event": "requested",
            "id": req_id,
            "agent": agent_name,
            "action": action,
            "description": description,
            "risk": risk,
            "root_source": origin.get("root_source", "") if isinstance(origin, dict) else "",
            "args": (req.payload or {}).get("args"),
            "pid": os.getpid(),
        }
    )

    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    _callbacks[req_id] = future

    if _delivery_fn:
        try:
            await _delivery_fn(req)
        except Exception as exc:
            logger.warning("HITL delivery failed: %s", exc)
    else:
        logger.warning(
            "HITL approval required [%s]: %s — %s (risk=%s)",
            req_id,
            action,
            description,
            risk,
        )

    try:
        approved = await asyncio.wait_for(future, timeout=timeout_seconds)
        return bool(approved)
    except TimeoutError:
        logger.warning("HITL request %s timed out", req_id)
        _resolve(req_id, approved=False, note="timed out", decided_by="system", via="timeout")
        return False
    finally:
        _pending.pop(req_id, None)
        _callbacks.pop(req_id, None)


def resolve(
    req_id: str,
    approved: bool,
    note: str = "",
    *,
    decided_by: str = "",
    via: str = "",
) -> bool:
    """Resolve a pending approval request.

    ``decided_by`` names who decided (``human-<username>`` for a dashboard
    user, the messenger identity for a chat reply) and ``via`` the surface;
    both land in the approval record.
    """
    return _resolve(req_id, approved, note, decided_by=decided_by, via=via)


def _resolve(
    req_id: str,
    approved: bool,
    note: str = "",
    *,
    decided_by: str = "",
    via: str = "",
) -> bool:
    req = _pending.get(req_id)
    if not req:
        return False
    if req.approved is not None:
        return False  # first decision wins; a second click must not rewrite it
    req.approved = approved
    req.resolved_at = datetime.now(UTC)
    req.resolver_note = note
    req.decided_by = decided_by or "unknown"
    req.decided_via = via or "unknown"
    _audit(
        {
            "event": "decided",
            "id": req_id,
            "agent": req.agent_name,
            "action": req.action,
            "approved": bool(approved),
            "decided_by": req.decided_by,
            "via": req.decided_via,
            "note": note,
        }
    )
    future = _callbacks.get(req_id)
    if future and not future.done():
        future.set_result(approved)
    return True


def list_pending() -> list[ApprovalRequest]:
    """List all unresolved approval requests."""
    return list(_pending.values())
