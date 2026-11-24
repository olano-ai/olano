"""Files view: one tree for every accessible agent, built to a budget.

The tree lists every agent the signed-in person can reach as a collapsed
row, with a ``workspace`` node under each that holds that agent's files. These
pin the properties that make it hold up at fleet scale rather than the
rendering of any one row:

* **zero listing requests on load** — a collapsed agent costs nothing;
* **one request per directory opened**, scoped to that agent on the wire;
* **windowed rendering** — a 2,000-entry listing puts a few dozen rows in the
  DOM, and the server-side cap is reported rather than hidden;
* **bounded re-hydration** — a persisted expansion reloads through a queue of
  at most six in flight, not as one burst (the runtime allows 240 a minute);
* **keyboard** — the WAI-ARIA tree pattern, arrows and all;
* **a truncated preview cannot be edited** — saving a head would destroy the
  tail.

The listing-size cases stub the network rather than seeding thousands of
files through the API (which would itself trip the request budget), so the
invariant is about what the tree does with a big answer, not about disk.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e.route_checks import (
    TREE,
    TREE_ROW,
    agent_row,
    click_nav_link,
    open_agent_workspace,
    tree_row,
    workspace_row,
)

pytestmark = pytest.mark.e2e

AGENT = "merli"
BIG_DIR = "e2e-big-listing"
BIG_FILE = "e2e-big-file.log"
HYDRATE_DIRS = [f"e2e-hydrate-{i:02d}" for i in range(12)]
LISTING_GLOB = "**/api/files?*"

# Counts concurrent `/api/files?` listings from inside the page, where the
# overlap is real: a route handler on the test side would serialize them.
COUNT_INFLIGHT = """
(() => {
  const orig = window.fetch;
  window.__files = { inflight: 0, maxInflight: 0, total: 0 };
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : input.url;
    const isListing = /\\/api\\/files\\?/.test(url);
    if (isListing) {
      const s = window.__files;
      s.total += 1;
      s.inflight += 1;
      s.maxInflight = Math.max(s.maxInflight, s.inflight);
    }
    const p = orig.apply(this, arguments);
    if (isListing) p.finally(() => { window.__files.inflight -= 1; });
    return p;
  };
})();
"""


def _seed_dirs(page, base: str, paths: list[str]) -> None:
    for path in paths:
        resp = page.request.post(
            f"{base}/api/files/create", data={"path": path, "type": "directory"}
        )
        assert resp.status in (200, 409), f"could not seed {path}: {resp.status} {resp.text()}"


def test_collapsed_agents_cost_no_listing_request(page, dashboard) -> None:
    base = dashboard.base_url.rstrip("/")
    # The last step waits for a listed entry, so the workspace must have one of
    # its own: on a worker where no other Files test ran first it is empty.
    # Seeded through the API, before the route below starts counting.
    _seed_dirs(page, base, ["e2e-collapsed"])
    calls: list[str] = []
    page.route(LISTING_GLOB, lambda route: (calls.append(route.request.url), route.continue_()))

    page.goto(f"{base}/files", wait_until="domcontentloaded")
    row = agent_row(page, AGENT).first
    row.wait_for(state="visible", timeout=15_000)
    assert row.get_attribute("aria-expanded") == "false"
    page.wait_for_timeout(1_500)
    assert calls == [], f"a collapsed tree must not list anything, saw {calls}"

    # Opening the agent alone still lists nothing: the workspace row is static.
    row.click()
    workspace_row(page, AGENT).first.wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(500)
    assert calls == []

    # Opening the workspace lists exactly that agent's root, once, with the cap.
    workspace_row(page, AGENT).first.click()
    page.wait_for_function(
        "() => document.querySelectorAll(\"[role='treeitem'][data-kind='entry']\").length > 0",
        timeout=15_000,
    )
    page.wait_for_timeout(500)
    assert len(calls) == 1, calls
    assert f"agent={AGENT}" in calls[0] and "limit=" in calls[0] and "path=" not in calls[0]


def test_a_huge_listing_renders_a_window_and_says_what_it_left_out(page, dashboard) -> None:
    base = dashboard.base_url.rstrip("/")
    _seed_dirs(page, base, [BIG_DIR])

    def _big(route):
        entries = [
            {
                "name": f"file-{i:05d}.txt",
                "path": f"{BIG_DIR}/file-{i:05d}.txt",
                "abs_path": f"/x/{BIG_DIR}/file-{i:05d}.txt",
                "type": "file",
                "size": 1,
                "modified": "2026-01-01T00:00:00",
            }
            for i in range(2000)
        ]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"entries": entries, "truncated": True, "total": 2500}),
        )

    page.route(f"**/api/files?*path={BIG_DIR}*", _big)
    page.goto(f"{base}/files", wait_until="domcontentloaded")
    open_agent_workspace(page, AGENT)
    tree_row(page, BIG_DIR).first.click()
    tree_row(page, "file-00000.txt").first.wait_for(state="visible", timeout=15_000)

    # Windowed: the DOM holds a viewport's worth of rows, never the listing.
    rows = page.locator(TREE_ROW).count()
    assert rows < 100, f"expected a windowed tree, found {rows} rows in the DOM"
    # Scrolling to the end reaches the last entry, and the cap is reported
    # rather than hidden: a row after it names what was left out.
    page.locator(TREE).first.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    tree_row(page, "file-01999.txt").first.wait_for(state="visible", timeout=10_000)
    truncated = page.locator("[data-truncated-row]").first
    truncated.wait_for(state="visible", timeout=10_000)
    assert "2,000" in truncated.inner_text() and "2,500" in truncated.inner_text()
    # Still a window after scrolling to the far end.
    rows = page.locator(TREE_ROW).count()
    assert rows < 100, f"expected a windowed tree after scrolling, found {rows} rows"


# Viewport-agnostic and the most expensive case here (12 folders opened one by
# one); the sidebar link it navigates with is a drawer below the breakpoint.
@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_rehydration_is_queued_not_burst(page, dashboard) -> None:
    """Twelve open folders + the root reload through a queue of at most six."""
    base = dashboard.base_url.rstrip("/")
    _seed_dirs(page, base, HYDRATE_DIRS)
    page.add_init_script(COUNT_INFLIGHT)

    page.goto(f"{base}/files", wait_until="domcontentloaded")
    open_agent_workspace(page, AGENT)
    for d in HYDRATE_DIRS:
        tree_row(page, d).first.click()
    # Every folder open (each lists once, sequentially — user clicks).
    page.wait_for_function(
        "() => document.querySelectorAll(\"[role='treeitem'][data-kind='entry'][aria-expanded='true']\").length >= 12",
        timeout=15_000,
    )
    page.wait_for_timeout(500)
    page.evaluate("() => { window.__files.total = 0; window.__files.maxInflight = 0; }")

    # Leave and come back through the SPA's own navigation (a full page load
    # would start the in-session expansion over): the expansion persisted, so
    # every open listing must reload — root + 12 folders, each once, and never
    # more than six at a time.
    click_nav_link(page, "/overview")
    page.wait_for_url("**/overview", timeout=10_000)
    click_nav_link(page, "/files")  # Files lives under More now; the helper opens it
    page.wait_for_url("**/files", timeout=10_000)
    page.wait_for_function(
        "() => document.querySelectorAll(\"[role='treeitem'][data-kind='entry'][aria-expanded='true']\").length >= 12"
        " && window.__files.inflight === 0 && window.__files.total >= 13",
        timeout=20_000,
    )
    stats = page.evaluate("() => window.__files")
    assert stats["total"] == 13, stats
    assert stats["maxInflight"] <= 6, stats


# Keyboard semantics don't change with the viewport; touch emulation only adds
# noise to focus handling.
@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_keyboard_walks_the_tree(page, dashboard) -> None:
    base = dashboard.base_url.rstrip("/")
    _seed_dirs(page, base, [BIG_DIR])
    page.goto(f"{base}/files", wait_until="domcontentloaded")
    row = agent_row(page, AGENT).first
    row.wait_for(state="visible", timeout=15_000)
    row.focus()

    page.keyboard.press("ArrowRight")  # expand the agent
    workspace_row(page, AGENT).first.wait_for(state="visible", timeout=10_000)
    page.keyboard.press("ArrowDown")  # onto the workspace row
    assert page.evaluate("() => document.activeElement.dataset.kind") == "workspace"
    page.keyboard.press("ArrowRight")  # expand the workspace → lists the root
    page.wait_for_function(
        "() => document.querySelectorAll(\"[role='treeitem'][data-kind='entry']\").length > 0",
        timeout=15_000,
    )
    page.keyboard.press("ArrowDown")  # first entry
    assert page.evaluate("() => document.activeElement.dataset.kind") == "entry"
    page.keyboard.press("ArrowLeft")  # a collapsed entry: step to the parent (workspace)
    assert page.evaluate("() => document.activeElement.dataset.kind") == "workspace"
    page.keyboard.press("ArrowLeft")  # collapse the workspace
    assert workspace_row(page, AGENT).first.get_attribute("aria-expanded") == "false"
    page.keyboard.press("Home")
    assert page.evaluate("() => document.activeElement.dataset.kind") == "agent"


def test_a_truncated_preview_cannot_be_edited(page, dashboard) -> None:
    base = dashboard.base_url.rstrip("/")
    resp = page.request.put(f"{base}/api/files/{BIG_FILE}?agent={AGENT}", data={"content": "small"})
    assert resp.ok, resp.text()

    def _head(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "path": BIG_FILE,
                    "content": "the head",
                    "size": 8,
                    "truncated": True,
                    "total_size": 987654321,
                }
            ),
        )

    page.route(f"**/api/files/{BIG_FILE}?*", _head)
    page.goto(f"{base}/files?path={BIG_FILE}&agent={AGENT}", wait_until="domcontentloaded")
    page.locator("[data-preview-truncated]").first.wait_for(state="visible", timeout=15_000)
    assert page.locator(".files-pane-actions button[aria-label='Edit']").count() == 0
    # The head is still shown, and the download route is still offered.
    page.locator(".files-preview-body pre:has-text('the head')").first.wait_for(
        state="visible", timeout=10_000
    )
    assert page.locator(".files-pane-actions a[download]").count() == 1
