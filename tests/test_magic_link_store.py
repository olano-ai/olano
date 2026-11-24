"""Unit tests for the magic-link store + service gates.

The security properties these lock down are the reason the feature can be
public at all: a link is only openable by someone holding both the token and
the PIN, it stops working the moment it is used, and none of the three secrets
involved (token, PIN, or a submitted credential) is ever written to disk.
"""

from __future__ import annotations

import json

import pytest

from olano.config.system_settings import MagicLinkSettings, set_magic_link_settings
from olano.magic_links import service, store
from olano.magic_links.flows import BaseFlow, register


@pytest.fixture(autouse=True)
def public_origin(monkeypatch):
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://acme.olano.run")
    # The origin resolver also consults a module-level "last seen" cache.
    import olano.core.mcp_setup as mcp_setup

    monkeypatch.setattr(mcp_setup, "_LAST_SEEN_ORIGIN", None)


class _FakeFlow(BaseFlow):
    """A form flow with no external dependencies, for exercising the machinery."""

    id = "test-flow"
    label = "Test service"
    kind = "form"

    def validate(self, params):
        if params.get("boom"):
            raise ValueError("nope")
        return dict(params)

    def submit(self, link, fields):
        return {"status": store.STATUS_DONE, "result": f"stored {fields.get('value', '')[:2]}…"}


register(_FakeFlow())


def _mint(flow="test-flow", **params):
    return service.mint_link(
        flow,
        params=params,
        requested_by={"channel": "telegram", "username": "jrivera", "agent": "merli"},
        notify={"channel_type": "telegram", "target": "42", "agent_name": "merli"},
        minted_by="test",
    )


# ── token + PIN ──────────────────────────────────────────────────────────────


def test_token_and_pin_are_never_persisted():
    minted = _mint()
    token = minted.url.rsplit("/", 1)[-1]
    raw = store.get_olano_paths().magic_links_store_path.read_text()
    assert token not in raw
    assert minted.pin not in raw
    # What IS stored is a hash of the token.
    assert store.hash_token(token) in raw


def test_resolves_by_token_and_rejects_anything_else():
    minted = _mint()
    token = minted.url.rsplit("/", 1)[-1]
    assert service.resolve_token(token) is not None
    assert service.resolve_token(token + "x") is None
    assert service.resolve_token("") is None


def test_wrong_pin_is_counted_and_right_pin_unlocks():
    minted = _mint()
    record = service.resolve_token(minted.url.rsplit("/", 1)[-1])
    wrong = "000000" if minted.pin != "000000" else "111111"

    ok, unlock, error = service.unlock_link(record, wrong)
    assert (ok, unlock) == (False, "")
    assert "Incorrect PIN" in error

    record = store.get(record.id)
    ok, unlock, error = service.unlock_link(record, minted.pin)
    assert ok and unlock and not error

    record = store.get(record.id)
    assert record.check_unlock(unlock)
    assert not record.check_unlock("some-other-value")


def test_link_burns_after_max_pin_attempts():
    set_magic_link_settings({"max_pin_attempts": 3})
    minted = _mint()
    token = minted.url.rsplit("/", 1)[-1]
    wrong = "000000" if minted.pin != "000000" else "111111"

    for _ in range(3):
        service.unlock_link(service.resolve_token(token), wrong)

    record = service.resolve_token(token)
    assert record.status == store.STATUS_REVOKED
    assert not record.serves_actions()
    # Even the correct PIN can't revive it.
    ok, _, error = service.unlock_link(record, minted.pin)
    assert not ok and "no longer active" in error


def test_attempt_unlock_check_and_count_is_one_atomic_operation():
    """The cap must not rest on the single-worker/no-await accident.

    ``store.attempt_unlock`` does the verify and the increment inside one locked
    read-modify-write, so N verifications against the same pre-burn record are
    impossible — the (N+1)th caller re-reads a record that is already revoked.
    """
    set_magic_link_settings({"max_pin_attempts": 2})
    minted = _mint()
    link_id = service.resolve_token(minted.url.rsplit("/", 1)[-1]).id
    wrong = "000000" if minted.pin != "000000" else "111111"

    first = store.attempt_unlock(link_id, wrong, 2)
    assert not first.ok and "1 try left" in first.error
    second = store.attempt_unlock(link_id, wrong, 2)
    assert not second.ok and "disabled" in second.error
    # The link is burned; a third attempt (even correct) sees it terminal.
    third = store.attempt_unlock(link_id, minted.pin, 2)
    assert not third.ok and "no longer active" in third.error
    assert store.get(link_id).status == store.STATUS_REVOKED


def test_attempt_unlock_success_issues_a_verifiable_token_and_resets_count():
    minted = _mint()
    link_id = service.resolve_token(minted.url.rsplit("/", 1)[-1]).id
    wrong = "000000" if minted.pin != "000000" else "111111"
    store.attempt_unlock(link_id, wrong, 5)  # one miss on the record

    out = store.attempt_unlock(link_id, minted.pin, 5)
    assert out.ok and out.raw_unlock
    record = store.get(link_id)
    assert record.pin_attempts == 0  # reset on success
    assert record.check_unlock(out.raw_unlock)
    assert not record.check_unlock("not-the-token")


def test_claim_finalize_returns_true_exactly_once_and_persists():
    """The completion guard is on the record (bounded by 24h retention), not an
    in-process set — so it survives a restart and never double-notifies."""
    minted = _mint()
    link_id = service.resolve_token(minted.url.rsplit("/", 1)[-1]).id
    store.finish(link_id, status=store.STATUS_DONE, result="ok")

    assert [store.claim_finalize(link_id) for _ in range(3)] == [True, False, False]
    assert store.get(link_id).finalized is True
    # A record that no longer exists can't be claimed.
    assert store.claim_finalize("hs_nonexistent") is False


def test_pin_can_be_disabled_by_policy():
    set_magic_link_settings({"pin": "off"})
    minted = _mint()
    assert minted.pin == ""
    record = service.resolve_token(minted.url.rsplit("/", 1)[-1])
    assert not record.requires_pin
    assert record.unlocked()


def test_unknown_pin_policy_value_falls_back_to_required():
    """A typo in config.yaml must not silently disable the PIN gate."""
    assert MagicLinkSettings.model_validate({"pin": "maybe"}).pin == "required"
    assert MagicLinkSettings.model_validate({"pin": "OFF"}).pin == "off"


# ── expiry + single use ──────────────────────────────────────────────────────


def test_expired_link_stops_serving_actions():
    from datetime import UTC, datetime, timedelta

    minted = _mint()
    record = service.resolve_token(minted.url.rsplit("/", 1)[-1])
    # Just past its TTL — a record older than the retention window is pruned
    # outright, which is a different path (see test_records_are_pruned…).
    lapsed = (datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds")
    store.mutate(record.id, lambda r: setattr(r, "expires_at", lapsed))
    record = store.get(record.id)
    assert record.is_expired
    assert not record.serves_actions()
    # Reading state finalizes it rather than leaving it dangling.
    service.link_state(record)
    assert store.get(record.id).status == store.STATUS_EXPIRED


def test_records_are_pruned_after_the_retention_window():
    minted = _mint()
    record = service.resolve_token(minted.url.rsplit("/", 1)[-1])
    store.mutate(record.id, lambda r: setattr(r, "expires_at", "2020-01-01T00:00:00+00:00"))
    # Any mutation prunes first, so a long-dead record simply disappears.
    store.mutate(record.id, lambda r: store.note(r, "touch"))
    assert store.get(record.id) is None


async def test_completed_link_cannot_be_reused():
    minted = _mint()
    record = service.resolve_token(minted.url.rsplit("/", 1)[-1])
    service.unlock_link(record, minted.pin)

    out = await service.act_on_link(store.get(record.id), "submit", {"value": "hunter2"})
    assert out["status"] == store.STATUS_DONE

    record = store.get(record.id)
    assert record.is_terminal and not record.serves_actions()
    with pytest.raises(service.MagicLinkError, match="no longer active"):
        await service.act_on_link(record, "submit", {"value": "hunter2"})


async def test_submitted_value_never_reaches_the_store():
    minted = _mint()
    record = service.resolve_token(minted.url.rsplit("/", 1)[-1])
    service.unlock_link(record, minted.pin)
    await service.act_on_link(store.get(record.id), "submit", {"value": "sk-live-supersecret"})
    raw = store.get_olano_paths().magic_links_store_path.read_text()
    assert "sk-live-supersecret" not in raw


def test_revoke_kills_a_live_link():
    minted = _mint()
    record = service.resolve_token(minted.url.rsplit("/", 1)[-1])
    assert service.revoke_link(record.id)
    assert not store.get(record.id).serves_actions()
    assert not service.revoke_link("hs_nonexistent")


# ── mint gates ───────────────────────────────────────────────────────────────


def test_group_chats_are_refused_by_default():
    with pytest.raises(service.MagicLinkError, match="group chat"):
        service.mint_link(
            "test-flow", params={}, requested_by={"is_group": True}, notify={}, minted_by="t"
        )


def test_group_chats_can_be_allowed_explicitly():
    set_magic_link_settings({"allow_groups": True})
    minted = service.mint_link(
        "test-flow", params={}, requested_by={"is_group": True}, notify={}, minted_by="t"
    )
    assert minted.url


def test_disabled_feature_refuses_to_mint():
    set_magic_link_settings({"enabled": False})
    with pytest.raises(service.MagicLinkError, match="disabled"):
        _mint()


def test_no_public_origin_gives_an_actionable_refusal(monkeypatch):
    monkeypatch.delenv("LIONCLAW_PUBLIC_URL", raising=False)
    with pytest.raises(service.MagicLinkError, match="LIONCLAW_PUBLIC_URL"):
        _mint()


def test_unknown_flow_lists_the_known_ones():
    with pytest.raises(service.MagicLinkError, match="Available:"):
        service.mint_link("no-such-flow", params={}, requested_by={}, notify={}, minted_by="t")


def test_adapter_validation_errors_surface_to_the_caller():
    with pytest.raises(service.MagicLinkError, match="nope"):
        _mint(boom=True)


# ── record hygiene ───────────────────────────────────────────────────────────


def test_public_record_carries_no_secrets():
    minted = _mint()
    record = service.resolve_token(minted.url.rsplit("/", 1)[-1])
    payload = json.dumps(record.public())
    assert "token_hash" not in payload
    assert "pin_hash" not in payload
    assert "unlock_hash" not in payload
    assert record.public()["pin_required"] is True


def test_audit_trail_is_bounded():
    minted = _mint()
    record = service.resolve_token(minted.url.rsplit("/", 1)[-1])
    for i in range(80):
        store.mutate(record.id, lambda r, i=i: store.note(r, "poll", detail=str(i)))
    assert len(store.get(record.id).audit) <= 50


# ── page hardening ───────────────────────────────────────────────────────────


def test_page_escapes_state_embedded_in_the_inline_script():
    """A provider ``error_description`` carrying ``</script>`` must not break out
    of the inline bootstrap script — even though CSP already contains it and the
    victim would have to hold their own link+PIN (self-XSS)."""
    from olano.magic_links import page

    html_out = page.render_page(
        base_path="/api/connect/x",
        requested_by={},
        state={
            "kind": "error",
            "status": "error",
            "error": "</script><img src=x onerror=alert(1)>",
        },
        locked=False,
    )
    # The raw closing tag never appears; the escaped form does.
    assert "</script><img" not in html_out
    assert "\\u003c/script" in html_out
