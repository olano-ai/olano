"""Shared path grants, sandbox-visible roots, and workspace projections."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from olano.config.agent_config import AgentConfig, SharedPathConfig
from olano.config.home import get_olano_paths, load_home_config

DEFAULT_SHARED_TARGET = "/shared/default"
_MOUNT_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


@dataclass(frozen=True)
class SharedMount:
    """Resolved share mount for the current subject."""

    id: str
    source: Path
    target: str
    mode: str
    persistence: str


def default_shared_source() -> Path:
    path = get_olano_paths().shared_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_shared_grant() -> SharedPathConfig:
    return SharedPathConfig(
        id="default",
        source=str(default_shared_source()),
        target=DEFAULT_SHARED_TARGET,
        mode="rw",
        persistence="persistent",
    )


def sanitize_mount_name(name: str) -> str:
    cleaned = _MOUNT_NAME_RE.sub("_", name.strip()).strip("._-")
    return cleaned or "shared"


def canonicalize_share_source(source: str | Path) -> Path:
    return Path(source).expanduser().resolve(strict=True)


def normalize_share_target(target: str | None, source: Path) -> str:
    if target:
        base = sanitize_mount_name(target)
    else:
        base = sanitize_mount_name(source.stem or source.name)
    return f"/shared/{base}"


def is_running_in_container() -> bool:
    return os.environ.get("LIONCLAW_CONTAINERIZED") == "1" or Path("/.dockerenv").exists()


def allowed_bind_roots() -> list[Path]:
    home_paths = get_olano_paths()
    home_config = load_home_config()
    env_extra = os.environ.get("LIONCLAW_EXTRA_BIND_ROOTS", "")
    roots = [home_paths.root]
    roots.extend(Path(p).expanduser().resolve() for p in home_config.extra_bind_roots)
    roots.extend(Path(p).expanduser().resolve() for p in env_extra.split(os.pathsep) if p.strip())
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def validate_share_source_path(source: Path) -> str | None:
    """Return an error string when a containerized install cannot see a host path."""
    if not is_running_in_container():
        return None
    for root in allowed_bind_roots():
        try:
            source.relative_to(root)
            return None
        except ValueError:
            continue
    default_root = get_olano_paths().shared_dir
    return (
        f"Path '{source}' is outside the container bind roots. "
        f"Add a startup bind root or copy/link it into '{default_root}'."
    )


def _subject_matches(grant: SharedPathConfig, subject_type: str, subject_name: str) -> bool:
    for subject in grant.subjects:
        if subject.type == "agent" and subject_type == "agent" and subject.name == "self":
            return True
        if (
            subject.type == "subagent"
            and subject_type == "subagent"
            and subject.name == subject_name
        ):
            return True
        if subject.type == "all_subagents" and subject_type == "subagent":
            return True
    return False


def resolve_shared_mounts(
    config: AgentConfig,
    session_grants: list[SharedPathConfig] | None = None,
    *,
    subject_type: str = "agent",
    subject_name: str = "self",
    include_default: bool = True,
) -> list[SharedMount]:
    mounts: list[SharedMount] = []
    if include_default:
        grant = default_shared_grant()
        mounts.append(
            SharedMount(
                id=grant.id,
                source=canonicalize_share_source(grant.source),
                target=grant.target,
                mode=grant.mode,
                persistence=grant.persistence,
            )
        )
    for grant in [*(config.shared_paths or []), *(session_grants or [])]:
        if not _subject_matches(grant, subject_type, subject_name):
            continue
        mounts.append(
            SharedMount(
                id=grant.id,
                source=canonicalize_share_source(grant.source),
                target=grant.target,
                mode=grant.mode,
                persistence=grant.persistence,
            )
        )
    return mounts


def sandbox_visible_roots(
    workspace_root: Path,
    mounts: list[SharedMount],
) -> list[Path]:
    visible = [workspace_root.resolve()]
    for mount in mounts:
        visible.append(mount.source)
    return visible


def workspace_share_path(workspace_root: Path, target: str) -> Path:
    rel = target.lstrip("/")
    return workspace_root / rel


def sync_workspace_share_projections(workspace_root: Path, mounts: list[SharedMount]) -> None:
    """
    Project sandbox-visible shared paths into workspace/shared/* for agent access.

    The workspace/shared namespace is reserved for Olano-managed projections.
    """
    shared_root = workspace_root / "shared"
    shared_root.mkdir(parents=True, exist_ok=True)

    for child in list(shared_root.iterdir()):
        if child.is_symlink():
            child.unlink()
            continue
        if child.is_dir():
            shutil.rmtree(child)
            continue
        child.unlink()

    for mount in mounts:
        target_path = workspace_share_path(workspace_root, mount.target)
        if shared_root not in target_path.parents and target_path != shared_root:
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists() or target_path.is_symlink():
            if target_path.is_dir() and not target_path.is_symlink():
                shutil.rmtree(target_path)
            else:
                target_path.unlink()
        target_path.symlink_to(mount.source, target_is_directory=mount.source.is_dir())


def is_path_within_roots(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False
