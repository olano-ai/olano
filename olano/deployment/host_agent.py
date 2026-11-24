"""Keep the HOST-side managed-update agent current from inside the runtime.

``/opt/olano/update-agent.sh`` is baked onto a managed box by cloud-init at
provision time and runs OUTSIDE any container (a systemd timer, as the host's
olano service user). Historically it never updated itself, so every fix to the
update flow — the disk preflight, honest failure messages, image cleanup —
reached only newly provisioned boxes while the installed fleet kept its
provision-day script forever (live consequence: a 2026-08 box hit mid-pull
ENOSPC that a newer preflight already prevented).

Current agents self-refresh (they compare their own sha256 against
``agent_script_sha256`` in the control plane's ``GET /api/v1/deployments/runtime``
and re-fetch on mismatch), but a box provisioned BEFORE that mechanism runs an
agent that does not know how. This module is the bridge: the runtime container
DOES update (every image pull replaces this code) and the all-in-one mounts the
host docker socket — so once per boot, on a managed box, we fetch the canonical
agent from the control plane and, when the host copy differs, install it via a
short-lived helper container that bind-mounts ``/opt/olano``. From then on the
host agent keeps itself current and this bridge finds nothing to do.

Trust model: the script arrives from the same control plane, over TLS,
authenticated with this deployment's bearer token — the exact channel that
delivered the original agent via cloud-init, and the one the new agent itself
trusts for self-refresh. The docker socket is already mounted for sandbox
spawning and disk actions, so no new privilege is introduced — an existing one
is used for fleet maintenance. Guard rails:

- managed-cloud installs only (installation manifest + deployment token);
- installs ONLY over an existing ``/opt/olano/update-agent.sh`` — a host
  without one was never provisioned with the agent, and this must never plant
  files on arbitrary setups;
- payload verified before install: shebang, ASCII-only (the report path
  mangled non-ASCII into mojibake), sha256 against the response's
  ``X-Agent-Sha256``, and ``bash -n`` inside the helper container;
- atomic install (write ``.new``, ``chmod``, ``mv``) with the helper detached
  from the network (``--network none``);
- ``LIONCLAW_HOST_AGENT_SYNC=0`` disables entirely.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

import httpx

from olano.deployment.sources import CloudConfig, cloud_settings_config

logger = logging.getLogger(__name__)

DISABLE_ENV = "LIONCLAW_HOST_AGENT_SYNC"
HOST_AGENT_PATH = "/host-opt/update-agent.sh"
FETCH_PATH = "/api/v1/deployments/update-agent"
FETCH_TIMEOUT_SECONDS = 30.0
HELPER_TIMEOUT_SECONDS = 60.0
MAX_SCRIPT_BYTES = 256 * 1024

# The backend container's fixed compose names — `container_name: olano` since
# the lionclaw → olano rename; boxes provisioned earlier still run
# `lionclaw` (their frozen compose file keeps recreating it under that name).
RUNTIME_CONTAINER_NAMES = ("olano", "lionclaw")

RunFn = Callable[..., subprocess.CompletedProcess[str]]


def sync_enabled() -> bool:
    """Managed-cloud box with a cloud channel, docker reachable, not opted out."""
    import os

    if os.environ.get(DISABLE_ENV, "").strip() == "0":
        return False
    try:
        from olano.deployment.sources import read_manifest

        manifest = read_manifest()
    except Exception:
        return False
    if not (manifest and manifest.managed and manifest.mode == "cloud"):
        return False
    if cloud_settings_config() is None:
        return False
    from olano.runtime.disk_monitor import docker_daemon_reachable

    return shutil.which("docker") is not None and docker_daemon_reachable()


def fetch_canonical_agent(cfg: CloudConfig) -> tuple[str, str] | None:
    """``(script, sha256)`` from the control plane, or None on any failure."""
    import hashlib

    url = f"{cfg.url.rstrip('/')}{FETCH_PATH}"
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {cfg.token}"},
            timeout=FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.info("host-agent sync: fetch failed (%s)", exc)
        return None
    script = resp.text
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
    advertised = (resp.headers.get("X-Agent-Sha256") or "").strip().lower()
    if advertised and advertised != digest:
        logger.warning("host-agent sync: response body does not hash to X-Agent-Sha256; refusing")
        return None
    return script, digest


def validate_script(script: str) -> str | None:
    """Reason the script is unacceptable, or None when it looks like the agent."""
    if not script.startswith("#!/usr/bin/env bash"):
        return "missing bash shebang"
    if len(script.encode("utf-8")) > MAX_SCRIPT_BYTES:
        return "script implausibly large"
    if not script.isascii():
        return "script contains non-ASCII bytes"
    # Anchors of the real agent — a wrong/rogue payload with the right hash
    # header still has to look like the update agent to be installed.
    for anchor in ("/opt/olano/olano.env", "LIONCLAW_DEPLOYMENT_TOKEN", "update-applied-at"):
        if anchor not in script:
            return f"script missing expected anchor {anchor!r}"
    return None


def _runtime_image(run_fn: RunFn = subprocess.run) -> str | None:
    """A docker image reference guaranteed present locally: our own.

    The helper container must not pull anything (the box may be disk-starved —
    that is often why the agent needs fixing), so it runs from the image id of
    the running backend container.
    """
    for name in RUNTIME_CONTAINER_NAMES:
        try:
            proc = run_fn(
                ["docker", "inspect", "--format", "{{.Image}}", name],
                capture_output=True,
                text=True,
                timeout=HELPER_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode == 0 and proc.stdout.strip().startswith("sha256:"):
            return proc.stdout.strip()
    return None


def read_host_agent_sha(image: str, run_fn: RunFn = subprocess.run) -> str | None:
    """sha256 of the host's /opt/olano/update-agent.sh, or None when absent."""
    try:
        proc = run_fn(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                "/opt/olano:/host-opt:ro",
                "--entrypoint",
                "sh",
                image,
                "-c",
                f"sha256sum {HOST_AGENT_PATH}",
            ],
            capture_output=True,
            text=True,
            timeout=HELPER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    parts = proc.stdout.split()
    return parts[0].lower() if parts and len(parts[0]) == 64 else None


def install_host_agent(image: str, script: str, run_fn: RunFn = subprocess.run) -> bool:
    """Atomically replace the host agent via a helper container.

    The script travels on stdin (never argv), lands as ``.new``, is re-checked
    with ``bash -n`` on the host-mounted copy, then chmod+mv into place —
    root-owned 755, exactly what cloud-init installs.
    """
    try:
        proc = run_fn(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--network",
                "none",
                "-v",
                "/opt/olano:/host-opt",
                "--entrypoint",
                "sh",
                image,
                "-c",
                (
                    f"cat >{HOST_AGENT_PATH}.new"
                    f" && bash -n {HOST_AGENT_PATH}.new"
                    f" && chmod 755 {HOST_AGENT_PATH}.new"
                    f" && mv {HOST_AGENT_PATH}.new {HOST_AGENT_PATH}"
                ),
            ],
            input=script,
            capture_output=True,
            text=True,
            timeout=HELPER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        logger.warning("host-agent sync: install helper failed: %s", proc.stderr.strip()[:500])
        return False
    return True


def sync_host_agent(
    *,
    cfg: CloudConfig | None = None,
    run_fn: RunFn = subprocess.run,
    fetch_fn: Callable[[CloudConfig], tuple[str, str] | None] = fetch_canonical_agent,
) -> dict[str, Any]:
    """One reconciliation pass. Returns a small status dict (logged + testable)."""
    cfg = cfg or cloud_settings_config()
    if cfg is None:
        return {"status": "no-cloud-config"}
    fetched = fetch_fn(cfg)
    if fetched is None:
        return {"status": "fetch-failed"}
    script, want_sha = fetched
    reason = validate_script(script)
    if reason is not None:
        return {"status": "invalid-script", "reason": reason}
    image = _runtime_image(run_fn)
    if image is None:
        return {"status": "no-runtime-image"}
    have_sha = read_host_agent_sha(image, run_fn)
    if have_sha is None:
        # No host agent = not a box this mechanism provisioned. Never create it.
        return {"status": "no-host-agent"}
    if have_sha == want_sha:
        return {"status": "in-sync", "sha": want_sha}
    if not install_host_agent(image, script, run_fn):
        return {"status": "install-failed", "from": have_sha, "to": want_sha}
    return {"status": "installed", "from": have_sha, "to": want_sha}


class HostAgentSync:
    """Boot-time, best-effort reconciler (started by the runtime server).

    Runs once shortly after boot — boot is exactly when this code can have
    changed (an update is a pull + recreate) — with a couple of retries for a
    control plane that is momentarily unreachable. No periodic loop: once the
    host agent is current it keeps itself current via its own self-refresh.
    """

    RETRY_DELAYS_SECONDS = (30.0, 300.0)

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(), name="host_agent_sync")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        delays = (0.0, *self.RETRY_DELAYS_SECONDS)
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                result = await asyncio.to_thread(sync_host_agent)
            except Exception:
                logger.exception("host-agent sync pass failed")
                return
            status = result.get("status")
            if status == "installed":
                logger.info(
                    "host-agent sync: updated /opt/olano/update-agent.sh %s → %s",
                    str(result.get("from"))[:12],
                    str(result.get("to"))[:12],
                )
                return
            if status in ("in-sync", "no-host-agent", "no-cloud-config", "invalid-script"):
                logger.debug("host-agent sync: %s", result)
                return
            # fetch-failed / no-runtime-image / install-failed → retry a bit.
            logger.info("host-agent sync attempt %d: %s", attempt, result)
        logger.info("host-agent sync: giving up until next boot")
