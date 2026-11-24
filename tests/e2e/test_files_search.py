"""E2E: the Files filter finds files the tree has never listed.

The filter box narrowed only what the browser had already loaded — and every
agent row starts collapsed, so on a fresh page ``MEMOR`` matched nothing even
though every agent has a ``MEMORY.md``. It now also asks the server, which walks
every workspace the person can read and answers with matches the tree renders as
rows under their agent. What only a browser can prove:

* with everything collapsed, typing the name shows the file under its agent
  without a single row having been opened;
* the request is not per keystroke — typing a word at normal speed costs one
  search request, and Enter forces one without the pause;
* activating the match opens the file and reveals it in the tree.

Seeds the file through the app's own API: the e2e workspace is not scaffolded,
so ``merli`` has no ``MEMORY.md`` of its own here.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import TREE_ROW, agent_row

pytestmark = pytest.mark.e2e

AGENT = "merli"
FILE = "MEMORY.md"
SEARCH_GLOB = "**/api/files/search?*"
FILTER = "input[placeholder='Filter…']"


@pytest.fixture
def memory_file(page, dashboard) -> str:
    base = dashboard.base_url.rstrip("/")
    resp = page.request.put(
        f"{base}/api/files/{FILE}?agent={AGENT}",
        data={"content": "# e2e memory\n"},
    )
    assert resp.status == 200, f"could not seed {FILE}: {resp.status} {resp.text()}"
    return FILE


def test_typing_a_name_finds_it_in_a_collapsed_tree_with_one_request(
    page, dashboard, memory_file
) -> None:
    base = dashboard.base_url.rstrip("/")
    searches: list[str] = []
    page.route(SEARCH_GLOB, lambda route: (searches.append(route.request.url), route.continue_()))

    page.goto(f"{base}/files", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    row = agent_row(page, AGENT).first
    row.wait_for(state="visible", timeout=15_000)
    assert row.get_attribute("aria-expanded") == "false"

    # A word typed at human speed: one request after the pause, not five.
    page.locator(FILTER).click()
    page.keyboard.type("MEMOR", delay=60)
    hit = page.locator(f"{TREE_ROW}[data-kind='hit'][data-agent='{AGENT}']", has_text=memory_file)
    hit.wait_for(state="visible", timeout=15_000)
    assert len(searches) == 1, f"expected one search for the word, saw {searches}"
    assert "q=memor" in searches[0].lower()

    # A longer query that extends a complete result is refined locally: no request.
    page.keyboard.type("Y", delay=60)
    page.wait_for_timeout(700)
    assert len(searches) == 1, f"an extension of a complete result must not re-ask: {searches}"
    hit.wait_for(state="visible", timeout=5_000)

    # Activate the hit: the file opens and the tree reveals it as a real entry.
    # `attached`, not `visible`: on a phone the preview takes the column and the
    # tree pane steps aside, so the revealed row exists but is off screen.
    hit.click()
    page.locator(
        f"{TREE_ROW}[data-kind='entry'][data-agent='{AGENT}']", has_text=memory_file
    ).wait_for(state="attached", timeout=15_000)
    # …and its content is on screen: the preview opened.
    page.locator("main").get_by_text("e2e memory").first.wait_for(state="visible", timeout=15_000)


def test_enter_searches_without_waiting_and_a_short_query_never_asks(
    page, dashboard, memory_file
) -> None:
    base = dashboard.base_url.rstrip("/")
    searches: list[str] = []
    page.route(SEARCH_GLOB, lambda route: (searches.append(route.request.url), route.continue_()))

    page.goto(f"{base}/files", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    agent_row(page, AGENT).first.wait_for(state="visible", timeout=15_000)

    # One character is below the minimum: the box only filters locally.
    page.locator(FILTER).click()
    page.keyboard.type("m")
    page.wait_for_timeout(600)
    assert searches == []

    # Enter flushes the debounce for a query long enough to mean something.
    page.keyboard.type("em")
    page.keyboard.press("Enter")
    page.wait_for_function(
        "() => document.querySelector(\"[data-kind='hit']\") !== null", timeout=15_000
    )
    assert len(searches) == 1
