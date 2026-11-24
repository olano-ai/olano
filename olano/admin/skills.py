"""Skill discovery + authoring + installation.

List discoverable skills, create a new managed skill (a ``SKILL.md`` with
valid YAML frontmatter under ``<home>/skills/<slug>/``), and install an
existing skill *directory* (SKILL.md + scripts/assets) into a target agent's
workspace or the managed skills dir. Written/copied skills are re-discovered
through :func:`olano.skills.loader.discover_skills` so a malformed
frontmatter is caught immediately.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from olano.config.home import get_olano_paths
from olano.skills.loader import discover_skills

_VALID_TIERS = ("light", "subagent")

# Bounds for install_skill's recursive copy — a skill is docs + a few scripts,
# not a dataset. Oversized sources are almost certainly a mistake (node_modules,
# a checkout) and would bloat the target workspace.
_INSTALL_MAX_FILES = 2_000
_INSTALL_MAX_BYTES = 50 * 1024 * 1024


def _slugify(name: str) -> str:
    from olano.skills.naming import skill_slug

    return skill_slug(name) or "skill"


def list_skills() -> list[dict[str, Any]]:
    """Return all skills discoverable in the home-managed skills directory.

    This is the *live* set an agent actually loads (built-ins synced from the
    package plus anything authored locally). For the read-only catalog of skills
    shipped with olano itself, use :func:`list_builtin_skills`.
    """
    skills_dir = get_olano_paths().managed_skills_dir
    if not skills_dir.exists():
        return []
    return discover_skills(skills_dir)


def list_builtin_skills() -> list[dict[str, Any]]:
    """Return the catalog of skills shipped with olano (the package built-ins).

    These are synced into ``<home>/skills/`` on install/update (and then surface
    via :func:`list_skills`); this lists the source catalog directly so
    AgentFather can recommend a built-in skill even before it has been synced.
    Each entry carries ``name``, ``description``, ``category``, ``tier``, and the
    on-disk ``location``.
    """
    from olano.skills.built_in_sync import _builtin_source_dir

    return discover_skills(_builtin_source_dir())


def list_agent_skills(agent: str) -> list[dict[str, Any]]:
    """Every skill *agent* can discover, each stamped with ``enabled``.

    The listing behind ``self_list_skills`` and the on/off surfaces: the same
    source dirs the agent's own build resolves (its ``skills:`` entries, the
    shared ``<home>/skills`` pack, its workspace and learned dirs), deduped
    last-source-wins exactly as deepagents does, with the skills it has turned
    off still present and marked — a listing that hid them would leave nothing
    to switch back on. Skills gated by ``requires_tools`` the agent lacks are
    present too, with ``gated`` naming the missing tools.
    """
    from olano.config.loader import load_agent_config
    from olano.config.workspace import get_workspace
    from olano.skills import browse as skill_browse
    from olano.skills.loader import load_skills_for_agent

    cfg = load_agent_config(agent)
    ws = get_workspace(cfg.workspace)
    by_name: dict[str, dict[str, Any]] = {}
    for source_dir in load_skills_for_agent(cfg, ws):
        for skill in discover_skills(Path(source_dir)):
            if skill.get("name"):
                by_name[str(skill["name"])] = skill
    rows = skill_browse.annotate(list(by_name.values()), cfg, ws)
    return sorted(rows, key=lambda s: str(s.get("name") or "").lower())


def set_skill_enabled(agent: str, skill: str, enabled: bool) -> dict[str, Any]:
    """Turn one skill on or off for *agent* by editing ``disabled_skills``.

    The skill itself is never touched: this only adds or removes its name from
    the agent's denylist, which is the only per-agent control there can be —
    skills arrive by directory, and the managed pack is one folder shared by
    every agent on the box.

    The name must resolve to a skill the agent can actually discover. Refusing
    an unknown name is the point: a typo would otherwise be saved as a denylist
    entry that silently does nothing, and the caller would believe the skill was
    off. Matching is case-insensitive, and the entry written is the skill's own
    spelling, so the file never disagrees with the SKILL.md it names.
    """
    from olano.admin.agents import update_agent
    from olano.config.loader import load_agent_config

    wanted = (skill or "").strip()
    if not wanted:
        raise ValueError("skill name is required.")

    known = {str(s.get("name") or ""): s for s in list_agent_skills(agent)}
    canonical = next((n for n in known if n.lower() == wanted.lower()), None)
    if canonical is None:
        raise ValueError(
            f"Agent '{agent}' has no skill named '{wanted}'. "
            f"Known skills: {', '.join(sorted(known, key=str.lower)) or '(none)'}."
        )
    # A gated skill (`requires_tools` this agent lacks) has no switch to flip:
    # the denylist is not what keeps it out, so removing it from the denylist
    # would report "on" for a skill the agent still never sees. Say what would
    # actually turn it on. Turning a gated skill OFF is still allowed — the
    # entry is harmless and survives a later tool grant as the operator's choice.
    gated = known[canonical].get("gated")
    if enabled and gated:
        raise ValueError(
            f"'{canonical}' cannot be turned on for '{agent}': it {gated}. "
            "Grant those tools first (the agent's Tools tab, or `tools` in its "
            "agent.json5), and the skill appears on its own."
        )

    cfg = load_agent_config(agent)
    current = list(cfg.disabled_skills or [])
    kept = [n for n in current if str(n).strip().lower() != canonical.lower()]
    new_list = kept if enabled else [*kept, canonical]
    changed = new_list != current
    if changed:
        update_agent(agent, {"disabled_skills": new_list})
    return {
        "agent": agent,
        "skill": canonical,
        "enabled": enabled,
        "changed": changed,
        "disabled_skills": new_list,
        "note": (
            "Saved. The running agent keeps its current skills until it restarts."
            if changed
            else f"'{canonical}' was already {'on' if enabled else 'off'} — nothing to change."
        ),
    }


def create_skill(
    name: str,
    description: str,
    body: str,
    *,
    category: str = "general",
    tier: str = "light",
    trigger: str = "",
    blocked_tools: list[str] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Author a new managed skill as ``<home>/skills/<slug>/SKILL.md``.

    ``tier`` is ``light`` (system-prompt injected) or ``subagent`` (delegatable
    specialist). ``body`` is the markdown that follows the frontmatter.
    """
    if tier not in _VALID_TIERS:
        raise ValueError(f"tier must be one of {_VALID_TIERS}, got {tier!r}.")
    slug = _slugify(name)
    skill_dir = get_olano_paths().managed_skills_dir / slug
    skill_path = skill_dir / "SKILL.md"
    if skill_path.exists() and not overwrite:
        raise FileExistsError(f"Skill '{slug}' already exists. Pass overwrite=True.")
    skill_dir.mkdir(parents=True, exist_ok=True)

    front = [
        "---",
        f"name: {slug}",
        f"category: {category}",
        f"description: {description}",
        f"tier: {tier}",
    ]
    if trigger:
        front.append(f"trigger: {trigger}")
    if tier == "subagent" and blocked_tools:
        front.append(f"blocked_tools: [{', '.join(blocked_tools)}]")
    front.append("---")
    content = "\n".join(front) + "\n\n" + body.strip() + "\n"
    skill_path.write_text(content, encoding="utf-8")

    # Re-discover to validate the frontmatter parses cleanly.
    discovered = {s["name"]: s for s in discover_skills(skill_dir)}
    if slug not in discovered:
        raise ValueError(
            f"Wrote {skill_path} but it did not parse as a valid skill — check the frontmatter."
        )
    return {"name": slug, "path": str(skill_path), "tier": tier, "created": True}


def _align_skill_name_to_dir(skill_md: Path, slug: str) -> str | None:
    """Rewrite ``name:`` in *skill_md*'s frontmatter to *slug*; return the old name.

    ``None`` when nothing needed changing (or the file could not be rewritten —
    a cosmetic normalization must never fail an install that otherwise worked).
    One definition with every other writer: :func:`olano.skills.naming.set_frontmatter_name`.
    """
    from olano.skills.naming import set_frontmatter_name

    return set_frontmatter_name(skill_md, slug)


def _repair_roots(agent: str | None) -> tuple[list[Path], Path]:
    """The skill dirs a repair walks, and the managed dir among them.

    For an agent, exactly the dirs its boot alert reads
    (``loader._skill_source_dirs``): the repair used to walk only the managed
    dir and ``<workspace>/skills``, so an offender the alert named under a
    ``config.skills`` dir could never be repaired from it. Falls back to that
    old pair when the agent's config cannot be loaded.
    """
    paths = get_olano_paths()
    managed = paths.managed_skills_dir
    roots: list[Path] = [managed]
    if agent:
        try:
            from olano.config.loader import load_agent_config
            from olano.config.workspace import get_workspace
            from olano.skills.loader import _skill_source_dirs

            cfg = load_agent_config(agent)
            roots += [Path(d) for d in _skill_source_dirs(cfg, get_workspace(cfg.workspace))]
        except Exception:
            roots.append(paths.agent_workspace_dir(agent) / "skills")
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique, managed


def carry_disabled_skill_renames(renames: dict[str, str], agents: list[str]) -> list[str]:
    """Point ``disabled_skills`` entries naming an old skill name at its new one.

    A skill switched off under its old (illegal) name would otherwise come back
    on after a repair: the denylist matches by name, and the old name no longer
    names anything. Matching is the denylist's own (``disabled_skill_names``:
    trimmed, case-insensitive). Returns the agents whose config changed. Never
    raises: a repair must not fail over one agent's unreadable config.
    """
    if not renames:
        return []
    from olano.admin.agents import update_agent
    from olano.config.loader import load_agent_config

    by_old = {old.strip().lower(): new for old, new in renames.items()}
    changed: list[str] = []
    for agent in agents:
        try:
            current = list(load_agent_config(agent).disabled_skills or [])
        except Exception:
            continue
        updated: list[str] = []
        for entry in current:
            new = by_old.get(str(entry).strip().lower())
            if new is not None and new not in updated:
                updated.append(new)
            elif new is None and entry not in updated:
                updated.append(entry)
        if updated == current:
            continue
        try:
            update_agent(agent, {"disabled_skills": updated})
        except Exception:
            continue
        changed.append(agent)
    return changed


def _agents_affected_by(skill_dir: Path, managed: Path, agent: str | None) -> list[str]:
    """Whose ``disabled_skills`` can name the skill in *skill_dir*.

    A managed skill is shared by every agent on the box; anything else belongs
    to the agent whose dirs were scanned.
    """
    try:
        skill_dir.relative_to(managed)
    except ValueError:
        return [agent] if agent else []
    from olano.config.loader import list_agents

    try:
        return list_agents()
    except Exception:
        return [agent] if agent else []


def repair_skill_names(
    agent: str | None = None,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Find (and optionally fix) skills whose folder disagrees with their name.

    The Agent Skills spec requires ``name`` to equal the directory holding
    ``SKILL.md``. deepagents warns once per offending skill on **every agent
    boot** and keeps loading it, so the only symptom is log noise — until an
    upstream release promotes that warning to a skip, at which point the agent
    silently loses the skill.

    The fix per case is :func:`olano.skills.naming.plan_name_fix`'s:

    * a legal name in another folder: the **folder** is renamed to the name.
      The name is the skill's identity everywhere it matters (the system
      prompt, the ``delegate_task`` registry, the shadow dedupe) while the
      folder path is re-resolved on every boot;
    * an illegal name (spaces, capitals) in a legal folder: the **name** is
      rewritten to the folder. This is the shape a model writes
      (``Olano Professional HTML Email Formatting`` in
      ``olano-professional-html-email-formatting/``) and the repair used to
      refuse it, so the alert came back on every boot;
    * both illegal: the folder is renamed to the name's slug and the name set
      to match.

    A changed name is carried into every affected agent's ``disabled_skills``
    so a skill that was switched off stays off.

    Reported but never touched: a target folder that already exists (renaming
    would clobber a real skill), and a learned fork (``.lc-provenance.json``;
    its name must stay the original's and its manifest records absolute paths).

    Dry-run by default: ``apply=False`` returns exactly what ``apply=True``
    would do. ``agent=None`` scans the managed ``<home>/skills`` dir; otherwise
    every skill dir that agent loads from too.
    """
    from olano.skills.naming import (
        RENAME_FOLDER,
        REWRITE_NAME,
        is_fork,
        plan_name_fix,
        set_frontmatter_name,
    )

    roots, managed = _repair_roots(agent)

    planned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    renames: dict[str, str] = {}
    affected: set[str] = set()
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for skill in discover_skills(root):
            if not skill.get("spec_error") or skill["location"] in seen:
                continue
            seen.add(skill["location"])
            skill_md = Path(skill["location"])
            skill_dir = skill_md.parent
            name = str(skill["name"])
            entry: dict[str, Any] = {
                "name": name,
                "path": str(skill_dir),
                "reason": skill["spec_error"],
            }
            if is_fork(skill_dir):
                skipped.append(
                    {**entry, "skipped": "learned fork; its provenance records this path"}
                )
                continue
            fix = plan_name_fix(name, skill_dir.name)
            if fix is None:
                skipped.append({**entry, "skipped": "no legal name can be derived from it"})
                continue
            target = skill_dir.parent / fix.target
            if fix.action != REWRITE_NAME and target.exists():
                skipped.append({**entry, "skipped": f"'{target}' already exists"})
                continue
            entry["action"] = fix.action
            if fix.action != REWRITE_NAME:
                entry["rename_to"] = str(target)
            if fix.action != RENAME_FOLDER:
                entry["new_name"] = fix.target
            if apply:
                try:
                    if fix.action != REWRITE_NAME:
                        skill_dir.rename(target)
                        entry["renamed"] = True
                    if fix.action != RENAME_FOLDER:
                        current_md = target / "SKILL.md" if entry.get("renamed") else skill_md
                        if set_frontmatter_name(current_md, fix.target) is None:
                            raise OSError("could not rewrite the name in SKILL.md")
                        entry["name_rewritten"] = True
                except OSError as exc:
                    skipped.append({**entry, "skipped": f"repair failed: {exc}"})
                    continue
                if fix.action != RENAME_FOLDER:
                    renames[name] = fix.target
                    affected.update(_agents_affected_by(skill_dir, managed, agent))
            planned.append(entry)

    carried = carry_disabled_skill_renames(renames, sorted(affected)) if apply else []
    result: dict[str, Any] = {
        "agent": agent,
        "applied": apply,
        "repaired" if apply else "would_repair": planned,
        "skipped": skipped,
        "count": len(planned),
    }
    if carried:
        result["disabled_skills_updated"] = carried
    return result


def _validate_skill_source_tree(source: Path) -> tuple[int, int]:
    """Reject symlinks and oversized trees; return (files, bytes).

    This function runs as the RUNTIME (root), copying a tree an agent (uid 1000)
    authored. A symlink in that tree could point anywhere the runtime can read —
    the vault, another agent's config — and a follow-the-link copy would launder
    that content into the destination workspace. Skills have no use for
    symlinks, so any symlink is an error rather than something to skip quietly.
    """
    files = 0
    total = 0
    if source.is_symlink():
        raise ValueError(f"Skill source '{source}' is a symlink — not allowed.")
    for dirpath, dirnames, filenames in os.walk(source):
        for name in (*dirnames, *filenames):
            entry = Path(dirpath) / name
            if entry.is_symlink():
                raise ValueError(
                    f"Skill source contains a symlink ('{entry}') — symlinks are not "
                    "allowed in a skill; copy the real files instead."
                )
        for name in filenames:
            files += 1
            if files > _INSTALL_MAX_FILES:
                raise ValueError(
                    f"Skill source has more than {_INSTALL_MAX_FILES} files — "
                    "that is not a skill directory. Point at the skill folder itself."
                )
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
            if total > _INSTALL_MAX_BYTES:
                raise ValueError(
                    f"Skill source exceeds {_INSTALL_MAX_BYTES // (1024 * 1024)} MB — "
                    "that is not a skill directory. Point at the skill folder itself."
                )
    return files, total


def _chown_tree_for_sandbox(root: Path) -> None:
    """Hand a freshly copied tree to the sandbox user (best effort).

    The copy runs as the runtime (root on managed installs); the target agent is
    uid 1000 and must own its own workspace files or it can read the skill but
    never edit or remove it. On non-root (self-host) installs the chown fails
    silently — the runtime user already owns everything there.
    """
    from olano.core.builder import chown_for_sandbox

    chown_for_sandbox(root)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            chown_for_sandbox(Path(dirpath) / name)


def install_skill(
    source_dir: str | Path,
    *,
    agent: str | None = None,
    overwrite: bool = False,
    allowed_source_roots: list[Path] | None = None,
) -> dict[str, Any]:
    """Install a skill DIRECTORY (SKILL.md + scripts/assets) where an agent finds it.

    This is the supported route for handing a skill an agent authored in its own
    workspace (e.g. inside a prototype repo) to another agent: agent shells are
    isolated from each other's workspaces by design, so the copy happens here,
    runtime-side. Missing parents — including the target's ``skills/`` dir and
    even the workspace of an agent that has never booted — are created.

    ``agent=None`` installs into the managed skills dir (``<home>/skills``),
    visible to every agent. Otherwise the skill lands in the named agent's
    ``workspace/skills/<slug>/`` and ownership is handed to the sandbox user.

    ``allowed_source_roots`` restricts where the source may live (the tool layer
    passes the calling agent's workspace + the shared dir); ``None`` skips that
    check for trusted callers (CLI, tests). Secret/product locations are denied
    unconditionally. The target agent discovers the skill on its next
    restart/build — installation does not hot-reload a running agent.
    """
    paths = get_olano_paths()
    source = Path(source_dir).expanduser()
    try:
        source = source.resolve(strict=True)
    except OSError as exc:
        raise FileNotFoundError(f"Skill source '{source_dir}' does not exist.") from exc
    if not source.is_dir():
        raise NotADirectoryError(f"Skill source '{source}' is not a directory.")

    # Unconditional deny of secret/product trees, independent of allowed roots.
    denied = [paths.vault_dir, Path("/opt/olano"), Path("/app"), Path("/etc")]
    for tree in denied:
        try:
            tree_resolved = tree.resolve() if tree.exists() else tree
        except OSError:
            tree_resolved = tree
        if source == tree_resolved or tree_resolved in source.parents:
            raise PermissionError(f"Skill source may not live under '{tree}'.")

    if allowed_source_roots is not None:
        roots = [r.resolve() for r in allowed_source_roots]
        if not any(source == r or r in source.parents for r in roots):
            raise PermissionError(
                f"Skill source '{source}' is outside your workspace and the shared "
                "folder. Copy the skill into your workspace first, then install it."
            )

    if not (source / "SKILL.md").is_file():
        raise FileNotFoundError(
            f"'{source}' has no SKILL.md. Point at the skill directory itself "
            "(the folder that directly contains SKILL.md)."
        )
    discovered = discover_skills(source)
    if not discovered:
        raise ValueError(
            f"'{source}/SKILL.md' did not parse as a valid skill — check the YAML frontmatter."
        )
    slug = _slugify(discovered[0]["name"])
    files, total_bytes = _validate_skill_source_tree(source)

    if agent:
        agent_config = paths.agents_dir / agent / "agent.json5"
        if not agent_config.is_file():
            raise ValueError(
                f"Agent '{agent}' does not exist (no {agent_config}). "
                "Create the agent first, then install the skill."
            )
        dest = paths.agent_workspace_dir(agent) / "skills" / slug
    else:
        dest = paths.managed_skills_dir / slug

    overwritten = dest.exists()
    if overwritten and not overwrite:
        raise FileExistsError(
            f"Skill '{slug}' already exists at {dest}. Pass overwrite=True to replace it."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    if overwritten:
        shutil.rmtree(dest)
    shutil.copytree(source, dest)

    # The destination folder is named from the SLUG, but the copy carries the
    # source's frontmatter verbatim — so a source named "LinkedIn Marketing"
    # used to land in `linkedin-marketing/` with `name: LinkedIn Marketing`
    # inside it, permanently violating the Agent Skills spec and warning on
    # every agent boot forever after. This is the one place olano itself can
    # mint that mismatch, so it is the one place to make the pair agree by
    # construction. Rewriting the single frontmatter line is the smaller edit:
    # the folder name is already committed to by `dest`.
    renamed_from = _align_skill_name_to_dir(dest / "SKILL.md", slug)

    # Validate the copy the same way create_skill validates its write.
    expected = slug if renamed_from else discovered[0]["name"]
    if not any(s["name"] == expected for s in discover_skills(dest)):
        shutil.rmtree(dest, ignore_errors=True)
        raise ValueError(f"Copied skill at {dest} did not re-discover cleanly; rolled back.")

    if agent:
        # The whole chain up from the skill dir may be freshly created (an agent
        # that has never booted) — hand every new level to the sandbox user.
        _chown_tree_for_sandbox(dest)
        from olano.core.builder import chown_for_sandbox

        ws_root = paths.agent_workspace_dir(agent)
        chown_for_sandbox(ws_root)
        chown_for_sandbox(ws_root / "skills")

    note = (
        "The target agent discovers this skill on its next restart/build; "
        "a running agent does not hot-reload skills."
    )
    if renamed_from:
        note += (
            f" Its frontmatter name was normalized from '{renamed_from}' to '{slug}' "
            "so it matches its folder, as the Agent Skills spec requires."
        )
    result: dict[str, Any] = {
        "name": slug,
        "agent": agent or None,
        "path": str(dest),
        "files": files,
        "bytes": total_bytes,
        "overwritten": overwritten,
        "note": note,
    }
    if renamed_from:
        result["renamed_from"] = renamed_from
    return result
