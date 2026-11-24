"""How external (MCP / A2A) exchanges fold into Agent to Agent conversations.

The dashboard's rail reads ``GET /api/a2a/threads``; these are the pure helpers
behind it. Inbound and remote frames must keep their own kind and protocol —
before this they folded into "peer", so a stranger's call over MCP rendered as
two Olano agents chatting.
"""

from __future__ import annotations

from olano.runtime.api import (
    _a2a_kind,
    _a2a_matches,
    _a2a_protocol,
    _a2a_thread_key,
    _fold_a2a_threads,
)


def _frame(**data):
    return {
        "type": "agent_message",
        "ts": data.get("ts", "2026-09-05T10:00:00+00:00"),
        "data": data,
    }


def test_kind_keeps_inbound_and_remote_and_defaults_to_peer():
    assert _a2a_kind({"kind": "inbound"}) == "inbound"
    assert _a2a_kind({"kind": "remote"}) == "remote"
    assert _a2a_kind({"kind": "subagent"}) == "subagent"
    assert _a2a_kind({"kind": "peer"}) == "peer"
    assert _a2a_kind({}) == "peer"
    assert _a2a_kind({"kind": "something-new"}) == "peer"


def test_protocol_is_explicit_or_read_off_the_party_prefix():
    assert _a2a_protocol({"protocol": "mcp"}) == "mcp"
    assert _a2a_protocol({"protocol": "A2A"}) == "a2a"
    # Frames written before the field existed.
    assert _a2a_protocol({"from": "mcp:Claude desktop", "to": "merli"}) == "mcp"
    assert _a2a_protocol({"from": "merli", "to": "a2a:scout-remote"}) == "a2a"
    assert _a2a_protocol({"from": "merli", "to": "scout"}) == ""
    assert _a2a_protocol({"protocol": "smtp", "from": "x", "to": "y"}) == ""


def test_external_keys_are_directional_and_never_collide_with_peer_keys():
    inbound = {"kind": "inbound", "from": "mcp:Claude", "to": "Merli"}
    remote = {"kind": "remote", "from": "merli", "to": "a2a:Scout"}
    assert _a2a_thread_key(inbound) == "inbound:mcp:claude::merli"
    assert _a2a_thread_key(remote) == "remote:merli::a2a:scout"
    # A peer pair is unordered; an external pair keeps caller → callee.
    assert _a2a_thread_key({"from": "b", "to": "a"}) == _a2a_thread_key({"from": "a", "to": "b"})
    assert _a2a_thread_key(inbound) != _a2a_thread_key({"from": "mcp:Claude", "to": "Merli"})


def test_matches_supports_the_external_umbrella_and_exact_kinds():
    inbound = {"kind": "inbound", "from": "mcp:c", "to": "merli"}
    remote = {"kind": "remote", "from": "merli", "to": "a2a:r"}
    peer = {"kind": "peer", "from": "merli", "to": "scout"}
    for data, expected in ((inbound, True), (remote, True), (peer, False)):
        assert _a2a_matches(data, kind="external", participants=set(), query="") is expected
    assert _a2a_matches(inbound, kind="inbound", participants=set(), query="")
    assert not _a2a_matches(inbound, kind="remote", participants=set(), query="")
    assert not _a2a_matches(inbound, kind="peer", participants=set(), query="")


def test_fold_keeps_kind_protocol_and_thread_id_per_conversation():
    events = [
        _frame(
            kind="inbound",
            protocol="mcp",
            phase="send",
            **{"from": "mcp:Claude desktop", "to": "merli"},
            message="hello",
            thread_id="mcp::claude-desktop::default",
            ts="2026-09-05T10:00:00+00:00",
        ),
        _frame(
            kind="inbound",
            protocol="mcp",
            phase="reply",
            **{"from": "mcp:Claude desktop", "to": "merli"},
            message="hello",
            reply="hi",
            thread_id="mcp::claude-desktop::default",
            ts_sent="2026-09-05T10:00:00+00:00",
            ts_reply="2026-09-05T10:00:02+00:00",
        ),
        _frame(
            kind="remote",
            phase="send",
            **{"from": "merli", "to": "a2a:scout-remote"},
            message="ping",
            ts="2026-09-05T11:00:00+00:00",
        ),
        _frame(kind="peer", phase="send", **{"from": "merli", "to": "scout"}, message="yo"),
    ]
    rows = {row["key"]: row for row in _fold_a2a_threads(events)}
    assert set(rows) == {
        "inbound:mcp:claude desktop::merli",
        "remote:merli::a2a:scout-remote",
        "peer:merli::scout",
    }
    inbound = rows["inbound:mcp:claude desktop::merli"]
    assert inbound["kind"] == "inbound" and inbound["protocol"] == "mcp"
    assert inbound["thread_id"] == "mcp::claude-desktop::default"
    assert (inbound["a"], inbound["b"]) == ("mcp:Claude desktop", "merli")  # caller → agent
    assert inbound["messages"] == 1 and inbound["in_flight"] == 0 and inbound["avg_ms"] == 2000
    remote = rows["remote:merli::a2a:scout-remote"]
    assert remote["kind"] == "remote" and remote["protocol"] == "a2a" and remote["thread_id"] == ""
    peer = rows["peer:merli::scout"]
    assert peer["kind"] == "peer" and peer["protocol"] == ""
