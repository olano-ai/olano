"""Autonomous ideation runners — brainstorming, ideas, prototyping, self-checks,
plus per-item exploration and promotion to projects.

Each idea/brainstorm/prototype is materialized as its own **folder** tracked in
the Cortex item index (:mod:`olano.cognition.cortex_items`), not appended to an
ever-growing flat file. Generation workers return structured JSON; the runner
writes the detail file + registers the item (so the index stays trustworthy).
Enrichment workers (explore / promote) operate on an existing item folder and may
write any supporting files they want.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from olano.cognition import cortex_items
from olano.cognition.backups import snapshot_backup
from olano.cognition.guidance import prepend_guidance
from olano.cognition.playbooks import (
    ADVANCE_ITEMS_PLAYBOOK,
    BRAINSTORM_PLAYBOOK,
    BRAINSTORM_TO_IDEA_PLAYBOOK,
    EXECUTE_ITEMS_ADDENDUM,
    IDEA_EXPLORE_PLAYBOOK,
    IDEA_GENERATION_PLAYBOOK,
    PROJECT_SCAFFOLD_PLAYBOOK,
    PROTOTYPE_PLAYBOOK,
    SELF_CHECK_PLAYBOOK,
)
from olano.cognition.worker import WorkerOutcome, run_worker
from olano.config.workspace import WORKSPACE_STATE_DIRNAME

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

logger = logging.getLogger(__name__)


def _deep_model(config: AgentConfig) -> str:
    from olano.cognition.router import effective_deep_model

    return effective_deep_model(config.cognition, config.model)


def _parse_json(text: str) -> Any:
    """Pull the first JSON value (object or array) out of a worker's summary.

    Robust to the common worker quirks: a ```json code fence, a prose preamble
    before the JSON, and a trailing ``ARTIFACTS:`` line. Tries a clean parse
    first, then the largest balanced array *and* object, returning whichever
    actually parses (preferring the array, since most idea/brainstorm playbooks
    ask for one)."""
    if not text:
        return None
    # Drop a ```json … ``` fence if present.
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    candidates = [text]
    if fenced:
        candidates.insert(0, fenced.group(1))
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
    # Fall back to extracting the largest array, then the largest object.
    for pattern in (r"\[.*\]", r"\{.*\}"):
        match = re.search(pattern, text, re.DOTALL)
        if not match:
            continue
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            continue
    return None


def _short_title(text: str, *, limit: int = 80) -> str:
    """A compact title from a longer sentence/idea blob (first clause or N chars)."""
    text = " ".join((text or "").split())
    if not text:
        return ""
    for sep in (". ", "; ", " — ", " - ", ": "):
        if sep in text[:limit]:
            head = text.split(sep, 1)[0].strip()
            if 8 <= len(head) <= limit:
                return head
    return text[:limit].rstrip(" ,.;:—-") + ("…" if len(text) > limit else "")


def _coerce_item(obj: Any) -> dict[str, Any] | None:
    """Normalize a model's idea/brainstorm object into ``{title, summary, detail,
    tags}``, tolerating renamed schema keys.

    Generation models routinely paraphrase the requested schema (e.g. return
    ``idea``/``why_now``/``next_action`` instead of ``title``/``summary``/
    ``detail``). Rather than silently drop such an item, pull from the common
    synonyms and synthesize the missing title/summary/detail so a usable idea is
    always captured. Returns ``None`` only for a genuinely empty object."""
    if not isinstance(obj, dict):
        return None

    def first(*keys: str) -> str:
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    title = first("title", "name", "headline", "topic", "heading")
    summary = first("summary", "content", "description", "tagline", "one_liner", "overview")
    detail = first("detail", "details", "body", "notes", "markdown", "text")
    # Some models pack the whole idea into one verbose field instead of title/summary.
    idea_text = first("idea", "concept", "proposal", "suggestion")
    why = first("why_now", "why", "rationale", "reason", "motivation")
    next_action = first(
        "next_action", "next_step", "suggested_next_step", "action", "first_step", "next"
    )

    if not summary:
        summary = idea_text or title
    if not title:
        title = _short_title(summary or idea_text)
    if not detail:
        parts = []
        if idea_text and idea_text != summary:
            parts.append(idea_text)
        if why:
            parts.append(f"**Why now:** {why}")
        if next_action:
            parts.append(f"**Next step:** {next_action}")
        detail = "\n\n".join(parts)

    if not title or not summary:
        return None
    tags = obj.get("tags") if isinstance(obj.get("tags"), list) else None
    return {"title": title, "summary": summary, "detail": detail, "tags": tags}


def _as_item_list(parsed: Any) -> list[Any]:
    """Coerce a parsed JSON value into a list of item objects.

    Handles the bare array (expected), a single object (wrap it), and the common
    ``{"ideas": [...]}`` / ``{"items": [...]}`` wrapper that models emit despite
    being asked for a bare array."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("ideas", "items", "results", "data", "list", "suggestions"):
            val = parsed.get(key)
            if isinstance(val, list):
                return val
        # A single idea object returned directly.
        if any(k in parsed for k in ("title", "name", "idea", "summary", "concept")):
            return [parsed]
    return []


def _active_titles(workspace_root: Path, kind: str, limit: int = 40) -> str:
    """Compact list of existing active item titles (dup-avoidance context)."""
    items = cortex_items.list_items(workspace_root, kind=kind, include_discarded=False)
    titles = [i.get("title", "") for i in items[:limit] if i.get("title")]
    if not titles:
        return "(none captured yet)"
    return "\n".join(f"- {t}" for t in titles)


def history_digest(config: AgentConfig, workspace_root: Path, *, max_each: int = 25) -> str:
    """Summarize previously approved/rejected pulse items so creative phases avoid
    regenerating things the user already acted on."""
    try:
        from olano.pulse.engine import PulseEngine
        from olano.pulse.models import PulseLane, PulseStatus

        engine = PulseEngine(config, workspace_root / WORKSPACE_STATE_DIRNAME)
        approved: list[str] = []
        rejected: list[str] = []
        for lane in (PulseLane.PULSE, PulseLane.APPROVAL):
            for item in engine.list_by_status(PulseStatus.APPROVED, lane=lane, max_items=max_each):
                t = (item.title or item.content or "").strip().split("\n")[0][:120]
                if t:
                    approved.append(t)
            for item in engine.list_by_status(PulseStatus.REJECTED, lane=lane, max_items=max_each):
                t = (item.title or item.content or "").strip().split("\n")[0][:120]
                if t:
                    rejected.append(t)
        lines: list[str] = []
        if rejected:
            lines.append("REJECTED before (do NOT repeat):")
            lines += [f"  - {t}" for t in rejected[:max_each]]
        if approved:
            lines.append("APPROVED/engaged before (find NEW angles):")
            lines += [f"  - {t}" for t in approved[:max_each]]
        return "\n".join(lines) if lines else "(no prior approved/rejected items yet)"
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("history_digest failed: %s", exc)
        return "(history unavailable)"


# ───────────────────────────── generation runners ───────────────────────────


async def run_brainstorm(
    config: AgentConfig, workspace_root: Path, *, budget_check: bool = True
) -> tuple[WorkerOutcome, int, str]:
    """Run one brainstorm session → a brainstorming/<slug>/ folder + index entry."""
    context = prepend_guidance(
        workspace_root,
        f"{cortex_items.portfolio_digest(workspace_root)}\n\n"
        f"{history_digest(config, workspace_root)}",
    )
    outcome = await run_worker(
        parent_config=config,
        workspace_root=workspace_root,
        playbook=BRAINSTORM_PLAYBOOK,
        goal=(
            "Run one focused brainstorm session grounded in the user's identity, "
            "memory and live web research. Return ONLY the JSON object."
        ),
        context=context,
        tools_allow=["read_file", "ls", "web_search", "web_fetch", "memory_search"],
        model=_deep_model(config),
        budget_check=budget_check,
        worker_label="brainstorm",
    )
    if outcome.status not in ("completed", "empty"):
        return outcome, 0, ""
    items = _as_item_list(_parse_json(outcome.summary))
    item = _coerce_item(items[0]) if items else None
    if item is None:
        return outcome, 0, ""
    verdict = dedup_verdict(workspace_root, "brainstorm", item["title"], item["summary"])
    if verdict.skip:
        outcome.status = "empty"
        outcome.summary = f"No new brainstorm: {verdict.reason}"
        return outcome, 0, ""
    _it, created = cortex_items.upsert_item(
        workspace_root,
        kind="brainstorm",
        title=item["title"],
        summary=item["summary"],
        detail=item["detail"],
        tags=item["tags"],
        source="autonomous",
        existing=verdict.improve,
    )
    outcome.summary = item["summary"] or item["title"]
    if not created:
        outcome.summary = f"Improved existing brainstorm: {_it.get('title')}"
    return outcome, (1 if created else 0), ""


async def run_idea_generation(
    config: AgentConfig, workspace_root: Path, *, budget_check: bool = True
) -> tuple[WorkerOutcome, int, str]:
    """Distil concrete ideas → one ideas/<slug>/ folder + index entry each."""
    context = prepend_guidance(
        workspace_root,
        f"{cortex_items.portfolio_digest(workspace_root)}\n\n"
        f"{history_digest(config, workspace_root)}",
    )
    outcome = await run_worker(
        parent_config=config,
        workspace_root=workspace_root,
        playbook=IDEA_GENERATION_PLAYBOOK,
        goal=(
            "Distil 2-4 genuinely new, concrete, actionable ideas from the latest "
            "brainstorming + the user's context. Return ONLY the JSON array."
        ),
        context=context,
        tools_allow=["read_file", "ls", "web_search", "web_fetch", "memory_search"],
        model=_deep_model(config),
        budget_check=budget_check,
        worker_label="idea_generation",
    )
    if outcome.status not in ("completed", "empty"):
        return outcome, 0, ""
    arr = _as_item_list(_parse_json(outcome.summary))
    if not arr:
        return outcome, 0, ""
    added = 0
    improved = 0
    skipped: list[str] = []
    titles: list[str] = []
    accepted: list[dict[str, Any]] = []
    for raw in arr:
        item = _coerce_item(raw)
        if item is None:
            continue
        # Against the whole portfolio AND against the ideas accepted a moment
        # ago in this same batch: a model asked for 2-4 ideas can hand back
        # the same one twice in different words.
        verdict = dedup_verdict(workspace_root, "idea", item["title"], item["summary"])
        if verdict.skip:
            skipped.append(f"{item['title']} ({verdict.reason})")
            continue
        if any(
            cortex_items.content_similarity(
                item["title"], item["summary"], a["title"], a["summary"]
            )
            >= cortex_items.SIMILAR_CONTENT_THRESHOLD
            for a in accepted
        ):
            skipped.append(f"{item['title']} (repeats another idea in this batch)")
            continue
        _it, created = cortex_items.upsert_item(
            workspace_root,
            kind="idea",
            title=item["title"],
            summary=item["summary"],
            detail=item["detail"],
            tags=item["tags"],
            source="autonomous",
            existing=verdict.improve,
        )
        accepted.append(item)
        if created:
            added += 1
            titles.append(item["title"])
        else:
            improved += 1
    if added:
        outcome.summary = f"Captured {added} idea(s): {', '.join(titles)}"
        if improved:
            outcome.summary += f" (improved {improved} existing)"
    elif improved:
        outcome.summary = f"No new ideas; improved {improved} existing instead."
    else:
        outcome.summary = "No new ideas."
    if skipped:
        outcome.summary += f" Skipped {len(skipped)} near-duplicate(s): " + "; ".join(skipped)
    return outcome, added, ""


@dataclass
class DedupVerdict:
    """What to do with a generated candidate: add it, improve ``improve``
    (a near-identical ACTIVE item of the same kind), or skip it with
    ``reason`` (it repeats an item of another kind, a promoted one, or a
    discarded one)."""

    skip: bool = False
    reason: str = ""
    improve: dict[str, Any] | None = None


def dedup_verdict(workspace_root: Path, kind: str, title: str, summary: str) -> DedupVerdict:
    """Decide a candidate against the WHOLE portfolio (every kind, every status).

    Same kind and active ⇒ improve it rather than add a near-copy (the old
    title-only rule, now on content). Anything else that matches, a project
    the idea restates, a prototype already built from it, a brainstorm the
    owner discarded, ⇒ skip: it exists, further along or rejected.
    """
    match = cortex_items.find_similar_any(workspace_root, title, summary)
    if match is None:
        return DedupVerdict()
    existing, score = match
    e_kind = str(existing.get("kind") or "")
    e_status = str(existing.get("status") or "")
    label = f"{e_kind} '{existing.get('title')}'"
    if e_kind == kind and e_status in ("active", "exploring"):
        return DedupVerdict(improve=existing)
    if e_status == "discarded":
        reason = f"restates the discarded {label}"
    elif e_status == "promoted":
        stage = (existing.get("links") or {}).get("promoted_to_kind") or "later stage"
        reason = f"restates {label}, already promoted to a {stage}"
    else:
        reason = f"restates the existing {label}"
    logger.info("cortex dedup: skipping '%s' (%.2f): %s", title, score, reason)
    return DedupVerdict(skip=True, reason=reason)


def _proto_dirs(workspace_root: Path) -> set[str]:
    p = workspace_root / "prototypes"
    return {d.name for d in p.iterdir() if d.is_dir()} if p.is_dir() else set()


async def run_prototype(
    config: AgentConfig, workspace_root: Path, *, budget_check: bool = True
) -> tuple[WorkerOutcome, int, str]:
    """Scaffold a runnable prototype under prototypes/<slug>/ and register it.

    The worker creates the folder + files itself (it may run safe_shell). We diff
    the prototypes/ dirs to find what it built and register each as a Cortex item.
    """
    before = _proto_dirs(workspace_root)
    backup = snapshot_backup(workspace_root, ["PROTOTYPING.md"], "prototype")
    outcome = await run_worker(
        parent_config=config,
        workspace_root=workspace_root,
        playbook=PROTOTYPE_PLAYBOOK,
        goal=(
            "Pick the single most promising idea (its detail is in your context if "
            "provided; otherwise read the ideas index) and scaffold a SMALL, runnable "
            "prototype at prototypes/<prototype-name>/: a README.md (goal + how to run) "
            "plus minimal starter files. Keep it tiny and self-contained. End with an "
            "ARTIFACTS: line listing the files you created."
        ),
        context=prepend_guidance(
            workspace_root,
            f"Ideas to build from (active, not yet built):\n"
            f"{_active_titles(workspace_root, 'idea')}\n\n"
            f"{cortex_items.portfolio_digest(workspace_root)}",
        ),
        tools_allow=[
            "read_file",
            "write_file",
            "ls",
            "web_search",
            "memory_search",
            "safe_shell",
            "node_run",
        ],
        model=_deep_model(config),
        budget_check=budget_check,
        worker_label="prototype",
        want_shell=True,  # run the scaffolded prototype to verify it works
    )
    if outcome.status not in ("completed", "empty"):
        return outcome, 0, backup
    registered = _register_new_prototypes(workspace_root, before)
    return outcome, len(registered), backup


def _register_new_prototypes(
    workspace_root: Path,
    before: set[str],
    *,
    links: dict[str, str] | None = None,
    source: str = "autonomous",
) -> list[dict[str, Any]]:
    """Register every prototype folder that appeared since ``before`` as an item.

    Shared by the autonomous prototyping phase and the idea → prototype
    promotion, so both apply the same two rules: an empty scaffold dir (a worker
    that ``mkdir``'d and never wrote) is not an item, and an EXACT-title
    re-scaffold of an existing active prototype is not registered twice
    (threshold 1.0 ⇒ normalized-equality only, so a versioned or genuinely
    distinct prototype still registers and ``merge_duplicates`` never drops real
    work). The scaffolded files stay on disk either way.
    """
    new_dirs = _proto_dirs(workspace_root) - before
    registered: list[dict[str, Any]] = []
    for slug in sorted(new_dirs):
        folder = workspace_root / "prototypes" / slug
        if not any(p.is_file() for p in folder.rglob("*")):
            logger.info("cortex prototype: skipping empty scaffold dir %s", slug)
            continue
        title, summary = _read_proto_meta(folder, slug)
        if cortex_items.find_similar(workspace_root, "prototype", title, threshold=1.0) is not None:
            logger.info("cortex prototype: skipping exact-duplicate prototype '%s'", title)
            continue
        registered.append(
            cortex_items.add_item(
                workspace_root,
                kind="prototype",
                title=title,
                summary=summary,
                slug=slug,
                write_detail=False,  # the worker already wrote README + files
                adopt_folder=True,  # ... into THIS folder, which must stay its path
                source=source,
                links=links,
            )
        )
    return registered


def _read_proto_meta(folder: Path, slug: str) -> tuple[str, str]:
    """Best-effort title + summary from a scaffolded prototype's README."""
    readme = folder / "README.md"
    title = slug.replace("-", " ").title()
    summary = ""
    if readme.is_file():
        try:
            text = readme.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return title, summary
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("# "):
                title = s[2:].strip() or title
                break
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                summary = s[:200]
                break
    return title, summary


async def run_self_check(
    config: AgentConfig, workspace_root: Path, *, budget_check: bool = True
) -> tuple[WorkerOutcome, int, str]:
    """Review workspace/agent health and surface issues (read-only)."""
    outcome = await run_worker(
        parent_config=config,
        workspace_root=workspace_root,
        playbook=SELF_CHECK_PLAYBOOK,
        goal=(
            "Do a quick health check of this agent's workspace: stale or contradictory "
            "instructions, broken references, oversized/duplicated memory, config smells. "
            "Report concrete findings (do NOT modify files). If healthy, say so."
        ),
        tools_allow=["read_file", "ls", "memory_search"],
        model=_deep_model(config),
        budget_check=budget_check,
        worker_label="self_check",
    )
    count = len([ln for ln in (outcome.summary or "").splitlines() if re.match(r"\s*[-*] ", ln)])
    return outcome, count, ""


async def run_advance_items(
    config: AgentConfig, workspace_root: Path, *, budget_check: bool = True
) -> tuple[WorkerOutcome, int, str]:
    """Review existing ACTIVE items and push the most promising ones forward.

    Develops what's already captured — researches/refines ideas + brainstorms,
    extends + RUNS prototypes, and drives active projects forward (advance the
    PLAN, run/verify, update STATUS + JOURNAL) — rather than generating new
    items. No-op when nothing's active.
    """
    # Self-heal: collapse any exact-duplicate items before developing, so effort
    # goes into one canonical copy rather than several near-identical ones.
    merged = cortex_items.merge_duplicates(workspace_root)
    if merged:
        logger.info("cortex advance: merged %d duplicate item(s) before advancing", merged)
    items = [
        it
        for it in cortex_items.list_items(workspace_root, include_discarded=False)
        if it.get("kind") in ("idea", "brainstorm", "prototype", "project")
        and it.get("status") in ("active", "exploring")
    ]
    if not items:
        return WorkerOutcome(status="empty", summary="No active items to advance."), 0, ""
    # Highest-rated first so the worker's "pick the 1-2 most promising" starts
    # where the triage phase pointed; unrated items keep their recency order.
    items.sort(key=lambda it: -(cortex_items.rating_score(it) or 0.0))
    lines = [_advance_line(it) for it in items[:30]]
    fully = _fully_autonomous(config)
    context_parts = ["Active items you can develop further:\n" + "\n".join(lines)]
    tools = [
        "read_file",
        "write_file",
        "ls",
        "web_search",
        "web_fetch",
        "memory_search",
        "safe_shell",
        "node_run",
    ]
    playbook = ADVANCE_ITEMS_PLAYBOOK
    goal = (
        "Review the active items in your CONTEXT and push the 1-2 most promising "
        "forward with concrete work in their folders: research/refine ideas; "
        "extend + RUN prototypes (capturing RESULTS.md); and for projects, advance "
        "the PLAN, run/verify what you can, and update STATUS.md + JOURNAL.md. End "
        "with an ARTIFACTS: line."
    )
    if fully:
        tools += execute_tool_names(config)
        playbook = ADVANCE_ITEMS_PLAYBOOK + EXECUTE_ITEMS_ADDENDUM
        goal = (
            "EXECUTE: take the highest-rated prototype or project with a doable next step "
            "and finish one real step end to end (build, run, verify, record the result in "
            "its folder). Schedule the follow-up with cortex_schedule when the work "
            "continues past this run, publish results as a page when they are worth "
            "showing, capture reusable procedures as learned skills, and tell the owner "
            "when something is ready. Then, time permitting, develop a second item. End "
            "with an ARTIFACTS: line."
        )
        context_parts.append(_pending_steps_context(config))
        context_parts.append(_team_context(config))
    context = prepend_guidance(workspace_root, "\n\n".join(p for p in context_parts if p))
    outcome = await run_worker(
        parent_config=config,
        workspace_root=workspace_root,
        playbook=playbook,
        goal=goal,
        context=context,
        tools_allow=tools,
        model=_deep_model(config),
        budget_check=budget_check,
        worker_label="advance_items",
        want_shell=True,  # run prototypes / project scripts to verify progress
    )
    return outcome, len(outcome.artifacts or []), ""


def _advance_line(it: dict[str, Any]) -> str:
    line = (
        f"- [{it['kind']}] {it['title']}: {it.get('summary', '')[:80]} "
        f"(id: {it.get('id')}, folder: {it.get('path')}, updated {it.get('updated_at', '')[:10]})"
    )
    rating = it.get("rating") if isinstance(it.get("rating"), dict) else None
    if rating:
        line += (
            f"\n    rated {rating.get('score')}/10, recommendation: {rating.get('recommendation')}"
        )
        if rating.get("next_step"):
            line += f"; next step: {str(rating['next_step'])[:160]}"
    return line


def _fully_autonomous(config: AgentConfig) -> bool:
    try:
        from olano.config.system_settings import auto_approve_enabled

        return auto_approve_enabled(config)
    except Exception:
        return False


#: Tools the Advance Work worker gets ONLY under Fully Autonomous, on top of
#: the research + build set every posture has. Each is bounded on its own
#: side: ``cortex_schedule`` by the self-scheduling caps, ``pages_start`` by the
#: owner-set viewer password, ``talk_to_agent`` by the peer's own rules and
#: the delegation depth, the notification tools by the owner's channels.
EXECUTE_TOOL_NAMES: tuple[str, ...] = (
    "cortex_list",
    "cortex_view",
    "cortex_edit",
    "cortex_schedule",
    "scheduler_list",
    "scheduler_get",
    "pages_start",
    "pages_status",
    "talk_to_agent",
    "notification_send_text",
    "notification_send_document",
    "render_html_image",
    "render_html_pdf",
)


def execute_tool_names(config: AgentConfig) -> list[str]:
    """The execution set plus the agent's OWN outward tools (email, calendar,
    posts): anything in the ``write_external`` HITL category it already holds.
    Those stay subject to ``cognition.approval_required`` on the run, so by
    default a send waits in Pulse → Approvals rather than going out unseen."""
    from olano.security.hitl_policy import CATEGORY_TOOLS

    external = CATEGORY_TOOLS.get("write_external", frozenset())
    own = [t for t in (config.tools or []) if t in external]
    seen: set[str] = set()
    out: list[str] = []
    for name in [*EXECUTE_TOOL_NAMES, *own]:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _team_context(config: AgentConfig) -> str:
    """The team digest (cognition/fleet.py), or "" when this agent may see no
    peer. Only the Fully Autonomous worker holds talk_to_agent, so only it
    gets the messageable form; the triage and scaffold workers get the same
    digest as information for feasibility and planning."""
    try:
        from olano.cognition.fleet import team_digest

        return team_digest(config)
    except Exception:
        return ""


def _pending_steps_context(config: AgentConfig) -> str:
    """What the agent has already scheduled for itself, so the worker extends a
    chain instead of starting a parallel one."""
    try:
        from olano.config.system_settings import effective_max_self_scheduled_tasks
        from olano.tools.cortex_tools import _pending_cortex_tasks

        pending = _pending_cortex_tasks(config.name)
        cap = effective_max_self_scheduled_tasks(config)
    except Exception:
        return ""
    if not pending:
        return f"Pending self-scheduled steps: none (you may hold up to {cap})."
    lines = [f"Pending self-scheduled steps ({len(pending)} of {cap}):"]
    for t in pending[:10]:
        when = t.get("schedule") or t.get("run_at") or "unscheduled"
        lines.append(f"- {t.get('name')}: {when}; next run {t.get('next_run') or 'pending'}")
    return "\n".join(lines)


# ──────────────────────────── per-item enrichment ───────────────────────────


async def run_idea_explore(
    config: AgentConfig, workspace_root: Path, item: dict[str, Any], *, budget_check: bool = True
) -> WorkerOutcome:
    """Deepen ONE existing item — research + write supporting files into its folder."""
    folder = item.get("path", "")
    detail_file = item.get("detail_file", "")
    detail_text = ""
    dpath = workspace_root / detail_file if detail_file else None
    if dpath and dpath.is_file():
        try:
            detail_text = dpath.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            pass
    context = (
        f"Item: {item.get('title')} ({item.get('kind')})\n"
        f"Folder (write supporting files here): {folder}/\n"
        f"Detail file: {detail_file}\n\nCurrent detail:\n{detail_text}"
    )
    return await run_worker(
        parent_config=config,
        workspace_root=workspace_root,
        playbook=IDEA_EXPLORE_PLAYBOOK,
        goal=(
            f"Explore '{item.get('title')}' in depth. Research it, then write your "
            f"findings and any supporting files INTO {folder}/, and append a dated "
            f"'## Exploration' section to {detail_file}. End with an ARTIFACTS: line."
        ),
        context=context,
        tools_allow=["read_file", "write_file", "ls", "web_search", "web_fetch", "memory_search"],
        model=_deep_model(config),
        budget_check=budget_check,
        worker_label="idea_explore",
    )


def _source_links(item: dict[str, Any]) -> dict[str, str]:
    return {"from_item": str(item.get("id", "")), "from_path": str(item.get("path", ""))}


def _item_detail_text(workspace_root: Path, item: dict[str, Any], limit: int = 4000) -> str:
    detail_file = str(item.get("detail_file") or "")
    path = workspace_root / detail_file if detail_file else None
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _mark_promoted(workspace_root: Path, item: dict[str, Any], target: dict[str, Any]) -> None:
    cortex_items.update_item(
        workspace_root,
        str(item.get("id", "")),
        status="promoted",
        links={
            **(item.get("links") or {}),
            "promoted_to": str(target.get("path", "")),
            "promoted_to_id": str(target.get("id", "")),
            "promoted_to_kind": str(target.get("kind", "")),
        },
    )


async def run_promote(
    config: AgentConfig,
    workspace_root: Path,
    item: dict[str, Any],
    *,
    target: str | None = None,
    source: str = "manual",
    budget_check: bool = True,
) -> tuple[WorkerOutcome, dict[str, Any] | None]:
    """Move ``item`` one stage down the funnel (or straight to a project).

    ``target`` defaults to the next stage in :data:`cortex_items.FUNNEL`
    (brainstorm → idea → prototype → project); ``"project"`` is accepted from
    any stage, which is what the dashboard's **Create project** button means.
    Returns ``(outcome, new_item|None)``; on success the source is marked
    ``promoted`` and linked both ways, on failure it is left exactly as it was.
    ``source`` stamps the new item (``"manual"`` for a human's click or chat
    request, ``"autonomous"`` for the triage phase).
    """
    kind = str(item.get("kind") or "")
    target = (target or "").strip().lower() or cortex_items.next_kind(kind)
    if target is None or kind == "project":
        return WorkerOutcome(status="error", error="A project is the end of the funnel."), None
    if target == "project":
        return await run_promote_to_project(
            config, workspace_root, item, source=source, budget_check=budget_check
        )
    if kind == "brainstorm" and target == "idea":
        return await _promote_brainstorm_to_idea(
            config, workspace_root, item, source=source, budget_check=budget_check
        )
    if kind == "idea" and target == "prototype":
        return await _promote_idea_to_prototype(
            config, workspace_root, item, source=source, budget_check=budget_check
        )
    return (
        WorkerOutcome(
            status="error",
            error=(
                f"Cannot promote a {kind} to a {target}. The funnel is "
                f"{' → '.join(cortex_items.FUNNEL)}; 'project' is reachable from any stage."
            ),
        ),
        None,
    )


async def _promote_brainstorm_to_idea(
    config: AgentConfig,
    workspace_root: Path,
    item: dict[str, Any],
    *,
    source: str,
    budget_check: bool,
) -> tuple[WorkerOutcome, dict[str, Any] | None]:
    """Distil ONE brainstorm's strongest angle into a concrete idea item."""
    title = str(item.get("title") or "Untitled")
    context = prepend_guidance(
        workspace_root,
        f"Brainstorm: {title}\nFolder: {item.get('path')}/\n\n"
        f"Brainstorm detail:\n{_item_detail_text(workspace_root, item, 6000)}\n\n"
        f"{cortex_items.portfolio_digest(workspace_root)}",
    )
    outcome = await run_worker(
        parent_config=config,
        workspace_root=workspace_root,
        playbook=BRAINSTORM_TO_IDEA_PLAYBOOK,
        goal=(
            f"Promote the brainstorm '{title}' into ONE concrete, actionable idea: pick "
            "its strongest angle and return ONLY the JSON object for that idea."
        ),
        context=context,
        tools_allow=["read_file", "ls", "web_search", "web_fetch", "memory_search"],
        model=_deep_model(config),
        budget_check=budget_check,
        worker_label="promote_to_idea",
    )
    if outcome.status not in ("completed", "empty"):
        return outcome, None
    items = _as_item_list(_parse_json(outcome.summary))
    parsed = _coerce_item(items[0]) if items else None
    if parsed is None:
        outcome.status = "error"
        outcome.error = "The worker returned no idea to promote into."
        return outcome, None
    idea, created = cortex_items.upsert_item(
        workspace_root,
        kind="idea",
        title=parsed["title"],
        summary=parsed["summary"],
        detail=parsed["detail"],
        tags=parsed["tags"],
        source=source,
        links=_source_links(item),
    )
    if not created:
        # A near-identical idea already existed: that IS the idea this
        # brainstorm leads to, so link to it rather than adding a near-copy.
        cortex_items.update_item(
            workspace_root,
            str(idea.get("id", "")),
            links={**(idea.get("links") or {}), **_source_links(item)},
        )
    _mark_promoted(workspace_root, item, idea)
    outcome.summary = f"Promoted brainstorm '{title}' into idea '{idea.get('title')}'" + (
        "" if created else " (an existing near-identical idea, now linked)"
    )
    return outcome, idea


async def _promote_idea_to_prototype(
    config: AgentConfig,
    workspace_root: Path,
    item: dict[str, Any],
    *,
    source: str,
    budget_check: bool,
) -> tuple[WorkerOutcome, dict[str, Any] | None]:
    """Scaffold (and when safe, run) a prototype for ONE idea and link them."""
    title = str(item.get("title") or "Untitled")
    before = _proto_dirs(workspace_root)
    context = prepend_guidance(
        workspace_root,
        f"Idea to build: {title}\nFolder: {item.get('path')}/\n\n"
        f"Idea detail:\n{_item_detail_text(workspace_root, item, 6000)}",
    )
    outcome = await run_worker(
        parent_config=config,
        workspace_root=workspace_root,
        playbook=PROTOTYPE_PLAYBOOK,
        goal=(
            f"Build a SMALL, runnable prototype of the idea '{title}' (its detail is in "
            "your CONTEXT) at prototypes/<prototype-name>/: a README.md (goal, approach, "
            "how to run) plus minimal starter files; run it when safe and record "
            "RESULTS.md. End with an ARTIFACTS: line listing the files you created."
        ),
        context=context,
        tools_allow=[
            "read_file",
            "write_file",
            "ls",
            "web_search",
            "memory_search",
            "safe_shell",
            "node_run",
        ],
        model=_deep_model(config),
        budget_check=budget_check,
        worker_label="promote_to_prototype",
        want_shell=True,
    )
    if outcome.status not in ("completed", "empty"):
        return outcome, None
    registered = _register_new_prototypes(
        workspace_root, before, links=_source_links(item), source=source
    )
    if not registered:
        outcome.status = "error"
        outcome.error = "The worker created no prototype folder under prototypes/."
        return outcome, None
    prototype = registered[0]
    _mark_promoted(workspace_root, item, prototype)
    outcome.summary = f"Promoted idea '{title}' into prototype '{prototype.get('title')}'"
    return outcome, prototype


async def run_promote_to_project(
    config: AgentConfig,
    workspace_root: Path,
    item: dict[str, Any],
    *,
    source: str = "manual",
    budget_check: bool = True,
) -> tuple[WorkerOutcome, dict[str, Any] | None]:
    """Promote an item into projects/<slug>/ with README/PLAN/STATUS/JOURNAL.

    Returns ``(outcome, project_item|None)``. The project item is registered and
    the source item is marked ``promoted`` + linked.
    """
    title = str(item.get("title") or "Untitled project")
    # Pre-create the project folder so the worker writes into a known path.
    project = cortex_items.add_item(
        workspace_root,
        kind="project",
        title=title,
        summary=str(item.get("summary", "")),
        write_detail=False,  # the worker writes README.md + the framework files
        source=source,
        links=_source_links(item),
    )
    project_dir = project["path"]
    src_detail = item.get("detail_file", "")
    src_text = ""
    sp = workspace_root / src_detail if src_detail else None
    if sp and sp.is_file():
        try:
            src_text = sp.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            pass
    team = _team_context(config)
    context = (
        f"Source item: {title} ({item.get('kind')})\n"
        f"Source detail:\n{src_text}\n\n"
        f"Target project folder (create the framework files here): {project_dir}/"
        + (
            f"\n\n{team}\n(When a PLAN step needs a teammate's capability, name that agent's "
            "handle on the step so the run that executes it knows whom to ask.)"
            if team
            else ""
        )
    )
    outcome = await run_worker(
        parent_config=config,
        workspace_root=workspace_root,
        playbook=PROJECT_SCAFFOLD_PLAYBOOK,
        goal=(
            f"Promote '{title}' into a project. Create README.md, PLAN.md (phased, "
            f"checklist-driven, with '✅ Verify:' items), STATUS.md and JOURNAL.md in "
            f"{project_dir}/, and do genuine initial planning. End with an ARTIFACTS: line."
        ),
        context=context,
        tools_allow=["read_file", "write_file", "ls", "web_search", "memory_search"],
        model=_deep_model(config),
        budget_check=budget_check,
        worker_label="promote_to_project",
    )
    if outcome.status in ("completed", "empty"):
        _mark_promoted(workspace_root, item, project)
        return outcome, project
    # Worker failed — roll the placeholder project back to discarded so it doesn't
    # linger as an empty shell.
    cortex_items.set_status(workspace_root, project["id"], "discarded")
    return outcome, None
