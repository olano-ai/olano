"""Fernhill Yoga, a small yoga studio: the team in the pages video.

Sage writes a timetable and price list into its workspace and publishes it. The publishing step
runs the product's own ``pages_start`` tool for the agent, so the page in the video is recorded in
the real page registry and served by the runtime at ``/pages/<slug>/``, behind the viewer password
the owner sets on the agent's Pages tab during the take.
"""

import os
from pathlib import Path

SLUG = "fernhill-classes"

TIMETABLE = """\
day,time,class,teacher,spaces
Monday,07:00,Morning Flow,Ines,6
Monday,18:30,Yin and Restore,Tomas,3
Wednesday,12:15,Lunch Hour Stretch,Ines,9
Thursday,18:30,Strong Vinyasa,Kofi,2
Saturday,09:30,Beginners Welcome,Tomas,8
"""

PRICES = """\
option,price
Drop-in class,$18
Five-class pass,$80
Monthly unlimited,$129
First week (new students),$25
"""

PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fernhill Yoga: classes and prices</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: Inter, system-ui, sans-serif; background: #f6f3ec; color: #24352c; }
  header { background: #2f5d47; color: #f6f3ec; padding: 34px 24px 30px; }
  header .in, main { max-width: 760px; margin: 0 auto; }
  .tag { font-size: 12px; letter-spacing: .14em; text-transform: uppercase; opacity: .8; }
  h1 { margin: 8px 0 6px; font-size: 34px; letter-spacing: -.01em; }
  header p { margin: 0; font-size: 16px; opacity: .9; }
  main { padding: 26px 24px 48px; }
  h2 { font-size: 13px; letter-spacing: .12em; text-transform: uppercase; color: #5b7466; margin: 26px 0 12px; }
  .cls { display: flex; align-items: center; gap: 16px; background: #fff; border-radius: 14px;
         padding: 14px 18px; margin-bottom: 10px; box-shadow: 0 1px 2px rgba(36, 53, 44, .08); }
  .when { width: 108px; flex: none; }
  .when b { display: block; font-size: 15px; }
  .when span { font-size: 13px; color: #5b7466; }
  .what { flex: 1; min-width: 0; }
  .what b { display: block; font-size: 17px; }
  .what span { font-size: 13px; color: #5b7466; }
  .left { flex: none; font-size: 13px; font-weight: 600; padding: 5px 11px; border-radius: 99px;
          background: #e3efe7; color: #2f5d47; }
  .left.few { background: #fbe9d7; color: #9a4f12; }
  .prices { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .price { background: #fff; border-radius: 14px; padding: 16px 18px; box-shadow: 0 1px 2px rgba(36, 53, 44, .08); }
  .price b { display: block; font-size: 26px; color: #2f5d47; }
  .price span { font-size: 14px; color: #5b7466; }
  footer { margin-top: 28px; font-size: 13px; color: #5b7466; }
  @media (max-width: 520px) { h1 { font-size: 28px; } .when { width: 86px; } .cls { gap: 12px; padding: 13px 14px; } }
</style>
</head>
<body>
<header><div class="in">
  <div class="tag">Fernhill Yoga</div>
  <h1>This week's classes</h1>
  <p>12 Mill Lane. Book at the desk or reply to your reminder.</p>
</div></header>
<main>
  <h2>Timetable</h2>
  <div class="cls"><div class="when"><b>Monday</b><span>07:00</span></div><div class="what"><b>Morning Flow</b><span>with Ines</span></div><div class="left">6 spaces</div></div>
  <div class="cls"><div class="when"><b>Monday</b><span>18:30</span></div><div class="what"><b>Yin and Restore</b><span>with Tomas</span></div><div class="left few">3 spaces</div></div>
  <div class="cls"><div class="when"><b>Wednesday</b><span>12:15</span></div><div class="what"><b>Lunch Hour Stretch</b><span>with Ines</span></div><div class="left">9 spaces</div></div>
  <div class="cls"><div class="when"><b>Thursday</b><span>18:30</span></div><div class="what"><b>Strong Vinyasa</b><span>with Kofi</span></div><div class="left few">2 spaces</div></div>
  <div class="cls"><div class="when"><b>Saturday</b><span>09:30</span></div><div class="what"><b>Beginners Welcome</b><span>with Tomas</span></div><div class="left">8 spaces</div></div>
  <h2>Prices</h2>
  <div class="prices">
    <div class="price"><b>$18</b><span>Drop-in class</span></div>
    <div class="price"><b>$80</b><span>Five-class pass</span></div>
    <div class="price"><b>$129</b><span>Monthly unlimited</span></div>
    <div class="price"><b>$25</b><span>First week, new students</span></div>
  </div>
  <footer>Questions: hello@fernhillyoga.example</footer>
</main>
</body>
</html>
"""


def publish(ctx: dict) -> str:
    """The agent's ``pages_start`` call, for real: the product's own tool, bound to this agent."""
    from olano.security.request_context import push_request_context
    from olano.tools._credentials import agent_runtime_context
    from olano.tools.pages_tools import pages_start

    agent = ctx["agent"]
    workspace = Path(os.environ["LIONCLAW_HOME"]) / "agents" / agent / "workspace"
    with (
        agent_runtime_context(agent, {}),
        push_request_context(metadata={"workspace_root": str(workspace)}),
    ):
        return str(pages_start.invoke({"slug": SLUG, "directory": "site"}))


TEAM = {
    "default_agent": "sage",
    "agents": [
        {
            "name": "sage",
            "emoji": "🌿",
            "description": "Keeps the timetable, answers students and publishes studio pages.",
            "title": "Studio Assistant",
            "manager": None,
            "reports": ["rowan", "lark"],
        },
        {
            "name": "rowan",
            "emoji": "🎟️",
            "description": "Memberships, passes and renewals.",
            "title": "Memberships",
            "manager": "sage",
        },
        {
            "name": "lark",
            "emoji": "📣",
            "description": "Newsletter and class announcements.",
            "title": "Announcements",
            "manager": "sage",
        },
    ],
    "files": {
        "sage": {
            "AGENTS.md": "# sage\n\nStudio assistant at Fernhill Yoga. Pages go in site/ and are published with pages_start.\n",
            "studio/timetable.csv": TIMETABLE,
            "studio/prices.csv": PRICES,
        },
        "rowan": {"AGENTS.md": "# rowan\n\nMemberships at Fernhill Yoga.\n"},
        "lark": {"AGENTS.md": "# lark\n\nAnnouncements at Fernhill Yoga.\n"},
    },
    "turns": {
        "sage": [
            {
                "steps": [
                    {"wait": 0.8},
                    {
                        "tool": "read_file",
                        "args": {"file_path": "studio/timetable.csv"},
                        "result": TIMETABLE,
                        "took": 1.1,
                    },
                    {
                        "tool": "read_file",
                        "args": {"file_path": "studio/prices.csv"},
                        "result": PRICES,
                        "took": 0.9,
                    },
                    {
                        "tool": "write_file",
                        "args": {"file_path": "site/index.html"},
                        "result": "Updated file site/index.html",
                        "took": 1.6,
                        "write": {"site/index.html": PAGE},
                    },
                    {
                        "tool": "pages_start",
                        "args": {"slug": SLUG, "directory": "site"},
                        "result": publish,
                        "took": 1.2,
                    },
                    {
                        "text": "The page is published: five classes and four prices, from the studio files.\n\n"
                        f"Address: `/pages/{SLUG}/`\n\n"
                        "It stays locked until you set a viewer password on my **Pages** tab. "
                        "I cannot see or change that password.",
                        "wps": 14,
                    },
                ]
            }
        ],
    },
}
