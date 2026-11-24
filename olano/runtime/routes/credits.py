"""Credits display, balance and managed service usage.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

from typing import Any

from fastapi import FastAPI

from olano.runtime import api as _api


def register_credits_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the credits routes to ``app``."""

    def _credits_account_available() -> bool:
        """Whether this deployment spends olano credits at all.

        Managed posture + a deployment token is the same signal the managed
        service clients use: it means there is a cloud account behind this box
        that a credit charge can land on. A self-hosted install has one only
        when its owner pasted an Olano account API key (Providers → Olano);
        without it every credits number the dashboard could draw would be a
        constant zero.
        """
        try:
            from olano.deployment import resolver as dep_resolver
            from olano.deployment.sources import deployment_token

            if dep_resolver.deployment_status().posture != "open":
                return bool(deployment_token())
            # A self-hosted box spends from the account wallet behind its
            # pasted Olano API key; the balance and usage proxies below read
            # that wallet through the key-authenticated twins of the routes.
            from olano.deployment.gateway_key import account_key_if_unmanaged

            return bool(account_key_if_unmanaged())
        except Exception:  # pragma: no cover - defensive
            return False

    @app.get("/api/credits")
    async def get_credits_display() -> dict:
        """How this deployment renders cost: the display unit + the conversion
        rate to apply, plus **which books it keeps at all**.

        Read by *every* dashboard surface that shows money (Insights, the
        drilldowns, the chat header, the Overview teaser), so it is a plain
        authenticated read with no admin gate — the admin gate is on *changing*
        the mode (`PUT /api/system/config`). The runtime keeps accounting in
        USD; the client multiplies by ``credits_per_usd`` when
        ``display == "credits"``.

        ``credits_enabled`` / ``byok_enabled`` describe the deployment's plan,
        and the dashboard renders one section per enabled book:

        - manual install (pure BYOK) → BYOK only, no credits anywhere
        - managed entry plans (credits only) → credits only, no USD/API-cost
        - Ultra and above (credits + BYOK) → both, shown side by side
        """
        from olano.catalog import credits_registry
        from olano.config.system_settings import cost_display_locked, cost_display_mode

        rate = credits_registry.get_credit_rate()
        return {
            "display": cost_display_mode(),
            # Which of the two accounting books this deployment actually uses.
            # Computed off the event loop is unnecessary — both are cached
            # in-process reads of the resolved deployment settings.
            "credits_enabled": _credits_account_available(),
            "byok_enabled": _api._byok_allowed(),
            # True on managed boxes: the unit comes from the signed deployment
            # settings and no local surface may change it.
            "display_locked": cost_display_locked(),
            "credits_per_usd": rate.get("creditsPerUsd"),
            "unit_name": rate.get("unitName"),
            "unit_name_plural": rate.get("unitNamePlural"),
            "decimals": rate.get("decimals"),
            # Where the rate came from (cloud/disk/bundled) + its catalog
            # revision, so the Config screen can say "synced" vs "using the
            # bundled launch rate" instead of implying the cloud confirmed it.
            "source": rate.get("source"),
            "revision": rate.get("revision"),
            "updated_at": rate.get("updatedAt"),
        }

    @app.get("/api/olano/credits")
    async def get_olano_credit_balance(force: bool = False) -> dict:
        """The deployment's olano-credit balance, proxied from cloud.olano.ai.

        The cloud ledger is the account; this runtime never computes a balance
        locally (its per-turn charges are display estimates). Cached ~60s with
        stale-on-error (`stale: true` + a growing `age_seconds`), so a cloud
        blip degrades freshness, never the card. `available: false` on
        unmanaged installs — there is no account to read and the dashboard
        hides the card.
        """
        import asyncio as _asyncio

        from olano.core.actionable_errors import deployment_card_url
        from olano.deployment.credits_client import get_balance

        balance = await _asyncio.to_thread(get_balance, force)
        if balance is None:
            return {"available": False}
        # Where to buy more — this deployment's card on cloud.olano.ai.
        return {"available": True, "manage_url": deployment_card_url(), **balance}

    @app.get("/api/olano/services/usage")
    async def get_olano_service_usage(force: bool = False) -> dict:
        """Per-service olano-credit usage, proxied from cloud.olano.ai.

        CREDITS ONLY: each managed service (chat tiers, olano-vision,
        olano-image, olano-tts, olano-stt, olano-web-search) with the credits it
        consumed, plus a total — sourced from the cloud gateway_calls fact table,
        so these numbers match the cloud deployment view exactly. Never exposes a
        backend model, token count, unit, or COGS. Cached ~60s, stale-on-error;
        `available: false` on unmanaged installs (the dashboard hides the card).
        """
        import asyncio as _asyncio

        from olano.deployment.service_usage_client import get_usage

        usage = await _asyncio.to_thread(get_usage, force)
        if usage is None:
            return {"available": False}
        return {"available": True, **usage}
