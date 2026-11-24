"""Benchmark olano's per-turn response performance.

Mirrors the production one-shot path (load config → build_agent_async →
AgentRunner.astream_events) and reports where the time goes:

* **import** — cost of importing the olano runtime modules
* **build** — agent construction, with the boot_profile phase tree
* **turns** — per-turn: time to first event, time to first text token,
  turn total, event counts; p50/p95 across turns

Run against the fake model to measure pure framework overhead, or a real
agent for end-to-end numbers:

    uv run python scripts/bench_agent_turn.py --agent test-agent-fake --turns 5
    uv run python scripts/bench_agent_turn.py --agent test-agent-openai --turns 3
    uv run python scripts/bench_agent_turn.py --agent test-agent-fake \
        --cprofile /tmp/turn.pstats     # cProfile the turn loop

Defaults LIONCLAW_CHECKPOINT_BACKEND=sqlite when unset so it runs anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1]


async def bench(args: argparse.Namespace) -> dict:
    t0 = time.perf_counter()
    from olano.config.loader import load_agent_config
    from olano.core import boot_profile
    from olano.core.builder import build_agent_async, build_agent_with_mcp
    from olano.core.runner import AgentRunner

    import_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    config = load_agent_config(args.agent)
    config_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    with boot_profile.profile_boot(config.name) as prof:
        if config.mcp_servers:
            agent = await build_agent_with_mcp(config)
        else:
            agent = await build_agent_async(config)
    build_s = time.perf_counter() - t0

    runner = AgentRunner(agent, config)
    thread_id = args.thread_id or str(uuid.uuid4())

    turns: list[dict] = []
    profiler = None
    if args.cprofile:
        import cProfile

        profiler = cProfile.Profile()
        profiler.enable()

    for i in range(args.turns):
        tid = str(uuid.uuid4()) if args.new_thread_per_turn else thread_id
        counts: dict[str, int] = {}
        first_event = first_text = None
        text_len = 0
        turn_t0 = time.perf_counter()
        async for etype, payload in runner.astream_events(args.message, thread_id=tid):
            now = time.perf_counter() - turn_t0
            counts[etype] = counts.get(etype, 0) + 1
            if first_event is None:
                first_event = now
            if etype == "text":
                if first_text is None:
                    first_text = now
                text_len += len(payload)
        total = time.perf_counter() - turn_t0
        turns.append(
            {
                "turn": i,
                "first_event_s": round(first_event or 0.0, 4),
                "first_text_s": round(first_text or 0.0, 4),
                "total_s": round(total, 4),
                "text_chars": text_len,
                "events": counts,
            }
        )
        print(
            f"turn {i}: first_event={first_event or 0:.3f}s "
            f"first_text={first_text or 0:.3f}s total={total:.3f}s "
            f"text={text_len}ch events={counts}",
            file=sys.stderr,
        )

    if profiler is not None:
        profiler.disable()
        profiler.dump_stats(args.cprofile)
        print(f"cProfile stats written to {args.cprofile}", file=sys.stderr)

    totals = [t["total_s"] for t in turns]
    firsts = [t["first_text_s"] for t in turns]
    report = {
        "agent": config.name,
        "model": config.model,
        "import_s": round(import_s, 3),
        "config_load_s": round(config_s, 3),
        "build_s": round(build_s, 3),
        "boot_profile": prof.to_dict(),
        "turns": turns,
        "summary": {
            "turn_total_p50_s": round(_pct(totals, 50), 3),
            "turn_total_p95_s": round(_pct(totals, 95), 3),
            "first_text_p50_s": round(_pct(firsts, 50), 3),
            "first_text_p95_s": round(_pct(firsts, 95), 3),
        },
    }
    print(prof.render(), file=sys.stderr)
    return report


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--agent", default="test-agent-fake")
    p.add_argument("--turns", type=int, default=3)
    p.add_argument("--message", default="Summarize what you can do in two sentences.")
    p.add_argument("--thread-id", default=None, help="fixed thread id (grow one thread)")
    p.add_argument(
        "--new-thread-per-turn",
        action="store_true",
        help="fresh thread per turn (measures cold-thread cost, not context growth)",
    )
    p.add_argument("--cprofile", default=None, help="write cProfile stats for the turn loop")
    p.add_argument("--json", dest="json_out", default=None, help="write full report JSON here")
    args = p.parse_args()

    os.environ.setdefault("LIONCLAW_CHECKPOINT_BACKEND", "sqlite")

    report = asyncio.run(bench(args))
    out = json.dumps(report, indent=2)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            f.write(out + "\n")
        print(f"report written to {args.json_out}", file=sys.stderr)
    print(out)

    # Background daemon threads (RAG watchers, cartography) keep the process
    # alive after the report; a benchmark should exit when it is done.
    try:
        from olano.core.builder import stop_rag_watchers

        stop_rag_watchers()
    except Exception:
        pass
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
