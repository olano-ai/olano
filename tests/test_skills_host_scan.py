"""deepagents' SkillsMiddleware scans skill sources on the host for a sandboxed agent.

The sandbox scan is one ``docker exec`` per SKILL.md on the first turn of every
thread (``skills_metadata`` is loaded once per thread); the host read lists the
same mounted directories and advertises the same absolute paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from olano.core import builder


class _NeverReadSandbox:
    """Stands in for the sandbox's file primitives: any read is a test failure."""

    def ls(self, path: str):  # pragma: no cover - the assertion is that it is never called
        raise AssertionError(f"sandbox ls({path!r}) called during the skills scan")

    def download_files(self, paths):  # pragma: no cover - same
        raise AssertionError(f"sandbox download_files({paths!r}) called during the skills scan")


def _docker_backend_stub():
    from olano.backends.lionclaw_docker import OlanoDockerBackend

    stub = OlanoDockerBackend.__new__(OlanoDockerBackend)
    sandbox = _NeverReadSandbox()
    stub.ls = sandbox.ls  # type: ignore[method-assign]
    stub.download_files = sandbox.download_files  # type: ignore[method-assign]
    return stub


def _write_skill(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A {name} skill for the host-scan test\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return d


def test_docker_sandbox_scans_on_the_host_filesystem() -> None:
    from deepagents.backends import FilesystemBackend

    scan = builder._skills_scan_backend(_docker_backend_stub())
    assert isinstance(scan, FilesystemBackend)
    assert scan.virtual_mode is False


def test_other_backends_keep_their_own_scan() -> None:
    from deepagents.backends import FilesystemBackend, StateBackend

    fs = FilesystemBackend(root_dir="/", virtual_mode=False)
    assert builder._skills_scan_backend(fs) is fs
    state = StateBackend()
    assert builder._skills_scan_backend(state) is state


def test_env_opt_out_keeps_the_sandbox_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(builder._SKILLS_HOST_SCAN_ENV, "0")
    stub = _docker_backend_stub()
    assert builder._skills_scan_backend(stub) is stub


def test_patched_graph_class_loads_skills_without_touching_the_sandbox(tmp_path: Path) -> None:
    """The class bound in ``deepagents.graph`` (what ``create_deep_agent``
    instantiates, for the main agent and every subagent) reads the host and
    keeps the name deepagents matches the skills slot by."""
    builder._ensure_skills_host_scan_patch()
    import deepagents.graph as graph
    from deepagents.middleware.skills import SkillsMiddleware as base

    assert getattr(graph.SkillsMiddleware, "_olano_host_scan", False)
    assert issubclass(graph.SkillsMiddleware, base)

    source = tmp_path / "skills"
    alpha = _write_skill(source, "alpha")
    _write_skill(source, "beta")
    mw = graph.SkillsMiddleware(backend=_docker_backend_stub(), sources=[str(source)])
    assert mw.name == "SkillsMiddleware"
    assert isinstance(mw, base)

    update = mw.before_agent({}, None, {})  # type: ignore[arg-type]
    assert update is not None
    loaded = {s["name"]: s for s in update["skills_metadata"]}
    assert set(loaded) == {"alpha", "beta"}
    # The advertised path is the host path, which the sandbox mounts identically.
    assert Path(loaded["alpha"]["path"]).resolve() == (alpha / "SKILL.md").resolve()
    assert update["skills_load_errors"] == []


def test_patch_is_wired_into_graph_creation() -> None:
    import inspect

    src = inspect.getsource(builder._create_agent_graph)
    assert "_ensure_skills_host_scan_patch()" in src


# ── read once per process, not once per thread ─────────────────────────────


def _counting(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count deepagents' per-source reads behind the patched class."""
    import deepagents.middleware.skills as skills_mw

    calls = {"n": 0}
    orig_sync, orig_async = skills_mw._list_skills_with_errors, skills_mw._alist_skills_with_errors

    def sync_wrapper(backend, source):
        calls["n"] += 1
        return orig_sync(backend, source)

    async def async_wrapper(backend, source):
        calls["n"] += 1
        return await orig_async(backend, source)

    monkeypatch.setattr(skills_mw, "_list_skills_with_errors", sync_wrapper)
    monkeypatch.setattr(skills_mw, "_alist_skills_with_errors", async_wrapper)
    return calls


def test_skill_list_is_read_once_and_reused_by_later_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    builder._ensure_skills_host_scan_patch()
    builder._reset_skills_list_cache()
    import deepagents.graph as graph

    calls = _counting(monkeypatch)
    source = tmp_path / "skills"
    _write_skill(source, "alpha")
    beta = _write_skill(source, "beta")

    first = graph.SkillsMiddleware(backend=_docker_backend_stub(), sources=[str(source)])
    update = first.before_agent({}, None, {})  # type: ignore[arg-type]
    assert {s["name"] for s in update["skills_metadata"]} == {"alpha", "beta"}
    assert calls["n"] == 1

    # A second thread (a fresh middleware instance, same sources) reads nothing.
    second = graph.SkillsMiddleware(backend=_docker_backend_stub(), sources=[str(source)])
    again = asyncio.run(second.abefore_agent({}, None, {}))  # type: ignore[arg-type]
    assert {s["name"] for s in again["skills_metadata"]} == {"alpha", "beta"}
    assert calls["n"] == 1
    # ...and gets its own list object, so a downstream edit cannot reach the cache.
    again["skills_metadata"].clear()
    assert len(first.before_agent({}, None, {})["skills_metadata"]) == 2  # type: ignore[arg-type,index]
    assert calls["n"] == 1

    # A thread that already holds the list is left alone, as upstream.
    assert second.before_agent({"skills_metadata": []}, None, {}) is None  # type: ignore[arg-type]

    # Editing a SKILL.md (new content, later mtime) makes the next thread read again.
    md = beta / "SKILL.md"
    md.write_text(md.read_text().replace("A beta skill", "A better beta skill"), encoding="utf-8")
    st = md.stat()
    import os

    os.utime(md, ns=(st.st_atime_ns + 2_000_000_000, st.st_mtime_ns + 2_000_000_000))
    third = graph.SkillsMiddleware(backend=_docker_backend_stub(), sources=[str(source)])
    refreshed = third.before_agent({}, None, {})  # type: ignore[arg-type]
    assert calls["n"] == 2
    assert {s["name"]: s["description"] for s in refreshed["skills_metadata"]}["beta"].startswith(
        "A better"
    )

    # An installed skill is a new directory: read again, listed next thread.
    _write_skill(source, "gamma")
    fourth = graph.SkillsMiddleware(backend=_docker_backend_stub(), sources=[str(source)])
    assert "gamma" in {s["name"] for s in fourth.before_agent({}, None, {})["skills_metadata"]}  # type: ignore[arg-type,index]
    assert calls["n"] == 3


def test_skill_list_cache_can_be_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder._ensure_skills_host_scan_patch()
    builder._reset_skills_list_cache()
    monkeypatch.setenv(builder._SKILLS_LIST_CACHE_ENV, "0")
    import deepagents.graph as graph

    calls = _counting(monkeypatch)
    source = tmp_path / "skills"
    _write_skill(source, "alpha")
    for _ in range(2):
        mw = graph.SkillsMiddleware(backend=_docker_backend_stub(), sources=[str(source)])
        assert mw.before_agent({}, None, {}) is not None  # type: ignore[arg-type]
    assert calls["n"] == 2


def test_virtual_backend_skill_lists_are_never_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A StateBackend holds its skills in memory, not on the host disk: the
    fingerprint cannot see them, so nothing is cached for it."""
    from deepagents.backends import StateBackend

    builder._ensure_skills_host_scan_patch()
    import deepagents.graph as graph

    mw = graph.SkillsMiddleware(backend=StateBackend(), sources=["/skills"])
    assert builder._skills_sources_fingerprint(mw) is None
