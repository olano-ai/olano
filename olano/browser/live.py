"""The live view: an agent's browser streamed to the dashboard, and taken over.

A viewer (the agent's Browser tab) holds one WebSocket. This module is the
browser-loop half of it:

- **frames out**: ``Page.startScreencast`` on a DevTools session attached to
  the agent's current page. Chromium pushes JPEG frames only when something
  changed, each acknowledged before the next is sent, so an idle page costs
  nothing and a slow viewer slows the stream instead of queueing it. When the
  agent's page changes (a popup, a new tab) the screencast follows it.
- **input in**: mouse, wheel and keyboard events from the viewer, replayed on
  the page, but **only while a person holds control** (``Session.controller ==
  "user"``). Watching is passive; nothing a viewer sends acts on the page until
  they press *Take control*, and while they hold it the agent's acting tools
  refuse (``tools/browser.USER_HAS_CONTROL``). This is how a person signs in,
  types a one-time code or solves a CAPTCHA without the credential ever
  passing through the model: the keystrokes go from their keyboard to the page.

Coordinates arrive normalised to the frame (0..1) and are mapped onto the
page's viewport here, so the dashboard never needs to know the page size.

The clipboard crosses the same way, under the same rule: a paste arrives as
text (``"text"``, inserted at the caret), and copy or cut (:func:`copy_selection`)
reads the page's selected text back so the dashboard can put it on the person's
own clipboard. The remote browser has a clipboard of its own, which nobody can
see, so replaying the shortcut keys alone would copy into nowhere. A page's own
copy button (``navigator.clipboard.writeText``, ``execCommand("copy")``) writes
there too, so :func:`arm_page_copies` hands what it wrote to the viewer as well.

A double or triple click reaches the page as one: the dashboard counts the
clicks and each press carries its ``count``, which is what makes Chromium fire
``dblclick`` and select a word or a paragraph. Two presses of count 1 are two
single clicks however close together they land.

**What the browser draws itself never reaches the screencast**, which captures
the page and nothing else. Three of those would leave a person stuck, so each
is brought to the dashboard instead, while a person holds control and watches:

- a page's ``alert`` / ``confirm`` / ``prompt`` / leave-this-page dialog
  (:func:`arm_page_for_people`): held as :class:`PendingDialog` and answered
  from the dashboard. A page with a dialog open takes no other input, so
  pointer and key events are refused until it is answered. The click or key
  that opened it does not finish until then either, so the answer travels
  outside the input queue (``routes/browser.py``). With nobody in control it
  is answered as Playwright answers one nobody listens for: a leave-this-page
  dialog accepted, every other kind dismissed;
- a file request (``<input type=file>``): held as :class:`PendingChooser`;
  the person picks files on their own device and the bytes go straight to the
  page (:func:`answer_files`). Watching the request also keeps Chromium from
  opening its own file dialog, which nobody could see;
- the option list of a ``<select>``: after a click lands on one,
  :func:`select_under` reads its options for the dashboard to show, and
  ``{"t": "select"}`` picks one (:data:`_SET_SELECT_JS`);
- the picker of a date, time, month, week or colour field: after a click
  lands on one, :func:`picker_under` describes it, the viewer draws its own
  picker, and ``{"t": "setvalue"}`` sets the choice (:data:`_SET_PICKER_JS`).

A fourth has no answer to give, only an explanation: a page asking for the
person's location, a camera, a microphone, the screen or leave to notify. The
agent's browser has none of these and refuses at once; :func:`arm_page_permissions`
tells the viewers what was asked and why it was declined, so a site that then
behaves oddly is not a mystery.

Tabs (``tab`` / ``newtab`` / ``closetab``), ``stop``, and the state frames'
``loading`` / ``can_back`` / ``can_forward`` / ``tabs`` (:func:`live_state`)
make the rest of a browser's chrome.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import itertools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from olano.browser import sessions
from olano.browser.guard import url_refusal

logger = logging.getLogger(__name__)

FRAME_QUALITY = 60
MAX_WIDTH = 1280
MAX_HEIGHT = 800
# Frames a second a viewer gets at most. Chromium sends the next frame only
# once the previous one is acknowledged, so holding the acknowledgement is what
# paces it, and the frames between two acknowledgements are never produced at
# all. Unpaced, a page that never stops changing (a video feed, which is what
# a sign-in lands on) was streamed at the compositor's rate: 35 frames a second
# of up to half a megabyte each through the container's exec pipe and on to a
# phone, whose web view then spent every moment unpacking pictures and had none
# left for the person's taps. The live view went dead the moment the sign-in
# went through, on exactly the page the person had taken control to reach.
MAX_FPS = 12
# What a viewer that cannot keep up is stepped down to, best first: the frame's
# largest size and its JPEG quality. Twelve frames a second was still too many
# for a phone on a page that keeps redrawing (a sign-in's verification step,
# with its spinners and its scripts): every frame crosses the native bridge as
# a few hundred kilobytes of text the web view evaluates on the thread the taps
# need. So a viewer paces the stream itself (``routes/browser.py``: the next
# frame waits for the viewer to say the last one was drawn) and a viewer slow
# to draw is sent smaller, rougher frames until it keeps up, then sharper ones
# again. The smallest level keeps a page's text legible when zoomed.
QUALITY_LADDER: tuple[tuple[int, int, int], ...] = (
    (MAX_WIDTH, MAX_HEIGHT, FRAME_QUALITY),
    (1024, 640, 50),
    (800, 500, 42),
    (640, 400, 35),
)
# No frame is ever smaller than this, whatever the viewer reports.
MIN_FRAME_WIDTH = 320
MIN_FRAME_HEIGHT = 200

_BUTTONS = {"left", "middle", "right"}
MAX_CLIPBOARD_CHARS = 200_000
MAX_PASTE_CHARS = 20_000
# Files a person sends to a page in one go. They cross the live view's
# WebSocket as base64, under the server's 16 MiB message limit.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_FILES = 20
MAX_TABS_LISTED = 30
MAX_SELECT_OPTIONS = 500
# A dialog or file request nobody answers is answered for them after this
# long, so a page is never held open forever by a person who walked away.
PENDING_TTL_S = 600.0
_DIALOG_REFUSAL = "The page is showing a dialog. Answer it first."
# A page's copy reaches the person's clipboard only this soon after they
# pressed or typed something on it: the same window in which Chromium itself
# lets a page act on a person's gesture, so a page cannot fill their clipboard
# whenever it likes.
COPY_GESTURE_S = 5.0
# Inputs that count as that gesture (a move or a scroll does not).
_GESTURE_KINDS = frozenset({"down", "up", "keydown", "keyup", "text"})

_ids = itertools.count(1)


@dataclass
class PendingDialog:
    """A page's JavaScript dialog waiting for the person in control."""

    id: int
    dialog: Any
    page: Any
    kind: str  # "alert" | "confirm" | "prompt" | "beforeunload"
    message: str
    default: str
    at: float

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "message": self.message, "default": self.default}


@dataclass
class PendingChooser:
    """A page's request for files, waiting for the person in control."""

    id: int
    chooser: Any
    page: Any
    multiple: bool
    accept: str
    at: float

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "multiple": self.multiple, "accept": self.accept}


# The selected text: inside a focused text field (where the page selection is
# empty) or on the page. A password field's contents never leave the page.
_SELECTION_JS = """() => {
  const el = document.activeElement;
  if (el && (el.tagName === "TEXTAREA" || el.tagName === "INPUT")) {
    if (el.type === "password") return "";
    try {
      if (el.selectionStart != null && el.selectionEnd != null) {
        return String(el.value).slice(el.selectionStart, el.selectionEnd);
      }
    } catch (e) {}
  }
  const sel = window.getSelection && window.getSelection();
  return sel ? String(sel) : "";
}"""

# How the page scripts below reach the runtime: a console message starting
# with a marker, written through ``console.debug`` as it was before any site
# script ran, and read off the context's ``console`` event. They used a
# Playwright binding (``expose_binding``), a function Playwright adds to every
# page, and a page carrying one is what made Cloudflare challenge the agent's
# next page load (measured on propertyguru.com.sg: any binding, even one
# nothing called, 8 of 8; the same scripts without it, 0 of 6). A console
# message is invisible to the page that sent it, and a site's own scripts can
# write one as easily as they could call the binding, so the handlers trust
# it no more than they trusted the binding.
_PAGE_COPY_MARKER = "__olano:page-copy:"

# A page's own copy (a "copy" button), reported to :func:`_on_page_copy` as
# well as written where the page asked. Only a copy made while the page holds
# a person's gesture is reported (``navigator.userActivation``), and the page
# is told it succeeded even when the remote browser's clipboard, which nobody
# can see, refused or never answered: the person's clipboard is the one that
# counts. A copy the page makes on its own goes where it always went.
PAGE_COPY_JS = (
    """(() => {
  try {
    const MARK = "__MARKER__";
    const say = console.debug;
    const define = (obj, name, value) =>
      Object.defineProperty(obj, name, { configurable: true, writable: true, value });
    const report = (text) => {
      try {
        if (typeof text !== "string" || !text) return false;
        const ua = navigator.userActivation;
        if (ua && !ua.isActive) return false;
        if (typeof say !== "function") return false;
        say.call(console, MARK + text);
        return true;
      } catch (e) {
        return false;
      }
    };
    const settled = (real) =>
      Promise.race([
        Promise.resolve(real).catch(() => {}),
        new Promise((r) => setTimeout(r, 400)),
      ]).then(() => undefined);
    const CB = typeof Clipboard !== "undefined" && Clipboard.prototype;
    if (CB && typeof CB.writeText === "function") {
      const writeText = CB.writeText;
      define(CB, "writeText", function (data) {
        const real = writeText.call(this, data);
        return report(String(data)) ? settled(real) : real;
      });
    }
    if (CB && typeof CB.write === "function") {
      const write = CB.write;
      define(CB, "write", function (items) {
        const real = write.call(this, items);
        const ua = navigator.userActivation;
        const item = items && items[0];
        if ((ua && !ua.isActive) || !item || !Array.from(item.types || []).includes("text/plain")) {
          return real;
        }
        return Promise.resolve(item.getType("text/plain"))
          .then((blob) => blob.text())
          .then((text) => (report(text) ? settled(real) : real));
      });
    }
    const selected = (doc) => {
      const el = doc.activeElement;
      if (el && (el.tagName === "TEXTAREA" || el.tagName === "INPUT")) {
        if (el.type === "password") return "";
        try {
          if (el.selectionStart != null && el.selectionEnd != null) {
            return String(el.value).slice(el.selectionStart, el.selectionEnd);
          }
        } catch (e) {}
      }
      const sel = doc.getSelection && doc.getSelection();
      return sel ? String(sel) : "";
    };
    let copying = null;
    const DT = typeof DataTransfer !== "undefined" && DataTransfer.prototype;
    if (DT && typeof DT.setData === "function") {
      const setData = DT.setData;
      define(DT, "setData", function (format, data) {
        if (copying && /^text(\\/plain)?$/i.test(String(format))) copying.text = String(data);
        return setData.call(this, format, data);
      });
    }
    const DOC = typeof Document !== "undefined" && Document.prototype;
    if (DOC && typeof DOC.execCommand === "function") {
      const exec = DOC.execCommand;
      define(DOC, "execCommand", function (command, ...rest) {
        const c = String(command).toLowerCase();
        if (c !== "copy" && c !== "cut") return exec.call(this, command, ...rest);
        // Read first: a cut removes what it copies.
        const before = selected(this);
        const outer = copying;
        copying = { text: null };
        let ok = false;
        let reported = false;
        try {
          ok = exec.call(this, command, ...rest);
        } finally {
          const text = copying.text != null ? copying.text : before;
          copying = outer;
          reported = report(text);
        }
        return ok || reported;
      });
    }
  } catch (e) {}
})();"""
).replace("__MARKER__", _PAGE_COPY_MARKER)


async def arm_page_copies(context: Any, sess: Any) -> None:
    """Hand a page's own copies to the person in control (:data:`PAGE_COPY_JS`).

    For the live browser only: it is the one a person takes over. Never
    raises: a context that cannot take the script copies as before, into the
    remote browser's own clipboard.
    """

    def _on_copy(message: Any, text: str) -> None:
        _on_page_copy(sess, message, text)

    try:
        _listen_console(context, _PAGE_COPY_MARKER, _on_copy)
        await context.add_init_script(PAGE_COPY_JS)
    except Exception as exc:
        logger.debug("live view: page copies not handed over: %s", exc)


def _listen_console(context: Any, marker: str, handler: Callable[[Any, str], None]) -> None:
    """Call ``handler(message, rest)`` for each ``console.debug`` message of
    *context* that starts with *marker* (see :data:`_PAGE_COPY_MARKER`)."""

    def _on_console(message: Any) -> None:
        try:
            if message.type != "debug":
                return
            text = str(message.text or "")
        except Exception:
            return
        if text.startswith(marker):
            handler(message, text[len(marker) :])

    context.on("console", _on_console)


def _on_page_copy(sess: Any, source: Any, text: Any) -> None:
    """A page copied *text*: give it to the viewers, if a person in control
    just acted on the page in front. Any page can write the message, so the
    page's own activation check is not trusted on its own."""
    if not isinstance(text, str) or not text:
        return
    if not _for_a_person(sess) or not sess.clipboard_listeners:
        return
    if time.time() - float(sess.last_input_at or 0.0) > COPY_GESTURE_S:
        return
    page = source.get("page") if isinstance(source, dict) else getattr(source, "page", None)
    if page is not sess.page:
        return
    text = text[:MAX_CLIPBOARD_CHARS]
    for listener in list(sess.clipboard_listeners):
        try:
            listener(text)
        except Exception:  # pragma: no cover - a viewer must never break the page
            logger.debug("live view: clipboard listener failed", exc_info=True)


class Screencast:
    """One viewer's screencast of one agent's browser. Lives on the browser loop.

    The same DevTools session also reports when the page starts and stops
    loading (``loading``), and calls *on_change* when that or the page's
    address changes, so the viewer's state frame goes out at once.
    """

    def __init__(
        self,
        agent: str,
        on_frame: Callable[[bytes, dict[str, Any]], None],
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.agent = agent
        self.on_frame = on_frame
        self.on_change = on_change or (lambda: None)
        self.loading = False
        self._cdp: Any = None
        self._page: Any = None
        self._main_frame = ""
        self._stopped = False
        self._last_ack = 0.0
        # The viewer's picture in device pixels (``{"t": "view"}``), and how far
        # down QUALITY_LADDER it has been stepped; together they size the frames.
        self._viewer: tuple[int, int] | None = None
        self.level = 0
        self._params: tuple[int, int, int] = QUALITY_LADDER[0]
        # The page area the last frame showed, in CSS pixels (its metadata),
        # which is what a viewer's fractions are mapped on; and when it came.
        self.frame_size: tuple[int, int] | None = None
        self.last_frame_at = 0.0

    async def start(self) -> None:
        sess = sessions.get(self.agent)
        if sess is None:
            raise sessions.BrowserUnavailable("This agent has no open browser.")
        sess.viewers += 1
        await self._attach(sess.page)

    # ── Frame size and quality ──────────────────────────────────────────────

    def params(self) -> tuple[int, int, int]:
        """``(max_width, max_height, quality)`` the screencast runs with."""
        width, height, quality = QUALITY_LADDER[min(max(self.level, 0), len(QUALITY_LADDER) - 1)]
        if self._viewer is not None:
            width = min(width, self._viewer[0])
            height = min(height, self._viewer[1])
        return max(width, MIN_FRAME_WIDTH), max(height, MIN_FRAME_HEIGHT), quality

    async def set_viewer(self, width: Any, height: Any) -> None:
        """The viewer's picture is *width* by *height* device pixels: frames
        larger than that are wasted, so the screencast is capped there."""
        try:
            size = (int(width), int(height))
        except (TypeError, ValueError):
            return
        if size[0] <= 0 or size[1] <= 0:
            return
        self._viewer = size
        await self._apply_params()

    async def set_level(self, level: int) -> None:
        """Step the frames down (a larger *level*) or back up QUALITY_LADDER."""
        self.level = min(max(int(level), 0), len(QUALITY_LADDER) - 1)
        await self._apply_params()

    async def _apply_params(self) -> None:
        wanted = self.params()
        if wanted == self._params:
            return
        self._params = wanted
        await self.restart()

    async def restart(self) -> None:
        """Start the screencast over on the page it is attached to, with the
        current size and quality. Chromium sends a frame on every start, so
        this also refreshes a picture that stopped moving (:meth:`poke`)."""
        cdp = self._cdp
        if cdp is None or self._stopped:
            return
        try:
            await cdp.send("Page.stopScreencast")
        except Exception:
            pass
        await self._start_cast(cdp)

    async def poke(self) -> None:
        """A fresh frame, whatever the page did: a person acted and nothing
        came back. A screencast can go quiet on a renderer swap or an
        occluded window, and the person must never be left looking at a
        picture of the past."""
        await self.restart()

    async def _start_cast(self, cdp: Any) -> None:
        width, height, quality = self._params
        try:
            await cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": quality,
                    "maxWidth": width,
                    "maxHeight": height,
                    "everyNthFrame": 1,
                },
            )
        except Exception:
            logger.debug("live view: screencast did not start", exc_info=True)

    def size(self) -> dict[str, int]:
        """The page area a frame shows, in CSS pixels: what a viewer's
        fractions are mapped on. The last frame's own metadata when there is
        one (it is the truth about what the viewer sees), else the viewport."""
        if self.frame_size is not None:
            return {"width": self.frame_size[0], "height": self.frame_size[1]}
        try:
            size = self._page.viewport_size if self._page is not None else None
        except Exception:
            size = None
        return dict(size) if size else {"width": MAX_WIDTH, "height": MAX_HEIGHT}

    async def _attach(self, page: Any) -> None:
        await self._detach()
        sess = sessions.get(self.agent)
        if sess is None or page is None or page.is_closed():
            return
        self._page = page
        self.frame_size = None
        cdp = await sess.context.new_cdp_session(page)
        self._cdp = cdp

        def _frame(params: dict[str, Any]) -> None:
            session_id = params.get("sessionId")
            meta = params.get("metadata") or {}
            try:
                width = int(meta.get("deviceWidth") or 0)
                height = int(meta.get("deviceHeight") or 0)
                if width > 0 and height > 0:
                    self.frame_size = (width, height)
            except (TypeError, ValueError):
                pass
            self.last_frame_at = time.monotonic()
            try:
                data = base64.b64decode(params.get("data", ""))
                self.on_frame(data, meta)
            except Exception:
                logger.debug("live view: dropping a frame", exc_info=True)
            if session_id is not None and not self._stopped:
                asyncio.ensure_future(self._ack(cdp, session_id))

        cdp.on("Page.screencastFrame", _frame)

        def _loading(started: bool) -> Callable[[dict[str, Any]], None]:
            def _on(params: dict[str, Any]) -> None:
                if params.get("frameId") != self._main_frame or self._stopped:
                    return
                if self.loading != started:
                    self.loading = started
                    self.on_change()

            return _on

        def _navigated(params: dict[str, Any]) -> None:
            frame = params.get("frame") or {}
            if not frame.get("parentId") and not self._stopped:
                self.on_change()

        cdp.on("Page.frameStartedLoading", _loading(True))
        cdp.on("Page.frameStoppedLoading", _loading(False))
        cdp.on("Page.frameNavigated", _navigated)
        self.loading = False
        self._main_frame = ""
        try:
            await cdp.send("Page.enable")
            tree = await cdp.send("Page.getFrameTree")
            self._main_frame = str(
                ((tree or {}).get("frameTree") or {}).get("frame", {}).get("id", "")
            )
            # A page already loading when the view attaches (a tab switched to
            # mid-load, a popup) sent its start event before anyone listened.
            ready = await asyncio.wait_for(
                cdp.send(
                    "Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True}
                ),
                2.0,
            )
            self.loading = ((ready or {}).get("result") or {}).get("value") != "complete"
        except Exception:
            logger.debug("live view: no load events for this page", exc_info=True)
        await self._start_cast(cdp)

    async def _ack(self, cdp: Any, session_id: int) -> None:
        # The frame itself went out at once; only the next one is held back.
        # Chromium keeps two frames in flight, so two acknowledgements can be
        # waiting at once: each takes the next slot when it is scheduled, not
        # when it wakes, or both would wake together and the rate would double.
        now = time.monotonic()
        due = max(now, self._last_ack + 1.0 / MAX_FPS)
        self._last_ack = due
        if due > now:
            await asyncio.sleep(due - now)
        if self._stopped or self._cdp is not cdp:
            return
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass

    async def _detach(self) -> None:
        cdp, self._cdp = self._cdp, None
        if cdp is not None:
            try:
                await cdp.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                await cdp.detach()
            except Exception:
                pass

    async def follow(self) -> None:
        """Re-attach if the agent's current page changed (popup, new tab)."""
        sess = sessions.get(self.agent)
        if sess is None or self._stopped:
            return
        if sess.page is not self._page and sess.page is not None:
            await self._attach(sess.page)

    async def history(self) -> tuple[bool, bool]:
        """Whether the page can go back and forward: ``(can_back, can_forward)``."""
        cdp = self._cdp
        if cdp is None:
            return False, False
        try:
            hist = await asyncio.wait_for(cdp.send("Page.getNavigationHistory"), 2.0)
        except Exception:
            return False, False
        index = int(hist.get("currentIndex", 0))
        count = len(hist.get("entries") or [])
        return index > 0, index < count - 1

    async def stop(self) -> None:
        self._stopped = True
        await self._detach()
        sess = sessions.get(self.agent)
        if sess is not None:
            sess.viewers = max(0, sess.viewers - 1)
            if sess.viewers == 0:
                # Nobody left to answer a dialog or pick a file.
                await clear_pending(sess)


# ── Tabs ────────────────────────────────────────────────────────────────────


def page_id(sess: Any, page: Any) -> int:
    """A small number for *page*, stable for its life, for the tab strip."""
    pid = sess.page_ids.get(page)
    if pid is None:
        sess.next_page_id += 1
        pid = sess.next_page_id
        sess.page_ids[page] = pid
    return int(pid)


def _open_pages(sess: Any) -> list[Any]:
    try:
        return [p for p in sess.context.pages if not p.is_closed()]
    except Exception:
        return []


def _page_by_id(sess: Any, pid: Any) -> Any:
    for page in _open_pages(sess):
        if sess.page_ids.get(page) == pid:
            return page
    return None


async def tabs(sess: Any) -> list[dict[str, Any]]:
    """The open pages, oldest first, with the one in front marked ``active``.

    A title is read with a short timeout (a page busy in a script, or showing
    a dialog, cannot answer) and the last good one is shown meanwhile.
    """
    pages = _open_pages(sess)
    if len(pages) > MAX_TABS_LISTED:
        # The newest, where popups and new tabs land, and always the one in front.
        kept = pages[-MAX_TABS_LISTED:]
        if sess.page in pages and sess.page not in kept:
            kept = [sess.page, *kept[1:]]
        pages = kept

    async def _title(page: Any) -> str:
        pid = page_id(sess, page)
        if sess.dialog is None or sess.dialog.page is not page:
            try:
                sess.titles[pid] = str(await asyncio.wait_for(page.title(), 0.5))[:200]
            except Exception:
                pass
        return str(sess.titles.get(pid, ""))

    # Read together, so a slow page costs one timeout, not one per tab.
    titles = await asyncio.gather(*(_title(p) for p in pages))
    return [
        {"id": page_id(sess, p), "url": str(p.url), "title": t, "active": p is sess.page}
        for p, t in zip(pages, titles, strict=True)
    ]


async def live_state(agent: str, cast: Screencast | None = None) -> dict[str, Any] | None:
    """What a live view shows besides the picture, or ``None`` when the
    session is gone."""
    sess = sessions.get(agent)
    if sess is None:
        return None
    _expire_pending(sess)
    can_back, can_forward = await cast.history() if cast is not None else (False, False)
    return {
        "loading": bool(cast.loading) if cast is not None else False,
        "can_back": can_back,
        "can_forward": can_forward,
        "tabs": await tabs(sess),
        "dialog": sess.dialog.public() if sess.dialog is not None else None,
        "chooser": sess.chooser.public() if sess.chooser is not None else None,
    }


# ── What the browser draws itself: dialogs, file requests ──────────────────


def _for_a_person(sess: Any) -> bool:
    return bool(sess.controller == "user" and sess.viewers > 0)


def _unattended(dialog: Any) -> Any:
    """Answer *dialog* the way Playwright does when nobody listens: accept a
    leave-this-page dialog (or a navigation the agent started would hang on
    it), dismiss every other kind."""
    if str(getattr(dialog, "type", "")) == "beforeunload":
        return dialog.accept()
    return dialog.dismiss()


async def _quietly(coro: Any) -> None:
    try:
        await coro
    except Exception:
        logger.debug("live view: answering the page failed", exc_info=True)


def arm_page_for_people(sess: Any, page: Any) -> None:
    """Route *page*'s dialogs and file requests to the person in control.

    Called by ``sessions`` for every page of an agent's context. With nobody
    in control a dialog is answered as Playwright answers one nobody listens
    for (:func:`_unattended`) and a file request goes unanswered, which is
    what the page saw before, minus Chromium's own file dialog, which nobody
    could see.
    """

    def _on_dialog(dialog: Any) -> None:
        if sess.dialog is not None and sess.dialog.dialog is dialog:
            return  # already held for the person
        if not _for_a_person(sess) or sess.dialog is not None:
            asyncio.ensure_future(_quietly(_unattended(dialog)))
            return
        sess.dialog = PendingDialog(
            id=next(_ids),
            dialog=dialog,
            page=page,
            kind=str(dialog.type or "alert"),
            message=str(dialog.message or "")[:4000],
            default=str(dialog.default_value or "")[:1000],
            at=time.time(),
        )
        sessions._emit(sess.agent, "dialog")

    async def _on_chooser(chooser: Any) -> None:
        if not _for_a_person(sess):
            return
        accept = ""
        try:
            accept = str(await chooser.element.get_attribute("accept") or "")[:300]
        except Exception:
            pass
        sess.chooser = PendingChooser(
            id=next(_ids),
            chooser=chooser,
            page=page,
            multiple=bool(chooser.is_multiple()),
            accept=accept,
            at=time.time(),
        )
        sessions._emit(sess.agent, "chooser")

    def _on_close(_page: Any) -> None:
        if sess.dialog is not None and sess.dialog.page is page:
            sess.dialog = None
        if sess.chooser is not None and sess.chooser.page is page:
            sess.chooser = None

    try:
        page.on("dialog", _on_dialog)
        page.on("filechooser", _on_chooser)
        page.on("close", _on_close)
    except Exception:  # pragma: no cover - a test double without events
        logger.debug("live view: page events unavailable", exc_info=True)


def _expire_pending(sess: Any) -> None:
    now = time.time()
    if sess.dialog is not None and now - sess.dialog.at > PENDING_TTL_S:
        dialog, sess.dialog = sess.dialog, None
        asyncio.ensure_future(_quietly(_unattended(dialog.dialog)))
    if sess.chooser is not None and now - sess.chooser.at > PENDING_TTL_S:
        sess.chooser = None


async def clear_pending(sess: Any) -> None:
    """Answer whatever waits for the person as if nobody were there."""
    dialog, sess.dialog = sess.dialog, None
    sess.chooser = None
    if dialog is not None:
        await _quietly(_unattended(dialog.dialog))


async def answer_dialog(agent: str, event: dict[str, Any]) -> str | None:
    sess = sessions.get(agent)
    if sess is None:
        return "This agent has no open browser."
    if sess.controller != "user":
        return "Take control first: watching does not act on the page."
    pending = sess.dialog
    if pending is None or event.get("id") != pending.id:
        return None  # already answered (another viewer, or it expired)
    sess.dialog = None
    sess.touch()
    if event.get("accept"):
        text = event.get("text")
        if pending.kind == "prompt" and isinstance(text, str):
            await _quietly(pending.dialog.accept(text[:MAX_PASTE_CHARS]))
        else:
            await _quietly(pending.dialog.accept())
    else:
        await _quietly(pending.dialog.dismiss())
    sessions._emit(agent, "dialog")
    return None


async def answer_files(agent: str, event: dict[str, Any]) -> str | None:
    """Hand the person's files (or none: cancelled) to the waiting request."""
    sess = sessions.get(agent)
    if sess is None:
        return "This agent has no open browser."
    if sess.controller != "user":
        return "Take control first: watching does not act on the page."
    pending = sess.chooser
    if pending is None or event.get("id") != pending.id:
        return None
    raw = event.get("files")
    files: list[dict[str, Any]] = []
    total = 0
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            data = base64.b64decode(str(item.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            return "A file could not be read. Try choosing it again."
        total += len(data)
        if total > MAX_UPLOAD_BYTES or len(files) >= MAX_UPLOAD_FILES:
            return (
                f"Files can be up to {MAX_UPLOAD_BYTES // (1024 * 1024)} MB in total "
                f"({MAX_UPLOAD_FILES} at most). Choose fewer or smaller files."
            )
        name = str(item.get("name") or "file").replace("/", "_").replace("\\", "_")[:200]
        mime = str(item.get("mime") or "application/octet-stream")[:200]
        files.append({"name": name, "mimeType": mime, "buffer": data})
    sess.chooser = None
    sess.touch()
    if files:
        if not pending.multiple:
            files = files[:1]
        try:
            await pending.chooser.set_files(files)
        except Exception as exc:
            sessions._emit(agent, "chooser")
            return f"The page did not take the files: {exc}"
    sessions._emit(agent, "chooser")
    return None


# ── <select> option lists ───────────────────────────────────────────────────

# The <select> a click just landed on, with its options; null otherwise.
# Follows focus into same-origin frames (a cross-origin frame cannot be read
# from the page, and its select keeps the keyboard: arrows still change it).
_SELECT_UNDER_JS = """([x, y, maxOptions]) => {
  let doc = document;
  let el = document.activeElement;
  let framed = false;
  while (el && (el.tagName === "IFRAME" || el.tagName === "FRAME")) {
    try { doc = el.contentDocument; } catch (e) { return null; }
    if (!doc) return null;
    el = doc.activeElement;
    framed = true;
  }
  if (!el || el.tagName !== "SELECT" || el.multiple || el.disabled) return null;
  if ((el.size || 0) > 1) return null;
  if (!framed) {
    const r = el.getBoundingClientRect();
    if (x < r.left || x > r.right || y < r.top || y > r.bottom) return null;
  }
  let label = "";
  try {
    if (el.labels && el.labels.length) label = el.labels[0].innerText;
  } catch (e) {}
  label = label || el.getAttribute("aria-label") || el.title || "";
  const options = Array.from(el.options).slice(0, maxOptions).map((o) => ({
    label: String(o.label || o.text || "").trim().slice(0, 200),
    disabled: !!o.disabled,
    group: o.parentElement && o.parentElement.tagName === "OPTGROUP" ? String(o.parentElement.label || "").slice(0, 120) : "",
  }));
  return { label: String(label).trim().slice(0, 160), selected: el.selectedIndex, options };
}"""

_SET_SELECT_JS = """(index) => {
  let el = document.activeElement;
  while (el && (el.tagName === "IFRAME" || el.tagName === "FRAME")) {
    try { el = el.contentDocument.activeElement; } catch (e) { return false; }
  }
  if (!el || el.tagName !== "SELECT" || el.disabled) return false;
  if (index < 0 || index >= el.options.length || el.options[index].disabled) return false;
  if (el.selectedIndex !== index) {
    el.selectedIndex = index;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  return true;
}"""


def _frame_area(page: Any, size: dict[str, int] | None) -> dict[str, int]:
    """What a viewer's fractions span: the frame's own area when the caller
    knows it (:meth:`Screencast.size`), else the page's viewport."""
    if size and size.get("width") and size.get("height"):
        return size
    return page.viewport_size or {"width": MAX_WIDTH, "height": MAX_HEIGHT}


async def select_under(
    agent: str, event: dict[str, Any], size: dict[str, int] | None = None
) -> dict[str, Any] | None:
    """After a click: the ``<select>`` it opened, for the dashboard to show.

    Chromium draws a select's option list as a window of its own, which the
    screencast never shows, so a person who clicked one saw nothing open.
    """
    sess = sessions.get(agent)
    if sess is None or sess.controller != "user" or sess.page is None or sess.dialog is not None:
        return None
    page = sess.page
    size = _frame_area(page, size)
    try:
        x = min(max(float(event.get("x", 0)), 0.0), 1.0) * size["width"]
        y = min(max(float(event.get("y", 0)), 0.0), 1.0) * size["height"]
        found = await asyncio.wait_for(
            page.evaluate(_SELECT_UNDER_JS, [x, y, MAX_SELECT_OPTIONS]), 3.0
        )
    except Exception:
        return None
    return found if isinstance(found, dict) and found.get("options") else None


# ── Date, time and colour pickers ───────────────────────────────────────────

# Inputs whose value Chromium picks in a popup of its own, which the screencast
# never shows: after a click lands on one, :func:`picker_under` describes it
# and the viewer draws its own picker; ``{"t": "setvalue"}`` sets what was
# chosen (:data:`_SET_PICKER_JS`). The popup the browser opened stays out of
# the way: it is a window of its own, which input dispatched to the page
# never reaches, and the page's next click closes it.
PICKER_TYPES: tuple[str, ...] = ("date", "time", "datetime-local", "month", "week", "color")

_PICKER_UNDER_JS = """([x, y, types]) => {
  let doc = document;
  let el = document.activeElement;
  let framed = false;
  while (el && (el.tagName === "IFRAME" || el.tagName === "FRAME")) {
    try { doc = el.contentDocument; } catch (e) { return null; }
    if (!doc) return null;
    el = doc.activeElement;
    framed = true;
  }
  if (!el || el.tagName !== "INPUT" || el.disabled || el.readOnly) return null;
  const type = String(el.type || "").toLowerCase();
  if (!types.includes(type)) return null;
  if (!framed) {
    const r = el.getBoundingClientRect();
    if (x < r.left || x > r.right || y < r.top || y > r.bottom) return null;
  }
  let label = "";
  try {
    if (el.labels && el.labels.length) label = el.labels[0].innerText;
  } catch (e) {}
  label = label || el.getAttribute("aria-label") || el.placeholder || el.title || "";
  return {
    kind: type,
    value: String(el.value || ""),
    min: String(el.min || ""),
    max: String(el.max || ""),
    step: String(el.step || ""),
    label: String(label).trim().slice(0, 160),
  };
}"""

# The value goes in through the element's own setter (a framework that tracks
# the value, React among them, only notices a change made that way) and the
# page hears input and change, as it would from the browser's picker. False
# when the browser refused the value (a date out of its range, a malformed one).
_SET_PICKER_JS = """([value, types]) => {
  let el = document.activeElement;
  while (el && (el.tagName === "IFRAME" || el.tagName === "FRAME")) {
    try { el = el.contentDocument.activeElement; } catch (e) { return false; }
  }
  if (!el || el.tagName !== "INPUT" || el.disabled || el.readOnly) return false;
  if (!types.includes(String(el.type || "").toLowerCase())) return false;
  if (el.value === value) return true;
  const proto = Object.getPrototypeOf(el);
  const desc = proto && Object.getOwnPropertyDescriptor(proto, "value");
  const set = (v) => { if (desc && desc.set) desc.set.call(el, v); else el.value = v; };
  const before = el.value;
  set(value);
  if (value && el.value !== value) {
    // Refused: the browser emptied the field, so what was there goes back.
    set(before);
    return false;
  }
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return true;
}"""


async def picker_under(
    agent: str, event: dict[str, Any], size: dict[str, int] | None = None
) -> dict[str, Any] | None:
    """After a click: the date, time or colour field it landed on, for the
    viewer to draw a picker for (``{kind, value, min, max, step, label}``)."""
    sess = sessions.get(agent)
    if sess is None or sess.controller != "user" or sess.page is None or sess.dialog is not None:
        return None
    page = sess.page
    size = _frame_area(page, size)
    try:
        x = min(max(float(event.get("x", 0)), 0.0), 1.0) * size["width"]
        y = min(max(float(event.get("y", 0)), 0.0), 1.0) * size["height"]
        found = await asyncio.wait_for(
            page.evaluate(_PICKER_UNDER_JS, [x, y, list(PICKER_TYPES)]), 3.0
        )
    except Exception:
        return None
    return found if isinstance(found, dict) and found.get("kind") else None


# ── What a page asks the browser for ────────────────────────────────────────

# A site that asks for the person's location, a camera, a microphone, the
# screen or leave to notify shows a prompt the browser draws outside the page,
# which no viewer sees; the agent's browser has none of these to give and is
# launched to refuse such requests at once (``docker/olano-browser/browserd.py``,
# ``--deny-permission-prompts``). Left at that, the site simply fails in a way
# nobody can explain, so the request is reported to the viewers as a notice
# instead (:func:`arm_page_permissions`): the page's own call goes on and is
# refused as before.
_PERMISSION_MARKER = "__olano:permission:"
PERMISSION_NOTES: dict[str, str] = {
    "location": (
        "The page asked for your location. The agent's browser has no location to give, "
        "so the request was declined."
    ),
    "notifications": (
        "The page asked to send notifications. The agent's browser does not show them, "
        "so the request was declined."
    ),
    "camera": "The page asked for a camera. The agent's browser has none, so the request was declined.",
    "microphone": (
        "The page asked for a microphone. The agent's browser has none, so the request was declined."
    ),
    "screen": (
        "The page asked to share a screen. The agent's browser cannot, so the request was declined."
    ),
}
# The same request from the same page is reported once per this many seconds
# (a page that watches the position asks again and again).
PERMISSION_NOTE_EVERY_S = 30.0

PERMISSION_JS = (
    """(() => {
  try {
    const MARK = "__MARKER__";
    const say = console.debug;
    const tell = (kind) => {
      try {
        if (typeof say === "function") say.call(console, MARK + kind);
      } catch (e) {}
    };
    const wrap = (obj, name, kindOf) => {
      if (!obj || typeof obj[name] !== "function") return;
      const real = obj[name];
      Object.defineProperty(obj, name, {
        configurable: true,
        writable: true,
        value: function (...args) {
          try { tell(kindOf(args)); } catch (e) {}
          return real.apply(this, args);
        },
      });
    };
    if (typeof Geolocation !== "undefined") {
      wrap(Geolocation.prototype, "getCurrentPosition", () => "location");
      wrap(Geolocation.prototype, "watchPosition", () => "location");
    }
    if (typeof Notification !== "undefined") {
      wrap(Notification, "requestPermission", () => "notifications");
    }
    if (typeof MediaDevices !== "undefined") {
      wrap(MediaDevices.prototype, "getUserMedia", (args) => {
        const c = (args && args[0]) || {};
        return c.video ? "camera" : "microphone";
      });
      wrap(MediaDevices.prototype, "getDisplayMedia", () => "screen");
    }
  } catch (e) {}
})();"""
).replace("__MARKER__", _PERMISSION_MARKER)


async def arm_page_permissions(context: Any, sess: Any) -> None:
    """Report what a page asks the browser for to the viewers (:data:`PERMISSION_JS`).

    For the live browser only. Never raises: a context that cannot take the
    script keeps refusing the requests quietly, as before.
    """

    def _on_ask(message: Any, kind: str) -> None:
        _on_page_permission(sess, message, kind)

    try:
        _listen_console(context, _PERMISSION_MARKER, _on_ask)
        await context.add_init_script(PERMISSION_JS)
    except Exception as exc:
        logger.debug("live view: permission requests not reported: %s", exc)


def _on_page_permission(sess: Any, source: Any, kind: Any) -> None:
    """The page in front asked for *kind*: tell the viewers, once in a while."""
    note = PERMISSION_NOTES.get(str(kind))
    if note is None or sess.viewers <= 0 or not sess.notice_listeners:
        return
    page = source.get("page") if isinstance(source, dict) else getattr(source, "page", None)
    if page is not sess.page:
        return
    now = time.time()
    if now - float(sess.permission_noted.get(str(kind), 0.0)) < PERMISSION_NOTE_EVERY_S:
        return
    sess.permission_noted[str(kind)] = now
    for listener in list(sess.notice_listeners):
        try:
            listener(note)
        except Exception:  # pragma: no cover - a viewer must never break the page
            logger.debug("live view: notice listener failed", exc_info=True)


def _click_count(event: dict[str, Any]) -> int:
    """Which click of a run this press is: 2 is a double click, 3 a triple
    (a paragraph). Beyond 3 a browser selects nothing more."""
    try:
        count = int(event.get("count") or 1)
    except (TypeError, ValueError):
        return 1
    return min(max(count, 1), 3)


async def dispatch_input(
    agent: str, event: dict[str, Any], size: dict[str, int] | None = None
) -> str | None:
    """Replay one viewer event on the agent's page. Returns an error or ``None``.

    *size* is the page area the viewer's fractions span (the screencast's
    frame, :meth:`Screencast.size`); without it the viewport is assumed.
    """
    sess = sessions.get(agent)
    if sess is None or sess.page is None or sess.page.is_closed():
        return "This agent has no open browser."
    if sess.controller != "user":
        return "Take control first: watching does not act on the page."
    sess.touch()
    page = sess.page
    kind = str(event.get("t") or "")
    if (
        sess.dialog is not None
        and sess.dialog.page is page
        and kind not in ("tab", "newtab", "closetab")
    ):
        # A page showing a dialog takes no input until it is answered, and a
        # DevTools input event sent meanwhile waits for it.
        return _DIALOG_REFUSAL
    size = _frame_area(page, size)

    def _xy() -> tuple[float, float]:
        x = min(max(float(event.get("x", 0)), 0.0), 1.0) * size["width"]
        y = min(max(float(event.get("y", 0)), 0.0), 1.0) * size["height"]
        return x, y

    button = str(event.get("button") or "left")
    if button not in _BUTTONS:
        button = "left"
    if kind in _GESTURE_KINDS:
        sess.last_input_at = time.time()
    try:
        if kind == "move":
            await page.mouse.move(*_xy())
        elif kind == "down":
            await page.mouse.move(*_xy())
            await page.mouse.down(button=button, click_count=_click_count(event))
        elif kind == "up":
            await page.mouse.move(*_xy())
            await page.mouse.up(button=button, click_count=_click_count(event))
        elif kind == "wheel":
            await page.mouse.wheel(float(event.get("dx", 0)), float(event.get("dy", 0)))
        elif kind in ("keydown", "keyup"):
            key = str(event.get("key") or "")
            try:
                if kind == "keydown":
                    await page.keyboard.down(key)
                else:
                    await page.keyboard.up(key)
            except Exception as exc:
                # A key this browser has no name for (a media key, a layout's
                # special key): nothing to replay, and not worth an error banner
                # in front of the person typing.
                if "Unknown key" in str(exc):
                    return None
                raise
        elif kind == "text":
            await page.keyboard.insert_text(str(event.get("text") or "")[:MAX_PASTE_CHARS])
        elif kind == "navigate":
            url = str(event.get("url") or "").strip()
            if url and "://" not in url:
                url = "https://" + url
            reason = await url_refusal(url, sess.guard.policy)
            if reason:
                return reason
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        elif kind == "back":
            await page.go_back(timeout=10_000)
        elif kind == "forward":
            await page.go_forward(timeout=10_000)
        elif kind == "stop":
            cdp = await sess.context.new_cdp_session(page)
            try:
                await cdp.send("Page.stopLoading")
            finally:
                await _quietly(cdp.detach())
        elif kind == "select":
            index = int(event.get("index", -1))
            if not await page.evaluate(_SET_SELECT_JS, index):
                return "That list is no longer open on the page. Tap it again."
        elif kind == "setvalue":
            value = str(event.get("value") or "")[:200]
            if not await page.evaluate(_SET_PICKER_JS, [value, list(PICKER_TYPES)]):
                return "The page did not take that value. Tap the field again and choose another."
        elif kind == "tab":
            target = _page_by_id(sess, event.get("id"))
            if target is None:
                return "That tab is closed."
            sess.page = target
            await _quietly(target.bring_to_front())
            sessions._emit(agent, "page")
        elif kind == "newtab":
            new = await sess.context.new_page()
            sess.page = new
            sessions._emit(agent, "page")
            # The context arms every new page (redirect-hop guard, identity)
            # in a task of its own; the first navigation must not race it.
            await asyncio.sleep(0)
            await asyncio.gather(*list(sessions._arming), return_exceptions=True)
            url = str(event.get("url") or "").strip()
            if url:
                return await dispatch_input(agent, {"t": "navigate", "url": url})
        elif kind == "closetab":
            target = _page_by_id(sess, event.get("id"))
            if target is None:
                return None
            if len(_open_pages(sess)) <= 1:
                # The last tab: keep the browser, show an empty page.
                await target.goto("about:blank")
            else:
                await target.close()
        elif kind == "reload":
            await page.reload(timeout=20_000)
        else:
            return f"Unknown input event {kind!r}."
    except Exception as exc:
        if kind in ("navigate", "back", "forward", "reload") and "ERR_ABORTED" in str(exc):
            return None  # stopped (Stop, or another navigation took over)
        return f"Input failed: {exc}"
    return None


async def copy_selection(agent: str, cut: bool = False) -> tuple[str, str | None]:
    """The page's selected text for the viewer's clipboard: ``(text, error)``.

    ``cut`` also removes it from the page (the page's own cut shortcut, so a
    text field records it as one undoable edit). Same rule as every other
    input: only while a person holds control.
    """
    sess = sessions.get(agent)
    if sess is None or sess.page is None or sess.page.is_closed():
        return "", "This agent has no open browser."
    if sess.controller != "user":
        return "", "Take control first: watching does not act on the page."
    sess.touch()
    page = sess.page
    try:
        text = await page.evaluate(_SELECTION_JS)
        text = str(text or "")[:MAX_CLIPBOARD_CHARS]
        if cut and text:
            await page.keyboard.press("Control+x")
    except Exception as exc:
        return "", f"Copy failed: {exc}"
    return text, None
