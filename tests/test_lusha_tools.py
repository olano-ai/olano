"""Tests for the Lusha toolkit (REST over httpx; Lusha ships no Python SDK)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from olano.tools import lusha_tools as lu


class _Recorder:
    """Stands in for ``httpx.request``: records each call, answers from a queue."""

    def __init__(self, *responses: tuple[int, Any] | tuple[int, Any, dict[str, str]]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses) or [(200, {})]

    def __call__(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"method": method, "url": url, **kw})
        spec = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        status, payload = spec[0], spec[1]
        headers = spec[2] if len(spec) > 2 else None
        return httpx.Response(
            status, json=payload, headers=headers, request=httpx.Request(method, url)
        )

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]


@pytest.fixture
def api(monkeypatch):
    """Install a key and a recorder; returns a setter for the canned responses."""
    monkeypatch.setenv("LUSHA_API_KEY", "test-key")

    def install(*responses: Any) -> _Recorder:
        rec = _Recorder(*responses)
        monkeypatch.setattr(lu.httpx, "request", rec)
        return rec

    return install


def _run(tool_name: str, /, **args: Any) -> str:
    tool = lu.get_tool(tool_name)
    assert tool is not None, tool_name
    return str(tool.invoke(args))


# ── Surface + registration ──────────────────────────────────────────────────

# Every v3 operation in Lusha's OpenAPI spec, and the tool that calls it.
_V3_OPERATIONS = {
    ("POST", "/v3/contacts/search"): "lusha_search_contacts",
    ("POST", "/v3/contacts/enrich"): "lusha_enrich_contacts",
    ("POST", "/v3/contacts/search-and-enrich"): "lusha_search_and_enrich_contacts",
    ("GET", "/v3/contacts/jobs/{jobId}"): "lusha_get_enrich_job",
    ("POST", "/v3/companies/search"): "lusha_search_companies",
    ("POST", "/v3/companies/enrich"): "lusha_enrich_companies",
    ("POST", "/v3/companies/search-and-enrich"): "lusha_search_and_enrich_companies",
    ("POST", "/v3/contacts/prospecting"): "lusha_prospect_contacts",
    ("POST", "/v3/companies/prospecting"): "lusha_prospect_companies",
    ("GET", "/v3/contacts/prospecting/filters"): "lusha_get_prospecting_filters",
    ("GET", "/v3/contacts/prospecting/filters/{filterType}"): "lusha_get_prospecting_filters",
    ("GET", "/v3/companies/prospecting/filters"): "lusha_get_prospecting_filters",
    ("GET", "/v3/companies/prospecting/filters/{filterType}"): "lusha_get_prospecting_filters",
    ("POST", "/v3/contacts/lookalike"): "lusha_find_contact_lookalikes",
    ("POST", "/v3/companies/lookalike"): "lusha_find_company_lookalikes",
    ("POST", "/v3/contacts/buying-group"): "lusha_get_buying_group",
    ("POST", "/v3/contacts/signals"): "lusha_get_contact_signals",
    ("POST", "/v3/companies/signals"): "lusha_get_company_signals",
    ("GET", "/v3/contacts/signals/types"): "lusha_get_signal_types",
    ("GET", "/v3/companies/signals/types"): "lusha_get_signal_types",
    ("GET", "/v3/companies/signals/filters"): "lusha_get_company_signal_filters",
    ("GET", "/v3/companies/signals/filters/{filterType}"): "lusha_get_company_signal_filters",
    ("POST", "/v3/companies/signal-score"): "lusha_score_by_signals",
    ("POST", "/v3/contacts/signal-score"): "lusha_score_by_signals",
    ("GET", "/v3/icp/profiles"): "lusha_list_target_profiles",
    ("POST", "/v3/companies/icp-fit-score"): "lusha_score_icp_fit",
    ("POST", "/v3/contacts/icp-fit-score"): "lusha_score_icp_fit",
    ("POST", "/v3/companies/website-visits"): "lusha_get_website_visitors",
    ("POST", "/v3/account/conversations/search"): "lusha_search_conversations",
    (
        "GET",
        "/v3/account/conversations/{conversationId}/transcript",
    ): "lusha_get_conversation_transcript",
    ("GET", "/v3/account/usage"): "lusha_get_account_usage",
    ("POST", "/api/subscriptions"): "lusha_create_webhook_subscriptions",
    ("GET", "/api/subscriptions"): "lusha_list_webhook_subscriptions",
    ("GET", "/api/subscriptions/{id}"): "lusha_get_webhook_subscription",
    ("PATCH", "/api/subscriptions/{id}"): "lusha_update_webhook_subscription",
    ("POST", "/api/subscriptions/{id}/test"): "lusha_test_webhook_subscription",
    ("POST", "/api/subscriptions/delete"): "lusha_delete_webhook_subscriptions",
    ("POST", "/api/subscriptions/opt-out"): "lusha_create_opt_out_subscription",
    ("GET", "/api/audit-logs"): "lusha_get_webhook_delivery_logs",
    ("GET", "/api/audit-logs/stats"): "lusha_get_webhook_delivery_stats",
    ("GET", "/api/account/secret"): "lusha_get_webhook_secret",
    ("POST", "/api/account/secret/regenerate"): "lusha_regenerate_webhook_secret",
}
# Contacts and companies tables share one tool per operation (``kind``).
_TABLE_OPERATIONS = {
    ("POST", "/tables"): "lusha_create_table",
    ("POST", "/tables/list"): "lusha_list_tables",
    ("GET", "/tables/columns"): "lusha_get_table_column_catalog",
    ("GET", "/tables/{table_id}"): "lusha_get_table",
    ("PATCH", "/tables/{table_id}"): "lusha_update_table",
    ("DELETE", "/tables/{table_id}"): "lusha_delete_table",
    ("GET", "/tables/{table_id}/entities"): "lusha_get_table_rows",
    ("POST", "/tables/{table_id}/entities"): "lusha_add_table_rows",
    ("DELETE", "/tables/{table_id}/entities"): "lusha_remove_table_rows",
    ("GET", "/tables/{table_id}/columns"): "lusha_list_table_columns",
    ("POST", "/tables/{table_id}/columns"): "lusha_add_table_columns",
    ("DELETE", "/tables/{table_id}/columns/{column_id}"): "lusha_remove_table_column",
    ("POST", "/tables/{table_id}/columns/{column_id}/run"): "lusha_run_table_column",
}
for (_verb, _tail), _name in _TABLE_OPERATIONS.items():
    for _kind in ("contacts", "companies"):
        _V3_OPERATIONS[(_verb, f"/v3/{_kind}{_tail}")] = _name


def test_every_v3_operation_has_a_tool():
    names = {t.name for t in lu.get_tools()}
    assert len(_V3_OPERATIONS) == 68  # the operation count of Lusha's v3 spec (October 2026)
    assert set(_V3_OPERATIONS.values()) | {"lusha_api_request"} == names
    assert all(n.startswith("lusha_") for n in names)


def test_registered_resolvable_and_indexed():
    from olano.tools._name_index import NAME_INDEX
    from olano.tools.registry import _TOOLKITS, resolve_tools
    from olano.tools.requirements import BUNDLE_REQUIREMENTS

    assert _TOOLKITS["lusha"] == "olano.tools.lusha_tools"
    for tool in lu.get_tools():
        assert NAME_INDEX[tool.name] == "olano.tools.lusha_tools"
    assert [t.name for t in resolve_tools(["lusha_get_account_usage"])] == [
        "lusha_get_account_usage"
    ]
    assert BUNDLE_REQUIREMENTS["lusha"]["env_vars"][0]["name"] == "LUSHA_API_KEY"


def test_approval_categories():
    from olano.security.hitl_policy import CATEGORY_TOOLS

    gated = CATEGORY_TOOLS["write_external"]
    for name in (
        "lusha_create_webhook_subscriptions",
        "lusha_update_webhook_subscription",
        "lusha_create_opt_out_subscription",
        "lusha_delete_webhook_subscriptions",
        "lusha_regenerate_webhook_secret",
        "lusha_delete_table",
        "lusha_remove_table_column",
        "lusha_api_request",
    ):
        assert name in gated, name
    assert "lusha_get_webhook_secret" in CATEGORY_TOOLS["secret_read"]
    for free in ("lusha_enrich_contacts", "lusha_prospect_contacts", "lusha_add_table_rows"):
        assert all(free not in tools for tools in CATEGORY_TOOLS.values()), free


def test_lusha_agent_definition_loads():
    from olano.subagents.registry import load_toolkit_agent, resolve_for_toolkit

    agent = load_toolkit_agent("LushaAgent")
    assert agent.toolkit == "lusha"
    assert resolve_for_toolkit("lusha").name == "LushaAgent"


# ── Credentials + transport ─────────────────────────────────────────────────


def test_missing_key_is_actionable(monkeypatch):
    monkeypatch.delenv("LUSHA_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(lu.httpx, "request", lambda *x, **k: called.append(1))
    out = _run("lusha_get_account_usage")
    assert out.startswith("[missing-credentials]")
    assert 'magic_link_request_secret("LUSHA_API_KEY")' in out
    assert not called


def test_key_rides_the_api_key_header(api):
    rec = api((200, {"credits": {"total": 100, "used": 10, "remaining": 90}}))
    out = _run("lusha_get_account_usage")
    call = rec.last
    assert call["method"] == "GET"
    assert call["url"] == "https://api.lusha.com/v3/account/usage"
    assert call["headers"]["api_key"] == "test-key"
    assert '"remaining": 90' in out


@pytest.mark.parametrize(
    ("status", "hint"),
    [
        (401, "magic_link_request_secret"),
        (402, "out of credits"),
        (403, "v3 API access"),
        (429, "rate limit"),
        (451, "GDPR"),
        (503, "Lusha's side"),
    ],
)
def test_http_errors_carry_the_fix(api, status, hint):
    api((status, {"statusCode": status, "message": "nope"}))
    out = _run("lusha_search_companies", domain="acme.com")
    assert out.startswith("[tool-error]")
    assert f"HTTP {status}" in out and "nope" in out
    assert hint in out


def test_rate_limit_names_the_remaining_quota(api):
    api(
        (
            429,
            {"statusCode": 429, "message": "Too many requests"},
            {"x-minute-requests-left": "0", "x-rate-limit-minute": "25"},
        )
    )
    out = _run("lusha_get_account_usage")
    assert "minute 0/25" in out


def test_transport_failure_is_a_tool_error(monkeypatch):
    monkeypatch.setenv("LUSHA_API_KEY", "k")

    def boom(*_a, **_k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(lu.httpx, "request", boom)
    assert _run("lusha_get_account_usage").startswith("[tool-error] Lusha GET /v3/account/usage")


# ── Search and enrich ───────────────────────────────────────────────────────


def test_search_contacts_single_record_and_preview(api):
    rec = api(
        (
            200,
            {
                "results": [
                    {
                        "id": "4415824633",
                        "firstName": "Orit",
                        "lastName": "Shilvock",
                        "jobTitle": {"title": "VP Partnerships", "seniority": "Vice President"},
                        "company": {"name": "Lusha", "domain": "lusha.com"},
                        "location": {"country": "Israel", "city": "Tel Aviv"},
                        "has": ["emails"],
                        "canReveal": [{"field": "emails", "credits": 1}],
                    }
                ],
                "billing": {"creditsCharged": 1, "resultsReturned": 1},
            },
        )
    )
    out = _run(
        "lusha_search_contacts",
        first_name="Orit",
        last_name="Shilvock",
        company_domain="lusha.com",
        signal_types="promotion",
    )
    body = rec.last["json"]
    assert rec.last["url"].endswith("/v3/contacts/search")
    assert body["contacts"] == [
        {"firstName": "Orit", "lastName": "Shilvock", "companyDomain": "lusha.com"}
    ]
    assert body["signals"] == {"types": ["promotion"]}
    assert "VP Partnerships @ Lusha lusha.com" in out
    assert "can reveal: emails (1 cr)" in out and "id=4415824633" in out
    assert "Credits charged: 1." in out


def test_search_contacts_batch_validates(api):
    rec = api()
    assert "not valid JSON" in _run("lusha_search_contacts", contacts_json="[nope")
    assert "JSON object" in _run("lusha_search_contacts", contacts_json="[1, 2]")
    too_many = json.dumps([{"email": f"{i}@x.io"} for i in range(101)])
    assert "at most 100" in _run("lusha_search_contacts", contacts_json=too_many)
    assert "Give contacts_json" in _run("lusha_search_contacts")
    assert not rec.calls
    rec = api((200, {"results": []}))
    _run("lusha_search_contacts", contacts_json='[{"email": "a@x.io"}]', email="b@x.io")
    assert rec.last["json"]["contacts"] == [{"email": "a@x.io"}, {"email": "b@x.io"}]


def test_enrich_contacts_reports_emails_phones_and_the_async_job(api):
    rec = api(
        (
            200,
            {
                "results": [
                    {
                        "id": "c1",
                        "fullName": "Daniel Turgeman",
                        "emails": [
                            {"email": "daniel@lusha.com", "type": "work", "confidence": "A+"}
                        ],
                        "phones": [{"number": "+1415", "type": "mobile", "doNotCall": True}],
                    }
                ],
                "status": "partial",
                "job": {"id": "wf_123", "status": "processing", "retryAfter": 7},
                "billing": {"creditsCharged": 2},
            },
        )
    )
    out = _run("lusha_enrich_contacts", contact_ids="c1, c2", reveal="Emails,phones")
    assert rec.last["json"] == {"ids": ["c1", "c2"], "reveal": ["emails", "phones"]}
    assert "daniel@lusha.com work, A+" in out and "+1415 mobile, do-not-call" in out
    assert "job_id=wf_123" in out and "7s" in out and "lusha_get_enrich_job" in out
    assert "Waterfall status: partial" in out


def test_enrich_contacts_validates_reveal_and_can_skip_the_waterfall(api):
    rec = api((200, {"results": []}))
    assert "reveal takes" in _run("lusha_enrich_contacts", contact_ids="c1", reveal="faxes")
    assert not rec.calls
    _run("lusha_enrich_contacts", contact_ids="c1", waterfall_enabled=False, table_id="t1")
    assert rec.last["json"] == {"ids": ["c1"], "tableId": "t1", "waterfallEnabled": False}


def test_enrich_job_states(api):
    api(
        (
            200,
            {
                "status": "processing",
                "retryAfter": 5,
                "covers": [{"contactId": "c1", "dataPoints": ["phone"]}],
            },
        )
    )
    out = _run("lusha_get_enrich_job", job_id="wf_1")
    assert "still processing" in out and "c1: phone" in out and not out.startswith("[tool-error]")
    api(
        (
            200,
            {
                "status": "complete",
                "contacts": [{"id": "c1", "phones": [{"number": "+1"}]}],
                "creditsCharged": 8,
            },
        )
    )
    out = _run("lusha_get_enrich_job", job_id="wf_1")
    assert "complete" in out and "+1" in out and "Credits charged by the job: 8" in out
    api((404, {"statusCode": 404, "message": "Not found"}))
    out = _run("lusha_get_enrich_job", job_id="wf_1")
    assert "Stop polling" in out and not out.startswith("[tool-error]")


def test_company_search_and_enrich(api):
    rec = api(
        (
            200,
            {
                "results": [
                    {
                        "id": "16303253",
                        "name": "Lusha",
                        "domain": "lusha.com",
                        "employeeCount": {"exact": 364},
                        "canReveal": [{"field": "competitors", "credits": 1}],
                    }
                ]
            },
        )
    )
    out = _run("lusha_search_companies", companies_json='[{"domain": "lusha.com"}]')
    assert rec.last["json"] == {"companies": [{"domain": "lusha.com"}]}
    assert "364 employees" in out and "competitors (1 cr)" in out
    rec = api((200, {"results": [{"id": "1", "name": "Lusha", "technologies": list(range(50))}]}))
    out = _run("lusha_enrich_companies", company_ids="1", reveal="competitors, directparent")
    assert rec.last["json"]["reveal"] == ["competitors", "directParent"]
    assert "Company Lusha" in out
    assert "not one of" in _run("lusha_enrich_companies", company_ids="1", reveal="gossip")


# ── Prospecting, lookalikes, buying groups ──────────────────────────────────


def test_prospect_contacts_builds_filters_and_merges_json(api):
    rec = api((200, {"results": [], "pagination": {"page": 0, "total": 0}}))
    _run(
        "lusha_prospect_contacts",
        job_titles="VP Sales, CRO",
        seniority_ids="4, 5",
        countries="United States",
        company_sizes="51-200; 1000-",
        company_industries="Software",
        filters_json='{"companies": {"exclude": {"domains": ["rival.com"]}}}',
        max_contacts_per_company=50,
        page=-3,
        page_size=500,
    )
    body = rec.last["json"]
    assert rec.last["url"].endswith("/v3/contacts/prospecting")
    assert body["pagination"] == {"page": 0, "size": 100}
    people = body["filters"]["contacts"]["include"]
    assert people["jobTitles"] == ["VP Sales", "CRO"]
    assert people["seniorityIds"] == [4, 5]
    assert people["locations"] == [{"country": "United States"}]
    companies = body["filters"]["companies"]
    assert companies["include"]["sizes"] == [{"min": 51, "max": 200}, {"min": 1000}]
    assert companies["include"]["industriesLabels"] == ["Software"]
    assert companies["exclude"] == {"domains": ["rival.com"]}
    assert body["options"] == {"maxContactsPerCompany": 20}


def test_prospecting_needs_a_filter(api):
    rec = api()
    assert "at least one filter" in _run("lusha_prospect_contacts")
    assert "at least one company filter" in _run("lusha_prospect_companies")
    assert not rec.calls


def test_prospect_companies_signals_and_search_text(api):
    rec = api(
        (200, {"results": [{"id": "1", "name": "Acme"}], "pagination": {"page": 2, "total": 90}})
    )
    out = _run(
        "lusha_prospect_companies",
        search_text="payments",
        technologies="Stripe",
        signal_types="surgeInHiring",
        signals_start_date="2026-09-01",
        page=2,
    )
    include = rec.last["json"]["filters"]["companies"]["include"]
    assert include["searchText"] == "payments"
    assert include["technologies"] == ["Stripe"]
    assert include["signals"] == {"types": ["surgeInHiring"], "startDate": "2026-09-01"}
    assert "page 2, 90 total" in out


def test_prospecting_filters_discovery_and_values(api):
    rec = api((200, {"availableFilters": [{"filterType": "locations", "requiresQuery": True}]}))
    out = _run("lusha_get_prospecting_filters", kind="contact")
    assert rec.last["url"].endswith("/v3/contacts/prospecting/filters")
    assert "locations | needs query" in out
    rec = api((200, {"values": [{"id": 4, "value": "VP"}]}))
    _run(
        "lusha_get_prospecting_filters", kind="Companies", filter_type="technologies", query="sales"
    )
    assert rec.last["url"].endswith("/v3/companies/prospecting/filters/technologies")
    assert rec.last["params"] == {"query": "sales"}
    assert "must be one of" in _run(
        "lusha_get_prospecting_filters", kind="contacts", filter_type="sics"
    )
    assert "kind must be" in _run("lusha_get_prospecting_filters", kind="widgets")


def test_lookalikes_need_five_to_a_hundred_seeds(api):
    rec = api()
    assert "5 to 100 seeds" in _run("lusha_find_company_lookalikes", seed_domains="a.com, b.com")
    assert not rec.calls
    rec = api(
        (
            200,
            {
                "dedupeSessionId": "s-1",
                "results": [{"id": "9", "name": "Hilton"}],
                "meta": {"hasMore": True},
            },
        )
    )
    out = _run(
        "lusha_find_company_lookalikes",
        seed_domains="a.com,b.com,c.com,d.com,e.com",
        exclude_domains="mine.com",
    )
    body = rec.last["json"]
    assert body["seeds"] == {"domains": ["a.com", "b.com", "c.com", "d.com", "e.com"]}
    assert body["exclude"] == {"domains": ["mine.com"]}
    assert "session_id=s-1" in out and "more" in out


def test_contact_lookalikes_accept_named_seeds(api):
    rec = api((200, {"results": []}))
    named = json.dumps(
        [{"firstName": f"A{i}", "lastName": "B", "companyDomain": "x.io"} for i in range(4)]
    )
    _run("lusha_find_contact_lookalikes", seed_contacts_json=named, seed_emails="z@x.io")
    assert len(rec.last["json"]["seeds"]["contacts"]) == 4
    assert "exclude" not in rec.last["json"]


def test_buying_group_validates_and_groups(api):
    rec = api()
    assert "personas must be" in _run(
        "lusha_get_buying_group", company_domains="a.com", personas="boss"
    )
    many = ",".join(f"d{i}.com" for i in range(26))
    assert "at most 25" in _run("lusha_get_buying_group", company_domains=many)
    assert not rec.calls
    rec = api(
        (
            200,
            {
                "results": [
                    {
                        "companyId": "1",
                        "contacts": [
                            {
                                "id": "c1",
                                "firstName": "Ann",
                                "roles": ["decision_maker"],
                                "score": 0.9,
                            }
                        ],
                    }
                ]
            },
        )
    )
    out = _run("lusha_get_buying_group", company_domains="acme.com", personas="decision_maker")
    assert rec.last["json"]["companies"] == [{"domain": "acme.com"}]
    assert "Company 1:" in out and "roles: decision_maker" in out and "score=0.9" in out


# ── Signals and scores ──────────────────────────────────────────────────────


def test_company_signals_body_and_output(api):
    rec = api(
        (
            200,
            {
                "results": [
                    {
                        "id": "1",
                        "companyName": "Acme",
                        "surgeInHiring": [{"signalDate": "2026-10-01"}],
                        "riskNews": [],
                    }
                ],
                "startDate": "2026-09-01",
                "endDate": "2026-10-18",
            },
        )
    )
    out = _run(
        "lusha_get_company_signals",
        company_ids="1",
        signal_types="surgeInHiring, newIntentTopic",
        intent_topics="Cloud Security",
        max_results_per_signal=500,
    )
    body = rec.last["json"]
    assert body["signalTypes"] == ["surgeInHiring", "newIntentTopic"]
    assert body["maxResultsPerSignal"] == 100
    assert body["filters"] == {"include": {"intentTopics": ["Cloud Security"]}}
    assert "surgeInHiring (1)" in out and "riskNews" not in out


def test_scores_route_by_kind(api):
    rec = api(
        (200, {"results": [{"name": "Acme", "icpFitScore": 0.92, "topProfile": {"name": "SaaS"}}]})
    )
    out = _run(
        "lusha_score_icp_fit",
        kind="company",
        target_profile_ids="p1",
        records_json='[{"domain": "acme.com"}]',
    )
    assert rec.last["url"].endswith("/v3/companies/icp-fit-score")
    assert rec.last["json"] == {"targetProfileIds": ["p1"], "companies": [{"domain": "acme.com"}]}
    assert "icpFitScore=0.92" in out and "best fit: SaaS" in out
    assert "1 to 3" in _run(
        "lusha_score_icp_fit", kind="contacts", target_profile_ids="a,b,c,d", records_json="[{}]"
    )
    _run("lusha_score_by_signals", kind="contacts", records_json='[{"email": "a@x.io"}]')
    assert rec.last["url"].endswith("/v3/contacts/signal-score")
    assert rec.last["json"] == {"contacts": [{"email": "a@x.io"}]}


# ── Tables ──────────────────────────────────────────────────────────────────


def test_table_tools_need_an_owner_email(api):
    rec = api()
    out = _run("lusha_list_tables", kind="contacts", owner_email="")
    assert out.startswith("[tool-error] owner_email is required")
    assert not rec.calls


def test_table_routes_carry_the_owner(api):
    rec = api((201, {"data": {"tableId": "482910", "name": "Q4", "entityCount": 0}}))
    out = _run("lusha_create_table", kind="companies", name="Q4", owner_email="me@acme.com")
    assert rec.last["url"].endswith("/v3/companies/tables")
    assert rec.last["json"] == {
        "name": "Q4",
        "visibility": "private",
        "owner": {"email": "me@acme.com"},
    }
    assert "id=482910" in out
    _run(
        "lusha_get_table_rows",
        kind="contacts",
        table_id="482910",
        owner_email="me@acme.com",
        page=1,
    )
    assert rec.last["method"] == "GET"
    assert rec.last["url"].endswith("/v3/contacts/tables/482910/entities")
    assert rec.last["params"] == {"email": "me@acme.com", "page": 1, "size": 25}
    _run(
        "lusha_remove_table_rows",
        kind="contacts",
        table_id="482910",
        owner_email="me@acme.com",
        ids="a,b",
    )
    assert rec.last["method"] == "DELETE"
    assert rec.last["json"] == {"entityIds": ["a", "b"], "owner": {"email": "me@acme.com"}}


def test_table_rows_read_as_cells(api):
    api(
        (
            200,
            {
                "data": [
                    {
                        "id": "v1.x",
                        "columns": [
                            {"name": "Job title", "value": "CTO", "status": "success"},
                            {"name": "Email", "value": None, "status": "not_run"},
                        ],
                    }
                ],
                "pagination": {"page": 0, "total": 1},
                "billing": {"creditsCharged": 1},
            },
        )
    )
    out = _run("lusha_get_table_rows", kind="contacts", table_id="1", owner_email="me@acme.com")
    assert "id=v1.x | Job title: CTO | Email: (not_run)" in out and "Credits charged: 1." in out


def test_columns_add_and_run(api):
    rec = api((200, {"data": [{"columnKey": "jobTitle", "status": "added", "columnId": "c_1"}]}))
    _run(
        "lusha_add_table_columns",
        kind="contacts",
        table_id="1",
        owner_email="me@acme.com",
        column_keys="jobTitle, seniority",
    )
    assert rec.last["json"]["columns"] == [{"columnKey": "jobTitle"}, {"columnKey": "seniority"}]
    assert "needs the row ids" in _run(
        "lusha_run_table_column",
        kind="contacts",
        table_id="1",
        column_id="c_1",
        owner_email="me@acme.com",
        run_scope="specific",
    )
    _run(
        "lusha_run_table_column",
        kind="contacts",
        table_id="1",
        column_id="c_1",
        owner_email="me@acme.com",
    )
    assert rec.last["url"].endswith("/v3/contacts/tables/1/columns/c_1/run")
    assert rec.last["json"] == {"runScope": "missing", "owner": {"email": "me@acme.com"}}


def test_update_table_archive_flag(api):
    rec = api((200, {"data": {}}))
    _run(
        "lusha_update_table",
        kind="contacts",
        table_id="1",
        owner_email="me@acme.com",
        archived="true",
    )
    assert rec.last["method"] == "PATCH"
    assert rec.last["json"] == {"owner": {"email": "me@acme.com"}, "archived": True}
    assert "archived must be" in _run(
        "lusha_update_table",
        kind="contacts",
        table_id="1",
        owner_email="me@acme.com",
        archived="maybe",
    )


# ── Webhooks ────────────────────────────────────────────────────────────────


def test_webhook_subscriptions_need_https_and_build_the_batch(api):
    rec = api((201, {"total": 2, "successful": 2, "failed": 0, "results": []}))
    assert "https://" in _run(
        "lusha_create_webhook_subscriptions", url="http://hook.example", entity_ids="1"
    )
    assert not rec.calls
    out = _run(
        "lusha_create_webhook_subscriptions",
        url="https://hook.example/lusha",
        entity_ids="1, 2",
        entity_type="company",
        signal_types="surgeInHiring",
    )
    assert rec.last["json"] == {
        "defaults": {
            "url": "https://hook.example/lusha",
            "entityType": "company",
            "signalTypes": ["surgeInHiring"],
        },
        "subscriptions": [{"entityId": "1"}, {"entityId": "2"}],
    }
    assert "2 of 2 succeeded" in out


def test_update_subscription_requires_a_change(api):
    rec = api((200, {}))
    assert "Nothing to change" in _run("lusha_update_webhook_subscription", subscription_id="s1")
    assert not rec.calls
    _run("lusha_update_webhook_subscription", subscription_id="s1", active="true")
    assert rec.last["json"] == {"isActive": True}


def test_test_subscription_passes_the_mode(api):
    rec = api((200, {"isSuccess": True, "testPayload": {"big": "x"}}))
    out = _run(
        "lusha_test_webhook_subscription", subscription_id="507f1f77bcf86cd799439011", mode="direct"
    )
    assert rec.last["params"] == {"mode": "direct"}
    assert "Test succeeded" in out and "testPayload" not in out


# ── Escape hatch ────────────────────────────────────────────────────────────


def test_api_request_reaches_v2_and_refuses_hosts(api):
    rec = api((200, {"data": {"firstName": "A"}}))
    out = _run(
        "lusha_api_request",
        method="get",
        path="v2/person",
        query_json='{"email": "a@x.io"}',
    )
    assert rec.last["url"] == "https://api.lusha.com/v2/person"
    assert rec.last["params"] == {"email": "a@x.io"}
    assert rec.last["json"] is None
    assert out.startswith("GET /v2/person OK")
    for bad in ("https://evil.example/x", "/v3/../api/account/secret", "/v2/person?email=x"):
        assert "relative API path" in _run("lusha_api_request", method="GET", path=bad)
    assert "method must be" in _run("lusha_api_request", method="TRACE", path="/x")
    assert "JSON object" in _run("lusha_api_request", method="GET", path="/x", query_json="[1]")


# ── Failures come back as text, never as exceptions ─────────────────────────


def _minimal_args(tool: Any) -> dict[str, Any]:
    """Valid-looking values for a tool's required arguments."""
    special = {
        "kind": "contacts",
        "owner_email": "me@acme.com",
        "records_json": '[{"email": "a@x.io"}]',
        "target_profile_ids": "p1",
        "url": "https://hook.example",
        "method": "GET",
        "path": "/v3/account/usage",
        "start_date": "2026-01-01",
        "end_date": "2026-02-01",
    }
    args: dict[str, Any] = {}
    schema = tool.args_schema.model_json_schema()
    for name in schema.get("required", []):
        args[name] = special.get(name, "x")
    if tool.name == "lusha_search_contacts" or tool.name == "lusha_search_and_enrich_contacts":
        args["email"] = "a@x.io"
    if tool.name in ("lusha_search_companies", "lusha_search_and_enrich_companies"):
        args["domain"] = "acme.com"
    if tool.name == "lusha_prospect_contacts":
        args["job_titles"] = "CTO"
    if tool.name == "lusha_prospect_companies":
        args["countries"] = "France"
    if tool.name == "lusha_find_company_lookalikes":
        args["seed_domains"] = "a.io,b.io,c.io,d.io,e.io"
    if tool.name == "lusha_find_contact_lookalikes":
        args["seed_emails"] = "a@x.io,b@x.io,c@x.io,d@x.io,e@x.io"
    if tool.name == "lusha_get_buying_group":
        args["company_domains"] = "acme.com"
    if tool.name == "lusha_update_webhook_subscription":
        args["name"] = "renamed"
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
            "results": "nope",
            "data": [1, {"columns": "x"}],
            "contacts": {"a": 1},
            "job": ["x"],
            "billing": 5,
            "pagination": "?",
            "segments": [None, {"text": 3}],
            "conversations": [{"summary": "flat"}],
            "values": {"a": 1},
            "status": "complete",
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
def test_every_tool_answers_in_text_whatever_lusha_does(monkeypatch, scenario):
    monkeypatch.setenv("LUSHA_API_KEY", "test-key")
    calls: list[str] = []

    def respond(m: str, u: str, **k: Any) -> httpx.Response:
        calls.append(u)
        return _HOSTILE[scenario](m, u, **k)

    monkeypatch.setattr(lu.httpx, "request", respond)
    for tool in lu.get_tools():
        before = len(calls)
        out = tool.invoke(_minimal_args(tool))  # must not raise
        assert isinstance(out, str) and out.strip(), tool.name
        assert len(calls) > before, (tool.name, out)  # the minimal args reach Lusha
        if scenario in ("html_500", "connect_error", "timeout", "invalid_url", "unexpected"):
            assert out.startswith("[tool-error]") or "Stop polling" in out, (tool.name, out)


def test_unexpected_failure_names_the_tool_and_the_next_step(api, monkeypatch):
    monkeypatch.setattr(lu, "_enriched_contact", _raise(KeyError("boom")))
    api((200, {"results": [{"id": "c1"}]}))
    out = _run("lusha_enrich_contacts", contact_ids="c1")
    assert out.startswith("[tool-error] lusha_enrich_contacts: KeyError")
    assert "before retrying" in out


def test_bad_integer_lists_are_reported(api):
    rec = api()
    out = _run("lusha_prospect_contacts", seniority_ids="vp")
    assert out.startswith("[tool-error] lusha_prospect_contacts: ValueError")
    assert not rec.calls


# ── Ids in URL paths ────────────────────────────────────────────────────────

_OWNER = {"kind": "contacts", "owner_email": "me@acme.com"}
_PATH_ID_CALLS = [
    ("lusha_get_enrich_job", {}, "job_id"),
    ("lusha_get_conversation_transcript", {}, "conversation_id"),
    ("lusha_get_table", _OWNER, "table_id"),
    ("lusha_update_table", {**_OWNER, "name": "x"}, "table_id"),
    ("lusha_delete_table", _OWNER, "table_id"),
    ("lusha_get_table_rows", _OWNER, "table_id"),
    ("lusha_add_table_rows", {**_OWNER, "ids": "1"}, "table_id"),
    ("lusha_remove_table_rows", {**_OWNER, "ids": "1"}, "table_id"),
    ("lusha_list_table_columns", _OWNER, "table_id"),
    ("lusha_add_table_columns", {**_OWNER, "column_keys": "jobTitle"}, "table_id"),
    ("lusha_remove_table_column", {**_OWNER, "table_id": "1"}, "column_id"),
    ("lusha_run_table_column", {**_OWNER, "table_id": "1"}, "column_id"),
    ("lusha_get_webhook_subscription", {}, "subscription_id"),
    ("lusha_update_webhook_subscription", {"name": "x"}, "subscription_id"),
    ("lusha_test_webhook_subscription", {}, "subscription_id"),
]


@pytest.mark.parametrize(
    "bad_id",
    ["../../api/account/secret/regenerate", "a/b", "abc?x=1", "abc#frag", "%2e%2e", "..", ""],
    ids=["traversal", "slash", "query", "fragment", "encoded", "dotdot", "empty"],
)
@pytest.mark.parametrize(
    ("name", "args", "id_arg"), _PATH_ID_CALLS, ids=[c[0] for c in _PATH_ID_CALLS]
)
def test_path_ids_cannot_reach_another_endpoint(api, name, args, id_arg, bad_id):
    """An id is one path segment: reading a table must never become a secret rotation."""
    rec = api()
    full = {"table_id": "1", "column_id": "c_1", **args, id_arg: bad_id}
    tool = lu.get_tool(name)
    assert tool is not None
    accepted = set(tool.args_schema.model_json_schema()["properties"])
    out = str(tool.invoke({k: v for k, v in full.items() if k in accepted}))
    assert out.startswith("[tool-error]") and id_arg in out
    assert not rec.calls


def test_valid_ids_still_reach_their_endpoint(api):
    rec = api((200, {"status": "processing"}))
    _run("lusha_get_enrich_job", job_id=" wf_3f2b9c1e-4a7d-4f3b-9c2e-1d6a5b8c7e90 ")
    assert rec.last["url"].endswith("/v3/contacts/jobs/wf_3f2b9c1e-4a7d-4f3b-9c2e-1d6a5b8c7e90")
    _run(
        "lusha_remove_table_column",
        kind="companies",
        table_id="583021",
        column_id="c_0ce4d3",
        owner_email="me@acme.com",
    )
    assert rec.last["url"].endswith("/v3/companies/tables/583021/columns/c_0ce4d3")
    assert rec.last["params"] == {"email": "me@acme.com"}
