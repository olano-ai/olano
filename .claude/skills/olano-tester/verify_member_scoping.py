#!/usr/bin/env python3
"""Browser verification of per-agent visibility scoping for non-admin members.

The default smoke harness boots single-user (implicit owner = admin), so it
renders every panel and never exercises the member scoping. This script boots a
*multi-user* dashboard, bootstraps an admin, creates a member granted ONE of two
agents, seeds some usage + A2A data, then drives a real browser as both the
member and the admin and screenshots the difference:

  * member sidebar: no Vault / Logs nav items (admin-only surfaces)
  * member Overview: no System control / Highlights / Memory / raw-log cards
  * member Insights: usage scoped to the granted agent only
  * admin: all of the above present, both agents visible

Run from the repo root:
    uv run python .claude/skills/olano-tester/verify_member_scoping.py [--headed]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# <repo>/.claude/skills/olano-tester/verify_member_scoping.py → parents[3] == repo root.
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

# Pin the zero-config sqlite checkpoint/store backends BEFORE anything imports
# the runtime — otherwise the throwaway home defaults to postgres-without-URL
# and /api/analytics/* 500s (unrelated to what we're verifying).
os.environ.setdefault("LIONCLAW_CHECKPOINT_BACKEND", "sqlite")
os.environ.setdefault("LIONCLAW_STORE_BACKEND", "sqlite")

from tests.e2e import harness


def _two_agent_gateway():
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    runners = {}
    for name, emoji in (("merli", "🦁"), ("scout", "🛰️")):
        cfg = AgentConfig(
            name=name,
            model="anthropic:claude-haiku-4-5-20251001",
            backend=FilesystemBackendConfig(root_dir="workspace"),
        )
        cfg.emoji = emoji
        cfg.description = f"{name} test agent"
        cfg.channels = []
        runners[name] = harness._FakeRunner(cfg, f"hi from {name}")
    return harness._gateway_for(runners)


def _seed_home_data() -> None:
    """Seed usage + A2A into the (already-set) throwaway LIONCLAW_HOME."""
    from olano.analytics.usage_ledger import record_usage_event
    from olano.config.home import get_olano_paths

    record_usage_event("merli", "anthropic:claude-haiku-4-5", 1200, 340, 0.42)
    record_usage_event("scout", "openai:gpt-5.4", 5000, 900, 3.10)

    logs = get_olano_paths().logs_dir
    logs.mkdir(parents=True, exist_ok=True)
    import json

    events = [
        {
            "kind": "peer",
            "phase": "send",
            "from": "merli",
            "to": "scout",
            "message": "ping from merli",
        },
        {
            "kind": "peer",
            "phase": "send",
            "from": "scout",
            "to": "ghost",
            "message": "secret scout↔ghost",
        },
    ]
    (logs / "a2a_activity.jsonl").write_text(
        "\n".join(
            json.dumps(
                {"type": "agent_message", "data": d, "ts": datetime.now(timezone.utc).isoformat()}
            )
            for d in events
        )
        + "\n",
        encoding="utf-8",
    )

    # A daily work-highlights entry with a distinct per-agent bullet each, so we
    # can assert the member sees ONLY merli's line and the admin sees both.
    from olano.analytics import journal

    day = datetime.now(timezone.utc).date().isoformat()
    entry = journal.build_journal_day(day, save=True)
    text = (
        "LEAD: A steady day across the fleet.\n"
        "[[agent:merli]]\nHandled MERLIBILL billing questions end to end.\n"
        "[[agent:scout]]\nRan SCOUTSCAN security scans, all clear.\n"
    )
    journal._apply_highlights(entry, text, "test:model")
    journal.journal_path(day).write_text(json.dumps(entry), encoding="utf-8")


def _setup_users(runtime_url: str) -> None:
    """Bootstrap admin (enables multi-user) + a member granted only merli."""
    import httpx

    with httpx.Client(base_url=runtime_url, timeout=30.0) as c:
        r = c.post(
            "/api/auth/bootstrap",
            json={
                "email": "admin@x.com",
                "password": "adminpw",
                "name": "Admin",
                # An unmanaged box with no accounts needs the first-run code.
                "setup_code": harness.first_run_setup_code(),
            },
        )
        r.raise_for_status()
        # Dismiss the first-run admin onboarding wizard, which otherwise
        # replaces the whole shell for the admin (app.tsx onboardingRequired
        # gate) and would hide the sidebar we assert on.
        c.post("/api/onboarding/complete", json={"steps": {}, "skipped": True})
        uid = c.post(
            "/api/admin/users",
            json={"email": "bob@x.com", "password": "memberpw", "name": "Bob"},
        ).json()["user"]["id"]
        c.post("/api/agents/merli/access", json={"user_id": uid}).raise_for_status()


def _login(context, base_url: str, email: str, password: str) -> None:
    """Set the session cookie in the browser context via the login endpoint."""
    resp = context.request.post(
        f"{base_url}/api/auth/login", data={"email": email, "password": password}
    )
    assert resp.ok, f"login failed for {email}: {resp.status} {resp.text()}"


def _nav_texts(page) -> list[str]:
    """All sidebar nav link labels, including the collapsible 'MORE' group.

    Vault/Logs/etc. live under a MORE section that's collapsed by default, so
    expand it first — otherwise a member absence-check would pass trivially and
    the admin presence-check would miss items that are merely hidden.
    """
    header = page.locator(
        "aside.sidebar[aria-label='Primary'] button.nav-group-header:not(.bot-section-header)"
    )
    if header.count() and (header.first.get_attribute("aria-expanded") or "").lower() != "true":
        header.first.click()
        page.wait_for_timeout(250)
    return [
        t.strip()
        for t in page.locator("aside.sidebar[aria-label='Primary'] nav a").all_inner_texts()
    ]


def run(base_url: str, runtime_url: str, shot_dir: Path, headed: bool) -> bool:
    from playwright.sync_api import sync_playwright

    shot_dir.mkdir(parents=True, exist_ok=True)
    ok = True

    def check(cond: bool, name: str, detail: str = "") -> None:
        nonlocal ok
        ok = ok and cond
        print(
            f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
            flush=True,
        )

    with sync_playwright() as pw:
        kwargs: dict = {"headless": not headed}
        exe = os.environ.get("LIONCLAW_PW_EXECUTABLE")
        if exe:
            kwargs["executable_path"] = exe
            kwargs["args"] = ["--no-sandbox"]
        browser = pw.chromium.launch(**kwargs)

        # ── Member ────────────────────────────────────────────────────────────
        member_ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        _login(member_ctx, base_url, "bob@x.com", "memberpw")
        page = member_ctx.new_page()
        page.goto(f"{base_url}/overview", wait_until="domcontentloaded")
        page.locator("aside.sidebar[aria-label='Primary']").wait_for(timeout=15000)
        page.wait_for_timeout(800)
        page.screenshot(path=str(shot_dir / "member-overview.png"), full_page=True)
        nav = _nav_texts(page)
        member_nav_joined = " | ".join(nav)
        check("Vault" not in nav, "member sidebar hides Vault", member_nav_joined)
        check("Logs" not in nav, "member sidebar hides Logs", member_nav_joined)
        check("Config" not in nav, "member sidebar hides Config", member_nav_joined)
        check(any("Overview" in n for n in nav), "member sidebar keeps Overview")
        # Overview admin-only cards must be absent for the member.
        body = page.locator(".page").inner_text()
        check("System" not in body or "Live snapshot" in body, "member Overview mounted")
        check(
            page.locator("h2:has-text('Recent activity')").count() == 0,
            "member Overview hides raw-log card",
        )
        # Daily-highlights digest: composed from ONLY the member's granted agent.
        check("MERLIBILL" in body, "member Overview highlights show granted agent")
        check("SCOUTSCAN" not in body, "member Overview highlights hide non-granted agent")

        page.goto(f"{base_url}/insights", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(shot_dir / "member-insights.png"), full_page=True)
        insights_txt = page.locator("body").inner_text()
        # Spend is scoped to merli ($0.42) — the fleet total incl. scout ($3.52)
        # must not appear. (Note: "scout" itself legitimately shows as merli's
        # A2A counterpart, since merli↔scout is the member's own conversation.)
        check("$0.42" in insights_txt, "member Insights spend scoped to merli", "$0.42")
        check("$3.5" not in insights_txt, "member Insights excludes fleet-wide spend")
        # The work-journal narrative is composed to the member's agent only.
        check("MERLIBILL" in insights_txt, "member Insights journal shows granted agent")
        check("SCOUTSCAN" not in insights_txt, "member Insights journal hides non-granted agent")
        # Daily / Weekly / Monthly period toggle is present.
        check(
            page.locator("button:has-text('Weekly')").count() >= 1,
            "member Insights journal has period toggle",
        )

        page.goto(f"{base_url}/activity", wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        page.screenshot(path=str(shot_dir / "member-activity.png"), full_page=True)
        activity_txt = page.locator("body").inner_text()
        check("ghost" not in activity_txt, "member A2A hides scout↔ghost exchange")
        member_ctx.close()

        # ── Admin (comparison) ─────────────────────────────────────────────────
        admin_ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        _login(admin_ctx, base_url, "admin@x.com", "adminpw")
        apage = admin_ctx.new_page()
        apage.goto(f"{base_url}/overview", wait_until="domcontentloaded")
        apage.locator("aside.sidebar[aria-label='Primary']").wait_for(timeout=15000)
        apage.wait_for_timeout(800)
        apage.screenshot(path=str(shot_dir / "admin-overview.png"), full_page=True)
        anav = _nav_texts(apage)
        check("Vault" in anav, "admin sidebar shows Vault", " | ".join(anav))
        check("Logs" in anav, "admin sidebar shows Logs")
        # Admin's daily highlights show the WHOLE fleet — both agents' bullets.
        abody = apage.locator(".page").inner_text()
        check(
            "MERLIBILL" in abody and "SCOUTSCAN" in abody,
            "admin Overview highlights show all agents",
        )

        apage.goto(f"{base_url}/insights", wait_until="domcontentloaded")
        apage.wait_for_timeout(1200)
        apage.screenshot(path=str(shot_dir / "admin-insights.png"), full_page=True)
        admin_txt = apage.locator("body").inner_text()
        check("scout" in admin_txt, "admin sees both agents in Insights")
        check(
            "MERLIBILL" in admin_txt and "SCOUTSCAN" in admin_txt,
            "admin Insights journal shows all agents' highlights",
        )
        admin_ctx.close()

        browser.close()
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    shot_dir = REPO / ".olano-tester" / f"member-{stamp}"

    with harness.launch_dashboard(gateway=_two_agent_gateway()) as dash:
        _seed_home_data()
        _setup_users(dash.runtime_url)
        ok = run(dash.base_url, dash.runtime_url, shot_dir, args.headed)

    print(f"\nScreenshots in {shot_dir}", flush=True)
    print("ALL PASSED" if ok else "SOME CHECKS FAILED", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
