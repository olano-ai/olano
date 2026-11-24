"""E2E: files an agent names in its reply become media, cards and Files links,
and a turn renders text → tool activity → text in the order it happened.

The transcript this guards came from a real session: an agent saved a chart
under ``reports/`` and told the user "if it doesn't render inline…", because
nothing in the chat turned the path into anything clickable — and its
paragraphs, split around tool calls, ran together in one bubble.

Seeded as a persisted thread (the reload path), which is also what the live
``thread_updated`` frame projects through, so the same shapes cover both.
The workspace files are created on disk where the runtime resolves this
agent's workspace *right now* (the root conftest repoints the home per test;
see ``test_connect_result_card._seed_thread`` for the same reasoning).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

AGENT = "merli"
PNG = "reports/accounts-by-country.png"
CSV = "reports/accounts-by-country.csv"
# A 1×1 opaque PNG — enough for <img> to decode and report a natural size.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cfc0f01f0005000301b7b3f3e00000000049454e44ae426082"
)


def _workspace_root() -> Path:
    from olano.runtime.api import _workspace_root

    return _workspace_root(AGENT)


def _seed_files() -> Path:
    root = _workspace_root()
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / PNG).write_bytes(_PNG_BYTES)
    (root / CSV).write_text("country,accounts\nCosta Rica,412\nPanama,133\n", encoding="utf-8")
    return root


def _seed_thread(rows: list[dict]) -> str:
    from olano.config.home import get_olano_paths

    thread_id = str(uuid.uuid4())
    sessions = get_olano_paths().sessions_dir / AGENT
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{thread_id}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    return thread_id


def _rows(root: Path) -> list[dict]:
    absolute = str(root / PNG)
    first = "Here is the breakdown by country."
    second = (
        f"Saved: {absolute}\n\n"
        f"The data behind it is in {CSV} — the same numbers as the table above."
    )
    return [
        {"ts": "2026-09-06T10:00:00Z", "role": "user", "content": "chart of accounts per country"},
        {
            "ts": "2026-09-06T10:00:01Z",
            "role": "tool_call",
            "tool_call_id": "call_1",
            "name": "render_html_image",
            "args": json.dumps({"html": "reports/accounts-by-country.html", "save_path": PNG}),
            "content": "",
        },
        {
            "ts": "2026-09-06T10:00:02Z",
            "role": "tool_result",
            "tool_call_id": "call_1",
            "name": "render_html_image",
            "args": "",
            "content": f"Rendered 2400x1350 → {absolute}",
        },
        {
            "ts": "2026-09-06T10:00:03Z",
            "role": "assistant",
            "content": f"{first}\n\n{second}",
            "segments": [
                {"text": first, "after": None},
                {"text": f"\n\n{second}", "after": "call_1"},
            ],
        },
    ]


def _open(page, base_url: str, thread_id: str) -> None:
    page.goto(f"{base_url.rstrip('/')}/chat?thread={thread_id}", wait_until="domcontentloaded")
    page.locator(".chat-media-img").first.wait_for(state="visible", timeout=20_000)


def test_named_files_render_as_media_cards_and_links(page, dashboard) -> None:
    root = _seed_files()
    thread_id = _seed_thread(_rows(root))
    base = dashboard.base_url.rstrip("/")
    _open(page, base, thread_id)

    # The PNG the reply named (by absolute path) is drawn inline from the raw
    # endpoint, and it actually decodes.
    img = page.locator(".chat-media-img").first
    assert "/api/files-raw/reports/accounts-by-country.png" in (img.get_attribute("src") or "")
    page.wait_for_function(
        "el => el.complete && el.naturalWidth > 0", arg=img.element_handle(), timeout=15_000
    )

    # The CSV (named by workspace-relative path) is a file card, not a picture,
    # with a Download link and an Open-in-Files link to the deep-link route.
    card = page.locator(".chat-file-card").first
    card.wait_for(state="visible", timeout=10_000)
    assert "accounts-by-country.csv" in (card.text_content() or "")
    download = card.locator("a[download]").first
    assert "download=1" in (download.get_attribute("href") or "")
    open_in_files = card.locator(f"a[href*='/files?path={CSV}']").first
    assert "agent=merli" in (open_in_files.get_attribute("href") or "")

    # Both mentions inside the prose are anchors the chat handles itself
    # (no target, internal-navigation marker), with server-built hrefs.
    mention = page.locator(
        ".bubble.assistant a[data-internal]", has_text="accounts-by-country.csv"
    ).first
    mention.wait_for(state="visible", timeout=10_000)
    assert f"/files?path={CSV}" in (mention.get_attribute("href") or "")
    assert mention.get_attribute("target") is None
    abs_mention = page.locator(".bubble.assistant a[data-internal]", has_text=str(root / PNG)).first
    assert abs_mention.count() == 1

    # Clicking a mention navigates to the Files view scoped to the agent.
    mention.click()
    page.wait_for_function("() => window.location.pathname.startsWith('/files')", timeout=15_000)


def test_turn_renders_text_tools_text_in_order(page, dashboard) -> None:
    root = _seed_files()
    thread_id = _seed_thread(_rows(root))
    base = dashboard.base_url.rstrip("/")
    _open(page, base, thread_id)

    # One reply message; inside it, the first paragraph, then the tool
    # activity line, then the second paragraph — never glued into one bubble.
    order = page.evaluate(
        """() => {
          const msg = [...document.querySelectorAll('.msg')].find(m => m.querySelector('.tool-activity'));
          if (!msg) return null;
          return [...msg.querySelectorAll('.bubble.assistant, .tool-activity')]
            .map(el => el.classList.contains('tool-activity') ? 'tools' : 'text:' + el.textContent.trim().slice(0, 24));
        }"""
    )
    assert order is not None, "no message with tool activity rendered"
    assert order[0].startswith("text:Here is the breakdown"), order
    assert order[1] == "tools", order
    assert order[2].startswith("text:Saved:"), order
    assert len(order) == 3, order
    # The tool line names the tool that ran.
    assert "render_html_image" in (page.locator(".tool-activity").first.text_content() or "")


def _set_ui_mode(value: str) -> None:
    from olano.config.home import ensure_olano_home, update_home_config

    ensure_olano_home()
    update_home_config({"ui_mode": value})


def test_the_simple_shell_draws_the_same_turn_in_the_same_order(page, dashboard) -> None:
    """The bot-chat shell had the defect this fixed, on its own copy of the
    layout: every tool card stacked above one glued reply, and the paths in the
    prose were plain text.

    Both shells now go through one component (`views/chat/TurnBlocks`), so what
    this pins is that the shared component is actually mounted here — the tool
    markup below it is still this shell's own (`.tool-activity` via
    `ToolTrail`), which is why sharing the ordering and not the markup was the
    call.
    """
    root = _seed_files()
    thread_id = _seed_thread(_rows(root))
    _set_ui_mode("simple")
    base = dashboard.base_url.rstrip("/")
    try:
        # This shell's 1:1 chat starts on Quiet, which draws no tool cards at
        # all (a group starts on Normal — it is opened to watch bots work). The
        # reading level is per browser, so it is set the way the shell stores
        # it rather than by clicking through the cycle.
        page.goto(f"{base}/", wait_until="domcontentloaded")
        page.evaluate("() => localStorage.setItem('lc.simple.detail', 'normal')")
        page.goto(f"{base}/?thread={thread_id}", wait_until="domcontentloaded")
        page.locator(".simple-chat .chat-media-img").first.wait_for(state="visible", timeout=20_000)

        order = page.evaluate(
            """() => {
              const msg = [...document.querySelectorAll('.simple-msg')]
                .find(m => m.querySelector('.tool-activity'));
              if (!msg) return null;
              return [...msg.querySelectorAll('.bubble.assistant, .tool-activity')]
                .map(el => el.classList.contains('tool-activity')
                  ? 'tools'
                  : 'text:' + el.textContent.trim().slice(0, 24));
            }"""
        )
        assert order is not None, "no message with tool activity rendered"
        assert order[0].startswith("text:Here is the breakdown"), order
        assert order[1] == "tools", order
        assert order[2].startswith("text:Saved:"), order
        assert len(order) == 3, order

        # …and the file mentions in the prose are links here too, which they
        # were not before: this shell rendered the body with no file index.
        mention = page.locator(
            ".simple-chat .bubble.assistant a[data-internal]", has_text="accounts-by-country.csv"
        ).first
        mention.wait_for(state="visible", timeout=10_000)
        assert f"/files?path={CSV}" in (mention.get_attribute("href") or "")
    finally:
        page.evaluate("() => localStorage.removeItem('lc.simple.detail')")
        _set_ui_mode("advanced")
