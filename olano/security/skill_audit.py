"""Skill security audit — scan SKILL.md files before activation."""

from __future__ import annotations

import re
from pathlib import Path

# Patterns that flag a skill as potentially dangerous.
#
# The execution patterns (eval/exec/os.system/subprocess) require *call/usage*
# syntax, not a bare mention: a SKILL.md is mostly prose, and technical docs
# routinely say "run evals" or "spawns a subprocess" without shipping any
# executable payload. `eval\s*\(` / `exec\s*\(` / `os\.system\s*\(` already
# need a paren; `subprocess` likewise requires an ``import`` or a
# ``subprocess.<attr>`` access so the bare English word (e.g. "uses `claude -p`
# via subprocess, not a browser") no longer trips a "high" finding and skips
# the skill. `rm -rf` and pipe-to-shell stay intentionally broad — those are
# dangerous as a prose *instruction* to the agent, not only as literal code.
_DANGEROUS_PATTERNS: list[tuple[str, str, str]] = [
    (r"\beval\s*\(", "eval() call detected", "high"),
    (r"\bexec\s*\(", "exec() call detected", "high"),
    (r"\bos\.system\s*\(", "os.system() call", "high"),
    (
        r"(?:\bimport\s+subprocess\b|\bfrom\s+subprocess\b|\bsubprocess\.\w+)",
        "subprocess usage",
        "high",
    ),
    (r"\brm\s+-rf", "destructive rm -rf", "high"),
    (r"curl\s+\S+\s*\|\s*(?:bash|sh|python)", "pipe-to-shell pattern", "high"),
    (r"https?://(?:bit\.ly|tinyurl|goo\.gl)", "URL shortener (obfuscation risk)", "medium"),
]

# Hardcoded-credential detection is handled separately from the simple pattern
# list so documentation placeholders don't trip a false "high" finding. Many
# legitimate API skills (X/Twitter, Binance, …) show example assignments like
# ``api_key = "YOUR_API_KEY"`` or ``token = "${X_BEARER_TOKEN}"`` — those are not
# secrets and must not block (or even warn about) the skill.
_CREDENTIAL_RE = re.compile(
    r"(?:password|passwd|secret|apikey|api_key|access_token|auth_token|bearer)"
    r"\s*[:=]\s*['\"]([^'\"]*)['\"]",
    re.IGNORECASE,
)

# Tokens that mark a captured value as a placeholder rather than a real secret.
_PLACEHOLDER_MARKERS = (
    "your",
    "xxx",
    "...",
    "<",
    ">",
    "example",
    "placeholder",
    "changeme",
    "change_me",
    "change-me",
    "redacted",
    "todo",
    "dummy",
    "sample",
    "env",
    "${",
    "{{",
    "os.environ",
    "getenv",
    "process.env",
    "*",
)


def _is_placeholder_secret(value: str) -> bool:
    """True when a quoted credential value is an obvious doc placeholder."""
    v = value.strip()
    if len(v) < 8:  # too short to be a real key/token
        return True
    low = v.lower()
    if any(marker in low for marker in _PLACEHOLDER_MARKERS):
        return True
    # Filler runs like "--------" or "########" (≤2 distinct chars).
    return len(set(v)) <= 2


class AuditResult:
    def __init__(self, skill_name: str, findings: list[dict], approved: bool) -> None:
        self.skill_name = skill_name
        self.findings = findings
        self.approved = approved

    @property
    def is_safe(self) -> bool:
        return self.approved and not any(f["severity"] == "high" for f in self.findings)


def _is_blocking(severity: str, level: str) -> bool:
    if level == "permissive":
        return severity == "high"
    if level == "paranoid":
        return severity in {"high", "medium"}
    return severity == "high"


def audit_skill(skill_path: Path, *, level: str = "strict") -> AuditResult:
    """Audit a SKILL.md file for dangerous patterns."""
    skill_name = skill_path.parent.name
    findings: list[dict] = []

    if not skill_path.exists():
        return AuditResult(skill_name=skill_name, findings=[], approved=True)

    content = skill_path.read_text(encoding="utf-8")

    for pattern, description, severity in _DANGEROUS_PATTERNS:
        matches = re.findall(pattern, content, re.IGNORECASE)
        if matches:
            findings.append(
                {
                    "severity": severity,
                    "pattern": pattern,
                    "description": description,
                    "occurrences": len(matches),
                }
            )

    # Hardcoded credentials — skip documentation placeholders (see _CREDENTIAL_RE).
    real_creds = [m for m in _CREDENTIAL_RE.findall(content) if not _is_placeholder_secret(m)]
    if real_creds:
        findings.append(
            {
                "severity": "high",
                "pattern": _CREDENTIAL_RE.pattern,
                "description": "hardcoded credential",
                "occurrences": len(real_creds),
            }
        )

    # Warn on external HTTP calls
    http_calls = re.findall(r"https?://\S+", content)
    if len(http_calls) > 5:
        findings.append(
            {
                "severity": "medium",
                "description": f"Many external URLs ({len(http_calls)}) — review carefully",
                "urls": http_calls[:5],
            }
        )

    approved = not any(_is_blocking(str(f["severity"]), level) for f in findings)
    return AuditResult(skill_name=skill_name, findings=findings, approved=approved)


def audit_skills_dir(skills_dir: Path, *, level: str = "strict") -> list[AuditResult]:
    """Audit all SKILL.md files in a directory tree."""
    results: list[AuditResult] = []
    for skill_md in skills_dir.rglob("SKILL.md"):
        results.append(audit_skill(skill_md, level=level))
    return results
