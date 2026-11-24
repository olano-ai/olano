from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from olano.cli.main import cli
from olano.cli.non_interactive import is_shell_command_allowed, parse_shell_allow_list
from olano.config.agent_config import AgentConfig, MemoryConfig


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def _make_config(tmp_path: Path, name: str = "tester") -> AgentConfig:
    from olano.config.backend_config import FilesystemBackendConfig

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "MEMORY.md").write_text("memory", encoding="utf-8")
    (workspace / "memories").mkdir(exist_ok=True)
    return AgentConfig(
        name=name,
        description="Test agent",
        model="anthropic:claude-haiku-4-5-20251001",
        system_prompt="You are a test assistant.",
        workspace=str(workspace),
        memory=MemoryConfig(markdown=True, rag=False, self_learning=False),
        backend=FilesystemBackendConfig(root_dir=str(workspace)),
    )


class _DummyRunner:
    events: list[tuple[str, object]] = []
    approval_actions: list[dict] | None = None
    last_message: str = ""
    last_thread_id: str | None = None
    last_decisions: list[dict] | None = None

    def __init__(self, agent, config) -> None:
        self.agent = agent
        self.config = config

    async def astream_events(self, message, thread_id=None, approval_handler=None):
        type(self).last_message = message
        type(self).last_thread_id = thread_id
        type(self).last_decisions = None
        if approval_handler and type(self).approval_actions is not None:
            type(self).last_decisions = await approval_handler(type(self).approval_actions)
        for event in type(self).events:
            yield event


def _patch_non_interactive_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    config: AgentConfig | None = None,
) -> None:
    active_config = config or _make_config(tmp_path)
    monkeypatch.setattr(
        "olano.config.loader.load_agent_config",
        lambda name, source="auto": active_config.model_copy(deep=True),
    )

    async def _fake_build(config):
        return object()

    monkeypatch.setattr("olano.core.builder.build_agent_async", _fake_build)
    monkeypatch.setattr("olano.core.builder.build_agent_with_mcp", _fake_build)
    monkeypatch.setattr("olano.core.builder.stop_rag_watchers", lambda: None)
    monkeypatch.setattr("olano.core.checkpoints.aclose_checkpointers", AsyncMock())
    monkeypatch.setattr("olano.core.runner.AgentRunner", _DummyRunner)


def test_parse_shell_allow_list_supports_exec_prefix_exact_and_all() -> None:
    rules = parse_shell_allow_list(
        "exec:rg,prefix:git status,exact:uv run pytest tests/test_security.py -q,all"
    )

    assert is_shell_command_allowed("rg foo", rules)
    assert is_shell_command_allowed("git status --short", rules)
    assert is_shell_command_allowed("uv run pytest tests/test_security.py -q", rules)
    assert is_shell_command_allowed("python -V", rules)


@pytest.mark.asyncio
async def test_run_non_interactive_rejects_shell_without_allow_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from olano.cli.non_interactive import run_non_interactive

    _DummyRunner.events = [("text", "done")]
    _DummyRunner.approval_actions = [
        {"name": "write_file", "interrupt_id": "w1", "args": {"path": "note.txt"}},
        {"name": "execute", "interrupt_id": "s1", "args": {"command": "pwd"}},
    ]
    _patch_non_interactive_runtime(monkeypatch, tmp_path)

    exit_code = await run_non_interactive("tester", "hello", quiet=True)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "done\n"
    assert _DummyRunner.last_decisions == [
        {"type": "approve"},
        {
            "type": "reject",
            "message": "Shell commands are not permitted in non-interactive mode without --shell-allow-list.",
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "command"),
    [
        ("exec:rg", "rg foo"),
        ("prefix:git status", "git status --short"),
        (
            "exact:uv run pytest tests/test_security.py -q",
            "uv run pytest tests/test_security.py -q",
        ),
        ("all", "python -V"),
    ],
)
async def test_run_non_interactive_accepts_shell_via_allow_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    spec: str,
    command: str,
) -> None:
    from olano.cli.non_interactive import run_non_interactive

    _DummyRunner.events = []
    _DummyRunner.approval_actions = [
        {"name": "execute", "interrupt_id": "s1", "args": {"command": command}}
    ]
    _patch_non_interactive_runtime(monkeypatch, tmp_path)

    exit_code = await run_non_interactive("tester", "hello", quiet=True, shell_allow_list=spec)

    assert exit_code == 0
    assert _DummyRunner.last_decisions == [{"type": "approve"}]


def test_cli_non_interactive_quiet_suppresses_diagnostics_on_terminal_stderr(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _DummyRunner.events = [
        ("tool_call", {"name": "read_file"}),
        ("status", {"message": "Compacting thread context. Preparing a fresh thread…"}),
        ("text", "hello"),
    ]
    _DummyRunner.approval_actions = None
    _patch_non_interactive_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr("olano.cli.non_interactive._quiet_diagnostics_enabled", lambda: False)

    result = cli_runner.invoke(cli, ["--agent", "tester", "-n", "say hi", "--quiet"])

    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert result.stderr == ""


def test_cli_non_interactive_quiet_keeps_pipe_safe_stderr_when_redirected(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _DummyRunner.events = [
        ("tool_call", {"name": "read_file"}),
        ("status", {"message": "Compacting thread context. Preparing a fresh thread…"}),
        ("text", "hello"),
    ]
    _DummyRunner.approval_actions = None
    _patch_non_interactive_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr("olano.cli.non_interactive._quiet_diagnostics_enabled", lambda: True)

    result = cli_runner.invoke(cli, ["--agent", "tester", "-n", "say hi", "--quiet"])

    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert "Calling tool: read_file" in result.stderr
    assert "Compacting thread context. Preparing a fresh thread" in result.stderr


def test_cli_non_interactive_no_stream_buffers_output(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _DummyRunner.events = [("text", "hello "), ("text", "world")]
    _DummyRunner.approval_actions = None
    _patch_non_interactive_runtime(monkeypatch, tmp_path)

    result = cli_runner.invoke(cli, ["--agent", "tester", "-n", "say hi", "--quiet", "--no-stream"])

    assert result.exit_code == 0
    assert result.stdout == "hello world\n"


def test_chat_subcommand_accepts_non_interactive_flags(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _DummyRunner.events = [("text", "hello")]
    _DummyRunner.approval_actions = None
    _patch_non_interactive_runtime(monkeypatch, tmp_path)

    result = cli_runner.invoke(cli, ["chat", "--agent", "tester", "-n", "say hi", "--quiet"])

    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert _DummyRunner.last_message == "say hi"


def test_cli_stdin_only_promotes_to_non_interactive(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _DummyRunner.events = [("text", "done")]
    _DummyRunner.approval_actions = None
    _patch_non_interactive_runtime(monkeypatch, tmp_path)

    result = cli_runner.invoke(cli, ["--agent", "tester", "--quiet"], input="from stdin")

    assert result.exit_code == 0
    assert _DummyRunner.last_message == "from stdin"
    assert result.stdout == "done\n"


def test_cli_stdin_and_non_interactive_merge(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _DummyRunner.events = [("text", "done")]
    _DummyRunner.approval_actions = None
    _patch_non_interactive_runtime(monkeypatch, tmp_path)

    result = cli_runner.invoke(
        cli, ["--agent", "tester", "-n", "summarize", "--quiet"], input="context"
    )

    assert result.exit_code == 0
    assert _DummyRunner.last_message == "context\n\nsummarize"


def test_cli_stdin_and_message_merge(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}

    async def _fake_send(agent_name, message, thread_id=None, stream=False, model_override=None):
        captured["agent"] = agent_name
        captured["message"] = message

    monkeypatch.setattr("olano.cli.main._send_message", _fake_send)
    monkeypatch.setattr("olano.cli.main._default_agent_name", lambda: "merli")

    result = cli_runner.invoke(cli, ["-m", "explain this"], input="error output")

    assert result.exit_code == 0
    assert captured == {"agent": "merli", "message": "error output\n\nexplain this"}


def test_cli_quiet_requires_non_interactive_or_piped_input(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(cli, ["--agent", "tester", "--quiet"])

    assert result.exit_code != 0
    assert "require --non-interactive (-n) or piped stdin" in result.output
