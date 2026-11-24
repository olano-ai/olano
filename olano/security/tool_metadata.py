"""Capability and risk metadata for tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ToolMetadata:
    capability: str = "read_only"
    risk: str = "low"
    requires_hitl: bool = False
    owner_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_READ_ONLY_VERBS = (
    "get_",
    "list_",
    "search",
    "read",
    "fetch",
    "extract",
    "describe",
    "summary",
    "status",
    "history",
)
_MUTATING_VERBS = (
    "create",
    "update",
    "delete",
    "send",
    "post",
    "upload",
    "write",
    "edit",
    "append",
    "add",
    "remove",
    "move",
    "run",
    "execute",
    "convert",
    "compress",
    "decompress",
    "generate",
    "install",
    "lock",
    "unlock",
    "broadcast",
    "publish",
    "set",
    "approve",
    "clear",
)
_HIGH_RISK_NAMES = {
    "execute",
    "safe_shell",
    "safe_shell_tool",
    "node_run",
    "node_run_tool",
    "npm_install",
    "npm_install_tool",
    "pages_deploy",
    "wallet_send",
    "wallet_unlock_hot_mode",
    "github_create_pr",
    "github_create_issue",
    "gmail_send",
    "drive_upload",
}
_OWNER_ONLY_NAMES = {
    "secret_get",
    "secret_set",
    "secret_delete",
    "wallet_unlock_hot_mode",
    "wallet_send",
}


def tool_metadata(
    *,
    capability: str,
    risk: str,
    requires_hitl: bool,
    owner_only: bool = False,
) -> Callable[[Any], Any]:
    """Attach security metadata to a tool or tool-producing callable."""

    metadata = ToolMetadata(
        capability=capability,
        risk=risk,
        requires_hitl=requires_hitl,
        owner_only=owner_only,
    )

    def decorator(obj: Any) -> Any:
        object.__setattr__(obj, "lionclaw_tool_metadata", metadata)
        existing = dict(getattr(obj, "metadata", {}) or {})
        existing["lionclaw_tool_metadata"] = metadata.to_dict()
        object.__setattr__(obj, "metadata", existing)
        return obj

    return decorator


def infer_tool_metadata(name: str) -> ToolMetadata:
    """Infer metadata conservatively when a tool is not explicitly tagged."""
    normalized = name.lower()
    capability = "read_only"
    if (
        normalized in _HIGH_RISK_NAMES
        or normalized.startswith(_MUTATING_VERBS)
        or any(f"_{verb}" in normalized for verb in _MUTATING_VERBS)
    ):
        capability = "mutating"

    risk = "low"
    if capability == "mutating":
        risk = "medium"
    if normalized in _HIGH_RISK_NAMES or "wallet" in normalized or "shell" in normalized:
        risk = "high"
    elif normalized.startswith(("browser_click", "browser_type", "scheduler_", "file_")):
        risk = "medium"
    elif normalized.startswith(_READ_ONLY_VERBS):
        risk = "low"

    requires_hitl = capability == "mutating" and risk in {"medium", "high"}
    owner_only = normalized in _OWNER_ONLY_NAMES
    return ToolMetadata(
        capability=capability,
        risk=risk,
        requires_hitl=requires_hitl,
        owner_only=owner_only,
    )


def get_tool_metadata(tool: Any) -> ToolMetadata:
    """Return explicit metadata or a conservative inferred fallback."""
    existing = getattr(tool, "lionclaw_tool_metadata", None)
    if isinstance(existing, ToolMetadata):
        return existing
    name = getattr(tool, "name", getattr(tool, "__name__", "tool"))
    metadata = infer_tool_metadata(name)
    object.__setattr__(tool, "lionclaw_tool_metadata", metadata)
    tool_meta = dict(getattr(tool, "metadata", {}) or {})
    tool_meta.setdefault("lionclaw_tool_metadata", metadata.to_dict())
    object.__setattr__(tool, "metadata", tool_meta)
    return metadata
