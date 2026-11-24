"""Default operations-management tool wiring."""

from __future__ import annotations

from typing import TYPE_CHECKING

# ── Ops toolset (universal defaults) ──────────────────────────────────────────
# Scheduler, heartbeat, PULSE, approvals and the ops queue. Every agent gets
# these at the top level (they are part of UNIVERSAL_AGENT_TOOL_NAMES below).
# The names live in security.ops_tool_names (a leaf: delegate_guards folds the
# mutating subset into its block list and must not import this package); the
# re-exports below keep this module their public home.
from olano.security.ops_tool_names import OPS_MUTATING_TOOL_NAMES as OPS_MUTATING_TOOL_NAMES
from olano.security.ops_tool_names import OPS_READ_ONLY_TOOL_NAMES as OPS_READ_ONLY_TOOL_NAMES
from olano.security.ops_tool_names import OPS_TOOL_NAMES as OPS_TOOL_NAMES

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

# ── Universal defaults ────────────────────────────────────────────────────────
# Every new agent gets these regardless of role. They form the social and
# cognitive fabric that makes agents useful in a multi-agent system.
BUILTIN_DOCUMENT_TOOL_NAMES: list[str] = [
    "document_to_text",  # Convert common local documents (.pdf/.docx/.pptx/.xlsx/etc.) to text
    "pdf_to_text",  # Extract text from local or downloaded PDFs
]

UNIVERSAL_AGENT_TOOL_NAMES: list[str] = [
    # Self-awareness
    "whoami",  # Returns agent's own name, model, channels, tools, skills
    # Time awareness
    "get_current_time",  # Current date/time in UTC + local tz; alias: get_current_date
    "get_current_date",  # Alias for get_current_time
    "wait_n_seconds",  # Non-blocking pause for a2a polling patterns (max 300s)
    # Inter-agent communication
    "talk_to_agent",  # Send messages to other agents in the runtime
    # User interaction
    "notification_send_text",  # Push text updates to the user
    "notification_send_photo",  # Push image attachments (local path or URL) to a channel
    "notification_send_document",  # Push file attachments (local path or URL) to a channel
    # Addressing companions for the notification tools — resolve chat/group/user
    # names to the platform ids the send tools take as `target`
    "chats_find",  # Look up a chat/group/channel id by name in the chat directory
    "chats_list",  # List known chats (optionally refresh via native platform enumeration)
    "chats_find_user",  # Look up a user's platform id by name/@handle
    "chat_info",  # Verify a chat id live + show directory history (do this before scheduling)
    "chat_messages_recent",  # Read recent listened group history (group_listen recording)
    "chat_messages_search",  # Search listened group history by text/sender
    # Issue tracker — durable, assignable work items between agents and humans.
    # Universal for the same reason talk_to_agent is: coordination primitives
    # only work if every agent has them, and the per-agent issue poll's
    # delivery prompt references these tools by name.
    "issue_create",
    "issue_comment",
    "issue_close",
    "issue_reopen",
    "issue_list",
    "issue_show",
    "issue_assign",
    # External knowledge
    "web_search",  # Search the web for current information
    "web_fetch",  # Fetch and read web pages
    # Local documents
    *BUILTIN_DOCUMENT_TOOL_NAMES,
    # Deterministic rendering on the bundled Chromium — HTML/CSS in, exact-pixel
    # PNG / PDF / SVG out (tools/render_tools.py). Local, offline, no credential,
    # no credits: the way a chart, a slide, a social graphic or a PDF report gets
    # made (see the charts / social-image-composer skills). Universal for the
    # same reason the document readers are — a deliverable with text on it is
    # rendered, never improvised with a plotting library the sandbox lacks.
    "render_html_image",
    "render_html_pdf",
    "render_html_svg",
    # Workspace housekeeping — recoverable trash can (downloads/scratch are conventions)
    "trash_put",  # Move a file/folder to workspace/trash/ (recoverable delete)
    "trash_list",  # List what's in the trash can
    "trash_restore",  # Restore a trashed item to its original location
    "trash_empty",  # Permanently empty the trash (confirm=true)
    # Operations: scheduler, heartbeat, PULSE, approvals, ops queue
    *OPS_TOOL_NAMES,
]

# ── Cortex item tools (agents with the Cortex engine enabled) ────────────────
# Injected by ``AgentConfig._inject_builtin_ops`` only when the Cortex engine is
# on for the agent (global cognition master switch AND per-agent
# ``cognition.enabled``), so the user can drive ideas / brainstorms / prototypes
# / projects straight from chat: list, view, add, edit, approve (promote),
# explore, discard, restore, and schedule a follow-up step on a prototype or
# project for itself. See ``olano/tools/cortex_tools.py``.
CORTEX_TOOL_NAMES: list[str] = [
    "cortex_list",
    "cortex_view",
    "cortex_add",
    "cortex_edit",
    "cortex_discard",
    "cortex_restore",
    "cortex_promote",
    "cortex_explore",
    "cortex_schedule",
]

# The optional OpsManager subagent's tool list. It is no longer a default
# subagent (its tools are universal now); an agent.json5 that still lists
# `builtin_subagents: ["ops"]` keeps it.
OPS_MANAGER_TOOL_NAMES: list[str] = [
    *OPS_TOOL_NAMES,
    "gmail_send",
    "notification_send_text",
    # Chat/user id lookup — scheduled sends should embed explicit chat ids,
    # so the ops manager must be able to resolve and verify them.
    "chats_find",
    "chats_list",
    "chats_find_user",
    "chat_info",
    "chat_messages_recent",
    "chat_messages_search",
    # Idea / brainstorming capture lives with the ops manager too, so the
    # capability stays reachable (via delegation) without every agent carrying
    # it at the top level.
    "idea_add",
    "ideas_list",
    "brainstorm_note",
]

# ── Deepagents built-ins ──────────────────────────────────────────────────────
# Filesystem + workflow tools that ``create_deep_agent`` always provides, no
# matter what ``agent.json5 → tools`` says. They never appear in an agent's
# configured ``tools`` list, so any "what can this agent do" inventory (CLI
# ``/tools``, dashboard ``/tools``, ``whoami``) must add them back to match the
# tools the model is actually bound to.
DEEPAGENTS_BUILTIN_TOOL_NAMES: list[str] = [
    "write_todos",
    "read_file",
    "write_file",
    "edit_file",
    "ls",
    "glob",
    "grep",
    "execute",
    "task",
    "compact_conversation",  # olano's on-demand /compact tool; see builder._build_compact_tool_middleware
]


def resolve_effective_tool_names(
    configured_tools: list[str],
    disabled_tools: list[str] | None = None,
    config: AgentConfig | None = None,
) -> list[str]:
    """Effective tool names an agent runs with, for display/inventory listings.

    ``configured_tools`` is an ``AgentConfig.tools`` list — already merged with
    the universal defaults by ``AgentConfig._inject_builtin_ops``.
    This appends the always-present deepagents built-ins (respecting
    ``disabled_tools``) so callers get the full set the model is bound to,
    matching what the agent's ``whoami`` reports. Order is preserved and
    duplicates are dropped.

    Pass ``config`` whenever you have it: the multimodal defaults
    (``config/multimodal_defaults``) are granted by the *builder* rather than by
    the config merge, because whether they exist depends on a provider key
    resolving — so they are absent from ``configured_tools`` and an inventory
    that omits them under-reports what the model is bound to. Best-effort: a
    failing credential probe just leaves them out.
    """
    multimodal: list[str] = []
    autonomy: list[str] = []
    if config is not None:
        try:
            from olano.config.multimodal_defaults import available_multimodal_tool_names

            multimodal = available_multimodal_tool_names(config)
        except Exception:
            multimodal = []
        # Same builder-granted shape as the multimodal defaults: the self_*
        # tools ride the resolved autonomy capabilities, not `tools:`.
        try:
            from olano.config.autonomy_defaults import (
                available_magic_link_tool_names,
                available_self_config_tool_names,
            )

            autonomy = [
                *available_self_config_tool_names(config),
                *available_magic_link_tool_names(config),
            ]
        except Exception:
            autonomy = []
        # And the outbound A2A tools, granted by the builder when the agent
        # lists a remote agent.
        try:
            from olano.config.internal_tools import (
                available_a2a_tool_names,
                available_whatsapp_business_tool_names,
            )

            autonomy += available_a2a_tool_names(config)
            # And the WhatsApp template send tools, granted with the connector.
            autonomy += available_whatsapp_business_tool_names(config)
        except Exception:
            pass
    disabled = set(disabled_tools or [])
    # The browser tools exist only while config.yaml → browser turns them on.
    from olano.browser.policy import BROWSER_TOOL_NAMES, browser_tools_allowed

    if any(n in BROWSER_TOOL_NAMES for n in configured_tools) and not browser_tools_allowed():
        disabled |= BROWSER_TOOL_NAMES
    seen: set[str] = set()
    names: list[str] = []
    for name in [*configured_tools, *multimodal, *autonomy, *DEEPAGENTS_BUILTIN_TOOL_NAMES]:
        if name and name not in seen and name not in disabled:
            seen.add(name)
            names.append(name)
    return names
