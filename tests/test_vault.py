"""Tests for olano.vault.*"""

from __future__ import annotations

import pytest


@pytest.fixture
def vault(tmp_path):
    from olano.vault.store import VaultStore

    return VaultStore.open(passphrase="test-passphrase-123", db_path=tmp_path / "vault.db")


class TestVaultStore:
    def test_set_and_get(self, vault):
        vault.set("MY_KEY", "super-secret-value")
        result = vault.get("MY_KEY")
        assert result == "super-secret-value"

    def test_get_missing_raises(self, vault):
        from olano.vault.store import VaultError

        with pytest.raises(VaultError, match="not found"):
            vault.get("NONEXISTENT_KEY")

    def test_delete(self, vault):
        from olano.vault.store import VaultError

        vault.set("TO_DELETE", "value")
        deleted = vault.delete("TO_DELETE")
        assert deleted is True

        with pytest.raises(VaultError):
            vault.get("TO_DELETE")

    def test_delete_nonexistent_returns_false(self, vault):
        assert vault.delete("NOPE") is False

    def test_list_names(self, vault):
        vault.set("KEY_A", "val_a", description="Key A", category="api")
        vault.set("KEY_B", "val_b", category="oauth")

        names = vault.list_names()
        assert len(names) == 2
        # Values should NOT be returned
        for entry in names:
            assert "val_a" not in str(entry)
            assert "val_b" not in str(entry)

    def test_list_names_by_category(self, vault):
        vault.set("API_KEY", "val", category="api")
        vault.set("DB_PASS", "val2", category="database")

        api_keys = vault.list_names(category="api")
        assert len(api_keys) == 1
        assert api_keys[0]["name"] == "API_KEY"

    def test_overwrite_key(self, vault):
        vault.set("KEY", "original")
        vault.set("KEY", "updated")
        assert vault.get("KEY") == "updated"

    def test_different_passphrase_fails(self, tmp_path):
        from olano.vault.store import VaultError, VaultStore

        db = tmp_path / "vault2.db"
        vault1 = VaultStore.open(passphrase="correct-pass", db_path=db)
        vault1.set("SECRET", "my-value")

        vault2 = VaultStore.open(passphrase="wrong-pass", db_path=db)
        with pytest.raises((VaultError, Exception)):
            vault2.get("SECRET")

    def test_unlock_for_session_rejects_wrong_passphrase(self, tmp_path, monkeypatch):
        """A wrong passphrase must fail at unlock time, not silently 'succeed'
        and only break later on the first reveal."""
        from olano.vault import runtime
        from olano.vault.store import VaultStore

        db = tmp_path / "vault.db"
        monkeypatch.setattr(runtime, "default_vault_path", lambda: db)
        VaultStore.open(passphrase="correct-pass", db_path=db).set("SECRET", "v")

        ok = runtime.unlock_vault_for_session(passphrase="correct-pass", register=False)
        assert ok.vault is not None and ok.error is None

        bad = runtime.unlock_vault_for_session(passphrase="wrong-pass", register=False)
        assert bad.vault is None
        assert bad.error

    def test_unlock_for_session_allows_empty_vault(self, tmp_path, monkeypatch):
        """An empty vault has no entry to validate against — any passphrase
        becomes the de-facto key, so unlock should succeed."""
        from olano.vault import runtime
        from olano.vault.store import VaultStore

        db = tmp_path / "vault.db"
        monkeypatch.setattr(runtime, "default_vault_path", lambda: db)
        VaultStore.open(passphrase="any", db_path=db)  # create empty db

        res = runtime.unlock_vault_for_session(passphrase="whatever", register=False)
        assert res.vault is not None and res.error is None

    def test_needs_rotation_empty(self, vault):
        """No credentials — empty rotation list."""
        result = vault.needs_rotation()
        assert result == []

    def test_needs_rotation_fresh_key(self, vault):
        """Freshly stored key should not need rotation."""
        vault.set("NEW_KEY", "value", rotation_reminder_days=90)
        result = vault.needs_rotation()
        assert "NEW_KEY" not in result

    def test_category_stored(self, vault):
        vault.set("KEY", "value", category="finance")
        names = vault.list_names()
        entry = next(e for e in names if e["name"] == "KEY")
        assert entry["category"] == "finance"

    def test_description_stored(self, vault):
        vault.set("KEY", "value", description="My important key")
        names = vault.list_names()
        entry = names[0]
        assert entry["description"] == "My important key"

    def test_env_var_passphrase(self, tmp_path, monkeypatch):
        """LIONCLAW_VAULT_KEY env var is used when no passphrase given."""
        from olano.vault.store import VaultStore

        monkeypatch.setenv("LIONCLAW_VAULT_KEY", "env-derived-key-abc123xyz")  # gitleaks:allow
        vault = VaultStore.open(db_path=tmp_path / "env_vault.db", use_keyring=False)
        vault.set("ENV_SECRET", "hello")
        assert vault.get("ENV_SECRET") == "hello"


class TestChangePassphrase:
    """One key encrypts all three tables, so a rekey must walk all three —
    the old endpoint-side copy walked only `credentials`, stranding per-agent
    overrides and customer rows under the dead key (and, by round-tripping
    through set(), nulled every expiry and reset rotation_reminder_days)."""

    OLD = "old-passphrase-123"
    NEW = "new-passphrase-456"

    def _seed_all_tables(self, tmp_path):
        from olano.vault.store import VaultStore

        db = tmp_path / "vault.db"
        v = VaultStore.open(passphrase=self.OLD, db_path=db)
        v.set(
            "SVC_TOKEN",
            "common-value",
            description="svc",
            category="api",
            expires_in_days=30,
            rotation_reminder_days=7,
        )
        v.set("SVC_TOKEN", "alice-value", agent="alice", expires_in_days=5)
        v.set_customer("CRM_KEY", agent="support", customer="cust-1", value="cust-value")
        return v, db

    def test_rekeys_every_table_in_place(self, tmp_path):
        from olano.vault.store import VaultStore

        v, db = self._seed_all_tables(tmp_path)
        summary = v.change_passphrase(self.NEW)

        assert summary == {"reencrypted": 3, "skipped": []}
        # The live object keeps working under the new key…
        assert v.get("SVC_TOKEN") == "common-value"
        # …and so does a fresh open with the new passphrase, for all tables.
        reopened = VaultStore.open(passphrase=self.NEW, db_path=db)
        assert reopened.get("SVC_TOKEN") == "common-value"
        assert reopened.get_override("SVC_TOKEN", "alice") == "alice-value"
        assert reopened.get_customer("CRM_KEY", "support", "cust-1") == "cust-value"

    def test_old_passphrase_stops_working(self, tmp_path):
        from olano.vault.store import VaultError, VaultStore

        v, db = self._seed_all_tables(tmp_path)
        v.change_passphrase(self.NEW)

        stale = VaultStore.open(passphrase=self.OLD, db_path=db)
        with pytest.raises(VaultError):
            stale.get("SVC_TOKEN")
        with pytest.raises(VaultError):
            stale.get_override("SVC_TOKEN", "alice")

    def test_metadata_survives(self, tmp_path):
        """In-place ciphertext update ⇒ expiry and rotation settings are not
        rewritten. The copy-based rekey nulled expires_at (set() has no
        absolute-expiry parameter) and reset rotation_reminder_days to 90."""
        import sqlite3

        v, db = self._seed_all_tables(tmp_path)
        before = v.list_names()[0]
        v.change_passphrase(self.NEW)

        after = v.list_names()[0]
        assert before["expires_at"] is not None
        assert after["expires_at"] == before["expires_at"]
        assert after["created_at"] == before["created_at"]
        assert v.list_overrides()[0]["expires_at"] is not None
        # rotation_reminder_days isn't in list_names(); check the column.
        row = (
            sqlite3.connect(db)
            .execute("SELECT rotation_reminder_days FROM credentials WHERE name='SVC_TOKEN'")
            .fetchone()
        )
        assert row[0] == 7

    def test_wrong_current_key_raises_and_writes_nothing(self, tmp_path):
        from olano.vault.store import VaultError, VaultStore

        _, db = self._seed_all_tables(tmp_path)
        wrong = VaultStore.open(passphrase="not-the-passphrase", db_path=db)

        with pytest.raises(VaultError):
            wrong.change_passphrase(self.NEW)
        # Nothing was rewritten: the real passphrase still opens everything.
        good = VaultStore.open(passphrase=self.OLD, db_path=db)
        assert good.get("SVC_TOKEN") == "common-value"
        assert good.get_customer("CRM_KEY", "support", "cust-1") == "cust-value"

    def test_wrong_key_raises_even_when_only_overrides_exist(self, tmp_path):
        """The hole the old path had: an empty common table meant zero decrypt
        probes, so a wrong passphrase sailed straight through."""
        from olano.vault.store import VaultError, VaultStore

        db = tmp_path / "vault.db"
        v = VaultStore.open(passphrase=self.OLD, db_path=db)
        v.set("ONLY_OVERRIDE", "alice-value", agent="alice")
        assert v.list_names() == []  # nothing in the common table

        wrong = VaultStore.open(passphrase="not-the-passphrase", db_path=db)
        with pytest.raises(VaultError):
            wrong.change_passphrase(self.NEW)

    def test_already_stranded_rows_are_skipped_not_fatal(self, tmp_path):
        """A vault damaged by the old partial rekey must stay rotatable: the
        unreadable row is reported by identity and left untouched, everything
        else moves to the new key."""
        import sqlite3

        from olano.vault.store import VaultStore

        v, db = self._seed_all_tables(tmp_path)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE credential_overrides SET ciphertext=? WHERE agent='alice'",
                (b"lcv2:garbage-not-decryptable-by-anything",),
            )

        summary = v.change_passphrase(self.NEW)

        assert summary["reencrypted"] == 2
        assert summary["skipped"] == [
            {"table": "credential_overrides", "name": "SVC_TOKEN", "agent": "alice"}
        ]
        reopened = VaultStore.open(passphrase=self.NEW, db_path=db)
        assert reopened.get("SVC_TOKEN") == "common-value"
        assert reopened.get_customer("CRM_KEY", "support", "cust-1") == "cust-value"

    def test_empty_vault_rekeys_trivially(self, tmp_path):
        from olano.vault.store import VaultStore

        v = VaultStore.open(passphrase=self.OLD, db_path=tmp_path / "vault.db")
        assert v.change_passphrase(self.NEW) == {"reencrypted": 0, "skipped": []}
        v.set("K", "v")
        assert v.get("K") == "v"


class TestVerifyKey:
    """Any row in any table is a valid probe: one key encrypts the whole
    vault, and a verification that only looked at the common table let a vault
    whose rows lived elsewhere 'unlock' under any passphrase — after which the
    first write under the wrong key locked the correct one out for good."""

    def test_right_key_passes_wrong_key_raises(self, tmp_path):
        from olano.vault.store import VaultError, VaultStore

        db = tmp_path / "vault.db"
        VaultStore.open(passphrase="right-pass", db_path=db).set("K", "v")

        VaultStore.open(passphrase="right-pass", db_path=db).verify_key()
        with pytest.raises(VaultError):
            VaultStore.open(passphrase="wrong-pass", db_path=db).verify_key()

    @pytest.mark.parametrize("kind", ["override", "customer"])
    def test_probes_whichever_table_has_rows(self, tmp_path, kind):
        from olano.vault.store import VaultError, VaultStore

        db = tmp_path / "vault.db"
        v = VaultStore.open(passphrase="right-pass", db_path=db)
        if kind == "override":
            v.set("K", "alice-v", agent="alice")
        else:
            v.set_customer("K", agent="support", customer="cust-1", value="cust-v")
        assert v.list_names() == []  # nothing in the common table

        VaultStore.open(passphrase="right-pass", db_path=db).verify_key()
        with pytest.raises(VaultError):
            VaultStore.open(passphrase="wrong-pass", db_path=db).verify_key()

    def test_empty_vault_passes(self, tmp_path):
        from olano.vault.store import VaultStore

        VaultStore.open(passphrase="anything", db_path=tmp_path / "vault.db").verify_key()


class TestVaultResolver:
    def test_resolve_no_refs(self):
        from olano.vault.resolver import resolve_vault_refs, set_vault

        set_vault(None)
        result = resolve_vault_refs("no references here")
        assert result == "no references here"

    def test_resolve_with_vault(self, vault):
        from olano.vault.resolver import resolve_vault_refs, set_vault

        vault.set("API_TOKEN", "tok-abc123")
        set_vault(vault)

        result = resolve_vault_refs("Authorization: Bearer ${vault:API_TOKEN}")
        assert result == "Authorization: Bearer tok-abc123"

    def test_resolve_env_fallback(self, monkeypatch):
        from olano.vault.resolver import resolve_vault_refs, set_vault

        set_vault(None)
        monkeypatch.setenv("MY_ENV_KEY", "env-value")
        result = resolve_vault_refs("key=${vault:MY_ENV_KEY}")
        assert result == "key=env-value"

    def test_resolve_missing_stays_as_is(self):
        from olano.vault.resolver import resolve_vault_refs, set_vault

        set_vault(None)
        original = "${vault:MISSING_KEY}"
        result = resolve_vault_refs(original)
        # Should keep the original reference
        assert result == original

    def test_resolve_config_dict(self, vault):
        from olano.vault.resolver import resolve_config_dict, set_vault

        vault.set("DB_PASSWORD", "secret123")
        set_vault(vault)

        config = {
            "host": "localhost",
            "password": "${vault:DB_PASSWORD}",
            "port": 5432,
            "nested": {"key": "${vault:DB_PASSWORD}"},
        }
        resolved = resolve_config_dict(config)
        assert resolved["password"] == "secret123"
        assert resolved["nested"]["key"] == "secret123"
        assert resolved["host"] == "localhost"
        assert resolved["port"] == 5432

    def test_resolve_bare_placeholder_from_vault(self, vault):
        """A bare ${NAME} placeholder (plain env syntax) resolves from the vault.

        agent.json5 channel tokens use ``${TELEGRAM_TOKEN_KEVIN}`` (not the
        ``${vault:...}`` form). Since secrets are no longer hydrated into
        os.environ, the bare placeholder must still resolve from the vault.
        """
        from olano.vault.resolver import resolve_vault_refs, set_vault

        vault.set("TELEGRAM_TOKEN_KEVIN", "860072:zxFM")
        set_vault(vault)
        assert resolve_vault_refs("${TELEGRAM_TOKEN_KEVIN}") == "860072:zxFM"

    def test_bare_placeholder_vault_beats_env(self, vault, monkeypatch):
        """Vault wins over os.environ for a bare ${NAME} (vault is higher priority)."""
        from olano.vault.resolver import resolve_vault_refs, set_vault

        vault.set("DUAL_KEY", "from-vault")
        set_vault(vault)
        monkeypatch.setenv("DUAL_KEY", "from-env")
        assert resolve_vault_refs("${DUAL_KEY}") == "from-vault"

    def test_bare_placeholder_env_fallback(self, monkeypatch):
        """When the vault lacks a bare ${NAME}, fall back to os.environ."""
        from olano.vault.resolver import resolve_vault_refs, set_vault

        set_vault(None)
        monkeypatch.setenv("ONLY_ENV", "env-only")
        assert resolve_vault_refs("${ONLY_ENV}") == "env-only"

    def test_bare_placeholder_missing_stays_literal(self, monkeypatch):
        """An unresolved bare ${NAME} is left untouched so callers can flag it."""
        from olano.vault.resolver import resolve_vault_refs, set_vault

        set_vault(None)
        monkeypatch.delenv("NOPE_MISSING_TOKEN", raising=False)
        assert resolve_vault_refs("${NOPE_MISSING_TOKEN}") == "${NOPE_MISSING_TOKEN}"

    def test_bare_placeholder_does_not_touch_vault_ref(self, monkeypatch):
        """The bare-${NAME} pass must not mangle an unresolved ${vault:NAME}."""
        from olano.vault.resolver import resolve_vault_refs, set_vault

        set_vault(None)
        monkeypatch.delenv("MISSING_KEY", raising=False)
        # ${vault:MISSING_KEY} contains a colon → must stay verbatim, not be
        # half-consumed by the bare-placeholder regex.
        assert resolve_vault_refs("${vault:MISSING_KEY}") == "${vault:MISSING_KEY}"

    def test_resolve_config_dict_bare_with_agent_override(self, vault):
        """resolve_config_dict threads the agent through for per-agent overrides."""
        from olano.vault.resolver import resolve_config_dict, set_vault

        vault.set("TELEGRAM_TOKEN_KEVIN", "common-tok")
        vault.set("TELEGRAM_TOKEN_KEVIN", "kevin-tok", agent="kevin")
        set_vault(vault)

        cfg = {"token": "${TELEGRAM_TOKEN_KEVIN}", "allowed_users": ["1"]}
        assert resolve_config_dict(cfg, agent="kevin")["token"] == "kevin-tok"
        # No agent → common value.
        assert resolve_config_dict(cfg)["token"] == "common-tok"
