"""Single source of truth for which tools require human approval (HITL).

Both ``core/builder.py`` (which builds the deepagents ``interrupt_on`` map) and
``core/runner.py`` (which classifies/decides on interrupts at runtime) import
from here, so the tool→category mapping lives in exactly one place. This
replaces the duplicated, drifting tool-name sets that previously lived in both
modules.

The model has three layers, resolved by :func:`resolve_approval_set`:

1. **Profile** (``security.profile``) — the persistent baseline. Each profile
   maps to a set of *categories* (see :data:`PROFILE_CATEGORIES`).
   - ``developer`` ("Trusted — act freely"): money + secret writes +
     cross-agent exec only.
   - ``safe`` ("Supervised — ask before writes"): + external writes, shell,
     secret reads.
   - ``production`` ("Locked-down — strictest"): + filesystem writes and
     subagent delegation.
   ``cross_agent_exec`` is in every profile: running a command in ANOTHER
   agent's workspace is always individually human-approved.
2. **Toggles** (``security.hitl_write_tools`` / ``security.hitl_shell``) — add
   the ``write_external`` / ``shell`` categories on top of the profile.
3. **Per-tool overrides** (``security.tools[name].approval`` = ``"always"`` |
   ``"never"``) — force a single tool's approval on or off; highest precedence.

``security.hitl_enabled`` is a master switch: setting it ``False`` disables HITL
entirely (an explicit "full autonomy" escape hatch). All profiles enable it by
default.
"""

from __future__ import annotations

from typing import Any

# Community interactions and publishing are externally visible writes.  Keep
# this generated from the shared social surface so a new platform cannot be
# accidentally granted silent write access by a hand-maintained list.
_SOCIAL_WRITE_SUFFIXES = {
    "create_post",
    "update_post",
    "delete_post",
    "like_post",
    "unlike_post",
    "bookmark_post",
    "remove_bookmark",
    "reply_to_comment",
    "react_to_comment",
    "remove_comment_reaction",
    "hide_comment",
    "delete_comment",
    "reply_to_dm",
    "send_dm",
    "react_to_dm",
    "remove_dm_reaction",
    "mark_dm_read",
    "follow_user",
    "unfollow_user",
}
_SOCIAL_WRITE_TOOLS = frozenset(
    f"{platform}_{suffix}"
    for platform in ("tiktok", "douyin", "rednote")
    for suffix in _SOCIAL_WRITE_SUFFIXES
)

# ── Canonical tool → category map ────────────────────────────────────────────
# A tool absent from every category never triggers HITL (e.g. read-only tools
# like ``coinbase_list_orders`` or ``drive_list``).
CATEGORY_TOOLS: dict[str, frozenset[str]] = {
    # Irreversible money movement.
    "financial": frozenset({"wallet_send"}),
    # Writing secrets/credentials. ``magic_link_request_secret`` mints a link
    # whose form writes straight to the vault, so it is a secret write even
    # though the agent never sees the value.
    "secret_write": frozenset({"secret_set", "magic_link_request_secret"}),
    # Reading secrets/credentials (exfiltration risk).
    # ``lusha_get_webhook_secret`` returns the key that signs Lusha's webhook
    # deliveries; whoever holds it can forge events to the user's receiver.
    "secret_read": frozenset({"secret_get", "lusha_get_webhook_secret"}),
    # Writes to external systems (email, calendar, posts, repos, drive, …).
    "write_external": frozenset(
        {
            "gmail_send",
            # Hands a message (and whatever the model puts in it) to an agent
            # run by another organisation, over the A2A protocol.
            "a2a_send_message",
            "calendar_create_event",
            "calendar_delete_event",
            "slack_send_message",
            "github_create_pr",
            "github_create_issue",
            # A push lands on the remote; clone/fetch/pull only change the workspace.
            "git_push",
            "notion_create_page",
            "drive_upload",
            "x_post_tweet",
            "linkedin_post",
            # Managed LinkedIn outreach: each lands in another member's
            # notifications (or a public thread) under the user's own name.
            "linkedin_follow_profile",
            "linkedin_send_invitation",
            "linkedin_send_message",
            "linkedin_comment_on_post",
            "buffer_create_post",
            "shopify_create_product",
            "stripe_create_invoice",
            # Apollo: enrolling contacts in a sequence starts real emails to
            # real people from the team's mailboxes, and the generic request
            # tool reaches the send-now endpoint. Searches, enrichment and
            # record edits stay approval-free.
            "apollo_add_contacts_to_sequence",
            "apollo_api_request",
            # Lusha: a webhook subscription makes Lusha push contact and
            # company data to whatever URL it names; deleting a table, a column
            # or a subscription and rotating the webhook secret cannot be
            # undone (the rotation breaks every receiver at once); the generic
            # request tool reaches all of them. Searches, enrichment, scores
            # and table edits stay approval-free.
            "lusha_create_webhook_subscriptions",
            "lusha_update_webhook_subscription",
            "lusha_create_opt_out_subscription",
            "lusha_delete_webhook_subscriptions",
            "lusha_regenerate_webhook_secret",
            "lusha_delete_table",
            "lusha_remove_table_column",
            "lusha_api_request",
            # QuickBooks writes land in a real company's books. Three of these
            # are more than a record edit: send emails an invoice to the
            # customer, pay_bill and record_payment move money against a
            # vendor/customer balance, and void rewrites a posted document.
            # Reads (list/get/search/reports) are deliberately not here.
            "quickbooks_create_customer",
            "quickbooks_update_customer",
            "quickbooks_create_vendor",
            "quickbooks_create_invoice",
            "quickbooks_send_invoice",
            "quickbooks_void_invoice",
            "quickbooks_create_estimate",
            "quickbooks_create_sales_receipt",
            "quickbooks_record_payment",
            "quickbooks_create_bill",
            "quickbooks_pay_bill",
            "quickbooks_create_expense",
            # WhatsApp templates over Twilio: a send is a billed message to a
            # real person; create/submit put text under the business's name in
            # Meta's review; delete removes an approved template account-wide.
            # The listing and status reads are deliberately not here.
            "whatsapp_template_send",
            "whatsapp_template_create",
            "whatsapp_template_submit",
            "whatsapp_template_delete",
            "whatsapp_profile_update",
            # Microsoft 365: what reaches other people or changes what they
            # see. Sends, replies and forwards deliver mail; event writes and
            # responses email invitations, updates and cancellations; a share
            # or an upload exposes a file; Excel writes change a shared
            # workbook; an automatic reply answers every sender. Drafts,
            # flags, folder moves, contacts and To Do stay approval-free, so
            # an agent can prepare a draft the user reviews and sends.
            "ms365_send_mail",
            "ms365_send_draft",
            "ms365_reply",
            "ms365_forward",
            "ms365_create_event",
            "ms365_update_event",
            "ms365_delete_event",
            "ms365_respond_to_event",
            "ms365_set_auto_reply",
            "ms365_upload_drive_file",
            "ms365_share_drive_item",
            "ms365_delete_drive_item",
            "ms365_excel_write_range",
            "ms365_excel_add_rows",
            "ms365_send_channel_message",
            "ms365_send_chat_message",
            # olano_admin (AgentFather) — mutating self-management ops.
            "olano_create_agent",
            "olano_update_agent",
            "olano_set_agent_external_access",
            "self_set_external_access",
            "magic_link_mint_access_token",
            "olano_delete_agent",
            "olano_scheduler_delete",
            # Cortex review of another agent (AgentFather): status flips the
            # dashboard makes with one click, and a promotion that runs the
            # target's own fixed promotion playbook. No prompt chosen here
            # reaches the target, so not cross_agent_exec.
            "olano_cortex_discard",
            "olano_cortex_restore",
            "olano_cortex_promote",
            "olano_scaffold_archetype",
            "olano_set_tool_enabled",
            "olano_add_mcp_server",
            "olano_remove_mcp_server",
            "olano_authorize_mcp_server",
            "olano_add_toolkit",
            "olano_remove_toolkit",
            # Mints a magic link: whoever holds it can connect an account to
            # this deployment, so the owner approves before it exists.
            "magic_link_connect_service",
            # Mints a link that issues a real dashboard session — the most
            # powerful magic link — so it is gated too.
            "magic_link_open_dashboard",
            # Mints a link that rewrites an agent's standing instructions.
            "magic_link_edit_instructions",
            # Mints a link that rewrites a file — and shows its current
            # contents to whoever opens it, which for a .env is the point.
            "magic_link_request_file_edit",
            # Mints a link that connects messengers (and writes bot tokens to
            # the vault) for an agent.
            "magic_link_connect_channels",
            "olano_create_skill",
            "olano_install_skill",
            "olano_write_agent_file",
            "olano_start_agent",
            "olano_stop_agent",
            "olano_restart_agent",
            "olano_add_subagent",
            "olano_toggle_subagent",
            "olano_update_home_config",
            # Human-user management (AgentFather): these mint login-capable
            # invite links, change who holds admin, and decide which humans
            # reach which agents. Every one is also owner_only via
            # @tool_metadata — the HITL card is the second gate, not the first.
            "olano_create_user",
            "olano_reset_user_password",
            "olano_set_user_role",
            "olano_update_user",
            "olano_delete_user",
            "olano_grant_agent_access",
            "olano_revoke_agent_access",
            # Disk cleanup deletes data (docker prune, cache clears, journald
            # vacuum); the skill repair renames folders on disk.
            "olano_disk_monitor",
            "olano_repair_skill_names",
            # self_config — an agent mutating ITSELF from chat (prompt, model,
            # subagents, MCP servers, messaging channels, restart). Read-only
            # siblings (self_get_config, the list/search tools) are not here.
            "self_set_model",
            "self_update_prompt",
            "self_restart",
            "self_add_subagent",
            "self_add_mcp_server",
            "self_remove_mcp_server",
            "self_authorize_mcp_server",
            "self_add_toolkit",
            "self_remove_toolkit",
            "self_connect_platform",
            "self_connect_whatsapp",
            "self_connect_weixin",
            "self_enable_skill",
            "self_disable_skill",
        }
    )
    | _SOCIAL_WRITE_TOOLS,
    # Writes to the local workspace filesystem.
    "write_fs": frozenset({"write_file", "edit_file"}),
    # Shell / code execution.
    # pages_deploy is here because its build/start commands run in the agent's
    # sandbox exactly like `execute`, only later and for as long as the page
    # is up.
    "shell": frozenset(
        {"execute", "safe_shell", "safe_shell_tool", "node_run", "olano_update", "pages_deploy"}
    ),
    # Cross-agent command execution (AgentFather running a command inside
    # ANOTHER agent's workspace/sandbox). Present in EVERY profile, and the
    # runner additionally refuses to blanket-approve it on scheduled/cognition
    # turns or via remembered "always" rules — each command is approved by a
    # human individually (see core/runner.py). Only the ``hitl_enabled``
    # master switch un-gates it.
    # Scheduling work for another agent belongs here for the same reason, and
    # reaches further: a scheduled job makes that agent take a full model turn
    # on a prompt chosen here, repeatedly and with nobody watching, where
    # olano_run_agent_command is one command run once under supervision.
    # Rewriting an existing job's prompt reaches the same state, and run_now
    # makes the agent take that turn immediately.
    "cross_agent_exec": frozenset(
        {
            "olano_run_agent_command",
            "olano_scheduler_create",
            "olano_scheduler_update",
            "olano_scheduler_run_now",
            # Makes another agent take a full turn (its tools included) on a
            # supplied message, like run_now; the card names both.
            "olano_test_agent",
        }
    ),
    # Subagent delegation.
    "delegation": frozenset({"task"}),
}

# ── Profile → categories ─────────────────────────────────────────────────────
PROFILE_CATEGORIES: dict[str, frozenset[str]] = {
    "developer": frozenset({"financial", "secret_write", "cross_agent_exec"}),
    "safe": frozenset(
        {"financial", "secret_write", "secret_read", "write_external", "shell", "cross_agent_exec"}
    ),
    "production": frozenset(
        {
            "financial",
            "secret_write",
            "secret_read",
            "write_external",
            "write_fs",
            "shell",
            "delegation",
            "cross_agent_exec",
        }
    ),
}

_DEFAULT_PROFILE = "developer"


def profile_categories(profile: str | None) -> frozenset[str]:
    """Return the category set for a security profile (falls back to developer)."""
    return PROFILE_CATEGORIES.get(profile or _DEFAULT_PROFILE, PROFILE_CATEGORIES[_DEFAULT_PROFILE])


def tool_category(name: str) -> str | None:
    """Return the category a tool belongs to, or ``None`` if it needs no HITL."""
    for category, tools in CATEGORY_TOOLS.items():
        if name in tools:
            return category
    return None


def tool_overrides(security: Any) -> dict[str, str]:
    """Extract per-tool approval overrides from ``security.tools``.

    Shape: ``{tool_name: {"approval": "always" | "never"}}``. Tolerant of the
    legacy/empty placeholder dict and any malformed entries.
    """
    raw = getattr(security, "tools", None) or {}
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for name, cfg in raw.items():
            if isinstance(cfg, dict):
                decision = cfg.get("approval")
                if decision in ("always", "never"):
                    out[str(name)] = decision
    return out


def resolve_approval_set(config: Any) -> set[str]:
    """Compute the set of tool names that require human approval for an agent.

    Combines the profile baseline, the explicit ``hitl_*`` toggles, and per-tool
    overrides. Returns an empty set when ``hitl_enabled`` is explicitly ``False``
    (master kill switch) or there is no security config.
    """
    sec = getattr(config, "security", None)
    if sec is None:
        return set()
    if not getattr(sec, "hitl_enabled", True):
        return set()

    categories = set(profile_categories(getattr(sec, "profile", _DEFAULT_PROFILE)))
    if getattr(sec, "hitl_write_tools", False):
        categories.add("write_external")
    if getattr(sec, "hitl_shell", False):
        categories.add("shell")

    names: set[str] = set()
    for category in categories:
        names |= CATEGORY_TOOLS.get(category, frozenset())

    for tool_name, decision in tool_overrides(sec).items():
        if decision == "always":
            names.add(tool_name)
        elif decision == "never":
            names.discard(tool_name)

    return names
