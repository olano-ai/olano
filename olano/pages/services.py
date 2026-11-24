"""The page supervisor: keeps every deployed page built, running and healthy.

The registry (``pages/registry.py``) holds what each page *should* be; this
module makes it so and records what *is*. It runs inside the runtime process
(``RuntimeServer`` starts it at boot and stops it at shutdown) and reconciles
from the registry file alone, so a deploy recorded by any process (an agent
turn, the CLI, the dashboard) takes effect within one tick, and a runtime
restart brings every page back without anything to re-adopt (the keeper dies
with the runtime, see ``pages/keeper.py``).

One :class:`PageRunner` per deployed page. For a **static** page with a build
it runs the build, snapshots the output into an immutable release and switches
to it (a failed build leaves the previous release live). For a **service** it
also starts the server through a keeper, waits until the health path answers,
then probes it on an interval and restarts it when it exits or stops
answering, with exponential backoff and a crash-loop ceiling: five crashes
without a stable run settle the page in ``failed`` with the log tail, instead of
restarting forever. A new deploy, a ``pages_restart`` (the record's
``generation`` moves) or a spec change clears that and starts over.

Infrastructure loss is not a crash: a keeper that vanishes without reporting an
exit (the sandbox was rebuilt, the docker daemon restarted) is restarted at
once and never counts toward the ceiling.

What each runner observed is snapshotted to ``<home>/run/pages-services.json``
on every transition, which is how the tools, the dashboard and the CLI read
state, and how a restarted runtime knows a static page's current release
already matches its spec and needs no rebuild.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpcore

from olano.pages import lanes as page_lanes
from olano.pages import policy
from olano.pages import registry as pages_registry
from olano.pages import releases as page_releases
from olano.pages.mux import MuxClosed, MuxNetworkBackend, MuxSession
from olano.pages.paths import (
    build_log_path,
    keeper_pidfile,
    service_log_path,
    services_state_path,
)
from olano.pages.spec import KIND_SERVICE, KIND_STATIC, DeploySpec, record_spec
from olano.pages.workspaces import agent_workspace_root

logger = logging.getLogger(__name__)

# States a page can be in. Plain pages (no deploy spec) have no runner and
# read as "static".
STOPPED = "stopped"
WAITING = "waiting"
BUILDING = "building"
STARTING = "starting"
RUNNING = "running"
BACKOFF = "backoff"
FAILED = "failed"
LIVE = "live"  # a static build's release is being served

#: Crashes without a stable run in between before a page settles in FAILED.
CRASH_LIMIT = 5
#: A run this long since becoming ready resets the crash count.
STABLE_SECONDS = 300.0
BACKOFF_CAP = 60.0
HEALTH_FAILURES_TO_RESTART = 3
HEALTH_TIMEOUT = 10.0
HELLO_TIMEOUT = 45.0
LOST_RESTART_DELAY = 2.0
RECONCILE_INTERVAL = 5.0
POLICY_RECHECK_SECONDS = 60.0
WAIT_RETRY_SECONDS = 10.0
SERVICE_LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_TAIL_LINES = 25


def tail_text(path: Path, lines: int = LOG_TAIL_LINES, max_bytes: int = 64 * 1024) -> str:
    """The last ``lines`` lines of a log, reading at most ``max_bytes``."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    return "\n".join(text.splitlines()[-lines:])


def prepare_state_dir(root: Path, slug: str, agent: str) -> Path:
    """Create the page's log folder and hand it to the sandbox user. Blocking.

    The keeper and the sandbox-lane build write their logs there as uid 1000,
    and the workspace's ``.olano`` folder is created by the runtime (root), so
    without this the first write fails with ``Permission denied`` inside the
    container. Only what this call creates (plus the page's own folder and its
    files) changes owner; ``.olano`` itself is never re-owned. A no-op hand-off
    on the host lane, where the runtime and the commands are the same user.
    """
    from olano.config.workspace import WORKSPACE_STATE_DIRNAME
    from olano.pages.paths import page_state_dir
    from olano.tools._sandbox_exec import handoff_to_sandbox, missing_parents

    path = page_state_dir(root, slug)
    created = missing_parents(path / "_")
    path.mkdir(parents=True, exist_ok=True)
    state_root = Path(root) / WORKSPACE_STATE_DIRNAME
    handoff_to_sandbox(path, created=[p for p in created if p != state_root], agent=agent)
    with contextlib.suppress(OSError):
        for child in path.iterdir():
            handoff_to_sandbox(child, agent=agent)
    return path


def page_env(slug: str, spec: DeploySpec) -> dict[str, str]:
    """Environment for a page's build and server: the spec's own plus the page's URL prefix."""
    env = dict(spec.env)
    env["OLANO_PAGE_SLUG"] = slug
    env["OLANO_PAGE_PREFIX"] = f"/pages/{slug}"
    return env


@dataclass
class PageState:
    """What the supervisor observes about one page. Serialized as-is."""

    slug: str
    agent: str = ""
    kind: str = KIND_STATIC
    state: str = STOPPED
    detail: str = ""
    lane: str = ""
    port: int = 0
    listen_scope: str = ""
    started_at: float = 0.0
    ready_at: float = 0.0
    restarts: int = 0
    last_exit: int | None = None
    last_error: str = ""
    release: str = ""
    applied_generation: int = 0
    applied_digest: str = ""
    build: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PageState:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


class PageRunner:
    """Supervises one deployed page until it is stopped, deleted or undeployed."""

    def __init__(self, manager: PageServiceManager, slug: str, prior: PageState | None) -> None:
        self.manager = manager
        self.slug = slug
        self.state = prior or PageState(slug=slug)
        self.wake = asyncio.Event()
        self.ready = asyncio.Event()
        self.session: MuxSession | None = None
        self.pool: httpcore.AsyncConnectionPool | None = None
        self.spec: DeploySpec | None = None
        self.record: dict[str, Any] = {}
        self.inflight: asyncio.Semaphore = asyncio.Semaphore(
            manager.settings().max_concurrent_requests
        )
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._crashes: list[float] = []
        # A page that settled in FAILED stays there across a runtime restart
        # until its deploy changes; otherwise every reboot would buy a crash
        # loop five more attempts.
        self._failed = prior is not None and prior.state == FAILED
        self._last_health = 0.0
        self._health_failures = 0
        self._last_policy_check = 0.0
        self._lane_name = ""
        self._lane_isolated = False

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"page:{self.slug}")

    @property
    def done(self) -> bool:
        return self._task is None or self._task.done()

    def poke(self) -> None:
        self.wake.set()

    async def close(self) -> None:
        self._closing = True
        self.wake.set()
        if self._task is not None:
            # Every wait in the loop is woken by `wake` or bounded by a short
            # interval; only a build runs long, and cancelling it kills it.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
            if not self._task.done():
                self._task.cancel()
        await self._stop_keeper()

    # ── state ───────────────────────────────────────────────────────────────

    def _set(self, state: str | None = None, **changes: Any) -> None:
        if state is not None and state != self.state.state:
            _publish_transition(self.state.agent, self.slug, state)
        if state is not None:
            self.state.state = state
        for key, value in changes.items():
            setattr(self.state, key, value)
        self.state.updated_at = time.time()
        self.manager.save_state_soon()

    async def _idle(self, seconds: float) -> None:
        """Sleep until ``seconds`` pass or something pokes this runner."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.wake.wait(), timeout=seconds)
        self.wake.clear()

    # ── the loop ────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        try:
            await self._loop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - a bug, never a page's fault
            logger.exception("page %s: supervisor loop failed", self.slug)
            self._set(FAILED, detail="the supervisor hit an internal error", last_error=str(exc))
        finally:
            await self._stop_keeper()

    async def _loop(self) -> None:
        while not self._closing:
            record = await asyncio.to_thread(pages_registry.get_page, self.slug)
            spec = record_spec(record)
            if record is None or not record.get("enabled") or spec is None:
                await self._stop_keeper()
                self._set(STOPPED, detail="", port=0)
                return
            self.record, self.spec = record, spec
            agent = str(record.get("agent") or "")
            self.state.agent, self.state.kind = agent, spec.kind
            generation = int(record.get("generation") or 0)
            applied = (self.state.applied_generation, self.state.applied_digest)
            if (generation, spec.digest()) != applied and not await self._apply(
                record, spec, generation
            ):
                continue
            if spec.kind == KIND_STATIC:
                if (
                    spec.build
                    and self.state.state != FAILED
                    and page_releases.current_release_id(self.slug) is None
                ):
                    # The spec was applied but its release is gone (a restored
                    # home, a hand-deleted directory): build it again.
                    self.state.applied_generation, self.state.applied_digest = 0, ""
                    continue
                await self._idle(RECONCILE_INTERVAL)
                continue
            if self._failed:
                await self._idle(RECONCILE_INTERVAL)
                continue
            if self.session is None or not self.session.alive:
                await self._start_service(agent, spec)
                continue
            outcome = await self._monitor(agent, spec)
            if outcome in ("exit", "unhealthy"):
                await self._handle_crash(outcome)

    async def _apply(self, record: dict[str, Any], spec: DeploySpec, generation: int) -> bool:
        """Bring a new deploy/restart into effect. False ⇒ loop again (waiting)."""
        agent = str(record.get("agent") or "")
        self._failed = False
        self._crashes.clear()
        if spec.build:
            ok = await self._build(agent, record, spec)
            if ok is None:  # blocked: retry the whole apply later
                await self._idle(WAIT_RETRY_SECONDS)
                return False
            if not ok:
                self.state.applied_generation, self.state.applied_digest = generation, spec.digest()
                if spec.kind == KIND_STATIC:
                    live = page_releases.current_release_id(self.slug)
                    self._set(
                        LIVE if live else FAILED,
                        detail=(
                            f"the last build failed; still serving release {live}"
                            if live
                            else "the build failed and there is no earlier release to serve"
                        ),
                    )
                elif self.session is not None and self.session.alive:
                    self._set(
                        RUNNING,
                        detail="the last build failed; the previous server is still running",
                    )
                else:
                    self._failed = True
                    self._set(FAILED, detail="the build failed; fix it and redeploy or restart")
                return True
        self.state.applied_generation, self.state.applied_digest = generation, spec.digest()
        if spec.kind == KIND_STATIC:
            self._set(LIVE, detail="", lane=self.state.lane)
        else:
            # A redeploy or restart replaces the running server.
            await self._stop_keeper()
            self._set(STARTING, detail="starting the new deploy", restarts=0, last_exit=None)
        return True

    # ── build ───────────────────────────────────────────────────────────────

    async def _build(self, agent: str, record: dict[str, Any], spec: DeploySpec) -> bool | None:
        denial = await asyncio.to_thread(policy.command_denial, agent)
        lane, blocker = (None, denial) if denial else page_lanes.resolve_lane(agent)
        if lane is None:
            self._set(WAITING, detail=blocker or "waiting for the agent's sandbox")
            return None
        root = await asyncio.to_thread(agent_workspace_root, agent)
        if root is None:
            self._set(WAITING, detail=f"agent '{agent}' has no workspace")
            return None
        try:
            directory = pages_registry._validate_directory(root, str(record.get("directory", ".")))
        except pages_registry.PagesError as exc:
            self._set(FAILED, detail=str(exc))
            return False
        cwd = root / directory
        await asyncio.to_thread(prepare_state_dir, root, self.slug, agent)
        log_path = build_log_path(root, self.slug)
        started = time.time()
        self._set(
            BUILDING,
            detail="building"
            + ("; the current version keeps serving" if self.ready.is_set() else ""),
            lane=lane.name,
            build={"state": "running", "started_at": started, "command": spec.build},
        )
        result = await lane.run_build(
            spec.build,
            cwd=cwd,
            env=page_env(self.slug, spec),
            timeout=spec.build_timeout,
            log_path=log_path,
        )
        build: dict[str, Any] = {
            "state": "succeeded" if result.ok else "failed",
            "started_at": started,
            "finished_at": time.time(),
            "seconds": round(result.seconds, 1),
            "code": result.code,
            "timed_out": result.timed_out,
            "command": spec.build,
        }
        if not result.ok:
            build["error"] = result.error
            tail = await asyncio.to_thread(tail_text, log_path)
            self._set(build=build, last_error=_join(result.error, tail))
            return False
        if spec.kind == KIND_STATIC:
            output = (cwd / spec.output).resolve()
            try:
                output.relative_to(root.resolve())
            except ValueError:
                build.update(state="failed", error="output resolves outside the workspace")
                self._set(build=build, last_error=build["error"])
                return False
            try:
                info = await asyncio.to_thread(page_releases.snapshot_release, self.slug, output)
                await asyncio.to_thread(page_releases.activate_release, self.slug, info.id)
                await asyncio.to_thread(page_releases.prune_releases, self.slug)
            except (page_releases.ReleaseError, OSError) as exc:
                build.update(state="failed", error=str(exc))
                self._set(build=build, last_error=str(exc))
                return False
            build["release"] = {
                "id": info.id,
                "files": info.files,
                "bytes": info.bytes,
                "skipped_symlinks": info.skipped_symlinks,
                "has_index": info.has_index,
            }
            self._set(build=build, release=info.id, last_error="")
            return True
        self._set(build=build, last_error="")
        return True

    # ── service ─────────────────────────────────────────────────────────────

    async def _start_service(self, agent: str, spec: DeploySpec) -> None:
        denial = await asyncio.to_thread(policy.command_denial, agent)
        lane, blocker = (None, denial) if denial else page_lanes.resolve_lane(agent)
        if lane is None:
            self._set(WAITING, detail=blocker or "waiting for the agent's sandbox", port=0)
            await self._idle(WAIT_RETRY_SECONDS)
            return
        root = await asyncio.to_thread(agent_workspace_root, agent)
        if root is None:
            self._set(WAITING, detail=f"agent '{agent}' has no workspace", port=0)
            await self._idle(WAIT_RETRY_SECONDS)
            return
        try:
            directory = pages_registry._validate_directory(
                root, str(self.record.get("directory", "."))
            )
        except pages_registry.PagesError as exc:
            self._failed = True
            self._set(FAILED, detail=str(exc))
            return
        await asyncio.to_thread(prepare_state_dir, root, self.slug, agent)
        keeper_spec = {
            "slug": self.slug,
            "cwd": str(root / directory),
            "command": spec.start,
            "port": spec.port,
            "log_path": str(service_log_path(root, self.slug)),
            "log_max_bytes": SERVICE_LOG_MAX_BYTES,
            "pidfile": str(keeper_pidfile(root, self.slug)),
            "stop_grace": 10,
        }
        # First call may inspect the docker network: keep it off the loop.
        isolated = await asyncio.to_thread(lambda: lane.isolated)
        self._lane_name, self._lane_isolated = lane.name, isolated
        self._set(STARTING, detail="starting the server", lane=lane.name, listen_scope="")
        started = time.time()
        try:
            proc = await lane.spawn_keeper(keeper_spec, env=page_env(self.slug, spec))
        except (OSError, RuntimeError) as exc:
            self._set(WAITING, detail=f"could not start the keeper: {exc}")
            await self._idle(WAIT_RETRY_SECONDS)
            return
        session = MuxSession(proc, label=f"{self.slug} ({lane.name})")
        self.session = session
        try:
            hello = await session.wait_hello(HELLO_TIMEOUT)
        except MuxClosed as exc:
            info = await session.shutdown(grace=5)
            self.session = None
            stderr = "\n".join(session.stderr_tail)
            if info.get("code") is not None:
                # The keeper ran and the command could not start (exit 127…).
                self._set(last_exit=info.get("code"), last_error=_join(str(exc), stderr))
                await self._handle_crash("exit")
            else:
                self._set(
                    WAITING, detail="the keeper did not start", last_error=_join(str(exc), stderr)
                )
                await self._idle(WAIT_RETRY_SECONDS)
            return
        port = int(hello.get("port") or 0)
        settings = self.manager.settings()
        self.pool = httpcore.AsyncConnectionPool(
            network_backend=MuxNetworkBackend(session),
            max_connections=settings.max_concurrent_requests,
            keepalive_expiry=30.0,
        )
        self.inflight = asyncio.Semaphore(settings.max_concurrent_requests)
        self._set(port=port, started_at=started, ready_at=0.0)
        deadline = time.monotonic() + spec.startup_timeout
        delay = 0.25
        while time.monotonic() < deadline and not self._closing:
            if not session.alive:
                await self._handle_crash("exit")
                return
            if await self._probe(spec):
                self._health_failures = 0
                self._last_health = time.monotonic()
                self.ready.set()
                self._set(
                    RUNNING,
                    detail=_scope_detail(
                        session.listen_scope, self._lane_name, self._lane_isolated
                    ),
                    ready_at=time.time(),
                    listen_scope=session.listen_scope or "",
                )
                return
            await asyncio.sleep(delay)
            delay = min(1.0, delay * 2)
        if self._closing:
            return
        log_tail = await asyncio.to_thread(tail_text, service_log_path(root, self.slug))
        await self._stop_keeper()
        self._set(
            last_error=_join(
                f"the server did not answer on {spec.healthcheck} within {spec.startup_timeout}s "
                f"(it must listen on 127.0.0.1:$PORT, here {port})",
                log_tail,
            )
        )
        await self._handle_crash("unhealthy", lost=False, already_stopped=True)

    async def _probe(self, spec: DeploySpec) -> bool:
        pool = self.pool
        if pool is None:
            return False
        port = self.state.port
        path = spec.healthcheck if spec.strip_prefix else f"/pages/{self.slug}{spec.healthcheck}"
        try:
            async with pool.stream(
                "GET",
                f"http://127.0.0.1:{port}{path}",
                headers={"Host": f"127.0.0.1:{port}", "User-Agent": "olano-pages-health/1"},
                extensions={
                    "timeout": {
                        "connect": HEALTH_TIMEOUT,
                        "read": HEALTH_TIMEOUT,
                        "write": HEALTH_TIMEOUT,
                        "pool": HEALTH_TIMEOUT,
                    }
                },
            ) as response:
                return response.status < 500
        except Exception as exc:  # any failure to get a status line is "not healthy"
            logger.debug("page %s: health probe failed: %s", self.slug, exc)
            return False

    async def _monitor(self, agent: str, spec: DeploySpec) -> str:
        """Watch a running server. Returns why the watch ended."""
        session = self.session
        assert session is not None
        interval = float(self.manager.settings().health_interval_seconds)
        while not self._closing:
            closed = asyncio.ensure_future(asyncio.shield(session.closed))
            woken = asyncio.ensure_future(self.wake.wait())
            done, pending = await asyncio.wait(
                {closed, woken},
                timeout=min(interval, RECONCILE_INTERVAL),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            self.wake.clear()
            if not session.alive:
                return "exit"
            if self._closing:
                return "closing"
            record = await asyncio.to_thread(pages_registry.get_page, self.slug)
            current = record_spec(record)
            if (
                record is None
                or not record.get("enabled")
                or current is None
                or int(record.get("generation") or 0) != self.state.applied_generation
                or current.digest() != self.state.applied_digest
            ):
                return "changed"
            if session.listen_scope and session.listen_scope != self.state.listen_scope:
                self._set(
                    listen_scope=session.listen_scope,
                    detail=_scope_detail(
                        session.listen_scope, self._lane_name, self._lane_isolated
                    ),
                )
            now = time.monotonic()
            if now - self._last_policy_check >= POLICY_RECHECK_SECONDS:
                self._last_policy_check = now
                denial = await asyncio.to_thread(policy.command_denial, agent)
                if denial:
                    await self._stop_keeper()
                    self._set(WAITING, detail=denial, port=0)
                    return "denied"
            if now - self._last_health >= interval:
                self._last_health = now
                if await self._probe(spec):
                    if self._health_failures:
                        self._set(
                            RUNNING,
                            detail=_scope_detail(
                                session.listen_scope, self._lane_name, self._lane_isolated
                            ),
                        )
                    self._health_failures = 0
                else:
                    self._health_failures += 1
                    self._set(
                        RUNNING,
                        detail=f"health check failed {self._health_failures} time(s) in a row",
                    )
                    if self._health_failures >= HEALTH_FAILURES_TO_RESTART:
                        return "unhealthy"
        return "closing"

    async def _handle_crash(
        self, why: str, *, lost: bool | None = None, already_stopped: bool = False
    ) -> None:
        session = self.session
        info: dict[str, Any] = {}
        if session is not None and not already_stopped:
            info = await session.shutdown(grace=15) if session.alive else session.closed.result()
        if not already_stopped:
            await self._stop_keeper()
        if lost is None:
            # No EXIT frame ⇒ the keeper itself vanished (sandbox rebuilt,
            # daemon restarted): infrastructure, not the page's fault.
            lost = why == "exit" and session is not None and session.exit_info is None
        root = await asyncio.to_thread(agent_workspace_root, self.state.agent)
        tail = await asyncio.to_thread(tail_text, service_log_path(root, self.slug)) if root else ""
        if lost:
            self._set(
                BACKOFF,
                detail="the connection to the server was lost; reconnecting",
                last_error=_join(
                    info.get("reason", ""), "\n".join(session.stderr_tail) if session else ""
                ),
            )
            await self._idle(LOST_RESTART_DELAY)
            return
        now = time.time()
        if self.state.ready_at and now - self.state.ready_at >= STABLE_SECONDS:
            self._crashes.clear()
        self._crashes.append(now)
        code = info.get("code", self.state.last_exit)
        reason = (
            f"the server exited with code {code}"
            if why == "exit"
            else "the server stopped answering its health check"
        )
        changes: dict[str, Any] = {
            "restarts": self.state.restarts + 1,
            "last_exit": code,
            "ready_at": 0.0,
            "port": 0,
        }
        if why == "exit" or not self.state.last_error:
            changes["last_error"] = _join(reason, tail)
        if len(self._crashes) >= CRASH_LIMIT:
            self._failed = True
            self._set(
                FAILED,
                detail=(
                    f"stopped restarting after {CRASH_LIMIT} crashes in a row; read the server "
                    "log, fix the cause, then pages_restart"
                ),
                **changes,
            )
            return
        delay = min(BACKOFF_CAP, 2.0 ** (len(self._crashes) - 1))
        self._set(BACKOFF, detail=f"{reason}; restarting in {delay:g}s", **changes)
        await self._idle(delay)

    async def _stop_keeper(self) -> None:
        self.ready.clear()
        pool, self.pool = self.pool, None
        session, self.session = self.session, None
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.aclose()
        if session is not None and session.alive:
            with contextlib.suppress(Exception):
                await session.shutdown(grace=15)

    # ── for the proxy ───────────────────────────────────────────────────────

    async def wait_ready(self, timeout: float) -> bool:
        if self.ready.is_set():
            return True
        if self.state.state not in (STARTING, BACKOFF, BUILDING, WAITING):
            return False
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.ready.wait(), timeout)
        return self.ready.is_set()


def _scope_detail(scope: str | None, lane: str = "", isolated: bool = False) -> str:
    """A warning when the server's port is reachable around the page password.

    In an isolated sandbox (the default lane) no other container can open a
    connection to it, so binding every interface costs nothing and says
    nothing. On the shared bridge other agents' sandboxes can; on the host
    lane anything that reaches the host can.
    """
    if scope != "all" or isolated:
        return ""
    if lane == "host":
        return (
            "listening on all interfaces of this machine: anything that can reach the "
            "machine can reach it without the page password; bind 127.0.0.1 (the HOST "
            "variable) instead"
        )
    return (
        "listening on all interfaces, and this box's sandboxes are not isolated from "
        "each other: other agents' sandboxes can reach it without the page password; "
        "bind 127.0.0.1 (the HOST variable) instead"
    )


def _join(*parts: str) -> str:
    return "\n".join(p.strip() for p in parts if p and p.strip())[-4000:]


class PageServiceManager:
    """Owns every :class:`PageRunner` in this process."""

    def __init__(self) -> None:
        self.runners: dict[str, PageRunner] = {}
        self._task: asyncio.Task[None] | None = None
        self._save_handle: asyncio.TimerHandle | None = None
        self._prior: dict[str, PageState] = {}
        self._settings_cache: tuple[float, Any] | None = None
        self._lock = asyncio.Lock()

    def settings(self) -> Any:
        """``pages:`` settings, re-read at most every few seconds."""
        now = time.monotonic()
        if self._settings_cache is None or now - self._settings_cache[0] > RECONCILE_INTERVAL:
            from olano.config.system_settings import load_pages_settings

            self._settings_cache = (now, load_pages_settings())
        return self._settings_cache[1]

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._prior = await asyncio.to_thread(load_state_snapshot)
        self._task = asyncio.create_task(self._reconcile_loop(), name="pages-supervisor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
        runners = list(self.runners.values())
        await asyncio.gather(*(r.close() for r in runners), return_exceptions=True)
        # Snapshot BEFORE forgetting the runners: the snapshot is what tells the
        # next boot that a page's current release already matches its spec.
        data = self.snapshot()
        self.runners.clear()
        await asyncio.to_thread(_write_state, data)

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pages supervisor: reconcile failed")
            await asyncio.sleep(RECONCILE_INTERVAL)

    async def reconcile(self) -> None:
        """Start a runner for every deployed, enabled page; forget finished ones."""
        async with self._lock:
            pages = await asyncio.to_thread(pages_registry.list_pages)
            wanted = {
                p["slug"]: p for p in pages if p.get("enabled") and record_spec(p) is not None
            }
            services_on = self.settings().services_enabled
            for slug, runner in list(self.runners.items()):
                if runner.done:
                    self.runners.pop(slug, None)
                    if slug not in wanted:
                        self._prior.pop(slug, None)
            for slug, record in wanted.items():
                if slug in self.runners:
                    continue
                spec = record_spec(record)
                if spec is not None and spec.kind == KIND_SERVICE and not services_on:
                    continue
                runner = PageRunner(self, slug, self._prior.get(slug))
                self.runners[slug] = runner
                runner.start()
            known = {p["slug"] for p in pages}
            await asyncio.to_thread(_drop_orphan_releases, known)
            if not services_on:
                for slug, runner in list(self.runners.items()):
                    if runner.state.kind == KIND_SERVICE:
                        await runner.close()
                        runner.state.state = WAITING
                        runner.state.detail = policy.SERVICES_DISABLED
                        self._prior[slug] = runner.state
                        self.runners.pop(slug, None)
            self.save_state_soon()

    def notify(self, slug: str) -> None:
        """A page's registry entry changed in this process: act now, not next tick."""
        runner = self.runners.get(slug)
        if runner is not None and not runner.done:
            runner.poke()
        else:
            self._kick()

    def _kick(self) -> None:
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self.reconcile())

    async def forget(self, slug: str) -> None:
        """Stop a page's runner now (deleted or stopped) and drop its state."""
        runner = self.runners.pop(slug, None)
        if runner is not None:
            await runner.close()
        self._prior.pop(slug, None)
        self.save_state_soon()

    # ── state ───────────────────────────────────────────────────────────────

    def runner(self, slug: str) -> PageRunner | None:
        return self.runners.get(slug)

    def page_state(self, slug: str) -> PageState | None:
        runner = self.runners.get(slug)
        if runner is not None:
            return runner.state
        return self._prior.get(slug)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        states = dict(self._prior)
        states.update({slug: r.state for slug, r in self.runners.items()})
        return {slug: state.to_dict() for slug, state in states.items()}

    def save_state_soon(self) -> None:
        """Coalesce bursts of transitions into one snapshot write."""
        if self._save_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._save_handle = loop.call_later(0.2, self._flush)

    def _flush(self) -> None:
        self._save_handle = None
        data = self.snapshot()
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().run_in_executor(None, _write_state, data)


def _drop_orphan_releases(known_slugs: set[str]) -> None:
    """Delete release directories whose page no longer exists in the registry.

    ``pages_delete`` removes a page's releases itself; this catches the one it
    cannot: a build that finished after the delete and wrote a release for a
    page that is gone.
    """
    from olano.pages.paths import releases_root

    try:
        entries = list(releases_root().iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_dir() and entry.name not in known_slugs:
            page_releases.delete_releases(entry.name)


def _write_state(data: dict[str, Any]) -> None:
    try:
        from olano.memory.markdown_memory import atomic_write_text

        path = services_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"version": 1, "pages": data}, indent=1, sort_keys=True))
    except Exception:  # pragma: no cover - a full disk must not stop serving
        logger.debug("pages supervisor: state snapshot write failed", exc_info=True)


def _publish_transition(agent: str, slug: str, state: str) -> None:
    """Tell dashboards a page changed state, so they refetch ``/api/pages``.

    One page per frame, attributed to its agent (the SSE stream only shows a
    frame to subscribers who may see that agent) and carrying no detail or log
    text: what a page printed is read through the admin-only listing, never
    broadcast.
    """
    if not agent:
        return
    try:
        from olano.runtime.api import _push_event

        _push_event("page_state", {"agent": agent, "slug": slug, "state": state})
    except Exception:  # pragma: no cover - no live dashboard stream
        pass


def load_state_snapshot() -> dict[str, PageState]:
    """The last observed state per page, as written by a supervisor (any process)."""
    try:
        raw = json.loads(services_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    pages = raw.get("pages") if isinstance(raw, dict) else None
    if not isinstance(pages, dict):
        return {}
    out: dict[str, PageState] = {}
    for slug, data in pages.items():
        if isinstance(data, dict):
            with contextlib.suppress(TypeError):
                out[str(slug)] = PageState.from_dict(data)
    return out


# ── process-wide handle ──────────────────────────────────────────────────────

_MANAGER: PageServiceManager | None = None


def get_manager() -> PageServiceManager | None:
    """This process's supervisor, or ``None`` outside a running runtime."""
    return _MANAGER


async def start_manager() -> PageServiceManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = PageServiceManager()
        await _MANAGER.start()
    return _MANAGER


async def stop_manager() -> None:
    global _MANAGER
    manager, _MANAGER = _MANAGER, None
    if manager is not None:
        await manager.stop()


def notify_page_changed(slug: str) -> None:
    """Tell this process's supervisor (if any) that ``slug`` changed. Never raises."""
    manager = _MANAGER
    if manager is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        manager.notify(slug)
    else:
        # Called from a worker thread (a sync tool body): hop onto the loop
        # the manager runs on.
        target = getattr(manager._task, "get_loop", lambda: None)()
        if target is not None and not target.is_closed():
            target.call_soon_threadsafe(manager.notify, slug)


def page_status(slug: str) -> dict[str, Any] | None:
    """Observed state for ``slug`` from the live supervisor, else the snapshot file."""
    manager = _MANAGER
    if manager is not None:
        state = manager.page_state(slug)
        return state.to_dict() if state is not None else None
    state = load_state_snapshot().get(slug)
    return state.to_dict() if state is not None else None


def all_page_status() -> dict[str, dict[str, Any]]:
    manager = _MANAGER
    if manager is not None:
        return manager.snapshot()
    return {slug: state.to_dict() for slug, state in load_state_snapshot().items()}
