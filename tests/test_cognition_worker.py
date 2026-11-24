"""Tests for the reusable background reasoning worker."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from olano.cognition.worker import (
    _parse_artifacts,
    _strip_artifacts_line,
    run_worker,
)
from olano.config.agent_config import AgentConfig
from olano.security.delegate_guards import DEPTH_VAR, MAX_DEPTH, current_depth


@pytest.fixture(autouse=True)
def _configured_model(monkeypatch):
    """These tests exercise worker mechanics and assume a reachable model, so
    provide a dummy provider key that satisfies the readiness preflight. Tests
    that assert the no-key / needs_setup path delete it explicitly."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


def _child_returning(content: str) -> MagicMock:
    child = MagicMock()
    msg = MagicMock()
    msg.content = content
    child.ainvoke = AsyncMock(return_value={"messages": [msg]})
    return child


# ----------------------------------------------------------------- unit helpers


def test_parse_artifacts() -> None:
    assert _parse_artifacts("did stuff\nARTIFACTS: IDEAS.md, MEMORY.md") == [
        "IDEAS.md",
        "MEMORY.md",
    ]
    assert _parse_artifacts("no artifacts here") == []
    assert _parse_artifacts("") == []


def test_strip_artifacts_line() -> None:
    assert _strip_artifacts_line("summary text\nARTIFACTS: a.md") == "summary text"


# ----------------------------------------------------------------- run_worker


async def test_run_worker_completed_with_artifacts(base_config: AgentConfig, tmp_path) -> None:
    child = _child_returning("Consolidated 3 facts.\nARTIFACTS: MEMORY.md, LEARNINGS.md")
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        outcome = await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="Do the thing.",
            goal="consolidate memory",
            worker_label="test",
        )
    assert outcome.status == "completed"
    assert outcome.summary == "Consolidated 3 facts."
    assert outcome.artifacts == ["MEMORY.md", "LEARNINGS.md"]


def _child_with_usage(content: str, input_tokens: int, output_tokens: int) -> MagicMock:
    child = MagicMock()
    msg = MagicMock()
    msg.content = content
    msg.usage_metadata = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    msg.response_metadata = {}
    child.ainvoke = AsyncMock(return_value={"messages": [msg]})
    child.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    return child


async def test_run_worker_ledgers_usage_under_the_owning_agent(
    base_config: AgentConfig, tmp_path
) -> None:
    """Cognition runs on the raw graph, so runner._record_usage never sees it —
    the worker must ledger its own spend, attributed to the PARENT agent (not
    the ephemeral `__cog__` child) and origin-tagged."""
    from olano.analytics.usage_ledger import read_usage_events

    child = _child_with_usage("done", 1200, 80)
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        outcome = await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="p",
            goal="g",
            worker_label="pulse_agent",
        )
    assert outcome.status == "completed"
    events = read_usage_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.agent == base_config.name
    assert ev.origin == "cognition"
    assert ev.input_tokens == 1200 and ev.output_tokens == 80 and ev.calls == 1


async def test_run_worker_delegate_label_gets_delegate_origin(
    base_config: AgentConfig, tmp_path
) -> None:
    from olano.analytics.usage_ledger import read_usage_events

    child = _child_with_usage("done", 100, 10)
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="p",
            goal="g",
            worker_label="sub__deploy",
        )
    events = read_usage_events()
    assert len(events) == 1 and events[0].origin == "delegate"


async def test_run_worker_passes_top_level_recursion_limit(
    base_config: AgentConfig, tmp_path
) -> None:
    """`recursion_limit` is a TOP-LEVEL RunnableConfig key; nested inside
    `configurable` langgraph ignores it and every worker ran at the default 25
    instead of the configured cap (~2x the intended spend)."""
    child = _child_returning("ok")
    child.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="p",
            goal="g",
            max_iterations=12,
        )
    cfg = child.ainvoke.call_args.kwargs["config"]
    assert cfg["recursion_limit"] == 12
    assert "recursion_limit" not in cfg["configurable"]
    assert cfg["configurable"]["thread_id"].startswith("cog::")


async def test_run_worker_is_isolated_from_the_callers_live_stream(
    base_config: AgentConfig, tmp_path
) -> None:
    """A worker's tokens are never the caller's reply.

    `delegate_task` calls this from inside the caller's turn, so without
    isolation LangGraph's StreamMessagesHandler rides the ambient run config into
    the worker and its output is spliced into the caller's channel message. The
    worker's result reaches the caller as a WorkerOutcome, never as streamed
    text. Both belts asserted: the ambient config is gone during the run, and the
    run is tagged `nostream` for LangGraph's own suppression.
    """
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.runnables.config import var_child_runnable_config

    from olano.core.stream_isolation import NOSTREAM_TAG

    class _ParentStreamHandler(BaseCallbackHandler):
        """Stands in for LangGraph's StreamMessagesHandler."""

    seen: dict[str, object] = {}
    parent_stream = {"callbacks": [_ParentStreamHandler()]}

    child = MagicMock()
    child.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))

    async def _ainvoke(_payload, config=None, **_kwargs):
        seen["ambient"] = var_child_runnable_config.get()
        msg = MagicMock()
        msg.content = "worker done"
        return {"messages": [msg]}

    child.ainvoke = AsyncMock(side_effect=_ainvoke)

    token = var_child_runnable_config.set(parent_stream)
    try:
        with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
            await run_worker(
                parent_config=base_config,
                workspace_root=tmp_path,
                playbook="p",
                goal="g",
            )
    finally:
        var_child_runnable_config.reset(token)

    assert seen["ambient"] == {}, "the caller's streaming callbacks leaked into the worker"
    assert NOSTREAM_TAG in child.ainvoke.call_args.kwargs["config"]["tags"]


async def test_run_worker_empty_goal_errors(base_config: AgentConfig, tmp_path) -> None:
    outcome = await run_worker(
        parent_config=base_config, workspace_root=tmp_path, playbook="x", goal="  "
    )
    assert outcome.status == "error"
    assert "goal" in (outcome.error or "")


async def test_run_worker_budget_blocked_skips_build(base_config: AgentConfig, tmp_path) -> None:
    build_mock = AsyncMock()
    with (
        patch("olano.cognition.worker.budget_gate", return_value=(False, "over budget")),
        patch("olano.core.builder.build_agent_async", new=build_mock),
    ):
        outcome = await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="x",
            goal="do it",
        )
    assert outcome.status == "budget_blocked"
    build_mock.assert_not_awaited()


async def test_run_worker_depth_rejected_skips_build(base_config: AgentConfig, tmp_path) -> None:
    build_mock = AsyncMock()
    token = DEPTH_VAR.set(MAX_DEPTH)
    try:
        with patch("olano.core.builder.build_agent_async", new=build_mock):
            outcome = await run_worker(
                parent_config=base_config,
                workspace_root=tmp_path,
                playbook="x",
                goal="do it",
                budget_check=False,
            )
    finally:
        DEPTH_VAR.reset(token)
    assert outcome.status == "rejected"
    assert "depth" in (outcome.error or "").lower()
    build_mock.assert_not_awaited()


async def test_run_worker_timeout(base_config: AgentConfig, tmp_path) -> None:
    async def hang(*_a, **_k):
        await asyncio.sleep(5)
        return {"messages": []}

    child = MagicMock()
    child.ainvoke = hang
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        outcome = await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="x",
            goal="stall",
            timeout=0.05,
            budget_check=False,
        )
    assert outcome.status == "timeout"
    assert "timeout" in (outcome.error or "").lower()


async def test_run_worker_build_failure(base_config: AgentConfig, tmp_path) -> None:
    with patch(
        "olano.core.builder.build_agent_async",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        outcome = await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="x",
            goal="do it",
            budget_check=False,
        )
    assert outcome.status == "error"
    assert "boom" in (outcome.error or "")


async def test_run_worker_increments_depth_then_restores(
    base_config: AgentConfig, tmp_path
) -> None:
    seen: list[int] = []

    async def capture(*_a, **_k):
        seen.append(current_depth())
        msg = MagicMock()
        msg.content = "done"
        return {"messages": [msg]}

    child = MagicMock()
    child.ainvoke = capture
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        outcome = await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="x",
            goal="go",
            budget_check=False,
        )
    assert outcome.status == "completed"
    assert seen == [1]
    assert current_depth() == 0


# ------------------------------------------------------ model readiness / no keys


async def test_run_worker_needs_setup_without_api_key(
    base_config: AgentConfig, tmp_path, monkeypatch
) -> None:
    """A fresh deploy with no provider key degrades gracefully — no build, no
    crash, an actionable message instead."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    build_mock = AsyncMock()
    with patch("olano.core.builder.build_agent_async", new=build_mock):
        outcome = await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="x",
            goal="do it",
            budget_check=False,
        )
    assert outcome.status == "needs_setup"
    assert "ANTHROPIC_API_KEY" in (outcome.error or "")
    build_mock.assert_not_awaited()  # never even attempts to build the model


async def test_run_worker_needs_setup_when_model_unset(base_config: AgentConfig, tmp_path) -> None:
    cfg = base_config.model_copy(update={"model": ""})
    build_mock = AsyncMock()
    with patch("olano.core.builder.build_agent_async", new=build_mock):
        outcome = await run_worker(
            parent_config=cfg,
            workspace_root=tmp_path,
            playbook="x",
            goal="do it",
            budget_check=False,
        )
    assert outcome.status == "needs_setup"
    assert "model" in (outcome.error or "").lower()
    build_mock.assert_not_awaited()


async def test_run_worker_classifies_slipped_auth_error_as_needs_setup(
    base_config: AgentConfig, tmp_path
) -> None:
    """A provider auth error at invoke time (key present but rejected) is surfaced
    as an actionable needs_setup, not a raw traceback."""
    child = MagicMock()
    child.ainvoke = AsyncMock(
        side_effect=TypeError(
            "Could not resolve authentication method. Expected one of api_key, "
            "auth_token, or credentials to be set."
        )
    )
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        outcome = await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="x",
            goal="go",
            budget_check=False,
        )
    assert outcome.status == "needs_setup"


def test_build_worker_config_inherits_the_parents_credential_scope(
    base_config: AgentConfig,
) -> None:
    """The ephemeral child runs under a fresh name but resolves CREDENTIALS as
    its parent: vault overrides, connector records and Google tokens keyed on
    the parent's name must not silently degrade to the common tier."""
    from olano.cognition.worker import _build_worker_config

    child = _build_worker_config(
        base_config,
        system_prompt="s",
        tools_allow=None,
        extra_blocked=None,
        model=None,
        label="t",
    )
    assert child.name != base_config.name  # ephemeral, uniquely named
    assert child.credential_scope == base_config.name
    # Chain-safe: a child of a child still points at the top-level agent.
    grandchild = _build_worker_config(
        child, system_prompt="s", tools_allow=None, extra_blocked=None, model=None, label="u"
    )
    assert grandchild.credential_scope == base_config.name
    # The scope is in-memory only — never written into agent.json5.
    assert "credential_scope" not in child.model_dump()
    assert "credential_scope" not in child.model_dump(exclude={"workspace"})


def test_child_scope_resolves_the_parents_vault_override(
    base_config: AgentConfig, tmp_path
) -> None:
    from olano.cognition.worker import _build_worker_config
    from olano.tools._credentials import agent_runtime_context, get_credential
    from olano.vault import resolver
    from olano.vault.store import VaultStore

    vault = VaultStore.open(passphrase="pw", db_path=tmp_path / "vault.db", use_keyring=False)
    vault.set("ANTHROPIC_API_KEY", "sk-common")
    vault.set("ANTHROPIC_API_KEY", "sk-parent", agent=base_config.name)
    resolver.set_vault(vault)
    try:
        child = _build_worker_config(
            base_config,
            system_prompt="s",
            tools_allow=None,
            extra_blocked=None,
            model=None,
            label="t",
        )
        with agent_runtime_context(child.name, child.env, credential_scope=child.credential_scope):
            assert get_credential("ANTHROPIC_API_KEY") == "sk-parent"
        # Without the scope the fresh name sees only the common value — the
        # bug the scope exists to close.
        with agent_runtime_context(child.name, child.env):
            assert get_credential("ANTHROPIC_API_KEY") == "sk-common"
    finally:
        resolver.set_vault(None)


def test_check_model_ready_missing_key(base_config: AgentConfig, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from olano.core.model_readiness import check_model_ready

    readiness = check_model_ready(base_config)
    assert not readiness.ok
    assert readiness.error is not None
    assert readiness.credential_env is None


def test_check_model_ready_with_env_key(base_config: AgentConfig, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")
    from olano.core.model_readiness import check_model_ready

    readiness = check_model_ready(base_config)
    assert readiness.ok
    assert readiness.credential_env == {"ANTHROPIC_API_KEY": "sk-live"}


# ----------------------------------------------------------------- shell backend

from olano.cognition.worker import _build_worker_config, _shell_backend_override
from olano.config.backend_config import FilesystemBackendConfig, LocalShellBackendConfig


def test_shell_override_promotes_filesystem_to_local_shell(base_config: AgentConfig) -> None:
    assert isinstance(base_config.backend, FilesystemBackendConfig)
    override = _shell_backend_override(base_config)
    assert isinstance(override, LocalShellBackendConfig)
    assert override.root_dir == base_config.backend.root_dir
    assert override.virtual_mode == base_config.backend.virtual_mode


def test_shell_override_keeps_local_shell_unchanged(base_config: AgentConfig) -> None:
    cfg = base_config.model_copy(
        update={"backend": LocalShellBackendConfig(root_dir="workspace", virtual_mode=False)}
    )
    assert _shell_backend_override(cfg) is None  # already shell-capable → no swap


def test_build_worker_config_grants_shell_when_requested(base_config: AgentConfig) -> None:
    child = _build_worker_config(
        base_config,
        system_prompt="x",
        tools_allow=["read_file"],
        extra_blocked=None,
        model=None,
        label="prototype",
        want_shell=True,
    )
    assert isinstance(child.backend, LocalShellBackendConfig)


def test_build_worker_config_no_shell_by_default(base_config: AgentConfig) -> None:
    child = _build_worker_config(
        base_config,
        system_prompt="x",
        tools_allow=["read_file"],
        extra_blocked=None,
        model=None,
        label="brainstorm",
    )
    assert isinstance(child.backend, FilesystemBackendConfig)  # unchanged


def test_shell_override_skips_read_only_filesystem(base_config: AgentConfig) -> None:
    # A read-only agent must NOT be silently upgraded to write + shell.
    ro = base_config.model_copy(
        update={
            "backend": FilesystemBackendConfig(
                root_dir="workspace", virtual_mode=False, access_mode="read"
            )
        }
    )
    assert _shell_backend_override(ro) is None
    child = _build_worker_config(
        ro,
        system_prompt="x",
        tools_allow=["read_file"],
        extra_blocked=None,
        model=None,
        label="prototype",
        want_shell=True,
    )
    assert isinstance(child.backend, FilesystemBackendConfig)  # left read-only
    assert child.backend.access_mode == "read"


async def test_run_worker_resolves_budget_from_cognition_config(
    base_config: AgentConfig, tmp_path
) -> None:
    """Omitted max_iterations/timeout resolve from the parent's cognition
    config — the documented knob was inert for every Think phase because each
    call site silently fell back to the signature default."""
    base_config.cognition.worker_max_iterations = 33
    base_config.cognition.worker_timeout_seconds = 123
    child = _child_returning("ok")
    child.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))

    orig_wait_for = asyncio.wait_for
    seen_timeouts: list[float | None] = []

    async def _spy_wait_for(aw, timeout=None):
        seen_timeouts.append(timeout)
        return await orig_wait_for(aw, timeout=timeout)

    with (
        patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)),
        patch("asyncio.wait_for", new=_spy_wait_for),
    ):
        await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="p",
            goal="g",
        )
    cfg = child.ainvoke.call_args.kwargs["config"]
    assert cfg["recursion_limit"] == 33
    assert 123.0 in seen_timeouts


async def test_run_worker_explicit_budget_beats_cognition_config(
    base_config: AgentConfig, tmp_path
) -> None:
    """An explicit caller budget (delegate_task skill frontmatter, the skill
    librarian's size-scaled cap) always wins over the cognition config."""
    base_config.cognition.worker_max_iterations = 33
    child = _child_returning("ok")
    child.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="p",
            goal="g",
            max_iterations=7,
        )
    assert child.ainvoke.call_args.kwargs["config"]["recursion_limit"] == 7


# ------------------------------------------------- two-tier budget resolution


async def test_run_worker_inherits_the_home_wide_budget(
    base_config: AgentConfig, tmp_path, monkeypatch
) -> None:
    """An agent with no opinion runs on the All-agents value.

    This is the case a plain ``int`` default made unreachable: the field used to
    be ``worker_max_iterations: int = 40``, so ``getattr`` returned 40 whether
    or not the key was on disk, and a fleet-wide raise could never reach a
    single agent.
    """
    from olano.config.system_settings import SystemSettings

    assert base_config.cognition.worker_max_iterations is None  # no opinion
    monkeypatch.setattr(
        "olano.config.system_settings.load_system_settings",
        lambda *a, **k: SystemSettings(worker_max_iterations=250, worker_timeout_seconds=3000),
    )
    child = _child_returning("ok")
    child.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))

    orig_wait_for = asyncio.wait_for
    seen: list[float | None] = []

    async def _spy_wait_for(aw, timeout=None):
        seen.append(timeout)
        return await orig_wait_for(aw, timeout=timeout)

    with (
        patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)),
        patch("asyncio.wait_for", new=_spy_wait_for),
    ):
        await run_worker(parent_config=base_config, workspace_root=tmp_path, playbook="p", goal="g")
    assert child.ainvoke.call_args.kwargs["config"]["recursion_limit"] == 250
    assert 3000.0 in seen


async def test_run_worker_defaults_when_neither_tier_is_set(
    base_config: AgentConfig, tmp_path
) -> None:
    """Nothing configured anywhere lands on the shipped default, not 40."""
    from olano.security.delegate_guards import DEFAULT_MAX_ITERATIONS

    child = _child_returning("ok")
    child.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        await run_worker(parent_config=base_config, workspace_root=tmp_path, playbook="p", goal="g")
    limit = child.ainvoke.call_args.kwargs["config"]["recursion_limit"]
    assert limit == DEFAULT_MAX_ITERATIONS == 100


# --------------------------------------------------- operator notifications


def _capture_notifications(monkeypatch) -> tuple[list[dict], list[str]]:
    import olano.runtime.notifications as notifications

    raised: list[dict] = []
    resolved: list[str] = []
    monkeypatch.setattr(notifications, "notify", lambda **kw: raised.append(kw) or None)
    monkeypatch.setattr(notifications, "resolve", lambda key: resolved.append(key) or True)
    return raised, resolved


async def test_step_budget_exhaustion_rings_the_bell_without_the_raw_advice(
    base_config: AgentConfig, tmp_path, monkeypatch
) -> None:
    """The reported gap: this failure only ever reached the Cortex Activity
    drawer, so nobody saw it. It must reach the bell — carrying the classified
    sentence, never LangGraph's advice to set a config key no user can set —
    once it has repeated: a single blown budget is held (a longer run may not
    repeat it), the second consecutive one rings."""
    raised, _resolved = _capture_notifications(monkeypatch)
    child = MagicMock()
    child.ainvoke = AsyncMock(
        side_effect=Exception(
            "Recursion limit of 100 reached without hitting a stop condition. "
            "You can increase the limit by setting the `recursion_limit` config key."
        )
    )
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        outcome = await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="p",
            goal="g",
            worker_label="research_note",
        )
        assert outcome.status == "error"
        assert "recursion_limit" not in (outcome.error or "")
        assert raised == [], "a first failure is held for a repeat"
        outcome = await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="p",
            goal="g",
            worker_label="research_note",
        )

    assert outcome.status == "error"
    assert len(raised) == 1
    row = raised[0]
    assert "Failed 2 runs in a row" in row["body"]
    assert row["kind"] == "cognition_worker_failed"
    assert row["level"] == "warn"
    assert "research_note" in row["title"]
    assert row["agent"] == base_config.name
    # One row per (agent, worker): a phase failing every six hours must show as
    # one row with a rising count, not forty rows.
    assert row["dedupe_key"] == f"cognition_worker_failed:{base_config.name}:research_note"
    assert row["action"]["href"] == f"/cortex?agent={base_config.name}"
    assert "recursion_limit" not in row["body"]


async def test_a_successful_run_clears_a_previous_failure(
    base_config: AgentConfig, tmp_path, monkeypatch
) -> None:
    """The bell reflects the world, not the history."""
    raised, resolved = _capture_notifications(monkeypatch)
    child = _child_returning("all good")
    child.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        outcome = await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="p",
            goal="g",
            worker_label="research_note",
        )
    assert outcome.status == "completed"
    assert raised == []
    assert resolved == [f"cognition_worker_failed:{base_config.name}:research_note"]


async def test_notify_operator_false_stays_silent(
    base_config: AgentConfig, tmp_path, monkeypatch
) -> None:
    """delegate_task's failure is already in a live turn the user is watching."""
    raised, resolved = _capture_notifications(monkeypatch)
    child = MagicMock()
    child.ainvoke = AsyncMock(side_effect=Exception("boom"))
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        await run_worker(
            parent_config=base_config,
            workspace_root=tmp_path,
            playbook="p",
            goal="g",
            notify_operator=False,
        )
    assert raised == [] and resolved == []


async def test_a_guard_rail_outcome_does_not_ring(
    base_config: AgentConfig, tmp_path, monkeypatch
) -> None:
    """Depth rejection is the guard doing its job and clears on its own —
    ringing for it would train the operator to ignore the bell."""
    raised, resolved = _capture_notifications(monkeypatch)
    token = DEPTH_VAR.set(MAX_DEPTH)
    try:
        outcome = await run_worker(
            parent_config=base_config, workspace_root=tmp_path, playbook="p", goal="g"
        )
    finally:
        DEPTH_VAR.reset(token)
    assert outcome.status == "rejected"
    assert raised == [] and resolved == []


async def test_a_success_between_failures_resets_the_streak(
    base_config: AgentConfig, tmp_path, monkeypatch
) -> None:
    """fail → succeed → fail is two one-offs, not a streak: no bell."""
    raised, resolved = _capture_notifications(monkeypatch)
    failing = MagicMock()
    failing.ainvoke = AsyncMock(side_effect=Exception("boom"))
    ok = _child_returning("fine")
    ok.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    kw = dict(parent_config=base_config, workspace_root=tmp_path, playbook="p", goal="g")
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=failing)):
        await run_worker(**kw)
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=ok)):
        await run_worker(**kw)
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=failing)):
        await run_worker(**kw)
    assert raised == []
    assert resolved == [f"cognition_worker_failed:{base_config.name}:worker"]


async def test_an_operator_remedy_rings_on_the_first_run(
    base_config: AgentConfig, tmp_path, monkeypatch
) -> None:
    """An expired sign-in never heals by itself — holding it for a streak would
    only delay the person who has to act."""
    raised, _resolved = _capture_notifications(monkeypatch)
    cfg = base_config.model_copy(update={"model": "openai-codex:gpt-5.4-mini"})
    child = MagicMock()
    child.ainvoke = AsyncMock(
        side_effect=Exception("401 unauthorized: your sign-in has expired, please sign in again")
    )
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        outcome = await run_worker(
            parent_config=cfg, workspace_root=tmp_path, playbook="p", goal="g"
        )
    assert outcome.status in ("error", "needs_setup")
    assert len(raised) == 1
    assert "in a row" not in raised[0]["body"]


async def test_bell_streak_threshold_env_restores_ring_on_first(
    base_config: AgentConfig, tmp_path, monkeypatch
) -> None:
    raised, _resolved = _capture_notifications(monkeypatch)
    monkeypatch.setenv("LIONCLAW_COGNITION_BELL_STREAK", "1")
    child = MagicMock()
    child.ainvoke = AsyncMock(side_effect=Exception("rate limit exceeded (429)"))
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        await run_worker(parent_config=base_config, workspace_root=tmp_path, playbook="p", goal="g")
    assert len(raised) == 1


async def test_a_guard_rail_outcome_leaves_the_streak_alone(
    base_config: AgentConfig, tmp_path, monkeypatch
) -> None:
    """budget_blocked between two real failures is not a success: the streak
    carries over and the second real failure rings."""
    from olano.cognition.reliability import failure_streak

    raised, _resolved = _capture_notifications(monkeypatch)
    child = MagicMock()
    child.ainvoke = AsyncMock(side_effect=Exception("boom"))
    kw = dict(parent_config=base_config, workspace_root=tmp_path, playbook="p", goal="g")
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        await run_worker(**kw)
    assert failure_streak(tmp_path, "worker") == 1
    token = DEPTH_VAR.set(MAX_DEPTH)
    try:
        blocked = await run_worker(**kw)
    finally:
        DEPTH_VAR.reset(token)
    assert blocked.status == "rejected"
    assert failure_streak(tmp_path, "worker") == 1
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        await run_worker(**kw)
    assert len(raised) == 1


# ------------------------------------------------------ concurrency slot


async def test_background_runs_are_bounded_by_the_slot(
    base_config: AgentConfig, tmp_path, monkeypatch
) -> None:
    """Two background workers on a limit of 1 never overlap, and the one that
    queued does not pay the wait out of its own clock."""
    import olano.cognition.reliability as reliability

    reliability.reset_slots()
    monkeypatch.setattr(reliability, "effective_worker_concurrency", lambda: 1)
    live = 0
    peak = 0

    async def _slow(*_a, **_k):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.2)
        live -= 1
        msg = MagicMock()
        msg.content = "done"
        return {"messages": [msg]}

    child = MagicMock()
    child.ainvoke = AsyncMock(side_effect=_slow)
    child.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    kw = dict(parent_config=base_config, workspace_root=tmp_path, playbook="p", goal="g")
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        a, b = await asyncio.gather(run_worker(**kw), run_worker(**kw))
    assert peak == 1
    assert a.status == b.status == "completed"
    # A run that counted its queue time would report ≥ 0.4s.
    assert max(a.duration_seconds, b.duration_seconds) < 0.35
    reliability.reset_slots()


async def test_delegate_subagents_bypass_the_slot(
    base_config: AgentConfig, tmp_path, monkeypatch
) -> None:
    """A `sub__*` worker runs inside a live turn and has its own fan-out cap —
    an interactive reply must never queue behind Cortex."""
    import olano.cognition.reliability as reliability

    reliability.reset_slots()
    monkeypatch.setattr(reliability, "effective_worker_concurrency", lambda: 1)
    live = 0
    peak = 0

    async def _slow(*_a, **_k):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.1)
        live -= 1
        msg = MagicMock()
        msg.content = "done"
        return {"messages": [msg]}

    child = MagicMock()
    child.ainvoke = AsyncMock(side_effect=_slow)
    child.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    kw = dict(
        parent_config=base_config,
        workspace_root=tmp_path,
        playbook="p",
        goal="g",
        worker_label="sub__research",
        notify_operator=False,
    )
    with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
        await asyncio.gather(run_worker(**kw), run_worker(**kw))
    assert peak == 2
    reliability.reset_slots()
