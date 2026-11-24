"""Tests for chat-platform slash-command dispatch and verbosity rendering.

These cover the shared inbound layer (:mod:`olano.platforms.inbound`) and the
pure renderer (:mod:`olano.platforms.verbosity`) that every platform adapter
and classic channel funnels through, so ``/help``, ``/model``, ``/verbosity`` &
friends behave identically on Telegram, WhatsApp, Discord, Slack and the Chinese
platforms.
"""

from __future__ import annotations

import pytest

from olano.config.agent_config import CommandsConfig, CustomCommand
from olano.platforms.inbound import (
    ACTIVE_THREAD_KEY,
    START_GREETING_PROMPT,
    THINK_KEY,
    VERBOSITY_KEY,
    SenderInfo,
    agent_default_verbosity,
    command_name,
    get_session_settings,
    maybe_handle_command,
)
from olano.platforms.verbosity import (
    DEFAULT_VERBOSITY,
    VERBOSITY_MODES,
    normalize_verbosity,
    render_events,
)

# ───────────────────────────── verbosity rendering ──────────────────────────


class TestNormalizeVerbosity:
    def test_canonical(self) -> None:
        for mode in VERBOSITY_MODES:
            assert normalize_verbosity(mode) == mode

    def test_aliases(self) -> None:
        assert normalize_verbosity("v") == "verbose"
        assert normalize_verbosity("FULL") == "verbose"
        assert normalize_verbosity("q") == "quiet"
        assert normalize_verbosity("silent") == "quiet"
        assert normalize_verbosity("default") == "normal"

    def test_unknown_and_none(self) -> None:
        assert normalize_verbosity("bananas") is None
        assert normalize_verbosity(None) is None
        assert normalize_verbosity("") is None


SAMPLE_EVENTS = [
    ("tool_call", {"id": "1", "name": "web_search", "args": '{"q": "weather"}'}),
    ("tool_result", {"id": "1", "name": "web_search", "content": "Sunny, 24C"}),
    ("text", "It is sunny, "),
    ("text", "24C today."),
]


class TestRenderEvents:
    def test_quiet_only_answer(self) -> None:
        out = render_events(SAMPLE_EVENTS, "quiet")
        assert out == "It is sunny, 24C today."
        assert "web_search" not in out

    def test_normal_only_answer(self) -> None:
        out = render_events(SAMPLE_EVENTS, "normal")
        assert out == "It is sunny, 24C today."
        assert "🔧" not in out

    def test_verbose_includes_tools(self) -> None:
        out = render_events(SAMPLE_EVENTS, "verbose")
        assert "web_search" in out
        assert "Sunny, 24C" in out
        assert "It is sunny, 24C today." in out
        # tool call should appear before the final answer
        assert out.index("web_search") < out.index("It is sunny")

    def test_normal_tool_only_turn_gets_note(self) -> None:
        events = [
            ("tool_call", {"id": "1", "name": "write_file", "args": "x"}),
            ("tool_result", {"id": "1", "name": "write_file", "content": "ok"}),
        ]
        out = render_events(events, "normal")
        assert "write_file" in out
        assert "tool call" in out

    def test_quiet_tool_only_turn_is_silent(self) -> None:
        events = [("tool_call", {"id": "1", "name": "write_file", "args": "x"})]
        assert render_events(events, "quiet") == ""

    def test_unknown_mode_falls_back_to_normal(self) -> None:
        assert render_events(SAMPLE_EVENTS, "bananas") == render_events(
            SAMPLE_EVENTS, DEFAULT_VERBOSITY
        )

    def test_long_result_is_truncated(self) -> None:
        big = "x" * 5000
        events = [("tool_result", {"id": "1", "name": "read", "content": big})]
        out = render_events(events, "verbose")
        assert len(out) < 2000
        assert "chars)" in out


# Marcus asks two peers at once + delegates to a subagent — the "many agents at
# the same time" scenario. Includes leaked interleaved subagent_text chunks (as
# rerouted by the runner) that must NOT be spliced into the reply verbatim.
MULTI_AGENT_EVENTS = [
    ("text", "Let me check with the team. "),
    (
        "tool_call",
        {
            "id": "p1",
            "name": "talk_to_agent",
            "args": '{"agent_name": "kevin", "message": "price of QCOM?"}',
        },
    ),
    (
        "tool_call",
        {
            "id": "p2",
            "name": "talk_to_agent",
            "args": '{"agent_name": "emily", "message": "draft the agenda"}',
        },
    ),
    (
        "tool_call",
        {
            "id": "t1",
            "name": "task",
            "args": '{"subagent_type": "research-agent", "description": "Scan the news"}',
        },
    ),
    ("subagent_text", {"text": "QCOM is trading", "namespace": ("peer",), "agents": ["kevin"]}),
    ("subagent_text", {"text": "Here is the agenda", "namespace": ("peer",), "agents": ["emily"]}),
    ("subagent_text", {"text": " at $228.90.", "namespace": ("peer",), "agents": ["kevin"]}),
    ("tool_result", {"id": "p1", "name": "talk_to_agent", "content": "QCOM is at $228.90."}),
    ("tool_result", {"id": "p2", "name": "talk_to_agent", "content": "Agenda drafted."}),
    ("tool_result", {"id": "t1", "name": "task", "content": "3 relevant articles found."}),
    ("text", "Kevin quotes $228.90 and Emily drafted the agenda."),
]


class TestRenderEventsMultiAgent:
    def test_peer_tool_names_in_sync_with_runner(self) -> None:
        from olano.core.runner import _PEER_TOOL_NAMES
        from olano.platforms.verbosity import PEER_TOOL_NAMES

        assert set(PEER_TOOL_NAMES) == set(_PEER_TOOL_NAMES)

    def test_quiet_and_normal_exclude_other_agents_text(self) -> None:
        for mode in ("quiet", "normal"):
            out = render_events(MULTI_AGENT_EVENTS, mode)
            assert (
                out
                == "Let me check with the team. Kevin quotes $228.90 and Emily drafted the agenda."
            )
            assert "trading" not in out  # leaked kevin stream never splices in

    def test_verbose_renders_attributed_conversation_cards(self) -> None:
        # default dialect = standard markdown (what most adapters expect and
        # convert to their native format themselves)
        out = render_events(MULTI_AGENT_EVENTS, "verbose")
        # peer calls render as conversation cards, not raw JSON
        assert "💬 → **kevin**: price of QCOM?" in out
        assert "💬 → **emily**: draft the agenda" in out
        assert '"agent_name"' not in out
        # replies attributed to the right agent via the call id, block-quoted
        assert "💬 **kevin** replied:\n> QCOM is at $228.90." in out
        assert "💬 **emily** replied:\n> Agenda drafted." in out
        # subagent delegation card
        assert "🤖 → subagent **research-agent**: Scan the news" in out
        assert "3 relevant articles found." in out

    def test_verbose_collapses_interleaved_agent_text_to_indicators(self) -> None:
        out = render_events(MULTI_AGENT_EVENTS, "verbose")
        # the raw interleaved chunks are never spliced into the reply…
        assert "QCOM is trading" not in out
        assert "Here is the agenda" not in out
        # …each agent gets exactly ONE "talking with" indicator (names are not
        # nested in extra entities — Telegram legacy rejects nested markup)
        assert out.count("_💬 talking with kevin…_") == 1
        assert out.count("_💬 talking with emily…_") == 1

    def test_verbose_generic_subagent_indicator_once_per_namespace(self) -> None:
        events = [
            ("subagent_text", {"text": "chunk a", "namespace": ("task:1",)}),
            ("subagent_text", {"text": "chunk b", "namespace": ("task:1",)}),
            ("text", "Done."),
        ]
        out = render_events(events, "verbose")
        assert out.count("talking with subagent") == 1
        assert "chunk a" not in out

    def test_verbose_tool_names_use_code_spans_not_emphasis(self) -> None:
        """Underscored tool names (web_search…) wrapped in _/**_ entities break
        Telegram's legacy Markdown parser — the whole verbose reply then falls
        back to unformatted plain text. Code spans are safe there (and on
        Slack/Discord), so tool names must render inside backticks."""
        out = render_events(SAMPLE_EVENTS, "verbose")
        assert "`web_search`" in out
        assert "_web_search_" not in out
        assert "**web_search**" not in out

    def test_normal_note_uses_friendly_peer_labels(self) -> None:
        events = [
            (
                "tool_call",
                {
                    "id": "p1",
                    "name": "talk_to_agent",
                    "args": '{"agent_name": "kevin", "message": "hi"}',
                },
            ),
            ("tool_result", {"id": "p1", "name": "talk_to_agent", "content": "hello"}),
        ]
        out = render_events(events, "normal")
        assert "talked with kevin" in out
        assert "talk_to_agent" not in out


# ─────────────────────────── per-platform dialects ──────────────────────────


class TestMarkupDialects:
    def test_discord_uses_native_markdown_and_subtext(self) -> None:
        out = render_events(MULTI_AGENT_EVENTS, "verbose", dialect="discord")
        assert "💬 → **kevin**: price of QCOM?" in out
        # Discord-only `-#` small-print for the liveness indicators
        assert "-# 💬 talking with kevin…" in out
        assert "-# 💬 talking with emily…" in out
        # block-quoted peer replies
        assert "💬 **kevin** replied:\n> QCOM is at $228.90." in out

    def test_telegram_legacy_single_char_entities_no_nesting(self) -> None:
        out = render_events(MULTI_AGENT_EVENTS, "verbose", dialect="telegram-legacy")
        assert "💬 → *kevin*: price of QCOM?" in out
        assert "**" not in out  # legacy Markdown has no double-asterisk bold
        # no block quotes (unsupported) and no nested entities in indicators
        assert "> QCOM" not in out
        assert "_💬 talking with kevin…_" in out
        assert "_💬 talking with *kevin*…_" not in out

    def test_slack_mrkdwn_dialect(self) -> None:
        out = render_events(MULTI_AGENT_EVENTS, "verbose", dialect="mrkdwn")
        assert "💬 → *kevin*: price of QCOM?" in out
        assert "**" not in out
        assert "💬 *kevin* replied:\n> QCOM is at $228.90." in out

    def test_plain_dialect_has_no_markup_at_all(self) -> None:
        out = render_events(MULTI_AGENT_EVENTS, "verbose", dialect="plain")
        assert "💬 → kevin: price of QCOM?" in out
        assert "💬 kevin replied:\nQCOM is at $228.90." in out
        assert "💬 talking with kevin…" in out
        assert "🤖 → subagent research-agent: Scan the news" in out
        for marker in ("*", "`", "```", "_(", ")_", "> "):
            assert marker not in out, f"plain output leaked markup {marker!r}: {out}"

    def test_plain_normal_note_has_no_markup(self) -> None:
        events = [("tool_call", {"id": "1", "name": "write_file", "args": "x"})]
        out = render_events(events, "normal", dialect="plain")
        assert out == "(done — 1 tool call(s): write_file)"

    def test_unknown_dialect_falls_back_to_markdown(self) -> None:
        assert render_events(MULTI_AGENT_EVENTS, "verbose", dialect="bananas") == render_events(
            MULTI_AGENT_EVENTS, "verbose", dialect="markdown"
        )

    def test_dialect_answer_identical_in_quiet_and_normal(self) -> None:
        # dialects only decorate the play-by-play; the answer itself is verbatim
        for dialect in ("markdown", "discord", "mrkdwn", "telegram-legacy", "plain"):
            assert render_events(MULTI_AGENT_EVENTS, "quiet", dialect=dialect) == (
                "Let me check with the team. Kevin quotes $228.90 and Emily drafted the agenda."
            )

    def test_dialect_for_platform_mapping(self) -> None:
        from olano.platforms.verbosity import dialect_for_platform

        # adapters that convert standard markdown themselves get "markdown"
        for name in ("telegram", "whatsapp", "slack", "mattermost", "matrix", "dingtalk", "wecom"):
            assert dialect_for_platform(name).key == "markdown", name
        assert dialect_for_platform("discord").key == "discord"
        # no-markup surfaces get plain
        for name in ("signal", "sms", "bluebubbles", "email", "homeassistant", "qqbot"):
            assert dialect_for_platform(name).key == "plain", name
        assert dialect_for_platform(None).key == "markdown"

    def test_dialect_for_channel_mapping(self) -> None:
        from olano.platforms.verbosity import dialect_for_channel

        # classic channels send text verbatim → raw platform syntax
        assert dialect_for_channel("telegram").key == "telegram-legacy"
        assert dialect_for_channel("slack").key == "mrkdwn"
        assert dialect_for_channel("discord").key == "discord"
        assert dialect_for_channel("email").key == "plain"
        assert dialect_for_channel("something-new").key == "markdown"


# ──────────────────────────── command detection ─────────────────────────────


class TestCommandName:
    def test_basic(self) -> None:
        assert command_name("/help") == "help"
        assert command_name("/Model anthropic:x") == "model"
        assert command_name("  /new ") == "new"

    def test_strips_bot_suffix(self) -> None:
        assert command_name("/help@mybot") == "help"

    def test_rejects_paths_and_plain_text(self) -> None:
        assert command_name("/etc/hosts") is None
        assert command_name("/usr/bin/env") is None
        assert command_name("hello there") is None
        assert command_name("") is None
        assert command_name("https://example.com") is None


# ─────────────────────────── maybe_handle_command ───────────────────────────


@pytest.fixture
def runner(base_config, mock_agent):
    from olano.core.runner import AgentRunner

    return AgentRunner(mock_agent, base_config)


class TestMaybeHandleCommand:
    async def test_non_command_passes_through(self, runner, base_config) -> None:
        outcome = await maybe_handle_command(
            "what's the weather?",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.handled is False
        assert outcome.reply == ""
        assert outcome.run_prompt is None

    async def test_help_is_dispatched(self, runner, base_config) -> None:
        outcome = await maybe_handle_command(
            "/help",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.handled is True
        assert "/help" in outcome.reply

    async def test_verbosity_persists_on_base_thread(self, runner, base_config) -> None:
        outcome = await maybe_handle_command(
            "/verbosity verbose",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.handled is True
        assert "verbose" in outcome.reply
        # persisted under the base id so the next ordinary turn picks it up
        state = runner.load_thread_state("chat-1")
        assert state[VERBOSITY_KEY] == "verbose"
        assert get_session_settings(runner, "chat-1")["verbosity"] == "verbose"

    async def test_think_persists(self, runner, base_config) -> None:
        await maybe_handle_command(
            "/think high",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert runner.load_thread_state("chat-1")[THINK_KEY] == "high"
        assert get_session_settings(runner, "chat-1")["think_level"] == "high"

    async def test_new_swaps_active_thread(self, runner, base_config) -> None:
        outcome = await maybe_handle_command(
            "/new",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.handled is True
        state = runner.load_thread_state("chat-1")
        new_thread = state[ACTIVE_THREAD_KEY]
        assert new_thread and new_thread != "chat-1"
        # subsequent ordinary turns resolve to the new active thread, while
        # verbosity (stored on the base id) is unaffected.
        assert get_session_settings(runner, "chat-1")["active_thread"] == new_thread

    async def test_sensitive_command_disabled_by_default(self, runner, base_config) -> None:
        # /trust is sensitive → off on a fresh agent, so it is refused.
        base_config.security.trust_level = 1
        outcome = await maybe_handle_command(
            "/trust 4",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.handled is True
        assert "disabled" in outcome.reply
        assert base_config.security.trust_level == 1  # never reached the dispatcher

    async def test_sensitive_command_runs_when_enabled(self, runner, base_config) -> None:
        base_config.commands = CommandsConfig(enabled={"trust": True})
        outcome = await maybe_handle_command(
            "/trust 3",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.handled is True
        assert "disabled" not in outcome.reply
        assert base_config.security.trust_level == 3

    async def test_safe_command_can_be_disabled(self, runner, base_config) -> None:
        # The owner can also hide a normally-exposed command.
        base_config.commands = CommandsConfig(enabled={"memory": False})
        outcome = await maybe_handle_command(
            "/memory",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.handled is True
        assert "disabled" in outcome.reply

    async def test_share_disabled_by_default(self, runner, base_config) -> None:
        outcome = await maybe_handle_command(
            "/share list",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.handled is True
        assert "disabled" in outcome.reply

    async def test_removed_bash_is_unknown_even_when_a_stale_config_enables_it(
        self, runner, base_config
    ) -> None:
        # /bash belonged to the removed terminal chat; an older agent.json5 may
        # still carry ``enabled: {bash: true}``, which must not bring it back.
        base_config.commands = CommandsConfig(enabled={"bash": True})
        outcome = await maybe_handle_command(
            "/bash ls",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.handled is True
        assert "Unknown command" in outcome.reply

    async def test_custom_command_runs_prompt(self, runner, base_config) -> None:
        base_config.commands = CommandsConfig(
            custom=[CustomCommand(name="standup", prompt="Summarize today from MEMORY.md.")]
        )
        outcome = await maybe_handle_command(
            "/standup",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.handled is True
        assert outcome.run_prompt == "Summarize today from MEMORY.md."

    async def test_custom_command_substitutes_args(self, runner, base_config) -> None:
        base_config.commands = CommandsConfig(
            custom=[CustomCommand(name="echo", prompt="Say: {args}")]
        )
        outcome = await maybe_handle_command(
            "/echo hello world",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.run_prompt == "Say: hello world"

    async def test_custom_command_appends_args_without_placeholder(
        self, runner, base_config
    ) -> None:
        base_config.commands = CommandsConfig(
            custom=[CustomCommand(name="brief", prompt="Give me a brief.")]
        )
        outcome = await maybe_handle_command(
            "/brief on the news",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.run_prompt == "Give me a brief.\n\non the news"

    async def test_unknown_command_is_handled_not_forwarded(self, runner, base_config) -> None:
        # An unknown but command-shaped message is still handled (so it is not
        # leaked to the LLM); the dispatcher reports it.
        outcome = await maybe_handle_command(
            "/definitelynotacommand",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.handled is True
        assert "Unknown command" in outcome.reply


class TestStartCommand:
    """``/start`` — the first message every Telegram user sends (the Start
    button), also synthesized by WeCom on subscribe. It must greet through the
    agent, never reach the dispatcher's "Unknown command" fallback."""

    async def test_start_greets_through_agent(self, runner, base_config) -> None:
        outcome = await maybe_handle_command(
            "/start",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.handled is True
        assert outcome.run_prompt == START_GREETING_PROMPT
        assert outcome.reply == ""

    async def test_start_with_bot_suffix(self, runner, base_config) -> None:
        # Telegram appends @botname in groups: "/start@RepuestosComodinBot".
        outcome = await maybe_handle_command(
            "/start@RepuestosComodinBot",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.handled is True
        assert outcome.run_prompt is not None
        assert outcome.run_prompt.startswith(START_GREETING_PROMPT)

    async def test_start_includes_deep_link_payload(self, runner, base_config) -> None:
        # t.me/bot?start=ref123 arrives as "/start ref123".
        outcome = await maybe_handle_command(
            "/start ref123",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.run_prompt is not None
        assert outcome.run_prompt.startswith(START_GREETING_PROMPT)
        assert 'start parameter: "ref123"' in outcome.run_prompt

    async def test_start_includes_sender_name(self, runner, base_config) -> None:
        outcome = await maybe_handle_command(
            "/start",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
            sender=SenderInfo(user_id="42", display_name="Jamie", channel="telegram"),
        )
        assert outcome.run_prompt is not None
        assert '"Jamie"' in outcome.run_prompt

    async def test_custom_start_overrides_default_greeting(self, runner, base_config) -> None:
        base_config.commands = CommandsConfig(
            custom=[CustomCommand(name="start", prompt="Say exactly: WELCOME_V2")]
        )
        outcome = await maybe_handle_command(
            "/start",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.handled is True
        assert outcome.run_prompt == "Say exactly: WELCOME_V2"


class TestSessionSettingsDefaults:
    def test_defaults_when_no_state(self, runner) -> None:
        settings = get_session_settings(runner, "fresh-chat")
        assert settings == {
            "verbosity": DEFAULT_VERBOSITY,
            "think_level": "off",
            "active_thread": "fresh-chat",
        }


# ───────────────────── per-agent default verbosity ──────────────────────────


class TestAgentDefaultVerbosity:
    """``AgentConfig.default_verbosity`` seeds chats that never chose."""

    def test_config_normalizes_aliases_and_junk(self) -> None:
        from olano.config.agent_config import AgentConfig

        assert AgentConfig(name="a").default_verbosity == "normal"
        assert AgentConfig(name="a", default_verbosity="quiet").default_verbosity == "quiet"
        # aliases accepted, same set /verbosity takes
        assert AgentConfig(name="a", default_verbosity="full").default_verbosity == "verbose"
        assert AgentConfig(name="a", default_verbosity="V").default_verbosity == "verbose"
        # a typo must not take the agent offline — it degrades to the default
        assert AgentConfig(name="a", default_verbosity="bananas").default_verbosity == "normal"
        assert AgentConfig(name="a", default_verbosity=None).default_verbosity == "normal"

    def test_helper_tolerates_missing_config(self) -> None:
        assert agent_default_verbosity(None) == DEFAULT_VERBOSITY
        assert agent_default_verbosity(object()) == DEFAULT_VERBOSITY

    def test_fresh_chat_follows_agent_default(self, runner, base_config) -> None:
        base_config.default_verbosity = "quiet"
        assert get_session_settings(runner, "fresh-chat")["verbosity"] == "quiet"

    def test_chat_choice_beats_agent_default(self, runner, base_config) -> None:
        base_config.default_verbosity = "quiet"
        runner.update_thread_state("chat-1", **{VERBOSITY_KEY: "verbose"})
        assert get_session_settings(runner, "chat-1")["verbosity"] == "verbose"

    async def test_command_pins_choice_matching_agent_default(self, runner, base_config) -> None:
        """A user who asks for the mode they are already on must still be pinned.

        Otherwise nothing is written, the chat keeps following the agent
        default, and changing that default later silently overrides them.
        """
        base_config.default_verbosity = "quiet"
        outcome = await maybe_handle_command(
            "/quiet",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert outcome.handled is True
        assert runner.load_thread_state("chat-1")[VERBOSITY_KEY] == "quiet"

        # the owner now raises the agent default — the pinned chat does not move
        base_config.default_verbosity = "verbose"
        assert get_session_settings(runner, "chat-1")["verbosity"] == "quiet"

    async def test_unrelated_command_does_not_pin(self, runner, base_config) -> None:
        """``/help`` must not freeze a chat onto the current agent default."""
        base_config.default_verbosity = "quiet"
        await maybe_handle_command(
            "/help",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert VERBOSITY_KEY not in (runner.load_thread_state("chat-1") or {})

        base_config.default_verbosity = "verbose"
        assert get_session_settings(runner, "chat-1")["verbosity"] == "verbose"

    async def test_verbosity_listing_reports_agent_default(self, runner, base_config) -> None:
        base_config.default_verbosity = "quiet"
        outcome = await maybe_handle_command(
            "/verbosity",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )
        assert "starts new chats on: quiet" in outcome.reply


# ───────────────────────── /olano owner gating ───────────────────────────


def _sender(**overrides) -> SenderInfo:
    defaults = dict(
        user_id="42",
        username="boss",
        chat_id="chat-42",
        channel="telegram",
        is_trusted=True,
        allowlist_configured=True,
    )
    defaults.update(overrides)
    return SenderInfo(**defaults)


@pytest.fixture(autouse=True)
def _clear_platform_pending():
    from olano.platform_control import commands as pc

    pc._PENDING.clear()
    yield
    pc._PENDING.clear()


@pytest.fixture
def platform_enabled(base_config, monkeypatch):
    """Expose /olano on the agent, bind a fake runtime (so lifecycle actions
    are permitted), and keep the status probe offline-safe."""
    import types

    from olano.platform_control import controller as ctrl
    from olano.platform_control.controller import PlatformController

    base_config.commands = CommandsConfig(enabled={"olano": True})

    async def _no_dashboard(self) -> bool:
        return False

    monkeypatch.setattr(PlatformController, "dashboard_reachable", _no_dashboard)
    monkeypatch.setattr(ctrl, "_RUNTIME", types.SimpleNamespace(_runners={}))
    return base_config


class TestPlatformCommandGating:
    async def test_disabled_by_default(self, runner, base_config) -> None:
        outcome = await maybe_handle_command(
            "/olano status",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
            sender=_sender(),
        )
        assert outcome.handled is True
        assert "disabled" in outcome.reply

    async def test_trusted_sender_gets_status(self, runner, platform_enabled) -> None:
        outcome = await maybe_handle_command(
            "/olano status",
            runner=runner,
            config=platform_enabled,
            base_thread_id="chat-1",
            sender=_sender(),
        )
        assert outcome.handled is True
        assert "Olano platform" in outcome.reply
        assert "Supervision:" in outcome.reply

    async def test_untrusted_sender_refused(self, runner, platform_enabled) -> None:
        outcome = await maybe_handle_command(
            "/olano status",
            runner=runner,
            config=platform_enabled,
            base_thread_id="chat-1",
            sender=_sender(is_trusted=False),
        )
        assert "restricted to this deployment's owner" in outcome.reply

    async def test_open_bot_refused_with_guidance(self, runner, platform_enabled) -> None:
        outcome = await maybe_handle_command(
            "/olano shutdown",
            runner=runner,
            config=platform_enabled,
            base_thread_id="chat-1",
            sender=_sender(is_trusted=False, allowlist_configured=False),
        )
        assert "allowed_users" in outcome.reply

    async def test_local_operator_not_gated(self, runner, platform_enabled) -> None:
        # sender=None == the local TUI / operator console.
        outcome = await maybe_handle_command(
            "/olano version",
            runner=runner,
            config=platform_enabled,
            base_thread_id="chat-1",
            sender=None,
        )
        assert "Olano v" in outcome.reply

    async def test_restart_stages_then_confirm_executes(
        self, runner, platform_enabled, monkeypatch
    ) -> None:
        from olano.platform_control.controller import PlatformController

        scheduled: list[dict] = []
        monkeypatch.setattr(
            PlatformController,
            "schedule_restart",
            lambda self, notify=None: scheduled.append({"notify": notify}) or "supervised-exit",
        )

        outcome = await maybe_handle_command(
            "/olano restart",
            runner=runner,
            config=platform_enabled,
            base_thread_id="chat-1",
            sender=_sender(),
        )
        assert "confirm" in outcome.reply
        assert scheduled == []  # staged, not executed

        outcome = await maybe_handle_command(
            "/olano restart confirm",
            runner=runner,
            config=platform_enabled,
            base_thread_id="chat-1",
            sender=_sender(),
        )
        assert "Restarting" in outcome.reply
        assert len(scheduled) == 1
        assert scheduled[0]["notify"].target == "chat-42"

    async def test_confirm_without_stage_is_refused(self, runner, platform_enabled) -> None:
        outcome = await maybe_handle_command(
            "/olano shutdown confirm",
            runner=runner,
            config=platform_enabled,
            base_thread_id="chat-1",
            sender=_sender(),
        )
        assert "Nothing staged" in outcome.reply

    async def test_destructive_refused_without_runtime(
        self, runner, platform_enabled, monkeypatch
    ) -> None:
        # No RuntimeServer bound (e.g. the standalone `olano chat` TUI): the
        # lifecycle actions must refuse rather than tear down the local process.
        from olano.platform_control import controller as ctrl

        monkeypatch.setattr(ctrl, "_RUNTIME", None)
        for sub in ("restart", "shutdown", "update", "rebuild"):
            outcome = await maybe_handle_command(
                f"/olano {sub}",
                runner=runner,
                config=platform_enabled,
                base_thread_id="chat-1",
                sender=_sender(),
            )
            assert "isn't available here" in outcome.reply, sub

    async def test_status_works_without_runtime(
        self, runner, platform_enabled, monkeypatch
    ) -> None:
        from olano.platform_control import controller as ctrl

        monkeypatch.setattr(ctrl, "_RUNTIME", None)
        outcome = await maybe_handle_command(
            "/olano status",
            runner=runner,
            config=platform_enabled,
            base_thread_id="chat-1",
            sender=_sender(),
        )
        assert "Olano platform" in outcome.reply

    async def test_status_reports_dashboard_freshness(self, runner, platform_enabled) -> None:
        outcome = await maybe_handle_command(
            "/olano status",
            runner=runner,
            config=platform_enabled,
            base_thread_id="chat-1",
            sender=_sender(),
        )
        # The Dashboard line now carries a build-freshness clause.
        assert "Dashboard:" in outcome.reply
        assert "build " in outcome.reply

    async def test_usage_lists_rebuild(self, runner, platform_enabled) -> None:
        outcome = await maybe_handle_command(
            "/olano bogus",
            runner=runner,
            config=platform_enabled,
            base_thread_id="chat-1",
            sender=_sender(),
        )
        assert "Unknown /olano subcommand" in outcome.reply
        assert "rebuild" in outcome.reply

    async def test_rebuild_starts_and_replies(self, runner, platform_enabled, monkeypatch) -> None:
        from olano.platform_control.controller import PlatformController

        started: list[bool] = []
        monkeypatch.setattr(
            PlatformController,
            "begin_rebuild",
            lambda self, notify=None: started.append(True) or True,
        )
        outcome = await maybe_handle_command(
            "/olano rebuild",
            runner=runner,
            config=platform_enabled,
            base_thread_id="chat-1",
            sender=_sender(),
        )
        assert started == [True]
        assert "Rebuilding the dashboard" in outcome.reply

    async def test_rebuild_unknown_target(self, runner, platform_enabled) -> None:
        outcome = await maybe_handle_command(
            "/olano rebuild server",
            runner=runner,
            config=platform_enabled,
            base_thread_id="chat-1",
            sender=_sender(),
        )
        assert "Unknown rebuild target" in outcome.reply

    async def test_update_force_threads_through_confirm(
        self, runner, platform_enabled, monkeypatch
    ) -> None:
        from olano.platform_control.controller import PlatformController
        from olano.platform_control.detect import InstallMode
        from olano.update.strategies import UpdateCheck

        calls: list[tuple[bool, bool]] = []
        monkeypatch.setattr(
            PlatformController,
            "begin_update",
            lambda self, notify=None, force=False, restart=True: (
                calls.append((force, restart)) or True
            ),
        )
        fake = UpdateCheck(
            mode=InstallMode.SOURCE,
            current="1.0.0",
            available="abc123 fix",
            update_available=True,
            can_apply=True,
            details="1 new commit(s) upstream",
        )
        monkeypatch.setattr("olano.update.strategies.check_update", lambda home: fake)

        staged = await maybe_handle_command(
            "/olano update --force",
            runner=runner,
            config=platform_enabled,
            base_thread_id="chat-1",
            sender=_sender(),
        )
        assert "confirm" in staged.reply
        assert "dashboard" in staged.reply.lower()

        confirmed = await maybe_handle_command(
            "/olano update confirm",
            runner=runner,
            config=platform_enabled,
            base_thread_id="chat-1",
            sender=_sender(),
        )
        assert "Updating" in confirmed.reply
        assert calls == [(True, True)]  # force staged, restart default


class TestCommandRepliesArePathSafe:
    """Command replies are listings, never file deliveries.

    /memory echoes MEMORY.md verbatim; if that content names an absolute path
    to a real attachable file, the adapter's implicit attach path
    (extract_local_files) would upload it. _result_reply defangs such
    references by backtick-wrapping them (code spans are never attached).
    """

    async def test_memory_reply_defangs_real_file_paths(
        self, runner, base_config, tmp_workspace
    ) -> None:
        doc = tmp_workspace / "notes.md"
        doc.write_text("hello", encoding="utf-8")
        (tmp_workspace / "MEMORY.md").write_text(
            f"# MEMORY.md - Long-Term Memory\n\nRoadmap: {doc}\n", encoding="utf-8"
        )

        outcome = await maybe_handle_command(
            "/memory",
            runner=runner,
            config=base_config,
            base_thread_id="chat-1",
        )

        assert outcome.handled is True
        assert f"`{doc}`" in outcome.reply
        from types import SimpleNamespace

        from olano.platforms.base import BasePlatformAdapter

        files, _ = BasePlatformAdapter.extract_local_files(SimpleNamespace(), outcome.reply)
        assert files == []


class TestHelpListing:
    """``/help`` lists what this agent will actually run, on this surface.

    It used to be a hand-written string list: it advertised every command
    regardless of the agent's exposure config — so a chat user could read
    ``/model`` in the listing, run it, and be told "🔒 /model is disabled for
    this agent" — and it never mentioned custom commands at all.
    """

    async def _help_text(self, runner, config, *, sender) -> str:
        outcome = await maybe_handle_command(
            "/help",
            runner=runner,
            config=config,
            base_thread_id="chat-1",
            sender=sender,
        )
        assert outcome.handled is True
        return outcome.reply

    async def test_chat_listing_hides_sensitive_commands(self, runner, base_config) -> None:
        base_config.commands = CommandsConfig()
        text = await self._help_text(runner, base_config, sender=_sender())
        assert "/status" in text
        for hidden in ("/model", "/share", "/reload-vault", "/olano"):
            assert hidden not in text, f"{hidden} is off by default but /help advertised it"

    async def test_chat_listing_follows_the_owner_overrides(self, runner, base_config) -> None:
        base_config.commands = CommandsConfig(enabled={"model": True, "status": False})
        text = await self._help_text(runner, base_config, sender=_sender())
        assert "/model" in text
        assert "/status" not in text

    async def test_custom_commands_are_listed(self, runner, base_config) -> None:
        base_config.commands = CommandsConfig(
            custom=[
                CustomCommand(name="standup", prompt="Summarize. {args}", description="Daily recap")
            ]
        )
        text = await self._help_text(runner, base_config, sender=_sender())
        assert "Custom commands:" in text
        assert "/standup" in text
        assert "Daily recap" in text

    async def test_operator_console_keeps_the_full_listing(self, runner, base_config) -> None:
        """``sender is None`` = an owner console with no per-sender identity."""
        base_config.commands = CommandsConfig()
        text = await self._help_text(runner, base_config, sender=None)
        for shown in ("/model", "/share", "/olano"):
            assert shown in text

    async def test_operator_console_still_honours_an_explicit_disable(
        self, runner, base_config
    ) -> None:
        base_config.commands = CommandsConfig(enabled={"share": False})
        text = await self._help_text(runner, base_config, sender=None)
        assert "/model" in text
        assert "/share" not in text


class TestContextOmitsTheModel:
    """``/context`` must not name the model on a chat platform.

    ``/model`` is sensitive and off by default there precisely so a bot doesn't
    advertise which provider backs it — and ``/context``, exposed by default,
    printed it in its header.
    """

    async def _context_text(self, runner, config, *, sender) -> str:
        outcome = await maybe_handle_command(
            "/context",
            runner=runner,
            config=config,
            base_thread_id="chat-1",
            sender=sender,
        )
        assert outcome.handled is True
        return outcome.reply

    async def test_chat_reply_hides_the_model(self, runner, base_config) -> None:
        text = await self._context_text(runner, base_config, sender=_sender())
        assert "Context" in text
        assert base_config.model not in text

    async def test_operator_console_still_shows_it(self, runner, base_config) -> None:
        text = await self._context_text(runner, base_config, sender=None)
        assert base_config.model in text
