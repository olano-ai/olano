"""Tests for the Intuit QuickBooks Online toolkit.

Network is faked at the ``httpx`` seam (the module-level reference is swapped,
so nothing global is patched) — these assert the things that are actually easy
to get wrong against a real company file: the query text sent, the write payload
shape (SyncToken + sparse), and the report renderer that keeps a 50 KB JSON
envelope out of the model's context.
"""

from __future__ import annotations

from typing import Any

import pytest

from olano.tools import quickbooks_tools as qb


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeHttpx:
    """Records every call and answers from a handler keyed on (method, url)."""

    def __init__(self, handler: Any = None) -> None:
        self.handler = handler or (lambda *_a, **_k: {})
        self.calls: list[dict[str, Any]] = []

    def _record(self, method: str, url: str, kw: dict[str, Any]) -> _Resp:
        self.calls.append({"method": method, "url": url, **kw})
        payload = self.handler(method, url, kw)
        return _Resp(payload if isinstance(payload, dict) else {})

    def get(self, url: str, **kw: Any) -> _Resp:
        return self._record("GET", url, kw)

    def post(self, url: str, **kw: Any) -> _Resp:
        return self._record("POST", url, kw)


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUICKBOOKS_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("QUICKBOOKS_REALM_ID", "4620816365")
    monkeypatch.delenv("QUICKBOOKS_ENVIRONMENT", raising=False)


def _install(monkeypatch: pytest.MonkeyPatch, handler: Any = None) -> _FakeHttpx:
    fake = _FakeHttpx(handler)
    monkeypatch.setattr(qb, "httpx", fake)
    return fake


def _queries(fake: _FakeHttpx) -> list[str]:
    return [c["params"]["query"] for c in fake.calls if (c.get("params") or {}).get("query")]


# ── Toolkit surface ─────────────────────────────────────────────────────────


class TestToolkitSurface:
    def test_covers_all_four_declared_categories(self):
        names = {t.name for t in qb.get_tools()}
        for expected in (
            # Customer management
            "quickbooks_list_customers",
            "quickbooks_get_customer",
            "quickbooks_search_customers",
            "quickbooks_create_customer",
            "quickbooks_update_customer",
            # Invoicing & payments
            "quickbooks_create_invoice",
            "quickbooks_get_invoice",
            "quickbooks_send_invoice",
            "quickbooks_void_invoice",
            "quickbooks_record_payment",
            "quickbooks_create_estimate",
            "quickbooks_create_sales_receipt",
            # Expenses
            "quickbooks_create_bill",
            "quickbooks_pay_bill",
            "quickbooks_list_vendors",
            "quickbooks_create_expense",
            "quickbooks_list_accounts",
            # Business insights
            "quickbooks_cash_flow",
            "quickbooks_ar_aging",
            "quickbooks_ap_aging",
            "quickbooks_sales_by_customer",
            "quickbooks_expenses_by_vendor",
            "quickbooks_trial_balance",
        ):
            assert expected in names, f"missing tool {expected}"

    def test_get_tool_by_name(self):
        assert qb.get_tool("quickbooks_create_invoice") is not None
        assert qb.get_tool("nope") is None


class TestNoCredentials:
    def test_missing_token_is_actionable(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("QUICKBOOKS_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("QUICKBOOKS_REALM_ID", "1")
        out = qb.get_tool("quickbooks_list_customers").invoke({"limit": 5})
        assert "missing-credentials" in out

    def test_missing_realm_is_actionable(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("QUICKBOOKS_ACCESS_TOKEN", "tok")
        monkeypatch.delenv("QUICKBOOKS_REALM_ID", raising=False)
        out = qb.get_tool("quickbooks_list_invoices").invoke({})
        assert "missing-credentials" in out


class TestEnvironmentRouting:
    def test_sandbox_env_targets_sandbox_host(self, monkeypatch: pytest.MonkeyPatch, creds):
        monkeypatch.setenv("QUICKBOOKS_ENVIRONMENT", "sandbox")
        fake = _install(monkeypatch)
        qb.get_tool("quickbooks_list_customers").invoke({"limit": 1})
        assert "sandbox-quickbooks.api.intuit.com" in fake.calls[0]["url"]

    def test_default_targets_production_host(self, monkeypatch: pytest.MonkeyPatch, creds):
        fake = _install(monkeypatch)
        qb.get_tool("quickbooks_list_customers").invoke({"limit": 1})
        assert "//quickbooks.api.intuit.com" in fake.calls[0]["url"]


# ── Pure helpers ────────────────────────────────────────────────────────────


class TestHelpers:
    def test_esc_neutralizes_quote_injection(self):
        assert qb._esc("O'Brien") == "O\\'Brien"
        assert qb._esc("back\\slash") == "back\\\\slash"

    @pytest.mark.parametrize(
        ("given", "expected"),
        [(0, 1), (-5, 1), (20, 20), (99999, 1000), ("nope", 20)],
    )
    def test_limit_is_clamped(self, given, expected):
        assert qb._limit(given) == expected

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            # The CamelCase spelling QuickBooks itself documents must survive:
            # str.title() would mangle it to "Creditcard" and reject a valid call.
            ("CreditCard", "CreditCard"),
            ("creditcard", "CreditCard"),
            ("credit card", "CreditCard"),
            ("credit_card", "CreditCard"),
            ("CREDITCARD", "CreditCard"),
            ("check", "Check"),
            (" Check ", "Check"),
            ("wire", None),
            ("", None),
        ],
    )
    def test_enum_normalizes_without_mangling_camelcase(self, given, expected):
        assert qb._enum(given, ("Cash", "Check", "CreditCard")) == expected

    def test_rows_tolerates_missing_array(self):
        assert qb._rows({}, "Customer") == []
        assert qb._rows({"QueryResponse": {}}, "Customer") == []
        assert qb._rows({"QueryResponse": {"Customer": [{"Id": "1"}]}}, "Customer") == [{"Id": "1"}]


class TestReportRenderer:
    """A report is an indented outline, never the raw ``Rows``/``ColData`` tree."""

    REPORT = {
        "Header": {
            "ReportName": "ProfitAndLoss",
            "StartPeriod": "2026-01-01",
            "EndPeriod": "2026-03-31",
            "Currency": "USD",
        },
        "Columns": {"Column": [{"ColTitle": ""}, {"ColTitle": "Total"}]},
        "Rows": {
            "Row": [
                {
                    "Header": {"ColData": [{"value": "Income"}, {"value": ""}]},
                    "Rows": {
                        "Row": [
                            {
                                "ColData": [
                                    {"value": "Design services", "id": "1"},
                                    {"value": "4500.00"},
                                ],
                                "type": "Data",
                            }
                        ]
                    },
                    "Summary": {"ColData": [{"value": "Total Income"}, {"value": "4500.00"}]},
                    "type": "Section",
                }
            ]
        },
    }

    def test_renders_nested_sections_as_outline(self):
        out = qb._render_report(self.REPORT)
        assert "ProfitAndLoss (2026-01-01 → 2026-03-31) [USD]" in out
        assert "Design services: 4500.00" in out
        assert "Total Income: 4500.00" in out
        # The raw envelope keys never reach the model.
        assert "ColData" not in out and "SummaryRow" not in out

    def test_indents_nested_rows_under_their_section(self):
        lines = qb._render_report(self.REPORT).splitlines()
        detail = next(ln for ln in lines if "Design services" in ln)
        assert detail.startswith("  "), "nested rows must be indented under their section"

    def test_empty_report_says_so_instead_of_rendering_nothing(self):
        out = qb._render_report({"Header": {"ReportName": "BalanceSheet"}, "Rows": {}})
        assert "No data for this period." in out

    def test_survives_a_malformed_payload(self):
        assert "Report" in qb._render_report(None)
        assert "Report" in qb._render_report({"Rows": {"Row": "not-a-list"}})

    def test_report_tool_drops_empty_params(self, monkeypatch: pytest.MonkeyPatch, creds):
        fake = _install(monkeypatch, lambda *_a, **_k: self.REPORT)
        qb.get_tool("quickbooks_balance_sheet").invoke({"as_of_date": ""})
        params = fake.calls[0]["params"]
        assert "as_of_date" not in params, "an empty date must not be sent as a blank param"
        assert params["accounting_method"] == "Accrual"


class TestSalesLines:
    def test_single_line_uses_amount_and_description(self):
        lines, err = qb._sales_lines(100.0, "Design", "7", "")
        assert err is None and lines is not None
        assert lines[0]["Amount"] == 100.0
        assert lines[0]["Description"] == "Design"
        assert lines[0]["SalesItemLineDetail"]["ItemRef"] == {"value": "7"}

    def test_multi_line_json_overrides_the_simple_path(self):
        lines, err = qb._sales_lines(
            999.0,
            "ignored",
            "7",
            '[{"amount": 10, "description": "A"}, {"quantity": 2, "unit_price": 25}]',
        )
        assert err is None and lines is not None
        assert [ln["Amount"] for ln in lines] == [10.0, 50.0]
        assert lines[1]["SalesItemLineDetail"]["Qty"] == 2.0

    def test_bad_json_is_actionable(self):
        lines, err = qb._sales_lines(1.0, "", "7", "{not json")
        assert lines is None and err is not None and "not valid JSON" in err

    def test_line_without_an_amount_is_refused(self):
        lines, err = qb._sales_lines(0.0, "", "7", '[{"description": "no amount"}]')
        assert lines is None and err is not None and "amount" in err

    def test_missing_item_resolves_the_first_active_item(
        self, monkeypatch: pytest.MonkeyPatch, creds
    ):
        _install(
            monkeypatch,
            lambda *_a, **_k: {"QueryResponse": {"Item": [{"Id": "42", "Type": "Service"}]}},
        )
        lines, err = qb._sales_lines(10.0, "x", "", "")
        assert err is None and lines is not None
        assert lines[0]["SalesItemLineDetail"]["ItemRef"] == {"value": "42"}

    def test_no_items_at_all_is_actionable(self, monkeypatch: pytest.MonkeyPatch, creds):
        _install(monkeypatch, lambda *_a, **_k: {"QueryResponse": {}})
        lines, err = qb._sales_lines(10.0, "x", "", "")
        assert lines is None and err is not None and "item_id" in err


class TestExpenseLines:
    def test_account_is_required(self):
        lines, err = qb._expense_lines(50.0, "Hosting", "", "")
        assert lines is None and err is not None and "account_id" in err

    def test_builds_account_based_detail(self):
        lines, err = qb._expense_lines(50.0, "Hosting", "63", "")
        assert err is None and lines is not None
        detail = lines[0]["AccountBasedExpenseLineDetail"]
        assert detail["AccountRef"] == {"value": "63"}
        assert lines[0]["DetailType"] == "AccountBasedExpenseLineDetail"


# ── Query construction ──────────────────────────────────────────────────────


class TestQueryConstruction:
    def test_unpaid_and_customer_filters_compose(self, monkeypatch: pytest.MonkeyPatch, creds):
        fake = _install(monkeypatch)
        qb.get_tool("quickbooks_list_invoices").invoke(
            {"customer_id": "12", "unpaid_only": True, "limit": 5}
        )
        q = _queries(fake)[0]
        assert "CustomerRef = '12'" in q
        assert "Balance > '0'" in q
        assert "MAXRESULTS 5" in q

    def test_no_filters_emits_no_where_clause(self, monkeypatch: pytest.MonkeyPatch, creds):
        fake = _install(monkeypatch)
        qb.get_tool("quickbooks_list_invoices").invoke({})
        assert "WHERE" not in _queries(fake)[0]

    def test_search_falls_back_to_email_when_name_misses(
        self, monkeypatch: pytest.MonkeyPatch, creds
    ):
        def handler(_method, _url, kw):
            query = (kw.get("params") or {}).get("query", "")
            if "PrimaryEmailAddr" in query:
                return {"QueryResponse": {"Customer": [{"Id": "9", "DisplayName": "Acme"}]}}
            return {"QueryResponse": {}}

        fake = _install(monkeypatch, handler)
        out = qb.get_tool("quickbooks_search_customers").invoke({"term": "acme"})
        queries = _queries(fake)
        assert len(queries) == 2, "a name miss should retry against email"
        assert "Acme" in out

    def test_search_term_is_escaped(self, monkeypatch: pytest.MonkeyPatch, creds):
        fake = _install(monkeypatch)
        qb.get_tool("quickbooks_search_customers").invoke({"term": "O'Brien"})
        assert "O\\'Brien" in _queries(fake)[0]

    def test_empty_search_term_is_refused(self, creds):
        out = qb.get_tool("quickbooks_search_customers").invoke({"term": "  "})
        assert "tool-error" in out

    def test_account_type_filter_is_applied(self, monkeypatch: pytest.MonkeyPatch, creds):
        fake = _install(monkeypatch)
        qb.get_tool("quickbooks_list_accounts").invoke({"account_type": "Expense"})
        assert "AccountType = 'Expense'" in _queries(fake)[0]


# ── Writes ──────────────────────────────────────────────────────────────────


class TestWrites:
    def test_update_customer_sends_synctoken_and_sparse(
        self, monkeypatch: pytest.MonkeyPatch, creds
    ):
        def handler(method, _url, _kw):
            if method == "GET":
                return {"Customer": {"Id": "3", "SyncToken": "7"}}
            return {"Customer": {"Id": "3", "DisplayName": "New Name"}}

        fake = _install(monkeypatch, handler)
        out = qb.get_tool("quickbooks_update_customer").invoke(
            {"customer_id": "3", "display_name": "New Name"}
        )
        body = next(c["json"] for c in fake.calls if c["method"] == "POST")
        assert body["SyncToken"] == "7", "QuickBooks rejects a write without the current SyncToken"
        assert body["sparse"] is True, "a partial update must be sparse or it blanks other fields"
        assert body["Id"] == "3"
        assert "New Name" in out

    def test_update_customer_with_no_fields_is_refused(self, creds):
        out = qb.get_tool("quickbooks_update_customer").invoke({"customer_id": "3"})
        assert "tool-error" in out and "at least one field" in out

    def test_void_invoice_uses_the_void_operation(self, monkeypatch: pytest.MonkeyPatch, creds):
        def handler(method, _url, _kw):
            if method == "GET":
                return {"Invoice": {"Id": "5", "SyncToken": "2"}}
            return {"Invoice": {"Id": "5", "DocNumber": "1001", "TotalAmt": 0}}

        fake = _install(monkeypatch, handler)
        out = qb.get_tool("quickbooks_void_invoice").invoke({"invoice_id": "5"})
        post = next(c for c in fake.calls if c["method"] == "POST")
        assert post["params"]["operation"] == "void"
        assert post["json"]["SyncToken"] == "2"
        assert "Voided" in out

    def test_record_payment_links_the_invoice(self, monkeypatch: pytest.MonkeyPatch, creds):
        fake = _install(monkeypatch, lambda *_a, **_k: {"Payment": {"Id": "8", "TotalAmt": 250.0}})
        qb.get_tool("quickbooks_record_payment").invoke(
            {"customer_id": "3", "amount": 250.0, "invoice_id": "5"}
        )
        body = fake.calls[0]["json"]
        assert body["Line"][0]["LinkedTxn"] == [{"TxnId": "5", "TxnType": "Invoice"}]
        assert body["TotalAmt"] == 250.0

    def test_record_payment_without_invoice_stays_unapplied(
        self, monkeypatch: pytest.MonkeyPatch, creds
    ):
        fake = _install(monkeypatch, lambda *_a, **_k: {"Payment": {"Id": "8", "TotalAmt": 250.0}})
        out = qb.get_tool("quickbooks_record_payment").invoke({"customer_id": "3", "amount": 250.0})
        assert "Line" not in fake.calls[0]["json"]
        assert "unapplied" in out

    def test_send_invoice_passes_the_override_address(self, monkeypatch: pytest.MonkeyPatch, creds):
        fake = _install(
            monkeypatch, lambda *_a, **_k: {"Invoice": {"Id": "5", "EmailStatus": "EmailSent"}}
        )
        out = qb.get_tool("quickbooks_send_invoice").invoke(
            {"invoice_id": "5", "email": "ap@acme.com"}
        )
        call = fake.calls[0]
        assert call["params"]["sendTo"] == "ap@acme.com"
        assert call["content"] == b"", "Intuit's /send expects an empty octet-stream body"
        assert "ap@acme.com" in out

    @pytest.mark.parametrize(
        ("pay_type", "expected_key", "ref_key"),
        [
            ("Check", "CheckPayment", "BankAccountRef"),
            ("CreditCard", "CreditCardPayment", "CCAccountRef"),
        ],
    )
    def test_pay_bill_routes_the_funding_account(
        self, monkeypatch: pytest.MonkeyPatch, creds, pay_type, expected_key, ref_key
    ):
        fake = _install(monkeypatch, lambda *_a, **_k: {"BillPayment": {"Id": "9"}})
        qb.get_tool("quickbooks_pay_bill").invoke(
            {
                "vendor_id": "2",
                "bill_id": "4",
                "amount": 100.0,
                "bank_account_id": "35",
                "pay_type": pay_type,
            }
        )
        body = fake.calls[0]["json"]
        assert body["PayType"] == pay_type
        assert body[expected_key][ref_key] == {"value": "35"}
        assert body["Line"][0]["LinkedTxn"] == [{"TxnId": "4", "TxnType": "Bill"}]

    def test_pay_bill_rejects_an_unknown_pay_type(self, creds):
        out = qb.get_tool("quickbooks_pay_bill").invoke(
            {
                "vendor_id": "2",
                "bill_id": "4",
                "amount": 100.0,
                "bank_account_id": "35",
                "pay_type": "Wire",
            }
        )
        assert "tool-error" in out

    def test_create_expense_rejects_an_unknown_payment_type(self, creds):
        out = qb.get_tool("quickbooks_create_expense").invoke(
            {"payment_account_id": "35", "amount": 10.0, "account_id": "63", "payment_type": "Wire"}
        )
        assert "tool-error" in out

    def test_create_invoice_carries_due_date_and_email(
        self, monkeypatch: pytest.MonkeyPatch, creds
    ):
        fake = _install(
            monkeypatch, lambda *_a, **_k: {"Invoice": {"Id": "5", "DocNumber": "1001"}}
        )
        qb.get_tool("quickbooks_create_invoice").invoke(
            {
                "customer_id": "3",
                "amount": 500.0,
                "description": "Retainer",
                "due_date": "2026-10-01",
                "item_id": "7",
                "email": "ap@acme.com",
            }
        )
        body = fake.calls[0]["json"]
        assert body["DueDate"] == "2026-10-01"
        assert body["BillEmail"] == {"Address": "ap@acme.com"}
        assert body["Line"][0]["Amount"] == 500.0


class TestAccountingMethod:
    def test_lowercase_method_is_normalized_for_intuit(
        self, monkeypatch: pytest.MonkeyPatch, creds
    ):
        fake = _install(monkeypatch, lambda *_a, **_k: {"Header": {"ReportName": "P&L"}})
        qb.get_tool("quickbooks_profit_and_loss").invoke(
            {"start_date": "2026-01-01", "end_date": "2026-03-31", "accounting_method": "cash"}
        )
        assert fake.calls[0]["params"]["accounting_method"] == "Cash"

    def test_unknown_method_is_refused(self, creds):
        out = qb.get_tool("quickbooks_profit_and_loss").invoke(
            {"start_date": "2026-01-01", "end_date": "2026-03-31", "accounting_method": "guess"}
        )
        assert "tool-error" in out


class TestHitlGating:
    """Every write is approval-gated; no read is.

    ``QuickBooksAgent`` ships ``hitl_write_tools: true``, but that toggle only
    adds the ``write_external`` **category** — a mutating tool missing from that
    frozenset posts to a real company's books with no approval card.
    """

    MUTATIONS = {
        "quickbooks_create_customer",
        "quickbooks_update_customer",
        "quickbooks_create_vendor",
        "quickbooks_create_invoice",
        "quickbooks_send_invoice",
        "quickbooks_void_invoice",
        "quickbooks_create_estimate",
        "quickbooks_create_sales_receipt",
        "quickbooks_record_payment",
        "quickbooks_create_bill",
        "quickbooks_pay_bill",
        "quickbooks_create_expense",
    }

    def test_every_mutating_tool_is_gated(self):
        from olano.security.hitl_policy import CATEGORY_TOOLS

        assert self.MUTATIONS <= CATEGORY_TOOLS["write_external"]

    def test_the_mutation_list_matches_the_toolkit(self):
        """A new create_/update_/void_/pay_/send_ tool fails here until it is gated."""
        verbs = ("create_", "update_", "void_", "pay_", "send_", "record_")
        found = {
            t.name
            for t in qb.get_tools()
            if any(t.name.startswith(f"quickbooks_{v}") for v in verbs)
        }
        assert found == self.MUTATIONS

    def test_reads_are_not_gated(self):
        from olano.security.hitl_policy import CATEGORY_TOOLS

        reads = {t.name for t in qb.get_tools()} - self.MUTATIONS
        assert not (reads & CATEGORY_TOOLS["write_external"])


class TestReadOnlyGuard:
    def test_query_refuses_non_select(self, creds):
        out = qb.get_tool("quickbooks_query").invoke({"query": "DELETE FROM Customer"})
        assert "only SELECT statements are allowed" in out
