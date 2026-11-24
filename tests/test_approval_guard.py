"""Unit tests for cognition/approval_guard — the shared classifier behind
``cognition.approval_required``."""

from __future__ import annotations

import json

from olano.cognition.approval_guard import (
    IDENTITY_FILES,
    KNOWN_CATEGORIES,
    action_categories,
    held_by,
    is_identity_path,
    item_categories,
    normalise_categories,
)
from olano.pulse.models import PulseItem, PulsePriority, PulseType


def _item(action=None, metadata=None) -> PulseItem:
    return PulseItem(
        id="x",
        source="self_learning",
        type=PulseType.PROPOSAL,
        priority=PulsePriority.NORMAL,
        content="c",
        action=action,
        metadata=metadata or {},
    )


class TestIdentityPath:
    def test_canonical_files_match_by_basename(self) -> None:
        for name in IDENTITY_FILES:
            assert is_identity_path(name)
            assert is_identity_path(f"./{name}")
            assert is_identity_path(f"/srv/olano/agents/merli/workspace/{name}")
            assert is_identity_path(f"C:\\ws\\{name}")

    def test_memory_and_other_files_are_not_identity(self) -> None:
        assert not is_identity_path("MEMORY.md")
        assert not is_identity_path("memories/2026-09-13.md")
        assert not is_identity_path("skills/learned/foo/SKILL.md")
        assert not is_identity_path("")
        assert not is_identity_path(None)

    def test_is_case_sensitive_like_the_scaffold(self) -> None:
        # The scaffold writes SOUL.md; a stray soul.md is a different file.
        assert not is_identity_path("soul.md")


class TestActionCategories:
    def test_external_send_maps_to_write_external(self) -> None:
        assert action_categories("gmail_send", {"to": "a@b.c"}) == {"external_send"}
        assert action_categories("a2a_send_message") == {"external_send"}

    def test_irreversible_maps_to_financial_and_secret_write(self) -> None:
        assert action_categories("wallet_send") == {"irreversible"}
        assert action_categories("secret_set", {"name": "X"}) == {"irreversible"}

    def test_identity_edit_is_path_based_on_fs_writes(self) -> None:
        assert action_categories("write_file", {"file_path": "SOUL.md"}) == {"identity_edit"}
        assert action_categories("edit_file", {"file_path": "/ws/AGENTS.md"}) == {"identity_edit"}
        assert action_categories("write_file", {"file_path": "MEMORY.md"}) == frozenset()
        assert action_categories("write_file", {"file_path": "notes/plan.md"}) == frozenset()

    def test_args_as_json_string(self) -> None:
        raw = json.dumps({"file_path": "IDENTITY.md", "content": "x"})
        assert action_categories("write_file", raw) == {"identity_edit"}

    def test_malformed_args_do_not_raise(self) -> None:
        assert action_categories("write_file", "{not json") == frozenset()
        assert action_categories("write_file", 42) == frozenset()
        assert action_categories("write_file", None) == frozenset()

    def test_ungated_tools_are_empty(self) -> None:
        assert action_categories("drive_list") == frozenset()
        assert action_categories("user_profile_save") == frozenset()
        assert action_categories("task") == frozenset()  # delegation, not a write


class TestItemCategories:
    def test_self_improvement_targeting_soul_is_identity(self) -> None:
        item = _item(
            action={"type": "append_lines", "path": "SOUL.md", "lines": ["be kinder"]},
            metadata={"kind": "self_improvement", "target_file": "SOUL.md"},
        )
        assert item_categories(item) == {"identity_edit"}

    def test_self_improvement_targeting_memory_is_not(self) -> None:
        item = _item(
            action={"type": "append_lines", "path": "MEMORY.md", "lines": ["fact"]},
            metadata={"kind": "self_improvement", "target_file": "MEMORY.md"},
        )
        assert item_categories(item) == frozenset()

    def test_metadata_hint_alone_is_enough(self) -> None:
        # A producer that sets the hint but no executable action still classifies.
        item = _item(action=None, metadata={"target_file": "AGENTS.md"})
        assert item_categories(item) == {"identity_edit"}

    def test_composite_recurses_into_steps(self) -> None:
        item = _item(
            action={
                "type": "composite",
                "steps": [
                    {"type": "append_text", "path": "MEMORY.md", "text": "a"},
                    {"type": "append_lines", "path": "./IDENTITY.md", "lines": ["b"]},
                ],
            }
        )
        assert item_categories(item) == {"identity_edit"}

    def test_learned_skill_and_run_worker_are_not_classified_here(self) -> None:
        skill = _item(action=None, metadata={"kind": "learned_skill"})
        worker = _item(action={"type": "run_worker", "goal": "rewrite SOUL.md"})
        assert item_categories(skill) == frozenset()
        # run_worker is decided per tool call by the runner, not by the sweep.
        assert item_categories(worker) == frozenset()


class TestNormaliseAndHold:
    def test_normalise_trims_lowercases_dedupes(self) -> None:
        assert normalise_categories([" Identity_Edit ", "identity_edit", ""]) == {"identity_edit"}

    def test_normalise_rejects_non_list_shapes(self) -> None:
        assert normalise_categories(None) == frozenset()
        assert normalise_categories("identity_edit") == frozenset()

    def test_held_by_is_intersection(self) -> None:
        required = frozenset(KNOWN_CATEGORIES)
        assert held_by(frozenset({"identity_edit"}), required) == {"identity_edit"}
        assert held_by(frozenset({"identity_edit"}), frozenset()) == frozenset()
        assert held_by(frozenset(), required) == frozenset()
