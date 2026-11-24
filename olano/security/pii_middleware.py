"""PII scanning middleware — detect and optionally redact personal data."""

from __future__ import annotations

import os
import re

# Common PII patterns
_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    "phone_us": re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "ip_address": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "private_key": re.compile(
        r"(?:-----BEGIN (?:RSA |EC )?PRIVATE KEY-----)|(?:0x[0-9a-fA-F]{40,})"
    ),
    "api_key_generic": re.compile(
        r"\b(?:sk|pk|api|key|token|secret|auth)[_-]?[0-9a-zA-Z]{16,}\b",
        re.IGNORECASE,
    ),
}

_REDACT_PLACEHOLDER = "[REDACTED]"

# Emails at these domains (or exact addresses) are the business's own public
# contact points, not customer PII — never flagged or redacted. Extend via
# LIONCLAW_PII_EMAIL_ALLOWLIST (comma-separated domains and/or full addresses).
_DEFAULT_EMAIL_ALLOWLIST = frozenset({"olano.ai"})
_EMAIL_ALLOWLIST_ENV = "LIONCLAW_PII_EMAIL_ALLOWLIST"

# Chat platforms address users as ``<opaque-id>@<namespace>``, and several of
# those namespaces are shaped exactly like a mail domain — so the sender tag the
# runtime prepends to every inbound message ("[from id=…@im.wechat]") reads as an
# email address to the pattern above. That misfires on ordinary traffic: a
# warning logged per message, the sender id redacted out of the agent's own
# input under ``pii_action: redact``, and — worst — **every message from these
# platforms rejected** under ``pii_action: block``. These are routing handles,
# never a person's mailbox, so they are not PII and never were.
#
# Matched as the WHOLE namespace after the last ``@``, never as a suffix, so a
# lookalike domain (``someone@evil-g.us.com``) is still treated as an email.
# Keep the list to namespaces a platform adapter actually mints (grep the
# adapters before adding). Note this exempts only the *email* pattern: a
# WhatsApp JID's local part really is the user's phone number, and it keeps
# being flagged as one — that is a genuine detection, not a false positive.
_PLATFORM_ID_NAMESPACES = frozenset(
    {
        "im.wechat",  # weixin / iLink — DM + room peers
        "s.whatsapp.net",  # WhatsApp user JID
        "g.us",  # WhatsApp group JID
        "c.us",  # WhatsApp legacy user JID
    }
)


def _email_allowlist() -> frozenset[str]:
    extra = os.environ.get(_EMAIL_ALLOWLIST_ENV, "")
    entries = {e.strip().lstrip("@").lower() for e in extra.split(",") if e.strip()}
    return _DEFAULT_EMAIL_ALLOWLIST | frozenset(entries)


def _is_platform_id(match: str) -> bool:
    """True for a chat platform's ``<id>@<namespace>`` routing handle."""
    return match.lower().rpartition("@")[2] in _PLATFORM_ID_NAMESPACES


def _is_allowlisted_email(match: str, allowlist: frozenset[str]) -> bool:
    addr = match.lower()
    if addr.rpartition("@")[2] in allowlist or addr in allowlist:
        return True
    return _is_platform_id(addr)


def _luhn_ok(digits: str) -> bool:
    """Return True iff ``digits`` passes the Luhn checksum.

    Real payment-card numbers (Visa/Mastercard/Amex/Discover) always satisfy
    Luhn; long numeric IDs that merely *look* card-shaped — social-media post
    ids (X status, LinkedIn share URN), order/tracking numbers, phone runs —
    pass only ~10% of the time. Gating the ``credit_card`` pattern on this kills
    the bulk of false positives without dropping a single genuine card (they all
    pass Luhn), so ``redact`` no longer corrupts such content and ``warn``
    stops crying wolf.
    """
    total = 0
    # Double every second digit counting from the right (Amex is 15 = odd len,
    # Visa 16 = even; parity keys the doubling off the string length).
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


#: Characters that glue a digit run into a file name or path (``voice-<stamp>.wav``,
#: ``uploads/<stamp>.jpg``, ``IMG_<digits>.png``).
_PATH_GLUE = frozenset("-_/\\")


def _is_path_token(text: str, start: int, end: int) -> bool:
    """True when the digits at ``text[start:end]`` are part of a file name or path.

    The runtime names every saved voice note ``voice-<unix timestamp>.wav``, and
    a ten-digit timestamp is exactly what the ``phone_us`` pattern matches, so
    the ``[Audio attached: voice-1792547952.wav, saved to .../uploads/...]`` note
    the runtime itself appends to a voice turn came back to the agent as
    ``voice-[PHONE_US_REDACTED].wav`` with a saved-to path that exists nowhere.
    A digit run that continues a token through a path separator or an
    underscore/hyphen, or that is followed by a dotted extension, is a file
    name, not a phone number; a phone in prose (``call 555-123-4567``, or
    ``5551234567.`` ending a sentence) is bounded by spaces or punctuation
    followed by whitespace and stays a finding.
    """
    if start > 0 and text[start - 1] in _PATH_GLUE:
        return True
    return end + 1 < len(text) and text[end] == "." and text[end + 1].isalnum()


def _is_credit_card(match: str) -> bool:
    """A candidate ``credit_card`` regex hit is a real card only if, once the
    space/dash separators are stripped, it is 13–19 digits AND passes Luhn."""
    digits = re.sub(r"[ \-]", "", match)
    return 13 <= len(digits) <= 19 and _luhn_ok(digits)


class PIIResult:
    def __init__(self, original: str, cleaned: str, findings: list[dict]) -> None:
        self.original = original
        self.cleaned = cleaned
        self.findings = findings  # list of {"type": str, "match": str, "start": int, "end": int}

    @property
    def has_pii(self) -> bool:
        return bool(self.findings)


def scan(text: str) -> PIIResult:
    """Scan text for PII. Returns a PIIResult with findings."""
    findings: list[dict] = []
    allowlist = _email_allowlist()
    for pii_type, pattern in _PATTERNS.items():
        for match in pattern.finditer(text):
            if pii_type == "email" and _is_allowlisted_email(match.group(), allowlist):
                continue
            # Long numeric IDs (social-media post ids, tracking numbers) look
            # card-shaped but aren't; require a valid Luhn checksum before we
            # flag/redact them as a credit card.
            if pii_type == "credit_card" and not _is_credit_card(match.group()):
                continue
            if pii_type == "phone_us" and _is_path_token(text, match.start(), match.end()):
                continue
            findings.append(
                {
                    "type": pii_type,
                    "match": match.group(),
                    "start": match.start(),
                    "end": match.end(),
                }
            )
    return PIIResult(original=text, cleaned=text, findings=findings)


def redact(text: str) -> PIIResult:
    """Scan and redact PII from text."""
    result = scan(text)
    cleaned = text
    # Redact in reverse order to preserve offsets
    for finding in sorted(result.findings, key=lambda f: f["start"], reverse=True):
        placeholder = f"[{finding['type'].upper()}_REDACTED]"
        cleaned = cleaned[: finding["start"]] + placeholder + cleaned[finding["end"] :]
    return PIIResult(original=text, cleaned=cleaned, findings=result.findings)


def peer_exchange_exempt(security: object) -> bool:
    """True when PII scanning should be skipped for the current exchange
    because it is agent↔agent traffic (a ``talk_to_agent`` / ``talk_to`` hop,
    which always runs under request-context ``source="peer"``).

    Peer exchanges stay inside the runtime's trust boundary — both agents run
    in the same process for the same operator — and scanning them breaks
    legitimate collaboration (a customer email handed to the billing agent
    arrives redacted, or the whole hand-off is blocked outright). The
    human-facing boundary is unaffected: the caller's own output scan still
    runs when it finally replies to a human.

    Agents can opt back into peer scanning with ``security.pii_scan_peer: true``.
    Injection scanning and credential scrubbing are NOT exempted — only PII.
    """
    if getattr(security, "pii_scan_peer", False):
        return False
    from olano.security.request_context import get_request_context

    return get_request_context().source == "peer"


def apply(text: str, action: str) -> tuple[str, list[dict]]:
    """
    Apply PII action to text.

    action: "warn" | "redact" | "block"
    Returns (processed_text, findings).
    Raises ValueError if action="block" and PII is found.
    """
    result = scan(text)
    if not result.has_pii:
        return text, []

    if action == "redact":
        result = redact(text)
        return result.cleaned, result.findings
    if action == "block":
        types = {f["type"] for f in result.findings}
        raise ValueError(f"PII detected in input: {', '.join(types)}. Message blocked.")
    # "warn" — return as-is, caller logs findings
    return text, result.findings
