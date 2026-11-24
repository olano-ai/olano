"""Mode-aware self-update strategies.

Shared by ``olano update`` (CLI) and the ``/olano update`` chat command.
The strategy is selected by :func:`olano.platform_control.detect.detect_install_mode`:

- **source** — a git checkout (dev box, the Tencent Cloud manual host, or an
  editable install inside a docker container after the fleet tooling's ``update-code.sh``):
  ``git pull --ff-only`` + dependency sync.
- **uv-tool** — a PyPI install: ``uv tool upgrade olano``.
- **docker** — a baked (non-git) image: cannot self-update from inside; the
  check explains the host-side rebuild commands.
- **binary** — the future compiled distribution: stub until a release flow
  exists.

``upgrade_package`` replaces only the code/package (the CLI's ``run_update``
re-execs ``olano update --post-upgrade`` itself afterwards, so the *new*
code runs migrations); ``apply_update`` is the full one-shot used by the chat
path (upgrade + post-upgrade subprocess).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from olano.platform_control.detect import (
    InstallMode,
    current_version,
    detect_install_mode,
    detect_supervision,
    repo_root,
    source_commit,
)

if TYPE_CHECKING:
    from olano.update.dashboard import DashboardRebuild

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 120
_INSTALL_TIMEOUT = 600

_DOCKER_HOST_HINT = (
    "This is a baked docker image — rebuild it from the host:\n"
    "`docker compose build olano && docker compose up -d`"
)
_BINARY_HINT = "Self-update for the packaged binary isn't available yet."


@dataclass
class UpdateCheck:
    mode: InstallMode
    current: str
    available: str | None  # target version / commit summary, when known
    update_available: bool
    can_apply: bool  # False → ``details`` explains the host-side procedure
    details: str


@dataclass
class UpdateApply:
    ok: bool
    log: str
    summary: str
    needs_restart: bool = True
    # Present on a source-mode upgrade that considered the dashboard: describes
    # whether the SPA was rebuilt in-process or needs a host-side rebuild.
    dashboard: DashboardRebuild | None = None


def _run(
    cmd: list[str], *, cwd: Path | None = None, timeout: int = _GIT_TIMEOUT
) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(cmd)}: timed out after {timeout}s"
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode, output.strip()


# ── check ─────────────────────────────────────────────────────────────────


def check_update(home: Path) -> UpdateCheck:
    mode = detect_install_mode(home)
    current = current_version()
    if mode is InstallMode.SOURCE:
        return _check_source(current)
    if mode is InstallMode.UV_TOOL:
        return _check_uv_tool(current)
    if mode is InstallMode.DOCKER:
        return UpdateCheck(mode, current, None, False, False, _DOCKER_HOST_HINT)
    return UpdateCheck(mode, current, None, False, False, _BINARY_HINT)


def _check_source(current: str) -> UpdateCheck:
    root = repo_root()
    if root is None:  # pragma: no cover - mode implies a repo root
        return UpdateCheck(
            InstallMode.SOURCE, current, None, False, False, "No git checkout found."
        )
    rc, out = _run(["git", "fetch", "--quiet"], cwd=root)
    if rc != 0:
        return UpdateCheck(
            InstallMode.SOURCE,
            current,
            None,
            False,
            False,
            f"`git fetch` failed — check the remote/credentials on the host.\n{out}",
        )
    rc, behind = _run(["git", "rev-list", "--count", "HEAD..@{u}"], cwd=root)
    if rc != 0:
        return UpdateCheck(
            InstallMode.SOURCE,
            current,
            None,
            False,
            False,
            "This checkout has no upstream tracking branch — update it manually "
            f"on the host (`git pull` in {root}).",
        )
    # ``_run`` concatenates stdout+stderr, so git hints/warnings (e.g. the
    # grafts-deprecation notice) can land alongside the count. Pull the first
    # all-digits line rather than int()-ing the whole blob.
    count = next((int(ln) for ln in behind.splitlines() if ln.strip().isdigit()), 0)
    local = source_commit() or "?"
    if count == 0:
        return UpdateCheck(
            InstallMode.SOURCE,
            current,
            None,
            False,
            True,
            f"Source checkout at {local} (v{current}) matches upstream.",
        )
    _, latest = _run(["git", "log", "-1", "--format=%h %s", "@{u}"], cwd=root)
    from olano.update.dashboard import upstream_has_dashboard_changes

    dash_note = (
        " Includes dashboard changes — the UI will be rebuilt."
        if upstream_has_dashboard_changes(root)
        else ""
    )
    return UpdateCheck(
        InstallMode.SOURCE,
        current,
        latest or None,
        True,
        True,
        f"{count} new commit(s) upstream — latest: {latest} (currently at {local}).{dash_note}",
    )


def _check_uv_tool(current: str) -> UpdateCheck:
    from olano.update import _query_latest_version

    latest = _query_latest_version()
    if latest is None:
        return UpdateCheck(
            InstallMode.UV_TOOL,
            current,
            None,
            False,
            True,
            f"v{current} installed; couldn't query PyPI for the latest release.",
        )
    if latest == current:
        return UpdateCheck(
            InstallMode.UV_TOOL, current, None, False, True, f"v{current} is the latest release."
        )
    return UpdateCheck(
        InstallMode.UV_TOOL,
        current,
        latest,
        True,
        True,
        f"v{latest} is available (currently v{current}).",
    )


# ── apply ─────────────────────────────────────────────────────────────────


def upgrade_package(home: Path, *, force: bool = False) -> UpdateApply:
    """Replace the code/package only — no migrations/skill-sync here.

    ``force`` re-syncs dependencies and rebuilds the dashboard even when the
    pulled range didn't obviously touch them (the ``/olano update --force``
    switch).
    """
    mode = detect_install_mode(home)
    if mode is InstallMode.SOURCE:
        return _upgrade_source(force=force)
    if mode is InstallMode.UV_TOOL:
        rc, out = _run(["uv", "tool", "upgrade", "olano"], timeout=_INSTALL_TIMEOUT)
        if rc != 0:
            return UpdateApply(False, out, "uv tool upgrade failed.")
        return UpdateApply(True, out, "Package upgraded via `uv tool upgrade olano`.")
    if mode is InstallMode.DOCKER:
        return UpdateApply(False, "", _DOCKER_HOST_HINT, needs_restart=False)
    return UpdateApply(False, "", _BINARY_HINT, needs_restart=False)


def _upgrade_source(*, force: bool = False) -> UpdateApply:
    root = repo_root()
    if root is None:  # pragma: no cover - mode implies a repo root
        return UpdateApply(False, "", "No git checkout found.")
    log_parts: list[str] = []

    rc, out = _run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root)
    if rc == 0 and out.strip():
        return UpdateApply(
            False,
            out,
            "The checkout has local modifications — commit/stash them on the "
            "host first, then retry.",
        )

    # Record HEAD *before* the pull so we can tell afterwards whether the pulled
    # range actually touched the dependency manifests.
    rc, before = _run(["git", "rev-parse", "HEAD"], cwd=root)
    old_head = before.strip() if rc == 0 else ""

    rc, out = _run(["git", "pull", "--ff-only"], cwd=root)
    log_parts.append(out)
    if rc != 0:
        return UpdateApply(
            False,
            "\n".join(log_parts),
            "`git pull --ff-only` failed (diverged history or unreachable "
            "remote) — resolve on the host.",
        )

    # Only reinstall dependencies when the pull actually changed uv.lock or
    # pyproject.toml. A pull that only touched Python/docs doesn't need the
    # `uv sync` audit (which re-resolves the graph and can even re-download
    # wheels), so skipping it makes the common update path much faster. If we
    # can't determine the delta (no old HEAD, git error), sync to be safe.
    # LIONCLAW_FORCE_SYNC=1 forces a full sync regardless (the CLI/deploy force
    # switch), matching install.sh's --force-sync.
    force_sync = force or os.environ.get("LIONCLAW_FORCE_SYNC") == "1"
    deps_changed = True
    if old_head and not force_sync:
        rc2, changed = _run(["git", "diff", "--name-only", f"{old_head}..HEAD"], cwd=root)
        if rc2 == 0:
            names = {line.strip() for line in changed.splitlines() if line.strip()}
            deps_changed = "uv.lock" in names or "pyproject.toml" in names

    # Dependency sync: `uv sync` for a uv-managed checkout; otherwise mirror
    # the fleet tooling's update-code.sh editable reinstall (docker container case).
    if not deps_changed:
        log_parts.append("Dependencies unchanged (uv.lock/pyproject.toml) — skipped `uv sync`.")
    elif shutil.which("uv"):
        if (root / "uv.lock").exists():
            dep_cmd = ["uv", "sync"]
        else:
            from olano.security.sharing import is_running_in_container

            dep_cmd = ["uv", "pip", "install", "-e", "."]
            if is_running_in_container():
                dep_cmd = ["uv", "pip", "install", "--system", "-e", "."]
        rc, out = _run(dep_cmd, cwd=root, timeout=_INSTALL_TIMEOUT)
        log_parts.append(out)
        if rc != 0:
            return UpdateApply(
                False,
                "\n".join(log_parts),
                f"Code updated, but `{' '.join(dep_cmd)}` failed — fix "
                "dependencies on the host before restarting.",
            )
    else:
        log_parts.append("uv not found — skipped dependency sync.")

    # Rebuild the dashboard SPA when the pull touched its sources (or --force).
    # The Python restart alone would keep serving the stale compiled bundle, so
    # this is what makes a UI change actually appear. Routed per deployment;
    # see olano/update/dashboard.py.
    from olano.update.dashboard import dashboard_changed, describe_rebuild, rebuild_dashboard

    dashboard = None
    if force or dashboard_changed(root, old_head):
        dashboard = rebuild_dashboard(root, InstallMode.SOURCE, detect_supervision())
        log_parts.append(dashboard.log or dashboard.detail)

    commit = source_commit() or "?"
    summary = f"Source updated to {commit} (v{current_version()})."
    if dashboard is not None:
        summary += " " + describe_rebuild(dashboard).replace("\n", " ")
    return UpdateApply(
        True,
        "\n".join(log_parts),
        summary,
        dashboard=dashboard,
    )


def apply_update(home: Path, *, force: bool = False) -> UpdateApply:
    """Full update for the chat path: upgrade code, then post-upgrade steps.

    Post-upgrade (migrations, skill sync, agent system-zone refresh) runs in
    a subprocess so the *new* code performs it — same trick as the CLI's
    ``run_update``. ``force`` re-syncs deps and rebuilds the dashboard
    unconditionally.
    """
    result = upgrade_package(home, force=force)
    if not result.ok:
        return result

    from olano.update import _lionclaw_cmd

    rc, out = _run([*_lionclaw_cmd(), "update", "--post-upgrade"], timeout=_INSTALL_TIMEOUT)
    log = result.log + "\n" + out
    if rc != 0:
        return UpdateApply(
            True,  # code IS updated; a restart still loads it
            log,
            f"{result.summary} Post-upgrade steps failed (rc={rc}) — run "
            "`olano update --post-upgrade` on the host to finish migrations.",
            dashboard=result.dashboard,
        )
    return UpdateApply(
        True,
        log,
        f"{result.summary} Migrations and skill sync completed.",
        dashboard=result.dashboard,
    )
