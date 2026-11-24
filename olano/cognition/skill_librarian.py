"""Autonomous skill librarian — dedupes/merges/improves ALL of an agent's skills.

`skillsmith` validates one candidate skill at a time; nothing reviews the whole
library. This worker reads every skill under ``skills/`` and ``skills/learned/``,
merges duplicates/near-duplicates, improves each survivor with best practices, and
removes merged-away folders. A snapshot is taken first so the user can Undo from a
single Pulse action.

Mirrors :mod:`olano.cognition.workspace_optimizer`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from olano.cognition.backups import snapshot_backup
from olano.cognition.playbooks import SKILL_LIBRARIAN_PLAYBOOK
from olano.cognition.worker import WorkerOutcome, run_worker
from olano.config.workspace import WORKSPACE_STATE_DIRNAME

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

logger = logging.getLogger(__name__)

# Step and wall-clock budget for one run, scaled by library size. The worker
# reads and may rewrite every skill it touches, and each of those is a model
# turn plus a tool call, so roughly 4 to 6 langgraph super-steps per skill; the
# base covers planning, the final summary and the stale-shadow/conflict rounds.
# The agent's own budget (Cortex > Autonomy) is the FLOOR, never replaced: the
# fixed 60/80-step tiers this replaces were below the 100-step default that
# every other background run gets, so an agent with 11 to 50 skills had fewer
# steps than one with 5, and the Autonomy setting the failure notice points at
# could not reach this phase at all. Both values still clamp to the shared
# bounds in ``delegate_guards``.
_STEPS_BASE = 40
_STEPS_PER_SKILL = 6
_SECONDS_BASE = 600
_SECONDS_PER_SKILL = 45

#: Per-workspace record of the last completed run, so an unchanged library is
#: not re-read by a deep model every week.
_STATE_FILE = "skill_librarian.json"

_GOAL = (
    "Clean up this agent's skill library. Skills under skills/ (NOT skills/learned/) "
    "and any managed/global skills listed in your context are READ-ONLY originals — "
    "never edit or delete them. You may only edit, merge, and delete files UNDER "
    "skills/learned/. To improve a read-only original, write an improved copy to "
    "skills/learned/<slug>/SKILL.md that reuses the original's exact name and records "
    "provenance (lc_improves, lc_source_path, lc_source_sha256) so it shadows the "
    "original without overwriting it. Within skills/learned/, merge duplicates into "
    "one authoritative skill (best name, trigger, union of steps) and delete the "
    "merged-away folders. Re-merge any stale shadows flagged in your context (their "
    "source changed). End with an ARTIFACTS: line listing the skill paths you changed "
    "or removed."
)


def _skill_files(workspace_root: Path) -> list[str]:
    """Return workspace-relative paths of every skill file under skills/.

    Skills are folder form only (``<name>/SKILL.md``, incl. ``learned/<slug>/
    SKILL.md``); ``rglob`` covers every depth.
    """
    skills_root = workspace_root / "skills"
    if not skills_root.exists():
        return []
    found = (p for p in skills_root.rglob("SKILL.md") if p.is_file())
    return sorted(str(p.relative_to(workspace_root)) for p in found)


def _frontmatter_value(text: str, field: str) -> str:
    m = re.search(rf"^{field}:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip().strip('"').strip("'") if m else ""


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _is_inside(location: str, root: Path) -> bool:
    """True iff ``location`` is the same path as, or nested under, ``root`` — using
    real path containment, NOT a string-prefix test (so a sibling like ``ws-extra``
    is not treated as inside ``ws``)."""
    if not location:
        return True
    try:
        Path(location).resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _stale_shadows(workspace_root: Path) -> list[dict[str, str]]:
    """Learned shadows whose source skill changed since the shadow was written.

    A shadow records ``lc_source_path`` + ``lc_source_sha256`` (see self_learning's
    ``_write_skill_file``). When the original's current hash differs — OR the shadow
    has a source path but the hash line was dropped/mangled — it needs (re)stamping
    and is flagged. Hashes are byte hashes on both sides so line-ending/encoding
    quirks don't cause permanent false positives.
    """
    from olano.skills.fork import is_fork

    learned = workspace_root / "skills" / "learned"
    if not learned.exists():
        return []
    stale: list[dict[str, str]] = []
    for skill_md in learned.rglob("SKILL.md"):
        # Manifest-bearing forks track per-file source drift and get the folder-
        # aware three-way re-merge (``skills.remerge``, run at the start of
        # ``run_skill_librarian``) — restamping just the SKILL.md hash here
        # would falsely mark script/reference drift resolved.
        if is_fork(skill_md.parent):
            continue
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        recorded = _frontmatter_value(text, "lc_source_sha256")
        src_ref = _frontmatter_value(text, "lc_source_path")
        if not src_ref:
            continue  # not a provenance-bearing shadow
        src_path = Path(src_ref)
        if not src_path.is_absolute():
            src_path = workspace_root / src_path
        if not src_path.exists():
            continue
        current = _sha256(src_path)
        # Flag when the source drifted OR the recorded hash is missing (worker
        # dropped the line) — both need a deterministic restamp.
        if current and (not recorded or current != recorded):
            stale.append(
                {
                    "shadow": str(skill_md.relative_to(workspace_root)),
                    "source": src_ref,
                    "recorded_sha256": recorded,
                    "current_sha256": current,
                }
            )
    return stale


def _readonly_skill_refs(parent_config: AgentConfig, workspace_root: Path) -> list[dict[str, str]]:
    """Skills the worker must NOT edit: managed/global skills that live outside the
    workspace (so the worker's file tools can't reach them). Surfaced as read-only
    references so the worker doesn't duplicate them and can shadow them if needed."""
    from olano.config.workspace import Workspace
    from olano.skills.loader import iter_agent_skills

    try:
        skills = iter_agent_skills(parent_config, Workspace(root=workspace_root))
    except Exception:
        return []
    refs: list[dict[str, str]] = []
    for s in skills:
        location = str(s.get("location", ""))
        if _is_inside(location, workspace_root):
            continue  # in-workspace skills are handled via the editable manifest
        refs.append({"name": s["name"], "description": s.get("description", ""), "path": location})
    return refs


_INLINE_UPSTREAM_CAP = 6_000


def _conflict_context_line(conflict: dict[str, str], workspace_root: Path) -> str:
    """One worker-context line per fork conflict. When the upstream file lives
    OUTSIDE the workspace (managed/global skill), its content is inlined —
    the worker's workspace-scoped file tools cannot read it by path."""
    line = (
        f"- fork: {conflict['fork_path']} | upstream: {conflict['source_path']} "
        f"| base: {conflict['base_path']}"
    )
    if _is_inside(conflict["source_path"], workspace_root):
        return line
    try:
        content = Path(conflict["source_path"]).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return line
    if len(content) > _INLINE_UPSTREAM_CAP:
        content = content[:_INLINE_UPSTREAM_CAP] + "\n… (truncated)"
    return (
        f"{line}\n  upstream content (inlined — path is outside the workspace):\n"
        f"  ```\n{content}\n  ```"
    )


def run_budget(parent_config: AgentConfig, skill_count: int) -> tuple[int, float]:
    """``(max_iterations, timeout_seconds)`` for one librarian run.

    The agent's resolved worker budget (per-agent override, else the home-wide
    setting) is the floor; the size-scaled estimate raises it for a library the
    default could never cover. Clamped like every other budget, so the largest
    library still stops at the shared ceiling.
    """
    from olano.config.system_settings import effective_worker_budget
    from olano.security.delegate_guards import clamp_max_iterations, clamp_timeout_seconds

    agent_steps, agent_seconds = effective_worker_budget(parent_config)
    steps = max(agent_steps, _STEPS_BASE + _STEPS_PER_SKILL * skill_count)
    seconds = max(agent_seconds, _SECONDS_BASE + _SECONDS_PER_SKILL * skill_count)
    return clamp_max_iterations(steps), float(clamp_timeout_seconds(seconds))


def library_fingerprint(workspace_root: Path, skill_files: list[str]) -> str:
    """One hash over every skill file's path and content bytes.

    Two libraries fingerprint the same iff the same files hold the same bytes,
    so a run can tell "nothing changed since I last finished" without reading
    anything through a model.
    """
    h = hashlib.sha256()
    for rel in skill_files:
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(_sha256(workspace_root / rel).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _state_path(workspace_root: Path) -> Path:
    return workspace_root / WORKSPACE_STATE_DIRNAME / "state" / _STATE_FILE


def _last_completed_fingerprint(workspace_root: Path) -> str:
    try:
        data = json.loads(_state_path(workspace_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = data.get("fingerprint") if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


def _record_completed_fingerprint(workspace_root: Path, fingerprint: str) -> None:
    """Remember the library the worker just left behind. Best-effort: a state
    file that cannot be written only costs one unnecessary run next week."""
    path = _state_path(workspace_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fingerprint": fingerprint}), encoding="utf-8")
    except OSError as e:  # pragma: no cover - defensive
        logger.debug("skill_librarian: could not record run state: %s", e)


def _restamp_shadows(workspace_root: Path, stale: list[dict[str, str]]) -> int:
    """After a COMPLETED librarian run, stamp ``lc_source_sha256`` to the source's
    current hash for every flagged shadow — whether or not the worker rewrote the
    body. The worker was given each stale shadow + the new source content and told
    to reconcile, so a completed run means each was addressed (a no-op reconcile is
    valid when the source change didn't affect the shadow). This guarantees the
    re-merge loop converges instead of re-flagging the same shadow forever, and it
    repairs a dropped/relocated hash line by re-inserting it after ``lc_source_path``.
    A backup snapshot was taken first, so the user can still Undo.
    """
    restamped = 0
    for s in stale:
        shadow_path = workspace_root / s["shadow"]
        try:
            text = shadow_path.read_text(encoding="utf-8")
        except OSError:
            continue
        target = f"lc_source_sha256: {s['current_sha256']}"
        if re.search(r"^lc_source_sha256:.*$", text, re.MULTILINE):
            new = re.sub(r"^lc_source_sha256:.*$", target, text, count=1, flags=re.MULTILINE)
        elif re.search(r"^lc_source_path:.*$", text, re.MULTILINE):
            new = re.sub(
                r"^(lc_source_path:.*)$", r"\1\n" + target, text, count=1, flags=re.MULTILINE
            )
        else:
            continue  # no provenance anchor to attach the hash to
        if new != text:
            try:
                shadow_path.write_text(new, encoding="utf-8")
                restamped += 1
            except OSError:
                continue
    return restamped


async def run_skill_librarian(
    parent_config: AgentConfig,
    workspace_root: Path,
    *,
    budget_check: bool = True,
) -> tuple[WorkerOutcome, str]:
    """Dedupe/merge/improve all skills in place.

    Returns ``(outcome, backup_job_id)``. ``backup_job_id`` is "" when there are
    no skills to snapshot.
    """
    # Deterministic three-way re-merge of stale forks FIRST (token-free): forks
    # whose source drifted absorb upstream changes file-by-file (own snapshot +
    # Undo pulse). The worker below then sees refreshed forks and only has to
    # reconcile the semantic PROSE conflicts the deterministic pass kept ours on.
    remerge_summaries: list[dict] = []
    try:
        from olano.skills.remerge import post_remerge_pulse, remerge_stale_forks

        remerge_summaries, remerge_backup = remerge_stale_forks(workspace_root)
        post_remerge_pulse(parent_config, workspace_root, remerge_summaries, remerge_backup)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("skill_librarian: fork re-merge failed: %s", e)

    skill_files = _skill_files(workspace_root)
    if not skill_files:
        return (
            WorkerOutcome(
                status="empty",
                summary=(
                    f"No skills found in {workspace_root / 'skills'}. This agent has no "
                    "skills of its own yet — learned skills appear here after the agent "
                    "saves reusable workflows. (Skills loaded from shared/managed "
                    "locations are read-only and not optimized.)"
                ),
            ),
            "",
        )

    # Split the manifest so the worker knows what it may edit (skills/learned/)
    # versus the read-only originals it must shadow rather than overwrite.
    editable = [p for p in skill_files if p.startswith("skills/learned/")]
    originals = [p for p in skill_files if not p.startswith("skills/learned/")]
    managed_refs = _readonly_skill_refs(parent_config, workspace_root)
    stale = _stale_shadows(workspace_root)
    prose_conflicts = [
        c
        for s in remerge_summaries
        for c in s.get("conflicts", [])
        if c.get("kind") == "prose"  # scripts/binary deliberately stay ours — never merge code
    ]

    # Nothing changed since the last run that finished, and nothing is flagged:
    # a deep model re-reading the whole library would find exactly what it
    # found last time. Skipping leaves the failure streak alone (it is not a
    # run) and posts no pulse, since there is nothing to undo.
    fingerprint = library_fingerprint(workspace_root, skill_files)
    if (
        not stale
        and not prose_conflicts
        and fingerprint
        and fingerprint == _last_completed_fingerprint(workspace_root)
    ):
        return (
            WorkerOutcome(
                status="skipped",
                summary=(
                    f"Skill library unchanged since the last cleanup "
                    f"({len(skill_files)} skills); nothing to do."
                ),
            ),
            "",
        )

    backup_job = snapshot_backup(workspace_root, skill_files, "skill_librarian")

    sections: list[str] = [
        f"EDITABLE — skills under skills/learned/ ({len(editable)}). You may improve, "
        "merge, and delete these:",
        "\n".join(f"- {p}" for p in editable) or "(none yet)",
        f"\nREAD-ONLY originals under skills/ ({len(originals)}). NEVER edit or delete "
        "these. To improve one, write a shadow to skills/learned/<slug>/SKILL.md that "
        "reuses the original's exact name + records lc_improves/lc_source_path/"
        "lc_source_sha256:",
        "\n".join(f"- {p}" for p in originals) or "(none)",
    ]
    if managed_refs:
        sections += [
            f"\nREAD-ONLY managed/global skills ({len(managed_refs)}) — these live "
            "outside this workspace and cannot be edited; shadow into skills/learned/ "
            "only if they genuinely need improvement:",
            "\n".join(f"- {r['name']}: {r['description']}" for r in managed_refs),
        ]
    if stale:
        sections += [
            f"\nSTALE shadows to RE-MERGE ({len(stale)}) — the source changed since the "
            "shadow was written. Reconcile the new source content with the shadow's "
            "improvements and update lc_source_sha256 to the value shown:",
            "\n".join(
                f"- {s['shadow']} (source: {s['source']}; set lc_source_sha256: "
                f"{s['current_sha256']})"
                for s in stale
            ),
        ]
    if prose_conflicts:
        sections += [
            f"\nFORK CONFLICTS to reconcile semantically ({len(prose_conflicts)}) — the "
            "source skill changed upstream AND the learned fork changed the same file; "
            "the fork's version was kept. Merge the upstream changes INTO the fork file "
            "WITHOUT losing any fork content (read the fork, upstream, and base "
            "versions). Do not touch .lc-provenance.json:",
            "\n".join(_conflict_context_line(c, workspace_root) for c in prose_conflicts),
        ]
    n = len(skill_files)
    max_iterations, timeout = run_budget(parent_config, n)
    sections.append(
        "\nWork in batches: cluster by capability, merge duplicate groups WITHIN "
        "skills/learned/, and improve survivors. Prioritize the largest duplicate "
        "clusters first so the biggest wins land even if you run out of time. "
        f"Your budget for this run is about {max_iterations // 5} tool calls, so "
        "read a read-only original only when a learned skill overlaps it or a "
        "flagged item names it; do not read every original, and leave an original "
        "alone unless you have a concrete improvement for it. Write your closing "
        "ARTIFACTS: line as soon as the flagged items and the duplicate clusters "
        "are handled, not after one more pass."
    )
    context = "\n".join(sections)

    # Use the cognition DEEP model — merging a whole skill library is heavy
    # reasoning a cheap agent model gives up on. Falls back to the agent's own
    # model when no deep model is configured.
    from olano.cognition.router import effective_deep_model

    deep_model = effective_deep_model(parent_config.cognition, parent_config.model)

    outcome = await run_worker(
        parent_config=parent_config,
        workspace_root=workspace_root,
        playbook=SKILL_LIBRARIAN_PLAYBOOK,
        goal=_GOAL,
        context=context,
        tools_allow=["read_file", "write_file", "ls", "memory_read"],
        model=deep_model,
        max_iterations=max_iterations,
        timeout=timeout,
        budget_check=budget_check,
        worker_label="skill_librarian",
    )
    if stale and outcome.status == "completed":
        _restamp_shadows(workspace_root, stale)
    if outcome.status in ("completed", "empty"):
        # Fingerprint what the worker LEFT, not what it was given: the restamp
        # above and the worker's own writes are part of the finished state.
        _record_completed_fingerprint(
            workspace_root, library_fingerprint(workspace_root, _skill_files(workspace_root))
        )
    _post_result_pulse(parent_config, workspace_root, outcome=outcome, backup_job=backup_job)
    return outcome, backup_job


def _post_result_pulse(
    config: AgentConfig,
    workspace_root: Path,
    *,
    outcome: WorkerOutcome,
    backup_job: str,
) -> None:
    """Post a FINDING Pulse summarizing the skill cleanup, with Undo."""
    if outcome.status not in ("completed", "empty"):
        return
    try:
        from olano.pulse.engine import PulseEngine
        from olano.pulse.models import PulseLane, PulsePriority, PulseType

        engine = PulseEngine(config, workspace_root / WORKSPACE_STATE_DIRNAME)
        action = {"type": "restore_backup", "job_id": backup_job} if backup_job else None
        engine.add(
            content=outcome.summary or "Skill library cleaned up.",
            source="cognition",
            lane=PulseLane.PULSE,
            type=PulseType.FINDING,
            priority=PulsePriority.LOW,
            title="Optimized skill library",
            why_now="Automatic self-optimization ran in the background.",
            suggested_next_step="Review the merged/improved skills; use Undo to revert.",
            references=outcome.artifacts,
            action=action,
            metadata={"kind": "auto_optimization", "auto_applied": True},
            dedup_window_hours=0.0,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("skill_librarian: result pulse failed: %s", e)
