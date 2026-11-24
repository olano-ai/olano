"""Credential scrubbing for tool and model outputs."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

_json5: ModuleType | None
try:
    import json5 as _json5
except ImportError:  # pragma: no cover - optional dependency
    _json5 = None


@dataclass(frozen=True)
class ScrubPattern:
    type: str
    regex: re.Pattern[str]
    replacement: str


_DEFAULT_PATTERNS = [
    ScrubPattern(
        type="api_key",
        regex=re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
        replacement="[REDACTED:api_key]",
    ),
    ScrubPattern(
        type="github_token",
        regex=re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        replacement="[REDACTED:github_token]",
    ),
    ScrubPattern(
        type="bearer_token",
        regex=re.compile(r"\bBearer\s+[A-Za-z0-9._=-]{16,}\b", re.IGNORECASE),
        replacement="[REDACTED:bearer_token]",
    ),
    ScrubPattern(
        type="aws_access_key",
        regex=re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        replacement="[REDACTED:aws_access_key]",
    ),
    ScrubPattern(
        type="connection_string",
        regex=re.compile(r"\b(?:postgres|mysql|mongodb(?:\+srv)?|redis)://[^\s]+", re.IGNORECASE),
        replacement="[REDACTED:connection_string]",
    ),
]
# Path separators ('/' and '\') are deliberately excluded from the token class
# so that filesystem paths and URLs split into individual segments before the
# entropy heuristic ever sees them. A path like
# ``/Users/me/.lionclaw/workspace/downloads/GIC_AR_2024-25_PRINT.pdf`` would
# otherwise match as one high-entropy token and be redacted wholesale, leaving
# the user unable to find their file. Known opaque-secret formats (api keys,
# bearer/github tokens, connection strings) are already caught by the explicit
# patterns above; this regex is only a backstop for contiguous tokens.
_HIGH_ENTROPY_TOKEN_RE = re.compile(r"\b[A-Za-z0-9+=_-]{24,}\b")
_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
# Excluding the separators is what splits an address into segments, but it also
# means the heuristic never sees the ``/`` that identifies one: by the time a
# token reaches the predicate below, ``https://host/api/connect/<token>`` has
# already become the bare ``<token>``, so the URL test inside it can never fire.
# The context therefore has to come from the surrounding text. It has to,
# because a link the deployment hands the user *on purpose* is high-entropy by
# construction — a magic link is ``/api/connect/`` + 256 random bits, an OAuth
# authorization URL carries ``state`` and ``code_challenge`` — and redacting
# the random part turns the Connect button into a 404 every single time.
# Anything in a URL that really is a credential has a known shape and is
# already gone: the explicit patterns above run first, over the whole text,
# inside URLs included.
_URL_SPAN_RE = re.compile(r"(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)[^\s\"'<>`\\]*", re.IGNORECASE)
# A container, not a string: ``"" in "/\\"`` is True, so a str here would exempt
# every token that starts or ends the text — i.e. most bare secrets.
_PATH_SEPARATORS = frozenset({"/", "\\"})


def _shannon_entropy(token: str) -> float:
    freq = {ch: token.count(ch) / len(token) for ch in set(token)}
    return -sum(p * math.log2(p) for p in freq.values())


def _load_patterns_from_path(path: str | None) -> list[ScrubPattern]:
    if not path:
        return []
    candidate = Path(path)
    if not candidate.exists():
        return []
    if _json5 is None:
        return []
    try:
        payload = _json5.loads(candidate.read_text(encoding="utf-8"))
    except Exception:
        return []
    patterns: list[ScrubPattern] = []
    if not isinstance(payload, list):
        return patterns
    for item in payload:
        if not isinstance(item, dict):
            continue
        regex = item.get("regex")
        scrub_type = item.get("type")
        replacement = item.get("replacement")
        if not isinstance(regex, str) or not isinstance(scrub_type, str):
            continue
        patterns.append(
            ScrubPattern(
                type=scrub_type,
                regex=re.compile(regex),
                replacement=replacement or f"[REDACTED:{scrub_type}]",
            )
        )
    return patterns


def _in_address_context(text: str, start: int, end: int, url_spans: list[tuple[int, int]]) -> bool:
    """Is the token at ``[start:end)`` part of a URL or a filesystem path?

    A path segment is recognized by the separator the token class dropped:
    ``/Users/me/downloads/<long-name>.pdf`` reaches the heuristic as the bare
    ``<long-name>``, and redacting that leaves the user unable to find their
    own file — the same defect as a redacted link, one address kind over.
    """
    if any(span_start <= start and end <= span_end for span_start, span_end in url_spans):
        return True
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    return before in _PATH_SEPARATORS or after in _PATH_SEPARATORS


def _looks_sensitive_entropy_token(token: str) -> bool:
    # URLs, filesystem paths, and UUIDs are not credentials — never redact them.
    # The first test is unreachable while the token class excludes separators
    # (``_in_address_context`` is what actually covers addresses); it stays as a
    # guard in case that class ever grows one back.
    if token.startswith(("http", "www")) or "/" in token or "\\" in token:
        return False
    if _UUID_RE.match(token):
        return False
    categories = sum(
        [
            any(ch.islower() for ch in token),
            any(ch.isupper() for ch in token),
            any(ch.isdigit() for ch in token),
            any(ch in "+/=_-" for ch in token),
        ]
    )
    return categories >= 3 and _shannon_entropy(token) >= 3.5


def scrub_text(text: str, patterns_path: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Redact common credential formats from text."""
    findings: list[dict[str, Any]] = []
    cleaned = text

    for pattern in [*_DEFAULT_PATTERNS, *_load_patterns_from_path(patterns_path)]:
        for match in pattern.regex.finditer(cleaned):
            findings.append({"type": pattern.type, "match": match.group(0)})
        cleaned = pattern.regex.sub(pattern.replacement, cleaned)

    # Substitute in one pass rather than replacing token-by-token: the offsets
    # a match reports are only valid against the string it was found in, and
    # the URL spans are read by offset.
    url_spans = [match.span() for match in _URL_SPAN_RE.finditer(cleaned)]

    def _redact_entropy(match: re.Match[str]) -> str:
        token = match.group(0)
        start, end = match.span()
        if _in_address_context(cleaned, start, end, url_spans):
            return token
        if not _looks_sensitive_entropy_token(token):
            return token
        findings.append({"type": "high_entropy", "match": token})
        return "[REDACTED:high_entropy]"

    cleaned = _HIGH_ENTROPY_TOKEN_RE.sub(_redact_entropy, cleaned)

    return cleaned, findings
