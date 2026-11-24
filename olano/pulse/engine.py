"""PULSE engine — queue management, preference parsing, and approval actions."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import yaml

from olano.pulse.models import (
    PulseItem,
    PulseLane,
    PulsePreferences,
    PulsePriority,
    PulseStatus,
    PulseType,
)

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig, PulseConfig

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_pulse_dir(state_dir: Path) -> Path:
    if state_dir.name == "pulse":
        return state_dir
    if state_dir.name == "state":
        return state_dir / "pulse"
    return state_dir / "state" / "pulse"


def _parse_time_string(value: str) -> tuple[int, int] | None:
    text = value.strip()
    if not text:
        return None
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _collect_bullets(body: str, heading: str) -> list[str]:
    lines = body.splitlines()
    wanted = heading.strip().lower()
    collecting = False
    items: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("## "):
            collecting = stripped[3:].strip().lower() == wanted
            continue
        if not collecting or not stripped:
            continue
        if stripped.startswith(("- ", "* ")):
            items.append(stripped[2:].strip())
    return items


def load_pulse_preferences(workspace_root: Path) -> PulsePreferences:
    """Parse PULSE.md frontmatter and list sections if the file exists."""
    pulse_path = workspace_root / "PULSE.md"
    if not pulse_path.exists():
        return PulsePreferences()

    text = pulse_path.read_text(encoding="utf-8")
    frontmatter: dict[str, Any] = {}
    body = text
    if text.startswith("---\n"):
        _, _, remainder = text.partition("---\n")
        fm_text, sep, rest = remainder.partition("\n---\n")
        if sep:
            frontmatter = yaml.safe_load(fm_text) or {}
            body = rest

    quiet = frontmatter.get("quiet_hours") or {}
    max_items = frontmatter.get("max_items")
    try:
        parsed_max_items = int(max_items) if max_items is not None else None
    except (TypeError, ValueError):
        parsed_max_items = None
    return PulsePreferences(
        delivery=str(frontmatter.get("delivery") or "batch"),
        cadence=str(frontmatter.get("cadence") or ""),
        max_items=parsed_max_items,
        quiet_hours_start=str(quiet.get("start") or ""),
        quiet_hours_end=str(quiet.get("end") or ""),
        timezone=str(frontmatter.get("timezone") or "UTC"),
        interests=_collect_bullets(body, "Interests"),
        check_routines=_collect_bullets(body, "Check Routines"),
        delivery_preferences=_collect_bullets(body, "Delivery Preferences"),
        excluded_topics=_collect_bullets(body, "Excluded Topics"),
    )


class PulseEngine:
    """
    Manages a priority-sorted queue of PulseItems.

    Queue storage lives at `.olano/state/pulse/inbox.json`. Older queue locations are
    read as fallbacks for compatibility.
    """

    def __init__(self, config: AgentConfig, state_dir: Path) -> None:
        self.config = config
        self.pulse_config: PulseConfig = config.pulse
        self.state_dir = state_dir
        self._pulse_dir = _resolve_pulse_dir(state_dir)
        self._queue_path = self._pulse_dir / "inbox.json"
        self._legacy_queue_paths = [
            state_dir / "pulse_queue.json",
            state_dir / "state" / "pulse_queue.json",
        ]
        self._items: list[PulseItem] = []
        self._user_routines_synced = False
        self.preferences = self._load_preferences()
        self._load()

    @property
    def workspace_root(self) -> Path | None:
        if self.config.workspace:
            return Path(self.config.workspace)
        if self._pulse_dir.name == "pulse" and self._pulse_dir.parent.name == "state":
            return self._pulse_dir.parent.parent
        return None

    def _load_preferences(self) -> PulsePreferences:
        root = self.workspace_root
        if root is None:
            return PulsePreferences()
        try:
            return load_pulse_preferences(root)
        except Exception as exc:
            logger.debug("PULSE: failed to parse PULSE.md: %s", exc)
            return PulsePreferences()

    # ──────────────────────────────────── Queue management ───────────────────

    def add(
        self,
        content: str,
        source: str = "olano",
        type: PulseType = PulseType.SUGGESTION,
        priority: PulsePriority | str | int = PulsePriority.NORMAL,
        lane: PulseLane | str = PulseLane.PULSE,
        title: str = "",
        why_now: str = "",
        suggested_next_step: str = "",
        references: list[str] | None = None,
        expires_in_hours: float | None = 48.0,
        action: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        dedup_window_hours: float = 1.0,
    ) -> PulseItem:
        """Add an item to the queue, deduplicating by content in the recent window.

        Also suppresses re-adding items that were recently rejected (7-day window)
        so that dismissed skills/pulse items don't keep reappearing.
        """
        window = timedelta(hours=max(0.0, dedup_window_hours))
        for item in self._items:
            if item.matches_duplicate(content, window=window, lane=lane, title=title):
                return item

        # Suppress re-add if a similar item was recently rejected.
        rejection_window = timedelta(hours=168)  # 7 days
        now = _utc_now()
        lane_type = PulseLane.from_value(lane)
        other_key = title.strip() or content.strip()
        for item in self._items:
            if item.status != PulseStatus.REJECTED:
                continue
            self_key = item.title.strip() or item.content.strip()
            if (
                item.lane_type == lane_type
                and self_key == other_key
                and now - item.created_at <= rejection_window
            ):
                logger.debug("PULSE: suppressing re-add of recently rejected item %s", item.id)
                return item

        expires_at = None
        if expires_in_hours is not None:
            expires_at = _utc_now() + timedelta(hours=expires_in_hours)

        item = PulseItem(
            id=str(uuid.uuid4())[:8],
            source=source,
            type=type,
            priority=priority,
            content=content,
            lane=PulseLane.from_value(lane),
            title=title.strip(),
            why_now=why_now.strip(),
            suggested_next_step=suggested_next_step.strip(),
            references=[str(ref).strip() for ref in (references or []) if str(ref).strip()],
            expires_at=expires_at,
            action=action,
            metadata=metadata or {},
        )
        self._items.append(item)
        self._save()
        logger.debug(
            "PULSE: added item %s [%s/%s] priority=%s",
            item.id,
            item.lane_type.value,
            source,
            item.priority_level.value,
        )
        # A genuinely new card (both dedupe paths above return early). The push
        # layer filters by lane and priority, and is a no-op outside the
        # runtime server, so the CLI and the tests never send anything.
        try:
            from olano.push import events as push_events

            push_events.pulse_item_added(agent=self.config.name, item=item)
        except Exception:
            logger.debug("PULSE: push hook failed", exc_info=True)
        return item

    def _source_enabled(self, primary: str, *aliases: str) -> bool:
        enabled = {str(source).strip().lower() for source in self.pulse_config.sources}
        candidates = {primary.strip().lower(), *(alias.strip().lower() for alias in aliases)}
        return bool(enabled & candidates)

    def _ensure_user_routines(self) -> None:
        if self._user_routines_synced or not self._source_enabled("user"):
            return
        self._user_routines_synced = True
        for routine in self.preferences.check_routines:
            self.add(
                content=f"Routine reminder: {routine}",
                source="user",
                type=PulseType.REMINDER,
                priority=PulsePriority.LOW,
                lane=PulseLane.PULSE,
                title=routine,
                suggested_next_step="Review this topic when you next plan work or check-ins.",
                expires_in_hours=24,
                metadata={"kind": "check_routine", "routine": routine},
                dedup_window_hours=24,
            )

    def _active_items(self, lane: PulseLane | str | None = PulseLane.PULSE) -> list[PulseItem]:
        self._ensure_user_routines()
        self._expire()
        active = [item for item in self._items if item.is_active]
        if lane is not None:
            lane_type = PulseLane.from_value(lane)
            active = [item for item in active if item.lane_type == lane_type]
        active.sort(key=lambda item: (item.priority_score, item.created_at), reverse=True)
        return active

    def list_pending(
        self,
        max_items: int | None = None,
        *,
        lane: PulseLane | str | None = PulseLane.PULSE,
    ) -> list[PulseItem]:
        """Return active items sorted by priority descending."""
        active = self._active_items(lane=lane)
        if lane == PulseLane.PULSE and self.preferences.max_items:
            limit = max_items or self.preferences.max_items
        else:
            limit = max_items or self.pulse_config.max_items_per_session
        return active[:limit]

    def count_pending(self, *, lane: PulseLane | str | None = PulseLane.PULSE) -> int:
        return len(self._active_items(lane=lane))

    def list_by_status(
        self,
        status: PulseStatus | str,
        *,
        lane: PulseLane | str | None = PulseLane.PULSE,
        max_items: int | None = None,
    ) -> list[PulseItem]:
        """Return items in a given resolution status (approved/rejected/…),
        newest first. Used by the dashboard to show resolved history alongside
        the pending queue."""
        self._expire()
        status_val = status if isinstance(status, PulseStatus) else PulseStatus(str(status))
        items = [item for item in self._items if item.status == status_val]
        if lane is not None:
            lane_type = PulseLane.from_value(lane)
            items = [item for item in items if item.lane_type == lane_type]

        def _resolved_at(item: PulseItem) -> str:
            meta = item.metadata or {}
            return str(
                meta.get("approved_at")
                or meta.get("accepted_at")
                or meta.get("delivered_at")
                or item.created_at.isoformat()
            )

        items.sort(key=_resolved_at, reverse=True)
        return items[:max_items] if max_items else items

    def list_all_pending(self, max_items: int | None = None) -> list[PulseItem]:
        active = self._active_items(lane=None)
        limit = max_items or self.pulse_config.max_items_per_session
        return active[:limit]

    def get_pending(self, max_items: int | None = None) -> list[PulseItem]:
        return self.list_pending(max_items=max_items)

    def get_deliverable(self, max_items: int | None = None) -> list[PulseItem]:
        if self._is_quiet_hours():
            return []
        items = [
            item
            for item in self.list_pending(
                max_items=max_items or self.pulse_config.max_items_per_session,
                lane=PulseLane.PULSE,
            )
            if item.status == PulseStatus.PENDING
        ]
        remaining = max(0, self.pulse_config.max_items_per_day - self._delivered_today_count())
        if remaining == 0:
            return []
        return items[:remaining]

    def get_item(self, item_id: str) -> PulseItem | None:
        return next((item for item in self._items if item.id == item_id), None)

    def resolve(self, item_id: str, status: PulseStatus) -> bool:
        item = self.get_item(item_id)
        if item is None:
            return False
        item.status = status
        if status == PulseStatus.DELIVERED:
            item.metadata["delivered_at"] = _utc_now().isoformat()
        self._save()
        return True

    def approve(self, item_id: str) -> tuple[bool, str]:
        item = self.get_item(item_id)
        if item is None:
            return False, f"PULSE item {item_id} not found."
        if not item.is_active:
            return False, f"PULSE item {item_id} is no longer active."
        # A ``restore_backup`` action is a REVERT wired to the explicit Undo
        # control — it must never run on approve. Approving an already-applied
        # autonomous artifact (e.g. an auto-optimization result) just
        # acknowledges it; the work is already done. (Prevents the data-loss bug
        # where clicking "Keep" on an optimization pulse reverted the changes.)
        if str((item.action or {}).get("type") or "") == "restore_backup":
            item.status = PulseStatus.APPROVED
            item.metadata["approved_at"] = _utc_now().isoformat()
            self._save()
            return True, "Kept — the changes remain applied."
        ok, message = self.execute_action(item)
        if ok:
            item.status = PulseStatus.APPROVED
            item.metadata["approved_at"] = _utc_now().isoformat()
            self._save()
        return ok, message

    async def approve_async(self, item_id: str) -> tuple[bool, str]:
        """Async approval — awaits ``run_worker`` actions inline (used by the
        cognition drainer). Sync callers should keep using :meth:`approve`,
        which enqueues run_worker actions instead of blocking."""
        item = self.get_item(item_id)
        if item is None:
            return False, f"PULSE item {item_id} not found."
        if not item.is_active:
            return False, f"PULSE item {item_id} is no longer active."
        # See approve(): a restore_backup action is a revert, never run on approve.
        if str((item.action or {}).get("type") or "") == "restore_backup":
            item.status = PulseStatus.APPROVED
            item.metadata["approved_at"] = _utc_now().isoformat()
            self._save()
            return True, "Kept — the changes remain applied."
        ok, message, _ = await self.execute_action_async(item)
        if ok:
            item.status = PulseStatus.APPROVED
            item.metadata["approved_at"] = _utc_now().isoformat()
            self._save()
        return ok, message

    def accept(self, item_id: str) -> tuple[bool, str]:
        item = self.get_item(item_id)
        if item is None:
            return False, f"PULSE item {item_id} not found."
        if not item.is_active:
            return False, f"PULSE item {item_id} is no longer active."
        if item.action:
            return self.approve(item_id)
        item.status = PulseStatus.APPROVED
        item.metadata["accepted_at"] = _utc_now().isoformat()
        self._save()
        return True, f"Accepted PULSE item {item_id}."

    def reject(self, item_id: str) -> bool:
        return self.resolve(item_id, PulseStatus.REJECTED)

    def defer(self, item_id: str, hours: float = 24.0) -> bool:
        item = self.get_item(item_id)
        if item is None:
            return False
        base = item.expires_at if item.expires_at and item.expires_at > _utc_now() else _utc_now()
        item.expires_at = base + timedelta(hours=max(0.0, hours))
        item.metadata["deferred_until"] = item.expires_at.isoformat()
        self._save()
        return True

    def mark_delivered(self, item_id: str) -> None:
        self.resolve(item_id, PulseStatus.DELIVERED)

    def clear_expired(self, *, lane: PulseLane | str | None = None) -> int:
        before = len(self._items)
        self._expire()
        lane_type = PulseLane.from_value(lane) if lane is not None else None
        self._items = [
            item
            for item in self._items
            if not (
                item.status == PulseStatus.EXPIRED
                and (lane_type is None or item.lane_type == lane_type)
            )
        ]
        removed = before - len(self._items)
        if removed:
            self._save()
        return removed

    # ──────────────────────────────────── Approval actions ───────────────────

    def execute_action(self, item: PulseItem) -> tuple[bool, str]:
        action = item.action or {}
        action_type = str(action.get("type") or "noop")
        if action_type == "noop":
            return True, "No action executed."
        if action_type == "append_lines":
            return self._execute_append_lines(action)
        if action_type == "append_text":
            return self._execute_append_text(action)
        if action_type == "composite":
            return self._execute_composite(action)
        if action_type == "restore_backup":
            return self._execute_restore_backup(action)
        if action_type in ("run_worker", "cortex_promote"):
            # Sync callers (dashboard, chat commands) must not block the event loop on a
            # potentially long reasoning run — enqueue it; the cognition drainer
            # runs it and posts a follow-up pulse with the artifact. A Cortex
            # promotion is a deep worker too (pulse_agent.run_pulse_worker_action).
            job_id = self._enqueue_worker_action(item)
            return (
                True,
                f"Working on it — I'll post the result to your Pulse when done. (job {job_id})",
            )
        return False, f"Unsupported PULSE action: {action_type}"

    async def execute_action_async(self, item: PulseItem) -> tuple[bool, str, Any]:
        """Async action execution. Runs ``run_worker`` actions inline (awaited);
        delegates every other action type to the sync :meth:`execute_action`.

        Returns ``(ok, message, WorkerOutcome | None)``.
        """
        action = item.action or {}
        if str(action.get("type")) in ("run_worker", "cortex_promote"):
            from olano.cognition.pulse_agent import run_pulse_worker_action

            root = self.workspace_root or Path.cwd()
            outcome, backup_job = await run_pulse_worker_action(self.config, root, action)
            if backup_job:
                item.metadata["backup_job_id"] = backup_job
            ok = outcome.status in ("completed", "empty")
            msg = outcome.summary or outcome.error or "Worker finished."
            return ok, msg, outcome
        ok, msg = self.execute_action(item)
        return ok, msg, None

    def _execute_restore_backup(self, action: dict[str, Any]) -> tuple[bool, str]:
        from olano.cognition.backups import restore

        job_id = str(action.get("job_id") or "")
        root = self.workspace_root
        if not job_id or root is None:
            return False, "restore_backup requires a job_id and a workspace."
        return restore(root, job_id)

    def _jobs_dir(self) -> Path:
        return self.state_dir / "state" / "cognition" / "jobs"

    def _enqueue_worker_action(self, item: PulseItem) -> str:
        """Persist a run_worker job descriptor for the background drainer."""
        jobs_dir = self._jobs_dir()
        jobs_dir.mkdir(parents=True, exist_ok=True)
        job_id = str(uuid.uuid4())[:12]
        descriptor = {
            "id": job_id,
            "agent": self.config.name,
            "item_id": item.id,
            "action": item.action,
            "status": "queued",
            "created_at": _utc_now().isoformat(),
        }
        (jobs_dir / f"{job_id}.json").write_text(json.dumps(descriptor, indent=2), encoding="utf-8")
        item.metadata["worker_job_id"] = job_id
        self._save()
        return job_id

    def undo(self, item_id: str) -> tuple[bool, str]:
        """Undo an autonomous artifact by firing the item's restore_backup action."""
        item = self.get_item(item_id)
        if item is None:
            return False, f"PULSE item {item_id} not found."
        if not item.action or str(item.action.get("type")) != "restore_backup":
            return False, f"No undo available for PULSE item {item_id}."
        ok, message = self._execute_restore_backup(item.action)
        if ok:
            item.metadata["undone_at"] = _utc_now().isoformat()
            self._save()
        return ok, message

    def _execute_composite(self, action: dict[str, Any]) -> tuple[bool, str]:
        results: list[str] = []
        for step in action.get("steps") or []:
            ok, message = self.execute_action(
                PulseItem(
                    id="action",
                    source="pulse",
                    type=PulseType.PROPOSAL,
                    priority=PulsePriority.NORMAL,
                    content="action",
                    action=step,
                )
            )
            if not ok:
                return False, message
            results.append(message)
        return True, "; ".join(results) if results else "Composite action executed."

    def _resolve_action_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            return path
        root = self.workspace_root or Path.cwd()
        return root / path

    def _execute_append_text(self, action: dict[str, Any]) -> tuple[bool, str]:
        raw_path = str(action.get("path") or "")
        text = str(action.get("text") or "")
        if not raw_path or not text:
            return False, "append_text action requires path and text."
        path = self._resolve_action_path(raw_path)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        separator = "" if not existing or existing.endswith("\n") else "\n"
        path.write_text(existing + separator + text, encoding="utf-8")
        return True, f"Updated {path.name}."

    @staticmethod
    def _line_tokens(text: str) -> set[str]:
        import re as _re

        # Drop bullet/markdown punctuation so "- Casual tone" ~ "Casual tone".
        return set(_re.findall(r"[a-z0-9]+", text.lower()))

    def _execute_append_lines(self, action: dict[str, Any]) -> tuple[bool, str]:
        raw_path = str(action.get("path") or "")
        lines = [str(line).strip() for line in action.get("lines") or [] if str(line).strip()]
        heading = str(action.get("heading") or "").strip()
        if not raw_path or not lines:
            return False, "append_lines action requires path and lines."

        path = self._resolve_action_path(raw_path)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""

        # Bullet-level dedup: skip any proposed line that is already present in
        # the file (exact or near-duplicate by token-set Jaccard ≥ 0.8). This is
        # the safety net that stops SOUL.md/AGENTS.md from accumulating reworded
        # repeats even if the model proposes them.
        existing_token_sets = [
            toks
            for raw in existing.splitlines()
            if (toks := self._line_tokens(raw)) and raw.strip().startswith(("-", "*", "1."))
        ]

        def _is_dup(line: str) -> bool:
            toks = self._line_tokens(line)
            if not toks:
                return True
            for prev in existing_token_sets:
                inter = len(toks & prev)
                union = len(toks | prev)
                if union and inter / union >= 0.8:
                    return True
            return False

        new_lines: list[str] = []
        for line in lines:
            if _is_dup(line):
                continue
            new_lines.append(line)
            # Guard against dupes *within* this same batch too.
            existing_token_sets.append(self._line_tokens(line))

        if not new_lines:
            return True, f"No new lines for {path.name} — all proposals already covered."

        blocks: list[str] = []
        if existing.strip():
            blocks.append(existing.rstrip())
        if heading and f"## {heading}" not in existing:
            blocks.append(f"## {heading}")
        blocks.extend(
            line if line.startswith(("- ", "* ", "1. ")) else f"- {line}" for line in new_lines
        )
        path.write_text("\n\n".join(blocks).rstrip() + "\n", encoding="utf-8")
        skipped = len(lines) - len(new_lines)
        suffix = f" ({skipped} duplicate(s) skipped)" if skipped else ""
        return True, f"Updated {path.name}{suffix}."

    # ──────────────────────────────────── Formatting ─────────────────────────

    # ──────────────────────────────────── Generators ─────────────────────────

    def generate_budget_alert(self, used_usd: float, limit_usd: float) -> PulseItem:
        pct = int(used_usd / limit_usd * 100) if limit_usd else 0
        return self.add(
            content=(
                f"Budget {pct}% used (${used_usd:.2f} / ${limit_usd:.2f}). "
                "Consider switching to a cheaper model."
            ),
            source="budget",
            type=PulseType.COST_ALERT,
            priority=PulsePriority.URGENT if pct >= 100 else PulsePriority.NORMAL,
            lane=PulseLane.OPS,
            title="Budget usage notice",
            expires_in_hours=24,
            metadata={"used_usd": used_usd, "limit_usd": limit_usd, "percent": pct},
            dedup_window_hours=12,
        )

    def generate_security_alert(self, description: str) -> PulseItem:
        return self.add(
            content=description,
            source="security",
            type=PulseType.ALERT,
            priority=PulsePriority.URGENT,
            lane=PulseLane.OPS,
            title="Security alert",
            expires_in_hours=12,
        )

    def generate_workspace_suggestion(self, description: str) -> PulseItem:
        return self.add(
            content=description,
            source="olano",
            type=PulseType.SUGGESTION,
            priority=PulsePriority.NORMAL,
            lane=PulseLane.PULSE,
        )

    # ──────────────────────────────────── Quiet hours ────────────────────────

    def _quiet_hours_window(self) -> tuple[str, str]:
        start = self.pulse_config.quiet_hours_start or self.preferences.quiet_hours_start
        end = self.pulse_config.quiet_hours_end or self.preferences.quiet_hours_end
        return start, end

    def _timezone(self) -> ZoneInfo:
        tz_name = self.preferences.timezone or "UTC"
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return ZoneInfo("UTC")

    def _is_quiet_hours(self) -> bool:
        start_text, end_text = self._quiet_hours_window()
        start = _parse_time_string(start_text)
        end = _parse_time_string(end_text)
        if start is None or end is None:
            return False

        now = datetime.now(self._timezone())
        current_minutes = now.hour * 60 + now.minute
        start_minutes = start[0] * 60 + start[1]
        end_minutes = end[0] * 60 + end[1]
        if start_minutes == end_minutes:
            return False
        if start_minutes < end_minutes:
            return start_minutes <= current_minutes < end_minutes
        return current_minutes >= start_minutes or current_minutes < end_minutes

    def _delivered_today_count(self) -> int:
        today = datetime.now(self._timezone()).date()
        count = 0
        for item in self._items:
            delivered_at = item.metadata.get("delivered_at")
            if not delivered_at:
                continue
            try:
                delivered = datetime.fromisoformat(str(delivered_at)).astimezone(self._timezone())
            except Exception:
                continue
            if delivered.date() == today:
                count += 1
        return count

    # ──────────────────────────────────── Persistence ────────────────────────

    def _expire(self) -> None:
        now = _utc_now()
        changed = False
        for item in self._items:
            if item.expires_at and now > item.expires_at and item.status == PulseStatus.PENDING:
                item.status = PulseStatus.EXPIRED
                changed = True
        if changed:
            self._save()

    def _save(self) -> None:
        try:
            self._pulse_dir.mkdir(parents=True, exist_ok=True)
            data = [item.to_dict() for item in self._items]
            self._queue_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("PULSE: failed to save queue: %s", exc)

    def _load(self) -> None:
        queue_path = self._queue_path
        if not queue_path.exists():
            queue_path = next(
                (path for path in self._legacy_queue_paths if path.exists()), queue_path
            )
        if not queue_path.exists():
            return
        try:
            data = json.loads(queue_path.read_text(encoding="utf-8"))
            self._items = [PulseItem.from_dict(item) for item in data]
        except Exception as exc:
            logger.warning("PULSE: failed to load queue: %s", exc)
