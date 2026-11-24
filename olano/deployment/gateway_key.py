"""Resolve — and if necessary self-enroll — this deployment's model-gateway key.

Olano-managed tiers (``olano:auto|turbo|pro|max``) are served by the
Olano model gateway at ``gateway.olano.ai``, authenticated by a
**per-deployment virtual key**. That key is the only credential this box holds
for managed inference: it is budgeted (it can spend exactly the deployment's
olano-credit balance and not one credit more), rate-limited, and revocable from
the control plane — which is precisely what an Olano-owned provider key could
never be on customer-controlled hardware.

How the key reaches a box:

- **New deployments**: the provisioning worker mints one and cloud-init writes
  ``OLANO_GATEWAY_KEY`` (+ ``OLANO_GATEWAY_URL``) into ``/opt/olano/olano.env``.
  Boxes provisioned before the rename carry the same values as
  ``LIONCLAW_OLANO_GATEWAY_KEY`` / ``LIONCLAW_OLANO_GATEWAY_URL``, and nothing
  rewrites that file, so the legacy spellings are honoured indefinitely (env
  and vault alike; ``migrations/0025_gateway_vault_key_rename`` moves the vault
  rows once, the env fallback is permanent).
- **Existing deployments**: there is NO path that rewrites a live box's
  env, so the runtime pulls its own key over the one credential it already has
  — the deployment token — via ``POST /api/v1/deployments/gateway-key`` (the
  relay-key pattern, see :mod:`olano.core.mcp_relay_enroll`), and persists
  the plaintext to the credentials vault.

Enrolling with a **fresh nonce** is the *rotation* mechanism: the control plane
mints a new key and revokes the prior one, so a suspected-leaked key is dead the
moment anyone rotates. That is also the self-heal for a 401 from the gateway
(key revoked/rotated elsewhere): re-enroll, don't ask the user.

Resolution order everywhere: **env → vault → (enroll)**. The env var is in the
agent-shell scrub list (``security/shell_guard.py``) for the same reason the
deployment token is — an agent script must never read it.

── Three rules this module exists to enforce ────────────────────────────────

**1. A retry must not rotate.** Plaintext is returned exactly once and stored
nowhere, so a lost response would leave the box with nothing while the control
plane had already revoked the previous key, and a naive retry would do it again
on every attempt, with every ``olano:`` tier down throughout. Each enrollment
*episode* therefore carries a stable idempotency nonce, reused across retries
and regenerated only once an enrollment is confirmed received. Same nonce ⇒ the
control plane replays the same key. New nonce ⇒ genuine rotation.

**2. Failure must get cheaper, not stay constant.** A flat retry window means a
multi-hour control-plane degradation costs one blocking request every minute
forever. The window is exponential (60s → 2× → 30 min cap) and resets on
success.

**3. Enrollment must never block an agent.** On the runtime server a background
loop owns it (:func:`set_background_enrollment`), and the build path reads cache
only. The inline path stays for the CLI, which has no loop to defer to.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

GATEWAY_URL_ENV = "OLANO_GATEWAY_URL"
GATEWAY_KEY_ENV = "OLANO_GATEWAY_KEY"
# Vault marker: the digest of an env gateway key the control plane has revoked.
# Durable, because the fact has to survive the restart that would otherwise put
# the dead key straight back in front (see `_env_key_is_dead`).
GATEWAY_KEY_DEAD_MARKER = "OLANO_GATEWAY_KEY_REVOKED_DIGEST"
# The OTHER key family: an account API key a person mints on cloud.olano.ai
# (Billing → API keys) and pastes into a self-hosted box. It is a LiteLLM virtual
# key like the enrolled one, so `gateway_key()` can hand it to the same client,
# but it belongs to an account wallet rather than a deployment, is never a
# managed marker (posture resolution does not know it exists) and is read ONLY
# on an open box: a managed deployment keeps its enrolled key and never looks.
ACCOUNT_KEY_ENV = "OLANO_CREDITS_KEY"
# What every account key starts with on the wire (LiteLLM requires `sk-`).
ACCOUNT_KEY_PREFIX = "sk-olano-"
# The pre-rename spellings. Read after the canonical name, never written. Not
# left to `env_aliases`: that layer pairs OLANO_GATEWAY_KEY with
# LIONCLAW_GATEWAY_KEY, which was never a name, so the fallback has to be
# explicit here.
LEGACY_GATEWAY_URL_ENV = "LIONCLAW_OLANO_GATEWAY_URL"
LEGACY_GATEWAY_KEY_ENV = "LIONCLAW_OLANO_GATEWAY_KEY"
LEGACY_GATEWAY_KEY_DEAD_MARKER = "LIONCLAW_OLANO_GATEWAY_KEY_REVOKED_DIGEST"
LEGACY_NAMES: dict[str, str] = {
    GATEWAY_URL_ENV: LEGACY_GATEWAY_URL_ENV,
    GATEWAY_KEY_ENV: LEGACY_GATEWAY_KEY_ENV,
    GATEWAY_KEY_DEAD_MARKER: LEGACY_GATEWAY_KEY_DEAD_MARKER,
}
# What each vault row says about itself. One place, because the enrollment
# write and the rename migration must file identical rows.
VAULT_ROW_DESCRIPTIONS: dict[str, str] = {
    GATEWAY_KEY_ENV: "Per-deployment virtual key for the Olano model gateway",
    GATEWAY_URL_ENV: "Olano model gateway URL (from enrollment)",
    GATEWAY_KEY_DEAD_MARKER: (
        "Digest of a revoked OLANO_GATEWAY_KEY env value, so it cannot shadow the enrolled key"
    ),
    ACCOUNT_KEY_ENV: "Olano account API key (cloud.olano.ai → API keys) this self-hosted box spends from",
}
VAULT_ROW_CATEGORY = "system"
DEFAULT_GATEWAY_URL = "https://gateway.olano.ai"
ENROLL_PATH = "/api/v1/deployments/gateway-key"
ENROLL_TIMEOUT_SECONDS = 10.0
# Bound for the best-effort "I have it, you can forget it" confirmation. Short
# because nothing depends on it: it only lets the control plane drop the
# plaintext from its replay memo ahead of that memo's own TTL.
CONFIRM_TIMEOUT_SECONDS = 3.0

# Placeholder api_key when no real key could be resolved at build time. Calls
# made with it 401 at the gateway, which routes users into the actionable-error
# + self-heal path instead of failing the whole agent build (an agent whose
# configured model can't boot is strictly worse than one whose first call
# errors with "retry in a moment").
UNENROLLED_SENTINEL = "olano-unenrolled"

# Exponential retry window, shared by every caller (agent builds, the background
# loop, the 401 self-heal). A dead cloud must cost one bounded POST per window,
# and the window must grow — see rule 2 above.
_BACKOFF_BASE_SECONDS = 60.0
_BACKOFF_MAX_SECONDS = 1800.0  # 30 min

_attempt_lock = threading.Lock()
_next_attempt_at: float = float("-inf")  # monotonic deadline
_consecutive_failures: int = 0

# The current enrollment episode's idempotency nonce. Survives across retries so
# the control plane replays rather than rotates (rule 1); cleared once an
# enrollment is confirmed received, so the NEXT enrollment is a real rotation.
_pending_nonce: str | None = None

# Diagnostics, surfaced by `enrollment_status()` → /api/diagnostics/gateway,
# the dashboard provider card and `admin/models.py`. Without these the only
# evidence of a failing enrollment was a WARNING in the process log, while every
# UI surface reported "connected".
_last_error: str | None = None
_last_attempt_wall: float | None = None
_last_success_wall: float | None = None
_sentinel_builds: int = 0

# Set by the runtime server while its enrollment loop is running. With a loop
# present, agent builds must never block on the network (rule 3).
_background_enrollment = False
_enroll_wanted = threading.Event()

# Process-memory copy of the last successfully enrolled key. The vault is the
# durable home, but a locked/read-only vault must NOT turn every agent build
# into a fresh enrollment: rotating churn kills the keys already handed to built
# models and 401s their next call. With this cache an unwritable vault costs one
# re-enroll per process lifetime, not one per build window.
_memory_key: str | None = None
_memory_url: str | None = None

# Digest of an env gateway key we watched the control plane revoke, loaded once
# per process from the vault. See `_env_key_is_dead` for why this has to exist.
_dead_env_digest: str | None = None
_dead_env_loaded = False

# The pasted account key, memoised in-process. It is reached from
# `load_speech_settings`, `load_system_settings` and `builtin_default_model_targets`,
# which run per build and per request, so a vault read per call would be the
# regression the config.yaml parse cache exists to prevent. A hit is held for
# the process (store/forget invalidate it); a miss is re-checked every
# `_ACCOUNT_MISS_TTL` seconds so a key written by another process (the CLI, a
# hand edit of .env) is picked up without a restart, and a hit every
# `_ACCOUNT_HIT_TTL` seconds so a key REPLACED from another process (the vault
# CLI, a sibling runtime sharing the home) is picked up too; the dashboard
# route resets the memo outright, so its own writes are immediate.
_ACCOUNT_MISS_TTL = 30.0
_ACCOUNT_HIT_TTL = 300.0
_account_lock = threading.Lock()
_account_key_memo: str | None = None
_account_key_loaded = False
_account_key_checked_at: float = float("-inf")
# Digest of an account key the gateway refused with a 401. The 401 self-heal
# cannot re-enroll a pasted key, so it records the refusal here for the
# Providers card and the error path; pasting a different key clears it.
_account_key_rejected_digest: str | None = None


def _env_value(name: str) -> tuple[str, str]:
    """``(spelling, value)`` for *name* from the environment: canonical, then legacy.

    The spelling comes back with the value so a message about the env file can
    name the line the operator will actually find there.
    """
    import os

    for spelling in (name, LEGACY_NAMES.get(name, "")):
        value = (os.environ.get(spelling) or "").strip() if spelling else ""
        if value:
            return spelling, value
    return name, ""


def _env_key() -> str:
    return _env_value(GATEWAY_KEY_ENV)[1]


def _vault_get_either(name: str) -> str | None:
    """A vault row under its canonical name, else under the pre-rename one."""
    got = _vault_get(name)
    if got:
        return got
    legacy = LEGACY_NAMES.get(name)
    return _vault_get(legacy) if legacy else None


def gateway_url() -> str:
    """Gateway origin: env → vault (persisted at enrollment) → memory → default."""
    env = _env_value(GATEWAY_URL_ENV)[1]
    if env:
        return env.rstrip("/")
    vaulted = _vault_get_either(GATEWAY_URL_ENV)
    if vaulted:
        return vaulted.rstrip("/")
    if _memory_url:
        return _memory_url.rstrip("/")
    return DEFAULT_GATEWAY_URL


def _key_digest(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _env_key_is_dead(env_value: str) -> bool:
    """Has the control plane revoked THIS env key?

    ── The bug this exists to make impossible ──────────────────────────────────

    Provisioning writes a freshly minted key into ``/opt/olano/olano.env``
    (``cloud_init.py``), and env has always outranked the vault — correctly, so
    an operator can drop a key in to unblock a box. But **every enrollment
    revokes the prior key**, and the prior key is usually that env value. So
    after any re-enrollment the box was in an unbreakable loop:

        build model with env key → 401 → self-heal enrolls a fresh key →
        persists it to the vault → next build reads env again → 401 → …

    Re-enrolling could never fix it, because the fix was never consulted. The
    box also looks healthy to every probe, since `gateway_key()` returns a
    non-empty string and `enrollment_state()` therefore says "enrolled" — so the
    background loop skips it too. Live symptom: "gateway access needed to be
    refreshed, it renews automatically" on every single turn, forever.

    The narrow fix: remember the digest of an env key we watched get revoked,
    and skip exactly that value. A *different* env key — an operator pasting a
    fresh one in — has a different digest and still wins, so the override that
    makes env the escape hatch keeps working. Only a key we have positive
    evidence is dead steps aside.

    Digest, never the key itself: this lands in the vault and in
    ``olano models gateway`` output.
    """
    global _dead_env_digest, _dead_env_loaded
    if not _dead_env_loaded:
        _dead_env_digest = _vault_get_either(GATEWAY_KEY_DEAD_MARKER)
        _dead_env_loaded = True
    return bool(_dead_env_digest) and _key_digest(env_value) == _dead_env_digest


def _mark_env_key_dead(env_value: str) -> None:
    """Record that this env key is revoked, in memory and in the vault."""
    global _dead_env_digest, _dead_env_loaded
    digest = _key_digest(env_value)
    _dead_env_digest = digest
    _dead_env_loaded = True
    env_name = _env_value(GATEWAY_KEY_ENV)[0]
    logger.warning(
        "olano gateway: the key in %s was revoked by the control plane; using the "
        "freshly enrolled key instead. Clear or update that env value in "
        "/opt/olano/olano.env when convenient — it is dead weight now.",
        env_name,
    )
    vault = _writable_vault()
    if vault is None:
        # Without the durable marker the next restart puts the dead key back in
        # front, and the loop resumes — so this is worth a loud line.
        logger.warning(
            "gateway key: vault not writable; the revoked %s will shadow the good "
            "key again after a restart",
            env_name,
        )
        return
    try:
        vault.set(
            GATEWAY_KEY_DEAD_MARKER,
            digest,
            description=VAULT_ROW_DESCRIPTIONS[GATEWAY_KEY_DEAD_MARKER],
            category=VAULT_ROW_CATEGORY,
        )
    except Exception as exc:
        logger.warning("gateway key: could not persist the revoked-env marker: %s", exc)


def openai_base_url() -> str:
    """The OpenAI-compatible base URL (``…/v1``) ChatOpenAI should point at."""
    url = gateway_url()
    return url if url.endswith("/v1") else f"{url}/v1"


def gateway_key() -> str | None:
    """The per-deployment gateway key: env → vault → memory. ``None`` when absent.

    With one exception, and it is the reason managed boxes could 401 forever:
    an env key that has been **proven revoked** steps aside. See
    :func:`_env_key_is_dead`.
    """
    env = _env_key()
    if env and not _env_key_is_dead(env):
        return env
    return _vault_get_either(GATEWAY_KEY_ENV) or _memory_key or _account_key_if_unmanaged()


def gateway_key_source() -> str | None:
    """Which family :func:`gateway_key` is serving: ``env`` (an operator's
    ``OLANO_GATEWAY_KEY``), ``deployment`` (the enrolled key), ``account`` (a
    pasted Olano API key on a self-hosted box) or ``None`` when there is none.
    Same resolution order as :func:`gateway_key`, so the two cannot disagree."""
    env = _env_key()
    if env and not _env_key_is_dead(env):
        return "env"
    if _vault_get_either(GATEWAY_KEY_ENV) or _memory_key:
        return "deployment"
    if _account_key_if_unmanaged():
        return "account"
    return None


_account_key_shape_warned: str | None = None


def _well_formed_account_key(value: str | None, where: str) -> str | None:
    """*value* when it can be an account key, else ``None`` (warned once per
    value). An account key always carries :data:`ACCOUNT_KEY_PREFIX`; anything
    else in the slot (a deployment key pasted in the wrong field, an empty
    export, garbage) must never reach the gateway's Authorization header,
    where it would 401 on every turn and read as a revoked account key."""
    global _account_key_shape_warned
    if not value:
        return None
    if value.startswith(ACCOUNT_KEY_PREFIX):
        return value
    digest = _key_digest(value)
    if _account_key_shape_warned != digest:
        _account_key_shape_warned = digest
        logger.warning(
            "ignoring %s from %s: an Olano API key starts with %r",
            ACCOUNT_KEY_ENV,
            where,
            ACCOUNT_KEY_PREFIX,
        )
    return None


def account_key() -> str | None:
    """The pasted Olano account API key, or ``None``. Env first (``OLANO_CREDITS_KEY``,
    mirrored from ``LIONCLAW_CREDITS_KEY`` by ``env_aliases``), then the vault.
    Memoised; see the module state for the policy. A value without the account
    prefix is not a key and reads as ``None``. This is the RAW slot: it says
    nothing about whether the box may spend from it. Every "does this box run
    on account credits" decision goes through :func:`account_key_if_unmanaged`
    (what :func:`gateway_key` serves), never through this function, so a stray
    variable on a managed box cannot flip a gate."""
    global _account_key_memo, _account_key_loaded, _account_key_checked_at
    _, env = _env_value(ACCOUNT_KEY_ENV)
    if env:
        return _well_formed_account_key(env, "the environment")
    with _account_lock:
        now = time.monotonic()
        if _account_key_loaded:
            age = now - _account_key_checked_at
            if _account_key_memo and age < _ACCOUNT_HIT_TTL:
                return _account_key_memo
            if not _account_key_memo and age < _ACCOUNT_MISS_TTL:
                return None
        _account_key_memo = _well_formed_account_key(_vault_get(ACCOUNT_KEY_ENV), "the vault")
        _account_key_loaded = True
        _account_key_checked_at = now
        return _account_key_memo


def _account_key_if_unmanaged() -> str | None:
    """The account key, but only on a box that is NOT a managed deployment.

    Three cheap, resolver-free checks, each env or a file stat, keep the managed
    path identical to before this family existed: a managed marker in the env,
    an installation manifest (managed or unreadable), or a deployment token
    means the box enrolls its own key and the account key is never read. The
    resolver itself is deliberately not consulted: it quarantines, prompts the
    vault and reads the network, none of which belongs inside a key lookup.
    """
    try:
        from olano.deployment import sources

        if sources.managed_markers():
            return None
        if sources.manifest_path().exists():
            manifest = sources.read_manifest()
            if manifest is None or manifest.managed:
                return None
        if sources.deployment_token():
            return None
    except Exception:
        return None
    return account_key()


def account_key_if_unmanaged() -> str | None:
    """The one predicate for "does this box run on an Olano account key".

    Every gate that turns managed defaults on (the ``olano:*`` tiers, media and
    speech defaults, the web services, the credits widgets) reads THIS, never
    :func:`account_key`: the raw slot answers on a managed box too, and a stray
    ``OLANO_CREDITS_KEY`` in a managed box's environment must change nothing
    there. Same answer as the account branch of :func:`gateway_key`."""
    return _account_key_if_unmanaged()


def reset_account_key_memo() -> None:
    """Forget the in-process memo (tests, and every write below)."""
    global _account_key_memo, _account_key_loaded, _account_key_checked_at
    with _account_lock:
        _account_key_memo = None
        _account_key_loaded = False
        _account_key_checked_at = float("-inf")


def store_account_key(key: str) -> None:
    """Persist a pasted account key to the vault and start serving it.

    Raises ``RuntimeError("vault_locked")`` when the vault cannot be written:
    unlike the enrolled key, there is no control plane to re-mint it from on
    the next boot, so a silent best-effort write would lose the key at the
    first restart. Clears a previous rejection mark: a new key is a new fact.
    """
    global _account_key_rejected_digest
    text = (key or "").strip()
    if not text:
        raise ValueError("empty key")
    vault = _writable_vault()
    if vault is None:
        raise RuntimeError("vault_locked")
    vault.set(
        ACCOUNT_KEY_ENV,
        text,
        description=VAULT_ROW_DESCRIPTIONS[ACCOUNT_KEY_ENV],
        category=VAULT_ROW_CATEGORY,
    )
    with _account_lock:
        _account_key_rejected_digest = None
    reset_account_key_memo()


def forget_account_key() -> bool:
    """Remove the pasted account key from the vault. True when a row was removed.
    Raises ``RuntimeError("vault_locked")`` when the vault cannot be written."""
    global _account_key_rejected_digest
    vault = _writable_vault()
    if vault is None:
        raise RuntimeError("vault_locked")
    removed = False
    try:
        removed = bool(vault.delete(ACCOUNT_KEY_ENV))
    except Exception as exc:
        logger.warning("account key: vault delete failed: %s", exc)
    with _account_lock:
        _account_key_rejected_digest = None
    reset_account_key_memo()
    return removed


def note_account_key_rejected() -> None:
    """Record that the gateway refused the account key currently held (401)."""
    global _account_key_rejected_digest
    key = account_key()
    if not key:
        return
    with _account_lock:
        _account_key_rejected_digest = _key_digest(key)


def account_key_rejected() -> bool:
    """True when the account key currently held is the one the gateway refused."""
    key = account_key()
    if not key:
        return False
    with _account_lock:
        digest = _account_key_rejected_digest
    return bool(digest) and _key_digest(key) == digest


def account_key_rejection_pending() -> bool:
    """Cheap: is any refusal recorded? (No vault read; the hot path asks this
    before paying for :func:`clear_account_key_rejection`.)"""
    with _account_lock:
        return _account_key_rejected_digest is not None


def clear_account_key_rejection() -> None:
    """The gateway accepted the held account key: the refusal is history.

    A 401 marks the key (``note_account_key_rejected``); nothing cleared the
    mark before this, so one transient gateway auth blip left the Providers
    card reading "refused" for the life of the process while every turn
    worked. Called from the gateway chat class after a successful call."""
    global _account_key_rejected_digest
    with _account_lock:
        _account_key_rejected_digest = None


def ensure_gateway_key() -> str | None:
    """Resolve the key, enrolling when the box has none.

    With a background enrollment loop running this NEVER touches the network —
    it nudges the loop and returns whatever is cached, so a degraded control
    plane can't put a 10-second stall inside an agent build or turn (rule 3).
    Without a loop (the CLI) it enrolls inline, throttled by the backoff window.
    """
    key = gateway_key()
    if key:
        return key
    if _background_enrollment:
        _enroll_wanted.set()
        return None
    return enroll_gateway_key()


def refresh_gateway_key() -> str | None:
    """Force a re-enrollment. The 401 self-heal.

    Bypasses the backoff window: a 401 is positive evidence the current key is
    dead, which is exactly when waiting is wrong. It does NOT force a rotation —
    if an enrollment episode is already pending, this retries that episode and
    replays its key rather than minting another (rule 1). The caller
    (``models/olano_gateway._spawn_self_heal``) has its own 60s guard, so this
    cannot become a hot loop.
    """
    return enroll_gateway_key(force=True)


def enroll_gateway_key(*, force: bool = False) -> str | None:
    """Pull a virtual key from the control plane and persist it.

    Authenticated by the deployment token; returns the plaintext key, or
    ``None`` when the box has no cloud channel, the backoff window applies,
    or the call failed (all logged, never raised — a gateway outage must not
    break an agent build).
    """
    global _next_attempt_at, _consecutive_failures, _pending_nonce
    global _last_error, _last_attempt_wall, _last_success_wall

    if not force and gateway_key():
        return gateway_key()

    from olano.deployment.sources import cloud_api_url, deployment_token

    token = deployment_token()
    if not token:
        logger.debug("gateway key: no deployment token; cannot enroll")
        return None

    with _attempt_lock:
        now = time.monotonic()
        if not force and now < _next_attempt_at:
            logger.debug(
                "gateway key: enrollment backing off (%.0fs remaining, %d consecutive failures)",
                _next_attempt_at - now,
                _consecutive_failures,
            )
            return None
        # Reserve the next window BEFORE the request and count the attempt as
        # failed up front: if this process dies mid-call we have already backed
        # off, which is the safe direction. Success resets both below.
        _next_attempt_at = now + _backoff_for(_consecutive_failures)
        _consecutive_failures += 1
        _last_attempt_wall = time.time()
        # Same nonce for every retry of this episode ⇒ the control plane replays
        # the key it already minted instead of rotating (rule 1).
        if _pending_nonce is None:
            _pending_nonce = _new_nonce()
        nonce = _pending_nonce

    # Wall-clock + edge headers around the call. A failing enrollment is
    # diagnosed almost entirely from these two facts, and neither was recorded:
    #
    #   HOW LONG   — which timeout produced the failure. A 504 at ~5s is a
    #                bounded upstream refusing; at ~60s it is a proxy giving up
    #                on a handler that hung.
    #   WHO ANSWERED: an application-origin header present means the request
    #                reached the control plane's application, so the handler is
    #                where to look. Absent, with an edge request id, means the
    #                edge/CDN produced the error itself and the origin never saw
    #                the request: a completely different bug, and not one any
    #                amount of handler work would fix.
    #
    # Managed instances have no shell, so the runtime log is the only place an
    # operator can obtain this.
    started = time.monotonic()
    try:
        import httpx

        resp = httpx.post(
            f"{cloud_api_url()}{ENROLL_PATH}",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": nonce,
            },
            timeout=ENROLL_TIMEOUT_SECONDS,
        )
        _log_enrollment_response(resp, time.monotonic() - started)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        elapsed = time.monotonic() - started
        with _attempt_lock:
            _last_error = str(exc)
            retry_in = max(0.0, _next_attempt_at - time.monotonic())
            failures = _consecutive_failures
        logger.warning(
            "olano gateway key enrollment failed after %.1fs (%s); olano tiers "
            "stay unavailable — attempt %d, retrying in %.0fs",
            elapsed,
            exc,
            failures,
            retry_in,
        )
        return None

    key = str(body.get("key") or "").strip() if isinstance(body, dict) else ""
    if not key:
        with _attempt_lock:
            _last_error = "control plane returned no key"
        logger.warning("gateway key: control plane returned no key")
        return None

    url = str(body.get("gateway_url") or "").strip() if isinstance(body, dict) else ""

    # A successful mint REVOKES whatever key the registry held. If that was the
    # env value we have been presenting, it is now dead — and env outranks
    # everything, so without this the box would keep reading the revoked key and
    # 401ing no matter how often it re-enrolls. Marking it is the whole reason
    # this branch exists; see `_env_key_is_dead`.
    env_key = _env_key()
    if env_key and env_key != key:
        _mark_env_key_dead(env_key)

    # Memory first (unconditional), THEN the durable vault write — the order
    # guarantees the process serves this key even when the vault can't take it.
    global _memory_key, _memory_url
    _memory_key = key
    _memory_url = url or None
    _persist(key, url or None)

    with _attempt_lock:
        _consecutive_failures = 0
        _next_attempt_at = float("-inf")
        _last_error = None
        _last_success_wall = time.time()
        _pending_nonce = None
        _enroll_wanted.clear()

    replayed = bool(body.get("replayed")) if isinstance(body, dict) else False
    logger.info(
        "olano gateway key enrolled (url=%s%s)",
        url or DEFAULT_GATEWAY_URL,
        ", replayed" if replayed else "",
    )
    # Let the control plane drop the plaintext from its replay memo now that we
    # hold it, rather than at that memo's TTL. Best-effort by construction.
    _confirm_receipt(cloud_api_url(), token, nonce)
    return key


def enroll_via_settings_channel() -> str | None:
    """Pull the key over the settings **GET** — the channel that works when the
    enrollment POST does not.

    On at least one production box (workbuddy, 2026-08-16) the enrollment
    endpoint 504s for authenticated callers indefinitely while the settings
    poll — same host, same token — succeeds throughout. The control plane puts
    the key on the settings response (`gateway_delivery` names the outcome);
    adoption happens inside :func:`olano.deployment.sources.fetch_remote`. But
    the ordinary poller runs every ~15 minutes, which is a long time for a box
    whose every ``olano:`` turn is failing — so the enrollment loop calls this
    immediately after a failed direct attempt, turning the retry ladder for the
    broken POST into the retry ladder for the working GET.

    Best-effort by construction: returns the key this box now holds, or
    ``None``. Never raises — like every enrollment path, a dead cloud must not
    break anything.
    """
    from olano.deployment.sources import RemoteFetchError, cloud_settings_config, fetch_remote

    cfg = cloud_settings_config()
    if cfg is None:
        return None
    try:
        # A delivered key is adopted as a side effect (adopt_delivered_key);
        # the license text itself is the poller's business, not ours.
        fetch_remote(cfg)
    except RemoteFetchError as exc:
        logger.debug("gateway key: settings-channel fallback fetch failed: %s", exc)
    except Exception:  # pragma: no cover - defensive; must never raise
        logger.debug("gateway key: settings-channel fallback failed", exc_info=True)
    return gateway_key()


def current_key_digest() -> str | None:
    """sha256 of the key this box currently holds, or ``None`` when it has none.

    Sent on every settings poll so the control plane can tell whether this
    deployment needs a key handed to it. A digest, never the key.
    """
    key = gateway_key()
    return _key_digest_full(key) if key else None


def adopt_delivered_key(key: str, url: str | None = None) -> bool:
    """Take a gateway key the control plane delivered over the settings channel.

    This is the path that actually gets a key onto a box. Self-enrollment has
    its own endpoint, and on at least one production deployment that endpoint
    returned 504 for authenticated callers indefinitely while the settings poll
    — same host, same token — succeeded throughout. Rather than keep repairing
    the broken path, the control plane now puts the key on the working one, and
    this adopts it.

    Adopting resets the enrollment backoff and clears any pending episode: the
    box has what enrollment was trying to obtain, so continuing to retry would
    be pure noise. Returns False for an unusable value rather than raising —
    the settings channel is how a box stays governable and must never fail over
    a credential.
    """
    global _memory_key, _memory_url, _consecutive_failures, _next_attempt_at
    global _last_error, _last_success_wall, _pending_nonce

    text = (key or "").strip()
    if not text or text == UNENROLLED_SENTINEL:
        return False
    if text == gateway_key():
        return False  # already holding it; nothing to do and nothing to log

    # A delivered key supersedes a revoked env value the same way an enrolled
    # one does — otherwise the dead env key keeps winning and the delivery is
    # invisible (the 2026-08-14 shadowing bug, via a different door).
    env_key = _env_key()
    if env_key and env_key != text:
        _mark_env_key_dead(env_key)

    _memory_key = text
    _memory_url = url or None
    _persist(text, url or None)

    with _attempt_lock:
        _consecutive_failures = 0
        _next_attempt_at = float("-inf")
        _last_error = None
        _last_success_wall = time.time()
        _pending_nonce = None
        _enroll_wanted.clear()

    logger.info(
        "olano gateway key delivered by the control plane over the settings channel "
        "(url=%s); olano tiers are available again",
        url or DEFAULT_GATEWAY_URL,
    )
    # Push it into models that are ALREADY BUILT. Agents boot before a key
    # exists, so their models hold UNENROLLED_SENTINEL; persisting to the vault
    # only helps the next build. Without this the box has a working key and
    # every running agent still 401s until the process restarts — which is
    # precisely what "I updated and it still doesn't work" looks like.
    try:
        from olano.models.olano_gateway import swap_live_models

        swap_live_models(text)
    except Exception:  # pragma: no cover - never break the settings channel
        logger.debug("gateway key: could not apply the delivered key to live models", exc_info=True)
    return True


def _key_digest_full(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def note_sentinel_build(model_name: str) -> None:
    """Record that a model was built with :data:`UNENROLLED_SENTINEL`.

    Building with a known-dead key is the right call — an agent whose model
    can't boot is worse than one whose first call errors — but it must not be
    invisible, or the only symptom is a confusing 401 at first use with every
    dashboard surface still reading "connected".
    """
    global _sentinel_builds
    with _attempt_lock:
        _sentinel_builds += 1
        count = _sentinel_builds
    logger.warning(
        "olano gateway: building %r with an unenrolled placeholder key — calls "
        "will 401 until enrollment succeeds (%d model(s) so far; see "
        "GET /api/diagnostics/gateway)",
        model_name,
        count,
    )


def enrollment_state() -> str:
    """``enrolled`` | ``unenrolled`` | ``unattached``.

    The distinction that matters: ``unenrolled`` means the box IS attached to
    the control plane but holds no gateway key, so every ``olano:`` tier will
    401. Collapsing that into "connected" — which is what
    ``bool(gateway_key() or deployment_token())`` did — is how a total managed-
    inference outage rendered as a healthy provider card.
    """
    from olano.deployment.sources import deployment_token

    if gateway_key():
        return "enrolled"
    return "unenrolled" if deployment_token() else "unattached"


def enrollment_status() -> dict[str, object]:
    """Full enrollment diagnostics for the API, CLI and dashboard."""
    from datetime import UTC, datetime

    from olano.deployment.sources import deployment_token

    def _iso(wall: float | None) -> str | None:
        return datetime.fromtimestamp(wall, UTC).isoformat() if wall else None

    with _attempt_lock:
        failures = _consecutive_failures
        last_error = _last_error
        last_attempt = _last_attempt_wall
        last_success = _last_success_wall
        sentinel_builds = _sentinel_builds
        pending = _pending_nonce is not None
        retry_in = _next_attempt_at - time.monotonic()

    state = enrollment_state()
    return {
        "state": state,
        "attached": bool(deployment_token()),
        "has_key": state == "enrolled",
        "gateway_url": gateway_url(),
        "consecutive_failures": failures,
        "last_error": last_error,
        "last_attempt_at": _iso(last_attempt),
        "last_success_at": _iso(last_success),
        "retry_in_seconds": round(retry_in, 1) if failures and retry_in > 0 else None,
        "enrollment_pending": pending,
        "background_loop": _background_enrollment,
        "unenrolled_model_builds": sentinel_builds,
        # True when OLANO_GATEWAY_KEY (or its legacy spelling) holds a key the
        # control plane has revoked. The box is fine — it serves the enrolled
        # key instead — but that env value is dead weight and worth clearing,
        # and naming it here is what turns "gateway access keeps needing a
        # refresh" from a mystery into one line.
        "env_key_revoked": _env_key_shadowed(),
        # The spelling that line uses in /opt/olano/olano.env, so the operator
        # message can quote it exactly.
        "env_key_name": _env_value(GATEWAY_KEY_ENV)[0],
        # Which family the key comes from. ``account`` only ever appears on a
        # self-hosted box; a managed deployment reads ``deployment`` or ``env``.
        "source": gateway_key_source(),
        # True when the pasted account key is the one the gateway refused. The
        # fix is a person pasting a new one, which is why it is surfaced here
        # rather than retried.
        "account_key_rejected": account_key_rejected(),
    }


def _env_key_shadowed() -> bool:
    env = _env_key()
    return bool(env) and _env_key_is_dead(env)


def set_background_enrollment(active: bool) -> None:
    """Declare that a background loop owns enrollment (runtime server only).

    While set, :func:`ensure_gateway_key` never blocks on the network — it
    signals the loop and returns cache. The CLI leaves this off and keeps the
    inline path, because it has no loop to defer to.
    """
    global _background_enrollment
    _background_enrollment = active
    if not active:
        _enroll_wanted.clear()


def wait_for_enrollment_request(timeout: float) -> bool:
    """Block up to ``timeout`` for someone to ask for a key. The loop's pacer.

    Returns True when an agent build actually wanted a key, so the loop can act
    immediately instead of waiting out its poll interval.
    """
    got = _enroll_wanted.wait(timeout)
    _enroll_wanted.clear()
    return got


def enrollment_due() -> bool:
    """Whether an enrollment attempt would be allowed right now."""
    with _attempt_lock:
        return time.monotonic() >= _next_attempt_at


def _backoff_for(failures: int) -> float:
    """60s, 120s, 240s … capped at 30 min."""
    if failures <= 0:
        return _BACKOFF_BASE_SECONDS
    # Cap the exponent before computing it. The result is clamped to
    # _BACKOFF_MAX_SECONDS regardless, so the only thing an unbounded
    # `2**failures` buys is a multi-thousand-bit int (and, as a float, an
    # OverflowError) once a box has been failing for long enough. 2**16 already
    # exceeds the cap by three orders of magnitude.
    steps = min(failures, 16)
    return min(_BACKOFF_BASE_SECONDS * float(2**steps), _BACKOFF_MAX_SECONDS)


def _log_enrollment_response(resp: object, elapsed: float) -> None:
    """Log who answered an enrollment attempt, and how fast. Never raises.

    Only logs at WARNING for a non-2xx — a healthy enrollment says its piece in
    the success line and does not need a second one.
    """
    try:
        status = int(getattr(resp, "status_code", 0) or 0)
        if 200 <= status < 300:
            return
        headers = getattr(resp, "headers", {}) or {}
        # Platform/edge headers: the first proves the response came from the
        # application rather than a CDN or captive portal, the second is the
        # edge's own request id.
        origin = headers.get("x-do-app-origin")
        ray = headers.get("cf-ray")
        # The distinction that matters: did the request reach the app at all?
        who = (
            f"reached the app (origin {origin})"
            if origin
            else ("never reached the app — the CDN answered" if ray else "unknown responder")
        )
        logger.warning(
            "olano gateway enrollment: HTTP %s after %.1fs — %s%s",
            status,
            elapsed,
            who,
            f" [cf-ray {ray}]" if ray else "",
        )
    except Exception:  # pragma: no cover - diagnostics must never raise
        logger.debug("gateway key: could not describe the enrollment response", exc_info=True)


def _new_nonce() -> str:
    import uuid

    return uuid.uuid4().hex


def _confirm_receipt(api_url: str, token: str, nonce: str) -> None:
    """Tell the control plane it can forget this nonce's plaintext. Never raises."""
    try:
        import httpx

        httpx.request(
            "DELETE",
            f"{api_url}{ENROLL_PATH}",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": nonce},
            timeout=CONFIRM_TIMEOUT_SECONDS,
        )
    except Exception:
        # The memo expires on its own; this is an optimization, not a contract.
        logger.debug("gateway key: receipt confirmation failed", exc_info=True)


def _persist(key: str, url: str | None) -> None:
    """Best-effort vault write. A read-only vault costs a re-enroll next boot —
    the control plane mints/revokes idempotently, so that is safe, just noisy."""
    vault = _writable_vault()
    if vault is None:
        logger.warning(
            "gateway key: vault not writable; the key will be re-enrolled on the next restart"
        )
        return
    try:
        vault.set(
            GATEWAY_KEY_ENV,
            key,
            description=VAULT_ROW_DESCRIPTIONS[GATEWAY_KEY_ENV],
            category=VAULT_ROW_CATEGORY,
        )
        if url:
            vault.set(
                GATEWAY_URL_ENV,
                url,
                description=VAULT_ROW_DESCRIPTIONS[GATEWAY_URL_ENV],
                category=VAULT_ROW_CATEGORY,
            )
    except Exception as exc:
        logger.warning("gateway key: vault persist failed: %s", exc)


def _vault_get(name: str) -> str | None:
    try:
        from olano.vault.resolver import get_vault

        got = get_vault().get(name, touch=False)
    except Exception:
        return None
    text = str(got).strip() if got else ""
    return text or None


def _writable_vault():
    try:
        from olano.vault.resolver import get_vault

        vault = get_vault()
    except Exception:
        return None
    return vault if vault is not None and hasattr(vault, "set") else None
