"""The agent handle is minted from the display name, never chosen.

A person names an agent ("Hillary"); its permanent key follows from that the
way a human's ``username`` follows from their name. The slug rule here must
match the dashboard's preview (``helpers.slugifyAgentName``) exactly — the
form shows the handle it expects, and the server must write that one.
"""

from __future__ import annotations

from olano.config.agent_names import (
    MAX_AGENT_NAME_LEN,
    is_valid_agent_name,
    mint_agent_name,
    slugify_agent_name,
)

# ------------------------------------------------------------------ slugify


def test_slugify_matches_the_dashboard_preview_rule():
    assert slugify_agent_name("Hillary") == "hillary"
    assert slugify_agent_name("Merli the Analyst") == "merli-the-analyst"
    # Runs of anything foreign collapse to one dash; ends are trimmed.
    assert slugify_agent_name("  Ana's Help Desk!  ") == "ana-s-help-desk"
    # Underscores are legal handle characters and survive.
    assert slugify_agent_name("sales_helper") == "sales_helper"


def test_slugify_never_starts_with_a_dash_or_underscore():
    assert slugify_agent_name("--_-Hillary") == "hillary"
    assert slugify_agent_name("_private") == "private"


def test_slugify_of_an_unslugifiable_name_is_empty():
    """All-CJK or all-punctuation: nothing survives, and the caller decides
    the fallback — never an empty string into a path."""
    assert slugify_agent_name("小明") == ""
    assert slugify_agent_name("!!!") == ""
    assert slugify_agent_name("") == ""
    assert slugify_agent_name(None) == ""


def test_slugify_caps_the_length_the_server_accepts():
    slug = slugify_agent_name("a" * 200)
    assert len(slug) == MAX_AGENT_NAME_LEN
    assert is_valid_agent_name(slug)


# -------------------------------------------------------------------- mint


def test_mint_uses_the_slug_when_free():
    assert mint_agent_name("Hillary", taken=[]) == "hillary"


def test_mint_suffixes_on_collision_instead_of_refusing():
    """The person never asked for a specific handle, so there is nothing to
    refuse — and a "taken" error on a field they cannot edit is a dead end."""
    assert mint_agent_name("Hillary", taken=["hillary"]) == "hillary-2"
    assert mint_agent_name("Hillary", taken=["hillary", "hillary-2"]) == "hillary-3"


def test_mint_compares_case_insensitively_like_agent_lookups_do():
    assert mint_agent_name("Hillary", taken=["HILLARY"]) == "hillary-2"


def test_mint_falls_back_to_something_meaningful():
    """An unslugifiable display name on an archetype install yields the
    archetype's slug, so the handle still says what the agent is."""
    assert mint_agent_name("小明", taken=[], fallback="recruitment-specialist") == (
        "recruitment-specialist"
    )
    assert mint_agent_name(
        "小明", taken=["recruitment-specialist"], fallback="recruitment-specialist"
    ) == ("recruitment-specialist-2")
    assert mint_agent_name("!!!", taken=[]) == "agent"


def test_mint_keeps_a_suffixed_handle_inside_the_length_cap():
    long = "a" * MAX_AGENT_NAME_LEN
    minted = mint_agent_name(long, taken=[long])
    assert minted.endswith("-2")
    assert len(minted) <= MAX_AGENT_NAME_LEN
    assert is_valid_agent_name(minted)


def test_mint_ignores_blank_entries_in_taken():
    assert mint_agent_name("Hillary", taken=["", None, "  "]) == "hillary"  # type: ignore[list-item]


# -------------------------------------------------------------- validation


def test_is_valid_agent_name_is_the_server_rule():
    assert is_valid_agent_name("hillary")
    assert is_valid_agent_name("a1_b-c")
    assert not is_valid_agent_name("Hillary")
    assert not is_valid_agent_name("-lead")
    assert not is_valid_agent_name("../../etc")
    assert not is_valid_agent_name("")
    assert not is_valid_agent_name(None)
    assert not is_valid_agent_name("a" * (MAX_AGENT_NAME_LEN + 1))
