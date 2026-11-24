"""Deployment Settings schema — the payload of every signed settings artifact.

Design rules (deployment-settings design record, §4):

- **Typed limits** carry structure (value + window). Tokens are non-negative
  integers; cost is integer **micro-USD** — no floating-point currency anywhere
  in enforcement. ``value=None`` means unlimited.
- **Open maps** for ``features`` / ``tabs``: a missing key means *enabled*, so
  new toggles need no schema change on either side. The cloud authoring schema
  is strict about names; this client parser is tolerant.
- Every model uses ``extra="allow"`` so unknown fields from a newer control
  plane survive round-trips (forward compatibility / diagnostics). Enforcement
  never keys off unknown fields — any new enforcement-relevant field bumps
  ``schema_version`` and older clients treat the artifact as unsupported.
- ``control_mode`` is deliberately NOT here: which source is authoritative is
  bootstrap (installation-manifest) configuration, not a signed setting.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_NAME = "olano.deployment-settings"
SUPPORTED_SCHEMA_VERSION = 1
AUDIENCE = "olano-deployment-settings"
ISSUER = "cloud.olano.ai"

WINDOWS: tuple[str, ...] = ("daily", "weekly", "monthly", "lifetime")
CHANNELS: tuple[str, ...] = ("cloud", "local")

# Canonical feature-toggle names. Missing key ⇒ enabled.
#
# `olano_models` gates the branded, Olano-managed tiers as a group; the
# per-tier names withhold one tier from a plan. `olano_model_flash` is the
# pre-launch name for `turbo`, kept so a settings document written before that
# rename still resolves.
#
# There is deliberately NO `olano_model_reasoning`: the top tier was renamed to
# `max` as a hard cutover. `features` is an open map, so a settings document
# still carrying that key withholds nothing and `olano_model_max` — absent —
# reads as enabled. Any plan that withheld the top tier must be re-issued under
# the new key. These gate **selection** on this box only — the cloud gateway
# re-authorizes every call, because a check that runs on customer-controlled
# hardware is a UI affordance, not an authorization.
KNOWN_FEATURES: tuple[str, ...] = (
    "shell",
    "terminal",
    "sandbox_exec",
    "father_agent",
    "cortex",
    "agent_config_raw",
    "model_selection",
    "olano_models",
    "olano_model_turbo",
    "olano_model_flash",  # legacy alias of turbo
    "olano_model_pro",
    "olano_model_max",
    # Per-service entitlement for the managed media/search services the product
    # calls on the agent's behalf. Missing ⇒ enabled (same open-map default as
    # every other feature), so existing deployments keep every service; a plan
    # withholds one by setting it False without touching the others. `olano_image`
    # covers both generation and edit (they share the olano-image gateway entry).
    "olano_vision",
    "olano_image",
    "olano_tts",
    "olano_stt",
    "olano_web_search",
    # LinkedIn outreach: a paid add-on (one seat per connected LinkedIn
    # account), not a plan feature. The cloud writes it true or false on every
    # artifact from the seat count, since a missing key reads as enabled. The
    # LinkedIn tools do not gate on it (the cloud's own `addon_required`
    # answer is current where this flag lags a poll); it is informational.
    "olano_linkedin_outreach",
    # BYOK: connecting the operator's own provider API keys (vault provider
    # env vars, Codex/subscription connects). Off on entry plans — Ultra and
    # up include it. Distinct from `model_selection` (which gates picking a
    # model at all): a box can have model_selection on with byok off, meaning
    # "choose among the Olano tiers only". Missing key ⇒ enabled (pre-epoch-5
    # artifacts), same open posture as every other feature.
    "byok",
    # Browser control (config.yaml `browser:`). `browser` gates every mode;
    # `browser_live` gates only the live mode, the full per-agent browser in its
    # own container that a person can watch and take over. Missing ⇒ enabled,
    # like every key above; a plan withholds live by sending it False. The
    # runtime also refuses either mode on hardware too small to carry it
    # (olano/browser/policy.py), so a plan flag is never the only guard.
    "browser",
    "browser_live",
)

# Dashboard tabs that can be hidden per deployment. Mirrors the NAV registry in
# dashboard_2.0/src/views/Layout.tsx. Missing key ⇒ visible.
KNOWN_TABS: tuple[str, ...] = (
    "overview",
    "insights",
    "chat",
    "agents",
    "cortex",
    "scheduler",
    "calendar",
    "files",
    "memory",
    "notes",
    "knowledge",
    "tools",
    "issues",
    "revisions",
    "providers",
    "connections",
    "vault",
    "activity",
    "orgchart",
    "logs",
    "terminal",
    "playground",
    "config",
    "admin",
)


class UsageLimit(BaseModel):
    """One usage budget: an integer amount per time window.

    ``value`` is tokens (for token limits) or micro-USD (for cost limits);
    ``None`` means unlimited. Unknown windows coerce to ``monthly`` rather than
    failing the whole artifact.
    """

    model_config = ConfigDict(extra="allow")

    value: int | None = Field(default=None, ge=0)
    window: str = "monthly"

    @field_validator("window", mode="before")
    @classmethod
    def _coerce_window(cls, v: object) -> str:
        return v if isinstance(v, str) and v in WINDOWS else "monthly"


class UsageLimits(BaseModel):
    """The six usage budgets. Cost values are integer micro-USD."""

    model_config = ConfigDict(extra="allow")

    input_tokens_per_agent: UsageLimit = Field(default_factory=UsageLimit)
    input_tokens_total: UsageLimit = Field(default_factory=UsageLimit)
    output_tokens_per_agent: UsageLimit = Field(default_factory=UsageLimit)
    output_tokens_total: UsageLimit = Field(default_factory=UsageLimit)
    cost_per_agent: UsageLimit = Field(default_factory=UsageLimit)
    cost_total: UsageLimit = Field(default_factory=UsageLimit)


class ResourceLimits(BaseModel):
    """Countable resource caps. ``None`` means unlimited."""

    model_config = ConfigDict(extra="allow")

    max_agents: int | None = Field(default=None, ge=0)
    max_connectors_per_agent: int | None = Field(default=None, ge=0)
    max_connectors_total: int | None = Field(default=None, ge=0)
    max_mcp_servers_total: int | None = Field(default=None, ge=0)
    max_webhooks_total: int | None = Field(default=None, ge=0)
    max_users: int | None = Field(default=None, ge=0)


class DeploymentSettings(BaseModel):
    """The ``settings`` object inside a signed artifact.

    Defaults are all-permissive: ``DeploymentSettings()`` is the OPEN posture.
    """

    model_config = ConfigDict(extra="allow")

    resources: ResourceLimits = Field(default_factory=ResourceLimits)
    usage: UsageLimits = Field(default_factory=UsageLimits)
    features: dict[str, bool] = Field(default_factory=dict)
    tabs: dict[str, bool] = Field(default_factory=dict)
    # Display-only placeholders (authorize nothing; surfaced in status/UI).
    credits_left: float | None = None
    olano_model_credits: dict[str, float] = Field(default_factory=dict)
    # The unit every cost in the dashboard renders in ("credits" | "usd").
    # Signed and cloud-controlled BY DESIGN: on a managed box neither the
    # operator (config.yaml) nor the agent may flip the display back to raw
    # provider dollars: that number is Olano's own supply cost, not the
    # customer's price. None ⇒ the managed default, "credits". Resolution lives in
    # config/system_settings.cost_display_mode().
    cost_display: str | None = None
    # Explicit bag for non-enforcement metadata from the control plane.
    extensions: dict = Field(default_factory=dict)

    def feature_enabled(self, name: str) -> bool:
        """Missing key ⇒ enabled."""
        return bool(self.features.get(name, True))

    def tab_enabled(self, name: str) -> bool:
        """Missing key ⇒ visible."""
        return bool(self.tabs.get(name, True))


class SignedSettings(BaseModel):
    """The full signed payload: identity + lifecycle claims + settings.

    Timestamps are integer epoch seconds. ``expires_at`` is required — no
    perpetual artifacts. ``refresh_after`` is optional (local licenses have no
    polling cadence). Validation of the *relationships* between claims (expiry
    vs issue time, clock skew, channel/deployment binding) lives in
    :mod:`olano.deployment.envelope`, not here.
    """

    model_config = ConfigDict(extra="allow")

    schema_: str = Field(alias="schema")
    schema_version: int
    issuer: str
    audience: str
    channel: str
    license_id: str | None = None
    deployment_id: str
    plan: str | None = None
    policy_revision: int = Field(ge=0)
    issued_at: int
    not_before: int | None = None
    refresh_after: int | None = None
    expires_at: int
    settings: DeploymentSettings = Field(default_factory=DeploymentSettings)


# The OPEN posture: what an unmanaged installation (and every pre-existing
# install) effectively runs under. Everything unlimited, everything enabled.
OPEN_SETTINGS = DeploymentSettings()

# The QUARANTINE posture: recovery-only surface for managed installs with no
# valid artifact (expired/tampered/revoked/suspended). Kept deliberately
# usable — the customer must be able to see what's wrong and re-license — but
# nothing more (deployment-settings design record, §5). The turn-level "only the
# default agent may speak" rule is enforced by posture in
# olano.deployment.enforcement, not by these numbers alone.
QUARANTINE_SETTINGS = DeploymentSettings(
    resources=ResourceLimits(
        max_agents=1,
        max_connectors_per_agent=0,
        max_connectors_total=0,
        max_mcp_servers_total=0,
        max_webhooks_total=0,
        max_users=1,
    ),
    usage=UsageLimits(
        # Small recovery allowance: enough to diagnose + re-license, monthly.
        input_tokens_per_agent=UsageLimit(value=200_000, window="monthly"),
        input_tokens_total=UsageLimit(value=200_000, window="monthly"),
        output_tokens_per_agent=UsageLimit(value=100_000, window="monthly"),
        output_tokens_total=UsageLimit(value=100_000, window="monthly"),
        cost_per_agent=UsageLimit(value=5_000_000, window="monthly"),  # $5
        cost_total=UsageLimit(value=5_000_000, window="monthly"),
    ),
    features={
        "shell": False,
        "terminal": False,
        "father_agent": False,
        "cortex": False,
        "agent_config_raw": False,
        "model_selection": False,
        "olano_models": False,
        "byok": False,
    },
    tabs={
        "terminal": False,
        "cortex": False,
        "playground": False,
        "insights": False,
        "activity": False,
        "orgchart": False,
    },
)
