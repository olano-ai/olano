"""The second key family: an Olano account API key pasted into a self-hosted box.

Property under test: on an OPEN box, `OLANO_CREDITS_KEY` (env or vault) makes
the box spend Olano credits exactly as a managed deployment does (olano:*
tiers, managed media/speech/cognition defaults, the managed services, the
credits widgets), while a MANAGED box never reads it and posture resolution
does not know it exists. Nothing on the box can renew a refused key, so a 401
becomes a "paste a valid key" error, never a re-enrollment loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from olano.config import system_settings as ss
from olano.core import actionable_errors as ae
from olano.deployment import gateway_key as gk
from olano.deployment import services_client

ACCOUNT = "sk-olano-" + "a" * 40


class _Vault:
    def __init__(self, rows: dict[str, str] | None = None) -> None:
        self.rows = dict(rows or {})
        self.sets: list[tuple[str, str, dict]] = []

    def get(self, name: str, touch: bool = False):
        return self.rows.get(name)

    def set(self, name: str, value: str, **kwargs) -> None:
        self.rows[name] = value
        self.sets.append((name, value, kwargs))

    def delete(self, name: str, agent: str | None = None) -> bool:
        return self.rows.pop(name, None) is not None


@pytest.fixture(autouse=True)
def _keyless(monkeypatch, tmp_path: Path):
    """No deployment key, no manifest, no token: a self-hosted box by default."""
    for name in (
        gk.GATEWAY_KEY_ENV,
        gk.LEGACY_GATEWAY_KEY_ENV,
        "LIONCLAW_DEPLOYMENT_TOKEN",
        "LIONCLAW_DEPLOYMENT_MANAGED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LIONCLAW_INSTALLATION_FILE", str(tmp_path / "installation.json"))
    monkeypatch.setattr(gk, "_memory_key", None)
    monkeypatch.setattr(gk, "_dead_env_loaded", True)
    monkeypatch.setattr(gk, "_dead_env_digest", None)
    monkeypatch.setattr(gk, "_vault_get", lambda name: None)
    monkeypatch.setattr(gk, "_writable_vault", lambda: None)
    from olano.deployment import sources

    monkeypatch.setattr(sources, "deployment_token", lambda: None)
    gk.reset_account_key_memo()


def _open(monkeypatch, features: dict[str, bool] | None = None) -> None:
    from olano.deployment import resolver

    feats = features or {}
    status = SimpleNamespace(
        posture="open", settings=SimpleNamespace(feature_enabled=lambda n: feats.get(n, True))
    )
    monkeypatch.setattr(resolver, "deployment_status", lambda now=None: status)


def _managed(monkeypatch) -> None:
    from olano.deployment import resolver

    status = SimpleNamespace(
        posture="enforced", settings=SimpleNamespace(feature_enabled=lambda n: True)
    )
    monkeypatch.setattr(resolver, "deployment_status", lambda now=None: status)


# ── resolution ───────────────────────────────────────────────────────────────


def test_names() -> None:
    assert gk.ACCOUNT_KEY_ENV == "OLANO_CREDITS_KEY"
    assert gk.ACCOUNT_KEY_PREFIX == "sk-olano-"
    assert gk.ACCOUNT_KEY_ENV in gk.VAULT_ROW_DESCRIPTIONS


def test_open_box_serves_the_account_key_from_env(monkeypatch) -> None:
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    assert gk.account_key() == ACCOUNT
    assert gk.gateway_key() == ACCOUNT
    assert gk.gateway_key_source() == "account"
    # The three-valued state is unchanged: a keyed box is enrolled, whichever
    # family the key comes from (every consumer does a dict lookup on it).
    assert gk.enrollment_state() == "enrolled"
    status = gk.enrollment_status()
    assert status["source"] == "account"
    assert status["account_key_rejected"] is False


def test_legacy_env_spelling_is_mirrored(monkeypatch) -> None:
    """env_aliases pairs OLANO_CREDITS_KEY with LIONCLAW_CREDITS_KEY, so a box
    that set the LIONCLAW_ spelling in .env still resolves it."""
    from olano import env_aliases

    monkeypatch.setenv("LIONCLAW_CREDITS_KEY", ACCOUNT)
    env_aliases.apply_env_aliases()
    assert gk.account_key() == ACCOUNT


def test_open_box_serves_the_account_key_from_the_vault(monkeypatch) -> None:
    monkeypatch.setattr(
        gk, "_vault_get", lambda name: ACCOUNT if name == gk.ACCOUNT_KEY_ENV else None
    )
    assert gk.gateway_key() == ACCOUNT
    assert gk.gateway_key_source() == "account"


def test_no_key_anywhere_is_still_unattached() -> None:
    assert gk.gateway_key() is None
    assert gk.gateway_key_source() is None
    assert gk.enrollment_state() == "unattached"


def test_enrolled_key_outranks_the_account_key(monkeypatch) -> None:
    """A box holding both (an operator who pasted a key, then attached) keeps
    spending from the deployment: that is the wallet the cloud bills."""
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    monkeypatch.setattr(gk, "_memory_key", "sk-enrolled")
    assert gk.gateway_key() == "sk-enrolled"
    assert gk.gateway_key_source() == "deployment"
    monkeypatch.setenv(gk.GATEWAY_KEY_ENV, "sk-from-env")
    assert gk.gateway_key() == "sk-from-env"
    assert gk.gateway_key_source() == "env"


@pytest.mark.parametrize("marker", ["LIONCLAW_DEPLOYMENT_TOKEN", "LIONCLAW_DEPLOYMENT_MANAGED"])
def test_a_managed_marker_hides_the_account_key(monkeypatch, marker) -> None:
    """Rule 4 of the plan: the managed path is identical to before this family
    existed. With any provisioning evidence the key is never even read."""
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    monkeypatch.setenv(marker, "1" if marker.endswith("MANAGED") else "olano_dep_tok")
    assert gk.gateway_key() is None
    assert gk.gateway_key_source() is None


def test_a_managed_manifest_hides_the_account_key(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    (tmp_path / "installation.json").write_text(
        json.dumps({"managed": True, "mode": "local", "deployment_id": "dep_1"})
    )
    assert gk.gateway_key() is None
    # An unreadable manifest fails closed on this path too.
    (tmp_path / "installation.json").write_text("{not json")
    assert gk.gateway_key() is None
    # `managed: false` is a self-hosted box and reads the key.
    (tmp_path / "installation.json").write_text(json.dumps({"managed": False}))
    assert gk.gateway_key() == ACCOUNT


def test_a_vault_deployment_token_hides_the_account_key(monkeypatch) -> None:
    from olano.deployment import sources

    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    monkeypatch.setattr(sources, "deployment_token", lambda: "olano_dep_tok")
    assert gk.gateway_key() is None
    assert gk.enrollment_state() == "unenrolled"


def test_vault_reads_are_memoised(monkeypatch) -> None:
    """Reached per build and per request; one vault read per process for a hit,
    one per TTL for a miss."""
    reads: list[str] = []

    def vault_get(name):
        reads.append(name)
        return None

    monkeypatch.setattr(gk, "_vault_get", vault_get)
    assert gk.account_key() is None
    assert gk.account_key() is None
    assert reads == [gk.ACCOUNT_KEY_ENV]
    # Past the miss TTL the vault is asked again (a key written elsewhere).
    monkeypatch.setattr(gk, "_account_key_checked_at", float("-inf"))
    monkeypatch.setattr(gk, "_vault_get", lambda name: ACCOUNT)
    assert gk.account_key() == ACCOUNT
    monkeypatch.setattr(gk, "_vault_get", vault_get)
    assert gk.account_key() == ACCOUNT  # a hit is held
    gk.reset_account_key_memo()
    assert gk.account_key() is None


# ── store / forget / rejection ───────────────────────────────────────────────


def test_store_and_forget_round_trip(monkeypatch) -> None:
    vault = _Vault()
    monkeypatch.setattr(gk, "_writable_vault", lambda: vault)
    monkeypatch.setattr(gk, "_vault_get", lambda name: vault.rows.get(name))

    gk.store_account_key(f"  {ACCOUNT}\n")
    assert vault.rows[gk.ACCOUNT_KEY_ENV] == ACCOUNT
    name, _, kwargs = vault.sets[0]
    assert kwargs["category"] == gk.VAULT_ROW_CATEGORY
    assert kwargs["description"] == gk.VAULT_ROW_DESCRIPTIONS[gk.ACCOUNT_KEY_ENV]
    assert gk.gateway_key() == ACCOUNT

    assert gk.forget_account_key() is True
    assert gk.ACCOUNT_KEY_ENV not in vault.rows
    assert gk.gateway_key() is None
    assert gk.forget_account_key() is False


def test_store_refuses_an_unwritable_vault(monkeypatch) -> None:
    """Unlike the enrolled key there is no control plane to re-mint from on the
    next boot, so a best-effort write would silently lose the key."""
    with pytest.raises(RuntimeError, match="vault_locked"):
        gk.store_account_key(ACCOUNT)
    with pytest.raises(RuntimeError, match="vault_locked"):
        gk.forget_account_key()
    with pytest.raises(ValueError):
        gk.store_account_key("   ")


def test_rejection_mark_follows_the_key(monkeypatch) -> None:
    vault = _Vault({gk.ACCOUNT_KEY_ENV: ACCOUNT})
    monkeypatch.setattr(gk, "_writable_vault", lambda: vault)
    monkeypatch.setattr(gk, "_vault_get", lambda name: vault.rows.get(name))

    assert gk.account_key_rejected() is False
    gk.note_account_key_rejected()
    assert gk.account_key_rejected() is True
    assert gk.enrollment_status()["account_key_rejected"] is True
    # Pasting a different key is a new fact.
    gk.store_account_key("sk-olano-" + "b" * 40)
    assert gk.account_key_rejected() is False
    # And a rejected key pasted again is rejected only if it is the same one.
    gk.note_account_key_rejected()
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    assert gk.account_key_rejected() is False


def test_401_self_heal_records_the_refusal_instead_of_re_enrolling(monkeypatch) -> None:
    from olano.models import olano_gateway as og

    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    called: list[str] = []
    monkeypatch.setattr(gk, "refresh_gateway_key", lambda: called.append("refresh") or None)
    # A model already holding the (refused) key: nothing to swap in, so the
    # heal reaches the decision between re-enrolling and recording.
    client = SimpleNamespace(api_key=ACCOUNT)
    model = SimpleNamespace(root_client=client, root_async_client=client)
    og._refresh_and_swap(model)
    assert called == []
    assert gk.account_key_rejected() is True


def test_a_value_without_the_prefix_is_not_a_key(monkeypatch) -> None:
    """A deployment key pasted in the wrong slot, or garbage, must never reach
    the gateway header as if it were an account key."""
    _open(monkeypatch)
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, "olano_dep_not_an_account_key")
    assert gk.account_key() is None
    assert gk.gateway_key() is None
    monkeypatch.delenv(gk.ACCOUNT_KEY_ENV)
    gk.reset_account_key_memo()
    monkeypatch.setattr(gk, "_vault_get", lambda name: "sk-live-looks-like-litellm")
    assert gk.account_key() is None
    gk.reset_account_key_memo()
    monkeypatch.setattr(gk, "_vault_get", lambda name: ACCOUNT)
    assert gk.account_key() == ACCOUNT


def test_a_vault_hit_expires_too(monkeypatch) -> None:
    """A key replaced from another process (the vault CLI) is picked up within
    the hit TTL; before, a hit was held for the life of the process."""
    values = [ACCOUNT]
    monkeypatch.setattr(gk, "_vault_get", lambda name: values[0])
    gk.reset_account_key_memo()
    assert gk.account_key() == ACCOUNT
    values[0] = "sk-olano-" + "c" * 40
    assert gk.account_key() == ACCOUNT  # memoised
    monkeypatch.setattr(gk, "_account_key_checked_at", float("-inf"))  # the TTL elapsed
    assert gk.account_key() == values[0]


def test_classifying_a_refused_account_key_marks_it(monkeypatch) -> None:
    """The 401 self-heal is throttled to one attempt per window, so the mark
    it records could lag the error the person just read by a minute; the
    classifier records it too."""
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    assert gk.account_key_rejected() is False
    err = ae.classify_exception(
        Exception("Error code: 401 - Invalid proxy server token passed"), "olano"
    )
    assert err is not None and err.code == ae.CODE_OLANO_KEY_INVALID
    assert gk.account_key_rejected() is True


def test_a_successful_call_clears_the_rejection_mark(monkeypatch) -> None:
    from olano.models import olano_gateway as og

    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    gk.note_account_key_rejected()
    assert gk.account_key_rejected() is True
    assert gk.account_key_rejection_pending() is True
    og._note_success()
    assert gk.account_key_rejected() is False
    assert gk.account_key_rejection_pending() is False
    og._note_success()  # idempotent, and cheap when nothing is recorded


def test_the_402_message_follows_the_wallet_not_the_token_spelling() -> None:
    """The managed sentence is the pre-account-key text (its dash written as a
    colon); the cloud's `scope` wins over the family when it names the wallet."""
    managed = services_client.credits_exhausted_message("deployment")
    assert managed == (
        "This deployment is out of olano credits: buy more on your "
        "deployment card at cloud.olano.ai."
    )
    assert "Your Olano account" in services_client.credits_exhausted_message("account")
    assert services_client.credits_exhausted_message("deployment", {"scope": "account"}).startswith(
        "Your Olano account"
    )
    assert services_client.credits_exhausted_message("account", {"scope": "bogus"}).startswith(
        "Your Olano account"
    )


# ── the predicate split ──────────────────────────────────────────────────────


def test_credits_available_on_an_open_box_only_with_a_key(monkeypatch) -> None:
    _open(monkeypatch)
    assert ss.managed_olano_default() is False
    assert ss.olano_credits_available() is False
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    assert ss.olano_credits_available() is True
    # Posture is untouched: the security gates keep reading open.
    assert ss.managed_olano_default() is False


def test_managed_box_is_available_without_consulting_the_key(monkeypatch) -> None:
    _managed(monkeypatch)
    assert ss.olano_credits_available() is True


def test_a_keyed_open_box_defaults_like_a_managed_one(monkeypatch, tmp_path: Path) -> None:
    """The stated behaviour change, and only on a self-hosted box: pasting a
    key moves every default:* alias, the cognition models, STT and the
    archetype brain onto the olano tiers; removing it moves them back."""
    from olano.archetypes import installer
    from olano.archetypes.installer import default_archetype_model

    _open(monkeypatch)
    byok = ss.builtin_default_model_targets()
    assert not any(v.startswith("olano:") for v in byok.values())
    assert default_archetype_model("merli") is None
    assert ss.default_cognition_models() == (
        ss.SystemSettings().fast_model,
        ss.SystemSettings().deep_model,
    )
    assert ss.load_speech_settings().stt.provider == "disabled"

    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    assert ss.builtin_default_model_targets() == dict(ss.MANAGED_DEFAULT_MODEL_TARGETS)
    assert default_archetype_model("merli") == installer.MANAGED_ARCHETYPE_MODEL
    assert default_archetype_model("merli") is not None
    assert ss.default_cognition_models() == (ss.OLANO_FAST_MODEL, ss.OLANO_DEEP_MODEL)
    assert ss.load_speech_settings().stt.provider == "olano"

    monkeypatch.delenv(gk.ACCOUNT_KEY_ENV)
    assert ss.builtin_default_model_targets() == byok


def test_cost_display_defaults_to_credits_but_stays_unlocked(monkeypatch) -> None:
    _open(monkeypatch)
    monkeypatch.delenv("LIONCLAW_COST_DISPLAY", raising=False)
    assert ss.cost_display_mode() == "usd"
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    assert ss.cost_display_mode() == "credits"
    assert ss.cost_display_locked() is False
    # The operator's own choice still wins, as does the env.
    ss.set_cost_display_mode("usd")
    assert ss.cost_display_mode() == "usd"
    ss.set_cost_display_mode("credits")
    monkeypatch.setenv("LIONCLAW_COST_DISPLAY", "usd")
    assert ss.cost_display_mode() == "usd"


def test_media_gateway_reachable_with_a_key_on_an_open_box(monkeypatch) -> None:
    from olano.tools import image_tools

    _open(monkeypatch)
    assert image_tools.gateway_media_reachable() is False
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    assert image_tools.gateway_media_reachable() is True


# ── the managed-service and credits clients ──────────────────────────────────


def test_services_available_with_a_key_on_an_open_box(monkeypatch) -> None:
    _open(monkeypatch)
    assert services_client.services_available() is False
    assert services_client.service_credential() is None
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    assert services_client.services_available() is True
    assert services_client.service_credential() == ACCOUNT
    # The entitlement check still has a say (every feature is on by default).
    _open(monkeypatch, features={"olano_web_search": False})
    assert services_client.services_available() is False


def test_managed_box_never_sends_the_account_key(monkeypatch) -> None:
    _managed(monkeypatch)
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    assert services_client.service_credential() is None
    assert services_client.services_available() is False


def test_service_search_posts_the_account_key(monkeypatch) -> None:
    from olano.deployment import sources

    _open(monkeypatch)
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    monkeypatch.setattr(sources, "cloud_api_url", lambda: "https://cloud.example.test")
    calls: list[dict] = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers})
        return httpx.Response(402, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(services_client.ServiceUnavailable) as info:
        services_client.service_search(query="x")
    assert info.value.code == "credits_exhausted"
    assert "account" in str(info.value).lower()
    assert "deployment card" not in str(info.value)
    assert calls[0]["url"] == "https://cloud.example.test/api/v1/services/search"
    assert calls[0]["headers"]["Authorization"] == f"Bearer {ACCOUNT}"


def test_credits_and_usage_clients_read_the_account_twins(monkeypatch) -> None:
    from olano.deployment import credits_client, service_usage_client, sources

    _open(monkeypatch)
    monkeypatch.setattr(sources, "cloud_api_url", lambda: "https://cloud.example.test")
    credits_client.invalidate_cache()
    service_usage_client.invalidate_cache()
    assert credits_client.get_balance(force=True) is None
    assert service_usage_client.get_usage(force=True) is None

    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    seen: list[tuple[str, str]] = []

    def get(url, headers=None, timeout=None, params=None):
        seen.append((url, headers["Authorization"]))
        body = (
            {"schemaVersion": 1, "balance": 500, "scope": "account"}
            if url.endswith("/balance")
            else {"schemaVersion": 1, "services": [], "scope": "account"}
        )
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    balance = credits_client.get_balance(force=True)
    usage = service_usage_client.get_usage(force=True)
    assert balance and balance["balance"] == 500
    assert usage and usage["services"] == []
    assert seen == [
        ("https://cloud.example.test/api/v1/account/credits/balance", f"Bearer {ACCOUNT}"),
        ("https://cloud.example.test/api/v1/account/usage/services", f"Bearer {ACCOUNT}"),
    ]


def test_deployment_token_still_takes_the_deployment_routes(monkeypatch) -> None:
    from olano.deployment import credits_client, sources

    monkeypatch.setattr(sources, "deployment_token", lambda: "olano_dep_tok")
    assert credits_client._credential() == ("olano_dep_tok", "/api/v1/credits/balance")


# ── errors ───────────────────────────────────────────────────────────────────


def test_401_on_an_account_key_is_a_paste_a_new_key_error(monkeypatch) -> None:
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    err = ae.classify_exception(Exception("Error code: 401 - unauthorized"), "olano")
    assert err is not None
    assert err.code == ae.CODE_OLANO_KEY_INVALID
    assert err.retryable is False
    assert err.remediation is not None
    assert err.remediation.kind == ae.REMEDY_SET_KEY
    assert err.remediation.action and err.remediation.action["provider"] == "olano"
    assert "cloud.olano.ai" in err.message


def test_401_on_a_deployment_key_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(gk, "_memory_key", "sk-enrolled")
    err = ae.classify_exception(Exception("Error code: 401 - unauthorized"), "olano")
    assert err is not None and err.code == ae.CODE_AUTH_EXPIRED


def test_exhausted_account_wallet_points_at_billing(monkeypatch) -> None:
    monkeypatch.setenv(gk.ACCOUNT_KEY_ENV, ACCOUNT)
    err = ae.classify_exception(Exception("ExceededBudget: max_budget reached"), "olano")
    assert err is not None and err.code == ae.CODE_OLANO_CREDITS
    assert "account" in err.message.lower()
    assert err.remediation is not None
    assert err.remediation.url == "https://cloud.olano.ai/app/billing"
    assert ae.deployment_card_url() == "https://cloud.olano.ai/app/billing"
