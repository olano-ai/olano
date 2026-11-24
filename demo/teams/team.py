"""Fieldnote Studio, a small marketing studio: the team in the team video."""

RESEARCH = (
    "Three findings for the Tidewater launch:\n"
    "1. Buyers search for 'refillable' twice as often as 'eco'.\n"
    "2. The two closest rivals price a starter kit at $28 and $32.\n"
    "3. Instagram Reels drove 61% of their launch-week traffic."
)
COPY = (
    "Headline: Clean that refills, not landfills.\n"
    "Subhead: The Tidewater starter kit, $26. One bottle, for good.\n"
    "Email subject: Your last plastic bottle."
)
PLAN = (
    "Here is the launch plan for Tidewater.\n\n"
    "**Positioning.** Lead with *refillable*, the word buyers search for most.\n\n"
    "**Price.** Starter kit at **$26**, under both rivals.\n\n"
    "| Week | What happens | Owner |\n|---|---|---|\n"
    "| 1 | Teaser Reels, waitlist page | Quill |\n"
    "| 2 | Launch email, starter-kit offer | Quill |\n"
    "| 3 | Results review, next steps | Nova |\n\n"
    "**Headline.** *Clean that refills, not landfills.*\n\n"
    "Nova's research and Quill's copy are both in. I opened three issues to track the work."
)

TEAM = {
    "default_agent": "atlas",
    "agents": [
        {
            "name": "atlas",
            "emoji": "🧭",
            "description": "Leads client projects and hands work to the team.",
            "title": "Project Lead",
            "manager": None,
            "reports": ["nova", "quill", "pixel"],
        },
        {
            "name": "nova",
            "emoji": "🔭",
            "description": "Market and competitor research.",
            "title": "Researcher",
            "manager": "atlas",
        },
        {
            "name": "quill",
            "emoji": "✍️",
            "description": "Headlines, emails and product copy.",
            "title": "Copywriter",
            "manager": "atlas",
        },
        {
            "name": "pixel",
            "emoji": "🎨",
            "description": "Social images and ad layouts.",
            "title": "Designer",
            "manager": "atlas",
        },
    ],
    "files": {
        "atlas": {
            "AGENTS.md": "# atlas\n\nProject lead at Fieldnote Studio. Ask nova for research and quill for copy.\n"
        },
    },
    "a2a": [
        {
            "from": "atlas",
            "to": "pixel",
            "message": "Can you resize the Harlow banner for mobile?",
            "reply": "Done. Three sizes are in the shared folder.",
        },
        {
            "from": "quill",
            "to": "nova",
            "message": "What do customers call the product in reviews?",
            "reply": "Mostly 'the refill bottle'. Almost nobody says 'dispenser'.",
        },
    ],
    "issues": [
        {
            "title": "Tidewater: teaser Reels and waitlist page",
            "body": "Week 1 of the launch plan. Three Reels and a one-page waitlist.",
            "author": "ai-atlas",
            "assignee": "ai-quill",
            "labels": ["tidewater", "launch"],
            "type": "task",
            "comments": [
                ("ai-quill", "Picking this up. First Reel script by tomorrow."),
                ("ai-pixel", "I can do the waitlist page layout once the headline is final."),
            ],
        },
        {
            "title": "Tidewater: launch email",
            "body": "Week 2. Subject line and body from the approved copy.",
            "author": "ai-atlas",
            "assignee": "ai-quill",
            "labels": ["tidewater", "launch"],
            "type": "task",
            "comments": [],
        },
        {
            "title": "Tidewater: results review",
            "body": "Week 3. Traffic, sign-ups and sales against the plan.",
            "author": "ai-atlas",
            "assignee": "ai-nova",
            "labels": ["tidewater"],
            "type": "task",
            "comments": [],
        },
        {
            "title": "Harlow banner looks cropped on phones",
            "body": "The client noticed the logo is cut off on small screens.",
            "author": "human-owner",
            "assignee": "ai-pixel",
            "labels": ["harlow", "bug"],
            "type": "bug",
            "comments": [("ai-pixel", "Fixed. Resized for three widths and checked on a phone.")],
            "close": "ai-pixel",
        },
        {
            "title": "Competitor price check for Tidewater",
            "body": "What do the closest rivals charge for a starter kit?",
            "author": "ai-atlas",
            "assignee": "ai-nova",
            "labels": ["tidewater", "research"],
            "type": "task",
            "comments": [("ai-nova", "$28 and $32. Details are in the research note.")],
            "close": "ai-nova",
        },
    ],
    "turns": {
        "atlas": [
            {
                "steps": [
                    {"wait": 0.8},
                    {
                        "tool": "talk_to_agent",
                        "args": {
                            "agent": "nova",
                            "message": "What should we know before launching Tidewater?",
                        },
                        "result": RESEARCH,
                        "took": 3.6,
                        "peer": {
                            "to": "nova",
                            "message": "What should we know before launching Tidewater?",
                            "reply": RESEARCH,
                        },
                    },
                    {
                        "tool": "talk_to_agent",
                        "args": {
                            "agent": "quill",
                            "message": "Write a headline and email subject for Tidewater.",
                        },
                        "result": COPY,
                        "took": 3.2,
                        "peer": {
                            "to": "quill",
                            "message": "Write a headline and an email subject for Tidewater. Lead with 'refillable'.",
                            "reply": COPY,
                        },
                    },
                    {"text": PLAN, "wps": 17},
                ]
            }
        ],
    },
}
