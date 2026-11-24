"""Envelope verification — every strict-rejection rule, rotation, golden vectors."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from olano.deployment.envelope import (
    LICENSE_PREFIX,
    MAX_ARTIFACT_BYTES,
    LicenseError,
    sign_license,
    verify_license,
)

FIXTURES = Path(__file__).parent / "fixtures" / "deployment"

# Deterministic "now" so lifecycle tests (and golden fixtures) never rot.
NOW = 1_780_000_000
KEY_ID = "settings-test-2026-07"


@pytest.fixture(scope="module")
def keypair() -> tuple[ed25519.Ed25519PrivateKey, dict[str, ed25519.Ed25519PublicKey]]:
    private = ed25519.Ed25519PrivateKey.generate()
    return private, {KEY_ID: private.public_key()}


def make_payload(**overrides) -> dict:
    payload: dict = {
        "schema": "olano.deployment-settings",
        "schema_version": 1,
        "issuer": "cloud.olano.ai",
        "audience": "olano-deployment-settings",
        "channel": "local",
        "license_id": "lic_test",
        "deployment_id": "dep_test",
        "plan": "pro",
        "policy_revision": 5,
        "issued_at": NOW - 1000,
        "not_before": NOW - 1000,
        "refresh_after": None,
        "expires_at": NOW + 86400,
        "settings": {"resources": {"max_agents": 3}},
    }
    payload.update(overrides)
    return payload


def mint(keypair, payload: dict, *, key_id: str = KEY_ID) -> str:
    private, _ = keypair
    return sign_license(payload, private, key_id=key_id)


class TestRoundTrip:
    def test_sign_verify_round_trip(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload())
        parsed = verify_license(
            text,
            expected_channel="local",
            expected_deployment_id="dep_test",
            keys=keys,
            now=NOW,
        )
        assert parsed.deployment_id == "dep_test"
        assert parsed.plan == "pro"
        assert parsed.policy_revision == 5
        assert parsed.settings.resources.max_agents == 3

    def test_whitespace_tolerated_around_artifact(self, keypair):
        _, keys = keypair
        text = "  " + mint(keypair, make_payload()) + "\n"
        parsed = verify_license(text, expected_channel="local", keys=keys, now=NOW)
        assert parsed.deployment_id == "dep_test"

    def test_deployment_id_check_optional(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload())
        parsed = verify_license(text, expected_channel="local", keys=keys, now=NOW)
        assert parsed.deployment_id == "dep_test"


def expect_code(code: str):
    return pytest.raises(LicenseError, match="") if code is None else _CodeChecker(code)


class _CodeChecker:
    def __init__(self, code: str) -> None:
        self.code = code

    def __enter__(self):
        self._ctx = pytest.raises(LicenseError)
        return self._ctx.__enter__()

    def __exit__(self, exc_type, exc, tb):
        result = self._ctx.__exit__(exc_type, exc, tb)
        if result:
            assert self._ctx.excinfo.value.code == self.code, (
                f"expected code {self.code!r}, got {self._ctx.excinfo.value.code!r}:"
                f" {self._ctx.excinfo.value}"
            )
        return result


class TestStructuralRejection:
    def test_tampered_payload_rejected(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload())
        prefix, key_id, payload_b64, sig_b64 = text.split(".")
        raw = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        tampered = json.loads(raw)
        tampered["settings"]["resources"]["max_agents"] = 999
        tampered_b64 = (
            base64.urlsafe_b64encode(json.dumps(tampered, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )
        with expect_code("bad_signature"):
            verify_license(
                f"{prefix}.{key_id}.{tampered_b64}.{sig_b64}",
                expected_channel="local",
                keys=keys,
                now=NOW,
            )

    def test_tampered_signature_rejected(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload())
        prefix, key_id, payload_b64, sig_b64 = text.split(".")
        flipped = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
        with expect_code("bad_signature"):
            verify_license(
                f"{prefix}.{key_id}.{payload_b64}.{flipped}",
                expected_channel="local",
                keys=keys,
                now=NOW,
            )

    def test_wrong_prefix_rejected(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload()).replace(LICENSE_PREFIX, "XXXX1", 1)
        with expect_code("malformed"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)

    def test_wrong_segment_count_rejected(self, keypair):
        _, keys = keypair
        with expect_code("malformed"):
            verify_license("LCDS1.only.two", expected_channel="local", keys=keys, now=NOW)

    def test_unknown_key_id_rejected(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload(), key_id="settings-unknown-9999-01")
        with expect_code("unknown_key"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)

    def test_unknown_key_id_rejected_against_production_map(self, keypair):
        # Default key map (embedded production keys) must not know the test key.
        text = mint(keypair, make_payload())
        with expect_code("unknown_key"):
            verify_license(text, expected_channel="local", now=NOW)

    def test_invalid_base64url_rejected(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload())
        prefix, key_id, _payload, sig = text.split(".")
        with expect_code("malformed"):
            verify_license(
                f"{prefix}.{key_id}.no+slash/allowed.{sig}",
                expected_channel="local",
                keys=keys,
                now=NOW,
            )

    def test_wrong_signature_length_rejected(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload())
        prefix, key_id, payload_b64, _sig = text.split(".")
        short_sig = base64.urlsafe_b64encode(b"short").decode().rstrip("=")
        with expect_code("malformed"):
            verify_license(
                f"{prefix}.{key_id}.{payload_b64}.{short_sig}",
                expected_channel="local",
                keys=keys,
                now=NOW,
            )

    def test_oversized_artifact_rejected(self, keypair):
        _, keys = keypair
        big = make_payload()
        big["settings"]["extensions"] = {"pad": "x" * MAX_ARTIFACT_BYTES}
        text = mint(keypair, big)
        with expect_code("oversized"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)

    def test_non_object_json_rejected(self, keypair):
        private, keys = keypair
        payload_bytes = json.dumps(["not", "an", "object"]).encode()
        sig = private.sign(payload_bytes)
        text = (
            f"{LICENSE_PREFIX}.{KEY_ID}."
            f"{base64.urlsafe_b64encode(payload_bytes).decode().rstrip('=')}."
            f"{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"
        )
        with expect_code("invalid_json"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)

    def test_duplicate_json_keys_rejected(self, keypair):
        private, keys = keypair
        payload_bytes = (
            b'{"schema":"olano.deployment-settings","schema":"olano.deployment-settings"}'
        )
        sig = private.sign(payload_bytes)
        text = (
            f"{LICENSE_PREFIX}.{KEY_ID}."
            f"{base64.urlsafe_b64encode(payload_bytes).decode().rstrip('=')}."
            f"{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"
        )
        with expect_code("invalid_json"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)

    def test_nan_infinity_rejected(self, keypair):
        private, keys = keypair
        payload_bytes = b'{"credits": NaN}'
        sig = private.sign(payload_bytes)
        text = (
            f"{LICENSE_PREFIX}.{KEY_ID}."
            f"{base64.urlsafe_b64encode(payload_bytes).decode().rstrip('=')}."
            f"{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"
        )
        with expect_code("invalid_json"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)

    def test_missing_required_claims_rejected(self, keypair):
        _, keys = keypair
        payload = make_payload()
        del payload["deployment_id"]
        text = mint(keypair, payload)
        with expect_code("invalid_payload"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)


class TestClaimRejection:
    def test_wrong_schema_rejected(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload(schema="olano.something-else"))
        with expect_code("wrong_schema"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)

    def test_newer_schema_version_rejected(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload(schema_version=2))
        with expect_code("schema_unsupported"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)

    def test_wrong_audience_rejected(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload(audience="olano-something-else"))
        with expect_code("wrong_audience"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)

    def test_wrong_issuer_rejected(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload(issuer="evil.example.com"))
        with expect_code("wrong_issuer"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)

    def test_channel_must_match_source(self, keypair):
        _, keys = keypair
        # A cloud artifact placed in the local license path must not verify.
        text = mint(keypair, make_payload(channel="cloud"))
        with expect_code("wrong_channel"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)
        # And vice versa.
        text = mint(keypair, make_payload(channel="local"))
        with expect_code("wrong_channel"):
            verify_license(text, expected_channel="cloud", keys=keys, now=NOW)

    def test_unknown_channel_rejected(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload(channel="carrier-pigeon"))
        with expect_code("wrong_channel"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)

    def test_deployment_mismatch_rejected(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload())
        with expect_code("deployment_mismatch"):
            verify_license(
                text,
                expected_channel="local",
                expected_deployment_id="dep_other",
                keys=keys,
                now=NOW,
            )


class TestLifecycle:
    def test_expired_rejected(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload(expires_at=NOW - 1))
        with expect_code("expired"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)

    def test_expiry_boundary_is_exclusive(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload(expires_at=NOW))
        with expect_code("expired"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)

    def test_future_issued_rejected_beyond_skew(self, keypair):
        _, keys = keypair
        text = mint(
            keypair, make_payload(issued_at=NOW + 301, not_before=None, expires_at=NOW + 90000)
        )
        with expect_code("future_issued"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)

    def test_future_issued_within_skew_tolerated(self, keypair):
        _, keys = keypair
        text = mint(
            keypair,
            make_payload(issued_at=NOW + 200, not_before=NOW - 10, expires_at=NOW + 90000),
        )
        parsed = verify_license(text, expected_channel="local", keys=keys, now=NOW)
        assert parsed.issued_at == NOW + 200

    def test_not_before_honored_beyond_skew(self, keypair):
        _, keys = keypair
        text = mint(keypair, make_payload(not_before=NOW + 301))
        with expect_code("not_yet_valid"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)

    def test_not_before_within_skew_tolerated(self, keypair):
        # The cloud stamps not_before = its own "now"; a client clock slightly
        # behind must not reject a freshly issued artifact.
        _, keys = keypair
        text = mint(keypair, make_payload(not_before=NOW + 200))
        parsed = verify_license(text, expected_channel="local", keys=keys, now=NOW)
        assert parsed.not_before == NOW + 200

    def test_expires_before_issued_rejected(self, keypair):
        _, keys = keypair
        # Would otherwise pass "not expired" (expires far future) — the
        # relationship itself must hold.
        text = mint(
            keypair,
            make_payload(issued_at=NOW + 90000, not_before=NOW - 10, expires_at=NOW + 100),
        )
        with expect_code("invalid_lifecycle"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)


class TestKeyRotation:
    def test_second_key_verifies(self):
        old = ed25519.Ed25519PrivateKey.generate()
        new = ed25519.Ed25519PrivateKey.generate()
        keys = {
            "settings-test-old": old.public_key(),
            "settings-test-new": new.public_key(),
        }
        for key_id, private in (("settings-test-old", old), ("settings-test-new", new)):
            text = sign_license(make_payload(), private, key_id=key_id)
            parsed = verify_license(text, expected_channel="local", keys=keys, now=NOW)
            assert parsed.deployment_id == "dep_test"

    def test_cross_key_signature_rejected(self):
        old = ed25519.Ed25519PrivateKey.generate()
        new = ed25519.Ed25519PrivateKey.generate()
        keys = {"settings-test-old": old.public_key(), "settings-test-new": new.public_key()}
        # Signed by new but claiming old's key id: signature check must fail.
        text = sign_license(make_payload(), new, key_id="settings-test-old")
        with expect_code("bad_signature"):
            verify_license(text, expected_channel="local", keys=keys, now=NOW)


class TestGoldenVectors:
    """Node-signed fixtures verified by this Python path.

    Generated by the cloud-side signing script (sign-license.mjs) with the dev
    fixture key. Uses a fixed injected `now` so the fixtures never expire.
    """

    def _keys(self):
        pub_b64 = (FIXTURES / "golden_pubkey.b64").read_text().strip()
        return {
            "settings-dev-2026-07": ed25519.Ed25519PublicKey.from_public_bytes(
                base64.b64decode(pub_b64)
            )
        }

    def test_golden_license_verifies(self):
        text = (FIXTURES / "golden.lic").read_text()
        parsed = verify_license(
            text,
            expected_channel="local",
            expected_deployment_id="dep_golden",
            keys=self._keys(),
            now=NOW,
        )
        assert parsed.plan == "pro"
        assert parsed.policy_revision == 1
        assert parsed.settings.resources.max_agents == 5
        assert parsed.settings.feature_enabled("terminal") is False

    def test_golden_tampered_rejected(self):
        text = (FIXTURES / "golden_tampered.lic").read_text()
        with expect_code("bad_signature"):
            verify_license(text, expected_channel="local", keys=self._keys(), now=NOW)

    def test_golden_expired_rejected(self):
        text = (FIXTURES / "golden_expired.lic").read_text()
        with expect_code("expired"):
            verify_license(text, expected_channel="local", keys=self._keys(), now=NOW)

    def test_golden_wrong_channel_rejected(self):
        # A cloud-channel artifact must not verify as a local license.
        text = (FIXTURES / "golden_cloud_channel.lic").read_text()
        with expect_code("wrong_channel"):
            verify_license(text, expected_channel="local", keys=self._keys(), now=NOW)

    def test_golden_wrong_deployment_rejected(self):
        text = (FIXTURES / "golden.lic").read_text()
        with expect_code("deployment_mismatch"):
            verify_license(
                text,
                expected_channel="local",
                expected_deployment_id="dep_someone_else",
                keys=self._keys(),
                now=NOW,
            )
