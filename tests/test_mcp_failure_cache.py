"""The boot-time MCP failure memo.

The runtime boots agents **serially** and bounds each MCP connect at
``BOOT_CONNECT_TIMEOUT + BOOT_CANCEL_GRACE`` (35s) *per agent*. Six agents
sharing one dead server therefore spent 3.5 minutes of boot re-discovering the
same outage — the single biggest contributor to "the runtime took six minutes to
come up". These tests pin the properties that make caching that verdict safe:

* a timeout generalises across agents holding **different** credentials (nothing
  came back, so no token could have changed it);
* any other failure does **not** — it stays scoped to byte-identical credentials;
* a success is never cached (that would hand one agent's toolset to another);
* an explicit retry (``Reload MCP``) always wins over the memo;
* a connect that was abandoned but later succeeds evicts its own entry.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from olano.cli._quiet import run_quietly_async
from olano.core import mcp_setup


@pytest.fixture(autouse=True)
def _clear_memo():
    mcp_setup.clear_boot_failures()
    yield
    mcp_setup.clear_boot_failures()


def _stdio_server(url: str, *, args: list[str] | None = None):
    from olano.config.agent_config import MCPServerConfig

    return MCPServerConfig(
        url=url,
        transport="stdio",
        command=sys.executable,
        args=args if args is not None else ["-c", "pass"],
    )


def _http_server(url: str, **kwargs: Any):
    from olano.config.agent_config import MCPServerConfig

    return MCPServerConfig(url=url, transport="http", **kwargs)


def _counting_client(monkeypatch, behaviour: dict[str, Any], counts: dict[str, int]):
    """Patch MultiServerMCPClient, counting how often each server is dialled."""
    import langchain_mcp_adapters.client as client_mod

    class _FakeClient:
        def __init__(self, conf: dict) -> None:
            self._url = next(iter(conf))

        async def get_tools(self):
            counts[self._url] = counts.get(self._url, 0) + 1
            outcome = behaviour[self._url]
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == "hang":
                await asyncio.sleep(3600)
            return []

    monkeypatch.setattr(client_mod, "MultiServerMCPClient", _FakeClient)


# ── keys ─────────────────────────────────────────────────────────────────────


def test_reach_key_ignores_headers_and_auth() -> None:
    """A timeout means nothing answered — a bearer token cannot change that, so
    agents holding different credentials must share the reachability verdict."""
    base = {"url": "https://mcp.example/mcp", "transport": "streamable_http"}
    plain = mcp_setup.connection_reach_key(base)
    with_header = mcp_setup.connection_reach_key({**base, "headers": {"Authorization": "Bearer x"}})
    with_auth = mcp_setup.connection_reach_key({**base, "auth": object()})
    assert plain == with_header == with_auth


def test_reach_key_separates_endpoints_and_stdio_launches() -> None:
    http_a = mcp_setup.connection_reach_key(
        {"url": "https://a/mcp", "transport": "streamable_http"}
    )
    http_b = mcp_setup.connection_reach_key(
        {"url": "https://b/mcp", "transport": "streamable_http"}
    )
    assert http_a != http_b

    stdio = {"url": "srv", "transport": "stdio", "command": "npx", "args": ["pkg"], "env": {}}
    assert mcp_setup.connection_reach_key(stdio) != mcp_setup.connection_reach_key(
        {**stdio, "args": ["other-pkg"]}
    )
    # For a child process the env *is* what it does, so it must split the key.
    assert mcp_setup.connection_reach_key(stdio) != mcp_setup.connection_reach_key(
        {**stdio, "env": {"TOKEN": "abc"}}
    )


def test_full_key_separates_headers_and_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_setup, "oauth_dir", lambda: tmp_path)
    base = {"url": "https://mcp.example/mcp", "transport": "streamable_http"}

    a = mcp_setup.connection_full_key(base, agent="a", url=base["url"])
    b = mcp_setup.connection_full_key(base, agent="b", url=base["url"])
    # No auth attached → credentials don't participate; identical config shares.
    assert a == b

    hdr = mcp_setup.connection_full_key(
        {**base, "headers": {"X-Key": "1"}}, agent="a", url=base["url"]
    )
    assert hdr != a

    # With an OAuth provider attached the *stored token bytes* split the key.
    authed = {**base, "auth": object()}
    mcp_setup.token_store_path("a", base["url"]).write_text('{"tokens": {"access_token": "aaa"}}')
    mcp_setup.token_store_path("b", base["url"]).write_text('{"tokens": {"access_token": "bbb"}}')
    assert mcp_setup.connection_full_key(
        authed, agent="a", url=base["url"]
    ) != mcp_setup.connection_full_key(authed, agent="b", url=base["url"])


def test_missing_tokens_never_share_a_key(tmp_path, monkeypatch) -> None:
    """When we can't prove two agents hold the same credential they must not
    share a credential-scoped verdict — an unreadable token digests per-agent."""
    monkeypatch.setattr(mcp_setup, "oauth_dir", lambda: tmp_path)
    params = {"url": "https://mcp.example/mcp", "transport": "streamable_http", "auth": object()}
    assert mcp_setup.connection_full_key(
        params, agent="a", url=params["url"]
    ) != mcp_setup.connection_full_key(params, agent="b", url=params["url"])


# ── memo mechanics ───────────────────────────────────────────────────────────


def test_recall_returns_the_first_live_entry() -> None:
    mcp_setup.remember_boot_failure("k1", status="timeout", message="timed out", url="srv")
    hit = mcp_setup.recall_boot_failure("nope", "k1")
    assert hit is not None
    assert (hit.status, hit.message, hit.url) == ("timeout", "timed out", "srv")
    assert mcp_setup.recall_boot_failure("nope") is None


def test_entries_expire(monkeypatch) -> None:
    monkeypatch.setenv("LIONCLAW_MCP_FAILURE_CACHE_TTL", "0.05")
    mcp_setup.remember_boot_failure("k", status="timeout", message="m", url="s")
    assert mcp_setup.recall_boot_failure("k") is not None
    import time as _time

    _time.sleep(0.06)
    assert mcp_setup.recall_boot_failure("k") is None


def test_ttl_zero_disables_the_memo(monkeypatch) -> None:
    monkeypatch.setenv("LIONCLAW_MCP_FAILURE_CACHE_TTL", "0")
    mcp_setup.remember_boot_failure("k", status="timeout", message="m", url="s")
    assert mcp_setup.recall_boot_failure("k") is None


def test_forget_and_clear() -> None:
    mcp_setup.remember_boot_failure("a", status="error", message="m", url="s")
    mcp_setup.remember_boot_failure("b", status="error", message="m", url="s")
    mcp_setup.forget_boot_failure("a")
    assert mcp_setup.recall_boot_failure("a") is None
    assert mcp_setup.clear_boot_failures() == 1
    assert mcp_setup.recall_boot_failure("b") is None


# ── boot path ────────────────────────────────────────────────────────────────


async def test_second_agent_skips_a_timed_out_server(base_config, monkeypatch) -> None:
    """The headline win: the 35s discovery is paid once per boot, not per agent."""
    from olano.core.builder import _build_mcp_tools

    counts: dict[str, int] = {}
    _counting_client(monkeypatch, {"dead": "hang"}, counts)
    monkeypatch.setattr(mcp_setup, "BOOT_CONNECT_TIMEOUT", 0.05)
    monkeypatch.setattr(mcp_setup, "BOOT_CANCEL_GRACE", 0.05)
    base_config.mcp_servers = [_stdio_server("dead")]

    before = asyncio.all_tasks()
    _tools, first = await run_quietly_async(_build_mcp_tools, base_config)
    assert [s["status"] for s in first] == ["timeout"]
    assert counts["dead"] == 1

    # A second agent with the identical server config never dials it.
    second_config = base_config.model_copy(update={"name": "other-agent"})
    _tools2, second = await run_quietly_async(_build_mcp_tools, second_config)
    assert [s["status"] for s in second] == ["timeout"]
    assert counts["dead"] == 1, "second agent re-dialled a server already known dead"
    assert "cached" in second[0]["message"]

    leftovers = [t for t in asyncio.all_tasks() - before if not t.done()]
    for task in leftovers:
        task.cancel()
    await asyncio.gather(*leftovers, return_exceptions=True)


async def test_timeout_is_shared_across_differing_oauth_credentials(
    base_config, monkeypatch, tmp_path
) -> None:
    """Different tokens, same dead endpoint → still one discovery. This is the
    case a credentials-inclusive key would have missed entirely."""
    from olano.core.builder import _build_mcp_tools

    monkeypatch.setattr(mcp_setup, "oauth_dir", lambda: tmp_path)
    url = "https://mcp.example/mcp"
    for agent, token in (("agent-a", "aaa"), ("agent-b", "bbb")):
        mcp_setup.token_store_path(agent, url).write_text(
            f'{{"tokens": {{"access_token": "{token}", "refresh_token": "r"}}}}'
        )

    counts: dict[str, int] = {}
    _counting_client(monkeypatch, {url: "hang"}, counts)
    monkeypatch.setattr(mcp_setup, "BOOT_CONNECT_TIMEOUT", 0.05)
    monkeypatch.setattr(mcp_setup, "BOOT_CANCEL_GRACE", 0.05)

    before = asyncio.all_tasks()
    for agent in ("agent-a", "agent-b"):
        cfg = base_config.model_copy(update={"name": agent})
        cfg.mcp_servers = [_http_server(url, auth="oauth")]
        _tools, summary = await run_quietly_async(_build_mcp_tools, cfg)
        assert [s["status"] for s in summary] == ["timeout"]
    assert counts[url] == 1

    leftovers = [t for t in asyncio.all_tasks() - before if not t.done()]
    for task in leftovers:
        task.cancel()
    await asyncio.gather(*leftovers, return_exceptions=True)


async def test_non_timeout_failure_is_not_shared_across_credentials(
    base_config, monkeypatch, tmp_path
) -> None:
    """A 401 is *this agent's* 401 — an agent with a different token must still
    get its own chance to connect."""
    from olano.core.builder import _build_mcp_tools

    monkeypatch.setattr(mcp_setup, "oauth_dir", lambda: tmp_path)
    url = "https://mcp.example/mcp"
    for agent, token in (("agent-a", "aaa"), ("agent-b", "bbb")):
        mcp_setup.token_store_path(agent, url).write_text(
            f'{{"tokens": {{"access_token": "{token}", "refresh_token": "r"}}}}'
        )

    counts: dict[str, int] = {}
    _counting_client(monkeypatch, {url: RuntimeError("401 Unauthorized")}, counts)

    for agent in ("agent-a", "agent-b"):
        cfg = base_config.model_copy(update={"name": agent})
        cfg.mcp_servers = [_http_server(url, auth="oauth")]
        _tools, summary = await run_quietly_async(_build_mcp_tools, cfg)
        assert [s["status"] for s in summary] == ["error"]
    assert counts[url] == 2, "a credential-specific failure leaked to another agent"


async def test_identical_credentials_do_share_a_non_timeout_failure(
    base_config, monkeypatch
) -> None:
    from olano.core.builder import _build_mcp_tools

    counts: dict[str, int] = {}
    _counting_client(monkeypatch, {"boom": RuntimeError("handshake failed")}, counts)
    base_config.mcp_servers = [_stdio_server("boom")]

    _tools, first = await run_quietly_async(_build_mcp_tools, base_config)
    assert [s["status"] for s in first] == ["error"]

    other = base_config.model_copy(update={"name": "other-agent"})
    _tools2, second = await run_quietly_async(_build_mcp_tools, other)
    assert [s["status"] for s in second] == ["error"]
    assert counts["boom"] == 1
    assert "cached" in second[0]["message"]


async def test_success_is_never_cached(base_config, monkeypatch) -> None:
    """Only failures are memoised — a shared success would mean handing agent B
    a toolset discovered with agent A's credentials."""
    from olano.core.builder import _build_mcp_tools

    counts: dict[str, int] = {}
    _counting_client(monkeypatch, {"ok": None}, counts)
    base_config.mcp_servers = [_stdio_server("ok")]

    for name in ("first", "second"):
        cfg = base_config.model_copy(update={"name": name})
        _tools, summary = await run_quietly_async(_build_mcp_tools, cfg)
        assert [s["status"] for s in summary] == ["connected"]
    assert counts["ok"] == 2


async def test_a_recovered_server_is_retried_after_the_memo_is_cleared(
    base_config, monkeypatch
) -> None:
    """`Reload MCP` is the user saying "I fixed it" — it must beat the memo."""
    from olano.core.builder import _build_mcp_tools

    counts: dict[str, int] = {}
    behaviour: dict[str, Any] = {"srv": RuntimeError("down")}
    _counting_client(monkeypatch, behaviour, counts)
    base_config.mcp_servers = [_stdio_server("srv")]

    _t, first = await run_quietly_async(_build_mcp_tools, base_config)
    assert [s["status"] for s in first] == ["error"]

    behaviour["srv"] = None  # server comes back
    mcp_setup.clear_boot_failures()
    _t2, second = await run_quietly_async(_build_mcp_tools, base_config)
    assert [s["status"] for s in second] == ["connected"]
    assert counts["srv"] == 2


async def test_late_success_of_an_abandoned_connect_evicts_the_memo(
    base_config, monkeypatch
) -> None:
    """A first-run stdio server whose `npx` was still downloading times out for
    the agent that triggered the download, then succeeds moments later. The memo
    must not turn "one agent missed it" into "every agent missed it"."""
    import langchain_mcp_adapters.client as client_mod

    from olano.core.builder import _build_mcp_tools

    class _SlowThenFine:
        def __init__(self, conf: dict) -> None:
            pass

        async def get_tools(self):
            try:
                await asyncio.sleep(0.15)  # outlives the timeout + cancel grace
            except asyncio.CancelledError:
                await asyncio.sleep(0.15)  # wedged cleanup → the connect is abandoned
            return []

    monkeypatch.setattr(client_mod, "MultiServerMCPClient", _SlowThenFine)
    monkeypatch.setattr(mcp_setup, "BOOT_CONNECT_TIMEOUT", 0.02)
    monkeypatch.setattr(mcp_setup, "BOOT_CANCEL_GRACE", 0.02)
    base_config.mcp_servers = [_stdio_server("slow-start")]

    before = asyncio.all_tasks()
    _tools, summary = await run_quietly_async(_build_mcp_tools, base_config)
    assert [s["status"] for s in summary] == ["timeout"]

    params = mcp_setup.build_connection_params(base_config.mcp_servers[0], base_config.name)
    assert params is not None
    reach_key = mcp_setup.connection_reach_key(params)
    assert mcp_setup.recall_boot_failure(reach_key) is not None

    # Let the abandoned connect finish; its done-callback evicts the memo.
    await asyncio.sleep(0.4)
    assert mcp_setup.recall_boot_failure(reach_key) is None

    leftovers = [t for t in asyncio.all_tasks() - before if not t.done()]
    for task in leftovers:
        task.cancel()
    await asyncio.gather(*leftovers, return_exceptions=True)


async def test_memo_does_not_leak_between_different_servers(base_config, monkeypatch) -> None:
    from olano.core.builder import _build_mcp_tools

    counts: dict[str, int] = {}
    _counting_client(monkeypatch, {"bad": RuntimeError("down"), "good": None}, counts)
    base_config.mcp_servers = [_stdio_server("bad", args=["-c", "pass"])]
    await run_quietly_async(_build_mcp_tools, base_config)

    other = base_config.model_copy(update={"name": "other"})
    other.mcp_servers = [_stdio_server("good", args=["-c", "print(1)"])]
    _tools, summary = await run_quietly_async(_build_mcp_tools, other)
    assert [s["status"] for s in summary] == ["connected"]
    assert counts["good"] == 1
