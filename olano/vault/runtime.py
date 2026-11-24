"""Runtime helpers for unlocking the credentials vault and exposing it to agents.

This module bridges the encrypted SQLite vault (`olano.vault.store`) and the
agent runtime by:

- attempting an unlock using whatever method is available (passphrase, env var,
  system keyring, or — when permitted — an interactive prompt),
- registering the unlocked store via `olano.vault.resolver.set_vault()` so
  that ${vault:NAME} references and the `secret_*` agent tools can read it,
- copying credential values into `os.environ` so any tool / MCP server that
  expects ambient env vars (Alpaca, Stripe, Shopify, …) just works.

The unlock helpers never raise on failure — agents must run even when the
vault is missing, locked, or has the wrong passphrase. Failures are logged.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import click

from olano.vault.keyring_backend import (
    _VAULT_KEYRING_KEY,
    _VAULT_KEYRING_SERVICE,
    secure_keyring_available,
)
from olano.vault.resolver import set_vault
from olano.vault.store import (
    BOOT_KEY_FILENAME,
    VaultError,
    VaultStore,
    boot_key_path,
    default_vault_path,
    read_boot_key,
    vault_key_from_env,
)

__all__ = [
    "BOOT_KEY_FILENAME",
    "VaultUnlockResult",
    "boot_key_path",
    "detect_unlock_mode",
    "maybe_hydrate_env_from_vault",
    "persist_auto_unlock_key",
    "read_boot_key",
    "unlock_vault_for_session",
    "write_boot_key",
]

logger = logging.getLogger(__name__)

UnlockMode = str  # "passphrase" | "env" | "bootfile" | "keyring" | "prompt" | "ephemeral" | "none"

# Auto-unlock boot-key file, stored beside the vault DB (0600; the path and the
# reader live in vault/store.py). Two writers:
#
# * quick-setup, when nobody supplied a vault passphrase: it generates one and
#   persists it here, which is what makes a headless box with no
#   OLANO_VAULT_KEY and no system keyring unlock its vault unattended.
# * a rekey (or a later successful passphrase unlock) on a box that already
#   auto-unlocks from key-on-box material, i.e. OLANO_VAULT_KEY is set (the
#   managed-cloud posture, where cloud-init baked the key into the container
#   env and nothing inside the runtime can update it) or this file exists. The
#   file then carries the *current* passphrase across restarts and the unlock
#   chain falls back to it when the env key goes stale.
#
# Boxes that unlock from a desktop keyring or an interactive passphrase never
# get this file from a rekey: no new key material lands on disk for them.


@dataclass(frozen=True)
class VaultUnlockResult:
    vault: VaultStore | None
    mode: UnlockMode
    db_existed: bool
    error: str | None = None


def detect_unlock_mode(passphrase: str | None = None) -> UnlockMode:
    """Return the unlock mechanism that would be used right now (no side effects)."""
    if passphrase:
        return "passphrase"
    if vault_key_from_env():
        return "env"
    if read_boot_key():
        return "bootfile"
    if secure_keyring_available():
        return "keyring"
    return "ephemeral"


# ── auto-unlock persistence (boot-key file + keyring refresh) ────────────────


def write_boot_key(passphrase: str) -> None:
    """Persist ``passphrase`` as this box's auto-unlock key (0600, atomic).

    For quick-setup, which calls it right after creating a vault with a
    passphrase it generated. The unlock chain reads the file after the env key
    and before the keyring, so the next ``olano serve`` opens that vault with
    no operator action.
    """
    _write_boot_key(passphrase)


def _write_boot_key(passphrase: str) -> None:
    p = boot_key_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(passphrase + "\n")
        tmp_path.chmod(0o600)
        tmp_path.replace(p)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _key_opens_current_vault(key: str) -> bool:
    """Does ``key`` decrypt the current vault DB? (False for a stale key.)"""
    try:
        vault = VaultStore.open(passphrase=key, db_path=default_vault_path(), use_keyring=False)
        _verify_vault_key(vault)
        return True
    except Exception:
        return False


def persist_auto_unlock_key(passphrase: str) -> list[str]:
    """Refresh this box's *existing* auto-unlock material with ``passphrase``.

    Called after a rekey (and after a successful explicit-passphrase unlock) so
    unattended restarts keep working when the previously-stored key went stale.
    Deliberately never *introduces* auto-unlock where none was configured:

    - **keyring** — replaced only when an entry already exists AND it no longer
      opens the current vault (the documented macOS "keyring drift" failure).
      Never creates a first entry from a typed passphrase.
    - **boot-key file** — written when ``OLANO_VAULT_KEY`` is set (the
      managed-cloud key-on-box posture; the baked env can't be rewritten from
      here) or when the file already exists. Never created otherwise.

    Returns the mechanisms refreshed (``["keyring", "bootfile"]``-subset).
    Never raises.
    """
    refreshed: list[str] = []
    try:
        if secure_keyring_available():
            try:
                import keyring

                existing = keyring.get_password(_VAULT_KEYRING_SERVICE, _VAULT_KEYRING_KEY)
                if existing == passphrase:
                    refreshed.append("keyring")
                elif existing and not _key_opens_current_vault(existing):
                    keyring.set_password(_VAULT_KEYRING_SERVICE, _VAULT_KEYRING_KEY, passphrase)
                    logger.info("Vault: stale keyring master key refreshed after rekey/unlock")
                    refreshed.append("keyring")
            except Exception as exc:
                logger.warning("Vault: keyring master-key refresh failed: %s", exc)
        if vault_key_from_env() or boot_key_path().exists():
            try:
                if read_boot_key() != passphrase:
                    _write_boot_key(passphrase)
                    logger.info(
                        "Vault: boot-key file refreshed (%s) — restarts auto-unlock with the "
                        "current passphrase even though the env key is stale.",
                        boot_key_path(),
                    )
                refreshed.append("bootfile")
            except Exception as exc:
                logger.warning("Vault: boot-key refresh failed: %s", exc)
    except Exception:  # pragma: no cover - persistence must never break an unlock
        logger.exception("Vault: auto-unlock persistence failed")
    return refreshed


def _verify_vault_key(vault: VaultStore) -> None:
    """Confirm the key the vault was opened with actually decrypts its data.

    Delegates to :meth:`VaultStore.verify_key`, which probes one row from the
    first non-empty table — common credentials, per-agent overrides, or
    customer credentials. All three count: a vault whose only rows live
    outside the common table used to validate against *any* passphrase, and a
    wrongly-"unlocked" vault accepts writes, so the first secret stored under
    the wrong key locked the correct passphrase out of its own vault.

    Raises ``VaultError`` when the key is wrong. A vault with no rows anywhere
    can't be validated (any passphrase becomes the de-facto key on first
    write), so it passes.
    """
    vault.verify_key()


def unlock_vault_for_session(
    *,
    passphrase: str | None = None,
    allow_prompt: bool = False,
    register: bool = True,
    max_prompt_attempts: int = 2,
) -> VaultUnlockResult:
    """Attempt to unlock the credentials vault for this process.

    Resolution order:
        1. explicit `passphrase` argument
        2. OLANO_VAULT_KEY env var (``LIONCLAW_VAULT_KEY`` still honoured)
        3. boot-key file (written by a rekey when env-based unlock is configured)
        4. system keyring
        5. interactive prompt (only when `allow_prompt` is True AND the vault
           DB already exists — we never prompt to create a new vault from chat)

    Every unattended candidate is decrypt-verified and a failing one FALLS
    THROUGH to the next — a baked ``OLANO_VAULT_KEY`` that went stale when
    the owner rekeyed the vault must not lock the boot while a fresher key
    (boot-key file, keyring) is available. When nothing works, the FIRST
    failure is reported (it names the configured mode, the most actionable).

    A successful unlock with an explicit passphrase (argument or prompt) also
    refreshes the box's existing auto-unlock material via
    :func:`persist_auto_unlock_key` (skipped for ``register=False`` probes), so
    one manual unlock re-arms unattended restarts after a rekey.

    On success, registers the store via `olano.vault.resolver.set_vault()`.
    On failure or absence of a vault DB, returns a result with `vault=None`
    and a populated `mode`/`error`.
    """
    db_path = default_vault_path()
    db_existed = db_path.exists()

    if passphrase:
        try:
            vault = VaultStore.open(passphrase=passphrase, db_path=db_path, use_keyring=False)
            _verify_vault_key(vault)
            if register:
                set_vault(vault)
                persist_auto_unlock_key(passphrase)
            return VaultUnlockResult(vault=vault, mode="passphrase", db_existed=db_existed)
        except Exception as exc:
            return VaultUnlockResult(
                vault=None, mode="passphrase", db_existed=db_existed, error=str(exc)
            )

    first_failure: VaultUnlockResult | None = None

    def _try_key(mode: UnlockMode, key: str) -> VaultUnlockResult | None:
        nonlocal first_failure
        try:
            vault = VaultStore.open(passphrase=key, db_path=db_path, use_keyring=False)
            _verify_vault_key(vault)
            if register:
                set_vault(vault)
            return VaultUnlockResult(vault=vault, mode=mode, db_existed=db_existed)
        except Exception as exc:
            logger.warning(
                "Vault unlock via %s failed (%s) — trying the next method. A stale "
                "OLANO_VAULT_KEY after a vault rekey is the usual cause for mode=env.",
                mode,
                exc,
            )
            if first_failure is None:
                first_failure = VaultUnlockResult(
                    vault=None, mode=mode, db_existed=db_existed, error=str(exc)
                )
            return None

    env_key = vault_key_from_env() or None
    if env_key:
        result = _try_key("env", env_key)
        if result is not None:
            return result

    boot_key = read_boot_key()
    if boot_key and boot_key != env_key:
        result = _try_key("bootfile", boot_key)
        if result is not None:
            return result

    if secure_keyring_available():
        keyring_key: str | None = None
        try:
            import keyring

            keyring_key = keyring.get_password(_VAULT_KEYRING_SERVICE, _VAULT_KEYRING_KEY)
        except Exception as exc:
            logger.debug("Vault: keyring read failed: %s", exc)
        if keyring_key:
            # Skip keys that already failed above — same key, same outcome.
            if keyring_key not in (env_key, boot_key):
                result = _try_key("keyring", keyring_key)
                if result is not None:
                    return result
        elif not env_key:
            # First-run desktop: no stored master key yet — let the store
            # create one in the OS keychain (legacy behavior preserved).
            try:
                vault = VaultStore.open(db_path=db_path, use_keyring=True)
                _verify_vault_key(vault)
                if register:
                    set_vault(vault)
                return VaultUnlockResult(vault=vault, mode="keyring", db_existed=db_existed)
            except Exception as exc:
                if first_failure is None:
                    first_failure = VaultUnlockResult(
                        vault=None, mode="keyring", db_existed=db_existed, error=str(exc)
                    )

    if allow_prompt and db_existed:
        return _prompt_for_passphrase(db_path, db_existed, max_prompt_attempts, register)

    if first_failure is not None:
        return first_failure

    # No persistent unlock method and we're not allowed to prompt. Return None
    # so callers can decide whether to fall back to ephemeral mode.
    return VaultUnlockResult(vault=None, mode="none", db_existed=db_existed)


def _prompt_for_passphrase(
    db_path,
    db_existed: bool,
    max_attempts: int,
    register: bool,
) -> VaultUnlockResult:
    """Prompt the user for a vault passphrase, retrying up to `max_attempts`."""
    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            passphrase = click.prompt(
                "Enter Olano vault passphrase (or press Ctrl-C to skip)",
                hide_input=True,
                default="",
                show_default=False,
            )
        except (click.Abort, KeyboardInterrupt, EOFError):
            return VaultUnlockResult(
                vault=None, mode="prompt", db_existed=db_existed, error="user-cancelled"
            )
        if not passphrase:
            return VaultUnlockResult(
                vault=None, mode="prompt", db_existed=db_existed, error="empty-passphrase"
            )
        try:
            vault = VaultStore.open(passphrase=passphrase, db_path=db_path, use_keyring=False)
            # Sanity check: decrypt an existing entry to detect a wrong passphrase early.
            try:
                _verify_vault_key(vault)
            except VaultError as decrypt_exc:
                last_error = str(decrypt_exc)
                if attempt < max_attempts:
                    click.echo(f"  Wrong passphrase ({decrypt_exc}). Try again.", err=True)
                    continue
                return VaultUnlockResult(
                    vault=None,
                    mode="prompt",
                    db_existed=db_existed,
                    error=last_error,
                )
            if register:
                set_vault(vault)
                # The operator just typed the working passphrase — re-arm any
                # configured auto-unlock that went stale (e.g. baked env key
                # after a rekey) so the next boot doesn't need the prompt.
                persist_auto_unlock_key(passphrase)
            return VaultUnlockResult(vault=vault, mode="prompt", db_existed=db_existed)
        except Exception as exc:
            last_error = str(exc)
            if attempt < max_attempts:
                click.echo(f"  Vault unlock failed: {exc}. Try again.", err=True)
                continue
    return VaultUnlockResult(vault=None, mode="prompt", db_existed=db_existed, error=last_error)


def hydrate_env_from_vault(
    vault: VaultStore | None,
    *,
    overwrite: bool = False,
) -> list[str]:
    """Copy credential values from the vault into os.environ.

    By default, existing env vars are preserved (so explicit `export FOO=...`
    in a shell wins over the vault). Returns the list of env var names
    populated from the vault. Runtime-owned entries (``vault/reserved.py``)
    never land in the environment: the process env is what every child
    process inherits, and the modules that need those values read the vault
    directly.
    """
    from olano.vault.reserved import without_runtime_secrets

    if vault is None:
        return []
    injected: list[str] = []
    try:
        entries = without_runtime_secrets(vault.list_names())
    except Exception as exc:
        logger.warning("Vault hydrate: list_names failed: %s", exc)
        return injected
    for entry in entries:
        name = entry["name"]
        if not overwrite and name in os.environ:
            continue
        try:
            value = vault.get(name)
        except Exception as exc:
            logger.warning("Vault hydrate: skipping %s — %s", name, exc)
            continue
        os.environ[name] = value
        injected.append(name)
    if injected:
        logger.debug("Vault hydrate: injected %d entries into env", len(injected))
    return injected


# Default-off: when set to "1", restores the legacy behaviour of copying every
# vault secret into the shared process environment at unlock/boot.
LEGACY_ENV_HYDRATE_FLAG = "LIONCLAW_LEGACY_ENV_HYDRATE"


def maybe_hydrate_env_from_vault(vault: VaultStore | None) -> list[str]:
    """Hydrate the global env from the vault **only** when explicitly opted in.

    Hardened default: secrets are NOT copied into the shared ``os.environ`` —
    toolkits read the vault per-agent via :mod:`olano.tools._credentials`, and
    stdio-MCP children get a scoped secret env at spawn (see
    :func:`resolve_agent_credentials`). Set ``LIONCLAW_LEGACY_ENV_HYDRATE=1`` to
    restore the old global hydration. Used by the automatic unlock/boot paths;
    the explicit ``/reload-vault`` command still hydrates directly.
    """
    if os.environ.get(LEGACY_ENV_HYDRATE_FLAG) == "1":
        return hydrate_env_from_vault(vault)
    return []


def resolve_agent_credentials(agent: str | None, vault: VaultStore | None = None) -> dict[str, str]:
    """Return an agent's full resolved secret set (common ∪ per-agent override).

    Override values win over common ones. Handed to a child process (a stdio-MCP
    server) as its env at spawn so it sees the running agent's credentials
    **without** the parent process having to carry them in ``os.environ``.
    Runtime-owned entries (``vault/reserved.py``) are left out: an MCP server
    is an arbitrary package, and the gateway key in its env is the deployment's
    credit balance in its env. Returns ``{}`` when the vault is unavailable.
    Never raises.
    """
    from olano.vault.reserved import is_runtime_secret

    if vault is None:
        from olano.vault.resolver import get_vault

        vault = get_vault()
    if vault is None:
        return {}
    resolved: dict[str, str] = {}
    try:
        names = {entry["name"] for entry in vault.list_names()}
        names |= {row["name"] for row in vault.list_overrides(agent=agent)}
        names = {name for name in names if not is_runtime_secret(name)}
    except Exception:
        return resolved
    for name in names:
        try:
            resolved[name] = vault.get(name, agent=agent, touch=False)
        except Exception:
            continue
    return resolved
