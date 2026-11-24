"""Which connectors the dashboard offers for a NEW connector.

Two reasons a row is suppressed, and they are not the same thing:

* ``deprecated`` — a classic ``BaseChannel`` type duplicated by a ``platform:``
  adapter that supersedes it. Every entry in ``CHANNEL_MAP`` is one now.
* ``hidden`` — a platform adapter that boots but has not been verified end to
  end yet. Nothing supersedes these; they are just unproven.

Both stay in the catalog so an agent already using one keeps an editable
connector, and both are withheld from the picker for new connectors.
"""

from __future__ import annotations

import re
from pathlib import Path

from olano.config.channel_secrets import CHANNEL_SECRET_KEYS
from olano.platforms.catalog import build_platform_catalog, hidden_channel_types

CATALOG_TS = (
    Path(__file__).resolve().parents[1] / "olano/dashboard_2.0/src/views/agents/connectorCatalog.ts"
)


def _rows() -> dict[str, dict]:
    return {row["type"]: row for row in build_platform_catalog()}


# ── deprecated: every classic adapter ───────────────────────────────────


def test_every_classic_channel_is_deprecated():
    """CHANNEL_MAP is duplicated end to end — no classic type may be offered."""
    from olano.runtime.server import CHANNEL_MAP

    rows = _rows()
    for ch_name in CHANNEL_MAP:
        assert rows[ch_name]["deprecated"] is True, ch_name


def test_each_deprecation_note_names_a_bootable_replacement():
    from olano.runtime.server import DEPRECATED_CHANNELS

    rows = _rows()
    for ch_name, replacement in DEPRECATED_CHANNELS.items():
        assert replacement in rows, f"{ch_name} points at a type that cannot boot"
        assert replacement in rows[ch_name]["note"]


def test_boot_warning_and_catalog_note_agree():
    """The runtime warning is the catalog's map, not a second copy of it."""
    from olano.platforms.catalog import DEPRECATED_CHANNELS as catalog_map
    from olano.runtime.server import DEPRECATED_CHANNELS as server_map

    assert server_map is catalog_map


# ── hidden: unverified platform adapters ────────────────────────────────


def test_unverified_platform_adapters_are_hidden():
    rows = _rows()
    for name in (
        "bluebubbles",
        "email",
        "homeassistant",
        "matrix",
        "mattermost",
        "webhook",
    ):
        row = rows[f"platform:{name}"]
        assert row["hidden"] is True, name
        assert row["note"], name
        # Hidden is not deprecated: nothing supersedes these.
        assert "deprecated" not in row, name


def test_verified_platform_adapters_stay_offered():
    rows = _rows()
    for name in (
        "telegram",
        "whatsapp",
        "discord",
        "slack",
        "signal",
        "feishu",
        # Verified once its turns ran on the agent's own runner and the
        # runtime published it at /api/openai/<agent>/v1.
        "api_server",
    ):
        row = rows[f"platform:{name}"]
        assert "hidden" not in row, name
        assert "deprecated" not in row, name


def test_hidden_channel_types_matches_the_flagged_rows():
    flagged = {
        r["type"] for r in build_platform_catalog() if r.get("deprecated") or r.get("hidden")
    }
    assert hidden_channel_types() == flagged


def test_every_offered_connector_is_a_platform_adapter():
    offered = [r for r in build_platform_catalog() if not (r.get("deprecated") or r.get("hidden"))]
    assert offered, "the picker cannot be empty"
    assert {r["kind"] for r in offered} == {"platform"}


# ── QR pairing metadata ─────────────────────────────────────────────────


def test_qr_connectors_advertise_pairing():
    """WhatsApp and WeChat pair by QR by default — the catalog must say so.

    The `pairing` field is what steers an agent (self_list_platforms /
    olano_list_platforms) to start the QR flow instead of asking the user for
    a token, and it is derived from the channel_setup connector registry so it
    can never drift from the machinery that actually runs the pairing.
    """
    rows = _rows()
    for t in ("platform:whatsapp", "platform:weixin"):
        assert rows[t].get("pairing") == "qr", t
        assert "default" in rows[t]["pairing_hint"].lower(), t
    # Token connectors must not claim a pairing flow they don't have.
    for t in ("platform:telegram", "platform:slack", "platform:discord"):
        assert "pairing" not in rows[t], t


# ── the dashboard's static fallback mirrors the backend ─────────────────


def test_dashboard_suppressed_types_mirror_the_backend():
    """`SUPPRESSED_TYPES` is the list shown when /api/catalog/channels fails.

    It is a hand-maintained copy, so it drifts silently unless something checks
    it — a connector deprecated here but missing there is offered to exactly the
    users whose runtime is already unreachable.
    """
    source = CATALOG_TS.read_text()
    block = re.search(r"const SUPPRESSED_TYPES = new Set\(\[(.*?)\]\)", source, re.S)
    assert block, "SUPPRESSED_TYPES not found in connectorCatalog.ts"
    mirrored = set(re.findall(r'"([^"]+)"', block.group(1)))
    assert mirrored == hidden_channel_types()


# ── the CLI writes maintained types too ─────────────────────────────────


def test_cli_channel_setup_writes_non_deprecated_types(monkeypatch, tmp_path):
    """`olano channel add <x>` must not write a type the picker refuses."""
    import click

    from olano.platforms import setup as channel_setup

    monkeypatch.setattr(click, "prompt", lambda *a, **k: k.get("default", "x"))
    for name, verifier in (
        ("telegram", "_verify_telegram"),
        ("slack", "_verify_slack"),
        ("discord", "_verify_discord"),
    ):
        monkeypatch.setattr(channel_setup, verifier, lambda *a, **k: None)
        written = channel_setup.get_setup_fn(name)(tmp_path)["type"]
        assert written == f"platform:{name}", name
        assert written not in hidden_channel_types(), name


# ── the dashboard's vault key names must be the backend's ────────────────


def test_dashboard_vault_keys_match_the_backend_map():
    """``vaultKey`` in the TS catalog is a duplicated string, so it can drift.

    The runtime resolves exactly the name in ``CHANNEL_SECRET_KEYS`` at boot,
    while the dashboard writes and reads exactly the name in the TS catalog.
    When they disagree the failure is silent in the worst direction: the form
    saves a key nothing reads, and the connector comes up with no credential —
    for ``api_server``, whose credential is not "required", with nothing
    reporting it at all.
    """
    declared = {key for fields in CHANNEL_SECRET_KEYS.values() for key in fields.values()}
    used = set(re.findall(r'vaultKey:\s*"([A-Z0-9_]+)"', CATALOG_TS.read_text(encoding="utf-8")))
    assert used, "no vaultKey entries found — has the catalog moved?"
    assert used <= declared, (
        f"dashboard names the backend does not resolve: {sorted(used - declared)}"
    )


# ── variants: Lark rides the Feishu adapter as its own picker row ────────


def test_feishu_row_carries_the_lark_variant():
    """Lark is Feishu's international edition on the same adapter.

    It used to be its own picker row and was folded into "Feishu / Lark" to
    shorten the list, which left Lark unreachable from the dashboard: the form
    had no domain field. The variant brings the row back without a second
    ``platform:`` type: same ``type`` and credentials, ``extra.domain`` preset.
    """
    from olano.platforms.catalog import connector_variants

    rows = _rows()
    variants = rows["platform:feishu"]["variants"]
    assert [v["id"] for v in variants] == ["lark"]
    assert variants[0]["preset"] == {"domain": "lark"}
    assert "Lark" in variants[0]["label"]
    assert connector_variants("platform:feishu") == variants
    assert connector_variants("feishu") == variants
    assert connector_variants("platform:telegram") == []
    # Only the one adapter has variants today; a new one must be deliberate.
    assert [r["type"] for r in build_platform_catalog() if r.get("variants")] == ["platform:feishu"]


def test_dashboard_lark_variant_mirrors_the_backend():
    """The TS catalog's static VARIANTS table is the offline copy of ``_VARIANTS``."""
    from olano.platforms.catalog import connector_variants

    source = CATALOG_TS.read_text(encoding="utf-8")
    block = re.search(r"const VARIANTS: Record<[^=]*= \{(.*?)\n\};", source, re.S)
    assert block, "VARIANTS not found in connectorCatalog.ts"
    for type_key, variants in re.findall(r'"([^"]+)": \[(.*?)\]', block.group(1), re.S):
        backend = {v["id"]: v["preset"] for v in connector_variants(type_key)}
        mirrored = {
            vid: dict(re.findall(r"(\w+): \"([^\"]+)\"", preset))
            for vid, preset in re.findall(r'id: "([^"]+)".*?preset: \{([^}]*)\}', variants, re.S)
        }
        assert mirrored == backend, type_key
    # The preset names a real field of the form, with the preset value among its options.
    feishu = re.search(r'"platform:feishu": \[(.*?)\n  \],', source, re.S)
    assert feishu and 'field("domain"' in feishu.group(1)
    assert '{ value: "lark", label: "catalog.feishu.domain.opt.lark" }' in feishu.group(1)


def test_variant_rows_stay_out_of_hidden_types():
    """A variant inherits its base row's visibility; it is never suppressed on its own."""
    assert not any("@" in t for t in hidden_channel_types())
