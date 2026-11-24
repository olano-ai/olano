"""Olano Credentials Vault — encrypted SQLite credential store."""

from olano.vault.resolver import resolve_vault_refs
from olano.vault.store import VaultError, VaultStore

__all__ = ["VaultError", "VaultStore", "resolve_vault_refs"]
