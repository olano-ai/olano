"""Boot-time auto-upgrade — apply post-upgrade steps when the running code changed.

``olano update`` and the chat ``/platform update`` already run the
post-upgrade steps (config migrations, built-in skill sync, all-agents
system-zone refresh) after replacing the code. But the most common upgrade
path for source deployments — ``git pull`` on the host followed by a restart —
bypasses both, so existing agents never received a new release's upgrades
unless the operator remembered to run them by hand.

This module closes that gap. The runtime server calls
:func:`run_startup_upgrades` on every boot; when the running code differs from
the last recorded boot (package version *or* git commit — the marker catches
``git pull`` where the version never bumps), or migrations are pending, the
same post-upgrade steps run automatically **before agents boot**, so agents
load the migrated configs and refreshed knowledge docs. A clean re-boot on
unchanged code is a no-op (one JSON read + a migration-ID scan).

Opt out with ``LIONCLAW_NO_AUTO_UPGRADE=1`` (checked by the caller).
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from pathlib import Path

from olano.migrations import pending_migration_ids, run_pending_migrations
from olano.platform_control.detect import current_version, source_commit
from olano.skills.built_in_sync import SyncReport, sync_builtin_skills
from olano.update.install_state import read_install_state, write_install_state
from olano.update.refresh import AgentRefreshResult, refresh_all_agents

logger = logging.getLogger(__name__)


@dataclass
class StartupUpgradeReport:
    """What one boot-time upgrade pass did."""

    marker: str
    previous_marker: str | None
    migrations_applied: list[str] = field(default_factory=list)
    skills: SyncReport | None = None
    agents_refreshed: list[AgentRefreshResult] = field(default_factory=list)

    def summary(self) -> str:
        refreshed = [r.agent for r in self.agents_refreshed if r.changed and not r.error]
        errors = [r.agent for r in self.agents_refreshed if r.error]
        parts = [
            f"code {self.previous_marker or '(first boot)'} → {self.marker}",
            f"{len(self.migrations_applied)} migration(s)"
            + (f" [{', '.join(self.migrations_applied)}]" if self.migrations_applied else ""),
            f"{len(refreshed)} agent system zone(s) refreshed"
            + (f" [{', '.join(refreshed)}]" if refreshed else ""),
        ]
        if errors:
            parts.append(f"{len(errors)} agent(s) failed refresh [{', '.join(errors)}]")
        return "; ".join(parts)


def code_marker() -> str:
    """Identity of the running code: ``<version>@<git-sha>`` for source
    checkouts (the sha catches ``git pull`` upgrades that never bump the
    package version), plain ``<version>`` otherwise."""
    version = current_version()
    commit = source_commit()
    return f"{version}@{commit}" if commit else version


def run_startup_upgrades(home: Path) -> StartupUpgradeReport | None:
    """Run post-upgrade steps iff the code changed or migrations are pending.

    Returns ``None`` on the fast no-op path (unchanged code, nothing pending).
    On success the new marker is persisted to ``.install.json`` so the next
    boot is a no-op; if a migration raises, nothing is recorded and the whole
    pass re-runs on the next boot (migrations are idempotent by contract).
    """
    state = read_install_state(home)
    marker = code_marker()
    pending = pending_migration_ids(state.applied_migrations)
    if marker == state.boot_marker and not pending:
        return None

    report = StartupUpgradeReport(marker=marker, previous_marker=state.boot_marker)

    # Skill sync and agent refresh are best-effort: a failure there shouldn't
    # block migrations or the boot. Migrations are NOT wrapped — a failing
    # migration must not be recorded as applied, and the caller logs it.
    try:
        report.skills = sync_builtin_skills(home)
    except Exception as exc:
        logger.warning("startup upgrade: built-in skill sync failed: %s", exc)
    # Skill trees written 0700 by an earlier release are unreadable from the
    # sandbox lane (uid 1000); the catalog sync only revisits a skill when its
    # revision moves, so this hook -- which runs exactly once per new code
    # marker -- is what repairs boxes that already have them.
    try:
        from olano.skills.sync import repair_skill_permissions

        repaired = repair_skill_permissions(home)
        if repaired:
            logger.info("startup upgrade: re-opened %d skill dir(s) to the agent lanes", repaired)
    except Exception as exc:
        logger.warning("startup upgrade: skill permission repair failed: %s", exc)
    try:
        from olano.skills.sync import retire_proprietary_skill_copies

        retired = retire_proprietary_skill_copies(home)
        if retired:
            logger.info("startup upgrade: removed retired skill copies: %s", ", ".join(retired))
    except Exception as exc:
        logger.warning("startup upgrade: retiring old skill copies failed: %s", exc)

    report.migrations_applied = run_pending_migrations(home, state.applied_migrations)

    try:
        report.agents_refreshed = refresh_all_agents(source="auto", check=False)
    except Exception as exc:
        logger.warning("startup upgrade: agent system-zone refresh failed: %s", exc)

    if report.migrations_applied:
        state.applied_migrations.extend(report.migrations_applied)
    version = current_version()
    if state.version != version:
        if state.version:
            state.history.append({"version": state.version, "installed_at": state.installed_at})
        state.version = version
        state.installed_at = datetime.datetime.now(datetime.UTC).isoformat()
    state.boot_marker = marker
    write_install_state(home, state)
    return report
