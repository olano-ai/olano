"""`inspect_external` — reading an external project's docs before anyone
installs it. The classifier is a table over textual markers; the fetch goes
through the sanitising `web_fetch` reader (stubbed here); a failure is a
`kind: "unknown"` answer with the error in `evidence`, never an exception."""

from __future__ import annotations

import json

import pytest

from olano import admin
from olano.admin.integrations import INSPECT_DISCLAIMER, inspect_external

STDIO_README = """# Acme MCP Server

Add it to your client:

```json
{"mcpServers": {"acme": {"command": "npx", "args": ["-y", "@acme/mcp-server"],
 "env": {"ACME_API_KEY": "<your key>"}}}}
```

Get ACME_API_KEY from the Acme console.
"""

REMOTE_README = """# Acme remote MCP

Connect your client to https://mcp.acme.test/mcp — it authenticates with OAuth
(register a client_id first).
"""

SKILL_README = """# A skill pack

Copy SKILL.md into your skills folder.
"""

CLI_README = """# acme-cli

Install: `npm install -g acme-cli`

Usage:
  acme login --token ACME_TOKEN
"""

PLAIN_README = "Just a library. Nothing to see here."


@pytest.fixture
def fetch(monkeypatch):
    """Route `_web_fetch_impl` to canned documents keyed by URL substring."""
    calls: list[str] = []
    docs: dict[str, str] = {}

    def fake(url, extractMode="markdown", maxChars=0):
        calls.append(url)
        for needle, text in docs.items():
            if needle in url:
                return json.dumps({"url": url, "text": text, "title": "Acme"})
        return "Error: Web fetch failed (404): not found"

    monkeypatch.setattr("olano.tools.web_fetch._web_fetch_impl", fake)
    docs["__calls__"] = ""  # placeholder so the dict is never empty
    fake.calls = calls  # type: ignore[attr-defined]
    fake.docs = docs  # type: ignore[attr-defined]
    return fake


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    monkeypatch.setattr("olano.tools._credentials.get_credential", lambda name, default="": default)


@pytest.mark.parametrize(
    ("readme", "kind", "runs_where"),
    [
        (
            STDIO_README,
            "mcp_stdio",
            "ephemeral sandbox container (supported: add it as a stdio MCP server)",
        ),
        (REMOTE_README, "mcp_remote", "remote (supported: add it as an http/sse MCP server)"),
        (
            SKILL_README,
            "skill",
            "your agent's sandbox (unsupported: Olano never authenticates it; at the user's risk)",
        ),
        (
            CLI_README,
            "cli",
            "your agent's sandbox (unsupported: Olano never authenticates it; at the user's risk)",
        ),
        (
            PLAIN_README,
            "unknown",
            "your agent's sandbox (unsupported: Olano never authenticates it; at the user's risk)",
        ),
    ],
)
def test_classifier_table(fetch, readme, kind, runs_where):
    fetch.docs["README.md"] = readme
    out = inspect_external("https://github.com/acme/acme-thing", "")
    assert out["kind"] == kind
    assert out["runs_where"] == runs_where
    assert out["disclaimer"] == INSPECT_DISCLAIMER


def test_github_repo_reads_the_raw_readme_first(fetch):
    fetch.docs["README.md"] = STDIO_README
    out = inspect_external("https://github.com/acme/acme-thing", "")
    assert fetch.calls[0] == "https://raw.githubusercontent.com/acme/acme-thing/HEAD/README.md"
    assert out["fetched"] == [fetch.calls[0]]
    assert out["needs"] == {"env": ["ACME_API_KEY"], "oauth": False}
    assert out["suggested_server"] == {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@acme/mcp-server"],
        "env": {"ACME_API_KEY": "${vault:ACME_API_KEY}"},
    }
    assert any(e.startswith("mcp_stdio:") for e in out["evidence"])


def test_bare_name_tries_npm_then_pypi(fetch):
    fetch.docs["pypi.org"] = CLI_README
    out = inspect_external("acme-cli", "")
    assert fetch.calls == [
        "https://www.npmjs.com/package/acme-cli",
        "https://pypi.org/project/acme-cli/",
    ]
    assert out["kind"] == "cli"
    assert any("npmjs.com" in e and "404" in e for e in out["evidence"])


def test_remote_hit_extracts_url_and_oauth(fetch):
    fetch.docs["docs.acme.test"] = REMOTE_README
    out = inspect_external("https://docs.acme.test/mcp-guide", "")
    assert out["kind"] == "mcp_remote"
    assert out["needs"]["oauth"] is True
    assert out["suggested_server"] == {
        "transport": "http",
        "url": "https://mcp.acme.test/mcp",
        "auth": "oauth",
    }
    assert "OAuth" in out["recommendation"]


def test_built_in_alternative_points_at_a_supported_integration(fetch):
    admin.create_agent("inspector", model="fake:instant")
    fetch.docs["README.md"] = STDIO_README.replace("Acme", "Notion").replace("acme", "notion")
    out = inspect_external("https://github.com/makenotion/notion-mcp-server", "inspector")
    ids = [(o["class"], o["id"]) for o in out["built_in_alternative"]]
    assert ("toolkit", "notion") in ids
    assert len(out["built_in_alternative"]) <= 3
    assert out["recommendation"].startswith("A supported integration already covers 'notion'")


def test_network_failure_is_unknown_with_the_error_in_evidence(monkeypatch):
    monkeypatch.setattr(
        "olano.tools.web_fetch._web_fetch_impl",
        lambda url, extractMode="markdown", maxChars=0: "Error: request timed out after 30s",
    )
    out = inspect_external("https://github.com/acme/acme-thing", "")
    assert out["kind"] == "unknown"
    assert out["needs"] == {"env": [], "oauth": False}
    assert any("timed out" in e for e in out["evidence"])
    assert out["disclaimer"] == INSPECT_DISCLAIMER


def test_a_raising_fetcher_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr("olano.tools.web_fetch._web_fetch_impl", boom)
    out = inspect_external("https://example.test/thing", "")
    assert out["kind"] == "unknown"
    assert any("no network" in e for e in out["evidence"])


@pytest.mark.parametrize("target", ["", "   ", "not a url or a name!"])
def test_junk_targets_are_unknown_not_errors(target):
    out = inspect_external(target, "")
    assert out["kind"] == "unknown"
    assert out["evidence"]
    assert "URL" in out["recommendation"]


def test_the_self_tool_returns_the_disclaimer(monkeypatch, fetch):
    from olano.tools import self_config_tools
    from olano.tools.talk_to import current_agent

    fetch.docs["README.md"] = STDIO_README
    admin.create_agent("selfie", model="fake:instant")
    token = current_agent.set("selfie")
    try:
        out = json.loads(
            self_config_tools.self_inspect_external.invoke({"url_or_name": "acme/acme-thing"})
        )
    finally:
        current_agent.reset(token)
    assert out["kind"] == "mcp_stdio"
    assert out["disclaimer"] == INSPECT_DISCLAIMER
