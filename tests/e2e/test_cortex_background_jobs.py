"""E2E: Create project runs in the background, and the card follows it.

The reported bug, from the user's side: tapping **Create project** opened a
"Planning the project..." panel whose only way out was Close, and closing it
left the card offering *Create project* again, so a second tap started a
second promotion while nothing said whether the first was still going.

Real backend (the runtime shares this process, so the slow promotion is a
monkeypatched ``run_promote`` and everything around it is the real route, job
registry and API), real browser, both viewports:

* the panel can be hidden and the run keeps going;
* the card then reads *Creating project...* and its button re-opens the same
  run rather than starting another (the fake counts its calls);
* the run ends in the panel with the project's path, and the Runs list holds it
  with its steps.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.e2e import harness
from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

CARD = ".cortex-card"
JOB_BADGE = "[data-testid='cortex-card-job']"
JOB_BUTTON = "[data-testid='cortex-card-job-button']"
DRAWER = "[data-testid='cortex-job-drawer']"


def _server_agent_config(dashboard: harness.RunningDashboard, name: str):
    """The config the running server holds for ``name``.

    An ``AgentConfig`` pins its workspace when it is built, and the session's
    server built its agents under the session home, not this test's home, so a
    freshly built config would point the seed at a workspace the server never
    reads. Ask the served gateway's runner instead. Scanning ``gc`` for any
    ``merli`` runner was order-dependent: an earlier test's own dashboard can
    leave a runner alive whose workspace no server reads, and the seed went
    there while the page showed an empty list.
    """
    runner = dashboard.gateway.get_runner(name) if dashboard.gateway is not None else None
    if runner is None:
        pytest.skip(f"no running {name!r} agent in this dashboard")
    return runner.config


@pytest.fixture
def slow_promotion(monkeypatch, dashboard):
    """Seed one idea and make promoting it take a few seconds, counting calls."""
    from olano.cognition import cortex_items as ci
    from olano.cognition import ideation
    from olano.cognition import jobs as cortex_jobs
    from olano.cognition.worker import WorkerOutcome
    from olano.config.workspace import get_workspace

    root = get_workspace(_server_agent_config(dashboard, "merli").workspace).root
    item = ci.add_item(root, kind="idea", title="Catalogue readiness gate", summary="Flag gaps.")
    calls: list[str] = []

    async def fake_run_promote(config, workspace_root, it, *, target=None, source="manual", **kw):
        calls.append(it["id"])
        for step in ("README.md", "PLAN.md", "STATUS.md"):
            cortex_jobs.note(
                f"{{'path': 'projects/catalogue/{step}'}}", type="tool", tool="write_file"
            )
            await asyncio.sleep(4)
        project = ci.add_item(workspace_root, kind="project", title=it["title"])
        ci.set_status(workspace_root, it["id"], "promoted")
        return WorkerOutcome(status="completed", summary="Planned three phases."), project

    monkeypatch.setattr(ideation, "run_promote", fake_run_promote)
    cortex_jobs.REGISTRY.reset()
    yield item, calls
    cortex_jobs.REGISTRY.reset()


def _open_ideas(page) -> None:
    base = page.url.split("/overview")[0].rstrip("/")
    page.goto(base + "/cortex/merli/ideas", wait_until="domcontentloaded", timeout=20_000)
    wait_for_app_shell(page)


def test_a_card_action_answers_the_click_itself(page, dashboard, slow_promotion) -> None:
    """Discard, before the server has answered: the pressed button spins and
    says what it is doing, the card waits dimmed and out of reach, and the
    moment the server agrees the card leaves the Active list, with a notice.
    A plain disabled button plus a spinner at the far end of the row was all
    the page used to show, and it read as nothing having happened."""
    from olano.cognition import cortex_items as ci
    from olano.config.workspace import get_workspace

    item, _calls = slow_promotion
    root = get_workspace(_server_agent_config(dashboard, "merli").workspace).root
    # One card by this title: the other viewport's run seeded its own.
    for other in ci.list_items(root, kind="idea"):
        if other["title"] == item["title"] and other["id"] != item["id"]:
            ci.set_status(root, other["id"], "discarded")
    _open_ideas(page)

    card = page.locator(CARD, has_text=item["title"]).first
    card.wait_for(state="visible", timeout=15_000)

    # The answer is held until the test releases it; on disk the discard
    # happens as the real route would do it, so the reload agrees with the card.
    held: list = []

    def _discard(route) -> None:
        ci.set_status(root, item["id"], "discarded")
        held.append(route)

    page.route("**/api/cortex/items/*/discard", _discard)
    card.get_by_role("button", name="Discard", exact=True).click()
    page.locator("[role='alertdialog']").get_by_role("button", name="Discard").click()

    busy = card.get_by_role("button", name="Discarding…")
    busy.wait_for(state="visible", timeout=5_000)
    assert busy.get_attribute("aria-busy") == "true"
    assert busy.locator(".btn-spinner").count() == 1
    assert card.get_attribute("aria-busy") == "true"
    assert card.get_by_role("button", name="Create project", exact=True).is_disabled()

    held[0].fulfill(json={"ok": True, "item": {**item, "status": "discarded"}})
    page.unroute("**/api/cortex/items/*/discard")
    card.wait_for(state="detached", timeout=10_000)
    page.locator(".toast", has_text="Discarded").wait_for(state="visible", timeout=5_000)


def test_the_item_drawer_acts_like_the_card(page, dashboard, slow_promotion) -> None:
    """Open an item: its drawer offers Create project and Discard with the
    card's own confirm and busy state (it used to offer only Chat and Close,
    so a person reading an item had to close it and find the card again).
    Discarding from the drawer closes it and takes the card out of the list;
    Create project from the drawer hands over to the run's panel, and the
    card follows the run."""
    from olano.cognition import cortex_items as ci
    from olano.config.workspace import get_workspace

    item, calls = slow_promotion
    root = get_workspace(_server_agent_config(dashboard, "merli").workspace).root
    for other in ci.list_items(root, kind="idea"):
        if other["title"] == item["title"] and other["id"] != item["id"]:
            ci.set_status(root, other["id"], "discarded")
    _open_ideas(page)

    card = page.locator(CARD, has_text=item["title"]).first
    card.wait_for(state="visible", timeout=15_000)
    card.get_by_role("button", name="Open", exact=True).click()
    drawer = page.locator(f"[role='dialog'][aria-label='{item['title']}']")
    drawer.wait_for(state="visible", timeout=10_000)
    drawer.locator("[data-testid='cortex-drawer-promote']").wait_for(state="visible", timeout=5_000)
    drawer.locator("[data-testid='cortex-drawer-discard']").wait_for(state="visible", timeout=5_000)

    # Discard from the drawer: confirm, the button spins, the drawer closes
    # once the server agrees and the card is gone.
    held: list = []

    def _discard(route) -> None:
        ci.set_status(root, item["id"], "discarded")
        held.append(route)

    page.route("**/api/cortex/items/*/discard", _discard)
    drawer.locator("[data-testid='cortex-drawer-discard']").click()
    page.locator("[role='alertdialog']").get_by_role("button", name="Discard").click()
    busy = drawer.get_by_role("button", name="Discarding…")
    busy.wait_for(state="visible", timeout=5_000)
    assert drawer.locator("[data-testid='cortex-drawer-promote']").is_disabled()
    held[0].fulfill(json={"ok": True, "item": {**item, "status": "discarded"}})
    page.unroute("**/api/cortex/items/*/discard")
    drawer.wait_for(state="detached", timeout=10_000)
    card.wait_for(state="detached", timeout=10_000)

    # A second idea: Create project from its drawer. The drawer gives way to
    # the run's panel, and the card reads Creating project...
    second = ci.add_item(root, kind="idea", title="Supplier onboarding in one form", summary="s")
    page.get_by_role("button", name="Refresh", exact=True).last.click()
    card2 = page.locator(CARD, has_text=second["title"]).first
    card2.wait_for(state="visible", timeout=15_000)
    card2.get_by_role("button", name="Open", exact=True).click()
    drawer2 = page.locator(f"[role='dialog'][aria-label='{second['title']}']")
    drawer2.wait_for(state="visible", timeout=10_000)
    drawer2.locator("[data-testid='cortex-drawer-promote']").click()
    page.locator("[role='alertdialog']").get_by_role("button", name="Create project").click()
    page.locator(DRAWER).wait_for(state="visible", timeout=10_000)
    drawer2.wait_for(state="detached", timeout=5_000)
    page.get_by_role("button", name="Hide (keeps running)").click()
    card2.locator(JOB_BADGE).wait_for(state="visible", timeout=10_000)
    _wait_job_end(page, calls, second["id"])


def _wait_job_end(page, calls: list[str], item_id: str, timeout_s: float = 20.0) -> None:
    """The promotion the test's fake runs ends on its own; wait for it so the
    session's job registry is quiet before the next test seeds its own."""
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if item_id in calls and page.locator(JOB_BADGE).count() == 0:
            return
        page.wait_for_timeout(500)
    raise AssertionError("the promotion started from the drawer did not finish")


def test_create_project_keeps_running_and_the_card_follows_it(page, slow_promotion) -> None:
    item, calls = slow_promotion
    _open_ideas(page)

    card = page.locator(CARD, has_text=item["title"]).first
    card.wait_for(state="visible", timeout=15_000)
    card.get_by_role("button", name="Create project").click()
    # The confirmation, then the run's panel.
    page.locator("[role='alertdialog']").get_by_role("button", name="Create project").click()
    drawer = page.locator(DRAWER)
    drawer.wait_for(state="visible", timeout=10_000)
    assert drawer.get_attribute("data-job-status") == "running"
    page.get_by_text("keeps running if you close this panel").wait_for(timeout=5_000)

    # Hide it: the run keeps going, and the card says so.
    page.get_by_role("button", name="Hide (keeps running)").click()
    drawer.wait_for(state="detached", timeout=5_000)
    card.locator(JOB_BADGE).wait_for(state="visible", timeout=10_000)
    assert "Creating project" in card.locator(JOB_BADGE).inner_text()
    assert card.get_by_role("button", name="Create project", exact=True).count() == 0

    # Tapping the card's button again follows the same run.
    card.locator(JOB_BUTTON).click()
    drawer.wait_for(state="visible", timeout=10_000)
    page.locator(f"{DRAWER}[data-job-status='done']").wait_for(timeout=20_000)
    page.get_by_text("Project created").first.wait_for(timeout=5_000)
    assert "write_file" in page.locator("[data-testid='cortex-job-log']").inner_text()
    assert calls == [item["id"]], f"promotion ran {len(calls)} times"

    page.get_by_role("button", name="Close", exact=True).last.click()
    drawer.wait_for(state="detached", timeout=5_000)

    # The Runs list holds it, finished.
    page.locator("[data-testid='cortex-runs-button']").click()
    row = page.locator("[data-testid='cortex-run-row']", has_text=item["title"]).first
    row.wait_for(state="visible", timeout=10_000)
    assert row.get_attribute("data-job-status") == "done"
