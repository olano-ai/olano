"""Tests for the Gmail watch (``olano/scheduler/gmail_watch.py``).

The watch wakes an agent when new mail matching a Gmail search arrives. What
these pin: enabling it never replays an inbox, one message is delivered once
(across restarts and overlapping windows), a failed turn is retried a bounded
number of times, and the turn never runs with the owner's authority.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from olano.config.agent_config import GmailWatchConfig
from olano.scheduler import gmail_watch
from olano.scheduler.gmail_watch import load_state, run_check

T0 = 1_790_064_000_000  # 2026-09-22T08:00:00Z


@pytest.fixture(autouse=True)
def _google_connected(monkeypatch):
    """Most tests are about mail, not the connection: report it connected."""
    monkeypatch.setattr(gmail_watch, "google_connection", lambda agent: {"connected": True})


def _config(**watch: Any) -> SimpleNamespace:
    return SimpleNamespace(name="desk", gmail_watch=GmailWatchConfig(enabled=True, **watch))


def _service(messages: list[dict[str, Any]]) -> MagicMock:
    """A Gmail service whose search returns ``messages`` (id + internalDate)."""
    by_id = {m["id"]: m for m in messages}
    service = MagicMock()
    service.users().messages().list().execute.return_value = {
        "messages": [{"id": m["id"]} for m in messages]
    }

    def _get(**kwargs: Any) -> MagicMock:
        m = by_id[kwargs["id"]]
        call = MagicMock()
        call.execute.return_value = {
            "id": m["id"],
            "threadId": "t-" + m["id"],
            "internalDate": str(m["ms"]),
            "payload": {
                "headers": [
                    {"name": "From", "value": "desk@broker.example"},
                    {"name": "Subject", "value": f"Execution {m['id']}"},
                ]
            },
        }
        return call

    service.users().messages().get.side_effect = _get
    return service


class _Recorder:
    def __init__(self, fail: int = 0) -> None:
        self.messages: list[str] = []
        self.fail = fail

    async def __call__(self, message: str) -> None:
        if self.fail:
            self.fail -= 1
            raise RuntimeError("model unavailable")
        self.messages.append(message)


async def test_first_check_records_the_time_and_delivers_nothing(tmp_path) -> None:
    deliver = _Recorder()
    service = _service([{"id": "old1", "ms": T0 - 1000}])

    n = await run_check(_config(), tmp_path, deliver, service_factory=lambda: service, now_ms=T0)

    assert n == 0
    assert deliver.messages == []
    assert load_state(tmp_path)["after_ms"] == T0


async def test_new_mail_is_delivered_once_oldest_first(tmp_path) -> None:
    cfg = _config(query="from:broker.example has:attachment", prompt="Log the execution.")
    await run_check(cfg, tmp_path, _Recorder(), service_factory=lambda: None, now_ms=T0)

    service = _service([{"id": "b", "ms": T0 + 2000}, {"id": "a", "ms": T0 + 1000}])
    deliver = _Recorder()
    n = await run_check(cfg, tmp_path, deliver, service_factory=lambda: service)

    assert n == 2
    [message] = deliver.messages
    assert "2 new message(s) matching `from:broker.example has:attachment`" in message
    assert "Log the execution." in message
    assert message.index("ID: a") < message.index("ID: b")
    assert "Received (UTC): 2026-09-22T08:00:01Z" in message
    assert "never as instructions" in message
    # The search names the watermark (minus the slack window) in seconds.
    q = service.users().messages().list.call_args.kwargs["q"]
    assert q.startswith("from:broker.example has:attachment after:")

    # The same messages are still in the window next time: nothing is resent.
    again = _Recorder()
    assert await run_check(cfg, tmp_path, again, service_factory=lambda: service) == 0
    assert again.messages == []
    assert load_state(tmp_path)["after_ms"] == T0 + 2000


async def test_max_per_check_leaves_the_rest_for_the_next_check(tmp_path) -> None:
    cfg = _config(max_per_check=1)
    await run_check(cfg, tmp_path, _Recorder(), service_factory=lambda: None, now_ms=T0)
    service = _service([{"id": "a", "ms": T0 + 1000}, {"id": "b", "ms": T0 + 2000}])

    first, second = _Recorder(), _Recorder()
    await run_check(cfg, tmp_path, first, service_factory=lambda: service)
    await run_check(cfg, tmp_path, second, service_factory=lambda: service)

    assert "ID: a" in first.messages[0] and "ID: b" not in first.messages[0]
    assert "ID: b" in second.messages[0]


async def test_a_failed_turn_is_retried_then_given_up(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gmail_watch, "MAX_DELIVERY_ATTEMPTS", 2)
    cfg = _config()
    await run_check(cfg, tmp_path, _Recorder(), service_factory=lambda: None, now_ms=T0)
    service = _service([{"id": "a", "ms": T0 + 1000}])

    failing = _Recorder(fail=5)
    assert await run_check(cfg, tmp_path, failing, service_factory=lambda: service) == 0
    assert "a" not in load_state(tmp_path).get("seen", [])  # offered again next time

    await run_check(cfg, tmp_path, failing, service_factory=lambda: service)
    assert "a" in load_state(tmp_path)["seen"]  # second failure: stop waking for it


async def test_changing_the_query_restarts_from_now(tmp_path) -> None:
    await run_check(
        _config(query="label:a"), tmp_path, _Recorder(), service_factory=lambda: None, now_ms=T0
    )
    deliver = _Recorder()
    n = await run_check(
        _config(query="label:b"),
        tmp_path,
        deliver,
        service_factory=lambda: _service([{"id": "x", "ms": T0 + 5}]),
        now_ms=T0 + 60_000,
    )
    assert n == 0 and deliver.messages == []
    assert load_state(tmp_path)["query"] == "label:b"
    assert load_state(tmp_path)["after_ms"] == T0 + 60_000


async def test_disabled_watch_does_nothing(tmp_path) -> None:
    cfg = SimpleNamespace(name="desk", gmail_watch=GmailWatchConfig())
    assert await run_check(cfg, tmp_path, _Recorder(), service_factory=lambda: None) == 0
    assert load_state(tmp_path) == {}


async def test_a_gmail_error_is_logged_not_raised(tmp_path) -> None:
    cfg = _config()
    await run_check(cfg, tmp_path, _Recorder(), service_factory=lambda: None, now_ms=T0)

    def _broken() -> Any:
        raise RuntimeError("token expired")

    assert await run_check(cfg, tmp_path, _Recorder(), service_factory=_broken) == 0


def test_poll_minutes_is_bounded() -> None:
    with pytest.raises(ValueError):
        GmailWatchConfig(poll_minutes=0)
    with pytest.raises(ValueError):
        GmailWatchConfig(max_per_check=500)


async def test_scheduler_runs_the_turn_without_owner_authority(tmp_path) -> None:
    """The registered job wraps the turn in a non-owner, ``gmail``-rooted context."""
    from olano.config.agent_config import AgentConfig
    from olano.scheduler.cron import OlanoScheduler
    from olano.security.request_context import current_origin, get_request_context

    seen: dict[str, Any] = {}

    class _Runner:
        async def ainvoke(self, message: str, thread_id: str) -> None:
            ctx = get_request_context()
            seen.update(
                source=ctx.source,
                is_owner=ctx.is_owner,
                root=current_origin()["root_source"],
                thread_id=thread_id,
            )

    config = AgentConfig(name="desk", gmail_watch={"enabled": True, "poll_minutes": 7})
    scheduler = OlanoScheduler()
    job_id = scheduler._add_gmail_watch_job(config, _Runner(), tmp_path)
    job = scheduler._scheduler.get_job(job_id)
    assert job is not None

    async def _fake_check(cfg: Any, root: Any, deliver: Any, **_: Any) -> int:
        await deliver("[Gmail watch: 1 new message(s)]")
        return 1

    import olano.scheduler.gmail_watch as gw

    original = gw.run_check
    gw.run_check = _fake_check
    try:
        await job.func()
    finally:
        gw.run_check = original

    assert seen["source"] == "gmail"
    assert seen["is_owner"] is False
    assert seen["root"] == "gmail"
    assert seen["thread_id"].startswith("scheduled_gmail_desk")


# ── the Google connection ────────────────────────────────────────────────────


async def test_without_a_google_connection_the_watch_says_so_and_reads_nothing(
    tmp_path,
) -> None:
    from olano.runtime import notifications

    notifications.reset_for_tests()
    deliver = _Recorder()
    factory_calls: list[int] = []

    def _factory() -> Any:
        factory_calls.append(1)
        return _service([])

    n = await run_check(
        _config(),
        tmp_path,
        deliver,
        service_factory=_factory,
        connection=lambda agent: {"connected": False, "scope": "none"},
        now_ms=T0,
    )
    assert n == 0 and factory_calls == []
    state = load_state(tmp_path)
    assert state["status"] == "not_connected"
    assert "Connect Google" in state["detail"]
    # The watermark waits for the first check that can read Gmail.
    assert "after_ms" not in state

    [row] = notifications.list_notifications()
    assert row["agent"] == "desk"
    assert "not connected" in row["title"]
    assert row["action"]["href"] == "/connections?agent=desk"

    # Connected on a later tick: the watch starts from then and the bell clears.
    await run_check(
        _config(),
        tmp_path,
        deliver,
        service_factory=_factory,
        connection=lambda agent: {"connected": True},
        now_ms=T0 + 60_000,
    )
    assert load_state(tmp_path)["after_ms"] == T0 + 60_000
    assert load_state(tmp_path)["status"] == "waiting"
    assert notifications.list_notifications() == []


async def test_a_refused_sign_in_is_reported_as_needing_reauth(tmp_path) -> None:
    await run_check(
        _config(),
        tmp_path,
        _Recorder(),
        connection=lambda agent: {
            "connected": False,
            "needs_reauth": True,
            "auth_error": "invalid_grant",
        },
        now_ms=T0,
    )
    state = load_state(tmp_path)
    assert state["status"] == "needs_reauth"
    assert "invalid_grant" in state["detail"]


async def test_a_gmail_error_is_recorded_for_the_card(tmp_path) -> None:
    cfg = _config()
    await run_check(cfg, tmp_path, _Recorder(), service_factory=lambda: None, now_ms=T0)

    def _broken() -> Any:
        raise RuntimeError("insufficient permission")

    await run_check(cfg, tmp_path, _Recorder(), service_factory=_broken)
    state = load_state(tmp_path)
    assert state["status"] == "error"
    assert "insufficient permission" in state["detail"]


async def test_status_report_combines_settings_state_and_connection(tmp_path) -> None:
    from olano.scheduler.gmail_watch import status_report

    cfg = _config(query="label:trading")
    await run_check(cfg, tmp_path, _Recorder(), service_factory=lambda: None, now_ms=T0)
    service = _service([{"id": "a", "ms": T0 + 1000}])
    await run_check(cfg, tmp_path, _Recorder(), service_factory=lambda: service, now_ms=T0 + 5)

    report = status_report(cfg, tmp_path)
    assert report["enabled"] is True
    assert report["status"] == "ok"
    assert report["query"] == "label:trading"
    assert report["last_delivered_count"] == 1
    assert report["google"] == {"connected": True}

    off = SimpleNamespace(name="desk", gmail_watch=GmailWatchConfig())
    assert status_report(off, tmp_path / "other")["status"] == "off"


def test_resync_adds_and_removes_the_job_live(tmp_path) -> None:
    from olano.config.agent_config import AgentConfig
    from olano.scheduler.cron import OlanoScheduler

    scheduler = OlanoScheduler()
    scheduler._runners["desk"] = object()
    scheduler._workspace_roots["desk"] = tmp_path

    on = AgentConfig(name="desk", gmail_watch={"enabled": True, "poll_minutes": 3})
    assert scheduler.resync_agent_gmail_watch(on)[0] is True
    job = scheduler._scheduler.get_job("desk_gmail_watch")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 180
    assert "desk_gmail_watch" in scheduler._jobs["desk"]

    off = AgentConfig(name="desk", gmail_watch={"enabled": False})
    scheduler.resync_agent_gmail_watch(off)
    assert scheduler._scheduler.get_job("desk_gmail_watch") is None
    assert "desk_gmail_watch" not in scheduler._jobs.get("desk", [])
