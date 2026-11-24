"""E2E: an issue can be copied, whole or one card at a time.

Selecting a long issue body by hand to paste it somewhere else (a chat, another
tracker, an agent's prompt) loses the Markdown and, on a phone, most of the
text. These pin the two copy affordances on the detail page in a real browser:

* each card's head has a copy icon that puts that card's Markdown source on the
  clipboard and confirms in place;
* **Copy issue** puts the whole thread (title, header line, description, every
  comment) on the clipboard as one Markdown document;
* neither moves the layout: the card head keeps its height, and the button stays
  inside the window at both viewports.

The clipboard is stubbed in the page, so the test reads exactly what was
written without depending on the browser's clipboard permission.
"""

from __future__ import annotations

import uuid

import pytest

from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

_STUB_CLIPBOARD = """() => {
  window.__copied = [];
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: async (text) => { window.__copied.push(text); } },
  });
}"""


@pytest.fixture
def commented_issue(page, dashboard) -> tuple[int, str]:
    base = dashboard.base_url.rstrip("/")
    title = f"E2E copy {uuid.uuid4().hex[:8]}"
    resp = page.request.post(
        f"{base}/api/issues",
        data={"title": title, "body": "Steps:\n\n- read twice\n- compare", "type": "bug"},
    )
    assert resp.status == 200, f"could not seed the issue: {resp.status} {resp.text()}"
    issue_id = int(resp.json()["issue"]["id"])
    commented = page.request.post(
        f"{base}/api/issues/{issue_id}/comment", data={"body": "Reproduced **again**."}
    )
    assert commented.status == 200, commented.text()
    return issue_id, title


def test_a_card_and_the_whole_issue_copy_as_markdown(page, dashboard, commented_issue) -> None:
    issue_id, title = commented_issue
    base = dashboard.base_url.rstrip("/")
    page.goto(f"{base}/issues?issue={issue_id}", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    page.locator("h1", has_text=title).wait_for(state="visible", timeout=15_000)
    page.evaluate(_STUB_CLIPBOARD)

    cards = page.locator(".issue-comment-card:has(.issue-comment-head)")
    assert cards.count() == 2
    comment_head = cards.nth(1).locator(".issue-comment-head")
    height_before = comment_head.bounding_box()["height"]

    copy_comment = comment_head.get_by_role("button", name="Copy comment")
    copy_comment.click()
    assert page.evaluate("() => window.__copied") == ["Reproduced **again**."]
    page.wait_for_selector(".issue-comment-card .issue-copy.copied", timeout=5_000)
    # The icon sits inside the head without growing it (a few px of rounding).
    assert abs(comment_head.bounding_box()["height"] - height_before) < 2

    cards.nth(0).get_by_role("button", name="Copy description").click()
    assert page.evaluate("() => window.__copied[1]") == "Steps:\n\n- read twice\n- compare"

    copy_issue = page.get_by_role("button", name="Copy issue")
    box = copy_issue.bounding_box()
    width = page.viewport_size["width"]
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= width
    copy_issue.click()
    page.get_by_text(f"Issue #{issue_id} copied as Markdown.").wait_for(timeout=5_000)
    copied = page.evaluate("() => window.__copied[2]")
    assert copied.startswith(f"# {title} (#{issue_id})\n\nOpen · bug · opened by ")
    assert "\n\nSteps:\n\n- read twice\n- compare\n\n---\n\n**" in copied
    assert copied.endswith(" UTC:\n\nReproduced **again**.\n")
