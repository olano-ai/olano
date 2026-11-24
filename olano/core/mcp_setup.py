"""MCP server connection helpers shared by the runtime and the dashboard.

This module is the single source of truth for:

- **Transport normalization** — the dashboard / agent.json5 speak ``sse`` /
  ``http`` / ``stdio``; langchain-mcp-adapters and the raw MCP SDK speak
  ``sse`` / ``streamable_http`` / ``stdio``. :func:`normalize_transport` bridges
  the two so ``http`` always means *streamable HTTP*.
- **OAuth token storage** — :class:`FileTokenStorage` persists the dynamically
  registered client + access/refresh tokens per ``(agent, server-url)`` under
  ``<LIONCLAW_HOME>/mcp_oauth/``.  Mirrors the codex ``auth.json`` pattern.
- **Connection params** — :func:`build_connection_params` produces the per-server
  dict consumed by ``MultiServerMCPClient`` (used by the agent builder), attaching
  a non-interactive :class:`OAuthClientProvider` for ``auth: oauth`` servers.
- **Probing** — :func:`probe_server` connects to a single server (any transport),
  lists its tools, and returns rich diagnostics (step-by-step logs, tool list,
  and whether the server *requires* OAuth) for the dashboard "Test connection".
- **Interactive OAuth** — :func:`start_oauth_flow` / :func:`poll_oauth_flow` /
  :func:`resolve_oauth_callback` drive the authorization-code + PKCE handshake
  from the setup panel: the agent kicks off a live connection, surfaces the
  authorization URL for the user to open, and captures the redirect callback.

The runtime build path (:func:`build_connection_params`) is **non-interactive**:
it only ever uses already-stored tokens (refreshing when possible).  If a server
needs OAuth and has not been authorized yet, the connection fails fast with a
clear message instead of blocking the agent boot.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

# How long (seconds) a single probe connection is allowed to take. Generous
# enough for a first `npx`/`uvx` run that must download the package before the
# server can start (the classic fresh-cloud-VM case).
PROBE_TIMEOUT = 60.0


def _probe_timeout(override: float | None = None) -> float:
    """Effective probe timeout: explicit override → env → :data:`PROBE_TIMEOUT`.

    ``LIONCLAW_MCP_PROBE_TIMEOUT`` exists for hosts where the default is too
    tight — e.g. a fresh cloud VM whose first ``npx``/``uvx`` run must download
    the package from a slow registry before the server can even start.
    """
    if override is not None:
        return override
    raw = (os.environ.get("LIONCLAW_MCP_PROBE_TIMEOUT") or "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning("Ignoring non-numeric LIONCLAW_MCP_PROBE_TIMEOUT=%r", raw)
    return PROBE_TIMEOUT


# How long (seconds) a single MCP server may take to connect + list tools during
# agent boot before it is skipped. Without this cap a slow/hung server would
# block the entire agent build (the boot path, unlike the probe, has no UI to
# show progress and no way to cancel).
BOOT_CONNECT_TIMEOUT = 30.0
# After the boot connect times out and is cancelled, how long (seconds) to wait
# for the transport to unwind before ABANDONING it. Cancelling an MCP connect is
# not guaranteed to return promptly: a stdio child stuck in an interactive
# prompt (e.g. an `mcp-remote` wrapper waiting for its own OAuth authorization)
# can wedge the SDK's cancellation/cleanup path, and `asyncio.wait_for` would
# then block the whole runtime boot indefinitely — the exact failure mode this
# grace bounds.
BOOT_CANCEL_GRACE = 5.0
# How many of an agent's MCP servers may connect at once during boot. The
# connect is pure I/O wait (TCP/TLS handshake, or an stdio child's startup), so
# overlapping them costs no measurable memory — unlike the RAG ingest, which is
# deliberately serialized because N concurrent ONNX runs multiply peak RSS.
# Serial connects made every BOOT_CONNECT_TIMEOUT additive: an agent with two
# unreachable servers paid 60s before it could boot. Bounded rather than
# unbounded so an agent with a dozen stdio servers doesn't fork a dozen children
# simultaneously on a small host.
BOOT_CONNECT_CONCURRENCY = 8


def boot_connect_concurrency() -> int:
    """Resolve the per-agent MCP boot fan-out (``LIONCLAW_MCP_BOOT_CONCURRENCY``).

    Set to 1 to restore the old fully-serial behaviour when debugging a server
    whose startup races with its siblings.
    """
    raw = (os.environ.get("LIONCLAW_MCP_BOOT_CONCURRENCY") or "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning("Ignoring non-numeric LIONCLAW_MCP_BOOT_CONCURRENCY=%r", raw)
    return BOOT_CONNECT_CONCURRENCY


# ── Boot-time failure memo ───────────────────────────────────────────────────
#
# The runtime boots agents **serially**, and the per-agent connect bound
# (BOOT_CONNECT_TIMEOUT + BOOT_CANCEL_GRACE = 35s) is paid once per *agent*, not
# once per server. A fleet where six agents share one dead server therefore
# spends 3.5 minutes of boot discovering the same outage six times — the single
# biggest contributor to "the runtime took six minutes to come up".
#
# So remember failures for a short while and let later agents skip straight to
# the same verdict. Only *failures* are cached: caching a success would mean
# handing agent B a toolset discovered with agent A's credentials, which is a
# different and much riskier proposition.
#
# Two keys, because not every failure generalises the same way:
#
# * **reach key** — "can this endpoint be reached at all": transport + endpoint,
#   plus the launch command/args/env for stdio (there, the env *is* what the
#   child does). Deliberately excludes request headers and OAuth tokens: a
#   timeout means no answer came back, and no bearer token changes TCP
#   reachability. This is what lets six agents holding six *different* OAuth
#   tokens for one dead server share a single 35s discovery.
# * **full key** — reach key + headers + the actual stored token bytes. Used for
#   failures that *are* credential-specific (a 401, a protocol error): only an
#   agent whose credentials are byte-identical may skip on one of those.
#
# Both keys hash credential material rather than storing it.
BOOT_FAILURE_TTL = 300.0


def boot_failure_ttl() -> float:
    """Effective failure-memo lifetime (``LIONCLAW_MCP_FAILURE_CACHE_TTL``).

    Sized to outlast a fleet boot (a 10-agent boot was measured at ~234s), not
    to be a durable health record. Set to ``0`` to disable the memo entirely and
    make every agent re-discover every failure.
    """
    raw = (os.environ.get("LIONCLAW_MCP_FAILURE_CACHE_TTL") or "").strip()
    if raw:
        try:
            value = float(raw)
            if value >= 0:
                return value
        except ValueError:
            logger.warning("Ignoring non-numeric LIONCLAW_MCP_FAILURE_CACHE_TTL=%r", raw)
    return BOOT_FAILURE_TTL


@dataclass(frozen=True)
class BootFailure:
    """A remembered per-connection boot failure."""

    status: str
    message: str
    url: str
    at: float

    def age(self) -> float:
        return max(0.0, time.time() - self.at)


_FAILURE_CACHE: dict[str, BootFailure] = {}
_FAILURE_LOCK = threading.Lock()


def _digest(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _token_digest(agent: str, url: str) -> str:
    """Digest of the stored OAuth material for *(agent, url)*.

    Unreadable/absent tokens digest to an agent-unique value, never a shared
    constant — when we cannot prove two agents hold the same credential, they
    must not share a credential-specific verdict.
    """
    try:
        raw = token_store_path(agent, url).read_bytes()
    except OSError:
        return _digest("no-token", agent, url)
    return hashlib.sha256(raw).hexdigest()


def connection_reach_key(params: dict[str, Any]) -> str:
    """Key for "is this endpoint reachable at all" — see the module notes above."""
    return _digest(
        "reach",
        str(params.get("transport", "")),
        str(params.get("url", "")),
        str(params.get("command") or ""),
        json.dumps(list(params.get("args") or []), sort_keys=True),
        # stdio only; for a child process the env decides what it actually does.
        _digest(json.dumps(dict(params.get("env") or {}), sort_keys=True))
        if params.get("env") is not None
        else "",
    )


def connection_full_key(params: dict[str, Any], *, agent: str, url: str) -> str:
    """Reach key plus everything that identifies *this agent's* credentials."""
    return _digest(
        "full",
        connection_reach_key(params),
        json.dumps(dict(params.get("headers") or {}), sort_keys=True),
        _token_digest(agent, url) if params.get("auth") is not None else "",
    )


def remember_boot_failure(key: str, *, status: str, message: str, url: str) -> None:
    """Record a failed boot connect so identical connections can skip it."""
    if boot_failure_ttl() <= 0:
        return
    with _FAILURE_LOCK:
        _FAILURE_CACHE[key] = BootFailure(status=status, message=message, url=url, at=time.time())


def recall_boot_failure(*keys: str) -> BootFailure | None:
    """Return the first live remembered failure among *keys*, expiring stale ones."""
    ttl = boot_failure_ttl()
    if ttl <= 0:
        return None
    now = time.time()
    with _FAILURE_LOCK:
        for key in keys:
            entry = _FAILURE_CACHE.get(key)
            if entry is None:
                continue
            if now - entry.at > ttl:
                _FAILURE_CACHE.pop(key, None)
                continue
            return entry
    return None


def forget_boot_failure(key: str) -> None:
    """Drop one remembered failure — the connection turned out to work after all."""
    with _FAILURE_LOCK:
        _FAILURE_CACHE.pop(key, None)


def clear_boot_failures() -> int:
    """Forget every remembered failure; returns how many were dropped.

    Called when the user explicitly asks for a retry (``Reload MCP``): the memo
    exists to stop a boot re-discovering the same outage, never to argue with
    someone who just fixed the server.
    """
    with _FAILURE_LOCK:
        count = len(_FAILURE_CACHE)
        _FAILURE_CACHE.clear()
    return count


# How long the interactive OAuth handshake may stay pending before giving up.
OAUTH_FLOW_TIMEOUT = 300.0
# Refresh a stored access token this many seconds *before* its real expiry, so a
# token that is valid at check-time but would expire mid-request is refreshed
# pre-emptively instead of racing the server.
TOKEN_EXPIRY_LEEWAY = 60.0
# Placeholder redirect URI used by the non-interactive runtime provider — never
# actually visited (token refresh / reuse does not need it), but the metadata
# model requires at least one redirect URI.
_PLACEHOLDER_REDIRECT = "http://localhost/olano/mcp/oauth/callback"


class McpAuthorizationRequired(RuntimeError):
    """A server needs an interactive OAuth grant this process cannot perform.

    Raised by the non-interactive provider's redirect/callback fuse the moment
    the SDK decides it must send a human to a consent screen. It is a
    ``RuntimeError`` subclass because that is what it used to be raised as and
    several layers still catch that broadly — but having a *type* is what lets
    the boot path record the server as ``auth_required`` instead of ``error``.
    That distinction is load-bearing: only ``auth_required`` is exempt from the
    health sweep's reconnect backoff, so misfiling it as ``error`` had the
    runtime rebuilding an agent's whole graph on a 5-min-to-6-h timer,
    indefinitely, to re-ask a question only a human can answer.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(
            f"MCP server '{url}' requires OAuth but has not been authorized yet. "
            "Open the agent's MCP setup panel and complete 'Authorize'."
        )


# ── Transport normalization ────────────────────────────────────────────────


def stdio_mcp_blocked() -> bool:
    """True when stdio MCP servers must not launch on the HOST (managed posture).

    A host-launched stdio MCP server is an ARBITRARY local command run by the
    runtime process with its full environment + the agent's vault secrets — the
    same source/secret exfiltration vector as a raw shell (an agent or the
    dashboard MCP "test" endpoint could set
    ``command=bash args=[-c, "cat /app/..; env"]``). So on a managed deployment
    stdio never runs as a host child, exactly like host shell; remote
    (http/sse) MCP servers are unaffected.

    This is the *posture* half of the decision. Whether a managed box can run
    the server anyway — inside an ephemeral hardened sandbox container instead
    of the host — is :func:`_stdio_launch_decision` /
    :func:`plan_stdio_launch`, which require this predicate plus the
    ``sandbox_exec`` entitlement, a docker CLI, and the sandbox image.

    **Fails closed**: an unresolvable posture blocks host stdio rather than
    permitting it. This vector is equivalent to a host shell, so it gets the
    same treatment as ``require_sandboxed_exec`` — remote MCP still works, so a
    misresolved open box loses a feature, not its data.
    """
    try:
        from olano.deployment import enforcement as dep_enforcement

        return dep_enforcement.require_sandboxed_exec()
    except Exception:
        logger.exception("stdio MCP posture gate failed; blocking stdio MCP (fail closed)")
        return True


# ── stdio launch policy: host, sandboxed, or blocked ───────────────────────
#
# On a managed box the runtime process must never be the parent of an arbitrary
# stdio command (see stdio_mcp_blocked above) — but the command can run in an
# EPHEMERAL, hardened sibling container instead: the stdio child the MCP SDK
# spawns becomes `docker run --rm -i … <sandbox image> sh -c "exec <command>"`,
# so JSON-RPC flows over the docker CLI's attached stdio while the server runs
# with no product source, no vault, no runtime env and no docker socket. One
# session = one clean container; stdin-EOF (session close) ends it.

# Env the docker-CLI stdio child needs: enough to reach the daemon, nothing
# else. Deliberately NOT os.environ — the runtime's env is exactly what must
# not leak past this boundary.
_STDIO_CLI_ENV_PASSTHROUGH = (
    "PATH",
    "HOME",
    "TMPDIR",
    "DOCKER_HOST",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)

# A missing image is re-checked on this cadence so a `docker pull` fixes the
# block without a restart; a present image is remembered for good (images are
# pinned against pruning on managed boxes, and a wrong positive just fails the
# launch loudly).
_STDIO_IMAGE_RECHECK_SECONDS = 60.0
_STDIO_IMAGE_MEMO: dict[str, Any] = {"image": None, "ok": False, "ts": 0.0}

# Workspaces already handed to the sandbox uid this process lifetime — the
# recursive chown is idempotent but walks the tree, so pay it once per agent.
_STDIO_SANDBOX_PREPARED: set[tuple[str, str]] = set()

_DOCKER_NESTED_REASON = (
    "this entry launches the docker CLI, which does not exist inside the "
    "isolated MCP sandbox — use the server's remote (http/sse) endpoint or "
    "preset instead of its docker wrapper"
)


@dataclass(frozen=True)
class StdioInvocation:
    """What to actually spawn for a stdio MCP server: command, argv, env."""

    command: str
    args: list[str]
    env: dict[str, str]
    sandboxed: bool


@dataclass(frozen=True)
class StdioLaunchPlan:
    """Outcome of :func:`plan_stdio_launch` — an invocation, or why not."""

    invocation: StdioInvocation | None
    blocked_reason: str | None = None
    note: str | None = None


def _stdio_block_message(reason: str) -> str:
    return (
        "stdio MCP servers are disabled on this managed deployment "
        f"({reason}). Use a remote (http/sse) MCP server instead."
    )


def _stdio_sandbox_image() -> str:
    from olano.config.backend_config import _default_sandbox_image

    return _default_sandbox_image()


def _stdio_sandbox_image_present() -> bool:
    image = _stdio_sandbox_image()
    now = time.monotonic()
    if _STDIO_IMAGE_MEMO["image"] == image and (
        _STDIO_IMAGE_MEMO["ok"]
        or now - float(_STDIO_IMAGE_MEMO["ts"]) < _STDIO_IMAGE_RECHECK_SECONDS
    ):
        return bool(_STDIO_IMAGE_MEMO["ok"])
    ok = False
    try:
        proc = subprocess.run(
            [shutil.which("docker") or "docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=15,
        )
        ok = proc.returncode == 0
    except Exception:
        ok = False
    _STDIO_IMAGE_MEMO.update({"image": image, "ok": ok, "ts": now})
    return ok


def ephemeral_launch_mode() -> tuple[str, str | None]:
    """Where a one-shot child (a stdio MCP server, a connected git job) may run.

    ``(mode, blocked_reason)`` with mode ``host`` / ``sandboxed`` / ``blocked``
    — the one policy seam every ephemeral launch asks, so a sandboxed MCP
    server and a git job can never disagree about the posture. Fails closed.
    """
    return _stdio_launch_decision()


def _host_env() -> dict[str, str]:
    """The runtime's environment as a HOST-mode stdio server may inherit it:
    everything but the platform secrets (the vault key, the deployment and
    dashboard tokens, the gateway key, the account credits key). A stdio
    server is third-party code; a credential that spends the box's credits
    outside the runtime's metering must not ride along with PATH and HOME."""
    from olano.security.shell_guard import strip_platform_secrets

    return strip_platform_secrets(os.environ)


def _stdio_launch_decision() -> tuple[str, str | None]:
    """``(mode, blocked_reason)`` with mode ``host``/``sandboxed``/``blocked``.

    ``host`` only on open posture (byte-identical behaviour to before the
    sandbox rewrite existed). Managed posture requires the explicit
    ``features.sandbox_exec`` entitlement (the same control-plane switch that
    arms the agent shell sandbox — it doubles as the kill-switch here), a
    docker CLI, and the sandbox image. **Fails closed** like every posture
    gate in this module.
    """
    try:
        if not stdio_mcp_blocked():
            return "host", None
        from olano.deployment import enforcement as dep_enforcement

        if not dep_enforcement.sandbox_exec_enabled():
            return "blocked", "sandboxed execution is not enabled for this deployment"
        if not shutil.which("docker"):
            return "blocked", "the docker CLI is not available in the runtime"
        if not _stdio_sandbox_image_present():
            return (
                "blocked",
                f"the sandbox image '{_stdio_sandbox_image()}' is not installed",
            )
        return "sandboxed", None
    except Exception:
        logger.exception("stdio MCP launch-mode resolution failed; blocking (fail closed)")
        return "blocked", "the deployment's execution posture could not be resolved"


def stdio_launch_mode() -> str:
    """``host`` | ``sandboxed`` | ``blocked`` — see :func:`_stdio_launch_decision`."""
    return _stdio_launch_decision()[0]


def _is_docker_command(command: str) -> bool:
    return Path(command).name == "docker"


def stdio_unavailable_reason(srv: Any) -> str | None:
    """Summary-row message when a configured stdio server cannot launch here.

    ``None`` when the entry is not stdio-with-command, or when stdio can launch
    (host or sandboxed). Lets the boot path label a policy-blocked server
    ``blocked`` with the real reason instead of ``misconfigured``. Cheap — the
    underlying probes are memoized.
    """
    command = getattr(srv, "command", None)
    transport = normalize_transport(getattr(srv, "transport", None), command=command)
    if transport != "stdio" or not command:
        return None
    mode, reason = _stdio_launch_decision()
    if mode == "host":
        return None
    if mode == "sandboxed":
        if _is_docker_command(command):
            return _stdio_block_message(_DOCKER_NESTED_REASON)
        return None
    return _stdio_block_message(reason or "posture unresolved")


def _minimal_cli_env() -> dict[str, str]:
    return {
        key: value
        for key in _STDIO_CLI_ENV_PASSTHROUGH
        if (value := os.environ.get(key)) is not None
    }


def _agent_workspace_root(agent_name: str) -> Path:
    """The workspace the agent's MCP sandbox mounts (rw, at its host path too).

    Resolved from the agent's own config so per-agent workspaces mount
    correctly; falls back to the home default when the config cannot be loaded
    (a probe for a not-yet-saved agent) — the mount is a convenience for
    filesystem-flavoured MCP servers, not a correctness requirement.
    """
    try:
        from olano.config.loader import load_agent_config
        from olano.config.workspace import get_workspace

        cfg = load_agent_config(agent_name)
        return Path(get_workspace(cfg.workspace).root)
    except Exception:
        from olano.config.workspace import get_workspace

        return Path(get_workspace(None).root)


def _prepare_stdio_sandbox_paths(agent_name: str, workspace_root: Path) -> None:
    """Make the mounts usable by the container's uid-1000 user, once per agent.

    A filesystem-backend agent's workspace may never have met the sandbox
    chown (only the shell-sandbox boot path runs it), and the runtime seeds
    workspaces as root — without this an MCP server that writes gets
    "permission denied". Best-effort, mirrors what the shell-sandbox start
    does, via the builder's own helpers so there is one definition of the
    hand-off.
    """
    key = (agent_name, str(workspace_root))
    if key in _STDIO_SANDBOX_PREPARED:
        return
    try:
        from olano.config.home import get_olano_paths
        from olano.core import builder as core_builder

        core_builder.ensure_tool_layer_layout(get_olano_paths().root / "sandbox" / "tools")
        core_builder._ensure_sandbox_shared_dir(workspace_root, recursive=True)
    except Exception:
        logger.exception("stdio MCP sandbox path preparation failed (continuing)")
    _STDIO_SANDBOX_PREPARED.add(key)


def plan_stdio_launch(
    command: str, args: list[str], srv_env: dict[str, str] | None, agent_name: str
) -> StdioLaunchPlan:
    """Decide how (and whether) a stdio MCP server launches on this box.

    The single policy seam shared by the boot path
    (:func:`build_connection_params`) and the probe (:func:`probe_server`):

    - ``host`` (open posture): the command as-is, env exactly as it has always
      been (``os.environ`` + the agent's secret env).
    - ``sandboxed`` (managed + entitled + docker + image): the docker CLI as
      the stdio child, launching the command inside an ephemeral hardened
      sandbox container. Only the server's **declared** env crosses
      (:func:`agent_declared_env`) — never the agent's whole vault — as
      name-only ``-e`` flags resolved from the CLI child's env, never on
      argv; the runtime's ``os.environ`` is deliberately NOT part of the
      container env.
    - ``blocked``: ``invocation is None`` with a user-facing
      ``blocked_reason``. Fail-closed on any planning error.
    """
    mode, reason = _stdio_launch_decision()
    if mode == "host":
        return StdioLaunchPlan(
            StdioInvocation(
                command,
                list(args),
                {**_host_env(), **agent_secret_env(srv_env or {}, agent_name)},
                sandboxed=False,
            )
        )
    if mode != "sandboxed":
        return StdioLaunchPlan(
            None, blocked_reason=_stdio_block_message(reason or "posture unresolved")
        )
    if _is_docker_command(command):
        return StdioLaunchPlan(None, blocked_reason=_stdio_block_message(_DOCKER_NESTED_REASON))
    try:
        docker_cli = shutil.which("docker")
        if not docker_cli:
            raise RuntimeError("docker CLI disappeared after the readiness check")
        from olano.backends.lionclaw_docker import mcp_stdio_run_args, mcp_stdio_shell_script

        secret_env = agent_declared_env(srv_env or {}, agent_name)
        workspace_root = _agent_workspace_root(agent_name)
        _prepare_stdio_sandbox_paths(agent_name, workspace_root)
        image = _stdio_sandbox_image()
        run_args = mcp_stdio_run_args(
            agent_name=agent_name,
            workspace_root=workspace_root,
            secret_names=list(secret_env),
            image=image,
        )
        # run_args[0] is the bare "docker" token; the spawn uses the absolute
        # CLI path because the child env is the minimal allowlist, not a full
        # login environment with a trustworthy PATH.
        argv = [*run_args[1:], "sh", "-c", mcp_stdio_shell_script(command, list(args))]
        return StdioLaunchPlan(
            StdioInvocation(
                docker_cli,
                argv,
                {**_minimal_cli_env(), **secret_env},
                sandboxed=True,
            ),
            note=f"Launching in an isolated sandbox container (image {image}).",
        )
    except Exception:
        logger.exception("sandboxed stdio MCP launch planning failed; blocking (fail closed)")
        return StdioLaunchPlan(
            None,
            blocked_reason=_stdio_block_message(
                "the sandboxed launch could not be prepared; see the runtime log"
            ),
        )


def normalize_transport(transport: str | None, *, command: str | None = None) -> str:
    """Map a user-facing transport label to the MCP SDK transport name.

    ``http`` (and friends) → ``streamable_http``.  When *transport* is blank we
    infer ``stdio`` if a launch *command* is present, else ``sse``.
    """
    value = (transport or "").strip().lower()
    if not value:
        return "stdio" if command else "sse"
    # "auto" → resolve to a concrete remote default (streamable HTTP). The probe
    # path tries both http and sse before this fallback ever matters.
    if value == "auto":
        return "streamable_http"
    if value in {"http", "streamable_http", "streamable-http", "streamablehttp", "shttp"}:
        return "streamable_http"
    if value in {"ws", "websocket"}:
        return "websocket"
    if value in {"sse", "stdio"}:
        return value
    return value


def is_remote_transport(transport: str) -> bool:
    """True for network transports that can require OAuth (http / sse / ws)."""
    return normalize_transport(transport) in {"streamable_http", "sse", "websocket"}


def _remote_transport_candidates(raw_transport: str | None, normalized: str) -> list[str]:
    """Ordered transports to try when probing a remote server.

    For ``auto`` (or a blank request), try streamable HTTP first then SSE. For an
    explicit choice, try it first and fall back to the other so a mislabeled
    server still connects — the probe reports whichever actually worked.
    """
    raw = (raw_transport or "").strip().lower()
    if raw in {"auto", ""}:
        return ["streamable_http", "sse"]
    if normalized == "sse":
        return ["sse", "streamable_http"]
    if normalized == "streamable_http":
        return ["streamable_http", "sse"]
    return [normalized]


# ── OAuth token storage ────────────────────────────────────────────────────


def _home_root() -> Path:
    from olano.config.home import get_olano_paths

    return get_olano_paths().root


def oauth_dir() -> Path:
    from olano.security.private_files import private_dir

    return private_dir(_home_root() / "mcp_oauth")


def _url_slug(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _agent_slug(agent: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (agent or "agent"))
    return safe or "agent"


def token_store_path(agent: str, url: str) -> Path:
    """Per-(agent, server) token file under ``<HOME>/mcp_oauth/``."""
    return oauth_dir() / f"{_agent_slug(agent)}__{_url_slug(url)}.json"


def has_oauth_tokens(agent: str, url: str) -> bool:
    path = token_store_path(agent, url)
    if not path.exists():
        return False
    try:
        return bool(json.loads(path.read_text()).get("tokens"))
    except (OSError, json.JSONDecodeError):
        return False


def clear_oauth_tokens(agent: str, url: str) -> bool:
    path = token_store_path(agent, url)
    if path.exists():
        with contextlib.suppress(OSError):
            path.unlink()
        return True
    return False


def clear_stale_oauth_client(agent: str, url: str, redirect_uri: str) -> list[str] | None:
    """Drop a cached **dynamically-registered** client whose redirect URI no
    longer matches the current one.

    A DCR client is permanently bound to the ``redirect_uris`` it registered
    with. When the dashboard origin changes (e.g. a different port), the cached
    client_id becomes unusable for new authorizations — the provider rejects the
    mismatched ``redirect_uri`` (``invalid_redirect_uri``). Wiping the token file
    forces the interactive flow to re-register with the current redirect URI.

    Returns the stale registered URIs when something was cleared, else ``None``.
    Statically-configured clients have no ``client_info`` in the file (see
    :class:`FileTokenStorage`) and are left untouched.
    """
    path = token_store_path(agent, url)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    registered = [str(u) for u in ((data.get("client_info") or {}).get("redirect_uris") or [])]
    if not registered or redirect_uri in registered:
        return None
    with contextlib.suppress(OSError):
        path.unlink()
    return registered


class FileTokenStorage:
    """``mcp.client.auth.TokenStorage`` backed by a single JSON file.

    Stores the dynamically-registered client info and the OAuth tokens so the
    runtime can reconnect (and refresh) without re-prompting the user.

    When *static_client_id* is supplied the server does **not** support Dynamic
    Client Registration (e.g. HubSpot). ``get_client_info`` then returns the
    pre-registered credentials so the MCP SDK skips DCR entirely, and
    ``set_client_info`` becomes a no-op (there is nothing dynamic to persist —
    only the tokens are written). The config stays the source of truth, so
    rotating the secret in agent.json5 takes effect without clearing a cache.
    """

    def __init__(
        self,
        path: Path,
        *,
        static_client_id: str | None = None,
        static_client_secret: str | None = None,
        redirect_uris: list[str] | None = None,
        scope: str | None = None,
    ) -> None:
        self._path = path
        self._static_client_id = static_client_id or None
        self._static_client_secret = static_client_secret or None
        self._redirect_uris = redirect_uris or []
        self._scope = scope or None

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        from olano.security.private_files import write_private_text

        data["updated_at"] = time.time()
        # Owner-only from the first byte: the file holds OAuth tokens.
        write_private_text(self._path, json.dumps(data, indent=2))

    def remember(self, **fields: Any) -> None:
        """Persist auxiliary fields (e.g. a discovered ``token_endpoint``)
        alongside the tokens, leaving the tokens themselves untouched."""
        data = self._read()
        data.update(fields)
        self._write(data)

    async def get_tokens(self) -> Any:
        from mcp.shared.auth import OAuthToken

        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: Any) -> None:
        """Persist *tokens*, carrying forward fields a renewal may omit.

        RFC 6749 §5.1/§6 make ``refresh_token`` and ``scope`` **optional** in a
        token-refresh response, and omission means "keep the previous value".
        The MCP SDK ignores that: ``_handle_refresh_response`` hands us the
        renewal response verbatim, so a blind overwrite silently drops the
        refresh token for every provider that doesn't re-issue one on refresh
        (Zoho, Salesforce, Atlassian, …). The connection then keeps working
        until that fresh access token expires — typically an hour — and dies
        *permanently*, because :func:`oauth_token_state` now reads ``expired``
        with nothing to renew from and the boot path skips the server. That is
        the "it keeps losing the connection and I have to reconnect by hand"
        failure: the very first successful refresh disarms all the others.

        Carrying forward is therefore the default, not an option. The one case
        it gets wrong — re-authorizing the *same* server for the *same* agent
        with a **different account**, where the new grant returns no refresh
        token — is recoverable with Disconnect (which clears the file) followed
        by Authorize.
        """
        data = self._read()
        previous = data.get("tokens")
        payload = tokens.model_dump(exclude_none=True, mode="json")
        if isinstance(previous, dict):
            for key in ("refresh_token", "scope"):
                if not payload.get(key) and previous.get(key):
                    payload[key] = previous[key]
        data["tokens"] = payload
        self._write(data)

    async def get_client_info(self) -> Any:
        from mcp.shared.auth import OAuthClientInformationFull

        if self._static_client_id:
            # Pre-registered client → SDK skips DCR. token_endpoint_auth_method
            # must be set explicitly so the secret is actually sent on the token
            # exchange (a None method sends nothing). redirect_uris is required by
            # the model; the live redirect_uri used in the flow comes from
            # client_metadata, so this value is only for model validity.
            return OAuthClientInformationFull(
                client_id=self._static_client_id,
                client_secret=self._static_client_secret,
                redirect_uris=self._redirect_uris or [_PLACEHOLDER_REDIRECT],  # type: ignore[arg-type]
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method=(
                    "client_secret_post" if self._static_client_secret else "none"
                ),
                scope=self._scope,
            )
        data = self._read()
        raw = data.get("client_info")
        if not raw:
            return None
        info = OAuthClientInformationFull.model_validate(raw)
        # Self-heal a client that was registered (before the _client_metadata fix)
        # as a confidential ``client_secret_basic`` client and never completed a
        # token exchange — the token endpoint rejects it with "Client must not use
        # multiple authentication methods". Drop it so the SDK re-runs DCR and this
        # time registers a public PKCE client. Only a client with no tokens is
        # dropped; one that already obtained tokens is left untouched.
        if info.token_endpoint_auth_method == "client_secret_basic" and not data.get("tokens"):
            return None
        return info

    async def set_client_info(self, client_info: Any) -> None:
        if self._static_client_id:
            # Pre-registered client — nothing dynamic to persist.
            return
        data = self._read()
        data["client_info"] = client_info.model_dump(exclude_none=True, mode="json")
        self._write(data)


# ── OAuth provider construction ────────────────────────────────────────────


def _token_expiry_from_data(
    data: dict[str, Any], *, leeway: float = TOKEN_EXPIRY_LEEWAY
) -> float | None:
    tokens = data.get("tokens")
    expires_in = tokens.get("expires_in") if isinstance(tokens, dict) else None
    updated_at = data.get("updated_at")
    if expires_in is None or updated_at is None:
        return None
    try:
        return float(updated_at) + float(expires_in) - leeway
    except (TypeError, ValueError):
        return None


def _file_mtime(path: Path | None) -> float | None:
    """Modification time of *path*, or ``None`` when it is absent/unreadable.

    Used as a cheap "did someone else rewrite the tokens?" marker — see
    ``_ExpiryAwareOAuthProvider.async_auth_flow``.
    """
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _stored_token_expiry(path: Path, *, leeway: float = TOKEN_EXPIRY_LEEWAY) -> float | None:
    """Absolute epoch seconds when the stored access token should be considered
    expired (real expiry minus *leeway*), or ``None`` when it can't be determined.

    Derived from the on-disk ``updated_at`` (set when the token was written ≈ when
    it was issued) plus the token's ``expires_in``. This is the missing piece the
    MCP SDK never restores on load — see :class:`_ExpiryAwareOAuthProvider`.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _token_expiry_from_data(data, leeway=leeway)


def oauth_token_state(agent: str, url: str) -> dict[str, Any]:
    """Expiry-aware classification of the stored OAuth tokens for *(agent, url)*.

    :func:`has_oauth_tokens` only answers "does a token file exist", which lets a
    dead connection read as authorized (a 1-hour access token stored without a
    refresh token stays on disk forever). This weighs the stored tokens against
    their recorded expiry instead:

    - ``none`` — no token file / no tokens stored.
    - ``live`` — access token not yet expired (or expiry unknown — some
      providers omit ``expires_in``; absence of evidence isn't death).
    - ``refreshable`` — access token expired but a refresh token is stored; the
      next connect renews it silently, so the server still counts as authorized.
    - ``expired`` — access token expired and **no refresh token**: dead until
      the user re-authorizes.

    ``authorized`` is True for ``live``/``refreshable``. ``expires_at`` is the
    leeway-adjusted epoch expiry (None when unknown).
    """
    dead = {"state": "none", "authorized": False, "expires_at": None, "has_refresh_token": False}
    try:
        data = json.loads(token_store_path(agent, url).read_text())
    except (OSError, json.JSONDecodeError):
        return dead
    if not isinstance(data, dict):
        return dead
    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or not tokens:
        return dead
    has_refresh = bool(tokens.get("refresh_token"))
    expires_at = _token_expiry_from_data(data)
    if expires_at is None or time.time() < expires_at:
        state = "live"
    elif has_refresh:
        state = "refreshable"
    else:
        state = "expired"
    return {
        "state": state,
        "authorized": state != "expired",
        "expires_at": expires_at,
        "has_refresh_token": has_refresh,
    }


async def _discover_oauth_metadata(server_url: str) -> Any:
    """Best-effort discovery of the Authorization-Server metadata for *server_url*.

    Returns an ``OAuthMetadata`` (so the SDK refreshes against the real
    ``token_endpoint``, not its ``<base>/token`` fallback) or ``None`` on any
    failure. Mirrors the SDK's own discovery: protected-resource metadata →
    authorization server → AS metadata. Never raises.
    """
    import httpx
    from mcp.shared.auth import OAuthMetadata

    parsed = urlparse(server_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    as_base = base
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            with contextlib.suppress(Exception):
                prm = await client.get(f"{base}/.well-known/oauth-protected-resource")
                if prm.status_code == 200:
                    servers = prm.json().get("authorization_servers") or []
                    if servers:
                        as_base = str(servers[0]).rstrip("/")
            for suffix in (
                "/.well-known/oauth-authorization-server",
                "/.well-known/openid-configuration",
            ):
                with contextlib.suppress(Exception):
                    resp = await client.get(f"{as_base}{suffix}")
                    if resp.status_code == 200:
                        return OAuthMetadata.model_validate(resp.json())
    except Exception:
        return None
    return None


# ── Proactive token maintenance ────────────────────────────────────────────
#
# Everything above renews a token *lazily*: the SDK refreshes on whichever
# request first finds one expired. That covers a busy connection and does
# nothing for an idle one — an agent whose Zoho tools go untouched overnight
# has an hour-old access token by morning, and providers that expire refresh
# tokens on disuse eventually leave nothing to renew from. It is also invisible
# to a restart: :func:`_build_mcp_tools` *skips* a server whose stored token
# reads ``expired``, so the tools stay gone for the whole process lifetime.
#
# The health loop calls the helpers below on an interval to keep the stored
# grant alive whether or not anyone is using it. They talk to the OAuth token
# endpoint directly rather than opening an MCP session, so a renewal still
# happens while the MCP server itself is down.

# Renew once the access token has less than this much life left. Comfortably
# longer than a health tick so a token is never first *noticed* after expiry.
TOKEN_REFRESH_WINDOW = 900.0


def token_needs_refresh(agent: str, url: str, *, window: float = TOKEN_REFRESH_WINDOW) -> bool:
    """True when *(agent, url)* holds a renewable token worth renewing now.

    False when there is nothing to renew from (no refresh token), when the
    expiry is unknown (some providers omit ``expires_in`` — absence of evidence
    is not an expiry to schedule against), and when the token still has more
    than *window* seconds of life.
    """
    state = oauth_token_state(agent, url)
    if not state["has_refresh_token"]:
        return False
    if state["state"] == "refreshable":
        return True  # already expired, renewable
    expires_at = state["expires_at"]
    return expires_at is not None and time.time() >= expires_at - window


async def _token_endpoint(url: str, data: dict[str, Any]) -> tuple[str, bool]:
    """Resolve the OAuth token endpoint for *url*; ``(endpoint, discovered)``.

    Prefers the value cached in the token file, then live AS-metadata
    discovery, then the SDK's own ``<origin>/token`` fallback. The cached value
    is what keeps a periodic renewal from re-running discovery every tick.
    """
    cached = data.get("token_endpoint")
    if isinstance(cached, str) and cached:
        return cached, False
    meta = await _discover_oauth_metadata(url)
    endpoint = str(getattr(meta, "token_endpoint", "") or "") if meta is not None else ""
    if endpoint:
        return endpoint, True
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/token", False


#: Token-endpoint responses that mean "this URL is not a token endpoint" rather
#: than "your grant is refused". RFC 6749 §5.2 spends a whole section on how a
#: refusal is expressed — ``400`` with an ``error`` body — and none of these is
#: it: the request reached a server that has nothing to say about OAuth at this
#: path. Treating them as a refusal throws away a working refresh token.
_NO_TOKEN_ENDPOINT_STATUSES = frozenset({404, 405, 410})


def _forget_token_endpoint(path: Path | None) -> None:
    """Drop a cached ``token_endpoint`` that turned out not to be one.

    The cache in the token file exists to keep periodic renewals from
    re-running discovery every tick — which also means a wrong value is sticky
    for the life of the grant. Best-effort: a token file we cannot rewrite
    still renews, it just re-pays discovery.
    """
    if path is None:
        return
    with contextlib.suppress(OSError, json.JSONDecodeError):
        data = json.loads(path.read_text())
        if isinstance(data, dict) and data.pop("token_endpoint", None) is not None:
            path.write_text(json.dumps(data, indent=2))


async def refresh_oauth_token(
    agent: str,
    url: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Renew *(agent, url)*'s access token from its stored refresh token.

    Non-interactive and side-effect-free on failure: the stored grant is only
    replaced by a successful renewal, and a rejection never deletes it (the
    dashboard still shows what is there, and the user decides whether to
    re-authorize). Returns ``{"ok", "status", "message", "expires_at"}`` with
    *status* one of ``refreshed`` / ``not_needed`` / ``no_refresh_token`` /
    ``no_client`` / ``rejected`` / ``unreachable`` / ``invalid_response``.

    *client_id* / *client_secret* override the stored client info for servers
    that don't support Dynamic Client Registration (the config is the source of
    truth for those — see :class:`FileTokenStorage`).
    """
    import httpx
    from mcp.shared.auth import OAuthToken

    def _result(ok: bool, status: str, message: str, expires_at: float | None = None) -> dict:
        return {"ok": ok, "status": status, "message": message, "expires_at": expires_at}

    path = token_store_path(agent, url)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    tokens = data.get("tokens")
    refresh_token = tokens.get("refresh_token") if isinstance(tokens, dict) else None
    if not refresh_token:
        return _result(False, "no_refresh_token", "No refresh token stored.")

    raw_client = data.get("client_info")
    stored_client: dict[str, Any] = raw_client if isinstance(raw_client, dict) else {}
    resolved_id = client_id or stored_client.get("client_id")
    resolved_secret = client_secret or stored_client.get("client_secret")
    if not resolved_id:
        return _result(False, "no_client", "No OAuth client is registered for this server.")

    endpoint, discovered = await _token_endpoint(url, data)
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": resolved_id,
    }
    if resolved_secret:
        # client_secret_post — mirrors the SDK's `prepare_token_auth` for a
        # confidential client. Public (PKCE) clients send client_id alone.
        body["client_secret"] = resolved_secret

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.post(
                endpoint,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as exc:
        return _result(False, "unreachable", f"Token endpoint unreachable: {_readable_exc(exc)}")

    if response.status_code != 200:
        detail = _strip_html(response.text)[:200]
        return _result(
            False,
            "rejected",
            f"Token endpoint returned {response.status_code}"
            + (f": {detail}" if detail else "")
            + ". Re-authorize this server to restore access.",
        )

    try:
        renewed = OAuthToken.model_validate_json(response.content)
    except Exception as exc:
        return _result(False, "invalid_response", f"Unparseable token response: {exc}")

    storage = FileTokenStorage(path)
    if discovered:
        # Cache the endpoint so later renewals skip discovery. Persisted before
        # the tokens so `set_tokens` owns the final `updated_at` — that stamp is
        # what the expiry of the token we are about to write is computed from.
        storage.remember(token_endpoint=endpoint)
    await storage.set_tokens(renewed)
    state = oauth_token_state(agent, url)
    return _result(True, "refreshed", "Access token renewed.", state["expires_at"])


def _make_expiry_aware_provider_cls() -> Any:
    """Build the ``OAuthClientProvider`` subclass lazily (the import is heavy and
    only needed when an OAuth server is actually configured)."""
    from mcp.client.auth import OAuthClientProvider

    class _ExpiryAwareOAuthProvider(OAuthClientProvider):  # type: ignore[misc,valid-type]
        """``OAuthClientProvider`` that restores token expiry + AS metadata from
        storage on init.

        Works around mcp<=1.26.0: ``OAuthContext._initialize`` loads the stored
        token but never recomputes ``token_expiry_time``, so an already-expired
        access token reports ``is_token_valid() == True`` → the SDK sends it →
        the server returns 401 → the SDK runs a *full* re-authorization (which a
        non-interactive provider cannot complete) instead of a refresh. Restoring
        the real expiry makes the SDK take its (working) refresh path; pre-seeding
        the AS metadata makes that refresh hit the real token endpoint.
        """

        def __init__(
            self,
            *args: Any,
            lc_expires_at: float | None = None,
            lc_token_path: Path | None = None,
            **kwargs: Any,
        ) -> None:
            super().__init__(*args, **kwargs)
            self._lc_expires_at = lc_expires_at
            self._lc_token_path = lc_token_path
            self._lc_loaded_mtime = _file_mtime(lc_token_path)

        async def _initialize(self) -> None:  # type: ignore[override]
            self._lc_loaded_mtime = _file_mtime(self._lc_token_path)
            await super()._initialize()
            ctx = self.context
            # Re-derived per initialization, not taken from construction time:
            # a re-init happens precisely because the file changed, and pinning
            # the *old* expiry onto a freshly-loaded token would mark it dead on
            # arrival and re-trigger the reload forever.
            expires_at = (
                _stored_token_expiry(self._lc_token_path)
                if self._lc_token_path is not None
                else self._lc_expires_at
            )
            if expires_at is not None and ctx.current_tokens is not None:
                ctx.token_expiry_time = expires_at
            # Whenever a future refresh is possible, ensure the SDK knows the real
            # token endpoint. Done once per provider (init is guarded), so the cost
            # is one metadata fetch — cheap next to the connection itself.
            if ctx.oauth_metadata is None and ctx.can_refresh_token():
                meta = await _discover_oauth_metadata(ctx.server_url)
                if meta is not None:
                    ctx.oauth_metadata = meta

        async def async_auth_flow(self, request: Any) -> Any:  # type: ignore[override]
            """Reload the token file when our copy expired *and* it changed on disk.

            The SDK loads tokens exactly once (``_initialized``) and then treats
            its in-memory copy as the truth for the life of the process. The file
            underneath is shared, though: the health loop renews it on a timer, a
            dashboard/CLI *Authorize* replaces it, another worker may have
            rotated it. A long-lived agent otherwise keeps refreshing from a copy
            that can be hours stale — and against a provider that rotates refresh
            tokens, stale means *rejected*, which is a re-authorization prompt a
            runtime agent cannot answer.

            Both halves of the condition matter. Only reloading when our own
            token has expired keeps the common path untouched (one ``stat`` per
            renewal, never per request); requiring the file to have actually
            moved keeps a genuinely revoked grant failing fast instead of
            re-reading and re-refreshing the same dead token on every call.
            """
            if (
                self._initialized
                and not self.context.is_token_valid()
                and _file_mtime(self._lc_token_path) != self._lc_loaded_mtime
            ):
                self._initialized = False
            # Manual delegation: httpx auth flows are *bidirectional* generators
            # (the caller sends each response back in), so `async for` would
            # silently drop the responses the SDK needs to drive its handshake.
            flow = super().async_auth_flow(request)
            try:
                outgoing = await flow.__anext__()
                while True:
                    response = yield outgoing
                    outgoing = await flow.asend(response)
            except StopAsyncIteration:
                return
            finally:
                # Never let cleanup raise. If the *outer* flow is abandoned
                # rather than driven to completion, the loop's async-generator
                # finalizer closes it from a different task, and the SDK's
                # `async with self.context.lock` then fails to release ("current
                # task is not holding this lock") — noise from a request nobody
                # is waiting on any more.
                with contextlib.suppress(Exception):
                    await flow.aclose()

        async def _handle_refresh_response(self, response: Any) -> bool:  # type: ignore[override]
            """Renewal handling that survives the two ways the SDK's version
            turns a recoverable hiccup into "please re-authorize by hand".

            On success: the SDK replaces ``current_tokens`` with the renewal
            response, so a provider that omits ``refresh_token`` on refresh
            leaves the *in-memory* context unable to renew again (the on-disk
            copy is repaired by :meth:`FileTokenStorage.set_tokens`, but nothing
            re-reads it while the process lives). Carry it forward in memory too.

            On failure: the SDK calls ``clear_tokens()`` for *any* non-200 — a
            502 from the token endpoint, or a lost race with a concurrent
            refresh that rotated the token underneath us — and the very next
            request then needs an interactive re-authorization that a runtime
            agent cannot perform. Those two cases keep their tokens and return
            False, which makes the SDK re-initialize from storage on the next
            request: the retry either succeeds or picks up whoever rotated it.
            A genuine rejection (``invalid_grant`` with the on-disk token still
            the one we used) still clears, so a revoked grant fails fast.

            **A 404/405/410 is not a rejection at all.** RFC 6749 §5.2 requires
            a refused grant to come back ``400`` with an ``invalid_grant`` body;
            "not found" / "method not allowed" / "gone" says the POST never
            reached a token endpoint. That happens whenever the endpoint we hold is wrong
            rather than the grant: metadata discovery failed at
            :meth:`_initialize` (transient DNS/5xx on the well-known document,
            a server that publishes it somewhere non-standard) and the SDK fell
            back to guessing ``<origin>/token`` — which 404s on every provider
            that does not happen to serve one there — or the AS moved its
            endpoint after we cached it. Clearing on that destroys a refresh
            token that was never refused, and the operator is told to
            re-authorize a grant that was fine. So it keeps the tokens and
            drops the endpoint we mis-sent to: returning False makes the SDK
            set ``_initialized = False``, and our ``_initialize`` then
            re-discovers the real token endpoint, so the next attempt renews
            against it with nobody in the loop.
            """
            ctx = self.context
            if response.status_code == 200:
                previous = ctx.current_tokens
                ok = await super()._handle_refresh_response(response)
                if (
                    ok
                    and previous is not None
                    and ctx.current_tokens is not None
                    and not ctx.current_tokens.refresh_token
                    and previous.refresh_token
                ):
                    ctx.current_tokens.refresh_token = previous.refresh_token
                return ok

            used = getattr(ctx.current_tokens, "refresh_token", None)
            stored = None
            with contextlib.suppress(Exception):
                stored = getattr(await ctx.storage.get_tokens(), "refresh_token", None)
            rotated = bool(stored and used and stored != used)
            transient = response.status_code >= 500 or response.status_code in (408, 429)
            misdirected = response.status_code in _NO_TOKEN_ENDPOINT_STATUSES
            if rotated or transient or misdirected:
                if misdirected:
                    # Forget the endpoint that answered "not found" so the
                    # re-initialization this False triggers rediscovers it
                    # instead of re-sending to the same wrong URL forever.
                    ctx.oauth_metadata = None
                    _forget_token_endpoint(self._lc_token_path)
                reason = (
                    "rotated concurrently"
                    if rotated
                    else "transient"
                    if transient
                    else "no token endpoint at that URL — rediscovering"
                )
                logger.warning(
                    "MCP token refresh for %s failed with %s (%s) — keeping stored "
                    "credentials; the next request reloads them and retries.",
                    ctx.server_url,
                    response.status_code,
                    reason,
                )
                return False
            logger.warning(
                "MCP token refresh for %s rejected with %s — re-authorization required.",
                ctx.server_url,
                response.status_code,
            )
            ctx.clear_tokens()
            return False

    return _ExpiryAwareOAuthProvider


def _client_metadata(redirect_uri: str, scope: str | None) -> Any:
    from mcp.shared.auth import OAuthClientMetadata

    return OAuthClientMetadata(
        client_name="Olano",
        redirect_uris=[redirect_uri],  # type: ignore[list-item]  # pydantic coerces str → AnyUrl
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scope or None,
        # Register as a PUBLIC (PKCE-only) client. When this is omitted, RFC 7591
        # says the authorization server defaults to ``client_secret_basic`` and
        # issues a secret — and the mcp<=1.26.0 token-exchange builder then sends
        # BOTH an ``Authorization: Basic`` header *and* ``client_id`` in the body
        # (oauth2.py:_exchange_token_authorization_code always adds the latter).
        # Strict servers (Notion) enforce RFC 6749 §2.3 ("the client MUST NOT use
        # more than one authentication method") and reject the exchange with
        # 400 "Client must not use multiple authentication methods". A public client
        # authenticates the token exchange with PKCE alone — one method — which every
        # DCR-capable MCP server accepts. Servers that need a pre-registered secret
        # use the static-client path instead (FileTokenStorage(static_client_id=…)).
        token_endpoint_auth_method="none",
    )


# Provider-specific extra authorize params required to obtain a **refresh
# token**, keyed by a substring matched against the MCP server hostname. The MCP
# SDK hardcodes the authorize query (oauth2.py:_perform_authorization_code_grant)
# and never asks for offline access, so we inject these before the URL reaches
# the browser. Zoho's OAuth v2 only returns a refresh token when the authorize
# request carries ``access_type=offline``, and it re-issues one on a *second*
# authorization for an already-consented account only when ``prompt=consent``
# forces the consent screen again — without it Zoho returns an access-token-only
# response and the connection dies ~1h later with nothing to refresh from.
_OFFLINE_AUTHORIZE_PARAMS: dict[str, dict[str, str]] = {
    "zoho": {"access_type": "offline", "prompt": "consent"},
}


def _offline_authorize_params(server_url: str) -> dict[str, str]:
    host = (urlparse(server_url).hostname or "").lower()
    for needle, params in _OFFLINE_AUTHORIZE_PARAMS.items():
        if needle in host:
            return params
    return {}


def _augment_authorize_url(auth_url: str, server_url: str) -> str:
    """Add provider-specific offline/consent params to an authorize URL.

    Only adds keys that aren't already present (never clobbers state/PKCE or an
    explicit value), so it is safe to apply to any redirect handler.
    """
    extra = _offline_authorize_params(server_url)
    if not extra:
        return auth_url
    parsed = urlparse(auth_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in extra.items():
        query.setdefault(key, [value])
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _wrap_redirect_for_offline_access(
    handler: Callable[[str], Awaitable[None]], server_url: str
) -> Callable[[str], Awaitable[None]]:
    async def _wrapped(auth_url: str) -> None:
        await handler(_augment_authorize_url(auth_url, server_url))

    return _wrapped


def make_oauth_provider(
    agent: str,
    url: str,
    *,
    redirect_uri: str | None = None,
    scope: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    redirect_handler: Callable[[str], Awaitable[None]] | None = None,
    callback_handler: Callable[[], Awaitable[tuple[str, str | None]]] | None = None,
    interactive: bool = False,
) -> Any:
    """Build an ``OAuthClientProvider`` bound to this agent+server's token file.

    With *interactive* False (the runtime path) and no handlers supplied, the
    provider raises a clear error the moment it would need user interaction — so
    a not-yet-authorized server fails fast instead of hanging the agent boot.

    When *client_id* is supplied the server does not support Dynamic Client
    Registration; the pre-registered credentials are used instead of DCR.
    """
    token_path = token_store_path(agent, url)
    storage = FileTokenStorage(
        token_path,
        static_client_id=client_id,
        static_client_secret=client_secret,
        redirect_uris=[redirect_uri] if redirect_uri else None,
        scope=scope,
    )

    if not interactive and redirect_handler is None and callback_handler is None:

        async def _needs_auth(*_args: Any) -> Any:
            raise McpAuthorizationRequired(url)

        redirect_handler = _needs_auth  # type: ignore[assignment]
        callback_handler = _needs_auth  # type: ignore[assignment]

    # Inject provider-specific offline/consent params (e.g. Zoho) into whatever
    # authorize URL the SDK generates — this is the single chokepoint every
    # OAuth flow (interactive setup, CLI, probe, runtime) passes through.
    if redirect_handler is not None:
        redirect_handler = _wrap_redirect_for_offline_access(redirect_handler, url)

    provider_cls = _make_expiry_aware_provider_cls()
    return provider_cls(
        server_url=url,
        client_metadata=_client_metadata(redirect_uri or _PLACEHOLDER_REDIRECT, scope),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=OAUTH_FLOW_TIMEOUT,
        lc_expires_at=_stored_token_expiry(token_path),
        lc_token_path=token_path,
    )


# ── Connection params (runtime build path) ─────────────────────────────────


_VAULT_REF = re.compile(r"\$\{vault:([^}]+)\}")


def _resolve_agent_ref(value: str, agent: str, vault: Any) -> str:
    """Resolve ``${vault:NAME}`` refs in *value* with per-agent precedence."""
    if vault is None or "${vault:" not in value:
        return value
    from olano.vault.reserved import is_runtime_secret

    def repl(match: re.Match) -> str:
        if is_runtime_secret(match.group(1)):
            # The deployment's own credentials never travel to an MCP server,
            # in a header or an env block. Verbatim, like any other miss, so
            # the refusal is visible as a 401 naming the ref.
            logger.warning(
                "MCP: ${vault:%s} is a runtime-managed entry and is not resolved for agent %s",
                match.group(1),
                agent or "<none>",
            )
            return str(match.group(0))
        try:
            return str(vault.get(match.group(1), agent=agent or None, touch=False))
        except Exception:
            # Left verbatim by design (a blanked credential reads as a server
            # bug) — but say so, or the resulting 401 arrives with no thread to
            # pull. Name only, never a value.
            logger.warning(
                "MCP: unresolved ${vault:%s} for agent %s — sent verbatim",
                match.group(1),
                agent or "<none>",
            )
            return str(match.group(0))

    return _VAULT_REF.sub(repl, value)


def agent_declared_env(srv_env: dict[str, str] | None, agent_name: str) -> dict[str, str]:
    """Exactly the server's declared ``env`` block, refs resolved — for the sandbox.

    The sandboxed launch forwards **only what the server declares**: an
    arbitrary directory package must not receive every secret the agent can
    see (which is what :func:`agent_secret_env` hands a host launch on the
    operator's own box). ``${vault:NAME}`` resolves through ``get_credential``
    under the agent's identity — config.env overlay → connector broker →
    vault override → common → os.environ — so a token connected in the
    dashboard reaches a stdio server the same way it reaches a toolkit. An
    unresolvable ref stays verbatim (a blanked credential reads as a server
    bug; a literal 401s with a name to pull on).
    """
    if not srv_env:
        return {}
    cfg_env: dict[str, str] | None = None
    try:
        from olano.config.loader import load_agent_config

        cfg_env = dict(load_agent_config(agent_name).env or {})
    except Exception:
        cfg_env = None
    from olano.tools._credentials import agent_runtime_context, get_credential

    def repl(match: re.Match) -> str:
        value = get_credential(match.group(1))
        if value:
            return value
        logger.warning(
            "MCP: unresolved ${vault:%s} for agent %s — sent verbatim",
            match.group(1),
            agent_name or "<none>",
        )
        return str(match.group(0))

    with agent_runtime_context(agent_name, cfg_env):
        return {key: _VAULT_REF.sub(repl, str(raw)) for key, raw in srv_env.items()}


def agent_secret_env(srv_env: dict[str, str] | None, agent_name: str) -> dict[str, str]:
    """Per-agent secret additions for a stdio MCP child process.

    Returns the running agent's resolved vault credentials (common ∪ override,
    override wins) plus the server's own ``${vault:...}``-resolved ``env`` block.
    How it merges depends on the launch mode (:func:`plan_stdio_launch`): a
    **host** launch (open posture) merges this **over** ``os.environ`` so the
    child gets a working environment + the agent's secrets, while the parent
    process never has to carry those secrets in its own env (see env hardening
    in vault/runtime). A **sandboxed** launch (managed posture) deliberately
    does NOT include ``os.environ`` — the container image supplies the working
    environment, and the runtime's env is exactly what must not cross that
    boundary — so there the merge base is the minimal docker-CLI allowlist.
    Do not "fix" the sandboxed path back to an os.environ merge.
    """
    from olano.vault.resolver import get_vault
    from olano.vault.runtime import resolve_agent_credentials

    extra: dict[str, str] = dict(resolve_agent_credentials(agent_name))
    if srv_env:
        vault = get_vault()
        for key, raw in srv_env.items():
            extra[key] = _resolve_agent_ref(str(raw), agent_name, vault)
    return extra


def agent_secret_headers(srv_headers: dict[str, str] | None, agent_name: str) -> dict[str, str]:
    """Resolve ``${vault:NAME}`` refs in a remote server's static headers.

    The ``env`` block has always resolved vault refs (stdio children get them
    through :func:`agent_secret_env`); ``headers`` did not, so the single most
    common way to authenticate a remote MCP server — ``Authorization: Bearer
    <key>`` — forced the key to be a **literal in agent.json5**, out of the
    vault and into a file that gets read, copied and committed. Now both blocks
    resolve the same refs with the same per-agent precedence.

    A value with no ref is passed through untouched, and an unresolvable ref is
    left verbatim rather than blanked, so a typo reaches the server as itself
    and shows up as an auth error naming the header — not as a silently empty
    credential.
    """
    if not srv_headers:
        return {}
    if not any("${vault:" in str(v) for v in srv_headers.values()):
        return dict(srv_headers)  # nothing to resolve — never open the vault

    try:
        from olano.vault.resolver import get_vault

        vault = get_vault()
        return {k: _resolve_agent_ref(str(v), agent_name, vault) for k, v in srv_headers.items()}
    except Exception:
        # Fail open to the documented "unresolvable ⇒ verbatim" behaviour: this
        # runs on the agent-boot path (build_connection_params has no guard at
        # its call site), and a vault hiccup must cost one server a 401, never
        # the whole boot.
        logger.warning("MCP: vault unavailable while resolving headers — sent verbatim")
        return dict(srv_headers)


def resolve_client_credentials(entry: Any, agent_name: str) -> tuple[str | None, str | None]:
    """``(client_id, client_secret)`` for an OAuth server entry, refs resolved.

    *entry* is a config dict or an ``MCPServerConfig``. The secret is stored in
    the vault and referenced from the entry as ``${vault:NAME}``
    (``config/mcp_secrets.py``), so every consumer — the runtime connect, the
    probe, the interactive flow, the health sweep's refresh — resolves it here
    with the same per-agent precedence ``headers`` get. A literal passes
    through untouched (a config written before the vaulting, or a box whose
    vault was locked at save time). An unresolvable reference is sent verbatim
    and logged, so the provider's ``invalid_client`` arrives with a name to
    pull on instead of a silently empty secret.
    """
    if isinstance(entry, dict):
        client_id = entry.get("oauth_client_id")
        secret = entry.get("oauth_client_secret")
    else:
        client_id = getattr(entry, "oauth_client_id", None)
        secret = getattr(entry, "oauth_client_secret", None)
    client_id = str(client_id).strip() if client_id else None
    secret = str(secret).strip() if secret else None
    if secret and "${vault:" in secret:
        vault = None
        try:
            from olano.vault.resolver import get_vault

            vault = get_vault()
        except Exception:  # pragma: no cover - defensive
            vault = None
        if vault is None:
            logger.warning(
                "MCP: vault locked — the client secret for agent %s stays an unresolved "
                "reference (%s)",
                agent_name or "<none>",
                secret,
            )
        else:
            secret = _resolve_agent_ref(secret, agent_name, vault)
    return client_id or None, secret or None


def build_connection_params(srv: Any, agent_name: str) -> dict[str, Any] | None:
    """Return the per-server dict for ``MultiServerMCPClient``.

    *srv* is an ``MCPServerConfig`` (or any object exposing the same fields).
    Returns ``None`` when the server cannot be connected (e.g. stdio without a
    command), so the caller can skip it.
    """
    transport = normalize_transport(
        getattr(srv, "transport", None), command=getattr(srv, "command", None)
    )

    if transport == "stdio":
        command = getattr(srv, "command", None)
        if not command:
            return None
        plan = plan_stdio_launch(
            command,
            list(getattr(srv, "args", []) or []),
            getattr(srv, "env", {}) or {},
            agent_name,
        )
        if plan.invocation is None:
            logger.warning(
                "stdio MCP server %r skipped — %s",
                getattr(srv, "name", command),
                plan.blocked_reason,
            )
            return None
        inv = plan.invocation
        # Host mode: the command as-is, per-agent secret env merged over the
        # (secret-free) process env. Sandboxed mode (managed posture): the
        # docker CLI launching the command in an ephemeral hardened container,
        # with a minimal CLI env instead of os.environ — see plan_stdio_launch.
        return {
            "command": inv.command,
            "args": inv.args,
            "transport": "stdio",
            "env": inv.env,
        }

    params: dict[str, Any] = {"url": srv.url, "transport": transport}
    headers = getattr(srv, "headers", None) or {}
    if headers:
        params["headers"] = agent_secret_headers(headers, agent_name)
    # Attach stored tokens even when the entry says auth:none — they only exist
    # because an Authorize flow completed for this (agent, url), and connecting
    # without them re-hits the 401 that flow was run to fix.
    if (getattr(srv, "auth", "none") or "none") == "oauth" or has_oauth_tokens(agent_name, srv.url):
        client_id, client_secret = resolve_client_credentials(srv, agent_name)
        params["auth"] = make_oauth_provider(
            agent_name,
            srv.url,
            scope=getattr(srv, "oauth_scope", None),
            client_id=client_id,
            client_secret=client_secret,
            interactive=False,
        )
    return params


# ── Low-level connect + list tools (all transports) ────────────────────────


async def _open_transport(
    url: str,
    transport: str,
    *,
    auth: Any = None,
    headers: dict[str, str] | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    errlog: Any = None,
    stdio_policy_checked: bool = False,
) -> Any:
    """Return an async-context-manager for the requested transport.

    *errlog* (stdio only) is a real file the child's stderr is redirected to,
    so the probe can echo the child's own error output instead of losing it to
    the parent process's stderr.

    *stdio_policy_checked* is the defense-in-depth backstop: it may be True
    only when the caller routed command/args/env through
    :func:`plan_stdio_launch` (which on managed posture rewrites the launch
    into an ephemeral sandbox container, or blocks it). A raw stdio launch on
    a managed box is refused here regardless of what the caller intended.
    """
    if transport == "stdio":
        if not stdio_policy_checked:
            mode, reason = _stdio_launch_decision()
            if mode != "host":
                raise RuntimeError(
                    _stdio_block_message(
                        reason or "raw stdio launches must go through the sandbox rewrite"
                    )
                )
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=command or "",
            args=list(args or []),
            # The planned env is complete (host mode carries os.environ itself;
            # sandboxed mode deliberately does not) — merging os.environ here
            # again would leak the runtime env into a sandboxed launch's CLI
            # child. Unplanned callers (stdio_policy_checked=False on an open
            # box) still get the historical inherit-plus-overrides behaviour.
            env=(dict(env or {}) if stdio_policy_checked else {**_host_env(), **(env or {})}),
        )
        if errlog is not None:
            return stdio_client(params, errlog=errlog)
        return stdio_client(params)
    if transport == "sse":
        from mcp.client.sse import sse_client

        return sse_client(url, headers=headers, auth=auth)
    # default: streamable_http
    from mcp.client.streamable_http import streamablehttp_client

    return streamablehttp_client(url, headers=headers, auth=auth)


async def _connect_and_list_tools(
    url: str,
    transport: str,
    *,
    auth: Any = None,
    headers: dict[str, str] | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    errlog: Any = None,
    log: Callable[[str, str], None] | None = None,
    stdio_policy_checked: bool = False,
) -> list[Any]:
    from mcp import ClientSession

    def _log(level: str, msg: str) -> None:
        if log:
            log(level, msg)

    cm = await _open_transport(
        url,
        transport,
        auth=auth,
        headers=headers,
        command=command,
        args=args,
        env=env,
        errlog=errlog,
        stdio_policy_checked=stdio_policy_checked,
    )
    async with cm as streams:
        read_stream, write_stream = streams[0], streams[1]
        _log("info", "Transport stream opened; initializing MCP session…")
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            server_name = getattr(getattr(init, "serverInfo", None), "name", None)
            if server_name:
                _log("info", f"Server identified: {server_name}")
            _log("info", "Session initialized; listing tools…")
            result = await session.list_tools()
            return list(result.tools)


# ── OAuth requirement detection ────────────────────────────────────────────


async def _detect_oauth_required(
    url: str, transport: str, headers: dict[str, str] | None
) -> tuple[bool, str | None]:
    """Best-effort: does an unauthenticated request return HTTP 401?

    Returns ``(needs_oauth, www_authenticate_header)``.  Never raises.
    """
    import httpx

    merged = {**(headers or {})}
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            if transport == "sse":
                resp = await client.get(url, headers={"Accept": "text/event-stream", **merged})
            else:
                payload = {
                    "jsonrpc": "2.0",
                    "id": "olano-auth-probe",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "olano", "version": "1.0"},
                    },
                }
                resp = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        **merged,
                    },
                )
        if resp.status_code in (401, 403):
            return True, resp.headers.get("WWW-Authenticate")
        return False, None
    except Exception:  # network errors are reported by the main connect path
        return False, None


def _flatten_exc(exc: BaseException) -> list[BaseException]:
    """Flatten anyio/TaskGroup ``ExceptionGroup``s into their leaf exceptions.

    ``streamablehttp_client`` / ``sse_client`` run inside an anyio task group, so
    a plain HTTP 401 surfaces wrapped as
    ``ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)`` — an
    unhelpful message that also hides the real cause. We unwrap it here.
    """
    leaves: list[BaseException] = []
    inner = getattr(exc, "exceptions", None)
    if isinstance(exc, BaseExceptionGroup) and inner:
        for sub in inner:
            leaves.extend(_flatten_exc(sub))
    else:
        leaves.append(exc)
    return leaves


def _exc_indicates_auth(exc: BaseException) -> bool:
    """Whether a connection failure looks like a missing-authorization error."""
    import httpx

    for leaf in _flatten_exc(exc):
        resp = getattr(leaf, "response", None)
        if isinstance(leaf, httpx.HTTPStatusError) and getattr(resp, "status_code", None) in (
            401,
            403,
        ):
            return True
        text = f"{type(leaf).__name__}: {leaf}".lower()
        if any(s in text for s in ("401", "403", "unauthorized", "forbidden", "www-authenticate")):
            return True
    return False


def needs_authorization(exc: BaseException) -> bool:
    """Whether *exc* is the non-interactive fuse asking for a human.

    Matches on the type, not the message. The connect path runs inside an anyio
    task group, so the fuse arrives wrapped in an ``ExceptionGroup`` — hence the
    flatten — and its wording is user-facing text that will be reworded.
    :func:`_exc_indicates_auth` is deliberately *not* consulted: it matches any
    401/403 anywhere in the failure, which is also how a server with a broken
    gateway or a mis-scoped token fails, and those are worth retrying.
    """
    return any(isinstance(leaf, McpAuthorizationRequired) for leaf in _flatten_exc(exc))


def _exc_indicates_dcr_unsupported(exc: BaseException) -> bool:
    """Whether the failure is the provider rejecting Dynamic Client Registration.

    Servers without DCR (e.g. HubSpot) return a 404/405 to the registration POST.
    The MCP SDK raises ``OAuthRegistrationError`` — recognised here so the UI can
    point the user at the pre-registered client_id/secret fields instead of
    dumping the provider's raw error page.
    """
    for leaf in _flatten_exc(exc):
        name = type(leaf).__name__
        if name == "OAuthRegistrationError" or "registration failed" in str(leaf).lower():
            return True
    return False


def _strip_html(text: str) -> str:
    """Collapse an HTML error page down to its readable text (provider 404 pages
    embed a full document in the exception message)."""
    if "<" not in text:
        return text
    no_tags = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    no_tags = re.sub(r"(?s)<[^>]+>", " ", no_tags)
    return re.sub(r"\s+", " ", no_tags).strip()


def _readable_exc(exc: BaseException) -> str:
    """A human-readable one-liner for a (possibly grouped) connection failure."""
    if _exc_indicates_dcr_unsupported(exc):
        return (
            "This MCP server does not support automatic client registration (DCR). "
            "Create an OAuth app on the provider (e.g. HubSpot → Developer account → "
            "MCP Auth Apps), then enter its Client ID and Client secret in the MCP "
            "server settings and authorize again."
        )
    leaves = _flatten_exc(exc)
    # Prefer a leaf that carries a real message over the opaque group wrapper.
    for leaf in leaves:
        msg = _strip_html(str(leaf).strip())
        if msg:
            if len(msg) > 300:
                msg = msg[:297] + "…"
            return f"{type(leaf).__name__}: {msg}"
    return f"{type(exc).__name__}: {exc}"


# ── Probe (dashboard "Test connection") ────────────────────────────────────

# How much captured child stderr the probe echoes back into its log.
_STDERR_TAIL_LINES = 40


def _log_child_stderr(log: Callable[[str, str], None], errlog: Any) -> None:
    """Echo the tail of a stdio child's captured stderr into the probe log.

    The child's stderr is where launchers (`npx`, `uvx`) and bridges
    (`mcp-remote`) print their real errors — package-download failures,
    unreachable URLs, retry loops. Without this the probe can only say
    "timed out" / "connection closed" with zero context.
    """
    try:
        errlog.flush()
        errlog.seek(0)
        lines = [ln.rstrip() for ln in errlog.read().splitlines() if ln.strip()]
    except Exception:
        lines = []
    if not lines:
        log("info", "No stderr output was captured from the child process.")
        return
    tail = lines[-_STDERR_TAIL_LINES:]
    skipped = len(lines) - len(tail)
    header = f"Child process stderr (last {len(tail)} line{'s' if len(tail) != 1 else ''}"
    header += f", {skipped} earlier omitted):" if skipped else "):"
    log("warning", header)
    for ln in tail:
        log("warning", f"  {ln[:500]}")


def _stdio_remote_wrapper_hint(command: str, args: list[str]) -> str | None:
    """A fix-it hint when the stdio command is just an ``mcp-remote`` bridge.

    ``mcp-remote`` exists to bridge remote HTTP/SSE servers into stdio-only
    clients. Olano speaks streamable HTTP natively, so the wrapper only adds
    a Node/npx dependency that can hang (cold npx cache, blocked registry) or
    die where the direct connection would produce a real error message.
    """
    if not any(t == "mcp-remote" or t.startswith("mcp-remote@") for t in [command, *args]):
        return None
    url = next((a for a in args if a.startswith(("http://", "https://"))), None)
    where = f" ({url})" if url else ""
    return (
        "This command wraps a remote MCP server with `mcp-remote`. Olano speaks "
        f"streamable HTTP natively — add the server's URL{where} directly with "
        "transport `http` instead of the stdio wrapper. That removes the Node/npx "
        "dependency and surfaces real connection errors instead of timeouts."
    )


async def probe_server(
    agent_name: str, entry: dict[str, Any], *, timeout: float | None = None
) -> dict[str, Any]:
    """Connect to a single MCP server and return rich diagnostics.

    Result shape::

        {
            "ok": bool,
            "status": "connected" | "auth_required" | "error",
            "message": str,
            "needs_oauth": bool,  # server requires OAuth
            "authorized": bool,  # we currently hold tokens for it
            "transport": str,  # normalized transport
            "logs": [{"ts", "level", "message"}, ...],
            "tools": [{"name", "description", "enabled"}, ...],
        }
    """
    timeout = _probe_timeout(timeout)
    logs: list[dict[str, Any]] = []

    def log(level: str, message: str) -> None:
        logs.append({"ts": time.time(), "level": level, "message": message})

    url = (entry.get("url") or "").strip()
    raw_transport = entry.get("transport")
    command = entry.get("command")
    transport = normalize_transport(raw_transport, command=command)
    auth_mode = (entry.get("auth") or "none").strip().lower()
    # Resolve ${vault:NAME} refs exactly as the runtime connect does — a probe
    # that sends the literal 401s on a server that works at runtime, and the
    # OAuth sniff below then misreads that 401 as "needs sign-in".
    headers = agent_secret_headers(entry.get("headers") or {}, agent_name) or None
    disabled = set(entry.get("disabled_tools") or [])
    scope = entry.get("oauth_scope") or None
    # Static (pre-registered) client credentials for servers without DCR (e.g.
    # HubSpot). MUST be threaded into the OAuth provider below, otherwise the SDK
    # cannot refresh an expired access token and the probe wrongly reports
    # ``auth_required`` even though valid refresh tokens are stored on disk.
    # The secret is a ${vault:…} reference on a saved entry — resolved here, or
    # the probe would hand the provider the literal reference as the secret.
    client_id, client_secret = resolve_client_credentials(entry, agent_name)

    log("info", f"Transport: {transport}" + (f" (from '{raw_transport}')" if raw_transport else ""))

    def _result(
        ok: bool,
        status: str,
        message: str,
        *,
        tools: list[Any] | None = None,
        needs_oauth: bool = False,
        authorized: bool = False,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        tool_rows = [
            {
                "name": getattr(tool, "name", ""),
                "description": getattr(tool, "description", "") or "",
                "enabled": getattr(tool, "name", "") not in disabled,
            }
            for tool in (tools or [])
        ]
        return {
            "ok": ok,
            "status": status,
            "message": message,
            "needs_oauth": needs_oauth,
            "authorized": authorized,
            "error_code": error_code,
            "transport": transport,
            "logs": logs,
            "tools": tool_rows,
        }

    if transport == "stdio":
        if not command:
            log("error", "stdio transport requires a launch command.")
            return _result(False, "error", "stdio transport requires a command.")
        stdio_args = list(entry.get("args") or [])
        # Same policy seam as the boot path: open posture launches the command
        # as-is; managed posture rewrites it into an ephemeral sandbox
        # container, or blocks it with the reason surfaced to the user.
        launch = plan_stdio_launch(command, stdio_args, entry.get("env") or {}, agent_name)
        if launch.invocation is None:
            reason = launch.blocked_reason or "stdio MCP is unavailable on this deployment."
            log("error", reason)
            return _result(False, "error", reason)
        if launch.note:
            log("info", launch.note)
        log("info", f"Launching: {command} {' '.join(stdio_args)}".strip())
        # The child's stderr goes to a temp file so it can be echoed into the
        # probe log on failure (errlog must be a real file — it becomes the
        # subprocess's stderr fd). For a sandboxed launch the docker CLI
        # forwards the container's stderr onto its own, so the capture works
        # unchanged.
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as child_err:
            try:
                tools = await asyncio.wait_for(
                    _connect_and_list_tools(
                        url,
                        transport,
                        command=launch.invocation.command,
                        args=launch.invocation.args,
                        env=launch.invocation.env,
                        errlog=child_err,
                        log=log,
                        stdio_policy_checked=True,
                    ),
                    timeout,
                )
                log("info", f"Connected · {len(tools)} tool(s) discovered.")
                return _result(
                    True,
                    "connected",
                    f"Connected · {len(tools)} tool{'s' if len(tools) != 1 else ''}",
                    tools=tools,
                )
            except TimeoutError:
                log(
                    "error",
                    f"Connection timed out after {timeout:g}s — the child process launched "
                    "but never completed the MCP handshake.",
                )
                _log_child_stderr(log, child_err)
                hint = _stdio_remote_wrapper_hint(command, stdio_args)
                if hint:
                    log("info", hint)
                log(
                    "info",
                    "Common causes on a fresh host: `npx`/`uvx` downloading the package on "
                    "first run (slow or blocked package registry), or outbound traffic to "
                    "the target URL being blocked (cloud firewall / security-group egress "
                    "rules). Raise the limit with LIONCLAW_MCP_PROBE_TIMEOUT if the host is "
                    "just slow.",
                )
                return _result(False, "error", f"Connection timed out after {timeout:g}s.")
            except Exception as conn_exc:
                for line in traceback.format_exc().rstrip().splitlines():
                    log("debug", line)
                _log_child_stderr(log, child_err)
                message = _readable_exc(conn_exc)
                log("error", message)
                return _result(False, "error", message)

    # ── Remote transports (http / sse) ──────────────────────────────────────
    if not url:
        return _result(False, "error", "Server URL is required for http/sse transports.")
    log("info", f"Endpoint: {url}")

    token_state = oauth_token_state(agent_name, url)
    authorized = token_state["authorized"]
    provider = None
    # Stored tokens for this (agent, url) are used even when the entry still
    # says auth:none — they only exist because an Authorize flow completed, and
    # probing without them would just re-hit the 401 that triggered that flow
    # (the classic case: a freshly-authorized server in the Add modal, before
    # the config entry has been saved/flipped to oauth).
    if auth_mode == "oauth" or token_state["state"] != "none":
        if token_state["state"] == "expired":
            # Expired access token with nothing to refresh from — connecting
            # would only raise the opaque "requires OAuth" error mid-handshake.
            log(
                "info",
                "Stored OAuth access token is expired and no refresh token was "
                "issued — re-authorization required.",
            )
            return _result(
                False,
                "auth_required",
                "OAuth access token expired and no refresh token is stored. "
                "Click 'Authorize' to sign in again.",
                needs_oauth=True,
                authorized=False,
            )
        if authorized:
            log("info", "Using stored OAuth tokens for this server.")
            provider = make_oauth_provider(
                agent_name,
                url,
                scope=scope,
                client_id=client_id,
                client_secret=client_secret,
                interactive=False,
            )
        else:
            # OAuth is selected but we hold no tokens — the only sensible next
            # step is to authorize. Don't probe (detection is unreliable and a
            # raw connect would just fail with an opaque error); say so directly.
            log("info", "OAuth selected and no tokens stored — authorization required.")
            return _result(
                False,
                "auth_required",
                "OAuth authorization required. Click 'Authorize' to sign in.",
                needs_oauth=True,
                authorized=False,
            )

    # Try the requested transport first, then the alternate remote transport so a
    # mislabeled (or "Auto") server still connects. An auth error means we reached
    # the right endpoint — stop and surface authorization instead of switching.
    candidates = _remote_transport_candidates(raw_transport, transport)
    if len(candidates) > 1:
        log("info", f"Auto-detecting transport: trying {' → '.join(candidates)}")
    exc: BaseException | None = None
    timed_out = False
    for i, cand in enumerate(candidates):
        try:
            tools = await asyncio.wait_for(
                _connect_and_list_tools(url, cand, auth=provider, headers=headers, log=log),
                timeout,
            )
            transport = cand  # report the transport that actually worked
            log("info", f"Connected via {cand} · {len(tools)} tool(s) discovered.")
            return _result(
                True,
                "connected",
                f"Connected via {cand} · {len(tools)} tool{'s' if len(tools) != 1 else ''}",
                tools=tools,
                needs_oauth=(auth_mode == "oauth" or authorized),
                authorized=authorized,
            )
        except TimeoutError:
            timed_out = True
            exc = None
            log("warning", f"{cand} timed out after {timeout:g}s.")
        except Exception as e:
            exc = e
            for line in traceback.format_exc().rstrip().splitlines():
                log("debug", line)
            # A 401/403 means this transport reached the server — don't keep
            # switching; go straight to the authorization path below.
            if _exc_indicates_auth(e):
                transport = cand
                break
        has_more = i + 1 < len(candidates)
        if has_more:
            log("info", f"{cand} did not connect; trying {candidates[i + 1]}…")

    if exc is None:
        if timed_out:
            log("error", f"Connection timed out after {timeout:g}s.")
            return _result(False, "error", f"Connection timed out after {timeout:g}s.")
        # Defensive: no exception and no success (shouldn't happen).
        return _result(False, "error", "Could not connect to MCP server.")

    message = _readable_exc(exc)
    log("error", message)
    # The connection may have failed precisely because the server needs OAuth
    # and we sent no credentials. Detect that (from the unwrapped exception or
    # a direct 401 probe) and surface it as an actionable state — Connect then
    # auto-launches the authorization flow.
    needed, www = await _detect_oauth_required(url, transport, headers)
    if needed or _exc_indicates_auth(exc):
        if www:
            log("info", f"WWW-Authenticate: {www}")
        log("info", "Server requires OAuth authorization.")
        return _result(
            False,
            "auth_required",
            "This server requires OAuth. Click 'Authorize' to sign in.",
            needs_oauth=True,
            authorized=False,
        )
    return _result(
        False,
        "error",
        message,
        error_code="dcr_unsupported" if _exc_indicates_dcr_unsupported(exc) else None,
    )


# ── Interactive OAuth flow ──────────────────────────────────────────────────


def _persist_oauth_auth_mode(
    agent: str,
    url: str,
    *,
    scope: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> bool:
    """Flip the configured server entry to ``auth: "oauth"`` after a successful flow.

    The Authorize flow is routinely launched for a server stored with
    ``auth: "none"`` — the probe detects a 401 and the dashboard starts the flow
    with a *transient* oauth copy that is never written back. Unless the mode is
    persisted here, every later code path reads ``none`` from config (agent
    boot, hot reload, probe, cheap status), connects unauthenticated, and
    reports the server unauthorized with zero tools even though valid tokens
    sit on disk. The client credentials/scope used by the flow are persisted
    too (when the entry doesn't already carry them) so later token refreshes
    work for static-client (DCR-less) providers.

    Raw read-modify-write of ``agent.json5`` (same mechanics as the runtime's
    MCP update endpoint) — no env interpolation is baked in. Best-effort:
    failures are logged, never raised.
    """
    try:
        from olano.config.loader import _load_json5, find_agent_config_path

        path = find_agent_config_path(agent)
        if path is None:
            return False
        data = _load_json5(path.read_text(encoding="utf-8")) or {}
        servers = data.get("mcp_servers") or []
        entry = next((s for s in servers if isinstance(s, dict) and s.get("url") == url), None)
        if entry is None:
            return False
        changed = False
        if (entry.get("auth") or "none").strip().lower() != "oauth":
            entry["auth"] = "oauth"
            changed = True
        for key, value in (
            ("oauth_scope", scope),
            ("oauth_client_id", client_id),
            ("oauth_client_secret", client_secret),
        ):
            if value and not entry.get(key):
                entry[key] = value
                changed = True
        if changed:
            from olano.config.loader import write_agent_config_text
            from olano.config.mcp_secrets import absorb_mcp_client_secrets_quietly

            # The secret the flow just used is the literal the user typed in
            # the Authorize dialog — it goes to the vault, the entry keeps a
            # reference (a no-op when the entry already carried one).
            absorb_mcp_client_secrets_quietly(agent, data)
            write_agent_config_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        return changed
    except Exception:
        logger.warning("Could not persist auth=oauth for %s / %s", agent, url, exc_info=True)
        return False


@dataclass
class OAuthFlowSession:
    session_id: str
    agent: str
    url: str
    transport: str
    status: str = "starting"  # starting|awaiting_authorization|exchanging|authorized|error|expired
    authorization_url: str | None = None
    message: str = ""
    error: str | None = None
    error_code: str | None = None  # e.g. "dcr_unsupported" — structured branch for the UI
    tool_count: int = 0
    logs: list[dict[str, Any]] = field(default_factory=list)
    state: str | None = None
    created_at: float = field(default_factory=time.time)
    _future: asyncio.Future[tuple[str, str | None]] | None = None
    _task: asyncio.Task[Any] | None = None

    def log(self, level: str, message: str) -> None:
        self.logs.append({"ts": time.time(), "level": level, "message": message})
        logger.log(
            logging.ERROR if level == "error" else logging.INFO,
            "mcp-oauth[%s]: %s",
            self.session_id,
            message,
        )

    def public(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "authorization_url": self.authorization_url,
            "message": self.message,
            "error": self.error,
            "error_code": self.error_code,
            "tool_count": self.tool_count,
            "logs": self.logs,
            "url": self.url,
            "transport": self.transport,
        }


_OAUTH_SESSIONS: dict[str, OAuthFlowSession] = {}
_OAUTH_BY_STATE: dict[str, str] = {}


def _prune_oauth_sessions(max_age: float = 1800.0) -> None:
    now = time.time()
    stale = [sid for sid, s in _OAUTH_SESSIONS.items() if now - s.created_at > max_age]
    for sid in stale:
        sess = _OAUTH_SESSIONS.pop(sid, None)
        if sess and sess.state:
            _OAUTH_BY_STATE.pop(sess.state, None)


async def _run_oauth_flow(
    session: OAuthFlowSession,
    *,
    redirect_uri: str,
    scope: str | None,
    client_id: str | None,
    client_secret: str | None,
    headers: dict[str, str] | None,
    timeout: float,
) -> None:
    async def _redirect_handler(auth_url: str) -> None:
        session.authorization_url = auth_url
        session.status = "awaiting_authorization"
        qs = parse_qs(urlparse(auth_url).query)
        states = qs.get("state") or []
        state = states[0] if states else None
        session.state = state
        if state:
            _OAUTH_BY_STATE[state] = session.session_id
        session.log("info", "Authorization URL ready — open it to grant access.")

    async def _callback_handler() -> tuple[str, str | None]:
        session.log("info", "Waiting for the authorization redirect…")
        assert session._future is not None
        code, state = await session._future
        session.status = "exchanging"
        session.log("info", "Authorization code received — exchanging for tokens…")
        return code, state

    try:
        session.log("info", f"Connecting to {session.url} ({session.transport}) to begin OAuth…")
        if not client_id:
            stale = clear_stale_oauth_client(session.agent, session.url, redirect_uri)
            if stale:
                session.log(
                    "info",
                    f"Redirect URI changed (was {', '.join(stale)}); re-registering OAuth client.",
                )
        provider = make_oauth_provider(
            session.agent,
            session.url,
            redirect_uri=redirect_uri,
            scope=scope,
            client_id=client_id,
            client_secret=client_secret,
            redirect_handler=_redirect_handler,
            callback_handler=_callback_handler,
            interactive=True,
        )
        tools = await asyncio.wait_for(
            _connect_and_list_tools(
                session.url, session.transport, auth=provider, headers=headers, log=session.log
            ),
            timeout,
        )
        session.tool_count = len(tools)
        # Persist the auth mode BEFORE the status flips to authorized, so any
        # poller that observes "authorized" (and hot-reloads the agent) already
        # reads `auth: oauth` — and the stored tokens — from config.
        if _persist_oauth_auth_mode(
            session.agent,
            session.url,
            scope=scope,
            client_id=client_id,
            client_secret=client_secret,
        ):
            session.log("info", "Saved auth: oauth on this server in the agent config.")
        session.status = "authorized"
        session.message = f"Authorized · {len(tools)} tool{'s' if len(tools) != 1 else ''}"
        session.log("info", session.message)
    except TimeoutError:
        session.status = "expired"
        session.error = "Authorization timed out."
        session.log("error", "Authorization timed out before completion.")
    except Exception as exc:
        for line in traceback.format_exc().rstrip().splitlines():
            session.log("debug", line)
        session.status = "error"
        if _exc_indicates_dcr_unsupported(exc):
            session.error_code = "dcr_unsupported"
        session.error = _readable_exc(exc)
        session.log("error", session.error)
    finally:
        if session.state:
            _OAUTH_BY_STATE.pop(session.state, None)


def start_oauth_flow(
    agent_name: str,
    entry: dict[str, Any],
    *,
    redirect_uri: str,
    timeout: float = OAUTH_FLOW_TIMEOUT,
) -> OAuthFlowSession:
    """Kick off the interactive authorization-code flow for one server.

    Spawns a background task that connects with an ``OAuthClientProvider``;
    poll :func:`poll_oauth_flow` to learn the authorization URL and final status.
    """
    _prune_oauth_sessions()
    url = (entry.get("url") or "").strip()
    transport = normalize_transport(entry.get("transport"), command=entry.get("command"))
    if not url or not is_remote_transport(transport):
        raise ValueError("OAuth is only supported for http/sse MCP servers.")

    session_id = uuid.uuid4().hex
    session = OAuthFlowSession(
        session_id=session_id,
        agent=agent_name,
        url=url,
        transport=transport,
    )
    loop = asyncio.get_running_loop()
    session._future = loop.create_future()
    _OAUTH_SESSIONS[session_id] = session
    # A saved entry carries the secret as a ${vault:…} reference (a freshly
    # typed one, from the dashboard dialog or the magic-link form, is still a
    # literal) — resolve either shape before the flow needs it.
    client_id, client_secret = resolve_client_credentials(entry, agent_name)
    session._task = loop.create_task(
        _run_oauth_flow(
            session,
            redirect_uri=redirect_uri,
            scope=entry.get("oauth_scope") or None,
            client_id=client_id,
            client_secret=client_secret,
            # Resolved like the runtime connect: an OAuth server can also carry
            # a static header (tenant id, API key), and sending the literal ref
            # fails the connect+list step AFTER the user has granted consent.
            headers=agent_secret_headers(entry.get("headers") or {}, agent_name) or None,
            timeout=timeout,
        )
    )
    return session


def poll_oauth_flow(session_id: str) -> OAuthFlowSession | None:
    return _OAUTH_SESSIONS.get(session_id)


def resolve_oauth_callback(
    state: str | None, code: str | None, error: str | None = None
) -> OAuthFlowSession | None:
    """Feed the OAuth redirect back into the waiting flow (matched by ``state``)."""
    if not state:
        return None
    sid = _OAUTH_BY_STATE.get(state)
    if not sid:
        return None
    session = _OAUTH_SESSIONS.get(sid)
    if not session or session._future is None or session._future.done():
        return session
    if error:
        session._future.set_exception(RuntimeError(f"Authorization denied: {error}"))
    elif code:
        session._future.set_result((code, state))
    else:
        session._future.set_exception(RuntimeError("Authorization callback missing code."))
    return session


# ── Headless / CLI OAuth (localhost redirect capture) ───────────────────────

_TERMINAL_OAUTH_STATUSES = {"authorized", "error", "expired"}

_OAUTH_CALLBACK_HTML = (
    '<!doctype html><html><head><meta charset="utf-8">'
    "<title>Olano · MCP OAuth</title>"
    "<style>body{{margin:0;min-height:100vh;display:flex;align-items:center;"
    "justify-content:center;font-family:-apple-system,BlinkMacSystemFont,"
    "'Segoe UI',sans-serif;background:#0b0d12;color:#e8eaed}}"
    ".card{{max-width:420px;padding:32px;border-radius:14px;background:#13161d;"
    "border:1px solid rgba(255,255,255,.08);text-align:center}}"
    ".dot{{width:10px;height:10px;border-radius:50%;background:{accent};"
    "display:inline-block;margin-right:8px}}h1{{font-size:18px;margin:0 0 12px}}"
    "p{{font-size:14px;line-height:1.55;color:#a9b0bd;margin:0}}</style></head>"
    '<body><div class="card"><h1><span class="dot"></span>{title}</h1>'
    "<p>{detail}</p></div></body></html>"
)


def _callback_page(session: OAuthFlowSession | None, error: str | None) -> str:
    if session is None:
        title, detail, accent = (
            "Authorization session not found",
            "This link has expired or was already used. Re-run the authorize command.",
            "#f46363",
        )
    elif error:
        title, detail, accent = (
            "Authorization failed",
            f"The provider reported: {error}. You can close this tab and retry.",
            "#f46363",
        )
    else:
        title, detail, accent = (
            "Authorization received",
            "Olano is finishing the connection. You can close this tab and return to the terminal.",
            "#48d389",
        )
    return _OAUTH_CALLBACK_HTML.format(title=title, detail=detail, accent=accent)


async def authorize_via_local_callback(
    agent_name: str,
    entry: dict[str, Any],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    on_authorization_url: Callable[[str], None] | None = None,
    poll_interval: float = 0.25,
    timeout: float = OAUTH_FLOW_TIMEOUT,
) -> OAuthFlowSession:
    """Drive the interactive authorization-code + PKCE flow for a CLI/headless caller.

    Hosts a throwaway ``http://host:port/`` server on the current event loop to
    capture the OAuth redirect, kicks off :func:`start_oauth_flow`, surfaces the
    authorization URL (via *on_authorization_url* and, when *open_browser*, the
    system browser), captures the ``?code=&state=`` callback, and returns the
    terminal :class:`OAuthFlowSession`.

    Everything runs on one event loop, so the callback feeds the waiting flow
    through :func:`resolve_oauth_callback` without cross-thread hand-off.
    """
    url = (entry.get("url") or "").strip()
    transport = normalize_transport(entry.get("transport"), command=entry.get("command"))
    if not url or not is_remote_transport(transport):
        raise ValueError("OAuth is only supported for http/sse MCP servers.")

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            target = ""
            parts = request_line.decode("latin-1", "replace").split()
            if len(parts) >= 2:
                target = parts[1]
            qs = parse_qs(urlparse(target).query)

            def _first(name: str) -> str | None:
                values = qs.get(name)
                return values[0] if values else None

            state = _first("state")
            code = _first("code")
            err = _first("error_description") or _first("error")
            session_for_page: OAuthFlowSession | None = None
            if state is not None:
                session_for_page = resolve_oauth_callback(state, code, err)
            body = _callback_page(session_for_page, err).encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        except (TimeoutError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                writer.close()

    server = await asyncio.start_server(_handle, host, port)
    try:
        bound_port = server.sockets[0].getsockname()[1]
        redirect_uri = f"http://{host}:{bound_port}/"
        flow_entry = dict(entry)
        flow_entry["auth"] = "oauth"
        session = start_oauth_flow(
            agent_name, flow_entry, redirect_uri=redirect_uri, timeout=timeout
        )

        # Surface the authorization URL (or an early failure) as soon as the flow
        # produces one, then wait for it to reach a terminal state. The flow's own
        # ``timeout`` (passed to start_oauth_flow) guarantees termination, so no
        # separate deadline is needed here.
        announced = False
        while session.status not in _TERMINAL_OAUTH_STATUSES:
            if session.authorization_url and not announced:
                announced = True
                if on_authorization_url:
                    on_authorization_url(session.authorization_url)
                if open_browser:
                    with contextlib.suppress(Exception):
                        import webbrowser

                        webbrowser.open(session.authorization_url)
            await asyncio.sleep(poll_interval)
        return session
    finally:
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()


# ── Central-relay OAuth (domain-less deployments) ───────────────────────────


def _public_origin() -> str | None:
    """Best-effort external HTTPS origin for this runtime (origin strategy).

    ``LIONCLAW_PUBLIC_URL`` env → the last external origin the dashboard was
    reached on (captured by the API when a browser hits it). Used only when no
    relay is configured; the relay is the recommended path for domain-less hosts.
    """
    env = os.environ.get("LIONCLAW_PUBLIC_URL")
    if env and env.strip():
        return env.strip().rstrip("/")
    return _LAST_SEEN_ORIGIN


_LAST_SEEN_ORIGIN: str | None = None


def remember_public_origin(origin: str | None) -> None:
    """Record the external origin a browser reached the dashboard on (origin strategy)."""
    global _LAST_SEEN_ORIGIN
    if origin and origin.strip():
        _LAST_SEEN_ORIGIN = origin.strip().rstrip("/")


def public_origin() -> str | None:
    """This deployment's browser-reachable origin, or ``None``.

    Public alias of :func:`_public_origin` so other subsystems that need to
    build a user-facing URL (magic links, page URLs) resolve it exactly the
    way OAuth redirects do — ``LIONCLAW_PUBLIC_URL`` first, then the last origin
    a browser actually reached the dashboard on — instead of re-deriving it and
    drifting.
    """
    return _public_origin()


async def _relay_pump(session: OAuthFlowSession, cfg: Any, *, poll_interval: float = 1.0) -> None:
    """Shuttle the authorization ``code`` from the relay into a waiting flow.

    Waits for the provider redirect handler to publish ``session.state``,
    registers it with the relay, then polls until the relay has the ``code``
    (or an error) and feeds it back via :func:`resolve_oauth_callback` — exactly
    as a direct browser callback would. Runs as a background task.
    """
    from olano.core import mcp_relay

    waited = 0.0
    while session.state is None and session.status not in _TERMINAL_OAUTH_STATUSES:
        await asyncio.sleep(0.1)
        waited += 0.1
        if waited > 30.0:
            break
    if session.state is None:
        return

    try:
        await mcp_relay.register_state(cfg, session.state)
        session.log("info", "Registered authorization with relay; awaiting sign-in…")
    except Exception as exc:
        session.log("error", f"Could not reach the OAuth relay to register: {exc}")
        resolve_oauth_callback(session.state, None, f"relay unreachable: {exc}")
        return

    elapsed = 0.0
    while session.status not in _TERMINAL_OAUTH_STATUSES and elapsed < OAUTH_FLOW_TIMEOUT:
        try:
            result = await mcp_relay.poll_code(cfg, session.state)
        except Exception as exc:
            session.log("debug", f"relay poll error: {exc}")
            result = {}
        status = result.get("status")
        if status == "ready" and result.get("code"):
            resolve_oauth_callback(session.state, str(result["code"]), None)
            return
        if status == "error":
            resolve_oauth_callback(session.state, None, str(result.get("error") or "denied"))
            return
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval


def _resolve_redirect_uri(entry: dict[str, Any], redirect_base: str | None) -> tuple[str, Any]:
    """Pick the OAuth redirect URI + (optional) relay config for a chat/agent flow.

    Precedence: explicit *redirect_base* → central relay → public origin. Returns
    ``(redirect_uri, relay_config_or_None)``. Raises ``ValueError`` when nothing
    is configured (so the caller can surface an actionable message).
    """
    from olano.core import mcp_relay

    if redirect_base and redirect_base.strip():
        return f"{redirect_base.strip().rstrip('/')}/api/mcp/oauth/callback", None
    cfg = mcp_relay.resolve_relay_config()
    if cfg is not None:
        return cfg.callback_uri, cfg
    origin = _public_origin()
    if origin:
        return f"{origin}/api/mcp/oauth/callback", None
    raise ValueError(
        "No MCP OAuth redirect target configured. On a self-hosted install, connect "
        "an Olano API key (Providers -> Olano) to use Olano's sign-in relay; or set "
        "LIONCLAW_PUBLIC_URL to this deployment's public HTTPS origin, or "
        "LIONCLAW_MCP_RELAY_URL (+ a relay credential) for a relay you run."
    )


def start_mcp_authorization(
    agent_name: str,
    entry: dict[str, Any],
    *,
    redirect_base: str | None = None,
) -> OAuthFlowSession:
    """Start an interactive MCP OAuth flow for a **non-terminal** caller (chat/agent).

    Non-blocking: returns the :class:`OAuthFlowSession` immediately — poll
    :func:`poll_oauth_flow` for the ``authorization_url`` (to hand the user over
    any channel) and the terminal status. The redirect target is chosen by
    :func:`_resolve_redirect_uri`; in relay mode a background pump feeds the
    callback back into the flow. Token exchange + storage stay local and
    per-``(agent, url)`` regardless of strategy.
    """
    url = (entry.get("url") or "").strip()
    transport = normalize_transport(entry.get("transport"), command=entry.get("command"))
    if not url or not is_remote_transport(transport):
        raise ValueError("OAuth is only supported for http/sse MCP servers.")

    redirect_uri, cfg = _resolve_redirect_uri(entry, redirect_base)
    flow_entry = dict(entry)
    flow_entry["auth"] = "oauth"
    session = start_oauth_flow(agent_name, flow_entry, redirect_uri=redirect_uri)
    if cfg is not None:
        asyncio.get_running_loop().create_task(_relay_pump(session, cfg))
    return session


async def authorize_mcp_and_wait(
    agent_name: str,
    entry: dict[str, Any],
    *,
    on_authorization_url: Callable[[str], None] | None = None,
    open_browser: bool = False,
    poll_interval: float = 0.25,
) -> OAuthFlowSession:
    """Blocking authorize for a terminal/CLI caller, relay-aware.

    Uses the central relay (or public origin) when configured — so an operator
    can authorize a domain-less deployment from anywhere — and otherwise falls
    back to the same-machine localhost callback (:func:`authorize_via_local_callback`).
    Waits until the flow reaches a terminal state and returns the session.
    """
    from olano.core import mcp_relay

    if mcp_relay.relay_url() or os.environ.get("LIONCLAW_PUBLIC_URL"):
        session = start_mcp_authorization(agent_name, entry)
        announced = False
        while session.status not in _TERMINAL_OAUTH_STATUSES:
            if session.authorization_url and not announced:
                announced = True
                if on_authorization_url:
                    on_authorization_url(session.authorization_url)
                if open_browser:
                    with contextlib.suppress(Exception):
                        import webbrowser

                        webbrowser.open(session.authorization_url)
            await asyncio.sleep(poll_interval)
        return session

    return await authorize_via_local_callback(
        agent_name,
        entry,
        open_browser=open_browser,
        on_authorization_url=on_authorization_url,
        poll_interval=poll_interval,
    )


# ---------------------------------------------------------------------------
# Tool schema compatibility
# ---------------------------------------------------------------------------
#
# An MCP server's ``inputSchema`` is handed to the model verbatim, and the
# providers do not agree on JSON Schema. OpenAI validates every tool on every
# request and refuses the *whole turn* when one ``pattern`` uses a construct
# its regex engine lacks (Lusha's ``email``: ``400 invalid_json_schema: regex
# lookaround is not supported``), so one server's schema bricked the agent.
# A ``pattern`` is a hint to the model; the server still validates its own
# input, so dropping an unportable one costs nothing a caller can notice.

# Lookahead / lookbehind, backreferences and atomic groups: the constructs
# RE2-style engines refuse. A false positive only drops a hint.
_UNPORTABLE_REGEX = re.compile(r"\(\?<?[=!]|\(\?>|\\[1-9]|\\k<")

# Keys whose value is instance data, not schema: never walk into them.
_SCHEMA_DATA_KEYS = frozenset({"enum", "const", "default", "examples", "example"})


def _portable_schema(node: Any) -> tuple[Any, int]:
    if isinstance(node, list):
        out_list, dropped = [], 0
        for item in node:
            clean, n = _portable_schema(item)
            out_list.append(clean)
            dropped += n
        return out_list, dropped
    if not isinstance(node, dict):
        return node, 0
    out: dict[str, Any] = {}
    dropped = 0
    for key, value in node.items():
        if key == "pattern" and isinstance(value, str) and _UNPORTABLE_REGEX.search(value):
            dropped += 1
            continue
        if key in _SCHEMA_DATA_KEYS:
            out[key] = value
            continue
        out[key], n = _portable_schema(value)
        dropped += n
    return out, dropped


def sanitize_mcp_tool_schemas(tools: list[Any], *, server: str = "") -> list[Any]:
    """Drop regex ``pattern`` keywords no provider-portable engine accepts.

    Mutates each tool's dict ``args_schema`` in place (replacing it with a
    cleaned copy) and returns the same list. Pydantic schemas are left alone:
    langchain-mcp-adapters always builds MCP tools with a plain dict.
    """
    for tool in tools:
        schema = getattr(tool, "args_schema", None)
        if not isinstance(schema, dict):
            continue
        clean, dropped = _portable_schema(schema)
        if dropped:
            tool.args_schema = clean
            logger.info(
                "MCP tool %r%s: dropped %d unportable regex pattern(s) from its schema",
                getattr(tool, "name", "?"),
                f" ({server})" if server else "",
                dropped,
            )
    return tools
