"""WebScan middleware — strips hidden prompt injections from web content.

Protects against:
  - Hidden divs / CSS-invisible text with LLM instructions
  - <meta> tag injections
  - HTML comment injections
  - Unicode homoglyph attacks in web content
  - "Ignore previous instructions" patterns embedded in scraped pages
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Patterns that indicate prompt injection in scraped web content
_INJECTION_PATTERNS = [
    # Classic injection phrases in web content
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions?", re.I),
    re.compile(r"disregard\s+(all\s+)?previous\s+(instructions?|context)", re.I),
    re.compile(r"you\s+are\s+now\s+(a|an)\s+\w+", re.I),
    re.compile(r"new\s+instructions?:\s*", re.I),
    re.compile(r"system\s*prompt\s*[:=]", re.I),
    re.compile(r"act\s+as\s+(a|an|if)\s+", re.I),
    re.compile(r"reveal\s+(your\s+)?(system\s+)?prompt", re.I),
    re.compile(r"<\|im_start\|>|<\|im_end\|>|\[INST\]|\[/INST\]", re.I),
    re.compile(r"assistant:\s*sure", re.I),
    # Data exfiltration attempts
    re.compile(r"send\s+(all|your|the)\s+(data|information|files)\s+to", re.I),
    re.compile(r"exfiltrate|data.exfil", re.I),
]

# HTML hidden content patterns (CSS/attribute-based hiding)
_HIDDEN_CSS_PATTERNS = [
    re.compile(r"display\s*:\s*none", re.I),
    re.compile(r"visibility\s*:\s*hidden", re.I),
    re.compile(r"opacity\s*:\s*0", re.I),
    re.compile(r"font-size\s*:\s*0", re.I),
    re.compile(r"color\s*:\s*white.*background.*white", re.I),
    re.compile(r"height\s*:\s*0", re.I),
    re.compile(r"width\s*:\s*0", re.I),
]

# Tags to strip from HTML before LLM processing
_STRIP_TAGS = re.compile(
    r"<(script|style|noscript|iframe|object|embed|applet|form)[^>]*>.*?</\1>", re.I | re.DOTALL
)

# HTML comments (may contain injections)
_HTML_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)

# Meta tag injections
_META_INJECTIONS = re.compile(
    r'<meta[^>]*content\s*=\s*["\'][^"\']*(?:'
    + "|".join(p.pattern for p in _INJECTION_PATTERNS[:4])
    + r')[^"\']*["\'][^>]*>',
    re.I,
)

# Hidden HTML elements with inline CSS
_HIDDEN_ELEMENT = re.compile(
    r'<[a-z][^>]*style\s*=\s*["\'][^"\']*(?:'
    + "|".join(p.pattern for p in _HIDDEN_CSS_PATTERNS)
    + r')[^"\']*["\'][^>]*>.*?</[a-z]+>',
    re.I | re.DOTALL,
)


@dataclass
class WebScanResult:
    original: str
    cleaned: str
    injections_found: list[str] = field(default_factory=list)
    hidden_content_removed: int = 0
    is_safe: bool = True

    @property
    def was_modified(self) -> bool:
        return self.original != self.cleaned


def strip_html_injections(html: str) -> tuple[str, int]:
    """Remove hidden HTML elements that may contain prompt injections.

    Returns (cleaned_html, count_removed).
    """
    removed = 0

    # Remove script/style/iframe/etc
    cleaned = _STRIP_TAGS.sub("", html)

    # Remove HTML comments
    old_len = len(cleaned)
    cleaned = _HTML_COMMENTS.sub("", cleaned)
    if len(cleaned) < old_len:
        removed += 1

    # Remove meta tag injections
    old_len = len(cleaned)
    cleaned = _META_INJECTIONS.sub("", cleaned)
    if len(cleaned) < old_len:
        removed += 1

    # Remove hidden elements with suspicious CSS
    old_len = len(cleaned)
    cleaned = _HIDDEN_ELEMENT.sub("", cleaned)
    removed += max(0, old_len - len(cleaned)) // 50  # rough count

    return cleaned, removed


def scan_web_content(content: str, strip_html: bool = True) -> WebScanResult:
    """Scan web-scraped content for prompt injection attempts.

    Args:
        content: Raw web content (HTML or plain text).
        strip_html: Whether to strip hidden HTML elements first.

    Returns:
        WebScanResult with cleaned content and findings.
    """
    cleaned = content
    hidden_removed = 0
    injections: list[str] = []

    # Step 1: Strip HTML injection vectors
    if strip_html and ("<" in content):
        cleaned, hidden_removed = strip_html_injections(cleaned)

    # Step 2: Extract plain text for pattern scanning
    # Simple tag stripping for text extraction
    text_only = re.sub(r"<[^>]+>", " ", cleaned)
    text_only = re.sub(r"\s+", " ", text_only).strip()

    # Step 3: Scan for injection patterns in the extracted text
    for pattern in _INJECTION_PATTERNS:
        matches = pattern.findall(text_only)
        if matches:
            for match in matches:
                snippet = match if isinstance(match, str) else match[0]
                injections.append(snippet[:80])
            # Remove matching sentences from cleaned content
            cleaned = pattern.sub("[content removed by WebScan]", cleaned)

    is_safe = len(injections) == 0 and hidden_removed == 0

    return WebScanResult(
        original=content,
        cleaned=cleaned,
        injections_found=injections,
        hidden_content_removed=hidden_removed,
        is_safe=is_safe,
    )


def apply_web_scan(content: str, action: str = "clean") -> tuple[str, list[str]]:
    """Apply web scanning with configurable action.

    Args:
        content: Web content to scan.
        action: "clean" (strip injections), "warn" (return original + warnings),
                "block" (raise ValueError if unsafe).

    Returns:
        (processed_content, list_of_findings)
    """
    result = scan_web_content(content)

    if action == "warn":
        return content, result.injections_found

    if action == "block" and not result.is_safe:
        raise ValueError(
            f"Web content blocked: {len(result.injections_found)} injection pattern(s) detected. "
            f"First finding: {result.injections_found[0] if result.injections_found else 'hidden content'}"
        )

    return result.cleaned, result.injections_found
