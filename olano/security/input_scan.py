"""Prompt injection and extraction attack detection."""

from __future__ import annotations

import re

# Patterns that indicate prompt injection attempts
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(your\s+)?system\s+prompt", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(a\s+)?(?:DAN|jailbreak|unrestricted)", re.IGNORECASE),
    re.compile(r"act\s+as\s+if\s+you\s+(have\s+no|don.t\s+have)\s+restrictions?", re.IGNORECASE),
    re.compile(r"pretend\s+you\s+(are\s+)?(?:a\s+)?different\s+(AI|model)", re.IGNORECASE),
    re.compile(r"reveal\s+(your\s+)?(system\s+prompt|instructions?|prompt)", re.IGNORECASE),
    re.compile(r"<\s*(?:system|SYSTEM)\s*>", re.IGNORECASE),  # Fake system tags
    re.compile(r"\[INST\]|\[\/INST\]"),  # Llama instruction injection
]

# Patterns that indicate data exfiltration attempts
_EXFIL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"send\s+(all\s+)?(?:my\s+)?(?:files?|data|credentials?|keys?)\s+to", re.IGNORECASE),
    re.compile(r"exfiltrate|exfil\b", re.IGNORECASE),
    re.compile(r"http[s]?://\S+\.(ngrok|requestbin|webhook)", re.IGNORECASE),
    re.compile(r"curl\s+-[xXdD]?\s*(?:POST|GET)\s+http", re.IGNORECASE),
]


class ScanResult:
    def __init__(self, text: str, findings: list[dict]) -> None:
        self.text = text
        self.findings = findings

    @property
    def is_safe(self) -> bool:
        return not self.findings

    @property
    def risk_level(self) -> str:
        if not self.findings:
            return "none"
        types = {f["category"] for f in self.findings}
        if "injection" in types:
            return "high"
        return "medium"


def scan_input(text: str) -> ScanResult:
    """Scan user input for prompt injection patterns."""
    findings: list[dict] = []
    for pattern in _INJECTION_PATTERNS:
        m = pattern.search(text)
        if m:
            findings.append(
                {"category": "injection", "pattern": pattern.pattern, "match": m.group()}
            )
    for pattern in _EXFIL_PATTERNS:
        m = pattern.search(text)
        if m:
            findings.append(
                {"category": "exfiltration", "pattern": pattern.pattern, "match": m.group()}
            )
    return ScanResult(text=text, findings=findings)


def scan_output(text: str) -> ScanResult:
    """Scan agent output for potential secret/credential leakage."""
    from olano.security.pii_middleware import _PATTERNS as PII_PATTERNS

    findings: list[dict] = []
    for pii_type, pattern in PII_PATTERNS.items():
        if pii_type in ("private_key", "api_key_generic"):
            for m in pattern.finditer(text):
                findings.append(
                    {"category": "credential_leak", "type": pii_type, "match": m.group()}
                )
    return ScanResult(text=text, findings=findings)
