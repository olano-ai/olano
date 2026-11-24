"""The live view's browser chrome, on a real Chromium (olano/browser/live.py).

Tabs, Stop, the loading and history state, and the three things the browser
draws itself that never reach the screencast: a page's dialogs, its file
requests and a ``<select>``'s option list. Each is answered from the dashboard
by the person in control. These drive the same functions the live-view socket
calls against real pages. Skipped when playwright or its Chromium build isn't
installed.
"""

from __future__ import annotations

import asyncio
import base64
from functools import lru_cache

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
_PAGE = """<!doctype html><title>Form</title><style>body{margin:0}</style>
<select id=s style="position:absolute;left:100px;top:100px;width:200px;height:30px"
  onchange="window.changed=(window.changed||0)+1">
  <option>one</option><optgroup label=More><option>two</option><option disabled>three</option></optgroup>
</select>
<input id=f type=file accept=".pdf,image/*" style="position:absolute;left:100px;top:300px">
<button id=ask style="position:absolute;left:100px;top:400px;width:200px;height:40px"
  onclick="window.answer = confirm('Delete it?')">Ask</button>
<button id=name style="position:absolute;left:400px;top:400px;width:200px;height:40px"
  onclick="window.answer = prompt('Your name?', 'Ann')">Name</button>
<label for=d style="position:absolute;left:400px;top:70px">Travel date</label>
<input id=d type=date min=2026-01-01 max=2026-12-31 style="position:absolute;left:400px;top:100px;width:200px;height:30px"
  onchange="window.dated=(window.dated||0)+1" oninput="window.datedInput=(window.datedInput||0)+1">
<input id=c type=color value="#000000" style="position:absolute;left:700px;top:100px;width:60px;height:30px"
  onchange="window.coloured=(window.coloured||0)+1">"""


@pytest.fixture
async def live(monkeypatch):
    from playwright.async_api import async_playwright

    from olano.browser import live as live_mod
    from olano.browser import sessions

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": _W, "height": _H})
        page = await context.new_page()
        sess = sessions.Session(agent="merli", kind="headless", context=context, page=page)
        sess.controller = "user"
        sess.viewers = 1
        live_mod.arm_page_for_people(sess, page)

        def _on_page(new) -> None:
            sess.page = new
            live_mod.arm_page_for_people(sess, new)

        context.on("page", _on_page)
        monkeypatch.setitem(sessions._sessions, "merli", sess)
        await page.set_content(_PAGE)
        try:
            yield sess
        finally:
            await browser.close()


def _at(x: float, y: float) -> dict[str, float]:
    return {"x": x / _W, "y": y / _H}


async def _click(x: float, y: float) -> None:
    from olano.browser.live import dispatch_input

    for kind in ("down", "up"):
        assert await dispatch_input("merli", {"t": kind, **_at(x, y)}) is None


async def _until(predicate, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never held")


async def test_a_clicked_select_is_offered_and_its_choice_lands(live):
    from olano.browser.live import dispatch_input, select_under

    await _click(200, 115)
    picker = await select_under("merli", _at(200, 115))
    assert picker is not None
    assert [o["label"] for o in picker["options"]] == ["one", "two", "three"]
    assert [o["group"] for o in picker["options"]] == ["", "More", "More"]
    assert picker["options"][2]["disabled"] is True
    assert picker["selected"] == 0

    assert await dispatch_input("merli", {"t": "select", "index": 1}) is None
    assert await live.page.evaluate("document.getElementById('s').value") == "two"
    assert await live.page.evaluate("window.changed") == 1
    # A disabled option is refused, as the page's own list would.
    assert await dispatch_input("merli", {"t": "select", "index": 2}) is not None


async def test_a_clicked_date_field_is_offered_as_a_picker_and_the_value_lands(live):
    """Chromium draws a date field's calendar in a popup the screencast never
    shows, so the viewer draws its own and sends the value back; the page
    hears input and change as it would from the calendar, a value the
    browser refuses (out of range) is reported, and a click elsewhere offers
    nothing."""
    from olano.browser.live import dispatch_input, picker_under, select_under

    await _click(500, 115)
    assert await select_under("merli", _at(500, 115)) is None
    picker = await picker_under("merli", _at(500, 115))
    assert picker == {
        "kind": "date",
        "value": "",
        "min": "2026-01-01",
        "max": "2026-12-31",
        "step": "",
        "label": "Travel date",
    }
    assert await dispatch_input("merli", {"t": "setvalue", "value": "2026-03-04"}) is None
    assert await live.page.evaluate("document.getElementById('d').value") == "2026-03-04"
    assert await live.page.evaluate("[window.dated, window.datedInput]") == [1, 1]
    # The browser refuses what its own calendar could not produce.
    assert await dispatch_input("merli", {"t": "setvalue", "value": "not a date"}) is not None
    assert await live.page.evaluate("document.getElementById('d').value") == "2026-03-04"
    # The same value again is taken quietly, with no second change event.
    assert await dispatch_input("merli", {"t": "setvalue", "value": "2026-03-04"}) is None
    assert await live.page.evaluate("window.dated") == 1

    await _click(730, 115)
    colour = await picker_under("merli", _at(730, 115))
    assert colour is not None and colour["kind"] == "color" and colour["value"] == "#000000"
    assert await dispatch_input("merli", {"t": "setvalue", "value": "#ff6a3d"}) is None
    assert await live.page.evaluate("document.getElementById('c').value") == "#ff6a3d"
    assert await live.page.evaluate("window.coloured") == 1

    await _click(700, 600)
    assert await picker_under("merli", _at(700, 600)) is None


async def test_a_pages_request_for_a_permission_is_explained_to_the_viewers(live, monkeypatch):
    """A page that asks for the person's location (or a camera, or leave to
    notify) is refused by the browser with nothing to see; the viewers get
    a notice saying what was asked, once per kind for a while."""
    from olano.browser import live as live_mod

    monkeypatch.setattr(live_mod, "PERMISSION_NOTE_EVERY_S", 0.5)
    await live_mod.arm_page_permissions(live.context, live)
    notes: list[str] = []
    live.notice_listeners.add(notes.append)
    # Armed before the page exists, as a session arms its context before its
    # first page (sessions.open): the fixture's page predates the script.
    live.page = await live.context.new_page()
    await live.page.set_content(_PAGE)
    ask = "new Promise(r => navigator.geolocation.getCurrentPosition(() => r('ok'), e => r('refused:' + e.code)))"
    await live.page.evaluate(ask)
    await _until(lambda: len(notes) == 1)
    assert "location" in notes[0]
    # Reported over the console: no function of ours on the page.
    names = await live.page.evaluate(
        "Object.getOwnPropertyNames(window).filter(n => /^__(olano|playwright|pw)/i.test(n))"
    )
    assert names == []
    # Asked again at once: not repeated. After the quiet spell: reported again.
    await live.page.evaluate(ask)
    await asyncio.sleep(0.2)
    assert len(notes) == 1
    await asyncio.sleep(0.5)
    await live.page.evaluate(ask)
    await _until(lambda: len(notes) == 2)
    # A different kind is its own notice.
    await live.page.evaluate("Notification.requestPermission().catch(() => {})")
    await _until(lambda: len(notes) == 3)
    assert "notifications" in notes[2]
    # Nobody watching: nothing is reported.
    await asyncio.sleep(0.6)
    live.viewers = 0
    await live.page.evaluate(ask)
    await asyncio.sleep(0.3)
    assert len(notes) == 3


async def test_a_click_elsewhere_offers_no_list(live):
    from olano.browser.live import select_under

    await _click(200, 115)
    assert await select_under("merli", _at(700, 600)) is None


async def test_a_confirm_waits_for_the_person_and_takes_their_answer(live):
    from olano.browser.live import answer_dialog, dispatch_input

    await live.page.evaluate("setTimeout(() => document.getElementById('ask').click(), 0)")
    await _until(lambda: live.dialog is not None)
    assert live.dialog.public()["kind"] == "confirm"
    assert live.dialog.public()["message"] == "Delete it?"
    # The page takes no other input while it waits.
    assert "dialog" in (await dispatch_input("merli", {"t": "move", **_at(10, 10)}) or "")
    assert await answer_dialog("merli", {"id": live.dialog.id, "accept": True}) is None
    assert live.dialog is None
    assert await live.page.evaluate("window.answer") is True


async def test_a_prompt_gets_the_persons_text(live):
    from olano.browser.live import answer_dialog

    await live.page.evaluate("setTimeout(() => document.getElementById('name').click(), 0)")
    await _until(lambda: live.dialog is not None)
    assert live.dialog.public()["default"] == "Ann"
    await answer_dialog("merli", {"id": live.dialog.id, "accept": True, "text": "Bea"})
    assert await live.page.evaluate("window.answer") == "Bea"


async def test_with_nobody_in_control_a_dialog_is_dismissed(live):
    live.controller = "agent"
    await live.page.evaluate("setTimeout(() => document.getElementById('ask').click(), 0)")
    await _until(lambda: True, 0.1)
    await live.page.wait_for_function("window.answer === false")
    assert live.dialog is None


async def test_handing_back_answers_a_waiting_dialog(live):
    from olano.browser import sessions

    await live.page.evaluate("setTimeout(() => document.getElementById('ask').click(), 0)")
    await _until(lambda: live.dialog is not None)
    await sessions.set_controller("merli", "agent")
    assert live.dialog is None
    await live.page.wait_for_function("window.answer === false")


async def test_a_file_request_takes_the_persons_files(live):
    from olano.browser.live import answer_files

    await _click(140, 310)
    await _until(lambda: live.chooser is not None)
    assert live.chooser.public() == {
        "id": live.chooser.id,
        "multiple": False,
        "accept": ".pdf,image/*",
    }
    data = base64.b64encode(b"%PDF-1.4 hello").decode()
    err = await answer_files(
        "merli",
        {
            "id": live.chooser.id,
            "files": [{"name": "a.pdf", "mime": "application/pdf", "data": data}],
        },
    )
    assert err is None
    got = await live.page.evaluate(
        "() => { const f = document.getElementById('f').files[0]; return f && [f.name, f.size]; }"
    )
    assert got == ["a.pdf", 14]


async def test_files_over_the_limit_are_refused(live, monkeypatch):
    from olano.browser import live as live_mod

    monkeypatch.setattr(live_mod, "MAX_UPLOAD_BYTES", 4)
    await _click(140, 310)
    await _until(lambda: live.chooser is not None)
    data = base64.b64encode(b"too big").decode()
    err = await live_mod.answer_files(
        "merli", {"id": live.chooser.id, "files": [{"name": "a", "data": data}]}
    )
    assert err and "MB" in err


async def test_tabs_open_switch_and_close(live):
    from olano.browser.live import dispatch_input, tabs

    first = live.page
    assert await dispatch_input("merli", {"t": "newtab"}) is None
    second = live.page
    assert second is not first
    listed = await tabs(live)
    assert [t["active"] for t in listed] == [False, True]
    assert listed[0]["title"] == "Form"

    assert await dispatch_input("merli", {"t": "tab", "id": listed[0]["id"]}) is None
    assert live.page is first
    assert await dispatch_input("merli", {"t": "closetab", "id": listed[1]["id"]}) is None
    await _until(lambda: second.is_closed())
    assert [t["id"] for t in await tabs(live)] == [listed[0]["id"]]
    # The last tab is never closed: it becomes an empty page instead.
    assert await dispatch_input("merli", {"t": "closetab", "id": listed[0]["id"]}) is None
    assert not first.is_closed()
    assert first.url == "about:blank"


async def test_the_screencast_reports_loading_and_history(live):
    from olano.browser.live import Screencast, dispatch_input

    changes: list[int] = []
    cast = Screencast("merli", lambda *_: None, on_change=lambda: changes.append(1))
    await cast.start()
    try:
        await live.page.route("https://slow.test/**", _slow_page)
        assert (
            await dispatch_input("merli", {"t": "navigate", "url": "https://slow.test/a"}) is None
        )
        assert await cast.history() == (True, False)
        assert changes, "navigation should wake the state frame"
        nav = asyncio.ensure_future(live.page.goto("https://slow.test/b?wait=1"))
        await _until(lambda: cast.loading)
        assert await dispatch_input("merli", {"t": "stop"}) is None
        await _until(lambda: not cast.loading)
        nav.cancel()
    finally:
        await cast.stop()


async def _slow_page(route) -> None:
    if "wait=1" in route.request.url:
        await asyncio.sleep(3)
    try:
        await route.fulfill(body="<title>t</title><p>x</p>", content_type="text/html")
    except Exception:
        pass


async def test_an_unattended_leave_dialog_is_accepted_so_navigation_goes_on(live):
    """Playwright accepts a leave-this-page dialog nobody listens for; with the
    live view's listener always on, it must still be accepted when nobody is
    in control, or every goto the agent makes from such a page hangs."""
    live.controller = "agent"
    await live.page.set_content(
        "<script>addEventListener('beforeunload', e => { e.preventDefault(); e.returnValue = ''; })</script>"
        "<input id=i style='position:absolute;left:0;top:0;width:200px;height:40px'>"
    )
    await live.page.click("#i")  # a user gesture, without which no leave dialog shows
    await live.page.keyboard.type("draft")
    await live.page.route(
        "https://next.test/", lambda r: r.fulfill(body="<p>next</p>", content_type="text/html")
    )
    await asyncio.wait_for(live.page.goto("https://next.test/"), 10)
    assert live.page.url == "https://next.test/"


async def test_the_tab_in_front_is_always_listed(live, monkeypatch):
    from olano.browser import live as live_mod

    monkeypatch.setattr(live_mod, "MAX_TABS_LISTED", 2)
    for _ in range(2):
        assert await live_mod.dispatch_input("merli", {"t": "newtab"}) is None
    # Three pages, room for two: the newest (in front, where a new tab lands)
    # must be among them.
    listed = await live_mod.tabs(live)
    assert len(listed) == 2
    assert listed[-1]["active"], "the page in front must be in the strip"


_BUSY = """<!doctype html><title>Feed</title><style>body{margin:0;background:#000}</style>
<canvas id=c width=1280 height=800></canvas>
<button id=b style="position:absolute;left:100px;top:100px;width:300px;height:40px;z-index:2"
 onclick="document.title='hit'+((window.hits=(window.hits||0)+1))">Button</button>
<script>
const ctx = document.getElementById('c').getContext('2d');
const img = ctx.createImageData(1280, 800);
function draw(){
  const d = img.data;
  for (let i = 0; i < d.length; i += 4) { const v = (Math.random()*255)|0; d[i]=v; d[i+1]=(v*3)&255; d[i+2]=(v*7)&255; d[i+3]=255; }
  ctx.putImageData(img, 0, 0);
  requestAnimationFrame(draw);
}
draw();
</script>"""


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Width and height from a JPEG's start-of-frame marker (no decoder needed)."""
    i = 2
    while i < len(data):
        assert data[i] == 0xFF, "not a JPEG marker"
        marker = data[i + 1]
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return width, height
        i += 2 + length
    raise AssertionError("no start of frame")


async def test_frames_are_no_larger_than_the_viewer_and_map_taps_on_the_frame(live):
    """A viewer says how big its picture is; the frames are capped there, a
    stepped-down viewer gets smaller ones still, and a tap's fractions are
    mapped on the page area the frame shows, whatever its pixel size."""
    from olano.browser.live import MIN_FRAME_WIDTH, QUALITY_LADDER, Screencast, dispatch_input

    await live.page.set_content(_BUSY)
    frames: list[bytes] = []
    cast = Screencast("merli", lambda data, _meta: frames.append(data))
    await cast.start()
    try:
        await _until(lambda: len(frames) >= 1)
        assert _jpeg_size(frames[-1]) == (_W, _H)
        assert cast.size() == {"width": _W, "height": _H}

        await cast.set_viewer(1000, 625)
        n = len(frames)
        await _until(lambda: len(frames) > n + 1)
        assert _jpeg_size(frames[-1]) == (1000, 625)
        # The frame still shows the whole page: fractions span the viewport.
        assert cast.size() == {"width": _W, "height": _H}
        for kind, extra in (("move", {}), ("down", {"button": "left"}), ("up", {"button": "left"})):
            assert (
                await dispatch_input("merli", {"t": kind, **_at(250, 120), **extra}, cast.size())
                is None
            )
        assert await live.page.title() == "hit1"

        # Stepped down the ladder: smaller than the viewer asked for.
        await cast.set_level(len(QUALITY_LADDER) - 1)
        n = len(frames)
        await _until(lambda: len(frames) > n + 1)
        width, _ = _jpeg_size(frames[-1])
        assert MIN_FRAME_WIDTH <= width < 1000
        # And back to the viewer's size.
        await cast.set_level(0)
        n = len(frames)
        await _until(lambda: len(frames) > n + 1)
        assert _jpeg_size(frames[-1]) == (1000, 625)
        # Never smaller than the floor, whatever the viewer says.
        await cast.set_viewer(10, 10)
        assert cast.params()[0] == MIN_FRAME_WIDTH
    finally:
        await cast.stop()


async def test_a_poke_brings_a_fresh_frame_of_a_still_page(live):
    """A page that stopped changing sends no frames; a poke makes Chromium
    send one, so a person who acted is never left looking at the past."""
    from olano.browser.live import Screencast

    await live.page.set_content("<title>still</title><p style='font-size:40px'>still</p>")
    frames: list[bytes] = []
    cast = Screencast("merli", lambda data, _meta: frames.append(data))
    await cast.start()
    try:
        await _until(lambda: len(frames) >= 1)
        await asyncio.sleep(0.5)
        n = len(frames)
        await asyncio.sleep(0.5)
        assert len(frames) == n, "a still page sends nothing"
        before = cast.last_frame_at
        await cast.poke()
        await _until(lambda: len(frames) > n)
        assert cast.last_frame_at > before
    finally:
        await cast.stop()


async def test_a_page_that_never_stops_changing_is_paced_and_still_takes_taps(live):
    """A video feed redraws every frame; the screencast sends at most MAX_FPS
    of them (the next frame waits for the acknowledgement, which is held),
    and a tap lands while it streams."""
    from olano.browser import live as live_mod
    from olano.browser.live import Screencast, dispatch_input

    await live.page.set_content(_BUSY)
    stamps: list[float] = []
    cast = Screencast("merli", lambda *_: stamps.append(asyncio.get_running_loop().time()))
    await cast.start()
    try:
        await asyncio.sleep(2.0)
        assert len(stamps) >= 5, "the feed should stream"
        span = stamps[-1] - stamps[0]
        assert len(stamps) - 1 <= live_mod.MAX_FPS * span * 1.25 + 2, (len(stamps), span)
        for kind, extra in (("move", {}), ("down", {"button": "left"}), ("up", {"button": "left"})):
            assert await dispatch_input("merli", {"t": kind, **_at(250, 120), **extra}) is None
        assert await live.page.title() == "hit1"
    finally:
        await cast.stop()
