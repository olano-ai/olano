"""The group toolkit: agents as first-class participants in group chats.

What must hold, and is pinned here:

- Gated exactly like the HTTP surface: every tool refuses while
  ``groups.enabled`` is off, naming the switch. (The switch now defaults on,
  so the refusal is what an operator who turned it off gets — the toolkit must
  never quietly work anyway.)
- Membership is the permission to speak — an agent sees and posts to only the
  groups it belongs to, and the author is stamped from the ``current_agent``
  contextvar, never from a parameter.
- A bot post follows the same autonomy contract as a human post: with
  ``groups.autonomous`` off the message just sits; with it on the bounded
  cascade runs the addressed member's turn.
- Follow-on turns run on the **creator's** authority: a creator who lost a
  grant, was deactivated or deleted stops the next hop.
- Thread ids match the human path's namespacing, so agent-initiated and
  human-initiated turns land in one conversation per member, not two.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from olano.config.home import get_olano_paths, write_home_config_text
from olano.groups.cascade import REGISTRY
from olano.groups.store import GroupError, get_group_store
from olano.tools import talk_to
from olano.tools.group_tools import (
    _creator_access_check,
    _thread_id_for,
    group_create,
    group_list,
    group_read,
    group_send,
)


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


@pytest.fixture(autouse=True)
def _as_alpha():
    """Bind the acting agent and leave both contextvar and registry clean."""
    token = talk_to.current_agent.set("alpha")
    REGISTRY.reset()
    yield
    talk_to.current_agent.reset(token)
    REGISTRY.reset()
    talk_to._RUNTIME = None


def _group(members=("alpha", "beta"), name="Standup", created_by="owner") -> dict:
    return get_group_store().create(name=name, members=list(members), created_by=created_by)


# ── the gate ────────────────────────────────────────────────────────────────


def test_every_tool_refuses_while_the_feature_is_off():
    """Turned off, the toolkit still exists — and every entry point says so
    by naming the switch, rather than answering as if it had no groups."""
    disable_groups()
    for invoke in (
        lambda: group_list.func(),
        lambda: group_read.func("1"),
        lambda: group_send.func("1", "hello"),
        lambda: group_create.func("War room", "alpha, beta"),
    ):
        out = invoke()
        assert "groups.enabled" in out, out


# ── visibility ──────────────────────────────────────────────────────────────


def test_list_shows_only_groups_the_agent_belongs_to():
    enable_groups()
    _group(("alpha", "beta"), name="Mine")
    _group(("beta", "gamma"), name="NotMine")
    out = group_list.func()
    assert "Mine" in out and "NotMine" not in out


def test_read_refuses_a_group_the_agent_is_not_in():
    """Non-membership reads exactly like non-existence — listing what an
    agent cannot reach would leak the deployment's group names."""
    enable_groups()
    other = _group(("beta", "gamma"), name="Secret")
    out = group_read.func(str(other["id"]))
    assert "[tool-error]" in out and "Secret" not in out


def test_read_formats_attribution():
    enable_groups()
    g = _group()
    store = get_group_store()
    store.add_message(g["id"], author="owner", author_kind="human", body="hi", addressee="alpha")
    store.add_message(g["id"], author="alpha", author_kind="agent", body="hello")
    out = group_read.func(str(g["id"]))
    assert "[human] owner -> alpha: hi" in out
    assert "[agent] alpha: hello" in out


# ── posting ─────────────────────────────────────────────────────────────────


def test_send_persists_as_the_acting_agent():
    enable_groups()
    g = _group()
    out = group_send.func(str(g["id"]), "status: pipeline drafted")
    assert "Posted to" in out
    msgs = get_group_store().messages(g["id"])
    assert msgs[-1]["author"] == "alpha"
    assert msgs[-1]["author_kind"] == "agent"


def test_send_accepts_the_group_name_too():
    enable_groups()
    _group(name="Warroom")
    assert "Posted to" in group_send.func("Warroom", "ping")


def test_send_refuses_outsiders_addressees_self_and_empty():
    enable_groups()
    g = _group(("alpha", "beta"))
    outside = _group(("beta", "gamma"), name="Elsewhere")
    assert "[tool-error]" in group_send.func(str(outside["id"]), "hi")
    assert "[tool-error]" in group_send.func(str(g["id"]), "hi", to="gamma")
    assert "cannot address yourself" in group_send.func(str(g["id"]), "hi", to="alpha")
    assert "[tool-error]" in group_send.func(str(g["id"]), "   ")


def test_send_without_autonomy_says_nobody_replies():
    """The message lands; the tool tells the model no turns will follow, so it
    does not sit waiting for an answer that cannot come."""
    enable_groups(autonomous=False)
    g = _group()
    out = group_send.func(str(g["id"]), "@beta can you take this?")
    assert "Posted to" in out
    assert "not reply on their own" in out


# ── the cascade, agent-initiated ────────────────────────────────────────────


def _bind_runtime(*names: str) -> MagicMock:
    rt = MagicMock()
    runners = {}
    for n in names:
        r = MagicMock()
        r.config = MagicMock(model="anthropic:claude-haiku-4-5-20251001")
        r.ainvoke = AsyncMock(return_value=f"{n} here")
        runners[n] = r
    rt._runners = runners
    talk_to._RUNTIME = rt
    return rt


async def test_send_with_autonomy_runs_the_addressed_members_turn():
    enable_groups(autonomous=True, max_hops=2)
    g = _group(("alpha", "beta"))
    rt = _bind_runtime("alpha", "beta")
    out = group_send.func(str(g["id"]), "@beta take the pipeline half")
    assert "@beta will take the next turn" in out
    state = REGISTRY.get(g["id"])
    assert state is not None and state.task is not None
    await state.task
    msgs = get_group_store().messages(g["id"])
    assert msgs[-1]["author"] == "beta"
    assert rt._runners["beta"].ainvoke.await_count == 1


async def test_agent_and_human_turns_share_one_thread_per_member():
    """The fork-prevention property: the thread id the tool-initiated cascade
    hands each member equals the one the human path would."""
    enable_groups(autonomous=True)
    g = _group(("alpha", "beta"))
    _bind_runtime("alpha", "beta")
    group_send.func(str(g["id"]), "@beta go")
    state = REGISTRY.get(g["id"])
    await state.task
    called_thread = _bind_thread(g)
    kwargs = talk_to._RUNTIME._runners["beta"].ainvoke.await_args.kwargs
    assert kwargs.get("thread_id") == called_thread


def _bind_thread(g: dict) -> str:
    # Single-user in tests: the human path's namespace_thread_id is a no-op,
    # so the expected id is the bare group thread.
    return f"group-{g['id']}-beta"


async def test_second_send_during_a_cascade_does_not_start_another():
    enable_groups(autonomous=True, max_hops=1)
    g = _group(("alpha", "beta"))
    _bind_runtime("alpha", "beta")

    # Occupy the group with a running cascade by hand.
    from olano.groups.cascade import CascadeLimits

    state = REGISTRY.begin(g["id"], limits=CascadeLimits(max_hops=1, timeout_seconds=30))
    try:
        out = group_send.func(str(g["id"]), "@beta also this")
        assert "already running" in out
    finally:
        REGISTRY.finish(state)


def test_no_bound_runtime_posts_but_says_no_turn_can_run():
    enable_groups(autonomous=True)
    g = _group(("alpha", "beta"))
    talk_to._RUNTIME = None
    out = group_send.func(str(g["id"]), "@beta go")
    assert "Posted to" in out and "no member turn can run here" in out
    assert get_group_store().messages(g["id"])[-1]["author"] == "alpha"


# ── the creator's authority ─────────────────────────────────────────────────


def test_creator_check_owner_is_implicit_admin():
    _creator_access_check("owner")("anything")  # must not raise


def test_creator_check_stops_on_deleted_or_revoked_creator(monkeypatch):
    from olano.runtime import auth as auth_layer

    monkeypatch.setattr(auth_layer, "multi_user_enabled", lambda: True)

    from olano.users.store import get_user_store, reset_user_store

    reset_user_store()
    store = get_user_store()
    member = store.create_user(email="m@x.com", password="pw", name="Mem")

    check = _creator_access_check(member.id)
    with pytest.raises(GroupError, match="may no longer reach"):
        check("beta")  # no grant

    store.db.grant_access(member.id, "beta", role="member", granted_by="t")
    check("beta")  # granted → passes

    store.db.delete_user(member.id)
    with pytest.raises(GroupError, match="gone"):
        check("beta")


def test_thread_ids_carry_the_creators_namespace(monkeypatch):
    from olano.runtime import auth as auth_layer

    monkeypatch.setattr(auth_layer, "multi_user_enabled", lambda: True)
    tid = _thread_id_for({"id": 7, "created_by": "user-123"})
    assert tid("beta") == auth_layer._thread_prefix("user-123") + "group-7-beta"
    # The owner's groups stay bare, matching namespace_thread_id's no-op.
    tid_owner = _thread_id_for({"id": 7, "created_by": "owner"})
    assert tid_owner("beta") == "group-7-beta"


# ── creating ────────────────────────────────────────────────────────────────


def test_create_in_single_user_belongs_to_the_owner():
    enable_groups()
    out = group_create.func("Launch pod", "alpha, beta")
    assert "Created group" in out
    groups = get_group_store().list_groups()
    assert groups and groups[-1]["created_by"] == "owner"


def test_create_refuses_without_a_human(monkeypatch):
    enable_groups()
    from olano.runtime import auth as auth_layer

    monkeypatch.setattr(auth_layer, "current_user", lambda: None)
    out = group_create.func("Ghost", "alpha, beta")
    assert "[tool-error]" in out and "nobody" in out
    assert all(g["name"] != "Ghost" for g in get_group_store().list_groups())


def test_create_enforces_the_creators_reach(monkeypatch):
    enable_groups()
    from olano.runtime import auth as auth_layer
    from olano.users.store import get_user_store, reset_user_store

    reset_user_store()
    store = get_user_store()
    member = store.create_user(email="m@x.com", password="pw", name="Mem")
    store.db.grant_access(member.id, "alpha", role="member", granted_by="t")
    monkeypatch.setattr(auth_layer, "current_user", lambda: member)

    out = group_create.func("Mixed", "alpha, beta")
    assert "[tool-error]" in out and "beta" in out

    store.db.grant_access(member.id, "beta", role="member", granted_by="t")
    out = group_create.func("Mixed", "alpha, beta")
    assert "Created group" in out
    assert get_group_store().list_groups(created_by=member.id)


def test_create_enforces_roster_bounds():
    enable_groups()
    out = group_create.func("Solo", "alpha")
    assert "[tool-error]" in out


# ── review regressions: scheduling from a worker thread, and never bricking ──


async def test_send_from_a_worker_thread_schedules_on_the_runtime_loop():
    """Production shape: a sync @tool runs in an executor thread with no loop.

    The first version called asyncio.get_running_loop() there, so the cascade
    NEVER ran outside async tests — and, worse, the RuntimeError path had
    already claimed the group in the registry and never released it, so every
    future cascade (human path included) answered "busy" until restart.
    """
    import asyncio

    from olano.platforms.delivery import get_channel_registry

    enable_groups(autonomous=True, max_hops=2)
    g = _group(("alpha", "beta"))
    rt = _bind_runtime("alpha", "beta")
    registry = get_channel_registry()
    registry._loop = asyncio.get_running_loop()
    try:
        out = await asyncio.to_thread(group_send.func, str(g["id"]), "@beta take it")
        assert "@beta will take the next turn" in out
        state = REGISTRY.get(g["id"])
        assert state is not None and state.task is not None
        # run_coroutine_threadsafe hands back a concurrent Future; wait for the
        # scheduled cascade to land on this loop.
        for _ in range(200):
            if state.task.done():
                break
            await asyncio.sleep(0.02)
        assert state.task.done()
        assert get_group_store().messages(g["id"])[-1]["author"] == "beta"
        assert rt._runners["beta"].ainvoke.await_count == 1
    finally:
        registry._loop = None


async def test_send_with_no_loop_anywhere_posts_and_releases_the_group():
    """No loop in the thread, none on the registry: the message must still
    post, the tool must say no turn can run, and the group's cascade slot
    must NOT be left claimed."""
    import asyncio

    from olano.groups.cascade import CascadeLimits
    from olano.platforms.delivery import get_channel_registry

    enable_groups(autonomous=True)
    g = _group(("alpha", "beta"))
    _bind_runtime("alpha", "beta")
    assert get_channel_registry().loop is None
    out = await asyncio.to_thread(group_send.func, str(g["id"]), "@beta go")
    assert "Posted to" in out and "no member turn can run" in out
    assert get_group_store().messages(g["id"])[-1]["author"] == "alpha"
    # The load-bearing half: the slot is free, a later cascade can claim it.
    state = REGISTRY.begin(g["id"], limits=CascadeLimits(max_hops=1, timeout_seconds=30))
    assert state is not None
    REGISTRY.finish(state)


def test_group_updated_pokes_carry_no_author(monkeypatch):
    """The shared-bus invariant: content-free means no member names either."""
    enable_groups()
    g = _group()
    seen: list[dict] = []

    import olano.runtime.groups_routes as routes

    monkeypatch.setattr(routes, "_publish", lambda gid, kind, **extra: seen.append(extra))
    group_send.func(str(g["id"]), "hello there")
    assert seen and all("author" not in extra for extra in seen)
