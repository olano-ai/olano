"""Default deepagents security middleware for Olano."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Populated by awrap_tool_call before each tool executes so runner.py can read
# the authoritative args regardless of how the LLM streamed them.
# Keyed by tool_call_id (UUID-like); popped by runner after consumption.
#
# Bounded FIFO: the runner only pops on the root-namespace streaming path, so
# the ainvoke / subagent / worker paths (scheduled tasks, heartbeats, headless
# workers) never remove their entries. Capping it (evict-oldest) makes the
# growth impossible regardless of code path — entries are consumed within the
# same turn, so the cap only ever discards ids that were already abandoned.
_TOOL_ARGS_REGISTRY_MAX = 4096
_TOOL_ARGS_REGISTRY: OrderedDict[str, dict[str, Any]] = OrderedDict()


def _record_tool_args(tool_call_id: str, tool_name: str, args: Any) -> None:
    """Record authoritative tool args for the runner to read, bounded FIFO
    (evict-oldest) so the registry can never grow without bound."""
    _TOOL_ARGS_REGISTRY[tool_call_id] = {"name": tool_name, "args": args}
    _TOOL_ARGS_REGISTRY.move_to_end(tool_call_id)
    while len(_TOOL_ARGS_REGISTRY) > _TOOL_ARGS_REGISTRY_MAX:
        _TOOL_ARGS_REGISTRY.popitem(last=False)


from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from olano.config.agent_config import AgentConfig, SharedPathConfig
from olano.core.tool_failures import (
    args_signature_text,
    current_turn_tool_failures,
    is_failed_tool_result,
    tool_call_signature,
)
from olano.security.egress import security_metadata as egress_security_metadata
from olano.security.egress_guard import governed as egress_governed
from olano.security.fleet_access import (
    FleetRoot,
    fleet_root_for_path,
    fleet_roots,
    handoff_fleet_trees,
)
from olano.security.output_scrub import scrub_text
from olano.security.pipeline import _sanitize_result, evaluate_tool_access
from olano.security.rate_limit import TOOL_RATE_LIMITER
from olano.security.read_file_policy import read_file_refusal
from olano.security.request_context import get_request_context, push_request_context
from olano.security.sharing import SharedMount, resolve_shared_mounts
from olano.security.shell_output import summarize_shell_output
from olano.security.tool_metadata import ToolMetadata, get_tool_metadata, infer_tool_metadata
from olano.security.trust_levels import TrustLevel

logger = logging.getLogger(__name__)

_WRITE_FILE_TOOL_NAMES = frozenset({"write_file", "edit_file"})
_READ_FILE_TOOL_NAMES = frozenset({"read_file", "ls", "glob", "grep"})
_PATH_NORMALIZATION_KEYS: dict[str, tuple[str, ...]] = {
    "read_file": ("path", "file_path", "filepath", "target_path"),
    "write_file": ("path", "file_path", "filepath", "target_path"),
    "edit_file": ("path", "file_path", "filepath", "target_path"),
    "ls": ("path", "directory", "dir"),
    "glob": ("path", "directory", "dir"),
    "grep": ("path", "file_path", "filepath", "target_path", "directory", "dir"),
}
_SHELL_TOOL_NAMES = frozenset(
    {
        "execute",
        "safe_shell",
        "safe_shell_tool",
        "node_run",
        "node_run_tool",
        "npm_install",
        "npm_install_tool",
    }
)
#: Tools that record commands for later execution (not run inline like the
#: shell family above): gated like a shell call, then left to their own body.
_PAGE_COMMAND_TOOL_NAMES = frozenset({"pages_deploy"})
_SHELL_LOG_PATH_PLACEHOLDER = "__LIONCLAW_SHELL_LOG_PATH__"


def _stringify_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(value)


def _replacement_update(messages: list[Any], idx: int, replaced: Any) -> dict[str, Any]:
    """A state update that rewrites ``messages[idx]`` as ``replaced``.

    The ``messages`` reducer merges by id, so a one-message update replaces the
    stored message in place and the step's write holds one message instead of
    the whole conversation. The whole list goes only when the message carries
    no id to merge on (LangGraph stamps one on every message it stores, so
    that is a hand-built state in a test, never a persisted thread).
    """
    if getattr(replaced, "id", None) is None:
        full = list(messages)
        full[idx] = replaced
        return {"messages": full}
    return {"messages": [replaced]}


def _replace_message_content(message: Any, content: str) -> Any:
    if isinstance(message, HumanMessage):
        return HumanMessage(content=content, id=message.id, name=message.name)
    if isinstance(message, AIMessage):
        return AIMessage(
            content=content,
            id=message.id,
            name=message.name,
            tool_calls=message.tool_calls,
            additional_kwargs=message.additional_kwargs,
            response_metadata=message.response_metadata,
        )
    if isinstance(message, ToolMessage):
        return ToolMessage(
            content=content,
            id=message.id,
            name=message.name,
            tool_call_id=message.tool_call_id,
            artifact=message.artifact,
            status=message.status,
            additional_kwargs=message.additional_kwargs,
            response_metadata=message.response_metadata,
        )
    return message


def _find_last_message_index(messages: list[Any], message_type: type) -> int | None:
    for idx in range(len(messages) - 1, -1, -1):
        if isinstance(messages[idx], message_type):
            return idx
    return None


def _normalize_tool_args(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return dict(raw_args)
    if isinstance(raw_args, str):
        stripped = raw_args.strip()
        if not stripped:
            return {}
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return dict(parsed)
            except Exception:
                pass
        return {"command": raw_args}
    return {}


def _path_from_tool_args(args: dict[str, Any]) -> str:
    for key in ("path", "file_path", "filepath", "target_path"):
        value = args.get(key)
        if value:
            return str(value)
    return ""


def _skill_md_target(
    args: dict[str, Any], workspace_root: Path, mounts: list[SharedMount]
) -> Path | None:
    """The host path of a file-tool call that targets a ``SKILL.md``, else ``None``."""
    path_text = _path_from_tool_args(args)
    if not path_text:
        return None
    path = _resolve_tool_path(path_text, workspace_root, mounts)
    from olano.skills.naming import SKILL_FILENAME

    return path if path.name == SKILL_FILENAME else None


def _skill_name_on_disk(skill_md: Path) -> str | None:
    """The ``name`` an existing SKILL.md declares (``None`` if absent or unreadable)."""
    from olano.skills.naming import frontmatter_name

    try:
        return frontmatter_name(skill_md.read_text(encoding="utf-8"))
    except OSError:
        return None


def _with_skill_note(message: Any, note: str | None) -> Any:
    """*message* with a ``[skill-name]`` note appended to its content."""
    if not note:
        return message
    content = _stringify_content(message.content)
    return _replace_message_content(message, f"{content.rstrip()}\n\n{note}")


def _workspace_shared_projection_for_path(
    path: Path,
    workspace_root: Path,
    mounts: list[SharedMount],
) -> Path | None:
    parts = path.parts
    shared_indexes = [idx for idx, part in enumerate(parts) if part == "shared"]
    if not shared_indexes:
        return None

    active_targets = {mount.target.lstrip("/") for mount in mounts}
    for idx in shared_indexes:
        rel_parts = parts[idx:]
        if len(rel_parts) < 2:
            continue
        rel_text = Path(*rel_parts).as_posix()
        if any(
            rel_text == target or rel_text.startswith(target + "/") for target in active_targets
        ):
            return (workspace_root / Path(*rel_parts)).absolute()
    return None


def _resolve_tool_path(path_text: str, workspace_root: Path, mounts: list[SharedMount]) -> Path:
    """
    Normalize a tool path without resolving symlinks.

    Shared-path projections are exposed inside the workspace as symlinks under
    ``workspace/shared/*`` and may also be referenced by the model using
    sandbox-visible absolute paths like ``/shared/manual/file.txt``. For policy
    checks we need to preserve that projected workspace path so we can
    correctly classify it as a workspace path and match it against the active
    shared mount. Fully resolving the path too early turns it back into the
    host source path and breaks shared-path enforcement.
    """
    path = Path(path_text).expanduser()
    if path.is_absolute():
        path_parts = path.parts
        if path_parts[:2] == ("/", "workspace"):
            suffix = Path(*path_parts[2:]) if len(path_parts) > 2 else Path()
            return (workspace_root / suffix).absolute()
        shared_projection = _workspace_shared_projection_for_path(path, workspace_root, mounts)
        if shared_projection is not None:
            return shared_projection
        return path.absolute()
    return (workspace_root / path).absolute()


def _normalize_path_tool_args(
    tool_name: str,
    args: dict[str, Any],
    *,
    workspace_root: Path,
    mounts: list[SharedMount],
) -> dict[str, Any]:
    keys = _PATH_NORMALIZATION_KEYS.get(tool_name, ())
    if not keys:
        return args
    updated = dict(args)
    changed = False
    for path_key in keys:
        value = updated.get(path_key)
        if not value or not isinstance(value, str):
            continue
        normalized = _resolve_tool_path(value, workspace_root, mounts)
        normalized_str = str(normalized)
        if normalized_str == value:
            continue
        updated[path_key] = normalized_str
        changed = True
    return updated if changed else args


def _memory_write_allowed(path: Path, workspace_root: Path) -> bool:
    try:
        rel = path.relative_to(workspace_root)
    except ValueError:
        return False
    rel_text = rel.as_posix()
    return rel_text == "MEMORY.md" or rel_text.startswith("memories/")


def _mount_for_workspace_path(
    path: Path, workspace_root: Path, mounts: list[SharedMount]
) -> SharedMount | None:
    try:
        rel = path.relative_to(workspace_root).as_posix()
    except ValueError:
        return None
    for mount in mounts:
        target = mount.target.lstrip("/")
        if rel == target or rel.startswith(target + "/"):
            return mount
    return None


def _skill_root_for_path(path: Path, skill_roots: list[Path]) -> Path | None:
    """Return the skill source root containing *path*, if any.

    The candidate path is fully resolved (symlinks followed) before matching,
    so a ``..`` component or a symlink escaping the skill dir never qualifies
    for the skill-root read grant.
    """
    if not skill_roots:
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    for root in skill_roots:
        try:
            resolved.relative_to(root)
            return root
        except ValueError:
            continue
    return None


def _mount_for_source_path(path: Path, mounts: list[SharedMount]) -> SharedMount | None:
    for mount in mounts:
        try:
            path.relative_to(mount.source)
            return mount
        except ValueError:
            continue
    return None


def _fleet_root_outside_workspace(
    path: Path, workspace_root: Path, fleet_roots: list[FleetRoot] | None
) -> FleetRoot | None:
    """The fleet root holding ``path`` when it lies outside the agent's own workspace.

    The agent's own workspace sits inside ``<home>/agents``, but its rules
    (``shared/`` projections, read-only shared mounts) still apply there, so a
    path inside it is never judged by the fleet grant.
    """
    if not fleet_roots:
        return None
    for candidate, root in ((path, workspace_root), (path, workspace_root.resolve())):
        try:
            candidate.relative_to(root)
            return None
        except ValueError:
            continue
    try:
        if workspace_root.resolve() in path.resolve().parents:
            return None
    except OSError:
        return None
    return fleet_root_for_path(path, fleet_roots)


def _filesystem_boundary_reason(
    *,
    path_text: str,
    resolved_path: Path,
    workspace_root: Path,
    mounts: list[SharedMount],
) -> tuple[str, SharedMount | None]:
    workspace_mount = _mount_for_workspace_path(resolved_path, workspace_root, mounts)
    try:
        resolved_path.relative_to(workspace_root)
    except ValueError:
        source_mount = _mount_for_source_path(resolved_path, mounts)
        if source_mount is not None:
            return "", source_mount
        return f"Path '{path_text}' is outside the workspace and active shared paths.", None

    shared_root = workspace_root / "shared"
    if resolved_path == shared_root:
        return "", None

    if shared_root in resolved_path.parents and workspace_mount is None:
        return f"Path '{path_text}' is not an active shared-path projection.", None

    return "", workspace_mount


def _session_shared_path_configs() -> list[SharedPathConfig]:
    raw = get_request_context().metadata.get("session_shared_paths", [])
    configs: list[SharedPathConfig] = []
    for item in raw:
        try:
            configs.append(SharedPathConfig.model_validate(item))
        except Exception:
            continue
    return configs


def _file_write_block_reason(
    *,
    tool_name: str,
    args: dict[str, Any],
    config: AgentConfig,
    workspace_root: Path,
    mounts: list[SharedMount],
    skill_roots: list[Path] | None = None,
    fleet_roots: list[FleetRoot] | None = None,
) -> str:
    path_text = _path_from_tool_args(args)
    if not path_text:
        return ""

    resolved_path = _resolve_tool_path(path_text, workspace_root, mounts)
    trust_level = int(getattr(config.security, "trust_level", 0))
    if trust_level <= TrustLevel.OBSERVER:
        return "Trust level 0 cannot modify files."
    if trust_level == TrustLevel.ASSISTANT and not _memory_write_allowed(
        resolved_path, workspace_root
    ):
        return "Trust level 1 may only write MEMORY.md and memories/*.md."

    # Fleet filesystem access (AgentFather): every agent's directory and the
    # common skills are writable, the logs are not. Checked before the skill
    # refusal below because `<home>/skills` is one of the writable trees.
    fleet_root = _fleet_root_outside_workspace(resolved_path, workspace_root, fleet_roots)
    if fleet_root is not None:
        if fleet_root.writable:
            return ""
        return f"'{fleet_root.path}' is read-only (fleet access grants it for reading)."

    # Skill sources outside the workspace (e.g. managed <home>/skills/) are
    # readable but never writable — a clearer refusal than the generic
    # outside-the-workspace message. Skill dirs inside the workspace fall
    # through to the normal workspace rules and stay writable.
    skill_root = _skill_root_for_path(resolved_path, skill_roots or [])
    if skill_root is not None:
        try:
            skill_root.relative_to(workspace_root.resolve())
        except ValueError:
            return (
                f"Skill directory '{skill_root}' is read-only for agents. "
                "Copy the skill into the workspace to modify it, or manage "
                "shared skills via the dashboard/CLI."
            )

    boundary_reason, mount = _filesystem_boundary_reason(
        path_text=path_text,
        resolved_path=resolved_path,
        workspace_root=workspace_root,
        mounts=mounts,
    )
    if boundary_reason:
        return boundary_reason
    if mount and mount.mode == "ro":
        return f"Shared path '{mount.target}' is read-only."

    return ""


def _read_tool_candidate_paths(tool_name: str, args: dict[str, Any]) -> list[str]:
    if tool_name == "read_file":
        return [
            value
            for key in ("path", "file_path", "filepath", "target_path")
            if isinstance((value := args.get(key)), str) and value
        ]
    if tool_name == "ls":
        return [
            value
            for key in ("path", "directory", "dir")
            if isinstance((value := args.get(key)), str) and value
        ]
    if tool_name == "glob":
        return [
            value
            for key in ("path", "directory", "dir")
            if isinstance((value := args.get(key)), str) and value
        ]
    if tool_name == "grep":
        return [
            value
            for key in ("path", "file_path", "filepath", "target_path", "directory", "dir")
            if isinstance((value := args.get(key)), str) and value
        ]
    return []


def _file_read_block_reason(
    *,
    tool_name: str,
    args: dict[str, Any],
    workspace_root: Path,
    mounts: list[SharedMount],
    skill_roots: list[Path] | None = None,
    fleet_roots: list[FleetRoot] | None = None,
) -> str:
    for path_text in _read_tool_candidate_paths(tool_name, args):
        resolved_path = _resolve_tool_path(path_text, workspace_root, mounts)
        if _fleet_root_outside_workspace(resolved_path, workspace_root, fleet_roots) is not None:
            continue
        # Skill sources (managed <home>/skills/, config.skills dirs) are
        # advertised in the system prompt with absolute SKILL.md locations —
        # they must be readable even when they live outside the workspace.
        if _skill_root_for_path(resolved_path, skill_roots or []) is not None:
            continue
        boundary_reason, _ = _filesystem_boundary_reason(
            path_text=path_text,
            resolved_path=resolved_path,
            workspace_root=workspace_root,
            mounts=mounts,
        )
        if boundary_reason:
            return boundary_reason
    return ""


def _shell_block_reason(
    *,
    command: str,
    config: AgentConfig,
    workspace_root: Path,
    mounts: list[SharedMount],
) -> str:
    from olano.security.shell_guard import validate_command

    ok, reason = validate_command(command)
    if not ok:
        return reason
    return ""


def _page_command_block_reason(
    *,
    args: dict[str, Any],
    config: AgentConfig,
    workspace_root: Path,
    mounts: list[SharedMount],
) -> str:
    """Why a ``pages_deploy`` call may not record its commands, or ``""``."""
    commands = [str(args.get(key) or "").strip() for key in ("build", "start")]
    commands = [c for c in commands if c]
    if not commands:
        return ""  # the tool itself explains that a deploy needs a command
    from olano.core.builder import sandbox_owner
    from olano.pages import policy

    denial = policy.command_denial(sandbox_owner(config), config)
    if denial:
        return denial
    for command in commands:
        reason = _shell_block_reason(
            command=command, config=config, workspace_root=workspace_root, mounts=mounts
        )
        if reason:
            return reason
    return ""


def _tool_metadata_for_request(tool: Any, tool_name: str) -> ToolMetadata:
    if tool is not None:
        return get_tool_metadata(tool)
    return infer_tool_metadata(tool_name)


def _tool_trusted(tool: Any) -> bool:
    metadata = dict(getattr(tool, "metadata", {}) or {})
    return metadata.get("trusted", True) is not False


def _tool_rate_limit(tool_name: str, config: AgentConfig) -> tuple[bool, int]:
    per_tool = dict((getattr(config.security, "rate_limits", {}) or {}).get("per_tool", {}) or {})
    limit = per_tool.get(tool_name)
    if limit is None:
        return True, 0
    ctx = get_request_context()
    key = f"{config.name}:{ctx.user_id}:{ctx.session_id or 'default'}:{tool_name}"
    decision = TOOL_RATE_LIMITER.check(key, max_calls=int(limit), window_seconds=60)
    return decision.allowed, decision.retry_after


def _block_tool_message(tool_name: str, tool_call_id: str, message: str) -> ToolMessage:
    return ToolMessage(
        content=f"Error: {message}",
        tool_call_id=tool_call_id,
        name=tool_name,
        status="error",
    )


def _note_turn_failure(tool_name: str, args_sig: str, status: Any, content: str) -> str:
    """Record this result on the turn's failure ledger; warn when the breaker is near.

    The runner stops a turn once the identical call has failed
    ``tool_error_retry_limit`` times, and it can only do so after the fact, from
    the stream. The model, though, reads *this* message, so the warning that one
    more identical failure ends the turn has to be written here, on the failing
    result itself. A successful write resets the ledger (the workspace changed).
    No-op outside a runner turn (no ledger bound).
    """
    ledger = current_turn_tool_failures()
    if ledger is None:
        return content
    if not is_failed_tool_result(status, content):
        ledger.record_success(tool_name)
        return content
    count = ledger.record_failure(tool_call_signature(tool_name, args_sig))
    notice = ledger.notice(tool_name, count)
    if not notice:
        return content
    return f"{content.rstrip()}\n\n{notice}"


def _with_turn_failure_notice(tool_name: str, args_sig: str, message: ToolMessage) -> ToolMessage:
    content = _stringify_content(message.content)
    noted = _note_turn_failure(tool_name, args_sig, message.status, content)
    if noted == content:
        return message
    return message.model_copy(update={"content": noted})


def _extract_sandbox(backend: Any | None) -> Any | None:
    """The OlanoDockerBackend reachable from `backend` — started or not.

    Handles a direct sandbox backend AND one nested as the default/a route of a
    deepagents CompositeBackend (whose own `.execute` may not delegate to the
    sandbox), so a composite agent with a shell route still runs `execute` in the
    isolated container instead of being refused.
    """
    if backend is None:
        return None
    try:
        from olano.backends.lionclaw_docker import OlanoDockerBackend

        if isinstance(backend, OlanoDockerBackend):
            return backend
        # CompositeBackend: check the default then the routes.
        candidates = []
        default = getattr(backend, "default", None)
        if default is not None:
            candidates.append(default)
        routes = getattr(backend, "routes", None)
        if isinstance(routes, dict):
            candidates.extend(routes.values())
        for sub in candidates:
            if isinstance(sub, OlanoDockerBackend):
                return sub
    except Exception:
        return None
    return None


def _sandbox_started(sandbox: Any | None) -> bool:
    """True when the backend is bound to a container, i.e. ``start()`` has run.

    ``bool(...)``, not ``is not None``: ``container_ref()`` returns
    ``_container_id or _container_name``, so an empty string is representable —
    ``terminal.sandbox_container_ref`` already normalises it with ``or None``.
    """
    try:
        return bool(sandbox is not None and sandbox.container_ref())
    except Exception:
        return False


def _extract_live_sandbox(backend: Any | None) -> Any | None:
    """The **started** OlanoDockerBackend reachable from `backend`, or None."""
    sandbox = _extract_sandbox(backend)
    return sandbox if _sandbox_started(sandbox) else None


def _is_live_sandbox_backend(backend: Any | None) -> bool:
    """True when a STARTED OlanoDockerBackend is reachable from `backend`.

    This used to be an `isinstance` check, which made an unstarted backend read
    as live: the managed fail-closed chokepoint in `_run_shell_command` waved it
    through and the command was routed into a container that had never been
    created, coming back as exit 127 instead of an actionable refusal.
    """
    return _extract_live_sandbox(backend) is not None


def _run_shell_command(
    *,
    tool_name: str,
    args: dict[str, Any],
    config: AgentConfig,
    workspace_root: Path,
    mounts: list[SharedMount],
    tool_call_id: str,
    backend: Any | None = None,
) -> ToolMessage:
    from olano.security.shell_guard import run_local, validate_command

    if tool_name in {"safe_shell", "safe_shell_tool", "execute"}:
        command = str(args.get("command") or "")
        timeout = int(args.get("timeout", 30) or 30)
    elif tool_name in {"node_run", "node_run_tool"}:
        script_or_command = str(args.get("script_or_command") or "")
        extra_args = [str(v) for v in (args.get("args") or [])]
        command = shlex.join([script_or_command, *extra_args]) if script_or_command else ""
        timeout = int(args.get("timeout", 60) or 60)
    else:
        package = str(args.get("package") or "")
        global_install = bool(args.get("global_install"))
        parts = ["npm", "install"]
        if global_install:
            parts.append("-g")
        if package:
            parts.append(package)
        command = shlex.join(parts)
        timeout = 120

    # FAIL-CLOSED chokepoint. Every shell-family tool (execute/safe_shell/
    # node_run/npm_install) reaches host execution ONLY through here. On a
    # managed box, host execution is forbidden — so unless there is a live
    # isolated sandbox to run in, refuse. This closes the bypass where an agent
    # whose backend was coerced to filesystem still lists a shell tool in its
    # `tools:` (which would set has_shell_access via the tool list and otherwise
    # fall through to run_local on the host).
    # A sandbox that exists in config but never came up gets its own message.
    # Without this the open-posture `else` below would silently run the command
    # on the HOST for an agent whose config explicitly asked for a container.
    dead_sandbox = _extract_sandbox(backend)
    if dead_sandbox is not None and not _sandbox_started(dead_sandbox):
        return _block_tool_message(
            tool_name,
            tool_call_id,
            "This agent's sandbox container is not running, so no command can "
            "execute. Restart the agent to rebuild its sandbox.",
        )

    if not _is_live_sandbox_backend(backend):
        try:
            from olano.deployment import enforcement as dep_enforcement

            if dep_enforcement.require_sandboxed_exec():
                return _block_tool_message(
                    tool_name,
                    tool_call_id,
                    "Shell execution is disabled on this managed deployment "
                    "(no code runs in the runtime container). Enable the sandbox "
                    "execution backend to run commands.",
                )
        except Exception:
            logger.exception("managed shell gate failed; denying shell to be safe")
            return _block_tool_message(tool_name, tool_call_id, "Shell execution is unavailable.")

    ok, block_reason = validate_command(command)
    if not ok:
        return _block_tool_message(tool_name, tool_call_id, block_reason)

    live_sandbox = _extract_live_sandbox(backend)
    if live_sandbox is not None:
        # Isolated execution: run in the sandbox container (docker exec). It
        # holds only the workspace at /workspace — no product source, no
        # secrets, no host filesystem — so `python -c "open('/app/...')"` and
        # `printenv` simply have nothing to steal. Use the extracted sandbox
        # (not a CompositeBackend wrapper, whose execute may not delegate).
        try:
            # `run()`, not deepagents' `execute()`: the summariser below previews
            # stdout and stderr separately, and `execute()` returns them merged.
            res = live_sandbox.run(command, timeout=timeout)
        except Exception as exc:
            return _block_tool_message(tool_name, tool_call_id, f"Sandbox execution failed: {exc}")
        _stdout, _stderr, _returncode = str(res.stdout), str(res.stderr), int(res.returncode)
    else:
        result = run_local(command, timeout=timeout, cwd=str(workspace_root))
        _stdout, _stderr, _returncode = result.stdout, result.stderr, result.returncode

    summary = summarize_shell_output(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        workspace_root=workspace_root,
        command=command,
        runtime_command=command,
        timeout=timeout,
        stdout=_stdout,
        stderr=_stderr,
        returncode=_returncode,
    )
    output = summary.content
    artifact = summary.artifact
    artifact_ref = str((artifact or {}).get("workspace_log_path") or "")
    if artifact_ref:
        output = output.replace(artifact_ref, _SHELL_LOG_PATH_PLACEHOLDER)
    output = _sanitize_result(tool_name, output, config)
    if artifact_ref:
        output = output.replace(_SHELL_LOG_PATH_PLACEHOLDER, artifact_ref)
    return ToolMessage(
        content=output or "(no output)",
        tool_call_id=tool_call_id,
        name=tool_name,
        artifact=artifact,
        status="success" if _returncode == 0 else "error",
    )


class OlanoSecurityMiddleware(AgentMiddleware):
    """Unified Phase 2 security middleware chain for deepagents."""

    def __init__(
        self,
        *,
        config: AgentConfig,
        workspace_root: Path,
        hooks: Any | None = None,
        backend: Any | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.workspace_root = workspace_root
        self.hooks = hooks
        # The live backend instance. When it is an isolated sandbox
        # (OlanoDockerBackend), shell/exec is routed to its `execute()`
        # (docker exec) instead of the host shell — the source-/secret-free
        # container is the ONLY place agent code runs on a managed box.
        self.backend = backend
        self._skill_roots: list[Path] | None = None
        # Fleet filesystem access (security/fleet_access.py): fixed for the
        # middleware's lifetime, like the config it is read from.
        self._fleet_roots: list[FleetRoot] = []
        try:
            self._fleet_roots = fleet_roots(config)
        except Exception:
            logger.exception("could not resolve fleet access roots; keeping the workspace boundary")

    @property
    def name(self) -> str:
        return "OlanoSecurityMiddleware"

    def _active_mounts(self) -> list[SharedMount]:
        return resolve_shared_mounts(self.config, _session_shared_path_configs())

    def _skill_read_roots(self) -> list[Path]:
        """Skill source dirs (resolved) this agent may read; cached per instance.

        The source dirs themselves are created at agent-build time, so the set
        is stable for the middleware's lifetime — only their *contents* change
        when skills are installed or learned mid-session.
        """
        if self._skill_roots is None:
            try:
                from olano.config.workspace import Workspace
                from olano.skills.loader import skill_read_roots

                self._skill_roots = skill_read_roots(
                    self.config, Workspace(root=self.workspace_root)
                )
            except Exception:
                logger.debug("Could not resolve skill read roots", exc_info=True)
                self._skill_roots = []
        return self._skill_roots

    # The four hooks below rewrite at most ONE message (the last human message
    # on the way in, the last AI message on the way out) and return a state
    # update holding only that message. They used to return the whole list:
    # the reducer merges by id either way, so the result was the same, but
    # LangGraph persists a hook's return value as that step's write, and on a
    # thread with months of history the write was the entire conversation,
    # every step (42 MB per step on a managed box's peer thread) and was then
    # replayed on every load of the thread until the next snapshot. See
    # ``_replacement_update``.

    def _scan_human(self, messages: list[Any], human_idx: int) -> Any | None:
        """Scan the last human message; the rewritten message or ``None``."""
        sec = self.config.security
        human = messages[human_idx]
        content = _stringify_content(human.content)
        if sec.input_scan:
            from olano.security.input_scan import scan_input

            result = scan_input(content)
            if not result.is_safe and result.risk_level == "high" and sec.profile == "production":
                findings = ", ".join(f["category"] for f in result.findings)
                raise ValueError(
                    f"Message blocked: potential prompt injection detected ({findings})."
                )
        if sec.pii_scan_input:
            from olano.security.pii_middleware import apply as pii_apply
            from olano.security.pii_middleware import peer_exchange_exempt

            # Peer (talk_to_agent) turns are exempt unless the agent opts
            # back in; mirrors runner._scan_input.
            if not peer_exchange_exempt(sec):
                cleaned, _ = pii_apply(content, sec.pii_action)
                if cleaned != content:
                    return _replace_message_content(human, cleaned)
        return None

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        human_idx = _find_last_message_index(messages, HumanMessage)
        if human_idx is None:
            return None
        replaced = self._scan_human(messages, human_idx)
        if replaced is None:
            return None
        return _replacement_update(messages, human_idx, replaced)

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        human_idx = _find_last_message_index(messages, HumanMessage)
        if human_idx is None:
            return None
        replaced = self._scan_human(messages, human_idx)
        if self.hooks and not self.hooks.is_empty():
            try:
                from olano.core.middleware_loader import InputBlockedError, run_input_hooks

                hook_context = {
                    "thread_id": get_request_context().session_id,
                    "agent_name": self.config.name,
                    "workspace_root": str(self.workspace_root),
                }
                human = replaced if replaced is not None else messages[human_idx]
                content = _stringify_content(human.content)
                hooked = await run_input_hooks(content, self.hooks, self.config, hook_context)
                if hooked != content:
                    replaced = _replace_message_content(human, hooked)
            except InputBlockedError as exc:
                raise ValueError(f"Message blocked by middleware: {exc}") from exc
        if replaced is None:
            return None
        return _replacement_update(messages, human_idx, replaced)

    def _scan_ai(self, messages: list[Any], idx: int) -> Any | None:
        """Scan the last AI message; the rewritten message or ``None``."""
        ai = messages[idx]
        original = _stringify_content(ai.content)
        content = original
        sec = self.config.security
        if sec.output_scan:
            from olano.security.input_scan import scan_output

            scan_output(content)
        if getattr(sec, "credential_scrub", True):
            content, _ = scrub_text(
                content, patterns_path=getattr(sec, "scrub_patterns_path", None)
            )
        if sec.pii_scan_output:
            from olano.security.pii_middleware import apply as pii_apply
            from olano.security.pii_middleware import peer_exchange_exempt

            # Peer (talk_to_agent) replies are exempt unless the agent opts
            # back in; mirrors runner._scan_output.
            if not peer_exchange_exempt(sec):
                content, _ = pii_apply(content, sec.pii_action)
        if content == original:
            return None
        return _replace_message_content(ai, content)

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        idx = _find_last_message_index(messages, AIMessage)
        if idx is None:
            return None
        replaced = self._scan_ai(messages, idx)
        if replaced is None:
            return None
        return _replacement_update(messages, idx, replaced)

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        idx = _find_last_message_index(messages, AIMessage)
        if idx is None:
            return None
        replaced = self._scan_ai(messages, idx)
        if self.hooks and not self.hooks.is_empty():
            from olano.core.middleware_loader import run_output_hooks

            hook_context = {
                "thread_id": get_request_context().session_id,
                "agent_name": self.config.name,
                "workspace_root": str(self.workspace_root),
            }
            ai = replaced if replaced is not None else messages[idx]
            content = _stringify_content(ai.content)
            hooked = await run_output_hooks(content, self.hooks, self.config, hook_context)
            if hooked != content:
                replaced = _replace_message_content(ai, hooked)
        if replaced is None:
            return None
        return _replacement_update(messages, idx, replaced)

    def _carry_skill_renames(self, renames: dict[str, str]) -> None:
        """Keep a skill this agent switched off switched off after its name moved."""
        try:
            from olano.admin.skills import carry_disabled_skill_renames

            carry_disabled_skill_renames(renames, [self.config.name])
        except Exception:
            logger.debug("Could not carry a skill rename into disabled_skills", exc_info=True)

    def _carry_skill_rename(self, before: str | None, skill_md: Path) -> None:
        """Carry the name *skill_md* held before this write, if the write changed it."""
        after = _skill_name_on_disk(skill_md)
        if before and after and before != after:
            self._carry_skill_renames({before: after})

    def _align_workspace_skill_names(self) -> str | None:
        """Sweep this workspace's skills after a shell command named SKILL.md.

        Applies only the fix that moves no path (``naming.align_skill_names``)
        and returns a note for the model naming each rewrite, or ``None``.
        """
        try:
            from olano.skills.naming import align_skill_names

            rewritten = align_skill_names([self.workspace_root / "skills"])
        except Exception:
            logger.debug("Skill name sweep after a shell command failed", exc_info=True)
            return None
        if not rewritten:
            return None
        self._carry_skill_renames({r["from"]: r["to"] for r in rewritten})
        listed = "; ".join(f"`{r['from']}` -> `{r['to']}`" for r in rewritten)
        return (
            f"[skill-name] Set skill names to their folders ({listed}): a skill's name must "
            "equal its folder and be lowercase letters, digits and single hyphens. Refer to "
            "these skills by the new names."
        )

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        return asyncio.run(self.awrap_tool_call(request, handler))

    def _publish_subagent_message(self, payload: dict[str, Any]) -> None:
        """Publish A2A task activity from the agent that actually owns it."""
        try:
            from olano.tools import talk_to as tt

            publisher = getattr(tt, "_PUBLISHER", None)
            if publisher:
                publisher("agent_message", payload)
        except Exception:
            logger.debug("Failed to publish subagent activity", exc_info=True)

    def _publish_thread_message(self, payload: dict[str, Any]) -> None:
        """Mirror tool activity into the owning agent's visible chat thread."""
        try:
            from olano.tools import talk_to as tt

            publisher = getattr(tt, "_PUBLISHER", None)
            if publisher:
                publisher("thread_message", payload)
        except Exception:
            logger.debug("Failed to publish thread message", exc_info=True)

    @staticmethod
    def _task_result_content(result: Any) -> str:
        if isinstance(result, ToolMessage):
            return _stringify_content(result.content)
        update = getattr(result, "update", None)
        if isinstance(update, dict):
            messages = update.get("messages") or []
            for msg in reversed(messages):
                if isinstance(msg, ToolMessage):
                    return _stringify_content(msg.content)
                content = getattr(msg, "content", None)
                if content:
                    return _stringify_content(content)
        if isinstance(result, dict):
            update = result.get("update") if isinstance(result.get("update"), dict) else result
            messages = update.get("messages") if isinstance(update, dict) else None
            if isinstance(messages, list):
                for msg in reversed(messages):
                    if isinstance(msg, ToolMessage):
                        return _stringify_content(msg.content)
                    if isinstance(msg, dict) and msg.get("content"):
                        return _stringify_content(msg["content"])
        return _stringify_content(result)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        tool_name = str(request.tool_call.get("name", "tool"))
        tool_call_id = str(request.tool_call.get("id", ""))
        raw_args = request.tool_call.get("args", {})
        args = _normalize_tool_args(raw_args)

        # Record before execution — this is the authoritative source for runner.py
        # regardless of how the LLM provider streamed the tool call args.
        if tool_call_id:
            _record_tool_args(tool_call_id, tool_name, args)
        # The breaker's "identical call" key is computed from these recorded
        # args (the runner reads the same registry entry), before hooks or path
        # normalization rewrite them.
        args_sig = args_signature_text(args)

        if self.hooks and not self.hooks.is_empty():
            try:
                from olano.core.middleware_loader import run_tool_call_hooks

                hook_context = {
                    "thread_id": get_request_context().session_id,
                    "agent_name": self.config.name,
                    "workspace_root": str(self.workspace_root),
                }
                hooked_args = await run_tool_call_hooks(
                    tool_name, args, self.hooks, self.config, hook_context
                )
                if hooked_args is None:
                    return _block_tool_message(
                        tool_name, tool_call_id, "Tool call blocked by middleware hook."
                    )
                args = hooked_args
                request = request.override(tool_call={**request.tool_call, "args": args})
            except Exception:
                logger.debug("Tool-call hooks skipped for %s", tool_name, exc_info=True)

        metadata = _tool_metadata_for_request(request.tool, tool_name)
        decision = evaluate_tool_access(tool_name, metadata, self.config)
        if not decision.allowed:
            return _block_tool_message(
                tool_name,
                tool_call_id,
                f"tool blocked at security layer {decision.layer}: {decision.reason}",
            )

        ctx = get_request_context()
        explicit_peer_target = str((ctx.metadata or {}).get("explicit_peer_target") or "")
        if tool_name == "task" and explicit_peer_target and ctx.source != "peer":
            return _block_tool_message(
                tool_name,
                tool_call_id,
                (
                    f"The user explicitly asked you to ask peer agent '{explicit_peer_target}'. "
                    "Do not use your own subagents for this turn. Call "
                    f'`talk_to_agent(agent_name="{explicit_peer_target}", message="...")` '
                    "and then report that peer's answer."
                ),
            )

        allowed, retry_after = _tool_rate_limit(tool_name, self.config)
        if not allowed:
            return _block_tool_message(
                tool_name,
                tool_call_id,
                f"tool rate limit exceeded for '{tool_name}'. Retry after {retry_after}s.",
            )

        mounts = self._active_mounts()
        normalized_args = _normalize_path_tool_args(
            tool_name,
            args,
            workspace_root=self.workspace_root,
            mounts=mounts,
        )
        if normalized_args != args:
            args = normalized_args
            request = request.override(tool_call={**request.tool_call, "args": args})

        if (
            self._fleet_roots
            and _extract_live_sandbox(self.backend) is not None
            and (
                tool_name in _WRITE_FILE_TOOL_NAMES
                or tool_name in _READ_FILE_TOOL_NAMES
                or tool_name in _SHELL_TOOL_NAMES
            )
        ):
            # The runtime keeps writing these trees as root (config saves, token
            # refreshes, skill syncs); re-own them before a tool that runs as the
            # sandbox user touches them. Only with a live container (a host shell
            # runs as the runtime's own user); throttled; a no-op when not root.
            await asyncio.to_thread(handoff_fleet_trees, self._fleet_roots)

        if tool_name in _WRITE_FILE_TOOL_NAMES:
            block_reason = _file_write_block_reason(
                tool_name=tool_name,
                args=args,
                config=self.config,
                workspace_root=self.workspace_root,
                mounts=mounts,
                skill_roots=self._skill_read_roots(),
                fleet_roots=self._fleet_roots,
            )
            if block_reason:
                return _block_tool_message(tool_name, tool_call_id, block_reason)

        # A skill's `name` must equal its folder (Agent Skills spec), and the
        # model composes that frontmatter itself whenever it writes a skill.
        # A whole-file write is judged before it lands (a title in `name:` is
        # set to the folder; a name that needs another folder is refused with
        # the path to use); an edit is checked after it (below).
        skill_md: Path | None = None
        skill_note: str | None = None
        skill_name_before: str | None = None
        if tool_name in _WRITE_FILE_TOOL_NAMES:
            skill_md = _skill_md_target(args, self.workspace_root, mounts)
        if skill_md is not None:
            skill_name_before = await asyncio.to_thread(_skill_name_on_disk, skill_md)
        if skill_md is not None and tool_name == "write_file":
            from olano.skills.naming import check_skill_write

            verdict = check_skill_write(
                skill_md, str(args.get("content") or ""), existing_name=skill_name_before
            )
            if verdict.refusal:
                return _block_tool_message(tool_name, tool_call_id, verdict.refusal)
            if verdict.content is not None:
                args = {**args, "content": verdict.content}
                request = request.override(tool_call={**request.tool_call, "args": args})
            skill_note = verdict.note

        if tool_name in _READ_FILE_TOOL_NAMES:
            block_reason = _file_read_block_reason(
                tool_name=tool_name,
                args=args,
                workspace_root=self.workspace_root,
                mounts=mounts,
                skill_roots=self._skill_read_roots(),
                fleet_roots=self._fleet_roots,
            )
            if block_reason:
                return _block_tool_message(tool_name, tool_call_id, block_reason)

        if tool_name == "read_file":
            # deepagents returns audio, video and documents as one base64 block
            # the context can never shrink; send them to the tool for the format.
            for path_text in _read_tool_candidate_paths(tool_name, args):
                refusal = await asyncio.to_thread(
                    read_file_refusal, _resolve_tool_path(path_text, self.workspace_root, mounts)
                )
                if refusal:
                    return _block_tool_message(tool_name, tool_call_id, refusal)

        if tool_name in _PAGE_COMMAND_TOOL_NAMES:
            # pages_deploy records build/start commands that the page
            # supervisor runs later, where this agent's shell runs. Gate them
            # here exactly like a shell call (shell access, the shell guard,
            # a managed box's sandbox rule), before the tool body records
            # anything: the supervisor re-checks, but a refused deploy should
            # never reach the registry in the first place.
            block_reason = _page_command_block_reason(
                args=args,
                config=self.config,
                workspace_root=self.workspace_root,
                mounts=mounts,
            )
            if block_reason:
                return _block_tool_message(tool_name, tool_call_id, block_reason)

        if tool_name in _SHELL_TOOL_NAMES:
            # CRITICAL: only dispatch to the local shell runner when the
            # agent actually has shell access. Without this guard, an LLM
            # that hallucinates `safe_shell_tool` (e.g. from a polluted
            # checkpointer history) will see its call SUCCEED — the
            # middleware would otherwise route any shell-style tool name
            # straight to the host shell regardless of whether the agent
            # registered the tool. Emily / Mark / etc. with `backend:
            # filesystem` and no shell tool in their tools list got a
            # real shell anyway.
            agent_tools = set(getattr(self.config, "tools", []) or [])
            backend_obj = getattr(self.config, "backend", None)
            backend_type = getattr(backend_obj, "type", None) or (
                backend_obj if isinstance(backend_obj, str) else ""
            )
            # A live sandbox backend (OlanoDockerBackend) grants shell too —
            # it runs the command in the isolated container, not on the host.
            live_sandbox = _is_live_sandbox_backend(self.backend)
            has_shell_access = (
                bool(agent_tools & _SHELL_TOOL_NAMES)
                or backend_type in ("local_shell", "filesystem+shell", "lionclaw_docker")
                or live_sandbox
            )
            if not has_shell_access:
                return _block_tool_message(
                    tool_name,
                    tool_call_id,
                    (
                        f"Tool '{tool_name}' is not available to this agent. "
                        f"This agent's tool list does not include any shell tool, and "
                        f"its backend ({backend_type or 'filesystem'}) does not grant "
                        f"shell access. To run shell commands, delegate to a peer agent "
                        f"that has shell access (e.g. the IT Admin) via talk_to_agent."
                    ),
                )

            shell_command = str(
                args.get("command") or args.get("script_or_command") or args.get("package") or ""
            )
            block_reason = _shell_block_reason(
                command=shell_command,
                config=self.config,
                workspace_root=self.workspace_root,
                mounts=mounts,
            )
            if block_reason:
                return _block_tool_message(tool_name, tool_call_id, block_reason)
            shell_result = _run_shell_command(
                tool_name=tool_name,
                args=args,
                config=self.config,
                workspace_root=self.workspace_root,
                mounts=mounts,
                tool_call_id=tool_call_id,
                backend=self.backend,
            )
            if "SKILL.md" in shell_command:
                # A heredoc or a script wrote a skill where no file tool could
                # judge it: align what the folder can absorb, right away.
                shell_result = _with_skill_note(
                    shell_result, await asyncio.to_thread(self._align_workspace_skill_names)
                )
            return _with_turn_failure_notice(tool_name, args_sig, shell_result)

        subagent_activity: dict[str, Any] | None = None
        if tool_name == "task":
            subagent_type = str(args.get("subagent_type") or "").strip()
            description = str(args.get("description") or "").strip()
            if subagent_type:
                ts_sent = datetime.now(timezone.utc).isoformat()
                subagent_activity = {
                    "kind": "subagent",
                    "phase": "send",
                    "from": self.config.name,
                    "to": subagent_type,
                    "message": description or "(delegated task)",
                    "ts": ts_sent,
                    "tool_call_id": tool_call_id,
                }
                self._publish_subagent_message(subagent_activity)
                thread_id = ctx.session_id
                if thread_id:
                    self._publish_thread_message(
                        {
                            "agent": self.config.name,
                            "thread_id": thread_id,
                            "role": "tool_call",
                            "message_id": f"task::{tool_call_id}",
                            "tool_call_id": tool_call_id,
                            "name": "task",
                            "args": json.dumps(
                                {
                                    "subagent_type": subagent_type,
                                    "description": description,
                                }
                            ),
                            "ts": ts_sent,
                        }
                    )

        # Deployment + agent egress lists, resolved per call so a Config-page
        # save reaches the next tool call (see security/egress.py).
        # `egress_governed` clears any exemption an enclosing nested-agent run
        # set, so a tool is always governed by its own agent's lists.
        with (
            push_request_context(metadata={"security": egress_security_metadata(self.config)}),
            egress_governed(),
        ):
            result = await handler(request)

        if isinstance(result, ToolMessage):
            content = _stringify_content(result.content)
            trusted = _tool_trusted(request.tool)
            content = _sanitize_result(tool_name, content, self.config, untrusted=not trusted)
            if self.hooks and not self.hooks.is_empty():
                try:
                    from olano.core.middleware_loader import run_tool_result_hooks

                    hook_context = {
                        "thread_id": get_request_context().session_id,
                        "agent_name": self.config.name,
                        "workspace_root": str(self.workspace_root),
                    }
                    content = await run_tool_result_hooks(
                        tool_name, content, self.hooks, self.config, hook_context
                    )
                except Exception:
                    logger.debug("Tool-result hooks skipped for %s", tool_name, exc_info=True)
            if subagent_activity is not None:
                self._publish_subagent_message(
                    {
                        "kind": "subagent",
                        "phase": "reply",
                        "from": subagent_activity["from"],
                        "to": subagent_activity["to"],
                        "message": subagent_activity["message"],
                        "reply": content,
                        "ts_sent": subagent_activity["ts"],
                        "ts_reply": datetime.now(timezone.utc).isoformat(),
                        "tool_call_id": tool_call_id,
                    }
                )
                thread_id = ctx.session_id
                if thread_id:
                    self._publish_thread_message(
                        {
                            "agent": self.config.name,
                            "thread_id": thread_id,
                            "role": "tool_result",
                            "message_id": f"task::{tool_call_id}",
                            "tool_call_id": tool_call_id,
                            "name": "task",
                            "content": content,
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }
                    )
            if skill_md is not None and result.status != "error":
                if tool_name == "edit_file":
                    from olano.skills.naming import check_skill_file

                    skill_note = await asyncio.to_thread(check_skill_file, skill_md)
                if skill_note:
                    content = f"{content.rstrip()}\n\n{skill_note}"
                    await asyncio.to_thread(self._carry_skill_rename, skill_name_before, skill_md)
            content = _note_turn_failure(tool_name, args_sig, result.status, content)
            return ToolMessage(
                content=content,
                tool_call_id=result.tool_call_id,
                name=result.name or tool_name,
                id=result.id,
                artifact=result.artifact,
                status=result.status,
                additional_kwargs=result.additional_kwargs,
                response_metadata=result.response_metadata,
            )

        if subagent_activity is not None:
            content = self._task_result_content(result)
            self._publish_subagent_message(
                {
                    "kind": "subagent",
                    "phase": "reply",
                    "from": subagent_activity["from"],
                    "to": subagent_activity["to"],
                    "message": subagent_activity["message"],
                    "reply": content,
                    "ts_sent": subagent_activity["ts"],
                    "ts_reply": datetime.now(timezone.utc).isoformat(),
                    "tool_call_id": tool_call_id,
                }
            )
            thread_id = ctx.session_id
            if thread_id:
                self._publish_thread_message(
                    {
                        "agent": self.config.name,
                        "thread_id": thread_id,
                        "role": "tool_result",
                        "message_id": f"task::{tool_call_id}",
                        "tool_call_id": tool_call_id,
                        "name": "task",
                        "content": content,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                )
        return result


def build_security_middleware(
    *,
    config: AgentConfig,
    workspace_root: Path,
    hooks: Any | None = None,
    backend: Any | None = None,
) -> list[AgentMiddleware]:
    return [
        OlanoSecurityMiddleware(
            config=config,
            workspace_root=workspace_root,
            hooks=hooks,
            backend=backend,
        )
    ]
