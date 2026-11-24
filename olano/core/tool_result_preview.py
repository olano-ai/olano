"""Content-aware previews for tool results that were offloaded to a file.

When a tool result is too large to keep inline, the full text goes to a file
and the model gets a pointer plus a preview. The preview used to be the first
and last five lines, whatever the content was. That is a poor sample of the
shapes MCP servers actually return: a minified JSON payload is one line, so the
"preview" was its first 1000 characters; a CSV export showed its header and
four rows; a log hid its one ``ERROR`` line in the truncated middle. The model
then pages through the file with ``read_file``, and every one of those calls
re-sends the whole conversation.

This module builds a preview that matches the content:

* **JSON** (one complete value): the structure is kept, every key included;
  long arrays keep their first and last elements, every element in an error
  state, numeric outliers and one example of each distinct element shape, and
  each run of left-out elements becomes a marker object that counts them and
  states what holds for *all* of them.
* **Tables** (CSV, TSV, Markdown): the header, the first and last rows, every
  row naming an error, each numeric column's extremes and one example of each
  kind of row, with the same counted markers for the rows left out.
* **Lines** (logs, JSON Lines, command output, prose): the head, the tail,
  every error / warning / stack-frame line and one example of each kind of
  line, with original line numbers so ``read_file`` offsets still line up.

Anything else (a single long line that is not JSON, or text too large to parse
quickly) falls back to a head and tail cut.

The selection rules and the run summaries are a simplified port of the
compressors in Caveman's engine (``engine/compressors/json.go``, ``tabular.go``,
``log.go``, ``invariants.go`` and ``redundancy.go``), Copyright 2026 Julius
Brussee, used under the Apache License 2.0 (``CAVEMAN_LICENSE.txt`` beside this
file; see ``THIRD_PARTY_NOTICES.md``). Changed from the original: rewritten in
Python; no query relevance (BM25), change-point or element-length heuristics;
every preview is fitted to a hard character budget by stepping down through
stricter selection levels; and the markers point at the offloaded file instead
of a recovery tool, because here the original is always on disk.

Two rules carry over unchanged because they are what make a preview safe to
read. **A summary states only facts checked across every left-out unit**: a
constant (``all status=active``), a complete enumeration with exact counts
(``stage: lead×12 won×3``), a numeric range taken from the original value
strings (``range amount=10..950``) or a distinct count with its bounds
(``id: 40 distinct, c-001..c-077``). A partial list would read as complete, so
an enumeration that does not fit is withheld, never shortened, and entries shed
for space are announced (``+2 omitted``). **Fields named like credentials are
never summarized**, because a marker is quoted far more freely than the data.

``build_preview`` never raises: any failure falls back to the plain head and
tail preview, so a malformed payload can only cost the improvement, never the
offload.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Upper bound on the preview's characters (about 1k tokens at 4 chars/token).
PREVIEW_BUDGET_CHARS = 4000
#: Above this size the payload is not parsed; a head/tail cut of a
#: multi-megabyte result costs nothing.
STRUCTURED_MAX_CHARS = 2_000_000
#: A left-out run longer than this gets a count and no summary: the summary
#: reads every unit, and on a run this long that is the whole cost of a preview.
SUMMARY_MAX_UNITS = 10_000
#: Shown lines are clipped to this many characters (deepagents uses 1000).
LINE_CHAR_LIMIT = 500

UNTRUSTED_MARKER = "[trusted=false]"

#: Keys of the marker object that replaces a run of left-out array elements.
ELIDED_KEY = "__elided__"
SUMMARY_KEY = "__all_elided__"

# ── what counts as signal ────────────────────────────────────────────────────

# Object keys whose subtrees carry what the model needs (errors, messages,
# stack traces): kept verbatim, never collapsed.
_ERROR_KEY_RE = re.compile(
    r"(?i)^(error|errors|message|msg|stack|stacktrace|stack_trace|trace|traceback|"
    r"exception|reason|detail|details|warning|warnings)$"
)
# An array element whose text names a failure state is always kept.
_ERROR_VALUE_RE = re.compile(
    r"(?i)\b(error|errors|exception|failed|failure|critical|fatal|crash|panic|abort|"
    r"timeout|denied|rejected)\b"
)
# A table row naming an error is always kept.
_TABLE_IMPORTANT_RE = re.compile(
    r"\b(ERROR|FAIL|FAILED|FATAL|PANIC|EXCEPTION|WARNING|SECURITY|DENIED|REJECTED)\b",
    re.IGNORECASE,
)
# A log line worth keeping: errors, failures, warnings, stack-trace frames.
_IMPORTANT_LINE_RE = re.compile(
    r"(?i)(\b(ERROR|FATAL|PANIC|EXCEPTION|TRACEBACK|FAIL|FAILED|FAILURE|WARN|WARNING|CRIT|"
    r"CRITICAL|ALERT|EMERG|EMERGENCY)\b|^\s+at\s|^\s+File \"|\.go:\d+|^\s+--->|caused by)"
)
_IMPORTANT_LITERALS = (
    "error",
    "fatal",
    "panic",
    "exception",
    "traceback",
    "fail",
    "warn",
    "crit",
    "alert",
    "emerg",
    ".go:",
    "caused by",
)
_MARKDOWN_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")


def _important_line(line: str) -> bool:
    if not line:
        return False
    if line[0] <= " " or not line.isascii():
        return bool(_IMPORTANT_LINE_RE.search(line))
    lower = line.lower()
    if any(literal in lower for literal in _IMPORTANT_LITERALS):
        return bool(_IMPORTANT_LINE_RE.search(line))
    return False


# ── run summaries (port of invariants.go) ────────────────────────────────────

_INV_MAX_BYTES = 160
_INV_MAX_SHARE_DEN = 4
_INV_MIN_BUDGET = 96
_INV_MIN_ELIDE_UNITS = 3
_INV_MIN_UNITS = 2
_INV_MAX_VALUE_BYTES = 40
_INV_MAX_NAME_BYTES = 32
_INV_MAX_FIELDS_PER_UNIT = 24
_INV_MAX_TRACKED_FIELDS = 48
_INV_MAX_ENUM_VALUES = 5
_INV_MAX_ENUM_VALUE_BYTES = 24
_INV_MAX_COVERAGE_VALUES = 4096
_INV_MAX_DENSE_DIGITS = 18
_INV_MAX_COVERAGE_ENTRIES = 2
_ABSENT = "absent"

_KIND_CONSTANT, _KIND_ENUM, _KIND_ENUM_WEAK, _KIND_COVERAGE_DENSE, _KIND_COVERAGE, _KIND_RANGE = (
    range(6)
)
# Shed order when a summary is over budget: identifier-ish enumerations first,
# class enumerations last (they answer "was anything in state X left out?").
_SHED_ORDER = (
    _KIND_ENUM_WEAK,
    _KIND_RANGE,
    _KIND_COVERAGE,
    _KIND_CONSTANT,
    _KIND_COVERAGE_DENSE,
    _KIND_ENUM,
)

_SECRET_FIELD_RE = re.compile(
    r"(?i)(secret|passw|token|api[_-]?key|authoriz|credential|cookie|session)"
)
_LOGFMT_PAIR_RE = re.compile(
    r"(?:^|[\s,\[{(])([A-Za-z_][A-Za-z0-9_.\-]*)=(\"[^\"]*\"|'[^']*'|[^\s,\]})]+)"
)

Field = tuple[str, str]


def _finite_number(value: str) -> float | None:
    """``value`` as a finite number, or None (``NaN``/``Inf`` are not numbers here)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _value_usable(value: str) -> bool:
    if not value or len(value.encode("utf-8")) > _INV_MAX_VALUE_BYTES:
        return False
    if ELIDED_KEY in value or "…" in value:
        return False
    return all(ch.isprintable() for ch in value)


def _enum_value_usable(value: str) -> bool:
    if not _value_usable(value) or len(value.encode("utf-8")) > _INV_MAX_ENUM_VALUE_BYTES:
        return False
    return " " not in value and "\t" not in value


class _Agg:
    __slots__ = (
        "constant",
        "counts",
        "cov_max",
        "cov_min",
        "cov_usable",
        "cov_values",
        "first",
        "max_str",
        "max_val",
        "min_str",
        "min_val",
        "numeric",
        "seen",
        "too_wide",
    )

    def __init__(self, value: str) -> None:
        number = _finite_number(value)
        self.seen = 0
        self.constant = True
        self.numeric = number is not None
        self.first = value
        self.min_str = self.max_str = value
        self.min_val = self.max_val = number if number is not None else 0.0
        self.counts: dict[str, int] = {}
        self.too_wide = False
        self.cov_values: set[str] = set()
        self.cov_min = self.cov_max = ""
        self.cov_usable = True

    def add(self, value: str) -> None:
        self.seen += 1
        if value != self.first:
            self.constant = False
        if self.numeric:
            number = _finite_number(value)
            if number is None:
                self.numeric = False
            else:
                if number < self.min_val:
                    self.min_val, self.min_str = number, value
                if number > self.max_val:
                    self.max_val, self.max_str = number, value
        if not self.too_wide:
            if value not in self.counts and len(self.counts) >= _INV_MAX_ENUM_VALUES:
                # A sixth distinct value: stop counting rather than keep a tally
                # that could only ever render as a partial list.
                self.too_wide = True
            else:
                self.counts[value] = self.counts.get(value, 0) + 1
        if self.cov_usable:
            if (
                not _enum_value_usable(value)
                or value == _ABSENT
                or len(self.cov_values) >= _INV_MAX_COVERAGE_VALUES
            ):
                self.cov_usable = False
            elif value not in self.cov_values:
                self.cov_values.add(value)
                if len(self.cov_values) == 1:
                    self.cov_min = self.cov_max = value
                else:
                    self.cov_min = min(self.cov_min, value)
                    self.cov_max = max(self.cov_max, value)


def _enumerate_field(name: str, agg: _Agg, absent: int) -> tuple[str, bool]:
    """``name: v×n …`` covering every unit, or ``""`` when that is not possible."""
    if agg.too_wide or not agg.counts:
        return "", False
    buckets: list[tuple[str, int]] = []
    for value, count in agg.counts.items():
        if value == _ABSENT or not _enum_value_usable(value):
            return "", False
        buckets.append((value, count))
    if len(buckets) + min(absent, 1) > _INV_MAX_ENUM_VALUES:
        return "", False
    buckets.sort(key=lambda bucket: -bucket[1])  # stable: ties keep first appearance
    if absent:
        buckets.append((_ABSENT, absent))
    units = sum(count for _, count in buckets)
    singletons = sum(1 for _, count in buckets if count == 1)
    if singletons == len(buckets):
        return "", False  # a different value in every unit: an identifier, not a class
    weak = 2 * len(buckets) >= units
    return name + ":" + "".join(f" {value}×{count}" for value, count in buckets), weak


def _split_trailing_digits(value: str) -> tuple[str, str]:
    end = len(value)
    while end > 0 and value[end - 1].isascii() and value[end - 1].isdigit():
        end -= 1
    return value[:end], value[end:]


def _dense_coverage(values: set[str]) -> tuple[str, str] | None:
    """Endpoints when the values are one prefix plus a gapless fixed-width integer run."""
    if len(values) < 2:
        return None
    prefix: str | None = None
    width = 0
    low = high = 0
    low_val = high_val = ""
    for value in values:
        body, digits = _split_trailing_digits(value)
        if not digits or len(digits) > _INV_MAX_DENSE_DIGITS:
            return None
        if prefix is None:
            prefix, width = body, len(digits)
            low = high = int(digits)
            low_val = high_val = value
            continue
        if body != prefix or len(digits) != width:
            return None
        number = int(digits)
        if number < low:
            low, low_val = number, value
        elif number > high:
            high, high_val = number, value
    if high - low + 1 != len(values):
        return None
    return low_val, high_val


def _coverage_entry(name: str, agg: _Agg, absent: int) -> str:
    distinct = len(agg.cov_values)
    if distinct < 2 or agg.cov_min == agg.cov_max:
        return ""
    if not absent:
        dense = _dense_coverage(agg.cov_values)
        if dense is not None:
            return f"{name}: {dense[0]}..{dense[1]} all {distinct} present"
    text = f"{name}: {distinct} distinct, {agg.cov_min}..{agg.cov_max}"
    if absent:
        text += f", {_ABSENT}×{absent}"
    return text


def _summary_budget(elided_bytes: int) -> int:
    if elided_bytes <= 0:
        return _INV_MAX_BYTES
    budget = max(elided_bytes // _INV_MAX_SHARE_DEN, _INV_MIN_BUDGET)
    return min(budget, elided_bytes // 2, _INV_MAX_BYTES)


def _render_summary(entries: list[tuple[int, str]], selected: list[bool], omitted: int) -> str:
    sections: list[str] = []
    constants = [
        t for (k, t), s in zip(entries, selected, strict=True) if s and k == _KIND_CONSTANT
    ]
    if constants:
        sections.append("all " + " ".join(constants))
    for (kind, text), keep in zip(entries, selected, strict=True):
        if keep and kind in (_KIND_ENUM, _KIND_ENUM_WEAK, _KIND_COVERAGE, _KIND_COVERAGE_DENSE):
            sections.append(text)
    ranges = [t for (k, t), s in zip(entries, selected, strict=True) if s and k == _KIND_RANGE]
    if ranges:
        sections.append("range " + " ".join(ranges))
    if not sections:
        return ""
    if omitted:
        sections.append(f"+{omitted} omitted")
    return "; ".join(sections)


def summarize_run(units: list[list[Field]], elided_bytes: int) -> str:
    """Facts that hold across every unit of one left-out run, or ``""``."""
    if len(units) < _INV_MIN_UNITS or len(units) > SUMMARY_MAX_UNITS:
        return ""
    aggs: dict[str, _Agg] = {}
    for fields in units:
        if not fields:
            return ""  # a unit we could not read is a unit we cannot make claims about
        seen_here: set[str] = set()
        for name, value in fields:
            if name in seen_here:
                continue
            seen_here.add(name)
            agg = aggs.get(name)
            if agg is None:
                if len(aggs) >= _INV_MAX_TRACKED_FIELDS:
                    continue
                agg = aggs[name] = _Agg(value)
            agg.add(value)

    total = len(units)
    entries: list[tuple[int, str]] = []
    coverages = 0
    for name, agg in aggs.items():
        if not name or len(name) > _INV_MAX_NAME_BYTES or _SECRET_FIELD_RE.search(name):
            continue
        if agg.seen == total and agg.constant:
            if _value_usable(agg.first):
                entries.append((_KIND_CONSTANT, f"{name}={agg.first}"))
            continue
        if agg.seen == total and agg.numeric and agg.min_str != agg.max_str:
            if _value_usable(agg.min_str) and _value_usable(agg.max_str):
                entries.append((_KIND_RANGE, f"{name}={agg.min_str}..{agg.max_str}"))
            continue
        text, weak = _enumerate_field(name, agg, total - agg.seen)
        if text:
            entries.append((_KIND_ENUM_WEAK if weak else _KIND_ENUM, text))
            continue
        if agg.cov_usable and coverages < _INV_MAX_COVERAGE_ENTRIES:
            coverage = _coverage_entry(name, agg, total - agg.seen)
            if coverage:
                dense = coverage.endswith(" present")
                entries.append((_KIND_COVERAGE_DENSE if dense else _KIND_COVERAGE, coverage))
                coverages += 1
    if not entries:
        return ""

    budget = _summary_budget(elided_bytes)
    selected = [True] * len(entries)
    omitted = 0
    while True:
        rendered = _render_summary(entries, selected, omitted)
        if len(rendered) <= budget:
            return rendered
        victim = -1
        for kind in _SHED_ORDER:
            for i in range(len(entries) - 1, -1, -1):
                if selected[i] and entries[i][0] == kind:
                    victim = i
                    break
            if victim >= 0:
                break
        if victim < 0:
            return ""
        selected[victim] = False
        omitted += 1


def _worth_eliding(unit_count: int, marker_chars: int, elided_chars: int, summary: str) -> bool:
    """A run of three or more always elides; a shorter one must carry a summary and halve."""
    if unit_count >= _INV_MIN_ELIDE_UNITS:
        return True
    return bool(summary) and 2 * marker_chars <= elided_chars


def _object_fields(value: Any) -> list[Field]:
    """Scalar top-level members of a JSON object, in document order."""
    if not isinstance(value, dict):
        return []
    fields: list[Field] = []
    for name, member in value.items():
        if len(fields) >= _INV_MAX_FIELDS_PER_UNIT:
            break
        if isinstance(member, bool):
            text = "true" if member else "false"
        elif isinstance(member, _RawNumber):
            text = member.text
        elif isinstance(member, str):
            text = member
        else:
            continue  # null, nested object, nested array
        if text:
            fields.append((str(name), text))
    return fields


def _logfmt_fields(line: str) -> list[Field]:
    if "=" not in line:
        return []
    fields: list[Field] = []
    for match in _LOGFMT_PAIR_RE.finditer(line):
        if len(fields) >= _INV_MAX_FIELDS_PER_UNIT:
            break
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            fields.append((match.group(1), value))
    return fields


def _line_fields(line: str) -> list[Field]:
    """One line read as a JSON object (JSON Lines) or as ``key=value`` pairs."""
    stripped = line.strip()
    if len(stripped) > 1 and stripped[0] == "{" and stripped[-1] == "}":
        try:
            fields = _object_fields(_loads(stripped))
        except ValueError:
            fields = []
        if fields:
            return fields
    return _logfmt_fields(line)


def _row_fields(header: list[str], row: list[str]) -> list[Field]:
    fields: list[Field] = []
    for i, cell in enumerate(row[:_INV_MAX_FIELDS_PER_UNIT]):
        if i >= len(header):
            break
        name, value = header[i].strip(), cell.strip()
        if name and value:
            fields.append((name, value))
    return fields


# ── "one example of each kind" (port of redundancy.go) ───────────────────────

_REDUNDANT_CONTAINMENT = 0.9
_REDUNDANCY_MAX_CLASSES = 64
_REDUNDANCY_MAX_UNIT_TOKENS = 128
_TOKEN_RE = re.compile(r"[^\W\d]+|\d+", re.UNICODE)
_DIGITS_RE = re.compile(r"\d+")


def _vocabulary(unit: str) -> tuple[frozenset[str], bool]:
    """A unit's word set with digit runs masked to ``#`` (unmasked when it has no words)."""
    tokens = _TOKEN_RE.findall(unit.lower())[: _REDUNDANCY_MAX_UNIT_TOKENS * 4]
    masked: set[str] = set()
    words = 0
    for token in tokens:
        key = "#" if token.isdigit() else token
        if key not in masked:
            masked.add(key)
            if key != "#":
                words += 1
        if len(masked) >= _REDUNDANCY_MAX_UNIT_TOKENS:
            break
    if words:
        return frozenset(masked), True
    return frozenset(tokens[:_REDUNDANCY_MAX_UNIT_TOKENS]), False


def _represented(
    vocab: tuple[frozenset[str], bool], classes: list[tuple[frozenset[str], bool]]
) -> bool:
    words, masked = vocab
    if not words:
        return True
    need = math.ceil(_REDUNDANT_CONTAINMENT * len(words))
    for class_words, class_masked in classes:
        if class_masked == masked and len(words & class_words) >= need:
            return True
    return False


def _first_of_kind(
    docs: list[str],
    keep: list[bool],
    limit: int,
    cache: dict[int, tuple[frozenset[str], bool]] | None = None,
) -> list[int]:
    """Indices (document order, at most ``limit``) of units no kept unit resembles."""
    if limit <= 0:
        return []
    memo: dict[int, tuple[frozenset[str], bool]] = {} if cache is None else cache

    def vocabulary(i: int) -> tuple[frozenset[str], bool]:
        found = memo.get(i)
        if found is None:
            found = memo[i] = _vocabulary(docs[i])
        return found

    kept = [i for i, k in enumerate(keep) if k]
    step = max(1, len(kept) // _REDUNDANCY_MAX_CLASSES)
    classes = [vocabulary(i) for i in kept[::step][:_REDUNDANCY_MAX_CLASSES]]
    picked: list[int] = []
    # Units that differ only in their digits (a log's timestamps, a table's
    # ids) share a vocabulary, and once a shape is represented it stays so
    # (classes only grow), so each shape is judged once.
    represented_shapes: set[str] = set()
    for i in range(len(docs)):
        if keep[i]:
            continue
        # Only a whole short unit is a shape: a vocabulary of a long one is
        # built from a token window this key would not cover.
        shape = _DIGITS_RE.sub("#", docs[i]) if len(docs[i]) <= 2000 else None
        if shape is not None and shape in represented_shapes:
            continue
        vocab = vocabulary(i)
        if _represented(vocab, classes):
            # Only a digit-masked vocabulary is the same for every unit of a shape.
            if shape is not None and vocab[1]:
                represented_shapes.add(shape)
            continue
        picked.append(i)
        classes.append(vocab)
        if len(picked) >= limit:
            break
    return picked


# ── numeric outliers ─────────────────────────────────────────────────────────


def _median(sorted_values: list[float]) -> float:
    n = len(sorted_values)
    if n == 0:
        return 0.0
    mid = n // 2
    return sorted_values[mid] if n % 2 else (sorted_values[mid - 1] + sorted_values[mid]) / 2


def _median_and_scale(values: list[float]) -> tuple[float, float]:
    ordered = sorted(values)
    med = _median(ordered)
    mad = _median(sorted(abs(v - med) for v in ordered))
    if mad > 0:
        return med, mad
    if len(ordered) < 2:
        return med, 0.0
    mean = sum(ordered) / len(ordered)
    std = math.sqrt(sum((v - mean) ** 2 for v in ordered) / (len(ordered) - 1))
    return mean, std / 0.6745


def _numeric_leaves(value: Any, prefix: str, out: dict[str, float]) -> None:
    if isinstance(value, _RawNumber):
        number = _finite_number(value.text)
        if number is not None:
            out[prefix] = number
    elif isinstance(value, dict):
        for key, member in value.items():
            _numeric_leaves(member, f"{prefix}.{key}" if prefix else str(key), out)


def _outliers(series_by_path: dict[str, dict[int, float]], z: float = 2.0) -> list[int]:
    """Indices that sit at least ``z`` robust (MAD) scales from a path's median."""
    flagged: set[int] = set()
    for path in sorted(series_by_path):
        by_index = series_by_path[path]
        if len(by_index) < 4:
            continue
        med, scale = _median_and_scale(list(by_index.values()))
        if scale <= 0:
            continue
        for index, value in by_index.items():
            if abs(0.6745 * (value - med) / scale) >= z:
                flagged.add(index)
    return sorted(flagged)


# ── JSON ─────────────────────────────────────────────────────────────────────


class _RawNumber:
    """A JSON number kept as its original text, so a preview never reformats one."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


class _Verbatim(str):
    """A string the preview wrote itself (a run summary): never clipped."""

    __slots__ = ()


def _loads(text: str) -> Any:
    return json.loads(text, parse_int=_RawNumber, parse_float=_RawNumber, parse_constant=_RawNumber)


def _render_json(value: Any, *, str_limit: int | None = None, sort_keys: bool = False) -> str:
    """Compact JSON, numbers verbatim, long strings optionally clipped."""
    if isinstance(value, _RawNumber):
        return value.text
    if isinstance(value, str):
        if str_limit is not None and len(value) > str_limit and not isinstance(value, _Verbatim):
            value = f"{value[:str_limit]}…[+{len(value) - str_limit} chars]"
        return json.dumps(value, ensure_ascii=False)
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, dict):
        items = sorted(value.items()) if sort_keys else value.items()
        inner = ",".join(
            f"{json.dumps(str(k), ensure_ascii=False)}:"
            f"{_render_json(v, str_limit=str_limit, sort_keys=sort_keys)}"
            for k, v in items
        )
        return "{" + inner + "}"
    if isinstance(value, list):
        return (
            "["
            + ",".join(_render_json(v, str_limit=str_limit, sort_keys=sort_keys) for v in value)
            + "]"
        )
    return json.dumps(str(value), ensure_ascii=False)


@dataclass(frozen=True)
class _Level:
    """How hard a preview selects; previews step down until they fit the budget."""

    keep_first: int
    keep_last: int
    max_signal: int  # error-state + outlier units kept beyond the anchors
    max_kinds: int  # first-of-their-kind units kept beyond those
    str_limit: int  # JSON strings clipped to this many characters
    array_threshold: int  # arrays at most this long are kept whole
    max_keys: int  # JSON objects show at most this many keys
    preserve_errors: bool = True  # keep error/message subtrees whole


_LEVELS = (
    _Level(3, 2, 6, 4, 240, 8, 200),
    _Level(2, 1, 3, 2, 120, 6, 60),
    _Level(1, 1, 1, 1, 80, 4, 30),
    _Level(1, 0, 0, 0, 60, 1, 12, preserve_errors=False),
)


@dataclass
class _JsonStats:
    arrays: list[tuple[str, int, int]]  # (path, length, elements shown)
    signal_dropped: int = 0  # error/outlier elements left out because the budget bound


class _JsonShaper:
    def __init__(self, level: _Level, cache: dict[int, tuple[list[str], list[int]]]) -> None:
        self.level = level
        self.stats = _JsonStats(arrays=[])
        # id(array) -> (canonical element docs, error/outlier indices). The
        # levels re-shape the same parsed value, so this work is done once.
        self._cache = cache

    def shape(self, value: Any, path: str = "$", preserve: bool = False) -> Any:
        if isinstance(value, dict):
            items = list(value.items())
            out: dict[str, Any] = {}
            for key, member in items[: self.level.max_keys]:
                error_key = self.level.preserve_errors and bool(_ERROR_KEY_RE.match(str(key)))
                out[str(key)] = self.shape(member, f"{path}.{key}", preserve or error_key)
            if len(items) > self.level.max_keys:
                out[ELIDED_KEY] = f"{len(items) - self.level.max_keys} more keys"
            return out
        if isinstance(value, list):
            if preserve:
                return value
            if len(value) <= self.level.array_threshold:
                return [self.shape(e, f"{path}[{i}]") for i, e in enumerate(value)]
            return self._select(value, path)
        return value

    def _select(self, arr: list[Any], path: str) -> list[Any]:
        level = self.level
        n = len(arr)
        cached = self._cache.get(id(arr))
        if cached is None:
            docs = [_render_json(e, sort_keys=True) for e in arr]
            candidates = [i for i, doc in enumerate(docs) if _ERROR_VALUE_RE.search(doc)]
            series: dict[str, dict[int, float]] = {}
            for i, element in enumerate(arr):
                leaves: dict[str, float] = {}
                _numeric_leaves(element, "", leaves)
                for leaf_path, number in leaves.items():
                    series.setdefault(leaf_path, {})[i] = number
            flagged = set(candidates)
            candidates.extend(i for i in _outliers(series) if i not in flagged)
            cached = self._cache[id(arr)] = (docs, candidates)
        docs, candidates = cached
        keep = [False] * n
        for i in list(range(min(level.keep_first, n))) + list(
            range(max(0, n - level.keep_last), n)
        ):
            keep[i] = True
        signal = [i for i in candidates if not keep[i]]
        for i in signal[: level.max_signal]:
            keep[i] = True
        self.stats.signal_dropped += max(0, len(signal) - level.max_signal)
        for i in _first_of_kind(docs, keep, level.max_kinds):
            keep[i] = True

        out: list[Any] = []
        run: list[int] = []

        def flush() -> None:
            if not run:
                return
            elided_chars = sum(len(docs[i]) + 1 for i in run)
            summary = (
                summarize_run([_object_fields(arr[i]) for i in run], elided_chars)
                if len(run) <= SUMMARY_MAX_UNITS
                else ""
            )
            marker: dict[str, Any] = {ELIDED_KEY: _RawNumber(str(len(run)))}
            if summary:
                marker[SUMMARY_KEY] = _Verbatim(summary)
            if not _worth_eliding(len(run), len(_render_json(marker)), elided_chars, summary):
                out.extend(self.shape(arr[i], f"{path}[{i}]") for i in run)
            else:
                out.append(marker)
            run.clear()

        for i, element in enumerate(arr):
            if keep[i]:
                flush()
                out.append(self.shape(element, f"{path}[{i}]"))
            else:
                run.append(i)
        flush()
        shown = sum(1 for e in out if not (isinstance(e, dict) and ELIDED_KEY in e))
        self.stats.arrays.append((path, n, shown))
        return out


def _json_preview(value: Any, budget: int) -> str:
    kind = (
        f"object with {len(value)} keys"
        if isinstance(value, dict)
        else f"array of {len(value)} items"
    )
    cache: dict[int, tuple[list[str], list[int]]] = {}
    header = body = ""
    for level in _LEVELS:
        shaper = _JsonShaper(level, cache)
        body = _render_json(shaper.shape(value), str_limit=level.str_limit)
        header = _json_header(kind, shaper.stats)
        text = f"{header}\n{body}"
        if len(text) <= budget:
            return text
    # Even the strictest level is over budget (a huge flat object, a giant
    # string): clip the strictest rendering and say so.
    room = max(0, budget - len(header) - 60)
    return f"{header}\n{body[:room]}…[preview clipped; {len(body) - room} more chars of this view]"


_INDEX_RE = re.compile(r"\[\d+\]")


def _json_header(kind: str, stats: _JsonStats) -> str:
    # Group by path with indices wildcarded, so the keyword list inside each of
    # 25 profiles reads as one line, not 25.
    groups: dict[str, list[int]] = {}  # path -> [arrays, total length, total shown]
    for path, length, shown in stats.arrays:
        if shown < length:
            group = groups.setdefault(_INDEX_RE.sub("[*]", path), [0, 0, 0])
            group[0] += 1
            group[1] += length
            group[2] += shown
    if not groups:
        return f"Preview (JSON {kind}; long strings are clipped):"
    ranked = sorted(groups.items(), key=lambda item: -item[1][1])
    parts = []
    for path, (arrays, length, shown) in ranked[:4]:
        if arrays == 1:
            parts.append(f"{path} {shown} of {length} shown")
        else:
            parts.append(f"{path} in {arrays} places, {shown} of {length} elements shown")
    more = f"; {len(ranked) - 4} more shortened paths" if len(ranked) > 4 else ""
    note = (
        f"Preview (JSON {kind}; arrays shortened: {'; '.join(parts)}{more}; long strings are "
        f'clipped). An object {{"{ELIDED_KEY}": N, "{SUMMARY_KEY}": "…"}} stands in for N '
        f"consecutive elements left out; its summary states only what holds for every one of "
        f"them. Every element in an error state and every numeric outlier is shown"
    )
    if stats.signal_dropped:
        note += f", except {stats.signal_dropped} that did not fit (search the file for them)"
    return note + ":"


# ── tables ───────────────────────────────────────────────────────────────────


@dataclass
class _Table:
    kind: str  # "CSV" | "TSV" | "Markdown"
    rows: list[list[str]]
    header_rows: int
    line_of_row: list[int] | None  # 1-based line per row when rows map 1:1 to lines


def _parse_table(text: str) -> _Table | None:
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 8:
        return None
    markdown = _parse_markdown_table(lines)
    if markdown is not None:
        return markdown
    first = lines[0]
    if first.lstrip()[:1] in ("{", "["):
        return None  # JSON Lines: same comma count on every line, but not a table
    delimiter, kind = ",", "CSV"
    if first.count("\t") >= 1 and first.count("\t") > first.count(","):
        delimiter, kind = "\t", "TSV"
    if first.count(delimiter) < 1:
        return None
    try:
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter, strict=True))
    except csv.Error:
        return None
    if len(rows) < 8:
        return None
    width = len(rows[0])
    if width < 2 or width > 128 or any(len(row) != width for row in rows):
        return None
    line_of_row = list(range(1, len(rows) + 1)) if len(rows) == len(lines) else None
    return _Table(kind, rows, 1, line_of_row)


def _parse_markdown_table(lines: list[str]) -> _Table | None:
    rows: list[list[str]] = []
    for line in lines:
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            return None
        rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
    width = len(rows[0])
    if width < 2 or len(rows[1]) != width:
        return None
    if not all(_MARKDOWN_SEPARATOR_RE.match(cell) for cell in rows[1]):
        return None
    if any(len(row) != width for row in rows[2:]):
        return None
    return _Table("Markdown", rows, 2, list(range(1, len(rows) + 1)))


def _numeric_extremes(rows: list[list[str]], start: int) -> list[int]:
    found: list[int] = []
    if start >= len(rows):
        return found
    for column in range(len(rows[0])):
        lo = hi = -1
        lo_val = hi_val = 0.0
        numeric = 0
        for r in range(start, len(rows)):
            number = _finite_number(rows[r][column].strip())
            if number is None:
                continue
            numeric += 1
            if lo < 0 or number < lo_val:
                lo, lo_val = r, number
            if hi < 0 or number > hi_val:
                hi, hi_val = r, number
        if numeric * 10 < (len(rows) - start) * 7:
            continue  # under 70% numeric: not a numeric column
        for r in (lo, hi):
            if r not in found:
                found.append(r)
    return found


def _numbered(line_no: int | None, text: str) -> str:
    clipped = text if len(text) <= LINE_CHAR_LIMIT else text[:LINE_CHAR_LIMIT] + "…"
    return f"{line_no:6d}\t{clipped}" if line_no is not None else clipped


def _table_preview(table: _Table, budget: int, raw_lines: list[str]) -> str:
    rows = table.rows
    data_rows = len(rows) - table.header_rows
    header_names = rows[0]
    columns = ", ".join(header_names[:12]) + (
        f", … ({len(header_names) - 12} more)" if len(header_names) > 12 else ""
    )
    kind = f"{table.kind} table, {data_rows} rows × {len(header_names)} columns ({columns})"
    docs = [" ".join(row) for row in rows]
    important = [
        i for i in range(table.header_rows, len(rows)) if _TABLE_IMPORTANT_RE.search(docs[i])
    ]
    extremes = _numeric_extremes(rows, table.header_rows)

    def row_text(i: int) -> str:
        if table.line_of_row is not None:
            return raw_lines[table.line_of_row[i] - 1]
        return ",".join(rows[i]) if table.kind != "TSV" else "\t".join(rows[i])

    def line_no(i: int) -> int | None:
        return table.line_of_row[i] if table.line_of_row is not None else None

    def summarize(run: list[int]) -> str:
        if len(run) > SUMMARY_MAX_UNITS:
            return ""
        elided = sum(len(docs[i]) + 1 for i in run)
        return summarize_run([_row_fields(header_names, rows[i]) for i in run], elided)

    return _fit_units(
        kind=kind,
        unit_name="rows",
        n=len(rows),
        pinned=list(range(table.header_rows)),
        start=table.header_rows,
        important=important,
        signal_extra=extremes,
        docs=docs,
        render=lambda i: _numbered(line_no(i), row_text(i)),
        summarize=summarize,
        budget=budget,
        numbered=table.line_of_row is not None,
    )


# ── lines ────────────────────────────────────────────────────────────────────


def _lines_preview(lines: list[str], budget: int) -> str:
    json_lines = sum(
        1 for line in lines if line.strip().startswith("{") and line.strip().endswith("}")
    )
    kind = (
        f"JSON Lines, {len(lines)} lines"
        if json_lines * 10 >= len(lines) * 9
        else f"text, {len(lines)} lines"
    )
    important = [i for i, line in enumerate(lines) if _important_line(line)]

    def summarize(run: list[int]) -> str:
        if len(run) > SUMMARY_MAX_UNITS:
            return ""
        elided = sum(len(lines[i]) + 1 for i in run)
        return summarize_run([_line_fields(lines[i]) for i in run], elided)

    return _fit_units(
        kind=kind,
        unit_name="lines",
        n=len(lines),
        pinned=[],
        start=0,
        important=important,
        signal_extra=[],
        docs=lines,
        render=lambda i: _numbered(i + 1, lines[i]),
        summarize=summarize,
        budget=budget,
        numbered=True,
    )


def _assemble_units(
    keep: list[bool],
    *,
    docs: list[str],
    render: Callable[[int], str],
    summarize: Callable[[list[int]], str],
    unit_name: str,
    numbered: bool,
) -> tuple[list[str], int]:
    """Kept units in order, each left-out run replaced by one marker line."""
    out: list[str] = []
    shown = 0
    run: list[int] = []

    def flush() -> None:
        nonlocal shown
        if not run:
            return
        summary = summarize(run)
        # Numbered views locate a run by the line numbers around it; an
        # unnumbered table states the row span itself (row 1 is the header).
        where = unit_name if numbered else f"{unit_name} {run[0] + 1}-{run[-1] + 1}"
        marker = f"... [{len(run)} {where} left out{': ' + summary if summary else ''}] ..."
        elided_chars = sum(len(docs[i]) + 1 for i in run)
        if _worth_eliding(len(run), len(marker), elided_chars, summary):
            out.append(marker)
        else:
            out.extend(render(i) for i in run)
            shown += len(run)
        run.clear()

    for i, kept in enumerate(keep):
        if kept:
            flush()
            out.append(render(i))
            shown += 1
        else:
            run.append(i)
    flush()
    return out, shown


def _fit_units(
    *,
    kind: str,
    unit_name: str,
    n: int,
    pinned: list[int],
    start: int,
    important: list[int],
    signal_extra: list[int],
    docs: list[str],
    render: Callable[[int], str],
    summarize: Callable[[list[int]], str],
    budget: int,
    numbered: bool,
) -> str:
    """Select, render and fit a line- or row-oriented preview to ``budget``."""
    important_set = set(important)
    candidates = important + [i for i in signal_extra if i not in important_set]
    vocab_cache: dict[int, tuple[frozenset[str], bool]] = {}
    text = ""
    for level in _LEVELS:
        keep = [False] * n
        for i in pinned:
            keep[i] = True
        body = list(range(start, n))
        tail = body[len(body) - level.keep_last :] if level.keep_last else []
        for i in body[: level.keep_first] + tail:
            keep[i] = True
        signal = [i for i in candidates if not keep[i]]
        # Errors are the reason to read a log: spend the signal allowance on
        # them before numeric extremes, and allow more of them than of rows.
        allowance = level.max_signal * 2
        for i in signal[:allowance]:
            keep[i] = True
        signal_dropped = max(0, len(signal) - allowance)
        for i in _first_of_kind(docs, keep, level.max_kinds, vocab_cache):
            keep[i] = True

        out, shown = _assemble_units(
            keep,
            docs=docs,
            render=render,
            summarize=summarize,
            unit_name=unit_name,
            numbered=numbered,
        )

        header = f"Preview ({kind}; {shown} shown"
        if shown < n:
            header += (
                f": the first and last {unit_name}, every {unit_name[:-1]} naming an error or "
                f"warning, and one example of each kind of {unit_name[:-1]}"
            )
            if signal_dropped:
                header += (
                    f" ({signal_dropped} error {unit_name} did not fit; search the file for them)"
                )
            header += (
                f". A line `... [N {unit_name} left out: …] ...` stands in for a run; its summary "
                f"states only what holds for every one of them"
            )
        header += "):"
        text = header + "\n" + "\n".join(out)
        if len(text) <= budget:
            return text
    return text[: max(0, budget - 40)] + "\n…[preview clipped to fit]"


# ── fallback ─────────────────────────────────────────────────────────────────


def head_tail_preview(content: str, *, head_lines: int = 5, tail_lines: int = 5) -> str:
    """Head + tail excerpt with 1-based line numbers, deepagents' layout."""
    lines = content.splitlines()

    def numbered(rows: list[str], first: int) -> str:
        return "\n".join(f"{first + i:6d}\t{row[:1000]}" for i, row in enumerate(rows))

    if len(lines) <= head_lines + tail_lines:
        return numbered(lines, 1)
    head = numbered(lines[:head_lines], 1)
    tail = numbered(lines[-tail_lines:], len(lines) - tail_lines + 1)
    omitted = len(lines) - head_lines - tail_lines
    return f"{head}\n... [{omitted} lines truncated] ...\n{tail}"


def _chars_preview(content: str, budget: int) -> str:
    """Head and tail characters of a payload with too few lines to sample."""
    room = max(200, budget - 160)
    if len(content) <= room:
        return f"Preview (text, {len(content)} chars):\n{content}"
    head = content[: room * 2 // 3]
    tail = content[-(room // 3) :]
    left_out = len(content) - len(head) - len(tail)
    return (
        f"Preview (text, {len(content)} chars; the start and the end are shown):\n"
        f"{head}\n... [{left_out} chars left out] ...\n{tail}"
    )


# ── entry point ──────────────────────────────────────────────────────────────


def build_preview(content: str, *, budget: int = PREVIEW_BUDGET_CHARS) -> str:
    """The preview an offload note shows for ``content``. Never raises."""
    try:
        return _build(content, budget)
    except Exception as exc:  # a preview must never cost the offload
        logger.warning("content-aware preview failed; using head/tail: %s", exc)
        return head_tail_preview(content)


def _build(content: str, budget: int) -> str:
    body = content
    if body.startswith(UNTRUSTED_MARKER):
        # The caller restates the marker at the top of the note; sampling it
        # would only hide the payload's real first line.
        body = body[len(UNTRUSTED_MARKER) :].lstrip("\r\n")
    if len(body) > STRUCTURED_MAX_CHARS:
        if body.count("\n") < 12:
            return _chars_preview(body, budget)
        return head_tail_preview(body)
    stripped = body.strip()
    if stripped[:1] in ("{", "["):
        try:
            value = _loads(stripped)
        except ValueError:
            value = None
        if isinstance(value, dict | list):
            return _json_preview(value, budget)
    lines = body.splitlines()
    table = _parse_table(body)
    if table is not None:
        return _table_preview(table, budget, lines)
    if len(lines) >= 4:
        return _lines_preview(lines, budget)
    return _chars_preview(body, budget)
