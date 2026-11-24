#!/usr/bin/env python3
"""Drive the Olano Mission Control dashboard in a real browser and report.

This is the runnable half of the `olano-tester` skill. It opens Chromium
(via Playwright), walks a checklist against the live dashboard, screenshots each
step, and prints a PASS/FAIL summary. Use it to *verify* GUI behaviour that
in-process API tests can't see (React rendering, routing, the auth gate).

Usage
-----
  # Against an already-running dashboard (e.g. you ran `olano dashboard`):
  python verify_dashboard.py --url http://localhost:3200

  # Self-contained: spin up a throwaway runtime + Vite UI, test it, tear down
  # (must be run from inside the olano repo — reuses tests/e2e/harness.py):
  python verify_dashboard.py --launch

  # Watch it happen in a headed browser:
  python verify_dashboard.py --url http://localhost:3200 --headed

Environment
-----------
  LIONCLAW_PW_EXECUTABLE   Path to a Chromium binary, when the Playwright-managed
                           download is unavailable (e.g. a pre-baked CI image
                           whose browser build doesn't match the pip package).
  LIONCLAW_USER /          Credentials to sign in when the dashboard runs in
  LIONCLAW_PASS            multi-user mode (a login gate instead of single-user
                           auto-login). When both are set and the sign-in form is
                           present, the checklist authenticates before walking the
                           routes. Ignored in single-user mode (no form appears).

Exit code is 0 only if every check passes.

The route checklist itself lives in ``tests/e2e/route_checks.py`` and is also
run by ``tests/e2e/test_route_checks.py`` (pytest, both viewports) — which is
what CI executes, via the ``e2e`` workflow, whenever the dashboard changes. Add
new routes there, not here. This script stays the interactive way to *watch* the
same checklist and collect screenshots.
"""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

TITLE = "Olano"

# The route checklist lives in the test suite, not here, so the pytest CI runs
# (tests/e2e/test_route_checks.py) and this hand-run script can never disagree
# about what "the dashboard renders" means. Add new routes there.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
try:
    from tests.e2e.route_checks import (
        ROUTE_CHECKS,
        SHELL_SELECTOR,
        close_mobile_nav,
        open_mobile_nav,
    )
except ImportError as _exc:  # pragma: no cover - only outside a repo checkout
    print(
        f"cannot import the route checklist ({_exc}).\n"
        "This script reads tests/e2e/route_checks.py — run it from inside the "
        "olano repo.",
        file=sys.stderr,
    )
    raise SystemExit(2) from None


# Browser-context kwargs per viewport. "mobile" (iPhone 12/13/14) sits below the
# dashboard's 900px breakpoint so the checklist + screenshots exercise the
# responsive layout; "desktop" matches a common laptop.
VIEWPORT_KWARGS: dict[str, dict] = {
    "desktop": {"viewport": {"width": 1440, "height": 900}},
    "mobile": {
        "viewport": {"width": 390, "height": 844},
        "is_mobile": True,
        "has_touch": True,
        "device_scale_factor": 3,
    },
}


class Report:
    """Collects check results and renders a terminal-friendly summary."""

    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def record(self, ok: bool, name: str, detail: str = "") -> None:
        self.rows.append((ok, name, detail))
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {name}"
        if detail:
            line += f" — {detail}"
        print(line, flush=True)

    @property
    def ok(self) -> bool:
        return all(ok for ok, _, _ in self.rows)


def _launch_browser(pw, headed: bool):
    kwargs: dict = {"headless": not headed}
    exe = os.environ.get("LIONCLAW_PW_EXECUTABLE")
    if exe:
        kwargs["executable_path"] = exe
        kwargs["args"] = ["--no-sandbox"]
    return pw.chromium.launch(**kwargs)


def run_checks(base_url: str, screenshot_dir: Path, headed: bool, viewports: list[str]) -> Report:
    from playwright.sync_api import sync_playwright

    screenshot_dir.mkdir(parents=True, exist_ok=True)
    report = Report()

    with sync_playwright() as pw:
        browser = _launch_browser(pw, headed)
        try:
            for vp in viewports:
                context = browser.new_context(**VIEWPORT_KWARGS[vp])
                page = context.new_page()
                try:
                    _run_checklist(page, base_url, report, screenshot_dir, vp)
                finally:
                    context.close()
        finally:
            browser.close()

    return report


def _run_checklist(page, base_url: str, report: Report, screenshot_dir: Path, vp: str) -> None:
    """Walk the full checklist once at the given viewport, prefixing names."""

    def shot(name: str, page) -> None:
        path = screenshot_dir / f"{vp}-{name}.png"
        page.screenshot(path=str(path), full_page=True)

    label = f"[{vp}] "
    page.goto(base_url, wait_until="domcontentloaded", timeout=20_000)

    # 1. SPA serves with the right title.
    title = page.title()
    report.record(title == TITLE, label + "page loads", f"title={title!r}")
    shot("01-loaded", page)

    # 2. React actually mounts content into #root.
    try:
        page.wait_for_function(
            "document.getElementById('root')?.children.length > 0", timeout=15_000
        )
        report.record(True, label + "react mounts")
    except Exception as exc:
        report.record(False, label + "react mounts", str(exc).splitlines()[0])

    # 2b. Multi-user mode shows a sign-in gate instead of single-user
    # auto-login. If credentials are supplied via env and the form is
    # present, authenticate so the shell + route checks can proceed.
    cred_user = os.environ.get("LIONCLAW_USER")
    cred_pass = os.environ.get("LIONCLAW_PASS")
    email_field = page.locator("form.auth-card input[type='email']")
    if cred_user and cred_pass and email_field.count() > 0:
        try:
            email_field.first.fill(cred_user)
            page.locator("form.auth-card input[type='password']").first.fill(cred_pass)
            page.locator("form.auth-card button.btn.primary").first.click()
            # Login resolves → the auth card is torn down and the shell mounts.
            page.locator("form.auth-card").wait_for(state="detached", timeout=15_000)
            report.record(True, label + "auth → signed in", cred_user)
        except Exception as exc:
            report.record(False, label + "auth → signed in", str(exc).splitlines()[0])
            shot("98-login-failure", page)

    # 3. Auth gate resolves to the app shell (single-user = implicit owner).
    shell_selector = SHELL_SELECTOR[vp]
    try:
        page.locator(shell_selector).wait_for(state="visible", timeout=15_000)
        report.record(True, label + "auth → app shell visible")
    except Exception as exc:
        report.record(False, label + "auth → app shell visible", str(exc).splitlines()[0])
        shot("99-auth-failure", page)

    # 3b. Mobile only: the hamburger toggles the off-canvas nav drawer. Real
    # clicks on purpose — the drawer sits below the topbar, so the hamburger
    # must stay clickable while open; if a layout change ever covers it, this
    # check should fail the way a real user would.
    if vp == "mobile":
        try:
            open_mobile_nav(page)
            shot("02-nav-drawer", page)
            close_mobile_nav(page)  # true toggle: the second tap closes
            report.record(True, label + "hamburger toggles nav drawer")
        except Exception as exc:
            report.record(False, label + "hamburger toggles nav drawer", str(exc).splitlines()[0])
            shot("97-drawer-failure", page)

    # 4. Each route mounts its view without crashing.
    for suffix, selector in ROUTE_CHECKS:
        try:
            page.goto(base_url.rstrip("/") + suffix, wait_until="domcontentloaded")
            page.locator(shell_selector if selector == "SHELL" else selector).wait_for(
                state="visible", timeout=10_000
            )
            report.record(True, label + f"route {suffix}")
            shot(f"route{suffix.replace('/', '-')}", page)
        except Exception as exc:
            report.record(False, label + f"route {suffix}", str(exc).splitlines()[0])
            shot(f"fail{suffix.replace('/', '-')}", page)

    # 5. Insights: the time-range picker is shared by BOTH tabs — the Drill-down
    # tab scopes its agent/thread/tool rankings to the same window, so the
    # buttons must survive the tab switch (they used to be Overview-only).
    try:
        page.goto(base_url.rstrip("/") + "/insights", wait_until="domcontentloaded")
        page.locator(".lc-in-tabs button:has-text('Drill-down')").click()
        page.locator(".lc-in-windows button:has-text('90 days')").click()
        page.locator("button[aria-pressed='true']:has-text('90 days')").wait_for(
            state="visible", timeout=10_000
        )
        # The drill-down's own content must still mount under the new window.
        page.locator(".card:has-text('Agents by cost')").wait_for(state="visible", timeout=10_000)
        report.record(True, label + "insights drill-down keeps the time-range picker")
        shot("route-insights-drilldown", page)
    except Exception as exc:
        report.record(
            False,
            label + "insights drill-down keeps the time-range picker",
            str(exc).splitlines()[0],
        )
        shot("fail-insights-drilldown", page)

    # 5b. Refresh is on BOTH tabs too (it was Overview-only alongside the
    # picker). Clicking it on Drill-down must re-run that tab's fetches and
    # settle — a button that stays disabled means the spinner never cleared.
    try:
        refresh = page.locator("button.lc-in-refresh")
        refresh.wait_for(state="visible", timeout=10_000)
        refresh.click()
        # It disables while in flight, then must come back enabled with the
        # drill-down content still mounted (i.e. the reload actually resolved).
        page.wait_for_function(
            "() => { const b = document.querySelector('button.lc-in-refresh');"
            " return b && !b.disabled; }",
            timeout=15_000,
        )
        page.locator(".card:has-text('Agents by cost')").wait_for(state="visible", timeout=10_000)
        report.record(True, label + "insights drill-down refresh button reloads and settles")
        shot("route-insights-drilldown-refresh", page)
    except Exception as exc:
        report.record(
            False,
            label + "insights drill-down refresh button reloads and settles",
            str(exc).splitlines()[0],
        )
        shot("fail-insights-drilldown-refresh", page)
    finally:
        # The tab + window are persisted in localStorage; reset so a later run
        # (or the operator's own browser profile) doesn't start on Drill-down.
        page.evaluate(
            "() => { localStorage.removeItem('insights.tab');"
            " localStorage.removeItem('insights.window'); }"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--url", help="Origin of a running dashboard, e.g. http://localhost:3200")
    src.add_argument(
        "--launch",
        action="store_true",
        help="Spin up a throwaway runtime + UI (run from the olano repo)",
    )
    parser.add_argument(
        "--screenshot-dir",
        default=None,
        help="Where to write screenshots (default: ./.olano-tester/<timestamp>)",
    )
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    parser.add_argument(
        "--viewport",
        choices=[*sorted(VIEWPORT_KWARGS), "both"],
        default="both",
        help="Run the checklist at one viewport or both (default: both)",
    )
    args = parser.parse_args(argv)

    if not args.url and not args.launch:
        args.url = "http://localhost:3200"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    screenshot_dir = Path(args.screenshot_dir or Path(".olano-tester") / stamp)

    # --launch reuses the test harness so the skill and the pytest suite share
    # one definition of "boot a dashboard".
    if args.launch:
        # sys.path already carries the repo root — importing the route
        # checklist at module load put it there.
        from tests.e2e import harness

        ctx = harness.launch_dashboard()
    else:
        ctx = nullcontext()

    viewports = sorted(VIEWPORT_KWARGS) if args.viewport == "both" else [args.viewport]

    print(f"Verifying dashboard · screenshots → {screenshot_dir}\n")
    with ctx as running:
        base_url = running.base_url if running is not None else args.url
        print(f"Target: {base_url} · viewports: {', '.join(viewports)}\n")
        report = run_checks(base_url, screenshot_dir, args.headed, viewports)

    passed = sum(1 for ok, _, _ in report.rows if ok)
    print(f"\n{passed}/{len(report.rows)} checks passed · screenshots in {screenshot_dir}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
