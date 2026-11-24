"""The file conversion tool says what it did — and nothing it did not.

Pins the branch a review caught after the sandbox hand-off was threaded
through this module: the JSON → CSV success line had slipped one indent out of
its guard, so a JSON file that is *not* a list of objects reported "Converted"
with no file written, and the error line below it became unreachable. A tool
that claims a conversion it never performed is worse than one that fails — the
agent quotes the path to the user.
"""

from __future__ import annotations

import json

from olano.tools.fileops_tools import file_convert


def test_json_list_of_objects_converts_to_csv(tmp_path):
    src = tmp_path / "rows.json"
    src.write_text(json.dumps([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]), encoding="utf-8")

    out = file_convert.func(source_path=str(src), target_format="csv")

    assert out.startswith("Converted JSON → CSV:"), out
    csv_path = tmp_path / "rows.csv"
    assert csv_path.exists()
    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "a,b"


def test_json_that_is_not_a_list_of_objects_is_refused_not_faked(tmp_path):
    src = tmp_path / "config.json"
    src.write_text(json.dumps({"a": 1}), encoding="utf-8")

    out = file_convert.func(source_path=str(src), target_format="csv")

    assert out.startswith("[tool-error]"), out
    assert "list of objects" in out
    # No half-result left behind for the agent to find and quote.
    assert not (tmp_path / "config.csv").exists()


def test_an_empty_json_list_is_refused_too(tmp_path):
    src = tmp_path / "empty.json"
    src.write_text("[]", encoding="utf-8")

    out = file_convert.func(source_path=str(src), target_format="csv")

    assert out.startswith("[tool-error]"), out
    assert not (tmp_path / "empty.csv").exists()
