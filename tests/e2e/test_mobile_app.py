"""The Olano mobile app (``mobile/``), end to end, against a real runtime.

The app's screens are a bundled Ionic app that talks to the runtime's API.
On a phone the OlanoHttp native plugin carries the requests; in a browser the
same screens use fetch() on their own origin, which the app's Vite dev server
proxies to the runtime (``mobile/vite.config.ts``). So this test runs the real
screens against the real API with nothing faked but the agents' model, in an
iPhone-sized Chromium with an iPhone user agent (Ionic then draws its iOS
style), and walks what a person does:

* connect with an address, land on Chats with every agent listed;
* open an agent, open a past conversation (its tool steps folded into one
  "1 step" line), go back, start a new one and watch the reply stream in with
  the step it took;
* Cortex shows the agents' items and one item's write-up, Inbox shows the
  deployment's alerts, Settings shows where the app is connected;
* **the app going to the background lets go of the turn's stream, the turn
  finishes with nobody watching, the runtime sends the phone a push**, and
  coming back shows the reply;
* photos: pick five, remove one, send four with a caption; the runtime saves
  them (scaled down, as JPEG) in the agent's workspace and tells the agent
  where; the viewer opens on the tapped photo; and a fresh screen shows the
  photos from the deployment with the caption alone, not the runtime's notes.

Set ``OLANO_MOBILE_SHOTS=<dir>`` to keep a screenshot of every screen.
"""

from __future__ import annotations

import array
import asyncio
import json
import math
import os
import re
import signal
import socket
import subprocess
import threading
import time
import wave
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e import harness

pytestmark = pytest.mark.e2e

MOBILE_DIR = Path(__file__).resolve().parents[2] / "mobile"
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1"
)
# An iPhone's status bar and home indicator: a desktop browser reports no
# safe-area insets, which hid a composer sitting under the home indicator.
# Ionic reads these variables before env(safe-area-inset-*).
IPHONE_INSETS = """
document.addEventListener("DOMContentLoaded", () => {
  const style = document.createElement("style");
  style.textContent = "html { --safe-area-inset-top: 59px; --safe-area-inset-bottom: 34px; }";
  document.head.appendChild(style);
});
"""
PUSH_TOKEN = "a" * 64
APP_CONFIG_URL = "https://cloud.olano.ai/api/v1/mobile/config"
REPLY_TEXT = "Three suppliers ship within a week; Meridian is cheapest at 4.10 a unit."
SLOW_FIRST = "Working on the forecast token HJKLQ"
SLOW_REST = "and the forecast is done token ZXCVB"


def _persist(agent: str, thread_id: str, row: dict[str, Any]) -> None:
    home = os.environ.get("LIONCLAW_HOME")
    if not home or not thread_id:
        return
    sessions = Path(home) / "sessions" / agent
    sessions.mkdir(parents=True, exist_ok=True)
    # The channel the real runner stamps (runner._append_session): MOBILE for
    # a turn the app sent, the dashboard otherwise.
    from olano.core.thread_labels import channel_for_request
    from olano.security.request_context import get_request_context

    channel = channel_for_request(get_request_context()) or "dashboard"
    row = {"ts": datetime.now(timezone.utc).isoformat(), "channel": channel, **row}
    with (sessions / f"{thread_id}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


class _MobileRunner:
    """A turn shaped like a real one: a tool call, its result, a streamed reply.

    With ``slow`` the reply comes in two halves seconds apart, long enough for
    the test to put the app in the background in between.
    """

    # A message carrying this fails the first time it is sent, for Try again.
    FAIL_ONCE = "[fail once]"
    _failed: set[str] = set()

    def __init__(self, config: Any, slow: bool = False) -> None:
        self.config = config
        self.slow = slow

    async def ainvoke(self, message: Any, thread_id: str | None = None, **_: Any) -> str:
        return REPLY_TEXT

    # The model and reasoning level each turn was asked to run on.
    overrides: list[tuple[str | None, str | None]] = []
    # Every message a turn was started with, in order, and the chat mode it ran in.
    messages: list[str] = []
    modes: list[str | None] = []
    # Threads with a turn running, and the asides sent to them (POST /api/chat/steer).
    _running: set[str] = set()
    _steers: dict[str, list[Any]] = {}

    def steer(self, thread_id: str, text: str, *, steer_id: str | None = None, **_: Any) -> Any:
        """The real runner hands an aside to the turn at its next step; this one
        reads it between the two halves of a slow reply."""
        from types import SimpleNamespace

        if thread_id not in self._running:
            return None
        aside = SimpleNamespace(id=steer_id or f"s{len(self._steers)}", text=text, turn_id="t1")
        self._steers.setdefault(thread_id, []).append(aside)
        return aside

    def cancel_steer(self, thread_id: str, steer_id: str) -> str:
        pending = self._steers.get(thread_id, [])
        for aside in pending:
            if aside.id == steer_id:
                pending.remove(aside)
                return "cancelled"
        return "unknown"

    async def astream_events(
        self,
        message: Any,
        thread_id: str | None = None,
        override_model: str | None = None,
        override_reasoning_effort: str | None = None,
        session_mode: str | None = None,
        **_: Any,
    ):
        name = self.config.name
        self.modes.append(session_mode)
        self.overrides.append((override_model, override_reasoning_effort))
        # The real runner stamps every reply with the model it ran on.
        stamp = {"model": override_model or self.config.model}
        if override_reasoning_effort:
            stamp["reasoning_effort"] = override_reasoning_effort
        _persist(name, thread_id or "", {"role": "user", "content": str(message)})
        text = str(message)
        self.messages.append(text)
        if self.FAIL_ONCE in text and text not in self._failed:
            self._failed.add(text)
            raise RuntimeError("The model provider is unavailable right now.")
        if self.slow:
            key = thread_id or ""
            self._running.add(key)
            try:
                for word in SLOW_FIRST.split(" "):
                    yield ("text", word + " ")
                await asyncio.sleep(6.0)
                for aside in self._steers.pop(key, []):
                    _persist(name, key, {"role": "user", "content": aside.text, "steer": True})
                    yield ("steer", {"id": aside.id, "text": aside.text, "sender": ""})
                for word in SLOW_REST.split(" "):
                    yield ("text", word + " ")
                _persist(name, key, {"role": "assistant", "content": f"{SLOW_FIRST} {SLOW_REST}"})
            finally:
                self._running.discard(key)
            return
        args = json.dumps({"query": "fastest suppliers"})
        yield ("tool_call", {"id": "call_1", "name": "web_search", "args": args})
        await asyncio.sleep(0.8)
        yield ("tool_result", {"id": "call_1", "name": "web_search", "content": "3 results"})
        for word in REPLY_TEXT.split(" "):
            await asyncio.sleep(0.03)
            yield ("text", word + " ")
        _persist(
            name,
            thread_id or "",
            {"role": "tool_call", "content": f"web_search: {args}", "tool_call_id": "call_1"},
        )
        _persist(
            name,
            thread_id or "",
            {"role": "tool_result", "content": "web_search: 3 results", "tool_call_id": "call_1"},
        )
        _persist(name, thread_id or "", {"role": "assistant", "content": REPLY_TEXT, **stamp})


AGENTS = [
    ("nova", "🔭", "Research Analyst", "Researches suppliers, markets and competitors."),
    ("aria", "💬", "Customer Support", "Answers customers on WhatsApp and email."),
    ("atlas", "🧭", "Operations Lead", "Keeps the team's work moving."),
]


def _build_gateway() -> Any:
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    runners: dict[str, Any] = {}
    for name, emoji, title, description in AGENTS:
        cfg = AgentConfig(
            name=name,
            model="anthropic:claude-haiku-4-5-20251001",
            backend=FilesystemBackendConfig(root_dir="workspace"),
        )
        cfg.emoji = emoji
        cfg.description = description
        cfg.channels = []
        runners[name] = _MobileRunner(cfg, slow=name == "atlas")
    return harness._gateway_for(runners)


def _seed(home: Path) -> None:
    """A past conversation with nova, a Cortex item, and an alert."""
    now = datetime.now(timezone.utc)

    def ts(minutes: float) -> str:
        return (now - timedelta(minutes=minutes)).isoformat()

    rows = [
        {
            "ts": ts(30),
            "role": "user",
            "content": "Compare our three packaging suppliers on price and lead time.",
            "channel": "dashboard",
        },
        {
            "ts": ts(29),
            "role": "tool_call",
            "content": 'read_file: {"file_path": "reports/suppliers.md"}',
            "tool_call_id": "c1",
        },
        {"ts": ts(29), "role": "tool_result", "content": "read_file: ...", "tool_call_id": "c1"},
        {
            "ts": ts(28),
            "role": "assistant",
            "content": "**Meridian** is the cheapest at 4.10 a unit, with a 5 day lead time.\n\n"
            "| Supplier | Unit | Lead time |\n|---|---|---|\n| Meridian | 4.10 | 5 days |\n| Boxwell | 4.45 | 3 days |",
            "channel": "dashboard",
        },
    ]
    sessions = home / "sessions" / "nova"
    sessions.mkdir(parents=True, exist_ok=True)
    with (sessions / "supplier-comparison.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    from olano.cognition import cortex_items

    workspace = home / "agents" / "nova" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    item = cortex_items.add_item(
        workspace,
        kind="idea",
        title="Bundle shipping with the spring order",
        summary="Combining the two spring orders saves about 9% on freight.",
        detail="## Why now\n\nBoth suppliers ship from the same port in March.",
    )
    cortex_items.set_rating(
        workspace, item["id"], 8, rationale="Clear savings, low effort.", recommendation="promote"
    )

    from olano.runtime import notifications

    notifications.notify(
        kind="channel_credential_missing",
        level="warn",
        title="aria's WhatsApp is not connected",
        body="Scan the QR code in the dashboard to reconnect.",
        agent="aria",
    )


class _Relay:
    """A stand-in for Olano Cloud's POST /api/v1/push."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                relay.bodies.append(body)
                sends = sum(len(n.get("devices", [])) for n in body.get("notifications", []))
                payload = json.dumps({"sent": sends, "failed": 0, "invalid_tokens": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args) -> None:
                pass

        with closing(socket.socket()) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def notifications(self, kind: str) -> list[dict]:
        out = [n for b in self.bodies for n in b.get("notifications", [])]
        return [n for n in out if n.get("data", {}).get("kind") == kind]


@dataclass
class MobileApp:
    base_url: str
    runtime_url: str
    relay: _Relay
    gateway: Any
    loop_box: list

    def push_event(self, event_type: str, data: dict) -> None:
        """A live ``/api/stream`` frame, published on the runtime's own loop
        (its subscriber queues are not thread-safe)."""
        from olano.runtime.api import _push_event

        self.loop_box[0].call_soon_threadsafe(_push_event, event_type, data)


def _mobile_vite(runtime_url: str, out_dir: Path) -> Iterator[str]:
    """The built app, served by ``vite preview`` with /api proxied to the runtime.

    The production bundle, not the dev server: it is what ships in the native
    projects, and the two differ in ways a phone shows (the order Ionic's own
    styles land in against the app's, which once left the message box's text
    against its edge on an iPhone while the dev server looked right). Built
    with an absolute base so a test can open a deep link directly; the native
    projects load the app from its root, where both bases resolve the same.
    """
    port = harness._free_port()
    env = {
        **os.environ,
        "OLANO_DEV_RUNTIME": runtime_url,
        "OLANO_DEV_PORT": str(port),
        "BROWSER": "none",
    }
    subprocess.run(
        ["npx", "vite", "build", "--base", "/", "--outDir", str(out_dir), "--emptyOutDir"],
        cwd=str(MOBILE_DIR),
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
        timeout=300,
    )
    proc = subprocess.Popen(
        ["npx", "vite", "preview", "--host", "127.0.0.1", "--outDir", str(out_dir)],
        cwd=str(MOBILE_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        harness._wait_until(
            lambda: httpx.get(base_url, timeout=2.0).status_code == 200,
            90,
            "the mobile app's preview server",
        )
        yield base_url
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.fixture(scope="module")
def mobile_app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[MobileApp]:
    if not harness.npm_available():
        pytest.skip("npm not on PATH, so the mobile app cannot be served")
    if not (MOBILE_DIR / "node_modules").is_dir():
        pytest.skip("mobile node_modules missing: run `cd mobile && npm install`")
    from contextlib import contextmanager

    from olano.push.dispatcher import PushDispatcher, set_push_dispatcher

    relay = _Relay()
    gateway = _build_gateway()
    loop_box: list = []
    try:
        with (
            harness._runtime_server(gateway, loop_box=loop_box) as runtime_url,
            contextmanager(_mobile_vite)(runtime_url, tmp_path_factory.mktemp("www")) as base_url,
        ):
            # A deployment token in the environment would make this box
            # "managed"; hand the dispatcher its cloud channel directly.
            dispatcher = PushDispatcher(cloud=lambda: (relay.url, "olano_dep_e2e"))
            dispatcher.start()
            set_push_dispatcher(dispatcher)
            yield MobileApp(
                base_url=base_url,
                runtime_url=runtime_url,
                relay=relay,
                gateway=gateway,
                loop_box=loop_box,
            )
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"the mobile app did not come up: {exc}")
    finally:
        set_push_dispatcher(None)
        relay.server.shutdown()


@pytest.fixture(autouse=True)
def _mobile_home(mobile_app: MobileApp) -> Iterator[None]:
    """Seed THIS test's home: ``tests/conftest.py`` gives every test a fresh
    ``LIONCLAW_HOME`` and the in-process runtime follows it (see
    ``harness.seed_agent_configs``). The agents' configs are written here rather
    than through ``harness._capture_seed_entries``, which would replace the
    shared dashboard's agents for every later test in the session."""
    from olano.runtime import notifications

    home = Path(os.environ["LIONCLAW_HOME"])
    for name, runner in mobile_app.gateway._runners.items():
        entry = {
            "name": name,
            "model": runner.config.model,
            "description": runner.config.description,
            "emoji": runner.config.emoji,
            "backend": {"type": "filesystem", "root_dir": "workspace"},
        }
        path = home / "agents" / name / "agent.json5"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        runner.config.workspace = str(home / "agents" / name / "workspace")
    harness.seed_onboarding_complete(str(home))
    notifications.reset_for_tests()
    _seed(home)
    yield
    # The notification store is one object for the whole process, shared with
    # the dashboard's tests: what a test here raised must not reach theirs.
    notifications.reset_for_tests()


@pytest.fixture
def phone(mobile_app: MobileApp) -> Iterator[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - environment dependent
        pytest.skip("playwright not installed")
    with sync_playwright() as pw:
        try:
            # A fake microphone (a steady tone), allowed without a prompt.
            browser = pw.chromium.launch(
                headless=True,
                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"],
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        context = browser.new_context(
            viewport={"width": 393, "height": 852},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            user_agent=IPHONE_UA,
        )
        context.grant_permissions(["microphone"], origin=mobile_app.base_url)
        context.add_init_script(IPHONE_INSETS)
        # The app's remote config (mobile/src/appConfig.ts) comes from Olano
        # Cloud; no test reaches the internet for it. The app then draws from
        # its bundled copy, and a test that needs a served one routes the URL
        # on its page (a page route wins over this one).
        context.route(APP_CONFIG_URL, lambda route: route.abort())
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(mobile_app.base_url, wait_until="domcontentloaded")
        try:
            yield page
            assert not errors, f"the app threw: {errors}"
        finally:
            context.close()
            browser.close()


def _shot(page: Any, name: str) -> None:
    folder = os.environ.get("OLANO_MOBILE_SHOTS")
    if not folder:
        return
    Path(folder).mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(600)  # let Ionic's page transition settle
    page.screenshot(path=str(Path(folder) / f"{name}.png"))


def _active(page: Any) -> Any:
    """The screen Ionic is showing. Earlier pages of a stack stay in the DOM
    (hidden), and so do the other tabs' stacks, so "visible" is the test."""
    return page.locator(".ion-page:not(.ion-page-hidden):visible").last


def _thread_row(page: Any, text: str) -> Any:
    """A conversation in an agent's list of conversations. The agents list shows
    each agent's latest conversation as its preview line, in the same words, and
    stays on screen while the next one slides in; only the conversations list
    has thread titles, so the preview can never be the match."""
    return _active(page).locator(".olano-thread-title", has_text=text)


def _open_agent(page: Any, agent: str) -> None:
    """Tap an agent in Chats: its last conversation, or a new one, opens.

    Waits for the Chats list to be the screen first: right after a Back, the
    conversation sliding away still counts as visible, its title carries the
    agent's name too, and a tap on it (then its menu, as it detaches) reaches
    nothing."""
    page.wait_for_url(re.compile(r"/chats/?$"))
    _active(page).get_by_role("button", name="Conversation menu").wait_for(state="hidden")
    _active(page).get_by_text(agent, exact=True).click()
    page.wait_for_url(re.compile(rf"/chats/{re.escape(agent)}/[^/]+$"))
    _active(page).get_by_role("button", name="Conversation menu").wait_for(timeout=20000)


def _open_threads(page: Any, agent: str) -> None:
    """An agent's whole thread list: its conversation's menu, All Conversations."""
    _open_agent(page, agent)
    _active(page).get_by_role("button", name="Conversation menu").click()
    _menu_choice(page, "All Conversations")
    _active(page).locator("ion-title", has_text="Conversations").wait_for()


def _new_conversation(page: Any, agent: str) -> None:
    """A new conversation with the agent, from its last one's menu if it has one."""
    _open_agent(page, agent)
    if not page.url.endswith("/new"):
        _active(page).get_by_role("button", name="Conversation menu").click()
        _menu_choice(page, "New Conversation")
        page.wait_for_url(re.compile(r"/new$"))
    _active(page).locator("ion-textarea textarea").wait_for()


def _connect(page: Any, mobile_app: MobileApp) -> None:
    page.get_by_text("Connect to your Olano").wait_for()
    _shot(page, "01-connect")
    page.locator("ion-input input").fill(mobile_app.base_url)
    page.get_by_role("button", name="Continue").click()
    _active(page).get_by_text("nova", exact=True).wait_for(timeout=20000)


def test_chat_cortex_inbox_settings(phone: Any, mobile_app: MobileApp) -> None:
    page = phone
    _connect(page, mobile_app)
    # Ionic picked its iOS style from the user agent.
    assert page.evaluate("document.documentElement.classList.contains('ios')")
    for name, *_ in AGENTS:
        _active(page).get_by_text(name, exact=True).wait_for()
    _shot(page, "02-chats")

    # Tapping an agent reopens the last conversation you had with it; the tool
    # rows fold into one "1 step" line above the reply.
    _open_agent(page, "nova")
    conversation = _active(page)
    conversation.get_by_text("is the cheapest at 4.10").wait_for()
    conversation.get_by_text("1 step").wait_for()
    assert page.locator("ion-tab-bar").is_hidden(), "a conversation gets the whole screen"
    _shot(page, "04-conversation")

    # A new conversation from its menu, with a streamed reply.
    conversation.get_by_role("button", name="Conversation menu").click()
    _menu_choice(page, "New Conversation")
    composer = _active(page).locator("ion-textarea textarea")
    composer.wait_for()
    composer.fill("Which supplier ships fastest?")
    _active(page).get_by_role("button", name="Send").click()
    # The sent message, not the message box, which can hold the words a moment longer.
    _active(page).locator(".olano-bubble.user", has_text="Which supplier ships fastest?").wait_for()
    _active(page).get_by_text("Searching the web").or_(
        _active(page).get_by_text("1 step")
    ).first.wait_for(timeout=15000)
    _active(page).get_by_text("Meridian is cheapest at 4.10 a unit.").wait_for(timeout=20000)
    # The stop button is gone: the turn ended, and the empty composer offers the microphone.
    _active(page).get_by_role("button", name="Record voice note").wait_for()
    _shot(page, "05-new-reply")

    # Every thread is one step away, in the menu: the new one is labelled
    # MOBILE, the old one DASHBOARD, and the chips filter by source.
    _active(page).get_by_role("button", name="Conversation menu").click()
    _menu_choice(page, "All Conversations")
    new_row = (
        _active(page)
        .locator("ion-item")
        .filter(has=page.locator(".olano-thread-title", has_text="Which supplier ships fastest?"))
    )
    new_row.locator(".olano-source-mobile", has_text="Mobile").wait_for(timeout=15000)
    _thread_row(page, "Compare our three packaging suppliers").wait_for()
    _shot(page, "03-threads")
    _active(page).locator(".olano-thread-filters ion-chip", has_text="Mobile").click()
    _thread_row(page, "Compare our three packaging suppliers").wait_for(state="hidden")
    _thread_row(page, "Which supplier ships fastest?").wait_for()
    _active(page).locator(".olano-thread-filters ion-chip", has_text="All").click()

    # Cortex: the agents' items, and one item's write-up.
    page.locator("ion-tab-button[tab='cortex']").click()
    _active(page).get_by_text("Bundle shipping with the spring order").wait_for(timeout=15000)
    # The Pulse card on top, then the stages in the order things flow.
    _active(page).locator(".olano-pulse-card").wait_for()
    assert _active(page).locator(".olano-stage").all_inner_texts() == [
        "Brainstorms 0",
        "Ideas 1",
        "Prototypes 0",
        "Projects 0",
    ]
    _active(page).locator(".olano-stage", has_text="Prototypes").click()
    _active(page).get_by_text("No prototypes yet").wait_for()
    _active(page).locator(".olano-stage", has_text="Prototypes").click()
    _active(page).get_by_text("Bundle shipping with the spring order").wait_for()
    _shot(page, "06-cortex")
    _active(page).get_by_text("Bundle shipping with the spring order").click()
    _active(page).get_by_text("Both suppliers ship from the same port in March.").wait_for()
    _active(page).get_by_text("Clear savings, low effort.").wait_for()
    _shot(page, "07-cortex-item")

    # Inbox: approvals first, then the deployment's alerts.
    page.locator("ion-tab-button[tab='inbox']").click()
    _active(page).get_by_text("Nothing to approve").wait_for(timeout=15000)
    _active(page).locator("ion-segment-button").filter(has_text="Alerts").click()
    _active(page).get_by_text("aria's WhatsApp is not connected").wait_for()
    _shot(page, "08-inbox")

    # Settings: where the app is connected.
    page.locator("ion-tab-button[tab='settings']").click()
    _active(page).get_by_text("Switch deployment").wait_for()
    _active(page).get_by_text("Single-user deployment").wait_for()
    _shot(page, "09-settings")


def test_a_conversation_left_mid_reply_is_the_one_its_agent_reopens(
    phone: Any, mobile_app: MobileApp
) -> None:
    """Start a conversation, leave before the reply lands, tap the agent again:
    that conversation opens, not the previous one or a blank chat.

    The agent's row in Chats opens the thread its "latest conversation" read
    names, and that read learned of a new thread only from a refresh: the
    conversation screen's own refresh runs at the turn's end, which a screen
    the person already left never sees, and the live events are the only
    other trigger. On a busy box (and here, with the event stream refused) no
    refresh lands in time, and the tap opened the previous conversation while
    the new one seemed gone until minutes later. The conversation now writes
    itself into that row when the deployment names its thread.
    """
    page = phone
    page.route("**/api/stream", lambda route: route.abort())
    _connect(page, mobile_app)
    _new_conversation(page, "atlas")
    _active(page).locator("ion-textarea textarea").fill("Plan the warehouse move")
    _active(page).get_by_role("button", name="Send").click()
    # The slow reply's first half: the turn is running and has a thread id.
    _active(page).get_by_text("HJKLQ").wait_for(timeout=15000)

    _active(page).locator("ion-back-button").click()
    page.wait_for_url(re.compile(r"/chats/?$"))
    row = _active(page).locator(".olano-agent-row", has=page.get_by_text("atlas", exact=True))
    row.get_by_text("Plan the warehouse move").wait_for(timeout=5000)

    _open_agent(page, "atlas")
    assert not page.url.endswith("/new"), page.url
    _active(page).locator(".olano-bubble.user", has_text="Plan the warehouse move").wait_for(
        timeout=10000
    )


def test_backgrounding_lets_the_runtime_push_the_reply(phone: Any, mobile_app: MobileApp) -> None:
    page = phone
    _connect(page, mobile_app)
    # The phone as push registration leaves it (the native plugin is not in a browser).
    httpx.post(
        f"{mobile_app.runtime_url}/api/push/devices", json={"token": PUSH_TOKEN, "platform": "ios"}
    ).raise_for_status()

    _new_conversation(page, "atlas")
    _active(page).locator("ion-textarea textarea").fill("Run the forecast")
    _active(page).get_by_role("button", name="Send").click()
    _active(page).get_by_text("HJKLQ").wait_for(timeout=15000)

    before = len(mobile_app.relay.notifications("reply"))
    page.evaluate(
        "() => { Object.defineProperty(document, 'hidden', { value: true, configurable: true });"
        " document.dispatchEvent(new Event('visibilitychange')); }"
    )
    harness._wait_until(
        lambda: len(mobile_app.relay.notifications("reply")) > before, 30, "the reply push"
    )
    push = mobile_app.relay.notifications("reply")[-1]
    assert push["data"]["agent"] == "atlas"
    assert push["data"]["thread_id"]

    page.evaluate(
        "() => { Object.defineProperty(document, 'hidden', { value: false, configurable: true });"
        " document.dispatchEvent(new Event('visibilitychange')); }"
    )
    _active(page).get_by_text("ZXCVB").wait_for(timeout=20000)


def _photo(folder: Path, name: str, color: tuple[int, int, int]) -> Path:
    """A photo larger than the app sends (3000 x 2000), so the test sees it scaled down."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (3000, 2000), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle((600, 500, 2400, 1500), fill=(250, 250, 245))
    draw.rectangle((800, 800, 2200, 900), fill=(30, 30, 30))
    path = folder / name
    img.save(path, "JPEG", quality=92)
    return path


def test_photos(phone: Any, mobile_app: MobileApp, tmp_path: Path) -> None:
    from PIL import Image

    page = phone
    _connect(page, mobile_app)
    _new_conversation(page, "nova")

    colors = [(214, 92, 64), (64, 140, 214), (90, 170, 100), (230, 180, 60), (150, 90, 200)]
    files = [_photo(tmp_path, f"label-{i + 1}.jpg", c) for i, c in enumerate(colors)]
    _active(page).get_by_label("Add photos").click()
    with page.expect_file_chooser() as chooser:
        page.get_by_role("button", name="Choose Photos").click()
    chooser.value.set_files([str(f) for f in files])

    tray = _active(page).get_by_role("list", name="Photos to send")
    ready = tray.locator(".olano-tray-item.ready")
    harness._wait_until(lambda: ready.count() == 5, 20, "five photos prepared")
    _shot(page, "13-photo-tray")
    tray.get_by_label("Remove photo 5").click()
    harness._wait_until(lambda: ready.count() == 4, 5, "the fifth photo removed")

    _active(page).locator("ion-textarea textarea").fill("What is on these labels?")
    _active(page).get_by_role("button", name="Send").click()
    sent = _active(page).locator(".olano-msg.user").last
    sent.locator(".olano-photos.grid").wait_for()
    sent.get_by_text("What is on these labels?").wait_for()
    assert tray.count() == 0, "the tray empties once the photos are sent"
    _active(page).get_by_text("Meridian is cheapest at 4.10 a unit.").wait_for(timeout=20000)
    _active(page).get_by_role("button", name="Record voice note").wait_for()
    _shot(page, "14-photos-sent")

    # The runtime saved each photo in nova's workspace, scaled down, and told
    # the agent where in the message it received.
    home = Path(os.environ["LIONCLAW_HOME"])
    uploads = sorted(home.rglob("uploads/photo-*.jpg"))
    assert len(uploads) == 4, uploads  # the camera photo below comes later
    for upload in uploads:
        with Image.open(upload) as img:
            assert img.format == "JPEG"
            assert max(img.size) == 2048, img.size
    thread_file = max((home / "sessions" / "nova").glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    rows = [json.loads(line) for line in thread_file.read_text(encoding="utf-8").splitlines()]
    user_row = next(r for r in rows if r["role"] == "user")
    assert user_row["content"].startswith("What is on these labels?")
    assert user_row["content"].count("[Image attached: photo-") == 4

    # Full screen from the second photo; swipe (here, the arrow key) to the third.
    sent.locator(".olano-photo").nth(1).click()
    viewer = page.get_by_role("dialog", name="Photo viewer")
    viewer.get_by_text("2 of 4").wait_for()
    viewer.locator("img").nth(1).wait_for()
    _shot(page, "15-photo-viewer")
    page.keyboard.press("ArrowRight")
    viewer.get_by_text("3 of 4").wait_for()
    viewer.get_by_role("button", name="Close").click()
    viewer.wait_for(state="detached")

    # Double-tap zooms in and back out; dragging down puts the photo back.
    sent.locator(".olano-photo").first.click()
    viewer.get_by_text("1 of 4").wait_for()
    image = viewer.locator("img").first
    image.wait_for()
    page.wait_for_timeout(400)  # the opening animation
    image.dblclick()
    harness._wait_until(
        lambda: "scale(2.5)" in (image.get_attribute("style") or ""), 5, "zoomed in"
    )
    image.dblclick()
    harness._wait_until(lambda: "scale(1)" in (image.get_attribute("style") or ""), 5, "zoomed out")
    page.wait_for_timeout(350)
    box = image.bounding_box()
    assert box
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2 + 260, steps=12)
    page.mouse.up()
    viewer.wait_for(state="detached")

    # One more, taken with the camera (a file input in a browser): one photo
    # is drawn at its own shape.
    _active(page).get_by_label("Add photos").click()
    with page.expect_file_chooser() as chooser:
        page.get_by_role("button", name="Take Photo").click()
    chooser.value.set_files(str(files[4]))
    harness._wait_until(lambda: ready.count() == 1, 20, "the camera photo prepared")
    _active(page).get_by_role("button", name="Send").click()
    single = (
        _active(page).locator(".olano-msg.user").last.locator(".olano-photos.single .olano-photo")
    )
    single.wait_for()
    harness._wait_until(
        lambda: abs(
            (single.bounding_box() or {"width": 0})["width"]
            / (single.bounding_box() or {"height": 1})["height"]
            - 1.5
        )
        < 0.05,
        10,
        "a 3:2 photo drawn 3:2",
    )
    _active(page).get_by_role("button", name="Record voice note").wait_for(timeout=20000)
    _shot(page, "16-photo-single")

    # A fresh screen (as after the app restarts) draws the photos from the
    # deployment, and the caption without the runtime's notes about them.
    fresh = page.context.new_page()
    fresh.goto(
        f"{mobile_app.base_url}/chats/nova/{thread_file.stem}", wait_until="domcontentloaded"
    )
    reloaded = _active(fresh).locator(".olano-msg.user").filter(has_text="What is on these labels?")
    reloaded.wait_for(timeout=20000)
    photos = reloaded.locator(".olano-photo img")
    harness._wait_until(lambda: photos.count() == 4, 20, "the photos from the deployment")
    assert all(src.startswith("blob:") for src in photos.evaluate_all("els => els.map(e => e.src)"))
    assert "Image attached" not in reloaded.inner_text()
    fresh.close()


def test_voice_notes(phone: Any, mobile_app: MobileApp, monkeypatch: pytest.MonkeyPatch) -> None:
    """Record a voice note (tap for hands-free), send it, see what the runtime
    heard under it, play it; hold and slide left to throw one away; a fresh
    screen plays it from the deployment with its transcript."""
    from olano.platforms import inbound

    heard = "Please check the label on the blue box"
    received: list[tuple[int, int, int, int, float]] = []

    async def transcribe(path: str) -> tuple[str | None, str | None]:
        # What a speech-to-text service would get: a WAV file it can read.
        with wave.open(path, "rb") as w:
            frames = w.readframes(w.getnframes())
            samples = array.array("h", frames)
            rms = math.sqrt(sum(v * v for v in samples) / max(1, len(samples)))
            received.append(
                (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes(), rms)
            )
        return heard, None

    monkeypatch.setattr(inbound, "_transcribe_audio", transcribe)

    page = phone
    _connect(page, mobile_app)
    _new_conversation(page, "nova")

    mic = _active(page).get_by_role("button", name="Record voice note")
    mic.click()  # a tap: records hands-free
    _active(page).get_by_label("Recording time").wait_for()
    page.wait_for_timeout(2300)
    _shot(page, "17-voice-recording")
    _active(page).get_by_role("button", name="Send voice note").click()

    sent = _active(page).locator(".olano-msg.user").last
    sent.locator(".olano-voice").wait_for()
    _active(page).get_by_text("Meridian is cheapest at 4.10 a unit.").wait_for(timeout=20000)
    # What the runtime heard, read back under the voice note.
    sent.get_by_text(heard).wait_for(timeout=15000)

    home = Path(os.environ["LIONCLAW_HOME"])
    uploads = sorted(home.rglob("uploads/voice-*"))
    assert len(uploads) == 1, uploads
    assert uploads[0].suffix == ".wav", uploads[0]
    # 16 kHz mono 16-bit, about as long as the recording, and not silence
    # (Chromium's fake microphone plays a tone).
    rate, channels, width, frames, rms = received[0]
    assert (rate, channels, width) == (16000, 1, 2)
    assert 1.5 * rate < frames < 4 * rate, frames
    assert rms > 50, rms
    thread_file = max((home / "sessions" / "nova").glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    rows = [json.loads(line) for line in thread_file.read_text(encoding="utf-8").splitlines()]
    user_row = next(r for r in rows if r["role"] == "user")
    assert "[Audio attached: voice-" in user_row["content"]
    assert f"[Voice message transcript] {heard}" in user_row["content"]

    # Play, change speed, pause.
    sent.get_by_role("button", name="Play voice note").click()
    sent.get_by_role("button", name="Pause voice note").wait_for(timeout=10000)
    sent.get_by_role("button", name="Playback speed").click()
    sent.get_by_role("button", name="Playback speed").filter(has_text="1.5x").wait_for()
    _shot(page, "18-voice-sent")
    sent.get_by_role("button", name="Pause voice note").click()
    sent.get_by_role("button", name="Play voice note").wait_for()

    # Hold the microphone, then slide left: thrown away, nothing sent.
    mic = _active(page).get_by_role("button", name="Record voice note")
    box = mic.bounding_box()
    assert box
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    _active(page).get_by_text("Slide to cancel").wait_for()
    page.wait_for_timeout(1200)
    page.mouse.move(x - 170, y, steps=10)
    page.mouse.up()
    _active(page).get_by_text("Slide to cancel").wait_for(state="detached")
    assert _active(page).locator(".olano-voice").count() == 1

    # A fresh screen plays it from the deployment, transcript under it.
    fresh = page.context.new_page()
    fresh.goto(
        f"{mobile_app.base_url}/chats/nova/{thread_file.stem}", wait_until="domcontentloaded"
    )
    reloaded = _active(fresh).locator(".olano-msg.user").last
    reloaded.get_by_text(heard).wait_for(timeout=20000)
    reloaded.get_by_role("button", name="Play voice note").wait_for(timeout=20000)
    assert "Audio attached" not in reloaded.inner_text()
    assert "Voice message transcript" not in reloaded.inner_text()
    fresh.close()


def test_member_voice_note(
    phone: Any, mobile_app: MobileApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member's voice note is transcribed and not saved (a member's
    environment is read-only): the agent gets the transcript, and a reopened
    conversation shows what was said where the recording was."""
    from olano.config.home import update_home_config
    from olano.platforms import inbound
    from olano.users.store import get_user_store

    heard = "Order two more pallets of the blue boxes"

    async def transcribe(_path: str) -> tuple[str | None, str | None]:
        return heard, None

    monkeypatch.setattr(inbound, "_transcribe_audio", transcribe)
    update_home_config({"multi_user_enabled": True})
    store = get_user_store()
    member = store.create_user("kim@example.com", "correct horse battery", name="Kim Lee")
    store.db.grant_access(member.id, "nova")

    page = phone
    page.get_by_text("Connect to your Olano").wait_for()
    page.locator("ion-input input").fill(mobile_app.base_url)
    page.get_by_role("button", name="Continue").click()
    page.locator("ion-input input[type='email']").fill("kim@example.com")
    page.locator("ion-input input[type='password']").fill("correct horse battery")
    page.get_by_role("button", name="Sign in").click()
    _active(page).get_by_text("nova", exact=True).wait_for(timeout=20000)
    _new_conversation(page, "nova")

    _active(page).get_by_role("button", name="Record voice note").click()
    _active(page).get_by_label("Recording time").wait_for()
    page.wait_for_timeout(1500)
    _active(page).get_by_role("button", name="Send voice note").click()
    sent = _active(page).locator(".olano-msg.user").last
    _active(page).get_by_text("Meridian is cheapest at 4.10 a unit.").wait_for(timeout=20000)
    sent.get_by_text(heard).wait_for(timeout=15000)

    home = Path(os.environ["LIONCLAW_HOME"])
    assert not list(home.rglob("uploads/voice-*"))
    thread_file = max((home / "sessions" / "nova").glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    rows = [json.loads(line) for line in thread_file.read_text(encoding="utf-8").splitlines()]
    user_row = next(r for r in rows if r["role"] == "user")
    assert user_row["content"] == f"[Voice message transcript] {heard}"

    fresh = page.context.new_page()
    fresh.goto(
        f"{mobile_app.base_url}/chats/nova/{thread_file.stem}", wait_until="domcontentloaded"
    )
    reloaded = _active(fresh).locator(".olano-msg.user").last
    reloaded.locator(".olano-voice-heard").get_by_text(heard).wait_for(timeout=20000)
    assert reloaded.get_by_text("Voice note", exact=True).count() == 1
    assert reloaded.get_by_role("button", name="Play voice note").count() == 0
    _shot(fresh, "24-voice-member-reopened")
    fresh.close()


# A phone's own speech (the OlanoSpeech plugin) for a browser, which has none:
# it "hears" one sentence in any recording and "reads" a reply for 2.5 seconds.
FAKE_PHONE_SPEECH = """
window.__heard = [];
window.__spoken = [];
window.__olanoFakeSpeech = {
  recognitionStatus: async () => ({ available: true, onDevice: true, permission: "granted", language: "en-US" }),
  transcribe: async ({ data, durationMs }) => {
    window.__heard.push({ bytes: data.length, durationMs });
    await new Promise((resolve) => setTimeout(resolve, 1500));
    return { text: "Order two more pallets of the blue boxes", onDevice: true, language: "en-US" };
  },
  speak: ({ text }) =>
    new Promise((resolve) => {
      window.__spoken.push(text);
      window.__stopSpeaking = () => resolve({ finished: false });
      setTimeout(() => resolve({ finished: true }), 2500);
    }),
  stopSpeaking: async () => {
    if (window.__stopSpeaking) window.__stopSpeaking();
  },
};
"""


def test_voice_note_heard_on_the_phone_and_the_reply_read_aloud(
    phone: Any, mobile_app: MobileApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A phone that can transcribe sends a voice note as its words: no upload,
    nothing for the deployment to transcribe, the same transcript line the
    runtime writes. The reply to it is read aloud, in the phone's voice while
    the deployment has no text to speech, and in the deployment's voice (the
    speaker under the reply) once it has."""
    from olano.platforms import inbound

    words = "Order two more pallets of the blue boxes"
    reply_text = "Meridian is cheapest at 4.10 a unit."
    reply_said = f"Three suppliers ship within a week; {reply_text}"

    async def no_deployment_stt(_path: str) -> tuple[str | None, str | None]:
        raise AssertionError("the deployment was asked to transcribe a voice note")

    monkeypatch.setattr(inbound, "_transcribe_audio", no_deployment_stt)

    page = phone
    # Voice notes go to the deployment unless the person (or the admin's
    # default) chose the phone: this person turned "Transcribe on this phone" on.
    page.evaluate(
        "localStorage.setItem('olano.voice', JSON.stringify("
        "{transcribeOn: 'phone', readAloud: 'voice', transcribeChosen: true}))"
    )
    page.reload(wait_until="domcontentloaded")
    page.evaluate(FAKE_PHONE_SPEECH)
    _connect(page, mobile_app)
    _new_conversation(page, "nova")

    _active(page).get_by_role("button", name="Record voice note").click()
    _active(page).get_by_label("Recording time").wait_for()
    page.wait_for_timeout(1800)
    _active(page).get_by_role("button", name="Send voice note").click()

    sent = _active(page).locator(".olano-msg.user").last
    sent.get_by_text("Transcribing…").wait_for(timeout=5000)
    sent.get_by_text(words).wait_for(timeout=10000)
    heard = page.evaluate("window.__heard")
    assert len(heard) == 1 and heard[0]["bytes"] > 10000 and heard[0]["durationMs"] > 1000, heard

    # The reply lands and is read aloud in the phone's voice: this deployment
    # has no text to speech.
    reply = _active(page).locator(".olano-msg.agent").last
    reply.get_by_text(reply_text).wait_for(timeout=20000)
    harness._wait_until(
        lambda: page.evaluate("window.__spoken.length") == 1, 15, "the reply read aloud"
    )
    assert page.evaluate("window.__spoken") == [reply_said]
    reply.get_by_role("button", name="Stop reading").wait_for(timeout=5000)
    _shot(page, "25-voice-heard-on-phone")
    reply.get_by_role("button", name="Read reply aloud").wait_for(timeout=10000)

    # The deployment got the words, never the recording.
    home = Path(os.environ["LIONCLAW_HOME"])
    assert not list(home.rglob("uploads/voice-*"))
    thread_file = max((home / "sessions" / "nova").glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    rows = [json.loads(line) for line in thread_file.read_text(encoding="utf-8").splitlines()]
    user_row = next(r for r in rows if r["role"] == "user")
    assert user_row["content"] == f"[Voice message transcript] {words}"

    # Tapped again while it reads, the speaker stops it.
    reply.get_by_role("button", name="Read reply aloud").click()
    reply.get_by_role("button", name="Stop reading").click()
    reply.get_by_role("button", name="Read reply aloud").wait_for(timeout=5000)
    assert page.evaluate("window.__spoken.length") == 2

    # Text to speech turned on at the deployment: the speaker uses its voice.
    spoken_by_deployment: list[tuple[str, str]] = []

    def synthesize(text: str, output_path: str, voice: str = "", audio_format: str = "") -> str:
        spoken_by_deployment.append((text, audio_format))
        out = Path(output_path).with_suffix(".wav")
        rate = 16000
        tone = array.array(
            "h", (int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate))
        )
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(tone.tobytes())
        return str(out)

    monkeypatch.setattr("olano.speech.synthesize_speech", synthesize)
    # A fresh screen: the app remembers for a while that a deployment had no voice.
    fresh = page.context.new_page()
    fresh.goto(
        f"{mobile_app.base_url}/chats/nova/{thread_file.stem}", wait_until="domcontentloaded"
    )
    fresh.evaluate(FAKE_PHONE_SPEECH)
    reloaded = _active(fresh).locator(".olano-msg.agent").last
    reloaded.get_by_text(reply_text).wait_for(timeout=20000)
    # Reopened, the voice note is what was said.
    _active(fresh).locator(".olano-msg.user").last.locator(".olano-voice-heard").get_by_text(
        words
    ).wait_for()
    reloaded.get_by_role("button", name="Read reply aloud").click()
    reloaded.get_by_role("button", name="Stop reading").wait_for(timeout=10000)
    assert spoken_by_deployment == [(reply_said, "mp3")]
    reloaded.get_by_role("button", name="Read reply aloud").wait_for(timeout=10000)
    assert fresh.evaluate("window.__spoken") == []
    fresh.close()

    # Settings: where voice notes become words, and when replies are read aloud.
    _active(page).locator("ion-back-button").click()
    page.locator("ion-tab-button[tab='settings']").click()
    settings = _active(page)
    settings.get_by_text("Transcribe on this phone").wait_for()
    # "American English" on a phone; headless Chromium has no language names.
    settings.get_by_text(re.compile(r"It listens for (American English|en-US)")).wait_for()
    settings.locator("ion-radio").filter(has_text="Never").click()
    settings.get_by_text("Replies are not read aloud by themselves.").wait_for()
    _shot(page, "26-voice-settings")
    assert json.loads(page.evaluate("localStorage.getItem('olano.voice')")) == {
        "transcribeOn": "phone",
        "readAloud": "never",
        "transcribeChosen": True,
        "dictation": True,
    }


# A phone whose own recognizer dictates (the OlanoSpeech plugin's live half):
# `startDictation` streams the words of `window.__dictationScript` as it would
# hear them, a loudness reading every 100 ms, and `stopDictation` closes the
# open segment with `window.__dictationTail`, the last words still in flight.
# Each is used once, so a later session (hands-free listening again) hears
# nothing unless the test scripts it.
FAKE_PHONE_DICTATION = """
window.__dictation = { started: 0, stopped: 0, cancelled: 0 };
(() => {
  const listeners = {};
  let timers = [];
  const emit = (event, data) => (listeners[event] || []).slice().forEach((fn) => fn(data));
  const clear = () => { timers.forEach((t) => clearTimeout(t)); timers = []; };
  window.__olanoFakeSpeech = {
    recognitionStatus: async () => ({ available: false, onDevice: false, permission: "prompt", language: "" }),
    transcribe: async () => { throw Object.assign(new Error("no"), { code: "unavailable" }); },
    speak: async () => ({ finished: true }),
    stopSpeaking: async () => {},
    dictationStatus: async () => ({ available: true, permission: "granted", language: "en-US" }),
    addListener: async (event, fn) => {
      (listeners[event] = listeners[event] || []).push(fn);
      return { remove: async () => { listeners[event] = (listeners[event] || []).filter((f) => f !== fn); } };
    },
    startDictation: async () => {
      window.__dictation.started++;
      const script = window.__dictationScript || [];
      window.__dictationScript = [];
      for (const [at, text, final] of script) {
        timers.push(setTimeout(() => emit("dictationText", { text, final }), at));
      }
      timers.push(setInterval(() => emit("dictationLevel", { level: Math.random() < 0.3 ? 0.02 : 0.3 + Math.random() * 0.7 }), 100));
      return { language: "en-US" };
    },
    stopDictation: async () => {
      clear();
      window.__dictation.stopped++;
      const tail = window.__dictationTail;
      window.__dictationTail = "";
      if (tail) emit("dictationText", { text: tail, final: true });
    },
    cancelDictation: async () => { clear(); window.__dictation.cancelled++; },
  };
})();
"""


def _dictate(page: Any, script: list[tuple[int, str, bool]], tail: str) -> None:
    page.evaluate(
        "([script, tail]) => { window.__dictationScript = script; window.__dictationTail = tail; }",
        [script, tail],
    )


def test_live_dictation(phone: Any, mobile_app: MobileApp, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tap on the microphone dictates: the words appear in the composer as
    they are said, stop leaves them in the box to edit, x throws a dictation
    away, and send sends the text, never a recording. Holding the microphone
    still records a voice note, and Settings can turn dictation off."""
    from olano.platforms import inbound

    async def transcribe(_path: str) -> tuple[str | None, str | None]:
        return "A voice note after all", None

    monkeypatch.setattr(inbound, "_transcribe_audio", transcribe)

    page = phone
    page.add_init_script(FAKE_PHONE_DICTATION)
    page.reload(wait_until="domcontentloaded")
    _connect(page, mobile_app)
    _new_conversation(page, "nova")
    chat = _active(page)
    mic = chat.get_by_role("button", name="Dictate, or hold to record a voice note")

    # Tap: the composer turns into the dictation card and the words stream in.
    _dictate(
        page,
        [
            (300, "Hey, just to make sure", False),
            (700, "Hey, just to make sure that you updated the Stripe pricing", False),
            (1000, "Hey, just to make sure that you updated the Stripe pricing correctly.", True),
            (1400, "Just make sure that", False),
            (1800, "Just make sure that everything is", False),
        ],
        "Just make sure that everything is consistent.",
    )
    mic.click()
    field = chat.get_by_label("Dictated text")
    field.get_by_text("Just make sure that everything is", exact=False).wait_for(timeout=10000)
    assert "Stripe pricing correctly. Just make sure" in field.inner_text()
    assert chat.locator(".olano-dictation-wave .bar").count() > 0
    assert chat.locator("ion-textarea").count() == 0
    _shot(page, "29-dictating")

    # Stop: the words, the last ones included, are in the box to edit.
    chat.get_by_role("button", name="Stop dictating").click()
    box = chat.locator("ion-textarea textarea")
    box.wait_for()
    said = (
        "Hey, just to make sure that you updated the Stripe pricing correctly. "
        "Just make sure that everything is consistent."
    )
    harness._wait_until(lambda: box.input_value() == said, 5, "the dictated words in the box")
    _shot(page, "30-dictated")

    # The microphone stays next to the words; x throws a second dictation away.
    _dictate(page, [(200, "Never mind", False)], "Never mind that")
    mic.click()
    field.get_by_text("Never mind", exact=False).wait_for(timeout=10000)
    chat.get_by_role("button", name="Cancel dictation").click()
    box.wait_for()
    assert box.input_value() == said
    assert page.evaluate("window.__dictation") == {"started": 2, "stopped": 1, "cancelled": 1}

    # Send from the dictation card: what was typed and what was said go as text.
    _dictate(page, [(200, "And ship it today", False)], "And ship it today.")
    mic.click()
    field.get_by_text("And ship it today", exact=False).wait_for(timeout=10000)
    chat.get_by_role("button", name="Send", exact=True).click()
    message = f"{said} And ship it today."
    sent = chat.locator(".olano-msg.user").last
    sent.get_by_text(message).wait_for(timeout=10000)
    chat.get_by_text("Meridian is cheapest at 4.10 a unit.").wait_for(timeout=20000)
    assert box.input_value() == ""
    home = Path(os.environ["LIONCLAW_HOME"])
    thread_file = max((home / "sessions" / "nova").glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    rows = [json.loads(line) for line in thread_file.read_text(encoding="utf-8").splitlines()]
    user_rows = [r for r in rows if r["role"] == "user"]
    assert [r["content"] for r in user_rows] == [message]
    assert not list(home.rglob("uploads/voice-*"))

    # Holding the microphone still records a voice note.
    mic.wait_for(timeout=10000)
    box_ = mic.bounding_box()
    assert box_
    page.mouse.move(box_["x"] + box_["width"] / 2, box_["y"] + box_["height"] / 2)
    page.mouse.down()
    chat.get_by_label("Recording time").wait_for(timeout=5000)
    page.wait_for_timeout(1800)
    page.mouse.up()
    voice = chat.locator(".olano-msg.user").last.locator(".olano-voice")
    voice.wait_for(timeout=10000)
    assert page.evaluate("window.__dictation")["started"] == 3

    # Settings: dictation can be turned off, and a tap records a voice note again.
    chat.locator("ion-back-button").click()
    page.locator("ion-tab-button[tab='settings']").click()
    settings = _active(page)
    toggle = settings.locator("ion-toggle", has_text="Dictate with the microphone")
    toggle.wait_for()
    settings.get_by_text("worked out on this phone", exact=False).wait_for()
    _shot(page, "31-dictation-settings")
    toggle.click()
    settings.get_by_text("A tap on the microphone records a voice note").wait_for()
    assert json.loads(page.evaluate("localStorage.getItem('olano.voice')"))["dictation"] is False


def test_threads_open_whatever_their_id(phone: Any, mobile_app: MobileApp) -> None:
    """Agent-to-agent, scheduled, email and Cortex threads carry ``:`` and
    ``@`` in their ids. The router hands those back still encoded, and reading
    them raw encoded them twice on the way to the deployment: the thread
    answered "not found" and opened as an empty conversation."""
    ids = [
        "acp::aria::nova",
        "scheduled_nova_1a2b3c4d@20261016T100000-0001abcd",
        "email:bob@example.com",
        "cortex:nova",
    ]
    sessions = Path(os.environ["LIONCLAW_HOME"]) / "sessions" / "nova"
    now = datetime.now(timezone.utc).isoformat()
    for i, tid in enumerate(ids):
        # The email channel stamps its turns like every messenger does.
        stamp = {"channel": "email"} if tid.startswith("email:") else {}
        with (sessions / f"{tid}.jsonl").open("w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"ts": now, "role": "user", "content": f"Id check question {i}", **stamp}
                )
                + "\n"
            )
            fh.write(
                json.dumps({"ts": now, "role": "assistant", "content": f"Id check answer {i}"})
                + "\n"
            )

    with (sessions / "8123456789.jsonl").open("w", encoding="utf-8") as fh:
        for role, text in (("user", "Can you send me the invoice?"), ("assistant", "Sent.")):
            row = {"ts": now, "role": role, "content": text, "channel": "telegram"}
            fh.write(json.dumps({**row, "sender": "Dana"} if role == "user" else row) + "\n")

    page = phone
    _connect(page, mobile_app)
    _open_threads(page, "nova")

    # Each row says where it came from, in the dashboard's words.
    def badge(title: str) -> str:
        title_cell = page.locator(".olano-thread-title", has_text=title)
        row = _active(page).locator("ion-item").filter(has=title_cell)
        return " ".join(row.locator(".olano-source").all_inner_texts()).lower()

    _thread_row(page, "Id check question 0").wait_for()
    assert badge("Id check question 0") == "agent"
    assert badge("Id check question 1") == "scheduled"
    assert badge("Id check question 2") == "email"
    assert badge("Id check question 3") == "cortex"
    assert badge("Can you send me the invoice?") == "telegram"
    assert badge("Compare our three packaging suppliers") == "dashboard"
    _shot(page, "25-thread-sources")

    for i in range(len(ids)):
        _thread_row(page, f"Id check question {i}").click()
        _active(page).get_by_text(f"Id check answer {i}").wait_for(timeout=10000)
        _active(page).locator("ion-back-button").click()
        _thread_row(page, f"Id check question {i}").wait_for()


def test_a_long_conversation_opens_on_its_newest_page(phone: Any, mobile_app: MobileApp) -> None:
    """A long conversation opens on its newest messages without the tool
    output the app never draws, and the earlier ones load as the person
    scrolls up, without moving the message they are reading, down to the
    first one."""
    sessions = Path(os.environ["LIONCLAW_HOME"]) / "sessions" / "nova"
    start = datetime.now(timezone.utc)
    with (sessions / "paging-long.jsonl").open("w", encoding="utf-8") as fh:
        for i in range(120):
            ts = (start + timedelta(seconds=i)).isoformat()
            for row in (
                {"role": "user", "content": f"Paging question {i}", "channel": "dashboard"},
                {
                    "role": "tool_call",
                    "content": json.dumps({"query": f"q{i}"}),
                    "name": "web_search",
                },
                {"role": "tool_result", "content": f"web_search: Paging output {i} " + "x" * 800},
                {"role": "assistant", "content": f"Paging answer {i}"},
            ):
                fh.write(json.dumps({"ts": ts, **row}) + "\n")

    page = phone
    reads: list[str] = []
    page.on("request", lambda r: reads.append(r.url) if "/threads/paging-long" in r.url else None)
    _connect(page, mobile_app)
    _open_agent(page, "nova")
    chat = _active(page)
    chat.get_by_text("Paging answer 119", exact=True).wait_for(timeout=15000)
    page.wait_for_timeout(1500)  # the page above may load at once: let it land

    assert "limit=50" in reads[0] and "tool_results=status" in reads[0], reads[0]
    assert chat.get_by_text("Paging question 0", exact=True).count() == 0

    # At the top, before the page above arrives: where the earliest message held sits.
    probe = """async (content) => {
        const el = await content.getScrollElement();
        el.scrollTop = 0;
        const bubbles = [...content.querySelectorAll('.olano-bubble.user')];
        const first = bubbles[0];
        return {text: first.innerText.trim(), top: first.getBoundingClientRect().top - el.getBoundingClientRect().top};
    }"""
    content = chat.locator("ion-content.olano-chat-content")
    held = content.evaluate(probe)
    earliest = int(held["text"].rsplit(" ", 1)[1])
    chat.get_by_text(f"Paging question {earliest - 1}", exact=True).wait_for(timeout=10000)
    page.wait_for_timeout(300)
    moved = content.evaluate(
        """async (content, text) => {
            const el = await content.getScrollElement();
            const node = [...content.querySelectorAll('.olano-bubble.user')].find((b) => b.innerText.trim() === text);
            return node.getBoundingClientRect().top - el.getBoundingClientRect().top;
        }""",
        held["text"],
    )
    assert abs(moved - held["top"]) <= 2, (held, moved)

    # On up to the first message.
    for _ in range(30):
        if chat.get_by_text("Start of conversation").count():
            break
        content.evaluate("async (c) => { (await c.getScrollElement()).scrollTop = 0; }")
        page.wait_for_timeout(400)
    chat.get_by_text("Start of conversation").wait_for(timeout=5000)
    assert chat.get_by_text("Paging question 0", exact=True).count() == 1
    assert chat.get_by_text("Paging output", exact=False).count() == 0
    # Each page once: 480 rows at 50 a page, and nothing asked twice.
    befores = [u for u in reads if "before=" in u]
    assert len(befores) == len(set(befores)) <= 10, reads
    _shot(page, "26-conversation-start")


def _last_reply_clear_of_the_composer(page: Any) -> dict[str, float]:
    """Where the newest reply's buttons end and the composer card begins, in
    the window's coordinates: the buttons belong above the card, on screen."""
    return _active(page).evaluate(
        """(root) => {
            const actions = [...root.querySelectorAll('.olano-actions:not(.mine)')].pop();
            const card = root.querySelector('.olano-composer-card');
            return {actions: actions.getBoundingClientRect().bottom, card: card.getBoundingClientRect().top};
        }"""
    )


def test_the_newest_reply_stays_above_the_keyboard_and_the_composer(
    phone: Any, mobile_app: MobileApp
) -> None:
    """A finished reply's buttons are scrolled into view above the composer,
    and the keyboard opening (the web view shrinks above it, as Capacitor's
    native resize does) carries the conversation up with it rather than
    hiding the newest messages behind the composer; someone reading further
    up keeps their place relative to the bottom."""
    sessions = Path(os.environ["LIONCLAW_HOME"]) / "sessions" / "nova"
    start = datetime.now(timezone.utc)
    with (sessions / "keyboard-long.jsonl").open("w", encoding="utf-8") as fh:
        for i in range(12):
            ts = (start + timedelta(seconds=i)).isoformat()
            fh.write(
                json.dumps(
                    {
                        "ts": ts,
                        "role": "user",
                        "content": f"Keyboard question {i}",
                        "channel": "dashboard",
                    }
                )
                + "\n"
            )
            fh.write(
                json.dumps(
                    {
                        "ts": ts,
                        "role": "assistant",
                        "content": f"Keyboard answer {i}. " + "Some words. " * 20,
                    }
                )
                + "\n"
            )

    page = phone
    _connect(page, mobile_app)
    _open_agent(page, "nova")
    chat = _active(page)
    chat.get_by_text("Keyboard question 11", exact=True).wait_for(timeout=15000)
    box = chat.locator("ion-textarea textarea")
    box.fill("Which supplier ships fastest?")
    chat.get_by_role("button", name="Send").click()
    chat.get_by_text("Meridian is cheapest at 4.10 a unit.").wait_for(timeout=20000)
    chat.get_by_role("button", name="Record voice note").wait_for()
    page.wait_for_timeout(400)
    where = _last_reply_clear_of_the_composer(page)
    assert where["actions"] <= where["card"], where

    # The keyboard comes up: the window loses its height and the composer
    # drops the home indicator's space.
    box.focus()
    page.evaluate(
        "document.documentElement.classList.add('olano-keyboard-open', 'olano-soft-keyboard')"
    )
    # The composer gives up the home indicator's space, and Ionic's fullscreen
    # content grows its scroll element by it a moment later; the browser then
    # clamps the offset. Wait for that to land before the shrink: in that
    # order the clamp's scroll event is dispatched in the same frame as the
    # shrink, already measured against the smaller box, and the anchor used to
    # read it as the person having scrolled up and left the newest reply under
    # the keyboard. The order is the phone's own (the keyboard class comes
    # first, the web view shrinks after), and it was the flaky half of this
    # test: whichever landed first decided the result.
    # Bounded: on a runner that never grows the box (no bottom inset), the
    # shrink goes ahead after a second and the test exercises the other order.
    content = chat.locator("ion-content.olano-chat-content")
    content.evaluate(
        "async (c) => { const el = await c.getScrollElement(); let left = 60;"
        " await new Promise((r) => { const t = () => (el.clientHeight > window.innerHeight || left-- <= 0 ? r() : requestAnimationFrame(t)); t(); }); }"
    )
    page.set_viewport_size({"width": 393, "height": 852 - 336})
    page.wait_for_timeout(500)
    where = _last_reply_clear_of_the_composer(page)
    assert 0 < where["actions"] <= where["card"], where
    _shot(page, "28-keyboard-up")

    # And down again: still the newest reply above the composer.
    page.evaluate(
        "document.documentElement.classList.remove('olano-keyboard-open', 'olano-soft-keyboard')"
    )
    page.set_viewport_size({"width": 393, "height": 852})
    page.wait_for_timeout(500)
    where = _last_reply_clear_of_the_composer(page)
    assert where["actions"] <= where["card"], where

    # Something growing at the bottom after the reply's last words (its
    # buttons, a picture loading) is followed too.
    from_bottom = """async (c) => { const el = await c.getScrollElement(); return el.scrollHeight - el.clientHeight - el.scrollTop; }"""
    chat.evaluate(
        "(root) => { const d = document.createElement('div'); d.style.height = '120px';"
        " [...root.querySelectorAll('.olano-msg')].pop().appendChild(d); }"
    )
    page.wait_for_timeout(300)
    assert content.evaluate(from_bottom) <= 2

    # Reading further up: the keyboard keeps the distance from the bottom.
    content.evaluate(
        "async (c) => { const el = await c.getScrollElement(); el.scrollTop = el.scrollHeight - el.clientHeight - 900; }"
    )
    page.wait_for_timeout(300)
    before = content.evaluate(from_bottom)
    page.set_viewport_size({"width": 393, "height": 852 - 336})
    page.wait_for_timeout(500)
    assert abs(content.evaluate(from_bottom) - before) <= 2, before


def test_scrolling_up_after_the_composer_grows_shows_the_jump_button(
    phone: Any, mobile_app: MobileApp
) -> None:
    """The composer growing a few lines (its own height, not the window's) is
    a change of the conversation's window too; scrolling up to read after it
    must still count as the person moving, so the jump-to-latest button comes
    up and a reply growing at the bottom leaves them where they are."""
    sessions = Path(os.environ["LIONCLAW_HOME"]) / "sessions" / "nova"
    start = datetime.now(timezone.utc)
    with (sessions / "composer-grows.jsonl").open("w", encoding="utf-8") as fh:
        for i in range(12):
            ts = (start + timedelta(seconds=i)).isoformat()
            fh.write(
                json.dumps(
                    {
                        "ts": ts,
                        "role": "user",
                        "content": f"Composer question {i}",
                        "channel": "dashboard",
                    }
                )
                + "\n"
            )
            fh.write(
                json.dumps(
                    {
                        "ts": ts,
                        "role": "assistant",
                        "content": f"Composer answer {i}. " + "Some words. " * 20,
                    }
                )
                + "\n"
            )

    page = phone
    _connect(page, mobile_app)
    _open_agent(page, "nova")
    chat = _active(page)
    chat.get_by_text("Composer question 11", exact=True).wait_for(timeout=15000)
    content = chat.locator("ion-content.olano-chat-content")
    # Five lines: the composer grows (Ionic then re-pads the conversation's
    # bottom by as much) while the window keeps its height.
    chat.locator("ion-textarea textarea").fill("One\nTwo\nThree\nFour\nFive")
    page.wait_for_timeout(500)
    jump = chat.get_by_role("button", name="Jump to the latest message")
    assert not jump.is_visible()

    content.evaluate(
        "async (c) => { const el = await c.getScrollElement(); el.scrollTop = el.scrollHeight - el.clientHeight - 900; }"
    )
    page.wait_for_timeout(500)
    assert jump.is_visible()

    # Reading up there, something growing at the bottom (a reply's buttons,
    # a picture loading) leaves them where they are.
    scroll_top = "async (c) => (await c.getScrollElement()).scrollTop"
    before = content.evaluate(scroll_top)
    chat.evaluate(
        "(root) => { const d = document.createElement('div'); d.style.height = '120px';"
        " [...root.querySelectorAll('.olano-msg')].pop().appendChild(d); }"
    )
    page.wait_for_timeout(300)
    assert abs(content.evaluate(scroll_top) - before) <= 1, before


def test_the_conversations_list_comes_in_pages_and_searches_on_the_deployment(
    phone: Any, mobile_app: MobileApp
) -> None:
    """An agent with months of history lists its newest twenty threads, loads
    the next twenty as the person scrolls, and finds threads by words in their
    messages on the deployment, with a line showing where."""
    sessions = Path(os.environ["LIONCLAW_HOME"]) / "sessions" / "nova"
    start = datetime.now(timezone.utc) - timedelta(days=2)
    for i in range(45):
        ts = (start + timedelta(minutes=i)).isoformat()
        path = sessions / f"paged-{i:03d}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": ts,
                        "role": "user",
                        "content": f"Paged conversation {i}",
                        "channel": "dashboard",
                    }
                )
                + "\n"
            )
            body = "The quarterly budget needs a second look." if i == 7 else f"Reply {i}."
            fh.write(json.dumps({"ts": ts, "role": "assistant", "content": body}) + "\n")
        stamp = (start + timedelta(minutes=i)).timestamp()
        os.utime(path, (stamp, stamp))

    page = phone
    reads: list[str] = []
    page.on(
        "request", lambda r: reads.append(r.url) if "/api/agents/nova/threads" in r.url else None
    )
    _connect(page, mobile_app)
    _open_threads(page, "nova")
    _thread_row(page, "Paged conversation 44").wait_for()
    # An exact match: "Paged conversation 2" is also the start of 25 to 29.
    assert (
        _active(page)
        .locator(".olano-thread-title", has_text=re.compile(r"^Paged conversation 2$"))
        .count()
        == 0
    )
    # The list asked for its first page of twenty (the agent's row asked for one).
    assert any("limit=20" in u and "before=" not in u for u in reads), reads

    # Scrolling to the bottom brings the next page, until the oldest thread.
    content = _active(page).locator("ion-content")
    for _ in range(12):
        if _thread_row(page, "Paged conversation 0").count():
            break
        content.evaluate(
            "async (c) => { const el = await c.getScrollElement(); el.scrollTop = el.scrollHeight; }"
        )
        page.wait_for_timeout(500)
    _thread_row(page, "Paged conversation 0").wait_for(timeout=10000)
    paged = [u for u in reads if "before=" in u]
    assert len(paged) >= 2 and len(paged) == len(set(paged)), paged
    _shot(page, "27-conversations-paged")

    # Search asks the deployment and shows where the words were found.
    _active(page).locator("ion-searchbar input").fill("quarterly budget")
    # The snippet, not the title: the title is on the regular list too until the
    # results replace it.
    _active(page).locator(".olano-thread-match", has_text="quarterly budget").wait_for(
        timeout=10000
    )
    assert _thread_row(page, "Paged conversation 7").count() == 1
    assert _thread_row(page, "Paged conversation 44").count() == 0
    assert any("/threads/search?" in u and "q=quarterly" in u for u in reads), reads
    _active(page).get_by_text("Every conversation searched").wait_for()
    _shot(page, "28-conversations-search")

    _active(page).locator("ion-searchbar input").fill("zebra giraffe")
    _active(page).get_by_text("Nothing found").wait_for(timeout=10000)


# What a deployment with Olano's tiers and one provider key would list.
MODEL_CATALOG = {
    "providers": [
        {
            "key": "olano",
            "name": "Olano",
            "models": ["max", "pro", "turbo"],
            "auth": {"connected": True},
            "model_meta": {
                "pro": {"reasoning_efforts": ["low", "medium", "high"], "default_effort": "medium"}
            },
        },
        {
            "key": "anthropic",
            "name": "Anthropic",
            "models": ["claude-sonnet-4-6"],
            "env_var": "ANTHROPIC_API_KEY",
            "env_set": True,
        },
    ],
    "model_aliases": [],
}


def test_model_per_conversation(phone: Any, mobile_app: MobileApp) -> None:
    """A conversation picks its own model and reasoning level, sends them with
    every message and reopens on them; a new conversation starts on the
    agent's own model again."""
    page = phone
    page.route(
        "**/api/catalog/providers*",
        lambda route: route.fulfill(json=MODEL_CATALOG),
    )
    _MobileRunner.overrides.clear()
    _connect(page, mobile_app)
    _new_conversation(page, "nova")

    # The chip draws the model and its level as two spans; its label reads
    # them as one line ("Olano Pro · High").
    chip = _active(page).locator(".olano-model-chip")
    default_line = _chip_line(chip)
    chip.click()
    sheet = page.locator("ion-modal.olano-model-sheet")
    sheet.get_by_text("For this conversation only.").wait_for()
    sheet.get_by_text("Olano Pro", exact=True).click()
    sheet.locator("ion-segment-button", has_text="High").click()
    _shot(page, "28-model-sheet")
    sheet.get_by_role("button", name="Done").click()
    sheet.wait_for(state="hidden")
    assert _chip_line(chip) == "Olano Pro · High"

    _active(page).locator("ion-textarea textarea").fill("Which supplier is fastest?")
    _active(page).get_by_role("button", name="Send").click()
    _active(page).get_by_text("Meridian is cheapest at 4.10 a unit.").wait_for(timeout=20000)
    assert _MobileRunner.overrides[-1] == ("olano:pro", "high")
    _shot(page, "29-model-picked")

    # Reopened, the conversation continues on what it last ran on.
    home = Path(os.environ["LIONCLAW_HOME"])
    thread_file = max((home / "sessions" / "nova").glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    fresh = page.context.new_page()
    fresh.route("**/api/catalog/providers*", lambda route: route.fulfill(json=MODEL_CATALOG))
    fresh.goto(
        f"{mobile_app.base_url}/chats/nova/{thread_file.stem}", wait_until="domcontentloaded"
    )
    _active(fresh).get_by_text("Meridian is cheapest at 4.10 a unit.").wait_for(timeout=20000)
    fresh_chip = _active(fresh).locator(".olano-model-chip")
    fresh_chip.and_(fresh.locator('[aria-label="Model: Olano Pro · High. Change it"]')).wait_for()
    fresh.close()

    # A new conversation starts on the agent's own model.
    _active(page).locator("ion-back-button").click()
    _new_conversation(page, "nova")
    _active(page).locator(
        f'.olano-model-chip[aria-label="Model: {default_line}. Change it"]'
    ).wait_for()


def _chip_line(chip) -> str:
    """The model line the composer's model chip names in its label."""
    label = chip.get_attribute("aria-label") or ""
    return label.removeprefix("Model: ").removesuffix(". Change it")


# The smallest PDF a reader opens: one blank page.
TINY_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def test_files_in_a_reply_open_in_the_app(phone: Any, mobile_app: MobileApp) -> None:
    """The files an agent names in its reply open inside the app: Markdown
    rendered, JSON laid out, a PDF drawn, anything else offered to other apps."""
    home = Path(os.environ["LIONCLAW_HOME"])
    reports = home / "agents" / "nova" / "workspace" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "daily-run.md").write_text(
        "# Daily run\n\nNo campaign reply was verified today.\n", encoding="utf-8"
    )
    (reports / "data.json").write_text('{"suppliers":["Meridian","Boxwell"]}', encoding="utf-8")
    (reports / "terms.pdf").write_bytes(TINY_PDF)
    (reports / "plan.docx").write_bytes(b"PK\x03\x04 not really a document")
    now = datetime.now(timezone.utc).isoformat()
    reply = (
        "Done.\n\nSaved: reports/daily-run.md\nSaved: reports/data.json\n"
        "Saved: reports/terms.pdf\nSaved: reports/plan.docx"
    )
    with (home / "sessions" / "nova" / "file-handoff.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": now, "role": "user", "content": "Send me the run files"}) + "\n")
        fh.write(json.dumps({"ts": now, "role": "assistant", "content": reply}) + "\n")

    page = phone
    _connect(page, mobile_app)
    _open_threads(page, "nova")
    _thread_row(page, "Send me the run files").click()
    viewer = page.locator("ion-modal.olano-file-viewer")

    _active(page).get_by_role("button", name="Open daily-run.md").click()
    viewer.locator(".olano-md h1", has_text="Daily run").wait_for(timeout=10000)
    _shot(page, "30-file-markdown")
    viewer.get_by_role("button", name="Close").click()
    viewer.wait_for(state="hidden")

    _active(page).get_by_role("button", name="Open data.json").click()
    viewer.locator(".olano-code").get_by_text('"Meridian"').wait_for(timeout=10000)
    assert '"suppliers": [' in viewer.locator(".olano-code pre").inner_text()
    viewer.get_by_role("button", name="Close").click()
    viewer.wait_for(state="hidden")

    _active(page).get_by_role("button", name="Open terms.pdf").click()
    frame = viewer.locator("iframe.olano-file-frame")
    frame.wait_for(timeout=10000)
    assert (frame.get_attribute("src") or "").startswith("blob:")
    viewer.get_by_role("button", name="Close").click()
    viewer.wait_for(state="hidden")

    _active(page).get_by_role("button", name="Open plan.docx").click()
    other = viewer.get_by_role("button", name="Open in another app")
    other.wait_for(timeout=10000)
    _shot(page, "31-file-other")
    with page.expect_download() as download:
        other.click()
    assert download.value.suggested_filename == "plan.docx"


def test_theme_choice(phone: Any, mobile_app: MobileApp) -> None:
    """Settings, Appearance: Dark overrides a light phone and survives a
    restart; System follows the phone again."""
    page = phone
    _connect(page, mobile_app)

    def dark() -> bool:
        return bool(
            page.evaluate("document.documentElement.classList.contains('ion-palette-dark')")
        )

    assert not dark(), "the phone is light, and the app follows it by default"
    page.locator("ion-tab-button[tab='settings']").click()
    _active(page).get_by_text("Appearance").wait_for()
    _active(page).locator("ion-segment-button").filter(has_text="Dark").click()
    harness._wait_until(dark, 5, "the dark theme")
    _active(page).get_by_text("Always dark, whatever your phone is set to.").wait_for()
    _shot(page, "19-settings-dark")

    # The choice is kept on the phone: a restart opens dark from the first frame.
    page.goto(mobile_app.base_url, wait_until="domcontentloaded")
    assert dark()
    _open_agent(page, "nova")
    _active(page).get_by_text("is the cheapest at 4.10").wait_for()
    bubble = page.evaluate(
        "getComputedStyle(document.querySelector('.olano-bubble.agent')).backgroundColor"
    )
    assert bubble == "rgb(38, 38, 42)", bubble

    page.locator("ion-tab-bar").wait_for(state="hidden")
    _active(page).locator("ion-back-button").click()
    page.locator("ion-tab-button[tab='settings']").click()

    # Volt is the dark palette plus its own class and colours: the agent's
    # bubble turns the volt grey, the brand colour the chartreuse with dark
    # type on it (white on chartreuse is unreadable), and the phone's setting
    # still has no say.
    def volt() -> bool:
        return bool(
            page.evaluate("document.documentElement.classList.contains('olano-theme-volt')")
        )

    _active(page).locator("ion-segment-button").filter(has_text="Volt").click()
    harness._wait_until(lambda: dark() and volt(), 5, "the volt theme")
    _active(page).get_by_text("Always black with a bright chartreuse accent").wait_for()
    primary = page.evaluate(
        "getComputedStyle(document.documentElement).getPropertyValue('--ion-color-primary').trim()"
    )
    assert primary == "#d4f24a", primary
    contrast = page.evaluate(
        "getComputedStyle(document.documentElement).getPropertyValue('--ion-color-primary-contrast').trim()"
    )
    assert contrast == "#0a0a0a", contrast
    _shot(page, "19b-settings-volt")
    page.locator("ion-tab-button[tab='chats']").click()
    _open_agent(page, "nova")
    _active(page).get_by_text("is the cheapest at 4.10").wait_for()
    bubble = page.evaluate(
        "getComputedStyle(document.querySelector('.olano-bubble.agent')).backgroundColor"
    )
    assert bubble == "rgb(29, 29, 29)", bubble
    page.locator("ion-tab-bar").wait_for(state="hidden")
    _active(page).locator("ion-back-button").click()
    page.locator("ion-tab-button[tab='settings']").click()

    _active(page).locator("ion-segment-button").filter(has_text="System").click()
    harness._wait_until(lambda: not dark() and not volt(), 5, "the phone's light theme again")
    _active(page).get_by_text("Follows your phone's light or dark setting.").wait_for()


def test_starters_and_message_menu(phone: Any, mobile_app: MobileApp) -> None:
    """An empty chat offers ways to start; a message pressed long (here, a
    right click) offers Copy; days are labelled."""
    page = phone
    _connect(page, mobile_app)
    _new_conversation(page, "aria")
    _active(page).get_by_text("Say hello to aria").wait_for()
    _shot(page, "20-starters")
    _active(page).get_by_role("listitem").filter(has_text="What can you help me with?").click()
    _active(page).locator(".olano-msg.user").get_by_text("What can you help me with?").wait_for()
    _active(page).get_by_text("Meridian is cheapest at 4.10 a unit.").wait_for(timeout=20000)
    _active(page).get_by_role("separator").filter(has_text="Today").wait_for()

    _active(page).locator(".olano-bubble.agent").last.click(button="right")
    sheet = page.locator("ion-action-sheet")
    sheet.get_by_role("button", name="Copy").wait_for()
    _shot(page, "21-message-menu")
    sheet.get_by_role("button", name="Copy").click()
    page.get_by_text("Message copied").wait_for()

    # The buttons under a reply and under your latest message.
    reply = _active(page).locator(".olano-msg.agent").last
    reply.get_by_role("button", name="Copy reply").click()
    reply.get_by_role("button", name="Copied").wait_for()
    mine = _active(page).locator(".olano-msg.user").last
    _shot(page, "26-message-buttons")
    mine.get_by_role("button", name="Edit message").click()
    box = _active(page).locator("ion-textarea textarea")
    assert box.input_value() == "What can you help me with?"
    _active(page).get_by_text("Editing your message").wait_for()
    box.fill("What can you help me with this week?")
    _active(page).get_by_role("button", name="Send").click()
    _active(page).locator(".olano-msg.user").get_by_text(
        "What can you help me with this week?"
    ).wait_for()
    _active(page).get_by_text("Editing your message").wait_for(state="detached")
    # Sent as a new message: the first one is still there.
    assert _active(page).locator(".olano-msg.user").count() == 2


def test_a_reply_can_be_reported(phone: Any, mobile_app: MobileApp) -> None:
    """A reply's menu has Report: the reason goes to the deployment as a
    thumbs-down its administrators see (POST /api/chat/feedback), the
    in-app way to flag what an agent wrote that App Review 1.2 and Google
    Play's AI-generated content policy ask for."""
    home = Path(os.environ["LIONCLAW_HOME"])
    sessions = home / "sessions" / "nova"
    sessions.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"ts": now, "role": "user", "content": "Summarise the supplier list"},
        {
            "ts": now,
            "role": "assistant",
            "content": "A reply worth reporting.",
            "turn_id": "turn-report-1",
        },
    ]
    (sessions / "report-me.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )

    page = phone
    _connect(page, mobile_app)
    page.goto(f"{mobile_app.base_url}/chats/nova/report-me", wait_until="domcontentloaded")
    _active(page).get_by_text("A reply worth reporting.").wait_for(timeout=20000)
    _active(page).locator(".olano-bubble.agent").last.click(button="right")
    page.locator("ion-action-sheet").get_by_role("button", name="Report").click()
    reasons = page.locator("ion-action-sheet", has_text="Report this reply")
    reasons.get_by_role("button", name="Inaccurate or misleading").click()
    page.get_by_text("Reported. Thank you.").wait_for()

    votes = [
        json.loads(line)
        for line in (sessions / "report-me.feedback.ndjson").read_text().splitlines()
    ]
    assert votes[-1]["turn_id"] == "turn-report-1"
    assert votes[-1]["value"] == -1
    assert votes[-1]["comment"] == "Reported in the app: Inaccurate or misleading"


def test_a_stopped_agent_says_so_instead_of_taking_messages(
    phone: Any, mobile_app: MobileApp
) -> None:
    """An agent an admin stopped shows why where the composer was, with Start
    for an admin, instead of a "Say hello" screen that takes a message and then
    reports the agent missing."""
    from olano.config.agent_state import set_agent_paused

    page = phone
    set_agent_paused("aria", True)
    try:
        _connect(page, mobile_app)
        row = _agent_row(page, "aria")
        row.locator(".olano-row-state", has_text="Stopped").wait_for(timeout=20000)
        _active(page).get_by_text("aria", exact=True).click()
        page.wait_for_url(re.compile(r"/chats/aria/[^/]+$"))
        _active(page).get_by_text("aria is stopped").first.wait_for(timeout=20000)
        banner = _active(page).locator(".olano-unavailable")
        banner.get_by_role("button", name="Start").wait_for()
        assert _active(page).locator("ion-textarea textarea").count() == 0
        assert _active(page).get_by_text("Say hello to aria").count() == 0
        _shot(page, "40-stopped-agent")
    finally:
        set_agent_paused("aria", False)


def test_try_again_after_a_failed_reply(phone: Any, mobile_app: MobileApp) -> None:
    """A reply that failed offers Try again, which sends the message again."""
    page = phone
    _connect(page, mobile_app)
    _new_conversation(page, "nova")
    box = _active(page).locator("ion-textarea textarea")
    box.fill("Check the stock levels [fail once]")
    _active(page).get_by_role("button", name="Send").click()
    _active(page).locator(".olano-msg-error").wait_for(timeout=20000)
    _shot(page, "27-try-again")
    _active(page).get_by_role("button", name="Try again").click()
    _active(page).get_by_text("Meridian is cheapest at 4.10 a unit.").wait_for(timeout=20000)
    _active(page).get_by_role("button", name="Try again").wait_for(state="detached")


def test_sign_in_and_out(phone: Any, mobile_app: MobileApp) -> None:
    """A managed deployment requires an account: the app signs in with the
    dashboard's email and password, and signing out comes back here."""
    from olano.config.home import update_home_config
    from olano.users.store import get_user_store

    update_home_config({"multi_user_enabled": True})
    get_user_store().create_user(
        "sam@example.com", "correct horse battery", name="Sam Rivera", is_admin=True
    )

    page = phone
    page.get_by_text("Connect to your Olano").wait_for()
    page.locator("ion-input input").fill(mobile_app.base_url)
    page.get_by_role("button", name="Continue").click()
    page.get_by_text("Sign in", exact=True).first.wait_for(timeout=20000)
    page.locator("ion-input input[type='email']").fill("sam@example.com")
    page.locator("ion-input input[type='password']").fill("wrong password")
    page.get_by_role("button", name="Sign in").click()
    page.get_by_text("don't match an account").wait_for()
    _shot(page, "10-sign-in")

    page.locator("ion-input input[type='password']").fill("correct horse battery")
    page.get_by_role("button", name="Sign in").click()
    _active(page).get_by_text("nova", exact=True).wait_for(timeout=20000)

    page.locator("ion-tab-button[tab='settings']").click()
    _active(page).get_by_text("Sam Rivera").wait_for()
    _active(page).get_by_text("sam@example.com").wait_for()
    _active(page).get_by_text("Sign out").click()
    page.locator("button.action-sheet-destructive").click()
    page.locator("ion-input input[type='password']").wait_for(timeout=15000)


def _serve_app_config(page: Any, config: dict) -> None:
    body = json.dumps(config)
    page.route(
        APP_CONFIG_URL,
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
            body=body,
        ),
    )


def test_try_the_demo(phone: Any, mobile_app: MobileApp, monkeypatch: pytest.MonkeyPatch) -> None:
    """The connect screen's "Try the demo" opens the demo deployment signed in
    as a demo member of the visitor's own, with no password typed: what App
    Review and a prospect without a deployment get (mobile/store/DEMO.md).
    The remote config names the demo and can take the entry away; an expired
    demo session is renewed from the sign-in screen with one tap."""
    from olano.config.home import update_home_config
    from olano.users.store import get_user_store

    update_home_config({"multi_user_enabled": True})
    store = get_user_store()
    store.create_user("owner@example.com", "correct horse battery", name="Owner", is_admin=True)
    template = store.create_user("demo@olano.run", "never typed in the app", name="Demo")
    store.db.grant_access(template.id, "nova")
    monkeypatch.setenv("OLANO_DEMO_ACCOUNT", "demo@olano.run")

    page = phone
    _serve_app_config(
        page,
        {
            "version": 1,
            "demo": {
                "enabled": True,
                "url": "https://demo.olano.run",
                "label": "Try the Olano demo",
                "accountHint": "A demo with sample agents, under an account of your own.",
            },
            "minSupportedVersion": None,
        },
    )
    page.reload(wait_until="domcontentloaded")
    page.get_by_text("Connect to your Olano").wait_for()
    page.get_by_text("A demo with sample agents, under an account of your own.").wait_for()
    _shot(page, "30-connect-demo")
    page.get_by_role("button", name="Try the Olano demo").click()
    _active(page).get_by_text("nova", exact=True).wait_for(timeout=20000)

    page.locator("ion-tab-button[tab='settings']").click()
    # A visitor account of its own, holding the template's agents as a member.
    _active(page).get_by_text("Demo visitor").wait_for()
    _active(page).get_by_text("You are in the Olano demo").wait_for()
    _shot(page, "31-settings-demo")

    # The session ran out (the nightly reset clears them): one tap renews it.
    for user in store.db.list_users():
        store.db.delete_sessions_for_user(user.id)
    page.reload(wait_until="domcontentloaded")
    page.get_by_role("button", name="Enter the demo again").click()
    # Back where the person was: Settings, signed in again.
    _active(page).get_by_text("You are in the Olano demo").wait_for(timeout=20000)

    # Switching the entry off in the remote config hides it for the next visit.
    _active(page).get_by_text("Switch deployment", exact=True).click()
    page.locator("button.action-sheet-destructive").click()
    page.get_by_text("Connect to your Olano").wait_for()
    page.unroute(APP_CONFIG_URL)
    _serve_app_config(page, {"version": 1, "demo": {"enabled": False}})
    page.evaluate("localStorage.removeItem('olano.appConfig')")
    page.reload(wait_until="domcontentloaded")
    page.get_by_text("Connect to your Olano").wait_for()
    page.wait_for_function("() => localStorage.getItem('olano.appConfig') !== null")
    # Until the answer lands the bundled copy offers the demo; then it goes.
    page.get_by_role("button", name="Try the demo").wait_for(state="detached")
    page.get_by_text("No deployment of your own yet?").wait_for(state="detached")


def test_dark_mode(mobile_app: MobileApp) -> None:
    """The app follows the phone's dark setting (Ionic's dark palette)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - environment dependent
        pytest.skip("playwright not installed")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 393, "height": 852},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            user_agent=IPHONE_UA,
            color_scheme="dark",
        )
        page = context.new_page()
        try:
            page.goto(mobile_app.base_url, wait_until="domcontentloaded")
            _connect(page, mobile_app)
            _shot(page, "11-chats-dark")
            _open_agent(page, "nova")
            _active(page).get_by_text("is the cheapest at 4.10").wait_for()
            bubble = page.evaluate(
                "getComputedStyle(document.querySelector('.olano-bubble.agent')).backgroundColor"
            )
            # The agent bubble's dark colour (--olano-bubble-agent, #26262a).
            assert bubble == "rgb(38, 38, 42)", bubble
            _shot(page, "12-conversation-dark")
            # Grouped screens: the cards a step lighter than the page.
            _active(page).locator("ion-back-button").click()
            page.locator("ion-tab-button[tab='cortex']").click()
            _active(page).get_by_text("Bundle shipping with the spring order").wait_for(
                timeout=15000
            )
            card = page.evaluate(
                "getComputedStyle(document.querySelector('.ion-page:not(.ion-page-hidden) ion-list.list-inset ion-item')).getPropertyValue('--background').trim()"
            )
            assert card in {"#1c1c1e", "var(--olano-card)"}, card
            _shot(page, "22-cortex-dark")
            page.locator("ion-tab-button[tab='inbox']").click()
            _active(page).get_by_text("Nothing to approve").wait_for(timeout=15000)
            _shot(page, "23-inbox-dark")
        finally:
            context.close()
            browser.close()


def _wait_for_socket(page: Any, predicate: Any, what: str, timeout_s: float = 10) -> None:
    """Wait for a message the page sends over a routed WebSocket.

    The sync API hands such a message to its handler only while a Playwright
    call is running, so a ``time.sleep`` loop (``harness._wait_until``) never
    sees one that arrives after the action that caused it returned: on a slow
    runner the click's message landed just after ``mouse.click`` and the wait
    timed out with the message queued. ``wait_for_timeout`` lets it through.
    """
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out after {timeout_s:.0f}s waiting for {what}")
        page.wait_for_timeout(100)


def _section_names(page: Any) -> list[str]:
    """The Chats list's sections, top to bottom, with the agents each shows."""
    out = []
    for section in _active(page).locator("section.olano-chat-group").all():
        title = section.locator(".olano-chat-section-title")
        heading = (title.text_content() or "") if title.count() else ""
        names = section.locator(".olano-row-top h2").all_inner_texts()
        out.append(f"{heading}: {', '.join(n.strip() for n in names)}")
    return out


def _menu_choice(page: Any, label: str) -> None:
    """Picks a row in the conversation's menu sheet (ui/MenuSheet.tsx) and
    waits for the sheet to go: the next sheet or screen opens after it."""
    # Each conversation page in the stack holds its own (closed) sheet.
    sheet = page.locator("ion-modal.olano-menu-sheet.show-modal")
    sheet.wait_for()
    _shot(page, "menu-sheet")
    sheet = page.locator(f"ion-modal#{sheet.get_attribute('id')}")
    row = sheet.locator("ion-item").filter(
        has=page.locator(".olano-menu-label").get_by_text(label, exact=True)
    )
    row.first.wait_for()
    row.first.click()
    sheet.wait_for(state="hidden")


def _sheet_choice(page: Any, name: str) -> None:
    """Picks a choice in the open action sheet and waits for it to close: a
    tap that lands while a sheet slides away reaches nothing."""
    button = page.locator("ion-action-sheet").get_by_role("button", name=name, exact=True)
    button.wait_for()
    sheet_id = button.evaluate("b => b.closest('ion-action-sheet').id")
    button.click()
    page.locator(f"ion-action-sheet#{sheet_id}").wait_for(state="detached")


def _agent_row(page: Any, name: str) -> Any:
    return (
        _active(page)
        .locator("ion-item.olano-row")
        .filter(has=page.locator("h2", has_text=re.compile(rf"^{name}$")))
    )


def test_chats_arranged_in_pinned_folders_and_all(phone: Any, mobile_app: MobileApp) -> None:
    """Agents arrange the way the dashboard's sidebar does: pinned first, the
    person's own folders, then All agents. The arrangement is kept on the
    deployment for the person, so it comes back after a reload."""
    page = phone
    _connect(page, mobile_app)

    # Pin nova from its menu (press and hold; a right click here).
    _agent_row(page, "nova").click(button="right")
    _sheet_choice(page, "Pin")
    _active(page).locator("section[data-section='pinned']").wait_for()

    # A new folder, then atlas moved into it.
    _active(page).get_by_label("New folder or group chat").first.click()
    _sheet_choice(page, "New Folder")
    page.locator("ion-alert input").fill("Research")
    page.locator("ion-alert").get_by_role("button", name="Save").click()
    _active(page).get_by_text("Empty. Swipe an agent").wait_for()
    _agent_row(page, "atlas").click(button="right")
    _sheet_choice(page, "Move to…")
    _sheet_choice(page, "Research")
    _active(page).get_by_text("Empty. Swipe an agent").wait_for(state="detached")
    _shot(page, "30-chats-arranged")

    sections = _section_names(page)
    assert sections[:3] == ["Pinned: nova", "Research: atlas", "All agents: aria, merli"], sections

    # Folding a section hides its agents and is remembered too.
    _active(page).get_by_role("button", name="Research").first.click()
    _agent_row(page, "atlas").wait_for(state="detached")

    # Kept on the deployment, for this person.
    def stored() -> dict:
        prefs = page.request.get(mobile_app.base_url + "/api/auth/me/prefs").json()["prefs"]
        return prefs.get("mobile_chat_arrangement") or {}

    harness._wait_until(lambda: bool(stored().get("folded")), 10, "the arrangement to be saved")
    saved = stored()
    folder = saved["groups"][0]
    assert folder["name"] == "Research"
    assert saved["assign"] == {"atlas": folder["id"]}
    assert saved["pins"] == ["nova"]
    assert saved["folded"] == [folder["id"]]

    # A phone without its own copy (another phone, a reinstall) reads it back.
    page.evaluate(
        "() => Object.keys(localStorage)"
        ".filter((k) => k.startsWith('olano.chats.arrangement:'))"
        ".forEach((k) => localStorage.removeItem(k))"
    )
    page.reload(wait_until="domcontentloaded")
    _active(page).locator("section[data-section='pinned']").wait_for(timeout=20000)
    assert _section_names(page)[:3] == ["Pinned: nova", "Research: ", "All agents: aria, merli"]

    # Searching opens a folded folder to show a match.
    _active(page).locator("ion-searchbar input").fill("atl")
    _agent_row(page, "atlas").wait_for()
    _active(page).locator("ion-searchbar input").fill("")

    # Deleting the folder puts atlas back under All agents.
    _active(page).get_by_label("Research folder options").click()
    _sheet_choice(page, "Delete Folder")
    page.locator("ion-alert").get_by_role("button", name="Delete").click()
    _agent_row(page, "atlas").wait_for()
    assert _section_names(page)[:2] == ["Pinned: nova", "All agents: aria, atlas, merli"]


def test_group_chat(phone: Any, mobile_app: MobileApp) -> None:
    """A group chat from the Chats tab: pick the agents, address two of them
    with @, each answers under its own name; rename it, add a member, delete it."""
    page = phone
    resp = page.request.put(
        mobile_app.base_url + "/api/system/config", data={"groups": {"enabled": True}}
    )
    assert resp.ok, resp.text()
    _connect(page, mobile_app)

    _active(page).get_by_text("Start a group chat with several agents").click()
    sheet = page.locator("ion-modal.olano-new-group")
    sheet.get_by_text("Pick at least 2 agents").wait_for()
    sheet.locator("ion-item", has_text="nova").click()
    sheet.locator("ion-item", has_text="aria").click()
    sheet.get_by_text("nova answers messages that don't name anyone").wait_for()
    sheet.locator("ion-input input").fill("Launch team")
    _shot(page, "31-new-group")
    sheet.get_by_role("button", name="Create").click()

    chat = _active(page)
    chat.get_by_text("Say hello to Launch team").wait_for(timeout=20000)
    box = chat.locator("ion-textarea textarea")
    box.fill("@")
    chat.locator(".olano-mention", has_text="aria").click()
    assert box.input_value() == "@aria "
    box.fill("@aria and @nova, which supplier should we use?")
    chat.get_by_text("Answering: aria and nova").wait_for()
    chat.get_by_role("button", name="Send").click()

    chat.get_by_text("To aria and nova").wait_for()
    # Each answers in turn, under its own name.
    harness._wait_until(
        lambda: chat.locator(".olano-msg.agent .olano-bubble").count() >= 2, 30, "two replies"
    )
    authors = chat.locator(".olano-author-name").all_inner_texts()
    assert [a.strip() for a in authors] == ["aria", "nova"], authors
    chat.get_by_role("button", name="Send").wait_for()
    _shot(page, "32-group-chat")

    # Details: rename, add a member.
    chat.get_by_label("Group info").first.click()
    info = _active(page)
    info.get_by_text("Answers messages that don't name anyone").wait_for()
    info.get_by_text("Name", exact=True).click()
    page.locator("ion-alert input").fill("Launch crew")
    page.locator("ion-alert").get_by_role("button", name="Save").click()
    info.locator(".olano-agent-hero h2", has_text="Launch crew").wait_for()
    info.get_by_text("Add an agent").click()
    _sheet_choice(page, "atlas")
    info.get_by_text("3 agents").wait_for()
    _shot(page, "33-group-info")

    # The Chats tab lists it with its last message.
    page.locator("ion-tab-button[tab='chats']").click()
    row = _active(page).locator("section[data-section='groups'] ion-item", has_text="Launch crew")
    row.wait_for(timeout=20000)
    row.get_by_text("nova: Three suppliers ship within a week").wait_for(timeout=20000)

    # Deleting it takes it off the list.
    row.click()
    _active(page).get_by_label("Group info").first.click()
    _active(page).get_by_text("Delete group chat").click()
    page.locator("ion-alert").get_by_role("button", name="Delete").click()
    _active(page).get_by_text("Start a group chat with several agents").wait_for(timeout=20000)


def test_links_in_messages_open(phone: Any, mobile_app: MobileApp) -> None:
    """Every address in a conversation is a link: a bare URL, one in code
    formatting, a www. address, a Markdown link, and the ones in your own
    messages. Markup in a reply is text. Tapping a link opens it outside the
    app's own page; pressing and holding one offers Copy Link."""
    sessions = Path(os.environ["LIONCLAW_HOME"]) / "sessions" / "nova"
    now = datetime.now(timezone.utc).isoformat()
    reply = (
        "Sign in at `https://acme.example.com/connect/abc`, the docs are at www.example.org/docs, "
        "the report is [here](https://example.com/report). Raw: https://example.net/a?b=1&c=2. "
        'Not markup: <img src=x onerror="window.__xss=1"><b>bold</b>'
    )
    with (sessions / "links.jsonl").open("w", encoding="utf-8") as fh:
        for row in (
            {
                "ts": now,
                "role": "user",
                "content": "Is https://shop.example.com/item/42 still in stock?",
            },
            {"ts": now, "role": "assistant", "content": reply},
        ):
            fh.write(json.dumps(row) + "\n")

    page = phone
    _connect(page, mobile_app)
    page.evaluate(
        "() => { window.__opened = []; window.open = (u) => { window.__opened.push(String(u)); return null; }; }"
    )
    _open_threads(page, "nova")
    _thread_row(page, "Is https://shop.example.com/item/42").click()
    chat = _active(page)
    bubble = chat.locator(".olano-bubble.agent").last
    bubble.get_by_text("bold").wait_for()
    hrefs = [a.get_attribute("href") for a in bubble.locator("a").all()]
    assert hrefs == [
        "https://acme.example.com/connect/abc",
        "https://www.example.org/docs",
        "https://example.com/report",
        "https://example.net/a?b=1&c=2",
    ], hrefs
    assert bubble.locator("img, b").count() == 0
    assert "<b>bold</b>" in bubble.inner_text()
    _shot(page, "34-links")

    bubble.locator("a", has_text="www.example.org/docs").click()
    mine = chat.locator(".olano-bubble.user").last.locator("a.olano-link")
    assert mine.get_attribute("href") == "https://shop.example.com/item/42"
    mine.click()
    assert page.evaluate("window.__opened") == [
        "https://www.example.org/docs",
        "https://shop.example.com/item/42",
    ]
    assert page.evaluate("window.__xss") is None
    # The page itself never navigated away.
    assert page.url.startswith(mobile_app.base_url)

    bubble.locator("a", has_text="here").click(button="right")
    sheet = page.locator("ion-action-sheet")
    sheet.get_by_role("button", name="Copy Link").wait_for()
    sheet.get_by_role("button", name="Cancel").click()


def test_magic_links(phone: Any, mobile_app: MobileApp) -> None:
    """A magic link an agent sent shows as a card built from the tool's result
    (the agent is told not to paste the link): Open copies the PIN and opens
    the page outside the app. When the person finishes there, the deployment
    reports it into the conversation, live, with a restart where one is
    needed; the transcript carries the same card after a reload."""
    sessions = Path(os.environ["LIONCLAW_HOME"]) / "sessions" / "nova"
    now = datetime.now(timezone.utc).isoformat()
    link = "https://acme.example.com/api/connect/Zk3x9QpLm2"
    result = {
        "id": "ml_1",
        "url": link,
        "pin": "482913",
        "flow": "google",
        "label": "Connect Google",
        "ttl_minutes": 15,
    }
    rows = [
        {"ts": now, "role": "user", "content": "Connect my Google account please"},
        {
            "ts": now,
            "role": "tool_call",
            "content": 'magic_link_connect_service: {"service": "google"}',
            "tool_call_id": "m1",
        },
        {
            "ts": now,
            "role": "tool_result",
            "content": "magic_link_connect_service: " + json.dumps(result),
            "tool_call_id": "m1",
        },
        {
            "ts": now,
            "role": "assistant",
            "content": "I made a sign-in link. Tap Open below and enter the PIN.",
        },
    ]
    path = sessions / "magic.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    page = phone
    _connect(page, mobile_app)
    page.evaluate(
        "() => { window.__opened = []; window.open = (u) => { window.__opened.push(String(u)); return null; }; }"
    )
    _open_threads(page, "nova")
    _thread_row(page, "Connect my Google account").click()
    chat = _active(page)
    card = chat.locator(".olano-connect")
    card.get_by_text("Connect Google").wait_for()
    card.get_by_text("acme.example.com").wait_for()
    card.get_by_text("482 913").wait_for()
    chat.get_by_text("Made a sign-in link").count()  # the step, folded into "1 step"
    _shot(page, "35-magic-link")
    card.get_by_role("button", name="Open").click()
    page.get_by_text("PIN 482 913 copied").wait_for()
    assert page.evaluate("window.__opened") == [link]

    # The person finishes on the page: the deployment reports it, live.
    page.route(
        "**/api/agents/nova/restart",
        lambda route: route.fulfill(status=200, content_type="application/json", body="{}"),
    )
    outcome = {
        "agent": "nova",
        "thread_id": "magic",
        "link_id": "ml_1",
        "status": "done",
        "flow": "google",
        "label": "Connect Google",
        "target_agent": "nova",
        "apply": "restart_needed",
        "text": "\u2705 **Google** is connected for nova.",
    }
    mobile_app.push_event("magic_link_updated", outcome)
    done = chat.locator(".olano-connect-result")
    done.get_by_text("Connected", exact=True).wait_for(timeout=15000)
    done.get_by_text("Google is connected for nova.").wait_for()
    done.get_by_text("nova needs a restart to pick this up.").wait_for()
    _shot(page, "36-magic-link-done")
    done.get_by_role("button", name="Restart nova").click()
    done.get_by_text("nova restarted.").wait_for()
    # The same event again (a reconnect replays nothing, but be safe) adds nothing.
    mobile_app.push_event("magic_link_updated", outcome)
    page.wait_for_timeout(800)
    assert chat.locator(".olano-connect-result").count() == 1

    # After a reload the transcript's own row draws the card.
    with path.open("a", encoding="utf-8") as fh:
        row = {"ts": now, "role": "system", "event": "magic_link", "content": outcome["text"]}
        row.update(
            {k: outcome[k] for k in ("link_id", "status", "flow", "label", "target_agent", "apply")}
        )
        fh.write(json.dumps(row) + "\n")
    page.reload(wait_until="domcontentloaded")
    _active(page).locator(".olano-connect-result").get_by_text(
        "Google is connected for nova."
    ).wait_for(timeout=20000)
    assert _active(page).locator(".olano-connect").count() == 1


def _page_jpeg() -> bytes:
    """A frame of the agent's browser: a login page, roughly."""
    import io

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1280, 800), (245, 246, 248))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1280, 70), fill=(32, 64, 120))
    draw.rectangle((440, 220, 840, 580), fill=(255, 255, 255), outline=(210, 210, 215), width=2)
    for top in (300, 380):
        draw.rectangle((480, top, 800, top + 48), outline=(180, 180, 190), width=2)
    draw.rectangle((480, 470, 800, 522), fill=(255, 106, 61))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=70)
    return out.getvalue()


def test_live_browser_watch_and_take_control(phone: Any, mobile_app: MobileApp) -> None:
    """With the live browser on, an agent's browser shows as a floating window
    over the conversation; the full view takes control (taps, typing, the
    page's own dialogs) and hands it back, which tells the agent. The socket
    here is Playwright's stand-in for the deployment's, speaking its protocol."""
    page = phone
    status = {
        "session": {
            "agent": "nova",
            "kind": "live",
            "opened_at": 1760000000.0,
            "controller": "agent",
            "attention": "Sign in to the supplier portal",
            "viewers": 0,
            "url": "https://portal.example.com/login",
        },
        "live_view": True,
        "availability": {"mode": "live", "enabled": True},
    }
    page.route(
        "**/api/agents/nova/browser",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(status)
        ),
    )
    sent: list[dict] = []
    sockets: list[Any] = []
    state = {
        "controller": "agent",
        "attention": "Sign in to the supplier portal",
        "url": "https://portal.example.com/login",
        "loading": False,
        "can_back": False,
        "can_forward": False,
        "tabs": [
            {
                "id": "t1",
                "url": "https://portal.example.com/login",
                "title": "Log in",
                "active": True,
            }
        ],
        "dialog": None,
        "chooser": None,
    }
    frame = _page_jpeg()

    def push_state(ws: Any, **changes: Any) -> None:
        state.update(changes)
        ws.send(json.dumps({"type": "state", "session": state}))

    def on_message(ws: Any, message: Any) -> None:
        data = json.loads(message)
        sent.append(data)
        if data.get("t") == "control":
            push_state(
                ws,
                controller=data["who"],
                attention=None if data["who"] == "agent" else state["attention"],
            )

    def live(ws: Any) -> None:
        sockets.append(ws)
        ws.on_message(lambda message: on_message(ws, message))
        push_state(ws)
        ws.send(frame)
        ws.send(
            json.dumps({"type": "pointer", "x": 0.5, "y": 0.62, "kind": "move", "at": time.time()})
        )

    page.route_web_socket("**/api/agents/nova/browser/live", live)
    # Sockets are routed in pages loaded after the route is set.
    page.reload(wait_until="domcontentloaded")

    _connect(page, mobile_app)
    _new_conversation(page, "nova")
    chat = _active(page)
    pip = chat.locator(".olano-pip")
    pip.wait_for(timeout=15000)
    pip.get_by_text("The agent needs you").wait_for()
    pip.locator("img").wait_for()
    # The viewer paces the stream: it says so (and how big its picture is),
    # and answers the frame once drawn.
    _wait_for_socket(
        page, lambda: any(m.get("t") == "view" and m.get("ack") is True for m in sent), "the view"
    )
    _wait_for_socket(page, lambda: {"t": "shown"} in sent, "the frame's acknowledgement")
    sized = [m for m in sent if m.get("t") == "view" and m.get("w")]
    assert sized and sized[-1]["w"] > 0 and sized[-1]["h"] > 0
    _shot(page, "37-browser-pip")

    pip.click()
    viewer = page.locator("ion-modal.olano-browser-view")
    viewer.get_by_text("The agent asked for you: Sign in to the supplier portal").wait_for()
    viewer.get_by_text("portal.example.com", exact=True).wait_for()
    viewer.locator(".olano-live-picture").wait_for()
    _shot(page, "38-browser-watching")

    # Watching sends nothing; taking control does.
    viewer.get_by_role("button", name="Take control").click()
    viewer.get_by_role("button", name="Hand back").wait_for()
    assert sent[-1] == {"t": "control", "who": "user"}

    # A tap in the middle of the picture is a click in the middle of the page.
    box = viewer.locator(".olano-live-picture").bounding_box()
    assert box
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] * 0.62)
    _wait_for_socket(page, lambda: any(m.get("t") == "up" for m in sent), "the click")
    clicks = [m for m in sent if m.get("t") in ("move", "down", "up")]
    assert [m["t"] for m in clicks[-3:]] == ["move", "down", "up"]
    assert abs(clicks[-1]["x"] - 0.5) < 0.02 and abs(clicks[-1]["y"] - 0.62) < 0.02
    assert clicks[-1]["button"] == "left" and clicks[-1]["count"] == 1

    # Typing goes in as text; the keys as keys.
    viewer.get_by_role("button", name="Keyboard").click()
    viewer.locator(".olano-browser-keys input").fill("sam@example.com")
    viewer.get_by_role("button", name="Send the text").click()
    viewer.get_by_role("button", name="Enter").click()
    _wait_for_socket(page, lambda: {"t": "keyup", "key": "Enter"} in sent, "the keys")
    assert {"t": "text", "text": "sam@example.com"} in sent
    _shot(page, "39-browser-in-control")

    # The page asks something: the person answers it.
    push_state(
        sockets[-1],
        dialog={"id": "d1", "kind": "confirm", "message": "Stay signed in on this device?"},
    )
    page.locator("ion-alert").get_by_text("Stay signed in on this device?").wait_for()
    page.locator("ion-alert").get_by_role("button", name="OK").click()
    _wait_for_socket(
        page, lambda: {"t": "dialog", "id": "d1", "accept": True} in sent, "the answer"
    )
    push_state(sockets[-1], dialog=None)

    # A tap landed on a date field: the browser's calendar is never in the
    # picture, so the phone's own picker asks, and the value goes back.
    sockets[-1].send(
        json.dumps(
            {
                "type": "picker",
                "kind": "date",
                "value": "2026-03-04",
                "min": "2026-01-01",
                "max": "2026-12-31",
                "label": "Travel date",
            }
        )
    )
    page.locator("ion-alert").get_by_text("Travel date").wait_for()
    page.locator("ion-alert input").fill("2026-05-06")
    page.locator("ion-alert").get_by_role("button", name="Set").click()
    _wait_for_socket(page, lambda: {"t": "setvalue", "value": "2026-05-06"} in sent, "the date")
    # What the page asked the browser for and could not have is explained.
    sockets[-1].send(
        json.dumps(
            {
                "type": "notice",
                "message": "The page asked for your location. The agent's browser has no location to give, so the request was declined.",
            }
        )
    )
    page.get_by_text("The page asked for your location.", exact=False).wait_for(timeout=10000)

    # Record the browser: the deployment says a recording runs (the state
    # frame carries it, so every viewer shows REC), and Stop hands back the
    # file, which the toast names with a way to it; a picture the same way.
    captures: list[dict] = []

    def capture(route: Any) -> None:
        body = json.loads(route.request.post_data or "{}")
        captures.append({"path": route.request.url.rsplit("/", 1)[-1], **body})
        if body.get("action") == "start":
            push_state(
                sockets[-1],
                recording={
                    "since": time.time(),
                    "seconds": 0,
                    "name": "browser-20261018-191502.mp4",
                },
            )
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "recording": {
                            "since": time.time(),
                            "seconds": 0,
                            "name": "browser-20261018-191502.mp4",
                        },
                        "file": None,
                    }
                ),
            )
            return
        if body.get("action") == "stop":
            push_state(sockets[-1], recording=None)
            file = {
                "path": "/w/recordings/browser-20261018-191502.mp4",
                "relative": "recordings/browser-20261018-191502.mp4",
                "name": "browser-20261018-191502.mp4",
                "bytes": 3 * 1024 * 1024,
                "seconds": 42,
                "url": "",
                "files_url": "",
            }
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"recording": None, "file": file}),
            )
            return
        file = {
            "path": "/w/screenshots/live-1.png",
            "relative": "screenshots/live-1.png",
            "name": "live-1.png",
            "bytes": 20480,
            "seconds": 0,
            "url": "",
            "files_url": "",
        }
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"file": file}))

    page.route("**/api/agents/nova/browser/recording", capture)
    page.route("**/api/agents/nova/browser/picture", capture)
    viewer.get_by_role("button", name="Record the browser").click()
    viewer.get_by_text(re.compile(r"^REC \d:\d\d$")).wait_for(timeout=10000)
    viewer.get_by_role("button", name="Stop recording").click()
    page.get_by_text("Saved browser-20261018-191502.mp4 in recordings (0:42, 3.0 MB).").wait_for(
        timeout=10000
    )
    assert [c.get("action") for c in captures] == ["start", "stop"]
    # The toast sits under the viewer's header, not on its buttons, so a
    # picture can be taken while it shows; that save gets a toast of its own.
    saved_toast = page.locator("ion-toast .toast-wrapper", has_text="Saved browser-")
    # Measured where it comes to rest, not mid-way through sliding in.
    saved_toast.evaluate("w => Promise.allSettled(w.getAnimations().map(a => a.finished))")
    toast_box = saved_toast.bounding_box()
    header_box = viewer.locator("ion-header").first.bounding_box()
    assert toast_box and header_box
    assert toast_box["y"] >= header_box["y"] + header_box["height"], (toast_box, header_box)
    viewer.get_by_role("button", name="Save a picture of the page").click(timeout=3000)
    page.get_by_text("Saved live-1.png in screenshots (20 KB).").wait_for(timeout=10000)
    assert captures[-1]["path"] == "picture"

    # Handing back after the agent asked for help tells the agent.
    viewer.get_by_role("button", name="Hand back").click()
    viewer.get_by_role("button", name="Take control").wait_for()
    assert {"t": "control", "who": "agent"} in sent
    viewer.get_by_role("button", name="Done").click()
    chat.locator(".olano-msg.user").get_by_text(
        "Done: I've handed the browser back to you."
    ).wait_for(timeout=15000)

    # The window hides with its x; the globe in the header opens the browser again.
    chat.get_by_label("Hide the browser window").click()
    pip.wait_for(state="detached")
    chat.get_by_label("The agent's browser").first.click()
    viewer.get_by_role("button", name="Take control").wait_for()


def _thread_rows(agent: str) -> list[dict]:
    """Every row of the agent's most recently written conversation."""
    home = Path(os.environ["LIONCLAW_HOME"])
    latest = max((home / "sessions" / agent).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return [json.loads(line) for line in latest.read_text(encoding="utf-8").splitlines() if line]


def test_steer_and_queue_while_the_agent_works(phone: Any, mobile_app: MobileApp) -> None:
    """While a reply is coming, Send hands the message to the turn that is
    running (the agent reads it at its next step, and it lands in the
    conversation where it was read); Send after holds a message until the
    reply is done and then sends it as the next turn."""
    page = phone
    _MobileRunner.messages.clear()
    _connect(page, mobile_app)
    _new_conversation(page, "atlas")
    box = _active(page).locator("ion-textarea textarea")
    box.fill("Forecast next quarter")
    _active(page).get_by_role("button", name="Send").click()
    _active(page).get_by_text("HJKLQ").wait_for(timeout=20000)

    box.fill("Use the March numbers")
    _active(page).get_by_text("atlas is working. Send adds this to what it's doing.").wait_for()
    _active(page).get_by_role("button", name="Add to what the agent is doing").click()
    queue = _active(page).locator(".olano-queue")
    queue.get_by_text("The agent reads this at its next step").wait_for()
    box.fill("Then email it to Jo")
    _active(page).get_by_role("button", name="Send after").click()
    queue.get_by_text("Sends when this reply is done").wait_for()
    _shot(page, "40-steer-and-queue")

    # The aside joins the conversation where the agent read it.
    _active(page).locator(".olano-msg.user.steered, .olano-msg.user").filter(
        has_text="Use the March numbers"
    ).get_by_text("Added while it worked").wait_for(timeout=20000)
    _active(page).get_by_text("ZXCVB").first.wait_for(timeout=20000)
    # And the waiting message went as the next turn.
    harness._wait_until(
        lambda: _MobileRunner.messages[-1:] == ["Then email it to Jo"], 20, "the queued message"
    )
    rows = _thread_rows("atlas")
    assert {"role": "user", "content": "Use the March numbers", "steer": True}.items() <= next(
        r for r in rows if r.get("content") == "Use the March numbers"
    ).items()
    _active(page).get_by_text("ZXCVB").nth(1).wait_for(timeout=20000)
    assert queue.count() == 0 or not queue.is_visible()


def test_plan_mode_and_the_conversation_menu(phone: Any, mobile_app: MobileApp) -> None:
    """The conversation menu sets how the agent works: in Plan first it drafts a
    plan and waits, Approve & run executes it; the menu also renames."""
    page = phone
    _MobileRunner.modes.clear()
    _MobileRunner.messages.clear()
    _connect(page, mobile_app)
    _new_conversation(page, "nova")
    _active(page).get_by_role("button", name="Conversation menu").click()
    _menu_choice(page, "Mode")
    _sheet_choice(page, "Plan first")
    _active(page).locator(".olano-mode-bar").get_by_text("Plan first").wait_for()

    _active(page).locator("ion-textarea textarea").fill("Plan the switch to Meridian")
    _active(page).get_by_role("button", name="Send").click()
    approve = _active(page).get_by_role("button", name="Approve & run")
    approve.wait_for(timeout=20000)
    assert _MobileRunner.modes[-1] == "plan"
    _shot(page, "41-plan-mode")
    approve.click()
    harness._wait_until(
        lambda: _MobileRunner.messages[-1:] == ["Approved. Execute the plan now."],
        20,
        "the approved plan's turn",
    )
    assert _MobileRunner.modes[-1] == "act"
    # Approving goes back to Auto for what follows.
    _active(page).locator(".olano-mode-bar").wait_for(state="detached")
    approve.wait_for(state="detached")

    _active(page).get_by_role("button", name="Conversation menu").click()
    _menu_choice(page, "Rename")
    page.locator("ion-alert input").fill("Meridian switch")
    page.locator("ion-alert").get_by_role("button", name="Save").click()
    page.get_by_text('Renamed to "Meridian switch"').wait_for()


def test_documents_in_a_message(phone: Any, mobile_app: MobileApp, tmp_path: Path) -> None:
    """Files go with a message: a text file is read into it, a PDF is saved in
    the agent's workspace; reopened, the message shows the files by name and
    the person's own words, never the file's contents or the runtime's notes."""
    page = phone
    _MobileRunner.messages.clear()
    notes = tmp_path / "notes.txt"
    notes.write_text("Meridian lead time is five days.\nBoxwell is three.", encoding="utf-8")
    report = tmp_path / "report.pdf"
    report.write_bytes(TINY_PDF)
    _connect(page, mobile_app)
    _new_conversation(page, "nova")
    _active(page).locator("[data-testid=file-picker]").set_input_files([str(notes), str(report)])
    tray = _active(page).locator(".olano-tray-files")
    tray.get_by_text("notes.txt").wait_for()
    tray.get_by_text("report.pdf").wait_for()
    _shot(page, "42-documents-tray")
    _active(page).locator("ion-textarea textarea").fill("Summarise these")
    _active(page).get_by_role("button", name="Send").click()
    mine = _active(page).locator(".olano-msg.user").last
    mine.get_by_text("notes.txt").wait_for()
    mine.get_by_text("report.pdf").wait_for()
    _active(page).get_by_text("Meridian is cheapest at 4.10 a unit.").wait_for(timeout=20000)

    sent = _MobileRunner.messages[-1]
    assert "--- attached: notes.txt" in sent and "Meridian lead time is five days." in sent
    assert "[Document attached: report.pdf" in sent and "saved to" in sent
    saved = list(
        (Path(os.environ["LIONCLAW_HOME"]) / "agents" / "nova" / "workspace" / "uploads").rglob(
            "report*.pdf"
        )
    )
    assert saved, "the PDF was not saved in the agent's workspace"

    rows = _thread_rows("nova")
    thread = Path(os.environ["LIONCLAW_HOME"]) / "sessions" / "nova"
    latest = max(thread.glob("*.jsonl"), key=lambda p: p.stat().st_mtime).stem
    assert any("--- attached: notes.txt" in str(r.get("content")) for r in rows)
    fresh = page.context.new_page()
    fresh.goto(f"{mobile_app.base_url}/chats/nova/{latest}", wait_until="domcontentloaded")
    again = _active(fresh).locator(".olano-msg.user").last
    again.get_by_text("Summarise these").wait_for(timeout=20000)
    again.get_by_text("notes.txt").wait_for()
    again.get_by_role("button", name="Open report.pdf").wait_for()
    body = again.inner_text()
    assert "five days" not in body and "attached" not in body and "saved to" not in body
    _shot(fresh, "43-documents-reloaded")
    fresh.close()


def test_sharing_into_olano(phone: Any, mobile_app: MobileApp) -> None:
    """A file shared from another app asks which agent gets it, then waits in
    a new conversation's composer, unsent."""
    page = phone
    _connect(page, mobile_app)
    page.evaluate(
        """() => window.__olanoShareIn("From Files", [
             new File(["Minutes of the supplier call"], "minutes.txt", { type: "text/plain" })])"""
    )
    page.locator("ion-action-sheet").get_by_text("Send to which agent?").wait_for()
    _shot(page, "44-share-picker")
    _sheet_choice(page, "aria")
    _active(page).locator(".olano-tray-files").get_by_text("minutes.txt").wait_for(timeout=10000)
    assert _active(page).locator("ion-textarea textarea").input_value() == "From Files"
    assert "/chats/aria/new" in page.url


def test_scheduled_tasks(phone: Any, mobile_app: MobileApp) -> None:
    """Settings lists every agent's scheduled tasks; a task is added with a
    plain schedule builder, paused with its switch, changed, run and deleted;
    a conversation's menu lists that agent's tasks."""
    page = phone
    _connect(page, mobile_app)
    page.locator("ion-tab-button", has_text="Settings").click()
    _active(page).get_by_text("Scheduled Tasks", exact=True).click()
    _active(page).get_by_text("No scheduled tasks").wait_for()
    _active(page).get_by_role("button", name="New scheduled task").click()
    editor = page.locator("ion-modal.olano-task-editor.show-modal")
    editor.locator("ion-textarea textarea").fill("Send me the supplier prices")
    editor.get_by_text("Every day at 09:00").wait_for()
    _shot(page, "45-task-editor")
    editor.get_by_role("button", name="Add").click()
    editor.wait_for(state="hidden")

    _active(page).locator(".olano-task-prompt", has_text="Send me the supplier prices").wait_for(
        timeout=10000
    )
    _active(page).get_by_text("Every day at 09:00").wait_for()
    _active(page).get_by_text(re.compile(r"^Runs as ")).wait_for()
    _shot(page, "46-task-detail")
    tasks = httpx.get(f"{mobile_app.runtime_url}/api/scheduler/tasks").json()
    (task,) = [t for t in tasks if t["prompt"] == "Send me the supplier prices"]
    assert task["schedule"] == "0 9 * * *" and task["enabled"] is True and task["name"]

    # Pause it.
    _active(page).locator("ion-toggle").click()
    harness._wait_until(
        lambda: not next(
            t
            for t in httpx.get(f"{mobile_app.runtime_url}/api/scheduler/tasks").json()
            if t["id"] == task["id"]
        )["enabled"],
        10,
        "the task to pause",
    )
    _active(page).get_by_text("Paused").first.wait_for()

    # Change it to weekly.
    _active(page).get_by_role("button", name="Edit task").click()
    editor.locator("ion-select").click()
    # A select's choices are radio buttons in its action sheet.
    page.locator("ion-action-sheet button", has_text="Every week").click()
    page.locator("ion-action-sheet").wait_for(state="detached")
    editor.get_by_text(re.compile(r"^Every week on .+ at 09:00$")).wait_for()
    editor.get_by_role("button", name="Save").click()
    editor.wait_for(state="hidden")
    harness._wait_until(
        lambda: next(
            t
            for t in httpx.get(f"{mobile_app.runtime_url}/api/scheduler/tasks").json()
            if t["id"] == task["id"]
        )["schedule"]
        == "0 9 * * mon",
        10,
        "the weekly schedule",
    )

    # Run now: the deployment runs it, or says why it cannot.
    _active(page).get_by_role("button", name="Run Now").click()
    page.locator("ion-toast").filter(
        has_text=re.compile(r"Running now|[Ss]cheduler")
    ).first.wait_for()

    # A conversation's menu lists that agent's tasks.
    page.goto(f"{mobile_app.base_url}/chats/nova/new", wait_until="domcontentloaded")
    _active(page).get_by_role("button", name="Conversation menu").click(timeout=20000)
    _menu_choice(page, "Scheduled Tasks")
    _active(page).get_by_text("nova's Tasks").wait_for()
    _active(page).locator("h3", has_text=task["name"]).click()
    _active(page).get_by_text("Delete Task").click()
    _sheet_choice(page, "Delete Task")
    harness._wait_until(
        lambda: all(
            t["id"] != task["id"]
            for t in httpx.get(f"{mobile_app.runtime_url}/api/scheduler/tasks").json()
        ),
        10,
        "the task to be deleted",
    )


def test_alerts_offer_their_fix(phone: Any, mobile_app: MobileApp) -> None:
    """An alert says what tapping it does and does it: an assigned issue opens
    in the app (comment, hand it to an agent, close it), a broken connection
    goes to the agent with the message ready, a skill-name mismatch is
    repaired in place."""
    from olano.issues.delivery import notify_assignment
    from olano.issues.store import get_issue_store
    from olano.runtime import notifications

    issue = get_issue_store().create(
        title="Confirm the Meridian contract",
        body="The **renewal** is due Friday.",
        author="ai-nova",
        assignee="human-owner",
    )
    notify_assignment(issue, "ai-nova")
    notifications.notify(
        kind="skills_spec_violation",
        level="warn",
        title="1 skill(s) have a name that doesn't match their folder",
        body="zoho-email",
        agent="nova",
        action={"label": "Open Files", "href": "/files?agent=nova"},
    )

    page = phone
    _connect(page, mobile_app)
    page.locator("ion-tab-button", has_text="Inbox").click()
    _active(page).locator("ion-segment-button", has_text="Alerts").click()
    alerts = _active(page)
    alerts.get_by_text(f"Open issue #{issue['id']}").wait_for()
    alerts.get_by_text("Ask aria to fix it").wait_for()
    alerts.get_by_text("Repair the folders").wait_for()
    _shot(page, "47-actionable-alerts")

    # The issue.
    alerts.get_by_text(f"Issue #{issue['id']} assigned to owner").click()
    _active(page).get_by_text("Confirm the Meridian contract").wait_for()
    _active(page).get_by_text("renewal").wait_for()
    page.locator("ion-footer ion-textarea textarea").fill("Checking with legal today.")
    page.get_by_role("button", name="Post comment").click()
    _active(page).locator(".olano-issue-comment").get_by_text(
        "Checking with legal today."
    ).wait_for()
    _active(page).get_by_role("button", name="Issue actions").click()
    _sheet_choice(page, "Hand to an Agent")
    _sheet_choice(page, "nova")
    _active(page).get_by_text(re.compile(r"assigned it to nova")).wait_for()
    _shot(page, "48-issue")
    _active(page).get_by_role("button", name="Issue actions").click()
    _sheet_choice(page, "Close Issue")
    _active(page).locator(".olano-issue-state.closed").wait_for()
    stored = get_issue_store().get(issue["id"])
    assert stored and stored["state"] == "closed" and stored["assignee"] == "ai-nova"

    # A broken connection: ask the agent, with the message ready to send.
    _active(page).locator("ion-back-button").click()
    _active(page).get_by_text("aria's WhatsApp is not connected").click()
    _sheet_choice(page, "Ask aria to Fix It")
    box = _active(page).locator("ion-textarea textarea")
    box.wait_for(timeout=10000)
    harness._wait_until(
        lambda: "aria's WhatsApp is not connected" in box.input_value(), 5, "the draft"
    )
    assert "/chats/aria/new" in page.url

    # A skill-name mismatch: repaired in place. (A conversation hides the tab bar.)
    page.goto(f"{mobile_app.base_url}/inbox", wait_until="domcontentloaded")
    _active(page).locator("ion-segment-button", has_text="Alerts").click(timeout=20000)
    _active(page).get_by_text("1 skill(s) have a name that doesn't match their folder").click()
    _sheet_choice(page, "Repair Skill Folders")
    page.locator("ion-toast").filter(
        has_text=re.compile(r"Nothing to repair|Repaired")
    ).first.wait_for()

    # Every issue, not just the ones an alert names: Inbox, Issues. The one
    # closed above is under All, marked Closed.
    _active(page).locator("ion-segment-button", has_text="Issues").click()
    _active(page).get_by_text("No open issues").wait_for()
    _active(page).locator(".olano-chips ion-chip", has_text="All").click()
    row = _active(page).locator("ion-item", has_text="Confirm the Meridian contract")
    row.locator(".olano-issue-closed").wait_for()
    _shot(page, "49-inbox-issues")
    row.click()
    _active(page).locator(".olano-issue-state.closed").wait_for()


def test_agent_files(phone: Any, mobile_app: MobileApp, tmp_path: Path) -> None:
    """An agent's files from Settings: its folders, a file opened in the
    viewer, a search across them, an upload, and the same name uploaded again
    kept beside the first."""
    home = Path(os.environ["LIONCLAW_HOME"])
    workspace = home / "agents" / "nova" / "workspace"
    (workspace / "reports").mkdir(parents=True, exist_ok=True)
    (workspace / "reports" / "suppliers.md").write_text(
        "# Suppliers\n\nMeridian ships in five days.\n", encoding="utf-8"
    )
    page = phone
    _connect(page, mobile_app)
    page.locator("ion-tab-button[tab='settings']").click()
    _active(page).get_by_text("Files", exact=True).click()
    # By the row's link, not its text: the Settings page underneath holds a
    # hidden "nova" option in its Default agent select.
    page.locator("ion-item[router-link='/settings/files/nova']").click()

    files = _active(page)
    files.locator("ion-title", has_text="nova's Files").wait_for(timeout=20000)
    files.locator(".olano-file-row", has_text="reports").click()
    folder = _active(page)
    folder.locator("ion-title", has_text="reports").wait_for()
    folder.locator(".olano-file-row", has_text="suppliers.md").click()
    page.get_by_text("Meridian ships in five days.").wait_for(timeout=20000)
    _shot(page, "44-file-open")
    page.locator("ion-modal").get_by_role("button", name="Close").first.click()

    # Search finds it from the top as well.
    folder.locator("ion-back-button").click()
    page.wait_for_url(re.compile(r"/settings/files/nova$"))
    files = _active(page)
    # The folder sliding away is the last visible page until it has gone.
    files.locator("ion-title", has_text="nova's Files").wait_for()
    files.locator("ion-searchbar input").fill("suppl")
    hit = files.locator(".olano-file-row", has_text="suppliers.md")
    hit.wait_for(timeout=20000)
    hit.get_by_text("reports").wait_for()
    files.locator("ion-searchbar input").fill("")

    # Upload, then the same name again: kept as a second file.
    note = tmp_path / "notes.txt"
    note.write_text("Call Boxwell on Monday.\n", encoding="utf-8")
    files.locator("input[type=file]").set_input_files(str(note))
    files.locator(".olano-file-row", has_text="notes.txt").wait_for(timeout=20000)
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "Call Boxwell on Monday.\n"
    files.locator("input[type=file]").set_input_files(str(note))
    _sheet_choice(page, "Keep Both")
    files.locator(".olano-file-row", has_text="notes 2.txt").wait_for(timeout=20000)
    assert (workspace / "notes 2.txt").exists()
    _shot(page, "45-files")


def test_agent_page_and_admin_controls(phone: Any, mobile_app: MobileApp) -> None:
    """Tapping the agent in a conversation opens its page: why it is not
    answering when it is not, and Start, Stop and Restart for an admin."""
    page = phone
    roster: dict[str, Any] = {
        "status": "error",
        "running": False,
        "error": "OPENAI_API_KEY is not set",
    }
    calls: list[str] = []

    def agents(route: Any) -> None:
        resp = route.fetch()
        data = resp.json()
        for agent in data:
            if agent["name"] == "nova":
                agent.update(roster)
        route.fulfill(response=resp, json=data)

    def lifecycle(route: Any) -> None:
        action = route.request.url.rsplit("/", 1)[-1]
        calls.append(action)
        roster.update(
            {"status": "running", "running": True, "error": ""}
            if action in ("start", "restart")
            else {"status": "stopped", "running": False, "paused": True}
        )
        route.fulfill(json={"ok": True})

    page.route("**/api/agents", agents)
    page.route(re.compile(r".*/api/agents/nova/(start|stop|restart)$"), lifecycle)
    _connect(page, mobile_app)
    _open_agent(page, "nova")
    _active(page).get_by_label("About nova").click()

    info = _active(page)
    info.get_by_text("Could not start").wait_for(timeout=20000)
    info.get_by_text("OPENAI_API_KEY is not set").wait_for()
    _shot(page, "46-agent-error")
    info.get_by_role("button", name="Start").click()
    info.get_by_text("Running", exact=True).wait_for(timeout=20000)
    info.get_by_role("button", name="Stop").click()
    _sheet_choice(page, "Stop")
    info.get_by_text("Stopped", exact=True).wait_for(timeout=20000)
    info.get_by_text("It stays stopped until it is started again.").wait_for()
    assert calls == ["start", "stop"]
    # Its files and scheduled tasks are one tap away.
    info.get_by_text("Files", exact=True).wait_for()
    info.get_by_text("Scheduled Tasks", exact=True).wait_for()


def test_add_an_agent_to_a_chat(phone: Any, mobile_app: MobileApp) -> None:
    """A one-to-one conversation becomes a group with a second agent, and the
    conversation so far comes along."""
    page = phone
    resp = page.request.put(
        mobile_app.base_url + "/api/system/config", data={"groups": {"enabled": True}}
    )
    assert resp.ok, resp.text()
    _connect(page, mobile_app)
    _open_agent(page, "nova")
    _active(page).get_by_text("is the cheapest at 4.10").wait_for()
    _active(page).get_by_role("button", name="Conversation menu").click()
    _menu_choice(page, "Add an Agent")
    _sheet_choice(page, "atlas")

    group = _active(page)
    group.locator("ion-title", has_text="nova & atlas").wait_for(timeout=20000)
    group.get_by_text("Compare our three packaging suppliers").wait_for(timeout=20000)
    group.get_by_text("is the cheapest at 4.10").wait_for()
    _shot(page, "47-chat-to-group")


def test_credits_and_usage(phone: Any, mobile_app: MobileApp) -> None:
    """On a deployment with Olano credits, Settings shows the balance and what
    each service spent, with nothing to buy."""
    page = phone
    page.route(
        "**/api/credits",
        lambda r: r.fulfill(
            json={
                "credits_enabled": True,
                "unit_name": "credit",
                "unit_name_plural": "credits",
                "decimals": 0,
            }
        ),
    )
    page.route(
        "**/api/olano/credits*",
        lambda r: r.fulfill(
            json={
                "available": True,
                "balance": 1250,
                "included_remaining": 1000,
                "purchased_remaining": 250,
                "monthly_included": 4000,
                "monthly_used": 3000,
                "period_end": "2026-11-01T00:00:00Z",
                "manage_url": "https://cloud.olano.ai/buy",
            }
        ),
    )
    page.route(
        "**/api/olano/services/usage*",
        lambda r: r.fulfill(
            json={
                "available": True,
                "window": {"since": "2026-09-18", "days": 30},
                "total_credits": 2900,
                "services": [
                    {"service": "chat", "label": "Chat", "calls": 120, "credits": 2400},
                    {"service": "image", "label": "Images", "calls": 5, "credits": 500},
                    {"service": "tts", "label": "Speech", "calls": 0, "credits": 0},
                ],
            }
        ),
    )
    _connect(page, mobile_app)
    page.locator("ion-tab-button[tab='settings']").click()
    _active(page).get_by_text("Credits and Usage").click()
    usage = _active(page)
    usage.get_by_text("1,250 credits").wait_for(timeout=20000)
    usage.get_by_text("3,000 of 4,000").wait_for()
    usage.get_by_text("By service, last 30 days").wait_for()
    assert usage.locator("ion-item", has_text="Chat").get_by_text("2,400 credits").count() == 1
    assert usage.get_by_text("Speech").count() == 0
    text = usage.inner_text()
    assert "Buy" not in text and "cloud.olano.ai" not in text
    _shot(page, "48-credits")


def test_app_lock(phone: Any, mobile_app: MobileApp) -> None:
    """With the lock on, the app asks the phone to unlock when it comes back
    and when it opens; nothing shows until it does."""
    page = phone
    # The phone's answer survives a reload, like the lock setting does.
    page.add_init_script(
        "window.__olanoLock = { status: { available: true, kind: 'biometric' },"
        " get answer() { return localStorage.getItem('test.lock.answer') || 'ok'; },"
        " set answer(v) { localStorage.setItem('test.lock.answer', v); } };"
    )
    page.reload(wait_until="domcontentloaded")
    _connect(page, mobile_app)
    page.locator("ion-tab-button[tab='settings']").click()
    toggle = _active(page).locator("ion-toggle", has_text="Lock with")
    toggle.click()
    _active(page).get_by_text("Ask again").wait_for()
    _shot(page, "49-lock-settings")

    def leave_and_return() -> None:
        page.evaluate(
            """() => {
              const set = (hidden) => {
                Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
                document.dispatchEvent(new Event("visibilitychange"));
              };
              set(true);
              set(false);
            }"""
        )

    # The phone says no: the app stays locked until it says yes.
    page.evaluate("window.__olanoLock.answer = 'cancelled'")
    leave_and_return()
    lock = page.locator(".olano-lock")
    lock.get_by_text("Olano is locked").wait_for()
    _shot(page, "50-locked")
    page.evaluate("window.__olanoLock.answer = 'ok'")
    lock.get_by_role("button", name=re.compile("Unlock")).click()
    lock.wait_for(state="detached")

    # Opening the app again asks straight away.
    page.evaluate("window.__olanoLock.answer = 'cancelled'")
    page.reload(wait_until="domcontentloaded")
    page.locator(".olano-lock").get_by_text("Olano is locked").wait_for(timeout=20000)


def test_hands_free(phone: Any, mobile_app: MobileApp, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hands-free: the phone listens, the voice note goes, the reply comes, and
    it listens again; End brings the keyboard back."""
    from olano.platforms import inbound

    async def transcribe(path: str) -> tuple[str | None, str | None]:
        return "What is the cheapest supplier?", None

    monkeypatch.setattr(inbound, "_transcribe_audio", transcribe)
    page = phone
    _connect(page, mobile_app)
    _new_conversation(page, "nova")
    conversation = _active(page)
    conversation.get_by_role("button", name="Conversation menu").click()
    _menu_choice(page, "Hands-Free Mode")

    panel = conversation.locator(".olano-handsfree")
    panel.get_by_text("Listening").wait_for(timeout=10000)
    assert not conversation.locator("ion-textarea").is_visible()
    _shot(page, "51-hands-free")
    page.wait_for_timeout(1500)
    # The big button sends now (the fake microphone's tone never pauses).
    panel.get_by_role("button", name="Send now").click()
    conversation.locator(".olano-msg.user .olano-voice").wait_for(timeout=20000)
    conversation.get_by_text("Meridian is cheapest at 4.10 a unit.").wait_for(timeout=30000)
    # Read (or not: headless Chromium may have no voice), then listening again.
    panel.get_by_text("Listening").wait_for(timeout=30000)
    panel.get_by_role("button", name="End Hands-Free").click()
    conversation.locator("ion-textarea textarea").wait_for()


def test_hands_free_with_live_dictation(phone: Any, mobile_app: MobileApp) -> None:
    """Where the phone dictates, hands-free listens with its recognizer: the
    words show in the panel as they are said, a pause sends them as a text
    message (no recording, nothing for the deployment to transcribe), the
    reply is read, and it listens again."""
    page = phone
    page.add_init_script(FAKE_PHONE_DICTATION)
    page.reload(wait_until="domcontentloaded")
    _connect(page, mobile_app)
    _new_conversation(page, "nova")
    conversation = _active(page)
    _dictate(
        page,
        [
            (300, "What is", False),
            (700, "What is the cheapest", False),
            (1100, "What is the cheapest supplier", False),
        ],
        "What is the cheapest supplier?",
    )
    conversation.get_by_role("button", name="Conversation menu").click()
    _menu_choice(page, "Hands-Free Mode")

    panel = conversation.locator(".olano-handsfree")
    panel.get_by_text("Listening").wait_for(timeout=10000)
    heard = panel.get_by_label("What was heard")
    heard.get_by_text("What is the cheapest supplier").wait_for(timeout=10000)
    _shot(page, "52-hands-free-words")

    # The pause sends the words, the last ones included, as text.
    sent = conversation.locator(".olano-msg.user").last
    sent.get_by_text("What is the cheapest supplier?", exact=True).wait_for(timeout=15000)
    assert sent.locator(".olano-voice").count() == 0
    conversation.get_by_text("Meridian is cheapest at 4.10 a unit.").wait_for(timeout=30000)
    home = Path(os.environ["LIONCLAW_HOME"])
    thread_file = max((home / "sessions" / "nova").glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    rows = [json.loads(line) for line in thread_file.read_text(encoding="utf-8").splitlines()]
    assert [r["content"] for r in rows if r["role"] == "user"] == ["What is the cheapest supplier?"]
    assert not list(home.rglob("uploads/voice-*"))

    # Read aloud, then listening again, with the phone's recognizer.
    panel.get_by_text("Listening").wait_for(timeout=30000)
    harness._wait_until(
        lambda: page.evaluate("window.__dictation.started") == 2, 15, "listening again"
    )
    assert page.evaluate("window.__dictation.stopped") == 1
    panel.get_by_role("button", name="End Hands-Free").click()
    conversation.locator("ion-textarea textarea").wait_for()
    harness._wait_until(
        lambda: page.evaluate("window.__dictation.cancelled") == 1, 5, "listening cancelled"
    )


def test_talk_it_over_starts_with_the_item(phone: Any, mobile_app: MobileApp) -> None:
    """Talk it over on a Cortex item opens a conversation with the item framed
    in the message box, unsent, for the person to add their own words; once
    the conversation exists, it reopens as it is."""
    page = phone
    _connect(page, mobile_app)
    page.locator("ion-tab-button[tab='cortex']").click()
    _active(page).get_by_text("Bundle shipping with the spring order").click()
    _active(page).get_by_role("button", name="Talk it over").click()

    conversation = _active(page)
    box = conversation.locator("ion-textarea textarea")
    box.wait_for(timeout=20000)
    harness._wait_until(lambda: box.input_value() != "", 10, "the draft in the message box")
    draft = box.input_value()
    assert draft.startswith(
        'The user wants to chat about this idea: "Bundle shipping with the spring order".'
    ), draft
    assert "Artifacts (full workspace path):\n/" in draft and "/workspace/" in draft, draft
    assert draft.endswith("User: "), draft
    # Nothing was sent on the person's behalf.
    assert conversation.locator(".olano-msg.user").count() == 0
    _shot(page, "52-talk-it-over")

    box.fill(draft + "Is March realistic?")
    conversation.get_by_role("button", name="Send").click()
    conversation.get_by_text("Meridian is cheapest at 4.10 a unit.").wait_for(timeout=30000)

    # Back on the item, Talk it over continues the conversation as it is.
    conversation.locator("ion-back-button").click()
    _active(page).get_by_role("button", name="Talk it over").click()
    again = _active(page)
    again.get_by_text("Is March realistic?").wait_for(timeout=20000)
    page.wait_for_timeout(500)
    assert again.locator("ion-textarea textarea").input_value() == ""


def test_talk_to_an_agent_opens_hands_free(phone: Any, mobile_app: MobileApp) -> None:
    """ "Talk to an Agent" (Siri, the Action button, a launcher shortcut): a new
    conversation with the agent opens and Hands-Free Mode starts on it; an
    agent the deployment does not have is refused with a notice."""
    page = phone
    _connect(page, mobile_app)
    # A desktop browser has no Siri; the hook stands in for the native request.
    page.evaluate("window.__olanoTalk('nova')")
    page.wait_for_url(re.compile(r"/chats/nova/new$"), timeout=20000)
    conversation = _active(page)
    panel = conversation.locator(".olano-handsfree")
    panel.get_by_text("Listening").wait_for(timeout=20000)
    assert not conversation.locator("ion-textarea").is_visible()
    _shot(page, "53-talk-to-agent")
    panel.get_by_role("button", name="End Hands-Free").click()
    conversation.locator("ion-textarea textarea").wait_for()

    # A request for an agent that is not here.
    page.evaluate("window.__olanoTalk('nobody')")
    page.get_by_text("There is no agent called nobody on this deployment.").wait_for(timeout=10000)
    assert page.url.endswith("/chats/nova/new")


def test_deep_links_open_screens(phone: Any, mobile_app: MobileApp) -> None:
    """The app's own links, as the widgets and controls open them: a tab, an
    agent's conversations, a conversation, and a talk request."""
    page = phone
    _connect(page, mobile_app)
    assert page.evaluate("window.__olanoDeepLink('olano://open?tab=inbox')")
    page.wait_for_url(re.compile(r"/inbox$"), timeout=20000)
    # The same link again, already there: nothing is pushed on top.
    assert page.evaluate("window.__olanoDeepLink('olano://open?tab=inbox')")
    page.wait_for_timeout(800)
    assert page.url.endswith("/inbox")
    # An agent alone (the Agent and Team widgets): the last conversation the
    # person had with it, as a tap in Chats opens, never the list of threads.
    assert page.evaluate("window.__olanoDeepLink('olano://open?agent=nova')")
    page.wait_for_url(re.compile(r"/chats/nova/supplier-comparison$"), timeout=20000)
    _active(page).get_by_role("button", name="Conversation menu").wait_for(timeout=20000)
    # An agent with no conversation yet: a new one.
    assert page.evaluate("window.__olanoDeepLink('olano://open?agent=aria')")
    page.wait_for_url(re.compile(r"/chats/aria/new$"), timeout=20000)
    assert page.evaluate("window.__olanoDeepLink('olano://open?agent=nova&thread=new')")
    page.wait_for_url(re.compile(r"/chats/nova/new$"), timeout=20000)
    _active(page).locator("ion-textarea textarea").wait_for(timeout=20000)
    # Not one of ours: left alone.
    assert not page.evaluate("window.__olanoDeepLink('https://example.com/')")
    assert page.url.endswith("/chats/nova/new")
    # A talk link is the same request Siri makes.
    assert page.evaluate("window.__olanoDeepLink('olano://talk?agent=nova')")
    # Listening on the page in front, not on the agent's earlier conversation
    # further down the stack (which once kept its "on screen" state and took
    # the request).
    _active(page).locator(".olano-handsfree").get_by_text("Listening").wait_for(timeout=20000)
    assert page.locator(".olano-handsfree").count() == 1
    _active(page).get_by_role("button", name="End Hands-Free").click()
    # The Ask widget's Message button: a new conversation with the keyboard up.
    assert page.evaluate(
        "window.__olanoDeepLink('olano://open?agent=nova&thread=new&compose=text')"
    )
    page.wait_for_url(re.compile(r"/chats/nova/new\?compose=text$"), timeout=20000)
    box = _active(page).locator("ion-textarea textarea")
    box.wait_for(timeout=20000)
    # The built app's CSP (script-src 'self') refuses the string predicate
    # ``wait_for_function`` compiles in the page, so poll with ``evaluate``.
    _wait_for_socket(
        page,
        lambda: bool(
            page.evaluate("document.activeElement && document.activeElement.tagName === 'TEXTAREA'")
        ),
        "the composer to take focus",
    )


@pytest.mark.parametrize(
    ("link", "lands_on"),
    [
        ("olano://open?tab=inbox", r"/inbox$"),
        ("olano://open?agent=nova", r"/chats/nova/supplier-comparison$"),
        ("olano://open?agent=nova&thread=new&compose=text", r"/chats/nova/new\?compose=text$"),
        ("olano://talk?agent=nova", r"/chats/nova/new$"),
    ],
)
def test_a_link_the_app_was_opened_with(
    phone: Any, mobile_app: MobileApp, link: str, lands_on: str
) -> None:
    """A widget, a control or Siri opening an app that was not running: the
    link arrives before the screens exist (here, before the deployment is
    even entered) and the app opens on it, not on Chats. The tabs mount at
    the root and redirect to Chats in an effect of their own; a push made in
    the same pass lost to that redirect's replace, which is the race this
    covers."""
    page = phone
    page.get_by_text("Connect to your Olano").wait_for()
    assert page.evaluate(f"window.__olanoDeepLink({link!r})")
    page.locator("ion-input input").fill(mobile_app.base_url)
    page.get_by_role("button", name="Continue").click()
    page.wait_for_url(re.compile(lands_on), timeout=20000)
    if link.startswith("olano://talk"):
        _active(page).locator(".olano-handsfree").get_by_text("Listening").wait_for(timeout=20000)
        _active(page).get_by_role("button", name="End Hands-Free").click()
    elif "agent=" in link:
        _active(page).locator("ion-textarea textarea").wait_for(timeout=20000)
    else:
        _active(page).locator("ion-title", has_text="Inbox").wait_for(timeout=20000)
    # Still the one screen it opened on: the redirect did not land on top.
    page.wait_for_timeout(1000)
    assert re.search(lands_on, page.url), page.url


def test_default_agent_setting(phone: Any, mobile_app: MobileApp) -> None:
    """Settings, Siri and widgets: the agent Siri, the widgets and the
    launcher reach when none was picked. The choice is kept on the phone."""
    page = phone
    _connect(page, mobile_app)
    page.locator("ion-tab-button", has_text="Settings").click()
    settings = _active(page)
    settings.get_by_text("Siri and widgets").wait_for(timeout=20000)
    select = settings.locator(".olano-default-agent ion-select")
    select.wait_for(timeout=20000)
    page.wait_for_function(
        "el => !el.hasAttribute('disabled') && el.value === ''",
        arg=select.element_handle(),
        timeout=20000,
    )
    # The choices are the agents, behind "first in the list".
    select.click()
    page.locator("ion-action-sheet button", has_text="First in the list (nova)").wait_for(
        timeout=10000
    )
    page.locator("ion-action-sheet button", has_text="aria").click()
    page.locator("ion-action-sheet").wait_for(state="detached")
    page.wait_for_function("el => el.value === 'aria'", arg=select.element_handle(), timeout=10000)
    assert page.evaluate("localStorage.getItem('olano.defaultAgent')") == "aria"
    _shot(page, "54-default-agent")
    # Kept on the phone: read back after a reload (the app remembers the
    # deployment and comes back on the same screen).
    page.reload(wait_until="domcontentloaded")
    select = _active(page).locator(".olano-default-agent ion-select")
    select.wait_for(timeout=20000)
    page.wait_for_function("el => el.value === 'aria'", arg=select.element_handle(), timeout=20000)


def _swipe(page: Any, row: Any, direction: str) -> None:
    """Drags a list row most of its width, slowly at the end, so Ionic's
    sliding item counts it as a full swipe (past the options plus its margin,
    not moving fast when the finger lifts)."""
    box = row.bounding_box()
    assert box is not None
    y = box["y"] + box["height"] / 2
    start, end = (
        (box["x"] + box["width"] * 0.85, box["x"] + 8)
        if direction == "left"
        else (box["x"] + 8, box["x"] + box["width"] * 0.85)
    )
    page.mouse.move(start, y)
    page.mouse.down()
    page.mouse.move((start + end) / 2, y, steps=8)
    page.mouse.move(end, y, steps=12)
    page.wait_for_timeout(150)
    page.mouse.move(end, y)
    page.mouse.up()


def test_cortex_triage(phone: Any, mobile_app: MobileApp) -> None:
    """Triage on the Cortex list: unopened rows are tinted until opened, a swipe
    left discards, a swipe right approves (the row is listed under the next
    stage at once), Select discards several at once, and the item page's own
    Discard button takes the item out of the list."""
    from olano.cognition import cortex_items

    workspace = Path(os.environ["LIONCLAW_HOME"]) / "agents" / "nova" / "workspace"
    cortex_items.add_item(
        workspace,
        kind="brainstorm",
        title="Shorter supplier onboarding",
        summary="Three forms could be one.",
        detail="## Forms\n\nThe supplier portal asks the same thing three times.",
    )
    cortex_items.add_item(
        workspace,
        kind="idea",
        title="Switch to recycled mailers",
        summary="Same cost, better story.",
        detail="## Why\n\nTwo customers asked.",
    )
    page = phone
    _connect(page, mobile_app)
    page.locator("ion-tab-button[tab='cortex']").click()
    cortex = _active(page)
    cortex.get_by_text("Shorter supplier onboarding").wait_for(timeout=15000)
    rows = cortex.locator("ion-item.olano-cortex-row")
    assert rows.count() == 3
    # Nothing opened on this phone yet: every row carries the unread tint.
    assert cortex.locator("ion-item.olano-cortex-unread").count() == 3
    cortex.get_by_text("Swipe a row right to approve it, left to discard it.").wait_for()
    _shot(page, "55-cortex-unread")

    # Opening an item clears its tint, and only its own.
    cortex.get_by_text("Switch to recycled mailers").click()
    item = _active(page)
    item.get_by_text("Two customers asked.").wait_for(timeout=15000)
    item.get_by_role("button", name="Make it a prototype").wait_for()
    item.get_by_role("button", name="Discard").wait_for()
    item.get_by_role("button", name="Talk it over").wait_for()
    _shot(page, "56-cortex-item-actions")
    item.locator("ion-back-button").click()
    cortex = _active(page)
    cortex.get_by_text("Shorter supplier onboarding").wait_for(timeout=15000)
    assert (
        page.evaluate("JSON.parse(localStorage.getItem('olano.cortex.seen') || '[]').length") == 1
    )
    cortex.locator("ion-item.olano-cortex-unread").filter(
        has_text="Switch to recycled mailers"
    ).wait_for(state="detached")
    assert cortex.locator("ion-item.olano-cortex-unread").count() == 2

    # Swipe right: approve. The deployment is told to promote it and the row
    # moves to the next stage at once, marked as on its way. The promotion
    # itself is a model run, so the route stands in for it and does on disk
    # what the job would: the brainstorm becomes promoted, an idea appears.
    promotes: list[str] = []

    def _promote(route: Any) -> None:
        promotes.append(route.request.url)
        source = _item_id(workspace, "Shorter supplier onboarding", kind="brainstorm")
        assert route.request.url.split("/items/")[1].split("/")[0] == source
        cortex_items.set_status(workspace, source, "promoted")
        cortex_items.add_item(
            workspace,
            kind="idea",
            title="Shorter supplier onboarding",
            summary="Three forms could be one.",
            detail="## Forms\n\nOne form, three sections.",
        )
        route.fulfill(
            json={
                "ok": True,
                "job": {"id": "job-1", "kind": "promote", "status": "done"},
                "already_running": False,
            }
        )

    page.route("**/api/cortex/items/*/promote", _promote)
    stages = cortex.locator(".olano-stage")
    assert stages.all_inner_texts() == ["Brainstorms 1", "Ideas 2", "Prototypes 0", "Projects 0"]
    _swipe(
        page, cortex.locator("ion-item-sliding", has_text="Shorter supplier onboarding"), "right"
    )
    cortex.get_by_text("Becoming an idea…").wait_for(timeout=10000)
    _wait_for_socket(page, lambda: len(promotes) == 1, "the promote request")
    assert stages.all_inner_texts() == ["Brainstorms 0", "Ideas 3", "Prototypes 0", "Projects 0"]
    _shot(page, "57-cortex-approved")
    page.unroute("**/api/cortex/items/*/promote")
    # The refresh that follows swaps the optimistic row for the real idea:
    # the counts hold, the "on its way" mark goes.
    cortex.get_by_text("Becoming an idea…").wait_for(state="detached", timeout=15000)
    assert stages.all_inner_texts() == ["Brainstorms 0", "Ideas 3", "Prototypes 0", "Projects 0"]

    # Swipe left: discard. The row goes at once and the deployment agrees.
    _swipe(page, cortex.locator("ion-item-sliding", has_text="Switch to recycled mailers"), "left")
    cortex.get_by_text("Switch to recycled mailers").wait_for(state="detached", timeout=10000)
    page.get_by_text("Discarded", exact=True).wait_for(timeout=10000)
    assert stages.all_inner_texts() == ["Brainstorms 0", "Ideas 2", "Prototypes 0", "Projects 0"]
    _wait_for_socket(
        page,
        lambda: cortex_items.get_item(workspace, _item_id(workspace, "Switch to recycled mailers"))[
            "status"
        ]
        == "discarded",
        "the discard to reach the deployment",
    )

    # Select: every row gets a checkbox, Select all picks them, the bar discards.
    cortex.get_by_role("button", name="Select").click()
    cortex.get_by_text("Tap rows to pick them, then discard them below.").wait_for()
    cortex.locator("ion-item-sliding", has_text="Bundle shipping with the spring order").locator(
        "ion-item"
    ).click()
    cortex.locator("ion-title", has_text="1 selected").first.wait_for()
    cortex.get_by_role("button", name="Select all").click()
    cortex.locator("ion-title", has_text="2 selected").first.wait_for()
    _shot(page, "58-cortex-select")
    page.get_by_role("button", name="Discard 2").click()
    page.locator("ion-alert").get_by_text("Discard 2 items?").wait_for()
    page.locator("ion-alert button", has_text="Discard").click()
    cortex.get_by_text("Nothing here yet").wait_for(timeout=10000)
    assert cortex.get_by_role("button", name="Select").count() == 0
    _wait_for_socket(
        page,
        lambda: all(
            cortex_items.get_item(workspace, _item_id(workspace, title, kind="idea"))["status"]
            == "discarded"
            for title in ("Bundle shipping with the spring order", "Shorter supplier onboarding")
        ),
        "both discards to reach the deployment",
    )
    _shot(page, "59-cortex-empty")


def test_cortex_item_promotion_feedback(phone: Any, mobile_app: MobileApp) -> None:
    """The item page answers "Make it a prototype" on the spot. The button
    spins with "Making it a prototype…" while the request is out (nothing
    else about the page changed, which read as nothing having happened); a
    banner then follows the run, which the detail endpoint reports on the
    item; when the deployment says the run ended the banner reads "Now a
    prototype" with a button that opens the new item's page."""
    from olano.cognition import cortex_items

    workspace = Path(os.environ["LIONCLAW_HOME"]) / "agents" / "nova" / "workspace"
    idea = cortex_items.add_item(
        workspace,
        kind="idea",
        title="Switch to recycled mailers",
        summary="Same cost, better story.",
        detail="## Why\n\nTwo customers asked.",
    )
    page = phone
    _connect(page, mobile_app)
    page.locator("ion-tab-button[tab='cortex']").click()
    cortex = _active(page)
    cortex.get_by_text("Switch to recycled mailers").wait_for(timeout=15000)

    # The promotion is a model run, so the route stands in for it: the answer
    # is held until the test releases it, and while the run "goes" the detail
    # endpoint reports the job the way the real one does.
    held: list[Any] = []
    running = {"on": False}
    job = {"id": "job-1", "kind": "promote", "status": "running", "detail": {"to": "prototype"}}

    def _promote(route: Any) -> None:
        running["on"] = True
        held.append(route)

    def _detail(route: Any) -> None:
        resp = route.fetch()
        data = resp.json()
        if running["on"]:
            data["item"]["job"] = {
                **job,
                "last_step": {"text": "writing README.md", "tool": "write_file"},
            }
        route.fulfill(response=resp, json=data)

    page.route("**/api/cortex/items/*/promote", _promote)
    page.route(f"**/api/cortex/items/{idea['id']}?*", _detail)

    cortex.get_by_text("Switch to recycled mailers").click()
    item = _active(page)
    item.get_by_text("Two customers asked.").wait_for(timeout=15000)
    item.get_by_role("button", name="Make it a prototype").click()
    # Before the deployment has answered: the button itself says so.
    item.get_by_role("button", name="Making it a prototype…").wait_for(timeout=5000)
    # The spinner sits on the button, and the others wait with it.
    assert item.locator("ion-button.olano-busy ion-spinner").count() == 1
    assert item.get_by_role("button", name="Discard").is_disabled()
    _shot(page, "60-cortex-item-making")

    held[0].fulfill(json={"ok": True, "job": job, "already_running": False})
    banner = item.locator("[data-testid='cortex-item-banner']")
    banner.wait_for(timeout=10000)
    assert re.search(r"is making this a prototype", banner.inner_text())
    item.get_by_role("button", name="Becoming a prototype…").wait_for(timeout=10000)
    # The reload brought the run's own step.
    banner.get_by_text("write_file: writing README.md").wait_for(timeout=10000)
    # The notice sits above the tab bar, not on it.
    toast = page.locator("ion-toast .toast-wrapper").first
    toast.wait_for(timeout=5000)
    toast_box = toast.bounding_box()
    bar_box = page.locator("ion-tab-bar").bounding_box()
    assert toast_box and bar_box
    assert toast_box["y"] + toast_box["height"] <= bar_box["y"], (toast_box, bar_box)
    _shot(page, "61-cortex-item-becoming")

    # The run ends: the deployment says so on the stream, the item is promoted
    # on disk, and the page says where it went.
    proto = cortex_items.add_item(
        workspace,
        kind="prototype",
        title="Switch to recycled mailers",
        summary="Same cost, better story.",
    )
    cortex_items.update_item(
        workspace,
        idea["id"],
        status="promoted",
        links={
            "promoted_to": proto["path"],
            "promoted_to_id": proto["id"],
            "promoted_to_kind": "prototype",
        },
    )
    running["on"] = False
    mobile_app.push_event("cortex_job", {"job": {**job, "status": "done", "agent": "nova"}})
    banner.get_by_text("Now a prototype").wait_for(timeout=15000)
    assert item.get_by_role("button", name="Make it a prototype").count() == 0
    _shot(page, "62-cortex-item-promoted")
    banner.get_by_role("button", name="Open the prototype").click()
    opened = _active(page)
    opened.get_by_role("button", name="Make it a project").wait_for(timeout=15000)
    assert opened.locator("ion-title").first.inner_text().strip() == "Prototype"
    assert opened.locator(".olano-item-summary").inner_text() == "Same cost, better story."


def _item_id(workspace: Path, title: str, kind: str | None = None) -> str:
    from olano.cognition import cortex_items

    for item in cortex_items.list_items(workspace, include_discarded=True):
        if item.get("title") == title and (kind is None or item.get("kind") == kind):
            return str(item["id"])
    raise AssertionError(f"no Cortex item titled {title!r}")
