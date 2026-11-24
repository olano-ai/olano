"""Snapshot + commit engine for home revision tracking.

One snapshot = enumerate allowlisted files → copy them (redacting
``agent.json5`` ``env`` values) into a temp tree → ``git add -A`` / commit
*against the temp tree* with the acting agent as author. The bare repo at
``<home>/revisions.git`` never has a work tree inside the live home, so no
git operation can ever sweep in a file the allowlist didn't name.

All functions are synchronous and safe to call from a thread; the debounce /
single-writer discipline lives in :mod:`olano.revisions.worker`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from olano.cartography.walker import _INSTALL_TREE_MARKERS, SKIP_DIR_NAMES

logger = logging.getLogger(__name__)

# ── what history tracks ──────────────────────────────────────────────────────
#
# STILL AN ALLOWLIST, never a denylist: a path is versioned only because it is
# one of the named home files, an agent's config, or a file of an allowed TYPE
# inside one of the named TREES. Adding a type or a tree is a security
# decision (everything here can be pushed to GitHub), so it belongs in its own
# reviewed commit. Never add databases, tokens, ``.env``, ``vault/``,
# ``platforms/``, ``users/``, ``handshake/``, sessions or logs: none of them is
# in a tree below or has an allowed extension.

# Home-level files, by exact name.
HOME_FILES: tuple[str, ...] = ("config.yaml", "organization.yaml", "approval_rules.yaml")
_AGENT_CONFIG_RE = re.compile(r"agents/[^/]+/agent\.json5\Z")

# Trees whose files are tracked by type. The legacy shared workspace, every
# agent's workspace (identity files, memory, skills, scripts, the documents
# and media agents produce or are given) and the home's installed skills.
_TREE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"workspace/"),
    re.compile(r"agents/[^/]+/workspace/"),
    re.compile(r"skills/"),
)

# Allowed types, split by whether the content is text (scanned for secrets
# before it may enter history) or binary (stored byte for byte).
TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        # notes and documents
        ".md", ".markdown", ".txt", ".rst", ".csv", ".tsv", ".html", ".htm", ".xml",
        # data and configuration
        ".json", ".json5", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
        # scripts and source code
        ".py", ".ipynb", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
        ".css", ".scss", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".rb", ".go", ".rs",
        ".java", ".kt", ".kts", ".scala", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp",
        ".cs", ".php", ".pl", ".lua", ".r", ".jl", ".sql", ".graphql", ".proto",
        ".dart", ".ex", ".exs", ".svg",
    }
)  # fmt: skip
BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        # documents
        ".pdf", ".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods",
        ".ppt", ".pptx", ".odp", ".epub",
        # audio and video
        ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".flac", ".opus",
        ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi",
        # images
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".ico",
    }
)  # fmt: skip
TRACKED_EXTENSIONS: frozenset[str] = TEXT_EXTENSIONS | BINARY_EXTENSIONS

# Directory names never descended into, wherever they appear in a tree: the
# workspace map's own list (dependency trees, caches, the workspace state
# folder, a customer-service agent's private per-customer memory, the vector
# store), plus any hidden directory.

# A config/data file whose NAME says it holds a credential is never tracked,
# whatever it contains (client_secret.json, token.json, credentials.yaml).
_SECRET_NAME_RE = re.compile(r"secret|token|credential|password|apikey|api_key", re.IGNORECASE)
_SECRET_NAME_EXTS = frozenset(
    {".json", ".json5", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".txt"}
)


# With revisions.include_memory=false (customers with deletion obligations —
# memory files carry distilled personal data), any matched file that IS a
# memory file is dropped: MEMORY.md itself, or anything under a memories/
# directory.
def _is_memory_file(rel: Path) -> bool:
    return rel.name == "MEMORY.md" or "memories" in rel.parts


def is_tracked_path(rel: str, *, include_memory: bool = True) -> bool:
    """True when history tracks ``rel`` (a home-relative posix path), by name.

    The one definition both directions use: the snapshot's walk filters with
    it, and a pull refuses to write any path it rejects, so GitHub can never
    put a file on this box that a snapshot would not have taken. (The walk
    also prunes directories by marker file, which a name cannot show.)
    """
    if not rel or "\\" in rel or "\0" in rel or rel.startswith("/"):
        return False
    parts = rel.split("/")
    if any(part in ("", ".", "..") or part.startswith(".") for part in parts):
        return False
    if not include_memory and _is_memory_file(Path(rel)):
        return False
    if rel in HOME_FILES or _AGENT_CONFIG_RE.match(rel):
        return True
    if not any(rx.match(rel) for rx in _TREE_RES):
        return False
    if any(part in SKIP_DIR_NAMES for part in parts[:-1]):
        return False
    name = parts[-1]
    ext = Path(name).suffix.lower()
    if ext not in TRACKED_EXTENSIONS:
        return False
    return not (ext in _SECRET_NAME_EXTS and _SECRET_NAME_RE.search(name))


# High-confidence credential shapes. A text file matching one is HELD BACK
# (its last clean version stays in history) instead of being committed:
# history is pushed to GitHub, and GitHub push protection would reject the
# push anyway. Deliberately narrow, so ordinary prose and code never trip it.
_SECRET_CONTENT_RE = re.compile(
    rb"(?:"
    rb"\bsk-(?:ant-|proj-|live-)?[A-Za-z0-9_-]{20,}"  # OpenAI / Anthropic style keys
    rb"|\bgh[pousr]_[A-Za-z0-9]{30,}"  # GitHub tokens
    rb"|\bgithub_pat_[A-Za-z0-9_]{40,}"
    rb"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"  # AWS access key ids
    rb"|\bxox[abposr]-[A-Za-z0-9-]{10,}"  # Slack
    rb"|\bAIza[0-9A-Za-z_-]{35}"  # Google API keys
    rb"|\b[rs]k_live_[0-9A-Za-z]{20,}"  # Stripe live keys
    rb"|-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY"
    rb"|\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?)://[^\s:@/]+:[^\s@/]+@"
    rb")"
)


def contains_secret(data: bytes) -> bool:
    return _SECRET_CONTENT_RE.search(data) is not None


# Why a tracked file's current version is not in the newest commit.
HELD_TOO_LARGE = "too_large"
HELD_SECRET = "secret"
HELD_UNPARSEABLE = "unparseable"


SYSTEM_AUTHOR = "olano"
_AUTHOR_DOMAIN = "agents.local"
_SYSTEM_EMAIL = "system@olano.local"
_GIT_TIMEOUT = 120
# Network and big-object operations (a push of a large history, hashing a
# 100 MB video) get longer.
_GIT_LONG_TIMEOUT = 1800
_CACHE_FILE = "olano-stat-cache.json"
# GitHub refuses any file larger than 100 MiB, so that is the ceiling; the
# default stays under its 50 MiB warning (see RevisionsSettings.max_file_kb).
GITHUB_MAX_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_FILE_KB = 50 * 1024
# A lock file older than this is provably stale: no legitimate git operation
# we run outlives _GIT_TIMEOUT, and we are the repo's single writer.
_STALE_LOCK_SECONDS = 600
# git's well-known ids: the empty tree (the "parent" of a root commit when
# diffing it) and the all-zero id (update-ref's "must not exist yet").
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_ZERO_OID = "0" * 40
# A rev from a request is passed to git as an argument, so it must never be
# able to read as an option: hex ids, HEAD, tags and remote refs only.
_REV_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/^~-]{0,199}\Z")
# A diff the dashboard renders: past this the patch is cut (the files we
# track are capped at max_file_kb, but a full-context diff of two is double).
_MAX_PATCH_CHARS = 2_000_000


def _safe_rev(rev: str) -> str:
    rev = (rev or "").strip()
    if not _REV_RE.match(rev) or ".." in rev:
        raise ValueError(f"invalid revision: {rev!r}")
    return rev


def _author_env(author: str) -> dict[str, str]:
    author = author or SYSTEM_AUTHOR
    email = _SYSTEM_EMAIL if author == SYSTEM_AUTHOR else f"{author}@{_AUTHOR_DOMAIN}"
    return {
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": SYSTEM_AUTHOR,
        "GIT_COMMITTER_EMAIL": _SYSTEM_EMAIL,
    }


@dataclass
class SnapshotResult:
    committed: bool
    commit: str = ""
    tag: str = ""
    files: int = 0
    skipped_large: list[str] = field(default_factory=list)
    held_back: list[dict[str, str]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "committed": self.committed,
            "commit": self.commit,
            "tag": self.tag,
            "files": self.files,
            "skipped_large": self.skipped_large,
            "held_back": self.held_back,
            "error": self.error,
        }


def _redact_agent_env(raw: str) -> tuple[str, bool]:
    """Redact ``env`` values in an agent.json5 text.

    Returns ``(text, redacted)``. When the env block is empty or absent the
    original bytes pass through untouched (diff fidelity for the common
    case). When values exist, the file is re-serialized as JSON5-compatible
    JSON with every env value replaced by ``"__vault__"`` — normalized
    formatting is the accepted price of keeping raw keys out of history.
    Unparseable files are treated as secret-bearing and skipped entirely
    (fail closed).
    """
    try:
        from olano.config.loader import _load_json5

        data = _load_json5(raw)
    except Exception:
        return "", True  # can't prove it's safe — skip the file
    env = data.get("env") if isinstance(data, dict) else None
    if not isinstance(env, dict) or not env:
        return raw, False
    data["env"] = {key: "__vault__" for key in env}
    header = "// env values redacted by olano revisions — live values stay in the local file\n"
    return header + json.dumps(data, indent=2, ensure_ascii=False) + "\n", True


class RevisionsEngine:
    def __init__(
        self,
        home: Path,
        *,
        include_memory: bool = True,
        max_file_kb: int = DEFAULT_MAX_FILE_KB,
    ) -> None:
        self.home = home
        self.repo = home / "revisions.git"
        self.include_memory = include_memory
        self.max_file_bytes = min(max(1, int(max_file_kb)) * 1024, GITHUB_MAX_FILE_BYTES)

    # ── git plumbing ───────────────────────────────────────────────────

    def _git(
        self,
        *args: str,
        work_tree: Path | None = None,
        env: dict | None = None,
        input: str | None = None,
    ) -> str:
        proc = self._git_proc(*args, work_tree=work_tree, env=env, input=input)
        if proc.returncode != 0:
            raise RuntimeError(f"git {args[0]}: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout

    def _git_proc(
        self,
        *args: str,
        work_tree: Path | None = None,
        env: dict | None = None,
        input: str | None = None,
        timeout: float = _GIT_TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        """Run git and return the process, whatever its exit code.

        For the commands whose non-zero exit is an answer rather than a
        failure (``merge-base --is-ancestor``, ``merge-tree``).
        """
        cmd = ["git", f"--git-dir={self.repo}"]
        if work_tree is not None:
            cmd.append(f"--work-tree={work_tree}")
        cmd.extend(args)
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(env or {})},
            input=input,
        )

    def ensure_repo(self) -> None:
        if not (self.repo / "HEAD").exists():
            self.repo.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "init", "--bare", "--initial-branch=main", str(self.repo)],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
                check=True,
            )

    @staticmethod
    def available() -> bool:
        return shutil.which("git") is not None

    def _clear_stale_locks(self) -> None:
        """Remove git lock files no live writer can own.

        A hard kill mid-commit leaves ``index.lock`` (or a ref lock) behind,
        and git never cleans those up itself — every later commit would fail
        until a human deletes the file, which is exactly the git surgery
        this feature promises users never do. We are the single writer by
        design (worker commit lock; one runtime per home), so a lock older
        than :data:`_STALE_LOCK_SECONDS` (≫ ``_GIT_TIMEOUT``) is provably
        stale. Fresh locks are respected: a genuinely concurrent process
        keeps winning and our snapshot fails clean and retries.
        """
        cutoff = time.time() - _STALE_LOCK_SECONDS
        candidates = [self.repo / "index.lock", self.repo / "HEAD.lock"]
        heads = self.repo / "refs" / "heads"
        if heads.is_dir():
            candidates.extend(heads.glob("*.lock"))
        for lock in candidates:
            try:
                if lock.is_file() and lock.stat().st_mtime < cutoff:
                    lock.unlink()
                    logger.warning("revisions: removed stale git lock %s", lock.name)
            except OSError:
                pass

    # ── snapshot ───────────────────────────────────────────────────────
    #
    # Nothing is copied: each tracked file is hashed straight into the object
    # store by NAME (``git hash-object -w -- <path>``, so git still never
    # walks the live home), the tree is built from those ids in the repo's own
    # index, and a stat cache (size, mtime, inode -> blob id) means a file
    # that has not changed is not even read again. With 100 MB videos in a
    # workspace, copying everything into a temp tree on every snapshot is
    # what this replaced.

    def _git_bytes(self, *args: str, input: bytes | None = None, env: dict | None = None) -> bytes:
        proc = subprocess.run(
            ["git", f"--git-dir={self.repo}", *args],
            capture_output=True,
            timeout=_GIT_LONG_TIMEOUT,
            env={**os.environ, **(env or {})},
            input=input,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"git {args[0]}: {err}")
        return proc.stdout

    def _walk_tree(self, base: Path) -> Iterator[Path]:
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            here = Path(dirpath)
            keep = []
            for name in dirnames:
                full = here / name
                if name.startswith(".") or name in SKIP_DIR_NAMES or full.is_symlink():
                    continue
                try:
                    inner = set(os.listdir(full))
                except OSError:
                    continue
                if ".git" in inner or not inner.isdisjoint(_INSTALL_TREE_MARKERS):
                    continue  # an embedded repo, a virtualenv, an npm/pnpm tree
                keep.append(name)
            dirnames[:] = sorted(keep)
            for name in sorted(filenames):
                yield here / name

    def _enumerate(self) -> tuple[list[tuple[str, Path, os.stat_result]], list[str]]:
        """Tracked files ``(rel, path, stat)`` + relpaths over the size cap."""
        candidates: list[Path] = [self.home / name for name in HOME_FILES]
        agents = self.home / "agents"
        if agents.is_dir():
            for agent_dir in sorted(agents.iterdir()):
                candidates.append(agent_dir / "agent.json5")
                if (agent_dir / "workspace").is_dir() and not agent_dir.is_symlink():
                    candidates.extend(self._walk_tree(agent_dir / "workspace"))
        for tree in ("workspace", "skills"):
            base = self.home / tree
            if base.is_dir() and not base.is_symlink():
                candidates.extend(self._walk_tree(base))
        files: list[tuple[str, Path, os.stat_result]] = []
        too_large: list[str] = []
        seen: set[str] = set()
        for path in candidates:
            try:
                if path.is_symlink() or not path.is_file():
                    continue  # never follow links: they can point anywhere
                rel = path.relative_to(self.home).as_posix()
                st = path.stat()
            except (OSError, ValueError):
                continue
            if rel in seen or not is_tracked_path(rel, include_memory=self.include_memory):
                continue
            seen.add(rel)
            if st.st_size > self.max_file_bytes:
                too_large.append(rel)
                continue
            files.append((rel, path, st))
        return files, too_large

    def _hash_file(self, rel: str, path: Path, *, write: bool = True) -> tuple[str, str]:
        """``(blob id, held reason)`` for one live file; reason "" when clean."""
        w = ["-w"] if write else []
        ext = Path(rel).suffix.lower()
        if rel.endswith("/agent.json5") or ext in TEXT_EXTENSIONS:
            data = path.read_bytes()
            if rel.endswith("/agent.json5"):
                text, redacted = _redact_agent_env(data.decode("utf-8", "replace"))
                if redacted and not text:
                    return "", HELD_UNPARSEABLE
                data = text.encode("utf-8")
            if contains_secret(data):
                return "", HELD_SECRET
            oid = self._git_bytes("hash-object", *w, "--no-filters", "--stdin", input=data)
        else:
            oid = self._git_bytes("hash-object", *w, "--no-filters", "--", str(path))
        return oid.decode().strip(), ""

    def _head_entries(self) -> dict[str, tuple[str, str]]:
        """``{path: (mode, blob id)}`` of the current commit."""
        if not self._head():
            return {}
        out = self._git_bytes("ls-tree", "-r", "-z", "HEAD")
        entries: dict[str, tuple[str, str]] = {}
        for record in out.split(b"\0"):
            if not record:
                continue
            meta, _, name = record.partition(b"\t")
            mode, _type, oid = meta.decode().split()
            entries[name.decode("utf-8", "surrogateescape")] = (mode, oid)
        return entries

    def _build_tree(self) -> tuple[str, int, list[dict[str, str]], list[str]]:
        """Hash the live allowlist into a tree.

        Returns ``(tree id, files staged, held back, too large)``. A held-back
        file (over the cap, carrying a secret, an unparseable agent.json5)
        keeps its last recorded version: leaving it out would record it as
        *deleted*, and a push would then delete it from GitHub too.
        """
        self.ensure_repo()
        self._clear_stale_locks()
        files, too_large = self._enumerate()
        cache_path = self.repo / _CACHE_FILE
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(cache, dict):
                cache = {}
        except (OSError, ValueError):
            cache = {}
        fresh: dict[str, list] = {}
        entries: dict[str, tuple[str, str]] = {}
        held: list[dict[str, str]] = [{"path": r, "reason": HELD_TOO_LARGE} for r in too_large]
        for rel, path, st in files:
            key = [st.st_size, st.st_mtime_ns, st.st_ino]
            cached = cache.get(rel)
            if isinstance(cached, list) and len(cached) == 5 and cached[:3] == key:
                oid, reason = cached[3], cached[4]
            else:
                try:
                    oid, reason = self._hash_file(rel, path)
                except OSError:
                    continue
            fresh[rel] = [*key, oid, reason]
            if reason:
                held.append({"path": rel, "reason": reason})
                continue
            mode = "100755" if st.st_mode & 0o111 else "100644"
            entries[rel] = (mode, oid)
        if held:
            previous = self._head_entries()
            for item in held:
                if item["path"] in previous:
                    entries[item["path"]] = previous[item["path"]]
        for item in held:
            logger.warning("revisions: holding back %s (%s)", item["path"], item["reason"])
        tmp = cache_path.with_name(f".{_CACHE_FILE}.tmp")
        tmp.write_text(json.dumps(fresh), encoding="utf-8")
        tmp.replace(cache_path)

        self._git("read-tree", "--empty")
        if entries:
            info = b"".join(
                f"{mode} {oid}\t{rel}".encode("utf-8", "surrogateescape") + b"\0"
                for rel, (mode, oid) in sorted(entries.items())
            )
            self._git_bytes("update-index", "--add", "-z", "--index-info", input=info)
        tree = self._git("write-tree").strip()
        staged = len(files) - sum(1 for h in held if h["reason"] != HELD_TOO_LARGE)
        return tree, staged, held, too_large

    def snapshot(
        self,
        *,
        author: str = SYSTEM_AUTHOR,
        reason: str = "update",
        tag: str = "",
    ) -> SnapshotResult:
        """Record the live allowlist as a new commit if anything changed."""
        if not self.available():
            return SnapshotResult(committed=False, error="git not installed")
        try:
            tree, staged, held, too_large = self._build_tree()
            result = self._commit(tree, author=author, reason=reason, tag=tag)
            result.files = staged
            result.skipped_large = too_large
            result.held_back = held
            return result
        except Exception as exc:
            logger.warning("revisions: snapshot failed: %s", exc)
            return SnapshotResult(committed=False, error=str(exc))

    def held_back(self) -> list[dict[str, str]]:
        """Tracked files whose current version is not being recorded, and why
        (``too_large``, ``secret``, ``unparseable``), for the dashboard."""
        files, too_large = self._enumerate()
        held = [{"path": r, "reason": HELD_TOO_LARGE} for r in too_large]
        try:
            cache = json.loads((self.repo / _CACHE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        for rel, _path, st in files:
            cached = cache.get(rel) if isinstance(cache, dict) else None
            key = [st.st_size, st.st_mtime_ns, st.st_ino]
            if isinstance(cached, list) and len(cached) == 5 and cached[:3] == key and cached[4]:
                held.append({"path": rel, "reason": cached[4]})
        return held

    def live_oid(self, rel: str) -> str | None:
        """The blob id a snapshot taken now would record for ``rel``; None
        when it would record nothing new (absent, untracked, a link, held)."""
        if not is_tracked_path(rel, include_memory=self.include_memory):
            return None
        path = self.home / rel
        try:
            if path.is_symlink() or not path.is_file():
                return None
            if path.stat().st_size > self.max_file_bytes:
                return None
            oid, reason = self._hash_file(rel, path, write=False)
        except OSError:
            return None
        return None if reason else oid

    def stored_oid(self, rel: str, rev: str) -> str | None:
        proc = self._git_proc("rev-parse", "--verify", "--quiet", f"{_safe_rev(rev)}:{rel}")
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def snapshot_tree(self) -> str:
        """Hash the live allowlist and return its tree id, without committing.

        Callers hold the writer lock; the remote sync builds its commits on
        this so the tree it records is exactly what the next snapshot would.
        """
        return self._build_tree()[0]

    def commit_tree(
        self, tree: str, parents: list[str], message: str, *, author: str = SYSTEM_AUTHOR
    ) -> str:
        """Create a commit object for ``tree`` (does not move any ref)."""
        args = ["commit-tree", tree]
        for parent in parents:
            args.extend(["-p", parent])
        args.extend(["-m", message])
        return self._git(*args, env=_author_env(author)).strip()

    def advance_head(self, new: str, old: str) -> None:
        """Move the branch to ``new``, refusing if it is no longer at ``old``."""
        self._git("update-ref", "HEAD", new, old or _ZERO_OID)

    def tree_of(self, rev: str) -> str:
        return self._git("rev-parse", f"{_safe_rev(rev)}^{{tree}}").strip()

    def _commit(self, tree: str, *, author: str, reason: str, tag: str) -> SnapshotResult:
        head = self._head()
        if (head and self.tree_of(head) == tree) or (not head and tree == EMPTY_TREE):
            # Still allow tagging the current head (pre-upgrade on a clean tree).
            if tag and head:
                self._tag(tag)
                return SnapshotResult(committed=False, tag=tag, commit=head)
            return SnapshotResult(committed=False)
        commit = self.commit_tree(tree, [head] if head else [], reason, author=author)
        self.advance_head(commit, head)
        if tag:
            self._tag(tag)
        return SnapshotResult(committed=True, commit=commit, tag=tag)

    def _head(self) -> str:
        try:
            return self._git("rev-parse", "--verify", "--quiet", "HEAD").strip()
        except Exception:
            return ""

    def _tag(self, tag: str) -> None:
        try:
            self._git("tag", tag)
        except RuntimeError:
            # Tag exists — make it unique rather than fail the snapshot.
            self._git("tag", f"{tag}-{self._head()[:7]}")

    # ── reads (API surface) ────────────────────────────────────────────

    def log(self, path: str = "", limit: int = 50, skip: int = 0) -> list[dict]:
        if not (self.repo / "HEAD").exists():
            return []
        args = [
            "log",
            f"--max-count={max(1, min(int(limit), 500))}",
            f"--skip={max(0, int(skip))}",
            "--pretty=format:%H%x1f%an%x1f%aI%x1f%s%x1f%P",
        ]
        if path:
            args.extend(["--", path])
        try:
            out = self._git(*args)
        except Exception:
            return []
        entries = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 5:
                entries.append(
                    {
                        "commit": parts[0],
                        "author": parts[1],
                        "ts": parts[2],
                        "message": parts[3],
                        "parents": parts[4].split(),
                    }
                )
        return entries

    def count(self, path: str = "") -> int:
        """How many commits :meth:`log` can page through (for one file, when given)."""
        if not self._head():
            return 0
        args = ["rev-list", "--count", "HEAD"]
        if path:
            args.extend(["--", path])
        try:
            return int(self._git(*args).strip() or 0)
        except Exception:
            return 0

    def show_file(self, path: str, rev: str = "HEAD") -> str | None:
        """File content at a revision, or None when absent. Path is validated
        against the repo (git errors on escapes), never resolved on disk."""
        try:
            return self._git("show", f"{_safe_rev(rev)}:{path}")
        except Exception:
            return None

    def show_blob(self, path: str, rev: str = "HEAD") -> bytes | None:
        """A file's exact bytes at a revision (documents, media), or None."""
        try:
            return self._git_bytes("show", f"{_safe_rev(rev)}:{path}")
        except Exception:
            return None

    def resolve(self, rev: str) -> str | None:
        """Full commit id for ``rev``, or None when it does not name a commit.
        Raises ValueError for a rev that is not even shaped like one."""
        rev = _safe_rev(rev)
        try:
            return self._git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").strip()
        except Exception:
            return None

    def commit_details(self, rev: str) -> dict | None:
        """One commit: who, when, why, and what it changed file by file.

        Changes are against the **first parent** (the empty tree for the first
        commit): for a commit made by pulling from a remote that is "what this
        commit changed on this box", which is the question the page asks.
        Renames are not detected; a moved file reads as deleted + added, which
        is what the snapshot model does to it anyway.
        """
        commit = self.resolve(rev)
        if commit is None:
            return None
        meta = self._git(
            "show", "-s", "--format=%H%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%cI%x1f%B", commit
        )
        parts = meta.split("\x1f", 6)
        if len(parts) != 7:
            return None
        parents = parts[1].split()
        base = parents[0] if parents else EMPTY_TREE
        statuses: dict[str, str] = {}
        out = self._git("diff", "--no-renames", "--name-status", "-z", base, commit)
        fields = out.split("\0")
        for i in range(0, len(fields) - 1, 2):
            statuses[fields[i + 1]] = fields[i]
        files: list[dict] = []
        out = self._git("diff", "--no-renames", "--numstat", "-z", base, commit)
        for record in out.split("\0"):
            cols = record.split("\t", 2)
            if len(cols) != 3:
                continue
            added, deleted, path = cols
            binary = added == "-"
            status = {"A": "added", "D": "deleted"}.get(statuses.get(path, "M"), "modified")
            files.append(
                {
                    "path": path,
                    "status": status,
                    "additions": 0 if binary else int(added),
                    "deletions": 0 if binary else int(deleted),
                    "binary": binary,
                }
            )
        files.sort(key=lambda f: f["path"])
        return {
            "commit": parts[0],
            "parents": parents,
            "author": parts[2],
            "email": parts[3],
            "ts": parts[4],
            "committed_ts": parts[5],
            "message": parts[6].rstrip("\n"),
            "files": files,
            "stats": {
                "files": len(files),
                "added": sum(1 for f in files if f["status"] == "added"),
                "modified": sum(1 for f in files if f["status"] == "modified"),
                "deleted": sum(1 for f in files if f["status"] == "deleted"),
                "additions": sum(f["additions"] for f in files),
                "deletions": sum(f["deletions"] for f in files),
            },
        }

    def file_diff(self, rev: str, path: str, *, context: int = 3, base: str = "") -> dict | None:
        """Unified diff of one file at ``rev`` against its first parent (or
        ``base``). Plain ``git diff`` output, so any git-diff renderer reads it;
        ``context`` past the file's length shows the whole file."""
        commit = self.resolve(rev)
        if commit is None or not path:
            return None
        if base:
            old = self.resolve(base)
            if old is None:
                return None
        else:
            parents = self._git("show", "-s", "--format=%P", commit).split()
            old = parents[0] if parents else EMPTY_TREE
        context = max(0, min(int(context), 1_000_000))
        patch = self._git(
            "diff", "--no-renames", "--no-color", f"-U{context}", old, commit, "--", path
        )
        # A hunk cut in half is not a diff any renderer can parse, so an
        # oversized patch is withheld whole and the page says so.
        truncated = len(patch) > _MAX_PATCH_CHARS
        if truncated:
            patch = ""
        return {
            "commit": commit,
            "base": old,
            "path": path,
            "patch": patch,
            "binary": "\nBinary files " in patch or patch.startswith("Binary files "),
            "truncated": truncated,
        }

    def repo_size_bytes(self) -> int:
        total = 0
        try:
            for path in self.repo.rglob("*"):
                if path.is_file():
                    total += path.stat().st_size
        except OSError:
            pass
        return total

    def gc(self) -> str:
        self._git("gc", "--auto")
        return "git gc completed"


def home_engine(home: str | Path | None = None) -> RevisionsEngine:
    """An engine for the live home, configured from ``config.yaml → revisions``."""
    from olano.config.home import get_olano_paths
    from olano.config.system_settings import load_revisions_settings

    settings = load_revisions_settings()
    return RevisionsEngine(
        get_olano_paths(home).root,
        include_memory=settings.include_memory,
        max_file_kb=settings.max_file_kb,
    )


def snapshot_home(
    *,
    author: str = SYSTEM_AUTHOR,
    reason: str = "update",
    tag: str = "",
    home: str | Path | None = None,
) -> SnapshotResult:
    """One-shot snapshot with live settings — the pre-upgrade entry point."""
    from olano.config.system_settings import load_revisions_settings

    if not load_revisions_settings().enabled:
        return SnapshotResult(committed=False, error="revisions disabled")
    return home_engine(home).snapshot(author=author, reason=reason, tag=tag)
