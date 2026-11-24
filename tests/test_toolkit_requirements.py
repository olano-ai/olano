"""One credential map: `tools/requirements.BUNDLE_REQUIREMENTS` is what a
toolkit reads.

Every `get_credential("NAME")` / `require_credentials([...])` literal in a
toolkit module must be declared in that toolkit's requirements entry
(optional allowed — aliases and per-mode keys are guidance, not a badge), so
the integration finder's "already connected" probe, the dashboard's
credential badge and the ToolkitAgent card all derive from the code's own
truth. `subagents/_defaults.default_credential_env_vars` is pinned to the
same map — the hand-maintained copy it replaced drifted (`NOTION_TOKEN` vs
the `NOTION_API_KEY` the toolkit reads)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from olano.subagents import _defaults
from olano.tools._name_index import NAME_INDEX
from olano.tools.registry import _TOOLKITS
from olano.tools.requirements import BUNDLE_REQUIREMENTS, TOOL_OVERRIDES

ROOT = Path(__file__).resolve().parents[1]

_SINGLE = re.compile(r"\bget_credential\(\s*\"([A-Z][A-Z0-9_]+)\"")
_MANY = re.compile(r"\b(?:get_credentials|require_credentials)\(\s*\[?([^)\]]*)")
_NAME = re.compile(r"\"([A-Z][A-Z0-9_]+)\"")


def _module_path(module: str) -> Path:
    return ROOT / (module.replace(".", "/") + ".py")


def credential_reads(module: str) -> set[str]:
    path = _module_path(module)
    if not path.exists():
        return set()
    src = path.read_text(encoding="utf-8")
    names = set(_SINGLE.findall(src))
    for group in _MANY.findall(src):
        names.update(_NAME.findall(group))
    return names


def declared_names(toolkit: str, module: str) -> set[str]:
    names = {str(s["name"]) for s in (BUNDLE_REQUIREMENTS.get(toolkit) or {}).get("env_vars") or []}
    for tool_name, owner in NAME_INDEX.items():
        if owner == module:
            names.update(
                str(s["name"]) for s in (TOOL_OVERRIDES.get(tool_name) or {}).get("env_vars") or []
            )
    return names


@pytest.mark.parametrize("toolkit", sorted(_TOOLKITS), ids=sorted(_TOOLKITS))
def test_every_credential_a_toolkit_reads_is_declared(toolkit: str) -> None:
    module = _TOOLKITS[toolkit]
    reads = credential_reads(module)
    if not reads:
        return
    assert toolkit in BUNDLE_REQUIREMENTS, (
        f"toolkit {toolkit!r} reads {sorted(reads)} but has no BUNDLE_REQUIREMENTS entry"
    )
    missing = reads - declared_names(toolkit, module)
    assert not missing, f"toolkit {toolkit!r} reads {sorted(missing)} undeclared in requirements"


@pytest.mark.parametrize("toolkit", sorted(_TOOLKITS), ids=sorted(_TOOLKITS))
def test_catalog_credentials_derive_from_requirements(toolkit: str) -> None:
    specs = (BUNDLE_REQUIREMENTS.get(toolkit) or {}).get("env_vars") or []
    expected = [str(s["name"]) for s in specs if not s.get("optional")]
    assert _defaults.default_credential_env_vars(toolkit) == expected


def test_the_hand_maintained_copy_is_gone() -> None:
    assert not hasattr(_defaults, "_CREDENTIAL_ENV_VARS")


@pytest.mark.parametrize(
    ("toolkit", "names"),
    [
        ("notion", ["NOTION_API_KEY"]),
        ("hubspot", ["HUBSPOT_ACCESS_TOKEN"]),
        ("jira", ["JIRA_API_TOKEN", "JIRA_EMAIL", "JIRA_SERVER"]),
        ("shopify", ["SHOPIFY_ACCESS_TOKEN", "SHOPIFY_STORE"]),
        ("microsoft365", ["MS365_ACCESS_TOKEN"]),
        ("google", []),
    ],
)
def test_reconciled_names_match_the_toolkit_code(toolkit: str, names: list[str]) -> None:
    """The entries that had drifted: the badge now names what the code reads."""
    assert _defaults.default_credential_env_vars(toolkit) == names
