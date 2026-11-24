"""Effective Deployment Settings — the managed-installation state machine.

Resolution (deployment-settings design record, §2):

- **Unmanaged** (no installation manifest): posture ``open`` — all-permissive,
  the only fail-open state in the system.
- **Managed-local**: a valid local license whose ``deployment_id`` matches the
  manifest, else **quarantine**. Deleting the license never reopens anything.
- **Managed-cloud**: the freshest accepted cloud artifact while its *signed*
  ``expires_at`` is in the future, else **quarantine**. A local license is
  ignored in cloud mode (mutual exclusivity). 401/403 from the control plane
  quarantines immediately (revocation); 429/5xx/network keep last-known-good
  until signed expiry. Lower ``policy_revision`` artifacts are rejected as
  rollback.

Public entry points never raise: :func:`get_effective_settings` and
:func:`deployment_status`. An injectable ``now`` keeps lifecycle behavior
deterministic under test.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from olano.deployment import sources
from olano.deployment.envelope import LicenseError, verify_license
from olano.deployment.schema import (
    OPEN_SETTINGS,
    QUARANTINE_SETTINGS,
    DeploymentSettings,
    SignedSettings,
)

logger = logging.getLogger(__name__)

# resolve() results are memoized briefly for hot call sites (per-request
# gates); anything that changes an input calls invalidate().
_RESOLVE_MEMO_TTL = 2.0


@dataclass
class DeploymentStatus:
    """Snapshot of the current entitlements state (never raises to build)."""

    posture: str  # "open" | "enforced" | "quarantine"
    source: str  # "none" | "local_license" | "cloud" | "cloud_cache" | "quarantine"
    settings: DeploymentSettings
    mode: str | None = None  # manifest mode ("local" | "cloud")
    deployment_id: str | None = None
    plan: str | None = None
    policy_revision: int | None = None
    issued_at: int | None = None
    expires_at: int | None = None
    refresh_after: int | None = None
    last_refresh: int | None = None  # epoch of last successful cloud fetch
    error: str | None = None

    @property
    def quarantined(self) -> bool:
        return self.posture == "quarantine"

    @property
    def enforced(self) -> bool:
        return self.posture in ("enforced", "quarantine")


@dataclass
class _CloudState:
    artifact: SignedSettings | None = None
    license_text: str | None = None
    last_fetch_ok: int | None = None
    revoked: bool = False
    last_error: str | None = None
    seeded: bool = False


@dataclass
class _ResolverState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    cloud: _CloudState = field(default_factory=_CloudState)
    memo: tuple[float, DeploymentStatus] | None = None


_STATE = _ResolverState()


def invalidate() -> None:
    """Drop memoized resolution + cloud seed (inputs changed on disk)."""
    global _broken_managed_logged
    with _STATE.lock:
        _STATE.memo = None
        _STATE.cloud = _CloudState()
        # Inputs changed — re-arm the broken-install error so a box that breaks
        # again (or was never repaired) says so again instead of staying quiet.
        _broken_managed_logged = False


def get_effective_settings(now: int | None = None) -> DeploymentSettings:
    return deployment_status(now=now).settings


def deployment_status(now: int | None = None) -> DeploymentStatus:
    """Resolve the current posture/settings. Never raises."""
    injected = now is not None
    ts = now if now is not None else int(time.time())
    with _STATE.lock:
        if not injected and _STATE.memo is not None:
            stamped, cached = _STATE.memo
            if time.monotonic() - stamped < _RESOLVE_MEMO_TTL:
                return cached
        try:
            status = _resolve_locked(ts)
        except Exception:
            logger.exception("deployment settings resolution failed")
            status = _fail_safe_status(ts)
        if not injected:
            _STATE.memo = (time.monotonic(), status)
        return status


SUPPORT_CONTACT = "support@olano.ai"

# Logged (loudly) and surfaced whenever a box that was PROVISIONED as managed
# cannot prove it. There is deliberately no fallback: the alternative to
# quarantine here is an open posture on a box holding the product source, which
# is the exact failure this layer exists to prevent.
_BROKEN_MANAGED_INSTALL = (
    "This deployment was provisioned as MANAGED (found: %s) but its installation "
    "manifest at %s is missing or unreadable, so its entitlements cannot be "
    "verified. Refusing to fall back to an unrestricted posture — the deployment "
    "is quarantined until this is repaired. If the runtime is containerized, the "
    "manifest directory must be mounted into the container (see "
    "deploy/docker-compose.allinone.yml). Contact " + SUPPORT_CONTACT + " for help."
)

# Set once the broken-install error has been logged (it resolves every ~2s).
_broken_managed_logged = False


def _open_or_broken(manifest_error: str) -> DeploymentStatus:
    """Resolve the no-usable-manifest case: open ONLY if provably unmanaged.

    A missing manifest means "unmanaged" only when nothing else on the box says
    it was provisioned as managed. If a managed marker IS present (see
    ``sources.managed_markers``) the manifest is missing for an *operational*
    reason — most often a containerized runtime that never got ``/etc/olano``
    mounted — and falling open would expose the product source and the vault.
    That case quarantines and says so, every time it resolves.
    """
    global _broken_managed_logged
    try:
        markers = sources.managed_markers()
    except Exception:  # marker detection must never decide "open" by accident
        logger.exception("managed-marker detection failed; assuming managed (fail closed)")
        markers = ["<marker detection failed>"]
    if not markers:
        return DeploymentStatus(posture="open", source="none", settings=OPEN_SETTINGS)
    # Resolution is memoized for ~2s, so an unconditional log here would spam.
    # Loud once per process; the posture itself stays quarantined for good and
    # the reason is always readable via GET /api/system/deployment.
    if not _broken_managed_logged:
        _broken_managed_logged = True
        logger.error(_BROKEN_MANAGED_INSTALL, ", ".join(markers), sources.manifest_path())
    return _quarantine(None, None, manifest_error)


def _fail_safe_status(now: int) -> DeploymentStatus:
    """Internal resolver error: open only if provably unmanaged, else quarantine."""
    try:
        manifest = sources.read_manifest()
    except Exception:
        # A manifest that exists but is unreadable ⇒ managed, fail closed.
        return _quarantine(None, None, "resolver_error")
    if manifest and manifest.managed:
        return _quarantine(manifest.mode, manifest.deployment_id, "resolver_error")
    return _open_or_broken("resolver_error")


def _quarantine(mode: str | None, deployment_id: str | None, error: str) -> DeploymentStatus:
    return DeploymentStatus(
        posture="quarantine",
        source="quarantine",
        settings=QUARANTINE_SETTINGS,
        mode=mode,
        deployment_id=deployment_id,
        error=error,
    )


def _resolve_locked(now: int) -> DeploymentStatus:
    try:
        manifest = sources.read_manifest()
    except sources.ManifestError as exc:
        logger.error("installation manifest invalid: %s", exc)
        return _quarantine(None, None, "invalid_manifest")

    if manifest is None or not manifest.managed:
        # NOT unconditionally open — a box carrying managed markers with no
        # usable manifest is a broken managed install, and must fail closed.
        return _open_or_broken("manifest_missing" if manifest is None else "manifest_not_managed")

    if manifest.mode == "local":
        return _resolve_local(manifest, now)
    return _resolve_cloud(manifest, now)


def _resolve_local(manifest: sources.InstallationManifest, now: int) -> DeploymentStatus:
    try:
        text = sources.load_local_license()
    except (ValueError, OSError) as exc:
        logger.error("local license unreadable: %s", exc)
        return _quarantine("local", manifest.deployment_id, "unreadable_license")
    if not text:
        return _quarantine("local", manifest.deployment_id, "missing_license")
    try:
        artifact = verify_license(
            text,
            expected_channel="local",
            expected_deployment_id=manifest.deployment_id,
            now=now,
        )
    except LicenseError as exc:
        logger.error("local license rejected (%s): %s", exc.code, exc)
        return _quarantine("local", manifest.deployment_id, exc.code)
    return DeploymentStatus(
        posture="enforced",
        source="local_license",
        settings=artifact.settings,
        mode="local",
        deployment_id=manifest.deployment_id,
        plan=artifact.plan,
        policy_revision=artifact.policy_revision,
        issued_at=artifact.issued_at,
        expires_at=artifact.expires_at,
        refresh_after=artifact.refresh_after,
    )


# ── Managed-cloud ────────────────────────────────────────────────────────────


def _resolve_cloud(manifest: sources.InstallationManifest, now: int) -> DeploymentStatus:
    state = _STATE.cloud
    if not state.seeded:
        _seed_from_cache(manifest, now)

    if state.revoked:
        return _quarantine("cloud", manifest.deployment_id, "revoked")

    artifact = state.artifact
    if artifact is None:
        return _quarantine("cloud", manifest.deployment_id, state.last_error or "no_artifact")
    if artifact.expires_at <= now:
        return _quarantine("cloud", manifest.deployment_id, "expired")

    fresh = state.last_fetch_ok is not None and (
        artifact.refresh_after is None or now <= artifact.refresh_after
    )
    return DeploymentStatus(
        posture="enforced",
        source="cloud" if fresh else "cloud_cache",
        settings=artifact.settings,
        mode="cloud",
        deployment_id=manifest.deployment_id,
        plan=artifact.plan,
        policy_revision=artifact.policy_revision,
        issued_at=artifact.issued_at,
        expires_at=artifact.expires_at,
        refresh_after=artifact.refresh_after,
        last_refresh=state.last_fetch_ok,
        error=state.last_error,
    )


def _seed_from_cache(manifest: sources.InstallationManifest, now: int) -> None:
    state = _STATE.cloud
    state.seeded = True
    text = sources.read_cache()
    if not text:
        return
    try:
        artifact = verify_license(
            text,
            expected_channel="cloud",
            expected_deployment_id=manifest.deployment_id,
            now=now,
        )
    except LicenseError as exc:
        # Expired cache still names the reason precisely; anything else is
        # a corrupt/foreign artifact.
        state.last_error = exc.code
        logger.warning("cached cloud settings rejected (%s): %s", exc.code, exc)
        return
    mark = sources.read_revision_mark()
    if mark is not None:
        mark_rev, mark_issued = mark
        if artifact.policy_revision < mark_rev or (
            artifact.policy_revision == mark_rev and artifact.issued_at < mark_issued
        ):
            state.last_error = "rollback"
            logger.error(
                "cached cloud settings are older than the recorded revision mark"
                " (cache rev %s < mark rev %s) — rejecting as rollback",
                artifact.policy_revision,
                mark_rev,
            )
            return
    state.artifact = artifact
    state.license_text = text


def _accept_candidate(
    manifest: sources.InstallationManifest, text: str, now: int
) -> tuple[bool, str | None]:
    """Verify + rollback-check a fetched artifact; persist on acceptance."""
    state = _STATE.cloud
    try:
        candidate = verify_license(
            text,
            expected_channel="cloud",
            expected_deployment_id=manifest.deployment_id,
            now=now,
        )
    except LicenseError as exc:
        return False, exc.code

    current = state.artifact
    floor_rev, floor_issued = -1, -1
    if current is not None:
        floor_rev, floor_issued = current.policy_revision, current.issued_at
    mark = sources.read_revision_mark()
    if mark is not None:
        mark_rev, mark_issued = mark
        if (mark_rev, mark_issued) > (floor_rev, floor_issued):
            floor_rev, floor_issued = mark_rev, mark_issued

    if candidate.policy_revision < floor_rev:
        logger.error(
            "fetched settings revision %s is below current %s — rejecting as rollback",
            candidate.policy_revision,
            floor_rev,
        )
        return False, "rollback"
    if candidate.policy_revision == floor_rev and candidate.issued_at < floor_issued:
        # Same revision, older signature: stale re-sign; keep what we have.
        state.last_fetch_ok = now
        state.revoked = False
        return True, None

    state.artifact = candidate
    state.license_text = text
    state.last_fetch_ok = now
    state.revoked = False
    state.last_error = None
    try:
        sources.write_cache(text, fetched_at=now)
        sources.write_revision_mark(candidate.policy_revision, candidate.issued_at)
    except OSError as exc:
        logger.warning("could not persist settings cache: %s", exc)
    return True, None


def _apply_fetch_error(exc: sources.RemoteFetchError) -> None:
    state = _STATE.cloud
    state.last_error = f"{exc.kind}: {exc}"
    if exc.kind == "revoked":
        state.revoked = True
        logger.error("control plane rejected the deployment token: %s", exc)
    else:
        logger.warning("settings fetch failed (%s); keeping last-known-good: %s", exc.kind, exc)


SETTLE_TIMEOUT_ENV = "LIONCLAW_SETTINGS_SETTLE_TIMEOUT"
DEFAULT_SETTLE_SECONDS = 45.0
SETTLE_POLL_SECONDS = 2.0


def settled(status: DeploymentStatus) -> bool:
    """Is this posture final for now, or merely "the artifact hasn't arrived yet"?

    A managed-cloud box resolves to **quarantine** from the moment it boots
    until its first signed settings artifact is fetched and accepted: not a
    verdict, just an absence. Everything that guards on the posture treats
    the two identically, which is right for a request handler and wrong for a
    one-shot decision made seconds after boot. The first-boot agent install
    is that decision -- it used to lose the race whenever the box came up
    faster than the fetch (a golden-image deploy does, reliably), and the
    designed recovery was "the next boot", which a running box never has.

    Settled means waiting would change nothing:

    * not quarantined at all (open, or enforced with settings);
    * quarantined for a reason a fetch cannot lift -- ``revoked`` (401/403),
      ``expired``, ``not_configured`` (no token), or a non-cloud mode (a local
      license is on disk or it isn't; a broken managed install has no manifest
      to fetch against).

    Unsettled is the transient set: ``no_artifact`` before the first fetch,
    and ``transient``/``invalid`` fetch failures that keep last-known-good.
    """
    if not status.quarantined:
        return True
    if status.mode != "cloud":
        return True
    err = status.error or ""
    return err == "expired" or err.startswith("revoked") or err.startswith("not_configured")


def settle_timeout() -> float:
    """Seconds a one-shot decision may wait for settings; ``0`` disables."""
    raw = os.environ.get(SETTLE_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_SETTLE_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_SETTLE_SECONDS


def wait_until_settled(
    timeout: float | None = None,
    *,
    poll: float = SETTLE_POLL_SECONDS,
    sleep: Any = time.sleep,
    clock: Any = time.monotonic,
) -> DeploymentStatus:
    """Refresh until :func:`settled` or ``timeout`` elapses; return the last status.

    Synchronous and blocking (it runs inside quick-setup, off the event loop
    on the first-boot path and in the CLI otherwise). Each iteration is a real
    fetch with its own network timeout, so the wall clock can overrun
    ``timeout`` by one fetch. Never raises: :func:`refresh` never does.
    """
    limit = settle_timeout() if timeout is None else max(0.0, timeout)
    status = refresh()
    if limit <= 0:
        return status
    deadline = clock() + limit
    while not settled(status):
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(poll, remaining))
        status = refresh()
    return status


def refresh(*, now: int | None = None) -> DeploymentStatus:
    """Fetch fresh settings (managed-cloud) and re-resolve. Never raises."""
    ts = now if now is not None else int(time.time())
    with _STATE.lock:
        _STATE.memo = None
        try:
            manifest = sources.read_manifest()
        except sources.ManifestError:
            manifest = None
        if manifest and manifest.managed and manifest.mode == "cloud":
            if not _STATE.cloud.seeded:
                _seed_from_cache(manifest, ts)
            cfg = sources.cloud_settings_config()
            if cfg is None:
                _STATE.cloud.last_error = "not_configured: no deployment token"
            else:
                try:
                    text = sources.fetch_remote(cfg)
                except sources.RemoteFetchError as exc:
                    _apply_fetch_error(exc)
                else:
                    accepted, code = _accept_candidate(manifest, text, ts)
                    if not accepted:
                        _STATE.cloud.last_error = code
    return deployment_status(now=now)


async def refresh_async(*, now: int | None = None) -> DeploymentStatus:
    """Async twin of :func:`refresh` (fetch off-lock, apply under lock)."""
    ts = now if now is not None else int(time.time())
    try:
        manifest = sources.read_manifest()
    except sources.ManifestError:
        manifest = None
    if manifest and manifest.managed and manifest.mode == "cloud":
        with _STATE.lock:
            _STATE.memo = None
            if not _STATE.cloud.seeded:
                _seed_from_cache(manifest, ts)
        cfg = sources.cloud_settings_config()
        if cfg is None:
            with _STATE.lock:
                _STATE.cloud.last_error = "not_configured: no deployment token"
        else:
            try:
                text = await sources.fetch_remote_async(cfg)
            except sources.RemoteFetchError as exc:
                with _STATE.lock:
                    _STATE.memo = None
                    _apply_fetch_error(exc)
            else:
                with _STATE.lock:
                    _STATE.memo = None
                    accepted, code = _accept_candidate(manifest, text, ts)
                    if not accepted:
                        _STATE.cloud.last_error = code
    return deployment_status(now=now)
