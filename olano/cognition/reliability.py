"""Reliability guard rails for background reasoning runs.

Three small pieces that together turn a transient provider refusal into a
retry instead of a dead run and a bell:

- **A concurrency slot** (:func:`background_slot`) — a process-wide cap on how
  many background workers talk to a provider at once. Before this, every agent
  on a box fired its Cortex phases in the same minute (identical cron strings,
  no per-agent stagger) and nothing bounded the burst, so an N-agent fleet
  self-inflicted a 429 on the very first model call of each run.
- **A retry policy** (:func:`retry_delays`, :data:`RETRYABLE_CODES`) — which
  classified failures a scheduled phase may be re-fired for, and how long to
  wait. The delays are on the *minute* scale a rate-limit window actually needs;
  the openai SDK's own transport retries are seconds apart and spent long
  before a quota window reopens.
- **A failure streak** (:func:`record_failure` / :func:`clear_failures`) — a
  per-workspace count of consecutive failed runs per worker, so the operator's
  bell rings for a *persistent* problem (two runs in a row) and not for a
  one-off that the retry above will heal on its own.

The retry itself is deliberately NOT inside :func:`olano.cognition.worker.run_worker`:
a worker's checkpoint thread is unique per run, so re-invoking the graph in
place would either resume a half-written checkpoint or append the goal message
a second time. CLAUDE.md states the same rule for the gateway client — a retry
lives outside the graph, never in it. The scheduler re-fires the whole phase
with a fresh worker instead (``scheduler/cron.py``), and every phase this
applies to is idempotent by construction (snapshot + rewrite from current
state).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from olano.config.workspace import WORKSPACE_STATE_DIRNAME
from olano.security.delegate_guards import (
    DEFAULT_WORKER_CONCURRENCY,
    clamp_worker_concurrency,
)

logger = logging.getLogger(__name__)

# ────────────────────────────── retry policy ────────────────────────────────

#: Classified failure codes (``core/actionable_errors``) a scheduled phase is
#: re-fired for. Deliberately only the two that heal on their own with time:
#: NOT ``billing_limit`` (a spent balance is not transient), NOT ``needs_setup``
#: / auth codes (no retry can mint a key), and NOT ``timeout`` — a worker that
#: ran its whole wall-clock budget will do so again, and re-buying twenty
#: minutes of the same run just converts one failure into two.
RETRYABLE_CODES: frozenset[str] = frozenset({"rate_limited", "overloaded"})

#: Total attempts per scheduled firing (the first run plus the retries) and the
#: backoff before each retry. 15 min, then 60 min: a provider rate-limit window
#: is minutes to an hour, and the retry must land *after* it reopens, not inside
#: it — the whole reason the SDK's seconds-apart retries never helped here.
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (900.0, 3600.0)


def retry_delays() -> tuple[float, ...]:
    """Backoff before each phase retry; empty disables retrying entirely.

    Env-overridable so an incident can be tuned on a live box without a
    release, mirroring the gateway client's knobs:
    ``LIONCLAW_COGNITION_RETRIES`` is the TOTAL attempt count (``1`` = no retry)
    and ``LIONCLAW_COGNITION_BACKOFF`` a comma-separated list of seconds. A
    malformed value falls back to the default rather than failing the run.
    """
    attempts = DEFAULT_RETRY_ATTEMPTS
    raw_attempts = os.environ.get("LIONCLAW_COGNITION_RETRIES", "").strip()
    if raw_attempts:
        try:
            attempts = max(1, int(raw_attempts))
        except ValueError:
            logger.warning("Ignoring malformed LIONCLAW_COGNITION_RETRIES=%r", raw_attempts)

    delays = DEFAULT_RETRY_BACKOFF_SECONDS
    raw_backoff = os.environ.get("LIONCLAW_COGNITION_BACKOFF", "").strip()
    if raw_backoff:
        try:
            delays = tuple(max(0.0, float(p)) for p in raw_backoff.split(",") if p.strip())
        except ValueError:
            logger.warning("Ignoring malformed LIONCLAW_COGNITION_BACKOFF=%r", raw_backoff)

    if attempts <= 1 or not delays:
        return ()
    needed = attempts - 1
    return tuple(delays[i] if i < len(delays) else delays[-1] for i in range(needed))


def jittered(delay: float) -> float:
    """±25% so a fleet retrying the same outage does not resonate."""
    return delay * random.uniform(0.75, 1.25)


def is_retryable_code(code: str | None) -> bool:
    return bool(code) and code in RETRYABLE_CODES


# ─────────────────────────────── failure streak ─────────────────────────────

DEFAULT_BELL_STREAK = 2

#: Remediation kinds that mean *the operator has to do something*: a sign-in
#: to redo, a key to set, a balance to top up, a vault to unlock, Olano to
#: contact. Those never heal on their own, so holding them back for a streak
#: would only delay the person who has to act. Everything else — ``retry``,
#: ``none``, or no remediation at all — is a failure a repeat may not repeat,
#: and waits for the streak.
OPERATOR_REMEDIES: frozenset[str] = frozenset(
    {"reconnect_provider", "set_api_key", "top_up_billing", "unlock_vault", "contact_support"}
)


def needs_operator(remediation: dict[str, object] | None) -> bool:
    """True when the classified failure needs a human before it can succeed."""
    kind = (remediation or {}).get("kind")
    return isinstance(kind, str) and kind in OPERATOR_REMEDIES


def holds_for_streak(status: str, remediation: dict[str, object] | None, streak: int) -> bool:
    """Whether a failed run stays off the bell/inbox for now.

    ``needs_setup`` and any operator-remedy failure surface on the first run;
    everything else surfaces once it has failed ``bell_streak_threshold()``
    runs in a row.
    """
    if status == "needs_setup" or needs_operator(remediation):
        return False
    return streak < bell_streak_threshold()


def bell_streak_threshold() -> int:
    """Consecutive failed runs of one worker before the bell rings.

    ``LIONCLAW_COGNITION_BELL_STREAK`` overrides; ``1`` restores ring-on-first.
    """
    raw = os.environ.get("LIONCLAW_COGNITION_BELL_STREAK", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            logger.warning("Ignoring malformed LIONCLAW_COGNITION_BELL_STREAK=%r", raw)
    return DEFAULT_BELL_STREAK


def _streak_path(workspace_root: Path) -> Path:
    return workspace_root / WORKSPACE_STATE_DIRNAME / "state" / "cognition" / "worker_failures.json"


def _load_streaks(workspace_root: Path) -> dict[str, dict[str, object]]:
    path = _streak_path(workspace_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_streaks(workspace_root: Path, data: dict[str, dict[str, object]]) -> None:
    path = _streak_path(workspace_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("reliability: failed to persist failure streaks: %s", exc)


def _as_int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def failure_streak(workspace_root: Path, label: str) -> int:
    """Consecutive failed runs recorded for ``label`` (0 when none)."""
    entry = _load_streaks(workspace_root).get(label) or {}
    return _as_int(entry.get("streak", 0))


def failure_since(workspace_root: Path, label: str) -> str | None:
    """ISO timestamp of the first failure in the current streak, if any."""
    entry = _load_streaks(workspace_root).get(label) or {}
    since = entry.get("since")
    return str(since) if since else None


def record_failure(workspace_root: Path, label: str) -> int:
    """Bump ``label``'s consecutive-failure count and return the new streak."""
    data = _load_streaks(workspace_root)
    entry = data.get(label) or {}
    streak = _as_int(entry.get("streak", 0)) + 1
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data[label] = {
        "streak": streak,
        "since": entry.get("since") if streak > 1 and entry.get("since") else now,
        "last": now,
    }
    _save_streaks(workspace_root, data)
    return streak


def clear_failures(workspace_root: Path, label: str) -> None:
    """Reset ``label``'s streak after a successful run. No-op when unset."""
    data = _load_streaks(workspace_root)
    if label in data:
        del data[label]
        _save_streaks(workspace_root, data)


# ───────────────────────────── concurrency slot ─────────────────────────────


def effective_worker_concurrency() -> int:
    """The home-wide ``cognition.worker_max_concurrency``, clamped."""
    try:
        from olano.config.system_settings import load_system_settings

        return clamp_worker_concurrency(load_system_settings().worker_max_concurrency)
    except Exception:  # pragma: no cover - settings must never break a run
        return DEFAULT_WORKER_CONCURRENCY


# loop → (limit, semaphore). A Semaphore binds to the loop it first waits on
# (same reason core/checkpoints caches its savers per loop), so the cache is
# keyed on the loop — weakly, so a finished loop is not kept alive by it — and
# a changed limit replaces the entry, so an operator raising the cap takes
# effect on the next run without a restart.
_SLOTS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]] = (
    weakref.WeakKeyDictionary()
)


def _semaphore(limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    entry = _SLOTS.get(loop)
    if entry is None or entry[0] != limit:
        entry = (limit, asyncio.Semaphore(limit))
        _SLOTS[loop] = entry
    return entry[1]


def reset_slots() -> None:
    """Test hook: forget every cached semaphore."""
    _SLOTS.clear()


@asynccontextmanager
async def background_slot(label: str = "worker") -> AsyncIterator[None]:
    """Hold one of the box-wide background-run slots for the block.

    Acquire it *before* the model build and before the run's own wall-clock
    timer starts: queueing behind another agent's run must cost a later start,
    never the run's own budget.
    """
    limit = effective_worker_concurrency()
    sem = _semaphore(limit)
    if sem.locked():
        logger.info("run_worker[%s] waiting for a background slot (limit=%d)", label, limit)
    async with sem:
        yield
