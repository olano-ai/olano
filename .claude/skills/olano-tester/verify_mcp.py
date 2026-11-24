#!/usr/bin/env python3
"""Browser + API verification of the MCP features in the dashboard.

A worked example of the **"feature that needs a configured agent"** recipe (see
SKILL.md): the default mock gateway has one bare agent with no MCP servers, so to
exercise MCP UI we build a *custom* gateway with an agent that HAS mcp_servers,
write its config to a CWD-local `agents/` dir, and seed the live MCP summary the
backend reads (`builder._MCP_SUMMARY`). Then we drive a real browser.

Run from the repo root:
    uv run python .claude/skills/olano-tester/verify_mcp.py [--headed]

Verifies:
  API:  GET /mcp/status (cheap, no live connect), ?probe=true routes to probe,
        GET /mcp/health (overall color + servers), POST /mcp/reload delegates.
  UI:   /chat MCP indicator chip + popover (server rows + Setup deep-link),
        /agents/<name>?tab=mcp deep-links to the MCP tab with a Reload button.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

# <repo>/.claude/skills/olano-tester/verify_mcp.py → parents[3] == repo root.
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

AGENT = "mcptest"


def _seed_agent_cwd() -> Path:
    """A temp dir, made CWD, holding agents/<AGENT>/agent.json5 with MCP servers.

    `agents/` resolution is CWD-relative (config.loader._LOCAL_AGENTS_DIR =
    Path("agents"); api._agent_config_path checks ./agents first), so chdir here
    isolates the test agent from the repo's real ./agents and ~/.olano.
    """
    tmp = Path(tempfile.mkdtemp(prefix="lc-mcp-verify-"))
    cfg_dir = tmp / "agents" / AGENT
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": AGENT,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
                "mcp_servers": [
                    {
                        "url": "everything",
                        "transport": "stdio",
                        "enabled": False,
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-everything"],
                    },
                    {
                        "url": "https://mcp.hubspot.com",
                        "transport": "streamable_http",
                        "enabled": True,
                        "auth": "oauth",
                        "oauth_client_id": "x",
                        "oauth_client_secret": "y",
                    },
                    {
                        "url": "https://mcp.notion.com",
                        "transport": "http",
                        "enabled": True,
                        "auth": "oauth",
                    },
                ],
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

    import olano.core.builder as builder
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig
    from tests.e2e import harness

    # Seed the live MCP summary the runtime would record after a build/reload:
    # one connected (with tool names), one disabled, one needing auth.
    builder._MCP_SUMMARY[AGENT] = [
        {
            "url": "everything",
            "transport": "stdio",
            "ok": False,
            "status": "disabled",
            "tools": [],
            "message": "Disabled in config",
        },
        {
            "url": "https://mcp.hubspot.com",
            "transport": "streamable_http",
            "ok": True,
            "status": "connected",
            "tools": ["hubspot_list_contacts", "hubspot_create_deal", "hubspot_search"],
            "message": "3 tools",
        },
        {
            "url": "https://mcp.notion.com",
            "transport": "streamable_http",
            "ok": False,
            "status": "auth_required",
            "tools": [],
            "message": "OAuth not authorized",
        },
    ]

    cfg = AgentConfig(
        name=AGENT,
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir="workspace"),
    )
    cfg.emoji = "🦁"
    cfg.description = "MCP verify agent"
    cfg.channels = []

    gw = harness._gateway_for({AGENT: harness._FakeRunner(cfg, harness.CHAT_REPLY_SENTINEL)})
    # Async runtime methods must be AsyncMock — a bare MagicMock attr isn't awaitable.
    gw.reload_agent_mcp = AsyncMock(
        return_value={"ok": True, "running": True, "servers": builder._MCP_SUMMARY[AGENT]}
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    shots = REPO / ".olano-tester" / f"mcp-{stamp}"
    shots.mkdir(parents=True, exist_ok=True)

    rows: list[tuple[bool, str, str]] = []

    def rec(ok: bool, name: str, detail: str = "") -> None:
        rows.append((ok, name, detail))
        print(
            f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
            flush=True,
        )

    import httpx
    from playwright.sync_api import sync_playwright

    def pw_browser(pw):
        kwargs: dict = {"headless": not args.headed}
        exe = os.environ.get("LIONCLAW_PW_EXECUTABLE")
        if exe:
            kwargs["executable_path"] = exe
            kwargs["args"] = ["--no-sandbox"]
        return pw.chromium.launch(**kwargs)

    print(f"Verifying MCP features · screenshots → {shots}\n")
    # launch_dashboard handles the IPv4 Vite bind + temp single-user HOME for us.
    with harness.launch_dashboard(gateway=gw) as dash:
        rt, base = dash.runtime_url, dash.base_url
        print(f"runtime={rt}  ui={base}\n")

        with httpx.Client(timeout=20.0) as c:
            r = c.get(f"{rt}/api/agents/{AGENT}/mcp/status")
            by = {s["url"]: s for s in r.json().get("servers", [])}
            rec(
                r.status_code == 200
                and by.get("https://mcp.hubspot.com", {}).get("status") == "auth_required"
                and by.get("everything", {}).get("status") == "disabled",
                "API GET /mcp/status (cheap, no live connect)",
                f"statuses={[s.get('status') for s in by.values()]}",
            )
            r = c.get(f"{rt}/api/agents/{AGENT}/mcp/status?probe=true")
            rec(
                r.status_code == 200 and len(r.json().get("servers", [])) == 3,
                "API GET /mcp/status?probe=true routes to probe",
            )
            r = c.get(f"{rt}/api/agents/{AGENT}/mcp/health")
            h = r.json()
            rec(
                r.status_code == 200
                and h.get("overall") == "yellow"
                and len(h.get("servers", [])) == 3,
                "API GET /mcp/health",
                f"overall={h.get('overall')}",
            )
            r = c.post(f"{rt}/api/agents/{AGENT}/mcp/reload", json={})
            rec(
                r.status_code == 200 and r.json().get("ok") is True,
                "API POST /mcp/reload delegates to runtime",
            )
            rec(
                gw.reload_agent_mcp.await_count >= 1,
                "reload_agent_mcp awaited",
                f"awaits={gw.reload_agent_mcp.await_count}",
            )

        with sync_playwright() as pw:
            browser = pw_browser(pw)
            page = browser.new_context().new_page()
            try:
                page.goto(f"{base}/chat", wait_until="domcontentloaded", timeout=30_000)
                page.locator("aside.sidebar[aria-label='Primary']").wait_for(
                    state="visible", timeout=20_000
                )
                rec(True, "UI /chat app shell mounts")

                chip = page.locator("button.chat-chip", has_text="MCP")
                chip.first.wait_for(state="visible", timeout=15_000)
                rec(True, "UI chat MCP indicator chip visible", chip.first.inner_text().strip())
                page.screenshot(path=str(shots / "01-chat-indicator.png"), full_page=True)

                chip.first.click()
                page.get_by_text("Setup →").wait_for(state="visible", timeout=8_000)
                rec(
                    page.get_by_text("mcp.hubspot.com", exact=False).count() > 0,
                    "UI indicator popover lists servers + Setup link",
                )
                page.screenshot(path=str(shots / "02-indicator-popover.png"), full_page=True)

                page.goto(
                    f"{base}/agents/{AGENT}?tab=mcp", wait_until="domcontentloaded", timeout=30_000
                )
                page.get_by_role("button", name="Reload agent").wait_for(
                    state="visible", timeout=15_000
                )
                rec(
                    page.get_by_text("mcp.hubspot.com", exact=False).count() > 0,
                    "UI ?tab=mcp deep-link opens MCP tab with servers",
                )
                page.screenshot(path=str(shots / "03-mcp-tab.png"), full_page=True)
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
