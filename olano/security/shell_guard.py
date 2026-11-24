"""Shell command validation middleware.

Validates and normalises shell commands before execution.
Docker/sandbox lifecycle has moved to OlanoDockerBackend (olano/backends/lionclaw_docker.py).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from olano.backends.interpreter_shims import with_interpreter_shims
from olano.env_aliases import spellings
from olano.security.sharing import SharedMount

# Commands that are always blocked regardless of trust level
_BLOCKED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+-rf\s+/", re.IGNORECASE),
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bdd\s+if=\s*/dev/zero", re.IGNORECASE),
    re.compile(r":\(\)\s*\{.*\}", re.IGNORECASE),  # fork bomb
    re.compile(r"\bchmod\s+-R\s+777\s+/"),
    re.compile(r"\bcurl\b.*\|\s*(?:bash|sh|python|ruby)\b", re.IGNORECASE),
    re.compile(r"\bwget\b.*-O-.*\|\s*(?:bash|sh)", re.IGNORECASE),
]

_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;)\s*")
_PATHLIKE_RE = re.compile(r"^(?:/|~|\.\.?/)")
_SEGMENT_CAPTURE_RE = re.compile(r"(\s*(?:&&|\|\||;)\s*)")
_COMPLEX_SHELL_RE = re.compile(r"(\||<|>|`|\$\(|\$\{|\*|\?|\[)")
_DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)
_READ_ONLY_BASE_COMMANDS = {
    "cat",
    "cut",
    "date",
    "diff",
    "echo",
    "env",
    "find",
    "grep",
    "head",
    "ls",
    "paste",
    "printenv",
    "pwd",
    "rg",
    "sed",
    "sort",
    "tail",
    "tr",
    "uniq",
    "wc",
    "which",
}
SHELL_ENV_DEFAULTS = {
    "NO_COLOR": "1",
    "CLICOLOR": "0",
    "GH_SPINNER_DISABLED": "1",
    "PIP_PROGRESS_BAR": "off",
    "npm_config_progress": "false",
    "NPM_CONFIG_PROGRESS": "false",
}

# Platform secrets that must NEVER be visible to an agent-run shell, regardless
# of posture. `printenv`/`env`/`cat /proc/self/environ` from a shell tool would
# otherwise hand out the vault master key (⇒ decrypt the whole credential vault),
# the MCP relay secret, and the per-deployment / dashboard tokens. These are
# infra secrets no agent *script* legitimately needs — agent code receives
# credentials through the per-agent vault resolver, not the process environment.
#
# Each name is listed once and stripped under both spellings: ``olano.env_aliases``
# mirrors every ``LIONCLAW_*`` onto ``OLANO_*`` and back at process start, so a
# set that knew one spelling would strip the original and hand out its copy.
_ALWAYS_STRIP_ENV: frozenset[str] = frozenset(
    spelling
    for name in (
        "LIONCLAW_VAULT_KEY",
        "LIONCLAW_VAULT_PASSWORD",
        "LIONCLAW_MCP_RELAY_SECRET",
        "LIONCLAW_DEPLOYMENT_TOKEN",
        "LIONCLAW_DASHBOARD_TOKEN",
        "LIONCLAW_ADMIN_PASSWORD",
        "OLANO_SETTINGS_SIGNING_KEY",
        # The one-time first-run setup code: whoever holds it before the first
        # admin exists can create that admin (runtime/setup_code.py).
        "OLANO_SETUP_CODE",
        # Per-deployment virtual key for the Olano model gateway. An agent
        # script holding this could burn the deployment's entire credit balance
        # from outside the runtime's metering; the runtime reads it directly
        # (deployment/gateway_key.py), never through the shell environment.
        # Both the canonical name and the pre-rename one: `spellings` pairs
        # OLANO_GATEWAY_KEY with LIONCLAW_GATEWAY_KEY, not with the legacy
        # LIONCLAW_OLANO_GATEWAY_KEY that older boxes carry in olano.env.
        "OLANO_GATEWAY_KEY",
        "LIONCLAW_OLANO_GATEWAY_KEY",
        # The account API key a self-hosted box spends from: same hazard, the
        # account's whole wallet instead of one deployment's.
        "OLANO_CREDITS_KEY",
        # The Langfuse secret key reads every trace in its project, which in
        # full capture is every traced agent's conversations. The runtime reads
        # it directly (observability/credentials.py); no shell needs it.
        "LANGFUSE_SECRET_KEY",
        "OLANO_LANGFUSE_SECRET_KEY",
    )
    for spelling in spellings(name)
)

# Broader secret-shaped suffixes stripped IN ADDITION under a managed
# (non-"open") deployment posture — a customer's agent shell has no business
# reading provider keys / tokens out of the runtime's own environment. Left
# intact under the open/unmanaged posture (the operator's own box).
_SECRET_SUFFIXES: tuple[str, ...] = (
    "_API_KEY",
    "_SECRET",
    "_SECRET_KEY",
    "_TOKEN",
    "_PASSWORD",
    "_PRIVATE_KEY",
    "_ACCESS_KEY",
)


def _managed_posture() -> bool:
    """True when this install runs under a managed (enforced/quarantine) posture.

    Best-effort and fail-safe: any error resolving the posture is treated as
    unmanaged (open) so the extra stripping never breaks an operator box; the
    always-strip set still applies unconditionally.
    """
    try:
        from olano.deployment import enforcement as dep_enforcement

        return dep_enforcement.deployment_posture().posture != "open"
    except Exception:
        return False


def strip_platform_secrets(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of *base* (default ``os.environ``) without the platform secrets.

    The narrow half of :func:`sanitized_shell_env`: only `_ALWAYS_STRIP_ENV`
    (the vault key, the deployment and dashboard tokens, the gateway key, the
    account credits key, under both spellings), no posture-dependent suffix
    rule, no shell defaults layered on. For the runtime's OWN child processes
    that inherit the environment wholesale and are not agent shells: the
    WhatsApp bridge and a host-mode stdio MCP server. They need PATH, HOME and
    their own WHATSAPP_* / server variables; none of them has any business
    holding a credential that spends the box's credits or unlocks its vault.
    """
    src = os.environ if base is None else base
    return {k: v for k, v in src.items() if k.upper() not in _ALWAYS_STRIP_ENV}


def sanitized_shell_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of the environment with platform secrets removed.

    Always strips the infra-secret allowlist; under a managed posture also
    strips provider-key / token-shaped variables. `SHELL_ENV_DEFAULTS` is layered
    on top so callers get the quiet-output defaults for free.
    """
    src = dict(os.environ if base is None else base)
    managed = _managed_posture()
    out: dict[str, str] = {}
    for key, val in src.items():
        upper = key.upper()
        if upper in _ALWAYS_STRIP_ENV:
            continue
        if managed and any(upper.endswith(sfx) for sfx in _SECRET_SUFFIXES):
            continue
        out[key] = val
    out.update(SHELL_ENV_DEFAULTS)
    return out


class ShellResult(NamedTuple):
    stdout: str
    stderr: str
    returncode: int
    blocked: bool = False
    block_reason: str = ""


def _token_looks_like_path(token: str) -> bool:
    if not token or token.startswith("-") or "://" in token:
        return False
    return bool(_PATHLIKE_RE.match(token) or "/" in token)


def normalize_command(command: str) -> str:
    """Normalize shell text to a stable representation before validation."""
    normalized = unicodedata.normalize("NFC", command)
    return normalized.translate(_DASH_TRANSLATION)


def validate_command(command: str) -> tuple[bool, str]:
    """Return (is_safe, reason). is_safe=False means the command was blocked."""
    command = normalize_command(command)
    if "\x00" in command:
        return False, "Blocked by shell guard: NUL bytes are not allowed in shell commands."
    try:
        parts = shlex.split(command)
    except ValueError:
        return False, "Blocked by shell guard: could not parse command."
    if not parts or any(part == "" for part in parts):
        return False, "Blocked by shell guard: empty commands or empty arguments are not allowed."
    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(command):
            return (
                False,
                f"Blocked by shell guard: matches pattern '{pattern.pattern}'. "
                "Use a workspace-scoped, non-destructive command instead.",
            )
    return True, ""


def _resolve_candidate_path(token: str, cwd: Path) -> Path | None:
    if not token or token in {"-", "--"}:
        return None
    if token.startswith("~"):
        return Path(token).expanduser()
    if token.startswith("/"):
        return Path(token)
    if token.startswith("./") or token.startswith("../") or ("/" in token and "://" not in token):
        return (cwd / token).resolve()
    return None


def _mount_for_container_token(token: str, mounts: list[SharedMount]) -> SharedMount | None:
    for mount in mounts:
        if token == mount.target or token.startswith(mount.target + "/"):
            return mount
    return None


def _container_path_to_host(
    token: str,
    workspace_root: Path,
    mounts: list[SharedMount],
) -> Path | None:
    if token == "/workspace" or token.startswith("/workspace/"):
        suffix = token[len("/workspace") :].lstrip("/")
        return (workspace_root / suffix).resolve()
    for mount in mounts:
        if token == mount.target or token.startswith(mount.target + "/"):
            suffix = token[len(mount.target) :].lstrip("/")
            return (mount.source / suffix).resolve()
    return None


def _is_within_roots(candidate: Path, roots: list[Path]) -> bool:
    resolved = candidate.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _mount_for_candidate_path(
    candidate: Path,
    *,
    workspace_root: Path,
    mounts: list[SharedMount],
) -> SharedMount | None:
    try:
        rel = candidate.absolute().relative_to(workspace_root.resolve()).as_posix()
    except ValueError:
        rel = ""
    for mount in mounts:
        target = mount.target.lstrip("/")
        if rel and (rel == target or rel.startswith(target + "/")):
            return mount
        try:
            candidate.resolve().relative_to(mount.source.resolve())
            return mount
        except ValueError:
            continue
    return None


def validate_command_paths(
    command: str,
    *,
    cwd: Path,
    workspace_root: Path,
    mounts: list[SharedMount],
) -> tuple[bool, str]:
    """Best-effort path validation — blocks commands that reference paths outside visible roots."""
    command = normalize_command(command)
    roots = [workspace_root.resolve(), *(mount.source.resolve() for mount in mounts)]
    segment_cwd = cwd.resolve()
    for segment in filter(None, _SEGMENT_SPLIT_RE.split(command.strip())):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return False, "Blocked by shell guard: could not parse command."
        if not tokens:
            continue
        base_command = tokens[0].split("/")[-1]
        segment_is_read_only = base_command in _READ_ONLY_BASE_COMMANDS and not any(
            token in segment for token in ("|", ">", ">>", "<", "$(")
        )

        if tokens[0] == "cd":
            target = tokens[1] if len(tokens) > 1 else str(workspace_root)
            candidate = _container_path_to_host(target, workspace_root, mounts)
            if candidate is None:
                candidate = _resolve_candidate_path(target, segment_cwd) or segment_cwd
            if not _is_within_roots(candidate, roots):
                return False, f"Blocked by sandbox: '{target}' is outside the visible roots."
            segment_cwd = candidate
            continue

        executable = tokens[0]
        if _token_looks_like_path(executable):
            candidate = _container_path_to_host(executable, workspace_root, mounts)
            if candidate is None:
                candidate = _resolve_candidate_path(executable, segment_cwd)
            if candidate is not None and not _is_within_roots(candidate, roots):
                return False, f"Blocked by sandbox: '{executable}' is outside the visible roots."

        for token in tokens[1:]:
            if not _token_looks_like_path(token):
                continue
            candidate = _container_path_to_host(token, workspace_root, mounts)
            if candidate is None:
                candidate = _resolve_candidate_path(token, segment_cwd)
            if candidate is None:
                continue
            if not _is_within_roots(candidate, roots):
                return False, f"Blocked by sandbox: '{token}' is outside the visible roots."
            if not segment_is_read_only:
                mount = _mount_for_candidate_path(
                    candidate,
                    workspace_root=workspace_root,
                    mounts=mounts,
                )
                if mount is not None and mount.mode == "ro":
                    return False, f"Blocked by sandbox: '{mount.target}' is read-only."
    return True, ""


def _host_path_to_container(path: Path, workspace_root: Path, mounts: list[SharedMount]) -> str:
    resolved = path.resolve()
    try:
        suffix = resolved.relative_to(workspace_root.resolve())
        return "/workspace" if str(suffix) == "." else f"/workspace/{suffix.as_posix()}"
    except ValueError:
        pass
    for mount in mounts:
        try:
            suffix = resolved.relative_to(mount.source.resolve())
            return mount.target if str(suffix) == "." else f"{mount.target}/{suffix.as_posix()}"
        except ValueError:
            continue
    return "/workspace"


def _translate_command_for_container(
    command: str,
    *,
    cwd: Path,
    workspace_root: Path,
    mounts: list[SharedMount],
) -> tuple[str, list[SharedMount]]:
    """Rewrite host-visible paths in a shell command to container-visible paths.

    Conservative rewrite: commands with shell metacharacters are left unchanged
    and all active mounts are passed through.
    """
    if _COMPLEX_SHELL_RE.search(command):
        return command, mounts

    roots = [workspace_root.resolve(), *(mount.source.resolve() for mount in mounts)]
    segment_cwd = cwd.resolve()
    required_mount_ids: set[str] = set()
    cwd_mount = _mount_for_candidate_path(
        segment_cwd,
        workspace_root=workspace_root,
        mounts=mounts,
    )
    if cwd_mount is not None:
        required_mount_ids.add(cwd_mount.id)

    translated_parts: list[str] = []
    for part in _SEGMENT_CAPTURE_RE.split(command):
        stripped = part.strip()
        if not stripped:
            translated_parts.append(part)
            continue
        if stripped in {"&&", "||", ";"}:
            translated_parts.append(part)
            continue
        try:
            tokens = shlex.split(part)
        except ValueError:
            return command, mounts
        if not tokens:
            translated_parts.append(part)
            continue

        translated_tokens: list[str] = []
        next_segment_cwd = segment_cwd
        for index, token in enumerate(tokens):
            translated = token
            candidate: Path | None = None
            mount = _mount_for_container_token(token, mounts)

            if token == "/workspace" or token.startswith("/workspace/") or mount is not None:
                candidate = _container_path_to_host(token, workspace_root, mounts)
            elif _token_looks_like_path(token) and not any(ch in token for ch in "*?[]"):
                candidate = _resolve_candidate_path(token, segment_cwd)

            if candidate is not None and _is_within_roots(candidate, roots):
                mount = _mount_for_candidate_path(
                    candidate,
                    workspace_root=workspace_root,
                    mounts=mounts,
                )
                if mount is not None:
                    required_mount_ids.add(mount.id)
                translated = _host_path_to_container(candidate, workspace_root, mounts)

            translated_tokens.append(translated)
            if tokens[0] == "cd" and index == 1 and candidate is not None:
                next_segment_cwd = candidate.resolve()

        if tokens[0] == "cd" and len(tokens) > 1:
            segment_cwd = next_segment_cwd

        translated_parts.append(shlex.join(translated_tokens))

    required_mounts = [mount for mount in mounts if mount.id in required_mount_ids]
    return "".join(translated_parts), required_mounts


def run_local(command: str, timeout: int = 30, cwd: str | None = None) -> ShellResult:
    """Run command locally after validation."""
    command = normalize_command(command)
    is_safe, reason = validate_command(command)
    if not is_safe:
        return ShellResult(
            stdout="", stderr=reason, returncode=1, blocked=True, block_reason=reason
        )

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            # Scrubbed env + the runtime-written `python`/`pip` shims appended
            # to PATH (interpreter_shims.py) — same treatment the sandbox lane
            # gets, so a bare `python` resolves on a python3-only host.
            env=with_interpreter_shims(sanitized_shell_env()),
        )
        return ShellResult(
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
        )
    except subprocess.TimeoutExpired:
        return ShellResult(stdout="", stderr=f"Command timed out after {timeout}s", returncode=1)
    except Exception as e:
        return ShellResult(stdout="", stderr=str(e), returncode=1)
