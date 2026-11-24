"""Harbor & Pine, an online home-goods shop: the team in the tour video."""

ORDERS_CSV = """\
product,category,units,revenue
Linen throw,Textiles,212,8268
Stoneware mug set,Kitchen,187,5423
Oak serving board,Kitchen,141,6768
Beeswax candle trio,Home scent,133,3059
Wool floor cushion,Textiles,58,4002
"""

BESTSELLERS = """\
# Best sellers, last week

| Product | Units | Revenue |
|---|---|---|
| Linen throw | 212 | $8,268 |
| Stoneware mug set | 187 | $5,423 |
| Oak serving board | 141 | $6,768 |

The linen throw leads for the third week running. Oak serving boards earn the
most per unit and are down to nine days of stock.

**Recommendation:** reorder oak serving boards this week.
"""

TEAM = {
    "default_agent": "nova",
    "agents": [
        {
            "name": "atlas",
            "emoji": "🧭",
            "description": "Runs day-to-day operations and hands work to the team.",
            "title": "Head of Operations",
            "manager": None,
            "reports": ["aria", "nova", "quill"],
        },
        {
            "name": "nova",
            "emoji": "🔭",
            "description": "Tracks sales, stock and suppliers; writes the reports.",
            "title": "Research Analyst",
            "manager": "atlas",
        },
        {
            "name": "aria",
            "emoji": "💬",
            "description": "Answers customers by email and chat.",
            "title": "Customer Support",
            "manager": "atlas",
        },
        {
            "name": "quill",
            "emoji": "✍️",
            "description": "Writes product pages and the weekly newsletter.",
            "title": "Content Writer",
            "manager": "atlas",
        },
    ],
    "files": {
        "nova": {
            "AGENTS.md": "# nova\n\nResearch analyst for Harbor & Pine. Save reports in reports/.\n",
            "data/orders-last-week.csv": ORDERS_CSV,
            "reports/supplier-review.md": "# Supplier review\n\nThree linen suppliers compared on price and lead time.\n",
        },
        "aria": {"AGENTS.md": "# aria\n\nCustomer support. Always ask before sending email.\n"},
        "quill": {"AGENTS.md": "# quill\n\nContent writer. Newsletter every Friday.\n"},
        "atlas": {
            "AGENTS.md": "# atlas\n\nHead of operations. Delegates to aria, nova and quill.\n"
        },
    },
    "memory": {
        "nova": "# MEMORY.md\n\n## Preferences\n- The owner wants weekly totals in USD.\n- Reports go in `reports/`, with a recommendation at the end.\n\n## Stock\n- Oak serving boards take three weeks to restock.\n",
    },
    "tasks": [
        {
            "name": "Morning inbox triage",
            "prompt": "Read new customer emails, answer the simple ones, list the rest for me.",
            "schedule": "0 8 * * mon-fri",
            "agent_name": "aria",
        },
        {
            "name": "Daily sales report",
            "prompt": "Summarise yesterday's orders and flag anything unusual.",
            "schedule": "30 8 * * mon-fri",
            "agent_name": "nova",
        },
        {
            "name": "Low-stock check",
            "prompt": "List every product with under two weeks of stock.",
            "schedule": "0 9 * * mon,thu",
            "agent_name": "nova",
        },
        {
            "name": "Friday newsletter draft",
            "prompt": "Draft this week's newsletter from the new arrivals.",
            "schedule": "0 14 * * fri",
            "agent_name": "quill",
        },
        {
            "name": "Weekly operations review",
            "prompt": "Review the week: orders, refunds, open issues. Write a one-page summary.",
            "schedule": "0 16 * * fri",
            "agent_name": "atlas",
        },
    ],
    "issues": [
        {
            "title": "Reorder oak serving boards",
            "body": "Nine days of stock left; restock takes three weeks.",
            "assignee": "ai-atlas",
            "labels": ["stock"],
            "type": "task",
            "comments": [],
        },
        {
            "title": "Write the autumn collection page",
            "body": "Six new products arrive on Monday.",
            "assignee": "ai-quill",
            "labels": ["content"],
            "type": "task",
            "comments": [],
        },
    ],
    "a2a": [
        {
            "from": "atlas",
            "to": "nova",
            "message": "Which products should we reorder this week?",
            "reply": "Oak serving boards first: nine days of stock, three weeks to restock.",
        },
        {
            "from": "atlas",
            "to": "quill",
            "message": "Please feature the linen throw in Friday's newsletter.",
            "reply": "Added as the lead story, with the new colours.",
        },
        {
            "from": "aria",
            "to": "atlas",
            "message": "A customer wants a refund for a late order. May I offer free shipping instead?",
            "reply": "Yes. Offer free shipping on the next order and apologise.",
        },
    ],
    "turns": {
        "nova": [
            {
                "steps": [
                    {"wait": 0.7},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "data/orders-last-week.csv"},
                        "result": ORDERS_CSV,
                        "took": 1.1,
                    },
                    {
                        "tool": "write_file",
                        "args": {"file_path": "reports/best-sellers.md"},
                        "result": "Updated file reports/best-sellers.md",
                        "took": 1.0,
                        "write": {"reports/best-sellers.md": BESTSELLERS},
                    },
                    {
                        "text": "Here are last week's best sellers:\n\n"
                        "| Product | Units | Revenue |\n|---|---|---|\n"
                        "| Linen throw | 212 | $8,268 |\n"
                        "| Stoneware mug set | 187 | $5,423 |\n"
                        "| Oak serving board | 141 | $6,768 |\n\n"
                        "**Recommendation:** reorder oak serving boards this week. Nine days of stock are left.\n\n"
                        "Saved: reports/best-sellers.md",
                        "wps": 16,
                    },
                ]
            }
        ],
        "aria": [
            {
                "steps": [
                    {"wait": 0.7},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "inbox/dana-late-order.md"},
                        "result": "From: Dana Whitfield. Order #2187 arrived six days late.",
                        "took": 0.9,
                    },
                    {
                        "text": "I've drafted an apology with free shipping on the next order. May I send it?\n\n",
                        "wps": 14,
                    },
                    {
                        "approve": [
                            {
                                "name": "gmail_send",
                                "args": {
                                    "to": "dana.whitfield@example.com",
                                    "subject": "Sorry your order was late",
                                    "body": "Hi Dana, I'm sorry order #2187 arrived late. Your next order ships free, on us.",
                                },
                                "risk": "medium",
                            }
                        ]
                    },
                    {
                        "tool": "gmail_send",
                        "args": {
                            "to": "dana.whitfield@example.com",
                            "subject": "Sorry your order was late",
                        },
                        "result": "Sent.",
                        "took": 0.9,
                    },
                    {"text": "Sent. Dana has the apology and a free-shipping code.", "wps": 14},
                ]
            }
        ],
    },
}
