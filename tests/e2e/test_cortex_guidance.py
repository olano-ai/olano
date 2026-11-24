"""E2E: the Cortex settings card's "Creative direction" sub-section.

Exercises the collapsible CORTEX.md editor that no in-process API test can see:
the section renders, expands with its help text, opens a raw editor, saves via
``PUT /api/files/CORTEX.md``, and re-renders the saved markdown as a preview.

It was a card of its own ("Customize your Cortex Engine") beside the autonomy
panel until both were consolidated into one collapsed **Cortex settings** card,
so the walk now opens that card first.

Runs at both viewports via the ``page`` fixture. Each run writes a unique marker
so the save stays "dirty" (the Save button is intentionally disabled for a no-op
edit) even though both viewport passes share one backend workspace.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.cortex_ui import open_cortex_settings, open_subsection

pytestmark = pytest.mark.e2e


def _collapse_sidebar(page) -> None:
    """Collapse the desktop nav to a rail so it can't overlay the content and
    intercept clicks. Below the breakpoint the sidebar is an off-canvas drawer
    (closed by default) whose toggle sits inside the hidden drawer — nothing
    overlays the content, so there is nothing to collapse."""
    if page.locator("header.mobile-topbar").is_visible():
        return
    toggle = page.locator("button.sidebar-toggle")
    if toggle.count() and toggle.first.get_attribute("aria-expanded") == "true":
        toggle.first.click()
        page.wait_for_timeout(300)


def test_cortex_guidance_edit_and_preview(page) -> None:
    base = page.url.split("/overview")[0].rstrip("/")
    page.goto(base + "/cortex", wait_until="domcontentloaded", timeout=20_000)
    wait_for_app_shell(page)
    _collapse_sidebar(page)

    # Everything configurable lives in one collapsed card; the editor is its
    # third sub-section, beside "How it runs" and "Phases & schedule".
    open_cortex_settings(page)
    page.get_by_text("Creative direction", exact=True).wait_for(state="visible", timeout=15_000)

    # Expanding it reveals the how-to help text explaining CORTEX.md.
    open_subsection(page, "Creative direction")
    page.get_by_text("CORTEX.md steers what your agent", exact=False).wait_for(
        state="visible", timeout=8_000
    )

    # Open the raw editor, write unique guidance, and save.
    page.get_by_role("button", name="Edit").first.click()
    editor = page.locator("section.card textarea.input")
    editor.wait_for(state="visible", timeout=8_000)

    marker = f"Ship it {uuid4().hex[:8]}"
    editor.fill(f"## Goals\n- {marker}\n\n## Avoid\n- No crypto-trading ideas\n")
    page.get_by_role("button", name="Save", exact=True).click()

    # The editor closes and the saved markdown renders as a preview bullet.
    page.get_by_text(marker, exact=False).wait_for(state="visible", timeout=10_000)
