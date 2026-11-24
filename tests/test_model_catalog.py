"""Tests for olano/models/catalog.py."""

from __future__ import annotations

from olano.models.catalog import (
    filter_catalog,
    load_catalog,
    recommended_entries,
)


def test_load_catalog_non_empty():
    catalog = load_catalog()
    assert len(catalog) >= 1


def test_every_entry_has_name_and_provider():
    for entry in load_catalog():
        assert ":" in entry.name, f"entry {entry} has no provider prefix"
        assert entry.provider


def test_catalog_has_recommended_entries():
    recs = recommended_entries()
    assert len(recs) >= 1


def test_recommended_per_major_providers():
    catalog = load_catalog()
    rec_providers = {e.provider for e in catalog if e.recommended}
    # At least anthropic and openai have recommended models
    assert "anthropic" in rec_providers
    assert "openai" in rec_providers or "google_genai" in rec_providers


def test_cloud_entries_have_requires_keys():
    catalog = load_catalog()
    cloud = [e for e in catalog if not e.local]
    # At least some cloud entries require keys
    have_keys = [e for e in cloud if e.requires_keys]
    assert len(have_keys) >= 1


def test_local_entries_have_disk_gb():
    catalog = load_catalog()
    local = [e for e in catalog if e.local]
    if local:
        for e in local:
            assert e.disk_gb is not None, f"local entry {e.name} missing disk_gb"


def test_filter_catalog_by_display_name():
    catalog = load_catalog()
    filtered = filter_catalog(catalog, "sonnet")
    assert all(
        "sonnet" in e.display_name.lower() or "sonnet" in e.description.lower() for e in filtered
    )
    assert len(filtered) < len(catalog)


def test_filter_catalog_empty_query_returns_all():
    catalog = load_catalog()
    assert filter_catalog(catalog, "") == catalog


def test_entry_name_format_roundtrip():
    """provider:model strings must split cleanly."""
    for entry in load_catalog():
        provider, _, model = entry.name.partition(":")
        assert provider == entry.provider
        assert model  # non-empty model string
