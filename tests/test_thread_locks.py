"""The per-thread lock writers and the checkpoint compactor share
(``core/thread_locks.py``): a turn holds it, the compactor only tries it.
"""

from __future__ import annotations

import asyncio

import pytest

from olano.core import thread_locks


@pytest.fixture(autouse=True)
def _clean_registry():
    thread_locks._reset_for_tests()
    yield
    thread_locks._reset_for_tests()


async def test_try_acquire_fails_while_a_turn_holds_the_thread() -> None:
    hold = await thread_locks.acquire("a::t1")
    assert thread_locks.is_held("a::t1")
    assert thread_locks.try_acquire("a::t1") is None
    # Another thread is unaffected.
    other = thread_locks.try_acquire("a::t2")
    assert other is not None
    other.release()
    hold.release()
    assert not thread_locks.is_held("a::t1")
    again = thread_locks.try_acquire("a::t1")
    assert again is not None
    again.release()
    # Nothing lingers once every holder is gone.
    assert thread_locks._REGISTRY == {}


async def test_a_turn_arriving_during_a_publish_waits_for_it() -> None:
    """The compactor holds the lock for its publish; a turn that arrives
    meanwhile waits and then runs, and the hand-off never lets a try_acquire
    in between steal the lock."""
    publish = thread_locks.try_acquire("a::t1")
    assert publish is not None
    order: list[str] = []

    async def turn() -> None:
        async with await thread_locks.acquire("a::t1"):
            order.append("turn")

    task = asyncio.create_task(turn())
    await asyncio.sleep(0.01)
    assert order == []  # still waiting on the publish
    assert thread_locks.try_acquire("a::t1") is None
    publish.release()
    # Handed straight to the waiter: nothing can take it in between.
    assert thread_locks.is_held("a::t1")
    assert thread_locks.try_acquire("a::t1") is None
    await task
    assert order == ["turn"]
    assert not thread_locks.is_held("a::t1")


async def test_release_is_idempotent_and_a_cancelled_waiter_leaves_cleanly() -> None:
    hold = await thread_locks.acquire("a::t1")

    async def waiter() -> None:
        await thread_locks.acquire("a::t1")

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    hold.release()
    hold.release()  # a second release is a no-op, never a double hand-off
    assert thread_locks._REGISTRY == {}


async def test_the_runner_holds_the_lock_for_the_whole_turn(base_config, mock_agent) -> None:
    """Pinned at the runner: the lock is taken before the impl runs and
    released in its finally, under the agent-scoped thread id the checkpoint
    rows carry."""
    from olano.core.checkpoints import scope_thread
    from olano.core.runner import AgentRunner

    runner = AgentRunner(mock_agent, base_config)
    seen: list[bool] = []

    async def fake_impl(*_a, **_k):
        seen.append(thread_locks.is_held(scope_thread(base_config.name, "t-lock")))
        yield ("text", "hi")

    runner._astream_events_impl = fake_impl  # type: ignore[method-assign]
    events = [ev async for ev in runner.astream_events("hello", thread_id="t-lock")]
    assert events and seen == [True]
    assert not thread_locks.is_held(scope_thread(base_config.name, "t-lock"))
