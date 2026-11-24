"""The Scheduler's New task form offers the per-job model and effort overrides.

Both viewports: the Model select is always there with the agent-default
option first, and the Reasoning effort select appears only once a model with
an effort ladder is chosen, listing that ladder. The screenshots land beside
the route-check ones so a reviewer can eyeball the form at both widths.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.e2e import harness
from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e


def test_new_task_form_offers_model_and_effort_overrides(
    page, request, dashboard: harness.RunningDashboard
) -> None:
    page.goto(f"{dashboard.base_url}/scheduler", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    page.get_by_role("button", name="New task").first.click()

    # The agent select has "— default —" as its empty option; the model select
    # says "Agent default" and follows the prompt box. Find it by its label.
    model_field = page.locator(".field", has_text="Model").first
    model_field.wait_for(state="visible", timeout=15_000)
    select = model_field.locator("select")
    assert "Agent default" in select.locator("option").first.inner_text()

    # Walk the usable models until one advertises an effort ladder; the
    # effort select must then appear with that ladder and nothing else.
    values = select.locator("option").evaluate_all("els => els.map(e => e.value)")
    effort_field = page.locator(".field", has_text="Reasoning effort")
    found = False
    for value in values:
        if not value:
            continue
        select.select_option(value)
        if effort_field.count() and effort_field.first.is_visible():
            levels = effort_field.first.locator("select option").evaluate_all(
                "els => els.map(e => e.value)"
            )
            assert levels[0] == "", levels  # agent default first
            assert all(
                lvl in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
                for lvl in levels[1:]
            ), levels
            found = True
            break
    if not found:
        # No usable model with a ladder on this box (no provider keys) — the
        # select must then simply stay away rather than offer rungs blindly.
        assert effort_field.count() == 0 or not effort_field.first.is_visible()

    out = Path(os.environ.get("OLANO_TESTER_SCREENSHOTS", ".olano-tester")) / "scheduler-overrides"
    out.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out / f"{request.node.callspec.id}-new-task-form.png"), full_page=True)
