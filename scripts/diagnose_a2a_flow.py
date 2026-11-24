"""End-to-end diagnostic for a2a + subagent + polling behaviour.

Runnable from the repo root with::

    uv run python scripts/diagnose_a2a_flow.py

The script exercises three concrete scenarios with mocked runners so we can
observe the contract that `talk_to_agent` provides — without booting real
LLMs or LangGraph state. Each scenario prints a "PASS" / "FAIL" verdict
followed by enough trace to debug a mismatch.

Scenarios
─────────
1. **Synchronous a2a chain with a subagent**
   anthropologist → kevin → TradingAgent (kevin's internal subagent) →
   kevin (formulates final reply) → anthropologist.
   Verifies: kevin's reply to anthropologist actually contains the
   subagent's output (i.e. kevin waits for the subagent and incorporates
   its result instead of replying early with "working on it").

2. **Polling pattern with wait_n_seconds**
   anthropologist asks kevin to do work, kevin says "check in 5s",
   anthropologist waits non-blockingly, then asks again.
   Verifies: `wait_n_seconds` is async-friendly (other coroutines make
   progress) AND the second call to kevin gets the fresh status (NOT
   blocked by the 30s idempotency cooldown — because the message is
   different).

3. **Async callback after the wait**
   While anthropologist is waiting, kevin (from a "scheduled" simulated
   run) calls anthropologist with a status update. Verifies that the
   callback routes onto anthropologist's user thread via the pair-map
   set up in step 2 — and that `thread_message` SSE events fire so the
   dashboard chat panel renders the live update.

The script does NOT use pytest so you can rerun it during exploratory
debugging and edit it freely. The accompanying unit tests in
`tests/test_talk_to.py` cover the same primitives at the contract level.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from olano.security.request_context import push_request_context
from olano.tools import talk_to as tt
from olano.tools.time_tools import wait_n_seconds

# ─── fake runtime plumbing ─────────────────────────────────────────────────


@dataclass
class _FakeCfg:
    name: str
    description: str = ""
    emoji: str = "🦁"
    tools: list[str] = field(default_factory=list)


class _FakeRunner:
    """Minimal `runner.ainvoke` stub. Each runner has a `responder` callable
    that gets `(message, thread_id)` and returns the reply string. This lets
    us simulate complex behaviours (e.g. kevin calls a subagent and waits)
    without spinning up real LangGraph state."""

    def __init__(self, name: str, responder: Any) -> None:
        self.config = _FakeCfg(name=name, description=f"peer:{name}", tools=["talk_to_agent"])
        self.invocations: list[tuple[str, str | None]] = []
        self._responder = responder

    async def ainvoke(self, message: str, thread_id: str | None = None) -> str:
        self.invocations.append((message, thread_id))
        result = self._responder(message, thread_id)
        if asyncio.iscoroutine(result):
            result = await result
        return result


class _FakeRuntime:
    def __init__(self, runners: dict[str, _FakeRunner]) -> None:
        self._runners = runners


# ─── shared test helpers ───────────────────────────────────────────────────


_events: list[tuple[str, dict]] = []


def _publisher(event_type: str, payload: dict) -> None:
    _events.append((event_type, payload))


def _reset_state() -> None:
    """Clear all module-level a2a state between scenarios."""
    tt._RUNTIME = None
    tt._PUBLISHER = None
    tt._pair_links.clear()
    tt._inflight.clear()
    tt._completed.clear()
    tt.current_agent.set("")
    _events.clear()


def _verdict(label: str, ok: bool) -> None:
    print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {label}")


# ─── scenario 1: sync chain with subagent ──────────────────────────────────


async def scenario_1_sync_subagent() -> None:
    """anthropologist → kevin → TradingAgent → kevin → anthropologist.

    Kevin's responder simulates calling a subagent (TradingAgent) by
    awaiting an inner coroutine — this is what `task(subagent_type=...)`
    looks like at the I/O boundary: the parent's ainvoke blocks until the
    subagent finishes and the result is incorporated into the parent's
    reply.
    """
    print("\nSCENARIO 1 — synchronous a2a chain with subagent")
    print("─" * 64)
    _reset_state()

    async def _trading_agent() -> str:
        # Simulate a 200ms broker round-trip.
        await asyncio.sleep(0.2)
        return (
            "Order accepted: BUY 1 TSLA market, order_id=ORD-001, status=filled, fill_price=$245.10"
        )

    async def kevin_responder(message: str, _thread_id: str | None) -> str:
        # Real kevin would call task(subagent_type="TradingAgent", ...).
        # We mirror that by awaiting the subagent coroutine directly — the
        # contract we care about is that kevin DOES wait and incorporates
        # the result before returning to the caller.
        trade_result = await _trading_agent()
        return (
            f"Done. I asked the TradingAgent to place the order and they "
            f"reported back: {trade_result}"
        )

    async def passthrough(
        _msg: str, _tid: str | None
    ) -> str:  # unused, anthropologist isn't invoked
        return "anthropologist-doesnt-self-invoke"

    runtime = _FakeRuntime(
        {
            "kevin": _FakeRunner("kevin", kevin_responder),
            "academic-anthropologist": _FakeRunner("academic-anthropologist", passthrough),
        }
    )
    tt.bind_runtime(runtime, publisher=_publisher)

    tt.current_agent.set("academic-anthropologist")
    USER_THREAD = "user-thread-scenario-1"
    with push_request_context(source="api", session_id=USER_THREAD):
        t0 = time.monotonic()
        reply = await tt.talk_to_agent.ainvoke(
            {
                "agent_name": "kevin",
                "message": "Place 1 TSLA market order via the TradingAgent",
            }
        )
        elapsed = time.monotonic() - t0

    print(f"  anthropologist saw (elapsed {elapsed * 1000:.0f}ms):")
    print(f"    {reply!r}")
    _verdict(
        "tool returns ONLY the peer's reply (no wrap header)",
        not reply.startswith("Message sent to") and not reply.startswith("Sent to"),
    )
    _verdict(
        "kevin's reply contains the subagent's trade result",
        "ORD-001" in reply and "fill_price=$245.10" in reply,
    )
    _verdict(
        "kevin's reply also describes WHO did the work",
        "TradingAgent" in reply,
    )
    _verdict(
        "kevin ran on the isolated acp:: thread (sync mode), not on the user thread",
        runtime._runners["kevin"].invocations[0][1] == "acp::kevin",
    )
    _verdict(
        "kevin actually waited for the subagent (>= ~200ms)",
        elapsed >= 0.18,  # small slack for scheduler jitter
    )
    _verdict(
        "pair-map remembered the route for any future callback",
        tt._recall_pair_thread("kevin", "academic-anthropologist") == USER_THREAD,
    )


# ─── scenario 2: polling pattern with wait_n_seconds ───────────────────────


async def scenario_2_polling() -> None:
    """anthropologist asks kevin; kevin says 'check back in 2s'; anthropologist
    waits non-blockingly; anthropologist asks again with a *different*
    message so the idempotency cache does not collapse the second call."""
    print("\nSCENARIO 2 — polling pattern with wait_n_seconds")
    print("─" * 64)
    _reset_state()

    submitted_at = {"ts": 0.0}

    def kevin_responder(message: str, _thread_id: str | None) -> str:
        if "place" in message.lower():
            submitted_at["ts"] = time.monotonic()
            return (
                "Submitted to broker. Confirmation lag ~1-2s. "
                "Call back with 'status?' once you've waited."
            )
        if "status" in message.lower():
            wait_elapsed = time.monotonic() - submitted_at["ts"]
            return (
                f"Filled. order_id=ORD-002, fill at $245.30 "
                f"(confirmation arrived after {wait_elapsed:.2f}s)"
            )
        return "I didn't understand that."

    async def noop(_msg: str, _tid: str | None) -> str:
        return ""

    runtime = _FakeRuntime(
        {
            "kevin": _FakeRunner("kevin", kevin_responder),
            "academic-anthropologist": _FakeRunner("academic-anthropologist", noop),
        }
    )
    tt.bind_runtime(runtime, publisher=_publisher)

    tt.current_agent.set("academic-anthropologist")
    USER_THREAD = "user-thread-scenario-2"

    async def _other_coroutine() -> int:
        """Sentinel running alongside the wait to prove the event loop
        wasn't blocked — this should tick rapidly while anthropologist
        waits."""
        ticks = 0
        try:
            while True:
                await asyncio.sleep(0.05)
                ticks += 1
        except asyncio.CancelledError:
            return ticks
        return ticks

    with push_request_context(source="api", session_id=USER_THREAD):
        # 1. Submit
        reply_1 = await tt.talk_to_agent.ainvoke(
            {
                "agent_name": "kevin",
                "message": "Place 1 TSLA market order, paper account",
            }
        )
        print(f"  [step 1] kevin:  {reply_1!r}")

        # 2. Wait (with a sentinel running in parallel to confirm non-blocking)
        sentinel = asyncio.create_task(_other_coroutine())
        wait_result = await wait_n_seconds.ainvoke({"seconds": 2, "reason": "broker fill"})
        sentinel.cancel()
        ticks = await sentinel
        print(f"  [step 2] wait:   {wait_result!r}  (parallel sentinel ticked {ticks}×)")

        # 3. Poll
        reply_2 = await tt.talk_to_agent.ainvoke(
            {
                "agent_name": "kevin",
                "message": "status?",
            }
        )
        print(f"  [step 3] kevin:  {reply_2!r}")

    _verdict(
        "first kevin call returned the 'submitted, check back' pattern",
        "Submitted" in reply_1,
    )
    _verdict(
        "wait_n_seconds did NOT block the event loop (sentinel ticked >= 20×)",
        ticks >= 20,
    )
    _verdict(
        "second kevin call was NOT collapsed by the idempotency cache",
        "Filled" in reply_2,
    )
    _verdict(
        "kevin was actually invoked twice (different messages, different dedup keys)",
        len(runtime._runners["kevin"].invocations) == 2,
    )


# ─── scenario 3: async callback during the wait ────────────────────────────


async def scenario_3_async_callback() -> None:
    """While anthropologist is waiting, kevin (simulating a scheduled run)
    proactively calls anthropologist with a status update. Verifies that
    the callback routes onto anthropologist's user thread thanks to the
    pair-map established in step 1, and that the dashboard receives the
    right `thread_message` SSE events to render it live."""
    print("\nSCENARIO 3 — async callback while caller waits")
    print("─" * 64)
    _reset_state()

    def kevin_responder(message: str, _thread_id: str | None) -> str:
        return "Submitted. I'll ping you when the broker confirms."

    anthr_called_with: list[tuple[str, str | None]] = []

    def anthr_responder(message: str, thread_id: str | None) -> str:
        anthr_called_with.append((message, thread_id))
        return "Thanks kevin — relaying that to the user."

    runtime = _FakeRuntime(
        {
            "kevin": _FakeRunner("kevin", kevin_responder),
            "academic-anthropologist": _FakeRunner("academic-anthropologist", anthr_responder),
        }
    )
    tt.bind_runtime(runtime, publisher=_publisher)
    USER_THREAD = "user-thread-scenario-3"

    # Step 1: anthropologist on her user thread asks kevin.
    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id=USER_THREAD):
        first = await tt.talk_to_agent.ainvoke(
            {
                "agent_name": "kevin",
                "message": "Place 1 TSLA order, will wait for callback",
            }
        )

    async def kevins_scheduled_callback() -> str:
        """Kevin's deferred work fires from a SCHEDULED context — note the
        request context source/session id, which is what proves we have
        no user-thread visibility on the caller side and must rely on
        the pair-map to route home."""
        tt.current_agent.set("kevin")
        with push_request_context(source="scheduled", session_id="scheduled_kevin_abc"):
            return await tt.talk_to_agent.ainvoke(
                {
                    "agent_name": "academic-anthropologist",
                    "message": "Filled. order_id=ORD-003, fill at $245.05",
                }
            )

    # Step 2: kick off kevin's scheduled callback concurrently with a wait.
    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id=USER_THREAD):
        callback_task = asyncio.create_task(kevins_scheduled_callback())
        wait_result = await wait_n_seconds.ainvoke({"seconds": 1, "reason": "give kevin time"})
        callback_reply = await callback_task

    print(f"  [step 1] sync kevin reply:  {first!r}")
    print(f"  [step 2] wait result:       {wait_result!r}")
    print(f"  [step 3] kevin's callback was answered with: {callback_reply!r}")
    print(f"  [step 3] anthropologist invoked with:        {anthr_called_with}")

    _verdict(
        "anthropologist was invoked on her ORIGINAL user thread (pair-map recall)",
        anthr_called_with and anthr_called_with[0][1] == USER_THREAD,
    )

    thread_msgs = [p for et, p in _events if et == "thread_message"]
    _verdict(
        "two thread_message SSE events fired for the callback (peer_inbound + assistant)",
        len(thread_msgs) == 2 and {m["role"] for m in thread_msgs} == {"peer_inbound", "assistant"},
    )
    _verdict(
        "thread_message events target the anthropologist's user thread",
        all(
            m["thread_id"] == USER_THREAD and m["agent"] == "academic-anthropologist"
            for m in thread_msgs
        ),
    )


# ─── runner ────────────────────────────────────────────────────────────────


async def main() -> None:
    print("=" * 64)
    print("Olano a2a flow diagnostic")
    print("=" * 64)
    await scenario_1_sync_subagent()
    await scenario_2_polling()
    await scenario_3_async_callback()
    print("\n" + "=" * 64)
    print("Done. ✅ = expected behaviour, ❌ = regression to investigate.")
    print("Re-run after touching olano/tools/talk_to.py to confirm contracts hold.")


if __name__ == "__main__":
    asyncio.run(main())
