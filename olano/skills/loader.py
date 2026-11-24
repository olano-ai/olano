"""Skills loader — discovers skill markdown files and provides paths to deepagents."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from olano.security.delegate_guards import DEFAULT_MAX_ITERATIONS, DEFAULT_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig
    from olano.config.workspace import Workspace

logger = logging.getLogger(__name__)


_VALID_TIERS = ("light", "subagent")

_DEFAULT_SUBAGENT_BLOCKED_TOOLS = (
    "delegate_task",
    "memory_write",
    "memory_append",
    "send_message",
    "schedule_cron",
    "shell",
)


def _read_frontmatter(content: str) -> dict[str, Any]:
    """
    Parse YAML frontmatter from a markdown string.

    Prefers ``yaml.safe_load`` so list/scalar values (e.g. ``blocked_tools: [a, b]``)
    are typed correctly, and falls back to a minimal key:value line parser when
    PyYAML is unavailable or the block is malformed.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end == -1:
        return {}
    block = "\n".join(lines[1:end])

    try:
        import yaml

        data = yaml.safe_load(block)
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items()}
    except Exception:
        pass

    # Fallback: naive scalar-only parser
    result: dict[str, Any] = {}
    for line in lines[1:end]:
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result


def _parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        # Accept comma- or whitespace-separated inline lists
        parts = raw.replace(",", " ").split()
        return [p for p in parts if p]
    return []


def _normalize_tier(value: Any) -> str:
    if isinstance(value, str):
        t = value.strip().lower()
        if t in _VALID_TIERS:
            return t
    return "light"


def _is_within(child: Path, parent: Path) -> bool:
    """True when *child* is *parent* itself or nested anywhere beneath it."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# Agent Skills spec cap on a skill name. Kept beside the validator below so the
# two move together.
MAX_SKILL_NAME_LENGTH = 64


def skill_name_violation(name: str, directory_name: str) -> str | None:
    """Why *name* breaks the Agent Skills spec for a skill in *directory_name*, or ``None``.

    A **mirror** of deepagents' ``middleware/skills.py:_validate_skill_name``,
    which logs one warning per offending skill on every single agent boot and —
    per its own comment — only keeps loading them "for backwards
    compatibility". Nothing on our side checked, so the first anyone heard of it
    was the log; and if upstream ever promotes that warning to a skip, the agent
    silently loses the skill instead.

    ``tests/test_skill_name_spec.py`` pins this against deepagents' own
    validator over a shared case table, the same anti-drift discipline
    ``tests/test_stream_isolation.py`` uses for the ``nostream`` tag: an
    upstream rule change must surface as a red test, not as skills quietly
    vanishing from a customer's prompt.

    The rules: 1-64 characters; lowercase alphanumeric (Unicode-aware, so
    ``café`` and ``über-tool`` pass) plus single hyphens; no leading, trailing
    or doubled ``-``; and the name must equal the directory holding SKILL.md.
    """
    if not name:
        return "name is required"
    if len(name) > MAX_SKILL_NAME_LENGTH:
        return "name exceeds 64 characters"
    if name.startswith("-") or name.endswith("-") or "--" in name:
        return "name must be lowercase alphanumeric with single hyphens only"
    for char in name:
        if char == "-":
            continue
        if (char.isalpha() and char.islower()) or char.isdigit():
            continue
        return "name must be lowercase alphanumeric with single hyphens only"
    if name != directory_name:
        return f"name '{name}' must match directory name '{directory_name}'"
    return None


def discover_skills(skills_dir: Path) -> list[dict[str, Any]]:
    """
    Recursively walk a skills directory and return a list of skill metadata dicts.

    Every ``SKILL.md`` found at any depth marks a skill (its parent folder), so
    nested layouts such as ``skills/learned/<slug>/SKILL.md`` are surfaced — not
    just immediate children. A skill that bundles its own nested ``SKILL.md``
    (e.g. example assets) is reported once: the outermost wins and its subtree is
    pruned.

    Note: deepagents' own ``SkillsMiddleware`` still only discovers one level
    deep, so ``_skill_source_dirs`` must keep passing ``skills/learned/``
    explicitly. This recursion only governs olano's own listings (``/skills``,
    the catalog endpoint, capability banner, skill-awareness).

    Returned dict keys:
      - ``name``, ``description``, ``category``, ``location`` (str)
      - ``tier``: ``"light"`` (default, injected into parent system prompt) or
        ``"subagent"`` (triggered on-demand via ``delegate_task``)
      - ``blocked_tools``: list[str] — tools the subagent cannot call
      - ``max_iterations``: int — hard step cap on the subagent run (a
        langgraph ``recursion_limit``; defaults to the shared
        ``delegate_guards`` budget for ``subagent``-tier skills)
      - ``timeout_seconds``: int — wall-clock cap on subagent execution
        (same default source)
      - ``trigger``: optional str — short hint on when to invoke this skill
      - ``requires_tools``: list[str] — tool names the skill assumes; an agent
        whose effective tool set lacks any of them never sees the skill (see
        :func:`compute_gated_skills`). Not a grant: it never adds a tool.
        Read from the top level or from ``metadata.requires_tools`` (the
        Agent Skills spec permits no custom top-level keys, so catalog
        skills use the latter).
      - ``spec_error``: optional str — why this skill breaks the Agent Skills
        naming spec (see :func:`skill_name_violation`), or ``None``. Reported,
        never enforced: deepagents still loads a violating skill, so dropping it
        here would take away a working skill over a naming rule.

    ``max_iterations`` / ``timeout_seconds`` default to the ``delegate_guards``
    constants for subagent-tier skills. That is a **per-skill** budget declared
    in frontmatter — deliberately not the two-tier
    ``system_settings.effective_worker_budget`` an agent's background runs
    resolve through, since a skill's author sized it for that skill's work.
    """
    skills: list[dict[str, Any]] = []
    if not skills_dir.exists():
        return skills

    found_dirs: list[Path] = []
    # Sort by path depth (then name) so shallower SKILL.md files are visited
    # before any nested under them — that ordering is what makes the prune below
    # keep the outermost skill.
    for skill_md in sorted(skills_dir.rglob("SKILL.md"), key=lambda p: (len(p.parts), str(p))):
        if not skill_md.is_file():
            continue
        skill_dir = skill_md.parent
        if any(_is_within(skill_dir, seen) for seen in found_dirs):
            continue
        found_dirs.append(skill_dir)

        content = skill_md.read_text(encoding="utf-8").strip()
        fm = _read_frontmatter(content)
        if fm:
            name = str(fm.get("name", skill_dir.name)).strip() or skill_dir.name
            description = str(fm.get("description", "")).strip()
            category = str(fm.get("category", "general")).strip() or "general"
        else:
            # Legacy fallback: H1 heading + second non-empty line
            name = skill_dir.name
            for line in content.splitlines():
                if line.startswith("# "):
                    name = line[2:].strip()
                    break
            description = _extract_description(content)
            category = "general"
            fm = {}

        tier = _normalize_tier(fm.get("tier"))
        blocked_tools = _parse_str_list(fm.get("blocked_tools"))
        if tier == "subagent" and not blocked_tools:
            blocked_tools = list(_DEFAULT_SUBAGENT_BLOCKED_TOOLS)
        # Subagent-tier defaults follow the shared delegate_guards budget —
        # a hardcoded literal here silently splits from what every other
        # background worker gets (12 lingered after the constant moved on).
        max_iterations = _parse_int(
            fm.get("max_iterations"), DEFAULT_MAX_ITERATIONS if tier == "subagent" else 0
        )
        timeout_seconds = _parse_int(
            fm.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS if tier == "subagent" else 0
        )
        trigger = fm.get("trigger")
        trigger_str = str(trigger).strip() if isinstance(trigger, str) and trigger.strip() else None
        # Case-sensitive on purpose: these are registry tool names, matched
        # against the agent's effective tool set, never typed by a human.
        requires_tools = _parse_str_list(fm.get("requires_tools"))
        if not requires_tools:
            # The Agent Skills spec allows no custom top-level keys, so a
            # spec-checked catalog skill (olano-ai/skills runs the reference
            # validator) declares the gate under `metadata:` instead. Both
            # spellings are the same gate; the top-level one stays for the
            # built-in olano-* skills, which never enter the catalog.
            meta = fm.get("metadata")
            if isinstance(meta, dict):
                requires_tools = _parse_str_list(meta.get("requires_tools"))

        # Optional HEARTBEAT.md inside the skill folder — defines recurring tasks
        # that are automatically registered with the workspace heartbeat scheduler.
        heartbeat_md = skill_dir / "HEARTBEAT.md"

        skills.append(
            {
                "name": name,
                "description": description,
                "category": category,
                "location": str(skill_md),
                "tier": tier,
                "blocked_tools": blocked_tools,
                "max_iterations": max_iterations,
                "timeout_seconds": timeout_seconds,
                "trigger": trigger_str,
                "requires_tools": requires_tools,
                # Present only when the skill defines its own recurring tasks.
                "heartbeat_path": str(heartbeat_md) if heartbeat_md.exists() else None,
                "spec_error": skill_name_violation(name, skill_dir.name),
            }
        )
    return skills


def _extract_description(content: str) -> str:
    """Extract a one-line description from skill markdown (second non-empty line after H1)."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if line.startswith("# ") and i + 1 < len(lines):
            return lines[i + 1][:200]
    return ""


def load_skills_for_agent(config: AgentConfig, workspace: Workspace) -> list[str]:
    """
    Return a list of skill directory paths for deepagents' skills parameter.

    Priority (last source wins per skill name, per deepagents' SkillsMiddleware):
    1. Agent-specific skills (from config.skills) — lowest
    2. Managed skills (<home>/skills/) — common to all agents
    3. Workspace skills (workspace/skills/)
    4. Learned skills (workspace/skills/learned/) — highest

    See ``_skill_source_dirs`` for ordering rationale.

    Note: subagent-tier skills in these dirs are also scanned by deepagents'
    built-in SkillsMiddleware and listed in the system prompt. The
    ``delegate_task`` tool additionally registers them — see
    ``load_subagent_registry``.

    Skills the security audit flags are **not** removed from the returned source
    dirs (deepagents discovers skills per-directory and one level deep, so a dir
    can't drop a single skill). Instead they are quarantined downstream: the
    ``SkillAuditFilterMiddleware`` prunes them from the main agent's prompt, and
    ``iter_agent_skills`` / ``/skills`` mark them skipped. Flagging never aborts
    startup — see ``compute_skipped_skills``.
    """
    paths = _skill_source_dirs(config, workspace)

    skipped = compute_skipped_skills(config, workspace, _source_dirs=paths)
    for name, reason in sorted(skipped.items()):
        # A flagged skill must never abort agent startup — warn and continue.
        logger.warning(
            "Skill %r skipped by audit (level=%s): %s. The agent still starts; the "
            "skill is not loaded into the prompt (shown as skipped in /skills).",
            name,
            getattr(config.security, "skill_audit_level", "strict"),
            reason,
        )

    return paths


def disabled_skill_names(config: AgentConfig | None) -> set[str]:
    """The skill names ``agent.json5 → disabled_skills`` turns off, normalized.

    Lowercased and stripped, because the field is typed by a human (or written
    from a dashboard row) while the name it must match comes from a SKILL.md's
    frontmatter — a case difference between the two would silently leave the
    skill on, which is the failure mode an operator would never think to check.

    The single definition of "is this skill off", asked by every surface that
    has an opinion: the prompt prune (:mod:`olano.core.skill_audit_middleware`),
    the delegable-subagent registry, ``iter_agent_skills`` (self-learning and
    skill-awareness), and the listings that badge a row disabled.
    """
    if config is None:
        return set()
    return {
        str(n).strip().lower()
        for n in (getattr(config, "disabled_skills", None) or [])
        if str(n).strip()
    }


def is_skill_disabled(name: str, config: AgentConfig | None) -> bool:
    """Whether skill *name* is turned off for this agent."""
    return str(name).strip().lower() in disabled_skill_names(config)


def effective_tool_names(config: AgentConfig | None) -> set[str] | None:
    """The tool names ``config`` runs with, for :func:`gated_skill_reason`.

    ``None`` when there is no agent to ask (an unscoped listing), which every
    caller treats as "nothing is gated". Pure over the config apart from the
    multimodal credential probe, so a listing computed without a build agrees
    with the builder — and cheap enough to compute once per call, which is
    what every caller must do (never once per skill).
    """
    if config is None:
        return None
    from olano.config.ops_defaults import resolve_effective_tool_names

    try:
        return set(
            resolve_effective_tool_names(
                list(config.tools or []), list(config.disabled_tools or []), config
            )
        )
    except Exception:  # pragma: no cover - defensive: never break a listing
        logger.debug("effective_tool_names: resolution failed", exc_info=True)
        return set(config.tools or [])


def gated_skill_reason(skill: dict[str, Any], effective: set[str] | None) -> str | None:
    """Why *skill* is withheld from an agent with tool set *effective*, or ``None``.

    A skill declaring ``requires_tools`` is advertised only when EVERY listed
    tool is in the agent's effective set. That is how a skill written for
    AgentFather's ``olano_*`` toolkit (``olano-admin``, ``olano-agent-builder``)
    stays out of every other agent's prompt without an operator adding it to
    each agent's ``disabled_skills`` — the skill says who it is for. Not a
    grant: the reason names the missing tools so the fix (grant them) is
    obvious, but nothing here binds one.
    """
    required = [str(t) for t in (skill.get("requires_tools") or []) if str(t)]
    if not required or effective is None:
        return None
    missing = [t for t in required if t not in effective]
    if not missing:
        return None
    return "needs tools: " + ", ".join(missing)


def compute_gated_skills(
    config: AgentConfig,
    workspace: Workspace,
    *,
    _source_dirs: list[str] | None = None,
) -> dict[str, str]:
    """Map ``skill_name -> reason`` for skills whose ``requires_tools`` this agent lacks.

    The third fact next to :func:`compute_disabled_skills` (a deliberate
    choice) and :func:`compute_skipped_skills` (a security finding): a gated
    skill is neither — it is simply not written for this agent. Same shape so
    the builder can feed all three to the one prompt-pruning middleware, while
    every human-facing surface keeps them apart.
    """
    effective = effective_tool_names(config)
    if effective is None:
        return {}
    paths = _source_dirs if _source_dirs is not None else _skill_source_dirs(config, workspace)
    found: dict[str, str] = {}
    for path in paths:
        for skill in discover_skills(Path(path)):
            name = str(skill.get("name") or "")
            if not name or name in found:
                continue
            reason = gated_skill_reason(skill, effective)
            if reason:
                found[name] = reason
    return found


def compute_disabled_skills(
    config: AgentConfig,
    workspace: Workspace,
    *,
    _source_dirs: list[str] | None = None,
) -> dict[str, str]:
    """Map ``skill_name -> reason`` for skills an operator (or the agent) turned off.

    Shaped exactly like :func:`compute_skipped_skills` so the two feed the same
    prompt-pruning middleware, but they are **different facts** and are kept
    apart everywhere a human reads them: an audit skip is a security finding the
    operator did not ask for, a disabled skill is a deliberate choice. Only
    names that actually resolve to a discovered skill are returned, so the
    listing can distinguish "off" from a stale entry naming a skill that is no
    longer installed.
    """
    disabled = disabled_skill_names(config)
    if not disabled:
        return {}
    paths = _source_dirs if _source_dirs is not None else _skill_source_dirs(config, workspace)
    found: dict[str, str] = {}
    for path in paths:
        for skill in discover_skills(Path(path)):
            name = str(skill.get("name") or "")
            if name.lower() in disabled and name not in found:
                found[name] = "turned off for this agent"
    return found


def compute_skipped_skills(
    config: AgentConfig,
    workspace: Workspace,
    *,
    _source_dirs: list[str] | None = None,
) -> dict[str, str]:
    """Map ``skill_name -> reason`` for skills the security audit flags.

    The single source of truth for "quarantined" skills. Returns an empty dict
    when ``security.skill_audit`` is disabled. The reason is a human-readable,
    de-duplicated summary of the audit findings (e.g. ``"eval() call detected;
    subprocess usage"``), suitable for ``/skills`` and log warnings.
    """
    if not getattr(config.security, "skill_audit", False):
        return {}

    from olano.security.skill_audit import audit_skills_dir

    paths = _source_dirs if _source_dirs is not None else _skill_source_dirs(config, workspace)
    level = getattr(config.security, "skill_audit_level", "strict")
    skipped: dict[str, str] = {}
    for path in paths:
        for result in audit_skills_dir(Path(path), level=level):
            if result.approved or result.skill_name in skipped:
                continue
            reasons = [
                str(f.get("description", "")) for f in result.findings if f.get("description")
            ]
            skipped[result.skill_name] = "; ".join(dict.fromkeys(reasons)) or "flagged by audit"
    return skipped


def load_subagent_registry(config: AgentConfig, workspace: Workspace) -> dict[str, dict[str, Any]]:
    """
    Return a registry of subagent-tier skills keyed by skill name.

    Scans the same source dirs as ``load_skills_for_agent`` and filters to
    ``tier == "subagent"``. Later sources override earlier ones (last one wins)
    so per-agent skills can shadow managed/global skills of the same name.
    Quarantined learned shadows are skipped (the original wins instead).

    The value dict contains: ``name``, ``description``, ``location``,
    ``blocked_tools``, ``max_iterations``, ``timeout_seconds``, ``trigger``.
    Skills gated by ``requires_tools`` this agent lacks are skipped for the
    same reason disabled ones are: a skill absent from the prompt must not
    stay callable by name.
    """
    quarantined = compute_quarantined_shadows(config, workspace)
    disabled = disabled_skill_names(config)
    effective = effective_tool_names(config)
    registry: dict[str, dict[str, Any]] = {}
    for d in _skill_source_dirs(config, workspace):
        for s in discover_skills(Path(d)):
            if s.get("tier") != "subagent":
                continue
            if s["location"] in quarantined:
                continue
            # A disabled skill must not stay delegable: pruning it from the
            # prompt but leaving it in this registry would keep it callable by
            # name via `delegate_task`, which is the whole capability the
            # operator just turned off.
            if str(s["name"]).lower() in disabled:
                continue
            if gated_skill_reason(s, effective):
                continue
            registry[s["name"]] = s
    return registry


def compute_quarantined_shadows(
    config: AgentConfig, workspace: Workspace
) -> dict[str, dict[str, Any]]:
    """Map ``shadow SKILL.md location → details`` for DEGRADED learned copies.

    The preservation gate (see :mod:`olano.skills.fork`): a machine-written
    learned skill that shadows an original by name may only win the last-source-
    wins dedupe if it did not lose the original's content. Two shapes are
    checked:

    - **Forks** (carry a ``.lc-provenance.json`` sidecar): checked against their
      own recorded base — files removed, prose hollowed out, SKILL.md section
      skeleton dropped.
    - **Legacy SKILL.md-only shadows** (``auto_learned``/``lc_*`` marked, no
      sidecar) whose name collides with a real original: checked against the
      CURRENT original. This retroactively heals workspaces that already hold
      degenerate auto-learned stubs — the stub stays on disk but stops winning.

    Hand-written learned skills (no machine markers) are never quarantined.
    Detail dict: ``name``, ``reason``, ``original_location`` (may be empty when
    the original was uninstalled), ``original_description``.
    """
    from olano.skills.fork import (
        fork_violations,
        is_fork,
        is_machine_written,
        shadow_violations_vs_source,
    )

    learned_root = workspace.learned_skills_dir
    if not learned_root.exists():
        return {}

    # Originals = every non-learned skill, keyed by name (last source wins,
    # mirroring dedupe priority). Entries physically under learned/ are excluded
    # even when surfaced by the recursive scan of <ws>/skills.
    originals: dict[str, dict[str, Any]] = {}
    learned_resolved = learned_root.resolve()
    for d in _skill_source_dirs(config, workspace):
        try:
            if Path(d).resolve() == learned_resolved:
                continue
        except OSError:
            continue
        for s in discover_skills(Path(d)):
            if _is_within(Path(s["location"]).resolve(), learned_resolved):
                continue
            originals[s["name"]] = s

    quarantined: dict[str, dict[str, Any]] = {}
    for s in discover_skills(learned_root):
        shadow_md = Path(s["location"])
        skill_dir = shadow_md.parent
        original = originals.get(s["name"])
        if is_fork(skill_dir):
            violations = fork_violations(skill_dir)
        else:
            if original is None or not is_machine_written(shadow_md):
                continue
            violations = shadow_violations_vs_source(shadow_md, Path(original["location"]))
        if not violations:
            continue
        quarantined[s["location"]] = {
            "name": s["name"],
            "reason": "; ".join(violations),
            "original_location": str(original["location"]) if original else "",
            "original_description": str(original.get("description", "")) if original else "",
        }
    return quarantined


def _skill_source_dirs(config: AgentConfig, workspace: Workspace) -> list[str]:
    """Return the ordered list of candidate skill source dirs (existing only).

    deepagents' SkillsMiddleware resolves duplicate skill names with
    **last-source-wins**, so the list is ordered lowest → highest priority:

    1. ``config.skills`` (agent-specific, lowest)
    2. ``<home>/skills/`` — managed skills common to all agents
    3. ``<workspace>/skills/`` — per-agent workspace skills
    4. ``<workspace>/skills/learned/`` — self-learning skills (highest)

    ``learned/`` must be listed explicitly because deepagents only discovers
    skills one level deep; passing ``<workspace>/skills/`` alone would not
    surface the ``learned/<slug>/SKILL.md`` entries underneath it.
    """
    paths: list[str] = []
    for p in config.skills:
        resolved = Path(p)
        if not resolved.is_absolute():
            resolved = workspace.root / resolved
        if resolved.exists() and str(resolved) not in paths:
            paths.append(str(resolved))
    candidates = [
        workspace.managed_skills_dir,  # <home>/skills           (lowest priority)
        workspace.skills_dir,  # <workspace>/skills
        workspace.learned_skills_dir,  # <workspace>/skills/learned  (highest priority)
    ]
    for d in candidates:
        if d.exists() and str(d) not in paths:
            paths.append(str(d))
    return paths


def skill_read_roots(config: AgentConfig, workspace: Workspace) -> list[Path]:
    """Resolved skill source directories the agent is allowed to READ.

    Exactly the dirs ``_skill_source_dirs`` hands to deepagents — the same
    dirs whose ``SKILL.md`` locations are advertised in the system prompt —
    resolved (symlinks followed) for path-boundary comparison. The security
    middleware and the deepagents permission rules grant read access to these
    roots so skills living outside the workspace (managed ``<home>/skills/``,
    absolute ``config.skills`` entries) are actually readable by the agent.
    Writes to skill roots outside the workspace remain blocked.
    """
    roots: list[Path] = []
    for d in _skill_source_dirs(config, workspace):
        resolved = Path(d).resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def iter_agent_skills(config: AgentConfig, workspace: Workspace) -> list[dict[str, Any]]:
    """Every skill discoverable across ALL of the agent's source dirs.

    Walks the full ordered source-dir list (config.skills, managed ``~/.olano/
    skills``, ``<ws>/skills``, ``<ws>/skills/learned``) and dedupes by skill name,
    keeping the **last** occurrence so the result mirrors deepagents' last-source-
    wins priority (a learned skill shadows a managed one of the same name). Each
    dict carries an extra ``source_dir`` key naming the directory it came from.

    This is the canonical "what skills does the agent currently have" helper —
    used by self-learning (to avoid drafting duplicates) and the skill-awareness
    middleware (to surface newly added skills mid-session).

    Skills the security audit flags are excluded so quarantined skills are never
    treated as available (not announced by skill-awareness, not seen by
    self-learning). They remain visible — marked skipped — via ``/skills``.
    Degraded learned shadows (``compute_quarantined_shadows``) are skipped by
    LOCATION so the original they shadow wins the by-name dedupe again. Skills
    turned off via ``agent.json5 → disabled_skills`` are excluded for the same
    reason and stay listed the same way (marked disabled), as are skills whose
    ``requires_tools`` this agent lacks (marked gated).
    """
    source_dirs = _skill_source_dirs(config, workspace)
    skipped = compute_skipped_skills(config, workspace, _source_dirs=source_dirs)
    quarantined = compute_quarantined_shadows(config, workspace)
    disabled = disabled_skill_names(config)
    effective = effective_tool_names(config)
    by_name: dict[str, dict[str, Any]] = {}
    for d in source_dirs:
        for skill in discover_skills(Path(d)):
            if skill["name"] in skipped:
                continue
            if skill["location"] in quarantined:
                continue
            # Same rule as the audit skips above: a skill that is off is not
            # "available", so skill-awareness never announces it mid-session and
            # self-learning never treats it as prior art.
            if str(skill["name"]).lower() in disabled:
                continue
            if gated_skill_reason(skill, effective):
                continue
            by_name[skill["name"]] = {**skill, "source_dir": d}
    return list(by_name.values())


def build_skills_prompt(skill_dirs: list[str]) -> str:
    """Build an XML-formatted skills listing for injection into system prompts.

    Light skills list their SKILL.md location for progressive disclosure.
    Subagent skills carry a ``<invoke_via>delegate_task</invoke_via>`` hint
    instead of a file location — the parent agent must delegate instead of
    reading the file itself.
    """
    # Dedupe by name (last source wins) — recursion means a single source dir can
    # yield a learned shadow plus its top-level namesake, and source dirs overlap
    # (skills/ contains skills/learned/).
    by_name: dict[str, dict[str, Any]] = {}
    for d in skill_dirs:
        for s in discover_skills(Path(d)):
            by_name[s["name"]] = s
    all_skills = list(by_name.values())

    if not all_skills:
        return ""

    lines = ["<available_skills>"]
    for s in all_skills:
        tier = s.get("tier", "light")
        lines.append("  <skill>")
        lines.append(f"    <name>{s['name']}</name>")
        lines.append(f"    <description>{s['description']}</description>")
        lines.append(f"    <tier>{tier}</tier>")
        if tier == "subagent":
            lines.append("    <invoke_via>delegate_task</invoke_via>")
            if s.get("trigger"):
                lines.append(f"    <trigger>{s['trigger']}</trigger>")
        else:
            lines.append(f"    <location>{s['location']}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)
