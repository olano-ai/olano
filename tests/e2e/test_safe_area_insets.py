"""E2E: every anchored surface responds to the iOS safe-area insets.

Installed to the home screen the dashboard runs edge to edge — ``index.html``
sets ``viewport-fit=cover`` and ``apple-mobile-web-app-status-bar-style:
black-translucent`` — so the OS draws its status bar and home indicator ON TOP
of the app. Anything anchored to y=0 or to the viewport bottom has to inset
itself or it renders underneath them: that is how the simple shell's header
ended up under the clock, unclickable.

**Why this test can exist at all.** Headless Chromium reports ``0px`` for every
``env(safe-area-inset-*)``, so a rule written with a bare ``env()`` is
untestable — it looks identical whether or not it is wired up. Every rule here
instead consumes ``var(--safe-top)`` / ``var(--safe-bottom)``, which this test
*overrides* with visible values. Keep it that way: a surface that reaches for
``env()`` directly silently drops out of this coverage.

The numbers are the real iPhone insets (44pt status bar, 34pt home indicator),
but nothing depends on the exact values — only that the padding responds.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import APP_SHELL, SIMPLE_COMPOSER, SIMPLE_ROOT

pytestmark = pytest.mark.e2e

SAFE_TOP = 44
SAFE_BOTTOM = 34

# Injected instead of toggling display-mode, which a page cannot fake. The
# tokens are the seam every rule goes through, so overriding them exercises the
# same cascade a real standalone launch would.
_OVERRIDE = f"""
:root {{
  --safe-top: {SAFE_TOP}px !important;
  --safe-bottom: {SAFE_BOTTOM}px !important;
}}
"""


def _px(page, selector: str, prop: str) -> float:
    """Computed length in px for the first match of `selector`.

    Waits for *attached*, not visible: ``.toast-host`` is an always-mounted
    portal that is empty (and therefore zero-size) until a toast fires, and a
    hidden element still has a computed style.
    """
    page.wait_for_selector(selector, state="attached", timeout=20_000)
    raw = page.evaluate(
        "([sel, prop]) => getComputedStyle(document.querySelector(sel))[prop]",
        [selector, prop],
    )
    return float(str(raw).removesuffix("px"))


def _set_ui_mode(value: str) -> None:
    from olano.config.home import ensure_olano_home, update_home_config

    ensure_olano_home()
    update_home_config({"ui_mode": value})


def _measure(page, selector: str, prop: str) -> tuple[float, float]:
    """(before, after) computed length, with the safe-area tokens overridden."""
    before = _px(page, selector, prop)
    page.add_style_tag(content=_OVERRIDE)
    after = _px(page, selector, prop)
    return before, after


def test_advanced_shell_insets_topbar_and_composer(page) -> None:
    """The dashboard's own anchored edges: the mobile topbar and the composer."""
    _set_ui_mode("advanced")
    page.reload()
    wait_for_app_shell(page)

    # The topbar is mobile-only. Decide on the viewport rather than on
    # is_visible(), so the mobile run can never quietly assert nothing.
    width = (page.viewport_size or {}).get("width", 0)
    if width > 900:
        pytest.skip("the mobile topbar is display:none above the breakpoint")

    before, after = _measure(page, ".mobile-topbar", "paddingTop")
    assert after - before == pytest.approx(SAFE_TOP), (
        "the mobile topbar must inset by the status-bar height, or its "
        "hamburger and bell sit under the clock"
    )


def test_simple_shell_header_insets_for_the_status_bar(page) -> None:
    """The reported bug: the simple shell's header under the iPhone clock.

    This shell never mounts the advanced ``<Layout>``, so it never rendered the
    one element that consumed ``--safe-top`` — the whole inset mechanism was
    dead code here until simple.css consumed the tokens itself.
    """
    _set_ui_mode("simple")
    page.reload()
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)

    before, after = _measure(page, ".simple-head", "paddingTop")
    assert after - before == pytest.approx(SAFE_TOP), (
        ".simple-head must inset by the status-bar height — it is the first "
        "pixel row of a 100dvh shell anchored at y=0"
    )


def test_simple_shell_composer_clears_the_home_indicator(page) -> None:
    _set_ui_mode("simple")
    page.reload()
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)

    before, after = _measure(page, SIMPLE_COMPOSER, "paddingBottom")
    # A floor, not a sum: the composer is lifted exactly clear of the home
    # indicator, never clear of it plus its original gap (which is the wasted
    # strip the advanced composer used to show).
    assert after == pytest.approx(SAFE_BOTTOM), (
        f"expected the bottom padding to become max(base, {SAFE_BOTTOM}px); "
        f"got {after} from a base of {before}"
    )


def test_toast_host_clears_the_home_indicator(page) -> None:
    """Shared by both shells, and reached by neither shell's own insets."""
    _set_ui_mode("advanced")
    page.reload()
    wait_for_app_shell(page)

    # .toast-host is always in the DOM (it is the portal), so it measures even
    # with no toast showing.
    before, after = _measure(page, ".toast-host", "bottom")
    assert after - before == pytest.approx(SAFE_BOTTOM), (
        "a toast must not land on the home indicator"
    )


# ── the window's own height ──────────────────────────────────────────────────
# The insets above are only half of running edge to edge. The other half is how
# tall the shell believes the window is: installed to the home screen, iOS
# resolves `dvh` to the window MINUS the status-bar inset, so a shell sized
# `100dvh` ended one inset short of the bottom of the screen — a dead strip of
# page background under the composer, with the home indicator floating in it.
# Both shells (and every fixed overlay) now read `--app-vh`, which resolves to
# `dvh` in a browser tab and back to `vh` when installed.
#
# Headless Chromium has no such quirk — `vh`, `dvh` and the window agree — so
# these assert the two halves of the mechanism rather than the symptom: that
# the shell reaches the bottom edge at every viewport, and that the installed
# cascade hands it `vh` rather than the unit that lies.


# Pixels of window left unused below a shell — the reported bug, measured.
_GAP_BELOW = """(sel) => {
  const box = document.querySelector(sel).getBoundingClientRect();
  return document.documentElement.clientHeight - box.bottom;
}"""


def _standalone_app_vh(page) -> tuple[str, str]:
    """(`--app-vh` in a tab, `--app-vh` installed) as the cascade resolves it.

    The inline value the runtime publishes is cleared first: it and the
    stylesheet must agree, and this half of the test is about the stylesheet.
    A custom property computes to its token, so the units survive to be read.
    """
    return tuple(
        page.evaluate(
            """() => {
              const root = document.documentElement;
              const read = () => getComputedStyle(root).getPropertyValue('--app-vh').trim();
              root.style.removeProperty('--app-vh');
              const tab = read();
              root.dataset.standalone = '1';
              const installed = read();
              delete root.dataset.standalone;
              return [tab, installed];
            }"""
        )
    )


def test_advanced_shell_reaches_the_bottom_of_the_window(page) -> None:
    _set_ui_mode("advanced")
    page.reload()
    wait_for_app_shell(page)

    gap = page.evaluate(_GAP_BELOW, APP_SHELL)
    assert gap == pytest.approx(0, abs=1), (
        f"the dashboard shell stops {gap}px above the bottom of the window — "
        "that strip is the wasted gap the installed app showed"
    )

    tab, installed = _standalone_app_vh(page)
    assert tab == "100dvh", "a browser tab must track its retractable chrome"
    assert installed == "100vh", (
        "installed there is no chrome to track and iOS's dvh under-reports; "
        f"the standalone cascade handed back {installed!r}"
    )


def test_simple_shell_reaches_the_bottom_of_the_window(page) -> None:
    _set_ui_mode("simple")
    page.reload()
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)

    gap = page.evaluate(_GAP_BELOW, SIMPLE_ROOT)
    assert gap == pytest.approx(0, abs=1), (
        f"the simple shell stops {gap}px above the bottom of the window"
    )

    tab, installed = _standalone_app_vh(page)
    assert (tab, installed) == ("100dvh", "100vh")


def test_installed_runtime_pins_the_window_height(page) -> None:
    """The measured half: `ui/viewportInset.ts` publishes the layout viewport.

    The stylesheet's `vh` and this measurement are belt and braces for the same
    number — the runtime one also survives an engine whose `vh` is wrong, and
    follows a rotation or a split-screen resize.
    """
    _set_ui_mode("advanced")
    page.reload()
    wait_for_app_shell(page)

    published = page.evaluate(
        """async () => {
          const root = document.documentElement;
          const settle = () =>
            new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
          root.dataset.standalone = '1';
          window.dispatchEvent(new Event('resize'));
          await settle();
          const installed = root.style.getPropertyValue('--app-vh');
          delete root.dataset.standalone;
          window.dispatchEvent(new Event('resize'));
          await settle();
          return { installed, tab: root.style.getPropertyValue('--app-vh'),
                   height: root.clientHeight };
        }"""
    )
    assert published["installed"] == f"{published['height']}px"
    assert published["tab"] == "", (
        "in a browser tab the stylesheet's dvh must stay in charge — an inline "
        "value on :root outranks every rule in the cascade"
    )
