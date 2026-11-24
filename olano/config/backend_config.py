"""Backend configuration models for Olano agents.

Each submodel maps 1:1 to a deepagents backend class.
The only custom backend is OlanoDockerBackend (type: lionclaw_docker).
"""

from __future__ import annotations

import os
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ─────────────────────── sandbox resource-limit primitives ───────────────────
# Shared by every surface that accepts a limit — the backend block here, the
# per-agent ``sandbox:`` override in agent.json5, the ``sandbox:`` block in
# config.yaml and the dashboard's PUT validation — so "what is a valid value"
# has exactly one definition.

# Docker's own --memory grammar (go-units RAMInBytes): digits with an optional
# b/k/m/g suffix, case-insensitive. A bare number is bytes.
SANDBOX_MEMORY_RE = re.compile(r"^\d+[bkmg]?$", re.IGNORECASE)
_MEMORY_UNIT_BYTES = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
# Docker refuses --memory below 6 MiB outright, so accepting less here would
# only move the failure to `docker run`, where it reads as a runtime crash.
MIN_SANDBOX_MEMORY_BYTES = 6 * 1024**2
# 0.1 is docker's own floor for --cpus; the ceiling is a sanity bound well
# above any machine olano runs on (a typo of 640 should not silently pass).
SANDBOX_CPUS_MIN = 0.1
SANDBOX_CPUS_MAX = 64.0


def parse_mem_size(value: str) -> int | None:
    """Bytes for a docker size string, or ``None`` when it is not one."""
    text = str(value).strip()
    if not SANDBOX_MEMORY_RE.match(text):
        return None
    suffix = text[-1].lower()
    if suffix.isdigit():
        return int(text)
    return int(text[:-1]) * _MEMORY_UNIT_BYTES[suffix]


def format_mib(num_bytes: int) -> str:
    """Render bytes as docker's ``<n>m`` — whole MiB, so limits stay stable.

    Auto-sized values feed the container signature, so they must round to the
    same string on every boot; whole MiB is the coarsest unit that still
    expresses every limit we care about.
    """
    return f"{max(1, num_bytes // 1024**2)}m"


def normalize_sandbox_memory(value: Any) -> str:
    """Validate + canonicalize a memory limit, raising on anything unusable."""
    text = str(value).strip().lower()
    parsed = parse_mem_size(text)
    if parsed is None:
        raise ValueError(
            f"invalid memory limit {value!r}: expected a docker size string like "
            "'2g', '2048m' or a byte count"
        )
    if parsed < MIN_SANDBOX_MEMORY_BYTES:
        raise ValueError(
            f"memory limit {value!r} is below docker's {MIN_SANDBOX_MEMORY_BYTES // 1024**2}m minimum"
        )
    return text


def normalize_sandbox_cpus(value: Any) -> float:
    """Validate a CPU limit, raising on a non-number or an out-of-range one."""
    try:
        cpus = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid cpu limit {value!r}: expected a number") from exc
    if not SANDBOX_CPUS_MIN <= cpus <= SANDBOX_CPUS_MAX:
        raise ValueError(
            f"cpu limit {cpus} is outside the supported range {SANDBOX_CPUS_MIN}–{SANDBOX_CPUS_MAX}"
        )
    return cpus


def _default_sandbox_image() -> str:
    """The sandbox image the runtime spawns per agent.

    Defaults to the local tag ``olano-sandbox:latest``. On a managed box
    the provisioner pulls ``ghcr.io/olano-ai/olano-sandbox:<tag>`` and
    ``docker tag``s it to that local name, so the config default just works.
    ``LIONCLAW_SANDBOX_IMAGE`` overrides it (e.g. to pin a GHCR ``:sha`` directly)
    without editing agent config.
    """
    return os.environ.get("LIONCLAW_SANDBOX_IMAGE", "").strip() or "olano-sandbox:latest"


class _StrictBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ─────────────────────────────── non-sandbox backends ────────────────────────


class StateBackendConfig(_StrictBase):
    """In-memory virtual filesystem. Thread-local; no real filesystem."""

    type: Literal["state"] = "state"


class FilesystemBackendConfig(_StrictBase):
    """Host filesystem without shell execution.

    access_mode controls what the agent can do:
    - "read"       → ls, read_file, glob, grep only (writes denied)
    - "read_write" → read + write_file, edit_file (restricted to workspace)

    root_dir="workspace" resolves to the current home's workspace root at build time.
    """

    type: Literal["filesystem"] = "filesystem"
    root_dir: str = "workspace"
    virtual_mode: bool = False
    access_mode: Literal["read", "read_write"] = "read_write"


class LocalShellBackendConfig(_StrictBase):
    """Read/write host filesystem plus shell execution (execute tool).

    Default for agents that need shell. Same options as FilesystemBackend.
    Under a Docker install, "local" means the runtime container.
    """

    type: Literal["local_shell"] = "local_shell"
    root_dir: str = "workspace"
    virtual_mode: bool = False


class StoreBackendConfig(_StrictBase):
    """Persistent KV store backend. Commonly wrapped in CompositeBackend."""

    type: Literal["store"] = "store"
    namespace: list[str] = Field(default_factory=list)


class CompositeBackendConfig(_StrictBase):
    """Routes filesystem paths to different sub-backends.

    Each route value is itself a BackendConfig (recursive).
    """

    type: Literal["composite"] = "composite"
    default: BackendConfig
    routes: dict[str, BackendConfig] = Field(default_factory=dict)


# ──────────────────────────────── custom backend ─────────────────────────────


class OlanoDockerBackendConfig(_StrictBase):
    """Olano-owned Docker sandbox backend.

    persistent=False  → thread-scoped (new container per conversation thread).
    persistent=True   → assistant-scoped (one container per agent, reused across threads).

    ``memory`` / ``cpus`` are the container's hard ceilings. ``None`` means
    "not decided here": the backend falls back to the historical
    512m/0.5 pair, and the builder normally fills them in first from
    ``core.host_resources.resolve_sandbox_limits`` (per-agent override → env →
    config.yaml → a fraction of the host). Both ride in the container
    signature, so changing either recreates the container on the agent's next
    start rather than leaving a stale one running the old caps.
    """

    type: Literal["lionclaw_docker"] = "lionclaw_docker"
    persistent: bool = False
    image: str = Field(default_factory=_default_sandbox_image)
    network: Literal["none", "bridge", "host"] = "none"
    mounts: list[str] = Field(default_factory=lambda: ["/workspace", "/shared"])
    egress_allow: list[str] = Field(default_factory=list)
    egress_deny: list[str] = Field(default_factory=list)
    snapshot_on_stop: bool = False
    memory: str | None = None
    cpus: float | None = None

    @field_validator("memory", mode="before")
    @classmethod
    def _validate_memory(cls, v: Any) -> str | None:
        # Raise rather than coerce: the whole model is extra="forbid", and a
        # typo in an agent's backend block should fail its boot loudly instead
        # of silently running on a different cap than the operator asked for.
        return None if v is None else normalize_sandbox_memory(v)

    @field_validator("cpus", mode="before")
    @classmethod
    def _validate_cpus(cls, v: Any) -> float | None:
        return None if v is None else normalize_sandbox_cpus(v)


# ─────────────────────────────── remote sandbox backends ─────────────────────


class DaytonaBackendConfig(_StrictBase):
    """Daytona remote sandbox (deepagents-native)."""

    type: Literal["daytona"] = "daytona"
    snapshot: str | None = None
    ttl: int = 3600
    labels: dict[str, Any] = Field(default_factory=dict)
    block_network: bool = False


class ModalBackendConfig(_StrictBase):
    """Modal remote sandbox (deepagents-native)."""

    type: Literal["modal"] = "modal"
    snapshot: str | None = None
    ttl: int = 3600
    labels: dict[str, Any] = Field(default_factory=dict)
    block_network: bool = False


class RunloopBackendConfig(_StrictBase):
    """Runloop remote sandbox (deepagents-native)."""

    type: Literal["runloop"] = "runloop"
    snapshot: str | None = None
    ttl: int = 3600
    labels: dict[str, Any] = Field(default_factory=dict)


class AgentCoreBackendConfig(_StrictBase):
    """AgentCore remote sandbox (deepagents-native)."""

    type: Literal["agentcore"] = "agentcore"
    snapshot: str | None = None
    ttl: int = 3600
    labels: dict[str, Any] = Field(default_factory=dict)


class LangSmithBackendConfig(_StrictBase):
    """LangSmith remote sandbox (deepagents-native, beta)."""

    type: Literal["langsmith"] = "langsmith"
    snapshot: str | None = None
    ttl: int = 3600
    labels: dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────── discriminated union ─────────────────────────

BackendConfig = Annotated[
    StateBackendConfig
    | FilesystemBackendConfig
    | LocalShellBackendConfig
    | StoreBackendConfig
    | CompositeBackendConfig
    | OlanoDockerBackendConfig
    | DaytonaBackendConfig
    | ModalBackendConfig
    | RunloopBackendConfig
    | AgentCoreBackendConfig
    | LangSmithBackendConfig,
    Field(discriminator="type"),
]

# Required for recursive CompositeBackendConfig
CompositeBackendConfig.model_rebuild()

# ─────────────────────────────── helpers ─────────────────────────────────────

_SANDBOX_TYPES = frozenset(
    {"lionclaw_docker", "daytona", "modal", "runloop", "agentcore", "langsmith"}
)
_REMOTE_SANDBOX_TYPES = frozenset({"daytona", "modal", "runloop", "agentcore", "langsmith"})


def is_sandbox_backend(cfg: Any) -> bool:
    """Return True if the backend config is a sandbox type (has execute isolation)."""
    return getattr(cfg, "type", None) in _SANDBOX_TYPES


def is_remote_sandbox_backend(cfg: Any) -> bool:
    """Return True if the backend requires remote workspace sync."""
    return getattr(cfg, "type", None) in _REMOTE_SANDBOX_TYPES


def default_backend() -> LocalShellBackendConfig:
    return LocalShellBackendConfig(root_dir="workspace", virtual_mode=False)
