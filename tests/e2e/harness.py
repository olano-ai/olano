"""Launch a live dashboard (runtime + web UI) for browser tests.

The harness boots two processes and tears them down cleanly:

  1. the FastAPI runtime (:N) — served in-process by uvicorn in a daemon
     thread, wrapping ``create_api(<gateway>)``. By default it wraps a *mock*
     gateway (the same shape used by ``tests/test_gateway_api.py``) so the UI
     has a deterministic backend with no API keys, agents config or network.
  2. the Vite dev server (:M) — serves the React SPA from source and proxies
     ``/api`` to the runtime, exactly like the production ``server.mjs`` proxy.

``launch_dashboard()`` yields a :class:`RunningDashboard` whose ``base_url`` is
the SPA origin you point Playwright at. Everything is best-effort: if Node /
``node_modules`` are missing the caller is expected to ``pytest.skip`` (see
``conftest.py``), not crash.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx

# dashboard_2.0 lives next to the olano package: <repo>/olano/dashboard_2.0
DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "olano" / "dashboard_2.0"

_READY_TIMEOUT_S = 60.0
_POLL_INTERVAL_S = 0.25


def node_modules_present() -> bool:
    """True when ``npm install`` has been run for the dashboard."""
    return (DASHBOARD_DIR / "node_modules").is_dir()


def npm_available() -> bool:
    return shutil.which("npm") is not None


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


CHAT_REPLY_SENTINEL = "E2E streaming reply token ZXCVB"


class _FakeRunner:
    """A runner that streams a deterministic reply — no LLM, no API key.

    Implements just the surface the chat endpoints touch: ``config`` (for
    ``/api/agents`` and the stream's model/HITL lookups), an async ``ainvoke``,
    and an async-generator ``astream_events`` yielding the ``("text", str)``
    frames the dashboard renders into ``.bubble.assistant``.
    """

    def __init__(self, config: Any, reply: str) -> None:
        self.config = config
        self._reply = reply

    async def ainvoke(self, message: Any, thread_id: str | None = None, **_: Any) -> str:
        return self._reply

    async def astream_events(self, message: Any, **_: Any):
        for word in self._reply.split(" "):
            yield ("text", word + " ")


CHAT_SLOW_PREFIX = "E2E partial reply token QWXYZ"
CHAT_SLOW_SUFFIX = "and the rest arrived token PLMNB"


class _SlowRunner:
    """A runner whose reply arrives in two halves, seconds apart.

    Models the turn the detached-run work exists for (``olano/core/runs.py``):
    long enough that a phone locks, an app is switched, or a tab is reloaded
    while it is still working. The browser is expected to LOSE its view of the
    stream and rejoin the run, so the second half must arrive after a gap the
    test can act inside — and, crucially, the run must keep producing it whether
    or not anyone is watching. That is exactly what a fake runner proves: it has
    no idea a client exists.
    """

    def __init__(self, config: Any, prefix: str, suffix: str, gap: float = 4.0) -> None:
        self.config = config
        self._prefix = prefix
        self._suffix = suffix
        self._gap = gap

    async def ainvoke(self, message: Any, thread_id: str | None = None, **_: Any) -> str:
        return f"{self._prefix} {self._suffix}"

    def _persist(self, thread_id: str, role: str, content: str) -> None:
        """Write a transcript row the way the real runner does.

        The other fakes persist nothing, which is fine while a test never leaves
        the page that started the turn. A **reload** is the whole point of this
        one, and a reloaded tab rebuilds itself from the server: no transcript
        means `GET /threads/{id}` 404s and the conversation comes back empty,
        which would fail the test for a reason that has nothing to do with
        reattaching.
        """
        import json as _json
        from datetime import datetime, timezone

        home = os.environ.get("LIONCLAW_HOME")
        if not home or not thread_id:
            return
        sessions = Path(home) / "sessions" / self.config.name
        sessions.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "role": role,
            "content": content,
            "channel": "api",
        }
        with (sessions / f"{thread_id}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(row) + "\n")

    async def astream_events(self, message: Any, thread_id: str | None = None, **_: Any):
        # The real runner persists the human turn before the model runs, which
        # is what makes the conversation survive a reload mid-reply.
        self._persist(thread_id or "", "user", str(message))
        for word in self._prefix.split(" "):
            yield ("text", word + " ")
        await asyncio.sleep(self._gap)
        for word in self._suffix.split(" "):
            yield ("text", word + " ")
        self._persist(thread_id or "", "assistant", f"{self._prefix} {self._suffix}")


CHAT_STALL_PREFIX = CHAT_SLOW_PREFIX


class _CutStreamRunner:
    """A runner that streams a partial reply, then severs the connection.

    Models the moment a client's connection dies mid-turn (phone lock, network
    drop): the browser receives the partial words but never the terminal
    ``done`` frame. Raising ``CancelledError`` (a BaseException) skips the API's
    ``except Exception`` error-frame path, so the HTTP response is simply cut —
    which is exactly what the dashboard's interrupted-turn handling
    (sessionStore truncation detection + reconcile) must recognize. The cut
    comes from the server because Playwright's ``set_offline`` blocks *new*
    requests but does not abort an established local response stream.
    """

    def __init__(self, config: Any, prefix: str) -> None:
        self.config = config
        self._prefix = prefix

    async def ainvoke(self, message: Any, thread_id: str | None = None, **_: Any) -> str:
        return self._prefix

    async def astream_events(self, message: Any, **_: Any):
        for word in self._prefix.split(" "):
            yield ("text", word + " ")
        await asyncio.sleep(2.0)  # let the client render the partial first
        raise asyncio.CancelledError


# The exact wording langchain_ollama raises when a message carries a content
# block it has no format for — the string that reached a user as an opaque
# "API error (ValueError)". Kept verbatim so the E2E drives the real
# classification (`message_format_unsupported`) rather than a stand-in.
UNSUPPORTED_BLOCK_ERROR = (
    "Unsupported message content type. Must either have type 'text' or type "
    "'image_url' with a string 'image_url' field."
)


class _BlockRefusalRunner:
    """A runner whose turn dies the way a stuck thread does on ollama.

    ``astream_events`` raises a plain ``ValueError`` (an ``Exception``, so the
    API's ``except Exception`` path classifies it and emits an ``error`` frame,
    unlike :class:`_CutStreamRunner`'s ``CancelledError``). That frame is the
    only way the browser ever receives a remediation, so this is the one shape
    that exercises the chat's fix-it CTA end to end.
    """

    def __init__(self, config: Any) -> None:
        self.config = config

    async def ainvoke(self, message: Any, thread_id: str | None = None, **_: Any) -> str:
        raise ValueError(UNSUPPORTED_BLOCK_ERROR)

    async def astream_events(self, message: Any, **_: Any):
        # A real turn fails on the FIRST model call, before any token — yield
        # nothing, then raise. (`yield` is unreachable but keeps this an async
        # generator, which is what the endpoint iterates.)
        if False:  # pragma: no cover - generator marker
            yield ("text", "")
        raise ValueError(UNSUPPORTED_BLOCK_ERROR)


def build_block_refusal_gateway() -> Any:
    """A gateway whose only agent fails with the unsupported-content-block error.

    Backs ``test_chat_error_remediation.py``: the dashboard must render the
    classified message *and* a "Start a new chat" button — a remediation with
    no ``action`` renders nothing at all, which is the regression this guards.
    """
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    config = AgentConfig(
        name="merli",
        model="ollama:llama3.2",
        backend=FilesystemBackendConfig(root_dir="workspace"),
    )
    config.emoji = "🦁"
    config.description = "E2E block-refusal agent"
    config.channels = []

    return _gateway_for({"merli": _BlockRefusalRunner(config)})


def _gateway_for(runners: dict[str, Any]) -> Any:
    """Wrap built runners in a gateway with the attributes the API reads.

    Every attribute ``/api/status`` and ``/api/agents`` read must be a real
    value, because a bare ``MagicMock`` answers *any* ``getattr`` with a truthy,
    non-serializable stand-in:

    * ``registry`` is a real dict (``.get`` → None) so ``/api/agents``
      serializes cleanly — a mock yields non-JSON ``entry.status`` values and
      500s the roster the agent picker depends on.
    * ``_agents_booting`` is a real ``False``. A mock is truthy, so
      ``/api/status`` reported ``booting: true`` forever, the dashboard latched
      "Starting up", and its full-screen scrim swallowed every click — which is
      what made the drill-down and nav-drawer checks time out even though the
      views underneath had rendered.
    * ``_started_at`` is a real datetime; a mock's ``.isoformat()`` serialized
      as ``[]`` and the Overview uptime stat rendered from garbage.
    * ``_blocked_agents`` is a real dict. A mock's ``.get`` returns a truthy
      stand-in, so every on-disk agent the roster's second pass lists read as
      ``blocked: true`` with a non-string reason.
    """
    gw = MagicMock()
    gw._runners = runners
    gw.registry = {}
    gw._channels = []
    gw._agent_resources = {}
    gw._blocked_agents = {}
    gw._agents_booting = False
    gw._started_at = datetime.now(timezone.utc)
    gw.scheduler = MagicMock()
    gw.scheduler.list_jobs.return_value = []
    gw.get_runner.side_effect = lambda name: runners.get(name)
    return gw


def build_mock_gateway() -> Any:
    """A mock gateway good enough for the UI to mount and stream a chat reply.

    One agent (``merli``) backed by a :class:`_FakeRunner` that streams
    :data:`CHAT_REPLY_SENTINEL`. No API key or network required.
    """
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    config = AgentConfig(
        name="merli",
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir="workspace"),
    )
    config.emoji = "🦁"
    config.description = "E2E test agent"
    config.channels = []

    return _gateway_for({"merli": _FakeRunner(config, CHAT_REPLY_SENTINEL)})


def build_cut_stream_gateway() -> Any:
    """A mock gateway whose single agent streams a partial reply, then the
    connection is severed mid-turn (no terminal frame reaches the client).

    Used by the interrupted-turn E2E (``test_resume.py``).
    """
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    config = AgentConfig(
        name="merli",
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir="workspace"),
    )
    config.emoji = "🦁"
    config.description = "E2E cut-stream agent"
    config.channels = []

    return _gateway_for({"merli": _CutStreamRunner(config, CHAT_STALL_PREFIX)})


def build_slow_stream_gateway() -> Any:
    """A dashboard whose only agent replies in two halves, seconds apart.

    The fixture behind the reattach E2E: the client is cut off between the
    halves and must rejoin the still-running turn.
    """
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    config = AgentConfig(
        name="merli",
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir="workspace"),
    )
    config.emoji = "🦁"
    config.description = "E2E slow-stream agent"
    config.channels = []

    return _gateway_for({"merli": _SlowRunner(config, CHAT_SLOW_PREFIX, CHAT_SLOW_SUFFIX)})


# Per-sub-call input token sizes for the context-bar test (a tool-heavy turn:
# each model round reports the full prompt for THAT round, growing toward the
# 128k window). Their SUM (320k) is the billing total; the LAST (120k) is the
# live context-window fill the header bar must show.
CONTEXT_SUBCALL_INPUTS = (90_000, 110_000, 120_000)
CONTEXT_SUBCALL_SUM = sum(CONTEXT_SUBCALL_INPUTS)  # 320_000
CONTEXT_LAST_FILL = CONTEXT_SUBCALL_INPUTS[-1]  # 120_000
CONTEXT_REPLY_SENTINEL = "E2E context reply token PLKMN"


def build_context_sum_gateway() -> Any:
    """A gateway whose one agent runs a REAL ``AgentRunner`` over a mock deepagent
    that streams several model sub-calls in a single turn — each with its own
    ``usage_metadata`` — exercising the runner's real usage-frame accounting.

    The point is the header context bar: the summed input (320k) must NOT drive
    it; the last sub-call's prompt (120k) must. The model id is arbitrary — the
    mock agent means no provider/network is touched, and the window the bar
    divides by comes from the catalog, so the test asserts the fill only.
    """
    from unittest.mock import AsyncMock, MagicMock

    from langchain_core.messages import AIMessageChunk

    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig
    from olano.core.runner import AgentRunner

    config = AgentConfig(
        name="merli",
        model="deepseek:deepseek-v4-pro",
        backend=FilesystemBackendConfig(root_dir="workspace"),
    )
    config.emoji = "🦁"
    config.description = "E2E context-sum agent"
    config.channels = []

    words = CONTEXT_REPLY_SENTINEL.split(" ")

    async def fake_astream(*_args, **_kwargs):
        for i, in_tok in enumerate(CONTEXT_SUBCALL_INPUTS):
            yield (
                AIMessageChunk(
                    content=words[i % len(words)] + " ",
                    usage_metadata={
                        "input_tokens": in_tok,
                        "output_tokens": 300,
                        "total_tokens": in_tok + 300,
                    },
                ),
                {},
            )

    agent = MagicMock()
    agent.astream = fake_astream
    agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    agent.ainvoke = AsyncMock(return_value={"messages": []})

    return _gateway_for({"merli": AgentRunner(agent, config)})


#: What the console shows for a turn the agent declined under its reply policy.
#: Mirrors `runtime/api._POLICY_IGNORED_DETAIL`'s opening words; the test
#: asserts the phrase, not the whole sentence, so a wording tweak there needs
#: no change here.
NO_REPLY_NOTE = "No reply"


def build_silence_gateway() -> Any:
    """A gateway whose one agent runs a REAL ``AgentRunner`` over a mock
    deepagent that **prefaces** its decline: it streams a sentence, a blank
    line, then the silence marker on its own line.

    That is the exact shape both Hermes and OpenClaw shipped verbatim into
    customers' chats. Here the runner's stream filter must re-arm at the line
    start, keep the marker off the wire, and report the decline so the console
    draws its note in place of a reply.
    """
    from unittest.mock import AsyncMock, MagicMock

    from langchain_core.messages import AIMessageChunk

    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig
    from olano.core.runner import AgentRunner
    from olano.security.reply_policy import SILENCE_MARKER

    config = AgentConfig(
        name="merli",
        model="deepseek:deepseek-v4-pro",
        backend=FilesystemBackendConfig(root_dir="workspace"),
        message_policy={"ignore_rules": ["The message is not written in English"]},
    )
    config.emoji = "🦁"
    config.description = "E2E silence agent"
    config.channels = []

    async def fake_astream(*_args, **_kwargs):
        for chunk in ("Let me check that.", "\n\n", SILENCE_MARKER):
            yield (AIMessageChunk(content=chunk), {})

    agent = MagicMock()
    agent.astream = fake_astream
    agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    agent.ainvoke = AsyncMock(return_value={"messages": []})

    return _gateway_for({"merli": AgentRunner(agent, config)})


def build_real_gateway(agent_names: list[str]) -> tuple[Any, Any]:
    """A real-LLM gateway plus an async builder to populate it *in the loop*.

    Returns ``(gateway, build_runners)``. ``build_runners`` is an async callable
    run inside the runtime's serving event loop (see ``launch_dashboard``) so the
    agents are built with :func:`build_agent_async` — i.e. an **async** SQLite
    checkpointer. Building synchronously instead wires a sync ``SqliteSaver`` that
    raises "does not support async methods" the moment the chat streams.

    Each agent's provider key must be present in the environment; the model is
    actually invoked at chat time (e.g. ``test-agent-deepseek``).
    """
    gateway = _gateway_for({})

    async def build_runners(gw: Any) -> None:
        from olano.config.loader import load_agent_config
        from olano.core.builder import build_agent_async
        from olano.core.runner import AgentRunner

        runners: dict[str, Any] = {}
        for name in agent_names:
            cfg = load_agent_config(name)
            agent = await build_agent_async(cfg, include_mcp=bool(cfg.mcp_servers))
            runners[name] = AgentRunner(agent, cfg)
        gw._runners = runners
        gw.get_runner.side_effect = lambda n: runners.get(n)

    return gateway, build_runners


@dataclass
class RunningDashboard:
    """A live dashboard the test can drive with Playwright."""

    base_url: str  # SPA origin (Vite dev server) — point the browser here
    runtime_url: str  # FastAPI runtime origin (for direct API assertions)
    # The runtime's serving loop, filled once it starts (see `push_event`).
    _loop_box: list = field(default_factory=list, repr=False)
    # The gateway this dashboard serves, for tests that need its live runners.
    gateway: Any = field(default=None, repr=False)

    def push_event(self, event_type: str, data: dict) -> None:
        """Publish a live ``/api/stream`` frame exactly as the runtime would.

        ``_push_event`` feeds ``asyncio.Queue``s owned by the serving loop, and
        an ``asyncio.Queue`` is not thread-safe: a ``put_nowait`` from the test
        thread can sit unseen until something else wakes that loop. So the call
        is scheduled onto the loop itself.
        """
        from olano.runtime.api import _push_event

        if not self._loop_box:
            raise RuntimeError("runtime loop not captured; was it started by launch_dashboard?")
        self._loop_box[0].call_soon_threadsafe(_push_event, event_type, data)


def _wait_until(predicate, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception:
            pass
        time.sleep(_POLL_INTERVAL_S)
    raise TimeoutError(f"timed out after {timeout_s:.0f}s waiting for {what}")


def first_run_setup_code() -> str:
    """The one-time setup code the running dashboard's home expects.

    An unmanaged box with no accounts refuses ``/api/auth/bootstrap`` without
    it (``olano/runtime/setup_code.py``). Call while ``launch_dashboard`` is
    active: the code lives under the harness home, which is the process's
    ``LIONCLAW_HOME`` for that span. Harmless when enforcement is off (the
    suite-wide default, ``tests/conftest.py``): the server ignores the field.
    """
    from olano.runtime import setup_code

    return setup_code.expected_code(announce=False)


@contextmanager
def _runtime_server(
    gateway: Any, build_runners: Any = None, loop_box: list | None = None
) -> Iterator[str]:
    """Run ``create_api(gateway)`` under uvicorn in a daemon thread.

    ``build_runners`` (async) runs once inside the serving loop *before* the
    server accepts requests, so real agents are built and their async
    checkpointers bound to the same loop that will later stream chat.
    ``loop_box``, when given, receives that serving loop (for
    :meth:`RunningDashboard.push_event`).
    """
    import uvicorn

    from olano.runtime.api import create_api

    port = _free_port()
    app = create_api(gateway)
    # lifespan="off": the gateway has no real startup/shutdown to run.
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)

    async def _serve() -> None:
        if loop_box is not None:
            loop_box.append(asyncio.get_running_loop())
        if build_runners is not None:
            await build_runners(gateway)
        await server.serve()

    thread = threading.Thread(target=lambda: asyncio.run(_serve()), daemon=True)
    thread.start()
    try:
        _wait_until(lambda: server.started, _READY_TIMEOUT_S, "runtime to start")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@contextmanager
def _vite_server(runtime_url: str) -> Iterator[str]:
    """Run ``npm run dev`` proxied at ``runtime_url``."""
    port = _free_port()
    env = {
        **os.environ,
        "LIONCLAW_RUNTIME_URL": runtime_url,
        # Vite occasionally opens a browser on start; never do that in tests.
        "BROWSER": "none",
    }
    proc = subprocess.Popen(
        # `--host 127.0.0.1` forces an IPv4 bind. Vite's default `localhost`
        # resolves to IPv6 `::1` on macOS, but we probe (and point Playwright at)
        # `127.0.0.1` — without this the readiness probe times out after 60s even
        # though the dev server is up. Binding IPv4 keeps both ends on the same
        # stack on every OS.
        ["npm", "run", "dev", "--", "--port", str(port), "--strictPort", "--host", "127.0.0.1"],
        cwd=str(DASHBOARD_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # New session so we can kill Vite *and* its child esbuild workers.
        start_new_session=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_until(
            lambda: httpx.get(base_url, timeout=2.0).status_code == 200,
            _READY_TIMEOUT_S,
            "Vite dev server to serve the SPA",
        )
        yield base_url
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


# The agent configs the running dashboard should have on disk, captured from
# the gateway at launch. Written into whichever home is active *when asked* —
# see `seed_agent_configs`.
_SEED_ENTRIES: dict[str, dict] = {}


def _capture_seed_entries(gateway: Any) -> None:
    """Record a minimal ``agent.json5`` per agent the gateway is serving.

    Minimal and hand-written in shape, the way a real ``agent.json5`` looks — a
    full ``model_dump`` does not round-trip (``exclude_defaults`` drops the
    backend's ``type`` discriminator) and the file is meant to carry only what
    differs from the defaults anyway. Values come from the gateway's own runner
    configs, so the file and the running agent agree.
    """
    _SEED_ENTRIES.clear()
    for name, runner in (getattr(gateway, "_runners", None) or {}).items():
        cfg = getattr(runner, "config", None)
        if cfg is None:
            continue
        backend = getattr(cfg, "backend", None)
        _SEED_ENTRIES[name] = {
            "name": name,
            "model": getattr(cfg, "model", "anthropic:claude-haiku-4-5-20251001"),
            "description": getattr(cfg, "description", "") or "",
            "emoji": getattr(cfg, "emoji", None) or "🦁",
            "backend": {
                "type": "filesystem",
                "root_dir": str(getattr(backend, "root_dir", None) or "workspace"),
            },
        }


def seed_agent_configs(home: str | None = None) -> None:
    """Write the captured agent configs into ``home`` (default: the active one).

    Without this the runtime has agents in memory and **nothing on disk**, so
    every mutating endpoint — which reads the agent's file first — 404s with
    ``Agent 'merli' not found``, and any E2E test that saves something fails for
    a reason that has nothing to do with the UI.

    It has to be callable per test rather than once at launch, because
    ``tests/conftest.py`` repoints ``LIONCLAW_HOME`` at a fresh ``tmp_path`` for
    every test. The runtime server shares this process and resolves the home
    from the environment *at call time*, so it follows that move: the home the
    dashboard booted with is not the home a test's requests read. Read paths hid
    it — ``/api/cognition/status`` answers from the running agent's in-memory
    config and looked fine — while every write 404'd.

    Safe for the roster: ``_agents_roster`` walks running agents first and marks
    them seen, so the disk pass cannot duplicate them.
    """
    import json

    root = Path(home or os.environ.get("LIONCLAW_HOME") or "")
    if not str(root):
        return
    for name, entry in _SEED_ENTRIES.items():
        path = root / "agents" / name / "agent.json5"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")


def seed_onboarding_complete(home: str | None = None) -> None:
    """Mark the throwaway home's first-run wizard as already completed.

    The setup wizard now gates EVERY fresh admin — single-user implicit owner
    included — so without this seed every browser test would land in the
    wizard instead of the app it means to exercise. The wizard's own E2E
    module re-arms it per test with ``DELETE /api/onboarding`` (which unlinks
    this file and clears the in-process memo), so seeding here costs it
    nothing.
    """
    import json

    root = Path(home or os.environ.get("LIONCLAW_HOME") or "")
    if not str(root):
        return
    root.mkdir(parents=True, exist_ok=True)
    (root / "onboarding.json").write_text(
        json.dumps({"completed": True, "skipped": True, "completed_by": "e2e-harness"}),
        encoding="utf-8",
    )


@contextmanager
def launch_dashboard(
    gateway: Any | None = None, build_runners: Any = None
) -> Iterator[RunningDashboard]:
    """Boot runtime + UI and yield a :class:`RunningDashboard`.

    Pass a custom ``gateway`` to exercise real backend behaviour; omit it for
    the deterministic mock used by the smoke tests. ``build_runners`` (async,
    paired with :func:`build_real_gateway`) populates the gateway with real
    agents inside the serving loop before requests are accepted.
    """
    gw = gateway if gateway is not None else build_mock_gateway()
    # Point LIONCLAW_HOME at a throwaway dir so the runtime always boots in
    # single-user mode (no auth gate) with isolated state — otherwise a dev whose
    # real ~/.olano has multi-user enabled gets a login wall and every /api
    # call 401s, leaving the SPA stuck with no sidebar. Restored on exit.
    #
    # The throwaway home has no config.yaml, so checkpoint-backend resolution
    # falls to the shipped default (postgres) and — by design, there is no
    # silent fallback — raises for want of a connection URL. That surfaced as a
    # 500 from /api/analytics/overview and an Insights tab with no data. Pin the
    # zero-config sqlite backend instead, so the suite needs no database: it
    # resolves to threads.db under this same temp home and is discarded with it.
    import tempfile

    prev_home = os.environ.get("LIONCLAW_HOME")
    prev_ckpt = os.environ.get("LIONCLAW_CHECKPOINT_BACKEND")
    with tempfile.TemporaryDirectory(prefix="olano-e2e-home-") as home:
        os.environ["LIONCLAW_HOME"] = home
        os.environ["LIONCLAW_CHECKPOINT_BACKEND"] = "sqlite"
        _capture_seed_entries(gw)
        seed_agent_configs(home)
        seed_onboarding_complete(home)
        loop_box: list = []
        try:
            with (
                _runtime_server(gw, build_runners, loop_box) as runtime_url,
                _vite_server(runtime_url) as base_url,
            ):
                yield RunningDashboard(
                    base_url=base_url, runtime_url=runtime_url, _loop_box=loop_box, gateway=gw
                )
        finally:
            if prev_ckpt is None:
                os.environ.pop("LIONCLAW_CHECKPOINT_BACKEND", None)
            else:
                os.environ["LIONCLAW_CHECKPOINT_BACKEND"] = prev_ckpt
            if prev_home is None:
                os.environ.pop("LIONCLAW_HOME", None)
            else:
                os.environ["LIONCLAW_HOME"] = prev_home
