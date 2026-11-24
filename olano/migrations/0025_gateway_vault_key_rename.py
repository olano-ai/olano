"""Move the model-gateway vault rows from ``LIONCLAW_OLANO_GATEWAY_*`` to ``OLANO_GATEWAY_*``.

``deployment/gateway_key.py`` persists three rows when a box enrolls a key
with the control plane: the key, the gateway URL and the digest of a revoked
env key. They were filed under the retired product prefix, doubled
(``LIONCLAW_OLANO_…``), and that is the spelling the Vault page showed.

The runtime does not need this migration to keep working: ``gateway_key``
reads the canonical name and falls back to the legacy one, so a row that never
moves still resolves. Moving it once keeps one source of truth, and lets the
legacy spellings leave ``vault/reserved.py`` one day.

Same shape as ``0018``: idempotent, never destructive on failure. A locked
vault (the common case on an unattended upgrade) cannot be read, so the rows
stay where they are and the read-time fallback carries the box until a later
boot finds the vault open. The delete only ever runs after the write for that
same name succeeded, and a value already under the new name wins (it is the
newer enrollment). Only the shared rows move: the runtime never writes
per-agent overrides for these names.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0025_gateway_vault_key_rename"

# Frozen snapshot of this rename. Never regenerate from gateway_key.LEGACY_NAMES:
# a later rename would silently change what this migration means.
RENAMES: dict[str, str] = {
    "LIONCLAW_OLANO_GATEWAY_KEY": "OLANO_GATEWAY_KEY",
    "LIONCLAW_OLANO_GATEWAY_URL": "OLANO_GATEWAY_URL",
    "LIONCLAW_OLANO_GATEWAY_KEY_REVOKED_DIGEST": "OLANO_GATEWAY_KEY_REVOKED_DIGEST",
}
CATEGORY = "system"
DESCRIPTIONS: dict[str, str] = {
    "OLANO_GATEWAY_KEY": "Per-deployment virtual key for the Olano model gateway",
    "OLANO_GATEWAY_URL": "Olano model gateway URL (from enrollment)",
    "OLANO_GATEWAY_KEY_REVOKED_DIGEST": (
        "Digest of a revoked OLANO_GATEWAY_KEY env value, so it cannot shadow the enrolled key"
    ),
}


def apply(home: Path) -> None:
    try:
        from olano.vault.resolver import get_vault

        vault = get_vault()
    except Exception as exc:  # pragma: no cover - vault module unavailable
        logger.debug("%s: vault unavailable (%s) — nothing to migrate", ID, exc)
        return
    if vault is None:
        logger.debug("%s: no vault on this box — nothing to migrate", ID)
        return

    try:
        present = {str(row.get("name")) for row in vault.list_names()}
    except Exception as exc:
        logger.warning("%s: could not list vault entries (%s) — leaving rows alone", ID, exc)
        return

    moved = 0
    for legacy, canonical in RENAMES.items():
        if legacy not in present:
            continue
        try:
            value = vault.get(legacy, touch=False)
        except Exception as exc:
            # Locked vault, or a row this process cannot decrypt. Keep both the
            # value and the old name: the read-time fallback still resolves it.
            logger.warning(
                "%s: could not read %s (%s) — leaving it under the old name; the "
                "gateway keeps working, and a later boot with the vault open moves it.",
                ID,
                legacy,
                exc,
            )
            continue

        if canonical in present:
            # The box enrolled again after the rename: that row is the newer
            # key, so the legacy one is stale and simply goes.
            try:
                vault.delete(legacy)
                logger.info("%s: %s already set — dropped the legacy %s row", ID, canonical, legacy)
            except Exception as exc:
                logger.warning("%s: could not drop legacy row %s: %s", ID, legacy, exc)
            continue

        try:
            vault.set(canonical, value, description=DESCRIPTIONS[canonical], category=CATEGORY)
        except Exception as exc:
            logger.warning(
                "%s: could not write %s (%s) — old row left in place", ID, canonical, exc
            )
            continue

        # Only now is dropping the old row safe: the value exists under the
        # canonical name.
        try:
            vault.delete(legacy)
        except Exception as exc:
            logger.warning(
                "%s: wrote %s but could not drop %s (%s) — harmless, the canonical "
                "name wins on read",
                ID,
                canonical,
                legacy,
                exc,
            )
        moved += 1

    if moved:
        logger.info("%s: moved %d gateway row(s) onto the OLANO_GATEWAY_* names", ID, moved)
