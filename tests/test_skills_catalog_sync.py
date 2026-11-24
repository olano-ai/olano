"""The cloud skills-pack catalog (`olano.catalog.skills_registry`) and the
fetchers that now prefer it.

The bug this exists for: every managed box ran `olano skills update` /
`olano archetypes update` at bootstrap and on every upgrade, both defaulting
to `git clone` of a PRIVATE repo, and a managed box holds no GitHub
credential — so both failed on every box, non-fatally and therefore silently.
These pin the replacement: the pack arrives from the cloud with the box's
deployment token, lands in the directory the clone used, overlays the common
skills dir the same way, and git is never attempted on a managed box without
an explicit URL.
"""

from __future__ import annotations

import base64

import pytest

from olano.catalog import skills_registry, store
from olano.skills import sync as skills_sync
from olano.skills.built_in_sync import _SKILLPACK

SKILL_MD = "---\nname: {name}\ndescription: {name} does things.\n---\n# {name}\n"


def _entry(slug: str, *, extra: dict | None = None, digest: str | None = None):
    files = {"SKILL.md": {"encoding": "utf8", "content": SKILL_MD.format(name=slug)}}
    files.update(extra or {})
    return {"slug": slug, "name": slug, "files": files, "contentHash": digest or f"h-{slug}"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def managed(monkeypatch):
    monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "olano_dep_test")


def _stub_fetch(monkeypatch, skills, revision=3):
    monkeypatch.setattr(skills_registry, "_fetch_all", lambda: (skills, revision))


# ── Materializing ───────────────────────────────────────────────────────────


def test_writes_every_file_where_the_git_clone_put_it(home, monkeypatch, managed):
    """Text as utf8, binaries decoded from base64, scripts executable, all
    under <home>/skill-packs/olano-skills/skills/<slug>/ — the clone's layout,
    so nothing downstream knows the transport changed."""
    png = bytes(range(256))
    _stub_fetch(
        monkeypatch,
        [
            _entry(
                "pdf",
                extra={
                    "scripts/run.py": {"encoding": "utf8", "content": "print(1)\n"},
                    "assets/logo.png": {
                        "encoding": "base64",
                        "content": base64.b64encode(png).decode(),
                    },
                },
            )
        ],
    )
    out = skills_registry.refresh_from_cloud()
    assert out and out["count"] == 1 and out["revision"] == 3

    root = skills_registry.pack_skills_dir()
    assert root == home / "skill-packs" / "olano-skills" / "skills"
    assert (root / "pdf" / "SKILL.md").read_text().startswith("---\nname: pdf")
    assert (root / "pdf" / "assets" / "logo.png").read_bytes() == png
    assert (root / "pdf" / "scripts" / "run.py").stat().st_mode & 0o111


def test_overlay_lands_in_the_common_skills_dir_with_the_pack_sentinel(home, monkeypatch, managed):
    """The overlay is the existing one: <home>/skills/<name>/ carrying
    .olano-skillpack, exactly as after a git-fed `olano skills update`."""
    _stub_fetch(monkeypatch, [_entry("pdf"), _entry("xlsx")])
    skills_registry.refresh_from_cloud()
    common = skills_sync.common_skills_dir()
    assert sorted(p.name for p in common.iterdir()) == ["pdf", "xlsx"]
    assert (common / "pdf" / _SKILLPACK).exists()


def test_withdrawn_skills_are_removed_from_pack_and_overlay(home, monkeypatch, managed):
    _stub_fetch(monkeypatch, [_entry("keeper"), _entry("goner")])
    skills_registry.refresh_from_cloud()
    _stub_fetch(monkeypatch, [_entry("keeper")], revision=4)
    skills_registry.refresh_from_cloud()
    assert [p.name for p in skills_registry.pack_skills_dir().iterdir()] == ["keeper"]
    assert [p.name for p in skills_sync.common_skills_dir().iterdir()] == ["keeper"]


def test_unchanged_hash_is_not_rewritten(home, monkeypatch, managed):
    _stub_fetch(monkeypatch, [_entry("pdf", digest="same")])
    skills_registry.refresh_from_cloud()
    marker = skills_registry.pack_skills_dir() / "pdf" / "local-marker"
    marker.write_text("kept if the dir was not rebuilt")
    _stub_fetch(monkeypatch, [_entry("pdf", digest="same")], revision=9)
    assert skills_registry.refresh_from_cloud()["count"] == 1
    assert marker.exists()
    assert store.state_entry("skills")["revision"] == 9


def test_poisoned_rows_never_escape_the_pack_dir(home, monkeypatch, managed):
    """Slugs and paths are validated the way the cloud validates them on
    ingest; a bad row is skipped, never partially written."""
    _stub_fetch(
        monkeypatch,
        [
            _entry("../escape"),
            _entry("ok"),
            {
                "slug": "traversal",
                "files": {
                    "SKILL.md": {"encoding": "utf8", "content": "x"},
                    "../../evil.sh": {"encoding": "utf8", "content": "rm -rf /"},
                },
                "contentHash": "t",
            },
            {"slug": "noskillmd", "files": {"README.md": {"encoding": "utf8", "content": "x"}}},
        ],
    )
    out = skills_registry.refresh_from_cloud()
    assert out["count"] == 1
    assert [p.name for p in skills_registry.pack_skills_dir().iterdir()] == ["ok"]
    assert not (home / "evil.sh").exists() and not (home / "skill-packs" / "evil.sh").exists()


def test_empty_payload_is_refused(home, monkeypatch, managed):
    _stub_fetch(monkeypatch, [_entry("keeper")])
    skills_registry.refresh_from_cloud()
    _stub_fetch(monkeypatch, [])
    assert skills_registry.refresh_from_cloud() is None
    assert [p.name for p in skills_registry.pack_skills_dir().iterdir()] == ["keeper"]


def test_no_deployment_token_means_no_fetch(home, monkeypatch):
    """The endpoint is token-gated; an unmanaged box does not even ask."""
    monkeypatch.delenv("LIONCLAW_DEPLOYMENT_TOKEN", raising=False)
    monkeypatch.setattr("olano.deployment.sources.deployment_token", lambda: None)
    calls = []

    class Boom:
        def __init__(self, *a, **k):
            calls.append(1)

    monkeypatch.setattr("httpx.Client", Boom, raising=False)
    assert skills_registry._fetch_all() is None
    assert calls == []
    assert skills_registry.catalog_status()["token"] is False


# ── The fetchers prefer the cloud on a managed box ──────────────────────────


def test_skills_update_on_a_managed_box_uses_the_cloud_and_never_git(home, monkeypatch, managed):
    _stub_fetch(monkeypatch, [_entry("pdf")])

    def no_git(*a, **k):
        raise AssertionError("git must not run on a managed box without an explicit URL")

    monkeypatch.setattr(skills_sync, "_git_clone_or_update", no_git)
    ok, msg = skills_sync.fetch_olano_skills(home)
    assert ok and "synced from the cloud" in msg
    assert (skills_sync.common_skills_dir(home) / "pdf" / "SKILL.md").exists()
    assert skills_sync.is_pack_installed(home)


def test_skills_update_on_a_managed_box_serves_the_disk_copy_when_the_cloud_is_down(
    home, monkeypatch, managed
):
    _stub_fetch(monkeypatch, [_entry("pdf")])
    skills_sync.fetch_olano_skills(home)
    monkeypatch.setattr(skills_registry, "_fetch_all", lambda: None)
    ok, msg = skills_sync.fetch_olano_skills(home)
    assert ok and "already on disk" in msg


def test_skills_update_on_a_managed_box_with_nothing_on_disk_says_so(home, monkeypatch, managed):
    monkeypatch.setattr(skills_registry, "_fetch_all", lambda: None)
    ok, msg = skills_sync.fetch_olano_skills(home)
    assert ok is False and "no pack on disk" in msg


def test_explicit_url_still_means_git_even_on_a_managed_box(home, monkeypatch, managed):
    seen = []
    monkeypatch.setattr(
        skills_sync,
        "_git_clone_or_update",
        lambda target, url, *, update_if_exists: seen.append(url) or (True, "cloned"),
    )
    skills_sync.fetch_olano_skills(home, url="https://example.invalid/skills.git")
    assert seen == ["https://example.invalid/skills.git"]


def test_archetypes_update_prefers_the_cloud(home, monkeypatch):
    from olano.archetypes import sync as arch_sync
    from olano.catalog import agents_registry

    monkeypatch.setattr(
        agents_registry,
        "refresh_from_cloud",
        lambda: {"count": 202, "revision": 5},
    )
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("git ran"))
    )
    ok, msg = arch_sync.fetch_lionclaw_agents()
    assert ok and "synced from the cloud" in msg and "202" in msg


def test_archetypes_update_on_a_managed_box_never_clones_when_the_cloud_is_down(
    home, monkeypatch, managed
):
    from olano.archetypes import sync as arch_sync
    from olano.catalog import agents_registry

    monkeypatch.setattr(agents_registry, "refresh_from_cloud", lambda: None)
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("git ran"))
    )
    ok, msg = arch_sync.fetch_lionclaw_agents()
    assert ok is False and "no pack on disk" in msg


def test_unmanaged_box_falls_back_to_git_when_the_cloud_is_down(home, monkeypatch):
    """A developer box with a GitHub credential keeps the clone path."""
    from olano.archetypes import sync as arch_sync
    from olano.catalog import agents_registry

    monkeypatch.delenv("LIONCLAW_DEPLOYMENT_TOKEN", raising=False)
    monkeypatch.setattr("olano.deployment.sources.deployment_token", lambda: None)
    monkeypatch.setattr(agents_registry, "refresh_from_cloud", lambda: None)
    seen = []

    class Done:
        returncode = 0
        stdout = stderr = ""

    def fake_run(cmd, **k):
        seen.append(cmd)
        return Done()

    monkeypatch.setattr("subprocess.run", fake_run)
    ok, msg = arch_sync.fetch_lionclaw_agents(quiet=True)
    assert ok and seen and seen[0][:2] == ["git", "clone"]


# ── permissions: every agent lane can read a skill ──────────────────────────
#
# tempfile.mkdtemp makes its directory 0700, the atomic swap renamed it into
# place as-is, and the overlay's copytree carried the mode into <home>/skills.
# Agent Father (in-process, root) read the charts skill fine; a sandboxed
# agent (uid 1000) got permission_denied on the same path and hand-built its
# chart. Skills are read by three identities and must be readable by all.

import stat as _stat


def _mode(p):
    return _stat.S_IMODE(p.stat().st_mode)


def test_a_materialized_skill_is_readable_from_the_sandbox_lane(home, monkeypatch, managed):
    _stub_fetch(
        monkeypatch,
        [
            _entry(
                "charts", extra={"scripts/chart.py": {"encoding": "utf8", "content": "print()\n"}}
            )
        ],
    )
    assert skills_registry.refresh_from_cloud()
    for root in (skills_registry.pack_skills_dir(), skills_sync.common_skills_dir()):
        skill = root / "charts"
        assert _mode(skill) & 0o055 == 0o055, (
            f"{skill}: {oct(_mode(skill))} is not world-traversable"
        )
        assert _mode(skill / "SKILL.md") & 0o044 == 0o044
        assert _mode(skill / "scripts") & 0o055 == 0o055
        assert _mode(skill / "scripts" / "chart.py") & 0o055 == 0o055, "still executable"


def test_an_unchanged_skill_left_0700_by_an_earlier_release_is_repaired(home, monkeypatch, managed):
    """The repair must not depend on content changing: the hash matches, so
    nothing else in the sync would ever revisit the directory."""
    _stub_fetch(monkeypatch, [_entry("charts")])
    assert skills_registry.refresh_from_cloud()
    pack = skills_registry.pack_skills_dir() / "charts"
    common = skills_sync.common_skills_dir() / "charts"
    for d in (pack, common):
        (d / "SKILL.md").chmod(0o600)
        d.chmod(0o700)

    out = skills_registry.refresh_from_cloud()
    assert out and out["count"] == 1
    for d in (pack, common):
        assert _mode(d) == 0o755, f"{d}: {oct(_mode(d))}"
        assert _mode(d / "SKILL.md") == 0o644


def test_startup_upgrade_repairs_common_skills_and_leaves_pinned_ones_alone(home):
    from olano.skills.built_in_sync import _PINNED

    common = skills_sync.common_skills_dir()
    broken = common / "charts"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: charts\n---\n")
    (broken / "SKILL.md").chmod(0o600)
    broken.chmod(0o700)
    mine = common / "mine"
    mine.mkdir()
    (mine / "SKILL.md").write_text("---\nname: mine\n---\n")
    (mine / _PINNED).touch()
    (mine / "SKILL.md").chmod(0o600)
    mine.chmod(0o700)

    assert skills_sync.repair_skill_permissions() == 1
    assert _mode(broken) == 0o755 and _mode(broken / "SKILL.md") == 0o644
    assert _mode(mine) == 0o700 and _mode(mine / "SKILL.md") == 0o600, "pinned is the owner's"
