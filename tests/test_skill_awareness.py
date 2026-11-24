"""Tests for the per-turn skill-awareness middleware and cross-dir skill enumeration."""

from __future__ import annotations

from pathlib import Path

import pytest

from olano.config.agent_config import AgentConfig
from olano.config.backend_config import FilesystemBackendConfig
from olano.config.workspace import Workspace
from olano.core.skill_awareness_middleware import (
    SkillAwarenessMiddleware,
    build_skill_awareness_middleware,
)


def _write_skill(skills_dir: Path, name: str, desc: str = "does things") -> None:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n", encoding="utf-8"
    )


@pytest.fixture
def cfg(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        name="awareness-agent",
        model="anthropic:claude-haiku-4-5-20251001",
        workspace=str(tmp_path),
        backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
    )


class _FakeReq:
    def __init__(self, state: dict, system_message=None) -> None:
        self.state = state
        self.system_message = system_message

    def override(self, *, system_message):  # mirrors langchain ModelRequest.override
        return _FakeReq(self.state, system_message)


class TestSkillAwareness:
    def test_no_new_skills_on_first_call(self, cfg: AgentConfig, tmp_path: Path) -> None:
        ws = Workspace(root=tmp_path)
        _write_skill(ws.skills_dir, "alpha")
        mw = SkillAwarenessMiddleware(config=cfg, workspace=ws)
        # deepagents' frozen baseline already contains alpha → nothing to announce.
        req = _FakeReq(state={"skills_metadata": [{"name": "alpha"}]})
        assert mw._new_skill_section(req) is None

    def test_announces_skill_added_after_start(self, cfg: AgentConfig, tmp_path: Path) -> None:
        ws = Workspace(root=tmp_path)
        _write_skill(ws.skills_dir, "alpha")
        mw = SkillAwarenessMiddleware(config=cfg, workspace=ws)
        mw._scan_ttl = 0.0  # bypass the scan memo — this test mutates skills mid-session
        req = _FakeReq(state={"skills_metadata": [{"name": "alpha"}]})
        # Seed the per-thread baseline (turn 1).
        assert mw._new_skill_section(req) is None
        # A skill learned mid-session appears under learned/.
        _write_skill(ws.learned_skills_dir, "beta", desc="brand new capability")
        section = mw._new_skill_section(req)
        assert section is not None
        assert "beta" in section
        assert "brand new capability" in section
        assert "alpha" not in section  # alpha was in the baseline

    def test_inject_appends_to_system_message(self, cfg: AgentConfig, tmp_path: Path) -> None:
        ws = Workspace(root=tmp_path)
        _write_skill(ws.skills_dir, "alpha")
        mw = SkillAwarenessMiddleware(config=cfg, workspace=ws)
        mw._scan_ttl = 0.0  # bypass the scan memo — this test mutates skills mid-session
        req = _FakeReq(state={"skills_metadata": [{"name": "alpha"}]})
        mw._new_skill_section(req)  # seed baseline
        _write_skill(ws.learned_skills_dir, "gamma")
        out = mw._inject(req)
        assert out is not req  # a new request with the injected section
        assert "gamma" in str(out.system_message.content)

    def test_skill_without_description_not_announced(
        self, cfg: AgentConfig, tmp_path: Path
    ) -> None:
        # deepagents skips skills lacking valid frontmatter (no description), so they
        # are never in the baseline — counting them would announce them every turn.
        ws = Workspace(root=tmp_path)
        _write_skill(ws.skills_dir, "alpha")
        mw = SkillAwarenessMiddleware(config=cfg, workspace=ws)
        mw._scan_ttl = 0.0  # bypass the scan memo — this test mutates skills mid-session
        req = _FakeReq(state={"skills_metadata": [{"name": "alpha"}]})
        assert mw._new_skill_section(req) is None
        # A frontmatter-less skill folder appears mid-session.
        d = ws.learned_skills_dir / "nodesc"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("# nodesc\n", encoding="utf-8")  # no description
        assert mw._new_skill_section(req) is None

    async def test_awrap_injects_off_loop(self, cfg: AgentConfig, tmp_path: Path) -> None:
        # awrap_model_call runs _inject via to_thread; the handler must still
        # receive the request with the new-skill section appended.
        ws = Workspace(root=tmp_path)
        _write_skill(ws.skills_dir, "alpha")
        mw = SkillAwarenessMiddleware(config=cfg, workspace=ws)
        mw._scan_ttl = 0.0  # bypass the scan memo — this test mutates skills mid-session
        req = _FakeReq(state={"skills_metadata": [{"name": "alpha"}]})
        mw._new_skill_section(req)  # seed baseline
        _write_skill(ws.learned_skills_dir, "delta")

        async def handler(r):
            return r

        out = await mw.awrap_model_call(req, handler)
        assert "delta" in str(out.system_message.content)

    def test_build_returns_single_middleware(self, cfg: AgentConfig, tmp_path: Path) -> None:
        mws = build_skill_awareness_middleware(config=cfg, workspace=Workspace(root=tmp_path))
        assert len(mws) == 1
        assert isinstance(mws[0], SkillAwarenessMiddleware)


class TestIterAgentSkills:
    def test_dedup_last_source_wins(self, cfg: AgentConfig, tmp_path: Path) -> None:
        from olano.skills.loader import iter_agent_skills

        ws = Workspace(root=tmp_path)
        # Same skill name in managed and learned; learned (highest priority) wins.
        _write_skill(ws.managed_skills_dir, "shared", desc="managed version")
        _write_skill(ws.learned_skills_dir, "shared", desc="learned version")
        _write_skill(ws.skills_dir, "solo", desc="only in workspace")

        skills = {s["name"]: s for s in iter_agent_skills(cfg, ws)}
        assert set(skills) == {"shared", "solo"}
        assert skills["shared"]["description"] == "learned version"
        # source_dir records where the winning copy came from.
        assert skills["shared"]["source_dir"] == str(ws.learned_skills_dir)
