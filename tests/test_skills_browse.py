"""Unit tests for ``olano.skills.browse`` — the skill viewer's read boundary.

The API tests cover the endpoints end to end; these pin the path arithmetic the
boundary is made of, where a wrong answer is a file the dashboard should not be
able to open (or a shared skill it should not offer to edit).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from olano.config.workspace import Workspace
from olano.skills import browse


def _skill(directory: Path, name: str, body: str = "Body.\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    md = directory / "SKILL.md"
    md.write_text(f"---\nname: {name}\ndescription: d\n---\n\n{body}", encoding="utf-8")
    return md


@pytest.fixture
def ws(tmp_path: Path, lionclaw_home: Path) -> Workspace:
    return Workspace(root=tmp_path / "ws")


def test_learned_beats_workspace_because_the_deepest_root_wins(ws) -> None:
    # learned/ is nested inside skills/, so a shallowest-wins match would label
    # every learned skill "workspace" and mislabel where it came from.
    learned = _skill(ws.learned_skills_dir / "picked-up", "picked-up").parent
    plain = _skill(ws.skills_dir / "hand-written", "hand-written").parent
    roots = browse.browse_roots(None, ws)

    assert browse.classify(learned, roots)[1] == browse.SCOPE_LEARNED
    assert browse.classify(plain, roots)[1] == browse.SCOPE_WORKSPACE


def test_a_configured_dir_that_renames_a_workspace_dir_keeps_the_specific_label(
    ws, base_config
) -> None:
    # config.skills may point at <workspace>/skills itself. Deduping to the last
    # occurrence keeps the loader's ordering, so the row still reads "Agent
    # skill" rather than the vaguer "Configured skill".
    _skill(ws.skills_dir / "shared-name", "shared-name")
    base_config.skills = [str(ws.skills_dir)]
    roots = browse.browse_roots(base_config, ws)

    resolved = [scope for _d, scope in roots]
    assert resolved.count(browse.SCOPE_WORKSPACE) == 1
    assert browse.SCOPE_CONFIGURED not in resolved


def test_a_configured_dir_outside_the_workspace_is_read_only(ws, base_config, tmp_path) -> None:
    outside = tmp_path / "team-skills"
    md = _skill(outside / "playbook", "playbook")
    base_config.skills = [str(outside)]

    ref = browse.resolve_skill(str(md), base_config, ws)
    assert ref.scope == browse.SCOPE_CONFIGURED
    # /api/files cannot write outside the workspace, so the viewer must not
    # offer to — and there is no workspace path to deep-link to either.
    assert ref.workspace_path is None
    assert ref.editable is False


def test_the_managed_pack_is_read_only_even_when_it_is_workspace_reachable(ws) -> None:
    # Editability is a policy, not just reachability: the managed pack is shared
    # by every agent on the box, so an agent-scoped page never offers to edit it
    # — including on a box whose home happens to sit inside a workspace.
    md = _skill(ws.managed_skills_dir / "common", "common")
    ref = browse.resolve_skill(str(md), None, ws)

    assert ref.scope == browse.SCOPE_MANAGED
    assert ref.editable is False
    assert browse.is_editable(browse.SCOPE_MANAGED, "skills/common") is False
    assert browse.is_editable(browse.SCOPE_WORKSPACE, "skills/common") is True


def test_only_a_file_named_skill_md_identifies_a_skill(ws) -> None:
    _skill(ws.skills_dir / "own", "own")
    (ws.skills_dir / "own" / ".env").write_text("TOKEN=shh\n", encoding="utf-8")

    with pytest.raises(browse.SkillAccessError):
        browse.resolve_skill(str(ws.skills_dir / "own" / ".env"), None, ws)
    with pytest.raises(browse.SkillAccessError):
        browse.resolve_skill("", None, ws)


def test_a_skills_root_is_not_itself_a_skill(ws) -> None:
    # <root>/SKILL.md would otherwise walk the whole root and list every skill's
    # files as one bundle.
    _skill(ws.skills_dir / "own", "own")
    with pytest.raises(browse.SkillAccessError):
        browse.resolve_skill(str(ws.skills_dir / "SKILL.md"), None, ws)


def test_paths_outside_every_root_are_refused(ws, tmp_path) -> None:
    md = _skill(tmp_path / "elsewhere" / "sneaky", "sneaky")
    with pytest.raises(browse.SkillAccessError):
        browse.resolve_skill(str(md), None, ws)


def test_a_symlink_inside_a_skill_cannot_read_out_of_it(ws, tmp_path) -> None:
    md = _skill(ws.skills_dir / "own", "own")
    secret = tmp_path / "secret.txt"
    secret.write_text("shh\n", encoding="utf-8")
    (md.parent / "escape.txt").symlink_to(secret)

    ref = browse.resolve_skill(str(md), None, ws)
    with pytest.raises(browse.SkillAccessError):
        browse.read_skill_file(ref, "escape.txt")
    with pytest.raises(browse.SkillAccessError):
        browse.read_skill_file(ref, "../../secret.txt")


def test_listing_caps_a_folder_that_is_secretly_a_checkout(ws, monkeypatch) -> None:
    md = _skill(ws.skills_dir / "big", "big")
    for i in range(12):
        (md.parent / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
    (md.parent / "node_modules" / "pkg").mkdir(parents=True)
    (md.parent / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")

    monkeypatch.setattr(browse, "MAX_ENTRIES", 5)
    ref = browse.resolve_skill(str(md), None, ws)
    listing = browse.list_skill_files(ref)

    assert len(listing["files"]) == 5
    assert listing["truncated"] is True
    # Vendored trees are pruned rather than counted against the cap.
    assert not any(f["path"].startswith("node_modules") for f in listing["files"])


def test_a_long_text_file_previews_truncated_rather_than_whole(ws, monkeypatch) -> None:
    md = _skill(ws.skills_dir / "big", "big")
    (md.parent / "notes.txt").write_text("y" * 200, encoding="utf-8")

    monkeypatch.setattr(browse, "MAX_TEXT_BYTES", 50)
    ref = browse.resolve_skill(str(md), None, ws)
    payload = browse.read_skill_file(ref, "notes.txt")

    assert payload["truncated"] is True
    assert payload["content"] == "y" * 50


def test_a_binary_file_is_described_not_inlined(ws) -> None:
    md = _skill(ws.skills_dir / "own", "own")
    (md.parent / "blob.bin").write_bytes(b"\x00\x01\x02")
    # Text by extension, binary in fact — caught by the NUL scan, not the suffix.
    (md.parent / "trap.txt").write_bytes(b"ok\x00more")

    ref = browse.resolve_skill(str(md), None, ws)
    assert browse.read_skill_file(ref, "blob.bin")["kind"] == "binary"
    assert browse.read_skill_file(ref, "blob.bin")["content"] is None
    assert browse.read_skill_file(ref, "trap.txt")["kind"] == "binary"


def test_annotate_and_resolve_agree_about_a_skill(ws) -> None:
    # The list badges and the viewer header must never disagree, so both route
    # through the same classification + editability rule.
    managed = _skill(ws.managed_skills_dir / "common", "common")
    own = _skill(ws.skills_dir / "own", "own")

    rows = browse.annotate(
        [{"name": "common", "location": str(managed)}, {"name": "own", "location": str(own)}],
        None,
        ws,
    )
    by_name = {r["name"]: r for r in rows}
    for name, location in (("common", managed), ("own", own)):
        ref = browse.resolve_skill(str(location), None, ws)
        assert by_name[name]["scope"] == ref.scope
        assert by_name[name]["editable"] == ref.editable
        assert by_name[name]["workspace_path"] == ref.workspace_path
