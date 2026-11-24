"""Content-aware previews for offloaded tool results (``core/tool_result_preview``).

The preview is what the model reads instead of an oversized MCP result, so two
things are pinned here besides "it picks the right rows": every fact a summary
states must be true of every unit it stands for, and the preview must fit its
budget whatever it is handed.
"""

from __future__ import annotations

import json
import re

import pytest

from olano.core import tool_result_preview as trp
from olano.core.tool_result_preview import (
    ELIDED_KEY,
    PREVIEW_BUDGET_CHARS,
    SUMMARY_KEY,
    build_preview,
    head_tail_preview,
    summarize_run,
)

# ── fixtures ─────────────────────────────────────────────────────────────────


def _people(n: int = 40) -> list[dict]:
    people = [
        {
            "id": f"p-{i:04d}",
            "name": f"Person{i}",
            "team": "sales",
            "stage": ["lead", "lead", "qualified", "won"][i % 4],
            "deal_value": 100 + i,
            "api_token": f"tok-{i}",
        }
        for i in range(n)
    ]
    people[17]["stage"] = "error"
    people[17]["note"] = "sync failed: upstream rejected"
    people[29]["deal_value"] = 99999  # an outlier
    return people


def _body(preview: str) -> object:
    """The JSON value under the preview's header line."""
    header, _, body = preview.partition("\n")
    assert header.startswith("Preview (JSON")
    return json.loads(body)


def _orders_csv(n: int = 300) -> str:
    rows = ["order_id,customer,status,amount,currency"]
    for i in range(n):
        status = ["fulfilled", "fulfilled", "fulfilled", "shipped"][i % 4]
        rows.append(f"ord-{1000 + i},cust-{i % 9:02d},{status},{10 + i % 50}.50,SGD")
    rows[150] = "ord-1149,cust-01,FAILED card declined,0.00,SGD"
    rows[220] = "ord-1219,cust-03,fulfilled,75000.00,SGD"
    return "\n".join(rows)


def _app_log(n: int = 500) -> str:
    lines = [
        f"2026-10-14T03:00:{i % 60:02d}Z level=info msg=served path=/api/items status=200 ms={20 + i % 30}"
        for i in range(n)
    ]
    lines[250] = "2026-10-14T03:04:10Z level=error msg=timeout path=/api/sync status=504 ms=30000"
    lines.insert(251, "Traceback (most recent call last):")
    lines.insert(252, '  File "/app/sync.py", line 88, in pull')
    return "\n".join(lines)


# ── JSON ─────────────────────────────────────────────────────────────────────


class TestJsonPreview:
    def test_shown_plus_elided_is_the_whole_array(self):
        people = _people()
        out = _body(build_preview(json.dumps({"people": people, "page": 1})))
        assert isinstance(out, dict)
        shown = [e for e in out["people"] if ELIDED_KEY not in e]
        elided = sum(e[ELIDED_KEY] for e in out["people"] if ELIDED_KEY in e)
        assert len(shown) + elided == len(people)
        assert out["page"] == 1  # keys outside the array survive

    def test_error_elements_and_outliers_are_kept_in_order(self):
        out = _body(build_preview(json.dumps(_people())))
        assert isinstance(out, list)
        ids = [e["id"] for e in out if ELIDED_KEY not in e]
        assert "p-0017" in ids  # the element in an error state
        assert "p-0029" in ids  # the numeric outlier
        assert ids == sorted(ids)  # original order
        assert ids[0] == "p-0000" and ids[-1] == "p-0039"  # the ends

    def test_every_summary_fact_holds_for_every_elided_element(self):
        people = _people()
        out = _body(build_preview(json.dumps(people)))
        assert isinstance(out, list)
        position = 0
        checked = 0
        for element in out:
            if ELIDED_KEY not in element:
                assert element["id"] == people[position]["id"]
                position += 1
                continue
            run = people[position : position + element[ELIDED_KEY]]
            position += element[ELIDED_KEY]
            summary = element.get(SUMMARY_KEY, "")
            if "all team=sales" in summary:
                assert all(p["team"] == "sales" for p in run)
            for match in re.finditer(r"stage:((?: \w+×\d+)+)", summary):
                counts = dict(pair.split("×") for pair in match.group(1).split())
                for stage, count in counts.items():
                    assert sum(1 for p in run if p["stage"] == stage) == int(count)
                assert sum(int(c) for c in counts.values()) == len(run)  # complete
            if m := re.search(r"deal_value=(\d+)\.\.(\d+)", summary):
                values = [p["deal_value"] for p in run]
                assert (min(values), max(values)) == (int(m.group(1)), int(m.group(2)))
            checked += 1
        assert position == len(people)
        assert checked >= 1

    def test_credential_named_fields_are_never_summarized(self):
        preview = build_preview(json.dumps(_people()))
        summaries = [e.get(SUMMARY_KEY, "") for e in _body(preview) if ELIDED_KEY in e]  # type: ignore[union-attr]
        assert summaries and not any("api_token" in s or "tok-" in s for s in summaries)

    def test_numbers_are_never_reformatted(self):
        rows = [{"price": "x", "amount": 1.50, "big": 12345678901234567890} for _ in range(3)]
        text = (
            '{"rows": ['
            + ",".join('{"amount": 1.50, "ratio": 1e3, "big": 12345678901234567890}' for _ in rows)
            + "]}"
        )
        preview = build_preview(text)
        assert "1.50" in preview and "1e3" in preview and "12345678901234567890" in preview

    def test_error_subtrees_are_kept_whole(self):
        payload = {
            "errors": [{"code": i, "message": f"bad {i}"} for i in range(30)],
            "data": list(range(50)),
        }
        out = _body(build_preview(json.dumps(payload)))
        assert isinstance(out, dict)
        assert len(out["errors"]) == 30  # never collapsed
        assert any(isinstance(e, dict) and ELIDED_KEY in e for e in out["data"])

    def test_header_names_the_shortened_arrays_once_per_path(self):
        payload = {"people": [{"id": i, "tags": [f"t{j}" for j in range(20)]} for i in range(30)]}
        header = build_preview(json.dumps(payload)).splitlines()[0]
        assert "$.people[*].tags in" in header
        assert "$.people " in header

    def test_a_short_array_passes_through_whole(self):
        payload = {"items": [{"id": i} for i in range(5)], "note": "x" * 50}
        out = _body(build_preview(json.dumps(payload)))
        assert out == payload


# ── tables ───────────────────────────────────────────────────────────────────


class TestTablePreview:
    def test_csv_keeps_header_ends_errors_and_extremes_with_true_line_numbers(self):
        text = _orders_csv()
        lines = text.splitlines()
        preview = build_preview(text)
        assert preview.startswith("Preview (CSV table, 300 rows × 5 columns")
        shown = {}
        for row in preview.splitlines()[1:]:
            if m := re.match(r"\s*(\d+)\t(.*)$", row):
                shown[int(m.group(1))] = m.group(2)
        for number, content in shown.items():
            assert lines[number - 1] == content  # numbers are the file's own lines
        assert 1 in shown  # header
        assert any("FAILED" in row for row in shown.values())
        assert any("75000.00" in row for row in shown.values())  # column maximum
        assert any("0.00," in row for row in shown.values())  # column minimum
        assert len(lines) in shown  # last row

    def test_row_markers_count_the_gap_exactly(self):
        preview = build_preview(_orders_csv())
        numbers: list[int] = []
        last = 0
        for row in preview.splitlines()[1:]:
            if m := re.match(r"\s*(\d+)\t", row):
                last = int(m.group(1))
                numbers.append(last)
            elif m := re.match(r"\.\.\. \[(\d+) rows left out", row):
                numbers.append(-int(m.group(1)))
        # Walk: every marker covers exactly the lines between its neighbours.
        expected = 1
        for value in numbers:
            if value > 0:
                assert value == expected
                expected += 1
            else:
                expected += -value
        assert expected == 302  # 301 lines, all accounted for

    def test_markdown_and_tsv_tables_are_detected(self):
        md = "| id | status |\n|---|---|\n" + "\n".join(f"| {i} | ok |" for i in range(40))
        assert build_preview(md).startswith("Preview (Markdown table, 40 rows")
        tsv = "id\tstatus\n" + "\n".join(f"{i}\tok" for i in range(40))
        assert build_preview(tsv).startswith("Preview (TSV table, 40 rows")

    def test_json_lines_are_not_a_table(self):
        text = "\n".join(json.dumps({"event": "x", "id": i, "status": "ok"}) for i in range(100))
        preview = build_preview(text)
        assert preview.startswith("Preview (JSON Lines, 100 lines")
        assert "all event=x status=ok" in preview


# ── lines ────────────────────────────────────────────────────────────────────


class TestLinesPreview:
    def test_error_and_stack_lines_survive_with_their_line_numbers(self):
        text = _app_log()
        lines = text.splitlines()
        preview = build_preview(text)
        assert f"   251\t{lines[250]}" in preview  # the error line
        assert "   252\tTraceback (most recent call last):" in preview
        assert '   253\t  File "/app/sync.py", line 88, in pull' in preview
        assert "level=error" in lines[250]

    def test_runs_are_summarized_with_logfmt_facts(self):
        preview = build_preview(_app_log())
        assert "all level=info msg=served path=/api/items status=200" in preview
        assert re.search(r"range ms=\d+\.\.\d+", preview)

    def test_too_many_errors_are_announced_not_silently_dropped(self):
        text = "\n".join(f"ERROR job {i} failed with code {i * 7}" for i in range(400))
        preview = build_preview(text)
        assert len(preview) <= PREVIEW_BUDGET_CHARS
        assert "did not fit; search the file for them" in preview

    def test_prose_still_gets_a_preview(self):
        text = "\n".join(
            f"Paragraph {i} talks about {w}." for i, w in enumerate(["cats", "dogs"] * 200)
        )
        preview = build_preview(text)
        assert preview.startswith("Preview (text, 400 lines")
        assert "     1\tParagraph 0 talks about cats." in preview


# ── summaries ────────────────────────────────────────────────────────────────


class TestSummarizeRun:
    def test_enumeration_is_complete_or_absent(self):
        # Six distinct states: listing five would read as the complete set.
        # It falls back to a distinct count with bounds, which claims no counts.
        units = [[("state", s)] for s in ["a", "b", "c", "d", "e", "f"] * 3]
        assert summarize_run(units, 10_000) == "state: 6 distinct, a..f"
        units = [[("state", s)] for s in ["a", "b", "c"] * 3]
        assert summarize_run(units, 10_000) == "state: a×3 b×3 c×3"

    def test_absent_bucket_keeps_counts_summing_to_the_run(self):
        units = [[("state", "ok")], [("state", "ok")], [("other", "1")], [("state", "bad")]]
        summary = summarize_run(units, 10_000)
        assert "state: ok×2 bad×1 absent×1" in summary

    def test_non_finite_values_never_widen_a_range(self):
        units = [[("v", "1")], [("v", "NaN")], [("v", "5")]]
        assert "range" not in summarize_run(units, 10_000)

    def test_dense_ids_say_all_present_and_gappy_ones_do_not(self):
        dense = [[("id", f"wh-{i:03d}")] for i in range(100, 140)]
        assert summarize_run(dense, 10_000) == "id: wh-100..wh-139 all 40 present"
        gappy = [[("id", f"wh-{i:03d}")] for i in range(100, 180, 2)]
        assert summarize_run(gappy, 10_000) == "id: 40 distinct, wh-100..wh-178"

    def test_shed_entries_are_announced(self):
        units = [
            [(f"field{j}", "constant-value-here") for j in range(10)] + [("k", str(i))]
            for i in range(10)
        ]
        summary = summarize_run(units, 400)
        assert summary.endswith("omitted")
        assert len(summary) <= 160

    def test_unreadable_unit_disables_the_summary(self):
        assert summarize_run([[("a", "1")], [], [("a", "1")]], 10_000) == ""


# ── budget and safety ────────────────────────────────────────────────────────


class TestBudgetAndFallbacks:
    @pytest.mark.parametrize(
        "text",
        [
            json.dumps({f"key{i}": "v" * 200 for i in range(500)}),  # huge flat object
            json.dumps({"blob": "z" * 50_000}),  # one giant string
            json.dumps([[list(range(30)) for _ in range(30)] for _ in range(30)]),  # nesting
            json.dumps([{"text": f"unique words number {i} " * 30} for i in range(300)]),
            "\n".join(f"distinct line {i} " + "word " * 120 for i in range(600)),
            _orders_csv(2000),
        ],
        ids=["flat-object", "giant-string", "nested", "distinct-elements", "long-lines", "csv"],
    )
    def test_preview_fits_the_budget(self, text):
        assert len(build_preview(text)) <= PREVIEW_BUDGET_CHARS

    def test_untrusted_marker_is_not_sampled_as_content(self):
        preview = build_preview("[trusted=false]\n" + json.dumps(_people()))
        assert preview.startswith("Preview (JSON array of 40 items")
        assert "trusted=false" not in preview

    def test_single_long_line_that_is_not_json_shows_both_ends(self):
        text = "HEAD" + "x" * 30_000 + "TAIL"
        preview = build_preview(text)
        assert "HEAD" in preview and "TAIL" in preview and "chars left out" in preview

    def test_oversized_payload_is_not_parsed(self, monkeypatch):
        monkeypatch.setattr(trp, "STRUCTURED_MAX_CHARS", 1000)
        text = "\n".join(f'{{"id": {i}}}' for i in range(500))
        assert build_preview(text) == head_tail_preview(text)

    def test_a_failure_falls_back_to_head_and_tail(self, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("bug")

        monkeypatch.setattr(trp, "_json_preview", boom)
        text = json.dumps(_people())
        assert build_preview(text) == head_tail_preview(text)
