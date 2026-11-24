"""Agent-facing PULSE management tools."""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import tool

from olano.config.agent_config import AgentConfig
from olano.config.workspace import get_workspace
from olano.pulse.engine import PulseEngine
from olano.pulse.generators import PulseGenerator
from olano.pulse.models import PulseLane
from olano.security.request_context import get_request_context
from olano.security.tool_metadata import tool_metadata


def _pulse_engine() -> PulseEngine:
    request_context = get_request_context()
    workspace_root = request_context.metadata.get("workspace_root")
    workspace = get_workspace(str(workspace_root) if workspace_root else None)
    workspace.init()
    from olano.config.backend_config import FilesystemBackendConfig

    config = AgentConfig(
        name="ops",
        workspace=str(workspace.root),
        backend=FilesystemBackendConfig(root_dir=str(workspace.root)),
    )
    return PulseEngine(config, workspace.state_dir)


def _workspace_root() -> Any:
    request_context = get_request_context()
    workspace_root = request_context.metadata.get("workspace_root")
    workspace = get_workspace(str(workspace_root) if workspace_root else None)
    workspace.init()
    return workspace.root


def _format_item(item) -> str:
    label = item.title or item.content
    parts = [
        f"[{item.id}]",
        f"[{item.type.value}]",
        f"[{item.priority_level.value}]",
        f"[{item.source}]",
        label[:140],
    ]
    lines = [" ".join(parts)]
    if item.why_now:
        lines.append(f"  Why now: {item.why_now[:160]}")
    if item.suggested_next_step:
        lines.append(f"  Next: {item.suggested_next_step[:160]}")
    return "\n".join(lines)


def _list_lane(lane: PulseLane, max_items: int) -> str:
    engine = _pulse_engine()
    items = engine.list_pending(max_items=max_items, lane=lane)
    if not items:
        if lane == PulseLane.PULSE:
            return "No curated PULSE items pending."
        if lane == PulseLane.APPROVAL:
            return "No approval items pending."
        return "No ops items pending."
    header = {
        PulseLane.PULSE: "PULSE",
        PulseLane.APPROVAL: "APPROVALS",
        PulseLane.OPS: "OPS",
    }[lane]
    lines = [f"{header} ({len(items)} items):"]
    lines.extend(_format_item(item) for item in items)
    return "\n".join(lines)


# The approvals lane is the human's review of what the agent wants to do, and
# accepting a PULSE item (or undoing one) runs or reverts the work behind it.
# All of these are owner-only: a talk_to_agent peer, an inbound MCP/A2A caller,
# a non-admin member or a customer on a channel must never clear a held action.
# The name-based fallback (security.tool_metadata.infer_tool_metadata) reads
# "approve"/"accept"/"undo"/"reject"/"defer" as read-only, which is why these
# carry explicit metadata now that they sit on every agent's top-level spec.
# No HITL on approve: the owner saying "approve X" is the approval.


@tool
def pulse_list(
    max_items: Annotated[int, "Maximum number of items to return"] = 10,
) -> str:
    """List curated PULSE items."""
    return _list_lane(PulseLane.PULSE, max_items)


@tool
async def pulse_generate(
    thread_id: Annotated[str, "Optional thread id to ground the curated generation"] = "",
) -> str:
    """Generate curated PULSE items from recent work, memory, and workspace context."""
    try:
        workspace_root = _workspace_root()
        from olano.config.backend_config import FilesystemBackendConfig

        config = AgentConfig(
            name="ops",
            workspace=str(workspace_root),
            backend=FilesystemBackendConfig(root_dir=str(workspace_root)),
        )
        generator = PulseGenerator(config, workspace_root)
        items = await generator.capture_agent_curated_pulse(
            thread_id=thread_id.strip() or None, force=True
        )
        if not items:
            return "No new curated PULSE items were generated."
        lines = [f"Generated {len(items)} curated PULSE item(s):"]
        lines.extend(_format_item(item) for item in items)
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] pulse_generate: {exc}"


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False, owner_only=True)
@tool
def pulse_accept(
    item_id: Annotated[str, "PULSE item id to accept"],
) -> str:
    """Accept a curated PULSE item."""
    engine = _pulse_engine()
    ok, message = engine.accept(item_id.strip())
    if ok:
        return message
    return f"Could not accept {item_id}: {message}"


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False, owner_only=True)
@tool
def pulse_undo(
    item_id: Annotated[str, "PULSE item id whose autonomous artifact to undo"],
) -> str:
    """Undo an autonomous artifact by reverting its backup (for outcome pulses)."""
    engine = _pulse_engine()
    ok, message = engine.undo(item_id.strip())
    return message if ok else f"Could not undo {item_id}: {message}"


@tool
def pulse_reject(
    item_id: Annotated[str, "PULSE item id to reject"],
) -> str:
    """Dismiss a curated PULSE item."""
    engine = _pulse_engine()
    if engine.reject(item_id.strip()):
        return f"Rejected PULSE item {item_id}."
    return f"PULSE item {item_id} not found."


@tool
def pulse_defer(
    item_id: Annotated[str, "PULSE item id to defer"],
    hours: Annotated[float, "Number of hours to defer the item"] = 24.0,
) -> str:
    """Defer a curated PULSE item by some number of hours."""
    engine = _pulse_engine()
    if engine.defer(item_id.strip(), hours=hours):
        return f"Deferred PULSE item {item_id} for {hours:g} hours."
    return f"PULSE item {item_id} not found."


@tool
def pulse_clear_expired() -> str:
    """Clear expired curated PULSE items from the queue."""
    engine = _pulse_engine()
    removed = engine.clear_expired(lane=PulseLane.PULSE)
    return f"Expired curated PULSE items cleared: {removed}."


@tool
def approvals_list(
    max_items: Annotated[int, "Maximum number of approval items to return"] = 10,
) -> str:
    """List pending approval items."""
    return _list_lane(PulseLane.APPROVAL, max_items)


@tool_metadata(capability="mutating", risk="medium", requires_hitl=False, owner_only=True)
@tool
def approvals_approve(
    item_id: Annotated[str, "Approval item id to approve"],
) -> str:
    """Approve an approval item and execute its action."""
    engine = _pulse_engine()
    ok, message = engine.approve(item_id.strip())
    if ok:
        return message
    return f"Could not approve {item_id}: {message}"


@tool_metadata(capability="mutating", risk="low", requires_hitl=False, owner_only=True)
@tool
def approvals_reject(
    item_id: Annotated[str, "Approval item id to reject"],
) -> str:
    """Reject a pending approval item."""
    engine = _pulse_engine()
    if engine.reject(item_id.strip()):
        return f"Rejected approval item {item_id}."
    return f"Approval item {item_id} not found."


@tool_metadata(capability="mutating", risk="low", requires_hitl=False, owner_only=True)
@tool
def approvals_defer(
    item_id: Annotated[str, "Approval item id to defer"],
    hours: Annotated[float, "Number of hours to defer the item"] = 24.0,
) -> str:
    """Defer a pending approval item."""
    engine = _pulse_engine()
    if engine.defer(item_id.strip(), hours=hours):
        return f"Deferred approval item {item_id} for {hours:g} hours."
    return f"Approval item {item_id} not found."


@tool_metadata(capability="mutating", risk="low", requires_hitl=False, owner_only=True)
@tool
def approvals_clear_expired() -> str:
    """Clear expired approval items from the queue."""
    engine = _pulse_engine()
    removed = engine.clear_expired(lane=PulseLane.APPROVAL)
    return f"Expired approval items cleared: {removed}."


@tool
def ops_list(
    max_items: Annotated[int, "Maximum number of ops items to return"] = 10,
) -> str:
    """List pending ops items."""
    return _list_lane(PulseLane.OPS, max_items)


@tool
def ops_reject(
    item_id: Annotated[str, "Ops item id to reject"],
) -> str:
    """Dismiss an ops item."""
    engine = _pulse_engine()
    if engine.reject(item_id.strip()):
        return f"Rejected ops item {item_id}."
    return f"Ops item {item_id} not found."


@tool
def ops_defer(
    item_id: Annotated[str, "Ops item id to defer"],
    hours: Annotated[float, "Number of hours to defer the item"] = 24.0,
) -> str:
    """Defer an ops item."""
    engine = _pulse_engine()
    if engine.defer(item_id.strip(), hours=hours):
        return f"Deferred ops item {item_id} for {hours:g} hours."
    return f"Ops item {item_id} not found."


@tool
def ops_clear_expired() -> str:
    """Clear expired ops items from the queue."""
    engine = _pulse_engine()
    removed = engine.clear_expired(lane=PulseLane.OPS)
    return f"Expired ops items cleared: {removed}."


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
