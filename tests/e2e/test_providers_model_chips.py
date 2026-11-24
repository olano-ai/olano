"""Providers view: a model chip reads green only when the provider is ready.

The card already reported readiness in its badges — *installed*, *env set*,
*connected* — but the model chips below them were painted one flat neutral,
so a provider whose key sits in the vault looked exactly like one with no
credential at all. The chips now carry the same answer.

Every branch of ``views/providers/types.ts:providerReady`` is driven here off a
stubbed ``/api/catalog/providers``, because each one is a different way to be
authenticated and only the browser can say which colour won:

* key present (env **or** vault — the backend folds both into ``env_set``) → green
* OAuth / deployment ``auth.connected`` with no env var at all → green
* ``auth.status == "degraded"`` — attached but not currently working → not green
* key missing, or the package not installed → not green

and a deprecated model stays neutral on a ready provider, so the strikethrough
is never contradicted by a colour that says "use this".
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.e2e

CHIP = ".model-chip"


def _provider(key: str, name: str, models: list[str], **over: object) -> dict:
    base = {
        "key": key,
        "name": name,
        "package": f"langchain-{key}",
        "env_var": f"{key.upper()}_API_KEY",
        "available": True,
        "env_set": False,
        "models": models,
        "model_meta": {},
        "notes": "",
        "auth": None,
    }
    base.update(over)
    return base


CATALOG = {
    "providers": [
        # Key present (process env or vault — `env_set` is the backend's fold of
        # both) and the package installed: everything this provider needs.
        _provider("deepseek", "DeepSeek", ["deepseek-v4-flash", "deepseek-chat"], env_set=True),
        # OAuth: no env var at all, authentication lives in `auth.connected`.
        _provider(
            "openai-codex",
            "OpenAI Codex (ChatGPT OAuth)",
            ["gpt-5.5"],
            env_var=None,
            auth={"type": "device_code", "connected": True, "status": "connected", "error": None},
        ),
        # Attached to the control plane but holding no working credential.
        _provider(
            "olano",
            "Olano",
            ["turbo"],
            env_var=None,
            auth={
                "type": "deployment_token",
                "connected": True,
                "status": "degraded",
                "error": "no gateway key yet",
            },
        ),
        # Installed, no key.
        _provider("mistral", "Mistral AI", ["mistral-large-latest"]),
        # Key set but the package was never installed.
        _provider("cohere", "Cohere", ["command-r"], available=False, env_set=True),
    ],
    "byok_locked": False,
    "fields": "full",
}
# The chip for a deprecated model must stay neutral even on a ready provider.
CATALOG["providers"][0]["model_meta"] = {"deepseek-chat": {"deprecated": True}}


def _open_providers(page, base_url: str) -> None:
    page.route(
        "**/api/catalog/providers*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(CATALOG)
        ),
    )
    page.goto(base_url.rstrip("/") + "/providers", wait_until="domcontentloaded")
    page.locator(CHIP).first.wait_for(state="visible", timeout=15_000)


def _is_ready(page, model: str) -> bool:
    chip = page.locator(CHIP, has_text=model).first
    chip.wait_for(state="attached", timeout=10_000)
    return "ready" in (chip.get_attribute("class") or "").split()


def test_model_chips_are_green_exactly_when_the_provider_is_ready(page, dashboard) -> None:
    _open_providers(page, dashboard.base_url)

    # Authenticated, one way or the other.
    assert _is_ready(page, "deepseek-v4-flash")
    assert _is_ready(page, "gpt-5.5")

    # Not usable right now — each for a different reason.
    assert not _is_ready(page, "turbo"), "a degraded provider must not read as ready"
    assert not _is_ready(page, "mistral-large-latest"), "no key set"
    assert not _is_ready(page, "command-r"), "package not installed"

    # Deprecated: ready provider, neutral chip.
    assert not _is_ready(page, "deepseek-chat")


def test_ready_chip_paints_the_success_colour(page, dashboard) -> None:
    """The class has to actually reach a green, not just land in the DOM.

    The base colours moved out of the chip's inline style for this: an inline
    `background` outranks any stylesheet rule, so a `.ready` tint written in CSS
    while the element still carried `background: var(--surface-2)` inline would
    have applied to the text and nothing else.
    """
    _open_providers(page, dashboard.base_url)

    ready = page.locator(f"{CHIP}.ready", has_text="deepseek-v4-flash").first
    plain = page.locator(CHIP, has_text="mistral-large-latest").first
    read = "el => { const s = getComputedStyle(el); return [s.color, s.backgroundColor]; }"
    ready_color, ready_bg = ready.evaluate(read)
    plain_color, plain_bg = plain.evaluate(read)

    assert ready_color != plain_color, f"ready chip kept the neutral text colour {ready_color}"
    assert ready_bg != plain_bg, f"ready chip kept the neutral background {ready_bg}"


# ── the Olano credits key on a self-hosted box ──────────────────────────────


def _credits_key_status(connected: bool) -> dict:
    return {
        "manageable": True,
        "posture": "open",
        "source": "account" if connected else None,
        "connected": connected,
        "key_prefix": "sk-olano-aaaaaa" if connected else None,
        "rejected": False,
        "cloud_url": "https://cloud.example.test",
        "key_url": "https://cloud.example.test/app/api-keys",
        "billing_url": "https://cloud.example.test/app/billing",
    }


def test_olano_card_takes_an_account_key_on_a_self_hosted_box(page, dashboard) -> None:
    """Self-hosted: the Olano card carries the paste form (auth.manageable), and
    a successful PUT turns it into the connected state with the cloud links.
    The key itself is verified and stored by the backend; here the route is
    stubbed, so the test is about the card's two states and the request it
    makes, never the vault."""
    catalog = json.loads(json.dumps(CATALOG))
    olano = next(p for p in catalog["providers"] if p["key"] == "olano")
    olano["auth"] = {
        "type": "deployment_token",
        "connected": False,
        "status": "not_connected",
        "error": "Olano models need an Olano API key pasted here",
        "source": None,
        "manageable": True,
    }
    state = {"connected": False}
    puts: list[dict] = []

    def key_route(route):
        if route.request.method == "PUT":
            puts.append(route.request.post_data_json)
            state["connected"] = True
            body = {
                **_credits_key_status(True),
                "verified": {
                    "org_name": "Acme",
                    "balance": 500,
                    "lifetime_used": 0,
                    "credits_per_usd": 1000,
                    "services": [],
                    "key": {},
                },
                "live_models_updated": 1,
            }
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        return route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_credits_key_status(state["connected"])),
        )

    page.route("**/api/olano/credits-key", key_route)
    page.route(
        "**/api/catalog/providers*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(catalog)
        ),
    )
    page.goto(dashboard.base_url.rstrip("/") + "/providers", wait_until="domcontentloaded")

    form = page.locator("form[data-testid='olano-credits-key']")
    form.wait_for(state="visible", timeout=15_000)
    assert "Run on Olano credits" in page.locator(".card", has_text="Olano").first.inner_text()
    # Connect stays off until something is typed, and on once a key is pasted.
    assert form.get_by_role("button", name="Connect").is_disabled()
    form.locator("input[name='olano-credits-key']").fill("sk-olano-" + "a" * 40)
    assert form.get_by_role("button", name="Connect").is_enabled()
    form.get_by_role("button", name="Connect").click()

    done = page.locator("[data-testid='olano-credits-key']", has_text="Connected")
    done.wait_for(state="visible", timeout=15_000)
    assert puts == [{"key": "sk-olano-" + "a" * 40}]
    assert "sk-olano-aaaaaa" in done.inner_text()
    assert done.get_by_role("link", name="Buy credits").get_attribute("href") == (
        "https://cloud.example.test/app/billing"
    )
    assert done.get_by_role("button", name="Remove").count() == 1


def test_olano_card_has_no_key_form_on_a_managed_deployment(page, dashboard) -> None:
    """A managed deployment's catalog row carries neither `manageable` nor
    `source` (byte-identical to before), so the card renders exactly as it
    did: badge, error line, chips, and no paste field anywhere."""
    _open_providers(page, dashboard.base_url)
    page.locator(".card", has_text="Olano").first.wait_for(state="visible", timeout=10_000)
    assert page.locator("[data-testid='olano-credits-key']").count() == 0
