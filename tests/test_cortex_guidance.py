"""Tests for optional CORTEX.md creative direction (olano/cognition/guidance.py)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from olano.cognition.guidance import (
    CORTEX_FILE,
    CORTEX_TEMPLATE,
    append_guidance,
    cortex_guidance_context,
    prepend_guidance,
    read_cortex_guidance,
)
from olano.cognition.worker import WorkerOutcome
from olano.config.agent_config import AgentConfig


@pytest.fixture(autouse=True)
def _configured_model(monkeypatch):
    """The curation-context test drives curate_pulse past its readiness preflight;
    provide a dummy provider key so it reaches the mocked worker."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


# ──────────────────────────────── reader ────────────────────────────────────


def test_read_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_cortex_guidance(tmp_path) == ""


def test_read_seeded_template_is_ignored(tmp_path: Path) -> None:
    # The seeded template is substance-free (headings + commented hints), so it
    # must NOT be injected until real bullets are added.
    (tmp_path / CORTEX_FILE).write_text(CORTEX_TEMPLATE, encoding="utf-8")
    assert read_cortex_guidance(tmp_path) == ""


def test_read_returns_content_when_bullets_present(tmp_path: Path) -> None:
    (tmp_path / CORTEX_FILE).write_text(
        "# CORTEX.md\n\n## Goals\n- Ship the dashboard\n\n## Avoid\n- No crypto\n",
        encoding="utf-8",
    )
    body = read_cortex_guidance(tmp_path)
    assert "Ship the dashboard" in body
    assert "No crypto" in body


def test_read_strips_comments(tmp_path: Path) -> None:
    (tmp_path / CORTEX_FILE).write_text(
        "## Goals\n<!-- hidden hint -->\n- Real goal\n", encoding="utf-8"
    )
    body = read_cortex_guidance(tmp_path)
    assert "Real goal" in body
    assert "hidden hint" not in body


def test_read_truncates_oversized_file(tmp_path: Path) -> None:
    big = "## Goals\n" + "\n".join(f"- goal {i}" for i in range(5000))
    (tmp_path / CORTEX_FILE).write_text(big, encoding="utf-8")
    body = read_cortex_guidance(tmp_path, max_chars=200)
    assert len(body) <= 220
    assert body.endswith("[truncated]")


# ──────────────────────────── context / prepend ─────────────────────────────


def test_context_empty_without_guidance(tmp_path: Path) -> None:
    assert cortex_guidance_context(tmp_path) == ""


def test_context_mentions_cortex_and_body(tmp_path: Path) -> None:
    (tmp_path / CORTEX_FILE).write_text("## Goals\n- Build X\n", encoding="utf-8")
    ctx = cortex_guidance_context(tmp_path)
    assert "CORTEX.md" in ctx
    assert "Build X" in ctx


def test_prepend_no_guidance_returns_context_unchanged(tmp_path: Path) -> None:
    assert prepend_guidance(tmp_path, "existing context") == "existing context"
    assert prepend_guidance(tmp_path, None) == ""


def test_prepend_places_guidance_first(tmp_path: Path) -> None:
    (tmp_path / CORTEX_FILE).write_text("## Goals\n- Build X\n", encoding="utf-8")
    out = prepend_guidance(tmp_path, "EXISTING")
    assert "Build X" in out
    assert out.strip().endswith("EXISTING")
    assert out.index("Build X") < out.index("EXISTING")


# ──────────────────────────── append_guidance ───────────────────────────────


def test_append_creates_file_and_section(tmp_path: Path) -> None:
    written = append_guidance(tmp_path, "Ship the app", section="Goals")
    assert written == "Goals"
    body = read_cortex_guidance(tmp_path)
    assert "- Ship the app" in body


def test_append_dedupes_within_section(tmp_path: Path) -> None:
    append_guidance(tmp_path, "Ship the app", section="Goals")
    append_guidance(tmp_path, "ship the app", section="Goals")  # normalized dup
    text = (tmp_path / CORTEX_FILE).read_text(encoding="utf-8")
    assert text.lower().count("ship the app") == 1


def test_append_normalizes_section_aliases(tmp_path: Path) -> None:
    assert append_guidance(tmp_path, "no ads", section="don't want") == "Avoid"
    assert append_guidance(tmp_path, "dream big", section="aspirations") == "Dreams"
    text = (tmp_path / CORTEX_FILE).read_text(encoding="utf-8")
    assert "## Avoid" in text
    assert "## Dreams" in text


def test_append_empty_content_is_noop(tmp_path: Path) -> None:
    assert append_guidance(tmp_path, "   ", section="Goals") == ""
    assert not (tmp_path / CORTEX_FILE).exists()


def test_append_into_seeded_template_keeps_headings(tmp_path: Path) -> None:
    (tmp_path / CORTEX_FILE).write_text(CORTEX_TEMPLATE, encoding="utf-8")
    append_guidance(tmp_path, "Grow the newsletter", section="Goals")
    body = read_cortex_guidance(tmp_path)
    assert "Grow the newsletter" in body


# ──────────────────────── injection into the phases ─────────────────────────


@pytest.fixture
def cfg_with_guidance(base_config: AgentConfig, tmp_workspace: Path) -> AgentConfig:
    (tmp_workspace / CORTEX_FILE).write_text(
        "## Goals\n- Build a finance tool\n\n## Avoid\n- No crypto ideas\n",
        encoding="utf-8",
    )
    return base_config


async def test_brainstorm_context_includes_guidance(
    cfg_with_guidance: AgentConfig, tmp_workspace: Path
) -> None:
    from olano.cognition.ideation import run_brainstorm

    mock = AsyncMock(return_value=WorkerOutcome(status="empty", summary=""))
    with patch("olano.cognition.ideation.run_worker", new=mock):
        await run_brainstorm(cfg_with_guidance, tmp_workspace, budget_check=False)
    ctx = mock.call_args.kwargs["context"]
    assert "Build a finance tool" in ctx
    assert "No crypto ideas" in ctx


async def test_idea_generation_context_includes_guidance(
    cfg_with_guidance: AgentConfig, tmp_workspace: Path
) -> None:
    from olano.cognition.ideation import run_idea_generation

    mock = AsyncMock(return_value=WorkerOutcome(status="empty", summary=""))
    with patch("olano.cognition.ideation.run_worker", new=mock):
        await run_idea_generation(cfg_with_guidance, tmp_workspace, budget_check=False)
    assert "Build a finance tool" in mock.call_args.kwargs["context"]


async def test_prototype_context_includes_guidance(
    cfg_with_guidance: AgentConfig, tmp_workspace: Path
) -> None:
    from olano.cognition.ideation import run_prototype

    mock = AsyncMock(return_value=WorkerOutcome(status="empty", summary=""))
    with patch("olano.cognition.ideation.run_worker", new=mock):
        await run_prototype(cfg_with_guidance, tmp_workspace, budget_check=False)
    assert "Build a finance tool" in mock.call_args.kwargs["context"]


async def test_no_guidance_means_no_injection(
    base_config: AgentConfig, tmp_workspace: Path
) -> None:
    from olano.cognition.ideation import run_brainstorm

    mock = AsyncMock(return_value=WorkerOutcome(status="empty", summary=""))
    with patch("olano.cognition.ideation.run_worker", new=mock):
        await run_brainstorm(base_config, tmp_workspace, budget_check=False)
    assert "CORTEX.md" not in mock.call_args.kwargs["context"]


async def test_pulse_curation_context_includes_guidance(
    cfg_with_guidance: AgentConfig, tmp_workspace: Path
) -> None:
    from olano.cognition import pulse_agent

    generator = SimpleNamespace(config=cfg_with_guidance, workspace_root=tmp_workspace)
    mock = AsyncMock(return_value=WorkerOutcome(status="empty", summary=""))
    with patch("olano.cognition.pulse_agent.run_worker", new=mock):
        await pulse_agent.curate_pulse(generator, force=True)
    assert "Build a finance tool" in (mock.call_args.kwargs["context"] or "")


# ──────────────────────────────── the tool ──────────────────────────────────


def test_cortex_guidance_save_tool_writes(tmp_workspace: Path) -> None:
    from olano.memory.markdown_memory import MarkdownMemory, make_memory_tools

    tools = {t.name: t for t in make_memory_tools(MarkdownMemory(tmp_workspace))}
    assert "cortex_guidance_save" in tools
    out = tools["cortex_guidance_save"].invoke({"content": "Ship v1", "section": "Goals"})
    assert "CORTEX.md" in out
    assert "Ship v1" in read_cortex_guidance(tmp_workspace)


# ──────────────────────────── installer seeding ─────────────────────────────


def test_installer_seeds_substance_free_cortex(tmp_path: Path) -> None:
    from olano.archetypes.installer import scaffold_agent_workspace

    ws = tmp_path / "ws"
    scaffold_agent_workspace("seed-agent", {"emoji": "🦁"}, ws)
    assert (ws / CORTEX_FILE).exists()
    # Seeded but ignored until the user/agent adds real bullets.
    assert read_cortex_guidance(ws) == ""
