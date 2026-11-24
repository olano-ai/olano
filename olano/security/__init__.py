"""Olano security middleware pipeline.

The public names below are re-exported lazily (PEP 562). Importing this
package — or any submodule such as ``olano.security.tool_metadata`` — no
longer eagerly pulls in ``deepagents_middleware`` and its
``langchain.agents.middleware`` → transformers/torch dependency chain (several
seconds of import time). The heavy import happens only when a re-exported name
is actually accessed, e.g. ``from olano.security import build_security_middleware``.

This keeps lightweight paths fast — notably listing ToolkitAgents / tools in the
dashboard, which reaches ``security.tool_metadata`` transitively and used to pay
the full middleware import cost on first load.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from olano.security.deepagents_middleware import build_security_middleware
    from olano.security.pipeline import (
        evaluate_tool_access,
        mark_tool_untrusted,
        mark_tools_untrusted,
        reset_global_tool_policy,
        set_global_tool_policy,
        wrap_tool_with_security,
        wrap_tools_with_security,
    )
    from olano.security.policies import SecurityProfile, get_profile
    from olano.security.trust_levels import TrustLevel

# Re-exported name → submodule that defines it. Resolved on first access.
_LAZY_EXPORTS: dict[str, str] = {
    "build_security_middleware": "olano.security.deepagents_middleware",
    "evaluate_tool_access": "olano.security.pipeline",
    "mark_tool_untrusted": "olano.security.pipeline",
    "mark_tools_untrusted": "olano.security.pipeline",
    "reset_global_tool_policy": "olano.security.pipeline",
    "set_global_tool_policy": "olano.security.pipeline",
    "wrap_tool_with_security": "olano.security.pipeline",
    "wrap_tools_with_security": "olano.security.pipeline",
    "SecurityProfile": "olano.security.policies",
    "get_profile": "olano.security.policies",
    "TrustLevel": "olano.security.trust_levels",
}

__all__ = [
    "SecurityProfile",
    "TrustLevel",
    "build_security_middleware",
    "evaluate_tool_access",
    "get_profile",
    "mark_tool_untrusted",
    "mark_tools_untrusted",
    "reset_global_tool_policy",
    "set_global_tool_policy",
    "wrap_tool_with_security",
    "wrap_tools_with_security",
]


def __getattr__(name: str) -> Any:
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_path), name)
    globals()[name] = value  # cache so subsequent lookups skip __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
