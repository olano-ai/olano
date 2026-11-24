"""`admin.install_skill` — the supported route for handing a skill to an agent.

Born from a live AgentFather session: it adapted a skill inside a prototype
repo in its own workspace, created a new agent, then hit
``mkdir: cannot create directory '…/agents/subs-mate': Permission denied``
trying to shell-copy the skill across — correctly, since agent shells are
isolated from each other's workspaces. The gap was that no tool performed the
copy runtime-side. This one does: it validates the source, creates every
missing parent (the target may never have booted), and hands ownership to the
sandbox user.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import olano.admin.skills as admin_skills
from olano.admin import install_skill


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("LIONCLAW_HOME", str(home))
    return home


@pytest.fixture
def caller_ws(home, tmp_path) -> Path:
    """The calling agent's workspace, holding an authored skill."""
    ws = home / "agents" / "agentfather" / "workspace"
    skill = ws / "repos" / "olano-prototypes" / "agents" / "subs-mate" / "skills" / "bill-watch"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: bill-watch\ndescription: Track recurring charges\n---\n\nWatch bills.\n",
        encoding="utf-8",
    )
    (skill / "scripts" / "bills.py").write_text("print('bills')\n", encoding="utf-8")
    return ws


def _skill_src(caller_ws: Path) -> Path:
    return (
        caller_ws / "repos" / "olano-prototypes" / "agents" / "subs-mate" / "skills" / "bill-watch"
    )


def _make_agent(home: Path, name: str) -> None:
    agent_dir = home / "agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.json5").write_text(json.dumps({"name": name}), encoding="utf-8")


# ── happy paths ───────────────────────────────────────────────────────────────


def test_install_into_agent_creates_every_missing_parent(home, caller_ws):
    """The exact live scenario: the target exists as config only — no workspace,
    no skills/ dir. The install must mkdir -p the whole chain."""
    _make_agent(home, "subs-mate")
    assert not (home / "agents" / "subs-mate" / "workspace").exists()

    result = install_skill(_skill_src(caller_ws), agent="subs-mate")

    dest = home / "agents" / "subs-mate" / "workspace" / "skills" / "bill-watch"
    assert result["agent"] == "subs-mate"
    assert result["path"] == str(dest)
    assert (dest / "SKILL.md").is_file()
    assert (dest / "scripts" / "bills.py").is_file()
    assert "restart" in result["note"]


def test_install_copies_the_entire_tree_not_just_skill_md_and_scripts(home, caller_ws):
    """A skill is whatever the author put in the folder — reference docs,
    formulas, data, templates, nested dirs. Everything ships, byte-identical,
    with the layout preserved."""
    src = _skill_src(caller_ws)
    (src / "references").mkdir()
    (src / "references" / "PRICING.md").write_text("# pricing tables\n", encoding="utf-8")
    (src / "references" / "formulas.py").write_text("RATE = 0.21\n", encoding="utf-8")
    (src / "data" / "samples").mkdir(parents=True)
    (src / "data" / "samples" / "ledger.csv").write_text("date,amount\n", encoding="utf-8")
    (src / "NOTES.md").write_text("author notes\n", encoding="utf-8")
    (src / "template.html").write_text("<html/>", encoding="utf-8")
    _make_agent(home, "subs-mate")

    result = install_skill(_skill_src(caller_ws), agent="subs-mate")

    dest = home / "agents" / "subs-mate" / "workspace" / "skills" / "bill-watch"
    for rel in (
        "SKILL.md",
        "scripts/bills.py",
        "references/PRICING.md",
        "references/formulas.py",
        "data/samples/ledger.csv",
        "NOTES.md",
        "template.html",
    ):
        assert (dest / rel).is_file(), f"missing {rel}"
        assert (dest / rel).read_bytes() == (src / rel).read_bytes(), f"content differs: {rel}"
    assert result["files"] == 7


def test_install_managed_reaches_all_agents(home, caller_ws):
    result = install_skill(_skill_src(caller_ws), agent=None)

    dest = home / "skills" / "bill-watch"
    assert result["agent"] is None
    assert (dest / "SKILL.md").is_file()
    assert (dest / "scripts" / "bills.py").is_file()


def test_frontmatter_name_wins_over_directory_name(home, caller_ws):
    src = _skill_src(caller_ws)
    renamed = src.parent / "some-checkout-dir"
    src.rename(renamed)

    result = install_skill(renamed)

    assert result["name"] == "bill-watch"
    assert (home / "skills" / "bill-watch" / "SKILL.md").is_file()


def test_overwrite_refused_then_replaces(home, caller_ws):
    src = _skill_src(caller_ws)
    install_skill(src)

    with pytest.raises(FileExistsError, match="overwrite=True"):
        install_skill(src)

    (src / "scripts" / "extra.py").write_text("x\n", encoding="utf-8")
    result = install_skill(src, overwrite=True)
    assert result["overwritten"] is True
    assert (home / "skills" / "bill-watch" / "scripts" / "extra.py").is_file()


def test_ownership_is_handed_to_the_sandbox_user(home, caller_ws, monkeypatch):
    """Workspace installs run as root on managed boxes; the target agent is uid
    1000 and must own its own workspace files (same rule the sandbox mounts
    enforce everywhere else)."""
    import olano.core.builder as builder

    chowned: list[str] = []
    monkeypatch.setattr(builder, "chown_for_sandbox", lambda p: chowned.append(str(p)) or True)
    _make_agent(home, "subs-mate")

    install_skill(_skill_src(caller_ws), agent="subs-mate")

    dest = home / "agents" / "subs-mate" / "workspace" / "skills" / "bill-watch"
    assert str(dest) in chowned
    assert str(dest / "SKILL.md") in chowned
    assert str(dest / "scripts" / "bills.py") in chowned
    assert str(dest.parent) in chowned  # the freshly created skills/ dir
    assert str(dest.parent.parent) in chowned  # and the workspace itself


def test_managed_install_does_not_chown(home, caller_ws, monkeypatch):
    """<home>/skills is read-only in sandboxes; nothing to hand over."""
    import olano.core.builder as builder

    chowned: list[str] = []
    monkeypatch.setattr(builder, "chown_for_sandbox", lambda p: chowned.append(str(p)) or True)

    install_skill(_skill_src(caller_ws))

    assert chowned == []


# ── refusals ──────────────────────────────────────────────────────────────────


def test_unknown_target_agent_is_a_clear_error(home, caller_ws):
    with pytest.raises(ValueError, match="Create the agent first"):
        install_skill(_skill_src(caller_ws), agent="ghost")


def test_source_without_skill_md_is_rejected(home, caller_ws):
    src = _skill_src(caller_ws).parent  # the skills/ parent, not the skill
    with pytest.raises(FileNotFoundError, match="no SKILL.md"):
        install_skill(src)


def test_frontmatterless_skill_falls_back_to_the_directory_name(home, caller_ws):
    """The skills loader is deliberately lenient — a SKILL.md without
    frontmatter is named after its directory. Install mirrors the loader: what
    the loader would accept at agent build time is installable."""
    src = _skill_src(caller_ws)
    (src / "SKILL.md").write_text("just prose\n", encoding="utf-8")

    result = install_skill(src)

    assert result["name"] == "bill-watch"  # the directory name
    assert (home / "skills" / "bill-watch" / "SKILL.md").is_file()


def test_symlink_in_source_tree_is_rejected(home, caller_ws, tmp_path):
    """A symlink would let a uid-1000 author launder root-readable content
    (vault, other configs) through the runtime-side copy."""
    src = _skill_src(caller_ws)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (src / "scripts" / "link.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        install_skill(src)


def test_source_outside_allowed_roots_is_rejected(home, caller_ws, tmp_path):
    elsewhere = tmp_path / "elsewhere" / "skill"
    elsewhere.mkdir(parents=True)
    (elsewhere / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")

    with pytest.raises(PermissionError, match="outside your workspace"):
        install_skill(elsewhere, allowed_source_roots=[caller_ws])

    # The same source passes when its root is granted.
    result = install_skill(elsewhere, allowed_source_roots=[tmp_path / "elsewhere"])
    assert result["name"] == "x"


def test_vault_is_denied_even_when_roots_would_allow_it(home):
    vault_skill = home / "vault" / "skill"
    vault_skill.mkdir(parents=True)
    (vault_skill / "SKILL.md").write_text("---\nname: v\n---\n", encoding="utf-8")

    with pytest.raises(PermissionError, match="may not live under"):
        install_skill(vault_skill, allowed_source_roots=[home / "vault"])


def test_oversized_source_is_rejected(home, caller_ws, monkeypatch):
    monkeypatch.setattr(admin_skills, "_INSTALL_MAX_FILES", 2)
    src = _skill_src(caller_ws)
    (src / "a.txt").write_text("x", encoding="utf-8")
    (src / "b.txt").write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="not a skill directory"):
        install_skill(src)


def test_failed_rediscovery_rolls_back(home, caller_ws, monkeypatch):
    calls = {"n": 0}
    real = admin_skills.discover_skills

    def flaky(path):
        calls["n"] += 1
        return real(path) if calls["n"] == 1 else []

    monkeypatch.setattr(admin_skills, "discover_skills", flaky)

    with pytest.raises(ValueError, match="rolled back"):
        install_skill(_skill_src(caller_ws))
    assert not (home / "skills" / "bill-watch").exists()


# ── the tool wrapper scopes sources to the caller ─────────────────────────────


def test_tool_wrapper_roots_are_caller_workspace_then_shared(home, caller_ws, monkeypatch):
    from olano.tools.olano_admin import _caller_source_roots
    from olano.tools.talk_to import current_agent

    _make_agent(home, "agentfather")
    token = current_agent.set("agentfather")
    try:
        roots = _caller_source_roots()
    finally:
        current_agent.reset(token)

    assert roots[0] == caller_ws
    assert roots[-1] == home / "shared"


def test_tool_wrapper_without_caller_falls_back_to_shared_only(home, monkeypatch):
    from olano.tools.olano_admin import _caller_source_roots
    from olano.tools.talk_to import current_agent

    token = current_agent.set("")
    try:
        roots = _caller_source_roots()
    finally:
        current_agent.reset(token)

    assert roots == [home / "shared"]


def test_hitl_gates_the_install_tool():
    from olano.security.hitl_policy import CATEGORY_TOOLS

    assert "olano_install_skill" in CATEGORY_TOOLS["write_external"]
