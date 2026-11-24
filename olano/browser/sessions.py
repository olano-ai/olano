"""One isolated browser session per agent, on the session loop.

The browser tools used to drive one module-global page: every agent on a box
shared one cookie jar, so one agent signing in signed every other agent in,
and two agents' calls fought over the same tab. A :class:`Session` now belongs
to exactly one agent and holds its own ``BrowserContext``:

- **headless**: a context on the one shared local Chromium (contexts share no
  cookies, storage or cache with each other);
- **live**: a context on the agent's own ``olano-browser`` container, reached
  through :class:`olano.browser.sidecar.ExecPipeRelay`;
- **remote**: a context on a hosted browser over the DevTools protocol.

**Sign-ins persist, outside the workspace.** A context's cookies and storage
(``storage_state``, IndexedDB included) are saved when the session closes,
when a person hands control back, and every few minutes, to
``<home>/browser/state/<agent>.json`` (0600). That file is a credential: it is
kept out of the agent's workspace so no file tool, sandbox or revision
snapshot can read it, and only this module loads it, into that agent's
context. ``forget_state`` deletes it.

**Bounded.** ``browser.max_sessions`` caps how many agents hold a session at
once (the least recently used idle one is closed to make room, else the call
is refused with who holds them); ``browser.idle_timeout`` closes a session
nobody used or watched for that long; a local session is refused when free
memory is below what one needs (:func:`olano.browser.policy.open_refusal`).
When the last headless session closes, the shared Chromium is shut down too,
so an idle box carries no browser at all.

**Identity, proxy, clock.** A headless context presents the identity read off
its own Chromium (:func:`olano.browser.launch.probe_identity`, once per
launch), every local context goes through ``BROWSER_PROXY_URL`` when one is
set, and every context keeps ``browser.timezone``. A session opened under a
different proxy or timezone than the call now asks for is reopened (sign-ins
saved first), so a changed setting applies on the agent's next browser call.

**Control.** ``controller`` is ``"agent"`` or ``"user"``. While a person holds
control (live mode, from the agent's Browser tab), the agent's acting tools
refuse with an explanation instead of fighting the person for the mouse.
``attention`` is set when the agent asks for a person (``browser_request_user``).

Every coroutine here runs on :mod:`olano.browser.loop`'s loop; nothing else
touches a Playwright object.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from olano.browser import blockpage, debug_trace, egress_proxy, launch, policy, sidecar
from olano.browser.guard import GuardPolicy, GuardState, guard_page, install_guards
from olano.browser.passkeys import refuse_passkeys

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 30.0
SAVE_INTERVAL_S = 300.0
# A session used within this many seconds is never evicted to make room.
EVICT_MIN_IDLE_S = 30.0


class BrowserBusy(RuntimeError):
    """Every session slot is held by an agent that is using it."""


class BrowserUnavailable(RuntimeError):
    """The browser cannot be opened right now; the message is user-facing."""


@dataclass
class Session:
    agent: str
    kind: str  # "headless" | "live" | "remote"
    context: Any
    page: Any
    browser: Any = None  # per-agent Browser for live/remote; None for headless
    relay: sidecar.ExecPipeRelay | None = None
    container: str = ""
    guard: GuardState = field(default_factory=GuardState)
    # The proxy every connection of a local context goes through, so the guard
    # sees WebSockets too (browser/egress_proxy.py); ``None`` for remote.
    egress: egress_proxy.EgressProxy | None = None
    opened_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    last_saved: float = field(default_factory=time.time)
    controller: str = "agent"
    attention: str = ""
    viewers: int = 0
    # The exit (launch.proxy_fingerprint) and clock this context was built with.
    proxy_key: str = ""
    timezone: str = ""
    # Where the agent last aimed, normalised to the viewport (0..1), for the
    # cursor the live view draws: a screencast frame never contains a pointer.
    pointer: dict[str, Any] | None = None
    # The compact snapshot the agent last read and the page it was of, so an
    # action on the same page can report only what changed (tools/browser.py).
    last_snapshot: str = ""
    last_url: str = ""
    pointer_listeners: set[Callable[[dict[str, Any]], None]] = field(default_factory=set)
    # Live views to wake when something they show changed (the page in front,
    # a dialog or file request waiting for the person); see browser/live.py.
    state_listeners: set[Callable[[str], None]] = field(default_factory=set)
    # Live views to hand text a page copied for the person in control (a
    # "copy" button on the page), and when that person last pressed or typed
    # something, which a page's copy must follow (live.arm_page_copies).
    clipboard_listeners: set[Callable[[str], None]] = field(default_factory=set)
    last_input_at: float = 0.0
    # Live views to tell what a page asked the browser for and could not have
    # (a location, a camera: live.arm_page_permissions), and when each kind
    # was last reported, so a page that keeps asking is reported once in a while.
    notice_listeners: set[Callable[[str], None]] = field(default_factory=set)
    permission_noted: dict[str, float] = field(default_factory=dict)
    # The recording of this browser that is running, if one is
    # (browser/recording.Recorder); ended with the session.
    recorder: Any = None
    # Tabs: a stable small number per page for the live view, and the last
    # title read from each (a busy page cannot answer for its title).
    page_ids: dict[Any, int] = field(default_factory=dict)
    titles: dict[int, str] = field(default_factory=dict)
    next_page_id: int = 0
    # A page's JavaScript dialog or file request waiting for the person in
    # control (live.PendingDialog / live.PendingChooser).
    dialog: Any = None
    chooser: Any = None
    # A check only a person can pass that the agent is waiting on
    # (olano.browser.human_check): what the dashboard shows, and whether the
    # person chose to skip it. ``human_check_choice`` is the mode the agent
    # picked for this site (browser_navigate's human_check), "" for the setting.
    human_check: dict[str, Any] | None = None
    human_check_skipped: bool = False
    human_check_choice: str = ""
    # Whether the run that last used this browser was started by a person in a
    # chat (only such a run may hold a check with no time limit).
    human_check_interactive: bool = True
    # The running or last debug recording of this browser (browser/debug_trace.py).
    trace: Any = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self) -> None:
        self.last_used = time.time()

    def alive(self) -> bool:
        try:
            if self.browser is not None and not self.browser.is_connected():
                return False
            if self.egress is not None and not self.egress.alive():
                return False
            return self.context is not None
        except Exception:
            return False

    def snapshot(self) -> dict[str, Any]:
        url = title = ""
        try:
            if self.page is not None and not self.page.is_closed():
                url = str(self.page.url)
        except Exception:
            pass
        return {
            "agent": self.agent,
            "kind": self.kind,
            "opened_at": self.opened_at,
            "last_used": self.last_used,
            "controller": self.controller,
            "attention": self.attention,
            "human_check": dict(self.human_check) if self.human_check else None,
            "viewers": self.viewers,
            "url": url,
            "title": title,
            "blocked": list(self.guard.blocked)[-5:],
            "recording": self.recorder.public() if self.recorder is not None else None,
        }


_sessions: dict[str, Session] = {}
_pw: Any = None
_headless: Any = None
_headless_args: list[str] = []
_headless_identity: launch.Identity | None = None
_sweeper: asyncio.Task | None = None
_listeners: list[Callable[[dict[str, Any]], None]] = []
# Arming tasks for pages a context opened on its own (popups): held here
# because the loop keeps only a weak reference to a task, and a collected
# task never runs.
_arming: set[asyncio.Task] = set()
_open_lock: tuple[int, asyncio.Lock] | None = None


def on_change(callback: Callable[[dict[str, Any]], None]) -> None:
    """Register ``callback(event)`` for session changes (the server emits SSE)."""
    _listeners.append(callback)


def _emit(agent: str, what: str) -> None:
    sess = _sessions.get(agent)
    event = {"agent": agent, "event": what, "session": sess.snapshot() if sess else None}
    for cb in list(_listeners):
        try:
            cb(event)
        except Exception:  # pragma: no cover - a listener must never break a session
            logger.debug("browser session listener failed", exc_info=True)
    if sess is not None:
        for wake in list(sess.state_listeners):
            try:
                wake(what)
            except Exception:  # pragma: no cover - same rule
                logger.debug("browser live listener failed", exc_info=True)


def kind_for(avail: policy.BrowserAvailability) -> str:
    if not avail.enabled:
        return "off"
    if avail.provider == "remote":
        return "remote"
    return avail.mode


def state_dir() -> Path:
    from olano.config.home import get_olano_paths

    return get_olano_paths().root / "browser" / "state"


def state_path(agent: str) -> Path:
    return state_dir() / f"{sidecar.agent_slug(agent)}.json"


def forget_state(agent: str) -> bool:
    path = state_path(agent)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _write_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    tmp.replace(path)


# Cookies a bot check sets on the browser it checked: Cloudflare's pass
# (cf_clearance), its bot score (__cf_bm), its visitor ids and the state of a
# challenge in progress. Each belongs to the browser it was issued to, so none
# is a sign-in to keep: restored into the next browser, a pass that browser
# was never given made Cloudflare challenge on the first page load and refuse
# every new pass a person earned while the old one kept coming back (measured
# on propertyguru.com.sg: the same exit and the same context setup loaded
# with no check at all once the saved file was left out). They stay in the
# open browser, where they belong; they are left out of the file both ways,
# so a file saved before this rule is cleaned the next time it is loaded.
_CHECK_COOKIES = frozenset({"cf_clearance", "__cf_bm", "_cfuvid", "__cfruid"})
_CHECK_COOKIE_PREFIXES = ("cf_chl_",)


def _is_check_cookie(name: str) -> bool:
    return name in _CHECK_COOKIES or name.startswith(_CHECK_COOKIE_PREFIXES)


def portable_state(data: dict[str, Any]) -> dict[str, Any]:
    """*data* (a ``storage_state``) without the cookies of :data:`_CHECK_COOKIES`."""
    cookies = data.get("cookies")
    if not isinstance(cookies, list):
        return data
    kept = [c for c in cookies if not _is_check_cookie(str((c or {}).get("name") or ""))]
    return {**data, "cookies": kept}


async def save_state(sess: Session) -> None:
    try:
        try:
            data = await sess.context.storage_state(indexed_db=True)
        except TypeError:  # older Playwright without the IndexedDB option
            data = await sess.context.storage_state()
        await asyncio.to_thread(_write_state, state_path(sess.agent), portable_state(data))
        sess.last_saved = time.time()
    except Exception as exc:
        logger.debug("browser: saving %s's sign-ins failed: %s", sess.agent, exc)


def _load_state(agent: str) -> dict[str, Any] | None:
    """The agent's saved sign-ins, ready for ``new_context(storage_state=...)``."""
    path = state_path(agent)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("browser: %s's saved sign-ins are unreadable, not loaded: %s", agent, exc)
        return None
    return portable_state(data) if isinstance(data, dict) else None


async def _playwright() -> Any:
    global _pw
    if _pw is None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - base dependency
            raise BrowserUnavailable("playwright is not installed on this machine.") from exc
        _pw = await async_playwright().start()
    return _pw


async def _shared_headless() -> Any:
    global _headless, _headless_args, _headless_identity
    args = launch.agent_launch_args()
    if _headless is not None and (not _headless.is_connected() or args != _headless_args):
        try:
            await _headless.close()
        except Exception:
            pass
        _headless = None
    if _headless is None:
        pw = await _playwright()
        _headless = await pw.chromium.launch(
            headless=True, args=args, **launch.agent_launch_kwargs()
        )
        _headless_args = args
        _headless_identity = await launch.probe_identity(_headless)
    return _headless


def _proxy(raw: str | None) -> dict[str, str] | None:
    """``BROWSER_PROXY_URL`` parsed, or the browser refused with why."""
    try:
        return launch.parse_proxy(raw)
    except launch.ProxyError as exc:
        raise BrowserUnavailable(
            f"{exc} Fix it in the vault, or delete it to browse without a proxy."
        ) from None


async def _close_headless_if_unused() -> None:
    global _headless
    if (
        _headless is not None
        and not _isolated_inflight
        and not any(s.kind == "headless" for s in _sessions.values())
    ):
        try:
            await _headless.close()
        except Exception:
            pass
        _headless = None


def _proxy_auth(proxy: dict[str, str] | None) -> tuple[str, str] | None:
    if proxy and proxy.get("username"):
        return proxy["username"], proxy.get("password", "")
    return None


async def _arm_page(
    context: Any,
    page: Any,
    state: GuardState,
    identity: launch.Identity | None,
    proxy: dict[str, str] | None,
) -> None:
    """What every page of an agent's context gets: the redirect-hop
    interception (:func:`guard_page`) and the identity override."""
    await guard_page(context, page, state, proxy_auth=_proxy_auth(proxy))
    await launch.apply_identity(context, page, identity)


def _arm_new_pages(
    context: Any,
    state: GuardState,
    identity: launch.Identity | None,
    proxy: dict[str, str] | None,
) -> None:
    """Arm every page *context* opens from now on (popups, ``target=_blank``).
    Installed after the first page is armed explicitly, so no page is armed
    twice and the first navigation never races the arming."""

    def _on_page(page: Any) -> None:
        try:
            task = asyncio.get_running_loop().create_task(
                _arm_page(context, page, state, identity, proxy)
            )
        except RuntimeError:  # pragma: no cover - no loop: nothing to schedule on
            return
        _arming.add(task)
        task.add_done_callback(_arming.discard)

    context.on("page", _on_page)


def _lock() -> asyncio.Lock:
    """The open/close lock, rebuilt if the session loop was restarted (an
    asyncio lock is bound to the loop it was first awaited on)."""
    global _open_lock
    from olano.browser.loop import generation

    gen = generation()
    if _open_lock is None or _open_lock[0] != gen:
        _open_lock = (gen, asyncio.Lock())
    return _open_lock[1]


async def _make_room(avail: policy.BrowserAvailability, agent: str) -> None:
    if len(_sessions) < avail.max_sessions:
        return
    now = time.time()
    candidates = [
        s
        for s in _sessions.values()
        if s.agent != agent
        and s.controller == "agent"
        and s.viewers == 0
        and not s.lock.locked()
        and now - s.last_used >= EVICT_MIN_IDLE_S
    ]
    if candidates:
        victim = min(candidates, key=lambda s: s.last_used)
        logger.info("browser: closing %s's idle session to make room for %s", victim.agent, agent)
        await close(victim.agent)
        return
    holders = ", ".join(sorted(_sessions)) or "other agents"
    raise BrowserBusy(
        f"All {avail.max_sessions} browser session(s) on this machine are in use ({holders}). "
        "Try again in a minute, or raise the browser session limit in Config."
    )


async def _open(
    agent: str,
    kind: str,
    gpol: GuardPolicy,
    remote_url: str | None,
    proxy: dict[str, str] | None = None,
    timezone: str = "",
) -> Session:
    state = _load_state(agent)
    guard = GuardState(policy=gpol)
    browser = None
    relay: sidecar.ExecPipeRelay | None = None
    container = ""
    identity: launch.Identity | None = None
    egress: egress_proxy.EgressProxy | None = None
    if kind == "headless":
        refusal = policy.open_refusal("headless")
        if refusal:
            raise BrowserUnavailable(refusal)
        shared = await _shared_headless()
        identity = _headless_identity
        egress = await egress_proxy.start_local(guard, upstream=proxy)
        try:
            context = await shared.new_context(
                **launch.context_kwargs(
                    headless=True,
                    identity=identity,
                    timezone=timezone,
                    proxy=egress.playwright_proxy(),
                ),
                storage_state=state,
            )
        except Exception:
            await egress.close()
            raise
    elif kind == "live":
        refusal = policy.open_refusal("live")
        if refusal:
            raise BrowserUnavailable(refusal)
        try:
            container, version = await asyncio.to_thread(
                sidecar.ensure_container, agent, proxied=proxy is not None
            )
        except sidecar.SidecarUnavailable as exc:
            raise BrowserUnavailable(str(exc)) from exc
        relay = sidecar.ExecPipeRelay(sidecar.pipe_argv(container))
        await relay.start()
        pw = await _playwright()
        try:
            browser = await pw.chromium.connect_over_cdp(
                relay.ws_url(str(version.get("webSocketDebuggerUrl", ""))),
                headers=relay.headers(),
                timeout=30_000,
            )
            egress = await egress_proxy.start_in_container(container, guard, upstream=proxy)
            context = await browser.new_context(
                **launch.context_kwargs(
                    headless=False, timezone=timezone, proxy=egress.playwright_proxy()
                ),
                storage_state=state,
            )
        except Exception:
            if egress is not None:
                await egress.close()
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
            await relay.close()
            raise
    elif kind == "remote":
        if not remote_url:
            raise BrowserUnavailable(
                "The remote browser provider is selected but no address is stored. "
                "Save the hosted browser's DevTools URL in the vault as BROWSER_REMOTE_CDP_URL."
            )
        pw = await _playwright()
        browser = await pw.chromium.connect_over_cdp(remote_url, timeout=30_000)
        # The hosted service picks its own exit; BROWSER_PROXY_URL is for a
        # browser on this machine.
        context = await browser.new_context(
            **launch.context_kwargs(headless=False, timezone=timezone), storage_state=state
        )
    else:
        raise BrowserUnavailable("Browser control is turned off on this machine.")

    sess = Session(
        agent=agent,
        kind=kind,
        context=context,
        page=None,
        browser=browser,
        relay=relay,
        container=container,
        guard=guard,
        egress=egress,
        proxy_key=launch.proxy_fingerprint(proxy) if kind != "remote" else "",
        timezone=timezone,
    )
    # A local context's WebSockets are checked by its egress proxy; only the
    # remote browser, which is not on this machine, needs Playwright's routing.
    await install_guards(context, sess.guard, websockets=egress is None)
    await refuse_passkeys(context)

    from olano.browser.live import arm_page_copies, arm_page_for_people, arm_page_permissions

    if kind == "live":
        await arm_page_copies(context, sess)
        await arm_page_permissions(context, sess)

    def _on_close(page: Any) -> None:
        sess.titles.pop(sess.page_ids.pop(page, -1), None)
        # The page in front closed (a sign-in popup closes itself once it has
        # handed the result back): fall back to the newest page still open,
        # the one that opened it. Left on the closed page, the live view froze
        # on its last frame and every input answered "no open browser".
        if sess.page is not page:
            _emit(agent, "tabs")
            return
        sess.page = _newest_open_page(sess.context)
        _emit(agent, "page")

    tracked: weakref.WeakSet[Any] = weakref.WeakSet()

    def _track(page: Any) -> None:
        # Once per page: the context's "page" event also fires for the first
        # page, which is tracked explicitly below as well, and a second dialog
        # listener would dismiss the dialog the first one holds for a person.
        if page in tracked:
            return
        tracked.add(page)
        page.on("close", _on_close)
        arm_page_for_people(sess, page)

    def _on_page(page: Any) -> None:
        # Follow popups and new tabs (an OAuth window, a target=_blank link):
        # the newest page is the one the agent and the viewer see.
        sess.page = page
        _track(page)
        _emit(agent, "page")

    context.on("page", _on_page)
    sess.page = await context.new_page()
    _track(sess.page)
    # The browser's proxy is the egress proxy, which needs no sign-in of its
    # own (the agent's proxy is its upstream); remote has none.
    await _arm_page(context, sess.page, sess.guard, identity, None)
    _arm_new_pages(context, sess.guard, identity, None)
    return sess


def _newest_open_page(context: Any) -> Any:
    """The most recently opened page of *context* that is still open, or ``None``."""
    try:
        pages = [p for p in context.pages if not p.is_closed()]
    except Exception:
        return None
    return pages[-1] if pages else None


async def acquire(
    agent: str,
    *,
    avail: policy.BrowserAvailability,
    gpol: GuardPolicy,
    remote_url: str | None = None,
    proxy_url: str | None = None,
) -> Session:
    """The agent's session, opened if needed. Runs on the session loop.

    *proxy_url* is the agent's ``BROWSER_PROXY_URL`` (ignored for the remote
    provider); a session built for another proxy or timezone is reopened.
    """
    kind = kind_for(avail)
    if kind == "off":
        raise BrowserUnavailable(avail.reason or "Browser control is turned off on this machine.")
    proxy = _proxy(proxy_url) if kind != "remote" else None
    proxy_key = launch.proxy_fingerprint(proxy)
    timezone = avail.timezone
    async with _lock():
        sess = _sessions.get(agent)
        if sess is not None and (
            sess.kind != kind
            or not sess.alive()
            or sess.proxy_key != proxy_key
            or sess.timezone != timezone
        ):
            await close(agent, save=sess.alive())
            sess = None
        if sess is None:
            await _make_room(avail, agent)
            sess = await _open(agent, kind, gpol, remote_url, proxy, timezone)
            _sessions[agent] = sess
            _ensure_sweeper()
            _emit(agent, "opened")
        sess.guard.policy = gpol
        sess.guard.site_refusal = _agent_site_refusal(
            sess, blockpage.navigation_guard(agent, proxy_key=proxy_key, live=kind == "live")
        )
        sess.touch()
        if sess.page is None or sess.page.is_closed():
            sess.page = _newest_open_page(sess.context) or await sess.context.new_page()
        return sess


def _agent_site_refusal(
    sess: Session, remembered: Callable[[str], str | None]
) -> Callable[[str], str | None]:
    """The block memory stops the agent's navigations, never a person's.

    The person the agent handed the browser to is there to get past the check
    that stopped it, and completing one navigates (Cloudflare posts back to
    the page and reloads it): refused, they got Chromium's "blocked" page
    instead of the site. The egress policy still applies to them. A check the
    agent is waiting on a person for is let through too: it reloads itself
    with a new box every two minutes or so, and refusing that reload turned it
    into an error page before anyone arrived.
    """

    def _refusal(url: str) -> str | None:
        if sess.controller == "user" or sess.human_check is not None:
            return None
        return remembered(url)

    return _refusal


_isolated_inflight = 0
# The egress proxy of each isolated context, closed with it.
_isolated_egress: dict[int, egress_proxy.EgressProxy] = {}


async def isolated_context(
    *,
    kind: str,
    gpol: GuardPolicy,
    viewport: dict[str, int],
    remote_url: str | None = None,
    disable_http2: bool = False,
    state: GuardState | None = None,
    proxy_url: str | None = None,
    timezone: str = "",
) -> tuple[Any, Any, Any]:
    """A throwaway guarded context for a one-shot capture, with its one page
    opened and armed: ``(context, browser, page)``.

    *state* is the guard state to install (pass one to read what the capture
    refused, or to let it open allowed local files); a fresh one otherwise.

    No saved sign-ins are loaded (a screenshot of a URL is the public page).
    Local kinds reuse the shared headless Chromium, so a screenshot costs a
    context rather than a browser launch; the HTTP/2 retry gets a private
    browser instead, because relaunching the shared one would cut every
    agent's session. Close both with :func:`release_isolated`.
    """
    global _isolated_inflight
    browser = None
    identity: launch.Identity | None = None
    proxy = _proxy(proxy_url) if kind != "remote" else None
    if state is None:
        state = GuardState(policy=gpol)
    else:
        state.policy = gpol
    egress: egress_proxy.EgressProxy | None = None
    if kind != "remote":
        egress = await egress_proxy.start_local(state, upstream=proxy)

    def _kwargs(headless: bool) -> dict[str, Any]:
        kwargs = launch.context_kwargs(
            headless=headless,
            identity=identity,
            timezone=timezone,
            proxy=egress.playwright_proxy() if egress is not None else None,
        )
        kwargs["viewport"] = dict(viewport)
        return kwargs

    try:
        if kind == "remote":
            if not remote_url:
                raise BrowserUnavailable(
                    "The remote browser provider is selected but BROWSER_REMOTE_CDP_URL is not set."
                )
            pw = await _playwright()
            browser = await pw.chromium.connect_over_cdp(remote_url, timeout=30_000)
            context = await browser.new_context(**_kwargs(False))
        elif disable_http2:
            pw = await _playwright()
            browser = await pw.chromium.launch(
                headless=True,
                args=launch.agent_launch_args() + ["--disable-http2"],
                **launch.agent_launch_kwargs(),
            )
            identity = await launch.probe_identity(browser)
            context = await browser.new_context(**_kwargs(True))
        else:
            refusal = policy.open_refusal("headless") if not _headless else None
            if refusal:
                raise BrowserUnavailable(refusal)
            shared = await _shared_headless()
            identity = _headless_identity
            context = await shared.new_context(**_kwargs(True))
    except Exception:
        if egress is not None:
            await egress.close()
        raise
    if egress is not None:
        _isolated_egress[id(context)] = egress
    _isolated_inflight += 1
    await install_guards(context, state, websockets=egress is None)
    page = await context.new_page()
    await _arm_page(context, page, state, identity, None)
    _arm_new_pages(context, state, identity, None)
    return context, browser, page


async def release_isolated(context: Any, browser: Any) -> None:
    global _isolated_inflight
    _isolated_inflight = max(0, _isolated_inflight - 1)
    try:
        await context.close()
    except Exception:
        pass
    egress = _isolated_egress.pop(id(context), None)
    if egress is not None:
        await egress.close()
    if browser is not None:
        try:
            await browser.close()
        except Exception:
            pass
    await _close_headless_if_unused()


def get(agent: str) -> Session | None:
    return _sessions.get(agent)


def all_sessions() -> list[dict[str, Any]]:
    return [s.snapshot() for s in list(_sessions.values())]


async def close(agent: str, *, save: bool = True) -> bool:
    sess = _sessions.pop(agent, None)
    if sess is None:
        return False
    if sess.trace is not None:
        debug_trace.stop(sess)
    if sess.recorder is not None:
        # The recording is saved before the browser goes, whatever ends the session.
        try:
            await sess.recorder.stop()
        except Exception:
            logger.debug("browser: recording did not stop cleanly", exc_info=True)
    if save and sess.alive():
        await save_state(sess)
    try:
        await sess.context.close()
    except Exception:
        pass
    if sess.egress is not None:
        await sess.egress.close()
    if sess.browser is not None:
        try:
            await sess.browser.close()
        except Exception:
            pass
    if sess.relay is not None:
        await sess.relay.close()
    if sess.kind == "live":
        await asyncio.to_thread(sidecar.stop_container, agent)
    await _close_headless_if_unused()
    _emit(agent, "closed")
    return True


async def close_all(*, save: bool = True) -> None:
    for agent in list(_sessions):
        await close(agent, save=save)


async def set_controller(agent: str, who: str) -> Session:
    sess = _sessions.get(agent)
    if sess is None:
        raise BrowserUnavailable("This agent has no open browser.")
    if who not in ("agent", "user"):
        raise ValueError("controller must be 'agent' or 'user'")
    sess.controller = who
    sess.touch()
    debug_trace.note(sess, "controller", who=who)
    if who == "agent":
        # A dialog or file request left for the person is answered for them
        # (dismissed, no file), as it would have been with nobody in control.
        from olano.browser.live import clear_pending

        await clear_pending(sess)
        # The person may have just signed in: keep it, and let the agent try
        # the sites that had blocked it again.
        sess.attention = ""
        await save_state(sess)
        from olano.browser import blockpage

        blockpage.forget(agent)
    _emit(agent, "controller")
    return sess


# Glide time per aimed action while someone watches: long enough that the
# screencast shows the pointer travel and the hover state, short enough that
# a watched agent is not visibly slower.
POINTER_STEPS = 8
POINTER_SETTLE_S = 0.08


def note_pointer(sess: Session, x: float, y: float, kind: str) -> None:
    """Record where the agent is acting and tell every live viewer."""
    size = _viewport(sess)
    event = {
        "x": round(min(max(x / size["width"], 0.0), 1.0), 4),
        "y": round(min(max(y / size["height"], 0.0), 1.0), 4),
        "kind": kind,
        "at": time.time(),
    }
    sess.pointer = event
    for listener in list(sess.pointer_listeners):
        try:
            listener(event)
        except Exception:  # pragma: no cover - a viewer must never break an action
            logger.debug("browser pointer listener failed", exc_info=True)


def _viewport(sess: Session) -> dict[str, int]:
    try:
        size = sess.page.viewport_size if sess.page is not None else None
    except Exception:
        size = None
    return size or dict(launch.VIEWPORT)


async def aim(sess: Session, locator: Any, kind: str) -> None:
    """Point at *locator* before the agent acts on it.

    Only does work while someone watches (``sess.viewers``): the mouse glides
    there in visible steps, so the screencast shows the travel and any hover
    effect, and the viewers get a pointer event to draw the cursor and a click
    ripple. Never raises: a target without a box (hidden, detached) simply
    gets no pointer, and the action itself reports any real problem.
    """
    if sess.viewers <= 0:
        return
    try:
        box = await locator.bounding_box(timeout=3_000)
    except Exception:
        box = None
    if not box:
        return
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    try:
        await sess.page.mouse.move(x, y, steps=POINTER_STEPS)
    except Exception:
        pass
    note_pointer(sess, x, y, "move")
    await asyncio.sleep(POINTER_SETTLE_S)
    note_pointer(sess, x, y, kind)


def begin_human_check(sess: Session, info: dict[str, Any]) -> None:
    """The agent is waiting for a person to pass a check (browser/human_check)."""
    sess.human_check = dict(info)
    sess.human_check_skipped = False
    sess.attention = str(info.get("reason") or "")[:500]
    debug_trace.start(sess, "human check")
    debug_trace.note(
        sess, "check_started", site=info.get("site"), who=info.get("who"), mode=info.get("mode")
    )
    _emit(sess.agent, "human_check")


async def end_human_check(sess: Session, outcome: str) -> None:
    """The wait is over (``passed``, ``skipped``, ``timeout``, ``cancelled``).

    A person still holding control when the page got past the check is done
    with it: control goes back to the agent, as Hand back would (sign-ins
    saved, blocks forgotten), so nobody has to press it.
    """
    sess.human_check = None
    sess.human_check_skipped = False
    sess.attention = ""
    debug_trace.note(sess, "check_ended", outcome=outcome)
    debug_trace.stop_later(sess)
    if outcome == "passed" and sess.controller == "user":
        await set_controller(sess.agent, "agent")
    elif outcome == "passed":
        await save_state(sess)
    _emit(sess.agent, "human_check")


async def skip_human_check(agent: str) -> bool:
    """The person chose not to pass the check: the waiting agent moves on."""
    sess = _sessions.get(agent)
    if sess is None or sess.human_check is None:
        return False
    sess.human_check_skipped = True
    return True


async def request_user(agent: str, reason: str) -> Session:
    sess = _sessions.get(agent)
    if sess is None:
        raise BrowserUnavailable("This agent has no open browser.")
    sess.attention = (reason or "The agent needs a person in the browser.").strip()[:500]
    _emit(agent, "attention")
    return sess


async def _sweep_once() -> None:
    avail = policy.browser_availability()
    wanted = kind_for(avail)
    now = time.time()
    for agent, sess in list(_sessions.items()):
        if wanted == "off" or sess.kind != wanted:
            await close(agent)
            continue
        if not sess.alive():
            await close(agent, save=False)
            continue
        idle = now - sess.last_used
        if sess.viewers == 0 and idle > avail.idle_timeout_minutes * 60:
            logger.info("browser: closing %s's session after %.0f min idle", agent, idle / 60)
            await close(agent)
            continue
        if now - sess.last_saved > SAVE_INTERVAL_S:
            await save_state(sess)


async def _sweep_forever() -> None:
    global _sweeper
    try:
        while _sessions:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            try:
                await _sweep_once()
            except Exception:
                logger.debug("browser sweep failed", exc_info=True)
    finally:
        _sweeper = None


def _ensure_sweeper() -> None:
    global _sweeper
    if _sweeper is None or _sweeper.done():
        _sweeper = asyncio.get_running_loop().create_task(_sweep_forever())


def reset_for_tests() -> None:
    """Forget every session without touching Playwright (unit tests only)."""
    global _pw, _headless, _headless_args, _headless_identity, _sweeper, _open_lock
    global _isolated_inflight
    _sessions.clear()
    _isolated_inflight = 0
    _pw = None
    _headless = None
    _headless_args = []
    _headless_identity = None
    _sweeper = None
    _open_lock = None
