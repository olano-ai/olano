"""Tests for MarkdownMemory — long-term and daily file memory."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from olano.memory.markdown_memory import MarkdownMemory, make_memory_tools


@pytest.fixture
def mem(tmp_workspace: Path) -> MarkdownMemory:
    return MarkdownMemory(tmp_workspace)


class TestMarkdownMemoryLongTerm:
    def test_read_default(self, mem: MarkdownMemory) -> None:
        content = mem.read()
        assert "MEMORY" in content

    def test_append_fact_creates_section(self, mem: MarkdownMemory) -> None:
        mem.append_fact("User prefers Python", section="Preferences")
        content = mem.read()
        assert "## Preferences" in content
        assert "User prefers Python" in content

    def test_append_fact_idempotent_section(self, mem: MarkdownMemory) -> None:
        mem.append_fact("Fact A", section="Notes")
        mem.append_fact("Fact B", section="Notes")
        content = mem.read()
        assert content.count("## Notes") == 1
        assert "Fact A" in content
        assert "Fact B" in content

    def test_search(self, mem: MarkdownMemory) -> None:
        mem.append_fact("User likes TypeScript")
        results = mem.search("typescript")
        assert any("TypeScript" in r for r in results)

    def test_search_matches_related_user_profile_terms(self, mem: MarkdownMemory) -> None:
        mem.write(
            "# Memory\n\n"
            "## User Profile\n"
            "- [2026-06-01] User's name is Carlos; prefers casual, direct communication\n"
            "- [2026-06-01] Stock interests: Qualcomm (QCOM), Nike (NKE), and Apple (AAPL)\n"
        )

        for query in (
            "preferred stocks QCOM NKE AAPL",
            "User stocks interest",
            "Carlos likes stocks",
        ):
            results = mem.search(query)
            assert results
            assert "Qualcomm (QCOM), Nike (NKE), and Apple (AAPL)" in results[0]

    def test_search_no_results(self, mem: MarkdownMemory) -> None:
        results = mem.search("xyzzy_impossible_string")
        assert results == []

    def test_get_section(self, mem: MarkdownMemory) -> None:
        mem.append_fact("Uses PostgreSQL 15", section="Tech Stack")
        content = mem.get_section("Tech Stack")
        assert "PostgreSQL" in content

    def test_get_missing_section(self, mem: MarkdownMemory) -> None:
        assert mem.get_section("NonExistent") == ""

    def test_list_sections(self, mem: MarkdownMemory) -> None:
        mem.append_fact("Fact 1", section="Alpha")
        mem.append_fact("Fact 2", section="Beta")
        sections = mem.list_sections()
        assert "Alpha" in sections
        assert "Beta" in sections

    def test_summarize_and_trim(self, mem: MarkdownMemory) -> None:
        # Inflate content beyond limit
        mem.write("# Memory\n\n" + "x" * 25_000)
        mem.summarize_and_trim(max_chars=20_000)
        assert len(mem.read()) <= 20_100  # small headroom


class TestMarkdownMemoryDailyFiles:
    def test_append_and_read_today(self, mem: MarkdownMemory) -> None:
        mem.append_daily("Worked on tests")
        content = mem.read_today()
        assert "Worked on tests" in content

    def test_daily_file_created(self, mem: MarkdownMemory, tmp_workspace: Path) -> None:
        mem.append_daily("Entry one")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = tmp_workspace / "memories" / f"{today}.md"
        assert path.exists()

    def test_read_yesterday_empty(self, mem: MarkdownMemory) -> None:
        content = mem.read_yesterday()
        assert content == ""

    def test_multiple_daily_entries(self, mem: MarkdownMemory) -> None:
        mem.append_daily("Morning standup notes")
        mem.append_daily("Afternoon PR review")
        content = mem.read_today()
        assert "Morning standup notes" in content
        assert "Afternoon PR review" in content

    def test_list_daily_files(self, mem: MarkdownMemory) -> None:
        mem.append_daily("Entry")
        files = mem.list_daily_files()
        assert len(files) >= 1

    def test_search_daily(self, mem: MarkdownMemory) -> None:
        mem.append_daily("Deployed version 2.0")
        results = mem.search_daily("deployed", days=1)
        assert any("2.0" in r for r in results)


class TestMemoryTools:
    def test_tools_created(self, mem: MarkdownMemory) -> None:
        tools = make_memory_tools(mem)
        names = [t.name for t in tools]
        assert "memory_save" in names
        assert "memory_search" in names
        assert "memory_read" in names
        assert "memory_append_daily" in names
        assert "memory_read_today" in names
        assert "memory_search_recent" in names
        assert "user_profile_save" in names
        assert "identity_preference_save" in names
        assert "soul_preference_save" in names

    def test_memory_save_tool(self, mem: MarkdownMemory) -> None:
        tools = {t.name: t for t in make_memory_tools(mem)}
        result = tools["memory_save"].invoke({"fact": "Test fact", "section": "Tests"})
        assert "Saved" in result
        assert "Test fact" in mem.read()

    def test_memory_read_today_tool(self, mem: MarkdownMemory) -> None:
        mem.append_daily("Today's entry")
        tools = {t.name: t for t in make_memory_tools(mem)}
        result = tools["memory_read_today"].invoke({})
        assert "Today's entry" in result

    def test_user_profile_save_tool_writes_user_md(
        self, mem: MarkdownMemory, tmp_workspace: Path
    ) -> None:
        tools = {t.name: t for t in make_memory_tools(mem)}
        result = tools["user_profile_save"].invoke({"key": "Preferred name", "value": "Carlitos"})
        assert "Saved to USER.md" in result
        assert "Carlitos" in (tmp_workspace / "USER.md").read_text(encoding="utf-8")

    def test_identity_preference_save_tool_writes_identity_md(
        self, mem: MarkdownMemory, tmp_workspace: Path
    ) -> None:
        tools = {t.name: t for t in make_memory_tools(mem)}
        result = tools["identity_preference_save"].invoke(
            {"key": "Preferred display name", "value": "Wukong"}
        )
        assert "Saved to IDENTITY.md" in result
        content = (tmp_workspace / "IDENTITY.md").read_text(encoding="utf-8")
        assert "User-Guided Identity Preferences" in content
        assert "Wukong" in content

    def test_identity_preference_save_updates_canonical_identity_fields(
        self, mem: MarkdownMemory, tmp_workspace: Path
    ) -> None:
        (tmp_workspace / "IDENTITY.md").write_text(
            "<!-- lionclaw:first-run -->\n# Identity\n\n<!-- lionclaw:blank-persona -->\n"
            "**Name:** *Not chosen yet*\n"
            "**Emoji:** 🤖\n"
            "**Role:** *To be discovered together during the first conversations.*\n",
            encoding="utf-8",
        )
        tools = {t.name: t for t in make_memory_tools(mem)}

        result_name = tools["identity_preference_save"].invoke({"key": "name", "value": "Marcus"})
        result_role = tools["identity_preference_save"].invoke(
            {"key": "role", "value": "team lead"}
        )

        content = (tmp_workspace / "IDENTITY.md").read_text(encoding="utf-8")
        assert result_name == "Saved to IDENTITY.md: Name = Marcus"
        assert result_role == "Saved to IDENTITY.md: Role = team lead"
        assert "**Name:** Marcus" in content
        assert "**Role:** team lead" in content
        assert "- **Name**: Marcus" in content
        assert "- **Role**: team lead" in content

    def test_user_profile_save_normalizes_common_keys(
        self, mem: MarkdownMemory, tmp_workspace: Path
    ) -> None:
        tools = {t.name: t for t in make_memory_tools(mem)}
        result = tools["user_profile_save"].invoke({"key": "preferred_name", "value": "Carlos"})

        content = (tmp_workspace / "USER.md").read_text(encoding="utf-8")
        assert result == "Saved to USER.md: Preferred name = Carlos"
        assert "**Preferred name**: Carlos" in content

    def test_soul_preference_save_tool_writes_soul_md(
        self, mem: MarkdownMemory, tmp_workspace: Path
    ) -> None:
        tools = {t.name: t for t in make_memory_tools(mem)}
        result = tools["soul_preference_save"].invoke(
            {"key": "Communication style", "value": "Keep it real and direct"}
        )
        assert "Saved to SOUL.md" in result
        content = (tmp_workspace / "SOUL.md").read_text(encoding="utf-8")
        assert "User-Guided Communication Preferences" in content
        assert "Keep it real and direct" in content
