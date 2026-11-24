"""File classification for workspace cartography."""

from __future__ import annotations

from pathlib import Path

from olano.config.workspace import LEGACY_WORKSPACE_STATE_DIRNAMES, WORKSPACE_STATE_DIRNAME

# (glob_pattern, category, label)
_RULES: list[tuple[str, str, str]] = [
    # Governance
    ("AGENTS.md", "governance", "Agent operating instructions"),
    ("SOUL.md", "identity", "Personality, tone, creative spirit"),
    ("USER.md", "identity", "User profile and preferences"),
    ("IDENTITY.md", "identity", "Agent identity and name"),
    ("TOOLS.md", "reference", "Tool notes and usage"),
    ("SAFETY_BOUNDARIES.md", "governance", "Safety rules and hard limits"),
    # Memory
    ("MEMORY.md", "memory:curated", "Long-term curated knowledge"),
    ("memories/*.md", "memory:daily", "Daily session log"),
    ("memories/heartbeat-state.json", "state", "Heartbeat state"),
    # Skills — learned rule first so folder-form learned skills get skill:auto, not
    # the generic skill (fnmatch '*' crosses '/', so skills/*/SKILL.md would match).
    ("skills/learned/*/SKILL.md", "skill:auto", "Auto-learned skill"),
    ("skills/*/SKILL.md", "skill", "Agent skill definition"),
    # Projects
    ("projects/*.md", "project", "Project notes"),
    ("projects/**/*.md", "project", "Project sub-document"),
    # Knowledge base
    ("knowledgebase/**/*", "knowledge", "Knowledge base document"),
    # System zone
    ("system/prompts/*.md", "system:prompt", "System-managed prompt override"),
    ("system/middleware/*.py", "system:middleware", "System middleware"),
    ("system/skills/*/SKILL.md", "system:skill", "System-managed skill"),
    # Extensions zone
    ("extensions/prompts/*.md", "extension:prompt", "User prompt override"),
    # State (the current folder name and the pre-rename one, which is a link
    # to it on upgraded workspaces)
    *(
        (f"{name}/**/*", "state", "Generated state artifact")
        for name in (WORKSPACE_STATE_DIRNAME, *LEGACY_WORKSPACE_STATE_DIRNAMES)
    ),
    # Output conventions (fnmatch '*' crosses '/', so one rule covers subfolders)
    ("reports/*", "deliverable", "Deliverable (report, chart, export)"),
    ("scratch/*", "scratch", "Scratch / intermediate file"),
    ("uploads/*", "upload", "Received in chat"),
    # Workspace root
    ("HEARTBEAT.md", "heartbeat", "Heartbeat task queue"),
    ("OPENCLAW.md", "reference", "OpenClaw compatibility notes"),
]

# MIME-like category for unknown file types
_EXTENSION_CATEGORIES: dict[str, str] = {
    ".md": "document",
    ".txt": "document",
    ".pdf": "document",
    ".docx": "document",
    ".json": "data",
    ".yaml": "config",
    ".yml": "config",
    ".toml": "config",
    ".py": "code",
    ".js": "code",
    ".ts": "code",
    ".sh": "script",
    ".bash": "script",
    ".csv": "data",
    ".png": "media",
    ".jpg": "media",
    ".jpeg": "media",
    ".gif": "media",
    ".mp4": "media",
    ".mp3": "media",
}


def classify_file(path: Path, workspace_root: Path) -> tuple[str, str]:
    """
    Return (category, label) for the given file relative to workspace_root.
    Falls back to extension-based category.
    """
    try:
        rel = str(path.relative_to(workspace_root))
    except ValueError:
        rel = path.name

    # Exact name matches first
    for pattern, category, label in _RULES:
        if "*" not in pattern and "**" not in pattern and (rel == pattern or path.name == pattern):
            return category, label

    # Glob-style pattern matching
    from fnmatch import fnmatch

    for pattern, category, label in _RULES:
        if fnmatch(rel, pattern):
            return category, label

    # Extension fallback
    ext = path.suffix.lower()
    cat = _EXTENSION_CATEGORIES.get(ext, "other")
    return cat, path.name


def category_emoji(category: str) -> str:
    """Return a display emoji for a file category."""
    return {
        "governance": "📋",
        "identity": "🪪",
        "memory:curated": "🧠",
        "memory:daily": "📓",
        "skill": "⚡",
        "skill:auto": "🤖",
        "project": "📁",
        "knowledge": "📚",
        "system:prompt": "⚙️",
        "system:middleware": "🔧",
        "system:skill": "⚙️",
        "extension:prompt": "🎨",
        "reference": "📖",
        "state": "💾",
        "heartbeat": "💓",
        "deliverable": "📦",
        "scratch": "🧪",
        "upload": "📥",
        "document": "📄",
        "data": "📊",
        "config": "⚙️",
        "code": "💻",
        "script": "🖥️",
        "media": "🎬",
    }.get(category, "📄")
