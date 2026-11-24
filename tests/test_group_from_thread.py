"""Promoting a one-to-one conversation into a group.

The request this answers is "I'm talking to Alpha, get Beta in on this". An
empty group is not that answer — it makes the human re-explain, which is the
thing they were trying to avoid — so the conversation carries, and these tests
pin what "carries" is allowed to mean:

- The transcript copies; the model context cannot (different stores, and every
  group member gets its own group-scoped thread either way).
- Attribution survives the copy. A carried row is never re-authored to whoever
  pressed the button.
- It is bounded and it is honest about being bounded: a trimmed history says so
  in the transcript rather than passing as the whole conversation.
- The conversation's own bot leads the roster, so the next message still goes
  where it was already going.
- A thread the caller cannot see is not a thread they can copy.
"""

from __future__ import annotations

import gzip
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from olano.config.home import get_olano_paths, write_home_config_text
from olano.groups.seed import MAX_BODY_CHARS, MAX_CARRY, build_carry_over, read_session_rows
from olano.runtime.api import create_api


def enable_groups(**overrides: object) -> None:
    paths = get_olano_paths()
    paths.root.mkdir(parents=True, exist_ok=True)
    raw: dict = {}
    if paths.config_path.exists():
        raw = yaml.safe_load(paths.config_path.read_text()) or {}
    raw["groups"] = {"enabled": True, **overrides}
    write_home_config_text(paths.config_path, yaml.safe_dump(raw, sort_keys=False))


def _runner(name: str) -> MagicMock:
    r = MagicMock()
    r.config = MagicMock(model="anthropic:claude-haiku-4-5-20251001")
    r.ainvoke = AsyncMock(return_value=f"{name} here")
    return r


@pytest.fixture
def runtime():
    rt = MagicMock()
    rt._runners = {n: _runner(n) for n in ("alpha", "beta", "gamma")}
    rt.registry = MagicMock()
    rt.registry.get.return_value = None
    return rt


@pytest.fixture
def client(runtime):
    enable_groups()
    return TestClient(create_api(runtime))


def _write_session(agent: str, thread_id: str, rows: list[dict], *, gzipped: bool = False) -> None:
    sessions = get_olano_paths().sessions_dir / agent
    sessions.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in rows)
    if gzipped:
        (sessions / f"{thread_id}.jsonl.gz").write_bytes(gzip.compress(text.encode()))
    else:
        (sessions / f"{thread_id}.jsonl").write_text(text, encoding="utf-8")


def _chat(n: int = 3) -> list[dict]:
    rows: list[dict] = []
    for i in range(n):
        rows.append({"ts": f"2026-01-01T00:{i:02d}:00Z", "role": "user", "content": f"ask {i}"})
        rows.append(
            {"ts": f"2026-01-01T00:{i:02d}:30Z", "role": "assistant", "content": f"answer {i}"}
        )
    return rows


def _promote(client, **body) -> dict:
    payload = {"agent": "alpha", "thread_id": "t1", "members": ["beta"], **body}
    resp = client.post("/api/groups/from-thread", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["group"]


def _messages(client, gid: int) -> list[dict]:
    return client.get(f"/api/groups/{gid}/messages").json()["messages"]


# ── the seed builder (no runtime) ────────────────────────────────────────────


class TestCarryOverBuilder:
    def test_attribution_is_preserved_per_role(self):
        carried = build_carry_over(_chat(1), agent="alpha", human="human-bob")
        assert [(c["author"], c["author_kind"]) for c in carried] == [
            ("human-bob", "human"),
            ("alpha", "agent"),
        ]

    def test_only_conversation_roles_carry(self):
        rows = [
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "runtime bookkeeping"},
            {"role": "tool_call", "content": "", "name": "read_file"},
            {"role": "tool_result", "content": "file contents"},
            {"role": "assistant", "content": "hello"},
        ]
        carried = build_carry_over(rows, agent="alpha", human="human-bob")
        assert [c["body"] for c in carried] == ["hi", "hello"]

    def test_empty_bodies_are_dropped(self):
        """An attachment-only turn has nothing to say in a transcript — and the
        store refuses a blank message anyway."""
        rows = [{"role": "user", "content": "   "}, {"role": "assistant", "content": "ok"}]
        carried = build_carry_over(rows, agent="alpha", human="human-bob")
        assert [c["body"] for c in carried] == ["ok"]

    def test_the_tail_is_what_survives_the_bound(self):
        rows = [{"role": "user", "content": f"m{i}"} for i in range(MAX_CARRY + 10)]
        carried = build_carry_over(rows, agent="alpha", human="human-bob")
        assert len(carried) == MAX_CARRY
        assert carried[-1]["body"] == f"m{MAX_CARRY + 9}"

    def test_a_long_message_is_clipped_not_dropped(self):
        rows = [{"role": "user", "content": "x" * (MAX_BODY_CHARS + 500)}]
        body = build_carry_over(rows, agent="alpha", human="human-bob")[0]["body"]
        assert len(body) == MAX_BODY_CHARS + 1  # the ellipsis
        assert body.endswith("…")

    def test_a_zero_limit_carries_nothing(self):
        assert build_carry_over(_chat(2), agent="alpha", human="human-bob", limit=0) == []


class TestSessionReader:
    def test_reads_gzipped_sessions(self, tmp_path):
        text = "".join(json.dumps(r) + "\n" for r in _chat(1))
        path = tmp_path / "t.jsonl.gz"
        path.write_bytes(gzip.compress(text.encode()))
        assert len(read_session_rows(path)) == 2

    def test_a_truncated_line_costs_that_line_only(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text('{"role": "user", "content": "ok"}\n{"role": "user", "cont\n')
        rows = read_session_rows(path)
        assert [r["content"] for r in rows] == ["ok"]

    def test_a_missing_file_is_empty_not_an_error(self, tmp_path):
        assert read_session_rows(tmp_path / "nope.jsonl") == []


# ── the route ────────────────────────────────────────────────────────────────


class TestPromoteRoute:
    def test_the_conversations_own_bot_leads_the_roster(self, client):
        _write_session("alpha", "t1", _chat(1))
        group = _promote(client)
        # members[0] answers anything that names nobody, so the next message
        # goes where it was already going.
        assert group["members"] == ["alpha", "beta"]

    def test_the_transcript_carries_with_attribution(self, client):
        _write_session("alpha", "t1", _chat(2))
        group = _promote(client)
        msgs = _messages(client, group["id"])
        carried = [m for m in msgs if m["author_kind"] != "system"]
        assert [m["body"] for m in carried] == ["ask 0", "answer 0", "ask 1", "answer 1"]
        assert [m["author_kind"] for m in carried] == ["human", "agent", "human", "agent"]
        # The agent's rows stay the agent's — never re-authored to the promoter.
        assert {m["author"] for m in carried if m["author_kind"] == "agent"} == {"alpha"}

    def test_a_seeded_group_says_where_it_came_from(self, client):
        _write_session("alpha", "t1", _chat(1))
        group = _promote(client)
        note = _messages(client, group["id"])[-1]
        assert note["author_kind"] == "system"
        assert "one-to-one conversation with alpha" in note["body"]

    def test_a_trimmed_history_admits_it_was_trimmed(self, client):
        _write_session("alpha", "t1", _chat(MAX_CARRY))  # 2x MAX_CARRY rows
        group = _promote(client)
        msgs = _messages(client, group["id"])
        assert len([m for m in msgs if m["author_kind"] != "system"]) == MAX_CARRY
        assert f"of its {MAX_CARRY * 2} messages" in msgs[-1]["body"]

    def test_carry_over_can_be_declined(self, client):
        _write_session("alpha", "t1", _chat(2))
        group = _promote(client, carry_over=False)
        assert _messages(client, group["id"]) == []

    def test_a_gzipped_session_still_carries(self, client):
        _write_session("alpha", "t1", _chat(1), gzipped=True)
        group = _promote(client)
        assert [m["body"] for m in _messages(client, group["id"])][:2] == ["ask 0", "answer 0"]

    def test_a_missing_session_still_produces_a_usable_group(self, client):
        """Seeding is best-effort: the group is what was asked for, the history
        is a bonus, and losing the bonus must not lose the group."""
        group = _promote(client, thread_id="never-existed")
        assert group["members"] == ["alpha", "beta"]
        assert _messages(client, group["id"]) == []

    def test_a_copy_that_dies_halfway_still_says_it_is_partial(self, client, monkeypatch):
        """The one failure this path cannot afford is a history that passes
        itself off as complete. The note is counted from what actually landed,
        so a store that starts refusing mid-copy still produces an honest
        transcript rather than a silently truncated one."""
        _write_session("alpha", "t1", _chat(3))  # 6 carryable rows
        from olano.groups.store import GroupStore

        real = GroupStore.add_message
        calls = {"n": 0}

        def flaky(self, group_id, **kw):
            # Let the note through; fail the 4th carried row onwards.
            if kw.get("author_kind") != "system":
                calls["n"] += 1
                if calls["n"] > 3:
                    raise RuntimeError("disk gave up")
            return real(self, group_id, **kw)

        # No monkeypatch.undo() here: conftest's `lionclaw_home` takes the same
        # monkeypatch instance, so undoing mid-test also reverts LIONCLAW_HOME
        # and points the store at a different home — the group vanishes and the
        # next read 404s. Teardown restores everything anyway, and the reads
        # below never call add_message.
        monkeypatch.setattr(GroupStore, "add_message", flaky)
        group = _promote(client)

        msgs = _messages(client, group["id"])
        carried = [m for m in msgs if m["author_kind"] != "system"]
        assert len(carried) == 3
        note = msgs[-1]
        assert note["author_kind"] == "system"
        # Names what landed and what did not — never "the 3 messages above".
        assert "most recent 3 of its 6 messages" in note["body"]

    def test_the_name_defaults_to_the_roster(self, client):
        _write_session("alpha", "t1", _chat(1))
        assert _promote(client)["name"] == "alpha & beta"

    def test_an_explicit_name_wins(self, client):
        _write_session("alpha", "t1", _chat(1))
        assert _promote(client, name="War room")["name"] == "War room"

    def test_the_leading_bot_cannot_be_added_twice(self, client):
        _write_session("alpha", "t1", _chat(1))
        group = _promote(client, members=["alpha", "beta"])
        assert group["members"] == ["alpha", "beta"]

    def test_more_than_one_bot_can_join_at_once(self, client):
        _write_session("alpha", "t1", _chat(1))
        group = _promote(client, members=["beta", "gamma"])
        assert group["members"] == ["alpha", "beta", "gamma"]

    def test_an_agent_is_required(self, client):
        assert client.post("/api/groups/from-thread", json={"members": ["beta"]}).status_code == 400

    def test_a_roster_over_the_max_is_refused(self, client):
        enable_groups(max_members=2)
        client_ = TestClient(create_api(MagicMock(_runners={}, registry=MagicMock())))
        resp = client_.post(
            "/api/groups/from-thread",
            json={"agent": "alpha", "members": ["beta", "gamma"]},
        )
        assert resp.status_code == 400
        # The store's own sentence reaches the human — it names both the bound
        # and what they asked for, which a generic 400 would not.
        assert resp.json()["detail"] == "A group holds 2–2 agents (got 3)."

    def test_404s_while_the_feature_is_off(self, runtime):
        enable_groups(enabled=False)
        off = TestClient(create_api(runtime))
        resp = off.post("/api/groups/from-thread", json={"agent": "alpha", "members": ["beta"]})
        assert resp.status_code == 404
