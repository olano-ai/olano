"""Interpreter shims: a bare ``python`` (and ``pip``) that resolves on every lane.

Models reach for ``python`` by reflex. The alias exists in the sandbox image
(``python-is-python3``), in the runtime image (``python:*-slim`` ships one) and
on apt hosts (``install.sh``) — and it still failed in production, because all
three fixes live *in an image*: a box running an older ``olano-sandbox:latest``,
or a persistent container created from one, has no ``python``, and re-pulling
the image never recreates a running container (the signature hashes the tag,
not a digest). Every image drift reopened the same hole.

This module moves the guarantee into the runtime, which owns the shell on every
lane. It writes two-line POSIX shims into the **shared tool layer** — the one
host directory (``<home>/sandbox/tools``) that every sandbox bind-mounts at
:data:`CONTAINER_TOOL_LAYER_DIR` and that the host lanes can reach directly —
and callers append that directory to the **end** of ``PATH``:

- the sandbox ``execute`` prefix (``lionclaw_docker._exec``) and the sandboxed
  stdio-MCP launch line;
- the open-posture ``LocalShellBackend`` env and ``shell_guard.run_local``;
- the ``safe_shell`` tool and ``tools/_sandbox_exec`` host branch;
- the dashboard Terminal, through :data:`TERMINAL_RC_NAME` (``bash --rcfile``),
  since Debian's ``/etc/profile`` resets PATH under ``bash -l``.

**Appended, never prepended.** A real ``python`` — the image alias, a venv,
pyenv — always wins; the shim only fills a name nothing else provides. That is
also why no ``_container_signature`` schema bump is needed: the mount is the
same and PATH is set per exec, so the container that is broken today picks the
fix up on its next command.

Everything here is best-effort and never raises: a shell that cannot get its
shim is exactly as broken as it was before, not more.
"""

from __future__ import annotations

import logging
import os
import shlex
from pathlib import Path

logger = logging.getLogger(__name__)

# ── container-side layout of the shared tool layer ────────────────────────────
# Single source for these strings; ``lionclaw_docker`` re-exports them under its
# historical underscore names. They describe paths INSIDE a sandbox container.
CONTAINER_TOOL_LAYER_DIR = "/opt/agent-tools"
CONTAINER_TOOL_LAYER_ENV: dict[str, str] = {
    # PIP_USER=1 makes a bare `pip install X` default to the user site
    # (PYTHONUSERBASE below) — the non-root sandbox user cannot write system
    # site-packages, so without this the natural `pip install` fails with
    # Permission denied. Together they land every install in the shared layer.
    "PIP_USER": "1",
    "PYTHONUSERBASE": f"{CONTAINER_TOOL_LAYER_DIR}/python",
    "PIP_CACHE_DIR": f"{CONTAINER_TOOL_LAYER_DIR}/cache/pip",
    "npm_config_prefix": f"{CONTAINER_TOOL_LAYER_DIR}/npm",
    "npm_config_cache": f"{CONTAINER_TOOL_LAYER_DIR}/cache/npm",
}
# Prepended to PATH: shared console-scripts (pip --user bin, npm -g bin) and
# seeded CLIs (Go binaries in bin/) must win over the image's own copies.
CONTAINER_TOOL_LAYER_PATH = (
    f"{CONTAINER_TOOL_LAYER_DIR}/python/bin:"
    f"{CONTAINER_TOOL_LAYER_DIR}/npm/bin:"
    f"{CONTAINER_TOOL_LAYER_DIR}/bin"
)

#: Per-installer directory skeletons the HOST side must create under the target
#: an env var names, because the installer expects them and will not make them.
#: Keyed by the ``CONTAINER_TOOL_LAYER_ENV`` entry they hang off; relative to it.
#:
#: ``npm_config_prefix``: npm never creates its prefix, and npm 9 and 10 (the
#: sandbox image's until it moved to Node 24's npm 11, and still the host
#: lane's) ``lstat`` ``<prefix>/lib`` on every ``npx`` (measured: ``npm/`` alone
#: moves the ENOENT one level down to ``npm/lib``; ``npm/lib`` is sufficient).
#: npm 11 runs ``npx`` without it (measured 2026-11-28), but an older image a
#: box has not re-pulled still needs it, so the skeleton stays. ``lib/node_modules`` + ``bin`` is
#: the exact tree ``npm install -g`` produces, so a later npm that looks one
#: level deeper still finds it. pip needs nothing: ``PIP_USER`` +
#: ``PYTHONUSERBASE`` is a user base, a tree Python builds on demand.
INSTALLER_SKELETONS: dict[str, tuple[str, ...]] = {
    "npm_config_prefix": ("lib/node_modules", "bin"),
}


def tool_layer_layout() -> tuple[str, ...]:
    """Every directory under the tool layer the host side must create, relative
    to it, each ancestor before its children.

    **Derived, not listed.** The runtime bind-mounts ``<home>/sandbox/tools``
    over ``/opt/agent-tools`` and a bind mount **shadows** rather than merges:
    the image creates ``python/ npm/ cache/ bin/`` too, and under the mount
    none of them exist. So whatever the host directory lacks, no installer in
    the container can reach — and which installers survive that is an
    accident of each one (pip builds its user base; npm never creates its
    prefix). Reading the layout off ``CONTAINER_TOOL_LAYER_ENV`` and
    ``CONTAINER_TOOL_LAYER_PATH`` means a new install target added to either —
    a ``CARGO_HOME``, a ``UV_TOOL_DIR``, a ``GOBIN`` — gets its directory by
    construction, with no second list to forget. Ancestors are emitted first
    so the caller can hand every level to the sandbox uid: a leaf owned by
    uid 1000 under a root-owned parent is writable in place and a dead end
    for any sibling the installer wants to create next to it.
    """
    prefix = f"{CONTAINER_TOOL_LAYER_DIR}/"
    out: list[str] = []

    def _add(rel: str) -> None:
        parts = [p for p in rel.split("/") if p]
        for i in range(1, len(parts) + 1):
            level = "/".join(parts[:i])
            if level not in out:
                out.append(level)

    for key, value in CONTAINER_TOOL_LAYER_ENV.items():
        if not value.startswith(prefix):
            continue  # a flag such as PIP_USER=1, not a path
        rel = value[len(prefix) :]
        _add(rel)
        for skeleton in INSTALLER_SKELETONS.get(key, ()):
            _add(f"{rel}/{skeleton}")
    for entry in CONTAINER_TOOL_LAYER_PATH.split(":"):
        if entry.startswith(prefix):
            _add(entry[len(prefix) :])
    return tuple(out)


#: The layout above, materialized once at import for the two callers that
#: create it host-side (``builder.ensure_tool_layer_layout``) and the tests.
TOOL_LAYER_INSTALL_SUBDIRS: tuple[str, ...] = tool_layer_layout()

#: Subdirectory of the tool layer holding the shims (host and container alike).
SHIM_SUBDIR = "shims"
#: Appended to PATH: last resort only, never shadows an image-provided binary.
CONTAINER_SHIM_PATH = f"{CONTAINER_TOOL_LAYER_DIR}/{SHIM_SUBDIR}"
#: The Terminal's rc file, beside the shims (see :func:`render_terminal_rc`).
TERMINAL_RC_NAME = "terminal.bashrc"

#: Subdirectory of the tool layer holding the font faces a skill ships, and the
#: one path ``docker/olano-sandbox/skill-fonts.conf`` points fontconfig at. Same
#: reasoning as the shims: a skill root is mounted at its HOST path, unknowable
#: when the image is built, while the tool layer is a fixed container path the
#: runtime writes to from the host side.
FONTS_SUBDIR = "fonts"
#: Where those faces land inside a container.
CONTAINER_FONTS_DIR = f"{CONTAINER_TOOL_LAYER_DIR}/{FONTS_SUBDIR}"

#: shim name → the line it execs. A shim must never name itself: ``python``
#: execs ``python3``, so a box with no python3 at all fails loudly on that name
#: instead of looping.
SHIMS: dict[str, str] = {
    "python": 'exec python3 "$@"',
    "pip": 'exec python3 -m pip "$@"',
}

_MODE = 0o755


def shell_path_export() -> str:
    """The one ``export PATH=…`` line every sandbox command is prefixed with.

    Tool layer first, then whatever the image had, then the shims — so the
    two consumers (``lionclaw_docker._exec`` and the Terminal rc) cannot drift.
    Nothing here needs quoting, but ``shlex.quote`` keeps that true if a path
    ever grows a space.
    """
    return (
        f"export PATH={shlex.quote(CONTAINER_TOOL_LAYER_PATH)}:$PATH:"
        f"{shlex.quote(CONTAINER_SHIM_PATH)}"
    )


def render_shim(name: str, body: str) -> str:
    return (
        "#!/bin/sh\n"
        f"# olano interpreter shim: `{name}` for an image or host that ships only the\n"
        f"# versioned name. Last on PATH, so a real `{name}` (image alias, venv, pyenv)\n"
        "# always wins over this file. Written by olano/backends/interpreter_shims.py.\n"
        f"{body}\n"
    )


def render_terminal_rc() -> str:
    """rc file the dashboard Terminal hands to ``bash --rcfile … -i``.

    ``bash -l`` sources ``/etc/profile``, which on Debian **resets PATH**, so an
    export before it is lost and nothing under the tool layer — pip ``--user``
    scripts, npm globals, seeded CLIs, the shims — was reachable from the
    Terminal even though the agent's ``execute`` had all of them. This file
    sources the same profile first (completion, prompt, history from
    ``profile-interactive.sh`` still load; ``$-`` carries ``i``), then applies
    the exact env + PATH ``execute`` gets, so ``pip install`` from the Terminal
    lands in the shared layer too.
    """
    lines = [
        "# Generated by olano (olano/backends/interpreter_shims.py) — do not edit.",
        "# Sourced by the dashboard Terminal via `bash --rcfile` so an interactive",
        "# shell sees the shared tool layer and the interpreter shims exactly as the",
        "# agent's `execute` tool does. /etc/profile resets PATH, so it runs FIRST.",
        "[ -r /etc/profile ] && . /etc/profile",
        '[ -r "$HOME/.bashrc" ] && . "$HOME/.bashrc"',
    ]
    for key, val in CONTAINER_TOOL_LAYER_ENV.items():
        lines.append(f"export {key}={shlex.quote(val)}")
    lines.append(shell_path_export())
    return "\n".join(lines) + "\n"


def _write_if_changed(path: Path, content: str) -> None:
    data = content.encode("utf-8")
    try:
        if path.read_bytes() == data and (path.stat().st_mode & 0o777) == _MODE:
            return
    except OSError:
        pass
    # Atomic: two agent builds (or a build and a shell) can race on this file,
    # and a half-written shim on PATH is worse than none.
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_bytes(data)
    tmp.chmod(_MODE)
    tmp.replace(path)


def ensure_interpreter_shims(tool_dir: Path) -> Path | None:
    """Materialize the shims under ``tool_dir/shims``; return that dir or None.

    Idempotent and cheap (a stat + a small read per file), so it is safe to
    call from every shell spawn. ``None`` on Windows (no ``#!/bin/sh``) or when
    the directory cannot be written — the caller then just leaves PATH alone.
    Files are 0755: the sandbox user (uid 1000) only needs to read and exec
    them, so no chown is involved and a non-root runtime works too.
    """
    if os.name == "nt":
        return None
    shim_dir = Path(tool_dir) / SHIM_SUBDIR
    try:
        shim_dir.mkdir(parents=True, exist_ok=True)
        try:
            shim_dir.chmod(_MODE)
        except OSError:
            pass  # not ours (e.g. pre-existing, other owner) — still usable
        for name, body in SHIMS.items():
            _write_if_changed(shim_dir / name, render_shim(name, body))
        _write_if_changed(shim_dir / TERMINAL_RC_NAME, render_terminal_rc())
    except OSError as exc:
        logger.debug("interpreter shims unavailable under %s: %s", shim_dir, exc)
        return None
    return shim_dir


def _skill_fonts_source() -> Path | None:
    """Where the bundled OFL faces live on this host, or ``None``.

    The synced copy under the olano home wins: it is what an agent's own skill
    prompt points at, so the faces LibreOffice can name are the faces the agent
    was told about. The in-package copy is the fallback for a box that has not
    synced yet (and for the tests).
    """
    candidates: list[Path] = []
    try:
        from olano.config.home import get_olano_paths

        candidates.append(get_olano_paths().managed_skills_dir / "canvas-design" / "canvas-fonts")
    except Exception:  # pragma: no cover - home resolution is not this call's job
        pass
    candidates.append(
        Path(__file__).resolve().parents[1]
        / "skills"
        / "built_in"
        / "canvas-design"
        / "canvas-fonts"
    )
    return next((c for c in candidates if c.is_dir()), None)


def ensure_skill_fonts(tool_dir: Path) -> Path | None:
    """Copy the skill's font faces into ``tool_dir/fonts``; return it or None.

    LibreOffice resolves a face by NAME through fontconfig, so a document that
    asks for one of the bundled families gets a substitute unless the file is
    somewhere fontconfig scans — and the image's ``skill-fonts.conf`` scans
    exactly one path, the container side of this directory. (The browser
    renderers need none of this: they embed their faces as data URIs.)

    Idempotent and cheap: a stat per face, a copy only when size or mtime moved.
    Best-effort like the shims — a box that cannot write the layer just keeps
    the system fonts.
    """
    src = _skill_fonts_source()
    if src is None:
        return None
    dest = Path(tool_dir) / FONTS_SUBDIR
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for face in sorted(src.glob("*.ttf")):
            target = dest / face.name
            try:
                st_src, st_dst = face.stat(), target.stat()
                if st_src.st_size == st_dst.st_size and st_dst.st_mtime >= st_src.st_mtime:
                    continue
            except OSError:
                pass
            tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
            tmp.write_bytes(face.read_bytes())
            tmp.chmod(0o644)
            tmp.replace(target)
    except OSError as exc:
        logger.debug("skill fonts unavailable under %s: %s", dest, exc)
        return None
    return dest


def host_tool_layer_dir() -> Path:
    """The host side of the shared tool layer (``<home>/sandbox/tools``).

    Mirrors ``builder._resolve_sandbox_shared_mounts`` and
    ``lionclaw_docker.mcp_stdio_run_args`` — one directory, three readers.
    """
    from olano.config.home import get_olano_paths

    return get_olano_paths().root / "sandbox" / "tools"


def host_shim_dir() -> Path | None:
    """The shim dir as a **host** path, materialized; None when unavailable."""
    try:
        return ensure_interpreter_shims(host_tool_layer_dir())
    except Exception as exc:  # get_olano_paths() misconfigured, etc.
        logger.debug("interpreter shims: no host dir: %s", exc)
        return None


def with_interpreter_shims(env: dict[str, str]) -> dict[str, str]:
    """Append the host shim dir to ``env["PATH"]`` (once, at the end) and return ``env``.

    For the host lanes: the open-posture ``LocalShellBackend``, ``run_local``,
    ``safe_shell`` and ``sandboxed_run``'s subprocess branch. A missing PATH
    starts from ``os.defpath`` — the same default ``/bin/sh`` would fall back
    to — so adding the shims can never *remove* a directory.
    """
    shim_dir = host_shim_dir()
    if shim_dir is None:
        return env
    current = env.get("PATH") or os.defpath
    entries = [p for p in current.split(os.pathsep) if p]
    shim = str(shim_dir)
    if shim not in entries:
        entries.append(shim)
    env["PATH"] = os.pathsep.join(entries)
    return env
