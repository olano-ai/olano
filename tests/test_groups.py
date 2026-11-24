"""Multi-bot group threads: store semantics, turn-taking, and the off switch.

Both switches ship **on**, and their independence is the first thing these
tests pin: ``enabled`` only decides whether a roster of bots can share a
conversation, while ``autonomous`` is what lets one reply spend another model
call with nobody watching — and turning either off must never move the other.
An operator who turns the feature off must still get the original guarantee —
every route 404s and nothing on ``/api/status`` admits they exist.

The rest is the two rules that make a group honest: exactly one member answers
a message, and every row in the transcript says who wrote it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from olano.config.home import get_olano_paths, write_home_config_text
from olano.config.system_settings import (
    GroupThreadSettings,
    group_threads_enabled,
    load_group_thread_settings,
)
from olano.groups.store import GroupError, GroupStore, get_group_store
from olano.runtime.api import create_api
from olano.runtime.groups_routes import compose_turn, pick_addressee


def _write_groups_block(block: dict | None) -> None:
    paths = get_olano_paths()
    paths.root.mkdir(parents=True, exist_ok=True)
    raw: dict = {}
    if paths.config_path.exists():
        raw = yaml.safe_load(paths.config_path.read_text()) or {}
    if block is None:
        raw.pop("groups", None)
    else:
        raw["groups"] = block
    write_home_config_text(paths.config_path, yaml.safe_dump(raw, sort_keys=False))


def enable_groups(**overrides: object) -> None:
    _write_groups_block({"enabled": True, **overrides})


def disable_groups() -> None:
    """Turn the feature off explicitly — the default no longer does it."""
    _write_groups_block({"enabled": False})


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
    return TestClient(create_api(runtime))


def _make_group(client, name="Standup", members=("alpha", "beta")) -> dict:
    resp = client.post("/api/groups", json={"name": name, "members": list(members)})
    assert resp.status_code == 200, resp.text
    return resp.json()["group"]


# ── settings ─────────────────────────────────────────────────────────────────


class TestSettings:
    def test_absent_block_is_on_and_autonomous(self):
        # An install that never writes the block gets the group surface, so the
        # UI that was already built is reachable — and the bounded cascade with
        # it, because a group whose members cannot answer each other is a
        # switchboard. The hop and wall-clock budgets keep that default honest.
        settings = load_group_thread_settings()
        assert settings.enabled is True
        assert settings.autonomous is True
        assert (settings.min_members, settings.max_members) == (2, 6)
        assert group_threads_enabled() is True

    def test_explicit_false_still_turns_it_off(self):
        disable_groups()
        assert load_group_thread_settings().enabled is False
        assert group_threads_enabled() is False

    def test_block_is_read_from_config_yaml(self):
        enable_groups(max_members=3)
        settings = load_group_thread_settings()
        assert settings.enabled is True
        assert settings.max_members == 3

    def test_bounds_are_clamped_to_the_designed_range(self):
        assert GroupThreadSettings(min_members=1).min_members == 2
        assert GroupThreadSettings(max_members=99).max_members == 6
        # An inverted pair resolves rather than producing a group nobody can build.
        inverted = GroupThreadSettings(min_members=4, max_members=2)
        assert inverted.max_members == 4

    def test_malformed_block_reads_as_the_defaults(self):
        # Every loader in system_settings falls back to its defaults rather
        # than raising, and this one is no exception — and both defaults are
        # now "on", so a junk value for EITHER key fails OPEN. That is a known
        # consequence of the default, pinned here so nobody rediscovers it: a
        # typo'd `autonomous:` means cascading, bounded by the two budgets.
        _write_groups_block({"enabled": "yes please", "autonomous": "of course"})
        settings = load_group_thread_settings()
        assert settings.enabled is True
        assert settings.autonomous is True


# ── store ────────────────────────────────────────────────────────────────────


class TestGroupStore:
    def test_create_and_read_back(self):
        store = get_group_store()
        group = store.create(name="Ops", members=["alpha", "beta"], created_by="u1")
        assert group["members"] == ["alpha", "beta"]
        assert group["created_by"] == "u1"
        assert group["message_count"] == 0
        assert store.get(group["id"]) == group

    def test_roster_order_is_preserved_and_deduplicated(self):
        group = get_group_store().create(
            name="Ops", members=["beta", "alpha", "beta", " alpha "], created_by="u1"
        )
        assert group["members"] == ["beta", "alpha"]

    def test_roster_bounds(self):
        store = get_group_store()
        with pytest.raises(GroupError):
            store.create(name="Solo", members=["alpha"], created_by="u1")
        with pytest.raises(GroupError):
            store.create(
                name="Crowd",
                members=[f"a{i}" for i in range(7)],
                created_by="u1",
            )

    def test_unnamed_group_rejected(self):
        with pytest.raises(GroupError):
            get_group_store().create(name="  ", members=["a", "b"], created_by="u1")

    def test_add_and_remove_member(self):
        store = get_group_store()
        group = store.create(name="Ops", members=["alpha", "beta"], created_by="u1")
        gid = group["id"]
        assert store.add_member(gid, "gamma")["members"] == ["alpha", "beta", "gamma"]
        with pytest.raises(GroupError):  # already a member
            store.add_member(gid, "gamma")
        assert store.remove_member(gid, "gamma")["members"] == ["alpha", "beta"]
        with pytest.raises(GroupError):  # would fall below the minimum
            store.remove_member(gid, "beta")

    def test_max_members_is_enforced_on_add(self):
        store = get_group_store()
        gid = store.create(name="Ops", members=["alpha", "beta"], created_by="u1")["id"]
        with pytest.raises(GroupError):
            store.add_member(gid, "gamma", max_members=2)

    def test_messages_are_attributed(self):
        store = get_group_store()
        gid = store.create(name="Ops", members=["alpha", "beta"], created_by="u1")["id"]
        store.add_message(
            gid, author="human-bob", author_kind="human", body="hi", addressee="alpha"
        )
        store.add_message(gid, author="alpha", author_kind="agent", body="hello")
        # A bot-to-bot turn records who it answers, separately from who it
        # hands the thread to — the two are different questions.
        store.add_message(
            gid, author="beta", author_kind="agent", body="hi alpha", reply_to="alpha"
        )
        rows = store.messages(gid)
        assert [(r["author"], r["author_kind"]) for r in rows] == [
            ("human-bob", "human"),
            ("alpha", "agent"),
            ("beta", "agent"),
        ]
        assert rows[0]["addressee"] == "alpha"
        assert [r["reply_to"] for r in rows] == ["", "", "alpha"]
        assert store.get(gid)["message_count"] == 3

    def test_segments_are_stored_only_when_the_turn_interleaved(self):
        """The reply's ordered spans, so a reload can put the tool cards between
        the paragraphs they belong to.

        A turn that never ran a tool has one span, which IS the pre-segment
        layout — the renderer handles its absence — so nothing is written and
        the row stays byte-identical to what earlier releases produced. Storing
        it anyway would put a JSON blob on every ordinary message for a layout
        that draws the same either way."""
        store = get_group_store()
        gid = store.create(name="Ops", members=["alpha", "beta"], created_by="u1")["id"]
        spans = [
            {"text": "Looking it up.", "after": None},
            {"text": "\n\nFound it.", "after": "call-1"},
        ]
        row = store.add_message(
            gid,
            author="alpha",
            author_kind="agent",
            body="Looking it up.\n\nFound it.",
            segments=spans,
        )
        assert row["segments"] == spans
        store.add_message(gid, author="beta", author_kind="agent", body="no tools here")
        store.add_message(
            gid,
            author="beta",
            author_kind="agent",
            body="one span",
            segments=[{"text": "one span", "after": None}],
        )
        rows = store.messages(gid)
        assert rows[0]["segments"] == spans
        # The invariant every renderer depends on: the spans join back to the
        # body. `buildTurnBlocks` silently falls back to the stacked layout when
        # they do not, so a store that normalized one side and not the other
        # would turn interleaving off product-wide with nothing failing.
        assert "".join(s["text"] for s in rows[0]["segments"]) == rows[0]["body"]
        assert "segments" not in rows[1]
        assert "segments" not in rows[2]

    def test_a_row_with_unparseable_segments_still_renders(self, tmp_path):
        """Decoding runs inside a request. A transcript that 500s because one
        row's JSON is malformed is a worse failure than one that renders the
        old way."""
        import sqlite3

        db = tmp_path / "groups.db"
        store = GroupStore(db)
        gid = store.create(name="Ops", members=["alpha", "beta"], created_by="u1")["id"]
        store.add_message(gid, author="alpha", author_kind="agent", body="hello")
        conn = sqlite3.connect(db)
        conn.execute("UPDATE group_messages SET segments = ?", ("{not json",))
        conn.commit()
        conn.close()

        rows = GroupStore(db).messages(gid)
        assert rows[0]["body"] == "hello"
        assert "segments" not in rows[0]

    def test_an_older_database_gains_the_reply_to_and_segments_columns(self, tmp_path):
        """``CREATE TABLE IF NOT EXISTS`` leaves an existing table alone, so a
        groups.db written before ``reply_to`` and ``segments`` existed has to be
        migrated on open — and its old rows must read back with the columns'
        defaults."""
        import sqlite3

        db = tmp_path / "groups.db"
        legacy = sqlite3.connect(db)
        legacy.executescript(
            """
            CREATE TABLE groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE group_members (
                group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                agent_name TEXT NOT NULL, position INTEGER NOT NULL, added_at TEXT NOT NULL,
                PRIMARY KEY (group_id, agent_name)
            );
            CREATE TABLE group_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                ts TEXT NOT NULL, author TEXT NOT NULL, author_kind TEXT NOT NULL,
                addressee TEXT NOT NULL DEFAULT '', body TEXT NOT NULL
            );
            INSERT INTO groups (id, name, created_by, created_at, updated_at)
                VALUES (1, 'Old', 'u1', 't', 't');
            INSERT INTO group_members VALUES (1, 'alpha', 0, 't'), (1, 'beta', 1, 't');
            INSERT INTO group_messages (group_id, ts, author, author_kind, addressee, body)
                VALUES (1, 't', 'alpha', 'agent', 'beta', 'from before');
            """
        )
        legacy.commit()
        legacy.close()

        store = GroupStore(db)
        store.add_message(
            1,
            author="beta",
            author_kind="agent",
            body="a\n\nb",
            reply_to="alpha",
            segments=[{"text": "a", "after": None}, {"text": "\n\nb", "after": "c1"}],
        )
        rows = store.messages(1)
        assert [(r["body"], r["reply_to"]) for r in rows] == [
            ("from before", ""),
            ("a\n\nb", "alpha"),
        ]
        # A row written before the column existed reads back as "no segments",
        # not as an empty list a renderer would take for an interleaved turn.
        assert "segments" not in rows[0]
        assert rows[1]["segments"][1]["after"] == "c1"
        # Opening it again is a no-op, not a second ALTER.
        assert GroupStore(db).messages(1)[-1]["reply_to"] == "alpha"

    def test_message_validation(self):
        store = get_group_store()
        gid = store.create(name="Ops", members=["alpha", "beta"], created_by="u1")["id"]
        with pytest.raises(GroupError):
            store.add_message(gid, author="x", author_kind="agent", body="  ")
        with pytest.raises(GroupError):
            store.add_message(gid, author="x", author_kind="robot", body="hi")
        with pytest.raises(GroupError):
            store.add_message(999, author="x", author_kind="agent", body="hi")

    def test_unseen_starts_after_the_agents_own_last_message(self):
        """What a member already holds is its own checkpoint up to its last
        turn; only what came after that has to travel into the next prompt."""
        store = get_group_store()
        gid = store.create(name="Ops", members=["alpha", "beta"], created_by="u1")["id"]
        store.add_message(gid, author="human-bob", author_kind="human", body="one")
        store.add_message(gid, author="alpha", author_kind="agent", body="alpha reply")
        store.add_message(gid, author="human-bob", author_kind="human", body="two")
        store.add_message(gid, author="beta", author_kind="agent", body="beta reply")

        assert [m["body"] for m in store.unseen_for(gid, "alpha")] == [
            "two",
            "beta reply",
        ]
        # beta has never spoken before its own last message, so it catches up
        # on the whole thread.
        assert [m["body"] for m in store.unseen_for(gid, "gamma")] == [
            "one",
            "alpha reply",
            "two",
            "beta reply",
        ]

    def test_delete_cascades(self):
        store = get_group_store()
        gid = store.create(name="Ops", members=["alpha", "beta"], created_by="u1")["id"]
        store.add_message(gid, author="alpha", author_kind="agent", body="hi")
        assert store.delete(gid) is True
        assert store.get(gid) is None
        assert store.messages(gid) == []
        assert store.delete(gid) is False

    def test_list_filters_by_creator(self):
        store = get_group_store()
        store.create(name="Mine", members=["alpha", "beta"], created_by="u1")
        store.create(name="Theirs", members=["alpha", "beta"], created_by="u2")
        assert [g["name"] for g in store.list_groups(created_by="u1")] == ["Mine"]
        assert len(store.list_groups()) == 2


# ── turn-taking helpers ──────────────────────────────────────────────────────


class TestTurnTaking:
    def test_first_member_answers_by_default(self):
        assert pick_addressee("anyone home?", ["alpha", "beta"]) == "alpha"

    def test_mention_wins(self):
        assert pick_addressee("hey @beta what do you think?", ["alpha", "beta"]) == "beta"

    def test_mention_is_case_insensitive(self):
        assert pick_addressee("@Beta ping", ["alpha", "beta"]) == "beta"

    def test_a_mention_that_ends_a_sentence_still_counts(self):
        # "@beta." used to be read as the handle "beta." — no member, so the
        # message fell back to the first member and beta never heard it.
        assert pick_addressee("over to you, @beta.", ["alpha", "beta"]) == "beta"
        assert pick_addressee("thanks @beta-", ["alpha", "beta"]) == "beta"
        # Dots and dashes INSIDE a handle are still part of it.
        assert pick_addressee("@dev.ops.bot hi", ["alpha", "dev.ops.bot"]) == "dev.ops.bot"

    def test_mention_of_a_non_member_is_ignored(self):
        assert pick_addressee("@nobody hi", ["alpha", "beta"]) == "alpha"

    def test_explicit_to_wins_over_a_mention(self):
        assert pick_addressee("@beta hi", ["alpha", "beta"], explicit="alpha") == "alpha"

    def test_compose_turn_names_the_others_and_carries_only_unseen(self):
        group = {"name": "Ops", "members": ["alpha", "beta"]}
        prompt = compose_turn(
            group,
            "alpha",
            [{"author": "human-bob", "body": "ship it"}],
        )
        assert '"alpha"' in prompt
        assert "beta" in prompt
        assert "human-bob: ship it" in prompt


# ── rename ───────────────────────────────────────────────────────────────────


class TestRename:
    """A group's name is a LABEL. Its identity is the numeric id every member's
    thread derives from, and its visibility is decided on ``created_by`` — so a
    rename must be able to touch neither, which is what makes it safe to hang
    off a right-click."""

    def test_rename_changes_the_name_and_nothing_else(self, client):
        enable_groups()
        group = _make_group(client, name="Standup", members=("alpha", "beta"))
        client.post(f"/api/groups/{group['id']}/messages", json={"text": "hello"})

        resp = client.patch(f"/api/groups/{group['id']}", json={"name": "  Launch war room  "})
        assert resp.status_code == 200, resp.text
        renamed = resp.json()["group"]
        assert renamed["name"] == "Launch war room"  # trimmed by the store
        assert renamed["id"] == group["id"]
        assert renamed["members"] == group["members"]
        assert renamed["created_by"] == group["created_by"]
        # The transcript is untouched — including who each row was addressed to.
        rows = get_group_store().messages(group["id"])
        assert [r["author_kind"] for r in rows] == ["human", "agent"]

    def test_a_rename_is_not_activity(self, client):
        """`updated_at` orders the rail. A rename must not float a group above
        threads that actually said something."""
        enable_groups()
        group = _make_group(client)
        before = get_group_store().require(group["id"])["updated_at"]
        client.patch(f"/api/groups/{group['id']}", json={"name": "Renamed"})
        assert get_group_store().require(group["id"])["updated_at"] == before

    def test_an_empty_name_is_refused(self, client):
        enable_groups()
        group = _make_group(client)
        resp = client.patch(f"/api/groups/{group['id']}", json={"name": "   "})
        assert resp.status_code == 400
        assert get_group_store().require(group["id"])["name"] == group["name"]

    def test_an_unknown_group_is_a_404(self, client):
        enable_groups()
        assert client.patch("/api/groups/9999", json={"name": "x"}).status_code == 404

    def test_the_store_refuses_a_rename_of_a_group_that_is_gone(self, tmp_path):
        enable_groups()
        store = get_group_store()
        group = store.create(name="Gone", members=["alpha", "beta"], created_by="owner")
        store.delete(group["id"])
        with pytest.raises(GroupError):
            store.rename(group["id"], "New name")


# ── routes: the off switch ───────────────────────────────────────────────────


class TestTurnedOff:
    """The off switch still has to be total — it is just no longer the default."""

    def test_every_path_404s(self, client):
        disable_groups()
        assert client.get("/api/groups").status_code == 404
        assert (
            client.post("/api/groups", json={"name": "x", "members": ["a", "b"]}).status_code == 404
        )
        assert client.get("/api/groups/1").status_code == 404
        assert client.patch("/api/groups/1", json={"name": "x"}).status_code == 404
        assert client.delete("/api/groups/1").status_code == 404
        assert client.post("/api/groups/1/members", json={"agent": "a"}).status_code == 404
        assert client.delete("/api/groups/1/members/a").status_code == 404
        assert client.get("/api/groups/1/messages").status_code == 404
        assert client.post("/api/groups/1/messages", json={"text": "hi"}).status_code == 404
        assert client.get("/api/groups/1/cascade").status_code == 404
        assert client.post("/api/groups/1/stop").status_code == 404

    def test_status_capability_is_off_when_disabled(self, runtime):
        disable_groups()
        client = TestClient(create_api(runtime))
        assert client.get("/api/status").json()["capabilities"]["groups_enabled"] is False

    def test_status_capability_is_on_by_default(self, client):
        assert client.get("/api/status").json()["capabilities"]["groups_enabled"] is True

    def test_status_capability_follows_the_switch(self, runtime):
        enable_groups()
        client = TestClient(create_api(runtime))
        assert client.get("/api/status").json()["capabilities"]["groups_enabled"] is True


# ── routes: enabled ──────────────────────────────────────────────────────────


class TestGroupRoutes:
    def test_create_list_get_delete(self, client):
        enable_groups()
        group = _make_group(client)
        gid = group["id"]
        assert group["members"] == ["alpha", "beta"]

        listed = client.get("/api/groups").json()["groups"]
        assert [g["id"] for g in listed] == [gid]
        assert client.get(f"/api/groups/{gid}").json()["group"]["name"] == "Standup"

        assert client.delete(f"/api/groups/{gid}").status_code == 200
        assert client.get(f"/api/groups/{gid}").status_code == 404

    def test_roster_bounds_are_a_400(self, client):
        enable_groups()
        resp = client.post("/api/groups", json={"name": "Solo", "members": ["alpha"]})
        assert resp.status_code == 400
        assert "2" in resp.json()["detail"]

    def test_configured_max_members_binds_the_route(self, client):
        enable_groups(max_members=2)
        assert (
            client.post(
                "/api/groups", json={"name": "Trio", "members": ["alpha", "beta", "gamma"]}
            ).status_code
            == 400
        )

    def test_add_and_remove_members(self, client):
        enable_groups()
        gid = _make_group(client)["id"]
        added = client.post(f"/api/groups/{gid}/members", json={"agent": "gamma"})
        assert added.json()["group"]["members"] == ["alpha", "beta", "gamma"]
        removed = client.delete(f"/api/groups/{gid}/members/gamma")
        assert removed.json()["group"]["members"] == ["alpha", "beta"]
        # Dropping below the minimum is refused rather than emptying the group.
        assert client.delete(f"/api/groups/{gid}/members/beta").status_code == 400

    def test_posting_runs_exactly_one_member(self, client, runtime):
        enable_groups()
        gid = _make_group(client)["id"]

        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "morning all"})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["addressee"] == "alpha"
        assert [m["author_kind"] for m in payload["messages"]] == ["human", "agent"]
        assert payload["messages"][1]["body"] == "alpha here"

        # One turn, one runner — the silent members must not have been invoked.
        assert runtime._runners["alpha"].ainvoke.await_count == 1
        assert runtime._runners["beta"].ainvoke.await_count == 0

    def test_a_reply_that_addresses_a_member_still_stops_there(self, client, runtime):
        """``groups.autonomous`` is a second switch, turned off here: a bot
        may name another member, and that member still does not get a turn.
        The hand-off is recorded on the message either way — attribution
        describes what was said; the switch governs turn-taking."""
        enable_groups(autonomous=False)
        gid = _make_group(client)["id"]
        runtime._runners["alpha"].ainvoke = AsyncMock(return_value="sure, @beta take it")

        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "morning all"})
        assert resp.json()["cascade"] == {"running": False, "reason": "off"}
        assert runtime._runners["beta"].ainvoke.await_count == 0
        assert resp.json()["messages"][1]["addressee"] == "beta"

    def test_mention_routes_the_turn(self, client, runtime):
        enable_groups()
        gid = _make_group(client)["id"]
        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "@beta your call"})
        assert resp.json()["addressee"] == "beta"
        assert runtime._runners["alpha"].ainvoke.await_count == 0
        assert runtime._runners["beta"].ainvoke.await_count == 1

    def test_explicit_addressee_must_be_a_member(self, client):
        enable_groups()
        gid = _make_group(client)["id"]
        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "hi", "to": "gamma"})
        assert resp.status_code == 400

    def test_transcript_accumulates_in_order(self, client):
        enable_groups()
        gid = _make_group(client)["id"]
        client.post(f"/api/groups/{gid}/messages", json={"text": "one"})
        client.post(f"/api/groups/{gid}/messages", json={"text": "@beta two"})

        messages = client.get(f"/api/groups/{gid}/messages").json()["messages"]
        assert [(m["author"], m["body"]) for m in messages] == [
            ("human-owner", "one"),
            ("alpha", "alpha here"),
            ("human-owner", "@beta two"),
            ("beta", "beta here"),
        ]

    def test_each_member_gets_a_group_scoped_thread(self, client, runtime):
        enable_groups()
        gid = _make_group(client)["id"]
        client.post(f"/api/groups/{gid}/messages", json={"text": "one"})
        client.post(f"/api/groups/{gid}/messages", json={"text": "@beta two"})

        alpha_thread = runtime._runners["alpha"].ainvoke.await_args.kwargs["thread_id"]
        beta_thread = runtime._runners["beta"].ainvoke.await_args.kwargs["thread_id"]
        assert alpha_thread == f"group-{gid}-alpha"
        assert beta_thread == f"group-{gid}-beta"

    def test_a_members_prompt_only_carries_what_it_has_not_seen(self, client, runtime):
        enable_groups()
        gid = _make_group(client)["id"]
        client.post(f"/api/groups/{gid}/messages", json={"text": "first"})
        client.post(f"/api/groups/{gid}/messages", json={"text": "second"})

        prompt = runtime._runners["alpha"].ainvoke.await_args.args[0]
        assert "second" in prompt
        # alpha already holds "first" and its own reply in its checkpoint.
        assert "first" not in prompt

    def test_empty_text_is_refused_before_any_agent_runs(self, client, runtime):
        enable_groups()
        gid = _make_group(client)["id"]
        assert client.post(f"/api/groups/{gid}/messages", json={"text": "  "}).status_code == 400
        assert runtime._runners["alpha"].ainvoke.await_count == 0

    def test_an_offline_member_is_a_404_not_a_lost_message(self, client, runtime):
        enable_groups()
        gid = _make_group(client)["id"]
        runtime._runners.pop("alpha")
        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "hi"})
        assert resp.status_code == 404
        assert client.get(f"/api/groups/{gid}/messages").json()["messages"] == []

    def test_a_failing_turn_keeps_the_human_message(self, client, runtime):
        enable_groups()
        gid = _make_group(client)["id"]
        runtime._runners["alpha"].ainvoke = AsyncMock(side_effect=RuntimeError("model down"))

        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "are you there"})
        assert resp.status_code == 502
        messages = client.get(f"/api/groups/{gid}/messages").json()["messages"]
        # The human's message survives, and the crash leaves a system line
        # naming who was asked — the same rule the cascade's stop notes follow.
        # No agent row: a transcript holds what was said, never what almost was.
        assert [m["author_kind"] for m in messages] == ["human", "system"]
        assert messages[0]["body"] == "are you there"
        assert "@alpha" in messages[1]["body"]


# ── routes: multi-user access ────────────────────────────────────────────────


def _admin(app) -> TestClient:
    c = TestClient(app)
    assert (
        c.post(
            "/api/auth/bootstrap",
            json={"email": "admin@x.com", "password": "pw", "name": "Admin"},
        ).status_code
        == 200
    )
    return c


def _member(app, admin: TestClient, email: str) -> tuple[TestClient, str]:
    uid = admin.post(
        "/api/admin/users", json={"email": email, "password": "pw", "name": email.split("@")[0]}
    ).json()["user"]["id"]
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"email": email, "password": "pw"}).status_code == 200
    return c, uid


class TestAccess:
    @pytest.fixture
    def app(self, runtime):
        return create_api(runtime)

    def test_a_member_cannot_group_agents_they_cannot_reach(self, app):
        admin = _admin(app)
        member, _ = _member(app, admin, "bob@x.com")
        enable_groups()
        resp = member.post("/api/groups", json={"name": "Ops", "members": ["alpha", "beta"]})
        assert resp.status_code == 403
        assert member.get("/api/groups").json()["groups"] == []

    def test_a_granted_member_can(self, app):
        admin = _admin(app)
        member, uid = _member(app, admin, "bob@x.com")
        from olano.users.store import get_user_store

        for agent in ("alpha", "beta"):
            get_user_store().db.grant_access(uid, agent, role="member")
        enable_groups()
        assert (
            member.post(
                "/api/groups", json={"name": "Ops", "members": ["alpha", "beta"]}
            ).status_code
            == 200
        )

    def test_groups_are_not_visible_across_humans(self, app):
        admin = _admin(app)
        alice, alice_id = _member(app, admin, "alice@x.com")
        bob, _ = _member(app, admin, "bob@x.com")
        from olano.users.store import get_user_store

        for agent in ("alpha", "beta"):
            get_user_store().db.grant_access(alice_id, agent, role="member")
        enable_groups()

        gid = alice.post(
            "/api/groups", json={"name": "Alice's", "members": ["alpha", "beta"]}
        ).json()["group"]["id"]

        assert [g["id"] for g in alice.get("/api/groups").json()["groups"]] == [gid]
        assert bob.get("/api/groups").json()["groups"] == []
        # Someone else's group is indistinguishable from one that never existed.
        assert bob.get(f"/api/groups/{gid}").status_code == 404
        assert bob.patch(f"/api/groups/{gid}", json={"name": "Bob's now"}).status_code == 404
        assert bob.delete(f"/api/groups/{gid}").status_code == 404
        assert alice.get(f"/api/groups/{gid}").json()["group"]["name"] == "Alice's"
        # The admin still sees everything.
        assert admin.get(f"/api/groups/{gid}").status_code == 200

    def test_a_revoked_grant_stops_the_next_turn(self, app):
        """The roster is not a standing permission — access is re-checked on
        every turn, so revoking it mid-thread takes effect immediately."""
        admin = _admin(app)
        member, uid = _member(app, admin, "bob@x.com")
        from olano.users.store import get_user_store

        db = get_user_store().db
        for agent in ("alpha", "beta"):
            db.grant_access(uid, agent, role="member")
        enable_groups()
        gid = member.post("/api/groups", json={"name": "Ops", "members": ["alpha", "beta"]}).json()[
            "group"
        ]["id"]
        assert member.post(f"/api/groups/{gid}/messages", json={"text": "hi"}).status_code == 200

        db.revoke_access(uid, "alpha")
        assert member.post(f"/api/groups/{gid}/messages", json={"text": "hi"}).status_code == 403
