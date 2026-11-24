"""The SQLite A2A task store: owner scoping, persistence, list semantics, retention.

a2a-sdk's ``InMemoryTaskStore`` never evicts and forgets every task on restart;
this store is what replaces it under every exposed agent's request handler.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.types import ListTasksRequest, Message, Part, Role, Task, TaskState, TaskStatus
from a2a.utils.errors import InvalidParamsError

from olano.a2a.task_store import SqliteTaskStore, default_task_store_path


class _User(User):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._name


def _ctx(owner: str) -> ServerCallContext:
    return ServerCallContext(user=_User(owner))


def _task(
    tid: str,
    *,
    context_id: str = "ctx",
    state: int = TaskState.TASK_STATE_COMPLETED,
    ts: int | None = None,
) -> Task:
    status = TaskStatus(state=state)
    if ts is not None:
        status.timestamp.FromSeconds(ts)
    return Task(
        id=tid,
        context_id=context_id,
        status=status,
        history=[
            Message(message_id=f"m-{tid}", role=Role.ROLE_USER, parts=[Part(text=f"hello {tid}")])
        ],
    )


async def test_roundtrip_is_owner_scoped_and_survives_a_new_instance(tmp_path: Path):
    path = tmp_path / "tasks.db"
    store = SqliteTaskStore(path)
    await store.save(_task("t1"), _ctx("token-a"))

    got = await store.get("t1", _ctx("token-a"))
    assert got is not None and got.id == "t1" and got.history[0].parts[0].text == "hello t1"
    # Another caller's token never sees it — same rule as the SDK's own store.
    assert await store.get("t1", _ctx("token-b")) is None

    # Saving again is an update, not a second row.
    await store.save(_task("t1", state=TaskState.TASK_STATE_FAILED), _ctx("token-a"))
    updated = await store.get("t1", _ctx("token-a"))
    assert updated is not None and updated.status.state == TaskState.TASK_STATE_FAILED
    assert await store.count() == 1
    store.close()

    # A "restart": a fresh instance over the same file still has the task.
    again = SqliteTaskStore(path)
    found = await again.get("t1", _ctx("token-a"))
    assert found is not None and found.id == "t1"
    await again.delete("t1", _ctx("token-a"))
    assert await again.get("t1", _ctx("token-a")) is None
    await again.delete("t1", _ctx("token-a"))  # unknown id is logged, never raised
    again.close()


async def test_default_path_is_a_private_file_under_the_home():
    store = SqliteTaskStore()
    assert store.path == default_task_store_path()
    assert store.path.parent.name == "a2a" and store.path.name == "tasks.db"
    await store.save(_task("t1"), _ctx("o"))
    assert store.path.exists() and (store.path.stat().st_mode & 0o777) == 0o600
    store.close()


async def test_list_filters_sorts_and_paginates_like_the_sdk_store(tmp_path: Path):
    store = SqliteTaskStore(tmp_path / "tasks.db")
    ctx = _ctx("o")
    await store.save(_task("old", ts=1_000), ctx)
    await store.save(_task("mid", ts=2_000, context_id="other"), ctx)
    await store.save(_task("new", ts=3_000, state=TaskState.TASK_STATE_FAILED), ctx)
    await store.save(_task("no-ts"), ctx)  # no status timestamp sorts last
    await store.save(_task("theirs", ts=9_000), _ctx("someone-else"))

    page = await store.list(ListTasksRequest(page_size=2), ctx)
    assert [t.id for t in page.tasks] == ["new", "mid"]  # newest first, nobody else's
    assert page.total_size == 4 and page.page_size == 2 and page.next_page_token
    page2 = await store.list(ListTasksRequest(page_size=2, page_token=page.next_page_token), ctx)
    assert [t.id for t in page2.tasks] == ["old", "no-ts"] and not page2.next_page_token

    by_ctx = await store.list(ListTasksRequest(context_id="other"), ctx)
    assert [t.id for t in by_ctx.tasks] == ["mid"]
    failed = await store.list(ListTasksRequest(status=TaskState.TASK_STATE_FAILED), ctx)
    assert [t.id for t in failed.tasks] == ["new"]
    after = ListTasksRequest()
    after.status_timestamp_after.FromSeconds(2_000)
    assert [t.id for t in (await store.list(after, ctx)).tasks] == ["new", "mid"]

    # A page token that no longer points into the (filtered) list is refused, as in the SDK.
    with pytest.raises(InvalidParamsError):
        await store.list(ListTasksRequest(page_token=page.next_page_token, context_id="other"), ctx)
    store.close()


async def test_sweep_deletes_old_rows_and_runs_from_save_with_the_live_window(tmp_path: Path):
    path = tmp_path / "tasks.db"
    retention = {"days": 7}
    store = SqliteTaskStore(
        path, retention_days=lambda: retention["days"], sweep_interval_seconds=0
    )
    ctx = _ctx("o")
    await store.save(_task("stale"), ctx)
    await store.save(_task("fresh"), ctx)

    # Age a row by hand: the sweep keys on when the store last wrote it.
    raw = sqlite3.connect(path)
    raw.execute(
        "UPDATE tasks SET updated_at = ? WHERE task_id = 'stale'", (time.time() - 8 * 86400,)
    )
    raw.commit()
    assert await store.sweep() == 1
    assert await store.get("stale", ctx) is None
    assert await store.get("fresh", ctx) is not None

    # A save sweeps too (the interval is zero here), and the window is read live —
    # the operator shortening it in Config takes effect on the next write.
    raw.execute(
        "UPDATE tasks SET updated_at = ? WHERE task_id = 'fresh'", (time.time() - 3 * 86400,)
    )
    raw.commit()
    raw.close()
    retention["days"] = 2
    await store.save(_task("newest"), ctx)
    assert await store.get("fresh", ctx) is None
    assert await store.count() == 1
    store.close()


async def test_sweep_is_throttled_between_saves(tmp_path: Path):
    store = SqliteTaskStore(tmp_path / "tasks.db", retention_days=1, sweep_interval_seconds=3600)
    ctx = _ctx("o")
    await store.save(
        _task("a"), ctx
    )  # first write sweeps (nothing to remove) and arms the throttle
    raw = sqlite3.connect(store.path)
    raw.execute("UPDATE tasks SET updated_at = 0 WHERE task_id = 'a'")
    raw.commit()
    raw.close()
    await store.save(_task("b"), ctx)  # inside the interval: no sweep, the stale row survives
    assert await store.count() == 2
    assert await store.sweep() == 1  # an explicit sweep ignores the throttle
    store.close()
