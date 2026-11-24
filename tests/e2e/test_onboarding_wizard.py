"""First-run onboarding wizard E2E: the first admin sign-in on a deployed
(multi-user) instance is gated by the full-screen setup wizard.

Uses its own dashboard instance (module-scoped) because it flips the
throwaway LIONCLAW_HOME into multi-user mode via /api/auth/bootstrap — the
shared session `dashboard` fixture must stay single-user for every other
test. The journey: bootstrap an admin over HTTP → sign in through the real
login form → wizard appears → walk to the profile step and back → skip the
tour → app shell renders → reload → wizard stays gone (server-side flag).

Four panels: welcome → your profile (claim the provisioned account + set the
vault passphrase) → AI provider → meet Agent Father, whose primary button
finishes the tour and lands in Agent Father's chat.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

ADMIN = {"email": "admin@olano.test", "password": "olano-e2e-pw", "name": "Admin"}


@pytest.fixture(autouse=True)
def lionclaw_home():
    """Override the root autouse home-swap (tests/conftest.py).

    The in-process runtime booted by `onboarding_dashboard` owns LIONCLAW_HOME
    for this whole module. The per-test tmp-home swap would point the live
    server at an empty users DB mid-test, turning the bootstrapped admin's
    login into `invalid_credentials` and flipping the gate back to single-user.
    """
    yield


@pytest.fixture(scope="module")
def onboarding_dashboard():
    """A dedicated dashboard whose home gets bootstrapped into multi-user mode."""
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard() as running:
            req = urllib.request.Request(
                running.runtime_url.rstrip("/") + "/api/auth/bootstrap",
                data=json.dumps({**ADMIN, "setup_code": harness.first_run_setup_code()}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as res:
                assert res.status == 200
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


# Rebind `page` so it consumes our multi-user dashboard, not the shared
# single-user session fixture (same pattern as test_context_bar, but
# parametrized over both viewports like the conftest fixture).
from tests.e2e.conftest import VIEWPORTS


@pytest.fixture(params=sorted(VIEWPORTS))
def page(request, onboarding_dashboard):
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(onboarding_dashboard.base_url, wait_until="domcontentloaded")
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def _reset_onboarding(runtime_url: str) -> None:
    """Clear the deployment-wide completion flag between viewport runs.

    The dashboard fixture is module-scoped, so the desktop run's skip would
    otherwise leave the mobile run with no wizard to test. Auth: log in over
    HTTP and replay the session cookie on DELETE /api/onboarding.
    """
    base = runtime_url.rstrip("/")
    login = urllib.request.Request(
        base + "/api/auth/login",
        data=json.dumps({"email": ADMIN["email"], "password": ADMIN["password"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(login, timeout=10) as res:
            cookie = res.headers.get("Set-Cookie", "").split(";", 1)[0]
    except urllib.error.HTTPError as exc:  # pragma: no cover - debug aid
        raise AssertionError(f"login failed: {exc.code} {exc.read().decode()[:200]}") from exc
    assert cookie.startswith("lc_session="), f"no session cookie in login response: {cookie!r}"
    delete = urllib.request.Request(
        base + "/api/onboarding", headers={"Cookie": cookie}, method="DELETE"
    )
    with urllib.request.urlopen(delete, timeout=10) as res:
        assert res.status == 200


def _sign_in(page) -> None:
    email = page.locator("form.auth-card input[type='email']")
    email.wait_for(state="visible", timeout=15_000)
    email.fill(ADMIN["email"])
    page.locator("form.auth-card input[type='password']").fill(ADMIN["password"])
    # Click waits for the button to enable (login is disabled until the
    # server-health probe reports online).
    page.locator("form.auth-card button.btn.primary").click()


CLOUD_DEPLOYMENT_ID = "dd82055d-a641-45e4-8afd-0b5b815764ab"


def _pose_as_managed_cloud(
    page,
    *,
    plan: str = "basic",
    features: dict | None = None,
    credits_left: float | None = None,
) -> None:
    """Make /api/system/deployment answer like a managed olano.ai deployment.

    The runtime under test has no /etc/olano/installation.json, so it really
    resolves to posture "open" with a null mode — which is exactly the
    self-hosted case the negative test below relies on. Stubbing the response
    is what lets us drive the managed branch of the same panels.

    ``features`` defaults to ``{}``, which is *every* service entitled: a key
    the control plane never sent means enabled (deployment/schema.py
    ``DeploymentSettings.feature_enabled``). Pass explicit ``False`` values to
    model a plan that withholds one.
    """
    page.route(
        "**/api/system/deployment",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "posture": "enforced",
                    "source": "cloud",
                    "mode": "cloud",
                    "deployment_id": CLOUD_DEPLOYMENT_ID,
                    "plan": plan,
                    "resources": [],
                    "usage": [],
                    "features": features if features is not None else {},
                    "tabs": {},
                    "credits_left": credits_left,
                }
            ),
        ),
    )


# Every managed AI service a plan can include, as the provider step lists them.
INCLUDED_LABELS = (
    "Intelligence",
    "Image generation",
    "Vision",
    "Web search",
    "Speech to text",
    "Text to speech",
)


def _stub_provider_catalog(page, *, byok_locked: bool, key_entry_locked: bool) -> None:
    """Serve GET /api/catalog/providers with a chosen entitlement posture.

    The two locks are deliberately separate on the wire: ``model_selection_locked``
    is "no key entry here" (the backend folds `model_selection` in with `byok`),
    while ``byok_locked`` is specifically the plan entitlement — the only one an
    upgrade fixes.
    """
    page.route(
        "**/api/catalog/providers*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "providers": [
                        {
                            "key": "anthropic",
                            "name": "Anthropic",
                            "package": "langchain-anthropic",
                            "env_var": "ANTHROPIC_API_KEY",
                            "available": True,
                            "env_set": False,
                            "models": [],
                            "notes": "",
                            "auth": None,
                            **({"locked": True} if key_entry_locked else {}),
                        }
                    ],
                    "model_selection_locked": key_entry_locked,
                    "byok_locked": byok_locked,
                    "fields": "basic",
                }
            ),
        ),
    )


def _start_wizard(page) -> None:
    """Leave the welcome panel and land on the profile step.

    Which half of it renders depends on the box: the module's admin is created
    by /api/auth/bootstrap, so no provisioned temporary password was ever in
    play and the profile step shows only its vault half. A box still holding a
    temporary password shows the account half too, and
    `_pose_as_unrotated_admin` puts a test in that world deliberately."""
    page.locator(".ob-root").wait_for(state="visible", timeout=15_000)
    page.locator(".ob-welcome .btn.primary").click()
    page.locator(".ob-form input[type='password']").first.wait_for(state="visible", timeout=10_000)


def _pose_vault(page, *, unlocked: bool) -> None:
    """Pin the vault's lock state, which decides the vault step's shape.

    An unlocked vault can be rekeyed without its current passphrase (the server
    rekeys from the live store), so the step becomes a plain "set a passphrase"
    form. The runtime under test resolves this on its own; stubbing it is what
    lets one box exercise both shapes.
    """
    page.route(
        "**/api/vault/status",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "unlocked": unlocked,
                    "keyring_available": False,
                    "env_key_set": False,
                    "boot_key_file": unlocked,
                    "has_vault": True,
                }
            ),
        ),
    )


def _pose_as_unrotated_admin(page) -> None:
    """Answer /api/auth/me as a box still holding its provisioned temporary
    admin password — the case where the profile step shows its account half."""

    def handler(route):
        response = route.fetch()
        body = response.json()
        body.setdefault("onboarding", {})["admin_password_rotated"] = False
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/auth/me", handler)


def _pose_agentfather_installed(page) -> None:
    """Serve POST /api/archetypes/install as a managed box does: Agent Father
    was provisioned with the deployment, so the install answers "already
    there" and the panel arrives ready without the harness growing an agent."""
    page.route(
        "**/api/archetypes/install",
        lambda route: route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps({"detail": "agent 'agentfather' already exists"}),
        ),
    )


def _skip_to_next_step(page) -> None:
    # By name, not position: on managed deployments the credential steps render
    # no Skip at all (a positional `.last` would click Back), and while the
    # deployment lookup is in flight Skip is briefly absent everywhere —
    # get_by_role waits for the named button to actually appear.
    page.locator(".ob-actions").get_by_role("button", name="Skip for now").click()


CODEX_SESSION = {
    "session_id": "e2e-codex-session",
    "user_code": "E2E1-CODE",
    "verification_url": "https://example.invalid/deviceauth",
    "interval": 1,  # poll once a second so the test isn't waiting on the real 5s
    "expires_in": 900,
    "error_message": None,
}


def _stub_codex_device_flow(page, starts: list[str]) -> None:
    """Serve the device flow from the browser: /start pends, /poll approves."""

    def on_start(route):
        starts.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({**CODEX_SESSION, "status": "pending"}),
        )

    page.route("**/api/auth/openai-codex/start", on_start)
    page.route(
        "**/api/auth/openai-codex/poll/**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({**CODEX_SESSION, "status": "approved", "models": ["gpt-5-codex"]}),
        ),
    )


def _walk_to_step(page, label: str) -> None:
    """Land on the step whose rail label matches ``label``, with the profile
    step recorded as done.

    That step is non-skippable on managed deployments (its provisioned
    temporary credentials must be rotated), so a skip-click path cannot cross
    it under a managed pose. Seeding the wizard's persisted position
    (views/onboarding/progress.ts) models an admin who already completed it
    and resumes further along — valid at every posture, and one reload instead
    of several click-waits."""
    page.locator(".ob-root").wait_for(state="visible", timeout=15_000)
    # Read the position off the rail rather than hardcoding it: the wizard drops
    # the profile step for an admin who already replaced both credentials, so a
    # given step's index differs between boxes.
    idx = page.evaluate(
        "(want) => [...document.querySelectorAll('.ob-steps .ob-step-label')]"
        ".findIndex((e) => (e.textContent || '').trim().toLowerCase() === want.toLowerCase())",
        label,
    )
    assert idx > 0, f"{label!r} step missing from the wizard rail"
    page.evaluate(
        "(i) => localStorage.setItem('lc.onboardingProgress', JSON.stringify("
        "{idx: i, maxIdx: i, outcomes: {profile: 'done'}}))",
        idx,
    )
    page.reload(wait_until="domcontentloaded")
    page.locator(".ob-root").wait_for(state="visible", timeout=15_000)


def _walk_to_provider_step(page) -> None:
    _walk_to_step(page, "AI provider")


def _codex_card_on_provider_step(page):
    """Walk to the provider step and return the OpenAI Codex card.

    Waits on the card rather than `.ob-provider-grid`: the grid renders empty
    (zero-height, so Playwright reads it as not visible) until GET
    /api/catalog/providers resolves, which is slow enough on a busy runtime to
    flake a 10s wait.
    """
    _walk_to_provider_step(page)
    card = page.locator(".ob-provider", has_text="OpenAI Codex")
    card.wait_for(state="visible", timeout=20_000)
    return card


def test_provider_step_explains_itself_while_the_catalog_loads(page, onboarding_dashboard) -> None:
    """The picker must never be a blank panel with no explanation.

    GET /api/catalog/providers used to be the step's silent dependency: the
    grid rendered empty until it resolved, so the admin stared at an unexplained
    gap. Holding the request open pins the replacement — skeleton tiles plus a
    live-region status line — and the tiles must be placeholders, not real
    cards, so nothing is clickable before the catalog lands.
    """
    _reset_onboarding(onboarding_dashboard.runtime_url)
    # Never fulfilled: the step stays in its loading state for the whole test.
    page.route("**/api/catalog/providers*", lambda route: None)

    _sign_in(page)
    _walk_to_provider_step(page)

    status = page.get_by_text("Loading available providers")
    status.wait_for(state="visible", timeout=15_000)
    assert page.locator(".ob-provider.skeleton").count() == 6
    # Placeholders only — a real (selectable) card would let the admin pick a
    # provider the catalog hasn't confirmed exists.
    assert page.locator(".ob-provider-grid [role='option']").count() == 0


def test_provider_step_offers_a_retry_when_the_catalog_fails(page, onboarding_dashboard) -> None:
    """A failed catalog fetch is recoverable in place, not a dead end.

    The load error used to be swallowed (`.catch(() => {})`), leaving the same
    empty grid as the loading case with no way to tell the difference and no way
    to re-trigger the fetch short of restarting the wizard.
    """
    _reset_onboarding(onboarding_dashboard.runtime_url)
    failed: list[str] = []
    # Gated rather than fail-once: StrictMode double-invokes the mount effect in
    # dev, so the step fires two catalog requests. Letting the second through
    # would flip the step to "ready" and pull the retry row out from under the
    # assertion. Everything fails until the test opens the gate.
    reachable = {"catalog": False}

    def route_catalog(route):
        if reachable["catalog"]:
            return route.continue_()
        failed.append(route.request.url)
        return route.abort()

    page.route("**/api/catalog/providers*", route_catalog)

    _sign_in(page)
    _walk_to_provider_step(page)

    retry = page.locator(".ob-retry")
    retry.wait_for(state="visible", timeout=15_000)
    assert "Couldn't load the provider list." in retry.inner_text()

    assert failed, "the catalog request should have been aborted"

    reachable["catalog"] = True
    retry.get_by_role("button", name="Try again").click()
    page.locator(".ob-provider", has_text="Anthropic").wait_for(state="visible", timeout=20_000)
    assert page.locator(".ob-retry").count() == 0


def test_codex_connect_settles_instead_of_restarting_the_device_flow(
    page, onboarding_dashboard
) -> None:
    """Regression: approval used to kick off a *second* device flow.

    CodexConnectDialog listed `onConnected` in its effect deps, and this step
    refreshes the provider catalog from that callback. So approval re-rendered
    the parent → fresh callback identity → effect teardown → another POST
    /start with a brand-new code, and the modal fell back to "waiting for you
    to approve" even though the tokens were already saved. Asserting on the
    /start count is what pins the fix: the dialog must mint exactly one code.
    """
    _reset_onboarding(onboarding_dashboard.runtime_url)
    starts: list[str] = []
    _stub_codex_device_flow(page, starts)

    _sign_in(page)
    _codex_card_on_provider_step(page).click()
    page.get_by_role("button", name="Connect with ChatGPT").click()

    # The modal reaches "connected" once the first poll comes back approved.
    connected = page.get_by_text("OpenAI Codex is connected.")
    connected.wait_for(state="visible", timeout=15_000)

    # …and stays there. Two poll intervals is long enough for a restarted flow
    # to have re-issued a code and flipped the modal back to pending.
    page.wait_for_timeout(2_500)
    assert connected.is_visible(), "modal fell back out of the connected state"
    assert page.get_by_text("Waiting for you to approve").count() == 0
    assert len(starts) == 1, f"device flow restarted: {len(starts)} POSTs to /start"

    # The step itself registers the connection, so Continue (not Skip) is
    # offered even though nothing was written to the vault.
    page.locator(".ob-form").get_by_text("ChatGPT subscription connected.").wait_for(
        state="visible", timeout=5_000
    )
    page.locator(".modal-overlay .btn.primary").last.click()  # Done → close modal
    page.get_by_role("button", name="Continue").wait_for(state="visible", timeout=5_000)


def test_credits_only_plan_leads_with_what_is_included(page, onboarding_dashboard) -> None:
    """A plan whose AI is prepaid must read as included, never as skippable.

    This step used to collapse to one grey line — "Model access is managed by
    your deployment plan — you can skip this step" — with Skip as its only
    forward action, for a customer whose plan already covers six managed AI
    services. Nothing named the plan, listed a service, or showed the balance.
    """
    _reset_onboarding(onboarding_dashboard.runtime_url)
    _pose_as_managed_cloud(page, plan="Pro", credits_left=12_500.0)
    _stub_provider_catalog(page, byok_locked=True, key_entry_locked=True)

    _sign_in(page)
    _walk_to_provider_step(page)

    page.locator(".ob-included").wait_for(state="visible", timeout=20_000)
    card = page.locator(".ob-card").inner_text()
    assert "Your AI is already included" in card
    assert "Pro" in card, "the panel should name the plan it is describing"
    for label in INCLUDED_LABELS:
        assert label in card, f"{label!r} missing from the included-services list"
    # The old copy, and its "you can skip this" framing, must be gone.
    assert "you can skip this step" not in card

    # The balance is display-only, so it renders as a chip rather than a number
    # the admin might read as something to spend here.
    chip = page.locator(".ob-credits-chip")
    chip.wait_for(state="visible", timeout=5_000)
    chip_text = chip.inner_text()
    assert "credits available" in chip_text
    assert "12" in chip_text, f"balance missing from the chip: {chip_text!r}"

    # Nothing to fill in: the plan excludes BYOK, so no picker and no key field.
    assert page.locator(".ob-provider-grid [role='option']").count() == 0
    assert page.locator(".ob-card input[type='password']").count() == 0
    # …but there is a way out of the plan, phrased as an upgrade rather than a
    # lock, and it points at this deployment's console card.
    upsell = page.locator(".ob-hint", has_text="Bring-your-own-keys")
    upsell.wait_for(state="visible", timeout=5_000)
    assert upsell.locator("a").get_attribute("href") == (
        f"https://cloud.olano.ai/app/deployments/{CLOUD_DEPLOYMENT_ID}"
    )

    # Model access *is* configured — by the plan — so the step is finished, and
    # the wizard offers Continue instead of asking the admin to skip.
    nav = page.locator(".ob-actions")
    nav.get_by_role("button", name="Continue").wait_for(state="visible", timeout=5_000)
    assert nav.get_by_role("button", name="Skip for now").count() == 0


def test_plan_with_byok_offers_keys_as_an_extra_on_top_of_credits(
    page, onboarding_dashboard
) -> None:
    """Credits first, keys second — an Ultra box has both, and said neither.

    The step used to render the bare provider grid here, identical to a
    self-hosted install: nothing told the customer their agents already run on
    included credits, so it read as "paste a key or you have no AI".
    """
    _reset_onboarding(onboarding_dashboard.runtime_url)
    _pose_as_managed_cloud(page, plan="Ultra")
    _stub_provider_catalog(page, byok_locked=False, key_entry_locked=False)

    _sign_in(page)
    _walk_to_provider_step(page)

    page.locator(".ob-included").wait_for(state="visible", timeout=20_000)
    split = page.locator(".ob-byok-split")
    split.wait_for(state="visible", timeout=10_000)
    split_text = split.inner_text()
    assert "Bring your own keys" in split_text
    assert "Optional" in split_text, "keys must not read as a requirement here"

    # The picker still works exactly as it does on a self-hosted box.
    page.locator(".ob-provider", has_text="Anthropic").click()
    page.locator(".ob-form input[type='password']").wait_for(state="visible", timeout=10_000)

    # No balance was published, so no chip invents one, and the plan includes
    # BYOK so there is nothing to upsell.
    assert page.locator(".ob-credits-chip").count() == 0
    assert page.locator(".ob-hint", has_text="Bring-your-own-keys").count() == 0

    # Credits already cover the box: done on arrival, key or no key.
    page.locator(".ob-actions").get_by_role("button", name="Continue").wait_for(
        state="visible", timeout=5_000
    )


def test_withheld_service_drops_out_of_the_included_list(page, onboarding_dashboard) -> None:
    """The checklist is an entitlement read, not a brochure.

    A plan withholds one managed service by setting its flag False; promising it
    anyway would send the customer to a tool that then refuses at the gateway.
    """
    _reset_onboarding(onboarding_dashboard.runtime_url)
    _pose_as_managed_cloud(page, features={"olano_tts": False, "olano_image": False})
    _stub_provider_catalog(page, byok_locked=True, key_entry_locked=True)

    _sign_in(page)
    _walk_to_provider_step(page)

    page.locator(".ob-included").wait_for(state="visible", timeout=20_000)
    listed = page.locator(".ob-included").inner_text()
    assert "Text to speech" not in listed
    assert "Image generation" not in listed
    # The rest of the plan is unaffected — a missing key still means enabled.
    assert "Intelligence" in listed
    assert "Speech to text" in listed


def test_quarantined_box_falls_back_instead_of_celebrating_nothing(
    page, onboarding_dashboard
) -> None:
    """Quarantine switches every managed service off (QUARANTINE_SETTINGS).

    The panel is the whole "you already have AI" message, so with an empty
    service list it must not render at all — an included-services card listing
    nothing would be worse than the plain locked line.
    """
    _reset_onboarding(onboarding_dashboard.runtime_url)
    _pose_as_managed_cloud(
        page,
        features={
            "olano_models": False,
            "olano_image": False,
            "olano_vision": False,
            "olano_web_search": False,
            "olano_stt": False,
            "olano_tts": False,
        },
    )
    _stub_provider_catalog(page, byok_locked=True, key_entry_locked=True)

    _sign_in(page)
    _walk_to_provider_step(page)

    fallback = page.locator(".ob-hint", has_text="managed by your deployment plan")
    fallback.wait_for(state="visible", timeout=20_000)
    assert page.locator(".ob-included").count() == 0
    assert page.locator(".ob-card").inner_text().count("Your AI is already included") == 0


def test_self_hosted_provider_step_still_asks_for_a_key(page, onboarding_dashboard) -> None:
    """No stub: an unmanaged box has no gateway, so BYOK is its only route.

    The credits panel must never appear here — there is no plan to include
    anything, and claiming otherwise would leave the admin with no working
    model and no idea why.
    """
    _reset_onboarding(onboarding_dashboard.runtime_url)
    _sign_in(page)
    _walk_to_provider_step(page)

    page.locator(".ob-provider", has_text="Anthropic").wait_for(state="visible", timeout=20_000)
    assert page.locator(".ob-included").count() == 0
    assert page.locator(".ob-credits-chip").count() == 0
    assert page.locator(".ob-byok-split").count() == 0
    card = page.locator(".ob-card").inner_text()
    assert "Connect an AI provider" in card


def _stub_credits_key(page) -> tuple[dict, list[dict]]:
    """Serve /api/olano/credits-key like a self-hosted box: no key yet, and a
    PUT that verifies and connects. Returns the mutable state + the PUT log."""
    state = {"connected": False}
    puts: list[dict] = []

    def status() -> dict:
        return {
            "manageable": True,
            "posture": "open",
            "source": "account" if state["connected"] else None,
            "connected": state["connected"],
            "key_prefix": "sk-olano-aaaaaa" if state["connected"] else None,
            "rejected": False,
            "cloud_url": "https://cloud.example.test",
            "key_url": "https://cloud.example.test/app/api-keys",
            "billing_url": "https://cloud.example.test/app/billing",
        }

    def handler(route):
        if route.request.method == "PUT":
            puts.append(route.request.post_data_json)
            state["connected"] = True
            body = {
                **status(),
                "verified": {
                    "org_name": "Acme",
                    "balance": 4_200,
                    "lifetime_used": 0,
                    "credits_per_usd": 1000,
                    "services": [
                        "intelligence",
                        "image",
                        "vision",
                        "stt",
                        "tts",
                        "web_search",
                        "web_fetch",
                    ],
                    "key": {"name": "office", "prefix": "sk-olano-aaaaaa"},
                },
                "live_models_updated": 0,
            }
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(status()))

    page.route("**/api/olano/credits-key", handler)
    return state, puts


def test_self_hosted_provider_step_offers_olano_credits_above_byok(
    page, onboarding_dashboard
) -> None:
    """Shape C grows a second route: an Olano API key from cloud.olano.ai.

    Offered, never assumed: before a key is pasted the step still asks for a
    provider, lists nothing as included, and stays skippable. Connecting a key
    turns the card into the same included checklist a managed plan shows (with
    the account's balance) and finishes the step, while the BYOK picker stays
    below it as the other route.
    """
    _reset_onboarding(onboarding_dashboard.runtime_url)
    _, puts = _stub_credits_key(page)
    _sign_in(page)
    _walk_to_provider_step(page)

    card = page.locator("[data-testid='ob-olano-key']")
    card.wait_for(state="visible", timeout=20_000)
    text = card.inner_text()
    assert "Run on Olano credits" in text
    assert "Optional" in text
    assert page.locator(".ob-included").count() == 0
    # The BYOK route is still right there, framed as the alternative.
    page.locator(".ob-provider", has_text="Anthropic").wait_for(state="visible", timeout=20_000)
    assert "Or bring your own keys" in page.locator(".ob-card").inner_text()
    assert "Connect an AI provider" in page.locator(".ob-card").inner_text()

    card.locator("input[name='olano-credits-key']").fill("sk-olano-" + "a" * 40)
    card.get_by_role("button", name="Connect").click()

    page.locator(".ob-included").wait_for(state="visible", timeout=15_000)
    assert puts == [{"key": "sk-olano-" + "a" * 40}]
    text = card.inner_text()
    assert "Olano credits connected" in text
    for label in INCLUDED_LABELS:
        assert label in text, f"{label!r} missing from the included list"
    chip = page.locator(".ob-credits-chip")
    chip.wait_for(state="visible", timeout=5_000)
    assert "4" in chip.inner_text()
    assert card.get_by_role("link", name="Buy credits").get_attribute("href") == (
        "https://cloud.example.test/app/billing"
    )
    # Model access is configured, so the footer offers Continue.
    nav = page.locator(".ob-actions")
    nav.get_by_role("button", name="Continue").wait_for(state="visible", timeout=5_000)


def test_bootstrap_created_admin_gets_the_vault_only_profile(page, onboarding_dashboard) -> None:
    """The module's admin came from /api/auth/bootstrap, which only runs on an
    empty user table — no account ever held the provisioned temporary password,
    and the rotation is recorded at bootstrap. Asking that owner to name and
    re-password the account they chose a minute ago would read as though it had
    not taken, so the profile step shows only its vault half."""
    _reset_onboarding(onboarding_dashboard.runtime_url)
    _pose_as_managed_cloud(page)
    _pose_vault(page, unlocked=False)
    _sign_in(page)

    page.locator(".ob-root").wait_for(state="visible", timeout=15_000)
    rail = page.locator(".ob-steps").inner_text()
    assert "Your profile" in rail
    assert "Meet Agent Father" in rail
    for retired in ("Login password", "Your account", "First agent", "Messaging app", "Web search"):
        assert retired not in rail

    _start_wizard(page)
    # No account half: no name, no email, no new-password pair.
    assert page.locator(".ob-form input[name='name']").count() == 0
    assert page.locator(".ob-form input[type='email']").count() == 0
    assert page.locator("[data-testid='handle-preview']").count() == 0
    # Locked vault: current + new + confirm passphrase.
    assert page.get_by_text("Current passphrase").count() == 1
    assert page.locator(".ob-form input[type='password']").count() == 3


def test_provisioned_admin_claims_the_account_on_the_profile_step(
    page, onboarding_dashboard
) -> None:
    """A box whose provisioned temporary password is still live shows the
    account half: name, the handle it mints (read-only), a new password pair
    — never the temporary password again — plus the vault pair, all on one
    panel, with Skip withheld until both halves are saved.

    The claim itself runs against the real backend in
    tests/runtime/test_auth_claim_api.py; here the two saves are stubbed so
    the module's shared home is not rewritten under the other tests."""
    _reset_onboarding(onboarding_dashboard.runtime_url)
    _pose_as_managed_cloud(page)
    _pose_as_unrotated_admin(page)
    _pose_vault(page, unlocked=True)
    page.route(
        "**/api/auth/claim",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "user": {
                        "id": "u1",
                        "email": ADMIN["email"],
                        "username": "carlos-rivera",
                        "name": "Carlos Rivera",
                        "is_admin": True,
                        "is_active": True,
                        "timezone": "UTC",
                        "created_at": "2026-09-05T00:00:00+00:00",
                    }
                }
            ),
        ),
    )
    page.route(
        "**/api/vault/rekey",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"ok": True})
        ),
    )
    _sign_in(page)

    page.locator(".ob-root").wait_for(state="visible", timeout=15_000)
    assert "Your profile" in page.locator(".ob-steps").inner_text()
    page.locator(".ob-welcome .btn.primary").click()

    name = page.locator(".ob-form input[name='name']")
    name.wait_for(state="visible", timeout=10_000)
    card = page.locator(".ob-card")
    # The panel names the account being claimed — same email, no second one.
    assert ADMIN["email"] in card.inner_text()
    # The temporary password is never asked for again.
    assert card.locator("input[autocomplete='current-password']").count() == 0
    # Two pairs: new password + confirm, vault passphrase + confirm.
    assert card.locator("input[type='password']").count() == 4

    # The handle is minted from the name as they type, and is not an input.
    name.fill("Carlos Rivera")
    preview = page.locator("[data-testid='handle-preview']")
    preview.wait_for(state="visible", timeout=5_000)
    assert "carlos-rivera" in preview.inner_text()
    assert preview.locator("input").count() == 0

    # Managed ⇒ no Skip on this step: only the required note until it's done.
    nav = page.locator(".ob-actions")
    nav.locator(".ob-required-note").wait_for(state="visible", timeout=10_000)
    assert nav.get_by_role("button", name="Skip for now").count() == 0

    pw = card.locator("input[type='password']")
    pw.nth(0).fill("my-own-password")
    pw.nth(1).fill("my-own-password")
    pw.nth(2).fill("my-own-vault-pass")
    pw.nth(3).fill("my-own-vault-pass")
    page.get_by_role("button", name="Save and continue").click()

    oks = page.locator(".ob-ok")
    oks.nth(1).wait_for(state="visible", timeout=15_000)
    assert (
        "Carlos Rivera" in oks.nth(0).inner_text() and "@carlos-rivera" in oks.nth(0).inner_text()
    )
    # Both halves saved → Continue replaces the required note.
    nav.get_by_role("button", name="Continue").wait_for(state="visible", timeout=5_000)


def test_managed_vault_step_links_to_the_cloud_console_while_locked(
    page, onboarding_dashboard
) -> None:
    """A locked vault still needs its current passphrase, and on a managed box
    that passphrase lives on the deployment's console card (the profile step's
    vault half, on a box whose owner bootstrapped their own account)."""
    _reset_onboarding(onboarding_dashboard.runtime_url)
    _pose_as_managed_cloud(page)
    _pose_vault(page, unlocked=False)
    _sign_in(page)
    _start_wizard(page)

    expected = f"https://cloud.olano.ai/app/deployments/{CLOUD_DEPLOYMENT_ID}"
    vault_link = page.locator(".ob-hint a")
    vault_link.wait_for(state="visible", timeout=10_000)
    assert vault_link.get_attribute("href") == expected
    # Managed boxes get the cloud line *instead of* the generic hint, whose
    # "self-hosted setups default it to the initial admin password" half is
    # actively misleading here.
    vault_text = page.locator(".ob-form").inner_text()
    assert "generated when it was provisioned" in vault_text
    assert "Self-hosted setups" not in vault_text
    # The profile step is required on a managed box.
    page.locator(".ob-actions .ob-required-note").wait_for(state="visible", timeout=10_000)
    assert page.locator(".ob-actions").get_by_role("button", name="Skip for now").count() == 0


def test_unlocked_vault_lets_the_owner_set_a_passphrase_outright(
    page, onboarding_dashboard
) -> None:
    """The managed box auto-unlocks from its boot key, so the owner is not sent
    to another website to copy a temporary passphrase they never chose: an open
    vault already proves key possession, and the step asks only for the new one."""
    _reset_onboarding(onboarding_dashboard.runtime_url)
    _pose_as_managed_cloud(page)
    _pose_vault(page, unlocked=True)
    _sign_in(page)
    _start_wizard(page)

    form = page.locator(".ob-form")
    assert form.locator("input[autocomplete='current-password']").count() == 0
    assert page.get_by_text("Current passphrase").count() == 0
    assert "set your own passphrase directly" in form.inner_text()
    # Still two fields: new + confirm.
    assert form.locator("input[type='password']").count() == 2


def test_managed_deployment_withholds_the_whole_tour_skip(page, onboarding_dashboard) -> None:
    """Skipping the whole tour would skip the mandatory credential rotation,
    so on a managed box both skip-alls — the welcome panel's "Skip the tour"
    and the rail's "Skip setup for now" — are withheld until the profile step
    is done. (Self-hosted keeps both:
    test_first_admin_login_walks_and_skips_the_wizard clicks one.)"""
    _reset_onboarding(onboarding_dashboard.runtime_url)
    _pose_as_managed_cloud(page)
    _sign_in(page)

    page.locator(".ob-root").wait_for(state="visible", timeout=15_000)
    page.locator(".ob-welcome .btn.primary").wait_for(state="visible", timeout=10_000)
    # The deployment stub answers instantly; give the hook a beat to settle,
    # then neither skip-all may exist anywhere in the wizard.
    page.wait_for_timeout(500)
    assert page.get_by_role("button", name="Skip the tour").count() == 0
    assert page.get_by_role("button", name="Skip setup for now").count() == 0


def test_self_hosted_install_shows_no_cloud_console_link(page, onboarding_dashboard) -> None:
    """No stub: the real runtime is unmanaged, so neither panel may link out.

    A self-hosted owner has no cloud console account — a dead link to one would
    be worse than no link at all.
    """
    _reset_onboarding(onboarding_dashboard.runtime_url)
    _pose_vault(page, unlocked=False)
    _sign_in(page)
    _start_wizard(page)

    assert page.locator(".ob-hint a[href*='cloud.olano.ai']").count() == 0
    # The generic hint is what a self-hosted owner needs, and it stays.
    assert "Self-hosted setups" in page.locator(".ob-form").inner_text()
    # Nothing here is mandatory on a self-hosted box: Skip is offered.
    _skip_to_next_step(page)


def test_first_admin_login_walks_and_skips_the_wizard(page, onboarding_dashboard) -> None:
    _reset_onboarding(onboarding_dashboard.runtime_url)
    _sign_in(page)

    # The wizard replaces the app for the first admin.
    wizard = page.locator(".ob-root")
    wizard.wait_for(state="visible", timeout=15_000)

    # Welcome panel → start → the profile step renders its form. The module's
    # admin created their own account, so only its vault half shows: the
    # password half has nothing left to rotate.
    page.locator(".ob-welcome .btn.primary").click()
    page.locator(".ob-form input[type='password']").first.wait_for(state="visible", timeout=10_000)
    # Back returns to the welcome panel.
    page.locator(".ob-actions .btn.ghost").first.click()
    page.locator(".ob-welcome").wait_for(state="visible", timeout=10_000)

    # Skip the whole tour → wizard completes server-side → app shell renders.
    # No Agent Father in the harness home, so there is no greeting thread to
    # land on and the tour ends on Overview.
    page.locator(".ob-welcome .btn.ghost").click()
    page.locator(".ob-root").wait_for(state="detached", timeout=15_000)
    page.wait_for_url("**/overview", timeout=15_000)

    # Reload: the flag is persisted server-side, so the wizard must NOT return.
    page.reload(wait_until="domcontentloaded")
    page.wait_for_url("**/overview", timeout=15_000)
    assert page.locator(".ob-root").count() == 0


def test_meet_agent_father_step_installs_and_lands_in_its_chat(page, onboarding_dashboard) -> None:
    """The last panel introduces Agent Father, brings it online on mount, and
    its primary button finishes the tour INTO Agent Father's chat — on the
    thread the runtime greeted the owner on, with the greeting already there.

    The harness home has no Agent Father (one fake `merli`), so the install,
    the completion's greeting pointer, the roster and the thread are served
    from the browser; the real greeting turn is covered in
    tests/runtime/test_onboarding_greeting.py."""
    _reset_onboarding(onboarding_dashboard.runtime_url)
    _pose_agentfather_installed(page)

    # The real completion still lands server-side (the flag must clear, or the
    # wizard would gate the chat we are about to assert on); only the greeting
    # pointer is injected into its answer.
    def complete(route):
        response = route.fetch()
        body = response.json()
        body["greeting"] = {"agent": "agentfather", "thread_id": "welcome-e2e"}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/onboarding/complete", complete)

    # Agent Father in the roster, so the chat's agent picker can land on it.
    def roster(route):
        response = route.fetch()
        agents = response.json()
        agents.append(
            {
                "name": "agentfather",
                "display_name": "Agent Father",
                "emoji": "🦁",
                "model": "default:turbo",
                "description": "Builds and runs your team.",
                "channels": [],
                "running": True,
                "status": "running",
            }
        )
        route.fulfill(status=200, content_type="application/json", body=json.dumps(agents))

    page.route(re.compile(r".*/api/agents(\?.*)?$"), roster)
    page.route(
        "**/api/agents/agentfather/threads/welcome-e2e",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "id": "welcome-e2e",
                    "origin": "interactive",
                    "channel": "dashboard",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": "Hi Carlos, I'm Agent Father. What shall we build first?",
                            "ts": "2026-09-05T10:00:05+00:00",
                        }
                    ],
                }
            ),
        ),
    )
    page.route(
        "**/api/agents/agentfather/threads",
        lambda route: route.fulfill(status=200, content_type="application/json", body="[]"),
    )

    _sign_in(page)
    _walk_to_step(page, "Meet Agent Father")

    card = page.locator(".ob-card")
    page.locator(".ob-father-caps").wait_for(state="visible", timeout=15_000)
    text = card.inner_text()
    assert "Creates agents" in text
    assert "Asks before it acts" in text
    # No first-agent form, no messenger token field, no search key: the tour
    # hands those to Agent Father.
    assert card.locator("input").count() == 0

    meet = card.get_by_role("button", name="Meet Agent Father")
    meet.wait_for(state="visible", timeout=15_000)
    meet.click()

    page.wait_for_url("**/chat/agentfather?thread=welcome-e2e", timeout=20_000)
    page.locator(".ob-root").wait_for(state="detached", timeout=15_000)
    page.get_by_text("Hi Carlos, I'm Agent Father.").wait_for(state="visible", timeout=20_000)

    # Reload keeps the conversation: the flag is server-side, the thread is in the URL.
    page.reload(wait_until="domcontentloaded")
    page.wait_for_url("**/chat/agentfather?thread=welcome-e2e", timeout=15_000)
    assert page.locator(".ob-root").count() == 0


def test_meet_agent_father_step_says_it_is_waking_and_the_chat_shows_it(
    page, onboarding_dashboard
) -> None:
    """The greeting is a plain invoke the runtime runs on its own thread — it is
    not a live run, so nothing streams and the chat had NO "thinking" state:
    a cold Agent Father boot left a blank thread for up to a minute and the
    owner assumed the wizard had broken. Two halves close that: the last panel
    says ahead of time that Agent Father wakes up and greets, with things to
    try and things to tell it; and the chat it lands in draws "waking up"
    instead of the empty-thread prompt until the first row arrives.

    The greeting thread is served EMPTY here, so the waking state is what the
    landing must show; the greeting-present path is the test above."""
    _reset_onboarding(onboarding_dashboard.runtime_url)
    _pose_agentfather_installed(page)

    def complete(route):
        response = route.fetch()
        body = response.json()
        body["greeting"] = {"agent": "agentfather", "thread_id": "welcome-e2e-waking"}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/onboarding/complete", complete)

    def roster(route):
        response = route.fetch()
        agents = response.json()
        agents.append(
            {
                "name": "agentfather",
                "display_name": "Agent Father",
                "emoji": "🦁",
                "model": "default:turbo",
                "description": "Builds and runs your team.",
                "channels": [],
                "running": True,
                "status": "running",
            }
        )
        route.fulfill(status=200, content_type="application/json", body=json.dumps(agents))

    page.route(re.compile(r".*/api/agents(\?.*)?$"), roster)
    # Nothing written yet: Agent Father is still booting.
    page.route(
        "**/api/agents/agentfather/threads/welcome-e2e-waking",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "id": "welcome-e2e-waking",
                    "origin": "interactive",
                    "channel": "dashboard",
                    "messages": [],
                }
            ),
        ),
    )
    page.route(
        "**/api/agents/agentfather/threads",
        lambda route: route.fulfill(status=200, content_type="application/json", body="[]"),
    )

    _sign_in(page)
    _walk_to_step(page, "Meet Agent Father")

    card = page.locator(".ob-card")
    page.locator(".ob-father-first").wait_for(state="visible", timeout=15_000)
    waking = card.locator(".ob-father-waking")
    assert waking.get_attribute("role") == "status"
    assert "wakes up and says hello" in waking.inner_text()
    text = card.inner_text()
    assert "Good first things to ask" in text
    assert "Which providers and connectors can I use?" in text
    assert "Tell it about yourself" in text
    assert "personally or for a business" in text
    assert "repetitive things you do every day or week" in text
    # Three sample prompts, four things to tell it.
    assert card.locator(".ob-prompt").count() == 3
    assert card.locator(".ob-first-col").nth(1).locator("li").count() == 4

    card.get_by_role("button", name="Meet Agent Father").click()
    page.wait_for_url("**/chat/agentfather?thread=welcome-e2e-waking", timeout=20_000)
    page.locator(".ob-root").wait_for(state="detached", timeout=15_000)

    status = page.locator("[role='status'][aria-busy='true']", has_text="is waking up")
    status.wait_for(state="visible", timeout=20_000)
    assert "Agent Father is waking up" in status.inner_text()
    assert "say hello here in a moment" in status.inner_text()
    # The plain empty-thread prompt must NOT show alongside it.
    assert page.get_by_text("Start a conversation with Agent Father").count() == 0
