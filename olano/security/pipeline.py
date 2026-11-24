"""Phase 2 tool security pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import StructuredTool

from olano.config.agent_config import AgentConfig
from olano.security.egress import security_metadata as egress_security_metadata
from olano.security.egress_guard import governed as egress_governed
from olano.security.output_scrub import scrub_text
from olano.security.rate_limit import TOOL_RATE_LIMITER
from olano.security.request_context import get_request_context, push_request_context
from olano.security.tool_metadata import ToolMetadata, get_tool_metadata

_WEB_TOOL_NAMES = frozenset(
    {
        "web_search",
        "page_extract",
        "web_extract_deep",
        "web_extract",
        "page_read",
        "web_fetch_fast",
        "web_fetch",
        "url_download",
        "web_download_file",
        "web_download",
        "page_screenshot",
        "image_describe",
        "http_request",
        "http_fetch_api",
        "http_fetch",
        "http_download",
        "firecrawl_scrape",
        "firecrawl_crawl",
        "firecrawl_map",
        "firecrawl_extract",
        "browser_extract",
        "browser_navigate",
    }
)
_OWNER_ONLY_DEFAULTS = {
    "wallet_unlock_hot_mode",
    "wallet_send",
    "secret_get",
    "secret_set",
    "secret_delete",
}
_RISK_ORDER = {"low": 1, "medium": 2, "high": 3}


@dataclass
class GlobalToolPolicy:
    allow: set[str] = field(default_factory=set)
    block: set[str] = field(default_factory=set)
    blocked_capabilities: set[str] = field(default_factory=set)


_GLOBAL_TOOL_POLICY = GlobalToolPolicy()


@dataclass(frozen=True)
class ToolAccessDecision:
    allowed: bool
    layer: int | None = None
    reason: str = ""


def reset_global_tool_policy() -> None:
    _GLOBAL_TOOL_POLICY.allow.clear()
    _GLOBAL_TOOL_POLICY.block.clear()
    _GLOBAL_TOOL_POLICY.blocked_capabilities.clear()


def set_global_tool_policy(
    *,
    allow: list[str] | None = None,
    block: list[str] | None = None,
    blocked_capabilities: list[str] | None = None,
) -> None:
    reset_global_tool_policy()
    if allow:
        _GLOBAL_TOOL_POLICY.allow.update(allow)
    if block:
        _GLOBAL_TOOL_POLICY.block.update(block)
    if blocked_capabilities:
        _GLOBAL_TOOL_POLICY.blocked_capabilities.update(blocked_capabilities)


def _risk_value(risk: str) -> int:
    return _RISK_ORDER.get(risk, 99)


def _governing_org(config: AgentConfig) -> Any | None:
    """The OrgConfig governing ``config``'s A2A policy.

    Mirrors ``talk_to._runner_org``: the central ``organization.yaml`` entry is
    authoritative when present (so a live dashboard edit applies without a
    restart), else the agent's own loaded ``org`` block."""
    try:
        from olano.config.org_store import get_org_if_present

        org = get_org_if_present(config.name)
        if org is not None:
            return org
    except Exception:
        pass
    return getattr(config, "org", None)


def evaluate_tool_access(
    tool_name: str,
    metadata: ToolMetadata,
    config: AgentConfig,
) -> ToolAccessDecision:
    """Evaluate the Phase 2 layered tool policy chain."""
    ctx = get_request_context()

    if _GLOBAL_TOOL_POLICY.allow and tool_name not in _GLOBAL_TOOL_POLICY.allow:
        return ToolAccessDecision(
            False, layer=2, reason=f"'{tool_name}' is not globally allowlisted."
        )
    if tool_name in _GLOBAL_TOOL_POLICY.block:
        return ToolAccessDecision(
            False, layer=2, reason=f"'{tool_name}' is blocked by global policy."
        )
    if metadata.capability in _GLOBAL_TOOL_POLICY.blocked_capabilities:
        return ToolAccessDecision(
            False,
            layer=2,
            reason=f"Capability '{metadata.capability}' is blocked by global policy.",
        )

    tool_policy = dict(getattr(config.security, "tools", {}) or {})
    allow = set(tool_policy.get("allow", []) or [])
    block = set(tool_policy.get("block", []) or [])
    allow_caps = set(tool_policy.get("allow_capabilities", []) or [])
    block_caps = set(tool_policy.get("block_capabilities", []) or [])
    max_risk = str(tool_policy.get("max_risk", "") or "").lower()

    if allow and tool_name not in allow:
        return ToolAccessDecision(
            False, layer=3, reason=f"'{tool_name}' is not allowed for agent '{config.name}'."
        )
    if tool_name in block:
        return ToolAccessDecision(
            False, layer=3, reason=f"'{tool_name}' is blocked for agent '{config.name}'."
        )
    if allow_caps and metadata.capability not in allow_caps:
        return ToolAccessDecision(
            False,
            layer=3,
            reason=f"Capability '{metadata.capability}' is not enabled for agent '{config.name}'.",
        )
    if metadata.capability in block_caps:
        return ToolAccessDecision(
            False,
            layer=3,
            reason=f"Capability '{metadata.capability}' is blocked for agent '{config.name}'.",
        )
    if max_risk and _risk_value(metadata.risk) > _risk_value(max_risk):
        return ToolAccessDecision(
            False,
            layer=3,
            reason=f"Risk tier '{metadata.risk}' exceeds the agent maximum '{max_risk}'.",
        )

    groups_policy = dict(tool_policy.get("groups", {}) or {})
    deny_group_caps = set(groups_policy.get("deny_capabilities", ["mutating"]) or [])
    if ctx.is_group and metadata.capability in deny_group_caps:
        return ToolAccessDecision(
            False,
            layer=4,
            reason=f"Capability '{metadata.capability}' is blocked in group chat contexts.",
        )

    channel_policy = dict((tool_policy.get("channels", {}) or {}).get(ctx.channel, {}) or {})
    if tool_name in set(channel_policy.get("block", []) or []):
        return ToolAccessDecision(
            False,
            layer=4,
            reason=f"'{tool_name}' is blocked on channel '{ctx.channel}'.",
        )
    if metadata.capability in set(channel_policy.get("deny_capabilities", []) or []):
        return ToolAccessDecision(
            False,
            layer=4,
            reason=f"Capability '{metadata.capability}' is blocked on channel '{ctx.channel}'.",
        )

    owner_only = set(tool_policy.get("owner_only", []) or []) | _OWNER_ONLY_DEFAULTS
    if (metadata.owner_only or tool_name in owner_only) and not ctx.is_owner:
        return ToolAccessDecision(
            False,
            layer=5,
            reason=f"'{tool_name}' is owner-only and not available in this context.",
        )

    # Layer 6 — read-only environment for non-admin members. A logged-in human
    # who is neither the owner nor an admin operates read-only: deny every
    # mutating tool so the agent cannot write workspace files, edit memory, or
    # otherwise persist on their behalf. Single-user installs (is_owner=True) and
    # admins are unaffected; unauthenticated system contexts (scheduler,
    # self-learning, channels) are not members and pass through.
    #
    # Peer (talk_to_agent) hops are NOT members: the deployment admin already
    # governs which agents may reach which via the org talk-to policy, so an
    # admitted peer gets full tool service. The one peer-specific restriction is
    # the opt-in ``org.mutating_from_manager_only`` gate below.
    if ctx.source == "peer":
        org = _governing_org(config)
        if (
            metadata.capability == "mutating"
            and org is not None
            and getattr(org, "mutating_from_manager_only", False)
            and not org.is_manager(ctx.user_id)
        ):
            return ToolAccessDecision(
                False,
                layer=6,
                reason=(
                    f"'{tool_name}' is a mutating tool and agent "
                    f"'{config.name}' only executes mutating tasks for its "
                    f"manager ({org.manager or 'unset'}); "
                    f"'{ctx.user_id}' is not its manager."
                ),
            )
    elif (
        ctx.authenticated
        and not ctx.is_owner
        and not ctx.is_admin
        and metadata.capability == "mutating"
    ):
        return ToolAccessDecision(
            False,
            layer=6,
            reason=f"'{tool_name}' is read-only-restricted for non-admin members.",
        )

    return ToolAccessDecision(True)


def _check_tool_rate_limit(tool_name: str, config: AgentConfig) -> tuple[bool, int]:
    rate_limits = dict(getattr(config.security, "rate_limits", {}) or {})
    per_tool = dict(rate_limits.get("per_tool", {}) or {})
    limit = per_tool.get(tool_name)
    if limit is None:
        return True, 0
    ctx = get_request_context()
    key = f"{config.name}:{ctx.user_id}:{ctx.session_id or 'default'}:{tool_name}"
    decision = TOOL_RATE_LIMITER.check(key, max_calls=int(limit), window_seconds=60)
    return decision.allowed, decision.retry_after


def _stringify_tool_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        return str(value)


def _sanitize_result(
    tool_name: str,
    result: Any,
    config: AgentConfig,
    *,
    untrusted: bool = False,
) -> Any:
    if not isinstance(result, str):
        if not untrusted:
            return result
        result = _stringify_tool_result(result)

    text = result
    if tool_name in _WEB_TOOL_NAMES and getattr(config.security, "web_scan", False):
        try:
            from olano.security.web_scan import apply_web_scan

            text, _ = apply_web_scan(text, action="clean")
        except Exception:
            pass

    if getattr(config.security, "credential_scrub", True):
        patterns_path = getattr(config.security, "scrub_patterns_path", None)
        text, _ = scrub_text(text, patterns_path=patterns_path)

    if untrusted:
        from olano.security.input_scan import scan_input

        scan = scan_input(text)
        if not scan.is_safe:
            categories = ", ".join(f["category"] for f in scan.findings)
            return (
                "[trusted=false]\n"
                f"Blocked untrusted MCP result due to prompt-injection risk: {categories}."
            )
        return f"[trusted=false]\n{text}"

    return text


async def _invoke_tool_async(tool: Any, kwargs: dict[str, Any]) -> Any:
    if hasattr(tool, "ainvoke"):
        return await tool.ainvoke(kwargs)
    return tool.invoke(kwargs)


def _invoke_tool_sync(tool: Any, kwargs: dict[str, Any]) -> Any:
    return tool.invoke(kwargs)


def wrap_tool_with_security(tool: Any, config: AgentConfig, *, untrusted: bool = False) -> Any:
    """Wrap a LangChain tool with policy, rate limiting, and output scrubbing."""
    if getattr(tool, "_lionclaw_security_wrapped", False):
        return tool
    if not hasattr(tool, "invoke"):
        return tool

    metadata = get_tool_metadata(tool)
    name = getattr(tool, "name", getattr(tool, "__name__", "tool"))
    description = getattr(tool, "description", "") or ""
    args_schema = getattr(tool, "args_schema", None)
    return_direct = getattr(tool, "return_direct", False)

    def _blocked_message(decision: ToolAccessDecision) -> str:
        return f"Error: tool blocked at security layer {decision.layer}: {decision.reason}"

    def _sync_wrapper(**kwargs: Any) -> Any:
        decision = evaluate_tool_access(name, metadata, config)
        if not decision.allowed:
            return _blocked_message(decision)
        allowed, retry_after = _check_tool_rate_limit(name, config)
        if not allowed:
            return f"Error: tool rate limit exceeded for '{name}'. Retry after {retry_after}s."
        # Resolved per call, not at wrap time: the deployment half lives in
        # config.yaml and a Config-page save must reach the next tool call.
        # `egress_governed` clears any exemption an enclosing nested-agent run
        # set, so a tool is always governed by its own agent's lists.
        with (
            push_request_context(metadata={"security": egress_security_metadata(config)}),
            egress_governed(),
        ):
            result = _invoke_tool_sync(tool, kwargs)
        return _sanitize_result(name, result, config, untrusted=untrusted)

    async def _async_wrapper(**kwargs: Any) -> Any:
        decision = evaluate_tool_access(name, metadata, config)
        if not decision.allowed:
            return _blocked_message(decision)
        allowed, retry_after = _check_tool_rate_limit(name, config)
        if not allowed:
            return f"Error: tool rate limit exceeded for '{name}'. Retry after {retry_after}s."
        with (
            push_request_context(metadata={"security": egress_security_metadata(config)}),
            egress_governed(),
        ):
            result = await _invoke_tool_async(tool, kwargs)
        return _sanitize_result(name, result, config, untrusted=untrusted)

    wrapped = StructuredTool.from_function(
        func=_sync_wrapper,
        coroutine=_async_wrapper,
        name=name,
        description=description,
        args_schema=args_schema,
        return_direct=return_direct,
    )
    wrapped.metadata = dict(getattr(tool, "metadata", {}) or {})
    wrapped.metadata["lionclaw_tool_metadata"] = metadata.to_dict()
    if untrusted:
        wrapped.metadata["trusted"] = False
    object.__setattr__(wrapped, "lionclaw_tool_metadata", metadata)
    object.__setattr__(wrapped, "_lionclaw_security_wrapped", True)
    return wrapped


def wrap_tools_with_security(
    tools: list[Any],
    config: AgentConfig,
    *,
    untrusted: bool = False,
) -> list[Any]:
    """Wrap each tool in a list with the Phase 2 security pipeline."""
    return [wrap_tool_with_security(tool, config, untrusted=untrusted) for tool in tools]


def mark_tool_untrusted(tool: Any) -> Any:
    """Tag a tool result source as untrusted without wrapping execution."""
    metadata = dict(getattr(tool, "metadata", {}) or {})
    metadata["trusted"] = False
    object.__setattr__(tool, "metadata", metadata)
    return tool


def mark_tools_untrusted(tools: list[Any]) -> list[Any]:
    return [mark_tool_untrusted(tool) for tool in tools]
