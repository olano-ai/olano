"""A plain browser beside the agent's, to tell an address problem from an automation one.

When a site's human check keeps coming back after a person passed it (the
debug trace reads ``clearance_rejected``: the pass was issued, sent back, and
refused), two explanations remain: the site scores the *address* (a datacenter
exit), or it scores the *browser* (the DevTools connection the agent drives it
through, the input that connection injects, the viewport it emulates). Nothing
in the agent's own browser can separate them, because the agent's tab exists
only while that connection is attached: Playwright creates its context with
``disposeOnDetach``, so unplugging the agent deletes the tab.

So this runs a second ``olano-browser`` container, same image, same network
lane, same exit, and opens the page in it in one of three ways:

- ``plain``: through Chromium's ``/json/new`` HTTP endpoint, so no DevTools
  client is ever attached. The person sees the virtual display itself (X11
  ``GetImage``, browser UI included) and acts through it (the XTEST
  extension), the way a remote desktop works: to the page, the click comes
  from the display, like a person's at a real screen.
- ``attached``: the same, with a DevTools client (Playwright) attached to the
  tab before it loads, as the agent's is. Input still goes through the display.
- ``devtools-input``: attached, and the person's clicks and keys are replayed
  through DevTools (``Input.dispatch*``), which is how the live view sends
  them today.
- ``context``: attached, the page opened in a context of its own as the
  agent's is, with any of :data:`CONTEXT_PARTS` of the agent's setup added,
  so the part that brings the check on can be found one at a time.

A check can also wait for the second page: Cloudflare's script on the first
page scores the browser into a cookie, and the next request is judged on it
(on sandbox-mem the agent's browser loaded the home page, then was challenged
on a search). ``then_url`` loads a second page ``then_after_s`` after the
first, in the same browser (a new tab without DevTools, the same tab in a
context), and the status keeps the first page's title beside the second's.

The person passes the check; :func:`status` reads each tab's title and URL off
``/json/list`` (no attach needed) and the exit address. Admin-only, a debugging
tool: nothing in the agent's own session changes, and the container is removed
with :func:`stop`, when the agent's browser closes, or at the next boot
(it carries the ``olano.browser`` label :func:`sidecar.reap_orphans` sweeps).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import struct
import time
import zlib
from dataclasses import dataclass, field
from typing import Any

from olano.browser import sidecar

logger = logging.getLogger(__name__)

VARIANTS = ("plain", "attached", "devtools-input", "context")
# What the ``context`` variant can add, one piece of the agent's own session
# each (sessions._open, launch.context_kwargs), so the one a check picks up
# can be named exactly:
# - emulation: ``viewport`` (window size and device scale), ``locale``,
#   ``timezone`` (browser.timezone; nothing when unset), ``color_scheme``,
#   ``service_workers`` (blocked);
# - ``state``: the agent's saved sign-ins;
# - guards: ``route_guard`` (every HTTP request through the egress check, as
#   the agent's browser has it; ``route_requests`` is the same), ``egress_proxy``
#   (every connection through the egress proxy, which is how the agent's
#   browser checks WebSockets now, browser/egress_proxy.py), ``page_guard``
#   (the DevTools redirect-hop interception on each page), and, for
#   comparison, ``route_websockets`` (Playwright's WebSocket routing, which
#   the agent's browser used to have: a page script and a binding);
# - page scripts: ``passkeys`` (refusal), ``copy_hook`` and ``permission_hook``
#   (the live view's, reporting over the console as the agent's browser does),
#   and, for comparison, ``copy_binding`` / ``permission_binding`` (the
#   bindings they used to report through), ``copy_script`` /
#   ``permission_script`` (the scripts alone), ``probe_binding`` (a binding
#   nothing uses); ``page_events`` (dialog and file request listeners).
CONTEXT_PARTS = (
    "viewport",
    "locale",
    "timezone",
    "color_scheme",
    "service_workers",
    "state",
    "route_guard",
    "route_requests",
    "route_websockets",
    "egress_proxy",
    "page_guard",
    "passkeys",
    "copy_hook",
    "copy_binding",
    "copy_script",
    "permission_hook",
    "permission_binding",
    "permission_script",
    "probe_binding",
    "page_events",
)
# Names for a group of the above.
PART_GROUPS = {
    "emulation": ("viewport", "locale", "timezone", "color_scheme", "service_workers"),
    "guards": ("route_guard", "egress_proxy", "page_guard"),
    "scripts": ("passkeys", "copy_hook", "permission_hook", "page_events"),
    # The agent's own setup: every piece it really has, none of the halves.
    "all": (
        "viewport",
        "locale",
        "timezone",
        "color_scheme",
        "service_workers",
        "state",
        "route_guard",
        "egress_proxy",
        "page_guard",
        "passkeys",
        "copy_hook",
        "permission_hook",
        "page_events",
    ),
}


def expand_parts(parts: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """*parts* with group names expanded, in :data:`CONTEXT_PARTS` order."""
    wanted: set[str] = set()
    for part in parts:
        if part in PART_GROUPS:
            wanted.update(PART_GROUPS[part])
        elif part in CONTEXT_PARTS:
            wanted.add(part)
        else:
            names = ", ".join([*CONTEXT_PARTS, *PART_GROUPS])
            raise ValueError(f"unknown part {part!r}; choose from {names}")
    return tuple(p for p in CONTEXT_PARTS if p in wanted)


SUFFIX = "-plain"
JPEG_QUALITY = 70
# Frames a second while the screen changes; an unchanged screen sends nothing.
FRAME_INTERVAL_S = 0.2

# The in-container side: standard library only, like browserd.py, and sent with
# each ``docker exec`` so the published image needs nothing new. It speaks the
# X11 wire protocol to the Xvfb display browserd started (no auth: Xvfb admits
# local clients of the same user, as it admits Chromium).
HELPER = r"""
import json, os, socket, struct, sys, urllib.request, zlib

DISPLAY = os.environ.get("OLANO_BROWSER_DISPLAY_NUM", "99")
CDP = "http://127.0.0.1:" + os.environ.get("OLANO_BROWSER_CDP_PORT", "9222")
SPECIAL = {
    "Enter": 0xFF0D, "Backspace": 0xFF08, "Tab": 0xFF09, "Escape": 0xFF1B,
    "Delete": 0xFFFF, "Insert": 0xFF63, "Home": 0xFF50, "End": 0xFF57,
    "PageUp": 0xFF55, "PageDown": 0xFF56, "ArrowLeft": 0xFF51, "ArrowUp": 0xFF52,
    "ArrowRight": 0xFF53, "ArrowDown": 0xFF54, "Shift": 0xFFE1, "Control": 0xFFE3,
    "Alt": 0xFFE9, "Meta": 0xFFEB, "CapsLock": 0xFFE5, " ": 0x20,
}
for _i in range(1, 13):
    SPECIAL["F%d" % _i] = 0xFFBD + _i


def recv(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise SystemExit("X display closed the connection")
        buf += chunk
    return buf


def pad(n):
    return (4 - n % 4) % 4


class X:
    def __init__(self):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect("/tmp/.X11-unix/X" + DISPLAY)
        self.s.sendall(struct.pack("<BxHHHHxx", ord("l"), 11, 0, 0, 0))
        head = recv(self.s, 8)
        extra = recv(self.s, struct.unpack("<H", head[6:8])[0] * 4)
        if head[0] != 1:
            raise SystemExit("X display refused: " + extra[: head[1]].decode("latin-1"))
        vlen = struct.unpack("<H", extra[16:18])[0]
        nformats = extra[21]
        self.min_key, self.max_key = extra[26], extra[27]
        off = 32 + vlen + pad(vlen) + 8 * nformats
        self.root = struct.unpack("<I", extra[off : off + 4])[0]
        self.width, self.height = struct.unpack("<HH", extra[off + 20 : off + 24])
        self.seq = 0
        self.xtest = self.extension(b"XTEST")
        self.keys = self.keymap()
        self.last_crc = None

    def request(self, data, reply):
        self.s.sendall(data)
        self.seq = (self.seq + 1) & 0xFFFF
        if not reply:
            return None
        while True:
            head = recv(self.s, 32)
            if head[0] == 1:
                more = struct.unpack("<I", head[4:8])[0] * 4
                return head + (recv(self.s, more) if more else b"")
            if head[0] == 0:
                code, seq = head[1], struct.unpack("<H", head[2:4])[0]
                if seq == self.seq:
                    raise RuntimeError("X error %d" % code)
                sys.stderr.write("X error %d on request %d\n" % (code, seq))

    def extension(self, name):
        req = struct.pack("<BxHHxx", 98, 2 + (len(name) + pad(len(name))) // 4, len(name))
        rep = self.request(req + name + b"\0" * pad(len(name)), True)
        if not rep[8]:
            raise SystemExit("the X display has no %s extension" % name.decode())
        return rep[9]

    def keymap(self):
        count = self.max_key - self.min_key + 1
        rep = self.request(struct.pack("<BxHBBxx", 101, 2, self.min_key, count), True)
        per = rep[1]
        syms = struct.unpack("<%dI" % (count * per), rep[32 : 32 + count * per * 4])
        found = {}
        for i in range(count):
            for col in range(min(per, 2)):
                sym = syms[i * per + col]
                if sym and sym not in found:
                    found[sym] = (self.min_key + i, col)
        return found

    def fake(self, kind, detail, x=0, y=0):
        self.request(
            struct.pack(
                "<BBHBBHIIIIhhIHBB",
                self.xtest, 2, 9, kind, detail, 0, 0, self.root if kind == 6 else 0,
                0, 0, x, y, 0, 0, 0, 0,
            ),
            False,
        )

    def frame(self):
        rep = self.request(
            struct.pack("<BBHIhhHHI", 73, 2, 5, self.root, 0, 0, self.width, self.height, 0xFFFFFFFF),
            True,
        )
        data = rep[32 : 32 + self.width * self.height * 4]
        crc = zlib.crc32(data)
        if crc == self.last_crc:
            return b""
        self.last_crc = crc
        return zlib.compress(data, 1)

    def keysym(self, key):
        if key in SPECIAL:
            return SPECIAL[key]
        if len(key) == 1:
            cp = ord(key)
            return cp if cp < 0x100 else 0x01000000 | cp
        return None

    def key(self, key, down):
        hit = self.keys.get(self.keysym(key) or -1)
        if hit:
            self.fake(2 if down else 3, hit[0])

    def type_text(self, text):
        shift = self.keys.get(0xFFE1)
        for ch in text:
            if ch == "\n":
                ch = "Enter"
            hit = self.keys.get(self.keysym(ch) or -1)
            if not hit:
                continue
            if hit[1] == 1 and shift:
                self.fake(2, shift[0])
            self.fake(2, hit[0])
            self.fake(3, hit[0])
            if hit[1] == 1 and shift:
                self.fake(3, shift[0])


def serve():
    x = X()
    out = sys.stdout.buffer
    for line in sys.stdin:
        try:
            cmd = json.loads(line)
        except ValueError:
            continue
        c = cmd.get("c")
        try:
            if c == "frame":
                data = x.frame()
                out.write(struct.pack(">IHH", len(data), x.width, x.height) + data)
                out.flush()
            elif c == "move":
                x.fake(6, 0, int(cmd["x"]), int(cmd["y"]))
            elif c == "button":
                x.fake(4 if cmd.get("down") else 5, int(cmd.get("b") or 1))
            elif c == "wheel":
                for axis, up, down in (("dy", 4, 5), ("dx", 6, 7)):
                    delta = float(cmd.get(axis) or 0)
                    for _ in range(min(10, max(1, round(abs(delta) / 60))) if delta else 0):
                        b = down if delta > 0 else up
                        x.fake(4, b)
                        x.fake(5, b)
            elif c == "key":
                x.key(str(cmd.get("key") or ""), bool(cmd.get("down")))
            elif c == "text":
                x.type_text(str(cmd.get("text") or "")[:2000])
        except (RuntimeError, KeyError, ValueError, TypeError) as exc:
            sys.stderr.write("input failed: %s\n" % exc)


def http(method, path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(CDP + path, method=method)
    with opener.open(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    cmd = sys.argv[1]
    if cmd == "serve":
        serve()
    elif cmd == "open":
        print(json.dumps(http("PUT", "/json/new?" + sys.argv[2])))
    elif cmd == "tabs":
        tabs = [
            {"id": t.get("id", ""), "title": t.get("title", ""), "url": t.get("url", "")}
            for t in http("GET", "/json/list")
            if t.get("type") == "page"
        ]
        print(json.dumps(tabs))
    elif cmd == "exit":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("https://www.cloudflare.com/cdn-cgi/trace", timeout=15) as resp:
            text = resp.read().decode("utf-8", "replace")
        print(json.dumps(dict(l.split("=", 1) for l in text.splitlines() if "=" in l)))


main()
"""


def container_name(agent: str) -> str:
    return sidecar.container_name(agent) + SUFFIX


def helper_argv(container: str, *args: str) -> list[str]:
    return ["docker", "exec", "-i", container, "python3", "-c", HELPER, *args]


@dataclass
class PlainTest:
    agent: str
    container: str
    variant: str
    url: str
    started_at: float = field(default_factory=time.time)
    # attached / devtools-input: the DevTools client on the browser loop.
    browser: Any = None
    relay: Any = None
    page: Any = None
    # The tab ``/json/new`` opened (its DevTools target id).
    target_id: str = ""
    # Where the page's viewport sits on the display (devtools-input maps there).
    offset: tuple[int, int] = (0, 0)
    # context: which of CONTEXT_PARTS were added.
    parts: tuple[str, ...] = ()
    # The egress proxy the ``egress_proxy`` part runs in the container.
    egress: Any = None
    # The second page, and how its load went: "" (none), "waiting", "done",
    # or the error. ``first_page`` is the first page as it stood just before.
    then_url: str = ""
    then_after_s: float = 0.0
    then_state: str = ""
    first_page: dict[str, Any] | None = None
    then_task: Any = None

    def public(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "container": self.container,
            "variant": self.variant,
            "parts": list(self.parts),
            "url": self.url,
            "then_url": self.then_url,
            "then_after_s": self.then_after_s,
            "then_state": self.then_state,
            "first_page": self.first_page,
            "started_at": self.started_at,
            "attached": self.browser is not None,
        }


_tests: dict[str, PlainTest] = {}


def get(agent: str) -> PlainTest | None:
    return _tests.get(agent)


def _helper(container: str, *args: str, timeout: float = 30) -> Any:
    proc = sidecar._run(helper_argv(container, *args), timeout=timeout)
    if proc.returncode != 0:
        # The last line of a traceback is the error; the rest is noise here.
        lines = (proc.stderr or proc.stdout or "no output").strip().splitlines()
        raise RuntimeError(lines[-1][-400:] if lines else "no output")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _start_container(agent: str) -> str:
    """Start the plain container (blocking). Same recipe as the agent's
    browser, under another name, never proxied: a page opened with no
    DevTools client has no per-context proxy, so it exits from this machine."""
    if not sidecar.docker_available():
        raise RuntimeError("The plain browser test needs Docker, and this machine has none.")
    if not sidecar.image_present():
        raise RuntimeError("The live browser image is not installed on this machine yet.")
    name = container_name(agent)
    sidecar._remove(name)
    args = sidecar.run_args(agent)
    args[args.index("--name") + 1] = name
    proc = sidecar._run(args, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(
            "Could not start the plain browser: "
            + (proc.stderr or proc.stdout or "").strip()[-300:]
        )
    sidecar.devtools_version(name)
    return name


async def _attach(test: PlainTest) -> None:
    """Attach Playwright to the plain browser before the tab opens (on the
    browser loop), so the page loads with a DevTools client on it."""
    from olano.browser import sessions

    version = await asyncio.to_thread(sidecar.devtools_version, test.container)
    relay = sidecar.ExecPipeRelay(sidecar.pipe_argv(test.container))
    await relay.start()
    pw = await sessions._playwright()
    test.browser = await pw.chromium.connect_over_cdp(
        relay.ws_url(str(version.get("webSocketDebuggerUrl", ""))),
        headers=relay.headers(),
        timeout=30_000,
    )
    test.relay = relay


async def _find_page(test: PlainTest) -> None:
    """The attached client's handle on the tab ``/json/new`` opened."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        for context in test.browser.contexts:
            for page in context.pages:
                cdp = await context.new_cdp_session(page)
                try:
                    info = await cdp.send("Target.getTargetInfo")
                finally:
                    await cdp.detach()
                if info.get("targetInfo", {}).get("targetId") != test.target_id:
                    continue
                test.page = page
                try:
                    # The tab just opened is in front; a background one would
                    # report its outer size as its inner one.
                    dims = await page.evaluate(
                        "[outerWidth - innerWidth, outerHeight - innerHeight, screenX, screenY]"
                    )
                    test.offset = (int(dims[2] + dims[0] / 2), int(dims[3] + dims[1]))
                except Exception:
                    logger.debug("plain check: no window size", exc_info=True)
                return
        await asyncio.sleep(0.3)


async def _open_in_context(test: PlainTest) -> None:
    """The ``context`` variant: a context of its own, as the agent's, with
    the parts of the agent's setup the test asked for, and the page in it."""
    from types import SimpleNamespace

    from olano.browser import egress_proxy, launch, live, policy, sessions
    from olano.browser.guard import (
        GuardPolicy,
        GuardState,
        guard_page,
        install_guards,
        private_network_allowed,
    )
    from olano.browser.live import arm_page_copies, arm_page_for_people, arm_page_permissions
    from olano.browser.passkeys import refuse_passkeys

    parts = set(test.parts)
    timezone = await asyncio.to_thread(lambda: policy.browser_availability().timezone)
    agent_kwargs = launch.context_kwargs(headless=False, timezone=timezone)
    # A missing key is the browser's own default; Playwright's own default
    # viewport (1280x720) is turned off unless the agent's is asked for.
    kwargs: dict[str, Any] = {"no_viewport": True}
    if "viewport" in parts:
        kwargs = {k: agent_kwargs[k] for k in ("viewport", "device_scale_factor")}
    for part, key in (
        ("locale", "locale"),
        ("timezone", "timezone_id"),
        ("color_scheme", "color_scheme"),
        ("service_workers", "service_workers"),
    ):
        if part in parts and key in agent_kwargs:
            kwargs[key] = agent_kwargs[key]
    if "state" in parts:
        kwargs["storage_state"] = sessions._load_state(test.agent)
    state = GuardState(policy=GuardPolicy(block_private=not private_network_allowed()))
    if "egress_proxy" in parts:
        test.egress = await egress_proxy.start_in_container(test.container, state)
        kwargs["proxy"] = test.egress.playwright_proxy()
    context = await test.browser.new_context(**kwargs)
    if parts & {"route_guard", "route_requests", "route_websockets"}:
        await install_guards(
            context,
            state,
            requests=bool(parts & {"route_guard", "route_requests"}),
            websockets="route_websockets" in parts,
        )
    # The hooks' callbacks act only for a person in control; nobody is.
    idle = SimpleNamespace(
        agent=test.agent,
        controller="agent",
        viewers=0,
        clipboard_listeners=set(),
        notice_listeners=set(),
        dialog=None,
        chooser=None,
    )
    if "passkeys" in parts:
        await refuse_passkeys(context)
    if "copy_hook" in parts:
        await arm_page_copies(context, idle)
    if "permission_hook" in parts:
        await arm_page_permissions(context, idle)
    for part, binding, script in (
        ("copy", "__olanoPageCopy", live.PAGE_COPY_JS),
        ("permission", "__olanoPermission", live.PERMISSION_JS),
    ):
        if f"{part}_binding" in parts and f"{part}_hook" not in parts:
            await context.expose_binding(binding, lambda source, *args: None)
        if f"{part}_script" in parts and f"{part}_hook" not in parts:
            await context.add_init_script(script)
    if "probe_binding" in parts:
        await context.expose_binding("__olanoProbe", lambda source, *args: None)
    page = await context.new_page()
    if "page_events" in parts:
        arm_page_for_people(idle, page)
    if "page_guard" in parts:
        await guard_page(context, page, state)
    cdp = await context.new_cdp_session(page)
    try:
        info = await cdp.send("Target.getTargetInfo")
    finally:
        await cdp.detach()
    test.target_id = str(info.get("targetInfo", {}).get("targetId") or "")
    test.page = page
    try:
        await page.goto(test.url, wait_until="domcontentloaded", timeout=30_000)
    except Exception:
        logger.debug("plain check: context page load", exc_info=True)


async def _goto(test: PlainTest, url: str) -> None:
    try:
        await test.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception:
        logger.debug("plain check: second page load", exc_info=True)


async def _then(test: PlainTest) -> None:
    """Load the second page in the same browser, *then_after_s* after the first."""
    from olano.browser import loop as browser_loop

    test.then_state = "waiting"
    await asyncio.sleep(test.then_after_s)
    try:
        tabs = await asyncio.to_thread(_helper, test.container, "tabs")
        test.first_page = next((t for t in tabs if t.get("id") == test.target_id), None)
        if test.variant == "context":
            await browser_loop.run_async(_goto(test, test.then_url), timeout_s=60)
        else:
            opened = await asyncio.to_thread(_helper, test.container, "open", test.then_url)
            test.target_id = str(opened.get("id") or "")
            if test.variant != "plain":
                await browser_loop.run_async(_find_page(test), timeout_s=30)
        test.then_state = "done"
    except Exception as exc:
        test.then_state = f"error: {exc}"


def _web_url(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    if not url.startswith(("http://", "https://")):
        raise ValueError("Only http and https addresses can be opened.")
    return url


async def start(
    agent: str,
    url: str,
    variant: str,
    parts: list[str] | tuple[str, ...] = (),
    *,
    then_url: str = "",
    then_after_s: float = 20.0,
) -> PlainTest:
    """Open *url* in a fresh plain browser for *agent* (any loop), and
    *then_url* in the same browser *then_after_s* later when given."""
    from olano.browser import loop as browser_loop

    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {', '.join(VARIANTS)}")
    chosen = expand_parts(parts)
    url = _web_url(url)
    then_url = _web_url(then_url) if then_url.strip() else ""
    await stop(agent)
    container = await asyncio.to_thread(_start_container, agent)
    test = PlainTest(
        agent=agent,
        container=container,
        variant=variant,
        url=url,
        parts=chosen if variant == "context" else (),
        then_url=then_url,
        then_after_s=max(0.0, min(300.0, float(then_after_s))),
    )
    _tests[agent] = test
    try:
        if variant != "plain":
            await browser_loop.run_async(_attach(test), timeout_s=60)
        if variant == "context":
            await browser_loop.run_async(_open_in_context(test), timeout_s=60)
        else:
            opened = await asyncio.to_thread(_helper, container, "open", url)
            test.target_id = str(opened.get("id") or "")
            if variant != "plain":
                await browser_loop.run_async(_find_page(test), timeout_s=30)
    except Exception:
        await stop(agent)
        raise
    if then_url:
        test.then_task = asyncio.get_running_loop().create_task(_then(test))
    return test


async def _detach(test: PlainTest) -> None:
    if test.browser is not None:
        try:
            await test.browser.close()
        except Exception:
            pass
    if test.relay is not None:
        await test.relay.close()
    if test.egress is not None:
        await test.egress.close()
    test.browser = test.relay = test.page = test.egress = None


async def stop(agent: str) -> bool:
    from olano.browser import loop as browser_loop

    test = _tests.pop(agent, None)
    if test is not None and test.then_task is not None:
        test.then_task.cancel()
    if test is None:
        await asyncio.to_thread(sidecar._remove, container_name(agent))
        return False
    if test.browser is not None:
        try:
            await browser_loop.run_async(_detach(test), timeout_s=30)
        except Exception:
            logger.debug("plain check: detach failed", exc_info=True)
    await asyncio.to_thread(sidecar._remove, test.container)
    return True


def status(agent: str) -> dict[str, Any] | None:
    """What the plain browser shows now (blocking): each tab's title and
    address, read without attaching, and the exit address."""
    test = _tests.get(agent)
    if test is None:
        return None
    out: dict[str, Any] = test.public()
    try:
        out["tabs"] = _helper(test.container, "tabs")
        # The tab the test opened (a page the person opened from it is listed too).
        out["page"] = next((t for t in out["tabs"] if t.get("id") == test.target_id), None)
    except Exception as exc:
        out["tabs_error"] = str(exc)
    try:
        trace = _helper(test.container, "exit")
        out["exit"] = {"ip": trace.get("ip", ""), "colo": trace.get("colo", "")}
    except Exception as exc:
        out["exit_error"] = str(exc)
    return out


class Screen:
    """The plain browser's display for one viewer: frames out, input in.

    One ``docker exec`` running :data:`HELPER` ``serve``. Frames are the whole
    virtual display (browser UI included) as JPEG; input coordinates arrive
    normalised to the frame. Runs on the caller's loop.
    """

    def __init__(self, test: PlainTest) -> None:
        self.test = test
        self.proc: asyncio.subprocess.Process | None = None
        self.width = 0
        self.height = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *helper_argv(self.test.container, "serve"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _send(self, cmd: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(cmd) + "\n").encode())
        await self.proc.stdin.drain()

    async def frame(self) -> bytes | None:
        """A JPEG of the display, or ``None`` when nothing changed."""
        assert self.proc is not None and self.proc.stdout is not None
        async with self._lock:
            await self._send({"c": "frame"})
            head = await self.proc.stdout.readexactly(8)
            length, width, height = struct.unpack(">IHH", head)
            data = await self.proc.stdout.readexactly(length) if length else b""
        self.width, self.height = width, height
        if not data:
            return None
        return await asyncio.to_thread(_jpeg, data, width, height)

    def _xy(self, event: dict[str, Any]) -> tuple[int, int]:
        x = min(max(float(event.get("x", 0)), 0.0), 1.0) * max(self.width - 1, 0)
        y = min(max(float(event.get("y", 0)), 0.0), 1.0) * max(self.height - 1, 0)
        return round(x), round(y)

    async def input(self, event: dict[str, Any]) -> str | None:
        """Replay one viewer event: on the display, or through DevTools for
        the ``devtools-input`` variant."""
        kind = str(event.get("t") or "")
        if self.test.variant == "devtools-input":
            return await self._devtools_input(kind, event)
        button = {"left": 1, "middle": 2, "right": 3}.get(str(event.get("button") or "left"), 1)
        async with self._lock:
            if kind in ("move", "down", "up"):
                x, y = self._xy(event)
                await self._send({"c": "move", "x": x, "y": y})
                if kind != "move":
                    await self._send({"c": "button", "b": button, "down": kind == "down"})
            elif kind == "wheel":
                await self._send({"c": "wheel", "dx": event.get("dx"), "dy": event.get("dy")})
            elif kind in ("keydown", "keyup"):
                await self._send({"c": "key", "key": event.get("key"), "down": kind == "keydown"})
            elif kind == "text":
                await self._send({"c": "text", "text": event.get("text")})
            else:
                return f"Unknown input event {kind!r}."
        return None

    async def _devtools_input(self, kind: str, event: dict[str, Any]) -> str | None:
        from olano.browser import loop as browser_loop

        page = self.test.page
        if page is None:
            return "The attached client has no handle on the tab."
        x, y = self._xy(event)
        px, py = x - self.test.offset[0], y - self.test.offset[1]
        button = str(event.get("button") or "left")
        if button not in ("left", "middle", "right"):
            button = "left"

        async def _act() -> None:
            if kind == "move":
                await page.mouse.move(px, py)
            elif kind == "down":
                await page.mouse.move(px, py)
                await page.mouse.down(button=button)
            elif kind == "up":
                await page.mouse.move(px, py)
                await page.mouse.up(button=button)
            elif kind == "wheel":
                await page.mouse.wheel(float(event.get("dx") or 0), float(event.get("dy") or 0))
            elif kind == "keydown":
                await page.keyboard.down(str(event.get("key") or ""))
            elif kind == "keyup":
                await page.keyboard.up(str(event.get("key") or ""))
            elif kind == "text":
                await page.keyboard.insert_text(str(event.get("text") or "")[:2000])

        if py < 0:
            return None  # the browser's own UI: DevTools input cannot reach it
        try:
            await browser_loop.run_async(_act(), timeout_s=15)
        except Exception as exc:
            if "Unknown key" in str(exc):
                return None
            return f"Input failed: {exc}"
        return None

    async def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 5)
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


def _jpeg(raw: bytes, width: int, height: int) -> bytes:
    from PIL import Image

    image = Image.frombuffer("RGB", (width, height), zlib.decompress(raw), "raw", "BGRX", 0, 1)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()
