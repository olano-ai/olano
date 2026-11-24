"""E2E: a dialog's action row is reachable on a phone.

The connector editor is the tall one — a form plus a six-step setup guide — and
on iOS it was unusable: ``.modal-overlay`` sized itself with ``inset: 0`` and
the panel capped at ``90vh``, both of which resolve against the *layout*
viewport. Mobile Safari/Chrome keep that at the toolbars-hidden height, so the
bottom of the dialog — the Cancel/Save row — sat behind the browser's bottom
bar. Scrolling the body only moved the guide; nothing brought the buttons back,
and the panel could not be moved or dismissed except by the X.

The shell is now viewport-aware (``100dvh`` + ``--modal-vh``) and, below
640px, a bottom sheet: full width, pinned action row, only the body scrolls.
These tests pin the properties that make it usable — the footer is inside the
viewport, it is the body that scrolls, and the buttons are thumb-sized on a
phone — at both viewports, since the desktop dialog must not regress into a
sheet.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

AGENT = "merli"
TAP_TARGET_PX = 44  # the usual floor for a touch target


@pytest.fixture(scope="module")
def sheet_dashboard():
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard(gateway=harness.build_mock_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


@pytest.fixture
def _agent_on_disk(lionclaw_home):
    """An agent with the Telegram connector — the tallest editor in the app."""
    cfg_dir = lionclaw_home / "agents" / AGENT
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": AGENT,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
                "channels": [{"type": "platform:telegram", "allowed_users": []}],
            }
        ),
        encoding="utf-8",
    )
    return cfg_dir


@pytest.fixture(params=["desktop", "mobile"])
def viewport(request):
    return request.param


@pytest.fixture
def modal(sheet_dashboard, _agent_on_disk, viewport):
    """The connector edit dialog, open, on a page sized for ``viewport``."""
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[viewport])
        page = ctx.new_page()
        page.goto(
            sheet_dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=connectors",
            wait_until="domcontentloaded",
        )
        edit = page.get_by_role("button", name="Edit connector").first
        edit.wait_for(state="visible", timeout=20_000)
        edit.click()
        page.locator(".connector-shell").first.wait_for(state="visible", timeout=15_000)
        page.wait_for_timeout(400)  # sheet transition
        try:
            yield page
        finally:
            ctx.close()
            browser.close()


def _viewport_height(page) -> int:
    return int(page.viewport_size["height"])


def test_the_action_row_is_inside_the_viewport(modal, viewport) -> None:
    """Save must be on screen — off-screen it may as well not exist."""
    save = modal.locator(".modal-foot button").last
    box = save.bounding_box()
    assert box is not None, "the dialog's primary action has no box"
    bottom = box["y"] + box["height"]
    assert bottom <= _viewport_height(modal) + 0.5, (
        f"[{viewport}] the action row ends {bottom:.0f}px down, "
        f"past the {_viewport_height(modal)}px viewport"
    )
    assert box["y"] >= -0.5, f"[{viewport}] the action row starts above the viewport"


def test_the_body_scrolls_and_the_footer_does_not(modal, viewport) -> None:
    """The guide is taller than any phone: it has to scroll *inside* the panel,
    leaving the actions pinned."""
    metrics = modal.evaluate(
        """() => {
          const b = document.querySelector('div[role=dialog] .modal-body');
          return b ? {scroll: b.scrollHeight, client: b.clientHeight} : null;
        }"""
    )
    assert metrics, "the dialog has no scrollable body"
    assert metrics["scroll"] > metrics["client"], (
        f"[{viewport}] the connector body should overflow its box: {metrics}"
    )

    save = modal.locator(".modal-foot button").last
    before = save.bounding_box()
    modal.evaluate(
        "() => { const b = document.querySelector('div[role=dialog] .modal-body');"
        " b.scrollTop = b.scrollHeight; }"
    )
    modal.wait_for_timeout(200)
    after = save.bounding_box()
    assert before and after
    assert abs(after["y"] - before["y"]) <= 1, (
        f"[{viewport}] the action row moved when the body scrolled "
        f"({before['y']:.0f} → {after['y']:.0f})"
    )


def test_the_panel_shrinks_with_the_visible_viewport(modal, viewport) -> None:
    """The regression itself.

    Headless Chromium has no toolbars, so the only way to reproduce an iPhone
    here is to shrink the visible viewport the way iOS does — which is exactly
    what ``--modal-vh`` carries (ui/viewportInset.ts publishes it from
    ``visualViewport``, i.e. minus the browser bars and the keyboard). Drop it
    by 200px and the dialog must give back those 200px. The old shell, sized
    ``inset: 0`` / ``90vh`` against the layout viewport, would leave the action
    row roughly where it was — under the browser bar, unreachable.
    """
    visible = _viewport_height(modal) - 200
    modal.evaluate(f"() => document.documentElement.style.setProperty('--modal-vh', '{visible}px')")
    modal.wait_for_timeout(200)

    box = modal.locator(".modal-foot button").last.bounding_box()
    assert box is not None
    bottom = box["y"] + box["height"]
    assert bottom <= visible + 0.5, (
        f"[{viewport}] with {visible}px visible the action row still ends at "
        f"{bottom:.0f}px — it is behind the browser chrome"
    )


def test_the_phone_gets_a_full_width_sheet_with_thumb_sized_actions(modal, viewport) -> None:
    """Below the sheet breakpoint the dialog spans the screen and its buttons
    are tap targets; above it, the centred dialog is untouched."""
    panel = modal.locator("div[role='dialog']").first
    pbox = panel.bounding_box()
    save = modal.locator(".modal-foot button").last
    sbox = save.bounding_box()
    assert pbox and sbox
    width = int(modal.viewport_size["width"])

    if viewport == "mobile":
        assert pbox["width"] >= width - 1, (
            f"the sheet should span the screen, got {pbox['width']:.0f}"
        )
        assert pbox["x"] <= 1, "the sheet should start at the left edge"
        assert sbox["height"] >= TAP_TARGET_PX - 0.5, (
            f"phone actions must be {TAP_TARGET_PX}px tall, got {sbox['height']:.0f}"
        )
    else:
        assert pbox["width"] < width, "the desktop dialog must stay a centred card"
        assert pbox["x"] > 1, "the desktop dialog must keep its backdrop margin"
