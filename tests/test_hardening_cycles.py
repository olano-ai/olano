"""Tests for the hardening sprint introduced in ADR-001.

Covers the new abstractions delivered across cycles 1–8:

* JSONL session schema versioning (cycle 1)
* CheckpointerFactory + default-factory swap (cycle 2)
* Provider health diagnostics (cycle 3)
* atomic_write_text + MarkdownMemory durability (cycle 4)
* SelfLearningService failure tally + status() (cycle 5)
* Provider-aware error classifier registry (cycle 6)
* CommandDispatcher.register / instance overlay (cycle 7)
* OlanoScheduler lock invariants (cycle 8)
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ─────────────────────────── Cycle 1: JSONL versioning ────────────────────────


def test_session_schema_version_constant_is_one() -> None:
    from olano.core.runner import SESSION_SCHEMA_VERSION

    assert SESSION_SCHEMA_VERSION == 1


def test_load_session_marks_legacy_lines_as_v0(tmp_path: Path, base_config) -> None:
    from olano.config.workspace import get_workspace
    from olano.core.runner import AgentRunner

    runner = AgentRunner(agent=MagicMock(), config=base_config)
    workspace = get_workspace(base_config.workspace)
    sessions_dir = workspace.agent_session_dir(base_config.name)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    p = sessions_dir / "thread-legacy.jsonl"
    legacy = json.dumps({"ts": "2024-01-01T00:00:00Z", "role": "user", "content": "old"})
    current = json.dumps(
        {"schema_version": 1, "ts": "2026-05-06T00:00:00Z", "role": "assistant", "content": "new"}
    )
    p.write_text(legacy + "\n" + current + "\n", encoding="utf-8")

    entries = runner.load_session("thread-legacy")
    assert len(entries) == 2
    assert entries[0]["schema_version"] == 0
    assert entries[1]["schema_version"] == 1


def test_load_session_skips_malformed_lines(tmp_path: Path, base_config) -> None:
    from olano.config.workspace import get_workspace
    from olano.core.runner import AgentRunner

    runner = AgentRunner(agent=MagicMock(), config=base_config)
    workspace = get_workspace(base_config.workspace)
    sessions_dir = workspace.agent_session_dir(base_config.name)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    p = sessions_dir / "thread-broken.jsonl"
    p.write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "content": "fine"}),
                "{not json",
                json.dumps({"role": "assistant", "content": "also fine"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    entries = runner.load_session("thread-broken")
    assert [e["role"] for e in entries] == ["user", "assistant"]


def test_append_session_writes_schema_version(tmp_path: Path, base_config) -> None:
    from olano.config.workspace import get_workspace
    from olano.core.runner import AgentRunner

    runner = AgentRunner(agent=MagicMock(), config=base_config)
    runner._append_session("t1", "user", "hello")
    runner.append_session_entry("t1", {"role": "assistant", "content": "world"})

    workspace = get_workspace(base_config.workspace)
    p = workspace.agent_session_dir(base_config.name) / "t1.jsonl"
    lines = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    assert all(line["schema_version"] == 1 for line in lines)


# ─────────────────────────── Cycle 2: CheckpointerFactory ─────────────────────


def test_checkpointer_factory_swap_round_trip() -> None:
    from olano.core.checkpoints import (
        CheckpointerFactory,
        get_default_factory,
        set_default_factory,
    )

    new_factory = CheckpointerFactory()
    previous = set_default_factory(new_factory)
    try:
        assert get_default_factory() is new_factory
    finally:
        set_default_factory(previous)
    assert get_default_factory() is previous


def test_checkpointer_factory_reset_is_idempotent() -> None:
    from olano.core.checkpoints import CheckpointerFactory

    f = CheckpointerFactory()
    # No-op when nothing is cached.
    f.reset()
    f.reset()
    # Internal dicts are still present and empty.
    assert f._sync_savers == {}
    assert f._async_savers == {}


def test_module_level_helpers_delegate_to_default_factory() -> None:
    from olano.core import checkpoints

    fake = MagicMock()
    fake.get.return_value = "saver"
    previous = checkpoints.set_default_factory(fake)
    try:
        result = checkpoints.get_checkpointer("agent-x")
        assert result == "saver"
        fake.get.assert_called_once_with("agent-x", None)
    finally:
        checkpoints.set_default_factory(previous)


# ─────────────────────────── Cycle 3: Provider health ─────────────────────────


def test_provider_health_unknown_key() -> None:
    from olano.models import check_provider_health, reset_provider_health

    reset_provider_health()
    status, _ = check_provider_health("not-a-real-provider")
    assert status == "unknown"


def test_provider_health_caches_result(monkeypatch: pytest.MonkeyPatch) -> None:
    from olano.models import (
        check_provider_health,
        reset_provider_health,
    )

    reset_provider_health()
    calls = {"n": 0}

    real_find_spec = __import__("importlib").util.find_spec

    def counting_find_spec(name, *a, **k):
        calls["n"] += 1
        return real_find_spec(name, *a, **k)

    monkeypatch.setattr("importlib.util.find_spec", counting_find_spec)
    check_provider_health("anthropic")
    first = calls["n"]
    check_provider_health("anthropic")
    assert calls["n"] == first  # cached, no second find_spec call


def test_provider_health_report_covers_all_providers() -> None:
    from olano.models import PROVIDERS, provider_health_report

    report = provider_health_report()
    assert set(report.keys()) == set(PROVIDERS.keys())
    for entry in report.values():
        assert entry["status"] in {"ok", "missing", "broken", "no_key", "skipped", "unknown"}


# ─────────────────────────── Cycle 4: atomic writes ───────────────────────────


def test_atomic_write_text_overwrite(tmp_path: Path) -> None:
    from olano.memory.markdown_memory import atomic_write_text

    p = tmp_path / "out.md"
    atomic_write_text(p, "first")
    assert p.read_text() == "first"
    atomic_write_text(p, "second")
    assert p.read_text() == "second"
    # No temp leftovers — only check for .tmp siblings, not full dir contents
    # (lionclaw_home autouse fixture also creates files in tmp_path).
    assert not any(x.suffix == ".tmp" for x in tmp_path.iterdir())


def test_atomic_write_text_keeps_existing_mode(tmp_path: Path) -> None:
    # mkstemp creates the staging file 0600; the rename used to carry that
    # onto MEMORY.md, which took the file away from the sandboxed agent.
    from olano.memory.markdown_memory import atomic_write_text

    p = tmp_path / "MEMORY.md"
    p.write_text("first")
    p.chmod(0o664)
    atomic_write_text(p, "second")
    assert p.read_text() == "second"
    assert p.stat().st_mode & 0o777 == 0o664

    private = tmp_path / "secret.json"
    private.write_text("{}")
    private.chmod(0o600)
    atomic_write_text(private, "{}")
    assert private.stat().st_mode & 0o777 == 0o600


def test_atomic_write_text_hands_workspace_files_to_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A runtime write inside a live sandbox's workspace belongs to the agent
    # (uid 1000) by default, directories the write created included; a write
    # anywhere else is left alone.
    from olano.backends.lionclaw_docker import OlanoDockerBackend
    from olano.core import builder
    from olano.memory.markdown_memory import MarkdownMemory, atomic_write_text

    workspace = tmp_path / "ws"
    workspace.mkdir()
    live = OlanoDockerBackend.__new__(OlanoDockerBackend)
    live._workspace_root = workspace
    live._container_id = "abc123"
    live._container_name = None
    monkeypatch.setitem(builder._SANDBOX_BACKENDS, "sandboxed-agent", live)

    handed: list[Path] = []
    monkeypatch.setattr(builder, "chown_for_sandbox", lambda p: handed.append(Path(p)) or True)

    atomic_write_text(workspace / "memories" / "2026-01-01.md", "x")
    assert handed == [workspace / "memories", workspace / "memories" / "2026-01-01.md"]

    handed.clear()
    atomic_write_text(workspace / "MEMORY.md", "y")  # a rewrite hands over too
    atomic_write_text(workspace / "MEMORY.md", "z")
    assert handed == [workspace / "MEMORY.md"] * 2

    handed.clear()
    atomic_write_text(tmp_path / "config.yaml", "outside: true")
    assert handed == []

    # MarkdownMemory needs nothing of its own: its writes go through the same seam.
    handed.clear()
    other = tmp_path / "ws2"
    live._workspace_root = other
    MarkdownMemory(other).append_daily("hello")
    assert other / "MEMORY.md" in handed
    assert other / "memories" in handed


def test_atomic_write_text_failure_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olano.memory import markdown_memory as mm

    target = tmp_path / "out.md"
    target.write_text("original")

    def boom(*_a, **_k):
        raise RuntimeError("simulated rename failure")

    monkeypatch.setattr(mm.os, "replace", boom)

    with pytest.raises(RuntimeError):
        mm.atomic_write_text(target, "new content")

    assert target.read_text() == "original"
    assert not any(x.suffix == ".tmp" for x in tmp_path.iterdir())


def test_markdown_memory_write_uses_atomic_path(tmp_workspace: Path) -> None:
    from olano.memory.markdown_memory import MarkdownMemory

    mm = MarkdownMemory(tmp_workspace)
    mm.write("# fresh content")
    assert mm.read() == "# fresh content"


# ─────────────────────────── Cycle 5: self-learning failures ─────────────────


def test_self_learning_record_failure_increments_and_logs(
    tmp_workspace: Path, base_config, caplog: pytest.LogCaptureFixture
) -> None:
    from olano.memory.markdown_memory import MarkdownMemory
    from olano.memory.self_learning import SelfLearningService

    svc = SelfLearningService(
        agent_config=base_config,
        workspace_root=tmp_workspace,
        markdown_memory=MarkdownMemory(tmp_workspace),
        rag_memory=None,
    )
    with caplog.at_level("WARNING", logger="olano.memory.self_learning"):
        svc._record_failure("distill_llm", ValueError("boom"))
        svc._record_failure("distill_llm", ValueError("boom again"))

    status = svc.status()
    assert status["failures"]["distill_llm"] == 2
    assert status["last_failure"] == ["distill_llm", "ValueError: boom again"]


# ─────────────────────────── Cycle 6: error classifier ────────────────────────


def test_classify_error_provider_overlay() -> None:
    from olano.core.runner import _classify_error

    err = Exception("Hit insufficient_quota for org")
    assert "OpenAI billing" in _classify_error(err, provider="openai")
    # No provider hint → falls through to the generic catch-all.
    assert "OpenAI billing" not in _classify_error(err)


def test_classify_error_register_and_reset() -> None:
    from olano.core.runner import (
        _classify_error,
        _has_any,
        register_provider_error_rules,
        reset_provider_error_rules,
    )

    reset_provider_error_rules()
    register_provider_error_rules(
        "groq",
        [(_has_any("model_not_loaded"), "Groq model warming up")],
    )
    try:
        msg = _classify_error(Exception("model_not_loaded"), provider="groq")
        assert msg == "Groq model warming up"
    finally:
        reset_provider_error_rules()
    # OpenAI overlay registered at import time should still apply, but groq is gone.
    msg2 = _classify_error(Exception("model_not_loaded"), provider="groq")
    assert "Groq" not in msg2


def test_classify_error_keeps_anthropic_messages() -> None:
    from olano.core.runner import _classify_error

    assert "API credits" in _classify_error(Exception("Your credit balance is too low"))
    assert "Rate limit" in _classify_error(Exception("rate_limit reached"))
    assert "Context window" in _classify_error(Exception("context_length_exceeded"))


# ─────────────────────────── Cycle 7: CommandDispatcher.register ──────────────


def test_command_dispatcher_register_overlay() -> None:
    from olano.cli.commands import CommandDispatcher, CommandResult

    d = CommandDispatcher()

    async def handler(self, args, ctx):
        return CommandResult(output=f"echoed:{','.join(args)}")

    d.register("/echo", handler, aliases=("/e",))
    # Aliases share the handler.
    assert d._resolve_handler("/echo") is handler
    assert d._resolve_handler("/e") is handler
    # Built-ins still resolve.
    assert d._resolve_handler("/help") is not None


def test_command_dispatcher_register_rejects_collision() -> None:
    from olano.cli.commands import CommandDispatcher

    d = CommandDispatcher()
    with pytest.raises(ValueError):
        d.register("/help", lambda *_: None)
    # override=True replaces.
    d.register("/help", lambda *_: None, override=True)


def test_command_dispatcher_unregister() -> None:
    from olano.cli.commands import CommandDispatcher

    d = CommandDispatcher()
    d.register("/temp", lambda *_: None)
    assert d._resolve_handler("/temp") is not None
    d.unregister("/temp")
    assert d._resolve_handler("/temp") is None


def test_command_dispatcher_register_requires_slash_prefix() -> None:
    from olano.cli.commands import CommandDispatcher

    d = CommandDispatcher()
    with pytest.raises(ValueError):
        d.register("noprefix", lambda *_: None)


# ─────────────────────────── Cycle 8: scheduler lock ──────────────────────────


def test_scheduler_lock_serializes_register_unregister(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two threads racing on register/unregister must not corrupt _jobs."""
    from olano.scheduler.cron import OlanoScheduler

    sched = OlanoScheduler()
    # Stub APScheduler to avoid the real loop.
    sched._scheduler = MagicMock()
    sched._scheduler.add_job.return_value = MagicMock(id="j-stub")

    fake_runner = MagicMock()

    cfg_a = MagicMock()
    cfg_a.name = "alpha"
    cfg_a.schedule = []  # nothing to register; we just want to exercise the lock
    cfg_b = MagicMock()
    cfg_b.name = "beta"
    cfg_b.schedule = []

    errors: list[Exception] = []

    def hammer(cfg):
        try:
            for _ in range(50):
                sched.register_agent(cfg, fake_runner, workspace_root=None)
                sched.unregister_agent(cfg.name)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=hammer, args=(cfg_a,))
    t2 = threading.Thread(target=hammer, args=(cfg_b,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == []
    # Both agents end unregistered; _jobs dict has no leftover keys.
    assert sched._jobs == {}
    assert sched._runners == {}
