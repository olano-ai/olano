"""A skill's name and its folder stay in agreement, whoever writes the skill.

Olano's own code writers already comply; the skills that broke the rule on real
boxes were written by a model (an agent following the skill-creator skill, the
Skillsmith, the librarian, a Cortex execution run) putting a title in ``name:``
inside a folder that is its slug. These tests pin each line of defence:

* ``skills.naming``: the one planner every caller asks;
* the security middleware: ``write_file`` judged before it lands, ``edit_file``
  after, a shell command naming ``SKILL.md`` swept;
* the boot sweep and the post-worker sweep, for writes no tool saw;
* ``carry_disabled_skill_renames``, so a switched-off skill stays off.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from olano.skills import naming
from olano.skills.loader import discover_skills

TITLE = "Olano Professional HTML Email Formatting"
SLUG = "olano-professional-html-email-formatting"


def _skill_text(name: str, extra: str = "") -> str:
    return f"---\nname: {name}\ndescription: Formats emails.\n{extra}---\n\n# {TITLE}\n\nBody.\n"


def _write_skill(root: Path, folder: str, name: str, extra: str = "") -> Path:
    skill_dir = root / folder
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_skill_text(name, extra), encoding="utf-8")
    return skill_dir


# ── the planner ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        (TITLE, SLUG),
        ("foo - bar", "foo-bar"),
        ("under_score", "under-score"),
        ("  --Trim--  ", "trim"),
        ("!!!", ""),
        ("a" * 80, "a" * 64),
        ("ab-" * 30, ("ab-" * 22)[:64].rstrip("-")),
    ],
)
def test_skill_slug_is_always_spec_legal_or_empty(text: str, slug: str) -> None:
    assert naming.skill_slug(text) == slug
    if slug:
        assert naming.is_legal_name(slug)


@pytest.mark.parametrize(
    ("name", "folder", "action", "target"),
    [
        ("pdf", "pdf", None, None),
        # A legal name keeps its name: the folder moves.
        ("linkedin-marketing", "linkedin-skills", naming.RENAME_FOLDER, "linkedin-marketing"),
        ("my-skill", "My Skill", naming.RENAME_FOLDER, "my-skill"),
        # The production shape: a title in a slug folder. The name takes the folder.
        (TITLE, SLUG, naming.REWRITE_NAME, SLUG),
        ("Something Else", "pdf-helper", naming.REWRITE_NAME, "pdf-helper"),
        # Neither legal: both become the name's slug.
        ("My Skill", "My Skill", naming.RENAME_BOTH, "my-skill"),
        ("!!!", "My Folder", naming.RENAME_BOTH, "my-folder"),
        ("!!!", "???", None, None),
    ],
)
def test_plan_name_fix(name: str, folder: str, action: str | None, target: str | None) -> None:
    fix = naming.plan_name_fix(name, folder)
    if action is None:
        assert fix is None
        return
    assert fix is not None
    assert (fix.action, fix.target) == (action, target)


def test_with_frontmatter_name_touches_only_the_top_level_line() -> None:
    text = "---\nmetadata:\n  name: nested\nname: Some Title\ndescription: d\n---\n\nname: body\n"
    out = naming.with_frontmatter_name(text, "some-title")
    assert out == text.replace("name: Some Title", "name: some-title")
    assert naming.with_frontmatter_name("no frontmatter\n", "x") is None
    assert naming.with_frontmatter_name("---\ndescription: d\n---\n", "x") is None


def test_frontmatter_name_reads_quoted_values_like_the_loader() -> None:
    assert naming.frontmatter_name('---\nname: "Quoted Title"\n---\n') == "Quoted Title"
    assert naming.frontmatter_name("---\ndescription: d\n---\n") is None
    assert naming.frontmatter_name("# heading only\n") is None


# ── write_file: judged before the write ──────────────────────────────────────


def test_a_title_in_name_is_set_to_the_folder_before_writing(tmp_path: Path) -> None:
    verdict = naming.check_skill_write(tmp_path / SLUG / "SKILL.md", _skill_text(TITLE))
    assert verdict.refusal is None
    assert verdict.content == _skill_text(SLUG)
    assert verdict.note and SLUG in verdict.note and "[skill-name]" in verdict.note


def test_a_legal_name_in_another_folder_is_refused_with_the_path_to_use(tmp_path: Path) -> None:
    verdict = naming.check_skill_write(
        tmp_path / "skills" / "pdf-v2" / "SKILL.md", _skill_text("pdf")
    )
    assert verdict.content is None
    assert verdict.refusal and str(tmp_path / "skills" / "pdf" / "SKILL.md") in verdict.refusal
    assert "name: pdf-v2" in verdict.refusal


def test_rewriting_a_skill_that_already_had_the_mismatch_is_not_refused(tmp_path: Path) -> None:
    """An agent maintaining its own skill must not be blocked over a mismatch it
    did not cause this turn: the write goes through, with the fix named."""
    path = tmp_path / "linkedin-skills" / "SKILL.md"
    verdict = naming.check_skill_write(
        path, _skill_text("linkedin-marketing"), existing_name="linkedin-marketing"
    )
    assert verdict.refusal is None and verdict.content is None
    assert verdict.note and "Move the folder" in verdict.note


def test_a_refusal_never_points_at_a_folder_another_skill_holds(tmp_path: Path) -> None:
    (tmp_path / "pdf").mkdir()
    verdict = naming.check_skill_write(tmp_path / "pdf-v2" / "SKILL.md", _skill_text("pdf"))
    assert verdict.refusal and "already lives" in verdict.refusal
    assert "name: pdf-v2" in verdict.refusal
    assert str(tmp_path / "pdf" / "SKILL.md") not in verdict.refusal


def test_an_illegal_folder_is_refused_with_the_slug_path(tmp_path: Path) -> None:
    verdict = naming.check_skill_write(tmp_path / "My Skill" / "SKILL.md", _skill_text("My Skill"))
    assert verdict.refusal and str(tmp_path / "my-skill" / "SKILL.md") in verdict.refusal


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("pdf/SKILL.md", _skill_text("pdf")),  # compliant
        ("pdf/README.md", _skill_text("Not A Skill")),  # not a SKILL.md
        ("pdf/SKILL.md", "# A skill with no frontmatter\n"),  # loader uses the folder
    ],
)
def test_writes_with_nothing_to_judge_pass_untouched(
    tmp_path: Path, relative: str, content: str
) -> None:
    assert naming.check_skill_write(tmp_path / relative, content) == naming.WriteCheck()


def test_a_learned_fork_is_never_judged(tmp_path: Path) -> None:
    """A fork keeps the original's name to shadow it; its manifest pins paths."""
    fork = tmp_path / "learned" / "pdf-fork"
    fork.mkdir(parents=True)
    (fork / naming.FORK_MANIFEST).write_text("{}", encoding="utf-8")
    assert naming.check_skill_write(fork / "SKILL.md", _skill_text("Pdf")) == naming.WriteCheck()


# ── edit_file and the sweep: checked after the fact ──────────────────────────


def test_an_edit_that_leaves_a_title_in_name_is_aligned_in_place(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, SLUG, TITLE)
    note = naming.check_skill_file(skill_dir / "SKILL.md")
    assert note and SLUG in note
    (found,) = discover_skills(tmp_path)
    assert found["name"] == SLUG and found["spec_error"] is None


def test_an_edit_that_needs_a_folder_move_is_reported_not_moved(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "pdf-v2", "pdf")
    note = naming.check_skill_file(skill_dir / "SKILL.md")
    assert note and "Move the folder" in note
    assert skill_dir.exists()
    assert discover_skills(tmp_path)[0]["name"] == "pdf"


def test_the_sweep_rewrites_only_what_moves_no_path(tmp_path: Path) -> None:
    _write_skill(tmp_path, SLUG, TITLE)
    _write_skill(tmp_path, "linkedin-skills", "linkedin-marketing")  # needs a folder move
    _write_skill(tmp_path, "My Skill", "My Skill")  # needs a folder move
    fork = _write_skill(tmp_path / "learned", "pdf", "Pdf Tools")
    (fork / naming.FORK_MANIFEST).write_text("{}", encoding="utf-8")

    rewritten = naming.align_skill_names([tmp_path, tmp_path / "learned"])

    assert rewritten == [{"path": str(tmp_path / SLUG), "from": TITLE, "to": SLUG}]
    by_folder = {Path(s["location"]).parent.name: s for s in discover_skills(tmp_path)}
    assert by_folder[SLUG]["spec_error"] is None
    assert by_folder["linkedin-skills"]["spec_error"]
    assert by_folder["My Skill"]["spec_error"]
    assert by_folder["pdf"]["name"] == "Pdf Tools"  # the fork is untouched
    assert naming.align_skill_names([tmp_path / "missing"]) == []


def test_the_rewrite_keeps_the_file_and_its_mode(tmp_path: Path) -> None:
    """In place, never a new inode: on a managed box the sandbox user owns it."""
    skill_md = _write_skill(tmp_path, SLUG, TITLE) / "SKILL.md"
    skill_md.chmod(0o640)
    inode = skill_md.stat().st_ino
    assert naming.set_frontmatter_name(skill_md, SLUG) == TITLE
    assert skill_md.stat().st_ino == inode
    assert skill_md.stat().st_mode & 0o777 == 0o640
    assert naming.set_frontmatter_name(skill_md, SLUG) is None  # already aligned


# ── disabled_skills follows the name ─────────────────────────────────────────


def _make_agent(name: str, disabled: list[str]) -> None:
    from olano.config.agent_config import AgentConfig
    from olano.config.loader import save_agent_config

    save_agent_config(
        AgentConfig(name=name, model="fake:instant", system_prompt="x", disabled_skills=disabled)
    )


def test_carry_replaces_the_old_name_and_leaves_the_rest(lionclaw_home: Path) -> None:
    from olano.admin.skills import carry_disabled_skill_renames
    from olano.config.loader import load_agent_config

    _make_agent("maria", ["olano professional html email formatting ", "pdf"])
    _make_agent("nova", ["pdf"])

    changed = carry_disabled_skill_renames({TITLE: SLUG}, ["maria", "nova", "ghost"])

    assert changed == ["maria"]
    assert load_agent_config("maria").disabled_skills == [SLUG, "pdf"]
    assert load_agent_config("nova").disabled_skills == ["pdf"]
    assert carry_disabled_skill_renames({}, ["maria"]) == []


# ── the boot sweep ───────────────────────────────────────────────────────────


def test_boot_aligns_the_name_then_clears_the_alert(
    lionclaw_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact report from the field: the alert raised 13 times, and Repair
    said "Nothing to rename". Now the boot fixes it before the alert is read."""
    import olano.runtime.notifications as notifications
    from olano.config.loader import load_agent_config
    from olano.config.workspace import get_workspace
    from olano.runtime.server import _align_skill_names_at_boot, _report_skill_spec_violations

    raised: list[dict] = []
    resolved: list[str] = []
    monkeypatch.setattr(notifications, "notify", lambda **kw: raised.append(kw) or None)
    monkeypatch.setattr(notifications, "resolve", lambda key: resolved.append(key) or True)

    ws = tmp_path / "ws"
    _write_skill(ws / "skills", SLUG, TITLE)
    _make_agent("zoho-email", [TITLE])
    config = load_agent_config("zoho-email").model_copy(update={"workspace": str(ws)})
    workspace = get_workspace(str(ws))

    assert _align_skill_names_at_boot("zoho-email", config, workspace) is True
    assert load_agent_config("zoho-email").disabled_skills == [SLUG]

    _report_skill_spec_violations("zoho-email", config, workspace)
    assert raised == []
    assert resolved == ["skills_spec_violation:zoho-email"]

    # A second boot has nothing left to do.
    assert _align_skill_names_at_boot("zoho-email", config, workspace) is False


def test_boot_leaves_a_folder_move_for_the_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import olano.runtime.notifications as notifications
    from olano.config.agent_config import AgentConfig
    from olano.config.workspace import get_workspace
    from olano.runtime.server import _align_skill_names_at_boot, _report_skill_spec_violations

    raised: list[dict] = []
    monkeypatch.setattr(notifications, "notify", lambda **kw: raised.append(kw) or None)
    monkeypatch.setattr(notifications, "resolve", lambda key: True)

    ws = tmp_path / "ws"
    _write_skill(ws / "skills", "linkedin-skills", "linkedin-marketing")
    config = AgentConfig(name="maria", model="fake:instant", workspace=str(ws))
    workspace = get_workspace(str(ws))

    assert _align_skill_names_at_boot("maria", config, workspace) is False
    assert (ws / "skills" / "linkedin-skills").exists()
    _report_skill_spec_violations("maria", config, workspace)
    assert len(raised) == 1 and "linkedin-skills/" in raised[0]["body"]


# ── after every cognition worker ─────────────────────────────────────────────


async def test_a_worker_run_is_followed_by_a_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skillsmith, the librarian and execution runs write skills by any route,
    a shell heredoc included. Whatever they left is aligned when they finish."""
    from olano.cognition import worker
    from olano.config.agent_config import AgentConfig

    async def fake_run_worker(**kwargs):
        _write_skill(kwargs["workspace_root"] / "skills" / "learned", SLUG, TITLE)
        return worker.WorkerOutcome(status="completed", summary="wrote a skill")

    monkeypatch.setattr(worker, "_run_worker", fake_run_worker)
    ws = tmp_path / "ws"
    ws.mkdir()
    await worker.run_worker(
        parent_config=AgentConfig(name="maria", model="fake:instant"),
        workspace_root=ws,
        playbook="p",
        goal="g",
        notify_operator=False,
    )
    (found,) = discover_skills(ws / "skills")
    assert found["name"] == SLUG and found["spec_error"] is None


# ── the middleware, end to end ───────────────────────────────────────────────


def _middleware(ws: Path):
    from olano.config.agent_config import AgentConfig
    from olano.security.deepagents_middleware import OlanoSecurityMiddleware

    return OlanoSecurityMiddleware(
        config=AgentConfig(name="zoho-email", system_prompt="x"), workspace_root=ws
    )


def _request(tool: str, args: dict):
    from langgraph.prebuilt.tool_node import ToolCallRequest

    return ToolCallRequest(
        tool_call={"name": tool, "args": args, "id": "c1"},
        tool=None,
        state={},
        runtime=SimpleNamespace(),
    )


async def _fake_file_tool(request):
    """A stand-in for deepagents' write_file / edit_file that really writes."""
    from langchain_core.messages import ToolMessage

    args = request.tool_call["args"]
    path = Path(args["file_path"])
    if request.tool_call["name"] == "write_file":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(args["old_string"], args["new_string"]), encoding="utf-8")
    return ToolMessage(content=f"Updated file {path}", tool_call_id="c1")


async def test_write_file_of_a_titled_skill_lands_aligned(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "skills" / SLUG / "SKILL.md"

    out = await _middleware(ws).awrap_tool_call(
        _request("write_file", {"file_path": str(target), "content": _skill_text(TITLE)}),
        _fake_file_tool,
    )

    assert target.read_text(encoding="utf-8") == _skill_text(SLUG)
    assert "[skill-name]" in str(out.content) and SLUG in str(out.content)
    assert discover_skills(ws / "skills")[0]["spec_error"] is None


async def test_write_file_needing_another_folder_never_lands(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock

    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "skills" / "pdf-v2" / "SKILL.md"
    handler = AsyncMock()

    out = await _middleware(ws).awrap_tool_call(
        _request("write_file", {"file_path": str(target), "content": _skill_text("pdf")}),
        handler,
    )

    handler.assert_not_awaited()
    assert not target.exists()
    assert str(ws / "skills" / "pdf" / "SKILL.md") in str(out.content)


async def test_edit_file_that_puts_a_title_in_name_is_aligned(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write_skill(ws / "skills", SLUG, SLUG)
    target = ws / "skills" / SLUG / "SKILL.md"

    out = await _middleware(ws).awrap_tool_call(
        _request(
            "edit_file",
            {
                "file_path": str(target),
                "old_string": f"name: {SLUG}",
                "new_string": f"name: {TITLE}",
            },
        ),
        _fake_file_tool,
    )

    assert discover_skills(ws / "skills")[0]["name"] == SLUG
    assert "[skill-name]" in str(out.content)


async def test_an_ordinary_write_is_untouched(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "notes.md"
    out = await _middleware(ws).awrap_tool_call(
        _request("write_file", {"file_path": str(target), "content": _skill_text(TITLE)}),
        _fake_file_tool,
    )
    assert target.read_text(encoding="utf-8") == _skill_text(TITLE)
    assert "[skill-name]" not in str(out.content)


# ── self-learning's own slug ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("summarize_emails", "summarize-emails"),
        ("GetCryptoPrice", "get-crypto-price"),
        ("foo - bar", "foo-bar"),  # used to be foo---bar: illegal as name and folder
        (TITLE, SLUG),
    ],
)
def test_self_learning_slugs_are_legal_names(name: str, slug: str) -> None:
    from olano.memory.self_learning import SelfLearningService

    assert SelfLearningService._skill_slug(name) == slug
    assert naming.is_legal_name(slug)


# ── a shell command that names SKILL.md ──────────────────────────────────────


def test_a_shell_write_is_swept_and_the_model_told(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write_skill(ws / "skills", SLUG, TITLE)
    note = _middleware(ws)._align_workspace_skill_names()
    assert note and f"`{TITLE}` -> `{SLUG}`" in note
    assert discover_skills(ws / "skills")[0]["spec_error"] is None
    assert _middleware(ws)._align_workspace_skill_names() is None


# ── the Repair button ────────────────────────────────────────────────────────


def test_repair_endpoint_fixes_the_field_case_and_clears_the_alert(
    lionclaw_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The phone's "Repair Skill Folders" used to answer "Nothing to rename.
    Left alone: ... (the skill's own name is not spec-legal)"."""
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    import olano.runtime.notifications as notifications
    from olano.config.agent_config import AgentConfig
    from olano.config.home import get_olano_paths
    from olano.config.loader import save_agent_config
    from olano.runtime.api import create_api

    resolved: list[str] = []
    monkeypatch.setattr(notifications, "resolve", lambda key: resolved.append(key) or True)
    save_agent_config(AgentConfig(name="zoho-email", model="fake:instant", system_prompt="x"))
    ws = get_olano_paths().agent_workspace_dir("zoho-email")
    _write_skill(ws / "skills", SLUG, TITLE)
    runtime = MagicMock()
    runtime._runners = {}
    client = TestClient(create_api(runtime))

    res = client.post("/api/skills/repair-names", json={"agent": "zoho-email", "apply": True})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 1 and body["skipped"] == []
    assert body["repaired"][0]["new_name"] == SLUG
    assert discover_skills(ws / "skills")[0]["spec_error"] is None
    assert resolved == ["skills_spec_violation:zoho-email"]
