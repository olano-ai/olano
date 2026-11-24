"""Deployment-aware dashboard (SPA) rebuild.

The Mission Control dashboard is a Vite build served as **static files** from
``olano/dashboard_2.0/dist/`` (git-ignored) by a process *separate* from the
Python runtime — a local ``server.mjs``, Caddy from a web root, or a baked
container. A code update that reloads the Python runtime therefore does NOT
refresh that bundle, so a UI change stays invisible until ``dist/`` is rebuilt
and re-published. That gap is exactly why ``/olano update`` used to pull new
code and restart yet still show the old dashboard.

This module rebuilds the SPA the right way for the detected deployment so the
update path can keep the browser in sync automatically — the operator never has
to know ``npm`` exists. Routing keys off ``(InstallMode, SupervisionMode)`` (the
only deployment facts the runtime can see; see ``platform_control/detect.py``):

- **source + foreground/daemon** (dev / local): run ``deploy/build-dashboard.sh``
  → the checkout ``dist/``. The local ``server.mjs`` re-reads ``dist/`` per
  request, so the new UI is live on the next browser refresh. Fully in-process.
- **source + systemd** (tc-manual-host): run
  ``deploy/tc-manual-host.sh dashboard rebuild`` (build → rsync into the Caddy
  web root → reload Caddy). Attempted in-process; if the privileged publish step
  fails, we surface that one host command instead of failing silently.
- **docker** (tc-manual-docker / baked image): the SPA is a separate Caddy
  container the backend can't touch — surface the host rebuild command.
- **uv-tool** (PyPI): the built bundle ships with the wheel; ``uv tool upgrade``
  already brought it. Nothing to rebuild.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from olano.platform_control.detect import (
    InstallMode,
    SupervisionMode,
    source_commit,
)
from olano.update.strategies import _run

logger = logging.getLogger(__name__)

# npm install (first run) + tsc + vite build can take a couple of minutes on a
# cold cache; be generous so a slow box doesn't spuriously time out mid-build.
_BUILD_TIMEOUT = 900

# Source paths whose changes require a dashboard rebuild. ``dist/`` is
# git-ignored so it never appears in a diff; the build config (vite/tsconfig/
# tailwind) all live under the package dir, and ``assets/`` is art-source that
# ``build-dashboard.sh`` syncs into the SPA before building.
_DASHBOARD_SOURCE_PREFIXES = ("olano/dashboard_2.0/", "assets/")

_DASHBOARD_PKG_REL = "olano/dashboard_2.0"

# What a host operator runs to rebuild the docker dashboard (separate Caddy
# container built from ``docker/dashboard/Dockerfile``).
_DOCKER_REBUILD_CMD = "docker compose -f deploy/docker-compose.allinone.yml up -d --build web"

# Written into ``dist/`` after a successful in-process build so ``status`` can
# tell whether the served bundle matches the current checkout.
_BUILD_MARKER = ".build-meta.json"


@dataclass
class DashboardRebuild:
    """Outcome of a (possibly deferred) dashboard rebuild."""

    attempted: bool  # did the runtime try to build in-process?
    built: bool  # did an in-process build succeed?
    host_command: str | None  # the one-line host command when we couldn't finish
    detail: str  # short human summary
    log: str = ""  # captured build output (for logs / failure tails)

    @property
    def ok(self) -> bool:
        """True when nothing more is needed (built, or genuinely nothing to do)."""
        return self.host_command is None


def dashboard_changed(root: Path, old_head: str) -> bool:
    """True when the pulled range ``old_head..HEAD`` touched dashboard sources.

    Mirrors the dependency-change detection in
    :func:`olano.update.strategies._upgrade_source`. When we can't determine
    the delta (no old HEAD, git error) we return ``True`` so an uncertain update
    rebuilds rather than risk shipping a stale bundle.
    """
    if not old_head:
        return True
    rc, out = _run(["git", "diff", "--name-only", f"{old_head}..HEAD"], cwd=root)
    if rc != 0:
        return True
    for line in out.splitlines():
        name = line.strip()
        if name and any(name.startswith(p) for p in _DASHBOARD_SOURCE_PREFIXES):
            return True
    return False


def upstream_has_dashboard_changes(root: Path) -> bool:
    """True when the not-yet-pulled upstream range touches dashboard sources.

    Used by ``/olano update check`` to preview whether applying the update will
    rebuild the dashboard. Best-effort: returns ``False`` on any git error.
    """
    rc, out = _run(["git", "diff", "--name-only", "HEAD..@{u}"], cwd=root)
    if rc != 0:
        return False
    return any(
        line.strip().startswith(_DASHBOARD_SOURCE_PREFIXES)
        for line in out.splitlines()
        if line.strip()
    )


def _host_rebuild_command(supervision: SupervisionMode) -> str:
    """The host command that rebuilds the dashboard for this supervision mode."""
    if supervision is SupervisionMode.DOCKER:
        return _DOCKER_REBUILD_CMD
    if supervision is SupervisionMode.SYSTEMD:
        return "bash deploy/tc-manual-host.sh dashboard rebuild"
    return "bash deploy/build-dashboard.sh"


def _write_build_marker(dashboard_dir: Path) -> None:
    dist = dashboard_dir / "dist"
    if not dist.is_dir():
        return
    try:
        (dist / _BUILD_MARKER).write_text(
            json.dumps({"commit": source_commit(), "ts": time.time()}),
            encoding="utf-8",
        )
    except OSError as exc:  # marker is best-effort
        logger.debug("dashboard: could not write build marker: %s", exc)


def rebuild_dashboard(
    root: Path | None,
    install_mode: InstallMode,
    supervision: SupervisionMode,
) -> DashboardRebuild:
    """Rebuild the dashboard SPA the right way for the detected deployment.

    Never raises for an expected condition — it always returns a
    :class:`DashboardRebuild` describing what happened (or the host command the
    operator should run when the runtime can't finish the job itself).
    """
    # Docker: the SPA is a separate Caddy container/image the backend can't
    # rebuild from inside (no docker socket). Point at the host command.
    if install_mode is InstallMode.DOCKER or supervision is SupervisionMode.DOCKER:
        return DashboardRebuild(
            attempted=False,
            built=False,
            host_command=_DOCKER_REBUILD_CMD,
            detail="Dashboard runs in a separate container — rebuild it from the host.",
        )

    # No checkout / PyPI install: the compiled bundle ships with the package, so
    # `uv tool upgrade` already refreshed it. Nothing to do.
    if root is None or install_mode is InstallMode.UV_TOOL:
        return DashboardRebuild(
            attempted=False,
            built=False,
            host_command=None,
            detail="Dashboard bundle ships with the package — nothing to rebuild here.",
        )

    dashboard_dir = root / _DASHBOARD_PKG_REL
    if not (dashboard_dir / "package.json").exists():
        return DashboardRebuild(
            attempted=False,
            built=False,
            host_command=None,
            detail="No dashboard sources in this checkout — skipped.",
        )

    if shutil.which("npm") is None:
        return DashboardRebuild(
            attempted=False,
            built=False,
            host_command=_host_rebuild_command(supervision),
            detail="npm isn't on the server's PATH — rebuild the dashboard from the host.",
        )

    # systemd (tc-manual-host): the deploy script builds AND publishes to the
    # Caddy web root (needs privileges for the rsync/chown). Try it; if the
    # publish step is blocked, fall back to surfacing that host command.
    if supervision is SupervisionMode.SYSTEMD:
        script = root / "deploy" / "tc-manual-host.sh"
        if script.exists():
            rc, out = _run(
                ["bash", str(script), "dashboard", "rebuild"],
                cwd=root,
                timeout=_BUILD_TIMEOUT,
            )
            if rc == 0:
                _write_build_marker(dashboard_dir)
                return DashboardRebuild(
                    True, True, None, "Dashboard rebuilt and published to the web root.", out
                )
            return DashboardRebuild(
                attempted=True,
                built=False,
                host_command="bash deploy/tc-manual-host.sh dashboard rebuild",
                detail=(
                    "Built the dashboard, but publishing to the web root needs host "
                    "privileges — run the rebuild from the host to finish."
                ),
                log=out,
            )
        # No deploy script (unusual for a systemd install) — fall through to the
        # plain build below and let the operator wire up publishing.

    # foreground / daemon (dev / local): build straight into the checkout
    # dist/, which the local server.mjs serves live.
    script = root / "deploy" / "build-dashboard.sh"
    if script.exists():
        rc, out = _run(["bash", str(script)], cwd=root, timeout=_BUILD_TIMEOUT)
    else:
        rc, out = _run(["npm", "run", "build"], cwd=dashboard_dir, timeout=_BUILD_TIMEOUT)
    if rc == 0:
        _write_build_marker(dashboard_dir)
        return DashboardRebuild(True, True, None, "Dashboard rebuilt.", out)
    return DashboardRebuild(
        attempted=True,
        built=False,
        host_command=None,
        detail=f"Dashboard build failed (rc={rc}).",
        log=out,
    )


def describe_rebuild(dash: DashboardRebuild) -> str:
    """A chat-ready line describing a rebuild outcome (for update/rebuild replies)."""
    if dash.built:
        return f"🖥️ {dash.detail}"
    if dash.host_command is not None:
        return f"🖥️ {dash.detail}\nHost command: `{dash.host_command}`"
    return f"🖥️ {dash.detail}"


def dashboard_freshness(
    root: Path | None,
    install_mode: InstallMode,
    supervision: SupervisionMode,
) -> str:
    """A short freshness label for ``/olano status`` (never raises)."""
    if install_mode is InstallMode.DOCKER or supervision is SupervisionMode.DOCKER:
        return "served by a separate container (rebuild on the host)"
    if root is None or install_mode is InstallMode.UV_TOOL:
        return "ships with the package"
    dist = root / _DASHBOARD_PKG_REL / "dist"
    if not dist.is_dir():
        return "not built — run `/olano rebuild`"
    try:
        marker = json.loads((dist / _BUILD_MARKER).read_text(encoding="utf-8"))
        built_commit = marker.get("commit")
    except (OSError, ValueError):
        built_commit = None
    if not built_commit:
        return "built (run `/olano rebuild` if the UI looks stale)"
    current = source_commit()
    if current and built_commit == current:
        return "up to date"
    # Built at a different commit — only call it stale if dashboard sources
    # actually changed since then.
    if dashboard_changed(root, built_commit):
        return "stale — run `/olano update` or `/olano rebuild`"
    return "up to date"
