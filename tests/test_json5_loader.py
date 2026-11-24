"""Regression tests for JSON5 config loading.

Guards against the bug where the loader's comment-stripping fallback treated
``//`` inside a string value (e.g. a URL like ``http://127.0.0.1:8080``) as the
start of a line comment, corrupting otherwise-valid JSON and making the agent
fail to boot (it then silently vanished from the dashboard). With ``json5``
declared as a dependency the real parser is used; these tests assert URLs with
``//`` survive a round-trip through the loaders.
"""

from __future__ import annotations

import json
from pathlib import Path

from olano.config.home import get_olano_paths
from olano.config.loader import load_agent_config
from olano.orchestration.subagent_store import _load_json5_file

SIGNAL_HTTP_URL = "http://127.0.0.1:8080"


def _write_agent_config(name: str, data: dict) -> Path:
    agent_dir = get_olano_paths().agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "agent.json5"
    # Match how the app writes configs: plain JSON via json.dumps.
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def test_load_agent_config_preserves_url_in_channel() -> None:
    """The aurelius repro: a signal channel with a ``//`` URL must load intact."""
    _write_agent_config(
        "signaltest",
        {
            "name": "signaltest",
            "description": "agent with a signal connector",
            "channels": [
                {
                    "type": "platform:signal",
                    "extra": {
                        "http_url": SIGNAL_HTTP_URL,
                        "account": "+12025550188",
                        "ignore_stories": False,
                    },
                }
            ],
        },
    )

    cfg = load_agent_config("signaltest")

    assert [c.type for c in cfg.channels] == ["platform:signal"]
    # The URL (with its //) must survive parsing — not be truncated to "http:".
    assert cfg.channels[0].extra["http_url"] == SIGNAL_HTTP_URL


def test_load_agent_config_handles_comments_and_trailing_commas(tmp_path: Path) -> None:
    """Hand-edited json5 niceties still parse (// comments, trailing commas)."""
    agent_dir = get_olano_paths().agents_dir / "handedited"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.json5").write_text(
        """{
  // this agent was hand-edited
  "name": "handedited",
  "model": "anthropic:claude-sonnet-4-6",
  "tools": [
    "web_fetch",  // a real trailing comment
  ],
}
""",
        encoding="utf-8",
    )

    cfg = load_agent_config("handedited")

    assert cfg.name == "handedited"
    # The // comments and trailing comma parsed cleanly; the declared tool
    # survives (the loaded list is also augmented with universal default tools).
    assert "web_fetch" in cfg.tools


def test_hidden_flag_round_trips_and_defaults_false() -> None:
    """The `hidden` flag (internal/test agents) loads as a real bool, default False."""
    _write_agent_config(
        "hiddentest",
        {"name": "hiddentest", "model": "anthropic:claude-haiku-4-5", "hidden": True},
    )
    _write_agent_config(
        "visibletest",
        {"name": "visibletest", "model": "anthropic:claude-haiku-4-5"},
    )

    assert load_agent_config("hiddentest").hidden is True
    # Omitting the field leaves the agent visible (backward compatible default).
    assert load_agent_config("visibletest").hidden is False


def test_subagent_store_loader_preserves_url(tmp_path: Path) -> None:
    """``_load_json5_file`` must not corrupt a def whose value contains a URL."""
    path = tmp_path / "researcher.json5"
    path.write_text(
        json.dumps(
            {
                "name": "researcher",
                "description": "uses an mcp server",
                "system_prompt": "Fetch from https://example.com/api when asked.",
                "mcp_servers": [{"url": "http://127.0.0.1:9000/sse"}],
            }
        ),
        encoding="utf-8",
    )

    data = _load_json5_file(path)

    assert data, "definition should not be silently dropped to {}"
    assert data["mcp_servers"][0]["url"] == "http://127.0.0.1:9000/sse"
    assert "https://example.com/api" in data["system_prompt"]
