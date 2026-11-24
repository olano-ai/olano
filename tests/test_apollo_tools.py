"""Tests for the Apollo.io toolkit (REST over httpx; Apollo ships no Python SDK)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from olano.tools import apollo_tools as a


class _Recorder:
    """Stands in for ``httpx.request``: records each call, answers from a queue."""

    def __init__(self, *responses: tuple[int, Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses) or [(200, {})]

    def __call__(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"method": method, "url": url, **kw})
        status, payload = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return httpx.Response(status, json=payload, request=httpx.Request(method, url))

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]


@pytest.fixture
def api(monkeypatch):
    """Install a key and a recorder; returns a setter for the canned responses."""
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")

    def install(*responses: tuple[int, Any]) -> _Recorder:
        rec = _Recorder(*responses)
        monkeypatch.setattr(a.httpx, "request", rec)
        return rec

    return install


def _run(name: str, **args: Any) -> str:
    tool = a.get_tool(name)
    assert tool is not None, name
    return str(tool.invoke(args))


# ── Surface + registration ──────────────────────────────────────────────────


def test_toolkit_surface():
    names = {t.name for t in a.get_tools()}
    for expected in (
        "apollo_search_people",
        "apollo_enrich_person",
        "apollo_bulk_enrich_people",
        "apollo_search_organizations",
        "apollo_enrich_organization",
        "apollo_search_contacts",
        "apollo_create_contact",
        "apollo_create_deal",
        "apollo_add_contacts_to_sequence",
        "apollo_create_task",
        "apollo_whoami",
        "apollo_api_request",
    ):
        assert expected in names
    assert all(n.startswith("apollo_") for n in names)


def test_registered_resolvable_and_indexed():
    from olano.tools._name_index import NAME_INDEX
    from olano.tools.registry import _TOOLKITS, resolve_tools
    from olano.tools.requirements import BUNDLE_REQUIREMENTS

    assert _TOOLKITS["apollo"] == "olano.tools.apollo_tools"
    assert NAME_INDEX["apollo_search_people"] == "olano.tools.apollo_tools"
    assert [t.name for t in resolve_tools(["apollo_whoami"])] == ["apollo_whoami"]
    assert BUNDLE_REQUIREMENTS["apollo"]["env_vars"][0]["name"] == "APOLLO_API_KEY"


def test_sequence_enrollment_needs_approval_under_supervised_profiles():
    from olano.security.hitl_policy import CATEGORY_TOOLS

    gated = CATEGORY_TOOLS["write_external"]
    assert "apollo_add_contacts_to_sequence" in gated
    assert "apollo_api_request" in gated
    assert "apollo_search_people" not in gated


def test_apollo_agent_definition_loads():
    from olano.subagents.registry import load_toolkit_agent, resolve_for_toolkit

    agent = load_toolkit_agent("ApolloAgent")
    assert agent.toolkit == "apollo"
    assert resolve_for_toolkit("apollo").name == "ApolloAgent"


# ── Credentials + transport ─────────────────────────────────────────────────


def test_missing_key_is_actionable(monkeypatch):
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(a.httpx, "request", lambda *x, **k: called.append(1))
    out = _run("apollo_whoami")
    assert out.startswith("[missing-credentials]")
    assert 'magic_link_request_secret("APOLLO_API_KEY")' in out
    assert not called


def test_key_rides_the_x_api_key_header(api):
    rec = api((200, {"user": {"id": "u1"}}))
    _run("apollo_whoami")
    call = rec.last
    assert call["method"] == "GET"
    assert call["url"] == "https://api.apollo.io/api/v1/users/api_profile"
    assert call["headers"]["x-api-key"] == "test-key"
    assert call["params"] == {"include_credit_usage": "true"}


@pytest.mark.parametrize(
    ("status", "hint"),
    [
        (401, "magic_link_request_secret"),
        (403, "master key"),
        (429, "rate limit"),
    ],
)
def test_http_errors_carry_the_fix(api, status, hint):
    api((status, {"error": "nope"}))
    out = _run("apollo_search_contacts", keywords="x")
    assert out.startswith("[tool-error]")
    assert f"HTTP {status}" in out and "nope" in out
    assert hint in out


def test_transport_failure_is_a_tool_error(api, monkeypatch):
    def boom(*_a, **_k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(a.httpx, "request", boom)
    assert _run("apollo_whoami").startswith("[tool-error] Apollo GET /users/api_profile")


# ── Prospecting ─────────────────────────────────────────────────────────────


def test_people_search_builds_the_documented_body(api):
    rec = api(
        (
            200,
            {
                "total_entries": 1,
                "people": [
                    {
                        "id": "p1",
                        "first_name": "John",
                        "last_name_obfuscated": "Do***e",
                        "title": "VP of Sales",
                        "has_email": True,
                        "organization": {"name": "Acme"},
                    }
                ],
            },
        )
    )
    out = _run(
        "apollo_search_people",
        titles="VP Sales, Head of Revenue",
        seniorities="vp,director",
        company_domains="acme.com",
        employee_ranges="1,50; 51,200",
        exact_titles=True,
        per_page=500,
    )
    call = rec.last
    assert call["method"] == "POST"
    assert call["url"].endswith("/mixed_people/api_search")
    body = call["json"]
    assert body["person_titles"] == ["VP Sales", "Head of Revenue"]
    assert body["person_seniorities"] == ["vp", "director"]
    assert body["q_organization_domains_list"] == ["acme.com"]
    assert body["organization_num_employees_ranges"] == ["1,50", "51,200"]
    assert body["include_similar_titles"] is False
    assert body["per_page"] == 100  # clamped
    assert "q_keywords" not in body  # unset args are not sent
    assert "John Do***e" in out and "VP of Sales @ Acme" in out and "id=p1" in out
    assert "enrich to reveal" in out


def test_enrich_person_requires_an_identifier(api):
    rec = api()
    out = _run("apollo_enrich_person", domain="acme.com")
    assert out.startswith("[tool-error]")
    assert not rec.calls


def test_enrich_person_phone_reveal_polls(api):
    rec = api(
        (
            200,
            {
                "request_id": 42,
                "person": {
                    "id": "p1",
                    "name": "Jordan Blake",
                    "email": "jordan@acme.com",
                    "employment_history": [{"title": "CEO", "organization_name": "Acme"}],
                },
            },
        )
    )
    out = _run("apollo_enrich_person", person_id="p1", reveal_phone_number=True)
    body = rec.last["json"]
    assert body == {"id": "p1", "reveal_phone_number": True, "poll_only": True}
    assert "jordan@acme.com" in out and "CEO | Acme" in out
    assert "request_id=42" in out and "apollo_get_enrichment_result" in out


def test_enrich_person_miss(api):
    api((200, {"person": None}))
    assert "no matching person" in _run("apollo_enrich_person", email="x@y.z")


def test_bulk_enrich_people_validates_and_caps(api):
    rec = api()
    assert "not valid JSON" in _run("apollo_bulk_enrich_people", people_json="{nope")
    eleven = json.dumps([{"email": f"{i}@x.io"} for i in range(11)])
    assert "at most 10" in _run("apollo_bulk_enrich_people", people_json=eleven)
    assert not rec.calls
    rec = api((200, {"matches": [{"id": "p1", "name": "A B", "email": "a@x.io"}]}))
    out = _run(
        "apollo_bulk_enrich_people", people_json='[{"email": "a@x.io"}, {"email": "b@x.io"}]'
    )
    assert rec.last["json"]["details"][1] == {"email": "b@x.io"}
    assert out.startswith("Matched 1 of 2")


def test_org_search_nests_revenue_range(api):
    rec = api(
        (
            200,
            {
                "pagination": {"page": 1, "total_pages": 3, "total_entries": 60},
                "organizations": [
                    {
                        "id": "o1",
                        "name": "Acme",
                        "primary_domain": "acme.com",
                        "estimated_num_employees": 80,
                    }
                ],
            },
        )
    )
    out = _run("apollo_search_organizations", keyword_tags="fintech", revenue_min=1_000_000)
    body = rec.last["json"]
    assert body["q_organization_keyword_tags"] == ["fintech"]
    assert body["revenue_range"] == {"min": 1_000_000}
    assert "page 1/3, 60 total" in out and "80 employees" in out


def test_enrich_organization_uses_query_params(api):
    rec = api(
        (200, {"organization": {"id": "o1", "name": "Apollo.io", "primary_domain": "apollo.io"}})
    )
    out = _run("apollo_enrich_organization", domain="apollo.io")
    assert rec.last["method"] == "GET"
    assert rec.last["params"] == {"domain": "apollo.io"}
    assert "Organization Apollo.io" in out


# ── Records + engagement ────────────────────────────────────────────────────


def test_create_contact_dedupes_by_default_and_merges_extra(api):
    rec = api((200, {"contact": {"id": "c1", "first_name": "Tim", "last_name": "Zheng"}}))
    out = _run(
        "apollo_create_contact",
        first_name="Tim",
        last_name="Zheng",
        list_names="Q4 targets",
        extra_json='{"present_raw_address": "Atlanta"}',
    )
    body = rec.last["json"]
    assert body["run_dedupe"] is True
    assert body["label_names"] == ["Q4 targets"]
    assert body["present_raw_address"] == "Atlanta"
    assert "id=c1" in out


def test_update_tools_reject_empty_fields(api):
    rec = api()
    for name, key in (
        ("apollo_update_contact", "contact_id"),
        ("apollo_update_account", "account_id"),
        ("apollo_update_deal", "deal_id"),
    ):
        assert "non-empty JSON object" in _run(name, **{key: "x", "fields_json": "{}"})
    assert not rec.calls


def test_update_deal_patches(api):
    rec = api((200, {"opportunity": {"id": "d1"}}))
    out = _run("apollo_update_deal", deal_id="d1", fields_json='{"amount": "500"}')
    assert rec.last["method"] == "PATCH"
    assert rec.last["url"].endswith("/opportunities/d1")
    assert "Updated deal d1 (amount)" in out


def test_list_stages_maps_kind(api):
    rec = api((200, {"opportunity_stages": [{"id": "s1", "name": "Won"}]}))
    out = _run("apollo_list_stages", kind="deals")
    assert rec.last["url"].endswith("/opportunity_stages")
    assert "Won" in out
    assert "kind must be" in _run("apollo_list_stages", kind="widgets")


def test_add_contacts_to_sequence(api):
    rec = api((200, {"contacts": [{"id": "c1"}], "skipped_contact_ids": {"c2": "no_email"}}))
    out = _run(
        "apollo_add_contacts_to_sequence",
        sequence_id="s1",
        contact_ids="c1, c2",
        send_from_email_account_id="m1",
    )
    call = rec.last
    assert call["url"].endswith("/emailer_campaigns/s1/add_contact_ids")
    assert call["json"] == {
        "emailer_campaign_id": "s1",
        "contact_ids": ["c1", "c2"],
        "send_email_from_email_account_id": "m1",
    }
    assert "added 1 of 2 contact(s)" in out and "no_email" in out


def test_update_sequence_contacts_validates_mode(api):
    rec = api()
    assert "mode must be" in _run(
        "apollo_update_sequence_contacts", sequence_ids="s1", contact_ids="c1", mode="pause"
    )
    assert not rec.calls


def test_create_task_validates_type(api):
    rec = api((200, {"task": {"id": "t1", "type": "call", "status": "scheduled"}}))
    bad = _run(
        "apollo_create_task",
        contact_id="c1",
        user_id="u1",
        due_at="2026-11-02T15:00:00Z",
        task_type="fax",
    )
    assert "task_type must be one of" in bad
    out = _run(
        "apollo_create_task",
        contact_id="c1",
        user_id="u1",
        due_at="2026-11-02T15:00:00Z",
        task_type="call",
    )
    assert rec.last["json"]["status"] == "scheduled"
    assert "id=t1" in out


def test_outreach_search_sends_array_query_params(api):
    rec = api((200, {"emailer_messages": []}))
    _run("apollo_search_outreach_emails", sequence_ids="s1,s2", statuses="opened")
    params = rec.last["params"]
    assert params["emailer_campaign_ids[]"] == ["s1", "s2"]
    assert params["emailer_message_stats[]"] == ["opened"]


# ── Escape hatch ────────────────────────────────────────────────────────────


def test_api_request_normalises_path_and_refuses_hosts(api):
    rec = api((200, {"ok": True}))
    out = _run(
        "apollo_api_request", method="post", path="api/v1/phone_calls/search", body_json="{}"
    )
    assert rec.last["url"] == "https://api.apollo.io/api/v1/phone_calls/search"
    assert rec.last["json"] == {}
    assert out.startswith("POST /phone_calls/search OK")
    assert "relative API path" in _run(
        "apollo_api_request", method="GET", path="https://evil.example/x"
    )
    assert "relative API path" in _run("apollo_api_request", method="GET", path="/../x")
    assert "method must be" in _run("apollo_api_request", method="TRACE", path="/x")


# ── Failures come back as text, never as exceptions ─────────────────────────


def _minimal_args(tool: Any) -> dict[str, Any]:
    """Valid-looking values for a tool's required arguments."""
    special = {
        "people_json": '[{"email": "a@x.io"}]',
        "fields_json": '{"title": "CTO"}',
        "method": "GET",
        "path": "/contacts/search",
        "mode": "remove",
        "modality": "contacts",
    }
    args: dict[str, Any] = {}
    schema = tool.args_schema.model_json_schema()
    for name in schema.get("required", []):
        args[name] = special.get(name, "x")
    return args


def _raise(exc: Exception):
    def boom(*_a: Any, **_k: Any) -> Any:
        raise exc

    return boom


_HOSTILE = {
    "json_list": lambda m, u, **k: httpx.Response(
        200, json=[1, "two", None], request=httpx.Request(m, u)
    ),
    "json_string": lambda m, u, **k: httpx.Response(200, json="ok", request=httpx.Request(m, u)),
    "wrong_shapes": lambda m, u, **k: httpx.Response(
        200,
        json={
            "people": "nope",
            "contacts": [1, 2],
            "person": ["x"],
            "organization": 5,
            "pagination": "?",
        },
        request=httpx.Request(m, u),
    ),
    "html_500": lambda m, u, **k: httpx.Response(
        500, text="<html>oops</html>", request=httpx.Request(m, u)
    ),
    "empty_200": lambda m, u, **k: httpx.Response(200, content=b"", request=httpx.Request(m, u)),
    "connect_error": _raise(httpx.ConnectError("refused")),
    "timeout": _raise(httpx.ReadTimeout("slow")),
    "invalid_url": _raise(httpx.InvalidURL("bad")),
    "unexpected": _raise(RuntimeError("kaboom")),
}


@pytest.mark.parametrize("scenario", sorted(_HOSTILE), ids=sorted(_HOSTILE))
def test_every_tool_answers_in_text_whatever_apollo_does(monkeypatch, scenario):
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    monkeypatch.setattr(a.httpx, "request", _HOSTILE[scenario])
    for tool in a.get_tools():
        out = tool.invoke(_minimal_args(tool))  # must not raise
        assert isinstance(out, str) and out.strip(), tool.name
        if scenario in ("html_500", "connect_error", "timeout", "invalid_url", "unexpected"):
            assert out.startswith("[tool-error]"), (tool.name, out)


def test_unexpected_failure_names_the_tool_and_the_next_step(api, monkeypatch):
    monkeypatch.setattr(a, "_person_detail", _raise(KeyError("boom")))
    api((200, {"person": {"id": "p1"}}))
    out = _run("apollo_enrich_person", person_id="p1")
    assert out.startswith("[tool-error] apollo_enrich_person: KeyError")
    assert "before retrying" in out


def test_timeout_message_suggests_a_smaller_page(api, monkeypatch):
    monkeypatch.setattr(a.httpx, "request", _raise(httpx.ReadTimeout("slow")))
    out = _run("apollo_search_people", titles="cto")
    assert "did not answer" in out and "per_page" in out


@pytest.mark.parametrize(
    ("status", "payload", "fragment"),
    [
        (500, "<html>oops</html>", "HTTP 500: <html>oops</html>"),
        (422, {"error": "Insufficient credits"}, "out of credits"),
    ],
)
def test_error_bodies_reach_the_agent(api, monkeypatch, status, payload, fragment):
    def respond(m: str, u: str, **_k: Any) -> httpx.Response:
        if isinstance(payload, str):
            return httpx.Response(status, text=payload, request=httpx.Request(m, u))
        return httpx.Response(status, json=payload, request=httpx.Request(m, u))

    monkeypatch.setattr(a.httpx, "request", respond)
    out = _run("apollo_enrich_organization", domain="acme.com")
    assert out.startswith("[tool-error]") and fragment in out


# ── Review fixes ────────────────────────────────────────────────────────────

# Every tool that puts an id into a URL path, with the arguments that reach it.
_PATH_ID_CALLS = [
    ("apollo_get_contact", {}, "contact_id"),
    ("apollo_update_contact", {"fields_json": '{"title": "x"}'}, "contact_id"),
    ("apollo_get_account", {}, "account_id"),
    ("apollo_update_account", {"fields_json": '{"name": "x"}'}, "account_id"),
    ("apollo_get_deal", {}, "deal_id"),
    ("apollo_update_deal", {"fields_json": '{"amount": "1"}'}, "deal_id"),
    ("apollo_get_organization_job_postings", {}, "organization_id"),
    ("apollo_get_enrichment_result", {}, "request_id"),
    ("apollo_complete_task", {}, "task_id"),
    (
        "apollo_add_contacts_to_sequence",
        {"contact_ids": "c1", "send_from_email_account_id": "m1"},
        "sequence_id",
    ),
]


@pytest.mark.parametrize(
    "bad_id",
    ["../emailer_messages/1/send_now?x=", "a/b", "abc?x=1", "abc#frag", "%2e%2e", ""],
    ids=["traversal", "slash", "query", "fragment", "encoded", "empty"],
)
@pytest.mark.parametrize(
    ("name", "args", "id_arg"), _PATH_ID_CALLS, ids=[c[0] for c in _PATH_ID_CALLS]
)
def test_path_ids_cannot_reach_another_endpoint(api, name, args, id_arg, bad_id):
    """An id is one path segment: completing a task must never become a send-now."""
    rec = api()
    out = _run(name, **{**args, id_arg: bad_id})
    assert out.startswith("[tool-error]") and id_arg in out
    assert not rec.calls


def test_valid_ids_still_reach_their_endpoint(api):
    rec = api((200, {"request_id": -42}))
    _run("apollo_get_enrichment_result", request_id="-1039995589705121900")
    assert rec.last["url"].endswith("/webhook_result/-1039995589705121900")
    _run("apollo_complete_task", task_id=" 66e9e215ece19801b219997f ")
    assert rec.last["url"].endswith("/tasks/66e9e215ece19801b219997f/complete")


def test_enrollment_reports_what_apollo_added_not_what_was_sent(api):
    api(
        (
            200,
            {"contacts": [], "skipped_contact_ids": {"c1": "contact_not_found", "c2": "no_email"}},
        )
    )
    out = _run(
        "apollo_add_contacts_to_sequence",
        sequence_id="s1",
        contact_ids="c1,c2",
        send_from_email_account_id="m1",
    )
    assert "added 0 of 2" in out and "contact_not_found" in out


@pytest.mark.parametrize(
    ("kind", "path"),
    [
        ("contacts", "/contact_stages"),
        ("Account", "/account_stages"),
        ("opportunity", "/opportunity_stages"),
        ("opportunities", "/opportunity_stages"),
    ],
)
def test_stage_kinds_and_aliases(api, kind, path):
    rec = api((200, {}))
    _run("apollo_list_stages", kind=kind)
    assert rec.last["url"].endswith(path)


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("apollo_create_contact", {"first_name": "A", "last_name": "B", "extra_json": "[1]"}),
        ("apollo_create_account", {"name": "A", "extra_json": '"x"'}),
        ("apollo_create_deal", {"name": "A", "extra_json": "[]"}),
        ("apollo_api_request", {"method": "GET", "path": "/labels", "query_json": "[1, 2]"}),
    ],
)
def test_non_object_json_is_refused_not_dropped(api, name, args):
    rec = api()
    out = str(a.get_tool(name).invoke(args))
    assert out.startswith("[tool-error]") and "JSON object" in out
    assert not rec.calls


_PENDING = {
    "error_code": "result_pending",
    "retry_after_seconds": 10,
    "error_details": {
        "code": "WEBHOOK_RESULT.NOT_FOUND.RESULT_PENDING",
        "message": "The enrichment for this request_id is still processing.",
        "suggestions": [{"label": "Wait, then poll again", "retry_after_seconds": 15}],
    },
}


def test_pending_enrichment_is_a_status_not_an_error(api):
    """Apollo's 404 RESULT_PENDING means 'not ready yet'; the agent must not read a failure."""
    api((404, _PENDING))
    out = _run("apollo_get_enrichment_result", request_id="5201603457547956693")
    assert not out.startswith("[tool-error]")
    assert "still processing" in out.lower() and "15s" in out
    out = _run("apollo_api_request", method="GET", path="/webhook_result/5201603457547956693")
    assert not out.startswith("[tool-error]") and "still processing" in out


def test_pending_without_deprecated_fields_still_reads_as_pending(api):
    api((404, {"error_details": {"code": "WEBHOOK_RESULT.NOT_FOUND.RESULT_PENDING"}}))
    out = _run("apollo_get_enrichment_result", request_id="42")
    assert "still processing" in out.lower() and "10s" in out


def test_other_404s_stay_errors(api):
    api((404, {"error": "Record not found"}))
    out = _run("apollo_get_enrichment_result", request_id="42")
    assert out.startswith("[tool-error]") and "HTTP 404" in out
