"""Embedded verification public keys for Deployment Settings artifacts.

Ed25519 public keys, base64 of the raw 32-byte key, keyed by ``key_id`` (the
second segment of the ``LCDS1.<key_id>.<payload>.<sig>`` wire format).

Two production keys with distinct duties (deployment-settings design record, §8):

- ``settings-cloud-*``   — online signing by the cloud.olano.ai settings API.
- ``settings-offline-*`` — the offline license CLI (long-lived local licenses).

Compromise of the web runtime's key must not grant the ability to mint
long-lived offline licenses, hence the split.

There is deliberately **no environment override** for this map — that would be
a one-line bypass knob. Tests pass their own keys explicitly via the ``keys=``
parameter of :func:`olano.deployment.envelope.verify_license`.

Rotation is two-phase: ship a client release containing the new public key,
then start signing with the new private key; retire the old id once every
artifact signed by it has expired.
"""

from __future__ import annotations

import base64
import functools

from cryptography.hazmat.primitives.asymmetric import ed25519

SETTINGS_VERIFY_KEYS_B64: dict[str, str] = {
    "settings-cloud-2026-07": "88XPugfBEWFJzW1LSt2TqMwt48d5y/c42bmI9Kt4+Io=",
    "settings-offline-2026-07": "4VPj65Behsyf546akOcSfJK3xcO1SLX9ciKnAjZz5Xo=",
    # The live settings API signs with the 2026-08 key; 2026-07 stays trusted
    # per the two-phase rotation above, until every artifact signed by it has
    # expired.
    "settings-cloud-2026-08": "eGj/EKRN9Cog/nzhNHI4Y8QPm554X6DN4SWNJ3yuVW8=",
}


@functools.lru_cache(maxsize=1)
def load_verify_keys() -> dict[str, ed25519.Ed25519PublicKey]:
    """The embedded key map as ready-to-use public key objects."""
    return {
        key_id: ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(b64))
        for key_id, b64 in SETTINGS_VERIFY_KEYS_B64.items()
    }
