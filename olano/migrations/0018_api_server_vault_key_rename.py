"""Move the OpenAI-compatible API connector's key from ``LIONCLAW_API_KEY`` to ``OLANO_API_KEY``.

The connector mints this key for clients calling the agent; every other entry
in ``CHANNEL_SECRET_KEYS`` is named for the remote service that issued it, so
this one reached for a product prefix and got the retired one.

The runtime does not need this migration to keep working — ``LEGACY_SECRET_KEYS``
makes ``hydrate_channel_credentials`` fall back to the old row, so a connector
whose key never moves still authenticates. What the fallback cannot fix is the
*dashboard*: ``VaultSecretField`` looks the key up by exact name, so until the
row moves the form reports "Not set — this connector will not start" beside a
connector that is running fine, and a user who retypes the key writes a second
row instead of replacing the first. Moving it once keeps one source of truth.

Idempotent: a home with no legacy row is left alone, and a row that already has
a canonical counterpart is dropped rather than overwriting the newer value.

Never destructive on failure. A locked vault (the common case on an unattended
upgrade — the key lives in the OS keyring or an env var that a boot may not
have) cannot be read, so the migration logs and leaves every row in place; the
read-time fallback carries those boxes until someone unlocks and reruns. The
delete only ever runs after the write for that same (name, agent) succeeded.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0018_api_server_vault_key_rename"

# Frozen snapshot of this rename — never regenerate from the live map, or a
# later rename would silently change what this migration means.
LEGACY_KEY = "LIONCLAW_API_KEY"
CANONICAL_KEY = "OLANO_API_KEY"


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

    # (name, agent) pairs to move. ``agent=None`` is the common (shared) row;
    # overrides carry a real agent name.
    targets: list[str | None] = []
    try:
        if any(row.get("name") == LEGACY_KEY for row in vault.list_names()):
            targets.append(None)
        targets.extend(
            str(row["agent"]) for row in vault.list_overrides(name=LEGACY_KEY) if row.get("agent")
        )
    except Exception as exc:
        logger.warning("%s: could not list vault entries (%s) — leaving rows alone", ID, exc)
        return

    if not targets:
        return

    moved = 0
    for agent in targets:
        scope = f"agent '{agent}'" if agent else "the shared entry"
        try:
            value = vault.get(LEGACY_KEY, agent=agent, touch=False)
        except Exception as exc:
            # Locked vault, or a row this process cannot decrypt. Keep both the
            # value and the old name: the read-time fallback still resolves it.
            logger.warning(
                "%s: could not read %s for %s (%s) — leaving it under the old name; "
                "the connector keeps working, and rerunning with the vault unlocked moves it.",
                ID,
                LEGACY_KEY,
                scope,
                exc,
            )
            continue

        try:
            existing = vault.get(CANONICAL_KEY, agent=agent, touch=False)
        except Exception:
            existing = None
        if existing:
            # Someone already set the new name for this scope; that value is
            # the newer intent, so drop the stale legacy row rather than
            # clobbering it.
            try:
                vault.delete(LEGACY_KEY, agent=agent)
                logger.info(
                    "%s: %s already set for %s — dropped the legacy %s row",
                    ID,
                    CANONICAL_KEY,
                    scope,
                    LEGACY_KEY,
                )
            except Exception as exc:
                logger.warning("%s: could not drop legacy row for %s: %s", ID, scope, exc)
            continue

        try:
            vault.set(CANONICAL_KEY, value, agent=agent)
        except Exception as exc:
            logger.warning(
                "%s: could not write %s for %s (%s) — old row left in place",
                ID,
                CANONICAL_KEY,
                scope,
                exc,
            )
            continue

        # Only now is dropping the old row safe: the value exists under the
        # canonical name for this exact scope.
        try:
            vault.delete(LEGACY_KEY, agent=agent)
        except Exception as exc:
            logger.warning(
                "%s: wrote %s for %s but could not drop %s (%s) — harmless, the "
                "canonical name wins on read",
                ID,
                CANONICAL_KEY,
                scope,
                LEGACY_KEY,
                exc,
            )
        moved += 1

    if moved:
        logger.info(
            "%s: moved %d API-server key(s) from %s to %s", ID, moved, LEGACY_KEY, CANONICAL_KEY
        )
