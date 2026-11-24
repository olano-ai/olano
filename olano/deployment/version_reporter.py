"""Tell the control plane what this box is running, at the moment it changes.

``deployments.runtime_version`` had exactly one writer: the update agent, on a
successful update. So a box that was provisioned and never updated through that
path reported nothing, forever — and the console rendered nothing as
``"latest (as deployed)"``, which reads like confirmation and means the opposite.

Boot is the right moment to report: it is precisely when the answer can have
changed (a pull + recreate is what an Update *is*), and it costs one small POST
per process lifetime. The daily disk report carries the same field as a
self-healing refresh, so a box that was offline when the control plane lost the
value fills it back in without a restart.

Best-effort throughout: a control plane that cannot be reached must never delay
or break a boot. Nothing here retries hard — the next boot, or the next daily
report, is the retry.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

HEALTH_PATH = "/api/v1/health"
POST_TIMEOUT_SECONDS = 10.0


def report_runtime_version(*, transport: object | None = None) -> bool:
    """POST this runtime's version to the control plane. Never raises.

    Returns True when the control plane accepted it. False covers "no cloud
    channel", "version unknown", and any network/HTTP failure — all of which are
    ordinary, none of which are worth waking anyone over.
    """
    try:
        from olano.deployment.sources import cloud_settings_config
        from olano.deployment.version import build_sha, package_version, runtime_version

        version = runtime_version()
        if not version:
            # An unknown version is a real state, and reporting a placeholder
            # would recreate the exact bug this module exists to kill. Say
            # nothing and let the console show "not reported".
            logger.debug("version-reporter: runtime version unknown; not reporting")
            return False

        cfg = cloud_settings_config()
        if cfg is None:
            return False

        # `build_sha` is the canonical identity the control plane compares and
        # displays (deployments.reported_commit); `runtime_version` stays for
        # older control planes, which ignore unknown fields. Omitted when the
        # image baked nothing — same never-a-placeholder rule as above.
        payload: dict[str, str] = {"runtime_version": version}
        sha = build_sha()
        if sha:
            payload["build_sha"] = sha
        # The release name beside the commit: the package version is what the
        # release tag carries, so the console can show "0.10.5" next to the
        # build id instead of the id alone. Display only on the control plane
        # (never compared; the commit is what the update verdict reads), and
        # omitted when unknown, like everything else in this payload.
        pkg = package_version()
        if pkg:
            payload["package_version"] = pkg

        import httpx

        client_kwargs: dict[str, object] = {"timeout": POST_TIMEOUT_SECONDS}
        if transport is not None:
            client_kwargs["transport"] = transport
        with httpx.Client(**client_kwargs) as client:  # type: ignore[arg-type]
            resp = client.post(
                f"{cfg.url}{HEALTH_PATH}",
                headers={"Authorization": f"Bearer {cfg.token}"},
                json=payload,
            )
        if not (200 <= resp.status_code < 300):
            logger.debug("version-reporter: HTTP %s", resp.status_code)
            return False
        logger.info("Reported runtime version to the control plane: %s", version)
        return True
    except Exception:  # pragma: no cover - reporting must never break a boot
        logger.debug("version-reporter: failed", exc_info=True)
        return False
