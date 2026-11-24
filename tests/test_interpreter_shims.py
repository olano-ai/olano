"""The runtime-owned `python`/`pip` shims (olano/backends/interpreter_shims.py).

Pins the property that makes them "once and for all": a bare `python` resolves
on every agent shell lane WITHOUT depending on an image alias — the alias was in
the sandbox image and still failed in production on a stale image/container.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from olano.backends import interpreter_shims as shims


def test_shims_are_written_executable_and_exec_python3(tmp_path):
    shim_dir = shims.ensure_interpreter_shims(tmp_path)

    assert shim_dir == tmp_path / "shims"
    for name in ("python", "pip"):
        path = shim_dir / name
        text = path.read_text()
        assert text.startswith("#!/bin/sh\n")
        assert "exec python3" in text
        # A shim must never name itself — that would loop, not fail loudly.
        assert f"exec {name} " not in text
        assert stat.S_IMODE(path.stat().st_mode) == 0o755, name
    rc = shim_dir / shims.TERMINAL_RC_NAME
    assert rc.exists()


def test_rewrite_is_idempotent(tmp_path):
    shims.ensure_interpreter_shims(tmp_path)
    before = {p.name: p.stat().st_mtime_ns for p in (tmp_path / "shims").iterdir()}

    shims.ensure_interpreter_shims(tmp_path)

    after = {p.name: p.stat().st_mtime_ns for p in (tmp_path / "shims").iterdir()}
    assert after == before  # unchanged content ⇒ no write (called per shell spawn)
    assert not [p for p in (tmp_path / "shims").iterdir() if p.name.startswith(".")]


def test_a_tampered_shim_is_restored(tmp_path):
    shim_dir = shims.ensure_interpreter_shims(tmp_path)
    (shim_dir / "python").write_text("#!/bin/sh\nexec rm -rf /\n")
    (shim_dir / "python").chmod(0o644)

    shims.ensure_interpreter_shims(tmp_path)

    text = (shim_dir / "python").read_text()
    assert "rm -rf" not in text
    assert stat.S_IMODE((shim_dir / "python").stat().st_mode) == 0o755


def test_unwritable_dir_degrades_to_none(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(Path, "mkdir", boom)
    assert shims.ensure_interpreter_shims(tmp_path / "x") is None


def test_shell_path_export_puts_tool_layer_first_and_shims_last():
    line = shims.shell_path_export()

    assert line.startswith(f"export PATH={shims.CONTAINER_TOOL_LAYER_PATH}:$PATH:")
    assert line.endswith(f":$PATH:{shims.CONTAINER_SHIM_PATH}")
    assert shims.CONTAINER_SHIM_PATH == "/opt/agent-tools/shims"


def test_terminal_rc_sources_profile_first_then_applies_the_exec_lane_env():
    rc = shims.render_terminal_rc().splitlines()
    body = [ln for ln in rc if ln and not ln.startswith("#")]

    # /etc/profile resets PATH on Debian — it must run BEFORE the exports.
    assert body[0] == "[ -r /etc/profile ] && . /etc/profile"
    assert body[-1] == shims.shell_path_export()
    for key, val in shims.CONTAINER_TOOL_LAYER_ENV.items():
        assert f"export {key}={val}" in body


def test_with_interpreter_shims_appends_once_at_the_end(lionclaw_home):
    env = shims.with_interpreter_shims({"PATH": "/usr/bin:/bin"})
    shim = str(lionclaw_home / "sandbox" / "tools" / "shims")

    assert env["PATH"] == f"/usr/bin:/bin:{shim}"
    assert (Path(shim) / "python").exists()  # materialized on the host side too
    # Idempotent: a second pass adds nothing.
    assert shims.with_interpreter_shims(env)["PATH"] == f"/usr/bin:/bin:{shim}"


def test_with_interpreter_shims_never_removes_a_directory(lionclaw_home):
    """A missing PATH starts from os.defpath — adding shims must not shrink it."""
    env = shims.with_interpreter_shims({})
    for entry in [p for p in os.defpath.split(os.pathsep) if p]:
        assert entry in env["PATH"].split(os.pathsep)


def test_with_interpreter_shims_leaves_env_alone_when_unavailable(monkeypatch):
    monkeypatch.setattr(shims, "host_shim_dir", lambda: None)
    env = {"PATH": "/usr/bin"}
    assert shims.with_interpreter_shims(env) == {"PATH": "/usr/bin"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX shim")
def test_bare_python_resolves_through_the_shim_on_a_python3_only_path(tmp_path):
    """The whole point, end to end: a PATH holding only `python3` plus the shim
    dir runs `python` — exactly the situation on a stale sandbox image."""
    python3 = shutil.which("python3")
    assert python3, "test host needs a python3"
    only3 = tmp_path / "only3"
    only3.mkdir()
    (only3 / "python3").symlink_to(python3)
    shim_dir = shims.ensure_interpreter_shims(tmp_path / "tools")
    assert shim_dir is not None

    env = {"PATH": f"{only3}:{shim_dir}"}
    assert shutil.which("python", path=env["PATH"]) == str(shim_dir / "python")
    out = subprocess.run(
        ["python", "-c", "import sys; print(sys.version_info.major)"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "3"

    pip = subprocess.run(
        # /bin/sh by absolute path: `env` above carries no /bin, and subprocess
        # resolves the argv[0] lookup against the env it was handed.
        [shutil.which("sh") or "/bin/sh", "-c", 'pip --version || echo "pip-missing:$?"'],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    # Either pip is present (version printed) or the failure came from
    # python3 itself, never from `pip: not found` (exit 127).
    assert "pip-missing:127" not in pip.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX shim")
def test_a_real_python_earlier_on_path_wins_over_the_shim(tmp_path):
    """Appended, never prepended: an image alias / venv `python` must win."""
    real = tmp_path / "real"
    real.mkdir()
    marker = real / "python"
    marker.write_text("#!/bin/sh\necho REAL\n")
    marker.chmod(0o755)
    shim_dir = shims.ensure_interpreter_shims(tmp_path / "tools")

    env = shims.with_interpreter_shims({"PATH": str(real)})
    # with_interpreter_shims appends the HOME-scoped dir; emulate the sandbox
    # ordering with this tmp shim dir the same way (tool layer, real, shims).
    env["PATH"] = f"{real}:{shim_dir}"
    out = subprocess.run(["python"], capture_output=True, text=True, env=env, timeout=30)
    assert out.stdout.strip() == "REAL"
