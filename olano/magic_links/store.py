"""Persistent records for magic links.

One JSON file, ``<home>/handshake/links.json``, written atomically under the
interprocess ``deployment_write_lock`` — the same shape and discipline as the
page registry (``olano/pages/registry.py``), because both are low-volume
host-wide state that the CLI and the runtime API may write concurrently.

What is stored, and what deliberately is not:

* the **SHA-256 of the link token**, never the token — a database read yields
  nothing that opens a link (``users/passwords.py`` idiom, same as sessions and
  invites);
* the **PBKDF2 hash of the PIN**, never the PIN;
* the flow id + its parameters, who asked for it, and where to report back;
* an append-only audit trail of every transition.

Never the secret a form flow collects: those go straight to the vault and are
not echoed into the record, the audit, the logs, or the completion notice.

Lifecycle::

    pending ──unlock──► pending ──start──► in_progress ──► done
        │                                        │
        └──────────── expired | revoked ◄────────┴──► error

A terminal or expired link keeps its (hashed) token for a short retention
window so the page can say "this link was already used" instead of a bare 404,
but :func:`serves_actions` is False for it — no unlock, no start, no submit.
Only a token matching *nothing* gets the uniform 404 (the webhook-ingress
idiom): a wrong guess must not reveal whether an id exists.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from olano.config.home import get_olano_paths
from olano.deployment.locks import deployment_write_lock
from olano.memory.markdown_memory import atomic_write_text
from olano.users.passwords import hash_password, verify_password

logger = logging.getLogger(__name__)

# Flow statuses. "pending" covers both "not yet opened" and "unlocked but not
# started" — page-session state (the unlock) is tracked separately, since it
# describes the browser tab, not the magic link.
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"

TERMINAL_STATUSES = frozenset({STATUS_DONE, STATUS_ERROR, STATUS_EXPIRED, STATUS_REVOKED})

# How long a finished/expired record is kept for the "already used" page and the
# dashboard's recent-links list before it is pruned.
RETENTION_HOURS = 24

# An unlocked page session lasts this long; re-entering the PIN renews it. Short
# enough that a borrowed/left-open tab stops working, long enough to sign in at
# a provider and come back.
UNLOCK_TTL_SECONDS = 900

_PIN_DIGITS = 6


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def hash_token(raw_token: str) -> str:
    """SHA-256 hex of a link token — what we persist (never the token)."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def new_token() -> str:
    """A fresh 256-bit URL-safe link token."""
    return secrets.token_urlsafe(32)


def new_pin() -> str:
    """A fresh 6-digit PIN, leading zeros preserved."""
    return f"{secrets.randbelow(10**_PIN_DIGITS):0{_PIN_DIGITS}d}"


@dataclass
class LinkRecord:
    """One magic link. Serialized verbatim into ``links.json``."""

    id: str
    token_hash: str
    flow: str
    params: dict[str, Any] = field(default_factory=dict)
    requested_by: dict[str, Any] = field(default_factory=dict)
    notify: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_PENDING
    session_id: str | None = None
    # Short human summary of the outcome, shown on the page and in the chat
    # notice ("Connected as jrivera", "Stored OPENAI_API_KEY"). Never a secret.
    result: str = ""
    error: str = ""
    pin_hash: str | None = None
    pin_attempts: int = 0
    # Wrong-password attempts on the owner-login form (a distinct credential
    # from the PIN, so counted separately before the link burns).
    grant_attempts: int = 0
    unlock_hash: str | None = None
    unlock_expires_at: str | None = None
    # Set once the completion side effects (chat notice + post_complete hook)
    # have run, so a page re-poll, the watcher, and a post-restart resume all
    # see it done. Persisted (not an in-process set) so the guard is bounded by
    # the record's own 24h retention and survives a restart.
    finalized: bool = False
    created_at: str = ""
    expires_at: str = ""
    audit: list[dict[str, Any]] = field(default_factory=list)

    # ── derived state ────────────────────────────────────────────────────────

    @property
    def is_expired(self) -> bool:
        deadline = _parse(self.expires_at)
        return deadline is not None and _now() > deadline

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def requires_pin(self) -> bool:
        return bool(self.pin_hash)

    def serves_actions(self) -> bool:
        """Whether unlock/start/submit are still allowed for this link."""
        return not self.is_terminal and not self.is_expired

    def unlocked(self) -> bool:
        """Whether a live page session exists (or none was ever needed)."""
        if not self.requires_pin:
            return True
        deadline = _parse(self.unlock_expires_at)
        return bool(self.unlock_hash) and deadline is not None and _now() <= deadline

    def check_pin(self, pin: str) -> bool:
        stored = self.pin_hash
        return bool(stored) and verify_password((pin or "").strip(), str(stored))

    def check_unlock(self, raw_unlock: str) -> bool:
        """Constant-time check of a page-session token against this link."""
        if not self.requires_pin:
            return True
        if not raw_unlock or not self.unlock_hash or not self.unlocked():
            return False
        return hmac.compare_digest(hash_token(raw_unlock), self.unlock_hash)

    def public(self) -> dict[str, Any]:
        """Admin-facing summary. Carries no token, PIN, or secret value."""
        return {
            "id": self.id,
            "flow": self.flow,
            "service": self.params.get("service") or self.params.get("name") or "",
            "agent": self.params.get("agent") or None,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "requested_by": dict(self.requested_by),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "expired": self.is_expired,
            "pin_required": self.requires_pin,
        }


# ── file IO ──────────────────────────────────────────────────────────────────


def _load_raw() -> dict[str, Any]:
    path = get_olano_paths().magic_links_store_path
    if not path.exists():
        return {"version": 1, "links": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("magic-link: links.json unreadable — starting from empty")
        return {"version": 1, "links": {}}
    if not isinstance(data, dict) or not isinstance(data.get("links"), dict):
        return {"version": 1, "links": {}}
    return data


def _save_raw(data: dict[str, Any]) -> None:
    path = get_olano_paths().magic_links_store_path
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - exotic filesystems
        pass


def _to_record(payload: dict[str, Any]) -> LinkRecord | None:
    try:
        known = {f for f in LinkRecord.__dataclass_fields__}
        return LinkRecord(**{k: v for k, v in payload.items() if k in known})
    except (TypeError, ValueError):
        return None


def _all_records(data: dict[str, Any]) -> dict[str, LinkRecord]:
    records: dict[str, LinkRecord] = {}
    for link_id, payload in data.get("links", {}).items():
        if isinstance(payload, dict):
            record = _to_record(payload)
            if record is not None:
                records[link_id] = record
    return records


def _prune(records: dict[str, LinkRecord]) -> dict[str, LinkRecord]:
    """Drop records past the retention window (terminal or long-expired)."""
    cutoff = _now() - timedelta(hours=RETENTION_HOURS)
    keep: dict[str, LinkRecord] = {}
    for link_id, record in records.items():
        reference = _parse(record.expires_at) or _parse(record.created_at)
        spent = record.is_terminal or record.is_expired
        if spent and reference is not None and reference < cutoff:
            continue
        keep[link_id] = record
    return keep


def _write(records: dict[str, LinkRecord]) -> None:
    _save_raw(
        {
            "version": 1,
            "links": {link_id: asdict(record) for link_id, record in records.items()},
        }
    )


# ── public API (every mutation is lock → read → modify → write) ──────────────


def create(
    *,
    flow: str,
    params: dict[str, Any],
    requested_by: dict[str, Any],
    notify: dict[str, Any],
    ttl_minutes: int,
    pin: str | None,
    minted_by: str = "",
) -> tuple[LinkRecord, str]:
    """Create a link. Returns the record and the **raw token** (shown once)."""
    raw_token = new_token()
    now = _now()
    record = LinkRecord(
        id=f"hs_{uuid.uuid4().hex[:16]}",
        token_hash=hash_token(raw_token),
        flow=flow,
        params=dict(params),
        requested_by=dict(requested_by),
        notify=dict(notify),
        pin_hash=hash_password(pin) if pin else None,
        created_at=_iso(now),
        expires_at=_iso(now + timedelta(minutes=ttl_minutes)),
        audit=[{"ts": _iso(now), "event": "minted", "by": minted_by or "unknown"}],
    )
    with deployment_write_lock():
        records = _prune(_all_records(_load_raw()))
        records[record.id] = record
        _write(records)
    return record, raw_token


def find_by_token(raw_token: str) -> LinkRecord | None:
    """The record a raw token opens, or ``None`` when nothing matches.

    Expired and terminal records still match — the page tells the user their
    link is spent rather than 404ing on a token that really was theirs.
    """
    if not raw_token:
        return None
    digest = hash_token(raw_token)
    for record in _all_records(_load_raw()).values():
        if hmac.compare_digest(record.token_hash, digest):
            return record
    return None


def get(link_id: str) -> LinkRecord | None:
    return _all_records(_load_raw()).get(link_id)


def list_all(*, include_terminal: bool = True) -> list[LinkRecord]:
    """Every live record, newest first."""
    records = list(_all_records(_load_raw()).values())
    if not include_terminal:
        records = [r for r in records if not r.is_terminal and not r.is_expired]
    return sorted(records, key=lambda r: r.created_at, reverse=True)


def mutate(link_id: str, apply: Callable[[LinkRecord], None]) -> LinkRecord | None:
    """Apply ``apply(record)`` under the write lock and persist the result.

    The callback receives the *current* record (re-read inside the lock, so it
    never operates on a stale copy) and mutates it in place. Returns the
    persisted record, or ``None`` if it no longer exists.
    """
    with deployment_write_lock():
        loaded = _all_records(_load_raw())
        records = _prune(loaded)
        record = records.get(link_id)
        if record is None:
            # The target is gone, but the prune may still have work to persist —
            # otherwise expired records only ever disappear on the next
            # *successful* mutation of some other link, and the retention
            # window quietly stops being real.
            if len(records) != len(loaded):
                _write(records)
            return None
        apply(record)
        records[link_id] = record
        _write(records)
        return record


def note(record: LinkRecord, event: str, detail: str = "", by: str = "") -> None:
    """Append an audit entry in place (caller persists via :func:`mutate`)."""
    entry: dict[str, Any] = {"ts": _iso(_now()), "event": event}
    if detail:
        entry["detail"] = detail
    if by:
        entry["by"] = by
    record.audit.append(entry)
    # Bound the trail: a poll loop can add entries for a long time, and this
    # file is rewritten whole on every mutation.
    if len(record.audit) > 50:
        del record.audit[:-50]


@dataclass(frozen=True)
class UnlockOutcome:
    """Result of one PIN attempt. ``raw_unlock`` is empty unless ``ok``."""

    ok: bool
    raw_unlock: str = ""
    error: str = ""


def attempt_unlock(link_id: str, pin: str, max_attempts: int) -> UnlockOutcome:
    """Verify a PIN and count the attempt **atomically**, under the write lock.

    Check-and-count in one :func:`mutate` so the "≤N tries then burn" invariant
    does not depend on the process/loop model. Were the read (``check_pin``) and
    the write (increment/burn) split across the lock — as they were when this
    lived half in the service — N concurrent ``POST /unlock`` requests could each
    evaluate the PIN against the pre-burn record and get N guesses. That is safe
    today only because uvicorn is single-worker and the unlock path has no
    ``await``; folding it here makes it safe regardless.

    On success mints and stores the page-session token and resets the counter;
    on the final failure flips the link to ``revoked``.
    """
    raw_unlock = new_token()
    outcome: dict[str, Any] = {"ok": False, "raw": "", "error": ""}

    def _apply(record: LinkRecord) -> None:
        if not record.serves_actions():
            outcome["error"] = "This link is no longer active."
            return
        if not record.requires_pin:
            outcome["ok"] = True  # no PIN policy → no page-session token needed
            return
        if record.check_pin(pin):
            record.unlock_hash = hash_token(raw_unlock)
            record.unlock_expires_at = _iso(_now() + timedelta(seconds=UNLOCK_TTL_SECONDS))
            record.pin_attempts = 0
            note(record, "unlocked")
            outcome.update(ok=True, raw=raw_unlock)
            return
        record.pin_attempts += 1
        note(record, "pin_failed", detail=f"attempt {record.pin_attempts}")
        if record.pin_attempts >= max_attempts:
            record.status = STATUS_REVOKED
            record.error = "Too many incorrect PIN attempts."
            note(record, "burned", detail="pin attempts exhausted")
            outcome["error"] = "Too many incorrect PINs — this link has been disabled."
        else:
            remaining = max_attempts - record.pin_attempts
            plural = "tries" if remaining != 1 else "try"
            outcome["error"] = f"Incorrect PIN. {remaining} {plural} left."

    if mutate(link_id, _apply) is None:
        return UnlockOutcome(False, "", "This link is no longer active.")
    return UnlockOutcome(bool(outcome["ok"]), str(outcome["raw"]), str(outcome["error"]))


def claim_finalize(link_id: str) -> bool:
    """Atomically claim the one-time completion side effects for ``link_id``.

    Returns True exactly once — for the caller that flips ``finalized`` — so the
    page re-poll, the background watcher, and a post-restart resume can all try
    and only one runs the notice + ``post_complete`` hook. The flag rides on the
    record, so it is bounded by the 24h retention and survives a restart (no
    in-process set to leak, and no double-notify after a reboot).
    """
    claimed = {"v": False}

    def _apply(record: LinkRecord) -> None:
        if not record.finalized:
            record.finalized = True
            claimed["v"] = True

    mutate(link_id, _apply)
    return claimed["v"]


def finish(link_id: str, *, status: str, result: str = "", error: str = "") -> LinkRecord | None:
    """Move a link to a terminal state and end its page session."""

    def _apply(record: LinkRecord) -> None:
        record.status = status
        if result:
            record.result = result
        if error:
            record.error = error
        record.unlock_hash = None
        record.unlock_expires_at = None
        note(record, "finished", detail=status)

    return mutate(link_id, _apply)
