"""Fixtures for the browser-based dashboard E2E suite.

Every fixture degrades to a clean ``pytest.skip`` when a prerequisite is
missing (Node, ``node_modules``, or the Chromium download) so the suite never
hard-fails on a machine that simply hasn't installed the browser tooling.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e import harness

# Load a repo-root .env so live provider keys (e.g. DEEPSEEK_API_KEY) set there
# are picked up by the live tests — mirroring how the CLI/runtime auto-load it.
# Existing environment variables win (override=False), so injected env secrets
# are never clobbered by a stale .env value.
try:
    from dotenv import load_dotenv

    _ENV = Path(__file__).resolve().parents[2] / ".env"
    if _ENV.exists():
        load_dotenv(_ENV, override=False)
except ImportError:  # pragma: no cover - python-dotenv is a runtime dep
    pass


# CI installs Node, node_modules and Chromium explicitly, so a *skip* there is
# not "this laptop lacks the tooling" — it is a broken runner quietly reporting
# green. Setting LIONCLAW_E2E_STRICT=1 turns every prerequisite skip in this
# file into a failure; leave it unset locally, where skipping is the point.
_STRICT = os.environ.get("LIONCLAW_E2E_STRICT", "") not in ("", "0", "false")


def _unavailable(reason: str) -> None:
    """Skip — or, under LIONCLAW_E2E_STRICT, fail — for a missing prerequisite."""
    if _STRICT:
        pytest.fail(f"{reason} (LIONCLAW_E2E_STRICT=1 — prerequisites must be installed)")
    pytest.skip(reason)


# Several test modules predate the shared fixtures and skip on the same
# prerequisites with their own wording. Rather than edit each one, catch the
# reasons centrally: under STRICT, a skip that names missing tooling becomes a
# failure. Deliberate skips (a viewport a test doesn't apply to, a live-provider
# key that isn't set) don't match these phrases and stay skips.
_PREREQ_SKIP_PHRASES = (
    "npm",
    "node_modules",
    "chromium",
    "playwright not installed",
    "dashboard did not come up",
    "dashboard ui tooling missing",
)


def _fail_prerequisite_skips(report) -> None:
    if not _STRICT or not report.skipped:
        return
    # A skip's longrepr is (path, lineno, "Skipped: <reason>").
    reason = str(report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr)
    if any(p in reason.lower() for p in _PREREQ_SKIP_PHRASES):
        report.outcome = "failed"
        report.longrepr = (
            f"{reason}\n\nLIONCLAW_E2E_STRICT=1: this run installs the browser "
            "tooling explicitly, so a missing-prerequisite skip means the runner "
            "is broken — not that the check is inapplicable."
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    _fail_prerequisite_skips(report)
    # The `page` fixture's teardown reads this to decide whether to dump failure
    # artifacts, so it is stashed *after* the promotion above.
    setattr(item, f"_e2e_report_{report.when}", report)


# ── failure artifacts ────────────────────────────────────────────────────────
# The E2E workflow uploads `.olano-tester/**` when the job fails, but nothing in
# this suite ever wrote there: every red run so far arrived with "No files were
# found", leaving a one-line Playwright timeout as the entire record of a
# browser-only failure. A failed `page` test now leaves the picture, the DOM it
# was looking at and the console behind, under the path the workflow already
# collects. The directory is git-ignored, so a local red run costs only disk.
ARTIFACT_DIR = Path(__file__).resolve().parents[2] / ".olano-tester" / "failures"


def _dump_failure_artifacts(node, page, console: list[str]) -> None:
    report = getattr(node, "_e2e_report_call", None) or getattr(node, "_e2e_report_setup", None)
    if report is None or not report.failed:
        return
    out = ARTIFACT_DIR / (re.sub(r"[^A-Za-z0-9._-]+", "-", node.nodeid).strip("-")[:120] or "test")
    try:
        out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / "screenshot.png"), full_page=True)
        (out / "page.html").write_text(page.content(), encoding="utf-8")
        (out / "console.log").write_text("\n".join([f"url: {page.url}", *console]), "utf-8")
    except Exception as exc:  # a page that already died must not mask the failure
        print(f"[e2e] could not capture failure artifacts for {node.nodeid}: {exc}")


@pytest.fixture(scope="session")
def dashboard() -> Iterator[harness.RunningDashboard]:
    """A live dashboard (runtime + Vite UI) shared across the E2E session."""
    if not harness.npm_available():
        _unavailable("npm not on PATH — cannot launch the dashboard UI")
    if not harness.node_modules_present():
        _unavailable(
            "dashboard node_modules missing — run `cd olano/dashboard_2.0 && npm install` first"
        )
    try:
        with harness.launch_dashboard() as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        _unavailable(f"dashboard did not come up: {exc}")


# Viewports the suite runs at. Every test using the ``page`` fixture runs once
# per entry, so a responsive regression at either breakpoint fails loudly.
# "mobile" (iPhone 12/13/14) sits below the dashboard's 900px breakpoint;
# "desktop" is a common laptop size. Tests that are viewport-agnostic and
# expensive can pin one: ``@pytest.mark.parametrize("page", ["desktop"],
# indirect=True)``.
VIEWPORTS: dict[str, dict] = {
    "desktop": {"viewport": {"width": 1440, "height": 900}},
    "mobile": {
        "viewport": {"width": 390, "height": 844},
        "is_mobile": True,
        "has_touch": True,
        "device_scale_factor": 3,
    },
}


@pytest.fixture(autouse=True)
def _seed_agents_into_the_active_home(dashboard: harness.RunningDashboard) -> None:
    """Put the dashboard's agents on disk in *this test's* home.

    The root ``tests/conftest.py`` gives every test a fresh ``LIONCLAW_HOME``
    under its own ``tmp_path``. The E2E runtime shares this process and resolves
    the home from the environment at call time, so it follows that move — which
    means the home the session-scoped dashboard booted with is not the home a
    test's requests read. Read endpoints hid this (they answer from the running
    agent's in-memory config), but every write 404'd with "Agent not found", so
    no E2E test could exercise a save.

    Autouse and ordered after the root fixture: fixtures from a parent conftest
    are set up first, so ``LIONCLAW_HOME`` already points at this test's home
    by the time this runs.

    The onboarding seed rides along for the same reason: the wizard gates
    every fresh admin (single-user included), and a fresh per-test home would
    otherwise re-arm it mid-session — flipping every navigation into the
    wizard instead of the view under test. The wizard's own module re-arms it
    deliberately via ``DELETE /api/onboarding`` after fixtures run.
    """
    harness.seed_agent_configs()
    harness.seed_onboarding_complete()


@pytest.fixture(params=sorted(VIEWPORTS))
def page(request, dashboard: harness.RunningDashboard) -> Iterator[object]:
    """A Playwright page already navigated to the dashboard root.

    Parametrized over :data:`VIEWPORTS` (the param id names the active one).
    Uses the *sync* Playwright API; the test functions are plain ``def`` so they
    run outside any asyncio loop (``asyncio_mode = "auto"`` only wraps ``async``
    tests), which is required for the sync API.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - environment dependent
        _unavailable("playwright not installed")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            _unavailable(f"Chromium unavailable — run `playwright install chromium` ({exc})")
        context = browser.new_context(**VIEWPORTS[request.param])
        pg = context.new_page()
        # Collected for the failure dump below: a view that threw on mount says
        # so here and nowhere else.
        console: list[str] = []
        pg.on("console", lambda m: console.append(f"console.{m.type}: {m.text}"))
        pg.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
        pg.goto(dashboard.base_url, wait_until="domcontentloaded")
        try:
            yield pg
        finally:
            _dump_failure_artifacts(request.node, pg, console)
            context.close()
            browser.close()


# The authenticated shell, named per viewport. The sidebar renders only once the
# auth gate passes, which made it the obvious "app is ready" signal — but below
# the 900px breakpoint it is an off-canvas drawer that is deliberately hidden
# (`visibility: hidden`) until the hamburger opens it, so waiting for it there
# waits forever. The mobile topbar is the same signal at that viewport: it, too,
# mounts only after the gate passes.
#
# Both halves carry ``:visible`` so the union matches only what is actually on
# screen. Without it the selector resolves in DOM order and waits on whichever
# comes first — the topbar hamburger, which is ``display: none`` on desktop.
APP_SHELL_SELECTOR = (
    "aside.sidebar[aria-label='Primary']:visible, "
    "header.mobile-topbar button.mobile-nav-btn:visible"
)


def wait_for_app_shell(page, timeout: int = 15_000) -> None:
    """Block until the authenticated app shell is on screen, at either viewport."""
    page.locator(APP_SHELL_SELECTOR).first.wait_for(state="visible", timeout=timeout)
