"""Intuit QuickBooks Online tools.

Authentication uses OAuth access tokens supplied by the host environment:
``QUICKBOOKS_ACCESS_TOKEN`` and ``QUICKBOOKS_REALM_ID``. Set
``QUICKBOOKS_ENVIRONMENT=sandbox`` to use the sandbox API host; any other value
uses production. The connector broker
(``olano/core/connectors/providers/quickbooks.py``) supplies all three after a
user connects, refreshing the access token automatically.

The toolkit covers the four app categories Olano declares to Intuit:

* **Customer management** — ``list``/``get``/``search``/``create``/``update`` customers.
* **Invoicing & payments** — invoices (incl. send + void), estimates, sales
  receipts, and customer payments applied against invoices.
* **Expenses** — vendors, bills, bill payments, and cash/card expenses.
* **Business insights** — P&L, balance sheet, cash flow, A/R + A/P aging,
  sales by customer, expenses by vendor, and trial balance.

Two conventions matter when extending this file:

* **Reports are rendered, never dumped.** A QuickBooks report is a deeply
  nested ``Rows``/``ColData`` tree that runs to tens of kilobytes of JSON; every
  report tool routes through ``_render_report`` so the model reads an indented
  outline instead of burning its context on the raw envelope.
* **Updates carry a SyncToken.** QuickBooks uses optimistic locking: a write
  must echo the record's current ``SyncToken`` or it is rejected. ``_sync_token``
  reads it immediately before the write, and mutations post ``sparse: true`` so
  unlisted fields are preserved rather than blanked.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

_QB_CREDS_MSG = (
    "[missing-credentials] QuickBooks not configured. Set QUICKBOOKS_ACCESS_TOKEN and "
    "QUICKBOOKS_REALM_ID. Use QUICKBOOKS_ENVIRONMENT=sandbox for the sandbox host."
)

# Pinned so every call agrees on the schema the responses are parsed against.
_MINOR_VERSION = "75"

# Endpoint path → the key the entity comes back under in the JSON response.
_ENTITY_KEYS: dict[str, str] = {
    "account": "Account",
    "bill": "Bill",
    "billpayment": "BillPayment",
    "customer": "Customer",
    "estimate": "Estimate",
    "invoice": "Invoice",
    "item": "Item",
    "payment": "Payment",
    "purchase": "Purchase",
    "salesreceipt": "SalesReceipt",
    "vendor": "Vendor",
}


def _base_url() -> tuple[str | None, str | None]:
    env = get_credential("QUICKBOOKS_ENVIRONMENT", "production").lower()
    host = "sandbox-quickbooks.api.intuit.com" if env == "sandbox" else "quickbooks.api.intuit.com"
    realm_id = get_credential("QUICKBOOKS_REALM_ID")
    if not realm_id:
        return None, _QB_CREDS_MSG
    return f"https://{host}/v3/company/{realm_id}", None


def _headers() -> tuple[dict[str, str] | None, str | None]:
    token = get_credential("QUICKBOOKS_ACCESS_TOKEN")
    if not token:
        return None, _QB_CREDS_MSG
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}, None


def _post_json(
    path: str, payload: dict[str, Any], params: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    base, err = _base_url()
    if err:
        return None, err
    headers, err = _headers()
    if err:
        return None, err
    try:
        r = httpx.post(
            f"{base}/{path.lstrip('/')}",
            headers=headers,
            json=payload,
            params=params,
            timeout=20,
        )
        r.raise_for_status()
        return r.json(), None
    except Exception as exc:
        return None, f"[tool-error] QuickBooks POST {path}: {exc}"


def _post_empty(
    path: str, params: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """POST with an empty body — the shape Intuit's ``/send`` actions expect."""
    base, err = _base_url()
    if err:
        return None, err
    headers, err = _headers()
    if err:
        return None, err
    send_headers = dict(headers or {})
    send_headers["Content-Type"] = "application/octet-stream"
    try:
        r = httpx.post(
            f"{base}/{path.lstrip('/')}",
            headers=send_headers,
            params=params,
            content=b"",
            timeout=30,
        )
        r.raise_for_status()
        return r.json(), None
    except Exception as exc:
        return None, f"[tool-error] QuickBooks POST {path}: {exc}"


def _get_json(
    path: str, params: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    base, err = _base_url()
    if err:
        return None, err
    headers, err = _headers()
    if err:
        return None, err
    try:
        r = httpx.get(f"{base}/{path.lstrip('/')}", headers=headers, params=params, timeout=20)
        r.raise_for_status()
        return r.json(), None
    except Exception as exc:
        return None, f"[tool-error] QuickBooks GET {path}: {exc}"


def _query(query: str) -> tuple[dict[str, Any] | None, str | None]:
    return _get_json("query", {"query": query, "minorversion": _MINOR_VERSION})


def _rows(data: dict[str, Any] | None, entity: str) -> list[dict[str, Any]]:
    """The ``QueryResponse.<Entity>`` array, always a list (QBO omits it when empty)."""
    found = (data or {}).get("QueryResponse", {}).get(entity, [])
    return [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []


def _lines(items: list[str], empty: str) -> str:
    return "\n".join(items) if items else empty


def _esc(value: str) -> str:
    """Escape a literal for interpolation into a QuickBooks query string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _limit(value: int, cap: int = 1000) -> int:
    """Clamp a caller-supplied row limit into the range QuickBooks accepts."""
    try:
        return max(1, min(int(value), cap))
    except (TypeError, ValueError):
        return 20


def _enum(value: str, allowed: tuple[str, ...]) -> str | None:
    """Resolve a caller's spelling to the exact casing QuickBooks expects.

    Matching ignores case, spaces and punctuation, so ``credit card``,
    ``credit_card`` and ``CREDITCARD`` all reach ``CreditCard``. Never use
    ``str.title()`` for this — it lowercases the interior of a CamelCase word,
    turning the already-correct ``CreditCard`` into the invalid ``Creditcard``.
    """
    key = "".join(ch for ch in value.lower() if ch.isalnum())
    for option in allowed:
        if key == "".join(ch for ch in option.lower() if ch.isalnum()):
            return option
    return None


def _ref_name(record: dict[str, Any], key: str) -> str:
    ref = record.get(key)
    if isinstance(ref, dict):
        return str(ref.get("name") or ref.get("value") or "")
    return ""


def _sync_token(entity: str, obj_id: str) -> tuple[str | None, str | None]:
    """Read a record's current SyncToken — required on every QuickBooks write."""
    data, err = _get_json(f"{entity}/{obj_id}", {"minorversion": _MINOR_VERSION})
    if err:
        return None, err
    record = (data or {}).get(_ENTITY_KEYS.get(entity, entity.capitalize())) or {}
    token = record.get("SyncToken") if isinstance(record, dict) else None
    if token is None:
        return None, f"[tool-error] QuickBooks {entity} {obj_id}: no SyncToken in the response."
    return str(token), None


def _default_item_id() -> tuple[str | None, str | None]:
    """The company's first sellable item — invoices need an ItemRef on every line."""
    data, err = _query("SELECT * FROM Item WHERE Active = true MAXRESULTS 50")
    if err:
        return None, err
    items = _rows(data, "Item")
    for item in items:
        if item.get("Type") in ("Service", "NonInventory", "Inventory"):
            return str(item.get("Id")), None
    if items:
        return str(items[0].get("Id")), None
    return None, (
        "[tool-error] No active product/service found in QuickBooks. Create one, or pass "
        "item_id explicitly (list candidates with quickbooks_list_items)."
    )


def _parse_line_items(line_items: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    if not line_items.strip():
        return [], None
    try:
        raw = json.loads(line_items)
    except json.JSONDecodeError as exc:
        return None, f"[tool-error] line_items is not valid JSON: {exc}"
    if not isinstance(raw, list) or not raw:
        return None, "[tool-error] line_items must be a non-empty JSON array."
    if not all(isinstance(entry, dict) for entry in raw):
        return None, "[tool-error] every line_items entry must be a JSON object."
    return list(raw), None


def _sales_lines(
    amount: float, description: str, item_id: str, line_items: str
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Build ``SalesItemLineDetail`` lines for an invoice/estimate/sales receipt."""
    parsed, err = _parse_line_items(line_items)
    if err:
        return None, err
    entries = parsed or [{"amount": amount, "description": description, "item_id": item_id}]

    default_item: str | None = None
    lines: list[dict[str, Any]] = []
    for entry in entries:
        ref = str(entry.get("item_id") or item_id or "").strip()
        if not ref:
            if default_item is None:
                default_item, item_err = _default_item_id()
                if item_err:
                    return None, item_err
            ref = default_item or ""
        if not ref:
            return None, "[tool-error] could not resolve an item for this line; pass item_id."

        try:
            qty = entry.get("quantity")
            unit_price = entry.get("unit_price")
            line_amount = entry.get("amount")
            if line_amount is None and qty is not None and unit_price is not None:
                line_amount = float(qty) * float(unit_price)
            if line_amount is None:
                return None, "[tool-error] each line needs amount, or quantity + unit_price."
            detail: dict[str, Any] = {"ItemRef": {"value": ref}}
            if qty is not None:
                detail["Qty"] = float(qty)
            if unit_price is not None:
                detail["UnitPrice"] = float(unit_price)
            line: dict[str, Any] = {
                "Amount": float(line_amount),
                "DetailType": "SalesItemLineDetail",
                "SalesItemLineDetail": detail,
            }
        except (TypeError, ValueError) as exc:
            return None, f"[tool-error] invalid number in line_items: {exc}"

        desc = str(entry.get("description") or "").strip()
        if desc:
            line["Description"] = desc
        lines.append(line)
    return lines, None


def _expense_lines(
    amount: float, description: str, account_id: str, line_items: str
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Build ``AccountBasedExpenseLineDetail`` lines for a bill/expense."""
    parsed, err = _parse_line_items(line_items)
    if err:
        return None, err
    entries = parsed or [{"amount": amount, "description": description, "account_id": account_id}]

    lines: list[dict[str, Any]] = []
    for entry in entries:
        ref = str(entry.get("account_id") or account_id or "").strip()
        if not ref:
            return None, (
                "[tool-error] every line needs an expense account_id — list them with "
                "quickbooks_list_accounts (account_type='Expense')."
            )
        try:
            line_amount = entry.get("amount")
            if line_amount is None:
                return None, "[tool-error] each line needs an amount."
            line: dict[str, Any] = {
                "Amount": float(line_amount),
                "DetailType": "AccountBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": ref}},
            }
        except (TypeError, ValueError) as exc:
            return None, f"[tool-error] invalid number in line_items: {exc}"

        desc = str(entry.get("description") or "").strip()
        if desc:
            line["Description"] = desc
        lines.append(line)
    return lines, None


def _coldata_line(cols: Any) -> str:
    if not isinstance(cols, list):
        return ""
    cells = [str(c.get("value", "")).strip() for c in cols if isinstance(c, dict)]
    while cells and not cells[-1]:
        cells.pop()
    if not cells:
        return ""
    label, values = cells[0], [v for v in cells[1:] if v]
    if not values:
        return label
    return f"{label or '—'}: {' | '.join(values)}"


def _report_lines(rows: Any, depth: int = 0) -> list[str]:
    """Flatten QuickBooks' nested report rows into an indented outline."""
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    pad = "  " * depth
    for row in rows:
        if not isinstance(row, dict):
            continue
        header = row.get("Header")
        if isinstance(header, dict):
            line = _coldata_line(header.get("ColData"))
            if line:
                out.append(f"{pad}{line}")
        line = _coldata_line(row.get("ColData"))
        if line:
            out.append(f"{pad}{line}")
        nested = row.get("Rows")
        if isinstance(nested, dict):
            out.extend(_report_lines(nested.get("Row"), depth + 1))
        summary = row.get("Summary")
        if isinstance(summary, dict):
            line = _coldata_line(summary.get("ColData"))
            if line:
                out.append(f"{pad}{line}")
    return out


def _render_report(data: dict[str, Any] | None) -> str:
    """Render a report response as readable text instead of a raw JSON dump."""
    payload = data or {}
    header = payload.get("Header")
    header = header if isinstance(header, dict) else {}

    title = str(header.get("ReportName") or "Report")
    period = " → ".join(
        str(header.get(key)) for key in ("StartPeriod", "EndPeriod") if header.get(key)
    )
    head = f"{title} ({period})" if period else title
    currency = str(header.get("Currency") or "")
    if currency:
        head = f"{head} [{currency}]"

    out = [head]
    columns = payload.get("Columns")
    if isinstance(columns, dict):
        titles = [
            str(c.get("ColTitle", "")).strip()
            for c in columns.get("Column", [])
            if isinstance(c, dict)
        ]
        titles = [t for t in titles if t]
        if len(titles) > 1:
            out.append("Columns: " + " | ".join(titles))

    rows = payload.get("Rows")
    body = _report_lines(rows.get("Row") if isinstance(rows, dict) else None)
    if not body:
        out.append("No data for this period.")
        return "\n".join(out)
    out.extend(body)
    return "\n".join(out)


def _report(name: str, params: dict[str, Any]) -> str:
    payload = {k: v for k, v in params.items() if v not in (None, "")}
    if "accounting_method" in payload:
        method = _enum(str(payload["accounting_method"]), ("Accrual", "Cash"))
        if method is None:
            return "[tool-error] accounting_method must be 'Accrual' or 'Cash'."
        payload["accounting_method"] = method
    payload["minorversion"] = _MINOR_VERSION
    data, err = _get_json(f"reports/{name}", payload)
    if err:
        return err
    return _render_report(data)


# ── Company & generic access ────────────────────────────────────────────────


@tool
def quickbooks_company_info() -> str:
    """Get basic QuickBooks company information."""
    data, err = _get_json("companyinfo/1", {"minorversion": _MINOR_VERSION})
    if err:
        return err
    info = (data or {}).get("CompanyInfo", {})
    return f"{info.get('CompanyName', 'Unknown company')} (id={info.get('Id', 'unknown')})"


@tool
def quickbooks_query(
    query: Annotated[str, "QuickBooks SQL-like query, e.g. SELECT * FROM Customer MAXRESULTS 10"],
) -> str:
    """Run a read-only QuickBooks query and return the JSON response."""
    lowered = query.strip().lower()
    if not lowered.startswith("select"):
        return "[tool-error] quickbooks_query: only SELECT statements are allowed."
    data, err = _query(query)
    if err:
        return err
    return str(data)


# ── Reference data (accounts & items) ───────────────────────────────────────


@tool
def quickbooks_list_accounts(
    account_type: Annotated[
        str, "Filter by account type, e.g. 'Expense', 'Bank', 'Income'. Empty lists all types."
    ] = "",
    limit: Annotated[int, "Maximum accounts to return"] = 50,
) -> str:
    """List chart-of-accounts entries — use this to find the account_id an expense or bill needs."""
    where = "WHERE Active = true"
    if account_type.strip():
        where += f" AND AccountType = '{_esc(account_type.strip())}'"
    data, err = _query(f"SELECT * FROM Account {where} MAXRESULTS {_limit(limit)}")
    if err:
        return err
    return _lines(
        [
            f"- [{a.get('Id')}] {a.get('Name')} ({a.get('AccountType')}"
            f"{'/' + str(a.get('AccountSubType')) if a.get('AccountSubType') else ''}) "
            f"balance={a.get('CurrentBalance')}"
            for a in _rows(data, "Account")
        ],
        "No accounts found.",
    )


@tool
def quickbooks_list_items(
    limit: Annotated[int, "Maximum items to return"] = 50,
) -> str:
    """List products/services — use this to find the item_id for invoice or estimate lines."""
    data, err = _query(f"SELECT * FROM Item WHERE Active = true MAXRESULTS {_limit(limit)}")
    if err:
        return err
    return _lines(
        [
            f"- [{i.get('Id')}] {i.get('Name')} type={i.get('Type')} price={i.get('UnitPrice')}"
            for i in _rows(data, "Item")
        ],
        "No items found.",
    )


# ── Customer management ─────────────────────────────────────────────────────


@tool
def quickbooks_list_customers(limit: Annotated[int, "Maximum customers to return"] = 20) -> str:
    """List QuickBooks customers with their outstanding balance."""
    data, err = _query(f"SELECT * FROM Customer MAXRESULTS {_limit(limit)}")
    if err:
        return err
    return _lines(
        [
            f"- [{c.get('Id')}] {c.get('DisplayName')} "
            f"<{c.get('PrimaryEmailAddr', {}).get('Address', '')}> "
            f"balance={c.get('Balance')}"
            for c in _rows(data, "Customer")
        ],
        "No customers found.",
    )


@tool
def quickbooks_search_customers(
    term: Annotated[str, "Text to match against customer display name, company name, or email"],
    limit: Annotated[int, "Maximum customers to return"] = 20,
) -> str:
    """Search customers by name, company, or email fragment."""
    if not term.strip():
        return "[tool-error] quickbooks_search_customers: term is required."
    needle = _esc(term.strip())
    data, err = _query(
        f"SELECT * FROM Customer WHERE DisplayName LIKE '%{needle}%' MAXRESULTS {_limit(limit)}"
    )
    if err:
        return err
    matches = _rows(data, "Customer")
    if not matches:
        data, err = _query(
            f"SELECT * FROM Customer WHERE PrimaryEmailAddr LIKE '%{needle}%' "
            f"MAXRESULTS {_limit(limit)}"
        )
        if err:
            return err
        matches = _rows(data, "Customer")
    return _lines(
        [
            f"- [{c.get('Id')}] {c.get('DisplayName')} "
            f"<{c.get('PrimaryEmailAddr', {}).get('Address', '')}> "
            f"balance={c.get('Balance')}"
            for c in matches
        ],
        f"No customers matching '{term}'.",
    )


@tool
def quickbooks_get_customer(customer_id: Annotated[str, "QuickBooks customer Id"]) -> str:
    """Get one customer's full detail — contact info, billing address, and balance."""
    data, err = _get_json(f"customer/{customer_id}", {"minorversion": _MINOR_VERSION})
    if err:
        return err
    c = (data or {}).get("Customer", {})
    if not c:
        return f"No customer {customer_id} found."
    addr = c.get("BillAddr", {}) or {}
    address = ", ".join(
        str(addr.get(k))
        for k in ("Line1", "City", "CountrySubDivisionCode", "PostalCode")
        if addr.get(k)
    )
    return (
        f"[{c.get('Id')}] {c.get('DisplayName')}\n"
        f"  company: {c.get('CompanyName', '')}\n"
        f"  email: {c.get('PrimaryEmailAddr', {}).get('Address', '')}\n"
        f"  phone: {c.get('PrimaryPhone', {}).get('FreeFormNumber', '')}\n"
        f"  billing address: {address}\n"
        f"  balance: {c.get('Balance')}  active: {c.get('Active')}"
    )


@tool
def quickbooks_create_customer(
    display_name: Annotated[str, "Unique display name for the customer"],
    email: Annotated[str, "Primary email address"] = "",
    company_name: Annotated[str, "Company name"] = "",
    phone: Annotated[str, "Primary phone number"] = "",
) -> str:
    """Create a QuickBooks customer."""
    payload: dict[str, Any] = {"DisplayName": display_name}
    if email:
        payload["PrimaryEmailAddr"] = {"Address": email}
    if company_name:
        payload["CompanyName"] = company_name
    if phone:
        payload["PrimaryPhone"] = {"FreeFormNumber": phone}
    data, err = _post_json("customer", payload, {"minorversion": _MINOR_VERSION})
    if err:
        return err
    customer = (data or {}).get("Customer", {})
    return f"Created customer {customer.get('DisplayName')} (id={customer.get('Id')})"


@tool
def quickbooks_update_customer(
    customer_id: Annotated[str, "QuickBooks customer Id"],
    display_name: Annotated[str, "New display name; empty leaves it unchanged"] = "",
    email: Annotated[str, "New primary email; empty leaves it unchanged"] = "",
    company_name: Annotated[str, "New company name; empty leaves it unchanged"] = "",
    phone: Annotated[str, "New phone number; empty leaves it unchanged"] = "",
) -> str:
    """Update a customer's contact details. Only the fields you pass are changed."""
    updates: dict[str, Any] = {}
    if display_name:
        updates["DisplayName"] = display_name
    if email:
        updates["PrimaryEmailAddr"] = {"Address": email}
    if company_name:
        updates["CompanyName"] = company_name
    if phone:
        updates["PrimaryPhone"] = {"FreeFormNumber": phone}
    if not updates:
        return "[tool-error] quickbooks_update_customer: pass at least one field to change."

    token, err = _sync_token("customer", customer_id)
    if err:
        return err
    payload: dict[str, Any] = {"Id": customer_id, "SyncToken": token, "sparse": True, **updates}
    data, err = _post_json("customer", payload, {"minorversion": _MINOR_VERSION})
    if err:
        return err
    customer = (data or {}).get("Customer", {})
    return (
        f"Updated customer {customer.get('DisplayName')} (id={customer.get('Id')}); "
        f"changed: {', '.join(sorted(updates))}"
    )


# ── Vendors ─────────────────────────────────────────────────────────────────


@tool
def quickbooks_list_vendors(limit: Annotated[int, "Maximum vendors to return"] = 20) -> str:
    """List QuickBooks vendors with their outstanding balance."""
    data, err = _query(f"SELECT * FROM Vendor MAXRESULTS {_limit(limit)}")
    if err:
        return err
    return _lines(
        [
            f"- [{v.get('Id')}] {v.get('DisplayName')} "
            f"<{v.get('PrimaryEmailAddr', {}).get('Address', '')}> "
            f"balance={v.get('Balance')}"
            for v in _rows(data, "Vendor")
        ],
        "No vendors found.",
    )


@tool
def quickbooks_create_vendor(
    display_name: Annotated[str, "Unique display name for the vendor"],
    email: Annotated[str, "Primary email address"] = "",
    company_name: Annotated[str, "Company name"] = "",
    phone: Annotated[str, "Primary phone number"] = "",
) -> str:
    """Create a QuickBooks vendor (a supplier you receive bills from)."""
    payload: dict[str, Any] = {"DisplayName": display_name}
    if email:
        payload["PrimaryEmailAddr"] = {"Address": email}
    if company_name:
        payload["CompanyName"] = company_name
    if phone:
        payload["PrimaryPhone"] = {"FreeFormNumber": phone}
    data, err = _post_json("vendor", payload, {"minorversion": _MINOR_VERSION})
    if err:
        return err
    vendor = (data or {}).get("Vendor", {})
    return f"Created vendor {vendor.get('DisplayName')} (id={vendor.get('Id')})"


# ── Invoicing ───────────────────────────────────────────────────────────────


@tool
def quickbooks_create_invoice(
    customer_id: Annotated[str, "QuickBooks customer Id to bill"],
    amount: Annotated[float, "Line amount, when building a single-line invoice"] = 0.0,
    description: Annotated[str, "Line description, when building a single-line invoice"] = "",
    due_date: Annotated[str, "Due date as YYYY-MM-DD; empty uses the customer's terms"] = "",
    item_id: Annotated[
        str, "Product/service Id for the line; empty resolves the first active item"
    ] = "",
    email: Annotated[str, "Billing email to record on the invoice"] = "",
    line_items: Annotated[
        str,
        'Optional JSON array for multi-line invoices, e.g. [{"amount": 100, "description": '
        '"Design", "item_id": "1"}, {"quantity": 2, "unit_price": 50, "item_id": "3"}]. '
        "Overrides amount/description when provided.",
    ] = "",
) -> str:
    """Create a QuickBooks invoice with one or many sales lines."""
    lines, err = _sales_lines(amount, description, item_id, line_items)
    if err:
        return err
    payload: dict[str, Any] = {"CustomerRef": {"value": customer_id}, "Line": lines}
    if due_date:
        payload["DueDate"] = due_date
    if email:
        payload["BillEmail"] = {"Address": email}
    data, err = _post_json("invoice", payload, {"minorversion": _MINOR_VERSION})
    if err:
        return err
    invoice = (data or {}).get("Invoice", {})
    return (
        f"Created invoice {invoice.get('DocNumber', invoice.get('Id'))} "
        f"(id={invoice.get('Id')}) total={invoice.get('TotalAmt')} "
        f"balance={invoice.get('Balance')}"
    )


@tool
def quickbooks_list_invoices(
    limit: Annotated[int, "Maximum invoices to return"] = 20,
    customer_id: Annotated[str, "Only invoices for this customer Id; empty lists all"] = "",
    unpaid_only: Annotated[bool, "Only invoices with an outstanding balance"] = False,
) -> str:
    """List recent QuickBooks invoices, optionally filtered by customer or unpaid status."""
    clauses: list[str] = []
    if customer_id.strip():
        clauses.append(f"CustomerRef = '{_esc(customer_id.strip())}'")
    if unpaid_only:
        clauses.append("Balance > '0'")
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    data, err = _query(f"SELECT * FROM Invoice {where}MAXRESULTS {_limit(limit)}")
    if err:
        return err
    return _lines(
        [
            f"- [{i.get('Id')}] doc={i.get('DocNumber')} "
            f"customer={_ref_name(i, 'CustomerRef')} date={i.get('TxnDate')} "
            f"due={i.get('DueDate')} total={i.get('TotalAmt')} balance={i.get('Balance')}"
            for i in _rows(data, "Invoice")
        ],
        "No invoices found.",
    )


@tool
def quickbooks_get_invoice(invoice_id: Annotated[str, "QuickBooks invoice Id"]) -> str:
    """Get one invoice's full detail, including every line item."""
    data, err = _get_json(f"invoice/{invoice_id}", {"minorversion": _MINOR_VERSION})
    if err:
        return err
    inv = (data or {}).get("Invoice", {})
    if not inv:
        return f"No invoice {invoice_id} found."
    lines = [
        f"  - {ln.get('Description', '(no description)')}: {ln.get('Amount')}"
        for ln in inv.get("Line", [])
        if isinstance(ln, dict) and ln.get("DetailType") == "SalesItemLineDetail"
    ]
    return (
        f"[{inv.get('Id')}] doc={inv.get('DocNumber')} "
        f"customer={_ref_name(inv, 'CustomerRef')}\n"
        f"  date: {inv.get('TxnDate')}  due: {inv.get('DueDate')}\n"
        f"  total: {inv.get('TotalAmt')}  balance: {inv.get('Balance')}\n"
        f"  email: {inv.get('BillEmail', {}).get('Address', '')}\n"
        f"  lines:\n" + ("\n".join(lines) if lines else "  (none)")
    )


@tool
def quickbooks_send_invoice(
    invoice_id: Annotated[str, "QuickBooks invoice Id"],
    email: Annotated[str, "Recipient address; empty uses the invoice's stored billing email"] = "",
) -> str:
    """Email an invoice to the customer through QuickBooks."""
    params: dict[str, Any] = {"minorversion": _MINOR_VERSION}
    if email.strip():
        params["sendTo"] = email.strip()
    data, err = _post_empty(f"invoice/{invoice_id}/send", params)
    if err:
        return err
    inv = (data or {}).get("Invoice", {})
    target = email.strip() or inv.get("BillEmail", {}).get("Address", "the stored address")
    return (
        f"Sent invoice {inv.get('DocNumber', invoice_id)} (id={inv.get('Id', invoice_id)}) "
        f"to {target}; status={inv.get('EmailStatus', 'unknown')}"
    )


@tool
def quickbooks_void_invoice(invoice_id: Annotated[str, "QuickBooks invoice Id"]) -> str:
    """Void an invoice — it stays on the books at zero value, preserving the audit trail."""
    token, err = _sync_token("invoice", invoice_id)
    if err:
        return err
    data, err = _post_json(
        "invoice",
        {"Id": invoice_id, "SyncToken": token},
        {"operation": "void", "minorversion": _MINOR_VERSION},
    )
    if err:
        return err
    inv = (data or {}).get("Invoice", {})
    return (
        f"Voided invoice {inv.get('DocNumber', invoice_id)} (id={inv.get('Id', invoice_id)}); "
        f"total is now {inv.get('TotalAmt')}"
    )


# ── Estimates & sales receipts ──────────────────────────────────────────────


@tool
def quickbooks_list_estimates(
    limit: Annotated[int, "Maximum estimates to return"] = 20,
    customer_id: Annotated[str, "Only estimates for this customer Id; empty lists all"] = "",
) -> str:
    """List recent QuickBooks estimates (quotes)."""
    where = f"WHERE CustomerRef = '{_esc(customer_id.strip())}' " if customer_id.strip() else ""
    data, err = _query(f"SELECT * FROM Estimate {where}MAXRESULTS {_limit(limit)}")
    if err:
        return err
    return _lines(
        [
            f"- [{e.get('Id')}] doc={e.get('DocNumber')} "
            f"customer={_ref_name(e, 'CustomerRef')} date={e.get('TxnDate')} "
            f"total={e.get('TotalAmt')} status={e.get('TxnStatus')}"
            for e in _rows(data, "Estimate")
        ],
        "No estimates found.",
    )


@tool
def quickbooks_create_estimate(
    customer_id: Annotated[str, "QuickBooks customer Id to quote"],
    amount: Annotated[float, "Line amount, when building a single-line estimate"] = 0.0,
    description: Annotated[str, "Line description, when building a single-line estimate"] = "",
    expiration_date: Annotated[str, "Expiration date as YYYY-MM-DD"] = "",
    item_id: Annotated[
        str, "Product/service Id for the line; empty resolves the first active item"
    ] = "",
    line_items: Annotated[
        str, "Optional JSON array for multi-line estimates — same shape as create_invoice."
    ] = "",
) -> str:
    """Create a QuickBooks estimate (quote) that can later be converted to an invoice."""
    lines, err = _sales_lines(amount, description, item_id, line_items)
    if err:
        return err
    payload: dict[str, Any] = {"CustomerRef": {"value": customer_id}, "Line": lines}
    if expiration_date:
        payload["ExpirationDate"] = expiration_date
    data, err = _post_json("estimate", payload, {"minorversion": _MINOR_VERSION})
    if err:
        return err
    est = (data or {}).get("Estimate", {})
    return (
        f"Created estimate {est.get('DocNumber', est.get('Id'))} "
        f"(id={est.get('Id')}) total={est.get('TotalAmt')}"
    )


@tool
def quickbooks_list_sales_receipts(
    limit: Annotated[int, "Maximum sales receipts to return"] = 20,
) -> str:
    """List recent QuickBooks sales receipts (paid-at-point-of-sale transactions)."""
    data, err = _query(f"SELECT * FROM SalesReceipt MAXRESULTS {_limit(limit)}")
    if err:
        return err
    return _lines(
        [
            f"- [{s.get('Id')}] doc={s.get('DocNumber')} "
            f"customer={_ref_name(s, 'CustomerRef')} date={s.get('TxnDate')} "
            f"total={s.get('TotalAmt')}"
            for s in _rows(data, "SalesReceipt")
        ],
        "No sales receipts found.",
    )


@tool
def quickbooks_create_sales_receipt(
    customer_id: Annotated[str, "QuickBooks customer Id"],
    amount: Annotated[float, "Line amount, when building a single-line receipt"] = 0.0,
    description: Annotated[str, "Line description, when building a single-line receipt"] = "",
    item_id: Annotated[
        str, "Product/service Id for the line; empty resolves the first active item"
    ] = "",
    line_items: Annotated[
        str, "Optional JSON array for multi-line receipts — same shape as create_invoice."
    ] = "",
) -> str:
    """Record a sale that was paid immediately — no invoice, no receivable."""
    lines, err = _sales_lines(amount, description, item_id, line_items)
    if err:
        return err
    payload: dict[str, Any] = {"CustomerRef": {"value": customer_id}, "Line": lines}
    data, err = _post_json("salesreceipt", payload, {"minorversion": _MINOR_VERSION})
    if err:
        return err
    receipt = (data or {}).get("SalesReceipt", {})
    return (
        f"Created sales receipt {receipt.get('DocNumber', receipt.get('Id'))} "
        f"(id={receipt.get('Id')}) total={receipt.get('TotalAmt')}"
    )


# ── Payments ────────────────────────────────────────────────────────────────


@tool
def quickbooks_list_payments(
    limit: Annotated[int, "Maximum payments to return"] = 20,
    customer_id: Annotated[str, "Only payments from this customer Id; empty lists all"] = "",
) -> str:
    """List recent QuickBooks customer payments."""
    where = f"WHERE CustomerRef = '{_esc(customer_id.strip())}' " if customer_id.strip() else ""
    data, err = _query(f"SELECT * FROM Payment {where}MAXRESULTS {_limit(limit)}")
    if err:
        return err
    return _lines(
        [
            f"- [{p.get('Id')}] customer={_ref_name(p, 'CustomerRef')} "
            f"date={p.get('TxnDate')} total={p.get('TotalAmt')} "
            f"unapplied={p.get('UnappliedAmt')}"
            for p in _rows(data, "Payment")
        ],
        "No payments found.",
    )


@tool
def quickbooks_record_payment(
    customer_id: Annotated[str, "QuickBooks customer Id who paid"],
    amount: Annotated[float, "Payment amount received"],
    invoice_id: Annotated[
        str, "Invoice Id to apply this against; empty leaves the payment unapplied"
    ] = "",
    payment_date: Annotated[str, "Payment date as YYYY-MM-DD; empty uses today"] = "",
) -> str:
    """Record a customer payment, optionally applying it to a specific invoice."""
    payload: dict[str, Any] = {
        "CustomerRef": {"value": customer_id},
        "TotalAmt": float(amount),
    }
    if payment_date:
        payload["TxnDate"] = payment_date
    if invoice_id.strip():
        payload["Line"] = [
            {
                "Amount": float(amount),
                "LinkedTxn": [{"TxnId": invoice_id.strip(), "TxnType": "Invoice"}],
            }
        ]
    data, err = _post_json("payment", payload, {"minorversion": _MINOR_VERSION})
    if err:
        return err
    payment = (data or {}).get("Payment", {})
    applied = f" applied to invoice {invoice_id}" if invoice_id.strip() else " (unapplied)"
    return (
        f"Recorded payment of {payment.get('TotalAmt')} from "
        f"{_ref_name(payment, 'CustomerRef')} (id={payment.get('Id')}){applied}"
    )


# ── Expenses: bills, bill payments, and direct spend ────────────────────────


@tool
def quickbooks_list_bills(
    limit: Annotated[int, "Maximum bills to return"] = 20,
    vendor_id: Annotated[str, "Only bills from this vendor Id; empty lists all"] = "",
    unpaid_only: Annotated[bool, "Only bills with an outstanding balance"] = False,
) -> str:
    """List recent QuickBooks bills (money you owe vendors)."""
    clauses: list[str] = []
    if vendor_id.strip():
        clauses.append(f"VendorRef = '{_esc(vendor_id.strip())}'")
    if unpaid_only:
        clauses.append("Balance > '0'")
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    data, err = _query(f"SELECT * FROM Bill {where}MAXRESULTS {_limit(limit)}")
    if err:
        return err
    return _lines(
        [
            f"- [{b.get('Id')}] vendor={_ref_name(b, 'VendorRef')} "
            f"date={b.get('TxnDate')} due={b.get('DueDate')} "
            f"total={b.get('TotalAmt')} balance={b.get('Balance')}"
            for b in _rows(data, "Bill")
        ],
        "No bills found.",
    )


@tool
def quickbooks_create_bill(
    vendor_id: Annotated[str, "QuickBooks vendor Id the bill is from"],
    amount: Annotated[float, "Line amount, when building a single-line bill"] = 0.0,
    description: Annotated[str, "Line description, when building a single-line bill"] = "",
    account_id: Annotated[
        str, "Expense account Id to categorize the line (see quickbooks_list_accounts)"
    ] = "",
    due_date: Annotated[str, "Due date as YYYY-MM-DD; empty uses the vendor's terms"] = "",
    line_items: Annotated[
        str,
        'Optional JSON array for multi-line bills, e.g. [{"amount": 250, "description": '
        '"Hosting", "account_id": "63"}]. Overrides amount/description when provided.',
    ] = "",
) -> str:
    """Record a vendor bill (an expense you owe and will pay later)."""
    lines, err = _expense_lines(amount, description, account_id, line_items)
    if err:
        return err
    payload: dict[str, Any] = {"VendorRef": {"value": vendor_id}, "Line": lines}
    if due_date:
        payload["DueDate"] = due_date
    data, err = _post_json("bill", payload, {"minorversion": _MINOR_VERSION})
    if err:
        return err
    bill = (data or {}).get("Bill", {})
    return (
        f"Created bill (id={bill.get('Id')}) for {_ref_name(bill, 'VendorRef')} "
        f"total={bill.get('TotalAmt')} balance={bill.get('Balance')}"
    )


@tool
def quickbooks_pay_bill(
    vendor_id: Annotated[str, "QuickBooks vendor Id being paid"],
    bill_id: Annotated[str, "Bill Id to pay"],
    amount: Annotated[float, "Amount to pay against the bill"],
    bank_account_id: Annotated[
        str, "Bank/credit account Id the money comes from (see quickbooks_list_accounts)"
    ],
    pay_type: Annotated[str, "Payment type: 'Check' or 'CreditCard'"] = "Check",
) -> str:
    """Pay an outstanding vendor bill from a bank or credit-card account."""
    kind = _enum(pay_type, ("Check", "CreditCard"))
    if kind is None:
        return "[tool-error] quickbooks_pay_bill: pay_type must be 'Check' or 'CreditCard'."
    payload: dict[str, Any] = {
        "VendorRef": {"value": vendor_id},
        "TotalAmt": float(amount),
        "PayType": kind,
        "Line": [
            {
                "Amount": float(amount),
                "LinkedTxn": [{"TxnId": bill_id, "TxnType": "Bill"}],
            }
        ],
    }
    account_ref = {
        "BankAccountRef" if kind == "Check" else "CCAccountRef": {"value": bank_account_id}
    }
    payload["CheckPayment" if kind == "Check" else "CreditCardPayment"] = account_ref
    data, err = _post_json("billpayment", payload, {"minorversion": _MINOR_VERSION})
    if err:
        return err
    payment = (data or {}).get("BillPayment", {})
    return (
        f"Paid {payment.get('TotalAmt')} to {_ref_name(payment, 'VendorRef')} "
        f"against bill {bill_id} (payment id={payment.get('Id')})"
    )


@tool
def quickbooks_list_expenses(
    limit: Annotated[int, "Maximum expenses to return"] = 20,
) -> str:
    """List recent cash, check, and credit-card expenses (QuickBooks Purchase records)."""
    data, err = _query(f"SELECT * FROM Purchase MAXRESULTS {_limit(limit)}")
    if err:
        return err
    return _lines(
        [
            f"- [{p.get('Id')}] {p.get('PaymentType')} "
            f"payee={_ref_name(p, 'EntityRef') or _ref_name(p, 'AccountRef')} "
            f"date={p.get('TxnDate')} total={p.get('TotalAmt')}"
            for p in _rows(data, "Purchase")
        ],
        "No expenses found.",
    )


@tool
def quickbooks_create_expense(
    payment_account_id: Annotated[
        str, "Bank or credit-card account Id the money came from (see quickbooks_list_accounts)"
    ],
    amount: Annotated[float, "Line amount, when building a single-line expense"] = 0.0,
    description: Annotated[str, "Line description, when building a single-line expense"] = "",
    account_id: Annotated[
        str, "Expense account Id to categorize the line (see quickbooks_list_accounts)"
    ] = "",
    payment_type: Annotated[str, "Payment type: 'Cash', 'Check', or 'CreditCard'"] = "Cash",
    vendor_id: Annotated[str, "Vendor Id the money was paid to; optional"] = "",
    expense_date: Annotated[str, "Expense date as YYYY-MM-DD; empty uses today"] = "",
    line_items: Annotated[
        str, "Optional JSON array for multi-line expenses — same shape as create_bill."
    ] = "",
) -> str:
    """Record money already spent by cash, check, or card — no bill, no payable."""
    kind = _enum(payment_type, ("Cash", "Check", "CreditCard"))
    if kind is None:
        return (
            "[tool-error] quickbooks_create_expense: payment_type must be "
            "'Cash', 'Check', or 'CreditCard'."
        )
    lines, err = _expense_lines(amount, description, account_id, line_items)
    if err:
        return err
    payload: dict[str, Any] = {
        "AccountRef": {"value": payment_account_id},
        "PaymentType": kind,
        "Line": lines,
    }
    if vendor_id.strip():
        payload["EntityRef"] = {"value": vendor_id.strip(), "type": "Vendor"}
    if expense_date:
        payload["TxnDate"] = expense_date
    data, err = _post_json("purchase", payload, {"minorversion": _MINOR_VERSION})
    if err:
        return err
    purchase = (data or {}).get("Purchase", {})
    return (
        f"Recorded {kind} expense of {purchase.get('TotalAmt')} "
        f"(id={purchase.get('Id')}) on {purchase.get('TxnDate')}"
    )


# ── Business insights (reports) ─────────────────────────────────────────────


@tool
def quickbooks_profit_and_loss(
    start_date: Annotated[str, "Period start as YYYY-MM-DD"],
    end_date: Annotated[str, "Period end as YYYY-MM-DD"],
    accounting_method: Annotated[str, "'Accrual' or 'Cash'"] = "Accrual",
) -> str:
    """Fetch a QuickBooks Profit and Loss report for a date range."""
    return _report(
        "ProfitAndLoss",
        {
            "start_date": start_date,
            "end_date": end_date,
            "accounting_method": accounting_method,
        },
    )


@tool
def quickbooks_balance_sheet(
    as_of_date: Annotated[str, "Balance date as YYYY-MM-DD; empty uses today"] = "",
    accounting_method: Annotated[str, "'Accrual' or 'Cash'"] = "Accrual",
) -> str:
    """Fetch a QuickBooks Balance Sheet report."""
    return _report(
        "BalanceSheet",
        {"as_of_date": as_of_date, "accounting_method": accounting_method},
    )


@tool
def quickbooks_cash_flow(
    start_date: Annotated[str, "Period start as YYYY-MM-DD"],
    end_date: Annotated[str, "Period end as YYYY-MM-DD"],
) -> str:
    """Fetch a QuickBooks Statement of Cash Flows for a date range."""
    return _report("CashFlow", {"start_date": start_date, "end_date": end_date})


@tool
def quickbooks_ar_aging(
    as_of_date: Annotated[str, "Aging date as YYYY-MM-DD; empty uses today"] = "",
) -> str:
    """Fetch the A/R aging summary — who owes you money and how overdue it is."""
    return _report("AgedReceivables", {"report_date": as_of_date})


@tool
def quickbooks_ap_aging(
    as_of_date: Annotated[str, "Aging date as YYYY-MM-DD; empty uses today"] = "",
) -> str:
    """Fetch the A/P aging summary — who you owe and how overdue it is."""
    return _report("AgedPayables", {"report_date": as_of_date})


@tool
def quickbooks_sales_by_customer(
    start_date: Annotated[str, "Period start as YYYY-MM-DD"],
    end_date: Annotated[str, "Period end as YYYY-MM-DD"],
) -> str:
    """Fetch total sales broken down by customer for a date range."""
    return _report("CustomerSales", {"start_date": start_date, "end_date": end_date})


@tool
def quickbooks_expenses_by_vendor(
    start_date: Annotated[str, "Period start as YYYY-MM-DD"],
    end_date: Annotated[str, "Period end as YYYY-MM-DD"],
) -> str:
    """Fetch total expenses broken down by vendor for a date range."""
    return _report("VendorExpenses", {"start_date": start_date, "end_date": end_date})


@tool
def quickbooks_trial_balance(
    start_date: Annotated[str, "Period start as YYYY-MM-DD"],
    end_date: Annotated[str, "Period end as YYYY-MM-DD"],
    accounting_method: Annotated[str, "'Accrual' or 'Cash'"] = "Accrual",
) -> str:
    """Fetch a trial balance — every account's debit and credit totals for a period."""
    return _report(
        "TrialBalance",
        {
            "start_date": start_date,
            "end_date": end_date,
            "accounting_method": accounting_method,
        },
    )


get_tools, get_tool = make_toolkit_api(globals())
