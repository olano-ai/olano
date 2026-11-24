"""A GitHub remote for the home revision history: connect, push, fetch, pull.

The local bare repo (``<home>/revisions.git``) stays canonical and keeps being
written only by snapshots. This module adds the one thing Contract B left
room for, a remote, and the two directions of traffic over it:

* **Push** sends local history to a GitHub repository the user created
  (empty the first time, checked before anything is sent). It is never
  forced on its own: when GitHub has commits this box does not, the push is
  refused, the state says a pull is needed, and only an explicit "force
  push" from the dashboard overwrites the remote.
* **Pull** brings GitHub's history in and applies it to the live home. It is
  the only code path that writes into live config from history, so it is
  built as a gate, not a copy:

  - every incoming path must pass :func:`is_tracked_path` (the snapshot's
    own allowlist, with ``include_memory`` applied) and be a regular file
    under the size cap; anything else is ignored and reported, never written;
  - ``agent.json5`` arrives with ``env`` values redacted to ``__vault__``,
    so the live values are merged back over it (section 3.3 of the design
    record); a key the live file does not have is dropped, never written
    as the placeholder;
  - ``config.yaml`` keeps the live ``revisions:`` block, so a pull can never
    disconnect the remote or turn history off by replaying an older file;
  - deleting a home config or an ``agent.json5`` is not applied (an agent is
    removed from the dashboard, not by a file vanishing upstream);
  - everything is parsed and validated before the first byte is written,
    and every file it would overwrite must still match the snapshot taken
    at the start, so a turn that wrote a file mid-pull aborts the pull
    instead of being clobbered.

  When both sides moved, ``git merge-tree --write-tree`` merges inside the
  bare repo (no working tree anywhere). A clean merge is applied like a
  fast-forward; a conflicted one is parked as a *pending merge* that the
  dashboard resolves file by file (keep this box's version, take GitHub's,
  or edit the merged text), and nothing touches the live home until it is
  resolved. This is the one place a user can meet a conflict, and only by
  pulling: the snapshot engine still has no merge code path.

The access token never enters the repo, the URL or the process arguments:
it rides ``http.https://github.com/.extraheader`` through ``GIT_CONFIG_*``
environment variables, scoped to github.com so a redirect elsewhere never
receives it. A pasted token lives in the vault under a runtime-reserved
name (:data:`TOKEN_VAULT_NAME`, see ``vault/reserved.py``) that no agent can
resolve; the alternative is the deployment's GitHub connection from the
Connections page. Settings live in ``config.yaml → revisions.remote`` (they
are not secret, and that file is itself versioned); sync *state* lives in
``revisions.git/olano-remote.json``, because writing it into config.yaml
would make every push produce a new commit to push.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from olano.revisions.engine import (
    _GIT_LONG_TIMEOUT,
    EMPTY_TREE,
    SYSTEM_AUTHOR,
    RevisionsEngine,
    home_engine,
    is_tracked_path,
)

logger = logging.getLogger(__name__)

TOKEN_VAULT_NAME = "OLANO_REVISIONS_REMOTE_TOKEN"
SYNC_MODES = ("manual", "daily", "on_commit")
AUTH_MODES = ("token", "connection")

_STATE_FILE = "olano-remote.json"
_MERGE_FILE = "olano-merge.json"
_OP_LOCK_FILE = "olano-sync.lock"
_DAILY_SECONDS = 24 * 3600
# After a failed automatic push, wait this long before the next unprompted try.
_RETRY_SECONDS = 15 * 60
_EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
_REGULAR_MODES = ("100644", "100755")
# Pulled deletions of these are reported and skipped, never applied.
_KEEP_ON_DELETE = re.compile(
    r"\A(?:config\.yaml|organization\.yaml|approval_rules\.yaml|agents/[^/]+/agent\.json5)\Z"
)
_YAML_FILES = ("config.yaml", "organization.yaml", "approval_rules.yaml")
_CONFLICT_MARKER = re.compile(r"^(?:<{7}|>{7})(?: |$)", re.MULTILINE)
_GITHUB_REPO = re.compile(
    r"\A(?:(?:https?://)?(?:www\.)?github\.com/|git@github\.com:)?"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9._-]{1,100})/?\Z"
)
_BRANCH = re.compile(r"\A[A-Za-z0-9._][A-Za-z0-9._/-]{0,99}\Z")

# One lock for every git writer on the repo: snapshots (the worker) and the
# remote operations here. Reentrant so a pull can snapshot under it.
WRITER_LOCK = threading.RLock()

# At most ONE remote operation (connect, push, fetch, pull, merge resolve or
# abort, disconnect, the worker's automatic push) runs at a time for a home,
# whoever started it. WRITER_LOCK alone serialized them but let them queue:
# three admins pressing Push sent the same commit to GitHub three times, each
# request waiting silently behind the others. Now a second operation is
# refused at once with who is running what (``busy``), never queued. Two
# layers, taken in this order and never the reverse of WRITER_LOCK:
#   * _OP_GATE: this process (every dashboard request and the worker);
#   * an ``flock`` on revisions.git/olano-sync.lock: any other process on the
#     same home (a second runtime started by hand). The kernel drops it when
#     the holder dies, so a crash can never leave the gate stuck.
_OP_GATE = threading.Lock()
_CURRENT_OP: dict[str, Any] = {}


class RemoteError(Exception):
    """A remote operation the user can act on; ``code`` lets the UI branch."""

    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


# ── repository + branch names ───────────────────────────────────────────────


def parse_github_repo(raw: str) -> tuple[str, str]:
    """``owner/repo`` from any of the ways people paste a GitHub repo."""
    text = (raw or "").strip()
    match = _GITHUB_REPO.match(text)
    if not match:
        raise RemoteError(
            "Enter a GitHub repository as owner/name or https://github.com/owner/name.",
            "invalid_repo",
        )
    owner, repo = match.group("owner"), match.group("repo")
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if repo in ("", ".", "..") or owner.endswith("-"):
        raise RemoteError("That is not a valid GitHub repository name.", "invalid_repo")
    return owner, repo


def github_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"


def repo_slug(url: str) -> str:
    try:
        owner, repo = parse_github_repo(url)
        return f"{owner}/{repo}"
    except RemoteError:
        return url


def _check_branch(branch: str) -> str:
    branch = (branch or "main").strip()
    if not _BRANCH.match(branch) or ".." in branch or branch.endswith((".lock", "/")):
        raise RemoteError(f"{branch!r} is not a usable branch name.", "invalid_branch")
    return branch


# ── credentials ─────────────────────────────────────────────────────────────


def _vault() -> Any:
    from olano.vault.resolver import get_vault, set_vault

    vault = get_vault()
    if vault is not None:
        return vault
    try:
        from olano.vault.runtime import unlock_vault_for_session

        result = unlock_vault_for_session(allow_prompt=False)
        if result and result.vault is not None:
            set_vault(result.vault)
            return result.vault
    except Exception:
        logger.debug("revisions remote: vault unlock failed", exc_info=True)
    return None


def store_token(token: str) -> None:
    vault = _vault()
    if vault is None:
        raise RemoteError(
            "The vault is locked, so the access token has nowhere safe to live. "
            "Unlock the vault (Vault page) and connect again.",
            "vault_locked",
        )
    vault.set(
        TOKEN_VAULT_NAME,
        token,
        description="GitHub access token the revision history pushes and pulls with",
        category="system",
    )


def forget_token() -> None:
    vault = _vault()
    if vault is None:
        return
    try:
        vault.delete(TOKEN_VAULT_NAME)
    except Exception:
        logger.debug("revisions remote: token delete failed", exc_info=True)


def _stored_token() -> str:
    vault = _vault()
    if vault is None:
        return ""
    try:
        return str(vault.get(TOKEN_VAULT_NAME, touch=False) or "")
    except Exception:
        return ""


def github_connection() -> dict[str, Any]:
    """The deployment-level GitHub connection (Connections page), if any."""
    try:
        import olano.core.connectors  # noqa: F401  (registers the providers)
        from olano.core.connectors.base import connector_auth_status
        from olano.core.connectors.registry import get_provider

        provider = get_provider("github")
        if provider is None:
            return {"available": False}
        status = connector_auth_status(provider, None)
        return {
            "available": bool(status.get("connected")),
            "account": status.get("account") or "",
        }
    except Exception:
        logger.debug("revisions remote: GitHub connection lookup failed", exc_info=True)
        return {"available": False}


def resolve_token(auth: str) -> str:
    if auth == "connection":
        try:
            import olano.core.connectors  # noqa: F401
            from olano.core.connectors.base import extract_exposed_field, get_live_record
            from olano.core.connectors.registry import get_provider

            provider = get_provider("github")
            record = get_live_record(provider, None) if provider else None
            token = extract_exposed_field(record, "access_token") if record else None
        except Exception:
            token = None
        if not token:
            raise RemoteError(
                "This deployment has no GitHub connection. Connect GitHub on the "
                "Connections page, or switch to an access token.",
                "no_connection",
            )
        return token
    token = _stored_token()
    if not token:
        raise RemoteError(
            "No GitHub access token is stored for the revision history. "
            "Disconnect and connect again with a token.",
            "no_token",
        )
    return token


def _auth_env(token: str | None) -> dict[str, str]:
    env = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            }
        )
    return env


def _explain(stderr: str, token: str | None) -> RemoteError:
    text = (stderr or "").strip()
    if token:
        text = text.replace(token, "***")
    low = text.lower()
    if "repository not found" in low or ("not found" in low and "remote" in low):
        return RemoteError(
            "GitHub says the repository does not exist, or the token cannot see it.",
            "not_found",
        )
    if any(s in low for s in ("authentication failed", "could not read username", "403", "401")):
        return RemoteError(
            "GitHub refused the credentials. Check that the token is valid and has "
            "read and write access to this repository's contents.",
            "auth",
        )
    if "could not resolve host" in low or "timed out" in low or "unable to access" in low:
        return RemoteError(f"Could not reach GitHub: {text[:300]}", "network")
    return RemoteError(text[:500] or "git failed", "git")


def github_repo_info(owner: str, repo: str, token: str) -> dict[str, Any]:
    """``GET /repos/{owner}/{repo}``: does it exist, is it private, may we push."""
    import httpx

    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "olano-revisions",
            },
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise RemoteError(f"Could not reach GitHub: {exc}", "network") from exc
    if resp.status_code == 401:
        raise RemoteError("GitHub refused the token (401). Check that it is valid.", "auth")
    if resp.status_code in (403, 404):
        raise RemoteError(
            f"GitHub cannot find {owner}/{repo} with this token. Check the name, and that "
            "the token was granted access to this repository.",
            "not_found",
        )
    if resp.status_code >= 400:
        raise RemoteError(f"GitHub answered {resp.status_code}.", "github")
    data: dict[str, Any] = resp.json()
    return data


# ── state files (inside revisions.git, never versioned) ─────────────────────


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict[str, Any] | None) -> None:
    if data is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    Path(tmp).replace(path)


def load_state(engine: RevisionsEngine) -> dict[str, Any]:
    return _read_json(engine.repo / _STATE_FILE)


def save_state(engine: RevisionsEngine, **updates: Any) -> dict[str, Any]:
    state = load_state(engine)
    state.update(updates)
    _write_json(engine.repo / _STATE_FILE, state)
    return state


def load_merge(engine: RevisionsEngine) -> dict[str, Any] | None:
    data = _read_json(engine.repo / _MERGE_FILE)
    return data or None


def _record_error(engine: RevisionsEngine, exc: RemoteError, *, op: str) -> None:
    save_state(
        engine,
        last_error=str(exc),
        last_error_code=exc.code,
        last_error_op=op,
        last_error_at=_now_iso(),
        **({"needs_pull": True} if exc.code == "rejected" else {}),
    )


def _clear_error(engine: RevisionsEngine) -> None:
    save_state(engine, last_error="", last_error_code="", last_error_op="", last_error_at="")


# ── one remote operation at a time ──────────────────────────────────────────


_OP_LABELS = {
    "connect": "connection to GitHub",
    "push": "push",
    "auto_push": "automatic push",
    "fetch": "check for updates",
    "pull": "pull",
    "merge": "merge",
    "abort": "merge abort",
    "disconnect": "disconnect",
}


def current_operation() -> dict[str, Any] | None:
    """The remote operation running in this process, for the dashboard."""
    op = dict(_CURRENT_OP)
    return op or None


# What each operation does, in order. The dashboard draws one row per step
# and a bar for the step under way; ``percent`` is that step's own progress
# (git's for an upload or download), or absent while it cannot be measured.
_OP_STEPS: dict[str, list[str]] = {
    "connect": ["check", "snapshot", "upload", "save"],
    "push": ["upload"],
    "auto_push": ["upload"],
    "fetch": ["download"],
    "pull": ["snapshot", "download", "apply"],
    "merge": ["apply"],
}


def report_progress(
    step: str,
    *,
    percent: float | None = None,
    phase: str = "",
    done: int | None = None,
    total: int | None = None,
    transfer: str = "",
) -> None:
    """Record where the running operation is; a no-op when none is running.

    The record is replaced, never edited in place, so the status request that
    reads it from another thread always sees one consistent value.
    """
    if not _CURRENT_OP:
        return
    progress: dict[str, Any] = {
        "steps": list((_CURRENT_OP.get("progress") or {}).get("steps") or []),
        "step": step,
        "percent": None if percent is None else round(max(0.0, min(percent, 100.0)), 1),
    }
    if phase:
        progress["phase"] = phase
    if done is not None and total is not None:
        progress["done"], progress["total"] = done, total
    if transfer:
        progress["transfer"] = transfer
    _CURRENT_OP["progress"] = progress


# git's own progress lines, e.g. "Writing objects:  37% (370/1000), 12.00 MiB |
# 3.00 MiB/s" (stderr, one per carriage return). ``--progress`` asks for them
# even though stderr is a pipe; LC_ALL=C keeps the phase names in English.
_GIT_PROGRESS = re.compile(
    r"^(?P<phase>(?:remote: )?[A-Za-z][A-Za-z ]+?):\s+(?P<pct>\d{1,3})%\s+"
    r"\((?P<done>\d+)/(?P<total>\d+)\)(?:,\s*(?P<rest>.*?))?(?:,\s*done\.?)?\s*$"
)
# Where each git phase sits inside its step, as a share of the step's bar.
_UPLOAD_PHASES = {
    "Enumerating objects": (0, 2),
    "Counting objects": (2, 5),
    "Compressing objects": (5, 25),
    "Writing objects": (25, 97),
    "remote: Resolving deltas": (97, 100),
}
_DOWNLOAD_PHASES = {
    "remote: Enumerating objects": (0, 2),
    "remote: Counting objects": (2, 5),
    "remote: Compressing objects": (5, 12),
    "Receiving objects": (12, 92),
    "Resolving deltas": (92, 100),
}
_PHASE_KEYS = {
    "Enumerating objects": "counting",
    "Counting objects": "counting",
    "Compressing objects": "compressing",
    "Writing objects": "writing",
    "Receiving objects": "receiving",
    "Resolving deltas": "resolving",
}


def _progress_reader(step: str, phases: dict[str, tuple[int, int]]) -> Any:
    """A line handler turning git's progress lines into :func:`report_progress`."""
    last = [0.0]

    def on_line(line: str) -> None:
        match = _GIT_PROGRESS.match(line.strip())
        if not match:
            return
        name = match.group("phase").strip()
        span = phases.get(name)
        if span is None:
            return
        pct = int(match.group("pct"))
        now = time.monotonic()
        if pct < 100 and now - last[0] < 0.25:
            return  # git repaints many times a second; the dashboard polls once
        last[0] = now
        lo, hi = span
        rest = (match.group("rest") or "").strip().rstrip(",")
        report_progress(
            step,
            percent=lo + (hi - lo) * pct / 100,
            phase=_PHASE_KEYS.get(name.removeprefix("remote: "), "working"),
            done=int(match.group("done")),
            total=int(match.group("total")),
            transfer=rest if "/s" in rest or "iB" in rest else "",
        )

    return on_line


def _busy(op: dict[str, Any] | None) -> RemoteError:
    if op:
        label = _OP_LABELS.get(op.get("op", ""), op.get("op", "operation"))
        by = op.get("by") or SYSTEM_AUTHOR
        return RemoteError(
            f"A {label} started by {by} is still running. Try again when it finishes.",
            "busy",
        )
    return RemoteError(
        "Another Olano process on this home is syncing with GitHub. Try again shortly.",
        "busy",
    )


@contextlib.contextmanager
def remote_operation(engine: RevisionsEngine, op: str, by: str = "") -> Iterator[None]:
    """Hold the single remote-operation slot, or raise ``busy`` immediately."""
    if not _OP_GATE.acquire(blocking=False):
        raise _busy(current_operation())
    fh = None
    try:
        engine.ensure_repo()
        fh = (engine.repo / _OP_LOCK_FILE).open("a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise _busy(None) from exc
        _CURRENT_OP.update(
            op=op,
            by=by or SYSTEM_AUTHOR,
            since=_now_iso(),
            pid=os.getpid(),
            progress={"steps": list(_OP_STEPS.get(op, [])), "step": None, "percent": None},
        )
        yield
    finally:
        _CURRENT_OP.clear()
        if fh is not None:
            fh.close()  # releases the flock
        _OP_GATE.release()


# ── the sync itself ─────────────────────────────────────────────────────────


@dataclass
class _MergeTree:
    tree: str
    conflicts: list[dict[str, Any]] = field(default_factory=list)


class RemoteSync:
    """Push / fetch / pull between the local revisions repo and one remote.

    ``url`` is any git URL (the dashboard only ever passes a validated
    ``https://github.com/...``; tests pass a local bare repo). Callers that
    mutate go through the public methods, which take :data:`WRITER_LOCK`.
    """

    def __init__(
        self,
        engine: RevisionsEngine,
        url: str,
        *,
        branch: str = "main",
        token: str | None = None,
        label: str = "GitHub",
    ) -> None:
        self.engine = engine
        self.url = url
        self.branch = _check_branch(branch)
        self.token = token
        self.label = label
        self.remote_ref = f"refs/remotes/origin/{self.branch}"

    # ── plumbing ──────────────────────────────────────────────────────

    def _net(self, *args: str) -> str:
        proc = self.engine._git_proc(*args, env=_auth_env(self.token), timeout=_GIT_LONG_TIMEOUT)
        if proc.returncode != 0:
            raise _explain(proc.stderr or proc.stdout, self.token)
        return proc.stdout

    def _net_streaming(self, *args: str, on_line: Any) -> subprocess.CompletedProcess[str]:
        """Run a network git command, feeding each stderr line to ``on_line``
        as it arrives (git repaints progress with carriage returns)."""
        cmd = ["git", f"--git-dir={self.engine.repo}", *args]
        env = {**os.environ, **_auth_env(self.token), "LC_ALL": "C"}
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        assert proc.stdout is not None and proc.stderr is not None
        stdout = proc.stdout
        out: list[bytes] = []
        reader = threading.Thread(target=lambda: out.append(stdout.read()), daemon=True)
        reader.start()
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(_GIT_LONG_TIMEOUT, _kill)
        timer.daemon = True
        timer.start()
        err = bytearray()
        pending = b""
        try:
            while True:
                chunk = os.read(proc.stderr.fileno(), 8192)
                if not chunk:
                    break
                err.extend(chunk)
                if len(err) > 256_000:
                    del err[:-128_000]  # keep the tail: the error is at the end
                pending += chunk
                *lines, pending = re.split(rb"[\r\n]", pending)
                for raw in lines:
                    if raw:
                        try:
                            on_line(raw.decode("utf-8", errors="replace"))
                        except Exception:
                            logger.debug("revisions remote: progress line failed", exc_info=True)
            proc.wait()
        finally:
            timer.cancel()
            reader.join(timeout=10)
        if timed_out.is_set():
            raise RemoteError(
                f"{self.label} did not answer in time; nothing was changed.", "timeout"
            )
        return subprocess.CompletedProcess(
            cmd,
            proc.returncode,
            (out[0] if out else b"").decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
        )

    def _ref(self, ref: str) -> str | None:
        proc = self.engine._git_proc("rev-parse", "--verify", "--quiet", ref)
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def _is_ancestor(self, older: str, newer: str) -> bool:
        proc = self.engine._git_proc("merge-base", "--is-ancestor", older, newer)
        if proc.returncode not in (0, 1):
            raise RemoteError(proc.stderr.strip() or "git merge-base failed", "git")
        return proc.returncode == 0

    def _blob(self, oid: str | None) -> bytes | None:
        if not oid:
            return None
        return self.engine._git_bytes("cat-file", "blob", oid)

    def remote_heads(self) -> dict[str, str]:
        """``{ref: oid}`` the remote advertises (branches and tags)."""
        out = self._net("ls-remote", "--heads", "--tags", self.url)
        heads: dict[str, str] = {}
        for line in out.splitlines():
            oid, _, ref = line.partition("\t")
            if ref:
                heads[ref] = oid
        return heads

    def fetch(self) -> str | None:
        """Update ``refs/remotes/origin/<branch>``; None when the branch is absent."""
        with WRITER_LOCK:
            self.engine.ensure_repo()
            report_progress("download")
            heads = self.remote_heads()
            if f"refs/heads/{self.branch}" not in heads:
                if self._ref(self.remote_ref):
                    self.engine._git("update-ref", "-d", self.remote_ref)
                save_state(self.engine, last_fetch_at=_now_iso())
                return None
            proc = self._net_streaming(
                "fetch",
                "--no-tags",
                "--progress",
                self.url,
                f"+refs/heads/{self.branch}:{self.remote_ref}",
                on_line=_progress_reader("download", _DOWNLOAD_PHASES),
            )
            if proc.returncode != 0:
                raise _explain(proc.stderr or proc.stdout, self.token)
            report_progress("download", percent=100)
            save_state(self.engine, last_fetch_at=_now_iso())
            return self._ref(self.remote_ref)

    def counts(self) -> dict[str, int]:
        """Commits only here (``ahead``) and only on the remote (``behind``),
        as of the last fetch or push."""
        head = self.engine._head()
        remote = self._ref(self.remote_ref)
        if not head:
            return {"ahead": 0, "behind": 0}
        if not remote:
            out = self.engine._git("rev-list", "--count", head)
            return {"ahead": int(out.strip() or 0), "behind": 0}
        out = self.engine._git("rev-list", "--left-right", "--count", f"{head}...{remote}")
        left, _, right = out.strip().partition("\t")
        return {"ahead": int(left or 0), "behind": int(right or 0)}

    def unpushed(self, limit: int = 500) -> list[str]:
        head = self.engine._head()
        if not head:
            return []
        args = ["rev-list", f"--max-count={limit}", head]
        remote = self._ref(self.remote_ref)
        if remote:
            args.extend(["--not", remote])
        return self.engine._git(*args).split()

    # ── push ──────────────────────────────────────────────────────────

    def push(self, *, force: bool = False) -> dict[str, Any]:
        with WRITER_LOCK:
            head = self.engine._head()
            if not head:
                return {"pushed": False, "reason": "nothing to push yet"}
            if not force and self._ref(self.remote_ref) == head:
                # GitHub already has this commit (as of the last push or
                # fetch): a second Push of the same history sends nothing.
                return {"pushed": False, "reason": "already pushed", "commit": head}
            spec = f"{'+' if force else ''}{head}:refs/heads/{self.branch}"
            report_progress("upload", percent=0)
            proc = self._net_streaming(
                "push",
                "--porcelain",
                "--progress",
                self.url,
                spec,
                on_line=_progress_reader("upload", _UPLOAD_PHASES),
            )
            if proc.returncode != 0:
                text = f"{proc.stdout}\n{proc.stderr}"
                if any(s in text for s in ("[rejected]", "non-fast-forward", "fetch first")):
                    raise RemoteError(
                        f"{self.label} has commits this box does not have yet. Pull first to "
                        "merge them, or force push to replace the remote history with this box's.",
                        "rejected",
                    )
                raise _explain(proc.stderr or proc.stdout, self.token)
            self.engine._git("update-ref", self.remote_ref, head)
            # Tags (pre-upgrade-<version>) ride along best-effort: a tag the
            # remote already has under another id must not fail the push.
            self.engine._git_proc(
                "push",
                "--quiet",
                self.url,
                "refs/tags/*:refs/tags/*",
                env=_auth_env(self.token),
                timeout=_GIT_LONG_TIMEOUT,
            )
            report_progress("upload", percent=100)
            now = _now_iso()
            save_state(
                self.engine,
                last_push_at=now,
                # GitHub just took this exact history, so this box knows
                # GitHub's state as of now as surely as a check would.
                last_fetch_at=now,
                last_push_ts=time.time(),
                last_push_commit=head,
                needs_pull=False,
            )
            _clear_error(self.engine)
            return {"pushed": True, "commit": head, "forced": force}

    # ── pull ──────────────────────────────────────────────────────────

    def pull(self, *, author: str = SYSTEM_AUTHOR) -> dict[str, Any]:
        with WRITER_LOCK:
            if load_merge(self.engine):
                raise RemoteError(
                    "A merge from the remote is waiting to be resolved. Finish or abort it first.",
                    "merge_pending",
                )
            report_progress("snapshot")
            snap = self.engine.snapshot(reason=f"snapshot before pulling from {self.label}")
            if snap.error:
                raise RemoteError(f"Could not snapshot before pulling: {snap.error}", "snapshot")
            theirs = self.fetch()
            report_progress("apply")
            ours = self.engine._head()
            if theirs is None:
                return {"status": "up_to_date", "reason": "remote_empty"}
            if ours == theirs or (ours and self._is_ancestor(theirs, ours)):
                save_state(self.engine, last_pull_at=_now_iso(), needs_pull=False)
                return {"status": "up_to_date"}
            if not ours or self._is_ancestor(ours, theirs):
                result = self._integrate(
                    self.engine.tree_of(theirs),
                    ours or "",
                    theirs,
                    kind="fast_forward",
                    author=author,
                    message=f"Pull from {self.label} ({self.branch})",
                )
            else:
                merged = self._merge_tree(ours, theirs)
                if merged.conflicts:
                    state = {
                        "ours": ours,
                        "theirs": theirs,
                        "tree": merged.tree,
                        "branch": self.branch,
                        "label": self.label,
                        "created_at": _now_iso(),
                        "conflicts": merged.conflicts,
                    }
                    _write_json(self.engine.repo / _MERGE_FILE, state)
                    return {"status": "conflicts", "merge": public_merge(state)}
                result = self._integrate(
                    merged.tree,
                    ours,
                    theirs,
                    kind="merged",
                    author=author,
                    message=f"Merge {self.label} ({self.branch}) into this box",
                )
            save_state(self.engine, last_pull_at=_now_iso(), needs_pull=False)
            return result

    def _merge_tree(self, ours: str, theirs: str) -> _MergeTree:
        proc = self.engine._git_proc("merge-tree", "--write-tree", "-z", ours, theirs)
        if proc.returncode not in (0, 1):
            err = proc.stderr.strip()
            if "unrelated histories" in err:
                raise RemoteError(
                    f"{self.label}'s history shares no commit with this box's, so the two "
                    "cannot be merged. Force push to replace the remote history, or connect "
                    "an empty repository.",
                    "unrelated",
                )
            raise RemoteError(err or "git merge-tree failed", "git")
        fields = proc.stdout.split("\0")
        tree = fields[0].strip()
        if proc.returncode == 0:
            return _MergeTree(tree=tree)
        by_path: dict[str, dict[str, Any]] = {}
        for entry in fields[1:]:
            if not entry:
                break  # the empty field ends the conflicted-file list
            meta, _, path = entry.partition("\t")
            parts = meta.split()
            if len(parts) != 3:
                continue
            _mode, oid, stage = parts
            slot = {"1": "base", "2": "ours", "3": "theirs"}.get(stage)
            if slot:
                by_path.setdefault(
                    path, {"path": path, "base": None, "ours": None, "theirs": None}
                )[slot] = oid
        return _MergeTree(tree=tree, conflicts=[by_path[p] for p in sorted(by_path)])

    # ── resolving a parked merge ──────────────────────────────────────

    def merge_file(self, path: str) -> dict[str, Any]:
        """Everything the resolver shows for one conflicted file."""
        state = load_merge(self.engine)
        conflict = _conflict(state, path)
        raw = {k: self._blob(conflict[k]) for k in ("base", "ours", "theirs")}
        texts = {k: _as_text(v) for k, v in raw.items()}
        binary = any(v is not None and texts[k] is None for k, v in raw.items())
        sizes = {k: (len(v) if v is not None else None) for k, v in raw.items()}
        if binary:
            # A document, image or recording: nothing to merge line by line,
            # so the resolver offers one side or the other (or deleting it).
            return {
                "path": path,
                "binary": True,
                "sizes": sizes,
                "base": None,
                "ours": None,
                "theirs": None,
                "merged": "",
                "patch": "",
            }
        base, ours, theirs = texts["base"], texts["ours"], texts["theirs"]
        if ours is not None and theirs is not None:
            merged = self._merge_text(base or "", ours, theirs)
        else:
            merged = ours if ours is not None else (theirs or "")
        a = conflict["ours"] or self._empty_blob()
        b = conflict["theirs"] or self._empty_blob()
        patch = self.engine._git("diff", "--no-color", "-U3", a, b)
        patch = patch.replace(f"a/{a}", f"a/{path}").replace(f"b/{b}", f"b/{path}")
        return {
            "path": path,
            "binary": False,
            "sizes": sizes,
            "base": base,
            "ours": ours,
            "theirs": theirs,
            "merged": merged,
            "patch": patch,
        }

    def _empty_blob(self) -> str:
        return self.engine._git("hash-object", "-w", "--stdin", input="").strip() or _EMPTY_BLOB

    def _merge_text(self, base: str, ours: str, theirs: str) -> str:
        with tempfile.TemporaryDirectory(prefix="olano-merge-") as tmp:
            files = []
            for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
                p = Path(tmp) / name
                p.write_text(text, encoding="utf-8")
                files.append(str(p))
            proc = self.engine._git_proc(
                "merge-file",
                "-p",
                "-L",
                "this box",
                "-L",
                "common ancestor",
                "-L",
                self.label,
                *files,
            )
            if proc.returncode < 0 or proc.returncode > 127:
                raise RemoteError(proc.stderr.strip() or "git merge-file failed", "git")
            return proc.stdout

    def resolve_merge(
        self, resolutions: dict[str, dict[str, Any]], *, author: str = SYSTEM_AUTHOR
    ) -> dict[str, Any]:
        """Finish a parked merge with one decision per conflicted file.

        A decision is ``{"action": "ours" | "theirs" | "delete"}`` or
        ``{"action": "content", "content": "<text>"}``.
        """
        with WRITER_LOCK:
            state = load_merge(self.engine)
            if not state:
                raise RemoteError("There is no merge waiting to be resolved.", "no_merge")
            decided: dict[str, str | None] = {}
            for conflict in state["conflicts"]:
                path = conflict["path"]
                decision = resolutions.get(path)
                if not isinstance(decision, dict):
                    raise RemoteError(f"{path} has no resolution yet.", "unresolved")
                action = decision.get("action")
                if action in ("ours", "theirs"):
                    decided[path] = conflict[action]
                elif action == "delete":
                    decided[path] = None
                elif action == "content":
                    text = decision.get("content")
                    if not isinstance(text, str):
                        raise RemoteError(f"{path}: the merged text is missing.", "unresolved")
                    if _CONFLICT_MARKER.search(text):
                        raise RemoteError(
                            f"{path} still contains conflict markers (lines starting with "
                            "<<<<<<< or >>>>>>>). Edit them out, or pick one side.",
                            "markers_left",
                        )
                    decided[path] = self.engine._git(
                        "hash-object", "-w", "--stdin", input=text
                    ).strip()
                else:
                    raise RemoteError(f"{path}: unknown resolution {action!r}.", "unresolved")
            tree = self._resolved_tree(state["tree"], decided)

            snap = self.engine.snapshot(reason="snapshot before finishing a merge")
            if snap.error:
                raise RemoteError(f"Could not snapshot before merging: {snap.error}", "snapshot")
            head = self.engine._head()
            label = state.get("label") or self.label
            message = f"Merge {label} ({state.get('branch') or self.branch}) into this box"
            if head == state["ours"]:
                result = self._integrate(
                    tree, head, state["theirs"], kind="merged", author=author, message=message
                )
            else:
                # Agents kept working while the user resolved: fold the resolved
                # merge into what this box has now, the same way a pull would.
                resolved = self.engine.commit_tree(
                    tree, [state["ours"], state["theirs"]], message, author=author
                )
                merged = self._merge_tree(head, resolved)
                if merged.conflicts:
                    state.update(
                        ours=head,
                        theirs=resolved,
                        tree=merged.tree,
                        conflicts=merged.conflicts,
                        created_at=_now_iso(),
                    )
                    _write_json(self.engine.repo / _MERGE_FILE, state)
                    return {"status": "conflicts", "merge": public_merge(state)}
                result = self._integrate(
                    merged.tree, head, resolved, kind="merged", author=author, message=message
                )
            _write_json(self.engine.repo / _MERGE_FILE, None)
            save_state(self.engine, last_pull_at=_now_iso(), needs_pull=False)
            return result

    def _resolved_tree(self, base_tree: str, decided: dict[str, str | None]) -> str:
        with tempfile.TemporaryDirectory(prefix="olano-merge-index-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            self.engine._git("read-tree", base_tree, env=env)
            for path, oid in decided.items():
                if oid is None:
                    self.engine._git("update-index", "--force-remove", "--", path, env=env)
                else:
                    self.engine._git(
                        "update-index", "--add", "--cacheinfo", f"100644,{oid},{path}", env=env
                    )
            return self.engine._git("write-tree", env=env).strip()

    # ── applying a tree to the live home ──────────────────────────────

    def _integrate(
        self,
        tree: str,
        ours: str,
        theirs: str,
        *,
        kind: str,
        author: str,
        message: str,
    ) -> dict[str, Any]:
        """Make the live home match ``tree`` and record it as the new head.

        Caller holds the lock and has just snapshotted, so ``ours`` is the
        live state as history knows it.
        """
        eng = self.engine
        base_tree = eng.tree_of(ours) if ours else EMPTY_TREE
        raw = eng._git("diff-tree", "-r", "-z", "--no-renames", base_tree, tree)
        fields = raw.split("\0")
        writes: dict[str, tuple[bytes, str]] = {}
        deletes: list[str] = []
        ignored: list[dict[str, str]] = []
        kept: list[str] = []
        for i in range(0, len(fields) - 1, 2):
            meta, path = fields[i], fields[i + 1]
            parts = meta.lstrip(":").split()
            if len(parts) < 5:
                continue
            new_mode, new_oid, status = parts[1], parts[3], parts[4]
            if not is_tracked_path(path, include_memory=eng.include_memory):
                ignored.append({"path": path, "reason": "not a tracked file"})
                continue
            if status == "D":
                if _KEEP_ON_DELETE.match(path):
                    kept.append(path)
                else:
                    deletes.append(path)
                continue
            if new_mode not in _REGULAR_MODES:
                ignored.append({"path": path, "reason": "not a regular file"})
                continue
            size = int(eng._git("cat-file", "-s", new_oid).strip() or 0)
            if size > eng.max_file_bytes:
                ignored.append({"path": path, "reason": "larger than the size cap"})
                continue
            writes[path] = (eng._git_bytes("cat-file", "blob", new_oid), new_mode)

        # Nothing it would touch may have moved since the snapshot. Compared
        # by blob id, so a 100 MB video is hashed, never held in memory twice.
        for path in [*writes, *deletes]:
            live_path = eng.home / path
            live = eng.live_oid(path)
            if live is None and (live_path.exists() or live_path.is_symlink()):
                # Held back here (over the cap, carrying a secret) or a link:
                # this box's copy wins and is never overwritten.
                ignored.append({"path": path, "reason": "the local file is not tracked"})
                writes.pop(path, None)
                if path in deletes:
                    deletes.remove(path)
                continue
            stored = eng.stored_oid(path, ours) if ours else None
            if live != stored:
                raise RemoteError(
                    f"{path} changed on this box while pulling. Nothing was applied; pull again.",
                    "changed_during_pull",
                )

        # Validate and transform everything before writing anything.
        final: dict[str, tuple[bytes, str]] = {}
        warnings: list[str] = []
        for path, (data, mode) in writes.items():
            prepared, warn = _prepare(eng.home, path, data)
            final[path] = (prepared, mode)
            warnings.extend(warn)

        for path, (data, mode) in final.items():
            _write_live(eng.home, path, data, executable=mode == "100755")
        for path in deletes:
            try:
                (eng.home / path).unlink()
            except FileNotFoundError:
                pass

        new_tree = eng.snapshot_tree()
        if kind == "fast_forward" and new_tree == eng.tree_of(theirs):
            new_head = theirs
        else:
            parents = [theirs] if kind == "fast_forward" else [p for p in (ours, theirs) if p]
            new_head = eng.commit_tree(new_tree, parents, message, author=author)
        eng.advance_head(new_head, ours)

        agents_changed = sorted(
            {
                p.split("/")[1]
                for p in [*final, *deletes]
                if p.startswith("agents/") and p.endswith("/agent.json5")
            }
        )
        return {
            "status": "pulled",
            "kind": kind,
            "commit": new_head,
            "written": sorted(final),
            "deleted": sorted(deletes),
            "kept": sorted(kept),
            "ignored": ignored,
            "warnings": warnings,
            "agents_changed": agents_changed,
            "config_changed": any(p in _YAML_FILES for p in final),
        }


def _conflict(state: dict[str, Any] | None, path: str) -> dict[str, Any]:
    if not state:
        raise RemoteError("There is no merge waiting to be resolved.", "no_merge")
    for conflict in state["conflicts"]:
        if conflict["path"] == path:
            return dict(conflict)
    raise RemoteError(f"{path} is not part of the pending merge.", "not_conflicted")


def public_merge(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    return {
        "ours": state["ours"],
        "theirs": state["theirs"],
        "branch": state.get("branch", ""),
        "created_at": state.get("created_at", ""),
        "files": [
            {
                "path": c["path"],
                # Which side still has the file: a modify/delete conflict is
                # resolved by keeping one version or deleting it.
                "ours_deleted": c["ours"] is None,
                "theirs_deleted": c["theirs"] is None,
            }
            for c in state["conflicts"]
        ],
    }


# ── turning pulled blobs into live files ─────────────────────────────────────


def _as_text(data: bytes | None) -> str | None:
    """``data`` as text, or None when it is binary (a NUL, or not UTF-8)."""
    if data is None or b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _prepare(home: Path, path: str, data: bytes) -> tuple[bytes, list[str]]:
    """Validate one incoming file and return the bytes to write live.

    Only the home's own configuration is parsed and rewritten; every other
    file (scripts, documents, media) is written exactly as GitHub has it.
    """
    name = path.rsplit("/", 1)[-1]
    is_agent = path.startswith("agents/") and name == "agent.json5"
    if not is_agent and path not in _YAML_FILES:
        return data, []
    text = _as_text(data)
    if text is None:
        raise RemoteError(f"{path} is not a text file.", "invalid_file")
    if is_agent:
        out, warnings = _restore_agent_env(home / path, path, text)
        return out.encode("utf-8"), warnings
    return _keep_live_revisions_block(home / path, path, text).encode("utf-8"), []


def _restore_agent_env(live_path: Path, path: str, text: str) -> tuple[str, list[str]]:
    from olano.config.loader import _load_json5

    try:
        data = _load_json5(text)
    except Exception as exc:
        raise RemoteError(f"{path} is not valid JSON5: {exc}", "invalid_file") from exc
    if not isinstance(data, dict):
        raise RemoteError(f"{path} must contain an object.", "invalid_file")
    env = data.get("env")
    if not isinstance(env, dict) or "__vault__" not in env.values():
        return text, []
    live_env: dict[str, Any] = {}
    try:
        live = _load_json5(live_path.read_text(encoding="utf-8"))
        if isinstance(live, dict) and isinstance(live.get("env"), dict):
            live_env = live["env"]
    except (OSError, ValueError, Exception):
        live_env = {}
    restored: dict[str, Any] = {}
    missing: list[str] = []
    for key, value in env.items():
        if value != "__vault__":
            restored[key] = value
        elif key in live_env:
            restored[key] = live_env[key]
        else:
            missing.append(key)
    data["env"] = restored
    warnings = [
        f"{path}: env value for {key} is not on this box (history stores it redacted), "
        "so it was left out. Set it on the agent's Variables tab."
        for key in missing
    ]
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n", warnings


def _keep_live_revisions_block(live_path: Path, path: str, text: str) -> str:
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RemoteError(f"{path} is not valid YAML: {exc}", "invalid_file") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise RemoteError(f"{path} must contain a mapping.", "invalid_file")
    if path != "config.yaml":
        return text
    live_block = None
    try:
        live = yaml.safe_load(live_path.read_text(encoding="utf-8"))
        if isinstance(live, dict):
            live_block = live.get("revisions")
    except (OSError, yaml.YAMLError):
        live_block = None
    if data.get("revisions") == live_block:
        return text
    if live_block is None:
        data.pop("revisions", None)
    else:
        data["revisions"] = live_block
    return str(yaml.dump(data, sort_keys=False))


def _atomic_write_bytes(target: Path, data: bytes, *, executable: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        mode = 0o644
        try:
            mode = target.stat().st_mode & 0o777
        except FileNotFoundError:
            pass
        Path(tmp).chmod(mode | 0o111 if executable else mode & ~0o111)
        Path(tmp).replace(target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_live(home: Path, path: str, data: bytes, *, executable: bool = False) -> None:
    target = home / path
    # Never write through a link: the snapshot never follows one either.
    cur = home
    for part in path.split("/")[:-1]:
        cur = cur / part
        if cur.is_symlink() or (cur.exists() and not cur.is_dir()):
            raise RemoteError(f"{path}: a parent directory is a link or a file.", "unsafe_path")
    if target.is_symlink():
        raise RemoteError(f"{path} is a link on this box; not overwriting it.", "unsafe_path")
    if not str(target.resolve()).startswith(str(home.resolve()) + os.sep):
        raise RemoteError(f"{path} resolves outside the home.", "unsafe_path")

    from olano.tools._sandbox_exec import handoff_to_sandbox, missing_parents

    created = missing_parents(target)
    if path == "config.yaml":
        from olano.config.home import write_home_config_text

        write_home_config_text(target, data.decode("utf-8"))
    elif path.startswith("agents/") and path.endswith("/agent.json5"):
        from olano.config.loader import write_agent_config_text

        target.parent.mkdir(parents=True, exist_ok=True)
        write_agent_config_text(target, data.decode("utf-8"))
    else:
        _atomic_write_bytes(target, data, executable=executable)
    parts = path.split("/")
    if len(parts) > 3 and parts[0] == "agents" and parts[2] == "workspace":
        try:
            handoff_to_sandbox(target, created=created, agent=parts[1])
        except Exception:
            logger.debug("revisions remote: sandbox hand-off failed for %s", path, exc_info=True)


# ── settings-level entry points (dashboard routes, worker) ──────────────────


def remote_settings() -> Any:
    from olano.config.system_settings import load_revisions_settings

    return load_revisions_settings().remote


def sync_from_settings(engine: RevisionsEngine | None = None) -> RemoteSync:
    remote = remote_settings()
    if not remote.url:
        raise RemoteError("The revision history is not connected to GitHub.", "not_connected")
    return RemoteSync(
        engine or home_engine(),
        remote.url,
        branch=remote.branch,
        token=resolve_token(remote.auth),
        label="GitHub",
    )


def _local_sync(engine: RevisionsEngine | None = None) -> RemoteSync:
    """A syncer for the operations that never talk to the remote (merge state)."""
    remote = remote_settings()
    return RemoteSync(engine or home_engine(), remote.url, branch=remote.branch or "main")


def merge_file(path: str) -> dict[str, Any]:
    return _local_sync().merge_file(path)


def resolve_merge(
    resolutions: dict[str, dict[str, Any]],
    *,
    author: str = SYSTEM_AUTHOR,
    engine: RevisionsEngine | None = None,
) -> dict[str, Any]:
    return _local_sync(engine).resolve_merge(resolutions, author=author)


def abort_merge(by: str = "") -> None:
    """Drop a pending merge. Nothing was applied, so nothing is undone."""
    engine = home_engine()
    with remote_operation(engine, "abort", by), WRITER_LOCK:
        _write_json(engine.repo / _MERGE_FILE, None)


def run_op(op: str, fn: Any, *, by: str = "") -> Any:
    """Run one remote operation in the single slot, recording a failure in
    the sync state. A ``busy`` refusal is not recorded: nothing failed."""
    engine = home_engine()
    with remote_operation(engine, op, by):
        try:
            return fn(engine)
        except RemoteError as exc:
            _record_error(engine, exc, op=op)
            raise


def connect(
    *,
    repo: str,
    auth: str = "token",
    token: str = "",
    branch: str = "main",
    sync: str = "manual",
    allow_public: bool = False,
    by: str = "",
) -> dict[str, Any]:
    """Validate the repository, push the whole history to it, save the settings."""
    with remote_operation(home_engine(), "connect", by):
        return _connect(
            repo=repo,
            auth=auth,
            token=token,
            branch=branch,
            sync=sync,
            allow_public=allow_public,
        )


def _connect(
    *,
    repo: str,
    auth: str,
    token: str,
    branch: str,
    sync: str,
    allow_public: bool,
) -> dict[str, Any]:
    from olano.config.system_settings import set_revisions_settings

    owner, name = parse_github_repo(repo)
    branch = _check_branch(branch)
    if sync not in SYNC_MODES:
        raise RemoteError(f"Unknown sync mode {sync!r}.", "invalid_sync")
    if auth not in AUTH_MODES:
        raise RemoteError(f"Unknown sign-in method {auth!r}.", "invalid_auth")
    token = (token or "").strip()
    if auth == "token" and not token:
        token = _stored_token()
    if auth == "token" and not token:
        raise RemoteError("Paste a GitHub access token.", "no_token")
    if auth == "connection":
        token = resolve_token("connection")

    report_progress("check")
    info = github_repo_info(owner, name, token)
    perms = info.get("permissions")
    if isinstance(perms, dict) and perms.get("push") is False:
        raise RemoteError(
            f"The token can read {owner}/{name} but not write to it. Grant it "
            "Contents: read and write.",
            "no_push",
        )
    if not info.get("private", False) and not allow_public:
        raise RemoteError(
            f"{owner}/{name} is public. The revision history holds your configuration "
            "and agent memory, so anyone could read it. Make the repository private, or "
            "confirm that a public repository is intended.",
            "public_repo",
        )

    engine = home_engine()
    url = github_url(owner, name)
    syncer = RemoteSync(engine, url, branch=branch, token=token)
    if syncer.remote_heads():
        raise RemoteError(
            f"{owner}/{name} is not empty. The first connection needs an empty repository "
            "(create it on GitHub without a README, license or .gitignore).",
            "not_empty",
        )
    with WRITER_LOCK:
        report_progress("snapshot")
        snap = engine.snapshot(reason="snapshot before connecting to GitHub")
        if snap.error:
            raise RemoteError(f"Could not snapshot before connecting: {snap.error}", "snapshot")
        _write_json(engine.repo / _MERGE_FILE, None)
        if syncer._ref(syncer.remote_ref):
            engine._git("update-ref", "-d", syncer.remote_ref)
        result = syncer.push()
        report_progress("save")
        if auth == "token":
            store_token(token)
        set_revisions_settings(
            {"remote": {"url": url, "branch": branch, "auth": auth, "sync": sync}}
        )
        save_state(engine, connected_at=_now_iso(), needs_pull=False)
    return {"connected": True, "url": url, "push": result}


def update_remote(*, sync: str | None = None) -> None:
    from olano.config.system_settings import set_revisions_settings

    remote = remote_settings()
    if not remote.url:
        raise RemoteError("The revision history is not connected to GitHub.", "not_connected")
    data = remote.model_dump()
    if sync is not None:
        if sync not in SYNC_MODES:
            raise RemoteError(f"Unknown sync mode {sync!r}.", "invalid_sync")
        data["sync"] = sync
    set_revisions_settings({"remote": data})


def disconnect(by: str = "") -> None:
    """Forget the remote here. The GitHub repository itself is left as it is."""
    from olano.config.system_settings import RevisionsRemoteSettings, set_revisions_settings

    engine = home_engine()
    with remote_operation(engine, "disconnect", by), WRITER_LOCK:
        remote = remote_settings()
        set_revisions_settings({"remote": RevisionsRemoteSettings().model_dump()})
        if remote.auth == "token":
            forget_token()
        if (engine.repo / "HEAD").exists():
            for ref in engine._git_proc(
                "for-each-ref", "--format=%(refname)", "refs/remotes/origin/"
            ).stdout.split():
                engine._git_proc("update-ref", "-d", ref)
        _write_json(engine.repo / _MERGE_FILE, None)
        _write_json(engine.repo / _STATE_FILE, None)


def status(*, fetch: bool = False, by: str = "") -> dict[str, Any]:
    from olano.config.system_settings import load_revisions_settings

    settings = load_revisions_settings()
    remote = settings.remote
    engine = home_engine()
    out: dict[str, Any] = {
        "enabled": settings.enabled,
        "connected": bool(remote.url),
        "url": remote.url,
        "repo": repo_slug(remote.url) if remote.url else "",
        "web_url": remote.url[: -len(".git")] if remote.url.endswith(".git") else remote.url,
        "branch": remote.branch,
        "auth": remote.auth,
        "sync": remote.sync,
        "connection": github_connection(),
        "merge": None,
        "ahead": 0,
        "behind": 0,
        "unpushed": [],
        "busy": current_operation(),
    }
    if not (engine.repo / "HEAD").exists():
        out["state"] = {}
        return out
    out["merge"] = public_merge(load_merge(engine))
    if remote.url:
        syncer = RemoteSync(engine, remote.url, branch=remote.branch, label="GitHub")
        if fetch:
            try:
                with remote_operation(engine, "fetch", by):
                    try:
                        syncer.token = resolve_token(remote.auth)
                        syncer.fetch()
                        _clear_error(engine)
                    except RemoteError as exc:
                        _record_error(engine, exc, op="fetch")
            except RemoteError:
                pass  # busy: whatever is running will refresh the counts itself
        try:
            out.update(syncer.counts())
            out["unpushed"] = syncer.unpushed()
        except Exception:
            logger.debug("revisions remote: counts failed", exc_info=True)
    state = load_state(engine)
    state.pop("last_push_ts", None)
    state.pop("last_auto_attempt_ts", None)
    out["state"] = state
    return out


def auto_sync(*, committed: bool) -> None:
    """The worker's hook: push when the configured sync mode says it is due.

    Never raises. ``manual`` never pushes; ``on_commit`` pushes after each
    commit (and retries a failure every :data:`_RETRY_SECONDS`); ``daily``
    pushes when the last successful push is a day old. A push GitHub refused
    because it moved on stops automatic pushing until a pull or a manual
    push clears ``needs_pull``: an unattended force is never right.
    """
    try:
        from olano.config.system_settings import load_revisions_settings

        settings = load_revisions_settings()
        remote = settings.remote
        if not settings.enabled or not remote.url or remote.sync == "manual":
            return
        engine = home_engine()
        state = load_state(engine)
        if state.get("needs_pull") or load_merge(engine):
            return
        head = engine._head()
        if not head or head == state.get("last_push_commit"):
            return
        now = time.time()
        since_attempt = now - float(state.get("last_auto_attempt_ts") or 0)
        if remote.sync == "on_commit":
            due = committed or since_attempt >= _RETRY_SECONDS
        else:
            since_push = now - float(state.get("last_push_ts") or 0)
            due = since_push >= _DAILY_SECONDS and since_attempt >= _RETRY_SECONDS
        if not due:
            return
        try:
            with remote_operation(engine, "auto_push"):
                save_state(engine, last_auto_attempt_ts=now)
                try:
                    result = sync_from_settings(engine).push()
                    if result.get("pushed"):
                        logger.info("revisions: pushed %s to %s", head[:10], repo_slug(remote.url))
                except RemoteError as exc:
                    _record_error(engine, exc, op="auto_push")
                    logger.warning("revisions: automatic push failed: %s", exc)
        except RemoteError:
            # Someone is already pushing or pulling; it carries the same
            # history, and the next tick re-checks whether anything is left.
            logger.debug("revisions: automatic push skipped, another sync is running")
    except Exception:
        logger.warning("revisions: automatic sync failed", exc_info=True)
