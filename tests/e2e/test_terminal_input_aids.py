"""E2E: the terminal's two input aids — command recall and the touch key bar.

Both exist because a browser terminal is missing things a real one has. Recall
answers "what did I run here last time" without a `history | grep`; the key bar
supplies Tab, the arrows, Esc and Ctrl — none of which a phone keyboard has —
plus clipboard actions, since there is no right-click menu on a canvas of text.

Path completion is deliberately NOT tested here: it belongs to the shell (the
PTY runs an interactive bash), and asserting on readline would be testing bash.
What IS asserted is the boundary — that recall never eats the keys the shell
needs, which is why accepting a suggestion is bound to `→` and not to Tab.

Skips cleanly when the deployment has no terminal (the view says so itself).
"""

from __future__ import annotations

import json

import pytest

from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

SEEDED = "echo olano-e2e-recall"
MOUNT = ".term-mount .xterm"
OPEN_DOT = ".term-tab.active .term-dot.open"
STRIP = ".term-suggest"
FIRST = ".term-suggest-item.is-first"
KEYS = ".term-keys"


def _open_terminal(page, dashboard) -> None:
    """Seed a command history, then open a live terminal session."""
    # Double-encoded on purpose: the inner dump is the stored value (a JSON
    # array), the outer one makes it a JS string literal in the script.
    page.add_init_script(
        "try { localStorage.setItem('lc.term.history', "
        + json.dumps(json.dumps([SEEDED]))
        + "); } catch {}"
    )
    page.goto(dashboard.base_url.rstrip("/") + "/terminal", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    if page.locator("text=Terminal is disabled").count():
        pytest.skip("terminal disabled on this deployment")
    page.wait_for_selector(MOUNT, timeout=20_000)
    # The shell must be live before we type: keystrokes sent to a connecting
    # session reach no PTY and never echo back.
    page.wait_for_selector(OPEN_DOT, timeout=20_000)
    # …and its *prompt* must be up, not just the socket. A login bash spends a
    # moment sourcing rc files, during which the tty buffers keystrokes and
    # readline has not started — so a `→` sent then reaches the shell as a bare
    # cursor key instead of being read as an accept.
    page.wait_for_function(
        "() => /[$#>]\\s*$/.test((document.querySelector('.term-host')?.innerText || '')"
        ".replace(/\\u00a0/g, ' ').trimEnd())",
        timeout=20_000,
    )


def _type_into_terminal(page, text: str) -> None:
    """Focus the terminal and type, at human speed.

    xterm listens on a hidden textarea that the store focuses on pointerdown;
    typing before that lands drops the first characters, and a dropped character
    means a different line — which is precisely what suppresses suggestions.
    """
    page.locator(".term-host").click()
    page.wait_for_function(
        "() => document.activeElement?.classList.contains('xterm-helper-textarea')",
        timeout=5_000,
    )
    page.keyboard.type(text, delay=40)


def test_recall_offers_a_previous_command_and_arrow_right_accepts(page, dashboard) -> None:
    _open_terminal(page, dashboard)

    _type_into_terminal(page, "echo ol")
    page.wait_for_selector(FIRST, timeout=5_000)
    assert SEEDED in (page.locator(FIRST).inner_text() or "")

    page.keyboard.press("ArrowRight")
    # Accepting types the remainder into the shell, so the line now IS the
    # suggestion — nothing left to suggest, and the shell echoes the whole
    # command back (the DOM renderer is what makes that readable from here).
    # Whitespace is stripped from both sides of the comparison: a 390px viewport
    # is ~30 columns, so the prompt plus the command wraps across rows and the
    # renderer puts a line break inside the text we are looking for.
    page.wait_for_selector(STRIP, state="detached", timeout=5_000)
    page.wait_for_function(
        "cmd => ((document.querySelector('.term-host')?.innerText || '')"
        ".replace(/\\s+/g, '')).includes(cmd.replace(/\\s+/g, ''))",
        arg=SEEDED,
        timeout=5_000,
    )


def test_tab_is_left_to_the_shell(page, dashboard) -> None:
    """Recall must never claim Tab — that key is how paths get completed."""
    _open_terminal(page, dashboard)

    _type_into_terminal(page, "echo ol")
    page.wait_for_selector(FIRST, timeout=5_000)

    page.keyboard.press("Tab")
    # readline rewrote (or refused to rewrite) the line; either way the local
    # model can no longer be trusted, so the strip stands down rather than
    # offering a completion against a line it can't see.
    page.wait_for_selector(STRIP, state="detached", timeout=5_000)


def test_key_bar_is_touch_only(page, dashboard) -> None:
    """Present where the keys are missing, absent where the keyboard has them."""
    _open_terminal(page, dashboard)

    coarse = page.evaluate("() => matchMedia('(pointer: coarse)').matches")
    if not coarse and page.viewport_size["width"] > 900:
        assert page.locator(KEYS).count() == 0
        return

    page.wait_for_selector(KEYS, timeout=5_000)
    # Every key is a real, thumb-sized target — the point of the bar.
    assert page.locator(f"{KEYS} button").count() >= 6
    assert page.locator(f"{KEYS} button").first.bounding_box()["height"] >= 32
