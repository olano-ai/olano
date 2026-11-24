"""Structured, *actionable* classification of runtime errors.

A raw provider exception like ``openai.AuthenticationError: 401 token_invalidated``
is useless to an end user. This module turns such exceptions into an
:class:`ActionableError` — a stable ``code``, a clear human ``message``, and an
optional :class:`Remediation` describing exactly how to fix it (and a
machine-readable ``action`` the dashboard can turn into a one-click button).

The same classification feeds every surface uniformly:

- **Dashboard chat** — :func:`to_error_frame` yields ``{message, code,
  remediation}`` in the SSE error frame; the UI renders a "Reconnect" / "Open
  Providers" call-to-action from ``remediation.action``.
- **Chat channels** (Telegram, WhatsApp, Discord, …) — :func:`channel_text`
  renders a self-contained message: what happened, how to fix it, and where
  (a dashboard link when one is configured). Channels can't show a popup, so the
  remediation travels as text.
- **Surfaces with their own generic wording** — the platform adapters
  (`platforms/base.py`) and the CLI kept their own message and printed the
  raw exception, so a classified error's remediation reached neither.
  :func:`channel_text_if_classified` and :func:`remediation_lines` let them
  defer to the classifier only when it recognized the failure.

This is the single source of truth: :mod:`olano.core.runner` delegates its
``_classify_error`` here, so adding a rule fixes the message everywhere at once.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Deliberately a literal rather than an import from ``olano.deployment.resolver``
# (which holds the same address for the licensing messages): every olano import
# in this module is lazy, inside a function, so the module stays stdlib-only at
# import time — and ``olano.core.__init__`` pulls builder+runner, so importing
# core from deployment would put a cycle between two packages that already reach
# for each other. `tests/test_actionable_errors.py` pins the two in sync.
SUPPORT_CONTACT = "support@olano.ai"

# ── Stable error codes ───────────────────────────────────────────────────────
# Kept stable because the dashboard and tests key off them.
CODE_AUTH_EXPIRED = "provider_auth_expired"  # OAuth/session token revoked → re-sign-in
CODE_INVALID_KEY = "invalid_api_key"  # static API key wrong/missing → set a new one
CODE_CREDITS_LOW = "credits_low"
CODE_OLANO_CREDITS = "olano_credits_exhausted"  # deployment's olano-credit balance spent
# A self-hosted box's pasted Olano account API key was refused by the gateway
# (revoked on cloud.olano.ai, or mistyped). Nothing on the box can renew it; a
# person pastes a valid one on Providers → Olano. Never raised on a managed
# deployment, whose key re-enrolls itself (CODE_AUTH_EXPIRED).
CODE_OLANO_KEY_INVALID = "olano_key_invalid"
# A self-hosted box has NO Olano key at all (none pasted, or the pasted one was
# removed) and an agent still points at an olano:* tier, so the gateway sees
# the unenrolled placeholder and refuses. Nothing retries its way out of this
# either; a person connects a key or picks a provider model.
CODE_OLANO_KEY_MISSING = "olano_key_missing"
# The gateway serves no model group by the name we asked for. Never a local
# misconfiguration: this box can only ever send a group name it hardcodes, so an
# unrouted one means the cloud routing table is missing an entry we depend on.
CODE_OLANO_UNROUTED = "olano_service_unrouted"
# The gateway routed us fine and the *upstream* leg refused — e.g. the provider
# account behind a managed tier ran out of balance. An Olano supply problem,
# never this deployment's key and never its credits.
CODE_OLANO_UPSTREAM = "olano_service_unavailable"
CODE_BILLING = "billing_limit"
CODE_RATE_LIMIT = "rate_limited"
CODE_OVERLOADED = "overloaded"
CODE_CONTEXT = "context_overflow"
CODE_TIMEOUT = "timeout"
# The HTTP connection to the model broke mid-call — a dropped socket, a reset
# peer, a half-read response body. Transient by construction and fixed by simply
# asking again, which is why it must never reach a user as an exception name.
CODE_NETWORK = "network_transient"
CODE_FS_ONLY = "filesystem_only"
CODE_DB_LOCKED = "session_db_locked"  # SQLite write-lock contention on threads.db → transient
# The provider's client refused the SHAPE of a message already in the thread —
# a content block it has no representation for. Distinct from every rule above
# because nothing about the account or the network is wrong, and distinct from
# a context overflow because retrying the same thread can never clear it: the
# offending block lives in the checkpoint, so the thread is stuck until it is
# started fresh. See `models/ollama_compat.py` for the case that produced it.
CODE_MESSAGE_FORMAT = "message_format_unsupported"
# LangGraph's recursion limit hit before the run produced a final answer — the
# run's STEP budget (super-steps, not tool calls) ran out mid-task.
CODE_STEP_BUDGET = "step_budget_exhausted"
CODE_GENERIC = "error"

# ── Remediation action kinds (the dashboard maps these to UI) ────────────────
REMEDY_RECONNECT = "reconnect_provider"  # re-run an OAuth/device sign-in flow
REMEDY_SET_KEY = "set_api_key"  # set/refresh an API key (Providers / vault)
REMEDY_TOP_UP = "top_up_billing"  # external billing page
REMEDY_RETRY = "retry"  # transient — just try again
REMEDY_CONTACT = "contact_support"  # nothing local to change; an Olano-side fix
REMEDY_NONE = "none"


@dataclass(frozen=True)
class Remediation:
    """How a user can fix an :class:`ActionableError`.

    ``action`` is a small machine-readable dict the dashboard turns into a
    button, e.g. ``{"type": "reconnect_provider", "provider": "openai-codex"}``.
    """

    kind: str
    summary: str
    provider: str | None = None
    env_var: str | None = None
    url: str | None = None
    action: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "provider": self.provider,
            "env_var": self.env_var,
            "url": self.url,
            "action": self.action,
        }


@dataclass(frozen=True)
class ActionableError:
    """A classified, user-presentable error."""

    code: str
    message: str
    remediation: Remediation | None = None
    retryable: bool = False
    # Carried for logging/telemetry; never shown to the user.
    detail: str = field(default="", compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "remediation": self.remediation.to_dict() if self.remediation else None,
        }


# ── Provider helpers ─────────────────────────────────────────────────────────


def _provider_name(provider: str | None) -> str:
    if not provider:
        return "the model provider"
    try:
        from olano.models import PROVIDERS

        info = PROVIDERS.get(provider)
        if info is not None:
            return info.name
    except Exception:
        pass
    return provider


def _provider_env_var(provider: str | None) -> str | None:
    if not provider:
        return None
    try:
        from olano.models import PROVIDERS

        info = PROVIDERS.get(provider)
        return info.env_var if info else None
    except Exception:
        return None


def _is_oauth_provider(provider: str | None) -> bool:
    """True for providers authenticated by an interactive sign-in (no static key)."""
    return provider == "openai-codex"


def dashboard_url() -> str | None:
    """Best-effort public dashboard URL for deep-links in channel messages.

    Reads ``LIONCLAW_PUBLIC_URL`` / ``LIONCLAW_DASHBOARD_URL``; falls back to a
    localhost URL on the configured dashboard port. Returns ``None`` if nothing
    is configured (channels then give instructions without a link).
    """
    for var in ("LIONCLAW_PUBLIC_URL", "LIONCLAW_DASHBOARD_URL"):
        val = os.environ.get(var, "").strip()
        if val:
            return val.rstrip("/")
    port = os.environ.get("LIONCLAW_DASHBOARD_PORT", "").strip()
    if port.isdigit():
        return f"http://localhost:{port}"
    return None


def _providers_deeplink(action: dict[str, Any] | None) -> str | None:
    base = dashboard_url()
    if not base:
        return None
    return f"{base}/providers"


# ── Remediation builders ─────────────────────────────────────────────────────


def remediation_for_auth(provider: str | None) -> Remediation:
    """Remediation for an expired/invalid credential, by provider kind."""
    if _is_oauth_provider(provider):
        name = _provider_name(provider)
        return Remediation(
            kind=REMEDY_RECONNECT,
            provider=provider,
            summary=f"Reconnect {name} in the dashboard → Providers.",
            action={
                "type": REMEDY_RECONNECT,
                "provider": provider,
                "auth_type": "device_code",
                "label": f"Reconnect {name}",
            },
        )
    env_var = _provider_env_var(provider)
    name = _provider_name(provider)
    if env_var:
        summary = f"Set a valid {env_var} in the dashboard → Providers (or the vault)."
    else:
        summary = f"Update {name}'s credentials in the dashboard → Providers."
    return Remediation(
        kind=REMEDY_SET_KEY,
        provider=provider,
        env_var=env_var,
        summary=summary,
        action={
            "type": REMEDY_SET_KEY,
            "provider": provider,
            "env_var": env_var,
            "label": "Open Providers",
        },
    )


def _billing_url(provider: str | None) -> str | None:
    if provider == "anthropic":
        return "https://console.anthropic.com/settings/billing"
    if provider in ("openai", "openai-codex"):
        return "https://platform.openai.com/account/billing"
    if provider == "deepseek":
        return "https://platform.deepseek.com/top_up"
    return None


def deployment_card_url() -> str:
    """Where this box's credits are bought on cloud.olano.ai: the deployment's
    card for a managed box, the account's Billing page for a self-hosted box
    running on a pasted account key, the site root when neither is known."""
    base = "https://cloud.olano.ai"
    try:
        from olano.deployment.sources import cloud_api_url, read_manifest

        base = cloud_api_url()
        manifest = read_manifest()
        if manifest and manifest.deployment_id:
            return f"{base}/app/deployments/{manifest.deployment_id}"
        if _gateway_key_source() == "account":
            return f"{base}/app/billing"
    except Exception:
        pass
    return base


def _note_account_key_rejected() -> None:
    try:
        from olano.deployment.gateway_key import note_account_key_rejected

        note_account_key_rejected()
    except Exception:  # pragma: no cover - bookkeeping must never fail a classification
        pass


def _gateway_key_source() -> str | None:
    """Which key family serves the gateway on this box (lazy, never raises)."""
    try:
        from olano.deployment.gateway_key import gateway_key_source

        return gateway_key_source()
    except Exception:
        return None


# The gateway's router-miss error names the group it could not resolve, e.g.
# "Invalid model name passed in model=olano-vision". Pulling that name out is
# what lets the message say *which* managed service is missing rather than "a
# model" — the difference between a diagnosable report and a shrug.
# Must not end on punctuation: the name is usually followed by the sentence's
# full stop ("…model=olano-vision. Call `/v1/models`…"), and capturing that
# trailing "." makes every lookup below miss, degrading the message to the
# useless "this service".
_UNROUTED_MODEL_RE = re.compile(r"model\s*=\s*([A-Za-z0-9_.:-]*[A-Za-z0-9])")

# Human names for the managed groups this box can ask for. Tiers are handled
# separately (they resolve through `normalize_tier`), so only the utility and
# metered groups need spelling out.
_MANAGED_GROUP_LABELS: dict[str, str] = {
    "olano-vision": "image understanding",
    "olano-image": "image generation and editing",
    "olano-tts": "speech synthesis",
    "olano-stt": "audio transcription",
}


def _managed_group_label(wire_name: str) -> str:
    """Human name for a wire group: ``olano-vision`` → "image understanding"."""
    known = _MANAGED_GROUP_LABELS.get(wire_name.lower())
    if known:
        return known
    suffix = wire_name.partition("-")[2] if wire_name.lower().startswith("olano-") else ""
    try:
        from olano.models.olano_gateway import normalize_tier

        tier = normalize_tier(suffix)
    except Exception:  # pragma: no cover - defensive
        tier = None
    return f"the {tier} tier" if tier else "this service"


def _classify_olano_unrouted(msg: str, matched: str) -> ActionableError | None:
    """The gateway has no model group by the name we sent.

    Checked BEFORE the auth rule below, because the gateway's router-miss text
    ends in "…to view available models for your key", close enough to the
    generic key/auth needles to be misfiled as "your API key is wrong", which
    would send the reader hunting for a credential that is perfectly fine.

    A box cannot cause this. Wire names are hardcoded here (`olano-vision`,
    `olano-pro`, …) and the gateway resolves them from the cloud routing table,
    so an unrouted name means a group we depend on was never published.
    """
    if not _has(msg, "invalid model name", "model_not_found", "model not found"):
        return None
    match = _UNROUTED_MODEL_RE.search(matched)
    wire = match.group(1) if match else ""
    label = _managed_group_label(wire) if wire else "this service"
    named = f" (it serves no `{wire}` model group)" if wire else ""
    url = deployment_card_url()
    return ActionableError(
        CODE_OLANO_UNROUTED,
        f"Olano-managed {label} is not available at the model gateway right "
        f"now{named}. This is an Olano-side routing problem — no API key or "
        "setting on this deployment can fix it, and your own provider (BYOK) "
        "models are unaffected.",
        remediation=Remediation(
            kind=REMEDY_CONTACT,
            provider="olano",
            summary=(
                "Nothing to change here. Run `olano models gateway --probe` to see "
                "which managed services this deployment can reach, and check your "
                f"deployment card at cloud.olano.ai if it does not recover. ({url})"
            ),
            url=url,
            action={"type": "open_url", "url": url, "label": "Open deployment card"},
        ),
        detail=matched,
    )


# The gateway client stamps the wire group it called on the exception on its way
# out (``olano.models.olano_gateway.GROUP_ATTR``). Read that FIRST: the call site
# knows the name exactly, and guessing it from provider prose degrades a real
# outage to the useless "This service (Olano-managed) is temporarily
# unavailable". A literal rather than an import, because every olano import in
# this module is lazy; ``tests/test_actionable_errors.py`` pins the two in sync.
_GROUP_ATTR = "olano_model_group"

# Fallback for a relayed error raised outside the gateway client (a probe, a
# re-wrapped worker error string). The gateway names the group it routed to in
# its error text ("Received Model Group=olano-turbo"). This is NOT the ``model=``
# shape `_UNROUTED_MODEL_RE` matches — that regex misses this one entirely. Same
# trailing-punctuation rule: the capture must not swallow the sentence's full
# stop, or every label lookup degrades to the useless "this service".
_MODEL_GROUP_RE = re.compile(r"model\s+group\s*=\s*([A-Za-z0-9_.:-]*[A-Za-z0-9])", re.I)

# Markers the gateway only ever adds once it has *routed* a request: the group
# it picked, the fallback list it had, and the router's exception namespace
# wrapping the provider's. Their presence proves the gateway accepted our key
# and the failure happened on the upstream leg, the one fact that separates an
# Olano supply problem from this deployment's key or credit balance.
_RELAY_MARKERS = ("received model group", "available model group fallbacks", "litellm.")

# 402 matched as a *status code*, never as three digits that happen to appear
# inside a larger number: a context-overflow message reading "you requested
# 14023 tokens" contains "402" as a substring, and a bare needle would file it
# as a billing problem. (The pre-existing 401/403/429 needles carry the same
# hazard; this one is new, so it is written tightly.)
_HTTP_402_RE = re.compile(r"(?:^|[^0-9])402(?![0-9])")


def _is_payment_refusal(msg: str) -> bool:
    """The account behind the call has no balance left, whoever owns it."""
    return _has(msg, "payment required", "insufficient balance", "insufficient funds") or bool(
        _HTTP_402_RE.search(msg)
    )


def _is_team_block(msg: str) -> bool:
    """The gateway refused because the org TEAM behind an account key is
    blocked: ``Team=<id> is blocked``. The cloud blocks the team while the
    account wallet is empty (the proxy's own team budget check is strict and
    would serve one more call at zero), so this is an out-of-credits refusal
    wearing different words."""
    return _has(msg, "is blocked") and _has(msg, "team=")


def _unattached_open_box() -> bool:
    """A self-hosted box with no deployment token (never a managed one)."""
    try:
        from olano.deployment import enforcement as dep_enforcement
        from olano.deployment.sources import deployment_token

        return dep_enforcement.deployment_posture().posture == "open" and not deployment_token()
    except Exception:
        return False


def _is_gateway_relayed(msg: str) -> bool:
    """True when the gateway routed the call and the *upstream* provider refused."""
    return _has(msg, *_RELAY_MARKERS)


def _deployment_id() -> str | None:
    """This deployment's id, when the box knows it (for a support subject line)."""
    try:
        from olano.deployment.sources import read_manifest

        manifest = read_manifest()
        return manifest.deployment_id if manifest else None
    except Exception:
        return None


def _support_mailto(subject: str) -> str:
    """A ``mailto:`` both the dashboard CTA and a channel message can use."""
    from urllib.parse import quote

    return f"mailto:{SUPPORT_CONTACT}?subject={quote(subject)}"


def _olano_credits_error(exc: Exception) -> ActionableError:
    """This box's own olano-credit balance is spent: the deployment's, or the
    account wallet behind a pasted Olano API key."""
    url = deployment_card_url()
    if _gateway_key_source() == "account":
        return ActionableError(
            CODE_OLANO_CREDITS,
            "Your Olano account is out of credits, so Olano-managed models are "
            "paused. Buy more credits on cloud.olano.ai (Billing → Account "
            "credits) to continue (BYOK models keep working).",
            remediation=Remediation(
                kind=REMEDY_TOP_UP,
                provider="olano",
                summary=f"Buy Olano credits for your account. ({url})",
                url=url,
                action={"type": "open_url", "url": url, "label": "Buy Olano credits"},
            ),
            detail=str(exc),
        )
    return ActionableError(
        CODE_OLANO_CREDITS,
        "This deployment is out of olano credits, so Olano-managed models "
        "are paused. Buy more credits on your deployment card at "
        "cloud.olano.ai to continue (BYOK models keep working).",
        remediation=Remediation(
            kind=REMEDY_TOP_UP,
            provider="olano",
            summary=f"Buy olano credits on your deployment card. ({url})",
            url=url,
            action={"type": "open_url", "url": url, "label": "Buy olano credits"},
        ),
        detail=str(exc),
    )


def _classify_olano_upstream(exc: Exception, msg: str, matched: str) -> ActionableError | None:
    """The gateway routed the call and the provider behind the tier refused it.

    The gateway relays an upstream refusal verbatim: the provider's own
    exception class, status code and wording, followed by ``Received Model
    Group=<group>``. Passed through, that text is wrong in two ways. It names
    nothing the reader can act on (their credits are fine and their box is
    healthy), and it leaks which provider backs the tier, which
    ``models/olano_gateway.py`` says must never be inferable from a user-facing
    surface. So this message names neither the provider nor the status code;
    the raw text still travels in ``detail`` for the logs.

    Checked ABOVE the auth rule below: a relayed upstream *authentication*
    failure carries the word "authentication" and would otherwise be filed as
    "this deployment's gateway access needed to be refreshed — retry", sending
    the reader into a retry loop against a problem no retry can fix.
    """
    if not _is_gateway_relayed(msg):
        return None
    wire = str(getattr(exc, _GROUP_ATTR, "") or "")
    if not wire:
        match = _MODEL_GROUP_RE.search(matched)
        wire = match.group(1) if match else ""
    label = _managed_group_label(wire) if wire else "this service"
    # Nothing on the box can fix an upstream account, so this log line is the
    # only signal the failure leaves.
    logger.warning(
        "Olano gateway relayed an upstream refusal for %s — the provider behind "
        "the tier rejected the call: %s",
        label,
        matched,
    )
    subject = f"Olano-managed model unavailable ({label})"
    deployment = _deployment_id()
    if deployment:
        subject = f"{subject} - deployment {deployment}"
    url = _support_mailto(subject)
    # The message says what broke; the remediation says what to do. They used to
    # say both, twice, and the mailto said it a third time — three paragraphs of
    # the same sentence in a chat bubble.
    return ActionableError(
        CODE_OLANO_UPSTREAM,
        f"{label[:1].upper()}{label[1:]} (Olano-managed) is temporarily "
        "unavailable — the request was refused upstream of this deployment. "
        "Nothing here is misconfigured: your olano credits are unaffected and "
        "your own provider (BYOK) models keep working.",
        remediation=Remediation(
            kind=REMEDY_CONTACT,
            provider="olano",
            summary=(f"Try again in a few minutes. If it persists, email {SUPPORT_CONTACT}."),
            url=url,
            action={"type": "open_url", "url": url, "label": "Email support"},
        ),
        # NOT retryable, even though the gateway client will try again a couple
        # of times before this message is ever built. A relayed *authentication*
        # failure lands in this same class, and no amount of retrying fixes an
        # upstream key — telling the reader to keep hammering would be the retry
        # loop the branch ordering above exists to prevent.
        detail=matched,
    )


def _classify_olano_gateway(exc: Exception, msg: str, matched: str) -> ActionableError | None:
    """Errors from the Olano model gateway (provider ``olano``).

    These never mean "fix your API key": the box holds no user-managed
    credential for managed tiers. A budget refusal means the deployment's
    olano-credit balance is spent (the gateway enforces the balance as the
    virtual key's max_budget); an auth failure means the per-deployment key was
    rotated/revoked — which the runtime heals by re-enrolling over the
    deployment token (``models/olano_gateway.py`` 401 hook), so the honest
    remedy is "retry", not a settings hunt.
    """
    unrouted = _classify_olano_unrouted(msg, matched)
    if unrouted is not None:
        return unrouted
    # Budget wording first: the gateway refuses on budget BEFORE it routes, so this
    # can never carry the relay markers the next rule keys on — and a genuine
    # credit exhaustion must never be re-labelled an Olano-side outage.
    if _has(
        msg,
        "budget has been exceeded",
        "exceeded budget",
        "exceededbudget",
        "budget_exceeded",
        "max budget",
        "max_budget",
    ):
        return _olano_credits_error(exc)
    upstream = _classify_olano_upstream(exc, msg, matched)
    if upstream is not None:
        return upstream
    # A payment refusal from the gateway ITSELF (no relay markers, so nothing was
    # routed) is this deployment's own balance — the same reading
    # `deployment/services_client.py` gives a 402 on the managed-services path.
    if _is_payment_refusal(msg) or _is_team_block(msg):
        return _olano_credits_error(exc)
    if _has(
        msg,
        "401",
        "unauthorized",
        "invalid_api_key",
        "invalid api key",
        "authentication",
        "api key",
    ):
        source = _gateway_key_source()
        if source is None and _unattached_open_box():
            # The model holds the unenrolled placeholder: no key was ever
            # connected, or it was removed. Nothing renews itself here, and
            # "retry in a few seconds" would be a lie on a loop.
            return ActionableError(
                CODE_OLANO_KEY_MISSING,
                "No Olano API key is connected on this box, so Olano-managed models "
                "cannot answer. Connect one on Providers → Olano (create it under "
                "API keys on cloud.olano.ai), or switch this agent to a provider model.",
                remediation=Remediation(
                    kind=REMEDY_SET_KEY,
                    provider="olano",
                    summary="Connect an Olano API key on Providers → Olano, or pick a provider model.",
                    action={
                        "type": REMEDY_SET_KEY,
                        "provider": "olano",
                        "env_var": None,
                        "label": "Open Providers",
                    },
                ),
                retryable=False,
                detail=str(exc),
            )
        if source == "account":
            # Nothing on this box can renew a pasted account key: it was revoked
            # on cloud.olano.ai (API keys page) or mistyped. "Retry" would be a
            # lie here; the fix is a person pasting a valid key. Record the
            # refusal here as well as in the 401 self-heal (which is throttled
            # to one attempt per window), so the Providers card reads
            # "refused" the moment the person sees this error.
            _note_account_key_rejected()
            return ActionableError(
                CODE_OLANO_KEY_INVALID,
                "Your Olano API key was not accepted by the Olano gateway. It may "
                "have been revoked on cloud.olano.ai, or pasted incorrectly. Open "
                "Providers → Olano and paste a valid key (create one under API keys "
                "on cloud.olano.ai).",
                remediation=Remediation(
                    kind=REMEDY_SET_KEY,
                    provider="olano",
                    summary="Paste a valid Olano API key on Providers → Olano.",
                    action={
                        "type": REMEDY_SET_KEY,
                        "provider": "olano",
                        "env_var": None,
                        "label": "Open Providers",
                    },
                ),
                retryable=False,
                detail=str(exc),
            )
        return ActionableError(
            CODE_AUTH_EXPIRED,
            "This deployment's gateway access needed to be refreshed. It "
            "renews automatically — send your message again in a few seconds. "
            "If this keeps happening, restart the runtime from your "
            "deployment card at cloud.olano.ai.",
            remediation=Remediation(
                kind=REMEDY_RETRY,
                provider="olano",
                summary="Retry in a few seconds (the gateway key re-enrolls itself).",
            ),
            retryable=True,
            detail=str(exc),
        )
    return None


# ── Classification ────────────────────────────────────────────────────────────


def _has(msg: str, *needles: str) -> bool:
    return any(n in msg for n in needles)


# Transport failures are the one error class that routinely carries NO message at
# all: ``str(httpx.ReadError())`` is the empty string, so every text-matching rule
# below misses and the generic fallback renders the exception's class name, so
# a chat answers a question with "Sorry, I encountered an error (AgentError).
# API error (ReadError):". Matching the TYPE NAME is therefore load-bearing
# here, not a nicety.
#
# Names, not `isinstance`, for the same reason the GraphRecursionError rule below
# uses one: httpx/aiohttp/openai are lazy imports in this stdlib-only module, and
# the same failure also arrives as re-wrapped TEXT (a `talk_to_agent` peer error,
# a cognition worker's error string) where no class survives at all — hence the
# needle list too.
_TIMEOUT_EXC_NAMES = frozenset(
    {
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "TimeoutException",
        "APITimeoutError",
        "ServerTimeoutError",
    }
)
_NETWORK_EXC_NAMES = frozenset(
    {
        # httpx
        "ConnectError",
        "ReadError",
        "WriteError",
        "CloseError",
        "NetworkError",
        "ProxyError",
        "RemoteProtocolError",
        "TransportError",
        # openai / anthropic SDKs
        "APIConnectionError",
        # aiohttp
        "ClientConnectorError",
        "ClientOSError",
        "ClientPayloadError",
        "ServerDisconnectedError",
        # stdlib
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "IncompleteRead",
    }
)

# Deliberately specific. A bare "connection" needle would file "connection to the
# database failed" here and send the reader off retrying a local problem.
_NETWORK_NEEDLES = (
    "connection error",
    "connection reset",
    "connection aborted",
    "connection closed",
    "connection broken",
    "server disconnected",
    "peer closed connection",
    "remote protocol error",
    "incomplete read",
    "response ended prematurely",
    "econnreset",
)


def _transient_transport_code(name: str, msg: str) -> str | None:
    """``CODE_TIMEOUT`` / ``CODE_NETWORK`` for a broken hop to the model, else None."""
    if name in _TIMEOUT_EXC_NAMES:
        return CODE_TIMEOUT
    if name in _NETWORK_EXC_NAMES or _has(msg, *_NETWORK_NEEDLES):
        return CODE_NETWORK
    return None


def classify_exception(
    exc: Exception, provider: str | None = None, *, text: str | None = None
) -> ActionableError | None:
    """Classify *exc* into an :class:`ActionableError`, or ``None`` if unknown.

    Returning ``None`` lets the caller fall back to its own generic handling
    (and to legacy provider-specific message rules).

    ``text`` overrides the string the rules match against. It exists for HTTP
    errors, where the meaning is in the response BODY and ``str(exc)`` carries
    only "Client error '400 Bad Request' for url …" — matching on that alone,
    every gateway refusal looks identical and none of them classify.
    """
    matched = text if text is not None else str(exc)
    msg = matched.lower()
    name = type(exc).__name__

    # 0. Olano-managed tiers first: their auth/billing errors are about the
    #    deployment's credit account and self-managed gateway key, and every
    #    generic rule below would misdiagnose them as user-key problems.
    if provider == "olano":
        olano = _classify_olano_gateway(exc, msg, matched)
        if olano is not None:
            return olano

    # 1. Expired / revoked sign-in — MUST come before the generic key/auth check
    #    so an OAuth token-invalidation isn't mistaken for a bad .env key.
    if _has(
        msg,
        "token_invalidated",
        "authentication token has been invalidated",
        "please try signing in again",
        "sign in again",
        "signin again",
        "invalid_grant",
        "refresh_token_reused",
        "relogin",
        "re-login",
        "session expired",
        "session has expired",
        "token has expired",
        "token expired",
        "expired token",
    ):
        pname = _provider_name(provider)
        if _is_oauth_provider(provider):
            message = (
                f"Your {pname} sign-in has expired, so the model can't be reached. "
                "Reconnect it to continue."
            )
        else:
            message = f"Your {pname} credentials are no longer valid. Refresh them to continue."
        return ActionableError(
            CODE_AUTH_EXPIRED, message, remediation=remediation_for_auth(provider), detail=str(exc)
        )

    # 2. Static API key invalid / missing.
    if _has(
        msg,
        "invalid_api_key",
        "incorrect api key",
        "no api key",
        "api key not valid",
        "invalid x-api-key",
        "missing api key",
        "api_key",
    ):
        pname = _provider_name(provider)
        message = f"The API key for {pname} is invalid or missing."
        return ActionableError(
            CODE_INVALID_KEY, message, remediation=remediation_for_auth(provider), detail=str(exc)
        )

    # 3. Generic auth failure (401/unauthorized/forbidden) — provider decides the
    #    remedy: OAuth providers re-sign-in, key providers set a key.
    if _has(msg, "unauthorized", "forbidden", "permission denied", "401", "403") or (
        "authentication" in msg
    ):
        pname = _provider_name(provider)
        if _is_oauth_provider(provider):
            message = f"Your {pname} sign-in is no longer authorized. Reconnect it to continue."
        else:
            message = f"Authentication with {pname} failed. Check its API key."
        return ActionableError(
            CODE_AUTH_EXPIRED if _is_oauth_provider(provider) else CODE_INVALID_KEY,
            message,
            remediation=remediation_for_auth(provider),
            detail=str(exc),
        )

    # 4. Credits / billing.
    if _has(msg, "credit balance", "credits"):
        url = _billing_url(provider)
        return ActionableError(
            CODE_CREDITS_LOW,
            "Your API credits are too low. Top up your provider billing to continue.",
            remediation=Remediation(
                kind=REMEDY_TOP_UP,
                provider=provider,
                summary="Top up your provider billing." + (f" ({url})" if url else ""),
                url=url,
                action={"type": "open_url", "url": url} if url else None,
            ),
            detail=str(exc),
        )
    if _has(msg, "insufficient_quota", "quota", "billing"):
        url = _billing_url(provider)
        return ActionableError(
            CODE_BILLING,
            "Your provider billing or quota limit was reached. Top up to continue.",
            remediation=Remediation(
                kind=REMEDY_TOP_UP,
                provider=provider,
                summary="Top up your provider billing." + (f" ({url})" if url else ""),
                url=url,
                action={"type": "open_url", "url": url} if url else None,
            ),
            detail=str(exc),
        )
    # A BYOK account that has simply run dry. DeepSeek says "Insufficient
    # Balance", which carries neither "quota" nor "credits" and so fell through
    # every rule above to the raw-exception fallback. Safe at this position: the
    # managed branch has already returned for `olano`, so nobody on a managed
    # tier is ever told to top up a provider account they do not have.
    if _is_payment_refusal(msg):
        url = _billing_url(provider)
        return ActionableError(
            CODE_BILLING,
            f"Your {_provider_name(provider)} account is out of balance. Top it up to continue.",
            remediation=Remediation(
                kind=REMEDY_TOP_UP,
                provider=provider,
                summary="Top up your provider billing." + (f" ({url})" if url else ""),
                url=url,
                action={"type": "open_url", "url": url} if url else None,
            ),
            detail=str(exc),
        )

    # 5. Transient conditions.
    if _has(msg, "rate_limit", "rate limit", "429"):
        return ActionableError(
            CODE_RATE_LIMIT,
            "Rate limit hit. Please wait a moment and try again.",
            remediation=Remediation(kind=REMEDY_RETRY, summary="Wait a few seconds and retry."),
            retryable=True,
            detail=str(exc),
        )
    if _has(msg, "overloaded", "529"):
        return ActionableError(
            CODE_OVERLOADED,
            "The model API is temporarily overloaded. Try again in a few seconds.",
            remediation=Remediation(kind=REMEDY_RETRY, summary="Try again shortly."),
            retryable=True,
            detail=str(exc),
        )
    # The hop to the model broke: a socket dropped mid-stream, a reset peer, a
    # body that ended early. Safe at this position — every credential, billing
    # and quota rule above has already had its say, and none of those failures
    # arrives as a transport exception. Retryable in the most literal sense:
    # the identical request usually succeeds on the next attempt, which is why
    # the message asks for exactly that and names nothing technical. The raw
    # exception still travels in ``detail`` for the logs.
    transport = _transient_transport_code(name, msg)
    if transport == CODE_NETWORK:
        return ActionableError(
            CODE_NETWORK,
            "Sorry, I had trouble processing that one — the connection dropped "
            "on my side. Could you send your message again?",
            remediation=Remediation(kind=REMEDY_RETRY, summary="Send the message again."),
            retryable=True,
            detail=str(exc) or name,
        )
    if transport == CODE_TIMEOUT:
        return ActionableError(
            CODE_TIMEOUT,
            "Sorry, that one took too long and timed out. Could you send your message again?",
            remediation=Remediation(kind=REMEDY_RETRY, summary="Send the message again."),
            retryable=True,
            detail=str(exc) or name,
        )
    # Local SQLite session store (threads.db) busy under concurrent writes. WAL +
    # busy_timeout make this rare, but if the wait is exhausted it is still just a
    # transient lock — never a credential or quota problem.
    if _has(msg, "database is locked", "database table is locked", "database is busy"):
        return ActionableError(
            CODE_DB_LOCKED,
            "The local session database is busy with concurrent writes. "
            "This is transient — please try again in a moment.",
            remediation=Remediation(kind=REMEDY_RETRY, summary="Wait a moment and retry."),
            retryable=True,
            detail=str(exc),
        )

    # 6. Request-shape problems.
    if _has(msg, "context_length", "too many tokens", "context window"):
        return ActionableError(
            CODE_CONTEXT,
            "Context window exceeded. Use /new (or compact the thread) to start fresh.",
            detail=str(exc),
        )
    if _has(msg, "timeout", "timed out"):
        # Same wording as the type-name branch in section 5 — one failure, one
        # sentence, whichever half of the exception carried the evidence.
        return ActionableError(
            CODE_TIMEOUT,
            "Sorry, that one took too long and timed out. Could you send your message again?",
            remediation=Remediation(kind=REMEDY_RETRY, summary="Send the message again."),
            retryable=True,
            detail=str(exc),
        )
    # The client could not represent a message already in the thread. Ollama is
    # the known offender (its converter knows four block kinds and raises on the
    # rest), which is why `models/ollama_compat.py` sanitizes ahead of it — this
    # rule is the backstop for whatever that shim does not cover, and for the
    # same class of refusal from another provider. Retrying is pointless: the
    # block is checkpointed, so only a fresh thread clears it.
    if _has(
        msg,
        "unsupported message content type",
        "only string image_url",
        "received unsupported message type",
        "invalid content block",
    ):
        # The chat stream calls `to_error_frame(exc)` with no provider, and
        # `_provider_name(None)` is the lowercase "the model provider" — fine
        # mid-sentence, where every other rule uses it, but this one opens with
        # it. Name the provider only when it is actually known.
        subject = _provider_name(provider) if provider else "This agent's model"
        return ActionableError(
            CODE_MESSAGE_FORMAT,
            (
                f"{subject} could not read part of this conversation's "
                "history — it contains a message block this model has no format for "
                "(usually left behind by a different model that ran on the same thread). "
                "Start a new thread to continue; retrying this one will fail the same way."
            ),
            # REMEDY_NONE, so `remediation_lines` never renders the summary as a
            # channel tail under a message that already gives the instruction.
            # The `action` is what the dashboard needs: `ErrorRemediation`
            # renders NOTHING for a remediation without one, so a summary alone
            # would have made this the only classified error in the product with
            # no affordance — the text says "start a new thread" and the UI
            # offers no way to. `new_thread` is handled in both chat shells and
            # degrades to a sentence where there is no chat to start (Cortex).
            remediation=Remediation(
                kind=REMEDY_NONE,
                provider=provider,
                summary="Start a new thread (/new in chat, or New chat in the dashboard).",
                action={"type": "new_thread"},
            ),
            detail=str(exc),
        )
    if _has(msg, "has no attribute 'operations'", "permissionmiddleware", "filesystempermission"):
        return ActionableError(
            CODE_FS_ONLY,
            (
                "This agent has filesystem-only access and cannot run shell commands. "
                "Change its Environment to 'filesystem+shell' to enable them."
            ),
            detail=str(exc),
        )

    # LangGraph step budget — "Recursion limit of N reached without hitting a
    # stop condition". Reached both as the live GraphRecursionError (chat /
    # runner) and as re-wrapped message TEXT (cognition routes worker error
    # strings through here via classify_cognition_error_full), so the message
    # match is load-bearing and the type name only a backstop. Without this
    # rule the raw LangGraph string — which tells users to "set the
    # `recursion_limit` config key", a key they cannot set — reached the UI
    # verbatim with no remediation.
    if name == "GraphRecursionError" or _has(msg, "recursion limit"):
        return ActionableError(
            CODE_STEP_BUDGET,
            (
                "The run hit its step limit before it could finish, so its work "
                "was discarded. Run it again — a retry often plans more tightly."
            ),
            remediation=Remediation(
                kind=REMEDY_RETRY,
                summary=(
                    "Run it again. If this keeps happening on Cortex/background "
                    "work, raise the step budget in Cortex → Autonomy — for this "
                    "agent, or for all agents."
                ),
            ),
            retryable=True,
            detail=str(exc),
        )

    return None


# ── Adapters for each surface ─────────────────────────────────────────────────


def _coerce(obj: Exception | ActionableError, provider: str | None = None) -> ActionableError:
    """Return an ActionableError for an AgentError (using its ``detail``), a raw
    exception, or an already-classified error."""
    if isinstance(obj, ActionableError):
        return obj
    detail = getattr(obj, "detail", None)
    if isinstance(detail, ActionableError):
        return detail
    classified = classify_exception(obj, provider) if isinstance(obj, Exception) else None
    if classified is not None:
        return classified
    return ActionableError(CODE_GENERIC, str(obj) or "Unexpected error.", detail=str(obj))


def to_error_frame(obj: Exception | ActionableError, provider: str | None = None) -> dict[str, Any]:
    """Payload for the dashboard SSE ``error`` frame (``type`` added by caller)."""
    err = _coerce(obj, provider)
    frame: dict[str, Any] = {"message": err.message, "code": err.code}
    if err.remediation is not None:
        frame["remediation"] = err.remediation.to_dict()
    return frame


def _is_followable_link(url: str) -> bool:
    """True for a link a chat client will render as a link and a reader can tap."""
    return url.startswith("http://") or url.startswith("https://")


def _normalize_for_compare(text: str) -> str:
    """Casefolded, whitespace- and punctuation-flattened, for containment checks."""
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def _restates(message: str, line: str) -> bool:
    """True when *line* adds nothing the message has not already said.

    Guards the shape that made one gateway outage read as three paragraphs of
    the same sentence: the message ended "Try again in a few minutes, and
    contact support@olano.ai", the remediation summary said "Try again in a few
    minutes; if it persists, email support@olano.ai", and the mailto spelled it
    out a third time. Exact containment after normalizing, never fuzzy matching —
    a remediation that says anything new must always survive.
    """
    body = line.lstrip("→ ").strip()
    if not body:
        return True
    return _normalize_for_compare(body) in _normalize_for_compare(message)


def channel_text_if_classified(
    obj: Exception | ActionableError, provider: str | None = None
) -> str | None:
    """:func:`channel_text` when the error was actually recognized, else ``None``.

    For surfaces that already carry their own generic wording — the platform
    adapters' "Sorry, I encountered an error…", the CLI's bare ``str(e)`` — so
    they can defer to the classifier when it knows something and keep their own
    text when it does not. Routing them through :func:`channel_text`
    unconditionally would trade a useful generic message for a bare exception
    string on every unclassified failure.
    """
    err = _coerce(obj, provider)
    if err.code == CODE_GENERIC:
        return None
    return channel_text(err)


def remediation_lines(obj: Exception | ActionableError, provider: str | None = None) -> list[str]:
    """The "how to fix it" lines alone, for a surface that prints the message itself.

    :func:`channel_text` renders message and remediation as one blob. The CLI
    already prints the message its own way (rich markup, its own colour), so it
    needs the tail only — and without it the terminal was the
    one surface where an actionable error's remediation went nowhere at all.
    """
    err = _coerce(obj, provider)
    rem = err.remediation
    if rem is None or rem.kind in (REMEDY_NONE, REMEDY_RETRY):
        return []
    lines = [f"→ {rem.summary}"] if rem.summary else []
    link = rem.url or _providers_deeplink(rem.action)
    # Only a link a reader can follow. A `mailto:` belongs on the dashboard's
    # remediation BUTTON, where the client opens it; pasted into a chat bubble it
    # renders as a wall of percent-encoding
    # ("mailto:…?subject=Olano-managed%20model%20unavailable%20%28this%20service%29…")
    # under a message that already gives the address in plain text.
    if link and _is_followable_link(link):
        lines.append(link)
    return lines


def channel_text(obj: Exception | ActionableError, provider: str | None = None) -> str:
    """A self-contained, user-friendly message for a chat channel.

    Includes what happened, how to fix it, and (when configured) a dashboard
    deep-link — because a channel can't render a popup.
    """
    err = _coerce(obj, provider)
    # The warning sign means "something here needs your attention": a dead key, a
    # spent balance, a thread that cannot continue. A failure that fixes itself on
    # the next message needs no siren — on a customer-facing channel it reads as
    # an alarm over what is really a "say that again?". So the transient class
    # (`retryable`: rate limit, overload, timeout, dropped connection, busy
    # session DB) is asked plainly, and everything else keeps the marker.
    lines = [err.message if err.retryable else f"⚠️ {err.message}"]
    tail = [line for line in remediation_lines(err) if not _restates(err.message, line)]
    if tail:
        lines.append("")
        lines.extend(tail)
    return "\n".join(lines)
