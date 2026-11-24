"""Tests for the HubSpot CRM toolkit and HubSpotAgent subagent."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from olano.tools import hubspot_tools as h

# ── Toolkit surface ─────────────────────────────────────────────────────────


class TestToolkitSurface:
    def test_get_tools_returns_all(self):
        names = {t.name for t in h.get_tools()}
        # Convenience + generic + activity/association/pipeline tools.
        for expected in (
            "hubspot_list_contacts",
            "hubspot_create_contact",
            "hubspot_update_contact",
            "hubspot_search_contacts",
            "hubspot_get_company",
            "hubspot_create_company",
            "hubspot_list_deals",
            "hubspot_create_deal",
            "hubspot_update_deal",
            "hubspot_create_ticket",
            "hubspot_create_product",
            "hubspot_create_line_item",
            "hubspot_log_activity",
            "hubspot_associate",
            "hubspot_get_pipeline",
            "hubspot_list_owners",
            "hubspot_list_objects",
            "hubspot_get_object",
            "hubspot_search",
            "hubspot_create_object",
            "hubspot_update_object",
        ):
            assert expected in names, f"missing tool {expected}"

    def test_get_tool_by_name(self):
        assert h.get_tool("hubspot_create_deal") is not None
        assert h.get_tool("nope") is None


# ── Credential / dependency handling ────────────────────────────────────────


class TestNoCredentials:
    def test_missing_token_is_actionable(self, monkeypatch):
        monkeypatch.delenv("HUBSPOT_ACCESS_TOKEN", raising=False)
        # Ensure the SDK import "succeeds" so we hit the token check, not the
        # dependency hint.
        monkeypatch.setattr(h, "optional_import", lambda *a, **k: (MagicMock(), None))
        out = h.get_tool("hubspot_list_contacts").invoke({"limit": 3})
        assert "missing-credentials" in out
        assert "HUBSPOT_ACCESS_TOKEN" in out

    def test_unknown_activity_type(self, monkeypatch):
        out = h.get_tool("hubspot_log_activity").invoke(
            {"activity_type": "smoke-signal", "body": "hi"}
        )
        assert "unknown activity_type" in out
        assert "note" in out and "meeting" in out


# ── Behaviour against a mocked SDK client ───────────────────────────────────


@pytest.fixture
def mock_client(monkeypatch):
    """Patch _hs_client to return a MagicMock HubSpot client."""
    client = MagicMock()
    monkeypatch.setattr(h, "_hs_client", lambda: (client, None))
    return client


class TestReads:
    def test_list_objects_summarizes(self, mock_client):
        mock_client.crm.objects.basic_api.get_page.return_value = SimpleNamespace(
            results=[
                SimpleNamespace(
                    id="1",
                    properties={"firstname": "Ada", "lastname": "Lovelace", "email": "ada@x.io"},
                ),
            ]
        )
        out = h.get_tool("hubspot_list_contacts").invoke({"limit": 5})
        assert "Ada Lovelace" in out
        assert "ada@x.io" in out
        assert "id=1" in out
        # object_type was passed through to the generic API
        _, kwargs = mock_client.crm.objects.basic_api.get_page.call_args
        assert kwargs["object_type"] == "contacts"

    def test_get_pipeline(self, mock_client):
        mock_client.crm.pipelines.pipelines_api.get_all.return_value = SimpleNamespace(
            results=[
                SimpleNamespace(
                    id="default",
                    label="Sales Pipeline",
                    stages=[SimpleNamespace(id="s1", label="Appointment")],
                )
            ]
        )
        out = h.get_tool("hubspot_get_pipeline").invoke({"object_type": "deals"})
        assert "Sales Pipeline" in out
        assert "Appointment" in out


class TestWrites:
    def test_create_deal(self, mock_client):
        mock_client.crm.objects.basic_api.create.return_value = SimpleNamespace(id="42")
        out = h.get_tool("hubspot_create_deal").invoke({"name": "Acme renewal", "amount": 5000})
        assert "id=42" in out
        _, kwargs = mock_client.crm.objects.basic_api.create.call_args
        assert kwargs["object_type"] == "deals"
        props = kwargs["simple_public_object_input_for_create"].properties
        assert props["dealname"] == "Acme renewal"
        assert props["amount"] == "5000.0"

    def test_create_company_merges_extra_props(self, mock_client):
        mock_client.crm.objects.basic_api.create.return_value = SimpleNamespace(id="7")
        out = h.get_tool("hubspot_create_company").invoke(
            {
                "name": "Acme",
                "domain": "acme.io",
                "properties_json": json.dumps({"industry": "Tech"}),
            }
        )
        assert "id=7" in out
        _, kwargs = mock_client.crm.objects.basic_api.create.call_args
        props = kwargs["simple_public_object_input_for_create"].properties
        assert props == {"name": "Acme", "domain": "acme.io", "industry": "Tech"}

    def test_update_object(self, mock_client):
        out = h.get_tool("hubspot_update_deal").invoke(
            {"deal_id": "99", "properties_json": json.dumps({"dealstage": "closedwon"})}
        )
        assert "Updated deals 99" in out
        _, kwargs = mock_client.crm.objects.basic_api.update.call_args
        assert kwargs["object_type"] == "deals"
        assert kwargs["object_id"] == "99"

    def test_log_activity_with_association(self, mock_client):
        mock_client.crm.objects.basic_api.create.return_value = SimpleNamespace(id="500")
        out = h.get_tool("hubspot_log_activity").invoke(
            {
                "activity_type": "note",
                "body": "Called the customer",
                "associate_object_type": "contacts",
                "associate_object_id": "1",
            }
        )
        assert "id=500" in out
        assert "Associated notes 500" in out
        # The note was created as a 'notes' object…
        _, ckwargs = mock_client.crm.objects.basic_api.create.call_args
        assert ckwargs["object_type"] == "notes"
        # …and associated via the v4 default endpoint.
        _, akwargs = mock_client.crm.associations.v4.basic_api.create_default.call_args
        assert akwargs["from_object_type"] == "notes"
        assert akwargs["to_object_type"] == "contacts"
        assert akwargs["to_object_id"] == "1"


# ── Extended REST tools (Properties, Lists, Batch, Workflows, …) ────────────


def _resp(status=200, payload=None, text=None):
    """Build a fake requests.Response-like object for client.api_request."""
    body_text = text if text is not None else (json.dumps(payload) if payload is not None else "")
    return SimpleNamespace(
        status_code=status,
        text=body_text,
        json=lambda: payload if payload is not None else json.loads(body_text or "null"),
    )


class TestExtendedRestTools:
    def test_create_property_builds_request(self, mock_client):
        mock_client.api_request.return_value = _resp(201, {"name": "tier"})
        out = h.get_tool("hubspot_create_property").invoke(
            {
                "object_type": "contacts",
                "name": "tier",
                "label": "Tier",
                "type": "enumeration",
                "field_type": "select",
                "options_json": json.dumps([{"label": "Gold", "value": "gold"}]),
            }
        )
        assert "Created property 'tier'" in out
        opts = mock_client.api_request.call_args[0][0]
        assert opts["method"] == "POST"
        assert opts["path"] == "/crm/v3/properties/contacts"
        assert opts["body"]["options"] == [{"label": "Gold", "value": "gold"}]

    def test_create_list_maps_object_type_id(self, mock_client):
        mock_client.api_request.return_value = _resp(200, {"list": {"listId": "55"}})
        out = h.get_tool("hubspot_create_list").invoke({"name": "VIPs", "object_type": "contacts"})
        assert "id=55" in out
        opts = mock_client.api_request.call_args[0][0]
        assert opts["body"]["objectTypeId"] == "0-1"
        assert opts["body"]["processingType"] == "MANUAL"

    def test_add_to_list_splits_ids(self, mock_client):
        mock_client.api_request.return_value = _resp(200, {})
        out = h.get_tool("hubspot_add_to_list").invoke({"list_id": "55", "record_ids": "1, 2 ,3"})
        assert "Added 3 record(s)" in out
        opts = mock_client.api_request.call_args[0][0]
        assert opts["path"] == "/crm/v3/lists/55/memberships/add"
        assert opts["body"] == ["1", "2", "3"]

    def test_batch_create_wraps_inputs(self, mock_client):
        mock_client.api_request.return_value = _resp(201, {"results": [{"id": "10"}, {"id": "11"}]})
        out = h.get_tool("hubspot_batch_create").invoke(
            {
                "object_type": "contacts",
                "records_json": json.dumps([{"email": "a@x.io"}, {"email": "b@x.io"}]),
            }
        )
        assert "Batch-created 2 contacts" in out
        opts = mock_client.api_request.call_args[0][0]
        assert opts["body"]["inputs"] == [
            {"properties": {"email": "a@x.io"}},
            {"properties": {"email": "b@x.io"}},
        ]

    def test_create_workflow_posts_definition(self, mock_client):
        mock_client.api_request.return_value = _resp(201, {"id": "900"})
        out = h.get_tool("hubspot_create_workflow").invoke(
            {"definition_json": json.dumps({"type": "CONTACT_FLOW", "name": "Welcome"})}
        )
        assert "id=900" in out
        opts = mock_client.api_request.call_args[0][0]
        assert opts["path"] == "/automation/v4/flows"
        assert opts["body"]["type"] == "CONTACT_FLOW"

    def test_send_transactional_email(self, mock_client):
        mock_client.api_request.return_value = _resp(200, {"status": "SENT"})
        out = h.get_tool("hubspot_send_transactional_email").invoke(
            {"email_id": 12, "to_email": "x@y.io"}
        )
        assert "SENT" in out
        opts = mock_client.api_request.call_args[0][0]
        assert opts["path"] == "/marketing/v3/transactional/single-email/send"
        assert opts["body"]["message"]["to"] == "x@y.io"

    def test_http_error_is_actionable(self, mock_client):
        mock_client.api_request.return_value = _resp(403, text="missing scope crm.lists.write")
        out = h.get_tool("hubspot_create_list").invoke({"name": "x"})
        assert "HTTP 403" in out
        assert "missing scope" in out
        # Not HubSpot's MISSING_SCOPES shape, so no reconnect advice.
        assert "magic_link_connect_service" not in out

    def test_missing_scopes_403_explains_the_reconnect(self, mock_client):
        body = json.dumps(
            {
                "status": "error",
                "message": "This app hasn't been granted all required scopes to make this call.",
                "category": "MISSING_SCOPES",
            }
        )
        mock_client.api_request.return_value = _resp(403, text=body)
        out = h.get_tool("hubspot_list_forms").invoke({"limit": 3})
        assert "HTTP 403" in out
        assert 'magic_link_connect_service("hubspot")' in out
        # A missing scope is not a refused sign-in: the error boundary must not
        # mark the connection broken over it.
        from olano.tools._connector_auth import is_auth_refusal

        assert not is_auth_refusal(out)

    def test_missing_scopes_on_sdk_exception_explains_the_reconnect(self, mock_client):
        mock_client.crm.owners.owners_api.get_page.side_effect = RuntimeError(
            '(403)\nReason: Forbidden\nHTTP response body: {"category":"MISSING_SCOPES"}'
        )
        out = h.get_tool("hubspot_list_owners").invoke({"limit": 5})
        assert out.startswith("[tool-error] hubspot_list_owners:")
        assert 'magic_link_connect_service("hubspot")' in out

    def test_generic_api_request_passes_through(self, mock_client):
        mock_client.api_request.return_value = _resp(200, {"results": [{"id": "1"}]})
        out = h.get_tool("hubspot_api_request").invoke(
            {
                "method": "get",
                "path": "/marketing/v3/campaigns",
                "query_json": json.dumps({"limit": 5}),
            }
        )
        assert "GET /marketing/v3/campaigns → OK" in out
        opts = mock_client.api_request.call_args[0][0]
        assert opts["method"] == "GET"
        assert opts["qs"] == {"limit": 5}

    def test_invalid_json_arg_is_caught(self, mock_client):
        out = h.get_tool("hubspot_create_workflow").invoke({"definition_json": "{not json"})
        assert "invalid JSON" in out
        # No request should have been attempted.
        mock_client.api_request.assert_not_called()

    def test_analytics_is_read_path(self, mock_client):
        mock_client.api_request.return_value = _resp(200, {"totals": {"visits": 10}})
        out = h.get_tool("hubspot_get_analytics").invoke(
            {"breakdown": "sources", "time_period": "total"}
        )
        assert "Analytics sources/total" in out
        opts = mock_client.api_request.call_args[0][0]
        assert opts["method"] == "GET"
        assert opts["path"] == "/analytics/v2/reports/sources/total"


# ── Subagent wiring ─────────────────────────────────────────────────────────


class TestSubagentWiring:
    def test_agent_resolves(self):
        from olano.subagents.registry import load_toolkit_agent

        defn = load_toolkit_agent("HubSpotAgent")
        assert defn.toolkit == "hubspot"
        assert defn.category == "CRM"
        assert "HUBSPOT_ACCESS_TOKEN" in defn.credential_env_vars

    def test_default_tools_all_exist_in_toolkit(self):
        from olano.subagents.registry import resolve_as_subagent

        sub = resolve_as_subagent("HubSpotAgent")
        toolkit_names = {t.name for t in h.get_tools()}
        assert sub.tools, "HubSpotAgent resolved with no tools"
        missing = [t for t in sub.tools if t not in toolkit_names]
        assert not missing, f"default_tools reference missing tools: {missing}"
