"""Process-memory diagnostics + the live monitor toggle.

Answers the operational question "what is holding the runtime's RAM?" — the
one thing :mod:`olano.admin.diagnostics` (online/offline, hangs, MCP health)
never told you. It reports **process** metrics (RSS/VmSize/Pss/threads/uptime)
*and* a breakdown of the in-process olano structures that actually accumulate
(the tool-arg registry, per-runner hook contexts, self-learning offsets,
checkpointer caches, loaded RAG collections, a gc census), so a real leak is
distinguishable from the inherent baseline (compiled agent graphs + the
embedding model + Chroma indices).

Like :mod:`olano.admin.diagnostics` it degrades through four data sources,
best first, and never raises just because the runtime is down:

1. **runtime** — in-process (AgentFather tool / the API handler / a slash
   command run inside ``olano serve``): ``/proc/self/*`` is the daemon, plus
   the full structure breakdown + gc + tracemalloc.
2. **api** — a separate CLI process fetches the daemon's own report over HTTP.
3. **proc** — daemon up but its API is unreachable: ``/proc/<pid>/*`` metrics
   only.
4. **none** — no daemon: this process only (or nulls off-Linux).

The **monitor** (the background sampler + tracemalloc tracing) is opt-in and
**toggleable at runtime** — ``set_memory_monitor("off")`` stops the sampler and
``tracemalloc.stop()`` so the monitor's own footprint (chiefly tracemalloc's
allocation traces) drops to zero without restarting the daemon.

All values are plain JSON-serialisable; every probe is wrapped so a failure
becomes ``None`` + a note rather than an exception. Stdlib only — no psutil.
"""

from __future__ import annotations

import gc
import json
import logging
import os
import sys
import tracemalloc
from collections import Counter
from typing import Any

try:  # POSIX only; absent on Windows.
    import resource
except ImportError:  # pragma: no cover - non-POSIX
    resource = None  # type: ignore[assignment]

from olano.admin.diagnostics import _api_get_json, _get_runtime

logger = logging.getLogger(__name__)

# Cap the gc census cost — iterating every live object is O(objects); the top-N
# type histogram is all we need to spot a runaway allocation.
_DEFAULT_GC_TOP = 15
_DEFAULT_TRACEMALLOC_TOP = 15
# Default sampler cadence when the monitor is turned on without an explicit
# ``LIONCLAW_MEM_SAMPLE_SECONDS`` — light enough for a RAM-constrained box.
_DEFAULT_SAMPLE_SECONDS = 600


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


# ── the live monitor toggle ───────────────────────────────────────────────────


class MemoryMonitor:
    """Runtime on/off state for the background sampler + tracemalloc tracing.

    Held on the :class:`~olano.runtime.server.RuntimeServer` and flipped live
    (API / MCP tool / CLI / slash) so the monitor's own overhead can be shed
    without a restart. ``set_enabled(False)`` also stops tracemalloc, freeing its
    allocation-trace buffers — the whole point of a runtime toggle on a box
    where the monitor itself might be the thing eating memory.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        interval_seconds: int = 0,
        tracemalloc_enabled: bool = False,
    ) -> None:
        self.enabled = bool(enabled)
        self.interval_seconds = int(interval_seconds or 0)
        self.tracemalloc_enabled = bool(tracemalloc_enabled)
        # Last cheap sample emitted by the sampler loop (for the dashboard card).
        self._last_sample: dict[str, Any] | None = None
        if tracemalloc_enabled and not tracemalloc.is_tracing():
            try:
                tracemalloc.start()
            except Exception:  # pragma: no cover - defensive
                pass

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if self.enabled and self.interval_seconds <= 0:
            self.interval_seconds = _DEFAULT_SAMPLE_SECONDS

    def set_tracemalloc(self, enabled: bool) -> None:
        self.tracemalloc_enabled = bool(enabled)
        try:
            if enabled and not tracemalloc.is_tracing():
                tracemalloc.start()
            elif not enabled and tracemalloc.is_tracing():
                tracemalloc.stop()  # releases the trace buffers
        except Exception:  # pragma: no cover - defensive
            pass

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval_seconds,
            "tracemalloc": bool(tracemalloc.is_tracing()),
            "tracemalloc_requested": self.tracemalloc_enabled,
            "last_sample": self._last_sample,
        }


def monitor_from_env() -> MemoryMonitor:
    """Build the initial monitor from env (both knobs default off)."""
    try:
        interval = int(os.environ.get("LIONCLAW_MEM_SAMPLE_SECONDS", "0") or "0")
    except ValueError:
        interval = 0
    tm = os.environ.get("LIONCLAW_TRACEMALLOC") == "1"
    return MemoryMonitor(
        enabled=interval > 0,
        interval_seconds=max(0, interval),
        tracemalloc_enabled=tm,
    )


# ── process metrics (stdlib /proc + resource) ─────────────────────────────────


def _read_proc_status(pid: int) -> dict[str, int]:
    """Parse ``/proc/<pid>/status`` Vm*/Threads lines into ``{key: kB or count}``."""
    out: dict[str, int] = {}
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                key = key.strip()
                if key in ("VmRSS", "VmSize", "VmHWM", "VmSwap", "Threads"):
                    tok = rest.strip().split()
                    if tok and tok[0].isdigit():
                        out[key] = int(tok[0])
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return out


def _read_smaps_rollup_pss_mb(pid: int) -> float | None:
    """Proportional set size (MB) from ``/proc/<pid>/smaps_rollup``; ``None`` if
    unreadable (older kernels, permissions)."""
    try:
        with open(f"/proc/{pid}/smaps_rollup", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("Pss:"):
                    tok = line.split()
                    if len(tok) >= 2 and tok[1].isdigit():
                        return round(int(tok[1]) / 1024, 1)
    except (FileNotFoundError, PermissionError, OSError):
        return None
    return None


def _proc_uptime_seconds(pid: int) -> int | None:
    """Elapsed seconds since the process started (Linux only).

    Reuses the ``/proc/<pid>/stat`` field-22 start-ticks pattern from
    :mod:`olano.platforms.status`, combined with system uptime.
    """
    try:
        with open(f"/proc/{pid}/stat") as fh:
            start_ticks = int(fh.read().split()[21])
        clk = os.sysconf("SC_CLK_TCK")
        with open("/proc/uptime") as fh:
            system_uptime = float(fh.read().split()[0])
        return max(0, int(system_uptime - (start_ticks / clk)))
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError, AttributeError):
        return None


def _maxrss_mb() -> float | None:
    """Peak RSS of *this* process. ``ru_maxrss`` is KiB on Linux (bytes on
    macOS — noted in the report), so this is Linux-accurate."""
    if resource is None:
        return None
    try:
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:  # pragma: no cover - defensive
        return None


def _kb_to_mb(kb: int | None) -> float | None:
    return round(kb / 1024, 1) if kb is not None else None


def _process_metrics(pid: int, *, is_self: bool) -> dict[str, Any]:
    """Process memory snapshot for ``pid`` — ``None`` fields off-Linux."""
    if not _is_linux():
        return {
            "rss_mb": None,
            "vms_mb": None,
            "hwm_mb": None,
            "swap_mb": None,
            "threads": None,
            "pss_mb": None,
            "maxrss_mb": _maxrss_mb() if is_self else None,
            "uptime_seconds": None,
        }
    status = _read_proc_status(pid)
    return {
        "rss_mb": _kb_to_mb(status.get("VmRSS")),
        "vms_mb": _kb_to_mb(status.get("VmSize")),
        "hwm_mb": _kb_to_mb(status.get("VmHWM")),
        "swap_mb": _kb_to_mb(status.get("VmSwap")),
        "threads": status.get("Threads"),
        "pss_mb": _read_smaps_rollup_pss_mb(pid),
        "maxrss_mb": _maxrss_mb() if is_self else None,
        "uptime_seconds": _proc_uptime_seconds(pid),
    }


def current_rss_mb() -> float | None:
    """Cheap current RSS (MB) of *this* process — used to bracket per-agent boot
    deltas. ``None`` off-Linux."""
    if not _is_linux():
        return None
    return _kb_to_mb(_read_proc_status(os.getpid()).get("VmRSS"))


def release_free_heap() -> float | None:
    """Ask glibc to return free heap arenas to the OS; return the MB reclaimed.

    Booting N agents allocates and frees a lot of transient scaffolding (config
    parsing, tool/MCP JSON schemas, prompt assembly). CPython hands those pages
    back to glibc's allocator, but glibc keeps them mapped in its per-arena free
    lists — so RSS stays at the boot *peak* even though the memory is dead. On a
    memory-capped host that difference is the margin between swapping and not.

    ``malloc_trim(0)`` walks the arenas and ``madvise``s the free top back. It's
    a no-op on non-glibc (musl, macOS) and safe to call at any time: it only
    touches memory the allocator already considers free.

    Returns ``None`` when unavailable (non-Linux, non-glibc) or on any failure —
    callers treat it as best-effort housekeeping, never a correctness step.
    """
    if not _is_linux():
        return None
    before = current_rss_mb()
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        trim = libc.malloc_trim
    except (OSError, AttributeError):
        return None  # musl or otherwise no glibc malloc_trim
    try:
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        trim(0)
    except Exception:  # pragma: no cover - defensive
        return None
    after = current_rss_mb()
    if before is None or after is None:
        return None
    return round(max(0.0, before - after), 1)


def glibc_allocator() -> dict[str, Any]:
    """What glibc's allocator holds: in use, free but still mapped, and mmapped.

    The question the RSS alone cannot answer. tracemalloc sees only the
    allocations made through Python's allocator, so a process reading 270 MB
    traced behind 4.6 GB of RSS (a managed box, 2026-10-21) has the
    difference somewhere these counters can at least sort: ``free_mb`` is
    memory freed and still mapped in the arenas (``top_releasable_mb`` is the
    part of it at the top of the heap, a subset, never an addition), which is
    what ``malloc_trim`` and a lower ``MALLOC_ARENA_MAX`` address;
    ``allocated_mb`` is everything glibc still regards as in use, which holds
    Python's own allocator arenas (the traced bytes sit inside it), native
    buffers tracemalloc never sees, and tracemalloc's own bookkeeping
    (``tracemalloc_overhead_mb``). A large ``allocated_mb`` says the memory
    has an owner, not who it is: attribution still needs a native profiler.
    These are allocator sizes, not resident pages; allocations outside glibc
    (a custom allocator, a direct mapping) are not counted, and a free arena
    byte is not necessarily one ``malloc_trim`` can return (a live block can
    pin the page). ``mallinfo2`` sums every arena. ``None`` fields mean no
    glibc (musl, macOS); nothing here allocates, frees, collects or trims.
    """
    out: dict[str, Any] = {
        "allocated_mb": None,
        "free_mb": None,
        "top_releasable_mb": None,
        "mmapped_mb": None,
        "mmapped_regions": None,
        "tracemalloc_overhead_mb": None,
    }
    if tracemalloc.is_tracing():
        out["tracemalloc_overhead_mb"] = round(tracemalloc.get_tracemalloc_memory() / 1048576, 1)
    if not _is_linux():
        return out
    try:
        import ctypes

        class _Mallinfo2(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_size_t)
                for name in (
                    "arena",
                    "ordblks",
                    "smblks",
                    "hblks",
                    "hblkhd",
                    "usmblks",
                    "fsmblks",
                    "uordblks",
                    "fordblks",
                    "keepcost",
                )
            ]

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        fn = libc.mallinfo2
        fn.argtypes = []
        fn.restype = _Mallinfo2
        info = fn()
    except (OSError, AttributeError):
        return out  # musl or a glibc without mallinfo2 (< 2.33)
    except Exception:  # pragma: no cover - defensive
        return out
    mb = 1048576
    out["allocated_mb"] = round(info.uordblks / mb, 1)
    out["free_mb"] = round(info.fordblks / mb, 1)
    out["top_releasable_mb"] = round(info.keepcost / mb, 1)
    out["mmapped_mb"] = round(info.hblkhd / mb, 1)
    out["mmapped_regions"] = int(info.hblks)
    return out


# ── child processes (real per-tool / per-connector RSS) ───────────────────────


def _read_proc_children(pid: int) -> list[int]:
    """Direct child PIDs of *pid* (Linux), gathered across all its threads."""
    kids: list[int] = []
    task_dir = f"/proc/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except (FileNotFoundError, PermissionError, OSError):
        return kids
    for tid in tids:
        try:
            with open(f"{task_dir}/{tid}/children") as fh:
                kids.extend(int(x) for x in fh.read().split())
        except (FileNotFoundError, PermissionError, OSError, ValueError):
            continue
    return kids


def _proc_label(pid: int) -> tuple[str, str]:
    """``(comm, human label)`` for a child pid, classifying the common tool /
    connector subprocesses olano spawns."""
    comm = ""
    try:
        with open(f"/proc/{pid}/comm") as fh:
            comm = fh.read().strip()
    except OSError:
        pass
    cmdline = ""
    try:
        with open(f"/proc/{pid}/cmdline") as fh:
            cmdline = fh.read().replace("\x00", " ").strip()
    except OSError:
        pass
    hay = f"{comm} {cmdline}".lower()
    if "chrome" in hay or "chromium" in hay or "playwright" in hay:
        label = "browser (playwright)"
    elif "signal-cli" in hay or "signal" in hay:
        label = "signal-cli"
    elif "mcp" in hay:
        label = "mcp server (stdio)"
    elif "node" in comm or "npx" in hay or "uvx" in hay:
        label = "node/uvx child"
    else:
        label = comm or f"pid {pid}"
    return comm, label


def _child_processes(pid: int, *, max_depth: int = 3) -> dict[str, Any]:
    """RSS of the runtime's child processes (browser, stdio MCP, signal-cli, …).

    These are **separate PIDs**, so this RAM is *additional* to the daemon's own
    RSS — the closest thing to real per-tool memory. Descends a few levels to
    catch helper processes (chromium spawns many)."""
    if not _is_linux():
        return {"supported": False, "total_rss_mb": None, "processes": []}
    seen: set[int] = set()
    rows: list[dict[str, Any]] = []
    frontier: list[tuple[int, int]] = [(k, 1) for k in _read_proc_children(pid)]
    while frontier:
        cpid, depth = frontier.pop()
        if cpid in seen:
            continue
        seen.add(cpid)
        rss = _kb_to_mb(_read_proc_status(cpid).get("VmRSS"))
        comm, label = _proc_label(cpid)
        rows.append({"pid": cpid, "rss_mb": rss, "label": label, "comm": comm})
        if depth < max_depth:
            frontier.extend((k, depth + 1) for k in _read_proc_children(cpid))
    rows.sort(key=lambda r: r["rss_mb"] or 0.0, reverse=True)
    total = round(sum(r["rss_mb"] or 0.0 for r in rows), 1)
    return {"supported": True, "total_rss_mb": total, "processes": rows}


# ── olano structure breakdown (the leak indicators) ────────────────────────


def _safe_len(obj: Any) -> int | None:
    try:
        return len(obj)
    except Exception:
        return None


def _tool_args_registry_len() -> int | None:
    try:
        from olano.security.deepagents_middleware import _TOOL_ARGS_REGISTRY

        return len(_TOOL_ARGS_REGISTRY)
    except Exception:
        return None


def _checkpointer_cache_sizes() -> dict[str, int | None] | None:
    try:
        from olano.core.checkpoints import get_default_factory

        f = get_default_factory()
        return {
            "sync_savers": _safe_len(getattr(f, "_sync_savers", {})),
            "sync_conns": _safe_len(getattr(f, "_sync_conns", {})),
            "async_savers": _safe_len(getattr(f, "_async_savers", {})),
            "async_conns": _safe_len(getattr(f, "_async_conns", {})),
            "pg_async_savers": _safe_len(getattr(f, "_pg_async_savers", {})),
            "pg_async_pools": _safe_len(getattr(f, "_pg_async_pools", {})),
            "pg_sync_savers": _safe_len(getattr(f, "_pg_sync_savers", {})),
            "pg_sync_pools": _safe_len(getattr(f, "_pg_sync_pools", {})),
        }
    except Exception:
        return None


def _embedding_model_resident() -> bool | None:
    try:
        import olano.memory.embeddings as emb

        return bool(getattr(emb, "_encoder", None) or getattr(emb, "_lc_embeddings", None))
    except Exception:
        return None


def _rag_collection_count(store: Any) -> int | None:
    """Vector count of a langchain Chroma store, defensively (Chroma internals)."""
    try:
        col = getattr(store, "_collection", None)
        if col is not None and hasattr(col, "count"):
            return int(col.count())
    except Exception:
        return None
    return None


def _rag_info(rt: Any, *, with_counts: bool = True) -> dict[str, Any]:
    """Loaded RAG collections per agent. With ``with_counts`` (full report), each
    collection carries its **vector count** — the single most useful per-agent
    RAG signal; the periodic sampler passes ``False`` to stay cheap."""
    collections: dict[str, dict[str, int | None]] = {}
    vectors_total = 0
    for svc in getattr(rt, "_self_learning", None) or []:
        rag = getattr(svc, "rag_memory", None)
        stores = getattr(rag, "_stores", None) if rag is not None else None
        if isinstance(stores, dict):
            name = getattr(getattr(svc, "config", None), "name", "?")
            per: dict[str, int | None] = {}
            for cname, store in stores.items():
                cnt = _rag_collection_count(store) if with_counts else None
                per[cname] = cnt
                if isinstance(cnt, int):
                    vectors_total += cnt
            collections[name] = per
    return {
        "collections": collections,  # agent → {collection: vector_count | None}
        "vectors_total": vectors_total,
        "embedding_model_resident": _embedding_model_resident(),
    }


def _structure_breakdown(rt: Any, *, with_rag_counts: bool = True) -> dict[str, Any]:
    """Counts of the in-process structures that accumulate over uptime, plus each
    runner's approximate boot delta-RSS.

    Cheap (``len()`` + a cached float, no gc) so the periodic sampler can call it
    every cycle with ``with_rag_counts=False``. Counts are lower bounds where
    noted: ``config_tools`` is configured names not the expanded bound set, and
    RAG collections only cover the self-learning instances reachable from the
    runtime.
    """
    runners = getattr(rt, "_runners", None) or {}
    per_agent: dict[str, Any] = {}
    hook_total = 0
    for name, runner in runners.items():
        hc = _safe_len(getattr(runner, "_hook_contexts", {})) or 0
        hook_total += hc
        cfg = getattr(runner, "config", None)
        per_agent[name] = {
            # Approximate RSS added while building this agent's compiled graph
            # (model client + tools + MCP schemas + subagents + middleware).
            # Excludes the embedding model / Chroma index (lazy on first RAG use).
            "boot_rss_mb": getattr(runner, "_boot_rss_mb", None),
            "hook_contexts": hc,
            "override_agents": _safe_len(getattr(runner, "_override_agents", {})),
            "post_turn_pulse_tasks": _safe_len(getattr(runner, "_post_turn_pulse_tasks", {})),
            "config_tools": _safe_len(getattr(cfg, "tools", None) or []),
            "mcp_servers": _safe_len(getattr(cfg, "mcp_servers", None) or []),
        }

    self_learning: list[dict[str, Any]] = []
    offsets_total = 0
    for svc in getattr(rt, "_self_learning", None) or []:
        po = _safe_len(getattr(svc, "_processed_offsets", {})) or 0
        offsets_total += po
        failures = getattr(svc, "_failures", {}) or {}
        self_learning.append(
            {
                "agent": getattr(getattr(svc, "config", None), "name", "?"),
                "processed_offsets": po,
                "distillation_count": getattr(svc, "_distillation_count", None),
                "failures": sum(v for v in failures.values() if isinstance(v, int)),
            }
        )

    try:
        scheduler_jobs = len(rt.scheduler.list_jobs()) if getattr(rt, "scheduler", None) else 0
    except Exception:
        scheduler_jobs = None

    return {
        "tool_args_registry": _tool_args_registry_len(),
        "hook_contexts_total": hook_total,
        "processed_offsets_total": offsets_total,
        "runners_count": len(runners),
        "background_tasks": _safe_len(getattr(rt, "_background_tasks", [])),
        "blocked_agents": _safe_len(getattr(rt, "_blocked_agents", {})),
        "scheduler_jobs": scheduler_jobs,
        "per_agent": per_agent,
        "self_learning": self_learning,
        "checkpointer": _checkpointer_cache_sizes(),
        "rag": _rag_info(rt, with_counts=with_rag_counts),
    }


# ── gc + tracemalloc (opt-in / diagnostic depth) ──────────────────────────────


def _gc_census(top: int) -> dict[str, Any]:
    objs = gc.get_objects()
    counter: Counter[str] = Counter()
    for obj in objs:
        try:
            counter[type(obj).__qualname__] += 1
        except Exception:
            continue
    return {"objects_total": len(objs), "top_types": counter.most_common(top)}


def _tracemalloc_top(top: int) -> dict[str, Any]:
    if not tracemalloc.is_tracing():
        return {
            "enabled": False,
            "hint": "turn the monitor's tracemalloc on (olano mem --monitor tracemalloc-on) "
            "or boot with LIONCLAW_TRACEMALLOC=1",
        }
    current, peak = tracemalloc.get_traced_memory()
    # Tracing's own bookkeeping is not in get_traced_memory(); read it before
    # the snapshot below adds its own transient to it.
    overhead = tracemalloc.get_tracemalloc_memory()
    stats = tracemalloc.take_snapshot().statistics("lineno")[:top]
    return {
        "enabled": True,
        "traced_current_kb": round(current / 1024, 1),
        "traced_peak_kb": round(peak / 1024, 1),
        "overhead_kb": round(overhead / 1024, 1),
        "top": [
            {"location": str(s.traceback), "size_kb": round(s.size / 1024, 1), "count": s.count}
            for s in stats
        ],
    }


# ── report assembly + four-tier source resolution ─────────────────────────────


def _daemon_pid() -> int | None:
    try:
        from olano.runtime import daemon

        pid = daemon.read_pid()
        if pid and daemon.is_pid_alive(pid):
            return int(pid)
    except Exception:
        return None
    return None


def _inprocess_report(
    rt: Any,
    *,
    include_gc: bool,
    gc_top: int,
    include_tracemalloc: bool,
    tracemalloc_top: int,
) -> dict[str, Any]:
    notes: list[str] = []
    if not _is_linux():
        notes.append("process metrics are Linux-only; some fields are null on this platform")
    notes.append(
        "config_tools counts configured names (not expanded bundles/MCP); "
        "RAG collections cover self-learning instances only — both are lower bounds"
    )
    notes.append(
        "allocator sizes overlap with traced memory and are not RSS; free arena bytes "
        "may be fragmented; allocations outside glibc are uncounted"
    )
    if include_gc:
        notes.append("gc is an object census; this report collects nothing and trims nothing")
    monitor = getattr(rt, "_memory_monitor", None)
    return {
        "source": "runtime",
        "running": True,
        "pid": os.getpid(),
        "platform": sys.platform,
        "process": _process_metrics(os.getpid(), is_self=True),
        "allocator": glibc_allocator(),
        "children": _child_processes(os.getpid()),
        "structures": _structure_breakdown(rt),
        "gc": _gc_census(gc_top) if include_gc else None,
        "tracemalloc": _tracemalloc_top(tracemalloc_top) if include_tracemalloc else None,
        "monitor": monitor.snapshot() if monitor is not None else None,
        "notes": notes,
        "detail": "in-process runtime snapshot",
    }


def memory_report(
    *,
    include_gc: bool = True,
    gc_top: int = _DEFAULT_GC_TOP,
    include_tracemalloc: bool = True,
    tracemalloc_top: int = _DEFAULT_TRACEMALLOC_TOP,
    pid: int | None = None,
) -> dict[str, Any]:
    """Process memory + olano structure breakdown, best available source.

    In-process (inside ``olano serve``) this is the live daemon; from a
    separate CLI it fetches the daemon's own report over the runtime API, or
    falls back to a ``/proc`` snapshot, or reports this process alone.
    """
    rt = _get_runtime()
    if rt is not None:
        return _inprocess_report(
            rt,
            include_gc=include_gc,
            gc_top=gc_top,
            include_tracemalloc=include_tracemalloc,
            tracemalloc_top=tracemalloc_top,
        )

    # Out-of-process: ask the daemon for its own full report.
    q = f"?gc={'true' if include_gc else 'false'}&tracemalloc={'true' if include_tracemalloc else 'false'}"
    api = _api_get_json("/api/diagnostics/memory" + q)
    if isinstance(api, dict):
        api["source"] = "api"
        api.setdefault("detail", "fetched from the running daemon's runtime API")
        return api

    # Daemon up but API unreachable → /proc of the daemon pid (metrics only).
    dpid = _daemon_pid()
    if dpid is not None:
        return {
            "source": "proc",
            "running": True,
            "pid": dpid,
            "platform": sys.platform,
            "process": _process_metrics(dpid, is_self=False),
            "structures": None,
            "gc": None,
            "tracemalloc": None,
            "monitor": None,
            "notes": ["daemon up but runtime API unreachable — process metrics only"],
            "detail": "out-of-process /proc snapshot of the daemon",
        }

    # No daemon at all — report this process only.
    this = pid or os.getpid()
    return {
        "source": "none",
        "running": False,
        "pid": this,
        "platform": sys.platform,
        "process": _process_metrics(this, is_self=(this == os.getpid())),
        "structures": None,
        "gc": None,
        "tracemalloc": None,
        "monitor": None,
        "notes": ["no olano daemon detected — showing this process only"],
        "detail": "no running runtime",
    }


# ── the monitor toggle (in-process direct, else POST to the daemon) ───────────

_MONITOR_ACTIONS = ("on", "off", "status", "tracemalloc-on", "tracemalloc-off")


def _api_post_json(path: str, payload: dict[str, Any], timeout: float = 3.0) -> Any | None:
    """POST JSON to the local runtime API; ``None`` if no daemon / it didn't answer.

    Mirrors :func:`olano.admin.diagnostics._api_get_json` (bearer token when
    ``LIONCLAW_DASHBOARD_TOKEN`` is set)."""
    import urllib.error
    import urllib.request

    from olano.runtime import daemon

    pid = daemon.read_pid()
    if not (pid and daemon.is_pid_alive(pid)):
        return None
    port = daemon._runtime_api_port()
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("LIONCLAW_DASHBOARD_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        # See diagnostics._api_get_json: an auth rejection must not read as
        # "no daemon" — log it so the degradation is diagnosable.
        if exc.code in (401, 403):
            logger.warning(
                "runtime API POST %s refused with HTTP %s (auth gate) — "
                "reporting no data; the loopback API requires a signed-in "
                "session on multi-user installs",
                path,
                exc.code,
            )
        return None
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def apply_monitor_action(rt: Any, action: str) -> dict[str, Any]:
    """Apply ``action`` to a live runtime's :class:`MemoryMonitor` and return its
    new snapshot. Used in-process by the API handler and the AgentFather tool."""
    action = (action or "status").strip().lower()
    mon: MemoryMonitor | None = getattr(rt, "_memory_monitor", None)
    if mon is None:
        return {"error": "runtime has no memory monitor", "enabled": None}
    if action in ("on", "enable"):
        mon.set_enabled(True)
    elif action in ("off", "disable"):
        mon.set_enabled(False)
        mon.set_tracemalloc(False)  # off also frees tracemalloc's trace buffers
    elif action == "tracemalloc-on":
        mon.set_tracemalloc(True)
    elif action == "tracemalloc-off":
        mon.set_tracemalloc(False)
    elif action == "status":
        pass
    else:
        return {"error": f"unknown action: {action}", **mon.snapshot()}
    return mon.snapshot()


def set_memory_monitor(action: str = "status") -> dict[str, Any]:
    """Toggle the background monitor / tracemalloc, or read its state.

    Actions: ``on`` / ``off`` / ``status`` / ``tracemalloc-on`` /
    ``tracemalloc-off``. ``off`` also stops tracemalloc so the monitor stops
    costing memory. In-process this flips the live monitor directly; from a
    separate CLI it POSTs to the running daemon.
    """
    action = (action or "status").strip().lower()
    rt = _get_runtime()
    if rt is not None:
        return apply_monitor_action(rt, action)
    res = _api_post_json("/api/diagnostics/memory/monitor", {"action": action})
    if isinstance(res, dict):
        return res
    return {"error": "no live runtime and daemon API unreachable", "enabled": None}


# ── periodic sampler (called by RuntimeServer._memory_sampler_loop) ───────────


def sample_memory_line(rt: Any, logger: Any) -> dict[str, Any]:
    """Emit one cheap ``mem-sample`` log line (``/proc`` + ``len()`` only) and
    stash it on the monitor for the dashboard. No gc / tracemalloc — safe to run
    on a short cadence."""
    proc = _process_metrics(os.getpid(), is_self=True)
    structs = _structure_breakdown(rt, with_rag_counts=False)
    ckpt = structs.get("checkpointer") or {}
    line = {
        "rss_mb": proc.get("rss_mb"),
        "vms_mb": proc.get("vms_mb"),
        "threads": proc.get("threads"),
        "tool_args": structs.get("tool_args_registry"),
        "hooks": structs.get("hook_contexts_total"),
        "offsets": structs.get("processed_offsets_total"),
        "ckpt_async": ckpt.get("async_conns"),
        "runners": structs.get("runners_count"),
    }
    try:
        logger.info(
            "mem-sample rss=%sMB vms=%sMB threads=%s tool_args=%s hooks=%s "
            "offsets=%s ckpt_async=%s runners=%s",
            line["rss_mb"],
            line["vms_mb"],
            line["threads"],
            line["tool_args"],
            line["hooks"],
            line["offsets"],
            line["ckpt_async"],
            line["runners"],
        )
    except Exception:  # pragma: no cover - logging must never break the loop
        pass
    mon = getattr(rt, "_memory_monitor", None)
    if mon is not None:
        mon._last_sample = line
    return line


# ── text rendering for CLI / chat ─────────────────────────────────────────────


def _fmt_mb(v: Any) -> str:
    return f"{v:,.0f} MB" if isinstance(v, int | float) else "—"


def _fmt(v: Any) -> str:
    return "—" if v is None else str(v)


def render_memory_report_text(report: dict[str, Any]) -> str:
    """Render :func:`memory_report` output as a plain-text block for the CLI
    and chat channels."""
    src = report.get("source", "?")
    lines = [f"🧠 Memory — source: {src}" + ("" if report.get("running") else "  (no daemon)")]

    p = report.get("process") or {}
    if p.get("rss_mb") is not None:
        rss = _fmt_mb(p.get("rss_mb"))
        pss = f" · Pss {_fmt_mb(p['pss_mb'])}" if p.get("pss_mb") is not None else ""
        lines.append(
            f"  RSS {rss}{pss} · VmSize {_fmt_mb(p.get('vms_mb'))} · peak {_fmt_mb(p.get('hwm_mb'))}"
        )
        extra = f"  Threads {_fmt(p.get('threads'))}"
        if p.get("uptime_seconds") is not None:
            extra += (
                f" · uptime {p['uptime_seconds'] // 3600}h{(p['uptime_seconds'] % 3600) // 60}m"
            )
        lines.append(extra)
    else:
        lines.append("  process metrics unavailable (non-Linux or no /proc)")

    s = report.get("structures")
    if s:
        lines.append("")
        lines.append("  Structures (leak indicators):")
        lines.append(
            f"    tool_args_registry {_fmt(s.get('tool_args_registry'))}"
            f" · hook_contexts {_fmt(s.get('hook_contexts_total'))}"
            f" · processed_offsets {_fmt(s.get('processed_offsets_total'))}"
        )
        ckpt = s.get("checkpointer") or {}
        lines.append(
            f"    runners {_fmt(s.get('runners_count'))}"
            f" · bg_tasks {_fmt(s.get('background_tasks'))}"
            f" · ckpt_async_conns {_fmt(ckpt.get('async_conns'))}"
            f" · scheduler_jobs {_fmt(s.get('scheduler_jobs'))}"
        )
        rag = s.get("rag") or {}
        lines.append(
            f"    embedding_model_resident {_fmt(rag.get('embedding_model_resident'))}"
            f" · rag_vectors {_fmt(rag.get('vectors_total'))}"
        )
        per_agent = s.get("per_agent") or {}
        if any(a.get("boot_rss_mb") is not None for a in per_agent.values()):
            lines.append("    Per-agent boot Δ-RSS (approx, graph build only):")
            for aname, a in sorted(
                per_agent.items(), key=lambda kv: kv[1].get("boot_rss_mb") or 0.0, reverse=True
            ):
                if a.get("boot_rss_mb") is not None:
                    lines.append(f"      {_fmt_mb(a['boot_rss_mb']):>10}  {aname}")

    children = report.get("children") or {}
    procs = [pr for pr in (children.get("processes") or []) if (pr.get("rss_mb") or 0) > 0]
    if procs:
        lines.append("")
        lines.append(
            f"  Subprocesses (separate PIDs, +{_fmt_mb(children.get('total_rss_mb'))} on top of RSS):"
        )
        for pr in procs[:8]:
            lines.append(
                f"    {_fmt_mb(pr.get('rss_mb')):>10}  {pr.get('label')}  (pid {pr.get('pid')})"
            )

    g = report.get("gc")
    if g and g.get("top_types"):
        lines.append("")
        lines.append(f"  GC objects {g.get('objects_total'):,} — top types:")
        for name, count in g["top_types"][:8]:
            lines.append(f"    {count:>10,}  {name}")

    tm = report.get("tracemalloc")
    if tm:
        lines.append("")
        if tm.get("enabled"):
            lines.append(
                f"  tracemalloc: {tm.get('traced_current_kb')} KB traced (peak {tm.get('traced_peak_kb')} KB)"
            )
            for entry in (tm.get("top") or [])[:5]:
                lines.append(f"    {entry['size_kb']:>10} KB  {entry['location']}")
        else:
            lines.append(f"  tracemalloc: off ({tm.get('hint', '')})")

    mon = report.get("monitor")
    if mon:
        lines.append("")
        lines.append(
            f"  Monitor: {'ON' if mon.get('enabled') else 'off'}"
            f" · sample every {mon.get('interval_seconds')}s"
            f" · tracemalloc {'on' if mon.get('tracemalloc') else 'off'}"
        )

    for note in report.get("notes") or []:
        lines.append(f"  · {note}")
    return "\n".join(lines)
