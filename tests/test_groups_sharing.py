"""Sharing a group with a teammate, and what it does to the room.

Humans are never roster members: the roster is agents, and its bound is a cost
guard on booted agents. A person reaches a group by creating it, by being an
admin, or by being *shared* into it. Three rules keep sharing from becoming a
side door, and these tests pin each one:

- a person is only shared into a group whose every agent they already hold a
  grant on, and an agent is only added to a shared group when every shared
  person holds one on it;
- every post is still checked against the poster's own grants;
- a shared person reads, posts and leaves, and nothing else.

The one behavioural change a share makes to the room is turn-taking: with more
than one person in it, a message that names no agent is for the other people
and wakes nobody. And the agents are told who is in the room, by handle, on
every turn.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.groups.cascade import group_briefing, people_block
from olano.groups.store import GroupError, get_group_store
from olano.runtime.api import create_api
from olano.runtime.groups_routes import (
    NOBODY_ADDRESSED,
    group_people,
    pick_addressees,
)
from tests.test_groups import _admin, _member, enable_groups


def _runner(name: str) -> MagicMock:
    r = MagicMock()
    r.config = MagicMock(model="fake:instant", description=f"{name} does things")
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
def app(runtime):
    return create_api(runtime)


def _grant(uid: str, *agents: str) -> None:
    from olano.users.store import get_user_store

    for agent in agents:
        get_user_store().db.grant_access(uid, agent, role="member")


def _team(app):
    """An admin, alice (creator-to-be) and bob (guest-to-be), both granted
    alpha and beta; a group of alice's on those two."""
    admin = _admin(app)
    alice, alice_id = _member(app, admin, "alice@x.com")
    bob, bob_id = _member(app, admin, "bob@x.com")
    _grant(alice_id, "alpha", "beta", "gamma")
    _grant(bob_id, "alpha", "beta")
    enable_groups()
    gid = alice.post("/api/groups", json={"name": "Ops", "members": ["alpha", "beta"]}).json()[
        "group"
    ]["id"]
    return admin, alice, alice_id, bob, bob_id, gid


# ── store ────────────────────────────────────────────────────────────────────


class TestStore:
    def test_share_and_unshare_round_trip(self):
        store = get_group_store()
        group = store.create(name="Ops", members=["a", "b"], created_by="u1")
        assert group["shared_with"] == []
        shared = store.share(group["id"], "u2", added_by="u1")
        assert shared["shared_with"] == ["u2"]
        # Idempotent: a second share is the same row, not an error.
        assert store.share(group["id"], "u2")["shared_with"] == ["u2"]
        assert store.unshare(group["id"], "u2")["shared_with"] == []
        with pytest.raises(GroupError):
            store.unshare(group["id"], "u2")

    def test_the_creator_is_never_a_share(self):
        store = get_group_store()
        group = store.create(name="Ops", members=["a", "b"], created_by="u1")
        with pytest.raises(GroupError):
            store.share(group["id"], "u1")

    def test_visible_to_lists_own_and_shared_groups(self):
        store = get_group_store()
        mine = store.create(name="Mine", members=["a", "b"], created_by="u1")
        theirs = store.create(name="Theirs", members=["a", "b"], created_by="u2")
        hidden = store.create(name="Hidden", members=["a", "b"], created_by="u3")
        store.share(theirs["id"], "u1")
        ids = {g["id"] for g in store.list_groups(visible_to="u1")}
        assert ids == {mine["id"], theirs["id"]}
        assert hidden["id"] not in ids
        # The narrower filter still means what it did.
        assert [g["id"] for g in store.list_groups(created_by="u1")] == [mine["id"]]

    def test_humans_are_the_transcripts_speakers_in_first_word_order(self):
        store = get_group_store()
        group = store.create(name="Ops", members=["a", "b"], created_by="u1")
        gid = group["id"]
        assert group["humans"] == []
        store.add_message(gid, author="human-carlos", author_kind="human", body="hi")
        store.add_message(gid, author="a", author_kind="agent", body="hello")
        store.add_message(gid, author="human-maria", author_kind="human", body="hey")
        store.add_message(gid, author="human-carlos", author_kind="human", body="again")
        assert store.get(gid)["humans"] == ["human-carlos", "human-maria"]

    def test_deleting_a_group_takes_its_shares_with_it(self):
        store = get_group_store()
        group = store.create(name="Ops", members=["a", "b"], created_by="u1")
        store.share(group["id"], "u2")
        store.delete(group["id"])
        assert store.list_groups(visible_to="u2") == []


# ── turn-taking ──────────────────────────────────────────────────────────────


class TestMentionOnly:
    def test_single_person_rooms_keep_the_first_member_fallback(self):
        assert pick_addressees("hello", ["a", "b"]) == ["a"]

    def test_a_shared_room_wakes_nobody_without_a_mention(self):
        assert pick_addressees("thanks maria", ["a", "b"], require_mention=True) == []
        assert pick_addressees("@b look", ["a", "b"], require_mention=True) == ["b"]
        # An explicit picker choice still wins, mention or not.
        assert pick_addressees("hello", ["a", "b"], "b", require_mention=True) == ["b"]


# ── briefing ─────────────────────────────────────────────────────────────────


class TestBriefing:
    def test_people_block_names_handle_display_name_and_how_they_got_here(self):
        block = people_block(
            [
                {
                    "username": "carlos",
                    "name": "Carlos M.",
                    "principal": "human-carlos",
                    "role": "creator",
                },
                {
                    "username": "maria",
                    "name": "maria",
                    "principal": "human-maria",
                    "role": "shared",
                },
                {
                    "username": "root",
                    "name": "Root Admin",
                    "principal": "human-root",
                    "role": "spoke",
                },
            ]
        )
        assert "- human-carlos (Carlos M.), created this group" in block
        # A display name equal to the handle is not repeated.
        assert "- human-maria, was let in by the creator" in block
        assert "- human-root (Root Admin), an admin who posted here" in block

    def test_people_block_is_lenient(self):
        assert people_block(None).startswith("- (one person")
        assert "human-x" in people_block([{"username": "x"}])
        assert people_block([{"name": "nobody"}]).startswith("- (one person")

    def test_briefing_states_the_people_rule_only_for_a_shared_room(self):
        group = {"name": "Ops", "members": ["a", "b"]}
        one = group_briefing(
            group, "a", humans=[{"username": "c", "principal": "human-c", "role": "creator"}]
        )
        two = group_briefing(
            group,
            "a",
            humans=[
                {"username": "c", "principal": "human-c", "role": "creator"},
                {"username": "m", "principal": "human-m", "role": "shared"},
            ],
        )
        assert "PEOPLE IN THIS ROOM" in one and "PEOPLE IN THIS ROOM" in two
        assert "talking to that one person" in one
        assert "Several PEOPLE are in this room" not in one
        assert "Several PEOPLE are in this room" in two
        assert "names no bot is for the other people" in two
        assert "human-m" in two

    def test_group_people_single_user_is_the_implicit_owner(self):
        group = {"id": 1, "created_by": "owner", "shared_with": [], "humans": ["human-owner"]}
        people = group_people(group)
        assert [p["role"] for p in people] == ["creator"]
        assert people[0]["principal"] == "human-owner"
        assert people[0]["is_admin"] is True


# ── routes ───────────────────────────────────────────────────────────────────


class TestShareRoutes:
    def test_sharing_lets_a_granted_teammate_see_post_and_leave(self, app, runtime):
        admin, alice, alice_id, bob, bob_id, gid = _team(app)
        assert bob.get(f"/api/groups/{gid}").status_code == 404

        resp = alice.post(f"/api/groups/{gid}/shares", json={"user": "bob"})
        assert resp.status_code == 200, resp.text
        group = resp.json()["group"]
        assert group["shared_with"] == [bob_id]
        roles = {p["username"]: p["role"] for p in group["people"]}
        assert roles == {"alice": "creator", "bob": "shared"}

        # Bob now lists it, opens it, and posts into it on his own grants.
        assert [g["id"] for g in bob.get("/api/groups").json()["groups"]] == [gid]
        assert bob.get(f"/api/groups/{gid}").status_code == 200
        resp = bob.post(f"/api/groups/{gid}/messages", json={"text": "@alpha hi"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["addressees"] == ["alpha"]
        rows = bob.get(f"/api/groups/{gid}/messages").json()["messages"]
        assert rows[0]["author"] == "human-bob" and rows[0]["mine"] is True
        # Alice reads the same row as bob's, not as her own.
        rows = alice.get(f"/api/groups/{gid}/messages").json()["messages"]
        assert rows[0]["author"] == "human-bob" and rows[0]["mine"] is False
        # Bob is in the room the agents are briefed on.
        prompt = runtime._runners["alpha"].ainvoke.await_args.args[0]
        assert "human-bob" in prompt and "human-alice" in prompt
        assert "Several PEOPLE are in this room" in prompt

        # Bob leaves; what he said stays.
        assert bob.delete(f"/api/groups/{gid}/shares/{bob_id}").status_code == 200
        assert bob.get(f"/api/groups/{gid}").status_code == 404
        assert bob.get("/api/groups").json()["groups"] == []
        rows = alice.get(f"/api/groups/{gid}/messages").json()["messages"]
        assert rows[0]["author"] == "human-bob"

    def test_a_guest_reads_and_posts_but_does_not_manage(self, app):
        admin, alice, alice_id, bob, bob_id, gid = _team(app)
        alice.post(f"/api/groups/{gid}/shares", json={"user": bob_id})
        assert bob.patch(f"/api/groups/{gid}", json={"name": "Bob's"}).status_code == 403
        assert bob.post(f"/api/groups/{gid}/members", json={"agent": "gamma"}).status_code in (
            403,
            400,
        )
        assert bob.delete(f"/api/groups/{gid}/members/alpha").status_code == 403
        assert bob.delete(f"/api/groups/{gid}").status_code == 403
        assert bob.get(f"/api/groups/{gid}/share-candidates").status_code == 403
        # Cannot re-share, cannot remove anyone but himself.
        assert bob.post(f"/api/groups/{gid}/shares", json={"user": "alice"}).status_code == 403
        assert bob.delete(f"/api/groups/{gid}/shares/{alice_id}").status_code == 403
        assert alice.get(f"/api/groups/{gid}").json()["group"]["name"] == "Ops"

    def test_a_share_needs_a_grant_on_every_agent(self, app):
        admin, alice, alice_id, bob, bob_id, gid = _team(app)
        from olano.users.store import get_user_store

        get_user_store().db.revoke_access(bob_id, "beta")
        resp = alice.post(f"/api/groups/{gid}/shares", json={"user": "bob"})
        assert resp.status_code == 400
        assert "beta" in resp.text
        assert bob.get(f"/api/groups/{gid}").status_code == 404
        # And the candidates list never offered him.
        assert alice.get(f"/api/groups/{gid}/share-candidates").json()["candidates"] == []

    def test_candidates_are_granted_non_admin_strangers(self, app):
        admin, alice, alice_id, bob, bob_id, gid = _team(app)
        carol, carol_id = _member(app, admin, "carol@x.com")
        _grant(carol_id, "alpha")  # beta missing
        names = {
            c["username"]
            for c in alice.get(f"/api/groups/{gid}/share-candidates").json()["candidates"]
        }
        assert names == {"bob"}
        alice.post(f"/api/groups/{gid}/shares", json={"user": bob_id})
        assert alice.get(f"/api/groups/{gid}/share-candidates").json()["candidates"] == []
        # Admins are refused outright: they already see everything.
        resp = alice.post(f"/api/groups/{gid}/shares", json={"user": "admin@x.com"})
        assert resp.status_code == 400 and "Admins" in resp.text
        assert alice.post(f"/api/groups/{gid}/shares", json={"user": "nobody"}).status_code == 404

    def test_an_agent_cannot_be_added_over_a_guests_head(self, app):
        admin, alice, alice_id, bob, bob_id, gid = _team(app)
        alice.post(f"/api/groups/{gid}/shares", json={"user": bob_id})
        # Bob holds alpha and beta, not gamma.
        resp = alice.post(f"/api/groups/{gid}/members", json={"agent": "gamma"})
        assert resp.status_code == 400
        assert "bob" in resp.text and "gamma" in resp.text
        assert alice.get(f"/api/groups/{gid}").json()["group"]["members"] == ["alpha", "beta"]
        _grant(bob_id, "gamma")
        assert alice.post(f"/api/groups/{gid}/members", json={"agent": "gamma"}).status_code == 200

    def test_admins_manage_any_group_and_see_it_without_a_share(self, app):
        admin, alice, alice_id, bob, bob_id, gid = _team(app)
        assert admin.post(f"/api/groups/{gid}/shares", json={"user": "bob"}).status_code == 200
        assert admin.delete(f"/api/groups/{gid}/shares/{bob_id}").status_code == 200
        # An admin who posts is in the room by having spoken.
        admin.post(f"/api/groups/{gid}/messages", json={"text": "@alpha status?"})
        people = alice.get(f"/api/groups/{gid}").json()["group"]["people"]
        assert {p["username"]: p["role"] for p in people} == {"alice": "creator", "admin": "spoke"}

    def test_a_guests_post_is_checked_against_their_own_grants(self, app):
        admin, alice, alice_id, bob, bob_id, gid = _team(app)
        alice.post(f"/api/groups/{gid}/shares", json={"user": bob_id})
        from olano.users.store import get_user_store

        get_user_store().db.revoke_access(bob_id, "alpha")
        assert (
            bob.post(f"/api/groups/{gid}/messages", json={"text": "@alpha hi"}).status_code == 403
        )
        # Alice's own grants are untouched.
        assert (
            alice.post(f"/api/groups/{gid}/messages", json={"text": "@alpha hi"}).status_code == 200
        )

    def test_sharing_is_refused_on_a_single_user_install(self, runtime):
        client = TestClient(create_api(runtime))
        enable_groups()
        gid = client.post("/api/groups", json={"name": "Ops", "members": ["alpha", "beta"]}).json()[
            "group"
        ]["id"]
        assert client.post(f"/api/groups/{gid}/shares", json={"user": "x"}).status_code == 400
        assert client.get(f"/api/groups/{gid}/share-candidates").json()["candidates"] == []
        group = client.get(f"/api/groups/{gid}").json()["group"]
        assert [p["role"] for p in group["people"]] == ["creator"]


class TestSharedRoomTurnTaking:
    def test_a_message_naming_nobody_wakes_nobody_in_a_shared_room(self, app, runtime):
        admin, alice, alice_id, bob, bob_id, gid = _team(app)
        alice.post(f"/api/groups/{gid}/shares", json={"user": bob_id})
        resp = alice.post(f"/api/groups/{gid}/messages", json={"text": "thanks bob, I'll take it"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["addressees"] == [] and body["addressee"] == ""
        assert body["cascade"] == {"running": False, "reason": NOBODY_ADDRESSED}
        assert [m["author_kind"] for m in body["messages"]] == ["human"]
        assert runtime._runners["alpha"].ainvoke.await_count == 0
        rows = bob.get(f"/api/groups/{gid}/messages").json()["messages"]
        assert [r["author"] for r in rows] == ["human-alice"]
        # A mention still works exactly as before.
        resp = bob.post(f"/api/groups/{gid}/messages", json={"text": "@alpha go"})
        assert resp.json()["addressees"] == ["alpha"]

    def test_the_fallback_returns_once_the_room_is_one_person_again(self, app, runtime):
        admin, alice, alice_id, bob, bob_id, gid = _team(app)
        alice.post(f"/api/groups/{gid}/shares", json={"user": bob_id})
        alice.delete(f"/api/groups/{gid}/shares/{bob_id}")
        resp = alice.post(f"/api/groups/{gid}/messages", json={"text": "hello"})
        assert resp.json()["addressees"] == ["alpha"]

    def test_the_streamed_turn_says_so_too(self, app, runtime):
        admin, alice, alice_id, bob, bob_id, gid = _team(app)
        alice.post(f"/api/groups/{gid}/shares", json={"user": bob_id})
        with alice.stream(
            "POST", f"/api/groups/{gid}/messages/stream", json={"text": "fyi bob"}
        ) as resp:
            assert resp.status_code == 200
            frames = [line for line in resp.iter_lines() if line.startswith("data:")]
        kinds = [f.split('"type": "')[1].split('"')[0] for f in frames if '"type"' in f]
        assert kinds[0] == "start" and "speaker" not in kinds
        assert any(NOBODY_ADDRESSED in f for f in frames)
        assert kinds[-1] == "done"
