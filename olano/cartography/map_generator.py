"""Workspace map generator — produces the L0 workspace_map.md."""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

from olano.cartography.classifier import category_emoji, classify_file
from olano.cartography.walker import EXTERNAL_REPO_LABEL, iter_workspace
from olano.config.workspace import WORKSPACE_STATE_DIRNAME

logger = logging.getLogger(__name__)

# Directories listed in the index but kept out of the always-on L0 map.
# `customers` holds the per-customer private memory of a customer-service agent
# (see olano/memory/customer_memory.py): mapping it would put other
# customers' ids — and their memory filenames — into the always-on L0 prompt
# block of every conversation. (The walker already prunes it from the index;
# kept here too so a stale index can never leak it into the map.)
_MAP_SKIP_DIRS = {"screenshots", "customers"}

# Maximum total characters for the workspace map (to keep L0 lean)
_MAX_MAP_CHARS = 3_000


def _scan_to_index(workspace_root: Path) -> dict[str, dict]:
    """Standalone scan for callers with no cartographer index at hand.

    Same pruned walk the cartographer uses (embedded repos and venvs are never
    descended into), classification done inline.
    """
    index: dict[str, dict] = {}
    for kind, rel, _stat in iter_workspace(workspace_root):
        if kind == "repo":
            index[rel] = {"category": "project", "label": EXTERNAL_REPO_LABEL}
            continue
        try:
            category, label = classify_file(workspace_root / rel, workspace_root)
        except Exception:
            continue
        index[rel] = {"category": category, "label": label}
    return index


def generate_workspace_map(
    workspace_root: Path,
    max_chars: int = _MAX_MAP_CHARS,
    *,
    index: dict[str, dict] | None = None,
) -> str:
    """Produce a compact Markdown map, grouped by category, one line per file.

    Built from *index* when the caller (the cartographer) already has one — the
    map generation itself then walks nothing. Without an index it runs its own
    pruned scan. Total length is capped at *max_chars*.
    """
    if not workspace_root.exists():
        return "Workspace not yet initialized."

    if index is None:
        index = _scan_to_index(workspace_root)

    files_by_category: dict[str, list[tuple[str, str]]] = defaultdict(list)
    total_files = 0

    for rel in sorted(index):
        meta = index[rel]
        parts = rel.rstrip("/").split("/")
        if parts[0] in _MAP_SKIP_DIRS:
            continue
        # Skip very deep paths (>4 levels)
        if len(parts) > 4:
            continue
        category = str(meta.get("category", "other"))
        if category == "state":
            continue  # Don't list state artifacts in the map
        files_by_category[category].append((rel, str(meta.get("label", ""))))
        total_files += 1

    # Group by readable section name
    section_order = [
        "governance",
        "identity",
        "memory:curated",
        "memory:daily",
        "skill",
        "skill:auto",
        "project",
        "knowledge",
        "reference",
        "extension:prompt",
        "system:prompt",
        "heartbeat",
        "document",
        "data",
        "config",
        "code",
        "script",
        "media",
        "other",
    ]

    lines: list[str] = [f"## Workspace ({total_files} files)\n"]

    for section in section_order:
        entries = files_by_category.get(section, [])
        if not entries:
            continue

        emoji = category_emoji(section)
        human_name = section.replace(":", " / ").replace("_", " ").title()
        lines.append(f"\n### {emoji} {human_name} ({len(entries)} files)")

        # Show individual files (cap at 10 per section to avoid bloat)
        for rel, label in entries[:10]:
            lines.append(f"- {rel} — {label}")

        if len(entries) > 10:
            lines.append(f"- ... and {len(entries) - 10} more")

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n\n[workspace map truncated]"
    return result


def save_workspace_map(workspace_root: Path, *, index: dict[str, dict] | None = None) -> Path:
    """Generate and save the workspace map to .olano/workspace_map.md."""
    state_dir = workspace_root / WORKSPACE_STATE_DIRNAME
    state_dir.mkdir(parents=True, exist_ok=True)
    map_path = state_dir / "workspace_map.md"
    content = generate_workspace_map(workspace_root, index=index)
    map_path.write_text(content, encoding="utf-8")
    logger.debug("Workspace map written to %s (%d chars)", map_path, len(content))
    return map_path
