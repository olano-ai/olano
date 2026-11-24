"""Gateway-key resolution and self-enrollment (`deployment/gateway_key.py`).

The property under test: the per-deployment virtual key reaches the runtime
through env → vault → a one-shot enrollment over the deployment token — and a
box that cannot get one degrades to an actionable first-call error, never a
crashed agent build or a box-side provider key.
"""

from __future__ import annotations

import httpx
import pytest

from olano.deployment import gateway_key as gk


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Each test starts keyless, backoff-reset, and vaultless by default."""
    monkeypatch.delenv(gk.GATEWAY_KEY_ENV, raising=False)
    monkeypatch.delenv(gk.GATEWAY_URL_ENV, raising=False)
    monkeypatch.delenv(gk.LEGACY_GATEWAY_KEY_ENV, raising=False)
    monkeypatch.delenv(gk.LEGACY_GATEWAY_URL_ENV, raising=False)
    monkeypatch.delenv(gk.ACCOUNT_KEY_ENV, raising=False)
    gk.reset_account_key_memo()
    monkeypatch.setattr(gk, "_next_attempt_at", float("-inf"))
    monkeypatch.setattr(gk, "_consecutive_failures", 0)
    monkeypatch.setattr(gk, "_pending_nonce", None)
    monkeypatch.setattr(gk, "_last_error", None)
    monkeypatch.setattr(gk, "_last_attempt_wall", None)
    monkeypatch.setattr(gk, "_last_success_wall", None)
    monkeypatch.setattr(gk, "_sentinel_builds", 0)
    monkeypatch.setattr(gk, "_background_enrollment", False)
    monkeypatch.setattr(gk, "_memory_key", None)
    monkeypatch.setattr(gk, "_memory_url", None)
    monkeypatch.setattr(gk, "_vault_get", lambda name: None)
    monkeypatch.setattr(gk, "_writable_vault", lambda: None)
    # The post-success receipt confirmation is best-effort and irrelevant to
    # every assertion here; stub it so no test does real DNS.
    monkeypatch.setattr(gk, "_confirm_receipt", lambda *a, **k: None)
    monkeypatch.setattr(gk, "_dead_env_digest", None)
    monkeypatch.setattr(gk, "_dead_env_loaded", True)
    gk._enroll_wanted.clear()


class _FakeVault:
    def __init__(self) -> None:
        self.stored: dict[str, str] = {}

    def set(self, name: str, value: str, **kwargs) -> None:
        self.stored[name] = value


# ── resolution order ─────────────────────────────────────────────────────────


def test_env_wins_over_vault(monkeypatch) -> None:
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-from-env")
    monkeypatch.setattr(gk, "_vault_get", lambda name: "sk-from-vault")
    assert gk.gateway_key() == "sk-from-env"


# ── the rename: OLANO_GATEWAY_* canonical, LIONCLAW_OLANO_GATEWAY_* honoured ──


def test_names_are_the_olano_spelling() -> None:
    assert gk.GATEWAY_KEY_ENV == "OLANO_GATEWAY_KEY"
    assert gk.GATEWAY_URL_ENV == "OLANO_GATEWAY_URL"
    assert gk.GATEWAY_KEY_DEAD_MARKER == "OLANO_GATEWAY_KEY_REVOKED_DIGEST"
    assert gk.LEGACY_NAMES == {
        "OLANO_GATEWAY_KEY": "LIONCLAW_OLANO_GATEWAY_KEY",
        "OLANO_GATEWAY_URL": "LIONCLAW_OLANO_GATEWAY_URL",
        "OLANO_GATEWAY_KEY_REVOKED_DIGEST": "LIONCLAW_OLANO_GATEWAY_KEY_REVOKED_DIGEST",
    }


def test_legacy_env_spelling_is_honoured(monkeypatch) -> None:
    """A box provisioned before the rename carries the legacy names in
    /opt/olano/olano.env forever; nothing rewrites that file."""
    monkeypatch.setenv(gk.LEGACY_GATEWAY_KEY_ENV, "sk-legacy-env")
    monkeypatch.setenv(gk.LEGACY_GATEWAY_URL_ENV, "https://legacy.gw.test/")
    assert gk.gateway_key() == "sk-legacy-env"
    assert gk.gateway_url() == "https://legacy.gw.test"
    assert gk.enrollment_status()["env_key_name"] == gk.LEGACY_GATEWAY_KEY_ENV


def test_canonical_env_spelling_wins_over_legacy(monkeypatch) -> None:
    monkeypatch.setenv(gk.LEGACY_GATEWAY_KEY_ENV, "sk-legacy-env")
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-new-env")
    assert gk.gateway_key() == "sk-new-env"
    assert gk.enrollment_status()["env_key_name"] == gk.GATEWAY_KEY_ENV


def test_legacy_vault_row_is_read_when_canonical_is_absent(monkeypatch) -> None:
    rows = {
        gk.LEGACY_GATEWAY_KEY_ENV: "sk-legacy-vault",
        gk.LEGACY_GATEWAY_URL_ENV: "https://legacy.vault.test",
    }
    monkeypatch.setattr(gk, "_vault_get", rows.get)
    assert gk.gateway_key() == "sk-legacy-vault"
    assert gk.gateway_url() == "https://legacy.vault.test"


def test_canonical_vault_row_wins_over_legacy(monkeypatch) -> None:
    rows = {gk.GATEWAY_KEY_ENV: "sk-new-vault", gk.LEGACY_GATEWAY_KEY_ENV: "sk-legacy-vault"}
    monkeypatch.setattr(gk, "_vault_get", rows.get)
    assert gk.gateway_key() == "sk-new-vault"


def test_revoked_marker_is_read_under_the_legacy_name(monkeypatch) -> None:
    """The digest a pre-rename box recorded must keep its dead env key out of
    the way after the upgrade, before any migration has run."""
    monkeypatch.setenv(gk.LEGACY_GATEWAY_KEY_ENV, "sk-revoked")
    monkeypatch.setattr(gk, "_dead_env_loaded", False)
    rows = {
        gk.LEGACY_GATEWAY_KEY_DEAD_MARKER: gk._key_digest("sk-revoked"),
        gk.GATEWAY_KEY_ENV: "sk-good",
    }
    monkeypatch.setattr(gk, "_vault_get", rows.get)
    assert gk.gateway_key() == "sk-good"
    assert gk.enrollment_status()["env_key_revoked"] is True


def test_enrollment_writes_only_the_canonical_names(monkeypatch) -> None:
    monkeypatch.setenv(gk.LEGACY_GATEWAY_KEY_ENV, "sk-provisioned")
    vault = _FakeVault()
    monkeypatch.setattr(gk, "_writable_vault", lambda: vault)
    monkeypatch.setattr(gk, "_dead_env_loaded", True)

    assert gk.adopt_delivered_key("sk-delivered", "https://gw.test") is True

    assert vault.stored[gk.GATEWAY_KEY_ENV] == "sk-delivered"
    assert vault.stored[gk.GATEWAY_URL_ENV] == "https://gw.test"
    assert vault.stored[gk.GATEWAY_KEY_DEAD_MARKER] == gk._key_digest("sk-provisioned")
    assert not any(name.startswith("LIONCLAW_") for name in vault.stored), vault.stored


def test_vault_answers_when_env_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        gk, "_vault_get", lambda name: "sk-from-vault" if name == gk.GATEWAY_KEY_ENV else None
    )
    assert gk.gateway_key() == "sk-from-vault"


def test_base_url_normalization(monkeypatch) -> None:
    assert gk.openai_base_url() == "https://gateway.olano.ai/v1"
    monkeypatch.setenv(gk.GATEWAY_URL_ENV, "https://gw.staging.test/")
    assert gk.openai_base_url() == "https://gw.staging.test/v1"
    # Already /v1 — never doubled.
    monkeypatch.setenv(gk.GATEWAY_URL_ENV, "https://gw.staging.test/v1")
    assert gk.openai_base_url() == "https://gw.staging.test/v1"


# ── enrollment ───────────────────────────────────────────────────────────────


def _enrollment_ok(monkeypatch, vault: _FakeVault, *, key: str = "sk-minted") -> list[str]:
    """Wire up a passing control plane; returns the list of URLs POSTed to."""
    from olano.deployment import sources

    calls: list[str] = []
    monkeypatch.setattr(sources, "deployment_token", lambda: "olano_dep_tok")
    monkeypatch.setattr(sources, "cloud_api_url", lambda: "https://cloud.example.test")
    monkeypatch.setattr(gk, "_writable_vault", lambda: vault)

    def fake_post(url, **kwargs):
        calls.append(url)
        assert kwargs["headers"]["Authorization"] == "Bearer olano_dep_tok"
        return httpx.Response(
            200,
            json={"gateway_url": "https://gw.staging.test", "key": key},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def test_ensure_enrolls_when_keyless_and_persists_to_vault(monkeypatch) -> None:
    vault = _FakeVault()
    calls = _enrollment_ok(monkeypatch, vault)

    assert gk.ensure_gateway_key() == "sk-minted"
    assert calls == ["https://cloud.example.test/api/v1/deployments/gateway-key"]
    # Plaintext lands in the vault (the durable store on a managed box), and
    # the returned gateway URL rides along so a staging cloud pairs with its
    # staging gateway.
    assert vault.stored[gk.GATEWAY_KEY_ENV] == "sk-minted"
    assert vault.stored[gk.GATEWAY_URL_ENV] == "https://gw.staging.test"


def test_ensure_skips_enrollment_when_key_exists(monkeypatch) -> None:
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-existing")

    def boom(*a, **k):  # pragma: no cover - the assertion
        raise AssertionError("must not call the control plane")

    monkeypatch.setattr(httpx, "post", boom)
    assert gk.ensure_gateway_key() == "sk-existing"


def test_enrollment_needs_a_deployment_token(monkeypatch) -> None:
    from olano.deployment import sources

    monkeypatch.setattr(sources, "deployment_token", lambda: None)
    assert gk.ensure_gateway_key() is None


def test_enrollment_failure_is_contained_and_throttled(monkeypatch) -> None:
    """A dead control plane costs one bounded POST per window — later callers
    (other agents booting, the 401 hook) skip straight to None."""
    from olano.deployment import sources

    calls: list[str] = []
    monkeypatch.setattr(sources, "deployment_token", lambda: "olano_dep_tok")
    monkeypatch.setattr(sources, "cloud_api_url", lambda: "https://cloud.example.test")

    def fake_post(url, **kwargs):
        calls.append(url)
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", fake_post)
    assert gk.ensure_gateway_key() is None
    assert gk.ensure_gateway_key() is None  # throttled: no second POST
    assert len(calls) == 1


def test_refresh_forces_a_new_key_even_when_one_exists(monkeypatch) -> None:
    """Rotation path (and the 401 self-heal): mint fresh even though a key is
    already present — the control plane revokes the prior one server-side."""
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-stale")
    vault = _FakeVault()
    calls = _enrollment_ok(monkeypatch, vault, key="sk-fresh")

    assert gk.refresh_gateway_key() == "sk-fresh"
    assert len(calls) == 1
    assert vault.stored[gk.GATEWAY_KEY_ENV] == "sk-fresh"


def test_readonly_vault_still_returns_the_key(monkeypatch) -> None:
    """No vault ⇒ the key works for this process lifetime and is re-enrolled
    next boot (enrollment is idempotent server-side). Never a hard failure."""
    vault_calls: list[str] = []
    from olano.deployment import sources

    monkeypatch.setattr(sources, "deployment_token", lambda: "olano_dep_tok")
    monkeypatch.setattr(sources, "cloud_api_url", lambda: "https://cloud.example.test")
    monkeypatch.setattr(gk, "_writable_vault", lambda: (vault_calls.append("x"), None)[1])
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **k: httpx.Response(
            200, json={"key": "sk-minted"}, request=httpx.Request("POST", url)
        ),
    )
    assert gk.ensure_gateway_key() == "sk-minted"
    assert vault_calls  # it tried


def test_unwritable_vault_does_not_churn_enrollments(monkeypatch) -> None:
    """The memory cache makes an unpersisted key stick for the process: a
    second resolve returns the SAME key with no new network mint. Without it
    every build window re-enrolled — and each enroll revokes the prior key,
    401-ing the models already built with it (a rotation storm)."""
    from olano.deployment import sources

    monkeypatch.setattr(sources, "deployment_token", lambda: "olano_dep_tok")
    monkeypatch.setattr(sources, "cloud_api_url", lambda: "https://cloud.example.test")
    posts: list[str] = []

    def _post(url, **k):
        posts.append(url)
        return httpx.Response(
            200, json={"key": f"sk-mint-{len(posts)}"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(gk, "_next_attempt_at", float("-inf"))

    assert gk.ensure_gateway_key() == "sk-mint-1"
    # Backoff window reset: a re-enroll WOULD be allowed — the cache means it
    # is not needed.
    monkeypatch.setattr(gk, "_next_attempt_at", float("-inf"))
    assert gk.ensure_gateway_key() == "sk-mint-1"
    assert len(posts) == 1


# ── idempotency: a retry must replay, never rotate ───────────────────────────


def _failing_cloud(monkeypatch, *, error: Exception | None = None) -> list[dict]:
    """A control plane that always fails; returns the recorded request kwargs."""
    from olano.deployment import sources

    seen: list[dict] = []
    monkeypatch.setattr(sources, "deployment_token", lambda: "olano_dep_tok")
    monkeypatch.setattr(sources, "cloud_api_url", lambda: "https://cloud.example.test")

    def fake_post(url, **kwargs):
        seen.append(kwargs)
        raise error or httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


def test_retries_reuse_one_nonce_so_the_control_plane_replays(monkeypatch) -> None:
    """The incident this prevents: plaintext is returned once and stored
    nowhere, so a lost response used to revoke a working key and deliver
    nothing — and the retry did it again, forever. A stable nonce per
    enrollment episode makes the retry a replay."""
    seen = _failing_cloud(monkeypatch)

    assert gk.enroll_gateway_key() is None
    monkeypatch.setattr(gk, "_next_attempt_at", float("-inf"))
    assert gk.enroll_gateway_key() is None
    monkeypatch.setattr(gk, "_next_attempt_at", float("-inf"))
    assert gk.enroll_gateway_key() is None

    nonces = {k["headers"]["Idempotency-Key"] for k in seen}
    assert len(seen) == 3
    assert len(nonces) == 1, "every retry of one episode must carry the same nonce"


def test_a_new_episode_gets_a_fresh_nonce_so_rotation_still_works(monkeypatch) -> None:
    """Same nonce ⇒ replay; new nonce ⇒ genuine rotation. Once an enrollment is
    received the nonce must be retired, or the box could never rotate again."""
    vault = _FakeVault()
    seen: list[dict] = []
    from olano.deployment import sources

    monkeypatch.setattr(sources, "deployment_token", lambda: "olano_dep_tok")
    monkeypatch.setattr(sources, "cloud_api_url", lambda: "https://cloud.example.test")
    monkeypatch.setattr(gk, "_writable_vault", lambda: vault)

    def fake_post(url, **kwargs):
        seen.append(kwargs)
        return httpx.Response(
            200, json={"key": f"sk-{len(seen)}"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    assert gk.enroll_gateway_key() == "sk-1"
    assert gk.refresh_gateway_key() == "sk-2"

    first, second = (k["headers"]["Idempotency-Key"] for k in seen)
    assert first != second


def test_receipt_is_confirmed_so_plaintext_leaves_cloud_memory_early(monkeypatch) -> None:
    vault = _FakeVault()
    _enrollment_ok(monkeypatch, vault)
    confirmed: list[tuple] = []
    monkeypatch.setattr(gk, "_confirm_receipt", lambda *a: confirmed.append(a))

    assert gk.enroll_gateway_key() == "sk-minted"
    assert len(confirmed) == 1
    # (api_url, token, nonce) — the nonce being the one we just enrolled under.
    assert confirmed[0][0] == "https://cloud.example.test"
    assert confirmed[0][1] == "olano_dep_tok"


# ── backoff ──────────────────────────────────────────────────────────────────


def test_backoff_grows_and_caps(monkeypatch) -> None:
    """A flat window meant a multi-hour outage cost one blocking POST every
    minute forever. Growth is the point; the cap keeps recovery bounded."""
    assert gk._backoff_for(0) == 60.0
    assert gk._backoff_for(1) == 120.0
    assert gk._backoff_for(2) == 240.0
    assert gk._backoff_for(50) == gk._BACKOFF_MAX_SECONDS


def test_consecutive_failures_widen_the_window(monkeypatch) -> None:
    _failing_cloud(monkeypatch)

    gk.enroll_gateway_key()
    assert gk._consecutive_failures == 1
    first_window = gk._next_attempt_at

    monkeypatch.setattr(gk, "_next_attempt_at", float("-inf"))
    gk.enroll_gateway_key()
    assert gk._consecutive_failures == 2
    # Second window is measured from a later "now", but the important property
    # is that the *span* grew.
    assert gk._backoff_for(1) > gk._backoff_for(0)
    assert first_window != float("-inf")


def test_success_resets_the_backoff(monkeypatch) -> None:
    _failing_cloud(monkeypatch)
    gk.enroll_gateway_key()
    assert gk._consecutive_failures == 1

    vault = _FakeVault()
    _enrollment_ok(monkeypatch, vault)
    monkeypatch.setattr(gk, "_next_attempt_at", float("-inf"))
    assert gk.enroll_gateway_key() == "sk-minted"
    assert gk._consecutive_failures == 0
    assert gk._pending_nonce is None


def test_force_bypasses_the_backoff_window(monkeypatch) -> None:
    """A 401 is positive evidence the current key is dead — exactly when
    waiting is wrong. Previously `force` skipped only the cache check and then
    hit the same throttle, so a failed background attempt could swallow the
    self-heal outright."""
    seen = _failing_cloud(monkeypatch)

    assert gk.enroll_gateway_key() is None
    assert len(seen) == 1
    # Window is now closed for a normal caller…
    assert gk.enroll_gateway_key() is None
    assert len(seen) == 1
    # …but the self-heal gets through.
    assert gk.refresh_gateway_key() is None
    assert len(seen) == 2


# ── never block an agent build ───────────────────────────────────────────────


def test_background_mode_never_touches_the_network(monkeypatch) -> None:
    """With the runtime loop owning enrollment, `ensure_gateway_key` must return
    cache immediately — a degraded control plane cannot put a 10s stall inside
    an agent build or turn."""
    from olano.deployment import sources

    monkeypatch.setattr(sources, "deployment_token", lambda: "olano_dep_tok")
    monkeypatch.setattr(sources, "cloud_api_url", lambda: "https://cloud.example.test")

    def boom(*a, **k):  # pragma: no cover - the assertion
        raise AssertionError("must not call the control plane from a build")

    monkeypatch.setattr(httpx, "post", boom)
    monkeypatch.setattr(gk, "_background_enrollment", True)

    assert gk.ensure_gateway_key() is None
    # …and the loop is told someone wanted one, so it can act at once.
    assert gk._enroll_wanted.is_set()
    assert gk.wait_for_enrollment_request(0) is True


# ── diagnostics ──────────────────────────────────────────────────────────────


def test_state_distinguishes_unenrolled_from_unattached(monkeypatch) -> None:
    """The collapse this replaces: `bool(gateway_key() or deployment_token())`
    reported "connected" for a box that is attached but holds no key — i.e. a
    total managed-inference outage rendered as a healthy provider card."""
    from olano.deployment import sources

    monkeypatch.setattr(sources, "deployment_token", lambda: None)
    assert gk.enrollment_state() == "unattached"

    monkeypatch.setattr(sources, "deployment_token", lambda: "olano_dep_tok")
    assert gk.enrollment_state() == "unenrolled"

    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-live")
    assert gk.enrollment_state() == "enrolled"


def test_status_reports_the_failure_for_a_human(monkeypatch) -> None:
    _failing_cloud(monkeypatch, error=httpx.ConnectError("504 Gateway Timeout"))
    gk.enroll_gateway_key()

    status = gk.enrollment_status()
    assert status["state"] == "unenrolled"
    assert status["attached"] is True
    assert status["has_key"] is False
    assert status["consecutive_failures"] == 1
    assert "504" in str(status["last_error"])
    assert status["last_attempt_at"]
    assert status["retry_in_seconds"] and float(status["retry_in_seconds"]) > 0
    assert status["enrollment_pending"] is True


def test_sentinel_builds_are_counted_not_silent(monkeypatch) -> None:
    """Building with a placeholder key is right, but it must leave a trace — or
    the only symptom is a confusing 401 at first use."""
    gk.note_sentinel_build("olano-turbo")
    gk.note_sentinel_build("olano-vision")
    assert gk.enrollment_status()["unenrolled_model_builds"] == 2


# ── a revoked env key must not shadow the enrolled one ───────────────────────


def test_env_key_survives_until_proven_revoked(monkeypatch) -> None:
    """The env override is the operator escape hatch — it must keep winning
    until there is positive evidence the control plane killed that key."""
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-from-env")
    monkeypatch.setattr(gk, "_vault_get", lambda name: "sk-from-vault")
    assert gk.gateway_key() == "sk-from-env"


def test_a_revoked_env_key_steps_aside_for_the_enrolled_one(monkeypatch) -> None:
    """THE 401 LOOP. Provisioning writes a key into /opt/olano/olano.env, and
    every later enrollment REVOKES it. env outranks the vault, so the box kept
    presenting the dead key, 401ing, re-enrolling, persisting the good key to a
    vault nothing read, and 401ing again — forever, while every probe reported
    "enrolled" because a non-empty string was present."""
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-provisioned-then-revoked")
    vault = _FakeVault()
    _enrollment_ok(monkeypatch, vault, key="sk-fresh")

    # The 401 self-heal forces a re-enrollment; the mint revokes the env key.
    assert gk.refresh_gateway_key() == "sk-fresh"

    # The dead env value no longer wins…
    monkeypatch.setattr(gk, "_vault_get", lambda name: vault.stored.get(name))
    assert gk.gateway_key() == "sk-fresh"
    # …and the fact is durable, so a restart cannot resurrect the loop.
    assert vault.stored[gk.GATEWAY_KEY_DEAD_MARKER] == gk._key_digest("sk-provisioned-then-revoked")


def test_the_revoked_marker_is_a_digest_never_the_key(monkeypatch) -> None:
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-secret-value")
    vault = _FakeVault()
    _enrollment_ok(monkeypatch, vault, key="sk-fresh")
    gk.refresh_gateway_key()
    stored = vault.stored[gk.GATEWAY_KEY_DEAD_MARKER]
    assert "sk-secret-value" not in stored
    assert len(stored) == 16


def test_a_new_env_key_still_overrides_after_a_revocation(monkeypatch) -> None:
    """An operator pasting a fresh key into olano.env to unblock a box must
    still win — only the exact revoked value is skipped, not the mechanism."""
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-old-revoked")
    vault = _FakeVault()
    _enrollment_ok(monkeypatch, vault, key="sk-fresh")
    gk.refresh_gateway_key()

    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-operator-just-pasted-this")
    assert gk.gateway_key() == "sk-operator-just-pasted-this"


def test_the_marker_is_read_back_from_the_vault_on_a_fresh_process(monkeypatch) -> None:
    """After a restart the in-memory digest is gone; the vault marker is what
    stops the dead env key taking the lead again."""
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-revoked")
    monkeypatch.setattr(gk, "_dead_env_digest", None)
    monkeypatch.setattr(gk, "_dead_env_loaded", False)
    monkeypatch.setattr(
        gk,
        "_vault_get",
        lambda name: {
            gk.GATEWAY_KEY_DEAD_MARKER: gk._key_digest("sk-revoked"),
            gk.GATEWAY_KEY_ENV: "sk-good",
        }.get(name),
    )
    assert gk.gateway_key() == "sk-good"


def test_status_names_the_shadowing_env_key(monkeypatch) -> None:
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-revoked")
    monkeypatch.setattr(gk, "_dead_env_digest", gk._key_digest("sk-revoked"))
    monkeypatch.setattr(gk, "_dead_env_loaded", True)
    assert gk.enrollment_status()["env_key_revoked"] is True


def test_status_is_quiet_when_the_env_key_is_healthy(monkeypatch) -> None:
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-live")
    assert gk.enrollment_status()["env_key_revoked"] is False


def test_an_unrelated_enrollment_does_not_mark_a_matching_env_key(monkeypatch) -> None:
    """If the control plane hands back the SAME key we already had in env
    (a replay), nothing was revoked and nothing should be marked."""
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-same")
    vault = _FakeVault()
    _enrollment_ok(monkeypatch, vault, key="sk-same")
    gk.refresh_gateway_key()
    assert gk.GATEWAY_KEY_DEAD_MARKER not in vault.stored


# ── the failure must describe itself ─────────────────────────────────────────


def test_a_cdn_generated_error_is_named_as_such(monkeypatch, caplog) -> None:
    """The distinction two wrong fixes turned on: a 504 carrying `cf-ray` but no
    `x-do-app-origin` never reached the application, so no amount of handler
    work would have fixed it."""
    import logging

    resp = httpx.Response(
        504,
        headers={"cf-ray": "a2b7b56afff68aaa-IAD"},
        request=httpx.Request("POST", "https://cloud.example.test"),
    )
    with caplog.at_level(logging.WARNING, logger="olano.deployment.gateway_key"):
        gk._log_enrollment_response(resp, 61.4)
    assert "never reached the app" in caplog.text
    assert "61.4s" in caplog.text


def test_an_app_generated_error_names_the_origin(monkeypatch, caplog) -> None:
    import logging

    resp = httpx.Response(
        504,
        headers={"cf-ray": "abc-IAD", "x-do-app-origin": "dda1c050-d977"},
        request=httpx.Request("POST", "https://cloud.example.test"),
    )
    with caplog.at_level(logging.WARNING, logger="olano.deployment.gateway_key"):
        gk._log_enrollment_response(resp, 4.9)
    assert "reached the app" in caplog.text
    assert "dda1c050-d977" in caplog.text


def test_a_healthy_response_stays_quiet(caplog) -> None:
    import logging

    resp = httpx.Response(200, request=httpx.Request("POST", "https://cloud.example.test"))
    with caplog.at_level(logging.WARNING, logger="olano.deployment.gateway_key"):
        gk._log_enrollment_response(resp, 0.3)
    assert caplog.text == ""


def test_the_failure_line_carries_the_elapsed_time(monkeypatch, caplog) -> None:
    """How long the call took is the single most diagnostic number, and it was
    not recorded anywhere — a 504 at 5s and one at 60s are different bugs."""
    import logging

    _failing_cloud(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="olano.deployment.gateway_key"):
        gk.enroll_gateway_key()
    assert "enrollment failed after" in caplog.text


# ── key delivered over the settings channel ──────────────────────────────────


def test_digest_is_a_hash_never_the_key(monkeypatch) -> None:
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-secret")
    digest = gk.current_key_digest()
    assert digest and "sk-secret" not in digest and len(digest) == 64


def test_no_key_reports_none_which_is_what_earns_a_delivery(monkeypatch) -> None:
    assert gk.current_key_digest() is None


def test_adopting_a_delivered_key_makes_it_the_live_key(monkeypatch) -> None:
    """The path that actually gets a key onto a box when self-enrollment's own
    endpoint cannot be made to work."""
    vault = _FakeVault()
    monkeypatch.setattr(gk, "_writable_vault", lambda: vault)

    assert gk.adopt_delivered_key("sk-delivered", "https://gw.test") is True
    monkeypatch.setattr(gk, "_vault_get", lambda name: vault.stored.get(name))
    assert gk.gateway_key() == "sk-delivered"
    assert vault.stored[gk.GATEWAY_KEY_ENV] == "sk-delivered"
    assert vault.stored[gk.GATEWAY_URL_ENV] == "https://gw.test"


def test_adopting_clears_the_enrollment_backoff(monkeypatch) -> None:
    """The box now has what enrollment was trying to obtain; continuing to retry
    on a 30-minute ladder would be pure noise."""
    _failing_cloud(monkeypatch)
    gk.enroll_gateway_key()
    assert gk._consecutive_failures == 1

    monkeypatch.setattr(gk, "_writable_vault", lambda: _FakeVault())
    gk.adopt_delivered_key("sk-delivered")
    assert gk._consecutive_failures == 0
    assert gk._pending_nonce is None
    assert gk.enrollment_due() is True


def test_a_delivered_key_supersedes_a_revoked_env_key(monkeypatch) -> None:
    """Same shadowing trap as enrollment, reached through a different door: a
    dead env value outranks the vault, so a delivery would be invisible."""
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-env-revoked")
    vault = _FakeVault()
    monkeypatch.setattr(gk, "_writable_vault", lambda: vault)

    gk.adopt_delivered_key("sk-delivered")
    monkeypatch.setattr(gk, "_vault_get", lambda name: vault.stored.get(name))
    assert gk.gateway_key() == "sk-delivered"
    assert vault.stored[gk.GATEWAY_KEY_DEAD_MARKER] == gk._key_digest("sk-env-revoked")


def test_adopting_the_key_we_already_hold_is_a_no_op(monkeypatch) -> None:
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-live")
    assert gk.adopt_delivered_key("sk-live") is False


def test_junk_and_the_sentinel_are_refused(monkeypatch) -> None:
    assert gk.adopt_delivered_key("") is False
    assert gk.adopt_delivered_key("   ") is False
    assert gk.adopt_delivered_key(gk.UNENROLLED_SENTINEL) is False


# ── settings-channel fallback + withheld-delivery diagnostics ────────────────


def test_settings_channel_fallback_adopts_a_delivered_key(monkeypatch) -> None:
    """After a failed direct enrollment the loop asks the settings GET — the
    channel that stays up when the enrollment POST does not (workbuddy,
    2026-08-16) — and a delivered key becomes the live key immediately."""
    from olano.deployment import sources

    vault = _FakeVault()
    monkeypatch.setattr(gk, "_writable_vault", lambda: vault)
    monkeypatch.setattr(
        sources, "cloud_settings_config", lambda: sources.CloudConfig(url="https://x", token="t")
    )

    def fake_fetch(cfg):
        # fetch_remote adopts as a side effect; the license text is not ours.
        gk.adopt_delivered_key("sk-settings", "https://gw.test")
        return "LCDS1.license"

    monkeypatch.setattr(sources, "fetch_remote", fake_fetch)
    assert gk.enroll_via_settings_channel() == "sk-settings"
    assert vault.stored[gk.GATEWAY_KEY_ENV] == "sk-settings"


def test_settings_channel_fallback_survives_a_dead_cloud(monkeypatch) -> None:
    from olano.deployment import sources

    monkeypatch.setattr(
        sources, "cloud_settings_config", lambda: sources.CloudConfig(url="https://x", token="t")
    )

    def failing_fetch(cfg):
        raise sources.RemoteFetchError("transient", "settings fetch failed")

    monkeypatch.setattr(sources, "fetch_remote", failing_fetch)
    assert gk.enroll_via_settings_channel() is None


def test_settings_channel_fallback_without_a_token_is_a_no_op(monkeypatch) -> None:
    from olano.deployment import sources

    calls: list[object] = []
    monkeypatch.setattr(sources, "cloud_settings_config", lambda: None)
    monkeypatch.setattr(sources, "fetch_remote", lambda cfg: calls.append(cfg))
    assert gk.enroll_via_settings_channel() is None
    assert calls == []


def test_fetch_remote_adopts_a_key_riding_on_the_settings_response(monkeypatch) -> None:
    from olano.deployment import sources

    vault = _FakeVault()
    monkeypatch.setattr(gk, "_writable_vault", lambda: vault)

    def fake_get(url, **kwargs):
        return httpx.Response(
            200,
            json={
                "license": "LCDS1.x",
                "gateway_key": "sk-delivered",
                "gateway_url": "https://gw.test",
                "gateway_delivery": "delivered",
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    assert sources.fetch_remote(sources.CloudConfig(url="https://x", token="t")) == "LCDS1.x"
    assert gk.gateway_key() == "sk-delivered"


def test_a_keyless_poll_on_a_keyless_box_names_the_control_planes_reason(
    monkeypatch, caplog
) -> None:
    """The 2026-08-16 outage was invisible from the box: delivery failed
    server-side on every poll and the reason lived in pod logs nobody can
    read. Now the box's own log states it."""
    import logging

    from olano.deployment import sources

    def fake_get(url, **kwargs):
        return httpx.Response(200, json={"license": "LCDS1.x", "gateway_delivery": "mint_failed"})

    monkeypatch.setattr(httpx, "get", fake_get)
    with caplog.at_level(logging.WARNING, logger="olano.deployment.sources"):
        sources.fetch_remote(sources.CloudConfig(url="https://x", token="t"))
    assert "mint_failed" in caplog.text
    assert "no gateway key" in caplog.text


def test_the_withheld_reason_stays_quiet_on_an_enrolled_box(monkeypatch, caplog) -> None:
    """`holds_current_key` every 15 minutes is not news; the WARNING is
    reserved for a box that actually has no key."""
    import logging

    from olano.deployment import sources

    monkeypatch.setattr(gk, "_memory_key", "sk-live")

    def fake_get(url, **kwargs):
        return httpx.Response(
            200, json={"license": "LCDS1.x", "gateway_delivery": "holds_current_key"}
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    with caplog.at_level(logging.WARNING, logger="olano.deployment.sources"):
        sources.fetch_remote(sources.CloudConfig(url="https://x", token="t"))
    assert caplog.text == ""


def test_a_reasonless_keyless_poll_stays_quiet(monkeypatch, caplog) -> None:
    """A pre-reason control plane (or a response where delivery was not
    consulted) must not produce a misleading warning."""
    import logging

    from olano.deployment import sources

    def fake_get(url, **kwargs):
        return httpx.Response(200, json={"license": "LCDS1.x"})

    monkeypatch.setattr(httpx, "get", fake_get)
    with caplog.at_level(logging.WARNING, logger="olano.deployment.sources"):
        sources.fetch_remote(sources.CloudConfig(url="https://x", token="t"))
    assert caplog.text == ""
