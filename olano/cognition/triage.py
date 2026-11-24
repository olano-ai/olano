"""Rate & Promote: the funnel's review step.

One deep worker rates every active brainstorm, idea, prototype and project
(:data:`~olano.cognition.playbooks.RATE_ITEMS_PLAYBOOK`), the ratings are
written onto the items, and then the decisions are made **in code**, not by
the model:

- Under Fully Autonomous (``auto_approve_enabled``), an item the worker
  recommends promoting at or above ``promote_threshold`` moves one stage down
  the funnel (brainstorm → idea → prototype → project) through
  :func:`~olano.cognition.ideation.run_promote`, at most
  ``max_promotions_per_run`` per run and never past the daily deep-run cap
  (each promotion is a deep worker). An item it recommends discarding at or
  below :data:`~olano.config.cortex_funnel.DISCARD_SCORE_MAX` is discarded
  when ``auto_discard`` is on: the folder stays and ``cortex_restore`` brings
  it back.
- With Fully Autonomous off, nothing moves: each promotion candidate becomes
  one APPROVAL-lane Pulse card carrying a ``cortex_promote`` action, so a
  click does exactly what the sweep would have done.

Ratings are always written; they are advisory until the posture says
otherwise, and the Advance Work phase reads them to pick what to execute.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from olano.cognition import cortex_items
from olano.cognition.audit import log_action
from olano.cognition.guidance import prepend_guidance
from olano.cognition.playbooks import RATE_ITEMS_PLAYBOOK
from olano.cognition.worker import WorkerOutcome, run_worker
from olano.config.cortex_funnel import DISCARD_SCORE_MAX
from olano.config.workspace import WORKSPACE_STATE_DIRNAME

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

logger = logging.getLogger(__name__)

#: Items one triage run rates. The worker reads each folder; past this the
#: run's step budget, not the portfolio, is what gives out.
MAX_ITEMS_PER_RUN = 40

#: How many days with no change before an unrated item is called stale in the
#: context handed to the worker (it decides what that means for momentum).
STALE_AFTER_DAYS = 14


def _deep_model(config: AgentConfig) -> str:
    from olano.cognition.router import effective_deep_model

    return effective_deep_model(config.cognition, config.model)


def _plan_progress(workspace_root: Path, item: dict[str, Any]) -> str:
    plan = workspace_root / str(item.get("path") or "") / "PLAN.md"
    if not plan.is_file():
        return ""
    try:
        text = plan.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    done = len(re.findall(r"(?mi)^\s*[-*]\s*\[x\]", text))
    todo = len(re.findall(r"(?mi)^\s*[-*]\s*\[ \]", text))
    return f"plan {done}/{done + todo} done" if done + todo else ""


def _render_item(workspace_root: Path, item: dict[str, Any]) -> str:
    """One item as the worker sees it: identity, state, rating, detail head."""
    head = ""
    detail_file = item.get("detail_file") or ""
    path = workspace_root / detail_file if detail_file else None
    if path is not None and path.is_file():
        try:
            head = " ".join(path.read_text(encoding="utf-8", errors="replace").split())[:600]
        except OSError:
            head = ""
    folder = workspace_root / str(item.get("path") or "")
    files = 0
    if folder.is_dir():
        files = sum(1 for p in folder.rglob("*") if p.is_file())
    bits = [
        f"- id: {item.get('id')} | kind: {item.get('kind')} | status: {item.get('status')}",
        f"  title: {item.get('title')}",
        f"  folder: {item.get('path')}/ ({files} file(s)); updated {str(item.get('updated_at') or '')[:10]}",
    ]
    if item.get("summary"):
        bits.append(f"  summary: {str(item.get('summary')).strip()[:300]}")
    progress = _plan_progress(workspace_root, item) if item.get("kind") == "project" else ""
    if progress:
        bits.append(f"  {progress}")
    rating = item.get("rating") if isinstance(item.get("rating"), dict) else None
    if rating:
        bits.append(
            f"  previous rating: {rating.get('score')}/10, {rating.get('recommendation')} "
            f"({str(rating.get('rated_at') or '')[:10]})"
        )
    links = item.get("links") or {}
    if links.get("from_path"):
        bits.append(f"  promoted from: {links['from_path']}")
    if head:
        bits.append(f"  detail: {head}")
    return "\n".join(bits)


def _parse_ratings(summary: str) -> list[dict[str, Any]]:
    from olano.cognition.ideation import _parse_json

    parsed = _parse_json(summary)
    if isinstance(parsed, dict):
        for key in ("ratings", "items", "results"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        else:
            parsed = [parsed] if "id" in parsed else []
    if not isinstance(parsed, list):
        return []
    return [r for r in parsed if isinstance(r, dict) and r.get("id")]


def active_items(workspace_root: Path) -> list[dict[str, Any]]:
    return [
        it
        for it in cortex_items.list_items(workspace_root, include_discarded=False)
        if it.get("kind") in cortex_items.KINDS and it.get("status") in ("active", "exploring")
    ]


async def run_triage(
    config: AgentConfig, workspace_root: Path, *, budget_check: bool = True
) -> tuple[WorkerOutcome, int, str]:
    """Rate every active item, then promote / discard / queue per the posture.

    Returns ``(outcome, rated_count, "")`` in the shape the other phase runners
    use. ``outcome.summary`` names what was rated, promoted, discarded or queued;
    ``outcome.artifacts`` lists the folders of items the run created.
    """
    items = active_items(workspace_root)[:MAX_ITEMS_PER_RUN]
    if not items:
        return WorkerOutcome(status="empty", summary="No active items to rate."), 0, ""

    from olano.cognition.ideation import _team_context

    team = _team_context(config)
    context = prepend_guidance(
        workspace_root,
        "Active items to rate (use the ids exactly as given):\n"
        + "\n".join(_render_item(workspace_root, it) for it in items)
        + (f"\n\n{team}" if team else ""),
    )
    outcome = await run_worker(
        parent_config=config,
        workspace_root=workspace_root,
        playbook=RATE_ITEMS_PLAYBOOK,
        goal=(
            f"Rate the {len(items)} active Cortex item(s) in your CONTEXT (read their folders "
            "as needed) and recommend what should happen to each. Return ONLY the JSON array."
        ),
        context=context,
        tools_allow=["read_file", "ls", "memory_search"],
        model=_deep_model(config),
        budget_check=budget_check,
        worker_label="triage",
    )
    if outcome.status not in ("completed", "empty"):
        return outcome, 0, ""

    by_id = {str(it.get("id")): it for it in items}
    rated = 0
    for raw in _parse_ratings(outcome.summary):
        item_id = str(raw.get("id") or "").strip()
        if item_id not in by_id:
            continue
        updated = cortex_items.set_rating(
            workspace_root,
            item_id,
            raw.get("score", 0),
            rationale=str(raw.get("rationale") or ""),
            dimensions=raw.get("dimensions") if isinstance(raw.get("dimensions"), dict) else None,
            recommendation=str(raw.get("recommendation") or "keep"),
            next_step=str(raw.get("next_step") or ""),
            rated_by="cortex",
        )
        if updated is not None:
            by_id[item_id] = updated
            rated += 1
        dup_of = str(raw.get("duplicate_of") or "").strip()
        if dup_of and dup_of != item_id and dup_of in by_id:
            linked = cortex_items.update_item(
                workspace_root,
                item_id,
                links={**(by_id[item_id].get("links") or {}), "duplicate_of": dup_of},
            )
            if linked is not None:
                by_id[item_id] = linked
    if rated:
        log_action(
            workspace_root,
            "cortex_rate",
            summary=f"Rated {rated} Cortex item(s)",
            detail={
                "items": [
                    {
                        "id": it.get("id"),
                        "kind": it.get("kind"),
                        "score": (it.get("rating") or {}).get("score"),
                        "recommendation": (it.get("rating") or {}).get("recommendation"),
                    }
                    for it in by_id.values()
                    if isinstance(it.get("rating"), dict)
                ]
            },
        )

    decisions, artifacts = await apply_decisions(config, workspace_root, list(by_id.values()))
    parts = [f"Rated {rated} item(s)."] if rated else ["The reviewer returned no ratings."]
    parts.extend(decisions)
    outcome.summary = " ".join(parts)
    outcome.artifacts = artifacts
    return outcome, rated, ""


def _rating_of(item: dict[str, Any]) -> dict[str, Any]:
    """The item's rating block, or an empty dict when it has none."""
    rating = item.get("rating")
    return dict(rating) if isinstance(rating, dict) else {}


def _promotion_candidates(items: list[dict[str, Any]], threshold: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in items:
        rating = it.get("rating") if isinstance(it.get("rating"), dict) else None
        if not rating or it.get("kind") == "project" or it.get("status") != "active":
            continue
        score = cortex_items.rating_score(it)
        if rating.get("recommendation") == "promote" and score is not None and score >= threshold:
            out.append(it)
    out.sort(key=lambda it: -(cortex_items.rating_score(it) or 0.0))
    return out


def _discard_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in items:
        rating = it.get("rating") if isinstance(it.get("rating"), dict) else None
        if not rating or it.get("status") != "active":
            continue
        score = cortex_items.rating_score(it)
        if (
            rating.get("recommendation") == "discard"
            and score is not None
            and score <= DISCARD_SCORE_MAX
        ):
            out.append(it)
    return out


async def apply_decisions(
    config: AgentConfig, workspace_root: Path, items: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Promote / discard / queue according to the ratings and the posture.

    Returns ``(summary_parts, artifacts)``. Pure function of the ratings on the
    items passed in (nothing is re-read), so the caller decides what counts.
    """
    from olano.config.system_settings import (
        auto_approve_enabled,
        auto_discard_enabled,
        effective_max_promotions_per_run,
        effective_promote_threshold,
    )

    fully = auto_approve_enabled(config)
    threshold = effective_promote_threshold(config)
    candidates = _promotion_candidates(items, threshold)
    parts: list[str] = []
    artifacts: list[str] = []

    if fully:
        budget = effective_max_promotions_per_run(config)
        promoted: list[str] = []
        for item in candidates[:budget]:
            if not _deep_run_allowed(config, workspace_root):
                parts.append("Further promotions wait for tomorrow's deep-run cap.")
                break
            created = await promote_autonomously(config, workspace_root, item)
            if created is not None:
                promoted.append(f"{item.get('title')} → {created.get('kind')}")
                artifacts.append(str(created.get("path") or ""))
        if promoted:
            parts.append("Promoted: " + "; ".join(promoted) + ".")
        skipped = len(candidates) - min(len(candidates), budget)
        if skipped > 0:
            parts.append(f"{skipped} more promotion candidate(s) wait for the next run.")
        if auto_discard_enabled(config):
            discarded = [
                discard_autonomously(workspace_root, it) for it in _discard_candidates(items)
            ]
            discarded = [d for d in discarded if d]
            if discarded:
                parts.append("Discarded (reversible): " + "; ".join(discarded) + ".")
    elif candidates:
        queued = [queue_promotion_approval(config, workspace_root, it) for it in candidates]
        n = sum(1 for q in queued if q)
        if n:
            parts.append(f"{n} promotion(s) wait for your approval in Cortex → Self-Improve.")
    return parts, [a for a in artifacts if a]


def _deep_run_allowed(config: AgentConfig, workspace_root: Path) -> bool:
    """A promotion is a deep run: it counts against the daily cap like a phase,
    and records itself the same way (before running, so a crash still counts)."""
    from olano.cognition.orchestrator import deep_gate
    from olano.cognition.router import record_deep_run

    ok, reason = deep_gate(config, workspace_root, force=False)
    if not ok:
        logger.info("cortex triage: promotion skipped (%s)", reason)
        return False
    record_deep_run(workspace_root)
    return True


async def promote_autonomously(
    config: AgentConfig, workspace_root: Path, item: dict[str, Any]
) -> dict[str, Any] | None:
    from olano.cognition.ideation import run_promote

    target = cortex_items.next_kind(str(item.get("kind") or ""))
    try:
        outcome, created = await run_promote(
            config, workspace_root, item, target=target, source="autonomous", budget_check=False
        )
    except Exception as exc:  # one failed promotion must not end the phase
        logger.exception("cortex triage: promotion of %s failed", item.get("id"))
        outcome, created = WorkerOutcome(status="error", error=str(exc)), None
    rating = _rating_of(item)
    log_action(
        workspace_root,
        "cortex_promote",
        summary=(
            f"Promoted {item.get('kind')} to {created.get('kind')}: {created.get('title')}"
            if created
            else f"Promotion of {item.get('kind')} '{item.get('title')}' failed"
        ),
        detail={
            "item_id": item.get("id"),
            "from_kind": item.get("kind"),
            "to_kind": created.get("kind") if created else target,
            "target_id": created.get("id") if created else None,
            "path": created.get("path") if created else None,
            "score": rating.get("score"),
            "autonomous": True,
            "error": None if created else (outcome.error or outcome.status),
        },
    )
    return created


def discard_autonomously(workspace_root: Path, item: dict[str, Any]) -> str:
    rating = _rating_of(item)
    updated = cortex_items.set_status(workspace_root, str(item.get("id") or ""), "discarded")
    if updated is None:
        return ""
    log_action(
        workspace_root,
        "cortex_discard",
        summary=f"Discarded {item.get('kind')}: {item.get('title')}",
        detail={
            "item_id": item.get("id"),
            "kind": item.get("kind"),
            "reason": rating.get("rationale") or "rated as not worth keeping",
            "score": rating.get("score"),
            "autonomous": True,
        },
    )
    score = cortex_items.rating_score(item)
    return f"{item.get('title')} ({score:g}/10)" if score is not None else str(item.get("title"))


def queue_promotion_approval(
    config: AgentConfig, workspace_root: Path, item: dict[str, Any]
) -> bool:
    """One APPROVAL-lane card per candidate, deduped per item for a week."""
    try:
        from olano.pulse.engine import PulseEngine
        from olano.pulse.models import PulseLane, PulsePriority, PulseType

        target = cortex_items.next_kind(str(item.get("kind") or "")) or "project"
        rating = _rating_of(item)
        score = cortex_items.rating_score(item)
        score_text = f"{score:g}" if score is not None else "?"
        engine = PulseEngine(config, workspace_root / WORKSPACE_STATE_DIRNAME)
        engine.add(
            content=(
                f"Rated {score_text}/10. {rating.get('rationale') or ''}".strip()
                + (f" Next: {rating.get('next_step')}" if rating.get("next_step") else "")
            ),
            source="cognition",
            lane=PulseLane.APPROVAL,
            type=PulseType.PROPOSAL,
            priority=PulsePriority.NORMAL,
            title=f"Promote {item.get('kind')} to {target}: {item.get('title')}",
            why_now="Cortex rated this item ready for its next stage.",
            suggested_next_step=f"Approve to promote it to a {target}; reject to keep it as is.",
            references=[str(item.get("path") or "")],
            action={"type": "cortex_promote", "item_id": str(item.get("id")), "target": target},
            metadata={"kind": "cortex_promotion", "item_id": str(item.get("id")), "target": target},
            dedup_window_hours=24.0 * 7,
        )
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("cortex triage: could not queue approval for %s: %s", item.get("id"), exc)
        return False
