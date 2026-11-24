"""olano update — self-update logic."""

from __future__ import annotations

import datetime
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from olano.skills.built_in_sync import SyncReport
from olano.update.install_state import (
    InstallState,
    read_install_state,
    record_version,
    write_install_state,
)
from olano.update.refresh import AgentRefreshResult

if TYPE_CHECKING:
    from olano.update.dashboard import DashboardRebuild

logger = logging.getLogger(__name__)


@dataclass
class UpdateReport:
    from_version: str | None
    to_version: str | None
    package_upgraded: bool = False
    skills: SyncReport = field(default_factory=lambda: SyncReport(0, 0, 0, 0, 0))
    migrations_applied: list[str] = field(default_factory=list)
    agents_refreshed: list[AgentRefreshResult] = field(default_factory=list)
    docker_rebuilt: bool = False
    check_only: bool = False
    rolled_back: bool = False
    # Dashboard-rebuild outcome from a source-mode upgrade (None when not
    # considered — e.g. check/rollback/legacy paths).
    dashboard: DashboardRebuild | None = None


def _current_version() -> str:
    try:
        from importlib.metadata import version

        return version("olano")
    except Exception:
        return "unknown"


def _run_post_upgrade(
    home: Path,
) -> tuple[SyncReport, list[str], list[AgentRefreshResult]]:
    """Run skill sync, migrations, and an all-agents system-zone refresh.

    Called after the package has been upgraded, so a new release's system
    prompts / knowledge docs propagate to every existing agent automatically.
    """
    from olano.migrations import run_pending_migrations
    from olano.skills.built_in_sync import sync_builtin_skills
    from olano.update.auto import code_marker
    from olano.update.refresh import refresh_all_agents

    skills = sync_builtin_skills(home)
    # Overlay the external skills catalog (olano-ai/skills) so an upgrade also
    # pulls newly published/updated common skills. Best-effort — a missing
    # git/network must never fail the upgrade; the package baseline still synced.
    try:
        from olano.skills.sync import fetch_olano_skills

        fetch_olano_skills(home, quiet=True)
    except Exception:
        logger.debug("Skill-pack refresh skipped during post-upgrade", exc_info=True)
    state = read_install_state(home)
    new_migrations = run_pending_migrations(home, state.applied_migrations)
    state.applied_migrations.extend(new_migrations)
    # Stamp the code identity so the runtime's boot-time auto-upgrade
    # (olano/update/auto.py) recognizes this work as already done.
    state.boot_marker = code_marker()
    write_install_state(home, state)
    agents_refreshed = refresh_all_agents(source="home", check=False)
    return skills, new_migrations, agents_refreshed


def run_update(
    home: Path,
    *,
    check: bool = False,
    rollback: bool = False,
    post_upgrade: bool = False,
) -> UpdateReport:
    """
    Main update entry point.

    - check=True: dry-run, no writes.
    - rollback=True: re-install previous version.
    - post_upgrade=True: run skills sync + migrations after package upgrade.
    """
    state = read_install_state(home)
    from_version = state.version

    if post_upgrade:
        new_version = _current_version()
        skills, migrations, agents_refreshed = _run_post_upgrade(home)
        now = datetime.datetime.now(datetime.UTC).isoformat()
        record_version(home, new_version, now)
        return UpdateReport(
            from_version=from_version,
            to_version=new_version,
            package_upgraded=True,
            skills=skills,
            migrations_applied=migrations,
            agents_refreshed=agents_refreshed,
        )

    if rollback:
        if not state.history:
            return UpdateReport(from_version=from_version, to_version=None, rolled_back=True)
        prev = state.history[-1].get("version")
        if not check:
            _upgrade_package(home, state, pin_version=prev)
        return UpdateReport(
            from_version=from_version,
            to_version=prev,
            package_upgraded=not check,
            rolled_back=True,
            check_only=check,
        )

    if check:
        from olano.update.strategies import check_update

        result = check_update(home)
        return UpdateReport(
            from_version=from_version,
            # When up to date, echo the current version rather than None (the CLI
            # renders None as "unknown", which reads as a failed lookup).
            to_version=result.available if result.update_available else from_version,
            check_only=True,
        )

    # Full upgrade: upgrade package (which also rebuilds the dashboard for a
    # source install) then re-exec --post-upgrade. If the package upgrade
    # refused/failed (e.g. a dirty source checkout), do NOT run the post-upgrade
    # migrations or claim success against unchanged code.
    from olano.update.strategies import upgrade_package

    apply_result = upgrade_package(home)
    if not apply_result.ok:
        logger.warning("Package upgrade failed: %s\n%s", apply_result.summary, apply_result.log)
        return UpdateReport(
            from_version=from_version,
            to_version=from_version,
            package_upgraded=False,
            dashboard=apply_result.dashboard,
        )
    # Re-exec the CLI with --post-upgrade so the new package code runs.
    subprocess.run([*_lionclaw_cmd(), "update", "--post-upgrade"], check=False)
    return UpdateReport(
        from_version=from_version,
        to_version=_current_version(),
        package_upgraded=True,
        dashboard=apply_result.dashboard,
    )


def _query_latest_version() -> str | None:
    try:
        result = subprocess.run(
            ["uv", "pip", "index", "versions", "olano"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            if "Available versions:" in line or line.strip().startswith("olano"):
                # Parse first version listed
                parts = line.split()
                for part in parts:
                    if part[0].isdigit():
                        return part.strip("(),")
    except Exception:
        pass
    return None


def _upgrade_package(home: Path, state: InstallState, pin_version: str | None = None) -> bool:
    """Replace the package/code. Returns True on success (best-effort True for
    rollback, which is fire-and-forget)."""
    if pin_version:
        # Rollback: only meaningful for PyPI installs (a source checkout
        # rolls back with git on the host).
        subprocess.run(
            ["uv", "tool", "install", f"olano=={pin_version}"],
            check=False,
        )
        return True
    # Mode-aware upgrade (source checkout → git pull; PyPI → uv tool
    # upgrade; docker/binary → explanatory no-op). Replaces the old
    # docker-vs-uv-tool branch that wrongly ran `uv tool upgrade` on git
    # checkouts.
    from olano.update.strategies import upgrade_package

    result = upgrade_package(home)
    if not result.ok:
        logger.warning("Package upgrade failed: %s\n%s", result.summary, result.log)
    return result.ok


def _lionclaw_cmd() -> list[str]:
    """Argv prefix that invokes the olano CLI.

    Prefers the ``olano`` console script; when it isn't on PATH (some
    source/venv layouts) falls back to ``python -m olano`` — NOT bare
    ``sys.executable``, which would treat ``update`` as a script name and fail.
    """
    import shutil

    found = shutil.which("olano")
    if found:
        return [found]
    return [sys.executable, "-m", "olano"]


def _lionclaw_binary() -> str:
    import shutil

    found = shutil.which("olano")
    return found or sys.executable
