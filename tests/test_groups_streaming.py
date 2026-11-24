"""The streaming group turn: words as they are written, one shared transcript.

The blocking POST made a group turn the one conversation in the product where
you watch a spinner instead of words arriving. The stream endpoint fixes that
without forking the rules: both handlers run the same ``_begin_group_turn`` /
``_finish_group_turn``, so validation, addressing, access and persistence
order cannot drift apart.

Pinned here:
- the frame order a client can rely on: start → text… → message → cascade → done
- the human turn is persisted before the model runs, and survives its failure
- a mid-stream model error becomes an ``error`` frame, never a half transcript
- an empty reply persists nothing and says so
- the endpoint 404s while ``groups.enabled`` is off, like every group route
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from olano.config.home import get_olano_paths, write_home_config_text
from olano.groups.store import get_group_store
from olano.runtime.api import create_api


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


def _streaming_runner(name: str, words: list[str] | None = None, raises: bool = False):
    r = MagicMock()
    r.config = MagicMock(model="anthropic:claude-haiku-4-5-20251001")
    r.ainvoke = AsyncMock(return_value=f"{name} here")

    async def astream_events(_prompt, **_kw):
        for w in words if words is not None else [f"{name} ", "reporting ", "in"]:
            yield ("text", w)
        if raises:
            raise RuntimeError("model fell over")
        yield ("tool_call", {"name": "noise"})  # must be dropped, not forwarded

    r.astream_events = astream_events
    return r


def _interleaving_runner(name: str, tail: str = "and here is the answer"):
    """A turn shaped like a real one: words, a tool, more words.

    The paragraph break is already in the text the runner yields (``_take_text``
    inserts it when text resumes after a tool boundary), so the route only has
    to record WHERE the break is — which is what ``segments`` carries.
    """
    r = MagicMock()
    r.config = MagicMock(model="anthropic:claude-haiku-4-5-20251001")
    r.ainvoke = AsyncMock(return_value=f"{name} here")

    async def astream_events(_prompt, **_kw):
        yield ("text", "Let me check.")
        yield ("tool_call", {"id": "call-1", "name": "read_file", "args": "{}"})
        yield ("tool_result", {"id": "call-1", "name": "read_file", "content": "ok"})
        yield ("text", "\n\n")
        yield ("text", tail)

    r.astream_events = astream_events
    return r


@pytest.fixture
def runtime():
    rt = MagicMock()
    rt._runners = {"alpha": _streaming_runner("alpha"), "beta": _streaming_runner("beta")}
    rt.registry = MagicMock()
    rt.registry.get.return_value = None
    return rt


@pytest.fixture
def client(runtime):
    return TestClient(create_api(runtime))


def _make_group(client, members=("alpha", "beta")) -> int:
    resp = client.post("/api/groups", json={"name": "Standup", "members": list(members)})
    assert resp.status_code == 200, resp.text
    return resp.json()["group"]["id"]


def _frames(response) -> list[dict]:
    out = []
    for line in response.iter_lines():
        line = line.decode() if isinstance(line, bytes) else line
        if line.startswith("data:"):
            out.append(json.loads(line[5:].strip()))
    return out


def test_stream_404s_while_the_feature_is_off(client):
    resp = client.post("/api/groups/1/messages/stream", json={"text": "hi"})
    assert resp.status_code == 404


def test_stream_frame_order_and_persistence(client):
    _write_groups_block({"enabled": True})
    gid = _make_group(client)
    with client.stream(
        "POST", f"/api/groups/{gid}/messages/stream", json={"text": "@alpha status?"}
    ) as resp:
        assert resp.status_code == 200
        frames = _frames(resp)

    kinds = [f["type"] for f in frames]
    assert kinds[0] == "start"
    assert kinds[1] == "speaker"  # every turn opens by naming who is talking
    assert kinds[-1] == "done"
    assert "message" in kinds and "cascade" in kinds
    assert kinds.index("message") < kinds.index("cascade")
    # Between the speaker frame and the reply there is the member's work: text
    # deltas plus the tool activity a group exists to make visible. Nothing
    # else from the runner (usage, approvals, thread switches) leaks in.
    #
    # `media` is the one non-runner kind allowed here: like `message` and
    # `cascade` it is written by the ROUTE, not forwarded from the stream, so
    # it is not what `STREAMED_EVENT_KINDS` filters.
    for k in kinds[2 : kinds.index("message")]:
        assert k in ("text", "tool_call", "tool_result", "subagent_text", "media")
    assert "tool_call" in kinds  # the runner yields one; it must reach the client

    start = frames[0]
    assert start["addressee"] == "alpha"
    assert start["addressees"] == ["alpha"]
    assert start["message"]["author_kind"] == "human"
    # Every frame after start names its author — a fan-out puts two members'
    # words in one stream, and an unattributed delta lands in the wrong bubble.
    for f in frames[1:]:
        if f["type"] in ("speaker", "text", "tool_call", "tool_result", "subagent_text", "media"):
            assert f["author"] == "alpha"

    joined = "".join(f["text"] for f in frames if f["type"] == "text")
    msg = next(f for f in frames if f["type"] == "message")
    assert msg["message"]["body"] == joined.strip() == "alpha reporting in"
    assert msg["message"]["author"] == "alpha"

    rows = get_group_store().messages(gid)
    assert [r["author_kind"] for r in rows] == ["human", "agent"]
    assert rows[-1]["body"] == "alpha reporting in"


def test_stream_model_error_keeps_the_human_turn(client, runtime):
    _write_groups_block({"enabled": True})
    runtime._runners["alpha"] = _streaming_runner("alpha", words=["half "], raises=True)
    gid = _make_group(client)
    with client.stream(
        "POST", f"/api/groups/{gid}/messages/stream", json={"text": "@alpha go"}
    ) as resp:
        frames = _frames(resp)
    kinds = [f["type"] for f in frames]
    assert "error" in kinds and kinds[-1] == "done"
    assert "message" not in kinds
    rows = get_group_store().messages(gid)
    # The half-written reply is NOT persisted — a transcript holds what was
    # said, never what almost was. The human message survives, and the crash
    # leaves a system line: the group is shared state, and a silence nobody
    # else can account for is worse than a short explanation.
    assert [r["author_kind"] for r in rows] == ["human", "system"]
    assert "alpha" in rows[-1]["body"] and "could not answer" in rows[-1]["body"]


def test_stream_empty_reply_persists_nothing(client, runtime):
    _write_groups_block({"enabled": True})
    runtime._runners["alpha"] = _streaming_runner("alpha", words=[])
    gid = _make_group(client)
    with client.stream(
        "POST", f"/api/groups/{gid}/messages/stream", json={"text": "@alpha go"}
    ) as resp:
        frames = _frames(resp)
    assert any(f["type"] == "error" and "no reply" in f["detail"] for f in frames)
    assert [r["author_kind"] for r in get_group_store().messages(gid)] == ["human"]


def test_stream_validation_fails_before_any_stream(client):
    """Refusals that precede the human turn stay ordinary HTTP errors —
    a client must be able to tell 'refused' from 'failed mid-reply'."""
    _write_groups_block({"enabled": True})
    gid = _make_group(client)
    resp = client.post(f"/api/groups/{gid}/messages/stream", json={"text": ""})
    assert resp.status_code == 400
    resp = client.post(f"/api/groups/{gid}/messages/stream", json={"text": "hi", "to": "gamma"})
    assert resp.status_code in (400, 404)
    assert get_group_store().messages(gid) == []


# ── review regressions on the blocking endpoint and thread identity ──────────


def test_blocking_post_answers_an_empty_reply_with_502_not_500(client, runtime):
    """An empty reply used to reach store.add_message(body=''), whose
    GroupError escaped the handler as an unhandled 500. The human message
    must survive either way."""
    _write_groups_block({"enabled": True})
    runtime._runners["alpha"].ainvoke = AsyncMock(return_value="   ")
    gid = _make_group(client)
    resp = client.post(f"/api/groups/{gid}/messages", json={"text": "@alpha go"})
    assert resp.status_code == 502
    assert "produced no reply" in resp.text
    assert [r["author_kind"] for r in get_group_store().messages(gid)] == ["human"]


def test_thread_ids_bind_to_the_creator_not_the_poster(monkeypatch):
    """The fork bug: an admin posting into a member's group used to run the
    agent on a u:<admin>:-prefixed thread — a fresh checkpoint with none of
    the conversation. The creator is the group's one stable identity."""
    from olano.runtime import auth as auth_layer
    from olano.runtime.groups_routes import group_thread_id

    monkeypatch.setattr(auth_layer, "multi_user_enabled", lambda: True)
    member_group = {"id": 9, "created_by": "user-123"}
    assert group_thread_id(member_group, "merli") == (
        auth_layer._thread_prefix("user-123") + "group-9-merli"
    )
    # The owner's groups stay bare, matching single-user behaviour.
    assert group_thread_id({"id": 9, "created_by": "owner"}, "merli") == "group-9-merli"


def test_viewer_relative_mine_stamps(client):
    """`mine` is stamped per request: the poster's own turns and nobody
    else's. Single-user here, so the one human's rows are all mine — the
    multi-user half is the label logic, which is pure and client-side."""
    _write_groups_block({"enabled": True})
    gid = _make_group(client)
    client.post(f"/api/groups/{gid}/messages", json={"text": "@alpha hello"})
    rows = client.get(f"/api/groups/{gid}/messages").json()["messages"]
    human = [r for r in rows if r["author_kind"] == "human"]
    agents = [r for r in rows if r["author_kind"] == "agent"]
    assert human and all(r["mine"] is True for r in human)
    assert agents and all(r["mine"] is False for r in agents)


def test_both_group_turn_paths_bind_agent_admin(client, runtime):
    """The self_config ownership gate reads AGENT_ADMIN_KEY from the turn
    context. The stream generator runs after Starlette's middleware has
    exited (same as /api/chat/stream), so it would otherwise see the unbound
    default context — whose local-operator fallback the gate trusts. Both
    endpoints must bind the answer resolved in _begin_group_turn; single-user
    here, so the implicit owner reads True — the point is a real bool, never
    None (unbound)."""
    from olano.security.request_context import context_agent_admin

    _write_groups_block({"enabled": True})
    seen: dict = {}

    r = MagicMock()
    r.config = MagicMock(model="fake:instant")

    async def _ainvoke(_prompt, **_kw) -> str:
        seen["blocking"] = context_agent_admin()
        return "alpha here"

    async def _astream(_prompt, **_kw):
        seen["stream"] = context_agent_admin()
        yield ("text", "alpha here")

    r.ainvoke = _ainvoke
    r.astream_events = _astream
    runtime._runners["alpha"] = r

    gid = _make_group(client)
    client.post(f"/api/groups/{gid}/messages", json={"text": "@alpha one"})
    with client.stream(
        "POST", f"/api/groups/{gid}/messages/stream", json={"text": "@alpha two"}
    ) as resp:
        assert resp.status_code == 200
        _frames(resp)

    assert seen["blocking"] is True
    assert seen["stream"] is True


# ── the turn as it happened: spans, and the files it named ───────────────────


def test_a_turn_that_interleaves_persists_its_spans(client, runtime):
    """A reply split by a tool call carries the split with it, live and on
    reload, so the tool card lands between the two paragraphs instead of above
    a glued one.

    The invariant the renderer depends on is the join: the spans must reproduce
    the body. ``buildTurnBlocks`` silently drops to the old stacked layout when
    they disagree, with no error anywhere, so a route that normalized one side
    and not the other would turn interleaving off with nothing failing.
    """
    _write_groups_block({"enabled": True})
    runtime._runners["alpha"] = _interleaving_runner("alpha")
    gid = _make_group(client)
    with client.stream(
        "POST", f"/api/groups/{gid}/messages/stream", json={"text": "@alpha check"}
    ) as resp:
        assert resp.status_code == 200
        frames = _frames(resp)

    msg = next(f for f in frames if f["type"] == "message")["message"]
    assert msg["body"] == "Let me check.\n\nand here is the answer"
    assert msg["segments"] == [
        {"text": "Let me check.", "after": None},
        {"text": "\n\nand here is the answer", "after": "call-1"},
    ]
    assert "".join(s["text"] for s in msg["segments"]) == msg["body"]

    # …and the reload says the same thing, which is the half the store exists
    # for: the client discards everything not persisted on its post-send refetch.
    row = client.get(f"/api/groups/{gid}/messages").json()["messages"][-1]
    assert row["segments"] == msg["segments"]


def test_a_turn_with_no_tools_stores_no_spans(client):
    """One span is the pre-segment layout, which the renderer handles by its
    absence — so an ordinary reply's row stays exactly what earlier releases
    wrote."""
    _write_groups_block({"enabled": True})
    gid = _make_group(client)
    with client.stream(
        "POST", f"/api/groups/{gid}/messages/stream", json={"text": "@alpha hi"}
    ) as resp:
        assert resp.status_code == 200
        frames = _frames(resp)

    msg = next(f for f in frames if f["type"] == "message")["message"]
    assert msg["body"] == "alpha reporting in"
    assert "segments" not in msg


def test_files_a_member_names_are_resolved_against_that_member_workspace(
    client, runtime, monkeypatch, tmp_path
):
    """A group has one workspace root per MEMBER.

    ``detect_media_refs`` stamps ``files_url``/``download_url`` with the agent it
    is handed, so resolving every row against one root would serve beta's file
    out of alpha's workspace — and offer a card for a path that does not exist
    there. Both the live frame and the reload go through the row's own author.
    """
    alpha_ws = tmp_path / "alpha"
    beta_ws = tmp_path / "beta"
    for ws in (alpha_ws, beta_ws):
        (ws / "reports").mkdir(parents=True)
    (alpha_ws / "reports" / "q3.png").write_bytes(b"\x89PNG")

    monkeypatch.setattr(
        "olano.runtime.api._workspace_root",
        lambda agent=None: alpha_ws if agent == "alpha" else beta_ws,
    )
    runtime._runners["alpha"] = _interleaving_runner("alpha", tail="Saved: reports/q3.png")
    runtime._runners["beta"] = _interleaving_runner("beta", tail="Saved: reports/q3.png")

    _write_groups_block({"enabled": True})
    gid = _make_group(client)
    with client.stream(
        "POST", f"/api/groups/{gid}/messages/stream", json={"text": "@alpha make it"}
    ) as resp:
        assert resp.status_code == 200
        frames = _frames(resp)

    kinds = [f["type"] for f in frames]
    # The turn-end frame lands BEFORE `message` — that is what puts the file on
    # the bubble the client is still streaming into rather than on the next one.
    assert "media" in kinds
    assert kinds.index("media") < kinds.index("message")
    media = next(f for f in frames if f["type"] == "media")
    assert media["author"] == "alpha"
    assert [i["name"] for i in media["items"]] == ["q3.png"]
    assert "agent=alpha" in media["items"][0]["files_url"]

    # Beta names the same relative path, and has no such file in ITS workspace.
    with client.stream(
        "POST", f"/api/groups/{gid}/messages/stream", json={"text": "@beta you too"}
    ) as resp:
        assert resp.status_code == 200
        beta_frames = _frames(resp)
    assert not [f for f in beta_frames if f["type"] == "media"]

    rows = client.get(f"/api/groups/{gid}/messages").json()["messages"]
    by_author = {r["author"]: r for r in rows if r["author_kind"] == "agent"}
    assert [i["name"] for i in by_author["alpha"]["media"]] == ["q3.png"]
    assert "media" not in by_author["beta"]


def test_media_detection_never_breaks_a_turn(client, runtime, monkeypatch):
    """A reply is delivered whether or not its files could be resolved. The
    workspace lookup parses the agent's config and touches the disk; neither is
    worth losing a turn over."""

    def boom(*_a, **_k):
        raise RuntimeError("no workspace")

    monkeypatch.setattr("olano.runtime.api._workspace_root", boom)
    _write_groups_block({"enabled": True})
    runtime._runners["alpha"] = _interleaving_runner("alpha", tail="Saved: reports/q3.png")
    gid = _make_group(client)
    with client.stream(
        "POST", f"/api/groups/{gid}/messages/stream", json={"text": "@alpha go"}
    ) as resp:
        assert resp.status_code == 200
        frames = _frames(resp)

    msg = next(f for f in frames if f["type"] == "message")["message"]
    assert msg["body"].endswith("Saved: reports/q3.png")
    assert not [f for f in frames if f["type"] == "media"]
    assert client.get(f"/api/groups/{gid}/messages").json()["messages"][-1]["body"] == msg["body"]


# ── detached runs: a group turn outlives the socket that started it ──────────
# A group transcript is shared state — several people may be watching one
# exchange — so a viewer dropping out must never cost the turn. See
# olano/core/runs.py.


@pytest.fixture(autouse=True)
def _clean_run_registry():
    from olano.core.runs import REGISTRY

    REGISTRY.reset()
    yield
    REGISTRY.reset()


def test_group_stream_frames_are_numbered(client):
    """`seq` is what a reconnecting viewer resumes from."""
    _write_groups_block({"enabled": True})
    gid = _make_group(client)
    with client.stream(
        "POST", f"/api/groups/{gid}/messages/stream", json={"text": "@alpha status?"}
    ) as resp:
        frames = _frames(resp)
    assert [f["seq"] for f in frames] == list(range(1, len(frames) + 1))


def test_a_finished_group_turn_can_be_attached_and_replayed(client):
    """Someone who joined the group mid-exchange, or came back from a dropped
    connection, gets what they missed rather than a 404."""
    _write_groups_block({"enabled": True})
    gid = _make_group(client)
    with client.stream(
        "POST", f"/api/groups/{gid}/messages/stream", json={"text": "@alpha status?"}
    ) as resp:
        frames = _frames(resp)

    with client.stream("GET", f"/api/groups/{gid}/messages/attach") as resp:
        assert resp.status_code == 200
        replayed = _frames(resp)
    assert [f["type"] for f in replayed] == [f["type"] for f in frames]


def test_group_attach_from_a_cursor_returns_only_the_tail(client):
    _write_groups_block({"enabled": True})
    gid = _make_group(client)
    with client.stream(
        "POST", f"/api/groups/{gid}/messages/stream", json={"text": "@alpha status?"}
    ) as resp:
        frames = _frames(resp)

    with client.stream("GET", f"/api/groups/{gid}/messages/attach?cursor=2") as resp:
        tail = _frames(resp)
    assert [f["seq"] for f in tail] == [f["seq"] for f in frames[2:]]


def test_group_attach_404s_when_no_run_exists(client):
    _write_groups_block({"enabled": True})
    gid = _make_group(client)
    assert client.get(f"/api/groups/{gid}/messages/attach").status_code == 404
