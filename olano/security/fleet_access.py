"""Fleet filesystem access: the deployment-wide reach AgentFather's shell gets.

Every agent's file tools and shell are confined to its own workspace (plus
``shared/`` and read-only skill sources). AgentFather operates the whole
deployment, and doing that one file at a time through the admin tools
(``olano_read_agent_file``, ``olano_tail_log``) made the simple things slow: no
grep across agents, no tail of a large tool-output log, no fix to a shared skill
in place. ``agent.json5 → fleet_access: true`` lifts the boundary for one agent
over three trees of the home:

* ``<home>/agents/`` read-write: every agent's directory, so a new agent is
  visible the moment it is created (one mount of the parent, not one per
  workspace). This is deliberately the whole directory, including each agent's
  ``agent.json5`` and its connector token files: the grant is for the
  deployment's administrator, who can read and change all of it anyway.
* ``<home>/skills/`` read-write: the common skills every agent loads.
* ``<home>/logs/`` read-only: ``olano.log``, ``runtime.log``, the A2A log.

The vault, the checkpoint databases, ``config.yaml`` and the rest of the home
stay outside the grant.

**Who may hold it.** The field is the grant, and writing it is an
administrator's decision: AgentFather's own config writes refuse to newly set it
without an admin on the turn (``olano_admin._fleet_access_denial``), the
dashboard's agent editors are admin-only, and the ``self_*`` tools write named
fields only. A cognition worker or ``delegate_task`` child inherits it with the
rest of its parent's config (``model_copy``), which is what keeps its sandbox
mounts identical to the parent's: it runs in the parent's container
(``builder.sandbox_owner``), and a different mount set would recreate it.

**Three enforcement points, one definition.** :func:`fleet_roots` is read by the
sandbox mount plan (``builder._resolve_sandbox_shared_mounts``, host-identical
paths like every other mount), by the security middleware's read/write boundary
(``deepagents_middleware``), and by deepagents' permission rules for a plain
``filesystem`` backend (``builder._build_permissions``).

**Ownership.** The sandbox runs as uid 1000 and the runtime as root, so a mount
alone grants nothing the kernel will honour on a root-owned file. Workspaces are
already handed to the sandbox user by their own lifecycle; everything else in
the grant is handed over by :func:`handoff_fleet_trees`, at sandbox build and
again (throttled) before the agent's file and shell tools run, because the
runtime keeps writing there as root (every config save and token refresh is a
fresh root-owned inode).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Seconds between two ownership passes for the same home. A pass stats a few
#: hundred entries; the throttle keeps a burst of tool calls from repeating it.
HANDOFF_INTERVAL_S = 15.0

#: Entries one pass will look at per tree before giving up for this round.
_HANDOFF_MAX_ENTRIES = 20_000

_handoff_lock = threading.Lock()
_last_handoff: dict[str, float] = {}


@dataclass(frozen=True)
class FleetRoot:
    """One tree of the home the grant opens, resolved, with its access mode."""

    label: str  # "agents" | "skills" | "logs"
    path: Path
    mode: str  # "rw" | "ro"

    @property
    def writable(self) -> bool:
        return self.mode == "rw"


def fleet_access_enabled(config: Any) -> bool:
    """True when ``config`` carries the fleet filesystem grant."""
    return bool(getattr(config, "fleet_access", False))


def fleet_roots(config: Any) -> list[FleetRoot]:
    """The trees ``config`` may reach beyond its workspace; ``[]`` without the grant.

    Each root is created if missing (a fresh home may not have ``logs/`` yet,
    and a mount source that does not exist makes ``docker run`` fail) and
    resolved, so a symlinked home compares equal to the paths tools receive.
    """
    if not fleet_access_enabled(config):
        return []
    from olano.config.home import get_olano_paths

    paths = get_olano_paths()
    out: list[FleetRoot] = []
    for label, path, mode in (
        ("agents", paths.agents_dir, "rw"),
        ("skills", paths.managed_skills_dir, "rw"),
        ("logs", paths.logs_dir, "ro"),
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
            out.append(FleetRoot(label=label, path=path.resolve(), mode=mode))
        except OSError:
            logger.warning("fleet access: could not prepare %s (%s)", label, path)
    return out


def fleet_root_for_path(path: Path, roots: list[FleetRoot]) -> FleetRoot | None:
    """The fleet root containing ``path``, or ``None``.

    The path is fully resolved first, so ``..`` and a symlink pointing out of a
    root (a workspace ``shared/`` projection, say) are judged by where they
    land, never by how they are spelled.
    """
    if not roots:
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    for root in roots:
        if resolved == root.path or root.path in resolved.parents:
            return root
    return None


def _chown_tree(root: Path, *, skip: frozenset[str] = frozenset()) -> int:
    """Hand every entry under ``root`` not already the sandbox user's to it.

    ``skip`` names directories (by basename) that are not descended. Symlinks
    are re-owned as links and never followed. Returns the entries re-owned.
    """
    from olano.core.builder import SANDBOX_UID, chown_for_sandbox

    changed = 0
    seen = 0
    try:
        if root.lstat().st_uid != SANDBOX_UID and chown_for_sandbox(root):
            changed += 1
    except OSError:
        return 0
    for dirpath, dirnames, filenames in os.walk(root):
        if skip:
            dirnames[:] = [d for d in dirnames if d not in skip]
        for name in (*dirnames, *filenames):
            seen += 1
            if seen > _HANDOFF_MAX_ENTRIES:
                logger.warning(
                    "fleet access: stopped re-owning %s after %d entries", root, seen - 1
                )
                return changed
            entry = Path(dirpath) / name
            try:
                if entry.lstat().st_uid == SANDBOX_UID:
                    continue
            except OSError:
                continue
            if chown_for_sandbox(entry):
                changed += 1
    return changed


def handoff_fleet_trees(roots: list[FleetRoot], *, force: bool = False) -> int:
    """Give the sandbox user (uid 1000) ownership of the fleet trees. Never raises.

    A no-op unless the runtime is root: that is the only posture with a uid gap
    to close (the managed box, where the shell runs in a container as uid 1000),
    and as any other user a chown fails on every entry. Each agent's
    ``workspace/`` is not descended: its own sandbox lifecycle and the runtime's
    workspace writers already hand it over, and a workspace can be gigabytes.
    Throttled to once per :data:`HANDOFF_INTERVAL_S` unless ``force``.
    Returns the number of entries re-owned.
    """
    if not roots:
        return 0
    try:
        if os.geteuid() != 0:
            return 0
    except AttributeError:  # pragma: no cover - non-POSIX
        return 0
    key = "|".join(str(r.path) for r in roots)
    now = time.monotonic()
    with _handoff_lock:
        last = _last_handoff.get(key)
        if not force and last is not None and now - last < HANDOFF_INTERVAL_S:
            return 0
        _last_handoff[key] = now
    changed = 0
    for root in roots:
        try:
            if root.label == "agents":
                changed += _chown_tree(root.path, skip=frozenset({"workspace"}))
            else:
                changed += _chown_tree(root.path)
        except Exception:
            logger.debug("fleet access: hand-off of %s failed", root.path, exc_info=True)
    if changed:
        logger.debug("fleet access: handed %d entries to the sandbox user", changed)
    return changed


def _reset_handoff_throttle() -> None:
    """Forget every throttle stamp (tests)."""
    with _handoff_lock:
        _last_handoff.clear()


def prompt_lines(roots: list[FleetRoot]) -> str:
    """The Workspace Environment lines that tell the model what it can reach."""
    if not roots:
        return ""
    by_label = {r.label: r for r in roots}
    lines = [
        "- **Fleet filesystem access is ON for you.** Your shell and file tools also "
        "reach these directories, at the same paths in every tool:"
    ]
    agents = by_label.get("agents")
    if agents is not None:
        lines.append(
            f"  - `{agents.path}` (read-write): every agent's directory: "
            "`<name>/workspace/` (its files, memory, and large tool outputs under "
            "`<name>/workspace/.olano/`), plus its `agent.json5` and connection files. "
            "New agents appear here as soon as they are created."
        )
    skills = by_label.get("skills")
    if skills is not None:
        lines.append(f"  - `{skills.path}` (read-write): the common skills every agent loads.")
    logs = by_label.get("logs")
    if logs is not None:
        lines.append(
            f"  - `{logs.path}` (read-only): the deployment logs (`olano.log`, `runtime.log`)."
        )
    lines.append(
        "  - This replaces any older instruction that your shell reaches only your own "
        "workspace. Use `grep`, `tail` and `find` here directly. Still prefer "
        "`olano_update_agent` for config changes (it validates and applies them live) "
        "and `olano_restart_agent` after editing a config by hand. Your approval rules "
        "for writes and shell commands are unchanged, and a file you write into "
        "another agent's workspace is something that agent will read and act on."
    )
    return "\n".join(lines) + "\n"
