"""Where Deployment Settings come from: manifest, license file, cloud, cache.

Three inputs feed the resolver (:mod:`olano.deployment.resolver`):

1. The **installation manifest** — written by the installer/cloud-init as
   root, *outside* the olano home (``/etc/olano/installation.json``). Its
   presence is what makes an installation *managed*; deleting licenses or
   caches can therefore never return a managed install to the open posture.
2. The **local license** (managed-local): env inline → env path →
   ``<home>/deployment.lic``.
3. The **cloud channel** (managed-cloud): ``GET /api/v1/deployments/settings``
   on the control plane, authenticated by the per-deployment bearer token,
   plus an atomic on-disk cache of the last accepted artifact. All lifecycle
   decisions use the *signed* claims; the cache metadata is diagnostic only.

File reads here are defensive: symlinks and oversized files are rejected
(a hostile local user gains nothing, and a confused one gets a clear error).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from olano.deployment.envelope import MAX_ARTIFACT_BYTES

if TYPE_CHECKING:
    from olano.config.home import OlanoHomePaths

logger = logging.getLogger(__name__)

DEFAULT_MANIFEST_PATH = "/etc/olano/installation.json"
MANIFEST_ENV = "LIONCLAW_INSTALLATION_FILE"

LICENSE_ENV = "LIONCLAW_LICENSE"
LICENSE_PATH_ENV = "LIONCLAW_LICENSE_PATH"

CLOUD_URL_ENV = "LIONCLAW_CLOUD_API_URL"
DEPLOYMENT_TOKEN_ENV = "LIONCLAW_DEPLOYMENT_TOKEN"
DEFAULT_CLOUD_URL = "https://cloud.olano.ai"
SETTINGS_PATH = "/api/v1/deployments/settings"
FETCH_TIMEOUT_SECONDS = 10.0

CACHE_FILE = "settings.cache.lic"
CACHE_META_FILE = "settings.cache.meta.json"
REVISION_FILE = "revision.json"
MAX_MANIFEST_BYTES = 16 * 1024


@dataclass(frozen=True)
class InstallationManifest:
    """The bootstrap identity of a managed installation."""

    managed: bool
    mode: str  # "local" | "cloud"
    deployment_id: str


class ManifestError(Exception):
    """The manifest exists but cannot be trusted (parse/shape failure)."""


def manifest_path() -> Path:
    override = os.environ.get(MANIFEST_ENV)
    return Path(override) if override and override.strip() else Path(DEFAULT_MANIFEST_PATH)


def read_manifest() -> InstallationManifest | None:
    """Read the installation manifest, or ``None`` for unmanaged installs.

    Raises :class:`ManifestError` when a manifest exists but is unreadable or
    malformed — a managed box with a broken manifest must fail closed, not
    silently become unmanaged.
    """
    path = manifest_path()
    if not path.exists():
        return None
    try:
        raw = _read_small_file(path, max_bytes=MAX_MANIFEST_BYTES, allow_symlink=False)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ManifestError("installation manifest is not a JSON object")
        managed = bool(data.get("managed", False))
        if not managed:
            return InstallationManifest(managed=False, mode="", deployment_id="")
        mode = str(data.get("mode", ""))
        deployment_id = str(data.get("deployment_id", ""))
        if mode not in ("local", "cloud") or not deployment_id:
            raise ManifestError(
                f"installation manifest missing/invalid mode ({mode!r}) or deployment_id"
            )
        return InstallationManifest(managed=True, mode=mode, deployment_id=deployment_id)
    except ManifestError:
        raise
    except Exception as exc:
        raise ManifestError(f"cannot read installation manifest {path}: {exc}") from exc


def _read_small_file(path: Path, *, max_bytes: int, allow_symlink: bool = False) -> str:
    if not allow_symlink and path.is_symlink():
        raise ValueError(f"{path} is a symlink (refusing)")
    if path.stat().st_size > max_bytes:
        raise ValueError(f"{path} exceeds {max_bytes} bytes (refusing)")
    return path.read_text(encoding="utf-8")


# ── Local license ────────────────────────────────────────────────────────────


def load_local_license(paths: OlanoHomePaths | None = None) -> str | None:
    """The signed local license text: env inline → env path → ``<home>/deployment.lic``.

    Returns ``None`` when no license is present; raises ``ValueError`` for a
    present-but-unreadable one (symlink/oversized) so the resolver can report
    it as an invalid source rather than "missing".
    """
    inline = os.environ.get(LICENSE_ENV)
    if inline and inline.strip():
        return inline.strip()
    env_path = os.environ.get(LICENSE_PATH_ENV)
    if env_path and env_path.strip():
        p = Path(env_path.strip())
        if not p.exists():
            return None
        return _read_small_file(p, max_bytes=MAX_ARTIFACT_BYTES).strip()
    if paths is None:
        from olano.config.home import get_olano_paths

        paths = get_olano_paths()
    p = paths.deployment_license_path
    if not p.exists():
        return None
    return _read_small_file(p, max_bytes=MAX_ARTIFACT_BYTES).strip()


# ── Cloud channel ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CloudConfig:
    url: str  # base, no trailing slash
    token: str

    @property
    def settings_url(self) -> str:
        return f"{self.url}{SETTINGS_PATH}"


def cloud_api_url() -> str:
    env = os.environ.get(CLOUD_URL_ENV)
    return env.strip().rstrip("/") if env and env.strip() else DEFAULT_CLOUD_URL


def deployment_token() -> str | None:
    """The per-deployment bearer token: env → vault. Never config.yaml (secret)."""
    env = os.environ.get(DEPLOYMENT_TOKEN_ENV)
    if env and env.strip():
        return env.strip()
    try:
        from olano.vault.resolver import get_vault

        got = get_vault().get(DEPLOYMENT_TOKEN_ENV, touch=False)
    except Exception:
        return None
    text = str(got).strip() if got else ""
    return text or None


def cloud_settings_config() -> CloudConfig | None:
    """The cloud channel config, or ``None`` when no deployment token is set."""
    token = deployment_token()
    if not token:
        return None
    return CloudConfig(url=cloud_api_url(), token=token)


# ── "Was this box PROVISIONED as managed?" ───────────────────────────────────
# The manifest is the bootstrap identity, but it is a *file* — and a file can go
# missing for reasons that have nothing to do with the operator's intent (the
# classic one: the runtime runs in a container and /etc/olano was never mounted,
# which silently turned every managed box into an open one). Treating a
# missing manifest as "unmanaged" is only safe when NOTHING ELSE says otherwise.
#
# These markers are the "something else". They ride in the machine bootstrap env
# (/opt/olano/olano.env, written by cloud-init and loaded by config.bootstrap_env
# at every startup), so they reach the runtime through a completely different
# path than the manifest file does. When a marker is present but the manifest is
# not, the install is BROKEN, not open — and the resolver quarantines it.

MANAGED_FLAG_ENV = "LIONCLAW_DEPLOYMENT_MANAGED"
_TRUTHY = {"1", "true", "yes", "on"}


def managed_markers() -> list[str]:
    """Env markers proving this box was provisioned as a managed deployment.

    Returns the names of every marker found (empty ⇒ no evidence of managed
    provisioning). Deliberately env-only and side-effect free: this runs on the
    fail-safe path, so it must not touch the vault or the network.
    """
    found: list[str] = []
    flag = os.environ.get(MANAGED_FLAG_ENV, "").strip().lower()
    if flag in _TRUTHY:
        found.append(MANAGED_FLAG_ENV)
    # A deployment token is minted per-deployment by the control plane and only
    # ever written to a managed box. Read from the env only — deployment_token()
    # also consults the vault, which can raise/unlock-prompt on this path.
    if os.environ.get(DEPLOYMENT_TOKEN_ENV, "").strip():
        found.append(DEPLOYMENT_TOKEN_ENV)
    return found


class RemoteFetchError(Exception):
    """A settings fetch failed. ``kind`` drives the resolver's response:

    - ``revoked``   — 401/403: credential revocation, quarantine immediately.
    - ``transient`` — 429/5xx/network/timeout: keep last-known-good.
    - ``invalid``   — 200 but unusable body: treated like transient.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _classify_response(resp: httpx.Response) -> str:
    if resp.status_code in (401, 403):
        return "revoked"
    return "transient"


def _extract_license(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError as exc:
        raise RemoteFetchError("invalid", f"settings response is not JSON: {exc}") from exc
    license_text = body.get("license") if isinstance(body, dict) else None
    if not isinstance(license_text, str) or not license_text.strip():
        raise RemoteFetchError("invalid", "settings response has no 'license' field")
    if len(license_text) > MAX_ARTIFACT_BYTES:
        raise RemoteFetchError("invalid", "settings license exceeds size limit")
    return license_text.strip()


def _settings_headers(cfg: CloudConfig) -> dict[str, str]:
    """Auth, plus a statement of which gateway key this box currently holds.

    The digest is how the control plane knows whether to hand one over. Saying
    "none" is the normal state for a box that could never enroll, and it is what
    gets it a key on the very next poll — see `gateway_key.adopt_delivered_key`.
    Never the key itself: a sha256 of it, which is useless to anyone who
    intercepts it.
    """
    headers = {"Authorization": f"Bearer {cfg.token}"}
    try:
        from olano.deployment.gateway_key import current_key_digest

        headers["X-Olano-Gateway-Key-Digest"] = current_key_digest() or "none"
    except Exception:  # pragma: no cover - the settings channel must never break
        pass
    return headers


def fetch_remote(cfg: CloudConfig) -> str:
    """Fetch the signed settings artifact (sync). Raises :class:`RemoteFetchError`."""
    try:
        resp = httpx.get(
            cfg.settings_url,
            headers=_settings_headers(cfg),
            timeout=FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise RemoteFetchError("transient", f"settings fetch failed: {exc}") from exc
    if resp.status_code != 200:
        raise RemoteFetchError(
            _classify_response(resp), f"settings fetch returned HTTP {resp.status_code}"
        )
    _adopt_any_delivered_key(resp)
    return _extract_license(resp)


async def fetch_remote_async(cfg: CloudConfig) -> str:
    """Async twin of :func:`fetch_remote` (used by the runtime poller)."""
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                cfg.settings_url,
                headers=_settings_headers(cfg),
                follow_redirects=False,
            )
    except httpx.HTTPError as exc:
        raise RemoteFetchError("transient", f"settings fetch failed: {exc}") from exc
    if resp.status_code != 200:
        raise RemoteFetchError(
            _classify_response(resp), f"settings fetch returned HTTP {resp.status_code}"
        )
    _adopt_any_delivered_key(resp)
    return _extract_license(resp)


def _adopt_any_delivered_key(resp: httpx.Response) -> None:
    """Take a gateway key the control plane put on the settings response.

    The settings channel carries it because self-enrollment's own endpoint could
    not be made to work on at least one production box, while this one answers
    reliably on the same host with the same token. Best-effort and silent when
    absent, which is every ordinary poll.
    """
    try:
        body = resp.json()
        if not isinstance(body, dict):
            return
        key = str(body.get("gateway_key") or "").strip()
        if key:
            from olano.deployment.gateway_key import adopt_delivered_key

            adopt_delivered_key(key, str(body.get("gateway_url") or "").strip() or None)
            return
        _log_withheld_delivery(body)
    except Exception:  # pragma: no cover - never break the settings channel
        logger.debug("settings: could not adopt a delivered gateway key", exc_info=True)


def _log_withheld_delivery(body: dict) -> None:
    """A keyless poll on a keyless box, explained in the box's OWN log.

    The control plane stamps ``gateway_delivery`` with why no key rode on this
    response (``throttled``, ``mint_failed``, ``budget_exceeded``, …). For an
    enrolled box that is the everyday quiet case and worth nothing; for a box
    with no key it is THE diagnostic — the 2026-08-16 workbuddy outage ran for
    days with the reason visible only in the control plane's pod logs, which a
    managed operator cannot read. One WARNING per poll while broken is the
    price of never debugging that blind again.
    """
    reason = str(body.get("gateway_delivery") or "").strip()
    if not reason:
        return  # pre-reason control plane, or delivery not consulted
    from olano.deployment.gateway_key import enrollment_state

    if enrollment_state() == "enrolled":
        return
    logger.warning(
        "settings poll delivered no gateway key (control plane says: %s); olano "
        "tiers stay unavailable until a poll delivers one — see "
        "GET /api/diagnostics/gateway and the deployment card at cloud.olano.ai",
        reason,
    )


# ── Cloud artifact cache (atomic, 0600; lifecycle lives in the signed claims) ─


def _deployment_dir(paths: OlanoHomePaths | None = None) -> Path:
    if paths is None:
        from olano.config.home import get_olano_paths

        paths = get_olano_paths()
    d = paths.deployment_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_cache(paths: OlanoHomePaths | None = None) -> str | None:
    """The cached cloud artifact text, or ``None``. Rejects symlink/oversized."""
    p = _deployment_dir(paths) / CACHE_FILE
    if not p.exists():
        return None
    try:
        return _read_small_file(p, max_bytes=MAX_ARTIFACT_BYTES).strip()
    except (ValueError, OSError) as exc:
        logger.warning("deployment settings cache unreadable: %s", exc)
        return None


def write_cache(license_text: str, *, fetched_at: int, paths: OlanoHomePaths | None = None) -> None:
    """Atomically persist the accepted cloud artifact (tmp + rename, 0600).

    The meta file is diagnostic only — validity always comes from the signed
    ``expires_at`` inside the artifact.
    """
    d = _deployment_dir(paths)
    _atomic_write(d / CACHE_FILE, license_text + "\n")
    _atomic_write(
        d / CACHE_META_FILE,
        json.dumps({"fetched_at": fetched_at, "note": "diagnostic only"}) + "\n",
    )


def read_revision_mark(paths: OlanoHomePaths | None = None) -> tuple[int, int] | None:
    """The (policy_revision, issued_at) high-water mark, or ``None``.

    Best-effort rollback protection: a fully privileged local user can delete
    it, but the signed ``expires_at`` bounds the usefulness of any replay.
    """
    p = _deployment_dir(paths) / REVISION_FILE
    if not p.exists():
        return None
    try:
        data = json.loads(_read_small_file(p, max_bytes=MAX_MANIFEST_BYTES))
        return int(data["policy_revision"]), int(data["issued_at"])
    except Exception as exc:
        logger.warning("deployment revision mark unreadable: %s", exc)
        return None


def write_revision_mark(
    policy_revision: int, issued_at: int, *, paths: OlanoHomePaths | None = None
) -> None:
    _atomic_write(
        _deployment_dir(paths) / REVISION_FILE,
        json.dumps({"policy_revision": policy_revision, "issued_at": issued_at}) + "\n",
    )


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        tmp_path.chmod(0o600)
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
