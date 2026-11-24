"""Auto-fetch the canonical common-skills repo (`olano-ai/skills`) into the common skills dir.

The repo is cloned into ``<home>/skill-packs/olano-skills/`` (a cache), and each
skill under its ``skills/`` directory is copied into the deployment's **common
skills directory** (``<home>/skills/`` — ``managed_skills_dir``), where every
agent picks it up. This mirrors how ``archetypes/sync.py`` fetches the canonical
agent pack (``olano-agents``) into ``<home>/archetypes/``.

Copied skills carry a distinct ``.olano-skillpack`` sentinel so they coexist
with the package-bundled built-in skills (``built_in_sync.sync_builtin_skills``):
built-in sync leaves skill-pack dirs untouched, and this overlay leaves user
``.olano-skill-pinned`` dirs untouched.

Where the pack comes from (``fetch_olano_skills``):

  1. An explicit ``url`` argument or ``LIONCLAW_SKILLS_REPO_URL`` → ``git``.
  2. Otherwise, on a **managed box** (a deployment token is present) → the cloud
     skills catalog (``olano.catalog.skills_registry``, token-gated
     ``GET /api/v1/skills``). The repo is private and a managed box holds no
     GitHub credential, so the default ``git clone`` below failed on every one
     of them, silently; the cloud path is the only one that can work there,
     and git is never attempted without an explicit URL.
  3. Otherwise (unmanaged, no token) → ``git`` with ``_DEFAULT_REPO_URL``, which
     works wherever a GitHub credential exists.

Whatever the transport, the result lands in the same clone directory and the
same overlay copies it into the common skills dir.

All failures are non-fatal — the package-bundled built-in skills
(``olano/skills/built_in/``) remain available even when the external pack
can't be fetched.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from olano.catalog.skills_registry import normalize_skill_perms
from olano.config.home import get_olano_paths
from olano.skills.built_in_sync import (
    SyncReport,
    _dir_content_hash,
    _has_managed_sentinel,
    _list_builtin_skills,
    is_pinned,
    is_retired_proprietary_copy,
    is_skillpack,
    mark_skillpack,
)

logger = logging.getLogger(__name__)

# Default canonical common-skills repo. Override at runtime with --url or
# LIONCLAW_SKILLS_REPO_URL. Set to None / empty to disable auto-fetch entirely.
_DEFAULT_REPO_URL = "https://github.com/olano-ai/skills.git"

# Subdirectory under <home>/skill-packs/ that holds the canonical pack clone.
_PACK_DIRNAME = "olano-skills"

# Subdirectory inside the cloned repo that holds the individual skill folders.
_REPO_SKILLS_SUBDIR = "skills"


def _resolve_repo_url(url: str | None = None) -> str | None:
    if url:
        return url.strip() or None
    env = os.environ.get("LIONCLAW_SKILLS_REPO_URL")
    if env:
        return env.strip() or None
    return _DEFAULT_REPO_URL or None


def _git_available() -> bool:
    return shutil.which("git") is not None


def _home_root(home: str | Path | None = None) -> Path:
    """Resolve the Olano home root, accepting an explicit override for tests."""
    if home is not None:
        return Path(home)
    return get_olano_paths().root


def olano_skills_pack_dir(home: str | Path | None = None) -> Path:
    """Where the canonical ``olano-ai/skills`` pack is cloned once fetched."""
    return _home_root(home) / "skill-packs" / _PACK_DIRNAME


def common_skills_dir(home: str | Path | None = None) -> Path:
    """The common skills directory the pack is copied into (``<home>/skills/``)."""
    return _home_root(home) / "skills"


def is_pack_installed(home: str | Path | None = None) -> bool:
    """A pack is on disk — from a git clone or from the cloud catalog."""
    pack = olano_skills_pack_dir(home)
    if (pack / ".git").exists():
        return True
    skills = pack / _REPO_SKILLS_SUBDIR
    return skills.is_dir() and any(
        (d / "SKILL.md").exists() for d in skills.iterdir() if d.is_dir()
    )


def _managed() -> bool:
    """A deployment token means a managed box: cloud catalog, never a bare git."""
    try:
        from olano.deployment.sources import deployment_token

        return deployment_token() is not None
    except Exception:
        return False


def _copy_pack_skills(pack_skills_dir: Path, target_root: Path) -> SyncReport:
    """Copy each skill from a cloned pack's ``skills/`` dir into ``target_root``.

    Idempotent (unchanged content is skipped via a content hash). Copied dirs are
    marked with ``.olano-skillpack``; user ``.olano-skill-pinned`` dirs are
    left completely untouched. Skill-pack dirs no longer present in the pack are
    pruned (only ones carrying the skill-pack sentinel — never user skills).
    """
    target_root.mkdir(parents=True, exist_ok=True)

    pack_names: set[str] = set()
    new = updated = unchanged = pinned = 0

    for src_skill in _list_builtin_skills(pack_skills_dir):
        if is_retired_proprietary_copy(src_skill):
            # A pack on disk that predates the Olano-authored replacement. Not
            # installed, and left out of `pack_names` so the prune below takes
            # away the copy an earlier overlay put there.
            continue
        name = src_skill.name
        pack_names.add(name)
        dst_skill = target_root / name

        if dst_skill.exists() and is_pinned(dst_skill):
            pinned += 1
            continue

        # copytree preserves the source's mode bits, and the pack copy used to
        # arrive 0700 (catalog/skills_registry.normalize_skill_perms explains
        # the root cause). Every branch below therefore ends by normalizing
        # the destination -- the UNCHANGED branch included, which is the only
        # way an already-affected box gets repaired: its content hash matches,
        # so nothing else here would touch the directory again. The same branch
        # re-marks a pre-rename sentinel under its current name.
        if not dst_skill.exists():
            shutil.copytree(src_skill, dst_skill)
            mark_skillpack(dst_skill)
            normalize_skill_perms(dst_skill)
            new += 1
            continue

        # An existing dir may currently be a built-in copy (different sentinel).
        # Overlay it whenever the content differs OR it isn't yet skill-pack
        # owned, so ownership transfers to the (richer) external pack.
        src_hash = _dir_content_hash(src_skill)
        dst_hash = _dir_content_hash(dst_skill)
        if src_hash == dst_hash and is_skillpack(dst_skill):
            mark_skillpack(dst_skill)
            normalize_skill_perms(dst_skill)
            unchanged += 1
        else:
            shutil.rmtree(dst_skill)
            shutil.copytree(src_skill, dst_skill)
            mark_skillpack(dst_skill)
            normalize_skill_perms(dst_skill)
            updated += 1

    # Remove stale skill-pack skills that were previously copied from a pack but
    # are no longer present in it. Only touches skill-pack-sentinel dirs, so
    # built-in and user-authored skills are never deleted here, and never a
    # pinned one: the pin is the owner keeping it.
    removed = 0
    for dst_skill in sorted(target_root.iterdir()):
        if (
            dst_skill.is_dir()
            and dst_skill.name not in pack_names
            and is_skillpack(dst_skill)
            and not is_pinned(dst_skill)
        ):
            shutil.rmtree(dst_skill)
            removed += 1

    return SyncReport(new=new, updated=updated, unchanged=unchanged, removed=removed, pinned=pinned)


def repair_skill_permissions(home: str | Path | None = None) -> int:
    """Re-open every common skill to the agent lanes; returns how many were touched.

    The startup-upgrade hook runs this so a box that already holds 0700 skill
    directories (every managed box that synced the cloud catalog before the
    fix -- see ``catalog/skills_registry.normalize_skill_perms``) is repaired
    at its next update, without waiting for a catalog revision that may never
    move. Pinned skills are the owner's and are left alone, as everywhere.
    Best-effort: never raises.
    """
    root = common_skills_dir(home)
    if not root.is_dir():
        return 0
    touched = 0
    for skill in sorted(root.iterdir()):
        if not skill.is_dir() or is_pinned(skill):
            continue
        try:
            normalize_skill_perms(skill)
            touched += 1
        except Exception:  # pragma: no cover - chmod failures are already swallowed
            logger.debug("permission repair skipped for %s", skill, exc_info=True)
    return touched


def retire_proprietary_skill_copies(home: str | Path | None = None) -> list[str]:
    """Remove the Anthropic document skills an earlier release synced; returns their names.

    The built-in removal pass already takes the package's own copies, and the
    pack overlay no longer installs one, but a pack copy stays until the next
    overlay runs, which on a managed box is the next catalog revision. The
    startup-upgrade hook runs this so the first boot of a release that no
    longer ships them clears them at once. Only a sync-managed copy goes (a
    built-in or skill-pack marker), never a pinned one and never a skill that
    carries no marker: those are the owner's. Best-effort: never raises.
    """
    root = common_skills_dir(home)
    if not root.is_dir():
        return []
    removed: list[str] = []
    for skill in sorted(root.iterdir()):
        try:
            if (
                skill.is_dir()
                and is_retired_proprietary_copy(skill)
                and (is_skillpack(skill) or _has_managed_sentinel(skill))
                and not is_pinned(skill)
            ):
                shutil.rmtree(skill)
                removed.append(skill.name)
        except OSError:
            logger.debug("could not retire %s", skill, exc_info=True)
    return removed


def _git_clone_or_update(
    target: Path, repo_url: str, *, update_if_exists: bool
) -> tuple[bool, str]:
    """Clone ``repo_url`` into ``target`` (or ``git pull`` if already cloned)."""
    if (target / ".git").exists():
        if not update_if_exists:
            return True, f"Skill pack already installed at {target}."
        cmd = ["git", "-C", str(target), "pull", "--ff-only"]
        action = "update"
    else:
        # Refuse to clone over a non-empty non-git dir to avoid clobbering work.
        if target.exists() and any(target.iterdir()):
            return False, (
                f"Refusing to clone into non-empty {target} (no .git found). "
                "Move or delete the directory and re-run."
            )
        cmd = ["git", "clone", "--depth", "1", repo_url, str(target)]
        action = "clone"

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, f"git {action} timed out after 120s."
    except OSError as exc:
        return False, f"git {action} failed: {exc}"

    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip().splitlines()
        last = msg[-1] if msg else f"exit {result.returncode}"
        return False, f"git {action} failed: {last}"

    return True, f"Skill pack {action}d at {target}."


def fetch_olano_skills(
    home: str | Path | None = None,
    *,
    url: str | None = None,
    update_if_exists: bool = True,
    quiet: bool = False,
) -> tuple[bool, str]:
    """Clone/update the canonical common-skills pack and copy it into the common skills dir.

    Returns ``(ok, message)``. ``ok=True`` on a successful clone+copy, a
    successful pull+copy, a copy from an already-present clone (even when the
    network pull failed), or a deliberate skip when no URL is configured.
    ``ok=False`` only when git is missing, or a configured fetch fails and no
    prior clone exists to copy from.
    """
    pack_dir = olano_skills_pack_dir(home)
    pack_dir.parent.mkdir(parents=True, exist_ok=True)
    target_skills = common_skills_dir(home)
    pack_skills_dir = pack_dir / _REPO_SKILLS_SUBDIR

    explicit_url = (
        _resolve_repo_url(url) if (url or os.environ.get("LIONCLAW_SKILLS_REPO_URL")) else None
    )
    if explicit_url is None and _managed():
        # Managed box: the cloud catalog is the transport. The default git URL
        # is a private repo this box cannot read, so a clone is not tried.
        from olano.catalog import skills_registry

        synced = skills_registry.refresh_from_cloud(home)
        if synced:
            return True, (
                f"Skills catalog synced from the cloud: {synced['count']} skills "
                f"(revision {synced['revision']}). {synced['overlay']}"
            )
        if pack_skills_dir.exists():
            # Cloud unreachable right now: serve what the last sync (or an old
            # clone) left on disk, and say so.
            report = _copy_pack_skills(pack_skills_dir, target_skills)
            return (
                True,
                f"Cloud skills catalog unavailable; using the pack already on disk. {report}",
            )
        return False, (
            "Cloud skills catalog unavailable and no pack on disk yet "
            "(the catalog sync retries on its schedule; `olano catalog refresh` forces it)."
        )

    if not _git_available():
        return False, "git not found on PATH; install git to fetch the skills pack."

    repo_url = explicit_url or _resolve_repo_url(None)
    if not repo_url:
        return True, "No skills repo URL configured; skipping fetch."

    git_ok, git_msg = _git_clone_or_update(pack_dir, repo_url, update_if_exists=update_if_exists)

    if not pack_skills_dir.exists():
        # Nothing to copy — surface whatever the git step said.
        return git_ok, git_msg

    report = _copy_pack_skills(pack_skills_dir, target_skills)
    if not quiet:
        logger.info("Skill pack copied into %s (%s)", target_skills, report)

    # If the git step failed but a prior clone let us copy, treat as a soft-OK so
    # a stale-but-present pack still populates the common skills dir.
    ok = git_ok or report.total_builtin > 0
    message = f"{git_msg} {report}"
    return ok, message
