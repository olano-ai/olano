"""Skill name/directory spec compliance — detection, prevention, repair.

The condition this guards: a ``SKILL.md`` whose frontmatter ``name`` differs
from the folder holding it. deepagents logs a warning per offending skill on
**every agent boot** and keeps loading it "for backwards compatibility", so the
only symptom is log noise — and one upstream release away, the agent silently
loses the skill instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from olano.admin import skills as admin_skills
from olano.skills.loader import discover_skills, skill_name_violation

# One table, checked against BOTH our validator and deepagents' own. Each entry
# is (name, directory_name, is_valid).
_CASES: list[tuple[str, str, bool]] = [
    ("pdf", "pdf", True),
    ("social-campaign", "social-campaign", True),
    ("a1", "a1", True),
    ("über-tool", "über-tool", True),  # Unicode lowercase is explicitly allowed
    # The production incident: a real name in a differently-named folder.
    ("linkedin-marketing", "linkedin-skills", False),
    ("x-marketing", "x-skills", False),
    ("facebook-marketing", "facebook-skills", False),
    # Shape violations, which fail regardless of the folder.
    ("", "", False),
    ("Capitals", "Capitals", False),
    ("has space", "has space", False),
    ("-leading", "-leading", False),
    ("trailing-", "trailing-", False),
    ("double--hyphen", "double--hyphen", False),
    ("under_score", "under_score", False),
    ("a" * 65, "a" * 65, False),
]


@pytest.mark.parametrize(("name", "directory", "valid"), _CASES)
def test_skill_name_violation_matches_the_spec(name: str, directory: str, valid: bool) -> None:
    assert (skill_name_violation(name, directory) is None) is valid


@pytest.mark.parametrize(("name", "directory", "valid"), _CASES)
def test_skill_name_violation_agrees_with_deepagents(
    name: str, directory: str, valid: bool
) -> None:
    """Pin our mirror to deepagents' own validator.

    Same anti-drift discipline as ``tests/test_stream_isolation.py`` pinning the
    ``nostream`` tag: if upstream changes the rule (or promotes the warning to a
    hard skip), this must go red rather than skills quietly vanishing from a
    customer's prompt.
    """
    upstream = pytest.importorskip("deepagents.middleware.skills")
    their_valid, their_error = upstream._validate_skill_name(name, directory)
    assert their_valid is valid
    ours = skill_name_violation(name, directory)
    assert (ours is None) is their_valid
    if not their_valid:
        assert ours == their_error


def _write_skill(root: Path, directory: str, name: str) -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A test skill.\n---\n\n# Body\n",
        encoding="utf-8",
    )
    return skill_dir


def test_discover_skills_reports_the_violation_but_still_loads(tmp_path: Path) -> None:
    """Reported, never enforced — deepagents still loads it, so dropping it here
    would take a working skill away from the agent over a naming rule."""
    _write_skill(tmp_path, "linkedin-skills", "linkedin-marketing")
    (found,) = discover_skills(tmp_path)
    assert found["name"] == "linkedin-marketing"  # the frontmatter name still wins
    assert "must match directory name 'linkedin-skills'" in found["spec_error"]


def test_discover_skills_leaves_spec_error_none_when_compliant(tmp_path: Path) -> None:
    _write_skill(tmp_path, "social-campaign", "social-campaign")
    (found,) = discover_skills(tmp_path)
    assert found["spec_error"] is None


# ── prevention: install_skill can no longer mint a mismatch ──────────────────


def test_install_skill_aligns_a_non_slug_name_with_its_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug: the destination folder is named from the SLUG while the copied
    SKILL.md kept the source's frontmatter, so any source whose name was not
    already kebab-case landed permanently non-compliant."""
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(admin_skills, "get_olano_paths", lambda *a, **k: _FakePaths(home))

    source = _write_skill(tmp_path / "src", "whatever", "LinkedIn Marketing")
    result = admin_skills.install_skill(source)

    assert result["name"] == "linkedin-marketing"
    assert result["renamed_from"] == "LinkedIn Marketing"
    (installed,) = discover_skills(home / "skills")
    assert installed["name"] == "linkedin-marketing"
    assert installed["spec_error"] is None


def test_install_skill_leaves_a_compliant_name_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(admin_skills, "get_olano_paths", lambda *a, **k: _FakePaths(home))

    source = _write_skill(tmp_path / "src", "social-campaign", "social-campaign")
    result = admin_skills.install_skill(source)

    assert "renamed_from" not in result
    assert discover_skills(home / "skills")[0]["spec_error"] is None


# ── repair ───────────────────────────────────────────────────────────────────


class _FakePaths:
    """Minimal stand-in for OlanoHomePaths (only what these paths touch)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.vault_dir = root / "vault"

    @property
    def managed_skills_dir(self) -> Path:
        return self.root / "skills"

    def agent_workspace_dir(self, agent: str) -> Path:
        return self.root / "agents" / agent / "workspace"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    (root / "skills").mkdir(parents=True)
    monkeypatch.setattr(admin_skills, "get_olano_paths", lambda *a, **k: _FakePaths(root))
    return root


def test_repair_renames_the_folder_not_the_name(home: Path) -> None:
    """The folder moves to match the name, never the reverse: the name is what
    the prompt and delegate_task refer to, the folder is re-resolved each boot."""
    _write_skill(home / "skills", "linkedin-skills", "linkedin-marketing")

    dry = admin_skills.repair_skill_names()
    assert dry["applied"] is False and dry["count"] == 1
    assert (home / "skills" / "linkedin-skills").exists()  # dry run touched nothing

    applied = admin_skills.repair_skill_names(apply=True)
    assert applied["count"] == 1
    assert not (home / "skills" / "linkedin-skills").exists()
    assert (home / "skills" / "linkedin-marketing" / "SKILL.md").exists()
    assert discover_skills(home / "skills")[0]["spec_error"] is None


def test_repair_is_a_no_op_when_everything_complies(home: Path) -> None:
    _write_skill(home / "skills", "pdf", "pdf")
    report = admin_skills.repair_skill_names(apply=True)
    assert report["count"] == 0
    assert report["skipped"] == []


def test_repair_refuses_a_collision(home: Path) -> None:
    """Renaming would clobber a real skill, so it is reported and left alone."""
    _write_skill(home / "skills", "linkedin-skills", "linkedin-marketing")
    _write_skill(home / "skills", "linkedin-marketing", "linkedin-marketing")

    report = admin_skills.repair_skill_names(apply=True)
    assert report["count"] == 0
    assert len(report["skipped"]) == 1
    assert "already exists" in report["skipped"][0]["skipped"]
    assert (home / "skills" / "linkedin-skills").exists()


def test_repair_sets_an_illegal_name_to_its_folder(home: Path) -> None:
    """The production case, reported for months: a model wrote a title as the
    name into a folder that is its slug. The repair used to refuse it ("the
    skill's own name is not spec-legal") so the alert came back on every boot;
    the folder is the only legal identity there is, so the name takes it."""
    folder = "olano-professional-html-email-formatting"
    _write_skill(home / "skills", folder, "Olano Professional HTML Email Formatting")

    dry = admin_skills.repair_skill_names()
    (planned,) = dry["would_repair"]
    assert planned["action"] == "rewrite_name"
    assert planned["new_name"] == folder
    assert "rename_to" not in planned
    assert discover_skills(home / "skills")[0]["spec_error"]  # dry run touched nothing

    report = admin_skills.repair_skill_names(apply=True)
    assert report["count"] == 1 and report["skipped"] == []
    (found,) = discover_skills(home / "skills")
    assert found["name"] == folder
    assert found["spec_error"] is None
    assert Path(found["location"]).parent.name == folder


def test_repair_renames_both_when_neither_is_legal(home: Path) -> None:
    _write_skill(home / "skills", "My Skill", "My Skill")

    report = admin_skills.repair_skill_names(apply=True)
    (entry,) = report["repaired"]
    assert entry["action"] == "rename_both"
    assert not (home / "skills" / "My Skill").exists()
    (found,) = discover_skills(home / "skills")
    assert found["name"] == "my-skill"
    assert found["spec_error"] is None


def test_repair_of_both_refuses_a_collision(home: Path) -> None:
    _write_skill(home / "skills", "My Skill", "My Skill")
    _write_skill(home / "skills", "my-skill", "my-skill")

    report = admin_skills.repair_skill_names(apply=True)
    assert report["count"] == 0
    assert "already exists" in report["skipped"][0]["skipped"]
    assert (home / "skills" / "My Skill").exists()


def test_repair_carries_a_changed_name_into_disabled_skills(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skill switched off under its old name must stay off: the denylist
    matches by name, and the old name no longer names anything."""
    carried: list[tuple[dict, list]] = []
    monkeypatch.setattr(
        admin_skills,
        "carry_disabled_skill_renames",
        lambda renames, agents: carried.append((renames, agents)) or [],
    )
    monkeypatch.setattr("olano.config.loader.list_agents", lambda *a, **k: ["maria", "nova"])
    _write_skill(home / "skills", "email-formatting", "Email Formatting")
    _write_skill(home / "skills", "linkedin-skills", "linkedin-marketing")  # folder move only

    admin_skills.repair_skill_names(apply=True)
    # A managed skill is shared, so every agent's list is checked; the folder
    # rename kept its name and carries nothing.
    assert carried == [({"Email Formatting": "email-formatting"}, ["maria", "nova"])]


def test_repair_skips_a_learned_fork(home: Path) -> None:
    """A fork's provenance manifest records absolute paths a rename would strand."""
    skill_dir = _write_skill(home / "skills", "forked-copy", "original-skill")
    (skill_dir / ".lc-provenance.json").write_text("{}", encoding="utf-8")

    report = admin_skills.repair_skill_names(apply=True)
    assert report["count"] == 0
    assert "provenance" in report["skipped"][0]["skipped"]
    assert skill_dir.exists()


# ── the boot-time notification ───────────────────────────────────────────────


def _boot_report(agent: str, workspace_root: Path, monkeypatch) -> tuple[list[dict], list[str]]:
    """Drive server._report_skill_spec_violations over a throwaway workspace."""
    import olano.runtime.notifications as notifications
    from olano.runtime.server import _report_skill_spec_violations

    raised: list[dict] = []
    resolved: list[str] = []
    monkeypatch.setattr(notifications, "notify", lambda **kw: raised.append(kw) or None)
    monkeypatch.setattr(notifications, "resolve", lambda key: resolved.append(key) or True)

    from olano.config.agent_config import AgentConfig
    from olano.config.workspace import get_workspace

    config = AgentConfig(name=agent, model="fake:instant", workspace=str(workspace_root))
    _report_skill_spec_violations(agent, config, get_workspace(str(workspace_root)))
    return raised, resolved


def test_a_boot_puts_mismatched_skill_names_on_the_bell(tmp_path: Path, monkeypatch) -> None:
    """deepagents logs one warning per offending skill per boot and nobody reads
    the log — which is exactly how three renamed folders went unnoticed."""
    ws = tmp_path / "ws"
    _write_skill(ws / "skills", "linkedin-skills", "linkedin-marketing")
    _write_skill(ws / "skills", "x-skills", "x-marketing")

    raised, resolved = _boot_report("maria", ws, monkeypatch)

    assert resolved == []
    assert len(raised) == 1  # one row per agent, not per skill
    row = raised[0]
    assert row["kind"] == "skills_spec_violation"
    assert row["agent"] == "maria"
    assert row["dedupe_key"] == "skills_spec_violation:maria"
    assert "2 skill(s)" in row["title"]
    # The body has to name each offender — "some skills are wrong" is not a
    # thing anyone can act on.
    assert "linkedin-skills/" in row["body"]
    assert "x-skills/" in row["body"]


def test_a_clean_workspace_clears_the_row(tmp_path: Path, monkeypatch) -> None:
    """The bell reflects the world, not the history: once the folders are
    renamed the next boot takes the row down."""
    ws = tmp_path / "ws"
    _write_skill(ws / "skills", "linkedin-marketing", "linkedin-marketing")

    raised, resolved = _boot_report("maria", ws, monkeypatch)

    assert raised == []
    assert resolved == ["skills_spec_violation:maria"]
