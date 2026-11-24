"""Credential vault: shared and per-agent secrets.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
from typing import Any, Optional

from fastapi import FastAPI, HTTPException

from olano.runtime import api as _api
from olano.runtime import auth as auth_layer
from olano.vault.reserved import RUNTIME_SECRET_REFUSAL, is_runtime_secret, without_runtime_secrets
from olano.vault.store import set_vault_key_env, vault_key_from_env


def _refuse_runtime_secret(name: str) -> None:
    """403 for a vault entry the runtime owns (``vault/reserved.py``).

    These rows never reach a listing, so a well-behaved client never sends
    their name; the refusal is the belt for a hand-made request, and for the
    override, set and delete paths, where a name is typed rather than picked.
    """
    if is_runtime_secret(name):
        raise HTTPException(status_code=403, detail=RUNTIME_SECRET_REFUSAL)


def register_vault_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the vault routes to ``app``."""
    # ── Vault (encrypted secrets) ─────────────────────────────────────────────

    def _vault_or_unlock():
        """Return the live VaultStore, unlocking it on demand if the runtime
        hasn't yet (e.g. dashboard daemon started before any agent booted).

        While the vault stays locked, every call re-attempts the unlock — a
        PBKDF2 key derivation (~0.5s CPU) — so async handlers must call this
        via asyncio.to_thread."""
        from olano.vault.resolver import get_vault, set_vault

        v = get_vault()
        if v is not None:
            return v
        from olano.vault.runtime import maybe_hydrate_env_from_vault, unlock_vault_for_session

        result = unlock_vault_for_session(allow_prompt=False)
        if result and result.vault is not None:
            set_vault(result.vault)
            # Hardened default: do NOT copy secrets into os.environ. Toolkits read
            # the vault per-agent and stdio-MCP children get a scoped env at spawn.
            # LIONCLAW_LEGACY_ENV_HYDRATE=1 restores the old global hydration.
            try:
                maybe_hydrate_env_from_vault(result.vault)
            except Exception:
                pass
            return result.vault
        return None

    @app.get("/api/vault/status")
    async def vault_status() -> dict:
        from olano.vault.runtime import secure_keyring_available

        v = await asyncio.to_thread(_vault_or_unlock)
        # has_vault: does an encrypted vault DB actually exist? (so the dashboard
        # only nags to unlock when there's something to unlock).
        has_vault = False
        try:
            from olano.vault.store import known_vault_paths

            has_vault = any(p.exists() for p in known_vault_paths())
        except Exception:
            pass
        # boot_key_file: a rekey on an env-unlocked box persisted the current
        # passphrase beside the vault DB (the baked env key is stale from then
        # on; the unlock chain falls back to this file at boot).
        boot_key_present = False
        try:
            from olano.vault.runtime import read_boot_key

            boot_key_present = read_boot_key() is not None
        except Exception:
            pass
        return {
            "unlocked": v is not None,
            "keyring_available": secure_keyring_available(),
            "env_key_set": bool(vault_key_from_env()),
            "boot_key_file": boot_key_present,
            "has_vault": has_vault,
        }

    @app.post("/api/vault/unlock")
    async def vault_unlock(body: dict) -> dict:
        """Unlock the vault for the current server session using a passphrase.
        Once unlocked, the dashboard and all running agents can access secrets."""
        passphrase = (body or {}).get("passphrase", "").strip()
        if not passphrase:
            raise HTTPException(status_code=400, detail="passphrase is required")
        from olano.vault.resolver import set_vault
        from olano.vault.runtime import maybe_hydrate_env_from_vault, unlock_vault_for_session

        # PBKDF2 key derivation (~0.5s CPU) — off the loop.
        result = await asyncio.to_thread(unlock_vault_for_session, passphrase=passphrase)
        if result and result.vault is not None:
            set_vault(result.vault)
            # Unlocking makes the vault available to per-agent resolution; it does
            # not pollute the global env unless LIONCLAW_LEGACY_ENV_HYDRATE=1.
            try:
                maybe_hydrate_env_from_vault(result.vault)
            except Exception:
                pass
            # Channels that booted while the vault was locked kept their stale
            # ${NAME} fallback; re-boot those agents now so they pick up the
            # freshly-resolved secrets without a manual restart.
            rebooted: list[str] = []
            try:
                rebooted = await runtime.reboot_unresolved_secret_agents()
            except Exception as exc:
                _api.logger.warning("Vault unlock: agent reboot pass failed: %s", exc)
            return {"ok": True, "mode": result.mode, "rebooted": rebooted}
        error = result.error if result else "Wrong passphrase or corrupt vault"
        raise HTTPException(status_code=401, detail=str(error) or "Wrong passphrase")

    @app.get("/api/vault/secrets")
    async def vault_list(
        category: Optional[str] = None,
        agent: Optional[str] = None,
        include_overrides: bool = False,
    ) -> list:
        v = await asyncio.to_thread(_vault_or_unlock)
        if v is None:
            raise HTTPException(status_code=503, detail="Vault is locked")
        # ?agent=X → just that agent's overrides. Otherwise the common secrets
        # (each tagged agent=None), optionally followed by every override so the
        # dashboard can group "common + per-agent overrides" in one call.
        # Runtime-owned rows are never listed (vault/reserved.py): the gateway
        # key is not something an admin manages, and listing it put Reveal,
        # Edit, Delete and Override under the deployment's own credential.
        if agent:
            return without_runtime_secrets(v.list_overrides(agent=agent))
        common = [
            {**row, "agent": None}
            for row in without_runtime_secrets(v.list_names(category=category))
        ]
        if include_overrides:
            return common + without_runtime_secrets(v.list_overrides())
        return common

    @app.post("/api/vault/secrets")
    async def vault_set(body: dict) -> dict:
        v = await asyncio.to_thread(_vault_or_unlock)
        if v is None:
            raise HTTPException(status_code=503, detail="Vault is locked")
        name = (body or {}).get("name", "").strip()
        value = (body or {}).get("value", "")
        agent = ((body or {}).get("agent") or "").strip() or None
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        _refuse_runtime_secret(name)
        if not isinstance(value, str) or value == "":
            raise HTTPException(status_code=400, detail="value is required")
        # BYOK gate: provider API keys can't be added when the plan excludes
        # BYOK (`features.byok`, Ultra+ only) or when model selection is
        # disabled for this deployment. Other vault secrets — channel tokens,
        # toolkit credentials — are unaffected.
        if name in _api._provider_env_vars():
            _api._reject_unless_byok_allowed()
        v.set(
            name=name,
            value=value,
            description=str(body.get("description", "")),
            category=str(body.get("category", "general")),
            expires_in_days=body.get("expires_in_days") or None,
            rotation_reminder_days=int(body.get("rotation_reminder_days") or 90),
            agent=agent,
        )
        return {"ok": True, "name": name, "agent": agent}

    @app.get("/api/vault/secrets/{name}/reveal")
    async def vault_reveal(name: str, agent: Optional[str] = None) -> dict:
        _refuse_runtime_secret(name)
        v = await asyncio.to_thread(_vault_or_unlock)
        if v is None:
            raise HTTPException(status_code=503, detail="Vault is locked")
        try:
            value = v.get(name, agent=agent or None)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"name": name, "value": value, "agent": agent or None}

    @app.delete("/api/vault/secrets/{name}")
    async def vault_delete(name: str, agent: Optional[str] = None) -> dict:
        _refuse_runtime_secret(name)
        v = await asyncio.to_thread(_vault_or_unlock)
        if v is None:
            raise HTTPException(status_code=503, detail="Vault is locked")
        ok = v.delete(name, agent=agent or None)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Secret '{name}' not found")
        return {"ok": True, "name": name, "agent": agent or None}

    @app.post("/api/vault/rekey")
    async def vault_rekey(body: dict) -> dict:
        """Re-encrypt the entire vault under a new passphrase.

        ``old_passphrase`` is optional **while the vault is open**. A managed
        box is provisioned with a temporary passphrase that lives in the cloud
        console and auto-unlocks here from the boot key, so the owner setting
        their own passphrase for the first time is being asked for a secret
        they have never seen — they have to go read it off another website to
        replace it. An open vault already proves key possession at the process
        level, and this route is admin-only (``_ADMIN_ONLY_PREFIXES``), so we
        rekey straight from the live store. A **locked** vault still demands
        the old passphrase: there, nothing has proved anything.

        The rekey is ``VaultStore.change_passphrase``: every ciphertext row in
        every table — common credentials, per-agent overrides, customer
        credentials — is re-encrypted **in place**, in one transaction, with
        expiry/rotation metadata untouched. (The previous copy-based rekey
        walked only the common table, so overrides and customer rows stayed
        under the dead key and every consumer swallowed the decrypt failure —
        an overridden key silently resolved to the operator's env var.) Rows
        already stranded under some earlier key are left as-is and reported in
        the response's ``skipped`` list — re-enter or delete those.

        On success the new passphrase becomes the active key and this box's
        *existing* auto-unlock material is refreshed with it (boot-key file
        when env-based unlock is configured — the managed-cloud posture, where
        the baked OLANO_VAULT_KEY can't be rewritten from here — and a stale
        keyring master key). The response's ``auto_unlock`` lists what was
        refreshed; when it's empty, unattended restarts need
        OLANO_VAULT_KEY=<new> exported manually.
        """
        from olano.vault.resolver import set_vault
        from olano.vault.store import VaultError, VaultStore, default_vault_path

        old = (body or {}).get("old_passphrase", "")
        new = (body or {}).get("new_passphrase", "")
        if not new:
            raise HTTPException(status_code=400, detail="new_passphrase is required")
        if len(new) < 8:
            raise HTTPException(
                status_code=400, detail="New passphrase must be at least 8 characters"
            )

        # VaultStore.open never verifies a passphrase (the key is a pure
        # function of it) — change_passphrase does, by decrypting every row:
        # rows present but none readable ⇒ VaultError, nothing written. That
        # closes the old hole where a wrong passphrase sailed through whenever
        # the common table happened to be empty.
        if old:
            store = await asyncio.to_thread(
                VaultStore.open, passphrase=old, db_path=default_vault_path(), use_keyring=False
            )
        else:
            store = await asyncio.to_thread(_vault_or_unlock)
            if store is None:
                raise HTTPException(
                    status_code=401,
                    detail="Vault is locked — enter the current passphrase to change it",
                )

        try:
            summary = await asyncio.to_thread(store.change_passphrase, new)
        except VaultError as exc:
            if old:
                raise HTTPException(
                    status_code=401, detail=f"Could not unlock with old passphrase: {exc}"
                ) from exc
            # The LIVE key failing on every row is corruption, not user error.
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        # Swap the resolver's vault reference; `store` now holds the new key.
        set_vault(store)
        # Keep unattended unlock working across restarts: refresh the box's
        # existing auto-unlock material (boot-key file / stale keyring entry)
        # with the new passphrase, and re-point the in-process env key. The
        # baked container env itself stays stale — the boot chain falls back to
        # the boot-key file when the env key no longer decrypts.
        from olano.vault.runtime import persist_auto_unlock_key

        auto_unlock = await asyncio.to_thread(persist_auto_unlock_key, new)
        if vault_key_from_env():
            set_vault_key_env(new)
        # The control plane's copy of the *temporary* vault passphrase is now a
        # dead credential — record + report the rotation (only the fact, never
        # the new key) so the cloud console purges it. Best-effort.
        try:
            from olano.deployment import credential_rotation

            credential_rotation.record_vault_password_rotation()
            credential_rotation.schedule_report()
        except Exception:  # pragma: no cover - must never break a rekey
            _api.logger.exception("credential-rotation note failed (vault rekey)")
        return {
            "ok": True,
            "rekeyed": summary["reencrypted"],
            "skipped": summary["skipped"],
            "auto_unlock": auto_unlock,
        }

    # ── Agent-scoped vault overrides (agent edit → Variables tab) ─────────────
    # Management surface for ONE agent's per-agent vault overrides. Unlike the
    # global-admin-only /api/vault/* prefix, these are gated in-handler by
    # assert_agent_admin — a global admin OR that agent's owner — so a non-admin
    # owner can manage their agent's variables without seeing the org-wide
    # vault. Plain members (chat-only grant) get 403 agent_admin_required. The
    # override scope is always forced from the path: this surface can never
    # read or write a common (shared) secret value.

    def _assert_agent_exists(name: str) -> None:
        try:
            _api._agent_config_path(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

    @app.get("/api/agents/{name}/vault/secrets")
    async def agent_vault_list(name: str) -> dict:
        """One agent's override rows plus the common key names it can override.

        ``common`` carries name-level metadata only (never values or usage
        stamps) — just enough for the "override a shared key" picker. A locked
        vault returns 200 with ``unlocked: false`` instead of the /api/vault
        503 convention: a non-admin owner cannot unlock the vault themselves,
        so the tab renders a notice rather than a dead-end error.
        """
        auth_layer.assert_agent_admin(name)
        _assert_agent_exists(name)
        v = await asyncio.to_thread(_vault_or_unlock)
        if v is None:
            return {"unlocked": False, "secrets": [], "common": []}
        common = [
            {
                "name": row.get("name", ""),
                "category": row.get("category") or "general",
                "description": row.get("description") or "",
            }
            for row in without_runtime_secrets(v.list_names())
        ]
        return {
            "unlocked": True,
            "secrets": without_runtime_secrets(v.list_overrides(agent=name)),
            "common": common,
        }

    @app.post("/api/agents/{name}/vault/secrets")
    async def agent_vault_set(name: str, body: dict) -> dict:
        """Create or update a per-agent override for this agent."""
        auth_layer.assert_agent_admin(name)
        _assert_agent_exists(name)
        v = await asyncio.to_thread(_vault_or_unlock)
        if v is None:
            raise HTTPException(status_code=503, detail="Vault is locked")
        key = str((body or {}).get("name", "")).strip()
        value = (body or {}).get("value", "")
        if not key:
            raise HTTPException(status_code=400, detail="name is required")
        _refuse_runtime_secret(key)
        if not isinstance(value, str) or value == "":
            raise HTTPException(status_code=400, detail="value is required")
        # Same BYOK gate as the global vault POST: provider API keys stay
        # blocked when the plan excludes BYOK or model selection is disabled.
        if key in _api._provider_env_vars():
            _api._reject_unless_byok_allowed()
        v.set(
            name=key,
            value=value,
            description=str((body or {}).get("description", "")),
            expires_in_days=(body or {}).get("expires_in_days") or None,
            rotation_reminder_days=int((body or {}).get("rotation_reminder_days") or 90),
            agent=name,
        )
        return {"ok": True, "name": key, "agent": name}

    @app.get("/api/agents/{name}/vault/secrets/{key}/reveal")
    async def agent_vault_reveal(name: str, key: str) -> dict:
        """Reveal THIS agent's override value — strictly the override row.

        Never falls back to the common value (``VaultStore.get`` would), so an
        agent owner cannot read shared org-wide secrets through their agent.
        """
        auth_layer.assert_agent_admin(name)
        _assert_agent_exists(name)
        _refuse_runtime_secret(key)
        v = await asyncio.to_thread(_vault_or_unlock)
        if v is None:
            raise HTTPException(status_code=503, detail="Vault is locked")
        try:
            value = v.get_override(key, name)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if value is None:
            raise HTTPException(status_code=404, detail=f"No '{key}' override for agent '{name}'")
        return {"name": key, "value": value, "agent": name}

    @app.delete("/api/agents/{name}/vault/secrets/{key}")
    async def agent_vault_delete(name: str, key: str) -> dict:
        """Delete THIS agent's override (the common value is untouchable here)."""
        auth_layer.assert_agent_admin(name)
        _assert_agent_exists(name)
        _refuse_runtime_secret(key)
        v = await asyncio.to_thread(_vault_or_unlock)
        if v is None:
            raise HTTPException(status_code=503, detail="Vault is locked")
        if not v.delete(key, agent=name):
            raise HTTPException(status_code=404, detail=f"No '{key}' override for agent '{name}'")
        return {"ok": True, "name": key, "agent": name}

    # Closures that later route modules bind (see routes/__init__.py).
    closures["_vault_or_unlock"] = _vault_or_unlock
