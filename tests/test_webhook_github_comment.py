"""The webhook platform's GitHub-comment delivery uses the agent's connection.

It used to shell out to ``gh pr comment`` on the runtime host — a command-line
tool nothing signs in any more. Now it posts through the GitHub API with the
``GITHUB_TOKEN`` the agent's connection resolves, and says how to connect when
there is none.
"""

from __future__ import annotations

import sys
import types

from olano.platforms.webhook import WebhookAdapter


def _adapter(agent_name: str = "alice") -> WebhookAdapter:
    adapter = object.__new__(WebhookAdapter)
    adapter.agent_name = agent_name
    return adapter


def _fake_pygithub(monkeypatch, calls: list):
    class _Issue:
        def create_comment(self, body):
            calls.append(("comment", body))

    class _Repo:
        def get_issue(self, number):
            calls.append(("issue", number))
            return _Issue()

    class _Github:
        def __init__(self, token):
            calls.append(("token", token))

        def get_repo(self, repo):
            calls.append(("repo", repo))
            return _Repo()

    monkeypatch.setitem(sys.modules, "github", types.SimpleNamespace(Github=_Github))


async def test_posts_through_the_api_with_the_agents_connection(tmp_path, monkeypatch):
    from olano.vault import resolver
    from olano.vault.store import VaultStore

    v = VaultStore.open(passphrase="pw", db_path=tmp_path / "v.db", use_keyring=False)
    v.set("GITHUB_TOKEN", "common-gh")
    v.set("GITHUB_TOKEN", "alice-gh", agent="alice")
    resolver.set_vault(v)
    calls: list = []
    _fake_pygithub(monkeypatch, calls)

    try:
        result = await _adapter("alice")._deliver_github_comment(
            "looks good", {"deliver_extra": {"repo": "org/repo", "pr_number": "42"}}
        )
    finally:
        resolver.set_vault(None)
    assert result.success, result.error
    assert ("token", "alice-gh") in calls  # the agent's own connection, not common
    assert ("repo", "org/repo") in calls
    assert ("issue", 42) in calls
    assert ("comment", "looks good") in calls


async def test_missing_connection_names_the_fix(monkeypatch):
    from olano.vault import resolver

    resolver.set_vault(None)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    calls: list = []
    _fake_pygithub(monkeypatch, calls)
    result = await _adapter("alice")._deliver_github_comment(
        "x", {"deliver_extra": {"repo": "org/repo", "pr_number": "1"}}
    )
    assert not result.success
    assert "Connections" in (result.error or "")
    assert not calls  # never reached the API


async def test_missing_target_is_refused():
    result = await _adapter()._deliver_github_comment("x", {"deliver_extra": {}})
    assert not result.success


def test_no_command_line_tool_is_involved():
    import inspect

    from olano.platforms import webhook

    assert webhook is not None
    src = inspect.getsource(WebhookAdapter._deliver_github_comment)
    assert '"gh"' not in src and "subprocess" not in src
