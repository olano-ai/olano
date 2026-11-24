"""Build a deepagent from an AgentConfig."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import logging
import os
import re
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast

from langchain_core.language_models import BaseChatModel

from olano.config.agent_config import AgentConfig, SandboxResourceOverride, SubAgentConfig
from olano.config.autonomy_defaults import (
    available_magic_link_tool_names,
    available_self_config_tool_names,
)
from olano.config.backend_config import (
    AgentCoreBackendConfig,
    BackendConfig,
    CompositeBackendConfig,
    DaytonaBackendConfig,
    FilesystemBackendConfig,
    LangSmithBackendConfig,
    LocalShellBackendConfig,
    ModalBackendConfig,
    OlanoDockerBackendConfig,
    RunloopBackendConfig,
    StateBackendConfig,
    StoreBackendConfig,
    _default_sandbox_image,
    is_remote_sandbox_backend,
)
from olano.config.internal_tools import (
    available_a2a_tool_names,
    available_whatsapp_business_tool_names,
)
from olano.config.multimodal_defaults import available_multimodal_tool_names
from olano.config.ops_defaults import BUILTIN_DOCUMENT_TOOL_NAMES
from olano.config.workspace import get_workspace
from olano.core.boot_profile import phase as boot_phase
from olano.core.checkpoints import get_async_checkpointer, get_checkpointer
from olano.core.stores import get_async_store, get_store
from olano.models import build_model
from olano.skills.loader import load_skills_for_agent, load_subagent_registry
from olano.tools._error_boundary import guard_tools
from olano.tools.registry import resolve_tools

if TYPE_CHECKING:
    from olano.core.host_resources import SandboxLimits

logger = logging.getLogger(__name__)


# Serializes the synchronous half of an agent build.
#
# Agent build is heavy, blocking CPU work — importing every toolkit (~5.8s on a
# cold process), installing the deepagents middleware patch (~2.1s), skill
# discovery, prompt assembly, graph compilation (~0.7s). Run inline on the event
# loop it starves uvicorn for the whole boot, so ``/api/status`` — the readiness
# probe the dashboard polls and every installer waits on — goes unanswered and a
# perfectly healthy runtime reads as offline. deepagents' boot-time RAG ingest
# hit this first and was moved to a thread (``_spawn_workspace_ingest``); the
# async build path now does the same for the rest of the build.
#
# That removes a guarantee the code used to get for free. Inline on the loop,
# ``_prepare_agent_build`` had no await inside it, so no two builds could ever
# interleave and the process-global state they touch (the one-shot deepagents
# patches, the tool registry) needed no protection. In a worker thread two
# builds genuinely run at once, so take the lock explicitly instead. Reentrant
# because the sync path composes several locked phases in one call.
_BUILD_LOCK = threading.RLock()


@contextlib.contextmanager
def _transient_env(env: dict[str, str]) -> Iterator[None]:
    """Apply ``env`` to ``os.environ`` for the block, then restore prior values.

    Per-agent ``config.env`` is resolved per-turn as a credential overlay
    (``tools/_credentials``) for call-time toolkit reads. But a few BUILD-time
    consumers — notably model provider keys read by
    ``models._hydrate_provider_env_from_vault`` — still read the ambient process
    env. We apply the agent's env transiently around those build steps and
    restore it, so per-agent provider keys keep working without permanently
    polluting the shared env that call-time resolution falls back to.

    **This window must not run off the event loop.** Its safety rests entirely on
    nothing else executing between the write and the restore: the keys are
    process-global, so a concurrent request handler reading ``ANTHROPIC_API_KEY``
    mid-window would get another agent's credential. On the loop that holds (no
    await inside); in a worker thread it does not. ``_prepare_agent_build_async``
    therefore keeps the overlay build on the loop — a ~1ms constructor call once
    ``_warm_model_provider`` has imported the provider package off-loop.
    """
    if not env:
        yield
        return
    saved: dict[str, str | None] = {key: os.environ.get(key) for key in env}
    try:
        for key, value in env.items():
            os.environ[key] = value
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _merge_provider_override(env: dict[str, str], config: AgentConfig) -> None:
    """Fold a per-agent vault **override** of this model's provider key into the
    transient build env.

    Common provider keys are already resolved by
    ``models._hydrate_provider_env_from_vault`` (which reads the common vault and
    persists into ``os.environ``), so this only acts when an actual per-agent
    *override* row exists — letting a different ``ANTHROPIC_API_KEY`` (etc.) live
    as a vault override per agent, not only in ``config.env``. ``config.env``
    still wins (we never clobber a key it already set). Best-effort; never raises.
    """
    try:
        from olano.models import PROVIDERS, parse_model_string
        from olano.vault.resolver import get_vault

        provider, _ = parse_model_string(config.model)
        if not provider:
            return
        info = PROVIDERS.get(provider)
        env_var = getattr(info, "env_var", None)
        if not env_var or env_var in env:
            return
        vault = get_vault()
        if vault is None:
            return
        # Only when a per-agent override row exists (not just a common value).
        scope = config.credential_scope or config.name
        if not vault.list_overrides(name=env_var, agent=scope):
            return
        value = vault.get(env_var, agent=scope, touch=False)
        if value:
            env[env_var] = value
    except Exception:
        return


# Eagerly import the MCP stdio client so that ``stdio_client``'s ``errlog``
# default argument (``sys.stderr``) is bound now—while stdout/stderr are the
# real terminal. If the first import happened lazily inside an output-suppression
# redirect (see ``olano.cli._quiet``), ``errlog`` would capture a temporary
# file that is closed once the build finishes, and every later stdio MCP tool
# call would fail with ``ValueError: I/O operation on closed file``.
# builder.py is always imported before any such redirect, so this binds safely.
_mcp_stdio_prewarm: ModuleType | None
try:  # pragma: no cover - optional dependency, presence verified at runtime
    import mcp.client.stdio as _mcp_stdio_prewarm
except Exception:  # pragma: no cover - langchain-mcp-adapters/mcp not installed
    _mcp_stdio_prewarm = None

# ── Helpers ───────────────────────────────────────────────────────────────────

_GRAPH_TOOL_NAMES = [
    "graph_search_entities",
    "graph_get_neighbors",
    "graph_add_entity",
    "graph_add_relationship",
    "graph_summary",
]

_NOTES_TOOL_NAMES = [
    "notes_add",
    "notes_get",
    "notes_search",
    "notes_list",
    "notes_backlinks",
]

_SECRET_TOOL_NAMES = [
    "secret_get",
    "secret_list",
    "secret_set",
]


def _warm_model_provider(model_str: str) -> None:
    """Import the provider's LangChain package ahead of the model build.

    ``init_chat_model`` imports ``langchain_<provider>`` on first use, and that
    import — not the constructor — is what makes a cold model build expensive.
    Doing it here lets the async path run the import off the event loop and keep
    only the constructor inside the ``_transient_env`` window that has to stay on
    it. Best-effort: a failure just means the import happens later, where it
    always used to.

    Never warms ``langchain_fireworks``: importing it used to register a vendored
    ``google/api/field_behavior.proto`` that collided with the langchain-google
    providers and broke every subsequent build (``models._PROVIDER_STRICT_SKIP``
    keeps the same distance). It also matters to
    ``_ensure_deepagents_provider_imports_deferred``: a loaded
    ``langchain_fireworks`` is what lets deepagents try its Fireworks
    prompt-caching middleware.
    """
    try:
        import importlib

        from olano.models import PROVIDERS, parse_model_string

        provider, _ = parse_model_string(model_str)
        info = PROVIDERS.get(provider or "")
        package = getattr(info, "package", None)
        if not package or not package.startswith("langchain-") or "fireworks" in package:
            return
        importlib.import_module(package.replace("-", "_"))
    except Exception as exc:  # pragma: no cover - provider not installed, etc.
        logger.debug("model provider warm-up skipped for %s: %s", model_str, exc)


def _build_model(
    model_str: str,
    model_kwargs: dict[str, Any],
    reasoning_effort: str | None = None,
    max_output_tokens: int | None = None,
) -> BaseChatModel:
    from olano.models.reasoning import resolve_reasoning_effort

    kwargs = dict(model_kwargs or {})
    # Effort is resolved per target model (explicit setting, else platform
    # default "low") and only injected when the model supports it — a stale
    # setting never follows the agent onto an incompatible provider.
    effort = resolve_reasoning_effort(model_str, reasoning_effort)
    if effort is not None:
        kwargs.setdefault("reasoning_effort", effort)
    # `message_policy.max_output_tokens`, passed by name so `build_model` can
    # map it to the provider's own spelling. Deliberately NOT forwarded to
    # subagent models (the two call sites below pass nothing): a subagent's
    # output is working material the parent reads, not the reply the cap is
    # about, and truncating it mid-result corrupts the delegation.
    if max_output_tokens:
        from olano.models.output_limit import warn_if_thin

        warn_if_thin(model_str, max_output_tokens, effort or reasoning_effort)
    return build_model(model_str, max_output_tokens=max_output_tokens, **kwargs)


def _resolve_root_dir(raw: str, workspace_root: Path) -> Path:
    """Resolve 'workspace' sentinel to workspace_root; otherwise treat as a path."""
    return workspace_root if raw == "workspace" else Path(raw)


def _store_namespace_factory(namespace: list[str] | tuple[str, ...] | str | None) -> Any | None:
    """Return the callable namespace shape expected by deepagents StoreBackend."""
    if namespace is None:
        return None
    parts = (namespace,) if isinstance(namespace, str) else tuple(namespace)
    if not parts:
        return None
    return lambda _runtime, ns=parts: ns


def _build_remote_sandbox(cfg: BackendConfig) -> Any:
    """Lazy-import and instantiate a deepagents-native remote sandbox backend."""
    _TYPE_MAP = {
        "daytona": ("deepagents.sandboxes.daytona", "DaytonaSandbox"),
        "modal": ("deepagents.sandboxes.modal", "ModalSandbox"),
        "runloop": ("deepagents.sandboxes.runloop", "RunloopSandbox"),
        "agentcore": ("deepagents.sandboxes.agentcore", "AgentCoreSandbox"),
        "langsmith": ("deepagents.sandboxes.langsmith", "LangSmithSandbox"),
    }
    backend_type = cfg.type  # type: ignore[union-attr]
    mod_path, cls_name = _TYPE_MAP[backend_type]
    try:
        import importlib

        mod = importlib.import_module(mod_path)
    except ImportError as exc:
        pkg = mod_path.split(".")[0]
        raise RuntimeError(
            f"Backend type '{backend_type}' requires the '{pkg}' package. "
            f"Install it with: uv add {pkg}"
        ) from exc
    cls = getattr(mod, cls_name)
    kwargs = cfg.model_dump(exclude={"type"}, exclude_none=True)
    return cls(**kwargs)


def _resolve_exec_posture() -> tuple[bool, bool, bool]:
    """``(shell_allowed, managed_exec, sandbox_ok)`` for this box. Never raises.

    One definition of the three-predicate exec gate, asked by BOTH the
    ``local_shell`` coercion path and an explicitly-configured
    ``lionclaw_docker`` backend — they used to disagree, and the disagreement
    was the whole bug: an explicit ``backend: {type: "lionclaw_docker"}`` got no
    gate at all *and* no container, so it read as configured and behaved as
    dead.

    The lazy module import + attribute lookup at call time is load-bearing:
    tests monkeypatch these three names on the ``enforcement`` module object.
    """
    try:
        from olano.deployment import enforcement as dep_enforcement

        return (
            dep_enforcement.feature_enabled("shell"),
            dep_enforcement.require_sandboxed_exec(),
            dep_enforcement.sandbox_exec_enabled(),
        )
    except Exception:
        # The enforcement resolver itself errored. Do NOT assume open — that
        # would hand a raw host shell (read /app + vault) to a managed box on
        # any transient error. Detect managed-ness independently from the
        # installation manifest and fail CLOSED when it exists (or can't be
        # ruled out): managed => no host shell AND no unvalidated sandbox.
        # Only a box with no manifest at all is the operator's own (open).
        logger.exception("enforcement resolution failed; falling back to manifest check")
        return True, _managed_install_fallback(), False


def sandbox_owner(config: AgentConfig) -> str:
    """The agent whose sandbox container a config's shell runs in.

    A top-level agent owns its own. A cognition worker or ``delegate_task``
    child runs under a fresh unique ``name`` (``<parent>__cog__<label>__<hex>``,
    see :func:`olano.cognition.worker._build_worker_config`) for checkpoints
    and attribution, and resolves credentials as its parent through
    ``credential_scope``. Its sandbox is the parent's for the same reason: it
    works the parent's workspace with the parent's tool layer, egress lists and
    installed packages. Keying the container on the child's own name minted a
    new persistent container per background run that nothing ever stopped,
    which is how one managed box reached 960 running sandboxes on twelve agents
    (the update agent's census: ``top=br-…:959 SATURATED … sbx=960``).
    """
    return config.credential_scope or config.name


def _owner_kwarg(agent_name: str | None, owner: str | None) -> dict[str, str]:
    """``{"sandbox_owner": owner}`` only when it differs from the agent's own name.

    Passed through ``**`` so the common case (an agent that owns its sandbox)
    reaches ``_try_build_sandbox_backend`` with exactly the arguments it always
    did.
    """
    if agent_name and owner and owner != agent_name:
        return {"sandbox_owner": owner}
    return {}


def _build_deepagents_backend(
    backend_cfg: BackendConfig,
    workspace_root: Path,
    *,
    agent_name: str | None = None,
    shared_mounts: list[tuple[str, str, str]] | None = None,
    sandbox_overrides: SandboxResourceOverride | None = None,
    sandbox_owner: str | None = None,
) -> Any:
    """Dispatch a BackendConfig to the corresponding deepagents backend instance.

    ``sandbox_overrides`` is the agent's own ``sandbox:`` block. It only
    matters for the two paths that end in a Docker sandbox, where it is the
    first layer of the resource-limit resolution.

    ``sandbox_owner`` names the agent whose container the two Docker paths
    attach to when it is not ``agent_name`` itself (a cognition worker running
    for its parent, see :func:`sandbox_owner`). ``None`` means the agent owns
    its own.
    """
    if isinstance(backend_cfg, StateBackendConfig):
        from deepagents.backends import StateBackend

        return StateBackend()

    if isinstance(backend_cfg, FilesystemBackendConfig):
        from deepagents.backends import FilesystemBackend

        return FilesystemBackend(
            root_dir=_resolve_root_dir(backend_cfg.root_dir, workspace_root),
            virtual_mode=backend_cfg.virtual_mode,
        )

    if isinstance(backend_cfg, LocalShellBackendConfig):
        from deepagents.backends import FilesystemBackend

        resolved_root = _resolve_root_dir(backend_cfg.root_dir, workspace_root)

        shell_allowed, managed_exec, sandbox_ok = _resolve_exec_posture()

        # Deployment entitlements: with the `shell` feature disabled the agent
        # is downgraded to a plain filesystem backend (belt-and-braces — the
        # safe_shell tool also refuses per invocation, so a live settings
        # change bites already-built agents too).
        if not shell_allowed:
            logger.warning(
                "deployment settings disable the shell feature — using a "
                "filesystem backend (no shell) for this agent"
            )
            _record_sandbox_status(
                agent_name,
                SANDBOX_SHELL_DISABLED,
                "the shell feature is disabled for this deployment",
            )
            return FilesystemBackend(root_dir=resolved_root, virtual_mode=backend_cfg.virtual_mode)

        # SECURITY: a managed box holds the product source + secrets in the
        # runtime container, and the customer reaches agents only via chat. A
        # raw host shell there is a de facto root shell (it can copy /app and
        # read the vault), so under a managed posture agents never run raw shell
        # in the runtime container. They get an isolated sandbox that contains
        # neither source nor secrets when the operator has enabled+validated it,
        # otherwise NO shell (fail closed — never a host-shell fallback).
        if managed_exec:
            if sandbox_ok:
                sandbox = _try_build_sandbox_backend(
                    backend_cfg,
                    workspace_root=workspace_root,
                    agent_name=agent_name,
                    shared_mounts=shared_mounts,
                    sandbox_overrides=sandbox_overrides,
                    **_owner_kwarg(agent_name, sandbox_owner),
                )
                if sandbox is not None:
                    return sandbox
                logger.error(
                    "managed posture + sandbox_exec enabled, but the docker "
                    "sandbox could not start — FAILING CLOSED to a filesystem "
                    "backend (no shell) rather than a host shell"
                )
            else:
                logger.warning(
                    "managed posture: agents do not get a host shell; the "
                    "isolated sandbox is not enabled — filesystem backend (no shell)"
                )
                _record_sandbox_status(
                    agent_name,
                    SANDBOX_NOT_ENTITLED,
                    "sandbox execution is not enabled for this deployment (features.sandbox_exec)",
                )
            return FilesystemBackend(root_dir=resolved_root, virtual_mode=backend_cfg.virtual_mode)

        from deepagents.backends import LocalShellBackend

        from olano.backends.interpreter_shims import with_interpreter_shims
        from olano.security.shell_guard import sanitized_shell_env

        # deepagents defaults `env` to {} — the command ran with no PATH, no
        # HOME, nothing (it only worked because dash falls back to a compiled
        # default path). Hand it the same scrubbed environment the security
        # middleware's host lane (`shell_guard.run_local`) already gives the
        # very same `execute` tool, plus the interpreter shims so a bare
        # `python` resolves on a python3-only host.
        return LocalShellBackend(
            root_dir=resolved_root,
            virtual_mode=backend_cfg.virtual_mode,
            env=with_interpreter_shims(sanitized_shell_env()),
        )

    if isinstance(backend_cfg, StoreBackendConfig):
        from deepagents.backends import StoreBackend

        # deepagents 0.7 types StoreBackend.namespace as a required Callable, but
        # None is valid at runtime (unnamespaced store / composite route).
        return StoreBackend(
            namespace=_store_namespace_factory(backend_cfg.namespace)  # type: ignore[arg-type]
        )

    if isinstance(backend_cfg, CompositeBackendConfig):
        from deepagents.backends import CompositeBackend

        # Thread agent_name + shared_mounts so a shell route inside a composite
        # is coerced to a real sandbox under managed posture (and registered for
        # tool routing), not silently dropped.
        default = _build_deepagents_backend(
            backend_cfg.default,
            workspace_root,
            agent_name=agent_name,
            shared_mounts=shared_mounts,
            sandbox_overrides=sandbox_overrides,
            sandbox_owner=sandbox_owner,
        )
        routes = {
            path: _build_deepagents_backend(
                sub,
                workspace_root,
                agent_name=agent_name,
                shared_mounts=shared_mounts,
                sandbox_overrides=sandbox_overrides,
                sandbox_owner=sandbox_owner,
            )
            for path, sub in backend_cfg.routes.items()
        }
        return CompositeBackend(default=default, routes=routes)

    if isinstance(backend_cfg, OlanoDockerBackendConfig):
        from deepagents.backends import FilesystemBackend

        # This branch used to `return OlanoDockerBackend(backend_cfg)` and stop
        # there — no start(), no chown, no registry entry. The agent booted
        # clean and then answered every file/shell call with exit 127
        # ("start() has not been called"), while get_sandbox_backend() returned
        # None so the terminal and chat `!` lanes reported no sandbox at all.
        # Two surfaces, two different lies, and nothing in diagnose_agent.
        shell_allowed, managed_exec, sandbox_ok = _resolve_exec_posture()
        who = agent_name or "<unnamed>"

        # Same entitlement floor as the local_shell branch above. Naming the
        # backend explicitly in agent.json5 must not be a way around the
        # operator's sandbox_exec opt-in: on a managed box the customer edits
        # that file from the dashboard.
        if not shell_allowed:
            logger.warning(
                "deployment settings disable the shell feature — the configured "
                "docker sandbox for %r is not started; filesystem backend (no shell)",
                who,
            )
            _record_sandbox_status(
                agent_name,
                SANDBOX_SHELL_DISABLED,
                "the shell feature is disabled for this deployment",
            )
            return FilesystemBackend(root_dir=workspace_root, virtual_mode=False)
        if managed_exec and not sandbox_ok:
            logger.warning(
                "managed posture: sandbox execution is not enabled for this "
                "deployment — the configured docker sandbox for %r is not "
                "started; filesystem backend (no shell)",
                who,
            )
            _record_sandbox_status(
                agent_name,
                SANDBOX_NOT_ENTITLED,
                "sandbox execution is not enabled for this deployment (features.sandbox_exec)",
            )
            return FilesystemBackend(root_dir=workspace_root, virtual_mode=False)

        # Resource ceilings, resolved like every other sandbox's: the backend
        # block's own fields are this agent's opinion, and its `sandbox:` block
        # fills the gaps, so declaring the backend explicitly does not opt an
        # agent out of the deployment's sizing back to the historical 512m/0.5.
        # Applied after hardening because that step decides privileges, not size.
        limits = _resolve_sandbox_limits_for(
            _first_set(backend_cfg.memory, getattr(sandbox_overrides, "memory", None)),
            _first_set(backend_cfg.cpus, getattr(sandbox_overrides, "cpus", None)),
            agent_name=agent_name,
        )
        hardened = _harden_sandbox_cfg(backend_cfg, managed=managed_exec, agent_name=agent_name)

        # OlanoDockerBackendConfig has no root_dir: the agent's workspace root
        # IS the mount root.
        sandbox = _start_and_register_sandbox(
            hardened.model_copy(update={"memory": limits.memory, "cpus": limits.cpus}),
            workspace_dir=workspace_root,
            agent_name=agent_name,
            shared_mounts=shared_mounts,
            **_owner_kwarg(agent_name, sandbox_owner),
        )
        if sandbox is not None:
            return sandbox
        logger.error(
            "agent %r declares backend.type=lionclaw_docker but the sandbox "
            "container could not start — FAILING CLOSED to a filesystem backend "
            "(no shell)",
            who,
        )
        return FilesystemBackend(root_dir=workspace_root, virtual_mode=False)

    # Remote sandbox types
    if isinstance(
        backend_cfg,
        DaytonaBackendConfig
        | ModalBackendConfig
        | RunloopBackendConfig
        | AgentCoreBackendConfig
        | LangSmithBackendConfig,
    ):
        return _build_remote_sandbox(backend_cfg)

    raise ValueError(f"Unknown backend config type: {type(backend_cfg)}")


def _first_set(*values: Any) -> Any:
    """The first value that isn't ``None`` — 0 and "" are never valid limits."""
    return next((v for v in values if v is not None), None)


def _resolve_sandbox_limits_for(
    memory: str | None,
    cpus: float | None,
    *,
    agent_name: str | None,
) -> SandboxLimits:
    """Resolve this sandbox's ceilings and record how each was decided.

    The log line is the only place a deployed box explains why an agent's
    sandbox is the size it is, so it names both values and their sources.
    """
    from olano.core.host_resources import describe_bytes, resolve_sandbox_limits

    limits = resolve_sandbox_limits(memory, cpus)
    logger.info(
        "sandbox limits for %s: memory=%s (%s), cpus=%s (%s) [host: %s cpus, %s, via %s]",
        agent_name or "agent",
        limits.memory,
        limits.memory_source,
        limits.cpus,
        limits.cpus_source,
        limits.host.ncpu if limits.host.ncpu is not None else "unknown",
        describe_bytes(limits.host.mem_bytes),
        limits.host.source,
    )
    return limits


def _try_build_sandbox_backend(
    local_cfg: Any,
    *,
    workspace_root: Path,
    agent_name: str | None,
    shared_mounts: list[tuple[str, str, str]] | None,
    sandbox_overrides: SandboxResourceOverride | None = None,
    role: str = "backend",
    sandbox_owner: str | None = None,
) -> Any | None:
    """Build + start a hardened OlanoDockerBackend, or None on any failure.

    Returns None (never raises) so the caller can fail closed to a no-shell
    filesystem backend. The sandbox image contains neither the product source
    nor secrets; commands run via `docker exec` in a bridge-networked, resource-
    capped, non-root container with the workspace bind-mounted. Package installs
    persist via a per-agent snapshot on stop.

    ``local_cfg`` is any backend config carrying a ``root_dir`` (the
    ``local_shell`` block this coerces, or the ``filesystem`` block a sidecar
    is started beside — see :func:`_start_sidecar_sandbox`). ``role`` is
    recorded in the status registry so readers can tell a sandbox that IS the
    agent's backend from one that merely accompanies it. ``sandbox_owner`` is
    the agent whose container this is when ``agent_name`` is a worker acting
    for it (see :func:`_start_and_register_sandbox`).

    Resource ceilings are resolved here rather than in the backend, so the
    backend keeps consuming concrete values and a directly-constructed one
    (tests, an explicit backend block) behaves exactly as before.
    """
    try:
        limits = _resolve_sandbox_limits_for(
            getattr(sandbox_overrides, "memory", None),
            getattr(sandbox_overrides, "cpus", None),
            agent_name=agent_name,
        )
        sandbox_cfg = OlanoDockerBackendConfig(
            persistent=True,
            # bridge so pip/npm + the customer's own DBs are reachable; the
            # sandbox is blocked from the host's internal services + cloud
            # metadata by the backend's run flags (see OlanoDockerBackend).
            network="bridge",
            snapshot_on_stop=True,
            memory=limits.memory,
            cpus=limits.cpus,
        )
        ws = _resolve_root_dir(getattr(local_cfg, "root_dir", "workspace"), workspace_root)
    except Exception:
        logger.exception("sandbox config synthesis failed — caller will fail closed")
        return None
    return _start_and_register_sandbox(
        sandbox_cfg,
        workspace_dir=ws,
        agent_name=agent_name,
        shared_mounts=shared_mounts,
        role=role,
        **_owner_kwarg(agent_name, sandbox_owner),
    )


def _start_and_register_sandbox(
    sandbox_cfg: OlanoDockerBackendConfig,
    *,
    workspace_dir: Path,
    agent_name: str | None,
    shared_mounts: list[tuple[str, str, str]] | None,
    role: str = "backend",
    sandbox_owner: str | None = None,
) -> Any | None:
    """Build, start and register an OlanoDockerBackend, or None on any failure.

    The five steps every sandbox needs, in ONE place so the two callers (the
    managed ``local_shell`` coercion and an explicitly-configured
    ``lionclaw_docker`` backend) cannot drift again — they did, and the explicit
    branch was left doing none of them: it returned an unstarted object whose
    every file/shell call answered exit 127 while the agent booted clean.

    Never raises: the caller fails closed to a no-shell filesystem backend.
    Registration happens only AFTER ``start()`` returns, so a failed start can
    never leave a half-live entry in the registry.

    ``sandbox_owner`` (a cognition worker's parent, see :func:`sandbox_owner`)
    changes WHOSE container this is, never who is registered: the entry is
    filed under ``agent_name`` so the worker's own turn (``current_agent`` is
    the child name) still reaches it through :func:`get_sandbox_backend`, and
    :func:`release_agent_sandbox` drops that entry when the run ends. The
    owner's live backend is handed over as-is when it has one, so a worker
    costs no container at all; without one the container is started under the
    owner's name, which is the name the owner's own next build looks up.
    """
    try:
        from olano.backends.lionclaw_docker import OlanoDockerBackend

        if sandbox_owner and sandbox_owner != agent_name:
            shared = _SANDBOX_BACKENDS.get(sandbox_owner)
            if shared is not None and agent_name:
                _SANDBOX_BACKENDS[agent_name] = shared
                network = _sandbox_network_report(shared)
                _record_sandbox_status(
                    agent_name,
                    SANDBOX_STARTED,
                    f"sharing agent {sandbox_owner!r}'s sandbox container",
                    container=shared.container_ref(),
                    role=role,
                    network=network,
                )
                logger.info(
                    "worker %r runs in agent %r's sandbox container %r",
                    agent_name,
                    sandbox_owner,
                    shared.container_ref(),
                )
                return shared

        backend = OlanoDockerBackend(sandbox_cfg)
        # The sandbox runs as uid 1000; the runtime creates the workspace AND
        # seeds it (AGENTS.md, MEMORY.md, memories/, system/…) as root, and a
        # bind mount preserves host ownership — so re-own the whole tree (best
        # effort) or the non-root agent can create new files but cannot edit,
        # delete, or write inside anything the runtime put there.
        _ensure_sandbox_shared_dir(workspace_dir, recursive=True)
        backend.start(
            agent_name=sandbox_owner or agent_name or "agent",
            workspace_root=workspace_dir,
            shared_mounts=shared_mounts or [],
        )
        _seed_sandbox_tool_sets(backend)
        if agent_name:
            _warn_on_sandbox_reregistration(agent_name, backend)
            _SANDBOX_BACKENDS[agent_name] = backend
        else:
            # Nothing to key the registry on, so the terminal, the chat `!` lane
            # and sandboxed_run() cannot reach this container even though it is
            # up. Callers always pass a name today; this is the tripwire.
            logger.warning(
                "sandbox started without an agent name — container %r is not in "
                "the per-agent registry and no surface can reach it",
                backend.container_ref(),
            )
        network = _sandbox_network_report(backend)
        _record_sandbox_status(
            agent_name,
            SANDBOX_STARTED,
            _sandbox_started_reason(network),
            container=backend.container_ref(),
            role=role,
            network=network,
        )
        return backend
    except Exception as exc:
        logger.exception("sandbox backend build/start failed — caller will fail closed")
        _record_sandbox_status(
            agent_name,
            SANDBOX_START_FAILED,
            f"sandbox container could not start: {exc}",
            role=role,
        )
        return None


def _warn_on_sandbox_reregistration(agent_name: str, backend: Any) -> None:
    """Log when a second, DIFFERENT container claims one agent's registry slot.

    ``_persistent_container_name`` keys on (agent, workspace) only, so a
    composite backend with two ``lionclaw_docker`` routes computes one name for
    both. Matching signatures just reattach (harmless); differing ones make the
    second start ``docker rm -f`` the first. Renaming the container is not an
    option (it would orphan containers already running on deployed hosts), so
    the collision is surfaced rather than silently resolved. Last write wins —
    that is also what an ordinary agent rebuild needs.
    """
    try:
        existing = _SANDBOX_BACKENDS.get(agent_name)
        if existing is None or existing is backend:
            return
        before, after = existing.container_ref(), backend.container_ref()
        if before and after and before != after:
            logger.warning(
                "agent %r registered a second sandbox container (%r replaces %r) — "
                "two docker backends in one config share a container name",
                agent_name,
                after,
                before,
            )
    except Exception:  # a warning must never break a build
        pass


def _harden_sandbox_cfg(
    cfg: OlanoDockerBackendConfig, *, managed: bool, agent_name: str | None
) -> OlanoDockerBackendConfig:
    """Coerce a customer-authored sandbox config to the managed-box floor.

    These fields were inert while the explicit branch never called ``start()``.
    Starting it for real makes them live, so the ones that carry a privilege
    are pinned here rather than shipped as a new hole.

    Returns ``cfg`` untouched on an open/unmanaged box — the operator's own
    machine, their container, their rules. Never mutates in place: the caller's
    ``config.backend`` is shared with the prompt assembler, the dashboard's
    agent-edit round-trip and the revisions snapshot.
    """
    _warn_on_unimplemented_sandbox_fields(cfg, agent_name)
    if not managed:
        return cfg

    who = agent_name or "<unnamed>"
    updates: dict[str, Any] = {}

    if cfg.network == "host":
        # The host network namespace reaches every 127.0.0.1 service on the
        # host (runtime API, Postgres, …) and turns the backend's
        # `--add-host=metadata.google.internal:127.0.0.1` from a block into a
        # pointer at the host itself.
        logger.warning(
            "managed posture: refusing network=host for agent %r (the host "
            "namespace exposes the host's internal services); using bridge",
            who,
        )
        updates["network"] = "bridge"

    default_image = _default_sandbox_image()
    if cfg.image != default_image:
        # `_base_run_args` passes no `--user`: non-root comes from the image's
        # own USER directive. An image with `USER root` writes root-owned files
        # into the bind-mounted workspace and can read the 0600 chat-`!` logs.
        # `_default_sandbox_image()` honours LIONCLAW_SANDBOX_IMAGE, so the
        # OPERATOR keeps this knob and the customer loses it.
        logger.warning(
            "managed posture: refusing custom sandbox image %r for agent %r "
            "(the run flags set no --user, so non-root comes from the image); using %s",
            cfg.image,
            who,
            default_image,
        )
        updates["image"] = default_image

    if not cfg.persistent:
        # `_start_ephemeral` runs `--rm -d` around a `sleep 3600` loop, and
        # nothing in production calls `stop()` — so `--rm` never fires and every
        # build (each boot, each /model switch, each reload) would leak an
        # anonymous container that no restart can find again.
        logger.warning(
            "managed posture: forcing persistent=True for agent %r — nothing "
            "stops an ephemeral sandbox, so each build would leak a container",
            who,
        )
        updates["persistent"] = True

    return cfg.model_copy(update=updates) if updates else cfg


def _warn_on_unimplemented_sandbox_fields(
    cfg: OlanoDockerBackendConfig, agent_name: str | None
) -> None:
    """Say so when a config sets a sandbox field that nothing reads.

    ``mounts`` is accepted by the schema and was documented as a working
    option, but the real mount plan comes from ``_resolve_sandbox_shared_mounts``.
    ``egress_allow`` / ``egress_deny`` on this block were in the same state for
    a long time; they are now **folded into the agent's top-level lists** by
    ``security/egress.py`` — so they govern the SSRF-gated tools exactly like
    the top-level fields do — but they still do NOT govern the sandbox shell,
    and a config that sets them here is told to move them so the one place
    the dashboard edits is the one place they live. Silently ignoring an
    ``egress_deny`` hands out a security guarantee that does not exist.
    """
    empty = OlanoDockerBackendConfig()
    who = agent_name or "<unnamed>"
    if cfg.mounts != empty.mounts:
        logger.warning(
            "agent %r sets sandbox backend field 'mounts', which is NOT implemented "
            "and has no effect: mounts come from the workspace + shared-path resolver",
            who,
        )
    legacy_egress = [n for n in ("egress_allow", "egress_deny") if getattr(cfg, n)]
    if legacy_egress:
        logger.warning(
            "agent %r sets %s on its sandbox backend block — these are folded into "
            "the agent's top-level egress_allow/egress_deny (governing web_fetch, "
            "http_request, media fetch and A2A) but do NOT restrict the sandbox "
            "shell's own network; move them to the top level of agent.json5",
            who,
            ", ".join(legacy_egress),
        )


# Registry of live per-agent sandbox backends, so IN-PROCESS tools that shell out
# (gcloud, ffmpeg, …) can route their command through the same isolated sandbox
# as the agent's `execute` rather than running on the runtime host. Populated at
# build time; read via get_sandbox_backend(). Mirrors _MIDDLEWARE_HOOKS.
_SANDBOX_BACKENDS: dict[str, Any] = {}


def get_sandbox_backend(agent_name: str | None) -> Any | None:
    """Return the live OlanoDockerBackend for an agent, or None (open posture / no sandbox)."""
    if not agent_name:
        return None
    return _SANDBOX_BACKENDS.get(agent_name)


def release_agent_sandbox(agent_name: str | None) -> None:
    """Forget a finished worker's registry and status entries.

    Never stops a container: a worker's backend is its parent's (see
    :func:`sandbox_owner`), and the parent keeps using it. Without this, every
    ``<parent>__cog__…`` name a background run minted stayed in both maps for
    the life of the process. Safe to call for a name that was never registered.
    """
    if not agent_name:
        return
    _SANDBOX_BACKENDS.pop(agent_name, None)
    _SANDBOX_STATUS.pop(agent_name, None)


# Why each agent's sandbox is (or isn't) live, written at every decision point in
# `_build_deepagents_backend`. Read by `admin/diagnostics.py`, so "my agent is
# online and healthy but every file tool fails" is answerable without SSH: the
# only previous record of a refused or failed sandbox was a log line.
_SANDBOX_STATUS: dict[str, dict[str, Any]] = {}

#: `state` values, most to least healthy.
SANDBOX_STARTED = "started"
SANDBOX_START_FAILED = "start_failed"
SANDBOX_NOT_ENTITLED = "not_entitled"
SANDBOX_SHELL_DISABLED = "shell_disabled"

#: `role` values. `backend`: the container IS the agent's deepagents backend —
#: its file tools and `execute` run inside it. `sidecar`: the agent's backend is
#: a plain filesystem (or virtual) one and the container merely accompanies it,
#: so the dashboard Terminal, the chat `!` lane and in-process tools that shell
#: out (`sandboxed_run`) have an isolated place to run on a managed box. A
#: sidecar never hands the model a shell: `execute` is granted from the backend
#: type, and the security middleware routes shell tools from the backend the
#: builder passed it, never from the registry.
SANDBOX_ROLE_BACKEND = "backend"
SANDBOX_ROLE_SIDECAR = "sidecar"


def _record_sandbox_status(
    agent_name: str | None,
    state: str,
    reason: str,
    *,
    container: str | None = None,
    role: str = SANDBOX_ROLE_BACKEND,
    network: dict[str, Any] | None = None,
) -> None:
    if agent_name:
        _SANDBOX_STATUS[agent_name] = {
            "state": state,
            "reason": reason,
            "container": container,
            "role": role,
            "network": network or {},
        }


def _sandbox_network_report(backend: Any) -> dict[str, Any]:
    """The backend's last reachability verdict — never let it break a boot."""
    try:
        report = backend.network_report()
        return dict(report) if isinstance(report, dict) else {}
    except Exception:
        return {}


def _sandbox_started_reason(network: dict[str, Any]) -> str:
    """ "Live" is not the whole truth when the container had to be rescued.

    A box nobody can SSH into reads this string in the Terminal tab, `doctor()`
    and the dashboard, so it is where a repaired — or an unrepairable — network
    has to be visible.
    """
    health = str(network.get("health") or "")
    name = str(network.get("network") or "")
    if health == "stranded":
        return (
            f"sandbox container is live but has no working network on {name!r} — "
            "package installs and stdio MCP servers will fail to resolve names"
        )
    if network.get("rescued"):
        return f"sandbox container is live (network repaired: moved to {name!r})"
    if network.get("isolated") is False and name not in ("", "none", "host"):
        return (
            f"sandbox container is live on {name!r}, which other containers can reach "
            "(sandbox isolation is off or unavailable on this box)"
        )
    return "sandbox container is live"


def sandbox_status(agent_name: str | None) -> dict[str, Any] | None:
    """Last recorded sandbox outcome for an agent, or None if it never wanted one."""
    if not agent_name:
        return None
    return _SANDBOX_STATUS.get(agent_name)


def sandbox_role(agent_name: str | None) -> str | None:
    """``backend`` / ``sidecar`` for an agent with a recorded sandbox, else None."""
    status = sandbox_status(agent_name)
    return None if status is None else str(status.get("role") or SANDBOX_ROLE_BACKEND)


def sandbox_is_backend(agent_name: str | None) -> bool:
    """True when the agent's LIVE sandbox is its deepagents backend.

    The one predicate the prompt assembler asks before telling the model its
    shell and file tools run in a container: a sidecar is live in the registry
    too, but that agent's tools run on the runtime filesystem with no shell.
    """
    if get_sandbox_backend(agent_name) is None:
        return False
    return sandbox_role(agent_name) != SANDBOX_ROLE_SIDECAR


# Backend types a sidecar may accompany: local, non-sandbox backends whose
# workspace lives on this host. Remote sandboxes (daytona/modal/…) run
# elsewhere; `lionclaw_docker` and a `local_shell` block coerced under managed
# posture already went through the sandbox path and recorded their own outcome.
_SIDECAR_BACKEND_TYPES = frozenset({"filesystem", "state", "store", "composite"})


def _start_sidecar_sandbox(config: AgentConfig, workspace_root: Path) -> Any | None:
    """Give a non-execute agent its persistent sandbox container on a managed box.

    The container used to exist only for agents whose backend is shell-capable
    (``local_shell`` coerced under managed posture, or explicit
    ``lionclaw_docker``). A "Read & Write" agent got no container at all, so on
    a managed deployment the dashboard Terminal closed 4503 ("sandbox isn't
    running — start the agent, then reconnect" — which starting the agent could
    never fix), the chat ``!`` lane refused, ``run_agent_command`` raised, and
    in-process tools that shell out (``sandboxed_run``: ffmpeg, gcloud, …) fell
    back to the RUNTIME HOST — the very thing the managed posture exists to
    prevent.

    Now every agent on a sandbox-entitled managed box has a container. The
    agent's deepagents backend is untouched — no ``execute`` tool, filesystem
    permission rules intact — so this is a sidecar: reachable by every surface
    that reads ``get_sandbox_backend()``, invisible to the model's tool surface
    (``sandbox_is_backend()`` tells the prompt not to claim otherwise). Same
    container name and signature as the execute path, so flipping Access to
    "Read, Write & Execute" later reattaches this exact container.

    Open/unmanaged posture: no-op (the Terminal is a host shell there). Never
    raises; a failed start is recorded like any other so diagnostics can name it.
    """
    name = config.name
    if not name or get_sandbox_backend(name) is not None or sandbox_status(name) is not None:
        return None  # the backend path already decided (started, refused or failed)
    backend_cfg = config.backend
    if getattr(backend_cfg, "type", None) not in _SIDECAR_BACKEND_TYPES:
        return None
    shell_allowed, managed_exec, sandbox_ok = _resolve_exec_posture()
    if not (shell_allowed and managed_exec and sandbox_ok):
        return None
    logger.info(
        "agent %r has a %r backend (no shell) — starting its sandbox as a sidecar "
        "for the Terminal, chat `!` and sandboxed tool subprocesses",
        name,
        getattr(backend_cfg, "type", None),
    )
    return _try_build_sandbox_backend(
        backend_cfg,
        workspace_root=workspace_root,
        agent_name=name,
        shared_mounts=_resolve_sandbox_shared_mounts(config, workspace_root),
        sandbox_overrides=config.sandbox,
        role=SANDBOX_ROLE_SIDECAR,
        **_owner_kwarg(name, sandbox_owner(config)),
    )


def _handoff_fleet_access(config: AgentConfig) -> None:
    """Hand the fleet trees to the sandbox user once the agent's container is up.

    Only when a container exists for this agent (its own, or its parent's for a
    worker): that is where the uid gap is. A host shell runs as the runtime's
    own user, and re-owning the home to uid 1000 there would hand it to
    whatever host account has that uid. The middleware repeats this (throttled)
    before each file and shell tool, since the runtime keeps writing as root.
    """
    try:
        from olano.security.fleet_access import fleet_roots, handoff_fleet_trees

        roots = fleet_roots(config)
        if roots and get_sandbox_backend(sandbox_owner(config)) is not None:
            handoff_fleet_trees(roots, force=True)
    except Exception:
        logger.debug("fleet access hand-off failed", exc_info=True)


def _seed_sandbox_tool_sets(backend: Any) -> None:
    """Kick off the tool-layer seed (once per set) inside the sandbox, detached.

    Reads home config ``sandbox_tool_sets`` (**empty by default** — no set is
    seeded unless the operator asks for one) and runs the image-baked
    ``olano-seed-tools <set>`` script backgrounded so it never blocks the agent
    build. The script is sentinel-guarded + best-effort and installs into the
    SHARED tool layer, so it effectively runs once per deployment and every
    agent inherits the result. Never raises.
    """
    try:
        from olano.config.home import load_home_config

        sets = load_home_config().sandbox_tool_sets or []
    except Exception:
        return
    for raw in sets:
        name = re.sub(r"[^a-z0-9_-]+", "", str(raw).strip().lower())
        if not name:
            continue
        try:
            backend.execute(
                "command -v olano-seed-tools >/dev/null 2>&1 && "
                f"nohup olano-seed-tools {name} "
                f">/opt/agent-tools/.seed-{name}.log 2>&1 &",
                timeout=20,
            )
        except Exception:
            logger.warning("tool-layer seed for set %r could not start", name)


def _managed_install_fallback() -> bool:
    """Best-effort "is this a managed box?" when the enforcement resolver errors.

    Managed installs carry an installation manifest outside the home
    (``/etc/olano/installation.json``). If it exists — or we cannot tell — treat
    the box as managed so exec fails CLOSED (no host shell) rather than handing
    out a raw shell on a transient resolver error.
    """
    try:
        from olano.deployment.sources import manifest_path

        return manifest_path().exists()
    except Exception:
        return True  # can't tell ⇒ safest assumption is managed


# uid/gid of the `sandbox` user baked into the sandbox image
# (docker/olano-sandbox/Dockerfile). Host paths bind-mounted into a sandbox must
# belong to it or the non-root agent cannot write them.
SANDBOX_UID = 1000
SANDBOX_GID = 1000

# Ceiling on the recursive workspace chown below, so a workspace someone filled
# with a node_modules / dataset tree can't stall an agent build. Generous enough
# that a real workspace never reaches it; when it does we say so rather than
# silently leaving part of the tree unwritable.
_SANDBOX_CHOWN_MAX_ENTRIES = 200_000


def chown_for_sandbox(path: Path) -> bool:
    """Best-effort: hand a single host path to the sandbox user. Never raises.

    Returns True when the path now belongs to the sandbox uid. The runtime runs
    as root and the sandboxed agent as uid 1000, so anything the runtime writes
    into a bind-mounted path is unreadable/unwritable to the agent until this
    runs — most visibly the chat ``!`` run log, which is written 0600 precisely
    so nothing else can read it.

    Symlinks are chowned as links (never followed): workspace ``shared/*``
    entries are projections into ``<home>/shared``, and following them would
    re-own the shared source.
    """
    try:
        os.chown(path, SANDBOX_UID, SANDBOX_GID, follow_symlinks=False)
    except (PermissionError, OSError, NotImplementedError):
        return False
    return True


def _ensure_sandbox_shared_dir(path: Path, *, recursive: bool = False) -> None:
    """Create a host dir the sandbox (uid 1000) must be able to write.

    Best-effort chown to the sandbox uid/gid; falls back to world-writable when
    the runtime isn't root. Never raises — a mount that can't be prepared is
    simply skipped by the caller.

    ``recursive`` also re-owns the existing contents, and is meant for the
    **agent's own workspace**: the runtime seeds it (AGENTS.md, MEMORY.md,
    memories/, system/knowledge/…) as root before the sandbox starts, so
    chowning only the top directory leaves the agent able to create new files
    but unable to edit, delete, or write inside anything the runtime made —
    `edit_file MEMORY.md` and `write_file memories/<today>.md` both come back
    "permission denied". Leave it off for the shared tool layer (pip/npm trees,
    huge) and for operator-declared shared sources (not ours to re-own).
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("sandbox: could not create %s", path)
        return
    if not chown_for_sandbox(path):
        try:
            path.chmod(0o777)
        except OSError:
            pass
        return  # not root ⇒ the recursive pass below would fail on every entry
    if not recursive:
        return

    seen = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(path):  # followlinks=False by default
        for name in (*dirnames, *filenames):
            if seen >= _SANDBOX_CHOWN_MAX_ENTRIES:
                truncated = True
                break
            seen += 1
            entry = Path(dirpath) / name
            try:
                if entry.lstat().st_uid == SANDBOX_UID:
                    continue  # already the agent's — the steady-state case
            except OSError:
                continue
            chown_for_sandbox(entry)
        if truncated:
            break
    if truncated:
        logger.warning(
            "sandbox: stopped re-owning %s after %d entries — the rest stays "
            "root-owned and the agent will not be able to write it",
            path,
            _SANDBOX_CHOWN_MAX_ENTRIES,
        )


def _sandbox_shared_source_allowed(source: Path) -> bool:
    """Whether a customer-declared ``shared_paths`` source may be bind-mounted
    into the sandbox.

    The sandbox is the source-/secret-free execution boundary; ``shared_paths``
    must not be a hole through it. Default-DENY: a source is allowed only when it
    resolves **inside** the designated cross-agent share dir (``<home>/shared``)
    or an operator-declared extra bind root (home config, NOT a customer's
    agent.json5), and is **never** inside a secret/source location (the vault,
    ``/opt/olano``, ``/app``, or the home root itself — which would expose the
    vault, per-agent tokens, and every agent's config).
    """
    from olano.config.home import get_olano_paths

    paths = get_olano_paths()
    try:
        home = paths.root.resolve()
        src = source.resolve()
    except Exception:
        return False

    def _within(child: Path, parent: Path) -> bool:
        return child == parent or parent in child.parents

    # Deny these paths and everything under them (secrets / product source).
    denied_trees = [
        paths.vault_dir.resolve(),
        home / "vault.db",
        Path("/opt/olano"),
        Path("/app"),
        Path("/etc"),
    ]
    for d in denied_trees:
        try:
            if _within(src, d.resolve() if d.exists() else d):
                return False
        except Exception:
            return False
    # Deny the blanket home ROOT itself (mounting all of home would expose the
    # vault, per-agent tokens, every agent's config) — but its safe subdirs
    # (e.g. <home>/shared) are still allowed below.
    if src == home:
        return False

    allowed = [paths.shared_dir.resolve()]
    try:
        from olano.security.sharing import allowed_bind_roots

        for r in allowed_bind_roots():
            rr = r.resolve()
            if rr != home:  # exclude the blanket home root (denied above)
                allowed.append(rr)
    except Exception:
        pass
    return any(_within(src, a) for a in allowed)


def ensure_tool_layer_layout(tool_dir: Path) -> None:
    """Create the shared tool layer AND the install targets inside it, host-side.

    The layer is bind-mounted over the image's ``/opt/agent-tools``, and a bind
    mount **shadows** rather than merges — so the ``python/ npm/ cache/ bin/``
    the Dockerfile creates do not exist in the container; only what this host
    directory holds does. pip did not care (``PYTHONUSERBASE`` is a tree
    Python builds on demand); npm never creates ``npm_config_prefix`` and npm 9
    lstats ``<prefix>/lib`` on every ``npx``, so every ``npx``, every
    ``npm install -g`` and every npx-launched stdio MCP server died ENOENT
    while ``pip install`` worked in the same shell.

    The layout is derived from the env/PATH that name the targets
    (``interpreter_shims.tool_layer_layout``), ancestors first, and every
    level goes through ``_ensure_sandbox_shared_dir`` — mkdir + hand-off to
    the sandbox uid, world-writable fallback on a non-root runtime — because
    a uid-1000 leaf under a root-owned parent is a dead end for the next
    sibling an installer creates. Idempotent and cheap (a stat per level), so
    both preparers call it on every build/spawn: this resolver for the agent
    sandboxes, ``mcp_setup._prepare_stdio_sandbox_paths`` for the ephemeral
    stdio-MCP and job containers, which mount the same directory without
    coming through here. Like the shims it needs **no** ``_container_signature``
    bump — same mount, and a new host subdir is visible in a running container
    immediately.
    """
    from olano.backends.interpreter_shims import tool_layer_layout

    _ensure_sandbox_shared_dir(tool_dir)
    for rel in tool_layer_layout():
        _ensure_sandbox_shared_dir(tool_dir / rel)


def _resolve_sandbox_shared_mounts(
    config: AgentConfig, workspace_root: Path
) -> list[tuple[str, str, str]]:
    """(host, container, mode) mounts shared into a managed agent's sandbox.

    Three kinds of sharing, all keyed on the single per-deployment home so every
    agent sees the same thing:
      • the tool layer (<home>/sandbox/tools → /opt/agent-tools): one place pip/
        npm install into, shared across all agents (see OlanoDockerBackend).
        Its install-target subdirs are created HERE, on the host side, because
        the mount hides the image's own — and, like the shims, that reaches an
        already-running container with no ``_container_signature`` bump: same
        mount, and a bind mount shows a new host subdir immediately;
      • the home ``shared/`` dir + any configured shared_paths, surfaced under
        ``/shared/*`` — but ONLY sources that pass ``_sandbox_shared_source_allowed``
        (default-deny), so a customer can't name the vault / ``/app`` / ``/opt/olano``
        as a "shared path" and mount secrets back into the isolated sandbox.
        Each allowed source is ALSO mounted at its own host path: the workspace
        ``shared/<name>`` entries are symlinks to the host source
        (``sync_workspace_share_projections``), and a symlink only resolves in
        the container when its target path exists there too;
      • skill source roots outside the workspace (managed ``<home>/skills``,
        absolute ``config.skills`` dirs), mounted READ-ONLY at their host paths.
        deepagents' SkillsMiddleware scans these dirs *through the backend* — in
        the container — and its prompt advertises host-absolute SKILL.md
        locations, so without the mount a sandboxed agent has no skills at all.
        Read-only makes "writes to skill roots outside the workspace are
        blocked" a kernel fact, not just middleware policy. Workspace-resident
        skill dirs need nothing: the backend mounts the workspace path-
        identically already.
    """
    from olano.backends.interpreter_shims import ensure_interpreter_shims, ensure_skill_fonts
    from olano.backends.lionclaw_docker import _TOOL_LAYER_DIR
    from olano.config.home import get_olano_paths

    mounts: list[tuple[str, str, str]] = []
    ws_resolved = workspace_root.resolve()
    # A container path may be claimed once; /workspace + the path-identical
    # workspace mount are claimed by the backend itself (_base_run_args).
    taken: set[str] = {"/workspace", str(ws_resolved)}

    def _add(host: str, container: str, mode: str) -> None:
        if container in taken:
            return
        taken.add(container)
        mounts.append((host, container, mode))

    tool_dir = get_olano_paths().root / "sandbox" / "tools"
    ensure_tool_layer_layout(tool_dir)
    # `python`/`pip` shims + the Terminal rc, appended to every sandbox PATH
    # (interpreter_shims.py). Written here, on the host side of the mount, so
    # they reach containers that already exist and images that lack the alias.
    ensure_interpreter_shims(tool_dir)
    # The bundled OFL faces, same host-side write for the same reason: fontconfig
    # in the image scans one fixed container path, and LibreOffice is the only
    # thing on the box that resolves a face by name instead of by file.
    ensure_skill_fonts(tool_dir)
    _add(str(tool_dir), _TOOL_LAYER_DIR, "rw")

    try:
        from olano.security.sharing import resolve_shared_mounts

        for m in resolve_shared_mounts(config):
            source = Path(m.source)
            if not _sandbox_shared_source_allowed(source):
                logger.warning(
                    "sandbox: refusing shared mount %s → %s (outside safe roots / "
                    "points at source or secrets)",
                    source,
                    m.target,
                )
                continue
            if not source.exists():
                _ensure_sandbox_shared_dir(source)
            src_resolved = source.resolve()
            # A writable share under the home shared tree is OURS to manage —
            # the home scaffold creates it as root, and a bind mount preserves
            # that, so without the hand-off the "rw" grant is a lie: every
            # agent gets "permission denied" creating a file in the common
            # folder. Operator-declared sources outside <home>/shared are left
            # alone (not ours to re-own).
            if m.mode == "rw":
                shared_root = get_olano_paths().shared_dir.resolve()
                if src_resolved == shared_root or shared_root in src_resolved.parents:
                    _ensure_sandbox_shared_dir(src_resolved, recursive=True)
            src = str(src_resolved)
            _add(src, m.target, m.mode)
            # Same source at its host path, so the workspace shared/<name>
            # symlink (which points at the host path) resolves in-container.
            _add(src, src, m.mode)
    except Exception:
        logger.exception("resolve_shared_mounts failed; sandbox gets tool layer only")

    # Fleet filesystem access (AgentFather): the whole `<home>/agents` tree and
    # the common skills read-write, the logs read-only, each at its host path.
    # Added BEFORE the skill roots so `<home>/skills` keeps this rw mount
    # instead of the read-only one below (a container path is claimed once).
    # The agent's own workspace sits inside `<home>/agents`; docker mounts a
    # parent before a child by destination depth, and both are the same host
    # directory, so the nesting changes nothing. The mount list is part of the
    # container signature, so turning the grant on or off recreates the
    # container on the next build.
    try:
        from olano.security.fleet_access import fleet_roots

        for fleet_root in fleet_roots(config):
            _add(str(fleet_root.path), str(fleet_root.path), fleet_root.mode)
    except Exception:
        logger.exception("fleet access mounts failed; sandbox keeps its own workspace only")

    try:
        from olano.config.workspace import Workspace
        from olano.skills.loader import skill_read_roots

        for root in skill_read_roots(config, Workspace(root=workspace_root)):
            resolved = root.resolve()
            if resolved == ws_resolved or ws_resolved in resolved.parents:
                continue  # inside the workspace — covered by the workspace mount
            if not resolved.is_dir():
                continue
            _add(str(resolved), str(resolved), "ro")
    except Exception:
        logger.exception("skill root mounts failed; sandbox agent may see no managed skills")

    return mounts


def _backend_uses_store(backend_cfg: Any) -> bool:
    """True when the backend config tree contains a StoreBackend.

    deepagents requires a ``store=`` on ``create_deep_agent`` whenever a
    ``StoreBackend`` is used. This walks the config so both a directly-selected
    store backend and a ``CompositeBackend`` route to one are covered.
    """
    if isinstance(backend_cfg, StoreBackendConfig):
        return True
    if isinstance(backend_cfg, CompositeBackendConfig):
        if _backend_uses_store(backend_cfg.default):
            return True
        return any(_backend_uses_store(sub) for sub in backend_cfg.routes.values())
    return False


def _build_permissions(
    config: AgentConfig, workspace_root: Path, skill_dirs: list[str] | None = None
) -> list[Any] | None:
    """Build deepagents permissions= for non-sandbox backends only.

    Returns None for sandbox backends (isolation is via mount shape / provider boundary).
    Returns None for root_dir="/" (no restriction).
    """
    from deepagents.middleware.permissions import FilesystemPermission

    backend_cfg = config.backend
    # deepagents _PermissionMiddleware only supports FilesystemBackend (read/write, no exec).
    # LocalShellBackend implements SandboxBackendProtocol and raises NotImplementedError.
    if not isinstance(backend_cfg, FilesystemBackendConfig):
        # Multi-user mode advertises a deny rule over `customers/**` that this
        # branch cannot install: deepagents refuses permission rules on any
        # execution-capable backend (`FilesystemMiddleware.__init__` raises
        # NotImplementedError unless every path is scoped to a CompositeBackend
        # route) because it cannot enforce them on `execute`. Silence here is
        # how an operator ends up believing a sandboxed support bot isolates
        # its users from each other — it does not; a container isolates the
        # agent from the HOST, while the workspace it mounts still holds every
        # user's folder. Say so once per boot, loudly enough to act on.
        if config.memory.per_customer:
            logger.warning(
                "[%s] multi-user mode is ON but the '%s' backend cannot carry the "
                "customers/** deny rule (deepagents refuses permissions on backends "
                "with command execution). The agent's file tools and shell can reach "
                "every user's folder; per-user isolation rests on the memory tools, "
                "your skill scripts and the prompt. For a hard boundary use the "
                "read-only 'filesystem' backend, or give each subscriber their own "
                "agent instance.",
                config.name,
                getattr(backend_cfg, "type", "?"),
            )
        return None
    if backend_cfg.root_dir == "/":
        return None

    def _subtree(root: Path) -> list[str]:
        # `<dir>/**` does not match the dir itself, so list both — otherwise
        # `ls` on the root is denied by the catch-all deny rule below.
        return [str(root), f"{root}/**"]

    # Per-customer memory lives INSIDE the agent workspace
    # (`<home>/agents/<name>/workspace/customers/<platform>-<user_id>/`), so the file tools
    # would otherwise let one customer's conversation read every other
    # customer's notes. deepagents takes the FIRST matching rule, so this deny
    # goes ahead of every allow — including on the `access_mode="read"` branch
    # below, where reads are otherwise entirely unrestricted. `read_file` is
    # refused outright; `ls`/`glob`/`grep` filter these paths out of results.
    # The tools themselves reach the store directly (not through the backend),
    # so they keep working — see olano/memory/customer_memory.py.
    customer_deny: list[Any] = []
    if config.memory.per_customer:
        from olano.memory.customer_memory import CUSTOMERS_DIR_NAME, customers_base

        deny_paths = _subtree(customers_base(config.name).resolve())
        ws_customers = (workspace_root / CUSTOMERS_DIR_NAME).resolve()
        if str(ws_customers) not in deny_paths:
            deny_paths.extend(_subtree(ws_customers))
        customer_deny.append(
            FilesystemPermission(operations=["read", "write"], paths=deny_paths, mode="deny")
        )

    if backend_cfg.access_mode == "read":
        # Deny all writes; reads stay permissive by default (ls, read_file, glob, grep)
        return [
            *customer_deny,
            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
        ]

    rules: list[Any] = [*customer_deny]

    # Fleet filesystem access (AgentFather): the same trees the sandbox mounts.
    # deepagents takes the first matching rule, so the read-only logs get their
    # write deny ahead of their read allow, and both ahead of the catch-all.
    from olano.security.fleet_access import fleet_roots

    for fleet_root in fleet_roots(config):
        if fleet_root.writable:
            rules.append(
                FilesystemPermission(
                    operations=["read", "write"], paths=_subtree(fleet_root.path), mode="allow"
                )
            )
        else:
            rules.append(
                FilesystemPermission(
                    operations=["write"], paths=_subtree(fleet_root.path), mode="deny"
                )
            )
            rules.append(
                FilesystemPermission(
                    operations=["read"], paths=_subtree(fleet_root.path), mode="allow"
                )
            )

    # Skill sources (managed <home>/skills/, config.skills dirs) are advertised
    # in the system prompt by absolute SKILL.md location — grant read access
    # even when they live outside the workspace. Writes only pass when the dir
    # is inside the workspace (covered by the read_write rule below).
    skill_read_paths: list[str] = []
    for d in skill_dirs or []:
        skill_read_paths.extend(_subtree(Path(d).resolve()))
    if skill_read_paths:
        rules.append(
            FilesystemPermission(operations=["read"], paths=skill_read_paths, mode="allow")
        )

    # read_write: allow reads+writes inside workspace, deny outside
    allowed_paths: list[str] = _subtree(workspace_root.resolve())
    for sp in config.shared_paths:
        allowed_paths.extend(_subtree(Path(sp.source).resolve()))
    rules.append(
        FilesystemPermission(operations=["read", "write"], paths=allowed_paths, mode="allow")
    )
    rules.append(FilesystemPermission(operations=["read", "write"], paths=["/**"], mode="deny"))
    return rules


def _build_compiled_subagent(
    sub: SubAgentConfig,
    parent_model: str,
    config: AgentConfig,
    workspace_root: Path,
    hooks: Any | None = None,
) -> Any:
    """Promote a subagent with its own backend to a CompiledSubAgent."""
    from deepagents import create_deep_agent

    from olano.security.deepagents_middleware import build_security_middleware

    backend = _build_deepagents_backend(
        sub.backend,  # type: ignore[arg-type]
        workspace_root,
        agent_name=getattr(sub, "name", None),
        shared_mounts=_resolve_sandbox_shared_mounts(config, workspace_root),
        # A subagent has no sandbox block of its own; it runs under the parent's
        # deployment sizing, like every other resource it inherits.
        sandbox_overrides=config.sandbox,
    )
    model = _build_model(sub.model or parent_model, {}, config.reasoning_effort)
    tools = resolve_tools(_with_builtin_document_tools(sub.tools))
    middleware = build_security_middleware(
        config=config, workspace_root=workspace_root, hooks=hooks, backend=backend
    )

    nested = [
        _build_subagent_dict(s, sub.model or parent_model, config, workspace_root, hooks)
        for s in sub.subagents
        if s.enabled
    ]

    guarded = guard_tools(tools)
    return create_deep_agent(
        model=model,
        tools=guarded if guarded else None,
        system_prompt=sub.system_prompt,
        middleware=middleware or (),
        subagents=nested if nested else None,
        backend=backend,
    )


def _build_subagent_dict(
    sub: SubAgentConfig,
    parent_model: str,
    config: AgentConfig,
    workspace_root: Path,
    hooks: Any | None = None,
) -> Any:
    from olano.security.deepagents_middleware import build_security_middleware

    # Tier 2: subagent has its own backend — promote to CompiledSubAgent
    if sub.backend is not None:
        return _build_compiled_subagent(sub, parent_model, config, workspace_root, hooks)

    # Tier 1: dict subagent — inherits parent backend
    d: dict[str, Any] = {
        "name": sub.name,
        "description": sub.description,
        "system_prompt": sub.system_prompt,
        "tools": guard_tools(resolve_tools(_with_builtin_document_tools(sub.tools))),
        "middleware": build_security_middleware(
            config=config,
            workspace_root=workspace_root,
            hooks=hooks,
        ),
    }
    # Pass provider-aware model instances into deepagents. Raw custom provider
    # strings such as ``openai-codex:*`` are not understood by LangChain's
    # generic provider inference inside deepagents.
    d["model"] = _build_model(sub.model or parent_model, {}, config.reasoning_effort)
    if sub.skills:
        d["skills"] = sub.skills
    if sub.subagents:
        d["subagents"] = [
            _build_subagent_dict(
                s,
                parent_model,
                config,
                workspace_root,
                hooks,
            )
            for s in sub.subagents
        ]
    return d


def _with_builtin_document_tools(tool_names: list[str]) -> list[str]:
    """Ensure subagents can read common local documents without bespoke config."""
    merged: list[str] = []
    seen: set[str] = set()
    for name in [*tool_names, *BUILTIN_DOCUMENT_TOOL_NAMES]:
        if name and name not in seen:
            seen.add(name)
            merged.append(name)
    return merged


def _register_tools(tools: list[Any]) -> None:
    """Register dynamically-created tools so subagents can resolve them by name."""
    from olano.tools.registry import register_tool

    for tool in tools:
        register_tool(tool.name, tool)


def _build_notes_tools(workspace_root: Path) -> list[Any]:
    """Create notes tools, wire notes path, and start filesystem watcher."""
    try:
        from olano.memory.notes_store import NotesStore
        from olano.memory.notes_tools import set_notes_path

        notes_path = workspace_root / "notes"
        legacy_vault_path = workspace_root / "vault"
        if legacy_vault_path.exists() and not notes_path.exists():
            legacy_vault_path.rename(notes_path)
        notes_path.mkdir(parents=True, exist_ok=True)
        set_notes_path(notes_path)

        # Start notes directory watcher so edits to .md files are auto-synced.
        try:
            from olano.memory.rag_watcher import NotesDirectoryWatcher

            watcher_key = ("notes", str(notes_path.resolve()))
            existing = _RAG_WATCHERS.get(watcher_key)
            if not existing or not getattr(existing, "is_running", False):
                if existing:
                    try:
                        existing.stop()
                    except Exception:
                        pass
                    _RAG_WATCHERS.pop(watcher_key, None)
                notes_instance = NotesStore(notes_dir=notes_path)
                watcher = NotesDirectoryWatcher(notes_instance, notes_path)
                if watcher.start():
                    _RAG_WATCHERS[watcher_key] = watcher
        except Exception as _w_exc:
            import logging as _log

            _log.getLogger(__name__).debug("Notes watcher start skipped: %s", _w_exc)

        tools = resolve_tools(_NOTES_TOOL_NAMES)
        _register_tools(tools)
        return tools
    except Exception as e:
        print(f"Warning: could not init notes tools: {e}")
        return []


def _register_secret_tools() -> None:
    """Register credentials-vault tools in the global registry (side effect only).

    ``secret_get``/``secret_list``/``secret_set`` are no longer auto-bound to
    every agent's top-level toolset — that used to mean any agent (a customer
    support bot included) could enumerate or read business credentials whether
    or not it ever asked for that capability. They are now ordinary opt-in
    tools: registering them here just makes the names resolvable, same as any
    other toolkit; an agent only gets them by listing ``secret_get`` /
    ``secret_list`` / ``secret_set`` in ``agent.json5 → tools`` (as the
    ``agentfather`` archetype already does for ``secret_list``/``secret_set``).
    """
    try:
        _register_tools(resolve_tools(_SECRET_TOOL_NAMES))
    except Exception as e:
        print(f"Warning: could not register credentials-vault tools: {e}")


def _build_graph_tools(workspace_root: Path) -> list[Any]:
    """Create memory graph tools and wire graph path."""
    try:
        from olano.memory.graph_tools import set_graph_path

        graph_path = workspace_root / "memories" / "graph.db"
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        set_graph_path(graph_path)
        tools = resolve_tools(_GRAPH_TOOL_NAMES)
        _register_tools(tools)
        return tools
    except Exception as e:
        print(f"Warning: could not init memory graph tools: {e}")
        return []


def _build_interrupt_on(config: AgentConfig) -> dict[str, bool | dict[str, Any]] | None:
    """Build the deepagents human-in-the-loop ``interrupt_on`` map.

    The approval set (which tools pause for approval) is resolved from the
    agent's security profile, the explicit ``hitl_*`` toggles, and per-tool
    overrides — see :func:`olano.security.hitl_policy.resolve_approval_set`,
    the single source of truth shared with the runtime decider in
    ``core/runner.py``.
    """
    from olano.security.hitl_policy import resolve_approval_set

    interrupt_names = resolve_approval_set(config)
    if not interrupt_names:
        return None

    return {
        tool_name: {"allowed_decisions": ["approve", "reject"]}
        for tool_name in sorted(interrupt_names)
    }


def _select_tool_subset(tools: list[Any], selected_names: list[str] | None) -> list[Any]:
    """Return a stable subset of tools by name, preserving tool list order."""
    if selected_names is None:
        return list(tools)
    selected = set(selected_names)
    return [tool for tool in tools if tool.name in selected]


def _build_memory_tools(
    config: AgentConfig,
    workspace_root: Any,
    *,
    categorized: bool = False,
) -> list[Any] | dict[str, list[Any]]:
    """Create and register MarkdownMemory/RAG tools for top-level or subagent use."""
    categories: dict[str, list[Any]] = {"memory": [], "rag": [], "customer": []}
    if config.memory.per_customer:
        # Customer-facing mode: isolate memory per authenticated sender. The
        # global shared MEMORY.md tools are deliberately NOT registered — only
        # the context-scoped per-customer tools plus a shared read-only KB.
        try:
            from olano.memory.customer_memory import (
                make_customer_memory_tools,
                make_shared_knowledge_tools,
            )

            # The top_level_memory_tools pin applies to categories["memory"]
            # ONLY — i.e. the per-customer memory + shared-knowledge tools it
            # can sensibly name. The rest of the multi-user set goes in
            # categories["customer"], EXEMPT from the pin: each of those rides
            # its own opt-in (allowed_actions, monetization, user_scheduling,
            # per_customer itself), and a config that pins e.g. [] to drop the
            # memory tools must not silently strip execute_action, the
            # customer file tools, subscription_status, the schedule_my_*
            # trio or the mute switches with it — that exact accident shipped
            # once and left the reference agent with no forget_me and no
            # actions at all.
            cust_tools = make_customer_memory_tools(config.name)
            know_tools = make_shared_knowledge_tools(Path(workspace_root))
            memory_like = [*cust_tools, *know_tools]
            customer_extras: list[Any] = []

            # Per-user files + the allow-listed action runner. Same identity
            # rule as the memory tools above: the platform and sender id come
            # from the request context, never from a tool argument, so there is
            # nothing for a prompt injection to re-point at another user.
            from olano.config.home import get_olano_paths
            from olano.tools.customer_actions import make_customer_action_tools

            # A relative script path in `allowed_actions` resolves against the
            # workspace, the agent's own directory (where a bundled skill's
            # scripts/ lives), and any configured skill root.
            action_roots = [
                Path(workspace_root),
                get_olano_paths().agent_workspace_dir(config.name).parent,
                *(Path(d) for d in (config.skills or [])),
            ]
            customer_extras.extend(
                make_customer_action_tools(config.name, config.allowed_actions, action_roots)
            )

            # A monetized agent can answer "am I still on the trial?" instead of
            # guessing. Read-only, and scoped to the asker by the same rule.
            if getattr(config.monetization, "enabled", False):
                from olano.monetization.tools import make_subscription_tools

                customer_extras.extend(make_subscription_tools(config.name))

            # Per-user scheduling (opt-in): the end-user can keep a few
            # scheduled jobs of their own, stored with THEIR identity and run
            # as them — capped and rate-floored by the customer_service knobs.
            if getattr(config.customer_service, "user_scheduling", False):
                from olano.tools.customer_scheduler import (
                    make_customer_scheduler_tools,
                )

                customer_extras.extend(
                    make_customer_scheduler_tools(config.name, config.customer_service)
                )

            # Subscriber-controlled delivery switches ("say stop any time").
            # The blocked flag is operator-only and has no tool here.
            from olano.memory.customer_prefs import make_customer_pref_tools

            customer_extras.extend(make_customer_pref_tools(config.name))

            # Consent-based customer↔customer connections (opt-in): invite
            # message out, `connect <CODE>` back in each side's own chat, and
            # a send tool whose recipient space is only the caller's own
            # consented connections.
            if getattr(config.customer_service, "connections", False):
                from olano.connections.tools import make_customer_connection_tools

                customer_extras.extend(make_customer_connection_tools(config.name, config))

            # Per-customer credentials: register the declared names for the
            # fail-closed resolver in every case (an operator can provision
            # values without the chat tools), and bind the tools on the
            # user_credentials opt-in.
            from olano.tools.customer_credentials import (
                make_customer_credential_tools,
                register_customer_credential_names,
            )

            register_customer_credential_names(
                config.name, getattr(config.customer_service, "customer_credential_names", [])
            )
            if getattr(config.customer_service, "user_credentials", False):
                customer_extras.extend(make_customer_credential_tools(config.name, config))

            _register_tools(memory_like + customer_extras)
            categories["memory"] = memory_like
            categories["customer"] = customer_extras
        except Exception as e:
            print(f"Warning: could not init per-customer memory: {e}")
    elif config.memory.markdown and (
        config.default_internal_tools or config.top_level_memory_tools
    ):
        # Shared MEMORY.md tools are a DEFAULT grant (memory.markdown is on out
        # of the box), so the default_internal_tools switch covers them — but a
        # non-empty top_level_memory_tools pin is the operator explicitly naming
        # the tools they want, so it survives the knob (the pin then narrows
        # this set in _build_extra_tools via _select_tool_subset). The
        # per-customer branch above is untouched: the multi-user set is an
        # explicit opt-in and exactly what a locked-down marketplace agent
        # still needs.
        try:
            from olano.memory.markdown_memory import MarkdownMemory, make_memory_tools

            mem = MarkdownMemory(workspace_root)
            mem_tools = make_memory_tools(mem)
            _register_tools(mem_tools)
            categories["memory"] = mem_tools
        except Exception as e:
            print(f"Warning: could not init markdown memory: {e}")

    if config.memory.rag:
        try:
            from olano.memory.rag_memory import RAGMemory, make_rag_tools
            from olano.tools.knowledge_tools import set_rag

            rag = RAGMemory(
                persist_dir=workspace_root / "rag",
                collection_name=config.memory.rag_collection,
            )
            # Share this RAG instance with the knowledge tools
            set_rag(rag)
            # Auto-ingest core workspace markdown files on boot — OFF the caller's
            # thread so the embedding-model load + doc embedding don't block it.
            _spawn_workspace_ingest(rag, workspace_root)
            rag_tools = make_rag_tools(rag)
            _register_tools(rag_tools)
            categories["rag"] = rag_tools
        except Exception as e:
            print(f"Warning: could not init RAG memory: {e}")

    if categorized:
        return categories
    return [*categories["memory"], *categories["customer"], *categories["rag"]]


# Per-agent snapshot of the last MCP build: which servers connected, with which
# tools, or why they didn't. Read by the runtime (reload delta), the dashboard
# chat indicator, and the prompt assembler (so the agent knows its live tools).
_MCP_SUMMARY: dict[str, list[dict[str, Any]]] = {}


def _tool_blurb(tool: Any, limit: int = 240) -> str:
    """First sentence-ish of a tool's description, for listings."""
    desc = " ".join(str(getattr(tool, "description", "") or "").split())
    return desc if len(desc) <= limit else desc[: limit - 1].rstrip() + "…"


def _tool_schema_tokens(tool: Any) -> int:
    """Estimated request cost of one bound tool schema (name + description + args)."""
    try:
        from olano.runtime.context_report import tool_schema_tokens

        return tool_schema_tokens(tool)
    except Exception:  # pragma: no cover - the estimate must never fail a connect
        return 0


def get_mcp_summary(name: str) -> list[dict[str, Any]]:
    """Return the last-known per-server MCP connection summary for an agent."""
    return _MCP_SUMMARY.get(name, [])


def mark_mcp_auth_required(agent: str, url: str, message: str) -> bool:
    """Record that *url* refused *agent*'s grant at call time (see ``_connector_auth``).

    The boot summary said ``connected`` because the connect succeeded; the
    server has since revoked the grant and a tool call was the first thing to
    learn it. Flipping the row to ``auth_required`` is what makes the MCP tab
    honest, keeps the health sweep from rebuilding the graph on a timer (that
    status is exempt from its backoff) and lets the next sweep ring the bell.
    Returns whether a row for *url* existed.
    """
    for row in _MCP_SUMMARY.get(agent, []):
        if row.get("url") == url:
            row["ok"] = False
            row["status"] = "auth_required"
            row["message"] = " ".join(str(message).split())[:200] or "OAuth authorization required"
            return True
    return False


def _reap_abandoned_mcp_connect(task: asyncio.Task[Any], reach_key: str = "") -> None:
    """Done-callback for an MCP boot connect that outlived its cancel grace.

    Retrieves the task's outcome so asyncio never logs "exception was never
    retrieved" for it, and records how it eventually ended (it may hold a
    zombie child process the whole time — worth a log line when it clears).

    A late success also **evicts the failure memo** for that endpoint. This is
    the honest case for a first-run stdio server whose ``npx``/``uvx`` had to
    download its package: the agent that triggered the download times out, but
    the package lands in the cache moments later and the next agent would
    genuinely connect. Without the eviction the memo would turn "one agent
    missed it" into "every agent missed it".
    """
    if task.cancelled():
        logger.debug("Abandoned MCP boot connect finally honored cancellation.")
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("Abandoned MCP boot connect finished with: %s", exc)
        return
    if reach_key:
        from olano.core.mcp_setup import forget_boot_failure

        forget_boot_failure(reach_key)
    logger.info(
        "Abandoned MCP boot connect eventually succeeded — its tools were "
        "discarded; use 'Reload MCP' to pick them up."
    )


async def _build_mcp_tools(config: AgentConfig) -> tuple[list[Any], list[dict[str, Any]]]:
    """Connect each enabled MCP server and return ``(tools, summary)``.

    ``summary`` has one row per *configured* server (connected or not) so callers
    can tell the agent and the dashboard exactly which MCP tools are live::

        {"url", "transport", "ok": bool, "status", "tools": [names], "message"}
    """
    summary: list[dict[str, Any]] = []
    if not config.mcp_servers:
        _MCP_SUMMARY[config.name] = summary
        return [], summary
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        from olano.core.mcp_setup import (
            BOOT_CANCEL_GRACE,
            BOOT_CONNECT_TIMEOUT,
            _stdio_remote_wrapper_hint,
            boot_connect_concurrency,
            build_connection_params,
            connection_full_key,
            connection_reach_key,
            needs_authorization,
            normalize_transport,
            oauth_token_state,
            recall_boot_failure,
            remember_boot_failure,
            sanitize_mcp_tool_schemas,
            stdio_unavailable_reason,
        )
    except ImportError:
        print("Warning: langchain-mcp-adapters not installed; MCP tools skipped.")
        _MCP_SUMMARY[config.name] = summary
        return [], summary

    def _row(srv: Any, *, ok: bool, status: str, tools: list[str], message: str) -> dict[str, Any]:
        return {
            "url": srv.url,
            "transport": normalize_transport(
                getattr(srv, "transport", None), command=getattr(srv, "command", None)
            ),
            "ok": ok,
            "status": status,
            "tools": tools,
            "message": message,
        }

    # Load each server in isolation. A single failing/unauthorized server must
    # never crash the agent boot or take down the other servers' tools — so each
    # gets its own client + try/except, and OAuth servers without stored tokens
    # are skipped proactively (otherwise the OAuth provider raises mid-connect).
    #
    # Two passes. First a synchronous classify pass that resolves everything
    # decidable without I/O (disabled / misconfigured / OAuth-unauthorized), then
    # a concurrent connect pass over whatever is left. The connect is pure I/O
    # wait, so running the servers serially just made each one's
    # BOOT_CONNECT_TIMEOUT additive — an agent with two dead servers spent 60s
    # of boot doing nothing. Results are reassembled in configured order so the
    # summary (and the tool list) stay deterministic regardless of finish order.
    all_tools: list[Any] = []
    # index → row, for the servers resolved without connecting
    resolved: dict[int, dict[str, Any]] = {}
    # (index, srv, params, reach_key, full_key) for servers needing a live connect
    pending: list[tuple[int, Any, dict[str, Any], str, str]] = []
    for idx, srv in enumerate(config.mcp_servers):
        if not srv.enabled:
            resolved[idx] = _row(
                srv, ok=False, status="disabled", tools=[], message="Disabled in config"
            )
            continue
        # Credentials (vault refs, OAuth token store) resolve as the agent this
        # config acts for — a worker under a fresh name still gets its parent's.
        params = build_connection_params(srv, config.credential_scope or config.name)
        if params is None:
            # Two distinct skips: a stdio entry the deployment policy refused
            # (blocked — the reason names the missing piece and the remote
            # alternative), vs. stdio without a command / otherwise
            # unconnectable (misconfigured). "blocked" is not in mcp_health's
            # recoverable statuses, so the sweep won't hammer it.
            blocked = stdio_unavailable_reason(srv)
            if blocked:
                print(f"Warning: MCP server '{srv.url or srv.command}' skipped — {blocked}")
                resolved[idx] = _row(srv, ok=False, status="blocked", tools=[], message=blocked)
            else:
                resolved[idx] = _row(
                    srv, ok=False, status="misconfigured", tools=[], message="No launch command"
                )
            continue
        if (getattr(srv, "auth", "none") or "none") == "oauth":
            token = oauth_token_state(config.name, srv.url)
            if not token["authorized"]:
                # No tokens, or an expired access token with no refresh token —
                # either way connecting would just raise mid-handshake. Skip with
                # a message that says which, so the log points at the real fix.
                expired = token["state"] == "expired"
                reason = (
                    "has an expired OAuth token and no refresh token"
                    if expired
                    else "is not authorized yet"
                )
                print(
                    f"Warning: MCP server '{srv.url}' requires OAuth but agent "
                    f"'{config.name}' {reason}; skipping. "
                    "Re-authorize it in the dashboard MCP setup panel."
                )
                resolved[idx] = _row(
                    srv,
                    ok=False,
                    status="auth_required",
                    tools=[],
                    message="OAuth token expired" if expired else "OAuth not authorized",
                )
                continue

        # Already known bad? An earlier agent in this boot sweep paid up to 35s
        # to learn it; don't pay it again. See the failure-memo notes in
        # mcp_setup for why the reachability verdict is shared across agents
        # holding different credentials while other failures are not.
        reach_key = connection_reach_key(params)
        full_key = connection_full_key(params, agent=config.name, url=srv.url)
        remembered = recall_boot_failure(reach_key, full_key)
        if remembered is not None:
            origin = "" if remembered.url == srv.url else f" of '{remembered.url}'"
            note = f"cached: same failure{origin} {remembered.age():.0f}s ago"
            print(
                f"Warning: MCP server '{srv.url}' skipped — {note}. "
                "Use 'Reload MCP' to retry it now."
            )
            resolved[idx] = _row(
                srv,
                ok=False,
                status=remembered.status,
                tools=[],
                message=f"{remembered.message} ({note})",
            )
            continue
        pending.append((idx, srv, params, reach_key, full_key))

    async def _connect_one(
        srv: Any, params: dict[str, Any], reach_key: str, full_key: str
    ) -> tuple[list[Any], dict[str, Any]]:
        """Connect one server, returning ``(tools, summary_row)``. Never raises:
        every failure mode is folded into the row so one bad server can neither
        crash the boot nor cancel its siblings' in-flight connects.

        Failures are remembered so the next agent to configure the same
        connection skips them — under the *reach* key for a timeout (nothing
        came back, so no credential could have changed that) and under the
        *full* key for everything else (a 401 is this agent's 401).
        """
        try:
            single: dict[str, Any] = {srv.url: params}
            client = MultiServerMCPClient(single)
            # Cap the connect+list so a slow/hung server can't block the whole
            # agent boot (the probe path is bounded the same way). Not
            # asyncio.wait_for: wait_for awaits the *cancellation* of the inner
            # task, and a stdio child stuck in an interactive prompt (e.g. an
            # `mcp-remote` wrapper waiting for OAuth in a headless service) can
            # wedge the SDK's cleanup so that cancellation never completes —
            # observed as a runtime that stays down for good. Instead: bounded
            # wait, then cancel with a short grace, then ABANDON the connect
            # task and keep booting.
            connect = asyncio.ensure_future(client.get_tools())
            done, _pending = await asyncio.wait({connect}, timeout=BOOT_CONNECT_TIMEOUT)
            if not done:
                connect.cancel()
                await asyncio.wait({connect}, timeout=BOOT_CANCEL_GRACE)
                if not connect.done():
                    # Detach: keep a reference alive via the callback and
                    # retrieve the eventual result/exception so the loop never
                    # logs "exception was never retrieved". The key rides along
                    # so a late success can evict the memo we're about to write.
                    connect.add_done_callback(
                        functools.partial(_reap_abandoned_mcp_connect, reach_key=reach_key)
                    )
                raise TimeoutError
            tools = connect.result()  # re-raises the server's own failure
            # Filter by *this* server's deny-list and return only what survives.
            # Filtering the combined list afterwards instead meant one server's
            # disabled_tools also stripped a same-named tool from every other
            # server on the agent.
            kept = [t for t in tools if getattr(t, "name", "") not in srv.disabled_tools]
            # The tools this server offered but the operator switched off (per
            # server `disabled_tools`). Never bound, never in the prompt's tool
            # list; recorded here so `self_list_mcp_tools` can tell the agent
            # "this exists, it is off" when a task would want it.
            disabled_rows = [
                {"name": getattr(t, "name", ""), "description": _tool_blurb(t)}
                for t in tools
                if getattr(t, "name", "") in srv.disabled_tools
            ]
            # A schema one provider rejects fails the whole turn, not one tool.
            sanitize_mcp_tool_schemas(kept, server=srv.url)
            # The adapter records nothing about where a tool came from. Tag
            # each one with its server so a call-time refusal (a revoked grant,
            # a 401 the SDK's own refresh could not cure) can be repaired and
            # explained per server: tools/_connector_auth.py reads these keys.
            auth_kind = str(getattr(srv, "auth", "none") or "none")
            for t in kept:
                meta = dict(getattr(t, "metadata", None) or {})
                meta.update({"mcp_server_url": srv.url, "mcp_auth": auth_kind})
                object.__setattr__(t, "metadata", meta)
            row = _row(
                srv,
                ok=True,
                status="connected",
                tools=[getattr(t, "name", "") for t in kept],
                message=f"{len(kept)} tool{'s' if len(kept) != 1 else ''}",
            )
            # Bound-tool inventory for `/context` and `self_list_mcp_tools`:
            # what each schema costs in the request (the dashboard used to
            # charge MCP tools 0 tokens as "on-demand", which they are not),
            # its one-line description, and the switched-off siblings.
            details = [
                {
                    "name": getattr(t, "name", ""),
                    "description": _tool_blurb(t),
                    "tokens": _tool_schema_tokens(t),
                }
                for t in kept
            ]
            row["tool_details"] = details
            row["tool_tokens"] = sum(d["tokens"] for d in details)
            row["disabled"] = disabled_rows
            return kept, row
        except TimeoutError:
            hint = _stdio_remote_wrapper_hint(
                getattr(srv, "command", None) or "", list(getattr(srv, "args", []) or [])
            )
            print(
                f"Warning: MCP server '{srv.url}' did not respond within "
                f"{BOOT_CONNECT_TIMEOUT:.0f}s; skipping. The agent will boot without "
                "its tools — use 'Reload MCP' once the server is reachable."
                + (f"\nHint: {hint}" if hint else "")
            )
            remember_boot_failure(
                reach_key, status="timeout", message="Connection timed out", url=srv.url
            )
            return [], _row(
                srv, ok=False, status="timeout", tools=[], message="Connection timed out"
            )
        except Exception as exc:
            # Isolate per-server failures so one bad server can't crash the boot.
            #
            # "Needs a human at a consent screen" is a different *kind* of
            # failure from "the server broke", and the difference decides what
            # the runtime does next: `error` is in mcp_health's
            # RECOVERABLE_STATUSES, so filing it there had the sweep rebuilding
            # this agent's entire graph on a 5-min-to-6-hour timer to re-ask a
            # question no amount of retrying can answer. The pre-flight check
            # above catches the common case (no token at all) before a provider
            # is ever built; this catches the rest — a grant that was revoked,
            # or one whose refresh the server just refused mid-handshake.
            if needs_authorization(exc):
                message = "OAuth authorization required"
                print(
                    f"Warning: MCP server '{srv.url}' needs authorization for agent "
                    f"'{config.name}'; skipping. Re-authorize it in the dashboard "
                    "MCP setup panel."
                )
                remember_boot_failure(
                    full_key, status="auth_required", message=message, url=srv.url
                )
                return [], _row(srv, ok=False, status="auth_required", tools=[], message=message)
            print(f"Warning: MCP server '{srv.url}' failed to load ({exc}); skipping.")
            message = str(exc)[:200]
            remember_boot_failure(full_key, status="error", message=message, url=srv.url)
            return [], _row(srv, ok=False, status="error", tools=[], message=message)

    if pending:
        gate = asyncio.Semaphore(boot_connect_concurrency())

        async def _guarded(
            srv: Any, params: dict[str, Any], reach_key: str, full_key: str
        ) -> tuple[list[Any], dict[str, Any]]:
            async with gate:
                # Timed inside the gate so the row reads as this server's own
                # latency; time spent queueing behind the semaphore shows up as
                # the parent phase's unattributed remainder instead.
                with boot_phase(f"connect {srv.url}"):
                    return await _connect_one(srv, params, reach_key, full_key)

        # _connect_one never raises, but return_exceptions=True keeps an
        # unforeseen failure (or a cancellation during shutdown) from taking
        # down the sibling connects mid-flight.
        results = await asyncio.gather(
            *(
                _guarded(srv, params, reach_key, full_key)
                for _idx, srv, params, reach_key, full_key in pending
            ),
            return_exceptions=True,
        )
        for (idx, srv, _params, _reach, _full), result in zip(pending, results, strict=True):
            if isinstance(result, BaseException):
                print(f"Warning: MCP server '{srv.url}' failed to load ({result}); skipping.")
                resolved[idx] = _row(
                    srv, ok=False, status="error", tools=[], message=str(result)[:200]
                )
                continue
            kept, row = result
            resolved[idx] = row
            all_tools.extend(kept)

    # Reassemble in configured order — gather() finish order is arbitrary.
    summary.extend(resolved[i] for i in sorted(resolved))

    _MCP_SUMMARY[config.name] = summary
    return all_tools, summary


# Module-level registry of active watchers — kept alive for the process lifetime
_RAG_WATCHERS: dict[tuple[str, str], Any] = {}


def _rag_ingest_gate() -> Any:
    """The process-wide background-ingest semaphore.

    One background ingest at a time: a runtime boots every agent's ingest thread
    within seconds of each other, and N concurrent ONNX embedding runs multiply
    peak memory/CPU by N (a 17-agent boot OOM-killed a 2-core/7.5 GB host at
    6.3 GB). The ingest is a background warm-up, so serializing it costs nothing.
    It lives in ``rag_memory`` and is shared with the directory watchers, so a
    file dropped into a knowledgebase mid-boot can't embed alongside the boot
    warm-up. Imported lazily to keep the RAG stack off builder's import path.
    """
    from olano.memory.rag_memory import BACKGROUND_INGEST_GATE

    return BACKGROUND_INGEST_GATE


def _rag_watcher_key(collection: str, directory: Path) -> tuple[str, str]:
    return collection, str(directory.resolve())


def _spawn_workspace_ingest(rag: Any, workspace_root: Path) -> None:
    """Run the boot-time RAG ingest OFF the caller's thread.

    ``_auto_ingest_workspace`` loads the sentence-transformers embedding model
    (torch import + weights) and embeds every workspace/knowledgebase doc —
    tens of seconds on a cold start. On the runtime it runs inside
    ``build_agent_async`` on the asyncio event loop, so doing it inline starves
    uvicorn: ``/api/status`` can't be answered until every agent's docs are
    embedded (the #1 cause of a slow ``Waiting for the local API...`` wait). The
    RAG *tools* are already wired by the time we get here — only store
    *population* is deferred, so the agent is fully functional immediately and
    RAG results warm up within a few seconds.

    Opt out (run inline — e.g. for deterministic tests or a CLI one-shot) with
    ``LIONCLAW_SYNC_RAG_INGEST=1``.
    """
    if os.environ.get("LIONCLAW_SYNC_RAG_INGEST") == "1":
        _auto_ingest_workspace(rag, workspace_root)
        return

    def _run() -> None:
        try:
            # Nice the whole ingest thread down: it is CPU-heavy work nobody is
            # waiting on, so the scheduler should hand the cores to a live agent
            # turn or the event loop the moment either wants them. Thread-scoped
            # on Linux (setpriority applies to the calling thread), a no-op if
            # the platform disagrees.
            try:
                os.nice(10)
            except (AttributeError, OSError):  # pragma: no cover - platform dependent
                pass
            with _rag_ingest_gate():
                _auto_ingest_workspace(rag, workspace_root)
        except Exception as exc:  # a background ingest must never crash the runtime
            logger.warning("Background RAG ingest failed for %s: %s", workspace_root, exc)

    threading.Thread(
        target=_run,
        name=f"rag-ingest-{workspace_root.name}",
        daemon=True,
    ).start()


def _auto_ingest_workspace(rag: Any, workspace_root: Path) -> None:
    """
    Boot-time RAG sync:

    1. workspace collection  — core markdown files (MEMORY.md, AGENTS.md, …)
    2. knowledgebase collection — all files under workspace/knowledgebase/
       Also starts a file-system watcher so the KB stays in sync at runtime.
    3. memories collection  — today's + yesterday's daily logs
    """
    # ── 1. workspace: core markdown files ────────────────────────────────────
    core_files = [
        "MEMORY.md",
        "AGENTS.md",
        "SOUL.md",
        "IDENTITY.md",
        "USER.md",
        "TOOLS.md",
        "HEARTBEAT.md",
        "TEAM.md",
        "LEARNINGS.md",
        "IDEAS.md",
        "BRAINSTORMING.md",
        "CAPABILITIES.md",
    ]
    for fname in core_files:
        fpath = workspace_root / fname
        if fpath.exists():
            try:
                rag.ingest_file(fpath, collection="workspace")
            except Exception:
                pass

    # ── 2. knowledgebase: managed KB directory ────────────────────────────────
    kb_dir = workspace_root / "knowledgebase"
    kb_dir.mkdir(parents=True, exist_ok=True)

    try:
        stats = rag.sync_directory(
            collection="knowledgebase",
            directory=kb_dir,
            delete_removed=True,
        )
        if any(stats.values()):
            print(
                f"  KB sync: +{stats['added']} added, "
                f"~{stats['updated']} updated, "
                f"-{stats['removed']} removed, "
                f"{stats['skipped']} unchanged"
            )
    except Exception as e:
        print(f"  Warning: KB directory sync failed: {e}")

    # Start real-time watcher for the knowledgebase directory
    try:
        from olano.memory.rag_watcher import RAGDirectoryWatcher

        watcher_key = _rag_watcher_key("knowledgebase", kb_dir)
        existing = _RAG_WATCHERS.get(watcher_key)
        if existing and getattr(existing, "is_running", False):
            pass
        else:
            if existing:
                try:
                    existing.stop()
                except Exception:
                    pass
                _RAG_WATCHERS.pop(watcher_key, None)
            watcher = RAGDirectoryWatcher(rag, collection="knowledgebase", directory=kb_dir)
            if watcher.start():
                _RAG_WATCHERS[watcher_key] = watcher
    except Exception as e:
        print(f"  Warning: KB watcher could not start: {e}")

    # ── 3. system/knowledge: platform documentation (static, no watcher needed) ─
    # A directory sync, not per-file ingest: `delete_removed` drops the chunks
    # of a file that has left the directory (the legacy ARCHITECTURE / COMMANDS /
    # TROUBLESHOOTING copies an upgrade purges), which `ingest_file` alone left
    # searchable forever. Removal is scoped to this directory, so the user KB
    # synced above is never touched.
    system_knowledge_dir = workspace_root / "system" / "knowledge"
    if system_knowledge_dir.is_dir():
        try:
            rag.sync_directory(
                collection="knowledgebase",
                directory=system_knowledge_dir,
                glob="*.md",
                delete_removed=True,
            )
        except Exception as e:
            print(f"  Warning: system knowledge sync failed: {e}")

    # ── 4. memory: recent daily logs ─────────────────────────────────────────
    from datetime import date, timedelta

    memories_dir = workspace_root / "memories"
    for delta in (0, 1):
        day = date.today() - timedelta(days=delta)
        log = memories_dir / f"{day.isoformat()}.md"
        if log.exists():
            try:
                rag.ingest_file(log, collection="memory")
            except Exception:
                pass


def stop_rag_watchers() -> None:
    """Stop all active RAG directory watchers (call on process shutdown)."""
    for w in _RAG_WATCHERS.values():
        try:
            w.stop()
        except Exception:
            pass
    _RAG_WATCHERS.clear()


def _build_whoami_tool(config: AgentConfig, workspace_root: Any) -> list[Any]:
    """Create and register the whoami self-description tool for this agent."""
    if "whoami" not in config.tools:
        return []
    try:
        from olano.tools.agent_tools import make_whoami_tool

        tool = make_whoami_tool(config, workspace_root)
        _register_tools([tool])
        return [tool]
    except Exception as e:
        logger.warning("Could not build whoami tool: %s", e)
        return []


def _build_extra_tools(config: AgentConfig, workspace_root: Any) -> list[Any]:
    """Extra tools from config + memory tools + graph tools (backend handles file/shell ops)."""
    with boot_phase("memory_tools"):
        dynamic_tools = cast(
            "dict[str, list[Any]]",
            _build_memory_tools(config, workspace_root, categorized=True),
        )
    memory_enabled = config.memory.markdown or config.memory.rag
    # `default_internal_tools: false` turns off every IMPLICIT builder grant
    # below (graph, notes, workspace_context, whoami) — the same switch that
    # stops the config-level universal injection. Explicit grants survive the
    # knob, whatever their spelling: RAG rides memory.rag, the multi-user
    # identity-injected set rides memory.per_customer inside
    # _build_memory_tools, whoami comes back when listed in `tools:`, and a
    # non-empty `top_level_memory_tools`/`top_level_graph_tools` pin (or a
    # `top_level_workspace_context` the config file sets itself) is the
    # operator naming exactly what they want — so it stays.
    defaults_on = config.default_internal_tools
    # top_level_workspace_context defaults to True, so only a value the config
    # actually set counts as an explicit grant under the knob.
    wc_explicit = "top_level_workspace_context" in config.model_fields_set and bool(
        config.top_level_workspace_context
    )
    # Graph tools only when memory is enabled (requires a persistent workspace)
    with boot_phase("graph_tools"):
        graph_tools = (
            _build_graph_tools(Path(workspace_root))
            if memory_enabled and (defaults_on or config.top_level_graph_tools)
            else []
        )
    # Knowledge-vault notes tools (markdown wiki)
    with boot_phase("notes_tools"):
        notes_tools = (
            _build_notes_tools(Path(workspace_root)) if memory_enabled and defaults_on else []
        )
    # Credentials-vault secret tools (encrypted SQLite) — register the names so
    # they're resolvable, but don't auto-bind them; see _register_secret_tools.
    with boot_phase("secret_tools"):
        _register_secret_tools()
    # L2 workspace context tool — lets agent pull specific files mid-conversation
    with boot_phase("l2_tool"):
        l2_tools = (
            _build_l2_tool(config, workspace_root)
            if memory_enabled and (defaults_on or wc_explicit)
            else []
        )
        if l2_tools:
            _register_tools(l2_tools)
    # whoami — built dynamically so it can close over config and workspace.
    # Under the knob it still binds when the agent asks for it BY NAME: the
    # registry has no static whoami, so the explicit `tools:` entry resolves
    # to nothing and this dynamic build is the only source.
    with boot_phase("whoami_tool"):
        whoami_tools = (
            _build_whoami_tool(config, workspace_root)
            if defaults_on or "whoami" in (config.tools or [])
            else []
        )

    # Multimodal defaults (see-hear-speak-draw): granted implicitly, but ONLY
    # when the provider key that would run them actually resolves for this
    # agent. A tool the model can plan around and then can't call is worse than
    # no tool, so a box with no image key never hears image generation exists.
    # The catalog in config/internal_tools mirrors this condition for reporting.
    with boot_phase("multimodal_defaults"):
        multimodal_names = available_multimodal_tool_names(config)
    # Outbound A2A: an agent with a configured remote agent gets the a2a_*
    # tools implicitly (visibility only — every call re-reads the list and the
    # home-wide policy, so a dashboard edit applies on the next call).
    with boot_phase("a2a_defaults"):
        a2a_names = available_a2a_tool_names(config)
        # WhatsApp templates, send side: an agent with the WhatsApp (Twilio
        # Business API) connector gets the list/send pair implicitly. The admin
        # side (create/submit/delete) is never implicit; an operator lists it.
        a2a_names += available_whatsapp_business_tool_names(config)
    # Chat self-configuration (self_* tools): granted implicitly per resolved
    # autonomy capability. Binding is visibility only — every tool re-checks
    # its capability at call time, so a dashboard toggle applies to the next
    # call, not the next boot.
    # The magic-link family rides the same mechanism under one capability
    # (`magic_links`): links are how a chat-only human finishes anything that
    # needs a browser, so every agent can send one unless an operator says no.
    with boot_phase("autonomy_defaults"):
        autonomy_names = [
            *available_self_config_tool_names(config),
            *available_magic_link_tool_names(config),
        ]
    with boot_phase(
        "resolve_tools "
        f"({len(config.tools) + len(multimodal_names) + len(autonomy_names) + len(a2a_names)})"
    ):
        config_tools = resolve_tools(
            [*config.tools, *multimodal_names, *autonomy_names, *a2a_names]
        )
    memory_tools = _select_tool_subset(dynamic_tools["memory"], config.top_level_memory_tools)
    rag_tools = _select_tool_subset(dynamic_tools["rag"], config.top_level_rag_tools)
    # The non-memory multi-user set (files/actions/subscription/scheduling/
    # mute) is exempt from the memory pin by design — disabled_tools below is
    # the only per-agent way to drop one of these.
    customer_tools = dynamic_tools.get("customer", [])
    graph_tools = _select_tool_subset(graph_tools, config.top_level_graph_tools)
    l2_tools = l2_tools if config.top_level_workspace_context else []

    # config.tools is already filtered by AgentConfig._inject_builtin_ops, but the
    # builder-injected extras (memory/notes/graph, workspace_context) are
    # assembled here — apply disabled_tools to the final list too, so a per-agent
    # opt-out genuinely removes them. secret_* tools are intentionally NOT in this
    # merge (see _register_secret_tools) — they only appear via config_tools, i.e.
    # when an agent explicitly lists them in `tools:`.
    disabled: set[str] = set(config.disabled_tools)
    seen: set[str] = set()
    all_tools: list[Any] = []
    for t in (
        whoami_tools  # prepend so it's always near the top of the tool list
        + config_tools
        + memory_tools
        + customer_tools
        + rag_tools
        + graph_tools
        + notes_tools
        + l2_tools
    ):
        name = str(getattr(t, "name", id(t)))
        if name not in seen and name not in disabled:
            seen.add(name)
            all_tools.append(t)
    return all_tools


def _build_l2_tool(config: AgentConfig, workspace_root: Any) -> list[Any]:
    """Create the workspace_context tool that lets agents pull L2 files mid-conversation."""
    try:
        from langchain_core.tools import StructuredTool
        from pydantic import BaseModel, Field

        from olano.core.prompt_assembler import PromptAssembler

        ws = get_workspace(config.workspace)
        assembler = PromptAssembler(config, ws)

        class _WorkspaceContextInput(BaseModel):
            file_paths: str = Field(
                description="Comma-separated workspace file paths to load into context "
                "(e.g. 'projects/roadmap.md,MEMORY.md,USER.md')"
            )

        def _workspace_context(file_paths: str) -> str:
            paths = [p.strip() for p in file_paths.split(",") if p.strip()]
            return (
                assembler.build_l2(file_paths=paths) or "No content found for the requested files."
            )

        tool = StructuredTool.from_function(
            func=_workspace_context,
            name="workspace_context",
            description=(
                "Load specific workspace files into context (L2 progressive disclosure). "
                "Use when you need a specific document, project file, or memory that was not "
                "included in the initial context. Pass comma-separated paths like "
                "'projects/roadmap.md,USER.md'."
            ),
            args_schema=_WorkspaceContextInput,
        )
        return [tool]
    except Exception as e:
        print(f"Warning: could not create workspace_context tool: {e}")
        return []


def _load_workspace_middleware(config: AgentConfig, workspace_root: Any) -> Any:
    """Load Python middleware hooks from system/middleware/ and extensions/middleware/."""
    try:
        from olano.config.workspace import get_workspace
        from olano.core.middleware_loader import MiddlewareHooks, load_middleware
        from olano.core.system_middleware import load_builtin_middleware

        ws = get_workspace(config.workspace)
        builtin_hooks = load_builtin_middleware(ws, config)
        workspace_hooks = load_middleware(ws, config)
        combined = MiddlewareHooks()
        combined.on_input = [*builtin_hooks.on_input, *workspace_hooks.on_input]
        combined.on_output = [*builtin_hooks.on_output, *workspace_hooks.on_output]
        combined.on_tool_call = [*builtin_hooks.on_tool_call, *workspace_hooks.on_tool_call]
        combined.on_tool_result = [
            *builtin_hooks.on_tool_result,
            *workspace_hooks.on_tool_result,
        ]
        return combined
    except Exception as e:
        import logging

        logging.getLogger(__name__).debug("Middleware load skipped: %s", e)
        return None


def _build_all_tools(config: AgentConfig, workspace_root: Any) -> list[Any]:
    """Alias kept for registry tests compatibility."""
    return _build_extra_tools(config, workspace_root)


# ── Public API ────────────────────────────────────────────────────────────────


def _ensure_vault_for_session() -> None:
    """Unlock the credentials vault (best-effort) and hydrate os.environ.

    Idempotent: if the vault has already been unlocked earlier in the process
    (e.g. by the chat command which prompts for a passphrase before this
    runs), we skip the silent unlock and only re-hydrate env from the
    already-registered store.
    """
    from olano.vault.resolver import get_vault
    from olano.vault.runtime import maybe_hydrate_env_from_vault, unlock_vault_for_session

    vault = get_vault()
    if vault is None:
        result = unlock_vault_for_session(allow_prompt=False)
        vault = result.vault
    if vault is not None:
        # Hardened default: no global env hydration — toolkits read the vault
        # per-agent (tools/_credentials). Opt back in via LIONCLAW_LEGACY_ENV_HYDRATE.
        maybe_hydrate_env_from_vault(vault)


def _build_prelude(config: AgentConfig) -> dict[str, Any]:
    """Everything that must happen *before* the model is constructed.

    Split out of ``_prepare_agent_build`` so the async path can run it off the
    event loop — the deepagents patch alone imports that library's whole
    middleware stack. Returns the workspace and the per-agent credential overlay
    the model build needs.
    """
    with _BUILD_LOCK:
        # Before any create_deep_agent call (this builds compiled subagents too),
        # and before anything else imports deepagents on a cold process: import
        # it with its optional provider SDKs masked, and gate the Bedrock /
        # Fireworks prompt-caching factories on their packages being loaded, so
        # an agent never pays for OpenAI/Bedrock/Fireworks packages its model
        # does not use.
        _ensure_deepagents_provider_imports_deferred()
        workspace = get_workspace(config.workspace)
        workspace.init()
        _ensure_vault_for_session()
        try:
            from olano.security.sharing import (
                resolve_shared_mounts,
                sync_workspace_share_projections,
            )

            sync_workspace_share_projections(workspace.root, resolve_shared_mounts(config))
        except Exception:
            pass

        # Per-agent env is bound per-turn as a credential overlay
        # (tools/_credentials.agent_runtime_context) for call-time toolkit reads, so
        # it must NOT permanently leak into the shared process os.environ — that
        # global flatten is exactly what made two agents collide on the same service.
        # The legacy permanent flatten stays available as an opt-in rollback seam.
        from olano.tools._credentials import _build_overlay

        agent_env = _build_overlay(config.env)
        # A per-agent vault override of the model's provider key (e.g. a different
        # ANTHROPIC_API_KEY for this agent) is folded in too, so it reaches the model
        # layer at build time alongside config.env.
        _merge_provider_override(agent_env, config)
        if os.environ.get("LIONCLAW_LEGACY_ENV_FLATTEN") == "1":
            for k, v in agent_env.items():
                os.environ[k] = v
        # Pay the provider package import here, so the overlay window below is a
        # constructor call the event loop can afford to run inline.
        _warm_model_provider(config.model)
        return {"workspace": workspace, "agent_env": agent_env}


def _build_main_model(config: AgentConfig, agent_env: dict[str, str]) -> BaseChatModel:
    """Construct the agent's model with its credential overlay applied.

    Apply the agent's env transiently around the model build so per-agent
    provider keys (config.env or vault override) reach the model layer. Callers
    running off the event loop must pass an EMPTY overlay — see ``_transient_env``
    for why the non-empty window is loop-only.
    """
    with _transient_env(agent_env):
        return _build_model(
            config.model,
            config.model_kwargs,
            config.reasoning_effort,
            max_output_tokens=config.message_policy.max_output_tokens,
        )


def _prepare_agent_build_rest(
    config: AgentConfig,
    *,
    workspace: Any,
    model: BaseChatModel,
    mcp_summary: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The rest of the build, once the model exists.

    Runs off the event loop in the async path (this is the expensive part: every
    toolkit import, skill discovery, subagent compilation, prompt assembly). It
    touches no process-global credential state — subagent models are built from
    the ambient env, exactly as they were when this was one function.
    """
    with _BUILD_LOCK:
        return _prepare_agent_build_rest_locked(
            config, workspace=workspace, model=model, mcp_summary=mcp_summary
        )


def _prepare_agent_build_rest_locked(
    config: AgentConfig,
    *,
    workspace: Any,
    model: BaseChatModel,
    mcp_summary: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    with boot_phase("extra_tools"):
        extra_tools = _build_extra_tools(config, workspace.root)
    with boot_phase("skills"):
        skill_dirs = load_skills_for_agent(config, workspace)
    with boot_phase("subagent_registry"):
        subagent_registry = load_subagent_registry(config, workspace)
    if subagent_registry:
        from olano.tools.delegate_task import make_delegate_task_tool

        delegate_tool = make_delegate_task_tool(
            parent_config=config,
            workspace=workspace,
            subagent_registry=subagent_registry,
        )
        if delegate_tool is not None:
            # Dedup: skip if user already registered a delegate_task tool.
            existing = {getattr(t, "name", None) for t in extra_tools}
            if "delegate_task" not in existing:
                extra_tools = [*extra_tools, delegate_tool]
    with boot_phase("workspace_middleware"):
        hooks = _load_workspace_middleware(config, workspace.root)

    # Only include enabled subagents. A subagent whose whole tool list is the
    # browser (the archetypes' BrowserAgent) is dropped while config.yaml →
    # browser leaves the browser off: resolve_tools would hand it nothing, and
    # a delegate that can do nothing still invites the model to delegate.
    from olano.browser.policy import BROWSER_TOOL_NAMES, browser_tools_allowed

    active_subagents = [s for s in config.subagents if s.enabled]
    if (
        any(s.tools and all(t in BROWSER_TOOL_NAMES for t in s.tools) for s in active_subagents)
        and not browser_tools_allowed()
    ):
        active_subagents = [
            s
            for s in active_subagents
            if not (s.tools and all(t in BROWSER_TOOL_NAMES for t in s.tools))
        ]
    with boot_phase(f"subagents ({len(active_subagents)})"):
        subagents = [
            _build_subagent_dict(s, config.model, config, workspace.root, hooks)
            for s in active_subagents
        ]
    # Guard: remote sandbox backends don't support shared paths
    if is_remote_sandbox_backend(config.backend) and config.shared_paths:
        raise ValueError(
            f"Shared paths are not supported with remote backend type "
            f"'{config.backend.type}'. Remove shared_paths or use a local backend."  # type: ignore[union-attr]
        )

    # A top-level StoreBackend ("Virtual File System" env mode) defaults its
    # namespace to the agent name so agents stay isolated inside the shared
    # store database.
    if isinstance(config.backend, StoreBackendConfig) and not config.backend.namespace:
        config = config.model_copy(
            update={"backend": config.backend.model_copy(update={"namespace": [config.name]})}
        )

    with boot_phase("backend"):
        backend = _build_deepagents_backend(
            config.backend,
            workspace.root,
            agent_name=config.name,
            shared_mounts=_resolve_sandbox_shared_mounts(config, workspace.root),
            sandbox_overrides=config.sandbox,
            sandbox_owner=sandbox_owner(config),
        )
    with boot_phase("sandbox_sidecar"):
        _start_sidecar_sandbox(config, workspace.root)
    with boot_phase("fleet_access"):
        _handoff_fleet_access(config)
    with boot_phase("permissions"):
        permissions = _build_permissions(config, workspace.root, skill_dirs)

    # Cartography refresh — in the background by default (the prompt below
    # reads the *previous* run's map from disk; a fresh walk of a big workspace
    # measured 130s and must never gate a boot). LIONCLAW_SYNC_CARTOGRAPHY=1
    # runs it inline, and then this phase times the real walk.
    with boot_phase("cartography"):
        _spawn_cartography_refresh(workspace)

    # Use PromptAssembler for layered L0/L1 context (2.0 feature)
    with boot_phase("system_prompt"):
        system_prompt = _build_system_prompt(config, workspace, mcp_summary=mcp_summary)

    # Load workspace middleware hooks (system/middleware/ + extensions/middleware/)
    _MIDDLEWARE_HOOKS[config.name] = hooks

    # Context-window policy: compaction keep/trigger for every summarization
    # middleware deepagents builds for this graph (read back through the
    # contextvar in `_create_agent_graph`) and the MCP result cap below.
    from olano.config.system_settings import effective_context_window

    context_policy: dict[str, Any] = {
        "policy": effective_context_window(config),
        # The workspace's generated-state folder (Workspace.state_dir): where
        # shell-run logs, the workspace map and every other runtime artefact
        # already live, so offloaded history and results sit beside them.
        "offload_root": Path(workspace.state_dir),
    }

    from langchain.agents.middleware import TodoListMiddleware

    from olano.core.clock_middleware import build_clock_middleware
    from olano.core.disabled_tools_middleware import build_disabled_tools_middleware
    from olano.core.mcp_result_cap_middleware import build_mcp_result_cap_middleware
    from olano.core.skill_audit_middleware import (
        build_skill_audit_filter_middleware,
        build_skill_quarantine_middleware,
    )
    from olano.core.skill_awareness_middleware import build_skill_awareness_middleware
    from olano.core.steer_middleware import build_steer_middleware
    from olano.security.deepagents_middleware import build_security_middleware
    from olano.skills.loader import (
        compute_disabled_skills,
        compute_gated_skills,
        compute_quarantined_shadows,
        compute_skipped_skills,
    )

    with boot_phase("skill_audit"):
        # Skills the security audit flags: kept on disk + listed in /skills, but
        # pruned from the prompt by the filter middleware below (never block startup).
        skipped_skills = compute_skipped_skills(config, workspace)
        # Skills an operator (or the agent itself) switched off in
        # `agent.json5 → disabled_skills`. Pruned by the SAME middleware — a
        # source dir can't drop one skill, which is exactly why that middleware
        # exists — but computed separately so every human-facing surface can
        # still tell "you turned this off" from "the audit flagged this".
        # Third fact, same middleware: skills whose `requires_tools` this
        # agent lacks (an AgentFather playbook on an ordinary agent). Listed
        # first so a skill that is both gated and turned off reports the
        # deliberate choice in the merged reason.
        hidden_skills = {
            **compute_gated_skills(config, workspace),
            **compute_disabled_skills(config, workspace),
            **skipped_skills,
        }
        # Degraded learned shadows (preservation gate): swapped back to their
        # originals in the prompt so a lossy auto-learned copy never hides a good
        # skill. The shadow stays on disk for inspection/repair.
        quarantined_shadows = compute_quarantined_shadows(config, workspace)

    return {
        "workspace": workspace,
        "model": model,
        "extra_tools": extra_tools,
        "skill_dirs": skill_dirs,
        "subagents": subagents,
        "backend": backend,
        "permissions": permissions,
        "system_prompt": system_prompt,
        "context_policy": context_policy,
        # Main agent only: TodoListMiddleware (restores the `write_todos` tool,
        # `todos` state channel, and todo-planning prompt — deepagents 0.7 dropped
        # it from the create_deep_agent default stack, so we opt back in; the
        # dashboard renders its output and the prompt references it) +
        # security chain + audit-flagged skill quarantine + per-turn
        # skill-awareness refresh so skills installed/learned mid-session become
        # usable without a new thread, plus deepagents' on-demand
        # `compact_conversation` tool (manual /compact; automatic bounding is
        # handled by the base-stack SummarizationMiddleware). Both of these two
        # opt-in middlewares respect `disabled_tools` (skipping the middleware
        # also drops its prompt section), and build_disabled_tools_middleware
        # enforces `disabled_tools` on EVERY model bind — including the tools
        # deepagents attaches on its own (`task`, the backend filesystem/shell
        # primitives), which the builder's own tool merge can't reach. That
        # filter is what makes the dashboard's internal-tools checkboxes real
        # for every row (see config/internal_tools.py).
        "middleware": [
            *build_disabled_tools_middleware(config.disabled_tools),
            *([] if "write_todos" in config.disabled_tools else [TodoListMiddleware()]),
            # Mid-turn steering (core/steering.py). Listed AHEAD of the security
            # middleware on purpose: langchain makes the first middleware that
            # defines `before_model` the node the `tools` edge returns to, so a
            # steer accepted while tools ran is appended to the state before
            # the next model call — and the security `before_model` that runs
            # right after still scans it like any other user message.
            *build_steer_middleware(config),
            *build_security_middleware(
                config=config,
                workspace_root=workspace.root,
                hooks=hooks,
                backend=backend,
            ),
            *build_skill_audit_filter_middleware(blocked_skills=hidden_skills),
            *build_skill_quarantine_middleware(quarantined=quarantined_shadows),
            *build_skill_awareness_middleware(config=config, workspace=workspace),
            # Today's date and weekday on every model request
            # (core/clock_middleware.py): the prompt itself never carried one,
            # and an autonomous run guessed the weekday from its own files.
            *build_clock_middleware(),
            # Oversized MCP results go to a workspace file instead of the
            # context window (core/mcp_result_cap_middleware.py). Built from the
            # live connect summary, so an agent without MCP pays nothing.
            *build_mcp_result_cap_middleware(
                mcp_summary=mcp_summary,
                token_limit=context_policy["policy"].mcp_result_token_limit,
                state_dir=context_policy["offload_root"],
                agent_name=config.name,
            ),
            *(
                [_compact_mw]
                if "compact_conversation" not in config.disabled_tools
                and (
                    _compact_mw := _build_compact_tool_middleware(
                        model,
                        backend,
                        policy=context_policy["policy"],
                        offload_root=context_policy["offload_root"],
                    )
                )
                is not None
                else []
            ),
        ],
    }


def _prepare_agent_build(
    config: AgentConfig, *, mcp_summary: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Assemble everything ``_create_agent_graph`` needs. Fully synchronous.

    Used by the sync ``build_agent`` (CLI one-shots) and by tests. The runtime
    goes through ``_prepare_agent_build_async``, which runs these same phases off
    the event loop.
    """
    prelude = _build_prelude(config)
    model = _build_main_model(config, prelude["agent_env"])
    return _prepare_agent_build_rest(
        config, workspace=prelude["workspace"], model=model, mcp_summary=mcp_summary
    )


async def _prepare_agent_build_async(
    config: AgentConfig, *, mcp_summary: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """``_prepare_agent_build`` with the blocking phases moved off the event loop.

    Only one step can't go: constructing the model while a per-agent credential
    overlay is temporarily written into the process-global env. That window stays
    inline (see ``_transient_env``) and costs ~1ms, because ``_build_prelude``
    already imported the provider package in the worker thread. An agent with no
    overlay — the common case — has no window to protect, so its model is built
    off-loop with everything else.
    """
    with boot_phase("prelude"):
        prelude = await asyncio.to_thread(_build_prelude, config)
    agent_env: dict[str, str] = prelude["agent_env"]
    with boot_phase("model"):
        model = (
            _build_main_model(config, agent_env)
            if agent_env
            else await asyncio.to_thread(_build_main_model, config, agent_env)
        )
    with boot_phase("rest"):
        return await asyncio.to_thread(
            _prepare_agent_build_rest,
            config,
            workspace=prelude["workspace"],
            model=model,
            mcp_summary=mcp_summary,
        )


_SUMMARIZATION_OFFLOAD_PATCHED = False


def _workspace_offload_prefix(backend: Any) -> str | None:
    """Writable ``conversation_history`` offload prefix for ``backend``, or ``None``.

    deepagents' ``FilesystemBackend`` / ``LocalShellBackend`` expose their root as
    ``backend.cwd`` — there is no ``root_dir`` attribute — so an earlier
    ``getattr(backend, "root_dir")`` read always returned ``None`` and the repoint
    silently never happened, leaving deepagents' default ``/conversation_history``
    (the host filesystem root, not writable by a non-root service). Only
    real-filesystem backends (``virtual_mode=False``) need repointing: in virtual
    mode the default already resolves under the backend root, and ``StateBackend`` /
    ``CompositeBackend`` (which expose no ``virtual_mode`` here) handle offloading
    themselves.
    """
    from olano.config.workspace import WORKSPACE_STATE_DIRNAME

    root = getattr(backend, "cwd", None) or getattr(backend, "root_dir", None)
    if root and not getattr(backend, "virtual_mode", True):
        return str(Path(root) / WORKSPACE_STATE_DIRNAME / "conversation_history")
    return None


def _repoint_summarization_offload(mw: Any, history_prefix: str) -> None:
    """Move every path a summarization middleware writes under ``history_prefix``'s parent.

    deepagents derives three prefixes from one ``artifacts_root`` at construction
    time, so repointing only ``_history_path_prefix`` (all olano did through
    0.7.11) left the other two on the host root: the media a summary uploads
    (``_media_prefix``, ``<history>/media``) and the oversized trailing tool
    results it offloads when a request overflows (``_large_tool_results_prefix``).
    The second matters more since 0.7.23, whose input-budget check offloads that
    tail before *any* over-budget call, not only after a provider rejects one.
    """
    root = Path(history_prefix).parent
    for attr, value in (
        ("_history_path_prefix", history_prefix),
        ("_media_prefix", f"{history_prefix}/media"),
        ("_large_tool_results_prefix", str(root / "large_tool_results")),
    ):
        if hasattr(mw, attr):
            setattr(mw, attr, value)


def _ensure_summarization_offload_patch() -> None:
    """Repoint deepagents' conversation-history offload to a writable workspace path.

    deepagents' ``SummarizationMiddleware`` offloads evicted (pre-summary) messages
    to ``<artifacts_root>/conversation_history/<thread>.md``; for a plain
    filesystem / local_shell backend ``artifacts_root`` is ``"/"``, so with
    ``virtual_mode=False`` the absolute path resolves to the *host filesystem root*,
    which is not writable — the offload silently fails and older detail becomes
    unrecoverable to the agent. deepagents exposes no supported per-call knob for
    this (still true in 0.7.23: the ``history_path_prefix`` kwarg is removed and
    the only lever is ``CompositeBackend.artifacts_root``, which would change the
    backend ``type`` and break olano's shell detection), so we wrap the factory
    ``create_deep_agent`` calls internally and repoint the prefixes under the
    backend's own ``root_dir`` (the workspace). The wrapper is also where olano's
    keep/trigger policy replaces deepagents' profile-less fallback (keep the last
    6 messages), which 0.7.23 still has. The directory is created lazily by the
    backend on first write.

    Best-effort and version-guarded: any failure leaves deepagents' default
    behavior untouched. Installed once per process; the wrapper reads each call's
    backend so the path stays correct per agent.
    """
    global _SUMMARIZATION_OFFLOAD_PATCHED
    # Locked, not just flag-guarded: builds run in worker threads now, and the
    # bare flag is set *before* the patch is installed — so a second build could
    # sail past it and call create_deep_agent against the unpatched factory.
    with _BUILD_LOCK:
        if _SUMMARIZATION_OFFLOAD_PATCHED:
            return
        _SUMMARIZATION_OFFLOAD_PATCHED = True  # set first so a failure doesn't retry every build
        _install_summarization_offload_patch()


def _install_summarization_offload_patch() -> None:
    try:
        import deepagents.graph as _dg_graph

        _orig = _dg_graph.create_summarization_middleware
    except Exception as exc:  # pragma: no cover - deepagents internals moved
        logger.debug("summarization offload patch unavailable: %s", exc)
        return

    def _patched(model: Any, backend: Any) -> Any:
        # Every summarization middleware deepagents builds for the graph being
        # created right now (main agent, general-purpose subagent, each inline
        # subagent) takes olano's keep/trigger policy instead of deepagents'
        # defaults — `_create_agent_graph` binds the policy for the duration
        # of `create_deep_agent`. Outside a build (or if anything goes wrong)
        # the original factory answers, so a patch failure is never a build
        # failure.
        active = _ACTIVE_CONTEXT_POLICY.get()
        if active is not None:
            try:
                return _make_summarization_middleware(
                    model,
                    backend,
                    policy=active["policy"],
                    offload_root=active.get("offload_root"),
                )
            except Exception as exc:  # pragma: no cover - never break agent build
                logger.warning("context-window policy skipped for this graph: %s", exc)
        mw = _orig(model, backend)
        try:
            prefix = _workspace_offload_prefix(backend)
            if prefix and hasattr(mw, "_history_path_prefix"):
                _repoint_summarization_offload(mw, prefix)
        except Exception as exc:  # pragma: no cover - never break agent build
            logger.debug("summarization offload repoint skipped: %s", exc)
        return mw

    # deepagents 0.7 types this module fn precisely; the wrapper keeps the same
    # runtime contract (model, backend) -> summarization middleware.
    _dg_graph.create_summarization_middleware = _patched  # type: ignore[assignment]


# The context-window policy of the graph being built on this thread/task, read
# by the two deepagents patches above and below. Set only inside
# `_create_agent_graph`; None everywhere else.
_ACTIVE_CONTEXT_POLICY: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "olano_active_context_policy", default=None
)


def _model_max_input_tokens(model: Any) -> int | None:
    """``model.profile["max_input_tokens"]`` when the client (or olano's catalog
    profile, see ``models.attach_context_profile``) knows it; else ``None``."""
    try:
        profile = model.profile
    except Exception:
        return None
    if not isinstance(profile, dict):
        return None
    value = profile.get("max_input_tokens")
    return value if isinstance(value, int) and value > 0 else None


def _make_summarization_middleware(
    model: Any, backend: Any, *, policy: Any, offload_root: Path | None
) -> Any:
    """A deepagents ``SummarizationMiddleware`` under olano's context policy.

    deepagents' factory keeps 10% of the window after a compaction when the
    model has a profile and **6 messages** when it does not; the second case
    is every model newer than the installed provider package, and 6 messages
    of a tool-heavy session is one exchange. ``policy`` (the resolved
    ``context_window`` block) supplies the trigger and keep instead:
    fractions of the window when ``max_input_tokens`` is known, absolute
    tokens otherwise — never a message count. Argument truncation and
    ``trim_tokens_to_summarize`` keep the factory's defaults. The evicted history lands under
    ``<workspace>/.olano/conversation_history/`` (deepagents' default is
    the host root, which is not writable).
    """
    from deepagents.middleware.summarization import (
        SummarizationMiddleware,
        compute_summarization_defaults,
    )

    defaults = compute_summarization_defaults(model)
    trigger, keep = policy.context_sizes(_model_max_input_tokens(model))
    mw = SummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=trigger,
        keep=keep,
        truncate_args_settings=defaults["truncate_args_settings"],
        # deepagents' factory passes None (summarize everything being evicted);
        # the class's own default is 4000, which summarized only the last 4k
        # tokens of the older history. olano builds the class directly because
        # the factory takes no trigger/keep, so it has to say this explicitly.
        # A summary call that overflows is retried trimmed upstream, and the
        # full evicted history is offloaded to a file either way.
        trim_tokens_to_summarize=None,
    )
    prefix = _workspace_offload_prefix(backend)
    if prefix is None and offload_root is not None and not _is_virtual_backend(backend):
        prefix = str(Path(offload_root) / "conversation_history")
    if prefix and hasattr(mw, "_history_path_prefix"):
        _repoint_summarization_offload(mw, prefix)
    return mw


def _is_virtual_backend(backend: Any) -> bool:
    """True for backends whose paths are their own namespace (StateBackend,
    CompositeBackend, a virtual-mode filesystem): an absolute host path means
    nothing to them, so an offload prefix must not be forced onto one."""
    if getattr(backend, "virtual_mode", None) is True:
        return True
    return type(backend).__name__ in {"StateBackend", "CompositeBackend"}


_SKILLS_HOST_SCAN_PATCHED = False

#: ``LIONCLAW_SKILLS_HOST_SCAN=0`` keeps deepagents scanning skill sources through
#: the sandbox (one ``docker exec`` per SKILL.md) instead of the host filesystem.
_SKILLS_HOST_SCAN_ENV = "LIONCLAW_SKILLS_HOST_SCAN"


def _skills_scan_backend(backend: Any) -> Any:
    """The backend deepagents' ``SkillsMiddleware`` reads SKILL.md files through.

    For an Olano docker sandbox that is the HOST filesystem; for every other
    backend it is ``backend`` itself. The skill roots a sandboxed agent sees
    are host directories mounted into the container at their host paths
    (``_resolve_sandbox_shared_mounts``), so a host read lists the same
    directories and yields the same absolute SKILL.md paths the sandbox scan
    advertised, and the agent's own ``read_file`` of a skill still goes
    through the sandbox as before. What changes is the cost: the sandbox scan
    was one ``ls`` per source plus one ``docker exec`` per skill, paid on the
    first turn of every thread (deepagents loads ``skills_metadata`` once per
    thread), measured at 110-250 ms per exec and 7-17 s per first turn on a
    box with 38 skills.
    """
    if os.environ.get(_SKILLS_HOST_SCAN_ENV, "1").strip().lower() in ("0", "false", "no", "off"):
        return backend
    try:
        from olano.backends.lionclaw_docker import OlanoDockerBackend
    except Exception:  # pragma: no cover - docker backend not importable here
        return backend
    if not isinstance(backend, OlanoDockerBackend):
        return backend
    from deepagents.backends import FilesystemBackend

    return FilesystemBackend(root_dir="/", virtual_mode=False)


def _ensure_skills_host_scan_patch() -> None:
    """Make deepagents' ``SkillsMiddleware`` scan skill sources on the host for
    sandboxed agents (see :func:`_skills_scan_backend`).

    deepagents constructs the middleware inside ``create_deep_agent`` as
    ``SkillsMiddleware(backend=backend, sources=skills)`` with the agent's
    main backend, for the main agent and every subagent, so the seam is the
    class bound in ``deepagents.graph``: it is replaced with a subclass of the
    same name (the middleware slot deepagents matches by ``name``, which is the
    class name) whose constructor swaps the backend. Locked and one-shot like
    its two siblings above; a missing target logs and leaves the sandbox scan
    in place, so the patch can never fail a build.
    """
    global _SKILLS_HOST_SCAN_PATCHED
    with _BUILD_LOCK:
        if _SKILLS_HOST_SCAN_PATCHED:
            return
        _SKILLS_HOST_SCAN_PATCHED = True
        _install_skills_host_scan_patch()


def _install_skills_host_scan_patch() -> None:
    try:
        import deepagents.graph as _dg_graph
        from deepagents.middleware.skills import SkillsMiddleware as _BaseSkillsMiddleware
        from langchain_core.runnables import RunnableConfig
        from langgraph.runtime import Runtime

        if _dg_graph.SkillsMiddleware is not _BaseSkillsMiddleware:
            raise RuntimeError(
                "deepagents.graph.SkillsMiddleware is not the skills middleware class"
            )
    except Exception as exc:  # pragma: no cover - deepagents internals moved
        logger.warning("skills host-scan patch unavailable (sandbox scan kept): %s", exc)
        return

    class SkillsMiddleware(_BaseSkillsMiddleware):  # type: ignore[misc]
        """deepagents' skills middleware, reading skill sources on the host for
        a sandboxed agent, and reading them once per process rather than once
        per thread. Same class name on purpose: ``AgentMiddleware.name`` is the
        class name and deepagents matches the skills slot by it."""

        _olano_host_scan = True

        def __init__(self, *, backend: Any, sources: Any, **kwargs: Any) -> None:
            super().__init__(backend=_skills_scan_backend(backend), sources=sources, **kwargs)

        # deepagents' hooks take (state, runtime, config). The annotations are
        # the contract, not decoration: langchain inspects them and hands a
        # hook its `config` only when the parameter is typed `RunnableConfig`;
        # typed `Any`, the call arrives without it and the first turn of
        # every thread dies with "missing 1 required positional argument".
        def before_agent(  # type: ignore[override]
            self, state: Any, runtime: Runtime, config: RunnableConfig
        ) -> Any:
            if state.get("skills_metadata") is not None:
                return None
            fingerprint = _skills_sources_fingerprint(self)
            cached = _cached_skills_update(self, fingerprint)
            if cached is not None:
                return cached
            update = super().before_agent(state, runtime, config)
            _remember_skills_update(self, fingerprint, update)
            return update

        async def abefore_agent(  # type: ignore[override]
            self, state: Any, runtime: Runtime, config: RunnableConfig
        ) -> Any:
            if state.get("skills_metadata") is not None:
                return None
            fingerprint = _skills_sources_fingerprint(self)
            cached = _cached_skills_update(self, fingerprint)
            if cached is not None:
                return cached
            update = await super().abefore_agent(state, runtime, config)
            _remember_skills_update(self, fingerprint, update)
            return update

    _dg_graph.SkillsMiddleware = SkillsMiddleware  # type: ignore[assignment,misc]


#: ``LIONCLAW_SKILLS_LIST_CACHE=0`` reads every SKILL.md again at the start of
#: every thread (deepagents' own behaviour) instead of once per process.
_SKILLS_LIST_CACHE_ENV = "LIONCLAW_SKILLS_LIST_CACHE"
# sources key -> (fingerprint of the files on disk, the loaded state update)
_SKILLS_LIST_CACHE: dict[tuple[str, ...], tuple[tuple[Any, ...], dict[str, Any]]] = {}
_SKILLS_LIST_LOCK = threading.Lock()


def _skills_source_paths(sources: Any) -> tuple[str, ...]:
    """The directory of each deepagents skill source (a path or a ``(path, label)`` pair)."""
    out: list[str] = []
    for source in sources or ():
        path = source[0] if isinstance(source, tuple | list) else source
        out.append(str(path))
    return tuple(out)


def _skills_sources_fingerprint(middleware: Any) -> tuple[Any, ...] | None:
    """What the skill list depends on: every source directory and the
    ``(name, mtime_ns, size)`` of each ``<dir>/<skill>/SKILL.md`` in it.

    One ``stat`` per skill, microseconds in all, so comparing it on every new
    thread is what lets the files themselves be read only when one changed:
    an installed, edited or removed skill moves the fingerprint, and the next
    thread reads the sources again. ``None`` when the list cannot be cached:
    the opt-out, or a middleware reading something other than the host
    filesystem (a remote sandbox), whose files these stats cannot see.
    """
    if os.environ.get(_SKILLS_LIST_CACHE_ENV, "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    try:
        from deepagents.backends import FilesystemBackend

        backend = middleware._backend
        if not isinstance(backend, FilesystemBackend) or getattr(backend, "virtual_mode", True):
            return None
    except Exception:
        return None
    parts: list[Any] = []
    for directory in _skills_source_paths(getattr(middleware, "sources", ())):
        try:
            entries = []
            with os.scandir(directory) as it:
                for entry in it:
                    if not entry.is_dir():
                        continue
                    try:
                        st = (Path(entry.path) / "SKILL.md").stat()
                    except OSError:
                        continue
                    entries.append((entry.name, st.st_mtime_ns, st.st_size))
            parts.append((directory, tuple(sorted(entries))))
        except OSError:
            parts.append((directory, None))
    return tuple(parts)


def _cached_skills_update(
    middleware: Any, fingerprint: tuple[Any, ...] | None
) -> dict[str, Any] | None:
    """The process-wide skill list for this middleware's sources, when the
    files it was read from still match ``fingerprint``; else ``None`` (read
    them). The fingerprint is taken BEFORE the read and stored with its
    result, so a file edited during the read is seen by the next thread."""
    if fingerprint is None:
        return None
    key = _skills_source_paths(getattr(middleware, "sources", ()))
    with _SKILLS_LIST_LOCK:
        hit = _SKILLS_LIST_CACHE.get(key)
    if hit is None or hit[0] != fingerprint:
        return None
    update = hit[1]
    # Fresh containers per thread: downstream middlewares build new lists from
    # this one, but nothing must be able to edit the cached copy in place.
    return {k: (list(v) if isinstance(v, list) else v) for k, v in update.items()}


def _remember_skills_update(
    middleware: Any, fingerprint: tuple[Any, ...] | None, update: Any
) -> None:
    if fingerprint is None or not isinstance(update, dict):
        return
    if update.get("skills_metadata") is None:
        return
    key = _skills_source_paths(getattr(middleware, "sources", ()))
    kept = {k: (list(v) if isinstance(v, list) else v) for k, v in update.items()}
    with _SKILLS_LIST_LOCK:
        _SKILLS_LIST_CACHE[key] = (fingerprint, kept)


def _reset_skills_list_cache() -> None:
    with _SKILLS_LIST_LOCK:
        _SKILLS_LIST_CACHE.clear()


_FILESYSTEM_OFFLOAD_PATCHED = False


def _ensure_filesystem_offload_patch() -> None:
    """Repoint deepagents' large-tool-result offload to a writable workspace path.

    ``FilesystemMiddleware`` evicts any tool result over
    ``tool_token_limit_before_evict`` (20k tokens) to
    ``<artifacts_root>/large_tool_results/<tool_call_id>`` — and for every
    backend but a ``CompositeBackend`` that root is ``"/"``, so on a
    filesystem-backed agent it tried to write ``/large_tool_results/…`` on the
    host, the write failed, and the result stayed inline. Same defect, same
    cure as ``_ensure_summarization_offload_patch``: wrap the constructor and
    move the prefix under ``<workspace>/.olano/`` when a build is active.
    Locked and one-shot for the same reason as its sibling.
    """
    global _FILESYSTEM_OFFLOAD_PATCHED
    with _BUILD_LOCK:
        if _FILESYSTEM_OFFLOAD_PATCHED:
            return
        _FILESYSTEM_OFFLOAD_PATCHED = True
        _install_filesystem_offload_patch()


def _install_filesystem_offload_patch() -> None:
    try:
        from deepagents.middleware.filesystem import FilesystemMiddleware

        _orig_init = FilesystemMiddleware.__init__
    except Exception as exc:  # pragma: no cover - deepagents internals moved
        logger.debug("filesystem offload patch unavailable: %s", exc)
        return

    def _patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        _orig_init(self, *args, **kwargs)
        try:
            backend = getattr(self, "backend", None)
            active = _ACTIVE_CONTEXT_POLICY.get()
            root = active.get("offload_root") if active else None
            if root is None or backend is None or _is_virtual_backend(backend):
                return
            # `_blobs_prefix` (0.7.23) is where `offload_binary_content=True` keeps
            # binary read_file results; olano leaves that off, but the prefix comes
            # from the same "/" root and would fail the same way if turned on.
            for attr, leaf in (
                ("_large_tool_results_prefix", "large_tool_results"),
                ("_conversation_history_prefix", "conversation_history"),
                ("_blobs_prefix", "blobs"),
            ):
                if hasattr(self, attr):
                    setattr(self, attr, str(Path(root) / leaf))
        except Exception as exc:  # pragma: no cover - never break agent build
            logger.debug("filesystem offload repoint skipped: %s", exc)

    FilesystemMiddleware.__init__ = _patched_init  # type: ignore[method-assign]


_DEEPAGENTS_PROVIDER_IMPORTS_DEFERRED = False

# Provider packages deepagents imports at module level for reasons that never
# need the package loaded up front. Since 0.7.23 that is ``langchain_openai``
# alone, imported (guarded by ``try/except ImportError``) by two middleware
# modules purely to hold its chat classes in a module constant:
# ``middleware/unsupported_content.py`` (an ``isinstance`` tuple deciding whether
# a non-PDF document block may reach an OpenAI Responses model) and
# ``middleware/_skill_tools.py`` (an exact-type check deciding whether a skill's
# tools can be disclosed inline). Masking it for the duration of the deepagents
# import takes the guarded branch, which leaves both constants empty;
# ``_fill_deferred_openai_types`` puts the real classes back at call time.
# (``langchain_google_genai`` was the second entry through 0.7.11; deepagents no
# longer imports it.)
_DEFERRED_PROVIDER_MODULES: tuple[str, ...] = ("langchain_openai",)

# Prompt-caching factories deepagents calls on every ``create_deep_agent`` that
# import a provider package to build a middleware which is a declared no-op
# (``unsupported_model_behavior="ignore"``) for every other provider. Each is
# gated on its package already being loaded, i.e. on a model of that provider
# having been built on this process.
_GATED_PROMPT_CACHING_FACTORIES: dict[str, str] = {
    "_create_bedrock_prompt_caching_middleware": "langchain_aws",
    "_create_fireworks_prompt_caching_middleware": "langchain_fireworks",
}


def _fill_deferred_openai_types() -> None:
    """Give deepagents the ``langchain_openai`` classes the masked import withheld.

    An instance of a ``langchain_openai`` class can only exist once that package
    is in ``sys.modules``, so the package is looked up there, never imported: a
    package that is not loaded cannot have produced the model being asked about,
    and an empty constant then gives the same answer the real classes would.
    Once filled, a constant stays filled. The constants are rebuilt exactly as
    deepagents builds them, so its own predicates (``isinstance`` with
    subclasses in one module, exact ``type(...) is`` in the other) decide.
    """
    module = sys.modules.get("langchain_openai")
    if module is None:
        return
    chat = getattr(module, "ChatOpenAI", None)
    azure = getattr(module, "AzureChatOpenAI", None)
    if not isinstance(chat, type):
        return
    uc = sys.modules.get("deepagents.middleware.unsupported_content")
    if uc is not None and not getattr(uc, "_OPENAI_FILE_MODEL_TYPES", ()):
        uc._OPENAI_FILE_MODEL_TYPES = tuple(t for t in (azure, chat) if isinstance(t, type))  # type: ignore[attr-defined]
    st = sys.modules.get("deepagents.middleware._skill_tools")
    if st is not None and getattr(st, "_CHAT_OPENAI_TYPE", None) is None:
        st._CHAT_OPENAI_TYPE = chat  # type: ignore[attr-defined]


def _filling_openai_types(fn: Any) -> Any:
    """Wrap a deepagents function that reads an OpenAI type constant so the
    constant is filled (``_fill_deferred_openai_types``) before it is read."""

    @functools.wraps(fn)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        _fill_deferred_openai_types()
        return fn(*args, **kwargs)

    _wrapper._olano_fills_openai_types = True  # type: ignore[attr-defined]
    return _wrapper


def _ensure_deepagents_provider_imports_deferred() -> None:
    """Import deepagents without dragging in every provider SDK it can name.

    Measured on a cold process (``scripts/bench_agent_memory.py
    --attribute-imports``): the first agent build cost ~141 MB of RSS on
    deepagents 0.7.11, and ~60 MB of that was provider packages the agent's
    *model does not use*. On 0.7.23 the module-level offender is
    ``langchain_openai`` (+ the ``openai`` SDK); every ``create_deep_agent``
    additionally tries ``langchain_aws`` (+ ``botocore``) and
    ``langchain_fireworks`` (+ the ``fireworks`` SDK, ~250 modules, ~0.5 s) for
    prompt-caching middleware that no-ops on every other provider. An agent on
    the fake model, on Anthropic, or on the olano gateway paid for all three.

    Three moves, all behaviour-preserving:

    1. Mask ``langchain_openai`` in ``sys.modules`` (a ``None`` entry makes
       ``import`` raise ``ModuleNotFoundError``, the documented CPython
       contract) for the duration of the first deepagents import, so the two
       guarded imports take their ``ImportError`` branch. The entry is removed
       again immediately, so a later genuine ``import langchain_openai`` (an
       OpenAI agent's model build) proceeds exactly as before; Python does not
       cache a failed import. Skipped when the package is already loaded:
       nothing to save there, and the constants are then already right.
    2. Wrap the two functions that read the emptied constants so they fill them
       from ``sys.modules`` first (``_fill_deferred_openai_types``). An OpenAI
       agent therefore still gets its non-PDF document pass-through and its
       inline skill-tool disclosure: by the time its model is asked about, its
       package is loaded. ``skills.py`` imported ``_inline_block_builder`` by
       name, so that binding is replaced too.
    3. Gate the Bedrock and Fireworks prompt-caching factories on their package
       being in ``sys.modules`` (``_GATED_PROMPT_CACHING_FACTORIES``), i.e. on a
       model of that provider having actually been built on this process
       (``_warm_model_provider`` loads the provider package before
       ``create_deep_agent``). deepagents already treats a ``None`` from either
       factory as "not installed". The Fireworks gate replaces a patch that
       disabled that factory outright: its import used to register a vendored
       ``google/api/field_behavior.proto`` that crashed the build beside the
       langchain-google providers, which the installed ``fireworks-ai`` no
       longer ships. ``langchain-fireworks`` 1.7 ships the middleware, so a
       Fireworks agent now gets prompt caching where the old patch denied it.

    ``anthropic`` (17 MB) stays: ``deepagents/graph.py`` and ``memory.py`` import
    ``langchain_anthropic`` unguarded, so it cannot be deferred from here.

    Best-effort and version-guarded: a failure leaves deepagents untouched, and
    a patch target that no longer exists is logged as a **warning**, not
    skipped quietly. Assigning to a removed module attribute succeeds, which is
    how the 0.7.11 predicate patch went on "working" against a function 0.7.23
    no longer has; ``tests/test_provider_import_deferral.py`` pins every target.
    Applied once per process, before the first deepagents import.
    """
    global _DEEPAGENTS_PROVIDER_IMPORTS_DEFERRED
    with _BUILD_LOCK:
        if _DEEPAGENTS_PROVIDER_IMPORTS_DEFERRED:
            return
        _DEEPAGENTS_PROVIDER_IMPORTS_DEFERRED = True  # set first so a failure doesn't retry forever

        masked = [name for name in _DEFERRED_PROVIDER_MODULES if name not in sys.modules]
        for name in masked:
            sys.modules[name] = None  # type: ignore[assignment]
        try:
            import deepagents.middleware._prompt_caching as _pc
            import deepagents.middleware._skill_tools as _st
            import deepagents.middleware.skills as _skills
            import deepagents.middleware.unsupported_content as _uc
        except Exception as exc:  # pragma: no cover - deepagents internals moved
            logger.warning("deepagents provider-import deferral unavailable: %s", exc)
            return
        finally:
            # Unmask whatever we masked and nothing else: a package that became
            # genuinely loaded inside the window (it cannot, but never assume)
            # would not be ``None`` and is left alone.
            for name in masked:
                if sys.modules.get(name, False) is None:
                    del sys.modules[name]

        def _missing(module: ModuleType, *names: str) -> bool:
            gone = [n for n in names if not hasattr(module, n)]
            if gone:
                logger.warning(
                    "deepagents provider-import deferral: %s no longer defines %s; "
                    "that part of the patch is skipped",
                    module.__name__,
                    ", ".join(gone),
                )
            return bool(gone)

        try:
            if not _missing(_uc, "_OPENAI_FILE_MODEL_TYPES", "_openai_responses_accepts"):
                _uc._openai_responses_accepts = _filling_openai_types(_uc._openai_responses_accepts)
            if not _missing(_st, "_CHAT_OPENAI_TYPE", "_inline_block_builder") and not _missing(
                _skills, "_inline_block_builder"
            ):
                wrapped = _filling_openai_types(_st._inline_block_builder)
                _st._inline_block_builder = wrapped
                _skills._inline_block_builder = wrapped
            # The constants are already right when the package was loaded before
            # the deepagents import; this fills them when it was masked but has
            # since been imported.
            _fill_deferred_openai_types()
        except Exception as exc:  # pragma: no cover
            logger.warning("deepagents OpenAI type deferral unavailable: %s", exc)

        for factory_name, package in _GATED_PROMPT_CACHING_FACTORIES.items():
            try:
                if _missing(_pc, factory_name):
                    continue
                eager_name = f"{factory_name}_eager"
                setattr(_pc, eager_name, getattr(_pc, factory_name))

                def _when_loaded(_package: str = package, _eager_name: str = eager_name) -> Any:
                    if _package not in sys.modules:
                        return None
                    # Looked up at call time, not captured, so a test can pin
                    # "delegates once the package is loaded" by swapping the eager one.
                    return getattr(_pc, _eager_name)()

                setattr(_pc, factory_name, _when_loaded)
            except Exception as exc:  # pragma: no cover
                logger.warning("deepagents %s gate unavailable: %s", factory_name, exc)


def _build_compact_tool_middleware(
    model: Any,
    backend: Any,
    *,
    policy: Any | None = None,
    offload_root: Path | None = None,
) -> Any | None:
    """Build deepagents' ``compact_conversation`` tool middleware for an agent.

    deepagents installs an *automatic* ``SummarizationMiddleware`` in its base stack
    (model-aware, fires near the model's real context limit). That handles unbounded
    growth. This adds the companion ``SummarizationToolMiddleware`` so the agent can
    compact *on demand* — used by the ``/compact`` endpoint (via
    ``AgentRunner.compact_thread``) and available to the model proactively. The tool
    never auto-compacts; it only summarizes when invoked.

    Its engine is built by ``_make_summarization_middleware`` under the same
    ``context_window`` policy as the automatic one (the two share the
    ``_summarization_event`` state key, so they must agree on what "keep"
    means), with the offload path under the workspace. Best-effort: any
    failure returns ``None`` and the agent simply lacks the tool.
    """
    try:
        from deepagents.middleware.summarization import SummarizationToolMiddleware

        if policy is None:
            from olano.config.system_settings import effective_context_window

            policy = effective_context_window(None)
        summ = _make_summarization_middleware(
            model, backend, policy=policy, offload_root=offload_root
        )
        return SummarizationToolMiddleware(summ)
    except Exception as exc:  # pragma: no cover - never break agent build
        logger.debug("compact_conversation tool middleware unavailable: %s", exc)
        return None


def _create_agent_graph(
    *,
    model: Any,
    tools: list[Any],
    system_prompt: str,
    subagents: list[Any],
    skill_dirs: list[str],
    checkpointer: Any,
    backend: Any,
    permissions: list[Any] | None,
    middleware: list[Any] | None,
    interrupt_on: dict[str, bool | dict[str, Any]] | None,
    store: Any | None = None,
    context_policy: dict[str, Any] | None = None,
) -> Any:
    _ensure_summarization_offload_patch()
    _ensure_filesystem_offload_patch()
    _ensure_skills_host_scan_patch()
    from deepagents import create_deep_agent

    guarded = guard_tools(tools)
    kwargs: dict[str, Any] = dict(
        model=model,
        tools=guarded if guarded else None,
        system_prompt=system_prompt,
        middleware=middleware or (),
        subagents=subagents if subagents else None,
        skills=skill_dirs if skill_dirs else None,
        checkpointer=checkpointer,
        backend=backend,
        interrupt_on=interrupt_on,
    )
    if permissions is not None:
        kwargs["permissions"] = permissions
    # A StoreBackend ("Virtual File System (Persistent)") requires a store=.
    if store is not None:
        kwargs["store"] = store
    # Bind the context-window policy for every summarization / filesystem
    # middleware deepagents constructs inside this call (main agent and each
    # subagent), then unbind so a later build — or a graph deepagents builds
    # for a `delegate_task` worker — never inherits it by accident.
    token = _ACTIVE_CONTEXT_POLICY.set(context_policy)
    try:
        return create_deep_agent(**kwargs)
    finally:
        _ACTIVE_CONTEXT_POLICY.reset(token)


# Module-level store for loaded middleware hooks per agent
_MIDDLEWARE_HOOKS: dict[str, Any] = {}


def get_middleware_hooks(agent_name: str) -> Any:
    """Return loaded middleware hooks for an agent, or None."""
    return _MIDDLEWARE_HOOKS.get(agent_name)


def _build_system_prompt(
    config: AgentConfig, workspace: Any, *, mcp_summary: list[dict[str, Any]] | None = None
) -> str:
    """Build the full system prompt using PromptAssembler (L0 + L1) with PULSE."""
    try:
        from olano.core.prompt_assembler import PromptAssembler
        from olano.pulse.engine import PulseEngine

        pulse_engine = PulseEngine(config, workspace.state_dir)
        pulse_items = [i.to_dict() for i in pulse_engine.get_pending(max_items=3)]

        assembler = PromptAssembler(config, workspace, mcp_summary=mcp_summary)
        return cast(str, assembler.build(pulse_items=pulse_items if pulse_items else None))
    except Exception:
        # Graceful fallback to legacy path
        return cast(str, workspace.build_system_prompt_context(config.system_prompt))


def _run_cartography_incremental(workspace: Any) -> None:
    """Run incremental workspace cartography (no LLM calls)."""
    try:
        from olano.cartography.cartographer import WorkspaceCartographer

        cart = WorkspaceCartographer(workspace)
        cart.run_incremental()
    except Exception:
        pass  # Cartography is non-critical


# One cartography walk at a time process-wide: a runtime boots every agent
# within seconds, and N concurrent tree walks on cold cloud block storage
# multiply seek pressure for a background nicety nobody is waiting on.
_CARTOGRAPHY_GATE = threading.Semaphore(1)
_CARTOGRAPHY_LOCK = threading.Lock()
_CARTOGRAPHY_INFLIGHT: set[str] = set()


def _spawn_cartography_refresh(workspace: Any) -> None:
    """Refresh the workspace map/index OFF the boot path.

    The boot only *reads* cartography's output: PromptAssembler loads
    `workspace_map.md` from disk, so the previous run's map serves the first
    prompt and the refresh lands moments later. Running the walk inline was the
    single biggest boot cost measured — 130.9s of a 136.0s agent boot against
    an 11 GB workspace of cloned repos (see cartography/walker.py, which also
    makes the walk itself cheap).

    Same pattern as `_spawn_workspace_ingest`: niced daemon thread, opt back
    into inline execution with ``LIONCLAW_SYNC_CARTOGRAPHY=1`` (tests, CLI
    one-shots that exit immediately). Deduplicated per workspace root so a
    fleet boot plus a single-agent restart can't queue the same walk twice.
    """
    if os.environ.get("LIONCLAW_SYNC_CARTOGRAPHY") == "1":
        _run_cartography_incremental(workspace)
        return

    key = str(workspace.root)
    with _CARTOGRAPHY_LOCK:
        if key in _CARTOGRAPHY_INFLIGHT:
            return
        _CARTOGRAPHY_INFLIGHT.add(key)

    def _run() -> None:
        try:
            try:
                os.nice(10)
            except (AttributeError, OSError):  # pragma: no cover - platform dependent
                pass
            with _CARTOGRAPHY_GATE:
                _run_cartography_incremental(workspace)
        finally:
            with _CARTOGRAPHY_LOCK:
                _CARTOGRAPHY_INFLIGHT.discard(key)

    threading.Thread(
        target=_run,
        name=f"cartography-{workspace.root.name}",
        daemon=True,
    ).start()


async def build_agent_with_mcp(
    config: AgentConfig, *, system_prompt_override: str | None = None
) -> Any:
    """Build agent with async MCP tool loading and async SQLite checkpointing."""
    return await build_agent_async(
        config, include_mcp=True, system_prompt_override=system_prompt_override
    )


def build_agent(config: AgentConfig) -> Any:
    """
    Build and return a compiled LangGraph agent from an AgentConfig.

    Uses a shared per-home SQLite checkpointer for sync execution paths.
    Thread state persists across processes in <home>/sessions/threads.db.
    """
    prepared = _prepare_agent_build(config)
    checkpointer = get_checkpointer(config.name)
    store = get_store() if _backend_uses_store(config.backend) else None
    return _create_agent_graph(
        model=prepared["model"],
        tools=prepared["extra_tools"],
        system_prompt=prepared["system_prompt"],
        subagents=prepared["subagents"],
        skill_dirs=prepared["skill_dirs"],
        checkpointer=checkpointer,
        backend=prepared["backend"],
        permissions=prepared["permissions"],
        middleware=prepared["middleware"],
        interrupt_on=_build_interrupt_on(config),
        store=store,
        context_policy=prepared.get("context_policy"),
    )


async def build_agent_async(
    config: AgentConfig,
    include_mcp: bool = False,
    *,
    system_prompt_override: str | None = None,
) -> Any:
    """
    Build and return a compiled LangGraph agent for async execution paths.

    Uses the home's checkpointer (see ``core/checkpoints.py``) so the runtime,
    one-shot CLI runs, ACP, and resumed threads persist state across process
    restarts.

    ``system_prompt_override`` replaces the whole assembled prompt (L0 + L1)
    with the given text. Only ``olano_test_agent``'s ``full`` mode passes it, for a
    test copy of the agent; every production build leaves it ``None``.

    The blocking work runs in worker threads so the event loop keeps serving
    while agents boot — see ``_BUILD_LOCK``. The checkpointer and store are
    opened on the loop first (both bind to the loop that creates them) and only
    then handed to the graph compiler, which merely stores them.
    """
    mcp_tools: list[Any] = []
    mcp_summary: list[dict[str, Any]] | None = None
    if include_mcp and config.mcp_servers:
        from olano.security.pipeline import mark_tools_untrusted

        # Connect MCP first so the assembled system prompt can truthfully tell the
        # agent which MCP tools are live this session.
        with boot_phase("mcp"):
            raw_tools, mcp_summary = await _build_mcp_tools(config)
        mcp_tools = mark_tools_untrusted(raw_tools)
    prepared = await _prepare_agent_build_async(config, mcp_summary=mcp_summary)
    with boot_phase("checkpointer"):
        checkpointer = await get_async_checkpointer(config.name)
        store = await get_async_store() if _backend_uses_store(config.backend) else None
    with boot_phase("graph"):
        return await asyncio.to_thread(
            _create_agent_graph,
            model=prepared["model"],
            tools=prepared["extra_tools"] + mcp_tools,
            system_prompt=(
                system_prompt_override
                if system_prompt_override is not None
                else prepared["system_prompt"]
            ),
            subagents=prepared["subagents"],
            skill_dirs=prepared["skill_dirs"],
            checkpointer=checkpointer,
            backend=prepared["backend"],
            permissions=prepared["permissions"],
            middleware=prepared["middleware"],
            interrupt_on=_build_interrupt_on(config),
            store=store,
            context_policy=prepared.get("context_policy"),
        )
