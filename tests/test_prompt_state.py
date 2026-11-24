"""Tests for olano.core.prompt_state — stable-section fingerprinting,
seen-map diffing, and the in-thread update notice."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from olano.config.agent_config import AgentConfig
from olano.core.prompt_state import (
    SECTION_TITLES,
    PromptState,
    compute_prompt_state,
    diff_seen_map,
    render_prompt_update_notice,
)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "MEMORY.md").write_text("# Memory\n- fact one\n", encoding="utf-8")
    (tmp_path / "memories").mkdir()
    return tmp_path


def make_config(ws: Path, **overrides) -> AgentConfig:
    base = dict(
        name="fp-agent",
        model="anthropic:claude-sonnet-4-6",
        system_prompt="You are a fingerprinted agent.",
        workspace=str(ws),
    )
    base.update(overrides)
    return AgentConfig(**base)


class TestComputePromptState:
    def test_full_mode_always_carries_the_stable_keys(self, ws: Path) -> None:
        state = compute_prompt_state(make_config(ws))
        # roster is absent (no runtime registry bound in tests); everything
        # else is present even when the backing file is empty/missing, so
        # clearing a file later still registers as a change.
        assert set(state.fingerprint) == {
            "identity",
            "instructions",
            "policies",
            "environment",
            "tools",
        }
        assert state.sections["instructions"].startswith("You are a fingerprinted agent.")

    def test_volatile_edits_do_not_move_the_fingerprint(self, ws: Path) -> None:
        config = make_config(ws)
        before = compute_prompt_state(config).fingerprint

        (ws / "MEMORY.md").write_text("# Memory\n- totally new fact\n", encoding="utf-8")
        (ws / "memories" / f"{date.today().isoformat()}.md").write_text(
            "worked on things\n", encoding="utf-8"
        )
        state_dir = ws / ".olano"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "workspace_map.md").write_text("# Map\nnew map\n", encoding="utf-8")
        (ws / "IDEAS.md").write_text("## Ideas\n- a big idea\n", encoding="utf-8")

        assert compute_prompt_state(config).fingerprint == before

    def test_system_prompt_edit_moves_only_instructions(self, ws: Path) -> None:
        before = compute_prompt_state(make_config(ws)).fingerprint
        after = compute_prompt_state(
            make_config(ws, system_prompt="You are a rewritten agent.")
        ).fingerprint
        assert after["instructions"] != before["instructions"]
        for key in before:
            if key != "instructions":
                assert after[key] == before[key]

    def test_agents_md_edit_moves_only_instructions(self, ws: Path) -> None:
        config = make_config(ws)
        before = compute_prompt_state(config).fingerprint
        (ws / "AGENTS.md").write_text("# Ops\nAlways answer in French.\n", encoding="utf-8")
        after = compute_prompt_state(config).fingerprint
        assert after["instructions"] != before["instructions"]
        for key in before:
            if key != "instructions":
                assert after[key] == before[key]

    def test_customer_service_extension_is_part_of_instructions(self, ws: Path) -> None:
        enabled = compute_prompt_state(
            make_config(ws, customer_service={"enabled": True, "prompt_extension": "SCOPE RULES"})
        )
        disabled = compute_prompt_state(
            make_config(ws, customer_service={"enabled": False, "prompt_extension": "SCOPE RULES"})
        )
        assert "SCOPE RULES" in enabled.sections["instructions"]
        assert "SCOPE RULES" not in disabled.sections["instructions"]
        assert enabled.fingerprint["instructions"] != disabled.fingerprint["instructions"]

    def test_policy_file_edit_moves_only_policies(self, ws: Path) -> None:
        config = make_config(ws)
        before = compute_prompt_state(config).fingerprint
        system = ws / "system" / "prompts"
        system.mkdir(parents=True)
        (system / "SAFETY_BOUNDARIES.md").write_text("# Safety\nNever rm -rf.\n", encoding="utf-8")
        after = compute_prompt_state(config).fingerprint
        assert after["policies"] != before["policies"]
        assert after["instructions"] == before["instructions"]

    def test_non_full_prompt_mode_is_inert(self, ws: Path) -> None:
        for mode in ("minimal", "task", "none"):
            state = compute_prompt_state(make_config(ws, prompt_mode=mode))
            assert state.fingerprint == {}
            assert state.sections == {}

    def test_never_raises_on_broken_config(self) -> None:
        state = compute_prompt_state(None)  # type: ignore[arg-type]
        assert state.fingerprint == {}


class TestDiffSeenMap:
    def _state(self, sections: dict[str, str], fingerprint: dict[str, str]) -> PromptState:
        return PromptState(sections=sections, fingerprint=fingerprint, built_at="t")

    def test_no_change(self) -> None:
        current = self._state({"instructions": "A"}, {"instructions": "h1"})
        changed, removed, updated = diff_seen_map({"instructions": "h1"}, current)
        assert changed == {} and removed == [] and updated == {"instructions": "h1"}

    def test_changed_section_returns_new_text(self) -> None:
        current = self._state({"instructions": "NEW TEXT"}, {"instructions": "h2"})
        changed, removed, updated = diff_seen_map({"instructions": "h1"}, current)
        assert changed == {"instructions": "NEW TEXT"}
        assert removed == []
        assert updated == {"instructions": "h2"}

    def test_new_key_adopted_silently(self) -> None:
        current = self._state(
            {"instructions": "A", "roster": "peers"},
            {"instructions": "h1", "roster": "r1"},
        )
        changed, removed, updated = diff_seen_map({"instructions": "h1"}, current)
        assert changed == {} and removed == []
        assert updated == {"instructions": "h1", "roster": "r1"}

    def test_key_missing_from_current_is_kept(self) -> None:
        # A CLI process can't compute the roster — its absence must not
        # clobber the daemon's marker or read as a change.
        current = self._state({"instructions": "A"}, {"instructions": "h1"})
        changed, removed, updated = diff_seen_map({"instructions": "h1", "roster": "r1"}, current)
        assert changed == {} and removed == []
        assert updated == {"instructions": "h1", "roster": "r1"}

    def test_cleared_section_reports_removed(self) -> None:
        current = self._state({"instructions": ""}, {"instructions": "h-empty"})
        changed, removed, updated = diff_seen_map({"instructions": "h1"}, current)
        assert changed == {}
        assert removed == ["instructions"]
        assert updated == {"instructions": "h-empty"}


class TestRenderNotice:
    def test_empty_inputs_render_nothing(self) -> None:
        assert render_prompt_update_notice({}, []) == ""

    def test_notice_contains_sections_in_registry_order(self) -> None:
        notice = render_prompt_update_notice(
            {"tools": "tool text", "instructions": "instr text"}, ["roster"]
        )
        assert notice.startswith("[System update — your operating instructions changed]")
        assert "### Updated: Instructions\ninstr text" in notice
        assert "### Removed: Agents you can collaborate with" in notice
        assert "### Updated: Tools guidance & inventory\ntool text" in notice
        # Registry order: instructions before roster before tools.
        assert (
            notice.index("Updated: Instructions")
            < notice.index("Removed: Agents you can collaborate with")
            < notice.index("Updated: Tools guidance")
        )
        assert "Apply these updates from now on." in notice

    def test_oversized_section_is_truncated(self) -> None:
        notice = render_prompt_update_notice({"instructions": "x" * 5000}, [])
        assert "…[truncated" in notice
        assert len(notice) < 5000

    def test_total_cap_collapses_largest_sections(self) -> None:
        changed = {key: "y" * 2900 for key in ("identity", "instructions", "policies", "tools")}
        notice = render_prompt_update_notice(changed, [])
        assert len(notice) <= 8000
        assert "(Updated — the new version is in effect in your current instructions.)" in notice

    def test_titles_cover_all_registry_keys(self) -> None:
        # Guard: every stable section key renders with a human title.
        for key in SECTION_TITLES:
            notice = render_prompt_update_notice({key: "text"}, [])
            assert f"### Updated: {SECTION_TITLES[key]}" in notice
