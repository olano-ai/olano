"""`message_policy` at the dashboard/REST seams — `/api/chat`, `/api/chat/stream`.

The unit suite proves `screen_inbound` decides correctly; these prove the
endpoints *ask* it, with a real `AgentConfig` on the runner (a MagicMock config
resolves to "no policy" by design, so the mock fixtures elsewhere never see
this code).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.config.agent_config import AgentConfig
from olano.runtime.api import create_api


def _runner(policy: dict) -> MagicMock:
    r = MagicMock()
    r.config = AgentConfig(name="merli", message_policy=policy)
    r.ainvoke = AsyncMock(return_value="hi from merli")
    return r


def _app(policy: dict) -> tuple[TestClient, MagicMock]:
    runtime = MagicMock()
    runner = _runner(policy)
    runtime._runners = {"merli": runner}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    client = TestClient(create_api(runtime))
    client.post("/api/auth/bootstrap", json={"email": "a@x.com", "password": "pw", "name": "A"})
    return client, runner


def test_an_over_long_message_is_refused_with_the_notice() -> None:
    client, runner = _app({"max_input_chars": 10, "oversize_notice": "Keep it short."})
    res = client.post("/api/chat", json={"agent": "merli", "message": "x" * 50})
    assert res.status_code == 422
    assert res.json()["detail"] == "Keep it short."
    runner.ainvoke.assert_not_awaited()


def test_a_pattern_hit_is_refused_and_named_on_the_console() -> None:
    # On a messenger this is silence; on the operator's own console silence
    # reads as a broken send, so the rule is named instead.
    client, runner = _app({"ignore_patterns": ["buy followers"]})
    res = client.post("/api/chat", json={"agent": "merli", "message": "please BUY FOLLOWERS"})
    assert res.status_code == 422
    assert "reply policy" in res.json()["detail"]
    runner.ainvoke.assert_not_awaited()


def test_truncate_hands_the_runner_the_shortened_text() -> None:
    client, runner = _app({"max_input_chars": 10, "on_oversize": "truncate"})
    res = client.post("/api/chat", json={"agent": "merli", "message": "0123456789ABCDEF"})
    assert res.status_code == 200
    sent = runner.ainvoke.await_args.args[0]
    assert sent.endswith("0123456789") and "ABCDEF" not in sent


def test_the_stream_endpoint_is_gated_the_same_way() -> None:
    client, runner = _app({"max_input_chars": 10})
    res = client.post("/api/chat/stream", json={"agent": "merli", "message": "x" * 50})
    assert res.status_code == 422
    assert "50" in res.json()["detail"] and "10" in res.json()["detail"]
    runner.astream_events.assert_not_called()


def test_a_steer_is_screened_like_a_message() -> None:
    # A steer is text the model reads mid-turn; without this, "hi" followed
    # by a 100k-character aside walks straight around the cap.
    client, runner = _app({"max_input_chars": 10})
    res = client.post(
        "/api/chat/steer", json={"agent": "merli", "message": "x" * 50, "thread_id": "t"}
    )
    assert res.status_code == 422
    runner.steer.assert_not_called()


def test_a_declined_stream_turn_emits_the_console_note_frame() -> None:
    """The runner reports a decline as a typed event; the stream endpoint must
    turn it into the one frame the client draws — `status` / `no_reply` — and
    the marker must not appear in any frame. This is the seam the e2e test
    covers end to end; here it is pinned in isolation."""
    import json

    client, runner = _app({"ignore_rules": ["The message is not in English"]})

    def fake_events(payload, **kwargs):
        async def gen():
            yield ("text", "Let me check that.")
            yield ("message_policy", {"action": "no_reply", "source": "ignore_rules"})

        return gen()

    runner.astream_events = fake_events
    res = client.post("/api/chat/stream", json={"agent": "merli", "message": "hola"})
    assert res.status_code == 200, res.text
    frames = [
        json.loads(line[len("data: ") :])
        for line in res.text.splitlines()
        if line.startswith("data: ")
    ]
    kinds = [(f.get("type"), f.get("kind")) for f in frames]
    assert ("status", "no_reply") in kinds, kinds
    assert not any(f.get("type") == "message_policy" for f in frames)
    assert "[[no_reply]]" not in res.text


def test_a_command_is_never_screened() -> None:
    client, runner = _app({"max_input_chars": 5, "ignore_patterns": [".*"]})
    res = client.post("/api/chat", json={"agent": "merli", "message": "/olano " + "x" * 100})
    # Whatever the command dispatcher does with it, the policy did not refuse it.
    assert res.status_code != 422


@pytest.mark.parametrize("policy", [{}, {"ignore_notice": "hi"}])
def test_an_unconfigured_agent_is_untouched(policy: dict) -> None:
    client, runner = _app(policy)
    res = client.post("/api/chat", json={"agent": "merli", "message": "x" * 100_000})
    assert res.status_code == 200
    runner.ainvoke.assert_awaited_once()
