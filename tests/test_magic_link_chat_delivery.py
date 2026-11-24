"""The dashboard chat gets told when a magic link finishes.

The bug these pin: a link minted from the dashboard chat rendered a Connect
card promising "the connection completes here on its own after you sign in",
the user completed it on their phone, and the chat then showed **nothing**.
``watch._notify`` listed ``api`` — the dashboard's channel — among the sources
with no chat to answer, so the ✅ notice was built and dropped; and there was
nothing to deliver it *through* either, since ``APIServerPlatform.send`` is a
stub and the transcript holds no live connection between turns.

So the fork below is the fix, and each half is load-bearing on its own: the
persisted row is what a reload and a second tab read, the SSE frame is what an
already-open chat renders without one.
"""

from __future__ import annotations

import json

import pytest

from olano.magic_links import chat_delivery, flows, store, watch


@pytest.fixture(autouse=True)
def public_origin(monkeypatch):
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://acme.olano.run")
    import olano.core.mcp_setup as mcp_setup

    monkeypatch.setattr(mcp_setup, "_LAST_SEEN_ORIGIN", None)


class _Runner:
    """Stands in for the agent's AgentRunner — only the append is exercised."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append_session_entry(self, thread_id: str, entry: dict) -> None:
        self.rows.append({"thread_id": thread_id, **entry})


class _Runtime:
    def __init__(self, runner: _Runner | None) -> None:
        self._runner = runner

    def get_runner(self, name: str):
        return self._runner


@pytest.fixture
def wired(monkeypatch):
    """A bound runtime plus a captured SSE bus — the two sinks `deliver` uses."""
    runner = _Runner()
    frames: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "olano.tools.talk_to.get_bound_runtime", lambda: _Runtime(runner), raising=False
    )
    monkeypatch.setattr(
        "olano.runtime.api._push_event",
        lambda kind, data: frames.append((kind, data)),
        raising=False,
    )
    return runner, frames


async def _async_ok(runtime, name) -> dict:
    return {"ok": True, "name": name, "status": "running", "restarted": True}


def _link(flow: str = "secret-entry", *, agent: str = "curtis", **params) -> store.LinkRecord:
    record, _ = store.create(
        flow=flow,
        params={"agent": agent, **params},
        requested_by={},
        notify={},
        ttl_minutes=10,
        pin=None,
        minted_by="test",
    )
    return record


# ── the fork ─────────────────────────────────────────────────────────────────


def test_dashboard_notice_is_persisted_and_pushed(wired):
    runner, frames = wired
    delivered = chat_delivery.deliver(
        agent="agentfather",
        thread_id="t-1",
        text="✅ curtis is connected.",
        link_id="hs_abc",
        status="done",
        flow="channel-connect",
        label="Connect channels",
        target_agent="curtis",
        apply=flows.APPLY_RESTARTED,
    )

    assert delivered is True
    (row,) = runner.rows
    assert row["thread_id"] == "t-1"
    assert row["role"] == "system"
    # The marker, not the wording — the thread endpoint keys its one exception
    # to "system rows stay hidden" on exactly this.
    assert row["event"] == chat_delivery.EVENT_KIND == "magic_link"
    assert row["link_id"] == "hs_abc"
    assert row["target_agent"] == "curtis"
    assert row["apply"] == "restarted"

    (kind, data) = frames[0]
    assert kind == "magic_link_updated"
    # `agent` is mandatory: auth.event_visible_to_agents scopes a frame by it,
    # so an unattributed frame would reach members with no grant on this agent.
    assert data["agent"] == "agentfather"
    assert data["thread_id"] == "t-1"
    assert data["text"] == "✅ curtis is connected."


@pytest.mark.parametrize("thread_id", ["", "owner"])
def test_no_thread_means_nothing_is_delivered(wired, thread_id):
    """`owner` is the mint paths' "I don't know which chat" sentinel.

    A messenger resolves it to the owner's DM; taking it literally here would
    create an `owner.jsonl` transcript that no thread view ever opens.
    """
    runner, frames = wired
    assert (
        chat_delivery.deliver(
            agent="agentfather",
            thread_id=thread_id,
            text="✅",
            link_id="hs_abc",
            status="done",
            flow="secret-entry",
            label="API key",
            target_agent="curtis",
            apply=flows.APPLY_RESTART,
        )
        is False
    )
    assert runner.rows == []
    assert frames == []


async def test_messenger_delivery_is_untouched(monkeypatch):
    """The dashboard fork must not cost Telegram/WhatsApp its notice."""
    sent: list = []

    class _Registry:
        async def send(self, envelope):
            sent.append(envelope)

    monkeypatch.setattr("olano.platforms.get_channel_registry", lambda: _Registry(), raising=False)
    await watch._notify(
        {"channel_type": "telegram", "target": "12345", "agent_name": "merli"},
        "✅ done",
        {
            "link_id": "hs_x",
            "status": "done",
            "flow": "f",
            "label": "L",
            "target_agent": "curtis",
            "apply": flows.APPLY_NONE,
        },
    )
    assert len(sent) == 1
    assert sent[0].channel_type == "telegram"
    assert sent[0].target == "12345"


@pytest.mark.parametrize(
    ("apply", "expected"),
    [
        (flows.APPLY_RESTARTED, "curtis has been restarted, so it is live now."),
        (flows.APPLY_RELOADED, "curtis's tools were reloaded, so they are usable now."),
        (flows.APPLY_RESTART_NEEDED, "Restart curtis for it to pick this up."),
        (flows.APPLY_NONE, None),
    ],
)
async def test_a_messenger_gets_the_consequence_in_the_sentence(monkeypatch, apply, expected):
    """A messenger has no card and no buttons, so what the outcome means for
    the agent has to be in the prose.

    It is appended here rather than baked into the flow's ``result`` because
    only ``post_complete`` knows whether the restart it attempted worked —
    channel-connect's result used to assert "The agent restarts to go live"
    unconditionally, which was a claim it could not back.
    """
    sent: list = []

    class _Registry:
        async def send(self, envelope):
            sent.append(envelope)

    monkeypatch.setattr("olano.platforms.get_channel_registry", lambda: _Registry(), raising=False)
    await watch._notify(
        {"channel_type": "telegram", "target": "12345", "agent_name": "merli"},
        "✅ curtis is connected — weixin: x.",
        {
            "link_id": "hs_x",
            "status": "done",
            "flow": "channel-connect",
            "label": "Connect channels",
            "target_agent": "curtis",
            "apply": apply,
        },
    )
    text = sent[0].text
    assert text.startswith("✅ curtis is connected — weixin: x.")
    if expected is None:
        assert text == "✅ curtis is connected — weixin: x."
    else:
        assert text.endswith(expected)


async def test_the_dashboard_text_carries_no_consequence_sentence(wired):
    """The card renders the consequence as its own line with a button, so
    duplicating it in the body would say the same thing twice."""
    runner, _ = wired
    await watch._notify(
        {"channel_type": "api", "target": "t-1", "agent_name": "agentfather"},
        "✅ curtis is connected — weixin: x.",
        {
            "link_id": "hs_x",
            "status": "done",
            "flow": "channel-connect",
            "label": "Connect channels",
            "target_agent": "curtis",
            "apply": flows.APPLY_RESTARTED,
        },
    )
    assert runner.rows[0]["content"] == "✅ curtis is connected — weixin: x."


@pytest.mark.parametrize("channel", ["", "local", "cli"])
async def test_non_chat_sources_still_deliver_nothing(wired, channel):
    runner, frames = wired
    await watch._notify(
        {"channel_type": channel, "target": "t-1", "agent_name": "merli"},
        "✅ done",
        {
            "link_id": "hs_x",
            "status": "done",
            "flow": "f",
            "label": "L",
            "target_agent": "curtis",
            "apply": flows.APPLY_NONE,
        },
    )
    assert runner.rows == []
    assert frames == []


def test_the_persisted_row_is_json_serialisable(wired):
    """It is written to a JSONL transcript, so it has to survive a round-trip."""
    runner, _ = wired
    chat_delivery.deliver(
        agent="agentfather",
        thread_id="t-1",
        text="✅ curtis is connected.",
        link_id="hs_abc",
        status="done",
        flow="channel-connect",
        label="Connect channels",
        target_agent="curtis",
        apply=flows.APPLY_RESTARTED,
    )
    assert json.loads(json.dumps(runner.rows[0]))["event"] == "magic_link"


# ── finalize: once, and with the right apply outcome ─────────────────────────


async def test_finalize_delivers_once_per_link(wired):
    """`claim_finalize` is the persisted guard — the page poll, the background
    watcher and the restart resume all call finalize, and only one may write."""
    runner, frames = wired
    record = _link()
    store.mutate(
        record.id,
        lambda c: c.notify.update(
            {"channel_type": "api", "target": "t-1", "agent_name": "agentfather"}
        ),
    )
    store.finish(record.id, status=store.STATUS_DONE, result="STRIPE_API_KEY saved for curtis.")

    await watch.finalize(record.id)
    await watch.finalize(record.id)

    assert len(runner.rows) == 1
    assert len(frames) == 1


async def _finalize_channel_connect(record) -> None:
    store.mutate(
        record.id,
        lambda c: c.notify.update(
            {"channel_type": "api", "target": "t-1", "agent_name": "agentfather"}
        ),
    )
    store.finish(record.id, status=store.STATUS_DONE, result="curtis is connected — weixin: x.")
    await watch.finalize(record.id)


async def test_a_restart_that_worked_is_reported_as_done(wired, monkeypatch):
    """channel-connect restarts the agent itself, so the notice states that
    rather than asking a question the backend already answered."""
    runner, _ = wired
    monkeypatch.setattr(
        "olano.runtime.agent_lifecycle.restart_agent",
        _async_ok,
        raising=False,
    )
    await _finalize_channel_connect(_link("channel-connect", platforms=["weixin"]))
    assert runner.rows[0]["apply"] == flows.APPLY_RESTARTED


async def test_a_restart_that_could_not_run_asks_for_one(wired, monkeypatch):
    """The case the user hit. The auto-restart is best-effort and used to fail
    behind a `logger.warning` nobody reads, leaving a "connected" agent still
    serving its old config. Saying so is what turns it into a button."""
    runner, _ = wired

    async def _boom(runtime, name):
        raise RuntimeError("boot failed")

    monkeypatch.setattr("olano.runtime.agent_lifecycle.restart_agent", _boom, raising=False)
    await _finalize_channel_connect(_link("channel-connect", platforms=["weixin"]))
    assert runner.rows[0]["apply"] == flows.APPLY_RESTART_NEEDED


async def test_a_failed_link_never_offers_a_restart(wired):
    runner, frames = wired
    record = _link()
    store.mutate(
        record.id,
        lambda c: c.notify.update(
            {"channel_type": "api", "target": "t-1", "agent_name": "agentfather"}
        ),
    )
    store.finish(record.id, status=store.STATUS_EXPIRED, error="The link expired.")

    await watch.finalize(record.id)

    row = runner.rows[0]
    assert row["status"] == store.STATUS_EXPIRED
    assert row["apply"] == flows.APPLY_NONE
    assert row["content"].startswith("⚠️")


async def test_delivery_failure_never_breaks_finalize(monkeypatch):
    """The tokens are already on disk — a dead sink must not raise past here."""

    class _Exploding:
        def get_runner(self, name):
            raise RuntimeError("runtime is gone")

    monkeypatch.setattr(
        "olano.tools.talk_to.get_bound_runtime", lambda: _Exploding(), raising=False
    )
    record = _link()
    store.mutate(
        record.id,
        lambda c: c.notify.update(
            {"channel_type": "api", "target": "t-1", "agent_name": "agentfather"}
        ),
    )
    store.finish(record.id, status=store.STATUS_DONE, result="saved.")

    await watch.finalize(record.id)  # must not raise


# ── the apply declaration ────────────────────────────────────────────────────


def test_every_flow_declares_a_known_apply_value():
    known = {flows.APPLY_SELF, flows.APPLY_RESTART, flows.APPLY_NONE}
    for flow in flows.all_flows():
        assert getattr(flow, "applies", None) in known, flow.id


def test_a_deployment_wide_link_has_no_agent_to_restart():
    """`APPLY_RESTART` needs a target; a link minted with no agent degrades to
    `none` rather than rendering a button that cannot name what it restarts."""
    flow = flows.get_flow("secret-entry")
    assert flow.applies == flows.APPLY_RESTART
    assert flows.resolve_apply(flow, _link(agent="curtis")) == flows.APPLY_RESTART
    assert flows.resolve_apply(flow, _link(agent="")) == flows.APPLY_NONE


def test_a_reported_outcome_outranks_the_declaration():
    flow = flows.get_flow("channel-connect")
    assert flow.applies == flows.APPLY_SELF
    reported = flows.resolve_apply(flow, _link(), flows.APPLY_RESTART_NEEDED)
    assert reported == flows.APPLY_RESTART_NEEDED


def test_a_restart_nobody_can_target_degrades_to_nothing():
    """`restart_needed` reported for a deployment-wide link is still unusable —
    there is no agent name to put on the button, so it degrades like the
    declaration does rather than promising an action with no target."""
    flow = flows.get_flow("channel-connect")
    assert (
        flows.resolve_apply(flow, _link(agent=""), flows.APPLY_RESTART_NEEDED) == flows.APPLY_NONE
    )
    assert (
        flows.resolve_apply(flow, _link(agent="curtis"), flows.APPLY_RESTART_NEEDED)
        == flows.APPLY_RESTART_NEEDED
    )
