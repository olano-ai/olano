"""Fleet-wide Cortex review: read, discard, restore and promote any agent's items.

An agent's own ``cortex_*`` tools act on *its* index (the workspace bound to the
turn). An operator reviewing the fleet wants the opposite: "discard every
brainstorm Scout has", "promote Mira's pricing idea to a prototype", from one
chat, without opening each agent's Cortex view. This module is that surface,
for the dashboard-equivalent ``olano_cortex_*`` admin tools.

It is a view over the same store and the same engine, never a second one:

* items are read and written through :mod:`olano.cognition.cortex_items` in the
  **target** agent's workspace (``get_workspace(cfg.workspace)``, exactly what
  the dashboard's ``/api/cortex/items`` routes resolve);
* a promotion is a Cortex job in :data:`olano.cognition.jobs.REGISTRY` running
  :func:`olano.cognition.ideation.run_promote` under the target's own config,
  started by :func:`start_promotion_job`, which the dashboard's promote route
  calls too. So the item's card reads *Creating project...* while AgentFather
  waits on it, a second request (a click, a repeated tool call) joins the run
  instead of scaffolding a second project, and the result is audited in the
  target's log like any other promotion.

**Everything here is deliberately fleet-scoped**, like ``admin.scheduler``: the
callers are the ``owner_only`` admin tools. An agent reviewing its own items
keeps using ``cortex_tools``.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from olano.cognition import cortex_items as ci
from olano.cognition.audit import log_action

#: Detail markdown returned by :func:`get_item`, so one huge file cannot flood
#: the caller's context (the agent-side ``cortex_view`` uses the same cap).
_DETAIL_MAX_CHARS = 8000
_FILES_MAX = 40
_SUMMARY_MAX_CHARS = 240
#: Statuses a bulk discard leaves alone, with the reason it reports.
_NOT_DISCARDABLE = {
    "discarded": "already discarded",
    "promoted": "already promoted (its successor carries the work on)",
}
#: Kinds a bulk discard accepts as a filter; ``all`` is spelled out, never implied.
_ALL = "all"
#: What a listing shows when no status is asked for: the items still in play.
#: An ``exploring`` item is an active one a research run is enriching right
#: now, so it belongs with them rather than disappearing for the run's length.
DEFAULT_LIST_STATUS = "active"
_ACTIVE_STATUSES = frozenset({"active", "exploring"})


# ── resolution ───────────────────────────────────────────────────────────────


def _agent_workspace(agent: str) -> tuple[Any, Path]:
    """The target agent's config and workspace root, or ``ValueError``.

    No ``workspace.init()``: reading the items of an agent that has never
    booted must not provision its workspace. A missing index reads as empty.
    """
    from olano.config.loader import load_agent_config
    from olano.config.workspace import get_workspace

    name = (agent or "").strip()
    if not name:
        raise ValueError("An agent name is required.")
    try:
        cfg = load_agent_config(name)
    except FileNotFoundError as exc:
        raise ValueError(f"Agent '{name}' does not exist.") from exc
    return cfg, get_workspace(cfg.workspace).root


def _fleet_names() -> list[str]:
    from olano.config.loader import list_agents

    return list(list_agents())


def _live_job(agent: str, item_id: str) -> Any:
    """The promote/explore job running on this item right now, if any."""
    from olano.cognition.jobs import REGISTRY

    for kind in ("promote", "explore"):
        job = REGISTRY.find_live(agent, kind, item_id)
        if job is not None:
            return job
    return None


def _check_kind(kind: str, *, allow_all: bool = False) -> str:
    kind = (kind or "").strip().lower()
    if kind and kind not in ci.KINDS and not (allow_all and kind == _ALL):
        valid = ", ".join([*ci.KINDS, *([_ALL] if allow_all else [])])
        raise ValueError(f"Unknown kind '{kind}'. Valid kinds: {valid}.")
    return kind


def _row(root: Path, it: dict[str, Any]) -> dict[str, Any]:
    """The slice of an item a review needs: identity, state, score, one line."""
    summary = (it.get("summary") or "").strip() or ci.preview_for(root, it)
    summary = " ".join(summary.split())
    if len(summary) > _SUMMARY_MAX_CHARS:
        summary = summary[: _SUMMARY_MAX_CHARS - 1] + "…"
    row: dict[str, Any] = {
        "id": it.get("id"),
        "kind": it.get("kind"),
        "status": it.get("status"),
        "title": it.get("title"),
        "summary": summary,
        "source": it.get("source"),
        "created": str(it.get("created_at") or "")[:10],
        "updated": str(it.get("updated_at") or "")[:10],
        "path": it.get("path"),
    }
    if it.get("tags"):
        row["tags"] = it["tags"]
    score = ci.rating_score(it)
    if score is not None:
        rating = it.get("rating") or {}
        row["score"] = score
        row["recommendation"] = rating.get("recommendation") or "keep"
    links = it.get("links") or {}
    if links.get("promoted_to"):
        row["promoted_to"] = links["promoted_to"]
    return row


def _plan_progress(folder: Path) -> dict[str, int] | None:
    plan = folder / "PLAN.md"
    if not plan.is_file():
        return None
    try:
        text = plan.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    done = len(re.findall(r"(?mi)^\s*[-*]\s*\[x\]", text))
    todo = len(re.findall(r"(?mi)^\s*[-*]\s*\[ \]", text))
    if not done + todo:
        return None
    return {"done": done, "total": done + todo}


# ── reads ────────────────────────────────────────────────────────────────────


def _status_filter(status: str) -> frozenset[str] | None:
    """The statuses a listing keeps, ``None`` for every one.

    Empty means :data:`DEFAULT_LIST_STATUS`. ``active`` covers ``exploring``
    too (see the constant). Several may be combined with commas
    (``"promoted,discarded"``); ``all`` lists everything.
    """
    wanted = [s.strip() for s in (status or DEFAULT_LIST_STATUS).lower().split(",") if s.strip()]
    if not wanted:
        wanted = [DEFAULT_LIST_STATUS]
    if _ALL in wanted:
        return None
    keep: set[str] = set()
    for value in wanted:
        if value not in ci.STATUSES:
            valid = ", ".join([*ci.STATUSES, _ALL])
            raise ValueError(f"Unknown status '{value}'. Valid statuses: {valid}.")
        keep |= _ACTIVE_STATUSES if value == "active" else {value}
    return frozenset(keep)


def _list_one(
    agent: str,
    *,
    kind: str,
    statuses: frozenset[str] | None,
    min_score: float | None,
    max_score: float | None,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int]]:
    _cfg, root = _agent_workspace(agent)
    everything = ci.list_items(root, kind=kind or None, include_discarded=True)
    by_status = {st: 0 for st in ci.STATUSES}
    for it in everything:
        st = str(it.get("status") or "")
        by_status[st] = by_status.get(st, 0) + 1
    items = [it for it in everything if statuses is None or it.get("status") in statuses]
    if min_score is not None or max_score is not None:
        kept = []
        for it in items:
            score = ci.rating_score(it)
            if score is None:
                continue
            if min_score is not None and score < min_score:
                continue
            if max_score is not None and score > max_score:
                continue
            kept.append(it)
        items = kept
    return [_row(root, it) for it in items], ci.counts_by_kind(root), by_status


def list_items(
    agent: str = "",
    *,
    kind: str = "",
    status: str = DEFAULT_LIST_STATUS,
    min_score: float | None = None,
    max_score: float | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """One agent's Cortex items, or every agent's when ``agent`` is empty.

    Newest first. ``status`` defaults to the items still in play (``active``,
    which includes ``exploring``); pass ``discarded``, ``promoted``,
    ``exploring``, a comma-separated mix, or ``all``. ``min_score`` /
    ``max_score`` keep only *rated* items inside the range (an unrated item has
    no score to compare). ``counts`` are the active items per kind and
    ``counts_by_status`` every item per status (for the ``kind`` asked), both
    whatever the other filters, so a filtered page still says what else there
    is. In fleet mode each row carries ``agent``, and an agent whose index
    cannot be read is reported under ``errors`` rather than silently reading as
    having no items.
    """
    kind = _check_kind(kind)
    statuses = _status_filter(status)
    shown = sorted(statuses) if statuses is not None else [_ALL]
    limit = max(1, min(int(limit or 50), 500))

    def _one(name: str) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int]]:
        return _list_one(
            name, kind=kind, statuses=statuses, min_score=min_score, max_score=max_score
        )

    if (agent or "").strip():
        rows, counts, by_status = _one(agent.strip())
        return {
            "agent": agent.strip(),
            "status_filter": shown,
            "counts": counts,
            "counts_by_status": by_status,
            "matched": len(rows),
            "items": rows[:limit],
            "truncated": len(rows) > limit,
        }

    all_rows: list[dict[str, Any]] = []
    by_agent: dict[str, dict[str, int]] = {}
    fleet_by_status = {st: 0 for st in ci.STATUSES}
    errors: dict[str, str] = {}
    for name in _fleet_names():
        try:
            rows, counts, by_status = _one(name)
        except ci.CortexIndexError as exc:
            errors[name] = str(exc)
            continue
        except Exception as exc:  # an unloadable config must not hide the rest
            errors[name] = f"{type(exc).__name__}: {exc}"
            continue
        by_agent[name] = counts
        for st, n in by_status.items():
            fleet_by_status[st] = fleet_by_status.get(st, 0) + n
        for row in rows:
            all_rows.append({"agent": name, **row})
    all_rows.sort(key=lambda r: str(r.get("created") or ""), reverse=True)
    out: dict[str, Any] = {
        "agent": "",
        "status_filter": shown,
        "counts_by_agent": by_agent,
        "counts_by_status": fleet_by_status,
        "matched": len(all_rows),
        "items": all_rows[:limit],
        "truncated": len(all_rows) > limit,
    }
    if errors:
        out["errors"] = errors
    return out


def get_item(agent: str, item_id: str) -> dict[str, Any]:
    """One item in full: metadata, rating, links, detail markdown, files, and
    (for a project) PLAN.md progress. ``ValueError`` when it does not exist."""
    _cfg, root = _agent_workspace(agent)
    item = ci.get_item(root, (item_id or "").strip())
    if item is None:
        raise ValueError(f"Cortex item '{item_id}' not found for agent '{agent}'.")
    detail, files = ci.resolve_detail(root, item)
    detail = detail.strip()
    truncated = len(detail) > _DETAIL_MAX_CHARS
    if truncated:
        detail = detail[:_DETAIL_MAX_CHARS] + "\n… (truncated; read the file for the rest)"
    out: dict[str, Any] = {
        "agent": agent,
        "item": item,
        "detail": detail,
        "detail_truncated": truncated,
        "files": files[:_FILES_MAX],
    }
    if item.get("kind") == "project":
        progress = _plan_progress(root / str(item.get("path") or ""))
        if progress:
            out["plan_progress"] = progress
    job = _live_job(agent, str(item.get("id") or ""))
    if job is not None:
        out["running_job"] = {"id": job.id, "kind": job.kind, "title": job.title}
    return out


# ── writes ───────────────────────────────────────────────────────────────────


def discard_items(
    agent: str,
    *,
    item_ids: list[str] | None = None,
    kind: str = "",
    max_score: float | None = None,
    reason: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """Discard items of one agent: the named ``item_ids``, or every active item
    matching ``kind`` (``"all"`` for every kind), optionally only those rated at
    or below ``max_score``.

    One of ``item_ids`` / ``kind`` is required: an empty selector is refused,
    never read as "everything". An item that is already discarded or promoted,
    or that a promote/explore job is working on right now, is skipped with the
    reason. Discarding keeps the folder; :func:`restore_items` undoes it. One
    audit entry in the target's log records the batch and who asked.
    """
    _cfg, root = _agent_workspace(agent)
    ids = [str(i).strip() for i in (item_ids or []) if str(i).strip()]
    kind = _check_kind(kind, allow_all=True)
    if not ids and not kind:
        raise ValueError(
            "Name the items to discard (item_ids) or a kind filter "
            f"({', '.join(ci.KINDS)}, or '{_ALL}'). Nothing was discarded."
        )

    items = ci.list_items(root, include_discarded=True)
    by_id = {str(it.get("id")): it for it in items}
    not_found: list[str] = []
    pre_skipped: list[dict[str, Any]] = []
    if ids:
        candidates = []
        for item_id in dict.fromkeys(ids):
            it = by_id.get(item_id)
            if it is None:
                not_found.append(item_id)
            elif kind and kind != _ALL and it.get("kind") != kind:
                pre_skipped.append(
                    {
                        "id": item_id,
                        "title": it.get("title"),
                        "reason": f"is a {it.get('kind')}, not a {kind}",
                    }
                )
            else:
                candidates.append(it)
    else:
        candidates = [it for it in items if kind == _ALL or it.get("kind") == kind]

    chosen: list[str] = []
    skipped: list[dict[str, Any]] = pre_skipped
    for it in candidates:
        item_id = str(it.get("id"))
        why = _NOT_DISCARDABLE.get(str(it.get("status") or ""))
        if why is None and _live_job(agent, item_id) is not None:
            why = "a promote/explore run is working on it now"
        if why is None and max_score is not None:
            score = ci.rating_score(it)
            if score is None:
                why = "not rated yet"
            elif score > max_score:
                why = f"scored {score:g}, above {max_score:g}"
        if why is None:
            chosen.append(item_id)
        elif ids or why not in _NOT_DISCARDABLE.values():
            # A filter that skips the already-discarded is not news; a named
            # item, or one held back for another reason, is.
            skipped.append({"id": item_id, "title": it.get("title"), "reason": why})

    discarded = ci.set_statuses(root, chosen, "discarded")
    if discarded:
        log_action(
            root,
            "cortex_discard",
            summary=(
                f"Discarded {len(discarded)} item(s)"
                + (f" at {actor}'s request" if actor else "")
                + (f": {reason.strip()}" if reason.strip() else "")
            ),
            detail={
                "item_ids": [it.get("id") for it in discarded],
                "kinds": sorted({str(it.get("kind")) for it in discarded}),
                "reason": reason.strip(),
                "by": actor,
            },
        )
    return {
        "agent": agent,
        "discarded": [
            {"id": it.get("id"), "kind": it.get("kind"), "title": it.get("title")}
            for it in discarded
        ],
        "skipped": skipped,
        "not_found": not_found,
        "counts": ci.counts_by_kind(root),
    }


def restore_items(agent: str, item_ids: list[str], *, actor: str = "") -> dict[str, Any]:
    """Return discarded items to active. Items that are not discarded are
    reported, never changed (restoring a promoted item would orphan the stage
    it was promoted into)."""
    _cfg, root = _agent_workspace(agent)
    ids = list(dict.fromkeys(str(i).strip() for i in (item_ids or []) if str(i).strip()))
    if not ids:
        raise ValueError("Name the discarded items to restore (item_ids).")
    by_id = {str(it.get("id")): it for it in ci.list_items(root, include_discarded=True)}
    chosen: list[str] = []
    skipped: list[dict[str, Any]] = []
    not_found: list[str] = []
    for item_id in ids:
        it = by_id.get(item_id)
        if it is None:
            not_found.append(item_id)
        elif it.get("status") != "discarded":
            skipped.append(
                {"id": item_id, "title": it.get("title"), "reason": f"status is {it.get('status')}"}
            )
        else:
            chosen.append(item_id)
    restored = ci.set_statuses(root, chosen, "active")
    if restored:
        log_action(
            root,
            "cortex_restore",
            summary=f"Restored {len(restored)} item(s)"
            + (f" at {actor}'s request" if actor else ""),
            detail={"item_ids": [it.get("id") for it in restored], "by": actor},
        )
    return {
        "agent": agent,
        "restored": [
            {"id": it.get("id"), "kind": it.get("kind"), "title": it.get("title")}
            for it in restored
        ],
        "skipped": skipped,
        "not_found": not_found,
    }


# ── promotion ────────────────────────────────────────────────────────────────


def start_promotion_job(
    agent: str,
    cfg: Any,
    workspace_root: Path,
    item: dict[str, Any],
    target: str,
    *,
    requested_by: str = "",
) -> tuple[Any, bool]:
    """Start (or join) the Cortex job that promotes ``item`` to ``target``.

    ``target`` is ``idea`` / ``prototype`` / ``project``, or ``next`` for the
    funnel's next stage. Returns ``(job, started)`` from the job registry:
    ``started`` is ``False`` when a promotion of this item is already running,
    which the caller joins. The dashboard's promote route and
    :func:`promote_item` both start promotions here, so the two cannot disagree
    about what a promotion records. Needs a running event loop.
    """
    from olano.cognition.ideation import run_promote
    from olano.cognition.jobs import REGISTRY

    item_id = str(item.get("id") or "")

    async def _run(scope: Any) -> None:
        scope.note(
            f"Promoting {item.get('kind')} '{item.get('title')}' to {target}"
            + (f" (requested by {requested_by})" if requested_by else "")
            + ".",
            type="note",
        )
        outcome, created = await run_promote(
            cfg,
            workspace_root,
            item,
            target=None if target == "next" else target,
            source="manual",
        )
        if created is None:
            scope.complete(
                outcome=outcome.status or "error",
                error=outcome.error or outcome.summary or "Promotion failed",
                status="error",
            )
            return
        detail: dict[str, Any] = {
            "item_id": item_id,
            "from_kind": item.get("kind"),
            "to_kind": created.get("kind"),
            "target_id": created.get("id"),
            "path": created.get("path"),
        }
        if requested_by:
            detail["by"] = requested_by
        log_action(
            workspace_root,
            "cortex_promote",
            summary=f"Promoted to {created.get('kind')}: {created.get('title')}",
            detail=detail,
        )
        scope.complete(
            outcome=outcome.status,
            summary=outcome.summary or "",
            result={
                "ok": True,
                # `project` is the historical key the dashboard reads; it is
                # the created item whatever its kind. `created` says which.
                "project": created,
                "created": created,
                "target": created.get("kind"),
                "summary": outcome.summary,
                "artifacts": outcome.artifacts,
            },
            status="done",
        )

    job_detail: dict[str, Any] = {
        "item_kind": item.get("kind"),
        "item_title": item.get("title"),
        "to": target,
    }
    if requested_by:
        job_detail["requested_by"] = requested_by
    return REGISTRY.start(
        agent=agent,
        workspace_root=workspace_root,
        kind="promote",
        target=item_id,
        title=(
            f"Create project: {item.get('title') or item_id}"
            if target == "project"
            else f"Promote to {target}: {item.get('title') or item_id}"
        ),
        detail=job_detail,
        run=_run,
    )


async def promote_item(
    agent: str,
    item_id: str,
    *,
    target: str = "",
    wait: bool = True,
    actor: str = "",
) -> dict[str, Any]:
    """Promote one of ``agent``'s items down the funnel, as the dashboard does.

    ``target`` empty means the next stage (brainstorm → idea → prototype →
    project); ``project`` is reachable from any stage. The deep worker runs
    under the **target agent's** config (its models, its budget, its
    workspace and sandbox), never the caller's. ``wait=False`` answers at once
    with the job; otherwise this waits for the result, shielded so a caller that
    stops waiting does not stop the run.
    """
    cfg, root = _agent_workspace(agent)
    item_id = (item_id or "").strip()
    item = ci.get_item(root, item_id)
    if item is None:
        raise ValueError(f"Cortex item '{item_id}' not found for agent '{agent}'.")
    kind = str(item.get("kind") or "")
    wanted = (target or "").strip().lower()
    if wanted in ("", "next"):
        resolved = ci.next_kind(kind)
    elif wanted in ci.FUNNEL[1:]:
        resolved = wanted
    else:
        raise ValueError(
            f"Unknown target '{target}'. Use idea, prototype or project, or leave it "
            "empty for the next stage."
        )
    if kind == "project" or resolved is None:
        raise ValueError(f"'{item.get('title')}' is already a project, the end of the funnel.")
    if ci.FUNNEL.index(resolved) <= ci.FUNNEL.index(kind):
        raise ValueError(
            f"Cannot promote a {kind} to a {resolved}: the funnel only moves forward "
            f"({' → '.join(ci.FUNNEL)})."
        )

    from olano.cognition.jobs import REGISTRY

    live = REGISTRY.find_live(agent, "promote", item_id)
    if live is None:
        if item.get("status") == "promoted":
            dest = (item.get("links") or {}).get("promoted_to") or "its next stage"
            raise ValueError(f"'{item.get('title')}' was already promoted to {dest}.")
        if item.get("status") == "discarded":
            raise ValueError(
                f"'{item.get('title')}' is discarded. Restore it first if it should move on."
            )

    job, started = start_promotion_job(agent, cfg, root, item, resolved, requested_by=actor)
    base = {
        "agent": agent,
        "item_id": item_id,
        "from_kind": kind,
        "to_kind": resolved,
        "job_id": job.id,
        "joined_running_job": not started,
    }
    if not wait:
        return {
            **base,
            "status": job.status,
            "note": "Running in the background; the item's Cortex card shows its progress.",
        }
    task = job.task
    if task is not None:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # The job was stopped (its task is done; the answer below says so),
            # or the caller was cancelled, which must keep propagating.
            if not task.done():
                raise
    if job.status != "done":
        return {**base, "status": job.status, "error": job.error or job.summary or job.status}
    created = (job.result or {}).get("created") or {}
    return {
        **base,
        "status": "done",
        "created": {
            "id": created.get("id"),
            "kind": created.get("kind"),
            "title": created.get("title"),
            "path": created.get("path"),
        },
        "summary": job.summary,
    }
