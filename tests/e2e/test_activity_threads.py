"""Agent-to-Agent view: conversations, not one row per two-minute burst.

The rail used to open a new "thread" whenever the (from, to) pair changed or
two minutes elapsed, so a single ongoing pair produced dozens of rows. These
tests pin the fix: one row per pair (both directions folded together), a
transcript with day separators behind it, and a working mobile drill-down.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

# Two peers talking both ways over three days plus one subagent delegation.
# Under the old grouping this is 9 rail rows; it must now be 2.
PAIRS = [("marcus", "valentina"), ("valentina", "marcus")]


def _seed(dashboard) -> None:
    from olano.config.home import get_olano_paths

    now = datetime.now(timezone.utc)
    # Anchor the exchanges to midday, NOT to "now minus a few hours".
    #
    # The turns are spread 1h and 4h back from their day's anchor, so anchoring
    # at `now` put the whole seed on 4 calendar days instead of 3 whenever CI
    # ran between 00:00 and 04:00 UTC — `now - 4h` had already crossed midnight.
    # `to_have_count(3)` on the day separators then failed for four hours out of
    # every twenty-four, on whatever commit happened to land in that window
    # (2026-08-16 01:35Z was one). Midday of the previous day is always in the
    # past and always leaves 4 hours of room before midnight, so the seed spans
    # exactly 3 distinct days at every hour of the clock. Reads have no lookback
    # window (`_A2A_SCAN_LIMIT` is a count), so dating the seed a day earlier
    # changes nothing else.
    anchor = (now - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    lines = []
    for day in range(3):
        for turn, (src, dst) in enumerate(PAIRS):
            # Well outside the 2-minute window that used to split threads.
            ts = anchor - timedelta(days=day, hours=turn * 3 + 1)
            msg = f"day{day} turn{turn}"
            lines.append(
                {
                    "type": "agent_message",
                    "ts": ts.isoformat(),
                    "data": {
                        "kind": "peer",
                        "phase": "send",
                        "from": src,
                        "to": dst,
                        "message": msg,
                    },
                }
            )
            lines.append(
                {
                    "type": "agent_message",
                    "ts": (ts + timedelta(seconds=5)).isoformat(),
                    "data": {
                        "kind": "peer",
                        "phase": "reply",
                        "from": src,
                        "to": dst,
                        "message": msg,
                        "reply": f"ack {msg}",
                        "ts_sent": ts.isoformat(),
                        "ts_reply": (ts + timedelta(seconds=5)).isoformat(),
                    },
                }
            )
    lines.append(
        {
            "type": "agent_message",
            "ts": now.isoformat(),
            "data": {
                "kind": "subagent",
                "phase": "send",
                "from": "marcus",
                "to": "researcher",
                "message": "dig into it",
                "tool_call_id": "call-1",
            },
        }
    )

    # An outside caller over MCP: must render as EXTERNAL with an MCP badge and
    # the caller's label, never as a peer conversation with a fake agent.
    ext_ts = now - timedelta(minutes=30)
    lines.append(
        {
            "type": "agent_message",
            "ts": ext_ts.isoformat(),
            "data": {
                "kind": "inbound",
                "protocol": "mcp",
                "phase": "send",
                "from": "mcp:Claude desktop",
                "to": "marcus",
                "message": "what is the order status?",
                "thread_id": "mcp::claude-desktop::default",
            },
        }
    )
    lines.append(
        {
            "type": "agent_message",
            "ts": (ext_ts + timedelta(seconds=4)).isoformat(),
            "data": {
                "kind": "inbound",
                "protocol": "mcp",
                "phase": "reply",
                "from": "mcp:Claude desktop",
                "to": "marcus",
                "message": "what is the order status?",
                "reply": "Shipped yesterday.",
                "thread_id": "mcp::claude-desktop::default",
                "ts_sent": ext_ts.isoformat(),
                "ts_reply": (ext_ts + timedelta(seconds=4)).isoformat(),
            },
        }
    )

    logs = get_olano_paths().logs_dir
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "a2a_activity.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


# The rail always renders an "All activity" pseudo-row, and it carries the same
# `.a2a-thread` class as the real conversations. So waiting on `.a2a-thread`
# only proves the rail shell mounted — it resolves before /api/a2a/threads has
# answered. Wait on a row the seed guarantees instead, and let the count
# assertions retry, or the first (cold-cache) viewport races the fetch.
SEEDED_ROW = ".a2a-thread:has-text('valentina')"


@pytest.fixture
def activity_page(page, dashboard):
    """The /activity route with a seeded A2A log, rail mounted and loaded."""
    _seed(dashboard)
    page.goto(f"{dashboard.base_url}/activity", wait_until="domcontentloaded")
    page.locator("[data-testid='a2a-rail']").wait_for(state="visible", timeout=20_000)
    page.wait_for_selector(SEEDED_ROW, timeout=20_000)
    return page


def test_rail_folds_a_pair_into_one_conversation(activity_page) -> None:
    """6 exchanges across 3 days between one pair (both directions) + 1 subagent
    delegation → 2 conversation rows, not 7."""
    rows = activity_page.locator(".a2a-thread")
    # +1 for the "All activity" pseudo-row, +1 for the external (MCP) conversation.
    expect(rows).to_have_count(4)

    labels = rows.all_inner_texts()
    peer_row = next(x for x in labels if "valentina" in x)
    assert "marcus" in peer_row
    assert "6 msgs" in peer_row
    assert any("researcher" in x for x in labels)


def test_segments_filter_by_kind(activity_page) -> None:
    activity_page.locator(".a2a-seg", has_text="Subagent").click()
    rows = activity_page.locator(".a2a-thread")
    expect(rows).to_have_count(2)  # "All activity" + the one subagent conversation
    assert "researcher" in rows.nth(1).inner_text()


def test_external_segment_shows_the_caller_with_a_protocol_badge(activity_page) -> None:
    """An inbound MCP call is its own kind: the External segment lists it, the
    row names the caller by its label (not "mcp:…") and carries an MCP badge,
    and the transcript keeps the badge on the caller's bubble."""
    activity_page.locator(".a2a-seg", has_text="External").click()
    rows = activity_page.locator(".a2a-thread")
    expect(rows).to_have_count(2)  # "All activity" + the one external conversation
    row = rows.nth(1)
    expect(row.locator(".a2a-proto-mcp")).to_have_text("MCP")
    text = row.inner_text()
    assert "Claude desktop" in text and "marcus" in text and "mcp:" not in text
    # It never shows under Peer.
    activity_page.locator(".a2a-seg", has_text="Peer").click()
    assert "Claude desktop" not in activity_page.locator(".a2a-thread").all_inner_texts()[1]

    activity_page.locator(".a2a-seg", has_text="External").click()
    activity_page.locator(".a2a-thread", has_text="Claude desktop").first.click()
    activity_page.wait_for_selector(".a2a-exchange", timeout=15_000)
    expect(activity_page.locator(".a2a-exchange .a2a-proto-mcp").first).to_have_text("MCP")
    assert "pair=inbound" in activity_page.url


def test_selecting_a_pair_shows_its_transcript_with_day_separators(activity_page) -> None:
    activity_page.locator(".a2a-thread", has_text="valentina").first.click()
    activity_page.wait_for_selector(".a2a-exchange", timeout=15_000)

    # 6 exchanges over 3 distinct days.
    expect(activity_page.locator(".a2a-exchange")).to_have_count(6)
    expect(activity_page.locator(".a2a-day")).to_have_count(3)
    # Deep-linkable, so the conversation survives a reload / can be shared.
    assert "pair=peer" in activity_page.url


def test_mobile_drills_down_and_back(page, dashboard) -> None:
    """On mobile the rail and the transcript are one column: picking a
    conversation replaces the list, and Back returns to it."""
    if page.viewport_size["width"] > 900:
        pytest.skip("drill-down is the mobile layout")
    _seed(dashboard)
    page.goto(f"{dashboard.base_url}/activity", wait_until="domcontentloaded")
    page.wait_for_selector(SEEDED_ROW, timeout=20_000)

    page.locator(".a2a-thread", has_text="valentina").first.click()
    page.wait_for_selector(".a2a-exchange", timeout=15_000)
    expect(page.locator("[data-testid='a2a-rail']")).to_have_count(0)

    page.locator(".a2a-pane-head button", has_text="Conversations").click()
    page.locator("[data-testid='a2a-rail']").wait_for(state="visible", timeout=10_000)
