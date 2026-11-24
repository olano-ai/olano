"""A self-hosted box joins the central MCP sign-in relay with its Olano API key.

The invariants under test, in the order they matter:

- a managed box is untouched: no enrollment, and ``relay_url()`` answers its
  configured URL without ever consulting the self-hosted path;
- no account key (or an operator's own relay, or an HTTPS public URL) means no
  enrollment and no relay;
- the enrollment request has the agreed shape (path, bearer, body), and the box
  stores its private key and the enrollment record only after the cloud said
  yes, so it never points sign-ins at a relay that does not know its key;
- a failure never raises, leaves the box as it was and backs off;
- the official-connector relay (``/connect``) stays off for a self-hosted
  identity, even if the operator points the MCP relay at the official URL.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import httpx
import pytest

from olano.core import connect_relay, mcp_relay, mcp_relay_selfhost
from olano.core.mcp_relay_enroll import _mint_keypair

ACCOUNT_KEY = "sk-olano-" + "A" * 32
OTHER_KEY = "sk-olano-" + "B" * 32


class _Vault:
    def __init__(self) -> None:
        self.stored: dict[str, str] = {}
        self.writes: list[str] = []

    def set(self, name: str, value: str, **kwargs: object) -> None:
        self.writes.append(name)
        self.stored[name] = value

    def get(self, name: str, agent: str | None = None, *, touch: bool = True) -> str:
        return self.stored.get(name, "")


class _Resp:
    def __init__(self, status: int, payload: Any) -> None:
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self) -> Any:
        return self._payload


@pytest.fixture
def selfhost(monkeypatch: pytest.MonkeyPatch) -> _Vault:
    """An unmanaged box with an Olano API key, no relay configured."""
    for name in (
        "LIONCLAW_MCP_RELAY_URL",
        "LIONCLAW_MCP_RELAY_SECRET",
        "LIONCLAW_MCP_RELAY_PRIVATE_KEY",
        "LIONCLAW_CONNECT_RELAY_URL",
        "LIONCLAW_DEPLOYMENT_TOKEN",
        "OLANO_DEPLOYMENT_TOKEN",
        "LIONCLAW_PUBLIC_URL",
        "OLANO_PUBLIC_URL",
        "LIONCLAW_INSTANCE_ID",
        "OLANO_INSTANCE_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LIONCLAW_CLOUD_API_URL", "https://cloud.example.com")
    monkeypatch.setenv("OLANO_CREDITS_KEY", ACCOUNT_KEY)
    from olano.deployment import gateway_key

    gateway_key.reset_account_key_memo()
    vault = _Vault()
    monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: vault)
    mcp_relay_selfhost._reset_for_tests()
    yield vault
    mcp_relay_selfhost._reset_for_tests()


def _capture(monkeypatch: pytest.MonkeyPatch, *responses: Any) -> list[dict[str, Any]]:
    """Patch httpx.post: each call pops the next response (or raises it)."""
    sent: list[dict[str, Any]] = []
    queue = list(responses)

    def fake_post(url: str, *, json: Any, headers: dict[str, str], timeout: float) -> _Resp:
        sent.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        nxt = (
            queue.pop(0)
            if queue
            else _Resp(200, {"ok": True, "relay_url": "https://auth.olano.ai"})
        )
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    monkeypatch.setattr(httpx, "post", fake_post)
    return sent


def _ok(**extra: Any) -> _Resp:
    return _Resp(
        200,
        {
            "ok": True,
            "outcome": "enrolled",
            "scope": "self_hosted",
            "relay_url": "https://auth.olano.ai",
            **extra,
        },
    )


# ── The happy path ───────────────────────────────────────────────────────────


def test_enrollment_request_shape_and_resulting_relay_config(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _capture(monkeypatch, _ok())
    assert mcp_relay.resolve_relay_config() is None  # nothing before enrollment

    assert mcp_relay_selfhost.maybe_enroll() is True

    (call,) = sent
    assert call["url"] == "https://cloud.example.com/api/v1/account/relay-key"
    assert call["headers"] == {"Authorization": f"Bearer {ACCOUNT_KEY}"}
    assert set(call["json"]) == {"public_key", "instance_id"}
    assert len(base64.b64decode(call["json"]["public_key"])) == 32
    assert call["json"]["instance_id"] == mcp_relay.instance_id()

    # The private half went to the vault, and it matches what was registered.
    private = selfhost.stored[mcp_relay._RELAY_PRIVATE_KEY_ENV]
    assert mcp_relay.public_key_b64(private) == call["json"]["public_key"]
    # Private key before record: the record is the switch that turns the relay on.
    assert selfhost.writes == [
        mcp_relay._RELAY_PRIVATE_KEY_ENV,
        mcp_relay_selfhost.ENROLLMENT_VAULT_NAME,
    ]

    cfg = mcp_relay.resolve_relay_config()
    assert cfg is not None
    assert cfg.url == "https://auth.olano.ai"
    assert cfg.callback_uri == "https://auth.olano.ai/mcp/callback"
    assert cfg.signs_ed25519 and cfg.private_key == private
    assert cfg.instance_id == call["json"]["instance_id"]
    # And the MCP OAuth redirect resolver picks the relay.
    from olano.core import mcp_setup

    redirect, relay_cfg = mcp_setup._resolve_redirect_uri({}, None)
    assert redirect == "https://auth.olano.ai/mcp/callback" and relay_cfg is not None


def test_an_existing_private_key_is_reused_not_replaced(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    private, public = _mint_keypair()
    selfhost.stored[mcp_relay._RELAY_PRIVATE_KEY_ENV] = private
    sent = _capture(monkeypatch, _ok())
    assert mcp_relay_selfhost.maybe_enroll() is True
    assert sent[0]["json"]["public_key"] == public
    assert selfhost.writes == [mcp_relay_selfhost.ENROLLMENT_VAULT_NAME]


def test_enrolled_box_does_not_re_enroll_until_the_daily_refresh(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _capture(monkeypatch, _ok(), _ok(outcome="rotated"))
    assert mcp_relay_selfhost.maybe_enroll()
    assert not mcp_relay_selfhost.enrollment_due()
    assert mcp_relay_selfhost.maybe_enroll() is False
    assert len(sent) == 1

    record = mcp_relay_selfhost.stored_enrollment()
    assert record is not None
    stale = mcp_relay_selfhost.Enrollment(
        instance_id=record.instance_id,
        public_key=record.public_key,
        relay_url=record.relay_url,
        account_key_digest=record.account_key_digest,
        enrolled_at=time.time() - mcp_relay_selfhost.REENROLL_AFTER_SECONDS - 1,
    )
    selfhost.stored[mcp_relay_selfhost.ENROLLMENT_VAULT_NAME] = stale.to_json()
    # Still usable while the refresh is pending.
    assert mcp_relay.relay_url() == "https://auth.olano.ai"
    assert mcp_relay_selfhost.enrollment_due()
    assert mcp_relay_selfhost.maybe_enroll()
    assert len(sent) == 2 and sent[1]["json"]["public_key"] == record.public_key


def test_a_different_account_key_needs_a_fresh_enrollment(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture(monkeypatch, _ok())
    assert mcp_relay_selfhost.maybe_enroll()
    monkeypatch.setenv("OLANO_CREDITS_KEY", OTHER_KEY)
    assert mcp_relay.relay_url() is None
    assert mcp_relay_selfhost.enrollment_due()


def test_instance_id_owned_elsewhere_mints_a_fresh_one_once(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = mcp_relay.instance_id()
    sent = _capture(monkeypatch, _Resp(409, {"error": "instance_not_owned"}), _ok())
    assert mcp_relay_selfhost.maybe_enroll() is True
    assert sent[0]["json"]["instance_id"] == before
    fresh = sent[1]["json"]["instance_id"]
    assert fresh != before and mcp_relay.instance_id() == fresh
    assert mcp_relay.resolve_relay_config().instance_id == fresh  # type: ignore[union-attr]


def test_a_pinned_instance_id_is_never_rotated(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIONCLAW_INSTANCE_ID", "pinned-box-id")
    sent = _capture(monkeypatch, _Resp(409, {"error": "instance_not_owned"}))
    assert mcp_relay_selfhost.maybe_enroll() is False
    assert len(sent) == 1
    assert mcp_relay_selfhost.ENROLLMENT_VAULT_NAME not in selfhost.stored


# ── Who must not enroll ──────────────────────────────────────────────────────


def test_no_account_key_means_no_enrollment_and_no_relay(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OLANO_CREDITS_KEY", raising=False)
    monkeypatch.delenv("LIONCLAW_CREDITS_KEY", raising=False)
    from olano.deployment import gateway_key

    gateway_key.reset_account_key_memo()
    sent = _capture(monkeypatch)
    assert not mcp_relay_selfhost.enrollment_due()
    assert mcp_relay_selfhost.maybe_enroll(force=True) is False
    assert sent == []
    assert mcp_relay.relay_url() is None
    assert mcp_relay.resolve_relay_config() is None


def test_managed_box_never_enrolls_and_keeps_its_configured_relay(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stray credits key in a managed box's env must change nothing.
    monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "olano_dep_tok")
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.olano.ai")
    sent = _capture(monkeypatch)
    assert mcp_relay_selfhost.eligible_account_key() is None
    assert mcp_relay_selfhost.maybe_enroll(force=True) is False
    assert sent == []


def test_configured_relay_url_short_circuits_the_selfhost_path(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Managed behaviour is byte-identical: with a configured URL the
    self-hosted resolver is never even asked."""
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.example.com/")

    def boom() -> None:
        raise AssertionError("self-hosted path consulted on a configured box")

    monkeypatch.setattr(mcp_relay_selfhost, "enrolled_relay_url", boom)
    assert mcp_relay.relay_url() == "https://auth.example.com"


def test_an_operators_own_relay_disables_selfhost_enrollment(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://relay.my-home-lab.example")
    sent = _capture(monkeypatch)
    assert mcp_relay_selfhost.maybe_enroll(force=True) is False
    assert sent == []


def test_https_public_url_keeps_the_boxs_own_callback(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture(monkeypatch, _ok())
    assert mcp_relay_selfhost.maybe_enroll()
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://box.example.com")
    assert mcp_relay.relay_url() is None
    from olano.core import mcp_setup

    redirect, relay_cfg = mcp_setup._resolve_redirect_uri({}, None)
    assert redirect == "https://box.example.com/api/mcp/oauth/callback" and relay_cfg is None
    # And a box that has one from the start never spends an enrollment slot.
    mcp_relay_selfhost._reset_for_tests()
    sent = _capture(monkeypatch)
    assert mcp_relay_selfhost.maybe_enroll(force=True) is False and sent == []


def test_plain_http_public_url_still_uses_the_relay(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "http://203.0.113.7:3000")
    _capture(monkeypatch, _ok())
    assert mcp_relay_selfhost.maybe_enroll()
    assert mcp_relay.relay_url() == "https://auth.olano.ai"


# ── Failure never breaks anything ────────────────────────────────────────────


def test_unreachable_cloud_leaves_the_box_as_it_was_and_backs_off(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="olano.core.mcp_relay_selfhost")
    sent = _capture(monkeypatch, httpx.ConnectError("no route"))
    assert mcp_relay_selfhost.maybe_enroll() is False
    assert selfhost.stored == {}  # no private key, no record
    assert mcp_relay.relay_url() is None
    # Backed off: the next tick does not hit the network again.
    assert not mcp_relay_selfhost.enrollment_due()
    assert mcp_relay_selfhost.maybe_enroll() is False
    assert len(sent) == 1
    (line,) = [r.getMessage() for r in caplog.records]
    assert "could not reach cloud.olano.ai" in line and "LIONCLAW_PUBLIC_URL" in line
    assert ACCOUNT_KEY not in line
    assert mcp_relay_selfhost.status()["last_error"]


def test_revoked_key_is_named_in_one_actionable_line(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="olano.core.mcp_relay_selfhost")
    _capture(
        monkeypatch, _Resp(401, {"error": "unauthorized"}), _Resp(401, {"error": "unauthorized"})
    )
    assert mcp_relay_selfhost.maybe_enroll() is False
    assert mcp_relay_selfhost.maybe_enroll(force=True) is False
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1  # the same failure is not repeated
    assert "refused this box's Olano API key" in lines[0]
    assert mcp_relay._RELAY_PRIVATE_KEY_ENV not in selfhost.stored


def test_instance_limit_is_explained(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="olano.core.mcp_relay_selfhost")
    _capture(monkeypatch, _Resp(409, {"error": "instance_limit_reached", "max_instances": 5}))
    assert mcp_relay_selfhost.maybe_enroll() is False
    assert "maximum number of self-hosted boxes" in caplog.records[0].getMessage()


def test_locked_vault_means_no_registration(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: None)
    sent = _capture(monkeypatch)
    assert mcp_relay_selfhost.maybe_enroll() is False
    assert sent == []


def test_unexpected_errors_are_swallowed(selfhost: _Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(**_: object) -> bool:
        raise RuntimeError("surprise")

    monkeypatch.setattr(mcp_relay_selfhost, "_enroll_locked", explode)
    assert mcp_relay_selfhost.maybe_enroll() is False


async def test_runtime_loop_survives_failures_and_sleeps_on_the_loop(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olano.runtime.server import RuntimeServer

    calls: list[str] = []

    def due() -> bool:
        calls.append("due")
        raise RuntimeError("cloud gone")

    monkeypatch.setattr(mcp_relay_selfhost, "enrollment_due", due)
    task = asyncio.create_task(RuntimeServer._selfhost_relay_loop(object()))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    assert calls == ["due"] and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_pasting_a_key_wakes_the_loop(selfhost: _Vault) -> None:
    from olano.runtime.routes import olano_credits

    assert mcp_relay_selfhost.take_request() is False
    olano_credits._invalidate_credit_caches()
    assert mcp_relay_selfhost.take_request() is True
    assert mcp_relay_selfhost.take_request() is False


# ── Official connectors stay managed-only ────────────────────────────────────


def test_connect_relay_stays_off_for_a_selfhost_box(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture(monkeypatch, _ok())
    assert mcp_relay_selfhost.maybe_enroll()
    assert mcp_relay.resolve_relay_config() is not None  # MCP: yes
    assert connect_relay.connect_relay_url() is None  # /connect: no URL at all
    assert connect_relay.resolve_connect_relay_config() is None
    # The broker's official floor asks the same resolver, so no official
    # client is offered even when the catalog publishes one.
    from olano.core.connectors import base as broker

    monkeypatch.setattr(
        "olano.catalog.connectors_registry.official_client", lambda _pid: "official-cid"
    )
    provider = next(
        (p for p in _providers() if broker.relay_capable(p)),
        None,
    )
    assert provider is not None
    assert broker._official_client(provider) is None


def _providers() -> list[Any]:
    from olano.core.connectors import get_provider, provider_ids

    return [get_provider(pid) for pid in provider_ids()]


def test_connect_relay_off_even_when_the_official_url_is_configured(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator who points the MCP relay at auth.olano.ai by hand still
    enrolls over the account key, and still gets no /connect."""
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.olano.ai")
    _capture(monkeypatch, _ok())
    assert mcp_relay_selfhost.maybe_enroll()
    assert mcp_relay.resolve_relay_config() is not None
    assert connect_relay.connect_relay_url() == "https://auth.olano.ai"
    assert mcp_relay_selfhost.holds_selfhost_identity()
    assert connect_relay.resolve_connect_relay_config() is None


def test_managed_identity_still_resolves_the_connect_relay(
    selfhost: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    private, _ = _mint_keypair()
    monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "olano_dep_tok")
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.olano.ai")
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_PRIVATE_KEY", private)
    cfg = connect_relay.resolve_connect_relay_config()
    assert cfg is not None and cfg.private_key == private
