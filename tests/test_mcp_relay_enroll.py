"""Runtime self-enrollment of the Ed25519 MCP relay key.

This runs unattended on every boot of every deployment, so the paths that matter
most are the ones where it must do *nothing*, and the ones where it must leave the
box on its existing working credential rather than half-migrated.

The invariant under test: the control plane never ends up expecting Ed25519 while
this box is still signing HMAC. That means the public key is registered first and
the private key is persisted only after — and if the private key can't be stored,
the box must not have enrolled at all.
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from olano.core import mcp_relay, mcp_relay_enroll


class _Vault:
    """Minimal stand-in for the credentials vault."""

    def __init__(self, *, fail: bool = False) -> None:
        self.stored: dict[str, str] = {}
        self._fail = fail

    def set(self, name: str, value: str, **kwargs: object) -> None:
        if self._fail:
            raise RuntimeError("vault is locked")
        self.stored[name] = value

    def get(self, name: str, agent: str | None = None, *, touch: bool = True) -> str:
        return self.stored.get(name, "")


@pytest.fixture
def managed_box(monkeypatch: pytest.MonkeyPatch) -> _Vault:
    """A managed box still on the legacy shared secret."""
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.example.com")
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_SECRET", "legacy-shared-secret")
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("LIONCLAW_CLOUD_API_URL", "https://cloud.example.com")
    monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "olano_dep_tok")
    monkeypatch.setenv("LIONCLAW_INSTANCE_ID", "dep-live-box")
    vault = _Vault()
    monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: vault)
    return vault


def _capture_registrations(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    sent: list[dict] = []

    def _fake(api_url: str, token: str, public_key: str, instance_id: str) -> None:
        sent.append(
            {
                "api_url": api_url,
                "token": token,
                "public_key": public_key,
                "instance_id": instance_id,
            }
        )

    monkeypatch.setattr(mcp_relay_enroll, "_register_public_key", _fake)
    return sent


# ── The happy path: a legacy box migrates itself ─────────────────────────────


def test_enrolls_and_persists_the_private_key(
    managed_box: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _capture_registrations(monkeypatch)
    assert mcp_relay_enroll.maybe_enroll_relay_key() is True

    assert len(sent) == 1
    stored = managed_box.stored["LIONCLAW_MCP_RELAY_PRIVATE_KEY"]
    # The registered public key must be the one derived from the stored private
    # key — otherwise the relay verifies against a key this box cannot sign with.
    key = serialization.load_der_private_key(base64.b64decode(stored), password=None)
    derived = base64.b64encode(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
    ).decode()
    assert derived == sent[0]["public_key"]
    assert sent[0]["instance_id"] == "dep-live-box"
    assert sent[0]["token"] == "olano_dep_tok"


def test_the_persisted_key_is_what_the_relay_client_then_uses(
    managed_box: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: after enrollment the box signs Ed25519, verifiable by the key
    the control plane was given."""
    sent = _capture_registrations(monkeypatch)
    assert mcp_relay_enroll.maybe_enroll_relay_key() is True

    # relay_private_key() resolves env → vault, so the freshly stored key is what
    # a subsequent boot picks up (env has none).
    assert mcp_relay.relay_private_key() == managed_box.stored["LIONCLAW_MCP_RELAY_PRIVATE_KEY"]
    cfg = mcp_relay.resolve_relay_config()
    assert cfg is not None and cfg.signs_ed25519

    parts = ("register", "dep-live-box", "state-1", "1779000000")
    wire = mcp_relay.sign_request(cfg, *parts)
    public = ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(sent[0]["public_key"]))
    public.verify(
        base64.urlsafe_b64decode(wire + "=" * (-len(wire) % 4)),
        mcp_relay.signed_message(*parts),
    )


def test_is_a_no_op_on_the_next_boot(managed_box: _Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _capture_registrations(monkeypatch)
    assert mcp_relay_enroll.maybe_enroll_relay_key() is True
    # Second boot: the vault now holds a private key, so nothing happens.
    assert mcp_relay_enroll.maybe_enroll_relay_key() is False
    assert len(sent) == 1


# ── Paths where it must do nothing ───────────────────────────────────────────


def test_skips_when_no_relay_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_URL", raising=False)
    monkeypatch.setattr(mcp_relay, "relay_url", lambda: None)
    sent = _capture_registrations(monkeypatch)
    assert mcp_relay_enroll.maybe_enroll_relay_key() is False
    assert sent == []


def test_skips_when_a_private_key_already_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    # A freshly provisioned box gets its key from cloud-init; it must not
    # immediately re-key itself and churn the control-plane row.
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.example.com")
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_PRIVATE_KEY", "already-here")
    sent = _capture_registrations(monkeypatch)
    assert mcp_relay_enroll.maybe_enroll_relay_key() is False
    assert sent == []


def test_skips_an_unmanaged_box(monkeypatch: pytest.MonkeyPatch) -> None:
    # No deployment token → no channel to register over. It keeps the shared
    # secret, which is the correct credential for a self-hosted reference relay.
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.example.com")
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_SECRET", "shared")
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("LIONCLAW_CLOUD_API_URL", raising=False)
    monkeypatch.delenv("LIONCLAW_DEPLOYMENT_TOKEN", raising=False)
    monkeypatch.setattr(mcp_relay, "relay_private_key", lambda: None)
    sent = _capture_registrations(monkeypatch)
    assert mcp_relay_enroll.maybe_enroll_relay_key() is False
    assert sent == []


# ── Failure paths: never leave the box half-migrated ─────────────────────────


def test_does_not_enroll_when_the_vault_cannot_be_written(
    managed_box: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The critical ordering case.

    If the private key can't be persisted, registering the public key would move
    the control-plane row to ed25519 while this box still signs HMAC — losing relay
    auth entirely. So the vault is checked BEFORE anything is registered.
    """
    monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: None)
    sent = _capture_registrations(monkeypatch)
    assert mcp_relay_enroll.maybe_enroll_relay_key() is False
    assert sent == [], "must not register a public key it cannot back with a stored private key"


def test_reports_failure_when_the_vault_write_itself_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.example.com")
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("LIONCLAW_CLOUD_API_URL", "https://cloud.example.com")
    monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "olano_dep_tok")
    monkeypatch.setattr(mcp_relay, "relay_private_key", lambda: None)
    monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: _Vault(fail=True))
    _capture_registrations(monkeypatch)
    with caplog.at_level("ERROR"):
        assert mcp_relay_enroll.maybe_enroll_relay_key() is False
    # This is the one genuinely bad state, so it must be loud and actionable.
    assert any("could not persist" in r.message for r in caplog.records)


def test_registration_failure_leaves_the_existing_credential_alone(
    managed_box: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.error

    def _boom(*a: object, **kw: object) -> None:
        raise urllib.error.URLError("control plane unreachable")

    monkeypatch.setattr(mcp_relay_enroll, "_register_public_key", _boom)
    assert mcp_relay_enroll.maybe_enroll_relay_key() is False
    assert managed_box.stored == {}
    # Still signs with the legacy secret, so MCP OAuth keeps working.
    cfg = mcp_relay.resolve_relay_config()
    assert cfg is not None and not cfg.signs_ed25519


def test_http_error_is_swallowed_with_the_server_detail(
    managed_box: _Vault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import io
    import urllib.error

    def _http_409(*a: object, **kw: object) -> None:
        raise urllib.error.HTTPError(
            "https://cloud.example.com",
            409,
            "Conflict",
            {},  # type: ignore[arg-type]
            io.BytesIO(json.dumps({"error": "instance_not_owned"}).encode()),
        )

    monkeypatch.setattr(mcp_relay_enroll, "_register_public_key", _http_409)
    with caplog.at_level("WARNING"):
        assert mcp_relay_enroll.maybe_enroll_relay_key() is False
    assert managed_box.stored == {}
    assert any("instance_not_owned" in r.getMessage() for r in caplog.records)


def test_never_raises_even_if_keygen_explodes(
    managed_box: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Boot must not depend on this working.
    monkeypatch.setattr(
        mcp_relay_enroll, "_mint_keypair", lambda: (_ for _ in ()).throw(RuntimeError("no entropy"))
    )
    assert mcp_relay_enroll.maybe_enroll_relay_key() is False
    assert managed_box.stored == {}
