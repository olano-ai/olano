"""Encrypted credential store backed by SQLite.

Credentials are encrypted using AES-256-GCM.
Legacy Fernet-encrypted entries are migrated in place on read.
The master key is derived from a passphrase using PBKDF2 or fetched from
the system keyring (if available).

Never stores credentials in prompt, memory, or logs.
Injects them only at tool-call time via VaultStore.get().
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from olano.config.home import get_olano_paths
from olano.vault.keyring_backend import secure_keyring_available

logger = logging.getLogger(__name__)
# AES-GCM additional authenticated data. NEVER rename this — it is not a
# brand string, it is authenticated input baked into every secret already
# encrypted on disk. Changing it makes GCM tag verification fail on every
# existing vault, which surfaces as "Decryption failed — wrong passphrase or
# corrupted data" on a vault whose passphrase is perfectly correct.
_VAULT_ASSOCIATED_DATA = b"lionclaw.vault.v2"
_VAULT_V2_PREFIX = b"lcv2:"

# The environment variable that unlocks the vault unattended. ``OLANO_VAULT_KEY``
# is the name every message, form and deploy file shows; ``LIONCLAW_VAULT_KEY``
# is what older boxes still carry in their container env and ``.env``, and
# both are read for good. ``env_aliases`` mirrors the two at process start,
# but a reader cannot rely on that alone: a value set after import (a test's
# monkeypatch, a daemon's child env) reaches only the spelling it was set
# under, so every reader goes through :func:`vault_key_from_env`, and every
# writer sets both (:func:`set_vault_key_env`).
VAULT_KEY_ENV = "OLANO_VAULT_KEY"
LEGACY_VAULT_KEY_ENV = "LIONCLAW_VAULT_KEY"
VAULT_KEY_ENV_NAMES: tuple[str, str] = (VAULT_KEY_ENV, LEGACY_VAULT_KEY_ENV)


def vault_key_from_env(env: os._Environ[str] | dict[str, str] | None = None) -> str:
    """The vault key set in the environment, or ``""``. Canonical name first."""
    source = os.environ if env is None else env
    for name in VAULT_KEY_ENV_NAMES:
        value = (source.get(name) or "").strip()
        if value:
            return value
    return ""


def set_vault_key_env(value: str, env: os._Environ[str] | dict[str, str] | None = None) -> None:
    """Write the vault key under both spellings, so every reader sees it."""
    target = os.environ if env is None else env
    for name in VAULT_KEY_ENV_NAMES:
        target[name] = value


_SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    name TEXT PRIMARY KEY,
    ciphertext BLOB NOT NULL,
    description TEXT DEFAULT '',
    category TEXT DEFAULT 'general',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    rotation_reminder_days INTEGER DEFAULT 90,
    last_used TEXT
);
CREATE TABLE IF NOT EXISTS credential_overrides (
    name TEXT NOT NULL,
    agent TEXT NOT NULL,
    ciphertext BLOB NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    rotation_reminder_days INTEGER DEFAULT 90,
    last_used TEXT,
    PRIMARY KEY (name, agent)
);
CREATE TABLE IF NOT EXISTS customer_credentials (
    agent TEXT NOT NULL,
    customer TEXT NOT NULL,
    name TEXT NOT NULL,
    ciphertext BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (agent, customer, name)
);
CREATE TABLE IF NOT EXISTS vault_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class VaultError(Exception):
    """Raised for vault access or decryption errors."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class VaultStore:
    """
    Encrypted credential store.

    Usage::

        vault = VaultStore.open(passphrase="my-secret")
        vault.set("OPENAI_API_KEY", "sk-...")
        key = vault.get("OPENAI_API_KEY")
    """

    def __init__(self, db_path: Path, _key: bytes, _legacy_key: bytes | None = None) -> None:
        self._db_path = db_path
        self._key = _key
        self._legacy_key = _legacy_key
        self._init_db()

    # ──────────────────────────────────── Factory ─────────────────────────────

    @classmethod
    def open(
        cls,
        passphrase: str | None = None,
        db_path: Path | None = None,
        use_keyring: bool = True,
    ) -> VaultStore:
        """Open (or create) the vault with the given passphrase."""
        path = db_path or default_vault_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        secret = passphrase or cls._get_or_create_keyring_key(use_keyring)
        key = cls._derive_key(secret)
        legacy_key = cls._derive_legacy_key(secret)
        return cls(db_path=path, _key=key, _legacy_key=legacy_key)

    @classmethod
    def _get_or_create_keyring_key(cls, use_keyring: bool) -> str:
        """Get or create a random master key from env var, boot-key file or system keyring."""
        env_key = vault_key_from_env()
        if env_key:
            return env_key
        boot_key = read_boot_key()
        if boot_key:
            return boot_key

        # Only persist to the keyring when a SECURE OS backend is active (macOS
        # Keychain, Linux SecretService/KWallet, Windows). On headless Linux this
        # is False, so we never write the master key to an insecure plaintext file
        # backend — we fall through to the ephemeral key instead.
        if use_keyring and secure_keyring_available():
            try:
                import keyring

                # Use the shared constant, never a copy: this is the lookup key
                # for master keys already in the OS keychain, and a second
                # literal here silently drifted from keyring_backend's during
                # the rename — finding nothing and minting an ephemeral key.
                from olano.vault.keyring_backend import _VAULT_KEYRING_SERVICE

                existing = keyring.get_password(_VAULT_KEYRING_SERVICE, "vault_master_key")
                if existing:
                    return cast(str, existing)
                # Generate and store a new random master key in the OS keychain.
                new_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
                keyring.set_password(_VAULT_KEYRING_SERVICE, "vault_master_key", new_key)
                logger.info("Vault: new master key stored in system keyring")
                return new_key
            except Exception as e:
                logger.warning("Vault: keyring unavailable: %s", e)

        # Fallback: generate ephemeral key (vault only works this session)
        logger.warning(
            "Vault: no OLANO_VAULT_KEY and no secure system keyring — using an "
            "ephemeral key (this session only). Set OLANO_VAULT_KEY for persistence "
            "(required on headless servers / cloud), or install 'keyring' on a desktop OS."
        )
        return base64.urlsafe_b64encode(os.urandom(32)).decode()

    @staticmethod
    def _derive_key(passphrase: str) -> bytes:
        """Derive a 32-byte AES-256-GCM key from passphrase."""
        salt = b"lionclaw_vault_v2"
        return hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, iterations=600_000)

    @staticmethod
    def _derive_legacy_key(passphrase: str) -> bytes:
        salt = b"lionclaw_vault_v1"
        dk = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, iterations=100_000)
        return base64.urlsafe_b64encode(dk)

    # ──────────────────────────────────── CRUD ────────────────────────────────

    def set(
        self,
        name: str,
        value: str,
        description: str = "",
        category: str = "general",
        expires_in_days: int | None = None,
        rotation_reminder_days: int = 90,
        agent: str | None = None,
    ) -> None:
        """Store or update a credential.

        ``agent=None`` (the default) writes the **common** value (shared by every
        agent). A non-empty ``agent`` writes a **per-agent override** that wins
        over the common value for that agent only (``credential_overrides`` table;
        ``category`` does not apply to overrides — they inherit the logical key).
        """
        ciphertext = self._encrypt(value)
        now = _utc_now().isoformat()
        expires_at = None
        if expires_in_days:
            expires_at = (_utc_now() + timedelta(days=expires_in_days)).isoformat()

        with self._conn() as conn:
            if agent:
                conn.execute(
                    """INSERT INTO credential_overrides (name, agent, ciphertext, description,
                       created_at, updated_at, expires_at, rotation_reminder_days)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(name, agent) DO UPDATE SET
                       ciphertext=excluded.ciphertext,
                       description=excluded.description,
                       updated_at=excluded.updated_at,
                       expires_at=excluded.expires_at,
                       rotation_reminder_days=excluded.rotation_reminder_days
                    """,
                    (
                        name,
                        agent,
                        ciphertext,
                        description,
                        now,
                        now,
                        expires_at,
                        rotation_reminder_days,
                    ),
                )
                logger.debug("Vault: stored override '%s' for agent '%s'", name, agent)
            else:
                conn.execute(
                    """INSERT INTO credentials (name, ciphertext, description, category,
                       created_at, updated_at, expires_at, rotation_reminder_days)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(name) DO UPDATE SET
                       ciphertext=excluded.ciphertext,
                       description=excluded.description,
                       updated_at=excluded.updated_at,
                       expires_at=excluded.expires_at,
                       rotation_reminder_days=excluded.rotation_reminder_days
                    """,
                    (
                        name,
                        ciphertext,
                        description,
                        category,
                        now,
                        now,
                        expires_at,
                        rotation_reminder_days,
                    ),
                )
                logger.debug("Vault: stored credential '%s'", name)

    def get(self, name: str, agent: str | None = None, *, touch: bool = True) -> str:
        """Retrieve and decrypt a credential, resolving per-agent overrides.

        When ``agent`` is set and a (non-expired) override exists for it, that
        value wins; otherwise the common value is returned. ``touch=False`` skips
        the ``last_used`` write — used by the high-frequency per-agent resolver so
        toolkit calls don't write to the vault on every read. Raises VaultError if
        neither an override nor a common value exists.
        """
        # 1. Per-agent override takes precedence (a lapsed override falls through
        #    to the common value rather than failing).
        if agent:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT ciphertext, expires_at FROM credential_overrides "
                    "WHERE name=? AND agent=?",
                    (name, agent),
                ).fetchone()
            if row:
                ciphertext, expires_at = row
                if not (expires_at and _parse_timestamp(expires_at) < _utc_now()):
                    if touch:
                        with self._conn() as conn:
                            conn.execute(
                                "UPDATE credential_overrides SET last_used=? "
                                "WHERE name=? AND agent=?",
                                (_utc_now().isoformat(), name, agent),
                            )
                    return self._decrypt(ciphertext)

        # 2. Common value.
        with self._conn() as conn:
            row = conn.execute(
                "SELECT ciphertext, expires_at FROM credentials WHERE name=?", (name,)
            ).fetchone()

        if not row:
            raise VaultError(f"Credential '{name}' not found in vault")

        ciphertext, expires_at = row
        if expires_at and _parse_timestamp(expires_at) < _utc_now():
            raise VaultError(f"Credential '{name}' has expired")

        if touch:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE credentials SET last_used=? WHERE name=?",
                    (_utc_now().isoformat(), name),
                )

        plaintext = self._decrypt(ciphertext)
        if isinstance(ciphertext, bytes | bytearray) and not bytes(ciphertext).startswith(
            _VAULT_V2_PREFIX
        ):
            with self._conn() as conn:
                conn.execute(
                    "UPDATE credentials SET ciphertext=?, updated_at=? WHERE name=?",
                    (self._encrypt(plaintext), _utc_now().isoformat(), name),
                )
        return plaintext

    def get_override(self, name: str, agent: str, *, touch: bool = False) -> str | None:
        """Decrypt one agent's override for ``name``, or ``None`` when no
        override row exists.

        Unlike :meth:`get`, this NEVER falls back to the common value — it is
        the read path for agent-scoped management surfaces (the agent edit
        "Variables" tab), where an agent owner must only ever see values stored
        for their agent, not the shared org-wide secrets. An expired override is
        still returned: the row exists and its owner needs to see what's stored
        to rotate it (expiry gates resolution in ``get()``, not management).
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT ciphertext FROM credential_overrides WHERE name=? AND agent=?",
                (name, agent),
            ).fetchone()
        if not row:
            return None
        if touch:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE credential_overrides SET last_used=? WHERE name=? AND agent=?",
                    (_utc_now().isoformat(), name, agent),
                )
        return self._decrypt(row[0])

    # ── per-customer credentials (multi-user agents) ─────────────────────────
    # One END-CUSTOMER's own secret for one agent (e.g. the customer's own
    # Google Sheets token on a bookkeeping agent), keyed by the customer's
    # folder key (<platform>-<id>) from the authenticated request context.
    # Deliberately a separate namespace from `credentials`/`credential_overrides`:
    # a customer's secret must never shadow — or be shadowed by — an operator
    # secret, and `agent_secret_env` (stdio-MCP children) reads only the agent
    # tables, so customer secrets can never leak into a long-lived per-agent
    # child process that serves every customer.

    def set_customer(self, name: str, agent: str, customer: str, value: str) -> None:
        """Store or update one customer's own credential (encrypted at rest)."""
        now = _utc_now().isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO customer_credentials (agent, customer, name, ciphertext,
                   created_at, updated_at) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(agent, customer, name) DO UPDATE SET
                   ciphertext=excluded.ciphertext, updated_at=excluded.updated_at""",
                (agent, customer, name, self._encrypt(value), now, now),
            )

    def get_customer(self, name: str, agent: str, customer: str) -> str | None:
        """One customer's credential, or None. NEVER falls back to the agent
        or common tables — that fallback decision belongs to the resolver,
        which fails closed for declared per-customer names."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT ciphertext FROM customer_credentials"
                " WHERE agent=? AND customer=? AND name=?",
                (agent, customer, name),
            ).fetchone()
        return self._decrypt(row[0]) if row else None

    def delete_customer(self, name: str, agent: str, customer: str) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM customer_credentials WHERE agent=? AND customer=? AND name=?",
                (agent, customer, name),
            )
            return cursor.rowcount > 0

    def list_customer_names(self, agent: str, customer: str) -> list[dict]:
        """Names + timestamps of one customer's stored credentials (no values)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name, created_at, updated_at FROM customer_credentials"
                " WHERE agent=? AND customer=? ORDER BY name",
                (agent, customer),
            ).fetchall()
        return [{"name": r[0], "created_at": r[1], "updated_at": r[2]} for r in rows]

    def erase_customer(self, agent: str, customer: str) -> int:
        """forget_me hook: drop every credential one customer stored."""
        with self._conn() as conn:
            return conn.execute(
                "DELETE FROM customer_credentials WHERE agent=? AND customer=?",
                (agent, customer),
            ).rowcount

    def delete(self, name: str, agent: str | None = None) -> bool:
        """Delete the common value (``agent=None``) or one agent's override."""
        with self._conn() as conn:
            if agent:
                cursor = conn.execute(
                    "DELETE FROM credential_overrides WHERE name=? AND agent=?", (name, agent)
                )
            else:
                cursor = conn.execute("DELETE FROM credentials WHERE name=?", (name,))
            return cursor.rowcount > 0

    def list_names(self, category: str | None = None) -> list[dict]:
        """List credential metadata (never returns plaintext values)."""
        with self._conn() as conn:
            if category:
                rows = conn.execute(
                    "SELECT name, description, category, created_at, updated_at, expires_at, last_used "
                    "FROM credentials WHERE category=?",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT name, description, category, created_at, updated_at, expires_at, last_used "
                    "FROM credentials"
                ).fetchall()
        return [
            {
                "name": r[0],
                "description": r[1],
                "category": r[2],
                "created_at": r[3],
                "updated_at": r[4],
                "expires_at": r[5],
                "last_used": r[6],
            }
            for r in rows
        ]

    def list_overrides(self, name: str | None = None, agent: str | None = None) -> list[dict]:
        """List per-agent override metadata (never returns plaintext values).

        Optionally filter by credential ``name`` and/or ``agent``. Each row is
        tagged with its ``agent`` so the dashboard can show, per common key, which
        agents override it.
        """
        query = (
            "SELECT name, agent, description, created_at, updated_at, expires_at, last_used "
            "FROM credential_overrides"
        )
        conds: list[str] = []
        params: list[str] = []
        if name:
            conds.append("name=?")
            params.append(name)
        if agent:
            conds.append("agent=?")
            params.append(agent)
        if conds:
            query += " WHERE " + " AND ".join(conds)
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "name": r[0],
                "agent": r[1],
                "description": r[2],
                "created_at": r[3],
                "updated_at": r[4],
                "expires_at": r[5],
                "last_used": r[6],
            }
            for r in rows
        ]

    def needs_rotation(self) -> list[str]:
        """Return names of credentials due for rotation."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name, updated_at, rotation_reminder_days FROM credentials"
            ).fetchall()

        due: list[str] = []
        for name, updated_at, days in rows:
            if not updated_at or not days:
                continue
            age = (_utc_now() - _parse_timestamp(updated_at)).days
            if age >= days:
                due.append(name)
        return due

    # ──────────────────────────────── Key verification ───────────────────────

    def verify_key(self) -> None:
        """Confirm the key this store was opened with decrypts its data.

        :meth:`open` only derives a key from the passphrase — it never checks
        it against stored ciphertext, so a wrong passphrase yields a
        valid-looking store that fails on the first read. Worse than failing:
        a wrongly-"unlocked" vault accepts *writes*, and the first secret
        written under the wrong key locks the correct passphrase out of its
        own vault (the probe then finds a row the right key can't decrypt).

        One key encrypts every table, so any row is a valid probe: decrypt one
        row from the first non-empty table (common credentials → per-agent
        overrides → customer credentials). Probing only the common table let a
        vault whose rows lived elsewhere validate against any passphrase.

        Raises :class:`VaultError` when the probe fails to decrypt. A vault
        with no rows anywhere passes — there is nothing to validate, and any
        key becomes the de-facto key on first write.
        """
        with self._conn() as conn:
            for table in ("credentials", "credential_overrides", "customer_credentials"):
                row = conn.execute(f"SELECT ciphertext FROM {table} LIMIT 1").fetchone()
                if row:
                    self._decrypt(row[0])
                    return

    # ──────────────────────────────────── Rekey ──────────────────────────────

    def change_passphrase(self, new_passphrase: str) -> dict:
        """Re-encrypt every ciphertext row under a key derived from
        ``new_passphrase``, **in place**, across all three tables — common
        ``credentials``, per-agent ``credential_overrides`` and
        ``customer_credentials``. One key encrypts the whole vault, so a rekey
        that misses a table strands its rows under the dead key; the previous
        endpoint-side copy did exactly that to overrides and customer rows, and
        (by round-tripping through :meth:`set`) also nulled every ``expires_at``
        and reset ``rotation_reminder_days``. Updating only the ciphertext blob
        leaves all metadata columns untouched.

        Read + write run in one ``BEGIN IMMEDIATE`` transaction, so a crash
        mid-rekey rolls back to a whole-old-key vault instead of a mixed one.

        A row that fails to decrypt (already stranded under some earlier key —
        the damage the old copy inflicted) is left as it is and reported in
        ``skipped`` by identity, never by value: it is unreadable either way,
        and refusing the whole rekey over it would make a damaged vault
        permanently un-rotatable. But if rows exist and NONE decrypt, this key
        never encrypted anything here — that is a wrong passphrase, and
        activating the new key would prove possession of nothing, so it raises
        instead (the check the old path skipped whenever ``credentials``
        happened to be empty).

        Returns ``{"reencrypted": int, "skipped": [{"table", ...id cols}]}``.
        On success the store object itself now holds the new key.
        """
        tables: tuple[tuple[str, tuple[str, ...]], ...] = (
            ("credentials", ("name",)),
            ("credential_overrides", ("name", "agent")),
            ("customer_credentials", ("agent", "customer", "name")),
        )
        # Derive before taking the write lock — PBKDF2 at 600k iterations is
        # ~0.5s of CPU that nothing else should wait on.
        new_key = self._derive_key(new_passphrase)
        new_legacy = self._derive_legacy_key(new_passphrase)

        reencrypt: list[tuple[str, int, str]] = []
        skipped: list[dict] = []
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for table, id_cols in tables:
                cols = ", ".join(id_cols)
                for row in conn.execute(f"SELECT rowid, ciphertext, {cols} FROM {table}"):
                    try:
                        plaintext = self._decrypt(row[1])
                    except VaultError:
                        skipped.append({"table": table, **dict(zip(id_cols, row[2:], strict=True))})
                        continue
                    reencrypt.append((table, row[0], plaintext))
            if skipped and not reencrypt:
                # Raising aborts the transaction (no commit on the way out).
                raise VaultError("Decryption failed — wrong passphrase or corrupted data")
            now = _utc_now().isoformat()
            for table, rowid, plaintext in reencrypt:
                conn.execute(
                    f"UPDATE {table} SET ciphertext = ?, updated_at = ? WHERE rowid = ?",
                    (self._encrypt(plaintext, key=new_key), now, rowid),
                )
        if skipped:
            logger.warning(
                "Vault rekey: %d row(s) were already unreadable and keep their old ciphertext: %s",
                len(skipped),
                skipped,
            )
        self._key = new_key
        self._legacy_key = new_legacy
        return {"reencrypted": len(reencrypt), "skipped": skipped}

    # ──────────────────────────────────── Crypto ─────────────────────────────

    def _encrypt(self, plaintext: str, *, key: bytes | None = None) -> bytes:
        use_key = self._key if key is None else key
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            nonce = os.urandom(12)
            ciphertext = AESGCM(use_key).encrypt(nonce, plaintext.encode(), _VAULT_ASSOCIATED_DATA)
            return _VAULT_V2_PREFIX + nonce + ciphertext
        except ImportError:
            # Fallback: simple XOR with key (NOT secure, just for dev)
            logger.warning(
                "Vault: 'cryptography' package not installed — using insecure fallback. "
                "Run: uv add cryptography"
            )
            return self._xor_encrypt(plaintext.encode(), use_key)

    def _decrypt(self, ciphertext: bytes) -> str:
        try:
            from cryptography.fernet import Fernet, InvalidToken
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            if ciphertext.startswith(_VAULT_V2_PREFIX):
                payload = ciphertext[len(_VAULT_V2_PREFIX) :]
                nonce = payload[:12]
                body = payload[12:]
                try:
                    return AESGCM(self._key).decrypt(nonce, body, _VAULT_ASSOCIATED_DATA).decode()
                except Exception as exc:
                    raise VaultError(
                        "Decryption failed — wrong passphrase or corrupted data"
                    ) from exc

            legacy_key = self._legacy_key or self._key
            f = Fernet(legacy_key)
            try:
                return f.decrypt(ciphertext).decode()
            except InvalidToken:
                raise VaultError("Decryption failed — wrong passphrase or corrupted data")
        except ImportError:
            return self._xor_encrypt(ciphertext, self._key).decode()

    @staticmethod
    def _xor_encrypt(data: bytes, key: bytes) -> bytes:
        key_bytes = key[:32]
        return bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(data))

    # ──────────────────────────────────── DB ─────────────────────────────────

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)


def default_vault_path() -> Path:
    """Return the preferred vault database path, preserving legacy installs."""
    paths = get_olano_paths()
    preferred = paths.vault_db_path
    legacy = paths.legacy_vault_db_path
    if legacy.exists() and not preferred.exists():
        return legacy
    return preferred


def known_vault_paths() -> list[Path]:
    """Return all supported vault locations, preferred path first."""
    paths = [default_vault_path()]
    current = get_olano_paths()
    for candidate in (current.vault_db_path, current.legacy_vault_db_path):
        if candidate not in paths:
            paths.append(candidate)
    return paths


# ── auto-unlock boot-key file ────────────────────────────────────────────────
# A passphrase kept beside the vault DB (0600) so unattended restarts open the
# vault on a box with no OLANO_VAULT_KEY and no system keyring. Written by
# quick-setup when it generated the passphrase, and by a rekey on a box whose
# baked env key went stale (see vault/runtime.py, which owns the writer). The
# store reads it so a passphrase-less ``VaultStore.open()`` agrees with the
# runtime's unlock chain instead of minting an ephemeral key next to the real
# one.
BOOT_KEY_FILENAME = "vault_boot_key"
_MAX_BOOT_KEY_BYTES = 4096


def boot_key_path() -> Path:
    return default_vault_path().parent / BOOT_KEY_FILENAME


def read_boot_key() -> str | None:
    """The persisted auto-unlock passphrase, or ``None``.

    Defensive like the deployment-cache readers: symlinks and oversized files
    are rejected, unreadable files read as absent.
    """
    p = boot_key_path()
    try:
        if p.is_symlink() or not p.exists():
            return None
        if p.stat().st_size > _MAX_BOOT_KEY_BYTES:
            return None
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
