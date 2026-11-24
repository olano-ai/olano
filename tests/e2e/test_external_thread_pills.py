"""E2E: an inbound MCP / A2A conversation is labelled as such in the chat rail.

A turn an outside system sent over the MCP endpoint runs as a real thread on the
agent (``mcp::<client>::<thread>``), so it appears in the agent's chat history.
Before this it read as an ordinary dashboard chat titled by the caller's first
message. Now the row carries the EXTERNAL origin pill and an MCP / A2A channel
pill, at both viewports.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

TOGGLE = "[data-testid='chat-history-toggle']"
DRAWER = "aside.chat-rail.is-drawer"


def _seed_transcripts(agent: str) -> None:
    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / agent
    sessions.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    threads = {
        "mcp::claude-desktop::default": ("mcp", "mcp:Claude desktop", "what is the order status?"),
        "a2a::zapier-orders::ctx-1": ("a2a", "a2a:Zapier orders", "list open orders for ACME"),
    }
    for i, (tid, (channel, sender, text)) in enumerate(threads.items()):
        ts = now - timedelta(minutes=10 * (i + 1))
        rows = [
            {
                "ts": ts.isoformat(),
                "role": "user",
                "content": text,
                "channel": channel,
                "sender": sender,
            },
            {
                "ts": (ts + timedelta(seconds=3)).isoformat(),
                "role": "assistant",
                "content": "Shipped yesterday.",
                "channel": channel,
            },
        ]
        (sessions / f"{tid}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )


def test_external_threads_carry_external_and_protocol_pills(page, dashboard) -> None:
    _seed_transcripts("merli")
    page.goto(dashboard.base_url.rstrip("/") + "/chat?agent=merli", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    page.wait_for_selector(TOGGLE, timeout=15_000)
    page.locator(TOGGLE).click()
    page.wait_for_selector(DRAWER, timeout=5_000)

    drawer = page.locator(DRAWER)
    mcp_row = drawer.locator(".thread-item", has_text="order status").first
    mcp_row.wait_for(state="visible", timeout=15_000)
    # Both pills, with their words: an outside caller over MCP.
    origin = mcp_row.locator(".thread-origin-external")
    assert origin.count() >= 1
    assert re.search("external", origin.first.inner_text(), re.I)
    channel = mcp_row.locator(".thread-channel-mcp")
    assert channel.count() >= 1
    assert re.search("mcp", channel.first.inner_text(), re.I)

    a2a_row = drawer.locator(".thread-item", has_text="open orders").first
    a2a_row.wait_for(state="visible", timeout=15_000)
    assert a2a_row.locator(".thread-origin-external").count() >= 1
    a2a_channel = a2a_row.locator(".thread-channel-a2a")
    assert a2a_channel.count() >= 1
    assert re.search("a2a", a2a_channel.first.inner_text(), re.I)
