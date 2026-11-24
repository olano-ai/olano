"""LinkedIn outreach on the owner's own Unipile workspace (BYOK).

A fake Unipile answers ``httpx.request``; the assertions are on what the box
sends, what it remembers, and what the tools tell the model. The managed path
is covered by test_linkedin_outreach_tools.py.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from olano.deployment import linkedin_client, linkedin_direct
from olano.tools import linkedin_outreach_tools as lo

DSN = "api9.unipile.com:13999"
ORIGIN = f"https://{DSN}"


class FakeUnipile:
    """A tiny workspace: accounts, one profile, chats per account, a call log."""

    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, Any]] = {}
        self.chats: dict[str, str] = {}  # chat id -> owning account id
        self.calls: list[dict[str, Any]] = []
        self.next_error: tuple[int, str] | None = None
        self.reads = 0

    def add_account(
        self, acc_id: str, created: str = "2026-10-01T00:00:00Z", status: str = "OK"
    ) -> None:
        self.accounts[acc_id] = {
            "id": acc_id,
            "type": "LINKEDIN",
            "name": f"Owner {acc_id}",
            "created_at": created,
            "sources": [{"status": status}],
            "connection_params": {"im": {"publicIdentifier": f"pub-{acc_id}"}},
        }

    def __call__(self, method, url, params=None, headers=None, json=None, files=None, timeout=None):
        assert url.startswith(ORIGIN + "/api/v1/"), url
        assert headers["X-API-KEY"] == "byok-key"
        path = url[len(ORIGIN) + len("/api/v1") :]
        self.calls.append(
            {"method": method, "path": path, "params": params or {}, "json": json, "files": files}
        )
        req = httpx.Request(method, url)
        if self.next_error:
            status, type_ = self.next_error
            self.next_error = None
            return httpx.Response(status, json={"type": type_, "detail": "nope"}, request=req)
        body = self.route(method, path, params or {}, json)
        if body is None:
            return httpx.Response(
                404, json={"type": "errors/not_found", "detail": "missing"}, request=req
            )
        return httpx.Response(200, json=body, request=req)

    def route(self, method, path, params, body):
        if method == "GET" and path == "/accounts":
            return {"items": list(self.accounts.values())}
        if path.startswith("/accounts/"):
            acc = path.split("/")[2]
            if method == "DELETE":
                return {} if self.accounts.pop(acc, None) else None
            return self.accounts.get(acc)
        if method == "POST" and path == "/hosted/accounts/link":
            return {"url": "https://account.unipile.com/link/abc"}
        if method == "GET" and path == "/users/invite/sent":
            # What Unipile answers: LinkedIn's wording in `date`, and that wording
            # resolved against the read in `parsed_datetime`.
            self.reads += 1
            return {
                "items": [
                    {
                        "object": "InvitationSent",
                        "id": "inv1",
                        "invited_user": "Bob Smith",
                        "invited_user_public_id": "bob",
                        "date": "Sent today",
                        "parsed_datetime": f"2026-10-16T22:{48 + self.reads}:34.202Z",
                        "invitation_text": None,
                    },
                    {"id": "inv2", "invited_user": "Ann", "date": "2026-10-09T13:07:00Z"},
                ]
            }
        if method == "GET" and path.startswith("/users/") and path.endswith("/posts"):
            return {
                "items": [{"social_id": "urn:li:activity:7222176104768270337", "text": "Shipped!"}]
            }
        if method == "GET" and path.startswith("/users/"):
            return {
                "provider_id": "ACo1",
                "first_name": "Bob",
                "last_name": "Smith",
                "public_identifier": "bob",
                "network_distance": "SECOND_DEGREE",
            }
        if method == "POST" and path == "/users/invite":
            return {"invitation_id": "inv1"}
        if method == "GET" and path.startswith("/chats/") and path.count("/") == 2:
            chat = path.split("/")[2]
            owner = self.chats.get(chat)
            return {"id": chat, "account_id": owner, "name": "Bob Smith"} if owner else None
        if method == "GET" and path.endswith("/messages"):
            return {"items": [{"id": "m1", "text": "Hello", "is_sender": 0, "sender_id": "ACo1"}]}
        if method == "GET" and path.endswith("/attendees"):
            return {"items": [{"provider_id": "ACo1", "name": "Bob Smith", "is_self": 0}]}
        if method == "POST" and path.endswith("/messages"):
            return {"message_id": "msg9"}
        return {}


@pytest.fixture
def unipile(monkeypatch):
    creds = {"UNIPILE_API_KEY": "byok-key", "UNIPILE_DSN": DSN}
    monkeypatch.setattr(
        "olano.tools._credentials.get_credential", lambda name, default="": creds.get(name, default)
    )
    # A public answer for the DSN host, so the private-network guard passes offline.
    monkeypatch.setattr(
        "olano.security.ssrf.socket.getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("93.184.216.34", 0))],
    )
    fake = FakeUnipile()
    monkeypatch.setattr(httpx, "request", fake)
    monkeypatch.setattr(lo, "_agent", lambda: "sales")
    lo._LAST_INTENT.clear()
    fake.creds = creds  # type: ignore[attr-defined]
    return fake


def test_byok_needs_both_names_and_takes_any_dsn_spelling(monkeypatch) -> None:
    creds: dict[str, str] = {"UNIPILE_API_KEY": "k"}
    monkeypatch.setattr(
        "olano.tools._credentials.get_credential", lambda name, default="": creds.get(name, default)
    )
    assert linkedin_direct.byok_credentials() is None
    creds["UNIPILE_DSN"] = "https://api1.unipile.com:13111/api/v1/"
    assert linkedin_direct.byok_credentials() == ("k", "https://api1.unipile.com:13111")
    assert (
        linkedin_direct.unipile_origin("api1.unipile.com:13111") == "https://api1.unipile.com:13111"
    )
    assert linkedin_direct.unipile_origin("http://api1.unipile.com") == ""


def test_byok_works_without_a_managed_deployment(unipile, monkeypatch) -> None:
    from types import SimpleNamespace

    from olano.deployment import resolver as dep_resolver

    monkeypatch.setattr(
        dep_resolver, "deployment_status", lambda now=None: SimpleNamespace(posture="open")
    )
    assert linkedin_client.linkedin_service_available() is True
    out = lo.linkedin_connect_account.func()
    assert "https://account.unipile.com/link/abc" in out
    link = next(c for c in unipile.calls if c["path"] == "/hosted/accounts/link")
    assert link["json"]["providers"] == ["LINKEDIN"] and link["json"]["api_url"] == ORIGIN
    assert "notify_url" not in link["json"]


def test_connect_then_status_binds_the_account_the_sign_in_added(unipile) -> None:
    unipile.add_account("old")  # already in the workspace: not this agent's
    unipile.add_account("other")
    lo.linkedin_connect_account.func()
    out = lo.linkedin_account_status.func()
    assert "not connected" in out and "not been completed" in out

    unipile.add_account("new", created="2026-10-08T10:00:00Z")
    out = lo.linkedin_account_status.func()
    assert "connected: Owner new" in out
    assert "own Unipile workspace" in out
    state = json.loads((linkedin_direct._state_path()).read_text())
    assert state["workspaces"][ORIGIN.lower()]["bindings"]["sales"] == {"id": "new", "via": "link"}


def test_a_single_account_is_adopted_and_a_pin_wins(unipile) -> None:
    unipile.add_account("solo")
    assert "connected: Owner solo" in lo.linkedin_account_status.func()

    unipile.add_account("two")
    unipile.creds["UNIPILE_ACCOUNT_ID"] = "two"
    assert "connected: Owner two" in lo.linkedin_account_status.func()


def test_invite_paces_locally_and_bills_nothing(unipile, monkeypatch) -> None:
    unipile.add_account("solo")
    monkeypatch.setenv("LINKEDIN_DAILY_CAP_INVITE", "1")
    out = lo.linkedin_send_invitation.func(profile="https://www.linkedin.com/in/bob", note="Hi Bob")
    assert "sent with your note" in out and "own Unipile workspace" in out and "1/1 today" in out
    invite = next(c for c in unipile.calls if c["path"] == "/users/invite")
    assert invite["json"] == {"account_id": "solo", "provider_id": "ACo1", "message": "Hi Bob"}

    out = lo.linkedin_send_invitation.func(profile="bob")
    assert "Daily limit reached" in out
    assert sum(1 for c in unipile.calls if c["path"] == "/users/invite") == 1


def test_a_pending_invitation_keeps_linkedins_wording_and_never_the_read_time(unipile) -> None:
    unipile.add_account("solo")
    first = lo.linkedin_list_sent_invitations.func(limit=100)
    second = lo.linkedin_list_sent_invitations.func(limit=3)
    assert first == second
    assert '- Bob Smith, LinkedIn shows "Sent today" (id: inv1)' in first
    assert "- Ann, sent 2026-10-09T13:07:00Z (id: inv2)" in first
    assert "22:4" not in first and "22:5" not in first
    assert linkedin_direct._sent_date("2026-13-45") == {"sent_at": None, "sent_label": "2026-13-45"}
    assert linkedin_direct._sent_date(None) == {"sent_at": None, "sent_label": None}


def test_a_chat_of_another_account_reads_as_unknown(unipile) -> None:
    unipile.add_account("solo")
    unipile.chats.update({"mine": "solo", "theirs": "someone-else"})
    assert "Hello" in lo.linkedin_read_conversation.func(conversation_id="mine")
    assert "No conversation with that id" in lo.linkedin_read_conversation.func(
        conversation_id="theirs"
    )
    reply = lo.linkedin_send_message.func(text="Thanks!", conversation_id="theirs")
    assert "No conversation with that id" in reply
    assert not any(c["method"] == "POST" and c["path"].endswith("/messages") for c in unipile.calls)


def test_a_lost_session_and_a_bad_key_say_different_things(unipile) -> None:
    unipile.add_account("solo")
    lo.linkedin_account_status.func()
    unipile.next_error = (401, "errors/disconnected_account")
    assert "signed this account out" in lo.linkedin_visit_profile.func(profile="bob")
    unipile.next_error = (401, "errors/unauthorized")
    assert "UNIPILE_API_KEY" in lo.linkedin_visit_profile.func(profile="bob")


def test_disconnect_deletes_only_what_this_box_connected(unipile) -> None:
    unipile.add_account("solo")  # adopted: the owner's own, never deleted
    lo.linkedin_account_status.func()
    lo.linkedin_disconnect_account.func()
    assert "solo" in unipile.accounts

    lo.linkedin_connect_account.func()
    unipile.add_account("linked", created="2026-10-08T10:00:00Z")
    lo.linkedin_account_status.func()
    lo.linkedin_disconnect_account.func()
    assert "linked" not in unipile.accounts


def test_a_private_dsn_is_refused(unipile, monkeypatch) -> None:
    unipile.creds["UNIPILE_DSN"] = "169.254.169.254"
    out = lo.linkedin_account_status.func()
    assert "UNIPILE_DSN is not usable" in out
    assert unipile.calls == []
