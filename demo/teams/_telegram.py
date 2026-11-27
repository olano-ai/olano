"""The Telegram seam shared by the ``customers`` and ``monetization`` videos.

A real deployment receives a Telegram message as a Bot API ``Update``: the product's
``TelegramAdapter`` parses it (``_handle_text_message`` -> ``_build_message_event``), hands it to
the runtime's platform handler (``RuntimeServer._boot_platform_adapter``'s ``handler``: sender
identity, the request context, the monetization gate and paywall, slash commands, the turn) and
sends the answer back with the Bot API.

This module runs exactly that code, with one thing stood in for: Telegram's network. There is no
bot token on this machine and ``api.telegram.org`` is not reachable, so

- an incoming message is a Bot API ``Update`` JSON (what Telegram would POST to a webhook),
  delivered to the adapter's own text handler by a small local HTTP endpoint (``serve``);
- the adapter's ``Bot`` is ``RecordingBot``: every Bot API call the adapter makes is recorded
  (method and arguments) instead of being sent, and read back by the flow. A call that needs
  Telegram's answer to go on (``createInvoiceLink`` for Stars) fails the way a call without a
  token fails, and the adapter carries on as it does then.

The platform handler is the product's own method, called on a minimal stand-in for the
``RuntimeServer`` it belongs to (the handler uses only the event bus and the platform list from it).
"""

from __future__ import annotations

import asyncio
import http.server
import itertools
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

_ids = itertools.count(1000)


class RecordingBot:
    """The adapter's ``telegram.Bot``, minus the network: each Bot API call is recorded."""

    def __init__(self, username: str = "demo_bot", bot_id: int = 8000000001) -> None:
        # What getMe would have told the adapter about its own bot.
        self.username = username
        self.id = bot_id
        self.first_name = username
        self.is_bot = True
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        with self.lock:
            self.calls.append({"method": method, "at": time.time(), **_plain(kwargs)})

    async def create_invoice_link(self, **kwargs: Any) -> str:
        self._record("createInvoiceLink", kwargs)
        raise RuntimeError("createInvoiceLink needs the bot's token; there is none here")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            if args:
                kwargs = {"args": list(args), **kwargs}
            self._record(_camel(name), kwargs)
            return SimpleNamespace(message_id=next(_ids), chat=SimpleNamespace(id=0))

        return call


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p.title() for p in rest)


def _plain(value: Any) -> Any:
    """Bot API arguments as JSON-able data (keyboards become their buttons' labels and URLs)."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    keyboard = getattr(value, "inline_keyboard", None)
    if keyboard is not None:
        return [[{"text": b.text, "url": b.url} for b in row] for row in keyboard]
    if hasattr(value, "label") and hasattr(value, "amount"):  # LabeledPrice
        return {"label": value.label, "amount": value.amount}
    return str(value)


class TelegramSeam:
    """One agent's Telegram adapter, wired to the runtime's own platform handler."""

    def __init__(
        self, agent: str, runner: Any, bot_username: str, extra: dict[str, Any] | None = None
    ) -> None:
        self.agent = agent
        self.runner = runner
        self.extra = extra or {}
        self.adapter: Any = None
        self.bot = RecordingBot(bot_username)
        self._update_ids = itertools.count(880001)
        self._message_ids = itertools.count(41)

    async def attach(self) -> None:
        from olano.orchestration.event_bus import get_event_bus
        from olano.runtime.server import RuntimeServer

        host = SimpleNamespace(event_bus=get_event_bus(), _platforms=[], _server_started=False)
        cfg = {"token": "", "extra": {"thinking_message": False, **self.extra}}
        await RuntimeServer._boot_platform_adapter(host, "telegram", cfg, self.runner, self.agent)
        self.adapter = host._platforms[-1]
        self.adapter._bot = self.bot  # the only stand-in: Telegram's network

    def update(self, user: dict[str, Any], text: str) -> dict[str, Any]:
        """A Bot API ``Update`` for a private message, as Telegram sends it."""
        sender = {
            "id": int(user["id"]),
            "is_bot": False,
            "first_name": user["first_name"],
            **({"last_name": user["last_name"]} if user.get("last_name") else {}),
            **({"username": user["username"]} if user.get("username") else {}),
            "language_code": user.get("language_code", "en"),
        }
        chat = {
            k: v for k, v in sender.items() if k in ("id", "first_name", "last_name", "username")
        }
        message: dict[str, Any] = {
            "message_id": next(self._message_ids),
            "from": sender,
            "chat": {**chat, "type": "private"},
            "date": int(time.time()),
            "text": text,
        }
        if text.startswith("/"):
            message["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ]
        return {"update_id": next(self._update_ids), "message": message}

    async def deliver(self, payload: dict[str, Any]) -> None:
        """Hand one ``Update`` to the adapter's own handler, as its webhook/poller would."""
        from telegram import Update

        upd = Update.de_json(payload, None)
        text = (upd.message.text or "") if upd.message else ""
        if text.startswith("/"):
            await self.adapter._handle_command(upd, None)
        else:
            await self.adapter._handle_text_message(upd, None)


def serve(
    seams: dict[str, TelegramSeam],
    loop: asyncio.AbstractEventLoop,
    port: int,
    extra_routes: dict[str, Any] | None = None,
) -> None:
    """A local endpoint the flow posts messages to and reads the bot's calls from.

    POST /message {"agent", "user": {...}, "text"} -> the Update is delivered; answers the Update.
    GET  /calls?agent=..&since=<n> -> the Bot API calls recorded from index n on.
    POST <extra route> {"user_id"} -> ``extra_routes[route](user_id)`` (a team's own hook).
    """
    routes = extra_routes or {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            return None

        def _json(self, body: Any, code: int = 200) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            from urllib.parse import parse_qs, urlparse

            q = parse_qs(urlparse(self.path).query)
            seam = seams.get((q.get("agent") or [""])[0])
            if seam is None:
                self._json({"error": "unknown agent"}, 404)
                return
            since = int((q.get("since") or ["0"])[0])
            with seam.bot.lock:
                calls = list(seam.bot.calls[since:])
            self._json({"calls": calls, "next": since + len(calls)})

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            if self.path in routes:
                self._json({"ok": routes[self.path](str(body.get("user_id", "")))})
                return
            seam = seams.get(body.get("agent", ""))
            if seam is None:
                self._json({"error": "unknown agent"}, 404)
                return
            payload = seam.update(body["user"], body["text"])
            fut = asyncio.run_coroutine_threadsafe(seam.deliver(payload), loop)
            fut.result(timeout=30)
            self._json({"update": payload})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def attach_all(seams: dict[str, TelegramSeam], loop: asyncio.AbstractEventLoop) -> None:
    for seam in seams.values():
        asyncio.run_coroutine_threadsafe(seam.attach(), loop).result(timeout=60)


def channel_runner(runner: Any) -> Any:
    """The demo's scripted runner, writing transcript rows the way the product's runner does.

    ``ScriptedRunner`` stamps every row ``channel: api``. The real ``AgentRunner._append_session``
    stamps the surface the turn arrived on and the sender's name from the live request context, and
    publishes each row (``thread_updated``) so an open conversation in the dashboard updates live.
    This subclass does the same, with the product's own ``channel_for_request`` and
    ``publish_thread_updated``. It also has the three things the platform handler asks a runner
    for (``workspace.root``, ``load_thread_state``, ``record_context_note``).
    """
    base = type(runner)

    class ChannelRunner(base):  # type: ignore[misc, valid-type]
        @property
        def workspace(self) -> Any:
            from pathlib import Path

            return SimpleNamespace(root=Path(str(self.config.workspace)))

        def load_thread_state(self, thread_id: str) -> dict[str, Any]:
            return {}

        def record_context_note(self, thread_id: str, note: str) -> None:
            return None

        def _persist(self, thread_id: str, row: dict[str, Any]) -> None:
            from olano.core.thread_events import publish_thread_updated
            from olano.core.thread_labels import channel_for_request
            from olano.security.request_context import get_request_context

            ctx = get_request_context()
            row = dict(row)
            if ctx.source == "platform":
                row["channel"] = channel_for_request(ctx)
                name = str((ctx.metadata or {}).get("name") or "").strip()
                if row.get("role") == "user" and name and name != str(ctx.user_id or ""):
                    row["sender"] = name
            super()._persist(thread_id, row)
            publish_thread_updated(self.config.name, thread_id, {"ts": _iso(), **row})

        async def astream_events(self, message: Any, thread_id: str | None = None, **kw: Any):
            from olano.core.thread_events import publish_thread_activity
            from olano.core.thread_labels import channel_for_request
            from olano.security.request_context import get_request_context

            channel = channel_for_request(get_request_context())
            tid = thread_id or ""
            publish_thread_activity(self.config.name, tid, "thinking", channel=channel)
            try:
                async for item in super().astream_events(message, thread_id=thread_id, **kw):
                    yield item
            finally:
                publish_thread_activity(self.config.name, tid, "idle", channel=channel)

    # The runtime's publisher for those frames, as ``RuntimeServer`` binds it at boot
    # (``talk_to.bind_runtime(self, publisher=_push_event)``); the launcher binds none.
    from olano.runtime.api import _push_event
    from olano.tools import talk_to

    if getattr(talk_to, "_PUBLISHER", None) is None:
        talk_to._PUBLISHER = _push_event

    new = ChannelRunner.__new__(ChannelRunner)
    new.__dict__.update(runner.__dict__)
    return new


def _iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
