"""`agent.json5 → message_policy` — the three caps and the seams they hold.

Grouped by the failure each test exists to prevent rather than by module, since
the feature's whole risk is that one of five ingress surfaces disagrees with the
others.
"""

from __future__ import annotations

import pytest

from olano.config.agent_config import AgentConfig, MessagePolicyConfig
from olano.models.output_limit import apply_output_limit, output_limit_param
from olano.security import reply_policy as rp


def _policy(**kwargs) -> MessagePolicyConfig:
    return MessagePolicyConfig(**kwargs)


# ── Defaults: an untouched agent must behave exactly as before ───────────────


class TestOffByDefault:
    def test_a_fresh_config_resolves_to_no_policy(self) -> None:
        # `resolve_policy` returning None is what lets every ingress site skip
        # the check entirely; if this ever returns a policy, every existing
        # deployment starts paying for a feature it never enabled.
        assert rp.resolve_policy(AgentConfig(name="a")) is None

    def test_a_policy_with_only_notices_still_resolves_to_none(self) -> None:
        # Notices are decoration for rules that do not exist yet.
        cfg = AgentConfig(
            name="a",
            message_policy={"ignore_notice": "hi", "oversize_notice": "hi"},
        )
        assert rp.resolve_policy(cfg) is None

    def test_no_policy_allows_everything(self) -> None:
        verdict = rp.screen_inbound("x" * 100_000, None)
        assert verdict.action == "allow"
        assert not verdict.blocked

    def test_an_object_without_the_field_does_not_raise(self) -> None:
        # A test double or a config from before this field existed.
        assert rp.resolve_policy(object()) is None

    def test_a_mock_config_resolves_to_no_policy(self) -> None:
        # `runner.config` is a MagicMock across the runtime suite, and on a
        # mock every attribute is a truthy mock — `len(text) > limit` raised
        # TypeError and took /api/chat down for 14 tests. The isinstance gate
        # is what makes "cannot state a policy" mean "has none".
        from unittest.mock import MagicMock

        assert rp.resolve_policy(MagicMock()) is None


class TestConfigBounds:
    def test_a_zero_or_negative_input_cap_is_refused(self) -> None:
        for bad in (0, -5):
            with pytest.raises(ValueError):
                AgentConfig(name="a", message_policy={"max_input_chars": bad})

    def test_an_output_cap_below_the_floor_is_refused(self) -> None:
        # 16 is the smallest ceiling that can hold a one-line reply on any
        # tokenizer; below it the field would only ever produce empty turns.
        with pytest.raises(ValueError):
            AgentConfig(name="a", message_policy={"max_output_tokens": 8})
        assert AgentConfig(name="a", message_policy={"max_output_tokens": 16})

    def test_list_fields_accept_one_per_line_text_and_drop_blanks(self) -> None:
        # The dashboard textarea and a hand-edited config both arrive here; a
        # stray blank line must never become a pattern that matches everything.
        cfg = AgentConfig(
            name="a",
            message_policy={"ignore_patterns": "spam\n\n  \nscam\n", "ignore_rules": "  \n x \n"},
        )
        assert cfg.message_policy.ignore_patterns == ["spam", "scam"]
        assert cfg.message_policy.ignore_rules == ["x"]

    def test_an_unknown_oversize_action_is_refused(self) -> None:
        with pytest.raises(ValueError):
            AgentConfig(name="a", message_policy={"on_oversize": "shrug"})


# ── Inbound size cap ─────────────────────────────────────────────────────────


class TestMaxInputChars:
    def test_under_the_limit_is_allowed_untouched(self) -> None:
        verdict = rp.screen_inbound("hello", _policy(max_input_chars=10))
        assert (verdict.action, verdict.text) == ("allow", "hello")

    def test_exactly_at_the_limit_is_allowed(self) -> None:
        # The field reads "accepts up to N", so N must pass.
        assert rp.screen_inbound("x" * 10, _policy(max_input_chars=10)).action == "allow"

    def test_over_the_limit_is_rejected_with_both_numbers(self) -> None:
        verdict = rp.screen_inbound("x" * 50, _policy(max_input_chars=10))
        assert verdict.action == "reject"
        assert verdict.blocked
        assert "50" in verdict.notice and "10" in verdict.notice

    def test_a_custom_notice_replaces_the_built_in_one(self) -> None:
        verdict = rp.screen_inbound(
            "x" * 50, _policy(max_input_chars=10, oversize_notice="Keep it short.")
        )
        assert verdict.notice == "Keep it short."

    def test_truncate_hands_the_model_exactly_the_limit(self) -> None:
        verdict = rp.screen_inbound("x" * 50, _policy(max_input_chars=10, on_oversize="truncate"))
        assert verdict.action == "truncate"
        assert verdict.text == "x" * 10
        assert not verdict.blocked

    def test_ignore_blocks_with_no_notice_by_default(self) -> None:
        verdict = rp.screen_inbound("x" * 50, _policy(max_input_chars=10, on_oversize="ignore"))
        assert verdict.blocked and verdict.notice == ""


# ── Ignore patterns ──────────────────────────────────────────────────────────


class TestIgnorePatterns:
    def test_a_match_anywhere_ignores_the_message(self) -> None:
        verdict = rp.screen_inbound(
            "please buy followers now", _policy(ignore_patterns=["buy followers"])
        )
        assert verdict.action == "ignore"

    def test_matching_is_case_insensitive(self) -> None:
        # The help text promises an operator can write `crypto` and mean it;
        # requiring `(?i)` on every line is a trap that fails silently.
        assert rp.screen_inbound(
            "BUY FOLLOWERS", _policy(ignore_patterns=["buy followers"])
        ).blocked

    def test_anchors_still_restrict_to_the_whole_message(self) -> None:
        policy = _policy(ignore_patterns=[r"^\W*$"])
        assert rp.screen_inbound("???", policy).blocked
        assert not rp.screen_inbound("??? and a real question", policy).blocked

    def test_the_reason_names_the_pattern_but_the_notice_does_not(self) -> None:
        # Telling a spammer which rule caught them is a tuning guide.
        verdict = rp.screen_inbound("spam", _policy(ignore_patterns=["spam"]))
        assert "spam" in verdict.reason
        assert verdict.notice == ""

    def test_patterns_are_checked_before_the_size_cap(self) -> None:
        # A spam blast that is also over-long must be dropped silently, not
        # answered with a "shorten your message" line that confirms a live bot.
        verdict = rp.screen_inbound(
            "buy followers " + "x" * 500,
            _policy(max_input_chars=10, ignore_patterns=["buy followers"]),
        )
        assert verdict.action == "ignore"

    def test_an_uncompilable_pattern_is_skipped_not_raised(self) -> None:
        # Config-time validation catches these; a hand-edited file must still
        # deliver messages rather than failing every one of them.
        policy = MessagePolicyConfig.model_construct(
            max_input_chars=None,
            on_oversize="reject",
            oversize_notice="",
            ignore_patterns=["(unclosed", "spam"],
            ignore_rules=[],
            ignore_notice="",
            max_output_tokens=None,
        )
        assert rp.screen_inbound("hello", policy).action == "allow"
        assert rp.screen_inbound("spam", policy).blocked

    def test_the_config_refuses_a_bad_pattern_at_save_time(self) -> None:
        with pytest.raises(ValueError, match="not a valid regular expression"):
            AgentConfig(name="a", message_policy={"ignore_patterns": ["(unclosed"]})


class TestPatternScanIsBounded:
    async def test_patterns_run_off_the_event_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `re` has no timeout; a backtracking pattern on a 29-char message took
        # 10 s on the loop — every agent, channel and the dashboard frozen with
        # it. The async form hands the scan to a worker thread whenever there
        # are patterns to run, so the cost lands on one thread, not the box.
        import asyncio
        import threading

        seen: dict[str, object] = {}
        real = rp.screen_inbound

        def spy(text, policy):
            seen["thread"] = threading.current_thread()
            return real(text, policy)

        monkeypatch.setattr(rp, "screen_inbound", spy)
        loop_thread = threading.current_thread()
        verdict = await rp.ascreen_inbound("spam here", _policy(ignore_patterns=["spam"]))
        assert verdict.blocked
        assert seen["thread"] is not loop_thread
        # …and with no patterns the size cap is a `len()` and stays inline.
        seen.clear()
        await rp.ascreen_inbound("x" * 50, _policy(max_input_chars=10))
        assert seen["thread"] is loop_thread
        assert asyncio.get_running_loop() is not None

    async def test_a_command_never_takes_the_thread_hop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Commands are never screened, so there is nothing to run off-loop;
        # spending a thread on `/help` would be pure overhead.
        import threading

        seen: dict[str, object] = {}
        real = rp.screen_inbound

        def spy(text, policy):
            seen["thread"] = threading.current_thread()
            return real(text, policy)

        monkeypatch.setattr(rp, "screen_inbound", spy)
        await rp.ascreen_inbound("/help", _policy(ignore_patterns=[".*"]))
        assert seen["thread"] is threading.current_thread()

    def test_patterns_see_a_bounded_window_but_the_cap_sees_it_all(self) -> None:
        # A signature buried past the window is missed by design — the size
        # cap still rejects the message, so it never reaches the model.
        policy = _policy(max_input_chars=100, ignore_patterns=["needle"])
        text = "x" * (rp.PATTERN_SCAN_CHARS + 10) + "needle"
        verdict = rp.screen_inbound(text, policy)
        assert verdict.action == "reject"
        inside = "x" * (rp.PATTERN_SCAN_CHARS - 10) + "needle"
        assert rp.screen_inbound(inside, policy).action == "ignore"


class TestCommandsAreNeverScreened:
    @pytest.mark.parametrize("text", ["/new", "/olano " + "x" * 900])
    def test_commands_bypass_every_rule(self, text: str) -> None:
        # A broad pattern or a small cap must not lock an operator out of their
        # own bot's control surface.
        policy = _policy(max_input_chars=5, ignore_patterns=[".*"])
        assert rp.screen_inbound(text, policy).action == "allow"

    def test_a_bang_prefix_is_ordinary_text(self) -> None:
        # "!" was the removed terminal chat's shell prefix. On a messenger it
        # is not a command, so it must not be a way around the policy.
        policy = _policy(ignore_patterns=["^!"])
        assert rp.screen_inbound("!ls -la", policy).action != "allow"


# ── The silence marker ───────────────────────────────────────────────────────


class TestSilenceMarker:
    def test_a_marker_opening_is_silence(self) -> None:
        assert rp.is_silenced(rp.SILENCE_MARKER)
        assert rp.is_silenced(f"  \n{rp.SILENCE_MARKER}")

    def test_a_marker_mid_sentence_is_not(self) -> None:
        # An agent explaining its own policy, or quoting a sender who pasted
        # the marker, must not silence a turn that was answering.
        assert not rp.is_silenced(f"I would answer with {rp.SILENCE_MARKER} if asked.")

    def test_strip_returns_any_trailing_courtesy_text(self) -> None:
        assert rp.strip_marker(f"{rp.SILENCE_MARKER} (not in English)") == "(not in English)"


class TestMarkerAfterAPreamble:
    """The leak both Hermes and OpenClaw shipped: a model that says something
    before deciding, then writes the marker on its own line."""

    def test_is_silenced_matches_a_marker_on_its_own_line(self) -> None:
        assert rp.is_silenced(f"Let me check that.\n\n{rp.SILENCE_MARKER}")
        assert rp.is_silenced(f"ok\n   {rp.SILENCE_MARKER}")  # indented still counts
        assert rp.is_silenced(f"Sure.\r\n\r\n{rp.SILENCE_MARKER}")  # CRLF providers

    def test_is_silenced_ignores_a_marker_inside_a_sentence(self) -> None:
        assert not rp.is_silenced(f"The marker is `{rp.SILENCE_MARKER}` and that is all.")
        assert not rp.is_silenced(f"- {rp.SILENCE_MARKER} is what I would write")

    def test_strip_marker_removes_every_marker_line(self) -> None:
        assert rp.strip_marker(f"Let me check.\n\n{rp.SILENCE_MARKER}\n") == "Let me check."
        assert rp.strip_marker(rp.SILENCE_MARKER) == ""

    def test_the_stream_filter_catches_the_marker_after_a_preamble(self) -> None:
        # The preamble has already streamed (a dashboard shows it, then the
        # note); what matters is that the marker itself never becomes text and
        # the turn is reported as a decline so a messenger sends nothing.
        f = rp.SilenceFilter()
        out = "".join(f.feed(c) for c in ("Let me check that.", "\n\n", "[[no_reply]]"))
        assert rp.SILENCE_MARKER not in out
        assert f.silenced

    def test_the_stream_filter_catches_a_marker_split_across_a_line_start(self) -> None:
        f = rp.SilenceFilter()
        out = "".join(f.feed(c) for c in ("The answer is 42.\n", "[[no", "_reply]]", " bye"))
        assert out == "The answer is 42.\n"
        assert f.silenced

    def test_a_code_block_full_of_newlines_passes_untouched(self) -> None:
        # Re-arming per line must cost nothing visible: every line start is
        # probed and released the moment it cannot be the marker.
        f = rp.SilenceFilter()
        text = "```py\nx = 1\ny = [[1, 2]]\n```\nDone."
        assert f.feed(text) + f.flush() == text
        assert not f.silenced

    def test_a_marker_mid_sentence_streams_through(self) -> None:
        f = rp.SilenceFilter()
        text = f"The marker is `{rp.SILENCE_MARKER}` and that's it."
        assert f.feed(text) + f.flush() == text
        assert not f.silenced


class TestSilenceFilter:
    def test_a_marker_split_across_chunks_never_reaches_the_wire(self) -> None:
        f = rp.SilenceFilter()
        out = "".join(f.feed(c) for c in ("[[no", "_re", "ply]]"))
        assert out == ""
        assert f.silenced

    def test_text_after_the_marker_is_dropped_too(self) -> None:
        # A model that declines and then keeps talking must not deliver the
        # tail — a fragment of a reply it chose not to send.
        f = rp.SilenceFilter()
        f.feed(rp.SILENCE_MARKER)
        assert f.feed(" actually, here is my answer") == ""

    def test_an_ordinary_reply_is_released_whole_and_in_order(self) -> None:
        f = rp.SilenceFilter()
        chunks = ["[[not", " a marker", " at all"]
        assert "".join(f.feed(c) for c in chunks) == "".join(chunks)
        assert not f.silenced

    def test_the_filter_becomes_a_pass_through_once_decided(self) -> None:
        f = rp.SilenceFilter()
        f.feed("Hello")
        assert f.feed(" world") == " world"

    def test_a_reply_shorter_than_the_marker_survives_the_flush(self) -> None:
        f = rp.SilenceFilter()
        assert f.feed("Hi") == "Hi"
        assert f.flush() == ""
        assert not f.silenced

    def test_a_turn_cut_off_inside_the_marker_stays_silent(self) -> None:
        # max_output_tokens, a stop, or a provider hiccup mid-marker: emitting
        # "[[no_repl" into a customer's chat is the one outcome to avoid.
        f = rp.SilenceFilter()
        assert f.feed("[[no_repl") == ""
        assert f.flush() == ""
        assert f.silenced

    def test_whitespace_alone_is_not_reported_as_a_policy_hit(self) -> None:
        f = rp.SilenceFilter()
        f.feed("   ")
        assert f.flush() == "   "
        assert not f.silenced


# ── The prompt block ─────────────────────────────────────────────────────────


class TestPromptBlock:
    def test_nothing_is_injected_without_rules(self) -> None:
        assert rp.policy_prompt_block(None) == ""
        assert rp.policy_prompt_block(_policy(max_input_chars=10, ignore_patterns=["x"])) == ""

    def test_rules_and_the_marker_both_reach_the_model(self) -> None:
        block = rp.policy_prompt_block(_policy(ignore_rules=["The message is not in English"]))
        assert "The message is not in English" in block
        assert rp.SILENCE_MARKER in block

    def test_the_block_scopes_rules_to_chat_messages(self) -> None:
        # L0 is built once per agent, so a scheduled task's prompt sees the
        # same block a Telegram message does. Without this sentence a Spanish
        # schedule entry plus a "not written in English" rule silences the
        # scheduled run — the model applies a chat rule to a task prompt.
        block = rp.policy_prompt_block(_policy(ignore_rules=["The message is not in English"]))
        for surface in ("scheduled task", "heartbeat", "another agent"):
            assert surface in block, surface

    def test_the_block_reaches_l0(self, tmp_workspace) -> None:
        # Code-owned, like the channel-trust block: an operator's rules must not
        # depend on the archetype's authored prompt mentioning the subject.
        from olano.config.workspace import get_workspace
        from olano.core.prompt_assembler import PromptAssembler

        cfg = AgentConfig(
            name="a", message_policy={"ignore_rules": ["The message is not in English"]}
        )
        assembler = PromptAssembler(cfg, get_workspace(str(tmp_workspace)))
        assert "The message is not in English" in assembler.build_l0()

    def test_the_block_survives_minimal_prompt_mode(self, tmp_workspace) -> None:
        # `_build_minimal` keeps the channel-security block by name and used
        # to drop everything else L0 generates — so an agent trimmed to a
        # minimal prompt for cost (the public support desk, typically) got no
        # rules at all, and nothing said so.
        from olano.config.workspace import get_workspace
        from olano.core.prompt_assembler import PromptAssembler

        cfg = AgentConfig(
            name="a",
            prompt_mode="minimal",
            message_policy={"ignore_rules": ["The message is not in English"]},
        )
        prompt = PromptAssembler(cfg, get_workspace(str(tmp_workspace))).build()
        assert "The message is not in English" in prompt
        assert rp.SILENCE_MARKER in prompt

    def test_the_rules_are_a_stable_section_so_an_edit_hot_swaps(self, tmp_workspace) -> None:
        # The runtime's prompt-refresh sweep diffs `stable_sections()`; a block
        # absent from it changes only on restart. The size cap and patterns
        # are read per message, so without this the dashboard's "applies to
        # the next message" would be true for two of the three and silently
        # false for the rules.
        from olano.config.workspace import get_workspace
        from olano.core.prompt_assembler import PromptAssembler

        ws = get_workspace(str(tmp_workspace))
        before = PromptAssembler(AgentConfig(name="a"), ws).stable_sections()["policies"]
        after = PromptAssembler(
            AgentConfig(name="a", message_policy={"ignore_rules": ["No Klingon"]}), ws
        ).stable_sections()["policies"]
        assert "No Klingon" not in before
        assert "No Klingon" in after

    def test_an_agent_without_rules_gets_no_l0_cost(self, tmp_workspace) -> None:
        from olano.config.workspace import get_workspace
        from olano.core.prompt_assembler import PromptAssembler

        assembler = PromptAssembler(AgentConfig(name="a"), get_workspace(str(tmp_workspace)))
        assert "Reply policy" not in assembler.build_l0()


# ── Output cap ───────────────────────────────────────────────────────────────


class TestOutputLimit:
    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            ("anthropic", "max_tokens"),
            ("openai", "max_tokens"),
            ("olano", "max_tokens"),
            ("openrouter", "max_tokens"),
            ("google_genai", "max_output_tokens"),
            ("google_vertexai", "max_output_tokens"),
            # ChatOllama has no `max_tokens` at all, so the default spelling
            # would be accepted and then ignored — a cap that silently does
            # nothing is the worst outcome for this feature.
            ("ollama", "num_predict"),
            ("", "max_tokens"),
        ],
    )
    def test_each_provider_gets_its_own_spelling(self, provider: str, expected: str) -> None:
        assert output_limit_param(provider) == expected
        assert apply_output_limit(provider, {}, 300) == {expected: 300}

    def test_no_cap_leaves_kwargs_untouched(self) -> None:
        assert apply_output_limit("anthropic", {"temperature": 0.2}, None) == {"temperature": 0.2}

    def test_the_policy_field_overrides_a_stale_model_kwarg(self) -> None:
        assert apply_output_limit("anthropic", {"max_tokens": 4000}, 300)["max_tokens"] == 300

    def test_a_spelling_from_another_provider_is_dropped(self) -> None:
        # A config that switched provider could otherwise reach Ollama with a
        # stray `max_tokens` beside the `num_predict` we set.
        assert apply_output_limit("ollama", {"max_tokens": 4000}, 300) == {"num_predict": 300}

    def test_the_cap_reaches_the_built_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from olano.core.builder import _build_model

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        model = _build_model("anthropic:claude-sonnet-4-6", {}, None, max_output_tokens=300)
        assert model.max_tokens == 300

    def test_subagent_models_are_not_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A subagent's output is working material the parent reads, not the
        # reply the cap is about; truncating it corrupts the delegation. The
        # two subagent call sites pass no cap, which this pins by construction.
        from olano.core.builder import _build_model

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        model = _build_model("anthropic:claude-sonnet-4-6", {}, None)
        assert model.max_tokens != 300


class TestOutputCapFollowsAModelOverride:
    async def test_the_per_chat_model_picker_keeps_the_cap(
        self, base_config: AgentConfig, mock_agent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The dashboard's model picker builds a transient agent from
        # `config.model_copy(update={"model": …})`. The cap has to ride along,
        # or switching model in the chat header silently uncaps the agent.
        from unittest.mock import AsyncMock, MagicMock

        from olano.core import builder
        from olano.core.runner import AgentRunner

        base_config.message_policy.max_output_tokens = 300
        seen: dict[str, AgentConfig] = {}

        async def fake_build(cfg, *a, **k):
            seen["cfg"] = cfg
            return MagicMock()

        monkeypatch.setattr(builder, "build_agent_async", AsyncMock(side_effect=fake_build))
        runner = AgentRunner(mock_agent, base_config)
        # Must differ from the configured model, or `_agent_for_model` treats
        # it as "no override" and never builds anything.
        override = "openai:gpt-5.5" if base_config.model != "openai:gpt-5.5" else "xai:grok-5"
        _agent, cfg = await runner._agent_for_model(override)
        assert seen["cfg"] is cfg
        assert cfg.model == override
        assert cfg.message_policy.max_output_tokens == 300


class TestTruncationContinuationIsOffUnderACap:
    """The interaction that would have made this feature *raise* cost.

    `runner._continue_truncated_response` re-invokes the whole graph on
    `finish_reason: "length"`, up to `truncation_retry_limit` times. Under an
    explicit ceiling that reason is the expected end of a capped reply, so
    continuing would both undo the cap and multiply the bill.
    """

    @pytest.fixture
    def runner(self, base_config, mock_agent):
        from olano.core.runner import AgentRunner

        return AgentRunner(mock_agent, base_config)

    async def test_a_capped_reply_is_not_continued(self, runner) -> None:
        runner.config.message_policy.max_output_tokens = 300
        called = False

        async def _fail(*a, **k):
            nonlocal called
            called = True
            raise AssertionError("continuation must not run under a cap")

        runner.agent.ainvoke = _fail
        assert await runner._continue_truncated_response("t", "length") == []
        assert not called

    async def test_an_uncapped_reply_still_continues(self, runner) -> None:
        # The existing behaviour must survive for every agent that set nothing.
        assert runner.config.message_policy.max_output_tokens is None
        calls = 0

        async def _once(*a, **k):
            nonlocal calls
            calls += 1
            return {"messages": [{"role": "assistant", "content": "more"}]}

        runner.agent.ainvoke = _once
        await runner._continue_truncated_response("t", "length")
        assert calls >= 1


class TestTheRunnerStream:
    """The filter where it actually runs — `astream_events`'s `_take_text`."""

    @staticmethod
    def _runner(config: AgentConfig, chunks: list[str]):
        from unittest.mock import AsyncMock, MagicMock

        from langchain_core.messages import AIMessageChunk

        from olano.core.runner import AgentRunner

        agent = MagicMock()

        async def stream(*args, **kwargs):
            for chunk in chunks:
                yield (AIMessageChunk(content=chunk), {})

        agent.astream = stream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock(return_value={"messages": []})
        return AgentRunner(agent, config)

    @pytest.fixture
    def silencing_config(self, base_config: AgentConfig) -> AgentConfig:
        base_config.message_policy = MessagePolicyConfig(
            ignore_rules=["The message is not written in English"]
        )
        return base_config

    async def test_a_declined_turn_emits_no_text_and_writes_no_row(
        self, silencing_config: AgentConfig
    ) -> None:
        # Split across chunks the way a real stream delivers it — the marker
        # must never surface as text, and the transcript must not record a
        # reply the sender never received.
        runner = self._runner(silencing_config, ["[[no", "_reply", "]]"])
        events = [ev async for ev in runner.astream_events("hola", thread_id="t1")]
        assert [p for k, p in events if k == "text"] == []
        assert any(k == "message_policy" for k, _ in events)
        assert [r for r in runner.load_session("t1") if r["role"] == "assistant"] == []

    async def test_an_ordinary_reply_is_unchanged_and_reports_nothing(
        self, silencing_config: AgentConfig
    ) -> None:
        runner = self._runner(silencing_config, ["Sure — ", "here you go."])
        events = [ev async for ev in runner.astream_events("hi", thread_id="t2")]
        assert "".join(p for k, p in events if k == "text") == "Sure — here you go."
        assert not any(k == "message_policy" for k, _ in events)

    async def test_a_reply_shorter_than_the_marker_still_arrives(
        self, silencing_config: AgentConfig
    ) -> None:
        # "Hi" is held by the filter until it can rule the marker out; without
        # the end-of-turn flush the whole reply would vanish.
        runner = self._runner(silencing_config, ["Hi"])
        events = [ev async for ev in runner.astream_events("hi", thread_id="t3")]
        assert "".join(p for k, p in events if k == "text") == "Hi"
        assert runner.load_session("t3")[-1]["content"] == "Hi"

    async def test_ainvoke_collapses_a_declined_reply_to_nothing(
        self, silencing_config: AgentConfig
    ) -> None:
        # The non-streaming entry point: REST `/api/chat`, peers, scheduled
        # turns. It is a wrapper over the stream path, so a decline must come
        # back as "" — never the marker — and must not leave an assistant row
        # claiming a reply was sent.
        runner = self._runner(silencing_config, [rp.SILENCE_MARKER])
        assert await runner.ainvoke("hola", thread_id="t5") == ""
        rows = [r for r in runner.load_session("t5") if r["role"] == "assistant"]
        assert rows == [], rows

        # A preamble before the marker streams (a console draws it) and is
        # the only thing the row keeps; the caller still gets "" because the
        # turn was declined.
        runner = self._runner(silencing_config, ["Let me check.", "\n\n", rp.SILENCE_MARKER])
        assert await runner.ainvoke("hola", thread_id="t5b") == ""
        rows = [r for r in runner.load_session("t5b") if r["role"] == "assistant"]
        assert all(rp.SILENCE_MARKER not in r["content"] for r in rows)

    async def test_a_marker_after_a_preamble_streams_the_preamble_then_declines(
        self, silencing_config: AgentConfig
    ) -> None:
        # End to end through the runner, not just the filter: the preamble
        # goes out (a console draws it), the marker never does, the turn is
        # reported as declined, and the persisted row holds only the preamble.
        runner = self._runner(silencing_config, ["Let me check that.", "\n\n", "[[no_reply]]"])
        events = [ev async for ev in runner.astream_events("hola", thread_id="t6")]
        text = "".join(p for k, p in events if k == "text")
        assert rp.SILENCE_MARKER not in text
        assert "Let me check that." in text
        assert any(k == "message_policy" for k, _ in events)
        rows = [r for r in runner.load_session("t6") if r["role"] == "assistant"]
        assert all(rp.SILENCE_MARKER not in r["content"] for r in rows)

    async def test_an_agent_without_rules_installs_no_filter(
        self, base_config: AgentConfig
    ) -> None:
        # The marker is only meaningful where rules exist; elsewhere it is
        # ordinary text and must pass through untouched, so enabling the
        # feature nowhere costs nothing and changes nothing.
        runner = self._runner(base_config, ["[[no_reply]]"])
        events = [ev async for ev in runner.astream_events("hi", thread_id="t4")]
        assert "".join(p for k, p in events if k == "text") == "[[no_reply]]"


# ── Ingress seams agree ──────────────────────────────────────────────────────


class TestSurfacesShareOneDefinition:
    def test_every_ingress_site_calls_the_shared_module(self) -> None:
        """Five surfaces, one policy — pinned by import, because the bug this
        feature can have is one seam quietly behaving differently."""
        from pathlib import Path

        root = Path(__file__).parent.parent / "olano"
        sites = {
            "runtime/server.py": "reply_policy",
            "runtime/api.py": "reply_policy",
            "platforms/api_server.py": "reply_policy",
            "core/runner.py": "reply_policy",
            "core/prompt_assembler.py": "reply_policy",
        }
        for path, needle in sites.items():
            assert needle in (root / path).read_text(encoding="utf-8"), path

    def test_the_classic_and_platform_channels_both_screen_and_both_silence(self) -> None:
        from pathlib import Path

        source = (Path(__file__).parent.parent / "olano" / "runtime" / "server.py").read_text(
            encoding="utf-8"
        )
        # One handler per channel family, each needing both halves: the ingress
        # screen and the "declined" short-circuit ahead of `render_events`
        # (which would otherwise turn a text-less turn into "(done — N tool
        # call(s))" and answer a message the agent chose not to answer).
        # `ascreen_inbound`, not `screen_inbound`: the async form is what keeps
        # a backtracking pattern off the event loop (see `ascreen_inbound`).
        assert source.count("await _rp.ascreen_inbound(") == 2
        assert source.count("_rp.screen_inbound(") == 0
        # The decline decision is one helper, asked before `render_events` in
        # both handlers: the helper's own tests cover the logic, this pins
        # that neither handler re-implements it or asks it too late.
        assert source.count("_rp.declined_reply(") == 2
        for handler_src in source.split("_rp.declined_reply(")[1:]:
            assert handler_src.index("return _declined") < handler_src.index(
                "response = render_events("
            )


class TestDeclinedReply:
    """The channel handlers' decision, tested where it lives."""

    def test_a_turn_without_the_event_renders_as_usual(self) -> None:
        assert rp.declined_reply([("text", "hi")], None) is None
        assert rp.declined_reply([], _policy(ignore_rules=["x"])) is None

    def test_a_declined_turn_sends_nothing_by_default(self) -> None:
        collected = [("message_policy", {"action": "no_reply"})]
        assert rp.declined_reply(collected, _policy(ignore_rules=["x"])) == ""
        assert rp.declined_reply(collected, None) == ""

    def test_the_operators_notice_replaces_silence(self) -> None:
        collected = [("message_policy", {"action": "no_reply"})]
        policy = _policy(ignore_rules=["x"], ignore_notice="Sorry, English only.")
        assert rp.declined_reply(collected, policy) == "Sorry, English only."

    def test_a_decline_after_tool_calls_still_sends_nothing(self) -> None:
        # The trap the ordering exists for: the model looked something up,
        # then declined. Rendered first, `normal` verbosity would answer with
        # "(done — 1 tool call(s): …)". Asked first, it is silence.
        from olano.platforms.verbosity import render_events

        collected = [
            ("tool_call", {"id": "c1", "name": "web_search", "args": "{}"}),
            ("tool_result", {"id": "c1", "name": "web_search", "content": "…"}),
            ("message_policy", {"action": "no_reply", "source": "ignore_rules"}),
        ]
        assert rp.declined_reply(collected, _policy(ignore_rules=["x"])) == ""
        # …and this is what a handler that rendered first would have sent.
        assert "done" in render_events(collected, "normal")
