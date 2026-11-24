"""Box-side Ed25519 relay signing + the cross-repo golden vector.

The relay stores only this box's **public** key, so a read of the control-plane
database yields nothing that can sign a request, which is the failure mode a
shared symmetric secret has.

Two things here can break production silently, so both are pinned:

1. **The frozen contract.** The signature this module produces must be exactly
   what the relay verifies. ``tests/fixtures/relay/relay_signing_golden.json`` is
   committed identically on the relay side (regenerated there with
   ``gen-relay-golden-vector.py --sync-olano``), and both test suites
   assert against it — a scheme change that isn't mirrored fails CI here.
2. **Credential precedence.** A box carrying only the legacy shared secret must
   keep working, and a box with a private key must use it.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from olano.core import mcp_relay

GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "relay" / "relay_signing_golden.json").read_text(
        encoding="utf-8"
    )
)
GOLDEN_PRIVATE = GOLDEN["ed25519"]["private_key_pkcs8_b64"]
GOLDEN_PUBLIC = GOLDEN["ed25519"]["public_key_b64"]
GOLDEN_SEED = GOLDEN["ed25519"]["private_key_seed_b64"]
GOLDEN_HMAC_SECRET = GOLDEN["hmac"]["secret"]


def _parts(case: dict) -> tuple[str, str, str, str]:
    return case["action"], case["instance_id"], case["state"], case["ts"]


def _new_key() -> tuple[str, str]:
    """A fresh keypair as ``(private_pkcs8_b64, public_raw_b64)``."""
    private = ed25519.Ed25519PrivateKey.generate()
    der = private.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_pub = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.b64encode(der).decode(), base64.b64encode(raw_pub).decode()


# ── The frozen contract, against the vector the relay also asserts ───────────


def test_golden_vector_signatures_reproduce_exactly() -> None:
    # Ed25519 is deterministic (RFC 8032 §5.1.6), so the same key and message
    # must reproduce the committed signature byte for byte. A failure means this
    # repo drifted from the relay.
    for case in GOLDEN["cases"]:
        assert (
            mcp_relay.sign_ed25519(GOLDEN_PRIVATE, *_parts(case))
            == (case["ed25519_signature_b64url"])
        ), case


def test_golden_vector_hmac_leg_reproduces_exactly() -> None:
    # The shared-secret scheme is pinned too — it still serves the reference relay
    # and any not-yet-re-keyed box, so it must not drift either.
    for case in GOLDEN["cases"]:
        assert mcp_relay.sign(GOLDEN_HMAC_SECRET, *_parts(case)) == case["hmac_signature_hex"]


def test_golden_vector_pins_the_signed_message() -> None:
    for case in GOLDEN["cases"]:
        assert mcp_relay.signed_message(*_parts(case)) == base64.b64decode(case["message_utf8_b64"])


def test_signature_is_verifiable_by_the_relays_public_key() -> None:
    # What the relay actually does: decode the stored base64 raw public key and
    # verify the base64url-unpadded wire signature over the same message.
    public = ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(GOLDEN_PUBLIC))
    for case in GOLDEN["cases"]:
        wire = mcp_relay.sign_ed25519(GOLDEN_PRIVATE, *_parts(case))
        raw = base64.urlsafe_b64decode(wire + "=" * (-len(wire) % 4))
        public.verify(raw, mcp_relay.signed_message(*_parts(case)))


def test_wire_signature_encoding_is_unpadded_base64url() -> None:
    wire = mcp_relay.sign_ed25519(GOLDEN_PRIVATE, "register", "inst", "st", "1779000000")
    # The relay reads `sig` out of a QUERY STRING on /mcp/pending, where standard
    # base64's '+' would arrive as a space. Hence base64url, and no '=' padding.
    assert "=" not in wire and "+" not in wire and "/" not in wire
    assert len(base64.urlsafe_b64decode(wire + "=" * (-len(wire) % 4))) == 64


def test_public_key_b64_matches_what_the_relay_stores() -> None:
    assert mcp_relay.public_key_b64(GOLDEN_PRIVATE) == GOLDEN_PUBLIC


# ── Private key loading ──────────────────────────────────────────────────────


def test_load_private_key_requires_pkcs8_not_a_raw_seed() -> None:
    # Strict on purpose. A raw 32-byte seed is a valid Ed25519 private key, but so
    # is any other 32 bytes — including a PUBLIC key. Accepting seeds would make
    # the two indistinguishable, so a misconfigured public key would sign garbage
    # and every relay call would 401 with nothing pointing at the cause.
    with pytest.raises(ValueError, match="PKCS8 DER"):
        mcp_relay.load_private_key(GOLDEN_SEED)


def test_load_private_key_rejects_garbage_base64() -> None:
    for bad in ("not base64!", "", "   "):
        with pytest.raises(ValueError):
            mcp_relay.load_private_key(bad)


def test_load_private_key_accepts_base64url_and_stray_whitespace() -> None:
    raw = base64.b64decode(GOLDEN_PRIVATE)
    urlsafe = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    expected = mcp_relay.sign_ed25519(GOLDEN_PRIVATE, "pending", "i", "s", "1")
    assert mcp_relay.sign_ed25519(urlsafe, "pending", "i", "s", "1") == expected
    assert mcp_relay.sign_ed25519(f"  {GOLDEN_PRIVATE}\n", "pending", "i", "s", "1") == expected


def test_load_private_key_rejects_a_public_key() -> None:
    # The confusion that matters: handing the signer the *public* key — the value
    # sitting right next to it in the control plane — must fail loudly rather than
    # produce signatures nothing can verify.
    with pytest.raises(ValueError, match="PKCS8 DER"):
        mcp_relay.sign_ed25519(GOLDEN_PUBLIC, "register", "i", "s", "1")


def test_load_private_key_rejects_a_non_ed25519_key() -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    der = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with pytest.raises(ValueError, match="not an Ed25519 private key"):
        mcp_relay.load_private_key(base64.b64encode(der).decode())


# ── Signature correctness ────────────────────────────────────────────────────


def test_signature_covers_every_part() -> None:
    private, public_b64 = _new_key()
    public = ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
    parts = ("pending", "inst", "state-1", "1779000000")
    wire = mcp_relay.sign_ed25519(private, *parts)
    raw = base64.urlsafe_b64decode(wire + "=" * (-len(wire) % 4))
    public.verify(raw, mcp_relay.signed_message(*parts))
    # Any changed part must no longer verify.
    for tampered in [
        ("register", "inst", "state-1", "1779000000"),
        ("pending", "other", "state-1", "1779000000"),
        ("pending", "inst", "state-2", "1779000000"),
        ("pending", "inst", "state-1", "1779000001"),
    ]:
        with pytest.raises(Exception):
            public.verify(raw, mcp_relay.signed_message(*tampered))


def test_two_instances_produce_different_signatures() -> None:
    a, _ = _new_key()
    b, _ = _new_key()
    parts = ("register", "inst", "st", "1779000000")
    assert mcp_relay.sign_ed25519(a, *parts) != mcp_relay.sign_ed25519(b, *parts)


# ── Config resolution + credential precedence ────────────────────────────────


def test_config_prefers_the_private_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.example.com")
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_PRIVATE_KEY", GOLDEN_PRIVATE)
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_SECRET", "left-over-secret")
    cfg = mcp_relay.resolve_relay_config()
    assert cfg is not None and cfg.signs_ed25519
    # A box mid-migration can briefly hold both; it must sign with the new key.
    assert mcp_relay.sign_request(cfg, "register", "i", "s", "1") == mcp_relay.sign_ed25519(
        GOLDEN_PRIVATE, "register", "i", "s", "1"
    )


def test_config_falls_back_to_the_shared_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # A box that has not been re-keyed, or a self-hoster running the
    # single-secret reference relay, must keep working untouched.
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.example.com")
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_SECRET", "shared")
    cfg = mcp_relay.resolve_relay_config()
    assert cfg is not None and not cfg.signs_ed25519
    assert mcp_relay.sign_request(cfg, "pending", "i", "s", "1") == mcp_relay.sign(
        "shared", "pending", "i", "s", "1"
    )


def test_config_errors_when_neither_credential_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.example.com")
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_SECRET", raising=False)
    monkeypatch.setattr(mcp_relay, "relay_private_key", lambda: None)
    monkeypatch.setattr(mcp_relay, "relay_secret", lambda: None)
    with pytest.raises(RuntimeError, match="no relay credential"):
        mcp_relay.resolve_relay_config()


def test_private_key_is_read_from_the_vault_when_not_in_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_PRIVATE_KEY", raising=False)

    class _Vault:
        def get(self, name: str, touch: bool = True) -> str:
            return GOLDEN_PRIVATE if name == "LIONCLAW_MCP_RELAY_PRIVATE_KEY" else ""

    monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: _Vault())
    assert mcp_relay.relay_private_key() == GOLDEN_PRIVATE


# ── Signed requests carry the right signature ────────────────────────────────


async def test_register_and_poll_sign_with_ed25519(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wire payloads must carry an Ed25519 signature, not an HMAC one."""
    private, public_b64 = _new_key()
    cfg = mcp_relay.RelayConfig(
        url="https://auth.example.com", secret="", instance_id="inst-9", private_key=private
    )
    public = ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
    seen: dict[str, dict] = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"status": "pending"}

    class _Client:
        def __init__(self, *a, **kw) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def post(self, url: str, json: dict) -> _Resp:
            seen["register"] = json
            return _Resp()

        async def get(self, url: str, params: dict) -> _Resp:
            seen["pending"] = params
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    await mcp_relay.register_state(cfg, "st-1")
    await mcp_relay.poll_code(cfg, "st-1")

    for action in ("register", "pending"):
        payload = seen[action]
        wire = payload["sig"]
        assert "+" not in wire and "/" not in wire and "=" not in wire, action
        raw = base64.urlsafe_b64decode(wire + "=" * (-len(wire) % 4))
        public.verify(
            raw,
            mcp_relay.signed_message(action, "inst-9", "st-1", payload["ts"]),
        )
