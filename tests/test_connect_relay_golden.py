"""Cross-repo golden vector for the /connect signing contract (box side).

``tests/fixtures/relay/connect_signing_golden.json`` is committed identically
on the relay side (regenerated there with
``gen-relay-golden-vector.py --sync-olano``) and pins what /connect
adds on top of the MCP relay contract: the ``connect_*`` action names riding
the same ``signed_message`` scheme, and refresh's state-slot binding —
``sha256_hex(refresh_token)`` over the UTF-8 token bytes. Both suites assert
against the same fixture, so a scheme change that isn't mirrored fails CI
before it breaks an in-flight authorization.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from olano.core import connect_relay, mcp_relay

GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "relay" / "connect_signing_golden.json").read_text(
        encoding="utf-8"
    )
)
GOLDEN_PRIVATE = GOLDEN["ed25519"]["private_key_pkcs8_b64"]
GOLDEN_HMAC_SECRET = GOLDEN["hmac"]["secret"]


def _parts(case: dict) -> tuple[str, str, str, str]:
    return case["action"], case["instance_id"], case["state"], case["ts"]


def test_golden_covers_every_connect_action() -> None:
    actions = {case["action"] for case in GOLDEN["cases"]}
    assert actions == {
        connect_relay.ACTION_REGISTER,
        connect_relay.ACTION_PENDING,
        connect_relay.ACTION_REFRESH,
    }


def test_golden_ed25519_signatures_reproduce_exactly() -> None:
    # Ed25519 is deterministic — this box's signer must reproduce the committed
    # wire form byte-for-byte, not merely something that verifies.
    for case in GOLDEN["cases"]:
        assert (
            mcp_relay.sign_ed25519(GOLDEN_PRIVATE, *_parts(case))
            == case["ed25519_signature_b64url"]
        )


def test_golden_hmac_leg_reproduces_exactly() -> None:
    for case in GOLDEN["cases"]:
        assert mcp_relay.sign(GOLDEN_HMAC_SECRET, *_parts(case)) == case["hmac_signature_hex"]


def test_golden_pins_the_signed_message() -> None:
    for case in GOLDEN["cases"]:
        assert mcp_relay.signed_message(*_parts(case)) == base64.b64decode(case["message_utf8_b64"])


def test_golden_refresh_binding_pinned() -> None:
    # The refresh case carries the token plaintext so the hashing itself —
    # UTF-8 before sha256 — is pinned, not just a signature over the result.
    cases = [c for c in GOLDEN["cases"] if c["action"] == connect_relay.ACTION_REFRESH]
    assert cases, "fixture must include a connect_refresh case"
    for case in cases:
        token = case["refresh_token"]
        expected = hashlib.sha256(token.encode("utf-8")).hexdigest()
        assert case["state"] == expected
        assert connect_relay.refresh_binding(token) == expected
