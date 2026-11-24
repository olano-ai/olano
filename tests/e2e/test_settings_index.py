"""Every settings-search entry still points at something that exists.

The command palette indexes settings by name and navigates to
``?focus=<setting id>``; ``core/settingFocus.ts`` then finds
``[data-setting-id="<id>"]`` and scrolls to it. Nothing in TypeScript ties the
catalog to the JSX that renders those attributes, so a renamed or deleted field
would leave the palette offering a result that lands on the right page and then
silently does nothing.

This is the guard. It reads the index the app itself loaded (the palette
publishes it on ``window.__olanoSettingsIndex`` for exactly this), groups the
entries by target route, and visits each route once — a dozen navigations, not
one per entry — asserting every anchor is in the DOM.

Reading the live index rather than a generated JSON file is deliberate: a build
artifact is one more thing that can go stale, and staleness is the failure this
module exists to catch.
"""

from __future__ import annotations

import json
from collections import defaultdict

import pytest

from tests.e2e import harness
from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

# The palette's own selectors. Kept here rather than in route_checks.py because
# nothing else drives the palette; move them there if that changes.
PALETTE = ".cmdk[role='dialog']"


def _load_index(page, base_url: str) -> list[dict]:
    """Open the palette so it imports the catalog chunk, then read the index."""
    page.goto(base_url.rstrip("/") + "/overview", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    # ⌘K/Ctrl+K is a bare window keydown listener in Layout; Control works on
    # the Linux headless build regardless of what the user's platform sends.
    page.keyboard.press("Control+k")
    page.locator(PALETTE).wait_for(state="visible", timeout=15_000)
    # The catalog is a dynamic import, so it lands a tick or two after the open.
    page.wait_for_function("() => Array.isArray(window.__olanoSettingsIndex)", timeout=15_000)
    index = page.evaluate("() => window.__olanoSettingsIndex")
    page.keyboard.press("Escape")
    return index


def _url(suffix: str, params: dict | None) -> str:
    if not params:
        return suffix
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{suffix}?{query}"


def test_every_setting_entry_has_a_live_anchor(
    page,
    dashboard: harness.RunningDashboard,
) -> None:
    """Each catalog id resolves to a data-setting-id on its own route."""
    index = _load_index(page, dashboard.base_url)
    assert index, "the settings index loaded empty — the catalog chunk failed to import"

    # Entries whose target only mounts under a live condition (a gateway is
    # attached, usage budgets are configured) are allowed to be missing; the
    # flag is set in the catalog, so an unflagged entry that vanishes still
    # fails here.
    checkable = [e for e in index if not e.get("conditional")]
    # Per-agent routes are substituted at select time from the current agent;
    # they get their own coverage with the agent-settings phase.
    checkable = [e for e in checkable if ":agent" not in e["route"]]
    assert checkable, "no unconditional entries to check — the catalog shape changed"

    by_target: dict[tuple[str, str], list[str]] = defaultdict(list)
    for entry in checkable:
        key = (entry["route"], json.dumps(entry.get("params") or {}, sort_keys=True))
        by_target[key].append(entry["id"])

    missing: list[str] = []
    for (route, params_json), ids in sorted(by_target.items()):
        page.goto(
            dashboard.base_url.rstrip("/") + _url(route, json.loads(params_json)),
            wait_until="domcontentloaded",
        )
        wait_for_app_shell(page)
        # Anchor on the first id so the page has actually rendered its form
        # before we measure the rest; otherwise a slow mount reads as 60 misses.
        try:
            page.locator(f'[data-setting-id="{ids[0]}"]').wait_for(state="attached", timeout=15_000)
        except Exception:
            missing.append(f"{route}: {ids[0]} (nothing rendered — is the route reachable?)")
            continue
        # "attached", not "visible": an anchor inside a collapsed <details> or
        # below the fold is still a valid target — settingFocus opens and
        # scrolls to it. Presence in the DOM is the contract.
        present = set(
            page.eval_on_selector_all(
                "[data-setting-id]",
                "els => els.map(e => e.getAttribute('data-setting-id'))",
            )
        )
        missing.extend(f"{route}: {i}" for i in ids if i not in present)

    if missing:
        pytest.fail(
            "Settings-search entries point at anchors that no longer exist:\n  "
            + "\n  ".join(missing)
            + "\n\nEither the field moved/was renamed (update its id in "
            "olano/dashboard_2.0/src/core/settingsIndex/catalog.ts and the "
            "settingId prop that renders it), or it is now conditionally "
            "rendered (mark the entry `conditional: true`)."
        )


def test_palette_search_navigates_to_the_field_and_flashes(
    page,
    dashboard: harness.RunningDashboard,
) -> None:
    """The end-to-end promise: type a field's name, land on that control.

    Asserted on Config → Speech → the TTS provider row, which is three sections
    and roughly forty controls down a page with no anchors of its own before
    this existed.
    """
    _load_index(page, dashboard.base_url)

    page.keyboard.press("Control+k")
    page.locator(PALETTE).wait_for(state="visible", timeout=15_000)
    page.locator(f"{PALETTE} input").fill("voice replies")

    row = page.locator(".cmdk-item", has_text="Voice replies")
    row.first.wait_for(state="visible", timeout=15_000)
    row.first.click()

    target = page.locator('[data-setting-id="config.speech.tts"]')
    target.wait_for(state="visible", timeout=15_000)
    # The highlight is what tells the user which of the page's controls they
    # were sent to; it is removed ~2s later, so this races deliberately close
    # to the navigation.
    page.locator('[data-setting-id="config.speech.tts"].setting-flash').wait_for(
        state="attached", timeout=5_000
    )
    # The focus param is consumed, so Back and reload don't replay the flash.
    page.wait_for_function(
        "() => !new URLSearchParams(location.search).has('focus')", timeout=5_000
    )
