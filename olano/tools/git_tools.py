"""Git network verbs — clone / fetch / pull / push with the agent's connected account.

Every tool here runs ``git`` as a **connected job** (``core/job_runner.py``): a
throwaway container the agent has no shell in, holding the credential for the
length of one command. The agent's own persistent sandbox never sees a token —
not in its env, not in a file, not in ``.git/config``. Local git (``status``,
``add``, ``commit``, ``diff``, ``checkout``, ``log``, ``merge``) needs no
credential and stays in the agent's own sandbox via ``execute``.

How the credential reaches git, and nothing else:

- ``https`` remotes only. Olano authenticates https; an ``ssh://`` or
  ``git@host:`` URL is refused, and a URL carrying its own ``user:pass@`` too.
- The remote's host must be the credential's host (``_HOST_CREDENTIALS``);
  the URL is read from ``.git/config`` in-process before anything runs.
- The token rides as ``GIT_JOB_TOKEN`` in the job env and is handed to git by
  a **credential helper scoped to that host** (``credential.https://<host>.helper``)
  written on the command line — never a file (an askpass in the shared tool
  layer would be agent-writable), never in the URL, never in ``.git/config``,
  and never offered to any other host (a submodule or redirect elsewhere gets
  nothing).
- Repo-local config an agent may have edited is neutralised: keys with a safe
  clearing value are overridden with ``-c`` (command-line config beats
  ``.git/config``), and the ones that have none — ``include.path``,
  ``url.*.insteadOf``, ``http.*`` (an empty ``http.proxy``/``sslCAInfo`` means
  "no proxy"/"no CA bundle", which breaks legitimate boxes), ``credential.*``,
  ``filter.*`` smudge commands — make the job refuse before it starts.

Verified on git 2.43: ``GIT_CONFIG_COUNT`` entries are applied *before* ``-c``
entries, so a ``-c credential.helper=`` clear would wipe a helper set through
``GIT_CONFIG_*``. Both the clear and the helper therefore travel as ``-c``
pairs, in that order; no ``GIT_CONFIG_*`` is used.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from langchain_core.tools import tool

from olano.core.job_runner import EXIT_BLOCKED, EXIT_TIMEOUT, run_connected_job
from olano.security.tool_metadata import tool_metadata
from olano.tools._credentials import get_credential
from olano.tools._sandbox_exec import ExecResult
from olano.tools._toolkit import make_toolkit_api
from olano.tools.talk_to import current_agent

logger = logging.getLogger(__name__)

JOB_LABEL = "git"
JOB_TIMEOUT = 600
TOKEN_ENV = "GIT_JOB_TOKEN"

# host → credential names tried in order (extensible later via a provider's
# `exposes`). A host not listed proceeds unauthenticated — public repos work.
_HOST_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "github.com": ("GITHUB_TOKEN", "GITHUB_PAT"),
}

_MISSING_GITHUB = (
    "[missing-credentials] GitHub is not connected for this agent — ask the person "
    'to connect it (magic_link_connect_service("github")); never ask for a token in chat.'
)
_HTTPS_ONLY = "Olano authenticates https remotes only"

# The helper reads the token from the job env at run time; the string itself
# holds only the variable's *name*, so it is safe on an argv.
_CREDENTIAL_HELPER = '!f(){ echo username=x-access-token; echo "password=$GIT_JOB_TOKEN"; }; f'

# Command-line config wins over `.git/config`, so these neutralise anything an
# agent wrote there for the keys that have a safe clearing value. Order
# matters for `credential.helper`: the empty value clears the list accumulated
# from files, then the host-scoped helper is appended (see module docstring).
_GIT_OVERRIDES: tuple[str, ...] = (
    "credential.helper=",
    "core.hooksPath=/nonexistent",
    "core.sshCommand=false",
    "core.gitProxy=",
    "core.fsmonitor=false",
    "core.askPass=",
    "http.sslVerify=true",
    "protocol.allow=never",
    "protocol.https.allow=always",
    "submodule.recurse=false",
    "fetch.recurseSubmodules=false",
)
_JOB_ENV: dict[str, str] = {"GIT_TERMINAL_PROMPT": "0"}

# `.git/config` content that cannot be neutralised with a `-c` override and
# could redirect or capture the credential inside the job container.
_HAZARD_SECTIONS: frozenset[str] = frozenset(
    {"include", "includeif", "url", "http", "credential", "filter"}
)
_HAZARD_KEYS: frozenset[tuple[str, str]] = frozenset(
    {
        ("core", "worktree"),  # checkout lands outside the workspace
        ("extensions", "worktreeconfig"),  # pulls .git/config.worktree in too
        ("remote", "proxy"),
        ("remote", "vcs"),  # picks a remote helper by name
    }
)

_SCP_LIKE_RE = re.compile(r"^[^/:@]+@[^/:]+:")
_SECTION_RE = re.compile(r'^\[\s*([^\s"\]]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]')
_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*))?$")
_USERINFO_RE = re.compile(r"(?<=://)[^/\s@]+@")
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_AUTH_FAILURE_MARKERS = (
    "authentication failed",
    "could not read username",
    "terminal prompts disabled",
    "repository not found",
    "invalid username or token",
    "permission to",
    "403",
    "401",
)
_MAX_OUTPUT = 4000


# ── workspace / path policy ──────────────────────────────────────────────────


def _agent_name() -> str:
    return current_agent.get() or "agent"


def _workspace_root() -> Path:
    from olano.core.mcp_setup import _agent_workspace_root

    return _agent_workspace_root(_agent_name())


def _resolve_inside(root: Path, raw: str) -> Path | None:
    """Resolve *raw* (relative to *root*, or absolute) and require it stays inside.

    The model sees host-absolute workspace paths in its prompt, so an absolute
    path is legitimate as long as it resolves under the workspace; ``..`` and
    symlinks out of it resolve outside and are refused.
    """
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        root_resolved = root.resolve()
    except OSError:
        return None
    if resolved != root_resolved and root_resolved not in resolved.parents:
        return None
    return resolved


def _repo_name(url: str) -> str:
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return tail or "repo"


def _bad_name(kind: str, value: str) -> str | None:
    if not value:
        return None
    if value.startswith("-") or not _NAME_RE.fullmatch(value):
        return f"[refused] {value!r} is not a valid {kind}."
    return None


# ── URL policy ───────────────────────────────────────────────────────────────


def _parse_https_url(url: str) -> tuple[str, str | None]:
    """``(host, None)`` for an acceptable https URL, else ``("", reason)``."""
    url = (url or "").strip()
    if not url:
        return "", "[refused] a repository URL is required."
    if _SCP_LIKE_RE.match(url):
        return "", f"[refused] {_HTTPS_ONLY} — {url!r} is an ssh remote."
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        shown = f"{parsed.scheme}://" if parsed.scheme else "no scheme"
        return "", f"[refused] {_HTTPS_ONLY} ({shown} given)."
    if parsed.username is not None or parsed.password is not None:
        return "", "[refused] the URL must not carry credentials; the connected account is used."
    host = (parsed.hostname or "").lower()
    if not host or any(c.isspace() for c in url):
        return "", f"[refused] {url!r} is not a valid https URL."
    return host, None


def _token_for(host: str) -> str:
    for name in _HOST_CREDENTIALS.get(host, ()):
        value = get_credential(name)
        if value:
            return value
    return ""


# ── `.git/config` (read in-process, before anything runs) ───────────────────


def _parse_git_config(text: str) -> list[tuple[str, str, str, str]]:
    """``[(section, subsection, key, value)]`` — sections/keys lowercased."""
    entries: list[tuple[str, str, str, str]] = []
    section, subsection = "", ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        head = _SECTION_RE.match(line)
        if head:
            section = head.group(1).lower()
            subsection = head.group(2) or ""
            # `[remote.origin]`-style dotted headers (deprecated but parsed by git).
            if not subsection and "." in section:
                section, subsection = section.split(".", 1)
            continue
        kv = _KEY_RE.match(line)
        if not kv or not section:
            continue
        value = (kv.group(2) or "").strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        entries.append((section, subsection, kv.group(1).lower(), value))
    return entries


def _open_repo(repo_path: str, root: Path) -> tuple[Path | None, str | None]:
    repo = _resolve_inside(root, (repo_path or "").strip() or ".")
    if repo is None:
        return None, f"[refused] repo_path must be inside the workspace ({root})."
    gitdir = repo / ".git"
    if not gitdir.is_dir():
        return None, (
            f"[refused] {repo} is not a git repository (no .git directory; linked "
            "worktrees are not supported)."
        )
    return repo, None


def _load_repo_config(repo: Path, root: Path) -> tuple[list[tuple[str, str, str, str]], str | None]:
    cfg = repo / ".git" / "config"
    try:
        resolved = cfg.resolve()
    except OSError:
        return [], f"[refused] {cfg} could not be resolved."
    if root.resolve() not in resolved.parents or not resolved.is_file():
        return [], f"[refused] {cfg} must be a regular file inside the workspace."
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], f"[refused] {cfg} could not be read: {exc}"
    return _parse_git_config(text), None


def _config_hazard(entries: list[tuple[str, str, str, str]], repo: Path) -> str | None:
    for section, subsection, key, _value in entries:
        if section in _HAZARD_SECTIONS or (section, key) in _HAZARD_KEYS:
            shown = f"{section}.{subsection}.{key}" if subsection else f"{section}.{key}"
            return (
                f"[refused] {repo}/.git/config sets `{shown}`, which could redirect or "
                "capture the credential inside the job; remove that entry (git config "
                f"--unset {shown}) and retry."
            )
    return None


def _remote_url(
    entries: list[tuple[str, str, str, str]], remote: str, *, for_push: bool
) -> str | None:
    url = pushurl = None
    for section, subsection, key, value in entries:
        if section != "remote" or subsection != remote:
            continue
        if key == "url" and url is None:
            url = value
        elif key == "pushurl" and pushurl is None:
            pushurl = value
    return (pushurl or url) if for_push else url


# ── running git ──────────────────────────────────────────────────────────────


def _git_argv(host: str, token: str) -> list[str]:
    argv = ["git"]
    for override in _GIT_OVERRIDES:
        argv += ["-c", override]
    if token:
        argv += ["-c", f"credential.https://{host}.helper={_CREDENTIAL_HELPER}"]
    return argv


def _run_git(argv: list[str], *, token: str, workspace_root: Path) -> ExecResult:
    return run_connected_job(
        argv,
        agent=_agent_name(),
        workspace_root=workspace_root,
        secret_env={TOKEN_ENV: token} if token else {},
        extra_env=dict(_JOB_ENV),
        timeout=JOB_TIMEOUT,
        label=JOB_LABEL,
    )


def _scrub(text: str) -> str:
    # Belt to the runner's braces: userinfo in any URL git echoes back.
    return _USERINFO_RE.sub("[redacted]@", text or "")


def _tail(text: str) -> str:
    text = text.strip()
    if len(text) > _MAX_OUTPUT:
        return "…" + text[-_MAX_OUTPUT:]
    return text


def _looks_like_auth_failure(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _AUTH_FAILURE_MARKERS)


def _render(result: ExecResult, *, success: str, host: str, token: str) -> str:
    output = _scrub("\n".join(part for part in (result.stdout, result.stderr) if part))
    if result.returncode == 0:
        tail = _tail(output)
        return f"{success}\n{tail}" if tail else success
    if result.returncode == EXIT_BLOCKED:
        return _tail(output)
    if result.returncode == EXIT_TIMEOUT:
        return f"[tool-error] git timed out after {JOB_TIMEOUT}s.\n{_tail(output)}"
    if _looks_like_auth_failure(output) and host in _HOST_CREDENTIALS:
        if not token:
            return f"{_MISSING_GITHUB}\n{_tail(output)}"
        return (
            "[tool-error] git: the connected GitHub account was refused — it may lack "
            "access to this repository, or the connection may need re-authorizing "
            '(magic_link_connect_service("github")).\n' + _tail(output)
        )
    return f"[tool-error] git exited {result.returncode}:\n{_tail(output)}"


def _prepare_remote_op(
    repo_path: str, remote: str, *, for_push: bool
) -> tuple[Path, Path, str, str] | str:
    """Containment, config scan, remote-host policy → ``(root, repo, host, token)`` or an error."""
    err = _bad_name("remote name", remote)
    if err:
        return err
    root = _workspace_root()
    repo, err = _open_repo(repo_path, root)
    if err or repo is None:
        return err or "[refused] invalid repo_path."
    entries, err = _load_repo_config(repo, root)
    if err:
        return err
    hazard = _config_hazard(entries, repo)
    if hazard:
        return hazard
    url = _remote_url(entries, remote, for_push=for_push)
    if not url:
        return f"[refused] remote {remote!r} has no url in {repo}/.git/config."
    host, err = _parse_https_url(url)
    if err:
        return f"[refused] remote {remote!r} is {url!r}: {_HTTPS_ONLY}."
    token = _token_for(host)
    if for_push and not token and host in _HOST_CREDENTIALS:
        return _MISSING_GITHUB
    return root, repo, host, token


# ── tools ────────────────────────────────────────────────────────────────────


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False)
@tool
def git_clone(
    url: Annotated[str, "https URL of the repository (ssh URLs are refused)"],
    dest: Annotated[
        str, "Directory to clone into, inside the workspace. Default: <workspace>/<repo name>"
    ] = "",
    branch: Annotated[str, "Branch or tag to check out (default: the remote's default)"] = "",
    depth: Annotated[int, "Shallow-clone depth; 0 = full history"] = 0,
) -> str:
    """Clone an https git repository into the workspace with the agent's connected account.

    The clone runs in a throwaway job container that holds the credential for
    this one command; your own sandbox never sees a token, and the files land
    in the workspace at the returned path. Private GitHub repositories need
    GitHub connected for this agent; public ones need nothing. Local git —
    status, add, commit, diff, checkout, log — runs in your own sandbox via
    `execute`. Submodules are never recursed.
    """
    host, err = _parse_https_url(url)
    if err:
        return err
    url = url.strip()
    err = _bad_name("branch", branch.strip())
    if err:
        return err
    root = _workspace_root()
    target = _resolve_inside(root, (dest or "").strip() or _repo_name(url))
    if target is None:
        return f"[refused] dest must be inside the workspace ({root})."
    if target == root.resolve():
        return "[refused] dest must be a directory inside the workspace, not the workspace itself."
    if target.exists() and any(target.iterdir()):
        return f"[refused] {target} already exists and is not empty."
    token = _token_for(host)
    argv = [*_git_argv(host, token), "clone"]
    if branch.strip():
        argv += ["--branch", branch.strip()]
    if depth and depth > 0:
        argv += ["--depth", str(int(depth))]
    argv += ["--", url, str(target)]
    result = _run_git(argv, token=token, workspace_root=root)
    return _render(result, success=f"Cloned {url} into {target}", host=host, token=token)


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False)
@tool
def git_fetch(
    repo_path: Annotated[str, "Path of the repository inside the workspace"],
    remote: Annotated[str, "Remote name (default: origin)"] = "origin",
) -> str:
    """Fetch a remote of a workspace repository with the agent's connected account.

    Runs in a throwaway job container holding the credential for this one
    command; the remote must be an https URL on the credential's host. Local
    git (status, merge, checkout, log) runs in your own sandbox via `execute`.
    """
    prepared = _prepare_remote_op(repo_path, remote, for_push=False)
    if isinstance(prepared, str):
        return prepared
    root, repo, host, token = prepared
    argv = [*_git_argv(host, token), "-C", str(repo), "fetch", "--", remote]
    result = _run_git(argv, token=token, workspace_root=root)
    return _render(result, success=f"Fetched {remote} in {repo}", host=host, token=token)


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False)
@tool
def git_pull(
    repo_path: Annotated[str, "Path of the repository inside the workspace"],
    remote: Annotated[str, "Remote name (default: origin)"] = "origin",
    branch: Annotated[str, "Remote branch to pull (default: the tracked branch)"] = "",
) -> str:
    """Fast-forward a workspace repository from its remote with the agent's connected account.

    Runs in a throwaway job container holding the credential for this one
    command. Fast-forward only: when histories have diverged, run `git_fetch`
    and merge or rebase in your own sandbox via `execute` (local git needs no
    credential). The remote must be an https URL on the credential's host.
    """
    err = _bad_name("branch", branch.strip())
    if err:
        return err
    prepared = _prepare_remote_op(repo_path, remote, for_push=False)
    if isinstance(prepared, str):
        return prepared
    root, repo, host, token = prepared
    argv = [*_git_argv(host, token), "-C", str(repo), "pull", "--ff-only", "--", remote]
    if branch.strip():
        argv.append(branch.strip())
    result = _run_git(argv, token=token, workspace_root=root)
    return _render(result, success=f"Pulled {remote} into {repo}", host=host, token=token)


@tool_metadata(capability="write_external", risk="high", requires_hitl=True)
@tool
def git_push(
    repo_path: Annotated[str, "Path of the repository inside the workspace"],
    remote: Annotated[str, "Remote name (default: origin)"] = "origin",
    branch: Annotated[str, "Branch to push (default: the current branch's upstream)"] = "",
    set_upstream: Annotated[bool, "Also set the branch's upstream (-u)"] = False,
) -> str:
    """Push a workspace repository to its https remote with the agent's connected account.

    Runs in a throwaway job container holding the credential for this one
    command; a force push is never available. Commit first in your own
    sandbox via `execute` (local git needs no credential). The remote must be
    an https URL on the credential's host, and GitHub must be connected for
    this agent.
    """
    err = _bad_name("branch", branch.strip())
    if err:
        return err
    prepared = _prepare_remote_op(repo_path, remote, for_push=True)
    if isinstance(prepared, str):
        return prepared
    root, repo, host, token = prepared
    argv = [*_git_argv(host, token), "-C", str(repo), "push"]
    if set_upstream:
        argv.append("--set-upstream")
    argv += ["--", remote]
    if branch.strip():
        argv.append(branch.strip())
    result = _run_git(argv, token=token, workspace_root=root)
    return _render(result, success=f"Pushed {repo} to {remote}", host=host, token=token)


get_tools, get_tool = make_toolkit_api(globals())
