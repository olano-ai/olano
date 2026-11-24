"""Thumbs up / down on a reply: kept on the box, scored in Langfuse when traced.

- The store: one vote per person per reply, the last row wins, 0 takes it
  back, comments capped, counts per agent over a window.
- The route: anyone who may read the thread may vote (a member without the
  agent may not); the reply must exist in the transcript, which also supplies
  the trace id (a client can never aim a score at a trace of its choosing).
- A reload reads the caller's own vote back, and nobody else's.
- Deleting the thread deletes its votes.
- Langfuse hears of a vote only when the reply was traced and tracing is still
  on; the comment never goes in ``metadata`` capture.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from olano.config.agent_config import AgentConfig
from olano.config.home import get_olano_paths
from olano.config.loader import save_agent_config
from olano.core import feedback as store

TRACE = "c" * 32


def _seed(agent: str, thread_id: str, *, traced: bool = True, turn: str = "turn-1") -> None:
    sessions = get_olano_paths().sessions_dir / agent
    sessions.mkdir(parents=True, exist_ok=True)
    reply: dict = {"role": "assistant", "content": "Here you go.", "turn_id": turn}
    if traced:
        reply["trace_id"] = TRACE
    rows = [{"role": "user", "content": "hi"}, reply, {"role": "assistant", "content": "old"}]
    (sessions / f"{thread_id}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


# ── the store ─────────────────────────────────────────────────────────────────


def test_last_vote_wins_and_zero_takes_it_back(lionclaw_home):
    store.record_vote("merli", "t1", "turn-1", user="u1", value=1)
    store.record_vote("merli", "t1", "turn-1", user="u2", value=-1, comment="no")
    assert store.votes_for("merli", "t1", "u1")["turn-1"].value == 1
    store.record_vote("merli", "t1", "turn-1", user="u1", value=-1, comment="  wrong  ")
    vote = store.votes_for("merli", "t1", "u1")["turn-1"]
    assert (vote.value, vote.comment) == (-1, "wrong")
    store.record_vote("merli", "t1", "turn-1", user="u1", value=0, comment="ignored")
    assert store.votes_for("merli", "t1", "u1") == {}
    # u2's vote is untouched, and the file only ever grew.
    assert store.votes_for("merli", "t1", "u2")["turn-1"].comment == "no"
    rows = store.feedback_path("merli", "t1").read_text().splitlines()
    assert len(rows) == 4
    assert json.loads(rows[-1])["comment"] is None


def test_bad_votes_are_refused(lionclaw_home):
    with pytest.raises(store.FeedbackError):
        store.record_vote("merli", "t1", "turn-1", user="u1", value=2)
    for bad in ("../x", "a/b", ".hidden", ""):
        with pytest.raises(store.FeedbackError):
            store.record_vote("merli", bad, "turn-1", user="u1", value=1)
    store.record_vote("merli", "t1", "turn-1", user="u1", value=-1, comment="x" * 5000)
    assert len(store.votes_for("merli", "t1", "u1")["turn-1"].comment) == store.COMMENT_MAX_CHARS


def test_summary_counts_standing_votes_in_the_window(lionclaw_home):
    store.record_vote("merli", "t1", "turn-1", user="u1", value=1)
    store.record_vote("merli", "t1", "turn-2", user="u1", value=-1)
    store.record_vote("merli", "t2", "turn-1", user="u1", value=1)
    store.record_vote("merli", "t2", "turn-1", user="u1", value=0)  # taken back
    store.record_vote("solo", "t9", "turn-1", user="u1", value=1)
    # An old vote, outside a 1-day window.
    old = store.feedback_path("solo", "t8")
    old.write_text(
        json.dumps(
            {
                "ts": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
                "turn_id": "turn-1",
                "user": "u1",
                "value": -1,
            }
        )
        + "\n"
    )
    since = datetime.now(timezone.utc) - timedelta(days=1)
    out = store.summarize(since)
    assert (out["up"], out["down"]) == (2, 1)
    assert out["by_agent"] == [
        {"agent": "merli", "up": 1, "down": 1},
        {"agent": "solo", "up": 1, "down": 0},
    ]
    assert store.summarize(since, {"solo"})["by_agent"] == [{"agent": "solo", "up": 1, "down": 0}]
    week = store.summarize(datetime.now(timezone.utc) - timedelta(days=7), {"solo"})
    assert week["down"] == 1


def test_turn_trace_id_reads_the_transcript(lionclaw_home):
    _seed("merli", "t1")
    assert store.turn_trace_id("merli", "t1", "turn-1") == TRACE
    assert store.turn_trace_id("merli", "t1", "nope") is None
    _seed("merli", "t2", traced=False)
    assert store.turn_trace_id("merli", "t2", "turn-1") == ""


# ── the API ───────────────────────────────────────────────────────────────────


@pytest.fixture
def app(lionclaw_home):
    from olano.runtime.api import create_api

    save_agent_config(AgentConfig(name="merli", model="anthropic:claude-haiku-4-5-20251001"))
    save_agent_config(AgentConfig(name="solo", model="anthropic:claude-haiku-4-5-20251001"))
    runtime = MagicMock()
    r = MagicMock()
    r.config = AgentConfig(name="merli", model="anthropic:claude-haiku-4-5-20251001")
    r.ainvoke = AsyncMock(return_value="ok")
    runtime._runners = {"merli": r}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    return create_api(runtime)


@pytest.fixture
def admin(app):
    # Entered, so every request shares one event loop: the vote route hands
    # the Langfuse send to a background task, and a client without `with`
    # runs each request on a loop that is gone by the time that task runs.
    with TestClient(app) as c:
        r = c.post(
            "/api/auth/bootstrap",
            json={"email": "admin@x.com", "password": "pw", "name": "Admin"},
        )
        assert r.status_code == 200, r.text
        yield c


def _member(app, admin, *, grant: str | None = None) -> TestClient:
    uid = admin.post(
        "/api/admin/users", json={"email": "bob@x.com", "password": "pw", "name": "Bob"}
    ).json()["user"]["id"]
    if grant:
        from olano.users.store import get_user_store

        get_user_store().db.grant_access(uid, grant, role="member")
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"email": "bob@x.com", "password": "pw"}).status_code
    return c


@pytest.fixture
def sends(monkeypatch):
    from olano.observability import feedback as obs_feedback

    calls: list[dict] = []
    monkeypatch.setattr(
        obs_feedback,
        "send_feedback",
        lambda config, trace_id, **kw: calls.append({"agent": config.name, "trace": trace_id, **kw})
        or True,
    )
    return calls


def _sent(sends: list[dict], n: int) -> list[dict]:
    """The score send runs in a background task after the response; wait for
    it rather than racing it (a loaded CI worker loses that race)."""
    for _ in range(500):
        if len(sends) >= n:
            break
        time.sleep(0.02)
    return sends


def _vote(client, value, *, thread="t1", turn="turn-1", agent="merli", comment=""):
    return client.post(
        "/api/chat/feedback",
        json={
            "agent": agent,
            "thread_id": thread,
            "turn_id": turn,
            "value": value,
            "comment": comment,
        },
    )


def test_vote_reads_back_on_the_thread(admin, sends):
    _seed("merli", "t1")
    r = _vote(admin, -1, comment="missed the point")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "turn_id": "turn-1", "value": -1, "comment": "missed the point"}
    messages = admin.get("/api/agents/merli/threads/t1").json()["messages"]
    replies = [m for m in messages if m["role"] == "assistant"]
    assert replies[0]["turn_id"] == "turn-1"
    assert replies[0]["feedback"] == {"value": -1, "comment": "missed the point"}
    assert "feedback" not in replies[1] and "turn_id" not in replies[1]
    # Sent to Langfuse with the transcript's trace id, off the request path.
    assert _sent(sends, 1) == [
        {
            "agent": "merli",
            "trace": TRACE,
            "user": sends[0]["user"],
            "value": -1,
            "comment": "missed the point",
        }
    ]
    assert _vote(admin, 0).json()["value"] == 0
    messages = admin.get("/api/agents/merli/threads/t1").json()["messages"]
    assert all("feedback" not in m for m in messages)
    assert _sent(sends, 2)[-1]["value"] == 0


def test_an_untraced_reply_is_kept_and_never_sent(admin, sends):
    _seed("merli", "t1", traced=False)
    assert _vote(admin, 1).status_code == 200
    assert sends == []
    row = json.loads(store.feedback_path("merli", "t1").read_text().splitlines()[0])
    assert row["trace_id"] is None and row["value"] == 1 and row["user"]


def test_unknown_reply_and_bad_values(admin, sends):
    _seed("merli", "t1")
    assert _vote(admin, 1, turn="not-a-turn").status_code == 404
    assert _vote(admin, 1, thread="missing").status_code == 404
    assert _vote(admin, 1, thread="../../etc").status_code == 404
    assert _vote(admin, 5).status_code == 422
    assert sends == []


def test_members_vote_only_where_they_may_read(app, admin, sends):
    _seed("merli", "t1")
    _seed("solo", "t1")
    bob = _member(app, admin, grant="merli")
    assert _vote(bob, 1).status_code == 200
    assert _vote(bob, 1, agent="solo").status_code == 403
    # Each person reads back their own vote only.
    _vote(admin, -1)
    bob_view = bob.get("/api/agents/merli/threads/t1").json()["messages"]
    admin_view = admin.get("/api/agents/merli/threads/t1").json()["messages"]
    assert [m.get("feedback") for m in bob_view if m.get("feedback")] == [
        {"value": 1, "comment": ""}
    ]
    assert [m.get("feedback") for m in admin_view if m.get("feedback")] == [
        {"value": -1, "comment": ""}
    ]


def test_a_vote_file_is_never_read_as_a_thread(admin, sends):
    """Every transcript reader globs `*.jsonl` in the sessions directory."""
    _seed("merli", "t1")
    _vote(admin, 1)
    assert not store.feedback_path("merli", "t1").name.endswith(".jsonl")
    threads = admin.get("/api/agents/merli/threads").json()
    assert [t["id"] for t in threads] == ["t1"]


def test_deleting_the_thread_deletes_its_votes(admin, sends, monkeypatch):
    from olano.observability import deletion

    monkeypatch.setattr(deletion, "delete_traces", lambda agent, ids: len(ids))
    _seed("merli", "t1")
    _vote(admin, 1)
    assert store.feedback_path("merli", "t1").exists()
    assert admin.delete("/api/agents/merli/threads/t1").status_code == 200
    assert not store.feedback_path("merli", "t1").exists()


def test_insights_counts(app, admin, sends):
    _seed("merli", "t1")
    _seed("solo", "t1")
    _vote(admin, 1)
    _vote(admin, -1, agent="solo")
    out = admin.get("/api/analytics/feedback", params={"days": 7}).json()
    assert (out["up"], out["down"]) == (1, 1)
    assert admin.get("/api/analytics/feedback", params={"hours": 24}).json()["up"] == 1
    bob = _member(app, admin, grant="merli")
    mine = bob.get("/api/analytics/feedback").json()
    assert mine["by_agent"] == [{"agent": "merli", "up": 1, "down": 0}]


# ── what Langfuse is told ─────────────────────────────────────────────────────


def _enable(capture: str) -> None:
    path = get_olano_paths().config_path
    raw = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    raw["observability"] = {"langfuse": {"enabled": True, "capture": capture}}
    path.write_text(yaml.safe_dump(raw))


@pytest.fixture
def scored(lionclaw_home, monkeypatch):
    from olano.observability import credentials, tracing

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-fb-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-fb-test-secret")
    credentials._reset_for_tests()
    calls: list[tuple] = []
    monkeypatch.setattr(
        tracing, "score", lambda trace_id, policy, **kw: calls.append(("score", trace_id, kw)) or 1
    )
    monkeypatch.setattr(
        tracing, "clear_score", lambda policy, score_id: calls.append(("clear", score_id)) or 1
    )
    monkeypatch.setattr(tracing, "_vault_values", lambda agent: [])
    return calls


def test_tracing_off_sends_nothing(scored):
    from olano.observability.feedback import send_feedback

    config = AgentConfig(name="merli", model="anthropic:claude-haiku-4-5-20251001")
    assert send_feedback(config, TRACE, user="u1", value=1) is False
    assert scored == []


def test_metadata_capture_withholds_the_comment(scored):
    from olano.observability.feedback import feedback_score_id, send_feedback

    _enable("metadata")
    config = AgentConfig(name="merli", model="anthropic:claude-haiku-4-5-20251001")
    assert send_feedback(config, TRACE, user="u1", value=-1, comment="private words")
    assert scored == [
        ("score", TRACE, {"value": -1, "comment": "", "score_id": feedback_score_id(TRACE, "u1")})
    ]
    send_feedback(config, TRACE, user="u1", value=0)
    assert scored[-1] == ("clear", feedback_score_id(TRACE, "u1"))
    assert feedback_score_id(TRACE, "u1") != feedback_score_id(TRACE, "u2")


def test_full_capture_masks_the_comment(scored, monkeypatch):
    from olano.observability.feedback import send_feedback

    _enable("full")
    monkeypatch.setenv("BILLING_API_TOKEN", "tok-unrelated-but-secret-123456")
    config = AgentConfig(name="merli", model="anthropic:claude-haiku-4-5-20251001")
    send_feedback(
        config,
        TRACE,
        user="u1",
        value=-1,
        comment="it printed tok-unrelated-but-secret-123456 and sk-ant-abcdefghijklmnopqrstuv",
    )
    comment = scored[0][2]["comment"]
    assert comment.startswith("it printed")
    assert "tok-unrelated-but-secret-123456" not in comment
    assert "sk-ant-abcdefghijklmnopqrstuv" not in comment
