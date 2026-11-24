"""Kettle Hill Coffee, a roaster with two shops: the team in the chat video."""

from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "assets"

SALES_CSV = """\
month,harbour_shop,market_shop,total
May,22900,18300,41200
Jun,24100,19800,43900
Jul,23400,18700,42100
Aug,25600,20200,45800
Sep,26100,21200,47300
Oct,30400,24200,54600
"""

REPORT = """\
# October sales report

October revenue was **$54,600**, up 15% on September and the best month of the year.

| Shop | September | October | Change |
|---|---|---|---|
| Harbour shop | $26,100 | $30,400 | +16% |
| Market shop | $21,200 | $24,200 | +14% |

## What drove it

- The autumn blend launched on 3 October and made up 22% of bean sales.
- Saturday footfall at the Harbour shop rose after the new opening hours.

**Recommendation:** keep the autumn blend through December and extend Saturday hours at the Market shop.
"""

TEAM = {
    "default_agent": "nova",
    "agents": [
        {
            "name": "nova",
            "emoji": "🔭",
            "description": "Sales and market analyst.",
            "title": "Analyst",
            "manager": "atlas",
        },
        {
            "name": "atlas",
            "emoji": "🧭",
            "description": "Runs day-to-day operations.",
            "title": "Operations Lead",
            "reports": ["nova", "juno"],
        },
        {
            "name": "juno",
            "emoji": "☕",
            "description": "Answers customers and wholesale buyers.",
            "title": "Customer Care",
            "manager": "atlas",
        },
    ],
    "files": {
        "nova": {
            "AGENTS.md": "# nova\n\nAnalyst for Kettle Hill Coffee. Save reports in reports/.\n",
            "data/sales-2026.csv": SALES_CSV,
            "reports/september-sales.md": "# September sales report\n\nRevenue was $47,300, up 3% on August.\n",
        },
    },
    "memory": {
        "nova": "# MEMORY.md\n\n## Preferences\n- The owner wants monthly totals in USD, both shops together.\n- Every report ends with a recommendation.\n",
    },
    "turns": {
        "nova": [
            {
                "match": "why",
                "steps": [
                    {"wait": 0.8},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "data/sales-2026.csv"},
                        "result": SALES_CSV,
                        "took": 1.0,
                    },
                    {
                        "text": "Two things stand out.\n\n"
                        "1. **The autumn blend.** It launched on 3 October and made up 22% of bean sales.\n"
                        "2. **Saturdays at the Harbour shop.** Footfall rose after the new opening hours.\n\n"
                        "Without the autumn blend, October would have been close to September.",
                        "wps": 15,
                    },
                ],
            },
            {
                "steps": [
                    {"wait": 0.8},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "data/sales-2026.csv"},
                        "result": SALES_CSV,
                        "took": 1.3,
                    },
                    {
                        "tool": "execute",
                        "args": {"command": "python3 analyse.py data/sales-2026.csv"},
                        "result": "Oct total 54600 (+15.4% vs Sep). Best month of 2026.",
                        "took": 1.5,
                    },
                    {
                        "tool": "render_html_image",
                        "args": {
                            "html": "reports/october-sales.html",
                            "save_path": "reports/october-sales.png",
                        },
                        "result": "Saved reports/october-sales.png (1200x760)",
                        "took": 1.6,
                        "write": {
                            "reports/october-sales.png": (ASSETS / "chat-sales.png").read_bytes()
                        },
                    },
                    {
                        "tool": "write_file",
                        "args": {"file_path": "reports/october-sales.md"},
                        "result": "Updated file reports/october-sales.md",
                        "took": 1.0,
                        "write": {"reports/october-sales.md": REPORT},
                    },
                    {
                        "text": "October was the best month of the year: **$54,600**, up **15%** on September. "
                        "Both shops grew, the Harbour shop fastest.\n\n"
                        "Chart: reports/october-sales.png\n\n"
                        "Saved: reports/october-sales.md",
                        "wps": 15,
                    },
                ],
            },
        ],
    },
}
