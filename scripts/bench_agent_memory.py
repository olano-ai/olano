"""Benchmark olano's *memory* footprint — per agent, idle vs. processing.

The sibling of ``scripts/bench_agent_turn.py``: that one answers "how long does
a turn take", this one answers "how many agents fit on the box". It walks the
same production path (load config → ``build_agent_async`` → ``AgentRunner``)
and reports RSS at every boundary, so the three numbers an operator actually
needs come out separated:

* **baseline** — interpreter + the olano import graph, paid **once** per
  process no matter how many agents run
* **per-agent build** — the marginal cost of the Nth agent (prompt, tool
  objects, compiled graph, backend, MCP toolset)
* **idle → processing → settled** — what a turn adds on top of a built agent,
  what it gives back, and what it *retains*

Retention is the number that decides density. A turn whose peak is large but
returns to the idle line costs nothing at rest; a turn that leaves 20 MB behind
costs that much on every thread, forever.

    uv run python scripts/bench_agent_memory.py --agents 3 --turns 3
    uv run python scripts/bench_agent_memory.py --agents 1 --turns 5 --tracemalloc
    uv run python scripts/bench_agent_memory.py --agents 1 --mcp --json out.json

``--agents N`` synthesizes N identical agents in a throwaway home so the Nth
build is comparable to the first. ``--archetype agentfather`` profiles the real
heavyweight instead. Defaults to the ``fake:`` model so it runs with no API key
and the turn measures olano's own allocation rather than a provider's.

Sampling note: the peak during a turn is taken by a 5 ms poller thread reading
``/proc/self/status``, because RSS is a kernel-side number that no Python hook
observes. ``--tracemalloc`` adds Python-level attribution (which *module*
allocated it) at roughly a 2x slowdown, so it is off by default and the RSS
figures in a ``--tracemalloc`` run are not comparable to a plain one.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import gc
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

MB = 1024.0 * 1024.0


# ---------------------------------------------------------------------------
# Measurement primitives
# ---------------------------------------------------------------------------


def _read_status() -> dict[str, int]:
    """VmRSS/VmHWM/VmSize in bytes from /proc/self/status (kB in the file)."""
    out: dict[str, int] = {}
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("VmRSS", "VmHWM", "VmSize", "VmData", "RssAnon", "RssFile", "RssShmem"):
                    out[key] = int(rest.split()[0]) * 1024
    except OSError:
        pass
    return out


def rss() -> int:
    return _read_status().get("VmRSS", 0)


def _smaps_rollup() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open("/proc/self/smaps_rollup") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("Pss", "Private_Clean", "Private_Dirty", "Shared_Clean", "Shared_Dirty"):
                    out[key] = int(rest.split()[0]) * 1024
    except OSError:
        pass
    return out


def malloc_trim() -> bool:
    """Ask glibc to return free arena pages to the kernel.

    Python's allocator hands freed blocks back to malloc, but glibc keeps them
    in per-thread arenas: RSS stays high after a turn even when nothing is
    reachable. Trimming separates "retained because it is still referenced"
    from "retained because the allocator has not given it back" — a distinction
    that decides whether a fix belongs in olano or in the malloc tuning.
    """
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
        return True
    except Exception:
        return False


def settle(*, trim: bool = True) -> int:
    """Collect garbage (and optionally trim) so a reading reflects live data."""
    gc.collect()
    gc.collect()
    if trim:
        malloc_trim()
    return rss()


class PeakSampler:
    """Poll RSS on a background thread — the kernel number no hook reports."""

    def __init__(self, interval: float = 0.005) -> None:
        self.interval = interval
        self.peak = 0
        self.samples = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> PeakSampler:
        self.peak = rss()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            cur = rss()
            if cur > self.peak:
                self.peak = cur
            self.samples += 1
            self._stop.wait(self.interval)

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        cur = rss()
        if cur > self.peak:
            self.peak = cur


class ImportAttribution:
    """Charge RSS growth to the module whose import caused it.

    The first agent build is dominated by *lazy imports* — the tool registry,
    the provider, deepagents' middleware stack — and tracemalloc reports that
    as one undifferentiated pile under ``importlib._bootstrap_external``. The
    question an operator needs answered is which library, so this wraps
    ``_find_and_load`` (called exactly once per module) and keeps a stack:
    a module's *self* cost is its total minus everything it imported in turn,
    so a package and its dependencies are never double-counted.

    RSS is the right meter here rather than tracemalloc: a C extension's arenas
    and an mmapped ONNX weight file never appear in tracemalloc at all, and
    those are exactly the ones worth finding.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.records: dict[str, dict[str, float]] = {}
        self._stack: list[list[int]] = []
        self._orig: Any = None

    def __enter__(self) -> ImportAttribution:
        if not self.enabled:
            return self
        import importlib._bootstrap as boot

        self._orig = boot._find_and_load  # type: ignore[attr-defined]
        orig = self._orig

        def traced(name: str, import_: Any) -> Any:
            before = rss()
            frame = [0]  # bytes attributed to children
            self._stack.append(frame)
            try:
                return orig(name, import_)
            finally:
                self._stack.pop()
                total = rss() - before
                own = total - frame[0]
                if self._stack:
                    self._stack[-1][0] += total
                rec = self.records.setdefault(name, {"self": 0.0, "total": 0.0})
                rec["self"] += own
                rec["total"] += total

        boot._find_and_load = traced  # type: ignore[attr-defined]
        return self

    def __exit__(self, *exc: object) -> None:
        if not self.enabled:
            return
        import importlib._bootstrap as boot

        if self._orig is not None:
            boot._find_and_load = self._orig  # type: ignore[attr-defined]

    def by_root_package(self, limit: int = 25) -> list[dict[str, Any]]:
        """Fold per-module self-costs up to their top-level distribution."""
        roots: dict[str, dict[str, float]] = {}
        for name, rec in self.records.items():
            root = name.split(".")[0]
            r = roots.setdefault(root, {"self_mb": 0.0, "modules": 0})
            r["self_mb"] += rec["self"]
            r["modules"] += 1
        rows: list[dict[str, Any]] = [
            {
                "package": k,
                "rss_mb": round(v["self_mb"] / MB, 2),
                "modules": int(v["modules"]),
            }
            for k, v in roots.items()
        ]
        rows.sort(key=lambda r: float(r["rss_mb"]), reverse=True)
        return [r for r in rows if float(r["rss_mb"]) >= 0.05][:limit]


class Timeline:
    """Ordered RSS marks, each reported as a delta from the previous one."""

    def __init__(self) -> None:
        self.marks: list[tuple[str, int]] = []

    def mark(self, label: str, *, value: int | None = None) -> int:
        v = rss() if value is None else value
        self.marks.append((label, v))
        return v

    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        prev = None
        for label, v in self.marks:
            rows.append(
                {
                    "label": label,
                    "rss_mb": round(v / MB, 2),
                    "delta_mb": round((v - prev) / MB, 2) if prev is not None else 0.0,
                }
            )
            prev = v
        return rows

    def render(self) -> str:
        width = max((len(r["label"]) for r in self.rows()), default=10)  # type: ignore[arg-type]
        lines = [f"{'phase'.ljust(width)}   {'RSS MB':>9}  {'delta MB':>9}"]
        lines.append("-" * (width + 24))
        for r in self.rows():
            lines.append(
                f"{str(r['label']).ljust(width)}   {r['rss_mb']:>9.2f}  {r['delta_mb']:>+9.2f}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Throwaway agent fleet
# ---------------------------------------------------------------------------

# search.parallel.ai is the one directory entry with auth: none — a real MCP
# toolset (network fetch, JSON schema → tool objects) with no credential, which
# is what makes "how much does an MCP server cost per agent" measurable here.
PARALLEL_MCP = {
    "name": "parallel-search",
    "transport": "http",
    "url": "https://search.parallel.ai/mcp",
}

# Keyless toolkits — no API key, so they run in a sandbox unchanged. Every tool
# object is built the same way a credentialled one is, so these price the
# *binding* (import the toolkit module, construct the langchain tools, attach
# them to the graph), not the provider.
KEYLESS_TOOLKITS = [
    "web",
    "git",
    "http",
    "wikipedia",
    "arxiv",
    "pubmed",
    "ddg",
    "weather",
    "time",
    "notes",
    "fileops",
    "knowledge",
]


def child_rss_total() -> tuple[int, int]:
    """(bytes, count) of RSS held by our descendant processes.

    A stdio MCP server is a *separate process* per agent, so its memory never
    appears in this process's RSS — and on a box sized by total RAM that is the
    cost that actually limits density. Walking /proc is enough here and avoids
    a psutil dependency the runtime does not otherwise carry.
    """
    me = os.getpid()
    kids: set[int] = {me}
    rows: list[tuple[int, int, int]] = []  # (pid, ppid, rss)
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            # comm can contain spaces and parens; fields after ')' are stable.
            tail = stat[stat.rindex(")") + 2 :].split()
            ppid = int(tail[1])
            rss_pages = int(tail[21])
        except (OSError, ValueError, IndexError):
            continue
        rows.append((int(entry.name), ppid, rss_pages * os.sysconf("SC_PAGE_SIZE")))

    total = 0
    count = 0
    # Repeat until the descendant set stops growing: /proc order is arbitrary,
    # so a grandchild can be seen before its parent joins the set.
    for _ in range(6):
        grew = False
        for pid, ppid, _rss in rows:
            if ppid in kids and pid not in kids:
                kids.add(pid)
                grew = True
        if not grew:
            break
    for pid, _ppid, rss_b in rows:
        if pid in kids and pid != me:
            total += rss_b
            count += 1
    return total, count


def seed_workspace_docs(ws: Path, count: int) -> None:
    """Give the workspace real knowledge content for RAG to ingest.

    A freshly scaffolded workspace holds almost nothing, so a benchmark run
    against it never exercises the embedding path at any volume — and the
    embedding arena is the largest single allocation on the box. Seeding makes
    the boot ingest resemble a customer's box instead of an empty one.
    """
    if count <= 0:
        return
    kb = ws / "system" / "knowledge"
    kb.mkdir(parents=True, exist_ok=True)
    para = (
        "The deployment routes every request through the gateway before it reaches "
        "a provider, so a refusal on the last leg is a supply problem rather than a "
        "configuration error. Operators should check the credit balance first, then "
        "the routing table, then the provider account itself. "
    )
    for i in range(count):
        (kb / f"bench_doc_{i:03d}.md").write_text(
            f"# Benchmark document {i}\n\n" + (para * 6) + "\n"
        )


def seed_user_guide(ws: Path) -> None:
    """Put the real user guide where the scaffold puts it.

    Every provisioned workspace carries ``system/knowledge/USER_GUIDE.md`` (~257
    KB), and it is the one *large* document a default box ingests — the many
    small ones (memory logs, notes) embed a few chunks per call, which is a
    shape where batch size never binds. This is the file that decides whether
    the embedding arena is 36 MB or 500 MB on a real deployment.
    """
    src = (
        Path(__file__).resolve().parent.parent / "olano" / "archetypes" / "knowledge_user_guide.md"
    )
    dst = ws / "system" / "knowledge"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst / "USER_GUIDE.md")


def write_fleet(
    home: Path,
    count: int,
    *,
    model: str,
    with_mcp: bool,
    with_toolkits: bool,
    mcp_local_tools: int | None = None,
    seed_docs: int = 0,
    seed_guide: bool = False,
    tools: list[str] | None = None,
    prompt: str | None = None,
) -> list[str]:
    """Write `count` identical agent configs into a throwaway home."""
    names = []
    for i in range(count):
        name = f"bench-agent-{i + 1}"
        d = home / "agents" / name
        d.mkdir(parents=True, exist_ok=True)
        ws = home / "workspaces" / name
        ws.mkdir(parents=True, exist_ok=True)
        seed_workspace_docs(ws, seed_docs)
        if seed_guide:
            seed_user_guide(ws)
        cfg: dict[str, object] = {
            "name": name,
            "description": f"Memory benchmark agent {i + 1}.",
            "model": model,
            # `workspace` is what the prompt assembler, RAG ingest and skill
            # discovery read; `backend.root_dir` is only the file tools' root.
            # A real agent.json5 uses the "workspace" sentinel so they coincide
            # — pin both to the seeded dir or the ingest reads an empty default.
            "workspace": str(ws),
            "backend": {"type": "filesystem", "root_dir": "workspace"},
            "builtin_subagents": [],
            "hidden": True,
        }
        if prompt:
            cfg["system_prompt"] = prompt
        if seed_docs > 0 or seed_guide:
            # `memory.rag` defaults off, and only a RAG-enabled agent ingests
            # its workspace — seeded docs with RAG off measure nothing.
            cfg["memory"] = {"markdown": True, "rag": True}
        if tools is not None:
            cfg["tools"] = tools
        elif with_toolkits:
            cfg["tools"] = list(KEYLESS_TOOLKITS)
        servers: list[dict] = []
        if with_mcp:
            servers.append(dict(PARALLEL_MCP))
        if mcp_local_tools is not None:
            servers.append(
                {
                    "name": "bench-local",
                    # `url` is required by the schema even for stdio, where it
                    # serves as the server's identity key rather than an endpoint.
                    "url": "stdio://olano-bench-local",
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [
                        str(Path(__file__).resolve().parent / "bench_mcp_server.py"),
                        "--tools",
                        str(mcp_local_tools),
                    ],
                }
            )
        if servers:
            cfg["mcp_servers"] = servers
        (d / "agent.json5").write_text(json.dumps(cfg, indent=2))
        names.append(name)
    return names


def archetype_fleet(
    home: Path, count: int, archetype: str, model: str, *, seed_docs: int = 0
) -> list[str]:
    """Clone a real archetype template `count` times into the throwaway home.

    Profiles the shape a customer actually deploys (AgentFather carries ~60
    tools and a multi-KB prompt) rather than a synthetic minimum.
    """
    from olano.archetypes import installer

    src = installer.find_archetype_path(archetype)
    if src is None:
        raise SystemExit(f"archetype not found: {archetype}")
    from olano.config.json5_fast import loads as json5_loads

    template = json5_loads(Path(src).read_text())
    names = []
    for i in range(count):
        name = f"bench-{archetype}-{i + 1}"
        d = home / "agents" / name
        d.mkdir(parents=True, exist_ok=True)
        ws = home / "workspaces" / name
        ws.mkdir(parents=True, exist_ok=True)
        seed_workspace_docs(ws, seed_docs)
        cfg = dict(template)
        cfg["name"] = name
        cfg["model"] = model
        cfg["workspace"] = str(ws)
        cfg["backend"] = {"type": "filesystem", "root_dir": "workspace"}
        cfg["hidden"] = True
        cfg.pop("from_archetype", None)
        (d / "agent.json5").write_text(json.dumps(cfg, indent=2, default=str))
        names.append(name)
    return names


# ---------------------------------------------------------------------------
# The benchmark
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> dict[str, Any]:
    tl = Timeline()
    tl.mark("interpreter start", value=args._baseline_rss)

    import_attrib = ImportAttribution(enabled=args.attribute_imports)
    build_attrib = ImportAttribution(enabled=args.attribute_imports)

    t0 = time.perf_counter()
    with import_attrib:
        from olano.config.loader import load_agent_config
        from olano.core.builder import build_agent_async, build_agent_with_mcp
        from olano.core.runner import AgentRunner

    import_s = time.perf_counter() - t0
    tl.mark("olano imports")
    after_import = settle()
    tl.mark("  + gc/trim", value=after_import)

    home = Path(os.environ["OLANO_HOME"])
    if args.archetype:
        names = archetype_fleet(
            home, args.agents, args.archetype, args.model, seed_docs=args.seed_docs
        )
    else:
        names = write_fleet(
            home,
            args.agents,
            model=args.model,
            with_mcp=args.mcp,
            with_toolkits=args.toolkits,
            mcp_local_tools=args.mcp_local,
            seed_docs=args.seed_docs,
            seed_guide=args.seed_guide,
        )

    tracer = None
    if args.tracemalloc:
        import tracemalloc

        tracemalloc.start(args.tracemalloc_frames)
        tracer = tracemalloc
        base_snapshot = tracer.take_snapshot()

    runners: list[AgentRunner] = []
    per_agent: list[dict] = []
    build_profiles: list[dict] = []

    for idx, name in enumerate(names, start=1):
        before = settle()
        t0 = time.perf_counter()
        config = load_agent_config(name)
        cfg_s = time.perf_counter() - t0

        from olano.core import boot_profile

        t0 = time.perf_counter()
        # Only the first build is attributed: it is the one that pays for the
        # lazy import graph, and leaving the hook on would charge agent 2's
        # already-imported modules nothing while still costing the overhead.
        build_attrib.enabled = args.attribute_imports and idx == 1
        with build_attrib, PeakSampler() as sampler:
            with boot_profile.profile_boot(config.name) as prof:
                if config.mcp_servers:
                    agent = await build_agent_with_mcp(config)
                else:
                    agent = await build_agent_async(config)
        build_s = time.perf_counter() - t0
        raw_after = rss()
        tl.mark(f"build agent {idx} ({name})", value=raw_after)
        after = settle()
        tl.mark("  + gc/trim", value=after)

        child_bytes, child_count = child_rss_total()
        mcp_rows = []
        try:
            from olano.core.builder import get_mcp_summary

            for row in get_mcp_summary(name) or []:
                mcp_rows.append(
                    {
                        "status": row.get("status"),
                        "ok": row.get("ok"),
                        "tools": len(row.get("tools") or []),
                        "message": (row.get("message") or "")[:120],
                    }
                )
        except Exception:
            pass

        runners.append(AgentRunner(agent, config))
        per_agent.append(
            {
                "index": idx,
                "agent": name,
                "child_processes": child_count,
                "child_rss_total_mb": round(child_bytes / MB, 2),
                "mcp": mcp_rows,
                "config_load_s": round(cfg_s, 3),
                "build_s": round(build_s, 3),
                "rss_before_mb": round(before / MB, 2),
                "rss_after_raw_mb": round(raw_after / MB, 2),
                "rss_after_settled_mb": round(after / MB, 2),
                "build_peak_mb": round(sampler.peak / MB, 2),
                "marginal_raw_mb": round((raw_after - before) / MB, 2),
                "marginal_settled_mb": round((after - before) / MB, 2),
                "transient_freed_mb": round((raw_after - after) / MB, 2),
                "tool_count": len(getattr(config, "tools", []) or []),
            }
        )
        build_profiles.append(prof.to_dict())

    idle_rss = settle()
    tl.mark("fleet idle (all built)", value=idle_rss)
    idle_child_bytes, idle_child_count = child_rss_total()

    # ---- turns -----------------------------------------------------------
    turn_rows: list[dict] = []
    concurrent_rows: list[dict] = []
    thread_ids = [str(uuid.uuid4()) for _ in runners]

    async def drain(runner: AgentRunner, thread_id: str) -> tuple[int, int]:
        events = chars = 0
        async for etype, payload in runner.astream_events(args.message, thread_id=thread_id):
            events += 1
            if etype == "text":
                chars += len(payload)
        return events, chars

    for t in range(args.turns):
        for i, runner in enumerate(runners):
            before = rss()
            with PeakSampler() as sampler:
                chars = 0
                events = 0
                t0 = time.perf_counter()
                async for etype, payload in runner.astream_events(
                    args.message, thread_id=thread_ids[i]
                ):
                    events += 1
                    if etype == "text":
                        chars += len(payload)
                dur = time.perf_counter() - t0
            raw_after = rss()
            settled = settle()
            turn_rows.append(
                {
                    "turn": t,
                    "agent": names[i],
                    "duration_s": round(dur, 3),
                    "events": events,
                    "text_chars": chars,
                    "rss_before_mb": round(before / MB, 2),
                    "peak_mb": round(sampler.peak / MB, 2),
                    "rss_after_raw_mb": round(raw_after / MB, 2),
                    "rss_after_settled_mb": round(settled / MB, 2),
                    "peak_over_idle_mb": round((sampler.peak - before) / MB, 2),
                    "retained_mb": round((settled - before) / MB, 2),
                    "reclaimed_by_trim_mb": round((raw_after - settled) / MB, 2),
                    "samples": sampler.samples,
                }
            )
            print(
                f"  turn {t} {names[i]}: peak +{(sampler.peak - before) / MB:.1f}MB "
                f"retained {(settled - before) / MB:+.1f}MB "
                f"({dur:.2f}s, {events} events)",
                file=sys.stderr,
            )

    # ---- concurrent turns -------------------------------------------------
    # Sequential turns understate the box's requirement: a starter instance
    # sizes for the moment every agent is answering at once, and that peak is
    # not the sequential peak times one — it is per-turn state held
    # simultaneously on one heap.
    if args.concurrent and len(runners) > 1:
        for t in range(args.concurrent_rounds):
            before = settle()
            with PeakSampler() as sampler:
                t0 = time.perf_counter()
                results = await asyncio.gather(
                    *(drain(r, f"{thread_ids[i]}-conc{t}") for i, r in enumerate(runners))
                )
                dur = time.perf_counter() - t0
            raw_after = rss()
            settled = settle()
            concurrent_rows.append(
                {
                    "round": t,
                    "agents": len(runners),
                    "duration_s": round(dur, 3),
                    "events": sum(e for e, _ in results),
                    "rss_before_mb": round(before / MB, 2),
                    "peak_mb": round(sampler.peak / MB, 2),
                    "peak_over_idle_mb": round((sampler.peak - before) / MB, 2),
                    "peak_per_agent_mb": round((sampler.peak - before) / MB / len(runners), 2),
                    "retained_mb": round((settled - before) / MB, 2),
                    "reclaimed_by_trim_mb": round((raw_after - settled) / MB, 2),
                }
            )
            print(
                f"  concurrent round {t}: {len(runners)} agents at once, "
                f"peak +{(sampler.peak - before) / MB:.1f}MB "
                f"({(sampler.peak - before) / MB / len(runners):.1f}MB/agent) "
                f"retained {(settled - before) / MB:+.1f}MB in {dur:.2f}s",
                file=sys.stderr,
            )
        tl.mark("after concurrent rounds")

    final_rss = settle()
    tl.mark("after all turns (settled)", value=final_rss)

    top_allocations = []
    if tracer is not None:
        snap = tracer.take_snapshot()
        for stat in snap.compare_to(base_snapshot, "filename")[:25]:
            top_allocations.append(
                {
                    "file": stat.traceback[0].filename if stat.traceback else "?",
                    "size_mb": round(stat.size / MB, 3),
                    "delta_mb": round(stat.size_diff / MB, 3),
                    "count": stat.count,
                }
            )
        tracer.stop()

    status = _read_status()
    rollup = _smaps_rollup()

    n = len(names)
    baseline = after_import
    marginals = [a["marginal_settled_mb"] for a in per_agent]
    report: dict[str, Any] = {
        "config": {
            "agents": n,
            "turns": args.turns,
            "model": args.model,
            "archetype": args.archetype,
            "mcp": args.mcp,
            "toolkits": args.toolkits,
            "tracemalloc": args.tracemalloc,
            "python": sys.version.split()[0],
        },
        "import_s": round(import_s, 3),
        "summary": {
            "baseline_mb": round(baseline / MB, 2),
            "fleet_idle_mb": round(idle_rss / MB, 2),
            "fleet_total_over_baseline_mb": round((idle_rss - baseline) / MB, 2),
            "per_agent_first_mb": marginals[0] if marginals else 0.0,
            "per_agent_last_mb": marginals[-1] if marginals else 0.0,
            "per_agent_mean_mb": round(sum(marginals) / len(marginals), 2) if marginals else 0.0,
            "final_after_turns_mb": round(final_rss / MB, 2),
            "turn_retention_total_mb": round((final_rss - idle_rss) / MB, 2),
            "child_processes": idle_child_count,
            "child_rss_total_mb": round(idle_child_bytes / MB, 2),
            "fleet_idle_incl_children_mb": round((idle_rss + idle_child_bytes) / MB, 2),
        },
        "timeline": tl.rows(),
        "per_agent": per_agent,
        "turns": turn_rows,
        "concurrent_turns": concurrent_rows,
        "proc_status_mb": {k: round(v / MB, 2) for k, v in status.items()},
        "smaps_rollup_mb": {k: round(v / MB, 2) for k, v in rollup.items()},
        "boot_profiles": build_profiles if args.boot_profiles else [],
        "top_allocations": top_allocations,
        "imports_at_module_level": import_attrib.by_root_package(),
        "imports_during_first_build": build_attrib.by_root_package(),
    }

    # Release the fleet and re-read: what an agent *holds* vs. what the process
    # keeps is the difference between "stop an idle agent to free memory" and
    # "restarting the process is the only way back".
    runners.clear()
    released = settle()
    report["summary"]["after_release_mb"] = round(released / MB, 2)
    report["summary"]["released_by_dropping_fleet_mb"] = round((idle_rss - released) / MB, 2)

    return report


def _shutdown_background_threads() -> None:
    """Stop the non-daemon threads a booted agent leaves running.

    A built agent owns an aiosqlite connection worker and (when RAG ingested
    the workspace) a watchdog observer, neither of them daemonised — so a
    benchmark that merely finishes its measurements hangs forever at
    interpreter shutdown. The runtime server stops these on its own teardown
    path; a one-shot script has to ask.
    """
    try:
        from olano.core.builder import stop_rag_watchers

        stop_rag_watchers()
    except Exception:
        pass


def main() -> None:
    baseline_rss = rss()

    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--agents", type=int, default=1, help="how many agents to build (default 1)")
    p.add_argument("--turns", type=int, default=3, help="turns per agent (default 3)")
    p.add_argument("--model", default="fake:instant", help="model string (default fake:instant)")
    p.add_argument("--message", default="Summarize what you can do in two sentences.")
    p.add_argument("--archetype", default=None, help="clone a real archetype (e.g. agentfather)")
    p.add_argument(
        "--mcp", action="store_true", help="attach the keyless parallel-search MCP server"
    )
    p.add_argument(
        "--mcp-local",
        type=int,
        default=None,
        metavar="N",
        help="attach a local stdio MCP server advertising N tools (scripts/bench_mcp_server.py)",
    )
    p.add_argument("--toolkits", action="store_true", help="attach keyless builtin toolkits")
    p.add_argument(
        "--tracemalloc", action="store_true", help="Python-level attribution (~2x slower)"
    )
    p.add_argument(
        "--attribute-imports",
        action="store_true",
        help="charge RSS growth to the importing package (names the lazy-import cost)",
    )
    p.add_argument("--tracemalloc-frames", type=int, default=1)
    p.add_argument("--boot-profiles", action="store_true", help="include the per-agent boot tree")
    p.add_argument(
        "--concurrent",
        action="store_true",
        help="also run every agent's turn simultaneously (the peak a box must size for)",
    )
    p.add_argument("--concurrent-rounds", type=int, default=2)
    p.add_argument(
        "--seed-docs",
        type=int,
        default=0,
        metavar="N",
        help="seed N knowledge documents per workspace so the RAG ingest is realistic",
    )
    p.add_argument(
        "--seed-guide",
        action="store_true",
        help="seed the real USER_GUIDE.md (~257 KB) into each workspace, as the scaffold does",
    )
    p.add_argument("--json", default=None, help="write the full report here")
    p.add_argument("--keep-home", action="store_true", help="do not delete the throwaway home")
    args = p.parse_args()
    args._baseline_rss = baseline_rss

    home = Path(tempfile.mkdtemp(prefix="olano-membench-"))
    os.environ["OLANO_HOME"] = str(home)
    os.environ["LIONCLAW_HOME"] = str(home)
    # Keep the measurement about the agent, not about ambient services: sqlite
    # checkpoints need no server, and the background cartography walk would
    # otherwise allocate on a thread the sampler cannot attribute.
    os.environ.setdefault("LIONCLAW_CHECKPOINT_BACKEND", "sqlite")
    os.environ.setdefault("LIONCLAW_DISABLE_LOCAL_AGENTS_DIR", "1")
    os.environ.setdefault("LIONCLAW_SYNC_CARTOGRAPHY", "0")

    try:
        report = asyncio.run(run(args))
    finally:
        if not args.keep_home:
            shutil.rmtree(home, ignore_errors=True)
        _shutdown_background_threads()

    print()
    tl_rows = report["timeline"]
    width = max(len(str(r["label"])) for r in tl_rows)
    print(f"{'phase'.ljust(width)}   {'RSS MB':>9}  {'delta MB':>9}")
    print("-" * (width + 24))
    for r in tl_rows:
        print(f"{str(r['label']).ljust(width)}   {r['rss_mb']:>9.2f}  {r['delta_mb']:>+9.2f}")

    s = report["summary"]
    print()
    print(f"baseline (interpreter + imports) : {s['baseline_mb']:.1f} MB")
    print(
        f"per-agent marginal (first / last): {s['per_agent_first_mb']:.1f} / {s['per_agent_last_mb']:.1f} MB"
    )
    print(
        f"fleet idle ({report['config']['agents']} agents)          : {s['fleet_idle_mb']:.1f} MB"
    )
    if s.get("child_processes"):
        print(
            f"  + {s['child_processes']} child process(es)        : "
            f"{s['child_rss_total_mb']:.1f} MB "
            f"(total {s['fleet_idle_incl_children_mb']:.1f} MB)"
        )
    mcp_states = [m for a in report["per_agent"] for m in (a.get("mcp") or [])]
    if mcp_states:
        ok = sum(1 for m in mcp_states if m.get("ok"))
        tools = sum(m.get("tools") or 0 for m in mcp_states)
        print(f"MCP servers connected            : {ok}/{len(mcp_states)} ({tools} tools bound)")
        for m in mcp_states:
            if not m.get("ok"):
                print(f"  ! {m.get('status')}: {m.get('message')}")
    if report["turns"]:
        peaks = [t["peak_over_idle_mb"] for t in report["turns"]]
        rets = [t["retained_mb"] for t in report["turns"]]
        print(
            f"turn peak over idle (max/mean)   : {max(peaks):.1f} / {sum(peaks) / len(peaks):.1f} MB"
        )
        print(
            f"turn retained (first / later)    : {rets[0]:+.1f} / {sum(rets[1:]) / max(len(rets) - 1, 1):+.1f} MB"
        )
    print(f"retained across all turns        : {s['turn_retention_total_mb']:+.1f} MB")
    if report["concurrent_turns"]:
        c = report["concurrent_turns"][-1]
        print(
            f"concurrent peak ({c['agents']} agents at once) : "
            f"+{c['peak_over_idle_mb']:.1f} MB ({c['peak_per_agent_mb']:.1f} MB/agent)"
        )

    for title, key in (
        ("imports at module level (baseline)", "imports_at_module_level"),
        ("imports during first agent build", "imports_during_first_build"),
    ):
        rows = report.get(key) or []
        if not rows:
            continue
        print(f"\n{title} — RSS charged to the package that triggered it")
        print(f"{'package'.ljust(28)} {'RSS MB':>8} {'modules':>8}")
        print("-" * 46)
        for r in rows:
            print(f"{r['package'].ljust(28)} {r['rss_mb']:>8.2f} {r['modules']:>8}")
        print(f"{'(sum of listed)'.ljust(28)} {sum(r['rss_mb'] for r in rows):>8.2f}")

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(f"\nfull report → {args.json}")

    # Every measurement is taken and written. The aiosqlite worker thread owned
    # by the checkpointer is not daemonised and joining it is neither possible
    # from here nor interesting, so leave by the short door.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
