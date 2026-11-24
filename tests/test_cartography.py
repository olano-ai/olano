"""Tests for olano.cartography.*"""

from __future__ import annotations

import pytest


@pytest.fixture
def workspace_dir(tmp_path):
    """Create a minimal workspace for testing."""
    (tmp_path / "AGENTS.md").write_text("# Agent Instructions\n", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("# Soul\n", encoding="utf-8")
    (tmp_path / "USER.md").write_text("# User\n", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("# Memory\n\n- Fact one\n", encoding="utf-8")
    memories = tmp_path / "memories"
    memories.mkdir()
    from datetime import date

    (memories / f"{date.today().isoformat()}.md").write_text("Today's log\n", encoding="utf-8")
    skills = tmp_path / "skills" / "my_skill"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: my_skill\ndescription: A skill\n---\n", encoding="utf-8"
    )
    return tmp_path


class TestClassifier:
    def test_classify_agents_md(self, workspace_dir):
        from olano.cartography.classifier import classify_file

        path = workspace_dir / "AGENTS.md"
        category, label = classify_file(path, workspace_dir)
        assert category == "governance"

    def test_classify_soul_md(self, workspace_dir):
        from olano.cartography.classifier import classify_file

        path = workspace_dir / "SOUL.md"
        category, label = classify_file(path, workspace_dir)
        assert category == "identity"

    def test_classify_memory_md(self, workspace_dir):
        from olano.cartography.classifier import classify_file

        path = workspace_dir / "MEMORY.md"
        category, label = classify_file(path, workspace_dir)
        assert category == "memory:curated"

    def test_classify_daily_log(self, workspace_dir):
        from datetime import date

        from olano.cartography.classifier import classify_file

        path = workspace_dir / "memories" / f"{date.today().isoformat()}.md"
        category, label = classify_file(path, workspace_dir)
        assert category == "memory:daily"

    def test_classify_skill(self, workspace_dir):
        from olano.cartography.classifier import classify_file

        path = workspace_dir / "skills" / "my_skill" / "SKILL.md"
        category, label = classify_file(path, workspace_dir)
        assert category == "skill"

    def test_classify_report_as_deliverable(self, workspace_dir):
        from olano.cartography.classifier import category_emoji, classify_file

        path = workspace_dir / "reports" / "q3" / "revenue.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG")
        category, label = classify_file(path, workspace_dir)
        assert category == "deliverable"
        assert category_emoji("deliverable") == "📦"
        scratch = workspace_dir / "scratch" / "build.py"
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text("x", encoding="utf-8")
        assert classify_file(scratch, workspace_dir)[0] == "scratch"

    def test_classify_unknown_py_file(self, workspace_dir):
        from olano.cartography.classifier import classify_file

        path = workspace_dir / "script.py"
        path.write_text("print('hello')", encoding="utf-8")
        category, label = classify_file(path, workspace_dir)
        assert category == "code"

    def test_category_emoji(self):
        from olano.cartography.classifier import category_emoji

        assert category_emoji("governance") != "📄"
        assert category_emoji("memory:curated") != "📄"


class TestMapGenerator:
    def test_generates_map(self, workspace_dir):
        from olano.cartography.map_generator import generate_workspace_map

        result = generate_workspace_map(workspace_dir)
        assert "Workspace" in result
        assert len(result) <= 3200  # Max chars + small buffer

    def test_map_includes_agents_md(self, workspace_dir):
        from olano.cartography.map_generator import generate_workspace_map

        result = generate_workspace_map(workspace_dir)
        assert "AGENTS.md" in result

    def test_map_respects_max_chars(self, workspace_dir):
        from olano.cartography.map_generator import generate_workspace_map

        # Create many files
        for i in range(50):
            (workspace_dir / f"doc_{i}.md").write_text(f"# Doc {i}\n", encoding="utf-8")

        result = generate_workspace_map(workspace_dir, max_chars=500)
        assert len(result) <= 600  # Allow small buffer for truncation marker

    def test_save_workspace_map(self, workspace_dir):
        from olano.cartography.map_generator import save_workspace_map

        path = save_workspace_map(workspace_dir)
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "Workspace" in content


class TestCartographer:
    @pytest.fixture
    def workspace(self, workspace_dir):
        from olano.config.workspace import Workspace

        return Workspace(root=workspace_dir)

    def test_run_incremental(self, workspace):
        from olano.cartography.cartographer import WorkspaceCartographer

        cart = WorkspaceCartographer(workspace)
        cart.run_incremental()  # Should not raise

        # Workspace map and bridge should be generated
        assert workspace.workspace_map_path.exists()
        assert workspace.workspace_bridge_path.exists()

    def test_health_score_full_workspace(self, workspace):
        from olano.cartography.cartographer import WorkspaceCartographer

        cart = WorkspaceCartographer(workspace)
        cart._phase_scan_and_classify()
        health = cart._phase_health()

        assert 0 <= health["score"] <= 100
        assert "issues" in health

    def test_health_score_empty_workspace(self, tmp_path):
        from olano.cartography.cartographer import WorkspaceCartographer
        from olano.config.workspace import Workspace

        ws = Workspace(root=tmp_path)
        cart = WorkspaceCartographer(ws)
        cart._phase_scan_and_classify()
        health = cart._phase_health()

        assert health["score"] < 80  # Missing required files
        assert len(health["issues"]) > 0

    def test_get_map_returns_string(self, workspace):
        from olano.cartography.cartographer import WorkspaceCartographer

        cart = WorkspaceCartographer(workspace)
        cart.run_incremental()
        result = cart.get_map()
        assert isinstance(result, str)
        assert len(result) > 0
