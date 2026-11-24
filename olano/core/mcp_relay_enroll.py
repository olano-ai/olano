"""Self-enroll this deployment's Ed25519 MCP relay key at runtime startup.

A managed box provisioned before the Ed25519 cutover still carries the legacy
symmetric ``LIONCLAW_MCP_RELAY_SECRET``. Re-keying it must not require SSH — the
whole point of a managed instance is that pressing **Update** in the cloud console
(new image → restart) is sufficient. So the migration happens *here*, inside the
runtime, on the boot that follows the update:

1. mint an Ed25519 keypair in-process,
2. register only the **public** half with the control plane, authenticated by the
   per-deployment bearer token this box already has,
3. persist the **private** half in the credentials vault, which lives on the
   olano home volume and therefore survives both container restarts and image
   updates.

:func:`olano.core.mcp_relay.relay_private_key` already resolves env → vault, so
once step 3 lands every subsequent boot signs with Ed25519 and no further work
happens. The private key never leaves the box, and the control plane only ever
stores a public key — a database read yields nothing that can sign a request,
which is the entire point of the migration.

Why the vault rather than ``/opt/olano/olano.env``: that file is bind-mounted
**read-only** into the runtime container, so the process genuinely cannot write
it. The vault is the deployment's own writable secret store and is already the
second lookup source for this credential.

Ordering is deliberate: **register the public key first, persist the private key
only once the control plane has accepted it.** The reverse would leave the box
signing with a key the relay has never heard of. If any step fails the box keeps
its existing working credential and the next restart retries — nothing here is
allowed to break boot.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_CLOUD_API_URL_ENV = "LIONCLAW_CLOUD_API_URL"
_DEPLOYMENT_TOKEN_ENV = "LIONCLAW_DEPLOYMENT_TOKEN"
_ENROLL_PATH = "/api/v1/deployments/relay-key"
_TIMEOUT_SEC = 20.0


def _mint_keypair() -> tuple[str, str]:
    """A new keypair as ``(private_pkcs8_b64, public_raw_b64)``.

    Serialization matches what the provisioning worker bakes into cloud-init, so
    both paths produce a credential this runtime can load (see
    ``mcp_relay.load_private_key``).
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    private = ed25519.Ed25519PrivateKey.generate()
    der = private.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.b64encode(der).decode("ascii"), base64.b64encode(raw_public).decode("ascii")


def _register_public_key(api_url: str, token: str, public_key: str, instance_id: str) -> None:
    """POST the public key to the control plane. Raises on any non-2xx."""
    payload = json.dumps({"public_key": public_key, "instance_id": instance_id}).encode("utf-8")
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}{_ENROLL_PATH}", data=payload, method="POST"
    )
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SEC) as resp:
        if not (200 <= resp.status < 300):  # pragma: no cover - urllib raises first
            raise RuntimeError(f"control plane returned HTTP {resp.status}")


def maybe_enroll_relay_key() -> bool:
    """Migrate this deployment to Ed25519 relay signing if it isn't already.

    Returns ``True`` when a key was enrolled on this call. Never raises: a failure
    here must leave the box exactly as it was — still able to sign with whatever
    credential it already has — and simply retry on the next restart.
    """
    from olano.core import mcp_relay

    # No relay configured → nothing to enroll against. Configured only: a
    # self-hosted box's default relay is mcp_relay_selfhost's business.
    if not mcp_relay.configured_relay_url():
        return False

    # Already migrated. Cheapest check, and the one that makes this a no-op on
    # every boot after the first (including a freshly provisioned box, whose
    # private key arrives via cloud-init).
    try:
        if mcp_relay.relay_private_key():
            return False
    except Exception:  # pragma: no cover - defensive; treat as "unknown"
        return False

    # Enrollment is authenticated by the managed-cloud deployment token. An
    # unmanaged box has no channel to register a key over and keeps the
    # credential it was installed with.
    api_url = (os.environ.get(_CLOUD_API_URL_ENV) or "").strip()
    token = (os.environ.get(_DEPLOYMENT_TOKEN_ENV) or "").strip()
    if not api_url or not token:
        logger.debug("MCP relay: no managed-cloud channel; keeping the current credential")
        return False

    vault = _writable_vault()
    if vault is None:
        # Without somewhere durable to put the private key, enrolling would move
        # the control-plane row to ed25519 while this box still signs HMAC — it
        # would lose relay auth entirely. Refuse and retry next boot.
        logger.warning(
            "MCP relay: cannot migrate to Ed25519 — the credentials vault is not "
            "writable (locked or absent), so the new private key could not be "
            "persisted. Still signing with the existing secret; will retry on the "
            "next restart."
        )
        return False

    try:
        private_key, public_key = _mint_keypair()
        instance_id = mcp_relay.instance_id()
        # Public key FIRST: only commit the private half once the relay will
        # accept signatures made with it.
        _register_public_key(api_url, token, public_key, instance_id)
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
        detail = getattr(exc, "read", None)
        body = ""
        if callable(detail):  # HTTPError carries the server's explanation
            try:
                body = f" — {detail().decode('utf-8', errors='replace')[:200]}"
            except Exception:
                body = ""
        logger.warning(
            "MCP relay: Ed25519 enrollment did not complete (%s%s). Keeping the "
            "existing credential; will retry on the next restart.",
            exc,
            body,
        )
        return False
    except Exception as exc:  # pragma: no cover - never break boot
        logger.warning("MCP relay: Ed25519 enrollment failed unexpectedly: %s", exc)
        return False

    try:
        vault.set(
            mcp_relay._RELAY_PRIVATE_KEY_ENV,
            private_key,
            description="Ed25519 private key this instance signs MCP relay requests with",
            category="system",
        )
    except Exception as exc:
        # The control plane now expects Ed25519 but we could not store the key.
        # Loud, and actionable: the operator can re-run the enrollment by
        # restarting once the vault is writable.
        logger.error(
            "MCP relay: enrolled an Ed25519 public key but could not persist the "
            "private key to the vault (%s). MCP authorization will fail until this "
            "is resolved — restart to retry.",
            exc,
        )
        return False

    logger.info(
        "MCP relay: migrated to Ed25519 signing (instance=%s, public key=%s). "
        "The control plane now stores only the public half.",
        instance_id,
        public_key,
    )
    return True


def _writable_vault() -> Any | None:
    """The unlocked vault, or ``None`` when there is nowhere durable to write.

    ``get_vault()`` returns the instance the runtime unlocked at boot
    (``_unlock_vault_for_boot``). A locked or absent vault yields ``None`` rather
    than an exception, so callers can treat it as "not now".
    """
    try:
        from olano.vault.resolver import get_vault

        vault = get_vault()
    except Exception:  # pragma: no cover - import guard
        return None
    return vault if vault is not None and hasattr(vault, "set") else None
