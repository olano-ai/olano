#!/usr/bin/env python3
"""Drive the notification surfaces in a real browser: bell + Overview section.

``verify_dashboard.py``'s route walk only proves they render *empty*. This
raises real notifications, then checks the unread badge, the panel (including
that it opens fully on screen — the bell sits at opposite edges in the two
breakpoints), the Overview section, and the mark-read path, at both viewports.

The e2e harness runs the runtime API in this same process, so
``notifications.notify()`` here writes to the very store the API serves.

    python .claude/skills/olano-tester/verify_notifications.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.e2e import harness

VIEWPORTS = {
    "desktop": {"viewport": {"width": 1440, "height": 900}},
    "mobile": {"viewport": {"width": 390, "height": 844}, "is_mobile": True},
}

OUT = Path(os.environ.get("OUT_DIR", ".olano-tester/notifications"))
OUT.mkdir(parents=True, exist_ok=True)

rows: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    rows.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}", flush=True)


def seed() -> None:
    from olano.runtime import notifications

    notifications.clear(read_only=False)
    notifications.notify(
        kind="channel_credential_missing",
        level="error",
        title="telegram is not running for support",
        body="No value for TELEGRAM_BOT_TOKEN. Add it in the agent's connector panel.",
        agent="support",
        dedupe_key="channel_credential_missing:support:telegram",
        action={"label": "Open connector", "href": "/agents/support?tab=connectors"},
    )
    notifications.notify(
        kind="disk_pressure",
        level="warn",
        title="Disk usage warn",
        body="91.2% used on /",
        dedupe_key="disk_pressure",
    )


def main() -> int:
    from playwright.sync_api import sync_playwright

    with harness.launch_dashboard() as running:
        base = running.base_url
        print(f"Target: {base}\n", flush=True)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                executable_path=os.environ.get("LIONCLAW_PW_EXECUTABLE") or None
            )
            for name, kwargs in VIEWPORTS.items():
                print(f"[{name}]", flush=True)
                seed()

                ctx = browser.new_context(**kwargs)
                page = ctx.new_page()
                page.goto(f"{base}/overview", wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                # Both breakpoints mount a bell (CSS hides one) — same pattern
                # as the brand wordmark. Target the one actually on screen.
                bell = page.locator("button.notification-bell:visible").first
                check(bell.count() > 0, f"{name}: bell is present")
                badge = bell.inner_text().strip() if bell.count() else ""
                check("2" in badge, f"{name}: badge shows the unread count (got {badge!r})")

                body = page.locator("body").inner_text()
                check("Notifications" in body, f"{name}: Overview has a Notifications section")
                check(
                    "telegram is not running for support" in body,
                    f"{name}: Overview lists the missing-credential row",
                )
                check("Disk usage warn" in body, f"{name}: Overview lists the disk row")
                check(
                    "TELEGRAM_BOT_TOKEN" in body,
                    f"{name}: the row body names the key to set",
                )
                check("Open connector" in body, f"{name}: the row offers its fix action")
                page.screenshot(path=str(OUT / f"{name}-overview.png"), full_page=True)

                bell.click()
                page.wait_for_timeout(900)
                panel = page.locator(".notification-panel").first
                check(panel.count() > 0, f"{name}: panel opens")
                if panel.count():
                    ptext = panel.inner_text()
                    check(
                        "telegram is not running for support" in ptext,
                        f"{name}: panel shows the notification",
                    )
                # The panel must be fully on screen: the desktop bell sits at
                # the sidebar's left edge and the mobile one at the right, so a
                # fixed anchor puts it off-viewport at one of them.
                if panel.count():
                    box = panel.bounding_box()
                    vw = page.viewport_size["width"]
                    check(
                        bool(box) and box["x"] >= 0 and box["x"] + box["width"] <= vw + 1,
                        f"{name}: panel is fully on screen (x={box and round(box['x'])}, "
                        f"w={box and round(box['width'])}, vw={vw})",
                    )
                page.screenshot(path=str(OUT / f"{name}-bell-open.png"))

                # Opening is the read receipt: badge clears, rows stay listed.
                page.keyboard.press("Escape")
                page.wait_for_timeout(1500)
                after = page.locator("button.notification-bell:visible").first.inner_text().strip()
                check(after == "", f"{name}: opening the panel cleared the badge (got {after!r})")
                body2 = page.locator("body").inner_text()
                check(
                    "telegram is not running for support" in body2,
                    f"{name}: read rows are still listed",
                )
                ctx.close()
            browser.close()

    passed = sum(1 for ok, _ in rows if ok)
    print(f"\n{passed}/{len(rows)} checks passed · screenshots in {OUT}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
