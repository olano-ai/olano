"""Global Olano system settings — home-wide cognition / autonomy switches.

These live in ``~/.olano/config.yaml`` under a ``cognition:`` block and toggle
whole feature groups on/off system-wide, plus the master **autonomy** posture and
**intensity** (cadence) of the always-on Cortex engine. They are intentionally
separate from :class:`~olano.config.home.HomeConfig` (which is dataclass-backed
and would clobber unknown keys on save) — this module reads/writes only the
``cognition`` sub-tree and preserves everything else in the file.

Feature groups (all default ON) — these mirror the dashboard's Cortex tiles:

- ``self_learning`` — session distillation + deep self-learning + self-improve
- ``brainstorming`` — autonomous brainstorming (BRAINSTORMING.md)
- ``idea_generation`` — idea distillation + expansion (IDEAS.md)
- ``prototyping`` — scaffolding runnable prototypes (prototypes/)
- ``skills_optimization`` — skill extraction (SkillSmith) + skill librarian
- ``workspace_optimization`` — identity/behavior file dedupe + self-improve proposals
- ``memory_optimization`` — memory gardening (MEMORY.md dedupe/merge)
- ``pulse`` — PulseAgent curation + outcome pulses

Top-level switches:

- ``enabled`` — master kill-switch for ALL cognition (manual *and* autonomous).
  Defaults OFF: the Cortex engine is opt-in, fleet-wide via this key (the
  dashboard's "Cortex engine" row, All agents column) or per agent via
  ``agent.json5 → cognition.enabled: true``, which wins over the global.
- ``autonomous`` — when True (default) the always-on Cortex engine schedules every
  enabled feature; when False, cognition is manual-only (dashboard "Think" / API).
- ``intensity`` — ``relaxed`` (default) | ``balanced`` | ``aggressive``. Controls
  how often the autonomous engine runs each phase + the daily deep-run cap. The
  concrete cadence table lives in :mod:`olano.cognition.orchestrator`.

Resolution: a feature *runs* only if the global master switch is on **and** the
global feature toggle is on **and** the per-agent ``cognition`` override is not
explicitly disabling it. See :func:`feature_enabled`. Whether it runs *on its own*
additionally requires :func:`autonomy_enabled`.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from olano.config.cortex_funnel import (
    DEFAULT_AUTO_DISCARD,
    DEFAULT_MAX_PROMOTIONS_PER_RUN,
    DEFAULT_MAX_SELF_SCHEDULED_TASKS,
    DEFAULT_PROMOTE_THRESHOLD,
    clamp_max_promotions,
    clamp_promote_threshold,
    clamp_self_scheduled,
)
from olano.config.home import (
    OlanoHomePaths,
    get_olano_paths,
    read_home_config_doc,
    write_home_config_text,
)
from olano.security.delegate_guards import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_WORKER_CONCURRENCY,
    clamp_max_iterations,
    clamp_timeout_seconds,
    clamp_worker_concurrency,
)

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

logger = logging.getLogger(__name__)

# Canonical feature-group names. Each maps to a field on CognitionFeatureToggles
# AND a same-named optional override field on the per-agent CognitionConfig.
FEATURES: tuple[str, ...] = (
    "self_learning",
    "brainstorming",
    "idea_generation",
    "prototyping",
    "skills_optimization",
    "workspace_optimization",
    "memory_optimization",
    "pulse",
)

# Cadence presets for the autonomous engine. The concrete per-phase schedule that
# each maps to is defined in olano.cognition.orchestrator.
INTENSITIES: tuple[str, ...] = ("relaxed", "balanced", "aggressive")

#: The cadence a box runs with when ``cognition.intensity`` is unset, and what
#: an unknown value (a typo, a retired preset) falls back to. The cheapest
#: preset: a fleet that never chose a cadence should not pay for the busiest one.
#: The single definition: the settings model, :func:`effective_intensity` and the
#: orchestrator's lookup fallbacks all read it, so they cannot disagree.
DEFAULT_INTENSITY = "relaxed"

# Pre-2026-06 feature names → canonical names, so old configs / call sites keep
# resolving. (Pre-release: kept only as a thin safety net, not a migration story.)
_FEATURE_ALIASES: dict[str, str] = {
    "learned_skills": "skills_optimization",
    "ideas_brainstorming_prototyping": "idea_generation",
    "memory_garden": "memory_optimization",
    "workspace_optimize": "workspace_optimization",
}


def canonical_feature(feature: str) -> str:
    """Resolve a (possibly legacy) feature name to its canonical form."""
    return _FEATURE_ALIASES.get(feature, feature)


class CognitionFeatureToggles(BaseModel):
    """Home-wide on/off switches for each cognition feature group."""

    self_learning: bool = True
    brainstorming: bool = True
    idea_generation: bool = True
    prototyping: bool = True
    skills_optimization: bool = True
    workspace_optimization: bool = True
    memory_optimization: bool = True
    pulse: bool = True


class HeartbeatSettings(BaseModel):
    """The ``heartbeat:`` block of ``~/.olano/config.yaml`` — home-wide defaults
    for the HEARTBEAT.md poll. Per-agent ``agent.json5 → heartbeat`` fields
    override these when set (non-None)."""

    enabled: bool = True
    interval_minutes: int = 60  # DEFAULT_HEARTBEAT_INTERVAL_MINUTES


class PlaygroundSettings(BaseModel):
    """The ``playground:`` block of ``~/.olano/config.yaml``.

    Home-wide switch for the dashboard Playground (the animated 2D world the
    agents walk around in). Turning it off:

    - hides the Playground nav item / view in the dashboard, and
    - stops character generation from rendering the animation **sprite sheets** —
      the (slow, multi-call) image-model step. The cheap **portrait** (the
      ``avatar.png`` profile cartoon) is still generated either way.

    Defaults OFF — the playground (and its expensive sprite generation) is
    opt-in; set ``playground.enabled: true`` in ``config.yaml`` to turn it on.
    """

    enabled: bool = False


class SelfServeAgentSettings(BaseModel):
    """The ``self_serve_agents:`` block of ``~/.olano/config.yaml``.

    Home-wide switch for **self-serve agent creation**: whether a non-admin
    member may create their own agent through ``POST /api/agents`` instead of
    asking an operator. Everything else about the member's posture is
    unchanged — this is the single narrowest exemption to the
    "agent configuration is admin-only" rule in
    ``runtime/auth.py:_member_forbidden``, and it covers exactly that one
    path (never ``/api/agents/{name}/…``).

    Defaults **ON** — "an assistant of your own" is the product every member
    of a team install is promised, so a box that never sets the block hands
    each member the New-bot button, bounded by the quota and the name/slug
    validation below. It shipped default-off for one release; that made the
    feature invisible on exactly the deployments it was built for (a managed
    box has no SSH, and until the Config toggle existed nothing could turn it
    on). Operators who do not want members creating bots set
    ``self_serve_agents.enabled: false`` — an explicit value always wins —
    from Config → Member bot creation or config.yaml.

    The runtime enforces the switch twice: the middleware exemption only
    opens the door, the handler re-checks it and applies the limits.

    Fields:

    - ``enabled``: master switch. Default ``True`` (opt-out).
    - ``max_per_user`` — how many agents one human may **own** (the
      ``agent_access`` rows with ``role='owner'``). Admins are exempt. Clamped
      to 1..100; default 3.
    - ``allowed_archetypes`` — when non-empty, a self-serve create must name
      an archetype from this list. Empty (the default) means *any* archetype in
      the catalog, and also allows a from-scratch create with no archetype at
      all.
    """

    enabled: bool = True
    max_per_user: int = 3
    allowed_archetypes: list[str] = Field(default_factory=list)

    @field_validator("max_per_user")
    @classmethod
    def _clamp_quota(cls, v: int) -> int:
        return max(1, min(int(v), 100))

    @field_validator("allowed_archetypes", mode="before")
    @classmethod
    def _clean_archetypes(cls, v: Any) -> Any:
        """A scalar or a list of junk normalizes to a clean list of slugs.

        A hand-edited ``allowed_archetypes: merli`` must not raise (the loader
        would fall back to defaults and silently allow *everything*, which is
        the wrong direction for a restriction).
        """
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list | tuple):
            return []
        return [str(item).strip() for item in v if str(item).strip()]


# The canonical capability names for chat self-configuration — one per field of
# AgentAutonomySettings / agent.json5's AutonomyConfig, and the vocabulary
# `autonomy_allowed()` accepts. The self_* toolkit, the builder's default
# binding, the internal-tools catalog and the dashboard knobs all speak these
# names, so a rename here is a rename everywhere.
AUTONOMY_CAPABILITIES: tuple[str, ...] = (
    "explore_archetypes",
    "change_model",
    "modify_prompt",
    "restart_self",
    "add_subagents",
    "connect_mcps",
    "connect_platforms",
    "manage_skills",
    "magic_links",
    "external_access",
)


class AgentAutonomySettings(BaseModel):
    """The ``agent_autonomy:`` block of ``~/.olano/config.yaml``.

    Home-wide defaults for **chat self-configuration**: what an agent may
    change about itself from inside a conversation, via the ``self_*`` tools
    (rewrite its own system prompt, switch its model, restart, add subagents,
    browse the archetype library, wire MCP servers and messaging channels onto
    itself). Per-agent ``agent.json5 → autonomy`` fields override these when
    set (non-``None``) — the usual two-tier rule, resolved only through
    :func:`autonomy_allowed`.

    Every capability defaults **ON** — self-configuring agents are the
    product posture ("ask your bot to hook itself up to Notion"), and each
    self-change stays inside the guardrails that already exist: config writes
    go through the validated admin layer, mutations are HITL-gated in the
    stricter security profiles, and every ``agent.json5`` change lands in the
    revision history attributed to the acting agent. An operator who wants a
    quieter fleet turns capabilities off here; one agent can be re-opened (or
    closed) individually from its own Autonomy settings. The one built-in
    exception: **customer-service agents** (``memory.per_customer``) inherit
    OFF — see :func:`autonomy_allowed`.

    These knobs gate the agent acting on ITSELF only. Fleet-scoped management
    (AgentFather's ``olano_admin`` toolkit) is an explicit ``tools:`` grant
    and is not touched by this block.

    ``magic_links`` is the one capability that is not a self-change: it grants
    the ``magic_link_*`` tools, which hand a human a short-lived, PIN-protected
    page to finish something the agent cannot do from a chat — sign in to a
    service, type a key straight into the vault, connect a messenger by QR,
    edit a file, open the dashboard on their phone. It rides this block so an
    operator has one switch for "may this bot send links?" next to the other
    self-configuration switches, and inherits the same per-customer default.
    The links themselves stay governed by ``magic_links:`` (``enabled`` is the
    hard off switch, ``owner_login.enabled`` gates the dashboard link).
    """

    explore_archetypes: bool = True
    change_model: bool = True
    modify_prompt: bool = True
    restart_self: bool = True
    add_subagents: bool = True
    connect_mcps: bool = True
    connect_platforms: bool = True
    manage_skills: bool = True
    magic_links: bool = True
    # Let the agent switch its own inbound exposure (MCP / A2A) and mint access
    # tokens for outside callers — through `self_set_external_access` and
    # `magic_link_mint_access_token`. The tools additionally require an ADMIN
    # human on the turn, so this capability only decides whether they exist.
    external_access: bool = True


class GroupThreadSettings(BaseModel):
    """The ``groups:`` block of ``~/.olano/config.yaml``.

    Home-wide switch for **multi-bot group threads** (``olano/groups/``,
    ``/api/groups*``): a human-visible conversation two to six agents share,
    where a posted message is answered by the members it names. Defaults **ON**
    — while off, every route 404s and the simple shell draws no group surface
    at all.

    The default used to be off, which made a fully built feature unreachable:
    the whole group UI is gated on the ``groups_enabled`` capability, and
    nothing outside a hand-edit of ``config.yaml`` could set it. On is the
    honest default for the plain behaviour — you post, the members you named
    answer, and the thread stops — which costs no more model calls than the
    1:1 chat the same message would otherwise go to.

    ``min_members``/``max_members`` bound a group's roster. The upper bound is
    a real cost guard (each member is a booted agent holding its own thread
    state), so it is clamped to the 2..6 the feature is designed around.

    ``autonomous`` is a **second, independent switch**, and it defaults **on**
    as well. With it on, a reply that addresses another member by ``@name``
    hands that member the next turn, with no human in between
    (``olano/groups/cascade.py``); with it off, the members a human named
    answer and the thread waits for the human. It shipped off at first because
    it is the half that spends *unattended* model calls, and that is still
    what it does — it is on because a group whose members cannot answer each
    other is a switchboard rather than a conversation, and the budgets below
    bound the spend. The two switches move independently, and an install
    upgrading from a release where this defaulted off starts cascading unless
    its ``config.yaml`` spells ``autonomous: false``.

    A cascade is an agent loop starting another agent loop, so it is bounded
    twice over and both bounds are clamped here rather than trusted from the
    file:

    * ``max_hops`` — bot-to-bot turns per human message (default 3, clamped
      1..10). Spending it settles the thread with a visible note.
    * ``cascade_timeout_seconds`` — wall clock for the whole cascade (default
      180, clamped 10..1800), so a slow model cannot pin a worker.
    """

    enabled: bool = True
    min_members: int = 2
    max_members: int = 6
    autonomous: bool = True
    max_hops: int = 3
    cascade_timeout_seconds: int = 180

    @field_validator("min_members")
    @classmethod
    def _clamp_min(cls, v: int) -> int:
        return max(2, min(int(v), 6))

    @field_validator("max_members")
    @classmethod
    def _clamp_max(cls, v: int) -> int:
        return max(2, min(int(v), 6))

    @field_validator("max_hops")
    @classmethod
    def _clamp_hops(cls, v: int) -> int:
        # 0 would read as "autonomous but mute"; the ceiling is what stops a
        # hand-edited config from turning the cascade into an unbounded loop.
        return max(1, min(int(v), 10))

    @field_validator("cascade_timeout_seconds")
    @classmethod
    def _clamp_cascade_timeout(cls, v: int) -> int:
        return max(10, min(int(v), 1800))

    @model_validator(mode="after")
    def _ordered(self) -> GroupThreadSettings:
        if self.max_members < self.min_members:
            self.max_members = self.min_members
        return self


# Allow-listed disk-cleanup action names. The concrete specs (argv, timeouts,
# guards) live in olano.runtime.disk_monitor.ACTION_REGISTRY; this tuple
# mirrors its keys so config validation never imports the runtime package.
# Every action here runs by default; deselecting is the opt-out (dashboard
# Config → Disk, config.yaml, or `olano disk`).
DISK_CLEANUP_ACTIONS: tuple[str, ...] = (
    "olano_caches",
    "revisions_gc",
    "olano_stale_images",
    "docker_image_prune",
    "docker_prune",
    "npm_cache",
    "journald_vacuum",
)

# Renamed actions: stored configs (and old dashboard tabs) may still say the
# legacy name — normalize instead of silently dropping the action, which would
# turn that cleanup off for everyone who ever saved an explicit action list.
DISK_CLEANUP_ACTION_ALIASES: dict[str, str] = {
    "lionclaw_caches": "olano_caches",
}

# The subset that only ever deletes caches/old logs — nothing another program
# would miss. docker_prune is NOT in it: it removes ALL unused Docker
# images/containers host-wide, so it wears the "aggressive" badge in the
# dashboard even though it is part of the default action set too.
# docker_image_prune is safe: dangling images only — an untagged image is by
# definition one nothing references; the leaked previous versions after an
# image update are exactly this.
# olano_stale_images is safe for the same reason, one step further: it removes
# superseded copies of images WE publish (tagged ones too — dangling-only
# pruning misses those), while never touching an image a container uses, the
# on-demand sandbox image, or any repo that isn't ours.
DISK_CLEANUP_SAFE_ACTIONS: tuple[str, ...] = (
    "olano_caches",
    "revisions_gc",
    "olano_stale_images",
    "docker_image_prune",
    "npm_cache",
    "journald_vacuum",
)

# The vacuum size is interpolated into ``journalctl --vacuum-size=<value>`` —
# accept only journald's own size grammar so config can never smuggle flags.
_VACUUM_SIZE_RE = re.compile(r"^\d+[KMGT]?$")

DISK_CHECK_INTERVAL_MIN_MINUTES = 5
DISK_CHECK_INTERVAL_MAX_MINUTES = 24 * 60
DISK_CLEANUP_COOLDOWN_MIN_MINUTES = 30
DISK_CLEANUP_COOLDOWN_MAX_MINUTES = 7 * 24 * 60


def _clamp_int(v: object, *, default: int, lo: int, hi: int) -> int:
    try:
        value: int = int(v)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


class DiskCleanupSettings(BaseModel):
    """The ``disk_monitor.cleanup:`` sub-block — what runs when disk fills up.

    **Enabled by default with every registry action selected**
    (:data:`DISK_CLEANUP_ACTIONS`), including the aggressive ``docker_prune``
    (removes ALL unused Docker images/containers host-wide — deselect it via
    the dashboard Config → Disk section, config.yaml, or ``olano disk`` if
    other software on the host relies on cached Docker data). ``actions`` can
    only select from that fixed allow-list — free-form commands are
    deliberately unsupported.
    """

    enabled: bool = True
    actions: list[str] = Field(default_factory=lambda: list(DISK_CLEANUP_ACTIONS))
    cooldown_minutes: int = 360  # min gap between automatic cleanups
    force_interval_days: int = 0  # 0 = threshold-only; >0 also clean every N days
    journald_vacuum_size: str = "200M"

    @field_validator("actions", mode="before")
    @classmethod
    def _coerce_actions(cls, v: object) -> list[str]:
        """Keep only known action names (registry order, deduped, legacy names
        renamed); a non-list hand-edit falls back to the full default set."""
        if not isinstance(v, list | tuple):
            return list(DISK_CLEANUP_ACTIONS)
        requested = {DISK_CLEANUP_ACTION_ALIASES.get(str(item), str(item)) for item in v}
        return [name for name in DISK_CLEANUP_ACTIONS if name in requested]

    @field_validator("cooldown_minutes", mode="before")
    @classmethod
    def _coerce_cooldown(cls, v: object) -> int:
        return _clamp_int(
            v,
            default=360,
            lo=DISK_CLEANUP_COOLDOWN_MIN_MINUTES,
            hi=DISK_CLEANUP_COOLDOWN_MAX_MINUTES,
        )

    @field_validator("force_interval_days", mode="before")
    @classmethod
    def _coerce_force_days(cls, v: object) -> int:
        return _clamp_int(v, default=0, lo=0, hi=90)

    @field_validator("journald_vacuum_size", mode="before")
    @classmethod
    def _coerce_vacuum_size(cls, v: object) -> str:
        if isinstance(v, int) and v > 0:
            return str(v)
        if isinstance(v, str) and _VACUUM_SIZE_RE.match(v.strip()):
            return v.strip()
        return "200M"


class DiskScanSettings(BaseModel):
    """The ``disk_monitor.scan:`` sub-block — the daily directory-size scan.

    One throttled walk of ``~/.olano`` (niced thread, sleep between entry
    batches, hard time budget) aggregated into depth-limited buckets: ``depth``
    levels below the home root plus ``agent_depth`` levels below each
    ``agents/<name>``. At most one scan per ``interval_hours`` — the stamp
    lives in the disk-monitor state file, so restarts don't re-trigger it.
    Results feed the dashboard (Overview DiskCard breakdown, Files view
    directory sizes), ``olano disk`` and the cloud disk report.
    """

    enabled: bool = True
    interval_hours: int = 24
    depth: int = 2
    agent_depth: int = 2
    time_budget_seconds: int = 600
    throttle_ms: int = 20  # sleep per batch of scanned entries; 0 = no throttle
    max_entries: int = 400  # keep the largest N buckets in the snapshot

    @field_validator("interval_hours", mode="before")
    @classmethod
    def _coerce_interval_hours(cls, v: object) -> int:
        return _clamp_int(v, default=24, lo=1, hi=24 * 7)

    @field_validator("depth", mode="before")
    @classmethod
    def _coerce_depth(cls, v: object) -> int:
        return _clamp_int(v, default=2, lo=1, hi=4)

    @field_validator("agent_depth", mode="before")
    @classmethod
    def _coerce_agent_depth(cls, v: object) -> int:
        return _clamp_int(v, default=2, lo=0, hi=4)

    @field_validator("time_budget_seconds", mode="before")
    @classmethod
    def _coerce_time_budget(cls, v: object) -> int:
        return _clamp_int(v, default=600, lo=30, hi=3600)

    @field_validator("throttle_ms", mode="before")
    @classmethod
    def _coerce_throttle(cls, v: object) -> int:
        return _clamp_int(v, default=20, lo=0, hi=1000)

    @field_validator("max_entries", mode="before")
    @classmethod
    def _coerce_max_entries(cls, v: object) -> int:
        return _clamp_int(v, default=400, lo=50, hi=2000)


class DiskMonitorSettings(BaseModel):
    """The ``disk_monitor:`` block of ``~/.olano/config.yaml``.

    A background loop in the runtime server measures ``shutil.disk_usage(path)``
    every ``check_interval_minutes`` and alerts (runtime log + dashboard SSE)
    from ``warn_percent``. At ``cleanup_percent`` it additionally runs the
    enabled cleanup actions (all of them by default — see
    :class:`DiskCleanupSettings`). The loop re-reads this block each tick, so
    changes apply within one interval without a restart. Kill switches:
    ``enabled: false`` here, ``cleanup.enabled: false`` for just the cleanup
    half, or ``LIONCLAW_NO_DISK_MONITOR=1`` at boot. The ``scan`` sub-block
    drives the daily directory-size scan (see :class:`DiskScanSettings`).
    """

    enabled: bool = True
    path: str = "/"  # filesystem to measure (shutil.disk_usage target)
    check_interval_minutes: int = 30
    warn_percent: int = 80
    cleanup_percent: int = 85
    cleanup: DiskCleanupSettings = Field(default_factory=DiskCleanupSettings)
    scan: DiskScanSettings = Field(default_factory=DiskScanSettings)

    @field_validator("path", mode="before")
    @classmethod
    def _coerce_path(cls, v: object) -> str:
        return v.strip() if isinstance(v, str) and v.strip() else "/"

    @field_validator("check_interval_minutes", mode="before")
    @classmethod
    def _coerce_interval(cls, v: object) -> int:
        return _clamp_int(
            v,
            default=30,
            lo=DISK_CHECK_INTERVAL_MIN_MINUTES,
            hi=DISK_CHECK_INTERVAL_MAX_MINUTES,
        )

    @field_validator("warn_percent", mode="before")
    @classmethod
    def _coerce_warn_percent(cls, v: object) -> int:
        return _clamp_int(v, default=80, lo=50, hi=99)

    @field_validator("cleanup_percent", mode="before")
    @classmethod
    def _coerce_cleanup_percent(cls, v: object) -> int:
        return _clamp_int(v, default=85, lo=50, hi=99)

    @model_validator(mode="after")
    def _order_thresholds(self) -> DiskMonitorSettings:
        """Cleanup can never trigger below warn — coerce rather than error."""
        if self.cleanup_percent < self.warn_percent:
            self.cleanup_percent = self.warn_percent
        return self


#: Spellings that mean "no ceiling at all" for a token budget, so an operator
#: can lift one without inventing a big number. A *bare* ``null`` deliberately
#: does NOT mean this — it reads as "no opinion", i.e. inherit the deployment
#: value, which is what an empty dashboard field also sends.
UNLIMITED_SPELLINGS = frozenset({"unlimited", "none", "off", "no-limit", "nolimit", "∞"})

#: Which usage budgets ``usage_budgets:`` may override. Token ceilings only —
#: see :class:`UsageBudgetSettings` for why the cost budgets are not here.
OVERRIDABLE_USAGE_LIMITS: tuple[str, ...] = (
    "input_tokens_per_agent",
    "input_tokens_total",
    "output_tokens_per_agent",
    "output_tokens_total",
)

#: Fractions of a budget worth telling the operator about, in ascending order.
#: 100 is always alerted on and is not listed here — that one is the refusal.
DEFAULT_BUDGET_ALERT_PERCENTS: tuple[int, ...] = (50, 60, 70, 80, 90, 95, 99)


class UsageBudgetLimit(BaseModel):
    """One locally-configured token ceiling. ``value: None`` = unlimited."""

    value: int | None = Field(default=None, ge=0)
    window: str = "monthly"

    @field_validator("window", mode="before")
    @classmethod
    def _coerce_window(cls, v: object) -> str:
        allowed = ("daily", "weekly", "monthly", "lifetime")
        return v if isinstance(v, str) and v in allowed else "monthly"


class UsageBudgetSettings(BaseModel):
    """The ``usage_budgets:`` block of ``~/.olano/config.yaml``.

    Deployment usage budgets are issued by the olano control plane in a signed
    artifact. This block lets an operator override the **token** ceilings of
    that artifact and choose when the dashboard warns about any of them::

        usage_budgets:
          input_tokens_per_agent: 50_000_000_000   # a bigger ceiling
          output_tokens_total: unlimited           # no ceiling at all
          alert_percents: [50, 75, 90, 99]

    Three states per key, and the difference matters: **absent** (or a bare
    ``null``) inherits the deployment value, an **integer** replaces it, and
    the word ``unlimited`` removes the ceiling entirely.

    **Only token budgets are overridable, never ``cost_per_agent`` /
    ``cost_total``.** The token ceilings exist to stop a runaway loop, not to
    meter anyone: on BYOK the tokens are the customer's own provider spend, and
    on managed models the gateway independently enforces the deployment's
    credit balance, so raising a token ceiling here cannot let a box spend what
    it does not have. The cost budgets are the ones that bound olano's own
    upstream bill, so they stay where they are signed. A ``cost_*`` key written
    into this block is ignored rather than honored.

    ``alert_percents`` drives the dashboard notification raised as a budget
    fills — once per threshold per window per budget, tokens and cost alike.
    """

    input_tokens_per_agent: UsageBudgetLimit | None = None
    input_tokens_total: UsageBudgetLimit | None = None
    output_tokens_per_agent: UsageBudgetLimit | None = None
    output_tokens_total: UsageBudgetLimit | None = None
    alerts_enabled: bool = True
    alert_percents: list[int] = Field(default_factory=lambda: list(DEFAULT_BUDGET_ALERT_PERCENTS))

    @field_validator(*OVERRIDABLE_USAGE_LIMITS, mode="before")
    @classmethod
    def _coerce_limit(cls, v: object) -> object:
        """``None``/junk → inherit · ``"unlimited"`` → no ceiling · int → value.

        Never raises: a malformed entry reads as "no opinion" so one typo in
        one key cannot reset every other budget on the box.
        """
        if v is None or isinstance(v, UsageBudgetLimit):
            return v
        if isinstance(v, str):
            text = v.strip().lower().replace("_", "")
            if not text:
                return None
            if text in UNLIMITED_SPELLINGS:
                return {"value": None}
            try:
                return {"value": max(0, int(text))}
            except ValueError:
                return None
        if isinstance(v, bool):  # `true`/`false` say nothing about a ceiling
            return None
        if isinstance(v, int | float):
            return {"value": max(0, int(v))}
        if isinstance(v, dict):
            # `{value: null}` is the long form of "unlimited"; a dict with no
            # `value` key at all is an empty opinion.
            return v if "value" in v else None
        return None

    @field_validator("alert_percents", mode="before")
    @classmethod
    def _coerce_percents(cls, v: object) -> list[int]:
        if not isinstance(v, list | tuple):
            return list(DEFAULT_BUDGET_ALERT_PERCENTS)
        out: set[int] = set()
        for item in v:
            if isinstance(item, bool):
                continue
            try:
                pct = int(item)  # type: ignore[call-overload]
            except (TypeError, ValueError):
                continue
            # 100 is always alerted (the budget is spent); anything at or above
            # it here would just duplicate that row.
            if 1 <= pct <= 99:
                out.add(pct)
        return sorted(out)


class CatalogSyncSettings(BaseModel):
    """The ``catalog_sync:`` block of ``~/.olano/config.yaml``.

    Governs the background :class:`olano.catalog.sync.CatalogSyncService`:
    how often the runtime polls the cloud's tiny per-catalog revision manifest
    (``GET /api/v1/catalog/manifest``) to decide whether the model catalog /
    MCP directory need re-fetching. The service re-reads this block every
    tick, so changes apply within one interval without a restart. Kill
    switches: ``enabled: false`` here, or ``LIONCLAW_NO_CATALOG_SYNC=1`` at
    boot. Disabling never breaks browsing — the disk cache and bundled
    snapshots keep serving, just without updates.
    """

    enabled: bool = True
    manifest_poll_minutes: int = 60

    @field_validator("manifest_poll_minutes", mode="before")
    @classmethod
    def _coerce_poll_minutes(cls, v: object) -> int:
        return _clamp_int(v, default=60, lo=15, hi=24 * 60)


COST_DISPLAY_MODES = ("usd", "credits")


class CreditsSettings(BaseModel):
    """The ``credits:`` block of ``~/.olano/config.yaml``.

    Chooses the **unit every cost in this deployment is displayed in**:

    - ``usd`` (default) — raw dollars, e.g. ``$1,573.43``.
    - ``credits`` — olano credits, e.g. ``157,343 credits``, converted at the
      rate published by the cloud (:mod:`olano.catalog.credits_registry`,
      v1 rate ``$1 = 1,000 credits``). The rate is **never** stored here: it
      is synced, so repricing a credit does not need a config edit or a
      redeploy.

    Display only. Accounting stays in USD end to end — the usage ledger, the
    threadcost service and every per-call record are unchanged — so flipping
    this back and forth never rewrites history and never loses precision.

    ``LIONCLAW_COST_DISPLAY=usd|credits`` overrides the file (useful for a
    one-off CLI run or a per-container override); see :func:`cost_display_mode`.
    """

    display: str = "usd"

    @field_validator("display", mode="before")
    @classmethod
    def _coerce_display(cls, v: object) -> str:
        """Unknown values fall back to ``usd`` rather than erroring — a typo in
        the config must not blank out every cost in the dashboard."""
        if isinstance(v, str) and v.strip().lower() in COST_DISPLAY_MODES:
            return v.strip().lower()
        return "usd"


class IssuesSettings(BaseModel):
    """The ``issues:`` block of ``~/.olano/config.yaml``.

    Home-wide defaults for the per-agent issue-tracker poll
    (:mod:`olano.scheduler.cron` ``_add_issues_job``): every agent checks
    for open assigned issues with new activity and processes them in one
    autonomous turn. Deliberately independent of the heartbeat — issue
    delivery must not depend on an unrelated feature being enabled.
    Per-agent overrides live in ``agent.json5 → issues:``
    (``auto_process``, ``poll_minutes``); ``auto_process: false`` is manual
    mode, where issues wait for a human prompt or a scheduled job.
    """

    auto_process: bool = True
    poll_minutes: int = 15

    @field_validator("poll_minutes", mode="before")
    @classmethod
    def _coerce_poll_minutes(cls, v: object) -> int:
        return _clamp_int(v, default=15, lo=1, hi=24 * 60)


class RevisionsRemoteSettings(BaseModel):
    """``revisions.remote:``: the GitHub repository the history syncs with.

    Written by the dashboard's Revisions page (``olano.revisions.remote``),
    never by hand in the normal flow. ``url`` empty means not connected.
    ``auth`` picks the credential: ``token`` (a pasted access token, stored
    in the vault under a runtime-reserved name, never here) or ``connection``
    (the deployment's GitHub connection from the Connections page). ``sync``
    is when local commits go up on their own: ``manual`` (only the Push
    button), ``daily`` (at most once a day), or ``on_commit`` (after every
    commit). Pulling is always manual. Nothing in this block is secret, and
    that matters: ``config.yaml`` is itself in the history being pushed.
    """

    url: str = ""
    branch: str = "main"
    auth: Literal["token", "connection"] = "token"
    sync: Literal["manual", "daily", "on_commit"] = "manual"

    @field_validator("auth", mode="before")
    @classmethod
    def _coerce_auth(cls, v: object) -> str:
        return v if v in ("token", "connection") else "token"

    @field_validator("sync", mode="before")
    @classmethod
    def _coerce_sync(cls, v: object) -> str:
        return v if v in ("manual", "daily", "on_commit") else "manual"

    @field_validator("url", "branch", mode="before")
    @classmethod
    def _coerce_str(cls, v: object) -> str:
        return v.strip() if isinstance(v, str) else ""


class RevisionsSettings(BaseModel):
    """The ``revisions:`` block of ``~/.olano/config.yaml``.

    Governs automatic revision tracking of the home
    (:mod:`olano.revisions`): allowlisted config + markdown files are
    committed to the bare repo at ``<home>/revisions.git`` by a background
    worker, authored by the agent whose turn changed them. ``enabled: false``
    stops the worker at the next server boot (existing history is kept —
    delete ``revisions.git`` to purge it). ``include_memory: false`` excludes
    ``MEMORY.md`` / ``memories/*.md`` for deployments with data-deletion
    obligations. ``debounce_seconds`` is the quiet period after the last
    change before a commit; ``max_file_kb`` (default 50 MiB, below GitHub's
    per-file warning; ceiling 100 MiB, GitHub's hard per-file limit) holds
    back any tracked file that outgrows it:
    its last recorded version stays in history until it fits again.
    ``remote`` is the optional GitHub sync (:class:`RevisionsRemoteSettings`).
    """

    enabled: bool = True
    include_memory: bool = True
    debounce_seconds: int = 45
    # 50 MiB by default: GitHub warns on every push carrying a larger file,
    # and every version of a big file stays in history. 100 MiB, GitHub's
    # hard per-file limit, is the most an operator can choose.
    max_file_kb: int = 50 * 1024
    remote: RevisionsRemoteSettings = Field(default_factory=RevisionsRemoteSettings)

    @field_validator("remote", mode="before")
    @classmethod
    def _coerce_remote(cls, v: object) -> object:
        return v if isinstance(v, dict | RevisionsRemoteSettings) else {}

    @field_validator("debounce_seconds", mode="before")
    @classmethod
    def _coerce_debounce(cls, v: object) -> int:
        return _clamp_int(v, default=45, lo=5, hi=3600)

    @field_validator("max_file_kb", mode="before")
    @classmethod
    def _coerce_max_file_kb(cls, v: object) -> int:
        return _clamp_int(v, default=50 * 1024, lo=16, hi=100 * 1024)


_PUSH_PRIORITIES = ("urgent", "normal", "low")
_PUSH_ALERT_LEVELS = ("warn", "error")


class PushSettings(BaseModel):
    """The ``push:`` block of ``~/.lionclaw/config.yaml``.

    Push notifications to the Olano mobile app (:mod:`olano.push`). They need
    the Olano Cloud relay, so they only ever leave a managed deployment.
    ``enabled: false`` stops the dispatcher at the next server boot; registered
    phones are kept. ``previews: true`` puts the start of a reply, or a Cortex
    finding's title, into the notification itself, which then passes through
    Apple's or Google's push service; off, a notification says only who needs
    you and why. ``cortex_min_priority`` is the lowest Pulse priority that
    pushes (``urgent`` by default, so a phone is not buzzed for every idea);
    Cortex items waiting for approval always push. ``alert_min_level`` is the
    lowest bell-notification level that pushes to admins.
    """

    enabled: bool = True
    previews: bool = False
    cortex_min_priority: str = "urgent"
    alert_min_level: str = "warn"

    @field_validator("cortex_min_priority", mode="before")
    @classmethod
    def _coerce_priority(cls, v: object) -> str:
        text = str(v or "").strip().lower()
        return text if text in _PUSH_PRIORITIES else "urgent"

    @field_validator("alert_min_level", mode="before")
    @classmethod
    def _coerce_level(cls, v: object) -> str:
        text = str(v or "").strip().lower()
        return text if text in _PUSH_ALERT_LEVELS else "warn"


class OwnerLoginSettings(BaseModel):
    """The ``magic_links.owner_login:`` sub-block.

    The chat → dashboard magic link (:mod:`olano.magic_links.owner_login`): an
    ``owner-login`` link whose completion issues a **real dashboard session**
    and sends the owner to a deep link, so they can edit the system prompt,
    vault, connections and more from their phone with no separate login.

    Because it grants a session — the most powerful thing a magic link can do —
    it is its own capability, off by default:

    - ``enabled``: master switch for the ``owner-login`` flow. Off ⇒ minting
      one refuses, and the flow is not offered.
    - ``auto_bind`` — when a deployment has exactly one admin and the chat
      sender is on the allowlist, bind to that admin **without** a
      password step. Convenience for solo installs; an assumption ("the
      allowlisted chat user is the admin"), so off by default. When off, the
      first link asks for the dashboard password once to prove the account.
    - ``session_hours`` — lifetime of the session the link issues. Shorter than
      the 14-day web default (a phone is likelier lost). Clamped 1h–30d.
    """

    enabled: bool = False
    auto_bind: bool = False
    session_hours: int = 24

    @field_validator("session_hours", mode="before")
    @classmethod
    def _coerce_session_hours(cls, v: object) -> int:
        return _clamp_int(v, default=24, lo=1, hi=24 * 30)


# The ``magic_links:`` block was called ``handshake:`` before the feature was
# renamed. Both names are read (new wins); every write emits the new one and
# drops the old, so a config can never carry two sources of truth.
MAGIC_LINKS_CONFIG_KEY = "magic_links"
LEGACY_MAGIC_LINKS_CONFIG_KEY = "handshake"


class MagicLinkSettings(BaseModel):
    """The ``magic_links:`` block of ``~/.olano/config.yaml``.

    Governs **magic links** (:mod:`olano.magic_links`) — the short-lived pages
    an agent sends into a chat so a user with no browser on the host can
    complete a device flow, an OAuth sign-in, edit a file, or hand over an API
    key without pasting it into the conversation.

    The legacy key for this block was ``handshake:``. It is still read (an
    explicit ``magic_links:`` wins when both are present) so a deployment that
    was never migrated keeps its settings; the first dashboard save rewrites it
    under the new name.

    - ``enabled``: master switch. Off ⇒ minting refuses everywhere (agent
      tool, ``/olano connect``, dashboard, CLI) and the public routes 404.
    - ``ttl_minutes`` — how long a fresh link stays openable.
    - ``pin`` — ``required`` (default) mints a 6-digit PIN alongside every link
      and the page reveals nothing until it is entered; ``off`` skips it. The
      PIN is what makes a leaked URL (chat backups, forwarded messages,
      notification previews, proxies) insufficient on its own.
    - ``max_pin_attempts`` — wrong PINs before the link burns itself.
    - ``allow_groups`` — whether a link may be minted from a group chat, where
      link and PIN are visible to every member. Default off.
    - ``owner_login`` — the chat → dashboard magic link (see
      :class:`OwnerLoginSettings`). A separate, more sensitive capability
      (it grants a real dashboard session), off by default.
    """

    enabled: bool = True
    ttl_minutes: int = 15
    pin: str = "required"  # required | off
    max_pin_attempts: int = 5
    allow_groups: bool = False
    owner_login: OwnerLoginSettings = Field(default_factory=lambda: OwnerLoginSettings())

    @field_validator("ttl_minutes", mode="before")
    @classmethod
    def _coerce_ttl(cls, v: object) -> int:
        return _clamp_int(v, default=15, lo=2, hi=24 * 60)

    @field_validator("pin", mode="before")
    @classmethod
    def _coerce_pin(cls, v: object) -> str:
        """Anything unrecognised means the *safer* setting, not the laxer one."""
        return "off" if isinstance(v, str) and v.strip().lower() == "off" else "required"

    @field_validator("max_pin_attempts", mode="before")
    @classmethod
    def _coerce_attempts(cls, v: object) -> int:
        return _clamp_int(v, default=5, lo=1, hi=10)


class PromptRefreshSettings(BaseModel):
    """The ``prompt_refresh:`` block of ``~/.olano/config.yaml``.

    Governs the runtime's prompt refresh sweep: a background loop that
    recomputes each running agent's *stable* prompt sections (instructions,
    customer-service extension, agent roster, identity/policy files, tool
    inventory — never memory/daily logs/PULSE) and hot-swaps a freshly built
    graph when any of them changed, so prompt edits apply without a restart.
    Existing threads additionally receive a one-shot in-conversation update
    notice describing the changed sections (see
    :mod:`olano.core.prompt_state`). The loop re-reads this block each
    tick, so changes apply within one interval without a restart.

    - ``enabled``: master switch for the sweep (the per-thread notice still
      fires after explicit rebuilds such as ``reload_agent_mcp`` or a
      restart, since thread seen-markers persist on disk).
    - ``check_interval_minutes`` — periodic tick; config-save and agent
      boot/stop events nudge the sweep immediately regardless.
    - ``notify_threads`` — when False, threads silently track the latest
      prompt version without injecting update notices.
    """

    enabled: bool = True
    check_interval_minutes: int = 2
    notify_threads: bool = True

    @field_validator("check_interval_minutes", mode="before")
    @classmethod
    def _coerce_check_interval(cls, v: object) -> int:
        return _clamp_int(v, default=2, lo=1, hi=24 * 60)


class McpHealthSettings(BaseModel):
    """The ``mcp_health:`` block of ``~/.olano/config.yaml``.

    Governs the runtime's MCP health loop (:mod:`olano.core.mcp_health`):
    the background sweep that keeps OAuth grants alive and re-connects MCP
    servers that dropped out, so a connection does not have to be re-authorized
    by hand every time its access token lapses or its server blips. The loop
    re-reads this block each tick, so changes apply within one interval without
    a restart.

    - ``enabled``: master switch for the whole sweep. Off ⇒ tokens are only
      renewed by the next request that needs one, and a server that failed to
      connect stays down until someone reloads MCP by hand (the pre-loop
      behaviour). ``LIONCLAW_NO_MCP_HEALTH=1`` disables it for one boot.
    - ``check_interval_minutes`` — how often the sweep runs.
    - ``refresh_tokens`` — renew a stored OAuth access token once it is within
      ``refresh_window_minutes`` of expiry, by talking to the provider's token
      endpoint directly (so it works even while the MCP server is unreachable).
      Off ⇒ health checks still run, tokens are left to expire.
    - ``refresh_window_minutes`` — how much life left counts as "renew now".
      Keep it comfortably above ``check_interval_minutes`` so a token is never
      first *noticed* after it has already lapsed.
    - ``auto_reconnect`` — re-connect servers whose last build left them
      timed-out or errored, with exponential backoff (5 min, doubling, capped
      at 6 h) per server. A server that needs interactive authorization is
      never retried this way — no amount of reconnecting invents consent.
    """

    enabled: bool = True
    check_interval_minutes: int = 10
    refresh_tokens: bool = True
    refresh_window_minutes: int = 20
    auto_reconnect: bool = True

    @field_validator("check_interval_minutes", mode="before")
    @classmethod
    def _coerce_check_interval(cls, v: object) -> int:
        return _clamp_int(v, default=10, lo=1, hi=24 * 60)

    @field_validator("refresh_window_minutes", mode="before")
    @classmethod
    def _coerce_refresh_window(cls, v: object) -> int:
        return _clamp_int(v, default=20, lo=1, hi=12 * 60)


class PagesSettings(BaseModel):
    """The ``pages:`` block of ``~/.olano/config.yaml``.

    Governs deployed pages: the ones an agent publishes with ``pages_deploy``,
    which run a build command, a long-running server, or both (plain pages
    published with ``pages_start`` only serve files and ignore this block).
    Re-read by the page supervisor on every tick, so a change applies within a
    few seconds without a restart.

    - ``services_enabled``: master switch for running anything. Off ⇒ no
      build runs, every page server is stopped, and ``pages_deploy`` refuses;
      plain pages keep serving.
    - ``max_services_per_agent``: how many deployed pages one agent may have
      serving at once. A deploy over the limit is refused with the reason.
    - ``health_interval_seconds``: how often a running server's health path
      is probed. Three failures in a row restart it.
    - ``request_timeout_seconds``: the longest a viewer's request waits for
      the server's next byte (connect, first byte, and between chunks of a
      streamed response).
    - ``max_request_body_mb``: the largest request body forwarded to a
      server; bigger uploads get ``413``.
    - ``max_concurrent_requests``: requests in flight per page server; past
      it viewers get ``503`` with ``Retry-After`` instead of queueing without
      bound.
    """

    services_enabled: bool = True
    max_services_per_agent: int = 5
    health_interval_seconds: int = 30
    request_timeout_seconds: int = 120
    max_request_body_mb: int = 25
    max_concurrent_requests: int = 64

    @field_validator("max_services_per_agent", mode="before")
    @classmethod
    def _coerce_max_services(cls, v: object) -> int:
        return _clamp_int(v, default=5, lo=1, hi=50)

    @field_validator("health_interval_seconds", mode="before")
    @classmethod
    def _coerce_health_interval(cls, v: object) -> int:
        return _clamp_int(v, default=30, lo=5, hi=3600)

    @field_validator("request_timeout_seconds", mode="before")
    @classmethod
    def _coerce_request_timeout(cls, v: object) -> int:
        return _clamp_int(v, default=120, lo=5, hi=3600)

    @field_validator("max_request_body_mb", mode="before")
    @classmethod
    def _coerce_body(cls, v: object) -> int:
        return _clamp_int(v, default=25, lo=1, hi=1024)

    @field_validator("max_concurrent_requests", mode="before")
    @classmethod
    def _coerce_concurrency(cls, v: object) -> int:
        return _clamp_int(v, default=64, lo=1, hi=1024)


BROWSER_MODES: tuple[str, ...] = ("off", "headless", "live")
BROWSER_PROVIDERS: tuple[str, ...] = ("local", "remote")
#: ``browser.human_check``: what an agent does when a site asks a person to
#: prove they are human (``olano.browser.human_check``).
HUMAN_CHECK_MODES: tuple[str, ...] = ("wait", "hold", "skip")
HUMAN_CHECK_WAIT_DEFAULT = 5
HUMAN_CHECK_WAIT_MAX = 120


def is_iana_timezone(name: str) -> bool:
    """Whether *name* is a time zone this machine (and so Chromium) knows."""
    if not name or name.startswith("/") or ".." in name:
        return False
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return True
    except Exception:
        return False


class BrowserSettings(BaseModel):
    """The ``browser:`` block of ``~/olano/config.yaml``.

    Decides whether agents get the ``browser_*`` tools at all, and where the
    browser they drive runs. Off by default: a browser is the heaviest thing an
    agent can start (measured ~250-350 MB per active headless session, ~500-700
    MB for a live one) and the one most exposed to prompt injection, so a box
    has to opt in. Resolution, the hardware gate and the deployment entitlement
    live in :mod:`olano.browser.policy`; never read ``mode`` directly to decide
    whether tools bind, ask ``browser_availability()``.

    - ``mode``: ``off`` (default): no agent has a browser, whatever its tool
      list says. ``headless``: an invisible Chromium inside the runtime, one
      isolated session per agent. ``live``: a full Chromium per agent in its
      own container (the ``olano-browser`` image, pulled when this is turned
      on), shown in a window in the agent's chat where a person can watch and
      take control.
    - ``max_sessions``: how many agents may hold an open browser at once.
    - ``idle_timeout``: minutes without a browser call before a session is
      closed (its sign-ins are saved first) and its memory returned.
    - ``provider``: ``local`` (default) runs the browser on this machine;
      ``remote`` connects to a hosted browser over the DevTools protocol, at
      the address stored in the vault as ``BROWSER_REMOTE_CDP_URL``, and costs
      this machine no browser memory.
    - ``timezone``: the IANA time zone every agent browser reports to sites
      (``Europe/Madrid``); empty (default) keeps the browser's own clock, which
      is this machine's. Set it to where ``BROWSER_PROXY_URL`` exits, so the
      clock agrees with the address sites see. An unknown name reads as empty.
    - ``human_check``: what an agent does when a site shows a check only a
      person can pass (Cloudflare's "Verify you are human" box), once the
      check has had its chance to let the browser through on its own.
      ``wait`` (default): ask for a person in the chat and on their phone and
      wait ``human_check_wait`` minutes. ``hold``: ask and wait with no time
      limit. ``skip``: do not ask; the agent is told at once and moves on.
      Live browser only: a headless browser has no screen to pass it on, so
      there it always behaves as ``skip``. Each agent can override it, and an
      agent can choose another mode for one task.
    - ``human_check_wait``: minutes ``wait`` waits, 1 to 120 (default 5).
    """

    mode: str = "off"
    max_sessions: int = 1
    idle_timeout: int = 10
    provider: str = "local"
    timezone: str = ""
    human_check: str = "wait"
    human_check_wait: int = HUMAN_CHECK_WAIT_DEFAULT

    @field_validator("mode", mode="before")
    @classmethod
    def _coerce_mode(cls, v: object) -> str:
        text = str(v or "").strip().lower()
        return text if text in BROWSER_MODES else "off"

    @field_validator("provider", mode="before")
    @classmethod
    def _coerce_provider(cls, v: object) -> str:
        text = str(v or "").strip().lower()
        return text if text in BROWSER_PROVIDERS else "local"

    @field_validator("max_sessions", mode="before")
    @classmethod
    def _coerce_max_sessions(cls, v: object) -> int:
        return _clamp_int(v, default=1, lo=1, hi=8)

    @field_validator("timezone", mode="before")
    @classmethod
    def _coerce_timezone(cls, v: object) -> str:
        text = str(v or "").strip()
        return text if is_iana_timezone(text) else ""

    @field_validator("idle_timeout", mode="before")
    @classmethod
    def _coerce_idle_timeout(cls, v: object) -> int:
        return _clamp_int(v, default=10, lo=1, hi=240)

    @field_validator("human_check", mode="before")
    @classmethod
    def _coerce_human_check(cls, v: object) -> str:
        text = str(v or "").strip().lower()
        return text if text in HUMAN_CHECK_MODES else "wait"

    @field_validator("human_check_wait", mode="before")
    @classmethod
    def _coerce_human_check_wait(cls, v: object) -> int:
        return _clamp_int(v, default=HUMAN_CHECK_WAIT_DEFAULT, lo=1, hi=HUMAN_CHECK_WAIT_MAX)


def _clamp_float(v: object, *, default: float, lo: float, hi: float) -> float:
    try:
        value = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    return max(lo, min(hi, value))


#: ``observability.langfuse.capture`` values. ``metadata`` (the default) sends
#: structure, timings, token counts and tool names but no message, tool or
#: error text; ``full`` sends content too, with secrets masked in both.
LANGFUSE_CAPTURE_MODES: tuple[str, ...] = ("metadata", "full")
LANGFUSE_CAPTURE_DEFAULT = "metadata"

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})
# Langfuse's own rule for an environment name: lowercase, digits, ``-``/``_``,
# at most 40 characters, and never starting with "langfuse".
_LANGFUSE_ENV_RE = re.compile(r"^(?!langfuse)[a-z0-9_-]{1,40}$")
_LANGFUSE_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def normalize_langfuse_host(value: object) -> str:
    """A Langfuse base URL, or ``""`` when the value is not one.

    ``https://`` for any host; plain ``http://`` only for a local instance
    (this machine or the docker host), because credentials ride every export.
    Trailing slashes and paths are dropped: the SDK appends its own paths.
    """
    from urllib.parse import urlsplit

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if not host or parts.username or parts.password:
        return ""
    if parts.scheme == "https" or (parts.scheme == "http" and host in _LOCAL_HOSTS):
        port = f":{parts.port}" if parts.port else ""
        shown = f"[{host}]" if ":" in host else host
        return f"{parts.scheme}://{shown}{port}"
    return ""


def coerce_langfuse_capture(
    value: object, default: str | None = LANGFUSE_CAPTURE_DEFAULT
) -> str | None:
    """``full`` only when it says exactly that; anything else is the default.

    The safe side of a typo is the mode that sends no content.
    """
    text = str(value or "").strip().lower()
    return text if text in LANGFUSE_CAPTURE_MODES else default


class LangfuseSettings(BaseModel):
    """The ``observability.langfuse`` block of ``config.yaml``.

    Opt-in export of every agent turn to an operator's Langfuse project as one
    trace (model calls and tool calls nested under it, grouped by thread as a
    session). Off by default; credentials alone never turn it on. Resolution
    against the per-agent ``agent.json5 → observability.langfuse`` override and
    the credentials lives in :mod:`olano.observability.policy`; never read
    ``enabled`` directly to decide whether a turn is traced.

    - ``enabled``: master switch for this deployment.
    - ``host``: the Langfuse base URL (``https://cloud.langfuse.com``,
      ``https://us.cloud.langfuse.com``, ``https://jp.cloud.langfuse.com`` or a
      self-hosted URL). Empty uses the host stored with the credentials, then
      Langfuse Cloud (EU).
    - ``capture``: ``metadata`` (default) or ``full``; see
      :data:`LANGFUSE_CAPTURE_MODES`.
    - ``sample_rate``: share of turns traced, 0.0 to 1.0, decided per turn
      from the trace id so a turn is either fully traced or not at all.
    - ``environment``: Langfuse environment tag (``production``, ``staging``);
      empty sends none.
    - ``trace_background``: whether Cortex and other background runs are
      traced too.
    - ``project_id``: written by the dashboard's Test connection, read to build
      links to traces. Not a secret.
    """

    enabled: bool = False
    host: str = ""
    capture: str = LANGFUSE_CAPTURE_DEFAULT
    sample_rate: float = 1.0
    environment: str = ""
    trace_background: bool = True
    project_id: str = ""

    @field_validator("host", mode="before")
    @classmethod
    def _coerce_host(cls, v: object) -> str:
        return normalize_langfuse_host(v)

    @field_validator("capture", mode="before")
    @classmethod
    def _coerce_capture(cls, v: object) -> str:
        return coerce_langfuse_capture(v) or LANGFUSE_CAPTURE_DEFAULT

    @field_validator("sample_rate", mode="before")
    @classmethod
    def _coerce_sample_rate(cls, v: object) -> float:
        return _clamp_float(v, default=1.0, lo=0.0, hi=1.0)

    @field_validator("environment", mode="before")
    @classmethod
    def _coerce_environment(cls, v: object) -> str:
        text = str(v or "").strip().lower()
        return text if _LANGFUSE_ENV_RE.match(text) else ""

    @field_validator("project_id", mode="before")
    @classmethod
    def _coerce_project_id(cls, v: object) -> str:
        text = str(v or "").strip()
        return text if _LANGFUSE_PROJECT_ID_RE.match(text) else ""


class ObservabilitySettings(BaseModel):
    """The ``observability:`` block of ``config.yaml`` (one backend today)."""

    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)

    @field_validator("langfuse", mode="before")
    @classmethod
    def _coerce_langfuse(cls, v: object) -> object:
        # A malformed hand-edit reads as the default (off), never as an error.
        return v if isinstance(v, dict | LangfuseSettings) else {}


# Context-window policy defaults. Kept as module constants so the per-agent
# resolver, the dashboard and the tests all read one definition.
CONTEXT_KEEP_FRACTION_DEFAULT = 0.35
CONTEXT_KEEP_TOKENS_DEFAULT = 40_000
CONTEXT_TRIGGER_FRACTION_DEFAULT = 0.85
CONTEXT_TRIGGER_TOKENS_DEFAULT = 170_000
MCP_RESULT_TOKEN_LIMIT_DEFAULT = 5_000


class ContextWindowSettings(BaseModel):
    """The ``context_window:`` block of ``~/.olano/config.yaml``.

    Home-wide policy for how an agent's conversation is bounded inside the
    model's context window. Two mechanisms read it, both wired in
    ``core/builder.py``:

    **Compaction** (deepagents' ``SummarizationMiddleware`` and the
    ``compact_conversation`` tool behind ``/compact``). When the conversation
    reaches the *trigger*, everything older than the *keep* budget is
    summarised into one paragraph and offloaded to
    ``<workspace>/.olano/conversation_history/``; the summary plus the
    kept tail is what the model sees from then on.

    - ``keep_fraction`` — share of the model's context window retained
      verbatim after a compaction, when the window is known (the catalog
      lists it, or the provider client reports it). ``0.35`` on a 272k
      model keeps the last ~95k tokens of conversation. deepagents' own
      default was 10%, and 6 *messages* for a model with no known window,
      which made an agent forget the start of a short session.
    - ``keep_tokens`` — the same budget as an absolute token count, used only
      when the window is unknown.
    - ``trigger_fraction`` / ``trigger_tokens`` — when compaction fires
      automatically (same known/unknown split). The manual
      ``compact_conversation`` tool becomes eligible at half the trigger.

    **MCP result offload.** ``mcp_result_token_limit`` — an MCP tool result
    larger than this (estimated at 4 characters per token) is written to
    ``<workspace>/.olano/large_tool_results/<tool_call_id>`` and replaced
    in the conversation by a head/tail preview plus the file path, so one
    search page cannot spend a quarter of the window. The agent reads the
    rest with ``read_file`` (offset/limit) or ``grep``. ``0`` disables the
    cap; results from olano's own toolkits are governed by deepagents'
    general 20k-token offload instead.

    Every field can be overridden per agent in ``agent.json5 →
    context_window`` (``None`` there inherits this block). Resolution lives in
    :func:`effective_context_window`.
    """

    keep_fraction: float = CONTEXT_KEEP_FRACTION_DEFAULT
    keep_tokens: int = CONTEXT_KEEP_TOKENS_DEFAULT
    trigger_fraction: float = CONTEXT_TRIGGER_FRACTION_DEFAULT
    trigger_tokens: int = CONTEXT_TRIGGER_TOKENS_DEFAULT
    mcp_result_token_limit: int = MCP_RESULT_TOKEN_LIMIT_DEFAULT

    @field_validator("keep_fraction", mode="before")
    @classmethod
    def _coerce_keep_fraction(cls, v: object) -> float:
        return _clamp_float(v, default=CONTEXT_KEEP_FRACTION_DEFAULT, lo=0.05, hi=0.8)

    @field_validator("keep_tokens", mode="before")
    @classmethod
    def _coerce_keep_tokens(cls, v: object) -> int:
        return _clamp_int(v, default=CONTEXT_KEEP_TOKENS_DEFAULT, lo=2_000, hi=2_000_000)

    @field_validator("trigger_fraction", mode="before")
    @classmethod
    def _coerce_trigger_fraction(cls, v: object) -> float:
        return _clamp_float(v, default=CONTEXT_TRIGGER_FRACTION_DEFAULT, lo=0.3, hi=0.98)

    @field_validator("trigger_tokens", mode="before")
    @classmethod
    def _coerce_trigger_tokens(cls, v: object) -> int:
        return _clamp_int(v, default=CONTEXT_TRIGGER_TOKENS_DEFAULT, lo=8_000, hi=4_000_000)

    @field_validator("mcp_result_token_limit", mode="before")
    @classmethod
    def _coerce_mcp_limit(cls, v: object) -> int:
        return _clamp_int(v, default=MCP_RESULT_TOKEN_LIMIT_DEFAULT, lo=0, hi=200_000)

    @model_validator(mode="after")
    def _keep_below_trigger(self) -> ContextWindowSettings:
        # A keep budget at or above the trigger would compact nothing (the
        # tail already fits) and then compact again on the next turn. Pin the
        # keep strictly below the trigger on both axes.
        if self.keep_fraction >= self.trigger_fraction:
            self.keep_fraction = round(self.trigger_fraction * 0.5, 3)
        if self.keep_tokens >= self.trigger_tokens:
            self.keep_tokens = max(2_000, self.trigger_tokens // 2)
        return self


class ContextWindowPolicy(BaseModel):
    """The resolved policy for one agent (home block + per-agent override).

    ``trigger`` / ``keep`` are in deepagents' ``ContextSize`` shape:
    ``("fraction", f)`` when the model's window is known, ``("tokens", n)``
    otherwise. ``model_window`` is the window the fraction applies to, or
    ``None``.
    """

    keep_fraction: float
    keep_tokens: int
    trigger_fraction: float
    trigger_tokens: int
    mcp_result_token_limit: int

    def context_sizes(
        self, model_window: int | None
    ) -> tuple[tuple[str, float], tuple[str, float]]:
        """``(trigger, keep)`` for a model whose max input is ``model_window``."""
        if model_window and model_window > 0:
            return ("fraction", self.trigger_fraction), ("fraction", self.keep_fraction)
        return ("tokens", float(self.trigger_tokens)), ("tokens", float(self.keep_tokens))


class RemoteAgentSettings(BaseModel):
    """The ``remote_agents:`` block of ``~/.olano/config.yaml``.

    Home-wide policy for the **outbound** A2A client — the ``a2a_*`` tools an
    agent gets when its ``agent.json5`` lists ``remote_agents``. The per-agent
    list decides *which* external agents are reachable; this block decides the
    fleet-wide rules every such call obeys.

    - ``enabled``: master switch. Off ⇒ no agent can reach any remote agent,
      whatever its own config says (the tools answer with an explanatory
      error rather than vanishing, so a misfire is diagnosable from chat).
    - ``allow_ad_hoc_discovery`` — whether ``a2a_discover_agent`` may fetch the
      Agent Card of a URL that is **not** in the calling agent's
      ``remote_agents`` list. Off by default: discovery of arbitrary hosts is
      an outbound-request primitive the model could be talked into pointing
      anywhere. Sending a message is never ad hoc — only listed agents are
      addressable, in every mode.
    - ``max_reply_chars`` — the remote reply is truncated to this many
      characters before it reaches the model (protects the context window
      from a chatty or hostile peer).
    - ``max_message_chars`` — an outbound message longer than this is refused.
    """

    enabled: bool = True
    allow_ad_hoc_discovery: bool = False
    max_reply_chars: int = 20_000
    max_message_chars: int = 20_000

    @field_validator("max_reply_chars", mode="before")
    @classmethod
    def _coerce_max_reply(cls, v: object) -> int:
        return _clamp_int(v, default=20_000, lo=500, hi=500_000)

    @field_validator("max_message_chars", mode="before")
    @classmethod
    def _coerce_max_message(cls, v: object) -> int:
        return _clamp_int(v, default=20_000, lo=500, hi=500_000)


class ExternalAccessJwtSettings(BaseModel):
    """The ``external_access.jwt:`` sub-block — accept OAuth 2.0 access tokens
    issued by an **external** authorization server (Auth0, Keycloak, Okta,
    Entra, …) on the MCP and A2A endpoints, in addition to tokens Olano mints
    or issues itself.

    When ``issuer`` and ``jwks_url`` are both set, a presented JWT is verified
    against the issuer's published signing keys, and its ``aud`` claim must
    name this deployment's endpoint (RFC 8707 audience binding — a token
    minted for some other API is refused even when the same issuer signed it).
    The token's ``scope`` claim grants agents: ``agent:<name>`` per agent, or
    ``agents:*`` for all exposed agents.
    """

    issuer: str = ""
    jwks_url: str = ""
    audience: str = ""
    required_scopes: list[str] = Field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.issuer.strip() and self.jwks_url.strip())


class ExternalAccessOAuthSettings(BaseModel):
    """The ``external_access.oauth:`` sub-block — Olano's **own** OAuth 2.1
    authorization server at ``<public URL>/api/oauth``.

    This is what lets an OAuth-only client (a Claude.ai custom connector, an
    A2A client that follows the card's ``oauth2`` scheme) connect with no
    external identity provider: the client registers itself (RFC 7591), sends
    the person to the dashboard, an **admin** approves which agents it may
    reach on the consent page, and the client receives short-lived access
    tokens plus a rotating refresh token. Every token it issues lands in the
    same token list as operator-minted ones and can be revoked there.

    - ``enabled``: serve the authorization server (default on; it only
      matters once an inbound surface is on).
    - ``dynamic_registration`` — accept RFC 7591 client registrations without
      an operator pre-registering the client (default on; registration grants
      nothing by itself — the consent page does).
    - ``access_token_minutes`` — lifetime of issued access tokens.
    - ``refresh_token_days`` — lifetime of refresh tokens (rotated on use).
    """

    enabled: bool = True
    dynamic_registration: bool = True
    access_token_minutes: int = 60
    refresh_token_days: int = 30

    @field_validator("access_token_minutes", mode="before")
    @classmethod
    def _coerce_access(cls, v: object) -> int:
        return _clamp_int(v, default=60, lo=5, hi=24 * 60)

    @field_validator("refresh_token_days", mode="before")
    @classmethod
    def _coerce_refresh(cls, v: object) -> int:
        return _clamp_int(v, default=30, lo=1, hi=365)


class ExternalAccessSurfaceSettings(BaseModel):
    """One inbound surface's switch (``external_access.mcp`` / ``.a2a``)."""

    enabled: bool = False


class ExternalAccessSettings(BaseModel):
    """The ``external_access:`` block of ``~/.olano/config.yaml``.

    Home-wide switches and policy for letting systems **outside** the
    deployment reach agents — the opposite direction from the agents' own
    ``mcp_servers`` / ``remote_agents``. Two surfaces, both **off** by default:

    - ``mcp`` — the MCP endpoint at ``<public URL>/api/mcp`` (agents flagged
      ``external_access.mcp`` become ``ask_<agent>`` tools).
    - ``a2a`` — per-agent A2A Agent Cards + JSON-RPC endpoints at
      ``<public URL>/api/a2a/agents/<name>`` (agents flagged
      ``external_access.a2a``).

    Shared policy:

    - ``require_https`` — refuse to serve when the deployment's public URL is
      not ``https://`` (default on). Tokens travel in the ``Authorization``
      header, so a plaintext public endpoint would leak them. A loopback /
      private-network URL (development) is exempt.
    - ``token_ttl_days`` — default lifetime of a bearer token minted without
      an explicit expiry (default 90; ``0`` = no expiry).
    - ``a2a_task_retention_days`` — how long the record of each inbound A2A
      call (task id, status, message, reply) stays retrievable through the
      protocol's task lookup before the store deletes it (default 7, 1–3650).
      Only the A2A envelope: the conversation lives in the agent's thread.
    - ``allowed_hosts`` / ``allowed_origins`` — extra ``Host`` / ``Origin``
      header values accepted by the DNS-rebinding protection, on top of the
      public URL's own host and loopback.
    - ``oauth`` — Olano's own authorization server (see
      :class:`ExternalAccessOAuthSettings`).
    - ``jwt`` — optional external authorization server (see
      :class:`ExternalAccessJwtSettings`).
    """

    mcp: ExternalAccessSurfaceSettings = Field(default_factory=ExternalAccessSurfaceSettings)
    a2a: ExternalAccessSurfaceSettings = Field(default_factory=ExternalAccessSurfaceSettings)
    require_https: bool = True
    token_ttl_days: int = 90
    a2a_task_retention_days: int = 7
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_origins: list[str] = Field(default_factory=list)
    oauth: ExternalAccessOAuthSettings = Field(default_factory=ExternalAccessOAuthSettings)
    jwt: ExternalAccessJwtSettings = Field(default_factory=ExternalAccessJwtSettings)

    @field_validator("token_ttl_days", mode="before")
    @classmethod
    def _coerce_ttl(cls, v: object) -> int:
        return _clamp_int(v, default=90, lo=0, hi=3650)

    @field_validator("a2a_task_retention_days", mode="before")
    @classmethod
    def _coerce_task_retention(cls, v: object) -> int:
        return _clamp_int(v, default=7, lo=1, hi=3650)

    @field_validator("mcp", "a2a", mode="before")
    @classmethod
    def _coerce_surface(cls, v: object) -> object:
        # Accept the shorthand `mcp: true` as well as `mcp: {enabled: true}`.
        if isinstance(v, bool):
            return {"enabled": v}
        return v


class SandboxSettings(BaseModel):
    """The ``sandbox:`` block of ``~/.olano/config.yaml``.

    Deployment-wide ceilings for the Docker sandbox container each agent's
    shell runs in. Both fields are ``None`` by default, which means *auto*:
    the runtime sizes each sandbox at a fraction of the detected host —
    25% of RAM and 50% of the cores, never below the historical 512m/0.5 —
    so a bigger host gives agents more room with no configuration.

    - ``memory`` — hard RAM ceiling, as a docker size string (``"4g"``,
      ``"2048m"``, or a byte count). Minimum ``6m`` (docker's own floor).
      A command that exceeds it is OOM-killed inside the sandbox; the agent
      and the runtime survive.
    - ``cpus`` — CPU ceiling in cores (``0.5`` = half a core), range 0.1–64.
      A ceiling, not a reservation: idle sandboxes cost nothing, and several
      busy agents can together exceed any one agent's share.

    Resolution per field (first hit wins): the agent's own ``sandbox:`` block
    in ``agent.json5`` → ``LIONCLAW_SANDBOX_MEMORY`` / ``LIONCLAW_SANDBOX_CPUS``
    → this block → auto. Values here are read when an agent builds, so a change
    reaches an agent on its next start or reload — and because docker cannot
    retune a live container, that start recreates its sandbox.

    Unparseable values coerce to ``None`` (auto) rather than raising: this
    loader must never take a box offline over a hand-edit. The dashboard's PUT
    validates loudly instead, so a bad value typed there is rejected on the
    spot.
    """

    memory: str | None = None
    cpus: float | None = None

    @field_validator("memory", mode="before")
    @classmethod
    def _coerce_memory(cls, v: object) -> str | None:
        if v is None or v == "":
            return None
        from olano.config.backend_config import normalize_sandbox_memory

        try:
            return normalize_sandbox_memory(v)
        except ValueError:
            logger.warning("sandbox.memory %r is not a valid size — using the automatic limit", v)
            return None

    @field_validator("cpus", mode="before")
    @classmethod
    def _coerce_cpus(cls, v: object) -> float | None:
        if v is None or v == "":
            return None
        from olano.config.backend_config import SANDBOX_CPUS_MAX, SANDBOX_CPUS_MIN

        try:
            cpus = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            logger.warning("sandbox.cpus %r is not a number — using the automatic limit", v)
            return None
        return max(SANDBOX_CPUS_MIN, min(SANDBOX_CPUS_MAX, cpus))


SPEECH_PROVIDERS = ("disabled", "olano", "openai", "groq", "qwen", "custom")
# Accepted values for ``speech.tts.audio_format`` (blank = per-provider auto).
# Declared here rather than imported from ``olano.speech``: this validator runs
# on every settings load, including from background threads, and reaching into
# a module that pulls in the tool registry would put a heavy import behind the
# import lock on a hot path. ``olano.speech`` consumes this tuple and owns the
# format → file-extension mapping; ``tests/test_speech.py`` pins them in sync.
TTS_AUDIO_FORMATS = ("aac", "flac", "m4a", "mp3", "opus", "pcm", "wav")


class SpeechServiceSettings(BaseModel):
    """One direction of the ``speech:`` block (``stt:`` or ``tts:``).

    ``provider`` picks the backend; the other fields refine it:

    - ``model`` — override the provider's default model (e.g. pin ``whisper-1``).
    - ``url`` — endpoint for ``provider: custom`` (any OpenAI-compatible
      ``/v1/audio/transcriptions`` / ``/v1/audio/speech`` server, e.g. a
      self-hosted Speaches/faster-whisper container).
    - ``api_key_name`` — which credential to resolve (per-agent config.env →
      vault → env). Empty = the provider's default (``OPENAI_API_KEY``,
      ``GROQ_API_KEY``, or ``LIONCLAW_SPEECH_API_KEY`` for custom).
    - ``voice`` — TTS only; the provider's default voice when empty.
    - ``audio_format`` — TTS only; the container to request (``opus``, ``mp3``,
      ``wav``, …). Empty picks per provider: ``opus`` for OpenAI/Olano (the
      only container the chat platforms can render as a native **voice
      bubble**), ``wav`` for Groq PlayAI, and nothing at all for Qwen (no such
      parameter) or ``custom`` (an unknown self-hosted server must not start
      receiving a format it may reject). Self-hosters wanting voice bubbles set
      this to ``opus`` explicitly.
    """

    provider: str = "disabled"
    model: str = ""
    url: str = ""
    api_key_name: str = ""
    voice: str = ""
    audio_format: str = ""

    @field_validator("provider", mode="before")
    @classmethod
    def _coerce_provider(cls, v: object) -> str:
        """Coerce unknown/missing provider to ``disabled`` rather than erroring,
        so a bad value can never brick message ingress."""
        return v if isinstance(v, str) and v in SPEECH_PROVIDERS else "disabled"

    @field_validator("audio_format", mode="before")
    @classmethod
    def _coerce_audio_format(cls, v: object) -> str:
        """Unknown container ⇒ empty (per-provider auto), never an error.

        Same fail-open contract as ``provider``: a typo in config.yaml must
        degrade to the default, not take voice replies down.
        """
        text = v.strip().lower() if isinstance(v, str) else ""
        return text if text in TTS_AUDIO_FORMATS else ""


class SpeechSettings(BaseModel):
    """The ``speech:`` block of ``~/.olano/config.yaml``.

    Provider-independent speech services used by the messaging platforms
    (voice-note transcription, voice replies) and the ``audio_to_text`` /
    ``text_to_speech`` agent tools. **STT defaults on for managed deployments**
    (the Olano service — see :func:`load_speech_settings`), everything else
    defaults OFF: with STT disabled inbound voice notes tell the agent to ask
    the sender to resend as text, and TTS is always an explicit opt-in in the
    dashboard Config → Speech section or this block — voice replies change how
    the agent behaves outwardly and cost money on every reply, so no
    deployment turns them on by itself.
    """

    stt: SpeechServiceSettings = Field(default_factory=SpeechServiceSettings)
    tts: SpeechServiceSettings = Field(default_factory=SpeechServiceSettings)


def load_speech_settings(override: str | Path | None = None) -> SpeechSettings:
    """Load the ``speech`` block from ``config.yaml``; defaults when missing.
    Never raises — a malformed file yields all-default settings.

    **The managed default covers STT only**: on a managed deployment an
    ``stt`` block whose provider was never explicitly configured resolves to
    the Olano speech service (billed in olano credits through the model
    gateway), so inbound voice notes transcribe out of the box. TTS stays
    ``disabled`` until someone picks a provider — replies remain text-only by
    default. An explicit value — including an explicit ``disabled`` — always
    wins, and unmanaged installs keep the all-off default (there is no gateway
    to bill through).
    """
    settings = SpeechSettings()
    stt_explicit = False
    try:
        config_path = get_olano_paths(override).config_path
        if config_path.exists():
            raw = read_home_config_doc(override)
            block = raw.get("speech") or {}
            settings = SpeechSettings.model_validate(block)
            sub = block.get("stt") if isinstance(block, dict) else None
            stt_explicit = isinstance(sub, dict) and "provider" in sub
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_speech_settings: falling back to defaults: %s", e)
        settings = SpeechSettings()

    if not stt_explicit and settings.stt.provider == "disabled" and olano_credits_available():
        settings.stt.provider = "olano"
    return settings


def managed_olano_default() -> bool:
    """True when this box is a MANAGED deployment (posture enforced/quarantine).

    The posture half of "does this box run on credits": the signal every
    security and enforcement gate reads, and the one-shot migration 0011 too.
    For the *spending* defaults (speech, cognition, the default:* aliases, the
    archetype brain, media and the managed services) use
    :func:`olano_credits_available`, which also answers True for a self-hosted
    box holding a pasted Olano account key. Never raises.
    """
    try:
        from olano.deployment import resolver as dep_resolver

        return dep_resolver.deployment_status().posture != "open"
    except Exception:  # pragma: no cover - defensive
        return False


def olano_credits_available() -> bool:
    """True when this box can spend Olano credits: a managed deployment, or a
    self-hosted (open) box whose owner pasted an Olano account API key.

    One definition for every "should this default to the Olano managed
    services" question, so speech, cognition, the default:* aliases, media and
    the managed service clients can never drift apart. The security gates stay
    on :func:`managed_olano_default`: a pasted key buys credits, never a change
    of posture. Never raises.
    """
    if managed_olano_default():
        return True
    try:
        from olano.deployment.gateway_key import account_key_if_unmanaged

        return bool(account_key_if_unmanaged())
    except Exception:  # pragma: no cover - defensive
        return False


def set_speech_settings(
    stt: dict | None = None,
    tts: dict | None = None,
    override: str | Path | None = None,
) -> Path:
    """Merge updates into the ``speech:`` block of ``config.yaml``, preserving
    all other keys. Each of ``stt``/``tts`` is a partial dict of
    :class:`SpeechServiceSettings` fields; unknown fields are dropped and the
    result is validated (bad provider values coerce to ``disabled``)."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    speech = raw.setdefault("speech", {})
    for section_name, updates in (("stt", stt), ("tts", tts)):
        if updates is None:
            continue
        section = speech.setdefault(section_name, {})
        if not isinstance(section, dict):  # malformed hand-edit; replace
            section = {}
            speech[section_name] = section
        allowed = set(SpeechServiceSettings.model_fields)
        section.update({k: str(v or "") for k, v in updates.items() if k in allowed})
        # Round-trip through the model so coercion (unknown provider →
        # disabled) is applied to what we persist.
        speech[section_name] = SpeechServiceSettings.model_validate(section).model_dump()
    return _write_raw(paths, raw)


# ── Media (vision + image) + web-search source selection ──────────────────────
# The customer's per-feature choice of WHICH source powers vision, image
# generation, and web search: the box-side counterpart to the cloud-side
# per-service entitlement flags. Stored in config.yaml (dashboard Config →
# "Media & search"), read by the resolvers as the primary pin with the legacy
# env vars (LIONCLAW_VISION_MODEL / LIONCLAW_IMAGE_MODEL) as the fallback rung.

#: Allowed values for ``web_search.provider`` ("" = auto).
WEB_SEARCH_SOURCES: tuple[str, ...] = ("", "olano", "tavily", "disabled")


class MediaSettings(BaseModel):
    """The ``media:`` block — vision + image SOURCE pins.

    Each value is one of: ``""`` (auto — the runtime's own chain: managed
    service on a managed box, else BYOK by key), ``"olano"`` (force the managed
    service, billed in credits), or a ``"provider:model"`` BYOK pin (e.g.
    ``"openai:gpt-image-2.5-flare"``, ``"anthropic:claude-haiku-4-5-20251001"``). A pin
    is STRICT — used as-is with no fallback — matching the
    ``LIONCLAW_VISION_MODEL`` / ``LIONCLAW_IMAGE_MODEL`` contract it supersedes.
    The dashboard writes this block; those env vars remain the fallback when a
    field is empty, so existing installs keep working.
    """

    vision: str = ""
    image: str = ""

    @field_validator("vision", "image", mode="before")
    @classmethod
    def _clean(cls, v: object) -> str:
        return v.strip() if isinstance(v, str) else ""


class WebSearchSettings(BaseModel):
    """The ``web_search:`` block — which web-search source the tools use.

    ``provider``: ``""`` (auto — Tavily when ``TAVILY_API_KEY`` is set, else the
    managed service), ``"olano"`` (force the managed service even with a Tavily
    key), ``"tavily"`` (force Tavily), or ``"disabled"`` (no web search). Tavily
    always needs ``TAVILY_API_KEY`` regardless of this choice. Fail-open: an
    unknown value coerces to ``""`` (auto), never an error.
    """

    provider: str = ""

    @field_validator("provider", mode="before")
    @classmethod
    def _coerce_provider(cls, v: object) -> str:
        text = v.strip().lower() if isinstance(v, str) else ""
        return text if text in WEB_SEARCH_SOURCES else ""


def load_media_settings(override: str | Path | None = None) -> MediaSettings:
    """Load the ``media:`` block; all-default (``""`` = auto) when missing.
    Never raises — a malformed file yields defaults."""
    try:
        raw = _load_raw(get_olano_paths(override))
        return MediaSettings.model_validate(raw.get("media") or {})
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_media_settings: falling back to defaults: %s", e)
        return MediaSettings()


def set_media_settings(
    vision: str | None = None,
    image: str | None = None,
    override: str | Path | None = None,
) -> Path:
    """Merge vision/image source pins into the ``media:`` block, preserving all
    other keys. A value of ``None`` leaves that field untouched."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    media = raw.setdefault("media", {})
    if not isinstance(media, dict):  # malformed hand-edit; replace
        media = {}
        raw["media"] = media
    if vision is not None:
        media["vision"] = str(vision or "").strip()
    if image is not None:
        media["image"] = str(image or "").strip()
    raw["media"] = MediaSettings.model_validate(media).model_dump()
    return _write_raw(paths, raw)


def load_web_search_settings(override: str | Path | None = None) -> WebSearchSettings:
    """Load the ``web_search:`` block; default (``""`` = auto) when missing.
    Never raises."""
    try:
        raw = _load_raw(get_olano_paths(override))
        return WebSearchSettings.model_validate(raw.get("web_search") or {})
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_web_search_settings: falling back to defaults: %s", e)
        return WebSearchSettings()


def set_web_search_settings(
    provider: str | None = None,
    override: str | Path | None = None,
) -> Path:
    """Merge the web-search source into the ``web_search:`` block."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    ws = raw.setdefault("web_search", {})
    if not isinstance(ws, dict):  # malformed hand-edit; replace
        ws = {}
        raw["web_search"] = ws
    if provider is not None:
        ws["provider"] = str(provider or "").strip().lower()
    raw["web_search"] = WebSearchSettings.model_validate(ws).model_dump()
    return _write_raw(paths, raw)


class MessageWindows(BaseModel):
    """Per-window caps on inbound messages. ``None`` ⇒ unlimited for that window.

    Shared by the home-wide ``rate_limits:`` block and the per-agent
    ``security.rate_limits`` overrides, so both tiers parse into the same shape.
    """

    per_minute: int | None = None
    per_hour: int | None = None
    per_day: int | None = None

    def to_limits(self) -> list[tuple[int, int]]:
        """As ``(max_calls, window_seconds)`` pairs for the rate limiter.

        Only windows with a positive cap are included; unset (``None``) windows
        are omitted (unlimited). A window explicitly set to ``0`` is kept so the
        limiter blocks it outright.
        """
        pairs: list[tuple[int, int]] = []
        if self.per_minute is not None:
            pairs.append((int(self.per_minute), 60))
        if self.per_hour is not None:
            pairs.append((int(self.per_hour), 3600))
        if self.per_day is not None:
            pairs.append((int(self.per_day), 86_400))
        return pairs


class RateLimitSettings(BaseModel):
    """The ``rate_limits:`` block of ``~/.olano/config.yaml`` — home-wide default
    caps on inbound messages. Per-agent ``agent.json5 → security.rate_limits``
    overrides these when set. Feature is opt-in: everything unset ⇒ unlimited."""

    messages: MessageWindows = Field(default_factory=MessageWindows)
    per_platform: dict[str, MessageWindows] = Field(default_factory=dict)


#: field → (default, clamp) for the funnel knobs' validator on SystemSettings.
_FUNNEL_KNOBS: dict[str, tuple[int, Any]] = {
    "promote_threshold": (DEFAULT_PROMOTE_THRESHOLD, clamp_promote_threshold),
    "max_promotions_per_run": (DEFAULT_MAX_PROMOTIONS_PER_RUN, clamp_max_promotions),
    "max_self_scheduled_tasks": (DEFAULT_MAX_SELF_SCHEDULED_TASKS, clamp_self_scheduled),
}


#: Cognition defaults on a managed deployment — the Olano chat tiers, billed in
#: credits with no provider key on the box. ``turbo`` IS the fast tier (it exists
#: to be the fast, no-knobs one; ``flash`` is its legacy alias), so it backs the
#: fast model; ``pro`` backs the deep runs. The class defaults below stay the
#: BYOK values an unmanaged install falls back to.
OLANO_FAST_MODEL = "olano:turbo"
OLANO_DEEP_MODEL = "olano:pro"


class SystemSettings(BaseModel):
    """The ``cognition:`` block of ``~/.olano/config.yaml``."""

    # Master switch for ALL cognition features. Off by default: every agent that
    # does not opt in itself (agent.json5 → cognition.enabled: true) runs no
    # Cortex until an operator turns it on for the fleet.
    enabled: bool = False
    autonomous: bool = True  # always-on autonomous engine (vs manual-only)
    auto_approve: bool = True  # "Fully Autonomous": auto-approve cognition outputs
    intensity: str = DEFAULT_INTENSITY  # relaxed | balanced | aggressive
    features: CognitionFeatureToggles = Field(default_factory=CognitionFeatureToggles)
    fast_model: str = "openai-codex:gpt-5.4-mini"
    deep_model: str | None = "openai-codex:gpt-5.5"  # None ⇒ inherit the agent's own model

    #: Fleet-wide budget for ONE background reasoning run — the tier an agent
    #: inherits when its own ``cognition.worker_max_iterations`` /
    #: ``worker_timeout_seconds`` are unset. ``worker_max_iterations`` is a
    #: langgraph ``recursion_limit`` counted in SUPER-STEPS (~4 per model turn),
    #: not tool calls; see ``security/delegate_guards`` for the sizing math.
    #: These exist because the knob used to be per-agent only, so an operator
    #: watching every agent hit the same step limit had no single place to raise
    #: it — and on a box with no SSH, no place at all.
    worker_max_iterations: int = DEFAULT_MAX_ITERATIONS
    worker_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    #: How many background reasoning runs may talk to a provider at once,
    #: box-wide (every agent's Cortex phases, self-learning distillation and
    #: Pulse actions share it). A home-wide knob only — it caps a shared
    #: resource, so a per-agent opinion would make no sense. Clamped to
    #: ``[1, 16]``; see ``security/delegate_guards.py`` for why the default is 2.
    worker_max_concurrency: int = DEFAULT_WORKER_CONCURRENCY

    #: The funnel's autonomous decisions, home tier (``olano.config.cortex_funnel``
    #: holds the bounds). Under Fully Autonomous the triage phase rates every
    #: active item and, for one it recommends promoting at or above
    #: ``promote_threshold`` (1-10), moves it to the next stage on its own
    #: (brainstorm → idea → prototype → project), at most
    #: ``max_promotions_per_run`` per run; it discards items it rated as not
    #: worth keeping when ``auto_discard`` is on (reversible). An agent may hold
    #: ``max_self_scheduled_tasks`` pending follow-up steps it scheduled for
    #: itself with ``cortex_schedule``. Each is overridable per agent
    #: (``agent.json5 → cognition``, ``null`` ⇒ inherit).
    promote_threshold: int = DEFAULT_PROMOTE_THRESHOLD
    max_promotions_per_run: int = DEFAULT_MAX_PROMOTIONS_PER_RUN
    max_self_scheduled_tasks: int = DEFAULT_MAX_SELF_SCHEDULED_TASKS
    auto_discard: bool = DEFAULT_AUTO_DISCARD

    @field_validator("worker_max_concurrency", mode="before")
    @classmethod
    def _coerce_worker_concurrency(cls, v: object) -> int:
        """Clamp to the legal range; fall back to the default on garbage."""
        try:
            return clamp_worker_concurrency(int(v))  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return DEFAULT_WORKER_CONCURRENCY

    @field_validator(
        "promote_threshold", "max_promotions_per_run", "max_self_scheduled_tasks", mode="before"
    )
    @classmethod
    def _coerce_funnel_knob(cls, v: object, info: ValidationInfo) -> int:
        """Clamp; fall back to the default on garbage (same rule as the other knobs)."""
        default, clamp = _FUNNEL_KNOBS[str(info.field_name)]
        try:
            return int(clamp(int(v)))  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return default

    @field_validator("intensity", mode="before")
    @classmethod
    def _coerce_intensity(cls, v: object) -> str:
        """Coerce unknown/missing intensity to the default rather than erroring,
        so a single bad value never resets the whole cognition block."""
        return v if isinstance(v, str) and v in INTENSITIES else DEFAULT_INTENSITY

    @field_validator("worker_max_iterations", "worker_timeout_seconds", mode="before")
    @classmethod
    def _coerce_worker_budget(cls, v: object, info: ValidationInfo) -> int:
        """Clamp to the legal range; fall back to the default on garbage.

        Same reasoning as ``intensity``: ``load_system_settings`` must never
        raise, because a single bad value in ``config.yaml`` would otherwise
        reset every home-level cognition field to its default at once.
        """
        is_steps = info.field_name == "worker_max_iterations"
        try:
            value = int(v)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return DEFAULT_MAX_ITERATIONS if is_steps else DEFAULT_TIMEOUT_SECONDS
        return clamp_max_iterations(value) if is_steps else clamp_timeout_seconds(value)


def load_system_settings(override: str | Path | None = None) -> SystemSettings:
    """Load the ``cognition`` block from ``config.yaml``; defaults when missing.

    **Managed default is the Olano tiers**: on a managed deployment a model that
    was never explicitly configured resolves to ``olano:turbo`` (fast) /
    ``olano:pro`` (deep), so cognition runs on credits with no provider key —
    the same rule the speech block follows. An explicit value in the file always
    wins, including one that points back at a BYOK provider, and unmanaged
    installs keep the BYOK class defaults (there is no gateway to bill through).

    Never raises — a missing or malformed file yields all-default settings so a
    broken config can never disable the product.
    """
    block: dict = {}
    try:
        config_path = get_olano_paths(override).config_path
        if config_path.exists():
            raw = read_home_config_doc(override)
            block = raw.get("cognition") or {}
            if not isinstance(block, dict):  # malformed hand-edit
                block = {}
        settings = SystemSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_system_settings: falling back to defaults: %s", e)
        block, settings = {}, SystemSettings()

    if ("fast_model" not in block or "deep_model" not in block) and olano_credits_available():
        if "fast_model" not in block:
            settings.fast_model = OLANO_FAST_MODEL
        if "deep_model" not in block:
            settings.deep_model = OLANO_DEEP_MODEL
    return settings


def default_cognition_models() -> tuple[str, str | None]:
    """``(fast, deep)`` this box falls back to with nothing configured.

    The single answer behind both the loader above and the reset path in
    :func:`set_cognition_model`, so clearing the field in the dashboard lands on
    the same model the box would have used before anyone touched it.
    """
    if olano_credits_available():
        return (OLANO_FAST_MODEL, OLANO_DEEP_MODEL)
    defaults = SystemSettings()
    return (defaults.fast_model, defaults.deep_model)


# ── `default:*` model aliases ─────────────────────────────────────────────────
#
# `default:turbo` / `default:pro` / `default:max` are the deployment-level model
# aliases: an agent.json5 (or a picker) that names one is asking for "whatever
# this box considers its turbo/pro/max brain" rather than pinning a literal.
# What each alias points at is decided here — the `default_models:` block of
# config.yaml when the operator set one, this box's built-in target otherwise —
# and resolved at build time by `olano.models.aliases.resolve_model_alias`, so
# retargeting the fleet is one config.yaml edit, never a file rewrite.
#
# Built-in targets fork on the same `managed_olano_default()` predicate as the
# speech/cognition/multimodal defaults: a managed box runs the Olano tiers on
# credits; an unmanaged box has no gateway to bill through (an `olano:*` target
# there could only 401), so it falls back to Codex literals reachable through
# the ChatGPT/Codex OAuth connection.

DEFAULT_MODEL_TIERS = ("turbo", "pro", "max")

MANAGED_DEFAULT_MODEL_TARGETS = {
    "turbo": "olano:turbo",
    "pro": "olano:pro",
    "max": "olano:max",
}

BYOK_DEFAULT_MODEL_TARGETS = {
    "turbo": "openai-codex:gpt-5.6-luna",
    "pro": "openai-codex:gpt-5.6-terra",
    "max": "openai-codex:gpt-5.6-sol",
}


class DefaultModelSettings(BaseModel):
    """The ``default_models:`` block of ``~/.olano/config.yaml``.

    Empty string = unset = fall back to this box's built-in target.
    """

    turbo: str = ""
    pro: str = ""
    max: str = ""

    @field_validator("turbo", "pro", "max", mode="before")
    @classmethod
    def _clean(cls, v: object) -> str:
        return v.strip() if isinstance(v, str) else ""


def builtin_default_model_targets() -> dict[str, str]:
    """The alias targets this box uses with nothing configured.

    The single answer behind both :func:`load_default_models` and the reset
    path in :func:`set_default_model`, so clearing a field in the dashboard
    lands on the same target the box would have used before anyone touched it.
    """
    table = (
        MANAGED_DEFAULT_MODEL_TARGETS if olano_credits_available() else BYOK_DEFAULT_MODEL_TARGETS
    )
    return dict(table)


def load_default_models(override: str | Path | None = None) -> dict[str, str]:
    """Resolve every alias tier to a concrete target: ``{"turbo": "...", ...}``.

    An explicit non-empty value in the ``default_models:`` block wins per key;
    an unset/empty key falls back to :func:`builtin_default_model_targets`.
    Never raises — a missing or malformed block yields the built-in targets so
    a hand-edit can never leave an agent without a brain.
    """
    settings = DefaultModelSettings()
    try:
        raw: dict = {}
        paths = get_olano_paths(override)
        if paths.config_path.exists():
            raw = read_home_config_doc(override)
        block = raw.get("default_models") or {}
        if isinstance(block, dict):
            settings = DefaultModelSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_default_models: falling back to defaults: %s", e)
        settings = DefaultModelSettings()

    resolved = builtin_default_model_targets()
    for tier in DEFAULT_MODEL_TIERS:
        value = getattr(settings, tier, "") or ""
        if value:
            resolved[tier] = value
    return resolved


def default_models_report(override: str | Path | None = None) -> dict[str, dict[str, str]]:
    """Per-tier ``{value, resolved, builtin}`` for the dashboard Config card.

    ``value`` is the operator's explicit target ("" = unset), ``resolved`` the
    target the alias currently points at, ``builtin`` this box's fallback — so
    the UI can render "inherited → Olano Turbo" vs. an explicit pin without
    re-implementing the resolution rules. Never raises.
    """
    settings = DefaultModelSettings()
    try:
        paths = get_olano_paths(override)
        if paths.config_path.exists():
            block = read_home_config_doc(override).get("default_models") or {}
            if isinstance(block, dict):
                settings = DefaultModelSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("default_models_report: falling back to defaults: %s", e)
        settings = DefaultModelSettings()

    resolved = load_default_models(override)
    builtin = builtin_default_model_targets()
    return {
        tier: {
            "value": getattr(settings, tier, "") or "",
            "resolved": resolved[tier],
            "builtin": builtin[tier],
        }
        for tier in DEFAULT_MODEL_TIERS
    }


def load_playground_settings(override: str | Path | None = None) -> PlaygroundSettings:
    """Load the ``playground`` block from ``config.yaml``; defaults when missing.

    Never raises — a missing or malformed file yields all-default settings so a
    broken config can never break the dashboard.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return PlaygroundSettings()
        raw = read_home_config_doc(override)
        block = raw.get("playground") or {}
        return PlaygroundSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_playground_settings: falling back to defaults: %s", e)
        return PlaygroundSettings()


def playground_enabled(
    override: str | Path | None = None,
    *,
    settings: PlaygroundSettings | None = None,
) -> bool:
    """True when the dashboard Playground (and its animation sprite generation)
    is enabled home-wide. Default OFF (opt-in)."""
    settings = settings or load_playground_settings(override)
    return settings.enabled


def load_self_serve_agent_settings(
    override: str | Path | None = None,
) -> SelfServeAgentSettings:
    """Load the ``self_serve_agents`` block from ``config.yaml``.

    Never raises: a missing, unreadable or malformed block yields the
    all-default settings. Note what that means since the default flipped to
    **on**: an operator's explicit ``enabled: false`` inside a block that
    later becomes unparseable reads as the default again — the same shape as
    ``multi_user_enabled``'s posture default, accepted for the same reason
    (a broken config must not take the dashboard down, and the quota +
    slug validation still bound what the default hands out).
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return SelfServeAgentSettings()
        raw = read_home_config_doc(override)
        block = raw.get("self_serve_agents") or {}
        return SelfServeAgentSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_self_serve_agent_settings: falling back to defaults: %s", e)
        return SelfServeAgentSettings()


def self_serve_agents_enabled(
    override: str | Path | None = None,
    *,
    settings: SelfServeAgentSettings | None = None,
) -> bool:
    """True when a non-admin member may create their own agent. Default ON."""
    settings = settings or load_self_serve_agent_settings(override)
    return settings.enabled


def self_serve_archetype_allowed(
    archetype: str,
    override: str | Path | None = None,
    *,
    settings: SelfServeAgentSettings | None = None,
) -> bool:
    """Whether a self-serve create may use ``archetype``.

    An empty ``allowed_archetypes`` means "any archetype in the catalog" — and
    also permits the from-scratch create (empty ``archetype``) the dashboard's
    plain New-agent form sends. A non-empty list is a strict allow-list, so a
    from-scratch create is refused too: the operator named the archetypes they
    are willing to hand out, and "none of them" is not one of them.
    """
    settings = settings or load_self_serve_agent_settings(override)
    if not settings.allowed_archetypes:
        return True
    return archetype.strip() in settings.allowed_archetypes


def load_agent_autonomy_settings(
    override: str | Path | None = None,
) -> AgentAutonomySettings:
    """Load the ``agent_autonomy`` block from ``config.yaml``.

    Never raises: a missing, unreadable or malformed block yields the
    all-default settings — everything **on**, this feature's shipped posture
    (same trade as ``self_serve_agents``: a broken config must not take the
    runtime down, and each capability's own guardrails still apply).
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return AgentAutonomySettings()
        raw = read_home_config_doc(override)
        block = raw.get("agent_autonomy") or {}
        return AgentAutonomySettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_agent_autonomy_settings: falling back to defaults: %s", e)
        return AgentAutonomySettings()


def autonomy_allowed(
    capability: str,
    config: AgentConfig | None = None,
    *,
    settings: AgentAutonomySettings | None = None,
) -> bool:
    """Whether the agent behind ``config`` may use ``capability`` on itself.

    The one resolution rule, both directions (see the cognition gates): the
    agent wins when it has an opinion (``autonomy.<capability>`` is a bool),
    the global ``agent_autonomy`` block applies when it doesn't. **This is the
    only reader** — ``getattr(cfg.autonomy, cap)`` reads ``None`` as falsy and
    would deny every unconfigured agent.

    One deliberate exception to the global default: an agent in
    **customer-service mode** (``memory.per_customer``) inherits **off**. Its
    chat surface is strangers on a shared channel, and "rewrite your prompt" /
    "connect yourself to my server" from a stranger is precisely the attack
    these gates exist to stop — the customer-service archetype's minimal tool
    surface is a pinned security property
    (``tests/test_customer_memory.py``). An explicit per-agent ``true`` still
    re-enables a capability deliberately.

    An unknown ``capability`` is ``False``, never an exception: the callers
    are tool bodies, and a typo'd gate must fail closed rather than crash a
    turn.
    """
    if capability not in AUTONOMY_CAPABILITIES:
        logger.warning("autonomy_allowed: unknown capability %r", capability)
        return False
    override = getattr(config, "autonomy", None) if config is not None else None
    value = getattr(override, capability, None) if override is not None else None
    if isinstance(value, bool):
        return value
    if config is not None and bool(getattr(getattr(config, "memory", None), "per_customer", False)):
        return False
    settings = settings or load_agent_autonomy_settings()
    return bool(getattr(settings, capability))


def set_agent_autonomy_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``agent_autonomy:`` block of
    ``config.yaml``, preserving all other keys. Round-tripped through
    :class:`AgentAutonomySettings` so only known capabilities land on disk."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("agent_autonomy")
    if not isinstance(block, dict):  # missing or malformed hand-edit; replace
        block = {}
    allowed = set(AgentAutonomySettings.model_fields)
    block.update({k: bool(v) for k, v in updates.items() if k in allowed})
    raw["agent_autonomy"] = AgentAutonomySettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def load_group_thread_settings(override: str | Path | None = None) -> GroupThreadSettings:
    """Load the ``groups`` block from ``config.yaml``; defaults (on) when missing.

    Never raises — a malformed block reads as the defaults. Those now have
    ``enabled=True``, so unlike every other fail-safe loader here this one falls
    back to a feature being *available*. That is deliberate and bounded: the
    default also has ``autonomous=False``, so what a broken config can turn on
    is the surface where one addressed member answers one message, never the
    cascade that spends model calls with nobody watching.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return GroupThreadSettings()
        raw = read_home_config_doc(override)
        block = raw.get("groups") or {}
        return GroupThreadSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_group_thread_settings: falling back to defaults: %s", e)
        return GroupThreadSettings()


def group_threads_enabled(
    override: str | Path | None = None,
    *,
    settings: GroupThreadSettings | None = None,
) -> bool:
    """True when multi-bot group threads (``/api/groups*``) are on. Default ON."""
    settings = settings or load_group_thread_settings(override)
    return settings.enabled


def load_disk_monitor_settings(override: str | Path | None = None) -> DiskMonitorSettings:
    """Load the ``disk_monitor`` block from ``config.yaml``; defaults when missing.

    Never raises — a missing or malformed file yields all-default settings
    (monitoring on, cleanup on with every registry action selected) so a broken
    config can never disable the monitor or silently widen the action set
    beyond the fixed allow-list.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return DiskMonitorSettings()
        raw = read_home_config_doc(override)
        block = raw.get("disk_monitor") or {}
        return DiskMonitorSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_disk_monitor_settings: falling back to defaults: %s", e)
        return DiskMonitorSettings()


def load_usage_budget_settings(override: str | Path | None = None) -> UsageBudgetSettings:
    """Load the ``usage_budgets`` block from ``config.yaml``; defaults when missing.

    Never raises — a missing or malformed file yields all-default settings,
    which override nothing and keep the deployment's own budgets in force. That
    is the safe direction: a broken config must not be able to silently lift a
    ceiling.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return UsageBudgetSettings()
        raw = read_home_config_doc(override)
        block = raw.get("usage_budgets") or {}
        return UsageBudgetSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_usage_budget_settings: falling back to defaults: %s", e)
        return UsageBudgetSettings()


def load_magic_link_settings(override: str | Path | None = None) -> MagicLinkSettings:
    """Load the ``magic_links`` block from ``config.yaml``; defaults when missing.

    Reads the legacy ``handshake:`` key as a fallback so a deployment that
    predates the rename keeps its settings; an explicit ``magic_links:`` wins
    when a config carries both.

    Never raises — a missing or malformed file yields all-default settings
    (links on, 15-minute TTL, PIN required, group mints refused), so a broken
    config can never silently *weaken* the magic-link gates.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return MagicLinkSettings()
        raw = read_home_config_doc(override)
        block = raw.get(MAGIC_LINKS_CONFIG_KEY) or raw.get(LEGACY_MAGIC_LINKS_CONFIG_KEY) or {}
        return MagicLinkSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_magic_link_settings: falling back to defaults: %s", e)
        return MagicLinkSettings()


def load_catalog_sync_settings(override: str | Path | None = None) -> CatalogSyncSettings:
    """Load the ``catalog_sync`` block from ``config.yaml``; defaults when missing.

    Never raises — a missing or malformed file yields all-default settings
    (sync on, hourly manifest poll) so a broken config can never silently
    stop catalog updates.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return CatalogSyncSettings()
        raw = read_home_config_doc(override)
        block = raw.get("catalog_sync") or {}
        return CatalogSyncSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_catalog_sync_settings: falling back to defaults: %s", e)
        return CatalogSyncSettings()


def load_credits_settings(override: str | Path | None = None) -> CreditsSettings:
    """Load the ``credits`` block from ``config.yaml``; defaults when missing.

    Never raises — a missing or malformed file yields ``display: usd``, the
    unit the runtime accounts in, so a broken config degrades to showing the
    raw numbers rather than to showing nothing.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return CreditsSettings()
        raw = read_home_config_doc(override)
        block = raw.get("credits") or {}
        return CreditsSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_credits_settings: falling back to defaults: %s", e)
        return CreditsSettings()


def cost_display_locked() -> bool:
    """True when the display unit is dictated by the signed deployment
    settings — i.e. this is a managed box, where neither config.yaml nor the
    env may override it. Never raises (resolver fails safe)."""
    try:
        from olano.deployment import resolver as dep_resolver

        return dep_resolver.deployment_status().posture != "open"
    except Exception:  # pragma: no cover - defensive
        return False


def cost_display_mode(override: str | Path | None = None) -> str:
    """The unit costs are displayed in: ``"usd"`` or ``"credits"``.

    **Managed deployments** (posture enforced/quarantine): the SIGNED
    deployment settings decide — ``cost_display`` from the artifact, default
    ``"credits"`` when the field is absent/None. config.yaml and the env var
    are deliberately ignored: the operator (and the agent, which can edit
    config.yaml) must not be able to flip a managed dashboard back to raw
    provider dollars, because that number is Olano's COGS, not their price.

    **Unmanaged installs** (open posture — OSS/dev): ``LIONCLAW_COST_DISPLAY``
    env → config.yaml ``credits.display`` → ``"usd"``. Dollars are the honest
    default when there is no cloud to meter credits.
    """
    try:
        from olano.deployment import resolver as dep_resolver

        status = dep_resolver.deployment_status()
        if status.posture != "open":
            signed = str(getattr(status.settings, "cost_display", None) or "").strip().lower()
            return signed if signed in COST_DISPLAY_MODES else "credits"
    except Exception:  # pragma: no cover — a broken resolver must not blank costs
        logger.debug("cost_display_mode: resolver unavailable", exc_info=True)
    env = os.environ.get("LIONCLAW_COST_DISPLAY", "").strip().lower()
    if env in COST_DISPLAY_MODES:
        return env
    configured = _configured_cost_display(override)
    if configured is not None:
        return configured
    # A self-hosted box spending from a pasted Olano account key keeps its
    # credits book, so the honest default is the unit that book is kept in.
    # Not locked: the operator may still pick dollars in Config.
    try:
        from olano.deployment.gateway_key import account_key_if_unmanaged

        if account_key_if_unmanaged():
            return "credits"
    except Exception:  # pragma: no cover - defensive
        pass
    return load_credits_settings(override).display


def _configured_cost_display(override: str | Path | None = None) -> str | None:
    """``credits.display`` as WRITTEN in config.yaml, or ``None`` when the
    operator never set it (the loader's default is not a choice)."""
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return None
        raw = read_home_config_doc(override)
        block = raw.get("credits") or {}
        value = str(block.get("display") or "").strip().lower() if isinstance(block, dict) else ""
        return value if value in COST_DISPLAY_MODES else None
    except Exception:  # pragma: no cover - defensive
        return None


def set_cost_display_mode(mode: str, override: str | Path | None = None) -> Path:
    """Persist the ``credits.display`` mode in config.yaml, preserving all other
    keys. Raises ValueError on an unknown mode — unlike the loader, an explicit
    write with a bad value is a caller bug worth surfacing."""
    normalized = (mode or "").strip().lower()
    if normalized not in COST_DISPLAY_MODES:
        raise ValueError(f"Unknown cost display mode: {mode!r} (expected usd or credits)")
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    # The loader tolerates a malformed block (falls back to usd), so a hand-edited
    # `credits: something` can reach here — replace it rather than raising
    # TypeError on subscripting a str.
    block = raw.get("credits")
    if not isinstance(block, dict):
        block = {}
        raw["credits"] = block
    block["display"] = normalized
    return _write_raw(paths, raw)


def load_issues_settings(override: str | Path | None = None) -> IssuesSettings:
    """Load the ``issues`` block from ``config.yaml``; defaults when missing.

    Never raises — a malformed config yields all-default settings (auto
    process on, 15-minute poll) so a broken file can never silently stop
    issue delivery.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return IssuesSettings()
        raw = read_home_config_doc(override)
        block = raw.get("issues") or {}
        return IssuesSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_issues_settings: falling back to defaults: %s", e)
        return IssuesSettings()


def effective_issues_auto_process(
    agent_cfg: AgentConfig | None = None,
    *,
    settings: IssuesSettings | None = None,
) -> bool:
    """Whether the issue poll auto-processes for this agent.

    Per-agent ``issues.auto_process`` override (non-None) wins; otherwise
    the home-wide ``issues.auto_process`` default (on).
    """
    override = getattr(getattr(agent_cfg, "issues", None), "auto_process", None)
    if override is not None:
        return bool(override)
    return (settings or load_issues_settings()).auto_process


def effective_issues_poll_minutes(
    agent_cfg: AgentConfig | None = None,
    *,
    settings: IssuesSettings | None = None,
) -> int:
    """Minutes between issue polls for this agent (clamped 1–1440)."""
    override = getattr(getattr(agent_cfg, "issues", None), "poll_minutes", None)
    if override is not None:
        return _clamp_int(override, default=15, lo=1, hi=24 * 60)
    return (settings or load_issues_settings()).poll_minutes


def load_revisions_settings(override: str | Path | None = None) -> RevisionsSettings:
    """Load the ``revisions`` block from ``config.yaml``; defaults when missing.

    Never raises — a malformed config yields all-default settings (tracking
    on, memory included) rather than silently disabling revision history.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return RevisionsSettings()
        raw = read_home_config_doc(override)
        block = raw.get("revisions") or {}
        return RevisionsSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_revisions_settings: falling back to defaults: %s", e)
        return RevisionsSettings()


class MobileAppInboxSettings(BaseModel):
    """``mobile_app.inbox``: the Inbox tab and its three sections."""

    enabled: bool = True
    approvals: bool = True
    alerts: bool = True
    issues: bool = True


class MobileAppSettingsTabSettings(BaseModel):
    """``mobile_app.settings_tab``: optional rows on the app's Settings tab."""

    files: bool = True
    credits_usage: bool = True
    transcribe_on_phone: bool = True


class MobileAppSettings(BaseModel):
    """The ``mobile_app:`` block of ``config.yaml``: how the Olano mobile app looks.

    Served to every signed-in phone by ``GET /api/mobile/config`` together with
    a ``revision`` (a hash of the resolved values), and re-read by the app at
    every launch and every return to the foreground, so an edit reaches phones
    without an app update or a runtime restart. Every switch defaults to shown,
    so a box that never set the block looks exactly as it did before.

    Hiding is presentation, not permission: the API behind a hidden tab still
    answers, it is just not offered on the phone. ``transcribe_on_phone_default``
    is where a voice note becomes words on a phone whose person has not chosen
    (and on every phone while the row is hidden): ``false`` sends the recording
    to the deployment, whose transcript the agent also gets on every channel.
    """

    cortex_tab: bool = True
    inbox: MobileAppInboxSettings = Field(default_factory=MobileAppInboxSettings)
    settings_tab: MobileAppSettingsTabSettings = Field(default_factory=MobileAppSettingsTabSettings)
    transcribe_on_phone_default: bool = False

    def revision(self) -> str:
        """A short content hash: the app re-applies its layout only when this moves."""
        import hashlib
        import json

        payload = json.dumps(self.model_dump(), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_mobile_app_settings(override: str | Path | None = None) -> MobileAppSettings:
    """Load the ``mobile_app`` block from ``config.yaml``; defaults when missing.

    Never raises: a malformed hand-edit reads as the defaults (everything
    shown), never as a phone with no tabs. Unknown keys are ignored.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return MobileAppSettings()
        raw = read_home_config_doc(override)
        block = raw.get("mobile_app") or {}
        if not isinstance(block, dict):
            return MobileAppSettings()
        return MobileAppSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_mobile_app_settings: falling back to defaults: %s", e)
        return MobileAppSettings()


def load_push_settings(override: str | Path | None = None) -> PushSettings:
    """Load the ``push`` block from ``config.yaml``; defaults when missing.

    Never raises. Read on every notification (the parse is cached by
    ``read_home_config_doc``), so an edit applies without a restart, except
    ``enabled``, which decides whether the dispatcher starts at boot.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return PushSettings()
        raw = read_home_config_doc(override)
        block = raw.get("push") or {}
        return PushSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_push_settings: falling back to defaults: %s", e)
        return PushSettings()


def load_prompt_refresh_settings(override: str | Path | None = None) -> PromptRefreshSettings:
    """Load the ``prompt_refresh`` block from ``config.yaml``; defaults when missing.

    Never raises — a missing or malformed file yields all-default settings
    (sweep on, 2-minute tick, thread notices on) so a broken config can never
    silently freeze running agents on a stale prompt.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return PromptRefreshSettings()
        raw = read_home_config_doc(override)
        block = raw.get("prompt_refresh") or {}
        return PromptRefreshSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_prompt_refresh_settings: falling back to defaults: %s", e)
        return PromptRefreshSettings()


class CheckpointGcSettings(BaseModel):
    """The ``checkpoint_gc:`` block of ``~/.olano/config.yaml``.

    Governs the checkpoint history sweep (:mod:`olano.core.checkpoint_gc`).
    LangGraph keeps every checkpoint a thread ever wrote and never deletes
    one; a load needs only the latest checkpoint and the writes back to each
    delta channel's last snapshot, and the rest grew to 40 GB on one box. The
    sweep keeps exactly that tail and removes the rest. Postgres only; the
    sqlite backend is left alone. The loop re-reads this block each tick.

    - ``enabled``: master switch. ``LIONCLAW_NO_CHECKPOINT_GC=1`` disables it
      for one boot.
    - ``interval_hours``: how often the sweep runs after the first pass.
    - ``boot_delay_minutes``: how long after the agents boot the first pass
      waits, so it never competes with the boot for the database.
    - ``keep_min``: checkpoints always kept per thread namespace, whatever
      the snapshots say; the margin keeps the sweep clear of a run in flight.
    - ``max_minutes``: budget per pass; a pass that runs out stops between
      threads and the next pass continues (largest threads go first).
    """

    enabled: bool = True
    interval_hours: int = 6
    boot_delay_minutes: int = 10
    keep_min: int = 3
    max_minutes: int = 30

    @field_validator("interval_hours", mode="before")
    @classmethod
    def _coerce_interval(cls, v: object) -> int:
        return _clamp_int(v, default=6, lo=1, hi=24 * 7)

    @field_validator("boot_delay_minutes", mode="before")
    @classmethod
    def _coerce_boot_delay(cls, v: object) -> int:
        return _clamp_int(v, default=10, lo=0, hi=24 * 60)

    @field_validator("keep_min", mode="before")
    @classmethod
    def _coerce_keep_min(cls, v: object) -> int:
        return _clamp_int(v, default=3, lo=1, hi=1000)

    @field_validator("max_minutes", mode="before")
    @classmethod
    def _coerce_max_minutes(cls, v: object) -> int:
        return _clamp_int(v, default=30, lo=1, hi=24 * 60)


def load_checkpoint_gc_settings(override: str | Path | None = None) -> CheckpointGcSettings:
    """Load the ``checkpoint_gc`` block from ``config.yaml``; defaults when missing.

    Never raises: a missing or malformed file yields the defaults (sweep on,
    every 6 hours), so a broken config cannot silently let history pile up.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return CheckpointGcSettings()
        raw = read_home_config_doc(override)
        block = raw.get("checkpoint_gc") or {}
        return CheckpointGcSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_checkpoint_gc_settings: falling back to defaults: %s", e)
        return CheckpointGcSettings()


class PeerThreadSettings(BaseModel):
    """The ``peer_threads:`` block of ``~/.olano/config.yaml``.

    How much conversation two agents carry into each ``talk_to_agent``
    exchange. A pair of agents shares one thread, and it used to be resumed
    for the life of the deployment, so every exchange replayed months of
    history nothing in the reply needed. Read at every peer call, so a change
    applies to the next exchange with no restart.

    - ``rotate``: master switch. Off, a pair keeps its one thread forever
      (the old behaviour).
    - ``max_age_days``: a pair's thread is retired once its first exchange
      is this old; the next exchange opens a fresh one. Agent-to-agent
      exchanges are mostly one-shot, so the default is short.
    - ``max_turns``: the same, once the thread's transcript holds this many
      rows (a row per message, so an exchange is about two).
    """

    rotate: bool = True
    max_age_days: int = 3
    max_turns: int = 200

    @field_validator("max_age_days", mode="before")
    @classmethod
    def _coerce_max_age(cls, v: object) -> int:
        return _clamp_int(v, default=3, lo=1, hi=365)

    @field_validator("max_turns", mode="before")
    @classmethod
    def _coerce_max_turns(cls, v: object) -> int:
        return _clamp_int(v, default=200, lo=10, hi=100_000)


class SchedulerSettings(BaseModel):
    """The ``scheduler:`` block of ``~/.olano/config.yaml``.

    How the runtime treats a scheduled task whose firing could not happen on
    the second. Two things used to lose a run outright: a firing more than
    five minutes late (APScheduler's grace, which the busy event loop of a
    big deployment exceeds) was recorded as skipped and never run, and a
    firing that fell inside a restart was not recorded at all, because the
    job came back with its next run computed from boot time. A daily report
    due at 08:00 is still wanted at 08:40; this block says how long.

    - ``misfire_grace_minutes``: how late a scheduled task or an agent.json5
      schedule entry may still run. Past it the firing is recorded as skipped
      (and a notification is raised). Heartbeat polls, issue polls and Cortex
      phases keep the short default, since the next tick is minutes away.
    - ``catch_up_on_boot``: at startup, a task whose most recent fire time
      fell inside the grace window and has no run recorded since (the
      runtime was down or restarting at the time) runs at once, its prompt
      saying how late it is. Off, a restart across a fire time drops it.
    - ``max_run_minutes``: the wall clock a single scheduled run may use.
      A run that is still going at the limit is stopped, recorded as failed
      with the reason, and reported on the bell; what it wrote stays. Only
      one run of a task executes at a time, so a run that never ends used
      to hold its task's slot and every later firing was dropped as
      "previous run still executing" (on one box three runs had been going
      for five to fifteen hours when a restart ended them). ``0`` removes
      the limit. Heartbeat polls get the same bound.
    """

    misfire_grace_minutes: int = 360
    catch_up_on_boot: bool = True
    max_run_minutes: int = 180

    @field_validator("max_run_minutes", mode="before")
    @classmethod
    def _coerce_max_run(cls, v: object) -> int:
        return _clamp_int(v, default=180, lo=0, hi=1440)

    @field_validator("misfire_grace_minutes", mode="before")
    @classmethod
    def _coerce_grace(cls, v: object) -> int:
        return _clamp_int(v, default=360, lo=1, hi=1440)


def load_scheduler_settings(override: str | Path | None = None) -> SchedulerSettings:
    """Load the ``scheduler`` block from ``config.yaml``; defaults when missing.

    Never raises: a missing or malformed file yields the defaults (six hours
    of grace, catch-up on), so a broken config cannot bring the five-minute
    drop back.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return SchedulerSettings()
        raw = read_home_config_doc(override)
        block = raw.get("scheduler") or {}
        return SchedulerSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_scheduler_settings: falling back to defaults: %s", e)
        return SchedulerSettings()


def load_peer_thread_settings(override: str | Path | None = None) -> PeerThreadSettings:
    """Load the ``peer_threads`` block from ``config.yaml``; defaults when missing.

    Never raises: a missing or malformed file yields the defaults (rotation
    on, 3 days, 200 rows), so a broken config cannot bring the unbounded
    thread back.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return PeerThreadSettings()
        raw = read_home_config_doc(override)
        block = raw.get("peer_threads") or {}
        return PeerThreadSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_peer_thread_settings: falling back to defaults: %s", e)
        return PeerThreadSettings()


def load_mcp_health_settings(override: str | Path | None = None) -> McpHealthSettings:
    """Load the ``mcp_health`` block from ``config.yaml``; defaults when missing.

    Never raises — a missing or malformed file yields all-default settings
    (sweep on, 10-minute tick, token renewal and auto-reconnect on) so a broken
    config can never silently stop MCP connections from being maintained.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return McpHealthSettings()
        raw = read_home_config_doc(override)
        block = raw.get("mcp_health") or {}
        return McpHealthSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_mcp_health_settings: falling back to defaults: %s", e)
        return McpHealthSettings()


def load_pages_settings(override: str | Path | None = None) -> PagesSettings:
    """Load the ``pages`` block from ``config.yaml``; defaults when missing.

    Never raises: a missing or malformed block reads as the defaults, so a
    broken file cannot strand running page servers in an unknown state.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return PagesSettings()
        raw = read_home_config_doc(override)
        block = raw.get("pages") or {}
        if not isinstance(block, dict):
            return PagesSettings()
        return PagesSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_pages_settings: falling back to defaults: %s", e)
        return PagesSettings()


def load_browser_settings(override: str | Path | None = None) -> BrowserSettings:
    """Load the ``browser`` block from ``config.yaml``; defaults when missing.

    Never raises. A missing, malformed or non-mapping block reads as the
    default, which is ``mode: off``, so a broken file can only ever take
    browsers away, never hand them out.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return BrowserSettings()
        raw = read_home_config_doc(override)
        block = raw.get("browser") or {}
        if not isinstance(block, dict):
            return BrowserSettings()
        return BrowserSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_browser_settings: falling back to defaults: %s", e)
        return BrowserSettings()


def load_observability_settings(override: str | Path | None = None) -> ObservabilitySettings:
    """Load the ``observability`` block from ``config.yaml``; defaults when missing.

    Never raises. A missing, malformed or non-mapping block reads as the
    default, which has tracing off, so a broken file can only ever stop an
    export, never start one.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return ObservabilitySettings()
        raw = read_home_config_doc(override)
        block = raw.get("observability") or {}
        if not isinstance(block, dict):
            return ObservabilitySettings()
        return ObservabilitySettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_observability_settings: falling back to defaults: %s", e)
        return ObservabilitySettings()


def load_context_window_settings(override: str | Path | None = None) -> ContextWindowSettings:
    """Load the ``context_window`` block from ``config.yaml``; defaults when missing.

    Never raises — a missing or malformed file yields the defaults, so a broken
    config can never leave an agent with deepagents' 6-message keep policy.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return ContextWindowSettings()
        raw = read_home_config_doc(override)
        block = raw.get("context_window") or {}
        return ContextWindowSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_context_window_settings: falling back to defaults: %s", e)
        return ContextWindowSettings()


def effective_context_window(
    agent_cfg: AgentConfig | None = None,
    *,
    settings: ContextWindowSettings | None = None,
) -> ContextWindowPolicy:
    """Resolve the context-window policy for one agent.

    Same rule as every other override block: *the agent wins when it has an
    opinion, the home block applies when it doesn't*. Each field resolves
    independently, and an agent value goes through the same clamps as the
    home block (by round-tripping the merged dict through
    :class:`ContextWindowSettings`), so a hand-edited ``keep_fraction: 5``
    cannot escape the validators.
    """
    base = settings or load_context_window_settings()
    merged = base.model_dump()
    override = getattr(agent_cfg, "context_window", None)
    for key in merged:
        value = getattr(override, key, None)
        if value is not None:
            merged[key] = value
    resolved = ContextWindowSettings.model_validate(merged)
    return ContextWindowPolicy(**resolved.model_dump())


def load_remote_agent_settings(override: str | Path | None = None) -> RemoteAgentSettings:
    """Load the ``remote_agents`` block from ``config.yaml``; defaults when missing.

    Never raises — a missing or malformed file yields the defaults (client on,
    ad-hoc discovery off), so a broken config can neither silently open
    arbitrary discovery nor strand a configured remote agent.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return RemoteAgentSettings()
        raw = read_home_config_doc(override)
        block = raw.get("remote_agents") or {}
        if not isinstance(block, dict):
            return RemoteAgentSettings()
        return RemoteAgentSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_remote_agent_settings: falling back to defaults: %s", e)
        return RemoteAgentSettings()


def load_external_access_settings(
    override: str | Path | None = None,
) -> ExternalAccessSettings:
    """Load the ``external_access`` block from ``config.yaml``; defaults when missing.

    Never raises — and both surfaces default to **off**, so a malformed block
    can never accidentally publish agents to the network.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return ExternalAccessSettings()
        raw = read_home_config_doc(override)
        block = raw.get("external_access") or {}
        if not isinstance(block, dict):
            return ExternalAccessSettings()
        return ExternalAccessSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_external_access_settings: falling back to defaults: %s", e)
        return ExternalAccessSettings()


def load_sandbox_settings(override: str | Path | None = None) -> SandboxSettings:
    """Load the ``sandbox`` block from ``config.yaml``; defaults when missing.

    Never raises — a missing or malformed file yields all-default settings
    (both limits unset ⇒ the runtime auto-sizes each sandbox from the host),
    so a broken config can never leave agents with no shell at all.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return SandboxSettings()
        raw = read_home_config_doc(override)
        block = raw.get("sandbox") or {}
        return SandboxSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_sandbox_settings: falling back to defaults: %s", e)
        return SandboxSettings()


def load_rate_limit_settings(override: str | Path | None = None) -> RateLimitSettings:
    """Load the ``rate_limits`` block from ``config.yaml``; defaults when missing.

    Never raises — a missing or malformed file yields all-default (unlimited)
    settings so a broken config can never brick message ingress.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return RateLimitSettings()
        raw = read_home_config_doc(override)
        block = raw.get("rate_limits") or {}
        return RateLimitSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_rate_limit_settings: falling back to defaults: %s", e)
        return RateLimitSettings()


# Heartbeat poll (HEARTBEAT.md checks) — system-wide defaults + per-agent overrides.
DEFAULT_HEARTBEAT_INTERVAL_MINUTES = 60
HEARTBEAT_INTERVAL_MIN_MINUTES = 1
HEARTBEAT_INTERVAL_MAX_MINUTES = 24 * 60


def load_heartbeat_settings(override: str | Path | None = None) -> HeartbeatSettings:
    """Load the ``heartbeat`` block from ``config.yaml``; defaults when missing.

    Never raises — a missing or malformed file yields all-default settings so a
    broken config can never brick the heartbeat poll.
    """
    try:
        config_path = get_olano_paths(override).config_path
        if not config_path.exists():
            return HeartbeatSettings()
        raw = read_home_config_doc(override)
        block = raw.get("heartbeat") or {}
        return HeartbeatSettings.model_validate(block)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("load_heartbeat_settings: falling back to defaults: %s", e)
        return HeartbeatSettings()


def clamp_heartbeat_interval(minutes: int) -> int:
    """Clamp a heartbeat poll interval to the supported 1 min – 24 h range."""
    return max(
        HEARTBEAT_INTERVAL_MIN_MINUTES,
        min(HEARTBEAT_INTERVAL_MAX_MINUTES, int(minutes)),
    )


def effective_heartbeat_enabled(
    agent_cfg: AgentConfig | None = None,
    *,
    settings: HeartbeatSettings | None = None,
) -> bool:
    """Whether the heartbeat poll runs for this agent.

    Per-agent ``heartbeat.enabled`` override (non-None) wins; otherwise the
    home-wide ``heartbeat.enabled`` default (on).
    """
    override = getattr(getattr(agent_cfg, "heartbeat", None), "enabled", None)
    if override is not None:
        return bool(override)
    return (settings or load_heartbeat_settings()).enabled


def effective_heartbeat_interval(
    agent_cfg: AgentConfig | None = None,
    *,
    settings: HeartbeatSettings | None = None,
) -> int:
    """Minutes between heartbeat polls for this agent, clamped to 1–1440.

    Per-agent ``heartbeat.interval_minutes`` override (non-None) wins; otherwise
    the home-wide default (60 = hourly).
    """
    override = getattr(getattr(agent_cfg, "heartbeat", None), "interval_minutes", None)
    value = (
        override
        if override is not None
        else (settings or load_heartbeat_settings()).interval_minutes
    )
    return clamp_heartbeat_interval(value)


def _deployment_cortex_enabled() -> bool:
    """The deployment-entitlements gate for ALL cognition (managed installs).

    A signed deployment-settings artifact can disable the Cortex engine for a
    whole deployment (``features.cortex: false``); it ANDs with the home-config
    master switch and per-agent overrides below. Checked per call — the engine
    consults these gates each cycle, so a remote change bites without restart.
    Defensive: an entitlements-layer failure never crashes cognition gating
    (the resolver itself fails closed for managed installs).
    """
    try:
        from olano.deployment import enforcement as dep_enforcement

        return dep_enforcement.feature_enabled("cortex")
    except Exception:
        logger.exception("deployment cortex gate failed; treating as disabled")
        return False


# ── The one resolution rule ──────────────────────────────────────────────────
# Every Cortex switch exists at two tiers: a home-wide default in ``config.yaml``
# and a per-agent override in ``agent.json5 → cognition``. The rule is:
#
#     the agent wins when it has an opinion; the global applies when it doesn't.
#
# ``None`` on the agent side means "inherit" — that is what makes the dashboard's
# Inherit / On / Off control mean what it says. Every override field is
# ``bool | None`` for exactly this reason, ``enabled`` included.
#
# These gates used to short-circuit on the global value BEFORE reading the agent
# block, so a per-agent value could only ever subtract. That made the ordinary
# arrangement — Cortex off across the fleet, on for one agent you're piloting —
# unreachable, while the UI kept offering a three-state control that promised it.
# Turning something on for one agent is not an edge case; it is the entire point
# of a per-agent override.
#
# The one thing that still short-circuits is the deployment entitlement
# (:func:`_deployment_cortex_enabled`). That is a licensing gate, not a
# preference, and an agent must not be able to opt its way past it.


def _cog_block(agent_cfg: AgentConfig | None) -> object | None:
    """The agent's ``cognition`` block, or None. Defensive via ``getattr`` so
    these gates work against any config-shaped object."""
    return getattr(agent_cfg, "cognition", None) if agent_cfg is not None else None


def _resolve(cog: object | None, field: str, global_value: bool) -> bool:
    """``field`` resolved across the two tiers. A non-bool (missing, ``None``)
    on the agent side means inherit."""
    override = getattr(cog, field, None) if cog is not None else None
    return override if isinstance(override, bool) else bool(global_value)


def _master_enabled(cog: object | None, settings: SystemSettings) -> bool:
    """The resolved Cortex master switch, without the deployment entitlement.

    Split out because :func:`auto_approve_enabled` deliberately does not consult
    the entitlement (see its docstring), while everything else does.
    """
    return _resolve(cog, "enabled", settings.enabled)


def cortex_enabled(
    agent_cfg: AgentConfig | None = None,
    *,
    settings: SystemSettings | None = None,
) -> bool:
    """True when the Cortex engine is enabled *at all* for this agent — manual
    or autonomous.

    The resolved master switch, ANDed with the deployment entitlement. Unlike
    :func:`autonomy_enabled` this does NOT require the ``autonomous`` switch — a
    manual-only Cortex still counts as enabled. Gates the agent-facing
    ``cortex_*`` chat tools (list / view / add / edit / promote / explore /
    discard / restore of Cortex items), and every scheduler decision about
    whether to register this agent's cognition jobs at all.
    """
    if not _deployment_cortex_enabled():
        return False
    settings = settings or load_system_settings()
    return _master_enabled(_cog_block(agent_cfg), settings)


def feature_enabled(
    feature: str,
    agent_cfg: AgentConfig | None = None,
    *,
    settings: SystemSettings | None = None,
) -> bool:
    """Effective toggle for one feature group.

    Cortex must be on for this agent (:func:`cortex_enabled`), and then the
    feature itself resolves across the two tiers. Legacy feature names are
    resolved via :data:`_FEATURE_ALIASES`.
    """
    feature = canonical_feature(feature)
    if feature not in FEATURES:
        raise ValueError(f"Unknown cognition feature: {feature!r}. Known: {FEATURES}")

    settings = settings or load_system_settings()
    if not cortex_enabled(agent_cfg, settings=settings):
        return False
    return _resolve(_cog_block(agent_cfg), feature, getattr(settings.features, feature, True))


def autonomy_enabled(
    agent_cfg: AgentConfig | None = None,
    *,
    settings: SystemSettings | None = None,
) -> bool:
    """True when the always-on autonomous Cortex engine should schedule work.

    Cortex must be on for this agent, and then ``autonomous`` resolves across
    the two tiers. Feature-level gating is still applied per-phase via
    :func:`feature_enabled`.
    """
    settings = settings or load_system_settings()
    if not cortex_enabled(agent_cfg, settings=settings):
        return False
    return _resolve(_cog_block(agent_cfg), "autonomous", settings.autonomous)


def auto_approve_enabled(
    agent_cfg: AgentConfig | None = None,
    *,
    settings: SystemSettings | None = None,
) -> bool:
    """True when "Fully Autonomous" mode should auto-approve cognition outputs.

    When on, cognition results that would otherwise wait for a human click —
    learned-skill drafts and self-improvement proposals (APPROVAL lane), plus
    cognition pulse items that carry an executable action — are approved/applied
    automatically. Defaults ON (so a fresh install is fully hands-off); flip it
    off to fall back to human approvals.

    Deliberately does NOT consult the deployment entitlement, unlike the gates
    above: this also answers "may the dashboard sweep the approval queue?", and
    a deployment that stops Cortex from producing new items should not strand
    the ones already sitting in the queue.
    """
    settings = settings or load_system_settings()
    cog = _cog_block(agent_cfg)
    if not _master_enabled(cog, settings):
        return False
    return _resolve(cog, "auto_approve", getattr(settings, "auto_approve", True))


def approval_required_categories(agent_cfg: AgentConfig | None = None) -> frozenset[str]:
    """The categories this agent must always route to a human, even when
    :func:`auto_approve_enabled` is on.

    Reads the per-agent ``cognition.approval_required`` list; a missing or
    malformed value falls back to :data:`~olano.config.agent_config.DEFAULT_APPROVAL_REQUIRED`
    rather than to "hold nothing", so a broken config fails safe. Names are
    normalised (trimmed, lower-cased); an unknown name matches nothing. This is
    per-agent only — the user guide documents it as a list with a concrete
    default, not a three-state inherit, and the docs move before the shape does.

    The two enforcement points import this and classify with
    :mod:`olano.cognition.approval_guard`, so the sweep and the runner can never
    disagree about what counts as identity, external or irreversible.
    """
    from olano.cognition.approval_guard import normalise_categories
    from olano.config.agent_config import DEFAULT_APPROVAL_REQUIRED

    cog = _cog_block(agent_cfg)
    raw = getattr(cog, "approval_required", None) if cog is not None else None
    if isinstance(raw, list | tuple | set | frozenset):
        return normalise_categories(raw)
    return normalise_categories(DEFAULT_APPROVAL_REQUIRED)


def effective_intensity(
    agent_cfg: AgentConfig | None = None,
    *,
    settings: SystemSettings | None = None,
) -> str:
    """Per-agent ``cognition.intensity`` override, else the global intensity."""
    settings = settings or load_system_settings()
    cog = getattr(agent_cfg, "cognition", None) if agent_cfg is not None else None
    override = getattr(cog, "intensity", None) if cog is not None else None
    value = override or settings.intensity
    return value if value in INTENSITIES else DEFAULT_INTENSITY


def _resolve_int(cog: object | None, field: str, global_value: int, clamp: Any) -> int:
    """An integer knob resolved across the two tiers: the agent's value when it
    is an int (``None`` ⇒ inherit), else the home-wide one; clamped either way."""
    override = getattr(cog, field, None) if cog is not None else None
    if isinstance(override, bool):  # a bool is an int in Python; never a knob value
        override = None
    value = override if isinstance(override, int) else int(global_value)
    return int(clamp(value))


def effective_promote_threshold(
    agent_cfg: AgentConfig | None = None,
    *,
    settings: SystemSettings | None = None,
) -> int:
    """Score (1-10) at or above which the triage phase promotes on its own."""
    settings = settings or load_system_settings()
    return _resolve_int(
        _cog_block(agent_cfg),
        "promote_threshold",
        settings.promote_threshold,
        clamp_promote_threshold,
    )


def effective_max_promotions_per_run(
    agent_cfg: AgentConfig | None = None,
    *,
    settings: SystemSettings | None = None,
) -> int:
    """Promotions one triage run may perform (each is a deep run)."""
    settings = settings or load_system_settings()
    return _resolve_int(
        _cog_block(agent_cfg),
        "max_promotions_per_run",
        settings.max_promotions_per_run,
        clamp_max_promotions,
    )


def effective_max_self_scheduled_tasks(
    agent_cfg: AgentConfig | None = None,
    *,
    settings: SystemSettings | None = None,
) -> int:
    """Pending ``cortex_schedule`` steps an agent may hold at once."""
    settings = settings or load_system_settings()
    return _resolve_int(
        _cog_block(agent_cfg),
        "max_self_scheduled_tasks",
        settings.max_self_scheduled_tasks,
        clamp_self_scheduled,
    )


def auto_discard_enabled(
    agent_cfg: AgentConfig | None = None,
    *,
    settings: SystemSettings | None = None,
) -> bool:
    """Whether the triage phase may discard items it rated as not worth keeping.

    Requires Fully Autonomous (:func:`auto_approve_enabled`): with the sweep
    off, every rating is advisory and a human discards. Then ``auto_discard``
    resolves across the two tiers like every other override.
    """
    settings = settings or load_system_settings()
    if not auto_approve_enabled(agent_cfg, settings=settings):
        return False
    return _resolve(_cog_block(agent_cfg), "auto_discard", settings.auto_discard)


def effective_worker_budget(
    agent_cfg: AgentConfig | None = None,
    *,
    settings: SystemSettings | None = None,
) -> tuple[int, int]:
    """``(max_iterations, timeout_seconds)`` for ONE background reasoning run.

    Same rule as every other override in this module: *the agent wins when it
    has an opinion; the global applies when it doesn't.* Both per-agent fields
    are ``int | None``, so ``None`` means inherit — that tri-state is the whole
    mechanism. They used to be plain ints defaulting to the shared constants,
    which made ``getattr`` return a number whether or not the key was on disk:
    every agent permanently "had an opinion", and a fleet-wide value could never
    reach a single one of them.

    **This is the only place these two fields may be read.** Reaching into
    ``cfg.cognition.worker_max_iterations`` directly now yields ``None`` for
    every unconfigured agent, and ``int(None)`` raises — the same class of trap
    as reading ``cognition.enabled`` instead of asking ``cortex_enabled()``.

    Both values are clamped, so a hand-edited ``config.yaml`` or ``agent.json5``
    can never hand langgraph a budget that fails every run before its first tool
    call, nor one that lets a single run bill without bound.
    """
    settings = settings or load_system_settings()
    cog = getattr(agent_cfg, "cognition", None) if agent_cfg is not None else None
    steps = getattr(cog, "worker_max_iterations", None) if cog is not None else None
    seconds = getattr(cog, "worker_timeout_seconds", None) if cog is not None else None
    return (
        clamp_max_iterations(steps if steps is not None else settings.worker_max_iterations),
        clamp_timeout_seconds(seconds if seconds is not None else settings.worker_timeout_seconds),
    )


def worker_budget_bounds() -> dict[str, int]:
    """The legal range for the two budget fields, for the dashboard to validate against.

    Served by ``GET /api/cognition/status`` so the UI clamps to the backend's
    numbers instead of keeping its own copy that can drift out of sync.
    """
    from olano.security.delegate_guards import (
        MAX_MAX_ITERATIONS,
        MAX_TIMEOUT_SECONDS,
        MIN_MAX_ITERATIONS,
        MIN_TIMEOUT_SECONDS,
    )

    return {
        "max_iterations_min": MIN_MAX_ITERATIONS,
        "max_iterations_max": MAX_MAX_ITERATIONS,
        "timeout_seconds_min": MIN_TIMEOUT_SECONDS,
        "timeout_seconds_max": MAX_TIMEOUT_SECONDS,
    }


# ----------------------------------------------------------------- writers


def _load_raw(paths: OlanoHomePaths) -> dict:
    raw: dict = {}
    if paths.config_path.exists():
        raw = read_home_config_doc(paths.root)
    return raw


def _write_raw(paths: OlanoHomePaths, raw: dict) -> Path:
    paths.root.mkdir(parents=True, exist_ok=True)
    write_home_config_text(paths.config_path, yaml.dump(raw, sort_keys=False))
    return paths.config_path


def set_cognition_feature(
    feature: str,
    enabled: bool,
    override: str | Path | None = None,
) -> Path:
    """Flip one feature toggle in ``config.yaml``, preserving all other keys.

    Used by the CLI affordance (``olano config cognition <feature> on|off``)
    and the dashboard per-feature toggle endpoint. Returns the config path written.
    """
    feature = canonical_feature(feature)
    if feature not in FEATURES:
        raise ValueError(f"Unknown cognition feature: {feature!r}. Known: {FEATURES}")

    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    cognition = raw.setdefault("cognition", {})
    features = cognition.setdefault("features", {})
    features[feature] = bool(enabled)
    return _write_raw(paths, raw)


def set_autonomy(
    enabled: bool,
    override: str | Path | None = None,
    *,
    master: bool | None = None,
) -> Path:
    """Flip the master ``autonomous`` switch (and optionally the master ``enabled``
    kill-switch), preserving all other keys.

    ``master=True`` also turns on ``enabled`` (so "enable autonomy" can't be a
    no-op when the master kill-switch is off); ``master=False`` flips the
    kill-switch directly.
    """
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    cognition = raw.setdefault("cognition", {})
    if master is not None:
        cognition["enabled"] = bool(master)
    cognition["autonomous"] = bool(enabled)
    # Enabling autonomy clears the master kill-switch (else it would be a no-op).
    if enabled:
        cognition["enabled"] = True
    return _write_raw(paths, raw)


def set_cortex_master(
    enabled: bool,
    override: str | Path | None = None,
) -> Path:
    """Set the home-wide Cortex master switch on its own.

    :func:`set_autonomy` can only move ``enabled`` as a side effect of moving
    ``autonomous``, so until now the fleet-wide kill-switch had no dashboard
    control at all — which is half of "Cortex off for every agent, on for the
    one I'm piloting". Agents that override ``cognition.enabled`` keep running;
    that is what an override is for.
    """
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    cognition = raw.setdefault("cognition", {})
    cognition["enabled"] = bool(enabled)
    return _write_raw(paths, raw)


def set_auto_approve(
    enabled: bool,
    override: str | Path | None = None,
) -> Path:
    """Flip the global "Fully Autonomous" auto-approve switch, preserving all other
    keys. When on, cognition outputs are approved/applied without a human click."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    cognition = raw.setdefault("cognition", {})
    cognition["auto_approve"] = bool(enabled)
    return _write_raw(paths, raw)


def set_intensity(
    value: str,
    override: str | Path | None = None,
) -> Path:
    """Set the autonomous engine intensity (relaxed|balanced|aggressive)."""
    if value not in INTENSITIES:
        raise ValueError(f"Unknown intensity: {value!r}. Known: {INTENSITIES}")
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    cognition = raw.setdefault("cognition", {})
    cognition["intensity"] = value
    return _write_raw(paths, raw)


def set_worker_budget(
    max_iterations: int | None = None,
    timeout_seconds: int | None = None,
    override: str | Path | None = None,
) -> Path:
    """Set the home-wide background-run budget, preserving every other key.

    The "All agents" tier behind ``effective_worker_budget``: an agent with no
    ``cognition.worker_max_iterations`` / ``worker_timeout_seconds`` of its own
    runs on these. Either argument may be omitted to leave that half alone.
    Values are clamped rather than rejected, matching the loader — an operator
    who types 5 gets the floor, not a 500.
    """
    if max_iterations is None and timeout_seconds is None:
        raise ValueError("set_worker_budget needs at least one of max_iterations/timeout_seconds.")
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    cognition = raw.setdefault("cognition", {})
    if max_iterations is not None:
        cognition["worker_max_iterations"] = clamp_max_iterations(max_iterations)
    if timeout_seconds is not None:
        cognition["worker_timeout_seconds"] = clamp_timeout_seconds(timeout_seconds)
    return _write_raw(paths, raw)


def set_cognition_model(
    which: str,
    value: str | None,
    override: str | Path | None = None,
) -> Path:
    """Set the cognition ``fast_model`` or ``deep_model`` in config.yaml.

    ``fast_model`` is the cheap/quick model that drives most cognition phases; an
    empty value resets it to this box's default — ``olano:turbo`` on a managed
    deployment, the BYOK literal otherwise — rather than to the BYOK literal
    everywhere, which would have quietly moved a managed box off credits the
    first time someone cleared the field. ``deep_model`` is optional — a falsy
    value is stored as ``None`` (⇒ inherit the agent's own model). Preserves every
    other key in the file."""
    if which not in ("fast_model", "deep_model"):
        raise ValueError(f"Unknown cognition model field: {which!r}")
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    cognition = raw.setdefault("cognition", {})
    if which == "deep_model":
        cognition["deep_model"] = str(value) if value else None
    else:
        cognition["fast_model"] = str(value) if value else default_cognition_models()[0]
    return _write_raw(paths, raw)


def set_default_model(
    tier: str,
    value: str | None,
    override: str | Path | None = None,
) -> Path:
    """Set (or reset) one ``default_models`` tier target in config.yaml.

    An empty/None value removes the key, so the tier falls back to this box's
    built-in target (:func:`builtin_default_model_targets`) — never to a frozen
    literal that would silently move a managed box off credits. A non-empty
    value must be a concrete ``provider:model``: another ``default:*`` alias is
    rejected (one level of indirection only — resolution must never recurse),
    and so are the Olano utility groups and unknown ``olano:`` tiers, which
    could otherwise brick every agent inheriting the alias. Preserves every
    other key in the file.
    """
    tier = str(tier or "").strip().lower()
    if tier not in DEFAULT_MODEL_TIERS:
        raise ValueError(
            f"Unknown default-model tier: {tier!r}. Known: {', '.join(DEFAULT_MODEL_TIERS)}"
        )

    cleaned = str(value or "").strip()
    if cleaned:
        if cleaned.lower().startswith("default:") or cleaned.lower() == "default":
            raise ValueError(
                f"'{cleaned}' is itself a default-model alias — a tier target must be a "
                "concrete provider:model (e.g. olano:turbo, anthropic:claude-sonnet-4-6)."
            )
        # Lazy: config parsing must never pull the model stack.
        from olano.models.olano_gateway import is_olano_utility_model, normalize_tier

        if is_olano_utility_model(cleaned):
            raise ValueError(
                f"'{cleaned}' is an Olano utility service the product calls on the agent's "
                "behalf, not a model you can assign as a default tier target."
            )
        provider, _, rest = cleaned.partition(":")
        if provider.strip().lower() == "olano" and normalize_tier(rest) is None:
            raise ValueError(
                f"Unknown Olano model tier '{rest}'. Use olano:turbo, olano:pro or olano:max."
            )

    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.setdefault("default_models", {})
    if cleaned:
        block[tier] = cleaned
    else:
        block.pop(tier, None)
        if not block:
            raw.pop("default_models", None)
    return _write_raw(paths, raw)


def set_playground_enabled(
    enabled: bool,
    override: str | Path | None = None,
) -> Path:
    """Flip the home-wide ``playground.enabled`` switch, preserving all other
    keys. When off, the dashboard hides the Playground and character generation
    skips the animation sprite sheets (the portrait is still generated)."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    playground = raw.setdefault("playground", {})
    playground["enabled"] = bool(enabled)
    return _write_raw(paths, raw)


def set_self_serve_agent_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``self_serve_agents:`` block of
    ``config.yaml``, preserving all other keys. The merged block is
    round-tripped through :class:`SelfServeAgentSettings`, so what lands on
    disk carries the loader's own guarantees (quota clamped to 1..100,
    archetype list coerced to clean strings) — the dashboard cannot persist a
    value a hand-edit would have been corrected on read."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("self_serve_agents")
    if not isinstance(block, dict):  # missing or malformed hand-edit; replace
        block = {}
    allowed = set(SelfServeAgentSettings.model_fields)
    block.update({k: v for k, v in updates.items() if k in allowed})
    raw["self_serve_agents"] = SelfServeAgentSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_disk_monitor_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Deep-merge a partial dict into the ``disk_monitor:`` block of
    ``config.yaml``, preserving all other keys. The merged block is
    round-tripped through :class:`DiskMonitorSettings` so what's persisted is
    always clamped/coerced (bad percents, unknown action names, junk vacuum
    sizes) — the same guarantees the loader gives."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("disk_monitor")
    if not isinstance(block, dict):  # missing or malformed hand-edit; replace
        block = {}
    top_allowed = set(DiskMonitorSettings.model_fields) - {"cleanup", "scan"}
    block.update({k: v for k, v in updates.items() if k in top_allowed})
    for sub_key, sub_model in (("cleanup", DiskCleanupSettings), ("scan", DiskScanSettings)):
        sub_updates = updates.get(sub_key)
        if isinstance(sub_updates, dict):
            sub = block.get(sub_key)
            if not isinstance(sub, dict):
                sub = {}
            allowed = set(sub_model.model_fields)
            sub.update({k: v for k, v in sub_updates.items() if k in allowed})
            block[sub_key] = sub
    raw["disk_monitor"] = DiskMonitorSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_usage_budget_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``usage_budgets:`` block of ``config.yaml``.

    Only keys the model knows are written, so a ``cost_*`` entry cannot be
    smuggled in through the dashboard any more than through a hand-edit.
    Passing ``None`` for a token key **clears** the override (back to the
    deployment's own ceiling) rather than writing a null — that is what an
    emptied dashboard field means, and it keeps "inherit" expressible.
    """
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("usage_budgets")
    if not isinstance(block, dict):  # missing or malformed hand-edit; replace
        block = {}
    for key, value in updates.items():
        if key not in UsageBudgetSettings.model_fields:
            continue
        if key in OVERRIDABLE_USAGE_LIMITS and value is None:
            block.pop(key, None)
            continue
        block[key] = value
    merged = UsageBudgetSettings.model_validate(block)
    persisted = merged.model_dump()
    # Drop the keys that carry no opinion so the file stays a record of what
    # the operator actually set — a null for every unset budget reads like a
    # configured "unlimited" to the next person who opens the file.
    for key in OVERRIDABLE_USAGE_LIMITS:
        if persisted.get(key) is None:
            persisted.pop(key, None)
    raw["usage_budgets"] = persisted
    return _write_raw(paths, raw)


def set_catalog_sync_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``catalog_sync:`` block of ``config.yaml``,
    preserving all other keys. Round-tripped through
    :class:`CatalogSyncSettings` so what's persisted is always clamped/coerced."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("catalog_sync")
    if not isinstance(block, dict):  # missing or malformed hand-edit; replace
        block = {}
    allowed = set(CatalogSyncSettings.model_fields)
    block.update({k: v for k, v in updates.items() if k in allowed})
    raw["catalog_sync"] = CatalogSyncSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_magic_link_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``magic_links:`` block of ``config.yaml``,
    preserving all other keys. Round-tripped through :class:`MagicLinkSettings`
    so what's persisted is always clamped/coerced — in particular an unknown
    ``pin`` value persists as ``required``, never as ``off``.

    A config still holding the legacy ``handshake:`` key is seeded from it and
    then rewritten under ``magic_links:`` — the old key is dropped so the two
    can never diverge after a write."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get(MAGIC_LINKS_CONFIG_KEY)
    if not isinstance(block, dict):
        legacy = raw.get(LEGACY_MAGIC_LINKS_CONFIG_KEY)
        block = legacy if isinstance(legacy, dict) else {}
    allowed = set(MagicLinkSettings.model_fields)
    top = {k: v for k, v in updates.items() if k in allowed and k != "owner_login"}
    block.update(top)
    # owner_login is a nested block — deep-merge so patching one of its keys
    # (e.g. enabled) doesn't drop the others.
    if isinstance(updates.get("owner_login"), dict):
        current = block.get("owner_login")
        if not isinstance(current, dict):
            current = {}
        ol_allowed = set(OwnerLoginSettings.model_fields)
        current.update({k: v for k, v in updates["owner_login"].items() if k in ol_allowed})
        block["owner_login"] = current
    raw[MAGIC_LINKS_CONFIG_KEY] = MagicLinkSettings.model_validate(block).model_dump()
    raw.pop(LEGACY_MAGIC_LINKS_CONFIG_KEY, None)
    return _write_raw(paths, raw)


def set_prompt_refresh_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``prompt_refresh:`` block of
    ``config.yaml``, preserving all other keys. Round-tripped through
    :class:`PromptRefreshSettings` so what's persisted is always
    clamped/coerced."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("prompt_refresh")
    if not isinstance(block, dict):  # missing or malformed hand-edit; replace
        block = {}
    allowed = set(PromptRefreshSettings.model_fields)
    block.update({k: v for k, v in updates.items() if k in allowed})
    raw["prompt_refresh"] = PromptRefreshSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_external_access_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``external_access:`` block of ``config.yaml``,
    preserving all other keys. Nested blocks (``mcp``, ``a2a``, ``oauth``,
    ``jwt``) merge key-by-key too, so a dashboard that sends only
    ``{"mcp": {"enabled": true}}`` cannot wipe the JWT issuer. Round-tripped
    through :class:`ExternalAccessSettings` so what's persisted is always
    clamped/coerced."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("external_access")
    if not isinstance(block, dict):  # missing or malformed hand-edit; replace
        block = {}
    allowed = set(ExternalAccessSettings.model_fields)
    for key, value in updates.items():
        if key not in allowed:
            continue
        current = block.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged = dict(current)
            merged.update(value)
            block[key] = merged
        elif isinstance(value, dict) and isinstance(current, bool):
            block[key] = value  # `mcp: true` shorthand replaced by the object form
        else:
            block[key] = value
    raw["external_access"] = ExternalAccessSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_browser_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``browser:`` block of ``config.yaml``,
    preserving all other keys. Round-tripped through :class:`BrowserSettings`
    so what's persisted is always clamped/coerced. Does not run the hardware
    check: callers that turn the browser on go through
    ``olano.browser.policy.enable_refusal`` first."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("browser")
    if not isinstance(block, dict):
        block = {}
    allowed = set(BrowserSettings.model_fields)
    block.update({k: v for k, v in updates.items() if k in allowed})
    raw["browser"] = BrowserSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_langfuse_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into ``observability.langfuse`` in ``config.yaml``,
    preserving all other keys. Round-tripped through :class:`LangfuseSettings`
    so what's persisted is always coerced (an unusable host is stored empty,
    an unknown capture mode as ``metadata``). Credentials never live here:
    they are reserved vault rows (:mod:`olano.observability.credentials`)."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("observability")
    if not isinstance(block, dict):
        block = {}
    lf = block.get("langfuse")
    if not isinstance(lf, dict):
        lf = {}
    allowed = set(LangfuseSettings.model_fields)
    lf.update({k: v for k, v in updates.items() if k in allowed})
    block["langfuse"] = LangfuseSettings.model_validate(lf).model_dump()
    raw["observability"] = block
    return _write_raw(paths, raw)


def set_mcp_health_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``mcp_health:`` block of ``config.yaml``,
    preserving all other keys. Round-tripped through :class:`McpHealthSettings`
    so what's persisted is always clamped/coerced."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("mcp_health")
    if not isinstance(block, dict):  # missing or malformed hand-edit; replace
        block = {}
    allowed = set(McpHealthSettings.model_fields)
    block.update({k: v for k, v in updates.items() if k in allowed})
    raw["mcp_health"] = McpHealthSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_scheduler_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``scheduler:`` block of ``config.yaml``,
    preserving all other keys; round-tripped through :class:`SchedulerSettings`
    so what is persisted is always clamped."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("scheduler")
    if not isinstance(block, dict):
        block = {}
    allowed = set(SchedulerSettings.model_fields)
    block.update({k: v for k, v in updates.items() if k in allowed})
    raw["scheduler"] = SchedulerSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_peer_thread_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``peer_threads:`` block of ``config.yaml``,
    preserving all other keys; round-tripped through :class:`PeerThreadSettings`
    so what is persisted is always clamped."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("peer_threads")
    if not isinstance(block, dict):
        block = {}
    allowed = set(PeerThreadSettings.model_fields)
    block.update({k: v for k, v in updates.items() if k in allowed})
    raw["peer_threads"] = PeerThreadSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_checkpoint_gc_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``checkpoint_gc:`` block of ``config.yaml``,
    preserving all other keys; round-tripped through :class:`CheckpointGcSettings`."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("checkpoint_gc")
    if not isinstance(block, dict):
        block = {}
    allowed = set(CheckpointGcSettings.model_fields)
    block.update({k: v for k, v in updates.items() if k in allowed})
    raw["checkpoint_gc"] = CheckpointGcSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_mobile_app_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``mobile_app:`` block of ``config.yaml``.

    The two nested groups (``inbox``, ``settings_tab``) merge key by key, so a
    caller can flip one switch without resending the rest. Round-tripped
    through :class:`MobileAppSettings`, so only known keys and booleans land.
    """
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("mobile_app")
    if not isinstance(block, dict):
        block = {}
    for key, value in updates.items():
        if key not in MobileAppSettings.model_fields:
            continue
        if key in ("inbox", "settings_tab") and isinstance(value, dict):
            current = block.get(key)
            merged = dict(current) if isinstance(current, dict) else {}
            merged.update(value)
            block[key] = merged
        else:
            block[key] = value
    raw["mobile_app"] = MobileAppSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_pages_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``pages:`` block of ``config.yaml``,
    preserving all other keys. Round-tripped through :class:`PagesSettings`
    so what's persisted is always clamped/coerced."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("pages")
    if not isinstance(block, dict):
        block = {}
    allowed = set(PagesSettings.model_fields)
    block.update({k: v for k, v in updates.items() if k in allowed})
    raw["pages"] = PagesSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_context_window_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``context_window:`` block of ``config.yaml``,
    preserving all other keys. Round-tripped through
    :class:`ContextWindowSettings` so what's persisted is always clamped."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("context_window")
    if not isinstance(block, dict):  # missing or malformed hand-edit; replace
        block = {}
    allowed = set(ContextWindowSettings.model_fields)
    block.update({k: v for k, v in updates.items() if k in allowed})
    raw["context_window"] = ContextWindowSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_sandbox_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``sandbox:`` block of ``config.yaml``,
    preserving all other keys. Round-tripped through :class:`SandboxSettings`
    so what's persisted is always normalized (``"2G"`` → ``"2g"``) and a value
    outside the supported range can never reach an agent build."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("sandbox")
    if not isinstance(block, dict):  # missing or malformed hand-edit; replace
        block = {}
    allowed = set(SandboxSettings.model_fields)
    block.update({k: v for k, v in updates.items() if k in allowed})
    raw["sandbox"] = SandboxSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_disk_cleanup_enabled(
    enabled: bool,
    override: str | Path | None = None,
) -> Path:
    """Flip the destructive-cleanup opt-in (``disk_monitor.cleanup.enabled``),
    preserving all other keys. Monitoring/alerts are governed separately by
    ``disk_monitor.enabled`` + ``LIONCLAW_NO_DISK_MONITOR``."""
    return set_disk_monitor_settings({"cleanup": {"enabled": bool(enabled)}}, override)


_RATE_LIMIT_WINDOW_KEYS = ("per_minute", "per_hour", "per_day")


def clean_message_windows(raw: object) -> dict[str, int]:
    """Keep only positive-int window caps from a window map.

    Null / zero / absent / non-int values are dropped (⇒ unlimited for that
    window), so the dashboard's "empty = unset" model round-trips cleanly and a
    stray ``0`` never becomes a silent block-all. ``bool`` is rejected (it is an
    ``int`` subclass but never a valid cap)."""
    out: dict[str, int] = {}
    if not isinstance(raw, dict):
        return out
    for key in _RATE_LIMIT_WINDOW_KEYS:
        val = raw.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, int) and val > 0:
            out[key] = val
    return out


def set_rate_limits(
    messages: object = None,
    per_platform: object = None,
    override: str | Path | None = None,
) -> Path:
    """Persist the home-wide ``rate_limits:`` block, preserving all other keys.

    ``messages`` is the agent-wide default window map (``per_minute``/``per_hour``/
    ``per_day``); ``per_platform`` maps a platform name to its own window map. Only
    positive-int windows are kept, empty platform maps are dropped, and if nothing
    remains the whole ``rate_limits`` key is removed — so an all-empty submission
    means "unlimited" rather than leaving a stale block behind."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)

    block: dict[str, object] = {}
    msg = clean_message_windows(messages)
    if msg:
        block["messages"] = msg
    platforms: dict[str, dict[str, int]] = {}
    if isinstance(per_platform, dict):
        for name, windows in per_platform.items():
            cleaned = clean_message_windows(windows)
            if cleaned:
                platforms[str(name).strip().lower()] = cleaned
    if platforms:
        block["per_platform"] = platforms

    if block:
        raw["rate_limits"] = block
    else:
        raw.pop("rate_limits", None)
    return _write_raw(paths, raw)


def set_group_thread_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``groups:`` block of ``config.yaml``,
    preserving all other keys. Round-tripped through
    :class:`GroupThreadSettings` so what's persisted is always clamped/coerced:
    neither the dashboard's Group chats card nor an agent's admin tool can
    persist a ``max_hops: 500`` the loader would have corrected on read, and
    ``max_members < min_members`` reconciles on a write exactly as on a
    hand-edit."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("groups")
    if not isinstance(block, dict):  # missing or malformed hand-edit; replace
        block = {}
    allowed = set(GroupThreadSettings.model_fields)
    block.update({k: v for k, v in updates.items() if k in allowed})
    raw["groups"] = GroupThreadSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_issues_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``issues:`` block of ``config.yaml``,
    preserving all other keys. Round-tripped through :class:`IssuesSettings`
    so what's persisted is always clamped/coerced (poll interval 1–1440 min)."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("issues")
    if not isinstance(block, dict):  # missing or malformed hand-edit; replace
        block = {}
    allowed = set(IssuesSettings.model_fields)
    block.update({k: v for k, v in updates.items() if k in allowed})
    raw["issues"] = IssuesSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)


def set_revisions_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``revisions:`` block of ``config.yaml``,
    preserving all other keys. Round-tripped through :class:`RevisionsSettings`
    so what's persisted is always clamped/coerced. ``enabled: false`` takes
    effect at the next server boot; existing history is kept."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("revisions")
    if not isinstance(block, dict):  # missing or malformed hand-edit; replace
        block = {}
    allowed = set(RevisionsSettings.model_fields)
    block.update({k: v for k, v in updates.items() if k in allowed})
    # Only what differs from the defaults is written, so a later change of a
    # default (max_file_kb went from 1 MB to 50 MB) reaches every box that
    # never chose a value, instead of being frozen by an incidental save.
    raw["revisions"] = RevisionsSettings.model_validate(block).model_dump(exclude_defaults=True)
    return _write_raw(paths, raw)


def set_heartbeat_settings(
    updates: dict,
    override: str | Path | None = None,
) -> Path:
    """Merge a partial dict into the ``heartbeat:`` block of ``config.yaml``,
    preserving all other keys. Round-tripped through :class:`HeartbeatSettings`;
    per-agent ``agent.json5 → heartbeat`` overrides still win where set."""
    paths = get_olano_paths(override)
    raw = _load_raw(paths)
    block = raw.get("heartbeat")
    if not isinstance(block, dict):  # missing or malformed hand-edit; replace
        block = {}
    allowed = set(HeartbeatSettings.model_fields)
    block.update({k: v for k, v in updates.items() if k in allowed})
    raw["heartbeat"] = HeartbeatSettings.model_validate(block).model_dump()
    return _write_raw(paths, raw)
