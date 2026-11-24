"""The Cortex index survives concurrent writers and never reads a damaged file as empty.

Five ``cortex_edit`` calls in one model turn run concurrently (LangGraph runs
parallel tool calls in executor threads). With plain ``write_text`` saves the
file tore (one document followed by the longer writer's tail), the loader
swallowed the parse error and every tool reported zero items while the file
visibly held them. These tests pin the three properties that make that
impossible: exclusive read-modify-write, atomic saves, and a loader that
recovers or refuses but never answers "empty" for a file with data in it.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from olano.cognition import cortex_items as ci
from olano.security.request_context import push_request_context
from olano.tools import cortex_tools


def _index(root: Path) -> Path:
    return ci._index_path(root)


def _seed(root: Path, n: int = 5) -> list[str]:
    return [
        ci.add_item(root, kind="idea", title=f"Idea {i}", summary="s" * 40, detail="x")["id"]
        for i in range(n)
    ]


# ───────────────────────────── concurrency ───────────────────────────────────


def test_concurrent_edits_keep_every_update_and_a_valid_file(tmp_path: Path) -> None:
    ids = _seed(tmp_path)
    barrier = threading.Barrier(len(ids))

    def edit(i: int, item_id: str) -> None:
        barrier.wait()
        ci.update_item(
            tmp_path,
            item_id,
            title=f"Edited idea {i} " + "t" * (i * 7),
            summary="summary " * (20 + i),
        )

    threads = [threading.Thread(target=edit, args=(i, iid)) for i, iid in enumerate(ids)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    raw = _index(tmp_path).read_text(encoding="utf-8")
    data = json.loads(raw)  # a torn file would raise here
    titles = sorted(it["title"] for it in data["items"])
    assert len(titles) == len(ids)
    assert all(t.startswith("Edited idea") for t in titles), titles
    assert len(ci.list_items(tmp_path)) == len(ids)
    # No temp files left beside the index.
    leftovers = [p.name for p in _index(tmp_path).parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_concurrent_adds_never_lose_an_item(tmp_path: Path) -> None:
    barrier = threading.Barrier(6)

    def add(i: int) -> None:
        barrier.wait()
        ci.add_item(tmp_path, kind="brainstorm", title=f"Topic {i}", detail="x")

    threads = [threading.Thread(target=add, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ci.list_items(tmp_path, kind="brainstorm")) == 6


def test_reader_never_sees_an_empty_index_during_a_write(tmp_path: Path) -> None:
    item_id = _seed(tmp_path, 1)[0]
    seen: list[int] = []
    barrier = threading.Barrier(2)

    def writer() -> None:
        barrier.wait()
        for i in range(20):
            ci.update_item(tmp_path, item_id, summary=("x" * 400) + str(i))

    def reader() -> None:
        barrier.wait()
        for _ in range(40):
            seen.append(len(ci.list_items(tmp_path)))

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen and min(seen) == 1


# ───────────────────────────── damaged files ─────────────────────────────────


def test_torn_index_is_recovered_behind_a_backup(tmp_path: Path) -> None:
    ids = _seed(tmp_path, 3)
    path = _index(tmp_path)
    good = path.read_text(encoding="utf-8")
    # The exact shape a torn write leaves: a complete document, then the tail
    # of a longer one.
    path.write_text(good + '\n      "links": {}\n    }\n  ]\n}', encoding="utf-8")

    items = ci.list_items(tmp_path)
    assert sorted(it["id"] for it in items) == sorted(ids)
    backups = list(path.parent.glob("items.json.corrupt-*"))
    assert len(backups) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["items"]  # rewritten clean
    # A second read is an ordinary read: no second backup.
    ci.list_items(tmp_path)
    assert len(list(path.parent.glob("items.json.corrupt-*"))) == 1


def test_unreadable_index_raises_and_is_never_overwritten(tmp_path: Path) -> None:
    _seed(tmp_path, 2)
    path = _index(tmp_path)
    path.write_text("{this is not json at all", encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(ci.CortexIndexError) as exc_info:
        ci.list_items(tmp_path)
    assert str(path) in str(exc_info.value)
    with pytest.raises(ci.CortexIndexError):
        ci.counts_by_kind(tmp_path)
    with pytest.raises(ci.CortexIndexError):
        ci.add_item(tmp_path, kind="idea", title="Must not land", detail="x")
    with pytest.raises(ci.CortexIndexError):
        ci.update_item(tmp_path, "whatever", title="nope")
    assert path.read_bytes() == before
    # The refused add created no folder either.
    assert not (tmp_path / "ideas" / "must-not-land").exists()


def test_valid_json_that_is_not_an_index_is_refused(tmp_path: Path) -> None:
    path = _index(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"id": "abc"}]), encoding="utf-8")
    with pytest.raises(ci.CortexIndexError):
        ci.list_items(tmp_path)


def test_empty_index_file_starts_fresh_with_a_backup(tmp_path: Path) -> None:
    path = _index(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    assert ci.list_items(tmp_path) == []
    assert list(path.parent.glob("items.json.corrupt-*"))
    ci.add_item(tmp_path, kind="idea", title="Fresh", detail="x")
    assert len(ci.list_items(tmp_path)) == 1


def test_missing_index_is_simply_empty(tmp_path: Path) -> None:
    assert ci.list_items(tmp_path) == []
    assert not _index(tmp_path).exists()


# ───────────────────────────── tool surface ──────────────────────────────────


def test_cortex_tools_report_an_unreadable_index_as_a_tool_error(tmp_path: Path) -> None:
    _seed(tmp_path, 1)
    _index(tmp_path).write_text("garbage", encoding="utf-8")
    with push_request_context(metadata={"workspace_root": str(tmp_path)}):
        listed = cortex_tools.cortex_list.invoke({})
        viewed = cortex_tools.cortex_view.invoke({"item_id": "abcd1234"})
        added = cortex_tools.cortex_add.invoke({"kind": "idea", "title": "x"})
    for out in (listed, viewed, added):
        assert out.startswith("[tool-error]")
        assert "unreadable" in out
    assert _index(tmp_path).read_text(encoding="utf-8") == "garbage"


async def test_async_cortex_tools_report_an_unreadable_index(tmp_path: Path) -> None:
    _index(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _index(tmp_path).write_text("garbage", encoding="utf-8")
    with push_request_context(metadata={"workspace_root": str(tmp_path)}):
        out = await cortex_tools.cortex_promote.ainvoke({"item_id": "abcd1234"})
    assert out.startswith("[tool-error]") and "unreadable" in out


# ───────────────────────────── workspace binding ─────────────────────────────


def test_tools_fall_back_to_the_bound_agents_workspace(tmp_path: Path, monkeypatch) -> None:
    """No request context (a background worker started top-level) must still
    resolve the AGENT's workspace, never the home default LIONCLAW_WORKSPACE."""
    from olano.tools.talk_to import current_agent

    home_ws = tmp_path / "home-workspace"
    agent_ws = tmp_path / "agent-workspace"
    agent_ws.mkdir()
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(home_ws))
    ci.add_item(agent_ws, kind="idea", title="Mine", detail="x")

    class _Cfg:
        workspace = str(agent_ws)

    token = current_agent.set("aurelius")
    try:
        with patch("olano.config.loader.load_agent_config", return_value=_Cfg()):
            out = cortex_tools.cortex_list.invoke({})
    finally:
        current_agent.reset(token)
    assert "Mine" in out
    assert not _index(home_ws).exists()


async def test_run_worker_binds_the_workspace_root_for_its_tools(
    base_config, tmp_path: Path, monkeypatch
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from olano.cognition.worker import run_worker
    from olano.security.request_context import get_request_context

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    seen: dict[str, str] = {}

    async def _ainvoke(*_a, **_k):
        seen["root"] = str(get_request_context().metadata.get("workspace_root"))
        msg = MagicMock()
        msg.content = "done"
        return {"messages": [msg]}

    child = MagicMock()
    child.ainvoke = AsyncMock(side_effect=_ainvoke)
    child.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        outcome = await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="Do the thing.",
            goal="bind me",
            worker_label="test",
        )
    assert outcome.status == "completed"
    assert seen["root"] == str(tmp_path)
