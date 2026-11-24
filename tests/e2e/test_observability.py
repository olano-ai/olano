"""E2E: Langfuse tracing in the dashboard (Config section, agent card, chat link).

What the in-process tests cannot see: that the Config section renders every
field with its help and saves through the page's one Save button, that picking
full capture puts the warning on screen before anything is saved, that the
agent's Overview card writes the three-state override where the runner reads
it, and that a traced reply in the chat carries a working "Langfuse" link to
its trace while an untraced one carries none. Both viewports run (the shared
``page`` fixture is parametrized), per the dashboard rule in CLAUDE.md.
"""

from __future__ import annotations

import json
import uuid

import pytest
import yaml

pytestmark = pytest.mark.e2e

ACTIONS = "[data-testid='config-actions']"
SAVE = f"{ACTIONS} button.primary"
SECTION = "section.card:has-text('Observability (Langfuse)')"
TRACE_ID = "0123456789abcdef0123456789abcdef"


def _home():
    from olano.config.home import get_olano_paths

    return get_olano_paths()


def _config_block() -> dict:
    path = _home().config_path
    raw = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    return (raw.get("observability") or {}).get("langfuse") or {}


def _write_block(block: dict) -> None:
    path = _home().config_path
    raw = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    raw["observability"] = {"langfuse": block}
    path.write_text(yaml.safe_dump(raw))


def _open_config(page, dashboard):
    page.goto(dashboard.base_url.rstrip("/") + "/config", wait_until="domcontentloaded")
    page.locator(ACTIONS).wait_for(state="visible", timeout=20_000)
    section = page.locator(SECTION)
    section.wait_for(state="visible", timeout=20_000)
    # The status half has answered (the key-source line renders after it).
    section.get_by_test_id("observability-key-source").wait_for(state="visible", timeout=20_000)
    section.scroll_into_view_if_needed()
    return section


def test_config_section_explains_itself_and_saves(page, dashboard) -> None:
    section = _open_config(page, dashboard)
    # Field labels are uppercased by CSS, and inner_text reports what is drawn.
    text = section.inner_text().lower()
    for label in (
        "Trace agent turns",
        "Langfuse address",
        "What each trace contains",
        "Share of turns traced",
        "Environment name",
        "Trace background work",
        "Langfuse keys",
    ):
        assert label.lower() in text, label
    assert "jp.cloud.langfuse.com" in text  # the region list is in the help
    assert "no keys yet" in text
    assert section.get_by_test_id("observability-full-warning").count() == 0

    section.get_by_test_id("observability-enabled").check()
    section.get_by_test_id("observability-capture").select_option("full")
    section.get_by_test_id("observability-full-warning").wait_for(state="visible", timeout=5_000)
    section.get_by_test_id("observability-host").fill("https://jp.cloud.langfuse.com")
    page.locator(SAVE).click()
    page.wait_for_function(
        "() => !document.querySelector(\"[data-testid='config-unsaved']\")", timeout=15_000
    )
    block = _config_block()
    assert block["enabled"] is True
    assert block["capture"] == "full"
    assert block["host"] == "https://jp.cloud.langfuse.com"


def test_a_bad_host_disables_save(page, dashboard) -> None:
    section = _open_config(page, dashboard)
    section.get_by_test_id("observability-host").fill("http://langfuse.example.com")
    assert page.locator(SAVE).is_disabled()


def test_connection_test_without_keys_says_what_is_missing(page, dashboard) -> None:
    section = _open_config(page, dashboard)
    section.get_by_test_id("observability-test").click()
    result = section.get_by_test_id("observability-test-result")
    result.wait_for(state="visible", timeout=15_000)
    assert "No Langfuse keys" in result.inner_text()


def test_agent_card_writes_the_override(page, dashboard) -> None:
    page.goto(
        dashboard.base_url.rstrip("/") + "/agents/merli?tab=overview", wait_until="domcontentloaded"
    )
    card = page.get_by_test_id("observability-card")
    card.wait_for(state="visible", timeout=20_000)
    card.scroll_into_view_if_needed()
    assert "follow config" in card.inner_text().lower()
    assert "not traced" in card.get_by_test_id("observability-agent-effective").inner_text()
    card.get_by_test_id("observability-agent-enabled").select_option("off")
    cfg_path = _home().agents_dir / "merli" / "agent.json5"
    written: dict = {}
    for _ in range(50):
        written = json.loads(cfg_path.read_text(encoding="utf-8"))
        if (written.get("observability") or {}).get("langfuse", {}).get("enabled") is False:
            break
        page.wait_for_timeout(200)
    assert written["observability"]["langfuse"]["enabled"] is False
    assert written["observability"]["langfuse"]["capture"] is None


def _seed_thread(rows: list[dict]) -> str:
    thread_id = str(uuid.uuid4())
    sessions = _home().sessions_dir / "merli"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{thread_id}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    return thread_id


def _rows(trace: bool) -> list[dict]:
    reply = {"ts": "2026-10-18T00:00:01Z", "role": "assistant", "content": "Here is the summary."}
    if trace:
        reply["trace_id"] = TRACE_ID
    return [{"ts": "2026-10-18T00:00:00Z", "role": "user", "content": "summarize"}, reply]


def test_a_traced_reply_links_to_its_trace(page, dashboard, monkeypatch) -> None:
    from olano.observability import credentials

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-e2e")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-e2e-secret")
    credentials._reset_for_tests()
    _write_block(
        {"enabled": True, "host": "https://jp.cloud.langfuse.com", "project_id": "proj-e2e"}
    )
    thread_id = _seed_thread(_rows(trace=True))
    page.goto(
        f"{dashboard.base_url.rstrip('/')}/chat?thread={thread_id}", wait_until="domcontentloaded"
    )
    link = page.get_by_test_id("langfuse-trace-link").first
    link.wait_for(state="visible", timeout=20_000)
    assert link.get_attribute("href") == (
        f"https://jp.cloud.langfuse.com/project/proj-e2e/traces/{TRACE_ID}"
    )
    assert link.get_attribute("target") == "_blank"


def test_an_untraced_reply_has_no_link(page, dashboard) -> None:
    thread_id = _seed_thread(_rows(trace=False))
    page.goto(
        f"{dashboard.base_url.rstrip('/')}/chat?thread={thread_id}", wait_until="domcontentloaded"
    )
    page.get_by_text("Here is the summary.").first.wait_for(state="visible", timeout=20_000)
    assert page.get_by_test_id("langfuse-trace-link").count() == 0
