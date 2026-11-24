"""Self-serve /subscribe, /billing, /trial handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from olano.config.agent_config import AgentConfig
from olano.monetization import service
from olano.monetization.commands import handle_monetization_command
from olano.monetization.providers.base import SubscriptionOffer
from olano.security.request_context import push_request_context


def _cfg() -> AgentConfig:
    return AgentConfig(
        name="sicop",
        monetization={
            "enabled": True,
            "providers": ["stripe"],
            "plan": {"stripe_price_id": "price_1", "offer_copy": "Join!"},
        },
    )


class FakeChannel:
    def __init__(self) -> None:
        self.offers: list = []

    async def send_subscription_offer(self, recipient: str, offer) -> bool:
        self.offers.append((recipient, offer))
        return True


async def test_non_command_returns_none() -> None:
    out = await handle_monetization_command(
        "hello there",
        agent="sicop",
        channel_name="telegram",
        channel=FakeChannel(),
        recipient="42",
        config=_cfg(),
        is_owner=False,
    )
    assert out is None


async def test_disabled_agent_returns_none() -> None:
    cfg = AgentConfig(name="x")  # monetization disabled by default
    out = await handle_monetization_command(
        "/subscribe",
        agent="x",
        channel_name="telegram",
        channel=FakeChannel(),
        recipient="1",
        config=cfg,
        is_owner=False,
    )
    assert out is None


async def test_owner_is_never_charged() -> None:
    out = await handle_monetization_command(
        "/subscribe",
        agent="sicop",
        channel_name="telegram",
        channel=FakeChannel(),
        recipient="1",
        config=_cfg(),
        is_owner=True,
    )
    assert "owner" in out.lower()


async def test_subscribe_presents_offer_through_channel() -> None:
    channel = FakeChannel()
    with push_request_context(channel="telegram", user_id="42", is_owner=False):
        with patch.object(
            service,
            "build_subscription_offer",
            new=AsyncMock(return_value=SubscriptionOffer(text="Join!", stripe_url="https://pay/x")),
        ):
            out = await handle_monetization_command(
                "/subscribe",
                agent="sicop",
                channel_name="telegram",
                channel=channel,
                recipient="42",
                config=_cfg(),
                is_owner=False,
            )
    assert channel.offers and channel.offers[0][0] == "42"
    assert "above" in out.lower()


async def test_billing_reports_trial_status() -> None:
    with push_request_context(channel="telegram", user_id="77", is_owner=False):
        out = await handle_monetization_command(
            "/billing",
            agent="sicop",
            channel_name="telegram",
            channel=FakeChannel(),
            recipient="77",
            config=_cfg(),
            is_owner=False,
        )
    assert "trial" in out.lower()
