"""Which local files a browser on this box may open, and where a tool may save.

Chromium runs in the runtime process, not in the agent's sandbox, so a
``file://`` URL reads the *runtime host's* disk: on a managed box that is every
agent's config, the vault and the deployment's own files. The rule is "what
this agent's sandbox can already read" (its workspace, skill roots, allowed
shared folders, the bundled fonts), checked on the **fully resolved** path: the
agent can plant a symlink in its workspace from the sandbox, and Chromium on
the host follows it to the host's target.

The same boundary, read-write only, decides where a screenshot or a render may
be saved (:func:`save_check`): ``save_path`` used to be taken as given, so a
prompt-injected agent could overwrite ``config.yaml`` or another agent's
``agent.json5`` with PNG bytes.

Only a browser on this host can be pointed at these files at all: the live
sidecar and a remote provider have their own filesystems, and the tools refuse
``file://`` there before any of this runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

logger = logging.getLogger(__name__)


def url_scheme(url: str) -> str:
    try:
        return urlsplit(url.strip()).scheme.lower()
    except ValueError:
        return ""


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def file_url_path(url: str) -> Path | None:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() != "file" or parts.netloc not in ("", "localhost"):
        return None
    raw = url2pathname(parts.path)
    return Path(raw) if raw else None


@dataclass(frozen=True)
class FileAccess:
    """The resolved roots one agent's browser may read (or a tool may write)."""

    roots: tuple[Path, ...]
    denied: tuple[Path, ...] = ()
    # "open" (a file the browser loads) or "save" (a file a tool writes); the
    # same boundary check, phrased for what the agent asked.
    what: str = "open"

    def refusal(self, url: str) -> str | None:
        path = file_url_path(url)
        if path is None:
            return f"'{url[:200]}' is not a local file the browser may open."
        return self.refusal_path(path)

    def refusal_path(self, path: Path) -> str | None:
        """Refuse *path* when its resolved location is outside the roots. A
        path that does not exist yet (a file about to be written) resolves as
        far as it exists, so a symlinked parent is still judged by its target."""
        try:
            real = path.resolve()
        except (OSError, RuntimeError):
            return f"Local file '{path}' could not be resolved."
        verb = "saved there" if self.what == "save" else "opened"
        if any(_within(real, d) for d in self.denied):
            return f"Local file '{path}' holds the browser's saved sign-ins and cannot be {verb}."
        if any(_within(real, r) for r in self.roots):
            return None
        if self.what == "save":
            return (
                f"'{path}' is outside this agent's workspace and shared folders, so the "
                "file cannot be saved there. Use a path inside the workspace."
            )
        return (
            f"Local file '{path}' is outside this agent's workspace, skills and shared "
            "folders, so the browser may not open it."
        )


def _signins_dir() -> Path | None:
    try:
        from olano.browser.sessions import state_dir

        return state_dir()
    except Exception:
        return None


def file_access(workspace_root: Path, agent: str, *, for_write: bool = False) -> FileAccess:
    """The local files *agent* may open, mirroring what its sandbox mounts
    (``builder._resolve_sandbox_shared_mounts``), plus the packaged built-in
    skills. With ``for_write``, the files a tool may *save*: the workspace and
    the shared folders mounted read-write, nothing read-only. Resolve in the
    sync tool body. Every lookup degrades to the narrower answer: a config that
    will not load leaves the workspace (and, for reads, the managed skills and
    the fonts)."""
    from olano.config.workspace import Workspace

    roots: list[Path] = [workspace_root]
    config = None
    if agent:
        try:
            from olano.config.loader import load_agent_config

            config = load_agent_config(agent)
        except Exception:
            logger.debug("browser: could not load %s's config for file access", agent)
    workspace = Workspace(root=workspace_root)
    if not for_write:
        try:
            if config is not None:
                from olano.skills.loader import skill_read_roots

                roots.extend(skill_read_roots(config, workspace))
            else:
                roots.append(workspace.managed_skills_dir)
        except Exception:
            logger.debug("browser: could not resolve skill roots", exc_info=True)
    if config is not None:
        try:
            from olano.core.builder import _sandbox_shared_source_allowed
            from olano.security.deepagents_middleware import _session_shared_path_configs
            from olano.security.sharing import resolve_shared_mounts

            for mount in resolve_shared_mounts(config, _session_shared_path_configs()):
                if for_write and mount.mode != "rw":
                    continue
                if _sandbox_shared_source_allowed(Path(mount.source)):
                    roots.append(Path(mount.source))
        except Exception:
            logger.debug("browser: could not resolve shared folders", exc_info=True)
    if not for_write:
        try:
            # The packaged built-in skills: public, and the same files every
            # box syncs into <home>/skills. A chart page built from this copy
            # (a dev box, an open-posture shell) references its vendored Vega.
            from olano.skills.built_in_sync import _builtin_source_dir

            roots.append(_builtin_source_dir())
        except Exception:
            logger.debug("browser: could not resolve the built-in skills dir", exc_info=True)
        try:
            from olano.backends.interpreter_shims import FONTS_SUBDIR
            from olano.config.home import get_olano_paths

            roots.append(get_olano_paths().root / "sandbox" / "tools" / FONTS_SUBDIR)
        except Exception:
            logger.debug("browser: could not resolve the fonts dir", exc_info=True)

    resolved: list[Path] = []
    for root in roots:
        try:
            real = root.resolve()
        except (OSError, RuntimeError):
            continue
        if real not in resolved:
            resolved.append(real)
    denied: list[Path] = []
    signins = _signins_dir()
    if signins is not None:
        try:
            denied.append(signins.resolve())
        except (OSError, RuntimeError):
            pass
    return FileAccess(
        roots=tuple(resolved),
        denied=tuple(denied),
        what="save" if for_write else "open",
    )


def save_check(path: Path, workspace_root: Path, agent: str) -> str | None:
    """Refusal for a file a tool was asked to write, or ``None``.

    Same boundary as ``write_file`` (workspace + read-write shared folders),
    deliberately without its trust-level rule: a memory-only agent may still
    render a chart.
    """
    return file_access(workspace_root, agent, for_write=True).refusal_path(path)
