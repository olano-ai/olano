"""Workspace housekeeping tools: a recoverable trash can, scratch, downloads.

Every agent gets a consistent set of well-known folders under its workspace
root so files always land somewhere predictable:

- ``downloads/`` — files fetched from the web land here by default
  (see ``http_download``). Use this when the user doesn't name a destination.
- ``scratch/``   — throwaway scripts, log files, and temporary artifacts.
- ``trash/``     — a *recoverable* deleted area. Instead of permanently
  removing a file, move it here with ``trash_put`` so it can be restored, then
  reclaim space with ``trash_empty`` when you're sure.

The trash is a lightweight take on the freedesktop trash spec: each trashed
item is stored under ``trash/<timestamp>__<name>`` alongside a ``.trashinfo``
JSON sidecar recording its original path and deletion time, so ``trash_restore``
can put it back exactly where it came from.

SECURITY — these tools move files with raw ``shutil`` calls, entirely outside
the deepagents backend/permission layer, so they enforce their own boundary:

- Every source, destination, and restore target is resolved (symlinks
  followed) and must land **inside the agent's workspace root** — absolute
  paths, ``..`` traversal, and symlinks pointing out of the workspace are
  refused. Without this, ``trash_put``/``trash_restore`` would be an
  arbitrary move-anything-anywhere primitive.
- Mutations (``trash_put``/``trash_restore``/``trash_empty``) are refused when
  the agent's backend is read-only (``access_mode: "read"``), which the
  runner stamps into the request context as ``workspace_access_mode``.
  Otherwise they would punch a write hole through a read-only backend.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool

from olano.config.workspace import get_workspace
from olano.security.request_context import get_request_context
from olano.security.tool_metadata import tool_metadata

_INFO_SUFFIX = ".trashinfo"


def _workspace_root() -> Path:
    """Resolve the running agent's workspace root from the request context.

    Falls back to the default workspace only when no context is active (e.g.
    background jobs), so trashing/restoring always targets the agent's own
    ``~/.olano/agents/<name>/workspace`` rather than the legacy/global one.
    """
    workspace_root = get_request_context().metadata.get("workspace_root")
    return get_workspace(str(workspace_root) if workspace_root else None).root


def _trash_dir() -> Path:
    d = _workspace_root() / "trash"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_source(path: str) -> Path:
    """Resolve a user-supplied path; relative paths are taken from the workspace root."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = _workspace_root() / p
    return p


def _access_mode() -> str:
    """The running agent's workspace access mode (``"read"`` | ``"read_write"``).

    Stamped into the request context by ``AgentRunner._request_context_metadata``
    from the agent's backend config. Contexts without the stamp (background
    jobs, CLI helpers, tests) default to ``read_write`` — the historical
    behavior.
    """
    mode = get_request_context().metadata.get("workspace_access_mode")
    return mode if mode in ("read", "read_write") else "read_write"


_READ_ONLY_ERROR = (
    'Error: this agent\'s workspace is read-only (backend access_mode "read"); '
    "trash operations that move or delete files are disabled."
)


def _confine_to_workspace(path: Path) -> Path | None:
    """Resolve *path* and return it only if it lies inside the workspace root.

    Symlinks are resolved first, so a link pointing outside the workspace is
    refused rather than followed. The workspace root itself is refused too —
    relocating the whole workspace is never a trash operation.
    """
    root = _workspace_root().resolve()
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return None
    if resolved == root or not resolved.is_relative_to(root):
        return None
    return resolved


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False)
@tool
def trash_put(
    path: Annotated[str, "Path to the file or folder to move to the trash"],
) -> str:
    """Move a file or folder into the workspace trash can (recoverable delete).

    Prefer this over permanently deleting files. The item can be restored with
    ``trash_restore`` until the trash is emptied with ``trash_empty``. Only
    paths inside the agent's workspace can be trashed.
    """
    if _access_mode() == "read":
        return _READ_ONLY_ERROR
    src = _confine_to_workspace(_resolve_source(path))
    if src is None:
        return (
            f"Error: '{path}' is outside this agent's workspace. Only files inside "
            "the workspace can be moved to its trash."
        )

    trash = _trash_dir()
    if src == trash or src.is_relative_to(trash):
        return f"Error: '{path}' is already in the trash."
    if not src.exists():
        return f"Error: '{path}' does not exist."
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    entry_name = f"{stamp}__{src.name}"
    dest = trash / entry_name
    # Avoid collisions if two items are trashed within the same second.
    counter = 1
    while dest.exists() or (trash / (entry_name + _INFO_SUFFIX)).exists():
        entry_name = f"{stamp}_{counter}__{src.name}"
        dest = trash / entry_name
        counter += 1

    info = {
        "original_path": str(src),
        "deleted_at": datetime.now(timezone.utc).isoformat(),
        "name": src.name,
    }
    try:
        shutil.move(str(src), str(dest))
    except Exception as exc:
        return f"Error moving '{path}' to trash: {exc}"
    (trash / (entry_name + _INFO_SUFFIX)).write_text(json.dumps(info, indent=2), encoding="utf-8")
    return (
        f'Moved to trash: {src} → trash/{entry_name}\nRestore with trash_restore("{entry_name}").'
    )


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def trash_list() -> str:
    """List items currently in the workspace trash can, with original paths and sizes."""
    trash = _trash_dir()
    entries = sorted(
        (p for p in trash.iterdir() if not p.name.endswith(_INFO_SUFFIX)),
        key=lambda p: p.name,
    )
    if not entries:
        return "Trash is empty."

    lines = ["Trash contents:"]
    total = 0
    for entry in entries:
        size = _dir_size(entry)
        total += size
        info_path = trash / (entry.name + _INFO_SUFFIX)
        origin = ""
        if info_path.exists():
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
                origin = f"  (from {info.get('original_path', '?')})"
            except Exception:
                pass
        lines.append(f"- {entry.name} — {_human_size(size)}{origin}")
    lines.append(f"\nTotal: {len(entries)} item(s), {_human_size(total)}.")
    return "\n".join(lines)


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False)
@tool
def trash_restore(
    entry_name: Annotated[str, "The trash entry name (as shown by trash_list)"],
    destination: Annotated[
        str, "Optional path to restore to; defaults to the original location"
    ] = "",
) -> str:
    """Restore an item from the trash back to its original location (or a given destination)."""
    if _access_mode() == "read":
        return _READ_ONLY_ERROR
    # The entry must be a bare name directly inside trash/ — no separators or
    # traversal, or this becomes a move-anything primitive.
    if not entry_name or Path(entry_name).name != entry_name or entry_name in {".", ".."}:
        return f"Error: invalid trash entry name '{entry_name}'. Use trash_list to see entries."
    trash = _trash_dir()
    entry = trash / entry_name
    if not entry.exists():
        return f"Error: no trash entry named '{entry_name}'. Use trash_list to see entries."

    info_path = trash / (entry_name + _INFO_SUFFIX)
    if destination:
        target = _resolve_source(destination)
    elif info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            target = Path(info["original_path"])
        except Exception:
            return "Error: trash metadata is unreadable; pass an explicit destination."
    else:
        return "Error: no original-path metadata; pass an explicit destination."

    # Covers explicit destinations AND sidecar original paths: entries trashed
    # from outside the workspace (pre-confinement layouts, edited sidecars)
    # must not restore outside it.
    confined = _confine_to_workspace(target)
    if confined is None:
        return (
            f"Error: refusing to restore outside this agent's workspace ('{target}'). "
            "Pass a destination inside the workspace instead."
        )
    target = confined

    if target.exists():
        return f"Error: '{target}' already exists. Pass a different destination."
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(entry), str(target))
    except Exception as exc:
        return f"Error restoring '{entry_name}': {exc}"
    if info_path.exists():
        info_path.unlink()
    return f"Restored: trash/{entry_name} → {target}"


@tool_metadata(capability="mutating", risk="high", requires_hitl=False)
@tool
def trash_empty(
    confirm: Annotated[
        bool, "Must be true to permanently delete. If false, returns a preview only."
    ] = False,
) -> str:
    """Permanently delete everything in the workspace trash can.

    Call with ``confirm=false`` first to preview what will be deleted; call again
    with ``confirm=true`` to actually empty the trash. This cannot be undone.
    """
    if _access_mode() == "read":
        return _READ_ONLY_ERROR
    trash = _trash_dir()
    entries = [p for p in trash.iterdir() if not p.name.endswith(_INFO_SUFFIX)]
    if not entries:
        return "Trash is already empty."

    total = sum(_dir_size(e) for e in entries)
    if not confirm:
        names = ", ".join(e.name for e in entries[:10])
        more = "" if len(entries) <= 10 else f" (+{len(entries) - 10} more)"
        return (
            f"Trash holds {len(entries)} item(s), {_human_size(total)}: {names}{more}.\n"
            "This is permanent and cannot be undone. Call trash_empty(confirm=true) to proceed."
        )

    removed = 0
    for entry in entries:
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            info_path = trash / (entry.name + _INFO_SUFFIX)
            if info_path.exists():
                info_path.unlink()
            removed += 1
        except Exception as exc:
            return f"Error while emptying trash (removed {removed} so far): {exc}"
    return f"Emptied trash: permanently deleted {removed} item(s), {_human_size(total)}."


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
