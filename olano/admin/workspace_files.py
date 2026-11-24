"""Cross-agent workspace file access for the admin (AgentFather) tool layer.

Agent shells are isolated from each other's workspaces by design — an agent's
sandbox cannot even see ``<home>/agents/<other>/...``. AgentFather's privilege
to inspect and modify other agents lives in THIS in-process layer instead, so
every operation is scoped, validated, and (for writes) HITL-gated at the tool
layer, rather than handed out as raw filesystem reach.

Every path is resolved and required to stay inside the target agent's
``workspace/`` — traversal (``..``) and symlink escapes land outside the root
after resolution and are rejected. Writes create missing parents (an agent that
exists only as config has no workspace yet) and hand ownership to the sandbox
uid so the target agent can edit what was written for it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from olano.config.home import get_olano_paths

# A workspace file an admin tool reads back into a model turn, not a data lane.
_READ_MAX_BYTES = 256 * 1024
_WRITE_MAX_BYTES = 2 * 1024 * 1024
_LIST_MAX_ENTRIES = 500


def _workspace_root(agent: str) -> Path:
    paths = get_olano_paths()
    if not (paths.agents_dir / agent / "agent.json5").is_file():
        raise ValueError(f"Agent '{agent}' does not exist. Create the agent first.")
    return paths.agent_workspace_dir(agent)


def _resolve_inside(root: Path, rel: str) -> Path:
    """Resolve ``rel`` against ``root`` and require the result stays inside it.

    ``resolve()`` follows symlinks and collapses ``..``, so a projection like
    ``shared/default`` (a symlink out of the workspace) or a crafted
    ``../../vault`` both resolve outside the root and are refused with the same
    message — the caller only ever learns "outside the workspace".
    """
    candidate = (root / rel.lstrip("/")) if rel else root
    resolved = candidate.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PermissionError(f"Path '{rel}' is outside agent workspace '{root}'.")
    return resolved


def list_agent_workspace(agent: str, path: str = "") -> dict[str, Any]:
    """List a directory inside another agent's workspace (read-only).

    Returns name/kind/size per entry, capped at ``_LIST_MAX_ENTRIES``. A missing
    workspace (agent never booted) reports ``exists: False`` rather than
    erroring — that state is the answer, not a failure.
    """
    root = _workspace_root(agent)
    target = _resolve_inside(root, path)
    if not target.exists():
        return {"agent": agent, "path": path or ".", "exists": False, "entries": []}
    if not target.is_dir():
        raise NotADirectoryError(f"'{path}' is a file — use olano_read_agent_file.")
    entries: list[dict[str, Any]] = []
    truncated = False
    for child in sorted(target.iterdir()):
        if len(entries) >= _LIST_MAX_ENTRIES:
            truncated = True
            break
        try:
            stat = child.lstat()
            kind = "symlink" if child.is_symlink() else "dir" if child.is_dir() else "file"
            entries.append({"name": child.name, "kind": kind, "size": stat.st_size})
        except OSError:
            continue
    return {
        "agent": agent,
        "path": path or ".",
        "exists": True,
        "entries": entries,
        "truncated": truncated,
    }


def read_agent_workspace_file(agent: str, path: str) -> dict[str, Any]:
    """Read a text file inside another agent's workspace (read-only, capped)."""
    root = _workspace_root(agent)
    target = _resolve_inside(root, path)
    if not target.is_file():
        raise FileNotFoundError(f"'{path}' does not exist in agent '{agent}''s workspace.")
    size = target.stat().st_size
    if size > _READ_MAX_BYTES:
        raise ValueError(
            f"'{path}' is {size} bytes — larger than the {_READ_MAX_BYTES}-byte admin "
            "read cap. Ask the target agent to summarize it, or read a narrower file."
        )
    return {
        "agent": agent,
        "path": path,
        "size": size,
        "content": target.read_text(encoding="utf-8", errors="replace"),
    }


def write_agent_workspace_file(
    agent: str, path: str, content: str, *, overwrite: bool = True
) -> dict[str, Any]:
    """Write a text file into another agent's workspace (HITL-gated at the tool).

    Creates missing parents — the workspace itself included, so seeding an agent
    that has never booted works. Ownership of the file and every freshly created
    directory level is handed to the sandbox uid, or the target agent could read
    but never edit what was written for it.
    """
    if len(content.encode("utf-8")) > _WRITE_MAX_BYTES:
        raise ValueError(f"Content exceeds the {_WRITE_MAX_BYTES}-byte admin write cap.")
    root = _workspace_root(agent)
    target = _resolve_inside(root, path)
    if target == root.resolve():
        raise IsADirectoryError("Path must name a file inside the workspace, not its root.")
    existed = target.exists()
    if existed and not overwrite:
        raise FileExistsError(f"'{path}' already exists. Pass overwrite=True to replace it.")
    if existed and target.is_dir():
        raise IsADirectoryError(f"'{path}' is a directory.")

    from olano.core.builder import chown_for_sandbox

    root_resolved = root.resolve()
    missing: list[Path] = []
    probe = target.parent
    while probe != root_resolved and not probe.exists():
        missing.append(probe)
        probe = probe.parent
    if not root_resolved.exists():
        missing.append(root_resolved)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    for created in (*missing, target):
        chown_for_sandbox(created)
    return {
        "agent": agent,
        "path": path,
        "bytes": len(content.encode("utf-8")),
        "created": not existed,
        "note": "A running agent sees the file immediately (same directory its tools use).",
    }
