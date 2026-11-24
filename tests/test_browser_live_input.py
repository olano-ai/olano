"""A person's input in the live view, replayed on a real headless Chromium.

The dashboard sends clicks as fractions of the frame, keys by name, a paste as
text, and asks for the selection on copy or cut (``olano/browser/live.py``).
These run those exact paths against a real page, because the failure they guard
against (a keystroke or a copy that reaches nothing) is invisible to a mock.
Skipped when playwright or its Chromium build isn't installed.
"""

from __future__ import annotations

import time
from functools import lru_cache
from types import SimpleNamespace

import pytest


@lru_cache(maxsize=1)
def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="playwright/chromium not installed"
)

_W, _H = 1280, 800
_PAGE = """<!doctype html><style>body{margin:0}
#u{position:absolute;left:100px;top:100px;width:300px;height:40px}
#pw{position:absolute;left:100px;top:200px;width:300px;height:40px}</style>
<input id=u><input id=pw type=password>"""


@pytest.fixture
async def live_page(monkeypatch):
    from playwright.async_api import async_playwright

    from olano.browser import sessions

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": _W, "height": _H})
        await page.set_content(_PAGE)
        sess = SimpleNamespace(page=page, controller="user", touch=lambda: None, dialog=None)
        monkeypatch.setitem(sessions._sessions, "merli", sess)
        try:
            yield sess
        finally:
            await browser.close()


def _at(x: float, y: float) -> dict[str, float]:
    return {"x": x / _W, "y": y / _H}


async def _value(page, sel: str) -> str:
    return await page.eval_on_selector(sel, "e => e.value")


async def test_a_click_then_keys_type_into_the_field(live_page):
    from olano.browser.live import dispatch_input

    for kind in ("down", "up"):
        assert await dispatch_input("merli", {"t": kind, **_at(250, 120)}) is None
    for ch in "Hi!":
        assert await dispatch_input("merli", {"t": "keydown", "key": ch}) is None
        assert await dispatch_input("merli", {"t": "keyup", "key": ch}) is None
    assert await _value(live_page.page, "#u") == "Hi!"


async def test_a_paste_arrives_as_text_at_the_caret(live_page):
    from olano.browser.live import dispatch_input

    await live_page.page.click("#u")
    assert await dispatch_input("merli", {"t": "text", "text": "pasted value"}) is None
    assert await _value(live_page.page, "#u") == "pasted value"


async def test_a_key_the_browser_has_no_name_for_is_ignored_quietly(live_page):
    from olano.browser.live import dispatch_input

    assert await dispatch_input("merli", {"t": "keydown", "key": "AudioVolumeMute-ish"}) is None
    assert await dispatch_input("merli", {"t": "keyup", "key": "AudioVolumeMute-ish"}) is None


async def test_copy_returns_the_selection_in_a_field_and_cut_removes_it(live_page):
    from olano.browser.live import copy_selection, dispatch_input

    page = live_page.page
    await page.fill("#u", "hello world")
    await page.eval_on_selector("#u", "e => { e.focus(); e.setSelectionRange(6, 11); }")
    assert await copy_selection("merli") == ("world", None)
    assert await _value(page, "#u") == "hello world"

    assert await copy_selection("merli", cut=True) == ("world", None)
    deadline = time.time() + 5
    while await _value(page, "#u") != "hello " and time.time() < deadline:
        await page.wait_for_timeout(50)
    assert await _value(page, "#u") == "hello "
    # Control (Command on a Mac, replayed as Control) + z undoes the cut.
    await dispatch_input("merli", {"t": "keydown", "key": "Control"})
    await dispatch_input("merli", {"t": "keydown", "key": "z"})
    await dispatch_input("merli", {"t": "keyup", "key": "z"})
    await dispatch_input("merli", {"t": "keyup", "key": "Control"})
    assert await _value(page, "#u") == "hello world"


async def test_a_password_never_leaves_the_page(live_page):
    from olano.browser.live import copy_selection

    page = live_page.page
    await page.fill("#pw", "s3cret")
    await page.eval_on_selector("#pw", "e => { e.focus(); e.select(); }")
    assert await copy_selection("merli") == ("", None)


async def test_copy_needs_control_like_every_other_input(live_page):
    from olano.browser.live import copy_selection

    live_page.controller = "agent"
    text, error = await copy_selection("merli")
    assert text == ""
    assert error and "Take control" in error


async def test_a_double_click_selects_a_word_and_two_single_clicks_do_not(live_page):
    """The page tells a double click by the press's click count: two presses
    of count 1 (what the live view sent before) selected nothing."""
    from olano.browser.live import dispatch_input

    page = live_page.page
    await page.set_content(
        "<p id=p style='position:absolute;left:0;top:0;margin:0;font:24px monospace'>"
        "alpha bravo charlie</p><script>window.dbl = 0;"
        "document.addEventListener('dblclick', () => window.dbl++);</script>"
    )
    box = await page.locator("#p").bounding_box()
    at = _at(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    for _ in range(2):
        for kind in ("down", "up"):
            assert await dispatch_input("merli", {"t": kind, **at, "count": 1}) is None
    assert await page.evaluate("String(getSelection())") == ""
    assert await page.evaluate("window.dbl") == 0

    for count in (1, 2):
        for kind in ("down", "up"):
            assert await dispatch_input("merli", {"t": kind, **at, "count": count}) is None
    assert await page.evaluate("String(getSelection()).trim()") == "bravo"
    assert await page.evaluate("window.dbl") == 1

    # A triple click takes the paragraph; nonsense counts are a single click.
    for kind in ("down", "up"):
        assert await dispatch_input("merli", {"t": kind, **at, "count": 3}) is None
    assert await page.evaluate("String(getSelection()).trim()") == "alpha bravo charlie"
    for bad in ("x", None, -4):
        assert await dispatch_input("merli", {"t": "down", **at, "count": bad}) is None
        assert await dispatch_input("merli", {"t": "up", **at, "count": bad}) is None


_COPY_PAGE = """<!doctype html><style>body{margin:0}
button{position:absolute;left:100px;width:200px;height:40px}</style>
<input id=u value="selected text">
<button id=api style="top:100px">copy</button>
<button id=exec style="top:200px">copy</button>
<button id=set style="top:300px">copy</button>
<script>
window.done = [];
api.onclick = () => navigator.clipboard.writeText("SECRET-123")
  .then(() => done.push("api ok"), () => done.push("api failed"));
exec.onclick = () => {
  const t = document.createElement("textarea");
  t.value = "from execCommand"; document.body.appendChild(t); t.select();
  done.push("exec " + document.execCommand("copy")); t.remove();
};
set.onclick = () => {
  const on = (e) => { e.clipboardData.setData("text/plain", "from setData"); e.preventDefault(); };
  document.addEventListener("copy", on, { once: true });
  document.execCommand("copy");
};
</script>"""


@pytest.fixture
async def copy_page(monkeypatch):
    from playwright.async_api import async_playwright

    from olano.browser import sessions
    from olano.browser.live import arm_page_copies

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": _W, "height": _H})
        copied: list[str] = []
        sess = SimpleNamespace(
            page=None,
            controller="user",
            viewers=1,
            touch=lambda: None,
            dialog=None,
            last_input_at=0.0,
            clipboard_listeners={copied.append},
        )
        await arm_page_copies(context, sess)
        # navigator.clipboard exists only on a secure page, never on about:blank.
        await context.route(
            "https://copy.test/**",
            lambda route: route.fulfill(body=_COPY_PAGE, content_type="text/html"),
        )
        sess.page = await context.new_page()
        await sess.page.goto("https://copy.test/")
        sess.copied = copied
        monkeypatch.setitem(sessions._sessions, "merli", sess)
        try:
            yield sess
        finally:
            await browser.close()


async def _click(sel_top: float) -> None:
    from olano.browser.live import dispatch_input

    for kind in ("down", "up"):
        assert await dispatch_input("merli", {"t": kind, **_at(200, sel_top + 20)}) is None


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    import asyncio

    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        await asyncio.sleep(0.05)


async def test_a_page_copy_button_reaches_the_person_in_control(copy_page):
    """A copy button writes to the remote browser's own clipboard, which no
    one can see; the person who pressed it gets the text instead."""
    await _click(100)
    await _wait_for(lambda: copy_page.copied)
    assert copy_page.copied == ["SECRET-123"]
    # The page hears that it worked, whatever the remote clipboard said.
    await _wait_for(lambda: False, timeout=0.6)
    assert await copy_page.page.evaluate("window.done") == ["api ok"]

    await _click(200)
    await _click(300)
    await _wait_for(lambda: len(copy_page.copied) >= 3)
    assert copy_page.copied[1:] == ["from execCommand", "from setData"]
    assert "exec true" in await copy_page.page.evaluate("window.done")


async def test_the_copy_hand_off_puts_nothing_on_the_page(copy_page):
    """The hand-off used to be a Playwright binding, a global function on
    every page, and a page carrying one is what brought Cloudflare's check
    on (live.py, _PAGE_COPY_MARKER). Nothing of ours is on the page now."""
    names = await copy_page.page.evaluate(
        "Object.getOwnPropertyNames(window).filter(n => /^__(olano|playwright|pw)/i.test(n))"
    )
    assert names == []


async def test_a_page_copy_needs_the_person_in_control_and_their_gesture(copy_page):
    import asyncio

    page = copy_page.page
    # The agent pressing the button: nothing reaches a person's clipboard.
    copy_page.controller = "agent"
    await page.click("#api")
    # A page writing on its own, no click at all.
    copy_page.controller = "user"
    await page.evaluate("navigator.clipboard.writeText('unasked').catch(() => {})")
    # A page writing the hand-off message itself, with no recent input from the person.
    await page.evaluate("console.debug('__olano:page-copy:forged')")
    await asyncio.sleep(0.5)
    assert copy_page.copied == []

    # Nobody watching: nowhere for it to go.
    copy_page.viewers = 0
    await _click(100)
    await asyncio.sleep(0.5)
    assert copy_page.copied == []
