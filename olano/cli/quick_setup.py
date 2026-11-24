"""Non-interactive one-shot bootstrap: home → workspace → agent → admin → vault.

``olano quick-setup`` takes a fresh/empty Olano home from zero to a usable,
logged-in-able backend with one working agent. It is designed for container and
scripted deploys where ``serve`` would otherwise boot against an empty home.

Every step is idempotent: re-running skips work already done and exits 0.
Neither secret has a default. An admin password nobody supplied is generated
(:mod:`olano.users.initial_password`), printed once by the CLI and kept under
``<home>/run/`` with mode 0600 until the account's password is changed. A vault
passphrase nobody supplied is generated too and persisted to the vault's
boot-key file, which is what ``olano serve`` unlocks the vault from on a box
with no ``OLANO_VAULT_KEY`` and no system keyring.

**The admin account always lands.** The agent install and the vault init are
each fenced: a failure there is recorded on the result (``failed: <why>``) and
the run carries on to the admin step instead of aborting. This is what keeps a
managed box from ever coming up with an empty user table — which is not a
cosmetic state: with zero users the dashboard serves the account-creation
screen to whoever reaches the URL first. The one failure that used to cause
exactly that: on a freshly provisioned managed box the deployment settings can
still resolve to the *quarantine* posture (the signed artifact not fetched yet),
under which ``validate_config_write`` refuses to create any agent — the
AgentFather install raised, quick-setup exited before the admin step, and the
owner's "Open dashboard" click landed on "Create your account" instead of the
sign-in screen with the temporary password.

The runtime runs the same function at first boot (``runtime/first_boot.py``)
when the environment carries provisioning credentials, so the account exists
before the API ever binds. The interactive ``olano setup`` wizard remains the
full-featured path; this module deliberately reuses the same primitives
(``install_archetype``, ``UserStore.bootstrap_admin``, ``VaultStore.open``) so
the paths never drift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Defaults for the parts that are not secrets. The admin password and the vault
# passphrase deliberately have none: unset means generated (module docstring).
DEFAULT_ADMIN_EMAIL = "admin@example.com"
DEFAULT_ADMIN_NAME = "Admin"
DEFAULT_AGENT = "merli"

# Status prefix a fenced step records when it raised. `failed_steps()` keys on
# it, and so does the runtime's first-boot retry (a step that failed on the
# boot that raced the deployment manifest is retried on the next one).
FAILED_PREFIX = "failed: "


@dataclass
class QuickSetupResult:
    """Per-step outcome of a quick-setup run (for reporting + tests)."""

    home: Path
    agent: str
    admin_email: str
    # The passphrase the vault was opened with on this run (None when an
    # existing vault was left alone because nothing could open it).
    vault_password: str | None
    steps: list[tuple[str, str]] = field(default_factory=list)  # (label, status)
    # Set only when this run created the admin with a password it generated.
    generated_admin_password: str | None = None
    initial_password_path: Path | None = None
    # How the runtime will unlock the vault created or found here:
    # "passphrase" (caller-supplied), "env" (OLANO_VAULT_KEY), "bootfile"
    # (the vault's boot-key file, written here when the passphrase was
    # generated) or "none" (an existing vault nothing on this box can open).
    vault_unlock: str = "none"
    vault_passphrase_generated: bool = False
    boot_key_path: Path | None = None

    def add(self, label: str, status: str) -> None:
        self.steps.append((label, status))

    def status(self, label: str) -> str | None:
        """The recorded status of one step, or ``None`` if it never ran."""
        return dict(self.steps).get(label)

    def failed_steps(self) -> list[tuple[str, str]]:
        """Every step that raised, as ``(label, reason)``."""
        return [
            (label, status[len(FAILED_PREFIX) :])
            for label, status in self.steps
            if status.startswith(FAILED_PREFIX)
        ]

    @property
    def ok(self) -> bool:
        return not self.failed_steps()


def _failure(exc: BaseException) -> str:
    """One-line ``failed: <why>`` status for a fenced step."""
    reason = str(exc).strip() or exc.__class__.__name__
    return f"{FAILED_PREFIX}{reason.splitlines()[0][:300]}"


def _settle_deployment_settings(log) -> None:
    """Give a managed box its settings before the one step that is judged by them.

    Says nothing when there is nothing to wait for (an unmanaged box, or a
    box whose artifact is already here), so the CLI transcript of a normal
    run is unchanged. Never raises: the install below is fenced on its own,
    and a settle failure must cost at most the wait.
    """
    try:
        from olano.deployment import resolver

        if resolver.settled(resolver.deployment_status()):
            return
        limit = resolver.settle_timeout()
        if limit <= 0:
            return
        log(f"[2/7] Waiting up to {limit:.0f}s for the deployment settings to arrive…")
        status = resolver.wait_until_settled(limit)
        if resolver.settled(status):
            log(f"[2/7] Deployment settings arrived (posture: {status.posture}).")
        else:
            log(
                f"[2/7] Deployment settings still pending after {limit:.0f}s "
                f"({status.error or 'no artifact'}) — installing anyway; "
                "a refusal here is retried once they land."
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("settling deployment settings failed: %s", exc)


def run_quick_setup(
    *,
    admin_email: str = DEFAULT_ADMIN_EMAIL,
    admin_password: str | None = None,
    admin_name: str = DEFAULT_ADMIN_NAME,
    vault_password: str | None = None,
    agent: str = DEFAULT_AGENT,
    sync_catalogs: bool = True,
    settle_settings: bool = True,
    log=print,
) -> QuickSetupResult:
    """Run the full non-interactive bootstrap. Safe to re-run (idempotent).

    Args:
        admin_email/admin_name: the first admin account to create.
        admin_password: its password. ``None`` generates a one-time password,
            reported on the result (``generated_admin_password``) and kept at
            ``initial_password_path`` until the account's password changes.
            Nothing is generated when an admin already exists.
        vault_password: passphrase used to initialize the encrypted vault.
            ``None`` means: the ``OLANO_VAULT_KEY`` environment variable if
            set, else the vault's boot-key file if present, else (for a vault
            that does not exist yet) a generated passphrase persisted to that
            boot-key file so the runtime can unlock the vault unattended. An
            existing vault with none of those is left alone (``vault_unlock``
            reads ``"none"``), never re-keyed.
        agent: archetype to install as the default agent.
        sync_catalogs: also sync the built-in skills and clone the external
            skills + archetype packs (network, seconds to minutes). The runtime's
            first-boot call passes ``False`` — those refreshes have their own
            boot-time paths, and nothing in provisioning should wait on git.
        settle_settings: before installing the agent on a managed box, wait
            (bounded — ``LIONCLAW_SETTINGS_SETTLE_TIMEOUT``, default 45s) for
            the deployment settings to arrive, so the install is judged by the
            real entitlements and not by the "no artifact yet" quarantine every
            managed box boots into. The race this closes is real: a
            golden-image box reached this step 10s after the runtime started
            and lost, and the designed recovery ("the next boot") never came.
            Only tests pass ``False``.
        log: callable for progress output (defaults to ``print``).

    Returns a :class:`QuickSetupResult` describing each step's outcome. Never
    raises for the agent or vault step (see the module docstring): check
    :meth:`QuickSetupResult.failed_steps` / :attr:`QuickSetupResult.ok`.
    """
    from olano.config.home import (
        HomeConfig,
        ensure_olano_home,
        load_home_config,
        save_home_config,
    )

    # ── 1. Home scaffold ─────────────────────────────────────────────────────
    paths = ensure_olano_home()
    result = QuickSetupResult(
        home=paths.root,
        agent=agent,
        admin_email=admin_email,
        vault_password=vault_password,
    )
    log(f"[1/7] Home ready at {paths.root}")
    result.add("home", "ready")

    # ── 2/3. Install the agent (this also seeds its full workspace) ───────────
    # install_archetype() is the exact code path `olano agents install <name>`
    # uses; scaffold_agent_workspace() inside it writes AGENTS.md, MEMORY.md,
    # memories/, system/knowledge/*, etc. So workspace + agent are one step.
    #
    # Fenced: a refused or broken install must not abort the run before the
    # admin account exists (module docstring). The deployment-limits guard
    # inside the config write is the known refuser — quarantine posture on a
    # box whose settings artifact hasn't arrived yet — and it is transient, so
    # the step is retried by every later idempotent run.
    from olano.archetypes.installer import install_archetype
    from olano.config.loader import list_agents

    try:
        if agent in list_agents(include_hidden=True):
            log(f"[2/7] Agent '{agent}' already installed — skipping.")
            agent_status = "skipped"
        else:
            if settle_settings:
                _settle_deployment_settings(log)
            config_path = install_archetype(agent, agent_name=agent, overwrite=False)
            log(f"[2/7] Agent '{agent}' installed at {config_path.parent}")
            log(f"[3/7] Workspace seeded at {config_path.parent / 'workspace'}")
            agent_status = "installed"

        # Persist `agent` as the home's default agent so `serve` boots it.
        home_config = load_home_config()
        if home_config.default_agent != agent:
            save_home_config(
                HomeConfig(
                    default_agent=agent,
                    preferred_model=home_config.preferred_model,
                    runtime_api_port=home_config.runtime_api_port,
                    dashboard_port=home_config.dashboard_port,
                    multi_user_enabled=home_config.multi_user_enabled,
                    # Carried through for the same reason as the flag above:
                    # this rebuilds the whole dataclass and save_home_config
                    # writes it with asdict(), so a field omitted here is
                    # silently reset. An operator who chose the simple shell
                    # would be put back on the dashboard by an unrelated
                    # default-agent change.
                    ui_mode=home_config.ui_mode,
                    extra_bind_roots=home_config.extra_bind_roots,
                    archetype_dirs=home_config.archetype_dirs,
                )
            )
        result.add("agent", agent_status)
    except Exception as exc:
        logger.warning("quick-setup: agent '%s' install failed: %s", agent, exc)
        log(f"[2/7] Agent '{agent}' install FAILED — continuing: {exc}")
        result.add("agent", _failure(exc))

    # ── 4. Admin user + multi-user mode ──────────────────────────────────────
    # Fenced too, so the vault step below still runs and the CLI / first-boot
    # caller gets a `failed:` status to report and retry rather than a stack
    # trace with no record of how far the run got.
    from olano.runtime.auth import set_multi_user_enabled
    from olano.users.store import get_user_store

    try:
        store = get_user_store()
        if store.db.count_users() > 0:
            existing = store.db.get_user_by_email(admin_email)
            if existing is not None:
                log(f"[4/7] Admin '{admin_email}' already exists — skipping.")
            else:
                log(f"[4/7] An admin already exists ('{admin_email}' not created) — skipping.")
            # Keep multi-user mode on (the admin already gates access).
            set_multi_user_enabled(True)
            result.add("admin", "skipped")
        else:
            # The handle is explicit, not a side effect of DEFAULT_ADMIN_NAME:
            # this account is provisioned on every managed box, so the handle
            # it burns ("admin") should be greppable, deliberate, and stable
            # even if someone renames the default display name later.
            from olano.users import initial_password
            from olano.users.db import PROVISIONED_ADMIN_HANDLE

            password = admin_password
            generated = password is None
            if password is None:
                password = initial_password.generate_password()
            admin = store.bootstrap_admin(
                email=admin_email,
                password=password,
                name=admin_name,
                username=PROVISIONED_ADMIN_HANDLE,
            )
            set_multi_user_enabled(True)
            if generated:
                # The caller prints it once; the note on disk is what the deploy
                # banners point at, and the next password change removes it.
                result.generated_admin_password = password
                try:
                    result.initial_password_path = initial_password.remember(admin.email, password)
                except OSError as exc:
                    logger.warning("quick-setup: one-time password not kept on disk: %s", exc)
                log(
                    f"[4/7] Admin created: {admin.email} "
                    "(multi-user mode ON; one-time password generated)"
                )
            else:
                log(f"[4/7] Admin created: {admin.email} (multi-user mode ON)")
            result.add("admin", "created")
    except Exception as exc:
        logger.error("quick-setup: admin account step failed: %s", exc)
        log(f"[4/7] Admin account FAILED — continuing: {exc}")
        result.add("admin", _failure(exc))

    # ── 5. Vault ─────────────────────────────────────────────────────────────
    # Fenced like the agent step: an existing vault this passphrase cannot open
    # (a re-run with a rotated passphrase) is a report, not an abort.
    from olano.users import initial_password
    from olano.vault.runtime import boot_key_path, read_boot_key, write_boot_key
    from olano.vault.store import VaultStore, default_vault_path

    vault_path = default_vault_path()
    vault_existed = vault_path.exists()
    try:
        passphrase = vault_password
        unlock = "passphrase"
        generated_passphrase = False
        if passphrase is None:
            # No passphrase from the caller: use what the runtime will unlock
            # with, in the runtime's own order (env, then the boot-key file),
            # and mint one only for a vault that does not exist yet.
            from olano.vault.store import vault_key_from_env

            env_key = vault_key_from_env()
            boot_key = read_boot_key()
            if env_key:
                passphrase, unlock = env_key, "env"
            elif boot_key:
                passphrase, unlock = boot_key, "bootfile"
            elif not vault_existed:
                passphrase = initial_password.generate_vault_passphrase()
                unlock, generated_passphrase = "bootfile", True
        if passphrase is None:
            # An existing vault and nothing on this box that opens it: not ours
            # to re-key. The operator holds the passphrase, or sets the env key.
            log(
                f"[5/7] Vault already exists at {vault_path}; no passphrase configured, left as is."
            )
            result.vault_unlock = "none"
            result.add("vault", "skipped")
        else:
            # Opening with a passphrase creates the encrypted SQLite store (and
            # schema) if it does not exist yet.
            VaultStore.open(passphrase=passphrase, db_path=vault_path, use_keyring=False)
            if generated_passphrase:
                # Written after the open succeeded, so a failed init never
                # leaves a key for a vault that was not created.
                write_boot_key(passphrase)
                result.boot_key_path = boot_key_path()
                result.vault_passphrase_generated = True
            result.vault_password = passphrase
            result.vault_unlock = unlock
            if vault_existed:
                log(f"[5/7] Vault already initialized at {vault_path}: skipping.")
                result.add("vault", "skipped")
            elif generated_passphrase:
                log(
                    f"[5/7] Vault initialized at {vault_path} (passphrase generated; "
                    f"the runtime unlocks it from {result.boot_key_path})"
                )
                result.add("vault", "initialized")
            else:
                log(f"[5/7] Vault initialized at {vault_path}")
                result.add("vault", "initialized")
    except Exception as exc:
        logger.warning("quick-setup: vault init at %s failed: %s", vault_path, exc)
        log(f"[5/7] Vault init FAILED, continuing: {exc}")
        result.add("vault", _failure(exc))

    if not sync_catalogs:
        return result

    # ── 6. Skills: package baseline + external catalog ───────────────────────
    # First copy the package-bundled skills into <home>/skills/ so `serve` boots
    # with a working skill set even offline. Then overlay the richer canonical
    # catalog (olano-ai/skills) into the same common skills dir. Both are
    # idempotent and non-fatal — an empty/partial skills dir must never block the
    # rest of the bootstrap.
    try:
        from olano.skills.built_in_sync import sync_builtin_skills

        skill_report = sync_builtin_skills(paths.root)
        log(f"[6/7] Skills synced: {skill_report}")
        result.add("skills", "synced")
    except Exception as exc:  # pragma: no cover - defensive
        log(f"[6/7] Skills sync skipped: {exc}")
        result.add("skills", "skipped")

    # Overlay the external skills pack (olano-ai/skills) — the canonical superset
    # of the common skills. Non-fatal: bundled skills already work if this fails.
    try:
        from olano.skills.sync import fetch_olano_skills

        ok, msg = fetch_olano_skills(paths.root, quiet=True)
        log(f"[6/7] Skills catalog: {msg}")
        result.add("skills_catalog", "fetched" if ok else "skipped")
    except Exception as exc:  # pragma: no cover - defensive
        log(f"[6/7] Skills catalog skipped: {exc}")
        result.add("skills_catalog", "skipped")

    # ── 7. Expert catalog (external archetype pack) ──────────────────────────
    # Clone/refresh the canonical archetype repo so the subagent catalog shows
    # the full expert list, not just the in-package templates. Non-fatal —
    # bundled archetypes always work even if git/network is unavailable.
    try:
        from olano.archetypes.sync import fetch_lionclaw_agents

        ok, msg = fetch_lionclaw_agents(quiet=True)
        log(f"[7/7] Expert catalog: {msg}")
        result.add("catalog", "fetched" if ok else "skipped")
    except Exception as exc:  # pragma: no cover - defensive
        log(f"[7/7] Expert catalog skipped: {exc}")
        result.add("catalog", "skipped")

    return result


def vault_key_for_runtime(vault_password: str | None) -> str | None:
    """Return the OLANO_VAULT_KEY value to advertise, or None when none is needed.

    Only a passphrase the operator supplied needs exporting: the runtime
    unlocks the vault from ``OLANO_VAULT_KEY`` when that is set, and a CLI
    process cannot persist an env var to the parent shell, so the caller prints
    the export line. A generated passphrase lives in the boot-key file, which
    the runtime reads on its own, and an already-set env key needs nothing.
    """
    from olano.vault.store import vault_key_from_env

    if not vault_password or vault_key_from_env():
        return None
    return vault_password
