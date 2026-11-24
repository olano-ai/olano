#!/usr/bin/env python3
"""Browser + API verification of the per-agent Commands tab.

Exercises the new agent.json5 `commands` config + the dashboard Commands editor:
  API:  GET /api/catalog/commands (groups + sensitive defaults),
        PUT /api/agents/<name> persists a commands block (enabled + custom),
        GET reflects it back.
  UI:   /agents/<name>?tab=commands renders grouped built-ins with toggles,
        sensitive commands default OFF + tagged, safe ones default ON;
        toggling a command and adding a custom command persist.

Run from the repo root:
    uv run python .claude/skills/olano-tester/verify_commands.py [--headed]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# <repo>/.claude/skills/olano-tester/verify_commands.py → parents[3] == repo root.
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

AGENT = "cmdtest"


def _seed_agent_cwd() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="lc-cmd-verify-"))
    cfg_dir = tmp / "agents" / AGENT
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": AGENT,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
            },
            indent=2,
        )
    )
    os.chdir(tmp)
    return tmp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    args = parser.parse_args(argv)

    _seed_agent_cwd()

    from tests.e2e import harness
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    cfg = AgentConfig(
        name=AGENT,
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir="workspace"),
    )
    cfg.emoji = "🦁"
    cfg.description = "Commands verify agent"
    cfg.channels = []

    gw = harness._gateway_for({AGENT: harness._FakeRunner(cfg, harness.CHAT_REPLY_SENTINEL)})

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    shots = REPO / ".olano-tester" / f"commands-{stamp}"
    shots.mkdir(parents=True, exist_ok=True)

    rows: list[tuple[bool, str, str]] = []

    def rec(ok: bool, name: str, detail: str = "") -> None:
        rows.append((ok, name, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)

    import httpx
    from playwright.sync_api import sync_playwright

    def pw_browser(pw):
        kwargs: dict = {"headless": not args.headed}
        exe = os.environ.get("LIONCLAW_PW_EXECUTABLE")
        if exe:
            kwargs["executable_path"] = exe
            kwargs["args"] = ["--no-sandbox"]
        return pw.chromium.launch(**kwargs)

    print(f"Verifying Commands tab · screenshots → {shots}\n")
    with harness.launch_dashboard(gateway=gw) as dash:
        rt, base = dash.runtime_url, dash.base_url
        print(f"runtime={rt}  ui={base}\n")

        with httpx.Client(timeout=20.0) as c:
            # ── catalog ──────────────────────────────────────────────────────
            r = c.get(f"{rt}/api/catalog/commands")
            cat = r.json()
            by = {x["name"]: x for x in cat.get("commands", [])}
            rec(
                r.status_code == 200
                and by.get("trust", {}).get("sensitive") is True
                and by.get("trust", {}).get("default_exposed") is False
                and by.get("help", {}).get("default_exposed") is True,
                "API GET /api/catalog/commands (groups + sensitive defaults)",
                f"groups={[g['id'] for g in cat.get('groups', [])]}",
            )

            # ── persist a commands block via PUT, read it back ───────────────
            patch = {
                "commands": {
                    "enabled": {"trust": True, "memory": False},
                    "custom": [
                        {"name": "standup", "prompt": "Summarize today. {args}", "description": "Daily"}
                    ],
                }
            }
            r = c.put(f"{rt}/api/agents/{AGENT}", json=patch)
            rec(r.status_code == 200, "API PUT commands block", r.text[:80])
            got = c.get(f"{rt}/api/agents/{AGENT}").json()
            cmds = got.get("commands", {})
            rec(
                cmds.get("enabled") == {"trust": True, "memory": False}
                and cmds.get("custom", [{}])[0].get("name") == "standup",
                "API GET reflects persisted commands",
                json.dumps(cmds)[:120],
            )
            # reset to defaults for the UI portion (so the browser starts clean)
            c.put(f"{rt}/api/agents/{AGENT}", json={"commands": {"enabled": {}, "custom": []}})

        with sync_playwright() as pw:
            browser = pw_browser(pw)
            page = browser.new_context().new_page()
            try:
                page.goto(f"{base}/agents/{AGENT}?tab=commands", wait_until="domcontentloaded", timeout=30_000)
                page.locator("aside.sidebar[aria-label='Primary']").wait_for(state="visible", timeout=20_000)
                page.get_by_text("Slash commands", exact=False).first.wait_for(state="visible", timeout=15_000)
                rec(True, "UI Commands tab mounts")

                # groups render
                for label in ("Info & inspection", "State-changing / sensitive"):
                    rec(page.get_by_text(label, exact=False).count() > 0, f"UI group '{label}' renders")

                # sensitive badge present, /trust + /help rows present
                rec(page.get_by_text("sensitive", exact=False).count() > 0, "UI 'sensitive' tag present")
                rec(page.locator('[data-command="trust"]').count() > 0, "UI /trust row present")
                rec(page.locator('[data-command="help"]').count() > 0, "UI /help row present")
                page.screenshot(path=str(shots / "01-commands-tab.png"), full_page=True)

                # default exposure: safe ON, sensitive OFF
                help_sw = page.locator('[data-command="help"]').get_by_role("switch")
                trust_sw = page.locator('[data-command="trust"]').get_by_role("switch")
                trust_sw.scroll_into_view_if_needed()
                rec(help_sw.get_attribute("aria-checked") == "true", "UI /help exposed by default (ON)")
                rec(trust_sw.get_attribute("aria-checked") == "false", "UI /trust sensitive → OFF by default")
                page.screenshot(path=str(shots / "02-sensitive-section.png"), full_page=True)

                # toggle /trust ON → persists.
                trust_sw.click()
                page.wait_for_timeout(1000)  # let savePatch → PUT + reload settle
                after_state = page.locator('[data-command="trust"]').get_by_role("switch").get_attribute("aria-checked")
                rec(after_state == "true", "UI toggle /trust ON", f"→{after_state}")

                # add a custom command via the modal
                page.get_by_role("button", name="Add").first.click()
                page.get_by_text("Add custom command", exact=False).wait_for(state="visible", timeout=8_000)
                page.get_by_placeholder("standup").fill("dailybrief")
                page.get_by_placeholder("Daily summary").fill("Daily brief")
                page.get_by_placeholder("Summarize what you did today from MEMORY.md. {args}").fill(
                    "Give me a daily brief. {args}"
                )
                page.screenshot(path=str(shots / "03-add-custom-modal.png"), full_page=True)
                page.get_by_role("button", name="Save").click()
                page.wait_for_timeout(1000)
                rec(
                    page.locator('[data-custom="dailybrief"]').count() > 0,
                    "UI custom command /dailybrief added",
                )
                page.screenshot(path=str(shots / "04-after-add.png"), full_page=True)

                # confirm both changes persisted through the API
                with httpx.Client(timeout=20.0) as c:
                    cmds = c.get(f"{rt}/api/agents/{AGENT}").json().get("commands", {})
                rec(cmds.get("enabled", {}).get("trust") is True, "persisted: trust enabled")
                rec(
                    any(x.get("name") == "dailybrief" for x in cmds.get("custom", [])),
                    "persisted: custom /dailybrief",
                    json.dumps(cmds)[:140],
                )
            except Exception as e:
                rec(False, "UI checks", str(e).splitlines()[0])
                page.screenshot(path=str(shots / "99-failure.png"), full_page=True)
            finally:
                browser.close()

    passed = sum(1 for ok, _, _ in rows if ok)
    print(f"\n{passed}/{len(rows)} checks passed · screenshots in {shots}")
    return 0 if all(ok for ok, _, _ in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
