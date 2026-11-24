"""The dependency-audit waivers live in three places, and they must agree.

- `.github/workflows/ci.yml`: the gate itself (`--ignore-vuln` flags).
- `Makefile`: `make audit`, so a contributor can run the gate locally.
- `SECURITY.md`: "Known advisories that do not affect Olano", the one place
  each waiver's reason, the release that would lift it and its review date
  are written down.

The Makefile already drifted once: CI gained the oauthlib waiver
(CVE-2026-49265) and the Makefile did not, so `make audit` failed on a tree CI
passed. The SECURITY.md list is what a maintainer links when dismissing a
Dependabot alert, so a waiver missing from it is an undocumented one, and an
entry left behind after its fix shipped claims a waiver that no longer exists.
These tests make all three follow each other.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_WAIVER = re.compile(r"--ignore-vuln\s+(\S+)")
_SECURITY_SECTION = "## Known advisories that do not affect Olano"
# A table row whose first cell is a backticked advisory ID: the ID the waiver uses.
_DOCUMENTED = re.compile(r"^\|\s*`([^`]+)`\s*\|", re.MULTILINE)


def _ci_waivers() -> list[str]:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    return _WAIVER.findall(text)


def _make_audit_waivers() -> list[str]:
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("audit:"))
    body: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        body.append(line)
    return _WAIVER.findall("\n".join(body))


def _security_md_waivers() -> list[str]:
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert _SECURITY_SECTION in text, f"SECURITY.md lost its {_SECURITY_SECTION!r} section"
    section = text.split(_SECURITY_SECTION, 1)[1]
    next_h2 = re.search(r"^## ", section, re.MULTILINE)
    if next_h2:
        section = section[: next_h2.start()]
    return _DOCUMENTED.findall(section)


def test_ci_waives_something_so_the_comparison_means_something() -> None:
    assert _ci_waivers(), "the CI audit step lost its waivers or its --ignore-vuln spelling"


def test_make_audit_waives_exactly_what_ci_waives() -> None:
    ci, make = _ci_waivers(), _make_audit_waivers()
    assert len(ci) == len(set(ci)), f"duplicate waiver in ci.yml: {ci}"
    assert sorted(make) == sorted(ci), (
        f"Makefile audit waivers {sorted(make)} differ from ci.yml {sorted(ci)}; "
        "copy the CI list (each waiver's reason lives in SECURITY.md)"
    )


def test_security_md_documents_exactly_what_ci_waives() -> None:
    ci, documented = _ci_waivers(), _security_md_waivers()
    assert len(documented) == len(set(documented)), (
        f"advisory listed twice in SECURITY.md: {documented}"
    )
    undocumented = sorted(set(ci) - set(documented))
    stale = sorted(set(documented) - set(ci))
    assert not undocumented, (
        f"ci.yml waives {undocumented} with no entry in SECURITY.md "
        f"({_SECURITY_SECTION!r}); add a row with the ID in backticks, why Olano "
        "is not affected, and the release that would lift it"
    )
    assert not stale, (
        f"SECURITY.md lists {stale} but ci.yml no longer waives them; remove the "
        "row (the fix shipped) or restore the waiver"
    )
