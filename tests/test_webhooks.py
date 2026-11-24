"""Tests for per-agent outbound webhooks."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

from olano.config.agent_config import AgentConfig, WebhookConfig
from olano.orchestration.event_bus import EventBus
from olano.webhooks.db import WebhookDB
from olano.webhooks.dispatcher import (
    WebhookDispatcher,
    build_inbound_message,
    parse_inbound_body,
    sign_payload,
)

# ─────────────────────────────────────── config model ────────────────────────


def test_webhook_config_defaults() -> None:
    wh = WebhookConfig(name="w", url="https://example.com/hook")
    assert wh.events == ["*"]
    assert wh.enabled is True
    assert len(wh.id) == 12
    assert wh.subscribes_to("message.sent") is True


def test_webhook_config_rejects_bad_url() -> None:
    with pytest.raises(ValueError):
        WebhookConfig(name="w", url="ftp://example.com")


def test_webhook_config_rejects_unknown_event() -> None:
    with pytest.raises(ValueError):
        WebhookConfig(name="w", url="https://x.com", events=["not.a.real.event"])


def test_webhook_subscribes_respects_enabled() -> None:
    wh = WebhookConfig(name="w", url="https://x.com", events=["agent.error"], enabled=False)
    assert wh.subscribes_to("agent.error") is False


def test_webhook_redacted_hides_secret() -> None:
    wh = WebhookConfig(name="w", url="https://x.com", secret="topsecret")
    view = wh.redacted()
    assert view["secret"] == "••••••••"
    assert view["has_secret"] is True
    assert "topsecret" not in json.dumps(view)


def test_agent_config_accepts_webhooks() -> None:
    ac = AgentConfig(
        name="a",
        webhooks=[{"name": "w", "url": "https://x.com/y", "events": ["agent.error"]}],
    )
    assert len(ac.webhooks) == 1
    assert ac.webhooks[0].events == ["agent.error"]


# ─────────────────────────────────────── db ──────────────────────────────────


def test_webhook_db_records_and_stats(tmp_path) -> None:
    db = WebhookDB(db_path=tmp_path / "wh.db")
    db.record(
        agent_name="a",
        webhook_id="w1",
        webhook_name="w",
        event_type="agent.error",
        url="https://x.com",
        ok=True,
        status_code=200,
        duration_ms=12.0,
    )
    db.record(
        agent_name="a",
        webhook_id="w1",
        webhook_name="w",
        event_type="agent.error",
        url="https://x.com",
        ok=False,
        status_code=500,
        error="boom",
    )
    stats = db.stats("a", "w1")
    assert stats["total"] == 2
    assert stats["ok"] == 1
    assert stats["failed"] == 1
    history = db.history("a", "w1")
    assert len(history) == 2
    assert history[0]["ok"] is False  # most recent first


def test_webhook_db_purge(tmp_path) -> None:
    db = WebhookDB(db_path=tmp_path / "wh.db")
    db.record(agent_name="a", webhook_id="w1", webhook_name="w", event_type="x", url="u", ok=True)
    assert db.purge_webhook("a", "w1") == 1
    assert db.history("a", "w1") == []


# ─────────────────────────────────────── delivery ────────────────────────────


class _Receiver:
    """Minimal aiohttp server capturing webhook deliveries."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200
        self.runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/hook", self._handle)
        app.router.add_post("/fail", self._fail)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return f"http://127.0.0.1:{self.port}"

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        self.requests.append({"headers": dict(request.headers), "body": body})
        return web.json_response({"received": True}, status=self.status)

    async def _fail(self, request: web.Request) -> web.Response:
        self.requests.append({"body": await request.read()})
        return web.Response(status=500)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()


async def test_dispatcher_delivers_signed_payload(tmp_path) -> None:
    receiver = _Receiver()
    base = await receiver.start()
    try:
        db = WebhookDB(db_path=tmp_path / "wh.db")
        disp = WebhookDispatcher(EventBus(), db=db)
        wh = WebhookConfig(
            name="w",
            url=f"{base}/hook",
            events=["agent.error"],
            secret="k",
            block_private_addresses=False,
            rate_limit_per_minute=0,
        )
        result = await disp.send(wh, "agent.error", "a", {"err": "x"}, is_test=False)
        assert result["ok"] is True
        assert result["status_code"] == 200
        assert len(receiver.requests) == 1

        req = receiver.requests[0]
        assert req["headers"]["X-Olano-Event"] == "agent.error"
        ts = req["headers"]["X-Olano-Timestamp"]
        expected = sign_payload("k", ts, req["body"])
        assert req["headers"]["X-Olano-Signature"] == expected

        payload = json.loads(req["body"])
        assert payload["event"] == "agent.error"
        assert payload["agent"] == "a"
        assert payload["data"] == {"err": "x"}
    finally:
        await receiver.stop()


async def test_dispatcher_retries_on_5xx(tmp_path) -> None:
    receiver = _Receiver()
    base = await receiver.start()
    try:
        disp = WebhookDispatcher(EventBus(), db=WebhookDB(db_path=tmp_path / "wh.db"))
        wh = WebhookConfig(
            name="w",
            url=f"{base}/fail",
            events=["*"],
            max_retries=2,
            block_private_addresses=False,
            rate_limit_per_minute=0,
            timeout_seconds=5,
        )
        result = await disp.send(wh, "agent.error", "a", {}, is_test=False)
        assert result["ok"] is False
        assert result["status_code"] == 500
        assert result["attempts"] == 3  # 1 + 2 retries
    finally:
        await receiver.stop()


async def test_dispatcher_blocks_private_address(tmp_path) -> None:
    disp = WebhookDispatcher(EventBus(), db=WebhookDB(db_path=tmp_path / "wh.db"))
    wh = WebhookConfig(
        name="w",
        url="http://127.0.0.1:9/hook",
        events=["*"],
        block_private_addresses=True,
    )
    result = await disp.send(wh, "agent.error", "a", {}, is_test=False)
    assert result["ok"] is False
    assert "private" in result["error"].lower()


async def test_dispatcher_routes_events_to_matching_webhooks(tmp_path) -> None:
    receiver = _Receiver()
    base = await receiver.start()
    try:
        bus = EventBus()
        db = WebhookDB(db_path=tmp_path / "wh.db")
        disp = WebhookDispatcher(bus, db=db)
        disp.start()
        wh = WebhookConfig(
            name="w",
            url=f"{base}/hook",
            events=["agent.error"],
            block_private_addresses=False,
            rate_limit_per_minute=0,
        )
        disp.register_agent("a", [wh])

        # Non-matching event → no delivery.
        await bus.emit_simple("message.sent", source="a", agent="a")
        # Matching event → one delivery.
        await bus.emit_simple("agent.error", source="a", agent="a", err="boom")
        await asyncio.sleep(0.2)  # let the spawned delivery task run

        assert len(receiver.requests) == 1
        stats = db.stats("a", wh.id)
        assert stats["total"] == 1
        disp.stop()
    finally:
        await receiver.stop()


async def test_dispatcher_rate_limit(tmp_path) -> None:
    receiver = _Receiver()
    base = await receiver.start()
    try:
        db = WebhookDB(db_path=tmp_path / "wh.db")
        disp = WebhookDispatcher(EventBus(), db=db)
        wh = WebhookConfig(
            name="w",
            url=f"{base}/hook",
            events=["*"],
            rate_limit_per_minute=1,
            block_private_addresses=False,
        )
        await disp._deliver(wh, "a", "agent.error", {})
        await disp._deliver(wh, "a", "agent.error", {})  # should be rate-limited
        # Only the first reached the receiver.
        assert len(receiver.requests) == 1
        stats = db.stats("a", wh.id)
        assert stats["failed"] == 1  # the rate-limited one logged as failure
    finally:
        await receiver.stop()


# ─────────────────────────────────────── inbound ─────────────────────────────


def test_inbound_config_url_optional() -> None:
    wh = WebhookConfig(name="in", direction="inbound", prompt="Process this.")
    assert wh.url == ""
    assert len(wh.ingress_token) == 32
    # Inbound webhooks never fire on the event bus.
    assert wh.subscribes_to("message.sent") is False


def test_outbound_still_requires_url() -> None:
    with pytest.raises(ValueError):
        WebhookConfig(name="out", direction="outbound")


def test_parse_inbound_body_json_form_text() -> None:
    parsed, raw = parse_inbound_body(b'{"a": 1}', "application/json")
    assert parsed == {"a": 1}
    parsed, _ = parse_inbound_body(b"a=1&b=2", "application/x-www-form-urlencoded")
    assert parsed == {"a": "1", "b": "2"}
    parsed, raw = parse_inbound_body(b"hello world", "text/plain")
    assert parsed is None
    assert raw == "hello world"


def test_build_inbound_message_appends_payload_and_filters_noise_headers() -> None:
    wh = WebhookConfig(name="gh", direction="inbound", prompt="Summarise this.")
    msg = build_inbound_message(
        wh,
        parsed={"action": "opened", "number": 7},
        raw_text='{"action": "opened"}',
        headers={"X-GitHub-Event": "issues", "X-Forwarded-For": "1.2.3.4", "Cookie": "secret"},
    )
    assert "Summarise this." in msg
    assert '"action": "opened"' in msg
    assert "X-GitHub-Event: issues" in msg
    assert "X-Forwarded-For" not in msg  # noise header filtered
    assert "Cookie" not in msg  # non-notable header excluded


def test_register_agent_skips_inbound(tmp_path) -> None:
    disp = WebhookDispatcher(EventBus(), db=WebhookDB(db_path=tmp_path / "wh.db"))
    inbound = WebhookConfig(name="in", direction="inbound")
    outbound = WebhookConfig(name="out", url="https://x.com/y")
    disp.register_agent("a", [inbound, outbound])
    registered = disp.webhooks_for("a")
    assert [w.name for w in registered] == ["out"]


async def test_ingest_runs_agent_and_logs_inbound(tmp_path) -> None:
    db = WebhookDB(db_path=tmp_path / "wh.db")
    disp = WebhookDispatcher(EventBus(), db=db)
    wh = WebhookConfig(name="in", direction="inbound", prompt="Handle it.")

    seen: dict = {}

    async def dispatch(message: str, thread_id: str) -> str:
        seen["message"] = message
        seen["thread_id"] = thread_id
        return "done"

    result = await disp.ingest(
        wh,
        "a",
        raw_body=b'{"k": "v"}',
        content_type="application/json",
        headers={},
        dispatch=dispatch,
    )
    assert result["ok"] is True
    assert result["response"] == "done"
    assert "Handle it." in seen["message"]
    assert '"k": "v"' in seen["message"]

    history = db.history("a", wh.id)
    assert len(history) == 1
    assert history[0]["direction"] == "inbound"
    assert history[0]["response_body"] == "done"


async def test_ingest_records_failure_when_agent_raises(tmp_path) -> None:
    db = WebhookDB(db_path=tmp_path / "wh.db")
    disp = WebhookDispatcher(EventBus(), db=db)
    wh = WebhookConfig(name="in", direction="inbound")

    async def boom(message: str, thread_id: str) -> str:
        raise RuntimeError("agent down")

    result = await disp.ingest(
        wh, "a", raw_body=b"{}", content_type="application/json", headers={}, dispatch=boom
    )
    assert result["ok"] is False
    assert "agent down" in result["error"]
    assert db.stats("a", wh.id)["failed"] == 1


async def test_ingest_thread_modes(tmp_path) -> None:
    db = WebhookDB(db_path=tmp_path / "wh.db")
    disp = WebhookDispatcher(EventBus(), db=db)
    threads: list[str] = []

    async def dispatch(message: str, thread_id: str) -> str:
        threads.append(thread_id)
        return "ok"

    persistent = WebhookConfig(name="p", direction="inbound", accumulate_thread=True)
    await disp.ingest(
        persistent, "a", raw_body=b"{}", content_type="", headers={}, dispatch=dispatch
    )
    await disp.ingest(
        persistent, "a", raw_body=b"{}", content_type="", headers={}, dispatch=dispatch
    )
    assert threads[0] == threads[1] == f"webhook-{persistent.id}"

    threads.clear()
    fresh = WebhookConfig(name="f", direction="inbound", accumulate_thread=False)
    await disp.ingest(fresh, "a", raw_body=b"{}", content_type="", headers={}, dispatch=dispatch)
    await disp.ingest(fresh, "a", raw_body=b"{}", content_type="", headers={}, dispatch=dispatch)
    assert threads[0] != threads[1]
