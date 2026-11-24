"""Auto-fetch and update the canonical archetype repo (`olano-agents`).

The repo is cloned into `<home>/archetypes/olano-agents/` so the archetype
loader (`installer._archetype_search_dirs`) picks it up automatically.

Where the pack comes from (``fetch_lionclaw_agents``):

  1. An explicit `--url` / `LIONCLAW_AGENTS_REPO_URL` → `git`.
  2. Otherwise the cloud catalog (`olano.catalog.agents_registry`, public
     `GET /api/v1/agents`), materialized into `<home>/catalog/archetypes/`,
     which the loader already ranks above this clone. The repo is private and
     a managed box holds no GitHub credential, so the default `git clone`
     failed on every one of them, silently.
  3. On an unmanaged box (no deployment token) whose cloud fetch failed →
     `git` with `_DEFAULT_REPO_URL`, which works wherever a credential exists.
     A managed box never tries the bare clone.

All failures are non-fatal — the bundled archetypes (`olano/archetypes/templates/`)
remain available even when the external pack can't be fetched.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

# Default canonical archetype repo. Override at runtime with --url or
# LIONCLAW_AGENTS_REPO_URL. Set to None / empty to disable auto-fetch entirely.
_DEFAULT_REPO_URL = "https://github.com/olano-ai/olano-agents.git"

# Subdirectory under <home>/archetypes/ that holds the canonical pack clone.
_PACK_DIRNAME = "olano-agents"


def _resolve_repo_url(url: str | None = None) -> str | None:
    if url:
        return url.strip() or None
    env = os.environ.get("LIONCLAW_AGENTS_REPO_URL")
    if env:
        return env.strip() or None
    return _DEFAULT_REPO_URL or None


def _git_available() -> bool:
    return shutil.which("git") is not None


def lionclaw_agents_pack_dir() -> Path:
    """Where the canonical `olano-agents` pack lives once cloned."""
    return get_olano_paths().external_archetypes_dir / _PACK_DIRNAME


def is_pack_installed() -> bool:
    """The canonical pack is on disk — a git clone or the cloud catalog copy."""
    if (lionclaw_agents_pack_dir() / ".git").exists():
        return True
    try:
        from olano.catalog.agents_registry import materialized_dir

        return any(materialized_dir().glob("*.json5"))
    except Exception:
        return False


def _managed() -> bool:
    try:
        from olano.deployment.sources import deployment_token

        return deployment_token() is not None
    except Exception:
        return False


def fetch_lionclaw_agents(
    *,
    url: str | None = None,
    update_if_exists: bool = True,
    quiet: bool = False,
) -> tuple[bool, str]:
    """Clone or update the canonical archetype pack.

    Returns ``(ok, message)``. ``ok=True`` on a successful clone, successful
    pull, or a deliberate skip when no URL is configured. ``ok=False`` only
    when git is missing or a configured fetch fails.
    """
    target = lionclaw_agents_pack_dir()
    target.parent.mkdir(parents=True, exist_ok=True)

    explicit_url = (
        _resolve_repo_url(url) if (url or os.environ.get("LIONCLAW_AGENTS_REPO_URL")) else None
    )
    if explicit_url is None:
        from olano.catalog import agents_registry

        synced = agents_registry.refresh_from_cloud()
        if synced:
            return True, (
                f"Archetype catalog synced from the cloud: {synced['count']} archetypes "
                f"(revision {synced['revision']}) at {agents_registry.materialized_dir()}."
            )
        if _managed():
            # Managed box: no GitHub credential by design, so a clone of the
            # private default repo cannot succeed. Report the cloud outcome.
            if is_pack_installed():
                return True, "Cloud archetype catalog unavailable; using the pack already on disk."
            return False, (
                "Cloud archetype catalog unavailable and no pack on disk yet "
                "(the catalog sync retries on its schedule; `olano catalog refresh` forces it)."
            )

    if not _git_available():
        return False, "git not found on PATH; install git to fetch archetype packs."

    repo_url = explicit_url or _resolve_repo_url(None)
    if not repo_url:
        return True, "No archetype repo URL configured; skipping fetch."

    if (target / ".git").exists():
        if not update_if_exists:
            return True, f"Pack already installed at {target}."
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
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, f"git {action} timed out after 120s."
    except OSError as exc:
        return False, f"git {action} failed: {exc}"

    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip().splitlines()
        last = msg[-1] if msg else f"exit {result.returncode}"
        return False, f"git {action} failed: {last}"

    # The pack IS the archetype catalog for most installs, so a successful
    # clone/pull is exactly the moment the compiled catalog stops describing
    # what is on disk. Drop it here rather than waiting for the fingerprint
    # throttle, so `olano archetypes update` is visible in the dashboard
    # immediately instead of up to 30s later.
    try:
        from olano.archetypes.catalog_index import invalidate

        invalidate()
    except Exception as exc:  # never fail a successful fetch on a cache drop
        logger.debug("archetype catalog invalidation skipped: %s", exc)

    if not quiet:
        logger.info("Archetype pack %sd at %s", action, target)
    return True, f"Pack {action}d at {target}."
