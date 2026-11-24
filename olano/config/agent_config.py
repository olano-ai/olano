"""Pydantic models for agent.json5 configuration schema."""

from __future__ import annotations

import re
from enum import IntEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from olano.config.backend_config import BackendConfig, LocalShellBackendConfig
from olano.config.ops_defaults import (
    CORTEX_TOOL_NAMES,
    OPS_MANAGER_TOOL_NAMES,
    OPS_TOOL_NAMES,
    UNIVERSAL_AGENT_TOOL_NAMES,
)
from olano.security.delegate_guards import clamp_max_iterations, clamp_timeout_seconds

# ─────────────────────────────── Model validation ────────────────────────────


def _reject_utility_model(value: str | None) -> str | None:
    """Reject an Olano utility group used as a selectable model/brain.

    ``olano:vision`` / ``olano:olano-image`` are services the product calls on
    the agent's behalf, never a brain. Validating here catches a hand-edited
    ``agent.json5``, the raw-config editor, and CLI config loads in one place —
    every surface that parses an ``AgentConfig``. The runtime ``build_model``
    guards the paths that bypass config parsing (``/model``, per-turn override).
    The check reads the single source of truth in ``olano_gateway`` and imports
    it lazily so config parsing never pulls the model stack.
    """
    if value is None:
        return value
    from olano.models.olano_gateway import is_olano_utility_model

    if is_olano_utility_model(value):
        raise ValueError(
            f"'{value}' is an Olano utility service (image understanding / "
            "generation) the product calls on the agent's behalf, not a model you "
            "can assign as a brain. Use olano:turbo, olano:pro or olano:max."
        )
    return value


# ─────────────────────────────────────── Trust ───────────────────────────────


class TrustLevel(IntEnum):
    """Agent autonomy trust level (0–4)."""

    OBSERVER = 0  # Read-only, suggest via PULSE
    ASSISTANT = 1  # Write to memory/logs only
    COLLABORATOR = 2  # Create/edit workspace files, Docker shell
    AUTONOMOUS = 3  # Full workspace access, Docker, auto-approve low-risk
    DEVELOPER = 4  # Local shell, auto-approve most (dev/CI only)


# ─────────────────────────────────────── Security ────────────────────────────


class SecurityConfig(BaseModel):
    """Security middleware configuration."""

    # Security profile preset (overrides individual flags when set)
    profile: str = "developer"  # "safe" | "developer" | "production"

    # Trust level governing agent autonomy
    trust_level: int = TrustLevel.COLLABORATOR

    # PII scanning
    pii_scan_input: bool = True
    pii_scan_output: bool = True
    pii_action: str = "warn"  # "warn" | "redact" | "block"
    # Whether PII scanning also applies to agent↔agent (talk_to_agent) traffic.
    # Off by default: peer exchanges stay inside the runtime's trust boundary,
    # and redacting/blocking them breaks legitimate hand-offs (e.g. passing a
    # customer's email to the billing agent). Human-facing input/output is
    # still scanned per the two flags above regardless of this setting.
    pii_scan_peer: bool = False

    # Prompt-injection / prompt-extraction scanning
    input_scan: bool = True
    output_scan: bool = True

    # Skill security audit before activation
    skill_audit: bool = True
    skill_audit_level: Literal["permissive", "strict", "paranoid"] = "strict"

    # Web content sanitization
    web_scan: bool = False

    # Shell execution guardrails
    shell_guard: bool = True

    # Human-in-the-loop approval flow
    hitl_enabled: bool = False
    hitl_shell: bool = False
    hitl_write_tools: bool = False
    hitl_auto_approve_low_risk: bool = True
    hitl_timeout_seconds: int = 300

    # Tool permission system and rate limits
    tools: dict[str, Any] = Field(default_factory=dict)
    # Rate limits. Recognized keys:
    #   per_tool: {tool_name: max_calls_per_minute}   — per-agent tool-call cap
    #   messages: {per_minute, per_hour, per_day}      — per-agent INBOUND message
    #       cap across all platforms (null/omit a window = unlimited)
    #   per_platform: {platform: {per_minute, per_hour, per_day}}
    #                                                  — per-platform inbound cap
    #                                                    (telegram, slack, dashboard, …)
    #   requests_per_minute: int                       — legacy alias for
    #       messages.per_minute; honored ONLY when a rate_limits block is set
    #       explicitly (the schema default of 60 never activates limiting).
    # Message limits are opt-in and enforced at ingress (external inbound only);
    # scheduled/heartbeat/autonomous turns are exempt. Home-wide defaults live in
    # ~/.olano/config.yaml under `rate_limits:` (see config/system_settings.py).
    rate_limits: dict[str, Any] = Field(
        default_factory=lambda: {"requests_per_minute": 60, "per_tool": {}}
    )

    # SSRF protection and output scrubbing. The two domain lists are the
    # ORIGINAL spelling of the agent-level egress lists; the top-level
    # `egress_allow` / `egress_deny` on AgentConfig are the ones the dashboard
    # edits and the user guide teaches. Both spellings are unioned by
    # security/egress.py, so a list written here keeps counting.
    ssrf_protection: bool = True
    ssrf_allowed_domains: list[str] = Field(default_factory=list)
    ssrf_blocked_domains: list[str] = Field(default_factory=list)
    credential_scrub: bool = True
    scrub_patterns_path: str = "system/middleware/scrub_patterns.json5"

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_security_keys(cls, data: Any) -> Any:
        legacy = {"sandbox_mode", "shell_use_docker", "docker_image", "docker_network"}
        if isinstance(data, dict):
            found = legacy & data.keys()
            if found:
                raise ValueError(
                    f"Removed security config keys: {sorted(found)}. "
                    "Shell/sandbox execution is now configured via the agent-level "
                    "'backend:' block. See REARCHITECTURE_BACKENDS.md §5."
                )
        return data

    @model_validator(mode="after")
    def _apply_profile_defaults(self) -> SecurityConfig:
        """Apply security profile defaults for fields that were not explicitly set."""
        from olano.security.policies import get_profile

        profile = get_profile(self.profile)
        fields_set = set(self.model_fields_set)
        defaults = {
            "trust_level": int(profile.trust_level),
            "pii_action": profile.pii_action,
            "input_scan": profile.input_scan,
            "output_scan": profile.output_scan,
            "hitl_enabled": profile.hitl_enabled,
            "hitl_auto_approve_low_risk": profile.hitl_auto_approve_low_risk,
            "skill_audit_level": profile.skill_audit_level,
        }
        for field_name, value in defaults.items():
            if field_name not in fields_set:
                setattr(self, field_name, value)
        return self


class ShareSubjectConfig(BaseModel):
    """Subject selector for a shared path grant."""

    type: Literal["agent", "subagent", "all_subagents"] = "agent"
    name: str = "self"


class SharedPathConfig(BaseModel):
    """A host path grant exposed to the shell/agent runtime."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    source: str
    target: str
    mode: Literal["ro", "rw"] = "ro"
    persistence: Literal["session", "persistent"] = "persistent"
    subjects: list[ShareSubjectConfig] = Field(
        default_factory=lambda: [ShareSubjectConfig(type="agent", name="self")]
    )

    @model_validator(mode="after")
    def _normalize_target_and_subjects(self) -> SharedPathConfig:
        if not self.target.startswith("/"):
            self.target = f"/{self.target.lstrip('/')}"
        if not self.target.startswith("/shared/"):
            suffix = self.target.lstrip("/")
            self.target = f"/shared/{suffix}"
        if not self.subjects:
            self.subjects = [ShareSubjectConfig(type="agent", name="self")]
        return self


# ─────────────────────────────────────── Budget / Cost ───────────────────────


class BudgetConfig(BaseModel):
    """Per-agent token / cost budget controls."""

    # Hard spend limits (USD)
    daily_usd: float | None = None
    weekly_usd: float | None = None
    monthly_usd: float | None = None

    # PULSE alert thresholds (fraction of limit)
    alert_at: float = 0.8  # alert at 80% usage

    # Auto-switch to cheaper model when nearing limit
    fallback_model: str | None = None

    # Disable model calls entirely when limit is exceeded
    hard_stop: bool = False


class PromptCacheConfig(BaseModel):
    """Prompt cache layout controls."""

    optimize_boundaries: bool = True
    sort_tools: bool = True


# ─────────────────────────────────────── Commands ────────────────────────────


class CustomCommand(BaseModel):
    """A user-defined slash command that sends a prompt to the agent.

    Typing ``/<name> [extra]`` on any chat surface sends ``prompt`` to the agent
    as the turn's message. If ``prompt`` contains ``{args}`` it is replaced with
    whatever the user typed after the command; otherwise the extra text (if any)
    is appended.
    """

    name: str
    prompt: str
    description: str = ""

    @model_validator(mode="after")
    def _normalize_and_validate(self) -> CustomCommand:
        import re

        self.name = self.name.strip().lstrip("/").lower()
        if not self.name:
            raise ValueError("custom command name must not be empty")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", self.name):
            raise ValueError(
                f"invalid custom command name {self.name!r}: use letters, digits, '-' or '_'"
            )
        from olano.config.commands_catalog import ALIAS_TO_CANONICAL

        if self.name in ALIAS_TO_CANONICAL:
            raise ValueError(f"custom command {self.name!r} collides with a built-in command")
        if not self.prompt.strip():
            raise ValueError(f"custom command {self.name!r} needs a non-empty prompt")
        return self


class CommandsConfig(BaseModel):
    """Per-agent control over which chat slash commands are exposed.

    Built-in commands are governed by :data:`commands_catalog.COMMAND_SPECS`;
    sensitive ones default to *off*. ``enabled`` stores only the deviations from
    those defaults (so a fresh agent keeps an empty map and new built-ins pick up
    their catalog default automatically). ``custom`` adds user-defined commands.

    Applies to every chat surface (messaging platforms/channels + the dashboard
    chat).
    """

    # canonical command name → exposed?  (absent ⇒ use the catalog default)
    enabled: dict[str, bool] = Field(default_factory=dict)
    custom: list[CustomCommand] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize(self) -> CommandsConfig:
        from olano.config.commands_catalog import canonical_command

        # Fold any alias keys onto their canonical names so lookups are stable.
        folded: dict[str, bool] = {}
        for key, value in self.enabled.items():
            folded[canonical_command(str(key))] = bool(value)
        self.enabled = folded

        seen: set[str] = set()
        for cmd in self.custom:
            if cmd.name in seen:
                raise ValueError(f"duplicate custom command {cmd.name!r}")
            seen.add(cmd.name)
        return self

    def is_exposed(self, name: str) -> bool:
        """Whether built-in *name* (canonical or alias) is exposed to chat users."""
        from olano.config.commands_catalog import canonical_command, default_exposed

        canonical = canonical_command(name)
        if canonical in self.enabled:
            return self.enabled[canonical]
        return default_exposed(canonical)

    def find_custom(self, name: str) -> CustomCommand | None:
        target = name.strip().lstrip("/").lower()
        for cmd in self.custom:
            if cmd.name == target:
                return cmd
        return None


# ─────────────────────────────────────── PULSE ───────────────────────────────


class PulseConfig(BaseModel):
    """Proactive intelligence (PULSE) configuration."""

    enabled: bool = True

    # Delivery preferences
    in_channels: bool = False  # Push via configured channels
    email_digest: bool = False  # Daily email digest
    digest_time: str = "09:00"  # Local time for digest

    # Quiet hours (24h format, empty = always active)
    quiet_hours_start: str = ""  # e.g. "22:00"
    quiet_hours_end: str = ""  # e.g. "08:00"

    # Rate limiting
    max_items_per_session: int = 5
    max_items_per_day: int = 20
    generated_items_per_cycle: int = 3
    pulse_item_ttl_hours: float = 168.0
    min_turns_for_generation: int = 4

    # Curated generation behaviour
    curation_cadence: Literal["session", "daily", "weekly"] = "daily"
    curation_interval_hours: float = 24.0
    focus_topics: list[str] = Field(default_factory=list)

    # Sources to enable. PULSE is the curated editorial lane; OPS and approvals
    # remain available as separate operational surfaces.
    sources: list[str] = Field(
        default_factory=lambda: ["agent", "user", "approvals", "ops"]
    )  # "agent" | "user" | "approvals" | "ops"


# ─────────────────────────────────────── Wallet ──────────────────────────────


class WalletConfig(BaseModel):
    """Web3 wallet security configuration."""

    enabled: bool = False
    mode: str = "cold"  # "cold" | "hot"
    chains: list[str] = Field(default_factory=lambda: ["ethereum"])

    # Hot-mode auto-lock
    hot_mode_lock_minutes: int = 30
    lock_on_session_end: bool = True

    # Spending limits (per-chain, USD)
    spending_limits: dict[str, dict[str, float]] = Field(default_factory=dict)

    # Approval policy
    require_approval: str = "always"  # "always" | "high_value_only" | "never"


# ─────────────────────────────────────── existing models (unchanged) ─────────


class MemoryConfig(BaseModel):
    markdown: bool = True
    rag: bool = False
    rag_collection: str = "lionclaw_memory"
    self_learning: bool = True
    # How often (seconds) the background self-learning job runs
    self_learning_interval: int = 3600
    # Customer-facing isolation: when True, long-term memory is namespaced per
    # authenticated channel sender *and* the platform they arrived on
    # (``<olano-home>/agents/<agent>/workspace/customers/<platform>-<user_id>/``)
    # instead of one shared MEMORY.md, so a single bot serving many customers
    # can never mix their data. Replaces the global markdown memory tools with
    # context-scoped ``remember``/``recall``/``forget_me`` tools and disables the
    # shared self-learning loop (which would distill all customers into one
    # file). See ``olano/memory/customer_memory.py``.
    per_customer: bool = False


class CustomerServiceConfig(BaseModel):
    """ "This is a Customer Service Agent" — one switch, works after creation.

    Before this existed, the only thing that gated the customer tools was
    ``memory.per_customer``, which had no dashboard control; the unrelated
    ``human_only`` flag (agent-to-agent peers, not channels) was the one
    operators could see and reasonably mistook for it. Enabling this turns on
    per-customer memory isolation, so the tools land through the existing
    ``per_customer`` path in ``core/builder._build_memory_tools`` — see the
    validator on ``AgentConfig``.

    ``prompt_extension`` is appended verbatim to the system prompt and is
    operator-owned: whatever it holds survives every save and reload untouched.
    Left **blank** while the switch is on, it resolves to the support-desk
    default (``DEFAULT_MULTI_USER_PROMPT_EXTENSION``) in the validator below —
    a shared agent should never face strangers with no privacy or scope rules.
    The dashboard still offers both suggestions behind copy buttons, so
    replacing the default with the privacy-only variant (or your own wording) is
    one paste away.
    """

    enabled: bool = False
    # Blank here means "fill with the default" while `enabled` is on; see
    # `_sync_customer_service_mode`. Non-blank text is never rewritten.
    prompt_extension: str = ""

    # ── Per-user scheduling (Type-2 jobs) ────────────────────────────────────
    # When True, a multi-user agent gets identity-injected scheduler tools
    # (schedule_my_task / list_my_scheduled_tasks / cancel_my_scheduled_task):
    # an end-user can ask for their own reminders, and the job is stored with
    # THEIR platform + sender id (resolved from the request context, never a
    # tool argument). When such a job fires, the run binds that identity, so
    # remember/recall/execute_action operate on the right customer, and the
    # turn's reply is delivered back to them. Off by default: enabling it means
    # end-users can create recurring model turns, i.e. spend the owner's tokens
    # on a schedule — the caps below bound that.
    user_scheduling: bool = False
    # Most enabled scheduled jobs one end-user may have at a time on this
    # agent. Creation beyond the cap is refused with a message telling the
    # user to cancel one first.
    max_jobs_per_user: int = 3
    # Frequency floor for a user-created recurring job: its cron may never
    # fire more often than this many minutes apart (default hourly). Guards
    # against "every minute" schedules burning tokens; one-time jobs are
    # unaffected. Together with max_jobs_per_user this bounds a user's daily
    # fires at max_jobs_per_user * (1440 / min_job_interval_minutes).
    min_job_interval_minutes: int = 60

    # ── Customer↔customer connections (consent-based messaging) ──────────────
    # When True, customers get the connection tools (olano/connections/):
    # A asks for an invite, forwards the generated message out-of-band, and B
    # accepts by sending its `connect <CODE>` line to the agent in B's own
    # authenticated chat — redemption IS the consent. send_to_connection can
    # then relay short messages between exactly those two, platform-routed,
    # with attribution and either side able to disconnect. Nobody ever
    # addresses anybody by platform id, so there is no recipient parameter
    # for a prompt injection to re-point. Off by default: only agents whose
    # product needs person-to-person relay (landlord↔tenant, business↔its
    # customers) should carry it.
    connections: bool = False
    # Most active connections one customer may hold at a time.
    max_connections_per_user: int = 25
    # Per-direction, per-connection daily message cap for send_to_connection.
    max_connection_sends_per_day: int = 20
    # Hours before an unaccepted invite code expires (default 7 days).
    connection_invite_ttl_hours: int = 168
    # Operator-written "how to reach this agent" line embedded in generated
    # invites (e.g. "Telegram: @MyAgentBot · WhatsApp: +65 8123 4567"). When
    # empty, the runtime asks live channel adapters for what they know about
    # themselves, and otherwise tells the inviter to add contact details when
    # forwarding.
    connection_contact_info: str = ""

    # ── Per-customer credentials (each customer brings their own token) ──────
    # When True, customers get set_my_credential / list_my_credentials /
    # delete_my_credential: identity-injected tools that store a secret
    # encrypted in the vault under (agent, customer, name) — a namespace fully
    # separate from the operator's own vault entries. Only names listed in
    # customer_credential_names may be stored, and for those names resolution
    # FAILS CLOSED during a customer-bound turn: the customer's own value or
    # nothing — never a silent fallback to the operator's token (which would
    # e.g. mirror customer A's books into the operator's spreadsheet). With no
    # customer bound (operator chat, classic scheduled runs) the normal chain
    # applies unchanged. Declared values are also injected into that
    # customer's `allowed_actions`/fan-out action runs as environment
    # variables. `forget_me` erases them.
    user_credentials: bool = False
    # The exact env-style names customers may store (e.g.
    # ["GOOGLE_SHEETS_TOKEN"]). Empty list = the tools accept nothing, so
    # declaring the names is part of enabling the feature.
    customer_credential_names: list[str] = Field(default_factory=list)


class MessagePolicyConfig(BaseModel):
    """What this agent accepts from a sender, and how much it says back.

    Three independent caps, all **off by default** so no existing agent changes
    behaviour. They exist for the same reason: an agent whose chat address is
    public is exposed to whatever a stranger types, and every one of those
    messages costs a model turn.

    - **Inbound size** (``max_input_chars``) — a deterministic ingress cap. Runs
      before the model, so an oversized message costs nothing.
    - **Silence** (``ignore_patterns`` / ``ignore_rules``) — two mechanisms with
      different prices. A *pattern* is a regex matched at ingress: free, exact,
      and it cannot be talked out of. A *rule* is a sentence the model judges,
      so it handles "not written in English" and "gibberish", which no regex
      can — but the judgement happens inside the turn, so the turn is paid for.
      Pair rules with ``max_input_chars`` + ``max_output_tokens`` and that turn
      is bounded; use patterns for anything a regex can already decide.
    - **Outbound size** (``max_output_tokens``) — a hard ceiling on one reply.

    Enforcement is at **external ingress only** — messenger channels, the
    dashboard/REST chat and the OpenAI-compatible server. Scheduled, heartbeat
    and cognition turns never pass through these seams, so an agent can still
    write a long report to disk on a timer while answering strangers in two
    sentences.
    """

    # ── Inbound size ────────────────────────────────────────────────────────
    max_input_chars: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Longest inbound message this agent accepts, counted in characters "
            "of the sender's text (attachments and the runtime's own [from …] "
            "prefix are not counted). Checked at ingress, before any model call, "
            "so an over-long message costs nothing. Leave empty for unlimited "
            "(the default). Typical anti-abuse values: 500-2000 for a support "
            "desk. What happens on a breach is `on_oversize`."
        ),
    )
    on_oversize: Literal["reject", "truncate", "ignore"] = Field(
        default="reject",
        description=(
            "What to do with a message longer than `max_input_chars`. "
            "'reject' (default) replies with a short notice telling the sender "
            "the limit and runs no model turn. 'truncate' cuts the text to the "
            "limit and answers the trimmed message — cheap, but the agent "
            "silently answers something the sender did not write, so prefer it "
            "only where messages are naturally long and the tail is padding. "
            "'ignore' drops the message with no reply at all. Has no effect "
            "while `max_input_chars` is empty."
        ),
    )
    oversize_notice: str = Field(
        default="",
        description=(
            "Your own wording for the 'reject' reply, e.g. \"Please keep "
            'messages under 500 characters.". Leave empty (the default) for '
            "the built-in notice, which names the limit and the length the "
            "sender actually sent. Ignored when `on_oversize` is not 'reject'."
        ),
    )

    # ── Silence ─────────────────────────────────────────────────────────────
    ignore_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Python regular expressions, one per line. An inbound message whose "
            "text matches any of them is dropped at ingress — no model turn, no "
            "reply (or `ignore_notice`, if you set one). Matching is "
            "case-insensitive and unanchored, so `crypto` matches anywhere in "
            "the message; use `^...$` to require the whole message. Examples: "
            "`^\\W*$` (nothing but punctuation), `\\b(?:buy|cheap) followers\\b` "
            "(a spam pitch), `https?://bit\\.ly/` (a shortened link). A pattern "
            "that fails to compile is logged once and skipped — it never breaks "
            "message handling. Empty (the default) means nothing is dropped by "
            "pattern. This is the cheap half of ignoring; `ignore_rules` is for "
            "what a regex cannot decide."
        ),
    )
    ignore_rules: list[str] = Field(
        default_factory=list,
        description=(
            "Plain-language conditions for staying silent, one per line, e.g. "
            '"The message is not written in English" or "The message is '
            'nonsense, empty of content, or a test of the bot". They are added '
            "to the agent's system prompt with an instruction to answer with a "
            "silence marker instead of a reply; the runtime strips the marker "
            "and sends nothing. Because the model decides, this handles "
            "language and intent, and it also costs a model turn even when it "
            "stays silent — cap that turn with `max_input_chars` and "
            "`max_output_tokens`, and move anything a regex can already catch "
            "into `ignore_patterns`. Write each rule as a condition that is "
            "true or false about one message, not as an instruction. Empty (the "
            "default) adds nothing to the prompt."
        ),
    )
    ignore_notice: str = Field(
        default="",
        description=(
            "What an ignored sender sees. Leave empty (the default) for true "
            "silence — no message is sent at all, which is what you want "
            "against spam, since any reply confirms a live bot on the other "
            'end. Set text (e.g. "Sorry, I can only help in English.") to '
            "send that one line instead, which is friendlier for a rule like a "
            "language restriction where the sender is a real customer. Applies "
            "to both `ignore_patterns` and `ignore_rules`, and to the 'ignore' "
            "setting of `on_oversize`."
        ),
    )

    # ── Outbound size ───────────────────────────────────────────────────────
    max_output_tokens: int | None = Field(
        default=None,
        ge=16,
        description=(
            "Hard ceiling on the tokens the model may generate per reply. "
            "Translated to whichever parameter the provider uses, so the same "
            "number works across Anthropic, OpenAI, Gemini, Ollama and the "
            "olano:* tiers. Roughly 1 token ≈ 4 characters of English, so 300 "
            "is about a short paragraph. Leave empty (the default) for the "
            "provider's own maximum. Three caveats: a reply that hits the "
            "ceiling stops mid-sentence rather than summarising itself, and is "
            "NOT auto-continued (the continuation retry is switched off while "
            "this is set — otherwise every capped reply would cost several "
            "extra turns); on reasoning models the ceiling counts thinking "
            "tokens too, so a low value can return an empty reply — keep it "
            "above ~1024 there or set `reasoning_effort` to none/low; and it "
            "applies to the agent's own replies only, never to its subagents, "
            "whose output is working material rather than an answer."
        ),
    )

    @field_validator("ignore_patterns", "ignore_rules", mode="before")
    @classmethod
    def _clean_lines(cls, value: Any) -> Any:
        """Accept a single string (one per line) as well as a list, and drop
        blanks — the dashboard textarea and a hand-edited config both arrive
        here, and a stray empty line must not become a regex that matches
        everything."""
        if isinstance(value, str):
            value = value.splitlines()
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return value

    @model_validator(mode="after")
    def _reject_uncompilable_patterns(self) -> MessagePolicyConfig:
        """Fail the save, not the message.

        A bad regex is skipped at runtime (ingress must never break), which
        would leave an operator with a rule that silently does nothing. Catch it
        here instead, where the dashboard can show the error next to the field.
        """
        import re as _re

        for pattern in self.ignore_patterns:
            try:
                _re.compile(pattern)
            except _re.error as exc:
                raise ValueError(
                    f"message_policy.ignore_patterns: {pattern!r} is not a valid "
                    f"regular expression ({exc})."
                ) from exc
        return self


class MonetizationPlan(BaseModel):
    """Pricing + copy for an agent's end-user subscription (non-secret).

    Secrets (``STRIPE_API_KEY``, ``STRIPE_WEBHOOK_SECRET``) are NOT here — they
    live in the per-agent credential vault so each owner uses their own Stripe
    account. Only the ids/amounts/copy needed to *present* the offer live here.
    """

    price_cents: int = 500  # display amount (Stripe price is the source of truth)
    currency: str = "usd"
    interval: Literal["month", "year"] = "month"
    # Owner's own recurring Stripe Price id (``price_...``). Empty ⇒ Stripe off.
    stripe_price_id: str = ""
    # Telegram Stars amount (XTR units) for the recurring invoice. 0 ⇒ Stars off.
    stars_amount: int = 0
    product_name: str = "Subscription"
    offer_copy: str = "Subscribe to keep receiving updates."
    # Where Stripe Checkout returns the user (defaults to a Telegram deep link).
    success_url: str = ""
    cancel_url: str = ""


class MonetizationConfig(BaseModel):
    """Charge an agent's END-USERS a subscription, with a free trial.

    Disabled by default, so every existing agent is unaffected — same
    backward-compat posture as :attr:`MemoryConfig.per_customer`. The agent
    *owner* (a trusted allowlist entry) always bypasses the gate and is never
    charged. See ``olano/monetization/``.
    """

    enabled: bool = False
    # Which payment rails to offer. Both can be on at once.
    providers: list[Literal["stripe", "telegram_stars"]] = Field(default_factory=list)
    # Free-trial length for a first-seen end-user before the paywall applies.
    trial_days: int = 3
    plan: MonetizationPlan = Field(default_factory=MonetizationPlan)
    # Channels this applies to; empty ⇒ every human channel on the agent.
    channels: list[str] = Field(default_factory=list)
    # Whether to gate group chats. Default False: billing a shared group is
    # ambiguous, so groups are left open and users are told to DM to subscribe.
    gate_groups: bool = False


#: Default ``cognition.approval_required``. Mirrored in the user guide (§4.19,
#: §7.2) and ``approval_guard.KNOWN_CATEGORIES``; a test pins the three in sync.
DEFAULT_APPROVAL_REQUIRED: tuple[str, ...] = ("external_send", "irreversible", "identity_edit")


class CognitionConfig(BaseModel):
    """Per-agent controls for background reasoning workers (pulse, self-learning,
    memory gardening, ideas/brainstorming/prototyping, skillsmith).

    Every on/off field is three-state: ``None`` ⇒ inherit the home-wide
    :mod:`olano.config.system_settings` value, ``True``/``False`` ⇒ this agent
    overrides it in that direction. See ``system_settings._resolve``.
    """

    # Master per-agent switch for all cognition. None ⇒ inherit the global
    # ``cognition.enabled``. This was a plain ``bool = True``, which made it the
    # one switch with no way to say "inherit" — so it could only ever turn
    # Cortex off, never on for an agent the fleet default had turned off.
    # A literal ``true`` on disk means force-on, which is what it meant when
    # someone clicked the master toggle to put it there.
    enabled: bool | None = None

    # Tiered models — None inherits from system settings / the agent's own model.
    fast_model: str | None = None
    deep_model: str | None = None

    _reject_utility_cognition_models = field_validator("fast_model", "deep_model")(
        _reject_utility_model
    )

    # Self-learning depth + autonomy posture.
    deep_self_learning: bool = True
    autonomous: bool | None = None  # None ⇒ defer to the global autonomy switch
    # "Fully Autonomous": auto-approve cognition outputs (learned skills,
    # self-improve proposals, actionable pulse items) without a human click.
    # None ⇒ defer to the global system_settings.auto_approve switch.
    auto_approve: bool | None = None
    # Cadence preset override: None ⇒ defer to the global intensity.
    intensity: str | None = None  # "relaxed" | "balanced" | "aggressive"
    # Categories that ALWAYS wait for a human, whatever ``auto_approve`` says.
    # Enforced at both places Fully Autonomous approves things: the Pulse sweep
    # (``orchestrator.auto_approve_cognition_items``) and HITL decisions on a
    # cognition-rooted turn (``runner._resolve_autonomous_decisions``), through
    # the one classifier in ``cognition/approval_guard.py``:
    #   "identity_edit"  - any write to SOUL.md / AGENTS.md / IDENTITY.md
    #   "external_send"  - the ``write_external`` HITL category
    #   "irreversible"   - the ``financial`` + ``secret_write`` HITL categories
    # Set to ``[]`` for an agent whose owner really wants everything unattended.
    # A concrete default rather than three-state ``None`` on purpose: the user
    # guide documents this as a per-agent list, and an inheriting global would
    # need the docs to change first.
    approval_required: list[str] = Field(default_factory=lambda: list(DEFAULT_APPROVAL_REQUIRED))

    # Scheduled cadences (cron strings). Three-state like every other override
    # here: ``None`` ⇒ inherit the cadence the home-wide ``intensity`` preset
    # gives the phase (``cognition/orchestrator.PHASES``), staggered per agent
    # so a fleet does not fire in the same minute. A concrete default string
    # here would make the intensity table unreachable for these four phases
    # (a truthy default always wins the ``or``) and pin every agent to the
    # identical minute, the same shape as ``enabled`` and the worker budget
    # below. An explicit value is honoured verbatim, never staggered.
    memory_garden_cron: str | None = None
    pulse_agent_cron: str | None = None
    idea_expand_cron: str | None = None  # opt-in autonomous idea research
    # Autonomous self-optimization of identity/behavior files and skills.
    workspace_optimize_cron: str | None = None
    skill_librarian_cron: str | None = None

    # Cost / runaway guards. `worker_max_iterations` is a langgraph
    # `recursion_limit` — SUPER-STEPS (~4 per model turn with the olano
    # middleware stack), not tool calls; see delegate_guards for the sizing
    # math. Both are resolved by `system_settings.effective_worker_budget` for
    # every background run whose call site doesn't pass an explicit budget.
    #
    # THREE-STATE, like every other override in this block: `None` ⇒ inherit the
    # home-wide `cognition.worker_max_iterations` / `worker_timeout_seconds`.
    # They were plain ints defaulting to the shared constants, which reads as
    # "every agent has an opinion, and it is 40/600" — so a fleet-wide raise
    # could never reach a single agent (`getattr` returns the class default
    # whether or not the key is on disk). That is the same shape as the
    # `cognition.enabled` bug: a default that masquerades as a choice.
    max_deep_runs_per_day: int = 12
    worker_max_iterations: int | None = None
    worker_timeout_seconds: int | None = None
    force_tier: str | None = None  # "fast" | "deep" | None

    # The funnel's autonomous decisions (brainstorm → idea → prototype →
    # project), three-state like everything else here: ``None`` ⇒ inherit the
    # home-wide ``cognition.promote_threshold`` / ``max_promotions_per_run`` /
    # ``max_self_scheduled_tasks`` / ``auto_discard``. Resolved by
    # ``system_settings.effective_promote_threshold`` and siblings; bounds in
    # ``config/cortex_funnel.py``. Read only through those resolvers.
    promote_threshold: int | None = None
    max_promotions_per_run: int | None = None
    max_self_scheduled_tasks: int | None = None
    auto_discard: bool | None = None

    @field_validator(
        "promote_threshold", "max_promotions_per_run", "max_self_scheduled_tasks", mode="before"
    )
    @classmethod
    def _clamp_funnel_knob(cls, v: object, info: Any) -> int | None:
        """Clamp a hand-edited knob instead of rejecting the config; empty or
        unparseable keeps the inherit semantics (same rule as the worker budget)."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        try:
            value = int(v)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return None
        from olano.config.cortex_funnel import (
            clamp_max_promotions,
            clamp_promote_threshold,
            clamp_self_scheduled,
        )

        clamp = {
            "promote_threshold": clamp_promote_threshold,
            "max_promotions_per_run": clamp_max_promotions,
            "max_self_scheduled_tasks": clamp_self_scheduled,
        }[str(info.field_name)]
        return clamp(value)

    # Per-feature on/off overrides (None ⇒ defer to global system setting).
    # Names mirror olano.config.system_settings.FEATURES + the Cortex tiles.
    self_learning: bool | None = None
    brainstorming: bool | None = None
    idea_generation: bool | None = None
    prototyping: bool | None = None
    skills_optimization: bool | None = None
    workspace_optimization: bool | None = None
    memory_optimization: bool | None = None
    pulse: bool | None = None

    @field_validator("worker_max_iterations", "worker_timeout_seconds", mode="before")
    @classmethod
    def _clamp_worker_budget(cls, v: object, info: Any) -> int | None:
        """Clamp a hand-edited budget instead of rejecting the whole config.

        A nonsense value here (``0``, ``"lots"``, ``999999``) must not make
        ``agent.json5`` unloadable — that would take the agent down over a
        preference. Empty/None keeps the inherit semantics; anything unparseable
        falls back to inheriting too.
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        try:
            value = int(v)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return None
        if info.field_name == "worker_max_iterations":
            return clamp_max_iterations(value)
        return clamp_timeout_seconds(value)

    # Per-worker enable flags (default on).
    enable_memory_gardener: bool = True
    enable_pulse_agent: bool = True
    enable_idea_expansion: bool = True
    enable_skillsmith: bool = True
    enable_workspace_optimizer: bool = True
    enable_skill_librarian: bool = True


class AutonomyConfig(BaseModel):
    """Per-agent overrides for **chat self-configuration** — what this agent may
    change about itself from inside a conversation (the ``self_*`` tools).

    Every field is three-state, same rule as :class:`CognitionConfig`:
    ``None`` ⇒ inherit the home-wide ``agent_autonomy:`` block in
    ``config.yaml`` (whose fields all default **on**), ``True``/``False`` ⇒
    this agent overrides in that direction. **Never read these fields
    directly** — ``None`` reads as falsy and would treat every unconfigured
    agent as denied; resolve through
    ``olano.config.system_settings.autonomy_allowed()``.

    These knobs gate what the AGENT may do to itself mid-conversation. They
    are unrelated to the human-side permission system (``is_admin``,
    ``agent_access``) and to the fleet-scoped ``olano_admin`` toolkit
    AgentFather holds.
    """

    # Search/read the archetype library (Teams & Experts) — read-only.
    explore_archetypes: bool | None = None
    # Switch the agent's own `model` (validated; applies after restart).
    change_model: bool | None = None
    # Rewrite the agent's own `system_prompt`.
    modify_prompt: bool | None = None
    # Restart itself so config changes take effect.
    restart_self: bool | None = None
    # Add/toggle subagents on itself (custom, or drafted from an archetype).
    add_subagents: bool | None = None
    # Search the MCP directory and add/remove/authorize MCP servers on itself.
    connect_mcps: bool | None = None
    # Wire messaging channels (Telegram, WhatsApp, …) onto itself, including
    # the QR pairing flow for QR platforms.
    connect_platforms: bool | None = None
    # List its own skills and switch them on/off for itself (`disabled_skills`).
    # Never installs, edits or deletes a skill — the switch only decides which
    # of the already-installed ones this agent loads.
    manage_skills: bool | None = None
    # Switch its own inbound exposure (MCP / A2A) and mint access tokens for
    # outside callers via `self_set_external_access` / `magic_link_mint_access_token`.
    # The tools also demand an admin human on the turn; this only decides
    # whether the agent holds them.
    external_access: bool | None = None
    # Send magic links (`magic_link_*` tools): PIN-protected pages a human
    # opens in their own browser to sign in to a service, hand over a key
    # straight into the vault, connect a messenger by QR or token, edit the
    # agent's instructions or a file, or open the dashboard on their phone.
    # Owner-only and HITL-gated at call time regardless of this switch; the
    # `magic_links:` block in config.yaml (`enabled`) is the hard off switch.
    magic_links: bool | None = None


class ScheduleConfig(BaseModel):
    """A single scheduled job for an agent."""

    # APScheduler cron expression fields (all optional; defaults = every minute)
    cron: str | None = None  # shorthand "minute hour dom month dow"
    minute: str = "*"
    hour: str = "*"
    day: str = "*"
    month: str = "*"
    day_of_week: str = "*"
    # IANA timezone the cron fields are interpreted in (e.g. "America/Costa_Rica").
    # None ⇒ the scheduler's default (server local time). Set this so an agent's
    # runs land at the intended local wall-clock time regardless of where the
    # runtime happens to be hosted.
    timezone: str | None = None
    # Message to send to the agent when the schedule fires. Required for a
    # normal entry; a `for_each_customer` entry may omit it when `action` is
    # set (the validator below enforces exactly the old error otherwise).
    message: str = ""
    # Thread id to use for the scheduled run (for memory continuity)
    thread_id: str | None = None
    # Human-friendly label shown in the dashboard Scheduler + Config views.
    # None ⇒ the UI falls back to a truncated `message`.
    name: str | None = None
    # Pause switch. False leaves the entry in agent.json5 but the scheduler
    # skips registering it (mirrors a paused DB task). Lets the dashboard
    # toggle a config job off without deleting it.
    enabled: bool = True
    # Model THIS job runs on, as `provider:model` (e.g. "olano:turbo",
    # "anthropic:claude-sonnet-4-6"). None/empty ⇒ the agent's configured
    # model. Lets a cheap tier do the nightly digest and a bigger one the
    # weekly analysis on the same agent; the run bills the model that ran.
    # A model whose provider is not connected fails the run with a history
    # error naming it rather than silently falling back.
    model: str | None = None
    # Reasoning effort for THIS job's runs: one of the levels the model that
    # runs the job accepts (`model` when set, else the agent's configured
    # model), the same per-turn override the chat's effort chip sets. None ⇒
    # the agent's own setting. Validated at save time by the dashboard and the
    # scheduler tools (scheduler/overrides.py); at run time a stale value is
    # dropped by the model builder with a log line rather than failing the
    # job, so an entry written by hand never stops an agent from booting.
    reasoning_effort: str | None = None

    # ── Multi-user fan-out (Type-1 jobs) ─────────────────────────────────────
    # When True, this job runs once PER CUSTOMER folder under the agent's
    # workspace `customers/` directory instead of once for the agent: for each
    # customer, either `action` (an `allowed_actions` name) runs as a
    # deterministic script — cwd = that customer's folder, identity in the
    # environment, no model turn — or `message` runs as ONE agent turn with
    # that customer's identity bound (so remember/recall/execute_action all
    # resolve them, and the reply is delivered back to them). In turn mode the
    # message may carry `{customer_key}` / `{platform}` / `{user_id}`
    # placeholders, substituted per customer by the runtime — for prompts that
    # must reference owner-keyed tools (see
    # scheduler/multi_user.render_customer_message). Requires
    # `memory.per_customer`; a normal agent's entries never set this and run
    # through the unchanged single-run path.
    for_each_customer: bool = False
    # Name of an `allowed_actions` entry to run per customer (deterministic,
    # no LLM). If the script prints JSON with a top-level "notify" string, the
    # runtime sends that text to the customer on their own platform; anything
    # else is logged only. Only valid together with `for_each_customer`.
    action: str | None = None

    @model_validator(mode="after")
    def _check_fan_out_shape(self) -> ScheduleConfig:
        """Fail loudly at load time — a silently ignored field is how drift starts."""
        if self.action is not None and not self.for_each_customer:
            raise ValueError(
                "schedule entry: `action` is only valid with `for_each_customer: true` "
                "(a normal scheduled job sends `message` to the agent)"
            )
        if self.for_each_customer:
            if not self.action and not self.message.strip():
                raise ValueError(
                    "schedule entry: a `for_each_customer` job needs `action` "
                    "(a script per customer) or `message` (an agent turn per customer)"
                )
        elif not self.message.strip():
            # Preserves the pre-fan-out contract: `message` was a required
            # field, so an entry without one must still refuse to parse.
            raise ValueError("schedule entry: `message` is required")
        return self


class HeartbeatConfig(BaseModel):
    """Heartbeat poll settings (HEARTBEAT.md checks) for this agent.

    ``None`` fields inherit the home-wide defaults from
    ``~/.olano/config.yaml → heartbeat:`` (enabled, hourly). Resolution
    lives in ``olano/config/system_settings.py`` —
    :func:`effective_heartbeat_enabled` / :func:`effective_heartbeat_interval`.
    """

    # None → inherit the system-wide switch (default: on).
    enabled: bool | None = None
    # Minutes between heartbeat polls (clamped to 1–1440 at resolution time).
    # None → inherit the system-wide interval (default: 60 = hourly).
    interval_minutes: int | None = None


class IssuesPollConfig(BaseModel):
    """Issue-tracker poll settings for this agent.

    Every agent gets its own issues poll — deliberately independent of the
    heartbeat, which is not enabled everywhere. ``None`` fields inherit the
    home-wide defaults from ``~/.olano/config.yaml → issues:``
    (auto-process on, every 15 minutes). Resolution lives in
    ``olano/config/system_settings.py`` —
    :func:`effective_issues_auto_process` / :func:`effective_issues_poll_minutes`.

    ``auto_process: false`` puts the agent in manual mode: it never polls on
    its own, and open assigned issues wait for a human to prompt it ("check
    your issues"), a scheduled job with such a message, or the dashboard.
    """

    # None → inherit the system-wide switch (default: on).
    auto_process: bool | None = None
    # Minutes between issue polls (clamped to 1–1440 at resolution time).
    # None → inherit the system-wide interval (default: 15).
    poll_minutes: int | None = None


class GmailWatchConfig(BaseModel):
    """Wake the agent when new Gmail matching a search arrives.

    Off by default. When ``enabled``, the scheduler checks the agent's
    connected Gmail every ``poll_minutes`` for messages matching ``query`` and
    starts one agent turn listing the new ones (ID, sender, subject, Gmail's
    receive time) followed by ``prompt``. Gmail offers push delivery only
    through a Google Cloud Pub/Sub topic, which most deployments do not have,
    so this is a poll; at the default interval a message is picked up within
    five minutes. Runs live in ``olano/scheduler/gmail_watch.py``.

    The first check after enabling records the current time and delivers
    nothing, so switching it on never replays an inbox. Message ids already
    delivered are remembered in the workspace state folder, so a restart or an
    overlapping search never delivers the same message twice.

    Email is written by people outside the deployment, so a watched turn runs
    with the same caution as an inbound webhook: low-risk tool calls proceed
    and anything that needs approval waits in the dashboard Approvals queue.
    """

    enabled: bool = Field(
        default=False,
        description=(
            "Start a turn when new Gmail matching `query` arrives. Needs the agent's "
            "Google connection (Connections); without it the watch reports "
            "'Gmail not connected' and reads nothing."
        ),
    )
    query: str = Field(
        default="",
        description=(
            "Gmail search, exactly as typed in Gmail's search box, e.g. "
            "'from:(@broker.example) has:attachment'. Empty watches the inbox (in:inbox)."
        ),
    )
    poll_minutes: int = Field(
        default=5,
        ge=1,
        le=1440,
        description="Minutes between checks (1-1440). New mail is picked up within this.",
    )
    prompt: str = Field(
        default="",
        description=(
            "What the agent should do with the new messages. Empty tells it to read "
            "each one, save the attachments it needs and act on its instructions."
        ),
    )
    max_per_check: int = Field(
        default=10,
        ge=1,
        le=50,
        description="At most this many messages per turn, oldest first (1-50); the rest wait.",
    )


class ContextWindowConfig(BaseModel):
    """Per-agent override of the home-wide ``context_window:`` policy.

    Every field defaults to ``None`` = inherit ``~/.olano/config.yaml →
    context_window`` (35% of the window kept after a compaction, 40k tokens
    when the window is unknown, compaction at 85% / 170k, MCP results capped
    at 5k tokens). Resolution lives in
    ``olano/config/system_settings.py`` — :func:`effective_context_window`;
    an agent value goes through the same clamps as the home block.
    """

    keep_fraction: float | None = Field(
        default=None,
        description=(
            "Share of the model's context window kept verbatim after a compaction "
            "(0.05–0.8). Leave unset to inherit the home default (0.35)."
        ),
    )
    keep_tokens: int | None = Field(
        default=None,
        description=(
            "Tokens kept verbatim after a compaction when the model's window is "
            "unknown (2000–2000000). Leave unset to inherit the home default (40000)."
        ),
    )
    trigger_fraction: float | None = Field(
        default=None,
        description=(
            "Share of the window at which automatic compaction fires (0.3–0.98). "
            "Leave unset to inherit the home default (0.85)."
        ),
    )
    trigger_tokens: int | None = Field(
        default=None,
        description=(
            "Token count at which automatic compaction fires when the window is "
            "unknown (8000–4000000). Leave unset to inherit the home default (170000)."
        ),
    )
    mcp_result_token_limit: int | None = Field(
        default=None,
        description=(
            "MCP tool results above this many tokens are offloaded to a file in the "
            "workspace and replaced by a preview; 0 disables. Leave unset to inherit "
            "the home default (5000)."
        ),
    )


class SandboxResourceOverride(BaseModel):
    """Per-agent ceilings for this agent's Docker sandbox container.

    Only meaningful where the agent's shell actually runs in a sandbox (a
    managed deployment, or an explicit ``backend: {type: lionclaw_docker}``).
    Both fields default to ``None`` = inherit, and each is resolved
    independently: an agent that sets only ``memory`` still gets the
    deployment's CPU answer.

    Resolution order per field (first hit wins): this block → the
    ``LIONCLAW_SANDBOX_MEMORY`` / ``LIONCLAW_SANDBOX_CPUS`` environment
    variables → ``~/.olano/config.yaml → sandbox:`` → a fraction of the
    detected host (25% RAM, 50% cores, never below 512m / 0.5).

    Changing either value recreates this agent's sandbox container on its next
    start: docker cannot retune a running container's caps, and reattaching to
    the old one would silently keep the old limits.
    """

    memory: str | None = Field(
        default=None,
        description=(
            "Hard RAM ceiling for this agent's sandbox container, as a docker size "
            "string ('2g', '2048m', or a plain byte count); minimum '6m'. A command "
            "that exceeds it is OOM-killed inside the sandbox — the agent and the "
            "runtime keep running. Leave unset to inherit the deployment default "
            "(25% of detected host RAM, never below 512m)."
        ),
    )
    cpus: float | None = Field(
        default=None,
        description=(
            "CPU ceiling for this agent's sandbox container, in cores (0.5 = half a "
            "core, 2 = two cores; range 0.1–64). This is a ceiling, not a "
            "reservation: an idle sandbox consumes nothing, and concurrent busy "
            "agents can together exceed any single agent's share. Leave unset to "
            "inherit the deployment default (50% of detected host cores, never "
            "below 0.5)."
        ),
    )

    @field_validator("memory", mode="before")
    @classmethod
    def _validate_memory(cls, v: Any) -> str | None:
        from olano.config.backend_config import normalize_sandbox_memory

        return None if v is None else normalize_sandbox_memory(v)

    @field_validator("cpus", mode="before")
    @classmethod
    def _validate_cpus(cls, v: Any) -> float | None:
        from olano.config.backend_config import normalize_sandbox_cpus

        return None if v is None else normalize_sandbox_cpus(v)


class MCPServerConfig(BaseModel):
    """Configuration for an MCP server."""

    url: str
    transport: str = "sse"  # "sse" | "http" (streamable HTTP) | "stdio"
    enabled: bool = True
    # For stdio transport
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    disabled_tools: list[str] = Field(default_factory=list)
    # For remote transports (http / sse): static request headers (e.g. a
    # pre-shared bearer token). Sent on every request to the server.
    headers: dict[str, str] = Field(default_factory=dict)
    # Authentication mode for remote transports: "none" (default) or "oauth".
    # When "oauth", the runtime attaches an OAuth2 (authorization-code + PKCE)
    # provider whose tokens are obtained interactively from the dashboard setup
    # panel and persisted under <LIONCLAW_HOME>/mcp_oauth/.
    auth: str = "none"  # "none" | "oauth"
    # Optional OAuth scope string requested during authorization.
    oauth_scope: str | None = None
    # Optional pre-registered OAuth client credentials. Supplied when the server
    # does NOT support Dynamic Client Registration (DCR) — e.g. HubSpot, which
    # requires creating an "MCP auth app" to obtain a client_id/client_secret.
    # When oauth_client_id is set, the runtime skips DCR and uses these instead.
    # The secret never stays in the file: every save moves a literal into the
    # vault as a per-agent override and leaves "${vault:MCP_OAUTH_CLIENT_SECRET_…}"
    # here (config/mcp_secrets.py); readers resolve it per agent.
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None


class ExternalAccessConfig(BaseModel):
    """Whether — and how — systems **outside** this deployment may reach the agent.

    Two doors, both off by default, both switched on home-wide as well
    (``config.yaml → external_access``) before a per-agent flag does anything:

    * ``mcp`` — the agent becomes an ``ask_<agent>`` tool on the deployment's
      MCP endpoint for platforms that speak the Model Context Protocol.
    * ``a2a`` — the agent gets an Agent Card and JSON-RPC endpoint for clients
      that speak the Agent2Agent protocol.

    This is the opposite direction from the agent's own ``mcp_servers`` (tools
    the agent *uses*) and ``remote_agents`` (agents the agent *calls*): here
    someone else is allowed to send this agent messages. Callers authenticate
    with a bearer token minted by an operator, or an OAuth sign-in an admin
    approves on the dashboard; each call runs as an outside, non-owner turn
    under the agent's approval rules.
    """

    mcp: bool = Field(
        default=False,
        description=(
            "Publish this agent as a tool on the deployment's MCP endpoint "
            "(`<public URL>/api/mcp`) so external platforms that speak the Model Context "
            "Protocol — Claude, ChatGPT, Cursor, other agent frameworks — can send it "
            "messages. Off by default. Also needs the home-wide switch (Config → External "
            "access → MCP) and a bearer token or an approved OAuth sign-in on the caller's "
            "side; there is no unauthenticated access. Not the same thing as the agent's "
            "MCP tab, which connects *this* agent to other servers. Takes effect on the "
            "agent's next restart."
        ),
    )
    a2a: bool = Field(
        default=False,
        description=(
            "Publish this agent over the Agent2Agent (A2A) protocol at "
            "`<public URL>/api/a2a/agents/<name>` — an Agent Card at its `.well-known` path "
            "plus a JSON-RPC endpoint — so agents and platforms that speak A2A can send it "
            "tasks. Off by default. Also needs the home-wide switch (Config → External "
            "access → A2A) and a bearer token or approved OAuth sign-in on the caller's "
            "side. Not the same thing as `remote_agents`, which lets *this* agent call "
            "others. Takes effect on the agent's next restart."
        ),
    )
    description: str = Field(
        default="",
        description=(
            "What this agent can do for an outside caller, in one to three plain "
            "sentences — e.g. `Answers questions about Acme order status and shipping "
            "times when given an order number; cannot change or cancel orders.` "
            "Required whenever MCP or A2A is on: it becomes the MCP tool description and "
            "the A2A Agent Card description, which is all a remote caller (human or "
            "model) sees when deciding whether to use this agent. Write it for a "
            "stranger: capabilities, inputs it needs, limits. It is NOT the system prompt "
            "and is never shown to the agent itself; keep internal instructions out of it."
        ),
    )

    @model_validator(mode="after")
    def _description_required_when_exposed(self) -> ExternalAccessConfig:
        if (self.mcp or self.a2a) and not self.description.strip():
            raise ValueError(
                "external_access.description is required when the agent is exposed over MCP "
                "or A2A: describe, for an outside caller, what the agent can do and what it "
                "needs (this is not the system prompt)."
            )
        if len(self.description) > 2000:
            raise ValueError("external_access.description must be 2000 characters or fewer")
        return self

    @property
    def exposed(self) -> bool:
        return bool(self.mcp or self.a2a)


BrowserAutoApproveAction = Literal["send", "submit", "signup"]

# Hostname, optionally with a leading "*." (sub-domains only). No scheme, port,
# path or other wildcard: a rule names a site, never a pattern.
_SITE_RE = re.compile(
    r"^(?:\*\.)?(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


class BrowserAutoApproveRule(BaseModel):
    """One narrow exception to "the browser always asks": a site and the kinds
    of action on it the agent may do without an approval card.

    Purchases, payments, transfers, donations, deletions, approvals and merges
    are never covered, whatever a rule says (``olano.browser.auto_approve``).
    """

    site: str = Field(
        description=(
            "The website the rule covers, as a bare host name: `linkedin.com` covers "
            "linkedin.com and every sub-domain (www.linkedin.com); `*.example.com` covers "
            "sub-domains only. No `https://`, no path, no port. The action must happen on "
            "this site, and a form must also send its data to it, or the agent asks as usual."
        ),
    )
    actions: list[BrowserAutoApproveAction] = Field(
        description=(
            "Which kinds of action on that site go ahead without asking, one or more of: "
            "`send` (a button or link that sends, replies, posts, publishes or invites, such "
            "as a message's Send button), `submit` (any other form that saves or changes "
            "something on the site, and buttons labelled Submit or Confirm), `signup` "
            "(subscribe, sign up, create account, book, reserve). Anything else keeps "
            "asking every time."
        ),
    )
    in_background: bool = Field(
        default=False,
        description=(
            "Also apply the rule to scheduled jobs and background (Cortex) runs, where no "
            "person is in the conversation. Off by default: the rule then applies only to "
            "turns started in a conversation by a person who owns the agent or is an admin, "
            "so the actions still happen while someone is there to see them. Never applies "
            "to requests from other agents, outside MCP/A2A callers, webhooks or watched "
            "email."
        ),
    )

    @field_validator("site", mode="before")
    @classmethod
    def _normal_site(cls, value: Any) -> str:
        site = str(value or "").strip().lower().rstrip(".")
        if not _SITE_RE.match(site):
            raise ValueError(
                f"browser.auto_approve: {value!r} is not a site name; use a bare host name "
                "such as linkedin.com (no https://, path or port)"
            )
        return site

    @field_validator("actions")
    @classmethod
    def _some_actions(cls, value: list[str]) -> list[str]:
        unique = list(dict.fromkeys(value))
        if not unique:
            raise ValueError("browser.auto_approve: a rule needs at least one action")
        return unique


class AgentLangfuseConfig(BaseModel):
    """Per-agent override of ``config.yaml → observability.langfuse``.

    Three-state like the cognition overrides: ``None`` inherits the home-wide
    value, anything else is this agent's own opinion and wins. Resolved only by
    ``olano.observability.policy.effective_trace_policy``. Changing it needs an
    admin (``olano_admin._observability_denial``): turning on ``full`` capture
    sends this agent's conversations to an outside service.
    """

    enabled: bool | None = Field(
        default=None,
        description=(
            "Trace this agent's turns to Langfuse. Empty inherits Config → Observability; "
            "true or false overrides it for this agent only. Tracing also needs Langfuse "
            "keys saved in Config → Observability (or this agent's own keys)."
        ),
    )
    capture: str | None = Field(
        default=None,
        description=(
            'What each trace contains. "metadata" sends the turn\'s structure, timings, '
            "token counts, credits and tool names, but no message, tool or error text. "
            '"full" also sends messages, tool arguments and results, with secrets masked. '
            "Empty inherits the deployment's setting (metadata unless an admin chose full)."
        ),
    )
    sample_rate: float | None = Field(
        default=None,
        description=(
            "Share of this agent's turns to trace, from 0.0 (none) to 1.0 (every turn). "
            "A turn is traced whole or not at all. Empty inherits the deployment's rate."
        ),
    )

    @field_validator("capture", mode="before")
    @classmethod
    def _coerce_capture(cls, v: object) -> str | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        from olano.config.system_settings import coerce_langfuse_capture

        return coerce_langfuse_capture(v)

    @field_validator("sample_rate", mode="before")
    @classmethod
    def _coerce_sample_rate(cls, v: object) -> float | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        try:
            value = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if value != value:  # NaN
            return None
        return max(0.0, min(1.0, value))


class AgentObservabilityConfig(BaseModel):
    """Per-agent observability settings (the home-wide ones live in
    ``config.yaml → observability``)."""

    langfuse: AgentLangfuseConfig = Field(default_factory=AgentLangfuseConfig)


class BrowserAgentConfig(BaseModel):
    """Per-agent browser settings (the home-wide ones live in ``config.yaml → browser``)."""

    auto_approve: list[BrowserAutoApproveRule] = Field(
        default_factory=list,
        description=(
            "Narrow exceptions to the browser's approval gate. Normally every send, post, "
            "form submit or purchase the agent tries in its browser waits for a person to "
            "approve it, in every chat mode. Each rule here lets one kind of action on one "
            "site go ahead without asking (for example: send messages on linkedin.com), and "
            "the chat still shows each one with the rule that allowed it. Purchases, "
            "payments, transfers, donations and deletions always ask. Empty (the default) "
            "means everything asks. Only an admin or an owner of this agent can change it."
        ),
    )

    human_check: str | None = Field(
        default=None,
        description=(
            "What this agent does when a site shows a check only a person can pass (such "
            "as Cloudflare's 'Verify you are human' box). 'wait': ask for a person in the "
            "chat and on their phone, and wait `human_check_wait` minutes. 'hold': ask and "
            "wait with no time limit. 'skip': do not ask; move on. Empty (the default) uses "
            "the home-wide setting (Config → Browser)."
        ),
    )
    human_check_wait: int | None = Field(
        default=None,
        description=(
            "Minutes 'wait' waits for a person, 1 to 120. Empty uses the home-wide setting."
        ),
    )

    @field_validator("human_check", mode="before")
    @classmethod
    def _human_check_mode(cls, value: object) -> str | None:
        from olano.config.system_settings import HUMAN_CHECK_MODES

        text = str(value or "").strip().lower()
        if not text:
            return None
        if text not in HUMAN_CHECK_MODES:
            raise ValueError(f"browser.human_check must be one of {', '.join(HUMAN_CHECK_MODES)}")
        return text

    @field_validator("human_check_wait", mode="before")
    @classmethod
    def _human_check_wait(cls, value: object) -> int | None:
        from olano.config.system_settings import HUMAN_CHECK_WAIT_MAX

        if value is None or value == "":
            return None
        try:
            minutes = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("browser.human_check_wait must be a number of minutes") from exc
        if not 1 <= minutes <= HUMAN_CHECK_WAIT_MAX:
            raise ValueError(f"browser.human_check_wait must be 1 to {HUMAN_CHECK_WAIT_MAX}")
        return minutes

    @field_validator("auto_approve")
    @classmethod
    def _bounded(cls, value: list[BrowserAutoApproveRule]) -> list[BrowserAutoApproveRule]:
        if len(value) > 20:
            raise ValueError("browser.auto_approve: at most 20 rules")
        seen: set[str] = set()
        for rule in value:
            if rule.site in seen:
                raise ValueError(
                    f"browser.auto_approve: {rule.site} appears twice; put its actions in one rule"
                )
            seen.add(rule.site)
        return value


class RemoteAgentConfig(BaseModel):
    """One external agent this agent may talk to over the A2A protocol.

    This is the outbound half of interoperability: a remote agent hosted by
    another vendor or on another Olano box, spoken to with Google's open
    Agent2Agent (A2A) protocol. Only agents listed here are reachable — the
    ``a2a_send_message`` tool refuses any name it does not find in this list,
    so the operator, not the model, decides which external systems an agent
    can hand work to.
    """

    name: str = Field(
        description=(
            "Short handle the agent uses to address this remote agent, e.g. "
            "`acme-support`. Letters, digits, `-` and `_` only; matching ignores "
            "capitalisation. This is the value the agent passes as `agent` to "
            "`a2a_send_message`, so pick something the model can say."
        ),
    )
    url: str = Field(
        description=(
            "Base URL of the remote agent, e.g. `https://agents.acme.com/support`. "
            "The Agent Card is fetched from `<url>/.well-known/agent-card.json` "
            "and the service endpoint is taken from that card. Must be `https://` "
            "— plain `http://` is refused unless Allow Insecure Http is on and the "
            "host is loopback (local development only). Hosts that resolve to "
            "private, link-local or cloud-metadata addresses are always refused."
        ),
    )
    enabled: bool = Field(
        default=True,
        description=(
            "Off keeps the entry in the config but removes it from the agent's "
            "reach: it is not listed by `a2a_list_agents` and `a2a_send_message` "
            "refuses it. Default on."
        ),
    )
    description: str = Field(
        default="",
        description=(
            "What this remote agent is for, in one or two sentences, e.g. "
            "`Acme's order-status agent: ask it for shipment status by order id`. "
            "Shown to the agent when it lists remote agents so it knows when to "
            "use one. Empty falls back to the description published in the "
            "remote Agent Card."
        ),
    )
    auth: Literal["none", "bearer", "api_key"] = Field(
        default="none",
        description=(
            "How to authenticate to the remote agent. `none` (default) sends no "
            "credential — only for public agents. `bearer` sends `Authorization: "
            "Bearer <credential>` (OAuth2 / OIDC access tokens and plain bearer "
            "tokens all travel this way). `api_key` sends the credential in the "
            "header named by Api Key Header. The credential itself goes in "
            "Credential, preferably as a vault reference."
        ),
    )
    credential: str = Field(
        default="",
        description=(
            "The secret sent per Auth. Use a vault reference — `${vault:ACME_A2A_TOKEN}` "
            "— so the token lives encrypted in the vault, never in this file; a "
            "literal value works but is stored in plain text and shows up in config "
            "history. Resolved per agent (this agent's vault override first, then "
            "the shared entry). Required when Auth is `bearer` or `api_key`; "
            "ignored when Auth is `none`."
        ),
    )
    api_key_header: str = Field(
        default="X-API-Key",
        description=(
            "Header name the API key is sent in when Auth is `api_key`, e.g. "
            "`X-API-Key` (default) or `Ocp-Apim-Subscription-Key`. Ignored for "
            "the other Auth modes."
        ),
    )
    timeout_seconds: int = Field(
        default=120,
        ge=5,
        le=900,
        description=(
            "How long one `a2a_send_message` call waits for the remote agent to "
            "answer, in seconds (default 120, minimum 5, maximum 900). A remote "
            "task still running at the deadline is returned as in-progress with "
            "its task id, so the agent can poll it with `a2a_get_task` rather "
            "than failing."
        ),
    )
    allow_insecure_http: bool = Field(
        default=False,
        description=(
            "Permit a plain `http://` Url when — and only when — its host is "
            "loopback (`localhost`, `127.0.0.1`, `::1`). For running a remote "
            "agent on the same machine during development. Default off; never "
            "turn it on for anything reachable from a network, because the "
            "credential would travel unencrypted."
        ),
    )
    same_origin_endpoint: bool = Field(
        default=True,
        description=(
            "Require the service endpoint advertised in the remote Agent Card to "
            "share scheme, host and port with Url (default on). This stops a "
            "compromised or misconfigured card from redirecting the agent — and "
            "its credential — to a different host. Turn off only for a provider "
            "that legitimately serves its card and its endpoint from different "
            "hosts, and only after checking where the card points."
        ),
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", v):
            raise ValueError(
                "remote agent name must be 1-64 characters of letters, digits, '-' or '_'"
            )
        return v

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        v = (v or "").strip()
        if not v.lower().startswith(("https://", "http://")):
            raise ValueError("remote agent url must start with https:// (or http:// for loopback)")
        return v.rstrip("/")


class SubAgentConfig(BaseModel):
    """Configuration for a subagent embedded inside an agent."""

    name: str
    description: str
    system_prompt: str
    model: str | None = None  # inherits parent model if omitted
    _reject_utility_subagent_model = field_validator("model")(_reject_utility_model)
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    # Nested subagents (recursive)
    subagents: list[SubAgentConfig] = Field(default_factory=list)
    # Whether this subagent is enabled (toggled via /subagent or the dashboard)
    enabled: bool = True
    # Optional own backend — None means inherit parent backend (Tier 1).
    # When set, the subagent is promoted to a CompiledSubAgent (Tier 2).
    backend: BackendConfig | None = None
    # Reference to a specialist archetype that provides defaults for this
    # subagent. When set in agent.json5, AgentConfig._resolve_subagent_references
    # loads the named archetype and fills in name/description/system_prompt/
    # tools/model as defaults; explicit fields on this block override.
    # The field is preserved here for traceability (so a dashboard can show
    # which archetype a subagent was sourced from).
    from_archetype: str | None = None
    # Owner-authored text APPENDED to the base system prompt at resolve time,
    # for catalog-sourced subagents (`from_archetype` refs and ToolkitAgent
    # entries). Unlike an explicit `system_prompt` override — which detaches
    # the subagent from its archetype's prompt entirely — the extension keeps
    # the base prompt live (pack updates still land) and adds instructions on
    # top. Both may be set: the extension appends to whichever base applies.
    system_prompt_extension: str | None = None


def _ops_manager_subagent() -> SubAgentConfig:
    return SubAgentConfig(
        name="OpsManager",
        description="Operations manager for scheduler, heartbeat, curated PULSE, approvals, idea/brainstorm capture, and outbound reports.",
        system_prompt=(
            "Manage Olano operations cleanly and safely.\n"
            "Own scheduler, heartbeat, curated PULSE, approval queue, ops queue requests, and "
            "idea/brainstorm capture.\n"
            "Prefer direct operational changes over vague advice when the user asks to create, "
            "inspect, modify, install, or remove automation.\n"
            "For scheduled reports, set up the automation first, then use Gmail or notification "
            "delivery only when the user explicitly wants outbound delivery.\n"
            "Treat PULSE as editorial insight, approvals as explicit review work, and ops as "
            "runtime/system notes.\n"
            "Do not replace the runtime services; manage them through their tools.\n"
        ),
        tools=list(OPS_MANAGER_TOOL_NAMES),
        enabled=True,
    )


_OPS_ALIASES: set[str] = {"ops", "opsmanager", "automationmanager"}


def _normalize_builtin_key(name: str) -> str:
    return str(name).strip().lower().replace("_", "").replace("-", "")


def _builtin_subagent_names() -> list[str]:
    """All names that can be referenced in ``builtin_subagents``.

    A single hardcoded ``ops`` entry (resolves to OpsManager) plus every
    ToolkitAgent in the registry. We expose only one ops alias here so that
    ``BuiltinSubagentConfig(disabled=["ops"])`` cleanly removes it.
    """
    from olano.subagents.registry import list_toolkit_agents

    names = ["ops"]
    for defn in list_toolkit_agents():
        names.append(defn.name)
    return names


def _resolve_builtin_subagents(entries: list[Any]) -> list[SubAgentConfig]:
    """Resolve a list of ``builtin_subagents`` entries into SubAgentConfigs.

    Each entry may be:
      - a bare string ("StripeAgent", "stripe", "ops")
      - a dict {"name": ..., "extra_tools": [...], "disabled_tools": [...],
                "model": ..., "system_prompt": ...}
    """
    from olano.subagents._schema import ToolkitAgentOverride
    from olano.subagents.registry import (
        load_toolkit_agent,
        resolve_as_subagent,
    )

    resolved: list[SubAgentConfig] = []
    seen: set[str] = set()

    for entry in entries:
        if isinstance(entry, dict):
            raw_name = entry.get("name", "")
            override: ToolkitAgentOverride | None = ToolkitAgentOverride(
                name=raw_name,
                extra_tools=list(entry.get("extra_tools") or []),
                disabled_tools=list(entry.get("disabled_tools") or []),
                model=entry.get("model"),
                system_prompt=entry.get("system_prompt"),
                system_prompt_extension=entry.get("system_prompt_extension"),
            )
        else:
            raw_name = str(entry)
            override = None

        key = _normalize_builtin_key(raw_name)

        if key in _OPS_ALIASES:
            sub = _ops_manager_subagent()
        else:
            try:
                load_toolkit_agent(raw_name)
            except KeyError:
                continue
            sub = resolve_as_subagent(raw_name, override)

        sub_key = sub.name.lower()
        if sub_key in seen:
            continue
        seen.add(sub_key)
        resolved.append(sub)

    return resolved


class BuiltinSubagentConfig(BaseModel):
    """Structured enable/disable config for built-in subagents (P6.10/P6.11).

    Supports three forms in agent config:

        # Enable all, disable specific ones
        builtin_subagents:
          enabled: all
          disabled: [VoiceAgent, DeBoxAgent]

        # Explicit allowlist
        builtin_subagents:
          enabled: [GoogleAgent, GitHubAgent]
          disabled: "*"

        # Disable all
        builtin_subagents:
          enabled: none
    """

    enabled: str | list[str] = "all"  # "all", "none", or list of names
    disabled: str | list[str] = Field(default_factory=list)  # "*" or list of names

    def resolve_names(self, available: list[str] | None = None) -> list[str]:
        """Return the names that should be loaded given the enable/disable config."""
        avail = available or []

        # Determine candidate set from `enabled`
        if isinstance(self.enabled, str):
            candidates = list(avail) if self.enabled.lower() in ("all", "*") else []
        else:
            candidates = [n for n in self.enabled if n in avail or not avail]

        # Apply `disabled` exclusions
        if isinstance(self.disabled, str) and self.disabled in ("*", "all"):
            return []
        disabled_set = {
            n.lower() for n in (self.disabled if isinstance(self.disabled, list) else [])
        }
        return [n for n in candidates if n.lower() not in disabled_set]

    def is_explicitly_enabled(self, name: str) -> bool:
        """Return True if a specific subagent name passes the enable/disable rules."""
        if isinstance(self.enabled, str):
            if self.enabled.lower() in ("none",):
                return False
            # "all" — check disabled
        else:
            if name.lower() not in {n.lower() for n in self.enabled}:
                return False

        if isinstance(self.disabled, str) and self.disabled in ("*", "all"):
            return False
        return not (
            isinstance(self.disabled, list) and name.lower() in {n.lower() for n in self.disabled}
        )


class ChannelConfig(BaseModel):
    """
    Configuration for a channel entry in agent.json5.

    Can be a plain string (channel type name) or a dict with full config:

        channels:
          - telegram                              # simple string
          - type: telegram                        # full dict
            token: "${TELEGRAM_BOT_TOKEN}"
            allowed_users: [123456789]            # numeric Telegram user IDs
    """

    type: str
    # All remaining keys are passed as-is to the channel adapter
    model_config = {"extra": "allow"}

    def channel_name(self) -> str:
        return self.type

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


# ─────────────────────────────────────── Webhooks ────────────────────────────

# Canonical outbound-webhook event names. ``*`` subscribes to every event.
WEBHOOK_EVENTS: tuple[str, ...] = (
    "message.received",
    "message.sent",
    "agent.started",
    "agent.stopped",
    "agent.error",
    "task.scheduled",
    "task.completed",
    "tool.called",
    "approval.requested",
    "budget.exceeded",
)


class WebhookConfig(BaseModel):
    """A single per-agent webhook — outbound (default) or inbound.

    **Outbound** (``direction: "outbound"``): when one of the subscribed
    ``events`` fires, the runtime POSTs a JSON payload to ``url``, optionally
    signed with an HMAC-SHA256 ``secret``. Delivery is rate-limited, retried,
    and logged.

        webhooks:
          - name: "ops-pager"
            url: "https://hooks.example.com/olano"
            events: ["agent.error", "budget.exceeded"]
            secret: "${WEBHOOK_SIGNING_SECRET}"

    **Inbound** (``direction: "inbound"``): exposes an unguessable ingress URL
    that an external service POSTs arbitrary payloads to. The agent processes
    each payload generically — the user-written ``prompt`` (a description plus
    processing guidelines) is sent to the agent with **every field the sender
    posted appended verbatim**, so the user never has to know the sender's
    schema in advance.

        webhooks:
          - name: "stripe-events"
            direction: inbound
            prompt: |
              This webhook receives Stripe billing events. Summarise what
              happened and, if a payment failed, draft a dunning email.
            respond: false
    """

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    name: str
    direction: Literal["outbound", "inbound"] = "outbound"
    # Outbound destination (required for outbound; unused for inbound).
    url: str = ""
    events: list[str] = Field(default_factory=lambda: ["*"])
    enabled: bool = True
    # HMAC-SHA256 signing secret. Env-interpolated (``${VAR}``) by the loader.
    secret: str | None = None
    # Extra headers merged into every outbound request (e.g. auth tokens).
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 10.0
    max_retries: int = 2
    # Per-webhook cap (delivery for outbound, ingress for inbound). 0 disables.
    rate_limit_per_minute: int = 60
    verify_tls: bool = True
    # Block delivery to private/internal addresses (SSRF guard). Keep on unless
    # the receiver is a trusted internal service.
    block_private_addresses: bool = True

    # ── Inbound-only ────────────────────────────────────────────────────────
    # Description + processing guidelines prepended to every received payload.
    prompt: str = ""
    # Unguessable path segment that authenticates the ingress URL.
    ingress_token: str = Field(default_factory=lambda: uuid4().hex)
    # Run the agent synchronously and return its reply in the HTTP response.
    respond: bool = False
    # Accumulate all deliveries in one thread (keeps context) vs. a fresh
    # thread per payload (default — avoids unbounded context growth).
    accumulate_thread: bool = False
    # Max accepted body size in bytes (defense against oversized payloads).
    max_body_bytes: int = 256_000

    model_config = {"extra": "ignore"}

    @model_validator(mode="after")
    def _validate(self) -> WebhookConfig:
        if not self.name or not str(self.name).strip():
            raise ValueError("webhook 'name' is required")
        if self.direction == "outbound":
            if not self.url or not str(self.url).strip():
                raise ValueError("outbound webhook 'url' is required")
            scheme = str(self.url).split("://", 1)[0].lower()
            if scheme not in ("http", "https"):
                raise ValueError(f"webhook url must be http(s), got: {self.url!r}")
            unknown = [e for e in self.events if e != "*" and e not in WEBHOOK_EVENTS]
            if unknown:
                raise ValueError(
                    f"unknown webhook event(s): {unknown}. Valid: {['*', *WEBHOOK_EVENTS]}"
                )
        return self

    def subscribes_to(self, event_type: str) -> bool:
        """True when this (outbound) webhook should fire for ``event_type``."""
        if not self.enabled or self.direction != "outbound":
            return False
        return "*" in self.events or event_type in self.events

    def redacted(self) -> dict[str, Any]:
        """Config view safe for API responses — never leaks the secret.

        The ``ingress_token`` IS exposed (the dashboard is admin-gated and the
        user needs the full ingress URL to hand to the sender), but it is never
        logged.
        """
        data = self.model_dump()
        if data.get("secret"):
            data["secret"] = "••••••••"
            data["has_secret"] = True
        else:
            data["has_secret"] = False
        return data


# ─────────────────────────────────────── Org / Collaboration ─────────────────


def _norm_agent_name(name: str | None) -> str:
    """Casefold an agent name for policy comparisons.

    Mirrors the case-insensitive matching the ``talk_to_agent`` resolver uses
    so a config that lists ``"Marketing"`` still matches the agent registered
    as ``marketing``.
    """
    return (name or "").strip().casefold()


class OrgConfig(BaseModel):
    """Org-chart placement and peer-messaging (``talk_to_agent``) governance.

    Two related concerns live here:

    1. **Peer-messaging allow-lists** govern which booted peer agents may
       reach this one (``input_agents``) and which this one may reach
       (``output_agents``) via ``talk_to_agent``. Names match
       case-insensitively against booted peer names. An **empty list means
       *no restriction*** — the default preserves the historical "any agent
       may talk to any agent" behaviour. A non-empty list switches that side
       into allow-list mode.

    2. **Hierarchy** (``manager`` / ``reports``) places the agent in the org
       chart. Hierarchy neighbours (an agent's manager and its direct
       reports) are *always* permitted to exchange messages regardless of the
       allow-lists, so declaring a reporting line "just works" for delegation
       without duplicating names into ``input_agents`` / ``output_agents``.

    Example::

        org: {
          title: "Marketing Manager",
          reports: ["content-writer", "seo-specialist"],
          output_agents: ["finance-bot"],   // may also message finance-bot
          mutating_from_manager_only: true, // peers other than my manager get read-only service
        }
    """

    # Peer-messaging governance (talk_to_agent). Empty list + no explicit mode
    # ⇒ unrestricted ("everyone").
    input_agents: list[str] = Field(default_factory=list)  # peers allowed to message ME
    output_agents: list[str] = Field(default_factory=list)  # peers I'm allowed to message

    # Explicit messaging modes. ``None`` means "infer from the list" (empty list
    # ⇒ everyone, non-empty ⇒ list) so legacy configs keep their behaviour. The
    # dashboard writes these explicitly. ``"none"`` blocks all peers *except*
    # the agent's own hierarchy line (manager / reports), so delegation never
    # breaks.
    input_mode: Literal["everyone", "none", "list"] | None = None
    output_mode: Literal["everyone", "none", "list"] | None = None

    # When True, peer (talk_to_agent) requests may only trigger *mutating*
    # tools when the requesting agent is this agent's ``manager``; any other
    # admitted peer gets read-only service. Off by default: every peer the
    # input policy admits may trigger all of this agent's tools.
    mutating_from_manager_only: bool = False

    # Org hierarchy among top-level (peer) agents.
    manager: str | None = None  # the agent I report to
    reports: list[str] = Field(default_factory=list)  # my direct reports ("employees")
    # Co-workers (mutual peers at a similar level) — display-only on the chart.
    coworkers: list[str] = Field(default_factory=list)

    # When True, derive both allow-lists from the org structure (manager +
    # reports + coworkers) instead of the manual input/output lists.
    autogenerate: bool = False

    # Optional role label shown on the org chart (e.g. "Marketing Manager").
    title: str | None = None

    def hierarchy_neighbors(self) -> set[str]:
        """Casefolded names of this agent's manager + direct reports."""
        names: set[str] = set()
        if self.manager:
            names.add(self.manager)
        names.update(r for r in self.reports if r)
        return {_norm_agent_name(n) for n in names if n}

    def is_manager(self, peer: str | None) -> bool:
        """True if ``peer`` is this agent's manager (case-insensitive)."""
        return bool(self.manager) and _norm_agent_name(peer) == _norm_agent_name(self.manager)

    def effective_mode(self, direction: str) -> str:
        """Resolve the messaging mode for ``direction`` ("input" / "output").

        An explicit mode wins; otherwise infer from the allow-list (empty ⇒
        ``"everyone"``, non-empty ⇒ ``"list"``) to preserve legacy semantics.
        """
        mode = self.input_mode if direction == "input" else self.output_mode
        if mode:
            return mode
        lst = self.input_agents if direction == "input" else self.output_agents
        return "list" if lst else "everyone"

    def _derived_peers(self) -> set[str]:
        """Casefolded manager + reports + coworkers — the ``autogenerate`` set."""
        names: set[str] = set()
        if self.manager:
            names.add(self.manager)
        names.update(self.reports)
        names.update(self.coworkers)
        return {_norm_agent_name(n) for n in names if n}

    def reachable_peer_names(self) -> set[str]:
        """Concrete named peers to auto-inject into this agent's prompt awareness.

        These are the peers the agent has an *explicit relationship* with — its
        manager, direct reports, coworkers, and any names on its ``talk_to``
        (``output_agents``) allow-list. They are always surfaced in the roster
        regardless of the agent's :class:`AgentVisibilityConfig` mode, so
        declaring a reporting line or a talk-to target automatically makes the
        agent aware that peer exists (name + description) without also flipping
        visibility to ``all``.

        The unbounded ``everyone`` output mode contributes **no** concrete names
        here on purpose: "may message anyone" is a permission, not an awareness
        list, so it must not dump every booted agent into the prompt (that is
        exactly the leak the visibility config exists to prevent).
        """
        if self.autogenerate:
            return set(self._derived_peers())
        names = set(self.hierarchy_neighbors())  # manager + direct reports
        names |= {_norm_agent_name(c) for c in self.coworkers if c}
        if self.effective_mode("output") == "list":
            names |= {_norm_agent_name(a) for a in self.output_agents if a}
        return names

    def allows_output(self, peer: str) -> bool:
        """True if this agent may *message* ``peer`` per its output policy.

        ``everyone`` ⇒ always; ``none`` ⇒ only the hierarchy line; ``list`` ⇒
        the allow-list plus hierarchy. ``autogenerate`` ignores the manual list
        and uses manager + reports + coworkers. Empty list + no mode ⇒ everyone.
        """
        target = _norm_agent_name(peer)
        if self.autogenerate:
            return target in self._derived_peers()
        mode = self.effective_mode("output")
        if mode == "everyone":
            return True
        if mode == "none":
            return target in self.hierarchy_neighbors()
        allowed = {_norm_agent_name(n) for n in self.output_agents}
        return target in (allowed | self.hierarchy_neighbors())

    def allows_input(self, peer: str) -> bool:
        """True if this agent *accepts* a message from ``peer`` per its input policy.

        Mirror of :meth:`allows_output` for the inbound direction.
        """
        source = _norm_agent_name(peer)
        if self.autogenerate:
            return source in self._derived_peers()
        mode = self.effective_mode("input")
        if mode == "everyone":
            return True
        if mode == "none":
            return source in self.hierarchy_neighbors()
        allowed = {_norm_agent_name(n) for n in self.input_agents}
        return source in (allowed | self.hierarchy_neighbors())


class AgentVisibilityConfig(BaseModel):
    """Which *other* agents this agent is told exist (prompt awareness).

    **Awareness is not permission.** Listing a peer here makes the current agent
    aware that it exists (its name + description appear in the system prompt) so
    it can reason about who's on the team — it does **not** grant the ability to
    message that peer. Direct messaging is governed separately by
    :class:`OrgConfig` (the ``talk_to`` / ``output_agents`` allow-list and the
    manager / direct-reports hierarchy).

    Modes:
      - ``"none"`` (default, safest): the agent is told about **no** other
        agents. Only the peers it already has an explicit relationship with —
        its ``talk_to`` allow-list plus manager / direct reports / coworkers
        (see :meth:`OrgConfig.reachable_peer_names`) — are auto-injected, so
        delegation keeps working while unrelated agents (including internal
        ``hidden`` test agents) stay invisible.
      - ``"all"``: every non-``hidden`` peer agent is listed.
      - ``"list"``: only the agents named in :attr:`agents` are listed (plus the
        always-injected reachable set).
    """

    mode: Literal["none", "all", "list"] = "none"
    # Explicit awareness list (used when mode == "list"). Names are matched
    # case-insensitively against booted peers.
    agents: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dedupe_agents(self) -> AgentVisibilityConfig:
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw in self.agents:
            name = (raw or "").strip()
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(name)
        self.agents = cleaned
        return self

    def is_visible(self, peer: str, reachable: set[str]) -> bool:
        """Whether ``peer`` should appear in the agent's prompt roster.

        ``reachable`` is the casefolded set from
        :meth:`OrgConfig.reachable_peer_names` — those peers are always visible
        regardless of mode.
        """
        norm = (peer or "").strip().casefold()
        if norm in reachable:
            return True
        if self.mode == "all":
            return True
        if self.mode == "list":
            return norm in {a.strip().casefold() for a in self.agents}
        return False


class AgentConfig(BaseModel):
    """Full configuration for a olano agent (agent.json5)."""

    # Identity
    #
    # `name` is the handle: the on-disk folder, the workspace path, the
    # checkpoint thread scope, the runner-registry key, the `(agent, channel)`
    # delivery key, a column in five DBs, ~40 API routes and the `ai-<name>`
    # issue principal. It is immutable — there is no rename endpoint, and
    # `_IMMUTABLE_AGENT_FIELDS` rejects writes to it.
    #
    # `display_name` is the face: what the agent is called in the dashboard,
    # in its own prompt, in `whoami`, in a peer's roster and on an outbound
    # email. It is freely editable precisely because nothing keys off it.
    # Empty falls back to `name` — read both through `.label`, never by
    # hand, so the fallback has one definition.
    name: str
    display_name: str = ""
    description: str = ""
    emoji: str = "🦁"
    # Optional accent color (CSS hex like "#c96442") used by the dashboard
    # to theme the UI when this agent is the active context. None defaults
    # to the global accent.
    color: str | None = None
    # Optional avatar image: a URL pointing at a square image used as the
    # agent's face in the dashboard (cards, playground head, chat assistant
    # avatar). Typically points at a workspace-relative
    # /api/agents/<name>/avatar endpoint that streams an uploaded or
    # generated PNG/JPG. None falls back to the agent's emoji.
    avatar_url: str | None = None

    # Archetype (pre-configured template this agent derives from)
    archetype: str | None = None  # e.g. "merli", "lionFinance", "lionDev"

    # The agent this config resolves CREDENTIALS as. In-memory only, never
    # persisted (``exclude=True`` keeps it out of every ``model_dump`` the
    # writers go through): a sub-agent / cognition worker runs under a fresh
    # unique ``name`` (``<parent>__cog__…``) for checkpoints and attribution,
    # but every agent-keyed credential layer — vault overrides, connector
    # token records, Google token files, MCP OAuth tokens — must resolve as
    # the parent it acts for, or the child silently degrades to the common /
    # system credential. ``None`` ⇒ ``name`` (a top-level agent).
    credential_scope: str | None = Field(default=None, exclude=True)

    # Model (provider:model format)
    model: str = "anthropic:claude-sonnet-4-6"
    _reject_utility_agent_model = field_validator("model")(_reject_utility_model)

    # System prompt (merged with bootstrap files)
    system_prompt: str = ""

    # How much of the agent's work its replies show, for a chat that has not
    # chosen for itself. "quiet" = the final answer only; "normal" = the answer
    # plus a one-line note when a turn ran tools but produced no text;
    # "verbose" = the full play-by-play (tool calls, truncated results,
    # peer/subagent cards) before the answer. See platforms/verbosity.py.
    #
    # This is the *starting point* for every new chat, not a ceiling: a user
    # who runs `/verbosity <mode>` (or `/quiet`, `/normal`, `/verbose`) pins
    # their own chat, and that choice — stored per chat on the base thread id —
    # wins from then on. Changing this field therefore only moves chats that
    # have never set it. Aliases ("v", "full", "debug", "min", …) are accepted
    # and normalized; an unrecognized value falls back to "normal".
    default_verbosity: Literal["quiet", "normal", "verbose"] = "normal"

    # Tools to enable (names of built-in tools or custom tool names)
    tools: list[str] = Field(default_factory=list)

    # Tools to remove from the injected defaults. Allows an agent to opt out of
    # specific default tools without disabling the whole category. Covers both the
    # universal/ops names merged into `tools` (see _inject_builtin_ops) AND the
    # extras the builder wires in on its own — credentials-vault secret_* tools,
    # auto-generated memory/notes/graph tools, workspace_context (see
    # core/builder.py:_build_extra_tools).
    # Example: disabled_tools: ["scheduler_delete", "secret_get", "trash_put"]
    disabled_tools: list[str] = Field(default_factory=list)

    # Master switch for everything olano grants WITHOUT a `tools:` entry.
    # True (default): today's behavior — universal defaults (whoami, time,
    # talk_to_agent, notification_*, chats_*, web_*, document
    # readers, trash_*, the scheduler_*/heartbeat_*/pulse_*/approvals_*/ops_*
    # operations tools), the Cortex item tools when cognition is on, the
    # shared-markdown memory tools, the knowledge-graph and notes tools, and
    # the workspace_context loader.
    # False: NONE of that is injected — the agent's tool surface is exactly
    # its explicit `tools:` list plus what its own opt-in modes bind:
    # the multi-user identity-injected set (remember/recall/recall_all/
    # forget_me + knowledge_*, execute_action + the customer file tools,
    # subscription_status, the schedule_my_* trio — governed by
    # memory.per_customer / allowed_actions / monetization /
    # customer_service.user_scheduling), RAG tools (memory.rag is itself an
    # explicit opt-in), MCP servers, and skills. Explicit spellings of a
    # default grant survive too: `whoami` listed in `tools:` binds, a
    # non-empty `top_level_memory_tools`/`top_level_graph_tools` pin keeps
    # exactly the pinned names, a `top_level_workspace_context` the config
    # file sets itself keeps the loader, and an explicitly set
    # `builtin_subagents` is still honored. deepagents' own loop/filesystem tools
    # (write_todos, task, read_file/…/execute) are governed by the backend and
    # `disabled_tools` as before — this knob is about olano's grants, not
    # the agent loop. Denylists (`disabled_tools`) still apply on top.
    # This is the "fully controllable" posture for marketplace agents: deny
    # by default, then grant tool-by-tool via `tools:`.
    default_internal_tools: bool = True

    # Optional overrides for which auto-generated memory tools are exposed on the
    # top-level agent. None preserves the legacy behavior for that category.
    top_level_memory_tools: list[str] | None = None
    top_level_rag_tools: list[str] | None = None
    top_level_graph_tools: list[str] | None = None
    top_level_workspace_context: bool = True

    # Subagents
    subagents: list[SubAgentConfig] = Field(default_factory=list)

    # Built-in subagent tiers to enable. Empty by default: the operations
    # tools the built-in OpsManager ("ops") used to carry are universal
    # top-level defaults now (ops_defaults.OPS_TOOL_NAMES), so it is opt-in.
    # Accepts:
    #   - legacy list of names: ["ops", "StripeAgent", "alpaca"]
    #   - list with per-entry overrides:
    #       [{"name": "AlpacaAgent", "disabled_tools": [...]}, "ops"]
    #   - structured {enabled/disabled} dict (P6.10/P6.11)
    builtin_subagents: list[str | dict[str, Any]] | BuiltinSubagentConfig = Field(
        default_factory=list
    )

    # Org-chart placement + peer-messaging (talk_to_agent) governance.
    # Empty allow-lists ⇒ unrestricted (any peer may talk to any peer), so this
    # is backward compatible. See OrgConfig for the full semantics.
    org: OrgConfig = Field(default_factory=OrgConfig)

    # Which OTHER agents this agent is told exist (prompt awareness). Defaults to
    # "none" (safest): the agent knows about no peers except the ones it already
    # has an explicit relationship with (talk_to allow-list + manager / reports /
    # coworkers), which are auto-injected. Awareness ≠ permission — see
    # AgentVisibilityConfig and OrgConfig.
    agent_visibility: AgentVisibilityConfig = Field(default_factory=AgentVisibilityConfig)

    # Human-only access. When True, no other agent may reach this agent via
    # `talk_to_agent` (enforced in tools/talk_to.py) regardless of org-chart
    # allow-lists. Intended for privileged operator agents like AgentFather that
    # manage the deployment itself. Channels (Telegram/WhatsApp/etc.) are
    # human-facing and are NOT affected — lock those down with per-channel
    # `allowed_users`. Such an agent should also list "talk_to_agent" in
    # `disabled_tools` so it cannot initiate peer messages either.
    human_only: bool = False

    # Fleet filesystem access (security/fleet_access.py). Granted only by an
    # administrator: olano_admin's config writes refuse to newly set it without
    # an admin on the turn, and the dashboard's agent editors are admin-only.
    fleet_access: bool = Field(
        default=False,
        description=(
            "Lets this agent's shell and file tools reach the whole deployment, not "
            "just its own workspace: every agent's directory under the Olano home "
            "(`agents/`, read-write: workspaces, configs and connection files), the "
            "common skills (`skills/`, read-write) and the deployment logs (`logs/`, "
            "read-only). Meant for AgentFather, which the deployment's administrator "
            "uses to operate everything. Off (false) by default, and only an "
            "administrator can turn it on. The vault, the conversation databases and "
            "config.yaml stay out of reach either way. Approval cards for writes and "
            "shell commands are unaffected. Turning it on or off rebuilds the agent's "
            "sandbox on its next restart."
        ),
    )

    # Hidden / internal agent. When True this agent is treated as an internal
    # tool (e.g. a test agent used to exercise a specific provider/model API)
    # and is withheld from every customer- and operator-facing listing:
    #   • excluded from the dashboard agent roster (GET /api/agents), the org
    #     chart, and every peer's prompt roster (never advertised to other agents)
    #   • NOT booted by the long-running runtime (`RuntimeServer.start()` skips
    #     it), so it consumes no resources and is never reachable in production.
    # It is meant for pytest and one-shot CLI runs (`olano --agent <name> -n
    # "..."`, which builds a single agent directly, bypassing the runtime boot
    # loop) and the test harness. To boot hidden agents inside a *dev* runtime
    # anyway, set LIONCLAW_BOOT_HIDDEN_AGENTS=1 (then their customer-facing
    # channels/platforms are still NOT started). Naming a hidden agent explicitly
    # (`olano serve --agent <name>`) also boots it. The management CLI
    # (`olano agents list`) still shows it, tagged "hidden".
    hidden: bool = False

    # Skills source directories (absolute or relative to workspace)
    skills: list[str] = Field(default_factory=list)

    # Skills this agent must NOT load, by skill NAME (the `name:` in the
    # SKILL.md frontmatter, which is also the folder name per the Agent Skills
    # spec). The sibling of `disabled_tools`, and it exists for the same reason:
    # skills arrive by the *directory* — the shared managed pack in
    # `<home>/skills` plus the workspace dirs — so there is otherwise no way to
    # keep one agent from loading one skill without deleting it for the whole
    # fleet. A named skill stays on disk and stays listed (marked disabled); it
    # is simply never advertised to the model, never delegable, and never seen
    # by self-learning. Matching is case-insensitive on the name; an entry that
    # matches nothing is harmless (a skill can be installed later).
    # Example: disabled_skills: ["canvas-design", "xlsx"]
    disabled_skills: list[str] = Field(default_factory=list)

    # MCP servers
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)

    # External agents reachable over the A2A protocol (outbound). Binding the
    # a2a_* tools follows this list: an agent with at least one enabled entry
    # gets them implicitly (see core/builder + config/internal_tools).
    remote_agents: list[RemoteAgentConfig] = Field(
        default_factory=list,
        description=(
            "External agents this agent may talk to over the open Agent2Agent "
            "(A2A) protocol — other vendors' agents, or agents on another Olano "
            "deployment. One entry per remote agent (see each field's help). "
            "Listing at least one enabled entry gives the agent the "
            "`a2a_list_agents`, `a2a_send_message` and `a2a_get_task` tools; an "
            "empty list (the default) means the agent cannot reach any external "
            "agent. Every exchange is recorded in the Agent to Agent activity view."
        ),
    )

    # Channels — accepts plain strings or full config dicts
    channels: list[str | ChannelConfig] = Field(default_factory=list)

    # Scheduled jobs
    schedule: list[ScheduleConfig] = Field(default_factory=list)

    # Heartbeat poll (HEARTBEAT.md checks). None fields inherit the home-wide
    # defaults in ~/.olano/config.yaml → heartbeat: (enabled, hourly).
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)

    # Issue-tracker poll (assigned-issue delivery). None fields inherit the
    # home-wide defaults in ~/.olano/config.yaml → issues: (on, 15 min).
    issues: IssuesPollConfig = Field(default_factory=IssuesPollConfig)

    # Gmail watch: wake the agent on new mail matching a Gmail search. Off by
    # default; see GmailWatchConfig.
    gmail_watch: GmailWatchConfig = Field(default_factory=GmailWatchConfig)

    # Context-window policy (compaction keep/trigger, MCP result offload).
    # None fields inherit ~/.olano/config.yaml → context_window:.
    context_window: ContextWindowConfig = Field(default_factory=ContextWindowConfig)

    # Outbound webhooks — POST agent events to external URLs
    webhooks: list[WebhookConfig] = Field(default_factory=list)

    # Memory configuration
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    # Multi-user mode (disabled by default). Turning this on adds the per-user
    # tools, isolates memory per sender, and appends ``prompt_extension`` to the
    # system prompt — filling a blank one with the support-desk default. See
    # config/customer_service.py.
    customer_service: CustomerServiceConfig = Field(default_factory=CustomerServiceConfig)

    # Inbound/outbound message caps — input length, silence rules, reply
    # ceiling. Every field is off by default. See security/reply_policy.py.
    message_policy: MessagePolicyConfig = Field(default_factory=MessagePolicyConfig)

    # Creator monetization: charge this agent's end-users a subscription
    # (disabled by default). See olano/monetization/.
    monetization: MonetizationConfig = Field(default_factory=MonetizationConfig)

    # Security configuration
    security: SecurityConfig = Field(default_factory=SecurityConfig)

    # Outbound-destination lists for this agent's SSRF-gated tools. Top-level
    # (not under `security`) on purpose: the schema-driven Advanced tab renders
    # a top-level list[str] as a proper list control with this description as
    # its hint, where a nested block collapses to a raw JSON editor. Resolved
    # against the deployment's lists (config.yaml → egress_allow/egress_deny)
    # by security/egress.py; the descriptions below are the user-facing
    # contract and are rendered verbatim in the dashboard.
    egress_allow: list[str] = Field(
        default_factory=list,
        description=(
            "Domains this agent's web and HTTP tools may reach. One entry per "
            "domain, like api.github.com or example.com — a domain covers its "
            "subdomains, and a leading *., an https:// prefix, a port or a path "
            "are stripped. Empty (the default) inherits the deployment's list "
            "from Config → Outbound access; when that is empty too, every public "
            "destination is allowed. A non-empty list here REPLACES the "
            "deployment's allow list for this agent — it does not add to it. "
            "egress_deny is checked first and always wins. Governs every "
            "outbound request this agent's tools make: web and HTTP tools, the "
            "headless browser, every integration toolkit (Stripe, Salesforce, "
            "Slack and the rest), AND the agent's sandbox shell — a curl, pip "
            "install or git clone inside a shell command. Internal and "
            "private-network addresses stay blocked regardless "
            "(security.ssrf_protection), and the deployment's own control plane "
            "is always reachable. Applies to the next tool or shell command, "
            "with no restart and no container rebuild."
        ),
    )
    egress_deny: list[str] = Field(
        default_factory=list,
        description=(
            "Domains this agent's web and HTTP tools must never reach, in the "
            "same format as egress_allow. Checked before the allow list, so a "
            "denied domain stays blocked even when it is allowed elsewhere. "
            "ADDED to the deployment's deny list (Config → Outbound access): an "
            "agent can add blocks but never remove one the deployment set. "
            "Empty (the default) adds nothing. Same coverage as egress_allow: "
            "every web, HTTP, browser and integration tool, plus the agent's "
            "sandbox shell."
        ),
    )
    shared_paths: list[SharedPathConfig] = Field(default_factory=list)

    # Chat slash-command exposure + custom commands (governs platforms/channels
    # and the dashboard chat)
    commands: CommandsConfig = Field(default_factory=CommandsConfig)

    # Budget / cost controls
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    # PULSE proactive intelligence
    pulse: PulseConfig = Field(default_factory=PulseConfig)

    # Background reasoning workers (pulse/self-learning/memory/ideas/skills)
    cognition: CognitionConfig = Field(default_factory=CognitionConfig)

    # Chat self-configuration overrides (None fields inherit the home-wide
    # `agent_autonomy:` block, which defaults everything ON). Resolve through
    # system_settings.autonomy_allowed(), never by reading fields directly.
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)

    # Web3 wallet (optional, disabled by default)
    wallet: WalletConfig = Field(default_factory=WalletConfig)

    # Prompt assembly controls
    prompt_mode: Literal["full", "task", "minimal", "none"] = "full"
    vibes_preset: Literal["precise", "warm", "executive-brief", "hacker-lab"] = "precise"
    prompt_cache: PromptCacheConfig = Field(default_factory=PromptCacheConfig)
    iteration_budget: int = 0
    truncation_retry_limit: int = 3
    # Circuit breaker: stop a turn after this many identical failing tool calls.
    # Recoverable errors otherwise flow back to the model so it can retry a
    # different approach. 0 disables the breaker entirely.
    tool_error_retry_limit: int = 3

    # Per-agent workspace path. In production this is auto-derived from the
    # agent's name (LIONCLAW_HOME/agents/<name>/workspace/) by the validator
    # below — `agent.json5` should NOT set this field. Tests may pass an
    # explicit override to point at a temp dir.
    workspace: str | None = None

    # External access: whether outside systems may reach this agent over MCP
    # and/or A2A (inbound). The opposite direction from mcp_servers /
    # remote_agents. Runtime wiring: runtime/server._boot_agent_locked.
    external_access: ExternalAccessConfig = Field(
        default_factory=ExternalAccessConfig,
        description=(
            "Let systems outside this deployment send this agent messages — over MCP "
            "(for Claude, ChatGPT, Cursor, agent frameworks) and/or A2A (for other agents). "
            "Both off by default; each also needs the matching home-wide switch under "
            "Config → External access. `description` is required when either is on: it "
            "is what a remote caller sees about this agent, and it is not the system "
            "prompt. Callers need a bearer token minted on the External access tab or an "
            "OAuth sign-in an admin approves. Not to be confused with the MCP tab "
            "(servers this agent connects to) or `remote_agents` (agents this agent calls)."
        ),
    )

    # Per-agent browser settings: the narrow auto-approval rules for the browser's
    # always-ask gate (olano/browser/auto_approve.py). Edited on the agent's
    # Browser tab, by browser_set_auto_approvals / olano_set_browser_auto_approvals,
    # and only by an admin or an owner of the agent.
    browser: BrowserAgentConfig = Field(
        default_factory=BrowserAgentConfig,
        description=(
            "This agent's browser settings. `auto_approve` lists narrow exceptions to the "
            "approval the browser asks for before it sends, posts or submits anything: a "
            "site and the kinds of action there that go ahead without asking. Empty by "
            "default (everything asks). Purchases, payments and deletions always ask. "
            "Edit it on the agent's Browser tab; only an admin or an owner of the agent can."
        ),
    )

    # Per-agent override of turn tracing to Langfuse. Admin-only to change
    # (olano_admin._observability_denial); resolved by
    # olano.observability.policy.effective_trace_policy.
    observability: AgentObservabilityConfig = Field(
        default_factory=AgentObservabilityConfig,
        description=(
            "Whether this agent's turns are traced to Langfuse, and how much each trace "
            "contains. Every field is empty by default, which inherits Config → "
            "Observability. Only an admin can change it."
        ),
    )

    # ACP server settings. Descriptions are rendered as the help text under the
    # dashboard's schema-driven Advanced controls, so they carry the full
    # explanation (format, default, caveats) — see CLAUDE.md "Dashboard UX".
    acp_enabled: bool = Field(
        default=False,
        description=(
            "Expose this agent over HTTP so other agents and external tools can call it. "
            "When on, the runtime starts a small local HTTP/SSE server on Acp Port at "
            "boot: POST /acp/agents/<name>/chat (JSON in, JSON out), POST "
            "/acp/agents/<name>/run (streams the reply as SSE), POST /acp/delegate "
            "(fire-and-forget, poll GET /acp/tasks/<id>), GET /acp/agents (who is "
            "reachable), GET /health. Default off. The server binds 127.0.0.1 only and "
            "has NO authentication — it is reachable from this machine (or from inside "
            "the runtime container) and from nowhere else; to reach it from your laptop, "
            "forward the port over SSH rather than publishing it. Takes effect on the "
            "agent's next restart. Olano agents calling each other do NOT need this: the "
            "talk_to_agent tool routes in-process."
        ),
    )
    acp_port: int = Field(
        default=8765,
        description=(
            "TCP port for the ACP server, as a plain number (default 8765). Only used "
            "when Acp Enabled is on. Agents that share a port share one server — the "
            "routes are keyed by agent name, so leaving every agent on 8765 lets one "
            "endpoint reach the whole fleet; give an agent its own port only if you want "
            "it isolated. Pick a free port: if something else already holds it, the ACP "
            "server for this agent fails to start and logs the collision (the agent "
            "itself still boots normally)."
        ),
    )

    # Backend — maps directly to a deepagents backend class.
    # Olano's only custom backend is lionclaw_docker.
    # Default: local_shell (filesystem + shell execution on the host or runtime container).
    backend: BackendConfig = Field(
        default_factory=lambda: LocalShellBackendConfig(root_dir="workspace", virtual_mode=False)
    )

    # Resource ceilings for this agent's sandbox container, when its shell runs
    # in one. None (or an unset field inside) inherits the deployment answer.
    sandbox: SandboxResourceOverride | None = Field(
        default=None,
        description=(
            "Resource ceilings for this agent's sandbox container, e.g. "
            "{memory: '4g', cpus: 2}. Applies where the agent's shell runs in a "
            "Docker sandbox. Omit (or omit either field) to inherit the "
            "deployment default — 25% of host RAM and 50% of host cores. "
            "Changing a value recreates the agent's sandbox on its next start."
        ),
    )

    # Named scripts this agent may run, as {action_name: script_path}. Surfaced
    # to the model as the `execute_action` tool, whose schema lists exactly
    # these names — there is no free-form command string, so an agent with
    # actions needs no shell and no sandbox.
    #
    # Paths are absolute or relative to the agent's workspace / skill dirs. The
    # action runs with cwd set to the CURRENT user's private folder and its
    # inputs as JSON on stdin; the platform and sender id are injected from the
    # authenticated request context, never passed by the model. See
    # ``olano/tools/customer_actions.py``.
    #
    # Only bound when ``memory.per_customer`` is on: without a resolved user
    # there is no folder to scope an action to.
    allowed_actions: dict[str, str] = Field(default_factory=dict)

    # Extra model kwargs
    model_kwargs: dict[str, Any] = Field(default_factory=dict)

    # Reasoning effort for models that support it (langchain's standardized
    # `reasoning_effort`; levels come from the model's profile, e.g.
    # none/low/medium/high). None → the platform default ("low") when the
    # model supports effort control; always ignored for models without it,
    # so the value survives provider switches harmlessly.
    reasoning_effort: str | None = Field(default=None)

    # Per-agent environment overrides
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_agent_keys(cls, data: Any) -> Any:
        legacy = {"sandbox_backend"}
        if isinstance(data, dict):
            found = legacy & data.keys()
            if found:
                raise ValueError(
                    f"Removed agent config keys: {sorted(found)}. "
                    "Use the 'backend:' block instead. See REARCHITECTURE_BACKENDS.md §5."
                )
            # 'sandbox' was a removed key and is now a live block of resource
            # ceilings. Only the old scalar spellings are rejected, and by
            # name, so an ancient config gets the migration hint instead of a
            # bare "Input should be a valid dictionary".
            sandbox = data.get("sandbox")
            if sandbox is not None and not isinstance(sandbox, dict):
                raise ValueError(
                    f"'sandbox: {sandbox!r}' is the old on/off spelling, which was removed. "
                    "Choose the backend with a 'backend:' block "
                    "(see REARCHITECTURE_BACKENDS.md §5); 'sandbox:' now takes resource "
                    "ceilings, e.g. sandbox: {memory: '4g', cpus: 2}."
                )
            if isinstance(data.get("backend"), str):
                old_val = data["backend"]
                raise ValueError(
                    f"'backend: \"{old_val}\"' is the old string format. "
                    "Replace with a 'backend:' block, e.g. "
                    "backend: {type: local_shell, root_dir: workspace}. "
                    "See REARCHITECTURE_BACKENDS.md §5."
                )
        return data

    @model_validator(mode="before")
    @classmethod
    def _normalize_default_verbosity(cls, data: Any) -> Any:
        """Accept the same spellings ``/verbosity`` does, not just the canon.

        An owner who writes ``default_verbosity: "full"`` in agent.json5 means
        ``verbose``; rejecting it (or, worse, silently keeping an unusable
        string that every consumer then falls back on) is a papercut for a
        field whose whole job is convenience. Unrecognized values degrade to
        the platform default rather than failing the agent's boot — a typo
        here should not take an agent offline.
        """
        if not isinstance(data, dict) or "default_verbosity" not in data:
            return data
        # Imported lazily: ``olano.platforms`` pulls in the whole adapter
        # stack (httpx, the base adapter, …) and agent_config is on the boot
        # path. verbosity.py itself is pure, but the package __init__ is not.
        from olano.platforms.verbosity import DEFAULT_VERBOSITY, normalize_verbosity

        data["default_verbosity"] = normalize_verbosity(data["default_verbosity"]) or (
            DEFAULT_VERBOSITY
        )
        return data

    @model_validator(mode="before")
    @classmethod
    def _resolve_subagent_references(cls, data: Any) -> Any:
        """Expand `from_archetype: <name>` subagent shorthand into full dicts.

        A team archetype can compose specialists by reference instead of
        copy-pasting their system_prompts:

            subagents:
              - from_archetype: engineering-frontend-developer
              - from_archetype: engineering-code-reviewer
                tools: [safe_shell]      # overrides specialist's default tools
                model: anthropic:claude-haiku-4-5  # overrides specialist's model

        Resolution loads the named specialist via `load_archetype_template`,
        uses its `name`/`label`/`description`/`system_prompt`/`tools`/`model`
        as defaults, and lets any explicit field on the subagent block win.

        Specialists must be leaf nodes — referencing a team (an archetype that
        itself has subagents) raises ValueError to keep the composition model
        flat and easy to reason about.
        """
        if not isinstance(data, dict):
            return data
        subs = data.get("subagents")
        if not isinstance(subs, list):
            return data

        from olano.archetypes.installer import load_archetype_template

        resolved: list[Any] = []
        for sub in subs:
            if not (isinstance(sub, dict) and sub.get("from_archetype")):
                resolved.append(sub)
                continue

            ref = sub["from_archetype"]
            try:
                specialist = load_archetype_template(ref)
            except FileNotFoundError as exc:
                raise ValueError(
                    f"Subagent references unknown archetype 'from_archetype: {ref}'. {exc}"
                ) from exc

            if specialist.get("subagents"):
                raise ValueError(
                    f"Cannot use team archetype '{ref}' as a subagent — it has "
                    f"its own subagents. Specialists referenced via "
                    f"`from_archetype` must be leaf nodes (subagents: []). "
                    f"Compose specialists directly instead."
                )

            defaults: dict[str, Any] = {
                "name": specialist.get("label") or specialist.get("name") or ref,
                "description": specialist.get("description", ""),
                "system_prompt": specialist.get("system_prompt", ""),
                "tools": list(specialist.get("tools", [])),
                "model": specialist.get("model"),
                "from_archetype": ref,
            }
            # Drop None overrides so they don't wipe out non-None defaults
            # (e.g. an explicit `model: null` should mean "inherit", which the
            # default already provides).
            overrides = {k: v for k, v in sub.items() if k != "from_archetype" and v is not None}
            merged = {**defaults, **overrides}
            # Owner-authored additions append to the base prompt (the explicit
            # override when one is set, the specialist's own prompt otherwise).
            # The endswith guard makes this idempotent: a dict that already
            # carries the composed prompt (an already-resolved config fed back
            # through validation) is not appended to twice.
            extension = str(merged.get("system_prompt_extension") or "").strip()
            base_prompt = str(merged.get("system_prompt") or "")
            if extension and not base_prompt.rstrip().endswith(extension):
                merged["system_prompt"] = (
                    f"{base_prompt.rstrip()}\n\n## Additional owner instructions\n{extension}"
                ).strip()
            resolved.append(merged)

        data["subagents"] = resolved
        return data

    @field_validator("egress_allow", "egress_deny", mode="before")
    @classmethod
    def _normalize_egress_lists(cls, v: Any) -> list[str]:
        """Strict: an entry that is not a host fails the config loudly, the
        way a bad ``sandbox.memory`` does — silently dropping it would leave
        the agent reaching (or blocked from) something other than what the
        author wrote. ``https://Example.com/x`` still coerces to ``example.com``."""
        from olano.security.egress import normalize_domains

        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list | tuple):
            raise ValueError("must be a list of domain names")
        return normalize_domains(v, strict=True)

    @field_validator("display_name", mode="before")
    @classmethod
    def _clean_display_name(cls, v: Any) -> Any:
        """Store the trimmed form so the dashboard round-trips what it shows.

        ``label`` strips anyway, but an untrimmed value written back to
        ``agent.json5`` reappears in the edit field with its padding intact.
        """
        return v.strip() if isinstance(v, str) else v

    @property
    def label(self) -> str:
        """What to call this agent — ``display_name`` when set, else ``name``.

        The single definition of the fallback: roughly twenty render sites
        (dashboard cards, chat header, ``whoami``, the peer roster, an
        outbound email ``From:``) need "the pretty name if there is one",
        and each writing its own ``display_name or name`` is how the two
        drift apart. A property and not a field, so ``model_dump`` never
        writes a derived value back into ``agent.json5``.
        """
        return self.display_name.strip() or self.name

    @model_validator(mode="after")
    def _default_display_name(self) -> AgentConfig:
        """Display name is never empty: blank ⇒ the handle, **verbatim**.

        This is also the automatic migration for pre-field ``agent.json5``
        files — every load fills it, the next save persists it, no sweep
        needed. Verbatim and never prettified (``merli``, not ``Merli``):
        whoami's ``Handle:`` line, the prompt's "You are called" line, the
        roster's bold face and the detail header's second line all gate on
        ``label != name``, so a beautified auto-fill would flip all four for
        every existing agent on upgrade.
        """
        if not self.display_name.strip():
            self.display_name = self.name
        return self

    @model_validator(mode="after")
    def _derive_workspace(self) -> AgentConfig:
        if self.workspace is None:
            from olano.config.home import get_olano_paths

            self.workspace = str(get_olano_paths().agent_workspace_dir(self.name))
        return self

    @model_validator(mode="after")
    def _sync_customer_service_mode(self) -> AgentConfig:
        """Keep ``customer_service.enabled`` and ``memory.per_customer`` coherent.

        Per-customer isolation *is* customer-service mode: it is what makes
        ``forget_me`` delete one person's data and what stops two customers on
        one bot from reading each other. Rather than add a second gate the
        builder would have to check, the switch drives the existing flag —
        every downstream consumer (``_build_memory_tools``, the self-learning
        loop, the prompt) keeps working untouched.

        The link is deliberately one-way. Enabling the switch turns isolation
        on, but turning ``per_customer`` on directly (as the
        ``CustomerServiceAgent`` archetype does, and as hand-edited configs
        predating this switch do) is *also* customer-service mode, so the
        switch reads back as enabled. Mirroring it keeps the dashboard toggle
        truthful for those agents instead of showing them as "off" while they
        plainly behave otherwise. Nothing here can silently disable isolation:
        clearing the switch on such an agent is an explicit edit to
        ``memory.per_customer``.

        It also fills a **blank** ``prompt_extension`` with the support-desk
        default, so a shared agent never runs with no privacy or scope rules at
        all. Blank-only: operator text is returned verbatim by
        ``resolve_prompt_extension``, so a save never rewrites somebody's
        wording. Doing it here rather than in the prompt assembler means every
        surface agrees — the dashboard textarea, the raw config editor and the
        system prompt all show the same text.
        """
        from olano.config.customer_service import resolve_prompt_extension

        if self.customer_service.enabled:
            self.memory.per_customer = True
        elif self.memory.per_customer:
            self.customer_service.enabled = True
        if self.customer_service.enabled:
            self.customer_service.prompt_extension = resolve_prompt_extension(
                self.customer_service.prompt_extension
            )

        # Multi-user-only schedule features need the multi-user store to exist:
        # a `for_each_customer` job on a single-user agent has no customers/
        # tree to fan out over, and user scheduling would record identities the
        # agent's tools can't act on. Fail at load, not silently at fire time.
        if not self.memory.per_customer:
            for index, sched in enumerate(self.schedule):
                if getattr(sched, "for_each_customer", False):
                    raise ValueError(
                        f"schedule[{index}]: `for_each_customer` requires multi-user "
                        "mode (customer_service.enabled / memory.per_customer)"
                    )
            if self.customer_service.user_scheduling:
                raise ValueError(
                    "customer_service.user_scheduling requires multi-user mode "
                    "(customer_service.enabled / memory.per_customer)"
                )
            if self.customer_service.connections:
                raise ValueError(
                    "customer_service.connections requires multi-user mode "
                    "(customer_service.enabled / memory.per_customer)"
                )
            if self.customer_service.user_credentials:
                raise ValueError(
                    "customer_service.user_credentials requires multi-user mode "
                    "(customer_service.enabled / memory.per_customer)"
                )
        return self

    @model_validator(mode="after")
    def _inject_builtin_ops(self) -> AgentConfig:
        # Agents with the Cortex engine enabled also get the cortex_* item tools
        # so the user can drive ideas/brainstorms/prototypes/projects from chat
        # (list / view / add / edit / promote / explore / discard / restore).
        from olano.config.system_settings import cortex_enabled

        cortex_tools = CORTEX_TOOL_NAMES if cortex_enabled(self) else []
        # default_internal_tools: false ⇒ nothing is granted implicitly — the
        # merged list is exactly the explicit `tools:` (minus disabled).
        injected = (
            [*UNIVERSAL_AGENT_TOOL_NAMES, *cortex_tools] if self.default_internal_tools else []
        )
        # Customer Service mode: the operator's scheduler, heartbeat, PULSE,
        # approvals and ops queue have no place in a stranger's conversation
        # (customers get schedule_my_* via customer_service.user_scheduling),
        # so the ops defaults are not granted. `_sync_customer_service_mode`
        # has mirrored customer_service.enabled onto memory.per_customer by
        # now. An explicit `tools:` entry still binds, like every default.
        if self.memory.per_customer:
            injected = [n for n in injected if n not in OPS_TOOL_NAMES]
        disabled: set[str] = set(self.disabled_tools)
        seen_tools: set[str] = set()
        merged_tools: list[str] = []
        for name in [*self.tools, *injected]:
            if name not in seen_tools and name not in disabled:
                seen_tools.add(name)
                merged_tools.append(name)
        self.tools = merged_tools

        existing_names = {sub.name.lower() for sub in self.subagents}
        merged_subagents = list(self.subagents)

        # Support both legacy list[str] and new BuiltinSubagentConfig
        if isinstance(self.builtin_subagents, BuiltinSubagentConfig):
            # Structured form: resolve against the known built-in names
            enabled_names = self.builtin_subagents.resolve_names(_builtin_subagent_names())
            builtins_to_inject = _resolve_builtin_subagents(enabled_names)
            # Respect per-subagent `enabled` flag from existing subagents list
            for sub in merged_subagents:
                if not sub.enabled:
                    existing_names.add(sub.name.lower())
        else:
            builtins_to_inject = _resolve_builtin_subagents(self.builtin_subagents)

        for builtin in builtins_to_inject:
            if builtin.name.lower() not in existing_names:
                existing_names.add(builtin.name.lower())
                merged_subagents.append(builtin)
        self.subagents = merged_subagents
        return self
