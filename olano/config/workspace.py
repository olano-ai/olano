"""Workspace path resolution, bootstrap file management, and zone model."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

# Name of the generated-state folder inside every agent workspace
# (``Workspace.state_dir``): workspace map, memory state, shell-run logs,
# offloaded tool results and conversation history. One definition, so nothing
# else spells the folder name; a rename happens here and nowhere else.
WORKSPACE_STATE_DIRNAME = ".olano"
# What that folder was called before the rename. ``adopt_legacy_state_dir``
# moves it to the current name once per workspace and leaves a relative
# symlink behind, so a path recorded under the old name (an offloaded tool
# result cited in a checkpoint, a note in MEMORY.md) keeps resolving.
LEGACY_WORKSPACE_STATE_DIRNAMES = (".lionclaw",)
# The per-workspace ignore file cartography reads (gitignore syntax), and the
# name it had before the rename. The current one wins when both exist.
WORKSPACE_IGNORE_FILENAME = ".olanoignore"
LEGACY_WORKSPACE_IGNORE_FILENAMES = (".lionclawignore",)

BOOTSTRAP_FILES = [
    "AGENTS.md",
    "SOUL.md",
    "TOOLS.md",
    "IDENTITY.md",
    "USER.md",
    "MEMORY.md",
]

# Files that are always loaded at L0 (safety boundaries, core vibes)
L0_FILES = ["CORE_VIBES.md", "SAFETY_BOUNDARIES.md"]

# Files loaded at L1 (session startup)
L1_FILES = ["SOUL.md", "AGENTS.md", "USER.md", "MEMORY.md"]
_DIRECTORY_PREVIEW_LIMIT = 20

# Well-known workspace folders the agent should use by default.
_DOWNLOADS_README = (
    "# downloads/\n\n"
    "Default destination for files fetched from the web (e.g. `http_download`).\n"
    "When the user asks to download something without naming a destination, "
    "save it here so it's always easy and consistent to find.\n"
)
_SCRATCH_README = (
    "# scratch/\n\n"
    "Throwaway space for temporary scripts, log files, and intermediate "
    "artifacts. Nothing here is precious — clean it up freely.\n"
)
_TRASH_README = (
    "# trash/\n\n"
    "Recoverable trash can. Instead of permanently deleting files, move them "
    "here with the `trash_put` tool. Restore with `trash_restore`, or reclaim "
    "space with `trash_empty`.\n"
)
_REPORTS_README = (
    "# reports/\n\n"
    "Every file produced for people lands here: charts, images, PDFs, "
    "spreadsheets, documents, exports. One canonical file per deliverable, "
    "revised in place; `reports/<topic>/` when one job yields several files. "
    "Scripts and intermediates go to `scratch/`, never here.\n"
)
_UPLOADS_README = (
    "# uploads/\n\n"
    "Files people send in chat are saved here by the platform. Inbound only: "
    "read them, never write your own output here — deliverables belong in "
    "`reports/`.\n"
)

# Default ("scaffold") contents written for the bootstrap markdown files when a
# workspace is first initialized. Kept at module scope so it is the single source
# of truth shared by ``Workspace.init()`` (which writes them) and
# ``is_scaffold_default()`` (which detects an untouched placeholder). The prompt
# assembler uses the detector so a pristine placeholder — e.g. the default
# AGENTS.md — never shadows the agent's real configured ``system_prompt`` /
# identity files.
BOOTSTRAP_DEFAULTS: dict[str, str] = {
    "AGENTS.md": "# Agent Instructions\n\nAdd operating instructions and memory here.\n",
    "SOUL.md": "# Soul\n\nPersona, boundaries, and tone.\n",
    "TOOLS.md": "# Tools\n\nNotes on tools and usage conventions.\n",
    "IDENTITY.md": "# Identity\n\nAgent name and personality.\n",
    "USER.md": "# User Profile\n\nUser preferences and context.\n",
    "MEMORY.md": "# Memory\n\nPersistent notes and learned facts.\n",
    "CORE_VIBES.md": (
        "# Core Vibes\n\n"
        "- Be curious before certain.\n"
        "- Write important things down.\n"
        "- Prefer reversible actions and earn trust incrementally.\n"
        "- Be proactive when helpful, quiet when not.\n"
        "- Be persistent: don't stop at the first error or a hard step. If there's a "
        "plausible path to a solution, investigate it, adjust your approach, and try "
        "again before giving up or handing the problem back to the user.\n"
        "- Treat obstacles as problems to diagnose, not walls. Surface a blocker only "
        "after genuinely attempting to solve it.\n"
    ),
    "SAFETY_BOUNDARIES.md": (
        "# Safety Boundaries\n\n"
        "- Never reveal private keys, credentials, or PII in plain text.\n"
        "- Always confirm before destructive operations.\n"
        "- Respect user privacy and data sovereignty.\n"
        "- When the user shares a secret in chat, suggest "
        "`olano vault set <NAME>` (preferred) or the `secret_set` tool. "
        "Do not ask them to re-paste a value the security middleware redacted — "
        "the real value never reaches you in either case.\n"
    ),
    "TEAM.md": (
        "# Team\n\n"
        "Team members, roles, and areas of expertise.\n\n"
        "<!-- Add entries like:\n"
        "## Alice (alice@example.com)\n"
        "- Role: Backend engineer\n"
        "- Expertise: Databases, API design\n"
        "-->\n"
    ),
    "LEARNINGS.md": (
        "# Learnings\n\n"
        "Lessons learned, distilled by the self-learning loop from past sessions.\n\n"
        "<!-- Entries are added automatically by the self-learning service. -->\n"
    ),
    "IDEAS.md": (
        "# Ideas\n\n"
        "Brainstorming candidates and prototyping ideas.\n\n"
        "<!-- Add ideas here. The agent can propose moving one to a PULSE item\n"
        "     for user approval and then create a prototype plan. -->\n"
    ),
    "BRAINSTORMING.md": (
        "# Brainstorming\n\n"
        "Active brainstorming sessions and in-progress exploration.\n\n"
        "<!-- The agent keeps running notes here during open-ended exploration. -->\n"
    ),
    "PROTOTYPING.md": (
        "# Prototyping\n\n"
        "Prototypes scaffolded from promising ideas (see projects/).\n\n"
        "<!-- The prototyper worker adds an entry here per scaffold, with how to run it. -->\n"
    ),
    "CAPABILITIES.md": (
        "# Capabilities\n\n"
        "Agent self-assessment of current capabilities. Updated by self-evolution.\n\n"
        "## Known Strong Areas\n\n"
        "<!-- Self-learning populates this automatically. -->\n\n"
        "## Known Gaps\n\n"
        "<!-- Self-learning populates this automatically. -->\n"
    ),
}

# First-run marker some seeded files carry (e.g. IDENTITY.md); stripped before
# comparing so its presence/absence doesn't defeat scaffold detection.
# On-disk marker in existing workspaces — never rename (see archetypes/installer.py).
_FIRST_RUN_MARKER = "<!-- lionclaw:first-run -->\n"


def is_scaffold_default(fname: str, content: str) -> bool:
    """Return True when ``content`` is still the pristine seeded scaffold for ``fname``.

    Whitespace at the edges and the optional first-run marker are ignored so an
    untouched placeholder is recognized regardless of trailing newlines. Returns
    False for any file without a known default, or once a human/agent has edited
    it. Used by the prompt assembler so a placeholder never overrides the agent's
    real configured instructions or identity.
    """
    default = BOOTSTRAP_DEFAULTS.get(fname)
    if default is None:
        return False

    def _norm(text: str) -> str:
        return text.replace(_FIRST_RUN_MARKER, "").strip()

    return _norm(content) == _norm(default)


def adopt_legacy_state_dir(root: str | Path) -> bool:
    """Move a workspace's state folder from its pre-rename name to the current one.

    Runs on every ``Workspace`` construction and at scaffold time, and does
    nothing once the current folder exists, so it costs two stats per call.
    The move is one ``os.rename`` (atomic on one filesystem; the content and
    its ownership are untouched, which matters for a sandbox that has the
    workspace bind-mounted), followed by a relative symlink at the old name so
    every path recorded before the rename keeps resolving. Both names present
    means someone already arranged things: nothing is touched. A failed rename
    is logged and the caller carries on; refusing to boot the agent over the
    folder's name would be worse, and the old folder stays readable at the
    paths that were recorded for it.

    Returns True when a folder was moved.
    """
    base = Path(root)
    current = base / WORKSPACE_STATE_DIRNAME
    if current.exists() or current.is_symlink():
        return False
    for legacy_name in LEGACY_WORKSPACE_STATE_DIRNAMES:
        legacy = base / legacy_name
        if legacy.is_symlink() or not legacy.is_dir():
            continue
        try:
            legacy.rename(current)
        except FileNotFoundError:
            # A concurrent construction of the same workspace won the race.
            return False
        except OSError:
            logger.warning(
                "Could not move %s to %s; keeping the old folder", legacy, current, exc_info=True
            )
            return False
        try:
            legacy.symlink_to(WORKSPACE_STATE_DIRNAME)
        except OSError:
            logger.debug("No compatibility link could be left at %s", legacy, exc_info=True)
        logger.info(
            "Workspace state moved from %s to %s (a link stays at the old name)", legacy, current
        )
        return True
    return False


def workspace_ignore_file(root: str | Path) -> Path | None:
    """The ignore file cartography reads for ``root``: the current name, else the old one."""
    for name in (WORKSPACE_IGNORE_FILENAME, *LEGACY_WORKSPACE_IGNORE_FILENAMES):
        candidate = Path(root) / name
        if candidate.is_file():
            return candidate
    return None


class Workspace:
    def __init__(self, root: str | Path | None = None) -> None:
        default_root = get_olano_paths().legacy_workspace_root
        self.root = Path(root) if root else default_root
        self.root.mkdir(parents=True, exist_ok=True)
        # A pre-rename state folder is moved to the current name once; then the
        # state directory exists either way.
        adopt_legacy_state_dir(self.root)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths

    @property
    def agents_dir(self) -> Path:
        return get_olano_paths().agents_dir

    @property
    def sessions_dir(self) -> Path:
        d = get_olano_paths().sessions_dir
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def skills_dir(self) -> Path:
        d = self.root / "skills"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def learned_skills_dir(self) -> Path:
        """Self-learning skills: <workspace>/skills/learned/"""
        return self.skills_dir / "learned"

    @property
    def managed_skills_dir(self) -> Path:
        d = get_olano_paths().managed_skills_dir
        d.mkdir(parents=True, exist_ok=True)
        return d

    def agent_session_dir(self, agent_name: str) -> Path:
        d = self.sessions_dir / agent_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------------------ zones

    @property
    def state_dir(self) -> Path:
        """Generated state zone — auto-generated artifacts, never user-edited."""
        return self.root / WORKSPACE_STATE_DIRNAME

    @property
    def state_memory_dir(self) -> Path:
        d = self.state_dir / "state" / "memory"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def system_dir(self) -> Path:
        """System zone — auto-updated by server, never user-edited."""
        return self.root / "system"

    @property
    def extensions_dir(self) -> Path:
        """Extension zone — user overrides that survive system updates."""
        return self.root / "extensions"

    @property
    def workspace_map_path(self) -> Path:
        return self.state_dir / "workspace_map.md"

    @property
    def workspace_bridge_path(self) -> Path:
        return self.state_dir / "workspace_bridge.md"

    @property
    def workspace_index_path(self) -> Path:
        return self.state_dir / "workspace_index.json"

    # ---------------------------------------------------------- bootstrap

    def read_bootstrap_files(self, subagent: bool = False) -> dict[str, str]:
        """Return a dict of filename → content for bootstrap context injection."""
        files = ["AGENTS.md", "TOOLS.md"] if subagent else BOOTSTRAP_FILES
        result: dict[str, str] = {}
        for fname in files:
            path = self.root / fname
            content = self._read_path_text(path, label=fname)
            if content:
                result[fname] = content
        return result

    def read_file(self, fname: str) -> str:
        """Read a workspace file by name, returning empty string if missing."""
        path = self.root / fname
        return self._read_path_text(path, label=fname)

    def _read_path_text(self, path: Path, *, label: str) -> str:
        """Read text-like workspace content without crashing on directories or binaries."""
        if not path.exists():
            return ""
        if path.is_dir():
            return self._describe_directory(path, label=label)
        if not path.is_file():
            return f"[Path '{label}' is not a regular file.]"
        try:
            return path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            return f"[Binary file '{label}' omitted from text context.]"
        except OSError as exc:
            return f"[Could not read '{label}': {exc}]"

    def _describe_directory(self, path: Path, *, label: str) -> str:
        lines = [f"[Directory '{label}']"]
        try:
            entries = sorted(path.iterdir(), key=lambda entry: entry.name.lower())
        except OSError as exc:
            return f"[Could not list directory '{label}': {exc}]"

        shown = 0
        for entry in entries:
            if shown >= _DIRECTORY_PREVIEW_LIMIT:
                break
            try:
                suffix = "/" if entry.is_dir() else ""
            except OSError:
                suffix = ""
            lines.append(f"- {entry.name}{suffix}")
            shown += 1

        remaining = len(entries) - shown
        if remaining > 0:
            lines.append(f"- ... and {remaining} more")
        return "\n".join(lines)

    def file_exists(self, fname: str) -> bool:
        return (self.root / fname).exists()

    def build_system_prompt_context(self, base_prompt: str, subagent: bool = False) -> str:
        """Merge base system prompt with bootstrap file contents (legacy path)."""
        parts = [base_prompt] if base_prompt else []
        bootstrap = self.read_bootstrap_files(subagent=subagent)
        if bootstrap:
            parts.append("\n\n---\n## Project Context\n")
            for fname, content in bootstrap.items():
                max_chars = 20_000
                if len(content) > max_chars:
                    content = content[:max_chars] + f"\n\n[...{fname} truncated]"
                parts.append(f"\n### {fname}\n{content}")
        return "".join(parts)

    # ---------------------------------------------------------- init

    def init(self) -> None:
        """Create default bootstrap files and directory structure if they don't exist."""
        for fname, content in BOOTSTRAP_DEFAULTS.items():
            path = self.root / fname
            if not path.exists():
                path.write_text(content, encoding="utf-8")

        # Create zone directories
        for d in [
            self.root / "system" / "prompts",
            self.root / "system" / "middleware",
            self.root / "system" / "skills" / "core",
            self.root / "system" / "policies",
            self.root / "system" / "schemas",
            self.root / "system" / "knowledge",
            self.root / "extensions" / "prompts",
            self.root / "extensions" / "middleware",
            self.root / "memories",
            self.root / "skills",
            self.root / "projects",
            self.root / "downloads",
            self.root / "scratch",
            self.root / "trash",
            self.root / "reports",
            self.root / "uploads",
            self.state_dir / "state" / "workspace",
            self.state_dir / "state" / "memory" / "working",
            self.state_dir / "state" / "memory" / "episodic",
            self.state_dir / "state" / "memory" / "semantic",
            self.state_dir / "state" / "memory" / "procedural",
            self.state_dir / "state" / "memory" / "preference",
            self.state_dir / "state" / "memory" / "safety",
            self.state_dir / "state" / "memory" / "relational",
            self.state_dir / "state" / "pulse",
            self.state_dir / "state" / "scheduler",
            self.state_dir / "state" / "agents",
            self.state_dir / "state" / "tasks",
            self.state_dir / "state" / "channels",
            self.state_dir / "state" / "sessions",
            self.state_dir / "cache",
            self.state_dir / "proposals",
            self.state_dir / "learned",
            self.state_dir / "logs",
        ]:
            d.mkdir(parents=True, exist_ok=True)

        placeholders = {
            self.workspace_map_path: "# Workspace Map\n\n_No workspace map generated yet._\n",
            self.workspace_bridge_path: "# Workspace Bridge\n\n_No workspace guide generated yet._\n",
            self.root / "downloads" / "README.md": _DOWNLOADS_README,
            self.root / "scratch" / "README.md": _SCRATCH_README,
            self.root / "trash" / "README.md": _TRASH_README,
            self.root / "reports" / "README.md": _REPORTS_README,
            self.root / "uploads" / "README.md": _UPLOADS_README,
        }
        for path, content in placeholders.items():
            if not path.exists():
                path.write_text(content, encoding="utf-8")


# Module-level singleton
def get_workspace(override: str | None = None) -> Workspace:
    override_env = os.environ.get("LIONCLAW_WORKSPACE")
    if override or override_env:
        return Workspace(root=override or override_env)
    # Auto-detect: if a local workspace/ dir exists in cwd or project root, use it
    for candidate in [Path.cwd() / "workspace", Path(__file__).parents[2] / "workspace"]:
        if (candidate / "AGENTS.md").exists() or (candidate / "IDENTITY.md").exists():
            return Workspace(root=candidate)
    return Workspace(root=get_olano_paths().legacy_workspace_root)
