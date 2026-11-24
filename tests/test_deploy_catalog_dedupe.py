"""The deploy scripts must not re-fetch what the running server already fetches.

Every deploy script blocks on "Runtime API is up" before it calls quick-setup.
A running server populates all three catalog-ish things itself:

  * built-in skills          -- ``update/auto.run_startup_upgrades`` on boot
  * the cloud skills catalog -- ``CatalogSyncService`` ~20s after boot
  * the archetype catalog    -- same service, same tick

The scripts used to do that work a second time in the foreground: a
``quick-setup`` without ``--skip-catalogs`` (steps 6 and 7), immediately
followed by ``skills sync`` + ``skills update`` + ``archetypes update``. On a
managed box that re-downloaded the same ~8 MB and paid three extra interpreter
cold starts, delaying every install for work that was about to happen anyway.

It was nearly free before the packs moved to the cloud -- both commands failed
instantly on a managed box for want of a GitHub credential -- which is why the
duplication survived unnoticed. Now that they succeed, it costs real time, so
these tests pin the fix in the two places it can regress: the scripts dropping
the flag, and the flag ceasing to mean what it means.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Every script that runs quick-setup against an ALREADY-RUNNING server. Each
# waits for the API first (asserted below) -- that wait is the precondition
# that makes skipping the catalogs correct rather than merely faster.
DEPLOY_SCRIPTS = [
    "deploy/bootstrap-allinone.sh",
    "deploy/bootstrap-remote.sh",
    "deploy/tc-manual-host.sh",
]

# The three commands whose work the server already does. A script that waited
# for the API has no reason to run any of them.
REDUNDANT_COMMANDS = ["skills sync", "skills update", "archetypes update"]


def _script(rel: str) -> str:
    path = REPO / rel
    assert path.exists(), f"{rel} is gone -- update DEPLOY_SCRIPTS"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("rel", DEPLOY_SCRIPTS)
def test_quick_setup_skips_catalogs(rel: str):
    """quick-setup is invoked with --skip-catalogs, not bare."""
    text = _script(rel)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "quick-setup" not in stripped:
            continue
        # The heredoc'd help text at the end of these scripts mentions
        # quick-setup in prose; only actual invocations carry --yes.
        if "--yes" not in stripped:
            continue
        assert "--skip-catalogs" in stripped, (
            f"{rel}: quick-setup runs without --skip-catalogs, so the install "
            f"re-downloads catalogs the running server is already fetching:\n  {stripped}"
        )


@pytest.mark.parametrize("rel", DEPLOY_SCRIPTS)
def test_no_redundant_catalog_refresh(rel: str):
    """The three refresh commands are gone from the post-quick-setup block."""
    text = _script(rel)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("echo") or stripped.startswith("say "):
            continue
        for cmd in REDUNDANT_COMMANDS:
            assert cmd not in stripped, (
                f"{rel}: `{cmd}` duplicates work the running server does on its own "
                f"(startup upgrades / CatalogSyncService):\n  {stripped}"
            )


@pytest.mark.parametrize("rel", DEPLOY_SCRIPTS)
def test_script_waits_for_the_api_before_quick_setup(rel: str):
    """The precondition for skipping: the server is up, so it can do the work.

    If a script ever stops waiting, --skip-catalogs becomes wrong there and this
    fails rather than silently shipping a box with no skills until the next boot.
    """
    text = _script(rel)
    assert "Runtime API is up" in text, (
        f"{rel}: no longer waits for the runtime API, so --skip-catalogs would "
        f"leave the catalogs to nothing at all"
    )


def test_skip_catalogs_flag_reaches_sync_catalogs():
    """The CLI flag is wired to run_quick_setup's sync_catalogs, inverted."""
    from click.testing import CliRunner

    from olano.cli.main import cli

    seen: dict[str, object] = {}

    def _fake_run_quick_setup(**kwargs):
        seen.update(kwargs)
        raise SystemExit(0)  # stop before any real filesystem work

    import olano.cli.quick_setup as qs

    runner = CliRunner()
    original = qs.run_quick_setup
    qs.run_quick_setup = _fake_run_quick_setup  # type: ignore[assignment]
    try:
        runner.invoke(cli, ["quick-setup", "--yes", "--skip-catalogs"])
        assert seen.get("sync_catalogs") is False, "--skip-catalogs must turn sync_catalogs off"
        seen.clear()
        runner.invoke(cli, ["quick-setup", "--yes"])
        assert seen.get("sync_catalogs") is True, (
            "without the flag quick-setup must still populate the catalogs -- "
            "a CLI install with no server running has no other source"
        )
    finally:
        qs.run_quick_setup = original  # type: ignore[assignment]
