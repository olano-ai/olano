"""Shared fixtures for olano tests."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from olano.config.agent_config import AgentConfig, MemoryConfig
from olano.config.backend_config import FilesystemBackendConfig
from olano.config.home import ensure_olano_home
from olano.core.checkpoints import (
    CHECKPOINT_AES_KEY_ENV,
    CHECKPOINT_REQUIRE_ENCRYPTED_ENV,
    reset_checkpoint_serde,
    reset_checkpointers,
)
from olano.core.stores import reset_stores
from olano.runtime.seal_guard import SEAL_STATE_ENV
from olano.security.rate_limit import RUNTIME_RATE_LIMITER

# Langfuse keys present in the developer's environment, captured before any
# test runs so the opt-in live test (tests/integration/test_langfuse_live.py)
# can use them. Every other test sees them removed (lionclaw_home below): with
# real keys in the env and tracing enabled by a test, the suite would export
# its fixtures to someone's real project.
LANGFUSE_ENV_SNAPSHOT: dict[str, str] = {
    k: v
    for k, v in os.environ.items()
    if k.startswith(("LANGFUSE_", "OLANO_LANGFUSE_", "LIONCLAW_LANGFUSE_"))
}


def _ensure_event_loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        policy = asyncio.get_event_loop_policy()
        try:
            return policy.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop
    except Exception:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


@pytest.fixture(autouse=True)
def lionclaw_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _ensure_event_loop()
    reset_checkpointers()
    reset_checkpoint_serde()
    reset_stores()
    # Monetization entitlement store is a module-level singleton keyed off the
    # olano home; clear it so each test rebinds to its own tmp home DB.
    from olano.monetization.store import reset_entitlement_store

    reset_entitlement_store()
    # Shared module-level singleton — clear so request-heavy API tests don't
    # exhaust the 60/60s bucket for later tests (TestClient host isn't localhost).
    RUNTIME_RATE_LIMITER.clear()
    # HITL approval state is module-global: create_api() registers a delivery_fn
    # that would otherwise leak across tests and make the resolver's defer path
    # block on a real 300s approval. Reset it (and any pending requests) so each
    # test starts with no inbox unless it wires one explicitly.
    from olano.security import hitl as _hitl

    _hitl.set_delivery(None)
    _hitl._pending.clear()
    _hitl._callbacks.clear()
    # The approval record reconciles once per process; each test has its own
    # home, so each must reconcile its own log.
    _hitl._AUDIT_RECONCILED = False
    from olano.platforms import chat_approval as _chat_approval

    _chat_approval._pending.clear()
    # The Cortex duplicate gate consults the fastembed encoder in its grey band;
    # a unit test must never download a model, so the lexical half decides
    # alone here. Tests of the semantic half set the variable back and patch
    # the embedder.
    monkeypatch.setenv("LIONCLAW_CORTEX_SEMANTIC_DEDUP", "0")
    # The cloud catalog read path self-heals inline when its disk cache is
    # missing or stale — and every test gets an empty tmp home, so that is the
    # normal case here. Any test that prices a call or sizes a context window
    # (test_context_report, threadcost, …) would therefore reach api.olano.ai
    # for real: a live dependency in the unit suite, and hardcoded expectations
    # silently rebased onto whatever the cloud currently publishes. Keep it
    # hermetic — clear the module caches (they outlive a tmp home) and stub the
    # three network entry points. Tests that exercise the cloud path patch these
    # back over the top (see test_catalog_sync.py / test_mcp_registry.py).
    from olano.catalog import connectors_registry as _connectors_registry
    from olano.catalog import models_registry as _models_registry
    from olano.catalog import sync as _catalog_sync
    from olano.core import mcp_registry as _mcp_registry

    _models_registry.invalidate_cache()
    _connectors_registry.invalidate_cache()
    _mcp_registry._cache.clear()
    monkeypatch.setattr(_models_registry, "_last_attempt", _models_registry.NEVER_ATTEMPTED)
    monkeypatch.setattr(_mcp_registry, "_last_attempt", _mcp_registry.NEVER_ATTEMPTED)
    monkeypatch.setattr(_models_registry, "_fetch_from_cloud", lambda: None)
    monkeypatch.setattr(_mcp_registry, "_fetch_full", lambda *a, **k: None)
    monkeypatch.setattr(_catalog_sync, "fetch_manifest", lambda: None)
    monkeypatch.setattr(_connectors_registry, "_fetch_from_cloud", lambda: None)
    # The connectors freshness kick spawns a fetch thread from /api/auth/status
    # when the disk copy is stale — which in a tmp home is always. Pin it off so
    # runtime-API tests stay thread- and network-free; the kick's own tests
    # (test_catalog_sync.py) patch the seams they need back over the top.
    monkeypatch.setattr(_connectors_registry, "kick_refresh_if_stale", lambda *a, **k: False)
    home = tmp_path / "olano-home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    # OLANO_HOME outranks LIONCLAW_HOME in the resolver, so it must be absent
    # for the pin above to govern. Registering the delete with monkeypatch also
    # makes it an undo point: set_olano_home() writes OLANO_HOME straight to
    # os.environ, and without this it would survive teardown and silently
    # redirect every later test to the previous test's home.
    monkeypatch.delenv("OLANO_HOME", raising=False)
    # First-boot provisioning (runtime/first_boot.py) keys on an explicit admin
    # password in the environment and would seed every RuntimeServer.start()
    # in the suite from the developer's own deploy `.env` (cli/main.py loads
    # it at import). Tests that exercise it set the variables themselves.
    for _first_boot_var in (
        "LIONCLAW_ADMIN_PASSWORD",
        "LIONCLAW_ADMIN_EMAIL",
        "LIONCLAW_ADMIN_NAME",
        "LIONCLAW_DEFAULT_AGENT",
    ):
        monkeypatch.delenv(_first_boot_var, raising=False)
    # Checkpoint encryption at rest keys on LANGGRAPH_AES_KEY and the sealed-
    # volume boot guard on /etc/olano/seal.json. Neither may leak in from the
    # developer's deploy `.env` (loaded at import, see above) or from the host
    # the suite runs on: every test writes plaintext rows and reports custody
    # "none" unless it sets them itself (test_checkpoints / test_seal_guard).
    monkeypatch.delenv(CHECKPOINT_AES_KEY_ENV, raising=False)
    # The strict flag must go with it: a sealed box's `.env` sets both, and the
    # flag alone makes every saver construction raise (strict mode with no key
    # is refused by design), which would turn the whole suite red at once.
    monkeypatch.delenv(CHECKPOINT_REQUIRE_ENCRYPTED_ENV, raising=False)
    # The pasted Olano account key (deployment/gateway_key.py) turns an open box
    # into one that spends credits: every managed-default, media and services
    # assertion in the suite would flip if the developer's own `.env` carried
    # one. Both spellings (env_aliases mirrors them), plus the in-process memo,
    # which would otherwise carry a key one test stored into the next.
    monkeypatch.delenv("OLANO_CREDITS_KEY", raising=False)
    monkeypatch.delenv("LIONCLAW_CREDITS_KEY", raising=False)
    # A self-host `.env` with `setup.sh --sandbox` opts an open box into
    # per-agent sandboxes (deployment/enforcement.open_box_sandbox_requested),
    # which would flip every open-posture shell assertion in the suite.
    monkeypatch.delenv("OLANO_SANDBOX_EXEC", raising=False)
    monkeypatch.delenv("LIONCLAW_SANDBOX_EXEC", raising=False)
    from olano.deployment import gateway_key as _gateway_key

    for _lf_var in LANGFUSE_ENV_SNAPSHOT:
        monkeypatch.delenv(_lf_var, raising=False)
    from olano.observability import credentials as _lf_credentials

    _lf_credentials._reset_for_tests()
    _gateway_key.reset_account_key_memo()
    monkeypatch.setattr(_gateway_key, "_account_key_rejected_digest", None)
    monkeypatch.setenv(SEAL_STATE_ENV, str(tmp_path / "no-seal-state.json"))
    # Postgres is the default persistence backend, but the unit suite must stay
    # hermetic (no live Postgres server). Pin SQLite via env (env > config), so
    # agent/checkpointer/store builds resolve the zero-config on-disk backend
    # against the tmp home. Tests that exercise the real default (test_home)
    # clear these first via _clear_backend_env; the Postgres integration suite
    # (test_checkpoints_postgres) re-sets them per test.
    monkeypatch.setenv("LIONCLAW_CHECKPOINT_BACKEND", "sqlite")
    monkeypatch.setenv("LIONCLAW_STORE_BACKEND", "sqlite")
    # Tests drive scheduled restarts against fake runtimes that never exit the
    # process — a live exit watchdog would os._exit the pytest process once its
    # deadline lapses. Watchdog tests opt back in with a patched _force_exit.
    monkeypatch.setenv("LIONCLAW_EXIT_WATCHDOG_TIMEOUT", "0")
    # Cartography runs in a background thread on the runtime; tests that build
    # agents assert on its outputs (workspace_map.md in the prompt), so run it
    # inline here. The background path is covered by test_cartography_scan.py.
    monkeypatch.setenv("LIONCLAW_SYNC_CARTOGRAPHY", "1")
    # The WhatsApp (Twilio) connector asks Twilio's Senders API about its
    # number at connect. A test that wants that call opts back in and fakes
    # the client; every other connect must not reach for the network.
    monkeypatch.setenv("LIONCLAW_TWILIO_SENDER_CHECK", "0")
    # Multimodal defaults (image_describe / image_generate / image_edit /
    # text_to_speech / audio_to_text) are granted per-agent only when a provider
    # key resolves. That makes every "these are exactly the agent's tools"
    # assertion depend on whether the machine running pytest happens to export
    # OPENAI_API_KEY / ANTHROPIC_API_KEY / …, which is precisely the kind of
    # environment dependence the unit suite must not have. Pin the probes off;
    # the tests that exercise the grant put their own entries back (see
    # tests/test_multimodal_defaults.py).
    from olano.config import multimodal_defaults as _multimodal_defaults

    monkeypatch.setattr(_multimodal_defaults, "_AVAILABILITY", {})
    # image_describe remembers a model that hard-failed so it stops recommending
    # it (olano/tools/vision.py failure memo). That memo is process-global and
    # outlives a test, so without this one test's simulated outage silently
    # rewrites the next test's "models available here" line — order-dependent
    # assertions that pass alone and fail in a suite.
    from olano.tools import vision as _vision

    _vision.reset_failure_memo()
    # The loader's managed-box gate for the project-local agents/ dir memoizes
    # once per process, but deployment tests toggle the env evidence it reads
    # (LIONCLAW_DEPLOYMENT_MANAGED / token / manifest). Reset per test so a
    # posture test can't latch the gate for every test that runs after it.
    from olano.config import loader as _loader

    monkeypatch.setattr(_loader, "_local_agents_dir_memo", None)
    ensure_olano_home(home)
    return home


@pytest.fixture(autouse=True)
def setup_code_not_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Create a first admin without the first-run setup code, suite-wide.

    Dozens of tests bootstrap an admin over ``/api/auth/bootstrap`` as setup,
    on what reads as an unmanaged box, where the runtime now demands the
    one-time code (``runtime/setup_code.py``). The tests of that rule, and the
    first-run e2e, override this fixture by name to switch it back on. The
    environment spellings go too, so a developer's own self-host ``.env``
    cannot flip ``multi_user_enabled``'s default under the suite.
    """
    from olano.runtime import setup_code as _setup_code

    monkeypatch.setattr(_setup_code, "ENFORCED", False)
    monkeypatch.delenv("OLANO_SETUP_CODE", raising=False)
    monkeypatch.delenv("LIONCLAW_SETUP_CODE", raising=False)
    _setup_code.reset_attempts_for_tests()


@pytest.fixture(autouse=True)
def no_real_sandbox_containers(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the unit suite off Docker now that agent builds really start containers.

    ``_build_deepagents_backend`` used to hand back an UNSTARTED
    ``OlanoDockerBackend`` for an explicit ``backend: {type: "lionclaw_docker"}``
    — that silence is the bug this guard outlives. Now that the branch starts
    the container for real, any test handing the builder a docker config would
    shell out to ``docker run`` on the developer's machine, and `docker` is on
    PATH in most dev containers. Patch ``start()`` to raise so a build fails
    closed to a FilesystemBackend exactly as a box with no docker would; tests
    that want a started backend patch this back over the top.

    ``tests/integration/`` is exempt — ``make test`` collects it, and those
    tests exist precisely to drive real containers (they skip without one).

    Also resets ``_SANDBOX_BACKENDS``, which is process-global and would
    otherwise leak a previous test's backend into the next one's
    ``get_sandbox_backend()``.
    """
    import olano.core.builder as _builder

    monkeypatch.setattr(_builder, "_SANDBOX_BACKENDS", {})
    monkeypatch.setattr(_builder, "_SANDBOX_STATUS", {})
    from olano.backends import lionclaw_docker as _lionclaw_docker

    # Sandbox isolation resolves the default lane by asking docker for (and
    # creating) the isolated network; the memo of what it found is process-
    # global. Reset it per test, and pin isolation off for the unit suite so no
    # unit test creates a docker network on the developer's daemon. The
    # integration suite keeps the real default, which is what production runs;
    # tests of the isolation logic opt back in with their own fake docker.
    monkeypatch.setattr(_lionclaw_docker, "_ISOLATION_STATE", {})
    monkeypatch.setattr(_lionclaw_docker, "_NETWORK_FAILED_AT", {})
    monkeypatch.setattr(_lionclaw_docker, "_ISOLATION_WARNED", False)
    if "integration" in Path(str(request.node.fspath)).parts:
        return
    monkeypatch.setenv("LIONCLAW_SANDBOX_ISOLATION", "0")

    def _refuse(self, **kwargs):
        raise RuntimeError(
            "unit tests must not start real sandbox containers "
            "(see tests/conftest.py::no_real_sandbox_containers)"
        )

    monkeypatch.setattr(_lionclaw_docker.OlanoDockerBackend, "start", _refuse)


@pytest.fixture
def cortex_on() -> None:
    """Turn the Cortex engine on for the fleet in this test's home.

    The engine is off by default (``cognition.enabled`` defaults to false), so
    a test about what Cortex does once it runs has to say so. Without it a
    "skips when X is off" test passes because the engine is off, not because
    of X. Modules that are entirely about a running engine take it with
    ``pytestmark = pytest.mark.usefixtures("cortex_on")``, which also orders it
    after ``lionclaw_home`` and before any config fixture a test asks for.
    """
    from olano.config.system_settings import set_cortex_master

    set_cortex_master(True)


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """A temporary workspace directory with bootstrap files."""
    (tmp_path / "MEMORY.md").write_text(
        "# MEMORY.md - Long-Term Memory\n\n*Test memory.*\n", encoding="utf-8"
    )
    (tmp_path / "memories").mkdir()
    return tmp_path


@pytest.fixture
def base_config(tmp_workspace: Path) -> AgentConfig:
    """A minimal AgentConfig pointing at the tmp workspace."""
    return AgentConfig(
        name="test-agent",
        description="Test agent",
        model="anthropic:claude-haiku-4-5-20251001",
        system_prompt="You are a test assistant.",
        workspace=str(tmp_workspace),
        memory=MemoryConfig(markdown=True, rag=False, self_learning=False),
        backend=FilesystemBackendConfig(root_dir=str(tmp_workspace), virtual_mode=False),
    )


@pytest.fixture
def mock_agent():
    """A fake deepagent that returns a canned response."""
    agent = MagicMock()
    agent.invoke.return_value = {
        "messages": [
            MagicMock(type="ai", content="Hello from mock agent!"),
        ]
    }
    agent.ainvoke = AsyncMock(
        return_value={
            "messages": [
                MagicMock(type="ai", content="Hello from mock agent!"),
            ]
        }
    )

    agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))

    async def _astream(*args, **kwargs):
        msg = MagicMock()
        msg.content = "Hello from mock agent!"
        yield (msg, {})

    agent.astream = _astream
    return agent


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Close whatever async checkpointer the LAST test left open.

    aiosqlite's worker is a non-daemon thread, so an ``AsyncSqliteSaver``
    connection a test opened and never closed keeps the interpreter alive after
    the report is printed. Between tests the autouse ``lionclaw_home`` fixture
    closes such leftovers (``reset_checkpointers``), but nothing did at the end
    of the session: any run whose final test drives a real agent turn
    (``test_fake_model.py::test_full_agent_turn_offline`` is one) passed and then
    hung until killed. This is the same reset, once more, after the last test.
    """
    reset_checkpointers()


@pytest.fixture(autouse=True)
def _restore_bound_runtime_globals():
    """Put the two process-global "bound runtime" slots back after every test.

    ``RuntimeServer.start`` binds itself into ``tools.talk_to`` and the
    platform-control controller, and a test that drives ``start()`` (the
    first-boot tests do) left it there for the rest of the xdist worker.
    ``PromptAssembler.stable_sections`` then reported a ``roster`` section in
    whatever unrelated file shared that worker next, so
    ``test_prompt_state.py::test_full_mode_always_carries_the_stable_keys``
    passed or failed depending on how ``--dist loadfile`` dealt the files.
    Snapshot and restore rather than clear: a test that binds its own fake
    inside its body still sees it for the test's duration.
    """
    from olano.platform_control import controller as _pc
    from olano.tools import talk_to as _tt

    saved_runtime, saved_publisher, saved_controller = _tt._RUNTIME, _tt._PUBLISHER, _pc._RUNTIME
    try:
        yield
    finally:
        _tt._RUNTIME, _tt._PUBLISHER = saved_runtime, saved_publisher
        _pc._RUNTIME = saved_controller


@pytest.fixture(autouse=True)
def _empty_tool_args_registry():
    """Start every test with an empty middleware tool-args registry.

    The security middleware records each tool call's real arguments in a
    process-global dict keyed by tool-call id, and the runner pops them as the
    results arrive. A test whose graph ends a turn early leaves its entries
    there for the rest of the xdist worker, and tests reuse short ids
    (``c0``, ``c1``): the runner then read another test's arguments for the
    same id, so ``test_runner_breaker_steps.py::test_a_failed_save_does_not_break_the_turn``
    saw two identical failing calls as different ones, the breaker never
    tripped, and the turn fell through to the no-text ``ainvoke`` fallback
    on its MagicMock graph, depending on how ``--dist loadfile`` dealt the
    files. Real tool-call ids are unique, so this is a test-isolation fix
    only; nothing is imported when the middleware was never loaded.
    """
    module = sys.modules.get("olano.security.deepagents_middleware")
    if module is not None:
        module._TOOL_ARGS_REGISTRY.clear()
    yield
