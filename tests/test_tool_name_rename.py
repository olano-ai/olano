"""Guards for the P4 ``lionclaw_* -> olano_*`` tool rename.

Renaming a tool is unusually easy to half-finish, because nothing fails loudly:
:func:`olano.tools.registry.resolve_tools` prints ``unknown tool 'x', skipping``
and carries on. An agent whose config names a tool that no longer exists boots
clean, reports healthy, and simply has fewer tools than its owner thinks.

So these tests assert the two halves agree — every tool name any shipped config
asks for must actually resolve — and that the migration converts a real,
deployed-shaped config without touching the things that only look similar.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


def _load_migration():
    path = _REPO / "olano" / "migrations" / "0010_olano_tool_names.py"
    spec = importlib.util.spec_from_file_location("m0010", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the two halves must agree ────────────────────────────────────────────────


def test_no_shipped_config_asks_for_a_tool_that_does_not_exist() -> None:
    """Every tool named by a bundled archetype/agent must resolve.

    This is the check that makes a half-finished rename fail loudly. Without
    it, a template can name a tool the registry dropped and nothing complains
    until an agent quietly can't do its job.
    """
    from olano.tools._name_index import NAME_INDEX

    known = set(NAME_INDEX)
    # Toolkit/bundle names are legal entries too, not just individual tools.
    from olano.tools.registry import _ALIASES, _COMPOSITE_BUNDLES, _TOOLKITS

    known |= set(_TOOLKITS) | set(_ALIASES) | set(_COMPOSITE_BUNDLES)

    missing: list[str] = []
    roots = [_REPO / "olano" / "archetypes" / "templates", _REPO / "agents"]
    for root in roots:
        if not root.is_dir():
            continue
        for cfg in sorted(root.rglob("*.json5")):
            raw = cfg.read_text(encoding="utf-8")
            from olano.config.loader import _load_json5

            try:
                data = _load_json5(raw) or {}
            except Exception:
                continue
            for name in data.get("tools") or []:
                # Bundles/categories resolve through other paths; only flag
                # names that look like our admin toolkit's.
                if isinstance(name, str) and name.startswith(("olano_", "lionclaw_")):
                    if name not in known:
                        missing.append(f"{cfg.relative_to(_REPO)}: {name}")

    assert not missing, "config names a tool that does not resolve:\n  " + "\n  ".join(missing)


def test_no_lionclaw_prefixed_tool_names_remain_in_the_registry() -> None:
    from olano.tools._name_index import NAME_INDEX

    stale = sorted(n for n in NAME_INDEX if n.startswith("lionclaw_"))
    assert not stale, f"tool names still carry the old prefix: {stale}"


def test_the_admin_toolkit_still_exposes_every_tool() -> None:
    """A rename that drops tools on the floor is worse than no rename."""
    from olano.tools.registry import resolve_bundle

    tools = resolve_bundle("olano_admin")
    # 55 at the 0010 rename; +10 user-admin, +2 disk, +1 skill-repair (0017);
    # +4 integrations (find/inspect/add-toolkit/remove-toolkit);
    # +1 external access (olano_set_agent_external_access, 0019);
    # +6 fleet scheduler (list/get/create/update/delete/run_now, 0021);
    # +2 browser auto-approval rules (olano_get/set_browser_auto_approvals);
    # +1 olano_test_agent (0026);
    # +5 fleet Cortex review (list/view/discard/restore/promote, 0028).
    assert len(tools) == 87, f"expected 87 admin tools, got {len(tools)}"
    assert all(t.name.startswith("olano_") for t in tools)


# ── the migration ────────────────────────────────────────────────────────────


@pytest.fixture
def migration():
    return _load_migration()


def _write(home: Path, name: str, cfg: dict) -> Path:
    d = home / "agents" / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "agent.json5"
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def test_migration_renames_tools_disabled_tools_and_nested_subagents(migration, tmp_path) -> None:
    p = _write(
        tmp_path,
        "agentfather",
        {
            "name": "agentfather",
            "tools": ["lionclaw_list_agents", "web_search"],
            # An opt-out must survive AS an opt-out, or the migration silently
            # re-enables a tool its owner turned off.
            "disabled_tools": ["lionclaw_delete_agent"],
            "subagents": [
                {
                    "name": "helper",
                    "tools": ["lionclaw_tail_log"],
                    "subagents": [{"name": "deep", "tools": ["lionclaw_doctor"]}],
                }
            ],
        },
    )
    migration.apply(tmp_path)
    out = json.loads(p.read_text(encoding="utf-8"))

    assert out["tools"] == ["olano_list_agents", "web_search"]
    assert out["disabled_tools"] == ["olano_delete_agent"]
    assert out["subagents"][0]["tools"] == ["olano_tail_log"]
    assert out["subagents"][0]["subagents"][0]["tools"] == ["olano_doctor"]


def test_migration_leaves_mcp_server_tool_names_alone(migration, tmp_path) -> None:
    """``mcp_servers[].disabled_tools`` holds the SERVER's tool names.

    They share our namespace only by coincidence; rewriting them would silently
    re-enable a tool the operator disabled on a third-party MCP server.
    """
    p = _write(
        tmp_path,
        "a",
        {
            "name": "a",
            "tools": ["lionclaw_doctor"],
            "mcp_servers": [{"url": "https://x/mcp", "disabled_tools": ["lionclaw_doctor"]}],
        },
    )
    migration.apply(tmp_path)
    out = json.loads(p.read_text(encoding="utf-8"))

    assert out["tools"] == ["olano_doctor"], "our own tools should be renamed"
    assert out["mcp_servers"][0]["disabled_tools"] == ["lionclaw_doctor"], (
        "an MCP server's own tool names must not be rewritten"
    )


def test_migration_updates_the_prompt_without_mangling_similar_words(migration, tmp_path) -> None:
    """The prompt tells the model which tools to call — it has to move too.

    But only on whole-word matches: ``lionclaw_admin_helper`` is a different
    identifier and must survive.
    """
    p = _write(
        tmp_path,
        "a",
        {
            "name": "a",
            "system_prompt": (
                "Call `lionclaw_list_agents`, then lionclaw_scaffold_archetype. "
                "Never touch lionclaw_admin_helper or the lionclaw_docker backend."
            ),
        },
    )
    migration.apply(tmp_path)
    prompt = json.loads(p.read_text(encoding="utf-8"))["system_prompt"]

    assert "olano_list_agents" in prompt
    assert "olano_scaffold_archetype" in prompt
    assert "lionclaw_admin_helper" in prompt, "word-boundary match failed"
    assert "lionclaw_docker" in prompt, "the backend type is not a tool and must not move"


def test_migration_is_idempotent_and_does_not_touch_clean_configs(migration, tmp_path) -> None:
    clean = _write(tmp_path, "clean", {"name": "clean", "tools": ["olano_doctor", "web_search"]})
    before = clean.read_text(encoding="utf-8")

    migration.apply(tmp_path)
    assert clean.read_text(encoding="utf-8") == before, "a clean config was rewritten"

    dirty = _write(tmp_path, "dirty", {"name": "dirty", "tools": ["lionclaw_doctor"]})
    migration.apply(tmp_path)
    once = dirty.read_text(encoding="utf-8")
    migration.apply(tmp_path)
    assert dirty.read_text(encoding="utf-8") == once, "second run changed the file again"


def test_every_renamed_name_actually_resolves_after_migration(migration) -> None:
    """The migration's targets must be real tools, not aspirational names.

    A typo in the rename table would write a name that resolves to nothing —
    the exact silent failure this migration exists to prevent.
    """
    from olano.tools._name_index import NAME_INDEX
    from olano.tools.registry import _ALIASES, _COMPOSITE_BUNDLES, _TOOLKITS

    known = set(NAME_INDEX) | set(_TOOLKITS) | set(_ALIASES) | set(_COMPOSITE_BUNDLES)
    unknown = sorted(new for new in migration.RENAMES.values() if new not in known)
    assert not unknown, f"migration would write names that do not resolve: {unknown}"


def test_migration_survives_escaped_emoji_in_a_config(migration, tmp_path) -> None:
    """A config written with ``ensure_ascii=True`` must still migrate.

    ``_load_json5`` does not recombine ``\\ud83e\\udd81`` into one astral
    character the way :func:`json.loads` does — it returns two lone surrogates.
    Writing those back out with ``ensure_ascii=False`` raises "surrogates not
    allowed", which killed this migration on the real AgentFather config the
    first time it ran. Every migration that writes a config shares this hazard.
    """
    p = _write(
        tmp_path,
        "a",
        {"name": "a", "tools": ["lionclaw_doctor"], "system_prompt": "AgentFather 🦁 ready"},
    )
    # json.dumps defaults to ensure_ascii=True — the shape that breaks.
    assert "\\ud83e" in p.read_text(encoding="utf-8"), "fixture must contain escaped emoji"

    migration.apply(tmp_path)  # must not raise

    out = json.loads(p.read_text(encoding="utf-8"))
    assert out["tools"] == ["olano_doctor"]
    assert "🦁" in out["system_prompt"], "the emoji must survive intact, not be mangled"
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in out["system_prompt"])
