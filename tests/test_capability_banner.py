"""Tests for olano/cli/capability_banner.py."""

from __future__ import annotations

from olano.cli.capability_banner import render_banner


def _make_skills(names_categories: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"name": n, "category": c, "description": f"desc of {n}"} for n, c in names_categories]


# ─────────────────────────────────────────────────────────────────
# Footer counts
# ─────────────────────────────────────────────────────────────────


def test_footer_counts_match_input():
    tools = ["github_create_pr", "github_list_prs", "file_read"]
    skills = _make_skills([("plan", "workflow"), ("debug", "engineering")])
    output = render_banner(tools, skills)
    assert "3 tools" in output
    assert "2 skills" in output


def test_empty_inventory_renders_footer_only():
    output = render_banner([], [])
    assert "0 tools" in output
    assert "0 skills" in output


# ─────────────────────────────────────────────────────────────────
# Grouping
# ─────────────────────────────────────────────────────────────────


def test_groups_by_toolset_prefix():
    tools = ["github_create_pr", "github_list_prs", "file_read"]
    output = render_banner(tools, [])
    assert "github:" in output
    assert "file:" in output


def test_skills_grouped_by_category():
    skills = _make_skills(
        [
            ("plan", "workflow"),
            ("writing-plans", "workflow"),
            ("debug", "engineering"),
        ]
    )
    output = render_banner([], skills)
    assert "workflow:" in output
    assert "engineering:" in output


# ─────────────────────────────────────────────────────────────────
# Truncation (compact mode)
# ─────────────────────────────────────────────────────────────────


def test_group_item_truncation_at_three():
    tools = ["gh_a", "gh_b", "gh_c", "gh_d", "gh_e"]
    output = render_banner(tools, [])
    assert "(and 2 more)" in output


def test_group_count_truncation_at_three():
    tools = [
        "a_1",
        "b_1",
        "c_1",
        "d_1",
    ]  # 4 groups: a, b, c, d
    output = render_banner(tools, [])
    assert "(and 1 more toolsets" in output


def test_no_truncation_in_full_mode():
    tools = ["a_1", "b_1", "c_1", "d_1", "e_1"]
    output = render_banner(tools, [], full=True)
    assert "(and" not in output


# ─────────────────────────────────────────────────────────────────
# Skills truncation
# ─────────────────────────────────────────────────────────────────


def test_skill_category_count_truncation():
    skills = _make_skills(
        [
            ("s1", "cat1"),
            ("s2", "cat2"),
            ("s3", "cat3"),
            ("s4", "cat4"),
        ]
    )
    output = render_banner([], skills)
    assert "(and 1 more categories" in output


# ─────────────────────────────────────────────────────────────────
# /tools footer hint
# ─────────────────────────────────────────────────────────────────


def test_footer_hint_present():
    output = render_banner([], [])
    assert "/tools for full list" in output


# ─────────────────────────────────────────────────────────────────
# tool name without underscore
# ─────────────────────────────────────────────────────────────────


def test_tool_without_underscore_is_its_own_group():
    tools = ["read", "write"]
    output = render_banner(tools, [])
    assert "read:" in output
    assert "write:" in output
