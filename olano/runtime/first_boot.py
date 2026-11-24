"""Provision a box's admin account at ``olano serve`` startup, before the API binds.

A managed deployment is handed to its owner as one sign-in: the email they
signed up with and a temporary password the cloud mailed them. That account is
created by ``olano quick-setup`` — which the all-in-one bootstrap runs **after**
``docker compose up``, i.e. after the runtime is already answering. Between the
two, and for good on any box where quick-setup died before its admin step, the
users table is empty, and an empty users table is not a blank page: the
dashboard's access gate answers ``setup_required`` and serves the *create your
account* screen to whoever reaches the URL first. The owner then sees no
password prompt at all and reads it as "did the cloud log me in?" — it did not;
the box simply had no account to log into, and a stranger could have created
one just the same.

So the runtime provisions itself. When the environment carries first-run
credentials (``LIONCLAW_ADMIN_PASSWORD`` — the compose file feeds the deploy
``.env`` into the container, and cloud-init writes the owner's email + temporary
password there) and the home has no users, ``serve`` runs the same idempotent
:func:`olano.cli.quick_setup.run_quick_setup` the CLI runs — admin + multi-user
flag, the default agent (``LIONCLAW_DEFAULT_AGENT``, AgentFather on a managed
box), the vault — before ``_start_api``. The deploy script's later quick-setup
then finds everything in place and only refreshes the skill/archetype packs.

Three rules keep this from ever surprising an existing box:

* **Only an explicit password provisions.** Nothing is ever provisioned
  unasked: no ``LIONCLAW_ADMIN_PASSWORD`` in the environment means this module
  does nothing, and a manual install behaves exactly as before. The vault
  passphrase is the one value that may be generated here (when neither
  ``LIONCLAW_VAULT_PASSWORD`` nor ``OLANO_VAULT_KEY`` names one): it lands
  in the vault's boot-key file, which the unlock that follows reads.
* **Only a never-provisioned home.** A home that already has users when this
  first runs was provisioned by something else (an older box upgrading, an
  operator who ran quick-setup by hand, an owner who created the first account
  themselves). It is recorded as such and never touched — in particular the
  default agent is not reinstalled on a box whose owner deleted it.
* **A failed step is retried, a finished one never is.** The outcome is kept
  in ``<home>/run/first-boot.json``. The agent install can legitimately fail on
  the very first boot — the deployment settings may still resolve to the
  quarantine posture, whose limits guard refuses to create any agent — so a
  step recorded ``failed: …`` runs again on the next boot, and once every step
  has succeeded the file is final.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ADMIN_EMAIL_ENV = "LIONCLAW_ADMIN_EMAIL"
ADMIN_PASSWORD_ENV = "LIONCLAW_ADMIN_PASSWORD"
ADMIN_NAME_ENV = "LIONCLAW_ADMIN_NAME"
VAULT_PASSWORD_ENV = "LIONCLAW_VAULT_PASSWORD"
# The runtime unlocks the vault from this one; the passphrase quick-setup uses
# to CREATE the vault must therefore agree with it when only it is set. Older
# boxes carry it under the legacy spelling, read second.
VAULT_KEY_ENV = "OLANO_VAULT_KEY"
LEGACY_VAULT_KEY_ENV = "LIONCLAW_VAULT_KEY"
DEFAULT_AGENT_ENV = "LIONCLAW_DEFAULT_AGENT"

STATE_FILENAME = "first-boot.json"
# Steps whose failure keeps the state file non-final (retried next boot).
RETRIED_STEPS = ("admin", "agent", "vault")


@dataclass(frozen=True)
class Provisioning:
    """What the environment asks the first boot to create."""

    admin_email: str
    admin_password: str
    admin_name: str
    # None: nothing in the environment names a vault passphrase, so quick-setup
    # generates one and persists it to the vault's boot-key file.
    vault_password: str | None
    agent: str


def provisioning_from_env(environ: Mapping[str, str] | None = None) -> Provisioning | None:
    """The provisioning the environment carries, or ``None`` when it carries none.

    Keyed on an explicit admin password — the one value that is never
    defaulted here. Everything else falls back to what ``olano quick-setup``
    would use with the same environment, so the two can never disagree about
    the account they create.
    """
    from olano.cli.quick_setup import DEFAULT_ADMIN_EMAIL, DEFAULT_ADMIN_NAME, DEFAULT_AGENT

    env = os.environ if environ is None else environ
    password = (env.get(ADMIN_PASSWORD_ENV) or "").strip()
    if not password:
        return None
    return Provisioning(
        admin_email=(env.get(ADMIN_EMAIL_ENV) or "").strip() or DEFAULT_ADMIN_EMAIL,
        admin_password=password,
        admin_name=(env.get(ADMIN_NAME_ENV) or "").strip() or DEFAULT_ADMIN_NAME,
        vault_password=(
            (env.get(VAULT_PASSWORD_ENV) or "").strip()
            or (env.get(VAULT_KEY_ENV) or "").strip()
            or (env.get(LEGACY_VAULT_KEY_ENV) or "").strip()
            or None
        ),
        agent=(env.get(DEFAULT_AGENT_ENV) or "").strip() or DEFAULT_AGENT,
    )


def state_path() -> Path:
    from olano.config.home import get_olano_paths

    return get_olano_paths().run_dir / STATE_FILENAME


def load_state() -> dict[str, Any]:
    """The recorded outcome of a previous first boot (``{}`` when none)."""
    path = state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("first-boot state unreadable at %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(data: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**data, "at": datetime.now(timezone.utc).isoformat()}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def steps_to_retry(state: Mapping[str, Any]) -> list[str]:
    """Steps a previous first boot recorded as failed."""
    from olano.cli.quick_setup import FAILED_PREFIX

    steps = state.get("steps")
    if not isinstance(steps, dict):
        return []
    return [
        label
        for label in RETRIED_STEPS
        if isinstance(steps.get(label), str) and steps[label].startswith(FAILED_PREFIX)
    ]


def _user_count() -> int:
    from olano.users.store import get_user_store

    return get_user_store().db.count_users()


def first_boot_provisioning(log: Callable[[str], Any] | None = None) -> Any | None:
    """Run at ``serve`` startup. Returns the :class:`QuickSetupResult` when it
    provisioned (or retried) something, ``None`` when there was nothing to do.

    Never raises for a provisioning failure — the caller logs and boots on; a
    box that cannot provision must still come up so the failure is reachable
    through the dashboard's logs rather than a dead container.
    """
    spec = provisioning_from_env()
    if spec is None:
        return None

    state = load_state()
    if state:
        retry = steps_to_retry(state)
        if not retry:
            return None
        logger.info("First-boot provisioning: retrying %s", ", ".join(retry))
    elif _user_count() > 0:
        # Provisioned by something other than this hook — leave it alone, and
        # say so once so the next boot does not have to ask the DB again.
        _write_state({"source": "external", "steps": {}})
        logger.info("First-boot provisioning: the home already has accounts; nothing to do.")
        return None
    else:
        logger.info(
            "First-boot provisioning: no accounts yet — creating admin %s and installing '%s'",
            spec.admin_email,
            spec.agent,
        )

    from olano.cli.quick_setup import run_quick_setup

    emit = log or logger.info
    result = run_quick_setup(
        admin_email=spec.admin_email,
        admin_password=spec.admin_password,
        admin_name=spec.admin_name,
        vault_password=spec.vault_password,
        agent=spec.agent,
        sync_catalogs=False,
        log=emit,
    )
    _write_state({"source": "env", "steps": dict(result.steps)})
    for label, reason in result.failed_steps():
        logger.warning(
            "First-boot provisioning: step '%s' failed (%s); it is retried at the next start.",
            label,
            reason,
        )
    return result


def temporary_password_deployment_id() -> str | None:
    """The managed deployment whose cloud console still holds this box's
    temporary sign-in password, or ``None``.

    What the sign-in screen asks before it tells a visitor where to find the
    password it expects (the welcome email, or the deployment's Security tab in
    the console). Three facts must all hold, and each one closes a way the hint
    could point somebody at a password that is not theirs to use:

    * **This runtime provisioned the account from the environment**: the state
      file reads ``source: env`` and its admin step did not fail. ``external`` (a
      home that already had accounts when this hook first ran: an older box
      upgrading, an owner who created the first account themselves) never
      qualifies, because nothing on such a box says its password was ever the
      temporary one. A retry pass records the admin step ``skipped``; that is
      the same account, so it still counts.
    * **Nobody has replaced it**: :func:`credential_rotation.admin_password_rotated`
      is the recorded fact the onboarding wizard and ``/api/auth/claim`` read,
      stamped by every path that retires the temporary password (a password
      change, a reset link, the claim, a self-created first account). It is also
      what tells the console to drop its copy, so the hint and the console go
      quiet together.
    * **The box is a managed-cloud deployment**: its installation manifest says
      ``mode: cloud``. Only then is there a console holding a copy to point at.

    Never raises: an unreadable input reads as "no hint", which leaves the
    ordinary sign-in screen, exactly what every box showed before this existed.
    """
    try:
        state = load_state()
        if state.get("source") != "env":
            return None
        steps = state.get("steps")
        admin_step = steps.get("admin") if isinstance(steps, dict) else None
        from olano.cli.quick_setup import FAILED_PREFIX

        if not isinstance(admin_step, str) or admin_step.startswith(FAILED_PREFIX):
            return None

        from olano.deployment import credential_rotation

        if credential_rotation.admin_password_rotated():
            return None

        from olano.deployment import sources

        manifest = sources.read_manifest()
        if manifest is None or not manifest.managed or manifest.mode != "cloud":
            return None
        return manifest.deployment_id or None
    except Exception:
        logger.debug("temporary-password hint unavailable", exc_info=True)
        return None
