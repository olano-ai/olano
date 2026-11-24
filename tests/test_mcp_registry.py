"""Coverage for the cloud MCP directory client and its CLI surface.

Two layers:

1. :mod:`olano.core.mcp_registry` — the cloud-directory client: cloud fetch,
   TTL cache, and the bundled offline fallback (search / filter / get_server).
2. The ``olano mcp browse|search|install`` commands that surface it.

Network is always mocked (``_fetch_from_cloud`` is monkeypatched) so tests are
deterministic and never touch api.olano.ai.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from olano import admin
from olano.cli.main import cli
from olano.core import mcp_registry


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    """Directory results are module-cached; clear it (and the inline-fetch
    throttle) around every test so each starts from a cold, fetch-permitted
    state."""
    mcp_registry._cache.clear()
    mcp_registry._last_attempt = mcp_registry.NEVER_ATTEMPTED
    yield
    mcp_registry._cache.clear()
    mcp_registry._last_attempt = mcp_registry.NEVER_ATTEMPTED


@pytest.fixture
def offline(monkeypatch):
    """Force the bundled fallback (no cloud) for a deterministic directory.

    `_fetch_full` is the single network entry point (`_fetch_from_cloud`
    delegates to it), so patching it covers directory refresh and search."""
    monkeypatch.setattr(mcp_registry, "_fetch_full", lambda *a, **k: None)


# ── Bundled fallback ─────────────────────────────────────────────────────────


def test_bundled_directory_shape(offline) -> None:
    d = mcp_registry.get_directory()
    assert d["source"] == "bundled"
    assert len(d["presets"]) == 19
    assert len(d["examples"]) == 6
    # Every entry is fully normalized (all fields present).
    for entry in d["presets"] + d["examples"]:
        assert set(entry) == set(mcp_registry._ENTRY_DEFAULTS)
    # Categories are the sorted distinct preset categories.
    assert d["categories"] == sorted(d["categories"])
    assert "Payments" in d["categories"]


def test_bundled_parallel_preset(offline) -> None:
    """The free Parallel Search server, under the CLOUD slug.

    The dashboard's own TS fallback calls the same server `parallel-search`;
    everything that resolves an entry keys on the catalog slug, so the other
    spelling would duplicate the row the moment a real sync lands.
    """
    entry = mcp_registry.get_server("parallel")
    assert entry is not None
    assert entry["url"] == "https://search.parallel.ai/mcp"
    assert entry["transport"] == "http"
    assert entry["auth"] == "none"
    assert entry["envKeys"] == []
    assert entry["category"] == "AI & Data"
    assert mcp_registry.get_server("parallel-search") is None


def test_search_is_token_aware_and_order_independent(offline) -> None:
    """Regression: `search("parallel mcp")` returned nothing.

    Both paths used a whole-phrase substring match, so one extra word — and
    "mcp" is the word a user is most likely to add — made every entry
    unmatchable.
    """
    assert "parallel" in [e["slug"] for e in mcp_registry.search("parallel mcp", limit=20)]
    assert [e["slug"] for e in mcp_registry.search("parallel mcp", limit=20)] == [
        e["slug"] for e in mcp_registry.search("mcp parallel", limit=20)
    ]
    # AND across fields, not OR: every token must land somewhere.
    assert [e["slug"] for e in mcp_registry.search("stripe payments")] == ["stripe"]
    assert mcp_registry.search("stripe zzz") == []


def test_search_matches_the_endpoint_url(offline) -> None:
    """`url` carries the brand when slug and name diverge, and is what makes
    the bare word "mcp" harmless rather than unmatchable."""
    assert [e["slug"] for e in mcp_registry.search("githubcopilot")] == ["github"]


def test_query_tokens_guard(offline) -> None:
    assert mcp_registry.query_tokens("Parallel MCP") == ["parallel"]
    assert mcp_registry.query_tokens("notion mcp server") == ["notion"]
    # All-noise keeps the user's own words rather than matching everything.
    assert mcp_registry.query_tokens("mcp server") == ["mcp", "server"]
    assert mcp_registry.query_tokens("") == []


def test_bundled_preset_fields(offline) -> None:
    hubspot = mcp_registry.get_server("hubspot")
    assert hubspot is not None
    assert hubspot["oauthClient"] == "preregistered"
    assert hubspot["appConsoleLabel"]
    assert hubspot["logoUrl"].startswith("https://www.google.com/s2/favicons")
    assert hubspot["url"] == "https://mcp.hubspot.com"


def test_every_preregistered_preset_names_its_app_console(offline) -> None:
    """A `preregistered` row opens the magic-link page on the Client ID /
    secret form, and that form's only pointer to where the app is made is
    `appConsoleUrl`; without it the user is asked for credentials with no idea
    where to get them."""
    presets = mcp_registry.get_directory()["presets"]
    preregistered = [p for p in presets if p["oauthClient"] == "preregistered"]
    assert preregistered
    for preset in preregistered:
        assert preset["appConsoleUrl"].startswith("https://"), preset["slug"]
        assert preset["appConsoleLabel"], preset["slug"]


def test_bundled_vendor_endpoints_match_vendor_docs(offline) -> None:
    """Rows corrected against the vendors' own docs on 2026-10-16 (cloud
    migration 0102 carries the same values). Asana's V2 server and Canva's MCP
    refuse automatic client registration; Atlassian recommends /v2/mcp for every
    client; Zapier serves every client at /api/v1/connect."""
    by_slug = {p["slug"]: p for p in mcp_registry.get_directory()["presets"]}
    assert by_slug["zapier"]["url"] == "https://mcp.zapier.com/api/v1/connect"
    assert by_slug["atlassian"]["url"] == "https://mcp.atlassian.com/v2/mcp"
    assert by_slug["asana"]["url"] == "https://mcp.asana.com/v2/mcp"
    assert by_slug["asana"]["oauthClient"] == "preregistered"
    assert by_slug["canva"]["url"] == "https://mcp.canva.com/mcp"
    assert by_slug["canva"]["oauthClient"] == "preregistered"
    assert by_slug["intercom"]["url"] == "https://mcp.intercom.com/mcp"
    assert "mcp.eu.intercom.com" in by_slug["intercom"]["notes"]


def test_bundled_composio_preset(offline) -> None:
    """Composio ships in the snapshot: the magic-link page offers it as the
    no-OAuth-app route for pre-registered servers, on a box that has never
    synced the directory too."""
    entry = mcp_registry.get_server("composio")
    assert entry is not None
    assert entry["url"] == "https://connect.composio.dev/mcp"
    assert entry["auth"] == "oauth" and entry["oauthClient"] == "dcr"
    assert entry["homepageUrl"] == "https://composio.dev"


def test_find_by_url_exact_then_unique_host(offline) -> None:
    # Exact, ignoring case and a trailing slash.
    assert mcp_registry.find_by_url("https://mcp.hubspot.com/")["slug"] == "hubspot"
    assert mcp_registry.find_by_url("HTTPS://MCP.HUBSPOT.COM")["slug"] == "hubspot"
    # A path variant still resolves when the host carries one preset.
    assert mcp_registry.find_by_url("https://mcp.hubspot.com/mcp")["slug"] == "hubspot"
    assert mcp_registry.find_by_url("https://task-mcp.parallel.ai/other")["slug"] == "parallel-task"
    assert mcp_registry.find_by_url("https://mcp.example.com/mcp") is None
    # Not a remote URL → never a hit (stdio examples key on a slug, not a URL).
    assert mcp_registry.find_by_url("everything") is None
    assert mcp_registry.find_by_url("") is None


def test_find_by_url_refuses_to_guess_between_presets_on_one_host(monkeypatch) -> None:
    fake = [
        {"slug": "acme-a", "name": "A", "kind": "preset", "url": "https://mcp.acme.com/a"},
        {"slug": "acme-b", "name": "B", "kind": "preset", "url": "https://mcp.acme.com/b"},
    ]
    monkeypatch.setattr(mcp_registry, "_fetch_full", lambda *a, **k: (fake, 1))
    mcp_registry.get_directory(force=True)
    assert mcp_registry.find_by_url("https://mcp.acme.com/a/")["slug"] == "acme-a"
    assert mcp_registry.find_by_url("https://mcp.acme.com/other") is None


def test_bundled_example_stdio(offline) -> None:
    ex = mcp_registry.get_server("everything-stdio")
    assert ex is not None
    assert ex["kind"] == "example"
    assert ex["command"] == "npx"
    assert ex["args"] == ["-y", "@modelcontextprotocol/server-everything"]
    assert ex["exampleJson"]


def test_bundled_github_local_example(offline) -> None:
    """The local github-mcp-server variant rides next to the remote preset."""
    ex = mcp_registry.get_server("github-docker-stdio")
    assert ex is not None
    assert ex["kind"] == "example"
    assert ex["command"] == "docker"
    assert "ghcr.io/github/github-mcp-server" in ex["args"]
    # envKeys drive the modal's required-env placeholder for the PAT.
    assert ex["envKeys"] == ["GITHUB_PERSONAL_ACCESS_TOKEN"]
    # The remote OAuth preset stays a separate entry (dedupe key is the url).
    preset = mcp_registry.get_server("github")
    assert preset is not None and preset["url"] == "https://api.githubcopilot.com/mcp/"


# ── Search / filter (offline) ────────────────────────────────────────────────


def test_search_matches_name_and_kind(offline) -> None:
    slugs = {e["slug"] for e in mcp_registry.search("stripe")}
    assert slugs == {"stripe", "stripe-example"}

    examples = mcp_registry.search("", kind="example")
    assert len(examples) == 6
    assert all(e["kind"] == "example" for e in examples)


def test_search_by_category(offline) -> None:
    payments = mcp_registry.search("", category="Payments")
    assert {e["slug"] for e in payments} == {"stripe", "paypal", "square"}


def test_search_no_match(offline) -> None:
    assert mcp_registry.search("zzz-nope") == []


# ── Cloud fetch path ─────────────────────────────────────────────────────────


def test_get_directory_uses_cloud(monkeypatch) -> None:
    fake = [
        {
            "slug": "acme",
            "name": "Acme",
            "kind": "preset",
            "category": "Dev",
            "transport": "http",
            "auth": "oauth",
            "oauthClient": "dcr",
            "url": "https://mcp.acme.com/mcp",
        },
        {
            "slug": "demo-example",
            "name": "Demo",
            "kind": "example",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "demo"],
        },
    ]
    monkeypatch.setattr(mcp_registry, "_fetch_full", lambda *a, **k: (fake, 3))

    d = mcp_registry.get_directory(force=True)
    assert d["source"] == "cloud"
    assert [p["slug"] for p in d["presets"]] == ["acme"]
    assert [e["slug"] for e in d["examples"]] == ["demo-example"]
    assert d["categories"] == ["Dev"]


def test_cache_then_bundled_on_later_failure(monkeypatch) -> None:
    calls = {"n": 0}

    def flaky(*_a, **_k):
        calls["n"] += 1
        return (
            ([{"slug": "acme", "name": "Acme", "kind": "preset"}], 1) if calls["n"] == 1 else None
        )

    monkeypatch.setattr(mcp_registry, "_fetch_full", flaky)
    first = mcp_registry.get_directory(force=True)
    assert first["source"] == "cloud"
    # A forced refetch that fails falls back to the last good copy, not empty —
    # served from the disk cache the successful fetch persisted.
    second = mcp_registry.get_directory(force=True)
    assert second["source"] == "disk"
    assert [p["slug"] for p in second["presets"]] == ["acme"]


# ── CLI: browse / search / install ───────────────────────────────────────────


def test_mcp_browse_cli(offline) -> None:
    result = CliRunner().invoke(cli, ["mcp", "browse", "--kind", "preset"])
    assert result.exit_code == 0, result.output
    assert "hubspot" in result.output
    assert "source: bundled" in result.output


def test_mcp_search_cli(offline) -> None:
    result = CliRunner().invoke(cli, ["mcp", "search", "notion"])
    assert result.exit_code == 0, result.output
    assert "notion" in result.output


def test_mcp_install_cli(offline) -> None:
    admin.create_agent("regcli", description="registry cli test")
    result = CliRunner().invoke(cli, ["mcp", "install", "linear", "--agent", "regcli"])
    assert result.exit_code == 0, result.output
    assert "Added" in result.output
    servers = admin.list_mcp_servers("regcli")
    assert any(s["url"] == "https://mcp.linear.app/mcp" for s in servers)


def test_mcp_install_unknown_slug(offline) -> None:
    result = CliRunner().invoke(cli, ["mcp", "install", "nope-not-real"])
    assert result.exit_code != 0
    assert "No directory entry" in result.output
