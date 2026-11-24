"""E2E: a key request in the dashboard chat is a form under the reply, not a link.

The magic link and its PIN exist for a messenger, where the reader is
unauthenticated and has no UI. In the dashboard the person is already signed
in on the deployment's own origin, so the chat draws a ``secret-entry`` /
``provider-key`` link as an inline card: paste the value, Save to vault. What
these prove, end to end through the real admin routes and the real link
store, is the property the link was protecting: the value reaches the vault
write and never becomes a message, never lands in the transcript. The
same-origin rule is proved too: a link to any other origin keeps the external
Connect button and never puts a credential field in the chat.
"""

from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.e2e

FORM_CARD = "[data-testid='secret-form-card']"
RESULT_CARD = ".connect-result"


def _seed_thread(agent: str, rows: list[dict]) -> str:
    """Write a transcript into the home the runtime is currently reading
    (see ``test_connect_result_card._seed_thread`` for why it is resolved here)."""
    from olano.config.home import get_olano_paths

    thread_id = str(uuid.uuid4())
    sessions = get_olano_paths().sessions_dir / agent
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{thread_id}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    return thread_id


def _rows(result: dict) -> list[dict]:
    """A turn in which merli asked for a key: the tool call, its result, the reply."""
    return [
        {"ts": "2026-10-25T09:00:00Z", "role": "user", "content": "add the stripe key for curtis"},
        {
            "ts": "2026-10-25T09:00:01Z",
            "role": "tool_call",
            "tool_call_id": "call_1",
            "name": "magic_link_request_secret",
            "args": json.dumps(
                {"name": "STRIPE_API_KEY", "agent": "curtis", "reason": "for invoices"}
            ),
            "content": "",
        },
        {
            "ts": "2026-10-25T09:00:02Z",
            "role": "tool_result",
            "tool_call_id": "call_1",
            "name": "magic_link_request_secret",
            "args": "",
            "content": json.dumps(result),
        },
        {
            "ts": "2026-10-25T09:00:03Z",
            "role": "assistant",
            "content": "Paste your Stripe key into the card below and I will use it for invoices.",
        },
    ]


@pytest.fixture
def minted(dashboard, monkeypatch):
    """A real secret-entry link whose page lives on the dashboard's own origin."""
    import olano.core.mcp_setup as mcp_setup
    from olano.magic_links import service

    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", dashboard.base_url)
    monkeypatch.setattr(mcp_setup, "_LAST_SEEN_ORIGIN", None)
    return service.mint_link(
        "secret-entry",
        params={"name": "STRIPE_API_KEY", "agent": "curtis", "reason": "for invoices"},
        requested_by={"channel": "api", "username": "owner", "agent": "merli"},
        notify={"channel_type": "api", "target": "", "agent_name": "merli"},
        minted_by="agent:merli",
    )


@pytest.fixture
def vault_writes(monkeypatch):
    """Capture the vault write instead of needing an unlocked vault."""
    from olano.magic_links.adapters import secret_entry

    calls: list[dict] = []

    def fake_store(name, value, *, agent, description, category="general"):
        calls.append({"name": name, "value": value, "agent": agent})

    monkeypatch.setattr(secret_entry, "store_secret", fake_store)
    return calls


def _open_thread(page, base_url: str, thread_id: str) -> None:
    page.goto(f"{base_url.rstrip('/')}/chat?thread={thread_id}", wait_until="domcontentloaded")


def test_a_key_request_is_filled_in_right_under_the_reply(page, dashboard, minted, vault_writes):
    from olano.config.home import get_olano_paths
    from olano.magic_links import store

    # The notice lands in the thread the link was minted from.
    thread_id = _seed_thread("merli", _rows({**minted.public(), "agent_guidance": "…"}))
    store.mutate(minted.id, lambda r: r.notify.update(target=thread_id))
    _open_thread(page, dashboard.base_url, thread_id)

    card = page.locator(FORM_CARD)
    card.wait_for(state="visible", timeout=20_000)
    # The form, not the link: the flow's own title and a password field, and
    # no Connect button, no PIN to read off.
    card.get_by_text("Add STRIPE_API_KEY").wait_for(state="visible", timeout=10_000)
    assert "for invoices" in card.inner_text()
    assert "PIN" not in card.inner_text()
    assert page.locator(".connect-card__btn", has_text="Connect").count() == 0
    field = card.locator("input[type='password']")
    assert field.count() == 1
    save = card.get_by_role("button", name="Save to vault")
    assert save.is_disabled()

    field.fill("sk_live_e2e_secret")
    assert save.is_enabled()
    save.click()

    card.get_by_text("Saved to the vault", exact=True).wait_for(state="visible", timeout=15_000)
    assert "STRIPE_API_KEY saved" in card.inner_text()
    assert vault_writes == [
        {"name": "STRIPE_API_KEY", "value": "sk_live_e2e_secret", "agent": "curtis"}
    ]
    record = store.get(minted.id)
    assert record.status == store.STATUS_DONE

    # The completion notice the watcher posts arrives in the chat as its own
    # card, exactly as it does for a link finished on a phone.
    result = page.locator(RESULT_CARD)
    result.wait_for(state="visible", timeout=40_000)
    assert "STRIPE_API_KEY saved" in result.inner_text()

    # The value went to the vault write and nowhere else: not into the
    # transcript, not into the link record.
    transcript = (get_olano_paths().sessions_dir / "merli" / f"{thread_id}.jsonl").read_text()
    assert "sk_live_e2e_secret" not in transcript
    assert "sk_live_e2e_secret" not in json.dumps(record.__dict__)

    # Reopened, the turn shows the saved state, never a second form.
    _open_thread(page, dashboard.base_url, thread_id)
    card = page.locator(FORM_CARD)
    card.wait_for(state="visible", timeout=20_000)
    card.get_by_text("Saved to the vault", exact=True).wait_for(state="visible", timeout=10_000)
    assert card.locator("input").count() == 0


def test_a_link_to_another_origin_keeps_the_connect_button(page, dashboard):
    """Detection is by tool name, and a tool merely named ``magic_link_*``
    (an MCP tool, say) could return any URL: that gets the external button,
    never a credential field in the chat."""
    result = {
        "id": "hs_0123456789abcdef",
        "url": "https://elsewhere.example/api/connect/some-token",
        "pin": "123456",
        "flow": "secret-entry",
        "label": "API key",
        "expires_at": "2026-10-25T10:00:00+00:00",
        "ttl_minutes": 15,
    }
    thread_id = _seed_thread("merli", _rows(result))
    _open_thread(page, dashboard.base_url, thread_id)

    button = page.locator(".connect-card__btn", has_text="Connect")
    button.wait_for(state="visible", timeout=20_000)
    assert page.locator(FORM_CARD).count() == 0
    assert page.locator(".connect-card input").count() == 0
    assert "123456" in page.locator(".connect-card").inner_text()


def test_a_sign_in_link_still_opens_as_a_page(page, dashboard, monkeypatch):
    """Only the two secret flows go inline; an OAuth sign-in on this very
    origin keeps the button, because the provider's consent screen has to be
    opened somewhere."""
    result = {
        "id": "hs_fedcba9876543210",
        "url": f"{dashboard.base_url}/api/connect/some-token",
        "pin": "654321",
        "flow": "connector-oauth",
        "label": "Connect Microsoft 365",
        "expires_at": "2026-10-25T10:00:00+00:00",
        "ttl_minutes": 15,
    }
    thread_id = _seed_thread("merli", _rows(result))
    _open_thread(page, dashboard.base_url, thread_id)

    page.locator(".connect-card__btn", has_text="Connect").wait_for(state="visible", timeout=20_000)
    assert page.locator(FORM_CARD).count() == 0
