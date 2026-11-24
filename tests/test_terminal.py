from __future__ import annotations

import os

from olano.runtime import terminal


def test_child_env_sets_runtime_venv(monkeypatch, tmp_path):
    venv = tmp_path / ".venv"
    vbin = venv / "bin"
    vbin.mkdir(parents=True)
    (vbin / "activate").write_text("# activate\n", encoding="utf-8")

    monkeypatch.setattr(terminal.sys, "executable", str(vbin / "python"))
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", str(vbin), "/bin"]))
    monkeypatch.setenv("PYTHONHOME", "/bad/pythonhome")

    env = terminal._child_env()

    assert env["VIRTUAL_ENV"] == str(venv)
    assert env["PATH"].split(os.pathsep)[0] == str(vbin)
    assert env["PATH"].split(os.pathsep).count(str(vbin)) == 1
    assert "PYTHONHOME" not in env


def test_prepare_zsh_startup_activates_after_user_rc(monkeypatch, tmp_path):
    home = tmp_path / "home"
    lionclaw_home = tmp_path / "olano-home"
    home.mkdir()
    vbin = tmp_path / ".venv" / "bin"
    vbin.mkdir(parents=True)
    (vbin / "activate").write_text("export VIRTUAL_ENV_ACTIVATED=1\n", encoding="utf-8")

    for name in [".zshenv", ".zprofile", ".zshrc", ".zlogin"]:
        (home / name).write_text(f"# {name}\n", encoding="utf-8")

    monkeypatch.setattr(terminal.os, "getpid", lambda: 12345)
    env = {"HOME": str(home), "LIONCLAW_HOME": str(lionclaw_home)}

    terminal._prepare_zsh_startup(env, vbin)

    zdir = lionclaw_home / "run" / "terminal-zdotdir-12345"
    assert env["ZDOTDIR"] == str(zdir)
    assert "source" in (zdir / ".zshrc").read_text(encoding="utf-8")
    zlogin = (zdir / ".zlogin").read_text(encoding="utf-8")
    assert str(home / ".zlogin") in zlogin
    assert str(vbin / "activate") in zlogin


def test_venv_activate_snippet_fallback_exports_venv(tmp_path):
    vbin = tmp_path / ".venv" / "bin"
    snippet = terminal._venv_activate_snippet(vbin)

    assert "export VIRTUAL_ENV=" in snippet
    assert f"export PATH={vbin}:$PATH" in snippet
