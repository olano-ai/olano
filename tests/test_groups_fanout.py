"""A group message can address SEVERAL bots, and each of them takes a turn.

The behaviour this pins is the one a group chat gets wrong most visibly. You
write "@a @b @c can you look into this", and the honest reading is one request
put to three teammates — but the first version of this surface answered it with
a single reply from ``a`` and left ``b`` and ``c`` looking like they had been
invited and then ignored.

So: every ``@name`` that matches a member earns a turn, in the order they were
written, and each turn is composed *after* the previous one landed so the
second bot can see what the first said and do a different part of the job.

Pinned here:

- the addressing rule itself (``pick_addressees``), including the composer's
  explicit ``to``, which still means exactly one bot
- the fan-out on both endpoints: replies in order, all persisted, all returned
- a later member genuinely SEES the earlier member's reply in its prompt
- one member failing does not cost the others their turn, and the failure is
  written into the transcript rather than swallowed
- the cascade is seeded once, after the whole fan-out, from the EARLIEST
  un-served hand-off — a hand-off to somebody who already spoke on this
  message is not one, and an earlier member's request is not dropped because
  a later member settled
- the briefing every turn carries: the roster with descriptions, what ``@name``
  means, and only the agent-to-agent tools the member actually holds
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from olano.config.home import get_olano_paths, write_home_config_text
from olano.groups.cascade import group_briefing
from olano.groups.store import get_group_store
from olano.runtime.api import create_api
from olano.runtime.groups_routes import briefing_for, compose_turn, pick_addressees


def _write_groups_block(block: dict) -> None:
    paths = get_olano_paths()
    paths.root.mkdir(parents=True, exist_ok=True)
    raw: dict = {}
    if paths.config_path.exists():
        raw = yaml.safe_load(paths.config_path.read_text()) or {}
    raw["groups"] = block
    write_home_config_text(paths.config_path, yaml.safe_dump(raw, sort_keys=False))


def _runner(name: str, *, description: str = "", tools: list[str] | None = None) -> MagicMock:
    """A runner that records every prompt it is handed."""
    r = MagicMock()
    r.config = MagicMock(
        model="fake:instant",
        description=description,
        tools=list(tools or []),
        disabled_tools=[],
        subagents=[],
    )
    r.prompts = []

    async def ainvoke(prompt, **_kw):
        r.prompts.append(prompt)
        return f"{name} here"

    async def astream_events(prompt, **_kw):
        r.prompts.append(prompt)
        yield ("tool_call", {"id": "c1", "name": "talk_to_agent", "args": '{"agent_name": "x"}'})
        yield ("tool_result", {"id": "c1", "name": "talk_to_agent", "content": "ok"})
        yield ("subagent_text", {"text": "digging", "namespace": ("researcher",)})
        yield ("usage", {"input_tokens": 1})  # must NOT reach a group transcript
        yield ("text", f"{name} here")

    r.ainvoke = AsyncMock(side_effect=ainvoke)
    r.astream_events = astream_events
    return r


@pytest.fixture
def runtime():
    rt = MagicMock()
    rt._runners = {
        "alpha": _runner("alpha", description="Runs the numbers", tools=["talk_to_agent"]),
        "beta": _runner("beta", description="Writes the copy"),
        "gamma": _runner("gamma", description="Ships it"),
    }
    rt.registry = MagicMock()
    rt.registry.get.return_value = None
    return rt


@pytest.fixture
def client(runtime):
    _write_groups_block({"enabled": True})
    return TestClient(create_api(runtime))


def _make_group(client, members=("alpha", "beta", "gamma")) -> int:
    resp = client.post("/api/groups", json={"name": "War room", "members": list(members)})
    assert resp.status_code == 200, resp.text
    return resp.json()["group"]["id"]


def _frames(response) -> list[dict]:
    out = []
    for line in response.iter_lines():
        line = line.decode() if isinstance(line, bytes) else line
        if line.startswith("data:"):
            out.append(json.loads(line[5:].strip()))
    return out


# ── the addressing rule ──────────────────────────────────────────────────────


class TestPickAddressees:
    members = ["alpha", "beta", "gamma"]

    def test_every_mention_earns_a_turn_in_order(self):
        assert pick_addressees("@gamma @alpha go", self.members) == ["gamma", "alpha"]

    def test_duplicates_and_case_collapse_to_one_turn(self):
        assert pick_addressees("@Beta @beta @BETA hi", self.members) == ["beta"]

    def test_non_members_are_ignored_but_do_not_swallow_the_rest(self):
        assert pick_addressees("@nobody @beta hi", self.members) == ["beta"]

    def test_a_mention_before_punctuation_still_earns_a_turn(self):
        # A sentence-final "@beta." is beta, not the unknown handle "beta.".
        assert pick_addressees("@beta. then @gamma!", self.members) == ["beta", "gamma"]

    def test_naming_nobody_falls_back_to_the_first_member(self):
        assert pick_addressees("anyone home?", self.members) == ["alpha"]

    def test_the_composer_picker_still_means_exactly_one(self):
        # A dropdown is a choice of one. Letting it fan out would make the
        # control mean something other than what it says.
        assert pick_addressees("@beta @gamma hi", self.members, explicit="alpha") == ["alpha"]


# ── the fan-out ──────────────────────────────────────────────────────────────


class TestFanOut:
    def test_blocking_post_gives_every_named_member_a_turn(self, client):
        gid = _make_group(client)
        resp = client.post(
            f"/api/groups/{gid}/messages", json={"text": "@alpha @gamma what do you think?"}
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["addressees"] == ["alpha", "gamma"]
        # `addressee` stays the one that answered FIRST: it is what the field
        # has always meant and what the client's typing line reads.
        assert payload["addressee"] == "alpha"
        assert [m["author"] for m in payload["messages"]] == [
            payload["messages"][0]["author"],
            "alpha",
            "gamma",
        ]
        rows = get_group_store().messages(gid)
        assert [r["author_kind"] for r in rows] == ["human", "agent", "agent"]
        assert [r["author"] for r in rows[1:]] == ["alpha", "gamma"]

    def test_the_human_row_records_everyone_it_asked(self, client):
        gid = _make_group(client)
        client.post(f"/api/groups/{gid}/messages", json={"text": "@alpha @gamma go"})
        human = get_group_store().messages(gid)[0]
        # Naming one of three and leaving the other two looking uninvited is
        # exactly what the "→" chip must not do.
        assert human["addressee"] == "alpha, gamma"

    def test_a_later_member_sees_what_the_earlier_one_said(self, client, runtime):
        gid = _make_group(client)
        client.post(f"/api/groups/{gid}/messages", json={"text": "@alpha @beta split this"})
        beta_prompt = runtime._runners["beta"].prompts[-1]
        # The whole reason each turn is composed only when it is that member's
        # turn: unseen_for can't carry alpha's reply until alpha has replied.
        assert "alpha here" in beta_prompt
        # …and it is told the request was shared, so it does its own part.
        assert "@alpha" in beta_prompt

    def test_one_member_failing_does_not_cost_the_others_their_turn(self, client, runtime):
        runtime._runners["alpha"].ainvoke = AsyncMock(side_effect=RuntimeError("model down"))
        gid = _make_group(client)
        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "@alpha @beta go"})
        assert resp.status_code == 200, resp.text
        rows = get_group_store().messages(gid)
        assert [r["author_kind"] for r in rows] == ["human", "system", "agent"]
        assert "@alpha" in rows[1]["body"] and "could not answer" in rows[1]["body"]
        assert rows[2]["author"] == "beta"

    def test_nobody_answering_is_a_502(self, client, runtime):
        for name in ("alpha", "beta"):
            runtime._runners[name].ainvoke = AsyncMock(side_effect=RuntimeError("model down"))
        gid = _make_group(client)
        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "@alpha @beta go"})
        assert resp.status_code == 502

    def test_an_offline_member_is_skipped_not_fatal(self, client, runtime):
        del runtime._runners["alpha"]
        gid = _make_group(client)
        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "@alpha @beta go"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["addressees"] == ["beta"]

    def test_nobody_named_is_running_is_a_404(self, client, runtime):
        del runtime._runners["alpha"]
        gid = _make_group(client)
        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "@alpha go"})
        assert resp.status_code == 404
        assert "alpha" in resp.text


class TestFanOutStreaming:
    def test_each_member_opens_with_its_own_speaker_frame(self, client):
        gid = _make_group(client)
        with client.stream(
            "POST", f"/api/groups/{gid}/messages/stream", json={"text": "@alpha @beta go"}
        ) as resp:
            frames = _frames(resp)
        speakers = [f for f in frames if f["type"] == "speaker"]
        assert [(f["author"], f["index"], f["of"]) for f in speakers] == [
            ("alpha", 0, 2),
            ("beta", 1, 2),
        ]
        assert frames[0]["addressees"] == ["alpha", "beta"]
        assert [f["message"]["author"] for f in frames if f["type"] == "message"] == [
            "alpha",
            "beta",
        ]

    def test_tool_and_subagent_frames_reach_the_client_attributed(self, client):
        gid = _make_group(client)
        with client.stream(
            "POST", f"/api/groups/{gid}/messages/stream", json={"text": "@alpha go"}
        ) as resp:
            frames = _frames(resp)
        kinds = [f["type"] for f in frames]
        # A member reaching a peer or a subagent IS the collaboration a group
        # exists to show, so these are carried, not dropped.
        assert "tool_call" in kinds and "tool_result" in kinds and "subagent_text" in kinds
        # Per-turn cost belongs on the dashboard, never in a shared transcript.
        assert "usage" not in kinds
        sub = next(f for f in frames if f["type"] == "subagent_text")
        assert sub["namespace"] == ["researcher"]  # tuples are not JSON
        assert all(
            f["author"] == "alpha"
            for f in frames
            if f["type"] in ("text", "tool_call", "tool_result", "subagent_text")
        )

    def test_a_crash_mid_fanout_still_gives_the_next_member_its_turn(self, client, runtime):
        async def boom(_prompt, **_kw):
            raise RuntimeError("model fell over")
            yield  # pragma: no cover - makes this an async generator

        runtime._runners["alpha"].astream_events = boom
        gid = _make_group(client)
        with client.stream(
            "POST", f"/api/groups/{gid}/messages/stream", json={"text": "@alpha @beta go"}
        ) as resp:
            frames = _frames(resp)
        err = next(f for f in frames if f["type"] == "error")
        assert err["author"] == "alpha"
        assert [f["message"]["author"] for f in frames if f["type"] == "message"] == ["beta"]
        assert frames[-1]["type"] == "done"


class TestFanOutAndTheCascade:
    @pytest.fixture(autouse=True)
    def _inline_cascade(self, monkeypatch):
        """Run the cascade inside the POST, the documented test escape hatch.

        A backgrounded cascade is not observable from a synchronous test
        client: asserting on the transcript right after the POST reads it
        somewhere between "gamma has not started" and "gamma has finished".
        """
        monkeypatch.setenv("LIONCLAW_SYNC_GROUP_CASCADE", "1")

    def test_a_handoff_to_someone_who_already_spoke_is_not_a_cascade(self, client, runtime):
        # beta ends by naming alpha — who the human already asked, and who has
        # already answered. Handing it back would run alpha twice on one message.
        _write_groups_block({"enabled": True, "autonomous": True})

        async def beta_reply(_prompt, **_kw):
            return "done — @alpha over to you"

        runtime._runners["beta"].ainvoke = AsyncMock(side_effect=beta_reply)
        gid = _make_group(client)
        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "@alpha @beta go"})
        assert resp.json()["cascade"]["reason"] == "settled"
        assert resp.json()["cascade"].get("hops", 0) == 0
        # Two agent rows and no third: alpha did not answer this message twice.
        assert [r["author"] for r in get_group_store().messages(gid)[1:]] == ["alpha", "beta"]

    def test_an_earlier_member_s_handoff_is_not_dropped(self, client, runtime):
        """The hole this rule closes. Seeding the cascade from the LAST speaker
        alone silently threw away "@gamma can you take this?" whenever a later
        member happened to settle — which is exactly the collaboration a group
        exists for."""
        _write_groups_block({"enabled": True, "autonomous": True})

        async def alpha_reply(_prompt, **_kw):
            return "numbers look fine — @gamma can you ship it?"

        runtime._runners["alpha"].ainvoke = AsyncMock(side_effect=alpha_reply)
        gid = _make_group(client)
        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "@alpha @beta go"})
        # beta answered last and named nobody; alpha's request still wins, and
        # gamma really does take the turn it was handed. (Inline, so `running`
        # is already False — the exchange is over; `hops` is what it did.)
        assert resp.json()["cascade"]["hops"] == 1
        assert [r["author"] for r in get_group_store().messages(gid)[1:]] == [
            "alpha",
            "beta",
            "gamma",
        ]

    def test_a_handoff_to_an_uninvited_member_still_cascades(self, client, runtime):
        _write_groups_block({"enabled": True, "autonomous": True})

        async def beta_reply(_prompt, **_kw):
            return "@gamma can you ship it?"

        runtime._runners["beta"].ainvoke = AsyncMock(side_effect=beta_reply)
        gid = _make_group(client)
        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "@alpha @beta go"})
        assert resp.json()["cascade"]["hops"] == 1
        assert get_group_store().messages(gid)[-1]["author"] == "gamma"


# ── the briefing every turn carries ──────────────────────────────────────────


class TestBriefing:
    group = {"id": 1, "name": "War room", "members": ["alpha", "beta"]}

    def test_the_roster_carries_each_member_s_own_description(self):
        text = group_briefing(
            self.group,
            "alpha",
            roster=[
                {"name": "alpha", "description": "Runs the numbers"},
                {"name": "beta", "description": "Writes the copy"},
            ],
        )
        assert "@alpha (this is you) — Runs the numbers" in text
        assert "@beta — Writes the copy" in text

    def test_a_member_with_nothing_on_file_still_gets_a_line(self):
        text = group_briefing(self.group, "alpha", roster=[{"name": "alpha"}])
        assert "@beta" in text and "no description on file" in text

    def test_an_offline_member_is_said_to_be_offline(self):
        text = group_briefing(self.group, "alpha", roster=[{"name": "beta", "online": False}])
        assert "not running right now" in text

    def test_only_tools_the_member_holds_are_named(self):
        held = group_briefing(self.group, "alpha", peer_tools=["talk_to_agent"])
        assert "talk_to_agent" in held
        assert "group_send" not in held
        # Naming a tool the agent was never bound to is worse than naming none:
        # the model plans around it and then fails the call.
        bare = group_briefing(self.group, "alpha", peer_tools=[])
        assert "talk_to_agent" not in bare
        assert "@mention" in bare  # the route that always exists

    def test_subagents_are_named_when_there_are_any(self):
        text = group_briefing(self.group, "alpha", subagents=["researcher"])
        assert "task(" in text and "researcher" in text
        assert "task(" not in group_briefing(self.group, "alpha")

    def test_the_autonomy_switch_changes_what_a_mention_promises(self):
        on = group_briefing(self.group, "alpha", autonomous=True)
        off = group_briefing(self.group, "alpha", autonomous=False)
        assert "handed the next turn automatically" in on
        assert "Follow-on turns are off" in off

    def test_it_states_the_rule_about_somebody_else_s_message(self):
        text = group_briefing(self.group, "alpha")
        assert "naming somebody else is context" in text

    def test_the_runtime_builder_resolves_the_roster_and_the_tools(self, runtime):
        text = briefing_for(runtime, MagicMock(autonomous=False), self.group, "alpha")
        assert "Runs the numbers" in text and "Writes the copy" in text
        assert "talk_to_agent" in text  # alpha's configured tool
        assert "group_send" not in text  # …and nothing it does not hold

    def test_compose_turn_prefixes_the_briefing_and_names_the_co_addressees(self):
        prompt = compose_turn(
            self.group,
            "alpha",
            [{"author": "human-bob", "body": "ship it"}],
            briefing="BRIEFING HERE",
            also_addressed=["alpha", "beta"],
        )
        assert prompt.startswith("BRIEFING HERE")
        assert "@beta" in prompt and "Do the part that is yours" in prompt
        assert "human-bob: ship it" in prompt

    def test_compose_turn_still_works_with_no_briefing(self):
        # The two-argument call is what a caller with no runtime makes.
        prompt = compose_turn(self.group, "alpha", [])
        assert '"alpha"' in prompt and "beta" in prompt
        assert "You are the one being addressed." in prompt
