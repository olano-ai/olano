"""The browser's request guard, the consequential-action gate, and the runner's
always-ask carve-outs."""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict
from unittest.mock import AsyncMock, MagicMock

import pytest

from olano.browser import consequence
from olano.browser.guard import (
    GuardPolicy,
    GuardState,
    blocked_message,
    guard_page,
    install_guards,
    metadata_refusal,
    url_refusal_sync,
)


def _pol(**kw: Any) -> GuardPolicy:
    return GuardPolicy(**{"allow": [], "deny": [], "block_private": True, **kw})


# ── guard ────────────────────────────────────────────────────────────────────


def test_schemes():
    assert url_refusal_sync("file:///etc/passwd", _pol())
    assert url_refusal_sync("chrome://settings", _pol())
    assert url_refusal_sync("data:text/html,hi", _pol()) is None
    assert url_refusal_sync("about:blank", _pol()) is None


def test_private_and_loopback_blocked_by_default():
    assert "private" in (url_refusal_sync("http://127.0.0.1:8765/api/status", _pol()) or "")
    assert url_refusal_sync("http://10.0.0.5/", _pol())
    assert url_refusal_sync("http://localhost/", _pol())


def test_metadata_blocked_even_with_private_allowed():
    pol = _pol(block_private=False)
    assert url_refusal_sync("http://10.0.0.5/", pol) is None
    assert "metadata" in url_refusal_sync("http://169.254.169.254/latest/", pol)
    assert url_refusal_sync("http://metadata.google.internal/", pol)
    assert url_refusal_sync("http://169.254.1.1/", pol)  # link-local


def test_allow_and_deny_lists():
    pol = _pol(allow=["example.com"], block_private=False)
    assert url_refusal_sync("https://www.example.com/x", pol) is None
    assert "allowlist" in url_refusal_sync("https://evil.test/", pol)
    deny = _pol(deny=["evil.test"], block_private=False)
    assert "deny" in url_refusal_sync("https://a.evil.test/", deny)


def test_policy_captured_from_the_governed_call():
    from olano.security.egress import ALLOW_KEY, DENY_KEY
    from olano.security.request_context import push_request_context

    with push_request_context(metadata={"security": {ALLOW_KEY: ["a.test"], DENY_KEY: ["b.test"]}}):
        pol = GuardPolicy.capture()
    assert pol.allow == ["a.test"] and pol.deny == ["b.test"] and pol.block_private


async def test_interceptor_aborts_refused_requests_and_records_them():
    ctx = MagicMock()
    handlers: dict[str, Any] = {}

    async def route(pattern, handler):
        handlers["http"] = handler

    async def route_ws(pattern, handler):
        handlers["ws"] = handler

    ctx.route = route
    ctx.route_web_socket = route_ws
    state = GuardState(policy=_pol(allow=["ok.test"], block_private=False))
    await install_guards(ctx, state)

    blocked_route, allowed_route = MagicMock(), MagicMock()
    for r in (blocked_route, allowed_route):
        r.abort = AsyncMock()
        r.continue_ = AsyncMock()
    await handlers["http"](blocked_route, MagicMock(url="https://evil.test/steal?x=1"))
    await handlers["http"](allowed_route, MagicMock(url="https://ok.test/page"))
    blocked_route.abort.assert_awaited_once()
    blocked_route.continue_.assert_not_awaited()
    allowed_route.continue_.assert_awaited_once()
    assert state.last_block_for("https://evil.test/steal?x=1")

    ws = MagicMock(url="wss://evil.test/socket")
    ws.close = AsyncMock()
    await handlers["ws"](ws)
    ws.close.assert_awaited_once()
    ws.connect_to_server.assert_not_called()


@pytest.mark.parametrize(
    "host",
    ["169.254.169.254", "[::ffff:169.254.169.254]", "::ffff:a9fe:a9fe", "metadata", "fe80::1"],
)
def test_metadata_is_refused_in_every_spelling(host):
    assert metadata_refusal(host)


async def _route_handler(state: GuardState):
    handlers: dict[str, Any] = {}
    ctx = MagicMock()

    async def route(pattern, handler):
        handlers["http"] = handler

    ctx.route = route
    ctx.route_web_socket = None
    await install_guards(ctx, state)
    return handlers["http"]


def _navigation(url: str) -> MagicMock:
    request = MagicMock(url=url)
    request.is_navigation_request.return_value = True
    request.frame.parent_frame = None
    return request


def _route() -> MagicMock:
    route = MagicMock()
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    route.fulfill = AsyncMock()
    response = MagicMock(status=200, headers={})
    route.fetch = AsyncMock(return_value=response)
    return route


async def test_navigations_are_left_to_chromium():
    """The handler never fetches a page itself: that fetch is Playwright's HTTP
    client, whose TLS fingerprint is no browser's (module docstring). Redirect
    hops are checked by ``guard_page`` instead."""
    for policy in (_pol(), _pol(allow=["example.com"]), _pol(block_private=False)):
        handler = await _route_handler(GuardState(policy=policy))
        route = _route()
        await handler(route, _navigation("https://example.com/"))
        route.continue_.assert_awaited_once()
        route.fetch.assert_not_awaited()
        route.fulfill.assert_not_awaited()


async def test_private_documents_are_refused_when_private_is_blocked():
    handler = await _route_handler(GuardState(policy=_pol()))
    route = _route()
    await handler(route, _navigation("http://10.0.0.5/app"))
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()


async def test_a_site_that_turned_the_browser_away_is_refused_at_the_request():
    """A typed address, a clicked link and a script's navigation all pass
    here, so the memory of a refusal stops every one of them before sending."""
    state = GuardState(
        policy=_pol(),
        site_refusal=lambda url: "Automation blocked: Not opened." if "reddit" in url else None,
    )
    handler = await _route_handler(state)
    refused, other, frame = _route(), _route(), _route()
    await handler(refused, _navigation("https://old.reddit.com/r/python"))
    await handler(other, _navigation("https://example.com/"))
    sub = _navigation("https://www.reddit.com/embed")
    sub.frame.parent_frame = MagicMock()  # a frame, not the page the agent opened
    await handler(frame, sub)
    refused.abort.assert_awaited_once()
    other.continue_.assert_awaited_once()
    frame.continue_.assert_awaited_once()
    (entry,) = state.drain()
    assert entry["site_block"] and entry["navigation"]
    assert blocked_message(entry) == "Automation blocked: Not opened."


class _FakeCdp:
    """A DevTools session: records what was sent, hands back a frame tree."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.handlers: dict[str, Any] = {}

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params or {}))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "MAIN"}}}
        return {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler


async def _armed(state: GuardState, **kw: Any) -> _FakeCdp:
    cdp = _FakeCdp()
    context = MagicMock()
    context.new_cdp_session = AsyncMock(return_value=cdp)
    assert await guard_page(context, MagicMock(), state, **kw) is cdp
    patterns = guard_page.__globals__["_HOP_PATTERNS"]
    assert ("Fetch.enable", {"patterns": patterns, "handleAuthRequests": True}) in cdp.sent
    return cdp


async def test_guard_page_answers_the_proxy_challenge_and_hands_a_site_s_on():
    """The hop guard's session sees a 407 before Playwright's answer gets its
    turn, so it signs in to the proxy itself; a site's own challenge keeps
    the default handling."""
    cdp = await _armed(GuardState(policy=_pol()), proxy_auth=("agent", "s3cret"))
    auth = cdp.handlers["Fetch.authRequired"]
    await auth({"requestId": "r1", "authChallenge": {"source": "Proxy", "origin": "http://gw"}})
    await auth({"requestId": "r2", "authChallenge": {"source": "Server", "origin": "http://s"}})
    assert cdp.sent[-2:] == [
        (
            "Fetch.continueWithAuth",
            {
                "requestId": "r1",
                "authChallengeResponse": {
                    "response": "ProvideCredentials",
                    "username": "agent",
                    "password": "s3cret",
                },
            },
        ),
        (
            "Fetch.continueWithAuth",
            {"requestId": "r2", "authChallengeResponse": {"response": "Default"}},
        ),
    ]
    unproxied = await _armed(GuardState(policy=_pol()))
    await unproxied.handlers["Fetch.authRequired"](
        {"requestId": "r3", "authChallenge": {"source": "Proxy", "origin": "http://gw"}}
    )
    assert unproxied.sent[-1][1]["authChallengeResponse"] == {"response": "Default"}


def _paused(url: str, *, hop: bool, frame: str = "MAIN") -> dict:
    event = {"requestId": "r1", "request": {"url": url}, "frameId": frame}
    if hop:
        event["redirectedRequestId"] = "r0"
    return event


async def test_guard_page_fails_a_refused_redirect_hop_before_it_is_sent():
    state = GuardState(policy=_pol(allow=["example.com"]))
    cdp = await _armed(state)
    await cdp.handlers["Fetch.requestPaused"](_paused("https://evil.example.net/", hop=True))
    assert cdp.sent[-1] == (
        "Fetch.failRequest",
        {"requestId": "r1", "errorReason": "BlockedByClient"},
    )
    (entry,) = state.drain()
    assert entry["redirect"] and entry["navigation"] and entry["sent"] is False
    assert blocked_message(entry).endswith("The redirect was not followed.")


async def test_guard_page_continues_everything_else():
    state = GuardState(policy=_pol(allow=["example.com"]))
    cdp = await _armed(state)
    handler = cdp.handlers["Fetch.requestPaused"]
    await handler(_paused("https://evil.example.net/", hop=False))  # the route decides
    await handler(_paused("https://example.com/next", hop=True))  # an allowed hop
    await handler(_paused("https://evil.example.net/", hop=True, frame="F2"))  # a frame's hop
    assert [m for m, _ in cdp.sent[-3:]] == [
        "Fetch.continueRequest",
        "Fetch.continueRequest",
        "Fetch.failRequest",
    ]
    (entry,) = state.drain()
    assert entry["navigation"] is False  # a frame, not the page


async def test_guard_page_without_devtools_keeps_the_page():
    context = MagicMock()
    context.new_cdp_session = AsyncMock(side_effect=RuntimeError("no cdp"))
    assert await guard_page(context, MagicMock(), GuardState(policy=_pol())) is None


# ── what counts as consequential ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"tag": "button", "label": "Place order", "submits": True, "method": "post"}, True),
        ({"tag": "button", "label": "Continue", "submits": True, "method": "post"}, True),
        ({"tag": "button", "label": "Search", "submits": True, "method": "get"}, False),
        ({"tag": "div", "label": "Send", "submits": False}, True),
        ({"tag": "button", "label": "Delete repository", "submits": False}, True),
        ({"tag": "button", "label": "Pay $20", "submits": False}, True),
        ({"tag": "button", "label": "Accept cookies", "submits": False}, False),
        ({"tag": "button", "label": "Apply filters", "submits": False}, False),
        ({"tag": "a", "label": "Pricing", "submits": False}, False),
        ({"tag": "a", "label": "Posts", "submits": False}, False),
        (None, False),
    ],
)
def test_judge(info, expected):
    assert consequence.judge(info, what="click").consequential is expected


def test_ask_approval_outside_a_graph_refuses():
    verdict = consequence.Verdict(True, "the control is labelled 'Pay'", "Pay")
    result = consequence.ask_approval(
        "browser_click", url="https://shop.test", action="click 'Pay'", verdict=verdict
    )
    assert result and result.startswith("Not done:")


def test_scratchpad_key_matches_langgraph():
    from langgraph._internal._constants import CONFIG_KEY_SCRATCHPAD

    assert consequence._SCRATCHPAD_KEY == CONFIG_KEY_SCRATCHPAD


class _S(TypedDict, total=False):
    result: str


def _graph():
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    calls: list[str] = []

    def act(state: _S) -> _S:
        verdict = consequence.Verdict(True, "the control is labelled 'Pay'", "Pay now")
        refusal = consequence.ask_approval(
            "browser_click", url="https://shop.test/cart", action="click 'Pay now'", verdict=verdict
        )
        if refusal:
            return {"result": refusal}
        calls.append("clicked")
        return {"result": "clicked"}

    g = StateGraph(_S)
    g.add_node("act", act)
    g.add_edge(START, "act")
    g.add_edge("act", END)
    return g.compile(checkpointer=InMemorySaver()), calls


@pytest.mark.parametrize(("decision", "clicked"), [("approve", True), ("reject", False)])
def test_interrupt_round_trip_in_a_real_graph(decision, clicked):
    """The request is shaped like deepagents' own (the runner extracts it), and
    the resume value the runner sends decides it."""
    from langgraph.types import Command

    from olano.core.runner import AgentRunner

    app, calls = _graph()
    cfg = {"configurable": {"thread_id": f"t-{decision}"}}
    app.invoke({}, cfg)
    state = app.get_state(cfg)
    actions = AgentRunner._extract_interrupt_actions(state)
    assert len(actions) == 1
    assert actions[0]["name"] == "browser_click"
    assert actions[0]["args"]["url"] == "https://shop.test/cart"
    assert "Pay" in actions[0]["args"]["why_approval"]
    out = app.invoke(Command(resume={"decisions": [{"type": decision}]}), cfg)
    assert (calls == ["clicked"]) is clicked
    assert out["result"] == "clicked" if clicked else out["result"].startswith("Not done:")


# ── runner carve-outs ────────────────────────────────────────────────────────


def _action(name: str = "browser_click") -> dict[str, Any]:
    return {"name": name, "args": {"url": "https://shop.test"}, "interrupt_id": "x"}


def test_browser_actions_always_ask(monkeypatch):
    from olano.core import runner

    cfg = MagicMock()
    cfg.name = "merli"
    cfg.security.hitl_auto_approve_low_risk = True
    monkeypatch.setattr("olano.security.hitl.is_tool_rule_allowed", lambda *a: True)
    assert runner._always_asks_human(_action())
    assert not runner._is_action_auto_approved(_action(), cfg)  # no remembered rule, no low-risk
    assert runner._classify_interrupt_action_risk(_action()) == "high"
    assert not runner._always_asks_human(_action("write_file"))


@pytest.mark.parametrize("root", ["scheduled", "cognition", "peer-unknown", "human"])
def test_autonomous_runs_defer_browser_actions_to_a_person(monkeypatch, root):
    from olano.core import runner

    deferred: list[str] = []

    async def fake_defer(action, config, origin, risk):
        deferred.append(action["name"])
        return {"type": "reject"}

    monkeypatch.setattr(runner, "_defer_to_inbox", fake_defer)
    monkeypatch.setattr(
        "olano.config.system_settings.auto_approve_enabled", lambda config: True, raising=False
    )
    decisions = asyncio.run(
        runner._resolve_autonomous_decisions([_action()], MagicMock(), {"root_source": root})
    )
    assert deferred == ["browser_click"]
    assert decisions == [{"type": "reject"}]


# ── the agent's pointer, for the live view's cursor ──────────────────────────


def _pointer_session(viewers: int):
    from olano.browser.sessions import Session

    page = MagicMock()
    page.viewport_size = {"width": 1000, "height": 500}
    page.mouse.move = AsyncMock()
    return Session(agent="a", kind="live", context=MagicMock(), page=page, viewers=viewers)


def test_note_pointer_normalises_and_tells_every_listener():
    from olano.browser.sessions import note_pointer

    sess = _pointer_session(1)
    got: list[dict] = []
    sess.pointer_listeners.add(got.append)
    note_pointer(sess, 250, 500, "click")
    assert got[0]["x"] == 0.25 and got[0]["y"] == 1.0 and got[0]["kind"] == "click"
    assert sess.pointer == got[0]
    note_pointer(sess, -50, 9999, "move")  # clamped into the frame
    assert (got[1]["x"], got[1]["y"]) == (0.0, 1.0)


async def test_aim_does_nothing_while_nobody_watches():
    from olano.browser.sessions import aim

    sess = _pointer_session(0)
    locator = MagicMock()
    locator.bounding_box = AsyncMock(return_value={"x": 0, "y": 0, "width": 10, "height": 10})
    await aim(sess, locator, "click")
    locator.bounding_box.assert_not_awaited()
    sess.page.mouse.move.assert_not_awaited()
    assert sess.pointer is None


async def test_aim_glides_the_mouse_then_reports_the_click(monkeypatch):
    from olano.browser import sessions

    monkeypatch.setattr(sessions, "POINTER_SETTLE_S", 0)
    sess = _pointer_session(1)
    got: list[dict] = []
    sess.pointer_listeners.add(got.append)
    locator = MagicMock()
    locator.bounding_box = AsyncMock(return_value={"x": 100, "y": 50, "width": 200, "height": 100})
    await sessions.aim(sess, locator, "click")
    sess.page.mouse.move.assert_awaited_once_with(200, 100, steps=sessions.POINTER_STEPS)
    assert [e["kind"] for e in got] == ["move", "click"]
    assert (got[-1]["x"], got[-1]["y"]) == (0.2, 0.2)


async def test_aim_never_raises_on_a_target_without_a_box():
    from olano.browser.sessions import aim

    sess = _pointer_session(1)
    locator = MagicMock()
    locator.bounding_box = AsyncMock(side_effect=RuntimeError("detached"))
    await aim(sess, locator, "click")
    assert sess.pointer is None
