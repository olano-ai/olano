"""Tests for the platform gateway approval queue (olano.platforms.approval).

These exercise the contract the platform button/callback handlers rely on:
``resolve_gateway_approval(session_key, choice) -> int`` synchronously unblocks
every agent turn awaiting on that session and reports how many it freed.
"""

from __future__ import annotations

import asyncio

import pytest

from olano.platforms import approval


@pytest.fixture(autouse=True)
def _clear_registry():
    """Keep the module-global pending registry isolated per test."""
    approval._pending.clear()
    yield
    approval._pending.clear()


async def test_resolve_unblocks_waiting_future_with_choice():
    """A registered approval is resolved with the human's choice and unblocks."""
    future = approval.register_gateway_approval("telegram:42")
    assert approval.pending_gateway_approvals("telegram:42") == 1

    count = approval.resolve_gateway_approval("telegram:42", "once")

    assert count == 1
    assert future.done()
    assert await asyncio.wait_for(future, timeout=1) == "once"
    # Registry is emptied once resolved.
    assert approval.pending_gateway_approvals("telegram:42") == 0


async def test_blocking_side_round_trip():
    """Simulate the request side awaiting while a button click resolves it."""
    future = approval.register_gateway_approval("discord:7")

    async def click_after_delay():
        await asyncio.sleep(0.01)
        return approval.resolve_gateway_approval("discord:7", "always")

    resolved, choice = await asyncio.gather(
        click_after_delay(),
        asyncio.wait_for(future, timeout=1),
    )
    assert resolved == 1
    assert choice == "always"


async def test_resolve_with_nothing_pending_is_noop():
    """Clicking a button for a session with no pending approval returns 0."""
    assert approval.resolve_gateway_approval("slack:nobody", "deny") == 0


async def test_resolve_all_pending_for_session():
    """Multiple concurrent approvals on one session resolve together."""
    f1 = approval.register_gateway_approval("feishu:9")
    f2 = approval.register_gateway_approval("feishu:9")
    assert approval.pending_gateway_approvals("feishu:9") == 2

    count = approval.resolve_gateway_approval("feishu:9", "deny")

    assert count == 2
    assert await asyncio.wait_for(f1, timeout=1) == "deny"
    assert await asyncio.wait_for(f2, timeout=1) == "deny"


async def test_sessions_are_isolated():
    """Resolving one session does not touch another's pending approvals."""
    fa = approval.register_gateway_approval("telegram:a")
    fb = approval.register_gateway_approval("telegram:b")

    count = approval.resolve_gateway_approval("telegram:a", "once")

    assert count == 1
    assert fa.done()
    assert not fb.done()
    assert approval.pending_gateway_approvals("telegram:b") == 1


async def test_discard_prevents_stale_resolution():
    """A discarded (e.g. timed-out) future is not resolved by a later click."""
    future = approval.register_gateway_approval("telegram:t")
    approval.discard_gateway_approval("telegram:t", future)

    assert approval.pending_gateway_approvals("telegram:t") == 0
    assert approval.resolve_gateway_approval("telegram:t", "once") == 0
    assert not future.done()


async def test_discard_is_idempotent_and_leaves_others():
    """Discarding one future leaves the session's other pending futures intact."""
    f1 = approval.register_gateway_approval("discord:multi")
    f2 = approval.register_gateway_approval("discord:multi")

    approval.discard_gateway_approval("discord:multi", f1)
    # Second discard of the same future is a harmless no-op.
    approval.discard_gateway_approval("discord:multi", f1)

    assert approval.pending_gateway_approvals("discord:multi") == 1
    assert approval.resolve_gateway_approval("discord:multi", "once") == 1
    assert f2.done()
    assert not f1.done()


async def test_already_resolved_future_is_not_double_set():
    """A future resolved out of band is skipped (counted as not freed)."""
    future = approval.register_gateway_approval("slack:x")
    future.set_result("manual")

    count = approval.resolve_gateway_approval("slack:x", "deny")

    assert count == 0
    assert await asyncio.wait_for(future, timeout=1) == "manual"
