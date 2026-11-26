"""Little Ember Bakehouse, a neighbourhood bakery: the team in the imagine video.

Olano's built-in image tools, ``image_generate`` and ``image_edit``. The three pictures in the video
were made once, ahead of the take, by running those two product tools for real
(``openai:gpt-image-2.5-flare`` on the operator's own OpenAI key) with the prompts and arguments shown
in the chat. ``assets/imagine-results.json`` holds what each call returned; ``assets/imagine-*.png``
are the images it saved. In the take a tool step writes that image into the agent's workspace under
the file name the tool chose and returns the tool's own result text with the take's path, so the
reply names the file and the dashboard draws it inline, as it does for a real run. Generating again
in every take would cost money and give different pictures than the captions describe.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ASSETS = Path(__file__).resolve().parents[1] / "assets"
RESULTS: dict[str, Any] = json.loads((ASSETS / "imagine-results.json").read_text(encoding="utf-8"))


def _image(key: str):
    """The tool's recorded result, with the file placed where the tool saved it in this take."""
    spec = RESULTS[key]

    def run(ctx: dict[str, Any]) -> str:
        ws = Path(os.environ["LIONCLAW_HOME"]) / "agents" / ctx["agent"] / "workspace"
        target = ws / "images" / spec["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ASSETS / spec["asset"]).read_bytes())
        recorded = spec["result"]
        old = recorded.split("Saved: ", 1)[1].split("\n", 1)[0]
        return recorded.replace(old, str(target))

    return run


def _args(key: str) -> dict[str, Any]:
    return dict(RESULTS[key]["args"])


def _path(key: str) -> str:
    return "images/" + RESULTS[key]["file"]


LOGO_REPLY = (
    "Here is a first logo for Little Ember Bakehouse: a round badge with a loaf, a small flame "
    "and the name around the edge. It reads well small, so it should work on bags and stickers too.\n\n"
    f"Saved: {_path('logo')}"
)
BUNS_REPLY = (
    "A menu photo of the cardamom buns: three on a wooden board, pearl sugar on top, soft window light.\n\n"
    f"Saved: {_path('buns')}"
)
WARM_REPLY = (
    "Same buns, warmer: golden light and a little steam, as if they just came out of the oven. "
    "The first version is still in the images folder if you want to compare.\n\n"
    f"Saved: {_path('warm')}"
)

TEAM = {
    "default_agent": "wren",
    "agents": [
        {
            "name": "wren",
            "emoji": "🎨",
            "description": "Makes the bakery's pictures: logos, posters, menu photos.",
            "title": "Design Helper",
            "manager": None,
        },
    ],
    "files": {
        "wren": {
            "AGENTS.md": (
                "# wren\n\nDesign helper at Little Ember Bakehouse, a neighbourhood bakery. "
                "Makes logos, posters and menu photos.\n"
            ),
        },
    },
    "turns": {
        "wren": [
            {
                "match": "warmer",
                "steps": [
                    {"wait": 0.4},
                    {
                        "tool": "image_edit",
                        "args": _args("warm"),
                        "result": _image("warm"),
                        "took": 3.0,
                    },
                    {"text": WARM_REPLY, "wps": 22},
                ],
            },
            {
                "match": "photo",
                "steps": [
                    {"wait": 0.4},
                    {
                        "tool": "image_generate",
                        "args": _args("buns"),
                        "result": _image("buns"),
                        "took": 3.0,
                    },
                    {"text": BUNS_REPLY, "wps": 22},
                ],
            },
            {
                "steps": [
                    {"wait": 0.4},
                    {
                        "tool": "image_generate",
                        "args": _args("logo"),
                        "result": _image("logo"),
                        "took": 3.0,
                    },
                    {"text": LOGO_REPLY, "wps": 22},
                ],
            },
        ],
    },
}
