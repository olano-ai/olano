"""Selectors and helpers for the Cortex settings card — one source of truth.

Three modules drive this card (``test_cortex_per_agent``,
``test_cortex_phase_rows``, ``test_cortex_guidance``), and everything
configurable about Cortex now lives inside it: the two-tier settings table
("How it runs"), the phase schedule, and CORTEX.md ("Creative direction").
The card is **collapsed by default**, so reaching any of those controls starts
with the same two lines — which is exactly the kind of duplication that leaves
one suite passing and another stranded when the markup moves. Same reasoning as
``route_checks.py`` holding the app-shell contract.
"""

from __future__ import annotations

SETTINGS_HEAD = ".cortex-settings-head"
SETTINGS_SUMMARY = ".cortex-settings-summary"
#: The three sub-sections inside the card, each a disclosure of its own.
SUBSECTION_TOGGLE = ".cortex-subsection-toggle"


def open_cortex_settings(page, timeout: int = 15_000) -> None:
    """Expand the settings card. No-op-safe only on a freshly loaded page —
    the card's open state persists, so callers land here right after a goto."""
    head = page.locator(SETTINGS_HEAD).first
    head.wait_for(state="visible", timeout=timeout)
    if not page.evaluate("el => !!el.closest('details')?.open", head.element_handle()):
        head.click()
    page.locator(".cortex-settings-body").first.wait_for(state="visible", timeout=timeout)


def open_subsection(page, name: str, timeout: int = 10_000) -> None:
    """Expand one of the card's sub-sections by its visible name, if collapsed.

    Asks the DOM whether it is already open rather than clicking blind: the
    disclosure states persist across a reload, so a blind click is as likely to
    close the section as to open it.
    """
    toggle = page.locator(f"{SUBSECTION_TOGGLE}:has-text('{name}')").first
    toggle.wait_for(state="visible", timeout=timeout)
    if not page.evaluate("el => !!el.closest('details')?.open", toggle.element_handle()):
        toggle.click()
