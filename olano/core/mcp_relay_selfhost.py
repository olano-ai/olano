"""Self-hosted boxes join the central MCP sign-in relay with their Olano API key.

A box Olano provisioned reaches the relay at ``https://auth.olano.ai`` with an
Ed25519 identity it enrolled over its deployment token
(:mod:`olano.core.mcp_relay_enroll`). A box someone installed themselves has no
deployment token, so until now its only MCP sign-in route without its own HTTPS
domain was running the reference relay. This module gives it the same relay,
enrolled with the **account API key** it already spends credits with
(``OLANO_CREDITS_KEY``, pasted under Providers -> Olano):

1. reuse the relay private key this box already holds, or mint one;
2. ``POST {cloud}/api/v1/account/relay-key`` with ``{public_key, instance_id}``
   and ``Authorization: Bearer sk-olano-...``;
3. only once the cloud accepted it, persist a newly minted private key (the
   same vault row the managed path uses, so ``mcp_relay.relay_private_key``
   finds it) and then the enrollment record (:data:`ENROLLMENT_VAULT_NAME`).

From then on :func:`enrolled_relay_url` answers the relay URL, which is what
``mcp_relay.relay_url()`` falls back to when nothing is configured, so
``resolve_relay_config()`` returns a usable config and every MCP sign-in rides
the relay. The relay only sees the opaque state and a short-lived code that is
useless without this box's PKCE verifier; tokens are exchanged and kept here.

What it deliberately does not do:

- **Managed boxes are untouched.** Eligibility goes through
  ``gateway_key.account_key_if_unmanaged()``, the one predicate for "does this
  box run on an account key", which is ``None`` whenever a managed marker, a
  managed manifest or a deployment token is present. A managed box keeps its
  configured relay URL, its own enrollment and its byte-identical behaviour.
- **An operator's own choices win.** A configured relay URL other than the
  official one (``LIONCLAW_MCP_RELAY_URL`` / ``mcp_relay_url``: a self-hoster
  running the reference relay) disables all of this, and so does an HTTPS
  ``LIONCLAW_PUBLIC_URL``: a box with a real domain keeps its own callback.
- **Official connectors stay off.** The cloud records the identity as
  ``self_hosted`` and the relay refuses it on ``/connect/*``; the box never
  tries, because ``connect_relay`` reads only a configured URL and steps aside
  for an identity enrolled here (:func:`holds_selfhost_identity`). Google or
  Microsoft on a self-hosted box needs the operator's own OAuth app.
- **Boot never waits on it.** The runtime runs :func:`maybe_enroll` from a
  background loop (``RuntimeServer._selfhost_relay_loop``); it never raises,
  backs off on failure, re-enrolls daily so the cloud keeps the slot, and logs
  one actionable line per distinct failure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

OFFICIAL_RELAY_URL = "https://auth.olano.ai"
ENROLL_PATH = "/api/v1/account/relay-key"
ENROLL_TIMEOUT_SECONDS = 15.0
# Vault row (category ``system``, reserved in ``vault/reserved.py``): what this
# box enrolled, so later boots and every relay lookup can tell a current
# enrollment from a stale one without a network call. No secret in it, but it
# is the runtime's own bookkeeping, never an agent's to read or edit.
ENROLLMENT_VAULT_NAME = "OLANO_MCP_RELAY_SELFHOST"
ENROLLMENT_DESCRIPTION = (
    "Self-hosted MCP sign-in relay enrollment (instance, public key, relay URL)"
)
# The cloud retires an instance unseen for 30 days; a daily re-enrollment keeps
# a running box well inside that.
REENROLL_AFTER_SECONDS = 24 * 3600.0
_BACKOFF_BASE_SECONDS = 300.0
_BACKOFF_MAX_SECONDS = 6 * 3600.0

_lock = threading.Lock()
_next_attempt_at: float = float("-inf")  # monotonic
_consecutive_failures = 0
_last_logged: str | None = None
_wake = threading.Event()
_last_error: str | None = None


@dataclass(frozen=True)
class Enrollment:
    instance_id: str
    public_key: str
    relay_url: str
    account_key_digest: str
    enrolled_at: float

    def to_json(self) -> str:
        return json.dumps(
            {
                "instance_id": self.instance_id,
                "public_key": self.public_key,
                "relay_url": self.relay_url,
                "account_key_digest": self.account_key_digest,
                "enrolled_at": self.enrolled_at,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> Enrollment | None:
        try:
            raw = json.loads(text)
            return cls(
                instance_id=str(raw["instance_id"]),
                public_key=str(raw["public_key"]),
                relay_url=str(raw["relay_url"]),
                account_key_digest=str(raw["account_key_digest"]),
                enrolled_at=float(raw["enrolled_at"]),
            )
        except Exception:
            return None


def _digest(value: str) -> str:
    """A short digest of the account key: says *which* key enrolled, reveals nothing."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


# ── Eligibility ─────────────────────────────────────────────────────────────


def _has_https_public_url() -> bool:
    url = (os.environ.get("LIONCLAW_PUBLIC_URL") or "").strip().lower()
    return url.startswith("https://")


def _configured_relay_is_foreign() -> bool:
    """True when the operator pointed this box at a relay that is not Olano's."""
    from olano.core import mcp_relay

    configured = mcp_relay.configured_relay_url()
    if not configured:
        return False
    return configured.rstrip("/") != OFFICIAL_RELAY_URL


def eligible_account_key() -> str | None:
    """The account key to enroll with, or ``None`` when this box must not.

    ``None`` on a managed box (``account_key_if_unmanaged`` is the managed
    gate), with no key, with an operator-configured relay other than Olano's,
    or with an HTTPS ``LIONCLAW_PUBLIC_URL`` (the box's own callback wins).
    Never raises.
    """
    try:
        if _has_https_public_url() or _configured_relay_is_foreign():
            return None
        from olano.deployment.gateway_key import account_key_if_unmanaged

        return account_key_if_unmanaged()
    except Exception:
        return None


# ── The enrollment record ───────────────────────────────────────────────────


def _vault() -> Any | None:
    try:
        from olano.vault.resolver import get_vault

        vault = get_vault()
    except Exception:
        return None
    return vault if vault is not None and hasattr(vault, "set") else None


def stored_enrollment() -> Enrollment | None:
    vault = _vault()
    if vault is None:
        return None
    try:
        text = vault.get(ENROLLMENT_VAULT_NAME, touch=False)
    except Exception:
        return None
    return Enrollment.from_json(str(text)) if text else None


def _current_enrollment(account_key: str) -> Enrollment | None:
    """The stored enrollment when it still describes this box, else ``None``.

    Current means: same instance id, enrolled by the key the box holds now,
    and a relay private key is present. A different key (another account,
    or a rotated key) needs a fresh enrollment before the relay is used.
    """
    from olano.core import mcp_relay

    record = stored_enrollment()
    if record is None:
        return None
    if record.account_key_digest != _digest(account_key):
        return None
    if record.instance_id != mcp_relay.instance_id():
        return None
    if not mcp_relay.relay_private_key():
        return None
    return record


def enrolled_relay_url() -> str | None:
    """The relay URL this self-hosted box may use, or ``None``.

    ``mcp_relay.relay_url()`` asks this only when no relay is configured, so on
    a managed box (which always has one configured) it is never reached. Cheap
    and silent: an env read, the memoised account key, one vault read.
    """
    key = eligible_account_key()
    if not key:
        return None
    try:
        record = _current_enrollment(key)
    except Exception:
        return None
    return record.relay_url if record else None


def holds_selfhost_identity() -> bool:
    """True when the relay identity this box signs with was enrolled here.

    The cloud marks such an identity ``self_hosted`` and refuses it on every
    ``/connect/*`` route, so ``connect_relay`` uses this to report "no connect
    relay" up front instead of offering a Connect button that can only fail.
    """
    record = stored_enrollment()
    if record is None:
        return False
    try:
        from olano.core import mcp_relay

        private_key = mcp_relay.relay_private_key()
        if not private_key:
            return False
        return mcp_relay.public_key_b64(private_key) == record.public_key
    except Exception:
        return False


# ── Enrollment ──────────────────────────────────────────────────────────────


class _EnrollRefused(Exception):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code
        self.detail = detail


def _post_enrollment(account_key: str, public_key: str, instance_id: str) -> dict[str, Any]:
    """POST the public key. Returns the cloud's JSON; raises on any refusal."""
    import httpx

    from olano.deployment.sources import cloud_api_url

    resp = httpx.post(
        f"{cloud_api_url()}{ENROLL_PATH}",
        json={"public_key": public_key, "instance_id": instance_id},
        headers={"Authorization": f"Bearer {account_key}"},
        timeout=ENROLL_TIMEOUT_SECONDS,
    )
    if not 200 <= resp.status_code < 300:
        code = ""
        try:
            data = resp.json()
            if isinstance(data, dict):
                code = str(data.get("error") or "")
        except ValueError:
            pass
        raise _EnrollRefused(resp.status_code, code, (resp.text or "")[:200])
    data = resp.json()
    if not isinstance(data, dict) or not data.get("ok"):
        raise _EnrollRefused(resp.status_code, "unexpected_response")
    return data


def _new_instance_id() -> str | None:
    """Mint and persist a fresh instance id; ``None`` when the env pins one."""
    from olano.core import mcp_relay

    if (os.environ.get(mcp_relay._INSTANCE_ID_ENV) or "").strip():
        return None
    new_id = uuid.uuid4().hex[:16]
    try:
        from olano.config.home import update_home_config

        update_home_config({"instance_id": new_id})
    except Exception:
        return None
    return new_id


def _actionable(exc: BaseException) -> str:
    """The one line an operator needs, per failure kind."""
    if isinstance(exc, _EnrollRefused):
        if exc.status in (401, 403):
            return (
                "cloud.olano.ai refused this box's Olano API key for MCP sign-in. It may "
                "have been revoked; paste a live key under Providers -> Olano."
            )
        if exc.code == "instance_limit_reached":
            return (
                "this Olano account already has the maximum number of self-hosted boxes "
                "using MCP sign-in. Boxes unused for 30 days free their slot; revoking an "
                "API key frees the boxes it enrolled."
            )
        if exc.code == "instance_not_owned":
            return (
                "this box's instance id belongs to another deployment or account, and "
                "LIONCLAW_INSTANCE_ID pins it; unset it or choose a unique value."
            )
        if exc.status == 404:
            return (
                "cloud.olano.ai does not offer MCP sign-in enrollment to self-hosted boxes "
                "(the route answered 404). Check LIONCLAW_CLOUD_API_URL."
            )
        if exc.status == 429:
            return "cloud.olano.ai is rate limiting enrollment; will retry."
        return f"cloud.olano.ai answered HTTP {exc.status} ({exc.code or 'no detail'}); will retry."
    return f"could not reach cloud.olano.ai ({exc}); will retry."


def _note_failure(message: str) -> None:
    global _consecutive_failures, _next_attempt_at, _last_logged, _last_error
    _consecutive_failures += 1
    delay = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * 2 ** (_consecutive_failures - 1))
    _next_attempt_at = time.monotonic() + delay
    _last_error = message
    if message != _last_logged:
        _last_logged = message
        # A failed daily refresh of an enrollment that still stands changes
        # nothing for sign-ins today; only a box with no enrollment is stuck.
        try:
            enrolled = bool(enrolled_relay_url())
        except Exception:
            enrolled = False
        suffix = (
            ""
            if enrolled
            else " Until then, MCP sign-in needs LIONCLAW_PUBLIC_URL set to this box's "
            "HTTPS address."
        )
        logger.warning("MCP sign-in relay: %s%s", message, suffix)


def _note_success() -> None:
    global _consecutive_failures, _next_attempt_at, _last_logged, _last_error
    _consecutive_failures = 0
    _next_attempt_at = float("-inf")
    _last_logged = None
    _last_error = None


def enrollment_due() -> bool:
    """Should the loop try now? Cheap: no network."""
    key = eligible_account_key()
    if not key:
        return False
    if time.monotonic() < _next_attempt_at:
        return False
    try:
        record = _current_enrollment(key)
    except Exception:
        return True
    if record is None:
        return True
    return time.time() - record.enrolled_at >= REENROLL_AFTER_SECONDS


def maybe_enroll(*, force: bool = False) -> bool:
    """Enroll (or refresh) this self-hosted box with the MCP relay.

    Returns ``True`` when the cloud accepted an enrollment on this call. Never
    raises: any failure leaves the box exactly as it was (a previous enrollment
    keeps working) and schedules a backed-off retry.
    """
    with _lock:
        try:
            return _enroll_locked(force=force)
        except Exception as exc:  # pragma: no cover - never break the caller
            _note_failure(f"enrollment failed unexpectedly ({exc}); will retry.")
            return False


def _enroll_locked(*, force: bool) -> bool:
    from olano.core import mcp_relay
    from olano.core.mcp_relay_enroll import _mint_keypair

    key = eligible_account_key()
    if not key:
        return False
    if not force and not enrollment_due():
        return False

    vault = _vault()
    if vault is None:
        _note_failure(
            "the credentials vault is locked, so the relay key cannot be stored; "
            "unlock it and the box enrolls on its own."
        )
        return False

    existing = mcp_relay.relay_private_key()
    minted: str | None = None
    if existing:
        try:
            private_key, public_key = existing, mcp_relay.public_key_b64(existing)
        except ValueError as exc:
            _note_failure(f"the relay private key on this box is unusable ({exc}).")
            return False
    else:
        private_key, public_key = _mint_keypair()
        minted = private_key

    instance_id = mcp_relay.instance_id()
    try:
        try:
            data = _post_enrollment(key, public_key, instance_id)
        except _EnrollRefused as exc:
            # The id belongs to a managed deployment or another account (a home
            # copied from elsewhere, a key from a different account). Mint a
            # fresh id once; the old one is not ours to reclaim.
            if exc.code != "instance_not_owned":
                raise
            fresh = _new_instance_id()
            if fresh is None:
                raise
            instance_id = fresh
            data = _post_enrollment(key, public_key, instance_id)
    except Exception as exc:
        _note_failure(_actionable(exc))
        return False

    relay_url = str(data.get("relay_url") or OFFICIAL_RELAY_URL).strip().rstrip("/")
    if not relay_url.startswith("https://"):
        relay_url = OFFICIAL_RELAY_URL

    # Public key first, private key second, record last: the box never holds a
    # record (and so never points MCP sign-in at the relay) for a key the relay
    # has not accepted, nor a key the relay expects but the box lost.
    try:
        if minted is not None:
            vault.set(
                mcp_relay._RELAY_PRIVATE_KEY_ENV,
                minted,
                description="Ed25519 private key this instance signs MCP relay requests with",
                category="system",
            )
        record = Enrollment(
            instance_id=instance_id,
            public_key=public_key,
            relay_url=relay_url,
            account_key_digest=_digest(key),
            enrolled_at=time.time(),
        )
        vault.set(
            ENROLLMENT_VAULT_NAME,
            record.to_json(),
            description=ENROLLMENT_DESCRIPTION,
            category="system",
        )
    except Exception as exc:
        _note_failure(f"enrolled, but could not store the relay key in the vault ({exc}).")
        return False

    _note_success()
    logger.info(
        "MCP sign-in relay: this self-hosted box is enrolled (instance=%s, relay=%s, %s). "
        "MCP sign-ins now work without an HTTPS address of its own.",
        instance_id,
        relay_url,
        str(data.get("outcome") or "enrolled"),
    )
    return True


def request_enrollment() -> None:
    """Wake the loop now (a key was just pasted or removed)."""
    global _next_attempt_at
    _next_attempt_at = float("-inf")
    _wake.set()


def take_request() -> bool:
    """True once after :func:`request_enrollment` (clears the flag). Non-blocking."""
    if _wake.is_set():
        _wake.clear()
        return True
    return False


def status() -> dict[str, Any]:
    """Diagnostics: is this box on the self-hosted relay, and if not, why."""
    key = eligible_account_key()
    record = stored_enrollment()
    current = None
    if key:
        try:
            current = _current_enrollment(key)
        except Exception:
            current = None
    return {
        "eligible": bool(key),
        "enrolled": current is not None,
        "relay_url": current.relay_url if current else None,
        "instance_id": record.instance_id if record else None,
        "enrolled_at": record.enrolled_at if record else None,
        "last_error": _last_error,
    }


def _reset_for_tests() -> None:
    global _next_attempt_at, _consecutive_failures, _last_logged, _last_error
    _next_attempt_at = float("-inf")
    _consecutive_failures = 0
    _last_logged = None
    _last_error = None
    _wake.clear()
