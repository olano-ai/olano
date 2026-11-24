"""The Olano account API key on a self-hosted box (``/api/olano/credits-key``).

A person mints an API key on cloud.olano.ai (API keys), pastes it here, and the
box spends that account's credits exactly as a managed deployment spends its
own: the ``olano:*`` tiers, image/vision/speech and the web services all ride
the same gateway with it (``deployment/gateway_key.py``). Three rules shape the
surface:

- **Open posture only.** A managed deployment enrolls its own key and bills its
  own wallet; a second key would be a second wallet behind one box, so the
  route answers ``409 managed_box`` there. The posture is never changed by
  pasting a key (``security``/``enforcement`` keep reading it).
- **Verified before stored.** ``GET /api/v1/account/verify`` with the pasted key
  decides whether it is live; a refused or unreachable key is never written.
- **Admin only**, by the exact prefix ``/api/olano/credits-key`` in
  ``runtime/auth._ADMIN_ONLY_PREFIXES``: the member-readable
  ``/api/olano/credits`` and ``/api/olano/services/usage`` share the segment.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import FastAPI, HTTPException

logger = logging.getLogger(__name__)

VERIFY_PATH = "/api/v1/account/verify"
VERIFY_TIMEOUT_SECONDS = 10.0
# What a key may look like before any network call: the wire prefix every
# account key carries (LiteLLM's ``sk-`` requirement, then ours), then the
# base64url body the cloud mints (32 chars today; 20+ keeps a future longer
# body valid). Anything else is a paste error worth naming without a round trip.
KEY_SHAPE = re.compile(r"^sk-olano-[A-Za-z0-9_-]{20,200}$")
# The verify payload's contract (apps/web/app/api/v1/account/verify): a 200
# that does not carry these is not the route we expect (a proxy page, an
# older cloud) and must not be mistaken for a live key.
VERIFY_SCHEMA_VERSION = 1


def _posture() -> str:
    from olano.deployment import resolver as dep_resolver

    try:
        return str(dep_resolver.deployment_status().posture)
    except Exception:  # pragma: no cover - defensive
        return "quarantine"


def _refuse_unless_open() -> None:
    posture = _posture()
    if posture != "open":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "managed_box",
                "message": (
                    "This deployment is managed by cloud.olano.ai and already bills its "
                    "own credits; an account API key applies to self-hosted installs only."
                ),
                "posture": posture,
            },
        )


def verify_account_key(key: str) -> dict[str, Any]:
    """Ask the cloud whether *key* is live. Returns the verify payload.

    Raises ``HTTPException``: 400 ``key_rejected`` for a 401, 502
    ``cloud_unreachable`` for anything that is not an answer.
    """
    import httpx

    from olano.deployment.sources import cloud_api_url

    try:
        resp = httpx.get(
            f"{cloud_api_url()}{VERIFY_PATH}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=VERIFY_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "cloud_unreachable",
                "message": (
                    "Could not reach cloud.olano.ai to check the key. Check this box's "
                    "outbound network and try again."
                ),
                "error": str(exc),
            },
        ) from exc
    if resp.status_code in (401, 403):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "key_rejected",
                "message": (
                    "cloud.olano.ai does not recognise this key. It may have been revoked "
                    "or pasted incompletely; create a new one under API keys and paste it "
                    "whole."
                ),
            },
        )
    if resp.status_code == 404:
        # The route is not there: this cloud does not offer account API keys
        # (an older release, or a private cloud without the feature).
        raise HTTPException(
            status_code=502,
            detail={
                "code": "account_credits_unavailable",
                "message": (
                    "cloud.olano.ai does not offer account API keys to this box (the "
                    "verify route answered 404). Check LIONCLAW_CLOUD_API_URL, or use a "
                    "provider key instead."
                ),
            },
        )
    if resp.status_code == 429:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "cloud_busy",
                "message": "cloud.olano.ai is rate-limiting key checks right now; try again in a minute.",
            },
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "cloud_error",
                "message": f"cloud.olano.ai answered {resp.status_code} while checking the key.",
            },
        )
    try:
        data = resp.json()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "cloud_error",
                "message": "cloud.olano.ai returned an unreadable answer.",
            },
        ) from exc
    # `org_name` is null when the org never named itself; the KEY block is
    # always there, so that is the field that proves this is a verification.
    if (
        not isinstance(data, dict)
        or data.get("schemaVersion") != VERIFY_SCHEMA_VERSION
        or not isinstance(data.get("key"), dict)
    ):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "cloud_error",
                "message": (
                    "cloud.olano.ai answered, but not with a key verification (a proxy "
                    "page or an older cloud?). Check LIONCLAW_CLOUD_API_URL."
                ),
            },
        )
    return data


def _invalidate_credit_caches() -> None:
    """Every reader that memoised "this box has no credits" re-reads."""
    from olano.deployment import credits_client, service_usage_client

    credits_client.invalidate_cache()
    service_usage_client.invalidate_cache()
    # A pasted key is also this box's ticket to the MCP sign-in relay; enroll
    # now instead of at the next minute's tick.
    try:
        from olano.core import mcp_relay_selfhost

        mcp_relay_selfhost.request_enrollment()
    except Exception:  # pragma: no cover - never fail a key save over this
        pass


def _status() -> dict[str, Any]:
    from olano.deployment import gateway_key as gk
    from olano.deployment.sources import cloud_api_url

    posture = _posture()
    key = gk.account_key_if_unmanaged() if posture == "open" else None
    source = gk.gateway_key_source()
    return {
        "manageable": posture == "open",
        "posture": posture,
        # Which family actually serves the gateway right now (``env`` when an
        # operator's OLANO_GATEWAY_KEY outranks the pasted key, ``deployment``
        # when an enrolled key does).
        "source": source,
        "connected": bool(key),
        # A stored key is not always the one in use: ``active`` says the
        # gateway serves THIS key; ``shadowed_by`` names what outranks it.
        "active": bool(key) and source == "account",
        "shadowed_by": source if key and source in ("env", "deployment") else None,
        # Set in the environment (OLANO_CREDITS_KEY), so the dashboard cannot
        # replace or remove it; the vault copy is what the route manages.
        "from_env": bool(gk._env_value(gk.ACCOUNT_KEY_ENV)[1]),
        "key_prefix": (key[: len(gk.ACCOUNT_KEY_PREFIX) + 6] if key else None),
        "rejected": gk.account_key_rejected() if key else False,
        "cloud_url": cloud_api_url(),
        "key_url": f"{cloud_api_url()}/app/api-keys",
        "billing_url": f"{cloud_api_url()}/app/billing",
    }


def _model_of_running(runtime: Any, name: str) -> str:
    runner = (getattr(runtime, "_runners", None) or {}).get(name)
    return str(getattr(getattr(runner, "config", None), "model", "") or "")


def _model_on_disk(name: str) -> str:
    try:
        from olano.config.loader import load_agent_config

        return str(load_agent_config(name).model or "")
    except Exception:  # pragma: no cover - an unloadable agent is not ours to restart
        return ""


def _is_tier(model: str) -> bool:
    """An explicit Olano tier (``olano:*``), or an alias resolving to one."""
    if model.startswith("olano:"):
        return True
    try:
        from olano.models.aliases import resolve_model_alias

        return str(resolve_model_alias(model)).startswith("olano:")
    except Exception:  # pragma: no cover - an alias that cannot resolve is not ours
        return False


def _agents_to_rebuild(runtime: Any, *, key_arrived: bool) -> list[str]:
    """Which agents a key change rebuilds.

    ``default:*`` agents always: the alias resolves to a tier while a key is
    connected and to the operator's BYOK default otherwise, so they change
    model either way. Explicit ``olano:*`` agents only when a key ARRIVES: they
    cannot boot without one (the builder refuses an unattached box, by design),
    so a removal leaves them running on the placeholder, where every turn is a
    clear ``olano_key_missing`` error, rather than stopping them into a 404.
    On arrival the ones that failed to boot earlier (absent from the runners)
    are booted too, from their config on disk.
    """
    names: list[str] = []
    running = list((getattr(runtime, "_runners", None) or {}).keys())
    for name in running:
        model = _model_of_running(runtime, name)
        if model.startswith("default:") or (key_arrived and _is_tier(model)):
            names.append(name)
    if key_arrived:
        try:
            from olano.config.loader import list_agents

            for name in list_agents():
                if name not in running and _is_tier(_model_on_disk(name)):
                    names.append(name)
        except Exception:  # pragma: no cover - the disk listing is best-effort
            pass
    return names


async def _rebuild_agents(runtime: Any, *, key_arrived: bool) -> list[str]:
    """Stop (when running) and boot the agents :func:`_agents_to_rebuild` names,
    the way ``POST /api/system/restart`` does, so a connected key reaches them
    at once and a removed one stops being used."""
    rebuilt: list[str] = []
    running = getattr(runtime, "_runners", None) or {}
    for name in _agents_to_rebuild(runtime, key_arrived=key_arrived):
        try:
            if name in running:
                await runtime._stop_agent(name)
            await runtime._boot_agent(name)
            rebuilt.append(name)
        except Exception:  # pragma: no cover - one agent must not stop the rest
            logger.warning(
                "olano credits: could not rebuild agent %r after the key change",
                name,
                exc_info=True,
            )
    return rebuilt


def register_olano_credits_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the account-key routes to ``app``."""

    @app.get("/api/olano/credits-key")
    async def olano_credits_key_status() -> dict:
        """Whether this box can take an account key, and whether it holds one.
        Never the key itself: ``key_prefix`` is all a card needs."""
        return await asyncio.to_thread(_status)

    @app.put("/api/olano/credits-key")
    async def olano_credits_key_set(body: dict) -> dict:
        """Verify a pasted key against cloud.olano.ai, store it in the vault and
        hand it to every live gateway model. The verify payload rides back so
        the dashboard can show the account and its balance at once."""
        from olano.deployment import gateway_key as gk

        _refuse_unless_open()
        key = str(body.get("key") or "").strip()
        if not KEY_SHAPE.match(key):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_key",
                    "message": (
                        f"An Olano API key starts with {gk.ACCOUNT_KEY_PREFIX!r}. Copy the whole "
                        "key from cloud.olano.ai (API keys); it is shown once, when created."
                    ),
                },
            )
        verified = await asyncio.to_thread(verify_account_key, key)
        # Whether any gateway key served the box before this paste decides
        # whether the default:* agents have to be rebuilt onto the tiers.
        had_gateway_key = await asyncio.to_thread(lambda: gk.gateway_key() is not None)
        try:
            await asyncio.to_thread(gk.store_account_key, key)
        except RuntimeError as exc:
            if str(exc) == "vault_locked":
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "vault_locked",
                        "message": "The vault is locked, so the key cannot be stored. Unlock it and try again.",
                    },
                ) from exc
            raise

        def _apply() -> int:
            from olano.models.olano_gateway import swap_live_models

            _invalidate_credit_caches()
            # The EFFECTIVE key, not the pasted one: an operator's
            # OLANO_GATEWAY_KEY or an enrolled key outranks it in gateway_key(),
            # and pushing the pasted key into live models would make them use a
            # key the box's own resolution says is not in charge.
            return swap_live_models(gk.gateway_key() or gk.UNENROLLED_SENTINEL)

        swapped = await asyncio.to_thread(_apply)
        restarted = [] if had_gateway_key else await _rebuild_agents(runtime, key_arrived=True)
        logger.info(
            "olano credits: account API key connected (%s…), %d live model(s) updated, %d agent(s) rebuilt",
            key[: len(gk.ACCOUNT_KEY_PREFIX) + 4],
            swapped,
            len(restarted),
        )
        status = await asyncio.to_thread(_status)
        return {
            **status,
            "verified": {
                "org_name": verified.get("org_name"),
                "balance": verified.get("balance"),
                "lifetime_used": verified.get("lifetime_used"),
                "credits_per_usd": verified.get("credits_per_usd"),
                "services": verified.get("services") or [],
                "key": verified.get("key") or {},
            },
            "live_models_updated": swapped,
            "agents_restarted": restarted,
        }

    @app.delete("/api/olano/credits-key")
    async def olano_credits_key_forget() -> dict:
        """Remove the pasted key. Live gateway models fall back to the
        unenrolled sentinel so the removed key is never used again; BYOK
        defaults return on the next agent build."""
        from olano.deployment import gateway_key as gk

        _refuse_unless_open()
        try:
            removed = await asyncio.to_thread(gk.forget_account_key)
        except RuntimeError as exc:
            if str(exc) == "vault_locked":
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "vault_locked",
                        "message": "The vault is locked, so the key cannot be removed. Unlock it and try again.",
                    },
                ) from exc
            raise

        def _apply() -> None:
            from olano.models.olano_gateway import swap_live_models

            _invalidate_credit_caches()
            # Whatever still serves the gateway (an env or enrolled key), else
            # the placeholder, so the removed key is never used again.
            swap_live_models(gk.gateway_key() or gk.UNENROLLED_SENTINEL)

        await asyncio.to_thread(_apply)
        # Agents on default:* fall back to the operator's BYOK default only
        # when rebuilt; without this they kept a model pointed at the
        # placeholder and 401'd on every turn until a manual restart.
        restarted = await _rebuild_agents(runtime, key_arrived=False) if removed else []
        status = await asyncio.to_thread(_status)
        return {**status, "removed": removed, "agents_restarted": restarted}
