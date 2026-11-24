"""Cortex jobs: every background Cortex run, what it did, and its history.

A Cortex action used to *be* the HTTP request that started it. **Create
project** was a ``POST`` the dashboard awaited behind a spinner, so the run
existed only as long as that page held the request: close the panel and the
card still said *Create project*, so a second tap started a second promotion of
the same idea; lock the phone and nothing on the page could say whether the
first one was still going. Nothing anywhere recorded *what* a background run
had done, either: the audit log keeps one summary line per action, and Cortex
workers write no transcript (``cognition/worker.run_worker`` drives the raw
graph).

A job is the run object those surfaces were missing. It is created by the code
that starts a run (a route, the scheduler, the Pulse drainer), lives in this
process while it works, and is persisted per agent under
``<workspace>/.olano/state/cortex_jobs/`` so the history survives a restart:

* **One live job per (agent, kind, target).** :meth:`JobRegistry.start` hands
  back the job already running instead of starting a second one, which is what
  makes a double tap, a second tab and a re-opened panel all land on the same
  run.
* **Every step is logged.** :data:`CURRENT_JOB` is bound for the run, and
  :func:`job_callbacks` hands ``run_worker`` a LangChain callback that records
  each tool call, tool result and model message into the job, so "what did it
  do" has an answer while it runs and afterwards.
* **Nested runs fold into the one that started them.** A Think-now run calls
  :func:`olano.cognition.orchestrator.run_phase` eight times; each phase opens
  :func:`track` too, finds a job already bound and records a step in it rather
  than appearing as eight unrelated rows.

In-process for the same reason as :mod:`olano.core.runs`: a running job *is*
coroutines on this event loop. A record that still reads ``running`` on disk
but has no live job behind it was cut off by a restart and is reported as
``interrupted``, never as running.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Job states. ``running`` is the only live one.
RUNNING = "running"
DONE = "done"
ERROR = "error"
STOPPED = "stopped"
INTERRUPTED = "interrupted"
TERMINAL = frozenset({DONE, ERROR, STOPPED, INTERRUPTED})

#: Log lines one job keeps. A worker that takes more steps than this has its
#: oldest lines dropped (``log_dropped`` counts them), never its newest.
MAX_LOG_ENTRIES = 400

#: Characters kept per log line: enough to read a tool's arguments or the first
#: screen of its result, not enough to copy a whole file into the history.
MAX_ENTRY_CHARS = 1500

#: Finished jobs kept on disk per agent. Older ones are pruned on each new job.
MAX_HISTORY = 200

#: How often a running job's log is flushed to disk at most.
PERSIST_INTERVAL_S = 2.0

#: How long a finished job stays in memory, so a card can say "last attempt
#: failed" and a page that polls a beat late still sees the ending.
RECENT_RETENTION_S = 3600.0

#: ...and how many, whatever their age: a finished job keeps its full log in
#: memory (up to MAX_LOG_ENTRIES lines), and a fleet of scheduled phases would
#: otherwise hold an hour of them. The oldest go first; the disk keeps them.
RECENT_MAX = 300

#: Worker / phase outcome statuses that mean the run did not do its job.
_FAILED_OUTCOMES = frozenset(
    {"error", "timeout", "rejected", "budget_blocked", "needs_setup", "disabled"}
)

#: The job the current task is running inside (None outside any job).
CURRENT_JOB: contextvars.ContextVar[CortexJob | None] = contextvars.ContextVar(
    "cortex_current_job", default=None
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, limit: int = MAX_ENTRY_CHARS) -> str:
    s = text if isinstance(text, str) else _stringify(text)
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + f" … [{len(s) - limit} more chars]"


def _stringify(value: Any) -> str:
    content = getattr(value, "content", None)
    if content is not None:
        value = content
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        if parts:
            return "\n".join(parts)
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def status_for_outcome(outcome: str) -> str:
    """Map a worker / phase outcome (``completed``, ``timeout``…) to a job state."""
    return ERROR if (outcome or "").strip().lower() in _FAILED_OUTCOMES else DONE


def jobs_dir(workspace_root: Path) -> Path:
    from olano.config.workspace import WORKSPACE_STATE_DIRNAME

    return Path(workspace_root) / WORKSPACE_STATE_DIRNAME / "state" / "cortex_jobs"


class CortexJob:
    """One background Cortex run: identity, state, result and step log."""

    def __init__(
        self,
        *,
        agent: str,
        workspace_root: Path,
        kind: str,
        target: str = "",
        title: str = "",
        source: str = "manual",
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex[:16]
        self.agent = agent
        self.workspace_root = Path(workspace_root)
        self.kind = kind
        self.target = target
        self.title = title or kind
        self.source = source
        self.detail: dict[str, Any] = dict(detail or {})
        self.status = RUNNING
        self.outcome = ""
        self.started_at = _now_iso()
        self.ended_at = ""
        self._t0 = time.monotonic()
        self.duration_seconds = 0.0
        self.summary = ""
        self.error = ""
        self.result: dict[str, Any] = {}
        self.log: list[dict[str, Any]] = []
        self.log_dropped = 0
        self.task: asyncio.Task | None = None
        self.stop_requested = False
        self._lock = threading.Lock()
        self._last_flush = 0.0
        self._flush_pending = False
        # Held across snapshot + write, so two flushes can never interleave:
        # a periodic flush that snapshotted before the run ended must not land
        # after the final one and put a stale record back on disk.
        self._write_lock = threading.Lock()

    # ── recording ────────────────────────────────────────────────────────
    def note(self, text: Any, *, type: str = "note", **extra: Any) -> None:
        """Append one log line. Never raises: logging must not break a run."""
        try:
            entry: dict[str, Any] = {"ts": _now_iso(), "type": type, "text": _clip(text)}
            for key, value in extra.items():
                if value not in (None, ""):
                    entry[key] = (
                        value if isinstance(value, int | float | bool) else _clip(value, 200)
                    )
            with self._lock:
                self.log.append(entry)
                overflow = len(self.log) - MAX_LOG_ENTRIES
                if overflow > 0:
                    del self.log[:overflow]
                    self.log_dropped += overflow
            self._maybe_flush()
        except Exception:  # pragma: no cover - defensive
            logger.debug("cortex job %s: note failed", self.id, exc_info=True)

    def complete(
        self,
        *,
        outcome: str = "completed",
        summary: str = "",
        error: str = "",
        result: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> None:
        """Record how the run ended. The job is closed by whoever opened it."""
        self.outcome = outcome or ""
        self.summary = summary or ""
        self.error = error or ""
        if result is not None:
            self.result = result
        self.status = status or (ERROR if error and not summary else status_for_outcome(outcome))

    # ── views ────────────────────────────────────────────────────────────
    @property
    def live(self) -> bool:
        if self.status != RUNNING:
            return False
        return not (self.task is not None and self.task.done())

    def elapsed(self) -> float:
        if self.status == RUNNING:
            return round(time.monotonic() - self._t0, 1)
        return self.duration_seconds

    def to_dict(self, *, include_log: bool = False) -> dict[str, Any]:
        with self._lock:
            d: dict[str, Any] = {
                "id": self.id,
                "agent": self.agent,
                "kind": self.kind,
                "target": self.target,
                "title": self.title,
                "source": self.source,
                "detail": self.detail,
                "status": self.status,
                "outcome": self.outcome,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "duration_seconds": self.elapsed(),
                "summary": self.summary,
                "error": self.error,
                "result": self.result,
                "log_count": len(self.log) + self.log_dropped,
            }
            if self.log:
                d["last_step"] = self.log[-1]
            if include_log:
                d["log"] = list(self.log)
                d["log_dropped"] = self.log_dropped
        return d

    # ── persistence ──────────────────────────────────────────────────────
    def _maybe_flush(self) -> None:
        now = time.monotonic()
        if self._flush_pending or now - self._last_flush < PERSIST_INTERVAL_S:
            return
        self._flush_pending = True
        self._last_flush = now
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            self._periodic_flush()
            return
        # Off the loop: the log file grows with the run, and a write on network
        # block storage is exactly the stall CLAUDE.md warns about.
        loop.run_in_executor(None, self._periodic_flush)

    def _periodic_flush(self) -> None:
        try:
            self.flush()
        finally:
            self._flush_pending = False

    def flush(self) -> None:
        """Write the job's record and log to disk. Never raises."""
        try:
            with self._write_lock:
                meta = self.to_dict()
                with self._lock:
                    log_doc = {"log": list(self.log), "log_dropped": self.log_dropped}
                d = jobs_dir(self.workspace_root)
                d.mkdir(parents=True, exist_ok=True)
                _atomic_write_json(d / f"{self.id}.log.json", log_doc)
                _atomic_write_json(d / f"{self.id}.json", meta)
        except Exception:
            logger.debug("cortex job %s: flush failed", self.id, exc_info=True)


def _atomic_write_json(path: Path, doc: Any) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


class _Scope:
    """What :func:`JobRegistry.track` yields: the job, and whether it is ours.

    A nested scope (a phase inside a Think-now run) records into the job that
    is already running instead of closing it, so ``complete`` there becomes one
    step line in the parent.
    """

    def __init__(self, job: CortexJob, *, nested: bool, title: str) -> None:
        self.job = job
        self.nested = nested
        self.title = title

    def note(self, text: Any, *, type: str = "note", **extra: Any) -> None:
        self.job.note(text, type=type, **extra)

    def complete(self, **kw: Any) -> None:
        if not self.nested:
            self.job.complete(**kw)
            return
        outcome = kw.get("outcome") or ""
        text = kw.get("summary") or kw.get("error") or ""
        status = kw.get("status") or status_for_outcome(outcome)
        line = f"{self.title}: {outcome or status}"
        if text:
            line += f". {text}"
        self.job.note(line, type="step_error" if status == ERROR else "step_done")


class JobRegistry:
    """The Cortex jobs alive in this process, plus their on-disk history."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: dict[str, CortexJob] = {}
        self._recent: dict[str, tuple[float, CortexJob]] = {}

    # ── lookup ───────────────────────────────────────────────────────────
    def find_live(self, agent: str, kind: str, target: str = "") -> CortexJob | None:
        with self._lock:
            self._sweep_locked()
            for job in self._live.values():
                if job.agent == agent and job.kind == kind and job.target == target and job.live:
                    return job
        return None

    def live_jobs(self, agents: set[str] | None = None) -> list[CortexJob]:
        with self._lock:
            self._sweep_locked()
            jobs = [j for j in self._live.values() if j.live]
        if agents is not None:
            jobs = [j for j in jobs if j.agent in agents]
        return sorted(jobs, key=lambda j: j.started_at, reverse=True)

    def latest_for_target(self, agent: str, target: str) -> CortexJob | None:
        """The newest job on ``target`` (live, or finished within the hour)."""
        if not target:
            return None
        best: CortexJob | None = None
        with self._lock:
            self._sweep_locked()
            pool = list(self._live.values()) + [j for _, j in self._recent.values()]
        for job in pool:
            if (
                job.agent == agent
                and job.target == target
                and (best is None or job.started_at > best.started_at)
            ):
                best = job
        return best

    def get_memory(self, job_id: str) -> CortexJob | None:
        with self._lock:
            self._sweep_locked()
            job = self._live.get(job_id)
            if job is None and job_id in self._recent:
                job = self._recent[job_id][1]
        return job

    def get(self, workspace_root: Path, job_id: str, *, include_log: bool = True) -> dict | None:
        """One job, live or from history. ``None`` when it does not exist."""
        job = self.get_memory(job_id)
        if job is not None and job.workspace_root == Path(workspace_root):
            return job.to_dict(include_log=include_log)
        if not _safe_id(job_id):
            return None
        d = jobs_dir(workspace_root)
        meta = _read_json(d / f"{job_id}.json")
        if not isinstance(meta, dict):
            return None
        meta = self._reconcile(meta)
        if include_log:
            log_doc = _read_json(d / f"{job_id}.log.json") or {}
            meta["log"] = list(log_doc.get("log") or [])
            meta["log_dropped"] = int(log_doc.get("log_dropped") or 0)
        return meta

    def history(
        self,
        workspace_root: Path,
        agent: str,
        *,
        limit: int = 50,
        kind: str | None = None,
        target: str | None = None,
    ) -> list[dict]:
        """Newest-first jobs for one agent: live ones, then the persisted history."""
        rows: dict[str, dict] = {}
        for job in self.live_jobs({agent}):
            if job.workspace_root == Path(workspace_root):
                rows[job.id] = job.to_dict()
        d = jobs_dir(workspace_root)
        if d.is_dir():
            for path in d.glob("*.json"):
                if path.name.endswith(".log.json") or path.name.startswith("."):
                    continue
                job_id = path.name[: -len(".json")]
                if job_id in rows:
                    continue
                meta = _read_json(path)
                if isinstance(meta, dict) and meta.get("id"):
                    rows[job_id] = self._reconcile(meta)
        out = [
            r
            for r in rows.values()
            if (kind is None or r.get("kind") == kind)
            and (target is None or r.get("target") == target)
        ]
        out.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
        return out[: max(1, limit)]

    def _reconcile(self, meta: dict) -> dict:
        """A record on disk that says ``running`` with no live job behind it was
        cut off by a restart: say so instead of claiming it is still going."""
        if meta.get("status") == RUNNING:
            live = self.get_memory(str(meta.get("id") or ""))
            if live is None or not live.live:
                meta = {**meta, "status": INTERRUPTED}
                if not meta.get("error"):
                    meta["error"] = "The runtime restarted while this was running."
        return meta

    # ── control ──────────────────────────────────────────────────────────
    def stop(self, job_id: str) -> bool:
        job = self.get_memory(job_id)
        if job is None or not job.live:
            return False
        job.stop_requested = True
        job.note("Stop requested.", type="note")
        task = job.task
        if task is not None and not task.done():
            task.cancel()
        return True

    def reset(self) -> None:
        """Forget every in-memory job (tests)."""
        with self._lock:
            self._live.clear()
            self._recent.clear()

    # ── running ──────────────────────────────────────────────────────────
    def _open(self, job: CortexJob) -> None:
        with self._lock:
            self._live[job.id] = job
        job.note(f"Started: {job.title}", type="start")
        job.flush()
        _prune_history(job.workspace_root, keep=MAX_HISTORY, protect={job.id})
        _publish(job)

    def _close(self, job: CortexJob, exc: BaseException | None) -> None:
        if isinstance(exc, asyncio.CancelledError):
            job.status = STOPPED if job.stop_requested else INTERRUPTED
            job.error = job.error or ("Stopped." if job.stop_requested else "Interrupted.")
        elif exc is not None:
            job.status = ERROR
            job.error = job.error or str(exc) or exc.__class__.__name__
        elif job.status == RUNNING:
            job.status = DONE
        job.duration_seconds = round(time.monotonic() - job._t0, 1)
        job.ended_at = _now_iso()
        if job.status == DONE:
            job.note(f"Finished in {job.duration_seconds:.0f}s.", type="end")
        else:
            job.note(job.error or job.status, type="end_error")
        job.flush()
        with self._lock:
            self._live.pop(job.id, None)
            self._recent[job.id] = (time.time(), job)
        _publish(job)

    @contextlib.asynccontextmanager
    async def track(
        self,
        *,
        agent: str,
        workspace_root: Path,
        kind: str,
        target: str = "",
        title: str = "",
        source: str = "manual",
        detail: dict[str, Any] | None = None,
    ) -> AsyncIterator[_Scope]:
        """Record the enclosed block as a job of the current task.

        Inside a job that is already running (the same agent's), the block is a
        step of that job instead; see :class:`_Scope`.
        """
        parent = CURRENT_JOB.get()
        if parent is not None and parent.agent == agent and parent.live:
            label = title or kind
            parent.note(f"{label}: started", type="step")
            try:
                yield _Scope(parent, nested=True, title=label)
            except asyncio.CancelledError:
                parent.note(f"{label}: stopped", type="step_error")
                raise
            except BaseException as exc:
                parent.note(f"{label}: failed. {exc or exc.__class__.__name__}", type="step_error")
                raise
            return
        job = CortexJob(
            agent=agent,
            workspace_root=workspace_root,
            kind=kind,
            target=target,
            title=title,
            source=source,
            detail=detail,
        )
        task = asyncio.current_task()
        job.task = task
        self._open(job)
        token = CURRENT_JOB.set(job)
        try:
            yield _Scope(job, nested=False, title=job.title)
        except BaseException as exc:
            self._close(job, exc)
            raise
        else:
            self._close(job, None)
        finally:
            CURRENT_JOB.reset(token)

    def start(
        self,
        *,
        agent: str,
        workspace_root: Path,
        kind: str,
        run: Callable[[_Scope], Awaitable[None]],
        target: str = "",
        title: str = "",
        source: str = "manual",
        detail: dict[str, Any] | None = None,
    ) -> tuple[CortexJob, bool]:
        """Start ``run`` as a detached job, or hand back the one already running.

        Returns ``(job, started)``. ``started`` is ``False`` when a job of the
        same kind on the same target is already running for this agent, which
        is the whole point: pressing the button again joins the run instead of
        starting a second one.
        """
        with self._lock:
            self._sweep_locked()
            for existing in self._live.values():
                if (
                    existing.agent == agent
                    and existing.kind == kind
                    and existing.target == target
                    and existing.live
                ):
                    return existing, False
            job = CortexJob(
                agent=agent,
                workspace_root=workspace_root,
                kind=kind,
                target=target,
                title=title,
                source=source,
                detail=detail,
            )
            # Claimed under the lock, before the task exists, so two requests
            # arriving together cannot both start one.
            self._live[job.id] = job

        async def _drive() -> None:
            token = CURRENT_JOB.set(job)
            try:
                await run(_Scope(job, nested=False, title=job.title))
            except BaseException as exc:
                if not isinstance(exc, asyncio.CancelledError):
                    logger.exception("cortex job %s (%s) failed", job.id, job.kind)
                self._close(job, exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
            else:
                self._close(job, None)
            finally:
                CURRENT_JOB.reset(token)

        def _ensure_closed(_task: asyncio.Task) -> None:
            # Cancelled before `_drive` took its first step (a Stop right after
            # the start): its body never ran, so nothing closed the job, and
            # the disk would keep saying "running" until a restart made it
            # "interrupted". Close it here as the stop it was.
            if job.status == RUNNING:
                self._close(job, asyncio.CancelledError())

        # The job is already in `_live`; `_open` records it and announces it.
        self._open(job)
        job.task = asyncio.get_running_loop().create_task(
            _drive(), name=f"cortex-job:{agent}:{kind}:{job.id}"
        )
        job.task.add_done_callback(_ensure_closed)
        return job, True

    def _sweep_locked(self) -> None:
        cutoff = time.time() - RECENT_RETENTION_S
        for job_id in [k for k, (ts, _) in self._recent.items() if ts < cutoff]:
            self._recent.pop(job_id, None)
        if len(self._recent) > RECENT_MAX:
            oldest = sorted(self._recent.items(), key=lambda kv: kv[1][0])
            for job_id, _ in oldest[: len(self._recent) - RECENT_MAX]:
                self._recent.pop(job_id, None)
        # A task that died without reaching _close (it cannot, but a bug that
        # made it would otherwise pin "running" on a card forever).
        for job_id in [k for k, j in self._live.items() if j.task is not None and j.task.done()]:
            job = self._live.pop(job_id)
            if job.status == RUNNING:
                job.status = INTERRUPTED
                job.ended_at = job.ended_at or _now_iso()
            self._recent[job_id] = (time.time(), job)


def _safe_id(job_id: str) -> bool:
    return bool(job_id) and len(job_id) <= 64 and all(c.isalnum() or c in "-_" for c in job_id)


def _prune_history(workspace_root: Path, *, keep: int, protect: set[str]) -> None:
    """Drop the oldest finished job records beyond ``keep``. Never raises."""
    try:
        d = jobs_dir(workspace_root)
        metas = [
            p
            for p in d.glob("*.json")
            if not p.name.endswith(".log.json") and not p.name.startswith(".")
        ]
        if len(metas) <= keep:
            return
        metas.sort(key=lambda p: p.stat().st_mtime)
        for path in metas[: len(metas) - keep]:
            job_id = path.name[: -len(".json")]
            if job_id in protect or (REGISTRY.get_memory(job_id) or _NOT_LIVE).live:
                continue
            for victim in (path, d / f"{job_id}.log.json"):
                with contextlib.suppress(OSError):
                    victim.unlink()
    except Exception:
        logger.debug("cortex jobs: prune failed", exc_info=True)


class _NotLive:
    live = False


_NOT_LIVE = _NotLive()


def _publish(job: CortexJob) -> None:
    """Tell open dashboards a job started or ended (best-effort)."""
    try:
        from olano.runtime.api import _push_event

        _push_event("cortex_job", {"agent": job.agent, "job": job.to_dict()})
    except Exception:
        logger.debug("cortex job %s: publish failed", job.id, exc_info=True)


# ── step capture for workers ─────────────────────────────────────────────────


def job_callbacks() -> list[Any]:
    """LangChain callbacks that log a worker's steps into the current job.

    Empty outside a job, so a worker reached from chat (``delegate_task``) is
    untouched.
    """
    job = CURRENT_JOB.get()
    if job is None:
        return []
    try:
        return [_JobLogHandler(job)]
    except Exception:  # pragma: no cover - langchain missing
        return []


def _make_handler_class() -> type:
    from langchain_core.callbacks import BaseCallbackHandler

    class JobLogHandler(BaseCallbackHandler):
        """Writes tool calls, tool results and model messages into a job's log."""

        # Appends under a lock; no reason to hop to an executor thread.
        run_inline = True
        raise_error = False

        def __init__(self, job: CortexJob) -> None:
            self.job = job
            self._tools: dict[Any, str] = {}

        def on_tool_start(
            self, serialized: dict[str, Any] | None, input_str: str, **kwargs: Any
        ) -> None:
            name = str((serialized or {}).get("name") or kwargs.get("name") or "tool")
            self._tools[kwargs.get("run_id")] = name
            raw = kwargs.get("inputs")
            self.job.note(raw if raw is not None else input_str, type="tool", tool=name)

        def on_tool_end(self, output: Any, **kwargs: Any) -> None:
            name = self._tools.pop(kwargs.get("run_id"), "") or str(kwargs.get("name") or "tool")
            self.job.note(_clip(output, 600), type="tool_result", tool=name)

        def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
            name = self._tools.pop(kwargs.get("run_id"), "") or "tool"
            self.job.note(str(error) or error.__class__.__name__, type="tool_error", tool=name)

        def on_llm_end(self, response: Any, **kwargs: Any) -> None:
            try:
                for gens in getattr(response, "generations", None) or []:
                    for gen in gens:
                        message = getattr(gen, "message", None)
                        text = (
                            _stringify(message) if message is not None else getattr(gen, "text", "")
                        )
                        if isinstance(text, str) and text.strip():
                            self.job.note(text, type="text")
            except Exception:  # pragma: no cover - defensive
                pass

    return JobLogHandler


_HANDLER_CLASS: type | None = None


def _JobLogHandler(job: CortexJob) -> Any:
    global _HANDLER_CLASS
    if _HANDLER_CLASS is None:
        _HANDLER_CLASS = _make_handler_class()
    return _HANDLER_CLASS(job)


#: Process-wide registry, like :data:`olano.core.runs.REGISTRY`.
REGISTRY = JobRegistry()


def note(text: Any, *, type: str = "note", **extra: Any) -> None:
    """Log a line into the current job, if any. A no-op outside a job."""
    job = CURRENT_JOB.get()
    if job is not None:
        job.note(text, type=type, **extra)
