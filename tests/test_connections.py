"""Tests for consent-based customer↔customer connections.

The security contract under test: identity always from the request context,
recipients only from the caller's own consented connections, codes random and
bounded (expiry/uses/revocation), peer platform ids never surfaced to the
other side, deliveries through the platform router, and erasure on forget_me.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from olano.config.agent_config import AgentConfig
from olano.connections.store import ConnectionsStore
from olano.connections.tools import build_invite_message, make_customer_connection_tools
from olano.memory.customer_memory import (
    active_customer_id,
    make_customer_memory_tools,
)
from olano.security.request_context import push_request_context

AGENT = "supportbot"


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _seed(platform: str, user: str) -> str:
    with push_request_context(source="channel", channel=platform, user_id=user):
        mem = make_customer_memory_tools(AGENT)
        _tool(mem, "remember").invoke({"fact": "seeded"})
        return str(active_customer_id())


def _config(**cs_extra) -> AgentConfig:
    return AgentConfig(
        name=AGENT,
        label="Support Bot",
        customer_service={"enabled": True, "connections": True, **cs_extra},
    )


@pytest.fixture
def conn_tools(lionclaw_home):
    return make_customer_connection_tools(AGENT, _config())


@pytest.fixture
def deliveries(monkeypatch):
    """Capture platform deliveries triggered from the connection tools."""
    from olano.connections import tools as conn_tools_mod

    sent: list[tuple[str, str, str]] = []

    def fake_deliver(agent_name, platform, user_id, text):
        sent.append((platform, user_id, text))
        return True

    monkeypatch.setattr(conn_tools_mod, "_deliver_now", fake_deliver)
    return sent


def _invite_code(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("connect "):
            return line.removeprefix("connect ").strip()
    raise AssertionError(f"no connect line in: {out}")


def _connect(conn_tools, deliveries=None) -> str:
    """Alice invites, Bob accepts. Returns the code used."""
    _seed("telegram", "alice")
    _seed("telegram", "bob")
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = _tool(conn_tools, "connection_invite").invoke(
            {"introduce_as": "Sarah, your landlord", "label": "tenant unit 3"}
        )
    code = _invite_code(out)
    with push_request_context(source="channel", channel="telegram", user_id="bob"):
        accepted = _tool(conn_tools, "connection_accept").invoke({"code": code})
    assert "Connected with Sarah, your landlord" in accepted
    return code


# ───────────────────────────── the invite message ─────────────────────────────


def test_invite_is_a_forwardable_message_with_a_paste_line(conn_tools):
    _seed("telegram", "alice")
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = _tool(conn_tools, "connection_invite").invoke(
            {"introduce_as": "Sarah, your landlord", "label": "tenant unit 3"}
        )
    # It reads as a message to the RECIPIENT, carries the agent's name (the
    # config `label` key is not an AgentConfig field, so the name is the
    # display identity), the sender's chosen introduction, and ends in a
    # literal paste-able line.
    assert "Sarah, your landlord would like to connect with you" in out
    assert AGENT in out
    code = _invite_code(out)
    assert len(code) == 9 and code[4] == "-"
    # No contact info configured and no live adapters: the tool says so to
    # the inviter instead of printing a made-up address.
    assert "tell the recipient how to reach me" in out


def test_invite_embeds_operator_contact_info(lionclaw_home):
    tools = make_customer_connection_tools(
        AGENT, _config(connection_contact_info="Telegram: @SupportBot")
    )
    _seed("telegram", "alice")
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = _tool(tools, "connection_invite").invoke({"introduce_as": "Sarah"})
    assert "(Telegram: @SupportBot)" in out
    assert "tell the recipient how to reach me" not in out


def test_invite_message_builder_is_pure():
    msg = build_invite_message(
        agent_label="Support Bot",
        introduce_as="Sarah",
        code="ABCD-2345",
        expires_at="2026-08-16T00:00:00+00:00",
        max_uses=3,
        contact="WhatsApp: +65 8123 4567",
    )
    assert "connect ABCD-2345" in msg
    assert "2026-08-16" in msg and "3 times" in msg and "WhatsApp: +65 8123 4567" in msg


# ───────────────────────────── consent + lifecycle ─────────────────────────────


def test_invite_accept_connects_and_notifies_the_inviter(conn_tools, deliveries):
    _connect(conn_tools, deliveries)
    # The inviter learned of the acceptance through the platform router,
    # addressed by THEIR stored identity — and by label, never by id.
    assert len(deliveries) == 1
    platform, user_id, text = deliveries[0]
    assert (platform, user_id) == ("telegram", "alice")
    assert "accepted" in text and "bob" not in text.lower()


def test_own_invite_cannot_be_self_accepted(conn_tools):
    _seed("telegram", "alice")
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = _tool(conn_tools, "connection_invite").invoke({"introduce_as": "Sarah"})
        code = _invite_code(out)
        refused = _tool(conn_tools, "connection_accept").invoke({"code": code})
    assert "meant for someone else" in refused


def test_bad_or_revoked_codes_fail_generically(conn_tools):
    _seed("telegram", "alice")
    _seed("telegram", "bob")
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = _tool(conn_tools, "connection_invite").invoke({"introduce_as": "Sarah"})
        code = _invite_code(out)
        assert "withdrawn" in _tool(conn_tools, "revoke_connection_invite").invoke({"code": code})
    with push_request_context(source="channel", channel="telegram", user_id="bob"):
        for attempt in (code, "ZZZZ-9999"):
            msg = _tool(conn_tools, "connection_accept").invoke({"code": attempt})
            # Revoked and never-existed answer identically — a guessed code
            # learns nothing.
            assert "does not match any open invite" in msg


def test_single_use_codes_are_spent(conn_tools, deliveries):
    code = _connect(conn_tools, deliveries)
    _seed("telegram", "carol")
    with push_request_context(source="channel", channel="telegram", user_id="carol"):
        msg = _tool(conn_tools, "connection_accept").invoke({"code": code})
    assert "does not match any open invite" in msg


def test_standing_invite_serves_many_and_labels_stay_distinct(conn_tools, deliveries):
    _seed("telegram", "biz")
    with push_request_context(source="channel", channel="telegram", user_id="biz"):
        out = _tool(conn_tools, "connection_invite").invoke(
            {"introduce_as": "Acme Reno", "label": "customer", "uses": 3}
        )
    code = _invite_code(out)
    for user, name in (("u1", "Priya"), ("u2", "")):
        _seed("telegram", user)
        with push_request_context(source="channel", channel="telegram", user_id=user):
            msg = _tool(conn_tools, "connection_accept").invoke({"code": code, "your_name": name})
        assert "Connected with Acme Reno" in msg
    with push_request_context(source="channel", channel="telegram", user_id="biz"):
        listing = _tool(conn_tools, "my_connections").invoke({})
    # The business tells its redeemers apart WITHOUT ever seeing chat ids.
    assert "Priya" in listing and "customer (joined" in listing
    assert "u1" not in listing and "u2" not in listing
    assert f"open invite {code}" in listing and "1 use(s) left" in listing


# ─────────────────────────────── send + sever ────────────────────────────────


def test_send_reaches_only_consented_connections(conn_tools, deliveries):
    _connect(conn_tools, deliveries)
    deliveries.clear()
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = _tool(conn_tools, "send_to_connection").invoke(
            {"connection": "tenant unit 3", "message": "Rent reminder: due Friday."}
        )
        assert "Delivered to 'tenant unit 3'" in out
        # A non-connection is unreachable, however it is named.
        refused = _tool(conn_tools, "send_to_connection").invoke(
            {"connection": "telegram-carol", "message": "hi"}
        )
        assert "No connection matches" in refused
    platform, user_id, text = deliveries[0]
    assert (platform, user_id) == ("telegram", "bob")
    assert "Sarah, your landlord" in text and "Rent reminder: due Friday." in text
    assert "disconnect" in text  # the opt-out hint rides every relay


def test_daily_send_cap(lionclaw_home, deliveries):
    tools = make_customer_connection_tools(AGENT, _config(max_connection_sends_per_day=2))
    _connect(tools, deliveries)
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        for _ in range(2):
            assert "Delivered" in _tool(tools, "send_to_connection").invoke(
                {"connection": "tenant unit 3", "message": "ping"}
            )
        out = _tool(tools, "send_to_connection").invoke(
            {"connection": "tenant unit 3", "message": "ping"}
        )
    assert "Daily limit reached" in out


def test_either_side_can_disconnect_and_sends_stop(conn_tools, deliveries):
    _connect(conn_tools, deliveries)
    with push_request_context(source="channel", channel="telegram", user_id="bob"):
        out = _tool(conn_tools, "disconnect_connection").invoke(
            {"connection": "Sarah, your landlord"}
        )
    assert "Disconnected" in out
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        refused = _tool(conn_tools, "send_to_connection").invoke(
            {"connection": "tenant unit 3", "message": "hello?"}
        )
    assert "No connection matches" in refused


def test_connection_cap(lionclaw_home, deliveries):
    tools = make_customer_connection_tools(AGENT, _config(max_connections_per_user=1))
    _connect(tools, deliveries)  # bob now holds 1 connection
    _seed("telegram", "dora")
    with push_request_context(source="channel", channel="telegram", user_id="dora"):
        out = _tool(tools, "connection_invite").invoke({"introduce_as": "Dora"})
    code = _invite_code(out)
    with push_request_context(source="channel", channel="telegram", user_id="bob"):
        msg = _tool(tools, "connection_accept").invoke({"code": code})
    assert "Connection limit reached" in msg


# ─────────────────────────────── erasure + wiring ─────────────────────────────


def test_forget_me_erases_connections_and_invites(conn_tools, deliveries):
    _connect(conn_tools, deliveries)
    mem = make_customer_memory_tools(AGENT)
    with push_request_context(source="channel", channel="telegram", user_id="bob"):
        _tool(mem, "forget_me").invoke({})
    store = ConnectionsStore(AGENT)
    bob_key = "telegram-bob"
    assert store.connections_for(bob_key) == []
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        listing = _tool(conn_tools, "my_connections").invoke({})
    assert "tenant unit 3" not in listing


def test_tools_refuse_without_an_identified_customer(conn_tools):
    with push_request_context(source="scheduled", channel="scheduler", authenticated=True):
        out = _tool(conn_tools, "connection_invite").invoke({"introduce_as": "X"})
    assert "No identified customer" in out


def test_builder_binds_connection_tools_only_when_opted_in(lionclaw_home, tmp_path: Path):
    from olano.core.builder import _build_extra_tools

    on = _config()
    names_on = {t.name for t in _build_extra_tools(on, tmp_path)}
    assert {"connection_invite", "connection_accept", "send_to_connection"} <= names_on

    off = AgentConfig(name=AGENT, customer_service={"enabled": True})
    names_off = {t.name for t in _build_extra_tools(off, tmp_path)}
    assert not ({"connection_invite", "send_to_connection"} & names_off)


def test_catalog_reports_connection_rows(lionclaw_home):
    from olano.config.internal_tools import effective_internal_tools

    rows = {r["name"]: r for r in effective_internal_tools(_config())}
    for name in ("connection_invite", "connection_accept", "send_to_connection"):
        assert rows[name]["on"] is True, name
    plain = AgentConfig(name=AGENT, customer_service={"enabled": True})
    rows = {r["name"]: r for r in effective_internal_tools(plain)}
    assert rows["connection_invite"]["on"] is False
    assert "connections" in rows["connection_invite"]["note"]
