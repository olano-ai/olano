"""The integration finder (`admin/integrations.py`).

What these guard:

* **Ranking is a table** — class order (channel < mcp < toolkit) is strict,
  score only orders within a class, at most three per class, then `limit`.
* **Every source is a catalog** — options are channels, MCP directory hits
  and built-in toolkits; nothing external is ever *listed* (it gets a note).
* **Connection state is per agent** — `already_connected` follows the vault
  as seen from the agent being asked about, not the process environment.
* **Connect steps are exact tool calls** — a stdio directory hit expands to
  `self_add_mcp_server(transport="stdio", command, command_args, env={KEY:
  "${vault:KEY}"})`, and the AgentFather surface renders `olano_*` twins.
"""

from __future__ import annotations

import pytest

from olano import admin
from olano.admin import integrations as integ
from olano.admin.integrations import (
    EXTERNAL_NOTE,
    POLICY,
    TOOLKIT_DENYLIST,
    _rank,
    find_integration,
    match_kind,
    normalise_query,
)
from olano.tools.talk_to import current_agent


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    """The developer's own environment must never decide a test."""
    monkeypatch.setattr("olano.tools._credentials.get_credential", lambda name, default="": default)
    monkeypatch.setattr(integ, "_google_connected", lambda agent: False)


def _opt(cls: str, id_: str, match: str = "exact", **extra) -> dict:
    return {"class": cls, "id": id_, "label": id_, "match": match, **extra}


# ── query normalisation & matching ───────────────────────────────────────────


class TestQuery:
    def test_filler_is_dropped_and_aliases_expand(self):
        q = normalise_query("Connect me to my GitHub please")
        assert q.tokens == ("github",)
        q = normalise_query("push my repo")
        assert set(q.alias_targets) == {"git", "github"}

    def test_multi_word_queries_join(self):
        q = normalise_query("microsoft 365")
        assert "microsoft365" in q.phrases

    def test_domain_noise_is_dropped_but_never_empties_the_query(self):
        """ "Parallel MCP" and "parallel" must ask the catalog the same thing.

        Stripped from the TOKENS, not just from `search_terms`: `probes` is
        tokens + phrases and feeds `match_kind` on all three legs.
        """
        assert normalise_query("Parallel MCP").tokens == ("parallel",)
        assert normalise_query("notion mcp server").tokens == ("notion",)
        assert normalise_query("Parallel MCP").phrases == ()
        # All-noise keeps the user's own words rather than raising.
        assert normalise_query("mcp server").tokens == ("mcp", "server")
        assert normalise_query("mcp").tokens == ("mcp",)

    def test_search_is_never_noise(self):
        """`search` names services ("Parallel Search") and aliases a toolkit."""
        assert "search" in normalise_query("web search").tokens
        assert normalise_query("parallel search").tokens == ("parallel", "search")

    def test_the_noise_set_has_one_definition(self):
        from olano.core.mcp_registry import DIRECTORY_NOISE

        assert normalise_query("parallel " + " ".join(DIRECTORY_NOISE)).tokens == ("parallel",)

    @pytest.mark.parametrize("query", ["", "   ", "connect to the"])
    def test_empty_query_is_an_error(self, query):
        with pytest.raises(ValueError, match="say what to connect"):
            normalise_query(query)

    @pytest.mark.parametrize(
        ("query", "keys", "aliases", "expected"),
        [
            ("notion", ["notion"], [], "exact"),
            ("gmail", ["google"], [], "alias"),
            ("gh", ["github"], [], "alias"),
            ("mail", ["google"], ["gmail"], None),
            ("calendar", ["google"], ["calendar"], "alias"),
            ("git", ["github"], [], "prefix"),
            ("hub", ["github"], [], "substring"),
            ("slak", ["slack"], [], "fuzzy"),
            ("whatsap", ["whatsapp"], [], "prefix"),
            ("x twitter", ["x_twitter"], [], "exact"),
            ("zzzz", ["slack"], [], None),
        ],
    )
    def test_match_kind_table(self, query, keys, aliases, expected):
        assert match_kind(normalise_query(query), keys, aliases) == expected


# ── ranking ──────────────────────────────────────────────────────────────────


class TestRank:
    def test_class_order_beats_score(self):
        ranked = _rank(
            [
                _opt("toolkit", "t", "exact"),
                _opt("mcp", "m", "fuzzy"),
                _opt("channel", "c", "fuzzy"),
            ],
            8,
        )
        assert [o["class"] for o in ranked] == ["channel", "mcp", "toolkit"]
        assert [o["rank"] for o in ranked] == [1, 2, 3]

    @pytest.mark.parametrize(
        ("option", "score"),
        [
            (_opt("toolkit", "a", "exact"), 100),
            (_opt("toolkit", "a", "alias"), 90),
            (_opt("toolkit", "a", "prefix"), 60),
            (_opt("toolkit", "a", "substring"), 40),
            (_opt("toolkit", "a", "fuzzy"), 20),
            (_opt("toolkit", "a", "exact", already_connected=True), 110),
            (_opt("toolkit", "a", "exact", on_agent=True), 105),
            (_opt("toolkit", "a", "exact", already_connected=True, on_agent=True), 115),
            (_opt("mcp", "a", "exact", entry_kind="example"), 75),
            (_opt("toolkit", "a", "exact", entry_kind="example"), 100),
        ],
    )
    def test_score_table(self, option, score):
        assert _rank([option], 8)[0]["score"] == score

    def test_within_class_by_score_then_label(self):
        ranked = _rank(
            [
                {"class": "mcp", "id": "b", "label": "b", "match": "exact"},
                {"class": "mcp", "id": "a", "label": "a", "match": "exact"},
                {
                    "class": "mcp",
                    "id": "c",
                    "label": "c",
                    "match": "exact",
                    "already_connected": True,
                },
            ],
            8,
        )
        assert [o["id"] for o in ranked] == ["c", "a", "b"]

    def test_at_most_three_per_class_then_limit(self):
        options = [_opt("mcp", f"m{i}") for i in range(5)] + [
            _opt("toolkit", f"t{i}") for i in range(5)
        ]
        ranked = _rank(options, 8)
        assert [o["class"] for o in ranked].count("mcp") == 3
        assert [o["class"] for o in ranked].count("toolkit") == 3
        assert len(_rank(options, 2)) == 2


# ── find_integration against the bundled catalogs ────────────────────────────


def _seed(name: str, **fields) -> None:
    admin.create_agent(name, model="fake:instant", **fields)


class TestFind:
    def test_result_shape_and_policy(self):
        _seed("finder")
        out = find_integration("notion", "finder")
        assert set(out) == {"query", "agent", "options", "external_note", "policy"}
        assert out["policy"] == POLICY
        assert out["external_note"] is None
        classes = [o["class"] for o in out["options"]]
        assert classes == sorted(classes, key=lambda c: {"channel": 0, "mcp": 1, "toolkit": 2}[c])
        for option in out["options"]:
            assert set(option) >= {
                "rank",
                "class",
                "id",
                "label",
                "match",
                "already_connected",
                "on_agent",
                "enables",
                "auth",
                "connect",
                "requires_restart",
                "how_to_get_it",
            }

    def test_never_returns_external(self):
        _seed("finder")
        out = find_integration("zzzz-no-such-service", "finder")
        assert out["options"] == []
        assert out["external_note"] == EXTERNAL_NOTE
        assert "self_inspect_external" in out["external_note"]

    def test_empty_query_raises(self):
        with pytest.raises(ValueError):
            find_integration("   ", "finder")

    def test_channel_ranks_first_with_the_magic_link_and_a_qr_alternative(self):
        _seed("finder")
        out = find_integration("whatsapp", "finder")
        first = out["options"][0]
        assert first["class"] == "channel"
        assert first["id"] == "platform:whatsapp"
        assert first["already_connected"] is False
        assert first["auth"]["best"] == "qr"
        assert first["connect"][0] == {
            "tool": "magic_link_connect_channels",
            "args": {"platforms": "whatsapp"},
            "requires_restart": False,
        }
        assert [c["tool"] for c in first["alternatives"]] == ["self_connect_whatsapp"]

    def test_a_configured_channel_reads_as_connected(self):
        _seed("finder", extra={"channels": [{"type": "platform:telegram"}]})
        out = find_integration("telegram", "finder")
        chan = next(o for o in out["options"] if o["class"] == "channel")
        assert chan["already_connected"] is True
        assert chan["connect"] == []

    @pytest.mark.parametrize(
        ("query", "expected_id", "expected_match"),
        [
            ("gmail", "google", "alias"),
            ("gh", "github", "alias"),
            ("push my repo", "github", "alias"),
        ],
    )
    def test_alias_hits(self, query, expected_id, expected_match):
        _seed("finder")
        out = find_integration(query, "finder")
        toolkit = next(
            o for o in out["options"] if o["class"] == "toolkit" and o["id"] == expected_id
        )
        assert toolkit["match"] == expected_match

    def test_denylisted_toolkits_are_never_options(self):
        _seed("finder")
        for name in sorted(TOOLKIT_DENYLIST):
            out = find_integration(name, "finder")
            assert not any(o["class"] == "toolkit" and o["id"] == name for o in out["options"]), (
                name
            )

    def test_already_connected_follows_the_agent_being_asked_about(self, monkeypatch):
        """The finder binds the target agent's credential scope, so the same
        query answers differently for two agents holding different vault
        overrides — and the AgentFather twin, asking as a different agent,
        gets the target's answer, not its own."""
        _seed("alpha")
        _seed("beta")

        def vault_backed(name: str, default: str = "") -> str:
            if name == "NOTION_API_KEY" and current_agent.get() == "alpha":
                return "ntn_secret"
            return default

        monkeypatch.setattr("olano.tools._credentials.get_credential", vault_backed)
        token = current_agent.set("agentfather")
        try:
            alpha = next(
                o for o in find_integration("notion", "alpha")["options"] if o["class"] == "toolkit"
            )
            beta = next(
                o for o in find_integration("notion", "beta")["options"] if o["class"] == "toolkit"
            )
        finally:
            current_agent.reset(token)
        assert alpha["already_connected"] is True
        assert beta["already_connected"] is False
        assert alpha["credentials"][0] == {"name": "NOTION_API_KEY", "resolved": True}
        # A connected toolkit that is not on the agent still needs the grant + restart.
        assert [c["tool"] for c in alpha["connect"]] == ["self_add_toolkit", "self_restart"]
        assert [c["tool"] for c in beta["connect"]] == [
            "self_add_toolkit",
            "magic_link_request_secret",
            "self_restart",
        ]
        assert beta["connect"][1]["args"]["name"] == "NOTION_API_KEY"

    def test_on_agent_toolkit_needs_no_restart(self):
        _seed("finder", tools=["notion_search"])
        out = find_integration("notion", "finder")
        toolkit = next(o for o in out["options"] if o["class"] == "toolkit")
        assert toolkit["on_agent"] is True
        assert toolkit["requires_restart"] is False
        assert "self_add_toolkit" not in [c["tool"] for c in toolkit["connect"]]
        assert toolkit["enables"]["kind"] == "toolkit"
        assert toolkit["enables"]["tool_count"] >= 5

    def test_provider_auth_is_ranked_official_byo_device_key(self, monkeypatch):
        _seed("finder")
        import olano.core.connectors as connectors

        monkeypatch.setattr(
            connectors,
            "connect_methods",
            lambda provider: {
                "official_oauth": False,
                "byo_oauth": True,
                "device": True,
                "key": ["GITHUB_TOKEN"],
            },
        )
        out = find_integration("github", "finder")
        toolkit = next(o for o in out["options"] if o["class"] == "toolkit" and o["id"] == "github")
        assert toolkit["auth"]["best"] == "byo_oauth"
        assert [m["method"] for m in toolkit["auth"]["methods"]] == [
            "official_oauth",
            "byo_oauth",
            "device",
            "key",
        ]
        assert toolkit["auth"]["methods"][0]["available"] is False
        assert "why" in toolkit["auth"]["methods"][0]
        link = next(c for c in toolkit["connect"] if c["tool"] == "magic_link_connect_service")
        assert link["args"] == {"service_name": "github", "method": "byo_oauth"}

    def test_key_only_toolkit_reports_key_and_the_secret_form(self):
        _seed("finder")
        out = find_integration("linear", "finder")
        toolkit = next(o for o in out["options"] if o["class"] == "toolkit" and o["id"] == "linear")
        assert toolkit["auth"] == {
            "best": "key",
            "methods": [{"method": "key", "available": True, "names": ["LINEAR_API_KEY"]}],
        }
        assert toolkit["how_to_get_it"][0]["name"] == "LINEAR_API_KEY"
        assert toolkit["how_to_get_it"][0]["doc_url"].startswith("https://")

    def test_mcp_oauth_hit_adds_then_signs_in(self):
        _seed("finder")
        out = find_integration("notion", "finder")
        mcp = next(o for o in out["options"] if o["class"] == "mcp")
        assert mcp["enables"] == {
            "kind": "mcp",
            "tool_count": None,
            "transport": "http",
            "auth": "oauth",
            "note": "known after the first connect",
        }
        assert [c["tool"] for c in mcp["connect"]] == [
            "self_add_mcp_server",
            "magic_link_connect_service",
        ]
        assert mcp["connect"][0]["args"]["auth"] == "oauth"
        assert mcp["connect"][1]["args"]["service_name"] == mcp["connect"][0]["args"]["url"]
        assert mcp["requires_restart"] is False

    def test_mcp_hit_says_when_the_server_wants_the_users_own_oauth_app(self):
        """HubSpot refuses automatic client registration: the agent must be
        able to tell the person before the link, not after it asks."""
        _seed("finder")
        hubspot = next(
            o for o in find_integration("hubspot", "finder")["options"] if o["class"] == "mcp"
        )
        assert hubspot["auth"]["oauth_client"] == "preregistered"
        assert "Client ID" in hubspot["auth"]["note"]
        assert "Composio" in hubspot["auth"]["note"]
        notion = next(
            o for o in find_integration("notion", "finder")["options"] if o["class"] == "mcp"
        )
        assert notion["auth"]["oauth_client"] == "dcr"
        assert "note" not in notion["auth"]

    def test_mcp_added_but_unauthorized_only_needs_the_sign_in(self, monkeypatch):
        _seed("finder")
        entry = admin.search_mcp_directory("notion")[0]
        admin.add_mcp_server("finder", entry["url"], transport=entry["transport"], auth="oauth")
        monkeypatch.setattr("olano.core.mcp_setup.has_oauth_tokens", lambda agent, url: False)
        mcp = next(
            o for o in find_integration("notion", "finder")["options"] if o["class"] == "mcp"
        )
        assert mcp["on_agent"] is True and mcp["already_connected"] is False
        assert [c["tool"] for c in mcp["connect"]] == ["magic_link_connect_service"]
        monkeypatch.setattr("olano.core.mcp_setup.has_oauth_tokens", lambda agent, url: True)
        mcp = next(
            o for o in find_integration("notion", "finder")["options"] if o["class"] == "mcp"
        )
        assert mcp["already_connected"] is True and mcp["connect"] == []

    def test_stdio_directory_hit_expands_to_the_full_add_call(self, monkeypatch):
        _seed("finder")
        entry = {
            "slug": "acme-stdio",
            "name": "Acme",
            "kind": "preset",
            "transport": "stdio",
            "auth": "none",
            "command": "npx",
            "args": ["-y", "@acme/mcp-server"],
            "envKeys": ["ACME_API_KEY"],
            "appConsoleUrl": "https://acme.test/console",
            "appConsoleLabel": "Settings → API keys",
        }
        monkeypatch.setattr("olano.admin.mcp.search_mcp_directory", lambda q, **kw: [entry])
        mcp = next(o for o in find_integration("acme", "finder")["options"] if o["class"] == "mcp")
        assert [c["tool"] for c in mcp["connect"]] == [
            "magic_link_request_secret",
            "self_add_mcp_server",
            "self_restart",
        ]
        assert mcp["connect"][0]["args"]["name"] == "ACME_API_KEY"
        add = mcp["connect"][1]
        assert add["args"] == {
            "url": "acme-stdio",
            "transport": "stdio",
            "command": "npx",
            "command_args": ["-y", "@acme/mcp-server"],
            "env": {"ACME_API_KEY": "${vault:ACME_API_KEY}"},
        }
        assert add["requires_restart"] is True
        assert mcp["how_to_get_it"] == [
            {
                "name": "ACME_API_KEY",
                "doc_url": "https://acme.test/console",
                "guidance": "Settings → API keys",
            }
        ]

    def test_api_key_remote_hit_uses_a_vault_bearer_header(self, monkeypatch):
        _seed("finder")
        entry = {
            "slug": "acme",
            "name": "Acme",
            "kind": "preset",
            "transport": "http",
            "auth": "none",
            "url": "https://mcp.acme.test/mcp",
            "envKeys": ["ACME_KEY"],
        }
        monkeypatch.setattr("olano.admin.mcp.search_mcp_directory", lambda q, **kw: [entry])
        mcp = next(o for o in find_integration("acme", "finder")["options"] if o["class"] == "mcp")
        add = next(c for c in mcp["connect"] if c["tool"] == "self_add_mcp_server")
        assert add["args"]["headers"] == {"Authorization": "Bearer ${vault:ACME_KEY}"}
        assert mcp["auth"]["best"] == "key"

    def test_example_entries_rank_below_presets(self, monkeypatch):
        _seed("finder")
        entries = [
            {
                "slug": "acme-example",
                "name": "Acme",
                "kind": "example",
                "transport": "http",
                "auth": "none",
                "url": "https://x.test/mcp",
            },
            {
                "slug": "acme",
                "name": "Acme",
                "kind": "preset",
                "transport": "http",
                "auth": "none",
                "url": "https://y.test/mcp",
            },
        ]
        monkeypatch.setattr("olano.admin.mcp.search_mcp_directory", lambda q, **kw: list(entries))
        mcps = [o for o in find_integration("acme", "finder")["options"] if o["class"] == "mcp"]
        assert [o["id"] for o in mcps] == ["acme", "acme-example"]

    def test_a_failing_source_is_skipped_not_fatal(self, monkeypatch):
        """A dead MCP leg costs its own options, never the whole answer.

        Both directory seams must be broken now: the leg makes two independent
        passes, and killing only one is the case below.
        """
        _seed("finder")

        def boom(*a, **k):
            raise RuntimeError("directory down")

        monkeypatch.setattr("olano.admin.mcp.search_mcp_directory", boom)
        monkeypatch.setattr("olano.admin.mcp.directory_entries", boom)
        out = find_integration("notion", "finder")
        assert any(o["class"] == "toolkit" for o in out["options"])
        assert not any(o["class"] == "mcp" for o in out["options"])

    def test_the_local_pass_survives_a_dead_directory_search(self, monkeypatch):
        """The remote free-text lookup failing no longer hides the catalog.

        The two passes are isolated from each other, so a directory search that
        raises (or times out) leaves the local match standing.
        """
        _seed("finder")

        def boom(*a, **k):
            raise RuntimeError("directory down")

        monkeypatch.setattr("olano.admin.mcp.search_mcp_directory", boom)
        out = find_integration("notion", "finder")
        mcp = next(o for o in out["options"] if o["class"] == "mcp")
        assert mcp["id"] == "notion" and mcp["match"] == "exact"

    def test_a_domain_word_does_not_hide_a_directory_hit(self):
        """Regression: `self_find_integration("Parallel MCP")` found nothing.

        The MCP leg was the only one that handed matching to a remote
        whole-phrase `ilike`: the tokens were re-joined into "parallel mcp",
        no catalog field contains that contiguous string, and the finder
        answered with EXTERNAL_NOTE — while `self_search_mcp_directory`
        ("parallel") hit the same row immediately.

        No monkeypatch: `conftest` pins the cloud fetch off, so this runs the
        whole chain (noise strip → local enumeration → grading → rank) against
        the bundled snapshot.
        """
        _seed("finder")
        out = find_integration("Parallel MCP", "finder")
        assert out["external_note"] is None
        mcp = next(o for o in out["options"] if o["class"] == "mcp")
        assert mcp["id"] == "parallel"
        # `exact`, not the `fuzzy` label the remote pass falls back to — proves
        # the local pass produced it.
        assert mcp["match"] == "exact"
        add = next(c for c in mcp["connect"] if c["tool"] == "self_add_mcp_server")
        assert add["args"]["url"] == "https://search.parallel.ai/mcp"
        assert add["args"]["transport"] == "http"
        # Free, no key: nothing to collect before adding it.
        assert not any(c["tool"] == "magic_link_request_secret" for c in mcp["connect"])
        assert mcp["auth"]["best"] == "none"

    @pytest.mark.parametrize(
        "query",
        ["notion", "notion mcp", "notion mcp server", "connect me to the notion mcp server"],
    )
    def test_an_extra_word_never_hides_a_directory_entry(self, query):
        """The MCP leg is now as word-tolerant as the channel and toolkit legs."""
        _seed("finder")
        mcp = next(o for o in find_integration(query, "finder")["options"] if o["class"] == "mcp")
        assert mcp["id"] == "notion" and mcp["match"] == "exact"

    def test_a_hit_only_the_remote_knows_still_ranks(self, monkeypatch):
        """The free-text pass is not dead code: it carries entries the local
        copy has never seen (added cloud-side since the last sync) and the
        description-driven hits the identity keyset deliberately misses."""
        _seed("finder")
        entry = {
            "slug": "acme",
            "name": "Acme",
            "kind": "preset",
            "transport": "http",
            "auth": "none",
            "url": "https://mcp.acme.test/mcp",
        }
        monkeypatch.setattr("olano.admin.mcp.search_mcp_directory", lambda q, **kw: [entry])
        mcp = next(o for o in find_integration("acme", "finder")["options"] if o["class"] == "mcp")
        assert mcp["id"] == "acme" and mcp["match"] == "exact"

    def test_a_paren_qualifier_does_not_block_an_exact_hit(self, monkeypatch):
        """Catalog names carry a pricing/auth qualifier; the brand is what a
        user says ("Parallel Search (Free · no API key)")."""
        _seed("finder")
        monkeypatch.setattr(
            "olano.admin.mcp.directory_entries",
            lambda: [{"slug": "zz-1", "name": "Acme (Free · no API key)", "kind": "preset"}],
        )
        monkeypatch.setattr("olano.admin.mcp.search_mcp_directory", lambda q, **kw: [])
        mcp = next(o for o in find_integration("acme", "finder")["options"] if o["class"] == "mcp")
        assert mcp["id"] == "zz-1" and mcp["match"] == "exact"

    def test_the_local_pass_is_bounded_and_deterministic(self, monkeypatch):
        """526 entries and growing: bound the option build, and never let a tie
        shuffle between runs (CI runs xdist)."""
        _seed("finder")
        rows = [
            {"slug": f"acme-{i:03d}", "name": "Acme", "kind": "preset", "auth": "none"}
            for i in range(200)
        ]
        monkeypatch.setattr("olano.admin.mcp.directory_entries", lambda: list(rows))
        monkeypatch.setattr("olano.admin.mcp.search_mcp_directory", lambda q, **kw: [])
        query = integ.normalise_query("acme")
        assert len(integ._mcp_options(query, None, "finder", "self")) == integ._MCP_LOCAL_LIMIT
        first = [o["id"] for o in find_integration("acme", "finder")["options"]]
        assert first == [o["id"] for o in find_integration("acme", "finder")["options"]]
        assert len([i for i in first if i.startswith("acme-")]) == integ.PER_CLASS_CAP

    def test_a_near_miss_is_not_promoted_by_the_local_pass(self, monkeypatch):
        """The local pass takes exact/alias only. The directory is ~5x the
        toolkit catalog, so the looser tiers answered "notion" with motion and
        "google drive" with bigquery; that is the free-text pass's job."""
        _seed("finder")
        monkeypatch.setattr(
            "olano.admin.mcp.directory_entries",
            lambda: [{"slug": "motion", "name": "Motion", "kind": "preset", "auth": "none"}],
        )
        monkeypatch.setattr("olano.admin.mcp.search_mcp_directory", lambda q, **kw: [])
        assert not [
            o for o in find_integration("notion", "finder")["options"] if o["class"] == "mcp"
        ]

    def test_admin_surface_renders_olano_twins_carrying_the_agent(self):
        _seed("finder")
        out = find_integration("notion", "finder", surface="admin")
        tools = {c["tool"] for o in out["options"] for c in o["connect"]}
        assert not any(t.startswith("self_") for t in tools)
        toolkit = next(o for o in out["options"] if o["class"] == "toolkit")
        assert toolkit["connect"][0] == {
            "tool": "olano_add_toolkit",
            "args": {"agent": "finder", "name": "notion"},
            "requires_restart": True,
        }
        assert toolkit["connect"][-1] == {
            "tool": "olano_restart_agent",
            "args": {"name": "finder"},
            "requires_restart": False,
        }
        for call in (
            c for o in out["options"] for c in o["connect"] if c["tool"].startswith("magic_link")
        ):
            assert call["args"]["agent"] == "finder"

    def test_a_missing_agent_config_still_answers(self):
        out = find_integration("notion", "ghost")
        assert out["options"]
        assert all(o["already_connected"] is False for o in out["options"] if o["class"] == "mcp")

    def test_lead_sentence_names_every_option_in_order(self):
        _seed("finder")
        out = find_integration("notion", "finder")
        lead = integ.lead_sentence(out)
        assert lead.startswith("Supported ways to connect 'notion', in order: 1. ")
        for option in out["options"]:
            assert f"{option['rank']}. {option['label']} ({option['class']}" in lead
        assert integ.lead_sentence({"options": [], "external_note": EXTERNAL_NOTE}) == EXTERNAL_NOTE


# ── toolkit grants ───────────────────────────────────────────────────────────


class TestToolkitGrants:
    def test_add_expands_to_member_names_and_skips_present_ones(self):
        _seed("grant", tools=["notion_search", "web_search"])
        out = admin.add_toolkit("grant", "notion")
        assert "notion_search" in out["already_present"]
        assert "notion_create_page" in out["added"]
        assert out["requires_restart"] is True
        assert {c["name"] for c in out["credentials"]} >= {"NOTION_API_KEY"}
        tools = admin.get_agent("grant")["tools"]
        assert tools[:2] == ["notion_search", "web_search"]  # existing order kept
        assert set(tools) >= set(integ.expand_toolkit("notion"))
        again = admin.add_toolkit("grant", "notion")
        assert again["added"] == [] and "nothing to do" in again["next"]

    def test_remove_drops_only_member_names(self):
        _seed("grant", tools=["notion_search", "web_search", "notion_create_page"])
        before = admin.get_agent("grant")["tools"]
        out = admin.remove_toolkit("grant", "notion")
        assert sorted(out["removed"]) == ["notion_create_page", "notion_search"]
        after = admin.get_agent("grant")["tools"]
        # Only the two member names went; everything else (web_search and the
        # default internal tools every writer persists) is untouched, in order.
        assert after == [t for t in before if not t.startswith("notion_")]
        assert "web_search" in after
        nothing = admin.remove_toolkit("grant", "notion")
        assert nothing["removed"] == [] and "disabled_tools" in nothing["next"]

    def test_composite_bundles_expand_too(self):
        _seed("grant")
        out = admin.add_toolkit("grant", "web")
        assert "page_read" in out["added"]

    @pytest.mark.parametrize("name", sorted(TOOLKIT_DENYLIST))
    def test_denylist_is_refused_before_any_write(self, name):
        _seed("grant")
        before = admin.get_agent("grant")["tools"]
        with pytest.raises(ValueError, match="cannot be granted"):
            admin.add_toolkit("grant", name)
        assert admin.get_agent("grant")["tools"] == before

    def test_unknown_toolkit_is_refused(self):
        _seed("grant")
        with pytest.raises(ValueError, match="unknown toolkit"):
            admin.add_toolkit("grant", "no-such-toolkit")

    def test_members_come_from_the_static_index_without_toolkit_imports(self, monkeypatch):
        import sys

        members = integ.toolkit_members()
        assert "notion_search" in members["notion"]
        assert "olano_admin" in members  # enumerated, only the finder hides it
        # The heavy toolkits stay unimported by the enumeration itself.
        monkeypatch.delitem(sys.modules, "olano.tools.google_tools", raising=False)
        integ.toolkit_members()
        assert "olano.tools.google_tools" not in sys.modules


# ── one brand, two products: WeChat vs WeCom; one adapter, two rows: Feishu / Lark ──


class TestWeChatAndLark:
    """Name matching alone confused the two WeChats in both directions.

    WeCom's old label carried the word "wechat", so "connect wechat" offered
    WeCom as a second option; "enterprise wechat" ranked the personal
    messenger FIRST (an exact hit) over WeCom (a substring hit). The flavour
    rule skips the other product outright, the lead sentence names every
    option, so out-scoring it was never enough.
    """

    @staticmethod
    def _channel_ids(query: str) -> list[str]:
        _seed("finder")
        out = find_integration(query, "finder")
        return [o["id"] for o in out["options"] if o["class"] == "channel"]

    @pytest.mark.parametrize(
        "query",
        ["wechat", "connect me to wechat please", "weixin", "wechat for my work team"],
    )
    def test_a_plain_wechat_is_the_personal_messenger_only(self, query):
        ids = self._channel_ids(query)
        assert "platform:weixin" in ids
        assert "platform:wecom" not in ids

    @pytest.mark.parametrize(
        "query",
        [
            "wecom",
            "enterprise wechat",
            "wechat enterprise",
            "connect my company wechat",
            "wechat work",
            "work wechat",
            "corporate weixin",
        ],
    )
    def test_an_enterprise_qualifier_is_wecom_only(self, query):
        ids = self._channel_ids(query)
        assert ids == ["platform:wecom"], ids

    def test_wecom_gets_at_least_an_alias_grade_when_only_the_qualifier_names_it(self):
        _seed("finder")
        out = find_integration("enterprise wechat", "finder")
        wecom = next(o for o in out["options"] if o["id"] == "platform:wecom")
        assert wecom["match"] == "alias"
        # The WeCom toolkit (the corp API pack) rides the same rule and the
        # keyword table, and the personal messenger stays out of the list.
        toolkits = [o["id"] for o in out["options"] if o["class"] == "toolkit"]
        assert "wecom" in toolkits
        assert not any(o["id"] == "platform:weixin" for o in out["options"])

    def test_the_wecom_toolkit_never_answers_a_plain_wechat(self):
        _seed("finder")
        out = find_integration("wechat", "finder")
        assert not any(o["class"] == "toolkit" and o["id"] == "wecom" for o in out["options"])

    def test_flavour_table(self):
        flavour = integ.wechat_flavour
        assert flavour(normalise_query("wechat")) == "personal"
        assert flavour(normalise_query("weixin")) == "personal"
        assert flavour(normalise_query("wechat business")) == "personal"
        assert flavour(normalise_query("wecom")) == "enterprise"
        assert flavour(normalise_query("enterprise wechat")) == "enterprise"
        assert flavour(normalise_query("wechat work")) == "enterprise"
        # "work" counts only right beside the WeChat word.
        assert flavour(normalise_query("wechat for my work team")) == "personal"
        assert flavour(normalise_query("telegram")) is None

    def test_the_catalog_labels_carry_the_brand_word_exactly_once(self):
        """The rule leans on the labels: "WeChat" must name one connector."""
        from olano.platforms.catalog import build_platform_catalog

        labels = {r["type"]: r["label"] for r in build_platform_catalog()}
        assert labels["platform:weixin"].startswith("WeChat (")
        assert labels["platform:wecom"].startswith("WeCom (")
        assert "iLink" in labels["platform:weixin"]
        assert "Enterprise WeChat" in labels["platform:wecom"]
        # Stripped of its parenthetical (what the finder keys on), WeCom's
        # label no longer contains "wechat", the substring hit is gone.
        assert "wechat" not in integ._PAREN_SUFFIX.sub("", labels["platform:wecom"]).lower()

    def test_lark_is_an_exact_hit_on_the_feishu_row_and_carries_the_variant(self):
        _seed("finder")
        out = find_integration("lark", "finder")
        chan = next(o for o in out["options"] if o["class"] == "channel")
        assert chan["id"] == "platform:feishu"
        assert chan["match"] == "exact"
        assert chan["enables"]["variants"] == [
            {
                "id": "lark",
                "label": "Lark (international Feishu · platform adapter)",
                "preset": {"domain": "lark"},
            }
        ]
        # A row without variants carries no key at all.
        out = find_integration("telegram", "finder")
        chan = next(o for o in out["options"] if o["class"] == "channel")
        assert "variants" not in chan["enables"]


class TestRefusedConnection:
    def test_a_marked_connection_is_not_already_connected(self, monkeypatch):
        """ "Connect me to Salesforce" on a refused token lists the sign-in, with the reason."""
        from olano.config.home import get_olano_paths
        from olano.core.connectors import base as broker
        from olano.core.connectors import get_provider

        _seed("finder")
        provider = get_provider("salesforce")
        assert provider is not None
        path = get_olano_paths().connector_token_path("salesforce")
        broker._save_record(
            {
                "service": "salesforce",
                "access_token": "dead",
                "expires_at": None,
                "client_id": "c",
                "client_secret": "s",
                "params": {},
                "extras": {"instance_url": "https://org.my.salesforce.com"},
            },
            path,
        )
        # The credential probe is monkeypatched off by the module fixture; the
        # broker mark is what this test is about. The box holds an OAuth client
        # (as any box with a connection does), so the sign-in step is a link.
        monkeypatch.setattr(integ, "_resolves", lambda name: True)
        monkeypatch.setattr(
            integ,
            "_provider_auth",
            lambda pid, required: {"provider": pid, "best": "byo_oauth", "methods": []},
        )
        try:
            out = find_integration("salesforce", "finder")
            toolkit = next(
                o for o in out["options"] if o["class"] == "toolkit" and o["id"] == "salesforce"
            )
            assert toolkit["already_connected"] is True
            assert "needs_reauth" not in toolkit

            broker.mark_auth_failed(provider, error="sf_query: 401 INVALID_SESSION_ID")
            out = find_integration("salesforce", "finder")
            toolkit = next(
                o for o in out["options"] if o["class"] == "toolkit" and o["id"] == "salesforce"
            )
            assert toolkit["already_connected"] is False
            assert toolkit["needs_reauth"] is True
            assert "INVALID_SESSION_ID" in toolkit["auth_error"]
            assert any(c["tool"] == "magic_link_connect_service" for c in toolkit["connect"])
        finally:
            broker._RECORD_CACHE.clear()
