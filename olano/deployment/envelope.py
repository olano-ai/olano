"""Signed-artifact envelope: parse + strictly verify Deployment Settings.

Wire format (one format for the local license file, the cloud response's
``license`` field, and the offline cache — one verification path):

    LCDS1.<key_id>.<base64url(payload_json_bytes)>.<base64url(signature)>

The Ed25519 signature covers the **exact payload bytes** — the payload is
never re-serialized or canonicalized before verification, so there is no
canonical-JSON pitfall and the algorithm is pinned by the prefix.

Every rejection raises :class:`LicenseError` with a stable ``code`` so the
resolver and CLI can report actionable reasons without string-matching.
"""

from __future__ import annotations

import base64
import json
import math
import re
import time
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519
from pydantic import ValidationError

from olano.deployment.keys import load_verify_keys
from olano.deployment.schema import (
    AUDIENCE,
    CHANNELS,
    ISSUER,
    SCHEMA_NAME,
    SUPPORTED_SCHEMA_VERSION,
    SignedSettings,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

LICENSE_PREFIX = "LCDS1"
MAX_ARTIFACT_BYTES = 64 * 1024
ED25519_SIGNATURE_LEN = 64
# Tolerated forward clock skew for issued_at (seconds).
CLOCK_SKEW_TOLERANCE = 300

_KEY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class LicenseError(Exception):
    """A settings artifact failed verification.

    ``code`` is stable and machine-readable: oversized | malformed |
    unknown_key | bad_signature | invalid_json | invalid_payload |
    schema_unsupported | wrong_schema | wrong_audience | wrong_issuer |
    wrong_channel | deployment_mismatch | invalid_lifecycle | future_issued |
    not_yet_valid | expired.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _b64url_decode(segment: str, *, what: str) -> bytes:
    """Strict base64url decode: charset-checked, canonical padding only."""
    if not segment or not _B64URL_RE.match(segment):
        raise LicenseError("malformed", f"{what} is not valid base64url")
    pad = -len(segment) % 4
    if pad == 3:  # a base64 segment can never need 3 padding chars
        raise LicenseError("malformed", f"{what} has invalid base64url length")
    try:
        return base64.urlsafe_b64decode(segment + "=" * pad)
    except (ValueError, TypeError) as exc:
        raise LicenseError("malformed", f"{what} failed base64url decoding") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise LicenseError("invalid_json", f"duplicate JSON key: {key!r}")
        obj[key] = value
    return obj


def _reject_non_finite(value: str) -> float:
    raise LicenseError("invalid_json", f"non-finite JSON number: {value}")


def parse_payload_json(payload_bytes: bytes) -> dict[str, object]:
    """Parse payload bytes as a strict JSON object.

    Rejects duplicate keys, NaN/Infinity, non-object top level, and any
    finite float that sneaks in as non-finite via nesting.
    """
    try:
        parsed = json.loads(
            payload_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except LicenseError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise LicenseError("invalid_json", f"payload is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LicenseError("invalid_json", "payload is not a JSON object")
    _check_no_non_finite(parsed)
    return parsed


def _check_no_non_finite(node: object) -> None:
    if isinstance(node, float) and not math.isfinite(node):
        raise LicenseError("invalid_json", "non-finite number in payload")
    if isinstance(node, dict):
        for v in node.values():
            _check_no_non_finite(v)
    elif isinstance(node, list):
        for v in node:
            _check_no_non_finite(v)


def verify_license(
    text: str,
    *,
    expected_channel: str,
    expected_deployment_id: str | None = None,
    keys: Mapping[str, ed25519.Ed25519PublicKey] | None = None,
    now: int | None = None,
) -> SignedSettings:
    """Verify a settings artifact and return its validated payload.

    ``expected_channel`` binds the artifact to the source it physically came
    from ("local" for a license file, "cloud" for a fetched/cached response) —
    a cloud artifact dropped into the license path (or vice versa) fails.
    ``expected_deployment_id`` binds it to this installation's manifest.
    ``keys`` defaults to the embedded production map; tests pass their own.
    ``now`` is injectable for deterministic lifecycle tests.
    """
    if now is None:
        now = int(time.time())
    if keys is None:
        keys = load_verify_keys()

    if not isinstance(text, str):
        raise LicenseError("malformed", "artifact must be a string")
    text = text.strip()
    if len(text.encode("utf-8", errors="replace")) > MAX_ARTIFACT_BYTES:
        raise LicenseError("oversized", f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")

    parts = text.split(".")
    if len(parts) != 4:
        raise LicenseError("malformed", "artifact must have 4 dot-separated segments")
    prefix, key_id, payload_b64, sig_b64 = parts
    if prefix != LICENSE_PREFIX:
        raise LicenseError("malformed", f"unknown artifact prefix {prefix!r}")
    if not _KEY_ID_RE.match(key_id):
        raise LicenseError("malformed", "invalid key id segment")

    public_key = keys.get(key_id)
    if public_key is None:
        raise LicenseError("unknown_key", f"no embedded verification key for id {key_id!r}")

    payload_bytes = _b64url_decode(payload_b64, what="payload")
    signature = _b64url_decode(sig_b64, what="signature")
    if len(signature) != ED25519_SIGNATURE_LEN:
        raise LicenseError("malformed", "signature has wrong length")

    try:
        public_key.verify(signature, payload_bytes)
    except InvalidSignature as exc:
        raise LicenseError("bad_signature", "signature verification failed") from exc

    raw = parse_payload_json(payload_bytes)
    try:
        payload = SignedSettings.model_validate(raw)
    except ValidationError as exc:
        raise LicenseError("invalid_payload", f"payload failed schema validation: {exc}") from exc

    # Identity claims.
    if payload.schema_ != SCHEMA_NAME:
        raise LicenseError("wrong_schema", f"unexpected schema {payload.schema_!r}")
    if payload.schema_version > SUPPORTED_SCHEMA_VERSION:
        # A newer control plane may carry enforcement semantics this client
        # cannot implement; silently under-enforcing is not acceptable.
        raise LicenseError(
            "schema_unsupported",
            f"schema_version {payload.schema_version} > supported {SUPPORTED_SCHEMA_VERSION}"
            " — upgrade olano",
        )
    if payload.audience != AUDIENCE:
        raise LicenseError("wrong_audience", f"unexpected audience {payload.audience!r}")
    if payload.issuer != ISSUER:
        raise LicenseError("wrong_issuer", f"unexpected issuer {payload.issuer!r}")
    if payload.channel not in CHANNELS:
        raise LicenseError("wrong_channel", f"unknown channel {payload.channel!r}")
    if payload.channel != expected_channel:
        raise LicenseError(
            "wrong_channel",
            f"artifact channel {payload.channel!r} does not match source {expected_channel!r}",
        )
    if expected_deployment_id is not None and payload.deployment_id != expected_deployment_id:
        raise LicenseError(
            "deployment_mismatch",
            "artifact was issued for a different deployment",
        )

    # Lifecycle claims (all integer epoch seconds).
    if payload.expires_at <= payload.issued_at:
        raise LicenseError("invalid_lifecycle", "expires_at must be after issued_at")
    if payload.issued_at > now + CLOCK_SKEW_TOLERANCE:
        raise LicenseError("future_issued", "artifact issued in the future")
    not_before = payload.not_before if payload.not_before is not None else payload.issued_at
    # Same skew tolerance as issued_at: the cloud stamps not_before = its own
    # "now", so a client clock even one second behind would otherwise reject
    # every freshly issued artifact as not_yet_valid.
    if not_before > now + CLOCK_SKEW_TOLERANCE:
        raise LicenseError("not_yet_valid", "artifact is not yet valid")
    if payload.expires_at <= now:
        raise LicenseError("expired", "artifact has expired")

    return payload


def sign_license(
    payload: dict[str, object],
    private_key: ed25519.Ed25519PrivateKey,
    *,
    key_id: str,
) -> str:
    """Mint a signed artifact from a payload dict.

    Test/tooling helper only: the real signers are the cloud settings API and
    the offline license CLI on the cloud side. Keeping this here is
    harmless — it is useless without a private key whose public half is in the
    embedded map — and it guarantees the Python and Node implementations agree
    byte-for-byte (see the golden-vector tests).
    """
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = private_key.sign(payload_bytes)
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode().rstrip("=")
    sig_b64 = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{LICENSE_PREFIX}.{key_id}.{payload_b64}.{sig_b64}"
