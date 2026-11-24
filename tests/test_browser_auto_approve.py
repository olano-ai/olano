"""Narrow browser auto-approvals (olano/browser/auto_approve.py): what a rule
may cover, which turns it applies to, who may change it, and the surfaces that
change it (the dashboard API, the agent's own tool, AgentFather's tool)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.browser import auto_approve
from olano.browser.consequence import Verdict, action_kind
from olano.config.agent_config import AgentConfig, BrowserAgentConfig
from olano.security.request_context import AGENT_ADMIN_KEY, push_request_context


@pytest.fixture
def agent():
    from olano.config.loader import save_agent_config

    save_agent_config(AgentConfig(name="scout"))
    return "scout"


def _as(name: str):
    from olano.tools.talk_to import current_agent

    return current_agent.set(name)


# ── what an action is ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("Send", "send"),
        ("Send message", "send"),
        ("Reply", "send"),
        ("Post", "send"),
        ("Invite Jane Doe to connect", "send"),
        ("Submit", "submit"),
        ("Save changes", "submit"),
        ("Log in", "submit"),
        ("Sign up", "signup"),
        ("Book now", "signup"),
        ("Subscribe", "signup"),
        # Money or deletion anywhere in the label wins over everything else.
        ("Send payment", "protected"),
        ("Confirm purchase", "protected"),
        ("Place order", "protected"),
        ("Delete", "protected"),
        ("Approve", "protected"),
        ("Merge pull request", "protected"),
        ("Donate", "protected"),
    ],
)
def test_action_kind(label, kind):
    assert action_kind(label) == kind


def test_a_rule_can_never_name_the_protected_kind():
    with pytest.raises(ValueError):
        BrowserAgentConfig.model_validate(
            {"auto_approve": [{"site": "shop.com", "actions": ["protected"]}]}
        )


@pytest.mark.parametrize(
    "site",
    [
        "https://linkedin.com",
        "linkedin.com/feed",
        "linkedin",
        "*",
        "*.com.",
        "linkedin.com:443",
        "",
    ],
)
def test_a_rule_names_a_bare_site(site):
    with pytest.raises(ValueError):
        BrowserAgentConfig.model_validate({"auto_approve": [{"site": site, "actions": ["send"]}]})


def test_rules_are_normalized_and_bounded():
    cfg = BrowserAgentConfig.model_validate(
        {"auto_approve": [{"site": " LinkedIn.com. ", "actions": ["send", "send"]}]}
    )
    assert cfg.auto_approve[0].site == "linkedin.com"
    assert cfg.auto_approve[0].actions == ["send"]
    assert cfg.auto_approve[0].in_background is False
    with pytest.raises(ValueError, match="at least one action"):
        BrowserAgentConfig.model_validate({"auto_approve": [{"site": "a.com", "actions": []}]})
    with pytest.raises(ValueError, match="twice"):
        BrowserAgentConfig.model_validate(
            {
                "auto_approve": [
                    {"site": "a.com", "actions": ["send"]},
                    {"site": "a.com", "actions": ["submit"]},
                ]
            }
        )
    with pytest.raises(ValueError, match="at most 20"):
        BrowserAgentConfig.model_validate(
            {"auto_approve": [{"site": f"s{i}.com", "actions": ["send"]} for i in range(21)]}
        )


@pytest.mark.parametrize(
    "pasted, site",
    [
        ("https://www.LinkedIn.com/feed/", "linkedin.com"),
        ("www.linkedin.com", "linkedin.com"),
        ("linkedin.com:443", "linkedin.com"),
        ("mail.google.com", "mail.google.com"),
        ("www.com", "www.com"),
        ("*.example.com", "*.example.com"),
    ],
)
def test_what_a_person_pastes_becomes_the_site_they_meant(pasted, site):
    assert auto_approve.normalize_site(pasted) == site


def test_site_covers():
    assert auto_approve.site_covers("linkedin.com", "linkedin.com")
    assert auto_approve.site_covers("linkedin.com", "www.linkedin.com")
    assert not auto_approve.site_covers("linkedin.com", "evil-linkedin.com")
    assert not auto_approve.site_covers("linkedin.com", "linkedin.com.evil.io")
    assert auto_approve.site_covers("*.example.com", "app.example.com")
    assert not auto_approve.site_covers("*.example.com", "example.com")


# ── which actions a rule lets through ─────────────────────────────────────────


def _rules(monkeypatch, *rules):
    cfg = BrowserAgentConfig.model_validate({"auto_approve": list(rules)})
    monkeypatch.setattr(auto_approve, "rules_for", lambda agent: list(cfg.auto_approve))


SEND = Verdict(True, "labelled send", "Send", "", "send")


def test_a_matching_rule_lets_the_action_through_with_a_visible_note(monkeypatch):
    _rules(monkeypatch, {"site": "linkedin.com", "actions": ["send"]})
    hit = auto_approve.match("scout", SEND, "https://www.linkedin.com/messaging/")
    assert hit is not None
    assert "linkedin.com" in hit.note() and "Approved automatically" in hit.note()


def test_no_rule_covers_another_site_another_kind_or_a_protected_action(monkeypatch):
    _rules(monkeypatch, {"site": "linkedin.com", "actions": ["send"]})
    assert auto_approve.match("scout", SEND, "https://example.com/") is None
    submit = Verdict(True, "posts a form", "Save", "", "submit")
    assert auto_approve.match("scout", submit, "https://linkedin.com/") is None
    pay = Verdict(True, "labelled pay", "Send payment", "", "protected")
    _rules(monkeypatch, {"site": "linkedin.com", "actions": ["send", "submit", "signup"]})
    assert auto_approve.match("scout", pay, "https://linkedin.com/") is None


def test_a_form_that_sends_its_data_elsewhere_still_asks(monkeypatch):
    _rules(monkeypatch, {"site": "linkedin.com", "actions": ["submit"]})
    away = Verdict(True, "posts a form", "Save", "https://collector.evil.io/x", "submit")
    assert auto_approve.match("scout", away, "https://linkedin.com/settings") is None
    home = Verdict(True, "posts a form", "Save", "https://www.linkedin.com/save", "submit")
    assert auto_approve.match("scout", home, "https://linkedin.com/settings") is not None


@pytest.mark.parametrize("source", ["peer", "mcp", "a2a", "webhook", "gmail"])
def test_a_rule_never_applies_to_a_turn_someone_else_started(monkeypatch, source):
    _rules(monkeypatch, {"site": "linkedin.com", "actions": ["send"], "in_background": True})
    with push_request_context(source=source):
        assert auto_approve.match("scout", SEND, "https://linkedin.com/") is None


def test_background_runs_need_the_rule_to_say_so(monkeypatch):
    _rules(monkeypatch, {"site": "linkedin.com", "actions": ["send"]})
    with push_request_context(source="scheduled"):
        assert auto_approve.match("scout", SEND, "https://linkedin.com/") is None
    _rules(monkeypatch, {"site": "linkedin.com", "actions": ["send"], "in_background": True})
    with push_request_context(source="scheduled"):
        assert auto_approve.match("scout", SEND, "https://linkedin.com/") is not None


def test_a_member_who_does_not_own_the_agent_still_gets_asked(monkeypatch):
    _rules(monkeypatch, {"site": "linkedin.com", "actions": ["send"]})
    with push_request_context(source="api", channel="dashboard", metadata={AGENT_ADMIN_KEY: False}):
        assert auto_approve.match("scout", SEND, "https://linkedin.com/") is None
    with push_request_context(source="api", channel="dashboard", metadata={AGENT_ADMIN_KEY: True}):
        assert auto_approve.match("scout", SEND, "https://linkedin.com/") is not None


def test_rules_are_read_fresh_from_the_agent_config(agent):
    assert auto_approve.rules_for(agent) == []
    auto_approve.save_rules(agent, [{"site": "linkedin.com", "actions": ["send"]}])
    assert [r.site for r in auto_approve.rules_for(agent)] == ["linkedin.com"]
    assert auto_approve.current_rules(agent) == [
        {"site": "linkedin.com", "actions": ["send"], "in_background": False}
    ]


# ── who may change them ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source", ["peer", "mcp", "a2a", "webhook", "gmail", "scheduled", "cognition"]
)
def test_only_a_person_in_a_conversation_may_change_rules(agent, source):
    token = _as(agent)
    try:
        with push_request_context(source=source):
            assert auto_approve.editor_denial(agent)
    finally:
        from olano.tools.talk_to import current_agent

        current_agent.reset(token)


def test_the_owner_may_change_their_agent_and_a_member_may_not(agent):
    from olano.tools.talk_to import current_agent

    token = _as(agent)
    try:
        with push_request_context(
            source="api", channel="dashboard", metadata={AGENT_ADMIN_KEY: True}
        ):
            assert auto_approve.editor_denial(agent) is None
        with push_request_context(
            source="api", channel="dashboard", metadata={AGENT_ADMIN_KEY: False}
        ):
            assert "owner" in str(auto_approve.editor_denial(agent))
    finally:
        current_agent.reset(token)


def test_set_tool_refuses_a_non_owner_and_writes_nothing(agent):
    from olano.tools.browser import browser_set_auto_approvals
    from olano.tools.talk_to import current_agent

    token = _as(agent)
    try:
        with push_request_context(
            source="api", channel="dashboard", metadata={AGENT_ADMIN_KEY: False}
        ):
            out = browser_set_auto_approvals.invoke(
                {"rules": [{"site": "linkedin.com", "actions": ["send"]}]}
            )
    finally:
        current_agent.reset(token)
    assert out.startswith("Not changed")
    assert auto_approve.current_rules(agent) == []


def test_set_tool_needs_a_person_to_confirm(agent):
    """Outside a graph nobody can confirm, so nothing is written."""
    from olano.tools.browser import browser_set_auto_approvals
    from olano.tools.talk_to import current_agent

    token = _as(agent)
    try:
        out = browser_set_auto_approvals.invoke(
            {"rules": [{"site": "linkedin.com", "actions": ["send"]}]}
        )
    finally:
        current_agent.reset(token)
    assert out.startswith("Not done") and "confirm" in out
    assert auto_approve.current_rules(agent) == []


def test_set_tool_writes_once_confirmed_and_get_reads_back(agent, monkeypatch):
    from olano.browser import consequence
    from olano.tools.browser import browser_get_auto_approvals, browser_set_auto_approvals
    from olano.tools.talk_to import current_agent

    asked: list[str] = []
    monkeypatch.setattr(
        consequence, "ask_rule_change", lambda *a, **kw: asked.append(kw["details"]) or None
    )
    token = _as(agent)
    try:
        out = browser_set_auto_approvals.invoke(
            {"rules": [{"site": "https://LinkedIn.com/", "actions": ["send"]}]}
        )
        assert '"changed": true' in out
        assert "linkedin.com" in browser_get_auto_approvals.invoke({})
        # The same rules again is not a change and asks nobody.
        browser_set_auto_approvals.invoke(
            {"rules": [{"site": "linkedin.com", "actions": ["send"]}]}
        )
    finally:
        current_agent.reset(token)
    assert len(asked) == 1 and "linkedin.com" in asked[0]
    assert auto_approve.current_rules(agent)[0]["site"] == "linkedin.com"


def test_admin_tool_checks_the_target_agent(agent, monkeypatch):
    from olano.browser import consequence
    from olano.tools.olano_admin import olano_set_browser_auto_approvals

    monkeypatch.setattr(consequence, "ask_rule_change", lambda *a, **kw: None)
    with push_request_context(source="peer"):
        out = olano_set_browser_auto_approvals.invoke(
            {"name": agent, "rules": [{"site": "linkedin.com", "actions": ["send"]}]}
        )
    assert "refused" in out and auto_approve.current_rules(agent) == []
    with push_request_context(source="api", channel="dashboard"):
        out = olano_set_browser_auto_approvals.invoke(
            {"name": agent, "rules": [{"site": "linkedin.com", "actions": ["send"]}]}
        )
    assert '"changed": true' in out


def test_rule_changes_always_ask_a_person():
    from olano.core.runner import _always_asks_human

    assert _always_asks_human({"name": "browser_set_auto_approvals"})
    assert _always_asks_human({"name": "olano_set_browser_auto_approvals"})


# ── the dashboard API ─────────────────────────────────────────────────────────


@pytest.fixture
def client():
    from olano.runtime.api import create_api

    return TestClient(create_api(MagicMock()))


def test_api_reads_validates_and_saves(client, agent):
    url = f"/api/agents/{agent}/browser/auto-approvals"
    body = client.get(url).json()
    assert body["rules"] == [] and body["can_edit"] is True
    assert set(body["action_kinds"]) == {"send", "submit", "signup"}

    bad = client.put(url, json={"rules": [{"site": "not a site", "actions": ["send"]}]})
    assert bad.status_code == 400 and "site" in bad.json()["detail"]
    nothing = client.put(url, json={"rules": [{"site": "x.com", "actions": []}]})
    assert nothing.status_code == 400 and "action" in nothing.json()["detail"]
    assert client.put(url, json={"rules": "nope"}).status_code == 400

    # A pasted address is accepted as its host name.
    ok = client.put(
        url, json={"rules": [{"site": "https://linkedin.com/feed", "actions": ["send"]}]}
    )
    assert ok.status_code == 200
    assert ok.json()["rules"][0]["site"] == "linkedin.com"
    assert client.get(url).json()["rules"][0]["actions"] == ["send"]
    assert client.get("/api/agents/nobody/browser/auto-approvals").status_code in (403, 404)


def test_api_save_is_for_admins_and_owners(client, agent, monkeypatch):
    from fastapi import HTTPException

    from olano.runtime import auth

    def deny(name):
        raise HTTPException(status_code=403, detail="agent_admin_required")

    monkeypatch.setattr(auth, "assert_agent_admin", deny)
    r = client.put(
        f"/api/agents/{agent}/browser/auto-approvals",
        json={"rules": [{"site": "linkedin.com", "actions": ["send"]}]},
    )
    assert r.status_code == 403
    assert auto_approve.current_rules(agent) == []
