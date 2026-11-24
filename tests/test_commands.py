"""Tests for CommandDispatcher — slash commands."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from olano.cli.commands import CommandContext, CommandDispatcher


@pytest.fixture
def dispatcher() -> CommandDispatcher:
    return CommandDispatcher()


@pytest.fixture
def ctx(base_config, mock_agent, tmp_workspace) -> CommandContext:
    from olano.core.runner import AgentRunner

    runner = AgentRunner(mock_agent, base_config)
    return CommandContext(
        config=base_config,
        runner=runner,
        thread_id="test-thread-001",
        verbosity="normal",
        think_level="off",
    )


class TestCommandDetection:
    def test_slash_command(self, dispatcher: CommandDispatcher) -> None:
        assert dispatcher.is_command("/help") is True

    def test_bang_is_not_a_command(self, dispatcher: CommandDispatcher) -> None:
        # "!" was the removed terminal chat's shell prefix; on every remaining
        # surface it is ordinary text.
        assert dispatcher.is_command("! ls") is False

    def test_normal_message(self, dispatcher: CommandDispatcher) -> None:
        assert dispatcher.is_command("hello there") is False

    def test_parse_command_and_args(self, dispatcher: CommandDispatcher) -> None:
        cmd, args = dispatcher.parse("/model anthropic:claude-sonnet-4-6")
        assert cmd == "/model"
        assert args == ["anthropic:claude-sonnet-4-6"]

    def test_strip_directives(self, dispatcher: CommandDispatcher) -> None:
        clean, directives = dispatcher.strip_directives("Hello /think high world")
        assert "/think" not in clean
        assert directives.get("/think") == "high"


class TestHelpCommand:
    @pytest.mark.asyncio
    async def test_help(self, dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
        result = await dispatcher.dispatch("/help", ctx)
        assert "/help" in result.output


class TestStatusCommand:
    @pytest.mark.asyncio
    async def test_status(self, dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
        result = await dispatcher.dispatch("/status", ctx)
        assert "Agent" in result.output
        assert "Thread" in result.output
        assert "test-agent" in result.output


class TestMemCommand:
    @pytest.mark.asyncio
    async def test_mem_report(self, dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
        result = await dispatcher.dispatch("/mem", ctx)
        assert "Memory" in result.output

    @pytest.mark.asyncio
    async def test_mem_monitor_toggle_graceful(
        self, dispatcher: CommandDispatcher, ctx: CommandContext
    ) -> None:
        # No live runtime + no daemon in the test process → a graceful status
        # line, never a crash.
        result = await dispatcher.dispatch("/mem status", ctx)
        assert "Memory monitor" in result.output


class TestNewSessionCommand:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/new", "/clear"])
    async def test_new_creates_new_thread(
        self, dispatcher: CommandDispatcher, ctx: CommandContext, command: str
    ) -> None:
        old_thread = ctx.thread_id
        result = await dispatcher.dispatch(command, ctx)
        assert result.new_thread_id is not None
        assert result.new_thread_id != old_thread


class TestThinkCommand:
    @pytest.mark.asyncio
    async def test_set_think_level(
        self, dispatcher: CommandDispatcher, ctx: CommandContext
    ) -> None:
        result = await dispatcher.dispatch("/think high", ctx)
        assert "high" in result.output
        assert ctx.think_level == "high"

    @pytest.mark.asyncio
    async def test_invalid_level(self, dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
        result = await dispatcher.dispatch("/think turbo", ctx)
        assert "Invalid" in result.output


class TestVerboseCommand:
    @pytest.mark.asyncio
    async def test_toggle_verbose(self, dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
        ctx.verbosity = "normal"
        result = await dispatcher.dispatch("/verbose on", ctx)
        assert ctx.verbose is True
        assert ctx.verbosity == "verbose"
        assert "verbose" in result.output

    @pytest.mark.asyncio
    async def test_toggle_off(self, dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
        ctx.verbosity = "verbose"
        result = await dispatcher.dispatch("/verbose off", ctx)
        assert ctx.verbose is False
        assert ctx.verbosity == "normal"

    @pytest.mark.asyncio
    async def test_no_arg_toggles(self, dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
        ctx.verbosity = "normal"
        await dispatcher.dispatch("/verbose", ctx)
        assert ctx.verbosity == "verbose"
        await dispatcher.dispatch("/verbose", ctx)
        assert ctx.verbosity == "normal"


class TestVerbosityCommand:
    @pytest.mark.asyncio
    async def test_show_current(self, dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
        ctx.verbosity = "quiet"
        result = await dispatcher.dispatch("/verbosity", ctx)
        assert "quiet" in result.output
        assert ctx.verbosity == "quiet"  # showing must not change it

    @pytest.mark.asyncio
    async def test_set_each_mode(self, dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
        for mode in ("quiet", "normal", "verbose"):
            result = await dispatcher.dispatch(f"/verbosity {mode}", ctx)
            assert ctx.verbosity == mode
            assert mode in result.output

    @pytest.mark.asyncio
    async def test_alias(self, dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
        await dispatcher.dispatch("/verbosity v", ctx)
        assert ctx.verbosity == "verbose"

    @pytest.mark.asyncio
    async def test_invalid(self, dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
        ctx.verbosity = "normal"
        result = await dispatcher.dispatch("/verbosity bananas", ctx)
        assert ctx.verbosity == "normal"  # unchanged
        assert "Invalid" in result.output

    @pytest.mark.asyncio
    async def test_quiet_and_normal_shortcuts(
        self, dispatcher: CommandDispatcher, ctx: CommandContext
    ) -> None:
        await dispatcher.dispatch("/quiet", ctx)
        assert ctx.verbosity == "quiet"
        await dispatcher.dispatch("/normal", ctx)
        assert ctx.verbosity == "normal"


class TestModeCommand:
    @pytest.mark.asyncio
    async def test_mode_none_defaults_to_fresh_thread(
        self, dispatcher: CommandDispatcher, ctx: CommandContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rebuilt = object()
        forked = "fork-none"
        monkeypatch.setattr(
            "olano.core.builder.build_agent_async",
            AsyncMock(return_value=rebuilt),
        )
        monkeypatch.setattr(
            ctx.runner,
            "fork_thread",
            AsyncMock(return_value=forked),
        )

        result = await dispatcher.dispatch("/mode none", ctx)

        assert result.new_thread_id == forked
        assert "fresh empty thread" in result.output
        ctx.runner.fork_thread.assert_awaited_once_with(
            "test-thread-001",
            carry_history=False,
            carry_state=False,
        )

    @pytest.mark.asyncio
    async def test_mode_none_can_carry_visible_history(
        self, dispatcher: CommandDispatcher, ctx: CommandContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rebuilt = object()
        forked = "fork-carry"
        monkeypatch.setattr(
            "olano.core.builder.build_agent_async",
            AsyncMock(return_value=rebuilt),
        )
        monkeypatch.setattr(
            ctx.runner,
            "fork_thread",
            AsyncMock(return_value=forked),
        )

        result = await dispatcher.dispatch("/mode none --carry-history", ctx)

        assert result.new_thread_id == forked
        assert "Visible history copied" in result.output
        ctx.runner.fork_thread.assert_awaited_once_with(
            "test-thread-001",
            carry_history=True,
            carry_state=True,
        )


class TestRemovedTerminalCommands:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["/stop", "/exit", "/quit", "/bash ls"])
    async def test_terminal_only_commands_are_gone(
        self, dispatcher: CommandDispatcher, ctx: CommandContext, text: str
    ) -> None:
        result = await dispatcher.dispatch(text, ctx)
        assert result.output.startswith("Unknown command")


class TestReloadAgentCommand:
    @pytest.mark.asyncio
    async def test_reload_agent_rebuilds_and_applies_config(
        self, dispatcher: CommandDispatcher, ctx: CommandContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rebuilt = object()
        fresh = ctx.config.model_copy(update={"model": "anthropic:claude-haiku-4-5"})
        monkeypatch.setattr(
            "olano.config.loader.load_agent_config",
            lambda name: fresh,
        )
        monkeypatch.setattr(
            "olano.core.builder.build_agent_async",
            AsyncMock(return_value=rebuilt),
        )

        result = await dispatcher.dispatch("/reload-agent", ctx)

        assert "Reloaded" in result.output
        # Live config object was mutated in place with the on-disk values.
        assert ctx.config.model == "anthropic:claude-haiku-4-5"
        # And the runner got the freshly built agent.
        assert ctx.runner.agent is rebuilt

    @pytest.mark.asyncio
    async def test_reload_agent_reports_config_error(
        self, dispatcher: CommandDispatcher, ctx: CommandContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(name: str):
            raise FileNotFoundError("no config")

        monkeypatch.setattr("olano.config.loader.load_agent_config", _boom)

        result = await dispatcher.dispatch("/reload-agent", ctx)

        assert "Failed to reload config" in result.output


class TestReloadVaultCommand:
    @pytest.mark.asyncio
    async def test_reload_vault_no_vault(
        self, dispatcher: CommandDispatcher, ctx: CommandContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: None)

        class _Result:
            vault = None

        monkeypatch.setattr(
            "olano.vault.runtime.unlock_vault_for_session",
            lambda **kwargs: _Result(),
        )

        result = await dispatcher.dispatch("/reload-vault", ctx)

        assert "No unlocked vault" in result.output

    @pytest.mark.asyncio
    async def test_reload_vault_rebuilds_agent(
        self, dispatcher: CommandDispatcher, ctx: CommandContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Vault:
            def list_names(self):
                return [{"name": "OPENAI_API_KEY"}]

        rebuilt = object()
        monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: _Vault())
        monkeypatch.setattr(
            "olano.vault.runtime.hydrate_env_from_vault",
            lambda vault, overwrite=False: ["OPENAI_API_KEY"],
        )
        monkeypatch.setattr(
            "olano.core.builder.build_agent_async",
            AsyncMock(return_value=rebuilt),
        )

        result = await dispatcher.dispatch("/reload-vault", ctx)

        assert "Vault reloaded" in result.output
        assert ctx.runner.agent is rebuilt


class TestUnknownCommand:
    @pytest.mark.asyncio
    async def test_unknown(self, dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
        result = await dispatcher.dispatch("/notacommand", ctx)
        assert "Unknown" in result.output


class TestShareCommand:
    @pytest.mark.asyncio
    async def test_share_add_session_grant(
        self, dispatcher: CommandDispatcher, ctx: CommandContext, tmp_path
    ) -> None:
        shared_file = tmp_path / "notes.txt"
        shared_file.write_text("hello", encoding="utf-8")

        result = await dispatcher.dispatch(
            f"/share add {shared_file} --mode ro --session --as notes",
            ctx,
        )

        assert "Shared" in result.output
        state = ctx.runner.load_thread_state(ctx.thread_id)
        assert len(state["session_shared_paths"]) == 1
        assert state["session_shared_paths"][0]["target"] == "/shared/notes"

    @pytest.mark.asyncio
    async def test_share_list_includes_default(
        self, dispatcher: CommandDispatcher, ctx: CommandContext
    ) -> None:
        result = await dispatcher.dispatch("/share list", ctx)
        assert "Shared paths" in result.output
        assert "/shared/default" in result.output


class TestBackendCommand:
    @pytest.mark.asyncio
    async def test_backend_shows_type(
        self,
        dispatcher: CommandDispatcher,
        ctx: CommandContext,
    ) -> None:
        result = await dispatcher.dispatch("/backend", ctx)
        assert ctx.config.backend.type in result.output


class TestContextCommand:
    @pytest.mark.asyncio
    async def test_context_renders_breakdown(
        self, dispatcher: CommandDispatcher, ctx: CommandContext
    ) -> None:
        result = await dispatcher.dispatch("/context", ctx)
        # Header + model + the static category labels + the estimate footer.
        assert "Context" in result.output
        assert ctx.config.model in result.output
        assert "System prompt" in result.output
        assert "MCP tools" in result.output
        assert "Estimated footprint" in result.output

    @pytest.mark.asyncio
    async def test_context_counts_session_messages(
        self, dispatcher: CommandDispatcher, ctx: CommandContext
    ) -> None:
        # A persisted conversation turn feeds the live "Messages" estimate.
        ctx.runner.append_session_entry(ctx.thread_id, {"role": "user", "content": "q" * 4000})
        ctx.runner.append_session_entry(ctx.thread_id, {"role": "assistant", "content": "a" * 4000})
        result = await dispatcher.dispatch("/context", ctx)
        assert "Messages" in result.output
        # ~2000 estimated message tokens → rendered as a "2.0k"-scale figure.
        assert "2.0k" in result.output or "1.9k" in result.output


class TestRegistryHandlerAlignment:
    """The platform menus derive from gateway_command_registry(); guard that
    every advertised command maps to a real dispatcher handler so a menu can't
    drift to advertise commands that just return 'Unknown command'."""

    def test_every_registry_command_has_a_handler(self, dispatcher: CommandDispatcher) -> None:
        from olano.cli.commands import gateway_command_registry

        missing = [
            entry.name
            for entry in gateway_command_registry()
            # Custom commands are the owner's own name → prompt shortcuts; the
            # inbound layer runs them as an ordinary turn, so by design they
            # have no dispatcher handler.
            if not entry.custom and dispatcher._resolve_handler(f"/{entry.name}") is None
        ]
        assert missing == [], f"registry commands without a handler: {missing}"

    def test_legacy_phantom_commands_absent(self) -> None:
        from olano.cli.commands import gateway_command_registry

        names = {entry.name for entry in gateway_command_registry()}
        legacy = {
            "personality",
            "retry",
            "undo",
            "resume",
            "provider",
            "insights",
            "voice",
            "sethome",
            "compress",
            "title",
            "reload-mcp",
        }
        assert not (names & legacy), f"legacy phantom commands leaked back in: {names & legacy}"


class TestTelegramMenuCommands:
    """``telegram_menu_commands`` must emit only Telegram-valid command names.

    Telegram's ``setMyCommands`` rejects the ENTIRE batch with
    ``BadRequest: Bot_command_invalid`` if any name violates the rule
    (1-32 chars, lowercase letters / digits / underscores). Hyphenated
    commands like ``reload-agent`` / ``self-improve`` therefore must be
    dropped from the menu rather than poisoning the whole registration.
    """

    import re as _re

    _VALID = _re.compile(r"^[a-z0-9_]{1,32}$")

    def test_no_invalid_names_emitted(self) -> None:
        from olano.cli.commands import telegram_menu_commands

        menu, _hidden = telegram_menu_commands()
        names = [name for name, _desc in menu]
        invalid = [n for n in names if not self._VALID.match(n)]
        assert invalid == [], f"Telegram-invalid command names emitted: {invalid}"

    def test_hyphenated_commands_dropped(self) -> None:
        from olano.cli.commands import telegram_menu_commands

        names = {name for name, _ in telegram_menu_commands()[0]}
        assert "reload-agent" not in names
        assert "reload-vault" not in names
        assert "self-improve" not in names

    def test_valid_commands_survive(self) -> None:
        from olano.cli.commands import telegram_menu_commands

        names = {name for name, _ in telegram_menu_commands()[0]}
        # Plain commands are unaffected.
        assert {"help", "status", "model", "new"} <= names
        # /reset was renamed to /new and removed entirely.
        assert "reset" not in names


class TestSkillsCommand:
    """/skills must be chat-safe: names + descriptions, never file paths.

    Chat adapters auto-attach absolute paths in a reply that resolve to real
    files (``extract_local_files``; ``.md`` is in ``DOCUMENT_EXTS``), so the
    old per-skill ``location`` line turned /skills on Telegram into one
    SKILL.md attachment per installed skill.
    """

    @pytest.fixture
    def skill_md(self, tmp_workspace):
        skill_dir = tmp_workspace / "skills" / "demo-skill"
        skill_dir.mkdir(parents=True)
        path = skill_dir / "SKILL.md"
        path.write_text(
            "---\nname: demo-skill\ndescription: Does a demo thing for testing.\n---\n\nBody.\n",
            encoding="utf-8",
        )
        return path

    @pytest.mark.asyncio
    async def test_lists_name_and_description(
        self, dispatcher: CommandDispatcher, ctx: CommandContext, skill_md
    ) -> None:
        result = await dispatcher.dispatch("/skills", ctx)
        assert "Skills — 1 loaded" in result.output
        assert "demo-skill" in result.output
        assert "Does a demo thing for testing" in result.output

    @pytest.mark.asyncio
    async def test_output_contains_no_file_paths(
        self, dispatcher: CommandDispatcher, ctx: CommandContext, skill_md, tmp_workspace
    ) -> None:
        result = await dispatcher.dispatch("/skills", ctx)
        assert str(skill_md) not in result.output
        assert str(tmp_workspace) not in result.output
        assert "SKILL.md" not in result.output
        # Nothing in the output may look like an attachable absolute path.
        import re

        assert not re.search(r"(?:~?/)(?:[\w.\-]+/)+[\w.\-]+\.\w+", result.output)

    @pytest.mark.asyncio
    async def test_subagent_tier_is_marked(
        self, dispatcher: CommandDispatcher, ctx: CommandContext, tmp_workspace
    ) -> None:
        skill_dir = tmp_workspace / "skills" / "worker"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: worker\ndescription: Delegated job.\ntier: subagent\n---\n\nBody.\n",
            encoding="utf-8",
        )
        result = await dispatcher.dispatch("/skills", ctx)
        assert "worker (subagent)" in result.output

    @pytest.mark.asyncio
    async def test_long_description_is_clipped(
        self, dispatcher: CommandDispatcher, ctx: CommandContext, tmp_workspace
    ) -> None:
        skill_dir = tmp_workspace / "skills" / "chatty"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: chatty\ndescription: " + "word " * 60 + "\n---\n\nBody.\n",
            encoding="utf-8",
        )
        result = await dispatcher.dispatch("/skills", ctx)
        line = next(ln for ln in result.output.splitlines() if "chatty" in ln)
        assert len(line) < 130
        assert line.endswith("…")


class TestToolsCommand:
    @pytest.mark.asyncio
    async def test_compact_groups_by_toolkit(
        self, dispatcher: CommandDispatcher, ctx: CommandContext
    ) -> None:
        result = await dispatcher.dispatch("/tools", ctx)
        assert result.output.startswith("🔧 Tools —")
        import re

        # One line per toolkit: "• group (n): name1, name2, …"
        assert re.search(r"^• [\w.\-]+ \(\d+\): \w+", result.output, re.M)
        assert "Built-in (always available):" in result.output
        assert "/tools verbose" in result.output

    @pytest.mark.asyncio
    async def test_verbose_lists_descriptions(
        self, dispatcher: CommandDispatcher, ctx: CommandContext
    ) -> None:
        result = await dispatcher.dispatch("/tools verbose", ctx)
        assert result.output.startswith("🔧 Tools —")
        import re

        # Per-tool bullets with a one-line description.
        assert re.search(r"^• \w+ — \S", result.output, re.M)
