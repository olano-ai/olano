"""The notifications surface behind the dashboard bell.

The runtime has always known when something needs a human — a connector skipped
for a missing credential, a locked vault, a disk over threshold — but those only
went to the log, so the visible symptom was a bot that quietly stopped replying.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path / "ws"))
    from unittest.mock import MagicMock

    from olano.runtime import notifications
    from olano.runtime.api import create_api

    notifications.reset_for_tests()
    runtime = MagicMock()
    runtime._runners = {}
    runtime._channels = []
    return TestClient(create_api(runtime))


@pytest.fixture
def notifications(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    from olano.runtime import notifications as module

    module.reset_for_tests()
    return module


def test_an_empty_bell_is_not_an_error(client):
    resp = client.get("/api/notifications")

    assert resp.status_code == 200
    assert resp.json() == {"notifications": [], "unread": 0, "total": 0, "level": None}


def test_a_raised_notification_shows_up_unread(client, notifications):
    notifications.notify(
        kind="channel_credential_missing",
        level="error",
        title="telegram is not running for support",
        body="No value for TELEGRAM_BOT_TOKEN.",
        agent="support",
    )

    body = client.get("/api/notifications").json()

    assert body["unread"] == 1
    assert body["level"] == "error"
    row = body["notifications"][0]
    assert row["agent"] == "support"
    assert row["read"] is False
    assert "TELEGRAM_BOT_TOKEN" in row["body"]


def test_the_badge_reports_the_worst_unread_level(client, notifications):
    notifications.notify(kind="a", level="info", title="fyi")
    notifications.notify(kind="b", level="error", title="broken")
    notifications.notify(kind="c", level="warn", title="hmm")

    assert client.get("/api/notifications").json()["level"] == "error"


def test_repeats_of_one_problem_collapse_into_one_row(client, notifications):
    """A boot loop must not stack 200 identical rows in the bell."""
    for _ in range(5):
        notifications.notify(
            kind="channel_credential_missing",
            level="error",
            title="telegram is not running for support",
            dedupe_key="channel_credential_missing:support:telegram",
        )

    body = client.get("/api/notifications").json()

    assert body["total"] == 1
    assert body["notifications"][0]["count"] == 5


def test_a_recurrence_pulls_a_dismissed_row_back_to_unread(client, notifications):
    notifications.notify(kind="x", level="warn", title="disk", dedupe_key="disk_pressure")
    notifications.mark_read()
    assert client.get("/api/notifications").json()["unread"] == 0

    notifications.notify(kind="x", level="error", title="disk", dedupe_key="disk_pressure")

    body = client.get("/api/notifications").json()
    assert body["unread"] == 1
    assert body["level"] == "error"


def test_marking_read_clears_the_badge_without_losing_history(client, notifications):
    notifications.notify(kind="a", level="warn", title="one")
    notifications.notify(kind="b", level="info", title="two")

    resp = client.post("/api/notifications/read", json={})

    assert resp.json()["marked"] == 2
    body = client.get("/api/notifications").json()
    assert body["unread"] == 0
    assert body["total"] == 2  # still listed, just read
    assert body["level"] is None


def test_marking_one_read_leaves_the_others(client, notifications):
    first = notifications.notify(kind="a", level="warn", title="one")
    notifications.notify(kind="b", level="warn", title="two")

    client.post("/api/notifications/read", json={"ids": [first.id]})

    assert client.get("/api/notifications").json()["unread"] == 1


def test_unread_only_filter(client, notifications):
    first = notifications.notify(kind="a", level="warn", title="one")
    notifications.notify(kind="b", level="warn", title="two")
    client.post("/api/notifications/read", json={"ids": [first.id]})

    body = client.get("/api/notifications?include_read=false").json()

    assert [r["title"] for r in body["notifications"]] == ["two"]


def test_dismissing_removes_the_row(client, notifications):
    row = notifications.notify(kind="a", level="warn", title="one")

    assert client.delete(f"/api/notifications/{row.id}").status_code == 200
    assert client.get("/api/notifications").json()["total"] == 0


def test_dismissing_an_unknown_id_is_a_404(client):
    assert client.delete("/api/notifications/nope").status_code == 404


def test_clear_drops_read_rows_and_keeps_unread(client, notifications):
    first = notifications.notify(kind="a", level="warn", title="read me")
    notifications.notify(kind="b", level="warn", title="still open")
    client.post("/api/notifications/read", json={"ids": [first.id]})

    assert client.post("/api/notifications/clear", json={}).json()["cleared"] == 1

    body = client.get("/api/notifications").json()
    assert [r["title"] for r in body["notifications"]] == ["still open"]


def test_clear_all_empties_the_bell(client, notifications):
    notifications.notify(kind="a", level="warn", title="one")
    notifications.notify(kind="b", level="warn", title="two")

    assert client.post("/api/notifications/clear", json={"all": True}).json()["cleared"] == 2
    assert client.get("/api/notifications").json()["total"] == 0


def test_resolving_a_fixed_condition_takes_it_off_the_bell(client, notifications):
    """The bell reflects the world, not the history: a connector that boots
    again clears its own alert."""
    notifications.notify(kind="x", level="error", title="down", dedupe_key="chan:support:telegram")

    assert notifications.resolve("chan:support:telegram") is True

    assert client.get("/api/notifications").json()["total"] == 0


def test_notifications_survive_a_restart(tmp_path, monkeypatch, notifications):
    """They outlive the process that raised them — a disk alert during the
    night must still be there in the morning."""
    notifications.notify(kind="a", level="error", title="persisted", dedupe_key="k")

    notifications.reset_for_tests()  # simulates a fresh process

    rows = notifications.list_notifications()
    assert [r["title"] for r in rows] == ["persisted"]


def test_an_issue_row_saved_before_links_existed_gets_its_link_back(notifications):
    """Surviving a restart cuts both ways: a bell can hold weeks of issue rows
    persisted with no ``action``, and teaching the producer to attach one would
    otherwise fix only the rows raised after it. Recover the link from the row."""
    notifications.notify(  # the shape issue assignments were persisted in
        kind="issue_assigned",
        level="info",
        title="Issue #10 assigned to carlos",
        dedupe_key="issue-10-assign-human-carlos",
    )
    assert notifications.list_notifications()[0]["action"] is None

    notifications.reset_for_tests()  # simulates a fresh process reading the file

    assert notifications.list_notifications()[0]["action"] == {
        "label": "Open issue",
        "href": "/issues?issue=10",
    }


def test_a_row_whose_link_is_not_recoverable_is_left_alone(notifications):
    """Only rows that carry enough to rebuild a link get one — a guess would
    send the operator somewhere the notification never pointed."""
    notifications.notify(kind="issue_assigned", level="info", title="no key at all")
    notifications.notify(kind="disk_pressure", level="warn", title="disk", dedupe_key="disk")

    notifications.reset_for_tests()

    assert [r["action"] for r in notifications.list_notifications()] == [None, None]
