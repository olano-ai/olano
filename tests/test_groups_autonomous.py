"""Autonomous bot-to-bot cascades: the budgets, and what happens when each blows.

The feature under test is an agent loop that can start another agent loop, so
most of this file is about the ways it must *stop*: the hop cap, the wall
clock, ping-pong, a self-mention, a mention of someone who is not in the room,
a grant revoked mid-exchange, and a model that raises halfway through. The
common assertion in every one of them is the same two-part promise —
**everything already said is still in the transcript, and the transcript says
why it stopped**.

``run_cascade`` takes its runner lookup, access check, thread namer and SSE
poke as arguments, so the loop is driven here with plain fakes: no runtime, no
FastAPI, no models. The route-level tests then check the wiring, with
``LIONCLAW_SYNC_GROUP_CASCADE=1`` so a synchronous test client can observe an
exchange that normally runs behind the response.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from olano.config.home import get_olano_paths, write_home_config_text
from olano.config.system_settings import GroupThreadSettings, load_group_thread_settings
from olano.groups import cascade as cascade_mod
from olano.groups.cascade import (
    ACCESS_DENIED,
    ERROR,
    HOP_CAP,
    OFFLINE,
    PING_PONG,
    SETTLED,
    STOPPED,
    TIMEOUT,
    CascadeLimits,
    CascadeRegistry,
    CascadeState,
    compose_hop_turn,
    mentioned_members,
    next_speaker,
    render_transcript,
    run_cascade,
)
from olano.groups.store import get_group_store
from olano.runtime.api import create_api

# ── harness ──────────────────────────────────────────────────────────────────


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


def enable_autonomous(**overrides: object) -> None:
    _write_groups_block({"enabled": True, "autonomous": True, **overrides})


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is process-global; a leaked entry would make the next
    test's group look busy."""
    cascade_mod.REGISTRY.reset()
    yield
    cascade_mod.REGISTRY.reset()


class FakeRunner:
    """A member that answers with a scripted line per turn.

    ``replies`` may hold a string (used forever), a list (one per turn, the
    last repeating) or an exception instance (raised).
    """

    def __init__(self, name: str, replies):
        self.name = name
        self._replies = replies if isinstance(replies, list) else [replies]
        self.calls: list[dict] = []

    async def ainvoke(self, message: str, thread_id: str | None = None) -> str:
        index = min(len(self.calls), len(self._replies) - 1)
        self.calls.append({"prompt": message, "thread_id": thread_id})
        reply = self._replies[index]
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return str(await reply())
        return str(reply)


def _group(members=("alpha", "beta"), name="Ops") -> dict:
    return get_group_store().create(name=name, members=list(members), created_by="u1")


def _drive(group: dict, *, speaker: str, reply: str, runners: dict, **kwargs):
    """Run a cascade over a real store with fake runners."""
    store = get_group_store()
    store.add_message(group["id"], author=speaker, author_kind="agent", body=reply, addressee="")
    params: dict = {
        "group_id": group["id"],
        "speaker": speaker,
        "reply": reply,
        "store": store,
        "limits": kwargs.pop("limits", CascadeLimits(max_hops=3, timeout_seconds=60)),
        "runner_for": kwargs.pop("runner_for", lambda n: runners.get(n)),
        "assert_access": kwargs.pop("assert_access", lambda n: None),
        "thread_id_for": kwargs.pop("thread_id_for", lambda n: f"group-{group['id']}-{n}"),
    }
    params.update(kwargs)
    return asyncio.get_event_loop().run_until_complete(run_cascade(**params))


def _bodies(group_id: int) -> list[tuple[str, str]]:
    return [(m["author"], m["body"]) for m in get_group_store().messages(group_id)]


# ── settings ─────────────────────────────────────────────────────────────────


class TestCascadeSettings:
    def test_autonomy_is_on_by_default(self):
        settings = load_group_thread_settings()
        assert settings.autonomous is True
        assert settings.max_hops == 3
        assert settings.cascade_timeout_seconds == 180

    def test_an_explicit_false_turns_autonomy_off(self):
        _write_groups_block({"enabled": True, "autonomous": False})
        settings = load_group_thread_settings()
        assert settings.enabled is True
        assert settings.autonomous is False

    def test_budgets_are_clamped_to_a_ceiling(self):
        assert GroupThreadSettings(max_hops=999).max_hops == 10
        assert GroupThreadSettings(max_hops=0).max_hops == 1
        assert GroupThreadSettings(cascade_timeout_seconds=99999).cascade_timeout_seconds == 1800
        assert GroupThreadSettings(cascade_timeout_seconds=1).cascade_timeout_seconds == 10

    def test_limits_read_from_the_block(self):
        enable_autonomous(max_hops=5, cascade_timeout_seconds=30)
        limits = CascadeLimits.from_settings(load_group_thread_settings())
        assert (limits.max_hops, limits.timeout_seconds) == (5, 30.0)

    def test_a_malformed_value_falls_back_to_the_default_which_is_on(self):
        # Fail-open, and deliberately pinned: the loader swallows junk and
        # returns defaults, and the default is now the cascade. An operator
        # who wants it off has to spell `false`.
        _write_groups_block({"enabled": True, "autonomous": "of course"})
        assert load_group_thread_settings().autonomous is True


# ── who gets the next turn ───────────────────────────────────────────────────


class TestNextSpeaker:
    def test_a_mention_hands_over(self):
        assert next_speaker("nice, @beta take it", speaker="alpha", members=["alpha", "beta"])
        assert (
            next_speaker("nice, @Beta take it", speaker="alpha", members=["alpha", "beta"])
            == "beta"
        )

    def test_no_mention_settles(self):
        assert next_speaker("done, shipping it", speaker="alpha", members=["alpha", "beta"]) == ""

    def test_self_mention_is_never_a_hop(self):
        """An agent naming itself is talking about itself; scheduling it would
        be a one-agent loop that only the hop cap could tell from progress."""
        assert (
            next_speaker("as @alpha I think so", speaker="alpha", members=["alpha", "beta"]) == ""
        )
        # ...and the next member named after itself still gets the turn.
        assert (
            next_speaker("@alpha and @beta agree", speaker="alpha", members=["alpha", "beta"])
            == "beta"
        )

    def test_a_non_member_is_ignored(self):
        assert next_speaker("@ceo please approve", speaker="alpha", members=["alpha", "beta"]) == ""

    def test_a_mention_that_ends_a_sentence_still_hands_over(self):
        """The regex admits ``.`` and ``-`` inside a handle, and used to keep
        a trailing one too — "over to you, @beta." read as the handle "beta.",
        matched nobody, and settled the thread with beta never called."""
        members = ["alpha", "beta"]
        assert next_speaker("over to you, @beta.", speaker="alpha", members=members) == "beta"
        assert next_speaker("(@beta)", speaker="alpha", members=members) == "beta"
        assert mentioned_members("@beta... @beta-", members) == ["beta"]
        # A dotted handle is still whole.
        assert mentioned_members("@a.b hi", ["a.b"]) == ["a.b"]

    def test_mentions_are_deduplicated_in_order(self):
        assert mentioned_members("@beta @gamma @beta", ["alpha", "beta", "gamma"]) == [
            "beta",
            "gamma",
        ]

    def test_hop_prompt_says_who_called_and_what_is_left(self):
        prompt = compose_hop_turn(
            {"name": "Ops", "members": ["alpha", "beta"]},
            "beta",
            [{"author": "alpha", "body": "@beta numbers please"}],
            called_by="alpha",
            hops_left=2,
        )
        assert '"beta"' in prompt and "alpha addressed you" in prompt
        assert "2 more bot-to-bot turn" in prompt
        assert "alpha: @beta numbers please" in prompt

    def test_hop_prompt_tells_the_last_speaker_to_land_it(self):
        prompt = compose_hop_turn(
            {"name": "Ops", "members": ["alpha", "beta"]},
            "beta",
            [],
            called_by="alpha",
            hops_left=0,
        )
        assert "last turn" in prompt


# ── the loop: the happy path ─────────────────────────────────────────────────


class TestCascade:
    def test_a_reply_that_hands_over_runs_the_next_member(self):
        group = _group(("alpha", "beta"))
        runners = {"beta": FakeRunner("beta", "numbers look fine")}
        result = _drive(group, speaker="alpha", reply="@beta numbers?", runners=runners)
        assert (result.hops, result.reason) == (1, SETTLED)
        assert _bodies(group["id"]) == [
            ("alpha", "@beta numbers?"),
            ("beta", "numbers look fine"),
        ]
        # Settling needs no note — silence after an answer is not a failure.
        assert result.note == ""

    def test_work_passes_along_a_chain(self):
        group = _group(("alpha", "beta", "gamma"))
        runners = {
            "beta": FakeRunner("beta", "drafted, @gamma review"),
            "gamma": FakeRunner("gamma", "looks good, done"),
        }
        result = _drive(group, speaker="alpha", reply="@beta draft it", runners=runners)
        assert (result.hops, result.reason) == (2, SETTLED)
        assert [a for a, _ in _bodies(group["id"])] == ["alpha", "beta", "gamma"]

    def test_each_hop_records_who_it_addressed_and_who_it_answered(self):
        """Attribution is the whole reason the UI can render 'beta → gamma' —
        and, separately, 'beta ↩ alpha'. Who a hop hands the thread to and
        who it was answering are different questions, kept in different
        columns so neither has to be spelled into the text."""
        group = _group(("alpha", "beta", "gamma"))
        runners = {
            "beta": FakeRunner("beta", "@gamma over to you"),
            "gamma": FakeRunner("gamma", "finished"),
        }
        _drive(group, speaker="alpha", reply="@beta start", runners=runners)
        rows = get_group_store().messages(group["id"])
        assert [(r["author"], r["addressee"], r["reply_to"]) for r in rows[1:]] == [
            ("beta", "gamma", "alpha"),
            ("gamma", "", "beta"),
        ]

    def test_a_reply_to_the_caller_is_a_hand_back_and_still_bounded(self):
        """The property #433's routing strip would have removed: in a
        two-member group beta CAN hand the thread back to alpha (a clarifying
        question deserves an answer), and the ordered-pair guard is what
        stops the volley — with a note, one hop before the cap."""
        group = _group(("alpha", "beta"))
        runners = {
            "alpha": FakeRunner("alpha", "@beta yes, the Q3 figure"),
            "beta": FakeRunner("beta", "@alpha here it is — did you mean Q2 or Q3?"),
        }
        result = _drive(
            group,
            speaker="alpha",
            reply="@beta the number please",
            runners=runners,
            limits=CascadeLimits(max_hops=5, timeout_seconds=60),
        )
        assert [
            (r["author"], r["reply_to"]) for r in get_group_store().messages(group["id"])[1:]
        ] == [
            ("beta", "alpha"),
            ("alpha", "beta"),
            ("system", ""),
        ]
        assert (result.hops, result.reason) == (2, PING_PONG)

    def test_the_catch_up_block_says_whom_a_hop_answered(self):
        """The next reader sees ``beta (to alpha): …`` rather than having to
        infer from adjacency — or from an ``@alpha`` the model was told to
        write, which would read as a hand-off."""
        group = _group(("alpha", "beta", "gamma"))
        runners = {
            "beta": FakeRunner("beta", "@gamma can you verify?"),
            "gamma": FakeRunner("gamma", "verified"),
        }
        _drive(group, speaker="alpha", reply="@beta numbers?", runners=runners)
        prompt = runners["gamma"].calls[0]["prompt"]
        assert "alpha: @beta numbers?" in prompt
        assert "beta (to alpha): @gamma can you verify?" in prompt
        # Rows from before the column existed (and human rows) render as before.
        assert render_transcript([{"author": "human-bob", "body": "hi"}]) == "human-bob: hi"

    def test_each_member_keeps_its_own_group_thread(self):
        group = _group(("alpha", "beta"))
        runners = {"beta": FakeRunner("beta", "ok")}
        _drive(group, speaker="alpha", reply="@beta ping", runners=runners)
        assert runners["beta"].calls[0]["thread_id"] == f"group-{group['id']}-beta"

    def test_a_hop_only_carries_what_that_member_has_not_seen(self):
        group = _group(("alpha", "beta"))
        store = get_group_store()
        store.add_message(group["id"], author="beta", author_kind="agent", body="old news")
        runners = {"beta": FakeRunner("beta", "ok")}
        _drive(group, speaker="alpha", reply="@beta fresh", runners=runners)
        prompt = runners["beta"].calls[0]["prompt"]
        assert "fresh" in prompt and "old news" not in prompt

    def test_an_empty_answer_settles_instead_of_looping(self):
        group = _group(("alpha", "beta"))
        result = _drive(
            group, speaker="alpha", reply="@beta ?", runners={"beta": FakeRunner("beta", "   ")}
        )
        assert result.reason == SETTLED
        assert [a for a, _ in _bodies(group["id"])] == ["alpha"]

    def test_a_hop_never_carries_a_human_ownership_grant(self):
        """A cascade hop is one agent answering another. The task running it
        may have snapshotted a human turn's context — request-context metadata
        merges, so AGENT_ADMIN_KEY would be inherited — and the hop must bind
        its own False so the self_config ownership gate refuses. Simulate the
        worst case: a cascade driven from inside an owner's bound context."""
        from olano.security.request_context import (
            AGENT_ADMIN_KEY,
            context_agent_admin,
            push_request_context,
        )

        enable_autonomous()
        group = _group()
        seen: dict = {}

        async def _capture() -> str:
            seen["agent_admin"] = context_agent_admin()
            return "done."

        runners = {"beta": FakeRunner("beta", _capture)}
        with push_request_context(source="api", channel="api", metadata={AGENT_ADMIN_KEY: True}):
            _drive(group, speaker="alpha", reply="@beta take it", runners=runners)
        assert seen["agent_admin"] is False


# ── the loop: every way it must stop ─────────────────────────────────────────


class TestBudgets:
    def test_hop_cap_settles_the_thread_with_a_visible_note(self):
        group = _group(("alpha", "beta"))
        runners = {
            "alpha": FakeRunner("alpha", "@beta again"),
            "beta": FakeRunner("beta", "@alpha and again"),
        }
        result = _drive(
            group,
            speaker="alpha",
            reply="@beta start",
            runners=runners,
            limits=CascadeLimits(max_hops=2, timeout_seconds=60),
        )
        assert (result.hops, result.reason) == (2, HOP_CAP)
        rows = get_group_store().messages(group["id"])
        assert len(rows) == 4  # opener + 2 hops + the note
        assert rows[-1]["author_kind"] == "system"
        assert "groups.max_hops" in rows[-1]["body"]

    def test_ping_pong_stops_before_the_hop_budget_is_spent(self):
        """alpha→beta→alpha→beta would repeat an ordered pair; the third hop
        never runs even though the cap allowed five."""
        group = _group(("alpha", "beta"))
        runners = {
            "alpha": FakeRunner("alpha", "@beta you"),
            "beta": FakeRunner("beta", "@alpha no you"),
        }
        result = _drive(
            group,
            speaker="alpha",
            reply="@beta you",
            runners=runners,
            limits=CascadeLimits(max_hops=5, timeout_seconds=60),
        )
        assert (result.hops, result.reason) == (2, PING_PONG)
        note = get_group_store().messages(group["id"])[-1]
        assert note["author_kind"] == "system"
        assert "back and forth" in note["body"]

    def test_the_wall_clock_stops_a_slow_exchange_and_keeps_what_it_produced(self):
        group = _group(("alpha", "beta", "gamma"))
        # Two readings inside the budget (the deadline, then hop 1), and the
        # clock is past it by the time hop 2 is considered.
        ticks = {"n": 0}

        def clock() -> float:
            ticks["n"] += 1
            return 0.0 if ticks["n"] <= 2 else 900.0

        runners = {
            "beta": FakeRunner("beta", "@gamma over"),
            "gamma": FakeRunner("gamma", "@beta back"),
        }
        result = _drive(
            group,
            speaker="alpha",
            reply="@beta go",
            runners=runners,
            limits=CascadeLimits(max_hops=5, timeout_seconds=60),
            clock=clock,
        )
        assert (result.hops, result.reason) == (1, TIMEOUT)
        rows = get_group_store().messages(group["id"])
        assert [r["author"] for r in rows] == ["alpha", "beta", "system"]
        assert "cascade_timeout_seconds" in rows[-1]["body"]

    def test_a_hop_that_never_returns_is_cut_at_the_budget(self):
        """The wall clock is enforced *inside* a hop too — a model that hangs
        cannot pin the worker past the budget."""
        group = _group(("alpha", "beta"))

        async def _hang() -> str:
            await asyncio.sleep(30)
            return "too late"

        runners = {"beta": FakeRunner("beta", _hang)}
        result = _drive(
            group,
            speaker="alpha",
            reply="@beta ping",
            runners=runners,
            limits=CascadeLimits(max_hops=3, timeout_seconds=0.05),
        )
        assert result.reason == TIMEOUT
        assert [a for a, _ in _bodies(group["id"])] == ["alpha", "system"]

    def test_a_failing_hop_keeps_every_earlier_message(self):
        group = _group(("alpha", "beta", "gamma"))
        runners = {
            "beta": FakeRunner("beta", "on it, @gamma finish"),
            "gamma": FakeRunner("gamma", RuntimeError("model down")),
        }
        result = _drive(group, speaker="alpha", reply="@beta start", runners=runners)
        assert (result.hops, result.reason) == (2, ERROR)
        rows = get_group_store().messages(group["id"])
        assert [r["author"] for r in rows] == ["alpha", "beta", "system"]
        assert "model down" in rows[-1]["body"]

    def test_an_offline_member_stops_the_cascade_and_says_so(self):
        group = _group(("alpha", "beta"))
        result = _drive(group, speaker="alpha", reply="@beta ping", runners={})
        assert (result.hops, result.reason) == (0, OFFLINE)
        assert "not running" in get_group_store().messages(group["id"])[-1]["body"]

    def test_access_lost_mid_cascade_stops_the_very_next_hop(self):
        """The roster is not a permission: the check that gates the human path
        gates every hop, so a grant revoked mid-exchange bites immediately."""
        group = _group(("alpha", "beta", "gamma"))
        revoked = {"gamma"}

        def _assert_access(name: str) -> None:
            if name in revoked:
                raise PermissionError("agent_access_denied")

        runners = {
            "beta": FakeRunner("beta", "@gamma your turn"),
            "gamma": FakeRunner("gamma", "should never run"),
        }
        result = _drive(
            group,
            speaker="alpha",
            reply="@beta go",
            runners=runners,
            assert_access=_assert_access,
        )
        assert (result.hops, result.reason) == (1, ACCESS_DENIED)
        assert runners["gamma"].calls == []
        rows = get_group_store().messages(group["id"])
        assert [r["author"] for r in rows] == ["alpha", "beta", "system"]
        assert "no longer have access" in rows[-1]["body"]

    def test_a_member_removed_mid_cascade_gets_no_further_turn(self):
        group = _group(("alpha", "beta", "gamma"))
        store = get_group_store()

        class _Dropping(FakeRunner):
            async def ainvoke(self, message: str, thread_id: str | None = None) -> str:
                store.remove_member(group["id"], "gamma")
                return await super().ainvoke(message, thread_id)

        runners = {
            "beta": _Dropping("beta", "@gamma take it"),
            "gamma": FakeRunner("gamma", "should never run"),
        }
        result = _drive(group, speaker="alpha", reply="@beta go", runners=runners)
        # gamma is no longer in the room, so the mention addresses nobody.
        assert (result.hops, result.reason) == (1, SETTLED)
        assert runners["gamma"].calls == []

    def test_a_stop_flag_ends_the_exchange_between_hops(self):
        group = _group(("alpha", "beta", "gamma"))
        state = CascadeState(group_id=group["id"])

        class _Stopping(FakeRunner):
            async def ainvoke(self, message: str, thread_id: str | None = None) -> str:
                state.stopped = True
                return await super().ainvoke(message, thread_id)

        runners = {
            "beta": _Stopping("beta", "@gamma keep going"),
            "gamma": FakeRunner("gamma", "should never run"),
        }
        result = _drive(group, speaker="alpha", reply="@beta go", runners=runners, state=state)
        assert (result.hops, result.reason) == (1, STOPPED)
        assert runners["gamma"].calls == []
        assert "a human ended" in get_group_store().messages(group["id"])[-1]["body"]

    def test_a_deleted_group_ends_the_cascade_without_resurrecting_it(self):
        group = _group(("alpha", "beta"))
        store = get_group_store()
        store.add_message(group["id"], author="alpha", author_kind="agent", body="@beta ping")
        store.delete(group["id"])
        runner = FakeRunner("beta", "hi")
        result = asyncio.get_event_loop().run_until_complete(
            run_cascade(
                group_id=group["id"],
                speaker="alpha",
                reply="@beta ping",
                store=store,
                limits=CascadeLimits(),
                runner_for=lambda n: runner,
                assert_access=lambda n: None,
                thread_id_for=lambda n: "t",
            )
        )
        assert result.hops == 0
        assert runner.calls == []
        assert store.messages(group["id"]) == []

    def test_every_message_is_persisted_as_it_lands(self):
        """A cascade that dies at hop 3 must leave hops 1–2 behind — so the
        store is written *during* the loop, never at the end."""
        group = _group(("alpha", "beta", "gamma"))
        seen: list[int] = []

        class _Watching(FakeRunner):
            async def ainvoke(self, message: str, thread_id: str | None = None) -> str:
                seen.append(len(get_group_store().messages(group["id"])))
                return await super().ainvoke(message, thread_id)

        runners = {
            "beta": _Watching("beta", "@gamma next"),
            "gamma": _Watching("gamma", RuntimeError("boom")),
        }
        _drive(group, speaker="alpha", reply="@beta go", runners=runners)
        # gamma's turn already saw beta's message on disk.
        assert seen == [1, 2]


# ── SSE pokes ────────────────────────────────────────────────────────────────


class TestPublishing:
    def test_each_message_pokes_the_stream_as_it_lands(self):
        group = _group(("alpha", "beta", "gamma"))
        frames: list[tuple] = []
        runners = {
            "beta": FakeRunner("beta", "@gamma next"),
            "gamma": FakeRunner("gamma", "done"),
        }
        _drive(
            group,
            speaker="alpha",
            reply="@beta go",
            runners=runners,
            publish=lambda kind, **extra: frames.append((kind, extra)),
        )
        assert [k for k, _ in frames] == ["message", "message", "cascade_end"]
        assert frames[0][1]["hop"] == 1 and frames[1][1]["hop"] == 2
        assert frames[-1][1]["reason"] == SETTLED

    def test_frames_carry_no_message_content(self):
        """The shared /api/stream bus fans out to every subscriber and only
        filters frames it can attribute to an agent — so a group frame must
        carry nothing but a poke."""
        group = _group(("alpha", "beta"))
        frames: list[tuple] = []
        _drive(
            group,
            speaker="alpha",
            reply="@beta secret plan",
            runners={"beta": FakeRunner("beta", "confidential answer")},
            publish=lambda kind, **extra: frames.append((kind, extra)),
        )
        blob = repr(frames)
        assert "confidential" not in blob and "secret" not in blob and "beta" not in blob


# ── the registry ─────────────────────────────────────────────────────────────


class TestRegistry:
    def test_one_cascade_per_group(self):
        reg = CascadeRegistry()
        limits = CascadeLimits()
        assert reg.begin(1, limits=limits) is not None
        assert reg.begin(1, limits=limits) is None  # already running
        assert reg.begin(2, limits=limits) is not None  # a different group is free

    def test_finishing_releases_the_group(self):
        reg = CascadeRegistry()
        state = reg.begin(1, limits=CascadeLimits())
        assert state is not None
        reg.finish(state)
        assert reg.get(1) is None
        assert reg.begin(1, limits=CascadeLimits()) is not None

    def test_stopping_nothing_is_not_an_error(self):
        assert CascadeRegistry().stop(404) is False

    async def test_stop_cancels_an_in_flight_hop(self):
        """A human must not have to wait out a model call to cut the bots off."""
        reg = CascadeRegistry()
        group = _group(("alpha", "beta"))
        store = get_group_store()
        store.add_message(group["id"], author="alpha", author_kind="agent", body="@beta go")
        started = asyncio.Event()

        async def _slow() -> str:
            started.set()
            await asyncio.sleep(30)
            return "never"

        state = reg.begin(group["id"], limits=CascadeLimits(max_hops=3, timeout_seconds=60))
        assert state is not None
        task = asyncio.get_running_loop().create_task(
            run_cascade(
                group_id=group["id"],
                speaker="alpha",
                reply="@beta go",
                store=store,
                limits=CascadeLimits(max_hops=3, timeout_seconds=60),
                runner_for=lambda n: {"beta": FakeRunner("beta", _slow)}.get(n),
                assert_access=lambda n: None,
                thread_id_for=lambda n: f"group-{group['id']}-{n}",
                state=state,
            )
        )
        state.task = task
        await asyncio.wait_for(started.wait(), timeout=5)
        assert reg.stop(group["id"]) is True
        result = await asyncio.wait_for(task, timeout=5)
        assert result.reason == STOPPED
        # The cut-off is recorded even though the task was cancelled mid-await.
        assert "a human ended" in store.messages(group["id"])[-1]["body"]

    async def test_a_cascade_cancelled_from_outside_still_reports_up(self):
        """A shutdown-driven cancel is not a human Stop: the note is written,
        and the cancellation is re-raised so the loop can finish tearing down."""
        group = _group(("alpha", "beta"))
        store = get_group_store()
        store.add_message(group["id"], author="alpha", author_kind="agent", body="@beta go")
        started = asyncio.Event()

        async def _slow() -> str:
            started.set()
            await asyncio.sleep(30)
            return "never"

        task = asyncio.get_running_loop().create_task(
            run_cascade(
                group_id=group["id"],
                speaker="alpha",
                reply="@beta go",
                store=store,
                limits=CascadeLimits(max_hops=3, timeout_seconds=60),
                runner_for=lambda n: {"beta": FakeRunner("beta", _slow)}.get(n),
                assert_access=lambda n: None,
                thread_id_for=lambda n: f"group-{group['id']}-{n}",
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.messages(group["id"])[-1]["author_kind"] == "system"


# ── routes ───────────────────────────────────────────────────────────────────


def _runner(name: str, reply: str) -> MagicMock:
    r = MagicMock()
    r.config = MagicMock(model="anthropic:claude-haiku-4-5-20251001")
    r.ainvoke = AsyncMock(return_value=reply)
    return r


@pytest.fixture
def runtime():
    rt = MagicMock()
    rt._runners = {
        "alpha": _runner("alpha", "on it, @beta take the numbers"),
        "beta": _runner("beta", "numbers are fine, nothing else needed"),
        "gamma": _runner("gamma", "gamma here"),
    }
    rt.registry = MagicMock()
    rt.registry.get.return_value = None
    return rt


@pytest.fixture
def client(runtime, monkeypatch):
    # The cascade normally runs behind the response; a synchronous test client
    # cannot observe a background task, so run it inline here.
    monkeypatch.setenv("LIONCLAW_SYNC_GROUP_CASCADE", "1")
    return TestClient(create_api(runtime))


def _make_group(client, members=("alpha", "beta")) -> dict:
    resp = client.post("/api/groups", json={"name": "Standup", "members": list(members)})
    assert resp.status_code == 200, resp.text
    return resp.json()["group"]


class TestRoutes:
    def test_with_autonomy_off_the_reply_still_stops_at_one_member(self, client, runtime):
        _write_groups_block({"enabled": True, "autonomous": False})
        gid = _make_group(client)["id"]
        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "morning"})
        assert resp.json()["cascade"] == {"running": False, "reason": "off"}
        assert runtime._runners["beta"].ainvoke.await_count == 0
        assert [
            m["author"] for m in client.get(f"/api/groups/{gid}/messages").json()["messages"]
        ] == [
            "human-owner",
            "alpha",
        ]

    def test_with_autonomy_on_the_addressed_member_takes_the_next_turn(self, client, runtime):
        enable_autonomous()
        gid = _make_group(client)["id"]
        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "morning"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["cascade"]["hops"] == 1
        assert runtime._runners["beta"].ainvoke.await_count == 1
        # A client that only polls sees the identical, complete transcript.
        rows = client.get(f"/api/groups/{gid}/messages").json()["messages"]
        assert [r["author"] for r in rows] == ["human-owner", "alpha", "beta"]
        assert rows[1]["addressee"] == "beta"

    def test_the_hop_cap_from_config_binds_the_route(self, client, runtime):
        enable_autonomous(max_hops=1)
        runtime._runners["beta"].ainvoke = AsyncMock(return_value="@gamma your turn")
        gid = _make_group(client, ("alpha", "beta", "gamma"))["id"]
        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "go"})
        assert resp.json()["cascade"]["reason"] == HOP_CAP
        assert runtime._runners["gamma"].ainvoke.await_count == 0
        rows = client.get(f"/api/groups/{gid}/messages").json()["messages"]
        assert rows[-1]["author_kind"] == "system"
        assert "groups.max_hops" in rows[-1]["body"]

    def test_a_reply_that_addresses_nobody_never_starts_a_cascade(self, client, runtime):
        enable_autonomous()
        runtime._runners["alpha"].ainvoke = AsyncMock(return_value="all done")
        gid = _make_group(client)["id"]
        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "status?"})
        assert resp.json()["cascade"] == {"running": False, "reason": "settled"}
        assert runtime._runners["beta"].ainvoke.await_count == 0

    def test_a_bot_cannot_summon_a_non_member(self, client, runtime):
        enable_autonomous()
        runtime._runners["alpha"].ainvoke = AsyncMock(return_value="@gamma help")
        gid = _make_group(client, ("alpha", "beta"))["id"]
        resp = client.post(f"/api/groups/{gid}/messages", json={"text": "go"})
        assert resp.json()["cascade"]["reason"] == "settled"
        assert runtime._runners["gamma"].ainvoke.await_count == 0

    def test_a_bot_that_mentions_itself_does_not_get_a_second_turn(self, client, runtime):
        enable_autonomous()
        runtime._runners["alpha"].ainvoke = AsyncMock(return_value="speaking as @alpha, done")
        gid = _make_group(client)["id"]
        client.post(f"/api/groups/{gid}/messages", json={"text": "go"})
        assert runtime._runners["alpha"].ainvoke.await_count == 1

    def test_the_status_endpoint_reports_the_budget(self, client):
        enable_autonomous(max_hops=4, cascade_timeout_seconds=45)
        gid = _make_group(client)["id"]
        payload = client.get(f"/api/groups/{gid}/cascade").json()
        assert payload["cascade"] == {"running": False}
        assert payload["settings"] == {
            "autonomous": True,
            "max_hops": 4,
            "cascade_timeout_seconds": 45,
        }

    def test_stop_is_honest_when_nothing_is_running(self, client):
        enable_autonomous()
        gid = _make_group(client)["id"]
        assert client.post(f"/api/groups/{gid}/stop").json() == {"ok": True, "stopped": False}

    def test_stop_ends_a_running_cascade(self, client, runtime):
        enable_autonomous()
        gid = _make_group(client)["id"]
        state = cascade_mod.REGISTRY.begin(gid, limits=CascadeLimits())
        assert state is not None
        assert client.post(f"/api/groups/{gid}/stop").json()["stopped"] is True
        assert state.stopped is True

    def test_the_new_paths_404_while_the_feature_is_off(self, client):
        assert client.get("/api/groups/1/cascade").status_code == 404
        assert client.post("/api/groups/1/stop").status_code == 404

    def test_the_new_paths_hide_someone_elses_group(self, runtime, monkeypatch):
        monkeypatch.setenv("LIONCLAW_SYNC_GROUP_CASCADE", "1")
        app = create_api(runtime)
        admin = TestClient(app)
        assert (
            admin.post(
                "/api/auth/bootstrap",
                json={"email": "admin@x.com", "password": "pw", "name": "Admin"},
            ).status_code
            == 200
        )
        from olano.users.store import get_user_store

        uid = admin.post(
            "/api/admin/users", json={"email": "bob@x.com", "password": "pw", "name": "bob"}
        ).json()["user"]["id"]
        for agent in ("alpha", "beta"):
            get_user_store().db.grant_access(uid, agent, role="member")
        enable_autonomous()
        gid = admin.post(
            "/api/groups", json={"name": "Admin's", "members": ["alpha", "beta"]}
        ).json()["group"]["id"]

        bob = TestClient(app)
        assert (
            bob.post("/api/auth/login", json={"email": "bob@x.com", "password": "pw"}).status_code
            == 200
        )
        assert bob.get(f"/api/groups/{gid}/cascade").status_code == 404
        assert bob.post(f"/api/groups/{gid}/stop").status_code == 404

    def test_a_revoked_grant_stops_the_cascade_and_keeps_what_was_said(self, runtime, monkeypatch):
        """The mid-cascade case: bob may talk to alpha, alpha hands off to
        beta, and bob's grant on beta is gone by then."""
        monkeypatch.setenv("LIONCLAW_SYNC_GROUP_CASCADE", "1")
        app = create_api(runtime)
        admin = TestClient(app)
        admin.post(
            "/api/auth/bootstrap",
            json={"email": "admin@x.com", "password": "pw", "name": "Admin"},
        )
        from olano.users.store import get_user_store

        db = get_user_store().db
        uid = admin.post(
            "/api/admin/users", json={"email": "bob@x.com", "password": "pw", "name": "bob"}
        ).json()["user"]["id"]
        for agent in ("alpha", "beta"):
            db.grant_access(uid, agent, role="member")
        enable_autonomous()
        bob = TestClient(app)
        bob.post("/api/auth/login", json={"email": "bob@x.com", "password": "pw"})
        gid = bob.post("/api/groups", json={"name": "Ops", "members": ["alpha", "beta"]}).json()[
            "group"
        ]["id"]

        # alpha answers and hands to beta — but bob's access to beta is revoked
        # while alpha is mid-turn.
        async def _hand_off(*a, **k):
            db.revoke_access(uid, "beta")
            return "on it, @beta take the numbers"

        runtime._runners["alpha"].ainvoke = AsyncMock(side_effect=_hand_off)
        resp = bob.post(f"/api/groups/{gid}/messages", json={"text": "go"})
        assert resp.status_code == 200
        assert resp.json()["cascade"]["reason"] == ACCESS_DENIED
        assert runtime._runners["beta"].ainvoke.await_count == 0
        rows = bob.get(f"/api/groups/{gid}/messages").json()["messages"]
        assert [r["author_kind"] for r in rows] == ["human", "agent", "system"]
        assert "no longer have access" in rows[-1]["body"]


# ── delivery: the cascade runs behind the response ───────────────────────────


class TestBackgroundDelivery:
    """The production path: the POST answers the human and the exchange keeps
    running behind it."""

    async def _await_settled(self, group_id: int, *, timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
            if cascade_mod.REGISTRY.get(group_id) is None:
                return
        raise AssertionError("cascade never finished")

    async def _client(self, runtime):
        import httpx

        transport = httpx.ASGITransport(app=create_api(runtime))
        return httpx.AsyncClient(transport=transport, base_url="http://t")

    async def test_the_post_returns_before_the_cascade_finishes(self, runtime):
        enable_autonomous()
        gate = asyncio.Event()

        async def _beta(*_a, **_k) -> str:
            await gate.wait()
            return "numbers are fine"

        runtime._runners["beta"].ainvoke = AsyncMock(side_effect=_beta)
        async with await self._client(runtime) as client:
            gid = (
                await client.post("/api/groups", json={"name": "Ops", "members": ["alpha", "beta"]})
            ).json()["group"]["id"]
            resp = await client.post(f"/api/groups/{gid}/messages", json={"text": "morning"})
            assert resp.json()["cascade"] == {"running": True, "hops": 0, "max_hops": 3}
            # The human already has their answer while beta is still thinking.
            rows = (await client.get(f"/api/groups/{gid}/messages")).json()["messages"]
            assert [r["author"] for r in rows] == ["human-owner", "alpha"]
            assert (await client.get(f"/api/groups/{gid}/cascade")).json()["cascade"]["running"]

            gate.set()
            await self._await_settled(gid)
            # ...and a client that only polls ends up with the whole transcript.
            rows = (await client.get(f"/api/groups/{gid}/messages")).json()["messages"]
            assert [r["author"] for r in rows] == ["human-owner", "alpha", "beta"]

    async def test_a_human_can_cut_a_running_exchange_off(self, runtime):
        enable_autonomous()
        started = asyncio.Event()

        async def _beta(*_a, **_k) -> str:
            started.set()
            await asyncio.sleep(30)
            return "never"

        runtime._runners["beta"].ainvoke = AsyncMock(side_effect=_beta)
        async with await self._client(runtime) as client:
            gid = (
                await client.post("/api/groups", json={"name": "Ops", "members": ["alpha", "beta"]})
            ).json()["group"]["id"]
            await client.post(f"/api/groups/{gid}/messages", json={"text": "morning"})
            await asyncio.wait_for(started.wait(), timeout=5)

            stop = await client.post(f"/api/groups/{gid}/stop")
            assert stop.json() == {"ok": True, "stopped": True}
            await self._await_settled(gid)
            rows = (await client.get(f"/api/groups/{gid}/messages")).json()["messages"]
            assert [r["author_kind"] for r in rows] == ["human", "agent", "system"]
            assert "a human ended" in rows[-1]["body"]

    async def test_two_human_messages_do_not_braid_two_exchanges(self, runtime):
        enable_autonomous()
        started = asyncio.Event()

        async def _beta(*_a, **_k) -> str:
            started.set()
            await asyncio.sleep(30)
            return "never"

        runtime._runners["beta"].ainvoke = AsyncMock(side_effect=_beta)
        async with await self._client(runtime) as client:
            gid = (
                await client.post("/api/groups", json={"name": "Ops", "members": ["alpha", "beta"]})
            ).json()["group"]["id"]
            await client.post(f"/api/groups/{gid}/messages", json={"text": "one"})
            await asyncio.wait_for(started.wait(), timeout=5)
            second = await client.post(f"/api/groups/{gid}/messages", json={"text": "two"})
            # The second message is still answered — only its follow-on turns
            # are declined while the first exchange is in flight.
            assert second.json()["cascade"] == {"running": False, "reason": "busy"}
            assert second.json()["messages"][1]["author"] == "alpha"
            await client.post(f"/api/groups/{gid}/stop")
            await self._await_settled(gid)
