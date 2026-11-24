"""Tests for olano.core.prompt_assembler."""

from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture
def workspace_dir(tmp_path):
    """Create a workspace with standard files."""
    (tmp_path / "AGENTS.md").write_text("# Agent Instructions\nBe helpful.\n", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("# Soul\nI am a helpful agent.\n", encoding="utf-8")
    (tmp_path / "USER.md").write_text("# User\nThe user is a developer.\n", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("# Memory\n- Fact one\n- Fact two\n", encoding="utf-8")

    memories = tmp_path / "memories"
    memories.mkdir()
    today = date.today().isoformat()
    (memories / f"{today}.md").write_text(f"# {today}\nToday's log entry.\n", encoding="utf-8")

    # State dir — workspace.state_dir = root / ".olano"
    state = tmp_path / ".olano"
    state.mkdir(parents=True)
    (state / "workspace_map.md").write_text(
        "# Workspace Map\n📂 root/\n  📄 AGENTS.md [governance]\n", encoding="utf-8"
    )

    # System zone
    system = tmp_path / "system" / "prompts"
    system.mkdir(parents=True)
    (system / "SAFETY_BOUNDARIES.md").write_text(
        "# Safety\nDo not execute destructive commands.\n", encoding="utf-8"
    )

    return tmp_path


@pytest.fixture
def workspace(workspace_dir):
    from olano.config.workspace import Workspace

    return Workspace(root=workspace_dir)


@pytest.fixture
def config():
    from olano.config.agent_config import (
        AgentConfig,
        BudgetConfig,
        MemoryConfig,
        PulseConfig,
        SecurityConfig,
        WalletConfig,
    )

    return AgentConfig(
        name="test_agent",
        model="anthropic:claude-sonnet-4-6",
        system_prompt="You are a test agent.",
        security=SecurityConfig(),
        budget=BudgetConfig(),
        pulse=PulseConfig(),
        wallet=WalletConfig(),
        memory=MemoryConfig(),
    )


@pytest.fixture
def assembler(config, workspace):
    from olano.core.prompt_assembler import PromptAssembler

    return PromptAssembler(config, workspace)


class TestMcpBlock:
    def test_no_block_without_summary(self, assembler):
        assert assembler._build_mcp_block() == ""

    def test_block_lists_connected_and_down(self, config, workspace):
        from olano.core.prompt_assembler import PromptAssembler

        summary = [
            {
                "url": "https://mcp.hubspot.com",
                "transport": "streamable_http",
                "ok": True,
                "status": "connected",
                "tools": ["hs_contacts", "hs_deals"],
                "message": "2 tools",
            },
            {
                "url": "https://mcp.stripe.com",
                "transport": "streamable_http",
                "ok": False,
                "status": "auth_required",
                "tools": [],
                "message": "OAuth not authorized",
            },
        ]
        block = PromptAssembler(config, workspace, mcp_summary=summary)._build_mcp_block()
        assert "MCP servers (live status)" in block
        # Connected server + its tool names are surfaced.
        assert "hs_contacts" in block and "mcp.hubspot.com" in block
        # Down server is flagged as not-connected, not claimed as usable.
        assert "mcp.stripe.com" in block and "auth_required" in block
        assert "not connected" in block


class TestL0Assembly:
    def test_l0_includes_safety_boundaries(self, assembler):
        l0 = assembler.build_l0()
        assert "Safety" in l0 or "destructive" in l0

    def test_l0_includes_workspace_root_guidance(self, assembler, workspace_dir):
        l0 = assembler.build_l0()
        assert str(workspace_dir.resolve()) in l0
        assert "/workspace/..." in l0

    def test_l0_states_the_workspace_layout_and_hand_off_rule(self, assembler):
        """The layout rules are what stop deliverables from landing in the root,
        in duplicate copies, or being described as "rendering inline"."""
        l0 = assembler.build_l0()
        assert "- Workspace layout — follow it strictly:" in l0
        assert "`reports/` — every file you produce for people" in l0
        assert "`scratch/` — scripts, intermediates" in l0
        assert "`uploads/` — files people sent you (read-only)" in l0
        assert "never produced files" in l0
        assert "One canonical file per deliverable" in l0
        assert "Saved: <path>" in l0
        assert "use the `charts` skill" in l0

    def test_l0_includes_workspace_map(self, assembler):
        l0 = assembler.build_l0()
        assert "Workspace Map" in l0 or "AGENTS.md" in l0

    def test_l0_includes_execution_bias_when_present(self, config, workspace, workspace_dir):
        from olano.core.prompt_assembler import PromptAssembler

        (workspace_dir / "system" / "prompts" / "EXECUTION_BIAS.md").write_text(
            "# Execution Bias (System)\n\nKeep working until the task is genuinely done.\n",
            encoding="utf-8",
        )
        l0 = PromptAssembler(config, workspace).build_l0()
        assert "## Execution Bias" in l0
        assert "Keep working until the task is genuinely done." in l0

    def test_l0_respects_max_chars(self, config, workspace):
        from olano.core.prompt_assembler import _L0_MAX_CHARS, PromptAssembler

        # Fill workspace_map.md with lots of content
        state_dir = workspace.state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "workspace_map.md").write_text("x" * 10_000, encoding="utf-8")

        assembler = PromptAssembler(config, workspace)
        l0 = assembler.build_l0()
        # Allow small buffer for truncation marker
        assert len(l0) <= _L0_MAX_CHARS + 100

    def test_l0_empty_workspace(self, config, tmp_path):
        from olano.config.workspace import Workspace
        from olano.core.prompt_assembler import PromptAssembler

        ws = Workspace(root=tmp_path)
        assembler = PromptAssembler(config, ws)
        l0 = assembler.build_l0()
        # Should not raise, may return empty string
        assert isinstance(l0, str)


class TestL1Assembly:
    def test_l1_includes_identity_not_agents(self, assembler):
        # AGENTS.md is now the system prompt (in build()), not in L1.
        # L1 includes IDENTITY, SOUL, USER instead.
        l1 = assembler.build_l1()
        # L1 should have at least one of the persona files
        assert any(x in l1 for x in ("SOUL", "USER", "IDENTITY", "Memory"))

    def test_l1_includes_memory(self, assembler):
        l1 = assembler.build_l1()
        assert "Memory" in l1 or "Fact one" in l1

    def test_l1_includes_daily_log(self, assembler):
        l1 = assembler.build_l1()
        today = date.today().isoformat()
        assert today in l1 or "Today's log" in l1

    def test_l1_includes_pulse_items(self, assembler):
        pulse_items = [
            {"source": "system", "priority": 80, "content": "Check budget usage"},
        ]
        l1 = assembler.build_l1(pulse_items=pulse_items)
        assert "PULSE" in l1 or "Check budget usage" in l1

    def test_l1_pulse_capped_at_five(self, assembler):
        pulse_items = [
            {"source": "system", "priority": i, "content": f"Item {i}"} for i in range(10)
        ]
        l1 = assembler.build_l1(pulse_items=pulse_items)
        # Should include at most 5
        count = sum(1 for i in range(10) if f"Item {i}" in l1)
        assert count <= 5

    def test_l1_respects_max_chars(self, config, workspace):
        from olano.core.prompt_assembler import _L1_MAX_CHARS, PromptAssembler

        # Make MEMORY.md massive
        (workspace.root / "MEMORY.md").write_text("x" * 30_000, encoding="utf-8")
        assembler = PromptAssembler(config, workspace)
        l1 = assembler.build_l1()
        assert len(l1) <= _L1_MAX_CHARS + 100

    def test_l1_tool_inventory_includes_generated_memory_tools(self, assembler):
        l1 = assembler.build_l1()
        assert "user_profile_save" in l1
        assert "memory_save" in l1


class TestPromptModes:
    def test_none_mode_injects_anti_hallucination_guard(self, assembler):
        prompt = assembler.build(mode="none")

        assert "none` mode" in prompt
        assert "No Olano startup context" in prompt
        assert "Do not invent file contents" in prompt


class TestSnapshotsAndCache:
    def test_full_prompt_snapshot_is_stable(self, config, workspace_dir, workspace, monkeypatch):
        from olano.core import prompt_assembler as prompt_assembler_module
        from olano.core.prompt_assembler import PromptAssembler

        class FakeDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 4, 12)

        monkeypatch.setattr(prompt_assembler_module, "date", FakeDate)
        (workspace_dir / "memories" / "2026-04-12.md").write_text(
            "# 2026-04-12\nToday's log entry.\n", encoding="utf-8"
        )
        assembler = PromptAssembler(config, workspace)

        result = assembler.build()

        expected = (
            "# Instructions\n"
            "You are a test agent.\n\n"
            "## Additional instructions (AGENTS.md)\n"
            "# Agent Instructions\n"
            "Be helpful.\n\n"
            "---\n\n"
            # No subagents and no peers: the guardrails get their own section.
            "# Tools\n"
            "## Tool-use guardrails\n"
            "Your tools are exactly: "
            "`approvals_approve`, `approvals_clear_expired`, `approvals_defer`, "
            "`approvals_list`, `approvals_reject`, "
            "`chat_info`, `chat_messages_recent`, `chat_messages_search`, "
            "`chats_find`, `chats_find_user`, "
            # No cortex_* item tools: the Cortex engine is off by default, and
            # they are bound only for an agent it is on for.
            "`chats_list`, `document_to_text`, `execute`, `get_current_date`, "
            "`get_current_time`, "
            "`heartbeat_add`, `heartbeat_configure`, `heartbeat_install_from_skill`, "
            "`heartbeat_list`, `heartbeat_remove`, `heartbeat_status`, `heartbeat_update`, "
            "`issue_assign`, `issue_close`, `issue_comment`, "
            "`issue_create`, `issue_list`, `issue_reopen`, `issue_show`, "
            "`notification_send_document`, "
            "`notification_send_photo`, `notification_send_text`, "
            "`ops_clear_expired`, `ops_defer`, `ops_list`, `ops_reject`, `pdf_to_text`, "
            "`pulse_accept`, `pulse_clear_expired`, `pulse_defer`, `pulse_generate`, "
            "`pulse_list`, `pulse_reject`, `pulse_undo`, "
            "`render_html_image`, `render_html_pdf`, `render_html_svg`, "
            "`scheduler_create`, `scheduler_delete`, `scheduler_get`, `scheduler_history`, "
            "`scheduler_list`, `scheduler_run_now`, `scheduler_search`, `scheduler_update`, "
            "`talk_to_agent`, "
            "`trash_empty`, `trash_list`, `trash_put`, `trash_restore`, "
            "`wait_n_seconds`, `web_fetch`, `web_search`, `whoami` "
            "— plus the auto-injected memory, notes, and secret helpers your "
            "runtime always provides. The full list shows up in the formal tool "
            "spec the model receives alongside this prompt.\n"
            "**You have shell access via the `execute` tool.** Use `execute` to "
            "run shell commands directly in your workspace whenever it helps you "
            "make progress. On failure, follow the error-recovery protocol (read "
            "the error, change approach, don't repeat the same call). Destructive "
            "commands still require confirmation.\n"
            "Never invent tool names. If you can't find the right tool in your "
            "spec, tell the user what's missing instead of guessing.\n\n"
            "---\n\n"
            "# Context (L0 — Core)\n"
            "## Safety\n"
            "# Safety\n"
            "Do not execute destructive commands.\n\n"
            "## Handling Credentials & Secrets\n"
            "When the user shares an API key, token, password, or other secret in chat:\n"
            "\n"
            "1. The transcript may show the value as `[API_KEY_...REDACTED]` — that is the "
            "Olano security middleware sanitising the conversation. The real value never "
            "reaches you. Do NOT ask the user to paste it again; that won't help.\n"
            "2. Suggest the encrypted credentials vault instead. Two ways to land the secret "
            "there:\n"
            "   - **Recommended:** ask the user to run "
            "`olano vault set <NAME>` from a terminal. The plaintext stays out of chat "
            "entirely.\n"
            "   - If the user insists on doing it from chat, you can call the `secret_set` "
            "tool — it is owner-only and HITL-gated.\n"
            "3. Once stored, the value is auto-injected into the chat session's environment on "
            'boot, so any tool / MCP server that reads `os.environ["<NAME>"]` (Alpaca, '
            "Stripe, Shopify, GitHub, …) will pick it up. The user does NOT need to set the env "
            "var manually.\n"
            '4. If the user mentions "I added X to the vault", call `secret_list` first to '
            "confirm it is there, then use it (via env or `secret_get`). Never assume.\n"
            "5. The vault requires a master key. If the chat says "
            '"Credentials vault locked" on startup, the user needs to either set '
            "`OLANO_VAULT_KEY=<passphrase>` or install the `keyring` package — tell them, "
            "do not silently work around it.\n"
            "\n"
            "Tool naming reminder:\n"
            "- `secret_get` / `secret_list` / `secret_set` → encrypted credentials vault "
            "(API keys, tokens).\n"
            "- `notes_get` / `notes_add` / `notes_search` / `notes_list` / `notes_backlinks` → "
            "workspace notes Markdown wiki (NOT for secrets).\n\n"  # join boundary
            "## Workspace Environment\n"
            f"- Workspace root: `{workspace_dir.resolve()}`\n"
            "- For filesystem tools, prefer workspace-relative paths like `SOUL.md` or "
            "`memories/2026-04-18.md`.\n"
            "- `/workspace/...` maps to this same root for container-style paths.\n"
            "- Shared paths, when active, appear under `shared/`.\n"
            "- For local or downloaded PDFs, Word docs, slide decks, spreadsheets, "
            "HTML, CSV/TSV, Markdown, JSON, and text files, use `document_to_text` "
            "directly on the workspace path, then summarize from the extracted text. "
            "For PDFs, `pdf_to_text` is also available as a focused shortcut. Do not "
            "use web scraping tools for `file://` URLs.\n"
            "- Use the `task` tool only for genuinely large independent work; do not "
            "delegate simple local file reads, PDF extraction, or one-file summaries.\n"
            "- Do not probe `/`, `/home`, or other host roots unless the user gave you "
            "an active shared path there.\n"
            "- Workspace layout — follow it strictly:\n"
            "  - `reports/` — every file you produce for people (charts, images, PDFs, "
            "spreadsheets, documents, exports); `reports/<topic>/` when one job yields "
            "several.\n"
            "  - `scratch/` — scripts, intermediates, crops, test renders; never a "
            "deliverable.\n"
            "  - `uploads/` — files people sent you (read-only). `downloads/`, `images/`, "
            "`renders/`, `screenshots/`, `web_scraping/`, `trash/` belong to the tools "
            "that write them.\n"
            "  - The workspace root holds only identity and memory files — never produced "
            "files.\n"
            "  - One canonical file per deliverable: revise in place; no copies, no "
            "`-v2`/`final`, no duplicate casing; lowercase-with-hyphens names "
            "(`reports/accounts-by-country.png`).\n"
            "- Hand-off: end with `Saved: <path>` (absolute or workspace-relative) on its "
            "own line, bare (no backticks) — that line shows the file in the dashboard "
            "chat (images inline, other files as a card with Download / Open in Files) and "
            "attaches it on chat channels.\n"
            "  - **Your final message is the only place a file can be handed over.** A path "
            "that appears only in a tool result is never attached, so a file you do not "
            "name in your answer is one the person cannot open.\n"
            "  - Name **every** file you produced or updated for the person this turn — one "
            "`Saved: <path>` line each, each with its full path — unless the person asked "
            "you not to. Repeating a path you already gave is fine: a file is attached once "
            "however many times it is named.\n"
            "  - Name only deliverables. Files you merely read, `scratch/` intermediates, "
            "and paths you are quoting from a document are not hand-offs — do not put a "
            "`Saved:` line on them.\n"
            '  - Never say a file "renders inline" or "is attached" unless a tool result '
            "said so."
            "\n- Charts and data visuals: use the `charts` skill (its script plus "
            "`render_html_image`); never install plotting libraries or fonts.\n\n"
            "## Workspace Map\n"
            "# Workspace Map\n"
            "📂 root/\n"
            "  📄 AGENTS.md [governance]\n\n"
            "---\n\n"
            "# Context (L1 — Session)\n"
            "## SOUL\n"
            "# Soul\n"
            "I am a helpful agent.\n\n"
            "## USER\n"
            "# User\n"
            "The user is a developer.\n\n"
            "## Memory\n"
            "# Memory\n"
            "- Fact one\n"
            "- Fact two\n\n"
            "## Daily Log (2026-04-12)\n"
            "# 2026-04-12\n"
            "Today's log entry.\n\n"
            "## Tool Inventory\n"
            "- `approvals_approve`\n"
            "- `approvals_clear_expired`\n"
            "- `approvals_defer`\n"
            "- `approvals_list`\n"
            "- `approvals_reject`\n"
            "- `chat_info`\n"
            "- `chat_messages_recent`\n"
            "- `chat_messages_search`\n"
            "- `chats_find`\n"
            "- `chats_find_user`\n"
            "- `chats_list`\n"
            "- `cortex_guidance_save`\n"
            "- `document_to_text`\n"
            "- `get_current_date`\n"
            "- `get_current_time`\n"
            "- `graph_add_entity`\n"
            "- `graph_add_relationship`\n"
            "- `graph_get_neighbors`\n"
            "- `graph_search_entities`\n"
            "- `graph_summary`\n"
            "- `heartbeat_add`\n"
            "- `heartbeat_configure`\n"
            "- `heartbeat_install_from_skill`\n"
            "- `heartbeat_list`\n"
            "- `heartbeat_remove`\n"
            "- `heartbeat_status`\n"
            "- `heartbeat_update`\n"
            "- `identity_preference_save`\n"
            "- `issue_assign`\n"
            "- `issue_close`\n"
            "- `issue_comment`\n"
            "- `issue_create`\n"
            "- `issue_list`\n"
            "- `issue_reopen`\n"
            "- `issue_show`\n"
            "- `memory_append_daily`\n"
            "- `memory_compact`\n"
            "- `memory_read`\n"
            "- `memory_read_today`\n"
            "- `memory_save`\n"
            "- `memory_search`\n"
            "- `memory_search_recent`\n"
            "- `notification_send_document`\n"
            "- `notification_send_photo`\n"
            "- `notification_send_text`\n"
            "- `ops_clear_expired`\n"
            "- `ops_defer`\n"
            "- `ops_list`\n"
            "- `ops_reject`\n"
            "- `pdf_to_text`\n"
            "- `pulse_accept`\n"
            "- `pulse_clear_expired`\n"
            "- `pulse_defer`\n"
            "- `pulse_generate`\n"
            "- `pulse_list`\n"
            "- `pulse_reject`\n"
            "- `pulse_undo`\n"
            "- `render_html_image`\n"
            "- `render_html_pdf`\n"
            "- `render_html_svg`\n"
            "- `scheduler_create`\n"
            "- `scheduler_delete`\n"
            "- `scheduler_get`\n"
            "- `scheduler_history`\n"
            "- `scheduler_list`\n"
            "- `scheduler_run_now`\n"
            "- `scheduler_search`\n"
            "- `scheduler_update`\n"
            "- `soul_preference_save`\n"
            "- `talk_to_agent`\n"
            "- `trash_empty`\n"
            "- `trash_list`\n"
            "- `trash_put`\n"
            "- `trash_restore`\n"
            "- `user_profile_save`\n"
            "- `wait_n_seconds`\n"
            "- `web_fetch`\n"
            "- `web_search`\n"
            "- `whoami`\n"
            "- `workspace_context`\n\n"
            "**Memory rule**: When the user explicitly states personal "
            "information (name, preferences, timezone, goals), call "
            "`user_profile_save` in the SAME response — before or alongside your "
            "reply. Save only what the user actually said: never invent or guess a "
            "value, never save on a bare greeting or small talk, and never save a "
            "name or role drawn from system context or login metadata. "
            "Never say you saved something without first calling the tool."
        )

        assert result == expected

    def test_cache_optimization_increases_stable_prefix_when_instructions_change(
        self, config, workspace_dir, workspace
    ):
        from olano.core.prompt_assembler import PromptAssembler

        def common_prefix_chars(left: str, right: str) -> int:
            count = 0
            for a, b in zip(left, right, strict=False):
                if a != b:
                    break
                count += 1
            return count

        (workspace_dir / "IDENTITY.md").write_text("# Identity\nBuilder\n", encoding="utf-8")

        assembler = PromptAssembler(config, workspace)
        optimized_first = assembler.build()
        (workspace_dir / "AGENTS.md").write_text(
            "# Agent Instructions\nBe precise, but now focus on code review.\n", encoding="utf-8"
        )
        optimized_second = PromptAssembler(config, workspace).build()

        config.prompt_cache.optimize_boundaries = False
        (workspace_dir / "AGENTS.md").write_text(
            "# Agent Instructions\nBe helpful.\n", encoding="utf-8"
        )
        unoptimized_first = PromptAssembler(config, workspace).build()
        (workspace_dir / "AGENTS.md").write_text(
            "# Agent Instructions\nBe precise, but now focus on code review.\n", encoding="utf-8"
        )
        unoptimized_second = PromptAssembler(config, workspace).build()

        optimized_prefix = common_prefix_chars(optimized_first, optimized_second)
        unoptimized_prefix = common_prefix_chars(unoptimized_first, unoptimized_second)

        assert optimized_prefix > unoptimized_prefix
        assert optimized_prefix - unoptimized_prefix >= 30


class TestFullBuild:
    def test_build_includes_system_prompt(self, assembler, workspace_dir):
        # Instructions are additive: config.system_prompt + authored AGENTS.md.
        # The fixture writes AGENTS.md "Be helpful." and config.system_prompt
        # "You are a test agent." — both must appear.
        result = assembler.build()
        assert "Be helpful" in result
        assert "You are a test agent." in result

    def test_build_includes_l0_and_l1(self, assembler):
        result = assembler.build()
        assert "Context (L0" in result or "Safety" in result
        assert "Context (L1" in result or "Memory" in result

    def test_pristine_scaffold_agents_does_not_shadow_config_prompt(
        self, config, workspace_dir, workspace
    ):
        """A never-edited AGENTS.md placeholder must fall back to config.system_prompt.

        Regression: agents whose rich prompt lives in agent.json5 (system_prompt)
        were rendered generic because Workspace.init() always seeds a non-empty
        placeholder AGENTS.md, which used to win over config.system_prompt.
        """
        from olano.config.workspace import BOOTSTRAP_DEFAULTS
        from olano.core.prompt_assembler import PromptAssembler

        # Replace the fixture's authored AGENTS.md with the seeded scaffold.
        (workspace_dir / "AGENTS.md").write_text(BOOTSTRAP_DEFAULTS["AGENTS.md"], encoding="utf-8")
        result = PromptAssembler(config, workspace).build()
        assert "You are a test agent." in result  # config.system_prompt is used
        assert "Add operating instructions and memory here" not in result  # placeholder dropped

    def test_authored_agents_appends_to_config_prompt(self, config, workspace_dir, workspace):
        """An authored AGENTS.md is ADDED to config.system_prompt (additive, not exclusive).

        Both sources must appear, under the "Additional instructions" header, so
        editing either the dashboard system-prompt field or AGENTS.md takes effect.
        """
        from olano.core.prompt_assembler import PromptAssembler

        (workspace_dir / "AGENTS.md").write_text(
            "# Agent Instructions\nBe a careful code reviewer.\n", encoding="utf-8"
        )
        result = PromptAssembler(config, workspace).build()
        assert "You are a test agent." in result  # config.system_prompt (base) kept
        assert "Be a careful code reviewer." in result  # authored AGENTS.md appended
        assert "## Additional instructions (AGENTS.md)" in result
        # Order: base before the appended AGENTS.md section.
        assert result.index("You are a test agent.") < result.index("Be a careful code reviewer.")

    def test_pristine_scaffold_identity_excluded_from_l1(self, config, workspace_dir, workspace):
        """A pristine IDENTITY.md placeholder must not leak into L1 context."""
        from olano.config.workspace import BOOTSTRAP_DEFAULTS
        from olano.core.prompt_assembler import PromptAssembler

        (workspace_dir / "IDENTITY.md").write_text(
            BOOTSTRAP_DEFAULTS["IDENTITY.md"], encoding="utf-8"
        )
        l1 = PromptAssembler(config, workspace).build_l1()
        assert "Agent name and personality" not in l1

    def test_build_no_system_prompt(self, workspace):
        from olano.config.agent_config import (
            AgentConfig,
            BudgetConfig,
            MemoryConfig,
            PulseConfig,
            SecurityConfig,
            WalletConfig,
        )
        from olano.core.prompt_assembler import PromptAssembler

        cfg = AgentConfig(
            name="no_prompt",
            model="anthropic:claude-sonnet-4-6",
            system_prompt="",
            security=SecurityConfig(),
            budget=BudgetConfig(),
            pulse=PulseConfig(),
            wallet=WalletConfig(),
            memory=MemoryConfig(),
        )
        assembler = PromptAssembler(cfg, workspace)
        result = assembler.build()
        assert isinstance(result, str)

    def _make_cfg(self, backend):
        from olano.config.agent_config import (
            AgentConfig,
            BudgetConfig,
            MemoryConfig,
            PulseConfig,
            SecurityConfig,
            WalletConfig,
        )

        return AgentConfig(
            name="shelltest",
            model="anthropic:claude-sonnet-4-6",
            system_prompt="You are a test agent.",
            backend=backend,
            security=SecurityConfig(),
            budget=BudgetConfig(),
            pulse=PulseConfig(),
            wallet=WalletConfig(),
            memory=MemoryConfig(),
        )

    def test_local_shell_backend_gets_execute_guidance(self, workspace):
        # The default backend (local_shell) exposes deepagents' `execute` tool even
        # though it never appears in config.tools — the prompt must advertise it.
        from olano.config.backend_config import LocalShellBackendConfig
        from olano.core.prompt_assembler import PromptAssembler

        cfg = self._make_cfg(LocalShellBackendConfig())
        result = PromptAssembler(cfg, workspace).build()
        assert "You have shell access via the `execute` tool" in result
        assert "`execute`" in result
        assert "You do NOT have shell access" not in result

    def test_non_shell_backend_keeps_no_shell_warning(self, workspace):
        from olano.config.backend_config import FilesystemBackendConfig
        from olano.core.prompt_assembler import PromptAssembler

        cfg = self._make_cfg(FilesystemBackendConfig())
        result = PromptAssembler(cfg, workspace).build()
        assert "You do NOT have shell access" in result
        assert "You have shell access via the `execute` tool" not in result


class TestL2Assembly:
    def test_l2_loads_specific_file(self, assembler, workspace_dir):
        custom = workspace_dir / "custom_doc.md"
        custom.write_text("# Custom\nImportant instructions.\n", encoding="utf-8")

        l2 = assembler.build_l2(file_paths=["custom_doc.md"])
        assert "Custom" in l2 or "Important instructions" in l2

    def test_l2_missing_file_skipped(self, assembler):
        l2 = assembler.build_l2(file_paths=["nonexistent.md"])
        # Should not raise, just return empty
        assert l2 == ""

    def test_l2_empty_returns_empty(self, assembler):
        l2 = assembler.build_l2()
        assert l2 == ""

    def test_l2_header_present(self, assembler, workspace_dir):
        f = workspace_dir / "info.md"
        f.write_text("Some info.\n", encoding="utf-8")
        l2 = assembler.build_l2(file_paths=["info.md"])
        assert "L2" in l2


class TestZonePrecedence:
    def test_extensions_takes_precedence_over_root(self, config, workspace_dir):
        """extensions/prompts/AGENTS.md should override root/AGENTS.md in build()."""
        from olano.config.workspace import Workspace
        from olano.core.prompt_assembler import PromptAssembler

        ext_dir = workspace_dir / "extensions" / "prompts"
        ext_dir.mkdir(parents=True)
        (ext_dir / "AGENTS.md").write_text(
            "# Overridden Agent Instructions\nCustom override.\n", encoding="utf-8"
        )

        ws = Workspace(root=workspace_dir)
        assembler = PromptAssembler(config, ws)
        # AGENTS.md is now the system prompt — it appears in build(), not build_l1()
        result = assembler.build()
        assert "Custom override" in result

    def test_system_dir_used_as_fallback(self, config, workspace_dir):
        """If root/SOUL.md is missing, system/prompts/SOUL.md should be used."""
        from olano.config.workspace import Workspace
        from olano.core.prompt_assembler import PromptAssembler

        # Remove root SOUL.md
        (workspace_dir / "SOUL.md").unlink()

        system_dir = workspace_dir / "system" / "prompts"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "SOUL.md").write_text("# System Soul\nFallback persona.\n", encoding="utf-8")

        ws = Workspace(root=workspace_dir)
        assembler = PromptAssembler(config, ws)
        l1 = assembler.build_l1()
        assert "Fallback persona" in l1


# ──────────────── P5.11: Vault auto-injection at L0 ─────────────────────────


class TestNotesAutoInjectL0:
    def test_auto_inject_entry_appears_in_l0(self, config, workspace_dir):
        """An auto_inject=true notes entry should appear in build_l0()."""
        from olano.config.workspace import Workspace
        from olano.core.prompt_assembler import PromptAssembler
        from olano.memory.knowledge_vault import NotesEntry, NotesStore

        notes_dir = workspace_dir / "notes"
        notes = NotesStore(notes_dir=notes_dir)
        notes.upsert(
            NotesEntry(
                title="Core Rule",
                content="Always respond in English.",
                auto_inject=True,
            )
        )

        ws = Workspace(root=workspace_dir)
        assembler = PromptAssembler(config, ws)
        l0 = assembler.build_l0()

        assert "Core Rule" in l0
        assert "Notes" in l0

    def test_non_auto_inject_entry_excluded_from_l0(self, config, workspace_dir):
        """Entries without auto_inject=true should NOT appear in build_l0()."""
        from olano.config.workspace import Workspace
        from olano.core.prompt_assembler import PromptAssembler
        from olano.memory.knowledge_vault import NotesEntry, NotesStore

        notes_dir = workspace_dir / "notes"
        notes = NotesStore(notes_dir=notes_dir)
        notes.upsert(
            NotesEntry(
                title="Hidden Doc",
                content="Should not appear.",
                auto_inject=False,
            )
        )

        ws = Workspace(root=workspace_dir)
        assembler = PromptAssembler(config, ws)
        l0 = assembler.build_l0()

        assert "Hidden Doc" not in l0

    def test_no_vault_dir_does_not_crash_l0(self, config, workspace_dir):
        """If notes/ directory doesn't exist, build_l0() still succeeds."""
        import shutil

        from olano.config.workspace import Workspace
        from olano.core.prompt_assembler import PromptAssembler

        # Ensure notes dir doesn't exist
        notes_dir = workspace_dir / "notes"
        if notes_dir.exists():
            shutil.rmtree(notes_dir)

        ws = Workspace(root=workspace_dir)
        assembler = PromptAssembler(config, ws)
        l0 = assembler.build_l0()  # Should not raise
        assert isinstance(l0, str)


class TestStableSections:
    """stable_sections() — the change-detection registry must mirror exactly
    what the corresponding parts of build() emit."""

    def test_sections_match_the_builders(self, assembler):
        sections = assembler.stable_sections()
        assert sections["identity"] == assembler._build_identity_block()
        assert sections["instructions"] == assembler._effective_system_prompt()
        assert sections["environment"] == assembler._build_workspace_environment()
        assert "## Safety\n" in sections["policies"]
        assert "## Tool Inventory" in sections["tools"]

    def test_instructions_text_lands_in_full_prompt(self, assembler):
        sections = assembler.stable_sections()
        assert sections["instructions"] in assembler.build()

    def test_roster_omitted_without_runtime_registry(self, assembler):
        # CLI/test processes have no bound runtime: the roster key must be
        # absent (not empty) so it can never clobber the daemon's marker.
        assert "roster" not in assembler.stable_sections()

    def test_roster_present_when_runtime_bound(self, assembler, monkeypatch):
        from olano.tools import talk_to

        monkeypatch.setattr(talk_to, "get_bound_runtime", lambda: object())
        assert "roster" in assembler.stable_sections()

    def test_volatile_files_never_appear(self, assembler, workspace_dir):
        joined = "\n".join(assembler.stable_sections().values())
        # MEMORY.md content and the workspace map are volatile context —
        # they belong to L1/L0, never to the stable registry.
        assert "Fact one" not in joined
        assert "Workspace Map" not in joined
