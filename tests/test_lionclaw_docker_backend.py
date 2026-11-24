"""OlanoDockerBackend conforms to the deepagents 0.7 backend protocol.

Regression cover for the production break where every chat turn died in
``SkillsMiddleware.abefore_agent`` with::

    AttributeError: 'OlanoDockerBackend' object has no attribute 'als'

The backend implemented a stale 0.5-era surface (``ls_info``/``glob_info``/
``grep_raw``, a ``read`` returning ``str``, an ``execute`` returning a dict) and
subclassed nothing, so deepagents found none of the protocol it calls.

Two layers here:

* **conformance** — the backend IS a ``SandboxBackendProtocol`` (which is what
  gates registration of the ``execute`` tool) and answers every sync/async
  method deepagents dispatches on;
* **behaviour** — the real deepagents command templates are run end-to-end
  against a real filesystem by swapping only the ``docker exec`` prefix for a
  local shell. No Docker daemon needed, but the JSON contract between
  deepagents' server-side scripts and its parsers is genuinely exercised.
"""

from __future__ import annotations

import subprocess

import pytest
from deepagents.backends.protocol import (
    BackendProtocol,
    ExecuteResponse,
    SandboxBackendProtocol,
)
from deepagents.backends.sandbox import BaseSandbox

import olano.backends.lionclaw_docker as lionclaw_docker
from olano.backends.lionclaw_docker import OlanoDockerBackend, SandboxRun
from olano.config.backend_config import OlanoDockerBackendConfig

# Everything deepagents may call on a sandbox backend. `als` is the one the
# production traceback named; the rest fail the same way once a turn gets past it.
_PROTOCOL_METHODS = (
    "ls",
    "als",
    "read",
    "aread",
    "write",
    "awrite",
    "edit",
    "aedit",
    "glob",
    "aglob",
    "grep",
    "agrep",
    "delete",
    "adelete",
    "upload_files",
    "aupload_files",
    "download_files",
    "adownload_files",
    "execute",
    "aexecute",
)


@pytest.fixture
def backend(monkeypatch, tmp_path):
    """A backend whose `docker exec` is rewired to run locally in `sh -c`.

    Only the container hop is faked: the command string handed to the shell is
    the one `_exec` really builds (tool-layer env prefix and all), running the
    real deepagents templates against `tmp_path`.
    """
    b = OlanoDockerBackend(OlanoDockerBackendConfig(persistent=True))
    b._container_name = "olano-test-container"
    b._workspace_root = tmp_path

    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        assert argv[:2] == ["docker", "exec"], argv[:2]
        # Non-login shell: a login shell's profile output would prepend itself to
        # the JSON that deepagents' file-operation templates print.
        assert argv[-3:-1] == ["sh", "-c"], argv[-3:-1]
        return real_run(["sh", "-c", argv[-1]], **kwargs)

    monkeypatch.setattr(lionclaw_docker.subprocess, "run", fake_run)
    return b


# ── protocol conformance ──────────────────────────────────────────────────────


def test_backend_is_a_deepagents_sandbox_backend():
    """`isinstance(backend, SandboxBackendProtocol)` is what registers the
    `execute` tool in deepagents' filesystem middleware — a bare class gets no
    shell at all."""
    b = OlanoDockerBackend(OlanoDockerBackendConfig())
    assert isinstance(b, SandboxBackendProtocol)
    assert isinstance(b, BackendProtocol)
    assert isinstance(b, BaseSandbox)


@pytest.mark.parametrize("method", _PROTOCOL_METHODS)
def test_protocol_method_is_present(method):
    b = OlanoDockerBackend(OlanoDockerBackendConfig())
    assert callable(getattr(b, method, None)), f"backend is missing {method}()"


def test_id_property_tracks_the_container():
    b = OlanoDockerBackend(OlanoDockerBackendConfig())
    assert b.id  # never blank — deepagents uses it for logging/keys
    b._container_name = "olano-alpha-0123456789"
    assert b.id == "olano-alpha-0123456789"
    assert b.container_ref() == "olano-alpha-0123456789"


async def test_als_returns_an_ls_result(backend, tmp_path):
    """The exact call from SkillsMiddleware.abefore_agent that used to crash."""
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")

    result = await backend.als(str(tmp_path / "skills"))

    assert result.error is None
    assert [entry["path"] for entry in result.entries] == [str(tmp_path / "skills" / "SKILL.md")]


async def test_als_on_a_missing_path_errors_without_raising(backend, tmp_path):
    result = await backend.als(str(tmp_path / "nope"))
    assert result.entries is None
    assert "path_not_found" in (result.error or "")


async def test_deepagents_skill_discovery_runs_against_the_backend(backend, tmp_path):
    """End-to-end through deepagents' own skills scanner — the failing frame.

    Skipped rather than failed if deepagents renames the helper: the contract
    that matters (`als` returning an `LsResult`) is pinned above regardless.
    """
    skills = pytest.importorskip("deepagents.middleware.skills")
    alist = getattr(skills, "_alist_skills_with_errors", None)
    if alist is None:
        pytest.skip("deepagents renamed its skill-listing helper")

    demo = tmp_path / "skills" / "demo"
    demo.mkdir(parents=True)
    (demo / "SKILL.md").write_text(
        "---\nname: demo\ndescription: A demo skill\n---\n\nBody\n", encoding="utf-8"
    )

    found, error = await alist(backend, str(tmp_path / "skills"))

    assert error is None
    assert [s["name"] for s in found] == ["demo"]


# ── file operations (real deepagents templates) ───────────────────────────────


def test_write_then_read_roundtrip(backend, tmp_path):
    target = str(tmp_path / "nested" / "note.txt")

    written = backend.write(target, "alpha\nbeta\ngamma\n")
    assert written.error is None
    assert written.path == target

    read = backend.read(target)
    assert read.error is None
    # deepagents' read template drops the final newline of the window.
    assert read.file_data["content"] == "alpha\nbeta\ngamma"
    assert read.total_lines == 3


def test_read_honours_offset_and_limit(backend, tmp_path):
    target = tmp_path / "many.txt"
    target.write_text("".join(f"line{i}\n" for i in range(10)), encoding="utf-8")

    read = backend.read(str(target), offset=3, limit=2)

    assert read.error is None
    assert read.file_data["content"] == "line3\nline4"
    assert (read.start_line, read.end_line, read.next_offset) == (4, 5, 5)


def test_read_missing_file_is_an_error_result(backend, tmp_path):
    read = backend.read(str(tmp_path / "ghost.txt"))
    assert read.file_data is None
    assert "file_not_found" in (read.error or "")


def test_edit_replaces_and_counts(backend, tmp_path):
    target = tmp_path / "cfg.py"
    target.write_text("DEBUG = True\nOTHER = 1\n", encoding="utf-8")

    edited = backend.edit(str(target), "DEBUG = True", "DEBUG = False")

    assert edited.error is None
    assert edited.occurrences == 1
    assert target.read_text(encoding="utf-8") == "DEBUG = False\nOTHER = 1\n"


def test_edit_refuses_an_ambiguous_match(backend, tmp_path):
    target = tmp_path / "dup.txt"
    target.write_text("x\nx\n", encoding="utf-8")

    edited = backend.edit(str(target), "x", "y")

    assert edited.occurrences is None
    assert "appears multiple times" in (edited.error or "")
    assert target.read_text(encoding="utf-8") == "x\nx\n"  # untouched


def test_ls_reports_files_and_dirs(backend, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "top.txt").write_text("hi", encoding="utf-8")

    result = backend.ls(str(tmp_path))

    assert result.error is None
    by_path = {entry["path"]: entry["is_dir"] for entry in result.entries}
    assert by_path[str(tmp_path / "sub")] is True
    assert by_path[str(tmp_path / "top.txt")] is False


def test_glob_matches_recursively(backend, tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "notes.md").write_text("", encoding="utf-8")

    result = backend.glob("**/*.py", str(tmp_path))

    assert result.error is None
    # Matches come back ABSOLUTE, like `ls` above — not relative to the search
    # root, which is what this asserted until deepagents 0.7.11. Upstream rooted
    # them deliberately: `glob`'s tool contract is absolute paths, and
    # `_check_fs_permission` matches absolute patterns only, so a relative match
    # slipped past every `deny` rule. That bypass was never reachable here —
    # `builder._build_permissions` returns None for sandbox backends, which
    # isolate by mount shape instead — so for Olano this is purely a contract
    # fix, and it is the contract we want: the model only ever sees host paths
    # (CLAUDE.md, "One path namespace"), which is why `ls` above is absolute too.
    assert [m["path"] for m in result.matches] == [str(tmp_path / "pkg" / "mod.py")]


def test_grep_finds_literal_matches(backend, tmp_path):
    (tmp_path / "a.txt").write_text("nothing\nNEEDLE here\n", encoding="utf-8")

    result = backend.grep("NEEDLE", str(tmp_path))

    assert result.error is None
    assert len(result.matches) == 1
    assert result.matches[0]["line"] == 2
    assert "NEEDLE here" == result.matches[0]["text"]


def test_delete_removes_the_path(backend, tmp_path):
    target = tmp_path / "gone.txt"
    target.write_text("bye", encoding="utf-8")

    result = backend.delete(str(target))

    assert result.error is None
    assert not target.exists()


def test_upload_download_roundtrip_handles_binary(backend, tmp_path):
    target = str(tmp_path / "bin" / "blob.dat")
    payload = bytes(range(256))

    (upload,) = backend.upload_files([(target, payload)])
    assert upload.error is None
    assert upload.path == target

    (download,) = backend.download_files([target])
    assert download.error is None
    assert download.content == payload


def test_download_maps_known_failures_to_error_codes(backend, tmp_path):
    (tmp_path / "adir").mkdir()

    missing, is_dir = backend.download_files([str(tmp_path / "nope"), str(tmp_path / "adir")])

    assert missing.error == "file_not_found"
    assert missing.content is None
    assert is_dir.error == "is_directory"


# ── execute() ─────────────────────────────────────────────────────────────────


def test_execute_returns_an_execute_response(backend):
    result = backend.execute("echo hello")

    assert isinstance(result, ExecuteResponse)
    assert result.output.strip() == "hello"
    assert result.exit_code == 0


def test_execute_combines_stderr_and_reports_the_exit_code(backend):
    result = backend.execute("echo out; echo err >&2; exit 3")

    assert "out" in result.output
    assert "err" in result.output
    assert result.exit_code == 3


def test_execute_applies_the_tool_layer_env_and_path(backend):
    """Shared pip/npm installs must land in /opt/agent-tools for every command."""
    result = backend.execute("printenv PYTHONUSERBASE; printenv PIP_USER")

    assert "/opt/agent-tools/python" in result.output
    assert "1" in result.output


def test_run_keeps_stdout_and_stderr_apart(backend):
    """Olano's own callers (shell summariser, sandboxed_run) need them split."""
    result = backend.run("echo out; echo err >&2; exit 4")

    assert isinstance(result, SandboxRun)
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert result.returncode == 4


async def test_aexecute_forwards_the_timeout(backend):
    result = await backend.aexecute("echo async-ok", timeout=30)
    assert result.output.strip() == "async-ok"
    assert result.exit_code == 0


def test_execute_timeout_reports_exit_124_instead_of_raising(backend):
    result = backend.execute("sleep 5", timeout=1)

    assert result.exit_code == 124
    assert "timed out" in result.output.lower()


def test_execute_on_an_unstarted_backend_is_an_error_not_a_crash():
    """A backend whose container never came up must degrade to an error result —
    deepagents calls file ops from inside the graph, where a raise kills the turn."""
    b = OlanoDockerBackend(OlanoDockerBackendConfig())

    result = b.execute("echo hi")

    assert result.exit_code == 127
    assert "start()" in result.output
    # Inherited file ops degrade to an empty/error result rather than raising.
    assert b.ls("/workspace").entries == []
    assert b.read("/workspace/x").error
