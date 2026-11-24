"""What the customer-facing user guide and the platform skills may say.

`olano/archetypes/knowledge_user_guide.md` is seeded into every agent workspace
and RAG-indexed, and the `olano-*` built-in skills are advertised to every
agent. Both teach the platform through its own surfaces — the dashboard and the
agents' built-in tools — never through the `olano` command line, which an agent
must never run or recommend (a person in a chat has no shell). This pins that,
and pins the emphasis the guide exists to carry: the self-configuration,
magic-link and admin tool families are named, so a rewrite cannot quietly drop
them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "olano" / "archetypes" / "knowledge_user_guide.md"
BUILT_IN = ROOT / "olano" / "skills" / "built_in"
PLATFORM_SKILLS = sorted(BUILT_IN.glob("olano-*/SKILL.md"))

# A backticked CLI invocation: `olano agents list`, `lionclaw vault set …`.
# Model ids (`olano:turbo`), hostnames (`cloud.olano.ai`) and tool names
# (`olano_list_agents`) do not match — the space after the word is the tell.
_CLI_INLINE = re.compile(r"`(olano|lionclaw)\s+[a-z][a-z-]*")
# A shell line inside a fenced block.
_CLI_FENCED = re.compile(r"^\s*(?:\$\s*)?(olano|lionclaw)\s+[a-z][a-z-]*", re.MULTILINE)


def _cli_mentions(text: str) -> list[str]:
    return [m.group(0) for m in _CLI_INLINE.finditer(text)] + [
        m.group(0) for m in _CLI_FENCED.finditer(text)
    ]


def test_the_user_guide_never_teaches_the_cli() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    assert _cli_mentions(text) == []
    # Prose mentions too: the guide describes surfaces, and a terminal is not one.
    # The sandbox's own binaries are "command-line tools" there, never "the CLI".
    assert not re.search(r"\b(the|olano|lionclaw) CLI\b", text)


@pytest.mark.parametrize("skill_md", PLATFORM_SKILLS, ids=lambda p: p.parent.name)
def test_platform_skills_never_teach_the_cli(skill_md: Path) -> None:
    text = skill_md.read_text(encoding="utf-8")
    # The one allowed shape is the rule itself ("never run … the `olano` command line").
    allowed = re.compile(r"`olano` command line")
    assert _cli_mentions(allowed.sub("", text)) == []


def test_the_platform_skill_set_is_complete() -> None:
    assert {p.parent.name for p in PLATFORM_SKILLS} == {
        "olano-admin",
        "olano-agent",
        "olano-agent-builder",
        "olano-cloud",
        "olano-platform",
    }


def test_the_guide_names_the_tool_families_it_exists_to_emphasise() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    for name in (
        "magic_link_request_secret",
        "magic_link_connect_service",
        "magic_link_connect_channels",
        "self_get_config",
        "self_find_integration",
        "self_add_toolkit",
        "self_add_mcp_server",
        "self_connect_platform",
        "olano_list_agents",
        "olano_create_user",
        "olano_diagnose_agent",
    ):
        assert name in text, name
    # The integration ladder is stated where connections are explained:
    # supported (channel → MCP server → built-in toolkit) before anything
    # external, found with one tool.
    assert "Supported integrations come first" in text


def test_the_guide_keeps_the_confidentiality_lines() -> None:
    """Mirrors CLAUDE.md's "what must never appear": the module paths that once
    leaked through the seeded engineering docs, and — in the skills, which have
    no reason to mention it — the retired `olano:reasoning` tier id (the guide
    itself keeps one note saying it no longer works)."""
    text = GUIDE.read_text(encoding="utf-8")
    assert not re.search(r"\bolano/(core|runtime|config|tools|skills)/[a-z_]+\.py", text)
    for skill_md in PLATFORM_SKILLS:
        skill_text = skill_md.read_text(encoding="utf-8")
        assert "olano:reasoning" not in skill_text, skill_md
        assert not re.search(r"\bolano/(core|runtime|config|tools)/[a-z_]+\.py", skill_text), (
            skill_md
        )
