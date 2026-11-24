"""E2E: thumbs up / down under a reply, and the Insights count.

What the in-process tests cannot see: that the thumbs render under a finished
reply at both viewports, that a vote is saved where the runtime reads it and
reads back as cast after a reload, that thumbs down opens the comment box (its
help text on screen) and the comment lands with the vote, that a reply with no
turn id draws no thumbs, and that Insights draws the per-agent count.
"""

from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.e2e

REPLY = "Here is the summary you asked for."


def _sessions():
    from olano.config.home import get_olano_paths

    d = get_olano_paths().sessions_dir / "merli"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed(*, turn_id: str | None = "turn-e2e") -> str:
    thread_id = str(uuid.uuid4())
    reply: dict = {"ts": "2026-10-18T00:00:01Z", "role": "assistant", "content": REPLY}
    if turn_id:
        reply["turn_id"] = turn_id
    rows = [{"ts": "2026-10-18T00:00:00Z", "role": "user", "content": "summarize"}, reply]
    (_sessions() / f"{thread_id}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    return thread_id


def _votes(thread_id: str) -> list[dict]:
    path = _sessions() / f"{thread_id}.feedback.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _wait_for(page, predicate, timeout_ms: int = 10_000):
    for _ in range(timeout_ms // 200):
        value = predicate()
        if value:
            return value
        page.wait_for_timeout(200)
    return predicate()


def _open(page, dashboard, thread_id: str) -> None:
    page.goto(
        f"{dashboard.base_url.rstrip('/')}/chat?thread={thread_id}", wait_until="domcontentloaded"
    )
    page.get_by_text(REPLY).first.wait_for(state="visible", timeout=20_000)


def test_a_vote_is_saved_and_reads_back(page, dashboard) -> None:
    thread_id = _seed()
    _open(page, dashboard, thread_id)
    up = page.get_by_test_id("reply-vote-up").first
    up.wait_for(state="visible", timeout=10_000)
    assert up.get_attribute("aria-pressed") == "false"
    up.click()
    votes = _wait_for(page, lambda: _votes(thread_id))
    assert votes and votes[-1]["value"] == 1 and votes[-1]["turn_id"] == "turn-e2e"

    page.reload(wait_until="domcontentloaded")
    page.get_by_text(REPLY).first.wait_for(state="visible", timeout=20_000)
    up = page.get_by_test_id("reply-vote-up").first
    page.wait_for_function(
        "() => document.querySelector(\"[data-testid='reply-vote-up']\")"
        "?.getAttribute('aria-pressed') === 'true'",
        timeout=10_000,
    )
    # Pressing the lit thumb again takes the vote back.
    up.click()
    assert _wait_for(page, lambda: _votes(thread_id)[-1]["value"] == 0)


def _color(page, testid: str) -> str:
    return page.evaluate(
        "(id) => getComputedStyle(document.querySelector(`[data-testid='${id}']`)).color",
        testid,
    )


def _settles_to(page, testid: str, color: str) -> bool:
    """The buttons fade between colours, so poll until the fade has finished."""
    return bool(_wait_for(page, lambda: _color(page, testid) == color, timeout_ms=3_000))


def test_a_thumb_is_plain_until_voted_then_green_or_red(page, dashboard) -> None:
    """Uncast thumbs wear the text colour of Copy beside them; only the
    person's own vote turns green (up) or red (down)."""
    thread_id = _seed()
    _open(page, dashboard, thread_id)
    up = page.get_by_test_id("reply-vote-up").first
    up.wait_for(state="visible", timeout=10_000)
    green, red = "rgb(34, 165, 91)", "rgb(229, 72, 77)"
    plain_up, plain_down = _color(page, "reply-vote-up"), _color(page, "reply-vote-down")
    assert plain_up == plain_down
    assert plain_up not in (green, red)

    up.click()
    page.wait_for_function(
        "() => document.querySelector(\"[data-testid='reply-vote-up']\")"
        "?.getAttribute('aria-pressed') === 'true'",
        timeout=10_000,
    )
    page.mouse.move(0, 0)
    assert _settles_to(page, "reply-vote-up", green)
    assert _settles_to(page, "reply-vote-down", plain_down)

    page.get_by_test_id("reply-vote-down").first.click()
    page.get_by_test_id("reply-feedback-form").get_by_role("button", name="Skip").click()
    page.wait_for_function(
        "() => document.querySelector(\"[data-testid='reply-vote-down']\")"
        "?.getAttribute('aria-pressed') === 'true'",
        timeout=10_000,
    )
    page.mouse.move(0, 0)
    assert _settles_to(page, "reply-vote-down", red)
    assert _settles_to(page, "reply-vote-up", plain_up)


def test_thumbs_down_takes_a_comment(page, dashboard) -> None:
    thread_id = _seed()
    _open(page, dashboard, thread_id)
    page.get_by_test_id("reply-vote-down").first.click()
    form = page.get_by_test_id("reply-feedback-form")
    form.wait_for(state="visible", timeout=10_000)
    assert "only when traces include full content" in form.inner_text()
    box = form.bounding_box()
    viewport = page.viewport_size
    assert box and viewport
    assert box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"] + 1
    form.locator("textarea").fill("It summarized the wrong file.")
    form.get_by_role("button", name="Send").click()
    rows = _wait_for(
        page, lambda: [r for r in _votes(thread_id) if r.get("comment")], timeout_ms=10_000
    )
    assert rows[-1]["value"] == -1
    assert rows[-1]["comment"] == "It summarized the wrong file."
    # The vote counted before the comment was written, and it is still down.
    assert _votes(thread_id)[-1]["value"] == -1


def test_a_reply_without_a_turn_has_no_thumbs(page, dashboard) -> None:
    thread_id = _seed(turn_id=None)
    _open(page, dashboard, thread_id)
    assert page.get_by_test_id("reply-vote-up").count() == 0


def test_insights_counts_votes(page, dashboard) -> None:
    from datetime import datetime, timezone

    thread_id = _seed()
    (_sessions() / f"{thread_id}.feedback.ndjson").write_text(
        json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "turn_id": "turn-e2e",
                "user": "someone",
                "value": -1,
            }
        )
        + "\n"
    )
    page.goto(dashboard.base_url.rstrip("/") + "/insights", wait_until="domcontentloaded")
    card = page.get_by_test_id("insights-feedback")
    card.wait_for(state="visible", timeout=20_000)
    card.scroll_into_view_if_needed()
    totals = card.get_by_test_id("insights-feedback-totals")
    totals.wait_for(state="visible", timeout=10_000)
    assert "1" in totals.inner_text()
