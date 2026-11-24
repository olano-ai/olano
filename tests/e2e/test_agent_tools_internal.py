"""E2E: internal tools are first-class in the agent Tools tab browser.

Drives ``/agents/<name>?tab=tools``. The internal tools — memory, notes,
graph, agent loop, filesystem primitives — used to live in a collapsed panel
below the full catalog browser, effectively invisible (the tab badge counted
them, the browser didn't show them). They now render as normal cards: an
**Internal** quick filter, searchable, checked while on. EVERY catalog row is
reported for every agent — rows whose mode condition isn't met (e.g.
per-customer memory without Customer Service mode) show locked with a
"requires …" note. Unchecking a toggleable one must persist to
``agent.json5 → disabled_tools`` — the same file the builder reads — so the
toggle is real, not cosmetic; that now covers the deepagents-attached tools
(``read_file``, ``task``, …) via core/disabled_tools_middleware.

Follows the ``test_agent_variables.py`` pattern: module-scoped dashboard over
the mock gateway, the agent config seeded per test into the ACTIVE per-test
home (the autouse ``lionclaw_home`` re-points ``LIONCLAW_HOME``), and a
locally rebound ``page`` fixture running at both viewports.
"""

from __future__ import annotations

import json
import re
import time

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

AGENT = "merli"


@pytest.fixture(scope="module")
def tools_dashboard():
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard(gateway=harness.build_mock_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


@pytest.fixture
def _agent_on_disk(lionclaw_home):
    """agents/<AGENT>/agent.json5 in the ACTIVE per-test home.

    Default memory (markdown on) so the memory/graph/notes built-ins are
    applicable, plus a couple of universal defaults exercised by the tests.
    """
    cfg_dir = lionclaw_home / "agents" / AGENT
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": AGENT,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
            }
        ),
        encoding="utf-8",
    )
    return cfg_dir


@pytest.fixture(params=["desktop", "mobile"])
def page(request, tools_dashboard, _agent_on_disk):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(
            tools_dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=tools",
            wait_until="domcontentloaded",
        )
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def _disabled_tools(cfg_path) -> list[str]:
    """``disabled_tools`` from agent.json5, or ``[]`` if it isn't readable yet.

    The runtime writes this file atomically (config.loader.write_agent_config_text),
    so a reader sees whole content or nothing — but this poll races a write by
    design, and a bad read must not abort the poll before its deadline. The
    caller's assertion reports the timeout instead.
    """
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8")).get("disabled_tools") or []
    except (OSError, ValueError):
        return []


def _wait_for_disabled(cfg_path, tool: str, timeout: float = 15.0) -> list[str]:
    """Poll agent.json5 until ``tool`` lands in ``disabled_tools``."""
    deadline = time.monotonic() + timeout
    disabled: list[str] = []
    while time.monotonic() < deadline:
        disabled = _disabled_tools(cfg_path)
        if tool in disabled:
            return disabled
        time.sleep(0.25)
    return disabled


def _rail_badge_count(page, label: str) -> int:
    btn = page.get_by_role("button", name=re.compile(rf"^{label}\b")).first
    btn.wait_for(state="visible", timeout=30_000)
    return int(btn.locator("span.badge").inner_text())


def test_internal_filter_lists_internal_cards(page) -> None:
    """The Internal quick filter exists and lists real cards: toggleable ones
    (whoami, task — checked), a condition-off one (remember: locked, with a
    note pointing at Customer Service mode), and a builder-injected memory
    tool."""
    # The rail renders once the catalog + agent config resolve.
    internal_btn = page.get_by_role("button", name=re.compile(r"^Internal\b")).first
    internal_btn.wait_for(state="visible", timeout=30_000)
    assert _rail_badge_count(page, "Internal") > 0
    internal_btn.click()

    whoami = page.locator('[data-tool-card="whoami"]').first
    whoami.wait_for(state="visible", timeout=15_000)
    # Toggleable internal tool: a real checkbox, on by default, tagged internal.
    assert whoami.locator("input[type=checkbox]").count() == 1
    assert whoami.locator("input[type=checkbox]").first.is_checked()
    whoami.get_by_text("internal", exact=True).first.wait_for(state="visible", timeout=5_000)

    # The deepagents-attached `task` is a real toggle now too
    # (core/disabled_tools_middleware enforces the opt-out at model bind).
    task = page.locator('[data-tool-card="task"]').first
    task.wait_for(state="visible", timeout=10_000)
    assert task.locator("input[type=checkbox]").count() == 1
    assert task.locator("input[type=checkbox]").first.is_checked()

    # Condition-off row: per-customer memory on a non-CS agent shows locked
    # (no checkbox) with the note saying what enables it.
    remember = page.locator('[data-tool-card="remember"]').first
    remember.wait_for(state="visible", timeout=10_000)
    assert remember.locator("input[type=checkbox]").count() == 0
    remember.get_by_text(re.compile("requires Customer Service")).first.wait_for(
        state="visible", timeout=5_000
    )

    # A builder-injected memory tool (no registry entry) is visible too.
    page.locator('[data-tool-card="memory_save"]').first.wait_for(state="visible", timeout=10_000)


def test_tools_tab_badge_matches_selected_count(page) -> None:
    """The tab badge and the browser's Selected filter are the same deduplicated
    union — the old code summed extras + internal tools and double-counted every
    universal default (badge 37 vs browser 13)."""
    # The agent-detail tab rail is a real ARIA tablist (role="tab", roving
    # tabindex, arrow-key navigation), not a row of plain buttons.
    tools_tab = page.get_by_role("tab", name=re.compile(r"^Tools\b")).first
    tools_tab.wait_for(state="visible", timeout=30_000)
    selected = _rail_badge_count(page, "Selected")
    badge = int(tools_tab.locator("span.badge").inner_text())
    assert badge == selected
    # Sanity: the union covers more than the 17 universal defaults — the
    # internal tools (memory, agent loop, filesystem) are counted in.
    assert badge > 17


def test_unchecking_internal_tool_persists_disabled_tools(page, _agent_on_disk) -> None:
    """Unchecking an internal card writes it to agent.json5 → disabled_tools —
    the field core/builder filters the injected tools by."""
    search = page.get_by_placeholder(re.compile("Search by name")).first
    search.wait_for(state="visible", timeout=30_000)
    search.fill("whoami")

    card = page.locator('[data-tool-card="whoami"]').first
    card.wait_for(state="visible", timeout=15_000)
    box = card.locator("input[type=checkbox]").first
    assert box.is_checked()
    box.click()

    cfg_path = _agent_on_disk / "agent.json5"
    disabled = _wait_for_disabled(cfg_path, "whoami")
    assert "whoami" in disabled
    # Unchecking one tool must never drag other names into disabled_tools.
    assert not ({"task", "read_file", "ls", "glob", "grep"} & set(disabled))
    # The card reflects the new state without a reload — and stays that way.
    # (A tap used to fire the label's synthetic click on top of the input's
    # own, silently re-enabling the tool right after disabling it on touch
    # devices; the steady-state re-read catches that regression.)
    assert not box.is_checked()
    page.wait_for_timeout(1_500)
    assert not box.is_checked()
    assert "whoami" in _disabled_tools(cfg_path)


def test_unchecking_runtime_primitive_persists_too(page, _agent_on_disk) -> None:
    """`read_file` comes from the deepagents backend, not the builder's merge —
    the newly-toggleable class of internal tools. Unchecking it must persist
    exactly like any other opt-out (enforced at model bind by
    core/disabled_tools_middleware)."""
    search = page.get_by_placeholder(re.compile("Search by name")).first
    search.wait_for(state="visible", timeout=30_000)
    search.fill("read_file")

    card = page.locator('[data-tool-card="read_file"]').first
    card.wait_for(state="visible", timeout=15_000)
    box = card.locator("input[type=checkbox]").first
    assert box.is_checked()
    box.click()

    cfg_path = _agent_on_disk / "agent.json5"
    assert "read_file" in _wait_for_disabled(cfg_path, "read_file")
    assert not box.is_checked()
