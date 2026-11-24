"""Schema-drift tolerance across Cortex stages.

Generation/curation models routinely paraphrase the requested JSON schema (rename
``title``→``idea``, ``name``→``skill_name``, ``soul_additions``→``soul``, wrap a
bare array in ``{"ideas": [...]}``, fence it in ```json). Before, any such drift
made a phase silently produce nothing ("No new ideas." / "Curated 0"). These
tests lock in that each stage now coerces the drift into usable items.
"""

from __future__ import annotations

from olano.cognition.ideation import _as_item_list, _coerce_item, _parse_json
from olano.memory.self_learning import SelfLearningService

# ── ideas / brainstorm ────────────────────────────────────────────────────────


def test_coerce_item_handles_renamed_idea_keys() -> None:
    # The EXACT shape gpt-5.4-mini returned for aurelius (dropped all 4 before).
    obj = {
        "idea": "Create a one-screen minimum-intake generator with 4 fields.",
        "why_now": "The next bottleneck is a usable flow.",
        "next_action": "Draft the prompt and test against 3 real ideas.",
    }
    item = _coerce_item(obj)
    assert item is not None
    assert item["title"] and len(item["title"]) <= 81
    assert "minimum-intake generator" in item["summary"]
    assert "Why now" in item["detail"] and "Next step" in item["detail"]


def test_coerce_item_prefers_canonical_keys_when_present() -> None:
    item = _coerce_item({"title": "T", "summary": "S", "detail": "D", "tags": ["a"]})
    assert item == {"title": "T", "summary": "S", "detail": "D", "tags": ["a"]}


def test_coerce_item_empty_returns_none() -> None:
    assert _coerce_item({}) is None
    assert _coerce_item("nope") is None


def test_as_item_list_unwraps_and_wraps() -> None:
    assert len(_as_item_list([{"title": "a"}, {"title": "b"}])) == 2
    assert len(_as_item_list({"ideas": [{"title": "a"}]})) == 1  # object wrapper
    assert len(_as_item_list({"items": [{"name": "a"}]})) == 1
    assert len(_as_item_list({"title": "solo", "summary": "x"})) == 1  # single object
    assert _as_item_list({"unrelated": 1}) == []


def test_parse_json_handles_fences_and_arrays() -> None:
    assert _parse_json('```json\n[{"title":"A"}]\n```') == [{"title": "A"}]
    assert _parse_json('Here you go:\n[{"title":"A"}]') == [{"title": "A"}]
    assert _parse_json('{"title":"A"}') == {"title": "A"}


# ── learned skills ────────────────────────────────────────────────────────────


def test_normalize_skill_candidate_handles_renamed_keys() -> None:
    s = SelfLearningService._normalize_skill_candidate(
        {
            "skill_name": "Download and summarize a PDF",
            "what": "Fetch a PDF and produce a summary.",
            "when": "user shares a PDF link",
            "instructions": ["download", "extract", "summarize"],
        }
    )
    assert s is not None
    assert s["name"] == "Download and summarize a PDF"
    assert s["description"].startswith("Fetch a PDF")
    assert s["trigger"] == "user shares a PDF link"
    assert s["steps"] == ["download", "extract", "summarize"]
    assert s["action"] == "create"


def test_normalize_skill_candidate_distinct_names_no_collapse() -> None:
    # Two differently-named skills must stay distinct (not both → "unnamed-skill").
    a = SelfLearningService._normalize_skill_candidate({"title": "Alpha skill"})
    b = SelfLearningService._normalize_skill_candidate({"title": "Beta skill"})
    assert a and b and a["name"] != b["name"]


def test_normalize_skill_candidate_empty_returns_none() -> None:
    assert SelfLearningService._normalize_skill_candidate({}) is None
    assert SelfLearningService._normalize_skill_candidate("x") is None


# ── self-improve proposals ────────────────────────────────────────────────────


def test_normalize_proposals_handles_renamed_buckets() -> None:
    p = SelfLearningService._normalize_proposals(
        {
            "soul": ["Be more concise."],
            "agents": "Prefer tables for comparisons.",  # string → wrapped in list
            "overview": "Two small tone tweaks.",
        }
    )
    assert p["soul_additions"] == ["Be more concise."]
    assert p["agents_additions"] == ["Prefer tables for comparisons."]
    assert p["summary"] == "Two small tone tweaks."
    assert p["memory_additions"] == []


def test_normalize_proposals_empty_stays_empty() -> None:
    assert SelfLearningService._normalize_proposals({}) == {}
