"""A keyless stdio MCP server whose tools return results too large to keep inline.

Used by ``tests/test_mcp_offload_preview_e2e.py`` to drive a real MCP result
through the agent build, the security pipeline and ``McpResultCapMiddleware``
with the offline ``fake:`` model. The payloads are deterministic and shaped
like what real servers send: one minified JSON line (a people search) and a
CSV export, each hiding one failed record in the middle.
"""

from __future__ import annotations

import json


def people_payload() -> str:
    people = []
    for i in range(120):
        people.append(
            {
                "id": f"p-{i:04d}",
                "name": f"Person {i}",
                "title": ["VP Sales", "Head of Growth", "CTO", "Founder"][i % 4],
                "email_status": "verified" if i % 3 else "guessed",
                "company": {"id": f"org-{i % 7}", "employees": 50 + i * 3},
                "headline": "Helping teams grow revenue with data-driven playbooks. " * 3,
            }
        )
    people[37]["email_status"] = "error"
    people[37]["note"] = "delivery failed: mailbox rejected"
    return json.dumps(
        {"people": people, "pagination": {"page": 1, "per_page": 120, "total_entries": 4812}},
        separators=(",", ":"),
    )


def orders_payload() -> str:
    rows = ["order_id,customer,status,amount,currency"]
    for i in range(900):
        status = ["fulfilled", "fulfilled", "fulfilled", "shipped"][i % 4]
        rows.append(f"ord-{10000 + i},cust-{i % 41:03d},{status},{12 + i % 80}.40,SGD")
    rows[512] = "ord-10511,cust-019,FAILED card declined,0.00,SGD"
    return "\n".join(rows)


def main() -> None:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("olano-large-results")

    def search_people(query: str = "") -> str:
        """Search people (returns 120 profiles as one JSON document)."""
        return people_payload()

    def export_orders() -> str:
        """Export every order as CSV."""
        return orders_payload()

    server.add_tool(search_people)
    server.add_tool(export_orders)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
